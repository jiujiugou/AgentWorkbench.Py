"""混合召回的不变量（F3 G3 / §1.7 召回格 / D-01）。

改坏融合公式：RRF 分数泄漏给判定 → 「第一名恒为 1/61」的分数被当成
相关度，库里完全没有答案的问题第一名照样高分，判定层拿到的是垃圾输入——
D-01 禁用融合分正是这个原因，融合分只能排序，不能判定。

改坏「证据只取一次」：同一问题两次检索结果不同 → 来源面板显示这批、
答案引用那批，而且不报错（§1.6 结构约束）。

改坏距离保留：向量路命中的 distance 丢了 → 判定层把混合结果
当成全文路（全 None）→ 未判定，检索明明有结果却「没能力判」。

改坏可复算：融合分里掺了时间戳或随机数 → 同一个问题两次检索结果不同，
用户刷新一次来源面板换一批块，引用全部失效。

RRF_K=60 与 MAX_QUERY_TOKENS=24 在这里硬编码文档定案值，不从被测模块
import——否则改常量时期望值跟着漂，公式守门失效（改坏验证一真踩过）。
"""

from __future__ import annotations

import itertools
import sqlite3
from collections.abc import Iterator

import pytest
from conftest import insert_document

from agentworkbench.core.documents import ChunkDraft
from agentworkbench.core.judgment import JudgmentValue, SufficiencyOptions
from agentworkbench.db import engine
from agentworkbench.guard.sufficiency import judge
from agentworkbench.llm.embeddings import HashEmbedder
from agentworkbench.rag.indexer import write_index
from agentworkbench.rag.retriever import retrieve

TENANT = "default"
"""这些测试的数据租户。检索必填它，夹具就把数据造在这里。"""


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """空内存库 + 全套 schema：检索测试不需要 10MB fixture 库。"""
    target = sqlite3.connect(":memory:")
    target.row_factory = sqlite3.Row
    engine.load_vec(target)  # 先加载扩展再建 vec0 虚表（迁移 0001 同序）
    engine.create_schema(target)
    try:
        yield target
    finally:
        target.close()


@pytest.fixture()
def indexed(conn: sqlite3.Connection) -> sqlite3.Connection:
    """一个最小可检索库：三块不同内容，向量用 HashEmbedder。"""
    embedder = HashEmbedder()
    drafts = [
        ChunkDraft(
            seq=0, page_from=1, page_to=1, content="H5U 的 E2 报警是通讯超时，检查波特率与站号。"
        ),
        ChunkDraft(
            seq=1, page_from=2, page_to=2, content="FC01 读线圈功能码用于读取输出继电器的状态。"
        ),
        ChunkDraft(
            seq=2,
            page_from=3,
            page_to=3,
            content="维护保养记录表：每季度清洁散热风道并紧固接线端子。",
        ),
    ]
    insert_document(conn, "doc-1", tenant_id=TENANT)
    write_index(
        conn,
        document_id="doc-1",
        drafts=drafts,
        embeddings=embedder([d.content for d in drafts]),
        now="2026-11-02T00:00:00",
    )
    conn.commit()
    return conn


def test_rrf_formula_single_route(indexed: sqlite3.Connection) -> None:
    """RRF 分数 = Σ 1/(k + rank + 1)（D-01：只表达排名，不表达相关度）。

    原文即问题时两路第一名恰好同块：FTS 1/61 + 向量 1/61 = 2/61——
    双路累加正是融合的定义，这条断言同时钉住公式与累加。
    """
    hits = retrieve(indexed, "FC01 读线圈", embedder=HashEmbedder(), tenant_id=TENANT, top_k=5)
    assert hits, "词面完全匹配的块必须命中"
    # 硬编码 60（文档定案值）：不从被测模块 import RRF_K，
    # 否则改常量时期望跟着漂，公式守门失效（改坏验证一真踩过）
    assert hits[0].rrf_score == pytest.approx(2.0 / 61)


