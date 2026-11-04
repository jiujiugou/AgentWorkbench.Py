"""不变量：核心能力只经 HTTP 走完全流程（S-V1 · REQ-17）。

被改坏时**用户**看到什么：产品退化成"只能在浏览器里用的网站"——提问要开页面、
看进度要盯着屏幕、断开连接任务就消失；而查代码的人看到的是"每个功能明明都有
REST 入口"。判据永远是这个流程能不能跑完，不是路由表存在不存在。

"删掉前端产品仍然成立"这句话的验收物就是这个流程本身——不是"额外写个
测试验证一下"，它就是 REQ-17 的判据。五步：上传 → 提问 → 取引用 →
查运行 → 查历史。**登录步刻意缺席**：F1 没有登录端点，接上后补第六步
并把 toml 里的状态升 pass；在此之前这条只能是 partial。

每一步都走公开 HTTP 端点（TestClient 只是传输层，不是对函数的直调），
这样"前端删掉后这些端点够不够用"与这条测试的通过是同一件事。

负控说明：判据原文的负控是「删掉 cookie jar 后同脚本必须 401」——那要等
F1 的登录态存在才有意义。现在的负控写不写都不红，写进 toml 的 negative
留给实现格。

改坏这个不变量会怎样：任何一步改成"需要先在页面上点过什么"（状态只活在
前端内存、引用只在 SignalR 推送里），这条当场红——这正是它存在的目的。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

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

SAMPLE_PDF = Path(__file__).resolve().parents[1] / "fixtures" / "sample.pdf"

TEXTS = [
    "H5U 的 E2 报警是通讯超时，检查波特率与站号设置。",
    "FC01 读线圈功能码用于读取输出继电器的状态。",
    "维护保养：每季度清洁散热风道并紧固接线端子。",
]
QUESTION = TEXTS[0]
ANSWER = "通讯超时，检查波特率与站号设置[1]。"


class ScriptedEmbedder:
    """与 test_ask_endpoint 同一套脚本向量：query 与块 0 同向量 → 判定有赢家。

    上传的 sample.pdf 走 fallback 向量（与脚本向量近乎正交），不会污染判定。
    """

    def __init__(self, indices: list[int], dim: int = 1024) -> None:
        self._vectors: dict[str, list[float]] = {}
        for text, index in zip(TEXTS, indices, strict=True):
            vec = [0.0] * dim
            vec[index % dim] = 1.0
            self._vectors[text] = vec

    def __call__(self, texts: list[str]) -> list[list[float]]:
        fallback = [0.5] * 1024
        return [self._vectors.get(text, fallback) for text in texts]


class FakeChat:
    def __init__(self) -> None:
        self.calls = 0
        self.last_usage: dict[str, int | None] = {"prompt_tokens": 900, "completion_tokens": 30}

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return ANSWER


class Harness:
    """真库 + 真 HTTP。ask 的模型客户端注入假实现（不打真 API）。"""

    def __init__(self, tmp_path: Path) -> None:
        self.settings = Settings(data_dir=tmp_path / "data")
        self.deps: Deps = build_deps(self.settings)
        self.embedder = ScriptedEmbedder([0, 1, 2])
        self.deps.embedder = cast(EmbedderPort, self.embedder)
        self.chat = FakeChat()
        self.deps.chat_factory = lambda _conn: (self.chat, "glm-4-flash")
        self.app = create_app(settings=self.settings, deps=self.deps)
        self.client = TestClient(self.app)
        sign_in(self.client, self.app)
        self._seed()

    def _seed(self) -> None:
        """三块索引，让提问有可答的资料（sample.pdf 的解析走真链路，见第一步）。"""
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

    def wait_until(self, condition: Callable[[], bool], *, what: str) -> None:
        import time

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.05)
        raise AssertionError(f"等待超时：{what}")


@pytest.fixture()
def smoke(tmp_path: Path) -> Iterator[Harness]:
    yield Harness(tmp_path)


def test_http_alone_completes_the_loop(smoke: Harness) -> None:
    """S-V1：上传 → 提问 → 取引用 → 查运行 → 查历史。全程只有 HTTP。"""
    # ── 第一步：上传（真 PDF，真解析队列） ──
    upload = smoke.client.post(
        "/api/documents",
        files={"file": ("addr-005.pdf", SAMPLE_PDF.read_bytes(), "application/pdf")},
    )
    assert upload.status_code == 202, upload.text
    doc_id = upload.json()["id"]
    smoke.wait_until(
        lambda: smoke.client.get(f"/api/documents/{doc_id}").json()["status"] == "parsed",
        what="上传解析到 parsed",
    )

    # ── 第二步：提问（产出 run 与回答） ──
    ask = smoke.client.post("/api/knowledge/ask", json={"question": QUESTION})
    assert ask.status_code == 200, ask.text
    ask_body = ask.json()
    assert ask_body["status"] == "succeeded"
    run_id = str(ask_body["runId"])
    conversation_id = str(ask_body["conversationId"])

    # ── 第三步：取引用（回答自带出处卡片） ──
    sources = cast(list[dict[str, object]], ask_body["sources"])
    assert sources, "没有出处卡片的回答过不了这一步"
    assert sources[0]["fileName"]

    # ── 第四步：查运行（run id 能回放全程） ──
    trace = smoke.client.get(f"/api/runs/{run_id}/trace")
    assert trace.status_code == 200, trace.text
    trace_body = trace.json()
    assert trace_body["status"] == "succeeded"
    kinds = [step["kind"] for step in trace_body["steps"]]
    assert "retrieval" in kinds, "检索步必须在轨迹里，否则'依据'不可考"

    # ── 第五步：查历史（换一次连接读回同样的东西） ──
    conversations = smoke.client.get("/api/conversations").json()
    assert any(item["id"] == conversation_id for item in conversations)
    history = smoke.client.get(f"/api/conversations/{conversation_id}/messages").json()
    messages = history["messages"]
    answer_row = next(m for m in messages if m["role"] == "assistant")
    assert answer_row["runId"] == run_id
    assert answer_row["citations"], "重开之后引用必须还在（这是 F5 的承诺，也是这步的目的）"

    # 全程 chat 被调过、且没有第二个模型客户端：证据在 FakeChat 的计数里
    assert smoke.chat.calls == 1
