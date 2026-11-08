"""ADR 0012：原件下载、知识库归档与管理员审计读取。

这些用例走 HTTP，因为三个风险都发生在「另一个出口」上：
列表挡住了但下载没挡、归档后用删除代替、审计只写不读。
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from harness import PASSWORD, TwoTeams, build_two_teams

from agentworkbench.db.repositories import documents as documents_repo

NOW = "2026-11-06T00:00:00"


@pytest.fixture()
def teams(tmp_path: Path) -> TwoTeams:
    return build_two_teams(tmp_path)


def _default_kb(teams: TwoTeams, tenant_id: str) -> str:
    with closing(teams.open_conn()) as conn:
        row = conn.execute(
            "select id from knowledge_bases where tenant_id = ? and is_default = 1",
            (tenant_id,),
        ).fetchone()
    assert row is not None
    return str(row["id"])


def _member_client(teams: TwoTeams) -> TestClient:
    client = TestClient(teams.app, raise_server_exceptions=False)
    response = client.post(
        "/api/auth/login",
        json={"email": "carol@example.com", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return client


def _add_document(teams: TwoTeams, *, doc_id: str, kb_id: str, file_name: str) -> None:
    with closing(teams.open_conn()) as conn:
        documents_repo.create(
            conn,
            doc_id=doc_id,
            tenant_id=teams.tenant_b,
            kb_id=kb_id,
            file_name=file_name,
            stored_path=f"seed/{doc_id}.pdf",
            sha256=f"sha-{doc_id}",
            size_bytes=10,
            now=NOW,
        )
        conn.execute("update documents set status = 'parsed' where id = ?", (doc_id,))
        conn.commit()


def test_original_download_uses_the_same_visibility_as_content(teams: TwoTeams) -> None:
    relative = teams.deps.storage.put(
        tenant_id=teams.tenant_b,
        digest="sha-doc-b",
        suffix=".pdf",
        data=b"original bytes",
    )
    with closing(teams.open_conn()) as conn:
        conn.execute(
            "update documents set stored_path = ? where id = ?",
            (relative, teams.doc_b),
        )
        conn.commit()

    allowed = teams.client_b.get(f"/api/documents/{teams.doc_b}/file")
    assert allowed.status_code == 200
    assert allowed.content == b"original bytes"
    assert "attachment" in allowed.headers["content-disposition"]

    assert teams.client_a.get(f"/api/documents/{teams.doc_b}/file").status_code == 404


def test_download_rejects_a_path_outside_the_storage_root(teams: TwoTeams) -> None:
    with closing(teams.open_conn()) as conn:
        conn.execute(
            "update documents set stored_path = '../outside.pdf' where id = ?",
            (teams.doc_b,),
        )
        conn.commit()

    assert teams.client_b.get(f"/api/documents/{teams.doc_b}/file").status_code == 404


def test_archiving_hides_content_but_keeps_the_rows(teams: TwoTeams) -> None:
    created = teams.client_b.post(
        "/api/knowledge-bases",
        json={"name": "archive-me", "visibility": "public"},
    )
    assert created.status_code == 201, created.text
    kb_id = str(created.json()["id"])
    _add_document(teams, doc_id="doc-archived", kb_id=kb_id, file_name="archived.pdf")

    before = teams.client_b.get("/api/documents").json()
    assert any(item["id"] == "doc-archived" for item in before)

    archived = teams.client_b.post(f"/api/knowledge-bases/{kb_id}/archive")
    assert archived.status_code == 204, archived.text
    assert teams.client_b.get("/api/documents/doc-archived").status_code == 404
    assert all(item["id"] != "doc-archived" for item in teams.client_b.get("/api/documents").json())

    with closing(teams.open_conn()) as conn:
        row = conn.execute(
            "select archived_at from knowledge_bases where id = ?", (kb_id,)
        ).fetchone()
        doc = conn.execute("select 1 from documents where id = 'doc-archived'").fetchone()
    assert row is not None and row["archived_at"] is not None
    assert doc is not None, "归档不能删除文档行"

    upload = teams.client_b.post(
        "/api/documents",
        files={"file": ("new.pdf", b"%PDF-1.4\nnew", "application/pdf")},
        data={"kb_id": kb_id},
    )
    assert upload.status_code == 400

    restored = teams.client_b.post(f"/api/knowledge-bases/{kb_id}/unarchive")
    assert restored.status_code == 204, restored.text
    assert teams.client_b.get("/api/documents/doc-archived").status_code == 200


def test_default_kb_cannot_be_archived_or_made_private(teams: TwoTeams) -> None:
    default = _default_kb(teams, teams.tenant_b)

    assert teams.client_b.post(f"/api/knowledge-bases/{default}/archive").status_code == 400
    changed = teams.client_b.patch(
        f"/api/knowledge-bases/{default}",
        json={"visibility": "private", "minLevelRank": 1},
    )
    assert changed.status_code == 400


def test_duplicate_kb_names_are_a_client_error(teams: TwoTeams) -> None:
    first = teams.client_b.post(
        "/api/knowledge-bases",
        json={"name": "duplicate", "visibility": "public"},
    )
    assert first.status_code == 201, first.text
    second = teams.client_b.post(
        "/api/knowledge-bases",
        json={"name": "duplicate", "visibility": "public"},
    )
    assert second.status_code == 400

    other = teams.client_b.post(
        "/api/knowledge-bases",
        json={"name": "other", "visibility": "public"},
    )
    assert other.status_code == 201, other.text
    renamed = teams.client_b.patch(
        f"/api/knowledge-bases/{other.json()['id']}",
        json={"name": "duplicate"},
    )
    assert renamed.status_code == 400


def test_admin_events_are_admin_only_tenant_scoped_and_audited(teams: TwoTeams) -> None:
    invite = teams.client_b.post(
        "/api/members/invites",
        json={"role": "member", "email": "new@example.com"},
    )
    assert invite.status_code == 201, invite.text
    invited = TestClient(teams.app, raise_server_exceptions=False).post(
        f"/api/auth/invites/{invite.json()['token']}",
        json={"email": "new@example.com", "password": "long-enough-password"},
    )
    assert invited.status_code == 201, invited.text
    assert teams.client_b.post(f"/api/members/{teams.member_b}/disable").status_code == 204
    assert teams.client_b.post(f"/api/members/{teams.member_b}/enable").status_code == 204

    page = teams.client_b.get("/api/admin/events", params={"limit": 2})
    assert page.status_code == 200, page.text
    body = page.json()
    assert len(body["events"]) == 2
    assert body["nextCursor"] is not None

    second = teams.client_b.get(
        "/api/admin/events",
        params={"limit": 2, "before": body["nextCursor"]},
    )
    assert second.status_code == 200
    first_ids = {row["eventId"] for row in body["events"]}
    second_ids = {row["eventId"] for row in second.json()["events"]}
    assert first_ids.isdisjoint(second_ids)

    all_actions = {
        row["action"]
        for row in teams.client_b.get("/api/admin/events", params={"limit": 20}).json()["events"]
    }
    assert {"invite.create", "user.accept_invite", "user.disable", "user.enable"} <= all_actions

    member = _member_client(teams)
    assert member.get("/api/admin/events").status_code == 403

    with closing(teams.open_conn()) as conn:
        team_a_count = conn.execute(
            "select count(*) from admin_events where tenant_id = ?", (teams.tenant_a,)
        ).fetchone()[0]
    assert team_a_count == 0


def test_pending_invites_can_be_listed_and_revoked(teams: TwoTeams) -> None:
    created = teams.client_b.post(
        "/api/members/invites",
        json={"role": "member", "email": "revoked@example.com"},
    )
    assert created.status_code == 201, created.text
    invite_id = str(created.json()["inviteId"])

    pending = teams.client_b.get("/api/members/invites")
    assert pending.status_code == 200
    assert [row["inviteId"] for row in pending.json()] == [invite_id]

    revoked = teams.client_b.delete(f"/api/members/invites/{invite_id}")
    assert revoked.status_code == 204, revoked.text
    assert teams.client_b.get("/api/members/invites").json() == []

    accepted = TestClient(teams.app, raise_server_exceptions=False).post(
        f"/api/auth/invites/{created.json()['token']}",
        json={"email": "revoked@example.com", "password": "long-enough-password"},
    )
    assert accepted.status_code == 404
