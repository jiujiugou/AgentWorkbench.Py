"""测试全局夹具。

**`AWB_SECRET_KEY`** 是生产启动的硬性要求（S-V6：缺密钥 `build_deps` 拒绝启动）。
测试进程用 `setdefault` 给一个统一默认值——测试不关心加解密本身的用例
不必每个都传密钥；要验证"缺密钥必须失败"的用例（test_provider_config）
自己用 monkeypatch 删掉它。

**`sign_in` 夹具**（F1 格 5）：阶段 1 的 82 条测试都用 `Owner("local", tenant)`
造数据，换血之后 HTTP 路由要真的登录才肯干活。夹具因此**复现同一个身份**：
`id='local'`、`tenant=settings.tenant_id`、角色 admin、且是平台所有者
（`is_platform_admin=1`，见 `seed_account` 的 docstring）。如果换成一个随机 id
的新账号，那些测试就必须改 82 处断言——而那会让「换血」变成「重写测试」，
真正的回归就没人看得见了。

夹具**直接写 users 表**而不是走 `service.bootstrap`：bootstrap 只允许在空库上跑一次，
而这些测试的库不是空库，也不该为夹具改变自己的形状。表结构是稳定的，
`SEED_USER_ID` 是夹具自己的常量——两者都对得上就行。
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

os.environ.setdefault("AWB_SECRET_KEY", "unit-test-secret-key")
# `session_invalidate_seconds=0`：**整个测试进程都不缓存会话判定**。
# 默认值是 60，而那会让「停用后下一个请求就 401」「改密后另一张票立刻失效」
# 这类断言变成「取决于这条用例之前有没有暖过缓存」——同样的断言在不同执行顺序下
# 给出不同结果，是最难查的一类不稳定。要验窗口本身的用例（test_global_gate.py）
# 自己构造 `Settings(auth=...)` 覆盖这一项。
os.environ.setdefault("AWB_AUTH__SESSION_INVALIDATE_SECONDS", "0")


def pytest_configure(config: pytest.Config) -> None:
    """给每次运行一个**唯一**的临时根目录，放在 `reports/`（gitignore）下。

    不用 pytest 默认的 `%TEMP%`，有两个都不轻的理由：

    1. **删除守卫会杀掉进程。** pytest 在会话结束时把当次所有临时目录移进
       `garbage-<uuid>/` 再一次性删除——几百个文件一次删，装过删除守卫的机器会
       直接拦下并终止进程。表现是：**全部测试都通过**，进程仍以非 0 退出，
       报告里一条失败也没有（D4、D6 各踩一次，各花半小时才定位）。
    2. `%TEMP%` 在系统盘。一次全量跑产生约 600MB，攒几次就把 C 盘吃满，
       而 C 盘满了的表现是 `OperationalError: database or disk is full`——
       看起来像数据库坏了。

    显式给 `basetemp` 之后 pytest 不再做那套「收集 + 批量删除」，只会在启动时
    清空给定目录；每次运行一个 `run-<纳秒>`，等于谁也不清。代价是 `reports/`
    下会留一堆临时目录，它们本来就是可丢弃的（同 `reports/acceptance/` 的定位）。
    """
    if not config.option.basetemp:
        root = Path(config.rootpath) / "reports" / "pytest-tmp"
        # 目录必须**先建出来**：pytest 拿到显式 basetemp 时只会清空、不会创建，
        # 而它下面还要再建 per-test 子目录，父目录不存在就 FileNotFoundError。
        root.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(root / f"run-{time.time_ns()}")


if TYPE_CHECKING:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

SEED_USER_ID = "local"
"""夹具账号的 id。**必须与阶段 1 的 `LOCAL_USER_ID` 相同**——理由见模块 docstring。"""

SEED_EMAIL = "local@example.com"
SEED_PASSWORD = "seed-password-for-tests"  # noqa: S105 —— 测试夹具的口令，不连真实账号

_HASH: str | None = None


def _seed_hash() -> str:
    """口令哈希算一次就够。argon2 是有意设计成慢的，每条测试算一遍会让 CI 多几十秒。"""
    global _HASH
    if _HASH is None:
        from agentworkbench.identity import credentials

        _HASH = credentials.hash_password(SEED_PASSWORD)
    return _HASH


def seed_account(app: FastAPI) -> None:
    """确保库里有一个 `local` 账号，租户就是 `settings.tenant_id`（默认 "default"）。

    幂等：已存在直接返回。同一个 app 上多个 client 各自登录不会重复插入。

    `is_platform_admin = 1`：夹具复现的是「一台机器一个人」的部署形态，
    那个人既是团队管理员，也是这套安装的所有者（`REQ-AUTH-12`）。
    这不是为了让测试好过——**不置 1 的话，凡是走 HTTP 配供应商的既有测试都会 403**，
    而它们验的是 F6 的装配，不是权限。权限本身由 `test_platform_admin.py` 单独验：
    那里造一个真正的团队管理员（`is_platform_admin = 0`），断言他 403。
    """
    from agentworkbench.core.clock import utc_now

    deps = app.state.deps
    tenant_id = deps.settings.tenant_id
    conn = deps.open_conn()
    try:
        if conn.execute("select 1 from users where id = ?", (SEED_USER_ID,)).fetchone():
            return
        now = utc_now()
        conn.execute(
            "insert or ignore into tenants (id, name, created_at) values (?, ?, ?)",
            (tenant_id, "测试团队", now),
        )
        conn.execute(
            "insert into users (id, tenant_id, email, password_hash, role, status,"
            " credential_epoch, failed_attempts, is_platform_admin, created_at, updated_at)"
            " values (?, ?, ?, ?, 'admin', 'active', 1, 0, 1, ?, ?)",
            (SEED_USER_ID, tenant_id, SEED_EMAIL, _seed_hash(), now, now),
        )
        conn.commit()
    finally:
        conn.close()


def sign_in(client: TestClient, app: FastAPI) -> None:
    """给这个客户端一个登录态（cookie 写进它自己的 jar）。

    走 `service.login` 而不是直接插一张票：签发票据的那条路径（版本号、
    过期时刻、哈希）只有一处实现，夹具绕过去就等于在测试里养了第二份。
    """
    from agentworkbench.api.identity_context import COOKIE_NAME
    from agentworkbench.identity import service

    seed_account(app)
    deps = app.state.deps
    conn = deps.open_conn()
    try:
        result = service.login(
            conn,
            email=SEED_EMAIL,
            password=SEED_PASSWORD,
            now=datetime.now(UTC),
            config=service.AuthConfig.from_settings(deps.settings),
        )
    finally:
        conn.close()
    if not isinstance(result, service.LoginSucceeded):
        raise AssertionError(f"夹具登录失败：{result.reason}")
    client.cookies.set(COOKIE_NAME, result.ticket)


def insert_document(
    conn: sqlite3.Connection,
    doc_id: str,
    *,
    tenant_id: str,
    file_name: str = "sample.pdf",
) -> None:
    """建一行 `documents`。**检索的团队条件走这张表**。

    只写 chunks / chunks_fts / chunks_vec 而不写主表的夹具，换血之后一条都检索不到。
    这不是测试写错了，而是新语义：文档行不存在（或不属于你）时，它残留的索引
    也不该被检索到——AUTH-06「没有归属的行对所有人 404」在检索侧的推论。
    """
    from agentworkbench.core.clock import utc_now

    now = utc_now()
    conn.execute(
        "insert or ignore into documents(id, tenant_id, file_name, stored_path, sha256,"
        " size_bytes, status, page_count, created_at, updated_at)"
        " values (?, ?, ?, ?, ?, ?, 'parsed', 1, ?, ?)",
        (doc_id, tenant_id, file_name, f"seed/{doc_id}.pdf", f"sha-{doc_id}", 1024, now, now),
    )


@pytest.fixture(name="sign_in")
def _sign_in_fixture() -> Callable[[TestClient, FastAPI], None]:
    """`sign_in(client, app)`：把登录态塞进这个客户端。"""
    return sign_in


def load_script(name: str) -> ModuleType:
    """按路径加载 `scripts/*.py`。

    `scripts` 不是一个包（没有 `__init__.py`），也没有进 `pythonpath`——
    把它变成包会让「脚本」变成「库」，而这两者的差别是「能不能被 import 之后
    不执行」在这里恰好是我们要的：加载不等于执行，`main()` 才执行。

    放在 conftest 而不是某一个测试文件里：引导脚本与绑定脚本各自要验退出码，
    而 `scripts` 不是一个能被 `import` 的包——两处各抄一份的话，抄错的那一份
    会用一个不存在的路径加载脚本，报出来的错是「找不到文件」而不是「断言失败」。
    """
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
