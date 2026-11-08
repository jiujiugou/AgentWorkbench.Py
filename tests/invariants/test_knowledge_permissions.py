# ruff: noqa: E501
"""知识库等级权限的判定面（REQ-24 / REQ-25 / ADR 0011）。

这些测试不建 HTTP：判定、可见性集合、去重、检索过滤都能在毫秒级跑完。
接口层只负责把同一份集合传给这些函数；在接口层重复写一遍
`public or rank >= min` 的实现，漏掉的 private 分支不会报错，只会让
某一条路径返回私人库内容。

改坏这里时用户会看到什么：低等级账号在列表、详情或检索里看到私人库内容；
管理员靠改自己的等级或重置高等级账号密码绕过内容边界；同一文件在私人库
与公共库之间被旧去重键错误地合并；降级后历史引用仍显示私人文件名。
"""

from __future__ import annotations

import sqlite3
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentworkbench.core.documents import ChunkDraft
from agentworkbench.core.identity import Owner
from agentworkbench.core.knowledge import (
    KnowledgeBaseVisibility,
    can_read,
    can_transition_visibility,
    require_visibility_transition,
)
from agentworkbench.db import engine
from agentworkbench.db.repositories import documents as documents_repo
from agentworkbench.db.repositories import knowledge as knowledge_repo
from agentworkbench.identity.models import (
    AccountStatus,
    Outcome,
    Principal,
    ReasonCode,
    Role,
)
from agentworkbench.identity.policy import (
    judge_kb_management,
    judge_kb_read,
    judge_level_assignment,
    judge_management,
)
from agentworkbench.llm.embeddings import HashEmbedder
from agentworkbench.rag.indexer import search_fts, write_index
from agentworkbench.rag.retriever import retrieve

TENANT = "team-a"
NOW = datetime(2026, 11, 6, 9, 0, 0, tzinfo=UTC)


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    target = sqlite3.connect(":memory:")
    target.row_factory = sqlite3.Row
    engine.load_vec(target)
    engine.create_schema(target)
    try:
        yield target
    finally:
        target.close()


def principal(level_rank: int, *, role: Role = Role.ADMIN) -> Principal:
    return Principal(
        owner=Owner(user_id="alice", tenant_id=TENANT),
        role=role,
        status=AccountStatus.ACTIVE,
        credential_epoch=1,
        level_rank=level_rank,
    )


def make_kb(
    conn: sqlite3.Connection,
    *,
    name: str,
    visibility: str,
    min_level_rank: int | None = None,
) -> str:
    return knowledge_repo.create(
        conn,
        tenant_id=TENANT,
        name=name,
        visibility=visibility,
        min_level_rank=min_level_rank,
        created_by="alice",
        now=NOW,
    )


# ── I-V20：等级是独立轴，管理员不能自升/越级 ──


def test_admin_cannot_raise_their_own_level_or_grant_above_their_own() -> None:
    actor = principal(2)
    own = judge_level_assignment(actor=actor, target_user_id="alice", target_level_rank=2)
    above = judge_level_assignment(actor=actor, target_user_id="bob", target_level_rank=3)
    equal = judge_level_assignment(actor=actor, target_user_id="bob", target_level_rank=2)

    assert own.outcome is Outcome.FORBIDDEN
    assert own.reason is ReasonCode.LEVEL_INSUFFICIENT
    assert above.outcome is Outcome.FORBIDDEN
    assert above.reason is ReasonCode.LEVEL_INSUFFICIENT
    assert equal.allowed


def test_admin_cannot_manage_an_account_above_their_level() -> None:
    actor = principal(2)
    decision = judge_management(
        actor=actor,
        target_user_id="bob",
        target_role=Role.MEMBER,
        target_level_rank=3,
    )

    assert decision.outcome is Outcome.FORBIDDEN
    assert decision.reason is ReasonCode.LEVEL_INSUFFICIENT


# ── I-V21 / V-20：公共/私人可见性；管理员无内容后门 ──


def test_public_and_private_kbs_have_different_accessible_sets(conn: sqlite3.Connection) -> None:
    public = make_kb(conn, name="公共", visibility="public")
    private = make_kb(conn, name="私人", visibility="private", min_level_rank=3)

    low = knowledge_repo.accessible_kb_ids(conn, tenant_id=TENANT, level_rank=1)
    high = knowledge_repo.accessible_kb_ids(conn, tenant_id=TENANT, level_rank=3)

    assert low == frozenset({public})
    assert high == frozenset({public, private})


