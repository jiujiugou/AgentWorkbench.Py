"""判定、拒答分档、引用校验的词汇表不变量（F3-知识问答 §1.3 / §1.4 / §1.5）。

改坏判定值集：「失败」混进判定值之后，「系统没能力判」和「系统挂了」
在界面上长得一样，用户对着错误态等一个不会来的重试，而真正的
「资料不足」反而被当成故障上报。

改坏呈现规则：「资料不足」显示来源列表 → 用户对着五块不相关的候选
误以为「系统查过但说没有」，比拒答更伤信任；「未判定」长得和「已回答」
一样 → 判了等于没判，引用全对不上的答案混进正常答案里。

改坏分档：三档混成一档「负例」，误拒率的分母里混进「该承认」和
「资料其实在库里」的题——两个比率同时变成假的，而且没人看得出来。

改坏引用词汇：二级冒出第三个「已核实但不太确定」，或编号漂成 0 起算、
延续上一轮——编号指向本次证据之外，等于无担保的担保，且不报错。

服务于：G9（判定）、G14（拒答分档）、G7/G8（引用校验）
"""

from __future__ import annotations

from agentworkbench.core.judgment import (
    CITATION_SEQ_START,
    JUDGMENT_PRESENTATIONS,
    TIER_METRIC_BUCKETS,
    CitationFirstCheck,
    CitationSecondCheck,
    JudgmentPresentation,
    JudgmentValue,
    RefusalTier,
)
from agentworkbench.core.runs import RunStatus


def test_exactly_four_judgment_values() -> None:
    """判定恰好四值（§1.3）。多一个值 = 多一种界面态没人消费（§5.4 通则）。"""
    assert {v.value for v in JudgmentValue} == {
        "answered",
        "weak",
        "insufficient",
        "undetermined",
    }


def test_failure_is_a_run_state_not_a_judgment() -> None:
    """「失败」不是第五个判定值（§1.3 原话：系统失败不在其中）。"""
    assert "failed" not in {v.value for v in JudgmentValue}
    assert "failure" not in {v.value for v in JudgmentValue}
    # 运行态那边确实有 FAILED——两条路径分开，正是这条不变量守的边界。
    assert RunStatus.FAILED.value == "failed"


def test_every_value_has_a_presentation() -> None:
    """每个判定值都必须有呈现规则；漏一个 = 界面遇到它时没人管。"""
    assert set(JUDGMENT_PRESENTATIONS) == set(JudgmentValue)


def test_presentations_are_mutually_distinct() -> None:
    """四个值的呈现互不相同（§1.6 判定必须可见：五种在界面上长得不一样）。"""
    visible = [p.user_visible for p in JUDGMENT_PRESENTATIONS.values()]
    assert len(visible) == len(set(visible)), f"呈现语义重复：{sorted(visible)}"


def test_insufficient_terminates_and_hides_sources() -> None:
    """「资料不足」不放行生成，且不显示来源列表与句级摘要（§1.3 表格原话）。"""
    p = JUDGMENT_PRESENTATIONS[JudgmentValue.INSUFFICIENT]
    assert p.generates is False
    assert p.hides_sources is True
    assert p.requires_fixed_copy is True


def test_undetermined_discloses_and_generates() -> None:
    """「未判定」放行但必须说出来（§1.3：不能显示成「已回答」）。

    D-12 的函数层语义落在这里：阈值缺失不是错误、不是 0 分，
    是一个必须独立呈现的结论。
    """
    p = JUDGMENT_PRESENTATIONS[JudgmentValue.UNDETERMINED]
    assert p.generates is True
    assert p.must_disclose is True


def test_weak_carries_verify_hint_answered_does_not() -> None:
    """「依据较弱」必须带核对提示；「已回答」不带——两者靠它区分。"""
    assert JUDGMENT_PRESENTATIONS[JudgmentValue.WEAK].verify_hint is True
    assert JUDGMENT_PRESENTATIONS[JudgmentValue.ANSWERED].verify_hint is False


def test_refusal_tiers_exactly_three() -> None:
    """负例恰好三档（§1.5）。多一档少一档都会让分母错。"""
    assert {t.value for t in RefusalTier} == {
        "out_of_corpus",
        "same_topic_gap",
        "beyond_retrieval",
    }


def test_tier_metric_buckets_are_disjoint() -> None:
    """三档的指标归属互不重叠（§1.5：混档计比 = 两个数同时变假）。

    库外进拒答准确率；同主题缺答案只进承认率、绝不进误拒率；
    超出检索能力不进任何比率（D-11）。
    """
    assert TIER_METRIC_BUCKETS[RefusalTier.OUT_OF_CORPUS] == "refusal_accuracy"
    assert TIER_METRIC_BUCKETS[RefusalTier.SAME_TOPIC_GAP] == "acknowledgment"
    assert TIER_METRIC_BUCKETS[RefusalTier.SAME_TOPIC_GAP] != "refusal_accuracy"
    assert TIER_METRIC_BUCKETS[RefusalTier.BEYOND_RETRIEVAL] == "none"


def test_citation_first_check_is_closed_two_values() -> None:
    """一级校验封闭两值（§1.4）：保留或剥离，没有「部分保留」。"""
    assert {c.value for c in CitationFirstCheck} == {"kept", "stripped"}


def test_citation_second_check_has_no_maybe_verified() -> None:
    """二级校验恰好三值，不存在「已核实但不太确定」的评分档（§1.4 / §5.2）。

    UNSUPPORTED 是分类结论（判为不支持=误挂被抓），不是 0.87 那种
    可信度评分——引用可信度评分被 §5.2 明确不做，一个 0.87 只制造
    新的误读。C# CiteCheck 三值（Ok/OutOfRange/Unsupported）映射到
    一级/二级两个维度后，UNVERIFIED 对应「阈值未配置只记录不判定」（D-12）。
    """
    assert {c.value for c in CitationSecondCheck} == {"supported", "unsupported", "unverified"}


def test_citation_seq_starts_at_one() -> None:
    """编号从 1 起算（§1.6 编号空间封闭）。

    钉成常量而不是散落字面量：0 起算或延续上一轮都会让编号
    指向本次证据之外，而且不报错。
    """
    assert CITATION_SEQ_START == 1


def test_presentation_is_frozen_dataclass() -> None:
    """呈现规则不可变：运行时改「资料不足要显示来源」必须改文档+这里，不能顺手改。"""
    p = JUDGMENT_PRESENTATIONS[JudgmentValue.INSUFFICIENT]
    assert isinstance(p, JudgmentPresentation)
    try:
        p.hides_sources = False  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("JudgmentPresentation 可被运行时修改——呈现契约没锁住")
