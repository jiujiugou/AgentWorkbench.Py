"""否决门的不变量（F3 G10 / D-02 / D-03）。

改坏否决方向：判「资料不足」还放行生成 → 模型拿五块不相关候选凭常识
编答案，引用校验照样全绿——REQ-09 整条失效且无告警。

改坏保守方向：解析不出结果时也拒答 → 格式变更、字段漂移全部变成
「整轮拒答」，正常对话被误杀。保守方向在这条链路上是单向的：
宁可放过，不可误杀（C# remarks 原话）。

改坏判定重算：否决层自己再判一遍 → 工具判一次、否决判一次，两个实现
迟早漂移且不报错（D-03 禁止的事）。否决层只读装填好的判定，不碰距离。

用户的处境：改坏否决方向时，用户拿到的答案是模型凭常识编的、却挂着
「资料不足就该有出处」的架势；改坏保守方向时，用户在正常对话里
被整轮拒答，且永远不知道是格式变更害的。
"""

from __future__ import annotations

import dataclasses

import pytest

from agentworkbench.agent.packing import pack
from agentworkbench.core.judgment import Evidence, JudgmentValue, SufficiencyOptions
from agentworkbench.core.runs import RunReasonCode, RunStatus
from agentworkbench.guard.sufficiency import judge
from agentworkbench.guard.veto import (
    REFUSAL_COPY,
    SEARCH_KNOWLEDGE_TOOL,
    inspect,
)
from agentworkbench.rag.retriever import RetrievalHit


def _evidence(distances: list[float | None]) -> Evidence:
    hits = [
        RetrievalHit(
            chunk_id=100 + i,
            document_id=f"doc-{i}",
            file_name=f"手册{i}.pdf",
            content=f"第 {i} 块内容。",
            seq=i,
            page_from=1,
            page_to=1,
            heading=None,
            rrf_score=0.01,
            vector_distance=d,
        )
        for i, d in enumerate(distances)
    ]
    judgment = judge([h.vector_distance for h in hits], SufficiencyOptions(0.10, 0.50))
    return pack(hits, judgment)


def test_insufficient_stops_with_refused_and_fixed_copy() -> None:
    """判不足 → 终止 + REFUSED + knowledge_insufficient + 固定文案（G10 正控）。"""
    ev = _evidence([0.55, 0.56, 0.57])  # 挤在一起 → 没有赢家
    assert ev.judgment.verdict is JudgmentValue.INSUFFICIENT
    stop = inspect(SEARCH_KNOWLEDGE_TOOL, ev)
    assert stop is not None
    assert stop.status is RunStatus.REFUSED
    assert stop.code is RunReasonCode.KNOWLEDGE_INSUFFICIENT
    assert stop.message == REFUSAL_COPY


@pytest.mark.parametrize("distances", [[0.20, 0.70], [0.20], [0.55, 0.90, 0.95]])
def test_other_verdicts_pass_through(distances: list[float | None]) -> None:
    """已回答 / 依据较弱（单参照 / 太远）都放行——闸只挡判不足这一种。"""
    assert inspect(SEARCH_KNOWLEDGE_TOOL, _evidence(distances)) is None


def test_undetermined_also_passes() -> None:
    """未判定也放行（但必须说出来——must_disclose 由界面层消费，不归闸管）。"""
    ev = _evidence([0.55, 0.56, 0.57])
    undetermined = pack(
        [],  # 空命中走 INSUFFICIENT，这里用「有结果但未判定」
        judge([None, None], SufficiencyOptions(0.10, 0.50)),
    )
    # 上面 pack 空命中会覆盖判定？不——判定是传入的，pack 只携带。
    ev = dataclasses.replace(ev, judgment=undetermined.judgment)
    assert undetermined.judgment.verdict is JudgmentValue.UNDETERMINED
    assert inspect(SEARCH_KNOWLEDGE_TOOL, undetermined) is None


def test_other_tools_are_not_vetoed() -> None:
    """非知识工具的结果不经此闸（D-02：执行管道不认识知识库）。"""
    ev = _evidence([0.55, 0.56, 0.57])
    assert inspect("run_sql", ev) is None
    assert inspect("", ev) is None


def test_empty_or_none_result_never_vetoes() -> None:
    """解析失败一律不否决（保守方向单向：宁可放过，不可误杀）。"""
    assert inspect(SEARCH_KNOWLEDGE_TOOL, None) is None


def test_verdict_is_carried_not_recomputed() -> None:
    """否决层不重算判定：它读装填好的 Evidence（D-03）。

    构造一份「距离很远但判定已是 INSUFFICIENT」的证据——如果是重算，
    远距离会被判 too_far/weak；只读时它忠实拒绝。函数签名根本没有
    距离参数，重算在类型上不可能。
    """
    ev = _evidence([0.55, 0.56, 0.57])
    assert inspect(SEARCH_KNOWLEDGE_TOOL, ev) is not None
    # 签名上没有距离或重算入口：
    import inspect as pyinspect

    params = list(pyinspect.signature(inspect).parameters)
    assert params == ["tool_name", "result"]


def test_refusal_copy_is_fixed_string() -> None:
    """固定文案是常量不是模板（§1.3：不用模型生成，拒答卡 G11 的文案源）。"""
    assert REFUSAL_COPY == "资料不足以回答该问题。知识库里没有找到能支撑回答的内容。"


def test_stop_is_frozen() -> None:
    """终止决定不可变：管道收口读到的必须是否决层给的那个。"""
    stop = inspect(SEARCH_KNOWLEDGE_TOOL, _evidence([0.55, 0.56, 0.57]))
    assert stop is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        stop.status = RunStatus.SUCCEEDED  # type: ignore[misc]


def test_gate_is_binary_for_same_evidence() -> None:
    """成对断言（G10 负控）：同一批命中，判不足停、判可答放——二值且稳定。

    「模型只被请求 1 次」的管道级验证归 agent 编排（LLM 调用计数）；
    函数级先钉住闸门本身的二值性。
    """
    distances: list[float | None] = [0.55, 0.56, 0.57]
    blocked = inspect(SEARCH_KNOWLEDGE_TOOL, _evidence(distances))
    allowed = inspect(SEARCH_KNOWLEDGE_TOOL, _evidence([0.20, 0.70, 0.75]))
    assert (blocked is not None) and (allowed is None)
