"""模型客户端：端点三元组 → OpenAI 兼容客户端。

**解析（选哪家、哪把钥匙、哪个模型）不在这层**——那要查 providers 两张表，
而 db 与 llm 同层、彼此不许 import。解析在 db 仓储（`resolve_active_row`，
负责四种未配置形态的显式报错）+ api 装配层（`deps.py`，负责解密与拼装），
本层只负责"给我三元组，我还你一个可复用的客户端"。

多供应商的实现方式与 C# `ChatClientFactory` 同构：不区分厂商，只要求
对方讲 OpenAI 协议，用 base_url 覆盖把 SDK 指向任意兼容 /v1 地址。
换供应商 = 换数据库里的配置行，上层代码零改动。

客户端按三元组（BaseUrl | ApiKey | Model）缓存：同一端点在进程内复用
同一个实例——凭据在构造时固化，三元组任一不同就是不同客户端。

来源：C# `Providers/ChatClientFactory.cs`
服务于：REQ-22
"""

from __future__ import annotations

from dataclasses import dataclass

from openai import OpenAI


@dataclass(frozen=True)
class ChatEndpoint:
    """一个可调用的端点：请求打向哪家、用哪把钥匙、叫哪个模型。"""

    provider_id: int
    name: str
    base_url: str
    api_key: str
    model: str

    @property
    def cache_key(self) -> str:
        return f"{self.base_url}|{self.api_key}|{self.model}"


_clients: dict[str, OpenAI] = {}


def client_for(endpoint: ChatEndpoint) -> OpenAI:
    """三元组缓存：同一端点在进程内复用同一个客户端（C# ConcurrentDictionary 同语义）。"""
    if endpoint.cache_key not in _clients:
        _clients[endpoint.cache_key] = OpenAI(
            api_key=endpoint.api_key,
            base_url=endpoint.base_url,
        )
    return _clients[endpoint.cache_key]