def test_low_level_admin_has_no_content_backdoor(conn: sqlite3.Connection) -> None:
    private = make_kb(conn, name="私人", visibility="private", min_level_rank=3)
    documents_repo.create(
        conn,
        doc_id="doc-private",
        tenant_id=TENANT,
        file_name="private.pdf",
        stored_path="seed/private.pdf",
        sha256="private-sha",
        size_bytes=1,
        now="2026-11-06T00:00:00",
        kb_id=private,
    )
    actor = principal(1)

    accessible = knowledge_repo.accessible_kb_ids(conn, tenant_id=TENANT, level_rank=1)
    assert documents_repo.get(conn, "doc-private", tenant_id=TENANT, kb_ids=accessible) is None
    decision = judge_kb_read(
        actor=actor,
        visibility=str(KnowledgeBaseVisibility.PRIVATE),
        min_level_rank=3,
    )
    assert decision.outcome is Outcome.NOT_FOUND
    assert decision.reason is ReasonCode.KB_NOT_VISIBLE
    manage = judge_kb_management(
        actor=actor,
        visibility=str(KnowledgeBaseVisibility.PRIVATE),
        min_level_rank=3,
    )
    assert manage.outcome is Outcome.FORBIDDEN
    assert manage.reason is ReasonCode.LEVEL_INSUFFICIENT


def test_can_read_is_public_or_level_threshold() -> None:
    assert can_read(1, "public", None)
    assert not can_read(2, "private", 3)
    assert can_read(3, "private", 3)
    assert not can_read(5, "private", None)


# ── 状态机：public ↔ private 可迁移，同态不合法 ──


def test_visibility_transition_is_registered_and_terminal_safe() -> None:
    assert can_transition_visibility("public", "private")
    assert can_transition_visibility("private", "public")
    assert not can_transition_visibility("public", "public")
    with pytest.raises(KeyError):
        can_transition_visibility("team", "private")
    with pytest.raises(ValueError):
        require_visibility_transition("private", "private")


# ── V-21：去重按知识库，不跨库共享行 ──


def test_same_file_can_exist_in_two_kbs_but_not_twice_in_one(conn: sqlite3.Connection) -> None:
    public = make_kb(conn, name="公共", visibility="public")
    private = make_kb(conn, name="私人", visibility="private", min_level_rank=3)
    common = {
        "tenant_id": TENANT,
        "file_name": "same.pdf",
        "stored_path": "seed/same.pdf",
        "sha256": "same-sha",
        "size_bytes": 10,
        "now": "2026-11-06T00:00:00",
    }
    documents_repo.create(conn, doc_id="doc-public", kb_id=public, **common)  # type: ignore[arg-type]
    documents_repo.create(conn, doc_id="doc-private", kb_id=private, **common)  # type: ignore[arg-type]

    rows = conn.execute("select id, kb_id from documents order by id").fetchall()
    assert [(row["id"], row["kb_id"]) for row in rows] == [
        ("doc-private", private),
        ("doc-public", public),
    ]

    with pytest.raises(sqlite3.IntegrityError):
        documents_repo.create(conn, doc_id="doc-public-2", kb_id=public, **common)  # type: ignore[arg-type]


# ── G16：检索两条路都按可访问知识库过滤 ──


def _seed_indexed_doc(conn: sqlite3.Connection, *, doc_id: str, kb_id: str, text: str) -> None:
    documents_repo.create(
        conn,
        doc_id=doc_id,
        tenant_id=TENANT,
        file_name=f"{doc_id}.pdf",
        stored_path=f"seed/{doc_id}.pdf",
        sha256=f"sha-{doc_id}",
        size_bytes=len(text),
        now="2026-11-06T00:00:00",
        kb_id=kb_id,
    )
    embedder = HashEmbedder()
    write_index(
        conn,
        document_id=doc_id,
        drafts=[ChunkDraft(seq=0, page_from=1, page_to=1, content=text)],
        embeddings=embedder([text]),
        now="2026-11-06T00:00:00",
    )


def test_retrieval_filters_text_and_vector_routes_by_accessible_kb(
    conn: sqlite3.Connection,
) -> None:
    public = make_kb(conn, name="公共", visibility="public")
    private = make_kb(conn, name="私人", visibility="private", min_level_rank=3)
    _seed_indexed_doc(conn, doc_id="doc-public", kb_id=public, text="公共资料：设备保养记录。")
    _seed_indexed_doc(conn, doc_id="doc-private", kb_id=private, text="私人资料：报价单编号。")
    conn.commit()

    public_only = retrieve(
        conn,
        "资料",
        embedder=HashEmbedder(),
        tenant_id=TENANT,
        kb_ids=frozenset({public}),
        top_k=5,
    )
    assert {hit.document_id for hit in public_only} == {"doc-public"}

    text_only = search_fts(
        conn,
        "资料",
        tenant_id=TENANT,
        kb_ids=frozenset({public}),
        limit=5,
    )
    assert {hit.document_id for hit in text_only} == {"doc-public"}

    both = retrieve(
        conn,
        "资料",
        embedder=HashEmbedder(),
        tenant_id=TENANT,
        kb_ids=frozenset({public, private}),
        top_k=5,
    )
    assert {hit.document_id for hit in both} == {"doc-public", "doc-private"}


# ── V-22：默认公共库兜底；新文档不会留下 kb_id 为空的行 ──


