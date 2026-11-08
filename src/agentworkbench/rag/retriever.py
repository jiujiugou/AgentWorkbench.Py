"""混合召回：全文 + 向量双路，倒数排名融合取前五。

组件分工（toml 组件表 + F3 §二）：向量存储是 sqlite-vec（adopted），
embedding 客户端归 LlamaIndex 两用之一（现 HashEmbedder 占位走端口注入，
bge-m3 接入时只换实现不改这里），FTS5 走 sqlite3 标准库。
RRF 自写约二十行是文档定案：没有可调权重，引库反引参数（§二）。

判据照 C# `HybridRetriever`：RRF k=60；候选宽度 ≥ topK（先取宽候选、
融合后截断）；超长问句截到 24 个 bigram（一个字产生一个 token，
24 覆盖约 25 字，多出的部分对召回已无增量）。

两条结构约束在这里落地：
- **融合分只用于排序**（D-01）：RRF 第一名恒为 1/(k+1)，只表达"这轮排第一"，
  不表达"有多相关"——库里完全没有答案的问题第一名照样是 1/61。
  判定层的输入是向量距离（`vector_distance`），拿不到融合分。
- **证据只取一次**（§1.6）：一次调用返回的列表原样分发给模型、来源面板、
  引用校验。函数可复算：同库同问两次，结果逐条相同。

来源：C# `Knowledge.Local/Retrieval/HybridRetriever.cs`
服务于：REQ-07（可用三 / 可用四）、G3
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Protocol, cast

import sqlite_vec  # type: ignore[import-untyped]

from agentworkbench.rag.freshness import ensure_index_fresh
from agentworkbench.rag.indexer import search_fts
from agentworkbench.rag.tokenizer import tokenize

RRF_K = 60
"""RRF 常数（C# 同款）：score = Σ 1/(k + rank_i)。"""

MAX_QUERY_TOKENS = 24
"""一次查询最多用多少个 bigram 构建 MATCH：24 覆盖约 25 字。"""

CANDIDATE_K = 10
"""每路候选宽度：先取宽候选供融合，再截断到 topK。"""


