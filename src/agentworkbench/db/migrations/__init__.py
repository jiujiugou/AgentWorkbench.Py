"""数据库迁移（Alembic）。

不逐个移植 C# 的 27 个迁移号：现存文件只到 19 个，M005 与 M007~M013 共 8 个号空着，
还躺着一条 DropOrphanTables —— 迁移历史已经不可信。
做法是从现状 schema 反推一条 initial，然后从零叠上去。

来源：C# `Persistence/Migrations/`
"""

from __future__ import annotations
