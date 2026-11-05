"""把 C# 版 `AgentWorkbench` 的数据搬进这套 Python 实现。

用法：

    AWB_SECRET_KEY=... python scripts/migrate_from_csharp.py --dry-run
    AWB_SECRET_KEY=... python scripts/migrate_from_csharp.py --password '你的新密码'

搬什么、不搬什么，逐条说清（**不搬的也是结论**）：

- `tenants`：原样，id 保持——团队是其它一切的归属轴。
- `users`：换哈希、补 email、role 映射。C# 用 PBKDF2，本实现用 Argon2，
  **哈希不可移植**；且源表只有 `user_name`，没有 email（登录用它）。
- `providers` / `provider_models`：密钥重新加密。源列 `api_key` 是**明文**，
  目标列 `api_key_encrypted` 是 Fernet 密文（S-V6）。
- `documents` / `document_pages` / `chunks`：原样搬，补 `tenant_id` 与相对路径。
  源没有 `tenant_id`（单工作区形态），目标必须有；源存绝对路径，目标存相对路径。
- `chunks_vec` / `vec_chunks_vec` / `chunks_fts`：三处一起搬。只搬一处 = 检索
  各走各的路径，其中两条静默返回空。
- `conversations` / `messages`：**不搬**。目标 `messages` 有硬约束「assistant 行
  必须有 run_id 且指向真实 `agent_runs`」，源里没有 run 记录，搬家就得**伪造 run**
  ——那等于往审计链里灌假数据。
- `workflow_*` / `memory_items` / `prompt_versions`：不搬，目标实现没有对应表。

**向量维度是硬门槛**：源 `vec_chunks_vec` 是 `FLOAT[1024]`（`BAAI/bge-m3`），
本实现的 `EMBEDDING_DIM` 也是 1024。两者不等时本脚本直接退出——
混着写会让每次插入报错，而不是"稍微不准"。

**换模型必须重算向量**：搬过来的向量是 bge-m3 算的，查询侧就必须是 bge-m3。
配置里 `AWB_EMBEDDING__MODEL` 写别的模型时，检索相似度是噪声，
所以脚本把 `embedding.model` 与 `embedding.synced` 都写成源向量实际用的模型。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from agentworkbench.core.clock import utc_now
from agentworkbench.core.tokenizer import to_index_text, tokenize
from agentworkbench.core.vectors import EMBEDDING_DIM
from agentworkbench.db import crypto, engine
from agentworkbench.db.repositories import system_state
from agentworkbench.identity.credentials import hash_password
from agentworkbench.ingest.storage import LocalStorage

DEFAULT_SOURCE = Path(r"D:\Code\AgentWorkbench\src\AgentWorkbench.Webapi\data\agentworkbench.db")
"""C# 版 `Webapi` 的数据目录。仓库根的 `data/` 是空库（192KB、零行），不是这个。"""

EMBEDDING_MODEL = "BAAI/bge-m3"
"""源向量实际用的模型（`appsettings.json` 的 `Knowledge.Embedding.Model`）。"""

ROLE_MAP = {"owner": "admin", "admin": "admin", "member": "member"}


@dataclass
class Report:
    """迁移结果。干跑时只填 `plan`，不碰目标库。"""

    counts: dict[str, int] = field(default_factory=dict)
    users: list[tuple[str, str, str]] = field(default_factory=list)
    providers: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        self.checks.append((label, ok, detail))


def _open_source(path: Path) -> sqlite3.Connection:
    """把源库复制到临时目录再读。

    **不直接开源库**：它可能被 C# 程序按 WAL 持有，直接读写会看到未合并的
    `-wal`，也可能因为我们的一次误写改变源数据。复制一份 `-wal` 一起读，
    是一次一致快照。
    """
    if not path.exists():
        raise SystemExit(f"源库不存在：{path}")
    scratch = Path(tempfile.mkdtemp(prefix="awb-migrate-")) / "source.db"
    shutil.copy2(path, scratch)
    wal = Path(str(path) + "-wal")
    if wal.exists():
        shutil.copy2(wal, str(scratch) + "-wal")
    conn = sqlite3.connect(scratch)
    conn.row_factory = sqlite3.Row
    engine.load_vec(conn)
    return conn


