"""平台级配置的写权限（`REQ-AUTH-12` / `I-V18`）。

这一条不来自任何表格，来自一次实读：`settings` 六条端点当时**只要求登录**，
而 `providers` / `provider_models` / `system_state` 三张表**没有团队列**。
两者叠起来是「任何成员都能改这套安装的模型来源」，比「少一个判断」严重：

    POST /api/settings/providers      → 建一个自己的供应商，base_url 指向自己的机器
    POST /api/settings/providers/{id}/activate

走完这两步，平台上所有问答都经那台机器：提问原文、检索出的知识库片段他全收到，
答案也由他决定。F3 的判定与引用在更上层，拦不住被投毒的正文。

根因不是「漏了判定」，是**权限被放在了错误的轴上**。`role` 回答「在我这个团队里
能管谁」，而这三张表不属于任何团队。所以这次修的是轴，不是补一个 `if`：

- 加一列 `users.is_platform_admin`，不进 `ROLE_RANK`；
- 只有引导路径能写 1（能上服务器的人 = 这套安装的所有者）；
- 四条写入口挂 `judge_platform`，403；读接口不动。

改坏这些不变量，用户看到什么：

- 写入口只判登录 → 任何成员架一台机器，全平台问答与资料片段都经它，界面一切正常；
- 把这一列并进 `ROLE_RANK` 当第三档 → 「目标角色不得高于自己」会把管供应商的人
  当成更高的团队角色，而他在自己团队里可能只是普通成员；
- 给它开一个授予接口 → 团队管理员自封，这条判据整体归零。

装配与种子复用 `test_global_gate.py`：那里已经有「bootstrap 出真管理员 + 邀请出真成员」
的一套，再造一份等于两处各错一次。
"""

from __future__ import annotations

import importlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_global_gate import build_app, events, login, open_conn, seed_admin, seed_member

from agentworkbench.api.deps import utc_now
from agentworkbench.api.identity_context import COOKIE_NAME
from agentworkbench.core.identity import Owner
from agentworkbench.db import crypto
from agentworkbench.db.repositories import providers as provider_repo
from agentworkbench.identity import policy, service, store
from agentworkbench.identity.models import ROLE_RANK, AccountStatus, Outcome, Principal, Role

# ruff: noqa: S105 —— 本文件的口令全是测试值，不连任何真实账号。

pytestmark = pytest.mark.invariant

PASSWORD = "correct horse battery staple"
TEAM_ADMIN_EMAIL = "teamadmin@example.com"
MEMBER_EMAIL = "member@example.com"
NOW = datetime.now(UTC)

CONFIG_TABLES = ("providers", "provider_models", "system_state")
"""这三张就是「平台级配置」的全部落点：谁改了它们，谁就改了这套安装的模型来源。"""

WRITES = [
    ("post", "/api/settings/providers", {"name": "偷梁换柱", "baseUrl": "http://evil.local"}),
    ("post", "/api/settings/providers/{id}/activate", None),
    ("post", "/api/settings/providers/{id}/models", {"model": "gpt-4o-mini"}),
    ("put", "/api/settings/embedding", {"model": "text-embedding-3-small"}),
]
"""四条写入口。**四条都要在列表里**：漏一条的表现是那条路由照常工作，
而它的后果（换 embedding 模型让全平台检索失效）比换供应商更安静。"""


# ── 辅助 ──


def fingerprint(app: FastAPI) -> dict[str, list[dict[str, Any]]]:
    """三张配置表的**逐行全列**快照。

    不用「count(*) 没变」：拒绝路径上真正该防的不是插入，而是「先删后插」
    或者「改了一半」——行数一样而内容已经换了。全列快照才能证明「什么都没发生」。
    """
    conn = open_conn(app)
    try:
        return {
            # S608 误报：table 只来自本模块写死的常量，不是外部输入
            table: [
                dict(row)
                for row in conn.execute(f"select * from {table} order by 1")  # noqa: S608
            ]
            for table in CONFIG_TABLES
        }
    finally:
        conn.close()


