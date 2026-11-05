"""不变量：迁移与建表函数产出**同一个 schema**。

被改坏时用户会看到：新装的环境能跑，升级的环境缺一列——报错是
`no such column: error_code`，发生在某个客户的库上，而不在 CI 里。

这条不变量之所以必须有：`create_schema()`（测试与空库起服务用）与 alembic
（升级用）是两条入口。两条入口各写一份 DDL 是最容易发生、也最晚被发现的漂移。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from agentworkbench.db import engine

ROOT = Path(__file__).resolve().parents[2]


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("select name from sqlite_master where type = 'table'").fetchall()
    # alembic 自己的版本号表不算业务 schema。
    return {str(row[0]) for row in rows if str(row[0]) != "alembic_version"}


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"pragma table_info({table})")]


def test_alembic_initial_matches_create_schema(tmp_path: Path) -> None:
    migrated_path = tmp_path / "migrated.db"
    direct_path = tmp_path / "direct.db"

    config = Config(str(ROOT / "alembic.ini"))
    url_engine = create_engine(f"sqlite:///{migrated_path.as_posix()}")
    with url_engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        connection.commit()
    url_engine.dispose()

    # 必须走 engine.connect：`vec_chunks_vec` 是 vec0 虚表，
    # 裸 sqlite3.connect 没加载扩展，连 pragma table_info 都会报 no such module。
    migrated = engine.connect(migrated_path)
    direct = engine.connect(direct_path)
    engine.create_schema(direct)

    assert _tables(migrated) == _tables(direct), "两条入口建出的表集合不一致"

    for table in sorted(_tables(direct)):
        assert _columns(migrated, table) == _columns(direct, table), f"{table} 的列不一致"

    migrated.close()
    direct.close()


def test_create_schema_adds_the_flag_to_an_unversioned_old_database(tmp_path: Path) -> None:
    """没有 `alembic_version` 的老库也要能补上首登标记。

    这条针对的是当前真实部署形态：库是 `create_schema` 建的，没有版本表，
    `alembic upgrade head` 会从 0001 重跑并撞上已存在的表。启动期只做
    **向后兼容的加列**，老账号默认 0；重复执行不能炸。
    """
    path = tmp_path / "legacy.db"
    conn = engine.connect(path)
    try:
        conn.executescript(
            "create table users ("
            " id text primary key, tenant_id text not null, email text not null,"
            " password_hash text not null, role text not null, status text not null,"
            " credential_epoch integer not null default 1, lockout_until text,"
            " failed_attempts integer not null default 0,"
            " is_platform_admin integer not null default 0,"
            " created_at text not null, updated_at text not null);"
            "insert into users (id, tenant_id, email, password_hash, role, status,"
            " credential_epoch, lockout_until, failed_attempts, is_platform_admin,"
            " created_at, updated_at)"
            " values ('u-old','t1','old@example.com','h','admin','active',1,null,0,1,'t','t');"
        )
        conn.commit()

        engine.create_schema(conn)
        engine.create_schema(conn)  # 幂等：第二次不能 duplicate column

        columns = [str(row[1]) for row in conn.execute("pragma table_info(users)")]
        assert columns.count("must_change_password") == 1
        row = conn.execute("select must_change_password from users where id = 'u-old'").fetchone()
        assert row is not None
        assert int(row[0]) == 0
    finally:
        conn.close()
