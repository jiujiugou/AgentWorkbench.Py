"""充分性判定的算术不变量（F3 G9 / D-01 / D-12）。

改坏比较方向或边界：等于拒答阈值时判不足 → 边界题被整批误拒，且两边都能
自证合理，谁也说服不了谁；「挤在一起但恰好都很近」被判成已回答 →
用户拿到一个没有赢家的答案还挂着角标，引用校验全绿，没有任何告警。

改坏阈值缺失语义：用 0 或 False 冒充「未配置」→ 「没判定」和「判定通过」
长得完全一样，D-12 要防的正是这个；只配一个阈值就开始下结论 →
没有赢家的形状被误判成已回答（文档 D-01 代价原话）。

改坏距离收集：NaN/inf 当 0 参与平均 → 间隔被算小，误判不足；
按总条数除 → 间隔被算大，误判已回答——两种错都长得像正常结果。
"""

from __future__ import annotations

import math

from agentworkbench.core.judgment import (
    Judgment,
    JudgmentReason,
    JudgmentValue,
    SufficiencyOptions,
)
from agentworkbench.guard.sufficiency import MARGIN_WINDOW, judge

COMPLETE = SufficiencyOptions(refuse_margin=0.10, sufficient_distance=0.50)
UNSET = SufficiencyOptions(refuse_margin=None, sufficient_distance=None)


def test_no_candidates_is_insufficient_not_undetermined() -> None:
    """检索为空不是「没找到」，是「资料不足」——由判定段说（§1.7 总表）。

    与「有命中但全没距离」必须分开：前者说明库里没有，后者说明
    语义路没参与，结论一个是拒、一个是「没能力判」。
    """
    r = judge([], COMPLETE)
    assert r.verdict is JudgmentValue.INSUFFICIENT
    assert r.reason is JudgmentReason.NO_CANDIDATES
    assert r.top_distance is None and r.margin is None


def test_hits_without_distances_is_undetermined() -> None:
    """候选全来自全文路（None 距离）→ 未判定 + no_vector（C# 同语义）。"""
    r = judge([None, None, None], COMPLETE)
    assert r.verdict is JudgmentValue.UNDETERMINED
    assert r.reason is JudgmentReason.NO_VECTOR


def test_non_finite_distances_are_ignored_not_zero() -> None:
    """NaN/inf 视同「这条没有距离」：当 0 会把间隔算小（误判不足）。"""
    r = judge([float("nan"), float("inf"), 0.30, 0.70], COMPLETE)
    # 有效距离只剩 0.30 / 0.70：间隔 = 0.70 − 0.30 = 0.40 ≥ 0.10，且 0.30 < 0.50
    assert r.verdict is JudgmentValue.ANSWERED
    assert r.top_distance == 0.30
    assert r.margin is not None and math.isclose(r.margin, 0.40)


def test_single_hit_is_weak_with_null_margin() -> None:
    """只有一条参照算不出间隔 → 依据较弱 + single_hit（§1.3 表格原话）。"""
    r = judge([0.20], COMPLETE)
    assert r.verdict is JudgmentValue.WEAK
    assert r.reason is JudgmentReason.SINGLE_HIT
    assert r.margin is None


def test_margin_is_mean_of_rest_minus_top() -> None:
    """间隔 = 第二~第五名平均距离 − 第一名（D-01 公式），窗口取 5。"""
    # 六条：第六名 0.99 不得参与——它进不了上下文，不该影响判定
    r = judge([0.20, 0.60, 0.70, 0.80, 0.90, 0.99], COMPLETE)
    assert MARGIN_WINDOW == 5
    expected = (0.60 + 0.70 + 0.80 + 0.90) / 4 - 0.20
    assert r.margin is not None and math.isclose(r.margin, expected)


def test_input_order_is_not_trusted() -> None:
    """顺序不可信，内部自己排；第一名取集合最小距离，不是列表第一条。"""
    ordered = judge([0.20, 0.70], COMPLETE)
    shuffled = judge([0.70, 0.20], COMPLETE)
    assert ordered.margin == shuffled.margin
    assert ordered.top_distance == shuffled.top_distance == 0.20


def test_margin_below_threshold_is_insufficient() -> None:
    """间隔低于拒答阈值：前几名挤在一起，没有赢家 → 资料不足 + no_winner。"""
    r = judge([0.55, 0.56, 0.57, 0.58], COMPLETE)  # margin = 0.02 < 0.10
    assert r.verdict is JudgmentValue.INSUFFICIENT
    assert r.reason is JudgmentReason.NO_WINNER