def seed_provider(app: FastAPI) -> int:
    """先放一条真实供应商。

    **必须先有这一行**：`/activate` 与 `/models` 的路径里带 id。没有行的话
    「403 而不是 404」仍然成立（依赖先于函数体），但「配置一字节不变」
    就变成了「表本来就是空的」——那什么都证明不了。
    """
    conn = open_conn(app)
    try:
        with conn:
            return provider_repo.insert_provider(
                conn,
                name="既有供应商",
                base_url="http://127.0.0.1:9",
                now=utc_now(),
                # 空 key 也走加密：仓库层要求非空密文（S-V6 的四种未配置形态由
                # 装配层判断，不靠这里的空串）。本文件测的是「谁能写」。
                api_key_encrypted=crypto.encrypt(cast(str, app.state.deps.settings.secret_key), ""),
            )
    finally:
        conn.close()


def write(client: TestClient, method: str, path: str, payload: Any) -> Any:
    call = getattr(client, method)
    return call(path) if payload is None else call(path, json=payload)


# ── 夹具 ──


@pytest.fixture(name="app")
def _app(tmp_path: Path) -> FastAPI:
    return build_app(tmp_path)


@pytest.fixture(name="live")
def _live(app: FastAPI) -> Iterator[TestClient]:
    """一个「让 app 活着」的客户端。

    为了让 lifespan 跑完——建库在那里发生，而 `seed_*` 直接写表。没有它，
    seed 拿到的是一个还没有表的库（`no such table: users`）。

    它自己不参与断言：每个用例用的身份各自建客户端，cookie jar 互不串。
    """
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture(name="boot")
def _boot(app: FastAPI, live: TestClient) -> service.BootstrapResult:
    return seed_admin(app)


@pytest.fixture(name="provider_id")
def _provider_id(app: FastAPI, live: TestClient) -> int:
    return seed_provider(app)


@pytest.fixture(name="owner_client")
def _owner_client(app: FastAPI, boot: service.BootstrapResult) -> Iterator[TestClient]:
    """引导出来的那个账号：既是团队管理员，也是这套安装的所有者。"""
    with TestClient(app, raise_server_exceptions=False) as client:
        assert login(client).status_code == 200
        yield client


@pytest.fixture(name="admin_client")
def _admin_client(app: FastAPI, boot: service.BootstrapResult) -> Iterator[TestClient]:
    """**真正的团队管理员**：`role=admin`，`is_platform_admin=0`。

    用邀请建号，因为那是「不是所有者的人」唯一合法的出生方式；
    直接把 boot 账号的 role 改掉没有意义——它本来就是所有者。
    """
    conn = open_conn(app)
    try:
        _, token = service.create_invite(
            conn, tenant_id=boot.tenant_id, role=Role.ADMIN, created_by=boot.user_id, now=NOW
        )
        accepted = service.accept_invite(
            conn,
            token=token,
            email=TEAM_ADMIN_EMAIL,
            password=PASSWORD,
            now=NOW,
            config=service.AuthConfig(),
        )
    finally:
        conn.close()
    with TestClient(app, raise_server_exceptions=False) as client:
        client.cookies.set(COOKIE_NAME, accepted.ticket)
        yield client


# ── 写入口的边界（判据主句） ──


@pytest.mark.parametrize(("method", "path", "payload"), WRITES)
def test_a_team_admin_cannot_write_any_platform_config(
    method: str,
    path: str,
    payload: Any,
    app: FastAPI,
    provider_id: int,
    admin_client: TestClient,
) -> None:
    """`role=admin` 的团队管理员对四条写入口全部 403，且三张表一字节不变。"""
    before = fingerprint(app)
    response = write(admin_client, method, path.format(id=provider_id), payload)
    assert response.status_code == 403, response.text
    assert fingerprint(app) == before, "被拒的写入口改了配置——403 只是个状态码"


@pytest.mark.parametrize(("method", "path", "payload"), WRITES)
def test_the_platform_owner_can_write(
    method: str,
    path: str,
    payload: Any,
    provider_id: int,
    owner_client: TestClient,
) -> None:
    """所有者这四条都走得通。

    只验「403」不验「所有者能过」，等于把一个功能锁死了也叫通过——判据的目的是
    **划清谁管**，不是禁止改配置。所有者自己改不了，产品就没法换模型了。
    """
    response = write(owner_client, method, path.format(id=provider_id), payload)
    assert response.status_code in (200, 201), response.text


def test_readers_stay_open_to_everyone_signed_in(admin_client: TestClient) -> None:
    """读接口不跟着收紧：界面要显示「当前用哪个模型」，那不是权力。

    收紧读的版本看起来更安全，代价是成员打开设置页看到一片 403，然后去找人要
    「管理员」——而他要的东西根本不是管理员。
    """
    assert admin_client.get("/api/settings/providers").status_code == 200
    assert admin_client.get("/api/settings/embedding").status_code == 200


