"""身份与成员端点的 HTTP 契约（I-V1 / V6 / V9 / V11 / V13 / V14 / V15 / I-H2）。

这一层测的是**判定层与服务层都已经验过的行为，在真实请求上还成不成立**。
不是重复：中间隔着 cookie 装配、状态码映射、错误信封三处，每一处都能把
一个正确的判定变成错误的响应。

改坏这些不变量，用户看到什么：

- 登录失败按原因给不同文案 → 「您的账号已被停用」这一句就把四个原因全暴露了，
  而它不需要任何权限就能读到（`I-H2`）；
- 令牌写进响应体 → 它会同时出现在前端 store、日志、错误上报里，
  而它本来只有一个存放处；
- 改密后重设 cookie → 掩盖「保留的票没升版本」这个 bug，用户仍然被踢；
- 管理接口先查目标再判角色 → 非管理员拿 id 打接口，404 与 403 的差别
  就是「这个人在不在」的探针；
- 停用先写后判 → 返回 403 但那一行已经变了，而 403 让调用方以为什么都没发生。
"""

from __future__ import annotations

import sqlite3
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentworkbench.api.app import create_app
from agentworkbench.api.deps import Deps
from agentworkbench.api.routes.declarations import absent_paths
from agentworkbench.config import AuthSettings, Settings
from agentworkbench.identity import credentials, service, store
from agentworkbench.identity.models import AccountStatus, Role

# ruff: noqa: S105, S106 —— 本文件的口令全是测试值，不连任何真实账号。

pytestmark = pytest.mark.invariant

ADMIN_EMAIL = "admin@example.com"
PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "a totally different passphrase"

NOW = datetime.now(UTC)


# ── 装配 ──


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    """阈值调成 3：锁定那条测试只差三次错口令，与断言无关。

    `session_invalidate_seconds=0` 是「不缓存、吊销立即生效」——判据要的
    「一个窗口内」在 0 下退化成「下一个请求」，测试因此不需要 sleep。
    """
    settings = Settings(
        data_dir=tmp_path,
        secret_key="endpoint-test-secret",
        auth=AuthSettings(
            login_max_failures=3,
            lockout_seconds=60,
            session_invalidate_seconds=0,
        ),
    )
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def another_client(app: FastAPI) -> TestClient:
    """另一个浏览器：独立 cookie。**必须真的独立**——同一个 client 登录两次，
    第二次会覆盖第一次的 cookie，而「两张票」正是 I-V9 的前提。"""
    return TestClient(app, raise_server_exceptions=False)


def open_conn(app: FastAPI) -> sqlite3.Connection:
    return cast(Deps, app.state.deps).open_conn()


def seed_admin(
    app: FastAPI, *, email: str = ADMIN_EMAIL, must_change_password: bool = False
) -> service.BootstrapResult:
    """空库引导。走服务层而不是 HTTP：引导**没有**端点，它是部署时的一条命令
    （`scripts/bootstrap_identity.py`），暴露成 HTTP 就等于把永久后门挂在网上。

    默认 `must_change_password=False`：大多数用例测的是登录/改密/成员管理本身，
    不是首登闸门；需要那条链的用例显式传 True。
    """
    conn = open_conn(app)
    try:
        return service.bootstrap(
            conn,
            email=email,
            password=PASSWORD,
            tenant_name="团队甲",
            now=NOW,
            must_change_password=must_change_password,
        )
    finally:
        conn.close()


def seed_member(
    app: FastAPI, *, tenant_id: str, admin_id: str, email: str, password: str = PASSWORD
) -> str:
    """用邀请造一个普通成员，返回 user_id。"""
    conn = open_conn(app)
    try:
        _, token = service.create_invite(
            conn,
            tenant_id=tenant_id,
            role=Role.MEMBER,
            created_by=admin_id,
            now=NOW,
        )
        accepted = service.accept_invite(
            conn,
            token=token,
            email=email,
            password=password,
            now=NOW,
            config=service.AuthConfig(),
        )
    finally:
        conn.close()
    return accepted.account.user_id


