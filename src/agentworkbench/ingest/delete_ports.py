"""删除端口的真实实现：对同一个 SQLite 连接清两处索引、删文件。

编排（`deletion.delete_document`）不知道 SQL；SQL 集中在这里，
所以"删除到底动了哪几张表"只有一个答案。

`count_*` 是删除补偿的关键：**"删掉了"的判据是再数一遍等于 0**，
不是"DELETE 语句没抛异常"——`chunks_fts` 与向量表都没有外键，
删不掉不报错，只留下检索能命中的幽灵（`db/consistency.py` 的 b2/c2 类）。

来源：C# `Knowledge.Local/IndexCleaner`
服务于：REQ-10（删除后不可检索）
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agentworkbench.ingest.storage import LocalStorage


class SqliteDeletePorts:
    """`DeletePorts` 的生产实现。一个连接 + 一个存储根。"""

    def __init__(self, conn: sqlite3.Connection, storage: LocalStorage) -> None:
        self._conn = conn
        self._storage = storage

    def count_fts(self, doc_id: str) -> int:
        row = self._conn.execute(
            "select count(*) from chunks_fts where document_id = ?", (doc_id,)
        ).fetchone()
        return int(row[0])

    def count_vectors(self, doc_id: str) -> int:
        row = self._conn.execute(
            "select count(*) from chunks_vec where DocumentId = ?", (doc_id,)
        ).fetchone()
        return int(row[0])

    def clear_fts(self, doc_id: str) -> None:
        self._conn.execute("delete from chunks_fts where document_id = ?", (doc_id,))
        self._conn.commit()

    def clear_vectors(self, doc_id: str) -> None:
        # 先删 vec 虚表行，再删元数据表：与 `indexer.clear_index` 同一顺序，
        # 让失败残留可判——元数据先没的话，vec 里的孤儿行查不出属于哪一块。
        self._conn.execute(
            "delete from vec_chunks_vec where ChunkId in"
            " (select ChunkId from chunks_vec where DocumentId = ?)",
            (doc_id,),
        )
        self._conn.execute("delete from chunks_vec where DocumentId = ?", (doc_id,))
        self._conn.commit()

    def delete_file(self, stored_path: str) -> None:
        path = self._storage.root / stored_path
        if path.is_relative_to(self._storage.root):
            Path.unlink(path, missing_ok=True)