def test_a_plain_member_is_refused_too(
    app: FastAPI, boot: service.BootstrapResult, provider_id: int, live: TestClient
) -> None:
    """不带 admin 的成员同样 403。

    与上一条合起来才锁死：只判 `role` 的实现会让「成员 403」这条过，
    但上一条（团队管理员被拒）会红。两条一起，「按角色判」和「按平台轴判」
    才区分得开。
    """
    seed_member(app, tenant_id=boot.tenant_id, admin_id=boot.user_id)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert login(client, email=MEMBER_EMAIL).status_code == 200
        response = client.post(
            "/api/settings/providers", json={"name": "x", "baseUrl": "http://evil.local"}
        )
        assert response.status_code == 403


def test_the_refusal_is_recorded_with_its_own_reason(
    admin_client: TestClient, app: FastAPI
) -> None:
    """记录里写 `platform_admin_required`，不是笼统的 `role_insufficient`。

    两者对运维的含义相反：前者要去问部署方，后者要找该团队的管理员。
    合并之后记录里所有 403 长得一样，而处置方式相反的两种拒绝并成了一种。
    """
    response = admin_client.post(
        "/api/settings/providers", json={"name": "x", "baseUrl": "http://evil.local"}
    )
    request_id = response.headers["x-request-id"]

    rows = [row for row in events(app) if row["request_id"] == request_id]
    assert len(rows) == 1
    assert rows[0]["reason_code"] == "platform_admin_required"
    assert rows[0]["actor_user_id"] is not None, "已识别的人也拒了，记录里必须有他是谁"


def test_the_check_happens_before_field_validation(admin_client: TestClient) -> None:
    """非所有者发一个**缺字段**的请求 → 403，不是 422。

    422 会说「这个端点要 `name` 与 `baseUrl`」，403 什么都不说。这条靠
    `dependencies=[...]`（依赖先于字段校验）与「函数体第一行」的区别——后者的
    顺序是「pydantic 先校验一遍，再在函数体里被拒」，于是非所有者能读到字段清单。

    **不是「畸形 JSON」**：那种请求在 `request.json()` 阶段就失败了，比任何依赖
    都早，403 不可能出现。把它写进断言只会测到 FastAPI 自身的顺序，与本条判据无关。
    """
    response = admin_client.post("/api/settings/providers", json={})
    assert response.status_code == 403, response.text


def test_no_endpoint_can_hand_out_the_flag(
    app: FastAPI, boot: service.BootstrapResult, admin_client: TestClient, live: TestClient
) -> None:
    """团队管理员做遍所有成员管理动作之后，自己仍然不是平台所有者。

    这条是「没有任何接口能授予」的**实证版本**，比遍历路由表找名字可靠：
    后者只能证明「没有一条路由叫 platform」，而授予可能藏在一个
    `PATCH /api/members/{id}` 的 body 字段里。
    """
    member_id = seed_member(app, tenant_id=boot.tenant_id, admin_id=boot.user_id)

    admin_client.patch(f"/api/members/{member_id}", json={"role": "admin"})
    admin_client.post(f"/api/members/{member_id}/disable")
    admin_client.post(f"/api/members/{member_id}/enable")
    admin_client.post("/api/members/invites", json={"role": "admin"})

    still_refused = admin_client.post(
        "/api/settings/providers", json={"name": "x", "baseUrl": "http://evil.local"}
    )
    assert still_refused.status_code == 403


# ── 两根轴不许合并 ──


