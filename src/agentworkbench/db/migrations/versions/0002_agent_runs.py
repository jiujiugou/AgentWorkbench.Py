"""agent_runs / agent_steps：执行记录的两张表（F4）。

Revision ID: 0002_agent_runs
Revises: 0001_initial
Create Date: 2026-11-02

同样**不写 DDL**，直接执行 `db.engine.RUNS_SCHEMA_SQL`——理由与 0001 一致：
迁移脚本与 `create_schema()` 是两条入口，各写一份建表语句必然在某次改动后分叉。

两条 DDL 都是 `IF NOT EXISTS`，所以这个迁移是幂等的：新库跑完 0001 已经带上
这两张表（`SCHEMA_SQL` 末尾拼接了 `RUNS_SCHEMA_SQL`），再走 0002 不会报错；
老库（只跑过 0001）走 0002 正好补上。
"""

from __future__ import annotations

import sqlite3
from typing import cast

from alembic import op

from agentworkbench.db.engine import RUNS_SCHEMA_SQL

revision = "0002_agent_runs"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    raw = cast(sqlite3.Connection, bind.connection.driver_connection)
    raw.executescript(RUNS_SCHEMA_SQL)


def downgrade() -> None:
    raise NotImplementedError("删表会连记录一起丢：宁可重建库，也不写一个会静默吞数据的回滚")
