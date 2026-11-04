"""不变量：每个错误码都有且只有一个传输约定，且可重试性与状态码自洽。

被改坏时用户会看到：调用方按文案判断错误、改一次文案全部失效；
或者界面提示「请重试」而错误其实是 4xx，重试一万次也没用（F6 §4.2）。
"""

from __future__ import annotations

import pytest

from agentworkbench.core.errors import (
    ALLOWED_HTTP_STATUSES,
    ERROR_SPECS,
    RETRYABLE_HTTP_STATUSES,
    AppError,
    ErrorCode,
)

pytestmark = pytest.mark.invariant


def test_every_code_has_spec() -> None:
    """加了枚举忘了登记 —— 这里红，而不是等到线上抛 KeyError。"""
    assert set(ERROR_SPECS) == set(ErrorCode)


@pytest.mark.parametrize("code", list(ErrorCode), ids=lambda c: c.value)
def test_http_status_is_registered(code: ErrorCode) -> None:
    assert ERROR_SPECS[code].http_status in ALLOWED_HTTP_STATUSES


@pytest.mark.parametrize("code", list(ErrorCode), ids=lambda c: c.value)
def test_retryable_matches_status(code: ErrorCode) -> None:
    """可重试的只许落在 500/503/504；4xx 一律不可重试。"""
    spec = ERROR_SPECS[code]
    if spec.retryable:
        assert spec.http_status in RETRYABLE_HTTP_STATUSES
    if spec.http_status < 500:
        assert not spec.retryable


def test_code_values_are_unique() -> None:
    """值重复会产生别名：两个码共用同一个字符串，调用方分不开。"""
    values = [c.value for c in ErrorCode]
    assert len(values) == len(set(values))


def test_not_found_is_404_not_403() -> None:
    """越权一律 404：403 会泄露「这个资源存在」。"""
    assert ERROR_SPECS[ErrorCode.NOT_FOUND].http_status == 404


def test_app_error_carries_code_and_spec() -> None:
    err = AppError(ErrorCode.MODEL_UNAVAILABLE, "模型不可用")
    assert err.code is ErrorCode.MODEL_UNAVAILABLE
    assert err.spec.http_status == 503
    assert err.spec.retryable
