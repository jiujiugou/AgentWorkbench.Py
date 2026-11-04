"""不变量：api_key 只以密文落库，配置失效必须显式报错（S-V6 / V7 / V8 / V9）。

被改坏时**用户**看到什么：
- encrypt 退化成恒等 / 读接口回传密文 → 库文件或抓包即凭据外泄；
- 缺 AWB_SECRET_KEY 服务照常启动 → 凭据以明文躺在库里，没有任何东西会红，直到那一天；
- 唯一激活只剩应用层事务 → 并发或手工 SQL 造出两个激活行，`resolve` 取 id 最小的
  那条，另一行静默失效（C# 版的真实历史）；
- 空 api_key 兜底成占位串继续答（llm 层旧实现）→ "忘了配钥匙"被伪装成
  "答了但答错"，用户拿到一段模型编的答案，而正确形态是一次 503。

分层说明（写给改这段代码的人）：db 与 llm 同层、互不 import——解密唯一一跳在
api 装配层（`deps._chat_for`），四种未配置形态的行级判别在仓储
（`resolve_active_row`）。测试的"零请求"断言用替换 `llm.providers.OpenAI` 的方式：
配置错误的路径上**连客户端对象都不许构造**，比"没收到请求"更硬——先构造再放弃
的实现（发出去才 401）在这条断言下藏不住。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from conftest import sign_in
from fastapi.testclient import TestClient

from agentworkbench.api.app import create_app
from agentworkbench.api.deps import Deps, build_deps
from agentworkbench.config import Settings
from agentworkbench.core.errors import AppError, ErrorCode
from agentworkbench.db import crypto, engine
from agentworkbench.db.crypto import MissingSecretKey

pytestmark = pytest.mark.invariant

SECRET = "unit-test-secret-key"  # noqa: S105 —— 测试用的假密钥，不是真实凭据
PLAINTEXT = "sk-live-abc123"
SENTINEL_KEY = "sk-sentinel-do-not-echo-9012"


# ── 原语层：db/crypto ──


def test_api_key_roundtrips_and_plaintext_never_stored() -> None:
    """往返成立，且密文里找不到明文——"加密"最起码的含义。"""
    ciphertext = crypto.encrypt(SECRET, PLAINTEXT)

    assert ciphertext != PLAINTEXT
    assert PLAINTEXT not in ciphertext
    assert crypto.decrypt(SECRET, ciphertext) == PLAINTEXT


def test_missing_secret_key_is_rejected() -> None:
    """空密钥必须明确拒绝，不许静默给默认值或退回明文。

    `require_secret` 是启动路径的关卡：过不去就起不来——
    这是判据要求的「明确失败」，不是遗漏。
    """
    with pytest.raises(MissingSecretKey):
        crypto.require_secret("")
    with pytest.raises(MissingSecretKey):
        crypto.require_secret(None)
    assert crypto.require_secret("some-secret") == "some-secret"


def test_decrypt_failure_raises_instead_of_returning_empty() -> None:
    """解不开必须报错，不许返回空串冒充"这个端点没配钥匙"。

    返回空串与解密失败是两件事：前者合法（本地端点），后者说明密钥或库被动过。
    混成一个返回值，第二种情况会静默变成第一种，用户只看到一次神秘 503。
    """
    ciphertext = crypto.encrypt("the-original-secret", PLAINTEXT)

    with pytest.raises(ValueError, match="不匹配"):
        crypto.decrypt("a-different-secret", ciphertext)


# ── 启动与存储面 ──


class Harness:
    """真库 + 真装配。供应商配置经 HTTP 建出来——直接 SQL 造行绕过了加密，
    而 S-V6 判的是"写入路径上的密文"，绕过去就测了个寂寞。"""

    def __init__(self, tmp_path: Path) -> None:
        self.settings = Settings(data_dir=tmp_path / "data")
        self.deps: Deps = build_deps(self.settings)
        self.app = create_app(settings=self.settings, deps=self.deps)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        sign_in(self.client, self.app)

    def open_direct(self) -> sqlite3.Connection:
        """绕过 HTTP 直连库——负控专用（S-V6 的 select / S-V7/V8 的插行）。"""
        conn = engine.connect(self.settings.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def create_provider(
        self,
        *,
        name: str = "main",
        base_url: str = "http://127.0.0.1:9/v1",
        api_key: str = SENTINEL_KEY,
        model: str | None = None,
        activate: bool = False,
    ) -> int:
        response = self.client.post(
            "/api/settings/providers",
            json={"name": name, "baseUrl": base_url, "apiKey": api_key},
        )
        assert response.status_code == 201, response.text
        provider_id = int(response.json()["id"])
        if model is not None:
            added = self.client.post(
                f"/api/settings/providers/{provider_id}/models",
                json={"model": model, "isDefault": True},
            )
            assert added.status_code == 201, added.text
        if activate:
            activated = self.client.post(f"/api/settings/providers/{provider_id}/activate")
            assert activated.status_code == 200, activated.text
        return provider_id


@pytest.fixture()
def h(tmp_path: Path) -> Iterator[Harness]:
    yield Harness(tmp_path)


class NoNetwork:
    """替换掉 OpenAI 构造函数：配置错误的路径上连客户端都不许建。

    记录而非直接抛：抛出去会变成 500，把"配置错误"淹没成另一种错误。
    用例末尾断言 `built == []`，比"没收到请求"硬一档——先构造再放弃
    （发出去才 401）的实现在这里现形。
    """

    # 类属性而不是实例属性：替换 OpenAI 构造函数时拿不到 self，只能挂类上收集
    built: ClassVar[list[str]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        NoNetwork.built.append(str(kwargs.get("base_url", "")))


@pytest.fixture()
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    NoNetwork.built = []
    monkeypatch.setattr("agentworkbench.llm.providers.OpenAI", NoNetwork)
    yield NoNetwork.built


def _ask(h: Harness) -> Any:
    """走真实 ask 入口（模型解析在 start_turn 之前）。"""
    return h.client.post("/api/knowledge/ask", json={"question": "波特率怎么设置？"})


def test_direct_sql_select_cannot_read_plaintext(h: Harness) -> None:
    """S-V6 端到端：经 HTTP 写入的 api_key，直接 SQL 读出来的是密文。

    这条的负控是 `encrypt` 退化成恒等——那时这一列就是明文本身，
    断言 `SENTINEL_KEY not in ciphertext` 当场红。
    """
    provider_id = h.create_provider()

    conn = h.open_direct()
    try:
        row = conn.execute(
            "select api_key_encrypted from providers where id = ?", (provider_id,)
        ).fetchone()
    finally:
        conn.close()

    ciphertext = str(row["api_key_encrypted"])
    assert ciphertext, "空串意味着'根本没存'——凭据丢了，不是加密了"
    assert SENTINEL_KEY not in ciphertext
    assert crypto.decrypt(str(h.settings.secret_key), ciphertext) == SENTINEL_KEY


def test_missing_secret_key_fails_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S-V6：不配 AWB_SECRET_KEY，`build_deps` 拒绝启动——不是第一个请求才炸。"""
    monkeypatch.delenv("AWB_SECRET_KEY", raising=False)
    settings = Settings(data_dir=tmp_path / "data-no-secret")

    with pytest.raises(MissingSecretKey):
        build_deps(settings)


