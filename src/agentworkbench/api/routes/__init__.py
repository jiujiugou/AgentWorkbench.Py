"""HTTP 路由：一个资源一个文件。

这一层只做协议转换，出现业务判断说明它漏上来了。

流式用 SSE（`text/event-stream`），不用 WebSocket：
本产品是单向推送，WebSocket 多一套握手与重连语义，不划算。

来源：C# `Webapi/Controllers/`
服务于：REQ-17 ~ REQ-20
"""

from __future__ import annotations
