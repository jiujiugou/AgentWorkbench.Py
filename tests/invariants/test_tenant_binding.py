"""存量归属绑定（`I-V5` 的最后一段）。

阶段 1 的每一行都落在 `tenant_id = "default"` 下，而真实团队是引导建出来的
`t-xxxx`。于是那批数据**对所有人 404**——按 `AUTH-06` 这是对的（宁可看不见，
不要所有人可见），但用户传过的文件不能真的丢。这一格补上那个一次性的搬迁动作。

改坏这些不变量，用户看到什么：

- 绑定写了一半就崩 → 一部分文档看得见、一部分看不见，而没有任何列能区分
  「没搬」和「搬了一半」；
- 目标团队不存在时照样「成功」 → 运维以为搬完了，实际那批行仍然对所有人 404，
  而命令的退出码是 0；
- 撞上 `unique(tenant_id, sha256)` 时只跳过冲突那几行 → 命令报成功，
  剩下几行还在旧租户里，没有任何东西会提醒他；
- 把 `messages` / `chunks` 也一起 UPDATE → 多改一处就多一处改漏的机会，
  而它们跟着父行走，本来不需要动。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import load_script

from agentworkbench.config import Settings
from agentworkbench.db import engine
from agentworkbench.db.rebind import DATA_TABLES, BindingConflict, bind
from agentworkbench.db.repositories import documents as repo

pytestmark = pytest.mark.invariant

LEGACY = "default"
TEAM = "t-0000000000000001"
NOW = datetime(2026, 11, 4, 9, 0, 0, tzinfo=UTC)
STAMP = NOW.strftime("%Y-%m-%dT%H:%M:%S")


# ── 装配 ──


@pytest.fixture(name="conn")
def _conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = engine.connect(tmp_path / "agentworkbench.db")
    engine.create_schema(connection)
    connection.execute(
        "insert into tenants(id, name, created_at) values (?, '团队甲', ?)", (TEAM, STAMP)
    )
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def _document(conn: sqlite3.Connection, doc_id: str, *, tenant: str, sha: str) -> None:
    conn.execute(
        "insert into documents(id, tenant_id, file_name, stored_path, sha256, size_bytes,"
        " status, page_count, created_at, updated_at)"
        " values (?, ?, ?, ?, ?, 1024, 'parsed', 1, ?, ?)",
        (doc_id, tenant, f"{doc_id}.pdf", f"store/{doc_id}.pdf", sha, STAMP, STAMP),
    )


def seed_legacy(conn: sqlite3.Connection) -> dict[str, int]:
    """在 `default` 租户下各造一行。返回 `{表: 行数}`，与 `DATA_TABLES` 对齐。"""
    _document(conn, "doc-legacy", tenant=LEGACY, sha="sha-legacy")
    conn.execute(
        "insert into conversations(id, user_id, tenant_id, title, created_at, updated_at)"
        " values ('c-legacy', 'local', ?, '旧会话', ?, ?)",
        (LEGACY, STAMP, STAMP),
    )
    conn.execute(
        "insert into agent_runs(id, user_id, tenant_id, question, status, started_at)"
        " values ('run-legacy', 'local', ?, '旧问题', 'succeeded', ?)",
        (LEGACY, STAMP),
    )
    conn.commit()
    return {table: 1 for table in DATA_TABLES}


def tenants_of(conn: sqlite3.Connection, table: str) -> list[str]:
    # S608 误报：table 只来自调用点写死的常量，不是外部输入
    sql = f"select tenant_id from {table}"  # noqa: S608
    return [str(row[0]) for row in conn.execute(sql).fetchall()]


# ── 预演与真跑 ──


def test_dry_run_counts_and_writes_nothing(conn: sqlite3.Connection) -> None:
    """默认预演。**一个字节都不写**——搬迁类命令里，「先看一眼」必须是最省事的动作。"""
    expected = seed_legacy(conn)

    report = bind(conn, source=LEGACY, target=TEAM)

    assert report.counts == expected
    assert report.applied is False
    for table in DATA_TABLES:
        assert tenants_of(conn, table) == [LEGACY], table


def test_apply_moves_the_rows_and_they_become_visible(conn: sqlite3.Connection) -> None:
    """绑过去之后，目标团队能按 id 取到那份文档——这才是「绑定成功」的定义。

    只断言 `tenant_id` 变了的版本会漏掉真正的问题：如果读取侧还有别的归属条件，
    行搬过去了但仍然取不到，而 `tenant_id` 看起来完全正确。
    """
    seed_legacy(conn)
    assert repo.get(conn, "doc-legacy", tenant_id=TEAM) is None

    report = bind(conn, source=LEGACY, target=TEAM, apply=True)

    assert report.applied is True
    assert repo.get(conn, "doc-legacy", tenant_id=TEAM) is not None
    for table in DATA_TABLES:
        assert tenants_of(conn, table) == [TEAM], table


def test_running_twice_is_harmless(conn: sqlite3.Connection) -> None:
    """第二次跑找到 0 行，不报错。

    重跑是这类命令的常态（第一次中断、第二次不确定有没有跑成）。
    第二次报错的版本会让人去查一个不存在的问题。
    """
    seed_legacy(conn)
    bind(conn, source=LEGACY, target=TEAM, apply=True)

    second = bind(conn, source=LEGACY, target=TEAM, apply=True)

    assert second.total == 0
    for table in DATA_TABLES:
        assert tenants_of(conn, table) == [TEAM], table


# ── 三种拒绝 ──


def test_an_unknown_target_is_refused(conn: sqlite3.Connection) -> None:
    """目标团队不存在 → 拒绝。

    放行的版本会「成功」地把行绑到一个不存在的团队上——那批行仍然对所有人 404，
    而退出码是 0。一个「看起来成功了、其实什么都没发生」的结果比失败更难查。
    """
    seed_legacy(conn)

    with pytest.raises(ValueError, match="不存在"):
        bind(conn, source=LEGACY, target="t-nope", apply=True)

    for table in DATA_TABLES:
        assert tenants_of(conn, table) == [LEGACY], table


def test_binding_to_itself_is_refused(conn: sqlite3.Connection) -> None:
    """源与目标相同 → 拒绝。它什么都不会改，却会让人以为做了点什么。"""
    seed_legacy(conn)

    with pytest.raises(ValueError, match="同一个团队"):
        bind(conn, source=LEGACY, target=LEGACY, apply=True)


def test_a_sha_collision_is_refused_before_anything_is_written(conn: sqlite3.Connection) -> None:
    """目标团队已有同一份文件 → 整批拒绝，且**三张表一行都没动**。

    直接 UPDATE 会撞 `unique(tenant_id, sha256)`，整批回滚，报错只有一句
    `UNIQUE constraint failed`——运维得自己猜是哪一份。所以冲突在写之前查，
    连文件名一起报。

    「只跳过冲突那几行」是更诱人的实现，也更糟：命令报成功，剩下几行还在旧租户里，
    没有任何东西会提醒他。断言里因此要看 `conversations` / `agent_runs` 也没动——
    它们本来没有冲突，但事务里它们排在 `documents` 之后。
    """
    seed_legacy(conn)
    _document(conn, "doc-already-here", tenant=TEAM, sha="sha-legacy")
    conn.commit()

    with pytest.raises(BindingConflict) as caught:
        bind(conn, source=LEGACY, target=TEAM, apply=True)

    assert [name for _, name in caught.value.conflicts] == ["doc-legacy.pdf"]
    # 文档表里本来就有两行（一行 legacy、一行目标团队的），所以这里不能数行数——
    # 断言的是「被绑的那一行还在原处」，以及另外两张表一行都没动。
    assert repo.get(conn, "doc-legacy", tenant_id=TEAM) is None
    assert repo.get(conn, "doc-legacy", tenant_id=LEGACY) is not None
    for table in ("conversations", "agent_runs"):
        assert tenants_of(conn, table) == [LEGACY], table


def test_a_collision_shows_up_in_the_dry_run_too(conn: sqlite3.Connection) -> None:
    """预演就把冲突列出来——那是加 `--apply` 之前唯一会看到的警告。"""
    seed_legacy(conn)
    _document(conn, "doc-already-here", tenant=TEAM, sha="sha-legacy")
    conn.commit()

    report = bind(conn, source=LEGACY, target=TEAM)

    assert [name for _, name in report.conflicts] == ["doc-legacy.pdf"]


# ── 命令行 ──


def _prepare_db(tmp_path: Path) -> None:
    connection = engine.connect(tmp_path / "agentworkbench.db")
    engine.create_schema(connection)
    connection.execute(
        "insert into tenants(id, name, created_at) values (?, '团队甲', ?)", (TEAM, STAMP)
    )
    seed_legacy(connection)
    connection.close()


def _rows(tmp_path: Path, table: str) -> list[str]:
    connection = engine.connect(tmp_path / "agentworkbench.db")
    try:
        return tenants_of(connection, table)
    finally:
        connection.close()


def test_cli_dry_run_exits_zero_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认预演，退出码 0，库里一行都没变。

    调的是 `main()` 的返回值而不是 `bind()`：判据里「退出码」是给人看的那个信号，
    而一个「逻辑对了但脚本参数接错」的实现，运维拿到的仍然是错误的结果。
    """
    module = load_script("bind_tenant")
    monkeypatch.setenv("AWB_DATA_DIR", str(tmp_path))
    _prepare_db(tmp_path)

    assert module.main(["--to", TEAM]) == 0

    for table in DATA_TABLES:
        assert _rows(tmp_path, table) == [LEGACY], table


