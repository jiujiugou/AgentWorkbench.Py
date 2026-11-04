"""D2 / P0-2：每个作答入口都写 run，且没有第二条写入路径。

三层证据：

1. **入口矩阵。** `declarations.PRODUCING` 里的每条路由都必须有一个端到端用例；
   调一次 → 恰好一条 run、属于当前团队、能追到一条 assistant 消息。
2. **唯一写入口。** 全仓 AST 扫描 `Recorder(` 的构造点，只允许共享执行流
   `api.answer_flow`；`execute_turn` / `start_turn_or_503` 也只允许被声明过的
   两个入口调用。
3. **负控。** 只读路由打一圈，run 数不变；新增一条产 run 路由而不补用例，
   入口矩阵的集合断言会红。
"""

from __future__ import annotations

import ast
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from harness import FakeChat, TwoTeams, build_two_teams

from agentworkbench.api.routes.declarations import PRODUCING
from agentworkbench.core.errors import AppError, ErrorCode
from agentworkbench.rag.indexer import clear_index

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
QUESTION = "波特率是多少？"


def _runs(teams: TwoTeams) -> list[Any]:
    return list(teams.runs_for(teams.tenant_a))


def _assistant_run_id(teams: TwoTeams, conversation_id: str) -> str | None:
    response = teams.client_a.get(f"/api/conversations/{conversation_id}/messages")
    if response.status_code != 200:
        return None
    for message in response.json()["messages"]:
        if message["role"] == "assistant" and message["runId"] is not None:
            return str(message["runId"])
    return None


def _run_status(teams: TwoTeams, run_id: str) -> str:
    response = teams.client_a.get(f"/api/runs/{run_id}")
    assert response.status_code == 200, response.text
    return str(response.json()["status"])


def _invoke_ask(teams: TwoTeams) -> tuple[str, str]:
    response = teams.client_a.post("/api/knowledge/ask", json={"question": QUESTION})
    assert response.status_code == 200, response.text
    body = response.json()
    return str(body["runId"]), str(body["conversationId"])


def _invoke_runs_until(teams: TwoTeams, terminal: set[str]) -> tuple[str, str]:
    response = teams.client_a.post("/api/runs", json={"question": QUESTION})
    assert response.status_code == 202, response.text
    conversation_id = str(response.json()["conversationId"])

    teams.wait_until(
        lambda: _assistant_run_id(teams, conversation_id) is not None,
        what="异步 run 的占位行",
    )
    run_id = _assistant_run_id(teams, conversation_id)
    assert run_id is not None
    teams.wait_until(
        lambda: _run_status(teams, run_id) in terminal,
        what=f"异步 run 到终态 {sorted(terminal)}",
    )
    return run_id, conversation_id


def _invoke_runs(teams: TwoTeams) -> tuple[str, str]:
    return _invoke_runs_until(teams, {"succeeded", "refused"})


def _messages(teams: TwoTeams, conversation_id: str) -> list[dict[str, Any]]:
    response = teams.client_a.get(f"/api/conversations/{conversation_id}/messages")
    assert response.status_code == 200, response.text
    messages: list[dict[str, Any]] = response.json()["messages"]
    return messages


def _conversation_count(teams: TwoTeams) -> int:
    with closing(teams.open_conn()) as conn:
        row = conn.execute(
            "select count(*) from conversations where tenant_id = ?", (teams.tenant_a,)
        ).fetchone()
    assert row is not None
    return int(row[0])


INVOKERS = {
    ("POST", "/api/knowledge/ask"): _invoke_ask,
    ("POST", "/api/runs"): _invoke_runs,
}


@pytest.fixture()
def teams(tmp_path: Path) -> TwoTeams:
    teams = build_two_teams(tmp_path)
    teams.deps.chat_factory = lambda _conn: (FakeChat(), "glm-4-flash")
    return teams


