"""消息与引用的持久化，以及消息到运行记录的关联。

这是 C# 版三个真缺口里最实的一个：引用只通过实时推送发过一次、从未落库，
消息与 run 之间没有关联字段——重开会话时引用消失，
也说不清「这条回答来自哪次运行」。

一条回答由三块组成：正文、引用、那次运行。存一块，等于没存。

**快照只复制，不重算。** 这里的每一条引用都来自当次的校验报告与当次证据，
不重新检索、不重新打分、不重新判定。重算过的历史会随着模型与阈值的变更
自己改口，而没有任何东西会红——它读起来和"当时就是这样"一模一样。

来源：C# `Webapi/Hubs/AgentHub.cs`（推送侧）的缺失面
服务于：REQ-21（F5 第三段，最容易漏的一段）
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import replace

from agentworkbench.core.conversation import EXCERPT_MAX_CHARS, CitationSnapshot, MessageRole
from agentworkbench.core.identity import Owner
from agentworkbench.core.judgment import (
    CitationFirstCheck,
    CitationSecondCheck,
    Evidence,
    NumberedHit,
)
from agentworkbench.db.repositories import conversations as repo
from agentworkbench.guard.citation import CitationReport

# 二级结论的严重程度。合并同一个编号的多条引用时，取**最坏**的那条：
# 一张卡片显示"支持"，而它同时被某句话误挂过，等于给了一句假担保——
# 而假担保比没有卡片更危险，因为它带着承诺。
_SECOND_SEVERITY = {
    CitationSecondCheck.SUPPORTED: 0,
    CitationSecondCheck.UNVERIFIED: 1,
    CitationSecondCheck.UNSUPPORTED: 2,
}


def snapshot_citations(
    report: CitationReport | None, hits: Sequence[NumberedHit]
) -> tuple[CitationSnapshot, ...]:
    """把当次校验结论抄成可入库的快照。**不查询任何表。**

    只留一级通过的（`KEPT`）。越界的那条（`STRIPPED`）没有对应的块，
    存下来就是一张没有原文的卡片——而"角标指向不存在的东西"正是校验要抓的问题，
    不该在历史里被固化成一条记录。越界数已经落在 `agent_runs` 上。

    同一个编号出现多次（两句话都引了 `[2]`）合并成一条：表上
    `UNIQUE(message_id, no)` 已经定了"一块一张卡片"，而合并时取最坏结论。

    `report is None` 时返回空——拒答没有正文可校验，也就没有引用。
    此时那块证据仍然在 `agent_runs` 上可查，不属于本函数的职责。
    """
    if report is None:
        return ()

    by_no = {hit.no: hit for hit in hits}
    merged: dict[int, CitationSnapshot] = {}

    for citation in report.citations:
        if citation.first is not CitationFirstCheck.KEPT:
            continue
        hit = by_no.get(citation.evidence_no)
        if hit is None:
            # 一级过了就说明编号在证据里；走到这里说明证据与报告不同源（D-03 被破坏）。
            # 跳过而不是抛错：抛错会让一次已经答完的问答在落库时变成 500，
            # 而用户手里的答案是对的。少一张卡片比丢一条回答轻。
            continue

        candidate = CitationSnapshot(
            no=citation.evidence_no,
            file_name=hit.file_name,
            seq=hit.seq,
            # 120 字与界面同数（`EXCERPT_MAX_CHARS`）：库里存的和界面显示的
            # 必须是同一份，否则"与当时一致"要按哪一份算就说不清了
            excerpt=hit.content[:EXCERPT_MAX_CHARS],
            check_first=citation.first,
            check_second=citation.second,
            coverage=citation.coverage,
            kb_id=hit.kb_id,
        )
        previous = merged.get(citation.evidence_no)
        merged[citation.evidence_no] = (
            candidate if previous is None else _worse(previous, candidate)
        )

    # 按编号排：界面按顺序渲染卡片，乱序会让同一条回答两次打开的顺序不同
    return tuple(merged[no] for no in sorted(merged))


def _worse(left: CitationSnapshot, right: CitationSnapshot) -> CitationSnapshot:
    """两条引用指向同一块时留哪一条：**留更坏的那条**。

    `coverage` 取最小值——卡片上只有一个数，它表示"这块被支持得最勉强的那次使用"。
    取最大值等于把最差的那次藏起来，而用户点开卡片时问的正是"凭什么说这句支持"。
    """
    pick = right if _severity(right.check_second) > _severity(left.check_second) else left
    coverages = [value for value in (left.coverage, right.coverage) if value is not None]
    return replace(pick, coverage=min(coverages) if coverages else None)


def _severity(check: CitationSecondCheck | None) -> int:
    """`None`（没判过）与 `UNVERIFIED`（判了但判不了）同级：都是"不知道"。

    合并时它们不会与 `SUPPORTED` 混在一起——阈值是全局参数，要么全都判了，
    要么全都判不了。
    """
    return 1 if check is None else _SECOND_SEVERITY[check]


def begin_answer(
    conn: sqlite3.Connection,
    owner: Owner,
    *,
    conversation_id: str,
    run_id: str,
    now: str,
) -> int:
    """run 一起来就占一行空 assistant，返回 message_id。

    这一行在作答结束之前就存在，所以"进程在写正文之前被杀"也有挂点（C-V11）。
    没有它，那次 run 在库里停在 `running`，而消息里一行都没有——它落不到任何
    一段历史，删会话也带不走，就那么一直躺着。

    正文用空串，不是 NULL 也不是占位文案：库层 `content` 是 NOT NULL，空串是
    "还没有正文"的唯一表示；占位文案会被当成模型真的这么答了一句。

    **这里不推 `updated_at`。** 推算"最后活动时间"时用回答落库那一刻：run 一起跑
    就推的话，一条跑了两分钟的会话会在列表里显示成"两分钟前刚聊过"，而它其实
    还没答完。
    """
    with conn:
        return repo.insert_message(
            conn,
            owner,
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT,
            content="",
            run_id=run_id,
            now=now,
        )


def complete_answer(
    conn: sqlite3.Connection,
    owner: Owner,
    *,
    conversation_id: str,
    run_id: str,
    answer: str,
    evidence: Evidence | None,
    report: CitationReport | None,
    now: str,
) -> int:
    """把占位行补全成一条回答：正文 + 引用快照，一次事务。

    **在原行上补，不另插一行。** 另插会撞 `ux_messages_run`（一次运行只能有
    一条回答），而如果那个索引不在了，就会变成"一问两答"——两条回答都指着
    同一次运行，界面上看起来像重复推送。

    分两次提交会出现"有正文没引用"的半条回答——它在界面上与"引用从未落库"
    完全一样，排查的人会去改前端渲染。

    `answer` 允许是空串：失败或取消的那一轮也要走完这一步（占位行本来就在，
    这里什么也不改），否则"它为什么没答上来"恰好无处可查。

    `evidence` 可以为 `None`，只在 `report` 也是 `None` 时成立——那是"检索或模型
    在返回结果之前就失败了"，此时没有证据，也没有卡片。反过来（有报告却没证据）
    是个编程错误，直接抛：那会让每张卡片都找不到原文，而库里看不出哪里不对。

    `run_id` 必须是一次**自己的**运行（由仓储的第二个 `exists` 保证），
    否则这条回答的轨迹永远展开不了，而写入时什么都不报。
    """
    if report is not None and evidence is None:
        raise ValueError("有校验报告却没有证据：引用卡片会指向不存在的块")

    snapshots = snapshot_citations(report, () if evidence is None else evidence.hits)

    with conn:
        message_id = repo.complete_message(
            conn,
            owner,
            conversation_id=conversation_id,
            run_id=run_id,
            content=answer,
        )
        if message_id is None:
            # 没有占位行（失败补写、或调用方没走两段写）：补一行。
            # 这条 run 不属于这个 owner 时 `insert_message` 会抛 KeyError——
            # 越权写入必须当场失败，不能"改不到就算了"。
            message_id = repo.insert_message(
                conn,
                owner,
                conversation_id=conversation_id,
                role=MessageRole.ASSISTANT,
                content=answer,
                run_id=run_id,
                now=now,
            )
        repo.insert_citations(conn, owner, message_id=message_id, snapshots=snapshots)
        # 答案落库才算这一轮结束：更新时间推到这里，列表里这条会话才会浮上来。
        # 放在占位那一步推，会有整整一段生成时间显示成"五分钟前刚聊过"
        repo.touch(conn, owner, conversation_id, now=now)

    return message_id


def save_answer(
    conn: sqlite3.Connection,
    owner: Owner,
    *,
    conversation_id: str,
    run_id: str,
    answer: str,
    evidence: Evidence | None,
    report: CitationReport | None,
    now: str,
) -> int:
    """旧的一次性写法：等价于 `complete_answer`（有占位行就补全，没有就补插）。

    保留这个名字是因为它读起来仍然是"存一条回答"。新代码请用两段写：
    先 `begin_answer`（run 一起来），再 `complete_answer`（跑完）。
    """
    return complete_answer(
        conn,
        owner,
        conversation_id=conversation_id,
        run_id=run_id,
        answer=answer,
        evidence=evidence,
        report=report,
        now=now,
    )
