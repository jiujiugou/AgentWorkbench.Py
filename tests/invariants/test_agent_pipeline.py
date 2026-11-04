"""供应商解析、四段编排，以及**每次作答都留下一次 run**（F3 G5/G6/G10 + F4 REQ-12）。

失效形态（用户会看到什么）：

**记录交给调用方。** 有人图省事在 `api` 里直接调 `ask` 不传 recorder，界面照常
给出答案，库里空空。这条链路就是 C# 的 `POST /api/knowledge/ask`——记录机制齐的，
客户实际在用的那条路偏偏没接。所以 `recorder` 是必传参数，凑不出不留痕的调用。

**判定或校验也记成步骤。** 步骤数不再等于真实调用数，「这次为什么慢/为什么错」
的排查建立在伪造的路径上。A-V7 的判据是步骤种类只有 retrieval / assistant。

**拒答记成成功。** 拒答率与误拒率同时失真——这是 F3 的一条不变量（`refused`
必须独立），在 F4 这边表现为 run 的终态必须如实。

**模型挂了但 run 停在 running。** 与「进程被杀」长得一模一样，事后无法区分
「还在跑」和「早就死了」。

改坏这个不变量会怎样：把 `recorder.finish` 从失败路径删掉 → 失败那条测试红
（status 停在 running）；给判定加一个 step → 种类断言红（多出第三种）。

服务于：A-V7（步骤数 == 调用数）、A-V10（缺值 NULL）、A-V11（写失败即失败）、G5 / G10
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from typing import cast

import pytest

from agentworkbench.agent import runner
from agentworkbench.agent.instructions import (
    ANSWER_WITH_CITATIONS,
    EVIDENCE_LINE,
    PROMPT_VERSION,
    USER_PROMPT_NO_EVIDENCE,
    USER_PROMPT_TEMPLATE,
)
from agentworkbench.core.errors import AppError, ErrorCode
from agentworkbench.core.judgment import JudgmentValue, SufficiencyOptions
from agentworkbench.core.runs import RunReasonCode, RunStatus
from agentworkbench.db.repositories.providers import resolve_active_row
from agentworkbench.guard.sufficiency import judge
from agentworkbench.llm.providers import ChatEndpoint
from agentworkbench.rag.retriever import RetrievalHit, retrieve
from agentworkbench.trace.recorder import Recorder, RecordingFailed

# ── 假实现：不打真 API ──


class FakeChat:
    """记录调用次数与消息的假模型。"""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.answer


def make_recorder(con: sqlite3.Connection) -> Recorder:
    """每条 run 一个写句柄。`ask` 的 recorder 是必传参数，凑不出不留痕的调用。"""
    return Recorder(con, user_id="user-test", tenant_id="tenant-test")


class StubEmbedder:
    """受控向量：每个注册文本映射到正交基向量，query 命中同文本时距离 0、其余 1。"""

    def __init__(self) -> None:
        self._map: dict[str, list[float]] = {}

    def register(self, text: str, index: int, dim: int = 1024) -> None:
        # vec0 列是 FLOAT[1024]：正交基在 1024 维上取，距离语义不变（同 0 异 1）
        vec = [0.0] * dim
        vec[index % dim] = 1.0
        self._map[text] = vec

    def __call__(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            if text not in self._map:
                # 未注册文本给一个全零向量之外的可复现向量（ hashing 顺序）
                out.append([float((len(text) + i) % 2) + 0.5 for i in range(1024)])
            else:
                out.append(self._map[text])
        return out


DB = tuple[sqlite3.Connection, StubEmbedder, list[str]]


def _seed_db(indices: list[int]) -> tuple[sqlite3.Connection, StubEmbedder, list[str]]:
    """三块内容按 indices 注册向量：同号 = 挤在一起，异号 = 正交可分。判定完全可控。"""
    from conftest import insert_document

    from agentworkbench.core.documents import ChunkDraft
    from agentworkbench.db import engine
    from agentworkbench.rag.indexer import write_index

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    engine.load_vec(con)
    engine.create_schema(con)

    embedder = StubEmbedder()
    texts = [
        "H5U 的 E2 报警是通讯超时，检查波特率与站号设置。",
        "FC01 读线圈功能码用于读取输出继电器的状态。",
        "维护保养：每季度清洁散热风道并紧固接线端子。",
    ]
    for _i, (text, idx) in enumerate(zip(texts, indices, strict=True)):
        embedder.register(text, idx)
    embedder.register(texts[0], indices[0])  # query 将用 texts[0]

    drafts = [ChunkDraft(seq=i, page_from=1, page_to=1, content=t) for i, t in enumerate(texts)]
    insert_document(con, "doc-1", tenant_id=TENANT)
    write_index(
        con,
        document_id="doc-1",
        drafts=drafts,
        embeddings=embedder(texts),
        now="2026-11-02T00:00:00",
    )
    con.commit()
    return con, embedder, texts


TENANT = "tenant-test"
"""这些测试的数据租户。检索必填它，夹具就把数据造在这里。"""


@pytest.fixture()
def controlled_db() -> Iterator[tuple[sqlite3.Connection, StubEmbedder, list[str]]]:
    """正交库：query=text_i 时块 i 距离 0、其余距离 1 → 有赢家且够近。"""
    con, embedder, texts = _seed_db([0, 1, 2])
    yield con, embedder, texts
    con.close()


# ── 供应商解析 ──


def test_no_active_provider_raises_configured_error() -> None:
    """没有激活供应商 → MODEL_NOT_CONFIGURED，message 指到该改的地方（不静默默认）。"""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "create table providers ("
        " id integer, name text, base_url text, api_key_encrypted text, is_active integer)"
    )
    con.execute(
        "create table provider_models ("
        " id integer, provider_id integer, model text, is_default integer)"
    )
    con.execute("insert into providers values (1, 'x', 'http://v1', 'k', 0)")
    with pytest.raises(AppError) as ei:
        resolve_active_row(con)
    assert ei.value.code is ErrorCode.MODEL_NOT_CONFIGURED


def test_active_without_default_model_raises() -> None:
    """激活供应商没配默认模型 → 独立报错（两种成因 message 不同）。"""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "create table providers ("
        " id integer, name text, base_url text, api_key_encrypted text, is_active integer)"
    )
    con.execute(
        "create table provider_models ("
        " id integer, provider_id integer, model text, is_default integer)"
    )
    con.execute("insert into providers values (1, 'zhipu', 'http://v4', 'k', 1)")
    con.execute("insert into provider_models values (1, 1, 'glm-4-flash', 0)")
    with pytest.raises(AppError) as ei:
        resolve_active_row(con)
    assert "默认" in str(ei.value)


def test_active_endpoint_resolved_with_default_model() -> None:
    """激活 + 默认模型 → 端点三元组就绪（真实库的 zhipu 场景）。"""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "create table providers ("
        " id integer, name text, base_url text, api_key_encrypted text, is_active integer)"
    )
    con.execute(
        "create table provider_models ("
        " id integer, provider_id integer, model text, is_default integer)"
    )
    con.execute(
        "insert into providers values (2, 'zhipu', 'https://open.bigmodel.cn/api/paas/v4', 'k', 1)"
    )
    con.execute("insert into provider_models values (1, 2, 'glm-4-flash-250414', 1)")
    row = resolve_active_row(con)
    assert row["model"] == "glm-4-flash-250414"
    assert row["name"] == "zhipu"
    assert "bigmodel" in row["base_url"]


def test_client_cache_by_triple() -> None:
    """同一三元组复用同一客户端实例（C# ConcurrentDictionary 同语义）。"""
    from agentworkbench.llm.providers import _clients, client_for

    ep = ChatEndpoint(
        provider_id=9, name="t", base_url="http://cache-test/v1", api_key="k", model="m"
    )
    a = client_for(ep)
    b = client_for(ep)
    assert a is b
    del _clients[ep.cache_key]  # 清理，不污染其他测试


