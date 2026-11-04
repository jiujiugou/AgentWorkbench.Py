"""不变量：轨迹读回来的是当时那份记录，原文不截断、结果不重算、归属不可越。

失效形态（用户会看到什么）：

- **读侧顺手重算** → 改了模型、提示词或阈值之后重开历史，同一条回答的过程变了。
  它读起来和"当时就是这样"一模一样，而没有任何东西会红（C-V6）。
- **存的是当次推送的 600 字预览** → 展开历史时工具返回被腰斩在中间，
  失败步骤的报错常常正好落在截断之外，于是最该看的那一条彻底看不到（C-V5）。
- **把"已发出未返回"写成"没有输出"** → 用户以为工具明确返回了空，去查工具；
  而实际要查的是那次调用本身（A-V6）。
- **`None` 补成 0** → "这次拒答"与"这次引用全对"在数据上分不开了：拒答压根没有
  正文可校验，而 0 的意思是"校验过、零处问题"。
- **越权读** → 拿别人的 run id 取轨迹，要么拿到内容（泄漏），要么回 403
  （等于承认这条 id 存在）。两者都不行（REQ-15）。

改坏这个不变量会怎样：让 `read_run` 截断 `output`、让 `get_for_owner` 去掉归属
条件、把 `None` 的计数补成 0、在 reader 里 import `guard.citation` 重算校验
——四处都会让对应用例当场红。这不是设想，是本文件写完后逐条改坏验过的。

服务于：C-V5 / C-V6（读侧；端点与端到端另验）
"""

from __future__ import annotations

import ast
import dataclasses
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentworkbench.core.identity import Owner
from agentworkbench.core.runs import RunReasonCode, RunStatus, StepKind
from agentworkbench.db import engine
from agentworkbench.trace import reader

ALICE = Owner(user_id="alice", tenant_id="team-a")
BOB = Owner(user_id="bob", tenant_id="team-a")

T0 = "2026-11-03T09:00:00"
T1 = "2026-11-03T09:00:02"

SRC = Path(__file__).resolve().parents[2] / "src"


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("pragma foreign_keys = on")
    engine.load_vec(connection)
    engine.create_schema(connection)
    yield connection
    connection.close()


def make_run(
    conn: sqlite3.Connection,
    owner: Owner = ALICE,
    *,
    run_id: str = "r1",
    status: RunStatus = RunStatus.SUCCEEDED,
    reason: str | None = None,
    out_of_range: int | None = None,
    unsupported: int | None = None,
    error: str | None = None,
) -> str:
    conn.execute(
        "insert into agent_runs (id, user_id, tenant_id, question, status, reason, model,"
        " prompt_version, started_at, finished_at, duration_ms, prompt_tokens,"
        " completion_tokens, citation_out_of_range, citation_unsupported, error)"
        " values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            owner.user_id,
            owner.tenant_id,
            "H5U 的 E2 报警怎么处理",
            status.value,
            reason,
            "glm-4-flash",
            "abc123def456",
            T0,
            T1,
            2000,
            1516,
            88,
            out_of_range,
            unsupported,
            error,
        ),
    )
    return run_id


def add_step(
    conn: sqlite3.Connection,
    *,
    run_id: str = "r1",
    seq: int,
    kind: StepKind = StepKind.TOOL,
    tool_name: str | None = "search_knowledge",
    payload: str | None = "{}",
    output: str | None = "ok",
    error: str | None = None,
) -> None:
    conn.execute(
        "insert into agent_steps (run_id, seq, kind, tool_name, input, output, error,"
        " model, duration_ms, prompt_tokens, completion_tokens, created_at)"
        " values (?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, seq, kind.value, tool_name, payload, output, error, None, 120, 10, 5, T0),
    )


def as_json(trace: reader.RunTrace) -> str:
    """回放的对外形状：字段顺序固定，所以逐字节比较是有意义的。"""
    return json.dumps(dataclasses.asdict(trace), ensure_ascii=False, sort_keys=False)


# ── 顺序与完整性 ──


