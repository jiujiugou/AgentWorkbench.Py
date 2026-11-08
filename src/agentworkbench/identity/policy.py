"""判定层：全站唯一允许多写一个 allow 出口的地方。

**放**：`Decision`、`judge_*` 纯函数、三台状态机的迁移白名单。
**不放**：任何 IO、任何第三方 import——core 层的规矩在这里同样适用，
因为判定必须能在没有数据库、没有请求对象的情况下被测。

判定四步的顺序写在这里，不写在 `api` 层：`api` 只做「原因码 → 状态码」的映射表。
api 里出现 if 就是在挪业务，而挪出去的那条分支永远比原版少一个条件。

改坏这里会怎样：把「先判归属再判角色」的顺序换过来，用户拿一个别人的 id
会收到「角色不够」而不是「不存在」——那个差别本身就是「这条记录存在」的信号。

**为什么四步写在一个函数里而不是拆成四个 `if` 分散到各路由**：`I-V16` 要求
「未登录访问他人资源 → 401（不是 404）；已登录访问同一 id → 404（不是 403）」。
这条判据的内容就是**顺序**，而顺序只有在同一个函数里才看得见。拆开之后，
每处调用都得自己记得先判身份；漏一处的表现不是报错，是「登录态成了可推断的信息」。

**一处文档冲突，取法写在 `login_block_reason` 的 docstring 里**：§1.6 的第 ② 步列了
`account_locked_out`，但 §1.4 与 `I-V8` 要求「锁定不踢已登录会话」。两者的分界是
**路径**：受保护请求只判 `status`，锁定只在登录路径上判。取了 `I-V8`——它是可执行的判据，
§1.6 是一张表；判据优先于表格。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, cast

from agentworkbench.core.identity import Owner
from agentworkbench.core.knowledge import can_read
from agentworkbench.identity.models import (
    ROLE_RANK,
    Account,
    AccountStatus,
    InviteState,
    Outcome,
    Principal,
    ReasonCode,
    ResourceRef,
    Role,
    Ticket,
    TicketState,
)

__all__ = [
    "ACCOUNT_TRANSITIONS",
    "HTTP_STATUS",
    "INVITE_TRANSITIONS",
    "TICKET_TRANSITIONS",
    "TRANSITIONS",
    "Decision",
    "IllegalTransition",
    "can_bootstrap",
    "can_transition",
    "judge_access",
    "judge_admin",
    "judge_invite",
    "judge_kb_management",
    "judge_kb_read",
    "judge_level_assignment",
    "judge_management",
    "judge_platform",
    "login_block_reason",
    "require_transition",
]


# 判定结果 → HTTP 状态码。**这是唯一的一张表**：api 层不许自己写状态码字面量。
# 401 出现两次（票据无效 / 账户不可用）是刻意的：两种原因对外必须同码同文案，
# 否则「这个账号被停用了」就能从响应里读出来（F1 §1.6）。
HTTP_STATUS: Final[dict[Outcome, int]] = {
    Outcome.ALLOW: 200,
    Outcome.UNAUTHENTICATED: 401,
    Outcome.NOT_FOUND: 404,
    Outcome.FORBIDDEN: 403,
}


@dataclass(frozen=True)
class Decision:
    """一次判定的结果。**构造即校验**：半成品决策不可能存在。

    三条校验各对应一种真实的事故形态：

    - `allow` 却没带 `principal` → 下游拿不到 `Owner`，仓储调用会退化成
      「不带归属的查询」，也就是全放行。所以这里直接拒绝构造。
    - `allow` 却带了 `reason` → 同一次判定既是放行又是拒绝，两个消费者各读一个字段，
      谁都不会报错。
    - 拒绝却没带 `reason` → 记录里没有原因码，运维只剩一句「404 了」，
      而 F1 §1.3 存在的全部理由就是别让运维只剩这一句。
    """

    outcome: Outcome
    reason: ReasonCode | None = None
    principal: Principal | None = None

    def __post_init__(self) -> None:
        if self.outcome is Outcome.ALLOW:
            if self.principal is None:
                raise ValueError("allow 必须带 principal：不带归属的放行等于全放行")
            if self.reason is not None:
                raise ValueError("allow 不许带 reason：放行与拒绝不能同时成立")
        elif self.reason is None:
            raise ValueError("拒绝必须带 reason：没有原因码的记录答不了「为什么」")

    @property
    def allowed(self) -> bool:
        return self.outcome is Outcome.ALLOW

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.outcome]

    @property
    def owner(self) -> Owner | None:
        """放行后的归属。拿不到就是拒绝，所以调用点不需要再判一次 `allowed`。"""
        return None if self.principal is None else self.principal.owner

    @property
    def actor_user_id(self) -> str | None:
        """拒绝记录要填的「谁被拒了」。未认证时为 None——那也要记，
        否则「没登录」和「系统坏了」在记录里分不开（F1 §1.3）。"""
        return None if self.principal is None else self.principal.owner.user_id

    @property
    def tenant_id(self) -> str | None:
        return None if self.principal is None else self.principal.owner.tenant_id


def _deny(outcome: Outcome, reason: ReasonCode, principal: Principal | None = None) -> Decision:
    """拒绝的内部构造点。存在的意义只是少写几遍 `Decision(...)`，**不是**第二个出口：
    它不接受 `Outcome.ALLOW`，传进来会在 `__post_init__` 抛。"""
    return Decision(outcome=outcome, reason=reason, principal=principal)


def judge_access(
    *,
    ticket: Ticket | None,
    account: Account | None,
    now: datetime,
    resource: ResourceRef | None = None,
) -> Decision:
    """判定四步，前一步不过不进下一步。**顺序不能换**（F1 §1.6）。

    | 序 | 判什么 | 不过时对外 |
    |---|---|---|
    | ① | 有没有有效票据 | 401 |
    | ② | 账户还能不能用（`status`） | 401，文案与 ① 相同 |
    | ③ | 这个对象属不属于本团队 | 404 |
    | ④ | 是不是本人的 | 404 |

    `resource` 为 `None` 表示这次只要判身份（列表类、提交类接口）——归属过滤由
    SQL 承担（F2 / F5 的地盘），本函数只负责「这个人是可信的」。

    **② 只看 `status`，不看 `lockout_until`**：锁定的作用范围是「不许再登录」，
    不是「把已经在用的人踢下去」。判据是 `I-V8`——否则拿别人的邮箱连错几次密码
    就能挤掉对方，成本比猜密码低得多。锁定的判定在 `login_block_reason`。

    **①② 与 ③④ 之间不能插任何东西。** 先碰资源再判账户，会让「这个 id 存不存在」
    先于「你还算不算登录着」被回答，那正是 `I-V16` 要防的侧信道。
    """
    # ① 票据：不存在 / 指向不存在的账户 / 已吊销 / 已过期 / 版本落后
    if ticket is None:
        return _deny(Outcome.UNAUTHENTICATED, ReasonCode.UNAUTHENTICATED)
    if account is None or ticket.user_id != account.user_id:
        # 票据指向一个读不出来的账户：与「没有票据」同一出口、同一文案。
        # 分成两种响应等于告诉外界「这个 user_id 曾经存在」。
        return _deny(Outcome.UNAUTHENTICATED, ReasonCode.UNAUTHENTICATED)
    if ticket.revoked_at is not None:
        return _deny(Outcome.UNAUTHENTICATED, ReasonCode.SESSION_EXPIRED)
    if ticket.expires_at <= now:
        return _deny(Outcome.UNAUTHENTICATED, ReasonCode.SESSION_EXPIRED)
    if ticket.credential_epoch != account.credential_epoch:
        # 改密 / 管理员重置之后，旧票据的版本号落后。**靠比对，不靠遍历删除**：
        # 删除是一批写操作，漏一条留一条后门；比对是一次整数比较，漏不掉。
        return _deny(Outcome.UNAUTHENTICATED, ReasonCode.SESSION_EXPIRED)

    if not account.tenant_id.strip():
        # 账户行没有归属（阶段 1 的存量数据可能长这样）。**判不清就不给**（AUTH-06），
        # 而且**不许回落到任何默认租户**——回落正是 C# 版 `GetOwner()` 返回空串的形态：
        # 空串在 SQL 里匹配不到任何行，于是每个请求都「什么都没查到」，看起来像数据没了。
        return _deny(Outcome.UNAUTHENTICATED, ReasonCode.UNAUTHENTICATED)

    principal = Principal(
        owner=Owner(user_id=account.user_id, tenant_id=account.tenant_id),
        role=account.role,
        status=account.status,
        credential_epoch=account.credential_epoch,
        lockout_until=account.lockout_until,
        is_platform_admin=account.is_platform_admin,
        must_change_password=account.must_change_password,
        level_rank=account.level_rank,
    )

    # ② 账户：只判 status。停止的账户连同已签发会话一起失效（那是「踢」的本意）；
    # 锁定不进这里（见 docstring）。
    if account.status is AccountStatus.DISABLED:
        return _deny(Outcome.UNAUTHENTICATED, ReasonCode.ACCOUNT_DISABLED, principal)

    # ③④ 资源：先团队，再本人
    if resource is not None:
        denied = _judge_resource(principal, resource)
        if denied is not None:
            return denied

    return Decision(outcome=Outcome.ALLOW, principal=principal)


def _judge_resource(principal: Principal, resource: ResourceRef) -> Decision | None:
    """步骤 ③④。**返回 `None` 表示通过**——只有「拒绝」是一个值。

    这样写是为了让调用点无法「忘了处理返回值」：拿到 `Decision` 就必须返回它，
    而通过的情况根本没有 `Decision` 可拿。
    """
    # ③ 团队。没有归属的行（tenant_id 为 None / 空串）落进同一分支：
    # 按 AUTH-06，它对所有人 404，包括管理员。
    if not resource.has_owner or resource.tenant_id != principal.owner.tenant_id:
        return _deny(Outcome.NOT_FOUND, ReasonCode.NOT_IN_TEAM, principal)

    # ④ 本人。`owner_user_id` 为 None 表示团队级资源（文档），只判到第 ③ 步；
    # 非 None 表示个人资源（会话 / 轨迹），必须是本人的。
    if resource.owner_user_id is not None and resource.owner_user_id != principal.owner.user_id:
        return _deny(Outcome.NOT_FOUND, ReasonCode.NOT_OWNER, principal)

    return None


def login_block_reason(account: Account | None, now: datetime) -> ReasonCode | None:
    """登录路径上的账户闸。**返回 `None` 表示可以继续校验口令。**

    不放回 `Decision`：登录时还没有票据、也没有 `Principal`，硬造一个会让
    `Decision` 的「allow 必须带归属」校验形同虚设。这里只需要回答一个更小的问题：
    这个账户现在能不能尝试登录。

    **锁定为什么在这里而不在 `judge_access`**：§1.6 的第 ② 步列了
    `account_locked_out`，但 §1.4 写明锁定的「踢已登录会话」是**否**，`I-V8`
    更是直接要求「别人连错几次，A 的在线会话仍是 200」。两条只能同时成立在一种
    读法下：锁定拦的是**下一次登录**，不是**下一次请求**。§1.6 那张表的第 ② 步
    因此只在登录路径上覆盖锁定。

    `account is None` 返回 `UNAUTHENTICATED` 而不是别的：**这个返回值只进记录、
    不进响应**——对外文案必须与「口令错」逐字相同，否则响应快慢与措辞就成了账号枚举器
    （`I-H2`）。调用方拿到非 None 之后仍然要跑一次假哈希，把耗时补齐。
    """
    if account is None:
        return ReasonCode.UNAUTHENTICATED
    if account.status is AccountStatus.DISABLED:
        return ReasonCode.ACCOUNT_DISABLED
    if account.lockout_until is not None and account.lockout_until > now:
        return ReasonCode.ACCOUNT_LOCKED_OUT
    return None


def judge_admin(*, actor: Principal) -> Decision:
    """**动作级**判定：调用者是不是管理员。不需要目标。

    存在的理由是**顺序**，不是重复：`judge_management` 要判「目标角色高不高」
    和「目标是不是自己」，所以它必须先拿到目标；而「能不能去查这个目标」
    必须先回答——否则非管理员拿一串 id 打管理接口，404 与 403 的差别
    就成了「这个人存不存在」的探针。

    于是管理接口的顺序是三步，不是两步：

        judge_admin（我是管理员吗）→ 查目标（不在本团队 → 404）→ judge_management（目标合不合规）

    漏掉第一步的实现功能全对：管理员照常能用，非管理员照常被拒。
    差别只在**响应**上——而那个差别不需要任何权限就能观测。
    """
    if actor.role is not Role.ADMIN:
        return _deny(Outcome.FORBIDDEN, ReasonCode.ROLE_INSUFFICIENT, actor)
    return Decision(outcome=Outcome.ALLOW, principal=actor)


def judge_platform(*, actor: Principal) -> Decision:
    """**动作级**判定：调用者是不是这套部署的所有者。不需要目标。

    与 `judge_admin` 是两件事，不是同一个东西的两个名字：前者问「在我这个团队里
    能不能管人」，这个问「能不能改这套安装」。`providers` / `provider_models` /
    `system_state` 三张表没有团队列，所以改它们的权限不可能由团队角色承载——
    第二个团队出现的那天，「A 队管理员改 B 队供应商」会是一条合法操作，
    而 B 队毫无察觉（REQ-AUTH-12）。

    **为什么不是 404**：这几条路由是公开存在的动作，本来就不该按 id 藏。
    这里判的是「你能不能做这件事」，不是「这个资源在不在」——§1.6 那张表里，
    403 只出现在「按动作调的接口」这一栏。

    **为什么原因码不用 `role_insufficient`**：那个码的处置是「去找团队管理员」，
    而这件事找团队管理员没有用，他在自己团队里权限再高也改不了全平台配置。
    """
    if not actor.is_platform_admin:
        return _deny(Outcome.FORBIDDEN, ReasonCode.PLATFORM_ADMIN_REQUIRED, actor)
    return Decision(outcome=Outcome.ALLOW, principal=actor)


def judge_kb_read(*, actor: Principal, visibility: str, min_level_rank: int | None) -> Decision:
    """知识库读权限。**不可见与不存在共用 404**。

    `public` 全员可读；`private` 要求 `actor.level_rank >= min_level_rank`。
    管理员没有内容后门：这个函数不读 `role`，所以低等级管理员与普通成员
    走同一条判定。
    """
    if can_read(actor.level_rank, visibility, min_level_rank):
        return Decision(outcome=Outcome.ALLOW, principal=actor)
    return _deny(Outcome.NOT_FOUND, ReasonCode.KB_NOT_VISIBLE, actor)


def judge_kb_management(
    *, actor: Principal, visibility: str, min_level_rank: int | None
) -> Decision:
    """知识库元数据/内容写权限：**管理员角色 + 知识库对本人可见**。

    用 403 而不是 404：这是按动作调的接口，动作本身公开存在。低等级管理员
    能知道知识库存在（元数据可见），但不能改它。
    """
    if actor.role is not Role.ADMIN:
        return _deny(Outcome.FORBIDDEN, ReasonCode.ROLE_INSUFFICIENT, actor)
    if not can_read(actor.level_rank, visibility, min_level_rank):
        return _deny(Outcome.FORBIDDEN, ReasonCode.LEVEL_INSUFFICIENT, actor)
    return Decision(outcome=Outcome.ALLOW, principal=actor)


def judge_level_assignment(
    *, actor: Principal, target_user_id: str | None, target_level_rank: int
) -> Decision:
    """管理员设置等级的上限：不能改自己，不能授予高于自己的等级。

    目标为 `None` 表示直接建号（还没有账号）；建号同样受上限约束，否则
    「不能授予高于自己」只要走建号路径就绕过去了。
    """
    if actor.role is not Role.ADMIN:
        return _deny(Outcome.FORBIDDEN, ReasonCode.ROLE_INSUFFICIENT, actor)
    if target_user_id is not None and target_user_id == actor.owner.user_id:
        return _deny(Outcome.FORBIDDEN, ReasonCode.LEVEL_INSUFFICIENT, actor)
    if target_level_rank > actor.level_rank:
        return _deny(Outcome.FORBIDDEN, ReasonCode.LEVEL_INSUFFICIENT, actor)
    return Decision(outcome=Outcome.ALLOW, principal=actor)


def judge_management(
    *, actor: Principal, target_user_id: str, target_role: Role, target_level_rank: int = 1
) -> Decision:
    """管理动作（停用 / 恢复 / 改角色 / 删成员）的边界。**全域唯一用 403 的地方。**

    三条规则，缺一条都有真实后果：

    1. **不是管理员 → 403。** 这里是「按动作调管理接口」，动作本身就是公开的，
       403 不泄露任何资源是否存在——所以它可以用 403，而「按 id 取」必须用 404
       （`I-V16`）。第一条与 `judge_admin` 重复，**不是可以删的那一半**：
       本函数假定目标已经查到，而「能不能去查目标」是另一个问题
       （见 `judge_admin` 的 docstring）。两条同时存在，是因为调用方可能只调这一个——
       少判一条不会报错，只会让非管理员拿到一个 404。
    2. **目标是自己 → 403。** 停用自己会把最后一个管理员锁在门外，而恢复它需要
       另一个管理员——如果这是唯一的管理员，系统就再也进不去了。
    3. **目标角色高于自己 → 403。** 不判这一条，管理员可以造一个比自己高的角色，
       再用它回来操作自己——「不得高于自己」只有在目标角色上也判才算数。
    4. **目标等级高于自己 → 403。** 否则低等级管理员可以重置高等级账号的密码，
       「管理员不能看私人库」就只剩界面隐藏。
    """
    if actor.role is not Role.ADMIN:
        return _deny(Outcome.FORBIDDEN, ReasonCode.ROLE_INSUFFICIENT, actor)
    if target_user_id == actor.owner.user_id:
        return _deny(Outcome.FORBIDDEN, ReasonCode.ROLE_INSUFFICIENT, actor)
    if ROLE_RANK[target_role] > ROLE_RANK[actor.role]:
        return _deny(Outcome.FORBIDDEN, ReasonCode.ROLE_INSUFFICIENT, actor)
    if target_level_rank > actor.level_rank:
        return _deny(Outcome.FORBIDDEN, ReasonCode.LEVEL_INSUFFICIENT, actor)
    return Decision(outcome=Outcome.ALLOW, principal=actor)


def judge_invite(*, actor: Principal, target_role: Role) -> Decision:
    """发邀请。**与 `judge_management` 共用两条规则，但没有目标账号。**

    单独成一个函数而不是复用 `judge_management(target_user_id=...)`：
    邀请发生时那个人还不存在，硬塞一个空串当「没有目标」是哨兵值——
    而哨兵值的下一次误用不会报错，只会把「目标是自己」那条规则悄悄跳过。

    这里判的是**邀请上写的角色**，不是「这个人以后会不会升上去」：
    若允许管理员发出 admin 邀请，等于他可以凭空造一个与自己同级的管理员，
    `AUTH-10` 的「不得造出高于自己」就成了摆设（同级已经足够互相操作）。

    没有第四条规则：邀请没有期限以外的状态，也没有「停用自己」那种自锁。
    """
    if actor.role is not Role.ADMIN:
        return _deny(Outcome.FORBIDDEN, ReasonCode.ROLE_INSUFFICIENT, actor)
    if ROLE_RANK[target_role] > ROLE_RANK[actor.role]:
        return _deny(Outcome.FORBIDDEN, ReasonCode.ROLE_INSUFFICIENT, actor)
    return Decision(outcome=Outcome.ALLOW, principal=actor)


def can_bootstrap(existing_user_count: int) -> bool:
    """引导只能跑一次。**判据是「库里有没有用户」，不是「有没有跑过这个命令」**——
    后者需要一张记状态的表，而那张表本身就是一个可被重置的开关。

    返回 `bool` 而不是 `Decision`：引导没有 actor，硬塞一个 `principal` 会让
    `Decision` 的一致性校验变成「允许没有归属的 allow」，那正是要防的形态。
    拒绝由调用方记 `ReasonCode.BOOTSTRAP_CLOSED`，不经过这里的返回值。
    """
    return existing_user_count <= 0


# ── 三台状态机的迁移白名单 ──
# 真源在代码里，状态集登记在 pyproject.toml（`[[tool.agentworkbench.state_machine]]`）。
# 这样拆的理由：状态集是给人看的清单，迁移是要被执行的规则——把规则写进 toml
# 等于让「能不能迁移」依赖一个配置文件有没有被正确解析。
ACCOUNT_TRANSITIONS: Final[dict[AccountStatus, frozenset[AccountStatus]]] = {
    AccountStatus.ACTIVE: frozenset({AccountStatus.DISABLED}),
    AccountStatus.DISABLED: frozenset({AccountStatus.ACTIVE}),
}

TICKET_TRANSITIONS: Final[dict[TicketState, frozenset[TicketState]]] = {
    TicketState.ISSUED: frozenset({TicketState.REVOKED, TicketState.EXPIRED}),
    TicketState.REVOKED: frozenset(),
    TicketState.EXPIRED: frozenset(),
}

INVITE_TRANSITIONS: Final[dict[InviteState, frozenset[InviteState]]] = {
    InviteState.PENDING: frozenset({InviteState.ACCEPTED, InviteState.EXPIRED}),
    InviteState.ACCEPTED: frozenset(),
    InviteState.EXPIRED: frozenset(),
}

# `cast` 是因为 StrEnum 的键在运行时就是 `str`（`AccountStatus.ACTIVE == "active"`
# 且哈希相同），但类型系统里 `dict[AccountStatus, X]` 与 `dict[str, X]` 不协变。
# 换成 `dict[object, ...]` 又会让 `can_transition` 的签名失去约束。
TRANSITIONS: Final[dict[str, dict[str, frozenset[str]]]] = {
    "account_status": cast("dict[str, frozenset[str]]", ACCOUNT_TRANSITIONS),
    "ticket_lifecycle": cast("dict[str, frozenset[str]]", TICKET_TRANSITIONS),
    "invite_lifecycle": cast("dict[str, frozenset[str]]", INVITE_TRANSITIONS),
}


class IllegalTransition(ValueError):
    """迁到白名单外的状态。与 `core.runs.IllegalTransition` 同样式：写入口调用
    `require_transition`，而不是自己写 `if`——自己写的那个 `if` 总有漏的一种状态。"""


def can_transition(machine: str, source: str, target: str) -> bool:
    """这次迁移合法吗。**同态到同态一律不合法**——白名单从不包含自身，
    所以「重复停用同一个账户」第二次会被拒，而不是静默成功。"""
    table = TRANSITIONS[machine]
    if source not in table:
        raise KeyError(f"未知状态：{machine}.{source}")
    return target in table[source]


def require_transition(machine: str, source: str, target: str) -> None:
    if not can_transition(machine, source, target):
        raise IllegalTransition(f"非法迁移：{machine} {source} -> {target}")