def _prepare_target(db_path: Path, *, force: bool) -> None:
    """建目标库。已存在且非空时**必须显式 --force**，并留一份 `.bak`。

    自动覆盖是这世界上最贵的便利：目标库里可能有已经建好的真实数据，
    而"重新跑一次脚本"是最常见的动作。
    """
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        rows = 0
        for table in ("documents", "chunks", "users", "providers"):
            try:
                rows += int(conn.execute(f"select count(*) from {table}").fetchone()[0])  # noqa: S608
            except sqlite3.OperationalError:
                continue
        conn.close()
        if rows and not force:
            raise SystemExit(
                f"目标库已有数据（{rows} 行）：{db_path}\n"
                "确认要覆盖请加 --force，原库会改名成 .bak 保留。"
            )
        if rows:
            backup = db_path.with_suffix(db_path.suffix + ".bak")
            shutil.copy2(db_path, backup)
            print(f"  已备份原库 → {backup.name}")
        # 空库也要删：它可能是**旧版本 schema** 建的（`create_schema` 只 IF NOT EXISTS，
        # 不会补列），留着会让第一条 insert 报 "table users has no column named ..."，
        # 而那句话看起来像"脚本写错了"，实际是"目标文件太老"。
        for suffix in ("", "-wal", "-shm"):
            leftover = Path(str(db_path) + suffix)
            if leftover.exists():
                leftover.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)


def _suffix_of(file_name: str) -> str:
    return Path(file_name).suffix.lower() or ".bin"


