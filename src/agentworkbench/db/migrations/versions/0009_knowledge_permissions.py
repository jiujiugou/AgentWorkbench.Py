"""账号等级 / 知识库可见性 / 引用 kb_id（REQ-24 / REQ-25 / ADR 0011）。

Revision ID: 0009_knowledge_permissions
Revises: 0008_conversation_kind
Create Date: 2026-11-06

**不写第二份 DDL**：与启动期共用 `engine.ensure_knowledge_schema`。
迁移额外负责存量数据：每个团队建默认公共库，把现有文档与引用快照
归入默认公共库——否则升级后低等级用户会突然看不到原有资料。
"""

from __future__ import annotations

import sqlite3
from typing import cast

from alembic import op

from agentworkbench.db.engine import ensure_knowledge_schema

revision = "0009_knowledge_permissions"
down_revision = "0008_conversation_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    ensure_knowledge_schema(cast(sqlite3.Connection, bind.connection.driver_connection))


def downgrade() -> None:
    raise NotImplementedError(
        "删掉知识库可见性等于把公共/私人资料重新混在一起：降级脚本无法回答"
        "「原来哪些文档属于私人库」。真要回退，先导出 knowledge_bases 与"
        "documents.kb_id，再人工决定归属——那是产品决定，不该藏在降级脚本里。"
    )
