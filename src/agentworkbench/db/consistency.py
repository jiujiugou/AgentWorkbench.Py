"""索引一致性检查：找出「列表说可检索，搜索却查不到」，以及反向的「失败却可命中」。

对应 F2 §1.3 / §5.5 第 1 条。四类判据来自 C# 侧踩过的坑，**不是猜的**：

[a] `status=parsed` 但 `chunks=0`          —— 切不出块，语义上必然不可检索
[b1] 有块无全文行                          —— 全文路搜不到，列表照样说可检索
[b2] 幽灵全文行                            —— 正文已删，检索仍能命中
[c1] 有块无向量                            —— 语义路永远查不到这篇
[c2] 幽灵向量元数据                        —— 列表按它数出「满」，检索却空
[e]  库级真空                              —— 没有这一条，空库会让前四类全部空转通过
[f]  `status=failed` 却有全文行            —— 反向伪装：失败的东西仍在提供依据

**为什么不能只比三张表的总行数**：局部坏掉时总量依然看起来正常。63 篇里有一篇
`chunks=0`、或者某篇的块全在 `chunks` 但 `chunks_fts` 一行都没有，总量才看得出来。

服务于：REQ-06 / REQ-10（F2）；判据由 `tests/invariants/test_index_consistency.py` 守着。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum


class IssueKind(StrEnum):
    """一类问题的名字。值即报告里的类别标识，**只许增、不许改语义**。"""

    PARSED_WITHOUT_CHUNKS = "a_parsed_without_chunks"
    CHUNK_WITHOUT_FTS = "b1_chunk_without_fts"
    FTS_WITHOUT_CHUNK = "b2_fts_without_chunk"
    CHUNK_WITHOUT_VECTOR = "c1_chunk_without_vector"
    VECTOR_WITHOUT_CHUNK = "c2_vector_without_chunk"
    LIBRARY_EMPTY = "e_library_empty"
    FAILED_BUT_SEARCHABLE = "f_failed_but_searchable"


@dataclass(frozen=True, slots=True)
class Issue:
    """一条问题。`document_id` 为空串表示库级问题（[e]）。"""

    kind: IssueKind
    document_id: str
    detail: str


def _doc_ids(conn: sqlite3.Connection, sql: str) -> list[str]:
    return [str(row[0]) for row in conn.execute(sql).fetchall()]


def _count(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0]) if row else 0


def check_consistency(conn: sqlite3.Connection) -> list[Issue]:
    """返回全部问题。空列表 = 干净。

    只读：不修改连接上的任何数据，可以安全地对生产库副本调用。
    """
    issues: list[Issue] = []

    # [e] 库级真空：0 篇文档，或一篇可检索的都没有。必须排在最前，
    # 否则空库会让其余各类全部「无行可查」而静默通过。
    docs = _count(conn, "select count(*) from documents")
    searchable = _count(
        conn,
        "select count(*) from documents d"
        " where exists (select 1 from chunks c where c.document_id = d.id)",
    )
    if docs == 0 or searchable == 0:
        issues.append(
            Issue(IssueKind.LIBRARY_EMPTY, "", f"文档 {docs} 篇，其中可检索 {searchable} 篇")
        )

    # [a] 说解析好了，却一个块都没有。
    for doc_id in _doc_ids(
        conn,
        "select d.id from documents d where d.status = 'parsed'"
        " and not exists (select 1 from chunks c where c.document_id = d.id)",
    ):
        issues.append(Issue(IssueKind.PARSED_WITHOUT_CHUNKS, doc_id, "parsed 但 chunks=0"))

    # [b1] 块在，全文行不在 —— 全文路搜不到。
    for doc_id in _doc_ids(
        conn,
        "select c.document_id from chunks c"
        " where not exists (select 1 from chunks_fts f where f.chunk_id = c.id)"
        " group by c.document_id",
    ):
        issues.append(Issue(IssueKind.CHUNK_WITHOUT_FTS, doc_id, "有块无全文行"))

    # [b2] 全文行在，块不在 —— 正文已删仍能命中。
    for doc_id in _doc_ids(
        conn,
        "select f.document_id from chunks_fts f"
        " where not exists (select 1 from chunks c where c.id = f.chunk_id)"
        " group by f.document_id",
    ):
        issues.append(Issue(IssueKind.FTS_WITHOUT_CHUNK, doc_id, "幽灵全文行"))

    # [c1] 块在，向量元数据不在 —— 语义路查不到（列表还会显示 text_only）。
    for doc_id in _doc_ids(
        conn,
        "select c.document_id from chunks c"
        " where not exists (select 1 from chunks_vec v where v.ChunkId = c.id)"
        " group by c.document_id",
    ):
        issues.append(Issue(IssueKind.CHUNK_WITHOUT_VECTOR, doc_id, "有块无向量"))

    # [c2] 向量元数据在，块不在 —— 列表按它数出「满」，检索却什么都没有。
    for doc_id in _doc_ids(
        conn,
        "select v.DocumentId from chunks_vec v"
        " where not exists (select 1 from chunks c where c.id = v.ChunkId)"
        " group by v.DocumentId",
    ):
        issues.append(Issue(IssueKind.VECTOR_WITHOUT_CHUNK, doc_id, "幽灵向量元数据"))

    # [f] 处理失败，却仍在提供依据 —— 比「失败被当成没有」更坏，因为它会被引用。
    for doc_id in _doc_ids(
        conn,
        "select d.id from documents d where d.status = 'failed'"
        " and exists (select 1 from chunks_fts f where f.document_id = d.id)",
    ):
        issues.append(Issue(IssueKind.FAILED_BUT_SEARCHABLE, doc_id, "failed 却有全文行"))

    return issues