def test_direct_sql_cannot_create_two_active_providers(h: Harness) -> None:
    """S-V7 负控：绕过仓储直接打 SQL，第二行 is_active=1 必须被库拒绝。

    去掉 `ux_providers_active` 这条就绿——那时 C# 版的事故原样复现：
    两个激活行，resolve 取 id 小的，另一行静默失效。
    """
    h.create_provider(activate=True)

    conn = h.open_direct()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "insert into providers (name, base_url, api_key_encrypted, is_active,"
                " created_at, updated_at) values ('second', 'http://b/v1', '', 1, 't', 't')"
            )
            conn.commit()
    finally:
        conn.close()


def test_activate_switches_exactly_one_row(h: Harness) -> None:
    """S-V7 正控：切换后全表恰好一行激活，且是新的那行。"""
    first = h.create_provider(name="first", activate=True)
    second = h.create_provider(name="second", activate=True)

    conn = h.open_direct()
    try:
        rows = conn.execute("select id from providers where is_active = 1").fetchall()
    finally:
        conn.close()

    assert [int(r["id"]) for r in rows] == [second]
    assert first != second


def test_default_model_is_unique_per_provider(h: Harness) -> None:
    """S-V8 负控：同一供应商第二个默认模型必须被 `ux_provider_models_default` 拒绝。"""
    provider_id = h.create_provider()

    conn = h.open_direct()
    try:
        conn.execute(
            "insert into provider_models (provider_id, model, is_default, created_at)"
            " values (?, 'glm-4-flash', 1, 't')",
            (provider_id,),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "insert into provider_models (provider_id, model, is_default, created_at)"
                " values (?, 'glm-4-plus', 1, 't')",
                (provider_id,),
            )
            conn.commit()
    finally:
        conn.close()


