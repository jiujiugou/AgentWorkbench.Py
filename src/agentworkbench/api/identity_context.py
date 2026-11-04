"""请求侧的身份：cookie → 票据 → 账户 → 判定四步。

**这是 api 层与 `identity` 之间唯一的一道门。** 路由不许自己读 cookie、
不许自己查票据、更不许自己判「这个人能不能」。理由不是洁癖：判定四步的
**顺序**就是判据（`I-V16`），而顺序只有在同一个函数里才看得见。散到各路由
之后，漏掉的那一处不报错，只是「登录态成了可推断的信息」。

**格 6 会把 `decision_for` 变成全局依赖。** 现在只有 members / auth 几条路由
主动调它，是因为阶段 1 的 65 处仓储调用还在用固定归属（格 5 换血、格 6 挂闸）。
到那时这个模块不删——它正是那个依赖的实现，只是调用点从十几处收敛成一处。

---

**CSRF 的做法：用浏览器自己的机制，不自己发令牌。**

cookie 承载会话之后，跨站表单就成了真问题。行业里的标准形态是
`SameSite=Lax`：浏览器不把 cookie 带进跨站 POST，语义由浏览器实现，
而不是由我们「在每一个写入口盖一个章」。自己生成 CSRF token 的那套，
漏一个端点就漏一个入口，而测试很难证明「每个写入口都盖了章」——
这正是 §1.3 反对的「靠记得」型防线。

代价说清楚：`SameSite=Lax` 不拦**同站**的跨站请求（子域之间），也不拦
客户端脚本（那本来就不是 CSRF）。本产品单进程单域名部署，两者都不在射程内；
将来做多子域时，这一条要重新评估，而不是继续加令牌。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from typing import Final, cast

from fastapi import Request, Response

from agentworkbench.api.deps import Deps
from agentworkbench.core.errors import AppError, ErrorCode
from agentworkbench.core.identity import Owner
from agentworkbench.identity import credentials, policy, service, store
from agentworkbench.identity.models import Outcome, Principal, ReasonCode
from agentworkbench.identity.sessions import SessionCache

__all__ = [
    "COOKIE_NAME",
    "PUBLIC_PATHS",
    "attach_cookie",
    "clear_cookie",
    "current_principal",
    "decision_for",
    "owner_of",
    "principal_of",
    "require_session",
    "ticket_from",
]

COOKIE_NAME: Final[str] = "awb_session"
"""会话 cookie 的名字。带 `awb_` 前缀是为了和同域下别的东西（反代的、
监控的）区分开——排查「为什么这个 cookie 一直带着」时省一次猜测。"""

PUBLIC_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/api/health",
        "/api/auth/login",
        # 接受邀请的人**还没有账号**，当然也没有会话。把它放进白名单不是放宽，
        # 是这条路径的定义：邀请令牌本身就是凭证，而它只能建一次号（I-V13）。
        "/api/auth/invites/{token}",
    }
)
"""不需要登录就能访问的路径（`I-V1`）。

**路径模板，不是运行时路径**：`/api/auth/invites/{token}` 在路由表里就叫这个名字，
匹配时用的是模板。用真实 URL 写白名单的版本，会在第一次有人真接受邀请时
把人挡在门外——而测试里如果一直用固定的假 token，那也发现不了。

这张表**必须比路由表小得多**。它每多一条，`I-V1` 的覆盖面就少一块，
而「加进白名单」这个动作在代码里看起来只是个字符串。
"""

_FRAMEWORK_PATHS: Final[frozenset[str]] = frozenset(
    {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
)
"""框架自带的文档路由。**单独一张表，不混进 `PUBLIC_PATHS`。**

两者都是「不需要登录」，但理由完全不同：`PUBLIC_PATHS` 里的三条是**业务决定**
（这三件事在拿到身份之前就要能做），而 `/docs` 是 FastAPI 自带的、和产品无关。

