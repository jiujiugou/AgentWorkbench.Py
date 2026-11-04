#!/usr/bin/env python
"""F3 检索评测（只测检索，不测生成）。

用法：
    python tests/benchmark/rag/run_rag_eval.py --mode fts

模式：
    fts     纯全文路（MATCH + LIKE），零外部依赖、可离线复算
    hybrid  全文 + 向量（需要 embedding 服务，暂未实现）

为什么要单独一个脚本而不是 dotnet test：
    检索质量是概率判据，写进单元测试只会得到一个随机摇摆的红灯。
    这里输出的是可对比的数字，进 git，跟代码版本对齐。

gold 用 (file, seq) 而不是 chunk_id —— chunk id 是自增的，
重解析一次就全变，而这件事不会报错，只会让 HitRate 悄悄归零。
"""
import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = ROOT / "src/AgentWorkbench.Webapi/data/agentworkbench.db"
QUESTIONS = Path(__file__).with_name("questions.json")

# --- 两种分词，必须与索引格式匹配 ---------------------------------------
# legacy：老实现（按空白/标点切整句），配 trigram 索引
# bigram：现实现（连续汉字滑窗取 bigram），配 unicode61 索引
# 两边不一致时结果是 0 命中，而且不报错 —— 所以下面有 assert_index_format()
LEGACY_SPLIT = re.compile(r"[\s\W]+", re.UNICODE)
ASCII_OR_HAN = re.compile(r"[A-Za-z0-9_#](?:[A-Za-z0-9_#.\-/]*[A-Za-z0-9_#])?|[\u4e00-\u9fff]+")


def legacy_terms(query: str):
    out, seen = [], set()
    for t in LEGACY_SPLIT.split(query):
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def bigram_terms(query: str, limit: int = 24):
    """复刻 BigramTokenizer.Tokens。"""
    out = []
    for m in ASCII_OR_HAN.finditer(query):
        run = m.group()
        if run[0] < "\u0080":
            out.append(run)
            continue
        if len(run) == 1:
            continue
        out.extend(run[i:i + 2] for i in range(len(run) - 1))
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq[:limit]


def index_text(text: str) -> str:
    """复刻 `BigramTokenizer.ToIndexText`：**不去重、不截断**。

    不能用 `bigram_terms` 算索引文本——那是查询侧形态（去重 + 默认截 24），
    索引侧两者都不做。混用会让每块有重复 bigram 的库被误报成"原文 trigram"。
    """
    out = []
    for match in ASCII_OR_HAN.finditer(text):
        run = match.group()
        if run[0] < "\u0080":
            out.append(run)
            continue
        if len(run) == 1:
            continue
        out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return " ".join(out)


def single_han_chars(query: str):
    return [m.group() for m in ASCII_OR_HAN.finditer(query)
            if m.group()[0] >= "\u0080" and len(m.group()) == 1]


def assert_index_format(con, tokenizer: str) -> None:
    """索引格式与查询分词器必须一致，否则 0 命中而不报错。这里让它显式报出来。"""
    row = con.execute(
        """SELECT ch.content, f.content FROM chunks ch
           JOIN chunks_fts f ON f.chunk_id = ch.id LIMIT 1"""
    ).fetchone()
    if not row:
        print("!! chunks_fts 为空：迁移后还没重建索引，先跑 POST /api/knowledge/documents/{id}/index",
              file=sys.stderr)
        return

    original, indexed = row
    expect = index_text(original)
    # 精确相等，不留「前缀相同就算对」的兜底：那个兜底让错的复刻在重复 bigram
    # 出现在前 40 字符之后时也能通过。两个夹具库 971 块实测全部精确相等。
    looks_bigram = indexed == expect
    want_bigram = tokenizer == "bigram"

    if looks_bigram != want_bigram:
        kind = "bigram(unicode61)" if looks_bigram else "原文(trigram)"
        print(f"!! 索引是 {kind}，但查询用的是 {tokenizer} —— "
              f"两边不一致会得到 0 命中且不报错。用 --tokenizer "
              f"{'bigram' if looks_bigram else 'legacy'} 重跑。", file=sys.stderr)
    else:
        kind = "bigram + unicode61" if looks_bigram else "原文 + trigram"
        print(f"索引格式: {kind}（与查询分词器一致）")


