"""拒绝记录的唯一写入口（F1 §1.3 / `I-V4`）。

## 为什么写在异常处理里，而不是散在各处 `raise` 之前

判据要的是「**一次越权 404 之后，按 `X-Request-Id` 能查到一条记录**」。这句话的对手
不是「写得对不对」，是「有没有漏」。路由里有 15 个 `not_found(...)` 出口，将来还会有
第 16 个；每一个都是一个人要记得调一次记录函数，而**忘掉的那个不会报错**——
它只是安静地少了一条证据，直到某天有人问「这个人到底有没有被拒过」。

所以记录挂在 `AppError` 的处理上：**一次拒绝必然经过这里**，而它是一次同步调用，
发生在响应发出之前。判据里那条负控（「关闭写入后同一压测 p95 必须下降」）要的正是
这个顺序——异步补记的实现，压测不会变慢，而那条记录会随机丢。

## 记什么、不记什么

记：谁（`actor_user_id`，未登录时为空）、什么动作（方法 + **路由模板**）、哪份资源、
为什么（原因码）、什么时候、哪次请求（`request_id`）。

不记：请求体、响应体、原始 URL。查询串里可能有值，而那张表是给运维查「有没有被拒」的，
不是给审计做全文检索的。轨迹里的证据属 F4，不属这里。

## 公开路径不记

`/api/auth/login` 与 `/api/auth/accept-invite` 是匿名可达的。在这两条上写记录，
等于给了一个**谁都能打满的写入口**——一次脚本就能把这张表撑爆，而撑爆之后真正有用
的那几条就淹在里面了。登录失败已经有它自己的记账（`users.failed_attempts` 与
`lockout_until`），不需要第二份。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from typing import Final, cast
from uuid import uuid4

from fastapi import Request

from agentworkbench.api.deps import Deps
from agentworkbench.core.errors import AppError, ErrorCode
from agentworkbench.identity import store
from agentworkbench.identity.models import IdentityEvent, ReasonCode

__all__ = ["REFUSAL_STATUSES", "action_of", "reason_of", "record", "resource_of"]

REFUSAL_STATUSES: Final[frozenset[int]] = frozenset({401, 403, 404})
"""被记录的那三种状态码。

400 不记：参数写错不是「被拒」，它没有可以回答「为什么」的归属问题，而且校验失败
的量级完全是另一回事（前端一个 bug 就能刷出几万条）。409 / 503 同理不记——
它们是「现在不行」，不是「你不许」。
"""

_RESOURCE_KINDS: Final[dict[str, str]] = {
    "doc_id": "document",
    "conversation_id": "conversation",
    "run_id": "run",
    "user_id": "user",
    "provider_id": "provider",
    "kb_id": "knowledge_base",
    "invite_id": "invite",
    "token": "invite",
}
"""路径参数名 → 资源种类。

**从路由模板推，不让路由自己传。** 让路由传的话，`resource_kind` 就成了第 16 个
「要记得写」的地方；而这一列是运维唯一能用来筛「这类事一共发生过几次」的维度，
它错了不会有人发现。

