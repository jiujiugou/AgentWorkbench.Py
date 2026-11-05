"""不变量：会话三张表把 REQ-04 / REQ-21 的硬约束下沉到库层。

失效形态（用户会看到什么）：

- **标题可空** → 没有标题时界面自己编一个，编出来的只活在这台浏览器里。
  换设备后所有会话同名「新会话」，用户以为历史乱了，其实只是名字丢了（C-V9）。
- **`conversations` 缺 owner 或 owner 可空** → 列表过滤没有可过滤的东西：
  写侧漏传一次就跨租户泄漏，而测试里只要都传同一个值就永远是绿的（C-V1 / C-V10）。
- **assistant 行允许没有 `run_id`** → 一条无法回放的回答。展开轨迹时只能按顺序猜
  「这条大概对应哪次运行」，而 3 条回答对 5 次运行的猜法，猜错没有报错（C-V4）。
- **`run_id` 不唯一** → 一次运行挂两条回答。回放时两条都说自己是那次运行的结果，
  而其中一条必然不是（C-V4）。
- **正文可空** → 失败/拒答那一轮写不进去，于是那一轮直接没有行。用户问「它为什么
  没答上来」时，库里连"问过"都没有（§1.9 第三条）。
- **引用快照存 `chunk_id` 指针** → 重建索引后卡片要么是空的，要么指向**别的内容**，
  而后者不报错。用户打开角标发现原文对不上，比没有引用更糟（C-V3）。
- **校验结论没存或取值不受限** → 重开后「当时标红的误挂」变成一条干净引用，
  它带着担保，而担保是假的（C-V3）。

改坏这个不变量会怎样：把 `title` 改成可空、去掉 `(role = 'assistant' AND run_id
IS NOT NULL)` 这条 CHECK、删掉 `ux_messages_run`、给 `message_citations` 加一列
`chunk_id`——四处都会让对应用例当场红。这不是设想，是本文件写完后逐条改坏验过的。

服务于：C-V1 / C-V3 / C-V4 / C-V9 / C-V10（存储层下限；端点与端到端另验）
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import cast

import pytest

from agentworkbench.core.conversation import MessageRole
from agentworkbench.db import engine

INSERT_RUN = """
insert into agent_runs (id, user_id, tenant_id, question, status, started_at)
values (?, ?, ?, ?, 'succeeded', ?)
"""

INSERT_CONVERSATION = """
insert into conversations (id, user_id, tenant_id, title, created_at, updated_at)
values (?, ?, ?, ?, ?, ?)
"""

INSERT_MESSAGE = """
insert into messages (conversation_id, role, content, run_id, created_at)
values (?, ?, ?, ?, ?)
"""

INSERT_CITATION = """
insert into message_citations
    (message_id, no, file_name, seq, excerpt, check_first, check_second, coverage)
