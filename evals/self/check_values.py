#!/usr/bin/env python
"""F3 第七条子问题：数值/型号比对（读错——数字、型号说错）。

引用角标全对只证明「这句话出自那一块」，不证明抄对了。模型完全可以把
32GB 抄成 64GB、把 E2 抄成 E3、把 9600 抄成 6900，而角标依旧正确。
本脚本对答案文本里出现的每个数值/型号/单位/地址，去标准块原文里找，
找不到就列出来 —— 逐项比值，不是比「有没有数字」。

用法：
    python check_values.py --self-test
    python check_values.py --question q12 --answer-file out.txt
    python check_values.py --question q12 < out.txt
    python check_values.py --all --answers-dir out/      # 每题一个 <qid>.txt

输出：命中数 / 未命中数 / 未命中清单（值 + 答案里的上下文）。
退出码：有未命中 = 1，全命中 = 0。

--- 自测（--self-test）三段，缺一不可 ---------------------------------
1) 归一化断言：32GB / 32 GB / 32g 同键；3.0 = 3；modbus = Modbus；
   全角半角同键；伏=V、毫秒=ms、兆=M。
2) 标准块自比：把 questions.json 里每题的 gold 块原文原样当「模型输出」
   跑一遍，必须全部命中、零未命中。有未命中 = 抽取器或归一化写错了。
   （自比是全绿的最低门槛，不构成有效性证明 —— 真正的反向检查在第 3 段。）
3) 5 个反例：把标准块里的一个值改错一位，必须全部被报为未命中。
   ---------------------------------------------------------------
   | # | 题号 | 标准块原文      | 反例（改错的值）      | 期望 |
   |---|------|----------------|----------------------|------|
   | 1 | q24  | 192.168.1.88   | 192.168.1.189 (改位) | 未命中 |
   | 2 | q24  | H5U            | H6U    (型号改位)    | 未命中 |
   | 3 | q26  | SD 卡 >256M    | 512M   (容量翻倍)    | 未命中 |
   | 4 | q28  | 50ms～10000ms  | 20000ms(上限翻倍)    | 未命中 |
   | 5 | q30  | 9600bps        | 6900bps(波特率改位)  | 未命中 |
   ---------------------------------------------------------------
   这 5 条就是「如果脚本永远绿，抄错还会不会发生」的答案：会。因为
   每一条都是真值被替换后脚本变红。反过来，脚本全绿只说明「答案里没有
   与标准块数值冲突的 token」，不说明语义正确（见文末局限）。

--- 局限（必须知道，别把它当全自动判卷）-----------------------------
* 块级包含，不是句级。同一块里别处出现过的值会被判命中（假绿）。
* 中文数字不抽取：标准块写「上午九时」，模型写「上午9时」会被漏掉
  （假红方向，不会放过抄错，只会多报）。
* 只做无歧义的中文单位映射（伏/毫秒/兆/欧/瓦/安/秒/赫兹），不猜。
* 否定与条件不建模：「不支持 64GB」里的 64GB 同样会被要求命中。
* 这脚本现在是「等待输入」状态：没有模型答案文本时它无话可说。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = ROOT / "src/AgentWorkbench.Webapi/data/agentworkbench.db"
QUESTIONS = Path(__file__).with_name("questions.json")

CTX = 10  # 未命中时上下文各取多少字

# --- 单位归一化 --------------------------------------------------------
# 只收明确无歧义的。中文一律映射到拉丁写法，拉丁一律小写。
UNIT_CANON = {
    # 存储 / 速率
    "tb": "tb", "gb": "gb", "mb": "mb", "kb": "kb", "g": "gb",
    "gbps": "gbps", "mbps": "mbps", "kbps": "kbps", "bps": "bps",
    # 电
    "kva": "kva", "va": "va", "kw": "kw", "w": "w",
    "ma": "ma", "a": "a", "mv": "mv", "v": "v",
    "kω": "kω", "mω": "mω", "ω": "ω",
    # 时间
    "ms": "ms", "us": "us", "μs": "us", "ns": "ns", "s": "s",
    "min": "min", "h": "h",
    # 频率
    "ghz": "ghz", "mhz": "mhz", "khz": "khz", "hz": "hz",
    # 长度 / 压力
    "mm": "mm", "cm": "cm", "km": "km",
    "mpa": "mpa", "kpa": "kpa", "pa": "pa", "bar": "bar",
    # 温度（NFKC 已把 ℃ 折成 °C）
    "°c": "°c",
    # 中文（无歧义的那几个）
    "伏": "v", "毫伏": "mv", "千伏": "kv",
    "毫秒": "ms", "微秒": "us", "秒": "s", "纳秒": "ns",
    "毫安": "ma", "安": "a", "千瓦": "kw", "瓦": "w",
    "欧": "ω", "千欧": "kω", "兆欧": "mω",
    "赫兹": "hz", "千赫": "khz", "兆赫": "mhz",
    "兆": "m",
}

_UNIT_ALT = "|".join(re.escape(u) for u in sorted(UNIT_CANON, key=len, reverse=True))
_NUMCORE = r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?"
_ENDB = r"(?![A-Za-z0-9_])"          # 单位后面的边界：只认 ASCII，中文允许紧跟
_SIGN = r"(?:(?<![A-Za-z0-9_.])[-−])?"

# 型号里允许出现的「协议名」，无数字也算型号（要求里点名了 Modbus RTU）
PROTOCOLS = ("modbus rtu", "modbus ascii", "modbus tcp", "opc ua",
             "profinet", "profibus", "ethercat", "canopen", "fins tcp", "fins udp")

_PAT = re.compile(
    r"(?P<ip>\d{1,3}(?:\.\d{1,3}){3}(?![\d.]))"
    r"|(?P<hex>0[xX][0-9A-Fa-f]+)"
    r"|(?P<range>" + _SIGN + r"(?:" + _NUMCORE + r")\s*(?:[~～–—]|至|到)\s*"
    r"(?:" + _SIGN + r"(?:" + _NUMCORE + r"))\s*(?:" + _UNIT_ALT + r")?" + _ENDB + r")"
    r"|(?P<model>[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*)"
    r"|(?P<num>" + _SIGN + r"(?:" + _NUMCORE + r")"
    r"(?:\s*(?:" + _UNIT_ALT + r")(?![A-Za-z0-9_])|(?![0-9.])))",
    re.IGNORECASE,
)

_PROTO_PATS = [
    (p, re.compile(r"\s*".join(map(re.escape, p.split())) + _ENDB, re.IGNORECASE))
    for p in PROTOCOLS
]
_RANGE_PARTS = re.compile(
    r"^\s*(?P<a>[-−]?(?:" + _NUMCORE + r"))\s*(?:[~～–—]|至|到)\s*"
    r"(?P<b>[-−]?(?:" + _NUMCORE + r"))\s*(?P<u>" + _UNIT_ALT + r")?\s*$",
    re.IGNORECASE,
)


class Token:
    """kind: num | sym。num 比的是 (值, 单位)，sym 比的是归一化字符串。"""

    __slots__ = ("kind", "key", "unit", "raw", "start", "end", "tag")

    def __init__(self, kind, key, unit, raw, start, end, tag=""):
        self.kind, self.key, self.unit = kind, key, unit
        self.raw, self.start, self.end, self.tag = raw, start, end, tag

    @property
    def ident(self):
        return (self.kind, self.key, self.unit)

    def __repr__(self):
        u = f" {self.unit}" if self.unit else ""
        return f"<{self.kind} {self.key}{u} [{self.tag}]>"


def normalize_text(s: str) -> str:
    """全角→半角、℃→°C、～→~、微符号 µ→μ。两侧都走这条路，才谈得上可比。"""
    return unicodedata.normalize("NFKC", s).replace("\u00b5", "\u03bc")


def canon_num(s: str) -> str:
    """3.0 → 3，0.50 → 0.5，128,000 → 128000。"""
    s = s.replace(",", "").replace("−", "-")
    neg = s.startswith("-")
    d = Decimal(s.lstrip("-"))
    key = str(int(d)) if d == d.to_integral_value() else format(d.normalize(), "f")
    return "-" + key if neg else key


def canon_unit(u: str) -> str:
    if not u:
        return ""
    return UNIT_CANON.get(u.strip().lower(), u.strip().lower())


def canon_sym(s: str) -> str:
    """型号/协议：大小写、空格、连字符都不算区别（S7-1200 = S7 1200 = s71200）。"""
    return re.sub(r"[\s\-_]", "", s.lower())


def extract(text: str):
    """按优先级从左到右扫一遍：IP > 十六进制 > 范围 > 型号 > 数值+单位。"""
    text = normalize_text(text)
    out = []

    for m in _PAT.finditer(text):
        raw = m.group()
        kind = m.lastgroup
        if kind == "ip":
            out.append(Token("sym", canon_sym(raw), "", raw, m.start(), m.end(), "ip"))
        elif kind == "hex":
            out.append(Token("sym", raw.lower(), "", raw, m.start(), m.end(), "hex"))
        elif kind == "range":
            parts = _RANGE_PARTS.match(raw)
            if not parts:
                continue
            unit = canon_unit(parts.group("u") or "")
            for side in ("a", "b"):
                num = canon_num(parts.group(side))
                out.append(Token("num", num, unit, raw, m.start(), m.end(), "range"))
        elif kind == "model":
            if not re.search(r"\d", raw):
                continue  # 纯字母不是型号（INT / REAL / STOP 之类）
            out.append(Token("sym", canon_sym(raw), "", raw, m.start(), m.end(), "model"))
        else:
            nm = re.search(_SIGN + r"(?:" + _NUMCORE + r")", raw)
            if not nm:
                continue
            num = canon_num(nm.group())
            rest = raw[nm.end():].strip().lower()
            unit = UNIT_CANON.get(rest, "") if rest else ""
            tag = "unit" if unit else ("decimal" if "." in num else "int")
            out.append(Token("num", num, unit, raw.strip(), m.start(), m.end(), tag))

    # 协议名：单独补扫，避免把 "Modbus RTU" 拆成两个无数字词而漏掉
    for _, pat in _PROTO_PATS:
        for m in pat.finditer(text):
            out.append(Token("sym", canon_sym(m.group()), "", m.group(),
                             m.start(), m.end(), "protocol"))

    out.sort(key=lambda t: (t.start, t.end))
    return out


def unique(tokens):
    seen, out = set(), []
    for t in tokens:
        if t.ident in seen:
            continue
        seen.add(t.ident)
        out.append(t)
    return out


def matches(t: Token, gold_idents) -> bool:
    """数值要求「值相同 + 单位相同或答案没写单位」；型号要求归一化相同。

    为什么不放宽到「值相同就当命中」：那样 32GB→64GB 依然会被抓，但
    32GB→32MB 会漏。单位是抄错的高发位，必须比值。
    """
    if t.kind == "num":
        return (t.kind, t.key, t.unit) in gold_idents or (t.kind, t.key, "") in gold_idents
    if ("sym", t.key, "") in gold_idents:
        return True
    # PDF 表格串行时会把相邻型号粘成一个长 token（Easy321S-3624EREasy321S-…），
    # 只对 >=4 字符的型号放行「包含」判定；IP/十六进制/协议名不放宽，
    # 否则 192.168.1.8 会被判进 192.168.1.88。
    if t.tag == "model" and len(t.key) >= 4:
        return any(k == "sym" and t.key in key for (k, key, _u) in gold_idents)
    return False


def context(text: str, t: Token) -> str:
    lo, hi = max(0, t.start - CTX), min(len(text), t.end + CTX)
    return (
        ("…" if lo else "") + text[lo:t.start] + "【" + text[t.start:t.end] + "】"
        + text[t.end:hi] + ("…" if hi < len(text) else "")
    )


def check(answer: str, gold_texts) -> tuple[int, list[tuple[Token, str]]]:
    # 必须先归一化再取上下文：offset 是归一化后文本里的位置，
    # 而 NFKC 会改长度（℃ → °C），拿原串切片会错位。
    answer = normalize_text(answer)
    gold_idents = {t.ident for text in gold_texts for t in extract(text)}
    hits, misses = 0, []
    for t in unique(extract(answer)):
        if matches(t, gold_idents):
            hits += 1
        else:
            misses.append((t, context(answer, t)))
    return hits, misses


# --- 数据 -----------------------------------------------------------------
def load_spec():
    return json.loads(QUESTIONS.read_text(encoding="utf-8"))


def resolve_gold(con, gold):
    """(file, seq) -> 块原文。file+seq 命中多行时全部收进来（重复文档）。"""
    texts = []
    for g in gold:
        rows = con.execute(
            """SELECT ch.content FROM documents d JOIN chunks ch ON ch.document_id = d.id
               WHERE d.file_name = ? AND ch.seq = ?""",
            (g["file"], g["seq"]),
        ).fetchall()
        if not rows:
            print(f"!! gold 解析为空: {g['file']} seq={g['seq']}", file=sys.stderr)
        texts.extend(r[0] for r in rows)
    # 去重但保序（q21 同一份文件有两条文档记录，内容相同）
    return list(dict.fromkeys(texts))


def open_db(path: str):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


# --- 自测 -----------------------------------------------------------------
# (原文, 期望键)。期望键 = (kind, key, unit)
NORMALIZATION_CASES = [
    ("32 GB", ("num", "32", "gb")),
    ("32GB", ("num", "32", "gb")),
    ("32g", ("num", "32", "gb")),
    ("1.5kW", ("num", "1.5", "kw")),
    ("9600bps", ("num", "9600", "bps")),
    ("24V", ("num", "24", "v")),
    ("24 伏", ("num", "24", "v")),
    ("50ms", ("num", "50", "ms")),
    ("50 毫秒", ("num", "50", "ms")),
    ("3.0", ("num", "3", "")),
    ("３２ＧＢ", ("num", "32", "gb")),
    ("120 欧", ("num", "120", "ω")),
    ("-10~55℃", ("num", "-10", "°c")),
    ("-10~55℃", ("num", "55", "°c")),
    ("128,000 元", ("num", "128000", "")),
    ("modbus rtu", ("sym", "modbusrtu", "")),
    ("Modbus RTU", ("sym", "modbusrtu", "")),
    ("S7-1200", ("sym", "s71200", "")),
    ("E2", ("sym", "e2", "")),
    ("D100", ("sym", "d100", "")),
    ("0x1F", ("sym", "0x1f", "")),
    ("192.168.1.88", ("sym", "192.168.1.88", "")),
]

# (题号, 标准块里的真值, 反例文本)  —— 每条只改一个值，都必须变红
COUNTEREXAMPLES = [
    ("q24", "192.168.1.88", "H5U 出厂默认 IP 为 192.168.1.189，长按 MFK 恢复。"),
    ("q24", "H5U", "H6U 出厂默认 IP 为 192.168.1.88，长按 MFK 恢复。"),
    ("q26", "256M", "SFTP 挂载的 SD 卡容量必须大于 512M，最多 10 个连接。"),
    ("q28", "10000ms", "超时时间设置范围为 50ms～20000ms，仅能设置为 50 的倍数。"),
    ("q30", "9600bps", "H5U 支持 6900bps、19200bps、38400bps、57600bps、115200bps。"),
]

TYPE_LABEL = [
    ("纯数字", lambda t: t.kind == "num" and t.tag == "int"),
    ("小数", lambda t: t.kind == "num" and t.tag == "decimal"),
    ("带单位", lambda t: t.kind == "num" and t.tag == "unit"),
    ("数值范围", lambda t: t.kind == "num" and t.tag == "range"),
    ("型号/寄存器", lambda t: t.kind == "sym" and t.tag == "model"),
    ("IP 地址", lambda t: t.kind == "sym" and t.tag == "ip"),
    ("十六进制", lambda t: t.kind == "sym" and t.tag == "hex"),
    ("协议名", lambda t: t.kind == "sym" and t.tag == "protocol"),
]


def self_test(spec, con) -> int:
    fail = 0

    print("=== 1) 归一化断言 ===")
    for text, want in NORMALIZATION_CASES:
        got = {t.ident for t in extract(text)}
        ok = want in got
        fail += 0 if ok else 1
        if not ok:
            print(f"  FAIL {text!r} 期望 {want} 实得 {sorted(got)}")
    print(f"  {len(NORMALIZATION_CASES)} 条断言，失败 {fail} 条")

    print("\n=== 2) 标准块自比（gold 原文当模型输出，必须零未命中）===")
    cases = 0
    samples = {name: [] for name, _ in TYPE_LABEL}
    for q in spec["questions"]:
        if not q["gold"]:
            continue
        gold = resolve_gold(con, q["gold"])
        if not gold:
            continue
        cases += 1
        text = "\n".join(gold)
        for t in extract(text):
            for name, pred in TYPE_LABEL:
                if pred(t) and t.raw not in samples[name]:
                    samples[name].append(t.raw)
        hits, miss = check(text, gold)
        if miss:
            fail += len(miss)
            print(f"  {q['id']} 命中 {hits} / 未命中 {len(miss)}")
            for t, ctx in miss[:8]:
                print(f"    {t.kind:<4}{t.key + (' ' + t.unit if t.unit else ''):<12} {ctx}")
    print(f"  {cases} 题标准块自比完成")

    print("\n=== 3) 反例（改错一个值，必须被抓到）===")
    caught = 0
    for qid, truth, wrong in COUNTEREXAMPLES:
        q = next(x for x in spec["questions"] if x["id"] == qid)
        gold = resolve_gold(con, q["gold"])
        hits, miss = check(wrong, gold)
        if miss:
            caught += 1
            print(f"  OK   {qid} 真值 {truth:<12} 反例 → 未命中 "
                  f"{', '.join(t.raw for t, _ in miss)}")
        else:
            fail += 1
            print(f"  FAIL {qid} 真值 {truth:<12} 反例全命中，抽取器漏抓")
    print(f"  {len(COUNTEREXAMPLES)} 条反例，抓到 {caught} 条")

    print("\n=== 抽取器能识别的类型（真实样例，取语料里最长的 3 个）===")
    for name, _ in TYPE_LABEL:
        got = sorted(samples[name], key=len, reverse=True)[:3]
        print(f"  {name:<12} {'、'.join(got) if got else '（语料里没出现）'}")

    print("\n=== 结论 ===")
    if fail:
        print(f"自测失败 {fail} 项：抽取或归一化有问题，先修这里，别急着跑真实答案。")
        return 1
    print("自测通过：归一化一致、标准块零未命中、5 条反例全部变红。")
    return 0


# --- 命令行 ---------------------------------------------------------------
def report(qid: str, answer: str, gold) -> int:
    hits, miss = check(answer, gold)
    print(f"题目 {qid} · 标准块 {len(gold)} 块 · 答案 {len(answer)} 字")
    print(f"命中 {hits} · 未命中 {len(miss)}")
    if miss:
        print("未命中清单：")
        for t, ctx in miss:
            val = t.key + (f" {t.unit}" if t.unit else "")
            print(f"  [{t.kind}/{t.tag}] {val:<14} {ctx}")
    return 1 if miss else 0


def run_all(spec, con, answers_dir: Path) -> int:
    print(f"=== 批量比对 · 答案目录 {answers_dir} ===")
    n_q = n_ok = n_hit = n_miss = 0
    missing_files = []
    for q in spec["questions"]:
        if not q["gold"]:
            continue  # 负控题没有标准块，比不了
        f = answers_dir / f"{q['id']}.txt"
        if not f.exists():
            missing_files.append(q["id"])
            continue
        gold = resolve_gold(con, q["gold"])
        if not gold:
            continue
        answer = f.read_text(encoding="utf-8")
        hits, miss = check(answer, gold)
        n_q += 1
        n_hit += hits
        n_miss += len(miss)
        n_ok += 0 if miss else 1
        flag = "OK" if not miss else "未命中"
        print(f"  {q['id']}  命中 {hits:>3} / 未命中 {len(miss):>3}  {flag}")
        for t, ctx in miss[:5]:
            print(f"        {t.key + (' ' + t.unit if t.unit else ''):<12} {ctx}")

    print("-" * 46)
    if n_q:
        print(f"题数 {n_q} · 全对 {n_ok}（{n_ok / n_q:.1%}）· "
              f"值命中 {n_hit} · 值未命中 {n_miss}")
    if missing_files:
        print(f"缺答案文件 {len(missing_files)} 题：{', '.join(missing_files)}")
    print("注：未命中 = 答案里的值在标准块里找不到（抄错或凭空加的）。")
    return 1 if n_miss else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="数值/型号逐值比对")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--question", help="题号，如 q12")
    ap.add_argument("--answer-file", help="答案文本文件；不给则读 stdin")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--answers-dir", help="批量模式：每题一个 <qid>.txt")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()

    spec = load_spec()
    con = open_db(args.db)

    if args.self_test:
        return self_test(spec, con)

    if args.all:
        if not args.answers_dir:
            ap.error("--all 需要 --answers-dir")
        return run_all(spec, con, Path(args.answers_dir))

    if not args.question:
        ap.error("要么 --self-test，要么 --question <qid>，要么 --all")

    q = next((x for x in spec["questions"] if x["id"] == args.question), None)
    if q is None:
        ap.error(f"没有题目 {args.question}")
    if not q["gold"]:
        print(f"{q['id']} 是负控题（kind=negative），没有标准块可比。")
        return 0

    if args.answer_file:
        answer = Path(args.answer_file).read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            ap.error("没给 --answer-file，stdin 也没有内容")
        answer = sys.stdin.read()

    return report(q["id"], answer, resolve_gold(con, q["gold"]))


if __name__ == "__main__":
    sys.exit(main())