def fts_search(con, query: str, k: int, tokenizer: str):
    """复刻 HybridRetriever.FtsSearchAsync：先 MATCH，命中为 0 才走 LIKE 兜底。"""
    if tokenizer == "bigram":
        terms = bigram_terms(query)
        min_len = 0
    else:
        terms = legacy_terms(query)
        min_len = 3  # trigram 需要 >=3 字符

    fts_terms = [t for t in terms if len(t) >= min_len]
    if fts_terms:
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in fts_terms)
        try:
            rows = con.execute(
                """SELECT chunk_id FROM chunks_fts
                   WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?""",
                (match, k),
            ).fetchall()
            if rows:
                return [r[0] for r in rows]
        except sqlite3.OperationalError:
            pass

    singles = single_han_chars(query)
    if not singles:
        return []
    clauses = " OR ".join(["content LIKE ?"] * len(singles))
    rows = con.execute(
        f"SELECT chunk_id FROM chunks_fts WHERE {clauses} ORDER BY chunk_id LIMIT ?",
        [f"%{c}%" for c in singles] + [k],
    ).fetchall()
    return [r[0] for r in rows]


def resolve_gold(con, gold):
    """(file, seq) -> chunk_id 集合。同一 file+seq 命中多块时全部收进来（重复文档）。"""
    ids = set()
    for g in gold:
        for (cid,) in con.execute(
            """SELECT ch.id FROM documents d JOIN chunks ch ON ch.document_id = d.id
               WHERE d.file_name = ? AND ch.seq = ?""",
            (g["file"], g["seq"]),
        ):
            ids.add(cid)
    return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--mode", choices=["fts"], default="fts")
    ap.add_argument("--tokenizer", choices=["bigram", "legacy"], default="bigram",
                    help="必须与索引格式匹配：bigram 配 unicode61，legacy 配 trigram")
    args = ap.parse_args()

    spec = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    print(f"=== {args.mode} 路 · TopK={args.top_k} · 库 {Path(args.db).name} ===")
    assert_index_format(con, args.tokenizer)

    stats = defaultdict(lambda: {"n": 0, "hit": 0, "rr": 0.0, "miss": []})
    neg = []

    for q in spec["questions"]:
        if q["kind"] == "negative":
            got = fts_search(con, q["question"], args.top_k, args.tokenizer)
            neg.append((q["id"], len(got)))
            continue

        gold = resolve_gold(con, q["gold"])
        if not gold:
            print(f"!! {q['id']} gold 解析为空，跳过", file=sys.stderr)
            continue

        got = fts_search(con, q["question"], args.top_k, args.tokenizer)
        rank = next((i + 1 for i, cid in enumerate(got) if cid in gold), 0)

        s = stats[q["kind"]]
        s["n"] += 1
        if rank:
            s["hit"] += 1
            s["rr"] += 1.0 / rank
        else:
            s["miss"].append(q["id"])

    total = {"n": 0, "hit": 0, "rr": 0.0}
    print(f"{'kind':<12}{'n':>4}{'HitRate@k':>12}{'MRR@k':>9}")
    for kind in ("literal", "paraphrase", "ambiguous"):
        s = stats[kind]
        if not s["n"]:
            continue
        print(
            f"{kind:<12}{s['n']:>4}{s['hit'] / s['n']:>12.3f}{s['rr'] / s['n']:>9.3f}"
        )
        total["n"] += s["n"]
        total["hit"] += s["hit"]
        total["rr"] += s["rr"]
        if s["miss"]:
            print(f"{'':12}未命中: {', '.join(s['miss'])}")

    if total["n"]:
        print("-" * 37)
        print(
            f"{'合计':<12}{total['n']:>4}"
            f"{total['hit'] / total['n']:>12.3f}{total['rr'] / total['n']:>9.3f}"
        )

    if neg:
        print(f"\n负控 {len(neg)} 条（应返回空；>0 表示会拿无关内容去答）:")
        for qid, n in neg:
            print(f"  {qid}: 召回 {n} 块" + ("" if n == 0 else "  <-- 非空"))

    print("\n注：命中 = gold 块出现在前 k 名；RR = 1/排名。"
          "绝对值不可跨库比较（库越小越容易命中），只能同库前后对比。")


if __name__ == "__main__":
    main()
