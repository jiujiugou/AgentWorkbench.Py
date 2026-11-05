"""通用对话路由：`POST /api/chat`。

这条路由与 `/api/knowledge/ask` 是**两个入口**，不是同一个入口的 mode：
它不检索、不产生引用、不经过 `runner.ask`。组合顺序与知识问答相同：

1. 先解析模型。未配置 → 503，一行不写。
2. 再开一轮（`kind=general`）。会话类型不匹配的 id 当作不可续聊，新开一条。
3. 生成并落回答；run 与 assistant 占位行走 F4/F5 的同一条写入路径。

来源：F7 §2.2
服务于：REQ-23
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from agentworkbench.api.answer_flow import execute_general_turn, start_turn_or_503
from agentworkbench.api.deps import Deps
from agentworkbench.api.errors import validation_failed
from agentworkbench.api.identity_context import owner_of
from agentworkbench.chat.conversation import StartedTurn
from agentworkbench.core.conversation import ConversationKind
from agentworkbench.core.identity import Owner
from agentworkbench.db.repositories import conversations as repo

router = APIRouter()


class ChatRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    conversation_id: str
    run_id: str
    status: str
    mode: str
    answer: str
    # 通用对话没有来源与引用。显式给空数组，界面就不必猜字段缺失是什么意思。
    sources: list[object]
    citations: list[object]


def _history(conn: sqlite3.Connection, owner: Owner, started: StartedTurn) -> list[tuple[str, str]]:
    """当前会话里除本轮问题以外的消息。

    通用会话里只会有通用消息（知识库会话 id 在 `start_turn` 那一步已被
    当作不可续聊），所以这里不需要再按 kind 过滤内容。
    """
    return [
        (str(row["role"]), str(row["content"]))
        for row in repo.list_messages(conn, owner, started.conversation_id)
        if int(row["id"]) != started.user_message_id and str(row["content"]).strip()
    ]


@router.post("/api/chat")
def chat(request: Request, payload: ChatRequest) -> ChatResponse:
    deps = cast(Deps, request.app.state.deps)
    question = payload.question.strip()
    if not question:
        raise validation_failed("问题不能为空")

    with closing(deps.open_conn()) as conn:
        owner = owner_of(conn, request)
        chat_client, model_name = deps.chat_factory(conn)
        started = start_turn_or_503(
            conn,
            owner,
            question=question,
            conversation_id=payload.conversation_id,
            kind=ConversationKind.GENERAL,
        )
        result = execute_general_turn(
            conn,
            owner,
            question=question,
            conversation_id=started.conversation_id,
            history=_history(conn, owner, started),
            chat=chat_client,
            model_name=model_name,
        )

    return ChatResponse(
        conversation_id=started.conversation_id,
        run_id=result.run_id,
        status=result.status.value,
        mode=ConversationKind.GENERAL.value,
        answer=result.answer,
        sources=[],
        citations=[],
    )
