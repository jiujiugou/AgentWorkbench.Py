"""知识库仓储：默认公共库、可见性集合、管理员操作审计。

**只做 SQL，不做「谁能看」的最终判定。** 可见性集合由 `level_rank` 与
`public/private` 生成，判定函数在 `identity.policy`；路由消费同一个集合。
这样「检索过滤」与「文档列表过滤」不会各写一遍 `visibility='public' or ...`
——两处各写，漏掉 private 判断的那一处不会报错，只会让私人库在一条路径上可见。

服务于：REQ-24 / REQ-25 / ADR 0011
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from agentworkbench.core.knowledge import (
    DEFAULT_LEVEL_RANK,
    KnowledgeBaseVisibility,
    require_level_rank,
)


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _new_kb_id() -> str:
    return f"kb-{uuid4().hex[:16]}"


def default_kb_id(conn: sqlite3.Connection, tenant_id: str) -> str | None:
    row = conn.execute(
        "select id from knowledge_bases where tenant_id = ? and is_default = 1",
        (tenant_id,),
    ).fetchone()
    return None if row is None else str(row["id"])


def ensure_default_kb(conn: sqlite3.Connection, tenant_id: str, *, now: datetime) -> str:
    """确保默认公共库存在，返回 id。**对外只暴露这个入口**，不让调用方自己拼 id。"""
    existing = default_kb_id(conn, tenant_id)
    if existing is not None:
        return existing
    kb_id = f"kb-default-{tenant_id}"
    moment = _stamp(now)
    conn.execute(
        "insert or ignore into knowledge_bases"
        " (id, tenant_id, name, visibility, min_level_rank, is_default,"
        "  created_by, archived_at, created_at, updated_at)"
        " values (?, ?, '公共知识库', 'public', null, 1, null, null, ?, ?)",
        (kb_id, tenant_id, moment, moment),
    )
    created = default_kb_id(conn, tenant_id)
    return created if created is not None else kb_id


def get(conn: sqlite3.Connection, kb_id: str, *, tenant_id: str) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        conn.execute(
            "select * from knowledge_bases where id = ? and tenant_id = ?",
            (kb_id, tenant_id),
        ).fetchone(),
    )


def list_all(conn: sqlite3.Connection, *, tenant_id: str) -> list[sqlite3.Row]:
    """管理员视图：元数据全量，包含高于自己等级的私人库名称。

    文档内容仍由 `accessible_kb_ids` 过滤；这个方法不返回文档。
    """
    return cast(
        "list[sqlite3.Row]",
        conn.execute(
            "select * from knowledge_bases where tenant_id = ?"
            " order by is_default desc, created_at, id",
            (tenant_id,),
        ).fetchall(),
    )


def accessible_kb_ids(
    conn: sqlite3.Connection, *, tenant_id: str, level_rank: int
) -> frozenset[str]:
    """当前等级可访问的知识库 id 集合。**这是读侧唯一的可见性入口。**

    公共库不要求等级；私人库要求 `min_level_rank <= level_rank`。
    `archived_at` 非空的知识库不进入集合（归档 = 不再参与检索/列表）。
    """
    rank = require_level_rank(level_rank)
    rows = conn.execute(
        "select id from knowledge_bases"
        " where tenant_id = ? and archived_at is null"
        " and (visibility = 'public' or (visibility = 'private' and min_level_rank <= ?))",
        (tenant_id, rank),
    ).fetchall()
    return frozenset(str(row["id"]) for row in rows)


def list_accessible(
    conn: sqlite3.Connection, *, tenant_id: str, level_rank: int
) -> list[sqlite3.Row]:
    ids = accessible_kb_ids(conn, tenant_id=tenant_id, level_rank=level_rank)
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return cast(
        "list[sqlite3.Row]",
        conn.execute(
            f"select * from knowledge_bases where id in ({marks})"  # noqa: S608
            " order by is_default desc, created_at, id",
            sorted(ids),
        ).fetchall(),
    )


def create(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    name: str,
    visibility: str,
    min_level_rank: int | None,
    created_by: str,
    now: datetime,
) -> str:
    """建知识库。`public` 不允许带门槛；`private` 必须有 1~5 的门槛。"""
    kind = KnowledgeBaseVisibility(visibility)
    if kind is KnowledgeBaseVisibility.PUBLIC:
        if min_level_rank is not None:
            raise ValueError("公共知识库不能设置 min_level_rank")
        threshold = None
    else:
        if min_level_rank is None:
            raise ValueError("私人知识库必须设置 min_level_rank")
        threshold = require_level_rank(min_level_rank)
    kb_id = _new_kb_id()
    moment = _stamp(now)
    conn.execute(
        "insert into knowledge_bases"
        " (id, tenant_id, name, visibility, min_level_rank, is_default,"
        "  created_by, archived_at, created_at, updated_at)"
        " values (?, ?, ?, ?, ?, 0, ?, null, ?, ?)",
        (kb_id, tenant_id, name.strip(), str(kind), threshold, created_by, moment, moment),
    )
    return kb_id


def update_metadata(
    conn: sqlite3.Connection,
    kb_id: str,
    *,
    tenant_id: str,
    name: str | None,
    visibility: str | None,
    min_level_rank: int | None,
    now: datetime,
) -> bool:
    """改名称/可见性/门槛。返回是否改到了行。

    可见性迁移的合法性由 `policy` 先判；这里只写。`public` 必须把门槛清空，
    `private` 必须给出门槛——不能指望调用方同时传对，所以在这里再收敛一次。
    """
    row = get(conn, kb_id, tenant_id=tenant_id)
    if row is None:
        return False
    target_visibility = str(row["visibility"]) if visibility is None else visibility
    kind = KnowledgeBaseVisibility(target_visibility)
    if kind is KnowledgeBaseVisibility.PUBLIC:
        threshold = None
    else:
        threshold = (
            int(row["min_level_rank"])
            if min_level_rank is None
            else require_level_rank(min_level_rank)
        )
        if threshold is None:
            raise ValueError("私人知识库必须设置 min_level_rank")
    cursor = conn.execute(
        "update knowledge_bases set name = ?, visibility = ?, min_level_rank = ?,"
        " updated_at = ? where id = ? and tenant_id = ?",
        (
            str(row["name"]) if name is None else name.strip(),
            str(kind),
            threshold,
            _stamp(now),
            kb_id,
            tenant_id,
        ),
    )
    return cursor.rowcount == 1


def set_archived(
    conn: sqlite3.Connection,
    kb_id: str,
    *,
    tenant_id: str,
    archived: bool,
    now: datetime,
) -> bool:
    """??/??????????????

    ?????????????????????????????????
    ????? actor ????????????????????????????
    """
    moment = _stamp(now)
    cursor = conn.execute(
        "update knowledge_bases set archived_at = ?, updated_at = ? where id = ? and tenant_id = ?",
        (moment if archived else None, moment, kb_id, tenant_id),
    )
    return cursor.rowcount == 1


def insert_admin_event(
    conn: sqlite3.Connection,
    *,
    actor_user_id: str | None,
    tenant_id: str | None,
    target_user_id: str | None,
    action: str,
    resource_kind: str | None,
    resource_id: str | None,
    old_value: str | None,
    new_value: str | None,
    request_id: str | None,
    now: datetime,
) -> None:
    conn.execute(
        "insert into admin_events"
        " (event_id, occurred_at, actor_user_id, tenant_id, target_user_id, action,"
        "  resource_kind, resource_id, old_value, new_value, request_id)"
        " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f"ae-{uuid4().hex}",
            _stamp(now),
            actor_user_id,
            tenant_id,
            target_user_id,
            action,
            resource_kind,
            resource_id,
            old_value,
            new_value,
            request_id,
        ),
    )


def list_admin_events(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    limit: int,
    before_id: int | None = None,
    action: str | None = None,
    resource_kind: str | None = None,
    resource_id: str | None = None,
    actor_user_id: str | None = None,
) -> list[sqlite3.Row]:
    """????????**????????**???????? principal ???

    ??? id ???????????????????????????/??
    `before_id` ??????????? id???? id ???
    """
    clauses = ["tenant_id = ?"]
    params: list[object] = [tenant_id]
    if before_id is not None:
        clauses.append("id < ?")
        params.append(before_id)
    if action is not None:
        clauses.append("action = ?")
        params.append(action)
    if resource_kind is not None:
        clauses.append("resource_kind = ?")
        params.append(resource_kind)
    if resource_id is not None:
        clauses.append("resource_id = ?")
        params.append(resource_id)
    if actor_user_id is not None:
        clauses.append("actor_user_id = ?")
        params.append(actor_user_id)
    params.append(limit)
    sql = (
        "select id, event_id, occurred_at, actor_user_id, target_user_id, action,"  # noqa: S608
        " resource_kind, resource_id, old_value, new_value, request_id"
        " from admin_events where " + " and ".join(clauses) + " order by id desc limit ?"
    )
    return cast("list[sqlite3.Row]", conn.execute(sql, params).fetchall())


def default_level_rank() -> int:
    return DEFAULT_LEVEL_RANK