def test_steps_come_back_in_seq_order_even_if_inserted_out_of_order(
    conn: sqlite3.Connection,
) -> None:
    """回放顺序靠 `seq`，不靠 `created_at`，也不靠插入顺序。

    同一毫秒的两步在 `created_at` 上分不出先后；按插入顺序排则依赖写入时机。
    只有 `seq` 是写入口分配的回放键。
    """
    make_run(conn)
    add_step(conn, seq=2, tool_name="second")
    add_step(conn, seq=1, tool_name="first")

    trace = reader.read_run(conn, ALICE, "r1")

    assert trace is not None
    assert [(step.seq, step.tool_name) for step in trace.steps] == [(1, "first"), (2, "second")]


def test_long_output_is_not_truncated(conn: sqlite3.Connection) -> None:
    """C-V5 的负控：长返回必须原样给出，不许截成当次推送那个 600 字预览。

    截断之后"工具返回了什么"这个问题就没法回答了，而失败步骤的报错
    常常正好在截断点之后——最该看的那一条反而最先丢。
    """
    make_run(conn)
    long_text = "E" * 4000
    add_step(conn, seq=1, output=long_text)

    trace = reader.read_run(conn, ALICE, "r1")

    assert trace is not None
    assert len(trace.steps[0].output or "") == 4000
    assert trace.steps[0].output == long_text


def test_in_flight_step_is_marked_as_not_finished(conn: sqlite3.Connection) -> None:
    """`output` 与 `error` 都为 NULL = 发出去了没回来，**不是"没有输出"**。

    这两件事在界面上要显示成两句话：一句是"这次调用没回来"（去查进程/网络），
    一句是"工具返回了空"（去查工具参数）。
    """
    make_run(conn, status=RunStatus.RUNNING)
    add_step(conn, seq=1, output=None, error=None)

    trace = reader.read_run(conn, ALICE, "r1")

    assert trace is not None
    assert len(trace.steps) == 1, "没回来的那一步也必须出现，它是唯一证据"
    assert trace.steps[0].in_flight is True


def test_failed_step_keeps_its_error_and_is_not_in_flight(conn: sqlite3.Connection) -> None:
    """失败写 `error` 不写 `output`：成败查库可判，不和"没回来"混在一起（A-H2）。"""
    make_run(conn, status=RunStatus.FAILED, reason=RunReasonCode.MODEL_UNAVAILABLE.value)
    add_step(conn, seq=1, output=None, error="模型端点连不上")

    trace = reader.read_run(conn, ALICE, "r1")

    assert trace is not None
    step = trace.steps[0]
    assert step.in_flight is False
    assert step.error == "模型端点连不上"
    assert trace.reason is RunReasonCode.MODEL_UNAVAILABLE


# ── 归属 ──


def test_foreign_run_is_indistinguishable_from_a_missing_one(conn: sqlite3.Connection) -> None:
    """C-V5/REQ-15：拿别人的 run id 取轨迹，与瞎编一个 id 返回完全相同。

    区分开（403 vs 404）本身就是存在性证据。
    """
    make_run(conn, BOB, run_id="bobs-run")
    add_step(conn, run_id="bobs-run", seq=1, output="别人的工具返回")

    assert reader.read_run(conn, ALICE, "bobs-run") is None
    assert reader.read_run(conn, ALICE, "no-such-run") is None


def test_foreign_steps_are_not_reachable_by_run_id(conn: sqlite3.Connection) -> None:
    """步骤本身没有归属列，归属只能从 run 上带——所以那条 join 必须写在 SQL 里。"""
    make_run(conn, BOB, run_id="bobs-run")
    add_step(conn, run_id="bobs-run", seq=1)

    from agentworkbench.db.repositories import runs as runs_repo

    assert runs_repo.list_steps_for_owner(conn, ALICE, "bobs-run") == []
    assert len(runs_repo.list_steps_for_owner(conn, BOB, "bobs-run")) == 1


# ── 只读快照 ──


