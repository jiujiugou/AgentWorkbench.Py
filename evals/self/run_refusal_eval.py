#!/usr/bin/env python
"""F3 拒答评测：量距离与间隔，按负控三档分开统计。

用法：
    # 只看分布
    python tests/benchmark/rag/run_refusal_eval.py

    # 扫阈值：给定 margin 网格，看每档被判成什么
    python tests/benchmark/rag/run_refusal_eval.py --scan

    # 试一组具体阈值
    python tests/benchmark/rag/run_refusal_eval.py --margin 0.08 --distance 0.40

跑之前必须做的一件事：把候选过滤放宽到 1.0 再起服务。

    Knowledge__MaxVectorDistance=1.0 bash scripts/dev.sh

默认 0.6 会在返回之前就把远处的候选滤掉，量到的是被剪过尾巴的分布——
看到的永远是"所有题都 ≤0.6"，据此定出来的阈值只会在 0.6 以内打转，
而下一次改配置，这个阈值立刻失效。脚本启动时会自己检查这一项并拒绝跑。

为什么单独一个脚本：
    拒答阈值是概率判据，进 dotnet test 只会得到一个随抽样摇摆的红灯。
    这里输出的是可复算的数字，进 git，跟代码版本对齐。

为什么按三档分开：
    八条负控不是一类人。完全库外该拒；同主题缺答案拒不得（拒了误伤真问题），
    只能靠第二层的"无资料支撑"暴露；超出检索能力的答不上来是检索的结构上限，
    把它算进拒答的分母里，误拒率会好看，真缺陷会被掩盖。
    混成一档，所有数字都说不清是谁的。

判定规则必须与 SufficiencyGate.Judge 完全一致，否则脚本量的和线上跑的不是一件事：
    一条候选都没有        -> insufficient
    没有带距离的候选      -> undetermined
    只有一条带距离的候选  -> weak
    间隔小于拒答阈值      -> insufficient
    两个阈值都在场且距离够近 -> answered
    两个阈值没同时在场    -> undetermined
    其余                  -> weak
"""
import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
QUESTIONS = Path(__file__).with_name("questions.json")
APPSETTINGS = ROOT / "src/AgentWorkbench.Webapi/appsettings.json"

# 只数前五条带距离的命中——与 SufficiencyGate.MarginWindow 一致。
MARGIN_WINDOW = 5

# 本机 HTTP_PROXY 会劫持 localhost 请求（代理连不上就报 ConnectionRefused，看起来像服务没起）。
# 显式绕开代理，否则这个脚本在开发机上永远打不通自己的服务。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def effective_vector_filter() -> tuple[float | None, str]:
    """读出当前生效的候选过滤阈值：环境变量优先于配置文件。"""
    raw = os.environ.get("Knowledge__MaxVectorDistance")
    if raw:
        try:
            return float(raw), "环境变量 Knowledge__MaxVectorDistance"
        except ValueError:
            return None, f"环境变量 Knowledge__MaxVectorDistance={raw!r} 不是数字"

    try:
        data = json.loads(APPSETTINGS.read_text(encoding="utf-8"))
        value = data.get("Knowledge", {}).get("MaxVectorDistance")
        return (float(value), "appsettings.json") if value is not None else (None, "appsettings.json 未配置")
    except (OSError, ValueError) as exc:
        return None, f"读不到 appsettings.json：{exc}"


def guard_vector_filter(force: bool) -> None:
    """拦住"用被剪过尾巴的分布定阈值"这件事。"""
    value, source = effective_vector_filter()
    if value is None:
        print(f"!! 无法确认候选过滤阈值（{source}），继续但结果不可信", file=sys.stderr)
        return
    if value >= 0.95:
        return
    message = (
        f"候选过滤阈值是 {value}（来自 {source}）。\n"
        "   它会先丢掉远处的候选再返回，量到的是剪过尾巴的分布。\n"
        "   用这个分布定出来的阈值，只在 0.6 以内成立。\n"
        "\n"
        "   先这样起服务再来：\n"
        "       Knowledge__MaxVectorDistance=1.0 bash scripts/dev.sh\n"
        "\n"
        "   确实要用当前配置量，加 --force。"
    )
    if force:
        print(f"!! 继续使用 {value}（--force）：{message}\n", file=sys.stderr)
        return
    print(message, file=sys.stderr)
    sys.exit(2)


