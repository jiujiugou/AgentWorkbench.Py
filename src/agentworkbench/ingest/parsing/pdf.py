"""PDF 抽取与失败归类。

用 `pypdf` 抽，不自己解析 PDF：格式里的对象流、交叉引用表、加密字典没有一件
是本项目该踩的坑，而页码锚点（引用的落点）只要按页迭代就有。

失败必须落到具体的码上，不能一句 `extract_error` 打发——`no_text_layer` 与
`corrupt` 的可重试性相反，混成一个码之后"这个月失败里多少是扫描件"就统计不出来，
而那是要不要做 OCR 的唯一依据（F2 §5.2）。

来源：C# `Documents/Parsing`（错误是自由文本，Python 版改成枚举 + 异常）
服务于：REQ-05 / REQ-06（F2）；判据由 `tests/invariants/test_parse_failure.py`、
`tests/invariants/test_pdf_parser.py` 守着。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from agentworkbench.core.documents import DocumentErrorCode
from agentworkbench.ingest.parsing.errors import ParseError


class ParseFailureCode(StrEnum):
    """解析失败的原因。只许增，不许改语义。"""

    NO_TEXT_LAYER = "no_text_layer"


class PdfParseError(ParseError):
    """PDF 抽取失败。**码在这里定，不由调用方猜。**

    原来自成一个类、自带 `__init__`；Office 进来后抽了 `ParseError` 基类，
    worker 的 except 收敛成一个。类名保留——它是 PDF 侧的身份，测试按它定位。
    """


def has_text_layer(pages: Sequence[str]) -> bool:
    """至少有一页有非空白文本，才算抽出内容。

    只用 `isspace` 不算：PDF 抽出来的"空页"常常是一堆换行与全角空格。
    """
    return any(page.strip() for page in pages)


def classify_parse_failure(pages: Sequence[str]) -> ParseFailureCode | None:
    """`None` 表示解析成功；否则给出原因码。

    第一版只有一种可命名的失败。别的异常（加密、损坏）由 `parse_pdf` 直接抛
    `PdfParseError`——没有样本的枚举只会变成一个新的垃圾桶。
    """
    if not has_text_layer(pages):
        return ParseFailureCode.NO_TEXT_LAYER
    return None


def parse_pdf(data: bytes) -> list[str]:
    """按页抽文本，返回页序列表（0-based 下标对应第 1 页）。

    加密与损坏在这里就分开：`encrypted` 的处置是"让用户解密重传"，
    `corrupt` 是"让用户重新导出"——两句话不一样，用户动作也不一样。
    """
    try:
        reader = PdfReader(BytesIO(data))
    except PdfReadError as exc:
        raise PdfParseError(DocumentErrorCode.CORRUPT, str(exc)) from exc

    if reader.is_encrypted:
        raise PdfParseError(DocumentErrorCode.ENCRYPTED, "PDF 有密码保护")

    pages: list[str] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            pages.append(page.extract_text() or "")
        except PdfReadError as exc:
            raise PdfParseError(
                DocumentErrorCode.EXTRACT_ERROR, f"第 {index} 页抽取失败：{exc}"
            ) from exc
    return pages