def test_the_entrypoint_matrix_covers_every_producing_route() -> None:
    """新增一条「产 run」路由而不补端到端用例，这里当场红。"""
    assert set(INVOKERS) == set(PRODUCING), (
        "产 run 路由与端到端用例不一致："
        f"缺用例 {sorted(set(PRODUCING) - set(INVOKERS))}，"
        f"多出用例 {sorted(set(INVOKERS) - set(PRODUCING))}"
    )


def test_a_new_producing_route_without_a_case_would_be_caught() -> None:
    """负控：集合断言不是恒真。"""
    fake = ("POST", "/api/test/answer")
    assert set({**PRODUCING, fake: "假路由"}) - set(INVOKERS) == {fake}


@pytest.mark.parametrize("route", sorted(PRODUCING))
def test_every_producing_route_writes_exactly_one_run(
    teams: TwoTeams, route: tuple[str, str]
) -> None:
    before = len(_runs(teams))
    run_id, conversation_id = INVOKERS[route](teams)
    rows = _runs(teams)

    assert len(rows) == before + 1, f"{route} 没有恰好产出一条 run"
    row = next(row for row in rows if row["id"] == run_id)
    assert row["user_id"] == teams.user_a
    assert row["tenant_id"] == teams.tenant_a
    assert row["status"] in {"succeeded", "refused"}

    messages = teams.client_a.get(f"/api/conversations/{conversation_id}/messages").json()[
        "messages"
    ]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["runId"] == run_id
    assert messages[1]["runStatus"] == row["status"], "历史里的终态与 run 行必须同源"


class ExplodingChat(FakeChat):
    """模型在生成阶段掉线：run 已经起来，失败也必须留挂点。"""

    def complete(self, system: str, user: str) -> str:
        raise AppError(ErrorCode.MODEL_UNAVAILABLE, "模型掉线")


def _raise_model_not_configured(_conn: object) -> tuple[Any, str]:
    raise AppError(ErrorCode.MODEL_NOT_CONFIGURED, "未配置模型")


