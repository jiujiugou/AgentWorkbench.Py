"""装填的不变量（F3 G4 / §1.7 装填格 / D-03 / D-05）。

改坏编号：编号漂成 0 起算或与检索顺序脱钩 → 模型写的 [n] 指向另一块，
引用校验用同一份编号却校不出错——无担保的担保且不报错。

改坏预算告警：超预算静默截断 → 用户只看到答案少了一半，日志里什么都
查不到（C# 原话：不记这个数，截断不会报错）；把「未定标」当 0 →
预算没配却天天告警，或者配了 0 却从不告警——两种都让告警失去意义。

改坏同源性：判定与命中分成两次调用 → 「判定针对 A 批、模型拿的是 B 批」，
这种错不报错（C# Payload remarks 原话）。
"""

from __future__ import annotations

import dataclasses

import pytest

from agentworkbench.agent.packing import (
    context_chars_of,
    exceeds_context_budget,
    pack,
)
from agentworkbench.core.judgment import (
    Judgment,
    JudgmentValue,
    NumberedHit,
    SufficiencyOptions,
)
from agentworkbench.guard.sufficiency import judge
from agentworkbench.rag.retriever import RetrievalHit


def _hit(seq: int, content: str, distance: float | None = None) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=100 + seq,
        document_id=f"doc-{seq}",
        file_name=f"手册{seq}.pdf",
        content=content,
        seq=seq,
        page_from=1,
        page_to=1,
        heading=None,
        rrf_score=0.01,
        vector_distance=distance,
    )


def _hits() -> list[RetrievalHit]:
    return [
        _hit(0, "H5U 的 E2 报警是通讯超时。", 0.31),
        _hit(1, "FC01 读线圈功能码用于读取输出继电器状态。", 0.58),
        _hit(2, "维护保养：每季度清洁散热风道。", None),
    ]


def test_numbering_starts_at_one_and_follows_retrieval_order() -> None:
    """编号从 1 起且等于检索顺序（CITATION_SEQ_START；D-05：编号即本次证据下标）。"""
    numbered = pack(_hits(), _judge(_hits())).hits
    assert [h.no for h in numbered] == [1, 2, 3]
    assert numbered[0].content == "H5U 的 E2 报警是通讯超时。"


def test_context_chars_is_sum_of_content_lengths() -> None:
    """字符预算的唯一可复算量 = Σ content 长度（C# ContextCharsOf 同语义）。"""
    hits = _hits()
    expected = sum(len(h.content) for h in hits)
    assert context_chars_of(hits) == expected == pack(hits, _judge(hits)).context_chars


def test_exceeds_budget_strictly_greater() -> None:
    """超过才告警：恰好等于预算不告警（C# `>` 同语义）。"""
    assert exceeds_context_budget(101, 100) is True
    assert exceeds_context_budget(100, 100) is False


def test_unset_budget_never_warns() -> None:
    """null 预算 = 还没定标，永不告警（C# 原话）；不是 0，0 是真预算。"""
    assert exceeds_context_budget(99999, None) is False
    assert exceeds_context_budget(0, 0) is False
    assert exceeds_context_budget(1, 0) is True


def test_evidence_three_sections_from_one_retrieval() -> None:
    """命中、字符数、判定必须同源（D-03；C# Payload remarks：分两次必出乱配窗口）。"""
    hits = _hits()
    judgment = _judge(hits)
    ev = pack(hits, judgment)
    assert ev.judgment is judgment
    assert [h.chunk_id for h in ev.hits] == [h.chunk_id for h in hits]
    assert ev.context_chars == context_chars_of(hits)


def test_judgment_is_carried_not_recomputed() -> None:
    """判定进装填时不重算：guard 算出的结论原样携带（否决层只读它，D-03）。"""
    hits = _hits()
    judgment = judge([h.vector_distance for h in hits], SufficiencyOptions(0.05, 0.20))
    ev = pack(hits, judgment)
    assert ev.judgment.verdict is judgment.verdict
    assert ev.judgment.margin == judgment.margin
    assert ev.judgment.reason is judgment.reason


def test_empty_retrieval_packs_empty_evidence() -> None:
    """空命中也照常装填：判定此时是 INSUFFICIENT，证据列表为空——结构一致。"""
    judgment = judge([], SufficiencyOptions(0.10, 0.50))
    ev = pack([], judgment)
    assert ev.hits == ()
    assert ev.context_chars == 0
    assert ev.judgment.verdict is JudgmentValue.INSUFFICIENT


def test_evidence_is_frozen() -> None:
    """装填结果不可变：模型输入、引用校验读到的必须是同一份（证据只取一次）。"""
    ev = pack(_hits(), _judge(_hits()))
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.context_chars = 0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.hits[0].no = 99  # type: ignore[misc]


def _judge(hits: list[RetrievalHit]) -> Judgment:
    return judge(
        [h.vector_distance for h in hits],
        SufficiencyOptions(refuse_margin=0.05, sufficient_distance=0.20),
    )


def test_numbered_hit_fields_carry_citation_facts() -> None:
    """引用卡片四字段（文件/序号/页码/原文）在装填时就在证据里（G12 的数据来源）。"""
    h: NumberedHit = pack(_hits(), _judge(_hits())).hits[0]
    assert h.document_id and h.content and h.page_from >= 1 and h.seq == 0