def test_same_model_cannot_be_added_twice(h: Harness) -> None:
    """S-V8 负控：同 (provider, model) 第二次入库必须被 UNIQUE 拒绝。

    重复行的危害在 `resolve_active_row` 的 `order by ... limit 1`：
    两行都可能是默认，取哪行不可预期。
    """
    provider_id = h.create_provider()

    conn = h.open_direct()
    try:
        conn.execute(
            "insert into provider_models (provider_id, model, is_default, created_at)"
            " values (?, 'glm-4-flash', 0, 't')",
            (provider_id,),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "insert into provider_models (provider_id, model, is_default, created_at)"
                " values (?, 'glm-4-flash', 0, 't')",
                (provider_id,),
            )
            conn.commit()
    finally:
        conn.close()


def test_no_read_response_carries_the_credential(h: Harness) -> None:
    """S-V8：读接口全树无凭据——明文、密文、连字段名都不许出现。

    字段名也在禁列里：`"apiKey": null` 这样的键告诉读者"这里有个凭据"，
    是在为将来"顺手把它填上"铺路。白名单 DTO 的意义就是默认不出去。
    """
    h.create_provider()

    response = h.client.get("/api/settings/providers")

    assert response.status_code == 200
    assert SENTINEL_KEY not in response.text
    assert "apiKey" not in response.text
    assert "api_key" not in response.text
    body = response.json()
    assert body["providers"][0]["hasApiKey"] is True, "界面需要'配没配'，但只需要布尔"


def test_empty_api_key_is_an_error_not_a_placeholder(h: Harness, no_network: list[str]) -> None:
    """S-V9：空 api_key → 明确 503，且零请求（连客户端对象都没构造）。

    旧行为是把空 key 兜底成占位串 "not-required" 继续答——"忘了配钥匙"
    被伪装成"答了但答错"。这条红的时候说明有人把兜底加回去了。
    """
    h.create_provider(api_key="", model="glm-4-flash", activate=True)

    response = _ask(h)

    assert response.status_code == 503
    assert response.json()["code"] == "model_not_configured"
    assert "api_key" in response.json()["message"]
    assert no_network == [], "配置错误的路径上连客户端对象都不许构造"


def test_missing_default_model_is_an_error(h: Harness, no_network: list[str]) -> None:
    """S-V9：激活但没配默认模型 → 明确 503，message 指到该改的地方。

    C# `ResolveAsync:120` 在这里落回 `_fallback.Model`——静默换了模型继续答，
    用户以为在跟 A 模型说话。零请求断言同上条。
    """
    h.create_provider(activate=True)  # 不传 model

    response = _ask(h)

    assert response.status_code == 503
    assert response.json()["code"] == "model_not_configured"
    assert "默认" in response.json()["message"]
    assert no_network == []


def test_unreachable_endpoint_is_model_unavailable() -> None:
    """S-V9 第三种失效：base_url 不可达 → MODEL_UNAVAILABLE（retryable），不静默兜底。

    直调客户端：不可达是**网络**失效，与配置失效分属两个错误码
    （一个可重试一个不可重试），合并它们运维就分不清"改配置"还是"等服务"。
    max_retries=0 让"不可达"立刻失败，测试不等重试退避。
    """
    from openai import OpenAI

    from agentworkbench.llm.client import OpenAIChatClient
    from agentworkbench.llm.providers import ChatEndpoint

    dead = OpenAIChatClient(
        ChatEndpoint(
            provider_id=1, name="dead", base_url="http://127.0.0.1:1/v1", api_key="k", model="m"
        ),
        client=OpenAI(api_key="k", base_url="http://127.0.0.1:1/v1", max_retries=0),
    )

    with pytest.raises(AppError) as ei:
        dead.complete("system", "user")

    assert ei.value.code is ErrorCode.MODEL_UNAVAILABLE
    assert ei.value.spec.retryable
