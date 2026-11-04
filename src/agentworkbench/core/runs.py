"""执行记录：run、step、终态与原因码，以及合法迁移表。

状态说「以什么结束」，原因码说「为什么」——只回一个终态时，
「跑满迭代被强制收口」和「模型答完了」长得一模一样。

来源：C# `Domain/Observability/AgentRunRecords.cs`、`Shared/Execution/AgentRunStatus.cs`
服务于：REQ-11 / REQ-13 / REQ-16（F4）

三条迁移规则（对应 F4 §1.8，被 tests/invariants/ 守着）：
1. 只有 `running` 能迁移，终态是吸收态。
2. 只有 `running` 能追加步骤；终态之后再写步骤一律抛错。
3. 迁移只发生一次；第二次 `finish` 抛错，不许「覆盖成最后一次」。
"""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    """一次执行的终态集合。五种是 REQ-13 的下界，不是上限。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LIMITED = "limited"
    REFUSED = "refused"


TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.LIMITED,
        RunStatus.REFUSED,
    }
)

# 迁移白名单是唯一真源；测试只是执行它。新增状态必须同时登记一行，否则测试会红。
ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.RUNNING: TERMINAL_STATUSES,
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.LIMITED: frozenset(),
    RunStatus.REFUSED: frozenset(),
}

# 这几种终态必须有原因码，否则「为什么失败/为什么拒」答不上来。
REQUIRES_REASON: frozenset[RunStatus] = frozenset(
    {RunStatus.FAILED, RunStatus.LIMITED, RunStatus.REFUSED}
)


class RunReasonCode(StrEnum):
    """终态的原因。同一件事不许状态和原因码两列都写。"""

    RUN_TIMEOUT = "run_timeout"
    MODEL_TIMEOUT = "model_timeout"
    ITERATION_LIMIT = "iteration_limit"
    BUDGET_EXCEEDED = "budget_exceeded"
    LOOP_DETECTED = "loop_detected"
    TOOL_ERROR = "tool_error"
    MODEL_UNAVAILABLE = "model_unavailable"
    KNOWLEDGE_INSUFFICIENT = "knowledge_insufficient"
    UNHANDLED_ERROR = "unhandled_error"


class StepKind(StrEnum):
    """步骤种类：只收录「真实发生过外部调用」的三类（F4 §1.9）。

    判定、否决、引用校验**不在此列**——它们没有产生任何调用。给它们留一个
    种类，等于给「伪造步骤」开一个入口（REQ-12：没有发生的步骤不得伪造）。
    步骤数 == 真实调用数，靠的就是这张封闭的小表。

    「失败」不是第四个种类，是 `error` 字段非空（F4 §三-5、A-H2）：
    失败的是某一次检索/生成，它仍然占自己那个序号，不能变成另一种步骤。
    """

    RETRIEVAL = "retrieval"
    ASSISTANT = "assistant"
    TOOL = "tool"


class IllegalTransition(Exception):
    """非法迁移：终态之后改写、或从终态出发的任何迁移。"""


def can_transition(source: RunStatus, target: RunStatus) -> bool:
    """这次迁移合法吗。同一状态到自身也不算合法（避免「重复 finish 静默成功」）。"""
    return target in ALLOWED_TRANSITIONS[source]


def require_transition(source: RunStatus, target: RunStatus) -> None:
    """不合法就抛。写入口调用它，而不是自己写 if。"""
    if not can_transition(source, target):
        raise IllegalTransition(f"非法迁移：{source.value} -> {target.value}")
