"""会话路由：列表 / 读消息 / 读轨迹。

三个都是 GET，都不写任何东西——**读路径不写库**，包括不"顺手"推
`updated_at`。在打开历史时更新它，会让"最后活动时间"变成"最后打开时间"，
而两者在一周之后就分不清是哪一天点开的。

这一层只做协议转换：归属校验、事务、排序都在下三层（`chat` / `trace` /
`db.repositories`）。路由里出现业务判断就是它漏上来了。唯一的例外是
"这条 id 存不存在"——404 是协议的一部分。

**会话的 404 不回显 id。** `documents.py` 那边写的是 `f"文档不存在：{doc_id}"`，
本域不能这么写：知识库是单租户共享的，回显 id 只是方便；而会话有归属，
回显 id 会让"你的会话"与"别人的会话"两条响应体天生不同（各自带着自己的 id），
于是 C-V1 那句"响应与不存在的 id 逐字节相同"就永远测不过——更糟的是，
它会诱使实现去比较两条响应体里的 id 是否相同，从而把判据改成能通过的样子。

服务于：REQ-04（自己的历史）/ REQ-14（过程可见）/ REQ-21（答案与引用重开仍在）
"""

from __future__ import annotations

from contextlib import closing
from typing import cast

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from agentworkbench.api.deps import Deps
from agentworkbench.api.errors import not_found
from agentworkbench.api.identity_context import current_principal, owner_of
from agentworkbench.chat import conversation as chat_turn
from agentworkbench.core.conversation import ConversationKind
from agentworkbench.core.runs import RunReasonCode, RunStatus, StepKind
from agentworkbench.db.repositories import conversations as repo
from agentworkbench.db.repositories import knowledge as knowledge_repo
from agentworkbench.trace.reader import RunTrace, read_run

router = APIRouter()

# 会话不存在与不是你的，用**同一句话**。两个字符串常量而不是就地写，
# 是为了让"逐字节相同"这件事在代码里看得见：两处引用同一个名字，
# 改一处漏一处的情况不会发生。
CONVERSATION_MISSING = "会话不存在。"
RUN_MISSING = "运行不存在。"


def _deps(request: Request) -> Deps:
    return cast(Deps, request.app.state.deps)


class ConversationDto(BaseModel):
    """列表的一条。形状对齐前端 `web/src/api/conversations.ts`。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    title: str
    kind: ConversationKind
    created_at: str
    updated_at: str


class CitationDto(BaseModel):
    """一张出处卡片。**全部字段都来自快照**，不回表查 `chunks`。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    no: int
    file_name: str
    seq: int
    excerpt: str
    check_first: str
    # None = 没有二级结论（数据里目前不会出现）；'unverified' = 判了但判不了。
    # 两者都不许补成 'supported'（D-12）。
    check_second: str | None
    coverage: float | None


class MessageDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: int
    role: str
    content: str
    created_at: str
    # 用户消息没有 run（库层 CHECK 保证）；assistant 必有，失败那一轮也有。
    run_id: str | None
    # 那条 run 的终态与原因。**不是消息自己的一列**：会话域不存状态（F5 §1.9），
    # 这里读的是 `agent_runs` 那一行。界面靠 `refused` 恢复拒答卡片——
    # 只比正文文案会在改文案那天把全部历史拒答变成普通回答（C-V13）。
    run_status: RunStatus | None
    run_reason: RunReasonCode | None
    citations: list[CitationDto]


