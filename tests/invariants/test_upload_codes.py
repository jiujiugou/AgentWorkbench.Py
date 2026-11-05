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


def test_the_frontend_accepts_exactly_what_the_backend_takes() -> None:
    """前端的 `ACCEPT_EXTS` 必须与后端白名单**逐个相等**。

    D7 UI 验收抓到的真缺陷：前端写的是 `['.pdf', '.txt', '.md', '.json']`，后端收
    `{.pdf, .docx, .xlsx}`。两个方向都错——Word / Excel 在界面上**根本传不上去**
    （被前端挡下并提示"仅支持 PDF / TXT / MD / JSON"），而 TXT / MD / JSON 放行之后
    被后端 400 拒。用户看到的是"这个系统不支持 Word"，而代码里明明支持。

    这条测试**读前端源码**：跨语言的两份常量没有任何编译器能对上，只有一条
    会失败的断言能。改坏任一侧 → 红。
    """
    import re
    from pathlib import Path

    from agentworkbench.ingest.upload import DEFAULT_ALLOWED_SUFFIXES

    source = (
        Path(__file__).resolve().parents[2] / "web" / "src" / "api" / "documents.ts"
    ).read_text(encoding="utf-8")
    match = re.search(r"export const ACCEPT_EXTS = \[(.*?)\]", source, re.S)
    assert match is not None, "前端找不到 ACCEPT_EXTS——它被改名或挪走了，这条契约就断了"

    frontend = {item.strip().strip("'\"") for item in match.group(1).split(",") if item.strip()}
    assert frontend == set(DEFAULT_ALLOWED_SUFFIXES), (
        f"前后端接受的文件类型不一致：前端 {sorted(frontend)}，"
        f"后端 {sorted(DEFAULT_ALLOWED_SUFFIXES)}"
    )


# ── D9：上限要在**读之前**生效，而不是读完再判 ──


class _EndlessStream:
    """一个无限的字节流，记录被读走了多少。

    用它而不是真的 100MB 字节串：要验的是「读到什么时候就停」，不是「能不能读完」。
    """

    def __init__(self) -> None:
        self.read_bytes = 0

    def read(self, size: int) -> bytes:
        self.read_bytes += size
        return b"x" * size


def test_the_reader_stops_at_the_limit_instead_of_buffering_everything() -> None:
    """超限时最多读到「上限 + 一块」，而不是整份读进内存再判。

    改坏这里会怎样：退回 `data = file.file.read()` 的写法，一个 100MB 的请求
    会把服务端 RSS 抬起 107MB 才回 400——「被拒之前先把内存占了」。
    D9 红队 C 的 R-02 就是这条，`scripts/repro/r04_upload_resource.py` 可复跑。
    """
    from agentworkbench.api.errors import UploadRejected
    from agentworkbench.api.routes.documents import _READ_CHUNK, _read_with_limit

    stream = _EndlessStream()
    limit = 4 * 1024 * 1024

    with pytest.raises(UploadRejected) as caught:
        _read_with_limit(stream, limit)  # type: ignore[arg-type]

    assert caught.value.code is UploadRejectCode.FILE_TOO_LARGE
    assert stream.read_bytes <= limit + _READ_CHUNK, (
        f"读了 {stream.read_bytes} 字节才停——上限是 {limit}"
    )


def test_the_reader_keeps_content_under_the_limit_intact() -> None:
    """没超限时逐字节原样返回——有上限的读不能把内容改掉。

    `file.file` 是二进制流，分块拼接写错一个 `extend`/`append` 就会把 bytes 变成
    "bytes 的列表"，而那要到解析阶段才炸，且错误信息完全不指向这里。
    """
    from io import BytesIO

    from agentworkbench.api.routes.documents import _read_with_limit

    payload = "液压站 E2 报警".encode() * 1000
    assert _read_with_limit(BytesIO(payload), len(payload)) == payload