def test_cli_apply_moves_the_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_script("bind_tenant")
    monkeypatch.setenv("AWB_DATA_DIR", str(tmp_path))
    _prepare_db(tmp_path)

    assert module.main(["--to", TEAM, "--apply"]) == 0

    for table in DATA_TABLES:
        assert _rows(tmp_path, table) == [TEAM], table


def test_cli_refuses_an_unknown_team_with_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """退出码必须非 0，且库不动——0 与 2 的区别就是「下次要不要再看一眼」。"""
    module = load_script("bind_tenant")
    monkeypatch.setenv("AWB_DATA_DIR", str(tmp_path))
    _prepare_db(tmp_path)

    assert module.main(["--to", "t-nope", "--apply"]) == 2

    for table in DATA_TABLES:
        assert _rows(tmp_path, table) == [LEGACY], table


def test_cli_defaults_the_source_to_the_legacy_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--from` 不给时用 `Settings.tenant_id`——那是阶段 1 唯一写过的那个值。

    `Settings.tenant_id` 在格 5 之后**只剩这一个消费者**：留着它是因为这条命令
    需要一个默认值，而「`default`」这个字面量分散在两个地方迟早会不一致。
    """
    module = load_script("bind_tenant")
    monkeypatch.setenv("AWB_DATA_DIR", str(tmp_path))
    _prepare_db(tmp_path)

    assert Settings().tenant_id == LEGACY
    assert module.main(["--to", TEAM, "--apply"]) == 0
    assert _rows(tmp_path, "documents") == [TEAM]
