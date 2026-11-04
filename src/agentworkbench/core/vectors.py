"""向量维度与归一化：一个数，两处用（写入与校验）。

放在 `core` 而不是 `db`，因为 `db` 与 `llm` 在分层里是**同一层、互不引用**。
维度写死两个地方，换模型时只改一处，另一处会在写索引时静默对不上——
`vec0` 的维度是建表时固定的，插错维度报错但不告诉你"应该是多少"。

来源：C# `Knowledge.Local/Embedding/EmbeddingOptions.cs`
服务于：REQ-06
"""

from __future__ import annotations

import math

# 建表时写进 `vec0` 的维度。改它 = 必须重建索引，不能原地迁移。
EMBEDDING_DIM = 1024


def normalize(vector: list[float]) -> list[float]:
    """L2 归一化。余弦距离在归一化之后等于点积，`vec0` 用 `distance=L2` 也能按余弦排序。"""
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]
