"""模型调用语义：单次请求的超时、重试、用量统计。

与「整条 run 的预算」是两件事，错误码必须分开：
`model_timeout` 与 `run_timeout` 合成一个码之后，
「模型慢」和「循环没收敛」就查不出来了。

`ChatClient` 是 Protocol：agent 编排只依赖它，测试注入假实现
（不打真 API），真实现是 `OpenAIChatClient`——供应商的原始失败
原样透出，不在这里吞（诊断层的位置，C# DiagnosticsChatClient 同理）。

来源：C# `ModelRuntime/`
服务于：REQ-11（记录）、REQ-18
"""

from __future__ import annotations

import logging
from typing import Protocol

from openai import OpenAI

from agentworkbench.core.errors import AppError, ErrorCode
from agentworkbench.llm.providers import ChatEndpoint, client_for

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60.0
"""单次请求的墙钟上限。模型慢不该拖死整轮——run 级预算另有守卫。"""


class ChatClient(Protocol):
    """一次补全的最小契约。多轮会话历史归 F5，这里只管「系统指令 + 问题 → 回答」。"""

    def complete(self, system: str, user: str) -> str: ...


class OpenAIChatClient:
    """openai SDK 实现。base_url 覆盖来自库里的供应商配置（见 providers.py）。"""

    def __init__(
        self,
        endpoint: ChatEndpoint,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: OpenAI | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._client = client if client is not None else client_for(endpoint)
        self._timeout = timeout
        self.last_usage: dict[str, int | None] = {}
        """最近一次调用的用量（prompt/completion/total）。REQ-11 记录的数据源。

        字段值可能是 `None`：供应商没回这一项，就是"不知道"，不是"用了 0 个"。
        这里曾经写 `usage.prompt_tokens or 0`，把两者抹成一个数——0 会被当成
        真实用量算进成本与平均值，**而且没有任何东西看起来不对**（A-V10）。
        空 dict 表示"整次调用没有 usage"（连对象都没回），与"字段为 None"是两层。
        """

    def complete(self, system: str, user: str) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self._endpoint.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                timeout=self._timeout,
            )
        except TimeoutError as exc:
            raise AppError(ErrorCode.MODEL_TIMEOUT, f"模型请求超时（{self._timeout}s）") from exc
        except Exception as exc:
            # 原文只进日志、不进用户文案（S-V5）：上游异常里可能有 base_url、
            # api_key、上游返回的内部信息。曾经把 `{exc}` 直接拼进 message——
            # 哨兵测试抓出来的现存泄露。文案说"哪家哪一型失败"即可，
            # name / model 是用户自己配的，不是内部细节。
            logger.exception(
                "模型调用失败 provider=%s model=%s", self._endpoint.name, self._endpoint.model
            )
            raise AppError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"模型调用失败（{self._endpoint.name}/{self._endpoint.model}）："
                "请检查供应商服务是否可用，详情见服务端日志。",
            ) from exc

        usage = response.usage
        if usage is not None:
            self.last_usage = {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            }

        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise AppError(
                ErrorCode.MODEL_UNAVAILABLE,
                "模型返回了空内容：可能被安全策略拦截，或端点异常。",
            )
        return content
