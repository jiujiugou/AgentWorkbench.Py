"""拒答：终止原因自带要显示给用户的那句话。

此时模型这条路已经断了——它一个字都没产出，答复只能从这里来，
否则界面是一片空白，或半句「我先查一下」。

同时提供独立状态与归类码，别把拒答记成「已回答」。

来源：C# `Knowledge.Local/KnowledgeRefusal.cs`
服务于：REQ-09（不编三 / 不编四 / 不编七）
"""

from __future__ import annotations
