"""不变量：设置页背后的那组端点（D7 补的）。

D7 UI 验收发现前端按 C# 那版契约调了 8 条设置端点，而 Python 版只有 `GET /providers`
一条——列表形状还不对（后端 `{providers: [...]}`、前端按裸数组解析）。结果是：
配好的模型在界面上永远显示「未配置模型」，设置页点任何按钮都 404。

这组测试守三件事，缺一条这个页面就还是坏的：

1. **形状**：列表是 `{providers: []}`，预览字段齐全（缺字段 → 界面读到 undefined）。
2. **凭据不进响应**：预览不说 key，探测失败文案也不带 key（S-V5）。
3. **写操作真的落库**：改、删、设默认、删模型各自产生可观察的终态。

改坏时会怎样：把 `update_provider` 的"空 key 不覆盖"去掉 → 改个名字把钥匙清空，
下一次作答 503；把 `set_default_model` 的"先清后置"去掉 → 两行默认，唯一索引拒绝，
用户点「设为默认」直接 500。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import sign_in
from fastapi.testclient import TestClient

from agentworkbench.api.app import create_app
from agentworkbench.api.deps import Deps, build_deps
from agentworkbench.config import Settings
from agentworkbench.db import crypto, engine

pytestmark = pytest.mark.invariant

SECRET = "unit-test-secret-key"  # noqa: S105 —— 测试用的假密钥
KEY = "sk-settings-surface-0042"


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.settings = Settings(data_dir=tmp_path / "data")
        self.deps: Deps = build_deps(self.settings)
        self.app = create_app(settings=self.settings, deps=self.deps)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        sign_in(self.client, self.app)

    def conn(self) -> sqlite3.Connection:
        return engine.connect(self.settings.db_path)

    def provider(self, *, name: str = "main", model: str = "glm-4-flash", key: str = KEY) -> int:
        created = self.client.post(
            "/api/settings/providers",
            json={"name": name, "baseUrl": "http://127.0.0.1:9/v1", "apiKey": key},
        )
        assert created.status_code == 201, created.text
        provider_id = int(created.json()["id"])
        added = self.client.post(
            f"/api/settings/providers/{provider_id}/models",
            json={"model": model, "isDefault": True},
        )
        assert added.status_code == 201, added.text
        return provider_id


@pytest.fixture()
def h(tmp_path: Path) -> Iterator[Harness]:
    yield Harness(tmp_path)


def _models(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = conn.execute(
        "select id, provider_id, model, is_default from provider_models order by id"
    ).fetchall()
    return rows


def test_preview_tells_the_truth_before_and_after_configuration(h: Harness) -> None:
    """未配置 → configured=false 且字段为空；配置后 → 如实报出地址与模型。"""
    empty = h.client.get("/api/settings")
    assert empty.status_code == 200, empty.text
    assert empty.json()["configured"] is False
    assert empty.json()["model"] is None
    assert empty.json()["temperature"] is None, "Python 版没有 temperature，不许编一个数出来"

    provider_id = h.provider(model="glm-4-flash")
    assert h.client.post(f"/api/settings/providers/{provider_id}/activate").status_code == 200

    body = h.client.get("/api/settings").json()
    assert body["configured"] is True
    assert body["model"] == "glm-4-flash"
    assert body["baseUrl"] == "http://127.0.0.1:9/v1"
    assert body["apiKeyConfigured"] is True
    assert body["activeProviderId"] == provider_id
    assert KEY not in empty.text and KEY not in str(body)


def test_the_list_endpoint_is_wrapped_the_way_the_ui_reads_it(h: Harness) -> None:
    """`{providers: [...]}` 是契约的一部分：前端按这个形状解析（D7 的形状错配就出在这）。"""
    provider_id = h.provider()
    body = h.client.get("/api/settings/providers").json()

    assert set(body) == {"providers"}
    assert len(body["providers"]) == 1
    entry = body["providers"][0]
    assert entry["id"] == provider_id
    assert entry["hasApiKey"] is True
    assert [m["model"] for m in entry["models"]] == ["glm-4-flash"]
    assert KEY not in str(body), "读接口回传了凭据"


def test_update_changes_name_and_credentials(h: Harness) -> None:
    provider_id = h.provider()
    new_key = "sk-after-update-7788"

    updated = h.client.put(
        f"/api/settings/providers/{provider_id}",
        json={"name": "改名后", "baseUrl": "http://127.0.0.1:10/v1", "apiKey": new_key},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "改名后"
    assert new_key not in updated.text

    conn = h.conn()
    try:
        row = conn.execute(
            "select name, base_url, api_key_encrypted from providers where id = ?", (provider_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["name"] == "改名后"
    assert row["base_url"] == "http://127.0.0.1:10/v1"
    assert crypto.decrypt(SECRET, str(row["api_key_encrypted"])) == new_key


def test_update_with_a_blank_key_keeps_the_existing_credential(h: Harness) -> None:
    """留空 = 不改凭据。**这条容易写成"清空"**，那会让用户改个名字就把钥匙丢了。"""
    provider_id = h.provider()
    updated = h.client.put(
        f"/api/settings/providers/{provider_id}",
        json={"name": "只改名", "baseUrl": "http://127.0.0.1:9/v1", "apiKey": ""},
    )
    assert updated.status_code == 200, updated.text

    conn = h.conn()
    try:
        row = conn.execute(
            "select api_key_encrypted from providers where id = ?", (provider_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert crypto.decrypt(SECRET, str(row["api_key_encrypted"])) == KEY


def test_deleting_a_provider_takes_its_models_with_it(h: Harness) -> None:
    provider_id = h.provider()
    assert h.client.delete(f"/api/settings/providers/{provider_id}").status_code == 204

    conn = h.conn()
    try:
        assert conn.execute("select count(*) from providers").fetchone()[0] == 0
        assert _models(conn) == [], "供应商没了，模型行还在——那是查不出归属的孤儿"
    finally:
        conn.close()
    assert h.client.delete(f"/api/settings/providers/{provider_id}").status_code == 404


def test_setting_the_default_model_moves_the_flag(h: Harness) -> None:
    """两个模型里切默认：永远只有一个 is_default=1（唯一索引会替我们兜底）。"""
    provider_id = h.provider(model="first")
    second = h.client.post(
        f"/api/settings/providers/{provider_id}/models", json={"model": "second"}
    )
    assert second.status_code == 201, second.text
    second_id = int(second.json()["id"])

    moved = h.client.put(f"/api/settings/providers/{provider_id}/models/{second_id}/default")
    assert moved.status_code == 200, moved.text

    conn = h.conn()
    try:
        rows = _models(conn)
    finally:
        conn.close()
    defaults = [row["model"] for row in rows if row["is_default"]]
    assert defaults == ["second"]


def test_deleting_the_default_model_leaves_no_default(h: Harness) -> None:
    """删掉默认模型后不能再指着它——预览会回到"未配置"，而不是留一个悬空的模型名。"""
    provider_id = h.provider()
    model_id = h.client.get("/api/settings/providers").json()["providers"][0]["models"][0]["id"]

    assert h.client.delete(f"/api/settings/models/{model_id}").status_code == 204
    assert h.client.delete(f"/api/settings/models/{model_id}").status_code == 404

    conn = h.conn()
    try:
        assert _models(conn) == []
    finally:
        conn.close()
    assert h.client.post(f"/api/settings/providers/{provider_id}/activate").status_code == 200


def test_connectivity_check_reports_failure_without_leaking_the_key(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连不上 → `ok=false` + 一句人话，**200**（结果不是错误），且不带 key。"""
    provider_id = h.provider()

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr("agentworkbench.api.routes.settings.httpx.get", boom)
    result = h.client.post(f"/api/settings/providers/{provider_id}/test")

    assert result.status_code == 200, result.text
    body = result.json()
    assert body["ok"] is False
    assert body["error"], "失败了却不说为什么，用户只能猜"
    assert KEY not in result.text


