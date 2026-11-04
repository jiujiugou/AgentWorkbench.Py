"""备份与恢复的不变量（F6 §1.12，NFR-08）。

MVP 第 4 句的原话是「能被别人装起来、备份、恢复（不是只在开发机上跑）」。
这一组测试回答的是「恢复出来的到底是不是一套系统」：
行数对齐只是下限，**恢复后的库要能登录（users）、能检索（vec KNN）**——
vec0 虚表在备份/恢复两段各有一次「模块没加载就全灭」的机会，
所以 KNN 查询在两条路径上都要真的跑一次，count(*) 对不上是抓不住它的。

热备的断言有两个方向：未提交的事务**不挡**备份（WAL 读快照），
也**不进**备份——备份把没提交的行带进去，等于恢复出一条用户没确认过的数据。

改坏后会怎样（每条的失效形态都以用户视角写在了各测试的断言消息里）：
备份只备库不备 files/ → 用户恢复出「目录说有、盘上没有」的半套系统；
备份允许覆盖 → 用户上一份好备份被静默毁掉；恢复不先验备份 → 用户的好数据
被一份损坏备份换掉；恢复不清 -wal/-shm → 用户打开恢复后的库直接损坏报错；
恢复把未提交事务带进去 → 用户看到一条自己从没确认过的数据。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import sqlite_vec  # type: ignore[import-untyped]

from agentworkbench.cli import main as cli_main
from agentworkbench.core.clock import utc_now
from agentworkbench.core.vectors import EMBEDDING_DIM
from agentworkbench.db import engine
from agentworkbench.db.backup import BACKUP_DB_NAME, FILES_DIR_NAME, BackupError, backup, restore

_NOW = utc_now()
# 单位向量随便取一个方向即可；KNN 断言的是「能查、命中对的行」，不是距离值
_VECTOR = [0.1] * EMBEDDING_DIM


# ── 造数据 ──────────────────────────────────────────────────────────────


def _make_data_dir(tmp_path: Path, *, name: str = "data") -> Path:
    data_dir = tmp_path / name
    data_dir.mkdir()
    conn = engine.connect(data_dir / BACKUP_DB_NAME)
    try:
        engine.create_schema(conn)
        conn.executemany(
            "insert into tenants (id, name, created_at) values (?, ?, ?)",
            [("t1", "甲团队", _NOW), ("t2", "乙团队", _NOW)],
        )
        conn.executemany(
            "insert into users (id, tenant_id, email, password_hash, role, status,"
            " credential_epoch, failed_attempts, created_at, updated_at)"
            " values (?, 't1', ?, 'hash', 'admin', 'active', 1, 0, ?, ?)",
            [(f"u{i}", f"u{i}@x.com", _NOW, _NOW) for i in (1, 2)],
        )
        conn.execute(
            "insert into documents (id, tenant_id, file_name, stored_path, sha256,"
            " size_bytes, status, page_count, created_at, updated_at)"
            " values ('d1', 't1', '手册.pdf', 'files/d1.pdf', 'sha-d1', 10, 'parsed', 1, ?, ?)",
            (_NOW, _NOW),
        )
        conn.execute(
            "insert into chunks"
            " (document_id, seq, page_from, page_to, content, char_count, created_at)"
            " values ('d1', 0, 1, 1, '退换货政策：七天无理由。', 14, ?)",
            (_NOW,),
        )
        chunk_id = conn.execute("select id from chunks").fetchone()[0]
        conn.execute(
            "insert into chunks_fts (content, document_id, chunk_id) values (?, 'd1', ?)",
            ("退换货政策：七天无理由。", chunk_id),
        )
        conn.execute(
            "insert into chunks_vec (ChunkId, DocumentId, Seq, PageFrom, PageTo)"
            " values (?, 'd1', 0, 1, 1)",
            (chunk_id,),
        )
        conn.execute(
            "insert into vec_chunks_vec (ChunkId, Embedding) values (?, ?)",
            (chunk_id, json.dumps(_VECTOR)),
        )
        files_dir = data_dir / FILES_DIR_NAME
        files_dir.mkdir()
        (files_dir / "d1.pdf").write_bytes(b"%PDF-1.4 original upload")
        conn.commit()
    finally:
        conn.close()
    return data_dir


def _open(path: Path) -> sqlite3.Connection:
    conn = engine.connect(path)
    engine.load_vec(conn)
    return conn


def _knn_first(conn: sqlite3.Connection) -> int:
    (row,) = conn.execute(
        "select ChunkId from vec_chunks_vec where Embedding match ? and k = 1",
        (sqlite_vec.serialize_float32(_VECTOR),),
    ).fetchall()
    return int(row[0])


# ── 备份 ────────────────────────────────────────────────────────────────


def test_backup_carries_every_table_and_the_vector_index(tmp_path: Path) -> None:
    data_dir = _make_data_dir(tmp_path)
    dest = backup(data_dir, tmp_path / "backup")

    # 逐表写死字面量 SQL，不做字符串拼接：表名来自本测试自己的清单，拼 f-string 是自找 S608
    counts = (
        ("select count(*) from tenants", 2),
        ("select count(*) from users", 2),
        ("select count(*) from documents", 1),
        ("select count(*) from chunks", 1),
        ("select count(*) from chunks_fts", 1),
        ("select count(*) from chunks_vec", 1),
        ("select count(*) from vec_chunks_vec", 1),
    )
    conn = _open(dest.parent / BACKUP_DB_NAME)
    try:
        for query, expected in counts:
            got = conn.execute(query).fetchone()[0]
            assert got == expected, f"{query}：备份 {got} 行，源 {expected} 行"
        assert conn.execute("pragma quick_check").fetchone()[0] == "ok"
        assert _knn_first(conn) == 1, "备份后的向量索引查不动，恢复出来就是半套系统"
    finally:
        conn.close()
    assert (dest.parent / FILES_DIR_NAME / "d1.pdf").read_bytes().startswith(b"%PDF")


def test_backup_is_hot_uncommitted_writes_neither_block_nor_leak(tmp_path: Path) -> None:
    data_dir = _make_data_dir(tmp_path)
    writer = _open(data_dir / BACKUP_DB_NAME)
    try:
        writer.execute("begin immediate")
        writer.execute(
            "insert into documents (id, tenant_id, file_name, stored_path, sha256,"
            " size_bytes, status, page_count, created_at, updated_at)"
            " values ('d2', 't1', '未提交.pdf', 'files/d2.pdf', 'sha-d2', 1, 'pending', 0, ?, ?)",
            (_NOW, _NOW),
        )
        dest = backup(data_dir, tmp_path / "backup")
        conn = _open(dest.parent / BACKUP_DB_NAME)
        try:
            assert conn.execute("select count(*) from documents").fetchone()[0] == 1, (
                "未提交的行进了备份：恢复会多出一条用户没确认过的数据"
            )
        finally:
            conn.close()
        writer.rollback()
        assert writer.execute("select count(*) from documents").fetchone()[0] == 1
    finally:
        writer.close()


def test_backup_refuses_to_overwrite_an_existing_backup(tmp_path: Path) -> None:
    data_dir = _make_data_dir(tmp_path)
    dest = tmp_path / "backup"
    backup(data_dir, dest)
    (dest / "keep.txt").write_text("上一份备份的标记")
    with pytest.raises(BackupError, match="拒绝覆盖"):
        backup(data_dir, dest)
    assert (dest / "keep.txt").exists(), "失败路径不许碰已有目录"


def test_backup_refuses_when_the_database_does_not_exist(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BackupError, match="源数据库不存在"):
        backup(empty, tmp_path / "backup")


# ── 恢复 ────────────────────────────────────────────────────────────────


def test_restore_produces_a_working_system(tmp_path: Path) -> None:
    data_dir = _make_data_dir(tmp_path)
    dest = backup(data_dir, tmp_path / "backup")

    target = tmp_path / "restored"
    restored_db = restore(dest.parent, target)
    conn = _open(restored_db)
    try:
        assert conn.execute("select count(*) from users").fetchone()[0] == 2, "登录侧的表没回来"
        assert conn.execute("pragma quick_check").fetchone()[0] == "ok"
        assert _knn_first(conn) == 1, "恢复后的向量索引查不动——这就是「行数都在、检索全灭」"
    finally:
        conn.close()
    assert (target / FILES_DIR_NAME / "d1.pdf").read_bytes().startswith(b"%PDF")


def test_restore_refuses_a_corrupt_backup_and_leaves_the_target_untouched(
    tmp_path: Path,
) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / BACKUP_DB_NAME).write_bytes(b"definitely not a sqlite database")
    # 带上 files/：入口校验若被跳过，流程会先把这个目录拷进目标再失败——
    # 「先验后动」的顺序由此才可判；只有库文件损坏、没有 files 的备份抓不住顺序
    (broken / FILES_DIR_NAME).mkdir()
    (broken / FILES_DIR_NAME / "stale.pdf").write_bytes(b"%PDF stale")

    target = tmp_path / "target"
    with pytest.raises(BackupError, match=r"损坏|可读"):
        restore(broken, target)
    # 恢复失败的正确形态是「什么都没发生」：目标目录连 files 都不该被建出来
    assert not (target / FILES_DIR_NAME).exists()
    assert not (target / BACKUP_DB_NAME).exists()


def test_restore_refuses_a_foreign_sqlite_file(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    conn = sqlite3.connect(str(foreign / BACKUP_DB_NAME))
    conn.execute("create table foo (id integer)")
    conn.commit()
    conn.close()
    with pytest.raises(BackupError, match="缺表"):
        restore(foreign, tmp_path / "target")


def test_restore_refuses_a_backup_directory_without_a_database(tmp_path: Path) -> None:
    hollow = tmp_path / "hollow"
    hollow.mkdir()
    (hollow / FILES_DIR_NAME).mkdir()
    with pytest.raises(BackupError, match="没有数据库文件"):
        restore(hollow, tmp_path / "target")


def test_restore_clears_stale_wal_side_files(tmp_path: Path) -> None:
    """旧库的 -wal/-shm 对不上新库：不清掉就是「换了个头、身体还是旧的」。"""
    data_dir = _make_data_dir(tmp_path)
    dest = backup(data_dir, tmp_path / "backup")

    target = tmp_path / "target"
    target.mkdir()
    old_db = target / BACKUP_DB_NAME
    old_db.write_bytes(b"old broken database")
    Path(str(old_db) + "-wal").write_bytes(b"stale wal")
    Path(str(old_db) + "-shm").write_bytes(b"stale shm")

    restored_db = restore(dest.parent, target)
    assert restored_db.read_bytes().startswith(b"SQLite format 3")
    assert not Path(str(old_db) + "-wal").exists()
    assert not Path(str(old_db) + "-shm").exists()
    conn = _open(restored_db)
    try:
        assert conn.execute("select count(*) from documents").fetchone()[0] == 1
    finally:
        conn.close()


# ── CLI ─────────────────────────────────────────────────────────────────


def test_cli_backup_and_restore_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = _make_data_dir(tmp_path)
    monkeypatch.setenv("AWB_DATA_DIR", str(data_dir))

    assert cli_main(["backup", "--to", str(tmp_path / "backup")]) == 0

    # 恢复走 --data-dir 旗标（而不是环境变量）：两条传参路径各验一条
    target = tmp_path / "restored"
    assert cli_main(["restore", "--from", str(tmp_path / "backup"), "--data-dir", str(target)]) == 0
    conn = _open(target / BACKUP_DB_NAME)
    try:
        assert conn.execute("select count(*) from users").fetchone()[0] == 2
    finally:
        conn.close()


def test_cli_reports_failure_with_a_nonzero_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AWB_DATA_DIR", str(tmp_path / "no-such-data"))
    assert cli_main(["backup", "--to", str(tmp_path / "backup")]) == 1
    assert "备份失败" in capsys.readouterr().err
    # 错误信息走 stderr，成功路径的 stdout 不能混进失败信息——排障时看的就是流的分离
