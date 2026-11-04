"""索引写入：正文块、全文索引、向量三处同时写。

三处任一缺失都要作为结果回到调用方，否则列表显示「可检索」、
检索却全空——用户会去查检索和阈值，方向全错。

三条实现约束（每条都对应一次踩过的坑）：
1. **先删后写**：重解析同一份文档时不清旧行，`seq` 会叠加，块数越滚越多。
2. **一个事务**：三处分开提交，中途失败会留下"有块没全文"的半成品。
3. **全文表存 token 串、不存原文**：`unicode61` 把整句中文当一个 token，
   所以写进去的是 `tokenizer.to_index_text(...)` 的结果。原文在 `chunks.content`。

来源：C# `Knowledge.Local/Indexing/ChunkIndexer.cs`、`VectorIndex.cs`
相关决策：ADR 0001（parsed 的含义是全文索引就绪）、ADR 0002（一个事务）
服务于：REQ-06 / REQ-10
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from agentworkbench.core.documents import ChunkDraft
from agentworkbench.rag.freshness import ensure_index_fresh
from agentworkbench.rag.tokenizer import single_han_chars, to_index_text, to_match_query


@dataclass(frozen=True)
class IndexCounts:
    """三处计数。`parsed` 的判据就是这三个数相等且大于 0。"""

    chunks: int
    fts: int
    vectors: int

    @property
    def aligned(self) -> bool:
        return self.chunks > 0 and self.chunks == self.fts == self.vectors


@dataclass(frozen=True)
class SearchHit:
    chunk_id: int
    document_id: str
    content: str


def write_index(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    drafts: list[ChunkDraft],
    embeddings: list[list[float]],
    now: str,
) -> None:
    """把一批块写进三处。调用方负责事务外的一切（解析、切块、向量化）。

    不在这里 catch：写失败就是写失败，由调用方决定标 `index_transient`
    还是 `index_permanent`——这两个码的区别在调用方知道的信息里。
    """
    if len(drafts) != len(embeddings):
        raise ValueError(f"块与向量数量不一致：{len(drafts)} vs {len(embeddings)}")

    clear_index(conn, document_id)

    for draft, embedding in zip(drafts, embeddings, strict=True):
        cursor = conn.execute(
            "insert into chunks(document_id, seq, page_from, page_to, heading, content,"
            " char_count, created_at) values (?,?,?,?,?,?,?,?)",
            (
                document_id,
                draft.seq,
                draft.page_from,
                draft.page_to,
                draft.heading,
                draft.content,
                len(draft.content),
                now,
            ),
        )
        chunk_id = int(cursor.lastrowid or 0)

        conn.execute(
            "insert into chunks_vec(ChunkId, DocumentId, Seq, Heading, PageFrom, PageTo)"
            " values (?,?,?,?,?,?)",
            (chunk_id, document_id, draft.seq, draft.heading, draft.page_from, draft.page_to),
        )
        conn.execute(
            "insert into vec_chunks_vec(ChunkId, Embedding) values (?,?)",
            (chunk_id, json.dumps(embedding)),
        )
        conn.execute(
            "insert into chunks_fts(content, document_id, chunk_id) values (?,?,?)",
            # chunk_id 必须是 int：fts5 虚表的列没有类型，存什么还什么。
            # 曾经写成 str(chunk_id)——单表查询自洽看不出，跨表与 chunks.id（整数）比对时
            # '487' in {487} 永远 False，检索全 0 且不报错。评测（T-002）当场抓住了它。
            (to_index_text(draft.content), document_id, chunk_id),
        )


def clear_index(conn: sqlite3.Connection, document_id: str) -> None:
    """清三处。顺序固定：全文 → 向量 → 正文块。

    顺序固定是为了让失败时的残留**可判**：如果先删正文块，`chunks_vec` 里的
    元数据就失去了对照，残留行查不出属于哪一块。
    """
    conn.execute("delete from chunks_fts where document_id = ?", (document_id,))
    conn.execute(
        "delete from vec_chunks_vec where ChunkId in"
        " (select ChunkId from chunks_vec where DocumentId = ?)",
        (document_id,),
    )
    conn.execute("delete from chunks_vec where DocumentId = ?", (document_id,))
    conn.execute("delete from chunks where document_id = ?", (document_id,))


def count_index(conn: sqlite3.Connection, document_id: str) -> IndexCounts:
    """数三处。这是判断"可检索"的唯一依据——不看 `documents.status`。"""
    chunks = conn.execute(
        "select count(*) from chunks where document_id = ?", (document_id,)
    ).fetchone()[0]
    fts = conn.execute(
        "select count(*) from chunks_fts where document_id = ?", (document_id,)
    ).fetchone()[0]
    vectors = conn.execute(
        "select count(*) from chunks_vec where DocumentId = ?", (document_id,)
    ).fetchone()[0]
    return IndexCounts(chunks=int(chunks), fts=int(fts), vectors=int(vectors))


def _match_rows(
    conn: sqlite3.Connection, query: str, limit: int, tenant_id: str
) -> list[sqlite3.Row]:
    """① MATCH 主路：OR 语义，命中任意 token 即入选。

    团队条件下推成 `document_id in (...)` 子查询，**不是取回来再过滤**：
    后者会让「前 k 条全属于别的团队」时本团队一条也拿不到，而界面上表现为
    「库里明明有，就是搜不出来」——一个不会报错、只会被当成索引坏了的现象。
    """
    match = to_match_query(query)
    if not match:
        return []
    return conn.execute(
        "select chunk_id, document_id from chunks_fts"
        " where chunks_fts match ?"
        " and document_id in (select id from documents where tenant_id = ?)"
        " order by rank limit ?",
        (match, tenant_id, limit),
    ).fetchall()


def _like_rows(
    conn: sqlite3.Connection, query: str, limit: int, tenant_id: str
) -> list[sqlite3.Row]:
    """② LIKE 兜底：单字汉字（`泵`、`站`）不产生 bigram，MATCH 永远够不着。

    查 `chunks_fts.content`（索引串）而不是 `chunks.content`（原文）——评测脚本
    查的就是前者。两边查不同的表就又是一次口径背离，与本次修的 bug 同类。
    """
    singles = single_han_chars(query)
    if not singles:
        return []
    clauses = " OR ".join(["content like ?"] * len(singles))
    return conn.execute(
        # S608 是误报：clauses 只拼「content like ?」占位符，值全部走参数绑定
        f"select chunk_id, document_id from chunks_fts where {clauses}"  # noqa: S608
        " and document_id in (select id from documents where tenant_id = ?)"
        " order by chunk_id limit ?",
        [f"%{char}%" for char in singles] + [tenant_id, limit],
    ).fetchall()


def search_fts(
    conn: sqlite3.Connection, query: str, *, tenant_id: str, limit: int = 5
) -> list[SearchHit]:
    """全文检索。查询串与索引串走同一个 `tokenizer`，否则命中 0 条且不报错。

    **`tenant_id` 必填、没有默认值**（`I-V5`）：默认 `None` 表示「不过滤」的版本，
    是「忘传等于全放行」的入口，而漏传一个关键字参数在 Python 里是 `TypeError`，
    在 SQL 里却是「什么都查得到」——后者不会报错，只会静默地跨团队。

    两步，与 C# `HybridRetriever.FtsSearchAsync` 同序：
    ① MATCH（OR 语义）
    ② **命中 0 条才**走 LIKE 兜底——兜底不是主路，只补单字查询那一类。

    为什么必须是 OR：中文长问句切出的 bigram 里有「是什」「么原」这类跨词组合，
    任何文档都不会同时具备；AND 会让每一句正常提问都必然 0 命中（见
    `core/tokenizer.py::to_match_query` 的注释）。

    评测脚本（`evals/self/run_rag_eval.py`）有一份同口径实现，两边对同一查询
    必须命中同一批块——`tests/invariants/test_query_contract.py` 守着。
    """
    # 入口闸门（S-V10）：索引 stale 时报错，不许走到下面返回空列表。
    # 与 `retrieve` 各放一次——只在一处放，另一条路就绕过了。
    ensure_index_fresh(conn)
    rows = _match_rows(conn, query, limit, tenant_id)
    if not rows:
        rows = _like_rows(conn, query, limit, tenant_id)
    hits: list[SearchHit] = []
    for row in rows:
        chunk_id = int(row["chunk_id"])
        content_row = conn.execute(
            "select content, document_id from chunks where id = ?", (chunk_id,)
        ).fetchone()
        if content_row is None:
            # 全文行还在、正文块已删：正是 `[b2]` 幽灵全文，这里不替它遮掩。
            continue
        hits.append(
            SearchHit(
                chunk_id=chunk_id,
                document_id=str(content_row["document_id"]),
                content=str(content_row["content"]),
            )
        )
    return hits
