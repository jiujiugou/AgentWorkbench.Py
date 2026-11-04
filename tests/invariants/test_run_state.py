"""不变量：状态机只允许从 running 出发的迁移，终态是吸收态。

被改坏时用户会看到：一次运行同时是「已成功」和「已取消」，或者同一次运行
有两条终态。记录自相矛盾，**却会被当成证据**——比没有记录更糟（F4 §1.5）。
"""

from __future__ import annotations

import pytest

from agentworkbench.core.runs import (
    ALLOWED_TRANSITIONS,
    REQUIRES_REASON,
    TERMINAL_STATUSES,
    IllegalTransition,
    RunStatus,
    can_transition,
    require_transition,
)

pytestmark = pytest.mark.invariant

TERMINALS = sorted(TERMINAL_STATUSES)
ALL = sorted(RunStatus)


def test_every_status_has_transition_row() -> None:
    """迁移表必须覆盖全部状态。漏一行等于给这个状态开了默认放行。"""
    assert set(ALLOWED_TRANSITIONS) == set(RunStatus)


def test_terminal_set_is_exactly_complement_of_running() -> None:
    """终态 = 全部状态 - {running}。两处定义必须同步。"""
    assert set(RunStatus) - {RunStatus.RUNNING} == TERMINAL_STATUSES


@pytest.mark.parametrize("target", TERMINALS)
def test_running_can_reach_each_terminal(target: RunStatus) -> None:
    """正控：running 到每个终态都合法，否则某种结局写不进库。"""
    assert can_transition(RunStatus.RUNNING, target)


@pytest.mark.parametrize("source", TERMINALS)
@pytest.mark.parametrize("target", ALL)
def test_terminal_is_absorbing(source: RunStatus, target: RunStatus) -> None:
    """负控：终态出发的任何迁移都必须被拒，尤其是 succeeded -> cancelled。"""
    assert not can_transition(source, target)


def test_self_transition_is_illegal() -> None:
    """重复 finish 不许静默成功（F4 迁移规则第 3 条）。"""
    assert not can_transition(RunStatus.RUNNING, RunStatus.RUNNING)
    assert not can_transition(RunStatus.SUCCEEDED, RunStatus.SUCCEEDED)


def test_require_transition_raises_on_illegal() -> None:
    with pytest.raises(IllegalTransition):
        require_transition(RunStatus.SUCCEEDED, RunStatus.CANCELLED)


def test_require_transition_passes_on_legal() -> None:
    require_transition(RunStatus.RUNNING, RunStatus.CANCELLED)


def test_reason_required_set_is_subset_of_terminals() -> None:
    """failed / limited / refused 必须有原因码；succeeded 不必。"""
    assert REQUIRES_REASON <= TERMINAL_STATUSES
    assert RunStatus.SUCCEEDED not in REQUIRES_REASON
    assert RunStatus.REFUSED in REQUIRES_REASON
