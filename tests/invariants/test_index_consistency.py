"""不变量：索引一致性检查器必须能报出五类"说可检索却搜不到"，以及反向的"失败却可命中"。

被改坏时用户会看到：列表显示"可检索"，搜什么都空，于是去调召回和阈值——
方向全错，而且没有任何告警（F2 §1.3 / §5.5 第 1 条）。

这一条现在就能写：它只读数据库，schema 在 fixture 里就有，不需要任何业务代码。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentworkbench.db import engine
from agentworkbench.db.consistency import IssueKind, check_consistency

pytestmark = pytest.mark.invariant

FIXTURE = Path(__file__).resolve().parents[2] / "evals" / "fixtures" / "agentworkbench.db"


TENANT = "default"
"""这些测试的数据租户。检索必填它，夹具就把数据造在这里。"""


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """把 fixture 复制进内存库（WAL 下不能用 cp，用 backup API）。

    复制进 `:memory:` 而不是磁盘临时文件：8 条用例各复制一次，落盘版本要 20 秒，
    内存版 2 秒。慢测试等于没测试。
    """
    src = sqlite3.connect(f"file:{FIXTURE.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(":memory:")
    src.backup(target)
    src.close()
    target.row_factory = sqlite3.Row
    # fixture 库是 F2 时期的快照：`documents` 没有 `tenant_id` 列，而 F1 之后
    # 检索的团队条件走它（`search_fts` 的子查询）。这里补列而不是重做二进制库——
    # 这份 fixture 的价值在**索引内容**（幽灵行、错位块那些形态），不在 schema 版本。
    target.execute("alter table documents add column tenant_id text not null default 'default'")
    # vec0 虚表模块必须显式加载——backup 过来的库有 vec 表但没有扩展，
    # 任何碰 vec_chunks_vec 的语句都会报 no such module（[d] 正控测试踩过）。
    engine.load_vec(target)
    try:
        yield target
    finally:
        target.close()


def _kinds(c: sqlite3.Connection) -> set[IssueKind]:
    return {issue.kind for issue in check_consistency(c)}


def _one_doc(c: sqlite3.Connection) -> str:
    row = c.execute("select id from documents limit 1").fetchone()
    assert row is not None
    return str(row[0])


def test_fixture_is_clean(conn: sqlite3.Connection) -> None:
    """正控：C# 产出的这份库是干净的（63 文档 / 439 块 / 439 全文 / 439 向量）。"""
    assert check_consistency(conn) == []


def test_parsed_without_chunks(conn: sqlite3.Connection) -> None:
    """[a] status=parsed 但切不出块 —— 语义上必然不可检索。"""
    conn.execute(
        "insert into documents(id, file_name, stored_path, sha256, size_bytes, status,"
        " page_count, created_at) values('probe-a','a.pdf','p','h',1,'parsed',1,'2026-01-01')"
    )
    assert IssueKind.PARSED_WITHOUT_CHUNKS in _kinds(conn)


def test_chunk_without_fts(conn: sqlite3.Connection) -> None:
    """[b1] 有块无全文行 —— 全文路搜不到，但列表照样说可检索。"""
    doc = _one_doc(conn)
    conn.execute("delete from chunks_fts where document_id = ?", (doc,))
    assert IssueKind.CHUNK_WITHOUT_FTS in _kinds(conn)


def test_fts_without_chunk(conn: sqlite3.Connection) -> None:
    """[b2] 幽灵全文行 —— 正文已删，检索仍能命中。"""
    conn.execute(
        "insert into chunks_fts(content, document_id, chunk_id) values('probe','ghost',999999)"
    )
    assert IssueKind.FTS_WITHOUT_CHUNK in _kinds(conn)


def test_chunk_without_vector(conn: sqlite3.Connection) -> None:
    """[c1] 有块无向量 —— 语义路永远查不到这篇。"""
    doc = _one_doc(conn)
    conn.execute("delete from chunks_vec where DocumentId = ?", (doc,))
    assert IssueKind.CHUNK_WITHOUT_VECTOR in _kinds(conn)


def test_vector_without_chunk(conn: sqlite3.Connection) -> None:
    """[c2] 幽灵向量元数据 —— 列表按它数出"满"，检索却什么都没有。"""
    conn.execute(
        "insert into chunks_vec(ChunkId, DocumentId, Seq, PageFrom, PageTo)"
        " values(999998,'ghost',0,1,1)"
    )
    assert IssueKind.VECTOR_WITHOUT_CHUNK in _kinds(conn)


def test_library_vacuum(conn: sqlite3.Connection) -> None:
    """[e] 库级真空 —— 没有这一条，空库会让前四类全部空转通过。"""
    conn.execute("delete from chunks")
    assert IssueKind.LIBRARY_EMPTY in _kinds(conn)


def test_failed_but_searchable(conn: sqlite3.Connection) -> None:
    """[f] status=failed 却有全文行 —— 反向伪装：失败的东西仍在提供依据。"""
    doc = _one_doc(conn)
    conn.execute("update documents set status = 'failed' where id = ?", (doc,))
    assert IssueKind.FAILED_BUT_SEARCHABLE in _kinds(conn)


def test_searchable_document_is_reachable_by_match(conn: sqlite3.Connection) -> None:
    """[d] 的正控：判"可检索"的文档，必须真能被 MATCH 查到。

    检查器的 [d] 类抓的是反向（判可检索却查不到）；这里验正向：
    走生产写入路径（tokenizer + write_index），再用与查询端相同的 token 构造
    MATCH，gold 词必须命中——索引与查询两端任何一处口径分叉（ASCII 词切分、
    CJK 范围、token 顺序、值类型），这条当场红。T-002 接入 LlamaIndex 后真实
    复现过一次：chunk_id 被写成字符串，单表自洽、跨表比对全 0 且不报错。
    """
    from agentworkbench.core.documents import ChunkDraft
    from agentworkbench.llm.embeddings import HashEmbedder
    from agentworkbench.rag.indexer import search_fts, write_index

    doc_id = _one_doc(conn)
    draft = ChunkDraft(seq=999, page_from=1, page_to=1, content="FC01 读线圈 H5U-Modbus 通讯参数表")
    write_index(
        conn,
        document_id=doc_id,
        drafts=[draft],
        embeddings=HashEmbedder()([draft.content]),
        now="2026-11-02T00:00:00",
    )
    conn.commit()

    # 查询端（search_fts 内部用 to_match_query 构造 MATCH，与索引同一 tokenize）
    hits = search_fts(conn, "FC01 读线圈", tenant_id=TENANT, limit=5)
    assert hits, "gold 词必须能命中——0 命中且不报错正是这条要堵死的失败方式"
    assert all(isinstance(hit.chunk_id, int) for hit in hits), (
        "chunk_id 必须是 int：fts5 虚表列无类型，写成 str 时跨表比对静默全 0"
    )

    # 裸 MATCH（不经封装）：验证索引文本本身可被 FTS5 语法命中
    row = conn.execute(
        "select chunk_id from chunks_fts where chunks_fts match ?", ('"FC01" OR "读线"',)
    ).fetchone()
    assert row is not None
