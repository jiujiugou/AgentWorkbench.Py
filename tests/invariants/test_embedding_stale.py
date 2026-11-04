"""不变量：embedding 配置与索引的新鲜度（S-V10）。

被改坏时**用户**看到什么：
- 改完 embedding 模型不重建，检索照常返回结果 → 相似度是跨模型的噪声，
  用户拿到一段"看起来有出处"的胡说，而没有任何东西报错；
- 改成返回空列表 → 与"库里真的没有资料"完全一样，用户去改问题、改阈值、
  重新上传，而真正该做的是重建索引；
- 两组配置合进同一个响应体 → 设置页把 chat 与 embedding 并排，有人会一起改，
  改 chat 也把索引搞脏（或反过来）；
- 顺手在检索里重建 → 一次检索卡半分钟，失败时一个字都没有。

改坏这些不变量会怎样：删掉 `ensure_index_fresh` 的抛错 → 检索两条红；
stale 判定改成恒 False → 同上；`set_embedding_model` 不固化"改之前的值" →
改完配置 stale 仍是 False，第一条红。
"""

from __future__ import annotations

import sqlite3
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
from agentworkbench.db.repositories import system_state
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
    """query 与块 0 同向量：检索有赢家。stale 检查在它之前，与它无关。"""

    def __init__(self, dim: int = 1024) -> None:
        self._dim = dim

    def __call__(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self._dim
            vec[0 if text in TEXTS else 1] = 1.0
            vectors.append(vec)
        return vectors


class PlainChat:
    """stale 时根本走不到它；留着是为了证明"报错发生在检索，不是模型"。"""

    def __init__(self) -> None:
        self.calls = 0
        self.last_usage: dict[str, int | None] = {"prompt_tokens": 1, "completion_tokens": 1}

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return "不该走到这里"


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.settings = Settings(data_dir=tmp_path / "data")
        self.deps: Deps = build_deps(self.settings)
        self.embedder = ScriptedEmbedder()
        self.deps.embedder = cast(EmbedderPort, self.embedder)
        self.chat = PlainChat()
        self.deps.chat_factory = lambda _conn: (cast(Any, self.chat), "glm-4-flash")
        self.app = create_app(settings=self.settings, deps=self.deps)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        sign_in(self.client, self.app)
        self._seed()

    def _seed(self) -> None:
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

    def mark_synced(self) -> None:
        """模拟"重建完成"：全量重建端点还没有（F2），直调仓储函数。"""
        conn = self.open_direct()
        try:
            system_state.mark_index_synced(conn, now="2026-11-03T00:01:00")
        finally:
            conn.close()

    def change_embedding(self, model: str = "bge-m3") -> Any:
        return self.client.put("/api/settings/embedding", json={"model": model})

    def search(self) -> Any:
        return self.client.get("/api/knowledge/search", params={"q": "报警"})

    def ask(self) -> Any:
        return self.client.post("/api/knowledge/ask", json={"question": QUESTION})


@pytest.fixture()
def h(tmp_path: Path) -> Iterator[Harness]:
    yield Harness(tmp_path)


# ── S-V10 ──


def test_embedding_change_marks_index_stale(h: Harness) -> None:
    """正控：改配置 → stale 变 True，且"改之前的值"被固化成 indexedModel。

    不固化的话：synced 仍是 None，stale 判不出来，检索照常——这正是负控点名的
    "改配置不重建、一切照旧"。
    """
    before = h.client.get("/api/settings/embedding").json()
    assert before["stale"] is False
    assert before["model"] is None, "从没配过：读出来是 None，不是编一个默认值"

    changed = h.change_embedding("bge-m3")

    assert changed.status_code == 200
    body = changed.json()
    assert body["model"] == "bge-m3"
    # 索引是"改之前"的形态建的：从没配过 → UNCONFIGURED
    assert body["indexedModel"] == system_state.UNCONFIGURED
    assert body["stale"] is True


def test_search_refuses_when_stale_instead_of_returning_empty(h: Harness) -> None:
    """负控：stale 时 `/search` 必须报 index_stale，**不是 200 + 空列表**。

    空列表与"库里没有相关块"在响应里一模一样——用户会去改问题，而不是去重建。
    """
    assert h.search().status_code == 200, "先确认索引本身是通的，否则下一条红得没有意义"

    h.change_embedding()
    refused = h.search()

    assert refused.status_code == 503
    assert refused.json()["code"] == "index_stale"
    assert "重建" in refused.json()["message"]
    assert refused.json()["retryable"] is False, "重试还是同一个库，可重试会引出自旋"


def test_ask_refuses_when_stale_too(h: Harness) -> None:
    """ask 与 search 是两条检索路径，**两条都要过闸门**。

    ask 走 `retrieve`，search 走 `search_fts`——只拦一条，另一条就成了后门。
    """
    h.change_embedding()

    refused = h.ask()

    assert refused.status_code == 503
    assert refused.json()["code"] == "index_stale"
    assert h.chat.calls == 0, "检索就拒绝了，不该已经调过模型"


def test_rebuild_marks_synced_again(h: Harness) -> None:
    """重建完成 → stale 回落，检索恢复。

    "重建完成"目前是直调仓储（全量重建端点归 F2）；这里守的是位的迁移：
    synced 写成"现在想要的"，之后就不再报错。
    """
    h.change_embedding()
    assert h.search().status_code == 503

    h.mark_synced()

    after = h.client.get("/api/settings/embedding").json()
    assert after["stale"] is False
    assert after["indexedModel"] == after["model"] == "bge-m3"
    assert h.search().status_code == 200


def test_embedding_settings_do_not_include_chat_config(h: Harness) -> None:
    """负控：embedding 响应体里不许混入 chat / 供应商字段。

    合在一起 → 设置页并排，有人会一起改；改 chat 不该让索引变脏。
    字段集写成硬断言：加字段必须改这条测试，那时作者会看到这一行注释。
    """
    h.client.post(
        "/api/settings/providers",
        json={"name": "main", "baseUrl": "http://127.0.0.1:9/v1", "apiKey": "sk-1"},
    )

    body = h.client.get("/api/settings/embedding").json()

    assert set(body) == {"model", "indexedModel", "stale"}
    assert not any("provider" in key.lower() or "chat" in key.lower() for key in body)
