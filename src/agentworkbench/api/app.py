"""组合根：建 app、挂路由、装配各包（显式构造，不用容器）。

把前端整个删掉，核心价值仍应完整——这个文件是那条判断的落地检查表。

当前路由：documents（六端点）+ knowledge（search / ask）+ conversations（三个只读）
+ settings（供应商与 embedding）+ runs（异步提交与状态）+ identity（登录/登出/改密/邀请）
+ members（列表/邀请/改角色/停用/恢复）。
**每条路由是否产出 run 都记在 `routes/declarations.py`**，那张表由
`tests/invariants/test_route_declarations.py` 守着，新增路由不表态会红。
SSE（对话流式）属于 F6，未接；F5 的读写走一次请求-响应。

来源：C# `Webapi/Program.cs`
服务于：REQ-17（可只经 HTTP 完成）、REQ-11（每条作答路都留痕）
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agentworkbench.api import errors, refusals
from agentworkbench.api.deps import Deps, build_deps
from agentworkbench.api.identity_context import require_session
from agentworkbench.api.request_context import RequestContextMiddleware
from agentworkbench.api.routes import (
    chat as chat_api,
)
from agentworkbench.api.routes import (
    conversations,
    documents,
    identity,
    knowledge,
    members,
    runs,
)

# `settings` 与 create_app 的参数同名，导入时起别名——不遮蔽，遮蔽的报错是
# `'NoneType' object has no attribute 'router'`，与真实成因（名字被参数盖住）相去甚远。
from agentworkbench.api.routes import settings as settings_api
from agentworkbench.api.spa_static import mount_spa
from agentworkbench.config import Settings
from agentworkbench.core.errors import ERROR_SPECS, AppError, ErrorCode

logger = logging.getLogger(__name__)


def _request_id_of(request: Request) -> str:
    """从 scope.state 取 request_id。中间件未挂时给空串而不是炸——
    错误响应永远要比中间件更皮实。"""
    return str(getattr(request.state, "request_id", "") or "")


_TYPE_HINTS: dict[str, str] = {
    "string_type": "应为字符串",
    "string_too_long": "超长",
    "string_too_short": "为空",
    "missing": "缺失",
    "int_parsing": "应为整数",
    "int_type": "应为整数",
    "bool_type": "应为布尔值",
    "json_invalid": "不是合法的 JSON",
    "model_attributes_type": "应为对象",
    "list_type": "应为数组",
}
"""pydantic 错误类型 → 给人看的一句话。**只翻类型，不翻值。**

