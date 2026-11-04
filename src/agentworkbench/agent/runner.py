"""一次知识问答 run 的编排门面：四段流水线（F3 §1.2，预检索形态）。

    ① 检索：一次，前五块（系统预检索，不等模型决定查不查——A-02 反证）
    ② 判定：四值 + 原因；「资料不足」→ 终止（③④ 不执行）
    ③ 生成：只能引用本次编号 [1..n]
    ④ 引用校验：一级范围 → 二级支持 → 报告给界面（不喂下一步）

证据只取一次（§1.6）：retrieve 的结果装填成 Evidence 后原样分发给
判定、模型、引用校验三处——本函数里检索只发生一次，没有第二个入口。

预检索把两件事变成结构事实：证据一定在手（G5 的 tools ≥ 1 由「检索必调一次」保证，
不是约定）；[n] 不依赖模型主动（编号在装填时已固定）。
它不解决「模型用了证据」——那靠 ④ 的校验报告。

**记录在本函数内做，不交给调用方**（F4 §1.9 t0~t8）。这一条是改过的判断，原稿
写的是"纯编排、落库由调用方执行"。改的原因是 F4 的反面教材：C# 的记录机制其实
齐的，`agent_runs` 有、步骤有、状态有，缺的是「还有没有别的路」——
`POST /api/knowledge/ask` 走独立链路、不留 run，而用户实际在用的就是它。
只要"记录"是调用方的一个可选动作，就一定存在一条路没做，而且当时无人察觉。

所以 `recorder` 是**必传参数，没有默认值**：签名上就凑不出一次不留痕的调用。
拒答时那份命中也是证据这件事顺带解决了——检索步在判定之前就写好了。

来源：C# `AgentHarness/AgentHarnessService.cs`（形态差异见 instructions.py docstring）
服务于：REQ-07 / 08 / 09、G5 / G6 / G10、A-V7 / A-V10 / A-V11
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from agentworkbench.agent.instructions import (
    ANSWER_WITH_CITATIONS,
    PROMPT_VERSION,
    build_user_prompt,
)
from agentworkbench.agent.packing import pack
from agentworkbench.core.errors import AppError, ErrorCode
from agentworkbench.core.judgment import (
    Evidence,
    Judgment,
    JudgmentValue,
    SufficiencyOptions,
)
from agentworkbench.core.runs import RunReasonCode, RunStatus, StepKind
from agentworkbench.guard.citation import CitationReport, verify
from agentworkbench.guard.sufficiency import judge
from agentworkbench.guard.veto import SEARCH_KNOWLEDGE_TOOL, inspect
from agentworkbench.llm.client import ChatClient
from agentworkbench.rag.retriever import RetrievalHit, retrieve
from agentworkbench.trace.recorder import Recorder, describe_error


class QueryEmbedder(Protocol):
    """查询向量化端口。与 rag.retriever 的端口同签名（结构化匹配，HashEmbedder 直接满足）。"""

    def __call__(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class AskResult:
    """一次问答的完整产出：无论拒答还是回答，证据都在（拒答时那份命中也是证据）。"""

    run_id: str
    question: str
    status: RunStatus  # SUCCEEDED（已回答/依据较弱/未判定放行）或 REFUSED
    reason: RunReasonCode | None  # REFUSED 时为 KNOWLEDGE_INSUFFICIENT
    verdict: JudgmentValue
    answer: str  # 放行 = 清洗后正文（越界标注已摘）；拒答 = 固定文案
    report: CitationReport | None  # 拒答时 None（没有正文可校验）
    evidence: Evidence


# 错误码 → 原因码。同一件事不许两处都写，所以这是唯一的映射点：
# run 行上的 `reason` 只能从这里来，调用方不许自己拼一个字符串。
_FAILURE_REASONS: dict[ErrorCode, RunReasonCode] = {
    ErrorCode.MODEL_TIMEOUT: RunReasonCode.MODEL_TIMEOUT,
    ErrorCode.MODEL_UNAVAILABLE: RunReasonCode.MODEL_UNAVAILABLE,
    ErrorCode.RUN_TIMEOUT: RunReasonCode.RUN_TIMEOUT,
}


def failure_reason(exc: BaseException) -> RunReasonCode:
    """把异常翻成原因码。认不出的异常一律 `unhandled_error`，不猜。"""
    if isinstance(exc, AppError):
        return _FAILURE_REASONS.get(exc.code, RunReasonCode.UNHANDLED_ERROR)
    return RunReasonCode.UNHANDLED_ERROR


def usage_of(chat: ChatClient) -> tuple[int | None, int | None]:
    """取这次调用的用量。**取不到就是 `(None, None)`，不是 `(0, 0)`**（A-V10）。

    `ChatClient` 是只有 `complete` 的 Protocol，所以这里 duck-typing 读
    `last_usage`。假客户端通常没有这个属性——那正好等价于「供应商没回 usage」，
    也正是要验的形态。0 是「用了零个 token」这个事实，None 是「不知道」，
    两者混同之后，账单对不上时没人能查清是哪一边错了。
    """
    raw = getattr(chat, "last_usage", None)
    if not isinstance(raw, dict):
        return None, None
    return raw.get("prompt_tokens"), raw.get("completion_tokens")


def evidence_digest(hits: list[RetrievalHit]) -> str:
    """证据步的内容：命中数 + 每条的出处与距离。

    不存正文：正文在 `chunks` 里，而 `chunks` 会被重解析换掉（id 全变）。
    记录里留 `document_id + 页码 + 距离`，才是「当时依据的是哪几段、
    模型看到的是什么距离」。存一份正文快照反而会让人以为那就是现在的库。

    距离为 `None`（只走了词面路）时 JSON 里是 `null`，不是 0——0 表示
    「完全匹配」，那是另一个意思。
    """
    return json.dumps(
        {
            "hits": len(hits),
            "top": [
                {
                    "chunk_id": h.chunk_id,
                    "document_id": h.document_id,
                    "seq": h.seq,
                    "page_from": h.page_from,
                    "page_to": h.page_to,
                    "vector_distance": h.vector_distance,
                }
                for h in hits
            ],
        },
        ensure_ascii=False,
    )


def ask(
    conn: sqlite3.Connection,
    question: str,
    *,
    chat: ChatClient,
    embedder: QueryEmbedder | None,
    recorder: Recorder,
    tenant_id: str,
    refuse_margin: float,
    sufficient_distance: float,
    model_name: str | None = None,
    coverage_threshold: float | None = None,
    top_k: int = 5,
    on_started: Callable[[str], None] | None = None,
) -> AskResult:
    """跑一次完整问答，并留下一次 run。

    `recorder` 必传：这个函数就是「一次作答」本身，所以它必须在 run 里进行。
    阈值由部署配置传入（空阈值启动失败是 G15 的事，不是这里的默认值）。
    `tenant_id` 同样必传且**没有默认值**：它是检索的团队范围，默认值等于
    「忘传就检索全部」——而那不会报错，只会让答案里出现别人的资料（`I-V2`）。

    `on_started` 是**注入点，不是依赖**：run 一建好、流水线还没开始跑时被调用，
    参数是 `run_id`。会话层用它先占一行空回答（C-V11），而本层不认识会话——
    `agent` 不 import `chat`，这个回调让调用方自己决定"开跑那一刻还要做什么"。
    它抛异常时这次 run 会被收口成 `failed`（调用点放在下面那个 `try` 里），
    不会停在 `running` 冒充"进程被杀"。
    """
    run_id = recorder.start(question=question, model=model_name, prompt_version=PROMPT_VERSION)

    # 用量在闭包里被赋值。用闭包而不是把参数再列一遍：列两遍的签名必然漂移，
    # 而这里漂移的后果（少传一个阈值）不会报错，只会让判定悄悄用了默认值。
    usage: tuple[int | None, int | None] = (None, None)

    def pipeline() -> AskResult:
        nonlocal usage

        # ⓪ 占位：run 已经起来了，先让这次运行有一条历史挂点。放在 try 内的
        #    第一句——占位写不进去时这次 run 应当收口成 failed，而不是留在
        #    `running` 上，让事后的人分不清"还在跑"和"早就死了"。
        if on_started is not None:
            on_started(run_id)

        # ① 检索：必调一次。tools ≥ 1 由这一行保证（G5）。
        with recorder.step(StepKind.RETRIEVAL) as step:
            hits: list[RetrievalHit] = retrieve(
                conn, question, embedder=embedder, tenant_id=tenant_id, top_k=top_k
            )
            step.output = evidence_digest(hits)
        # ② 判定 + 装填：同一份结果，判定写进证据（D-03）。不成步——
        #   它是检索的产物，不是另一次调用。
        judgment: Judgment = judge(
            [h.vector_distance for h in hits],
            SufficiencyOptions(
                refuse_margin=refuse_margin, sufficient_distance=sufficient_distance
            ),
        )
        evidence = pack(hits, judgment)

        # 否决门：判不足 → 终止，模型只被请求 0 次（G10）。
        # 这里不成步：没有产生任何调用，给否决造一行就是伪造步骤（A-V7）。
        stop = inspect(SEARCH_KNOWLEDGE_TOOL, evidence)
        if stop is not None:
            return AskResult(
                run_id=run_id,
                question=question,
                status=RunStatus.REFUSED,
                reason=stop.code,
                verdict=judgment.verdict,
                answer=stop.message,
                report=None,
                evidence=evidence,
            )

        # ③ 生成：证据 + 编号进 prompt（预检索形态，模型没有工具可调）
        with recorder.step(StepKind.ASSISTANT) as step:
            answer = chat.complete(
                ANSWER_WITH_CITATIONS,
                build_user_prompt(question, evidence.hits),
            )
            step.output = answer
            step.model = model_name
            usage = usage_of(chat)
            step.prompt_tokens, step.completion_tokens = usage

        # ④ 引用校验：一级范围 + 二级支持；cleaned_answer 是给界面的正文。
        # 不成步：后处理，不是调用；结果落 run 的两个计数列。
        report = verify(answer, evidence.hits, coverage_threshold)

        return AskResult(
            run_id=run_id,
            question=question,
            status=RunStatus.SUCCEEDED,
            reason=None,
            verdict=judgment.verdict,
            answer=report.cleaned_answer,
            report=report,
            evidence=evidence,
        )

    try:
        result = pipeline()
    except Exception as exc:
        # 失败也要收口。停在 `running` 的 run 与「进程被杀」长得一样，
        # 事后分不清是还在跑还是早就死了。
        # 注意不捕获 BaseException：KeyboardInterrupt / 进程被杀的形态就是
        # 停在 running，那正是 A-V5 要的证据，不能在这里"帮忙"收口。
        recorder.finish(
            RunStatus.FAILED,
            reason=failure_reason(exc),
            error=describe_error(exc),
        )
        raise

    recorder.finish(
        result.status,
        reason=result.reason,
        prompt_tokens=usage[0],
        completion_tokens=usage[1],
        # 拒答时没有正文可校验 → NULL，不是 0。0 是「校验过、零处问题」
        citation_out_of_range=None if result.report is None else result.report.out_of_range_count,
        citation_unsupported=None if result.report is None else result.report.unsupported_count,
    )
    return result
