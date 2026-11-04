"""D3 / P0-6: lockout, disable/enable, password change, and role boundaries."""

# ruff: noqa: S105, S106 -- test-only credentials, not real accounts.

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from harness import CAROL, PASSWORD, TwoTeams, build_two_teams, without_request_id

NEW_PASSWORD = "a totally different passphrase"
UNKNOWN_EMAIL = "nobody@example.com"


def _login(
    client: TestClient,
    email: str = CAROL,
    password: str = PASSWORD,
) -> Any:
    return client.post("/api/auth/login", json={"email": email, "password": password})


def _signed_in(teams: TwoTeams, email: str = CAROL) -> TestClient:
    client = TestClient(teams.app, raise_server_exceptions=False)
    response = _login(client, email)
    assert response.status_code == 200, response.text
    return client


def _user_row(teams: TwoTeams, user_id: str) -> tuple[str, str]:
    with closing(teams.open_conn()) as conn:
        row = conn.execute("select role, status from users where id = ?", (user_id,)).fetchone()
    assert row is not None
    return str(row["role"]), str(row["status"])


@pytest.fixture()
def teams(tmp_path: Path) -> Iterator[TwoTeams]:
    yield build_two_teams(tmp_path)


def test_lockout_blocks_only_new_logins_and_expires(teams: TwoTeams) -> None:
    carol = _signed_in(teams)

    for _ in range(5):
        assert _login(carol, password="wrong").status_code == 401

    blocked = _login(carol)
    unknown = _login(carol, email=UNKNOWN_EMAIL)
    assert blocked.status_code == unknown.status_code == 401
    assert without_request_id(blocked) == without_request_id(unknown)

    assert carol.get("/api/auth/me").status_code == 200, "lockout must not kill a live session"

    with closing(teams.open_conn()) as conn:
        conn.execute(
            "update users set lockout_until = '2000-01-01T00:00:00' where id = ?",
            (teams.member_b,),
        )
        conn.commit()

    fresh = TestClient(teams.app, raise_server_exceptions=False)
    assert _login(fresh).status_code == 200, "lockout must expire by clock, not by a write"
    assert fresh.get("/api/auth/me").status_code == 200


def test_disable_then_enable_does_not_revive_the_old_cookie(teams: TwoTeams) -> None:
    carol = _signed_in(teams)

    assert teams.client_b.post(f"/api/members/{teams.member_b}/disable").status_code == 204
    assert carol.get("/api/auth/me").status_code == 401

    disabled = _login(carol)
    unknown = _login(carol, email=UNKNOWN_EMAIL)
    assert disabled.status_code == unknown.status_code == 401
    assert without_request_id(disabled) == without_request_id(unknown)

    assert teams.client_b.post(f"/api/members/{teams.member_b}/enable").status_code == 204
    assert carol.get("/api/auth/me").status_code == 401, "enable must not revive old tickets"

    fresh = _signed_in(teams)
    assert fresh.get("/api/auth/me").status_code == 200


def test_admin_cannot_change_own_role_or_disable_self(teams: TwoTeams) -> None:
    before = _user_row(teams, teams.user_b)

    role = teams.client_b.patch(f"/api/members/{teams.user_b}", json={"role": "member"})
    disable = teams.client_b.post(f"/api/members/{teams.user_b}/disable")

    assert role.status_code == disable.status_code == 403
    assert _user_row(teams, teams.user_b) == before
    assert teams.client_b.get("/api/auth/me").status_code == 200


def test_member_cannot_manage_and_admin_cannot_cross_the_team_boundary(
    teams: TwoTeams,
) -> None:
    carol = _signed_in(teams)

    existing = carol.post(f"/api/members/{teams.user_b}/disable")
    missing = carol.post("/api/members/u-does-not-exist/disable")
    assert existing.status_code == missing.status_code == 403
    assert without_request_id(existing) == without_request_id(missing)

    foreign = teams.client_b.patch(f"/api/members/{teams.user_a}", json={"role": "member"})
    nobody = teams.client_b.patch("/api/members/u-does-not-exist", json={"role": "member"})
    assert foreign.status_code == nobody.status_code == 404
    assert without_request_id(foreign) == without_request_id(nobody)
    assert _user_row(teams, teams.user_a) == ("admin", "active")


def test_change_password_keeps_the_current_device_and_kills_the_other(
    teams: TwoTeams,
) -> None:
    current = _signed_in(teams)
    other = _signed_in(teams)

    changed = current.post(
        "/api/auth/password",
        json={"oldPassword": PASSWORD, "newPassword": NEW_PASSWORD},
    )

    assert changed.status_code == 204
    assert current.get("/api/auth/me").status_code == 200
    assert other.get("/api/auth/me").status_code == 401

    fresh = TestClient(teams.app, raise_server_exceptions=False)
    assert _login(fresh, password=PASSWORD).status_code == 401
    assert _login(fresh, password=NEW_PASSWORD).status_code == 200
