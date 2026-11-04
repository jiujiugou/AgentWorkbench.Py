#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F3 知识问答「可用」子问题：索引一致性检查（列表说"可检索"，搜索却查不到）。

背景
----
现象一：库是空的 -> 用户搜什么都空。
现象二：库(或这篇文档)其实是空的，列表却仍显示"可检索" -> 用户以为检索/阈值坏了，
        去调召回参数，方向全错。后者比前者更糟，因为它不会报错。

只比"三个表行数相等"是抓不到这两种情况的：63 篇文档里有一篇 chunks=0、
或者某篇的块全在 chunks 但 chunks_fts 里一行都没有，总量对不上才会红，
而局部坏掉时总量依然"看起来正常"。所以本脚本按四类逐一验证。

检查项
------
[a] 文档 status=parsed 但 chunks=0            -> 切不出块，语义上必然不可检索
[b] chunks 与 chunks_fts 不一致               -> b1 有块无全文行(全文搜不到) / b2 幽灵行(正文已删仍能命中)
[c] chunks 与 chunks_vec 不一致               -> c1 有块无向量(语义搜不到) / c2 幽灵向量
    / c3 chunks_vec 元数据在、vec0 存储里没有（列表显示 full，语义检索仍永远查不到）
[d] 复刻 IndexStates.Describe：被判为 full/text_only/partial 的文档，
    用它自己的块正文构造一次真实 FTS5 MATCH，必须命中它自己；命中 0 就是
    "列表说可检索但搜不到"。这一步才是现象二的正面检查。
[e] 库级真空：0 篇文档，或 63 篇文档但没有任何一篇可检索。
    没有这一条，空库会让 a~d 全部空转通过——脚本永远绿，而用户搜什么都空。

退出码：发现问题 1，全干净 0。

字段与判据都是核对过源码的，不是猜的：
  * IndexStates.Describe —— src/AgentWorkbench.Knowledge.Abstractions/IKnowledgeEngine.cs:39
      failed -> failed；非 parsed -> pending；chunks==0 -> empty；vectors>=chunks -> full；
      vectors==0 -> text_only；否则 partial。可检索 = full / text_only / partial。
  * 列表页的"向量数"取自 chunks_vec —— src/AgentWorkbench.Persistence/DocumentRepository.cs:203
      （SELECT COUNT(1) FROM chunks_vec WHERE DocumentId=?；表不存在按 0 处理）
      注意：chunks_vec 是普通表（ChunkId/DocumentId/Seq/...），真正的 vec0 虚表叫
      vec_chunks_vec，没有扩展读不了，所以不在本脚本的读取路径里。
  * chunks_fts 是普通 FTS5 表（不是 external content 表），content 列存的是
    bigram 化后的 token 串 —— src/AgentWorkbench.Knowledge.Local/Indexing/ChunkIndexer.cs:237，
    查询侧必须调同一个 BigramTokenizer。本脚本按
    src/AgentWorkbench.Knowledge.Local/Retrieval/BigramTokenizer.cs:28 的正则原样复刻，
    并用"重新分词 chunks.content 与 chunks_fts.content 逐块比对"自校准；
    生产库 439 块漂移 0 条，说明复刻与 C# 一致（漂移不为 0 会直接算进 [b]）。

反向检查（证伪过程，可复现）
--------------------------
生产库当前是干净的，所以"跑出 0 条"本身不能证明脚本有效。下面这段是可复现的证伪：

    python tests/benchmark/rag/check_index_consistency.py --self-test

  --self-test 做四件事，全程只读生产库（用 Connection.backup() 复制到系统临时目录，
  WAL 模式下直接 cp 会拿到坏库，所以不能用复制文件的办法）：
    1. Backup 生产库 -> temp/clean.db，对其跑检查，断言退出码 0、无任何问题；
    2. Backup 生产库 -> temp/broken.db，人为注入四类坏数据：
         - 删掉某文档的全部 chunks_fts 行          -> 必须报 [b]
         - 删掉某文档的全部 chunks_vec 行          -> 必须报 [c]
         - 插入一条 status=parsed 但没有 chunks 的文档 -> 必须报 [a]
         - 删掉"只有 1 块"的文档的那 1 行全文行    -> 该类报 [b]，且该文档被判
           full 却 MATCH 不到 -> 必须报 [d]
       （注入点各挑不同文档，避免互相掩盖；[d] 单独挑单块文档，因为只删一块时，
        多块文档还有别的块能命中，不会暴露 [d]。）
    3. 对 broken.db 跑检查，断言退出码 1 且 [a][b][c][d] 都 > 0；
    4. 清理临时文件，打印 PASS/FAIL。

