"""不变量：会话仓储的每一次读写都被 owner 拦住，且过滤发生在 SQL 里。

失效形态（用户会看到什么）：

- **过滤写在 Python 里而不是 SQL 里** → 分页的 `COUNT(*)` 仍是全表。用户翻到
  最后一页看到"共 3 条、本页 1 条"，就能反推出有两条他看不到的会话（C-V10）。
- **owner 是可选参数（为空则不过滤）** → 漏传一次就跨租户读写，而所有测试都传
  同一个 owner，漏与不漏结果一样。所以这里每个用例都有**两个** owner（C-V1）。
- **越权与不存在返回不同的东西**（403 vs 404、或"无权限"文案）→ 报错本身
  就是存在性证据：同一条 id 报 403、瞎编一条报 404，两者一比就分出来了。
- **插入不带归属校验** → 内容写进别人的历史，两边界面都正常，没有任何地方报错。
- **删会话只删消息** → 界面说"已删除"，`agent_runs` 里还留着用户的原话与工具参数。
  而删除是不可撤销的承诺，留一份看不见的副本等于承诺是假的（C-V8）。
- **`touch` 顺手改标题** → 标题随最后一次提问变，两个人看同一条会话看到两个标题，
  "换设备一模一样"当场不成立（C-V9）。

改坏这个不变量会怎样：把 `list_for_owner` 的 `where tenant_id = ? and user_id = ?`
删掉、把 `count_for_owner` 改成 `select count(*) from conversations`、把
`insert_message` 的 `where exists` 去掉、把 `delete` 里的 `agent_runs` 那两行删掉
——四处都会让对应用例当场红。这不是设想，是本文件写完后逐条改坏验过的。

服务于：C-V1 / C-V2 / C-V8 / C-V9 / C-V10（仓储层；端点与端到端另验）
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from agentworkbench.core.conversation import (
    TITLE_MAX_CHARS,
    CitationSnapshot,
    MessageRole,
    derive_title,
)
from agentworkbench.core.identity import Owner
from agentworkbench.core.judgment import CitationFirstCheck, CitationSecondCheck
from agentworkbench.db import engine
from agentworkbench.db.repositories import conversations as repo

ALICE = Owner(user_id="alice", tenant_id="team-a")
BOB = Owner(user_id="bob", tenant_id="team-a")
# 不同租户、同名用户：这是最容易漏的一种越权——只按 user_id 过滤的实现会放行
ALICE_ELSEWHERE = Owner(user_id="alice", tenant_id="team-b")

T0 = "2026-11-03T09:00:00"
T1 = "2026-11-03T09:05:00"
T2 = "2026-11-03T09:10:00"


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("pragma foreign_keys = on")
    engine.load_vec(connection)
    engine.create_schema(connection)
    yield connection
    connection.close()


def make_conversation(
    conn: sqlite3.Connection,
    owner: Owner = ALICE,
    *,
    conversation_id: str,
    title: str = "会话",
    now: str = T0,
) -> str:
    repo.create(conn, owner, conversation_id=conversation_id, title=title, now=now)
    return conversation_id


def make_run(conn: sqlite3.Connection, owner: Owner = ALICE, *, run_id: str) -> str:
    conn.execute(
        "insert into agent_runs (id, user_id, tenant_id, question, status, started_at)"
        " values (?, ?, ?, ?, 'succeeded', ?)",
        (run_id, owner.user_id, owner.tenant_id, "问", T0),
    )
    return run_id


DELETABLE_TABLES = (
    "conversations",
    "messages",
    "message_citations",
    "agent_runs",
    "agent_steps",
)


def row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """五张表的行数。表名来自本文件的常量元组，不含任何外部输入——

    写成 f-string 只因为 SQL 的表名不能参数化，不是因为拼接了不可信的东西。
    """
    return {
        table: int(
            conn.execute(f"select count(*) as n from {table}").fetchone()["n"]  # noqa: S608
        )
        for table in DELETABLE_TABLES
    }


# ── 读：只含自己的 ──


def test_list_contains_only_my_conversations(conn: sqlite3.Connection) -> None:
    """正控。少了它，"永远返回空列表"也能过下面所有负控。"""
    make_conversation(conn, ALICE, conversation_id="a1", now=T1)
    make_conversation(conn, ALICE, conversation_id="a2", now=T2)
    make_conversation(conn, BOB, conversation_id="b1", now=T2)

    rows = repo.list_for_owner(conn, ALICE)

    assert [row["id"] for row in rows] == ["a2", "a1"], "新的在前"


def test_another_owner_writing_does_not_change_my_list(conn: sqlite3.Connection) -> None:
    """别人新建会话，我的条数、首末时间、顺序一个都不许变（C-V1 / C-V10）。

    "顺序不变"单独拎出来断言，是因为它最容易在实现成"先取全部再按 owner 过滤"时
    仍然通过——那种实现下顺序恰好也对，只有条数会多。
    """
    make_conversation(conn, ALICE, conversation_id="a1", now=T1)
    make_conversation(conn, ALICE, conversation_id="a2", now=T2)
    before = [(row["id"], row["updated_at"]) for row in repo.list_for_owner(conn, ALICE)]

    make_conversation(conn, BOB, conversation_id="b1", now=T2)
    make_conversation(conn, BOB, conversation_id="b2", now=T2)
    make_conversation(conn, ALICE_ELSEWHERE, conversation_id="x1", now=T2)

    after = [(row["id"], row["updated_at"]) for row in repo.list_for_owner(conn, ALICE)]
    assert after == before


def test_count_is_filtered_not_global(conn: sqlite3.Connection) -> None:
    """总数必须过滤（C-V10 的负控：未过滤的 COUNT 就是这条红）。

    用户不需要这个数字，但它是自动泄漏的：总数走全表时，"共 N 条"里
    就含着他看不见的那些。
    """
    make_conversation(conn, ALICE, conversation_id="a1")
    make_conversation(conn, ALICE, conversation_id="a2")
    make_conversation(conn, BOB, conversation_id="b1")
    make_conversation(conn, ALICE_ELSEWHERE, conversation_id="x1")

    assert repo.count_for_owner(conn, ALICE) == 2
    assert len(repo.list_for_owner(conn, ALICE, limit=1)) == 1, "分页取到的是过滤后的前一条"


def test_foreign_tenant_with_the_same_user_name_is_not_mine(conn: sqlite3.Connection) -> None:
    """只按 `user_id` 过滤的实现会在这里放行——两个租户各有一个 alice。"""
    make_conversation(conn, ALICE, conversation_id="a1")
    make_conversation(conn, ALICE_ELSEWHERE, conversation_id="x1")

    assert [row["id"] for row in repo.list_for_owner(conn, ALICE)] == ["a1"]
    assert repo.get(conn, ALICE, "x1") is None


def test_order_breaks_ties_by_id(conn: sqlite3.Connection) -> None:
    """同一秒建的两条不许换位。换成 `order by updated_at desc` 单独一项时，

    SQLite 对相同键不保证稳定次序——两次查询可能给出两种结果，于是
    "换设备一模一样"在测试里变成随机红。
    """
    make_conversation(conn, ALICE, conversation_id="a1", now=T0)
    make_conversation(conn, ALICE, conversation_id="a2", now=T0)

    for _ in range(3):
        assert [row["id"] for row in repo.list_for_owner(conn, ALICE)] == ["a2", "a1"]


# ── 越权与不存在：必须长得一样 ──


def test_foreign_id_looks_exactly_like_a_missing_id(conn: sqlite3.Connection) -> None:
    """C-V1 的负控：拿别人的 id 取，与瞎编一个 id，返回必须**完全一致**。

    一致到什么程度：`get` 都是 `None`、`list_messages` 都是 `[]`、
    `delete` 都是 `None`。任何一处能区分，就说明存在性泄漏了。
    """
    make_conversation(conn, BOB, conversation_id="b1")
    repo.insert_message(
        conn, BOB, conversation_id="b1", role=MessageRole.USER, content="秘密", run_id=None, now=T0
    )

    assert repo.get(conn, ALICE, "b1") is None
    assert repo.get(conn, ALICE, "does-not-exist") is None
    assert repo.list_messages(conn, ALICE, "b1") == []
    assert repo.list_messages(conn, ALICE, "does-not-exist") == []
    assert repo.list_citations(conn, ALICE, "b1") == []
    assert repo.delete(conn, ALICE, "b1") is None
    assert repo.delete(conn, ALICE, "does-not-exist") is None

    # 而且确实什么都没删
    assert repo.get(conn, BOB, "b1") is not None


# ── 写：插入自带归属 ──


def test_insert_message_refuses_a_foreign_conversation(conn: sqlite3.Connection) -> None:
    """写进别人的会话必须抛错，**并且一行都不许落**。

    静默返回 0 更糟：调用方多半不看返回值，内容照样没写进去，而
    "这次回答丢了"要等到用户重开历史才发现。
    """
    make_conversation(conn, BOB, conversation_id="b1")

    with pytest.raises(KeyError, match="不属于该用户"):
        repo.insert_message(
            conn,
            ALICE,
            conversation_id="b1",
            role=MessageRole.USER,
            content="塞进去",
            run_id=None,
            now=T0,
        )

    assert conn.execute("select count(*) as n from messages").fetchone()["n"] == 0


def test_insert_message_writes_under_my_conversation(conn: sqlite3.Connection) -> None:
    """正控：自己的会话写得进去，且读得回来。"""
    make_conversation(conn, ALICE, conversation_id="a1")

    message_id = repo.insert_message(
        conn,
        ALICE,
        conversation_id="a1",
        role=MessageRole.USER,
        content="H5U 的 E2 报警怎么处理",
        run_id=None,
        now=T0,
    )

    rows = repo.list_messages(conn, ALICE, "a1")
    assert [row["id"] for row in rows] == [message_id]
    assert rows[0]["content"] == "H5U 的 E2 报警怎么处理"


def test_messages_require_a_run_for_the_assistant(conn: sqlite3.Connection) -> None:
    """assistant 行没有 run 时，库层直接拒绝（C-V4 的结构下限）。

    这里验的是"仓储没有替调用方编一个 run_id"：真编一个随机值，`ux_messages_run`
    与 FK 都拦不住它，回放时才表现为"按 run 取步骤得到空"。
    """
    make_conversation(conn, ALICE, conversation_id="a1")

    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_message(
            conn,
            ALICE,
            conversation_id="a1",
            role=MessageRole.ASSISTANT,
            content="答案",
            run_id=None,
            now=T0,
        )


def test_touch_moves_time_but_never_the_title(conn: sqlite3.Connection) -> None:
    """标题写一次之后不再变（C-V9）。`touch` 只动 `updated_at`。"""
    make_conversation(conn, ALICE, conversation_id="a1", title="H5U 的 E2 报警", now=T0)

    assert repo.touch(conn, ALICE, "a1", now=T2) is True

    row = repo.get(conn, ALICE, "a1")
    assert row is not None
    assert row["updated_at"] == T2
    assert row["title"] == "H5U 的 E2 报警"


def test_touch_cannot_move_someone_elses_conversation(conn: sqlite3.Connection) -> None:
    """越权 touch 返回 `False` 且不改动——但它**不能**变成 404 之外的信号。"""
    make_conversation(conn, BOB, conversation_id="b1", now=T0)

    assert repo.touch(conn, ALICE, "b1", now=T2) is False

    row = repo.get(conn, BOB, "b1")
    assert row is not None
    assert row["updated_at"] == T0


# ── 引用快照 ──


def test_citation_snapshot_roundtrips_the_four_judged_fields(conn: sqlite3.Connection) -> None:
    """四字段逐字回来，且**没有**回表查任何东西（C-V3 的仓储部分）。

    `check_second=None` 也要能原样回来：它与 `'unverified'` 是两件事
    （没判过 vs 判了判不了），落库时混成一个值，D-12 就没有落点了。
    """
    make_conversation(conn, ALICE, conversation_id="a1")
    run_id = make_run(conn, ALICE, run_id="r1")
    message_id = repo.insert_message(
        conn,
        ALICE,
        conversation_id="a1",
        role=MessageRole.ASSISTANT,
        content="答",
        run_id=run_id,
        now=T1,
    )
    snapshot = CitationSnapshot(
        no=1,
        file_name="汇川H5U用户手册.pdf",
        seq=7,
        excerpt="E2 表示通讯超时，检查波特率与站号。",
        check_first=CitationFirstCheck.KEPT,
        check_second=CitationSecondCheck.UNSUPPORTED,
        coverage=0.42,
    )

    repo.insert_citations(conn, ALICE, message_id=message_id, snapshots=[snapshot])

    rows = repo.list_citations(conn, ALICE, "a1")
    assert len(rows) == 1
    row = rows[0]
    assert (row["file_name"], row["seq"], row["excerpt"], row["no"]) == (
        "汇川H5U用户手册.pdf",
        7,
        "E2 表示通讯超时，检查波特率与站号。",
        1,
    )
    assert row["check_first"] == CitationFirstCheck.KEPT.value
    assert row["check_second"] == CitationSecondCheck.UNSUPPORTED.value
    assert row["coverage"] == pytest.approx(0.42)


def test_unjudged_second_check_stays_null(conn: sqlite3.Connection) -> None:
    """没有二级结论时存 `NULL`，**不许被写成任何结论**。

    注意 `NULL` 不等于"阈值没配"：阈值没配时 guard 判的是 `'unverified'`，
    那也是一个结论。这里验的是仓储只负责原样落库——替它填一个 `'unverified'`
    会把"没判过"说成"判不了"，填 `'supported'` 更糟（给一句没担保的话盖章，D-12）。
    """
    make_conversation(conn, ALICE, conversation_id="a1")
    message_id = repo.insert_message(
        conn,
        ALICE,
        conversation_id="a1",
        role=MessageRole.ASSISTANT,
        content="答",
        run_id=make_run(conn, ALICE, run_id="r1"),
        now=T1,
    )
    repo.insert_citations(
        conn,
        ALICE,
        message_id=message_id,
        snapshots=[
            CitationSnapshot(
                no=1,
                file_name="手册.pdf",
                seq=1,
                excerpt="原文",
                check_first=CitationFirstCheck.KEPT,
                check_second=None,
            )
        ],
    )

    row = repo.list_citations(conn, ALICE, "a1")[0]
    assert row["check_second"] is None
    assert row["coverage"] is None


def test_citations_cannot_be_attached_to_a_foreign_message(conn: sqlite3.Connection) -> None:
    """给别人的回答挂引用：一条也不许落，并且必须抛错（C-V3 的越权写）。

    少写一条引用不会让任何断言变红——它只会安静地变成一张少了出处卡片的
    历史回答，而那与"当时确实只有两条"无法区分。
    """
    make_conversation(conn, BOB, conversation_id="b1")
    foreign_message = repo.insert_message(
        conn,
        BOB,
        conversation_id="b1",
        role=MessageRole.ASSISTANT,
        content="答",
        run_id=make_run(conn, BOB, run_id="rb"),
        now=T1,
    )

    with pytest.raises(KeyError, match="未写全"):
        repo.insert_citations(
            conn,
            ALICE,
            message_id=foreign_message,
            snapshots=[
                CitationSnapshot(
                    no=1,
                    file_name="手册.pdf",
                    seq=1,
                    excerpt="原文",
                    check_first=CitationFirstCheck.KEPT,
                )
            ],
        )

    assert conn.execute("select count(*) as n from message_citations").fetchone()["n"] == 0


# ── 删除：五处一起消失 ──


def test_delete_removes_messages_citations_runs_and_steps(conn: sqlite3.Connection) -> None:
    """C-V8 的仓储部分：删会话之后，五张表里一行都不许剩。

    漏掉 `agent_runs` 是有真实代价的：那张表里有用户的原话
    （`agent_runs.question`）和工具参数与返回。界面说"已删除"，
    库里还留着一份没人读得到、也永远不会被清掉的副本。
    """
    make_conversation(conn, ALICE, conversation_id="a1")
    run_id = make_run(conn, ALICE, run_id="r1")
    conn.execute(
        "insert into agent_steps (run_id, seq, kind, created_at) values (?, 1, 'retrieval', ?)",
        (run_id, T0),
    )
    message_id = repo.insert_message(
        conn,
        ALICE,
        conversation_id="a1",
        role=MessageRole.ASSISTANT,
        content="答",
        run_id=run_id,
        now=T1,
    )
    repo.insert_citations(
        conn,
        ALICE,
        message_id=message_id,
        snapshots=[
            CitationSnapshot(
                no=1,
                file_name="手册.pdf",
                seq=1,
                excerpt="原文",
                check_first=CitationFirstCheck.KEPT,
            )
        ],
    )

    assert repo.delete(conn, ALICE, "a1") == 1

    assert row_counts(conn) == dict.fromkeys(DELETABLE_TABLES, 0)


def test_delete_still_cleans_up_when_foreign_keys_are_off(conn: sqlite3.Connection) -> None:
    """外键开关没开时，删除必须照样删干净。

    这条是"显式删五处"的唯一证据。开着外键时，`agent_steps` 会被
    `agent_runs` 的级联带走，于是删掉那句显式删除测试**照样全绿**——
    实际写这条测试时就撞上了。而开关是连接级的：`engine.connect()` 会开，
    任何直接 `sqlite3.connect()` 的路径（脚本、迁移、以后的批处理）都不会开，
    忘了开就留孤儿行，且不报错。

    所以这里把开关关掉再删一次。关掉之后级联不存在，留下来的每一行
    都只能是显式删除删掉的——少一句就当场红。
    """
    make_conversation(conn, ALICE, conversation_id="a1")
    run_id = make_run(conn, ALICE, run_id="r1")
    conn.execute(
        "insert into agent_steps (run_id, seq, kind, created_at) values (?, 1, 'retrieval', ?)",
        (run_id, T0),
    )
    message_id = repo.insert_message(
        conn,
        ALICE,
        conversation_id="a1",
        role=MessageRole.ASSISTANT,
        content="答",
        run_id=run_id,
        now=T1,
    )
    repo.insert_citations(
        conn,
        ALICE,
        message_id=message_id,
        snapshots=[
            CitationSnapshot(
                no=1,
                file_name="手册.pdf",
                seq=1,
                excerpt="原文",
                check_first=CitationFirstCheck.KEPT,
            )
        ],
    )

    # 先提交再关开关：`pragma foreign_keys` 在事务里是**静默无效**的
    # （SQLite 不报错，值也不变），而前面那些 insert 已经开了事务。
    # 关完读回来断言——不读回来，这句 pragma 可能什么都没做，而测试照样绿：
    # 那正是本条想防的失效形态，只不过发生在测试自己身上。
    conn.commit()
    conn.execute("pragma foreign_keys = off")
    assert conn.execute("pragma foreign_keys").fetchone()[0] == 0

    assert repo.delete(conn, ALICE, "a1") == 1

    assert row_counts(conn) == dict.fromkeys(DELETABLE_TABLES, 0)


def test_delete_by_a_foreign_owner_deletes_nothing(conn: sqlite3.Connection) -> None:
    """别人的删除动作不许动我的任何一行——包括间接的。"""
    make_conversation(conn, BOB, conversation_id="b1")
    run_id = make_run(conn, BOB, run_id="rb")
    message_id = repo.insert_message(
        conn,
        BOB,
        conversation_id="b1",
        role=MessageRole.ASSISTANT,
        content="答",
        run_id=run_id,
        now=T1,
    )
    repo.insert_citations(
        conn,
        BOB,
        message_id=message_id,
        snapshots=[
            CitationSnapshot(
                no=1,
                file_name="手册.pdf",
                seq=1,
                excerpt="原文",
                check_first=CitationFirstCheck.KEPT,
            )
        ],
    )

    assert repo.delete(conn, ALICE, "b1") is None

    assert row_counts(conn) == {
        "conversations": 1,
        "messages": 1,
        "message_citations": 1,
        "agent_runs": 1,
        "agent_steps": 0,
    }


def test_delete_reports_how_many_messages_it_took(conn: sqlite3.Connection) -> None:
    """删了几条要能报出来：日志那行写的是"删了几条消息"（§1.7 第三条）。

    报不出来时，日志只能写"删了一条会话"——而用户说"我删的那条里面有 40 轮对话"，
    这边回答不了。
    """
    make_conversation(conn, ALICE, conversation_id="a1")
    for index in range(3):
        repo.insert_message(
            conn,
            ALICE,
            conversation_id="a1",
            role=MessageRole.USER,
            content=f"问 {index}",
            run_id=None,
            now=T1,
        )

    assert repo.delete(conn, ALICE, "a1") == 3


# ── 标题派生 ──


def test_title_is_the_first_18_chars_with_whitespace_collapsed() -> None:
    """标题 = 首条问题前 18 字，换行折叠成空格（C-V9 的规则部分）。

    折叠换行不是美化：列表按单行高度排，带换行的标题会把行撑开，
    而"标题相同"这条判据在两台设备上仍然成立——只是两边都难看。
    """
    question = "H5U 的 E2 报警怎么处理\n我这边是 Modbus 通讯，波特率 9600"

    title = derive_title(question)

    assert len(title) == TITLE_MAX_CHARS
    assert "\n" not in title
    # 18 个字，数出来的：H5U(3) + 空格(1) + 的(1) + 空格(1) + E2(2) + 空格(1)
    # + 报警怎么处理(6) + 空格(1) + 我这(2) = 18。第 19 个字「边」被截掉
    assert title == "H5U 的 E2 报警怎么处理 我这"


def test_empty_question_has_no_title() -> None:
    """空问题派生不出标题：抛错而不是编一个"新会话"。

    空问题在入口就该被拒。给它一个默认标题，等于把"校验漏了"
    变成一条看起来正常的历史记录。
    """
    with pytest.raises(ValueError, match="空问题"):
        derive_title("   \n  ")


def test_owner_rejects_an_empty_field() -> None:
    """空 owner 在构造处拒绝。`= ''` 匹配不到任何行，于是每次读写都静默失败，
    而现象看起来是"数据没了"。"""
    with pytest.raises(ValueError, match="不能为空"):
        Owner(user_id="", tenant_id="team-a")
    with pytest.raises(ValueError, match="不能为空"):
        Owner(user_id="alice", tenant_id="")
