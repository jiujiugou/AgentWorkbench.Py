"""不变量：运行的状态查询与断连容忍（S-V2 / S-V3）。

注入点走真实路径：假 chat 用 `threading.Event` 阻塞——提交（202）先返回，
run 停在 running，用例想让它走到哪一步就放行到哪一步。「断开」在这个测试里的
等价形态 = 提交响应返回之后客户端不再有任何行为：ASGI 传输层里没有真的
socket 可掐，而服务端可观察的行为（不持有 Request、不被取消、继续到终态）
与真实断开完全一致。真实 socket 版的验证归部署后的人工项（S-H1）。

改坏这些不变量时**用户**看到什么：
- 把执行挪回请求线程（或把取消令牌绑到连接上）→ 程序化调用方提交后断开，
  那轮作答无声消失——重连什么都查不到，也没有任何东西报错；
- 后台线程改用裸引用不持集合 → 任务偶发消失，且只在负载高时复现；
- answer 改从内存 / 当次返回拿 → 重连查不到结果，202 等于白条；
- 别人的 run 能查到状态 → run id 成了横向越权的探针。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import sign_in
from fastapi.testclient import TestClient

from agentworkbench.api.app import create_app
from agentworkbench.api.deps import Deps, build_deps
from agentworkbench.api.routes import runs as runs_routes
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
ANSWER_TEXT = "E2 报警是通讯超时，检查波特率与站号设置。[1]"


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


class BlockingChat:
    """进了 complete 就等放行：用例控制 run 停在 running 的时长。"""

    def __init__(self, answer: str = ANSWER_TEXT) -> None:
        self.answer = answer
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.last_usage: dict[str, int | None] = {"prompt_tokens": 10, "completion_tokens": 5}

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=10), "用例没有放行——10 秒后放弃，避免挂死整个测试"
        return self.answer


class PlainChat:
    """直接返回：同步 ask 与"不阻塞的异步提交"用。"""

    def __init__(self, answer: str = ANSWER_TEXT) -> None:
        self.answer = answer
        self.last_usage: dict[str, int | None] = {"prompt_tokens": 10, "completion_tokens": 5}

    def complete(self, system: str, user: str) -> str:
        return self.answer


class Harness:
    """真库 + 真装配。chat 由用例装（阻塞或直返），embedder 固定为能判过的脚本。"""

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
        """三块索引 + 真文档行（chunks 的外键在真实库上不是摆设）。"""
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

    def open_direct(self) -> sqlite3.Connection:
        conn = engine.connect(self.settings.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def use_chat(self, chat: object) -> None:
        """替换 chat 工厂。闭包返回固定实例：路由与后台线程拿到同一个对象。"""
        # object → ChatClient 的收窄由用例自己担保（假实现都满足 complete 契约）
        self.deps.chat_factory = lambda _conn: (cast(Any, chat), "glm-4-flash")

    def wait_until(self, condition: Callable[[], bool], *, what: str, timeout: float = 5.0) -> None:
        """条件等待：小步轮询到条件成立或超时。绝不裸 sleep——时序假设写进断言。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.01)
        raise AssertionError(f"超时：{what}")

    def run_id_of(self, conversation_id: str) -> str | None:
        body = self.client.get(f"/api/conversations/{conversation_id}/messages")
        if body.status_code != 200:
            return None
        for message in body.json()["messages"]:
            if message["role"] == "assistant" and message["runId"] is not None:
                return str(message["runId"])
        return None


@pytest.fixture()
def h(tmp_path: Path) -> Iterator[Harness]:
    yield Harness(tmp_path, [0, 1, 2])


def _without_request_id(response: Any) -> dict[str, object]:
    """剥掉 request_id 后的响应体：随机字段不参与存在性判据（理由同 C-V1）。"""
    body = cast(dict[str, object], json.loads(response.content))
    body.pop("request_id", None)
    return body


# ── S-V3：提交与执行分离 ──


def test_submit_returns_202_before_the_answer_exists(h: Harness) -> None:
    """202 的响应体只有 conversationId：受理回执，不是结果。

    runId 也不在——run 行在线程里才创建。客户端发现 runId 的路径是消息列表
    （C-V11 的占位行），这里不发明第二条。
    """
    blocking = BlockingChat()
    h.use_chat(blocking)

    response = h.client.post("/api/runs", json={"question": QUESTION})

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"conversationId"}, f"202 不该携带结果字段：{body}"


def test_submitted_run_survives_the_client_leaving(h: Harness) -> None:
    """提交之后客户端不再有任何行为，run 仍跑到终态。

    「断开」的可观察等价：202 返回后用例对提交这次请求做不了任何事
    （TestClient 的响应已经结束），后续只是另起连接查询。
    执行若被绑在连接上（C# 的移植禁忌），这条永远等不到终态。
    """
    blocking = BlockingChat()
    h.use_chat(blocking)

    submitted = h.client.post("/api/runs", json={"question": QUESTION})
    assert submitted.status_code == 202
    conversation_id = submitted.json()["conversationId"]

    # run 起来了（占位行出现）→ 放行 → 等终态
    h.wait_until(lambda: h.run_id_of(conversation_id) is not None, what="占位行出现")
    blocking.release.set()

    run_id = h.run_id_of(conversation_id)
    assert run_id is not None
    h.wait_until(
        lambda: h.client.get(f"/api/runs/{run_id}").json()["status"] == "succeeded",
        what="run 到终态",
    )


