"""providers / provider_models / system_state：平台配置的三张表（F6）。

Revision ID: 0004_providers
Revises: 0003_conversations
Create Date: 2026-11-03

同样**不写 DDL**，直接执行 `db.engine.PROVIDERS_SCHEMA_SQL`——建表函数与
迁移共用同一份 DDL 是 `test_migrations` 那条不变量要求的，理由见 0003。
"""

from __future__ import annotations

import sqlite3
from typing import cast

from alembic import op

from agentworkbench.db.engine import PROVIDERS_SCHEMA_SQL

revision = "0004_providers"
down_revision = "0003_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    raw = cast(sqlite3.Connection, bind.connection.driver_connection)
    raw.executescript(PROVIDERS_SCHEMA_SQL)


def downgrade() -> None:
    raise NotImplementedError("providers 表里是加密凭据：删表会让配置直接丢失，宁可重建库")
