"""连接层：并发开连接时，拿到的必须**能写**。

D6 抓到的真问题（Windows + WAL）：多条连接同时打开时，`-shm` 的映射会偶发失败，
而 SQLite 的降级方式是**把这条连接当成只读**——开的时候不报错，第一条 `update`
才炸 `SQLITE_READONLY: attempt to write a readonly database`。用户看到随机 500，
且只在并发时出现：单线程跑一万次也复现不了，所以它躲过了此前所有测试。

实测数据（SQLite 3.53.1 / Windows 11 / 8 线程各写一次）：全新库首个连接即并发
**133/160 失败**；热库 **7/160**；加一条常驻连接 **0/160**；`engine.connect` 的
「探测 + 重开」**0/160**。修复见 `db/engine.py::connect` 的 docstring。

改坏后会怎样：把 `_is_writable` 的探测去掉（直接返回 True），本文件第一条在
这类机器上会以 10%~80% 的概率红——概率取决于机器，所以判定靠**循环**而不是一次。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agentworkbench.db import engine


def _fresh_db(tmp_path: Path) -> Path:
    path = tmp_path / "probe.db"
    conn = engine.connect(path)
    try:
        engine.create_schema(conn)
        conn.execute("create table if not exists probe_log(id integer primary key, n integer)")
        conn.execute("insert into probe_log(n) values (0)")
        conn.commit()
    finally:
        conn.close()
    return path


def test_every_concurrent_connection_can_write(tmp_path: Path) -> None:
    """8 个线程 × 3 轮，每次都开新连接并写一行——一次 `SQLITE_READONLY` 都不许有。"""
    path = _fresh_db(tmp_path)
    errors: list[str] = []
    started = threading.Barrier(8)

    def work(_round: int) -> None:
        started.wait()
        try:
            conn = engine.connect(path)
            try:
                conn.execute("insert into probe_log(n) values (?)", (_round,))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:  # pragma: no cover - 出问题才走到
            errors.append(f"{type(exc).__name__}: {exc}")

    for _ in range(3):
        started.reset()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(work, range(8)))

    assert not errors, f"并发连接里出现了只读连接：{errors[:3]}"


def test_the_probe_leaves_no_open_transaction(tmp_path: Path) -> None:
    """探测取过一次写锁，必须自己收回——否则第一条业务语句会撞
    「cannot start a transaction within a transaction」。"""
    conn = engine.connect(tmp_path / "clean.db")
    try:
        conn.execute("create table if not exists t(id integer primary key)")
        conn.execute("insert into t(id) values (1)")
        conn.commit()
        assert conn.execute("select count(*) from t").fetchone()[0] == 1
        assert not conn.in_transaction, "连接上还挂着一个没结束的事务"
    finally:
        conn.close()


def test_a_busy_database_still_hands_out_a_working_connection(tmp_path: Path) -> None:
    """库被别的写者占着时，`connect` 不许干等 5 秒，也不许被判成只读。

    做法：另开一条连接持有写锁，再 connect。探测会拿到 `SQLITE_BUSY`——
    那证明连接**可写**，只是现在轮不到它，所以必须当成功返回。
    """
    path = _fresh_db(tmp_path)
    holder = engine.connect(path)
    try:
        holder.execute("begin immediate")
        holder.execute("update probe_log set n = n + 1 where id = 1")
        started = time.monotonic()
        conn = engine.connect(path)
        elapsed = time.monotonic() - started
        conn.close()
        assert elapsed < 1.0, f"探测把「库忙」当成了「连接坏了」，白等 {elapsed:.2f}s"
    finally:
        holder.execute("rollback")
        holder.close()
