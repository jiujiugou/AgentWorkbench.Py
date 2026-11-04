"""不变量：执行记录的两张表把 REQ-11 / 12 / 13 的硬约束下沉到库层。

失效形态（用户会看到什么）：

- **`status` 有默认值** → 漏传状态的运行落成「成功」。事后查库看到的是一次
  从没发生过的成功，而它会被当成证据（C# `M014:14` 的 `DEFAULT 'succeeded'`）。
- **`user_id` / `tenant_id` 可空** → 记录无法归属。「谁问的、哪个团队的」答不上，
  越权排查与追责同时失效（C# 这两列根本不存在，只有 `session_id`）。
- **步骤序号不唯一** → 回放顺序错乱，同一个位置出现两条记录，而两条都「真实」。
- **失败写进 `output` 文本而不是独立字段** → 直接查库分不出成败，展示层只能猜
  （A-H2 的人工判据当场不成立）。
- **token 缺值写成 0** → 0 会被当成真实用量参与成本统计（A-V10）。

改坏这个不变量会怎样：给 `status` 加回 `DEFAULT 'succeeded'`、把 `user_id` 改成
可空、去掉 `UNIQUE(run_id, seq)`——三条都会让对应用例当场红。这不是设想，
是本文件写完后逐条改坏验过的。

服务于：A-V8（归属）、A-V9（状态无默认值）、A-H2（失败独立字段）、A-V10（缺值 NULL）
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import cast

import pytest

from agentworkbench.core.runs import RunStatus
from agentworkbench.db import engine

INSERT_RUN = """
insert into agent_runs (id, user_id, tenant_id, question, status, started_at)
values (?, ?, ?, ?, ?, ?)
"""

INSERT_RUN_WITHOUT_STATUS = """
insert into agent_runs (id, user_id, tenant_id, question, started_at)
values (?, ?, ?, ?, ?)
"""

INSERT_STEP = """
insert into agent_steps (run_id, seq, kind, created_at)
values (?, ?, ?, ?)
"""


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    # 外键是连接级开关：engine.connect() 会开，裸 connect 必须自己开，
    # 否则 FK 约束形同不存在，孤儿步骤行能插进来
    connection.execute("pragma foreign_keys = on")
    engine.load_vec(connection)
    engine.create_schema(connection)
    yield connection
    connection.close()


def _new_run(conn: sqlite3.Connection, run_id: str = "run-1", status: str = "running") -> None:
    conn.execute(
        INSERT_RUN,
        (
            run_id,
            "local",
            "default",
            "Modbus 异常码 0x02 是什么原因",
            status,
            "2026-11-02T00:00:00",
        ),
    )


def _column(conn: sqlite3.Connection, table: str, name: str) -> sqlite3.Row:
    rows = cast(
        "list[sqlite3.Row]",
        conn.execute(f"pragma table_info({table})").fetchall(),  # 表名来自本文件常量
    )
    for row in rows:
        if row["name"] == name:
            return row
    raise AssertionError(f"{table} 没有列 {name}")


def test_status_has_no_default_value(conn: sqlite3.Connection) -> None:
    """`status` 不许有默认值——漏传就该报错，不许落成任何终态。"""
    assert _column(conn, "agent_runs", "status")["dflt_value"] is None


def test_owner_columns_are_not_null(conn: sqlite3.Connection) -> None:
    """归属两列必须非空，而且必须现在就建——不许事后补（F4 §1.7 第 3 条）。"""
    for name in ("user_id", "tenant_id"):
        assert _column(conn, "agent_runs", name)["notnull"] == 1, f"{name} 允许为空"


def test_insert_without_status_is_rejected(conn: sqlite3.Connection) -> None:
    """不传 status → 写库报错。这是 A-V9 的机制面：默认值一旦存在，这条必红。"""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            INSERT_RUN_WITHOUT_STATUS,
            ("run-1", "local", "default", "问", "2026-11-02T00:00:00"),
        )


@pytest.mark.parametrize("status", list(RunStatus))
def test_every_run_status_is_accepted(conn: sqlite3.Connection, status: RunStatus) -> None:
    """`RunStatus` 的每个值都必须能写进库——库层的 CHECK 不许比代码的枚举窄。"""
    _new_run(conn, run_id=f"run-{status.value}", status=status.value)
    assert conn.execute("select count(*) from agent_runs").fetchone()[0] == 1


def test_unknown_status_is_rejected(conn: sqlite3.Connection) -> None:
    """库层的 CHECK 是第二道闸：代码里造不出的状态，也不许直接写进来。"""
    with pytest.raises(sqlite3.IntegrityError):
        _new_run(conn, status="finished")


def test_token_and_duration_columns_accept_null(conn: sqlite3.Connection) -> None:
    """拿不到用量就是 NULL，不是 0——0 会被当成真实值统计（A-V10）。"""
    _new_run(conn)
    row = conn.execute(
        "select duration_ms, prompt_tokens, completion_tokens from agent_runs"
    ).fetchone()
    assert row["duration_ms"] is None
    assert row["prompt_tokens"] is None
    assert row["completion_tokens"] is None


def test_running_row_may_have_no_finished_at(conn: sqlite3.Connection) -> None:
    """开始时只写 `running`，结束时刻留空——「先写 running 再干活」的物理前提。"""
    _new_run(conn)
    row = conn.execute("select status, finished_at from agent_runs").fetchone()
    assert row["status"] == "running"
    assert row["finished_at"] is None


def test_step_seq_is_unique_per_run(conn: sqlite3.Connection) -> None:
    """同一 run 内序号唯一；不同 run 各自从 1 起——回放顺序不能靠时间戳。"""
    _new_run(conn, "run-1")
    _new_run(conn, "run-2")
    conn.execute(INSERT_STEP, ("run-1", 1, "retrieval", "2026-11-02T00:00:01"))
    conn.execute(INSERT_STEP, ("run-2", 1, "retrieval", "2026-11-02T00:00:02"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(INSERT_STEP, ("run-1", 1, "assistant", "2026-11-02T00:00:03"))


@pytest.mark.parametrize("kind", ["retrieval", "assistant", "tool"])
def test_step_kind_accepts_the_three_real_kinds(conn: sqlite3.Connection, kind: str) -> None:
    """三类步骤对应 §1.9 的「哪些成步」表：检索、模型、工具。

    **失败不是第四类**——失败是某一次检索/生成带了 `error`，它仍占自己那个
    序号（F4 §三-5）。把失败做成种类会让「一次失败的检索」和「一次检索」
    变成两种步骤，统计步骤数时就对不上了（A-V7）。
    """
    _new_run(conn)
    conn.execute(INSERT_STEP, ("run-1", 1, kind, "2026-11-02T00:00:01"))


def test_unknown_step_kind_is_rejected(conn: sqlite3.Connection) -> None:
    """判定、校验这些「不成步」的动作没有种类可写——想在库里造一条也没有位置。"""
    _new_run(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(INSERT_STEP, ("run-1", 1, "judgment", "2026-11-02T00:00:01"))


def test_step_requires_an_existing_run(conn: sqlite3.Connection) -> None:
    """步骤必须挂在真实存在的 run 上，孤立的步骤行不许存在。"""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(INSERT_STEP, ("run-missing", 1, "retrieval", "2026-11-02T00:00:01"))


def test_error_column_is_separate_from_output(conn: sqlite3.Connection) -> None:
    """失败有自己的列——A-H2 的人工判据靠它一眼认出失败步。"""
    _new_run(conn)
    conn.execute(
        "insert into agent_steps (run_id, seq, kind, output, error, created_at)"
        " values (?, ?, ?, ?, ?, ?)",
        ("run-1", 1, "assistant", None, "模型超时", "2026-11-02T00:00:01"),
    )
    row = conn.execute("select output, error from agent_steps").fetchone()
    assert row["error"] == "模型超时"
    assert row["output"] is None
