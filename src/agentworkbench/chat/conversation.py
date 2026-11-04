"""会话：开一轮对话（新建 / 复用 / 他人的 id 当作不存在）、删除。

**这一层是事务边界的主人。** 仓储不 commit（它只发 SQL），这里调 `with conn:`
把"新建会话 + 落用户消息 + 推 updated_at"合成一次提交，把"三块一起写"合成另一次。

三条由调用顺序保证的事，不靠调用方记得：

1. **他人的 `conversationId` 当作不存在。** 不报错、不复用、不为他新建到别人的会话里
   ——报错等于承认这条 id 存在，复用等于把内容写进他人历史。
2. **标题只在新建那一次写。** 复用路径完全不碰 `title`：跟着改的话，两个人看
   同一条会话会看到两个标题。
3. **删除是一个事务。** 五处要么全没、要么全在。删一半时界面上显示的是"删除成功"，
   而库里还留着一份没人读得到的副本。

来源：C# `Webapi/Controllers/ConversationsController.cs`
服务于：REQ-04（自己的历史）/ REQ-21（三块一起保存）
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from agentworkbench.core.conversation import MessageRole, derive_title
from agentworkbench.core.identity import Owner
from agentworkbench.db.repositories import conversations as repo

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StartedTurn:
    """一轮问答的开头：落到哪条会话、是否新建、用户那条消息的 id。"""

    conversation_id: str
    created: bool
    user_message_id: int


def start_turn(
    conn: sqlite3.Connection,
    owner: Owner,
    *,
    question: str,
    conversation_id: str | None = None,
    now: str,
) -> StartedTurn:
    """开一轮：定位/新建会话，然后把用户那句话落行。

    新建与落消息在**同一个事务**里：分开提交时，进程若在两者之间停下，
    库里会多出一条没有任何消息的会话——列表里看起来像"用户点开了又关掉"，
    而实际上是写到一半。

    空问题在这里就拒绝：`derive_title` 只在新建路径上被调用，复用路径不会
    碰到它，于是空问题能一路写进历史的用户消息里。校验放在入口这一处，
    两条路径才一样。
    """
    if not question.strip():
        raise ValueError("空问题不落行")

    with conn:
        # 他人的 id 查到 `None`，与"查不到"走同一条路：**不复用、不报错**。
        # 报错会泄漏"这条 id 存在"，复用会把内容写进他人历史。
        target = None if conversation_id is None else repo.get(conn, owner, conversation_id)
        created = target is None
        if target is None:
            resolved = uuid4().hex
            repo.create(
                conn,
                owner,
                conversation_id=resolved,
                title=derive_title(question),
                now=now,
            )
        else:
            resolved = str(target["id"])

        user_message_id = repo.insert_message(
            conn,
            owner,
            conversation_id=resolved,
            role=MessageRole.USER,
            content=question,
            run_id=None,
            now=now,
        )
        repo.touch(conn, owner, resolved, now=now)

    return StartedTurn(conversation_id=resolved, created=created, user_message_id=user_message_id)


def delete_conversation(conn: sqlite3.Connection, owner: Owner, conversation_id: str) -> int | None:
    """删一条会话与它的全部痕迹。返回删掉的消息条数；`None` = 没删到。

    `None` 不区分"不存在"与"不是你的"——调用方照着回 404 就行。

    日志在**提交之后**写：提交前写的话，事务一失败就留下一条"已删除"的记录，
    而库里什么都没少。日志里只有会话 id、归属、条数、时间，**没有内容**——
    它是给运维回答"这条会话是被删了还是从来没建过"的，不是第二份历史。
    """
    with conn:
        deleted = repo.delete(conn, owner, conversation_id)

    if deleted is None:
        return None

    logger.info(
        "会话已删除 conversation_id=%s user_id=%s tenant_id=%s messages=%d",
        conversation_id,
        owner.user_id,
        owner.tenant_id,
        deleted,
    )
    return deleted
