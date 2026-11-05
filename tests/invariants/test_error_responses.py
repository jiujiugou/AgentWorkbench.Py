"""不变量：错误响应的稳定性与不泄露（S-V4 / S-V5）。

注入点走真实路径：monkeypatch `chat_factory` 抛 RuntimeError（消息里埋哨兵），
ask 路由的兜底让它变成 500 `internal_error`。**不造测试专用路由**——专用路由
过不了 declarations 闸门（每条路由必须表态产不产 run），而真实路径上的泄露
才是泄露；假路由上的干净什么也证明不了。

三类哨兵对应三类泄露（F6 §4.2 铁律）：
- 凭据：api_key 的值进过异常（连接串、SDK 异常都爱带它）
- 他人数据：别的团队的文档名——错误信息里出现它等于横向越权读
- 内网地址：base_url 的主机名，只该出现在日志里

改坏这些不变量会怎样：把异常原文拼回 message（client 曾这么写）→ 当场红；
把 `logger.exception` 从兜底 handler 删掉 → "日志里有"那半句红——
那时"不泄露"就退化成了"不记录"，用户看不见的运维也看不见。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import sign_in
from fastapi.testclient import TestClient

from agentworkbench.api.app import create_app
from agentworkbench.api.deps import Deps, build_deps
from agentworkbench.config import Settings
from agentworkbench.core.documents import ChunkDraft
from agentworkbench.db import engine
from agentworkbench.ingest.worker import EmbedderPort
from agentworkbench.rag.indexer import write_index

pytestmark = pytest.mark.invariant

TEXTS = [
    "H5U 的 E2 报警是通讯超时，检查波特率与站号设置。",
    "FC01 读线圈功能码用于读取输出继电器的状态。",
    "维护保养：每季度清洁散热风道并紧固接线端子。",
]
QUESTION = TEXTS[0]


class ScriptedEmbedder:
    """按文本给向量：query 与块 0 同向量（距离 0）→ 判定有赢家，能走到 chat。"""

    def __init__(self, indices: list[int], dim: int = 1024) -> None:
        self._vectors: dict[str, list[float]] = {}
        for text, index in zip(TEXTS, indices, strict=True):
            vec = [0.0] * dim
            vec[index % dim] = 1.0
            self._vectors[text] = vec

    def __call__(self, texts: list[str]) -> list[list[float]]:
        fallback = [0.5] * 1024
        return [self._vectors.get(text, fallback) for text in texts]


class Harness:
    """与 test_ask_endpoint 同构的最小装配：真库 + 注入 embedder，chat 由用例给。"""

    def __init__(self, tmp_path: Path, indices: list[int]) -> None:
        self.settings = Settings(data_dir=tmp_path / "data")
        self.deps: Deps = build_deps(self.settings)
        self.embedder = ScriptedEmbedder(indices)
        self.deps.embedder = cast(EmbedderPort, self.embedder)
        self.app = create_app(settings=self.settings, deps=self.deps)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        sign_in(self.client, self.app)
        self._seed(indices)

    def _seed(self, indices: list[int]) -> None:
        """三块索引。必须有真文档行：chunks 的外键在真实库上不是摆设。"""
        conn = engine.connect(self.settings.db_path)
        try:
            now = "2026-11-03T00:00:00"
            conn.execute(
                "insert into documents(id, tenant_id, file_name, stored_path, sha256,"
                " size_bytes, status, page_count, created_at, updated_at)"
                " values (?,?,?,?,?,?,?,?,?,?)",
                (
                    "doc-1",
                    self.settings.tenant_id,
                    "manual.pdf",
                    "seed/manual.pdf",
                    "sha-doc-1",
                    1024,
                    "parsed",
                    1,
                    now,
                    now,
                ),
            )
            drafts = [
                ChunkDraft(seq=i, page_from=1, page_to=1, content=text)
                for i, text in enumerate(TEXTS)
            ]
            write_index(
                conn,
                document_id="doc-1",
                drafts=drafts,
                embeddings=self.embedder(TEXTS),
                now=now,
            )
            conn.commit()
        finally:
            conn.close()


@pytest.fixture()
def h(tmp_path: Path) -> Iterator[Harness]:
    yield Harness(tmp_path, [0, 1, 2])


class ExplodingChat:
    """一调就抛：message 里埋着哨兵。last_usage 兜住 runner 的用量读取。"""

    def __init__(self, message: str) -> None:
        self.message = message
        self.calls = 0
        self.last_usage: dict[str, int | None] = {"prompt_tokens": None, "completion_tokens": None}

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        raise RuntimeError(self.message)


# 返回值是 starlette TestClient 的 httpx.Response；openai 新版类型存根把 httpx
# 别名成 httpx2，两个类型系统对不上——注解 Any，断言交给测试自己。
def _ask_with_failure(h: Harness, message: str) -> Any:
    """让 chat 抛哨兵异常，走完整 ask 路径，返回 500 响应。日志在 caplog 里收。"""
    chat = ExplodingChat(message)
    h.deps.chat_factory = lambda _conn: (chat, "glm-4-flash")
    response = h.client.post("/api/knowledge/ask", json={"question": QUESTION})
    assert response.status_code == 500, response.text
    assert chat.calls == 1, "chat 没被调用的话，哨兵异常根本没发生，这条测试什么也没验"
    return response


def test_same_error_twice_has_the_same_code_and_status(h: Harness) -> None:
    """S-V4：同一错误两次调用，code 与 retryable 完全一致，message 不参与比对。

    message 是给人看的、可改；调用方只许按 code 判断。断言里刻意**不比 message**——
    比了它，改一次文案这条就红，等于把"文案可改"判成违规。
    """
    bodies = []
    for _ in range(2):
        response = h.client.get("/api/conversations/never-existed/messages")
        assert response.status_code == 404
        bodies.append(response.json())

    first, second = bodies
    assert first["code"] == second["code"] == "not_found"
    assert first["retryable"] == second["retryable"] is False
    # 状态码也一致：同一码不许"这次 404 下次 500"
    assert response.status_code == 404


def test_error_body_carries_a_request_id(h: Harness) -> None:
    """S-V4：错误体五字段齐全，request_id 非空。

    五字段是**对外协议**：调用方按名字读，缺一个就是破坏契约——
    哪怕少的是"现在没人用"的那个字段。404（AppError 路径）与 500（兜底路径）
    各验一次：两条 handler 是分开写的，只验一条等于没验另一条。
    """
    not_found = h.client.get("/api/conversations/never-existed/messages")
    assert not_found.status_code == 404
    for field in ("code", "http_status", "message", "retryable", "request_id"):
        assert field in not_found.json(), f"错误体缺 {field}"
    assert not_found.json()["request_id"]
    assert not_found.json()["http_status"] == 404, "body 里的 http_status 必须与状态行一致"


def test_response_header_matches_the_body_request_id(h: Harness) -> None:
    """S-V4：响应头 X-Request-Id 与 body 的 request_id 是**同一个值**。

    两个地方各生成一个 id 是最容易出现的事故形态：日志里查头上的 id，
    工单里贴的是 body 的，两边对不上，request_id 就白设计了。
    """
    not_found = h.client.get("/api/conversations/never-existed/messages")
    header = not_found.headers.get("x-request-id")
    assert header, "响应头必须有 X-Request-Id"
    assert header == not_found.json()["request_id"]


def test_unexpected_errors_also_carry_the_request_id(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """S-V4 × S-V5 交界：500 的兜底路径同样带 request_id，且进日志。

    兜底是最容易漏的一处——写它的那天往往还没有 request_id。
    """
    sentinel = "兜底路径的哨兵-4471"
    chat = ExplodingChat(f"boom {sentinel}")
    h.deps.chat_factory = lambda _conn: (chat, "glm-4-flash")
    with caplog.at_level(logging.ERROR):
        response = h.client.post("/api/knowledge/ask", json={"question": QUESTION})

    assert response.status_code == 500
    body = response.json()
    assert body["request_id"], "500 也必须带 request_id"
    assert response.headers.get("x-request-id") == body["request_id"]
    assert body["request_id"] in caplog.text, "日志里的 request_id 要能对上响应里的"


def test_two_requests_get_different_request_ids(h: Harness) -> None:
    """不同请求的 id 必须不同：id 相同 = 两次独立请求在日志里无法区分。"""
    first = h.client.get("/api/conversations/never-existed/messages")
    second = h.client.get("/api/conversations/never-existed/messages")

    assert first.json()["request_id"] != second.json()["request_id"]


def test_credential_sentinel_reaches_logs_not_response(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """S-V5 ① 凭据：api_key 的值不许出现在响应体，必须出现在日志。"""
    sentinel = "sk-sentinel-credential-9876"
    with caplog.at_level(logging.ERROR):
        response = _ask_with_failure(h, f"上游组件故障：{sentinel}（配置探测信息）")
    body = response.json()

    assert body["code"] == "internal_error"
    assert body["message"] == "服务器内部错误。"
    assert sentinel not in response.text, "凭据泄露进了响应体"
    assert sentinel in caplog.text, "连日志都没有——'不泄露'退化成了'不记录'，排障无从下手"


def test_foreign_data_sentinel_reaches_logs_not_response(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """S-V5 ② 他人数据：错误信息里出现别的团队的文档名，等于横向越权读。"""
    sentinel = "他人文档《工资表2026.pdf》"
    with caplog.at_level(logging.ERROR):
        response = _ask_with_failure(h, f"上游组件故障：{sentinel}（配置探测信息）")

    assert sentinel not in response.text, "他人数据泄露进了响应体"
    assert sentinel in caplog.text


def test_internal_address_sentinel_reaches_logs_not_response(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """S-V5 ③ 内网地址：base_url 的主机名只该运维看得见，不该用户看得见。"""
    sentinel = "http://10.0.20.5:11434/v1"
    with caplog.at_level(logging.ERROR):
        response = _ask_with_failure(h, f"上游组件故障：{sentinel}（配置探测信息）")

    assert sentinel not in response.text, "内网地址泄露进了响应体"
    assert sentinel in caplog.text


# ── D6 补：校验失败的形态，与「按 request_id 查得到」 ──


def test_validation_failures_use_the_same_envelope(h: Harness) -> None:
    """S-V4：**校验失败也是五字段信封**，不是框架默认的 `{"detail": [...]}`。

    前端只认 `message`；默认体没有这个字段时，用户看到的是整段 JSON 原文。
    这条与 404/500 分开验，因为它是**第三个** handler——前两个写对了，
    第三个仍然可以是默认实现（D6 之前就是）。
    """
    response = h.client.post("/api/knowledge/ask", json={"question": ""})
    assert response.status_code == 400, response.text
    body = response.json()
    for field in ("code", "http_status", "message", "retryable", "request_id"):
        assert field in body, f"校验失败的错误体缺 {field}"
    assert body["code"] == "validation_failed"
    assert body["http_status"] == 400 == response.status_code
    assert body["retryable"] is False
    assert response.headers["x-request-id"] == body["request_id"]


def test_validation_failures_do_not_echo_the_rejected_value(h: Harness) -> None:
    """S-V5：被拒的**值**不回显——只回「哪个字段、什么毛病」。

    这是 D6 实测抓到的泄露：pydantic 每条错误都带 `input`（客户端提交的原值）。
    类型写错的 apiKey 会被原样放进响应体，于是密钥进了浏览器 devtools、
    反代访问日志、前端错误上报。
    """
    sentinel = "sk-d6-echo-sentinel"
    response = h.client.post("/api/knowledge/ask", json={"question": {"nested": sentinel}})

    assert response.status_code == 400, response.text
    assert sentinel not in response.text, "被拒的值被回显了"
    assert "question" in response.json()["message"], "至少要告诉用户是哪个字段写错了"


def test_every_failure_is_findable_by_request_id_in_the_logs(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """S-H2：失败请求必须按 request_id 在日志里查得到（不只是「500 有栈」）。

    改坏前的形态：只有兜底 500 写日志，503 / 404 一条不留——客户端贴来
    `X-Request-Id`，运维在日志里搜不到任何东西。响应里带一个查不到的编号，
    等于这个能力不存在。
    """
    with caplog.at_level(logging.INFO):
        response = h.client.get("/api/conversations/never-existed/messages")

    request_id = response.headers["x-request-id"]
    ours = [record for record in caplog.records if record.name.startswith("agentworkbench")]
    matching = [record for record in ours if request_id in record.getMessage()]
    assert matching, f"按 request_id={request_id} 在日志里查不到"
    joined = "\n".join(record.getMessage() for record in matching)
    assert "not_found" in joined, "日志里没有错误码"
    assert "/api/conversations/" in joined, "日志里没有路径"


def test_an_unknown_path_uses_the_same_envelope(h: Harness) -> None:
    """未匹配的路径也是五字段信封 + `X-Request-Id`。

    改坏这里会怎样：删掉 `on_http_error`，Starlette 的默认实现回来，响应变成
    `{"detail": "Not Found"}`——前端 `parseError` 只认 `message`，于是用户看到的是
    这段 JSON 原文；而且这种失败**没法按 request_id 去日志里定位**，
    因为那个头也不存在了（D9 红队 C 的 R-05）。
    """
    response = h.client.get("/api/nope")

    assert response.status_code == 404, response.text
    body = response.json()
    for field in ("code", "http_status", "message", "retryable", "request_id"):
        assert field in body, f"未匹配路径的错误体缺 {field}：{body}"
    assert body["code"] == "not_found"
    assert response.headers["x-request-id"] == body["request_id"]


def test_a_wrong_method_looks_exactly_like_a_missing_path(h: Harness) -> None:
    """方法不对也归 404：这个产品不确认「某个路径存在」。

    405 会告诉未登录的人「这个路径是真的」，而那是**不需要任何权限**就能做的探测。
    `/api/documents/{别人的 id}` 与不存在的 id 都是 404，是同一条取向。
    """
    response = h.client.put("/api/health")

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"
