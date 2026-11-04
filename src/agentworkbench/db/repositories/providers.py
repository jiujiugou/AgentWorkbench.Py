"""供应商仓储：供应商与模型两张表。

`is_active` 需要库层唯一约束（C# 版只有应用层事务，并发或直接 SQL 能造出两个激活供应商）。

**本层只管存储与行级判别，不做加解密**：`api_key_encrypted` 进出都是密文，
明文↔密文的转换在 api 装配层（deps.py）——db 与 llm 同层、彼此不许 import，
而"解出来的钥匙交给哪个客户端"本来就该由装配层拼。

来源：C# `Persistence/ProviderRepository.cs`
服务于：REQ-22 / S-V6 ~ S-V9
"""

from __future__ import annotations

import sqlite3
from typing import cast

from agentworkbench.core.errors import AppError, ErrorCode


def insert_provider(
    conn: sqlite3.Connection,
    *,
    name: str,
    base_url: str,
    api_key_encrypted: str,
    now: str,
) -> int:
    """新建供应商。`api_key_encrypted` 必须是**调用方加密后的密文**（加密在装配层）。"""
    cur = conn.execute(
        "insert into providers"
        " (name, base_url, api_key_encrypted, is_active, created_at, updated_at)"
        " values (?, ?, ?, 0, ?, ?) returning id",
        (name, base_url, api_key_encrypted, now, now),
    )
    # insert ... returning 必然有一行；None 只在类型系统的疑心里存在
    return int(cur.fetchone()["id"])


def list_providers_with_models(
    conn: sqlite3.Connection,
) -> list[tuple[sqlite3.Row, list[sqlite3.Row]]]:
    """全部供应商 + 各自的模型列表。

    返回密文列——读接口的**白名单 DTO 在路由层逐字段挑**，本层给全行
    是为了让"少给一个字段"这类事只发生在 DTO 一处，而不是在这里猜调用方要什么。
    """
    providers = cast(
        list[sqlite3.Row],
        conn.execute("select * from providers order by id").fetchall(),
    )
    models = cast(
        list[sqlite3.Row],
        conn.execute("select * from provider_models order by id").fetchall(),
    )
    by_provider: dict[int, list[sqlite3.Row]] = {}
    for model in models:
        by_provider.setdefault(int(model["provider_id"]), []).append(model)
    return [(provider, by_provider.get(int(provider["id"]), [])) for provider in providers]


def set_active(conn: sqlite3.Connection, provider_id: int, now: str) -> bool:
    """激活一个供应商：**先全表清 0 再置 1，一个事务**。

    终态由 `ux_providers_active`（全表级部分唯一索引）兜底——事务保证切换
    过程不出现"两行都是 1"的中间态被并发读看到，索引保证并发或手工 SQL
    也造不出两行激活（S-V7 的负控就是绕过这层直接打 SQL）。

    目标不存在时抛错回滚：清 0 已经执行，不能留下"全部失活、谁也没激活"的库。
    """
    with conn:
        conn.execute(
            "update providers set is_active = 0, updated_at = ? where is_active = 1", (now,)
        )
        cur = conn.execute(
            "update providers set is_active = 1, updated_at = ? where id = ?", (now, provider_id)
        )
        if cur.rowcount == 0:
            raise LookupError(f"provider {provider_id} 不存在")
    return True


def insert_model(
    conn: sqlite3.Connection,
    *,
    provider_id: int,
    model: str,
    is_default: bool,
    now: str,
) -> int:
    """给供应商加模型。同 (provider_id, model) 重复由 UNIQUE 拒绝（S-V8）。"""
    cur = conn.execute(
        "insert into provider_models (provider_id, model, is_default, created_at)"
        " values (?, ?, ?, ?) returning id",
        (provider_id, model, int(is_default), now),
    )
    return int(cur.fetchone()["id"])


def set_default_model(conn: sqlite3.Connection, provider_id: int, model_id: int, now: str) -> bool:
    """把某个模型设为该供应商的默认：先清该供应商的全部默认再置这一行。

    目标不存在或不属于这个供应商时抛错回滚——**不许用别人的模型行**
    给自己的供应商设默认，跨 provider 的行得查不出来才对。
    """
    with conn:
        conn.execute(
            "update provider_models set is_default = 0 where provider_id = ?", (provider_id,)
        )
        cur = conn.execute(
            "update provider_models set is_default = 1 where id = ? and provider_id = ?",
            (model_id, provider_id),
        )
        if cur.rowcount == 0:
            raise LookupError(f"model {model_id} 不属于 provider {provider_id}")
    return True


def resolve_active_row(conn: sqlite3.Connection) -> sqlite3.Row:
    """激活供应商 + 其默认模型的那一行（`api_key_encrypted` 仍是密文）。

    四种未配置形态各自报错（S-V9），message 指到该改的地方：
    没有激活供应商 / 有激活但没配默认模型 / 缺 base_url / **缺 api_key**。

    最后一种是这一格修掉的现存违规：llm 层旧实现把空 key 兜底成占位串
    "not-required" 继续答——"忘了配钥匙"被伪装成"答了但答错"，而后者没有任何
    东西会红。本地端点确实可能不校验密钥，但那也要**显式表明没钥匙**（配置里
    留空 + 界面显示"未设密钥"），不是在解析层偷偷塞一个串。
    """
    row = conn.execute(
        "select p.id, p.name, p.base_url, p.api_key_encrypted, m.id as model_id, m.model"
        " from providers p join provider_models m on m.provider_id = p.id"
        " where p.is_active = 1 and m.is_default = 1"
        " order by p.id, m.id limit 1"
    ).fetchone()
    if row is None:
        # 区分两种成因：连激活供应商都没有，还是有但没配默认模型
        active = conn.execute("select id, name from providers where is_active = 1").fetchall()
        if not active:
            raise AppError(
                ErrorCode.MODEL_NOT_CONFIGURED,
                "没有激活的模型供应商：请在供应商设置里启用一个端点（providers.is_active）。",
            )
        names = ", ".join(str(r["name"]) for r in active)
        raise AppError(
            ErrorCode.MODEL_NOT_CONFIGURED,
            f"激活供应商（{names}）没有标记默认的模型：请在模型列表里选一个默认（provider_models.is_default）。",
        )
    if not row["base_url"]:
        raise AppError(
            ErrorCode.MODEL_NOT_CONFIGURED,
            f"激活供应商 {row['name']} 缺少 base_url：无法确定请求打向哪里。",
        )
    if not row["api_key_encrypted"]:
        raise AppError(
            ErrorCode.MODEL_NOT_CONFIGURED,
            f"激活供应商 {row['name']} 没有配置 api_key：请在供应商设置里填写凭据。",
        )
    return cast(sqlite3.Row, row)
