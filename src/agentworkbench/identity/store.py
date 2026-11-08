"""持久化：`tenants` / `users` / `tickets` / `invites` / `identity_events` 五张表。

**放**：SQL。`conn` 是第一个参数，与 `db/repositories/` 里现有仓储同样式。
**不放**：任何「能不能」的判断——判定在 `policy`。这里的条件只表达
「这个 id 对应的行在不在这个团队里」，不表达「该不该给」。

归属过滤与读取写成**同一条语句**，不做「先查再判」两步：两步也能对，
但顺序一写反就是跨租户读取，而测试很可能是「先读后校验」——那时它依然全绿。
F5 的 `list_messages` 已经踩过这个形状。

改坏这里会怎样：把 `where tenant_id = ?` 挪到 Python 里做过滤，
用户就能用一串 id 试出「哪条真的存在」——响应一样是 404，但耗时不一样。

**两处刻意的「不给」**：

- `Account` 上**没有** `password_hash`，取它要走 `password_hash_for()`。
  这样任何把 `Account` 直接塞进 DTO 的写法都不可能顺手带出口令哈希——
  不是靠谁记得删字段，是那个字段根本不在对象上。
- 同理 `Ticket` 上**没有** `token_hash` 之外的明文：明文只在 cookie 里，
  库里、内存里、日志里都不存在第二份。

**时间**：库里是秒级 ISO 文本（与全项目一致），进出这一层才转 `datetime`。
转换放在这里而不是 `models`，是因为「秒级无时区后缀」是**存储格式**，
不是领域类型——领域层不该知道数据库用哪种字符串。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Final, cast
from uuid import uuid4

from agentworkbench.identity.models import (
    Account,
    AccountStatus,
    IdentityEvent,
    Invite,
    ReasonCode,
    Role,
    Tenant,
    Ticket,
)

__all__ = [
    "STAMP_FORMAT",
    "accept_invite",
    "bump_credential_epoch",
    "clear_failed_attempts",
    "count_users",
    "get_invite_by_hash",
    "get_tenant",
    "get_ticket_by_hash",
    "get_user",
    "get_user_by_email",
    "insert_event",
    "insert_invite",
    "insert_tenant",
    "insert_ticket",
    "insert_user",
    "list_events",
    "list_users",
    "new_id",
    "parse_stamp",
    "password_hash_for",
    "record_failed_attempt",
    "refresh_ticket_epoch",
    "revoke_ticket",
    "revoke_user_tickets",
    "set_level",
    "set_lockout",
    "set_must_change_password",
    "set_password_hash",
    "set_role",
    "set_status",
    "stamp",
]

STAMP_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S"
"""与 `core.clock.utc_now()` 同一格式（秒级、无时区后缀）。

