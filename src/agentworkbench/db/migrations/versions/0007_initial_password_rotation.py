"""`users.must_change_password`：首个管理员首登必须改密（REQ-AUTH-11 / I-V19）。

Revision ID: 0007_initial_password_rotation
Revises: 0006_platform_admin
Create Date: 2026-11-05

为什么是加一列而不是复用 `credential_epoch`：epoch 是凭据版本号，管理员
重置口令时也会 +1。用 `is_platform_admin and epoch == 1` 推断“还没改初始
口令”，在重置之后就会把事实抹掉——用户拿着运维给的一次性口令可以一直用。
这一列回答的是另一件事，必须自己存在。

加列逻辑与启动期补列共用 `engine.ensure_must_change_password_column`：
两份 DDL 必然漂移，而漂移的表现是“新装能跑、升级库缺列”。
"""

from __future__ import annotations

import sqlite3
from typing import cast

from alembic import op

from agentworkbench.db.engine import ensure_must_change_password_column

revision = "0007_initial_password_rotation"
down_revision = "0006_platform_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    ensure_must_change_password_column(cast(sqlite3.Connection, bind.connection.driver_connection))


def downgrade() -> None:
    raise NotImplementedError(
        "删掉这一列等于取消“首登必须改密”：已经轮换过的账号看不出来，"
        "而还没轮换的账号会静默恢复成长期有效。真要回退，先回答"
        "“那些还拿着初始口令的账号怎么办”——那是产品决定，不该藏在降级脚本里。"
    )
