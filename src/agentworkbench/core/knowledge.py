"""知识库可见性：授权单位是知识库，文档继承知识库。

**不在这里做 IO，也不在这里读账号。** 这里只有两个纯函数：可见性状态机与
「这个等级能不能读这个知识库」。判定层与仓储都消费它，避免两处各写一遍
`public or rank >= min`——那两处一旦有一条漏了 `private` 判断，表现是
私人库在某条路径上可见，而测试只会覆盖到其中一条。

改坏这里会怎样：把 `private` 判断写成 `rank > min_level_rank`，恰好等于
门槛的账号会被挡在外面；把 `public` 写成需要等级，公共库对所有人消失。
两种都不报错，只会让**某一条**路径给出错误答案。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

MIN_LEVEL_RANK: Final[int] = 1
MAX_LEVEL_RANK: Final[int] = 5
DEFAULT_LEVEL_RANK: Final[int] = MIN_LEVEL_RANK


def require_level_rank(value: int) -> int:
    """等级必须是 1~5 的整数。**构造即校验**，不让 0 或 6 落进判定。"""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("账号等级必须是 1~5 的整数")
    if not MIN_LEVEL_RANK <= value <= MAX_LEVEL_RANK:
        raise ValueError(f"账号等级必须在 {MIN_LEVEL_RANK}~{MAX_LEVEL_RANK}：{value}")
    return value


class KnowledgeBaseVisibility(StrEnum):
    """知识库可见性。`public` 全员可读；`private` 按 `min_level_rank`。"""

    PUBLIC = "public"
    PRIVATE = "private"


VISIBILITY_TRANSITIONS: Final[dict[KnowledgeBaseVisibility, frozenset[KnowledgeBaseVisibility]]] = {
    KnowledgeBaseVisibility.PUBLIC: frozenset({KnowledgeBaseVisibility.PRIVATE}),
    KnowledgeBaseVisibility.PRIVATE: frozenset({KnowledgeBaseVisibility.PUBLIC}),
}
"""可见性状态机。public ↔ private 可迁移；同态迁移不合法。"""


class IllegalVisibilityTransition(ValueError):
    """迁到白名单外的可见性。"""


def can_transition_visibility(source: str, target: str) -> bool:
    """这次可见性迁移合法吗。**同态一律不合法**，与账户状态机同款。"""
    try:
        src = KnowledgeBaseVisibility(source)
        dst = KnowledgeBaseVisibility(target)
    except ValueError as exc:
        raise KeyError(f"未知知识库可见性：{source} -> {target}") from exc
    return dst in VISIBILITY_TRANSITIONS[src]


def require_visibility_transition(source: str, target: str) -> None:
    if not can_transition_visibility(source, target):
        raise IllegalVisibilityTransition(f"非法知识库可见性迁移：{source} -> {target}")


def can_read(level_rank: int, visibility: str, min_level_rank: int | None) -> bool:
    """这个等级能不能读这个知识库。

    `public` 不要求等级；`private` 要求 `level_rank >= min_level_rank`。
    `min_level_rank` 缺失的私人库一律不可读——判不清就不给（AUTH-06）。
    """
    rank = require_level_rank(level_rank)
    try:
        kind = KnowledgeBaseVisibility(visibility)
    except ValueError:
        return False
    if kind is KnowledgeBaseVisibility.PUBLIC:
        return True
    if min_level_rank is None:
        return False
    try:
        threshold = require_level_rank(min_level_rank)
    except ValueError:
        return False
    return rank >= threshold


@dataclass(frozen=True, slots=True)
class KnowledgeBase:
    """知识库的领域视图。**文档只认 `kb_id`，不复制可见性字段。**

    文档行复制 `visibility` 会在知识库改可见性时留下旧值；读侧必须回表
    查知识库/或用可访问集合，不能用文档上的副本。
    """

    kb_id: str
    tenant_id: str
    name: str
    visibility: KnowledgeBaseVisibility
    min_level_rank: int | None
    is_default: bool = False
    archived_at: str | None = None

    def readable_by(self, level_rank: int) -> bool:
        return self.archived_at is None and can_read(
            level_rank, str(self.visibility), self.min_level_rank
        )
