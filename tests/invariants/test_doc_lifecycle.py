"""文档状态机与失败码：这条不变量被改坏时，用户会看到什么。

改坏状态机：一份"删除中"的文档重新出现在检索结果里，或者一份失败文档永远卡在
"处理中"——两种都不抛异常，只在用户问"为什么搜不到 / 为什么删不掉"时暴露。

改坏失败码：失败只剩一句自由文本，统计不出"这个月失败里多少是扫描件"，
于是要不要做 OCR 这件事永远决定不了。
"""

from __future__ import annotations

import pytest

from agentworkbench.core.documents import (
    ALLOWED_TRANSITIONS,
    DOC_ERROR_SPECS,
    TERMINAL_STATUSES,
    DocStatus,
    DocumentErrorCode,
    require_transition,
)


def test_every_status_has_a_transition_entry() -> None:
    """每个状态都必须在迁移表里有条目；漏一个就等于"能不能离开"没人管。"""
    assert set(ALLOWED_TRANSITIONS) == set(DocStatus)


def test_only_failed_may_go_back_to_pending() -> None:
    """倒退只允许"重试"这一种：FAILED → PENDING。其余倒退会让终态失去意义。"""
    for src, targets in ALLOWED_TRANSITIONS.items():
        if src is DocStatus.FAILED:
            continue
        assert DocStatus.PENDING not in targets, f"{src} 不许回到 pending"


def test_delete_pending_absorbs() -> None:
    """删除中之后不许回到任何"可用"状态——否则撤销被静默吞掉。"""
    assert ALLOWED_TRANSITIONS[DocStatus.DELETE_PENDING] == frozenset()


def test_no_status_is_a_dead_end_unless_terminal() -> None:
    """除终态外每个状态都要有出口，否则会永久卡住，而界面看起来像"正在处理"。"""
    for status in DocStatus:
        if status in TERMINAL_STATUSES:
            continue
        assert ALLOWED_TRANSITIONS[status], f"{status} 是死路，但它不是终态"


def test_self_transition_is_illegal() -> None:
    """X → X 不入表：同一状态反复写入会让"状态变更时间"失去意义（V-7 靠它算超时）。"""
    for src, targets in ALLOWED_TRANSITIONS.items():
        assert src not in targets


def test_terminal_statuses_match_table() -> None:
    """终态就是出边为空的那几个，不许另立一张表说另一套。"""
    derived = {status for status, targets in ALLOWED_TRANSITIONS.items() if not targets}
    assert derived == set(TERMINAL_STATUSES)


@pytest.mark.parametrize(
    "src,dst",
    [
        (DocStatus.PENDING, DocStatus.PARSED),
        (DocStatus.PENDING, DocStatus.PENDING),
        (DocStatus.PARSED, DocStatus.PARSING),
        (DocStatus.DELETE_PENDING, DocStatus.PARSED),
    ],
)
def test_illegal_transition_raises(src: DocStatus, dst: DocStatus) -> None:
    with pytest.raises(ValueError):
        require_transition(src, dst)


@pytest.mark.parametrize(
    "src,dst",
    [
        (DocStatus.PENDING, DocStatus.PARSING),
        (DocStatus.PARSING, DocStatus.PARSED),
        (DocStatus.PARSED, DocStatus.FAILED),
        (DocStatus.FAILED, DocStatus.PENDING),
        (DocStatus.PARSED, DocStatus.DELETE_PENDING),
    ],
)
def test_legal_transition_passes(src: DocStatus, dst: DocStatus) -> None:
    require_transition(src, dst)


@pytest.mark.parametrize("code", list(DocumentErrorCode))
def test_every_error_code_has_spec(code: DocumentErrorCode) -> None:
    """每个对外码都要有规格；漏登记的码在界面上会退化成一句英文枚举名。"""
    assert code in DOC_ERROR_SPECS


def test_no_orphan_spec() -> None:
    assert set(DOC_ERROR_SPECS) == set(DocumentErrorCode)


@pytest.mark.parametrize("code", list(DocumentErrorCode))
def test_spec_hint_is_readable(code: DocumentErrorCode) -> None:
    spec = DOC_ERROR_SPECS[code]
    assert spec.user_hint.strip(), f"{code} 没有给人看的文案"
    assert len(spec.user_hint) <= 60, f"{code} 文案超过 60 字，界面上放不下"


def test_retryable_codes_have_a_way_back() -> None:
    """标了可重试，就必须真的存在 FAILED → PENDING 这条边，否则这个标记是假的。"""
    for code, spec in DOC_ERROR_SPECS.items():
        if spec.retryable:
            assert DocStatus.PENDING in ALLOWED_TRANSITIONS[DocStatus.FAILED], code


def test_permanent_codes_are_not_retryable() -> None:
    """这几类重试不会变好：同样一份没文本层的 PDF，再解析一次还是没文本层。"""
    for code in (
        DocumentErrorCode.NO_TEXT_LAYER,
        DocumentErrorCode.ENCRYPTED,
        DocumentErrorCode.CORRUPT,
        DocumentErrorCode.CHUNK_EMPTY,
    ):
        assert not DOC_ERROR_SPECS[code].retryable, code
