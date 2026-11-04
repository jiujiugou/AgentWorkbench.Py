"""运行路由：提交（202，异步执行）与状态查询（REQ-18 / REQ-19）。

**提交与执行分离**是这条路由存在的全部意义。同步的 `/api/knowledge/ask`
把 HTTP 连接和一次作答绑在一起：客户端断开，请求处理线程被取消（uvicorn 的
行为取决于部署，但契约上"响应没送达 = 这轮白跑"）——而程序化调用方（CI 脚本、
另一个服务）恰恰要"发出去就走，回头来取"。C# `AgentHub.RunAsync` 把取消令牌
绑到 `Context.ConnectionAborted` 上，断开即取消整条 run，REQ-19 在那边不成立。

三条铁律（判据 S-V3 的实现侧）：

1. **执行体不持有 Request / 连接对象。** 线程自己开自己的连接（WAL 一写多读）；
   chat 客户端在请求线程构造好再传进去——配置错误因此在 202 **之前**就 503，
   一行不写，与同步版同语义。
2. **①② 同步、③④ 异步。** 模型解析与 start_turn 在请求里做完：
   用户的问题在 202 返回时已经落库——进程在提交之后被杀，问题也还在。
3. **强引用 + 退出移除。** 线程集合持有运行中的执行线程（asyncio 裸
   create_task 会被 GC 的教训，代码规范 §4.2；线程虽由解释器持有，集合让
   "谁在跑"可枚举、可测试），跑完自己摘掉。

**202 的响应体只有 conversationId，没有 runId**——run 行在线程里才创建，
而客户端发现 runId 的路径已经存在：F5 的占位行（C-V11）保证 run 一启动，
消息列表里就出现一条带 runId 的空 assistant。轮询消息 → 拿到 runId → 查状态，
三段都是已有能力，这里不发明第四种。

服务于：REQ-18（S-V2）/ REQ-19（S-V3）
"""

from __future__ import annotations

import logging
import threading
from contextlib import closing
from typing import cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from agentworkbench.api.answer_flow import execute_turn, start_turn_or_503
from agentworkbench.api.deps import Deps
from agentworkbench.api.errors import not_found, validation_failed
from agentworkbench.api.identity_context import owner_of
from agentworkbench.api.routes.conversations import RUN_MISSING
from agentworkbench.core.identity import Owner
from agentworkbench.db.repositories import conversations as repo
from agentworkbench.trace.reader import read_run

logger = logging.getLogger(__name__)

router = APIRouter()

# 运行中的执行线程。集合 + 锁：add 在 start 之前（start 后立刻查询也看得见），
# discard 在线程自己的 finally 里——谁跑完谁摘，主线程不替它收尸。
_threads: set[threading.Thread] = set()
_threads_lock = threading.Lock()


class SubmitRunBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    question: str = Field(min_length=1, max_length=2000)


class RunDto(BaseModel):
    """一次运行的状态与已落库结果。

    **answer 读的是 `messages` 那一行，不是内存**：running 阶段它是空串
    （占位行，C-V11），终态之后是当时的正文。没有挂点行（run 起来后、
    占位之前进程被杀）是 None——"还没有"与"答了但空"是两个事实。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    run_id: str
    question: str
    status: str
    reason: str | None
    model: str | None
    started_at: str
    finished_at: str | None
    duration_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    citation_out_of_range: int | None
    citation_unsupported: int | None
    error: str | None
    answer: str | None
    # 拿到它就能顺藤摸到历史（GET /api/conversations/{id}/messages）——
    # 状态端点给"那一次"，历史端点给"全部"，两条路共享同一个 id。
    conversation_id: str | None


@router.post("/api/runs", status_code=202)
def submit_run(request: Request, payload: SubmitRunBody) -> dict[str, str]:
    """提交一次作答，立刻返回。**这条路由产 run**（见 `declarations.PRODUCING`）。"""
    deps = cast(Deps, request.app.state.deps)
    question = payload.question.strip()
    if not question:
        raise validation_failed("问题不能为空")

    with closing(deps.open_conn()) as conn:
        owner = owner_of(conn, request)
        # ① 模型解析：没配 → 503，一行不写。客户端构造好传进线程——
        # 配置错误在这里就失败，而不是 202 之后让一个注定失败的 run 去污染库
        chat, model_name = deps.chat_factory(conn)
        # ② 开一轮：问题落库。进程在 202 之后被杀，问题也还在（REQ-19 的"任务已受理"）
        started = start_turn_or_503(conn, owner, question=question)

    # ③ 执行：线程自开连接，不携带任何 Request / 路由层的连接
    thread = threading.Thread(
        target=_execute,
        args=(deps, owner, question, started.conversation_id, chat, model_name),
        name="run-executor",
        daemon=True,
    )
    with _threads_lock:
        _threads.add(thread)
    thread.start()

    return {"conversationId": started.conversation_id}


def _execute(
    deps: Deps,
    owner: Owner,
    question: str,
    conversation_id: str,
    chat: object,
    model_name: str,
) -> None:
    thread = threading.current_thread()
    try:
        with closing(deps.open_conn()) as conn:
            # 与 ask 同一个函数：占位、失败补行、快照事务都不会在这里走样
            execute_turn(
                conn,
                owner,
                deps,
                question=question,
                conversation_id=conversation_id,
                chat=chat,
                model_name=model_name,
            )
    except Exception:
        # 后台失败没有"响应"可背：run 行上已有终态与原因（recorder 收口），
        # 这里只留日志。吞掉它不报 500——202 已经说过了"受理"，结果是查出来的。
        logger.exception("后台 run 执行失败：conversation_id=%s", conversation_id)
    finally:
        with _threads_lock:
            _threads.discard(thread)


@router.get("/api/runs/{run_id}")
def read_run_status(request: Request, run_id: str) -> RunDto:
    """按 id 查状态与结果。别人的 id 与不存在的 id 用同一句 404（C-V1 同款）。"""
    deps = cast(Deps, request.app.state.deps)

    with closing(deps.open_conn()) as conn:
        owner = owner_of(conn, request)
        trace = read_run(conn, owner, run_id)
        if trace is None:
            raise not_found(RUN_MISSING)
        # answer 从 messages 按 run_id 取（占位行 / 正文都在那）。
        # 不重算、不从内存拿——这个端点可能在另一个进程、另一次部署里被调，
        # 内存里什么都没有，库里有的才是承诺过的（S-V2）。
        message = repo.find_message_by_run(conn, owner, run_id)

    return RunDto(
        run_id=trace.run_id,
        question=trace.question,
        status=trace.status.value,
        reason=None if trace.reason is None else trace.reason.value,
        model=trace.model,
        started_at=trace.started_at,
        finished_at=trace.finished_at,
        duration_ms=trace.duration_ms,
        prompt_tokens=trace.prompt_tokens,
        completion_tokens=trace.completion_tokens,
        citation_out_of_range=trace.citation_out_of_range,
        citation_unsupported=trace.citation_unsupported,
        error=trace.error,
        answer=None if message is None else str(message["content"]),
        conversation_id=None if message is None else str(message["conversation_id"]),
    )
