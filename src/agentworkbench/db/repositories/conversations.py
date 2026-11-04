"""会话仓储：会话 / 消息 / 引用快照的原始读写。

**这里只有 SQL，没有规则。** 新不新开会话、标题怎么派生、越权该回 404 还是
403，全在 `chat/` 那一层；这里只回答"这行写进去了吗""查得到几行"。

三条结构性保证，都靠 SQL 自身表达，不靠调用方记得：

1. **`owner` 是每个函数参数表里的第二个名字**（`conn` 之后），没有默认值、
   没有"全部"取值。可选 owner（为空则不过滤）是最坏的设计：漏传一次就是
   跨租户泄漏，而测试里所有用例都传同一个 owner，漏与不漏结果一模一样。
2. **过滤写在 `WHERE` 里，不是"查回来再过滤"。** 查全部再过滤会让分页的
   总数泄漏——`COUNT(*)` 仍然是全表，用户能从"第 2 页只有 1 条"推断出
   存在一条他看不到的会话。业务上根本不需要知道这个数，但泄漏是自动发生的。
3. **写操作自带归属校验。** 插入用 `insert ... select ... where exists(...)`，
   归属不成立就一行都插不进去，而不是"先查一次再插"——那两步之间是窗口，
   而窗口在单机测试里永远不会出现。

**本模块不 `commit`。** 事务边界归调用方：删会话要删五处（F5 §1.7），
中途 commit 就会留下"消息删了、运行还在"的半成品，而界面上显示的是"删除成功"。

一处已知边界（不在本文件补）：`agent_runs` 与会话之间唯一的边是
`messages.run_id`。所以**崩溃在写回答之前的那次 run 从会话这边够不着**——
它没有 assistant 行，就没有指向它的边。要不要给 `agent_runs` 加
`conversation_id`，等 `chat/messages.py` 有真实写入方时一起定（C-V11）。

来源：C# `Persistence/ConversationRepository.cs`
服务于：REQ-04（自己的历史）/ REQ-21（三块一起保存）
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import cast

from agentworkbench.core.conversation import CitationSnapshot, MessageRole
from agentworkbench.core.identity import Owner


def create(
    conn: sqlite3.Connection,
    owner: Owner,
    *,
    conversation_id: str,
    title: str,
    now: str,
) -> None:
    """建一条会话。`title` 由调用方派生（`core.conversation.derive_title`），

    这里不派生：仓储不该知道"18 字"这条规则，否则改标题规则要改两处。
    """
    conn.execute(
        "insert into conversations(id, user_id, tenant_id, title, created_at, updated_at)"
        " values (?,?,?,?,?,?)",
        (conversation_id, owner.user_id, owner.tenant_id, title, now, now),
    )


def get(conn: sqlite3.Connection, owner: Owner, conversation_id: str) -> sqlite3.Row | None:
    """按 id 取。**返回 `None` 有两种可能，调用方不许区分**：

    - 这条会话不存在
    - 存在，但不是你的

    区分开（回 403 而不是 404）就等于承认了"这条 id 存在"，存在性照样泄漏。
    """
    return cast(
        sqlite3.Row | None,
        conn.execute(
            "select * from conversations where id = ? and tenant_id = ? and user_id = ?",
            (conversation_id, owner.tenant_id, owner.user_id),
        ).fetchone(),
    )


def list_for_owner(
    conn: sqlite3.Connection, owner: Owner, *, limit: int = 50, offset: int = 0
) -> list[sqlite3.Row]:
    """列表。过滤与排序都在 SQL 里，索引 `ix_conversations_owner_updated` 直接命中。

    `id` 进排序键不是洁癖：`updated_at` 只到秒，同一秒建的两条靠时间排会换位，
    而"换设备一模一样"落到测试里就变成随机红——随机红最后一定被人用 `-x` 跳过。
    """
    return cast(
        list[sqlite3.Row],
        conn.execute(
            "select * from conversations where tenant_id = ? and user_id = ?"
            " order by updated_at desc, id desc limit ? offset ?",
            (owner.tenant_id, owner.user_id, limit, offset),
        ).fetchall(),
    )


def count_for_owner(conn: sqlite3.Connection, owner: Owner) -> int:
    """总数。**必须在这里就过滤**：分页的总数如果取全表，用户翻到最后一页

    看到"共 3 条、本页 1 条"就能反推出有两条他看不到。这个数字业务上没人需要，
    但泄漏是自动发生的。
    """
    row = conn.execute(
        "select count(*) as n from conversations where tenant_id = ? and user_id = ?",
        (owner.tenant_id, owner.user_id),
    ).fetchone()
    return int(row["n"])


def find_message_by_run(conn: sqlite3.Connection, owner: Owner, run_id: str) -> sqlite3.Row | None:
    """按 run_id 找它的挂点消息（状态端点的 answer 从这来，F6 S-V2）。

    归属校验与读取是同一条语句（与 `list_messages` 同款）——分成
    "先查 run 再取消息"两步也能对，但那是两条语句两种时序，跨租户读取
    会藏进第二条的窗口里。
    """
    return cast(
        sqlite3.Row | None,
        conn.execute(
            "select m.id, m.conversation_id, m.role, m.content from messages m"
            " join conversations c on c.id = m.conversation_id"
            " where m.run_id = ? and c.tenant_id = ? and c.user_id = ?",
            (run_id, owner.tenant_id, owner.user_id),
        ).fetchone(),
    )


def touch(conn: sqlite3.Connection, owner: Owner, conversation_id: str, *, now: str) -> bool:
    """把 `updated_at` 推到 `now`。返回是否真的改到了行。

    **只动 `updated_at`**：标题写一次之后不再变（C-V9）。让它跟着刷新，
    列表里就会出现"标题随最后一次提问变"，而两个人看同一条会话会看到
    两个标题——那正是"一模一样"这条判据的反面。
    """
    cursor = conn.execute(
        "update conversations set updated_at = ? where id = ? and tenant_id = ? and user_id = ?",
        (now, conversation_id, owner.tenant_id, owner.user_id),
    )
    return cursor.rowcount > 0


def delete(conn: sqlite3.Connection, owner: Owner, conversation_id: str) -> int | None:
    """删一条会话**和它的全部痕迹**。返回删掉的消息条数；`None` = 没删到。

    `None` 同样不区分"不存在"与"不是你的"（见 `get`）。

    **没有"先查一次再删"这一步。** 每条语句都自带 owner 过滤，所以传别人的 id
    时它们全部是空操作，最后由删会话那一句判断到底有没有这行。先查再删的写法
    在两步之间留了一个窗口，而窗口在单机测试里永远不出现——它只在被利用时出现。

    五处顺序是有意的，不能换：

    1. 引用快照（`messages` 的 `ON DELETE CASCADE` 本来会带走，但**显式删**：
       级联开关是连接级的（`pragma foreign_keys`），谁忘了开就留下孤儿行，
       而且不报错——留下的是原文片段，正是 §1.7 最不该留的那一份）
    2. 取 `run_id`（**必须在删消息之前**：消息一删，这条边就没了）
    3. 消息
    4. `agent_steps`（run 的 `ON DELETE CASCADE` 同样不依赖）
    5. `agent_runs`，最后才删会话本身

    第 5 步放在最后不是为了好看：`messages.run_id` 是 `ON DELETE RESTRICT`，
    反过来的顺序（先删 run）会被库直接拒绝——这个拒绝是有用的，它证明
    "回答还在时 run 删不掉"（C-V4）。
    """
    scope = (conversation_id, owner.tenant_id, owner.user_id)

    conn.execute(
        "delete from message_citations where message_id in ("
        "  select m.id from messages m where m.conversation_id in ("
        "    select id from conversations where id = ? and tenant_id = ? and user_id = ?))",
        scope,
    )

    run_ids = [
        str(row["run_id"])
        for row in conn.execute(
            "select m.run_id from messages m where m.conversation_id in ("
            "  select id from conversations where id = ? and tenant_id = ? and user_id = ?)"
            " and m.run_id is not null",
            scope,
        ).fetchall()
    ]

    deleted = conn.execute(
        "delete from messages where conversation_id in ("
        "  select id from conversations where id = ? and tenant_id = ? and user_id = ?)",
        scope,
    ).rowcount

    if run_ids:
        marks = ",".join("?" * len(run_ids))
        conn.execute(f"delete from agent_steps where run_id in ({marks})", run_ids)  # noqa: S608
        conn.execute(f"delete from agent_runs where id in ({marks})", run_ids)  # noqa: S608

    cursor = conn.execute(
        "delete from conversations where id = ? and tenant_id = ? and user_id = ?", scope
    )
    return int(deleted) if cursor.rowcount > 0 else None


def insert_message(
    conn: sqlite3.Connection,
    owner: Owner,
    *,
    conversation_id: str,
    role: MessageRole,
    content: str,
    run_id: str | None,
    now: str,
) -> int:
    """写一行消息，返回自增 id。

    归属不成立时 `where exists` 让这次插入一行都不产生，这里抛 `KeyError`。
    **不静默返回 0**：把别人的会话 id 当自己的写进去，正是最难发现的一类越权——
    内容进了他人历史，两边界面都正常。

    `run_id` 非空时还要求这次运行**也是自己的**（第二个 `exists`）。不查的话，
    一条回答可以挂到别人的 run 上：写入一切正常，直到展开轨迹时按 owner 过滤
    查不到那条 run，表现成"这条回答没有过程"，而没有任何地方会报错。

    角色与 run 的配对由库层的 CHECK 兜底（assistant 必须有 run、user 不许有），
    所以这里传错会当场 `IntegrityError`，不会写成一条无法回放的回答。
    """
    row = conn.execute(
        "insert into messages(conversation_id, role, content, run_id, created_at)"
        " select ?, ?, ?, ?, ?"
        " where exists (select 1 from conversations"
        "   where id = ? and tenant_id = ? and user_id = ?)"
        # `? is null or ...`：user 行没有 run，跳过这一条。
        "   and (? is null or exists (select 1 from agent_runs"
        "     where id = ? and tenant_id = ? and user_id = ?))"
        " returning id",
        (
            conversation_id,
            role.value,
            content,
            run_id,
            now,
            conversation_id,
            owner.tenant_id,
            owner.user_id,
            run_id,
            run_id,
            owner.tenant_id,
            owner.user_id,
        ),
    ).fetchone()
    if row is None:
        raise KeyError(f"会话不存在或不属于该用户：{conversation_id}")
    return int(row["id"])


def complete_message(
    conn: sqlite3.Connection,
    owner: Owner,
    *,
    conversation_id: str,
    run_id: str,
    content: str,
) -> int | None:
    """把**已经占好位**的那一行补全成回答。返回 message_id；没有占位行时返回 `None`。

    为什么是 UPDATE 而不是 INSERT（C-V11）：run 一起来就先占了一行空正文，
    这样进程在写正文之前被杀，那次运行仍然挂在某条会话上——不是"少一条记录"，
    而是"这条 run 落不到任何历史，删会话也带不走它"。

    UPDATE 的条件里带 owner（`exists`），不是"先查到再改"：漏了归属的写
    在测试里通常看不出来（所有用例都是同一个 owner），却能改写别人的回答。

    返回 `None` 的两种情形由调用方区分：
    - 占位行从来没写过（老调用方 / 失败补写）→ 调用方补插一行；
    - 这条 run 不属于这个 owner → 补插会抛 `KeyError`，越权写入当场失败。
    """
    row = conn.execute(
        "update messages set content = ?"
        " where run_id = ? and conversation_id = ?"
        "   and exists (select 1 from conversations"
        "     where id = ? and tenant_id = ? and user_id = ?)"
        " returning id",
        (
            content,
            run_id,
            conversation_id,
            conversation_id,
            owner.tenant_id,
            owner.user_id,
        ),
    ).fetchone()
    return None if row is None else int(row["id"])


def list_messages(
    conn: sqlite3.Connection, owner: Owner, conversation_id: str
) -> list[sqlite3.Row]:
    """一条会话的全部消息，按写入顺序，带上每条回答所属那次运行的终态。

    **归属校验与读取是同一条语句**（`join conversations ... where c.user_id`）。
    分成"先查会话、再按 id 取消息"两步也能对，但顺序一写反就是跨租户读取，
    而测试很可能是"先读后校验"——那时它依然全绿。

    为什么把 run 的 `status` / `reason` 带出来（C-V13）：历史里要把"拒答"与
    "普通回答"分开，而正文都是一段文字。**不在 `messages` 上再存一份状态**——
    两份会互相矛盾且发现不了；这里读的是 `agent_runs` 那一行，它一旦落终态
    就是吸收态，不会改口。`left join` 只为 user 行（`run_id is null`）不落空。
    """
    return cast(
        list[sqlite3.Row],
        conn.execute(
            "select m.*, r.status as run_status, r.reason as run_reason"
            " from messages m"
            " join conversations c on c.id = m.conversation_id"
            " left join agent_runs r on r.id = m.run_id"
            " where m.conversation_id = ? and c.tenant_id = ? and c.user_id = ?"
            # 按自增 id 排 = 按写入顺序。`created_at` 只到秒，同一秒的两条
            # （问完立刻答）会换位——回放时"答在问之前"，而那是历史本身错了
            " order by m.id",
            (conversation_id, owner.tenant_id, owner.user_id),
        ).fetchall(),
    )


def insert_citations(
    conn: sqlite3.Connection,
    owner: Owner,
    *,
    message_id: int,
    snapshots: Sequence[CitationSnapshot],
) -> None:
    """把当次的引用抄一份存下来。**与正文同一个事务**（本模块不 commit）。

    归属同样写进语句里：`message_id` 属于别人的会话时一条也插不进去。
    这在正常流程里不会发生（`message_id` 刚刚由 `insert_message` 返回），
    但它是"写侧唯一入口"被绕开时的最后一道门。

    条数对不上就抛错。少写一条不会让任何测试变红——它会安静地变成
    一张少了出处卡片的历史回答，而那与"当时确实只有两条"无法区分。
    """
    if not snapshots:
        return

    before = conn.total_changes
    conn.executemany(
        "insert into message_citations("
        "  message_id, no, file_name, seq, excerpt, check_first, check_second, coverage)"
        " select ?, ?, ?, ?, ?, ?, ?, ?"
        " where exists ("
        "   select 1 from messages m join conversations c on c.id = m.conversation_id"
        "    where m.id = ? and c.tenant_id = ? and c.user_id = ?)",
        [
            (
                message_id,
                snapshot.no,
                snapshot.file_name,
                snapshot.seq,
                snapshot.excerpt,
                snapshot.check_first.value,
                None if snapshot.check_second is None else snapshot.check_second.value,
                snapshot.coverage,
                message_id,
                owner.tenant_id,
                owner.user_id,
            )
            for snapshot in snapshots
        ],
    )
    inserted = conn.total_changes - before
    if inserted != len(snapshots):
        raise KeyError(
            f"引用快照未写全（{inserted}/{len(snapshots)}）：消息 {message_id} 不属于该用户"
        )


def list_citations(
    conn: sqlite3.Connection, owner: Owner, conversation_id: str
) -> list[sqlite3.Row]:
    """整条会话的引用，按 (消息, 编号) 排。**不回表查 `chunks`**——快照自包含。"""
    return cast(
        list[sqlite3.Row],
        conn.execute(
            "select mc.* from message_citations mc"
            " join messages m on m.id = mc.message_id"
            " join conversations c on c.id = m.conversation_id"
            " where m.conversation_id = ? and c.tenant_id = ? and c.user_id = ?"
            " order by mc.message_id, mc.no",
            (conversation_id, owner.tenant_id, owner.user_id),
        ).fetchall(),
    )
