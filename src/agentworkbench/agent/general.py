"""通用对话的一次作答：不检索、不引用，但必须留一条 run（F7）。

与 `agent.runner.ask` 刻意分成两条链路。知识问答的每一轮都从检索开始，
通用对话如果复用那条链，就只有两种做法：要么先查一遍再假装没查（伪造步骤），
要么在同一个函数里按 mode 分叉（两套合同共用一段状态机）。两条都正是 F7
禁止的形态，所以这里只保留生成这一件事。

**记录仍在 `trace.recorder` 里，不在这里。** 本模块拿到 `Recorder`，负责
在真实模型调用前后留 step；run 的启动、终态、原因码都由 recorder 收口。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from agentworkbench.agent.instructions import (
    GENERAL_CHAT_SYSTEM,
    GENERAL_PROMPT_VERSION,
    build_general_user_prompt,
)
from agentworkbench.agent.runner import failure_reason, usage_of
from agentworkbench.core.runs import RunStatus, StepKind
from agentworkbench.guard.citation import strip_citations
from agentworkbench.llm.client import ChatClient
from agentworkbench.trace.recorder import Recorder, describe_error

HISTORY_MAX_MESSAGES = 12
HISTORY_MAX_CHARS = 6000
"""历史窗口。不是模型窗口的真实上限，只是防止旧会话把输入无限撑大。"""


@dataclass(frozen=True, slots=True)
class GeneralResult:
    run_id: str
    question: str
    status: RunStatus
    answer: str


def render_history(messages: Sequence[tuple[str, str]]) -> str:
    """把会话历史压成给模型看的文本，保留最近的消息。

    正序输出（旧到新）；超限时从最旧的丢起，而不是从最新的丢——当前问题
    另有一段，历史的作用是给上下文，不是复述全部聊天记录。
    """
    lines = [
        f"{'用户' if role == 'user' else '助手'}：{content}"
        for role, content in messages
        if content.strip()
    ]
    selected: list[str] = []
    used = 0
    for line in reversed(lines[-HISTORY_MAX_MESSAGES:]):
        if selected and used + len(line) > HISTORY_MAX_CHARS:
            break
        selected.append(line)
        used += len(line)
    return "\n".join(reversed(selected))


def chat_general(
    question: str,
    *,
    history: Sequence[tuple[str, str]],
    chat: ChatClient,
    recorder: Recorder,
    model_name: str,
    on_started: Callable[[str], None] | None = None,
) -> GeneralResult:
    """跑一次通用作答。只调用模型一次，只写一条 assistant 步骤。"""
    run_id = recorder.start(
        question=question,
        model=model_name,
        prompt_version=GENERAL_PROMPT_VERSION,
    )
    usage: tuple[int | None, int | None] = (None, None)

    def pipeline() -> GeneralResult:
        nonlocal usage
        if on_started is not None:
            on_started(run_id)

        with recorder.step(StepKind.ASSISTANT, input=question) as step:
            answer = chat.complete(
                GENERAL_CHAT_SYSTEM,
                build_general_user_prompt(render_history(history), question),
            )
            # 模型自己写出的 [1] 不是我们生成的引用，但它会被界面当成角标。
            # 通用对话没有证据可指，保留它等于给一句没有担保的话盖章。
            answer = strip_citations(answer).strip()
            step.output = answer
            step.model = model_name
            usage = usage_of(chat)
            step.prompt_tokens, step.completion_tokens = usage

        return GeneralResult(
            run_id=run_id,
            question=question,
            status=RunStatus.SUCCEEDED,
            answer=answer,
        )

    try:
        result = pipeline()
    except Exception as exc:
        recorder.finish(
            RunStatus.FAILED,
            reason=failure_reason(exc),
            error=describe_error(exc),
        )
        raise

    recorder.finish(
        RunStatus.SUCCEEDED,
        prompt_tokens=usage[0],
        completion_tokens=usage[1],
        # 通用对话没有引用校验：NULL 不是“校验过、零问题”。
        citation_out_of_range=None,
        citation_unsupported=None,
    )
    return result
