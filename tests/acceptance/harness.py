# ruff: noqa: S105, S106 —— 本文件的口令与密钥全是测试值。
"""D2 验收夹具：两个真实团队，各自带一份文档、一条会话、一次运行。

这里不复用 `tests/invariants/test_tenant_isolation.py` 的夹具：验收层的对象
要同时覆盖「文档 / 会话 / 运行 / 成员」四类资源，而隔离层的夹具只造前三类。
把两套夹具合并会让隔离测试为了验收层的需要多造一条成员行——那不是它要验的东西。

**不用 `settings.tenant_id` 造数据。** 两个团队都由仓储直接建，tenant_id 由库生成；
团队甲的首个管理员带 `is_platform_admin`，与 `service.bootstrap` 的结果一致。
这正是 D2 要验的形态：团队不是配置常量。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentworkbench.api.app import create_app
from agentworkbench.api.deps import Deps, build_deps
from agentworkbench.chat import conversation as chat_turn
from agentworkbench.config import AuthSettings, Settings
from agentworkbench.core.documents import ChunkDraft
from agentworkbench.core.identity import Owner
from agentworkbench.core.runs import RunStatus
from agentworkbench.identity import credentials, store
from agentworkbench.identity.models import Role
from agentworkbench.llm.embeddings import HashEmbedder
from agentworkbench.rag.indexer import write_index
from agentworkbench.trace.recorder import Recorder

PASSWORD = "correct horse battery staple"
ALICE = "alice@example.com"
BOB = "bob@example.com"
CAROL = "carol@example.com"

TEAM_A_SECRET = "液压站 E2 报警的处理步骤：先查波特率，再查站号。"
TEAM_B_SECRET = "竞争对手的报价单编号 QX-2026-004，单价与折扣表在附件里。"
TEAM_B_ONLY_WORD = "报价单编号"

NOW = "2026-11-04T00:00:00"

_PASSWORD_HASH: str | None = None


def _password_hash() -> str:
    """argon2 很慢；夹具只算一次，别让 12 个用例各算两遍。"""
    global _PASSWORD_HASH
    if _PASSWORD_HASH is None:
        _PASSWORD_HASH = credentials.hash_password(PASSWORD)
    return _PASSWORD_HASH


def _seed_document(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    doc_id: str,
    text: str,
) -> None:
    conn.execute(
        "insert or ignore into documents(id, tenant_id, file_name, stored_path, sha256,"
        " size_bytes, status, page_count, created_at, updated_at)"
        " values (?, ?, ?, ?, ?, ?, 'parsed', 1, ?, ?)",
        (
            doc_id,
            tenant_id,
            f"{doc_id}.pdf",
            f"seed/{doc_id}.pdf",
            f"sha-{doc_id}",
            1024,
            NOW,
            NOW,
        ),
    )
    embedder = HashEmbedder()
    write_index(
        conn,
        document_id=doc_id,
        drafts=[ChunkDraft(seq=0, page_from=1, page_to=1, content=text)],
        embeddings=embedder([text]),
        now=NOW,
    )
    conn.commit()


def _seed_conversation(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    user_id: str,
    question: str,
) -> str:
    started = chat_turn.start_turn(
        conn,
        Owner(user_id=user_id, tenant_id=tenant_id),
        question=question,
        now=NOW,
    )
    return started.conversation_id


def _seed_run(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    user_id: str,
    question: str,
) -> str:
    recorder = Recorder(conn, user_id=user_id, tenant_id=tenant_id)
    run_id = recorder.start(question=question, prompt_version="v1")
    recorder.finish(RunStatus.SUCCEEDED)
    return run_id


def _login(client: TestClient, email: str) -> None:
    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


@dataclass
class TwoTeams:
    """两个团队 + 四类他方资源。字段名里的 `b` 表示「团队乙的」。"""

    app: FastAPI
    deps: Deps
    settings: Settings
    client_a: TestClient
    client_b: TestClient
    tenant_a: str
    user_a: str
    tenant_b: str
    user_b: str
    member_b: str
    doc_a: str
    doc_b: str
    conv_a: str
    conv_b: str
    run_a: str
    run_b: str

    def open_conn(self) -> sqlite3.Connection:
        return self.deps.open_conn()

    def events_for(self, request_id: str) -> list[sqlite3.Row]:
        with closing(self.open_conn()) as conn:
            return conn.execute(
                "select * from identity_events where request_id = ?", (request_id,)
            ).fetchall()

    def runs_for(self, tenant_id: str) -> list[sqlite3.Row]:
        with closing(self.open_conn()) as conn:
            return conn.execute(
                "select * from agent_runs where tenant_id = ? order by rowid", (tenant_id,)
            ).fetchall()

    def wait_until(self, condition: Callable[[], bool], *, what: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.01)
        raise AssertionError(f"超时：{what}")


def build_two_teams(tmp_path: Path) -> TwoTeams:
    settings = Settings(
        data_dir=tmp_path / "data",
        secret_key="acceptance-test-secret",
        auth=AuthSettings(session_invalidate_seconds=0),
    )
    deps = build_deps(settings)
    app = create_app(settings=settings, deps=deps)

    password_hash = _password_hash()
    conn = deps.open_conn()
    try:
        tenant_a = store.insert_tenant(conn, name="团队甲", now=datetime.now(UTC))
        user_a = store.insert_user(
            conn,
            tenant_id=tenant_a,
            email=ALICE,
            password_hash=password_hash,
            role=Role.ADMIN,
            now=datetime.now(UTC),
            is_platform_admin=True,
        )
        tenant_b = store.insert_tenant(conn, name="团队乙", now=datetime.now(UTC))
        user_b = store.insert_user(
            conn,
            tenant_id=tenant_b,
            email=BOB,
            password_hash=password_hash,
            role=Role.ADMIN,
            now=datetime.now(UTC),
        )
        member_b = store.insert_user(
            conn,
            tenant_id=tenant_b,
            email=CAROL,
            password_hash=password_hash,
            role=Role.MEMBER,
            now=datetime.now(UTC),
        )
        conn.commit()

        _seed_document(conn, tenant_id=tenant_a, doc_id="doc-a", text=TEAM_A_SECRET)
        _seed_document(conn, tenant_id=tenant_b, doc_id="doc-b", text=TEAM_B_SECRET)
        conv_a = _seed_conversation(conn, tenant_id=tenant_a, user_id=user_a, question="A 的问题")
        conv_b = _seed_conversation(conn, tenant_id=tenant_b, user_id=user_b, question="B 的问题")
        run_a = _seed_run(conn, tenant_id=tenant_a, user_id=user_a, question="A 的运行")
        run_b = _seed_run(conn, tenant_id=tenant_b, user_id=user_b, question="B 的运行")
    finally:
        conn.close()

    client_a = TestClient(app, raise_server_exceptions=False)
    client_b = TestClient(app, raise_server_exceptions=False)
    _login(client_a, ALICE)
    _login(client_b, BOB)

    return TwoTeams(
        app=app,
        deps=deps,
        settings=settings,
        client_a=client_a,
        client_b=client_b,
        tenant_a=tenant_a,
        user_a=user_a,
        tenant_b=tenant_b,
        user_b=user_b,
        member_b=member_b,
        doc_a="doc-a",
        doc_b="doc-b",
        conv_a=conv_a,
        conv_b=conv_b,
        run_a=run_a,
        run_b=run_b,
    )


def without_request_id(response: Any) -> dict[str, Any]:
    """剥掉随机 request_id，只比较「存在性」相关的字段。"""
    body: dict[str, Any] = json.loads(response.content)
    body.pop("request_id", None)
    return body


class FakeChat:
    """不联网的 chat：只保证「有模型可解析」且能产出一条带角标的回答。"""

    def __init__(self, answer: str = "通讯超时，检查波特率与站号[1]。") -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []
        self.last_usage: dict[str, int | None] = {
            "prompt_tokens": 900,
            "completion_tokens": 30,
        }

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.answer