混在一起的话，`PUBLIC_PATHS` 就不再是「产品允许的匿名入口」那张清单——
而它被反复检查、反复读，正是因为它足够短、每一条都能说出理由。
"""


def ticket_from(request: Request) -> str | None:
    """取 cookie 里的票据明文。没有就是 `None`，不编一个空串——
    空串会一路走到 `hash_ticket`，得到一个「存在但永不匹配」的摘要，
    排查时看到的是「票据无效」而不是「没带票据」。"""
    return request.cookies.get(COOKIE_NAME)


def decision_for(
    conn: sqlite3.Connection,
    request: Request,
    *,
    now: datetime | None = None,
) -> policy.Decision:
    """把一个请求走完判定四步的前两步（身份与账户）。

    第 ③④ 步（资源归属）不在这里：那需要具体资源，由路由在读到那一行之后
    用 `policy.judge_access(resource=...)` 补。**顺序仍然是保证的**——
    本函数先跑完 ①②，路由才可能拿到 `principal` 去读资源。

    `now` 可注入是为了让「票据过期」这类断言不用 sleep（代码规范 §6.4）。
    """
    moment = now or datetime.now(UTC)
    plain = ticket_from(request)
    row = None if plain is None else store.get_ticket_by_hash(conn, credentials.hash_ticket(plain))
    account = None if row is None else store.get_user(conn, row.user_id)
    return policy.judge_access(ticket=row, account=account, now=moment)


_STATE_PRINCIPAL: Final[str] = "principal"
"""`request.state` 上的键名。取值只有一处写（`require_session`）、两处读
（`owner_of` / `current_principal`）——写成字面量三遍也行，但它一旦拼错，
错的那一处是 `getattr(..., None)`，会安静地退回「再判一次」而不是报错。"""


def require_session(request: Request) -> None:
    """**全局闸门**（`I-V1`）：白名单之外，没有有效票据一律 401。

    在 `create_app` 里挂成 app 级依赖，于是它对**每一条**路由生效——包括将来新增
    的那些。这正是它存在的理由：在此之前，documents / conversations / knowledge /
    runs 四条路由各自记得调 `owner_of`，而 `settings` 下的六条一次都没调。
    「各自记得」的防线在新增第 21 条路由那天就会破一次，而那次没有测试会红
    （新路由自己没写测试的话）。

    白名单用**路由模板**比对：`/api/auth/invites/{token}` 在路由表里就叫这个名字，
    而 `request.scope["route"].path` 拿到的也是模板。用真实 URL 比对的白名单在
    第一次有人真接受邀请时把人挡在门外——而那是一个「新用户第一次进来」的时刻。

    拒绝时**不给 `request.state` 留任何东西**：拒绝记录要的是「谁被拒了」，
    而那个人在这条路径上本来就没有身份；留一个半成品反而会让下游以为验过了。
    """
    template = _template_of(request)
    if template in PUBLIC_PATHS or template in _FRAMEWORK_PATHS:
        return

    deps = cast(Deps, request.app.state.deps)
    with closing(deps.open_conn()) as conn:
        principal = _judge(conn, request, cache=deps.session_cache)
    setattr(request.state, _STATE_PRINCIPAL, principal)


def _template_of(request: Request) -> str:
    """路由模板。取不到时返回空串——空串不在白名单里，于是按「要登录」处理。

    取不到只发生在没匹配到路由的请求上，而那种请求根本走不到依赖这一步；
    留这个分支是为了让「认不出来」的默认行为是**更严**，不是更松。
    """
    return str(getattr(request.scope.get("route"), "path", "") or "")


def _judge(
    conn: sqlite3.Connection,
    request: Request,
    *,
    cache: SessionCache,
) -> Principal:
    """判定四步的前两步，带一层短期缓存（`SessionCache`）。"""
    plain = ticket_from(request)
    if plain is None:
        raise AppError(
            ErrorCode.NOT_AUTHENTICATED,
            _REJECTION[Outcome.UNAUTHENTICATED][1],
            reason=str(ReasonCode.UNAUTHENTICATED),
        )

    digest = credentials.hash_ticket(plain)
    cached = cache.get(digest)
    if cached is not None:
        return cached

    principal = principal_of(decision_for(conn, request))
    cache.put(digest, principal)
    return principal


def current_principal(request: Request) -> Principal:
    """全局闸门验过之后的那份身份。

    **调用点用它，别再用 `decision_for` 重判一次**：闸门已经判过，重判是同一份工作
    做两遍，而更糟的是两处都有了「要不要放行」的权力——将来只改了其中一处，
    另一处仍然按旧规则放行，且没有任何测试会同时覆盖两条路径。

    抛 `RuntimeError`（→ 500）而不是 401：走到这里而 `request.state` 是空的，
    说明这条路由没经过闸门。那不是「用户没登录」，是**装配错了**，
    用 401 会把它伪装成一个用户可以自己解决的登录问题。
    """
    principal = getattr(request.state, _STATE_PRINCIPAL, None)
    if principal is None:
        raise RuntimeError(
            "路由没经过全局闸门就取了身份：检查 create_app 的 dependencies 是否被绕过"
        )
    return cast(Principal, principal)


# 拒绝 → 错误码与文案。**一张表，三个出口（`I-V16` 只认这三种）。**
# 文案里不许出现「账号被停用」「已锁定」「不存在」——三种情况在界面上必须
# 一模一样，否则响应本身就成了一台探测仪（`I-H2`）。
_REJECTION: Final[dict[Outcome, tuple[ErrorCode, str]]] = {
    Outcome.UNAUTHENTICATED: (
        ErrorCode.NOT_AUTHENTICATED,
        "未登录或登录已失效，请重新登录。",
    ),
    Outcome.NOT_FOUND: (ErrorCode.NOT_FOUND, "资源不存在。"),
    Outcome.FORBIDDEN: (
        ErrorCode.FORBIDDEN,
        "这个操作需要更高的权限，或不能对自己执行。",
    ),
}
"""`ALLOW` 不在表里：放行没有错误码，`principal_of` 直接返回。

