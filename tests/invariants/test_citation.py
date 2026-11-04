"""引用两级校验的不变量（F3 G7 / G8 / D-04 / D-05）。

改坏一级：越界编号原样透传 → 用户点开角标，指向一个不存在的东西——
无担保的担保。整段连好引用一起删 → 「越界的是那个编号，不是这句话
的出处」（[2,9] 删 9 后必须剩 [2]）。

改坏归属：标号在句号之后算进下一句 → 句级统计整整错一位，
每个「无出处句」的数字都反着来。

改坏二级复算性：覆盖率用与索引不同的分词 → 「检索判定相关」和
「引用判定对得上」互相矛盾；残留 [n] 参与统计 → ASCII 词 n 拉低
覆盖率，好引用被判成指向错块。

改坏阈值语义：未配置当 0 → 「没判定」和「判定不达标」长得完全一样，
所有引用全变 UNSUPPORTED（D-12 禁止的事）。
"""

from __future__ import annotations

import dataclasses

import pytest

from agentworkbench.core.judgment import (
    CitationFirstCheck,
    CitationSecondCheck,
    NumberedHit,
    SupportVerdict,
)
from agentworkbench.guard.citation import (
    CitationReport,
    keep_citations,
    parse,
    strip_citations,
    verify,
)


def _hit(no: int, content: str) -> NumberedHit:
    return NumberedHit(
        no=no,
        chunk_id=no,
        document_id=f"doc-{no}",
        file_name=f"手册{no}.pdf",
        content=content,
        seq=no,
        page_from=1,
        page_to=1,
        heading=None,
        vector_distance=0.3,
    )


def _evidence() -> list[NumberedHit]:
    return [
        _hit(1, "H5U 的 E2 报警是通讯超时，检查波特率与站号设置。"),
        _hit(2, "FC01 读线圈功能码用于读取输出继电器的状态。"),
        _hit(3, "维护保养：每季度清洁散热风道并紧固接线端子。"),
    ]


def test_sentences_split_on_zh_ends_not_dots() -> None:
    """切句只认中文句末标点与换行；英文句点不切（192.168.1.88 不许切碎）。"""
    parsed = parse("地址是 192.168.1.88。[1]版本 V4.2！重启即可。")
    assert len(parsed.sentences) == 3
    assert "192.168.1.88" in parsed.sentences[0]
    assert "V4.2" in parsed.sentences[1]
    # [1] 紧跟第一句句号之后 → 归前一句（归属规则不受句数影响）
    assert parsed.raw[0][0] == 0


def test_consecutive_punctuation_is_one_boundary() -> None:
    """连续标点（？！）并入同一句的结尾，不产生空句。"""
    parsed = parse("真的吗？！确实如此。")
    assert len(parsed.sentences) == 2


def test_mark_after_sentence_end_belongs_to_previous() -> None:
    """紧跟句末标点的标注归前一句（LocateSentence 的存在理由）。

    C# 原例：按位置归属会把「……没有插好。[1] 请重新插拔。」算成
    第二句有出处、第一句是结论没依据——整整错一位。
    """
    ev = _evidence()
    report = verify("请检查网线是否松动。[1]然后重启设备。", ev)
    first = report.citations[0]
    assert first.sentence_index == 0  # 归「请检查网线是否松动。」不是下一句


def test_strip_citations_removes_all_marks() -> None:
    assert strip_citations("通讯超时[1]，检查[2,3]波特率。") == "通讯超时，检查波特率。"


def test_keep_citations_filters_per_number() -> None:
    """[2,9] 里只有 9 越界 → 剩 [2]：越界的是编号，不是这句话的出处。"""
    assert keep_citations("内容[2,9]。", lambda no: no <= 3) == "内容[2]。"
    assert keep_citations("内容[9]。", lambda no: no <= 3) == "内容。"


def test_in_range_kept_out_of_range_stripped() -> None:
    """一级：范围内 KEPT，越界 STRIPPED 且 second/coverage 为 None。"""
    report = verify("通讯超时[1]。线圈读取[2]。保修政策[9]。", _evidence())
    checks = [(c.evidence_no, c.first) for c in report.citations]
    assert checks == [
        (1, CitationFirstCheck.KEPT),
        (2, CitationFirstCheck.KEPT),
        (9, CitationFirstCheck.STRIPPED),
    ]
    oor = report.citations[2]
    assert oor.second is None and oor.coverage is None
    assert oor.document_id == "" and oor.chunk_id == 0


def test_cleaned_answer_drops_out_of_range_only() -> None:
    """越界编号指向不存在的东西，留着就是假担保；好标注原样保留。"""
    report = verify("通讯超时[1]。保修政策[9]。", _evidence())
    assert report.cleaned_answer == "通讯超时[1]。保修政策。"
    assert report.out_of_range_count == 1


def test_second_check_unverified_when_threshold_unset() -> None:
    """阈值未配置 → 只记录覆盖率数值，判 UNVERIFIED 不标黄（D-12）。"""
    report = verify("通讯超时[1]。", _evidence(), coverage_threshold=None)
    c = report.citations[0]
    assert c.second is CitationSecondCheck.UNVERIFIED
    assert c.coverage is not None and 0.0 <= c.coverage <= 1.0
    assert report.unsupported_count == 0


