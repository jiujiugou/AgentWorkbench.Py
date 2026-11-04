"""不变量：卡死判定（F2 V-7）。

「处理中不会变成永久态」的第二半条：`pending` 有 `recover_pending` 兜底，
`parsing` 有 `scan_stalled` 判死。改坏它（判据写成 created_at、T 写死、
不清索引残留）用户会看到：worker 死掉后文档永远停在 parsing——列表显示
"处理中"，用户等一个永远不会来的结果；更糟的是"failed 却可命中"的文档
会混进检索结果。

判据是 `updated_at` 而不是 `created_at`：800 页的文档可以合法地解析很久，
但它不能在同一个状态上停很久。用 created_at 当判据会把"慢"判成"死"。

服务于：V-7（pyproject 验证登记表）
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from agentworkbench.core.documents import DocStatus, DocumentErrorCode
from agentworkbench.db import engine
from agentworkbench.ingest.queue import scan_stalled
from agentworkbench.rag.indexer import count_index

NOW = datetime(2026, 11, 2, 12, 0, 0)
STAMP = NOW.strftime("%Y-%m-%dT%H:%M:%S")


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = engine.connect(":memory:")
    engine.create_schema(c)
    return c


def _parsing_document(
    conn: sqlite3.Connection,
    doc_id: str,
    *,
    updated_at: str,
    created_at: str = "2026-11-01T00:00:00",
    with_index: bool = False,
) -> None:
    """造一条 parsing 记录。`with_index` 顺手写半份索引，验证判死时会清残留。"""
    conn.execute(
        "insert into documents(id, tenant_id, file_name, stored_path, sha256, size_bytes,"
        " status, page_count, created_at, updated_at)"
        " values (?,?,?,?,?,?,?,?,?,?)",
        (
            doc_id,
            "t1",
            "卡死.pdf",
            "t1/aa/aabb.pdf",
            "aabb",
            10,
            DocStatus.PARSING,
            0,
            created_at,
            updated_at,
        ),
    )
    if with_index:
        # 半份索引：正文块在、全文行也在。判死必须连它一起清，
        # 否则"failed 却可命中"——那正是 V-5 的负控要抓的坏数据。
        conn.execute(
            "insert into chunks(document_id, seq, page_from, page_to, content, char_count,"
            " created_at) values (?,?,?,?,?,?,?)",
            (doc_id, 0, 1, 1, "半途而废的块", 6, STAMP),
        )
        conn.execute(
            "insert into chunks_fts(chunk_id, document_id, content) values (?,?,?)",
            (conn.execute("select last_insert_rowid()").fetchone()[0], doc_id, "半途而废的块"),
        )
    conn.commit()


def test_stale_parsing_is_killed_and_index_cleared(conn: sqlite3.Connection) -> None:
    """正控：parsing 停留超过 T → failed / parse_stalled，半份索引一起清。"""
    _parsing_document(conn, "doc-stale", updated_at="2026-11-02T11:00:00", with_index=True)

    killed = scan_stalled(conn, now=NOW, stall_minutes=15)

    assert killed == ["doc-stale"]
    row = conn.execute("select * from documents where id = 'doc-stale'").fetchone()
    assert row["status"] == str(DocStatus.FAILED)
    assert row["error_code"] == str(DocumentErrorCode.PARSE_STALLED)
    counts = count_index(conn, "doc-stale")
    assert (counts.chunks, counts.fts, counts.vectors) == (0, 0, 0)


def test_fresh_parsing_is_not_killed(conn: sqlite3.Connection) -> None:
    """负控一：parsing 但刚有进展（updated_at 新）→ 不能误杀。"""
    _parsing_document(conn, "doc-fresh", updated_at="2026-11-02T11:55:00")

    killed = scan_stalled(conn, now=NOW, stall_minutes=15)

    assert killed == []
    row = conn.execute("select status from documents where id = 'doc-fresh'").fetchone()
    assert row["status"] == str(DocStatus.PARSING)


def test_old_created_at_alone_does_not_kill(conn: sqlite3.Connection) -> None:
    """负控二（这条是 V-7 的灵魂）：created_at 很老、updated_at 很新 → 不杀。

    800 页文档从昨天解析到今天，是"慢"，不是"死"。判据写错成 created_at
    时这条红——它存在的意义就是把这个错误钉死。
    """
    _parsing_document(
        conn, "doc-slow", created_at="2026-11-01T00:00:00", updated_at="2026-11-02T11:55:00"
    )

    assert scan_stalled(conn, now=NOW, stall_minutes=15) == []


def test_threshold_is_configurable(conn: sqlite3.Connection) -> None:
    """T 可配置。写死就等于没有（V-7 判据原话）。"""
    # 同一条记录停了 10 分钟：默认 15 分钟不杀，5 分钟就杀。
    # 注意判据是「超过 T」（updated_at < now - T），恰好等于 T 不算——慢半拍无害。
    _parsing_document(conn, "doc-t5", updated_at="2026-11-02T11:50:00")
    assert scan_stalled(conn, now=NOW) == []
    assert scan_stalled(conn, now=NOW, stall_minutes=5) == ["doc-t5"]


def test_parsed_documents_are_never_touched(conn: sqlite3.Connection) -> None:
    """只有 parsing 会被判死。parsed 是终态，不是"卡住"。"""
    conn.execute(
        "insert into documents(id, tenant_id, file_name, stored_path, sha256, size_bytes,"
        " status, page_count, created_at, updated_at) values (?,?,?,?,?,?,?,?,?,?)",
        (
            "doc-parsed",
            "t1",
            "正常.pdf",
            "t1/bb/bbcc.pdf",
            "bbcc",
            10,
            DocStatus.PARSED,
            3,
            "2026-11-01T00:00:00",
            "2026-11-01T00:10:00",
        ),
    )
    conn.commit()

    assert scan_stalled(conn, now=NOW, stall_minutes=15) == []
