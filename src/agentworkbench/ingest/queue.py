"""解析队列：`huey` 的 SQLite 后端，指向**同一个库文件**。

**队列可以丢任务，状态不能丢。** 这一句是整个域最重要的一条划法：
队列只负责"尽快开始"，真源永远是 `documents.status`。所以：

- 入队失败不回滚记录——那条记录已经 `pending`，下次启动扫得到。
- 进程崩溃后被丢的任务，由 `recover_pending` 扫 `pending / parsing` 补回来。
- worker 取走任务后死掉也一样：状态停在 `parsing`，由 `scan_stalled` 判死。

为什么用 `huey` 而不是自己写 30 行：**重试退避与周期任务**。
V-7（`parsing` 超过 T 分钟转失败）需要一个定时扫描，手写版只有启动时扫一次——
进程不重启，卡死的记录永远不会被判死。`huey.periodic_task` 正好是它。

`InMemoryQueue` 保留，但身份是**测试替身**，不是生产实现。两者都满足 `QueuePort`，
所以流水线不知道差别——这正是要端口的原因。

来源：C# `Documents/DocumentParseQueue.cs`（内存无界 Channel，启动不重扫）
服务于：REQ-06
"""

from __future__ import annotations

import sqlite3
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from huey import SqliteHuey, crontab  # type: ignore[import-untyped]

from agentworkbench.core.documents import DocStatus, DocumentErrorCode
from agentworkbench.db.repositories import documents as repo
from agentworkbench.rag.indexer import clear_index

# 判定"卡死"的默认阈值。必须可配——写死就等于没有（V-7）。
DEFAULT_STALL_MINUTES = 15


class QueuePort(Protocol):
    """入队只需一件事：给一个文档 id。"""

    def enqueue(self, document_id: str) -> None: ...


class InMemoryQueue:
    """测试替身。丢任务是可接受的——`recover_pending` 兜底。"""

    def __init__(self) -> None:
        self._pending: deque[str] = deque()

    def enqueue(self, document_id: str) -> None:
        self._pending.append(document_id)

    def drain(self) -> list[str]:
        """取走全部待办。模拟"worker 把队列跑空一次"。"""
        items = list(self._pending)
        self._pending.clear()
        return items

    def __len__(self) -> int:
        return len(self._pending)


class HueyQueue:
    """生产实现。重试与定时调度在 `huey` 里，这里只是入队口。"""

    def __init__(self, huey: SqliteHuey, task: Callable[[str], object]) -> None:
        self._huey = huey
        self._task = task

    def enqueue(self, document_id: str) -> None:
        self._task(document_id)


def build_huey(
    db_path: str,
    *,
    run: Callable[[str], None],
    scan: Callable[[], None],
    stall_minutes: int = DEFAULT_STALL_MINUTES,
) -> tuple[SqliteHuey, HueyQueue]:
    """建队列与调度：一条解析任务 + 一条周期扫描。

    `filename` 指向业务库同一个文件——不引 Redis、不引第二个数据库，
    队列表（`sqlite_task` 等）与业务表同桌共存，运维只需要备份一个文件。
    """
    huey = SqliteHuey("agentworkbench", filename=db_path)

    # huey 没有类型标注，装饰器在 strict 下会报 untyped-decorator。
    # 这两条 ignore 是"库没有 py.typed"，不是"我们放松标准"。
    @huey.task(retries=2, retry_delay=10)  # type: ignore[untyped-decorator]
    def _process(document_id: str) -> None:
        run(document_id)

    @huey.periodic_task(crontab(minute=f"*/{max(1, stall_minutes // 3)}"))  # type: ignore[untyped-decorator]
    def _scan() -> None:
        scan()

    return huey, HueyQueue(huey, _process)


def recover_pending(conn: sqlite3.Connection, queue: QueuePort) -> int:
    """启动时扫一遍，把"处理中"的重新入队。返回补了多少条。

    `parsing` 也要重扫，理由：worker 取走任务后进程崩溃，那一行永远停在
    `parsing`，状态看起来像"正在干活"，比 `pending` 更隐蔽。
    """
    rows = repo.list_by_status(conn, [DocStatus.PENDING, DocStatus.PARSING])
    for row in rows:
        queue.enqueue(str(row["id"]))
    return len(rows)


def scan_stalled(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    stall_minutes: int = DEFAULT_STALL_MINUTES,
) -> list[str]:
    """把 `parsing` 停留过久的记录判为失败。返回被判死的 id 列表。

    判据是 `updated_at`，不是 `created_at`：一份 800 页的文档可以合法地解析很久，
    但它**不能**在同一个状态上停很久。用 `created_at` 会把"慢"判成"死"。
    """
    cutoff = (now - timedelta(minutes=stall_minutes)).strftime("%Y-%m-%dT%H:%M:%S")
    stalled = conn.execute(
        "select id from documents where status = ? and updated_at < ?",
        (str(DocStatus.PARSING), cutoff),
    ).fetchall()

    stamp = now.strftime("%Y-%m-%dT%H:%M:%S")
    killed: list[str] = []
    for row in stalled:
        document_id = str(row["id"])
        # 卡死的文档可能已经写了半个索引，不清就会"失败却可命中"（`[f]` 类）。
        clear_index(conn, document_id)
        repo.set_status(
            conn,
            document_id,
            DocStatus.FAILED,
            now=stamp,
            error_code=str(DocumentErrorCode.PARSE_STALLED),
            error=f"解析超过 {stall_minutes} 分钟无进展",
        )
        killed.append(document_id)
    conn.commit()
    return killed
