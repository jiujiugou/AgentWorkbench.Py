"""轨迹写入：run 与 step 的唯一写入口。

**唯一是重点。** 只要存在第二条写入路径，「每条操作都留痕」就不成立——
被质疑时无法证明「没被记录的操作不存在」。`tests/invariants/test_trace_write_port.py`
用 AST 扫全仓，只允许本模块 import `db.repositories.runs`。

这个模块负责三件仓储不该管的事：

1. **顺序**：先写 `running`，再干活。进程被杀时库里已经有那一行，
   「没有记录」与「没发生过」才区分得开（A-V5）。
2. **合法性**：每次写前查库里的当前状态，用 `core/runs.py` 的迁移白名单判定。
   不缓存状态——缓存意味着内存与库能不一致，而 A-V3 的反例正是手改库。
3. **缺值**：拿不到 token 就是 `None`（落 NULL），不是 0。`value or 0` 在这个
   文件里一次都不许出现（A-V10）。

写入失败不吞：抛 `RecordingFailed`。**「记录没写进去」不是「日志没打上」，
是这次回答不该被当成成功**（F4 §三 硬规则）。要不要因此让请求失败，
是调用方的决定；写入口只保证它不可能静默。

来源：C# `Observability/ExecutionTraceStore.cs`、`ExecutionTrace.cs`
服务于：REQ-11 / REQ-12（F4 写侧）、A-V2 ~ A-V6、A-V10、A-V11
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from agentworkbench.core.clock import elapsed_ms, monotonic, utc_now
from agentworkbench.core.errors import AppError, ErrorCode
from agentworkbench.core.identity import Owner
from agentworkbench.core.runs import (
    REQUIRES_REASON,
    IllegalTransition,
    RunReasonCode,
    RunStatus,
    StepKind,
    require_transition,
)
from agentworkbench.db.repositories import runs as runs_repo


class RecordingFailed(AppError):
    """记录写不进去：这次作答没有记录，所以不算成功。

    继承 `AppError` 而不是自带一套翻译：错误码 `audit_unavailable` 是稳定契约，
    HTTP 状态码与 `retryable` 由 `ERROR_SPECS` 决定——这里不重复定义一份，
    否则两边迟早不一致。真正"写不进去"的原因放 message 里，只给人看。
    """

    def __init__(self, detail: str) -> None:
        super().__init__(ErrorCode.AUDIT_UNAVAILABLE, f"这次回答无法留下执行记录：{detail}")


@dataclass
class StepHandle:
    """一次已发出的调用。字段由调用方在 `with` 块里填，退出时落库。

    保持可变是有意的：结果只有调用返回之后才知道，而那一行在调用之前
    就已经写下去了（这是两段写的代价，也是 A-V6 只有这样才能验）。
    """

    id: int
    seq: int
    kind: StepKind
    output: str | None = None
    error: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


def describe_error(exc: BaseException) -> str:
    """异常转一行文本。带类型名——只有 message 时 `ValueError()` 是空字符串。

    不写 traceback：步骤表是给人扫一眼的，栈在日志里。
    """
    return f"{type(exc).__name__}: {exc}"


def cancel_run(
    conn: sqlite3.Connection,
    owner: Owner,
    run_id: str,
    *,
    now: Callable[[], str] = utc_now,
) -> RunStatus | None:
    """从**另一个请求**收口一次仍在运行的作答（REQ-16）。

    停止请求和作答不在同一个执行体里：作答线程可能正阻塞在模型调用中，
    这里只负责把 run 从 `running` 收口成 `cancelled`。已经在飞的步骤不删——
    它的 `output` 仍是 NULL，回放时显示成「已发出未返回」，正是“实际停在哪”
    的证据；迟到的结果由 `close_step` 的 `exists` 守卫丢弃。

    已经生成出正文的 run 不收口（仓储里的 `not exists` 条件）：那时停止
    来得太晚，让它正常落地，避免 `cancelled` 里挂着一个完整成功的生成步。

    返回收口后的状态；`None` = 这条 run 不存在或不属于调用者。已经处于
    终态的 run 原样返回，不覆盖（A-V4）。
    """
    row = runs_repo.get_for_owner(conn, owner, run_id)
    if row is None:
        return None
    current = RunStatus(str(row["status"]))
    if current is not RunStatus.RUNNING:
        return current

    require_transition(current, RunStatus.CANCELLED)
    try:
        written = runs_repo.cancel_run(conn, run_id=run_id, finished_at=now())
        conn.commit()
    except sqlite3.Error as exc:
        raise RecordingFailed(f"停止收口写不进去：{exc}") from exc

    if written:
        return RunStatus.CANCELLED
    # 没写进去有两种：并发方已经收口，或模型刚好返回、assistant 步已完整。
    # 后者按“停止太晚”处理，返回仓储里的真实状态，让调用方不要假装已取消。
    raw = runs_repo.get_status(conn, run_id)
    return None if raw is None else RunStatus(raw)


class Recorder:
    """一条 run 的写句柄。一个实例只服务一次执行（`start` 只能调一次）。

    状态判定每次都读库，不读自己的字段。这样即使有人绕过本类改库
    （A-V3 的反例），下一次写也会撞在真实状态上。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str,
        tenant_id: str,
        now: Callable[[], str] = utc_now,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._conn = conn
        self._user_id = user_id
        self._tenant_id = tenant_id
        self._now = now
        self._clock = clock
        self._run_id: str | None = None
        self._started: float | None = None

    @property
    def run_id(self) -> str:
        if self._run_id is None:
            raise RuntimeError("还没 start：先写 running 行，再干活")
        return self._run_id

    def start(
        self,
        *,
        question: str,
        model: str | None = None,
        prompt_version: str | None = None,
    ) -> str:
        """写 `running` 行并返回 run_id。**这一步必须在业务开始之前**。

        失败时不留下半状态：先插入，插成功了才记 `_run_id`。
        反过来写的话，插入炸了会留下一个"看起来已开始"的对象。
        """
        if self._run_id is not None:
            raise RuntimeError("这条 Recorder 已经 start 过：一次执行一个 run")

        run_id = uuid.uuid4().hex
        started_at = self._now()
        started_mono = self._clock()
        try:
            runs_repo.insert_run(
                self._conn,
                run_id=run_id,
                user_id=self._user_id,
                tenant_id=self._tenant_id,
                question=question,
                status=RunStatus.RUNNING,
                started_at=started_at,
                model=model,
                prompt_version=prompt_version,
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            raise RecordingFailed(f"run 起不来，这次执行没有记录：{exc}") from exc

        self._run_id = run_id
        self._started = started_mono
        return run_id

    @contextmanager
    def step(
        self,
        kind: StepKind,
        *,
        tool_name: str | None = None,
        input: str | None = None,
    ) -> Iterator[StepHandle]:
        """记一次真实外部调用。

        进入时插一行、退出时补结果。所以「已发出未返回」在库里是一个明确的
        形态（`output` 与 `error` 都 NULL），而不是缺席——缺席无法与
        「没发生过」区分。

        只有真实调用才能用这个函数。判定、否决、引用校验**不成步**：
        给它们各造一行，步骤数就不再等于调用数（A-V7 从此永远红），
        而且那正是 REQ-12 禁止的伪造。
        """
        run_id = self.run_id  # 未 start 时在这里抛
        status = self._status_of(run_id)
        if status is None:
            raise RecordingFailed(f"run 不存在：{run_id}")
        if status is not RunStatus.RUNNING:
            # 终态之后不许再追加步骤（A-V3）。必须在这里拦：
            # 出口拦不住「新起的调用」，而新起调用正是要反的那件事。
            raise IllegalTransition(f"run 已 {status.value}，不能再追加 {kind.value} 步骤")

        started_mono = self._clock()
        try:
            step_id, seq = runs_repo.insert_step(
                self._conn,
                run_id=run_id,
                kind=kind,
                created_at=self._now(),
                tool_name=tool_name,
                input=input,
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            raise RecordingFailed(f"步骤写不进去，这次调用没有记录：{exc}") from exc

        handle = StepHandle(id=step_id, seq=seq, kind=kind)
        try:
            yield handle
        except BaseException as exc:
            self._close(handle, run_id=run_id, started_mono=started_mono, error=describe_error(exc))
            raise
        else:
            self._close(handle, run_id=run_id, started_mono=started_mono, error=handle.error)

    def finish(
        self,
        status: RunStatus,
        *,
        reason: RunReasonCode | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        citation_out_of_range: int | None = None,
        citation_unsupported: int | None = None,
        error: str | None = None,
    ) -> None:
        """收口，只收一次。

        第二次调用抛 `IllegalTransition`，**先前的终态原样留在库里**——
        不做「覆盖成最后一次」（A-V4）。这是 `close_run` 的
        `where status = 'running'` 给的，不是这里"先查后写"的判断。
        """
        run_id = self.run_id
        current = self._status_of(run_id)
        if current is None:
            raise RecordingFailed(f"run 不存在：{run_id}")
        if status is RunStatus.RUNNING:
            raise ValueError("终态不能是 running：收口是走到头，不是重新开始")

        require_transition(current, status)
        if status in REQUIRES_REASON and reason is None:
            raise ValueError(f"{status.value} 必须有原因码，否则「为什么」答不上来")

        duration = None if self._started is None else elapsed_ms(self._started, clock=self._clock)
        try:
            written = runs_repo.close_run(
                self._conn,
                run_id=run_id,
                status=status,
                finished_at=self._now(),
                reason=None if reason is None else reason.value,
                duration_ms=duration,
                # 缺值原样传 None。这里出现 `or 0` 就等于把「供应商没回」
                # 记成「用了 0 个 token」，成本统计从此不可信（A-V10）
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                citation_out_of_range=citation_out_of_range,
                citation_unsupported=citation_unsupported,
                error=error,
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            raise RecordingFailed(f"终态写不进去，这次执行无法证明结束：{exc}") from exc

        if not written:
            # 走到这里说明查的时候是 running、写的时候不是——同一进程里只有
            # 「两个 Recorder 指向同一条 run」能做到。宁可报错，不许假装成功。
            raise IllegalTransition(f"run 已经有终态了：{run_id}")

    def _close(
        self,
        handle: StepHandle,
        *,
        run_id: str,
        started_mono: float,
        error: str | None,
    ) -> None:
        """补第二段。run 已收口时库里不动——那一步的答案就是「没回来」。"""
        try:
            runs_repo.close_step(
                self._conn,
                step_id=handle.id,
                run_id=run_id,
                output=handle.output,
                error=error,
                model=handle.model,
                duration_ms=elapsed_ms(started_mono, clock=self._clock),
                prompt_tokens=handle.prompt_tokens,
                completion_tokens=handle.completion_tokens,
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            raise RecordingFailed(f"步骤结果写不进去：{exc}") from exc

    def _status_of(self, run_id: str) -> RunStatus | None:
        raw = runs_repo.get_status(self._conn, run_id)
        return None if raw is None else RunStatus(raw)
