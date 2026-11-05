"""不变量：向量化客户端的四处边界。

被测的是**协议之上的那层语义**，不是 HTTP 本身：

1. **批内保序**。439 个片段按 32 一批发，最后拼回来的顺序必须与入参一致。
   顺序错一位，向量就挂到别人的片段上，检索命中的是"隔壁那段"——
   而检索有结果、引用也有内容，看不出错。
2. **归一化**。写入侧与查询侧口径不一致时，同一个含义会得到长度不同的两个向量。
   `vec0` 用 cosine 时这不致命，但一旦有人把距离度量换成 L2 就变成静默降级。
3. **维度不符是配置问题，不是故障**。上游给 768 维而索引是 1024 维时，
   重试一万次都一样——必须报不可重试的 `model_not_configured`，且文案指出
   「要改哪个常量、要不要重建索引」。
4. **不回显上游原文**（S-V5）。上游 4xx 的响应体会带它自己的内部信息，
   拼进 `AppError.message` 等于原样转发给用户。

注入方式：`httpx.MockTransport`，不打真网络，也不 mock 掉被测对象。

改坏这里会怎样：把批次的拼接顺序改成 `reverse()`（或先给向量排序再返回），
`test_batches_keep_the_input_order` 立刻红——而线上表现为"检索命中的是隔壁那段"，
有结果、有引用、看不出错。把维度校验从"报错"改成"截断到 dim"，
`test_wrong_dimension_is_a_configuration_error_not_a_retry` 红；
那条守的是"上游换模型"这件事必须以配置错误收场，而不是让半个向量混进索引。
"""

from __future__ import annotations

import json

import httpx
import pytest

from agentworkbench.core.errors import AppError, ErrorCode
from agentworkbench.core.vectors import EMBEDDING_DIM, normalize
from agentworkbench.llm.embeddings import EmbeddingEndpoint, HashEmbedder, OpenAIEmbedder

pytestmark = pytest.mark.invariant

ENDPOINT = EmbeddingEndpoint(
    base_url="http://10.0.20.5:9999/v1", api_key="sk-test", model="BAAI/bge-m3"
)


def _embedder(handler: object, *, batch_size: int = 32) -> OpenAIEmbedder:
    return OpenAIEmbedder(
        ENDPOINT,
        batch_size=batch_size,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


def _vector(slot: int) -> list[float]:
    """一个可辨识的向量：第 `slot` 位是 1，其余是 0。

    用 one-hot 而不是"首元素当种子"：向量归一化会把首元素按长度缩放，
    1 与 2 这类小种子缩放后几乎重合，断言就退化成"看谁都是 1"。
    one-hot 归一化后仍是同一个 one-hot，位置就是可读的标记。
    """
    vector = [0.0] * EMBEDDING_DIM
    vector[slot] = 1.0
    return vector


def test_batches_keep_the_input_order() -> None:
    """3 批发出去，拼回来必须与入参逐条对应（长度与首元素都能看出来）。"""
    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "BAAI/bge-m3"
        batch = payload["input"]
        seen.append(batch)
        # 每条文本按"第几批-第几条"编个标记位回给它，用来验证顺序
        data = [{"embedding": _vector(len(seen) * 10 + index)} for index in range(len(batch))]
        return httpx.Response(200, json={"data": data})

    texts = [f"片段{i}" for i in range(5)]
    vectors = _embedder(handler, batch_size=2).embed(texts)

    assert [len(batch) for batch in seen] == [2, 2, 1], "批次切分不对"
    assert len(vectors) == len(texts)
    # 第 n 批第 m 条的标记位是 n*10+m，顺序错一位立刻看得出来
    assert [vector.index(max(vector)) for vector in vectors] == [10, 11, 20, 21, 30]


def test_vectors_are_normalized() -> None:
    """上游给的是未归一的长向量，落库前必须变成单位向量。"""

    def handler(request: httpx.Request) -> httpx.Response:
        count = len(json.loads(request.content)["input"])
        return httpx.Response(200, json={"data": [{"embedding": [3.0] * EMBEDDING_DIM}] * count})

    vector = _embedder(handler).embed(["x"])[0]
    assert len(vector) == EMBEDDING_DIM
    assert abs(sum(value * value for value in vector) - 1.0) < 1e-6
    assert vector == normalize([3.0] * EMBEDDING_DIM)


def test_wrong_dimension_is_a_configuration_error_not_a_retry() -> None:
    """上游 768 维、索引 1024 维 → 不可重试，且文案说清该做什么。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [0.1] * 768}]})

    with pytest.raises(AppError) as exc_info:
        _embedder(handler).embed(["x"])

    err = exc_info.value
    assert err.code is ErrorCode.MODEL_NOT_CONFIGURED
    assert err.spec.retryable is False, "维度不符标成可重试，用户会一直重试下去"
    assert "768" in err.message and str(EMBEDDING_DIM) in err.message
    assert "EMBEDDING_DIM" in err.message, "文案要指出改哪个常量"


def test_upstream_failures_do_not_echo_the_body() -> None:
    """上游 401 的响应体带内部信息 → 只进日志，不进用户可见文案。"""
    sentinel = "internal: key sk-leaked-9f3a quota=0 on node prod-7"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": sentinel}})

    with pytest.raises(AppError) as exc_info:
        _embedder(handler).embed(["x"])

    err = exc_info.value
    assert err.code is ErrorCode.MODEL_UNAVAILABLE
    assert err.spec.retryable is True, "上游 4xx/5xx 属于可重试的一侧"
    assert "sk-leaked" not in err.message
    assert "prod-7" not in err.message
    assert "401" in err.message, "至少要说清是哪一型失败"


def test_short_response_is_rejected_rather_than_padded() -> None:
    """上游少回一条 → 报错，不补空向量。

    补一条零向量的后果是：那条片段永远检索不到，而库里看起来"索引是全的"。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": _vector(1)}]})

    with pytest.raises(AppError) as exc_info:
        _embedder(handler).embed(["a", "b"])

    assert exc_info.value.code is ErrorCode.MODEL_UNAVAILABLE
    assert "1" in exc_info.value.message and "2" in exc_info.value.message


def test_empty_input_makes_no_request() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"data": []})

    assert _embedder(handler).embed([]) == []
    assert called is False, "空输入不该打上游"


def test_placeholder_and_remote_agree_on_shape() -> None:
    """两个实现的形状契约一致：同样的入参长度、同样的维度、都归一。

    这条守的是"换实现"这一动作：占位实现与真实实现如果形状不同，
    换上真模型的那一刻，写入路径会在别处以 AttributeError 的形式炸。
    """
    placeholder = HashEmbedder()
    remote = _embedder(
        lambda request: httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": _vector(index)}
                    for index in range(len(json.loads(request.content)["input"]))
                ]
            },
        )
    )
    texts = ["甲", "乙", "丙"]
    for embedder in (placeholder, remote):
        vectors = embedder(texts)
        assert len(vectors) == 3
        assert all(len(vector) == EMBEDDING_DIM for vector in vectors)
        assert all(abs(sum(v * v for v in vector) - 1.0) < 1e-6 for vector in vectors)