values (?, ?, ?, ?, ?, ?, ?, ?)
"""

NOW = "2026-11-03T09:00:00"


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    # 外键是连接级开关：engine.connect() 会开，裸 connect 必须自己开，
    # 否则 FK 与级联形同不存在，孤儿消息行能插进来
    connection.execute("pragma foreign_keys = on")
    engine.load_vec(connection)
    engine.create_schema(connection)
    yield connection
    connection.close()


def make_run(conn: sqlite3.Connection, run_id: str = "run-1") -> str:
    conn.execute(INSERT_RUN, (run_id, "alice", "team-a", "H5U 的 E2 报警怎么处理", NOW))
    return run_id


def make_conversation(conn: sqlite3.Connection, conversation_id: str = "conv-1") -> str:
    conn.execute(
        INSERT_CONVERSATION,
        (conversation_id, "alice", "team-a", "H5U 的 E2 报警怎么处理", NOW, NOW),
    )
    return conversation_id


def make_answer(
    conn: sqlite3.Connection,
    *,
    conversation_id: str = "conv-1",
    run_id: str = "run-1",
    content: str = "E2 表示通讯超时[1]。",
) -> int:
    cursor = conn.execute(INSERT_MESSAGE, (conversation_id, "assistant", content, run_id, NOW))
    return int(cursor.lastrowid or 0)


def columns(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    rows = cast(list[sqlite3.Row], conn.execute(f"pragma table_info({table})").fetchall())
    return {str(row["name"]): row for row in rows}


def test_the_three_tables_exist(conn: sqlite3.Connection) -> None:
    """会话、消息、引用快照各一张。快照独立成表，不是塞进正文的 JSON。

    独立成表是为了能被逐条比对与统计（C-V3 要比四字段、要数无出处句）。
    塞进 JSON 之后，任何比对都得先在 Python 里解析——那正是「校验与被校验分叉」
    最容易发生的地方。
    """
    names = {
        str(row[0]) for row in conn.execute("select name from sqlite_master where type = 'table'")
    }
    assert {"conversations", "messages", "message_citations"} <= names


def test_owner_columns_are_not_null(conn: sqlite3.Connection) -> None:
    """归属两列必填。可空或可选参数，会让「漏传一次」变成跨租户泄漏。

    这是 C-V1 / C-V10 的存储层下限：写入口还远，但表先不许有"无主的会话"。
    """
    info = columns(conn, "conversations")
    assert info["user_id"]["notnull"] == 1
    assert info["tenant_id"]["notnull"] == 1

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(INSERT_CONVERSATION, ("conv-x", None, "team-a", "标题", NOW, NOW))


def test_title_is_required(conn: sqlite3.Connection) -> None:
    """标题必须有值。可空会催生「没有标题时界面自己编一个」——C# 的形态。

    注意断言的是 NOT NULL，不是"非空串"：空串是调用方的错，由写入口挡；
    库这一层只保证「有一条记录的标题字段没被忘掉」。
    """
    assert columns(conn, "conversations")["title"]["notnull"] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(INSERT_CONVERSATION, ("conv-x", "alice", "team-a", None, NOW, NOW))


def test_kind_defaults_to_knowledge_and_cannot_be_rewritten(conn: sqlite3.Connection) -> None:
    """kind 是创建时二选一的合同，不是运行时可改的 mode（F7 §3.1）。

    默认 knowledge 只为把 F7 之前的会话接上来；新入口显式写 general。
    触发器挡住 UPDATE：改 kind 会让同一条历史里混入两套答案合同。
    """
    make_conversation(conn)
    row = conn.execute("select kind from conversations where id = 'conv-1'").fetchone()
    assert row is not None
    assert row["kind"] == "knowledge"

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("update conversations set kind = 'general' where id = 'conv-1'")
    row = conn.execute("select kind from conversations where id = 'conv-1'").fetchone()
    assert row is not None
    assert row["kind"] == "knowledge"

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "insert into conversations"
            " (id, user_id, tenant_id, kind, title, created_at, updated_at)"
            " values ('conv-bad', 'alice', 'team-a', 'other', '标题', ?, ?)",
            (NOW, NOW),
        )


def test_role_accepts_only_the_two_enum_values(conn: sqlite3.Connection) -> None:
    """role 只有 user / assistant，取值来自 `core.conversation.MessageRole`。

    加一个 `system` 会让"历史消息"里出现用户从没见过的文字，而展示层会照常渲染。
    """
    make_conversation(conn)
    conn.execute(INSERT_MESSAGE, ("conv-1", MessageRole.USER.value, "问题", None, NOW))

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(INSERT_MESSAGE, ("conv-1", "system", "系统提示词", None, NOW))


def test_assistant_message_requires_a_run(conn: sqlite3.Connection) -> None:
    """回答必须有 run_id——没有它，这条回答无法回放（C-V4）。

    「先允许空、以后再补」是行不通的：真的补的时候，老数据里那些空值既分不清
    「失败轮次」还是「忘了写」，也永远接不回某次运行。
    """
    make_conversation(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(INSERT_MESSAGE, ("conv-1", "assistant", "答案", None, NOW))


def test_user_message_must_not_carry_a_run(conn: sqlite3.Connection) -> None:
    """用户那一行不带 run：run 属于"一次作答"，问题不是作答。

    允许它带，就等于允许一次运行挂两条消息，`ux_messages_run` 会挡住，
    但那样报出来的错是"唯一索引冲突"，看不出是语义错。
    """
    make_run(conn)
    make_conversation(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(INSERT_MESSAGE, ("conv-1", "user", "问题", "run-1", NOW))


def test_one_run_belongs_to_exactly_one_answer(conn: sqlite3.Connection) -> None:
    """同一次运行不许挂两条回答。

    否则回放时两条都说"我是那次运行的结果"，而其中一条必然不是——这种错误
    不体现在任何一方的字段里，只体现为"两条都能查出同一批步骤"。
    """
    make_run(conn)
    make_conversation(conn)
    make_answer(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(INSERT_MESSAGE, ("conv-1", "assistant", "另一条回答", "run-1", NOW))


def test_failed_turn_may_have_empty_content_but_not_null(conn: sqlite3.Connection) -> None:
    """失败/拒答那一轮：正文空串可以写，NULL 不行。

    空串 = 「这一轮存在但没有正文」，NULL = 「这一轮没有内容这一项」。
    两者在界面上都是空白，在库里却是"有挂点"与"没挂点"的区别——
    而失败原因恰恰是最需要回看的东西（§1.9 第三条）。
    """
    make_run(conn)
    make_conversation(conn)
    make_answer(conn, content="")

    stored = conn.execute("select content from messages where role = 'assistant'").fetchone()
    assert stored is not None
    assert stored["content"] == ""

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(INSERT_MESSAGE, ("conv-1", "assistant", None, "run-2", NOW))


def test_updated_at_never_precedes_created_at(conn: sqlite3.Connection) -> None:
    """列表按 updated_at 排序，倒挂会让"最近"排到最后。

    这条 CHECK 很便宜，而它挡住的是时钟回拨与手写时间戳两类错误。
    """
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            INSERT_CONVERSATION,
            ("conv-x", "alice", "team-a", "标题", NOW, "2026-11-02T09:00:00"),
        )


def test_list_index_covers_owner_and_a_deterministic_order(conn: sqlite3.Connection) -> None:
    """列表查询必须有覆盖 owner 过滤与确定次序的索引。

    判据不只是"有索引"，而是**列顺序**：`(tenant_id, user_id, updated_at DESC, id DESC)`。
    id 进排序键是为了同一秒创建的两条不换位——否则"换设备一模一样"在测试里
    变成随机红，而随机红最后一定被人用 -x 跳过。
    """
    rows = cast(
        list[sqlite3.Row],
        conn.execute("pragma index_xinfo(ix_conversations_owner_updated)").fetchall(),
    )
    keyed = [row for row in rows if row["key"] == 1]

    assert [str(row["name"]) for row in keyed] == [
        "tenant_id",
        "user_id",
        "updated_at",
        "id",
    ]
    assert [int(row["desc"]) for row in keyed] == [0, 0, 1, 1]


def test_snapshot_has_no_pointer_columns(conn: sqlite3.Connection) -> None:
    """快照表里不许有 chunk_id / document_id——自包含是结构性质，不是纪律。

    存指针的两种坏结果里只有一种会报错：查不到（空卡片）会报错，指向别的内容
    不会。用户看到的是"引用还在，只是原文对不上"，而原文正是他来看的东西（§1.6）。
    """
    info = columns(conn, "message_citations")
    forbidden = {"chunk_id", "chunkid", "document_id", "documentid"}
    assert not (set(info) & forbidden), f"快照里出现了指针列：{set(info) & forbidden}"


def test_snapshot_requires_the_four_judged_fields(conn: sqlite3.Connection) -> None:
    """文件名、片段序号、原文、当时的一级结论，四样都必填。

    这四样就是 C-V3 的比对对象。少一样，"与当时一致"就没有可比的。
    """
    info = columns(conn, "message_citations")
    for field in ("file_name", "seq", "excerpt", "check_first"):
        assert info[field]["notnull"] == 1, f"{field} 可空"

    make_run(conn)
    make_conversation(conn)
    message_id = make_answer(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(INSERT_CITATION, (message_id, 1, "手册.pdf", 3, None, "kept", None, None))


def test_snapshot_check_values_are_limited_to_the_enums(conn: sqlite3.Connection) -> None:
    """两级校验的结论只能取枚举值——否则重开后界面按值分支会静默渲染成"没问题"。

    特别注意 `check_second` 可空：NULL 是「没判过」，'unverified' 是「判了但判不了」。
    把两者混成一个值，D-12 的「不许把未判定当已核实」就没有落点。
    """
    make_run(conn)
    make_conversation(conn)
    message_id = make_answer(conn)

    conn.execute(
        INSERT_CITATION,
        (message_id, 1, "手册.pdf", 3, "原文片段", "kept", None, None),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(INSERT_CITATION, (message_id, 2, "手册.pdf", 4, "原文", "maybe", None, None))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(INSERT_CITATION, (message_id, 3, "手册.pdf", 5, "原文", "kept", "ok", None))


def test_citation_number_is_unique_within_a_message(conn: sqlite3.Connection) -> None:
    """同一编号不许出现两次：编号是「本次证据」的索引空间（D-05）。

    重复编号会让界面上的 [1] 对应两条卡片，而两条都自称是那句话的出处。
    """
    make_run(conn)
    make_conversation(conn)
    message_id = make_answer(conn)
    conn.execute(INSERT_CITATION, (message_id, 1, "手册.pdf", 3, "原文", "kept", "supported", 0.9))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            INSERT_CITATION, (message_id, 1, "另一份.pdf", 8, "别的原文", "kept", None, None)
        )


def test_deleting_a_conversation_takes_messages_and_citations(conn: sqlite3.Connection) -> None:
    """删会话：消息与引用快照跟着走（§1.11 的前三行）。

    这里验的是**外键声明**成立（`foreign_keys=on` 之后级联真的会走）。
    产品路径仍然显式删五处：开关是连接级的，谁忘了开就留孤儿行，而孤儿行不抛异常。
    """
    make_run(conn)
    make_conversation(conn)
    message_id = make_answer(conn)
    conn.execute(INSERT_CITATION, (message_id, 1, "手册.pdf", 3, "原文", "kept", "supported", 0.9))

    conn.execute("delete from conversations where id = 'conv-1'")

    assert conn.execute("select count(*) from messages").fetchone()[0] == 0
    assert conn.execute("select count(*) from message_citations").fetchone()[0] == 0


def test_deleting_a_run_that_an_answer_points_at_is_refused(conn: sqlite3.Connection) -> None:
    """删 run 而回答还指着它 → 报错，不许把回答一起删掉。

    这是 `ON DELETE RESTRICT` 的用处：静默丢一条回答，比删不动难查得多。
    删除顺序因此被定死（先删会话连带消息，再删 run），而顺序写错会当场报错，
    不是安静地少一块。
    """
    make_run(conn)
    make_conversation(conn)
    make_answer(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("delete from agent_runs where id = 'run-1'")
