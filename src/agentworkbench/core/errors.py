"""异常与错误码：本项目的错误词汇表。

业务分支不用异常（判定四值是返回值），异常只用于「本不该发生」。
每个异常必须带稳定错误码，调用方按码判断，不按文案判断。

来源：C# `Shared/Execution/AgentRunCode.cs`
服务于：REQ-20（错误反馈明确稳定）

设计约束（对应 F6 §4.2，被 tests/invariants/ 守着）：
- 码是枚举，**只许增、不许改语义**；文案随便改，码改了调用方全挂。
- 一个码只对应一个 HTTP 状态；反过来不要求唯一（多个码可以同为 503）。
- `retryable` 必须与状态码自洽：可重试的只出现在 500/503/504。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# 响应只允许出现这些状态码。新增一个码要同时想清「调用方该不该重试」。
ALLOWED_HTTP_STATUSES: frozenset[int] = frozenset({400, 401, 403, 404, 409, 422, 500, 503, 504})

# 允许被标为可重试的状态码：只有「过一会儿再试可能就好了」这类。
RETRYABLE_HTTP_STATUSES: frozenset[int] = frozenset({500, 503, 504})


class ErrorCode(StrEnum):
    """对外稳定的错误码。枚举值即响应里的 `code` 字段。"""

    INVALID_CREDENTIALS = "invalid_credentials"
    NOT_AUTHENTICATED = "not_authenticated"
    # 403 只用于「按动作调的接口」：角色不够（F1 §1.6 第 4 步），
    # 或首次登录还没改初始口令（I-V19）。**按 id 取资源一律 404**——那是
    # 「这个 id 存不存在」的侧信道，而动作本身是公开的，403 不泄露任何资源。
    FORBIDDEN = "forbidden"
    PASSWORD_CHANGE_REQUIRED = "password_change_required"  # noqa: S105 —— 错误码，不是口令
    NOT_FOUND = "not_found"
    VALIDATION_FAILED = "validation_failed"
    MODEL_NOT_CONFIGURED = "model_not_configured"
    MODEL_UNAVAILABLE = "model_unavailable"
    MODEL_TIMEOUT = "model_timeout"
    RUN_TIMEOUT = "run_timeout"
    INDEX_STALE = "index_stale"
    AUDIT_UNAVAILABLE = "audit_unavailable"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """一个错误码的传输层约定。用户文案不在这里——文案可改，码不可改。"""

    http_status: int
    retryable: bool


# 每个码都必须在这里登记。tests/invariants/test_error_codes.py 断言两边集合相等，
# 所以「加了枚举忘了登记」会当场红，而不是等到线上返回 KeyError。
ERROR_SPECS: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.INVALID_CREDENTIALS: ErrorSpec(http_status=401, retryable=False),
    ErrorCode.NOT_AUTHENTICATED: ErrorSpec(http_status=401, retryable=False),
    ErrorCode.FORBIDDEN: ErrorSpec(http_status=403, retryable=False),
    ErrorCode.PASSWORD_CHANGE_REQUIRED: ErrorSpec(http_status=403, retryable=False),
    ErrorCode.NOT_FOUND: ErrorSpec(http_status=404, retryable=False),
    ErrorCode.VALIDATION_FAILED: ErrorSpec(http_status=400, retryable=False),
    ErrorCode.MODEL_NOT_CONFIGURED: ErrorSpec(http_status=503, retryable=False),
    ErrorCode.MODEL_UNAVAILABLE: ErrorSpec(http_status=503, retryable=True),
    ErrorCode.MODEL_TIMEOUT: ErrorSpec(http_status=504, retryable=True),
    ErrorCode.RUN_TIMEOUT: ErrorSpec(http_status=504, retryable=False),
    ErrorCode.INDEX_STALE: ErrorSpec(http_status=503, retryable=False),
    # 记录写不进去 → 这次作答不算成功（F4 §三硬规则）。不可重试：
    # 库写不进去通常是磁盘满 / 库被锁 / 权限，重试一次多半还是同一结果，
    # 而 `retryable=True` 会让前端自动重试，把一次故障变成一串。
    ErrorCode.AUDIT_UNAVAILABLE: ErrorSpec(http_status=503, retryable=False),
    ErrorCode.INTERNAL_ERROR: ErrorSpec(http_status=500, retryable=True),
}


class AppError(Exception):
    """带稳定错误码的异常。

    只用于「本不该发生」：业务分支（判定不足、拒答）走返回值，不抛异常。

    `reason` 是**内部诊断标签，不进响应体**（F1 §1.3）：`NOT_AUTHENTICATED` 一条
    错误码下面有六种原因（没带票 / 票据过期 / 已吊销 / 账户停用 / 换了密码 / 无此票），
    响应必须一模一样，而拒绝记录要分得清。让异常自己带着这个标签，是**为了避免
    「记录的人拿不到原因」**——把它做成本函数的第二个参数、或让调用点各自补一遍，
    都会在某条分支上漏掉，而漏掉的那条在记录里表现为一个错误的默认值。
    """

    def __init__(self, code: ErrorCode, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        if code not in ERROR_SPECS:
            raise LookupError(f"错误码未登记：{code}")
        self.code = code
        self.spec = ERROR_SPECS[code]
        self.message = message
        self.reason = reason

    def __str__(self) -> str:
        return f"[{self.code.value}] {self.message}"