def test_fetch_models_returns_upstream_ids(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """拉模型列表：上游 `data[].id` → `{models: [...]}`，路径拼成 `{baseUrl}/models`。"""
    provider_id = h.provider()
    seen: list[str] = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"object": "list", "data": [{"id": "glm-4-flash"}, {"id": "glm-4-plus"}]}

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        seen.append(url)
        return FakeResponse()

    monkeypatch.setattr("agentworkbench.api.routes.settings.httpx.get", fake_get)
    result = h.client.post(f"/api/settings/providers/{provider_id}/fetch-models")

    assert result.status_code == 200, result.text
    assert result.json() == {"models": ["glm-4-flash", "glm-4-plus"]}
    assert seen == ["http://127.0.0.1:9/v1/models"]


# ── D9：上游探测的地址校验（红队 C 的 R-01） ──


def test_upstream_probe_refuses_targets_that_are_never_model_endpoints() -> None:
    """非 http(s)、云元数据、带凭据的 URL 一律拒；本地模型端点照旧允许。

    「获取模型列表」这个端点**本来就是**打一个用户给的地址，所以不能一刀切禁内网——
    本产品的主场景是模型跑在 `127.0.0.1:11434`（Ollama）。拦的只能是那种
    「任何情况下都不是模型端点」的地址。

    改坏这里会怎样：把 `_blocked_reason` 改成恒 `None`，平台管理员就能拿这一条当
    内网探测器（探端口、用 `?` 劫持 `/models` 打任意 path、把 Bearer 发给目标），
    D9 实测可以打出 `{"models":["internal-secret-model"]}`。
    """
    from agentworkbench.api.routes.settings import _blocked_reason

    for url, why in [
        ("file:///C:/Windows/win.ini", "非 http(s)"),
        ("ftp://example.com/", "非 http(s)"),
        ("http://169.254.169.254/latest/meta-data/", "云元数据"),
        ("http://100.100.100.200/latest/meta-data/", "云元数据（阿里云）"),
        ("http://user:pass@example.com/v1", "URL 里带凭据"),
    ]:
        assert _blocked_reason(url) is not None, f"{url}（{why}）应当被拒"

    for url in [
        "http://127.0.0.1:11434/v1",  # 本地 Ollama
        "http://192.168.1.20:8000/v1",  # 局域网推理机
        "https://api.siliconflow.cn/v1",
    ]:
        assert _blocked_reason(url) is None, f"{url} 是正常的模型端点，不该被拒"
