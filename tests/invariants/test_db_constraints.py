"""连接层的两条硬保证：外键真的开着，级联真的生效。

D4 变异测试发现的缺口：把 `db/engine.py` 的 `pragma foreign_keys = on` 改成 `off`，
**没有一条测试会红**。原因是删除路径刻意显式删三张表、不依赖级联（见
`ingest/deletion.py` 的注释），于是「外键开着」这件事从来没有被断言过——
它是连接工厂的一句 pragma，谁删掉都不会有任何功能表现。

**但它是数据完整性的下限**：外键关掉之后，写进一条指向不存在租户的用户、
指向不存在 run 的助手消息都不会报错，只会在某个列表页上表现为「这行数据
查不出归属」。而这类孤儿行是**不会自己消失**的。

改坏后会怎样（用户会看到什么）：把 pragma 改成 off，本文件三条全红；
把 `ON DELETE CASCADE` 从 document_pages 的 DDL 里去掉，级联那条红——
用户删掉一份资料后，正文页仍然留在库里，占用空间且无法通过界面清除。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentworkbench.core.clock import utc_now
from agentworkbench.db import engine

_NOW = utc_now()


@pytest.fixture(name="conn")
def _conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = engine.connect(tmp_path / "constraints.db")
    engine.create_schema(connection)
    yield connection
    connection.close()


def test_foreign_keys_are_enforced_on_every_connection(conn: sqlite3.Connection) -> None:
    """pragma 是连接级开关：`engine.connect` 少写一行，所有连接一起失效。

    直接断言 pragma 值而不是只验行为：这是**配置**，行为断言在删除路径
    显式删表之后抓不到它（D4 的 `db-foreign-keys` 就是这么活下来的）。
    """
    assert conn.execute("pragma foreign_keys").fetchone()[0] == 1


def test_a_row_pointing_at_a_missing_parent_is_rejected(conn: sqlite3.Connection) -> None:
    """孤儿行必须写不进去——不是「以后清理」，是当场报错。"""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "insert into users (id, tenant_id, email, password_hash, role, status,"
            " credential_epoch, failed_attempts, created_at, updated_at)"
            " values ('orphan', 'no-such-tenant', 'o@x.com', 'hash', 'member',"
            " 'active', 1, 0, ?, ?)",
            (_NOW, _NOW),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "insert into document_pages (document_id, page_no, raw_text)"
            " values ('no-such-doc', 1, '正文')"
        )


def test_deleting_a_document_cascades_its_pages(conn: sqlite3.Connection) -> None:
    """级联是 DDL 的一部分：`document_pages` 的 ON DELETE CASCADE 被改成 NO ACTION 时红。"""
    conn.execute(
        "insert into documents (id, tenant_id, file_name, stored_path, sha256,"
        " size_bytes, status, page_count, created_at, updated_at)"
        " values ('d1', 't1', 'a.pdf', 'files/a.pdf', 'sha', 1, 'parsed', 1, ?, ?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "insert into document_pages (document_id, page_no, raw_text) values ('d1', 1, '正文')"
    )
    conn.commit()

    conn.execute("delete from documents where id = 'd1'")
    conn.commit()
    assert conn.execute("select count(*) from document_pages").fetchone()[0] == 0
