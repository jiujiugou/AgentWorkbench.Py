"""D3 / P0-3: history survives reindex, a second client, and an app restart."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from harness import ALICE, PASSWORD, TEAM_A_SECRET, FakeChat, TwoTeams, build_two_teams

from agentworkbench.api.app import create_app
from agentworkbench.api.deps import build_deps
from agentworkbench.core.documents import ChunkDraft
from agentworkbench.llm.embeddings import HashEmbedder
from agentworkbench.rag.indexer import clear_index, write_index

QUESTION = "液压站 E2 的故障怎么排查？"
NEW_TEXT = "重建后的文档：只有冷却塔水位需要检查。"
RENAMED = "renamed-after-reindex.pdf"


def _login(client: TestClient, email: str) -> None:
    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


def _new_client(teams: TwoTeams, *, cookie: str | None = None) -> TestClient:
    client = TestClient(teams.app, raise_server_exceptions=False)
    if cookie is None:
        _login(client, ALICE)
    else:
        client.cookies.set("awb_session", cookie)
    return client


def _history(client: TestClient, conversation_id: str) -> dict[str, Any]:
    response = client.get(f"/api/conversations/{conversation_id}/messages")
    assert response.status_code == 200, response.text
    return dict(response.json())


def _trace(client: TestClient, run_id: str) -> dict[str, Any]:
    response = client.get(f"/api/runs/{run_id}/trace")
    assert response.status_code == 200, response.text
    return dict(response.json())


def _ask(teams: TwoTeams) -> tuple[str, str]:
    response = teams.client_a.post("/api/knowledge/ask", json={"question": QUESTION})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded", body
    assert body["answer"]
    return str(body["runId"]), str(body["conversationId"])


def _reindex_with_different_content(teams: TwoTeams) -> None:
    embedder = HashEmbedder()
    with closing(teams.open_conn()) as conn:
        clear_index(conn, teams.doc_a)
        conn.execute(
            "update documents set file_name = ? where id = ?",
            (RENAMED, teams.doc_a),
        )
        write_index(
            conn,
            document_id=teams.doc_a,
            drafts=[ChunkDraft(seq=0, page_from=1, page_to=1, content=NEW_TEXT)],
            embeddings=embedder([NEW_TEXT]),
            now="2026-11-05T00:00:00",
        )
        conn.commit()


@pytest.fixture()
def teams(tmp_path: Path) -> Iterator[TwoTeams]:
    teams = build_two_teams(tmp_path)
    teams.deps.chat_factory = lambda _conn: (
        FakeChat(answer=f"{TEAM_A_SECRET}[1]"),
        "glm-4-flash",
    )
    yield teams


def test_reindex_does_not_change_history_or_citations(teams: TwoTeams) -> None:
    run_id, conversation_id = _ask(teams)
    before = _history(teams.client_a, conversation_id)
    before_trace = _trace(teams.client_a, run_id)

    assistant = next(message for message in before["messages"] if message["role"] == "assistant")
    assert assistant["citations"], "ask must produce a citation before reindex is meaningful"
    citation = assistant["citations"][0]
    assert citation["excerpt"] == TEAM_A_SECRET
    assert citation["fileName"] == f"{teams.doc_a}.pdf"

    _reindex_with_different_content(teams)

    after = _history(teams.client_a, conversation_id)
    assert after == before
    assert _trace(teams.client_a, run_id) == before_trace


def test_same_account_on_another_client_sees_identical_history(teams: TwoTeams) -> None:
    run_id, conversation_id = _ask(teams)
    first = _history(teams.client_a, conversation_id)
    second_client = _new_client(teams)

    assert _history(second_client, conversation_id) == first
    assert (
        second_client.get("/api/conversations").json()
        == teams.client_a.get("/api/conversations").json()
    )
    assert _trace(second_client, run_id) == _trace(teams.client_a, run_id)


def test_history_survives_an_app_restart_with_the_same_cookie(teams: TwoTeams) -> None:
    run_id, conversation_id = _ask(teams)
    before = _history(teams.client_a, conversation_id)
    before_trace = _trace(teams.client_a, run_id)
    raw_cookie = teams.client_a.cookies["awb_session"]

    restarted_deps = build_deps(teams.settings)
    restarted_app = create_app(settings=teams.settings, deps=restarted_deps)
    restarted = TestClient(restarted_app, raise_server_exceptions=False)
    restarted.cookies.set("awb_session", raw_cookie)

    assert restarted.get("/api/auth/me").status_code == 200
    assert _history(restarted, conversation_id) == before
    assert _trace(restarted, run_id) == before_trace