def migrate(
    source: sqlite3.Connection,
    *,
    db_path: Path,
    storage_root: Path,
    secret: str,
    password: str,
    tenant_id: str | None,
    dry_run: bool,
) -> Report:
    report = Report()
    now = utc_now()

    # ── 维度门槛先过：不等就别搬 ──
    # 源 `vec_chunks_vec` 是按它自己的维度建的。搬进维度不同的库，第一条 insert
    # 就报错；而"搬到一半失败"比"一开始就拒绝"难收拾得多（目标库已成半成品）。
    sample = source.execute(
        "select vec_to_json(Embedding) as v from vec_chunks_vec limit 1"
    ).fetchone()
    if sample is not None:
        source_dim = len(json.loads(str(sample["v"])))
        if source_dim != EMBEDDING_DIM:
            raise SystemExit(
                f"源向量是 {source_dim} 维，本实现的索引是 {EMBEDDING_DIM} 维。\n"
                "要么改 core/vectors.py::EMBEDDING_DIM 并重建索引，"
                "要么先用源模型把向量重算一遍——不能混着写。"
            )
        report.check("向量维度一致", True, f"{source_dim} 维")
    tenants = source.execute("select id, name, created_at from tenants order by id").fetchall()
    users = source.execute(
        "select id, tenant_id, user_name, display_name, role, is_active, created_at"
        " from users order by id"
    ).fetchall()
    providers = source.execute(
        "select id, name, base_url, api_key, is_active, created_at, updated_at"
        " from providers order by id"
    ).fetchall()
    models = source.execute(
        "select provider_id, model, is_default, created_at from provider_models"
        " order by provider_id, id"
    ).fetchall()
    documents = source.execute(
        "select id, file_name, stored_path, sha256, size_bytes, status, page_count,"
        " error, created_at, parsed_at from documents order by created_at, id"
    ).fetchall()
    pages = source.execute(
        "select document_id, page_no, raw_text from document_pages order by document_id, page_no"
    ).fetchall()
    chunks = source.execute(
        "select id, document_id, seq, page_from, page_to, heading, content, char_count,"
        " created_at from chunks order by id"
    ).fetchall()
    chunk_meta = source.execute(
        "select ChunkId, DocumentId, Seq, Heading, PageFrom, PageTo from chunks_vec"
    ).fetchall()
    vectors = source.execute(
        "select ChunkId, vec_to_json(Embedding) from vec_chunks_vec"
    ).fetchall()

    report.counts = {
        "tenants": len(tenants),
        "users": len(users),
        "providers": len(providers),
        "models": len(models),
        "documents": len(documents),
        "document_pages": len(pages),
        "chunks": len(chunks),
        "vectors": len(vectors),
    }
    if not chunks:
        raise SystemExit("源库没有任何片段：这份库是空的（仓库根的 data/ 就是这种），确认路径。")

    # 文档归属：源没有 tenant_id，落到指定团队（默认"文档属于哪个团队"这件事在源里不存在）
    if tenant_id is None:
        # 源没有文档与团队的关联，只能猜"主工作区"。用**人数最多的团队**当默认，
        # 平手时按创建时间取早的：人数比 id 排序更接近"哪边才是主要在用"，
        # 而它仍然是猜——所以下面必须落一条 warning，并且 --tenant 能直接覆盖。
        owner_rows = [row for row in users if ROLE_MAP.get(str(row["role"])) == "admin"]
        if not owner_rows:
            raise SystemExit("源库没有团队所有者，无法确定文档归属")
        counts: dict[str, int] = {}
        for row in users:
            counts[str(row["tenant_id"])] = counts.get(str(row["tenant_id"]), 0) + 1
        tenant_id = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
        report.warn(
            f"源库的文档没有团队归属（单工作区形态），全部归到「{tenant_id}」。"
            "多团队的真实隔离要在界面上重传，或另写一份按团队拆分的导入。"
        )

    if dry_run:
        report.users = [
            (str(row["user_name"]), str(row["user_name"]) + "@migrated.local", "dry-run")
            for row in users
        ]
        report.providers = [(str(row["name"]), str(row["base_url"])) for row in providers]
        return report

    storage = LocalStorage(storage_root)
    storage_root.mkdir(parents=True, exist_ok=True)

    target = engine.connect(db_path)
    engine.create_schema(target)
    target.execute("pragma foreign_keys = off")  # 顺序由本脚本保证，先关再开，最后逐个验

    # ── 团队与用户 ──
    for row in tenants:
        target.execute(
            "insert into tenants(id, name, created_at) values (?,?,?)",
            (row["id"], row["name"], row["created_at"]),
        )
    first_admin = True
    for row in users:
        email = f"{row['user_name']}@migrated.local"
        role = ROLE_MAP.get(str(row["role"]), "member")
        is_admin = first_admin and role == "admin"
        first_admin = first_admin and not is_admin
        target.execute(
            "insert into users(id, tenant_id, email, password_hash, role, status,"
            " credential_epoch, lockout_until, failed_attempts, is_platform_admin,"
            " created_at, updated_at)"
            " values (?,?,?,?,?,?,1,null,0,?,?,?)",
            (
                row["id"],
                row["tenant_id"],
                email,
                hash_password(password),
                role,
                "active" if row["is_active"] else "disabled",
                1 if is_admin else 0,
                row["created_at"],
                now,
            ),
        )
        report.users.append((str(row["user_name"]), email, "admin" if is_admin else role))

    # ── 供应商与模型（明文 → Fernet）──
    provider_ids: dict[int, int] = {}
    for row in providers:
        cursor = target.execute(
            "insert into providers(name, base_url, api_key_encrypted, is_active,"
            " created_at, updated_at) values (?,?,?,?,?,?)",
            (
                row["name"],
                row["base_url"],
                crypto.encrypt(secret, str(row["api_key"] or "")),
                int(row["is_active"]),
                row["created_at"],
                row["updated_at"],
            ),
        )
        provider_ids[int(row["id"])] = int(cursor.lastrowid or 0)
        report.providers.append((str(row["name"]), str(row["base_url"])))
    if any(int(row["is_active"]) for row in providers):
        report.check(
            "激活供应商唯一",
            target.execute("select count(*) from providers where is_active = 1").fetchone()[0] == 1,
        )
    for row in models:
        target.execute(
            "insert into provider_models(provider_id, model, is_default, created_at)"
            " values (?,?,?,?)",
            (
                provider_ids[int(row["provider_id"])],
                row["model"],
                int(row["is_default"]),
                row["created_at"],
            ),
        )
    if models and not any(int(row["is_default"]) for row in models):
        report.warn("源里没有任何默认模型：聊天会报「未配置模型」，需要在设置页选一个。")

    # ── 文档与文件 ──
    missing_files: list[str] = []
    digest_mismatch: list[str] = []
    for row in documents:
        source_file = Path(str(row["stored_path"]))
        if not source_file.exists():
            missing_files.append(str(row["file_name"]))
            continue
        data = source_file.read_bytes()
        digest = storage.digest_of(data)
        if digest != str(row["sha256"]):
            # 内容与记录不符：片段是按旧内容切的，搬过去等于"正文与文件不是一份"
            digest_mismatch.append(str(row["file_name"]))
            continue
        relative = storage.put(
            tenant_id=str(tenant_id),
            digest=digest,
            suffix=_suffix_of(str(row["file_name"])),
            data=data,
        )
        target.execute(
            "insert into documents(id, tenant_id, file_name, stored_path, sha256, size_bytes,"
            " status, page_count, error_code, error, created_at, updated_at, parsed_at)"
            " values (?,?,?,?,?,?,?,?,null,?,?,?,?)",
            (
                row["id"],
                tenant_id,
                row["file_name"],
                relative,
                row["sha256"],
                row["size_bytes"],
                row["status"],
                row["page_count"],
                row["error"],
                row["created_at"],
                row["parsed_at"] or row["created_at"],
                row["parsed_at"],
            ),
        )
    if missing_files:
        report.warn(f"{len(missing_files)} 份文件的实体已不在源盘上，跳过：{missing_files[:3]}")
    if digest_mismatch:
        report.warn(
            f"{len(digest_mismatch)} 份文件的字节与记录的 sha256 不符，跳过：{digest_mismatch[:3]}"
        )

    kept_docs = {str(row[0]) for row in target.execute("select id from documents")}
    for row in pages:
        if str(row["document_id"]) in kept_docs:
            target.execute(
                "insert into document_pages(document_id, page_no, raw_text) values (?,?,?)",
                (row["document_id"], row["page_no"], row["raw_text"]),
            )

    # ── 片段与三处索引 ──
    for row in chunks:
        if str(row["document_id"]) not in kept_docs:
            continue
        target.execute(
            "insert into chunks(id, document_id, seq, page_from, page_to, heading, content,"
            " char_count, created_at) values (?,?,?,?,?,?,?,?,?)",
            (
                row["id"],
                row["document_id"],
                row["seq"],
                row["page_from"],
                row["page_to"],
                row["heading"],
                row["content"],
                row["char_count"],
                row["created_at"],
            ),
        )
        target.execute(
            "insert into chunks_fts(content, document_id, chunk_id) values (?,?,?)",
            (to_index_text(str(row["content"])), row["document_id"], row["id"]),
        )
    # 向量必须按**片段**过滤，而不是按文档：被跳过的文档（文件丢了 / 摘要不符）
    # 名下的片段不搬，但它的向量还在源表里——只按文档过滤元数据、向量照单全收，
    # 结果就是 437 条片段配 439 条向量。两个孤儿向量不会被检索到（join 时丢），
    # 于是**看起来一切正常**，只有"三处条数一致"这条校验能发现。
    kept_chunks: set[int] = set()
    for row in chunk_meta:
        if str(row["DocumentId"]) in kept_docs:
            kept_chunks.add(int(row["ChunkId"]))
            target.execute(
                "insert into chunks_vec(ChunkId, DocumentId, Seq, Heading, PageFrom, PageTo)"
                " values (?,?,?,?,?,?)",
                (
                    row["ChunkId"],
                    row["DocumentId"],
                    row["Seq"],
                    row["Heading"],
                    row["PageFrom"],
                    row["PageTo"],
                ),
            )
    orphans = 0
    for row in vectors:
        if int(row[0]) not in kept_chunks:
            orphans += 1
            continue
        target.execute(
            "insert into vec_chunks_vec(ChunkId, Embedding) values (?,?)", (row[0], row[1])
        )
    if orphans:
        report.warn(f"源里有 {orphans} 条向量指向已跳过的片段，未搬（否则索引条数会不一致）。")

    # 索引写的模型必须写进状态：否则 `index_is_stale` 要么误报要么漏报
    system_state.put(target, system_state.EMBEDDING_MODEL_KEY, EMBEDDING_MODEL, now=now)
    system_state.put(target, system_state.EMBEDDING_SYNCED_KEY, EMBEDDING_MODEL, now=now)
    target.commit()
    target.execute("pragma foreign_keys = on")

    report.counts["documents_kept"] = len(kept_docs)
    return report


