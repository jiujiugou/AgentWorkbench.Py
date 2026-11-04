"""`users.is_platform_admin`：平台级配置的写权限（REQ-AUTH-12）。

Revision ID: 0006_platform_admin
Revises: 0005_identity
Create Date: 2026-11-04

为什么是加一列而不是加一个角色档：`role` 与这一列是**两个范围**。
`role` 说「在我这个团队里能管谁」，这一列说「能不能改这套安装」。
`providers` / `provider_models` / `system_state` 三张表没有团队列，
所以改它们的权限不可能由任何团队角色承载——第二个团队出现的那天，
「A 队管理员改 B 队供应商」就会变成一条合法操作。

为什么不复用 `IDENTITY_SCHEMA_SQL`（0003/0005 的做法）：那些迁移建的是**新表**，
`CREATE TABLE IF NOT EXISTS` 是新库老库都对的写法。这里改的是**已存在的表**，
`IF NOT EXISTS` 对加列无效（它只判断表在不在，不判断列在不在）。
所以这里必须写 ``ALTER TABLE``，而且要先用 ``PRAGMA table_info`` 探一次：
新库走「建表 → 再按版本号迁移」的路径，建表时列已经在，直接 ALTER 会
``duplicate column name`` 而在启动时失败。

列上带 ``DEFAULT 0``：老库里的账号都是团队账号，不是平台所有者。
**默认值不是 0 反而危险**——那些库里的第一个管理员会在升级后静默获得改全平台配置的权力。
"""

from __future__ import annotations

import sqlite3
from typing import cast

from alembic import op

revision = "0006_platform_admin"
down_revision = "0005_identity"
branch_labels = None
depends_on = None

_COLUMN = "is_platform_admin"


def add_platform_admin_column(conn: sqlite3.Connection) -> bool:
    """给 `users` 补上这一列。**返回是否真的加过**（已存在返回 `False`）。

    抽成独立函数的原因不是好看：`upgrade()` 需要 alembic 的绑定胶水，而那段胶水
    在测试里要么被 mock（等于没测），要么得引入 SQLAlchemy 引擎（本项目不用它）。
    抽出之后，测试直接调这个函数——测的是**同一份**代码，绕开的只是绑定。
    """
    columns = {row[1] for row in conn.execute("pragma table_info(users)").fetchall()}
    if _COLUMN in columns:
        return False
    conn.execute(
        f"alter table users add column {_COLUMN} integer not null default 0"
        f" check ({_COLUMN} in (0, 1))"
    )
    return True


def upgrade() -> None:
    bind = op.get_bind()
    add_platform_admin_column(cast(sqlite3.Connection, bind.connection.driver_connection))


def downgrade() -> None:
    raise NotImplementedError(
        "这一列记的是「谁能改这套安装」：删掉它等于把平台配置的写入口还给所有人，"
        "而那个状态不会报错。真要回退，先回答「回退之后谁管供应商」——那是一个决定，"
        "不该藏在降级脚本里。"
    )
