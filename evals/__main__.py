"""评测入口：一条命令跑完全部离线评测，把数字写进 baseline.json。

    python -m evals run      跑评测并写基线
    python -m evals run --no-write   只跑不写（改代码后先看数）
    python -m evals show     打印当前基线

目录约定——三类评测分开放，互不覆盖：

    evals/self/       自建 40 题（63 篇制造业 PDF，439 块），主力
    evals/public/     公开基准（RGB 拒答子集、C-MTEB 重排），外部校准
    evals/fixtures/   库副本，不进 git

为什么要一个入口：这五个脚本原来要人肉敲五次路径、再手工抄数字。
抄错一次，后面所有对比都是假的——而"数字抄错"不会报错，只会让你信一个不存在的基线。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

EVALS = Path(__file__).resolve().parent
SELF = EVALS / "self"
DB = EVALS / "fixtures" / "agentworkbench.db"
BASELINE = EVALS / "baseline.json"

REPORT_DIR = EVALS / "reports"


def _run(script: str, *args: str) -> tuple[int, str]:
    """跑一个脚本，返回（退出码，stdout+stderr）。不抛异常：评测失败也要留下输出。"""
    # 子进程的输出必须强制 UTF-8：Windows 下 stdout 被管道接走时默认用 GBK，
    # 而这里按 UTF-8 解码，中文标签会变成替换字符，`合计` 行永远匹配不上，
    # 结果是「脚本跑成功、解析器报 overall=None」。PYTHONUTF8 兜住 C 层输出。
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    proc = subprocess.run(
        [sys.executable, script, *args],
        cwd=SELF,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
        env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _int(pattern: str, text: str, group: int = 1) -> int | None:
    found = re.search(pattern, text)
    return int(found.group(group)) if found else None


def _float(pattern: str, text: str, group: int = 1) -> float | None:
    found = re.search(pattern, text)
    return float(found.group(group)) if found else None


def parse_index(text: str) -> dict[str, Any]:
    """索引一致性：库总量 + 可检索文档数。全为 0 时通常意味着指错了库。"""
    return {
        "documents": _int(r"文档\s+(\d+)", text),
        "chunks": _int(r"块\s+(\d+)", text),
        "fts_rows": _int(r"全文行\s+(\d+)", text),
        "vectors": _int(r"向量\s+(\d+)", text),
        "searchable": _int(r"判为可检索\s+(\d+)", text),
        "consistent": "未发现问题" in text,
    }


def parse_chunks(text: str) -> dict[str, Any]:
    """切块质量：接缝率。这一条是「块不能从半句开始」的量化形式。"""
    return {
        "total": _int(r"块总数\s*(\d+)", text),
        "seams": _int(r"接缝\s*(\d+)\s*条", text),
        "suspect_places": _int(r"发现\s*(\d+)\s*处", text),
        "suspect_blocks": _int(r"涉及\s*(\d+)\s*块", text),
        "ratio": _float(r"=\s*([\d.]+)%", text),
        "threshold": _float(r"阈值\s*([\d.]+)%", text),
    }


_ROW = re.compile(r"^(literal|paraphrase|ambiguous)\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s*$")
_TOTAL = re.compile(r"^合计\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s*$")
_MISS = re.compile(r"^\s{2,}未命中:\s*(.+)$")
_NEG = re.compile(r"^\s+(q\d+):\s*召回\s+(\d+)\s*块")


def parse_retrieval(text: str) -> dict[str, Any]:
    """检索：分档命中率、合计、负控召回情况。

    负控那一行很重要：非空说明检索层根本没有"拒答"能力，
    拒答只能靠后面的判定层——这正是 REQ-09 存在的理由。
    """
    by_kind: dict[str, dict[str, Any]] = {}
    misses: dict[str, list[str]] = {}
    negative: dict[str, int] = {}
    total: dict[str, Any] | None = None
    last_kind: str | None = None

    for line in text.splitlines():
        row = _ROW.match(line)
        if row:
            last_kind = row.group(1)
            by_kind[last_kind] = {
                "n": int(row.group(2)),
                "hit_rate": float(row.group(3)),
                "mrr": float(row.group(4)),
            }
            continue
        miss = _MISS.match(line)
        if miss and last_kind:
            misses[last_kind] = [q.strip() for q in miss.group(1).split(",")]
            continue
        tot = _TOTAL.match(line)
        if tot:
            total = {
                "n": int(tot.group(1)),
                "hit_rate": float(tot.group(2)),
                "mrr": float(tot.group(3)),
            }
            continue
        neg = _NEG.match(line)
        if neg:
            negative[neg.group(1)] = int(neg.group(2))

    return {
        "top_k": 5,
        "tokenizer": "bigram" if "bigram" in text else None,
        "by_kind": by_kind,
        "overall": total,
        "misses": misses,
        "negative": {
            "count": len(negative),
            "nonempty": sum(1 for n in negative.values() if n > 0),
            "detail": negative,
        },
    }


def _db_fingerprint() -> dict[str, Any]:
    """库指纹：换了库，数字不可比，必须能看出来。"""
    digest = hashlib.sha256(DB.read_bytes()).hexdigest()[:12]
    return {"file": DB.name, "sha256_12": digest, "bytes": DB.stat().st_size}


def cmd_run(write: bool) -> int:
    if not DB.exists():
        print(f"缺少库副本：{DB}\n从 C# 仓库复制一份到 evals/fixtures/ 再来。", file=sys.stderr)
        return 2

    started = time.time()
    stages: dict[str, Any] = {}
    raw: dict[str, str] = {}

    # T-002 起全部对准 llama 库（LlamaIndex 切块，532 块）：
    # v2 runner 与 self 原版唯一差异是题库指向 questions-v2.json。
    # self/ 原版与 fixture 库原样保留（C# 基线 0.812/0.588 可随时对账）。
    llama_db = EVALS / "fixtures" / "agentworkbench-llama.db"
    plan = (
        ("index", "索引一致性", ("check_index_consistency.py", "--db", str(llama_db))),
        ("chunks", "切块质量", ("sample_chunks.py", "--db", str(llama_db), "--sample", "0")),
        (
            "retrieval",
            "检索命中",
            (str(EVALS / "run_rag_eval_v2.py"), "--db", str(llama_db), "--top-k", "5"),
        ),
    )

    for key, label, argv in plan:
        print(f"── {label} …", flush=True)
        _, text = _run(*argv)
        raw[key] = text
        parser = {"index": parse_index, "chunks": parse_chunks, "retrieval": parse_retrieval}[key]
        stages[key] = parser(text)
        print("   ok")

    # 三类「不太好」分开对待，混成一条状态就没人看得懂：
    #   crashed  —— 数字都没解析出来，入口坏了，必须红
    #   quality  —— 数字解析出来了，但没达到目标，是产品现状，不该让入口红
    crashed: list[str] = []
    if stages["index"].get("documents") is None:
        crashed.append("索引一致性")
    if stages["chunks"].get("total") is None:
        crashed.append("切块质量")
    if stages["retrieval"].get("overall") is None:
        crashed.append("检索命中")

    quality: list[str] = []
    if not stages["index"].get("consistent"):
        quality.append("索引存在不一致")
    ratio, threshold = stages["chunks"].get("ratio"), stages["chunks"].get("threshold")
    if ratio is not None and threshold is not None and ratio > threshold:
        quality.append(f"接缝率 {ratio}% 高于目标 {threshold}%")

    REPORT_DIR.mkdir(exist_ok=True)
    for key, text in raw.items():
        (REPORT_DIR / f"{key}.txt").write_text(text, encoding="utf-8")

    # 入口坏了就到此为止：下面要解引用 overall / negative，先返回才能给出
    # 「评测入口故障」这句人话，而不是一个 TypeError 栈。
    if crashed:
        print(f"评测入口故障：{crashed}", file=sys.stderr)
        return 1

    result: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_seconds": round(time.time() - started, 1),
        "fixture": _db_fingerprint(),
        **stages,
        "crashed": crashed,
        "quality_notes": quality,
    }

    print()
    overall = stages["retrieval"]["overall"]
    print(f"合计命中率 {overall['hit_rate']:.3f}  MRR {overall['mrr']:.3f}  （{overall['n']} 题）")
    print(
        f"负控 {stages['retrieval']['negative']['count']} 条，"
        f"其中 {stages['retrieval']['negative']['nonempty']} 条召回了内容（检索层无拒答能力）"
    )
    for note in quality:
        print(f"注意：{note}")

    if write:
        BASELINE.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"基线已写入 {BASELINE.relative_to(EVALS.parent)}")
    else:
        print("（--no-write：未覆盖基线）")
    return 0


def cmd_show() -> int:
    if not BASELINE.exists():
        print("还没有基线，先跑 python -m evals run", file=sys.stderr)
        return 2
    data: dict[str, Any] = json.loads(BASELINE.read_text(encoding="utf-8"))
    retrieval = data.get("retrieval", {})
    print(f"基线时间：{data.get('generated_at')}")
    print(f"库指纹：{data.get('fixture', {}).get('sha256_12')}")
    for kind, row in retrieval.get("by_kind", {}).items():
        print(f"  {kind:<12}{row['n']:>4}  HitRate {row['hit_rate']:.3f}  MRR {row['mrr']:.3f}")
    overall = retrieval.get("overall") or {}
    if overall:
        print(
            f"  {'合计':<12}{overall['n']:>4}  HitRate {overall['hit_rate']:.3f}"
            f"  MRR {overall['mrr']:.3f}"
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m evals", description="离线评测")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="跑评测并写基线")
    run_p.add_argument("--no-write", action="store_true", help="只跑不写基线")
    sub.add_parser("show", help="打印当前基线")
    args = ap.parse_args()

    if args.cmd == "run":
        return cmd_run(write=not args.no_write)
    if args.cmd == "show":
        return cmd_show()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
