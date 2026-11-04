"""存量归属绑定：把阶段 1 的行从一个租户挪到另一个。

## 为什么需要它

阶段 1 没有身份：每条数据写进 `tenant_id = settings.tenant_id`，默认 `"default"`。
引入 F1 之后，第一个管理员是从引导里建出来的，他的团队 id 是 `t-xxxx`，
于是**所有旧数据都不属于任何真实团队**——对所有人 404。

这不是 bug，是 `AUTH-06` 要的那一侧：宁可「数据看不见」（一查就发现），
也不要「所有人可见」（发现不了）。但用户传过的文件不能真的丢，
所以需要一个**一次性、可预演、可重跑**的绑定动作。

## 为什么不做成自动的

自动推断「这批 `default` 的行该归谁」需要一个默认值，而默认值意味着
**任何一个新建的团队都可能继承上一批数据**。这在多团队部署里是灾难，
而且它不会报错——只会让新团队一登录就看见别人的文件。

所以它是运维显式跑的一条命令，而且默认只预演（`apply=False`）：
先看「会发生什么」，再决定要不要加 `--apply`。

## 只改三张表

`documents` / `conversations` / `agent_runs` 是**带租户列的父表**；
`messages`、`message_citations`、`agent_steps`、`chunks`、`document_pages`
都没有租户列，它们跟着父行走。改父表就够了——多改一张就多一处可能改漏的地方。

`users` / `invites` / `identity_events` 不在这里：它们属于身份域，
「把某个账户挪到另一个团队」是另一个问题（那人手里的票据、他建过的邀请都还要重算），
不是一次数据搬迁能顺手解决的。

## 唯一约束是这里最真实的风险

`documents` 上有 `unique(tenant_id, sha256)`。如果目标团队里已经有同一个文件
（很常见：用户在阶段 1 传过一次，接入后又传了一次），直接 UPDATE 会撞唯一索引，
整批回滚——而回滚的报错只有一句 `UNIQUE constraint failed`，运维得自己猜是哪一份。
所以冲突**在写之前就查出来**，连文件名一起报，让人去决定留哪边。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

__all__ = ["DATA_TABLES", "BindReport", "BindingConflict", "bind"]

DATA_TABLES: tuple[str, ...] = ("documents", "conversations", "agent_runs")
"""要搬的三张父表。顺序固定：报告与日志按它排，重跑时两次输出可以直接 diff。"""


class BindingConflict(Exception):
    """目标团队里已有同 `sha256` 的文档：整批拒绝，不做「跳过冲突那几行」。

    跳过的版本看起来更友好，但它会让运维以为「跑完了」，而实际上一部分行还在旧租户里——
    那些行对所有人 404，且没有任何东西会提醒他。
    """

    def __init__(self, conflicts: tuple[tuple[str, str], ...]) -> None:
        super().__init__(f"目标团队里已有同一份文件：{len(conflicts)} 处")
        self.conflicts = conflicts


@dataclass(frozen=True)
class BindReport:
    """一次绑定的结果。**预演和真跑返回同一个形状**——两套结构迟早对不上，
    而「预演看到的」与「真跑的」不一致时，人只会相信自己想看的那一份。"""

    source: str
    target: str
    counts: dict[str, int]
    conflicts: tuple[tuple[str, str], ...]
    applied: bool

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _conflicts(
    conn: sqlite3.Connection, *, source: str, target: str
) -> tuple[tuple[str, str], ...]:
    """源租户里哪些文档在目标租户已有同 sha256。

    `sha256` 是文件内容的摘要，同一个文件传两次摘要相同——它不是「同一份文档」
    的严格定义（同名不同内容会算两份），但对唯一索引来说恰好是它。
    """
    rows = conn.execute(
        "select d.id, d.file_name from documents d"
        " where d.tenant_id = ? and exists ("
        "   select 1 from documents t where t.tenant_id = ? and t.sha256 = d.sha256"
        " )"
        " order by d.file_name",
        (source, target),
    ).fetchall()
    return tuple((str(row["id"]), str(row["file_name"])) for row in rows)


def bind(
    conn: sqlite3.Connection,
    *,
    source: str,
    target: str,
    apply: bool = False,
) -> BindReport:
    """把 `source` 租户下的三张表改到 `target`。

    `apply=False`（默认）只数行、查冲突，一个字节都不写。

    拒绝的三种情况都在写之前：源与目标相同（等于什么都没做，但会让人以为做了）、
    目标团队不存在（绑过去仍然对所有人 404，而命令会报成功）、存在 sha256 冲突
    （整批回滚，报错只有一行）。
    """
    if source == target:
        raise ValueError("源与目标是同一个团队：这条命令什么都不会改，别让它看起来像成功")
    if not target.strip():
        raise ValueError("目标团队不能为空")
    row = conn.execute("select 1 from tenants where id = ?", (target,)).fetchone()
    if row is None:
        raise ValueError(f"目标团队不存在：{target}（先跑 scripts/bootstrap_identity.py）")

    conflicts = _conflicts(conn, source=source, target=target)
    if apply and conflicts:
        raise BindingConflict(conflicts)

    counts: dict[str, int] = {}
    for table in DATA_TABLES:
        # S608 是误报：table 只来自上面那个冻结的常量元组，不是外部输入；
        # 值全部走参数绑定。表名不能参数化，这是 SQLite 的语法限制。
        sql = f"select count(*) from {table} where tenant_id = ?"  # noqa: S608
        counts[table] = int(conn.execute(sql, (source,)).fetchone()[0])

    if apply:
        # 一个事务：搬到一半的库是最难解释的状态——一部分行看得见、一部分看不见，
        # 而没有任何列能区分「没搬」和「搬了一半」。
        with conn:
            for table in DATA_TABLES:
                conn.execute(
                    f"update {table} set tenant_id = ? where tenant_id = ?",  # noqa: S608
                    (target, source),
                )

    return BindReport(
        source=source, target=target, counts=counts, conflicts=conflicts, applied=apply
    )
