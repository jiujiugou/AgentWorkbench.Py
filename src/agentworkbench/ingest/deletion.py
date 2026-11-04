"""删除一份资料的补偿流程（REQ-10）。

删除要动四处，只有第一处能回滚：

    L1 `documents` / `document_pages` / `chunks`   同库，可事务
    L2 `chunks_fts` / `vec_chunks_vec`             跨表、跨扩展，不可回滚
    L3 磁盘文件                                     不可回滚，失败只留垃圾

顺序固定为"先索引、后记录、再文件"，理由是**失败后用户还有入口补救**：
先删记录会留下"列表看不见、检索还能命中"的幽灵，而用户已经没法再点一次删除。

来源：C# `DocumentsController.Delete` + `DocumentRepository`
服务于：REQ-10（F2）
"""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from enum import StrEnum
from typing import Protocol

from agentworkbench.core.documents import DocStatus, require_transition


class DeleteOutcome(StrEnum):
    """一次删除尝试的结果。**没有"部分成功"**——那正是中间态要表达的东西。"""

    DONE = "done"
    RETRY_LATER = "retry_later"
    ABORTED = "aborted"


class DeletePorts(Protocol):
    """删除时需要的外部动作。

    做成协议是为了让编排可测：真正的实现要碰 FTS5 与 `sqlite-vec`，
    而 `vec0` 虚表在没装扩展时连 `count(*)` 都会报 `no such module: vec0`。
    """

    def count_fts(self, doc_id: str) -> int: ...

    def count_vectors(self, doc_id: str) -> int: ...

    def clear_fts(self, doc_id: str) -> None: ...

    def clear_vectors(self, doc_id: str) -> None: ...

    def delete_file(self, stored_path: str) -> None: ...


def delete_document(conn: sqlite3.Connection, doc_id: str, ports: DeletePorts) -> DeleteOutcome:
    """尝试删一次。失败不清场，把状态留在 `delete_pending` 等下次重试。"""
    row = conn.execute(
        "select status, stored_path from documents where id = ?", (doc_id,)
    ).fetchone()
    if row is None:
        return DeleteOutcome.ABORTED

    status = DocStatus(row[0])

    # `delete_pending` 是吸收态，重入必须放行：重试按钮按第二次不该报"非法迁移"。
    if status is not DocStatus.DELETE_PENDING:
        require_transition(status, DocStatus.DELETE_PENDING)
        conn.execute(
            "update documents set status = ? where id = ?",
            (DocStatus.DELETE_PENDING, doc_id),
        )
        conn.commit()

    ports.clear_fts(doc_id)
    ports.clear_vectors(doc_id)
    if ports.count_fts(doc_id) or ports.count_vectors(doc_id):
        return DeleteOutcome.RETRY_LATER

    # 显式删三张，不依赖 `PRAGMA foreign_keys`——它是连接级开关，
    # 谁忘了开就留下孤儿行，而孤儿行不会报错（见 db/consistency.py 的 b2/c2）。
    with conn:
        conn.execute("delete from chunks where document_id = ?", (doc_id,))
        conn.execute("delete from document_pages where document_id = ?", (doc_id,))
        conn.execute("delete from documents where id = ?", (doc_id,))

    if row[1]:
        # 文件删不掉只留垃圾，不回退状态：记录回来的话，索引却是空的。
        with suppress(OSError):
            ports.delete_file(row[1])

    return DeleteOutcome.DONE
