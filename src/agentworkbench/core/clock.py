"""时间：全项目唯一的时间戳格式与计时源。

`utc_now` 用**秒级** ISO（无时区后缀），与 `documents.updated_at` 的字符串比较
一致——`scan_stalled` 靠它判「卡死」，改成微秒会改变已有数据的排序与比较语义。

**时长不用 `started_at` 相减。** 那是秒级精度：一次两秒内跑完的作答算出来是 0，
而 0 是「没有耗时」的意思（A-V10 反对的正是拿 0 假装有值）。单调时钟不受系统
时间调整影响，也便于测试注入。

来源：C# `Shared/Time/SystemClock.cs`（那边只有 `UtcNow`，没有单调时钟，
因为耗时全在数据库侧算）
服务于：REQ-12（耗时落库）、A-V10
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime


def utc_now() -> str:
    """落库用的时间戳。秒级、无时区后缀——与现有列格式保持一致。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def monotonic() -> float:
    """单调时钟。只用来算时长，不许落库（它的零点没有跨进程含义）。"""
    return time.monotonic()


def elapsed_ms(start: float, *, clock: Callable[[], float] = monotonic) -> int:
    """从 `start` 到现在多少毫秒。负数归零——时钟被调整时宁可报 0，不报负数。"""
    return max(0, int((clock() - start) * 1000))
