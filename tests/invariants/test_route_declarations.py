"""不变量：每条路由都必须表态「产不产 run」，且这个表态不会骗人（A-V1 / A-V1b）。

失效形态（用户会看到什么）：

**新增一条会作答的路，没人在声明表上表态。** 审计链断在没人走过的那条路上，
**而且当时无人察觉**——库里其它记录都好好的，只是少了那一类。
C# 就是这么掉进去的：`POST /api/knowledge/ask` 走独立链路不留 run，
而用户实际在用的就是它。六格做完五格，剩下那一格正好是客户在用的那条。

**遍历失效但测试全绿。** 这一条比上一条更隐蔽。FastAPI 0.162 起
`include_router` 挂进 `app.routes` 的是 `_IncludedRouter` 包装，没有 `path`；
照老写法遍历会得到 0 条业务路由，而"0 条未声明"意味着断言恒真。
所以本文件有两个自检：扫描结果必须非空，且必须看得见已知路由。

**声明表漂移。** 路由改名或删掉，表里还留着旧条目。它不会报错，
只会让人以为那条路还在。反向也要查。

改坏这个不变量会怎样：注一条 `POST /api/test/answer` 不声明 → 第一条当场红
（这正是 A-V1b 的判据）。把 `api_routes` 改回只遍历 `app.routes` → 自检红
（扫到 0 条）。

服务于：A-V1（无后门）、A-V1b（V1 自身不是恒真）
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from agentworkbench.api.app import create_app
from agentworkbench.api.deps import build_deps
from agentworkbench.api.routes.declarations import (
    NOT_PRODUCING,
    PRODUCING,
    api_routes,
    declared,
    undeclared,
)
from agentworkbench.config import Settings

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    settings = Settings(data_dir=tmp_path / "data")
    return create_app(settings=settings, deps=build_deps(settings))


def test_every_route_is_declared(app: FastAPI) -> None:
    """没有任何一条业务路由是"没表态"的。"""
    assert undeclared(app) == set(), (
        "有路由没在 declarations.py 里表态；新增作答路径时必须同时声明它产 run"
    )


def test_scanner_actually_sees_the_routes(app: FastAPI) -> None:
    """自检：遍历方式失效时，上面那条会变成恒真。

    FastAPI 0.162 的 `_IncludedRouter` 是真实踩到的坑——按老写法遍历 `app.routes`
    会扫到 0 条业务路由。这里把"扫得到"本身钉成断言。
    """
    routes = api_routes(app)
    assert len(routes) >= 10, f"只扫到 {len(routes)} 条路由，遍历方式八成失效了"
    assert ("GET", "/api/health") in routes
    assert ("POST", "/api/knowledge/ask") in routes
    # HEAD 是 GET 的自动伴生，声明表里不该为它占一行
    assert ("HEAD", "/api/health") not in routes


def test_an_undeclared_route_turns_it_red(app: FastAPI) -> None:
    """A-V1b：注入一条假路由，断言必须报出它。

    没有这一条，V1 从第一天起就是装饰——表里写什么都会绿。
    """
    assert undeclared(app) == set()

    @app.post("/api/test/answer")
    def _fake() -> dict[str, bool]:
        return {"ok": True}

    assert undeclared(app) == {("POST", "/api/test/answer")}


def test_declarations_do_not_drift_from_the_code(app: FastAPI) -> None:
    """反向：表里有、代码里没有的路由。声明表比代码活得久，这是必然发生的。"""
    present = set(api_routes(app))
    ghosts = set(declared()) - present
    assert ghosts == set(), f"这些声明已经没有对应的路由了：{sorted(ghosts)}"


def test_a_route_cannot_be_declared_both_ways() -> None:
    """同一条路既产 run 又不产 run——两张表各自看着都对，合起来自相矛盾。"""
    assert set(PRODUCING) & set(NOT_PRODUCING) == set()


@pytest.mark.parametrize(("table", "label"), [(PRODUCING, "产 run"), (NOT_PRODUCING, "不产 run")])
def test_every_declaration_states_a_reason(table: dict[tuple[str, str], str], label: str) -> None:
    """表态必须带理由。"不需要"三个字不算理由——它省掉的正是判断本身。"""
    for route, reason in table.items():
        assert len(reason) >= 8, f"{route} 声明为{label}，但理由只有：{reason!r}"


def test_every_producing_route_is_exercised_by_a_test() -> None:
    """声明"产 run"的路由必须在测试里被真的打过。

    否则声明表可以写得很漂亮而没有任何东西支撑它——"声明了但没验"
    与"没声明"在事后看是一样的。
    """
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "tests").rglob("test_*.py")
    )
    for _method, path in PRODUCING:
        assert path in sources, f"{path} 声明产 run，但没有测试打过这条路由"
