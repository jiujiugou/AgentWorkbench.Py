"""不变量：模型客户端不伪造用量。

失效形态（用户会看到什么）：

**供应商没回 token，库里记成 0。** 看起来很干净——成本报表有数、平均值有数、
每一次调用都有"用量"。但那个 0 是造出来的：它同时意味着"这次只用了 0 个 token"。
于是账单对不上时，没人能查清是哪一边错了，因为数据里根本没有"不知道"这个状态。

这一条与 `test_recorder.py` 的 A-V10 是同一条链的两端：
客户端把 `None` 抹成 0，写入口再老实也救不回来——它收到的就是一个可信的 0。

改坏这个不变量会怎样：把 `usage.prompt_tokens` 改回 `usage.prompt_tokens or 0`
→ 下面第一条当场红（0 is not None）。

服务于：A-V10
"""

from __future__ import annotations

from typing import Any, cast

from openai import OpenAI

from agentworkbench.llm.client import OpenAIChatClient
from agentworkbench.llm.providers import ChatEndpoint

ENDPOINT = ChatEndpoint(
    provider_id=1,
    name="fake",
    base_url="http://localhost:1/v1",
    api_key="sk-test",
    model="fake-model",
)


def fake_client(*, usage: object, content: str = "答案") -> OpenAI:
    """最小 OpenAI 替身：只有 `chat.completions.create` 一条路。"""

    class _Completions:
        def create(self, **_kwargs: Any) -> Any:
            message = type("Message", (), {"content": content})()
            return type(
                "Response",
                (),
                {"usage": usage, "choices": [type("Choice", (), {"message": message})()]},
            )()

    class _Chat:
        completions = _Completions()

    return cast(OpenAI, type("Client", (), {"chat": _Chat()})())


def test_absent_usage_field_stays_none() -> None:
    """供应商回了 usage 对象，但某一项是 null → 必须是 None。"""
    usage = type(
        "Usage", (), {"prompt_tokens": None, "completion_tokens": 12, "total_tokens": None}
    )()
    client = OpenAIChatClient(ENDPOINT, client=fake_client(usage=usage))

    client.complete("系统", "问题")

    assert client.last_usage["prompt_tokens"] is None
    assert client.last_usage["completion_tokens"] == 12
    assert client.last_usage["total_tokens"] is None


def test_genuine_zero_is_kept_as_zero() -> None:
    """正控：真回了 0 就存 0。只有负控的话，「全写 None」也能过。"""
    usage = type("Usage", (), {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})()
    client = OpenAIChatClient(ENDPOINT, client=fake_client(usage=usage))

    client.complete("系统", "问题")

    assert client.last_usage == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def test_no_usage_object_means_empty_not_zeroes() -> None:
    """整次调用连 usage 都没回 → 空 dict。

    与「字段为 None」是两层：空 dict 说"这次调用没有用量信息"，
    字段 None 说"这一项没有"。两者都写 0 的话，三态压成一态。
    """
    client = OpenAIChatClient(ENDPOINT, client=fake_client(usage=None))

    client.complete("系统", "问题")

    assert client.last_usage == {}