参数名是唯一稳定的线索：`/api/documents/{doc_id}` 与 `/api/documents/{doc_id}/chunks`
是同一个资源种类，而它们的方法和路径都不同。将来新增路由只要路径参数起对了名，
这一列自动是对的；起错了（比如 `{id}`）会掉进「认不出来」而不是「猜一个」。
"""

_FALLBACK_REASON: Final[dict[ErrorCode, ReasonCode]] = {
    ErrorCode.NOT_AUTHENTICATED: ReasonCode.UNAUTHENTICATED,
    ErrorCode.FORBIDDEN: ReasonCode.ROLE_INSUFFICIENT,
    ErrorCode.PASSWORD_CHANGE_REQUIRED: ReasonCode.PASSWORD_CHANGE_REQUIRED,
    # 404 只有一个理由：对你不可见。它同时覆盖「不存在」与「属于别人」——
    # 路由按团队条件查，本来就分不出这两者（见 `ReasonCode.RESOURCE_MISSING`）。
    ErrorCode.NOT_FOUND: ReasonCode.RESOURCE_MISSING,
}


def _template_of(request: Request) -> str:
    """路由模板，`/api/documents/{doc_id}` 这种形态。

    取不到时返回空串而不是猜。空串会让调用方按「不在白名单」处理——**宁可多拦一次，
    不许因为认不出来就放行**。取不到只发生在没匹配到路由的请求上，那时也走不到这里。
    """
    route = request.scope.get("route")
    return str(getattr(route, "path", "") or "")


def action_of(request: Request) -> str:
    """方法 + 模板。**不用 `request.url.path`**：那会把 `doc_id` 的真实值写进
    动作列，于是「这个动作发生过多少次」变成一个按 id 分组的问题。"""
    return f"{request.method} {_template_of(request)}"


def resource_of(request: Request) -> tuple[str | None, str | None]:
    """（资源种类，资源 id）。认不出就是两个 `None`——`I-V4` 要求字段「齐全」，
    不要求「猜对」；猜错比留空更难查。"""
    template = _template_of(request)
    if not template:
        return None, None
    for name, kind in _RESOURCE_KINDS.items():
        if "{" + name + "}" in template:
            value = request.path_params.get(name)
            return kind, None if value is None else str(value)
    return None, None


def reason_of(error: AppError) -> ReasonCode | None:
    """这次拒绝的原因。`None` 表示「不该记」（不是拒绝类错误码）。

    优先用异常自己带的（判定层知道得最准：没带票 / 票过期 / 账户停用 在这个层面
    是三种不同的事），没有才按错误码兜底。兜底不是冗余——路由层产生的 404
    没有判定对象可问，它只知道「查不到」。
    """
    if error.reason:
        try:
            return ReasonCode(error.reason)
        except ValueError:
            # 判定层给了个不在枚举里的字符串：宁可落兜底，也不让记录写不进去。
            # 这条分支出现就说明 `Decision.reason` 里混进了别的东西。
            pass
    return _FALLBACK_REASON.get(error.code)


def record(request: Request, error: AppError) -> None:
    """写一条拒绝记录。**抛异常就抛**，由调用方翻译成 500。

    不吞异常是判据要求的（`I-V4` 的负控第三条）：记录写不进去时仍然回 404，
    等于告诉调用方「这条不存在」——而系统手里没有任何证据支撑这句话。
    """
    reason = reason_of(error)
    if reason is None:
        return

    deps = cast(Deps, request.app.state.deps)
    kind, resource_id = resource_of(request)
    principal = getattr(request.state, "principal", None)
    event = IdentityEvent(
        # 与 run id 不同源：拒绝不是一次执行（`IdentityEvent` 的 docstring）
        event_id=f"evt-{uuid4().hex}",
        occurred_at=datetime.now(UTC),
        action=action_of(request),
        reason_code=reason,
        actor_user_id=None if principal is None else principal.owner.user_id,
        tenant_id=None if principal is None else principal.owner.tenant_id,
        resource_kind=kind,
        resource_id=resource_id,
        request_id=_request_id_of(request),
    )
    with closing(deps.open_conn()) as conn:
        _insert(conn, event)


def _insert(conn: sqlite3.Connection, event: IdentityEvent) -> None:
    """单独一函数，只为让测试能把「写失败」注入进来（monkeypatch 它）。"""
    with conn:  # 提交或回滚：写了一半的事件等于没有
        store.insert_event(conn, event)


def _request_id_of(request: Request) -> str | None:
    """空串归一成 `None`：中间件没挂时 `x-request-id` 是空的，
    而「空串」在 SQL 里既不等于 `NULL` 也不等于任何值——按它反查永远查不到。"""
    return str(getattr(request.state, "request_id", "") or "") or None
