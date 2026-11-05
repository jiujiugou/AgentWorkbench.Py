"""存储面的不变量：唯一约束、原子守卫、三列分离（I-V7 / I-V9 / I-V13）。

这一层测的是**库替我们做的事**，不是应用层记不记得做。两者的差别在并发与手工 SQL
上才显现，而那时已经晚了——所以每条约束都要有一条「绕过应用层直接写，必须被拒」的用例。

改坏这些不变量，用户会看到什么：

- 邮箱只在应用层查重 → 一次脚本导入就出现同一个邮箱两个账号，**登录时取哪一行看运气**，
  而两个团队各自看都正常（`I-V2` 的表面绿，数据面已经串了）；
- 邀请「先查再用」→ 并发点两次，同一张邀请建出两个账号，第二个人从此没有来源可追溯；
- 改密时遍历吊销全部票据 → 发起改密的人立刻被踢下线，用户会以为系统坏了，
  然后去找管理员——而那正是刚刚发生的事导致的（`I-V9`）；
- 把口令哈希放进 `Account` → 某个 `asdict()` 就把它带进了日志或响应，
  而这条路径不会有任何报错。
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentworkbench.db import engine
from agentworkbench.identity import store
from agentworkbench.identity.models import (
    Account,
    AccountStatus,
    IdentityEvent,
    ReasonCode,
    Role,
    Ticket,
)

NOW = datetime(2026, 11, 4, 9, 0, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """空库 + schema。必须走 `engine.connect`：`vec_chunks_vec` 是 vec0 虚表，
    裸 `sqlite3.connect` 没加载扩展，建表就会报 `no such module`。"""
    connection = engine.connect(tmp_path / "identity.db")
    engine.create_schema(connection)
    yield connection
    connection.close()


def make_tenant(conn: sqlite3.Connection, name: str = "团队甲") -> str:
    tenant_id = store.insert_tenant(conn, name=name, now=NOW)
    conn.commit()
    return tenant_id


def make_user(
    conn: sqlite3.Connection,
    tenant_id: str,
    *,
    email: str = "alice@example.com",
    role: Role = Role.MEMBER,
) -> str:
    user_id = store.insert_user(
        conn,
        tenant_id=tenant_id,
        email=email,
        # S106 是"硬编码口令"的检查；这里是假串，不参与任何校验
        password_hash="argon2-placeholder",  # noqa: S106
        role=role,
        now=NOW,
    )
    conn.commit()
    return user_id


# ── 表结构与约束（库层保证） ──


def test_all_five_tables_exist(conn: sqlite3.Connection) -> None:
    rows = conn.execute("select name from sqlite_master where type = 'table'").fetchall()
    names = {str(row["name"]) for row in rows}

    assert {"tenants", "users", "tickets", "invites", "identity_events"} <= names


def test_must_change_password_is_explicit_and_defaults_to_false(conn: sqlite3.Connection) -> None:
    """首登改密标记是**库里的一列**，不是从 epoch / 平台标记推断出来的。

    推断的版本今天能过，但重置口令会把 epoch 加一，于是“还没改初始口令”这个事实
    被一个无关的版本号顺手抹掉；而这一列不会。默认 0 是判据：存量账号升级后
    不能被误伤成“所有人都必须先改密”。
    """
    tenant_id = make_tenant(conn)
    user_id = make_user(conn, tenant_id)

    account = store.get_user(conn, user_id)
    assert account is not None
    assert account.must_change_password is False

    assert store.set_must_change_password(conn, user_id, required=True, now=NOW) is True
    marked = store.get_user(conn, user_id)
    assert marked is not None and marked.must_change_password is True

    assert store.set_must_change_password(conn, user_id, required=False, now=NOW) is True
    cleared = store.get_user(conn, user_id)
    assert cleared is not None and cleared.must_change_password is False


def test_email_is_unique_across_the_whole_table(conn: sqlite3.Connection) -> None:
    """邮箱**全局**唯一，不是团队内唯一。

    登录只凭 email + 口令，请求里没有团队上下文——两行同 email 时，取哪一行是任意的。
    那不是「两个团队各有一个账号」，是「同一个人可能登进别人的团队」。
    """
    first = make_tenant(conn, "团队甲")
    second = make_tenant(conn, "团队乙")
    make_user(conn, first, email="alice@example.com")

    with pytest.raises(sqlite3.IntegrityError):
        make_user(conn, second, email="alice@example.com")


def test_email_uniqueness_ignores_case(conn: sqlite3.Connection) -> None:
    """大小写不同也是同一个邮箱。列上的 `COLLATE NOCASE` 是第二道保险——
    应用层归一化会被一次手工 SQL 或一次 CSV 导入绕过去。"""
    tenant_id = make_tenant(conn)
    make_user(conn, tenant_id, email="alice@example.com")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "insert into users (id, tenant_id, email, password_hash, role, status,"
            " credential_epoch, failed_attempts, created_at, updated_at)"
            " values ('u-raw', ?, 'ALICE@Example.COM', 'x', 'member', 'active', 1, 0, ?, ?)",
            (tenant_id, store.stamp(NOW), store.stamp(NOW)),
        )


def test_lookup_by_email_is_case_insensitive(conn: sqlite3.Connection) -> None:
    tenant_id = make_tenant(conn)
    make_user(conn, tenant_id, email="alice@example.com")

    found = store.get_user_by_email(conn, "  ALICE@Example.com ")

    assert found is not None
    assert found.email == "alice@example.com"


def test_email_normalization_drops_whitespace_on_write(conn: sqlite3.Connection) -> None:
    tenant_id = make_tenant(conn)
    user_id = make_user(conn, tenant_id, email="  Bob@Example.com  ")

    row = conn.execute("select email from users where id = ?", (user_id,)).fetchone()
    assert row["email"] == "bob@example.com"


@pytest.mark.parametrize(
    ("column", "value"),
    [("role", "owner"), ("status", "deleted")],
)
def test_role_and_status_check_rejects_unknown_values(
    conn: sqlite3.Connection, column: str, value: str
) -> None:
    """枚举值由 CHECK 兜底。应用层枚举加了新值而忘了改 CHECK，会在这里红——
    而不是等到某个凌晨，有人写进一个判定里没有分支的状态。"""
    tenant_id = make_tenant(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "insert into users (id, tenant_id, email, password_hash, role, status,"
            " credential_epoch, failed_attempts, created_at, updated_at)"
            " values ('u-bad', ?, 'bad@example.com', 'x', ?, ?, 1, 0, ?, ?)",
            (
                tenant_id,
                value if column == "role" else "member",
                value if column == "status" else "active",
                store.stamp(NOW),
                store.stamp(NOW),
            ),
        )


def test_credential_epoch_cannot_be_zero(conn: sqlite3.Connection) -> None:
    """从 1 起：0 与「没有版本」不可区分，而「没有版本」会让所有旧票据意外通过比较。"""
    tenant_id = make_tenant(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "insert into users (id, tenant_id, email, password_hash, role, status,"
            " credential_epoch, failed_attempts, created_at, updated_at)"
            " values ('u-zero', ?, 'zero@example.com', 'x', 'member', 'active', 0, 0, ?, ?)",
            (tenant_id, store.stamp(NOW), store.stamp(NOW)),
        )


def test_ticket_token_hash_is_unique(conn: sqlite3.Connection) -> None:
    """两行同哈希时，「吊销哪一条」成了不明确的操作——而吊销失败是静默的。"""
    tenant_id = make_tenant(conn)
    user_id = make_user(conn, tenant_id)
    ticket = Ticket(
        ticket_id="tk-1",
        user_id=user_id,
        token_hash="a" * 64,
        credential_epoch=1,
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    store.insert_ticket(conn, ticket)

    with pytest.raises(sqlite3.IntegrityError):
        store.insert_ticket(conn, dataclasses.replace(ticket, ticket_id="tk-2"))


def test_account_does_not_carry_the_password_hash() -> None:
    """`Account` 上**没有** `password_hash` 字段。

    这不是洁癖：那个对象会到处传，将来某处一个 `asdict()` 就把它带进日志或响应，
    而那条路径不会报错。哈希要单独取（`password_hash_for`），多写一行换来「不可能顺手泄露」。
    """
    names = {field.name for field in dataclasses.fields(Account)}

    assert "password_hash" not in names
    assert "password" not in names


# ── 原子守卫（应用层做不对，必须靠 SQL） ──


def test_accepting_an_invite_twice_is_refused(conn: sqlite3.Connection) -> None:
    """I-V13：同一邀请第二次接受必须失败。

    守卫写在 SQL 里（`and accepted_at is null`）。「先查再用」在并发下两次都成功，
    而两次成功意味着同一张邀请建出两个账号——第二个人从此没有来源可追溯。
    """
    tenant_id = make_tenant(conn)
    admin = make_user(conn, tenant_id, email="admin@example.com", role=Role.ADMIN)
    invite_id = store.insert_invite(
        conn,
        tenant_id=tenant_id,
        role=Role.MEMBER,
        token_hash="b" * 64,
        expires_at=NOW + timedelta(days=1),
        created_by=admin,
        now=NOW,
    )

    assert store.accept_invite(conn, invite_id, accepted_by=admin, now=NOW) is True
    assert store.accept_invite(conn, invite_id, accepted_by=admin, now=NOW) is False


def test_revoking_a_ticket_twice_reports_the_second_as_no_change(
    conn: sqlite3.Connection,
) -> None:
    """重复登出不该被当成成功——那会让「这次登出真的生效了吗」无从判断。"""
    tenant_id = make_tenant(conn)
    user_id = make_user(conn, tenant_id)
    store.insert_ticket(
        conn,
        Ticket(
            ticket_id="tk-1",
            user_id=user_id,
            token_hash="c" * 64,
            credential_epoch=1,
            issued_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        ),
    )

    assert store.revoke_ticket(conn, "tk-1", now=NOW) is True
    assert store.revoke_ticket(conn, "tk-1", now=NOW) is False


def test_revoking_all_tickets_can_keep_the_one_that_asked(conn: sqlite3.Connection) -> None:
    """I-V9：改密时保留发起改密的那条，其余全吊销。

    不保留的话，用户改完密码立刻被踢下线——第一反应是「系统坏了」，
    而原因正是他刚做的事。
    """
    tenant_id = make_tenant(conn)
    user_id = make_user(conn, tenant_id)
    for index in ("1", "2", "3"):
        store.insert_ticket(
            conn,
            Ticket(
                ticket_id=f"tk-{index}",
                user_id=user_id,
                token_hash=index * 64,
                credential_epoch=1,
                issued_at=NOW,
                expires_at=NOW + timedelta(hours=12),
            ),
        )

    revoked = store.revoke_user_tickets(conn, user_id, now=NOW, keep_ticket_id="tk-1")

    assert revoked == 2
    still_valid = {
        str(row["id"])
        for row in conn.execute(
            "select id from tickets where revoked_at is null and user_id = ?", (user_id,)
        )
    }
    assert still_valid == {"tk-1"}


def test_status_change_requires_the_expected_current_value(
    conn: sqlite3.Connection,
) -> None:
    """停用 / 恢复带 CAS：两个管理员同时操作时，后到的那个**静默失败**而不是覆盖。

    不带 expected 的话，两条操作在界面上都显示成功，而库里只剩一个结果，
    谁都不知道自己那一下其实没生效。
    """
    tenant_id = make_tenant(conn)
    user_id = make_user(conn, tenant_id)

    assert (
        store.set_status(
            conn,
            user_id,
            expected=AccountStatus.ACTIVE,
            target=AccountStatus.DISABLED,
            now=NOW,
        )
        is True
    )
    # 第二次：调用方仍以为当前是 active，实际已经是 disabled → 不生效
    assert (
        store.set_status(
            conn,
            user_id,
            expected=AccountStatus.ACTIVE,
            target=AccountStatus.DISABLED,
            now=NOW,
        )
        is False
    )
    account = store.get_user(conn, user_id)
    assert account is not None
    assert account.status is AccountStatus.DISABLED


def test_failed_attempts_accumulate_in_sql(conn: sqlite3.Connection) -> None:
    """计数在 SQL 里 +1。读出来加一再写回去，在并发下会丢计数——
    而丢计数意味着阈值永远到不了，锁定形同虚设。"""
    tenant_id = make_tenant(conn)
    user_id = make_user(conn, tenant_id)

    counts = [store.record_failed_attempt(conn, user_id, now=NOW) for _ in range(3)]

    assert counts == [1, 2, 3]


def test_bumping_the_epoch_returns_the_new_version(conn: sqlite3.Connection) -> None:
    tenant_id = make_tenant(conn)
    user_id = make_user(conn, tenant_id)

    assert store.bump_credential_epoch(conn, user_id, now=NOW) == 2
    assert store.bump_credential_epoch(conn, user_id, now=NOW) == 3


# ── 拒绝记录的存法 ──


def test_deleting_a_user_keeps_their_denial_records(conn: sqlite3.Connection) -> None:
    """账户删了，拒绝记录还在。

    所以 `identity_events` **刻意没有指向 users 的外键**：加了 CASCADE 就等于
    把审计证据跟着被审计对象一起删掉，而那正是事后追查最需要它的时候。
    """
    tenant_id = make_tenant(conn)
    user_id = make_user(conn, tenant_id)
    store.insert_event(
        conn,
        IdentityEvent(
            event_id="ev-1",
            occurred_at=NOW,
            action="GET /api/conversations/{id}/messages",
            reason_code=ReasonCode.NOT_OWNER,
            actor_user_id=user_id,
            tenant_id=tenant_id,
            resource_kind="conversation",
            resource_id="c-other",
            request_id="req-1",
        ),
    )

    conn.execute("delete from users where id = ?", (user_id,))
    conn.commit()

    events = store.list_events(conn, tenant_id=tenant_id)
    assert [event.actor_user_id for event in events] == [user_id]


def test_events_are_filtered_by_tenant_time_and_reason(conn: sqlite3.Connection) -> None:
    """最小只读接口：范围与原因码都在 SQL 里，不做 Python 过滤。
    团队条件是必填参数——没有「不传就查全部」的重载。"""
    mine = make_tenant(conn, "我的团队")
    theirs = make_tenant(conn, "别的团队")

    def record(event_id: str, tenant: str, at: datetime, reason: ReasonCode) -> None:
        store.insert_event(
            conn,
            IdentityEvent(
                event_id=event_id,
                occurred_at=at,
                action="GET /api/runs/{id}/trace",
                reason_code=reason,
                tenant_id=tenant,
                request_id=event_id,
            ),
        )

    record("ev-1", mine, NOW, ReasonCode.NOT_OWNER)
    record("ev-2", mine, NOW + timedelta(minutes=5), ReasonCode.NOT_IN_TEAM)
    record("ev-3", theirs, NOW, ReasonCode.NOT_OWNER)
    conn.commit()

    only_mine = store.list_events(conn, tenant_id=mine)
    assert {event.event_id for event in only_mine} == {"ev-1", "ev-2"}

    narrowed = store.list_events(
        conn,
        tenant_id=mine,
        since=NOW + timedelta(minutes=1),
        reason_codes=(ReasonCode.NOT_IN_TEAM,),
    )
    assert [event.event_id for event in narrowed] == ["ev-2"]


def test_count_users_is_what_bootstrap_reads(conn: sqlite3.Connection) -> None:
    """引导的判据（`policy.can_bootstrap`）直接吃这个数，所以它必须**数全表**：
    引导发生在有团队之前，按团队数会恒为 0，于是后门永远开着。"""
    assert store.count_users(conn) == 0

    tenant_id = make_tenant(conn)
    make_user(conn, tenant_id, email="a@example.com")
    make_user(conn, tenant_id, email="b@example.com")

    assert store.count_users(conn) == 2


def test_stamp_round_trips_at_second_granularity() -> None:
    """存储格式是秒级、无时区后缀。往返只在整秒上相等——这一层不承诺更高精度。"""
    moment = datetime(2026, 11, 4, 9, 30, 15, tzinfo=UTC)

    text = store.stamp(moment)

    assert text == "2026-11-04T09:30:15"
    assert store.parse_stamp(text) == moment
    assert store.parse_stamp(None) is None