def test_second_check_supported_and_unsupported() -> None:
    """覆盖率达标 SUPPORTED、低于阈值 UNSUPPORTED（误挂被抓 = 可核三的观测点）。"""
    report = verify("E2 报警是通讯超时，检查波特率与站号。[1]", _evidence(), 0.30)
    assert report.citations[0].second is CitationSecondCheck.SUPPORTED
    # 内容完全来自另一块：误挂
    report = verify("每季度清洁散热风道并紧固接线端子。[1]", _evidence(), 0.60)
    assert report.citations[0].second is CitationSecondCheck.UNSUPPORTED
    assert report.unsupported_count == 1


def test_coverage_is_reproducible() -> None:
    """同一份输入永远同一个数（D-04：可复算，不看模型脸色）。"""
    a = verify("E2 报警是通讯超时。[1]", _evidence(), 0.30).citations[0].coverage
    b = verify("E2 报警是通讯超时。[1]", _evidence(), 0.30).citations[0].coverage
    assert a == b


def test_uncited_sentences_counted() -> None:
    """无出处的句子被数出来（可核一：零角标抓取的统计基础）。"""
    report = verify("这句有依据[1]。这句没有任何标注。这也没有。", _evidence())
    assert report.sentence_count == 3
    assert report.cited_sentence_count == 1
    assert report.uncited_sentence_count == 2


def test_empty_answer_gives_empty_report() -> None:
    report = verify("", _evidence())
    assert report.sentence_count == 0 and report.citations == ()
    assert verify(None, None).sentence_count == 0


def test_no_evidence_everything_stripped() -> None:
    """证据池为空：所有编号都越界（编号空间封闭：没有本次证据就没有合法编号）。"""
    report = verify("通讯超时[1]。", [], 0.30)
    assert report.out_of_range_count == 1
    assert report.cleaned_answer == "通讯超时。"


def test_report_is_frozen() -> None:
    """报告不可变：它只给界面不喂下一步，但也不许被中途改写。"""
    report = verify("通讯超时[1]。", _evidence())
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.out_of_range_count = 0  # type: ignore[misc]


def test_zero_threshold_is_a_real_threshold() -> None:
    """coverage ≥ 0 恒真：0 阈值下一切范围内引用都 SUPPORTED（写了 0 就是 0）。"""
    report = verify("完全无关的内容[1]。", _evidence(), 0.0)
    assert report.citations[0].second is CitationSecondCheck.SUPPORTED


# ── 整段结论：support_verdict / uncited_ratio（F3 §四 run 18 的形态）──


def test_verdict_is_unsupported_when_no_sentence_has_a_citation() -> None:
    """一句出处都没有 → unsupported。

    这就是 F3 run 18：模型一次没检索，凭常识列了六条排查建议，
    `cited_sentence_count=0`。它比空答案更危险——读起来很像手册里的内容，
    所以这条结论必须在界面上变成一句明确的警告，而不是留白。
    """
    report = verify("先检查信号干扰。再检查接线。最后看参数设置。", _evidence())

    assert report.cited_sentence_count == 0
    assert report.support_verdict is SupportVerdict.UNSUPPORTED
    assert report.uncited_ratio == 1.0


def test_verdict_is_supported_when_every_citation_checks_out() -> None:
    """正控：真挂了角标、内容也对得上 → supported。没有这一条，"永远说没出处"也能过。"""
    report = verify("维护保养是每季度清洁散热风道并紧固接线端子[3]。", _evidence(), 0.3)

    assert report.cited_sentence_count == 1
    assert report.support_verdict is SupportVerdict.SUPPORTED


def test_one_misattached_citation_makes_the_whole_answer_unsupported() -> None:
    """一条挂错块 → 整段不可引用。

    读者看到角标就会打开原文，打开发现没有那句话——一条错挂足以让整段
    可信度归零，所以不做"大部分对就算对"。
    """
    # [1] 指向 H5U 报警那块，而这句话讲的是维护保养：号码对、内容对不上
    report = verify("维护保养是每季度清洁散热风道并紧固接线端子[1]。", _evidence(), 0.9)

    assert report.unsupported_count == 1
    assert report.support_verdict is SupportVerdict.UNSUPPORTED


def test_verdict_is_undetermined_when_the_threshold_is_missing() -> None:
    """阈值没配 → undetermined，**不许当已核实**（D-12）。

    这是整套判据里最不能容忍的一步：把"没能力判"显示成"已核实"。
    """
    report = verify("维护保养是每季度清洁散热风道并紧固接线端子[3]。", _evidence(), None)

    assert report.support_verdict is SupportVerdict.UNDETERMINED


def test_empty_answer_is_undetermined_not_unsupported() -> None:
    """空答案没有内容可判 → undetermined。与"有内容但没出处"是两件事。"""
    report = CitationReport.empty("")

    assert report.support_verdict is SupportVerdict.UNDETERMINED
    assert report.uncited_ratio == 0.0


def test_uncited_ratio_is_computed_on_the_server() -> None:
    """无出处句占比由服务端算：界面自己除，0/0 在 JS 是 NaN、在 Python 会抛。"""
    report = verify("维护保养是每季度清洁散热风道并紧固接线端子[3]。再检查信号干扰。", _evidence())

    assert report.sentence_count == 2
    assert report.uncited_sentence_count == 1
    assert report.uncited_ratio == 0.5
