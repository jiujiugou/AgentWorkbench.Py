"""平台状态仓储：`system_state` 键值表。

目前只放 embedding 的两条状态——它是「索引是哪个模型建的」这个事实的唯一
落脚点。放全局键值表而不是给 `documents` 加列：这是**全局**状态，不属于
任何一份文档；也不是配置文件的活（配置在环境变量里，而这个必须随库走——
把库拷到另一台机器，索引的模型必须跟着走）。

**stale 是位，不是状态机**（`pyproject.toml` 的 state_machine 登记写的是
ready/stale 两个值）：`embedding.model`（想要的）与 `embedding.synced`
（索引实际用的）两个值不同 = 待重建。没有"正在重建"这种中间态——
重建是 F2 的重活，做完才写 synced，中途失败就是没做完。

**检索层只读不写。** stale 时检索必须报错而不是顺手重建：把一次重解析的
重活藏在一次检索里，用户等 30 秒拿到答案，而失败时什么都没说（S-V10 的负控
点名的正是这一形态——返回空列表与"库里没有资料"不可区分）。

服务于：REQ-22⑤ / S-V10
"""

from __future__ import annotations

import sqlite3

EMBEDDING_MODEL_KEY = "embedding.model"
"""想要的模型（设置接口写入）。"""

EMBEDDING_SYNCED_KEY = "embedding.synced"
"""索引实际用的模型（重建完成时写入）。"""

UNCONFIGURED = "(未配置)"
"""第一次配置模型时的"改之前"值。

为什么不是空串或干脆不写：空串与"没配过"不可区分，而「从来没有配置过 vs
配置过又被清空」在排障时是两件事。字面量写在库里，查的人一眼看懂。
"""


def get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("select value from system_state where key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def put(conn: sqlite3.Connection, key: str, value: str, *, now: str) -> None:
    """写一个键。**调用方负责事务**（与仓储层其他函数一致：这里不自开 `with conn`）。"""
    conn.execute(
        "insert into system_state (key, value, updated_at) values (?, ?, ?)"
        " on conflict(key) do update set value = excluded.value, updated_at = excluded.updated_at",
        (key, value, now),
    )


def embedding_state(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """(想要的, 索引实际用的)。两个都可能为 None——那就是"从没配过"。"""
    return get(conn, EMBEDDING_MODEL_KEY), get(conn, EMBEDDING_SYNCED_KEY)


def set_embedding_model(conn: sqlite3.Connection, model: str, *, now: str) -> None:
    """改配置：写『想要的』，并**把改之前的值固化成『索引实际用的』**。

    固化这一步是关键：不写它，改完配置后 `synced` 仍是 None，`stale` 判不出来，
    检索照常返回——那正是 S-V10 要消灭的形态（症状与"库里没资料"一样）。
    """
    with conn:
        wanted, synced = embedding_state(conn)
        if synced is None:
            put(conn, EMBEDDING_SYNCED_KEY, wanted if wanted is not None else UNCONFIGURED, now=now)
        put(conn, EMBEDDING_MODEL_KEY, model, now=now)


def mark_index_synced(conn: sqlite3.Connection, *, now: str) -> None:
    """重建完成：索引实际用的 = 现在想要的。**只能由重建流程调用**。

    重建（F2 的全量 reparse）还没有端点；现在的调用方是测试与
    `index_sync` 状态机的人工收尾。端点建起来之后，这里加一行"必须由它调"。
    """
    wanted, _ = embedding_state(conn)
    with conn:
        put(conn, EMBEDDING_SYNCED_KEY, wanted if wanted is not None else UNCONFIGURED, now=now)


def index_is_stale(conn: sqlite3.Connection) -> bool:
    """两个值都存在且不同 = 待重建。

    任一缺失 → False：从没配置过模型，就没有"索引是哪个模型建的"这回事，
    未配置不等于脏（老库、评测库、单模型部署都走这条）。
    """
    wanted, synced = embedding_state(conn)
    if wanted is None or synced is None:
        return False
    return wanted != synced