def test_equal_to_refuse_margin_does_not_refuse() -> None:
    """严格小于才拒：等于阈值不拒（C# 备注：改成 <= 边界行为漂移）。"""
    # 构造 margin 恰好等于 0.10：top=0.30，其余平均 0.40
    r = judge([0.30, 0.40, 0.40, 0.40], COMPLETE)
    assert r.margin is not None and math.isclose(r.margin, 0.10)
    assert r.verdict is not JudgmentValue.INSUFFICIENT


def test_top_must_be_closer_than_sufficient_distance() -> None:
    """有赢家但第一名不够近 → 依据较弱 + too_far（§1.3：够近但没有赢家 vs 没有赢家）。"""
    r = judge([0.60, 0.95, 0.96], COMPLETE)  # margin = 0.355 ≥ 0.10，但 0.60 > 0.50
    assert r.verdict is JudgmentValue.WEAK
    assert r.reason is JudgmentReason.TOO_FAR


def test_equal_to_sufficient_distance_is_not_answered() -> None:
    """等于充分距离不算够近（严格小于）：边界上宁可弱依据不冒充已回答。"""
    r = judge([0.50, 0.95, 0.96], COMPLETE)  # top 恰好 = 0.50
    assert r.verdict is JudgmentValue.WEAK


def test_partial_thresholds_never_answer() -> None:
    """缺充分距离时绝不判「已回答/依据较弱」（D-01 代价原话防的就是这个）。

    拒答不需要两个阈值：只配拒答阈值时，「挤在一起」照样能判没有赢家——
    判"没赢家"不需要知道"够不够近"。但「已回答」必须两阈值齐：
    只配充分距离时，「挤在一起但恰好都很近」会被误判成已回答，
    而那正是没有赢家的形状——这条负控专防它。
    """
    crowded_close = [0.10, 0.12, 0.14]  # 挤在一起且都很近
    spread = [0.20, 0.60, 0.70]  # 分得开
    only_distance = SufficiencyOptions(refuse_margin=None, sufficient_distance=0.50)
    only_margin = SufficiencyOptions(refuse_margin=0.10, sufficient_distance=None)

    # 只配充分距离 + 挤在一起很近：不能判已回答 → 未判定（D-01 原话的情形）
    r = judge(crowded_close, only_distance)
    assert r.verdict is JudgmentValue.UNDETERMINED
    assert r.reason is JudgmentReason.THRESHOLD_UNSET

    # 只配拒答阈值：拒答照常工作（单阈值够用）
    assert judge(crowded_close, only_margin).verdict is JudgmentValue.INSUFFICIENT
    # 但分得开的候选：无法判"够不够近" → 依然未判定
    r = judge(spread, only_margin)
    assert r.verdict is JudgmentValue.UNDETERMINED
    assert r.reason is JudgmentReason.THRESHOLD_UNSET

    # 两个都没配：什么都不判
    assert judge(crowded_close, UNSET).verdict is JudgmentValue.UNDETERMINED


def test_zero_is_a_real_threshold_not_unconfigured() -> None:
    """写了 0 就是 0，系统不替人猜「这是没配吧」（D-12 后半句）。

    margin 恒 ≥ 0（排序后第一名恒为最小），所以 refuse_margin=0.0 的
    可观测效果是「永不因间隔拒答，但参与判定」——与 None（落未判定）
    形成对照：前者能给出结论，后者诚实说没能力。
    """
    clustered = [0.30, 0.30, 0.30]  # margin 恰为 0：0 < 0 不成立，不拒
    zero = SufficiencyOptions(refuse_margin=0.0, sufficient_distance=0.50)
    unset = SufficiencyOptions(refuse_margin=None, sufficient_distance=0.50)

    assert zero.is_complete is True
    r = judge(clustered, zero)
    assert r.verdict is JudgmentValue.ANSWERED  # 参与判定，给出结论

    r = judge(clustered, unset)
    assert r.verdict is JudgmentValue.UNDETERMINED  # 没配 → 诚实说没能力


def test_answered_carries_no_reason_weak_and_blocked_do() -> None:
    """原因码只在 weak / insufficient / undetermined 上有值；已回答为 None（C# 契约）。"""
    ok = judge([0.20, 0.70, 0.80], COMPLETE)
    assert ok.verdict is JudgmentValue.ANSWERED and ok.reason is None
    assert judge([0.55, 0.56], COMPLETE).reason is not None
    assert judge([0.20], COMPLETE).reason is not None
    assert judge([], COMPLETE).reason is not None


def test_judgment_is_frozen() -> None:
    """判定结论不可变（D-03：判定只算一次，写进工具输出，否决层只读不重算）。"""
    r: Judgment = judge([0.20, 0.70], COMPLETE)
    try:
        r.verdict = JudgmentValue.INSUFFICIENT  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("Judgment 可被运行时改写——否决层读到的就不是算出来的那个")