def verify(db_path: Path, storage_root: Path) -> Report:
    """结构校验。**每条都能独立失败**，不是"跑完没报错"。"""
    report = Report()
    conn = engine.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 表名来自本函数内的常量元组，不接受外部输入（S608 的口子只对字符串拼接有意义）
    counts = {
        table: int(conn.execute(f"select count(*) from {table}").fetchone()[0])  # noqa: S608
        for table in ("tenants", "users", "providers", "provider_models", "documents", "chunks")
    }
    report.counts.update(counts)

    report.check("外键完整", conn.execute("pragma foreign_key_check").fetchall() == [])
    report.check(
        "每份文档都有片段",
        conn.execute(
            "select count(*) from documents d where not exists"
            " (select 1 from chunks c where c.document_id = d.id)"
        ).fetchone()[0]
        == 0,
    )
    report.check(
        "每个片段都有向量",
        conn.execute(
            "select count(*) from chunks c where not exists"
            " (select 1 from vec_chunks_vec v where v.ChunkId = c.id)"
        ).fetchone()[0]
        == 0,
    )
    report.check(
        "每个片段都能全文检索到",
        conn.execute(
            "select count(*) from chunks c where not exists"
            " (select 1 from chunks_fts f where f.chunk_id = c.id)"
        ).fetchone()[0]
        == 0,
    )
    report.check(
        "三处索引条数一致",
        conn.execute("select count(*) from chunks").fetchone()[0]
        == conn.execute("select count(*) from chunks_vec").fetchone()[0]
        == conn.execute("select count(*) from vec_chunks_vec").fetchone()[0]
        == conn.execute("select count(*) from chunks_fts").fetchone()[0],
    )
    report.check(
        "文件都在盘上",
        all(
            (storage_root / str(row["stored_path"])).exists()
            for row in conn.execute("select stored_path from documents")
        ),
    )
    report.check(
        "索引模型状态一致",
        system_state.embedding_state(conn) == (EMBEDDING_MODEL, EMBEDDING_MODEL)
        and not system_state.index_is_stale(conn),
    )

    # 向量维度：随便取一条，解出来的分量数必须等于 EMBEDDING_DIM
    sample = conn.execute(
        "select vec_to_json(Embedding) as v from vec_chunks_vec limit 1"
    ).fetchone()
    if sample is not None:
        dim = len(json.loads(str(sample["v"])))
        report.check("向量维度", dim == EMBEDDING_DIM, f"{dim} vs {EMBEDDING_DIM}")

    # 全文路真的能搜到东西（不是"表里有行"）
    #
    # 查询串必须**先过同一个分词器**：`chunks_fts.content` 里存的是两字片段
    # （`to_index_text`），直接拿原文前 4 个字去 MATCH 会命中 0 条——而且
    # 带点号/冒号的原文还会被 fts5 当成列名或语法，报 `no such column`。
    sample_text = str(
        conn.execute("select content from chunks order by id limit 1").fetchone()["content"]
    )
    tokens = tokenize(sample_text)[:3]
    query = " ".join(f'"{token}"' for token in tokens)
    hits = conn.execute(
        "select count(*) from chunks_fts where chunks_fts match ?", (query,)
    ).fetchone()[0]
    report.check("全文检索可命中", hits > 0, f"查询「{query}」命中 {hits} 条")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 C# 版数据搬进 Python 实现")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--target", type=Path, default=Path("data"), help="目标数据目录")
    parser.add_argument("--password", default="", help="给迁移过来的账号设的初始口令")
    parser.add_argument(
        "--tenant", default=None, help="文档归属的团队 id（默认第一个所有者所在团队）"
    )
    parser.add_argument("--force", action="store_true", help="目标库非空时覆盖（会留 .bak）")
    parser.add_argument("--dry-run", action="store_true", help="只读源库并打印计划")
    args = parser.parse_args(argv)

    if not args.dry_run and (not args.password or len(args.password) < 8):
        print(
            "必须先给 --password（至少 8 位）：源库的 PBKDF2 哈希在本实现里验不过。",
            file=sys.stderr,
        )
        return 2
    secret = crypto.require_secret(__import__("os").environ.get("AWB_SECRET_KEY"))

    db_path = args.target / "agentworkbench.db"
    storage_root = args.target / "files"
    source = _open_source(args.source)
    print(f"源库：{args.source}")
    print(f"目标：{db_path}")
    if not args.dry_run:
        _prepare_target(db_path, force=args.force)

    report = migrate(
        source,
        db_path=db_path,
        storage_root=storage_root,
        secret=secret,
        password=args.password,
        tenant_id=args.tenant,
        dry_run=args.dry_run,
    )
    print("\n源库清单：" + "，".join(f"{key} {value}" for key, value in report.counts.items()))
    if report.users:
        print("账号（口令 = --password）：")
        for name, email, role in report.users:
            print(f"  {name:<6} {email:<26} {role}")
    if report.providers:
        print("供应商：")
        for name, base_url in report.providers:
            print(f"  {name:<14} {base_url}")

    if args.dry_run:
        print("\n干跑结束：没有写任何文件。")
    else:
        for message in report.warnings:
            print(f"注意：{message}")
        print("\n校验：")
        result = verify(db_path, storage_root)
        for label, ok, detail in result.checks:
            print(f"  {'PASS' if ok else 'FAIL'}  {label}{('：' + detail) if detail else ''}")
        if not all(ok for _, ok, _ in result.checks):
            print("\n有校验未通过，先别跑业务。", file=sys.stderr)
            return 1
        print("\n迁移完成。下一步：")
        print("  AWB_SECRET_KEY=... AWB_EMBEDDING__BASE_URL=https://api.siliconflow.cn/v1 \\")
        print("  AWB_EMBEDDING__API_KEY=... AWB_EMBEDDING__MODEL=BAAI/bge-m3 \\")
        print("  python -m agentworkbench.cli serve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