def login(client: TestClient, *, email: str = ADMIN_EMAIL, password: str = PASSWORD) -> Any:
    return client.post("/api/auth/login", json={"email": email, "password": password})


def without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    """比较错误体前剥掉 `request_id`：它是每请求随机的 uuid，与原因无关。
    排除随机字段后的逐字段相同，说明其余字段不携带任何可用于区分的信息。"""
    return {key: value for key, value in body.items() if key != "request_id"}


# ── 登录与 cookie ──


def test_login_sets_an_httponly_lax_cookie_and_keeps_the_token_out_of_the_body(
    app: FastAPI, client: TestClient
) -> None:
    """票据只在 cookie 里。

    响应体断言的是**字段集合相等**，不是「没有 token 字段」：后者在有人
    加了一个 `session` 或 `access_token` 时不会红。
    """
    seed_admin(app)

    response = login(client)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"id", "email", "role", "status", "tenantId", "mustChangePassword"}
    assert body["email"] == ADMIN_EMAIL
    assert body["role"] == "admin"
    assert body["mustChangePassword"] is False

    cookie = response.headers["set-cookie"].lower().replace(" ", "")
    assert cookie.startswith("awb_session=")
    assert "httponly" in cookie, "JS 读得到会话 = 一行 document.cookie 就带走"
    assert "samesite=lax" in cookie, "没有 SameSite，跨站表单就能代替用户操作"
    assert "path=/" in cookie


def test_login_failures_are_indistinguishable(app: FastAPI, client: TestClient) -> None:
    """`I-H2` 的自动化部分：口令错 / 账号不存在两种响应**逐字段相同**。

    停用与锁定那两条在下面各自断言「与不存在相同」，因为它们的构造更麻烦
    （要先有账号、先被锁），混在一条里会让失败时的定位成本变高。
    """
    seed_admin(app)

    wrong = login(client, password="not the password")
    missing = login(client, email="nobody@example.com", password="not the password")

    assert wrong.status_code == missing.status_code == 401
    assert without_request_id(wrong.json()) == without_request_id(missing.json())


def test_a_disabled_account_looks_exactly_like_a_wrong_password(
    app: FastAPI, client: TestClient
) -> None:
    """停用之后用**正确口令**登录，响应与「口令错」逐字段相同。

    这是本域最容易被写歪的一句提示：「您的账号已被停用，请联系管理员」——
    它对用户是善意的，对任何拿着邮箱列表的人是一台状态查询接口。
    """
    boot = seed_admin(app)
    bob = seed_member(app, tenant_id=boot.tenant_id, admin_id=boot.user_id, email="bob@example.com")
    conn = open_conn(app)
    try:
        assert service.disable_user(conn, user_id=bob, now=NOW) is True
    finally:
        conn.close()

    disabled = login(client, email="bob@example.com", password=PASSWORD)
    # 对照组必须是**另一个不存在的账号**，不能是「bob 的错误口令」：
    # bob 被停用之后，连错口令也会先撞上停用那道闸，两个请求拿到同一个原因码——
    # 那样比等于自己跟自己比，「按原因拼不同文案」这种改坏照样绿。
    # 这一条是被改坏验证抓出来的，不是想出来的。
    unrelated = login(client, email="nobody@example.com", password=PASSWORD)

    assert disabled.status_code == unrelated.status_code == 401
    assert without_request_id(disabled.json()) == without_request_id(unrelated.json())


def test_a_locked_account_looks_exactly_like_a_wrong_password(
    app: FastAPI, client: TestClient
) -> None:
    """锁定到期前后都不给提示。`login_max_failures=3`（见 fixture）。"""
    boot = seed_admin(app)
    seed_member(app, tenant_id=boot.tenant_id, admin_id=boot.user_id, email="bob@example.com")

    for _ in range(3):
        login(client, email="bob@example.com", password="nope")
    locked = login(client, email="bob@example.com", password=PASSWORD)
    missing = login(client, email="nobody@example.com", password=PASSWORD)

    assert locked.status_code == missing.status_code == 401
    assert without_request_id(locked.json()) == without_request_id(missing.json())