def test_result_matches_an_inline_run(h: Harness) -> None:
    """异步跑完的 run 与同步 ask 产出的 run **同构**：同一批字段、同一种终态。

    「异步版是二等公民」是最容易滑进去的形态——少记几个字段、少落一份快照，
    界面看不出来，程序化调用方拿到的是缺斤短两的结果。
    """
    h.use_chat(PlainChat())
    inline = h.client.post("/api/knowledge/ask", json={"question": QUESTION})
    assert inline.status_code == 200
    inline_run_id = inline.json()["runId"]

    submitted = h.client.post("/api/runs", json={"question": QUESTION})
    conversation_id = submitted.json()["conversationId"]
    h.wait_until(lambda: h.run_id_of(conversation_id) is not None, what="占位行出现")
    async_run_id = h.run_id_of(conversation_id)
    assert async_run_id is not None
    h.wait_until(
        lambda: h.client.get(f"/api/runs/{async_run_id}").json()["status"] == "succeeded",
        what="异步 run 到终态",
    )

    inline_status = h.client.get(f"/api/runs/{inline_run_id}")
    async_status = h.client.get(f"/api/runs/{async_run_id}")
    assert inline_status.status_code == async_status.status_code == 200
    assert set(inline_status.json()) == set(async_status.json()), "字段集不同构"
    assert async_status.json()["status"] == "succeeded"
    assert async_status.json()["answer"], "异步结果不该缺正文"


# ── S-V2：状态查询 ──


def test_run_is_readable_before_and_after_terminal(h: Harness) -> None:
    """running 行可读（否则 202 之后的轮询无从谈起），终态后 answer 从库里来。"""
    blocking = BlockingChat()
    h.use_chat(blocking)

    submitted = h.client.post("/api/runs", json={"question": QUESTION})
    conversation_id = submitted.json()["conversationId"]
    h.wait_until(lambda: h.run_id_of(conversation_id) is not None, what="占位行出现")
    run_id = h.run_id_of(conversation_id)
    assert run_id is not None

    # 还没放行：running，answer 是占位行的空串——"还没有"是可见的事实
    running = h.client.get(f"/api/runs/{run_id}")
    assert running.status_code == 200
    assert running.json()["status"] == "running"
    assert running.json()["answer"] == ""

    blocking.release.set()
    h.wait_until(
        lambda: h.client.get(f"/api/runs/{run_id}").json()["status"] == "succeeded",
        what="run 到终态",
    )

    finished = h.client.get(f"/api/runs/{run_id}")
    assert finished.json()["answer"] == ANSWER_TEXT
    assert finished.json()["conversationId"] == conversation_id
    assert finished.json()["promptTokens"] == 10, "用量来自 run 行，不是猜的"


def test_foreign_run_is_byte_identical_to_missing(h: Harness) -> None:
    """别人的 run 与不存在的 run：剥掉 request_id 后逐字段相同（C-V1 同款判据）。"""
    conn = h.open_direct()
    try:
        conn.execute(
            "insert into agent_runs (id, user_id, tenant_id, question, status, started_at)"
            " values ('run-foreign', 'someone-else', 'default', 'q', 'succeeded', 't')"
        )
        conn.commit()
    finally:
        conn.close()

    theirs = h.client.get("/api/runs/run-foreign")
    nobody = h.client.get("/api/runs/never-existed")

    assert theirs.status_code == nobody.status_code == 404
    assert _without_request_id(theirs) == _without_request_id(nobody)
    assert b"run-foreign" not in theirs.content, "id 回显就是在告诉攻击者这条存在"


def test_saved_answer_is_readable_through_the_run(h: Harness) -> None:
    """答案经 run_id 取回，且与会话历史里的那行**一致**——同一份库，两个读法。"""
    h.use_chat(PlainChat())
    submitted = h.client.post("/api/runs", json={"question": QUESTION})
    conversation_id = submitted.json()["conversationId"]
    h.wait_until(lambda: h.run_id_of(conversation_id) is not None, what="占位行出现")
    run_id = h.run_id_of(conversation_id)
    assert run_id is not None
    h.wait_until(
        lambda: h.client.get(f"/api/runs/{run_id}").json()["status"] == "succeeded",
        what="run 到终态",
    )

    status = h.client.get(f"/api/runs/{run_id}").json()
    history = h.client.get(f"/api/conversations/{conversation_id}/messages").json()
    answer_row = next(m for m in history["messages"] if m["role"] == "assistant")

    assert status["answer"] == answer_row["content"]
    assert status["conversationId"] == conversation_id
    assert answer_row["runId"] == run_id