def test_documents_created_without_kb_are_assigned_to_default_public(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "insert into documents(id, tenant_id, file_name, stored_path, sha256,"
        " size_bytes, status, page_count, created_at, updated_at)"
        " values ('doc-legacy', 'legacy-team', 'legacy.pdf', 'seed/legacy.pdf',"
        " 'legacy-sha', 1, 'parsed', 1, '2026-11-06T00:00:00', '2026-11-06T00:00:00')"
    )
    row = conn.execute("select kb_id from documents where id = 'doc-legacy'").fetchone()
    assert row is not None and row["kb_id"] is not None
    kb = knowledge_repo.get(conn, str(row["kb_id"]), tenant_id="legacy-team")
    assert kb is not None
    assert str(kb["visibility"]) == "public"
    assert bool(kb["is_default"])


# ?? V-22???????????????????? kb_id ?? ??


def test_ensure_knowledge_schema_backfills_legacy_rows(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    conn = engine.connect(path)
    try:
        conn.executescript(
            "create table tenants (id text primary key, name text not null, created_at text not null);"
            "create table users (id text primary key, tenant_id text not null, email text not null,"
            " password_hash text not null, role text not null, status text not null,"
            " credential_epoch integer not null default 1, lockout_until text,"
            " failed_attempts integer not null default 0, is_platform_admin integer not null default 0,"
            " must_change_password integer not null default 0, created_at text not null, updated_at text not null);"
            "create table documents (id text primary key, tenant_id text not null, file_name text not null,"
            " stored_path text not null, sha256 text not null, size_bytes integer not null, status text not null,"
            " page_count integer not null default 0, error_code text, error text, created_at text not null,"
            " updated_at text not null, parsed_at text);"
            "create table conversations (id text primary key, user_id text not null, tenant_id text not null,"
            " kind text not null default 'knowledge', title text not null, created_at text not null,"
            " updated_at text not null);"
            "create table messages (id integer primary key autoincrement, conversation_id text not null,"
            " role text not null, content text not null, run_id text, created_at text not null);"
            "create table message_citations (id integer primary key autoincrement, message_id integer not null,"
            " no integer not null, file_name text not null, seq integer not null, excerpt text not null,"
            " check_first text not null, check_second text, coverage real, unique(message_id, no));"
            "insert into tenants values ('t-legacy', '???', '2026-11-06T00:00:00');"
            "insert into users (id, tenant_id, email, password_hash, role, status, created_at, updated_at)"
            " values ('u-legacy', 't-legacy', 'old@example.com', 'h', 'admin', 'active',"
            " '2026-11-06T00:00:00', '2026-11-06T00:00:00');"
            "insert into documents (id, tenant_id, file_name, stored_path, sha256, size_bytes, status,"
            " page_count, created_at, updated_at) values ('doc-legacy', 't-legacy', 'legacy.pdf',"
            " 'seed/legacy.pdf', 'sha-legacy', 1, 'parsed', 1, '2026-11-06T00:00:00',"
            " '2026-11-06T00:00:00');"
            "insert into conversations values ('c-legacy', 'u-legacy', 't-legacy', 'knowledge',"
            " '???', '2026-11-06T00:00:00', '2026-11-06T00:00:00');"
            "insert into messages (conversation_id, role, content, created_at)"
            " values ('c-legacy', 'assistant', '???', '2026-11-06T00:00:00');"
            "insert into message_citations (message_id, no, file_name, seq, excerpt, check_first)"
            " values (1, 1, '???.pdf', 1, '???', 'kept');"
        )
        conn.commit()

        engine.ensure_knowledge_schema(conn)
        conn.commit()

        doc = conn.execute("select kb_id from documents where id = 'doc-legacy'").fetchone()
        citation = conn.execute("select kb_id from message_citations where id = 1").fetchone()
        kb = conn.execute(
            "select visibility, is_default from knowledge_bases where tenant_id = 't-legacy'"
        ).fetchone()
        assert doc is not None and doc["kb_id"] is not None
        assert citation is not None and citation["kb_id"] == doc["kb_id"]
        assert kb is not None and str(kb["visibility"]) == "public" and bool(kb["is_default"])
        assert "level_rank" in {str(row[1]) for row in conn.execute("pragma table_info(users)")}
    finally:
        conn.close()


def test_visibility_state_machine_matches_toml_registration() -> None:
    root = Path(__file__).resolve().parents[2]
    with (root / "pyproject.toml").open("rb") as handle:
        machines = tomllib.load(handle)["tool"]["agentworkbench"]["state_machine"]
    ours = {
        row["name"]: {state.strip() for state in row["states"].split(",")}
        for row in machines
        if row["source"] == "agentworkbench.core.knowledge"
    }
    from agentworkbench.core.knowledge import VISIBILITY_TRANSITIONS

    assert ours, "toml ????? core.knowledge ????"
    assert set(VISIBILITY_TRANSITIONS) == ours["knowledge_base_visibility"]
