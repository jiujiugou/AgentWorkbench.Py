"""题库重标：块边界全变后的 gold 对齐（T-002）。

换 LlamaIndex 切块后 439 → 532 块，`seq` 全变，`questions.json` 的
gold（file, seq）全部失效。重标**不靠人**，靠内容对齐：

1. 用生产组件（`chunk_pages` + `write_index` + `HashEmbedder`）把 fixture 库
   重建为 llama 版副本——三处索引写法与生产完全一致，评测检索的就是真实形态
2. 旧 gold 块的文本在旧库里有；在新库里找「内容包含这段文本」的新块
3. 一个新块包含 → 直接换 seq；文本被重叠复制到两块 → 两块都进 gold（命中任一算对，
   与 ambiguous 同语义）；找不到 → 退到「旧块开头 60 字」对齐（跨页旧块用）；再找不到 →
   标 `relabel_failed` 人工看，**不许静默丢掉**

`evals/self/` 一个字节不动：v1 题库留在原地（C# 基线可对账），重标结果写
`questions-v2.json`，llama 版库副本写 `agentworkbench-llama.db`（fixtures 不进 git）。

用法：python evals/relabel_gold.py
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentworkbench.db import engine  # noqa: E402
from agentworkbench.ingest.parsing.chunker import chunk_pages  # noqa: E402
from agentworkbench.llm.embeddings import HashEmbedder  # noqa: E402
from agentworkbench.rag.indexer import write_index  # noqa: E402

EVALS = ROOT / "evals"
SRC_DB = EVALS / "fixtures" / "agentworkbench.db"
V2_DB = EVALS / "fixtures" / "agentworkbench-llama.db"
QUESTIONS_V1 = EVALS / "self" / "questions.json"
QUESTIONS_V2 = EVALS / "self" / "questions-v2.json"
NOW = "2026-11-02T00:00:00"


def _norm(text: str) -> str:
    """归一化：去掉全部空白。旧块与新块来自同一页文本，字符序列应完全一致。"""
    return "".join(text.split())


def rebuild_llama_db() -> sqlite3.Connection:
    """fixture 库副本 + 生产切块 + 生产索引写入。向量用确定性占位（评测只走 FTS 路）。"""
    shutil.copy(SRC_DB, V2_DB)
    conn = engine.connect(V2_DB)
    with conn:
        conn.execute("delete from chunks_fts")
        conn.execute("delete from vec_chunks_vec")
        conn.execute("delete from chunks_vec")
        conn.execute("delete from chunks")

    pages = conn.execute(
        "select document_id, page_no, raw_text from document_pages order by document_id, page_no"
    ).fetchall()
    by_doc: dict[str, list[str]] = defaultdict(list)
    for doc_id, _page_no, text in pages:
        by_doc[str(doc_id)].append(str(text))

    embedder = HashEmbedder()
    total = 0
    for doc_id, page_texts in by_doc.items():
        drafts = chunk_pages(page_texts)
        if not drafts:
            continue
        write_index(
            conn,
            document_id=doc_id,
            drafts=drafts,
            embeddings=embedder([d.content for d in drafts]),
            now=NOW,
        )
        total += len(drafts)
    conn.commit()
    print(f"llama 库副本：{V2_DB.name}，{len(by_doc)} 篇 / {total} 块")
    return conn


def relabel(conn: sqlite3.Connection) -> dict[str, int]:
    spec = json.loads(QUESTIONS_V1.read_text(encoding="utf-8"))
    old = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True)
    old.row_factory = sqlite3.Row

    stats: dict[str, int] = defaultdict(int)
    for question in spec["questions"]:
        new_gold: list[dict[str, object]] = []
        for entry in question.get("gold", []):
            row = old.execute(
                "select ch.content from documents d join chunks ch on ch.document_id = d.id"
                " where d.file_name = ? and ch.seq = ?",
                (entry["file"], entry["seq"]),
            ).fetchone()
            if row is None:
                stats["old_gold_missing"] += 1
                print(f"!! {question['id']}: 旧库里找不到 {entry['file']} seq={entry['seq']}")
                continue
            needle = _norm(str(row["content"]))
            candidates = [
                (int(r["seq"]), _norm(str(r["content"])))
                for r in conn.execute(
                    "select ch.seq, ch.content from documents d join chunks ch"
                    " on ch.document_id = d.id where d.file_name = ? order by ch.seq",
                    (entry["file"],),
                ).fetchall()
            ]

            exact = [seq for seq, content in candidates if needle and needle in content]
            if exact:
                mode = "exact"
            else:
                head = needle[:60]
                exact = [seq for seq, content in candidates if head and head in content]
                mode = "head60"

            if not exact:
                stats["failed"] += 1
                print(f"!! {question['id']}: 对齐失败 {entry['file']} seq={entry['seq']}")
                continue
            stats[mode] += 1
            if len(exact) > 1:
                stats["multi_hit"] += 1
            for seq in exact:
                new_gold.append({"file": entry["file"], "seq": seq})
            question.setdefault("relabel", {})[f"{entry['file']}#{entry['seq']}"] = {
                "mode": mode,
                "to": exact,
            }
        if new_gold:
            question["gold"] = new_gold
        elif "gold" in question:
            del question["gold"]  # 全部对齐失败的题：宁可挖掉 gold 也不留错误答案

    spec["note_v2"] = (
        "T-002 重标：切块器换 LlamaIndex（800/120）后 439→532 块，seq 全变。"
        "gold 由内容对齐生成（relabel 字段记录每条的对齐方式与去向）。"
        "v1 保留在 questions.json（C# 基线可对账），两份不互相同步。"
    )
    QUESTIONS_V2.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return dict(stats)


def main() -> int:
    conn = rebuild_llama_db()
    stats = relabel(conn)
    print("重标统计：", stats)
    print(f"题库：{QUESTIONS_V2.name}")
    return 0 if stats.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
