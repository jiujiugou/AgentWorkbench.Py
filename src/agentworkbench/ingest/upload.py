"""上传入口的判定：四个对外码，以及它们的**判定顺序**。

顺序本身就是契约（F2 §3 第 2 项）：空文件同时扩展名也不对时，用户该看到
`FILE_EMPTY`——先解决更基础的问题。这条靠测试守，不靠注释。

来源：C# `Domain/Documents/DocumentErrorCodes.cs`
服务于：REQ-05（F2）；判据由 `tests/invariants/test_upload_codes.py` 守着。
"""

from __future__ import annotations

from enum import StrEnum

# 三类是判据（V-13）钉的：SOP 在 Word、BOM 与检验记录在 Excel，只收 PDF 等于把
# 最想问的资料挡在门外。`.doc` / `.xls` 是二进制复合格式，两条抽取库都不读；
# `.xlsm` 敞开着能收（openpyxl 忽略宏），但"收了却静默丢宏"比拒收更坏。
DEFAULT_ALLOWED_SUFFIXES: frozenset[str] = frozenset({".pdf", ".docx", ".xlsx"})


class UploadRejectCode(StrEnum):
    """四个对外码。值即响应里的 `code`，只许增、不许改语义。"""

    NO_FILE = "NO_FILE"
    FILE_EMPTY = "FILE_EMPTY"
    FILE_TYPE_UNSUPPORTED = "FILE_TYPE_UNSUPPORTED"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"


def judge_upload(
    *,
    filename: str | None,
    size_bytes: int,
    max_bytes: int,
    allowed_suffixes: frozenset[str] = DEFAULT_ALLOWED_SUFFIXES,
) -> UploadRejectCode | None:
    """返回拒收原因；`None` 表示可以收。

    顺序固定，改它等于改契约：无文件 → 空 → 格式 → 大小。
    """
    if filename is None or not filename.strip():
        return UploadRejectCode.NO_FILE
    if size_bytes <= 0:
        return UploadRejectCode.FILE_EMPTY
    if not _suffix_allowed(filename, allowed_suffixes):
        return UploadRejectCode.FILE_TYPE_UNSUPPORTED
    if size_bytes > max_bytes:
        return UploadRejectCode.FILE_TOO_LARGE
    return None


def _suffix_allowed(filename: str, allowed: frozenset[str]) -> bool:
    lowered = filename.strip().lower()
    return any(lowered.endswith(suffix) for suffix in allowed)