# ── 四段编排（假 chat，不打真 API）──


def test_refused_path_never_calls_model() -> None:
    """判不足 → REFUSED + 固定文案 + 模型调用次数 0（G10 成对断言的管道半边）。

    三块注册同一向量：query 进来三块距离全 0，挤成一团 → margin=0 →
    没有赢家 → 资料不足。这正是「库外问题在向量空间里的形状」的可控复现。
    """
    con, embedder, texts = _seed_db([0, 0, 0])
    chat = FakeChat("不应该被调用")
    result = runner.ask(
        con,
        texts[0],
        chat=chat,
        embedder=embedder,
        recorder=make_recorder(con),
        tenant_id=TENANT,
        refuse_margin=0.30,
        sufficient_distance=0.50,
    )
    assert result.status is RunStatus.REFUSED
    assert result.reason is RunReasonCode.KNOWLEDGE_INSUFFICIENT
    assert len(chat.calls) == 0, "判不足时模型一次都不许被请求"
    assert result.report is None
    assert result.evidence.hits, "拒答时那份命中也是证据"


def test_answered_path_calls_model_exactly_once(controlled_db: DB) -> None:
    """可答 → 模型恰好被请求 1 次（预检索结构：没有第二次机会，G5/G10 管道级）。"""
    con, embedder, texts = controlled_db
    chat = FakeChat("H5U 的 E2 报警是通讯超时[1]。")
    result = runner.ask(
        con,
        texts[0],  # query 与块 0 同文本：距离 0 → 有赢家且够近
        chat=chat,
        embedder=embedder,
        recorder=make_recorder(con),
        tenant_id=TENANT,
        refuse_margin=0.10,
        sufficient_distance=0.50,
    )
    assert result.status is RunStatus.SUCCEEDED
    assert result.verdict is JudgmentValue.ANSWERED
    assert len(chat.calls) == 1
    (_system, user) = chat.calls[0]
    assert "[1]" in user and "【资料】" in user  # 证据连编号进 prompt
    assert result.evidence.hits  # 预检索必发生（G5：tools ≥ 1 的结构保证）


