"""D8：真实模型的质量评测。两段，目的不同，不能混。

**第一段：检索标定。** 只跑 `retrieve()`，不动模型。逐题记下首名向量距离与
「第 2~5 名平均距离 − 首名」的间隔。这一段的产物是**阈值**：`refuse_margin`
与 `sufficient_distance` 现在是代码里的临时默认值（0.10 / 0.50），
在 bge-m3 的余弦距离尺度上从来没有被标定过。

**第二段：端到端问答。** 走真实 `POST /api/knowledge/ask`：检索 → 判定 →
拼上下文 → 真模型生成 → 引用核对 → 落库。逐题记下 status / verdict /
引用到了哪篇 / 编号有没有越界 / 有多少句没出处。

为什么不合成一个 pass：检索错了和生成错了是两类缺陷，合成一个总分之后
「准确率 71%」既不能说明检索该改，也不能说明提示词该改。

**不污染生产库**：先把 `--data` 整棵复制到 `--out/work`，评测在副本上跑。
答过的 28 个会话会写进副本，生产库里一条都不留。

用法（密钥只从环境变量读，不进命令行参数，避免进 shell 历史）：

    export AWB_SECRET_KEY=...
    export AWB_EMBEDDING__API_KEY=...        # bge-m3
    python scripts/eval_quality.py --password '迁移时设的口令'
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agentworkbench.api.app import create_app
from agentworkbench.api.deps import Deps, build_deps
from agentworkbench.config import Settings
from agentworkbench.rag.retriever import RetrievalHit, retrieve

DEFAULT_EMAIL = "wang@migrated.local"
DEFAULT_QUESTIONS = Path("docs/验收/d8-题库.json")


@dataclass
class Case:
    id: str
    category: str
    question: str
    expect: str
    gold: list[str]
    fact: str
    note: str
    # 第一段
    top_distance: float | None = None
    margin: float | None = None
    retrieval_files: list[str] = field(default_factory=list)
    # 第二段
    status: str = ""
    verdict: str = ""
    reason: str | None = None
    answer: str = ""
    cited_files: list[str] = field(default_factory=list)
    # 二级校验的覆盖率：**引用片段里到底有多少句子里的话**（词面重叠，不是语义）
    coverages: list[float | None] = field(default_factory=list)
    out_of_range: int | None = None
    uncited_ratio: float | None = None
    support_verdict: str = ""
    seconds: float = 0.0


def _load(path: Path) -> list[Case]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [Case(**item) for item in payload["questions"]]


def _gold_in(names: list[str], gold: list[str]) -> bool:
    return any(token in name for token in gold for name in names)


def _gold_all(names: list[str], gold: list[str]) -> bool:
    return bool(gold) and all(any(token in name for name in names) for token in gold)


def calibrate(conn: Any, deps: Deps, cases: list[Case], tenant_id: str) -> None:
    """第一段：只量检索。距离与间隔是阈值标定的唯一输入。"""
    for case in cases:
        hits: list[RetrievalHit] = retrieve(
            conn, case.question, embedder=deps.embedder, tenant_id=tenant_id, top_k=5
        )
        case.retrieval_files = [hit.file_name for hit in hits]
        # **必须按距离升序**，不能按 hits 的顺序：hits 是按 RRF 融合分排的，
        # 排第一的块可能来自全文路（没有距离）。判定层（`guard/sufficiency.judge`）
        # 自己会 `sorted(...)[:5]`，这里不照做就量出一组与线上不同的数——
        # 第一版评测正是这么错的（拿 A09 算出的间隔比 A13 还小，却只有 A09 被放行）。
        distances = sorted(hit.vector_distance for hit in hits if hit.vector_distance is not None)[
            :5
        ]
        if not distances:
            continue
        case.top_distance = distances[0]
        if len(distances) > 1:
            case.margin = statistics.fmean(distances[1:]) - distances[0]


def ask_all(client: TestClient, cases: list[Case]) -> None:
    """第二段：真模型。每题一个独立会话（不带 conversationId），互不影响。"""
    for case in cases:
        started = time.monotonic()
        response = client.post("/api/knowledge/ask", json={"question": case.question})
        case.seconds = time.monotonic() - started
        if response.status_code != 200:
            case.status = f"HTTP {response.status_code}"
            case.answer = response.text[:400]
            continue
        body = response.json()
        case.status = body["status"]
        case.verdict = body["verdict"]
        case.reason = body.get("reason")
        case.answer = body["answer"]
        report = body.get("report") or {}
        case.out_of_range = report.get("outOfRangeCount")
        case.uncited_ratio = report.get("uncitedRatio")
        case.support_verdict = report.get("supportVerdict") or ""
        rows = report.get("citations") or []
        case.cited_files = [row["fileName"] for row in rows]
        case.coverages = [row.get("coverage") for row in rows]


def sweep(cases: list[Case]) -> list[tuple[float, float, int, int]]:
    """在已测到的数据上网格搜索两个阈值。

    判定规则照抄 `guard/sufficiency.judge`（间隔不够 → 不足；首名不够近 → 弱；
    两者都不触发 → 回答）。每个格子记两件事：

    - **误拒**（应答题被判不足）：用户问到了、库里有，系统说"不知道"
    - **误答**（应拒答题被放行）：库外问题被当成有依据——这类更贵，
      因为它给的是**看起来有出处的编造**

    `J` 是 Youden 指数（灵敏度 + 特异度 − 1），只用来看哪些格子明显更差。
    最终取值不看 J 最大，看"误答为 0 的前提下误拒最少"——这个产品的立场是
    宁可说依据不足，但不能编；所以先约束误答，再优化误拒。
    """
    rows: list[tuple[float, float, int, int]] = []
    for margin in [0.0, 0.005, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10]:
        for distance in [0.35, 0.38, 0.40, 0.42, 0.45, 0.48, 0.50, 0.55]:
            false_refuse = 0
            false_answer = 0
            for case in cases:
                if case.top_distance is None:
                    continue
                too_far = case.top_distance >= distance
                no_winner = case.margin is not None and case.margin < margin
                refused = too_far or no_winner
                if case.expect == "answer" and refused:
                    false_refuse += 1
                if case.expect == "refuse" and not refused:
                    false_answer += 1
            rows.append((margin, distance, false_refuse, false_answer))
    return rows


def summarise(cases: list[Case]) -> dict[str, Any]:
    answerable = [case for case in cases if case.expect == "answer"]
    refusable = [case for case in cases if case.expect == "refuse"]

    gate_hits = [case for case in answerable if _gold_in(case.retrieval_files, case.gold)]
    citation_any = [case for case in answerable if _gold_in(case.cited_files, case.gold)]
    citation_all = [case for case in answerable if _gold_all(case.cited_files, case.gold)]
    answered = [case for case in answerable if case.status == "succeeded"]
    refused_right = [case for case in refusable if case.status == "refused"]
    over_range = [case for case in cases if (case.out_of_range or 0) > 0]

    verdicts: dict[str, int] = {}
    for case in cases:
        verdicts[case.verdict or "(无)"] = verdicts.get(case.verdict or "(无)", 0) + 1

    in_scope_top = [case.top_distance for case in answerable if case.top_distance is not None]
    out_scope_top = [
        case.top_distance
        for case in refusable
        if case.top_distance is not None and case.category == "C"
    ]
    near_miss_top = [
        case.top_distance
        for case in refusable
        if case.top_distance is not None and case.category == "D"
    ]
    margins_in = [case.margin for case in answerable if case.margin is not None]
    margins_out = [case.margin for case in refusable if case.margin is not None]

    all_coverages = [value for case in cases for value in case.coverages if value is not None]
    weak_support = sorted(
        (
            (round(min(values), 3), case.id)
            for case in cases
            if (values := [v for v in case.coverages if v is not None])
        ),
    )
    return {
        "应答题数": len(answerable),
        "覆盖率_全体": _stats(all_coverages),
        "覆盖率最低的 5 题": weak_support[:5],
        "应拒答数": len(refusable),
        "检索命中@5": len(gate_hits),
        "引用命中（任一 gold）": len(citation_any),
        "引用命中（全部 gold）": len(citation_all),
        "给出回答": len(answered),
        "正确拒答": len(refused_right),
        "引用编号越界题数": len(over_range),
        "verdict 分布": verdicts,
        "平均耗时秒": round(statistics.fmean([case.seconds for case in cases]), 2),
        "距离_可答首名": _stats(in_scope_top),
        "距离_库外首名": _stats(out_scope_top),
        "距离_边界首名": _stats(near_miss_top),
        "间隔_可答": _stats(margins_in),
        "间隔_应拒": _stats(margins_out),
    }


def _stats(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "min": round(ordered[0], 3),
        "median": round(statistics.median(ordered), 3),
        "max": round(ordered[-1], 3),
    }


def _sweep_markdown(rows: list[tuple[float, float, int, int]]) -> list[str]:
    """把扫描压成一张能读的表：每个格子写「误拒/误答」。"""
    margins = sorted({row[0] for row in rows})
    distances = sorted({row[1] for row in rows})
    grid = {(row[0], row[1]): (row[2], row[3]) for row in rows}

    lines = [
        "## 阈值扫描（格子 = 误拒 / 误答）",
        "",
        "| refuse_margin ↓ / sufficient_distance → | "
        + " | ".join(f"{value:.2f}" for value in distances)
        + " |",
        "|---" * (len(distances) + 1) + "|",
    ]
    for margin in margins:
        cells = []
        for distance in distances:
            refuse, answer = grid.get((margin, distance), ("-", "-"))
            mark = "**" if (refuse == 0 and answer == 0) else ""
            cells.append(f"{mark}{refuse}/{answer}{mark}")
        lines.append(f"| {margin:.3f} | " + " | ".join(cells) + " |")

    perfect = [row for row in rows if row[2] == 0 and row[3] == 0]
    zero_miss = sorted((row for row in rows if row[3] == 0), key=lambda row: row[2])
    lines += ["", f"- 误拒与误答同时为 0 的格子：{len(perfect)} 个"]
    if zero_miss:
        best = zero_miss[0]
        lines.append(
            f"- 误答为 0 的格子里误拒最少的：margin={best[0]:.3f}、distance={best[1]:.2f}"
            f"（误拒 {best[2]} 题）"
        )
    return lines


def _markdown(
    cases: list[Case], summary: dict[str, Any], rows: list[tuple[float, float, int, int]]
) -> str:
    lines = ["# D8 质量评测结果", "", "## 汇总", "", "| 指标 | 值 |", "|---|---|"]
    for key, value in summary.items():
        lines.append(f"| {key} | {value} |")

    lines += [
        "",
        *_sweep_markdown(rows),
        "",
        "## 逐题",
        "",
        "| 题 | 类别 | 期望 | 状态 | 判定 | 检索首名 | 引用 | 越界 | 秒 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for case in cases:
        retrieval = case.retrieval_files[0][:26] if case.retrieval_files else "—"
        cited = "、".join(sorted(set(name[:20] for name in case.cited_files))) or "—"
        lines.append(
            f"| {case.id} | {case.category} | {case.expect} | {case.status} | {case.verdict} "
            f"| {retrieval} | {cited} | {case.out_of_range} | {case.seconds:.1f} |"
        )

    lines += ["", "## 逐题原文", ""]
    for case in cases:
        lines += [
            f"### {case.id}　{case.question}",
            "",
            f"- 期望：{case.expect}　事实锚点：{case.fact}",
            f"- 检索首名：{case.retrieval_files[0] if case.retrieval_files else '(空)'}"
            f"　距离：{case.top_distance}　间隔：{case.margin}",
            f"- 判定：{case.verdict}　状态：{case.status}　原因：{case.reason}",
            f"- 引用：{'、'.join(sorted(set(case.cited_files))) or '(无)'}",
            "",
            "```",
            (case.answer or "").strip()[:1200],
            "```",
            "",
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="D8 真实模型质量评测")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument(
        "--data", type=Path, default=Path("data"), help="迁移后的数据目录（只读复制）"
    )
    parser.add_argument("--out", type=Path, default=Path("reports/acceptance/2026-11-05/d8"))
    parser.add_argument("--password", default=os.environ.get("AWB_EVAL_PASSWORD", ""))
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--only", default="", help="只跑某几类，如 A,B")
    parser.add_argument("--skip-ask", action="store_true", help="只跑检索标定")
    parser.add_argument("--margin", type=float, default=None, help="覆盖 settings.refuse_margin")
    parser.add_argument(
        "--distance", type=float, default=None, help="覆盖 settings.sufficient_distance"
    )
    args = parser.parse_args(argv)

    secret = os.environ.get("AWB_SECRET_KEY")
    if not secret:
        print("缺少 AWB_SECRET_KEY（库里凭据是密文，没有它解不开）", file=sys.stderr)
        return 2
    if not args.skip_ask and not args.password:
        print("缺少 --password 或 AWB_EVAL_PASSWORD（登录评测账号用）", file=sys.stderr)
        return 2

    cases = _load(args.questions)
    if args.only:
        wanted = {item.strip().upper() for item in args.only.split(",")}
        cases = [case for case in cases if case.category in wanted]

    work = args.out / "work"
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(args.data, work)
    args.out.mkdir(parents=True, exist_ok=True)

    overrides: dict[str, Any] = {}
    if args.margin is not None:
        overrides["refuse_margin"] = args.margin
    if args.distance is not None:
        overrides["sufficient_distance"] = args.distance
    settings = Settings(data_dir=work, secret_key=secret, **overrides)
    if not settings.embedding.configured:  # 占位嵌入下评测没有意义
        print(
            "未配置 AWB_EMBEDDING__*：占位嵌入会让检索退化成随机邻居，评测结果无意义",
            file=sys.stderr,
        )
        return 2
    deps = build_deps(settings)
    app = create_app(settings=settings, deps=deps)

    conn = deps.open_conn()
    tenant_id = str(
        conn.execute("select tenant_id from users where email = ?", (args.email,)).fetchone()[0]
    )

    print(f"题库 {len(cases)} 题，模型 {settings.embedding.model}，数据副本 {work}")
    calibrate(conn, deps, cases, tenant_id)
    print("检索标定完成")

    if not args.skip_ask:
        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login", json={"email": args.email, "password": args.password}
            )
            if login.status_code != 200:
                print(f"登录失败：{login.status_code} {login.text[:200]}", file=sys.stderr)
                return 2
            ask_all(client, cases)
        print("端到端问答完成")

    summary = summarise(cases)
    rows = sweep(cases)
    (args.out / "d8-results.json").write_text(
        json.dumps(
            {"summary": summary, "cases": [case.__dict__ for case in cases]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (args.out / "d8-quality.md").write_text(_markdown(cases, summary, rows), encoding="utf-8")

    for key, value in summary.items():
        print(f"  {key}: {value}")
    for line in _sweep_markdown(rows):
        print(line)
    print(f"\n结果：{args.out / 'd8-quality.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