def retrieve(base_url: str, query: str, top_k: int, timeout: float):
    payload = json.dumps({"query": query, "topK": top_k}).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/knowledge/retrieve",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with OPENER.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def measure(hits) -> tuple[float | None, float | None]:
    """复刻 SufficiencyGate：第一名取最小距离，间隔是前五里其余几条的平均减第一名。"""
    distances = []
    for hit in hits:
        score = hit.get("vectorScore")
        if score is None:
            continue
        score = float(score)
        if math.isfinite(score):
            distances.append(score)
    if not distances:
        return None, None

    distances.sort()
    window = distances[:MARGIN_WINDOW]
    top = window[0]
    if len(window) < 2:
        return top, None
    return top, sum(window[1:]) / len(window[1:]) - top


def judge(hit_count: int, top, margin, refuse, sufficient) -> str:
    if hit_count == 0:
        return "insufficient"
    if top is None:
        return "undetermined"
    if margin is None:
        return "weak"
    if refuse is None or sufficient is None:
        return "undetermined"
    if margin < refuse:
        return "insufficient"
    if top < sufficient:
        return "answered"
    return "weak"


def bucket(question) -> str:
    if question.get("kind") != "negative":
        return "正例"
    return {
        "off_topic": "完全库外",
        "same_topic_missing": "同主题缺答案",
        "beyond_retrieval": "超出检索能力",
    }.get(question.get("negative_kind") or "", "未分档")


