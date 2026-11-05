"""`conversations.kind`：通用对话与知识库问答是两个入口（F7 / REQ-23）。

Revision ID: 0008_conversation_kind
Revises: 0007_initial_password_rotation
Create Date: 2026-11-06

默认 `knowledge` 不是随手选的：F7 之前创建的会话全部来自知识库问答。
升级时把它们标成 general，会让带引用的历史被界面当成“未使用企业资料”，
而两种答案合同从此无法从数据上区分。新入口必须显式传 kind；默认值只负责
把老库接上来。

加列与不可变触发器共用 `engine.ensure_conversation_kind_column`——启动期补列
和 Alembic 迁移必须执行同一份逻辑，否则新装能跑、升级库缺列。
"""

from __future__ import annotations

import sqlite3
from typing import cast

from alembic import op

from agentworkbench.db.engine import ensure_conversation_kind_column

revision = "0008_conversation_kind"
down_revision = "0007_initial_password_rotation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    ensure_conversation_kind_column(cast(sqlite3.Connection, bind.connection.driver_connection))


def downgrade() -> None:
    raise NotImplementedError(
        "删掉 kind 等于把通用会话与知识库会话合并成一种历史：带引用的回答和"
        "“未使用企业资料”的回答再也分不开。真要回退，先回答历史里的答案合同"
        "怎么解释——那是产品决定，不该藏在降级脚本里。"
    )
