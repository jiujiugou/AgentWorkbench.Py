# ruff: noqa: S106 —— 本文件的密钥全是哨兵值。
"""D6 / P0-7：密钥不回传、不进日志；错误体稳定且脱敏。

判据是**三条通道一起干净**，任何一条漏了都不算通过：

1. **响应体**（尤其是**错误路径**）：明文、密文、字段名都不出现。
2. **日志**：一次真实失败必须能按客户端贴来的 `X-Request-Id` 翻到，而那条日志里
   不出现凭据——「带 request_id」和「按 request_id 查得到」是两件事。
3. **落盘**：库与备份文件里只有密文；备份被整目录拷走也读不出明文。

这一组是**对抗**而不是回归：每一条都从一个「攻击者/事故视角」出发——
我拿到了备份、我拿到了日志、我是那个把 apiKey 写成对象的客户端。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from harness import ALICE, PASSWORD, TwoTeams, build_two_teams

from agentworkbench.api.app import create_app
from agentworkbench.api.deps import build_deps
from agentworkbench.config import AuthSettings, Settings
from agentworkbench.db import backup as backup_mod
from agentworkbench.db import crypto

SENTINEL = "sk-D6-SENTINEL-4c1f"
"""凭据哨兵。它出现在任何响应体、日志行、库文件、备份文件里 = 判据红。"""

WRONG_TYPE_MODEL = "d6-sentinel-model"


@pytest.fixture
def teams(tmp_path: Path) -> Iterator[TwoTeams]:
    yield build_two_teams(tmp_path)


def _configure_provider(
    teams: TwoTeams,
    *,
    api_key: str = SENTINEL,
    base_url: str = "http://127.0.0.1:9",
    model: str = WRONG_TYPE_MODEL,
) -> int:
    """建一个供应商 + 一个默认模型 + 激活。走真实 HTTP，不绕过白名单 DTO。"""
    created = teams.client_a.post(
        "/api/settings/providers",
        json={"name": "d6", "baseUrl": base_url, "apiKey": api_key},
    )
    assert created.status_code == 201, created.text
    provider_id = int(created.json()["id"])
    added = teams.client_a.post(
        f"/api/settings/providers/{provider_id}/models",
        json={"model": model, "isDefault": True},
    )
    assert added.status_code == 201, added.text
    activated = teams.client_a.post(f"/api/settings/providers/{provider_id}/activate")
    assert activated.status_code == 200, activated.text
    return provider_id


ENVELOPE_KEYS = {"code", "http_status", "message", "retryable", "request_id"}


def test_validation_failure_uses_the_five_field_envelope(teams: TwoTeams) -> None:
    """校验失败也是**五字段信封**，不是框架默认的 `{"detail": [...]}`。

    为什么值得一条独立断言：前端 `parseError` 只认 `message`。默认体里没有
    `message` 时，用户看到的是整段 JSON 原文——而那原文里包含他自己提交的凭据
    （见下一条）。同一个缺陷同时是「体验问题」和「泄露问题」。
    """
    response = teams.client_a.post(
        "/api/settings/providers",
        json={"name": "d6", "baseUrl": "u", "apiKey": 12345},
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert set(body) == ENVELOPE_KEYS, f"信封字段不对：{sorted(body)}"
    assert body["code"] == "validation_failed"
    assert body["http_status"] == 400 == response.status_code, "body 与状态行不一致"
    assert body["retryable"] is False
    assert body["message"], "没有 message，前端只能把原始 JSON 显示给用户"
    assert response.headers["x-request-id"] == body["request_id"]


def test_rejected_credential_is_not_echoed_back(teams: TwoTeams) -> None:
    """把 apiKey 写成对象（类型错、值对）→ 响应体里不许出现那个值。

    FastAPI 默认 422 的每一条错误都带 `input`，即客户端提交的原值。这是 D6 实测
    抓到的真实泄露：浏览器 devtools、反代访问日志、前端错误上报都会留一份，
    「密钥不回传」按字面就破了。
    """
    response = teams.client_a.post(
        "/api/settings/providers",
        json={"name": "d6", "baseUrl": "u", "apiKey": {"nested": SENTINEL}},
    )
    assert response.status_code == 400, response.text
    assert SENTINEL not in response.text, "错误体回显了提交的凭据"
    # 只报「哪个字段、什么毛病」，不报值
    assert "apiKey" in response.json()["message"]


def test_malformed_json_is_not_echoed_back(teams: TwoTeams) -> None:
    """畸形 JSON 的原始字节不进响应体。"""
    raw = b'{"name":"d6","baseUrl":"u","apiKey":"' + SENTINEL.encode() + b'",}'
    response = teams.client_a.post(
        "/api/settings/providers",
        content=raw,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400, response.text
    assert SENTINEL not in response.text


def test_a_real_failure_is_findable_by_request_id_in_the_logs(
    teams: TwoTeams, caplog: pytest.LogCaptureFixture
) -> None:
    """跑一次真实失败（端点不可达），按 request_id 能查到，且日志里没有凭据。

    这条补的是 S-H2 那半句「日志里有」：此前 503 路径**一条日志都不写**，
    客户端贴来 request_id 时运维在日志里什么也搜不到——响应里带一个查不到的
    编号，等于没有这个能力。
    """
    _configure_provider(teams)
    caplog.set_level(logging.DEBUG)

    response = teams.client_a.post("/api/knowledge/ask", json={"question": "液压站报警怎么处理？"})

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "model_unavailable"
    request_id = response.headers["x-request-id"]

    ours = [record for record in caplog.records if record.name.startswith("agentworkbench")]
    matching = [record for record in ours if request_id in record.getMessage()]
    assert matching, f"按 request_id={request_id} 在日志里查不到这次请求"
    joined = "\n".join(record.getMessage() for record in matching)
    assert "model_unavailable" in joined, "日志里没有错误码，运维只能看到「失败了」"
    assert "/api/knowledge/ask" in joined, "日志里没有路径，定位不到入口"

    app_logs = "\n".join(record.getMessage() for record in ours)
    assert SENTINEL not in app_logs, "应用日志里出现了凭据明文"
    assert SENTINEL not in response.text


def test_credential_lives_only_as_ciphertext_on_disk_and_in_the_backup(
    teams: TwoTeams, tmp_path: Path
) -> None:
    """库文件与备份文件里都只有密文：拿到文件也读不出 key。"""
    _configure_provider(teams)

    with closing(teams.open_conn()) as conn:
        row = conn.execute("select id, api_key_encrypted from providers").fetchone()
    assert row is not None
    ciphertext = str(row["api_key_encrypted"])
    assert ciphertext and SENTINEL not in ciphertext, "库里存的是明文"
    assert crypto.decrypt("acceptance-test-secret", ciphertext) == SENTINEL, "密文解不回来"

    data_dir = teams.settings.data_dir
    for path in sorted(data_dir.glob("agentworkbench.db*")):
        assert SENTINEL.encode() not in path.read_bytes(), f"{path.name} 里有明文凭据"

    backup_dir = backup_mod.backup(data_dir, tmp_path / "backup")
    for path in sorted(Path(backup_dir).rglob("*")):
        if path.is_file():
            assert SENTINEL.encode() not in path.read_bytes(), f"备份 {path.name} 里有明文凭据"


def test_rotated_secret_is_a_configuration_error_not_an_internal_error(
    teams: TwoTeams,
) -> None:
    """换过 `AWB_SECRET_KEY` → 503 配置失效（不可重试），不是 500「稍后重试」。

    改坏前的行为：`crypto.decrypt` 的 ValueError 一路冒泡到兜底 handler，
    用户看到「服务器内部错误」且 `retryable=True`——他重试一万次也不会好，
    而真正该做的那件事（重新保存一次凭据）只留在日志里。
    """
    _configure_provider(teams)

    rotated = Settings(
        data_dir=teams.settings.data_dir,
        secret_key="a-different-secret-after-rotation",
        auth=AuthSettings(session_invalidate_seconds=0),
    )
    deps = build_deps(rotated)
    app = create_app(settings=rotated, deps=deps)

    with TestClient(app, raise_server_exceptions=False) as rotated_client:
        login = rotated_client.post("/api/auth/login", json={"email": ALICE, "password": PASSWORD})
        assert login.status_code == 200, login.text
        response = rotated_client.post(
            "/api/knowledge/ask", json={"question": "液压站报警怎么处理？"}
        )

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "model_not_configured"
    assert body["retryable"] is False, "配置失效被标成可重试 → 前端会把一次故障变成一串"
    assert "重新保存" in body["message"], "文案没说用户该做什么"
    assert SENTINEL not in response.text
    # 密文本身也不许出去
    with closing(teams.open_conn()) as conn:
        stored = conn.execute("select api_key_encrypted from providers").fetchone()
    assert stored is not None
    assert str(stored["api_key_encrypted"]) not in response.text


def test_direct_sql_read_cannot_recover_the_key_without_the_secret(
    teams: TwoTeams, tmp_path: Path
) -> None:
    """绕过应用直接开库，仍拿不到明文（密文与密钥分离的落点）。

    这条与上一条的区别：上一条验「应用层怎么翻译失败」，这条验「攻击者拿到了
    库文件之后能做什么」。备份被拷走是同一个场景。
    """
    _configure_provider(teams)
    copied = tmp_path / "stolen.db"
    with closing(teams.open_conn()) as conn:
        conn.execute("pragma wal_checkpoint(full)")
    source = teams.settings.db_path
    copied.write_bytes(Path(source).read_bytes())

    conn = sqlite3.connect(copied)
    try:
        row = conn.execute("select api_key_encrypted from providers").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert SENTINEL not in str(row[0])
    with pytest.raises(ValueError):
        crypto.decrypt("guessed-secret", str(row[0]))