若把上面任一检查删掉（例如只留 [d]），[a][b][c] 就不再被覆盖，脚本重新变成"永远绿"。
这就是"如果脚本永远返回绿，那两件事还会不会发生"的答案所在。

用法
----
    python tests/benchmark/rag/check_index_consistency.py
    python tests/benchmark/rag/check_index_consistency.py --db <path-to.db>
    python tests/benchmark/rag/check_index_consistency.py --self-test

只读：连接用 mode=ro + PRAGMA query_only=ON，不改任何表，不建任何文件到仓库里。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DB = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "AgentWorkbench.Webapi"
    / "data"
    / "agentworkbench.db"
)

MAX_DETAILS = 10

# 与 BigramTokenizer.SegmentPattern() 逐字一致
_SEGMENT = re.compile(r"[A-Za-z0-9_#](?:[A-Za-z0-9_#.\-/]*[A-Za-z0-9_#])?|[\u4e00-\u9fff]+")

# 与 HybridRetriever.MaxQueryTokens 一致
_MAX_QUERY_TOKENS = 24


# ---------------------------------------------------------------- 分词（复刻 C#）


def tokens(text: str | None) -> list[str]:
    """BigramTokenizer.Tokens：ASCII 词原样、连续汉字滑窗 bigram、单字不入索引。"""
    if not text:
        return []
    out: list[str] = []
    for match in _SEGMENT.finditer(text):
        run = match.group(0)
        if run[0] < "\u0080":
            out.append(run)
            continue
        if len(run) == 1:
            continue
        out.extend(run[i : i + 2] for i in range(len(run) - 1))
    return out


def index_text(text: str | None) -> str:
    """BigramTokenizer.ToIndexText：写入 chunks_fts.content 的形态。"""
    return " ".join(tokens(text))


def match_expression(text: str) -> str:
    """按 HybridRetriever 的写法构造 MATCH 串：去重取前 24 个 token，OR 连接。"""
    seen: list[str] = []
    for token in tokens(text):
        if token not in seen:
            seen.append(token)
        if len(seen) >= _MAX_QUERY_TOKENS:
            break
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in seen)


# ---------------------------------------------------------------- 数据模型


@dataclass
class DocRow:
    id: str
    file_name: str
    status: str
    chunks: int
    fts: int
    vectors: int
    probe_text: str | None = None  # 最长块的正文，用于真实 MATCH 探针


@dataclass
class Finding:
    category: str
    reason: str
    doc_id: str = ""
    file_name: str = ""
    status: str = ""
    chunks: int = -1
    extra: str = ""


@dataclass
class Report:
    db: str
    documents: int = 0
    chunks: int = 0
    fts_rows: int = 0
    vector_rows: int = 0
    searchable: int = 0
    findings: dict[str, list[Finding]] = field(
        default_factory=lambda: {k: [] for k in ("a", "b", "c", "d", "e")}
    )
    fatal: str = ""

    def add(self, finding: Finding) -> None:
        self.findings[finding.category].append(finding)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.findings.values())


# ---------------------------------------------------------------- 检查逻辑


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)
    ).fetchone()
    return row is not None


def describe(status: str, chunk_count: int, vector_count: int) -> str:
    """IndexStates.Describe 的 Python 复刻（IKnowledgeEngine.cs:39）。"""
    if status == "failed":
        return "failed"
    if status != "parsed":
        return "pending"
    if chunk_count == 0:
        return "empty"
    if vector_count >= chunk_count:
        return "full"
    return "text_only" if vector_count == 0 else "partial"