def test_me_without_a_cookie_is_401(app: FastAPI, client: TestClient) -> None:
    """白名单之外的读接口，没登录就是 401，不是 404 也不是空对象。"""
    seed_admin(app)

    response = client.get("/api/auth/me")

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def test_me_round_trips_the_account(client: TestClient, app: FastAPI) -> None:
    seed_admin(app)
    login(client)

    body = client.get("/api/auth/me").json()

    assert set(body) == {"id", "email", "role", "status", "tenantId", "mustChangePassword"}
    assert body["status"] == "active"
    assert body["mustChangePassword"] is False


# ── 首登改密（I-V19） ──


def test_login_reports_must_change_password(app: FastAPI, client: TestClient) -> None:
    """登录成功也要把“还没改初始口令”带回来；否则前端不知道该进哪一页。"""
    seed_admin(app, must_change_password=True)

    response = login(client)

    assert response.status_code == 200
    assert response.json()["mustChangePassword"] is True


def test_first_login_is_limited_to_me_logout_and_change_password(
    app: FastAPI, client: TestClient
) -> None:
    """改密前业务接口 403；me / logout / 改密本身仍然可达。

    只拦业务接口、不拦 logout：否则一个被强制改密的用户连“换个人登录”都做不到。
    只拦业务接口、不拦 me：否则前端拿不到“为什么被拦”，只能显示一个空白页。
    """
    seed_admin(app, must_change_password=True)
    assert login(client).status_code == 200

    blocked = client.get("/api/documents")
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "password_change_required"

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["mustChangePassword"] is True

    # 登出在另一个客户端上验，主客户端还要留着 cookie 去改密。
    other = another_client(app)
    assert login(other).status_code == 200
    assert other.post("/api/auth/logout").status_code == 204

    assert (
        client.post(
            "/api/auth/password",
            json={"oldPassword": PASSWORD, "newPassword": NEW_PASSWORD},
        ).status_code
        == 204
    )


def test_changing_the_initial_password_unlocks_the_api(app: FastAPI, client: TestClient) -> None:
    """改密成功后同一张 cookie 立即可用，标记必须清 0。"""
    seed_admin(app, must_change_password=True)
    assert login(client).status_code == 200
    assert client.get("/api/documents").status_code == 403

    changed = client.post(
        "/api/auth/password",
        json={"oldPassword": PASSWORD, "newPassword": NEW_PASSWORD},
    )
    assert changed.status_code == 204

    assert client.get("/api/documents").status_code == 200
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["mustChangePassword"] is False


# ── 登出（I-V14） ──


def test_logout_makes_the_same_cookie_useless(app: FastAPI, client: TestClient) -> None:
    """登出之后再拿同一张 cookie 请求 → 401，且库里那一行有 `revoked_at`。

    只清客户端 cookie 的实现，在这里会红：TestClient 的 cookie jar 里
    那条已经被删了，但我们要证的是**服务端**不认它了——所以这条测试
    手工把原值塞回去再请求一次。
    """
    seed_admin(app)
    response = login(client)
    raw = response.cookies["awb_session"]

    assert client.post("/api/auth/logout").status_code == 204

    client.cookies.set("awb_session", raw)
    assert client.get("/api/auth/me").status_code == 401

    conn = open_conn(app)
    try:
        row = store.get_ticket_by_hash(conn, credentials.hash_ticket(raw))
    finally:
        conn.close()
    assert row is not None and row.revoked_at is not None


# ── 改密（I-V9） ──


