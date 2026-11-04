"""conversations / messages / message_citations：会话与记录的三张表（F5）。

Revision ID: 0003_conversations
Revises: 0002_agent_runs
Create Date: 2026-11-03

同样**不写 DDL**，直接执行 `db.engine.CONVERSATIONS_SCHEMA_SQL`——理由与 0001 / 0002 一致：
`create_schema()`（测试与空库起服务）与 alembic（升级）是两条入口，各写一份建表语句
必然在某次改动后分叉，而分叉的表现是"新装的环境能跑、升级的环境缺一列"。

三条 DDL 都是 `IF NOT EXISTS`，所以这个迁移是幂等的：新库跑完 0001 已带上这三张表
（`SCHEMA_SQL` 末尾拼接了 `CONVERSATIONS_SCHEMA_SQL`），再走 0003 不会报错；
老库（只跑过 0002）走 0003 正好补上。
"""

from __future__ import annotations

import sqlite3
from typing import cast

from alembic import op

from agentworkbench.db.engine import CONVERSATIONS_SCHEMA_SQL

revision = "0003_conversations"
down_revision = "0002_agent_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    raw = cast(sqlite3.Connection, bind.connection.driver_connection)
    raw.executescript(CONVERSATIONS_SCHEMA_SQL)


def downgrade() -> None:
    raise NotImplementedError("删表会连会话内容一起丢：宁可重建库，也不写一个会静默吞数据的回滚")