def test_two_routes_add_scores(indexed: sqlite3.Connection) -> None:
    """两路都命中的块分数相加 → 排最前（RRF 融合的定义）。"""
    hits = retrieve(
        indexed, "H5U 的 E2 报警是通讯超时", embedder=HashEmbedder(), tenant_id=TENANT, top_k=5
    )
    # 原文即问题：FTS 必中第一名；向量路用同一文本查询也必中
    assert hits[0].rrf_score > 1.0 / 61, "双路命中应当累加超过单路最高分"


def test_vector_distance_kept_text_route_none(indexed: sqlite3.Connection) -> None:
    """向量路命中带距离，全文路命中 distance 为 None——判定的输入不能丢。"""
    hits = retrieve(
        indexed, "H5U 的 E2 报警是通讯超时", embedder=HashEmbedder(), tenant_id=TENANT, top_k=5
    )
    # doc-1 的三块：命中里向量路来的带距离
    assert any(h.vector_distance is not None for h in hits), "向量路命中必须有距离"
    # 判定可算：距离列表喂 judge，不得把 None 当 0
    distances = [h.vector_distance for h in hits]
    r = judge(distances, SufficiencyOptions(refuse_margin=0.05, sufficient_distance=0.9))
    assert r.verdict in {JudgmentValue.ANSWERED, JudgmentValue.WEAK, JudgmentValue.INSUFFICIENT}


def test_fusion_score_never_used_for_judgment(indexed: sqlite3.Connection) -> None:
    """融合分只用于排序。判定的输入是向量距离，不是 rrf_score（D-01）。"""
    hits = retrieve(
        indexed, "H5U 的 E2 报警是通讯超时", embedder=HashEmbedder(), tenant_id=TENANT, top_k=5
    )
    assert all(0 < h.rrf_score <= 2.0 / 61 for h in hits)
    # 融合分与距离是两个字段，判定层读不到融合分
    assert all(h.vector_distance is None or 0.0 <= h.vector_distance <= 2.0 for h in hits)


def test_top_k_truncation(indexed: sqlite3.Connection) -> None:
    """top_k 截断：只要 2 块就返回 2 块，且按融合分降序。"""
    hits = retrieve(indexed, "H5U E2 FC01 维护", embedder=HashEmbedder(), tenant_id=TENANT, top_k=2)
    assert len(hits) == 2
    assert all(a.rrf_score >= b.rrf_score for a, b in itertools.pairwise(hits))


def test_empty_query_returns_empty(indexed: sqlite3.Connection) -> None:
    """空问题不检索（C# 同语义：IsNullOrWhiteSpace → []）。"""
    assert retrieve(indexed, "   ", embedder=HashEmbedder(), tenant_id=TENANT, top_k=5) == []
    assert retrieve(indexed, "", embedder=HashEmbedder(), tenant_id=TENANT, top_k=5) == []


def test_vector_route_always_returns_nearest(indexed: sqlite3.Connection) -> None:
    """向量路 KNN 永远返回最近 k 条——库里没有答案也照样凑（G3 负控 7/8 非空的原因）。

    这不是缺陷是结构：sqlite-vec 没有命中阈值。所以「检索为空」不能当
    拒答信号（§1.5），拒答只能靠判定层——本测试把这条实测依据钉进代码。
    """
    hits = retrieve(indexed, "辞职信怎么写", embedder=HashEmbedder(), tenant_id=TENANT, top_k=5)
    # FTS 一路 0 命中（词面完全无交集），向量一路仍返回最近邻
    assert hits, "KNN 语义：永远返回最近邻，空结果是判定层的事不是检索层的事"


def test_same_query_same_results(indexed: sqlite3.Connection) -> None:
    """可复算（§1.6 证据只取一次的前提）：同库同问两次，结果逐条相同。"""
    a = retrieve(indexed, "H5U 的 E2 报警", embedder=HashEmbedder(), tenant_id=TENANT, top_k=5)
    b = retrieve(indexed, "H5U 的 E2 报警", embedder=HashEmbedder(), tenant_id=TENANT, top_k=5)
    assert [(h.chunk_id, h.rrf_score, h.vector_distance) for h in a] == [
        (h.chunk_id, h.rrf_score, h.vector_distance) for h in b
    ]