def test_change_password_keeps_the_asking_cookie_and_kills_the_other(
    app: FastAPI, client: TestClient
) -> None:
    """HTTP 版的 `I-V9`：cookie1 仍 200，cookie2 401。

    这条同时守住「改密后不重设 cookie」：重设会把「保留的票没升版本」
    这个 bug 盖住——用户手里是新票，测试全绿，而真正的问题没被证伪。
    """
    seed_admin(app)
    login(client)
    other = another_client(app)
    login(other)

    changed = client.post(
        "/api/auth/password",
        json={"oldPassword": PASSWORD, "newPassword": NEW_PASSWORD},
    )

    assert changed.status_code == 204
    assert client.get("/api/auth/me").status_code == 200, "发起改密的那条被踢了"
    assert other.get("/api/auth/me").status_code == 401, "别的会话还活着"

    # 新口令可用、旧口令不可用
    fresh = another_client(app)
    assert login(fresh, password=NEW_PASSWORD).status_code == 200
    assert login(fresh, password=PASSWORD).status_code == 401


def test_change_password_with_a_wrong_old_password_is_401_and_changes_nothing(
    app: FastAPI, client: TestClient
) -> None:
    seed_admin(app)
    login(client)

    response = client.post(
        "/api/auth/password",
        json={"oldPassword": "not the old one", "newPassword": NEW_PASSWORD},
    )

    assert response.status_code == 401
    fresh = another_client(app)
    assert login(fresh, password=PASSWORD).status_code == 200, "旧口令仍然可用"
    assert login(fresh, password=NEW_PASSWORD).status_code == 401


# ── 成员列表与隔离（I-V2 端点层） ──


def test_members_requires_a_session(client: TestClient) -> None:
    assert client.get("/api/members").status_code == 401


def test_member_list_is_scoped_to_my_team(app: FastAPI, client: TestClient) -> None:
    """两个团队各一人，互相看不到对方。

    这层是 SQL 的 `where tenant_id = ?` 在起作用；把它挪到 Python 里过滤
    也能过（列表结果一样），差别在读没读进内存——那正是 `I-V5` 反对的形态。
    """
    boot = seed_admin(app)
    seed_member(app, tenant_id=boot.tenant_id, admin_id=boot.user_id, email="bob@example.com")

    # 第二个团队：直接写库，因为引导只能跑一次，而邀请只能进已有团队。
    conn = open_conn(app)
    try:
        other_tenant = store.insert_tenant(conn, name="团队乙", now=NOW)
        store.insert_user(
            conn,
            tenant_id=other_tenant,
            email="carol@example.com",
            password_hash=credentials.hash_password(PASSWORD),
            role=Role.MEMBER,
            now=NOW,
        )
        conn.commit()
    finally:
        conn.close()

    login(client)
    mine = {row["email"] for row in client.get("/api/members").json()}
    assert mine == {ADMIN_EMAIL, "bob@example.com"}

    other = another_client(app)
    assert login(other, email="carol@example.com").status_code == 200
    theirs = {row["email"] for row in other.get("/api/members").json()}
    assert theirs == {"carol@example.com"}


# ── 停用 / 恢复 / 改角色（I-V6 / V11） ──


def test_disable_kills_the_members_session_immediately(app: FastAPI, client: TestClient) -> None:
    boot = seed_admin(app)
    bob = seed_member(app, tenant_id=boot.tenant_id, admin_id=boot.user_id, email="bob@example.com")
    bob_client = another_client(app)
    assert login(bob_client, email="bob@example.com").status_code == 200
    login(client)

    assert client.post(f"/api/members/{bob}/disable").status_code == 204

    assert bob_client.get("/api/auth/me").status_code == 401
    assert login(bob_client, email="bob@example.com").status_code == 401


def test_disabling_yourself_is_403_and_leaves_the_row_untouched(
    app: FastAPI, client: TestClient
) -> None:
    """`I-V11`：403 **且 status 不变**。

    「先写后判」的实现也返回 403，但那一行已经变了——而 403 会让调用方
    以为什么都没发生，于是没人去查。
    """
    boot = seed_admin(app)
    login(client)

    response = client.post(f"/api/members/{boot.user_id}/disable")

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"
    conn = open_conn(app)
    try:
        account = store.get_user(conn, boot.user_id)
    finally:
        conn.close()
    assert account is not None and account.status is AccountStatus.ACTIVE
    assert client.get("/api/auth/me").status_code == 200


