"""向量化：把片段变成向量，失败必须能回到调用方。

只建了全文索引和全部建成，在系统里不能长得一样——
所以失败要作为一个返回值往上走，不是打条日志就算。

**当前只有占位实现**。`HashEmbedder` 用 sha256 派生确定性向量：
维度与可重复性是真的，语义相似度是假的（同义词不会靠近）。
它存在的理由：让"上传 → 解析 → 切块 → 三处索引对齐"这条链能端到端跑通，
把解析层与检索层的验证从"向量还没接"里解耦出来。

**换真模型必须重建索引**：`vec0` 的维度在建表时固定，且旧向量与新模型的
向量空间不可比——混用等于检索静默降级，不会报错。

来源：C# `Knowledge.Local/Embedding/`
服务于：REQ-06（只有处理成功才能标可检索）
"""

from __future__ import annotations

import hashlib

from agentworkbench.core.vectors import EMBEDDING_DIM, normalize


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
