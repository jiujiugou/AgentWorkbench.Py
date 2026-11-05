# ruff: noqa: S105 —— 本文件的密钥全是哨兵值。
"""D6 / 供应商并发与密钥：并发改配置不许把库写坏、也不许串钥匙。

`S-V7` 证明的是「终态恰好一行激活」（含直接打 SQL 的负控），`S-V6` 证明的是
「明文不落库」。这一组补的是**并发**这个维度——单线程跑一万次全绿也说明不了它：

1. 多个管理员同时点「启用」两个不同供应商：终态必须**恰好一行**激活，
   且任何一次请求都不许 500（`database is locked` 在这条路径上是会发生的，
   只是默认 busy_timeout 把它挡住了；默认值改了就会现形）。
2. 多个线程同时写不同供应商的凭据：每行密文必须解回**自己那把**钥匙，
   不允许出现「A 的钥匙配了 B 的密文」——那种错不会报错，只会在调用时 401。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from harness import ALICE, PASSWORD, TwoTeams, build_two_teams

from agentworkbench.db import crypto

SECRET = "acceptance-test-secret"


@pytest.fixture
def teams(tmp_path: Path) -> Iterator[TwoTeams]:
    yield build_two_teams(tmp_path)


def _provider_id(teams: TwoTeams, name: str, api_key: str = "sk-placeholder") -> int:
    created = teams.client_a.post(
        "/api/settings/providers",
        json={"name": name, "baseUrl": f"http://127.0.0.1:9/{name}", "apiKey": api_key},
    )
    assert created.status_code == 201, created.text
    return int(created.json()["id"])


def _logged_in_client(teams: TwoTeams) -> TestClient:
    """每个线程一个客户端 + 一次真实登录。

    **不复用同一个 `TestClient`**：它内部是一条 httpx 连接加一份 cookie jar，
    并发复用它测到的是客户端自己，而不是服务端。这也更接近真实形态——多个
    浏览器标签、多个管理员。
    """
    client = TestClient(teams.app, raise_server_exceptions=False)
    login = client.post("/api/auth/login", json={"email": ALICE, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return client


def test_concurrent_activation_never_leaves_zero_or_two_active(teams: TwoTeams) -> None:
    """8 个线程 × 4 次交叉激活，终态恰好一行激活，全程无 5xx。"""
    first = _provider_id(teams, "alpha")
    second = _provider_id(teams, "beta")
    targets = [first, second] * 16

    def activate(provider_id: int, worker: int) -> int:
        client = _logged_in_client(teams)
        return client.post(f"/api/settings/providers/{provider_id}/activate").status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(activate, targets, range(len(targets))))

    assert set(statuses) == {200}, f"并发激活出现非 200：{sorted(set(statuses))}"

    with closing(teams.open_conn()) as conn:
        active = conn.execute("select id from providers where is_active = 1").fetchall()
    assert len(active) == 1, f"激活行数是 {len(active)}（必须恰好 1）"


def test_concurrent_credential_writes_never_swap_keys(teams: TwoTeams) -> None:
    """6 个线程同时写 6 把不同的钥匙，每行密文必须解回自己那把。"""
    keys = {f"p{n}": f"sk-D6-CONCURRENT-{n:02d}" for n in range(6)}

    def create(item: tuple[str, str]) -> int:
        name, key = item
        client = _logged_in_client(teams)
        created = client.post(
            "/api/settings/providers",
            json={"name": name, "baseUrl": f"http://127.0.0.1:9/{name}", "apiKey": key},
        )
        assert created.status_code == 201, created.text
        return int(created.json()["id"])

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(create, sorted(keys.items())))

    with closing(teams.open_conn()) as conn:
        rows: list[sqlite3.Row] = conn.execute(
            "select name, api_key_encrypted from providers order by name"
        ).fetchall()

    assert len(rows) == len(keys)
    for row in rows:
        name = str(row["name"])
        ciphertext = str(row["api_key_encrypted"])
        assert crypto.decrypt(SECRET, ciphertext) == keys[name], f"{name} 的密文解出了别人的钥匙"

    # 落盘侧：任何一把明文都不在库文件里
    payload = Path(teams.settings.db_path).read_bytes()
    for key in keys.values():
        assert key.encode() not in payload, f"库文件里出现了明文：{key}"