def load_documents(conn: sqlite3.Connection, has_fts: bool, has_vec: bool) -> list[DocRow]:
    fts_expr = "(SELECT COUNT(1) FROM chunks_fts WHERE document_id = d.id)" if has_fts else "0"
    vec_expr = "(SELECT COUNT(1) FROM chunks_vec WHERE DocumentId = d.id)" if has_vec else "0"
    sql = f"""
        SELECT d.id, d.file_name, d.status,
               (SELECT COUNT(1) FROM chunks WHERE document_id = d.id) AS chunk_count,
               {fts_expr} AS fts_count,
               {vec_expr} AS vec_count
        FROM documents d
        ORDER BY d.file_name
    """
    docs = []
    for row in conn.execute(sql):
        docs.append(DocRow(row[0], row[1], row[2], row[3], row[4], row[5]))
    return docs


def probe_document(conn: sqlite3.Connection, doc: DocRow) -> Finding | None:
    """用该文档自己的块正文构造真实 MATCH，命中 0 条即"列表说可检索但搜不到"。"""
    expr = match_expression(doc.probe_text or "")
    if not expr:
        return Finding(
            "d", "块正文切不出任何 bigram token，索引里没有可命中的词", doc.id, doc.file_name,
            doc.status, doc.chunks, "probe_tokens=0",
        )
    try:
        hits = conn.execute(
            "SELECT COUNT(1) FROM chunks_fts WHERE chunks_fts MATCH ? AND document_id = ?",
            (expr, doc.id),
        ).fetchone()[0]
    except sqlite3.OperationalError as exc:
        return Finding(
            "d", f"实际 MATCH 查询报错：{exc}", doc.id, doc.file_name, doc.status, doc.chunks,
        )
    if hits == 0:
        return Finding(
            "d", "列表判为可检索，但用本文档块正文构造的 MATCH 命中 0 条", doc.id, doc.file_name,
            doc.status, doc.chunks, f"probe={expr[:60]}…",
        )
    return None


