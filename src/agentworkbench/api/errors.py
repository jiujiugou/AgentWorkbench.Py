"""错误到 HTTP 的唯一翻译点。

内部只有两种异常形态：
- `AppError`（`core/errors.py`）：带稳定错误码，按 `ERROR_SPECS` 取状态码。
- `UploadRejected`：上传四码是**返回值**不是异常（`judge_upload` 的契约），
  但在 HTTP 边界表达为异常最省样板——这里做一次包装。

错误体是**五字段信封**（S-V4）：`code` 给程序判断、`http_status` 与状态行同值
（只看 body 的调用方也拿得到）、`retryable` 给调用方决定要不要重试、
`message` 是给人看的那句（前端 parseError 只认它）、`request_id` 可拿去日志
定位同一次请求。**谁要用哪个字段自己挑——只回 `{message}` 会把"能不能重试"
和"出了事找谁"都丢掉。**

服务于：REQ-20（错误反馈明确稳定）
"""

from __future__ import annotations

from agentworkbench.core.errors import AppError
from agentworkbench.core.errors import ErrorCode as CoreCode
from agentworkbench.ingest.upload import UploadRejectCode

# 上传四码的对外文案。与 `DOC_ERROR_SPECS` 同一风格：说清楚用户下一步该做什么。
UPLOAD_MESSAGES: dict[UploadRejectCode, str] = {
    UploadRejectCode.NO_FILE: "请求里没有文件字段。",
    UploadRejectCode.FILE_EMPTY: "文件是空的。",
    UploadRejectCode.FILE_TYPE_UNSUPPORTED: "只支持 PDF / Word（.docx）/ Excel（.xlsx）文件。",
    UploadRejectCode.FILE_TOO_LARGE: "文件超过大小上限。",
}


class UploadRejected(Exception):
    """上传拒收。码只能是四个上传码之一。"""

    def __init__(self, code: UploadRejectCode) -> None:
        super().__init__(UPLOAD_MESSAGES[code])
        self.code = code
        self.message = UPLOAD_MESSAGES[code]


def status_of(error: AppError | UploadRejected) -> int:
    if isinstance(error, UploadRejected):
        return 400
    return error.spec.http_status


def body_of(error: AppError | UploadRejected, request_id: str = "") -> dict[str, str | bool | int]:
    """五字段信封。`http_status` 进 body 是刻意的：curl -i 以外的调用方
    （几乎所有 HTTP 库的默认用法）拿不到状态行的原始值，body 里再给一份，
    两处必须一致——这正是 `test_response_header_matches_the_body_request_id`
    同款的自洽要求。"""
    if isinstance(error, UploadRejected):
        return {
            "code": str(error.code),
            "http_status": 400,
            "message": error.message,
            "retryable": False,
            "request_id": request_id,
        }
    return {
        "code": str(error.code),
        "http_status": error.spec.http_status,
        "message": error.message,
        "retryable": error.spec.retryable,
        "request_id": request_id,
    }


def not_found(message: str) -> AppError:
    return AppError(CoreCode.NOT_FOUND, message)


def validation_failed(message: str) -> AppError:
    return AppError(CoreCode.VALIDATION_FAILED, message)
