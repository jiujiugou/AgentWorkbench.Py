"""alembic 运行环境：只做一件事——把连接交给迁移脚本。

两条使用路径，不允许再出现第三个：

- CLI：`alembic upgrade head`，连接串来自 `AGENTWORKBENCH_DB` 或 `alembic.ini`
- 代码 / 测试：`config.attributes["connection"] = conn`，完全不走 ini

迁移脚本里不写建表 SQL，而是执行 `db.engine.SCHEMA_SQL`——**schema 只有一个来源**。
两份 DDL 必然会分叉，而分叉时不报错，只在某个客户库上炸。
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from typing import cast

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _url() -> str:
    path = os.environ.get("AGENTWORKBENCH_DB")
    if path:
        return f"sqlite:///{path}"
    return cast(str, config.get_main_option("sqlalchemy.url"))


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    existing = config.attributes.get("connection")
    if existing is not None:
        context.configure(connection=existing, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
