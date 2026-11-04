"""模型供应商与可用模型记录。

平台级配置：同一时刻只有一个激活供应商、每个供应商只有一个默认模型。
凭据不得回传给任何客户端。

来源：C# `Domain/Providers/ProviderRecords.cs`
服务于：REQ-22（F6）
"""

from __future__ import annotations