class QueryEmbedder(Protocol):
    """查询向量化端口。与写入侧共用实现（同模型），结构化匹配即可。"""

    def __call__(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class RetrievalHit:
    """一次召回的一条命中。

    `vector_distance` 是判定层的唯一输入来源：向量路命中带距离，
    全文路命中为 None（judge 把 None 视作"这条没有距离"）。
    `rrf_score` 只用于本列表内部的排序，判定与引用层不消费它。
    """

    chunk_id: int
    document_id: str
    # 文件名随命中一起带出：界面要靠它写出处，而界面不该再回库查第二遍（§1.6）
    file_name: str
    content: str
    seq: int
    page_from: int
    page_to: int
    heading: str | None
    rrf_score: float
    vector_distance: float | None
    kb_id: str | None = None


def retrieve(
    conn: sqlite3.Connection,
    query: str,
    *,
    embedder: QueryEmbedder | None,
    tenant_id: str,
    kb_ids: frozenset[str] | None = None,
    top_k: int = 5,
    candidate_k: int = CANDIDATE_K,
) -> list[RetrievalHit]:
    """一次混合检索。`tenant_id` 是 REQ-07 的团队范围。

    **必填、没有默认值**（`I-V5`）。这是一个刻意的破坏性签名：默认 `None`
    表示「不过滤」的版本，是「忘传等于全放行」的入口，而它不会报错——
    只会让检索在某个部署里悄悄返回别的团队的片段。改成必填之后，漏传是
    `TypeError`，在第一次调用时就炸，而不是在某次演示里被客户看见。

    两条路各自过滤（`search_fts` 的 SQL + `_meta_of` 的 join），**不在这里
    过滤融合结果**：向量路返回的是 chunk_id，只有回表才知道它属于谁；
    取回来再丢会让「前 k 条全是你团队之外的」时本团队一条也拿不到。

    **`embedder=None` = 只走词面路**（未配置语义模型时的降级形态）。这不是调试
    开关，是一个合法部署态：语义路没接上时不硬凑一路噪声进去。代价写在
    判定层——此时所有命中都没有向量距离，`judge` 会返回 `UNDETERMINED /
    no_vector`，生成照常进行并在界面披露「未判定」（F3 §1.3 的既定行为）。
    """
    if not query or not query.strip():
        return []
    # 入口闸门（S-V10）：ask 走这里。`search_fts` 里也有一次——两条入口各自
    # 独立成立，不靠"反正会调到那边"。
    ensure_index_fresh(conn)

    k = max(candidate_k, top_k)
    fused: dict[int, dict[str, object]] = {}

    def _accumulate(
        chunk_id: int,
        rrf: float,
        vector_distance: float | None,
        meta: dict[str, object],
    ) -> None:
        if chunk_id not in fused:
            fused[chunk_id] = {**meta, "rrf": rrf, "distance": vector_distance}
        else:
            entry = fused[chunk_id]
            entry["rrf"] = cast(float, entry["rrf"]) + rrf
            if vector_distance is not None:
                # 两路同块时保留向量距离：判定要用的就是它
                entry["distance"] = vector_distance

    # 全文路（FTS5/BM25 rank 排序）；超长问句截到 24 个 bigram
    text_hits = search_fts(conn, _capped_query(query), tenant_id=tenant_id, limit=k, kb_ids=kb_ids)
    for rank, hit in enumerate(text_hits):
        meta = _meta_of(conn, hit.chunk_id, hit.document_id, hit.content, tenant_id, kb_ids)
        if meta is None:
            continue
        _accumulate(hit.chunk_id, 1.0 / (RRF_K + rank + 1), None, meta)

    # 向量路（sqlite-vec KNN，cosine 距离升序）
    if embedder is None:
        embedding: list[float] | None = None
    else:
        embedding = embedder([query])[0]
    for rank, (chunk_id, distance) in enumerate(
        _vector_search(conn, embedding, k) if embedding is not None else []
    ):
        meta = _meta_of(conn, int(chunk_id), None, None, tenant_id, kb_ids)
        if meta is None:
            # 这条块属于别的团队（或文档已删）。**不累加**：融合分只对
            # 本团队的候选有意义，给它一个分数会让它挤掉本团队的名额。
            continue
        _accumulate(int(chunk_id), 1.0 / (RRF_K + rank + 1), float(distance), meta)

    ordered = sorted(fused.items(), key=lambda item: cast(float, item[1]["rrf"]), reverse=True)
    hits: list[RetrievalHit] = []
    for chunk_id, e in ordered[:top_k]:
        hits.append(
            RetrievalHit(
                chunk_id=chunk_id,
                document_id=str(e["document_id"]),
                file_name=str(e["file_name"]),
                content=str(e["content"]),
                seq=cast(int, e["seq"]),
                page_from=cast(int, e["page_from"]),
                page_to=cast(int, e["page_to"]),
                heading=cast("str | None", e["heading"]),
                rrf_score=cast(float, e["rrf"]),
                vector_distance=cast("float | None", e["distance"]),
                kb_id=cast("str | None", e.get("kb_id")),
            )
        )
    return hits


def _capped_query(query: str) -> str:
    """超长问句截到 MAX_QUERY_TOKENS 个 token（C# MaxQueryTokens 同语义）。

    只截 FTS 路：向量路拿完整 query（语义在整句里）。bigram 下一个字
    产生一个 token，24 覆盖约 25 字——超过这个长度，多出的部分对
    词面召回已无增量，反而稀释 BM25。
    """
    return " ".join(tokenize(query)[:MAX_QUERY_TOKENS])


def _vector_search(
    conn: sqlite3.Connection, embedding: list[float], k: int
) -> list[tuple[int, float]]:
    """sqlite-vec KNN：cosine 距离升序。空库/无向量返回空，不报错。"""
    blob = sqlite_vec.serialize_float32(embedding)
    try:
        rows = conn.execute(
            "select ChunkId, distance from vec_chunks_vec where Embedding match ? and k = ?",
            (blob, k),
        ).fetchall()
    except sqlite3.OperationalError:
        # 空向量表时 vec0 可能报 invalid match——与"没命中"同义，不当异常抛
        return []
    return [(int(r["ChunkId"]), float(r["distance"])) for r in rows]


def _meta_of(
    conn: sqlite3.Connection,
    chunk_id: int,
    document_id: str | None,
    content: str | None,
    tenant_id: str,
    kb_ids: frozenset[str] | None = None,
) -> dict[str, object] | None:
    """补齐块元数据。全文路已带 content，向量路从 chunks 表取。

    **返回 `None` 表示这一块不属于本团队**（或文档行已被删）。调用方必须
    `continue`——返回一份空元数据的版本会让它挤进候选，而它带着空串文件名
    与空正文，最终表现为「引用里有一条没有出处的片段」。

    过滤写进 join 条件，不是取回来再判：后者要先把别人的正文读进内存，
    而「读过」这件事在缓存与日志里留得下痕迹。
    """

    def _scope(sql: str, params: list[object]) -> tuple[str, list[object]]:
        if kb_ids is None:
            return sql, params
        if not kb_ids:
            return sql + " and 1 = 0", params
        marks = ",".join("?" * len(kb_ids))
        return sql + f" and d.kb_id in ({marks})", [*params, *sorted(kb_ids)]

    if document_id is not None and content is not None:
        sql = (
            # chunks_vec 是 PascalCase 列名（C# 迁移遗留）：SQLite 不区分大小写
            # 但区分下划线，page_from ≠ PageFrom——写错不报 schema 错，报 no such column
            "select v.Seq, v.PageFrom, v.PageTo, v.Heading, d.file_name, d.kb_id"
            " from chunks_vec v"
            " join documents d on d.id = ? and d.tenant_id = ?"
            " where v.ChunkId = ?"
        )
        sql, params = _scope(sql, [document_id, tenant_id, chunk_id])
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return {
            "document_id": document_id,
            "file_name": (row["file_name"] if row["file_name"] else ""),
            "content": content,
            "seq": row["Seq"] if row else 0,
            "page_from": row["PageFrom"] if row else 1,
            "page_to": row["PageTo"] if row else 1,
            "heading": row["Heading"] if row else None,
            "kb_id": row["kb_id"],
        }
    sql = (
        "select v.Seq, v.PageFrom, v.PageTo, v.Heading, v.DocumentId, c.content,"
        " d.file_name, d.kb_id from chunks_vec v join chunks c on c.id = v.ChunkId"
        " join documents d on d.id = v.DocumentId and d.tenant_id = ?"
        " where v.ChunkId = ?"
    )
    sql, params = _scope(sql, [tenant_id, chunk_id])
    row = conn.execute(sql, params).fetchone()
    if row is None:
        # 两种可能：块属于别的团队（绝不返回），或者 vec 行在、正文块已删
        # （[c2] 类幽灵）。两者都不该进候选——前者是越权，后者是噪声。
        # 一致性检查会单独报幽灵，不需要检索替它遮掩。
        return None
    return {
        "document_id": row["DocumentId"],
        "file_name": row["file_name"] or "",
        "content": row["content"],
        "seq": row["Seq"],
        "page_from": row["PageFrom"],
        "page_to": row["PageTo"],
        "heading": row["Heading"],
        "kb_id": row["kb_id"],
    }
