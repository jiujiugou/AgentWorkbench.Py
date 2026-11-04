"""不变量：模型客户端的错误文案不回显上游异常（S-V5 的客户端一半）。

写这条测试时它**当场红**：`OpenAIChatClient.complete` 的 MODEL_UNAVAILABLE 分支
曾把 `{exc}` 原文拼进用户可见 message。上游的 APIStatusError 会带着响应体原文
——那是供应商侧的内部信息（内网地址、账号状态），拼进文案等于原样转发给用户。
修复后的形状：固定文案说清"哪家哪一型失败"，原文 `logger.exception` 留给日志。

注入方式：httpx.MockTransport 让 SDK 收到一个 500 + 带内部信息的响应体。
不打真网络，也不 mock 掉被测对象——被测的就是"异常从 SDK 冒出来之后
到 AppError 之间发生了什么"。

改坏这个不变量会怎样：把 `{exc}` 拼回 message → 当场红。
"""

from __future__ import annotations

import httpx
import pytest
from openai import OpenAI

from agentworkbench.core.errors import AppError, ErrorCode
from agentworkbench.llm.client import OpenAIChatClient
from agentworkbench.llm.providers import ChatEndpoint

pytestmark = pytest.mark.invariant

# 哨兵：模拟供应商 500 响应体里的内部信息（内网地址 + 内部标识）。
SENTINEL = "internal: listening on 10.0.20.5:11434, worker=prod-7"


def _client() -> OpenAIChatClient:
    endpoint = ChatEndpoint(
        provider_id=1,
        name="内网供应商",
        base_url="http://10.0.20.5:11434/v1",
        api_key="sk-test",
        model="m1",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # 上游 500，响应体带着它的内部细节——SDK 会把它装进 APIStatusError 的
        # message。之后是拼进文案还是只进日志，就是这条测试要守的界线。
        return httpx.Response(500, json={"error": {"message": SENTINEL}})

    sdk = OpenAI(
        api_key="sk-test",
        base_url=endpoint.base_url,
        # openai 新版把 httpx 类型别名为 httpx2（vendored 补丁），传入标准 Client 类型上等价
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
    )
    return OpenAIChatClient(endpoint, client=sdk)


def test_client_errors_do_not_echo_the_exception() -> None:
    with pytest.raises(AppError) as exc_info:
        _client().complete("系统指令", "问题")

    err = exc_info.value
    assert err.code is ErrorCode.MODEL_UNAVAILABLE
    # 响应体原文（含哨兵）不许进用户可见文案；文案里许有的是用户自己配的
    # name / model——那本来就在设置页上，不是泄露。
    assert SENTINEL not in err.message
    assert "10.0.20.5" not in err.message
    assert "prod-7" not in err.message
