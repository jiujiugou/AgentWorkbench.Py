"""管理员审计读取：`GET /api/admin/events`。

只读、只对管理员、只按当前团队。写入仍走 `knowledge_repo.insert_admin_event`；
这里不回显请求体、口令、token 或文件内容，也不提供编辑/删除。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Annotated, cast

from fastapi import APIRouter, Query, Request

from agentworkbench.api.deps import Deps
from agentworkbench.api.identity_context import require_admin
from agentworkbench.api.schemas import ApiModel
from agentworkbench.db.repositories import knowledge as repo

router = APIRouter(prefix="/api/admin")


class AdminEventDto(ApiModel):
    event_id: str
    occurred_at: str
    actor_user_id: str | None
    target_user_id: str | None
    action: str
    resource_kind: str | None
    resource_id: str | None
    old_value: str | None
    new_value: str | None
    request_id: str | None


class AdminEventPageDto(ApiModel):
    events: list[AdminEventDto]
    next_cursor: str | None


def _dto(row: sqlite3.Row) -> AdminEventDto:
    return AdminEventDto(
        event_id=str(row["event_id"]),
        occurred_at=str(row["occurred_at"]),
        actor_user_id=None if row["actor_user_id"] is None else str(row["actor_user_id"]),
        target_user_id=None if row["target_user_id"] is None else str(row["target_user_id"]),
        action=str(row["action"]),
        resource_kind=None if row["resource_kind"] is None else str(row["resource_kind"]),
        resource_id=None if row["resource_id"] is None else str(row["resource_id"]),
        old_value=None if row["old_value"] is None else str(row["old_value"]),
        new_value=None if row["new_value"] is None else str(row["new_value"]),
        request_id=None if row["request_id"] is None else str(row["request_id"]),
    )


def _deps(request: Request) -> Deps:
    return cast(Deps, request.app.state.deps)


@router.get("/events")
def list_events(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[int | None, Query(ge=1)] = None,
    action: str | None = None,
    resource_kind: str | None = None,
    resource_id: str | None = None,
    actor_user_id: str | None = None,
) -> AdminEventPageDto:
    """按 id 倒序分页。`before` 是上一页最后一条事件的内部游标。"""
    principal = require_admin(request)
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        rows = repo.list_admin_events(
            conn,
            tenant_id=principal.owner.tenant_id,
            limit=limit,
            before_id=before,
            action=action,
            resource_kind=resource_kind,
            resource_id=resource_id,
            actor_user_id=actor_user_id,
        )
    return AdminEventPageDto(
        events=[_dto(row) for row in rows],
        next_cursor=str(rows[-1]["id"]) if len(rows) == limit else None,
    )
