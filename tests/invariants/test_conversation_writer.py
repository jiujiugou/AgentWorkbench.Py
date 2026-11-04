"""不变量：一轮问答的写入是原子的，标题只写一次，回答必须指着自己的那次运行。

失效形态（用户会看到什么）：

- **新建与落消息分两次提交** → 进程停在两者之间，列表里多出一条没有任何消息的
  会话，看起来像"用户点开了又关掉"，实际是写到一半。
- **标题跟着追问刷新** → 同一条会话在两台设备上显示两个名字，"换设备一模一样"
  当场不成立，而消息本身是对的（C-V9）。
- **他人的 `conversationId` 报错而不是当作不存在** → 报错本身就是存在性证据：
  同一条 id 报错、瞎编一条不报，两者一比就分出来了（C-V2）。
- **正文与引用分两次提交** → 出现"有正文没引用"的半条回答。它在界面上与
  "引用从未落库"完全一样，排查的人会去改前端渲染（C-V3）。
- **回答可以挂到别人的 run 上** → 写入一切正常，展开轨迹时按 owner 过滤查不到，
  表现成"这条回答没有过程"，而没有任何地方会报错（C-V11）。
- **同一块被两句话引用时留了"支持"那条** → 卡片给出一句假担保，而另一句话
  其实是误挂；用户点开原文才发现对不上。
- **删除不是全有或全无** → 界面说"删除成功"，库里还剩一半（C-V8）。

改坏这个不变量会怎样：把 `start_turn` 的两次写拆成两次提交、把标题改成每次追问
都重写、把 `insert_message` 的 run 归属 `exists` 去掉、把 `save_answer` 的
`with conn:` 去掉——四处都会让对应用例当场红。这不是设想，是本文件写完后
逐条改坏验过的。

服务于：C-V2 / C-V3 / C-V9 / C-V11（写入层；端点与端到端另验）
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator

import pytest

from agentworkbench.chat import conversation as chat
from agentworkbench.chat import messages as writer
from agentworkbench.core.conversation import EXCERPT_MAX_CHARS, MessageRole
from agentworkbench.core.identity import Owner
from agentworkbench.core.judgment import (
    CitationSecondCheck,
    Evidence,
    Judgment,
    JudgmentValue,
    NumberedHit,
)
from agentworkbench.db import engine
from agentworkbench.guard.citation import verify

ALICE = Owner(user_id="alice", tenant_id="team-a")
BOB = Owner(user_id="bob", tenant_id="team-a")

T0 = "2026-11-03T09:00:00"
T1 = "2026-11-03T09:05:00"

MANUAL = "维护保养每季度清洁散热风道并紧固接线端子。"
ALARM = "E2 报警表示通讯超时，先确认波特率与站号是否和主站一致。"
UNRELATED = "本周天气不错适合出门散步。"


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("pragma foreign_keys = on")
    engine.load_vec(connection)
    engine.create_schema(connection)
    yield connection
    connection.close()


def hit(
    no: int, content: str, *, file_name: str = "汇川H5U用户手册.pdf", seq: int = 1
) -> NumberedHit:
    return NumberedHit(
        no=no,
        chunk_id=no * 10,
        document_id="doc-1",
        file_name=file_name,
        content=content,
        seq=seq,
        page_from=1,
        page_to=2,
        heading=None,
        vector_distance=None,
    )


def evidence_of(*hits: NumberedHit) -> Evidence:
    return Evidence(
        hits=hits,
        context_chars=sum(len(item.content) for item in hits),
        judgment=Judgment(
            verdict=JudgmentValue.ANSWERED, top_distance=0.1, margin=0.2, reason=None
        ),
    )


def make_run(conn: sqlite3.Connection, owner: Owner = ALICE, *, run_id: str) -> str:
    conn.execute(
        "insert into agent_runs (id, user_id, tenant_id, question, status, started_at)"
        " values (?, ?, ?, ?, 'succeeded', ?)",
        (run_id, owner.user_id, owner.tenant_id, "问", T0),
    )
    return run_id


def answer(
    conn: sqlite3.Connection,
    conversation_id: str,
    *,
    run_id: str,
    text: str,
    hits: tuple[NumberedHit, ...],
    threshold: float | None = 0.5,
    now: str = T1,
) -> int:
    """走真实链路：verify 出报告 → save_answer 落库。"""
    report = verify(text, hits, threshold)
    return writer.save_answer(
        conn,
        ALICE,
        conversation_id=conversation_id,
        run_id=run_id,
        answer=report.cleaned_answer,
        evidence=evidence_of(*hits),
        report=report,
        now=now,
    )


# ── 开一轮：新建 / 复用 / 他人的 id ──


def test_first_question_creates_a_conversation_named_after_it(conn: sqlite3.Connection) -> None:
    started = chat.start_turn(conn, ALICE, question=ALARM, now=T0)

    assert started.created is True
    row = conn.execute(
        "select * from conversations where id = ?", (started.conversation_id,)
    ).fetchone()
    assert row["title"] == ALARM[:18]
    assert row["user_id"] == "alice"


def test_second_question_lands_in_the_same_conversation(conn: sqlite3.Connection) -> None:
    """C-V2 正控：追问不新开会话，消息 +2，`updated_at` 前进。"""
    first = chat.start_turn(conn, ALICE, question=ALARM, now=T0)
    make_run(conn, run_id="r1")
    answer(
        conn,
        first.conversation_id,
        run_id="r1",
        text=f"{MANUAL}[1]",
        hits=(hit(1, MANUAL),),
        now=T0,
    )

    second = chat.start_turn(
        conn, ALICE, question="再问一句", conversation_id=first.conversation_id, now=T1
    )
    make_run(conn, run_id="r2")
    answer(
        conn,
        second.conversation_id,
        run_id="r2",
        text=f"{MANUAL}[1]",
        hits=(hit(1, MANUAL),),
        now=T1,
    )

    assert second.created is False
    assert conn.execute("select count(*) as n from conversations").fetchone()["n"] == 1
    assert conn.execute("select count(*) as n from messages").fetchone()["n"] == 4
    row = conn.execute("select updated_at from conversations").fetchone()
    assert row["updated_at"] == T1


def test_follow_up_never_rewrites_the_title(conn: sqlite3.Connection) -> None:
    """C-V9 负控：标题写一次之后，追问多长都不改它。

    跟着改的话，两个人看同一条会话会看到两个名字——而两个都是"当前正确"的，
    没有哪一边能判断自己看到的是不是当时那个。
    """
    first = chat.start_turn(conn, ALICE, question=ALARM, now=T0)

    chat.start_turn(
        conn,
        ALICE,
        question="换个完全不相干的话题：今天天气怎么样，适合出去走走吗",
        conversation_id=first.conversation_id,
        now=T1,
    )

    row = conn.execute("select title from conversations").fetchone()
    assert row["title"] == ALARM[:18]


def test_a_foreign_conversation_id_starts_a_new_one(conn: sqlite3.Connection) -> None:
    """C-V2 负控：别人的 id 当作不存在——为他新建一条自己的，别人那条纹丝不动。"""
    bobs = chat.start_turn(conn, BOB, question=ALARM, now=T0)

    alices = chat.start_turn(
        conn, ALICE, question=ALARM, conversation_id=bobs.conversation_id, now=T1
    )

    assert alices.created is True
    assert alices.conversation_id != bobs.conversation_id
    assert conn.execute("select count(*) as n from conversations").fetchone()["n"] == 2
    bob_messages = conn.execute(
        "select count(*) as n from messages where conversation_id = ?", (bobs.conversation_id,)
    ).fetchone()["n"]
    assert bob_messages == 1, "别人会话里只应有他自己那条问题"
    alice_messages = conn.execute(
        "select count(*) as n from messages where conversation_id = ?", (alices.conversation_id,)
    ).fetchone()["n"]
    assert alice_messages == 1


def test_conversation_is_not_created_when_its_first_message_cannot_be_written(
    conn: sqlite3.Connection,
) -> None:
    """新建与落消息是同一个事务：消息写不进去时，会话也不许留下。

    分开提交的话，列表里会多出一条没有任何消息的会话——看起来像"用户点开了
    又关掉"，而实际是写到一半。这种行还会一直排在最前面，因为它没有后续更新。
    """
    conn.execute(
        "create trigger boom before insert on messages"
        " begin select raise(abort, '第一句写不进去'); end"
    )

    with pytest.raises(sqlite3.DatabaseError, match="第一句写不进去"):
        chat.start_turn(conn, ALICE, question=ALARM, now=T0)

    assert conn.execute("select count(*) as n from conversations").fetchone()["n"] == 0


def test_empty_question_never_reaches_the_history(conn: sqlite3.Connection) -> None:
    """空问题在写入口就拒绝。

    只靠 `derive_title` 挡不住：它只在**新建**路径上被调用，复用路径完全不碰它，
    于是空问题能一路写进历史的用户消息里，界面显示成一条空白气泡。
    """
    with pytest.raises(ValueError, match="空问题"):
        chat.start_turn(conn, ALICE, question="   ", now=T0)

    assert conn.execute("select count(*) as n from conversations").fetchone()["n"] == 0
    assert conn.execute("select count(*) as n from messages").fetchone()["n"] == 0


# ── 回答 ↔ 运行 ──


def test_answer_row_points_at_its_run(conn: sqlite3.Connection) -> None:
    """C-V11 正控：按会话列得出的 run，就是回答行上那个 id。"""
    started = chat.start_turn(conn, ALICE, question=ALARM, now=T0)
    make_run(conn, run_id="r1")
    message_id = answer(
        conn, started.conversation_id, run_id="r1", text=f"{MANUAL}[1]", hits=(hit(1, MANUAL),)
    )

    row = conn.execute("select run_id from messages where id = ?", (message_id,)).fetchone()
    listed = conn.execute(
        "select m.run_id from messages m where m.conversation_id = ? and m.run_id is not null",
        (started.conversation_id,),
    ).fetchall()

    assert row["run_id"] == "r1"
    assert [item["run_id"] for item in listed] == ["r1"]


def test_answer_cannot_point_at_someone_elses_run(conn: sqlite3.Connection) -> None:
    """C-V11 负控：拿别人的 run 挂自己的回答，一行都不许落。"""
    started = chat.start_turn(conn, ALICE, question=ALARM, now=T0)
    make_run(conn, BOB, run_id="bobs-run")

    with pytest.raises(KeyError, match="不属于该用户"):
        answer(
            conn,
            started.conversation_id,
            run_id="bobs-run",
            text=f"{MANUAL}[1]",
            hits=(hit(1, MANUAL),),
        )

    assert conn.execute("select count(*) as n from messages").fetchone()["n"] == 1, "只剩那条问题"


def test_placeholder_row_exists_before_the_answer_is_known(conn: sqlite3.Connection) -> None:
    """C-V11 的崩溃边：run 一起来就有一行，此时正文还没有。

    这一行是「进程在写正文之前被杀」的唯一挂点。没有它，那次 run 停在
    `running`，而消息里一行都没有——它落不到任何会话，删会话也带不走它。

    正文是空串而不是占位文案：占位文案会被界面当成模型真的这么答了一句。
    """
    started = chat.start_turn(conn, ALICE, question=ALARM, now=T0)
    make_run(conn, run_id="r1")

    message_id = writer.begin_answer(
        conn, ALICE, conversation_id=started.conversation_id, run_id="r1", now=T1
    )

    row = conn.execute(
        "select role, content, run_id from messages where id = ?", (message_id,)
    ).fetchone()
    assert row["role"] == "assistant"
    assert row["content"] == ""
    assert row["run_id"] == "r1"
    # 引用一条都没有，也不是"引用丢了"：这时候还没有回答可校验
    assert conn.execute("select count(*) as n from message_citations").fetchone()["n"] == 0


def test_placeholder_is_completed_in_place_not_duplicated(conn: sqlite3.Connection) -> None:
    """补全是在**原行**上补，不是再插一行。

    再插会撞 `ux_messages_run`；万一索引不在了就变成"一问两答"——两条回答都指着
    同一次运行，界面上看起来像重复推送，而库里没有任何东西会报错。
    """
    started = chat.start_turn(conn, ALICE, question=ALARM, now=T0)
    make_run(conn, run_id="r1")
    placeholder = writer.begin_answer(
        conn, ALICE, conversation_id=started.conversation_id, run_id="r1", now=T0
    )

    completed = answer(
        conn, started.conversation_id, run_id="r1", text=f"{MANUAL}[1]", hits=(hit(1, MANUAL),)
    )

    rows = conn.execute("select id, content from messages where run_id = 'r1'").fetchall()
    assert completed == placeholder, "补全应当返回同一个 message_id"
    assert len(rows) == 1, "一次运行只能有一条回答"
    assert rows[0]["content"] == f"{MANUAL}[1]", "正文落在占位那一行上，不是另起一行"
    # 引用挂在同一行上：不是另起一条"有卡片的空回答"
    assert (
        conn.execute(
            "select count(*) as n from message_citations where message_id = ?", (placeholder,)
        ).fetchone()["n"]
        == 1
    )


# ── 三块一起写 ──


def test_answer_and_its_citations_land_together_or_not_at_all(conn: sqlite3.Connection) -> None:
    """C-V3 的原子性：引用写失败时，正文也一起回滚。

    用触发器制造失败，而不是"少传一条引用"——少传不会失败，只会安静地写出
    一条没有卡片的历史回答，而那与"当时确实没引用"无法区分。
    """
    started = chat.start_turn(conn, ALICE, question=ALARM, now=T0)
    make_run(conn, run_id="r1")
    conn.execute(
        "create trigger boom before insert on message_citations"
        " begin select raise(abort, '引用写不进去'); end"
    )

    with pytest.raises(sqlite3.DatabaseError, match="引用写不进去"):
        answer(
            conn, started.conversation_id, run_id="r1", text=f"{MANUAL}[1]", hits=(hit(1, MANUAL),)
        )

    assert conn.execute("select count(*) as n from messages").fetchone()["n"] == 1
    assert conn.execute("select count(*) as n from message_citations").fetchone()["n"] == 0


def test_refusal_is_stored_with_its_text_and_no_cards(conn: sqlite3.Connection) -> None:
    """拒答也有一行：正文是固定文案，没有任何卡片（没有正文可校验）。"""
    started = chat.start_turn(conn, ALICE, question=ALARM, now=T0)
    make_run(conn, run_id="r1")
    hits = (hit(1, ALARM),)

    message_id = writer.save_answer(
        conn,
        ALICE,
        conversation_id=started.conversation_id,
        run_id="r1",
        answer="资料中没有足够依据回答这个问题。",
        evidence=evidence_of(*hits),
        report=None,
        now=T1,
    )

    row = conn.execute("select * from messages where id = ?", (message_id,)).fetchone()
    assert row["content"] == "资料中没有足够依据回答这个问题。"
    assert row["run_id"] == "r1"
    assert conn.execute("select count(*) as n from message_citations").fetchone()["n"] == 0


def test_empty_answer_still_leaves_a_row(conn: sqlite3.Connection) -> None:
    """失败/取消的那一轮也要有一行（正文为空串），否则"它为什么没答上来"无处可查。"""
    started = chat.start_turn(conn, ALICE, question=ALARM, now=T0)
    make_run(conn, run_id="r1")

    message_id = writer.save_answer(
        conn,
        ALICE,
        conversation_id=started.conversation_id,
        run_id="r1",
        answer="",
        evidence=evidence_of(),
        report=None,
        now=T1,
    )

    row = conn.execute("select * from messages where id = ?", (message_id,)).fetchone()
    assert row["content"] == ""
    assert row["role"] == MessageRole.ASSISTANT.value


def test_excerpt_is_cut_to_the_length_the_interface_shows(conn: sqlite3.Connection) -> None:
    """库里存的原文与界面切的是同一个长度（C-V3）。

    两边不同数时，"与当时一致"要按哪一份算就说不清了；而两份都"看着正常"。
    """
    started = chat.start_turn(conn, ALICE, question=ALARM, now=T0)
    make_run(conn, run_id="r1")
    long_text = MANUAL * 20

    answer(
        conn,
        started.conversation_id,
        run_id="r1",
        text=f"{long_text}[1]",
        hits=(hit(1, long_text),),
    )

    excerpt = conn.execute("select excerpt from message_citations").fetchone()["excerpt"]
    assert len(excerpt) == EXCERPT_MAX_CHARS
    assert excerpt == long_text[:EXCERPT_MAX_CHARS]


def test_out_of_range_citation_leaves_no_card(conn: sqlite3.Connection) -> None:
    """越界的 `[9]` 不落卡片：它没有对应的块，存下来就是一张没有原文的卡片。"""
    started = chat.start_turn(conn, ALICE, question=ALARM, now=T0)
    make_run(conn, run_id="r1")

    answer(
        conn,
        started.conversation_id,
        run_id="r1",
        text=f"{MANUAL}[1]。还有一条查不到出处的说法[9]。",
        hits=(hit(1, MANUAL),),
    )

    numbers = [row["no"] for row in conn.execute("select no from message_citations")]
    assert numbers == [1]


def test_two_sentences_citing_the_same_block_make_one_card_with_the_worst_verdict(
    conn: sqlite3.Connection,
) -> None:
    """同一块被两句话引用时合并成一张卡片，结论取**最坏**那条。

    留"支持"那条等于给出一句假担保：卡片看着干净，而另一句话其实是误挂。
    用户点开原文才发现对不上——那时候他已经在怀疑整段答案了。
    """
    started = chat.start_turn(conn, ALICE, question=ALARM, now=T0)
    make_run(conn, run_id="r1")

    answer(
        conn,
        started.conversation_id,
        run_id="r1",
        text=f"{MANUAL}[1]。{UNRELATED}[1]。",
        hits=(hit(1, MANUAL),),
    )

    rows = conn.execute("select no, check_second, coverage from message_citations").fetchall()
    assert len(rows) == 1
    assert rows[0]["no"] == 1
    assert rows[0]["check_second"] == CitationSecondCheck.UNSUPPORTED.value
    assert rows[0]["coverage"] is not None and rows[0]["coverage"] < 0.5


def test_missing_threshold_is_stored_as_unverified_not_supported(conn: sqlite3.Connection) -> None:
    """阈值没配时存的是 `'unverified'`（判了但判不了），**不是 `'supported'`**。

    这一格是 D-12 的落点。写成 `'supported'` 的话，一条从没被核对过的引用
    在历史里看起来是"已核实"——而用户点开卡片问的正是"凭什么说这句有出处"。
    """
    started = chat.start_turn(conn, ALICE, question=ALARM, now=T0)
    make_run(conn, run_id="r1")

    answer(
        conn,
        started.conversation_id,
        run_id="r1",
        text=f"{MANUAL}[1]",
        hits=(hit(1, MANUAL),),
        threshold=None,
    )

    row = conn.execute("select check_second, coverage from message_citations").fetchone()
    assert row["check_second"] == CitationSecondCheck.UNVERIFIED.value
    # 覆盖率照样记下来：事后定标时要能拿历史数据重算，而重算需要当时那个数
    assert row["coverage"] is not None


# ── 删除 ──


def test_delete_logs_one_line_without_content(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """C-V8 的日志部分：一行、有会话 id 与条数、**没有内容**。

    运维要能回答"这条会话是被删了，还是从来没建过"。而日志里带原文，
    等于把删掉的东西又抄了一份到日志系统里——那是第二个副本，且不受本域管理。
    """
    started = chat.start_turn(conn, ALICE, question=ALARM, now=T0)
    make_run(conn, run_id="r1")
    answer(conn, started.conversation_id, run_id="r1", text=f"{MANUAL}[1]", hits=(hit(1, MANUAL),))

    with caplog.at_level(logging.INFO, logger="agentworkbench.chat.conversation"):
        assert chat.delete_conversation(conn, ALICE, started.conversation_id) == 2

    records = [
        record for record in caplog.records if record.name == "agentworkbench.chat.conversation"
    ]
    assert len(records) == 1
    message = records[0].getMessage()
    assert started.conversation_id in message
    assert "messages=2" in message
    assert ALARM not in message, "日志里不许出现会话内容"


def test_delete_of_a_foreign_conversation_logs_nothing(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """删别人的会话：返回 `None`，且不留日志行——没删到就没有"已删除"这回事。"""
    bobs = chat.start_turn(conn, BOB, question=ALARM, now=T0)

    with caplog.at_level(logging.INFO, logger="agentworkbench.chat.conversation"):
        assert chat.delete_conversation(conn, ALICE, bobs.conversation_id) is None

    assert not [r for r in caplog.records if r.name == "agentworkbench.chat.conversation"]
    assert conn.execute("select count(*) as n from conversations").fetchone()["n"] == 1


def test_delete_is_all_or_nothing(conn: sqlite3.Connection) -> None:
    """删到一半失败时，前面删掉的全部回来（C-V8 的"同一事务"）。

    用触发器让最后一步（删 run）失败。没有事务时，用户看到的是"删除成功"，
    库里剩下消息已删、run 还在的半成品——而没有任何地方会报错。
    """
    started = chat.start_turn(conn, ALICE, question=ALARM, now=T0)
    make_run(conn, run_id="r1")
    answer(conn, started.conversation_id, run_id="r1", text=f"{MANUAL}[1]", hits=(hit(1, MANUAL),))
    conn.execute(
        "create trigger nope before delete on agent_runs begin select raise(abort, '删不动'); end"
    )

    with pytest.raises(sqlite3.DatabaseError, match="删不动"):
        chat.delete_conversation(conn, ALICE, started.conversation_id)

    assert conn.execute("select count(*) as n from conversations").fetchone()["n"] == 1
    assert conn.execute("select count(*) as n from messages").fetchone()["n"] == 2
    assert conn.execute("select count(*) as n from message_citations").fetchone()["n"] == 1
    assert conn.execute("select count(*) as n from agent_runs").fetchone()["n"] == 1
