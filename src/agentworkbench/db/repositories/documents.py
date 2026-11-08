"""文档仓储：文档 / 页 / 片段三层的读写。

列表页的「可检索」判定看块数与向量数，不看状态列——
状态说已索引、数据却是空的时候，必须以数据为准。

去重靠**唯一约束**，不靠"先查再插"：两个请求同时查到"不存在"，各插一条，
两条共享同一磁盘文件，删一条连带毁另一条（C# `DocumentRepository.InsertAsync`
的注释原话）。所以这里两个函数都保留：`find_by_digest` 用于快路径，
`create` 撞约束时由调用方捕获 `sqlite3.IntegrityError` 回落。

来源：C# `Persistence/DocumentRepository.cs`
服务于：REQ-05 / REQ-06 / REQ-10
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import cast

from agentworkbench.core.documents import DocStatus, require_transition
from agentworkbench.db.repositories import knowledge as knowledge_repo


def create(
    conn: sqlite3.Connection,
    *,
    doc_id: str,
    tenant_id: str,
    file_name: str,
    stored_path: str,
    sha256: str,
    size_bytes: int,
    now: str,
    kb_id: str | None = None,
) -> None:
    """建一条 `pending`。撞 `(tenant_id, kb_id, sha256)` 唯一约束时抛 `IntegrityError`。

    `kb_id` 为空时兜底到默认公共库：旧测试与手工 SQL 不传，但新入口必须显式传。
    兜底只发生在写入口，读侧仍要求可访问集合包含该 id。
    """
    target_kb = kb_id or knowledge_repo.ensure_default_kb(conn, tenant_id, now=datetime.now(UTC))
    conn.execute(
        "insert into documents(id, tenant_id, file_name, stored_path, sha256, size_bytes,"
        " status, page_count, created_at, updated_at, kb_id)"
        " values (?,?,?,?,?,?,?,?,?,?,?)",
        (
            doc_id,
            tenant_id,
            file_name,
            stored_path,
            sha256,
            size_bytes,
            DocStatus.PENDING,
            0,
            now,
            now,
            target_kb,
        ),
    )


def find_by_digest(
    conn: sqlite3.Connection, *, tenant_id: str, sha256: str, kb_id: str | None = None
) -> sqlite3.Row | None:
    """快路径。真正的保证在唯一约束上——这里返回 `None` 不代表插得进去。

    去重键按知识库：同一文件可以分别存在于公共库与私人库。不传 `kb_id` 时
    只查默认公共库，保持旧调用方的行为。
    """
    target_kb = kb_id or knowledge_repo.ensure_default_kb(conn, tenant_id, now=datetime.now(UTC))
    return cast(
        sqlite3.Row | None,
        conn.execute(
            "select * from documents where tenant_id = ? and kb_id = ? and sha256 = ?",
            (tenant_id, target_kb, sha256),
        ).fetchone(),
    )


def get(
    conn: sqlite3.Connection,
    doc_id: str,
    *,
    tenant_id: str,
    kb_ids: frozenset[str] | None = None,
) -> sqlite3.Row | None:
    """按 id 取一份。**团队条件与 id 在同一条语句里**（`I-V2`）。

    `kb_ids` 非空时同时限制可访问知识库；路由必须传，测试/迁移可省略。
    `tenant_id` 是空串时不返回任何行（`where tenant_id = ''` 匹配不到）——
    这正是 AUTH-06 要的一侧：判不清就不给，而不是「没填就都给你」。
    """
    sql = "select * from documents where id = ? and tenant_id = ?"
    params: list[object] = [doc_id, tenant_id]
    if kb_ids is not None:
        if not kb_ids:
            return None
        marks = ",".join("?" * len(kb_ids))
        sql += f" and kb_id in ({marks})"
        params.extend(sorted(kb_ids))
    return cast(
        sqlite3.Row | None,
        conn.execute(sql, params).fetchone(),
    )


def get_unscoped(conn: sqlite3.Connection, doc_id: str) -> sqlite3.Row | None:
    """按 id 取一份，**不带团队条件**。只给后台解析 worker 用。

    名字里带 `unscoped` 是要它在代码里刺眼：这是一个绕开归属的入口，
    而绕开的理由只有一条——worker 处理的是**队列里那一份**，队列是进程内的、
    由上传请求写进去的，不存在「别人排进来的文档」。它拿不到 `Owner`，
    也不该拿到：给它一个租户参数只会变成「传什么都对」的摆设。

    **任何 HTTP 路径都不许调它。** 路由要读文档一律走 `get(..., tenant_id=)`，
    那里漏传是 `TypeError`；这里漏判是越权，而两者在调用点长得一模一样。
    """
    return cast(
        sqlite3.Row | None,
        conn.execute("select * from documents where id = ?", (doc_id,)).fetchone(),
    )


def set_status(
    conn: sqlite3.Connection,
    doc_id: str,
    dst: DocStatus,
    *,
    now: str,
    error_code: str | None = None,
    error: str | None = None,
    page_count: int | None = None,
    parsed_at: str | None = None,
) -> None:
    """迁移状态。非法迁移抛 `ValueError`，不静默兜底。

    `updated_at` 每次都刷：V-7 靠它算"`parsing` 停了多久"，没有这一列，
    "卡死"永远判定不出来。`error_code` 只在成功时清空——失败转成功要抹掉旧原因，
    但成功转失败必须写新原因，否则会读到上一次的。
    """
    # 内部按 id 读状态做迁移判定：这里读的是**同一行**，不是「用户能不能看」。
    # 用 `get_unscoped` 而不是 `get`——后者要租户参数，而写路径上的归属
    # 已经在路由那边判过了；在这里再要一个「反正一定对」的租户，只会让
    # 人以为状态迁移自己会做越权检查。
    row = get_unscoped(conn, doc_id)
    if row is None:
        raise KeyError(f"文档不存在：{doc_id}")
    current = DocStatus(str(row["status"]))
    if current is not dst:
        require_transition(current, dst)
    conn.execute(
        "update documents set status = ?, updated_at = ?,"
        " error_code = ?, error = ?,"
        " page_count = coalesce(?, page_count),"
        " parsed_at = coalesce(?, parsed_at) where id = ?",
        (
            dst,
            now,
            error_code,
            error,
            page_count,
            parsed_at,
            doc_id,
        ),
    )


def write_pages(conn: sqlite3.Connection, doc_id: str, pages: list[str]) -> None:
    """页文本落库。页码 1-based，与 `chunks.page_from` 同一套编号。"""
    conn.executemany(
        "insert or replace into document_pages(document_id, page_no, raw_text) values (?,?,?)",
        [(doc_id, index, text) for index, text in enumerate(pages, start=1)],
    )


def list_by_status(conn: sqlite3.Connection, statuses: list[DocStatus]) -> list[sqlite3.Row]:
    """按状态捞。启动补齐用它——这是"处理中不会变成永久态"的唯一机制。"""
    marks = ",".join("?" * len(statuses))
    return conn.execute(
        f"select * from documents where status in ({marks}) order by created_at",  # noqa: S608
        [str(status) for status in statuses],
    ).fetchall()


def list_all(
    conn: sqlite3.Connection, *, tenant_id: str, kb_ids: frozenset[str] | None = None
) -> list[sqlite3.Row]:
    """列表页用。新的在前——刚传的文件要立刻看见。

    排序键只到 `created_at`（秒级），同秒内多条顺序不定；加上 `id` 让分页稳定，
    否则同一页翻两次会出现"同一条出现两次、另一条消失"。

    **团队条件在 SQL 里，不做 Python 过滤**：后者要先把所有团队的行读进内存
    再丢掉，而「读过」在缓存、日志、未来的分页里都留得下痕迹（`I-V2`）。
    `kb_ids` 非空时同时限制可访问知识库。
    """
    sql = "select * from documents where tenant_id = ?"
    params: list[object] = [tenant_id]
    if kb_ids is not None:
        if not kb_ids:
            return []
        marks = ",".join("?" * len(kb_ids))
        sql += f" and kb_id in ({marks})"
        params.extend(sorted(kb_ids))
    sql += " order by created_at desc, id desc"
    return cast("list[sqlite3.Row]", conn.execute(sql, params).fetchall())


def list_pages(
    conn: sqlite3.Connection,
    doc_id: str,
    *,
    tenant_id: str,
    kb_ids: frozenset[str] | None = None,
) -> list[sqlite3.Row]:
    """页文本，按页码升序。**没有 `cleaned_text` 列**：清理只在切块时做，不单独存。

    团队条件走 `documents` 的存在性：页表自己没有 tenant 列，也不该有——
    多一列就多一处可能与主表不一致的地方。
    """
    sql = (
        "select p.page_no, p.raw_text from document_pages p"
        " join documents d on d.id = p.document_id"
        " where p.document_id = ? and d.tenant_id = ?"
    )
    params: list[object] = [doc_id, tenant_id]
    if kb_ids is not None:
        if not kb_ids:
            return []
        marks = ",".join("?" * len(kb_ids))
        sql += f" and d.kb_id in ({marks})"
        params.extend(sorted(kb_ids))
    sql += " order by p.page_no"
    return cast("list[sqlite3.Row]", conn.execute(sql, params).fetchall())


def list_chunks(
    conn: sqlite3.Connection,
    doc_id: str,
    *,
    tenant_id: str,
    kb_ids: frozenset[str] | None = None,
) -> list[sqlite3.Row]:
    """块，按 `seq` 升序。顺序即语义——切块质量就是靠"相邻两块读起来连不连"判断的。"""
    sql = (
        "select c.seq, c.page_from, c.page_to, c.heading, c.content, c.char_count"
        " from chunks c join documents d on d.id = c.document_id"
        " where c.document_id = ? and d.tenant_id = ?"
    )
    params: list[object] = [doc_id, tenant_id]
    if kb_ids is not None:
        if not kb_ids:
            return []
        marks = ",".join("?" * len(kb_ids))
        sql += f" and d.kb_id in ({marks})"
        params.extend(sorted(kb_ids))
    sql += " order by c.seq"
    return cast("list[sqlite3.Row]", conn.execute(sql, params).fetchall())


def delete_row(conn: sqlite3.Connection, doc_id: str) -> None:
    """删主表行。页与块由 `delete_document` 在同一事务里先删。"""
    conn.execute("delete from documents where id = ?", (doc_id,))


def count_chunks_and_vectors(conn: sqlite3.Connection, doc_id: str) -> tuple[int, int]:
    """列表页要的两个数：块数、向量数。

    列表要对每篇文档都做一次——**不做 N+1 拆解的理由是列表页有分页上限**，
    不是"这样更快"。文档上千之后这里要改成一次 `group by`，那时的判据是
    列表接口的 p95 超过 200ms，不是有人觉得慢。
    """
    chunks = conn.execute(
        "select count(*) from chunks where document_id = ?", (doc_id,)
    ).fetchone()[0]
    vectors = conn.execute(
        "select count(*) from chunks_vec where DocumentId = ?", (doc_id,)
    ).fetchone()[0]
    return int(chunks), int(vectors)