漏一个 `Outcome` 的后果是 `KeyError` → 500，而不是「静默放行」——
这是刻意的一侧：拒绝路径上出错宁可 500，也不要悄悄给一个空 `Principal`。

**403 的文案要同时覆盖两种拒绝**：团队管理（「你是管理员吗」）与平台配置
（「你是不是这套部署的所有者」，`REQ-AUTH-12`）。所以它写的是「更高的权限」，
不写「管理员权限」——后者会让一个团队管理员跑去找人要管理员身份，而他要不到
的东西根本不是管理员。具体原因在记录里（`reason_code` 分得开），不在这里。
"""


def principal_of(decision: policy.Decision) -> Principal:
    """拒绝就抛，放行就返回身份。**调用点拿不到 `Decision`，只拿得到 `Principal`。**

    这样写是为了让「忘了处理拒绝」不可能：`Decision` 只在路由内部存活几行，
    而 `Principal` 是「已验过票」的类型证据（`models.Principal` 的 docstring）。

    **判断必须是 `decision.allowed`，不能是 `principal is not None`。**
    拒绝的 `Decision` 也可能带着 principal——`judge_management` 拒一个管理员时
    会把它带出来，因为拒绝记录要写「谁被拒了」（`Decision.actor_user_id`）。
    用「有没有 principal」判放行的版本，会把每一次 403 都变成放行，
    而 `judge_access` 的 401 因为不带 principal 仍然正确——
    于是这个 bug 只在管理接口上出现，而那些接口恰好是最少被点的那几个。

    抛出的 `AppError` 带 `reason`：对外一视同仁，对内要分得清（`api/refusals.py`
    把它写进记录）。这一步不做的话，「六种原因为什么都是 401」就永远答不上来。
    """
    if decision.allowed and decision.principal is not None:
        return decision.principal
    code, message = _REJECTION[decision.outcome]
    raise AppError(code, message, reason=None if decision.reason is None else str(decision.reason))


def owner_of(conn: sqlite3.Connection, request: Request) -> Owner:
    """当前请求的归属。**路由拿这个去调仓储，不自己拼 `Owner`。**

    它是「换血」的落点：阶段 1 的 `deps.caller_of()` 返回固定值
    `Owner("local", settings.tenant_id)`，现在换成登录值。签名没变——
    `insert_message(conn, owner, ...)` 那 65 处调用一行都不用动，
    这正是当初把归属做成一个类型而不是两个字符串的收益。

    闸门跑过之后这里只是一次取值（`request.state`），不再查库。**保留了「没跑过闸门
    就自己判一次」的退路**：那条退路让直接调用路由函数的代码（以及将来可能出现的
    非 HTTP 入口）仍然是安全的，而它的存在不影响闸门的必要性——闸门管的是
    「有没有登录」，这里管的是「归属是谁」，两件事。
    """
    principal = getattr(request.state, _STATE_PRINCIPAL, None)
    if principal is not None:
        return cast(Principal, principal).owner
    return principal_of(decision_for(conn, request)).owner


def attach_cookie(response: Response, ticket: str, *, config: service.AuthConfig) -> None:
    """把票据写进 cookie。**明文只在这里出现一次**，之后只在浏览器里。"""
    response.set_cookie(
        key=COOKIE_NAME,
        value=ticket,
        # `max_age` 与库里 `expires_at` 是两件事：前者让浏览器按时丢掉，
        # 后者是服务端的判定。两者不一致时**服务端更严**（库里先过期），
        # 所以浏览器多留一会儿不会延长任何权限。
        max_age=config.ticket_ttl_hours * 3600,
        # httponly：JS 读不到。XSS 仍在，但至少不是「一行 document.cookie 就能带走会话」。
        httponly=True,
        # 见模块 docstring：这是本项目唯一的 CSRF 防线，且它是浏览器实现的。
        samesite="lax",
        # 私有化内网多是 http，配置默认 false；对外部署必须改 true（§1.8）。
        secure=config.cookie_secure,
        path="/",
    )


def clear_cookie(response: Response) -> None:
    """删 cookie。属性要与设置时一致，否则浏览器可能留下另一条同名的。"""
    response.delete_cookie(key=COOKIE_NAME, path="/", httponly=True, samesite="lax")
