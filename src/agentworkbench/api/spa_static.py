"""SPA 静态托管：磁盘上没有的路径回落 `index.html`，但 `/api` 例外。

为什么要有这个子类：前端是 SPA，`/chat`、`/login` 这些路由只存在于前端——
磁盘上没有 `chat.html`，静态层按文件找不到就是 404。回落到 index 让前端
路由接手，用户刷新任何一页都活着。

为什么 `/api` 例外：接口的 404 是"路径写错"的证据。回落一页 HTML 过去，
调用方拿到 200 去解析 JSON，炸出来的错看起来像数据问题——
伪装成功的错误比失败难查一个量级。

**挂载顺序由 `create_app` 保证**：静态层挂在业务路由之后（Starlette 按注册
顺序匹配），先注册先匹配，`/api/health` 永远到不了这里。顺序本身有测试
（`test_api_routes_win_over_the_static_mount`），因为"部件各自对、装配顺序错"
在单元测试里全绿。

静态资产不要求登录：登录页本身需要 js/css 才能加载，"资产先于身份"是
刻意的（F6 §1.11）。边界在 API 层（全局闸门），不在资产层。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

_API_PREFIX = "/api"


class SpaStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # 404 在 StaticFiles 里是**抛异常**不是返回值——按返回值判断的版本
            # 永远不触发回落，而它看起来是对的（S-V11 的测试抓的就是这个）。
            if exc.status_code == 404 and not str(scope.get("path", "")).startswith(_API_PREFIX):
                return await super().get_response("index.html", scope)
            raise


def mount_spa(app: FastAPI, dist_dir: Path) -> None:
    """把前端产物挂到根路径。独立函数只为让 `create_app` 的意图一行读完。"""
    app.mount("/", SpaStaticFiles(directory=dist_dir, html=True), name="web")
