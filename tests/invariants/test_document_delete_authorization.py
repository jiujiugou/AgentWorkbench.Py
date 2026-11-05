"""删除文档的权限边界（ADR 0008 / REQ-10）。

D9 的红队 B 组实测：最低权限成员能删掉**管理员上传**的文档，一条 `DELETE`
就把团队知识库清空，没有二次确认、没有软删除、也没有任何入口能看出是谁删的。
根因不是权限写错，而是**两处说法并存**——REQ-10 的 actor 表写「管理员」，
实现只判「是不是这个团队的」。

三条断言，各自对应一种失效方式：

1. **成员被拒，且行还在。** 只断言 403 的版本，在一个「先删后判」的实现上
   依然绿——那正是 `members.py` 里花整段注释解释的顺序问题。
2. **成员拿别人的 id 与随机 id 得到同一个响应。** 403 与 404 的差别就是一台
   「这个文档在不在」的探针，这一步不需要任何权限就能试。
3. **管理员照常能删。** 只测拒绝的版本，在「谁都不能删」的实现上也绿——
   那会把一条权限规则变成一个功能缺失。

改坏这些不变量，用户看到什么：

- 成员能删 → 一个离职账号、一次误点，团队知识库没了，而引用过的历史回答还在；
- 成员被 404/403 区分 → 拿一串 id 就能数出这个团队有多少份资料；
- 管理员也不能删 → 传错的资料永远在库里，检索一直命中一份没人认领的文件。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import insert_document
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentworkbench.api.app import create_app
from agentworkbench.api.deps import Deps
from agentworkbench.config import AuthSettings, Settings
from agentworkbench.identity import service
from agentworkbench.identity.models import Role
from agentworkbench.identity.service import BootstrapResult

# ruff: noqa: S105, S106 —— 口令是测试值，不连任何真实账号。

pytestmark = pytest.mark.invariant

ADMIN_EMAIL = "admin@example.com"
MEMBER_EMAIL = "member@example.com"
PASSWORD = "correct horse battery staple"
NOW = datetime.now(UTC)
DOC_ID = "doc-of-team"
MISSING_ID = "11111111-2222-3333-4444-555555555555"


def open_conn(app: FastAPI) -> sqlite3.Connection:
    deps: Deps = app.state.deps
    return deps.open_conn()


@pytest.fixture(name="app")
def fixture_app(tmp_path: Path) -> FastAPI:
    settings = Settings(
        data_dir=tmp_path,
        secret_key="delete-authz-test-secret",
        auth=AuthSettings(session_invalidate_seconds=0),
    )
    return create_app(settings)


def seed(app: FastAPI) -> BootstrapResult:
    """一个管理员 + 一个成员 + 一份「管理员上传」的文档。"""
    conn = open_conn(app)
    try:
        boot = service.bootstrap(
            conn,
            email=ADMIN_EMAIL,
            password=PASSWORD,
            tenant_name="团队甲",
            now=NOW,
            must_change_password=False,
        )
        _, token = service.create_invite(
            conn, tenant_id=boot.tenant_id, role=Role.MEMBER, created_by=boot.user_id, now=NOW
        )
        service.accept_invite(
            conn,
            token=token,
            email=MEMBER_EMAIL,
            password=PASSWORD,
            now=NOW,
            config=service.AuthConfig(),
        )
        insert_document(conn, DOC_ID, tenant_id=boot.tenant_id, file_name="管理员上传的.pdf")
        conn.commit()
        return boot
    finally:
        conn.close()


def client_for(app: FastAPI, email: str) -> TestClient:
    test_client = TestClient(app, raise_server_exceptions=False)
    response = test_client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, f"夹具登录失败：{email} → {response.status_code}"
    return test_client


def count_documents(app: FastAPI, doc_id: str) -> int:
    conn = open_conn(app)
    try:
        row = conn.execute("select count(*) from documents where id = ?", (doc_id,)).fetchone()
        assert row is not None
        return int(row[0])
    finally:
        conn.close()


def stripped(body: dict[str, object]) -> dict[str, object]:
    """`request_id` 每次不同，是唯一允许的差别（`I-V2` 的同款口径）。"""
    return {key: value for key, value in body.items() if key != "request_id"}


def test_a_member_cannot_delete_a_document(app: FastAPI) -> None:
    seed(app)
    with client_for(app, MEMBER_EMAIL) as member:
        response = member.delete(f"/api/documents/{DOC_ID}")

    assert response.status_code == 403, "成员删掉了文档——REQ-10 的 actor 是管理员"
    assert response.json()["code"] == "forbidden"
    assert count_documents(app, DOC_ID) == 1, "403 之后行已经没了：判定写在了删除之后"


def test_a_member_gets_the_same_answer_for_a_missing_id(app: FastAPI) -> None:
    """成员问「这个 id 在不在」，两个回答必须逐字段相同。

    只断言「都是 403」不够：`message` 里带一句「文档不存在」也能过——
    而那句话就是探针本身。
    """
    seed(app)
    with client_for(app, MEMBER_EMAIL) as member:
        existing = member.delete(f"/api/documents/{DOC_ID}")
        missing = member.delete(f"/api/documents/{MISSING_ID}")

    assert existing.status_code == missing.status_code == 403
    assert stripped(existing.json()) == stripped(missing.json())


def test_an_admin_can_still_delete(app: FastAPI) -> None:
    """只测拒绝的版本，在「谁都不能删」的实现上也绿。"""
    seed(app)
    with client_for(app, ADMIN_EMAIL) as admin:
        response = admin.delete(f"/api/documents/{DOC_ID}")

    assert response.status_code == 204
    assert count_documents(app, DOC_ID) == 0, "204 但行还在：删除编排没有真的走完"
