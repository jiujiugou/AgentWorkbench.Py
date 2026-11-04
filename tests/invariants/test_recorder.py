"""不变量：写入口的六条保证。

每一条都对应一个「记录看起来完好、实际什么也证明不了」的失效形态：

- **先干活再记录**（A-V5）→ 进程被杀那轮库里没有行。「没记录」与「没发生」
  从此不可区分，而事后追问"那次为什么错"的正是那一轮。
- **终态覆盖写**（A-V4）→ 第二次收口把第一次冲掉。库里那个终态是最后一次
  调用留下的，不是实际发生的那次，**却被当成证据**。
- **终态之后还能追步骤**（A-V3）→ `cancelled` 的 run 里挂着一条完整成功的
  assistant 步，记录自相矛盾。
- **缺值写 0**（A-V10）→ 供应商不回 usage 记成「用了 0 token」，成本统计与
  账单永远对不上，且对不上时查不出原因。
- **写失败只记日志**（A-V11）→ 用户拿到了答案，库里没有这次执行。C# 的
  `LogWarning` 就是这么干的（`AgentHarnessService.cs:430`）。
- **失败写进 output 文本**（A-H2）→ 直接查库分不出成败，展示层只能猜。

**改坏这个不变量会怎样**（都是本轮实测过的，不是设想）：

- 去掉 `close_run` 的 `and status = 'running'` → 第二次收口会改掉行。只靠应用层
  的 `require_transition` 是**测不出来**的：那条测试依然全绿，因为它只证明了
  「同一对象重复调用被拒」。
- 把 `prompt_tokens` 写成 `prompt_tokens or 0` → `test_missing_usage_is_null_not_zero` 红。
- 去掉 `start` 里的 `commit()` → 强杀测试红（行留在未提交事务里，进程一死就没了）。

服务于：A-V3 / A-V4 / A-V5 / A-V10 / A-V11 / A-H2
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import cast

import pytest

from agentworkbench.core.runs import IllegalTransition, RunReasonCode, RunStatus, StepKind
from agentworkbench.db import engine
from agentworkbench.trace.recorder import Recorder, RecordingFailed

OWNER = "user-1"
TENANT = "tenant-1"


class FakeClock:
    """可推进的单调时钟。真实时钟会让「耗时 == 250ms」变成靠运气。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, ms: float) -> None:
        self.value += ms / 1000


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("pragma foreign_keys = on")
    engine.load_vec(connection)
    engine.create_schema(connection)
    yield connection
    connection.close()


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


def make_recorder(
    conn: sqlite3.Connection,
    clock: FakeClock,
    *,
    user_id: str = OWNER,
    tenant_id: str = TENANT,
) -> Recorder:
    """固定时间戳：断言 `started_at` 与 `finished_at` 时不用猜格式。"""
    return Recorder(
        conn,
        user_id=user_id,
        tenant_id=tenant_id,
        now=lambda: "2026-11-02T00:00:00",
        clock=clock,
    )


