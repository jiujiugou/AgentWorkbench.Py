"""轨迹回放：按 runId 取出当时的步骤、耗时与终态。

承诺的是「记录可回放」，不是「结果可重现」——LLM 输出不确定，
声称能重放的产品都在说谎。

**这个模块不 import 检索、判定、校验。** 不是纪律问题，是依赖方向问题：
它拿不到那些模块，「顺手重算一遍」在结构上就不成立。历史之所以是历史，
是因为它只被读出来，不被重新算一遍——重算过的历史会随模型与阈值的变更
自己改口，而读起来和"当时就是这样"一模一样（C-V6）。

**每一样都按原样给出，不截断、不摘要。** 当次推送给界面的那份是 600 字预览，
它是给人看着玩的；存进库、再从这里取出来的必须是全文。把预览当历史的后果
是失败步骤永远看不到——它恰恰是最需要回看的那一条（C-V5）。

来源：C# `AgentHarness/AgentHarnessService.GetRunAsync`
服务于：REQ-14 / REQ-15
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from agentworkbench.core.identity import Owner
from agentworkbench.core.runs import RunReasonCode, RunStatus, StepKind
from agentworkbench.db.repositories import runs as runs_repo


@dataclass(frozen=True, slots=True)
class TraceStep:
    """一步的完整记录：参数、返回、耗时、成败，以及"没回来"。"""

    seq: int
    kind: StepKind
    tool_name: str | None
    input: str | None
    output: str | None
    error: str | None
    model: str | None
    duration_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    created_at: str

    @property
    def in_flight(self) -> bool:
        """发出去了、没回来。

        不是"没有输出"，是"不知道结果"——进程被杀，或停止请求落在等返回那一段。
        这个三态必须能分开：把它显示成"没有输出"，用户会以为工具明确返回了空，
        于是去查工具；而实际要查的是那次调用本身（A-V6）。
        """
        return self.output is None and self.error is None


@dataclass(frozen=True, slots=True)
class RunTrace:
    """一次运行的完整回放：终态、原因、用量，与按序号排好的步骤。"""

    run_id: str
    question: str
    status: RunStatus
    reason: RunReasonCode | None
    model: str | None
    prompt_version: str | None
    started_at: str
    finished_at: str | None
    duration_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    citation_out_of_range: int | None
    citation_unsupported: int | None
    error: str | None
    steps: tuple[TraceStep, ...]


def read_run(conn: sqlite3.Connection, owner: Owner, run_id: str) -> RunTrace | None:
    """读一次运行的轨迹。`None` = 不存在，或者不是你的——两者不给区分。

    `citation_out_of_range` / `citation_unsupported` 为 `None` 表示**没有正文可校验**
    （拒答、失败），与 0（校验过、零处问题）是两件事。回放时按原样给出，
    不在这一层把 `None` 补成 0——补了之后"这次拒答"和"这次引用全对"
    在数据上就分不开了。
    """
    row = runs_repo.get_for_owner(conn, owner, run_id)
    if row is None:
        return None

    steps = tuple(_step_of(step) for step in runs_repo.list_steps_for_owner(conn, owner, run_id))

    return RunTrace(
        run_id=str(row["id"]),
        question=str(row["question"]),
        status=RunStatus(str(row["status"])),
        reason=None if row["reason"] is None else RunReasonCode(str(row["reason"])),
        model=None if row["model"] is None else str(row["model"]),
        prompt_version=None if row["prompt_version"] is None else str(row["prompt_version"]),
        started_at=str(row["started_at"]),
        finished_at=None if row["finished_at"] is None else str(row["finished_at"]),
        duration_ms=None if row["duration_ms"] is None else int(row["duration_ms"]),
        prompt_tokens=None if row["prompt_tokens"] is None else int(row["prompt_tokens"]),
        completion_tokens=(
            None if row["completion_tokens"] is None else int(row["completion_tokens"])
        ),
        citation_out_of_range=(
            None if row["citation_out_of_range"] is None else int(row["citation_out_of_range"])
        ),
        citation_unsupported=(
            None if row["citation_unsupported"] is None else int(row["citation_unsupported"])
        ),
        error=None if row["error"] is None else str(row["error"]),
        steps=steps,
    )


def _step_of(row: sqlite3.Row) -> TraceStep:
    """一行 → 一步。字符串列按原样取，不做长度处理。"""
    return TraceStep(
        seq=int(row["seq"]),
        kind=StepKind(str(row["kind"])),
        tool_name=None if row["tool_name"] is None else str(row["tool_name"]),
        input=None if row["input"] is None else str(row["input"]),
        output=None if row["output"] is None else str(row["output"]),
        error=None if row["error"] is None else str(row["error"]),
        model=None if row["model"] is None else str(row["model"]),
        duration_ms=None if row["duration_ms"] is None else int(row["duration_ms"]),
        prompt_tokens=None if row["prompt_tokens"] is None else int(row["prompt_tokens"]),
        completion_tokens=(
            None if row["completion_tokens"] is None else int(row["completion_tokens"])
        ),
        created_at=str(row["created_at"]),
    )