漏掉某个类型不是灾难（落到默认的「不合法」），而把 `input` 抄进来是——见
`_validation_message` 的 docstring。
"""


def _validation_message(exc: RequestValidationError) -> str:
    """把 pydantic 的错误清单压成一句人能看懂、且**不回显提交值**的话。

    这是 D6 抓到的一个真实泄露：FastAPI 默认的 422 体是
    `{"detail": [{"loc": [...], "input": <客户端提交的原值>}]}`。
    提交一个 `apiKey: {"nested": "sk-..."}`（类型错、值对）时，那个哨兵会被
    原样放进响应体——浏览器 devtools、反代访问日志、前端错误上报里都会留一份。
    「密钥不回传」这条判据按字面就不成立了。

    所以这里只取 `loc`（字段路径）与 `type`（错误种类），`input` / `ctx` 一概不碰；
    客户端本来就知道自己提交了什么，它需要的是**哪个字段错了**，不是再看一遍原值。
    """
    parts: list[str] = []
    for error in exc.errors():
        loc = [str(item) for item in error.get("loc", ()) if item not in ("body", "query", "path")]
        field = ".".join(loc) or "请求体"
        hint = _TYPE_HINTS.get(str(error.get("type", "")), "不合法")
        parts.append(f"{field}（{hint}）")
    # 同一条请求里同一个字段只报一次：整份 body 缺字段时会列出十几个，
    # 拼成一整句反而看不出该改哪个。
    unique = list(dict.fromkeys(parts))
    return "请求字段不合法：" + "、".join(unique[:5]) + "。"


def create_app(
    settings: Settings | None = None,
    deps: Deps | None = None,
) -> FastAPI:
    """建 app。测试传自定义 `settings` 或整份 `deps`；生产用环境变量（`AWB_` 前缀）。"""
    cfg = settings or Settings()
    app = FastAPI(
        title="AgentWorkbench",
        version="0.0.0",
        # 全局闸门（`I-V1`）：每一条路由——包括将来新增的——都先过这里。
        # 挂成 app 级依赖而不是在每条路由上写一遍，是因为「写了 20 条」和
        # 「挡得住第 21 条」是两件事，而后者没有办法靠自觉保证。
        dependencies=[Depends(require_session)],
    )

    # vite dev server（5173）是另一个源，跨域必须显式放行；
    # 生产由本进程托管 `web/dist`，同源，这段不生效也无害。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.origin_set,
        allow_methods=["*"],
        allow_headers=["*"],
        # F1 起会话靠 cookie 承载，跨域请求必须允许带凭据，否则 5173 上
        # 的开发态「登录成功但下一个请求 401」——而那个现象看起来像后端丢会话。
        # `allow_origins` 是显式列表（不是 "*"），两者不能共存。
        allow_credentials=True,
    )
    # add_middleware 是"洋葱"：后加的在外层。RequestContext 放最外，
    # 让每一个请求——包括将来被 CORS 预检拦下的——都带着 request_id。
    app.add_middleware(RequestContextMiddleware)

    app.state.deps = deps if deps is not None else build_deps(cfg)

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """请求体/查询参数校验失败 → 与业务错误同一套五字段信封。

        **不用框架默认的 422**，两个理由（D6 实测）：

        1. **默认体会回显提交值**（`input` 字段），凭据类字段一旦类型写错就被
           原样回给客户端——「密钥不回传」在错误路径上直接破功。
        2. **默认体没有 `message`**：前端 `parseError` 只认 `message`，
           于是用户看到的是 `{"detail":[...]}` 这段原文（里面正是他的 key）。

        状态码用 400 而不是 422：`ERROR_SPECS` 里 `validation_failed` 就是 400，
        一个码只对应一个状态码这条约束（`core/errors.py` 模块 docstring）不能为了
        框架默认值开口子。代价是「畸形 JSON 从 422 变 400」——这在语义上更准，
        且没有任何调用方依赖过 422（前端只读 `message`）。
        """
        message = _validation_message(exc)
        request_id = _request_id_of(request)
        logger.info(
            "请求校验失败 path=%s request_id=%s detail=%s",
            request.url.path,
            request_id,
            message,
        )
        return JSONResponse(
            status_code=400,
            content=errors.body_of(AppError(ErrorCode.VALIDATION_FAILED, message), request_id),
        )

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """框架自己抛的 `HTTPException` → 同样五字段信封。

        为什么需要这一条：`AppError` 那个 handler 只覆盖**我们主动抛**的异常。
        未匹配的路径（`GET /api/nope`）、路径编码后不再匹配路由（`/api/documents/a%2Fb`）、
        方法不对（`PUT /api/health`）走的是 Starlette 的默认实现，返回
        `{"detail": "Not Found"}`——**没有 `message`、没有 `request_id`**。
        前端 `parseError` 只认 `message`，于是用户看到的是这段 JSON 原文；
        而且这种失败无法按 request_id 去日志里定位。

        **405 也归到 404**（同一个码、同一个状态）：这个产品的既有取向是
        「不确认某个路径存不存在」——`/api/documents/{别人的 id}` 与不存在都是 404。
        方法不对若单独回 405，等于告诉未登录的人「这个路径是真的」，
        而那正是一条不需要权限就能做的探测。
        """
        request_id = _request_id_of(request)
        method_mismatch = exc.status_code in (404, 405)
        if not method_mismatch and exc.status_code >= 500:
            code = ErrorCode.INTERNAL_ERROR
        else:
            code = ErrorCode.NOT_FOUND
        spec = ERROR_SPECS[code]
        return JSONResponse(
            status_code=spec.http_status,
            content={
                "code": str(code),
                "http_status": spec.http_status,
                "message": "这个路径不支持该方法。" if exc.status_code == 405 else "路径不存在。",
                "retryable": spec.retryable,
                "request_id": request_id,
            },
        )

    @app.exception_handler(errors.UploadRejected)
    async def on_upload_rejected(request: Request, exc: errors.UploadRejected) -> JSONResponse:
        return JSONResponse(status_code=400, content=errors.body_of(exc, _request_id_of(request)))

    @app.exception_handler(AppError)
    async def on_app_error(request: Request, exc: AppError) -> JSONResponse:
        """稳定错误码 → HTTP。状态码与 retryable 一律查 ERROR_SPECS，不手写。

        **拒绝在这里留痕**（`I-V4`）：一次 401/403/404 必然经过这个函数，而它发生在
        响应发出之前。把记录散到各路由的 `raise` 之前也能用，但那是 15 个「要记得」；
        这里是 1 个「必然」——第 16 个出口明天才出现，而它今天就已被覆盖。
        """
        request_id = _request_id_of(request)
        status = errors.status_of(exc)
        # S-H2 的后半句「日志里有」：一次失败必须能按客户端贴来的 request_id 翻到，
        # 否则「响应里带 request_id」只是把一个无处可查的编号印在响应上。
        # 此前只有 500 兜底路径写日志，503（模型不可用 / 未配置 / 索引待重建）
        # 一条都不留——D6 真跑不可达端点时按 request_id 查不到任何东西。
        #
        # 5xx 归运维（warning），4xx 归调用方（info）；**不记 message 原文的请求体**：
        # 日志的用途是「这次请求怎么了」，不是把用户提交的内容再存一份。
        log = logger.warning if status >= 500 else logger.info
        log(
            "请求失败 code=%s status=%s path=%s request_id=%s reason=%s",
            exc.code.value,
            status,
            request.url.path,
            request_id,
            exc.reason or "-",
        )
        if status in refusals.REFUSAL_STATUSES:
            try:
                refusals.record(request, exc)
            except Exception:
                # 记录写不进去时**不许**照常回 404（判据原话：翻译成 500）。
                # 照常回 404 等于说「查过了，没有」——而系统手上没有任何证据。
                logger.exception(
                    "拒绝记录写入失败 path=%s request_id=%s", request.url.path, request_id
                )
                return JSONResponse(
                    status_code=500,
                    content=errors.body_of(
                        AppError(ErrorCode.INTERNAL_ERROR, "服务器内部错误。"), request_id
                    ),
                    headers={"x-request-id": request_id},
                )
        return JSONResponse(
            status_code=status,
            content=errors.body_of(exc, request_id),
        )

    @app.exception_handler(Exception)
    async def on_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """兜底：没接住的异常返回 500 + INTERNAL_ERROR，不把堆栈泄给客户端。

        `logger.exception` 是判据 S-V5 的前半句（「日志里有」）：响应侧只有
        固定文案，排障靠日志里这条含堆栈的记录。request_id 打进日志，
        客户端贴来的那个 id 才能在服务端对上号。
        """
        request_id = _request_id_of(request)
        logger.exception("未处理异常 path=%s request_id=%s", request.url.path, request_id)
        response = JSONResponse(
            status_code=500,
            content={
                "code": "internal_error",
                "http_status": 500,
                "message": "服务器内部错误。",
                "retryable": True,
                "request_id": request_id,
            },
        )
        # 这条响应不流经 RequestContext 的 send（兜底由 Starlette 最外层的
        # ServerErrorMiddleware 生成），头必须在这里自己设，中间件补不到。
        response.headers["x-request-id"] = request_id
        return response

    app.include_router(documents.router)
    app.include_router(knowledge.router)
    app.include_router(chat_api.router)
    app.include_router(conversations.router)
    app.include_router(settings_api.router)
    app.include_router(runs.router)
    # 身份与成员（F1）：登录是唯一先于身份的入口，members 自己判「我是不是管理员」。
    # 全局依赖（除白名单外一律 401）是格 6 的事——先挂闸会让现有 82 条走 HTTP 的
    # 测试全红，因为它们的夹具还没有登录态。
    app.include_router(identity.router)
    app.include_router(members.router)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # 静态托管必须**最后**挂（Starlette 按注册顺序匹配）：先挂会把整个 API 吞掉，
    # 而走注入桩的单元测试全绿——装配顺序的错只有端到端那条测试看得见（S-V11）。
    # CORS 那段注释里说的「生产同源托管」指的就是这次挂载——它曾经只存在于
    # 注释里，代码里没有；那是一段基于不存在实现的推理。
    dist = cfg.web_dist
    if dist is not None and Path(dist).is_dir():
        mount_spa(app, Path(dist))
    elif dist is not None:
        logger.warning(
            "web_dist=%s 不存在——前端界面不可用（API 照常）。构建：cd web && npm run build",
            dist,
        )

    return app


app = create_app()