def analyze(conn: sqlite3.Connection, db_label: str) -> Report:
    report = Report(db=db_label)

    has_fts = _table_exists(conn, "chunks_fts")
    has_vec = _table_exists(conn, "chunks_vec")

    docs = load_documents(conn, has_fts, has_vec)
    report.documents = len(docs)
    report.chunks = conn.execute("SELECT COUNT(1) FROM chunks").fetchone()[0]
    report.fts_rows = conn.execute("SELECT COUNT(1) FROM chunks_fts").fetchone()[0] if has_fts else 0
    report.vector_rows = conn.execute("SELECT COUNT(1) FROM chunks_vec").fetchone()[0] if has_vec else 0

    by_id = {d.id: d for d in docs}

    # ---- [a] parsed 但 chunks=0
    for doc in docs:
        if doc.status == "parsed" and doc.chunks == 0:
            report.add(Finding(
                "a", "status=parsed 但一块都没切出来（列表会显示 empty/不可检索，全文与向量都无内容）",
                doc.id, doc.file_name, doc.status, doc.chunks,
            ))

    # ---- [b] chunks <-> chunks_fts
    if not has_fts:
        for doc in docs:
            if doc.chunks > 0:
                report.add(Finding(
                    "b", "chunks_fts 表不存在，任何全文检索都会失败",
                    doc.id, doc.file_name, doc.status, doc.chunks,
                ))
    else:
        for row in conn.execute(
            """
            SELECT c.document_id, COUNT(1)
            FROM chunks c
            WHERE NOT EXISTS (SELECT 1 FROM chunks_fts f WHERE f.chunk_id = c.id)
            GROUP BY c.document_id
            """
        ):
            doc = by_id.get(row[0])
            report.add(Finding(
                "b", f"有 {row[1]} 块在 chunks 里但 chunks_fts 无对应行（全文搜不到）",
                row[0], doc.file_name if doc else "?", doc.status if doc else "?",
                doc.chunks if doc else -1, f"missing_fts={row[1]}",
            ))
        for row in conn.execute(
            """
            SELECT f.document_id, COUNT(1)
            FROM chunks_fts f
            WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.id = f.chunk_id)
            GROUP BY f.document_id
            """
        ):
            doc = by_id.get(row[0])
            report.add(Finding(
                "b", f"有 {row[1]} 行 chunks_fts 找不到对应块（幽灵命中，正文已不存在）",
                row[0], doc.file_name if doc else "（文档已删）", doc.status if doc else "-",
                doc.chunks if doc else 0, f"orphan_fts={row[1]}",
            ))
        # 内容漂移：索引内容与重新分词结果不一致，MATCH 会静默 0 命中
        drift: dict[str, list[int]] = {}
        for row in conn.execute(
            "SELECT c.document_id, c.content, f.content FROM chunks c JOIN chunks_fts f ON f.chunk_id = c.id"
        ):
            if index_text(row[1]) != (row[2] or ""):
                entry = drift.setdefault(row[0], [0, 0])
                entry[0] += 1
        for doc_id, entry in drift.items():
            doc = by_id.get(doc_id)
            report.add(Finding(
                "b", f"有 {entry[0]} 块的 chunks_fts.content 与块正文重新分词结果不一致（分词/内容已漂移）",
                doc_id, doc.file_name if doc else "?", doc.status if doc else "?",
                doc.chunks if doc else -1, f"drifted={entry[0]}",
            ))

    # ---- [c] chunks <-> chunks_vec
    if not has_vec:
        for doc in docs:
            if doc.chunks > 0:
                report.add(Finding(
                    "c", "chunks_vec 表不存在，列表的向量数全按 0 处理（只能全文检索）",
                    doc.id, doc.file_name, doc.status, doc.chunks,
                ))
    else:
        for row in conn.execute(
            """
            SELECT c.document_id, COUNT(1)
            FROM chunks c
            WHERE NOT EXISTS (SELECT 1 FROM chunks_vec v WHERE v.ChunkId = c.id)
            GROUP BY c.document_id
            """
        ):
            doc = by_id.get(row[0])
            report.add(Finding(
                "c", f"有 {row[1]} 块没有向量（语义检索覆盖不到）",
                row[0], doc.file_name if doc else "?", doc.status if doc else "?",
                doc.chunks if doc else -1, f"missing_vec={row[1]}",
            ))
        for row in conn.execute(
            """
            SELECT v.DocumentId, COUNT(1)
            FROM chunks_vec v
            WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.id = v.ChunkId)
            GROUP BY v.DocumentId
            """
        ):
            doc = by_id.get(row[0])
            report.add(Finding(
                "c", f"有 {row[1]} 条向量找不到对应块（幽灵向量，占 top-K 名额）",
                row[0], doc.file_name if doc else "（文档已删）", doc.status if doc else "-",
                doc.chunks if doc else 0, f"orphan_vec={row[1]}",
            ))

        # 向量真正的落盘位置是 vec0 虚表 vec_chunks_vec（没有扩展读不了），
        # 但它的影子表 vec_chunks_vec_rowids.rowid 与 chunks.id 一一对应（生产库 439/439 对上）。
        # 少了这一层，就会出现「chunks_vec 元数据还在、向量数据已丢」——
        # 列表照旧显示 full，语义检索却永远查不到，正是本子问题要抓的形态。
        if _table_exists(conn, "vec_chunks_vec_rowids"):
            try:
                stored = conn.execute("SELECT COUNT(1) FROM vec_chunks_vec_rowids").fetchone()[0]
                if stored < report.vector_rows:
                    for row in conn.execute(
                        """
                        SELECT v.DocumentId, COUNT(1)
                        FROM chunks_vec v
                        WHERE NOT EXISTS (
                            SELECT 1 FROM vec_chunks_vec_rowids r WHERE r.rowid = v.ChunkId
                        )
                        GROUP BY v.DocumentId
                        """
                    ):
                        doc = by_id.get(row[0])
                        report.add(Finding(
                            "c", f"有 {row[1]} 条向量只在 chunks_vec 元数据里，vec0 存储中不存在"
                                 f"（列表仍会显示 full，语义检索永远查不到）",
                            row[0], doc.file_name if doc else "（文档已删）",
                            doc.status if doc else "-", doc.chunks if doc else 0,
                            f"vec0_missing={row[1]}",
                        ))
            except sqlite3.OperationalError as exc:
                print(f"（提示）vec_chunks_vec_rowids 无法核对，跳过 vec0 层：{exc}")

    # ---- [d] Describe 判可检索 -> 真实 MATCH 必须命中它自己
    probe_rows = {}
    for row in conn.execute(
        """
        SELECT document_id, content FROM chunks
        WHERE id IN (SELECT MAX(id) FROM chunks GROUP BY document_id)
        """
    ):
        probe_rows[row[0]] = row[1]
    for doc in docs:
        state = describe(doc.status, doc.chunks, doc.vectors)
        if state not in ("full", "text_only", "partial"):
            continue
        report.searchable += 1
        doc.probe_text = probe_rows.get(doc.id)
        if not doc.probe_text:
            continue
        finding = probe_document(conn, doc)
        if finding is not None:
            finding.extra = f"state={state} {finding.extra}".strip()
            report.add(finding)

    # ---- [e] 库级真空：否则空库会让 a~d 全部空转通过
    if report.documents == 0:
        report.add(Finding(
            "e", "documents 表 0 行：库是空的，任何查询都必然为空（这不是检索或阈值问题）",
            status="-", chunks=0,
        ))
    elif report.chunks == 0:
        report.add(Finding(
            "e", f"{report.documents} 篇文档但 chunks 表 0 行：库里没有任何可检索内容",
            status="-", chunks=0,
        ))
    elif report.searchable == 0:
        report.add(Finding(
            "e", f"{report.documents} 篇文档、{report.chunks} 块，但没有一篇被判为可检索：搜索必然为空",
            status="-", chunks=0,
        ))

    return report


