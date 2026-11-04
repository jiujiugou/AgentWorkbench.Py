"""索引新鲜度闸门：stale 时检索必须报错，不许返回空冒充「资料里没有」。

**为什么检查点放在检索入口而不是路由层。** 路由层只有今天这两条（`/search`
与 ask），而检索函数是所有调用方的公共入口——评测脚本、未来的批量任务、
下一个域接检索都会经过它。放在路由层等于"今天记得写、明天忘一处"；
放在这里，绕过它的唯一办法是不用检索（那也就没有这个风险）。

**为什么不是「顺手重建」。** 重建是 F2 的重活（全量 reparse），把它藏在一次
检索里：用户等半分钟才拿到一个可能是旧结果的答案，而失败时一个字都没有。
stale 的承诺是"明确说这库现在不能检索"，代价（去点重建）由用户决定何时付。

**为什么不返回空列表。** 空列表与"库里真的没有相关块"在响应里完全一样——
用户会去改问题、改阈值、重新上传，而真正该做的是重建索引。症状一样、
没人报错，正是 S-V10 的负控点名的形态。

服务于：REQ-22⑤ / S-V10
"""

from __future__ import annotations

import sqlite3

from agentworkbench.core.errors import AppError, ErrorCode
from agentworkbench.db.repositories import system_state


def ensure_index_fresh(conn: sqlite3.Connection) -> None:
    """stale → 503 `index_stale`（不可重试：重试还是同一个库）。"""
    if not system_state.index_is_stale(conn):
        return
    wanted, synced = system_state.embedding_state(conn)
    raise AppError(
        ErrorCode.INDEX_STALE,
        "embedding 模型已经改过（现在配置的是 "
        f"{wanted}，索引是用 {synced} 建的），索引需要重建后才能检索。",
    )
