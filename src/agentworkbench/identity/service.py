"""编排：一次登录 / 改密 / 停用 / 邀请 / 引导的步骤顺序。

**放**：四步判序的调用、失败计数、签发票据、吊销、事务边界。
**不放**：SQL（在 `store`）、哈希算法（在 `credentials`）、判定规则（在 `policy`）。

「改状态与吊销票据在同一个事务里」这句是这里的责任。拆开就会出现
「密码已经改了，旧会话还能用一整个窗口」的缝——而这一整个窗口里，
所有测试都是绿的，因为每一半单独看都对。事务边界因此写在每个写函数里
（`with conn:`），而不是交给调用方：调用方一旦忘了 commit，写进去的东西
在下一次连接关闭时静默消失，而所有断言都在同一个连接里做，看不出来。

改坏这里会怎样：把吊销放到改密事务之后单独提交，用户改完密码发现另一台设备
还登着，而「改密会踢掉其他设备」是他改密的全部理由。

**两处文档与判据的冲突，取法一致：判据优先于表格。**

1. §1.7 登录流程的最后一步写「记一条成功登录」，但 `I-V4` 的判据是
   「同一时刻一次正常的 200 不产生任何记录」，而 §1.3 整节叫「拒绝记录」。
   两者只能同时成立在一种读法下：成功登录进访问日志，不进 `identity_events`。
   取 `I-V4`——它可执行；而且一旦成功也写，`identity_events` 的语义就从
   「被拒的人」变成「所有人」，`I-H3` 的对照实验（不查记录时答不出来）也就失效了。
2. §1.6 第 ② 步列了 `account_locked_out`，但 §1.4 与 `I-V8` 要求锁定不踢会话。
   分界在**路径**：受保护请求只判 `status`，锁定只在登录路径上判。
   理由写在 `policy.login_block_reason` 的 docstring 里。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from agentworkbench.config import AuthSettings, Settings
from agentworkbench.identity import credentials, policy, store
from agentworkbench.identity.models import (
    Account,
    AccountStatus,
    ReasonCode,
    Role,
    Ticket,
)

__all__ = [
    "INVITE_TTL_HOURS",
    "AcceptedInvite",
    "AuthConfig",
    "BootstrapClosed",
    "BootstrapResult",
    "EmailAlreadyTaken",
    "InviteUnusable",
    "LoginRejected",
    "LoginResult",
    "LoginSucceeded",
    "PasswordUnchanged",
    "accept_invite",
    "bootstrap",
    "change_password",
    "change_role",
    "create_invite",
    "disable_user",
    "enable_user",
    "login",
    "logout",
    "reset_password",
]

INVITE_TTL_HOURS: Final[int] = 72
"""邀请有效期。**刻意不做成配置键**：§1.8 的五个键每一个都有 REQ 要求可配，
邀请有效期没有。多一个没人验收的旋钮，等于多一条「配了不生效」的路径。
需要它可配时，先补 REQ 与判据（见 §四「什么条件下重开」）。"""

_DEFAULTS: Final[AuthSettings] = AuthSettings()
"""`Settings.auth` 的默认实例。只用来给 `AuthConfig` 的字段取默认值，
不参与任何运行时判断——真正生效的值永远来自 `AuthConfig.from_settings`。"""


@dataclass(frozen=True)
class AuthConfig:
    """`Settings.auth` 的只读视图。**服务层拿这个，不拿 `Settings`。**

    多一层不是为了好看：直接拿 `Settings` 的服务函数在测试里必须造一个完整的
    配置对象（数据目录、密钥、阈值全要填），而那些值与本函数无关。
    传 `AuthConfig` 之后，一条测试只需要写它真的在乎的那两个数。

    它仍然是「从配置来」的：`from_settings` 是唯一的生产构造入口，
    测试里手写 `AuthConfig(...)` 等于声明「这条测的是行为不是装配」。

    默认值从 `AuthSettings()` 派生，**不抄第二份数字**：抄一份的后果是
    「文档说 5 次锁定、代码里测试用的是 3 次、生产的默认值是 5」，
    三方各自自洽，而没人发现测试根本没覆盖生产的那条路径。
    """

    login_max_failures: int = _DEFAULTS.login_max_failures
    lockout_seconds: int = _DEFAULTS.lockout_seconds
    ticket_ttl_hours: int = _DEFAULTS.ticket_ttl_hours
    session_invalidate_seconds: int = _DEFAULTS.session_invalidate_seconds
    cookie_secure: bool = _DEFAULTS.cookie_secure

    def __post_init__(self) -> None:
        # 四个必须为正：配成 0 的后果都是「功能悄悄消失」而不是报错。
        for name in ("login_max_failures", "lockout_seconds", "ticket_ttl_hours"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须为正：0 会让对应的保护静默失效")
        if self.session_invalidate_seconds < 0:
            raise ValueError("session_invalidate_seconds 不能为负：0 表示不缓存")

    @classmethod
    def from_settings(cls, settings: Settings) -> AuthConfig:
        auth = settings.auth
        return cls(
            login_max_failures=auth.login_max_failures,
            lockout_seconds=auth.lockout_seconds,
            ticket_ttl_hours=auth.ticket_ttl_hours,
            session_invalidate_seconds=auth.session_invalidate_seconds,
            cookie_secure=auth.cookie_secure,
        )


# ── 登录 ──


@dataclass(frozen=True)
class LoginSucceeded:
    """登录成功。`ticket` 是**明文**，只在调用方设置 cookie 的那一刻存在。"""

    ticket: str
    account: Account


@dataclass(frozen=True)
class LoginRejected:
    """登录失败。`reason` **只进记录，不进响应**（§1.3）。

    `actor_user_id` 有值表示「账号找到了、是这个人的凭据没过」，为空表示
    「根本没找到这个账号」。这个区别只允许出现在记录里：对外文案、状态码、
    耗时三者必须一致，否则响应本身就是一台账号枚举器（`I-H2`）。

    不放 `account`：一旦这个对象带着完整账户被返回，某个调用点就会拿
    `rejected.account.status` 去拼一句「您的账号已被停用」——那句话是
    管理员停用与系统锁定的唯一区别，也正是 `I-V6` 要捂住的东西。
    """

    reason: ReasonCode
    actor_user_id: str | None = None


LoginResult = LoginSucceeded | LoginRejected


def login(
    conn: sqlite3.Connection,
    *,
    email: str,
    password: str,
    now: datetime,
    config: AuthConfig,
) -> LoginResult:
    """一次登录。**四步的顺序就是这个函数的顺序**（§1.7）。

    关键的写法是「先跑哈希，再分支」：账号不存在、已停用、锁定中、口令错，
    四条路都必须走完一次 argon2，耗时才有可比性。写成

        if account is None: return rejected()   # ← 提前返回
        matched, _ = verify_password(...)

    功能上完全正确，测试也全绿，但「不存在」比「口令错」快几十毫秒——
    拿一份邮箱列表就能把注册过的账号全筛出来，而且不需要任何额外接口。

    失败计数与锁定也在这里：口令错 → 计数 +1 → 达阈值写 `lockout_until`
    并把计数归零（否则解锁后第一次再错就又被锁，等于永久锁死）。
    """
    account = store.get_user_by_email(conn, email)
    hashed = None if account is None else store.password_hash_for(conn, account.user_id)
    # 纯内存判断，不碰库：放在哈希之前是为了让三条拒绝路径共用同一次哈希。
    blocked = policy.login_block_reason(account, now)
    matched, rehashed = credentials.verify_password(password, hashed)

    if account is None:
        return LoginRejected(reason=ReasonCode.UNAUTHENTICATED)

    if blocked is not None:
        # 停用 / 锁定。**不增加失败计数**：账号已经不是「口令对不对」的问题，
        # 再叠加计数会让管理员解锁之后仍然要等一轮倒计时。
        return LoginRejected(reason=blocked, actor_user_id=account.user_id)

    if not matched:
        with conn:
            attempts = store.record_failed_attempt(conn, account.user_id, now=now)
            if attempts >= config.login_max_failures:
                store.set_lockout(
                    conn,
                    account.user_id,
                    now + timedelta(seconds=config.lockout_seconds),
                    now=now,
                )
                store.clear_failed_attempts(conn, account.user_id, now=now)
        return LoginRejected(reason=ReasonCode.UNAUTHENTICATED, actor_user_id=account.user_id)

    with conn:
        store.clear_failed_attempts(conn, account.user_id, now=now)
        if account.lockout_until is not None:
            # 锁定期已过（`login_block_reason` 放行了才走到这里）。清掉那一列
            # 不是必须的——到期本来就靠时间比较自愈——但留着会让「上一次被锁过」
            # 一直显示在管理界面上，而它已经不生效了。
            store.set_lockout(conn, account.user_id, None, now=now)
        if rehashed is not None:
            # 参数调高之后的平滑重哈希：只换哈希，**不动版本号**，
            # 否则每次登录都会把自己别的设备踢下线。
            store.set_password_hash(conn, account.user_id, rehashed, now=now)
        ticket = _issue_ticket(conn, account, now=now, config=config)
    return LoginSucceeded(ticket=ticket, account=account)


def _issue_ticket(
    conn: sqlite3.Connection,
    account: Account,
    *,
    now: datetime,
    config: AuthConfig,
) -> str:
    """签一张票，返回**明文**。库里的行只有 SHA-256。

    `credential_epoch` 取账户当前值：这张票只在当前这一代凭据下有效，
    改密之后它自动作废——不需要有人记得去删它。
    """
    plain = credentials.issue_ticket()
    store.insert_ticket(
        conn,
        Ticket(
            ticket_id=store.new_id("tk"),
            user_id=account.user_id,
            token_hash=credentials.hash_ticket(plain),
            credential_epoch=account.credential_epoch,
            issued_at=now,
            expires_at=now + timedelta(hours=config.ticket_ttl_hours),
        ),
    )
    return plain


def logout(conn: sqlite3.Connection, *, ticket: str, now: datetime) -> bool:
    """登出：把库里的那一行标记为已吊销，返回是否真的改到了行。

    **不只是让浏览器丢掉 cookie**：那样旧票据在过期前一直是有效凭证，
    而「登出」对用户的承诺是「这台机器上别人拿不到了」（`I-V14`）。

    票据不存在时返回 False 而不是抛：登出是幂等入口，调用方一律回 204。
    """
    row = store.get_ticket_by_hash(conn, credentials.hash_ticket(ticket))
    if row is None:
        return False
    with conn:
        return store.revoke_ticket(conn, row.ticket_id, now=now)


# ── 凭据变更 ──


class PasswordUnchanged(ValueError):
    """新口令与旧口令相同。

    它不是“旧口令错”，所以不能返回 False（调用方会把它翻成 401，用户会以为
    自己输错了旧口令）；也不是服务故障。执行“必须改密”时，这一步是判据本身：
    原样提交等于没改。
    """


def change_password(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    current_ticket: str,
    old_password: str,
    new_password: str,
    now: datetime,
) -> bool:
    """本人改密。返回旧口令是否正确。

    三件事必须在**同一个事务**里：换哈希、版本 +1、吊销其余票据。
    少任何一件都留一条缝：

    - 只换哈希 → 泄露的旧会话照旧可用，「改密码」等于没改（`I-V9` 的反例）；
    - 涨版本但不吊销 → 靠整数比较也能失效，但库里留着一批永远不会再用的行，
      而「哪些会话还有效」从此要靠推断；
    - 吊销全部（含当前这条）→ 用户改完密码立刻被踢下线，第一反应是
      「系统坏了」，然后去找管理员——而那正是刚刚发生的事导致的。

    保留的那张票要**升到新一代**（`refresh_ticket_epoch`）：保留行但留着旧版本号，
    下一次请求仍然会被版本比较拒掉，等于没保留。这一步是 `I-V9` 的真正难点。
    """
    hashed = store.password_hash_for(conn, user_id)
    matched, _ = credentials.verify_password(old_password, hashed)
    if not matched or hashed is None:
        return False
    if old_password == new_password:
        raise PasswordUnchanged("新口令不能与当前口令相同")

    keep = store.get_ticket_by_hash(conn, credentials.hash_ticket(current_ticket))
    keep_id = keep.ticket_id if keep is not None and keep.user_id == user_id else None
    new_hash = credentials.hash_password(new_password)

    with conn:
        store.set_password_hash(conn, user_id, new_hash, now=now)
        store.set_must_change_password(conn, user_id, required=False, now=now)
        epoch = store.bump_credential_epoch(conn, user_id, now=now)
        store.revoke_user_tickets(conn, user_id, now=now, keep_ticket_id=keep_id)
        if keep_id is not None and not store.refresh_ticket_epoch(conn, keep_id, epoch=epoch):
            # 事务里那张票已被吊销（并发登出）。保留失败不是错误，
            # 但也不能假装成功：调用方按「这次会话已失效」处理即可。
            return True
    return True


def reset_password(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    new_password: str,
    now: datetime,
    must_change_password: bool = True,
) -> None:
    """管理员重置口令。**吊销全部票据，一张不留。**

    与本人改密的差别就在这一点：管理员重置的前提通常是「这个人进不去了」
    或「凭据可能已泄露」。前者没有「当前会话」可保留，后者保留哪一条都是在赌。
    用户重置后必须重新登录，这是重置的语义。

    默认把“登录后必须改密”置 1：运维知道这个临时口令，用户不该长期沿用。
    测试/红队夹具要模拟“已经轮换过”的账号时才传 False——那是显式的夹具动作，
    不是生产默认。
    """
    new_hash = credentials.hash_password(new_password)
    with conn:
        store.set_password_hash(conn, user_id, new_hash, now=now)
        # 重置后的口令同样是一次性的：运维知道它，用户必须在下次登录后换掉。
        store.set_must_change_password(conn, user_id, required=must_change_password, now=now)
        store.bump_credential_epoch(conn, user_id, now=now)
        store.revoke_user_tickets(conn, user_id, now=now)


# ── 停用与恢复 ──


def disable_user(conn: sqlite3.Connection, *, user_id: str, now: datetime) -> bool:
    """停用：置 `disabled` 并吊销该用户全部票据，同一事务。返回是否真的改了。

    **不带 `expected` 的写法会静默成功**：两个管理员同时点「停用」，
    后到的那个覆盖前一个，两个人都看到成功。这里用 CAS，
    第二次返回 False，调用方可以报「状态已变更」。

    「不许停用自己」不在这里判——那是权限边界，在 `policy.judge_management`。
    放在这里判的话，绕过它的方式就是「直接调 service」，而 service 是
    给路由用的内部接口，不是第二个入口。
    """
    with conn:
        changed = store.set_status(
            conn,
            user_id,
            expected=AccountStatus.ACTIVE,
            target=AccountStatus.DISABLED,
            now=now,
        )
        if not changed:
            return False
        store.revoke_user_tickets(conn, user_id, now=now)
    return True


def enable_user(conn: sqlite3.Connection, *, user_id: str, now: datetime) -> bool:
    """恢复：只把 `status` 置回 `active`，**不碰 `lockout_until`**。

    两列各自恢复、互不影响（`I-V7`）。这里顺手解掉锁定看起来更「贴心」，
    代价是「管理员恢复账号」变成一次隐式的解锁——而锁定是系统的判断，
    管理员并不知道它为什么发生，随手清掉等于绕过失败计数。
    要提前解锁是一个单独的动作，应当有它自己的按钮与记录。

    旧票据不会因为恢复而复活：吊销是永久的，恢复之后要重新登录。
    这条是刻意的——「停用期间签发的会话」根本不存在，而「停用前签发的」
    正是管理员想断掉的那批。
    """
    with conn:
        return store.set_status(
            conn,
            user_id,
            expected=AccountStatus.DISABLED,
            target=AccountStatus.ACTIVE,
            now=now,
        )


def change_role(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    expected: Role,
    target: Role,
    now: datetime,
) -> bool:
    """改角色。返回是否真的改了。

    `expected` 由调用方从「刚读到的那一行」传进来，不是从请求体——
    请求体里只有目标角色，拿它当 expected 的话，这次 CAS 永远成立，
    等于没有 CAS。

    **不动票据、不动版本号**：角色在每次请求时从 `users` 表读
    （`judge_access` 构造 `Principal` 时就地取），所以下一条请求立刻生效；
    涨版本会把这个人所有设备踢下线，而「升职」不该有这样的副作用。
    降职同理——但那一条要有人想清楚「他正开着管理页面时被降职」会怎样：
    答案是下一个请求 403，页面上残留的按钮点一次报一次错。
    """
    with conn:
        return store.set_role(conn, user_id, expected=expected, target=target, now=now)


# ── 邀请 ──


class InviteUnusable(Exception):
    """邀请不存在 / 已用 / 已过期 / 与定向邮箱不符。

    **四种原因共用一个异常**，因为它们对外必须是同一个 404、同一段文案
    （`I-V13`）。分开成四个类型之后，api 层迟早会为其中一个写一句更具体的提示，
    而那句话就是在说「这张邀请曾经存在」。
    """


class EmailAlreadyTaken(Exception):
    """建号时邮箱已被占用。与 `InviteUnusable` 不同：这一条**可以**告诉用户，
    因为他填的是自己的邮箱，知道「已注册」不泄露任何别人的信息。"""


@dataclass(frozen=True)
class AcceptedInvite:
    """邀请建号的结果。建完即登录，所以带一张新票据。"""

    account: Account
    ticket: str


def create_invite(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    role: Role,
    created_by: str,
    now: datetime,
    email: str | None = None,
    ttl_hours: int = INVITE_TTL_HOURS,
) -> tuple[str, str]:
    """签一张一次性邀请，返回 `(邀请 id, 明文)`。

    明文只出现在返回值和邀请链接里，库里是它的 SHA-256——与票据同一套做法。
    这样「备份泄露」不等于「能加进团队」，而后者是比读数据更难察觉的事：
    新成员进来之后，团队里的一切对他都是正常可见的。
    """
    plain = credentials.issue_ticket()
    with conn:
        invite_id = store.insert_invite(
            conn,
            tenant_id=tenant_id,
            role=role,
            token_hash=credentials.hash_ticket(plain),
            expires_at=now + timedelta(hours=ttl_hours),
            created_by=created_by,
            now=now,
            email=email,
        )
    return invite_id, plain


def accept_invite(
    conn: sqlite3.Connection,
    *,
    token: str,
    email: str,
    password: str,
    now: datetime,
    config: AuthConfig,
) -> AcceptedInvite:
    """凭邀请建号，落在**邀请者的团队**里，然后直接登录。

    顺序是判据（`I-V13`）：**先判过期与已用，再建号**。反过来写（先建号再判）
    会在过期邀请上先造出一个账号再报错——那个人从此存在，但没有任何入口，
    而下一次有人用同一张邀请时，唯一约束会在邮箱上炸，报的却是一个看不懂的错。

    建号与「标记邀请已用」同事务：`accept_invite` 的原子守卫写在 SQL 里
    （`and accepted_at is null`），并发的第二次会拿到 0 行 → 整个事务回滚 →
    那个被抢先建出来的账号也一起消失。没有这一步，同一张邀请能建出两个账号，
    第二个人从此没有来源可追溯。
    """
    row = store.get_invite_by_hash(conn, credentials.hash_ticket(token))
    if row is None:
        raise InviteUnusable("邀请不存在")
    if row.accepted_at is not None:
        raise InviteUnusable("邀请已使用")
    if row.expires_at <= now:
        raise InviteUnusable("邀请已过期")
    if row.email is not None and row.email != email.strip().casefold():
        raise InviteUnusable("邀请不属于这个邮箱")

    if store.get_user_by_email(conn, email) is not None:
        # **先把这张邀请烧掉，再报错。** 不烧的话「已注册」这条失败路径不消耗令牌，
        # 一张邀请就能无限次问「这个邮箱注册过吗」——D9 红队把它当跨团队的邮箱
        # 枚举器用（`redteam-A.md` R-03 / `redteam-B.md` R-02）。烧掉之后
        # 一张邀请最多买到一个答案，探测成本从「两次请求」变成「一张邀请」。
        #
        # 仍然泄漏的那一半写在这里：**令牌持有者这一次**依然知道该邮箱已注册。
        # 这是全局唯一邮箱 + 允许凭邀请自建号这两条设计合起来的固有结果，
        # 不是这一行能消掉的；要彻底消掉得让邮箱按团队唯一（登录方式随之改变）。
        with conn:
            store.burn_invite(conn, row.invite_id, now=now)
        raise EmailAlreadyTaken(email)

    password_hash = credentials.hash_password(password)
    try:
        with conn:
            user_id = store.insert_user(
                conn,
                tenant_id=row.tenant_id,
                email=email,
                password_hash=password_hash,
                role=row.role,
                now=now,
            )
            if not store.accept_invite(conn, row.invite_id, accepted_by=user_id, now=now):
                # 并发的另一次已经用掉了这张邀请。抛出去让事务回滚，
                # 外面翻译成与「不存在」完全相同的 404。
                raise InviteUnusable("邀请已被并发使用")
            account = store.get_user(conn, user_id)
            if account is None:  # pragma: no cover —— 同一事务里刚写进去的行
                raise RuntimeError(f"建号后读不回账户：{user_id}")
            ticket = _issue_ticket(conn, account, now=now, config=config)
    except sqlite3.IntegrityError as exc:
        # 预检查与写入之间可能有另一个人抢先注册同一个邮箱。约束是最后一道，
        # 翻译成同一个异常，而不是把 sqlite 的报错原样漏给路由。
        raise EmailAlreadyTaken(email) from exc
    return AcceptedInvite(account=account, ticket=ticket)


# ── 引导 ──


class BootstrapClosed(Exception):
    """库里已有用户，引导不再开放。**这不是错误配置，是设计**（`I-V12`）。"""


@dataclass(frozen=True)
class BootstrapResult:
    """一次成功引导产出的两个 id。**不含口令、不含票据**：引导脚本会把它打印出来，
    而终端输出会进 CI 日志与运维的复制粘贴历史。"""

    tenant_id: str
    user_id: str


def bootstrap(
    conn: sqlite3.Connection,
    *,
    email: str,
    password: str,
    tenant_name: str,
    now: datetime,
    must_change_password: bool = True,
) -> BootstrapResult:
    """空库上建首个团队 + 首个管理员。**只能跑一次**。

    判据是「库里有没有用户」，不是「有没有跑过这个命令」：后者需要一张记状态的表，
    而那张表本身就是一个可以被重置的开关。留着可用的引导路径就是永久后门，
    而且没人会记得关（`I-V12`）。

    并发两次引导会建出两个团队——`count_users` 与写入之间没有锁。不修：
    引导是部署时人手敲一次的命令，两个人在同一秒敲它的概率远低于「为它加一张
    全局锁表」带来的复杂度。真要防，应当由启动脚本的进程锁负责，不是这一层。
    """
    if not policy.can_bootstrap(store.count_users(conn)):
        raise BootstrapClosed("库里已有用户：引导只允许在空库上执行一次")

    password_hash = credentials.hash_password(password)
    with conn:
        tenant_id = store.insert_tenant(conn, name=tenant_name, now=now)
        user_id = store.insert_user(
            conn,
            tenant_id=tenant_id,
            email=email,
            password_hash=password_hash,
            role=Role.ADMIN,
            now=now,
            # **全仓唯一一处写 True 的地方。** 引导的人是能上服务器的人，
            # 也就是这套安装的所有者；而「能不能上服务器」在产品里没有别的投影，
            # 所以只能在这里落一次（REQ-AUTH-12）。
            # 任何接口都不能改这一列：有授予入口，团队管理员就能自封。
            is_platform_admin=True,
            # 生产路径默认要求首登改密；测试/迁移若需要“已经轮换过”的账号，
            # 显式传 False，而不是把生产默认值改成安全的一侧之外。
            must_change_password=must_change_password,
        )
    return BootstrapResult(tenant_id=tenant_id, user_id=user_id)
