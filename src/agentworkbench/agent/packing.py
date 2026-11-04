"""装填：把检索结果变成模型的证据——编号、算账、判定同行（§1.7 装填格，①→③）。

纯数据变换，不碰模型不碰库。三件事必须出自同一份检索结果（D-03）：
编号命中列表、字符数、判定结论。分成两次调用就一定会有「判定针对 A 批、
模型拿的是 B 批」的窗口，而这种错不会报错（C# Payload remarks 原话）。

编号在这里固定（D-05）：no = 列表下标 + 1，就是模型在答案里写的 [n]，
也是引用校验的编号空间。服务端事后按同一顺序还原，不需要额外存储。
编号空间封闭的物理来源就在这一行——候选取自本次证据，每次从 1 起算。

字符预算：拿不到精确 token 数时，字符数是唯一自己能算、也能被别人复算的量。
超预算**告警而不截断**——截断不会报错，用户只看到答案少了一半，日志里
什么都查不到。budget=None 表示还没定标，永不告警；写了 0 就是 0（D-12）。

来源：C# `Knowledge.Local/Tools/SearchKnowledgeTool.cs`（编号 / ContextCharsOf /
ExceedsContextBudget / Payload）
服务于：REQ-07 / REQ-08（可用五、G4）、D-03 / D-05
"""

from __future__ import annotations

from collections.abc import Sequence

from agentworkbench.core.judgment import Evidence, Judgment, NumberedHit
from agentworkbench.rag.retriever import RetrievalHit


def number_hits(hits: Sequence[RetrievalHit]) -> tuple[NumberedHit, ...]:
    """检索结果 → 编号命中。no 从 1 起（core.judgment.CITATION_SEQ_START），
    顺序 = 检索顺序——两处任何一处变了，引用校验都校得出错。"""
    return tuple(
        NumberedHit(
            no=index + 1,
            chunk_id=hit.chunk_id,
            document_id=hit.document_id,
            file_name=hit.file_name,
            content=hit.content,
            seq=hit.seq,
            page_from=hit.page_from,
            page_to=hit.page_to,
            heading=hit.heading,
            vector_distance=hit.vector_distance,
        )
        for index, hit in enumerate(hits)
    )


def context_chars_of(hits: Sequence[NumberedHit] | Sequence[RetrievalHit]) -> int:
    """本轮送进模型的资料字符数。

    它的消费者是「内容被截断」那条判据（G4），不是界面——
    单独抽出来是为了能直接单测（C# 同款）。
    """
    return sum(len(h.content) for h in hits)


def exceeds_context_budget(context_chars: int, budget: int | None) -> bool:
    """是否超出资料预算。None = 还没定标，永不告警；写了 0 就是 0（D-12）。"""
    return budget is not None and context_chars > budget


def pack(
    hits: Sequence[RetrievalHit], judgment: Judgment, *, char_budget: int | None = None
) -> Evidence:
    """装填一次检索的结果。三段同源由签名强制：进一份、出一包。

    预算超限**不在这里截断**——证据原样全量传递，超限事实作为
    `exceeds_budget` 带出去由调用方告警（日志/轨迹），静默截断是本函数
    禁止的行为（G4 负控：构造超预算输入必须告警而不是悄悄少给）。
    """
    numbered = number_hits(hits)
    return Evidence(
        hits=numbered,
        context_chars=context_chars_of(numbered),
        judgment=judgment,
    )


def budget_state(ev: Evidence, char_budget: int | None) -> bool:
    """装填后的预算判定。独立于 pack：预算是部署配置，装填是数据变换。"""
    return exceeds_context_budget(ev.context_chars, char_budget)