秒级是**有损**的：`datetime` 进来、出去就只剩整秒。所以这一层不承诺往返相等，
只承诺「不比原来更晚」——而所有时间比较都是「现在 vs 某个时刻」，
整秒截断最多让边界条件差一秒，不会让顺序反转。
"""


def stamp(moment: datetime) -> str:
    """`datetime` → 入库文本。**统一转 UTC 再去掉时区**：库里只有一种时间基准，
    否则跨时区部署时「同一个瞬间」会有两种写法，比较就退化成字符串比较。"""
    return moment.astimezone(UTC).strftime(STAMP_FORMAT)


def parse_stamp(text: str | None) -> datetime | None:
    """入库文本 → `datetime`（UTC）。空值原样返回 None，不编一个默认时刻。"""
    if text is None:
        return None
    return datetime.strptime(text, STAMP_FORMAT).replace(tzinfo=UTC)


def new_id(prefix: str) -> str:
    """业务 id。带前缀是为了让日志里一眼看出是哪类对象——
    `t-3f2a…` 与 `u-3f2a…` 长得几乎一样，但排查时差别很大。"""
    return f"{prefix}-{uuid4().hex[:16]}"


def _normalize_email(email: str) -> str:
    """写入前归一化。列上还有 `COLLATE NOCASE` 兜底，这里是第二道：
    只靠列排序规则的写法，在「导出一份 CSV 再导回来」这种路径上会漏掉空白字符。"""
    return email.strip().casefold()


# ── 行 → 领域对象 ──
# 只映射需要的列，`select *` 不用：加一列时 `select *` 会让映射悄悄多拿一份
# 不该出现在对象上的东西（比如以后给 users 加的某个敏感列）。


def _account(row: sqlite3.Row) -> Account:
    return Account(
        user_id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        email=str(row["email"]),
        role=Role(row["role"]),
        status=AccountStatus(row["status"]),
        credential_epoch=int(row["credential_epoch"]),
        lockout_until=parse_stamp(row["lockout_until"]),
        failed_attempts=int(row["failed_attempts"]),
        is_platform_admin=bool(row["is_platform_admin"]),
        must_change_password=bool(row["must_change_password"]),
        level_rank=int(row["level_rank"]),
    )


def _ticket(row: sqlite3.Row) -> Ticket:
    return Ticket(
        ticket_id=str(row["id"]),
        user_id=str(row["user_id"]),
        token_hash=str(row["token_hash"]),
        credential_epoch=int(row["credential_epoch"]),
        issued_at=parse_stamp(row["issued_at"]) or datetime.min.replace(tzinfo=UTC),
        expires_at=parse_stamp(row["expires_at"]) or datetime.min.replace(tzinfo=UTC),
        revoked_at=parse_stamp(row["revoked_at"]),
    )


def _invite(row: sqlite3.Row) -> Invite:
    return Invite(
        invite_id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        role=Role(row["role"]),
        expires_at=parse_stamp(row["expires_at"]) or datetime.min.replace(tzinfo=UTC),
        email=None if row["email"] is None else str(row["email"]),
        accepted_at=parse_stamp(row["accepted_at"]),
    )


def _tenant(row: sqlite3.Row) -> Tenant:
    return Tenant(
        tenant_id=str(row["id"]),
        name=str(row["name"]),
        created_at=parse_stamp(row["created_at"]) or datetime.min.replace(tzinfo=UTC),
    )


def _event(row: sqlite3.Row) -> IdentityEvent:
    return IdentityEvent(
        event_id=str(row["event_id"]),
        occurred_at=parse_stamp(row["occurred_at"]) or datetime.min.replace(tzinfo=UTC),
        action=str(row["action"]),
        reason_code=ReasonCode(row["reason_code"]),
        actor_user_id=None if row["actor_user_id"] is None else str(row["actor_user_id"]),
        tenant_id=None if row["tenant_id"] is None else str(row["tenant_id"]),
        resource_kind=None if row["resource_kind"] is None else str(row["resource_kind"]),
        resource_id=None if row["resource_id"] is None else str(row["resource_id"]),
        request_id=None if row["request_id"] is None else str(row["request_id"]),
    )


# ── 团队 ──


def count_users(conn: sqlite3.Connection) -> int:
    """引导的唯一判据（`policy.can_bootstrap` 的输入）。**全表计数，不带团队**：
    引导发生在有团队之前。"""
    row = conn.execute("select count(*) as n from users").fetchone()
    return int(row["n"])


def insert_tenant(conn: sqlite3.Connection, *, name: str, now: datetime) -> str:
    tenant_id = new_id("t")
    conn.execute(
        "insert into tenants (id, name, created_at) values (?, ?, ?)",
        (tenant_id, name.strip(), stamp(now)),
    )
    return tenant_id


def get_tenant(conn: sqlite3.Connection, tenant_id: str) -> Tenant | None:
    row = conn.execute(
        "select id, name, created_at from tenants where id = ?", (tenant_id,)
    ).fetchone()
    return None if row is None else _tenant(row)


# ── 账户 ──


def insert_user(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    email: str,
    password_hash: str,
    role: Role,
    now: datetime,
    is_platform_admin: bool = False,
    must_change_password: bool = False,
    level_rank: int = 1,
) -> str:
    """建号。`credential_epoch` 从 1 起——0 与「没有版本」不可区分。

    `is_platform_admin` **默认 False，而这个默认值是一条判据**：忘了传就得到
    「不是平台所有者」；反过来默认 True 会让每一条建号路径（邀请建号、将来的
    批量导入）都悄悄多出一个能改全平台配置的人，而没有任何地方会报错。
    今天只有 `service.bootstrap` 传 True，且它只允许在空库上跑一次。

    `must_change_password` 同样默认 False：邀请建号的人自己设了口令，不该被
    再要求改一次；只有引导/管理员重置这条路径显式置 True。
    """
    user_id = new_id("u")
    moment = stamp(now)
    conn.execute(
        "insert into users"
        " (id, tenant_id, email, password_hash, role, status, credential_epoch,"
        "  lockout_until, failed_attempts, is_platform_admin, must_change_password,"
        "  level_rank, created_at, updated_at)"
        " values (?, ?, ?, ?, ?, 'active', 1, null, 0, ?, ?, ?, ?, ?)",
        (
            user_id,
            tenant_id,
            _normalize_email(email),
            password_hash,
            str(role),
            1 if is_platform_admin else 0,
            1 if must_change_password else 0,
            level_rank,
            moment,
            moment,
        ),
    )
    return user_id


def get_user(conn: sqlite3.Connection, user_id: str) -> Account | None:
    """按 id 取账户。**不带团队条件**——这是身份本身，不是团队内的资源：
    票据先指向它，之后才谈「这个人的团队是什么」。
    带上团队条件会让「票据有效但账户换了团队」变成 401，而那是另一回事。
    """
    row = conn.execute(
        "select id, tenant_id, email, role, status, credential_epoch,"
        " lockout_until, failed_attempts, is_platform_admin, must_change_password,"
        " level_rank from users where id = ?",
        (user_id,),
    ).fetchone()
    return None if row is None else _account(row)


def get_user_by_email(conn: sqlite3.Connection, email: str) -> Account | None:
    """登录用。邮箱全局唯一（见 `ux_users_email`），所以这里不需要团队上下文。"""
    row = conn.execute(
        "select id, tenant_id, email, role, status, credential_epoch,"
        " lockout_until, failed_attempts, is_platform_admin, must_change_password,"
        " level_rank from users where email = ? collate nocase",
        (_normalize_email(email),),
    ).fetchone()
    return None if row is None else _account(row)


def list_users(conn: sqlite3.Connection, tenant_id: str) -> list[Account]:
    """成员列表。团队条件在 SQL 里，不做 Python 过滤。"""
    rows = conn.execute(
        "select id, tenant_id, email, role, status, credential_epoch,"
        " lockout_until, failed_attempts, is_platform_admin, must_change_password,"
        " level_rank from users where tenant_id = ? order by created_at, id",
        (tenant_id,),
    ).fetchall()
    return [_account(row) for row in rows]


def password_hash_for(conn: sqlite3.Connection, user_id: str) -> str | None:
    """取口令哈希。**单独一个函数而不是放进 `Account`**：那个对象会到处传，
    哈希混进去之后，某天一个 `asdict()` 就把它带进了日志或响应。"""
    row = conn.execute("select password_hash from users where id = ?", (user_id,)).fetchone()
    return None if row is None else str(row["password_hash"])


def set_password_hash(
    conn: sqlite3.Connection, user_id: str, password_hash: str, *, now: datetime
) -> None:
    """只换哈希，**不动 `credential_epoch`**——递增版本是另一个动作
    （`bump_credential_epoch`），因为「改密要踢别的会话」与「重哈希同一个口令」
    是两件事：参数调高触发的平滑重哈希不该把用户踢下线。
    """
    conn.execute(
        "update users set password_hash = ?, updated_at = ? where id = ?",
        (password_hash, stamp(now), user_id),
    )


def set_must_change_password(
    conn: sqlite3.Connection, user_id: str, *, required: bool, now: datetime
) -> bool:
    """设置/清除「首次登录必须改密」。返回是否真的改到了行。

    它和 `credential_epoch` 是两件事：版本号负责让旧票据失效，这一列负责
    告诉请求侧“业务面还没开”。混成一个值时，管理员重置会把版本号加一，
    于是“还没改初始口令”这个事实被无关字段顺手抹掉。
    """
    cursor = conn.execute(
        "update users set must_change_password = ?, updated_at = ? where id = ?",
        (1 if required else 0, stamp(now), user_id),
    )
    return cursor.rowcount == 1


def set_status(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    expected: AccountStatus,
    target: AccountStatus,
    now: datetime,
) -> bool:
    """停用 / 恢复。**带 expected 的 CAS**：读到的状态与写时的状态不一致就什么都不做。

    不加 `expected` 的话，两个管理员同时点「停用 / 恢复」，后到的那个会静默覆盖，
    而两条操作在界面上都显示成功。返回 False 让调用方有机会报冲突，而不是假装成功。
    """
    cursor = conn.execute(
        "update users set status = ?, updated_at = ? where id = ? and status = ?",
        (str(target), stamp(now), user_id, str(expected)),
    )
    return cursor.rowcount == 1


def set_role(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    expected: Role,
    target: Role,
    now: datetime,
) -> bool:
    """改角色。**带 `expected` 的 CAS**，理由与 `set_status` 相同：
    两个管理员同时改同一个人的角色，后到的静默覆盖会让前一个人以为生效了。

    改角色**不动 `credential_epoch`**：角色每次请求都从 `users` 表读，
    不需要靠版本号让谁失效。涨版本会把这个人所有设备踢下线——
    一次「升职」不该有这样的副作用。
    """
    cursor = conn.execute(
        "update users set role = ?, updated_at = ? where id = ? and role = ?",
        (str(target), stamp(now), user_id, str(expected)),
    )
    return cursor.rowcount == 1


def set_level(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    expected: int,
    target: int,
    now: datetime,
) -> bool:
    """改等级。**带 expected 的 CAS**：并发后台管理员各自提交时，后到的不能
    静默覆盖前一条。等级变更不需要吊销票据，但调用方必须清会话缓存。"""
    cursor = conn.execute(
        "update users set level_rank = ?, updated_at = ? where id = ? and level_rank = ?",
        (target, stamp(now), user_id, expected),
    )
    return cursor.rowcount == 1


def bump_credential_epoch(conn: sqlite3.Connection, user_id: str, *, now: datetime) -> int:
    """版本号 +1，返回新版本。**靠比对让旧票据失效，不靠遍历删除**：
    批量删是一次写操作，漏一条就留一条后门；版本比较漏不掉。"""
    row = conn.execute(
        "update users set credential_epoch = credential_epoch + 1, updated_at = ?"
        " where id = ? returning credential_epoch",
        (stamp(now), user_id),
    ).fetchone()
    if row is None:
        raise LookupError(f"账户不存在：{user_id}")
    return int(row["credential_epoch"])


def record_failed_attempt(conn: sqlite3.Connection, user_id: str, *, now: datetime) -> int:
    """失败计数 +1，返回累计值。**在 SQL 里 +1**，因为「读出来加一再写回去」
    在并发下会丢计数——而丢计数意味着阈值永远到不了。"""
    row = conn.execute(
        "update users set failed_attempts = failed_attempts + 1, updated_at = ?"
        " where id = ? returning failed_attempts",
        (stamp(now), user_id),
    ).fetchone()
    if row is None:
        raise LookupError(f"账户不存在：{user_id}")
    return int(row["failed_attempts"])


def clear_failed_attempts(conn: sqlite3.Connection, user_id: str, *, now: datetime) -> None:
    conn.execute(
        "update users set failed_attempts = 0, updated_at = ? where id = ?",
        (stamp(now), user_id),
    )


def set_lockout(
    conn: sqlite3.Connection, user_id: str, until: datetime | None, *, now: datetime
) -> None:
    """写锁定到期时刻。`None` 表示解除——到期本来靠时间比较自动生效，
    这一列只在「管理员提前解锁」时才需要被清空。"""
    conn.execute(
        "update users set lockout_until = ?, updated_at = ? where id = ?",
        (None if until is None else stamp(until), stamp(now), user_id),
    )


# ── 票据 ──


def insert_ticket(conn: sqlite3.Connection, ticket: Ticket) -> None:
    conn.execute(
        "insert into tickets"
        " (id, user_id, token_hash, credential_epoch, issued_at, expires_at, revoked_at)"
        " values (?, ?, ?, ?, ?, ?, ?)",
        (
            ticket.ticket_id,
            ticket.user_id,
            ticket.token_hash,
            ticket.credential_epoch,
            stamp(ticket.issued_at),
            stamp(ticket.expires_at),
            None if ticket.revoked_at is None else stamp(ticket.revoked_at),
        ),
    )


def get_ticket_by_hash(conn: sqlite3.Connection, token_hash: str) -> Ticket | None:
    """按哈希取票据。cookie 里是明文，进来先哈希——**库里没有明文可比对**。"""
    row = conn.execute(
        "select id, user_id, token_hash, credential_epoch, issued_at, expires_at,"
        " revoked_at from tickets where token_hash = ?",
        (token_hash,),
    ).fetchone()
    return None if row is None else _ticket(row)


def revoke_ticket(conn: sqlite3.Connection, ticket_id: str, *, now: datetime) -> bool:
    """吊销一张票。已经吊销过就返回 False——重复登出不该被当成成功。"""
    cursor = conn.execute(
        "update tickets set revoked_at = ? where id = ? and revoked_at is null",
        (stamp(now), ticket_id),
    )
    return cursor.rowcount == 1


def refresh_ticket_epoch(conn: sqlite3.Connection, ticket_id: str, *, epoch: int) -> bool:
    """把一张票搬到新一代凭据上。**只为「发起改密的那一条」存在。**

    没有它，`revoke_user_tickets(keep_ticket_id=...)` 是个半成品：
    那张票的 `credential_epoch` 还停在旧代，而 `judge_access` 的第 ① 步
    比的正是这个整数——于是「保留」只保留了行，下一次请求照样 401。
    现象对用户是「改完密码就被踢下线」，正好是 `I-V9` 反对的那个结果，
    只不过原因藏在另一层。

    `and revoked_at is null` 是必要的：一张已经被吊销的票不该被复活
    （吊销是永久的，恢复登录要重新签发）。

    返回是否真的改到了行。False 意味着那张票在事务里已经不可用，
    调用方应当按「这次改密的会话已失效」处理，而不是假装保留成功。
    """
    cursor = conn.execute(
        "update tickets set credential_epoch = ? where id = ? and revoked_at is null",
        (epoch, ticket_id),
    )
    return cursor.rowcount == 1


def revoke_user_tickets(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    now: datetime,
    keep_ticket_id: str | None = None,
) -> int:
    """吊销该用户的全部票据，**可以保留一张**。

    `keep_ticket_id` 就是「改密时保留发起改密的那条」（REQ-AUTH-09）：不保留的话，
    用户改完密码立刻被踢下线，第一反应是「系统坏了」，然后去找管理员——
    而那正是刚刚发生的事导致的。

    返回吊销了几条，供日志用（**只记数量，不记票据 id**）。
    """
    sql = "update tickets set revoked_at = ? where user_id = ? and revoked_at is null"
    params: list[object] = [stamp(now), user_id]
    if keep_ticket_id is not None:
        sql += " and id <> ?"
        params.append(keep_ticket_id)
    cursor = conn.execute(sql, params)
    return cursor.rowcount


# ── 邀请 ──


def insert_invite(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    role: Role,
    token_hash: str,
    expires_at: datetime,
    created_by: str,
    now: datetime,
    email: str | None = None,
) -> str:
    invite_id = new_id("i")
    conn.execute(
        "insert into invites"
        " (id, tenant_id, email, role, token_hash, expires_at, accepted_at,"
        "  accepted_by, created_by, created_at)"
        " values (?, ?, ?, ?, ?, ?, null, null, ?, ?)",
        (
            invite_id,
            tenant_id,
            None if email is None else _normalize_email(email),
            str(role),
            token_hash,
            stamp(expires_at),
            created_by,
            stamp(now),
        ),
    )
    return invite_id


def get_invite_by_hash(conn: sqlite3.Connection, token_hash: str) -> Invite | None:
    row = conn.execute(
        "select id, tenant_id, email, role, expires_at, accepted_at from invites"
        " where token_hash = ?",
        (token_hash,),
    ).fetchone()
    return None if row is None else _invite(row)


def get_invite(conn: sqlite3.Connection, invite_id: str, *, tenant_id: str) -> Invite | None:
    row = conn.execute(
        "select id, tenant_id, email, role, expires_at, accepted_at from invites"
        " where id = ? and tenant_id = ?",
        (invite_id, tenant_id),
    ).fetchone()
    return None if row is None else _invite(row)


def list_pending_invites(
    conn: sqlite3.Connection, *, tenant_id: str, now: datetime
) -> list[sqlite3.Row]:
    """????????????????????? token_hash?"""
    return cast(
        "list[sqlite3.Row]",
        conn.execute(
            "select id, email, role, expires_at, created_at from invites"
            " where tenant_id = ? and accepted_at is null and expires_at > ?"
            " order by created_at desc, id desc",
            (tenant_id, stamp(now)),
        ).fetchall(),
    )


def revoke_invite(
    conn: sqlite3.Connection, invite_id: str, *, tenant_id: str, now: datetime
) -> bool:
    """?????????????

    ?? `accepted_at` ????????????????????????
    ??????????????????????????? admin_events?
    """
    cursor = conn.execute(
        "update invites set accepted_at = ?, accepted_by = null"
        " where id = ? and tenant_id = ? and accepted_at is null",
        (stamp(now), invite_id, tenant_id),
    )
    return cursor.rowcount == 1


def accept_invite(
    conn: sqlite3.Connection, invite_id: str, *, accepted_by: str, now: datetime
) -> bool:
    """标记邀请已用。**原子守卫写在 SQL 里**（`and accepted_at is null`）：
    「先查再用」的写法在并发下两次都会成功，而两次成功意味着同一张邀请建出两个号，
    且没有任何东西会报错——第二个人的账号从此没有可追溯的来源。
    """
    cursor = conn.execute(
        "update invites set accepted_at = ?, accepted_by = ? where id = ? and accepted_at is null",
        (stamp(now), accepted_by, invite_id),
    )
    return cursor.rowcount == 1


def burn_invite(conn: sqlite3.Connection, invite_id: str, *, now: datetime) -> bool:
    """作废一张邀请：**没人用它建号，但也不能再用**。

    唯一的调用点是「接受时邮箱已被注册」。为什么必须烧掉而不是放着：不烧的话
    `400 已注册` 这条失败路径**不消耗令牌**，于是一张邀请可以无限次问
    「这个邮箱注册过吗」——红队把它当全局邮箱枚举器用（D9 R-03）。

    `accepted_by` 留空是刻意的：这张邀请没有任何人接受过，把它记成某人接受过
    会让审计回答一个错误的问题。烧掉的动作仍然原子（`accepted_at is null`）。
    """
    cursor = conn.execute(
        "update invites set accepted_at = ? where id = ? and accepted_at is null",
        (stamp(now), invite_id),
    )
    return cursor.rowcount == 1


# ── 拒绝记录 ──


def insert_event(conn: sqlite3.Connection, event: IdentityEvent) -> None:
    """**只做 INSERT**，不改任何业务表。所有拒绝出口都调它，写失败由调用方
    翻译成 500（不许返回 404——那会变成一条没有证据的「不存在」）。
    """
    conn.execute(
        "insert into identity_events"
        " (event_id, occurred_at, actor_user_id, tenant_id, action, resource_kind,"
        "  resource_id, reason_code, request_id)"
        " values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event.event_id,
            stamp(event.occurred_at),
            event.actor_user_id,
            event.tenant_id,
            event.action,
            event.resource_kind,
            event.resource_id,
            str(event.reason_code),
            event.request_id,
        ),
    )


def list_events(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
    reason_codes: tuple[ReasonCode, ...] = (),
    limit: int = 200,
) -> list[IdentityEvent]:
    """最小只读接口（F1 §1.3）：按时间范围与原因码列本团队的拒绝记录。

    **不做聚合、不做趋势、不做跨团队视图**——那是团队级审计，§2.1 明确不做。
    未来要加，先回答「管理员能否读全员操作历史」这个新问题。

    团队条件在 SQL 里，且 `tenant_id` 是必填——没有「不传就查全部」的重载。
    """
    sql = (
        "select event_id, occurred_at, actor_user_id, tenant_id, action,"
        " resource_kind, resource_id, reason_code, request_id"
        " from identity_events where tenant_id = ?"
    )
    params: list[object] = [tenant_id]
    if since is not None:
        sql += " and occurred_at >= ?"
        params.append(stamp(since))
    if until is not None:
        sql += " and occurred_at < ?"
        params.append(stamp(until))
    if reason_codes:
        placeholders = ", ".join("?" for _ in reason_codes)
        sql += f" and reason_code in ({placeholders})"
        params.extend(str(code) for code in reason_codes)
    # 自增 id 兜底：同一秒内的多条记录靠它保持插入顺序，否则回放会换位
    sql += " order by occurred_at desc, id desc limit ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [_event(row) for row in rows]
