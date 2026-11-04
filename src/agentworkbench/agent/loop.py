"""执行控制：迭代预算、run 级墙钟超时、重复调用与用量守卫。

「跑满迭代」不是失败：框架会清空工具逼模型用文本收口然后正常返回，
不主动问守卫，就和「答完了」区分不开。

来源：C# `AgentLoop/`
服务于：REQ-13 / REQ-16
"""

from __future__ import annotations
