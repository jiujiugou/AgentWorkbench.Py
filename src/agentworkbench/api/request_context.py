"""请求上下文中间件：给每个请求一个 request_id。

它要同时出现在三个地方，缺一个判据就不成立（S-V4）：
- 响应头 `X-Request-Id`：客户端拿到的
- 错误体 `request_id` 字段：报错时贴在工单里的
- 日志：运维按它对上同一次请求

**为什么是纯 ASGI 而不是 BaseHTTPMiddleware**：后者会给每个请求包一层
任务与缓冲，对流式响应（F6 后半的 SSE）有已知的干扰方式；这里只做
"生成 id → 塞进 scope.state → 给响应头加一行"，不需要那层包装。

id 是 uuid4 hex，不是自增：自增 id 会把请求量泄露给任何能触发错误的人，
而 uuid 没有这个副作用。日志侧用它检索即可，不需要它可排序。

服务于：S-V4（REQ-20 错误稳定）
"""

from __future__ import annotations

from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "x-request-id"


class RequestContextMiddleware:
    """每个请求生成一个 request_id，写进 scope.state 并加到响应头。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid4().hex
        # scope["state"] 是 Starlette 给请求级数据的官方通道：
        # exception_handler 里新建的 `Request(scope)` 读 `request.state` 拿到的就是它。
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # 兜底 500 由 Starlette 最外层的 ServerErrorMiddleware 生成，
                # 那条响应不流经这里的 send——它自己在 handler 里设头。
                # 所以这里只**补缺**，不重复：每个响应恰好一个头。
                if REQUEST_ID_HEADER not in headers:
                    headers.append(REQUEST_ID_HEADER, request_id)
            await send(message)

        await self.app(scope, receive, send_with_header)
