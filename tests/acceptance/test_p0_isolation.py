"""D2 / P0-1：两团队全端点 IDOR 扫描 + 拒绝日志。

判据不是「挑几条路由都是 404」，而是四件事一起成立：

1. **分类完整。** 每一条带路径参数的资源路由都必须表态：团队资源、平台资源、
   还是公开令牌。新增一条 `{id}` 路由而不来这里分类，测试当场红。
2. **不可区分。** 拿团队乙的真实 id 与随机 id 请求团队甲，剥掉 `request_id`
   后逐字段相同。只断言「都是 404」的版本，在消息里回显 id 时依然绿。
3. **拒绝有记录。** 每次越权 404 都能按 `X-Request-Id` 在 `identity_events`
   里查到人、动作、资源、原因。只有 404 没有记录，等于系统手上没有证据。
4. **写路由不落笔。** 越权删除 / 重解析 / 停用之后，团队乙的行必须原样还在。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from harness import TEAM_B_ONLY_WORD, FakeChat, TwoTeams, build_two_teams, without_request_id

from agentworkbench.api.routes.declarations import api_routes

TENANT_SCOPED = {
    ("GET", "/api/documents/{doc_id}"),
    ("GET", "/api/documents/{doc_id}/pages"),
    ("GET", "/api/documents/{doc_id}/chunks"),
    ("GET", "/api/documents/{doc_id}/file"),
    ("POST", "/api/documents/{doc_id}/reparse"),
    ("DELETE", "/api/documents/{doc_id}"),
    ("GET", "/api/conversations/{conversation_id}/messages"),
    ("DELETE", "/api/conversations/{conversation_id}"),
    ("GET", "/api/runs/{run_id}"),
    ("GET", "/api/runs/{run_id}/trace"),
    ("PATCH", "/api/members/{user_id}"),
    ("POST", "/api/members/{user_id}/disable"),
    ("POST", "/api/members/{user_id}/enable"),
    ("POST", "/api/members/{user_id}/reset-password"),
    ("DELETE", "/api/members/invites/{invite_id}"),
    ("PATCH", "/api/knowledge-bases/{kb_id}"),
    ("POST", "/api/knowledge-bases/{kb_id}/archive"),
    ("POST", "/api/knowledge-bases/{kb_id}/unarchive"),
}
"""走团队归属的资源路由。**必须逐条列出**，不能从「有路径参数」推。"""

GLOBAL_PATH_PARAM = {
    ("POST", "/api/settings/providers/{provider_id}/activate"): (
        "平台级资源：providers 没有 tenant 列"
    ),
    ("POST", "/api/settings/providers/{provider_id}/models"): (
        "平台级资源：provider_models 没有 tenant 列"
    ),
    ("PUT", "/api/settings/providers/{provider_id}"): "平台级资源：providers 没有 tenant 列",
    ("DELETE", "/api/settings/providers/{provider_id}"): "平台级资源：providers 没有 tenant 列",
    ("DELETE", "/api/settings/models/{model_id}"): "平台级资源：provider_models 没有 tenant 列",
    ("PUT", "/api/settings/providers/{provider_id}/models/{model_id}/default"): (
        "平台级资源：provider_models 没有 tenant 列"
    ),
    ("POST", "/api/settings/providers/{provider_id}/test"): (
        "平台级资源：探测的是这套部署的供应商，不涉及任何团队数据"
    ),
    ("POST", "/api/settings/providers/{provider_id}/fetch-models"): (
        "平台级资源：同上，产物是上游模型清单"
    ),
    ("POST", "/api/auth/invites/{token}"): "公开入口：邀请令牌本身是全局一次性凭证",
}
"""有路径参数、但不属于团队边界的路由。**每一条都要写理由。**"""


@dataclass(frozen=True)
class Case:
    method: str
    template: str
    param: str
    resource_kind: str
    foreign_id: str
    random_prefix: str
    payload: dict[str, object] | None = None
    state_unchanged: Callable[[TwoTeams, str], None] | None = None


def _doc_status(teams: TwoTeams, doc_id: str) -> str:
    with closing(teams.open_conn()) as conn:
        row = conn.execute("select status from documents where id = ?", (doc_id,)).fetchone()
    assert row is not None, "被越权操作的那份文档不见了"
    return str(row["status"])


def _doc_still_parsed(teams: TwoTeams, doc_id: str) -> None:
    assert _doc_status(teams, doc_id) == "parsed", "越权重解析把别人的状态改了"


def _doc_still_exists(teams: TwoTeams, doc_id: str) -> None:
    with closing(teams.open_conn()) as conn:
        row = conn.execute("select 1 from documents where id = ?", (doc_id,)).fetchone()
    assert row is not None, "越权删除把别人的文档删掉了"


def _conversation_still_exists(teams: TwoTeams, conversation_id: str) -> None:
    with closing(teams.open_conn()) as conn:
        row = conn.execute(
            "select 1 from conversations where id = ?", (conversation_id,)
        ).fetchone()
    assert row is not None, "越权删除把别人的会话删掉了"


def _kb_b(teams: TwoTeams) -> str:
    with closing(teams.open_conn()) as conn:
        row = conn.execute(
            "select id from knowledge_bases where tenant_id = ? and is_default = 1",
            (teams.tenant_b,),
        ).fetchone()
    assert row is not None, "团队乙没有默认知识库"
    return str(row["id"])


def _kb_not_archived(teams: TwoTeams, kb_id: str) -> None:
    with closing(teams.open_conn()) as conn:
        row = conn.execute(
            "select archived_at from knowledge_bases where id = ?", (kb_id,)
        ).fetchone()
    assert row is not None, "???????"
    assert row["archived_at"] is None, "????????????"


def _member_unchanged(teams: TwoTeams, user_id: str) -> None:
    with closing(teams.open_conn()) as conn:
        row = conn.execute("select role, status from users where id = ?", (user_id,)).fetchone()
    assert row is not None, "成员行不见了"
    assert (row["role"], row["status"]) == ("member", "active"), "越权管理改了别人的成员"


def _cases(teams: TwoTeams) -> dict[str, Case]:
    return {
        "document_get": Case(
            "GET", "/api/documents/{doc_id}", "doc_id", "document", teams.doc_b, "doc"
        ),
        "document_pages": Case(
            "GET", "/api/documents/{doc_id}/pages", "doc_id", "document", teams.doc_b, "doc"
        ),
        "document_chunks": Case(
            "GET", "/api/documents/{doc_id}/chunks", "doc_id", "document", teams.doc_b, "doc"
        ),
        "document_download": Case(
            "GET", "/api/documents/{doc_id}/file", "doc_id", "document", teams.doc_b, "doc"
        ),
        "document_reparse": Case(
            "POST",
            "/api/documents/{doc_id}/reparse",
            "doc_id",
            "document",
            teams.doc_b,
            "doc",
            state_unchanged=_doc_still_parsed,
        ),
        "document_delete": Case(
            "DELETE",
            "/api/documents/{doc_id}",
            "doc_id",
            "document",
            teams.doc_b,
            "doc",
            state_unchanged=_doc_still_exists,
        ),
        "conversation_messages": Case(
            "GET",
            "/api/conversations/{conversation_id}/messages",
            "conversation_id",
            "conversation",
            teams.conv_b,
            "conv",
        ),
        "conversation_delete": Case(
            "DELETE",
            "/api/conversations/{conversation_id}",
            "conversation_id",
            "conversation",
            teams.conv_b,
            "conv",
            state_unchanged=_conversation_still_exists,
        ),
        "run_status": Case("GET", "/api/runs/{run_id}", "run_id", "run", teams.run_b, "run"),
        "run_trace": Case("GET", "/api/runs/{run_id}/trace", "run_id", "run", teams.run_b, "run"),
        "member_role": Case(
            "PATCH",
            "/api/members/{user_id}",
            "user_id",
            "user",
            teams.member_b,
            "user",
            payload={"role": "member"},
            state_unchanged=_member_unchanged,
        ),
        "member_disable": Case(
            "POST",
            "/api/members/{user_id}/disable",
            "user_id",
            "user",
            teams.member_b,
            "user",
            state_unchanged=_member_unchanged,
        ),
        "member_enable": Case(
            "POST",
            "/api/members/{user_id}/enable",
            "user_id",
            "user",
            teams.member_b,
            "user",
            state_unchanged=_member_unchanged,
        ),
        "member_reset": Case(
            "POST",
            "/api/members/{user_id}/reset-password",
            "user_id",
            "user",
            teams.member_b,
            "user",
            payload={"password": "a-long-enough-password"},
            state_unchanged=_member_unchanged,
        ),
        "invite_revoke": Case(
            "DELETE",
            "/api/members/invites/{invite_id}",
            "invite_id",
            "invite",
            "invite-missing",
            "i",
        ),
        "kb_update": Case(
            "PATCH",
            "/api/knowledge-bases/{kb_id}",
            "kb_id",
            "knowledge_base",
            _kb_b(teams),
            "kb",
            payload={"name": "renamed"},
        ),
        "kb_archive": Case(
            "POST",
            "/api/knowledge-bases/{kb_id}/archive",
            "kb_id",
            "knowledge_base",
            _kb_b(teams),
            "kb",
            state_unchanged=_kb_not_archived,
        ),
        "kb_unarchive": Case(
            "POST",
            "/api/knowledge-bases/{kb_id}/unarchive",
            "kb_id",
            "knowledge_base",
            _kb_b(teams),
            "kb",
            state_unchanged=_kb_not_archived,
        ),
    }


CASE_NAMES = (
    "document_get",
    "document_pages",
    "document_chunks",
    "document_download",
    "document_reparse",
    "document_delete",
    "conversation_messages",
    "conversation_delete",
    "run_status",
    "run_trace",
    "member_role",
    "member_disable",
    "member_enable",
    "member_reset",
    "invite_revoke",
    "kb_update",
    "kb_archive",
    "kb_unarchive",
)


@pytest.fixture()
def teams(tmp_path: Path) -> TwoTeams:
    return build_two_teams(tmp_path)


def test_every_path_param_route_is_classified(teams: TwoTeams) -> None:
    """新增带 `{id}` 的路由必须来这里表态——否则它会悄悄绕过整张扫描表。"""
    present = {route for route in api_routes(teams.app) if "{" in route[1]}
    unknown = present - TENANT_SCOPED - set(GLOBAL_PATH_PARAM)
    assert unknown == set(), f"这些带路径参数的路由没有分类：{sorted(unknown)}"

    assert present >= TENANT_SCOPED, "分类表里有代码中已不存在的路由"
    assert present >= set(GLOBAL_PATH_PARAM), "平台路由分类表已漂移"


def test_the_case_table_covers_every_tenant_scoped_route(teams: TwoTeams) -> None:
    """扫描用例必须覆盖分类表里的每一条，不能只挑几条。"""
    cases = _cases(teams)
    assert set(cases) == set(CASE_NAMES)
    assert {(case.method, case.template) for case in cases.values()} == TENANT_SCOPED


@pytest.mark.parametrize("name", CASE_NAMES)
def test_foreign_resource_is_404_identical_and_logged(teams: TwoTeams, name: str) -> None:
    case = _cases(teams)[name]
    foreign_path = case.template.format(**{case.param: case.foreign_id})
    random_id = f"{case.random_prefix}-{uuid4().hex[:12]}"
    missing_path = case.template.format(**{case.param: random_id})
    kwargs: dict[str, Any] = {} if case.payload is None else {"json": case.payload}

    foreign = teams.client_a.request(case.method, foreign_path, **kwargs)
    missing = teams.client_a.request(case.method, missing_path, **kwargs)

    assert foreign.status_code == missing.status_code == 404, (
        case.method,
        case.template,
        foreign.status_code,
        foreign.text,
    )
    assert without_request_id(foreign) == without_request_id(missing), (
        "别人的 id 与随机 id 的响应可区分——这本身就是一条存在性侧信道"
    )
    assert case.foreign_id.encode() not in foreign.content, "404 响应回显了真实 id"

    events = teams.events_for(foreign.headers["x-request-id"])
    assert len(events) == 1, f"越权请求没有留下唯一一条拒绝记录：{events}"
    event = events[0]
    assert event["action"] == f"{case.method} {case.template}"
    assert event["reason_code"] == "resource_missing"
    assert event["resource_kind"] == case.resource_kind
    assert event["resource_id"] == case.foreign_id
    assert event["actor_user_id"] == teams.user_a
    assert event["tenant_id"] == teams.tenant_a

    if case.state_unchanged is not None:
        case.state_unchanged(teams, case.foreign_id)


def test_200_cross_team_requests_all_404_and_logged(teams: TwoTeams) -> None:
    """`I-H3` 的自动化抽样：200 次真实跨团队请求，100% 404 且每次都有记录。"""
    cases = list(_cases(teams).values())
    for index in range(200):
        case = cases[index % len(cases)]
        path = case.template.format(**{case.param: case.foreign_id})
        kwargs: dict[str, Any] = {} if case.payload is None else {"json": case.payload}
        response = teams.client_a.request(case.method, path, **kwargs)

        assert response.status_code == 404, (index, case.method, case.template)
        events = teams.events_for(response.headers["x-request-id"])
        assert len(events) == 1, (index, case.method, case.template)
        assert events[0]["reason_code"] == "resource_missing"
        assert events[0]["resource_id"] == case.foreign_id

    for case in cases:
        if case.state_unchanged is not None:
            case.state_unchanged(teams, case.foreign_id)


def test_team_a_can_still_read_its_own_resources(teams: TwoTeams) -> None:
    """正控：上面那组 404 不能靠「什么都取不到」通过。"""
    assert teams.client_a.get(f"/api/documents/{teams.doc_a}").status_code == 200
    assert teams.client_a.get(f"/api/documents/{teams.doc_a}/pages").status_code == 200
    chunks = teams.client_a.get(f"/api/documents/{teams.doc_a}/chunks")
    assert chunks.status_code == 200 and chunks.json(), "自己的块必须读得到"
    assert teams.client_a.get(f"/api/conversations/{teams.conv_a}/messages").status_code == 200
    assert teams.client_a.get(f"/api/runs/{teams.run_a}").status_code == 200
    assert teams.client_a.get(f"/api/runs/{teams.run_a}/trace").status_code == 200

    member_ids = {row["id"] for row in teams.client_a.get("/api/members").json()}
    assert teams.user_a in member_ids
    assert teams.user_b not in member_ids
    assert teams.member_b not in member_ids

    hits = teams.client_a.get("/api/knowledge/search", params={"q": "波特率"}).json()
    assert hits and all(hit["documentId"] == teams.doc_a for hit in hits)


def test_search_never_returns_another_teams_fragment(teams: TwoTeams) -> None:
    hits = teams.client_a.get("/api/knowledge/search", params={"q": TEAM_B_ONLY_WORD}).json()
    assert hits == []
    assert TEAM_B_ONLY_WORD not in str(hits)


def test_ask_with_a_foreign_conversation_id_starts_a_new_one(teams: TwoTeams) -> None:
    """传别人的会话 id → 新建一条自己的；别人的消息数一条都不许多。"""
    teams.deps.chat_factory = lambda _conn: (FakeChat(), "glm-4-flash")
    before = _message_count(teams, teams.conv_b)

    response = teams.client_a.post(
        "/api/knowledge/ask",
        json={"question": "A 的追问", "conversationId": teams.conv_b},
    )

    assert response.status_code == 200, response.text
    assert response.json()["conversationId"] != teams.conv_b
    assert _message_count(teams, teams.conv_b) == before
    assert teams.conv_b not in {
        row["id"] for row in teams.client_a.get("/api/conversations").json()
    }


def _message_count(teams: TwoTeams, conversation_id: str) -> int:
    with closing(teams.open_conn()) as conn:
        row = conn.execute(
            "select count(*) as n from messages where conversation_id = ?",
            (conversation_id,),
        ).fetchone()
    assert row is not None
    return int(row["n"])
