"""判定层不变量：四步顺序、三种出口、三个正交维度（I-V5 / I-V8 / I-V9 / I-V11 / I-V16）。

这一层不建库、不碰 HTTP，所以整套判据能在毫秒级跑完——**这是它独立存在的理由**：
如果判定只能连着数据库和请求对象一起测，那它永远只会被覆盖到「顺手测到的那几条」。

改坏这些不变量，用户会看到什么（逐条对应下面的用例）：

- 把第 ③ 步（团队）挪到第 ① 步（票据）前面 → 未登录的人访问别人的 id 会收到 404
  而不是 401，于是「这个 id 存不存在」在登录之前就被回答了（`I-V16`）；
- 把归属判定换成 403 → 用户能从状态码区分「不存在」与「存在但不是你的」，
  两串 id 就能试出哪条真的存在（`I-V16`）；
- 把 `lockout_until` 的判定写进会话校验 → 任何人拿别人的邮箱随手输错几次密码，
  对方就被踢下线，而这件事比猜中密码便宜得多（`I-V8`）；
- 把 `status` 与 `lockout_until` 合成一个 `enabled` 布尔 → 管理员再也答不出
  「这个人是被停用还是被系统锁的」，恢复动作也会做错（`I-V7`）；
- 允许 `allow` 决策不带归属 → 下游仓储调用拿不到 `Owner`，查询退化成全放行，
  用户看到的是别人的数据出现在自己列表里（`I-V5`）。
"""

from __future__ import annotations

import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentworkbench.core.identity import Owner
from agentworkbench.identity.models import (
    Account,
    AccountStatus,
    Outcome,
    Principal,
    ReasonCode,
    ResourceRef,
    Role,
    Ticket,
)
from agentworkbench.identity.policy import (
    TRANSITIONS,
    Decision,
    IllegalTransition,
    can_bootstrap,
    can_transition,
    judge_access,
    judge_admin,
    judge_management,
    login_block_reason,
    require_transition,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 4, 9, 0, 0, tzinfo=UTC)
ALICE = "alice"
BOB = "bob"
TEAM_A = "team-a"
TEAM_B = "team-b"


def account(
    *,
    user_id: str = ALICE,
    tenant_id: str = TEAM_A,
    role: Role = Role.MEMBER,
    status: AccountStatus = AccountStatus.ACTIVE,
    epoch: int = 1,
    lockout_until: datetime | None = None,
) -> Account:
    return Account(
        user_id=user_id,
        tenant_id=tenant_id,
        email=f"{user_id}@example.com",
        role=role,
        status=status,
        credential_epoch=epoch,
        lockout_until=lockout_until,
    )


