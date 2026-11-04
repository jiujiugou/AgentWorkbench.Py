"""解析失败的统一基类：码在这里定，不由调用方猜。

`PdfParseError` 当初自成一个类，Office 进来之后要么复制一份 `OfficeParseError`
（两条 except 并存，漏一个就是兜底的 `extract_error`），要么抽基类。抽基类的
代价是改一处 `worker.py` 的 except，换来的是"以后每加一种格式，worker 一行不动"。

来源：`ingest/parsing/pdf.py` 的 `PdfParseError`（Python 版原创，C# 版是自由文本）
服务于：REQ-06（失败归类）；判据 V-3 / V-16 / V-17。
"""

from __future__ import annotations

from agentworkbench.core.documents import DocumentErrorCode


class ParseError(Exception):
    """带 `DocumentErrorCode` 的抽取失败。

    调用方（worker）只负责落库，不负责判断"这是扫描件还是坏文件"——
    那个判断需要格式内部信息，只有解析器知道。
    """

    def __init__(self, code: DocumentErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
