"""知识库管理路由：列表、建库、改可见性（REQ-24 / ADR 0011）。

**管理员看到的是元数据，不是内容。** 列表对管理员返回全部知识库（包括高于
自己等级的私人库名称）；文档列表/正文仍由 `accessible_kb_ids` 过滤，见
`documents.py`。这不是 UI 隐藏，而是两条不同的 SQL 条件。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from typing import cast

from fastapi import APIRouter, Request

from agentworkbench.api.deps import Deps
from agentworkbench.api.errors import not_found, validation_failed
from agentworkbench.api.identity_context import current_principal, principal_of, require_admin
from agentworkbench.api.schemas import ApiModel
from agentworkbench.core.knowledge import (
    KnowledgeBaseVisibility,
    require_level_rank,
    require_visibility_transition,
)
from agentworkbench.db.repositories import knowledge as repo
from agentworkbench.identity import policy
from agentworkbench.identity.models import Role

router = APIRouter(prefix="/api/knowledge-bases")

KB_MISSING = "知识库不存在。"


def _deps(request: Request) -> Deps:
    return cast(Deps, request.app.state.deps)


class KnowledgeBaseDto(ApiModel):
    id: str
    name: str
    visibility: str
    min_level_rank: int | None
    is_default: bool
    created_at: str
    archived_at: str | None = None


class CreateKnowledgeBaseBody(ApiModel):
    name: str
    visibility: str
    min_level_rank: int | None = None


class UpdateKnowledgeBaseBody(ApiModel):
    name: str | None = None
    visibility: str | None = None
    min_level_rank: int | None = None


def _dto(row: sqlite3.Row) -> KnowledgeBaseDto:
    return KnowledgeBaseDto(
        id=str(row["id"]),
        name=str(row["name"]),
        visibility=str(row["visibility"]),
        min_level_rank=None if row["min_level_rank"] is None else int(row["min_level_rank"]),
        is_default=bool(row["is_default"]),
        created_at=str(row["created_at"]),
        archived_at=None if row["archived_at"] is None else str(row["archived_at"]),
    )


def _threshold(row: sqlite3.Row) -> int | None:
    return None if row["min_level_rank"] is None else int(row["min_level_rank"])


def _audit(
    conn: sqlite3.Connection,
    request: Request,
    *,
    actor_user_id: str | None,
    tenant_id: str | None,
    action: str,
    resource_id: str | None,
    old_value: str | None,
    new_value: str | None,
) -> None:
    repo.insert_admin_event(
        conn,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        target_user_id=None,
        action=action,
        resource_kind="knowledge_base",
        resource_id=resource_id,
        old_value=old_value,
        new_value=new_value,
        request_id=str(getattr(request.state, "request_id", "") or ""),
        now=datetime.now(UTC),
    )


@router.get("")
def list_knowledge_bases(request: Request) -> list[KnowledgeBaseDto]:
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        principal = current_principal(request)
        if principal.role is Role.ADMIN:
            rows = repo.list_all(conn, tenant_id=principal.owner.tenant_id)
        else:
            rows = repo.list_accessible(
                conn, tenant_id=principal.owner.tenant_id, level_rank=principal.level_rank
            )
        return [_dto(row) for row in rows]


@router.post("", status_code=201)
def create_knowledge_base(body: CreateKnowledgeBaseBody, request: Request) -> KnowledgeBaseDto:
    deps = _deps(request)
    name = body.name.strip()
    if not name:
        raise validation_failed("知识库名称不能为空。")
    try:
        visibility = KnowledgeBaseVisibility(body.visibility)
    except ValueError:
        raise validation_failed("可见性只能是 public 或 private。") from None
    with closing(deps.open_conn()) as conn:
        principal = require_admin(request)
        threshold = None if visibility is KnowledgeBaseVisibility.PUBLIC else body.min_level_rank
        if visibility is KnowledgeBaseVisibility.PRIVATE:
            if threshold is None:
                raise validation_failed("私人知识库必须设置 minLevelRank。")
            try:
                threshold = require_level_rank(threshold)
            except ValueError as exc:
                raise validation_failed(str(exc)) from None
        principal_of(
            policy.judge_kb_management(
                actor=principal, visibility=str(visibility), min_level_rank=threshold
            )
        )
        try:
            kb_id = repo.create(
                conn,
                tenant_id=principal.owner.tenant_id,
                name=name,
                visibility=str(visibility),
                min_level_rank=threshold,
                created_by=principal.owner.user_id,
                now=datetime.now(UTC),
            )
        except sqlite3.IntegrityError:
            raise validation_failed("????????") from None
        row = repo.get(conn, kb_id, tenant_id=principal.owner.tenant_id)
        if row is None:  # pragma: no cover —— 同一事务里刚写
            raise not_found(KB_MISSING)
        _audit(
            conn,
            request,
            actor_user_id=principal.owner.user_id,
            tenant_id=principal.owner.tenant_id,
            action="kb.create",
            resource_id=kb_id,
            old_value=None,
            new_value=f"{visibility}:{threshold}",
        )
        conn.commit()
        return _dto(row)


def _set_archived(request: Request, kb_id: str, *, archived: bool) -> None:
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        principal = require_admin(request)
        row = repo.get(conn, kb_id, tenant_id=principal.owner.tenant_id)
        if row is None:
            raise not_found(KB_MISSING)
        principal_of(
            policy.judge_kb_management(
                actor=principal,
                visibility=str(row["visibility"]),
                min_level_rank=_threshold(row),
            )
        )
        if archived and bool(row["is_default"]):
            raise validation_failed("?????????")
        if (row["archived_at"] is not None) is archived:
            return
        if not repo.set_archived(
            conn,
            kb_id,
            tenant_id=principal.owner.tenant_id,
            archived=archived,
            now=datetime.now(UTC),
        ):
            raise not_found(KB_MISSING)
        _audit(
            conn,
            request,
            actor_user_id=principal.owner.user_id,
            tenant_id=principal.owner.tenant_id,
            action="kb.archive" if archived else "kb.unarchive",
            resource_id=kb_id,
            old_value="archived" if row["archived_at"] is not None else "active",
            new_value="archived" if archived else "active",
        )
        conn.commit()


@router.post("/{kb_id}/archive", status_code=204)
def archive_knowledge_base(request: Request, kb_id: str) -> None:
    """??????/??/??????????????????"""
    _set_archived(request, kb_id, archived=True)


@router.post("/{kb_id}/unarchive", status_code=204)
def unarchive_knowledge_base(request: Request, kb_id: str) -> None:
    """?????????????????????? active?"""
    _set_archived(request, kb_id, archived=False)


@router.patch("/{kb_id}")
def update_knowledge_base(
    kb_id: str, body: UpdateKnowledgeBaseBody, request: Request
) -> KnowledgeBaseDto:
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        principal = require_admin(request)
        row = repo.get(conn, kb_id, tenant_id=principal.owner.tenant_id)
        if row is None:
            raise not_found(KB_MISSING)
        principal_of(
            policy.judge_kb_management(
                actor=principal,
                visibility=str(row["visibility"]),
                min_level_rank=_threshold(row),
            )
        )
        if row["is_default"]:
            if body.visibility is not None and body.visibility != str(row["visibility"]):
                raise validation_failed("????????????")
            if body.min_level_rank is not None:
                raise validation_failed("?????????????")
        target_visibility = str(row["visibility"]) if body.visibility is None else body.visibility
        try:
            kind = KnowledgeBaseVisibility(target_visibility)
        except ValueError:
            raise validation_failed("可见性只能是 public 或 private。") from None
        if body.visibility is not None and target_visibility != str(row["visibility"]):
            require_visibility_transition(str(row["visibility"]), target_visibility)
        if kind is KnowledgeBaseVisibility.PUBLIC:
            target_threshold = None
        else:
            target_threshold = (
                _threshold(row) if body.min_level_rank is None else body.min_level_rank
            )
            if target_threshold is None:
                raise validation_failed("私人知识库必须设置 minLevelRank。")
            try:
                target_threshold = require_level_rank(target_threshold)
            except ValueError as exc:
                raise validation_failed(str(exc)) from None
        # 目标可见性也必须对 actor 可见：不能把库改成自己看不到的高等级私人库。
        principal_of(
            policy.judge_kb_management(
                actor=principal,
                visibility=str(kind),
                min_level_rank=target_threshold,
            )
        )
        try:
            changed = repo.update_metadata(
                conn,
                kb_id,
                tenant_id=principal.owner.tenant_id,
                name=body.name,
                visibility=str(kind),
                min_level_rank=target_threshold,
                now=datetime.now(UTC),
            )
        except sqlite3.IntegrityError:
            raise validation_failed("????????") from None
        if not changed:
            raise not_found(KB_MISSING)
        updated = repo.get(conn, kb_id, tenant_id=principal.owner.tenant_id)
        if updated is None:  # pragma: no cover
            raise not_found(KB_MISSING)
        _audit(
            conn,
            request,
            actor_user_id=principal.owner.user_id,
            tenant_id=principal.owner.tenant_id,
            action="kb.update",
            resource_id=kb_id,
            old_value=f"{row['visibility']}:{row['min_level_rank']}",
            new_value=f"{kind}:{target_threshold}",
        )
        conn.commit()
        return _dto(updated)
