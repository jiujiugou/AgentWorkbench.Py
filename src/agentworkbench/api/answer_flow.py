"""一轮问答的执行流：作答 → 占位 → 落回答，**全项目唯一的写入路径**。

ask（同步，`routes/knowledge.py`）与 runs（异步，`routes/runs.py`）走的是
同一条路——不是"长得一样"，是**同一个函数**。复制一份"异步版执行流"就是
第二个写入者：两边的占位时机、失败补行、快照事务各自漂移，半年后一边修了
C-V11 的坑另一边没有，而界面上看起来都"能跑"。

四个步骤与 ask 路由 docstring 里写的同一套（顺序不能换）：
① 模型解析（调用方做，失败一行不写）② start_turn（调用方做，用户问了就得留下）
③ 作答：run 一起来就占一行空回答（C-V11 的第一段在生成之前）
④ 落回答：正文 + 引用快照 + run_id，一个事务。

服务于：REQ-03 / REQ-05（ask）、REQ-18 / REQ-19（runs）
"""

from __future__ import annotations

import logging
import sqlite3
from functools import partial
from typing import cast

from agentworkbench.agent import runner
from agentworkbench.agent.runner import AskResult
from agentworkbench.api.deps import Deps, utc_now
from agentworkbench.chat import conversation as chat_turn
from agentworkbench.chat import messages as chat_writer
from agentworkbench.core.identity import Owner
from agentworkbench.llm.client import ChatClient
from agentworkbench.trace.recorder import Recorder, RecordingFailed

_ = ChatClient  # cast 的字符串前向引用要用这个名字；显式引用防 ruff 当未使用删掉

logger = logging.getLogger(__name__)


def start_turn_or_503(
    conn: sqlite3.Connection,
    owner: Owner,
    *,
    question: str,
    conversation_id: str | None = None,
) -> chat_turn.StartedTurn:
    """开一轮：会话 + 用户消息，一个事务。库写不进去 → 503 `audit_unavailable`。

    翻译必须发生在**第一个写入点**：库一坏（只读 / 锁死 / 磁盘满），撞上来的
    是这里而不是 recorder——不翻译的话，"这次对话没能留下来"会被冒充成
    "服务器内部出错"，运维去查代码而用户该去查磁盘。
    """
    try:
        return chat_turn.start_turn(
            conn,
            owner,
            question=question,
            conversation_id=conversation_id,
            now=utc_now(),
        )
    except sqlite3.Error as exc:
        raise RecordingFailed(f"会话写不进去，这次对话没有记录：{exc}") from exc


def execute_turn(
    conn: sqlite3.Connection,
    owner: Owner,
    deps: Deps,
    *,
    question: str,
    conversation_id: str,
    chat: object,
    model_name: str,
) -> AskResult:
    """③④ 两步：作答 + 落回答。同步与异步的**同一段代码**。"""
    recorder = Recorder(conn, user_id=owner.user_id, tenant_id=owner.tenant_id)
    try:
        result = runner.ask(
            conn,
            question,
            chat=cast("ChatClient", chat),
            embedder=deps.embedder,
            recorder=recorder,
            # 检索的团队范围来自**这次请求的身份**，不是配置里的单租户常量。
            # 这一行就是 I-V2 的落点：`runner.ask` 早就有这个参数，
            # 而在此之前从来没有人传过它——签名对了但没人传，是这条判据最典型的坑。
            tenant_id=owner.tenant_id,
            refuse_margin=deps.settings.refuse_margin,
            sufficient_distance=deps.settings.sufficient_distance,
            coverage_threshold=deps.settings.coverage_threshold,
            model_name=model_name,
            # 两段写的第一段：run 一起来就先占一行空回答（在生成之前，C-V11）。
            on_started=partial(_save_placeholder, conn, owner, conversation_id=conversation_id),
        )
    except Exception:
        _save_failed_answer(conn, owner, conversation_id=conversation_id, recorder=recorder)
        raise

    # ④ 正文 + 快照 + run_id，一个事务
    chat_writer.save_answer(
        conn,
        owner,
        conversation_id=conversation_id,
        run_id=result.run_id,
        answer=result.answer,
        evidence=result.evidence,
        report=result.report,
        now=utc_now(),
    )
    return result


def _save_placeholder(
    conn: sqlite3.Connection, owner: Owner, run_id: str, *, conversation_id: str
) -> None:
    """run 一起来就占一行空回答（C-V11 的第一段）。

    返回值丢掉：补全时按 `run_id` 找回同一行，这正是"在原行上补、不另插一行"的保证。
    回调签名是 `Callable[[str], None]`——lambda 返回 int 会污染"回调不返回值"。
    """
    chat_writer.begin_answer(
        conn, owner, conversation_id=conversation_id, run_id=run_id, now=utc_now()
    )


def _save_failed_answer(
    conn: sqlite3.Connection,
    owner: Owner,
    *,
    conversation_id: str,
    recorder: Recorder,
) -> None:
    """失败那一轮也留一行 assistant：正文空、`run_id` 指向那次失败的运行。

    没有它，"它为什么没答上来"在历史里就是一片空白。run 都没起来
    （`recorder.start` 就失败）时直接返回：写一行指向不存在的 run 会被外键拒绝。
    """
    try:
        run_id = recorder.run_id
    except RuntimeError:
        return

    try:
        chat_writer.save_answer(
            conn,
            owner,
            conversation_id=conversation_id,
            run_id=run_id,
            answer="",
            # 检索或模型在返回结果之前就失败了，没有证据也没有卡片
            evidence=None,
            report=None,
            now=utc_now(),
        )
    except Exception:
        # 不覆盖原始异常：调用方要看到真正的失败原因。这里只留日志——
        # 它说明历史里少了一条挂点，是运维要知道的事。
        logger.exception("失败轮次的回答行没写进去：conversation_id=%s", conversation_id)