def run_row(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = cast(
        sqlite3.Row | None,
        conn.execute("select * from agent_runs where id = ?", (run_id,)).fetchone(),
    )
    assert row is not None, "run 行不存在"
    return row


# ── A-V5：先写 running，再干活 ────────────────────────────────


def test_start_writes_a_running_row_before_any_work(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    """业务开始之前库里就有这一行。

    反向写法（跑完再插）在这个测试里立刻暴露：`start` 一返回就必须能查到行，
    而不是等 `finish` 之后。
    """
    recorder = make_recorder(conn, clock)
    run_id = recorder.start(question="ADDR-005 的地址范围是多少", model="glm-4-flash")

    row = run_row(conn, run_id)
    assert row["status"] == RunStatus.RUNNING.value
    assert row["user_id"] == OWNER
    assert row["tenant_id"] == TENANT
    assert row["question"] == "ADDR-005 的地址范围是多少"
    assert row["model"] == "glm-4-flash"
    assert row["started_at"] == "2026-11-02T00:00:00"
    assert row["finished_at"] is None, "还没结束就不许有结束时间"


def test_owner_columns_follow_the_caller(conn: sqlite3.Connection, clock: FakeClock) -> None:
    """换调用者，行必须跟着换（A-V8 的 negative：写死常量 → 红）。

    这不依赖 F1。F1 解决的是「user_id 从哪来」（登录态），不是「列会不会跟随」——
    后者现在就能验：两个不同的调用者各起一条 run，两行的归属必须不同。
    写死成配置里的 `default` 会让这条当场红，而那正是最省事的实现方式。
    """
    make_recorder(conn, clock, user_id="alice", tenant_id="team-a").start(question="q1")
    make_recorder(conn, clock, user_id="bob", tenant_id="team-b").start(question="q2")

    owners = {
        row["question"]: (row["user_id"], row["tenant_id"])
        for row in conn.execute("select * from agent_runs").fetchall()
    }
    assert owners == {"q1": ("alice", "team-a"), "q2": ("bob", "team-b")}


def test_start_twice_is_a_programming_error(conn: sqlite3.Connection, clock: FakeClock) -> None:
    """一条 Recorder 只服务一次执行。允许 start 两次，就会出现一条没有归属的孤儿 run。"""
    recorder = make_recorder(conn, clock)
    recorder.start(question="q1")
    with pytest.raises(RuntimeError, match="已经 start 过"):
        recorder.start(question="q2")


def test_step_and_finish_before_start_raise(conn: sqlite3.Connection, clock: FakeClock) -> None:
    recorder = make_recorder(conn, clock)
    with pytest.raises(RuntimeError, match="还没 start"), recorder.step(StepKind.RETRIEVAL):
        pass
    with pytest.raises(RuntimeError, match="还没 start"):
        recorder.finish(RunStatus.SUCCEEDED)


# ── 两段写：进入即有行，退出才补结果（A-V6 的形态保证）────────


def test_step_row_exists_while_the_call_is_still_in_flight(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    """`with` 块里的这一刻，库里已经有一行，且 output/error 都是 NULL。

    这是「已发出未返回」能被认出来的唯一方式。如果步骤行等到调用返回才插，
    进程在调用中途被杀就什么也没留下——正好是最需要证据的那种情况。
    """
    recorder = make_recorder(conn, clock)
    run_id = recorder.start(question="q")

    with recorder.step(StepKind.ASSISTANT, tool_name=None) as step:
        assert step.seq == 1
        in_flight = conn.execute("select * from agent_steps where id = ?", (step.id,)).fetchone()
        assert in_flight is not None, "调用还没返回，这一步就该已经写下去了"
        assert in_flight["output"] is None
        assert in_flight["error"] is None

        clock.advance(250)
        step.output = '{"answer": "0x0000-0xFFFF"}'
        step.model = "glm-4-flash"
        step.prompt_tokens = 1200
        step.completion_tokens = 40

    done = conn.execute("select * from agent_steps where id = ?", (step.id,)).fetchone()
    assert done["output"] == '{"answer": "0x0000-0xFFFF"}'
    assert done["model"] == "glm-4-flash"
    assert done["duration_ms"] == 250
    assert done["prompt_tokens"] == 1200
    assert (
        conn.execute(
            "select count(*) as n from agent_steps where run_id = ?", (run_id,)
        ).fetchone()["n"]
        == 1
    )


def test_seq_is_assigned_by_the_writer_and_increases(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    """序号从 1 开始、连续。调用方没有 seq 参数——自己排号等于自己决定回放顺序。"""
    recorder = make_recorder(conn, clock)
    recorder.start(question="q")

    with recorder.step(StepKind.RETRIEVAL) as first:
        pass
    with recorder.step(StepKind.ASSISTANT) as second:
        pass

    assert (first.seq, second.seq) == (1, 2)


# ── A-V3：终态之后不许再追加步骤 ─────────────────────────────


@pytest.mark.parametrize(
    ("terminal", "reason"),
    [
        (RunStatus.SUCCEEDED, None),
        (RunStatus.FAILED, RunReasonCode.UNHANDLED_ERROR),
        (RunStatus.CANCELLED, None),
        (RunStatus.REFUSED, RunReasonCode.KNOWLEDGE_INSUFFICIENT),
    ],
)
def test_no_step_after_terminal(
    conn: sqlite3.Connection, clock: FakeClock, terminal: RunStatus, reason: RunReasonCode | None
) -> None:
    """四种终态一个都不许再挂步骤。

    `FAILED` 那一格就是 A-V3 的反例原文：「failed 后写成功步 → 抛错」。
    """
    recorder = make_recorder(conn, clock)
    recorder.start(question="q")
    recorder.finish(terminal, reason=reason)

    with pytest.raises(IllegalTransition, match="不能再追加"), recorder.step(StepKind.ASSISTANT):
        pass
    assert conn.execute("select count(*) as n from agent_steps").fetchone()["n"] == 0


def test_step_results_arriving_after_cancel_are_discarded(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    """停止请求落在「等返回」那一段：行留着，但结果不许再落进去。

    反过来的写法（无条件 update）会造出 `cancelled` 的 run 里挂着一条完整
    成功的 assistant 步——记录自相矛盾，而且看上去像是"取消了但答完了"。
    """
    recorder = make_recorder(conn, clock)
    recorder.start(question="q")

    with recorder.step(StepKind.ASSISTANT) as step:
        recorder.finish(RunStatus.CANCELLED)
        step.output = "迟到的答案"
        step.prompt_tokens = 900

    row = conn.execute("select * from agent_steps where id = ?", (step.id,)).fetchone()
    assert row is not None, "这一步确实发出过，行必须留着"
    assert row["output"] is None, "终态之后的结果不许落库"
    assert row["prompt_tokens"] is None
    assert run_row(conn, recorder.run_id)["status"] == RunStatus.CANCELLED.value


# ── A-V4：终态只写一次 ───────────────────────────────────────


def test_second_finish_raises_and_keeps_the_first_terminal(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    """第二次收口抛错，**第一次的终态原样留着**。

    这是本文件最值钱的一条：如果实现改成无条件 update，第二次会「成功」，
    库里的状态变成最后一次写进去的那个——而那次根本没发生。
    """
    recorder = make_recorder(conn, clock)
    run_id = recorder.start(question="q")
    recorder.finish(RunStatus.SUCCEEDED, prompt_tokens=100, completion_tokens=20)

    clock.advance(5000)
    with pytest.raises(IllegalTransition, match=r"非法迁移|已经有终态"):
        recorder.finish(RunStatus.FAILED, reason=RunReasonCode.UNHANDLED_ERROR)

    row = run_row(conn, run_id)
    assert row["status"] == RunStatus.SUCCEEDED.value, "先前的终态被冲掉了"
    assert row["reason"] is None, "失败的 reason 不许盖到成功记录上"
    assert row["finished_at"] == "2026-11-02T00:00:00"


def test_terminal_cannot_be_running(conn: sqlite3.Connection, clock: FakeClock) -> None:
    """`finish(running)` 是把收口当成重新开始——状态机会允许一切，所以这里必须挡。"""
    recorder = make_recorder(conn, clock)
    recorder.start(question="q")
    with pytest.raises(ValueError, match="终态不能是 running"):
        recorder.finish(RunStatus.RUNNING)


def test_storage_layer_holds_even_if_the_app_check_is_bypassed(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    """绕过写入口直接调仓储，第二次收口也改不动行。

    为什么要单测这一条：上面那条 `test_second_finish_raises...` 走的是
    `require_transition`——应用层拦住的。把 `close_run` 的
    `and status = 'running'` 去掉，那条测试**依然全绿**（改坏验证时实测）。
    也就是说它只证明了「同一对象重复调用会被拒」，没证明「库里只可能有一个终态」。

    真正会绕过应用层的是第二个写入者：另一个进程、另一条连接、同一 run_id
    （停止请求与正常收口撞在一起就是这种场景）。它们各自查库都看到 `running`，
    各自写一个终态——只有 SQL 的 `where` 能挡住后者。
    """
    from agentworkbench.db.repositories import runs as runs_repo

    recorder = make_recorder(conn, clock)
    run_id = recorder.start(question="q")

    def close(status: RunStatus, reason: str | None) -> bool:
        return runs_repo.close_run(
            conn,
            run_id=run_id,
            status=status,
            finished_at="2026-11-02T00:00:09",
            reason=reason,
            duration_ms=1,
            prompt_tokens=None,
            completion_tokens=None,
            citation_out_of_range=None,
            citation_unsupported=None,
            error=None,
        )

    assert close(RunStatus.SUCCEEDED, None) is True, "第一次收口必须成功"
    assert close(RunStatus.FAILED, "unhandled_error") is False, "第二次收口改动了行"
    assert run_row(conn, run_id)["status"] == RunStatus.SUCCEEDED.value
    assert run_row(conn, run_id)["reason"] is None


def test_storage_layer_drops_step_results_after_terminal(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    """`close_step` 的守卫同样要在仓储层成立，不能只靠 recorder 的入口检查。

    入口检查管的是「新起的调用」；这里管的是「已经在飞的那一次回来了」。
    两件事，两个地方，缺一个就漏一种。
    """
    from agentworkbench.core.runs import StepKind as Kind
    from agentworkbench.db.repositories import runs as runs_repo

    recorder = make_recorder(conn, clock)
    run_id = recorder.start(question="q")
    step_id, _ = runs_repo.insert_step(
        conn, run_id=run_id, kind=Kind.ASSISTANT, created_at="2026-11-02T00:00:01"
    )
    runs_repo.close_run(
        conn,
        run_id=run_id,
        status=RunStatus.CANCELLED,
        finished_at="2026-11-02T00:00:02",
        reason=None,
        duration_ms=1,
        prompt_tokens=None,
        completion_tokens=None,
        citation_out_of_range=None,
        citation_unsupported=None,
        error=None,
    )

    written = runs_repo.close_step(
        conn,
        step_id=step_id,
        run_id=run_id,
        output="迟到的结果",
        error=None,
        model="glm-4-flash",
        duration_ms=10,
        prompt_tokens=5,
        completion_tokens=5,
    )

    assert written is False
    row = conn.execute("select * from agent_steps where id = ?", (step_id,)).fetchone()
    assert row["output"] is None and row["model"] is None


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.LIMITED, RunStatus.REFUSED])
def test_terminals_that_need_a_reason_reject_empty_reason(
    conn: sqlite3.Connection, clock: FakeClock, status: RunStatus
) -> None:
    """「为什么失败/为什么拒」答不上来的终态不许写。"""
    recorder = make_recorder(conn, clock)
    recorder.start(question="q")
    with pytest.raises(ValueError, match="原因码"):
        recorder.finish(status)


def test_duration_comes_from_the_monotonic_clock(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    """耗时不是 `finished_at - started_at`：那是秒级，两秒内跑完会算成 0。"""
    recorder = make_recorder(conn, clock)
    run_id = recorder.start(question="q")
    clock.advance(1875)
    recorder.finish(RunStatus.SUCCEEDED)
    assert run_row(conn, run_id)["duration_ms"] == 1875


# ── A-V10：缺值写 NULL，不写 0 ───────────────────────────────


def test_missing_usage_is_null_not_zero(conn: sqlite3.Connection, clock: FakeClock) -> None:
    """供应商不回 usage 时两列必须是 NULL。

    0 会被当成真实用量算进成本与平均值里，而且**没有任何东西看起来不对**。
    """
    recorder = make_recorder(conn, clock)
    run_id = recorder.start(question="q")
    recorder.finish(RunStatus.SUCCEEDED)

    row = run_row(conn, run_id)
    assert row["prompt_tokens"] is None
    assert row["completion_tokens"] is None


def test_reported_usage_is_stored_as_reported(conn: sqlite3.Connection, clock: FakeClock) -> None:
    """正控：真回了就要真存。只有负控的话，「永远写 NULL」也能过。"""
    recorder = make_recorder(conn, clock)
    run_id = recorder.start(question="q")
    recorder.finish(RunStatus.SUCCEEDED, prompt_tokens=0, completion_tokens=7)

    row = run_row(conn, run_id)
    assert row["prompt_tokens"] == 0, "0 是真实值时要原样存——与 NULL 是两件事"
    assert row["completion_tokens"] == 7


def test_usage_absent_from_one_side_does_not_zero_the_other(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    """只回一半也要按一半存。`a or b` 这类写法会把 None 抹成 0，这里钉死。"""
    recorder = make_recorder(conn, clock)
    run_id = recorder.start(question="q")
    recorder.finish(RunStatus.SUCCEEDED, prompt_tokens=12)
    row = run_row(conn, run_id)
    assert row["prompt_tokens"] == 12
    assert row["completion_tokens"] is None


# ── A-H2：失败独立成列 ───────────────────────────────────────


def test_failed_step_keeps_error_separate_and_re_raises(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    """调用炸了：error 有值、output 为 NULL，**原始异常继续往上抛**。

    吞掉异常是另一种静默——上层会以为调用成功了。
    """
    recorder = make_recorder(conn, clock)
    recorder.start(question="q")

    with (
        pytest.raises(TimeoutError, match="model timeout"),
        recorder.step(StepKind.ASSISTANT) as step,
    ):
        raise TimeoutError("model timeout")
    recorder.finish(RunStatus.FAILED, reason=RunReasonCode.MODEL_TIMEOUT)

    row = conn.execute("select * from agent_steps where id = ?", (step.id,)).fetchone()
    assert row["error"] == "TimeoutError: model timeout"
    assert row["output"] is None, "失败不许写进 output——那样查库就分不出成败"


# ── A-V11：写失败不吞 ────────────────────────────────────────


def test_write_failure_raises_instead_of_logging(
    conn: sqlite3.Connection, clock: FakeClock
) -> None:
    """库写不进去 → 抛 `RecordingFailed`。

    C# 在这里 `catch { LogWarning }`，用户照拿答案（`AgentHarnessService.cs:430`）。
    那意味着这次回答在事后无法被证明存在，而当时没有任何人知道。
    """
    recorder = make_recorder(conn, clock)
    conn.execute("pragma query_only = on")

    with pytest.raises(RecordingFailed, match="run 起不来"):
        recorder.start(question="q")


def test_step_write_failure_also_raises(conn: sqlite3.Connection, clock: FakeClock) -> None:
    """步骤写不进去同样不许静默。只守 run 那一处，等于留了一半的洞。"""
    recorder = make_recorder(conn, clock)
    recorder.start(question="q")
    conn.execute("pragma query_only = on")

    with pytest.raises(RecordingFailed, match="步骤写不进去"), recorder.step(StepKind.RETRIEVAL):
        pass


def test_completion_write_failure_raises(conn: sqlite3.Connection, clock: FakeClock) -> None:
    """终态写不进去时，这次执行无法证明结束——必须在调用方那里变成失败。"""
    recorder = make_recorder(conn, clock)
    recorder.start(question="q")
    conn.execute("pragma query_only = on")

    with pytest.raises(RecordingFailed, match="终态写不进去"):
        recorder.finish(RunStatus.SUCCEEDED)