def test_a_member_cannot_manage_anyone_even_a_nonexistent_id(
    app: FastAPI, client: TestClient
) -> None:
    """非管理员对**不存在的 id** 也拿到 403，不是 404。

    这一条守的是顺序：先查目标再判角色，非管理员就成了「这个 id 在不在」
    的探测器——而探测不需要任何权限。
    """
    boot = seed_admin(app)
    seed_member(app, tenant_id=boot.tenant_id, admin_id=boot.user_id, email="bob@example.com")
    bob_client = another_client(app)
    login(bob_client, email="bob@example.com")

    existing = bob_client.post(f"/api/members/{boot.user_id}/disable")
    missing = bob_client.post("/api/members/u-does-not-exist/disable")

    assert existing.status_code == missing.status_code == 403
    assert without_request_id(existing.json()) == without_request_id(missing.json())


def test_a_member_cannot_invite_an_admin(app: FastAPI, client: TestClient) -> None:
    boot = seed_admin(app)
    seed_member(app, tenant_id=boot.tenant_id, admin_id=boot.user_id, email="bob@example.com")
    bob_client = another_client(app)
    login(bob_client, email="bob@example.com")

    response = bob_client.post("/api/members/invites", json={"role": "admin"})

    assert response.status_code == 403


def test_promoting_a_member_takes_effect_on_the_next_request(
    app: FastAPI, client: TestClient
) -> None:
    """改角色不重签票据：角色每次请求从 `users` 表读，所以下一条请求就生效。

    这条同时证明「改角色不踢会话」——`credential_epoch` 没动，
    否则 bob 手里那张票会立刻失效，而他只是被升了职。
    """
    boot = seed_admin(app)
    bob = seed_member(app, tenant_id=boot.tenant_id, admin_id=boot.user_id, email="bob@example.com")
    bob_client = another_client(app)
    login(bob_client, email="bob@example.com")
    assert bob_client.post("/api/members/invites", json={"role": "member"}).status_code == 403

    login(client)
    promoted = client.patch(f"/api/members/{bob}", json={"role": "admin"})

    assert promoted.status_code == 200
    assert promoted.json()["role"] == "admin"
    assert bob_client.post("/api/members/invites", json={"role": "member"}).status_code == 201


def test_an_unknown_role_in_the_body_is_400(app: FastAPI, client: TestClient) -> None:
    """枚举外的值给 400 而不是 500：500 会让调用方重试，而重试一万次也一样。"""
    boot = seed_admin(app)
    bob = seed_member(app, tenant_id=boot.tenant_id, admin_id=boot.user_id, email="bob@example.com")
    login(client)

    response = client.patch(f"/api/members/{bob}", json={"role": "owner"})

    assert response.status_code == 400
    assert response.json()["code"] == "validation_failed"


# ── 邀请（I-V13） ──


def test_an_invite_brings_a_member_into_the_inviters_team(app: FastAPI, client: TestClient) -> None:
    """发邀请 → 另一个浏览器接受 → 201、拿到 cookie、落在同一个团队。"""
    boot = seed_admin(app)
    login(client)

    created = client.post(
        "/api/members/invites", json={"role": "member", "email": "dave@example.com"}
    )
    assert created.status_code == 201
    token = created.json()["token"]
    assert created.json()["inviteId"]

    invitee = another_client(app)
    accepted = invitee.post(
        f"/api/auth/invites/{token}",
        json={"email": "dave@example.com", "password": PASSWORD},
    )

    assert accepted.status_code == 201
    assert accepted.json()["tenantId"] == boot.tenant_id
    assert accepted.json()["role"] == "member"
    assert invitee.get("/api/auth/me").status_code == 200