# ---------------------------------------------------------------- 输出


_CATEGORY_TITLES = {
    "a": "[a] parsed 但 chunks=0（切不出块）",
    "b": "[b] chunks 与 chunks_fts 不一致（全文路）",
    "c": "[c] chunks 与 chunks_vec 不一致（向量路）",
    "d": "[d] 列表判\"可检索\"但实测 MATCH 查不到",
    "e": "[e] 库级真空（无文档 / 无块 / 无可检索文档）",
}


def print_report(report: Report) -> None:
    print(f"数据库: {report.db}")
    print(
        f"总览: 文档 {report.documents} | 块 {report.chunks} | 全文行 {report.fts_rows} "
        f"| 向量 {report.vector_rows} | 判为可检索 {report.searchable}"
    )
    if report.fatal:
        print(f"致命: {report.fatal}")
    print()

    for key in ("a", "b", "c", "d", "e"):
        items = report.findings[key]
        print(f"{_CATEGORY_TITLES[key]:<46} {len(items):>3} 条")
        for item in items[:MAX_DETAILS]:
            line = (
                f"    - doc={item.doc_id or '-'} file={item.file_name or '-'} "
                f"status={item.status or '-'} chunks={item.chunks} | {item.reason}"
            )
            if item.extra:
                line += f" ({item.extra})"
            print(line)
        if len(items) > MAX_DETAILS:
            print(f"    … 另有 {len(items) - MAX_DETAILS} 条未列出")

    print()
    if report.total == 0:
        print("结论: 索引一致，未发现问题（退出码 0）")
    else:
        print(f"结论: 发现 {report.total} 处不一致（退出码 1）")


# ---------------------------------------------------------------- 只读连接


def open_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"数据库不存在: {path}")
    # mode=ro：不写库；query_only 再加一道保险
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def run_check(db_path: Path) -> int:
    conn = open_readonly(db_path)
    try:
        report = analyze(conn, str(db_path))
    finally:
        conn.close()
    print_report(report)
    return 1 if report.total else 0


# ---------------------------------------------------------------- 反向检查


