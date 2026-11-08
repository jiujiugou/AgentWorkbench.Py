"""领域模型：账号、团队、票据、邀请、身份事件。

**放**：frozen dataclass 与枚举（`Role` / `AccountStatus` / `ReasonCode`）。
**不放**：SQL、pydantic、fastapi。

三个正交维度在这里就定成三个字段（`status` / `lockout_until` / `credential_epoch`），
**不合并成 `enabled`**：合并会同时丢掉「谁改的」（管理员手动还是系统自动）
与「要不要踢下线」（停用要踢，锁定不许踢）——F1 §1.4。

改坏这里会怎样：把 `lockout_until` 塞进 `status` 的两态里，
「锁定不动在线会话」那条判据当场红，而用户看到的是「别人随手输错几次密码，
我就被踢下线了」。

时间一律是 `datetime`（带时区），不是字符串。库里存的是秒级 ISO 文本，
**那是存储格式，不是领域类型**——转换在 `store` 一侧做。判定里若拿字符串比较，
`lockout_until > now` 就退化成字典序比较，而字典序在跨年、跨时区偏移上与时间顺序
不一致（这类错误只在某几天里发作）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from agentworkbench.core.identity import Owner
from agentworkbench.core.knowledge import DEFAULT_LEVEL_RANK, MAX_LEVEL_RANK, MIN_LEVEL_RANK

__all__ = [
    "DEFAULT_LEVEL_RANK",
    "MAX_LEVEL_RANK",
    "MIN_LEVEL_RANK",
    "ROLE_RANK",
    "Account",
    "AccountStatus",
    "IdentityEvent",
    "Invite",
    "InviteState",
    "Outcome",
    "Principal",
    "ReasonCode",
    "ResourceRef",
    "Role",
    "Tenant",
    "Ticket",
    "TicketState",
]


class Role(StrEnum):
    """团队内的两种角色。

    只有两个，是刻意的取舍：三维权限矩阵（文档级 / 部门级 / 岗位级）漏一处就漏数据，
    二维靠一条条件就能守住（F1 §2.1）。加第三种之前，先回答「每个入口都测了吗」。
    """

    ADMIN = "admin"
    MEMBER = "member"


# 高低的唯一来源。判定里不许出现 `role == "admin"` 这类散落比较——
# 加第三种角色时，漏改的那一处不会报错，只会放行。
ROLE_RANK: dict[Role, int] = {Role.MEMBER: 1, Role.ADMIN: 2}


class AccountStatus(StrEnum):
    """账户状态机（`account_status`，两态）。

    **与 `lockout_until` 是两件事**：这个是管理员手动改、直到恢复、必须踢掉已签发会话；
    锁定是系统自动、到期自愈、**不许踢会话**——否则拿别人邮箱连错几次密码就能挤掉对方，
    是一条成本极低的 DoS。
    """

    ACTIVE = "active"
    DISABLED = "disabled"


class TicketState(StrEnum):
    """票据状态机（`ticket_lifecycle`，三态）。

    `expired` 是时间比较的结果，**不是一次写操作**——所以它不会让 `revoked_at` 变成非空。
    两者在库里可区分：「过期」到点自动发生，「吊销」是某个人做了某件事。
    """

    ISSUED = "issued"
    REVOKED = "revoked"
    EXPIRED = "expired"


class InviteState(StrEnum):
    """邀请状态机（`invite_lifecycle`，三态）。一次性：接受即终结。"""

    PENDING = "pending"
    ACCEPTED = "accepted"
    EXPIRED = "expired"


class ReasonCode(StrEnum):
    """拒绝原因。**只进记录，不进响应。**

    对外文案与它无关：401 / 403 / 404 各一种体，都不随原因码变化。
    分开记是为了运维能回答「这是管理员停的、系统锁的，还是他本来就没权限」——
    这三种情况的界面完全一样（F1 §1.3）。
    """

    UNAUTHENTICATED = "unauthenticated"
    SESSION_EXPIRED = "session_expired"
    ACCOUNT_DISABLED = "account_disabled"
    ACCOUNT_LOCKED_OUT = "account_locked_out"
    NOT_IN_TEAM = "not_in_team"
    NOT_OWNER = "not_owner"
    ROLE_INSUFFICIENT = "role_insufficient"
    PLATFORM_ADMIN_REQUIRED = "platform_admin_required"
    """「这件事只有这套部署的所有者能做」。

    **刻意不与 `role_insufficient` 合并**：运维看到前者会去找该团队的管理员，
    而这件事找团队管理员没有用——他在自己的团队里权限再高也改不了全平台配置。
    合并之后，记录里所有 403 长得一样，而处置方式相反的两种拒绝并成了一种。"""
    LEVEL_INSUFFICIENT = "level_insufficient"
    """「你的账号等级不够」：内容读侧按 404、管理动作按 403，对外不泄露原因。"""
    KB_NOT_VISIBLE = "kb_not_visible"
    """「这个知识库对你不可见」：与不存在的知识库/文档共用 404。"""
    PASSWORD_CHANGE_REQUIRED = "password_change_required"  # noqa: S105 —— 原因码，不是口令
    """「你还没改初始口令」。它也是 403，但处置动作是改密，不是找人授权。"""
    BOOTSTRAP_CLOSED = "bootstrap_closed"
    RESOURCE_MISSING = "resource_missing"
    """「按这个 id 取不到」的唯一理由。

    **它同时表示「不存在」与「属于别人」，因为我们本来就分不出来**——路由按团队
    条件查，查不到就是查不到，再查一次「它到底在不在」会造出判据 `I-V2` 明令禁止
    的那条侧信道。分成两个码反而更糟：读记录的人会以为自己知道了一件其实没验的事。"""


class Outcome(StrEnum):
    """判定的四种结果，恰好是三种拒绝出口加一个放行。

    做成枚举而不是 `bool`：拒绝不是一个东西——401（你是谁）、404（当它不存在）、
    403（知道你能来但不许动）对调用方的含义完全不同。合成 `False` 之后由调用点
    各自编一个状态码，第一个编错的地方不会有人发现。
    """

    ALLOW = "allow"
    UNAUTHENTICATED = "unauthenticated"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class Tenant:
    """一个团队。数据归属的边界。"""

    tenant_id: str
    name: str
    created_at: datetime


@dataclass(frozen=True)
class Account:
    """一个账户。三个维度都在这里，谁也不代替谁。

    `failed_attempts` 不是第四个维度，是锁定的**输入**：达阈值时由服务层写
    `lockout_until` 并把计数归零。放在账户上而不是单独一张表，是因为它只在
    登录这一条路径上被读写，没有独立生命周期。

    `is_platform_admin` 也**不是第四个维度，是另一根轴**（REQ-AUTH-12）：
    `role` 的射程是这个团队，它的射程是这套安装。所以它不进 `ROLE_RANK`——
    并进等级轴之后，「目标角色高于自己」那条判定会把一个管供应商的人当成
    「更高的团队角色」，而那个人在自己的团队里可能只是 member。
    """

    user_id: str
    tenant_id: str
    email: str
    role: Role
    status: AccountStatus
    credential_epoch: int
    lockout_until: datetime | None = None
    failed_attempts: int = 0
    is_platform_admin: bool = False
    must_change_password: bool = False
    level_rank: int = DEFAULT_LEVEL_RANK

    @property
    def normalized_email(self) -> str:
        """比较用的小写形式。**归一化必须与唯一索引同一种**——否则会出现
        「库里认为不重复、登录时认为找不到」，两套规则各自自洽却互相矛盾。"""
        return self.email.strip().casefold()


@dataclass(frozen=True)
class Ticket:
    """一张票据 = cookie 里那串明文（库里只有它的 SHA-256）。

    `credential_epoch` 是签发时刻账户的版本号。它让「改密后旧票据失效」变成一次
    整数比较，而不是「遍历所有会话删一遍」——后者漏一条就留一条后门。
    """

    ticket_id: str
    user_id: str
    token_hash: str
    credential_epoch: int
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class Invite:
    """一张一次性邀请。`expires_at` 与 `accepted_at` 都能让 `pending` 终结。"""

    invite_id: str
    tenant_id: str
    role: Role
    expires_at: datetime
    email: str | None = None
    accepted_at: datetime | None = None


@dataclass(frozen=True)
class Principal:
    """通过判定 ①（票据有效）之后的那份身份。

    **只能由 `policy.judge_access` 产出**，不要从 `Account` 直接构造：直接构造等于
    跳过票据校验，而「停用后旧 cookie 一个窗口内失效」正是靠那一步才成立的。
    做成独立类型而不是复用 `Account`，是为了让「这个身份验过票」成为类型上的事实，
    而不是每个调用点的自觉。

    `is_platform_admin` 从 `Account` 一路带到这里，是因为**判定要用它**：
    `judge_platform` 只拿得到 `Principal`。它不随任何响应出去——
    成员的 DTO 是逐字段写死的，「谁是平台所有者」不进成员列表。
    """

    owner: Owner
    role: Role
    status: AccountStatus
    credential_epoch: int
    lockout_until: datetime | None = None
    is_platform_admin: bool = False
    must_change_password: bool = False
    level_rank: int = DEFAULT_LEVEL_RANK


@dataclass(frozen=True)
class ResourceRef:
    """被访问对象的归属，**只有归属，没有内容**。

    判定层不读被访问的数据，只判「这份数据归谁」——所以它拿到的东西必须小到
    无法泄露内容：两个 id，仅此而已。

    `tenant_id` 为 `None` 或空串表示**没有归属**（阶段 1 的存量数据就是这样）。
    按 AUTH-06，这种行对所有人 404，包括管理员：宁可「数据看不见」（一查就发现），
    也不要「所有人可见」（发现不了）。
    """

    tenant_id: str | None
    owner_user_id: str | None = None

    @property
    def has_owner(self) -> bool:
        return bool((self.tenant_id or "").strip())


@dataclass(frozen=True)
class IdentityEvent:
    """一条拒绝记录（F1 §1.3）。

    **只记元数据，不记内容**：不记请求体、不记响应体。轨迹里的证据属 F4，不属这里。
    `event_id` 与 run 的 id 不同源，避免「拒绝也是一次执行」的误读。
    """

    event_id: str
    occurred_at: datetime
    action: str
    reason_code: ReasonCode
    actor_user_id: str | None = None
    tenant_id: str | None = None
    resource_kind: str | None = None
    resource_id: str | None = None
    request_id: str | None = None
