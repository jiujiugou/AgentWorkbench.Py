"""知识检索与问答路由：`/api/knowledge`。

`/search`（F2 时期）只查不答，`/ask`（F4）才是作答路径——两者不是同一件事：
前者没有模型调用、没有判定放行，后者每一次都会产出一条 run。这条区分写进
`declarations.py` 的两张表里，不靠读代码的人自己判断。

V-8（删除后不可检索）的验证手段在 `/search`：删除前后各查一次同一个词。

来源：C# `Webapi/Controllers/KnowledgeController.cs`
服务于：REQ-07（可用三 / 可用四）、REQ-11（每条作答路都留痕）
"""

from __future__ import annotations

import logging
from contextlib import closing
from typing import cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from agentworkbench.agent import runner
from agentworkbench.api.answer_flow import execute_turn, start_turn_or_503
from agentworkbench.api.deps import Deps
from agentworkbench.api.errors import validation_failed
from agentworkbench.api.identity_context import owner_of
from agentworkbench.rag.indexer import search_fts

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge")


class SearchHitDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    chunk_id: int
    document_id: str
    content: str


@router.get("/search")
def search(request: Request, q: str, limit: int = 5) -> list[SearchHitDto]:
    """全文检索。空查询直接 400——FTS5 对空 MATCH 串的行为是"全表匹配"，
    不是"零结果"，不能让它走到 SQL 那一层。"""
    if not q.strip():
        raise validation_failed("查询词不能为空")
    deps = cast(Deps, request.app.state.deps)
    with closing(deps.open_conn()) as conn:
        # 团队范围来自登录身份。这条路由是「只检索不生成」的调试入口，
        # 但它同样会返回正文片段——不过滤的话，它就是一个绕开 ask 的读取口：
        # 问答里查不到的资料，在这个端点上搜得到（I-V2 的「检索结果」那一路）。
        tenant_id = owner_of(conn, request).tenant_id
        hits = search_fts(conn, q, tenant_id=tenant_id, limit=min(max(limit, 1), 50))
        return [
            SearchHitDto(chunk_id=hit.chunk_id, document_id=hit.document_id, content=hit.content)
            for hit in hits
        ]


# ── 问答（F4：这条路由产 run）────────────────────────────────


class AskRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    # 上限是为了让"问题太长"在入口就明确失败。不设上限的话它会一路走到
    # 模型那边才被拒，而那时 run 已经开了、检索也做完了——用户等了几秒
    # 拿到一个看不懂的 400。
    question: str = Field(min_length=1, max_length=2000)
    # 续聊时带上；不带就新开一条。**他人的 id 会被当作"不存在"**（新建一条），
    # 不报错也不复用——报错等于承认这条 id 存在（C-V2）。
    conversation_id: str | None = None


class SourceDto(BaseModel):
    """证据面板的一条。

    正文随响应一起给：证据只取一次（§1.6），界面不该再回库查第二遍——
    那第二遍查到的可能已经不是当时那份。文件名同理：要让界面能写
    「《汇川H5U用户手册》第 12 页」，而不是一串 uuid。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    no: int
    chunk_id: int
    document_id: str
    file_name: str
    seq: int
    page_from: int
    page_to: int
    heading: str | None
    content: str


class CitationDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    sentence_index: int
    evidence_no: int
    document_id: str
    file_name: str
    seq: int
    chunk_id: int
    first: str
    second: str | None
    coverage: float | None


class ReportDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    sentence_count: int
    cited_sentence_count: int
    uncited_sentence_count: int
    out_of_range_count: int
    unsupported_count: int
    # 整段结论与无出处句占比：都由服务端算好。界面自己数，就会出现
    # 「同一份报告在两处得出不同结论」，而且 0/0 在两种语言里表现还不一样。
    support_verdict: str
    uncited_ratio: float
    citations: list[CitationDto]


class AskResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    # 落在哪条会话。新建时前端必须拿到它，否则下一次追问会再开一条。
    conversation_id: str
    run_id: str
    status: str
    reason: str | None
    verdict: str
    answer: str
    # 拒答时整体为 null，不是"六个 0"——0 是「校验过、零处问题」的意思，
    # 拒答根本没有正文可校验。界面据此决定显示哪一套（拒答卡片 / 引用清单）。
    report: ReportDto | None
    sources: list[SourceDto]


def to_response(result: runner.AskResult, *, conversation_id: str) -> AskResponse:
    report = result.report
    return AskResponse(
        conversation_id=conversation_id,
        run_id=result.run_id,
        status=result.status.value,
        reason=None if result.reason is None else result.reason.value,
        verdict=result.verdict.value,
        answer=result.answer,
        report=None
        if report is None
        else ReportDto(
            sentence_count=report.sentence_count,
            cited_sentence_count=report.cited_sentence_count,
            uncited_sentence_count=report.uncited_sentence_count,
            out_of_range_count=report.out_of_range_count,
            unsupported_count=report.unsupported_count,
            support_verdict=report.support_verdict.value,
            uncited_ratio=report.uncited_ratio,
            citations=[
                CitationDto(
                    sentence_index=c.sentence_index,
                    evidence_no=c.evidence_no,
                    document_id=c.document_id,
                    file_name=c.file_name,
                    seq=c.seq,
                    chunk_id=c.chunk_id,
                    first=c.first.value,
                    second=None if c.second is None else c.second.value,
                    coverage=c.coverage,
                )
                for c in report.citations
            ],
        ),
        sources=[
            SourceDto(
                no=hit.no,
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                file_name=hit.file_name,
                seq=hit.seq,
                page_from=hit.page_from,
                page_to=hit.page_to,
                heading=hit.heading,
                content=hit.content,
            )
            for hit in result.evidence.hits
        ],
    )


@router.post("/ask")
def ask(request: Request, payload: AskRequest) -> AskResponse:
    """一次知识问答。**这条路由产 run**（见 `declarations.PRODUCING`）。

    三段顺序不能换（判据与理由见 `answer_flow` 模块 docstring）：

    1. **先解析模型。** 没配时抛 503，此时库里什么都没写。"服务没就绪"不该记成
       "用户问过一次"，也不该在历史里留一条没有回答的问题。
    2. **再开一轮。** 会话 + 用户消息一个事务。用户问了就得留下，哪怕后面模型挂了。
    3. **作答 + 落回答。** `execute_turn`——与 `POST /api/runs` 共享的**唯一写入路径**，
       占位（C-V11）、失败补行（C-V4）、快照事务都在那一个函数里，这里不再有第二份。

    异步版（`routes/runs.py`）走同一个函数：它只把 ③ 挪进后台线程，
    ①② 留在请求里同步做——所以配置错误在 202 之前就 503，
    用户的问题在进程被杀之后也还留着。
    """
    deps = cast(Deps, request.app.state.deps)
    question = payload.question.strip()
    if not question:
        raise validation_failed("问题不能为空")

    with closing(deps.open_conn()) as conn:
        owner = owner_of(conn, request)
        # ① 模型没配 → 503，且**一行都不写**
        chat, model_name = deps.chat_factory(conn)
        # ② 开一轮：会话（标题只写一次）+ 用户消息，一个事务
        started = start_turn_or_503(
            conn, owner, question=question, conversation_id=payload.conversation_id
        )
        # ③④ 作答与落回答：与异步提交共用同一段代码（不许第二个写入者）
        result = execute_turn(
            conn,
            owner,
            deps,
            question=question,
            conversation_id=started.conversation_id,
            chat=chat,
            model_name=model_name,
        )

    return to_response(result, conversation_id=started.conversation_id)