# ── REQ-16：停止 ──


def _running_threads() -> set[threading.Thread]:
    with runs_routes._threads_lock:
        return set(runs_routes._threads)


def test_cancel_closes_the_inflight_run_and_keeps_the_unanswered_step(h: Harness) -> None:
    """模型调用阻塞中发停止：终态 cancelled，且“已发出未返回”的那一步还在。

    停止不能等模型返回——它要做的是把 run 收口；迟到的结果由 close_step 丢弃。
    """
    blocking = BlockingChat()
    h.use_chat(blocking)
    before = _running_threads()

    submitted = h.client.post("/api/runs", json={"question": QUESTION})
    conversation_id = submitted.json()["conversationId"]
    h.wait_until(lambda: h.run_id_of(conversation_id) is not None, what="占位行出现")
    run_id = h.run_id_of(conversation_id)
    assert run_id is not None
    worker = next(iter(_running_threads() - before))
    h.wait_until(lambda: blocking.entered.is_set(), what="模型调用已发出")

    stopped = h.client.post("/api/runs/cancel", json={"runId": run_id})

    assert stopped.status_code == 200
    body = stopped.json()
    assert body["cancelled"] is True
    assert body["status"] == "cancelled"
    assert body["runId"] == run_id
    assert body["conversationId"] == conversation_id

    trace = h.client.get(f"/api/runs/{run_id}/trace").json()
    assert trace["status"] == "cancelled"
    assistant = next(step for step in trace["steps"] if step["kind"] == "assistant")
    assert assistant["output"] is None
    assert assistant["inFlight"] is True

    blocking.release.set()
    h.wait_until(lambda: not worker.is_alive(), what="后台线程退出")

    after = h.client.get(f"/api/runs/{run_id}/trace").json()
    assert after["status"] == "cancelled", "迟到的模型结果不许把终态改成 succeeded"
    assistant = next(step for step in after["steps"] if step["kind"] == "assistant")
    assert assistant["output"] is None, "取消后迟到的步骤结果必须被丢弃"


def test_cancel_without_run_id_targets_the_active_run(h: Harness) -> None:
    """前端不知道 runId 也能停：按当前会话 / 当前用户最新的 running 找到它。"""
    blocking = BlockingChat()
    h.use_chat(blocking)
    before = _running_threads()

    submitted = h.client.post("/api/runs", json={"question": QUESTION})
    conversation_id = submitted.json()["conversationId"]
    h.wait_until(lambda: h.run_id_of(conversation_id) is not None, what="占位行出现")
    run_id = h.run_id_of(conversation_id)
    assert run_id is not None
    worker = next(iter(_running_threads() - before))
    h.wait_until(lambda: blocking.entered.is_set(), what="模型调用已发出")

    stopped = h.client.post("/api/runs/cancel", json={"conversationId": conversation_id})

    assert stopped.status_code == 200
    body = stopped.json()
    assert body["cancelled"] is True
    assert body["runId"] == run_id
    assert body["conversationId"] == conversation_id

    blocking.release.set()
    h.wait_until(lambda: not worker.is_alive(), what="后台线程退出")


def test_cancel_after_terminal_does_not_rewrite_the_run(h: Harness) -> None:
    """已经成功的 run 不会被“停止”改写成 cancelled，正文也不会被清掉。"""
    h.use_chat(PlainChat())

    inline = h.client.post("/api/knowledge/ask", json={"question": QUESTION})
    assert inline.status_code == 200
    run_id = inline.json()["runId"]
    answer = inline.json()["answer"]

    stopped = h.client.post("/api/runs/cancel", json={"runId": run_id})

    assert stopped.status_code == 200
    body = stopped.json()
    assert body["cancelled"] is False
    assert body["status"] == "succeeded"
    assert h.client.get(f"/api/runs/{run_id}").json()["status"] == "succeeded"
    assert h.client.get(f"/api/runs/{run_id}").json()["answer"] == answer


def test_cancelling_a_foreign_run_is_404_like_missing(h: Harness) -> None:
    """别人的 run 与不存在的 run：剥掉 request_id 后逐字段相同（C-V1 同款判据）。"""
    conn = h.open_direct()
    try:
        conn.execute(
            "insert into agent_runs (id, user_id, tenant_id, question, status, started_at)"
            " values ('run-foreign', 'someone-else', 'default', 'q', 'running', 't')"
        )
        conn.commit()
    finally:
        conn.close()

    theirs = h.client.post("/api/runs/cancel", json={"runId": "run-foreign"})
    nobody = h.client.post("/api/runs/cancel", json={"runId": "never-existed"})

    assert theirs.status_code == nobody.status_code == 404
    assert _without_request_id(theirs) == _without_request_id(nobody)
    assert b"run-foreign" not in theirs.content, "id 回显就是在告诉攻击者这条存在"