def test_citation_verification_integrated(controlled_db: DB) -> None:
    """④ 校验接在生成后：越界编号在清洗后消失（引用校验不是摆设）。"""
    con, embedder, texts = controlled_db
    chat = FakeChat("通讯超时[1]。另外查天气预报[9]。")
    result = runner.ask(
        con,
        texts[0],
        chat=chat,
        embedder=embedder,
        recorder=make_recorder(con),
        tenant_id=TENANT,
        refuse_margin=0.10,
        sufficient_distance=0.50,
        coverage_threshold=None,
    )
    assert result.report is not None
    assert result.report.out_of_range_count == 1
    assert "[9]" not in result.answer and "[1]" in result.answer


def test_ask_result_is_frozen(controlled_db: DB) -> None:
    """run 产出不可变：轨迹、界面、落库读到的必须是同一个。"""
    import dataclasses

    con, embedder, texts = controlled_db
    result = runner.ask(
        con,
        texts[0],
        chat=FakeChat("答案[1]。"),
        embedder=embedder,
        recorder=make_recorder(con),
        tenant_id=TENANT,
        refuse_margin=0.10,
        sufficient_distance=0.50,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = RunStatus.REFUSED  # type: ignore[misc]


def test_retrieval_hits_shape_matches_judge_input(controlled_db: DB) -> None:
    """检索距离直接喂判定：None（全文路）不炸、可复算（判定契约的集成点）。"""
    con, embedder, texts = controlled_db
    hits: list[RetrievalHit] = retrieve(con, texts[0], embedder=embedder, tenant_id=TENANT, top_k=5)
    distances = [h.vector_distance for h in hits]
    judgment = judge(distances, SufficiencyOptions(0.10, 0.50))
    assert judgment.verdict in {JudgmentValue.ANSWERED, JudgmentValue.WEAK}


# ── 记录：一次作答 = 一次 run（F4 §1.9 t0~t8）─────────────────


class MeteredChat(FakeChat):
    """上报了用量的假模型。`usage_of` 走 duck-typing 读 `last_usage`。"""

    def __init__(self, answer: str, usage: dict[str, int | None]) -> None:
        super().__init__(answer)
        self.last_usage = usage


class BrokenChat(FakeChat):
    """调用就炸的假模型。用来验失败路径也要收口。"""

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        raise AppError(ErrorCode.MODEL_TIMEOUT, "模型请求超时（60.0s）")


def ask_once(
    con: sqlite3.Connection, embedder: StubEmbedder, question: str, chat: FakeChat
) -> runner.AskResult:
    return runner.ask(
        con,
        question,
        chat=chat,
        embedder=embedder,
        recorder=make_recorder(con),
        tenant_id=TENANT,
        refuse_margin=0.10,
        sufficient_distance=0.50,
        model_name="glm-4-flash",
    )


def only_run(con: sqlite3.Connection) -> sqlite3.Row:
    rows = cast(list[sqlite3.Row], con.execute("select * from agent_runs").fetchall())
    assert len(rows) == 1, f"应该恰好一条 run，实际 {len(rows)} 条"
    return rows[0]


def step_kinds(con: sqlite3.Connection) -> list[str]:
    rows = con.execute("select kind from agent_steps order by seq").fetchall()
    return [str(r["kind"]) for r in rows]


def test_answered_run_lands_complete(controlled_db: DB) -> None:
    """可答的一次：一条 run、终态 succeeded、结束时间与 prompt 版本都在。"""
    con, embedder, texts = controlled_db
    result = ask_once(con, embedder, texts[0], FakeChat("通讯超时[1]。"))

    row = only_run(con)
    assert row["id"] == result.run_id
    assert row["status"] == RunStatus.SUCCEEDED.value
    assert row["reason"] is None
    assert row["finished_at"] is not None
    assert row["prompt_version"] == PROMPT_VERSION
    assert row["model"] == "glm-4-flash"
    assert row["user_id"] == "user-test" and row["tenant_id"] == "tenant-test"


def test_step_kinds_are_only_retrieval_and_assistant(controlled_db: DB) -> None:
    """步骤数 == 真实调用数（A-V7）。

    判定与引用校验**各只做过一次计算**，没有调用任何外部东西——给它们造行，
    这条断言当场红。这是本域唯一能抓住「伪造步骤」的判据。
    """
    con, embedder, texts = controlled_db
    chat = FakeChat("通讯超时[1]。")
    ask_once(con, embedder, texts[0], chat)

    kinds = step_kinds(con)
    assert kinds == ["retrieval", "assistant"], f"多出了不该成步的动作：{kinds}"
    assert kinds.count("assistant") == len(chat.calls)


def test_refused_run_has_no_assistant_step() -> None:
    """判不足：模型一次没调，库里就只有一条检索步（A-V7 的负控原文）。

    A-V7 的 negative 是「判不足的 run 里出现 assistant 步（模型根本没被调用过）」。
    """
    con, embedder, texts = _seed_db([0, 0, 0])
    chat = FakeChat("不应该被调用")
    result = ask_once(con, embedder, texts[0], chat)

    assert result.status is RunStatus.REFUSED
    assert len(chat.calls) == 0
    assert step_kinds(con) == ["retrieval"], "模型没被调用，就不许有 assistant 步"

    row = only_run(con)
    assert row["status"] == RunStatus.REFUSED.value
    assert row["reason"] == RunReasonCode.KNOWLEDGE_INSUFFICIENT.value
    assert row["citation_out_of_range"] is None, "拒答没有正文可校验——NULL，不是 0"


def test_retrieval_step_records_where_the_evidence_came_from(controlled_db: DB) -> None:
    """检索步要能回答「当时依据的是哪几段、距离多少」。

    不存正文：`chunks` 会被重解析换掉，存快照会让人以为那就是现在的库。
    """
    import json

    con, embedder, texts = controlled_db
    ask_once(con, embedder, texts[0], FakeChat("通讯超时[1]。"))

    row = con.execute("select output from agent_steps where kind = 'retrieval'").fetchone()
    digest = json.loads(row["output"])
    assert digest["hits"] >= 1
    top = digest["top"][0]
    assert set(top) >= {"chunk_id", "document_id", "page_from", "vector_distance"}
    assert top["vector_distance"] == 0.0, "同文本命中距离必须是 0"


def test_missing_usage_lands_as_null_end_to_end(controlled_db: DB) -> None:
    """供应商不回 usage → run 与 step 的 token 都是 NULL（A-V10 的端到端）。

    `FakeChat` 没有 `last_usage`，那正是「供应商没回」的等价形态。
    """
    con, embedder, texts = controlled_db
    ask_once(con, embedder, texts[0], FakeChat("通讯超时[1]。"))

    row = only_run(con)
    assert row["prompt_tokens"] is None and row["completion_tokens"] is None
    step = con.execute("select * from agent_steps where kind = 'assistant'").fetchone()
    assert step["prompt_tokens"] is None and step["completion_tokens"] is None


def test_reported_usage_is_stored_on_both_rows(controlled_db: DB) -> None:
    """正控：真回了就真存，run 与 step 各一份（汇总量 vs 单次量，不是重复）。"""
    con, embedder, texts = controlled_db
    chat = MeteredChat("通讯超时[1]。", {"prompt_tokens": 1200, "completion_tokens": 40})
    ask_once(con, embedder, texts[0], chat)

    row = only_run(con)
    assert (row["prompt_tokens"], row["completion_tokens"]) == (1200, 40)
    step = con.execute("select * from agent_steps where kind = 'assistant'").fetchone()
    assert (step["prompt_tokens"], step["completion_tokens"]) == (1200, 40)
    assert step["model"] == "glm-4-flash"
    assert step["duration_ms"] is not None and step["duration_ms"] >= 0


def test_citation_counts_are_recorded_on_the_run(controlled_db: DB) -> None:
    """④ 校验不成步，结果落 run 的两个计数列（§1.9 t7）。"""
    con, embedder, texts = controlled_db
    ask_once(con, embedder, texts[0], FakeChat("通讯超时[1]。另外查天气预报[9]。"))

    row = only_run(con)
    assert row["citation_out_of_range"] == 1, "越界的 [9] 要算进计数"
    assert step_kinds(con) == ["retrieval", "assistant"], "校验不许成步"


def test_model_failure_closes_the_run_as_failed(controlled_db: DB) -> None:
    """模型挂了：run 收成 failed + model_timeout，assistant 步记 error。

    不收口的话，这条 run 会永远停在 `running`，与「进程被杀」长得一样。
    """
    con, embedder, texts = controlled_db
    with pytest.raises(AppError):
        ask_once(con, embedder, texts[0], BrokenChat("不会用到"))

    row = only_run(con)
    assert row["status"] == RunStatus.FAILED.value
    assert row["reason"] == RunReasonCode.MODEL_TIMEOUT.value
    assert row["error"] is not None and "超时" in row["error"]
    assert row["finished_at"] is not None

    step = con.execute("select * from agent_steps where kind = 'assistant'").fetchone()
    assert step["error"] is not None and "超时" in step["error"]
    assert step["output"] is None, "失败不许写进 output"


def test_recording_failure_aborts_the_answer(controlled_db: DB) -> None:
    """记录写不进去 → 这次作答不返回结果（A-V11）。

    C# 在这里 `catch { LogWarning }`：用户照拿答案，库里没有这次执行。
    宁可失败，不可静默——「记录没了但回答成功」是事后最查不清的状态。
    """
    con, embedder, texts = controlled_db
    con.execute("pragma query_only = on")

    with pytest.raises(RecordingFailed):
        ask_once(con, embedder, texts[0], FakeChat("通讯超时[1]。"))


def test_each_call_gets_its_own_run(controlled_db: DB) -> None:
    """两次作答两条 run，互不覆盖——`run_id` 就是「这一次」的身份。"""
    con, embedder, texts = controlled_db
    first = ask_once(con, embedder, texts[0], FakeChat("通讯超时[1]。"))
    second = ask_once(con, embedder, texts[0], FakeChat("通讯超时[1]。"))

    assert first.run_id != second.run_id
    rows = con.execute("select id from agent_runs").fetchall()
    assert len(rows) == 2
    assert {r["id"] for r in rows} == {first.run_id, second.run_id}


def test_prompt_version_is_derived_from_the_instruction_text() -> None:
    """版本号是派生的，不是手填的（§三-3：手填必然忘）。

    这里把哈希独立算一遍：谁把它改回 `PROMPT_VERSION = "v3"`，这条当场红。
    改了 prompt 时不需要做任何事——版本自己会变。
    """
    payload = "\x1f".join(
        (ANSWER_WITH_CITATIONS, USER_PROMPT_TEMPLATE, USER_PROMPT_NO_EVIDENCE, EVIDENCE_LINE)
    )
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    assert expected == PROMPT_VERSION
    assert len(PROMPT_VERSION) == 12 and all(c in "0123456789abcdef" for c in PROMPT_VERSION)