def _backup(src: Path, dst: Path) -> None:
    """WAL 模式下必须用 SQLite 自己的 backup API；直接复制文件会拿到坏库。"""
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _run_subprocess(db_path: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--db", str(db_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout + proc.stderr


def _counts(output: str) -> dict[str, int]:
    counts = {}
    for key in ("a", "b", "c", "d", "e"):
        match = re.search(rf"^\[{key}\].*?(\d+) 条$", output, re.MULTILINE)
        counts[key] = int(match.group(1)) if match else -1
    return counts


def self_test(db_path: Path) -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="awb-index-check-"))
    ok = True
    try:
        clean = tmpdir / "clean.db"
        broken = tmpdir / "broken.db"
        _backup(db_path, clean)
        _backup(db_path, broken)

        # ---- 干净副本必须全绿
        code, output = _run_subprocess(clean)
        clean_counts = _counts(output)
        if code == 0 and all(v == 0 for v in clean_counts.values()):
            print("[self-test] 干净副本: 退出码 0，四类全 0 -> PASS")
        else:
            ok = False
            print(f"[self-test] 干净副本: 期望退出码 0 且全 0，实际 code={code} {clean_counts} -> FAIL")

        # ---- 注入坏数据（各挑不同文档，避免互相掩盖）
        conn = sqlite3.connect(str(broken))
        try:
            conn.execute("PRAGMA foreign_keys = OFF")

            # [b] 选块数最多的文档，删掉它全部全文行
            doc_b = conn.execute(
                "SELECT document_id FROM chunks GROUP BY document_id ORDER BY COUNT(1) DESC LIMIT 1"
            ).fetchone()[0]
            deleted_fts = conn.execute(
                "DELETE FROM chunks_fts WHERE document_id = ?", (doc_b,)
            ).rowcount

            # [c] 另选一篇块数最多的文档，删掉它全部向量
            doc_c = conn.execute(
                """
                SELECT v.DocumentId FROM chunks_vec v
                WHERE v.DocumentId <> ?
                GROUP BY v.DocumentId ORDER BY COUNT(1) DESC LIMIT 1
                """,
                (doc_b,),
            ).fetchone()[0]
            deleted_vec = conn.execute(
                "DELETE FROM chunks_vec WHERE DocumentId = ?", (doc_c,)
            ).rowcount

            # [a] 造一条 parsed 但没有块的文档
            conn.execute(
                """
                INSERT INTO documents (id, file_name, stored_path, sha256, size_bytes,
                                       status, page_count, error, created_at, parsed_at)
                VALUES ('selftest-empty-doc', 'selftest-empty.pdf', '/dev/null',
                        'selftest', 0, 'parsed', 0, NULL,
                        '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
                """
            )

            # [d] 找一篇只有 1 块的文档，删掉那唯一一行全文 -> 仍判 full，但 MATCH 必然 0 命中
            single = conn.execute(
                """
                SELECT c.document_id, c.id FROM chunks c
                WHERE c.document_id NOT IN (?, 'selftest-empty-doc')
                  AND (SELECT COUNT(1) FROM chunks WHERE document_id = c.document_id) = 1
                LIMIT 1
                """,
                (doc_b,),
            ).fetchone()
            if single is None:
                print("[self-test] 找不到单块文档，无法注入 [d] -> FAIL")
                ok = False
            else:
                conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (single[1],))

            conn.commit()
            print(
                f"[self-test] 注入: 删全文 {deleted_fts} 行(doc={doc_b[:8]})、"
                f"删向量 {deleted_vec} 条(doc={doc_c[:8]})、"
                f"造 1 篇 parsed 无块文档、"
                f"删单块文档全文 1 行(doc={single[0][:8] if single else '-'})"
            )
        finally:
            conn.close()

        # ---- 坏副本必须四类都报
        code, output = _run_subprocess(broken)
        broken_counts = _counts(output)
        print("[self-test] 坏副本计数: " + " ".join(f"{k}={v}" for k, v in broken_counts.items()))
        expected = ("a", "b", "c", "d")
        missing = [k for k in expected if broken_counts.get(k, 0) <= 0]
        if code == 1 and not missing:
            print("[self-test] 坏副本: 退出码 1 且 [a][b][c][d] 均报出 -> PASS")
        else:
            ok = False
            print(f"[self-test] 坏副本: 期望退出码 1 且四类全报，实际 code={code} 缺失={missing} -> FAIL")
            print(output)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    print("[self-test] 结论: PASS" if ok else "[self-test] 结论: FAIL")
    return 0 if ok else 1


# ---------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查知识库索引一致性：列表说可检索 vs 实际能不能搜到"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 数据库路径（只读）")
    parser.add_argument("--self-test", action="store_true", help="复制库并注入坏数据，证伪本脚本")
    args = parser.parse_args()

    if args.self_test:
        return self_test(args.db)
    return run_check(args.db)


if __name__ == "__main__":
    sys.exit(main())