def test_a_used_invite_is_indistinguishable_from_a_missing_one(
    app: FastAPI, client: TestClient
) -> None:
    """`I-V13`：用过 / 不存在，两个 404 剥掉 request_id 后逐字段相同。"""
    boot = seed_admin(app)
    login(client)
    token = client.post("/api/members/invites", json={"role": "member"}).json()["token"]
    invitee = another_client(app)
    invitee.post(
        f"/api/auth/invites/{token}", json={"email": "erin@example.com", "password": PASSWORD}
    )

    used = another_client(app).post(
        f"/api/auth/invites/{token}", json={"email": "frank@example.com", "password": PASSWORD}
    )
    missing = another_client(app).post(
        "/api/auth/invites/never-issued",
        json={"email": "frank@example.com", "password": PASSWORD},
    )

    assert used.status_code == missing.status_code == 404
    assert without_request_id(used.json()) == without_request_id(missing.json())
    assert boot.tenant_id == client.get("/api/auth/me").json()["tenantId"]


def test_an_expired_invite_is_indistinguishable_from_a_missing_one(
    app: FastAPI, client: TestClient
) -> None:
    """过期那条：把 `expires_at` 改到过去，不 sleep（代码规范 §6.4）。

    「过期与不存在的响应逐字段相同」这条判据，服务层只能验到「同一个异常类型」；
    真正决定用户看到什么的是路由层——而路由层只要有一天为过期写了句
    「邀请已过期，请联系管理员」，这张表的存在性就漏了。
    """
    boot = seed_admin(app)
    login(client)
    token = client.post("/api/members/invites", json={"role": "member"}).json()["token"]

    conn = open_conn(app)
    try:
        conn.execute(
            "update invites set expires_at = ? where token_hash = ?",
            (store.stamp(NOW - timedelta(days=1)), credentials.hash_ticket(token)),
        )
        conn.commit()
    finally:
        conn.close()

    expired = another_client(app).post(
        f"/api/auth/invites/{token}", json={"email": "gina@example.com", "password": PASSWORD}
    )
    missing = another_client(app).post(
        "/api/auth/invites/never-issued",
        json={"email": "gina@example.com", "password": PASSWORD},
    )

    assert expired.status_code == missing.status_code == 404
    assert without_request_id(expired.json()) == without_request_id(missing.json())
    assert boot.tenant_id  # 引导成功才说明这条测试的前置成立


def test_no_oauth_provider_or_mail_library_is_wired_in() -> None:
    """`I-V15` 的后半句：配置里没有 OAuth 键，依赖里没有身份框架与邮件库。

    这一条防的不是「写错」，是「顺手接进来」：`fastapi-users` 一旦装进依赖，
    它的默认路由（找回密码、邮箱确认、OAuth 回调）就在那里了，而没有任何人
    会去验收它们——这正是本域拒绝整套框架的第一个理由。

    断言依赖清单而不是 `find_spec`：包可能作为别的东西的传递依赖被装上，
    而「它出现在 `project.dependencies` 里」才是「我们决定用它」的证据。
    """
    fields = set(Settings.model_fields)
    assert not {
        name for name in fields if any(word in name for word in ("oauth", "smtp", "mail"))
    }, "配置里出现了身份提供方或邮件相关的键"

    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )
    declared = " ".join(pyproject["project"]["dependencies"]).lower()
    for package in ("fastapi-users", "authlib", "fastapi-mail", "aiosmtpd", "python-jose"):
        assert package not in declared, f"{package} 不该出现在依赖里（§2.1 / §2.5）"


# ── 不存在的端点（I-V15） ──


@pytest.mark.parametrize(("method", "path"), absent_paths())
def test_endpoints_that_must_stay_absent_are_absent(
    method: str, path: str, client: TestClient
) -> None:
    """自助找回与邮箱确认**一律 404**（§2.1 / §2.5）。

    这条判据防的不是「我们写错了」，而是「框架替我们写好了」：
    `fastapi-users` 这类库带上这两个端点，接进来那一刻它们就存在了，
    而没有任何人会去验收它们。
    """
    response = client.request(method, path, json={})

    assert response.status_code == 404


