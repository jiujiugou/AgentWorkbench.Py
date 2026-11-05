"""成员管理路由：列表、发邀请、改角色、停用、恢复（REQ-03 / AUTH-10）。

**每一次写动作的顺序都是「取目标 → 判 → 写」。** 判定必须在写之前，
不是因为它更好看，而是因为 `I-V11` 的判据是「停用自己被 403 **且 status 不变**」——
先写后判的实现会返回 403，但那一行已经变了，而 403 会让调用方以为什么都没发生。

判定用的是 `policy.judge_management`，它是**全项目唯一用 403 的地方**：
按动作调管理接口不泄露任何资源是否存在（动作本身是公开的），
而按 id 取资源必须用 404（`I-V16`）。

**这里没有「删除成员」。** §2.1 的账号生命周期属于重开条件之一：删掉一个
`users` 行会牵动 F5 的会话归属与 F4 的轨迹归属（那个人写过的东西要么跟着消失、
要么变成孤儿），而这不是「管理员点一下」应该承担的后果。停用已经解决了
「他不能再进来」，删除解决的是「他的数据该怎么处理」——那是另一个问题，
要另一个判据。hard delete 之前先回答：他上传的文档归谁？

服务于：REQ-03 / REQ-AUTH-10 / I-V11 / I-V13
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from typing import cast

from fastapi import APIRouter, Request

from agentworkbench.api import identity_context
from agentworkbench.api.deps import Deps
from agentworkbench.api.errors import not_found
from agentworkbench.api.schemas import ApiModel, UserDto
from agentworkbench.core.errors import AppError, ErrorCode
from agentworkbench.identity import policy, service, store
from agentworkbench.identity.models import Account, Principal, Role

router = APIRouter()

_MEMBER_MISSING = "成员不存在。"


def _deps(request: Request) -> Deps:
    return cast(Deps, request.app.state.deps)


def _dto(account: Account) -> UserDto:
    return UserDto(
        id=account.user_id,
        email=account.email,
        role=str(account.role),
        status=str(account.status),
        tenant_id=account.tenant_id,
    )


def _role_of(raw: str) -> Role:
    """请求体里的角色串 → 枚举。非法值给 400，不让 `ValueError` 冒成 500。

    500 与 400 的差别在这里不只是数字：400 是「你传错了」，
    500 是「服务器坏了」——后者会让调用方去重试，而重试一万次也是同一个错。
    """
    try:
        return Role(raw)
    except ValueError:
        raise AppError(ErrorCode.VALIDATION_FAILED, "角色只能是 admin 或 member。") from None


def _admin(request: Request, conn: sqlite3.Connection) -> Principal:
    """取当前身份，并确认是管理员。**必须在查目标之前调用。**

    顺序就是判据：先判「你有没有资格调这个动作」（403，不需要目标），
    再查目标（404），最后判目标本身（403）。反过来写，非管理员拿一串 id
    打管理接口，404 与 403 的差别就成了「这个人在不在」的探针——
    而这一步不需要任何权限就能试。

    实现在 `identity_context.require_admin`：删除文档（ADR 0008）用的是同一条
    规则，两份实现迟早分岔，而分岔的那一次不会报错。
    """
    del conn  # 保留参数是为了不改三处调用点；判定不需要连接
    return identity_context.require_admin(request)


def _target(conn: sqlite3.Connection, principal: Principal, user_id: str) -> Account:
    """取目标成员，**不带团队条件就不返回**。

    别人的团队成员与不存在的成员走同一个 404、同一句话：两者可区分的话，
    拿一串 id 就能试出「这个系统里有几个团队、每个团队有几个人」。
    """
    account = store.get_user(conn, user_id)
    if account is None or account.tenant_id != principal.owner.tenant_id:
        raise not_found(_MEMBER_MISSING)
    return account


class InviteBody(ApiModel):
    """发邀请。`email` 可选：不填就是「谁拿到链接谁都能用」的通用邀请。"""

    role: str
    email: str | None = None


class ChangeRoleBody(ApiModel):
    role: str


class InviteDto(ApiModel):
    """一次性邀请。**`token` 只在这里出现一次**，库里只有它的 SHA-256。

    没有「再取一次邀请链接」的接口：那等于把库里的凭证重新变成可用凭证，
    而「备份泄露不等于能加团队」正是哈希存储换来的东西。
    """

    invite_id: str
    token: str


@router.get("/api/members")
def list_members(request: Request) -> list[UserDto]:
    """本团队成员列表。**普通成员也能看**（REQ-03「成员可见」）。

    团队条件在 SQL 里（`store.list_users` 的 where），不在 Python 里过滤：
    后者要先把别人的行读进内存再丢掉，而「读过」与「没读过」在日志与
    未来的缓存里是不一样的。
    """
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        principal = identity_context.principal_of(identity_context.decision_for(conn, request))
        accounts = store.list_users(conn, principal.owner.tenant_id)
    return [_dto(account) for account in accounts]


@router.post("/api/members/invites", status_code=201)
def create_invite(body: InviteBody, request: Request) -> InviteDto:
    """发一张邀请。只有管理员能发，且邀请上的角色不得高于自己。"""
    deps = _deps(request)
    role = _role_of(body.role)
    with closing(deps.open_conn()) as conn:
        principal = identity_context.principal_of(identity_context.decision_for(conn, request))
        identity_context.principal_of(policy.judge_invite(actor=principal, target_role=role))
        invite_id, token = service.create_invite(
            conn,
            tenant_id=principal.owner.tenant_id,
            role=role,
            # 邀请的创建者是当前这个人，不是「系统」。没有它，一张被转发的
            # 邀请事后追不到任何来源。
            created_by=principal.owner.user_id,
            now=datetime.now(UTC),
            email=body.email,
        )
    return InviteDto(invite_id=invite_id, token=token)


@router.patch("/api/members/{user_id}")
def change_role(user_id: str, body: ChangeRoleBody, request: Request) -> UserDto:
    """改角色。目标角色不得高于自己，也不能改自己（`AUTH-10`）。"""
    deps = _deps(request)
    role = _role_of(body.role)
    with closing(deps.open_conn()) as conn:
        principal = _admin(request, conn)
        target = _target(conn, principal, user_id)
        identity_context.principal_of(
            policy.judge_management(
                actor=principal, target_user_id=target.user_id, target_role=role
            )
        )
        changed = service.change_role(
            conn,
            user_id=target.user_id,
            # expected 取**刚读到的那一行**，不是请求体：拿请求体当 expected，
            # 这次 CAS 永远成立，等于没有 CAS。
            expected=target.role,
            target=role,
            now=datetime.now(UTC),
        )
        if not changed:
            # 读与写之间有人改过这一行。409 而不是 200：调用方看到的角色
            # 与他刚提交的那次操作不一致，而返回 200 会让界面显示一个假状态。
            raise AppError(ErrorCode.VALIDATION_FAILED, "这个成员的角色刚刚被其他人修改过。")
        updated = store.get_user(conn, target.user_id)
    if updated is None:  # pragma: no cover —— 同一事务里刚写过的行
        raise not_found(_MEMBER_MISSING)
    # 降职/升职必须立刻体现在下一次判定上：缓存里那份 `Principal` 还带着旧角色。
    deps.session_cache.forget_user(target.user_id)
    return _dto(updated)


@router.post("/api/members/{user_id}/disable", status_code=204)
def disable_member(user_id: str, request: Request) -> None:
    """停用：置 disabled 并吊销该成员全部票据（同一事务，`I-V6`）。

    停用自己 → 403 且 `status` 不变。这条判定在写之前，
    理由见模块 docstring。
    """
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        principal = _admin(request, conn)
        target = _target(conn, principal, user_id)
        identity_context.principal_of(
            policy.judge_management(
                actor=principal, target_user_id=target.user_id, target_role=target.role
            )
        )
        if not service.disable_user(conn, user_id=target.user_id, now=datetime.now(UTC)):
            raise AppError(ErrorCode.VALIDATION_FAILED, "这个成员已经是停用状态。")
    # 停用的意义是「立刻踢下线」。库里的票已全吊销，缓存不清就还得等一个窗口——
    # 而那个窗口里他仍是管理员，还能改别人的角色（D9 红队实测：改动是永久的）。
    deps.session_cache.forget_user(target.user_id)


@router.post("/api/members/{user_id}/enable", status_code=204)
def enable_member(user_id: str, request: Request) -> None:
    """恢复。**不动锁定**——两列各自恢复（`I-V7`），顺手解锁等于绕过失败计数。"""
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        principal = _admin(request, conn)
        target = _target(conn, principal, user_id)
        identity_context.principal_of(
            policy.judge_management(
                actor=principal, target_user_id=target.user_id, target_role=target.role
            )
        )
        if not service.enable_user(conn, user_id=target.user_id, now=datetime.now(UTC)):
            raise AppError(ErrorCode.VALIDATION_FAILED, "这个成员当前不是停用状态。")
    # 恢复同理：缓存里若有他停用前的身份，状态字段会与库不一致。
    deps.session_cache.forget_user(target.user_id)
