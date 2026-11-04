"""不变量：进程被强杀之后，库里还有那一行 `running`。

`kill -9` 是目前唯一能让「先写记录、再干活」这条顺序**无可辩驳**的实验：
进程没有机会执行任何收尾代码，写下去的行却还在。换成本进程内 `raise`，
总有人能说"那是 catch 里补写的"。

这一条专门反 C# 的形态（F4 §1.7 第 1 条）：那边从来只在结束时写一行终态，
所以进程被杀那轮**库里什么都没有**。事后看，「没记录」与「没发生」长得一样，
而用户恰恰会来问那一轮。

负控（`skip-start`）：不写 `running` 就直接进入长任务，同样被杀 → 库里 0 行。
它证明行是 `start` 写的，不是连接、建表或进程启动的副产品。

**改坏这个不变量会怎样**（本轮实测）：去掉 `start` 里的 `commit()` → 那行留在
未提交事务里，进程一死就跟着没了，上面第一条当场红（实际 0 行）。也就是说
「写了但没提交」和「根本没写」对用户是同一件事。

服务于：A-V5（有）
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agentworkbench.db import engine

SRC = Path(__file__).resolve().parents[2] / "src"

# 子进程做的事：连库 → （可选）写 running → 报告就绪 → 卡住等被杀。
# READY 在 start 之后打印，它到达父进程时那行已经提交进 WAL。
CHILD = textwrap.dedent(
    """
    import sys, time
    from agentworkbench.db import engine
    from agentworkbench.trace.recorder import Recorder

    db_path, mode = sys.argv[1], sys.argv[2]
    conn = engine.connect(db_path)
    recorder = Recorder(conn, user_id="user-kill", tenant_id="tenant-kill")
    if mode == "with-start":
        recorder.start(question="kill 掉这一轮", model="glm-4-flash")
    print("READY", flush=True)
    time.sleep(30)
    """
)


def run_until_ready(db_path: Path, mode: str) -> subprocess.Popen[str]:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    # 参数全是本文件里的常量：脚本是 CHILD 常量，路径来自 tmp_path。
    # 这不是"执行不可信输入"，是测试需要真起一个能被 SIGKILL 的进程。
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", CHILD, str(db_path), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert process.stdout is not None
    line = process.stdout.readline().strip()
    if line != "READY":
        process.kill()
        stderr = "" if process.stderr is None else process.stderr.read()
        pytest.fail(f"子进程没走到 READY（拿到 {line!r}）：\n{stderr}")
    return process


@pytest.mark.slow
def test_killed_process_leaves_a_running_row_behind(tmp_path: Path) -> None:
    db_path = tmp_path / "kill.db"
    with sqlite3.connect(db_path) as setup:
        setup.row_factory = sqlite3.Row
        engine.load_vec(setup)
        engine.create_schema(setup)

    process = run_until_ready(db_path, "with-start")
    process.kill()  # SIGKILL / TerminateProcess：没有收尾，没有 finally
    process.wait(timeout=15)
    assert process.returncode != 0, "进程应该是被杀掉的，不是自己退出的"

    conn = engine.connect(db_path)
    try:
        rows = conn.execute("select * from agent_runs").fetchall()
        assert len(rows) == 1, f"应该只有被杀那一轮的一行，实际 {len(rows)} 行"
        row = rows[0]
        assert row["status"] == "running", "终态还没写，状态必须停在 running"
        assert row["finished_at"] is None
        # 归属必须在：只留一个 run_id 的话，"是谁跑的、问的什么"事后答不上来
        assert row["user_id"] == "user-kill"
        assert row["tenant_id"] == "tenant-kill"
        assert row["question"] == "kill 掉这一轮"
    finally:
        conn.close()


@pytest.mark.slow
def test_without_the_first_write_the_kill_leaves_nothing(tmp_path: Path) -> None:
    """负控：跳过 `start` 再被杀 → 库里 0 行。

    没有这一条，上面那条的「有行」可能来自建表、连接或任何别的副作用。
    有这一条，才证明那行确实是「先写，再干活」写下的。
    """
    db_path = tmp_path / "kill-none.db"
    with sqlite3.connect(db_path) as setup:
        setup.row_factory = sqlite3.Row
        engine.load_vec(setup)
        engine.create_schema(setup)

    process = run_until_ready(db_path, "skip-start")
    process.kill()
    process.wait(timeout=15)

    conn = engine.connect(db_path)
    try:
        count = conn.execute("select count(*) as n from agent_runs").fetchone()["n"]
        assert count == 0, "没写 running 却被杀，库里不该有任何行"
    finally:
        conn.close()
