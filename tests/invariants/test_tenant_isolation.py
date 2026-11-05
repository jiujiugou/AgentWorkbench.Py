"""团队隔离的四类路径（`I-V2` / `I-V3`）。

**这是「换血」之后唯一真正新增能力的判据**：阶段 1 只有一个固定归属，
所有隔离测试都只能证明「SQL 里有个 where」；这一组证明的是
**换个人登录，where 里的值真的变了**。

四条路径各一组，因为它们的分工不同：

| 路径 | 落点 | 漏了会怎样 |
|---|---|---|
| 文档列表 / 按 id 取 | `documents.tenant_id` | 一个团队的资料出现在另一个团队里 |
| 检索 | `search_fts` 子查询 + `_meta_of` join | 问答里查不到、调试接口搜得到 |
| 会话列表 / 按 id 取 | `conversations` 的 owner 条件 | 别人的对话可读可删 |
| 轨迹 | 与 run 同一套归属 | 轨迹面板成为绕过文档权限的出口 |

**「取不到」的判据是逐字段相同**，不是「返回 404」：两者可区分的话，
拿一串 id 就能试出「这个 id 存在」——而试这个不需要任何权限。

改坏这些不变量，**用户**会看到什么：

- 归属参数有个默认值（`tenant_id=None` 表示不过滤）→ 某个新接口漏传，
  它的响应里就多出别人的资料，而调用点看起来完全正常；
- 检索只在路由层过滤、`retrieve` 里的 `_ = scope` 还在 → 问答里查不到，
  但调试接口 `GET /api/knowledge/search` 搜得到，任何人直接 curl 就绕开了；
- 文档 404 回显 id → 「别人的 id」与「随机 id」两个响应不再相同，
  任何人都能用一串 id 试出哪篇文档存在；
- 文档删除先删后判 → 越权那条返回 404 但行已经没了，而 404 让调用方
  以为什么都没发生（`I-V3`）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from conftest import insert_document
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentworkbench.api.app import create_app
from agentworkbench.api.deps import Deps
from agentworkbench.chat import conversation as chat_turn
from agentworkbench.config import AuthSettings, Settings
from agentworkbench.core.documents import ChunkDraft
from agentworkbench.core.identity import Owner
from agentworkbench.identity import credentials, store
from agentworkbench.identity.models import Role
from agentworkbench.llm.embeddings import HashEmbedder
from agentworkbench.rag.indexer import write_index
from agentworkbench.rag.retriever import retrieve
from agentworkbench.trace.recorder import Recorder

pytestmark = pytest.mark.invariant

# ruff: noqa: S105, S106 —— 本文件的口令全是测试值。

PASSWORD = "correct horse battery staple"
TEAM_A_SECRET = "液压站 E2 报警的处理步骤：先查波特率，再查站号。"
TEAM_B_SECRET = "竞争对手的报价单编号 QX-2026-004，单价与折扣表在附件里。"
# 只在团队 B 文档里出现的词。团队 A 搜它必须 0 条——不是「不显示」，
# 是**服务端没有返回它的任何片段**。
TEAM_B_ONLY_WORD = "报价单编号"

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    settings = Settings(
        data_dir=tmp_path,
        secret_key="isolation-test-secret",
        auth=AuthSettings(session_invalidate_seconds=0),
    )
    return create_app(settings)


def open_conn(app: FastAPI) -> sqlite3.Connection:
    return cast(Deps, app.state.deps).open_conn()


def new_client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def make_team(app: FastAPI, *, name: str, email: str) -> tuple[str, str]:
    """建一个团队 + 一个管理员，返回 `(tenant_id, user_id)`。

    **不走引导**：引导只允许在空库上跑一次。第二个团队用仓储直接建，
    这也是真实部署里的形态——第一个团队由引导产生，之后靠邀请。
    """
    conn = open_conn(app)
    try:
        now = datetime.now(UTC)
        tenant_id = store.insert_tenant(conn, name=name, now=now)
        user_id = store.insert_user(
            conn,
            tenant_id=tenant_id,
            email=email,
            password_hash=credentials.hash_password(PASSWORD),
            role=Role.ADMIN,
            now=now,
        )
        conn.commit()
    finally:
        conn.close()
    return tenant_id, user_id


def seed_document(app: FastAPI, *, tenant_id: str, doc_id: str, text: str) -> None:
    """造一份「已解析、已索引」的文档。索引内容就是那条团队机密。"""
    conn = open_conn(app)
    try:
        insert_document(conn, doc_id, tenant_id=tenant_id, file_name=f"{doc_id}.pdf")
        embedder = HashEmbedder()
        write_index(
            conn,
            document_id=doc_id,
            drafts=[ChunkDraft(seq=0, page_from=1, page_to=1, content=text)],
            embeddings=embedder([text]),
            now=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        )
        conn.commit()
    finally:
        conn.close()


def seed_conversation(app: FastAPI, *, tenant_id: str, user_id: str, question: str) -> str:
    conn = open_conn(app)
    try:
        started = chat_turn.start_turn(
            conn,
            Owner(user_id=user_id, tenant_id=tenant_id),
            question=question,
            now=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        )
    finally:
        conn.close()
    return started.conversation_id


def login(client: TestClient, email: str) -> None:
    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


def without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key != "request_id"}


@pytest.fixture
def two_teams(app: FastAPI) -> Iterator[tuple[str, str, str, str]]:
    """团队 A（alice）与团队 B（bob），各自一份文档 + 一条会话。

    返回 `(a_tenant, a_user, b_tenant, b_user)`。
    """
    from agentworkbench.identity import service as identity_service

    conn = open_conn(app)
    try:
        boot = identity_service.bootstrap(
            conn,
            email=ALICE,
            password=PASSWORD,
            tenant_name="团队甲",
            now=datetime.now(UTC),
            must_change_password=False,
        )
    finally:
        conn.close()
    b_tenant, b_user = make_team(app, name="团队乙", email=BOB)

    seed_document(app, tenant_id=boot.tenant_id, doc_id="doc-a", text=TEAM_A_SECRET)
    seed_document(app, tenant_id=b_tenant, doc_id="doc-b", text=TEAM_B_SECRET)
    seed_conversation(app, tenant_id=boot.tenant_id, user_id=boot.user_id, question="A 的问题")
    seed_conversation(app, tenant_id=b_tenant, user_id=b_user, question="B 的问题")

    yield boot.tenant_id, boot.user_id, b_tenant, b_user


# ── 文档 ──


def test_document_list_shows_only_my_team(
    app: FastAPI, two_teams: tuple[str, str, str, str]
) -> None:
    alice = new_client(app)
    login(alice, ALICE)

    ids = {row["id"] for row in alice.get("/api/documents").json()}

    assert ids == {"doc-a"}


def test_another_teams_document_is_byte_identical_to_a_random_id(
    app: FastAPI, two_teams: tuple[str, str, str, str]
) -> None:
    """`I-V2` 的判据原文：拿真实 id 与随机 UUID，剥掉 request_id 后逐字段相同。

    只断言「都是 404」的版本，在消息里回显 id 时依然绿——而回显 id 的那份
    响应比随机 id 的那份多了一句「这个 id 存在」。
    """
    alice = new_client(app)
    login(alice, ALICE)
    random_id = f"doc-{uuid4().hex[:12]}"

    foreign = alice.get("/api/documents/doc-b")
    missing = alice.get(f"/api/documents/{random_id}")

    assert foreign.status_code == missing.status_code == 404
    assert without_request_id(foreign.json()) == without_request_id(missing.json())

    # 页与块是同一条判定的另外两个出口，一起验：漏一个就是一个读取口
    assert alice.get("/api/documents/doc-b/pages").status_code == 404
    assert alice.get("/api/documents/doc-b/chunks").status_code == 404


def test_deleting_another_teams_document_leaves_it_intact(
    app: FastAPI, two_teams: tuple[str, str, str, str]
) -> None:
    """`I-V3` 的文档那一路：越权删除 → 404，且**行还在、索引还在**。"""
    alice = new_client(app)
    login(alice, ALICE)

    response = alice.delete("/api/documents/doc-b")

    assert response.status_code == 404
    bob = new_client(app)
    login(bob, BOB)
    assert {row["id"] for row in bob.get("/api/documents").json()} == {"doc-b"}


# ── 检索 ──


def test_search_returns_no_fragment_from_another_team(
    app: FastAPI, two_teams: tuple[str, str, str, str]
) -> None:
    """`REQ-AUTH-04` 的落点是查询条件，不是提示词。

    这条断言的是**响应里没有那段文本**，不是「界面不显示它」——
    后者在接口层看不出来，而任何人都能直接 curl 这个端点。
    """
    alice = new_client(app)
    login(alice, ALICE)

    hits = alice.get("/api/knowledge/search", params={"q": TEAM_B_ONLY_WORD}).json()

    assert hits == []
    assert TEAM_B_SECRET not in str(hits)


def test_search_still_finds_my_own_document(
    app: FastAPI, two_teams: tuple[str, str, str, str]
) -> None:
    """正控。只验「搜不到别人的」会被「什么都搜不到」通过——那是另一种坏。"""
    alice = new_client(app)
    login(alice, ALICE)

    hits = alice.get("/api/knowledge/search", params={"q": "波特率"}).json()

    assert hits, "本团队的文档必须搜得到"
    assert all(hit["documentId"] == "doc-a" for hit in hits)


def test_single_character_fallback_is_scoped_to_my_team(
    app: FastAPI, two_teams: tuple[str, str, str, str]
) -> None:
    """单字查询走 LIKE 兜底路，那条路的团队条件必须和主路一样硬。

    **D4 变异测试发现的缺口**（2026-11-04）：`_match_rows` 与 `_like_rows`
    各有一份团队子查询，原来的隔离测试只用了多字词（走 MATCH 主路），
    把 `_like_rows` 的条件删掉时全部测试仍然绿。

    用户能看到的后果：搜一个「竞」字，响应里就出现竞争对手文档的片段——
    而中文单字查询是正常用法（`泵`、`站`、`阀`），不是构造出来的攻击输入。
    """
    alice = new_client(app)
    login(alice, ALICE)

    hits = alice.get("/api/knowledge/search", params={"q": "竞"}).json()

    assert hits == [], "单字兜底路漏了团队条件：别的团队的片段进了响应"
    assert TEAM_B_SECRET not in str(hits)

    # 正控：过滤没有把本团队的单字查询一起挡掉
    own = alice.get("/api/knowledge/search", params={"q": "液"}).json()
    assert own, "本团队的文档必须搜得到（LIKE 路也是）"
    assert all(hit["documentId"] == "doc-a" for hit in own)


def test_hybrid_retrieval_never_surfaces_another_teams_chunks(
    app: FastAPI, two_teams: tuple[str, str, str, str]
) -> None:
    """直接调 `retrieve` 而不是走 HTTP：**向量路只有它覆盖得到**。

    `GET /api/knowledge/search` 走的是 `search_fts`（全文路），而问答走的是
    混合检索——向量路的 chunk_id 只有在 `_meta_of` 回表时才知道属于谁。
    只验 HTTP 那一条的实现，`_meta_of` 里的 join 条件可以整个删掉而测试全绿，
    然后答案里就会出现别人文档的片段（`I-V2` 的「不只是不显示」）。
    """
    a_tenant, _a_user, _b_tenant, _b_user = two_teams
    conn = open_conn(app)
    try:
        hits = retrieve(
            conn,
            TEAM_B_ONLY_WORD,
            embedder=HashEmbedder(),
            tenant_id=a_tenant,
            top_k=5,
        )
    finally:
        conn.close()

    assert hits, "本团队有块，向量路必定给出最近邻——空列表说明过滤把自家人也挡了"
    # 断言的是「一条都不来自别的团队」，不是「返回空列表」：向量路对任何查询
    # 都会给最近邻（HashEmbedder 尤其如此），把「空」当判据等于要求检索
    # 对无关问题也必须拒答——那是判定层的活（F3），不是归属的活。
    assert all(hit.document_id == "doc-a" for hit in hits)
    assert not any(TEAM_B_SECRET in hit.content for hit in hits), "别人的片段出现在证据里"


def test_hybrid_retrieval_still_finds_my_own_chunks(
    app: FastAPI, two_teams: tuple[str, str, str, str]
) -> None:
    """正控：过滤没有把本团队的内容一起挡掉。"""
    a_tenant, _a_user, _b_tenant, _b_user = two_teams
    conn = open_conn(app)
    try:
        hits = retrieve(conn, "波特率", embedder=HashEmbedder(), tenant_id=a_tenant, top_k=5)
    finally:
        conn.close()

    assert hits
    assert all(hit.document_id == "doc-a" for hit in hits)


def test_a_document_without_a_tenant_belongs_to_nobody(
    app: FastAPI, two_teams: tuple[str, str, str, str]
) -> None:
    """`AUTH-06` / `I-V5`：`tenant_id` 为空串的行，**对所有人 404，包括管理员**。

    阶段 1 的存量数据长这样（那时没有归属）。取舍写在 F1 §三：「宁可数据看不见
    （一查就发现），也不要所有人可见（发现不了）」。这条测试就是那个取舍的
    可执行版本——它在存量绑定之前会一直绿，绑定之后依然绿（那时它们有主了）。
    """
    conn = open_conn(app)
    try:
        conn.execute(
            "insert into documents (id, tenant_id, file_name, stored_path, sha256,"
            " size_bytes, status, page_count, created_at, updated_at)"
            " values ('doc-orphan', '', 'x.pdf', 'p', 'h', 1, 'parsed', 1,"
            " '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    alice = new_client(app)
    login(alice, ALICE)

    assert alice.get("/api/documents/doc-orphan").status_code == 404
    assert "doc-orphan" not in {row["id"] for row in alice.get("/api/documents").json()}


# ── 会话与轨迹 ──


def test_the_same_user_in_another_tenant_owns_nothing_here(
    app: FastAPI, two_teams: tuple[str, str, str, str]
) -> None:
    """**团队才是边界，不是用户 id**（`I-V3`）。

    造一条 `user_id` 与 alice 相同、`tenant_id` 是团队乙的会话。这种行真的会出现：
    格 7 的存量绑定会把旧数据整批挪进一个团队，同一个人的痕迹可能落在两处。

    这条测试存在的理由很具体：没有它，「删除要带团队条件」那句话可以整段删掉而
    测试**不会红**——因为两个用户的 `user_id` 本来就不同，只用 `user_id` 过滤
    也删不掉别人的会话。也就是说，那条 `and tenant_id = ?` 之前是没人验过的。
    """
    _a_tenant, a_user, b_tenant, _b_user = two_teams
    ghost = seed_conversation(app, tenant_id=b_tenant, user_id=a_user, question="另一份自己")

    alice = new_client(app)
    login(alice, ALICE)

    assert alice.get(f"/api/conversations/{ghost}/messages").status_code == 404
    assert alice.delete(f"/api/conversations/{ghost}").status_code == 404

    conn = open_conn(app)
    try:
        row = conn.execute("select tenant_id from conversations where id = ?", (ghost,)).fetchone()
    finally:
        conn.close()
    assert row is not None and row["tenant_id"] == b_tenant, "另一层归属下的行被删掉了"


def test_conversation_list_and_detail_are_scoped(
    app: FastAPI, two_teams: tuple[str, str, str, str]
) -> None:
    a_tenant, _a_user, b_tenant, _b_user = two_teams
    conn = open_conn(app)
    try:
        mine = {
            str(row["id"])
            for row in conn.execute(
                "select id from conversations where tenant_id = ?", (a_tenant,)
            ).fetchall()
        }
        row = conn.execute(
            "select id from conversations where tenant_id = ?", (b_tenant,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and mine

    alice = new_client(app)
    login(alice, ALICE)

    assert {item["id"] for item in alice.get("/api/conversations").json()} == mine

    foreign = alice.get(f"/api/conversations/{row['id']}/messages")
    missing = alice.get(f"/api/conversations/conv-{uuid4().hex[:12]}/messages")
    assert foreign.status_code == missing.status_code == 404
    assert without_request_id(foreign.json()) == without_request_id(missing.json())


def test_trace_of_another_teams_run_is_not_reachable(
    app: FastAPI, two_teams: tuple[str, str, str, str]
) -> None:
    """轨迹独立成一条：它与会话用的是同一套归属，但**读侧是另一个函数**
    （`trace.read_run`），漏改一处就漏一个出口。

    用的是 B 的**真实 run id**，不是随机 id——随机 id 那条只能证明格式对，
    证明不了「这个 id 真的存在但拿不到」。
    """
    _a_tenant, _a_user, b_tenant, b_user = two_teams
    conn = open_conn(app)
    try:
        b_run = Recorder(conn, user_id=b_user, tenant_id=b_tenant).start(
            question="B 的问题", prompt_version="v1"
        )
    finally:
        conn.close()

    alice = new_client(app)
    login(alice, ALICE)

    foreign = alice.get(f"/api/runs/{b_run}/trace")
    missing = alice.get(f"/api/runs/run-{uuid4().hex[:12]}/trace")

    assert foreign.status_code == missing.status_code == 404
    assert without_request_id(foreign.json()) == without_request_id(missing.json())
