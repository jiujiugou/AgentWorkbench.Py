# ruff: noqa: S106 —— 本文件的 secret_key 全是测试值，不连任何真实账号。
"""部署入口与静态托管的不变量：SPA 回落、API 优先、dist 缺失降级。

**改坏之后用户看到什么**（写不出失效形态的断言本来就不该存在）：

- 回落对 `/api` 也生效：前端拼错一个接口路径，拿到的是 200 加一页 HTML——
  「路径写错」被伪装成「请求成功」，排查的人对着 200 一脸茫然
- 静态挂载排在业务路由之前：整个 API 被静态层吞掉，所有请求 404，
  而走注入桩的单元测试全绿（它们根本不经过 mount 这条路）
- `dist` 缺失时启动抛异常：开发态被挡在门外——后端调试不需要前端产物
- 前端路由回落写成 404：用户刷新一下 `/chat` 就死在错误页

服务于：S-V11（`pyproject.toml`）；F6 文档 §1.11。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentworkbench.api.app import create_app
from agentworkbench.config import Settings


def _dist(tmp_path: Path) -> Path:
    """造一份最小前端产物：index.html + 一个带 hash 目录感的资产。"""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>INDEX-MARK</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return dist


@contextmanager
def _client(tmp_path: Path, dist: Path | None) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path / "data", secret_key="serve-test", web_dist=dist)
    app: FastAPI = create_app(settings)
    with TestClient(app) as client:
        yield client


def test_slash_serves_the_index_page(tmp_path: Path) -> None:
    """`GET /` 是界面的入口，返回的是构建产物里的 index。"""
    with _client(tmp_path, _dist(tmp_path)) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "INDEX-MARK" in response.text


def test_frontend_routes_fall_back_to_the_index_page(tmp_path: Path) -> None:
    """`/chat`、`/login` 磁盘上没有对应文件，统一回落 index。

    SPA 的路由只存在于前端：刷新任何一页都是新的一次 GET，
    回落缺失的话，用户从首页点进 /chat 再刷新一下就死在 404。
    """
    with _client(tmp_path, _dist(tmp_path)) as client:
        for route in ("/chat", "/login", "/documents"):
            response = client.get(route)
            assert response.status_code == 200, route
            assert "INDEX-MARK" in response.text, route


def test_unknown_api_paths_stay_404(tmp_path: Path) -> None:
    """`/api` 下的未知路径不回落。

    回一页 HTML 会把「路径写错」伪装成「请求成功」——调用方拿着 200 去解析
    JSON，炸出来的错看起来像数据问题。接口的 404 就是 404。
    """
    with _client(tmp_path, _dist(tmp_path)) as client:
        response = client.get("/api/definitely-not-a-route")
    assert response.status_code == 404
    assert "INDEX-MARK" not in response.text


def test_api_routes_win_over_the_static_mount(tmp_path: Path) -> None:
    """静态层挂在业务路由**之后**：/api/health 不被吞。

    mount 一样叫 `/`，先注册先匹配——把挂载挪到 include_router 之前，
    这条就红。而桩注入的单元测试不经过 mount，全绿照旧：这正是
    「只测部件、不测装配顺序」会漏掉的那类错。
    """
    with _client(tmp_path, _dist(tmp_path)) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_missing_dist_still_serves_the_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`dist` 不存在：API 照常，日志里有一句人话。

    开发态经常没有前端产物，托管缺失不该挡住后端调试；
    但「界面为什么是白的」必须有一条日志可查，不能只是静默不挂载。

    断言用**调用点打桩**而不是 caplog / 自挂 handler：那两条都依赖进程级
    logging 状态（有效等级、handler 链、`logging.disable`），全量顺序下
    表现不一致——测试不该赌执行顺序。桩打在 `app.py` 引用的那个 logger
    实例的 `warning` 方法上，**与 logging 机器完全解耦**：只要那一行代码
    执行了就能看见，其余状态无论被谁改成什么样都不影响。
    """
    from agentworkbench.api import app as app_module

    missing = tmp_path / "nowhere"
    messages: list[str] = []
    monkeypatch.setattr(
        app_module.logger, "warning", lambda msg, *args: messages.append(str(msg % args))
    )

    settings = Settings(data_dir=tmp_path / "data", secret_key="serve-test", web_dist=missing)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert any("web_dist" in message for message in messages)


def test_static_assets_are_served(tmp_path: Path) -> None:
    """带目录的资产按原路径给（js/css 由 index 引用，404 就是白屏）。"""
    with _client(tmp_path, _dist(tmp_path)) as client:
        response = client.get("/assets/app.js")
    assert response.status_code == 200
    assert "console.log(1)" in response.text
