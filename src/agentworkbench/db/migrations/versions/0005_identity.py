"""tenants / users / tickets / invites / identity_events：身份与访问的五张表（F1）。

Revision ID: 0005_identity
Revises: 0004_providers
Create Date: 2026-11-04

同样**不写 DDL**，直接执行 `db.engine.IDENTITY_SCHEMA_SQL`——建表函数与迁移
共用同一份 DDL 是 `test_migrations` 那条不变量要求的，理由见 0003。

五张表里四张有外键，建表顺序不能换：`tenants` 必须最先（`users` 指向它），
`users` 次之（`tickets` / `invites` 指向它），`identity_events` 刻意没有外键，
放最后只为读起来顺。顺序一旦反了，SQLite 会拒绝建表——外键目标不存在。
"""

from __future__ import annotations

import sqlite3
from typing import cast

from alembic import op

from agentworkbench.db.engine import IDENTITY_SCHEMA_SQL

revision = "0005_identity"
down_revision = "0004_providers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    raw = cast(sqlite3.Connection, bind.connection.driver_connection)
    raw.executescript(IDENTITY_SCHEMA_SQL)


def downgrade() -> None:
    raise NotImplementedError(
        "身份表里是账号与票据：删表等于把所有人锁在系统外，宁可重建库。"
        "真要回退，先导出 users 表并确认引导路径可用——那是一个人工步骤，不该藏在降级脚本里。"
    )
