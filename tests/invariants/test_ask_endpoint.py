"""端到端：`POST /api/knowledge/ask` —— 每条作答路径都留下一次 run（A-V1 / A-V11）。

失效形态（用户会看到什么）：

**接口答了、库里没有。** 这就是 C# `POST /api/knowledge/ask` 的形状：记录机制齐的，
客户天天在用的那条路偏偏没接。所以本文件不只验"答案对不对"，更验"这条路
有没有进 run"——拒答、模型超时、引用异常，三种都必须在库里留下痕迹。

**camelCase 对不上。** Python 默认 snake_case，前端读 `runId` 读到 undefined，
页面一片空白、不报错。这是本项目已经踩过一次的坑（F2 端到端抓出来的）。

**记录写不进去但答案照发。** 库存不进这次执行，用户却拿到了回答；
三个月后有人问"那次为什么错"，什么也查不到。所以库不可写时必须是错误响应，
而不是一个看起来正常的 200。

**模型没配也产 run。** 反过来的错法：把"服务没就绪"记成"用户问过一次"。
装配失败不属于一次作答——这里用测试把这条界线钉住，让它是一个决定，
不是一处遗漏。

改坏这个不变量会怎样：把路由里的 `Recorder(...)` 去掉 → 所有 run 断言红；
把 `run_id` 改成 `runid` → camelCase 断言红。

服务于：A-V1（无后门）、A-V11（写失败即失败）、REQ-11
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from conftest import sign_in
from fastapi.testclient import TestClient

from agentworkbench.api.app import create_app
from agentworkbench.api.deps import Deps, build_deps
from agentworkbench.config import Settings
from agentworkbench.core.documents import ChunkDraft
from agentworkbench.core.errors import AppError, ErrorCode
from agentworkbench.db import engine
from agentworkbench.ingest.worker import EmbedderPort
from agentworkbench.rag.indexer import write_index

TEXTS = [
    "H5U 的 E2 报警是通讯超时，检查波特率与站号设置。",
    "FC01 读线圈功能码用于读取输出继电器的状态。",
    "维护保养：每季度清洁散热风道并紧固接线端子。",
]
QUESTION = TEXTS[0]


class ScriptedEmbedder:
    """按文本给向量。同一向量 → 距离 0；正交 → 距离 1。判定因此完全可控。"""

    def __init__(self, indices: list[int], dim: int = 1024) -> None:
        self._vectors: dict[str, list[float]] = {}
        for text, index in zip(TEXTS, indices, strict=True):
            vec = [0.0] * dim
            vec[index % dim] = 1.0
            self._vectors[text] = vec

    def __call__(self, texts: list[str]) -> list[list[float]]:
        fallback = [0.5] * 1024
        return [self._vectors.get(text, fallback) for text in texts]


class FakeChat:
    def __init__(self, answer: str = "通讯超时，检查波特率与站号[1]。") -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []
        self.last_usage: dict[str, int | None] = {"prompt_tokens": 900, "completion_tokens": 30}

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.answer


class Harness:
    """一套 app + client + 直连。`indices` 决定判定结果（全同 = 判不足）。"""

    def __init__(self, tmp_path: Path, indices: list[int]) -> None:
        self.settings = Settings(data_dir=tmp_path / "data")
        self.deps: Deps = build_deps(self.settings)
        self.chat = FakeChat()
        self.deps.chat_factory = lambda _conn: (self.chat, "glm-4-flash")
        self.embedder = ScriptedEmbedder(indices)
        self.deps.embedder = cast(EmbedderPort, self.embedder)
        self.app = create_app(settings=self.settings, deps=self.deps)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        sign_in(self.client, self.app)
        self._seed(indices)

    def _seed(self, indices: list[int]) -> None:
        conn = self.open_direct()
        try:
            # chunks 有外键指向 documents。引擎连接默认开 foreign_keys（生产如此），
            # 所以这里必须真的先有一份文档——`:memory:` 的测试常常靠"没开外键"
            # 混过去，而真实库不会。
            now = "2026-11-02T00:00:00"
            conn.execute(
                "insert into documents(id, tenant_id, file_name, stored_path, sha256,"
                " size_bytes, status, page_count, created_at, updated_at)"
                " values (?,?,?,?,?,?,?,?,?,?)",
                (
                    "doc-1",
                    self.settings.tenant_id,
                    "manual.pdf",
                    # 只是一个字符串列，文件不真存在——检索只读 chunks，
                    # 不读 stored_path。写成固定串是为了让种子数据可重复。
                    "seed/manual.pdf",
                    "sha-doc-1",
                    1024,
                    "parsed",
                    1,
                    now,
                    now,
                ),
            )
            drafts = [
                ChunkDraft(seq=i, page_from=1, page_to=1, content=text)
                for i, text in enumerate(TEXTS)
            ]
            write_index(
                conn,
                document_id="doc-1",
                drafts=drafts,
                embeddings=self.embedder(TEXTS),
                now=now,
            )
            conn.commit()
        finally:
            conn.close()

    def open_direct(self) -> sqlite3.Connection:
        return engine.connect(self.settings.db_path)

    def ask(
        self, question: str = QUESTION, conversation_id: str | None = None
    ) -> tuple[int, dict[str, object]]:
        payload: dict[str, object] = {"question": question}
        if conversation_id is not None:
            payload["conversationId"] = conversation_id
        response = self.client.post("/api/knowledge/ask", json=payload)
        return response.status_code, cast(dict[str, object], response.json())

    def conversations(self) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], self.client.get("/api/conversations").json())

    def history(self, conversation_id: str) -> list[dict[str, object]]:
        body = self.client.get(f"/api/conversations/{conversation_id}/messages").json()
        return cast(list[dict[str, object]], body["messages"])

    def runs(self) -> list[sqlite3.Row]:
        conn = self.open_direct()
        try:
            return cast(list[sqlite3.Row], conn.execute("select * from agent_runs").fetchall())
        finally:
            conn.close()

    def steps(self) -> list[sqlite3.Row]:
        conn = self.open_direct()
        try:
            return cast(
                list[sqlite3.Row],
                conn.execute("select * from agent_steps order by seq").fetchall(),
            )
        finally:
            conn.close()


@pytest.fixture()
def answerable(tmp_path: Path) -> Iterator[Harness]:
    """正交库：query 与块 0 同向量 → 距离 0，其余 1 → 有赢家且够近。"""
    yield Harness(tmp_path, [0, 1, 2])


@pytest.fixture()
def insufficient(tmp_path: Path) -> Iterator[Harness]:
    """三块挤成一团：距离全 0 → 间隔 0 → 判不足。"""
    yield Harness(tmp_path, [0, 0, 0])


def test_answer_comes_back_in_camel_case(answerable: Harness) -> None:
    """前端读的是 `runId` 不是 `run_id`。字段名错了不报错，页面只是空白。"""
    status, body = answerable.ask()

    assert status == 200
    assert body["runId"]
    assert body["status"] == "succeeded"
    assert body["reason"] is None
    assert body["verdict"] in {"answered", "weak", "undetermined"}
    assert "通讯超时" in str(body["answer"])
    sources = cast(list[dict[str, object]], body["sources"])
    assert sources and "pageFrom" in sources[0] and "documentId" in sources[0]
    # 出处要能写出文件名：界面显示「《manual.pdf》第 1 页」，不是一串 uuid
    assert sources[0]["fileName"] == "manual.pdf"
    report = cast(dict[str, object], body["report"])
    assert "outOfRangeCount" in report and "citedSentenceCount" in report


def test_answer_writes_one_run_with_owner_and_prompt_version(answerable: Harness) -> None:
    status, body = answerable.ask()
    assert status == 200

    runs = answerable.runs()
    assert len(runs) == 1
    row = runs[0]
    assert row["id"] == body["runId"]
    assert row["status"] == "succeeded"
    assert row["finished_at"] is not None
    assert row["user_id"] == "local" and row["tenant_id"] == answerable.settings.tenant_id
    assert row["model"] == "glm-4-flash"
    assert row["prompt_version"] and len(row["prompt_version"]) == 12
    assert row["prompt_tokens"] == 900 and row["completion_tokens"] == 30


def test_answer_records_exactly_two_steps(answerable: Harness) -> None:
    """检索 + 生成。判定与引用校验不成步（A-V7 的 HTTP 侧）。"""
    answerable.ask()
    kinds = [row["kind"] for row in answerable.steps()]
    assert kinds == ["retrieval", "assistant"]
    assert len(answerable.chat.calls) == 1


def test_refusal_is_a_200_with_refused_status(insufficient: Harness) -> None:
    """判不足是**业务结果**，不是错误：200 + refused，界面据此显示拒答卡片。"""
    status, body = insufficient.ask()

    assert status == 200, "拒答不该变成 4xx/5xx——它是系统诚实，不是故障"
    assert body["status"] == "refused"
    assert body["reason"] == "knowledge_insufficient"
    assert body["report"] is None, "没有正文可校验，报告整体为 null 而不是六个 0"
    assert insufficient.chat.calls == []


def test_refusal_still_leaves_a_run(insufficient: Harness) -> None:
    """拒答也产 run——这是 F4 最容易漏的一格。

    漏掉的话，库里只有成功与失败，而"我们拒绝回答了多少次"永远算不出来，
    误拒率也就无从谈起。
    """
    _, body = insufficient.ask()

    runs = insufficient.runs()
    assert len(runs) == 1
    assert runs[0]["id"] == body["runId"]
    assert runs[0]["status"] == "refused"
    assert runs[0]["reason"] == "knowledge_insufficient"
    assert runs[0]["citation_out_of_range"] is None
    assert [row["kind"] for row in insufficient.steps()] == ["retrieval"]


def test_model_not_configured_fails_before_any_run(answerable: Harness) -> None:
    """模型没配 → 503 + 不产 run。这是**定下来的界线**，不是漏了。

    装配失败说明服务没就绪，还没进入"一次作答"。反过来把这种请求也记成用户
    问过一次，会让"多少次因为配置问题没答上来"永远算不清。
    """

    def boom(_conn: sqlite3.Connection) -> tuple[FakeChat, str]:
        raise AppError(ErrorCode.MODEL_NOT_CONFIGURED, "没有激活的供应商")

    answerable.deps.chat_factory = boom
    status, body = answerable.ask()

    assert status == 503
    assert body["code"] == "model_not_configured"
    assert answerable.runs() == [], "没进入作答流程就不该有 run"


def test_model_timeout_returns_504_and_the_run_is_failed(answerable: Harness) -> None:
    """模型超时：run 收成 failed + model_timeout，错误码给 504。"""

    class TimeoutChat(FakeChat):
        def complete(self, system: str, user: str) -> str:
            raise AppError(ErrorCode.MODEL_TIMEOUT, "模型请求超时（60.0s）")

    answerable.deps.chat_factory = lambda _conn: (TimeoutChat(), "glm-4-flash")
    status, body = answerable.ask()

    assert status == 504
    assert body["code"] == "model_timeout"

    runs = answerable.runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["reason"] == "model_timeout"
    assert runs[0]["error"] is not None


def test_recording_failure_returns_audit_unavailable(answerable: Harness) -> None:
    """库写不进去 → 503 `audit_unavailable`，并且**不返回答案**（A-V11）。

    这是 F4 §三那条硬规则在 HTTP 上的形状：记录没了，这次回答不算成功。
    C# 的做法是 catch + LogWarning，用户照拿答案——那正是要反的样板。
    """

    def readonly_conn() -> sqlite3.Connection:
        conn = engine.connect(answerable.settings.db_path)
        conn.execute("pragma query_only = on")
        return conn

    answerable.deps.open_conn = readonly_conn  # type: ignore[method-assign]
    status, body = answerable.ask()

    assert status == 503
    assert body["code"] == "audit_unavailable"
    assert body["retryable"] is False
    assert "answer" not in body, "记录失败时不许把答案发出去"


def test_empty_question_is_rejected_before_a_run(answerable: Harness) -> None:
    """空问题在入口 400：让它走到检索层会白开一条 run，再回一个看不懂的失败。"""
    response = answerable.client.post("/api/knowledge/ask", json={"question": "   "})
    assert response.status_code == 400
    assert answerable.runs() == []


# ─────────────────────────────────────────────────────────────
# ask 与会话是同一条链：问一次 → 一条会话 + 一条回答 + 一次 run
#
# F5 接入前，ask 是一次性问答：答案出来了，历史里什么都没有。那正是 C# 的形状
# （缺口①②）。下面这一组断言就是"接上了"的定义——不是"代码里调了 start_turn"，
# 而是"按会话能列出回答，按回答能找到 run，按 run 能展开步骤"。
# ─────────────────────────────────────────────────────────────


def test_first_ask_opens_a_conversation_named_after_the_question(answerable: Harness) -> None:
    """第一次问（不带 conversationId）→ 新建一条会话，标题是问题前 18 字。

    标题必须在服务端派生并落库：留在前端内存里的话，换设备就是「新会话」，
    而 C-V9 要的正是"两台设备看到同一个名字"。
    """
    status, body = answerable.ask()

    assert status == 200
    conversation_id = body["conversationId"]
    assert isinstance(conversation_id, str) and conversation_id

    listed = answerable.conversations()
    assert [row["id"] for row in listed] == [conversation_id]
    assert listed[0]["title"] == QUESTION[:18]


def test_one_turn_leaves_a_question_and_an_answer_pointing_at_its_run(
    answerable: Harness,
) -> None:
    """一次作答 = 两条消息，且 assistant 那条带着它的 `runId`。

    `runId` 是"回答 → 轨迹"唯一的边。它为空的话，展开轨迹时什么也取不到，
    而界面只会显示"没有过程"——看不出是漏写还是真没有。
    """
    _, body = answerable.ask()

    messages = answerable.history(str(body["conversationId"]))

    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == QUESTION
    assert messages[0]["runId"] is None
    assert messages[1]["runId"] == body["runId"]
    assert "通讯超时" in str(messages[1]["content"])


def test_the_answer_carries_its_citations_into_history(answerable: Harness) -> None:
    """引用随回答一起落进历史，四字段齐全，且**没有回表**（库里根本没建过 chunks 的正文快照）。"""
    _, body = answerable.ask()

    messages = answerable.history(str(body["conversationId"]))
    citations = cast(list[dict[str, object]], messages[1]["citations"])

    assert citations, "带角标的回答必须在历史里有卡片"
    assert citations[0]["fileName"] == "manual.pdf"
    assert citations[0]["excerpt"]


def test_follow_up_with_the_same_id_stays_in_the_same_conversation(answerable: Harness) -> None:
    """C-V2 的端点层：追问不新开会话，消息 +2，标题不变。"""
    _, first = answerable.ask()
    conversation_id = str(first["conversationId"])
    status, second = answerable.ask("再问一句", conversation_id=conversation_id)

    assert status == 200
    assert second["conversationId"] == conversation_id
    assert len(answerable.conversations()) == 1
    assert len(answerable.history(conversation_id)) == 4
    assert answerable.conversations()[0]["title"] == QUESTION[:18], "标题不许跟着追问改"


def test_a_foreign_conversation_id_starts_a_new_one_instead_of_failing(
    answerable: Harness,
) -> None:
    """传别人的 id → **新建一条自己的**，不报错、不写进别人的会话。

    报错就等于承认这条 id 存在：报错的那条是真 id，不报的是瞎编的，一比就分出来了。
    """
    conn = answerable.open_direct()
    try:
        conn.execute(
            "insert into conversations(id, tenant_id, user_id, title, created_at, updated_at)"
            " values ('theirs', ?, 'someone-else', '别人的会话', ?, ?)",
            (answerable.settings.tenant_id, "2026-11-02T00:00:00", "2026-11-02T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    status, body = answerable.ask(conversation_id="theirs")

    assert status == 200
    assert body["conversationId"] != "theirs"
    # 别人的会话对当前用户是 404，所以只能按库查它的消息数
    conn = answerable.open_direct()
    try:
        count = conn.execute(
            "select count(*) as n from messages where conversation_id = 'theirs'"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert count == 0


def test_every_answer_can_be_traced_back_to_its_run(answerable: Harness) -> None:
    """C-V11：两次问答 → 两条 assistant，各自的 runId 都能取到轨迹。

    这条是"每条 run 都能落到一条会话"的端到端形状：不是看库里的某个列，
    而是走用户真实会走的路径——按会话列回答，拿 runId 展开轨迹。
    """
    _, first = answerable.ask()
    conversation_id = str(first["conversationId"])
    _, second = answerable.ask("再问一句", conversation_id=conversation_id)

    answers = [m for m in answerable.history(conversation_id) if m["role"] == "assistant"]
    assert len(answers) == 2

    run_ids = [str(m["runId"]) for m in answers]
    assert run_ids == [first["runId"], second["runId"]]
    for run_id in run_ids:
        trace = answerable.client.get(f"/api/runs/{run_id}/trace")
        assert trace.status_code == 200, f"回答指向的 run 取不到轨迹：{run_id}"
        assert trace.json()["runId"] == run_id


def test_refusal_is_saved_as_the_answer_text_with_no_cards(insufficient: Harness) -> None:
    """判不足也落一条回答：正文是拒答文案、没有卡片。

    拒答不落行的话，"我们拒绝了多少次"在历史里看不到，而误拒率正是最该盯的数字。
    """
    status, body = insufficient.ask()

    assert status == 200
    messages = insufficient.history(str(body["conversationId"]))
    answer = messages[1]
    assert answer["role"] == "assistant"
    assert answer["content"] == body["answer"], "历史里的正文必须与当时返回的逐字相同"
    assert answer["content"], "拒答也要有话，空正文会显示成一条没有回复的问题"
    assert answer["citations"] == []


def test_failed_model_still_leaves_an_answer_row(answerable: Harness) -> None:
    """模型超时：错误响应照常，但历史里必须有一行空正文的回答 + failed 的 run。

    没有这一行，"它为什么没答上来"在历史里就是一片空白——用户只看到一条
    没有回复的问题，查不到任何原因（C-V4）。
    """

    class TimeoutChat(FakeChat):
        def complete(self, system: str, user: str) -> str:
            raise AppError(ErrorCode.MODEL_TIMEOUT, "模型请求超时（60.0s）")

    answerable.deps.chat_factory = lambda _conn: (TimeoutChat(), "glm-4-flash")
    status, _ = answerable.ask()
    assert status == 504

    conn = answerable.open_direct()
    try:
        conversation_id = conn.execute("select id from conversations").fetchone()["id"]
    finally:
        conn.close()

    messages = answerable.history(str(conversation_id))
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "", "失败轮次正文是空串，不是占位文案"
    assert messages[1]["runId"], "失败也要挂着那次 run，否则轨迹无处可展开"

    trace = answerable.client.get(f"/api/runs/{messages[1]['runId']}/trace").json()
    assert trace["status"] == "failed"
    assert trace["reason"] == "model_timeout"


def test_model_not_configured_leaves_no_conversation_either(answerable: Harness) -> None:
    """模型没配时**会话也不建**——不只是不产 run。

    反过来的写法（先开会话再解析模型）会在历史里留一条没有回答的问题，
    而那条问题永远不会有人回答：用户下次打开会以为系统丢了回复。
    """

    def boom(_conn: sqlite3.Connection) -> tuple[FakeChat, str]:
        raise AppError(ErrorCode.MODEL_NOT_CONFIGURED, "没有激活的供应商")

    answerable.deps.chat_factory = boom
    status, _ = answerable.ask()

    assert status == 503
    assert answerable.conversations() == []
    conn = answerable.open_direct()
    try:
        assert conn.execute("select count(*) as n from messages").fetchone()["n"] == 0
    finally:
        conn.close()


def test_empty_question_creates_nothing(answerable: Harness) -> None:
    """空问题在入口 400，会话与消息都不许留。"""
    response = answerable.client.post("/api/knowledge/ask", json={"question": "   "})

    assert response.status_code == 400
    assert answerable.conversations() == []


def test_refusal_survives_a_reopen_with_the_same_reason(insufficient: Harness) -> None:
    """C-V13 端到端：这一次是拒答，重开历史之后它还是拒答。

    重开靠的是 `GET /messages`，它按 `run_id` 取那次运行的终态与原因。
    **不在 `messages` 上再存一份状态**：两份会互相矛盾且发现不了（一份写 refused、
    一份写 succeeded，谁都不报错）。正文也必须一字不变——判据是"与当时一致"。
    """
    status, body = insufficient.ask()
    assert status == 200

    conversation_id = str(body["conversationId"])
    answer_row = next(
        message
        for message in insufficient.history(conversation_id)
        if message["role"] == "assistant"
    )

    assert answer_row["runStatus"] == "refused"
    assert answer_row["runReason"] == "knowledge_insufficient"
    assert answer_row["content"] == body["answer"]


def test_run_killed_before_the_answer_still_leaves_a_row(answerable: Harness) -> None:
    """C-V11 崩溃边（端到端）：run 起来之后、正文写下去之前被杀。

    用 `BaseException` 子类模拟"进程被杀"：runner 的 `except Exception` 刻意
    不收口它，于是这次 run 停在 `running`——与真被杀留下的形态一样。

    此时那一行占位必须已经在库里。没有它，这次 run 落不到任何会话，
    删会话也带不走它，界面上完全看不见——不是"少一条记录"，是"这条运行没有主人"。
    """
    from agentworkbench.agent import runner
    from agentworkbench.chat import conversation as chat
    from agentworkbench.chat import messages as writer
    from agentworkbench.core.identity import Owner
    from agentworkbench.trace.recorder import Recorder

    class Killed(BaseException):
        """进程被杀：不是 Exception，所以不会被收口成 failed。"""

    class DyingChat:
        def complete(self, system: str, user: str) -> str:
            raise Killed

    owner = Owner(user_id="local", tenant_id=answerable.settings.tenant_id)
    now = "2026-11-03T09:00:00"
    conn = answerable.open_direct()
    try:
        started = chat.start_turn(conn, owner, question=QUESTION, now=now)
        recorder = Recorder(conn, user_id=owner.user_id, tenant_id=owner.tenant_id)

        def placeholder(run_id: str) -> None:
            """run 一起来就占一行——回调不返回值（与路由的 `_save_placeholder` 同形）。"""
            writer.begin_answer(
                conn, owner, conversation_id=started.conversation_id, run_id=run_id, now=now
            )

        with pytest.raises(Killed):
            runner.ask(
                conn,
                QUESTION,
                chat=DyingChat(),
                embedder=answerable.embedder,
                recorder=recorder,
                # 检索的团队范围就是这条会话的归属：这条测试直接调 runner，
                # 不经过路由，所以它要自己把 owner 的租户传进去。
                tenant_id=owner.tenant_id,
                refuse_margin=answerable.settings.refuse_margin,
                sufficient_distance=answerable.settings.sufficient_distance,
                model_name="glm-4-flash",
                on_started=placeholder,
            )

        rows = conn.execute(
            "select content, run_id from messages where role = 'assistant'"
        ).fetchall()
        assert len(rows) == 1, "占位行必须在正文写下来之前就存在"
        assert rows[0]["content"] == "", "被杀时还没有正文，空串而不是占位文案"
        run_row = conn.execute(
            "select status from agent_runs where id = ?", (rows[0]["run_id"],)
        ).fetchone()
        assert run_row["status"] == "running", "被杀的 run 停在 running，这正是它的证据"
    finally:
        conn.close()