def test_ask_refusal_still_writes_one_run_and_a_refusal_message(teams: TwoTeams) -> None:
    """拒答不是「没答」，run 与 assistant 行都要在。"""
    with closing(teams.open_conn()) as conn:
        clear_index(conn, teams.doc_a)
        conn.commit()

    before = len(_runs(teams))
    response = teams.client_a.post(
        "/api/knowledge/ask",
        json={"question": QUESTION, "conversationId": teams.conv_a},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "refused"
    assert body["reason"] == "knowledge_insufficient"

    rows = _runs(teams)
    assert len(rows) == before + 1
    row = next(row for row in rows if row["id"] == body["runId"])
    assert row["status"] == "refused"
    assert row["reason"] == "knowledge_insufficient"

    assistant = _messages(teams, teams.conv_a)[-1]
    assert assistant["role"] == "assistant"
    assert assistant["runId"] == body["runId"]
    assert assistant["runStatus"] == "refused"
    assert assistant["content"] == body["answer"]


def test_runs_refusal_still_writes_one_run_and_a_refusal_message(teams: TwoTeams) -> None:
    with closing(teams.open_conn()) as conn:
        clear_index(conn, teams.doc_a)
        conn.commit()

    before = len(_runs(teams))
    run_id, conversation_id = _invoke_runs(teams)
    rows = _runs(teams)

    assert len(rows) == before + 1
    row = next(row for row in rows if row["id"] == run_id)
    assert row["status"] == "refused"
    assert row["reason"] == "knowledge_insufficient"

    assistant = _messages(teams, conversation_id)[-1]
    assert assistant["role"] == "assistant"
    assert assistant["runId"] == run_id
    assert assistant["runStatus"] == "refused"
    assert assistant["content"] != ""


def test_ask_failure_still_writes_one_run_and_an_empty_assistant_row(
    teams: TwoTeams,
) -> None:
    """生成阶段失败：run 收口为 failed，历史里留一行空 assistant。"""
    teams.deps.chat_factory = lambda _conn: (ExplodingChat(), "glm-4-flash")

    before = len(_runs(teams))
    response = teams.client_a.post(
        "/api/knowledge/ask",
        json={"question": QUESTION, "conversationId": teams.conv_a},
    )
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "model_unavailable"

    rows = _runs(teams)
    assert len(rows) == before + 1
    row = next(row for row in rows if row["status"] == "failed")
    assert row["reason"] == "model_unavailable"

    assistant = _messages(teams, teams.conv_a)[-1]
    assert assistant["role"] == "assistant"
    assert assistant["runId"] == row["id"]
    assert assistant["runStatus"] == "failed"
    assert assistant["content"] == ""


def test_runs_failure_still_writes_one_run_and_an_empty_assistant_row(
    teams: TwoTeams,
) -> None:
    teams.deps.chat_factory = lambda _conn: (ExplodingChat(), "glm-4-flash")

    before = len(_runs(teams))
    run_id, conversation_id = _invoke_runs_until(teams, {"failed"})
    rows = _runs(teams)

    assert len(rows) == before + 1
    row = next(row for row in rows if row["id"] == run_id)
    assert row["status"] == "failed"
    assert row["reason"] == "model_unavailable"

    assistant = _messages(teams, conversation_id)[-1]
    assert assistant["role"] == "assistant"
    assert assistant["runId"] == run_id
    assert assistant["runStatus"] == "failed"
    assert assistant["content"] == ""


@pytest.mark.parametrize("route", sorted(PRODUCING))
def test_model_resolution_failure_writes_nothing(teams: TwoTeams, route: tuple[str, str]) -> None:
    """模型都没解析出来：run 与用户消息一行都不写。"""
    teams.deps.chat_factory = _raise_model_not_configured
    before_runs = len(_runs(teams))
    before_conversations = _conversation_count(teams)

    if route == ("POST", "/api/knowledge/ask"):
        response = teams.client_a.post(
            "/api/knowledge/ask",
            json={"question": QUESTION, "conversationId": teams.conv_a},
        )
    else:
        response = teams.client_a.post("/api/runs", json={"question": QUESTION})

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "model_not_configured"
    assert len(_runs(teams)) == before_runs
    assert _conversation_count(teams) == before_conversations


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _call_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _modules_calling(name: str) -> set[str]:
    modules: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        if name in _call_names(path.read_text(encoding="utf-8")):
            modules.add(_module_name(path))
    return modules


def test_the_ast_scan_actually_sees_the_real_call_site() -> None:
    assert "agentworkbench.api.answer_flow" in _modules_calling("Recorder")


def test_only_the_shared_flow_constructs_the_recorder() -> None:
    """全仓只有 `api.answer_flow` 能 new Recorder；任何新入口都必须走它。"""
    assert _modules_calling("Recorder") == {"agentworkbench.api.answer_flow"}


def test_only_declared_entrypoints_call_execute_turn() -> None:
    assert _modules_calling("execute_turn") == {
        "agentworkbench.api.routes.knowledge",
        "agentworkbench.api.routes.runs",
    }


def test_only_declared_entrypoints_call_start_turn_or_503() -> None:
    assert _modules_calling("start_turn_or_503") == {
        "agentworkbench.api.routes.knowledge",
        "agentworkbench.api.routes.runs",
    }


@pytest.mark.parametrize(
    ("method", "path", "params"),
    [
        ("GET", "/api/documents", None),
        ("GET", "/api/conversations", None),
        ("GET", "/api/members", None),
        ("GET", "/api/settings/providers", None),
        ("GET", "/api/settings/embedding", None),
        ("GET", "/api/knowledge/search", {"q": "波特率"}),
    ],
)
def test_read_routes_do_not_write_runs(
    teams: TwoTeams,
    method: str,
    path: str,
    params: dict[str, str] | None,
) -> None:
    """只读路由打一圈，run 数不变。这是入口矩阵的负控。"""
    before = len(_runs(teams))
    response = teams.client_a.request(method, path, params=params)
    assert response.status_code == 200, (method, path, response.status_code, response.text)
    assert len(_runs(teams)) == before, f"{method} {path} 意外产出了 run"