def test_reading_twice_returns_byte_identical_json(conn: sqlite3.Connection) -> None:
    """C-V6 正控：连开两次，JSON 逐字节相同。"""
    make_run(conn)
    add_step(conn, seq=1)
    add_step(conn, seq=2, kind=StepKind.ASSISTANT, output="答案")

    first = reader.read_run(conn, ALICE, "r1")
    second = reader.read_run(conn, ALICE, "r1")

    assert first is not None and second is not None
    assert as_json(first) == as_json(second)


def test_history_does_not_change_when_the_knowledge_tables_change(conn: sqlite3.Connection) -> None:
    """C-V6 的负控：改知识库之后重读，历史一个字都不许变。

    轨迹里的每一步都是当时抄下来的输入与返回。它若跟着 `chunks` 变，
    说明读侧回去查了证据表——而那时的证据已经不是当时那份了。
    """
    make_run(conn)
    add_step(conn, seq=1, payload='{"q": "E2 报警"}', output="E2 表示通讯超时")
    before = as_json(reader.read_run(conn, ALICE, "r1") or pytest.fail("读不到"))

    conn.execute(
        "insert into documents(id, tenant_id, file_name, stored_path, sha256, size_bytes,"
        " status, page_count, created_at, updated_at)"
        " values ('doc-1','team-a','手册.pdf','p','sha',1,'parsed',1,?,?)",
        (T0, T0),
    )
    conn.execute(
        "insert into chunks(document_id, seq, page_from, page_to, heading, content,"
        " char_count, created_at)"
        " values ('doc-1', 1, 1, 1, NULL, '完全不同的内容', 8, ?)",
        (T0,),
    )

    after = as_json(reader.read_run(conn, ALICE, "r1") or pytest.fail("读不到"))

    assert before == after


PIPELINE_MODULES = ("agentworkbench.agent", "agentworkbench.guard", "agentworkbench.rag")


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def test_reader_does_not_import_the_pipeline() -> None:
    """C-V6 的结构原因：读侧拿不到检索/判定/校验，重算在结构上不成立。

    这不是"我们保证不重算"，是"想重算也没有工具"。纪律会松，依赖方向不会
    ——除非有人改这一行，而改这一行会被这条例外当场抓住。
    """
    imported = _top_level_imports(SRC / "agentworkbench" / "trace" / "reader.py")

    offenders = sorted(
        name
        for name in imported
        if any(name == prefix or name.startswith(prefix + ".") for prefix in PIPELINE_MODULES)
    )
    assert not offenders, f"读侧 import 了流水线模块，重算就有了可能：{offenders}"


def test_the_import_scanner_sees_the_repository(conn: sqlite3.Connection) -> None:
    """自检：扫描器瞎了的话，上面那条恒真。它必须看得见 reader 确实 import 了 db。"""
    imported = _top_level_imports(SRC / "agentworkbench" / "trace" / "reader.py")

    assert "agentworkbench.db.repositories.runs" in imported


# ── 缺值的语义 ──


def test_refusal_keeps_citation_counts_null(conn: sqlite3.Connection) -> None:
    """拒答时两个计数是 `NULL`，不是 0——没有正文可校验，与"校验过、零处问题"不同。"""
    make_run(conn, status=RunStatus.REFUSED, reason=RunReasonCode.KNOWLEDGE_INSUFFICIENT.value)

    trace = reader.read_run(conn, ALICE, "r1")

    assert trace is not None
    assert trace.citation_out_of_range is None
    assert trace.citation_unsupported is None
    assert trace.reason is RunReasonCode.KNOWLEDGE_INSUFFICIENT


def test_zero_counts_stay_zero(conn: sqlite3.Connection) -> None:
    """正控：0 就是 0，别被当成"没有值"补成 None（那也是另一种编）。"""
    make_run(conn, out_of_range=0, unsupported=0)

    trace = reader.read_run(conn, ALICE, "r1")

    assert trace is not None
    assert trace.citation_out_of_range == 0
    assert trace.citation_unsupported == 0
