"""备份与恢复：MVP 第 4 句「能备份、能恢复」的全部实现。

**为什么是 `VACUUM INTO` 而不是 `Connection.backup()`**：VACUUM INTO 产出的是
重新整理过的一致紧凑副本（不是逐页拷贝），单文件、无 -wal 伴生，拷走即用；
且它是服务端在线操作——另一连接持有未提交写事务时照常执行，备份看到的是
已提交的那个快照。目标文件已存在时 SQLite 直接报错，恰好成了「不许静默覆盖
上一份备份」的库层保证。

**为什么备份里带上 `files/`**：`documents.stored_path` 指向的是上传原件，
库里只有元数据。只备库不备文件，恢复出来的是一套「目录说有、盘上没有」的
半套系统——比全丢更难排查。

**恢复为什么必须停服**：备份可以热做，恢复不行——`os.replace` 换掉库文件的
一瞬间，正在运行的进程还握着旧连接的页缓存，WAL 伴生文件也会被清掉。
这不是实现偷懒，是 SQLite 单文件形态的边界：文档与 CLI 提示都写明「先停服」。

密钥提示：库里的 `api_key` 是 Fernet 密文（S-V6），**换机器恢复必须带上同一个
`AWB_SECRET_KEY`**，否则供应商配置解不开——库本身完好，但等于丢了一半。
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from agentworkbench.db.engine import load_vec

BACKUP_DB_NAME = "agentworkbench.db"
FILES_DIR_NAME = "files"

# 恢复校验用的最小表集：一个随机 sqlite 文件（或别的系统的库）不该被当成
# 本产品的备份装进去。刻意取最稳的几张核心表而不是全部——校验集越接近完整
# schema，未来一次改名就让恢复变红，而校验的目的只是「这确实是本产品的库」。
_REQUIRED_TABLES = ("documents", "users")

# 复制库文件用的临时后缀：与目标同目录（保证 os.replace 同盘原子），
# 带明显标记，恢复中途失败时残骸一眼可辨、可安全删除。
_RESTORE_TMP_SUFFIX = ".restoring-tmp"


class BackupError(Exception):
    """备份/恢复的操作性失败。message 直接给人看，不进 HTTP（这两条路径没有 HTTP）。"""


def backup(data_dir: Path, dest_dir: Path) -> Path:
    """热备：库（VACUUM INTO 一致副本）+ 上传文件，产出一个自足的备份目录。

    `dest_dir` 必须不存在或为空——覆盖写一份已存在的备份，毁掉的是用户
    最后一次的好版本，这个操作没有任何「确认」兜底。
    返回备份库文件的路径。
    """
    src_db = data_dir / BACKUP_DB_NAME
    if not src_db.is_file():
        raise BackupError(f"源数据库不存在：{src_db}（尚未启动过服务？）")
    if dest_dir.exists() and any(dest_dir.iterdir()):
        raise BackupError(f"备份目录已存在且非空，拒绝覆盖：{dest_dir}")
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_db = dest_dir / BACKUP_DB_NAME
    conn = sqlite3.connect(str(src_db))
    try:
        # 连接不复用 engine.connect：备份只需要读，不需要建 schema；
        # 但 vec0 必须加载——VACUUM INTO 会在目标库里重建虚表，没有模块直接失败。
        load_vec(conn)
        conn.execute("VACUUM INTO ?", (str(dest_db),))
    finally:
        conn.close()

    src_files = data_dir / FILES_DIR_NAME
    if src_files.is_dir():
        shutil.copytree(src_files, dest_dir / FILES_DIR_NAME)
    return dest_db


def restore(backup_dir: Path, data_dir: Path) -> Path:
    """把一个备份目录装回 `data_dir`。**停服后执行**（模块 docstring 顶部的理由）。

    顺序刻意是「先验备份、再动目标」：校验不过时目标一字不动——
    恢复失败的正确形态是「什么都没发生」，不是「换上了一半」。
    库文件走 临时文件 → 再验 → `os.replace` 原子替换；同时清掉目标库的
    `-wal`/`-shm` 残留——旧库的 WAL 内容对不上新库，留着就是损坏。
    上传文件是合并拷贝（备份里有才覆盖），不是删了重来：多删的每一个文件
    都是不可逆的，而「备份之后删掉的文件恢复不回来」可以在文档里说清楚。
    返回装好的库文件路径。
    """
    backup_db = backup_dir / BACKUP_DB_NAME
    if not backup_db.is_file():
        raise BackupError(f"备份目录里没有数据库文件：{backup_db}")
    _validate(backup_db)

    data_dir.mkdir(parents=True, exist_ok=True)
    src_files = backup_dir / FILES_DIR_NAME
    if src_files.is_dir():
        shutil.copytree(src_files, data_dir / FILES_DIR_NAME, dirs_exist_ok=True)

    target_db = data_dir / BACKUP_DB_NAME
    tmp_db = data_dir / (BACKUP_DB_NAME + _RESTORE_TMP_SUFFIX)
    tmp_db.unlink(missing_ok=True)
    shutil.copyfile(backup_db, tmp_db)
    _validate(tmp_db)  # 拷贝完整性再验一次：源盘坏道拷出坏副本，校验的是落到目标盘的那份
    for side in ("-wal", "-shm"):
        Path(str(target_db) + side).unlink(missing_ok=True)
    tmp_db.replace(target_db)
    return target_db


def _validate(db_file: Path) -> None:
    """备份文件的两层校验：结构完整 + 确实是本产品的库。

    读方式打开（mode=ro）：校验动作自己不许有写的副作用。
    vec0 仍然要加载——quick_check 会扫到虚表。
    """
    if not db_file.is_file() or db_file.stat().st_size == 0:
        raise BackupError(f"备份文件缺失或为空：{db_file}")
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        load_vec(conn)
        result = conn.execute("pragma quick_check").fetchone()
        verdict = result[0] if result else "无结果"
        if verdict != "ok":
            raise BackupError(f"备份文件损坏（quick_check: {verdict}）：{db_file}")
        names = {
            row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")
        }
        missing = [table for table in _REQUIRED_TABLES if table not in names]
        if missing:
            raise BackupError(
                f"这不是 AgentWorkbench 的备份（缺表 {', '.join(missing)}）：{db_file}"
            )
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"备份文件不是可读的 SQLite 数据库：{db_file}（{exc}）") from exc
    finally:
        conn.close()
