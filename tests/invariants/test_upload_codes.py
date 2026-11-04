"""不变量：四个拒收码的判定顺序。

被改坏时用户会看到：传一个空文件却被告知「格式不支持」，去改扩展名还是失败；
或者传一个超大 PDF 被告知「文件为空」。**顺序本身就是契约**（F2 §3 第 2 项）。
"""

from __future__ import annotations

import pytest

from agentworkbench.ingest.upload import UploadRejectCode, judge_upload

pytestmark = pytest.mark.invariant

MAX = 1000


def test_no_file() -> None:
    assert judge_upload(filename=None, size_bytes=0, max_bytes=MAX) is UploadRejectCode.NO_FILE
    assert judge_upload(filename="  ", size_bytes=10, max_bytes=MAX) is UploadRejectCode.NO_FILE


def test_empty_beats_bad_type_and_size() -> None:
    """空文件同时扩展名不对、还超限，也必须报 FILE_EMPTY。"""
    assert (
        judge_upload(filename="a.docx", size_bytes=0, max_bytes=MAX) is UploadRejectCode.FILE_EMPTY
    )
    assert judge_upload(filename="a.docx", size_bytes=0, max_bytes=0) is UploadRejectCode.FILE_EMPTY


def test_type_beats_size() -> None:
    """格式先于大小：一个超限的不支持格式该报格式，不是「太大」。

    例子曾经用 `.docx`——那时白名单只有 `.pdf`，它是现成的反例。
    Office 三类进来之后，`.docx` 成了合法格式，超限的 docx 正确地报
    「太大」（先收下再谈大小）。反例换成 `.xls`。
    """
    assert (
        judge_upload(filename="a.xls", size_bytes=MAX + 1, max_bytes=MAX)
        is UploadRejectCode.FILE_TYPE_UNSUPPORTED
    )


def test_too_large() -> None:
    assert (
        judge_upload(filename="a.pdf", size_bytes=MAX + 1, max_bytes=MAX)
        is UploadRejectCode.FILE_TOO_LARGE
    )


def test_boundary_is_inclusive() -> None:
    """正好等于上限要收。"""
    assert judge_upload(filename="a.pdf", size_bytes=MAX, max_bytes=MAX) is None


def test_ok() -> None:
    assert judge_upload(filename="a.pdf", size_bytes=1, max_bytes=MAX) is None


def test_suffix_case_insensitive() -> None:
    assert judge_upload(filename="A.PDF", size_bytes=1, max_bytes=MAX) is None


def test_no_file_beats_everything() -> None:
    """四种问题叠在一起，只能报最基础的那个。"""
    assert judge_upload(filename=None, size_bytes=0, max_bytes=0) is UploadRejectCode.NO_FILE
