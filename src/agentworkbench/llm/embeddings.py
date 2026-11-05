"""向量化：把片段变成向量，失败必须能回到调用方。

两种实现，选择在装配层（`api/deps.py`）：

- `OpenAIEmbedder` —— 生产实现，打 OpenAI 兼容的 `POST {base_url}/embeddings`。
  兼容面很宽（硅基流动 `BAAI/bge-m3`、智谱 `embedding-3`、OpenAI、one-api），
  所以这里不区分厂商，只要对方讲这个协议。
- `HashEmbedder` —— **占位实现**，sha256 派生确定性向量。维度与可重复性是真的，
  语义相似度是假的（同义词不会靠近）。它让"上传 → 解析 → 切块 → 三处索引对齐"
  这条链在还没有模型时能端到端跑通，把解析层与检索层的验证从"向量还没接"
  里解耦出来。**在生产里用它，检索等于随机取样**，所以装配层会为此落 WARNING。

**换模型必须重建索引**：`vec0` 的维度在建表时固定，且旧向量与新模型的向量空间
不可比——混用等于检索静默降级，不会报错。`system_state` 的 `embedding.synced`
记的就是"索引是哪个模型建的"，两者不同时检索报 `index_stale`。

失败的分类（调用方据此决定标哪个错误码）：
- 连不上 / 上游 4xx 5xx / 回的结构不对 → `MODEL_UNAVAILABLE`（可重试）
- 回的维度不等于 `EMBEDDING_DIM` → `MODEL_NOT_CONFIGURED`（不可重试，配置/迁移问题）

来源：C# `Knowledge.Local/Embedding/`（Ollama 与 OpenAI 兼容两实现）
服务于：REQ-06（只有处理成功才能标可检索）
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from agentworkbench.core.errors import AppError, ErrorCode
from agentworkbench.core.vectors import EMBEDDING_DIM, normalize

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingEndpoint:
    """一个向量化端点：打向哪家、用哪把钥匙、叫哪个模型。

    与 `llm/providers.py::ChatEndpoint` 分开而不是复用：那个带 `provider_id`
    与"激活"语义（聊天供应商来自库里的配置行），向量化是全局单例。
    两者唯一的共同点是"OpenAI 协议"，而协议一致不等于配置形态一致。
    """

    base_url: str
    api_key: str
    model: str


class HashEmbedder:
    """确定性占位嵌入。同一个文本永远得到同一个向量——评测的可重复性靠它。"""

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        if dim <= 0:
            raise ValueError(f"维度必须是正数：{dim}")
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """一批文本 → 一批单位向量。空输入返回空列表，不报错。"""
        return [self._one(text) for text in texts]

    def __call__(self, texts: list[str]) -> list[list[float]]:
        """让实例直接满足 `EmbedderPort`（结构化协议按 __call__ 匹配）。"""
        return self.embed(texts)

    def _one(self, text: str) -> list[float]:
        # sha256 每 32 字节一轮，循环填充到 dim 个 float（0~1 均匀分布）。
        raw: list[float] = []
        block = 0
        while len(raw) < self.dim:
            digest = hashlib.sha256(f"{text}#{block}".encode()).digest()
            raw.extend(byte / 255.0 for byte in digest)
            block += 1
        return normalize(raw[: self.dim])


class OpenAIEmbedder:
    """OpenAI 兼容 `/embeddings` 实现。

    三处刻意的选择：

    1. **按批发**。439 个片段一次塞进一个请求，上游会截断或超时；一批 32 个是
       各家都吃得下的量级。批内保序，所以返回的列表与入参一一对应。
    2. **自己归一化**。`vec0` 用的是 `distance_metric=cosine`，对余弦而言长度无关，
       但写入侧与查询侧若一个归一一个不归一，就会出现"同一个含义两个向量"的
       隐蔽偏差。这里统一归一，与 `HashEmbedder` 的口径一致。
    3. **错误文案不回显上游原文**（S-V5）。上游的 4xx 响应体会带它自己的内部信息
       （账号状态、内网地址），原样拼进 `AppError.message` 就是把它转发给用户。
       原文只进日志。
    """

    def __init__(
        self,
        endpoint: EmbeddingEndpoint,
        *,
        timeout: float = 20.0,
        batch_size: int = 32,
        transport: httpx.BaseTransport | None = None,
        dim: int = EMBEDDING_DIM,
    ) -> None:
        if not endpoint.base_url.strip() or not endpoint.model.strip():
            raise ValueError("向量化端点缺 base_url 或 model——半配置不该走到这里")
        if batch_size <= 0:
            raise ValueError(f"batch_size 必须是正数：{batch_size}")
        self._endpoint = endpoint
        self._timeout = timeout
        self._batch_size = batch_size
        self._transport = transport
        self.dim = dim

    @property
    def model(self) -> str:
        """索引要记住的模型名（写进 `system_state['embedding.synced']`）。"""
        return self._endpoint.model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(self._one_batch(texts[start : start + self._batch_size]))
        return vectors

    def __call__(self, texts: list[str]) -> list[list[float]]:
        """满足 `EmbedderPort`（结构化协议按 __call__ 匹配）。"""
        return self.embed(texts)

    def _one_batch(self, batch: list[str]) -> list[list[float]]:
        url = self._endpoint.base_url.rstrip("/") + "/embeddings"
        headers = {"content-type": "application/json"}
        if self._endpoint.api_key:
            headers["authorization"] = f"Bearer {self._endpoint.api_key}"
        payload = {"model": self._endpoint.model, "input": batch}

        try:
            with httpx.Client(
                timeout=self._timeout,
                transport=self._transport,
                # 不吃 HTTP(S)_PROXY：这台机器上配了代理时，httpx 会把请求送给代理，
                # 于是"向量化失败"的原因变成"代理不通"，与配置无关。
                trust_env=False,
            ) as client:
                response = client.post(url, json=payload, headers=headers)
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("向量化请求失败（%s）：%s", url, exc)
            raise AppError(
                ErrorCode.MODEL_UNAVAILABLE,
                "向量化服务连不上，请检查地址与网络后重试。",
            ) from exc

        if response.status_code != 200:
            logger.warning(
                "向量化上游返回 %s（%s）：%s",
                response.status_code,
                url,
                response.text[:500],
            )
            raise AppError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"向量化服务返回 HTTP {response.status_code}，稍后重试。",
            )

        try:
            body: Any = response.json()
            rows = body["data"]
            raw = [row["embedding"] for row in rows]
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("向量化响应结构不对（%s）：%s", url, response.text[:500])
            raise AppError(
                ErrorCode.MODEL_UNAVAILABLE,
                "向量化服务返回的内容无法解析，请检查 base_url 是否正确。",
            ) from exc

        if len(raw) != len(batch):
            raise AppError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"向量化服务返回 {len(raw)} 条，请求了 {len(batch)} 条，稍后重试。",
            )
        return [self._normalized(vector) for vector in raw]

    def _normalized(self, vector: Any) -> list[float]:
        """校验维度并归一化。

        维度对不上是**配置/迁移问题**，重试一万次也一样——所以报不可重试的
        `model_not_configured`，文案说清"要改哪个常量、要不要重建索引"。
        """
        if not isinstance(vector, list) or len(vector) != self.dim:
            actual = len(vector) if isinstance(vector, list) else type(vector).__name__
            raise AppError(
                ErrorCode.MODEL_NOT_CONFIGURED,
                f"向量维度是 {actual}，与索引的 {self.dim} 维不一致："
                f"模型「{self._endpoint.model}」不能配在这个库上。"
                "换模型要同时改 core/vectors.py 的 EMBEDDING_DIM 并重建索引。",
            )
        return normalize([float(value) for value in vector])
