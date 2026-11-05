"""会话三个只读端点的 HTTP 契约：归属不可推断、引用自包含、轨迹不截断。

失效形态（用户会看到什么）：

- **越权的 404 与不存在的 404 不是同一个响应。** 只要两者差一个字（比如把 id
  回显进消息），任何人就能用一串 id 试出"哪条真的存在"——报错不同的那条就是。
  所以判据是**逐字节相同**，不是"都返回 404"。
  精确化（2026-11-03，S-V4 加了 request_id 之后）：比较前**剥掉 `request_id`**——
  它是每请求随机的 uuid，与资源存在性无关；排除随机字段后的逐字节相同，
  说明其余字段不携带任何可用于区分的信息，判据强度不变。
- **引用卡片回表查 `chunks`。** 当次显示正常，索引一重建就变成空卡片或指向别的内容，
  而"当时确实是这段原文"已经无从证明（C-V3）。
- **轨迹被截断或补 0。** 推送时代用的 600 字预览被当成历史存下来，展开后看不到
  长返回的尾部；或者把"没有正文可校验"补成 0，于是"这次拒答"和"这次引用全对"
  在数据上分不开（C-V5 / D-12）。
- **读路径顺手写库。** 打开历史时更新 `updated_at`，"最后活动时间"变成"最后打开
  时间"，一周后再也说不清用户是哪天真正问过的。

改坏这个不变量会怎样：把 404 消息改成带回显的 f-string → 逐字节相同那条红；
把 `list_citations` 换成回表查 `chunks` → 快照自包含那条红；把 trace 的
`output` 截到 600 字 → 不截断那条红；在读端点里加一行 `repo.touch` → 只读那条红。

服务于：C-V1（隔）/ C-V4 / C-V5（后端一半）/ C-V3（快照自包含）
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from conftest import sign_in
from fastapi.testclient import TestClient

from agentworkbench.api.app import create_app
from agentworkbench.api.deps import Deps, build_deps
from agentworkbench.chat import conversation as chat
from agentworkbench.chat import messages as writer
from agentworkbench.config import Settings
from agentworkbench.core.identity import Owner
from agentworkbench.core.judgment import Evidence, Judgment, JudgmentValue, NumberedHit
from agentworkbench.core.runs import RunReasonCode, RunStatus, StepKind
from agentworkbench.db import engine
from agentworkbench.guard.citation import verify
from agentworkbench.trace.recorder import Recorder

T = TypeVar("T")

T0 = "2026-11-03T09:00:00"
T1 = "2026-11-03T09:05:00"

MANUAL = "维护保养每季度清洁散热风道并紧固接线端子。"
ALARM = "E2 报警表示通讯超时，先确认波特率与站号是否和主站一致。"


def hit(
    no: int, content: str, *, file_name: str = "汇川H5U用户手册.pdf", seq: int = 12
) -> NumberedHit:
    return NumberedHit(
        no=no,
        chunk_id=no * 10,
        document_id="doc-1",
        file_name=file_name,
        content=content,
        seq=seq,
        page_from=12,
        page_to=13,
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


class Harness:
    """真库文件 + TestClient。归属由 `caller_of` 固定成 `local`。

    造数据走真实写入口（`chat.start_turn` / `writer.save_answer` / `Recorder`），
    不手写 SQL：手写的那份会与写入口漂移，而这里要验的正是"写进去的东西
    能不能原样读出来"。
    """

    def __init__(self, tmp_path: Path) -> None:
        self.settings = Settings(data_dir=tmp_path / "data")
        self.deps: Deps = build_deps(self.settings)
        self.app = create_app(settings=self.settings, deps=self.deps)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        sign_in(self.client, self.app)
        # 接口层的调用者。别人的数据用同一个 tenant、不同的 user 造出来，
        # 这样"看不到"只能由 user 过滤解释，不会被 tenant 顺手挡住。
        self.me = Owner(user_id="local", tenant_id=self.settings.tenant_id)
        self.other = Owner(user_id="someone-else", tenant_id=self.settings.tenant_id)

    def conn(self) -> sqlite3.Connection:
        return engine.connect(self.settings.db_path)

    def with_conn(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """借一条直连给测试函数用，用完就关。路由用的是自己开的连接。"""
        conn = self.conn()
        try:
            return fn(conn)
        finally:
            conn.close()

    # ── 造数据 ──

    def seed_answer(
        self,
        *,
        owner: Owner | None = None,
        question: str = ALARM,
        answer_text: str | None = None,
        conversation_id: str | None = None,
        status: RunStatus = RunStatus.SUCCEEDED,
        reason: RunReasonCode | None = None,
        citation_out_of_range: int | None = 0,
        long_output: str | None = None,
    ) -> dict[str, object]:
        """一问一答，全走真实写入路径。返回 conversation_id / run_id / message_id。"""
        who = owner or self.me
        text = answer_text if answer_text is not None else f"{MANUAL}[1]"

        def run(conn: sqlite3.Connection) -> dict[str, object]:
            started = chat.start_turn(
                conn, who, question=question, conversation_id=conversation_id, now=T0
            )
            recorder = Recorder(conn, user_id=who.user_id, tenant_id=who.tenant_id)
            run_id = recorder.start(question=question, model="glm-4-flash", prompt_version="abc123")
            with recorder.step(StepKind.RETRIEVAL, tool_name="search_knowledge", input=question):
                pass
            if long_output is None:
                with recorder.step(StepKind.ASSISTANT, input=question):
                    pass
            else:
                # 长返回：证明端点不截断。真实场景是工具回了一大段原文。
                with recorder.step(StepKind.ASSISTANT, input=question) as step:
                    step.output = long_output

            if status is RunStatus.SUCCEEDED:
                report = verify(text, (hit(1, MANUAL),), 0.5)
                message_id = writer.save_answer(
                    conn,
                    who,
                    conversation_id=started.conversation_id,
                    run_id=run_id,
                    answer=report.cleaned_answer,
                    evidence=evidence_of(hit(1, MANUAL)),
                    report=report,
                    now=T1,
                )
            else:
                message_id = writer.save_answer(
                    conn,
                    who,
                    conversation_id=started.conversation_id,
                    run_id=run_id,
                    # 拒答那一轮的正文是服务端写死的那句话；用 answer_text 可以
                    # 造出"文案改了之后历史变不变"的场景（C-V13 的负控）
                    answer=answer_text or "",
                    evidence=evidence_of(hit(1, MANUAL)),
                    report=None,
                    now=T1,
                )

            recorder.finish(
                status,
                reason=reason,
                prompt_tokens=1200,
                completion_tokens=40,
                citation_out_of_range=citation_out_of_range
                if status is RunStatus.SUCCEEDED
                else None,
                citation_unsupported=None,
            )
            return {
                "conversation_id": started.conversation_id,
                "run_id": run_id,
                "message_id": message_id,
            }

        return self.with_conn(run)


@pytest.fixture()
def h(tmp_path: Path) -> Iterator[Harness]:
    yield Harness(tmp_path)


# ── 列表 ──


def test_list_is_a_bare_camel_case_array(h: Harness) -> None:
    """裸数组 + camelCase。前端 `conversationsApi.list` 直接吃它。

    snake_case 会让前端读到 `undefined` 而不是报错——本项目踩过一次的坑。
    """
    h.seed_answer()

    body = h.client.get("/api/conversations").json()

    assert isinstance(body, list) and len(body) == 1
    assert set(body[0]) == {"id", "title", "kind", "createdAt", "updatedAt"}
    assert body[0]["kind"] == "knowledge"
    assert body[0]["title"] == ALARM[:18]


def test_list_never_shows_another_users_conversation(h: Harness) -> None:
    """别人的会话一条都不出现（C-V1 正控的端点层）。"""
    mine = h.seed_answer()
    other = h.seed_answer(owner=h.other, question="别人的问题")

    ids = [row["id"] for row in h.client.get("/api/conversations").json()]

    assert ids == [mine["conversation_id"]]
    assert other["conversation_id"] not in ids


# ── 消息 + 引用 ──


def test_citations_come_from_the_snapshot_not_the_index(h: Harness) -> None:
    """出处四字段原样返回，且**不需要 `chunks` 存在**（C-V3 的自包含）。

    这里根本没有建过任何 chunk：如果实现回表 join，这条会取到空卡片甚至 500。
    """
    seeded = h.seed_answer()

    body = h.client.get(f"/api/conversations/{seeded['conversation_id']}/messages").json()

    answer = next(m for m in body["messages"] if m["role"] == "assistant")
    assert answer["runId"] == seeded["run_id"]
    citation = answer["citations"][0]
    assert citation["fileName"] == "汇川H5U用户手册.pdf"
    assert citation["seq"] == 12
    assert citation["excerpt"] == MANUAL
    assert citation["checkFirst"] == "kept"
    assert citation["checkSecond"] == "supported"


def test_failed_round_still_has_an_answer_row(h: Harness) -> None:
    """失败那一轮也有一行（正文空、run 在），否则"它为什么没答上来"无处可查。"""
    seeded = h.seed_answer(status=RunStatus.FAILED, reason=RunReasonCode.MODEL_UNAVAILABLE)

    body = h.client.get(f"/api/conversations/{seeded['conversation_id']}/messages").json()

    answer = next(m for m in body["messages"] if m["role"] == "assistant")
    assert answer["content"] == ""
    assert answer["runId"] == seeded["run_id"]
    assert answer["citations"] == []


def test_reading_a_conversation_does_not_touch_updated_at(h: Harness) -> None:
    """读路径不写库。**在打开历史时推 `updated_at`，「最后活动」就变成「最后打开」**。"""
    seeded = h.seed_answer()
    before = h.with_conn(
        lambda conn: conn.execute(
            "select updated_at from conversations where id = ?", (seeded["conversation_id"],)
        ).fetchone()["updated_at"]
    )

    h.client.get(f"/api/conversations/{seeded['conversation_id']}/messages")
    h.client.get("/api/conversations")

    after = h.with_conn(
        lambda conn: conn.execute(
            "select updated_at from conversations where id = ?", (seeded["conversation_id"],)
        ).fetchone()["updated_at"]
    )
    assert after == before


def test_a_conversation_without_messages_is_empty_not_missing(h: Harness) -> None:
    """「存在但没有消息」与「不存在」是两件事。

    现在 `start_turn` 一定会落一条用户消息，所以这种会话只能由 `repo.create`
    造出来。留着这个分支并测它，是因为以后加"新建空会话"时那条语义不该反转——
    而一条没被测的分支，就是一条永远不会被执行的代码。
    """
    from agentworkbench.db.repositories import conversations as repo

    def create(conn: sqlite3.Connection) -> None:
        repo.create(conn, h.me, conversation_id="empty-one", title="空会话", now=T0)
        conn.commit()

    h.with_conn(create)

    response = h.client.get("/api/conversations/empty-one/messages")

    assert response.status_code == 200
    assert response.json() == {
        "conversationId": "empty-one",
        "kind": "knowledge",
        "messages": [],
    }


# ── 404 与越权 ──


# response 类型是 TestClient 的（starlette 的 httpx 版本与注解层的 httpx2 在
# 类型上对不上，与 test_error_responses 同款问题）——参数用 Any。
def _without_request_id(response: Any) -> dict[str, object]:
    """剥掉 request_id 后的响应体：随机字段不参与存在性判据（见模块 docstring）。"""
    import json

    body = json.loads(response.content)
    body.pop("request_id", None)
    return cast(dict[str, object], body)


def test_missing_and_foreign_conversations_answer_byte_identically(h: Harness) -> None:
    """C-V1 的核心断言：剥掉 request_id 后两条响应**逐字段相同**，不是"都 404"。

    只要差一个字（哪怕只是回显各自的 id），别人就能用一串 id 试出哪条真存在。
    """
    foreign = h.seed_answer(owner=h.other)

    theirs = h.client.get(f"/api/conversations/{foreign['conversation_id']}/messages")
    nobody = h.client.get("/api/conversations/0123456789abcdef/messages")

    assert theirs.status_code == nobody.status_code == 404
    assert _without_request_id(theirs) == _without_request_id(nobody)
    # 连消息体里都不许出现那个 id——出现了就说明有回显
    assert str(foreign["conversation_id"]).encode() not in theirs.content


def test_trace_of_a_foreign_run_is_byte_identical_to_missing(h: Harness) -> None:
    """轨迹端点同样：别人的 run 与不存在的 run 一个响应。"""
    foreign = h.seed_answer(owner=h.other)

    theirs = h.client.get(f"/api/runs/{foreign['run_id']}/trace")
    nobody = h.client.get("/api/runs/deadbeefdeadbeef/trace")

    assert theirs.status_code == nobody.status_code == 404
    assert _without_request_id(theirs) == _without_request_id(nobody)


# ── 轨迹 ──


def test_trace_returns_steps_in_seq_order(h: Harness) -> None:
    """步骤按 seq 回放：检索在前、生成在后。顺序不是 `created_at` 给的。"""
    seeded = h.seed_answer()

    body = h.client.get(f"/api/runs/{seeded['run_id']}/trace").json()

    assert [step["seq"] for step in body["steps"]] == [1, 2]
    assert [step["kind"] for step in body["steps"]] == ["retrieval", "assistant"]
    assert body["status"] == "succeeded"
    assert body["promptTokens"] == 1200


def test_answer_to_run_to_trace_is_one_chain(h: Harness) -> None:
    """C-V4：拿列表 → 读消息 → 用它给的 runId 取轨迹，三段能串起来。"""
    h.seed_answer(question=ALARM)

    conversation = h.client.get("/api/conversations").json()[0]
    messages = h.client.get(f"/api/conversations/{conversation['id']}/messages").json()
    run_id = next(m for m in messages["messages"] if m["role"] == "assistant")["runId"]
    trace = h.client.get(f"/api/runs/{run_id}/trace").json()

    assert conversation["title"] == ALARM[:18]
    assert trace["runId"] == run_id
    assert trace["question"] == ALARM


def test_long_output_is_returned_in_full(h: Harness) -> None:
    """C-V5：长返回不截断。推送时代存 600 字预览，展开后看不到尾部。"""
    long_text = "报警码说明：" + "E" * 2000

    seeded = h.seed_answer(long_output=long_text)

    body = h.client.get(f"/api/runs/{seeded['run_id']}/trace").json()
    assistant = next(step for step in body["steps"] if step["kind"] == "assistant")

    assert assistant["output"] == long_text
    assert len(assistant["output"]) > 600


def test_citation_counts_are_null_not_zero_when_there_was_no_answer(h: Harness) -> None:
    """拒答/失败时两个计数是 `NULL`，不是 0（D-12）。

    0 的意思是"校验过、零处问题"。补成 0 之后，"这次拒答"和"这次引用全对"
    在数据上就分不开了。
    """
    seeded = h.seed_answer(status=RunStatus.REFUSED, reason=RunReasonCode.KNOWLEDGE_INSUFFICIENT)

    body = h.client.get(f"/api/runs/{seeded['run_id']}/trace").json()

    assert body["citationOutOfRange"] is None
    assert body["citationUnsupported"] is None
    assert body["status"] == "refused"
    assert body["reason"] == "knowledge_insufficient"


def test_in_flight_step_is_flagged_apart_from_empty_output(h: Harness) -> None:
    """「发出去了没回来」与「返回了空」必须分开。

    前者要查那次调用（进程被杀 / 停止落在等返回那一段），后者要查工具本身。
    合并成一个 `inFlight: false` 加空 output，排查方向就反了。
    """
    seeded = h.seed_answer()

    # 直接改库造"没回来"：recorder 的两段写在正常路径下总会进出成对，
    # 只有进程被杀才会停在中间（A-V6 的场景）。
    def stop_before_returning(conn: sqlite3.Connection) -> None:
        conn.execute(
            "update agent_steps set output = NULL where run_id = ? and kind = 'assistant'",
            (seeded["run_id"],),
        )
        conn.commit()

    h.with_conn(stop_before_returning)

    body = h.client.get(f"/api/runs/{seeded['run_id']}/trace").json()
    assistant = next(step for step in body["steps"] if step["kind"] == "assistant")

    assert assistant["output"] is None
    assert assistant["inFlight"] is True


def test_user_message_never_carries_a_run(h: Harness) -> None:
    """用户消息没有 run（库层 CHECK 保证），接口原样给出 `null`。"""
    seeded = h.seed_answer()

    body = h.client.get(f"/api/conversations/{seeded['conversation_id']}/messages").json()

    user = next(m for m in body["messages"] if m["role"] == "user")
    assert user["runId"] is None
    assert user["content"] == ALARM
    assert user["citations"] == []


def test_list_order_is_stable_when_two_conversations_share_a_second(h: Harness) -> None:
    """同一秒建的两条会话，顺序也必须定死：`updated_at DESC, id DESC`。

    只按时间排的话，同一秒的两条会换位——"换设备一模一样"落到测试里就是随机红，
    而随机红最后一定被人用 `-x` 跳过，于是这条判据实际上没人守。

    id 必须是**可控的**（这里用 "a1" / "b1"），不能靠 `start_turn` 生成的 uuid：
    那样"顺序对不对"就取决于两个随机十六进制串谁大，测试有一半概率靠运气过。
    一条靠运气的断言比没有断言更糟——它会让人以为这条判据被守住了。

    **这条测的是行为，不是守卫，得说清楚。** 实测过：把 `ORDER BY` 整个删掉、
    或只删掉 `id desc`，这条都**不会红**——因为索引
    `(tenant_id, user_id, updated_at DESC, id DESC)` 本身就给出这个顺序，
    优化器走索引，`ORDER BY` 在这里是冗余的。所以真正守住顺序的是**索引定义**，
    由 `test_conversations_schema.py` 里的索引断言负责。这里锁的是"接口实际返回的
    顺序"，它会在索引被改动、或将来加了别的排序依据时红——那时才知道要回头看。
    """
    from agentworkbench.db.repositories import conversations as repo

    def seed_two(conn: sqlite3.Connection) -> None:
        # 同一个 now：时间分不出先后，顺序只能由 id 决定。
        # **插入顺序要故意与 id 降序相反**（先 a1 再 b1）：
        # 否则 SQLite 在时间相同时按 rowid 升序返回，恰好与 id 升序一致，
        # 少了 `id desc` 的实现也能蒙对——那就成了一条永远绿的断言。
        repo.create(conn, h.me, conversation_id="a1", title="先建的", now=T0)
        repo.create(conn, h.me, conversation_id="b1", title="后建的", now=T0)
        conn.commit()

    h.with_conn(seed_two)

    ids = [row["id"] for row in h.client.get("/api/conversations").json()]

    assert ids == ["b1", "a1"]


# ── 删除：对外可用的动作（C-V12） ──
#
# 仓储层的"五处一起删"由 test_conversation_writer.py 守着。这里守的是另一件事：
# **用户在界面上点得到这个动作**。一条只活在仓储层的删除，等于没有删除——
# 按钮每次都失败，而看代码的人以为它测过了。


def test_delete_removes_the_conversation_from_the_list(h: Harness) -> None:
    """正控：DELETE 之后列表里没有它。"""
    seeded = h.seed_answer()
    conversation_id = str(seeded["conversation_id"])

    assert h.client.delete(f"/api/conversations/{conversation_id}").status_code == 204
    ids = [row["id"] for row in h.client.get("/api/conversations").json()]
    assert conversation_id not in ids


def test_delete_makes_messages_and_trace_unreachable(h: Harness) -> None:
    """删的不只是列表项：消息与轨迹两条读路径都必须一起断。

    只从列表里藏起来是最坏的一种"删除"——用户以为删了，换一条路径还能读到
    原文与工具参数（§1.7 那张表的由来）。
    """
    seeded = h.seed_answer()
    conversation_id = str(seeded["conversation_id"])
    run_id = str(seeded["run_id"])

    h.client.delete(f"/api/conversations/{conversation_id}")

    assert h.client.get(f"/api/conversations/{conversation_id}/messages").status_code == 404
    assert h.client.get(f"/api/runs/{run_id}/trace").status_code == 404


def test_deleting_twice_is_indistinguishable_from_never_existing(h: Harness) -> None:
    """负控：第二次删除与"删一条从来没存在过的 id"逐字节相同。

    删除也是探针——回 "已经删过了" 等于承认这条 id 曾经存在（§1.5）。
    """
    seeded = h.seed_answer()
    conversation_id = str(seeded["conversation_id"])
    h.client.delete(f"/api/conversations/{conversation_id}")

    twice = h.client.delete(f"/api/conversations/{conversation_id}")
    never = h.client.delete("/api/conversations/never-existed-at-all")

    assert twice.status_code == never.status_code == 404
    assert _without_request_id(twice) == _without_request_id(never)


def test_deleting_someone_elses_conversation_leaves_it_intact(h: Harness) -> None:
    """负控：别人的会话删不掉，而且那边**一列都没变**（`I-V3`）。

    只数行数的版本有个洞：删一条消息再插一条，行数一样、`updated_at` 可能一样，
    而内容已经换了。所以这里比的是**整行**（`select *`）——它不会因为以后加列而失效，
    加进来的列自动被包含进指纹。

    断言三样：会话那一行的全部列、消息条数、引用条数。引用单独数一次是必要的：
    `ON DELETE CASCADE` 让「删会话」连带删掉它们，而这个连带是行数看不出来的。
    """
    theirs = h.seed_answer(owner=h.other)
    conversation_id = str(theirs["conversation_id"])

    def snapshot(conn: sqlite3.Connection) -> tuple[tuple[object, ...], int, int]:
        row = conn.execute(
            "select * from conversations where id = ?", (conversation_id,)
        ).fetchone()
        assert row is not None, "会话在删除请求之前就该存在"
        messages = conn.execute(
            "select count(*) from messages where conversation_id = ?", (conversation_id,)
        ).fetchone()[0]
        citations = conn.execute(
            "select count(*) from message_citations c join messages m on m.id = c.message_id"
            " where m.conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]
        return tuple(row), int(messages), int(citations)

    before = h.with_conn(snapshot)
    response = h.client.delete(f"/api/conversations/{conversation_id}")
    after = h.with_conn(snapshot)

    assert response.status_code == 404
    assert after == before, "删别人的会话必须是「什么都没发生」，不是「删了一部分」"
    assert before[1] > 0, "这条用例本身要有消息可比，否则等于什么都没验"


# ── 拒答在历史里还是拒答（C-V13） ──
#
# 拒答的正文是一句固定文案。历史只存正文时，重开之后它变成普通回答气泡——
# 字一样，意思从「我没答」变成「我答了这么一句」。差别不在内容，在这是不是一次回答。


def test_refused_answer_is_marked_in_history(h: Harness) -> None:
    """正控：重开历史，拒答那条带着那次运行的终态与原因。"""
    refusal_text = "资料中没有找到能回答这个问题的内容。"
    seeded = h.seed_answer(
        status=RunStatus.REFUSED,
        reason=RunReasonCode.KNOWLEDGE_INSUFFICIENT,
        answer_text=refusal_text,
    )

    body = h.client.get(f"/api/conversations/{seeded['conversation_id']}/messages").json()
    answer_row = next(m for m in body["messages"] if m["role"] == "assistant")

    assert answer_row["runStatus"] == "refused"
    assert answer_row["runReason"] == "knowledge_insufficient"
    # 正文一字不变：判据是"与当时一致"，不是"重新生成一句拒答"
    assert answer_row["content"] == refusal_text


def test_answered_message_is_not_marked_as_refused(h: Harness) -> None:
    """负控：正常回答不许被标成拒答。

    只在正控上断言"拒答有标记"是不够的——把每一条都标成拒答也能过，
    而那等于所有历史回答都变成了"系统说资料里没有"。
    """
    seeded = h.seed_answer()

    body = h.client.get(f"/api/conversations/{seeded['conversation_id']}/messages").json()
    answer_row = next(m for m in body["messages"] if m["role"] == "assistant")

    assert answer_row["runStatus"] == "succeeded"
    assert answer_row["runReason"] is None
