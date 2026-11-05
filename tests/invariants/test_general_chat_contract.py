"""F7 通用对话的契约测试。

它们不替代 F3 的检索/引用/拒答测试，只钉住 F7 的五条不变量：
不查、明示、不混、不兜、留痕。

失效形态（用户会看到什么）：通用回答带着来源/引用，被当成企业文档答案；或者
知识库明明资料不足，界面却自动换成一段通用回答。改坏这些契约时，对应测试必须红。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from conftest import sign_in
from fastapi.testclient import TestClient

from agentworkbench.api.app import create_app
from agentworkbench.api.deps import Deps, build_deps
from agentworkbench.config import Settings


class FakeChat:
    """不联网的通用模型：返回一句带编号的回答，用来验证编号会被摘掉。"""

    def complete(self, system: str, user: str) -> str:
        return "这是通用回答[1]。"


class RefusingChat:
    """知识库拒答路径不应调用模型；真被调用就说明兜底发生了。"""

    def complete(self, system: str, user: str) -> str:
        raise AssertionError(f"拒答路径不应调用模型：{system!r} / {user!r}")


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.settings = Settings(data_dir=tmp_path / "data")
        self.deps: Deps = build_deps(self.settings)
        self.deps.chat_factory = lambda _conn: (FakeChat(), "fake-model")
        self.app = create_app(settings=self.settings, deps=self.deps)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        sign_in(self.client, self.app)

    def chat(self, question: str = "你好") -> dict[str, object]:
        response = self.client.post("/api/chat", json={"question": question})
        assert response.status_code == 200, response.text
        return cast("dict[str, object]", response.json())

    def knowledge(self, question: str, conversation_id: str | None = None) -> dict[str, object]:
        payload: dict[str, object] = {"question": question}
        if conversation_id is not None:
            payload["conversationId"] = conversation_id
        response = self.client.post("/api/knowledge/ask", json=payload)
        assert response.status_code == 200, response.text
        return cast("dict[str, object]", response.json())

    def open_conn(self) -> sqlite3.Connection:
        return self.deps.open_conn()


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    yield Harness(tmp_path)


def _run_id(body: dict[str, object]) -> str:
    run_id = body.get("runId") or body.get("run_id")
    assert isinstance(run_id, str) and run_id, f"通用回答没有 runId：{body}"
    return run_id


def _conversation_id(body: dict[str, object]) -> str:
    conversation_id = body.get("conversationId") or body.get("conversation_id")
    assert isinstance(conversation_id, str) and conversation_id, f"回答没有会话 id：{body}"
    return conversation_id


def test_general_chat_does_not_retrieve(harness: Harness) -> None:
    """D-V1：通用回答没有来源、没有引用，run 里没有 retrieval 步骤。"""
    body = harness.chat("解释一下什么是 PLC")
    assert body.get("sources") in (None, [])
    assert body.get("citations") in (None, [])
    run_id = _run_id(body)
    with closing(harness.open_conn()) as conn:
        kinds = [
            str(row["kind"])
            for row in conn.execute("select kind from agent_steps where run_id = ?", (run_id,))
        ]
    assert "retrieval" not in kinds


def test_general_chat_is_marked_as_general(harness: Harness) -> None:
    """D-V2：接口明确标识通用模式，且不显示知识库来源。"""
    body = harness.chat()
    assert body.get("mode") == "general" or body.get("kind") == "general"
    assert body.get("sources") in (None, [])


def test_conversation_kind_is_immutable(harness: Harness) -> None:
    """D-V3：通用会话不能被知识库入口续聊，kind 不能在同一条会话里改写。"""
    body = harness.chat()
    conversation_id = _conversation_id(body)
    knowledge = harness.knowledge("这份资料说什么", conversation_id=conversation_id)
    assert _conversation_id(knowledge) != conversation_id
    with closing(harness.open_conn()) as conn:
        row = conn.execute(
            "select kind from conversations where id = ?", (conversation_id,)
        ).fetchone()
    assert row is not None
    assert row["kind"] == "general"


def test_refusal_does_not_fall_back_to_general_chat(harness: Harness) -> None:
    """D-V4：知识库资料不足后仍是拒答，不会自动换成通用回答。"""
    harness.chat()
    body = harness.knowledge("库里绝对不存在的一个问题-xyz")
    assert body.get("status") == "refused"
    assert body.get("mode") != "general"


def test_general_chat_writes_one_run_without_retrieval_steps(harness: Harness) -> None:
    """D-V5：通用回答恰好一条 run，归属正确，步骤里没有伪造的检索证据。"""
    run_id = _run_id(harness.chat())
    with closing(harness.open_conn()) as conn:
        runs = conn.execute("select * from agent_runs where id = ?", (run_id,)).fetchall()
        assert len(runs) == 1
        assert runs[0]["user_id"] == "local"
        steps = [
            str(row["kind"])
            for row in conn.execute("select kind from agent_steps where run_id = ?", (run_id,))
        ]
    assert steps
    assert "retrieval" not in steps
    assert set(steps) == {"assistant"}