def ticket(
    *,
    user_id: str = ALICE,
    epoch: int = 1,
    revoked_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> Ticket:
    return Ticket(
        ticket_id="t-1",
        user_id=user_id,
        token_hash="0" * 64,
        credential_epoch=epoch,
        issued_at=NOW - timedelta(hours=1),
        expires_at=expires_at or NOW + timedelta(hours=1),
        revoked_at=revoked_at,
    )


def principal(
    *,
    user_id: str = ALICE,
    tenant_id: str = TEAM_A,
    role: Role = Role.ADMIN,
) -> Principal:
    return Principal(
        owner=Owner(user_id=user_id, tenant_id=tenant_id),
        role=role,
        status=AccountStatus.ACTIVE,
        credential_epoch=1,
    )


# ── 四步顺序与三种出口（I-V16） ──


def test_missing_ticket_is_401_even_when_the_resource_exists() -> None:
    """I-V16 前半：未登录 + 一个真实存在的他人资源 → **401，不是 404**。

    这条测的是顺序。第 ① 步先于第 ③ 步，所以「你有没有票」先于「这条归不归你」
    被回答。反过来的话，任何人拿一串 id 就能试出哪条真的存在——不需要登录。
    """
    decision = judge_access(
        ticket=None,
        account=None,
        now=NOW,
        resource=ResourceRef(tenant_id=TEAM_A, owner_user_id=BOB),
    )

    assert decision.outcome is Outcome.UNAUTHENTICATED
    assert decision.http_status == 401
    assert decision.reason is ReasonCode.UNAUTHENTICATED


def test_foreign_resource_is_404_not_403() -> None:
    """I-V16 中段：已登录访问同团队的他人会话 → **404，不是 403**。

    403 的意思是「它在那儿，但不给」，那本身已经泄露了「它在那儿」。
    """
    decision = judge_access(
        ticket=ticket(),
        account=account(),
        now=NOW,
        resource=ResourceRef(tenant_id=TEAM_A, owner_user_id=BOB),
    )

    assert decision.outcome is Outcome.NOT_FOUND
    assert decision.http_status == 404
    assert decision.reason is ReasonCode.NOT_OWNER


def test_own_resource_is_allowed_and_carries_the_owner() -> None:
    """I-V16 后段：自己的 → 200，且带出来的 `Owner` 就是此人。"""
    decision = judge_access(
        ticket=ticket(),
        account=account(),
        now=NOW,
        resource=ResourceRef(tenant_id=TEAM_A, owner_user_id=ALICE),
    )

    assert decision.allowed
    assert decision.http_status == 200
    assert decision.owner == Owner(user_id=ALICE, tenant_id=TEAM_A)


def test_identity_is_judged_before_ownership() -> None:
    """顺序锁：同一份「他人资源」，未登录与已登录拿到**不同**的出口。

    这条同时断言 `reason`：仅仅「都拒绝了」不算数——`not_owner` 与
    `unauthenticated` 在运维那里是两种完全不同的排查方向。
    """
    foreign = ResourceRef(tenant_id=TEAM_A, owner_user_id=BOB)

    anonymous = judge_access(ticket=None, account=None, now=NOW, resource=foreign)
    signed_in = judge_access(ticket=ticket(), account=account(), now=NOW, resource=foreign)

    assert (anonymous.http_status, anonymous.reason) == (
        401,
        ReasonCode.UNAUTHENTICATED,
    )
    assert (signed_in.http_status, signed_in.reason) == (404, ReasonCode.NOT_OWNER)


def test_disabled_account_blocks_before_ownership_is_consulted() -> None:
    """停用的账户连「这条归不归你」都不该问——第 ② 步在第 ③ 步之前。"""
    decision = judge_access(
        ticket=ticket(),
        account=account(status=AccountStatus.DISABLED),
        now=NOW,
        resource=ResourceRef(tenant_id=TEAM_A, owner_user_id=BOB),
    )

    assert decision.http_status == 401
    assert decision.reason is ReasonCode.ACCOUNT_DISABLED


# ── 没有归属的数据行（I-V5 / AUTH-06） ──


@pytest.mark.parametrize("tenant", [None, "", "   "])
def test_resource_without_tenant_is_404_for_everyone_including_admin(
    tenant: str | None,
) -> None:
    """I-V5：`tenant_id` 为 NULL / 空串的行，对**任何人**都 404。

    包括管理员：宁可「数据看不见」（一查就能发现），也不要「所有人可见」
    （发现不了——它看起来一直是正常的）。
    """
    decision = judge_access(
        ticket=ticket(),
        account=account(role=Role.ADMIN),
        now=NOW,
        resource=ResourceRef(tenant_id=tenant, owner_user_id=ALICE),
    )

    assert decision.outcome is Outcome.NOT_FOUND
    assert decision.reason is ReasonCode.NOT_IN_TEAM


def test_account_without_a_tenant_is_denied_and_falls_back_to_nothing() -> None:
    """I-V5 后半：身份不自洽（账户没有归属）→ deny，**不回落到任何默认租户**。

    回落的形态就是 C# 版 `GetOwner()` 返回空串：空串在 SQL 里匹配不到任何行，
    于是每个请求都「什么都没查到」，看起来像数据没了，而不是像拒了。
    """
    decision = judge_access(ticket=ticket(), account=account(tenant_id=""), now=NOW)

    assert not decision.allowed
    assert decision.outcome is Outcome.UNAUTHENTICATED
    assert decision.owner is None


def test_other_team_resource_is_not_in_team() -> None:
    """第 ③ 步：别的团队 → 404 `not_in_team`（与「不是本人的」区分开）。"""
    decision = judge_access(
        ticket=ticket(),
        account=account(tenant_id=TEAM_A),
        now=NOW,
        resource=ResourceRef(tenant_id=TEAM_B, owner_user_id=ALICE),
    )

    assert decision.reason is ReasonCode.NOT_IN_TEAM


def test_team_wide_resource_skips_the_ownership_step() -> None:
    """`owner_user_id is None` = 团队级资源（文档）：判到第 ③ 步为止。"""
    decision = judge_access(
        ticket=ticket(),
        account=account(),
        now=NOW,
        resource=ResourceRef(tenant_id=TEAM_A),
    )

    assert decision.allowed


# ── 票据的三种失效（I-V9 的一半） ──


def test_expired_ticket_is_session_expired() -> None:
    decision = judge_access(ticket=ticket(expires_at=NOW), account=account(), now=NOW)

    assert decision.http_status == 401
    assert decision.reason is ReasonCode.SESSION_EXPIRED


def test_revoked_ticket_is_rejected_before_it_expires() -> None:
    """吊销不靠等过期：登出之后同一张票立刻不能用了。"""
    decision = judge_access(
        ticket=ticket(revoked_at=NOW - timedelta(minutes=1)),
        account=account(),
        now=NOW,
    )

    assert decision.reason is ReasonCode.SESSION_EXPIRED


def test_ticket_from_an_older_credential_epoch_is_rejected() -> None:
    """I-V9 的一半：改密让 `credential_epoch` 递增，旧票据的版本号落后 → 失效。

    这里刻意**不遍历删除**：删除是一批写操作，漏一条就留一条后门；
    版本号比较是一次整数比较，漏不掉。
    """
    decision = judge_access(ticket=ticket(epoch=1), account=account(epoch=2), now=NOW)

    assert decision.reason is ReasonCode.SESSION_EXPIRED


def test_ticket_pointing_at_another_account_is_indistinguishable_from_no_ticket() -> None:
    """票据与账户对不上时，出口与「没有票据」**逐字段相同**——
    分成两种响应等于承认「这个 user_id 曾经存在」。"""
    mismatch = judge_access(ticket=ticket(user_id=BOB), account=account(), now=NOW)
    absent = judge_access(ticket=None, account=None, now=NOW)

    assert mismatch.outcome is absent.outcome
    assert mismatch.reason is absent.reason


# ── 锁定与停用是两个维度（I-V7 / I-V8） ──


def test_locked_account_can_still_use_an_existing_session() -> None:
    """I-V8：账户被锁定，但**已登录的会话仍能访问业务接口**。

    这是本文件最重要的一条。锁定的意思是「不许再猜密码」，不是「把你踢下去」——
    否则任何人拿别人的邮箱连错五次，就能把对方挤下线，成本比猜密码低得多。
    改坏方式：把 `lockout_until` 的判定搬进 `judge_access` 的第 ② 步。
    """
    locked = account(lockout_until=NOW + timedelta(minutes=15))

    decision = judge_access(ticket=ticket(), account=locked, now=NOW)

    assert decision.allowed, "锁定不许影响已经签发的会话——那是可预测的 DoS"
    assert decision.outcome is Outcome.ALLOW


def test_locked_account_cannot_start_a_new_login() -> None:
    """同一份账户，换个入口（登录）就被拦——这才是锁定该起作用的地方。"""
    locked = account(lockout_until=NOW + timedelta(minutes=15))

    assert login_block_reason(locked, NOW) is ReasonCode.ACCOUNT_LOCKED_OUT


def test_lockout_expires_by_time_comparison_not_by_a_write() -> None:
    """到期即恢复：靠时间比较，不靠有人去把那一列清空。

    改坏方式：把 `lockout_until > now` 写成 `lockout_until is not None` ——
    账户就永久锁死了，而「永久」只在真出事后才被发现。
    """
    expired = account(lockout_until=NOW - timedelta(seconds=1))

    assert login_block_reason(expired, NOW) is None
    assert judge_access(ticket=ticket(), account=expired, now=NOW).allowed


def test_disabled_and_locked_report_different_reasons() -> None:
    """I-V7：两者在记录里必须可区分——恢复动作完全不同。

    停用要管理员手动恢复；锁定等时间到就行。合成一个 `enabled` 布尔之后，
    运维看到「账户不可用」无法判断该找谁。
    """
    disabled = account(status=AccountStatus.DISABLED)
    locked = account(lockout_until=NOW + timedelta(minutes=5))

    assert login_block_reason(disabled, NOW) is ReasonCode.ACCOUNT_DISABLED
    assert login_block_reason(locked, NOW) is ReasonCode.ACCOUNT_LOCKED_OUT
    # 锁定的人 `status` 仍是 active——这正是「不要合成布尔」的证据
    assert locked.status is AccountStatus.ACTIVE


def test_disabled_account_takes_precedence_over_lockout_in_the_record() -> None:
    """两个维度同时成立时，记录里先写「停用」——那是需要人动手的那个。"""
    both = account(status=AccountStatus.DISABLED, lockout_until=NOW + timedelta(minutes=5))

    assert login_block_reason(both, NOW) is ReasonCode.ACCOUNT_DISABLED


# ── 管理动作的边界（I-V11） ──


def test_the_authority_check_does_not_need_a_target() -> None:
    """`judge_admin` 是**动作级**判定：它连目标在哪都不知道。

    这正是它存在的理由。管理接口的三步顺序是
    「我是不是管理员 → 目标在不在本团队 → 目标本身合不合规」，
    少了第一步，非管理员拿一串 id 打接口时，404 与 403 的差别就成了
    「这个人在不在」的探针——而探测不需要任何权限。
    """
    assert judge_admin(actor=principal(role=Role.MEMBER)).outcome is Outcome.FORBIDDEN
    assert judge_admin(actor=principal(role=Role.ADMIN)).outcome is Outcome.ALLOW


def test_member_cannot_manage_anyone() -> None:
    decision = judge_management(
        actor=principal(role=Role.MEMBER),
        target_user_id=BOB,
        target_role=Role.MEMBER,
    )

    assert decision.http_status == 403
    assert decision.reason is ReasonCode.ROLE_INSUFFICIENT


def test_admin_cannot_disable_themselves() -> None:
    """I-V11：管理员停用自己 → 403。

    没有这一条，最后一个管理员能把自己锁在门外，而恢复需要另一个管理员——
    系统就再也进不去了。
    """
    decision = judge_management(
        actor=principal(user_id=ALICE, role=Role.ADMIN),
        target_user_id=ALICE,
        target_role=Role.ADMIN,
    )

    assert decision.http_status == 403


def test_admin_cannot_grant_a_role_above_their_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """I-V11：只校验「是不是管理员」不够——还要校验**目标角色**不高于自己。

    当前只有 `admin` / `member` 两档，`admin` 已是最高的，所以这一条**走不到**。
    这正是要测它的原因：它是为「以后加第三档角色」准备的，而加第三档的那天，
    没有这条测试的话，`judge_management` 会把「造一个比自己高的角色」放行，
    且没有任何东西会红。

    用 monkeypatch 临时把 `member` 抬到高于 `admin`，走一遍真实分支——
    比断言「函数里有一行比较」诚实，也比留一个 `# pragma: no cover` 强。
    """
    from agentworkbench.identity.models import ROLE_RANK

    monkeypatch.setitem(ROLE_RANK, Role.MEMBER, ROLE_RANK[Role.ADMIN] + 1)

    decision = judge_management(
        actor=principal(role=Role.ADMIN),
        target_user_id=BOB,
        target_role=Role.MEMBER,
    )

    assert decision.http_status == 403
    assert decision.reason is ReasonCode.ROLE_INSUFFICIENT


def test_admin_can_manage_a_member() -> None:
    decision = judge_management(
        actor=principal(role=Role.ADMIN),
        target_user_id=BOB,
        target_role=Role.MEMBER,
    )

    assert decision.allowed


# ── Decision 自己的一致性（I-V5 的可构造性） ──


def test_allow_without_a_principal_cannot_even_be_constructed() -> None:
    """**不带归属的放行不允许存在**，连构造都不行。

    这不是防御性编程：`Decision(Outcome.ALLOW)` 一旦被造出来，下游拿不到 `Owner`，
    仓储就会退化成「不带归属的查询」——而那个查询不会报错，只会返回全部。
    """
    with pytest.raises(ValueError):
        Decision(outcome=Outcome.ALLOW)


def test_a_denial_cannot_exist_without_a_reason_code() -> None:
    with pytest.raises(ValueError):
        Decision(outcome=Outcome.NOT_FOUND)


def test_a_denial_can_still_carry_who_was_denied() -> None:
    """拒绝时 `principal` 允许存在——拒绝记录要写「谁被拒了」。

    未认证时为 None，已认证越权时为非 None，两者都必须可构造：
    把 `principal` 一律禁掉，`I-V4` 的记录就永远填不出 actor。
    """
    denied = judge_access(
        ticket=ticket(),
        account=account(),
        now=NOW,
        resource=ResourceRef(tenant_id=TEAM_A, owner_user_id=BOB),
    )

    assert not denied.allowed
    assert denied.actor_user_id == ALICE
    assert denied.tenant_id == TEAM_A


# ── 三台状态机（I-V6 / I-V13 的判定面） ──


def test_same_state_transition_is_illegal() -> None:
    """重复停用同一个账户第二次会被拒，而不是静默成功。

    静默成功会让「是谁改的、什么时候改的」这类问题在日志里出现两条一样的记录。
    """
    assert not can_transition("account_status", "active", "active")
    assert not can_transition("ticket_lifecycle", "issued", "issued")


def test_terminal_states_have_no_way_out() -> None:
    """吊销的票据不会变回可用，接受的邀请不会变回待接受。

    这不是「没写」：终态必须显式是不可离开的，否则以后有人加一条
    `revoked → issued` 时，没有任何东西会拦住他。
    """
    for source in ("revoked", "expired"):
        for target in ("issued", "revoked", "expired"):
            assert not can_transition("ticket_lifecycle", source, target)
    for source in ("accepted", "expired"):
        for target in ("pending", "accepted", "expired"):
            assert not can_transition("invite_lifecycle", source, target)


def test_illegal_transition_raises_instead_of_returning_false() -> None:
    with pytest.raises(IllegalTransition):
        require_transition("account_status", "disabled", "disabled")


def test_unknown_state_is_a_programming_error_not_a_denial() -> None:
    """状态名写错要抛，不能当「不合法」返回 False——那会把代码错误伪装成业务拒绝。"""
    with pytest.raises(KeyError):
        can_transition("account_status", "suspended", "disabled")


def test_transition_tables_match_the_states_registered_in_toml() -> None:
    """代码里的状态集与 toml 登记的状态集必须一致。

    两处各写一遍是刻意的（toml 给人看、代码给人执行），但**不一致必须被发现**：
    只在 toml 加了状态、代码没加，表现是「那个状态永远迁移不进去」，
    而报错是一句没人看的 KeyError。
    """
    with (ROOT / "pyproject.toml").open("rb") as handle:
        registered = tomllib.load(handle)["tool"]["agentworkbench"]["state_machine"]
    ours = {
        row["name"]: {state.strip() for state in row["states"].split(",")}
        for row in registered
        if row["source"] == "agentworkbench.identity.policy"
    }

    assert ours, "toml 里没有登记任何归属 identity.policy 的状态机"
    for name, states in ours.items():
        assert set(TRANSITIONS[name]) == states, f"{name} 的状态集与 toml 不一致"


# ── 引导只能一次（I-V12 的判定面） ──


def test_bootstrap_is_open_only_on_an_empty_library() -> None:
    """判据是「库里有没有用户」，不是「有没有跑过这个命令」。"""
    assert can_bootstrap(0)
    assert not can_bootstrap(1)
    assert not can_bootstrap(100)
