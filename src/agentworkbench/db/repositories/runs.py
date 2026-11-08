"""执行记录仓储：run 与 step 的原始读写。

**这里只有 SQL，没有规则。** 能不能写、该不该写、写成什么状态，全在
`core/runs.py`（迁移白名单）与 `trace/recorder.py`（唯一写入口）。
把判断散进仓储，就会出现"两处各自决定算不算合法"，而它们迟早不一致。

两处守卫写在这里而不是上层，因为它们是**原子性**要求，不是业务规则：

1. `close_run` 的 `where status = 'running'`——第二次收口改不动任何行。
   先查再写的写法有窗口：两个请求同时看到 `running`，各写一个终态，
   后写的覆盖先写的，而两条都"通过"了检查（A-V4 反的正是这个）。
2. `close_step` 的"run 仍是 running"条件——终态之后再落到 step 上的结果
   一律丢弃。否则 `cancelled` 的 run 里会出现一条完整成功的 assistant 步，
   记录自相矛盾却被当成证据。

来源：C# `Persistence/AgentRunRepository.cs`
服务于：REQ-11 ~ REQ-15
"""

from __future__ import annotations

import sqlite3
from typing import cast

from agentworkbench.core.identity import Owner
from agentworkbench.core.runs import RunStatus, StepKind


def insert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    user_id: str,
    tenant_id: str,
    question: str,
    status: RunStatus,
    started_at: str,
    model: str | None = None,
    prompt_version: str | None = None,
) -> None:
    """建一行 run。

    `status` 没有默认值，必须显式传（A-V9 在库层守着）；这里也不给默认——
    给默认等于把库层那条 CHECK 绕过去。
    """
    conn.execute(
        "insert into agent_runs(id, user_id, tenant_id, question, status, model,"
        " prompt_version, started_at) values (?,?,?,?,?,?,?,?)",
        (run_id, user_id, tenant_id, question, status.value, model, prompt_version, started_at),
    )


def get_for_owner(conn: sqlite3.Connection, owner: Owner, run_id: str) -> sqlite3.Row | None:
    """按 id 取一行，**归属写在 WHERE 里**。

    返回 `None` 有两种可能，调用方不许区分：这条 run 不存在，或存在但不是你的。
    区分开（回 403）就等于承认这条 id 存在——那是 REQ-15 明确不许的。
    """
    return cast(
        sqlite3.Row | None,
        conn.execute(
            "select * from agent_runs where id = ? and tenant_id = ? and user_id = ?",
            (run_id, owner.tenant_id, owner.user_id),
        ).fetchone(),
    )


def get_status(conn: sqlite3.Connection, run_id: str) -> str | None:
    """只取状态。写入口每次写前用它判断，不缓存——库里是唯一真相。"""
    row = conn.execute("select status from agent_runs where id = ?", (run_id,)).fetchone()
    return None if row is None else str(row["status"])


def find_running_for_owner(
    conn: sqlite3.Connection,
    owner: Owner,
    *,
    conversation_id: str | None = None,
) -> str | None:
    """找当前用户最新的 `running` run，供“停止当前作答”使用。

    `conversation_id` 存在时按会话过滤：多标签页同时提问时，停止只收口
    当前这条会话里的那次运行，不去碰另一个标签页。没有会话 id（新会话的
    首问还没把响应交回来）时退回“该用户最新的 running”——界面同一时刻
    只允许一次生成，这是它成立的边界。
    """
    if conversation_id is None:
        row = conn.execute(
            "select id from agent_runs where tenant_id = ? and user_id = ? and status = ?"
            " order by started_at desc, rowid desc limit 1",
            (owner.tenant_id, owner.user_id, RunStatus.RUNNING.value),
        ).fetchone()
    else:
        row = conn.execute(
            "select r.id from agent_runs r join messages m on m.run_id = r.id"
            " where r.tenant_id = ? and r.user_id = ? and r.status = ? and m.conversation_id = ?"
            " order by r.started_at desc, r.rowid desc limit 1",
            (owner.tenant_id, owner.user_id, RunStatus.RUNNING.value, conversation_id),
        ).fetchone()
    return None if row is None else str(row["id"])


def cancel_run(conn: sqlite3.Connection, *, run_id: str, finished_at: str) -> bool:
    """把仍可停止的运行收口成 `cancelled`。返回是否真的改动了行。

    **“已生成”的 run 不允许再取消。** 条件里排除已经有 `output` 的 assistant
    步：模型已经返回、只是后处理还没走完时，停止请求到达得太晚，让它正常
    落地比把一条“取消了的运行”里留一条完整成功的生成步更诚实（A-V3）。
    `not exists` 与 `update` 在同一条语句里，挡住“检查后完成、收口前写入”
    的竞态。
    """
    cursor = conn.execute(
        "update agent_runs set status = ?, reason = ?, finished_at = ?"
        " where id = ? and status = ? and not exists ("
        "   select 1 from agent_steps where run_id = ? and kind = ? and output is not null)",
        (
            RunStatus.CANCELLED.value,
            None,
            finished_at,
            run_id,
            RunStatus.RUNNING.value,
            run_id,
            StepKind.ASSISTANT.value,
        ),
    )
    return cursor.rowcount > 0