class MessagesDto(BaseModel):
    """一条会话的全部历史。

    外层包一层而不是裸数组：引用是按消息挂的，两条消息各带各的卡片，
    裸数组装不下这个关系。前端 `conversationsApi.messages` 要跟着改一次。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    conversation_id: str
    kind: ConversationKind
    messages: list[MessageDto]


class TraceStepDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    seq: int
    kind: StepKind
    tool_name: str | None
    input: str | None
    output: str | None
    error: str | None
    model: str | None
    duration_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    created_at: str
    # 发出去了没回来。与"返回了空"是两件事，界面上要分开说。
    in_flight: bool


class TraceDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    run_id: str
    question: str
    status: RunStatus
    reason: RunReasonCode | None
    model: str | None
    prompt_version: str | None
    started_at: str
    finished_at: str | None
    duration_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    # None = 没有正文可校验（拒答 / 失败），0 = 校验过、零处问题。原样给出。
    citation_out_of_range: int | None
    citation_unsupported: int | None
    error: str | None
    steps: list[TraceStepDto]


@router.get("/api/conversations")
def list_conversations(
    request: Request, kind: ConversationKind | None = None
) -> list[ConversationDto]:
    """裸数组，按 `updated_at DESC, id DESC`。过滤与排序都在 SQL 里。

    `kind` 可选：不传时两类会话混排，每条仍带自己的类型；传了只返回该类型。
    """
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        rows = repo.list_for_owner(conn, owner_of(conn, request), kind=kind)
        return [
            ConversationDto(
                id=str(row["id"]),
                title=str(row["title"]),
                kind=ConversationKind(str(row["kind"])),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
            for row in rows
        ]


@router.get("/api/conversations/{conversation_id}/messages")
def list_messages(request: Request, conversation_id: str) -> MessagesDto:
    """消息 + 每条回答的引用快照。

    会话不存在与不是你的，返回**同一个** 404（见模块头）。不用先查会话再取消息：
    仓储的两条 SQL 各自带归属，两条都空就说明这条会话对你不可见。
    """
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        owner = owner_of(conn, request)
        messages = repo.list_messages(conn, owner, conversation_id)
        if not messages:
            # 空会话（建了没说话）也是合法的历史。所以不能"取不到消息 = 404"，
            # 还得看会话本身在不在——但**只查一次，且在消息为空时才查**，
            # 免得正常路径多一次往返。
            conversation = repo.get(conn, owner, conversation_id)
            if conversation is None:
                raise not_found(CONVERSATION_MISSING)
            return MessagesDto(
                conversation_id=conversation_id,
                kind=ConversationKind(str(conversation["kind"])),
                messages=[],
            )

        principal = current_principal(request)
        accessible = knowledge_repo.accessible_kb_ids(
            conn, tenant_id=owner.tenant_id, level_rank=principal.level_rank
        )
        by_message: dict[int, list[CitationDto]] = {}
        for row in repo.list_citations(conn, owner, conversation_id):
            kb_id = None if row["kb_id"] is None else str(row["kb_id"])
            visible = kb_id is None or kb_id in accessible
            by_message.setdefault(int(row["message_id"]), []).append(
                CitationDto(
                    no=int(row["no"]),
                    # 不可访问时不回显文件名/片段/页码；占位只说明曾有一条来源。
                    file_name=str(row["file_name"]) if visible else "来源已不可访问",
                    seq=int(row["seq"]) if visible else 0,
                    excerpt=str(row["excerpt"]) if visible else "",
                    check_first=str(row["check_first"]),
                    check_second=(
                        None
                        if not visible or row["check_second"] is None
                        else str(row["check_second"])
                    ),
                    coverage=(
                        None if not visible or row["coverage"] is None else float(row["coverage"])
                    ),
                )
            )

        return MessagesDto(
            conversation_id=conversation_id,
            kind=ConversationKind(str(messages[0]["conversation_kind"])),
            messages=[
                MessageDto(
                    id=int(row["id"]),
                    role=str(row["role"]),
                    content=str(row["content"]),
                    created_at=str(row["created_at"]),
                    run_id=None if row["run_id"] is None else str(row["run_id"]),
                    # left join 对 user 行给 NULL；assistant 行一定有值（库层 CHECK），
                    # 所以这里的 None 只可能是"这条消息不是回答"
                    run_status=None if row["run_status"] is None else RunStatus(row["run_status"]),
                    run_reason=(
                        None if row["run_reason"] is None else RunReasonCode(row["run_reason"])
                    ),
                    citations=by_message.get(int(row["id"]), []),
                )
                for row in messages
            ],
        )


@router.delete("/api/conversations/{conversation_id}", status_code=204)
def delete_conversation(request: Request, conversation_id: str) -> Response:
    """删一条会话与它的全部痕迹（五处同事务，见 `chat.delete_conversation`）。

    **这条路由是"删除"这个功能的入口，不是它的实现。** 仓储早就删对了，
    但没有它时界面上的删除按钮每次都失败——用户看到"删不掉"，而查代码的人
    看到"删除逻辑明明测过了"。一条只活在仓储层的删除等于没有删除（C-V12）。

    不存在 / 不是你的 / 已经删过，三种情况回**同一句** 404（C-V12、§1.5）。
    删除也是最容易被当成探针的动作：回 "这条不是你的" 等于承认它在。

    204 而不是 200：没有内容可回。也不回删掉几条——那是给日志和运维的，
    不是给这个用户的协议字段。
    """
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        deleted = chat_turn.delete_conversation(conn, owner_of(conn, request), conversation_id)
    if deleted is None:
        raise not_found(CONVERSATION_MISSING)
    return Response(status_code=204)


@router.get("/api/runs/{run_id}/trace")
def get_trace(request: Request, run_id: str) -> TraceDto:
    """一次运行的完整回放。

    挂在 `/api/runs` 而不是 `/api/conversations/{id}/runs`：轨迹的主键就是 run，
    而一条回答只指向一个 run。放在会话下会让人以为"一条会话有一串 run 要选"，
    而实际要选的那个 run 已经写在 assistant 行的 `run_id` 上了。
    """
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        owner = owner_of(conn, request)
        principal = current_principal(request)
        accessible = knowledge_repo.accessible_kb_ids(
            conn, tenant_id=owner.tenant_id, level_rank=principal.level_rank
        )
        trace = read_run(conn, owner, run_id, kb_ids=accessible)
        if trace is None:
            raise not_found(RUN_MISSING)
        return _trace_dto(trace)


def _trace_dto(trace: RunTrace) -> TraceDto:
    return TraceDto(
        run_id=trace.run_id,
        question=trace.question,
        status=trace.status,
        reason=trace.reason,
        model=trace.model,
        prompt_version=trace.prompt_version,
        started_at=trace.started_at,
        finished_at=trace.finished_at,
        duration_ms=trace.duration_ms,
        prompt_tokens=trace.prompt_tokens,
        completion_tokens=trace.completion_tokens,
        citation_out_of_range=trace.citation_out_of_range,
        citation_unsupported=trace.citation_unsupported,
        error=trace.error,
        # 顺序就是 seq（读侧已排好）。这一层不重排、不筛选、不截断：
        # 只要这里动一次手，"历史轨迹是快照"这条就变成"历史轨迹是投影"。
        steps=[
            TraceStepDto(
                seq=step.seq,
                kind=step.kind,
                tool_name=step.tool_name,
                input=step.input,
                output=step.output,
                error=step.error,
                model=step.model,
                duration_ms=step.duration_ms,
                prompt_tokens=step.prompt_tokens,
                completion_tokens=step.completion_tokens,
                created_at=step.created_at,
                in_flight=step.in_flight,
            )
            for step in trace.steps
        ],
    )
