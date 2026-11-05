"""全局闸门、拒绝记录、会话窗口（`I-V1` / `I-V4` / `I-V10`）。

这一格补的是**「谁都得过闸」的性质**，不是某一处判定对不对。两者的差别在失效方式上：

- 判定写错 → 某条路径行为不对，一条测试能钉住它；
- 闸门不存在 → 每条路径各自记得调判定，**新加的那一条忘了**，而忘了不会报错。
  它只是安静地对外开着，直到有人在它上面传了一份别人的文件。

所以这里的第一条测试是**遍历路由表**而不是挑几条写：遍历的版本在新增第 21 条路由
的那天就会红（这条路由要么在白名单里，要么会被 401）。第二条测试更进一步——
在 `create_app` 之后现加一条路由，断言它同样被挡。

改坏这些不变量，用户看到什么：

- 闸门只挂在「记得调它的路由」上 → `settings` 六条配置路由无票可读，改一下
  `base_url` 就能把全平台的问答导向别人的服务器；
- 拒绝记录写在响应之后 → 压测不变慢，记录随机丢，事后查不到任何证据；
- 拒绝记录写失败时仍然回 404 → 一句「不存在」，而系统手上没有任何证据；
- `session_invalidate_seconds` 写成模块常量 → 配 0 与配 600 行为一致，
  而「配了不生效的旋钮」正是 §1.8 列在拒绝项里的东西。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import insert_document
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentworkbench.api import refusals
from agentworkbench.api.app import create_app
from agentworkbench.api.deps import Deps
from agentworkbench.api.identity_context import COOKIE_NAME, PUBLIC_PATHS
from agentworkbench.api.routes.declarations import FRAMEWORK_PATHS, api_routes
from agentworkbench.config import AuthSettings, Settings
from agentworkbench.identity import credentials, service, store
from agentworkbench.identity.models import Role
from agentworkbench.identity.sessions import MAX_ENTRIES, SessionCache

# ruff: noqa: S105, S106 —— 本文件的口令全是测试值，不连任何真实账号。

pytestmark = pytest.mark.invariant

ADMIN_EMAIL = "admin@example.com"
MEMBER_EMAIL = "member@example.com"
OUTSIDER_EMAIL = "outsider@example.com"
PASSWORD = "correct horse battery staple"
NOW = datetime.now(UTC)


# ── 装配 ──


def build_app(tmp_path: Path, *, session_window: int = 0) -> FastAPI:
    """`session_window=0` 是默认：不缓存，吊销的下一个请求即 401。

    要验窗口本身的用例显式传别的值——「默认值悄悄当成了规格」是这一格最容易
    犯的错：判据要的是「改配置行为真的变」，而两边都用默认值断言的版本
    区分不出「读配置」和「读常量」。
    """
    settings = Settings(
        data_dir=tmp_path,
        secret_key="gate-test-secret",
        auth=AuthSettings(session_invalidate_seconds=session_window),
    )
    return create_app(settings)


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return build_app(tmp_path)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def open_conn(app: FastAPI) -> sqlite3.Connection:
    deps: Deps = app.state.deps
    return deps.open_conn()


def seed_admin(app: FastAPI, *, must_change_password: bool = False) -> service.BootstrapResult:
    conn = open_conn(app)
    try:
        return service.bootstrap(
            conn,
            email=ADMIN_EMAIL,
            password=PASSWORD,
            tenant_name="团队甲",
            now=NOW,
            must_change_password=must_change_password,
        )
    finally:
        conn.close()


def seed_member(app: FastAPI, *, tenant_id: str, admin_id: str) -> str:
    conn = open_conn(app)
    try:
        _, token = service.create_invite(
            conn, tenant_id=tenant_id, role=Role.MEMBER, created_by=admin_id, now=NOW
        )
        accepted = service.accept_invite(
            conn,
            token=token,
            email=MEMBER_EMAIL,
            password=PASSWORD,
            now=NOW,
            config=service.AuthConfig(),
        )
    finally:
        conn.close()
    return accepted.account.user_id


def login(client: TestClient, *, email: str = ADMIN_EMAIL, password: str = PASSWORD) -> Any:
    return client.post("/api/auth/login", json={"email": email, "password": password})


def concrete(path: str) -> str:
    """把 `{doc_id}` 换成一段真实形态的 id。**不是空串**：空串会被路由的
    类型转换或校验挡下（400），而那样一来「401」这条断言就没验到闸门。"""
    return re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000000", path)


def events(app: FastAPI) -> list[sqlite3.Row]:
    """直接读表，不走 `store.list_events`：那条读侧按团队过滤，而**未登录的拒绝
    记录没有团队**（`tenant_id` 为 NULL）——用带团队条件的读侧去验它，
    会得到「一条都没有」这个错误结论。"""
    conn = open_conn(app)
    try:
        return conn.execute("select * from identity_events order by id").fetchall()
    finally:
        conn.close()


# ── 闸门（I-V1） ──


def test_every_business_route_is_behind_the_gate(app: FastAPI, client: TestClient) -> None:
    """无票据访问每一条非白名单路由都是 401。

    **遍历而不是挑几条**：挑着写的版本在新增路由那天仍然全绿。
    两条自检（路由数、实际检查数）防的是另一种沉默：遍历到 0 条时，
    这条不变量恒真且全绿——`declarations.api_routes` 的 docstring 记过同款陷阱。
    """
    routes = api_routes(app)
    assert len(routes) >= 30, f"只遍历到 {len(routes)} 条路由，扫描本身失效了"

    checked = 0
    for method, path in routes:
        if path in PUBLIC_PATHS or path in FRAMEWORK_PATHS:
            continue
        response = client.request(method, concrete(path))
        assert response.status_code == 401, (method, path, response.status_code, response.text)
        checked += 1

    assert checked >= 20, f"只检查了 {checked} 条，白名单把业务路由吃掉了"


def test_a_route_added_after_create_app_is_still_gated(app: FastAPI) -> None:
    """闸门挂的是「app 上的依赖」，不是「20 条路由各自写一遍」。

    这条测试是这一格的**定义**：如果哪天有人把闸门改成在每条路由上写
    `dependencies=[Depends(...)]`，遍历那条测试仍然全绿（20 条都写了），
    而这条会红——第 21 条路由不受保护。
    """

    @app.get("/api/late-arrival")
    def late_arrival() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/late-arrival").status_code == 401


def test_must_change_blocks_a_route_added_after_create_app(
    app: FastAPI, client: TestClient
) -> None:
    """首登闸门也在 app 级依赖里：**第 22 条路由**同样挡得住。

    只测已存在的 `/api/documents` 不够——把闸门写成“每条路由自己判”的版本，
    现有路由全绿，新路由静默开放，而首登口令正是最不该漏的那一条。
    """
    seed_admin(app, must_change_password=True)
    assert login(client).status_code == 200

    @app.get("/api/late-arrival-must-change")
    def late_arrival() -> dict[str, bool]:
        return {"ok": True}

    response = client.get("/api/late-arrival-must-change")

    assert response.status_code == 403
    assert response.json()["code"] == "password_change_required"


def test_must_change_403_is_recorded_with_its_own_reason(app: FastAPI, client: TestClient) -> None:
    """被首登闸门挡下的请求也要留记录，原因码不能兜底成“角色不足”。"""
    seed_admin(app, must_change_password=True)
    assert login(client).status_code == 200

    response = client.get("/api/documents")
    assert response.status_code == 403
    request_id = response.headers["x-request-id"]

    rows = [row for row in events(app) if row["request_id"] == request_id]
    assert len(rows) == 1
    assert rows[0]["reason_code"] == "password_change_required"


EXPECTED_PUBLIC_PATHS = frozenset(
    {
        "/api/health",
        "/api/auth/login",
        "/api/auth/invites/{token}",
    }
)


def test_the_whitelist_is_reviewed_not_silently_grown() -> None:
    """白名单是**逐条审过的**，不是「谁方便就加一条」。

    上面那条遍历测试的跳过集合**就是这张白名单**——于是它有一个自己验不出自己的洞：
    把一条业务路由加进白名单之后，遍历会自动跳过它，那条测试仍然全绿。
    这条测试堵的就是这个洞：白名单一变，它立刻红，改的人必须来这里
    把新的一条连同理由一起写下来。

    这不是形式主义。白名单多一条，`I-V1` 的覆盖面就少一块，而这个动作在代码里
    看起来只是加了一个字符串。
    """
    assert PUBLIC_PATHS == EXPECTED_PUBLIC_PATHS


def test_the_whitelist_is_reachable_without_a_session(client: TestClient) -> None:
    """白名单里的三条不返回 401。

    判据原文是「不返回 401」，但这里要**区分是谁的 401**：登录失败也是 401
    （`invalid_credentials`），只看状态码的版本会在「登录入口被闸门挡住」
    和「口令确实错了」之间分不出来。所以断言的是错误码。
    """
    assert client.get("/api/health").status_code == 200

    failed_login = client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "whatever"}
    )
    assert failed_login.status_code == 401
    assert failed_login.json()["code"] == "invalid_credentials", "登录入口被闸门挡住了"

    invite = client.post(
        "/api/auth/invites/never-issued",
        json={"email": "someone@example.com", "password": PASSWORD},
    )
    assert invite.status_code == 404, "接受邀请被闸门挡住了——那是新用户第一次进来的地方"


def test_the_gate_runs_before_the_body_is_validated(client: TestClient) -> None:
    """无票据 + 空 body 的 POST 返回 401，不是 400。

    顺序不是细节：校验失败会告诉未登录的人「这个端点存在，而且它要一个字段」，
    而 401 什么都不说。实现上它靠的是「依赖先于请求体校验」——那是可以被一次
    改造不经意改掉的（比如把校验挪进路由函数）。

    **只断言空 body**：body 是**非法 JSON** 时，FastAPI 在解析阶段就失败，
    早于任何依赖。那是框架行为，不是本项目的选择；把它写成断言等于把一处分不清
    「端点存在」的响应固化成规格。（D6 之后这类请求回 400 五字段信封，状态码从
    422 改成了 400，但「校验先于闸门」这件事没变。）
    """
    assert client.post("/api/documents").status_code == 401
    assert client.patch("/api/members/someone").status_code == 401


# ── 拒绝记录（I-V4） ──


def test_a_404_leaves_a_record_findable_by_request_id(app: FastAPI, client: TestClient) -> None:
    """一次越权 404 之后，按响应的 `X-Request-Id` 查到一条记录，四个字段齐全。"""
    seed_admin(app)
    login(client)

    missing = "11111111-2222-3333-4444-555555555555"
    response = client.get(f"/api/documents/{missing}")
    assert response.status_code == 404

    request_id = response.headers["x-request-id"]
    rows = [row for row in events(app) if row["request_id"] == request_id]
    assert len(rows) == 1, "按 request_id 查不到（或查到多条）——记录与这一次请求对不上"

    row = rows[0]
    assert row["action"] == "GET /api/documents/{doc_id}", "动作记了真实 URL 而不是路由模板"
    assert row["resource_kind"] == "document"
    assert row["resource_id"] == missing
    assert row["reason_code"] == "resource_missing"
    assert row["actor_user_id"] is not None, "已登录的拒绝必须记下「谁被拒了」"
    assert row["tenant_id"] is not None
    assert row["occurred_at"]


def test_a_successful_request_writes_nothing(app: FastAPI, client: TestClient) -> None:
    """正常的 200 不产生任何记录。

    这条是整套记录能不能用的前提：一旦成功也写，那张表的语义就从「被拒的人」
    变成「所有人」，而「他是没权限，还是系统坏了」这个它唯一要回答的问题
    就再也答不出来了——三种情况都在表里，等于都没有证据。
    """
    seed_admin(app)
    login(client)

    assert client.get("/api/documents").status_code == 200
    assert client.get("/api/auth/me").status_code == 200
    assert events(app) == []


def test_a_401_is_recorded_without_an_actor(app: FastAPI, client: TestClient) -> None:
    """未登录的拒绝也要记——**没有「谁」也要有这一条**。

    不记的话，「没登录」和「系统坏了」在记录里分不开，而这两种的排查方向相反。
    """
    response = client.get("/api/documents")
    assert response.status_code == 401

    rows = [row for row in events(app) if row["request_id"] == response.headers["x-request-id"]]
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] is None
    assert rows[0]["tenant_id"] is None
    assert rows[0]["reason_code"] == "unauthenticated"


def test_a_disabled_account_loses_every_business_route(app: FastAPI, client: TestClient) -> None:
    """`I-V6`：「任一业务接口」是**逐条走一遍**，不是挑一条代表。

    停用之后旧 cookie 在 401 上停住的理由是票据被吊销（第 ① 步），不是账户状态
    （第 ② 步）——两条路径对外同为 401，所以这里断言状态码而不是原因码；
    原因码的差别在 `test_a_401_records_the_exact_reason_not_a_generic_one` 里验。

    恢复那半条同样重要：只验「停用生效」的版本，可以用「把 status 写成 disabled
    且永不改回」通过，而用户看到的是一场再也进不去的封号。
    """
    boot = seed_admin(app)
    member_id = seed_member(app, tenant_id=boot.tenant_id, admin_id=boot.user_id)

    with TestClient(app, raise_server_exceptions=False) as member:
        login(member, email=MEMBER_EMAIL)
        assert member.get("/api/documents").status_code == 200

        login(client)
        assert client.post(f"/api/members/{member_id}/disable").status_code == 204

        for path in ("/api/documents", "/api/conversations", "/api/auth/me"):
            assert member.get(path).status_code == 401, path

        assert client.post(f"/api/members/{member_id}/enable").status_code == 204
        assert member.get("/api/auth/me").status_code == 401, "吊销是永久的：恢复不等于复活旧票"

    # 恢复之后：旧 cookie 不复活（吊销是永久的），重新登录可以
    with TestClient(app, raise_server_exceptions=False) as fresh:
        assert login(fresh, email=MEMBER_EMAIL).status_code == 200
        assert fresh.get("/api/auth/me").status_code == 200


def test_identity_is_judged_before_ownership(app: FastAPI, client: TestClient) -> None:
    """`I-V16` 的端到端一半：同一个**存在的** id，三种身份给三种出口。

    未登录 → 401 而不是 404：反过来的话，「有没有登录」本身成了可推断的信息——
    随便试一串 id，404 说明「这个 id 不存在」，401 说明「存在，但你得先登录」。
    已登录看别人的 → 404 而不是 403：403 等于确认「它存在，只是不归你」。

    这三条只有在闸门挂上之后才验得全：在此之前「未登录」那一条由各路由
    自己的 `owner_of` 兜着，而它们各自写的 401 文案并不一致。
    """
    boot = seed_admin(app)
    conn = open_conn(app)
    try:
        with conn:
            insert_document(conn, "doc-of-team-a", tenant_id=boot.tenant_id)
            other_tenant = store.insert_tenant(conn, name="团队乙", now=NOW)
            store.insert_user(
                conn,
                tenant_id=other_tenant,
                email=OUTSIDER_EMAIL,
                password_hash=credentials.hash_password(PASSWORD),
                role=Role.ADMIN,
                now=NOW,
            )
    finally:
        conn.close()

    anonymous = TestClient(app, raise_server_exceptions=False)
    assert anonymous.get("/api/documents/doc-of-team-a").status_code == 401

    login(client)
    assert client.get("/api/documents/doc-of-team-a").status_code == 200

    with TestClient(app, raise_server_exceptions=False) as outsider:
        login(outsider, email=OUTSIDER_EMAIL)
        assert outsider.get("/api/documents/doc-of-team-a").status_code == 404


def test_a_lockout_elsewhere_does_not_kick_an_existing_session(
    app: FastAPI, client: TestClient
) -> None:
    """`I-V8` 的端到端：别人拿 A 的邮箱连错口令，A 已登录的会话照常可用。

    锁定的判定只在**登录路径**上（`login_block_reason`），不在受保护请求上。
    把两者合成一处看起来更「一致」，代价是一条可预测的 DoS：
    拿别人邮箱乱试就能把他踢下线，成本比猜中密码低得多。
    """
    boot = seed_admin(app)
    seed_member(app, tenant_id=boot.tenant_id, admin_id=boot.user_id)
    limit = app.state.deps.settings.auth.login_max_failures

    with TestClient(app, raise_server_exceptions=False) as member:
        login(member, email=MEMBER_EMAIL)
        assert member.get("/api/auth/me").status_code == 200

        for _ in range(limit):
            assert login(client, email=MEMBER_EMAIL, password="wrong").status_code == 401

        # 他自己这次也登不进来了（锁定确实生效），但手里那张票还在
        assert login(member, email=MEMBER_EMAIL).status_code == 401
        assert member.get("/api/auth/me").status_code == 200, "被别人乱试就掉线 = 一条 DoS"


def test_a_403_records_who_was_refused_and_why(app: FastAPI, client: TestClient) -> None:
    """403 记下谁、对什么动作、哪份资源。

    这里**不断言「原因码来自判定层而不是兜底」**：403 只有一种原因
    （角色不够），而兜底给的也是它，两边断言得出同一个值——那样的断言改坏了也不会红。
    能区分这两者的只有 401（见下一条），因为 401 下面压着六种原因。
    """
    boot = seed_admin(app)
    member_id = seed_member(app, tenant_id=boot.tenant_id, admin_id=boot.user_id)

    with TestClient(app, raise_server_exceptions=False) as member:
        login(member, email=MEMBER_EMAIL)
        # 成员能看本团队名单（那是产品决定），但改角色要管理员。
        assert member.get("/api/members").status_code == 200
        response = member.patch(f"/api/members/{member_id}", json={"role": "admin"})

    assert response.status_code == 403
    rows = [row for row in events(app) if row["request_id"] == response.headers["x-request-id"]]
    assert len(rows) == 1
    assert rows[0]["reason_code"] == "role_insufficient"
    assert rows[0]["action"] == "PATCH /api/members/{user_id}"
    assert rows[0]["resource_kind"] == "user"
    assert rows[0]["resource_id"] == member_id
    assert rows[0]["actor_user_id"] is not None


def test_a_401_records_the_exact_reason_not_a_generic_one(app: FastAPI, client: TestClient) -> None:
    """401 的六种原因必须区分开：停用的会话记 `session_expired`，没带票记 `unauthenticated`。

    这是**唯一**能证伪「原因码一路传到了记录里」的断言。判据 `I-V4` 要的字段里
    `reason_code` 最容易做成摆设：兜底映射也能填一个值，而填出来的值对六种
    401 全都一样——运维拿到的还是「401 了」这一句，`1.3` 存在的理由就没了。
    """
    boot = seed_admin(app)
    member_id = seed_member(app, tenant_id=boot.tenant_id, admin_id=boot.user_id)

    login(client)  # 管理员
    with TestClient(app, raise_server_exceptions=False) as member:
        login(member, email=MEMBER_EMAIL)
        assert member.get("/api/auth/me").status_code == 200

        assert client.post(f"/api/members/{member_id}/disable").status_code == 204
        revoked = member.get("/api/auth/me")

    assert revoked.status_code == 401
    # 对照组：同一个 app、同一条路由，只是这一次真的没带票
    client.cookies.clear()
    no_ticket = client.get("/api/auth/me")
    assert no_ticket.status_code == 401
    assert revoked.headers["x-request-id"] != no_ticket.headers["x-request-id"]

    by_id = {row["request_id"]: row for row in events(app)}
    assert by_id[revoked.headers["x-request-id"]]["reason_code"] == "session_expired"
    assert by_id[no_ticket.headers["x-request-id"]]["reason_code"] == "unauthenticated"


def test_a_failed_record_write_becomes_500_not_404(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """记录写不进去 → 500。**不许照常回 404。**

    404 是一句「查过了，没有」。系统在没有任何证据的情况下说这句话，
    事后就再也分不清它是真的查过、还是根本没记下来。
    """

    def boom(conn: sqlite3.Connection, event: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(refusals, "_insert", boom)

    response = client.get("/api/documents/11111111-2222-3333-4444-555555555555")

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"


def test_public_paths_do_not_flood_the_table(app: FastAPI, client: TestClient) -> None:
    """登录失败不记。

    这两条匿名可达：记一次就给了一个**谁都能打满的写入口**，一次脚本就能把表撑爆，
    而真正有用的那几条会淹在里面。登录失败已经有它自己的记账
    （`users.failed_attempts` 与 `lockout_until`）。
    """
    for _ in range(3):
        login(client, email="nobody@example.com")

    assert [row for row in events(app) if row["action"].startswith("POST /api/auth/login")] == []


# ── 会话窗口（I-V10） ──


def _revoke(app: FastAPI, client: TestClient) -> None:
    """直接在库里吊销这张票。**走库不走 HTTP**：登出这条路上有没有清缓存
    是另一件事，而这里要验的是「判定被复用了多久」。"""
    ticket = client.cookies.get(COOKIE_NAME)
    assert ticket is not None
    conn = open_conn(app)
    try:
        with conn:
            conn.execute(
                "update tickets set revoked_at = ? where token_hash = ?",
                (store.stamp(NOW), credentials.hash_ticket(ticket)),
            )
    finally:
        conn.close()


def test_zero_window_makes_revocation_immediate(tmp_path: Path) -> None:
    """配 0 → 吊销后的**下一个请求**即 401。"""
    app = build_app(tmp_path, session_window=0)
    with TestClient(app, raise_server_exceptions=False) as client:
        seed_admin(app)
        login(client)
        assert client.get("/api/auth/me").status_code == 200

        _revoke(app, client)

        assert client.get("/api/auth/me").status_code == 401


def test_a_nonzero_window_really_holds_the_judgment(tmp_path: Path) -> None:
    """配 N → 吊销后的下一个请求**仍然 200**，过了窗口才 401。

    这条反过来断言「不服立断」是刻意的：如果两条都断言「立刻 401」，
    那么把配置读成常量、甚至完全不实现缓存，测试也全绿——判据原文那句
    「同一断言在配置改大后失败，证明读的是配置不是常量」，说的正是这里。
    """
    app = build_app(tmp_path, session_window=60)

    now = [1000.0]
    app.state.deps.session_cache = SessionCache(60, clock=lambda: now[0])

    with TestClient(app, raise_server_exceptions=False) as client:
        seed_admin(app)
        login(client)
        assert client.get("/api/auth/me").status_code == 200  # 暖一次缓存

        _revoke(app, client)

        assert client.get("/api/auth/me").status_code == 200, "窗口内被复用了？说明缓存没生效"

        now[0] += 61

        assert client.get("/api/auth/me").status_code == 401, "过了窗口还在放行——那吊销就永远不生效"


def test_the_window_is_read_from_configuration_not_a_constant(tmp_path: Path) -> None:
    """两个 app 只差一个配置键，行为就不同。"""
    immediate = build_app(tmp_path / "a", session_window=0)
    delayed = build_app(tmp_path / "b", session_window=600)

    assert immediate.state.deps.session_cache.enabled is False
    assert delayed.state.deps.session_cache.enabled is True


# ── SessionCache 自身（纯单测，不建 app） ──


def _principal(epoch: int = 1) -> Any:
    from agentworkbench.core.identity import Owner
    from agentworkbench.identity.models import AccountStatus, Principal

    return Principal(
        owner=Owner(user_id="u1", tenant_id="t1"),
        role=Role.ADMIN,
        status=AccountStatus.ACTIVE,
        credential_epoch=epoch,
    )


def test_a_disabled_cache_remembers_nothing() -> None:
    cache = SessionCache(0)
    cache.put("h", _principal())

    assert cache.enabled is False
    assert cache.get("h") is None


def test_an_expired_entry_is_not_returned_and_is_dropped() -> None:
    now = [0.0]
    cache = SessionCache(10, clock=lambda: now[0])
    cache.put("h", _principal())

    assert cache.get("h") is not None
    now[0] += 11
    assert cache.get("h") is None
    # 顺手删掉了：留着不会出错，但会让上限提前触发
    assert cache._entries == {}


def test_the_cache_is_bounded() -> None:
    """到上限时先清过期，清完还满就整体清空——**不许无界增长**。

    每个登录过的票据都会进来一条，而票据在过期之前不会自己消失；
    不设上限的实现会在跑满一个月之后慢慢吃空内存，而那天没有异常的日志。
    """
    now = [0.0]
    cache = SessionCache(10, clock=lambda: now[0])
    for index in range(MAX_ENTRIES):
        cache.put(f"h{index}", _principal())

    cache.put("overflow", _principal())

    assert len(cache._entries) <= MAX_ENTRIES
    assert cache.get("overflow") is not None


# ── 吊销的即时性（D9 红队抓到的窗口） ──


def test_the_default_configuration_does_not_cache_session_judgments() -> None:
    """默认 `session_invalidate_seconds = 0`。

    这条守的是一个**默认值**，看起来像废话，但 D9 之前它正是 60：本地 SQLite 上一次
    判定是几十微秒，60 秒窗口换来的是「被停用的管理员在一个窗口内还能改别人的角色」——
    窗口是临时的，那次改角色的结果不是。而 794 个测试全绿，因为
    `tests/conftest.py` 把这条旋钮设成了 0：**测试环境从来没有跑过生产默认值**。

    改回 60 会让这条红。真要窗口，改这个断言并在 `identity/sessions.py` 里
    重新说明代价——两件事必须同时发生。

    **断言的是「类上声明的默认值」，不是 `Settings()` 跑出来的值**：
    `tests/conftest.py` 把 `AWB_AUTH__SESSION_INVALIDATE_SECONDS` 固定成 `0`，
    于是 `Settings()` 无论代码默认写成多少都会读到 0。第一版就是这么写的，
    改坏验证时把默认改回 60 它**照样绿**——一个永远不会红的空测试。
    同一个陷阱还解释了为什么 D9 之前 794 个测试全绿却漏掉了这条缺陷：
    测试环境从来没有跑过生产默认值。
    """
    declared = AuthSettings.model_fields["session_invalidate_seconds"].default
    assert declared == 0, f"代码里的默认值被改成了 {declared}"

    # 顺带确认「测试环境确实把旋钮钉成 0」这个前提还成立——它一旦变了，
    # 上面那句的解释就不再成立，而这里会先红。
    assert Settings(data_dir=Path(".")).auth.session_invalidate_seconds == 0


def test_disabling_a_member_kicks_the_session_out_on_the_very_next_request(
    tmp_path: Path,
) -> None:
    """停用之后**下一个请求**就 401，即使缓存窗口是 60 秒。

    改坏这里会怎样：把 `forget_user` 去掉，这条在窗口内会拿到 200——
    而现实中那 60 秒里被停用的管理员还能改别人的角色，改动是永久的。
    红队两个小组各自独立复现过（`reports/acceptance/2026-11-05/d9/redteam-A.md` R-01）。
    """
    app = build_app(tmp_path, session_window=60)
    with (
        TestClient(app, raise_server_exceptions=False) as admin_client,
        TestClient(app, raise_server_exceptions=False) as member_client,
    ):
        seeded = seed_admin(app)
        member_id = seed_member(app, tenant_id=seeded.tenant_id, admin_id=seeded.user_id)
        login(admin_client)
        login(member_client, email=MEMBER_EMAIL)

        # 先请求一次，让这张票进缓存——否则测的是「没缓存」而不是「缓存被清掉」
        assert member_client.get("/api/documents").status_code == 200

        assert admin_client.post(f"/api/members/{member_id}/disable").status_code == 204

        assert member_client.get("/api/documents").status_code == 401


def test_logout_invalidates_the_ticket_even_inside_a_window(tmp_path: Path) -> None:
    """登出后重放同一个 cookie → 401。缓存窗口不影响这条。"""
    app = build_app(tmp_path, session_window=60)
    with TestClient(app, raise_server_exceptions=False) as client:
        seed_admin(app)
        login(client)
        assert client.get("/api/auth/me").status_code == 200  # 预热缓存

        ticket = client.cookies.get(COOKIE_NAME)
        assert ticket, "登录后应该有 awb_session cookie"
        assert client.post("/api/auth/logout").status_code == 204

        replay = client.get("/api/auth/me", headers={"cookie": f"{COOKIE_NAME}={ticket}"})
        assert replay.status_code == 401, "登出后同一张票还能用——缓存把吊销吞掉了"


def test_changing_the_password_kills_the_other_device_immediately(tmp_path: Path) -> None:
    """改密：当前设备保住，别的设备**立刻**失效（`I-V9`），不等窗口。"""
    app = build_app(tmp_path, session_window=60)
    with (
        TestClient(app, raise_server_exceptions=False) as device_one,
        TestClient(app, raise_server_exceptions=False) as device_two,
    ):
        seed_admin(app)
        login(device_one)
        login(device_two)

        assert device_two.get("/api/auth/me").status_code == 200  # 预热缓存

        changed = device_one.post(
            "/api/auth/password",
            json={"oldPassword": PASSWORD, "newPassword": PASSWORD + "-new"},
        )
        assert changed.status_code == 204, changed.text

        assert device_one.get("/api/auth/me").status_code == 200, "当前设备不该被踢"
        assert device_two.get("/api/auth/me").status_code == 401, "另一台设备还在放行"


def test_a_role_change_takes_effect_on_the_next_request(tmp_path: Path) -> None:
    """降职之后下一次请求就按新角色判——缓存里那份 `Principal` 还带着旧角色。"""
    app = build_app(tmp_path, session_window=60)
    with (
        TestClient(app, raise_server_exceptions=False) as owner_client,
        TestClient(app, raise_server_exceptions=False) as peer_client,
    ):
        seeded = seed_admin(app)
        conn = open_conn(app)
        try:
            _, token = service.create_invite(
                conn,
                tenant_id=seeded.tenant_id,
                role=Role.ADMIN,
                created_by=seeded.user_id,
                now=NOW,
            )
            peer = service.accept_invite(
                conn,
                token=token,
                email="peer@example.com",
                password=PASSWORD,
                now=NOW,
                config=service.AuthConfig(),
            )
        finally:
            conn.close()

        login(owner_client)
        login(peer_client, email="peer@example.com")
        # 预热：peer 以管理员身份进缓存。用 `GET /api/members` 是因为它**不判管理员**
        # （成员可见），正好只用来把这张票塞进缓存，不夹带权限判定。
        assert peer_client.get("/api/members").status_code == 200

        demoted = owner_client.patch(
            f"/api/members/{peer.account.user_id}", json={"role": "member"}
        )
        assert demoted.status_code == 200, demoted.text

        # 探针必须是走 `_admin`（读缓存身份）的端点。`POST /api/members/invites`
        # 不用它——那条自己又判了一次库，缓存不缓存都是 403，验不出东西。
        blocked = peer_client.post(f"/api/members/{seeded.user_id}/disable")
        assert blocked.status_code == 403, "降职后仍能执行管理员动作——角色没随吊销生效"


def test_forget_user_only_drops_that_users_entries() -> None:
    """`forget_user` 是定点清除：别人那张票不该被顺手清掉。

    两个不同的 user_id 是这条的全部意义——两边都用 `u1` 的版本无论如何都会「通过」，
    它其实只验了 `get` 返回 None，什么也没证明。
    """
    from agentworkbench.core.identity import Owner
    from agentworkbench.identity.models import AccountStatus, Principal

    cache = SessionCache(60, clock=lambda: 0.0)
    cache.put("mine", _principal())
    cache.put(
        "theirs",
        Principal(
            owner=Owner(user_id="u2", tenant_id="t1"),
            role=Role.ADMIN,
            status=AccountStatus.ACTIVE,
            credential_epoch=1,
        ),
    )

    cache.forget_user("u1")

    assert cache.get("mine") is None
    assert cache.get("theirs") is not None, "清自己的票把别人的也清了——那是整表清空"
