"""不变量：「没有文本层」必须被单独命名，不能混进通用失败。

被改坏时用户看到的是「不支持」，而不是「这是扫描件、需要 OCR」——
而后者是做不做 OCR 的唯一依据（F2 §5.2）。
"""

from __future__ import annotations

import pytest

from agentworkbench.ingest.parsing.pdf import (
    ParseFailureCode,
    classify_parse_failure,
    has_text_layer,
)

pytestmark = pytest.mark.invariant


def test_all_pages_blank() -> None:
    assert classify_parse_failure(["", "   ", "\n\n"]) is ParseFailureCode.NO_TEXT_LAYER


def test_whitespace_only_is_blank() -> None:
    """PDF 的「空页」常是一堆换行与全角空格，不能用 `== ""` 判。"""
    assert not has_text_layer(["\n\n\n", "\u3000\u3000", "\t "])


def test_one_page_with_text_passes() -> None:
    assert classify_parse_failure(["", "有字", ""]) is None


def test_empty_sequence_is_failure() -> None:
    assert classify_parse_failure([]) is ParseFailureCode.NO_TEXT_LAYER


def test_single_char_counts_as_text() -> None:
    """一个字符也算有文本层：宁可让它进切块，也不要因为长度误判成扫描件。"""
    assert has_text_layer(["图"])
