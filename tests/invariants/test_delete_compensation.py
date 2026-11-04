"""删除补偿：这条不变量被改坏时，用户会看到什么。

改坏顺序：列表里已经看不到这份文档，检索却还能命中它——用户以为删掉了，
答案里却继续引用它，而他没有任何入口再点一次删除（"幽灵"）。

改坏中间态：删到一半失败，记录停在 `parsed`，列表显示"可检索"，
而索引已经空了——搜不到，也没人知道为什么（"残废记录"）。

两者都不抛异常，只在用户问"为什么删不掉 / 为什么还搜得到"时暴露。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest

from agentworkbench.core.documents import DocStatus
from agentworkbench.ingest.deletion import DeleteOutcome, delete_document

FIXTURE = Path(__file__).resolve().parents[2] / "evals" / "fixtures" / "agentworkbench.db"


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """复制 fixture 到内存（WAL 下直接 cp 会拿到坏库，用 backup API）。"""
    src = sqlite3.connect(f"file:{FIXTURE.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(":memory:")
    src.backup(dst)
    src.close()
    dst.row_factory = sqlite3.Row
    try:
        yield dst
    finally:
        dst.close()


class FakePorts:
    """假的索引与文件操作。

    真的那几个要 FTS5 与 sqlite-vec，而 `vec0` 虚表在没装扩展时连 `count(*)` 都报错，
    所以这一步必须能被替换，否则整条链路在 CI 里跑不了。
    """

    def __init__(
        self, *, leftover_fts: int = 0, leftover_vec: int = 0, file_error: bool = False
    ) -> None:
        self.fts: dict[str, int] = {}
        self.vec: dict[str, int] = {}
        self.deleted_files: list[str] = []
        self.calls: list[str] = []
        self.on_clear_fts: Callable[[], None] | None = None
        self._leftover_fts = leftover_fts
        self._leftover_vec = leftover_vec
        self._file_error = file_error

    def count_fts(self, doc_id: str) -> int:
        return self.fts.get(doc_id, 0)

    def count_vectors(self, doc_id: str) -> int:
        return self.vec.get(doc_id, 0)

    def clear_fts(self, doc_id: str) -> None:
        if self.on_clear_fts is not None:
            self.on_clear_fts()
        self.calls.append("clear_fts")
        self.fts[doc_id] = self._leftover_fts

    def clear_vectors(self, doc_id: str) -> None:
        self.calls.append("clear_vectors")
        self.vec[doc_id] = self._leftover_vec

    def delete_file(self, stored_path: str) -> None:
        self.calls.append("delete_file")
        if self._file_error:
            raise OSError("disk on fire")
        self.deleted_files.append(stored_path)


def _any_doc(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        "select id, stored_path from documents where status = 'parsed' limit 1"
    ).fetchone()
    assert row is not None, "fixture 里应当有可检索文档"
    return cast(sqlite3.Row, row)


def _count(conn: sqlite3.Connection, sql: str, doc_id: str) -> int:
    return int(conn.execute(sql, (doc_id,)).fetchone()[0])


def test_happy_path_clears_all_four_places(conn: sqlite3.Connection) -> None:
    doc = _any_doc(conn)
    ports = FakePorts()
    ports.fts[doc["id"]] = 5
    ports.vec[doc["id"]] = 5

    assert delete_document(conn, doc["id"], ports) is DeleteOutcome.DONE

    assert _count(conn, "select count(*) from documents where id = ?", doc["id"]) == 0
    assert _count(conn, "select count(*) from chunks where document_id = ?", doc["id"]) == 0
    assert _count(conn, "select count(*) from document_pages where document_id = ?", doc["id"]) == 0
    assert ports.count_fts(doc["id"]) == 0
    assert ports.count_vectors(doc["id"]) == 0
    assert ports.deleted_files == [doc["stored_path"]]


def test_order_is_index_then_record_then_file(conn: sqlite3.Connection) -> None:
    """顺序反了就留下幽灵：记录先没，用户再也没入口清索引。"""
    doc = _any_doc(conn)
    ports = FakePorts()
    ports.fts[doc["id"]] = 1
    ports.vec[doc["id"]] = 1

    delete_document(conn, doc["id"], ports)

    assert ports.calls == ["clear_fts", "clear_vectors", "delete_file"]


def test_status_is_marked_before_index_is_touched(conn: sqlite3.Connection) -> None:
    """先落状态再动手：中途崩溃时，用户至少能看到"删除未完成"。"""
    doc = _any_doc(conn)
    seen: list[str] = []
    ports = FakePorts()
    ports.fts[doc["id"]] = 1
    ports.vec[doc["id"]] = 1
    ports.on_clear_fts = lambda: seen.append(
        conn.execute("select status from documents where id = ?", (doc["id"],)).fetchone()[0]
    )

    delete_document(conn, doc["id"], ports)

    assert seen == [DocStatus.DELETE_PENDING]


def test_leftover_index_keeps_record_and_delete_pending(conn: sqlite3.Connection) -> None:
    """索引没清干净就不许删记录——宁可留着让用户重试，也不留幽灵。"""
    doc = _any_doc(conn)
    ports = FakePorts(leftover_fts=2)
    ports.fts[doc["id"]] = 5

    assert delete_document(conn, doc["id"], ports) is DeleteOutcome.RETRY_LATER

    row = conn.execute("select status from documents where id = ?", (doc["id"],)).fetchone()
    assert row["status"] == DocStatus.DELETE_PENDING
    assert _count(conn, "select count(*) from chunks where document_id = ?", doc["id"]) > 0
    assert ports.deleted_files == []


def test_reentry_after_failure_finishes_the_job(conn: sqlite3.Connection) -> None:
    """`delete_pending` 是吸收态，重入必须是合法的——否则重试按钮按一次就报错。"""
    doc = _any_doc(conn)
    first = FakePorts(leftover_fts=3)
    first.fts[doc["id"]] = 5
    assert delete_document(conn, doc["id"], first) is DeleteOutcome.RETRY_LATER

    second = FakePorts()
    second.fts[doc["id"]] = 3
    assert delete_document(conn, doc["id"], second) is DeleteOutcome.DONE
    assert _count(conn, "select count(*) from documents where id = ?", doc["id"]) == 0


def test_missing_document_aborts_without_raising(conn: sqlite3.Connection) -> None:
    """重复删除是正常操作（用户连点两次），不该变成 500。"""
    assert delete_document(conn, "no-such-id", FakePorts()) is DeleteOutcome.ABORTED


def test_file_error_does_not_roll_back_the_record(conn: sqlite3.Connection) -> None:
    """文件删不掉只留垃圾，不回退状态——否则记录回来了，索引却是空的。"""
    doc = _any_doc(conn)
    ports = FakePorts(file_error=True)
    ports.fts[doc["id"]] = 1
    ports.vec[doc["id"]] = 1

    assert delete_document(conn, doc["id"], ports) is DeleteOutcome.DONE
    assert _count(conn, "select count(*) from documents where id = ?", doc["id"]) == 0


def test_no_orphan_rows_even_when_foreign_keys_pragma_is_off(conn: sqlite3.Connection) -> None:
    """不依赖 `PRAGMA foreign_keys` 的开关状态——它是连接级的，谁忘了开就留脏行。"""
    conn.execute("pragma foreign_keys = off")
    doc = _any_doc(conn)
    ports = FakePorts()
    ports.fts[doc["id"]] = 1
    ports.vec[doc["id"]] = 1

    delete_document(conn, doc["id"], ports)

    assert _count(conn, "select count(*) from chunks where document_id = ?", doc["id"]) == 0
    assert _count(conn, "select count(*) from document_pages where document_id = ?", doc["id"]) == 0


def test_unknown_status_raises_instead_of_guessing(conn: sqlite3.Connection) -> None:
    """库里出现未定义的状态值时，猜一个比报错更危险。"""
    doc = _any_doc(conn)
    conn.execute("update documents set status = 'weird' where id = ?", (doc["id"],))
    conn.commit()

    with pytest.raises(ValueError):
        delete_document(conn, doc["id"], FakePorts())