def quantile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low, high = int(math.floor(pos)), int(math.ceil(pos))
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def fmt(value) -> str:
    return "—" if value is None else f"{value:.3f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="F3 拒答评测：间隔分布与三档统计")
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--margin", type=float, default=None, help="拒答阈值；不给则只报分布")
    parser.add_argument("--distance", type=float, default=None, help="充分阈值；与 --margin 一起才判定")
    parser.add_argument("--scan", action="store_true", help="扫 margin 网格，看每档被判成什么")
    parser.add_argument("--force", action="store_true", help="候选过滤未放宽也继续（结果不可信）")
    parser.add_argument("--json", default=None, help="逐题明细写到这个路径")
    args = parser.parse_args()

    guard_vector_filter(args.force)

    try:
        with OPENER.open(args.base_url.rstrip("/") + "/api/knowledge/health", timeout=args.timeout) as response:
            health = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as exc:
        print(f"!! 连不上 {args.base_url}：{exc}\n   服务没起，或端口不是 8080。", file=sys.stderr)
        return 2

    spec = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    questions = spec["questions"]
    indexed = health.get("documentCount") or health.get("documents") or "?"
    print(f"服务 {args.base_url} 正常（文档数 {indexed}），题目 {len(questions)} 条\n")

    rows = []
    for question in questions:
        try:
            hits = retrieve(args.base_url, question["question"], args.top_k, args.timeout)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"!! {question['id']} 检索失败：{exc}", file=sys.stderr)
            continue

        top, margin = measure(hits)
        gold = question.get("gold") or []
        gold_pairs = {(g["file"], g["seq"]) for g in gold}
        rank = next(
            (
                index + 1
                for index, hit in enumerate(hits)
                if (hit.get("fileName"), hit.get("seq")) in gold_pairs
            ),
            0,
        )
        rows.append(
            {
                "id": question["id"],
                "bucket": bucket(question),
                "kind": question.get("kind"),
                "negativeKind": question.get("negative_kind"),
                "question": question["question"],
                "hitCount": len(hits),
                "top": top,
                "margin": margin,
                "rank": rank,
                "hasGold": bool(gold_pairs),
            }
        )

    if not rows:
        print("没有一条跑成功。", file=sys.stderr)
        return 1

    # ---- 间隔分布 ----------------------------------------------------------
    print("=== 间隔分布（越大 = 第一名的领先越明显；None = 带距离的候选不足两条）===")
    print(f"{'档':<16}{'题数':>4}{'最小':>9}{'中位':>9}{'最大':>9}{'算不出':>8}")
    order = ["正例", "完全库外", "同主题缺答案", "超出检索能力"]
    for name in order:
        values = [row["margin"] for row in rows if row["bucket"] == name and row["margin"] is not None]
        total = sum(1 for row in rows if row["bucket"] == name)
        missing = total - len(values)
        print(
            f"{name:<16}{total:>4}{fmt(quantile(values, 0)):>9}{fmt(quantile(values, 0.5)):>9}"
            f"{fmt(quantile(values, 1)):>9}{missing:>8}"
        )

    unknown = [row for row in rows if row["bucket"] not in order]
    if unknown:
        print(f"（另有 {len(unknown)} 条负控没有 negative_kind 标签）")

    # ---- 阈值扫描 ----------------------------------------------------------
    if args.scan:
        print("\n=== margin 扫描（充分距离固定 1.0，即不限制远近）===")
        print(f"{'margin':>7}{'正例误拒率':>12}{'库外拒答率':>12}{'同主题误拒':>12}{'超出检索被拒':>14}")
        positives = [row for row in rows if row["bucket"] == "正例"]
        off_topic = [row for row in rows if row["bucket"] == "完全库外"]
        same_topic = [row for row in rows if row["bucket"] == "同主题缺答案"]
        beyond = [row for row in rows if row["bucket"] == "超出检索能力"]

        def rate(group, margin):
            if not group:
                return None
            hit = sum(
                1
                for row in group
                if judge(row["hitCount"], row["top"], row["margin"], margin, 1.0) == "insufficient"
            )
            return hit / len(group)

        for step in range(0, 31):
            margin = step / 100
            print(
                f"{margin:>7.2f}{rate(positives, margin):>12.0%}{rate(off_topic, margin):>12.0%}"
                f"{rate(same_topic, margin):>12.0%}{rate(beyond, margin):>14.0%}"
            )
        print("\n想要的点：正例误拒率低、库外拒答率高。两条曲线交叉的地方就是阈值的上限。")
        print("同主题缺答案那一档不该被拒——它若也被拒，说明阈值已经低于真实问题的间隔。")

    # ---- 单组阈值 ----------------------------------------------------------
    if args.margin is not None:
        print(f"\n=== 判定结果（margin={args.margin}，distance={args.distance}）===")
        for name in order:
            group = [row for row in rows if row["bucket"] == name]
            if not group:
                continue
            verdicts = {}
            for row in group:
                verdict = judge(row["hitCount"], row["top"], row["margin"], args.margin, args.distance)
                verdicts[verdict] = verdicts.get(verdict, 0) + 1
            detail = "，".join(f"{key} {value}" for key, value in sorted(verdicts.items()))
            print(f"{name:<16}{detail}")

        positives = [row for row in rows if row["bucket"] == "正例"]
        rejected = [
            row
            for row in positives
            if judge(row["hitCount"], row["top"], row["margin"], args.margin, args.distance) == "insufficient"
        ]
        if positives:
            print(f"\n误拒率 {len(rejected)}/{len(positives)} = {len(rejected) / len(positives):.0%}")
            for row in rejected:
                print(f"  误拒 {row['id']} margin={fmt(row['margin'])} top={fmt(row['top'])} {row['question'][:40]}")

    # ---- 逐题明细 ----------------------------------------------------------
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n逐题明细写到 {args.json}")

    # 命中率的旁证：正例里 gold 排第一的比例。
    # 这个数不是结论，是先决条件——检索本身坏了的时候，调阈值毫无意义：
    # 阈值再准，也救不回一块根本没被召回的答案。
    positives = [row for row in rows if row["bucket"] == "正例" and row["hasGold"]]
    if positives:
        first = sum(1 for row in positives if row["rank"] == 1)
        ratio = first / len(positives)
        print(f"\n旁证：正例 gold 排第一 {first}/{len(positives)} = {ratio:.0%}")
        if ratio < 0.5:
            print("  !! 这个比例偏低。先查检索本身（索引是否齐、分词格式是否匹配），别急着调阈值。")

    print("\n注：间隔为 None 的题不进分布统计，它们会被判成依据较弱或未判定，不算拒答。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