# ── D9 红队抓到的两条：空口令 / 邀请被当邮箱枚举器 ──


def test_an_empty_password_cannot_create_an_account(app: FastAPI, client: TestClient) -> None:
    """接受邀请时口令不能是空串——否则建出一个「知道邮箱就能登录」的账号。

    D9 红队 A 的 R-02：`POST /api/auth/invites/<t> {"password": ""}` 返回 201，
    随后 `POST /api/auth/login {"password": ""}` 返回 200。这种账号在成员列表里
    与正常账号长得一模一样，入侵者可以先把自己降成空口令再躲在里面。

    同时断言**这张邀请没有被烧掉**：口令格式错误是客户端的问题，不该让被邀请人
    回去找管理员重发一张。
    """
    seed_admin(app)
    assert login(client).status_code == 200
    token = client.post("/api/members/invites", json={"role": "member"}).json()["token"]

    anonymous = TestClient(app, raise_server_exceptions=False)
    rejected = anonymous.post(
        f"/api/auth/invites/{token}", json={"email": "new@example.com", "password": "   "}
    )
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["code"] == "validation_failed"

    # 换一个正常口令，同一张邀请仍然可用
    accepted = anonymous.post(
        f"/api/auth/invites/{token}",
        json={"email": "new@example.com", "password": "a-real-passphrase"},
    )
    assert accepted.status_code == 201, "口令格式错误把邀请也烧掉了——那会逼用户找人重发"


def test_changing_the_password_to_empty_is_rejected(app: FastAPI, client: TestClient) -> None:
    """改密同样不许把口令改成空串，且失败后旧口令仍然有效。"""
    seed_admin(app)
    assert login(client).status_code == 200

    rejected = client.post("/api/auth/password", json={"oldPassword": PASSWORD, "newPassword": ""})
    assert rejected.status_code == 400, rejected.text

    # 旧口令没被改动：换一个新客户端重新登录仍然可以
    fresh = TestClient(app, raise_server_exceptions=False)
    assert login(fresh).status_code == 200


def test_probing_a_registered_email_burns_the_invite(app: FastAPI, client: TestClient) -> None:
    """一张邀请只能换到一次「这个邮箱注册过吗」的答案。

    D9 红队 A/B 各自复现过：`400 这个邮箱已经有账号了` 这条失败路径**不消耗令牌**，
    于是一张邀请可以拿一份邮箱名单逐个跑一遍——而邀请会被转发、会留在聊天记录里。

    现在的语义：问一次，令牌作废（`accepted_at` 有值、`accepted_by` 为空）。
    这一步不能消掉「这一次的答案」，但把它从「无限次」变成「一次」。
    """
    seeded = seed_admin(app)
    assert login(client).status_code == 200
    invite = client.post("/api/members/invites", json={"role": "member"}).json()
    token, invite_id = invite["token"], invite["inviteId"]

    anonymous = TestClient(app, raise_server_exceptions=False)
    probe = anonymous.post(
        f"/api/auth/invites/{token}", json={"email": ADMIN_EMAIL, "password": PASSWORD}
    )
    assert probe.status_code == 400, probe.text
    assert "已经有账号" in probe.json()["message"]

    # 同一张邀请再试一次（哪怕换一个全新邮箱）→ 已经没用了
    again = anonymous.post(
        f"/api/auth/invites/{token}",
        json={"email": "brand-new@example.com", "password": PASSWORD},
    )
    assert again.status_code == 404, "这张邀请还在被复用——枚举器仍然成立"

    conn = open_conn(app)
    try:
        row = conn.execute(
            "select accepted_at, accepted_by from invites where id = ?", (invite_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["accepted_at"] is not None, "邀请没有被标记作废"
    assert row["accepted_by"] is None, "没人接受过它，记成某人接受会让审计回答错问题"
    assert seeded.user_id  # 只是让 seed 的返回值被用到，别被 lint 当成未使用
