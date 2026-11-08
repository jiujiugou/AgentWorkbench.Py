"""历史读侧的等级过滤（C-V14）：引用占位、证据过滤、正文保留、恢复可见。

这条用真实会话/消息/引用表，而不是断言路由里的某个字符串：
「降级后仍能看到文件名」是数据出口问题，只有把整条读侧跑一遍才能证明。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from harness import TwoTeams, build_two_teams

from agentworkbench.core.conversation import CitationSnapshot, MessageRole
from agentworkbench.core.identity import Owner
from agentworkbench.core.judgment import CitationFirstCheck
from agentworkbench.db.repositories import conversations as conversations_repo
from agentworkbench.db.repositories import knowledge as knowledge_repo
from agentworkbench.identity import store

NOW = "2026-11-06T00:00:00"


@pytest.fixture()
def teams(tmp_path: Path) -> TwoTeams:
    return build_two_teams(tmp_path)


def _set_level(teams: TwoTeams, *, expected: int, target: int) -> None:
    with teams.open_conn() as conn:
        store.set_level(
            conn,
            teams.user_b,
            expected=expected,
            target=target,
            now=datetime.now(UTC),
        )
        conn.commit()


def _seed_private_citation(teams: TwoTeams) -> tuple[str, int]:
    with teams.open_conn() as conn:
        private = knowledge_repo.create(
            conn,
            tenant_id=teams.tenant_b,
            name="私人",
            visibility="private",
            min_level_rank=3,
            created_by=teams.user_b,
            now=datetime.now(UTC),
        )
        message_id = conversations_repo.insert_message(
            conn,
            Owner(user_id=teams.user_b, tenant_id=teams.tenant_b),
            conversation_id=teams.conv_b,
            role=MessageRole.ASSISTANT,
            content="答案正文保留。",
            run_id=teams.run_b,
            now=NOW,
        )
        conversations_repo.insert_citations(
            conn,
            Owner(user_id=teams.user_b, tenant_id=teams.tenant_b),
            message_id=message_id,
            snapshots=[
                CitationSnapshot(
                    no=1,
                    file_name="私人报价单.pdf",
                    seq=3,
                    excerpt="私人报价单片段",
                    check_first=CitationFirstCheck.KEPT,
                    kb_id=private,
                )
            ],
        )
        conn.execute(
            "insert into agent_steps (run_id, seq, kind, output, created_at)"
            " values (?, 99, 'retrieval', ?, ?)",
            (
                teams.run_b,
                '{"hits": 1, "top": [{"kb_id": "'
                + private
                + '", "document_id": "doc-secret", "file_name": "私人报价单.pdf"}]}',
                NOW,
            ),
        )
        conn.commit()
    return private, message_id


def test_history_citation_and_trace_are_filtered_by_current_level(teams: TwoTeams) -> None:
    _set_level(teams, expected=1, target=3)
    _private, _ = _seed_private_citation(teams)

    visible = teams.client_b.get(f"/api/conversations/{teams.conv_b}/messages").json()
    citation = visible["messages"][-1]["citations"][0]
    assert citation["fileName"] == "私人报价单.pdf"
    assert citation["excerpt"] == "私人报价单片段"

    _set_level(teams, expected=3, target=1)
    hidden = teams.client_b.get(f"/api/conversations/{teams.conv_b}/messages").json()
    hidden_message = hidden["messages"][-1]
    hidden_citation = hidden_message["citations"][0]
    assert "答案正文保留" in hidden_message["content"], "正文必须保留"
    assert hidden_citation["fileName"] == "来源已不可访问"
    assert hidden_citation["excerpt"] == ""
    assert hidden_citation["seq"] == 0
    assert "私人报价单" not in str(hidden)

    trace = teams.client_b.get(f"/api/runs/{teams.run_b}/trace").json()
    retrieval = next(step for step in trace["steps"] if step["kind"] == "retrieval")
    assert "doc-secret" not in (retrieval["output"] or "")
    assert "私人报价单" not in (retrieval["output"] or "")
    assert "redacted" in (retrieval["output"] or "")

    _set_level(teams, expected=1, target=3)
    restored = teams.client_b.get(f"/api/conversations/{teams.conv_b}/messages").json()
    assert restored["messages"][-1]["citations"][0]["fileName"] == "私人报价单.pdf"


def test_low_level_admin_cannot_read_private_document(teams: TwoTeams) -> None:
    with teams.open_conn() as conn:
        private = knowledge_repo.create(
            conn,
            tenant_id=teams.tenant_b,
            name="私人",
            visibility="private",
            min_level_rank=3,
            created_by=teams.user_b,
            now=datetime.now(UTC),
        )
        conn.execute(
            "insert into documents(id, tenant_id, kb_id, file_name, stored_path, sha256,"
            " size_bytes, status, page_count, created_at, updated_at)"
            " values ('doc-private', ?, ?, 'private.pdf', 'seed/private.pdf', 'sha-private',"
            " 1, 'parsed', 1, ?, ?)",
            (teams.tenant_b, private, NOW, NOW),
        )
        conn.commit()

    response = teams.client_b.get("/api/documents/doc-private")
    assert response.status_code == 404
    listing = teams.client_b.get("/api/documents").json()
    assert all(item["id"] != "doc-private" for item in listing)


def test_admin_cannot_create_a_private_kb_above_their_level(teams: TwoTeams) -> None:
    _set_level(teams, expected=1, target=2)

    denied = teams.client_b.post(
        "/api/knowledge-bases",
        json={"name": "????", "visibility": "private", "minLevelRank": 3},
    )
    assert denied.status_code == 403

    allowed = teams.client_b.post(
        "/api/knowledge-bases",
        json={"name": "L2 ??", "visibility": "private", "minLevelRank": 2},
    )
    assert allowed.status_code == 201
    assert allowed.json()["visibility"] == "private"


def test_direct_account_creation_cannot_exceed_admin_level(teams: TwoTeams) -> None:
    _set_level(teams, expected=1, target=3)
    denied = teams.client_b.post(
        "/api/members",
        json={
            "email": "high@example.com",
            "password": "a-long-enough-password",
            "role": "member",
            "levelRank": 4,
        },
    )
    assert denied.status_code == 403

    created = teams.client_b.post(
        "/api/members",
        json={
            "email": "peer@example.com",
            "password": "a-long-enough-password",
            "role": "member",
            "levelRank": 3,
        },
    )
    assert created.status_code == 201
    assert created.json()["levelRank"] == 3


def test_upload_into_selected_kb_and_reject_higher_private(teams: TwoTeams) -> None:
    teams.deps.auto_process = False
    with teams.open_conn() as conn:
        allowed_kb = knowledge_repo.create(
            conn,
            tenant_id=teams.tenant_b,
            name="L1 private",
            visibility="private",
            min_level_rank=1,
            created_by=teams.user_b,
            now=datetime.now(UTC),
        )
        denied_kb = knowledge_repo.create(
            conn,
            tenant_id=teams.tenant_b,
            name="L3 private",
            visibility="private",
            min_level_rank=3,
            created_by=teams.user_b,
            now=datetime.now(UTC),
        )
        conn.commit()

    accepted = teams.client_b.post(
        "/api/documents",
        files={"file": ("selected.pdf", b"%PDF-1.4\nselected", "application/pdf")},
        data={"kb_id": allowed_kb},
    )
    assert accepted.status_code == 202, accepted.text
    body = accepted.json()
    assert body["kbId"] == allowed_kb
    with teams.open_conn() as conn:
        row = conn.execute("select kb_id from documents where id = ?", (body["id"],)).fetchone()
        assert row is not None and row["kb_id"] == allowed_kb

    denied = teams.client_b.post(
        "/api/documents",
        files={"file": ("denied.pdf", b"%PDF-1.4\ndenied", "application/pdf")},
        data={"kb_id": denied_kb},
    )
    assert denied.status_code == 403
    with teams.open_conn() as conn:
        count = conn.execute(
            "select count(*) from documents where tenant_id = ? and kb_id = ? and file_name = ?",
            (teams.tenant_b, denied_kb, "denied.pdf"),
        ).fetchone()[0]
        assert count == 0
