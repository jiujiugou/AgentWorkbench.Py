"""上传编排：一条直线上六步，顺序即契约。

    ① 四码判定 → ② sha256 → ③ 去重 → ④ 落盘 → ⑤ 建记录(pending) → ⑥ 入队

三步的位置是刻意的，调换任何一处都会产生一类不报错的坏数据：

| 顺序 | 调换之后 |
|---|---|
| ② 在 ④ 之前 | 先落盘再算哈希，去重命中时盘上多一份孤儿文件（V-1 要求恰好 1 份） |
| ③ 在 ④ 之前 | 去重排在落盘之后，同上；且命中时白写一次盘 |
| ④ 在 ⑤ 之前 | 反过来则是"记录在、文件不在"——那份文档永远解析不了，而且看不出原因 |
| ⑥ 在 ⑤ 之后 | 入队失败不回滚记录。反过来的话，任务先入队、记录还没提交，worker 取到就查不到 |

这就是 `docs/实现/流程.md` 说的"直线流程的编排是副产品"：它没有分支，
但**每一步的位置都是一个决定**，而这些决定只能靠代码摆在一处才看得出来。

来源：C# `Documents/DocumentsController.Upload` + `DocumentRepository`
服务于：REQ-05
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from agentworkbench.db.repositories import documents as repo
from agentworkbench.ingest.queue import QueuePort
from agentworkbench.ingest.storage import LocalStorage
from agentworkbench.ingest.upload import DEFAULT_ALLOWED_SUFFIXES, UploadRejectCode, judge_upload


@dataclass(frozen=True)
class IngestResult:
    """`reject` 非空时 `document_id` 一定是 `None`：拒收不留任何痕迹。"""

    document_id: str | None
    created: bool
    reject: UploadRejectCode | None

    @property
    def accepted(self) -> bool:
        return self.reject is None


def ingest(
    conn: sqlite3.Connection,
    *,
    storage: LocalStorage,
    queue: QueuePort,
    tenant_id: str,
    filename: str | None,
    data: bytes,
    now: str,
    doc_id: str,
    max_bytes: int,
    allowed_suffixes: frozenset[str] = DEFAULT_ALLOWED_SUFFIXES,
) -> IngestResult:
    """收一份文件。返回时**只承诺 `pending`**，不承诺解析完。

    同步解析完再返回也能让端到端测试变绿，但那样断连容忍、状态可见、
    失败分类全废——所以这条断言（返回时状态必须是 `pending`）先于实现存在。
    """
    reject = judge_upload(
        filename=filename,
        size_bytes=len(data),
        max_bytes=max_bytes,
        allowed_suffixes=allowed_suffixes,
    )
    if reject is not None:
        return IngestResult(document_id=None, created=False, reject=reject)
    # judge_upload 已保证这里非空；cast 只是让类型变窄，不产生运行时检查。
    name = cast(str, filename)

    digest = storage.digest_of(data)

    existing = repo.find_by_digest(conn, tenant_id=tenant_id, sha256=digest)
    if existing is not None:
        return IngestResult(document_id=str(existing["id"]), created=False, reject=None)

    stored_path = storage.put(
        tenant_id=tenant_id, digest=digest, suffix=Path(name).suffix, data=data
    )

    try:
        repo.create(
            conn,
            doc_id=doc_id,
            tenant_id=tenant_id,
            file_name=name,
            stored_path=stored_path,
            sha256=digest,
            size_bytes=len(data),
            now=now,
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # 并发同传一份：输的一方没有新行，但盘上路径相同，不产生孤儿。
        conn.rollback()
        winner = repo.find_by_digest(conn, tenant_id=tenant_id, sha256=digest)
        if winner is None:
            raise
        return IngestResult(document_id=str(winner["id"]), created=False, reject=None)

    queue.enqueue(doc_id)
    return IngestResult(document_id=doc_id, created=True, reject=None)