def test_the_two_axes_do_not_imply_each_other() -> None:
    """平台轴与团队角色轴**互不蕴含**，两个方向都要断。

    只有第一个方向（平台所有者不能管团队）是可以写成恒真式的：`judge_admin` 的
    第一条就是 `role is not ADMIN → 403`，而测试用的 `role=member` 正好在那里出局。
    改坏验证抓到过一次——那一版无论怎么改等级判定都不会红，因为它根本没走到那条。
    所以这里必须**两个方向**都断，第二条才真正钉住「`judge_admin` 不许拿平台标记
    当挡箭牌」。

    改坏它，用户看到什么：一个人被设为管供应商之后，顺手也能停用团队管理员——
    而这两件事本来就该由两个人分别持有。
    """
    platform_owner_but_member = Principal(
        owner=Owner(user_id="u1", tenant_id="t1"),
        role=Role.MEMBER,
        status=AccountStatus.ACTIVE,
        credential_epoch=1,
        is_platform_admin=True,
    )
    team_admin_but_not_owner = Principal(
        owner=Owner(user_id="u2", tenant_id="t1"),
        role=Role.ADMIN,
        status=AccountStatus.ACTIVE,
        credential_epoch=1,
    )

    # 方向一：管得了供应商，管不了人
    assert policy.judge_platform(actor=platform_owner_but_member).outcome is Outcome.ALLOW
    assert policy.judge_admin(actor=platform_owner_but_member).outcome is Outcome.FORBIDDEN

    # 方向二：管得了人，管不了供应商
    assert policy.judge_admin(actor=team_admin_but_not_owner).outcome is Outcome.ALLOW
    assert policy.judge_platform(actor=team_admin_but_not_owner).outcome is Outcome.FORBIDDEN

    # 等级表仍然只有两档：平台标记没有变成第三种角色
    assert ROLE_RANK[Role.ADMIN] == 2


def test_the_owner_flag_comes_from_bootstrap_only(
    app: FastAPI, boot: service.BootstrapResult, live: TestClient
) -> None:
    """引导出来的账号是所有者；邀请建出来的不是。

    第二句是 `insert_user(is_platform_admin=False)` 那个默认值的判据：
    默认 True 的版本会让每一次邀请都悄悄多一个能改全平台配置的人。
    """
    invited_id = seed_member(app, tenant_id=boot.tenant_id, admin_id=boot.user_id)

    conn = open_conn(app)
    try:
        owner = store.get_user(conn, boot.user_id)
        invited = store.get_user(conn, invited_id)
    finally:
        conn.close()

    assert owner is not None and owner.is_platform_admin is True
    assert invited is not None and invited.is_platform_admin is False


# ── 老库升级（迁移 0006） ──


def _run_0006(conn: sqlite3.Connection) -> bool:
    """在给定连接上跑 0006 的加列逻辑。

    模块名以数字开头，`import` 语句写不出来，所以走 `import_module`。
    调用的是 `add_platform_admin_column` 而不是 `upgrade()`：后者要 alembic 的
    绑定胶水（`op.get_bind()`），而那层胶水在测试里只能 mock——mock 过的迁移
    等于没测。加列的判断与 SQL 都在被调的那个函数里。
    """
    module = importlib.import_module("agentworkbench.db.migrations.versions.0006_platform_admin")
    return bool(module.add_platform_admin_column(conn))


@pytest.fixture(name="legacy")
def _legacy(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """阶段 1 形态的 users 表：**没有** `is_platform_admin`。"""
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "create table users ("
        " id text primary key, tenant_id text not null, email text not null,"
        " password_hash text not null, role text not null, status text not null,"
        " credential_epoch integer not null default 1, lockout_until text,"
        " failed_attempts integer not null default 0,"
        " created_at text not null, updated_at text not null);"
        "insert into users (id, tenant_id, email, password_hash, role, status,"
        " credential_epoch, lockout_until, failed_attempts, created_at, updated_at)"
        " values ('u-old','t1','old@example.com','h','admin','active',1,null,0,'t','t');"
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def test_an_old_database_gains_the_column_with_zero(legacy: sqlite3.Connection) -> None:
    """老库升级后：列在，老账号是 0。

    **默认值必须是 0。** 反过来（或迁移时把已有 admin 置 1）的版本会让老库里的
    第一个管理员在升级之后静默获得改全平台配置的权力——升级本身没有提示，
    而那一刻正好是「谁都没在看着配置」的时候。
    """
    _run_0006(legacy)

    row = legacy.execute("select is_platform_admin from users where id = 'u-old'").fetchone()
    assert row["is_platform_admin"] == 0


def test_running_the_migration_twice_is_harmless(legacy: sqlite3.Connection) -> None:
    """新库建表时列已经在（`IDENTITY_SCHEMA_SQL`），迁移再跑一次不能炸。

    这不是理论情况：新库启动走「建表 → 按版本号补迁移」，而 `create table
    if not exists` 对加列无效。第一次遇到的表现是启动时
    `duplicate column name: is_platform_admin`——服务起不来，库却是好的。
    """
    _run_0006(legacy)
    _run_0006(legacy)

    columns = [row[1] for row in legacy.execute("pragma table_info(users)").fetchall()]
    assert columns.count("is_platform_admin") == 1
