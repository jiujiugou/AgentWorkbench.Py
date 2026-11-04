"""会话与消息记录。

一条回答实际由三块组成：正文、引用、那次运行。三者必须能对上，
否则「可回放」在用户侧不成立。

来源：C# `Domain/Conversations/ConversationRecords.cs`、`Domain/ChatTurn.cs`
服务于：REQ-04 / REQ-21（F5）
"""

from __future__ import annotations