def test_deleted_document_not_retrievable(indexed: sqlite3.Connection) -> None:
    """删除后 0 命中（V-8 联动）：索引清干净就不许再回来。

    直接走 clear_index + 删行（delete_document 的四步补偿中检索相关的两步），
    存储层不在本测试范围。
    """
    from agentworkbench.rag.indexer import clear_index

    clear_index(indexed, "doc-1")
    indexed.execute("delete from chunks where document_id = ?", ("doc-1",))
    indexed.execute("delete from documents where id = ?", ("doc-1",))
    indexed.commit()
    assert (
        retrieve(indexed, "FC01 读线圈", embedder=HashEmbedder(), tenant_id=TENANT, top_k=5) == []
    )


def test_long_query_capped_at_max_tokens() -> None:
    """超长问句截到 24 个 bigram（C# 同款：多出的部分对召回无增量）。"""
    from agentworkbench.rag.retriever import _capped_query

    long_q = "液压站异常报警导致停机" * 5  # 50 字，bigram 远超 24
    capped = _capped_query(long_q)
    assert capped.count(" ") == 23, f"应恰好 24 个 token：{len(capped.split())}"


def test_tenant_parameter_is_required(indexed: sqlite3.Connection) -> None:
    """团队范围是**必填参数、没有默认值**（`I-V5` 的负控在签名上）。

    阶段 1 这里是 `scope: str | None = None`，注释写着「F1 落地后在此过滤」——
    而那一行 `_ = scope` 正是「签名对了但没人传」的形态：参数在，值被丢掉，
    测试全绿，检索照样跨团队。现在它没有默认值，漏传是 `TypeError`。

    断言 `default is empty`，不是只断言参数名存在：后者对
    `tenant_id: str | None = None` 也成立，而那正是要被禁止的写法。
    """
    import inspect

    sig = inspect.signature(retrieve)
    assert "tenant_id" in sig.parameters
    assert sig.parameters["tenant_id"].default is inspect.Parameter.empty


def test_hit_carries_the_file_name_of_its_document(indexed: sqlite3.Connection) -> None:
    """出处要能写出文件名，不是一串 uuid。

    界面要显示「《汇川H5U用户手册》第 12 页」。让界面拿 document_id 再查一次
    文件名，等于把证据拆成两份——两份之间文档被删或改名，界面显示的出处
    就和模型当时看到的那份对不上了（§1.6 证据只取一次）。
    """
    # 文档行由 `indexed` 夹具建好（文件名 `sample.pdf`）——夹具必须建它，
    # 因为团队条件走 `documents` 表：只有索引、没有文档行的库一条都检索不到。
    hits = retrieve(indexed, "FC01 读线圈", embedder=HashEmbedder(), tenant_id=TENANT, top_k=5)

    assert hits
    assert hits[0].file_name == "sample.pdf"
    # 两条路（词面 / 向量）都要带上文件名，不能只有一条带
    assert all(hit.file_name == "sample.pdf" for hit in hits)


def test_index_without_its_document_row_is_not_retrievable(
    indexed: sqlite3.Connection,
) -> None:
    """索引残留、文档行没了 → **这条命中不该出现**（AUTH-06 在检索侧的推论）。

    这是换血时**行为反转**的一条，值得写清楚为什么改：

    阶段 1 用的是 `left join documents`，理由是「孤儿块（文档被删、索引还在）
    不该让命中消失，宁可少一个文件名」。那时没有归属概念，所以这个取舍成立。

    F1 之后不成立了：团队条件走 `documents` 表，文档行不存在就意味着
    **这块内容没有归属**，而「没有归属的行对所有人 404」（`I-V5`）在检索侧
    的推论就是「它也查不到」。保留它的实现会给出一条没有出处的片段——
    在别的团队那里的表现正是一段读不到来源的资料。
    """
    indexed.execute("delete from documents where id = 'doc-1'")

    hits = retrieve(indexed, "FC01 读线圈", embedder=HashEmbedder(), tenant_id=TENANT, top_k=5)

    assert hits == []
