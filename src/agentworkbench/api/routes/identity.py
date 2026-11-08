"""身份路由：登录、登出、我是谁、改密、接受邀请（REQ-01 / 09 / AUTH-11）。

**这个模块是「先于身份」的唯一写入口。** 登录与接受邀请发生在没有任何会话的
时候，所以它们必须自己成事，不能依赖全局依赖——否则会变成「要先登录才能登录」。

三条纪律：

1. **失败只有一种说法。** 口令错、账号不存在、已停用、锁定中，四种情况
   返回同一个码同一句话（`I-H2`）。`service.login` 的返回值里带着具体原因，
   **这里不读它**——读了就会有人忍不住拼一句「您的账号已被停用」。
2. **票据只进 cookie。** 响应体里没有 token，也没有任何「把 cookie 手动带上」
   的字段。写进 body 的令牌会被日志、前端 store、错误上报各存一份，
   而它本来只有一个存放处。
3. **改密不换 cookie。** 发起改密的那张票在服务端被升到新一代（`I-V9`），
   客户端什么都不用做；这里也**不重设** cookie——重设就会掩盖「保留的票没升级」
   这个 bug（那正是格 3 最隐蔽的坑）。

服务于：REQ-01 / REQ-AUTH-09 / I-V9 / I-V14 / I-H2
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from typing import cast

from fastapi import APIRouter, Request, Response

from agentworkbench.api import identity_context
from agentworkbench.api.deps import Deps
from agentworkbench.api.errors import not_found
from agentworkbench.api.schemas import ApiModel, AuthUserDto
from agentworkbench.core.errors import AppError, ErrorCode
from agentworkbench.db.repositories import knowledge as knowledge_repo
from agentworkbench.identity import credentials, service, store
from agentworkbench.identity.models import Account

router = APIRouter()

# 登录失败的唯一文案。**四种原因共用这一句**，见模块 docstring 第 1 条。
_LOGIN_FAILED = "邮箱或口令不正确。"

MIN_PASSWORD_LENGTH = 12
"""新口令的最小长度。**它是门槛，不是强度策略**：这里只拦“空/太短”，
真正的强度策略（常见口令、复用历史）需要另立规则与数据。"""


def _deps(request: Request) -> Deps:
    return cast(Deps, request.app.state.deps)


def _config(deps: Deps) -> service.AuthConfig:
    """每个请求现算一次。5 个字段，比缓存与失效逻辑便宜。"""
    return service.AuthConfig.from_settings(deps.settings)


def _dto(account: Account) -> AuthUserDto:
    return AuthUserDto(
        id=account.user_id,
        email=account.email,
        role=str(account.role),
        status=str(account.status),
        tenant_id=account.tenant_id,
        level_rank=account.level_rank,
        must_change_password=account.must_change_password,
    )


class LoginBody(ApiModel):
    email: str
    password: str


class InviteBody(ApiModel):
    """接受邀请：口令由被邀请人自己定，邮箱可以不等于邀请上写的那个。

    不要求相等是刻意的——邀请上的邮箱是「发给谁」，而登录身份是
    「他是谁」。定向邀请仍然只对那个邮箱有效（`service.accept_invite` 里判），
    所以这里收到别的邮箱会在服务层被拒，不是这里放行。
    """

    email: str
    password: str


class ChangePasswordBody(ApiModel):
    old_password: str
    new_password: str


@router.post("/api/auth/login")
def login(body: LoginBody, request: Request, response: Response) -> AuthUserDto:
    """登录。成功签发一张票（明文进 cookie，库里只有 SHA-256）。

    响应体里**没有令牌**：见模块 docstring 第 2 条。
    """
    deps = _deps(request)
    config = _config(deps)
    with closing(deps.open_conn()) as conn:
        result = service.login(
            conn,
            email=body.email,
            password=body.password,
            now=datetime.now(UTC),
            config=config,
        )
    if isinstance(result, service.LoginRejected):
        # `result.reason` 在这里**故意不读**：它只该进拒绝记录（格 6）。
        raise AppError(ErrorCode.INVALID_CREDENTIALS, _LOGIN_FAILED)
    identity_context.attach_cookie(response, result.ticket, config=config)
    return _dto(result.account)


@router.post("/api/auth/logout", status_code=204)
def logout(request: Request, response: Response) -> None:
    """登出：吊销库里的那一行 + 让浏览器丢掉 cookie。

    **幂等**：没带 cookie、或票据本来就不存在，同样 204。登出失败没有任何
    用户能做的补救动作，把它报成错误只会让人重试。
    """
    deps = _deps(request)
    plain = identity_context.ticket_from(request)
    if plain is not None:
        with closing(deps.open_conn()) as conn:
            service.logout(conn, ticket=plain, now=datetime.now(UTC))
        # 库里那一行已经吊销，但缓存里还留着「这张票验过了」。不清掉的话，
        # 登出后的下一个请求照样 200（D9 红队实测，窗口由配置决定，默认曾是 60 秒）。
        deps.session_cache.forget_ticket(credentials.hash_ticket(plain))
    identity_context.clear_cookie(response)


@router.get("/api/auth/me")
def me(request: Request) -> AuthUserDto:
    """我是谁。前端刷新页面后靠它判断「要不要跳登录页」。"""
    deps = _deps(request)
    principal = identity_context.current_principal(request)
    with closing(deps.open_conn()) as conn:
        account = store.get_user(conn, principal.owner.user_id)
    if account is None:  # pragma: no cover —— 票据指向的账户在判定阶段必然存在
        raise AppError(ErrorCode.NOT_AUTHENTICATED, "未登录或登录已失效，请重新登录。")
    return _dto(account)


def _require_password(value: str) -> None:
    """拒绝空口令（含纯空白）。

    为什么单独立一条而不是写进 pydantic 的 `min_length`：这里要表达的不是「长度不够」
    而是「口令是唯一认证因素，它不能是空的」。D9 红队实测：`POST /api/auth/invites/<t>`
    与 `POST /api/auth/password` 都接受 `""`，于是能建出一个**任何知道邮箱的人
    都能登录**的账号，而它在成员列表里与正常账号长得一模一样（`redteam-A.md` R-02）。

    没有顺手加上「至少 8 位」：产品从没承诺过长度策略，运维手上可能有一批既有账号，
    凭空加一条会让它们在下一次改密时突然失败。空与非空是**缺陷**，长度是**政策**，
    政策要单独拍板。
    """
    if not value.strip():
        raise AppError(ErrorCode.VALIDATION_FAILED, "口令不能为空。")
    if len(value) < MIN_PASSWORD_LENGTH:
        raise AppError(ErrorCode.VALIDATION_FAILED, f"口令至少 {MIN_PASSWORD_LENGTH} 位。")


@router.post("/api/auth/password", status_code=204)
def change_password(body: ChangePasswordBody, request: Request) -> None:
    """本人改密。成功后：当前这张票仍可用，其余全部失效（`I-V9`）。

    旧口令错 → 401 而不是 400：这是一次凭据校验失败，不是参数格式问题。
    用 400 会让前端把它归到「输入有误、改一下再提交」那一类，
    而实际要表达的是「你得先证明你是本人」。
    """
    _require_password(body.new_password)
    deps = _deps(request)
    principal = identity_context.current_principal(request)
    with closing(deps.open_conn()) as conn:
        plain = identity_context.ticket_from(request)
        # 走到这里 `plain` 必非空：`principal` 只在票据有效时才存在。
        try:
            changed = service.change_password(
                conn,
                user_id=principal.owner.user_id,
                current_ticket=plain or "",
                old_password=body.old_password,
                new_password=body.new_password,
                now=datetime.now(UTC),
            )
        except service.PasswordUnchanged:
            raise AppError(ErrorCode.VALIDATION_FAILED, "新口令不能与当前口令相同。") from None
    if not changed:
        raise AppError(ErrorCode.INVALID_CREDENTIALS, "当前口令不正确。")
    # 改密把 `credential_epoch` 加一、并吊销其余票据；缓存里那些旧身份同样要摘掉，
    # 否则「另一台设备」在本进程内还能用满一个窗口（`I-V9` 要的是立即失效）。
    # 当前这张票会被重新判一次，而它已被刷到新 epoch，所以照旧 200。
    deps.session_cache.forget_user(principal.owner.user_id)


@router.post("/api/auth/invites/{token}", status_code=201)
def accept_invite(
    token: str, body: InviteBody, request: Request, response: Response
) -> AuthUserDto:
    """凭邀请建号，建完直接登录（201 = 建了一个新账号）。

    邀请的四种不可用形态（不存在 / 已用 / 过期 / 邮箱不符）**共用同一个 404
    与同一句话**（`I-V13`）。区别只在服务层内部的那个异常消息里，
    而这里连消息都不读——读了就会有人把它转成给用户看的提示，
    而那句话等于告诉对方「这张邀请曾经存在」。
    """
    _require_password(body.password)
    deps = _deps(request)
    config = _config(deps)
    try:
        with closing(deps.open_conn()) as conn:
            accepted = service.accept_invite(
                conn,
                token=token,
                email=body.email,
                password=body.password,
                now=datetime.now(UTC),
                config=config,
            )
            knowledge_repo.insert_admin_event(
                conn,
                actor_user_id=accepted.account.user_id,
                tenant_id=accepted.account.tenant_id,
                target_user_id=accepted.account.user_id,
                action="user.accept_invite",
                resource_kind="user",
                resource_id=accepted.account.user_id,
                old_value=None,
                new_value=f"{accepted.account.role}:{accepted.account.level_rank}",
                request_id=str(getattr(request.state, "request_id", "") or "") or None,
                now=datetime.now(UTC),
            )
            conn.commit()
    except service.InviteUnusable:
        raise not_found("邀请不存在或已失效。") from None
    except service.EmailAlreadyTaken:
        # 这句仍是明说的（「你已经有账号了，直接登录」是邀请流程里必须有的一句话），
        # 但**代价变了**：服务层在抛异常之前已经把这张邀请烧掉，于是一张邀请
        # 最多换到一个答案。此前这条路径不消耗令牌，一张邀请就能把一份邮箱名单
        # 逐个问一遍（D9 红队 A/B 两组各自复现）。
        raise AppError(
            ErrorCode.VALIDATION_FAILED, "这个邮箱已经有账号了，直接登录即可。"
        ) from None
    identity_context.attach_cookie(response, accepted.ticket, config=config)
    return _dto(accepted.account)