def close_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    status: RunStatus,
    finished_at: str,
    reason: str | None,
    duration_ms: int | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    citation_out_of_range: int | None,
    citation_unsupported: int | None,
    error: str | None,
) -> bool:
    """收口。返回是否真的改动了行：`False` 说明这条 run 不在 `running`（被收过口、或不存在）。

    `duration_ms` / `prompt_tokens` / `completion_tokens` 缺值一律传 `None`，
    落成 NULL。**不许在这里做 `value or 0`**——0 会被当成真实用量参与成本统计，
    而 NULL 表示"供应商没回"（A-V10）。
    """
    cursor = conn.execute(
        "update agent_runs set status = ?, reason = ?, finished_at = ?, duration_ms = ?,"
        " prompt_tokens = ?, completion_tokens = ?, citation_out_of_range = ?,"
        " citation_unsupported = ?, error = ?"
        " where id = ? and status = ?",
        (
            status.value,
            reason,
            finished_at,
            duration_ms,
            prompt_tokens,
            completion_tokens,
            citation_out_of_range,
            citation_unsupported,
            error,
            run_id,
            RunStatus.RUNNING.value,
        ),
    )
    return cursor.rowcount > 0


def insert_step(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    kind: StepKind,
    created_at: str,
    tool_name: str | None = None,
    input: str | None = None,
) -> tuple[int, int]:
    """先写一行「已发出」的步骤，返回 `(step_id, seq)`。

    这是两段写的第一段，**发生在调用之前**。所以进程在调用中途被杀时，
    库里留下的是 `output` 与 `error` 都为 NULL 的一行——那不是脏数据，
    是「这次调用发出去了、没回来」的唯一证据（A-V6）。

    `seq` 在 INSERT 的子查询里算，不由调用方传：调用方自己排号，等于让它
    决定回放顺序。两条语句（先查 max 再插）会在中间留下窗口，
    `UNIQUE(run_id, seq)` 撞上时表现为 `IntegrityError` 而不是静默重排。
    """
    row = conn.execute(
        "insert into agent_steps(run_id, seq, kind, tool_name, input, created_at)"
        " values (?, (select coalesce(max(seq), 0) + 1 from agent_steps where run_id = ?),"
        " ?, ?, ?, ?) returning id, seq",
        (run_id, run_id, kind.value, tool_name, input, created_at),
    ).fetchone()
    return int(row["id"]), int(row["seq"])


def close_step(
    conn: sqlite3.Connection,
    *,
    step_id: int,
    run_id: str,
    output: str | None,
    error: str | None,
    model: str | None,
    duration_ms: int | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> bool:
    """第二段写：把结果补到那一行上。返回是否真的写进去了。

    `False` 有两种可能，都不是错误：run 已经收口（停止请求落在等返回的那一段），
    或步骤行不存在。调用方保持沉默是对的——那一步的证据形态就是「没回来」。
    """
    cursor = conn.execute(
        "update agent_steps set output = ?, error = ?, model = ?, duration_ms = ?,"
        " prompt_tokens = ?, completion_tokens = ?"
        " where id = ? and exists ("
        "   select 1 from agent_runs where id = ? and status = ?)",
        (
            output,
            error,
            model,
            duration_ms,
            prompt_tokens,
            completion_tokens,
            step_id,
            run_id,
            RunStatus.RUNNING.value,
        ),
    )
    return cursor.rowcount > 0


def list_steps_for_owner(conn: sqlite3.Connection, owner: Owner, run_id: str) -> list[sqlite3.Row]:
    """按序号取步骤，归属写在 join 里。

    **不能先取 run 再取 steps。** 两步式在顺序写反时就是跨租户读取，而写成测试
    很容易变成"先读后校验"——那时它依然全绿。这里一条语句同时回答两件事：
    这次运行是不是你的，以及它的步骤是什么。

    回放顺序靠 `seq`，不靠 `created_at`——同毫秒会乱。
    """
    rows = conn.execute(
        "select s.* from agent_steps s join agent_runs r on r.id = s.run_id"
        " where s.run_id = ? and r.tenant_id = ? and r.user_id = ? order by s.seq",
        (run_id, owner.tenant_id, owner.user_id),
    ).fetchall()
    return cast(list[sqlite3.Row], rows)
