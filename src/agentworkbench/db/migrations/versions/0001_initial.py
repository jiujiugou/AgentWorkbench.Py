"""initial：从 C# 现状 schema 反推的一条基线。

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-02

**不写 DDL**，直接执行 `db.engine.SCHEMA_SQL`。这样做是因为：
迁移脚本与 `create_schema()` 是两条入口，如果各写一份建表语句，
它们一定会在某次改动后分叉——而分叉时不报错，只在某个客户的库上炸。
`tests/invariants/test_migrations.py` 会比对两条入口产出的表结构。
"""

from __future__ import annotations

import sqlite3
from typing import cast

from alembic import op

from agentworkbench.db.engine import SCHEMA_SQL, load_vec

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 一段 SQL 里有几十条 DDL，`executescript` 才能一次跑完；
    # 逐个 split(";") 会在将来某条 DDL 含分号时静默切错。
    bind = op.get_bind()
    raw = cast(sqlite3.Connection, bind.connection.driver_connection)
    # `vec_chunks_vec` 是 vec0 虚表，扩展没加载时建表直接报 no such module。
    # 用 engine 里同一个函数，避免"迁移能建、运行建不出"。
    load_vec(raw)
    raw.executescript(SCHEMA_SQL)


def downgrade() -> None:
    raise NotImplementedError("initial 迁移不回退：删库重来比回滚更安全，也更可能对")
