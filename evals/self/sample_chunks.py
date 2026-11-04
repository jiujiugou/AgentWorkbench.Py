#!/usr/bin/env python
"""F3 知识问答「可用」第二条子问题：切块质量抽样——检查块边界是否把一句话切成两半。

为什么要这个脚本
----------------
现有「分词 46 条测试」量的是 BigramTokenizer 的输出形态。切块把一句话切成两半时，
分词测试照样全绿：它测的是 tokenizer，不是块边界。所以它不回答这条子问题。
验证文档里已经承认过这一点，这里补上真正管这条的检查。

判定的是「边界完整性」，不是「每块多少字」
----------------------------------------
如果这个脚本永远绿，"句子被切成两半"还会不会发生？会 —— 所以它必须检查边界两侧
的语言形态，而不是数长度。四类判据（常量集中在文件头，方便以后调）：

  1. head_fragment  块首不像句首
       首行以标点/闭括号开头、以"的/了/和/与/及/或"这类只能承接上文的字开头、
       以小写拉丁字母开头（半截标识符，如 "wRingPos"、"odbus"）。
  2. tail_fragment  块尾不像句尾
       末字符是逗号/顿号/分号/冒号或"的/和/与/及/或/把/被/将/对/以/按/于/在"这类
       连词介词助词，或小写拉丁字母；或者末行未收尾，而在后一块的行序列里紧随其后
       的那一行仍然是同一段散文（既不是新编号，也不是新标题/表格行）。
  3. word_split     相邻块拼接成词
       前块末 token 与后块首 token 拼起来是一个词：数字接数字、标识符接标识符、
       中文两字词被劈开（WORD_BRIDGE_PAIRS），或两个词被拼成一句
       （PHRASE_BRIDGE_PAIRS，例如"报警"+"代码"）。
  4. structure_cut  标题/表格行被切断
       heading 字段本身是半截标题（结尾悬空、编号还在标题只剩一个字、或长句没写完），
       或者接缝两侧同属一个表格行（末行未收尾且两侧都是表格行形态）。

按块计，不按长度计：一块只要命中任一类就算"疑似切断"；四类会同时命中，
所以报告末尾给一行"按主类别去重"，避免分类数字加起来和总数对不上。

为什么只读 chunks、不查 document_pages
-------------------------------------
Chunker（src/AgentWorkbench.Documents/Parsing/Chunker.cs）按行累积、每 800 字 flush、
带 120 字尾部重叠。于是块边界一定落在**行边界**上，且后一块的首行部分能在前一块的
行序列里找到（重叠区）。这带来两个好处：

  * "接缝前一行/后一行"直接在相邻块里就能拿到，脚本只依赖 chunks 表；
  * 反向检查只改 chunks 内容、不动原文，注入的坏块照样能被判 —— 若判据依赖原文，
    这条反向检查就退化成"原文里有这段就放过"，测不出东西。

因为重叠，一条被切断的句子往往两边都能读到半句，检索命中后读起来仍然是
"从半句开始"或"话没说完"。这正是用户报的现象，也正是这里要数的东西。
反过来说：前块末行与后块首行**是同一行**时，整行两块都有，没切掉内容，
这种情况不算表格行被切断（判据里显式排除）。

用法
----
    python tests/benchmark/rag/sample_chunks.py                  # 全量，明细每类 5 条
    python tests/benchmark/rag/sample_chunks.py --sample 20      # 明细每类 20 条
    python tests/benchmark/rag/sample_chunks.py --sample 0       # 明细不限
    python tests/benchmark/rag/sample_chunks.py --db other.db    # 换库
    python tests/benchmark/rag/sample_chunks.py --max-ratio 0.2  # 放宽退出阈值
    python tests/benchmark/rag/sample_chunks.py --selftest       # 跑一遍证伪（见下）

退出码：疑似切断块占比 > --max-ratio（默认 0.05）返回 1，否则 0；自检失败返回 2。

反向检查（证伪过程）
--------------------
这一步是脚本有没有用的证明，`--selftest` 可复现：

  1. 用 sqlite3.Connection.backup() 把库复制到系统临时目录（不用 cp：WAL 下直接复制
     文件会拿到缺 -wal 内容、不一致的库）；
  2. 再复制一份，在副本上注入 3 条故意切断的接缝 —— 取同一文档里相邻的块 X、Y，
     把前半句写进 X 的末尾、后半句写进 Y 的开头：
       a) 词内切断："……查看报警" / "代码E-102的说明……"
          两侧单独看都像正常行，只有 word_split 的短语桥判据能看出来；
       b) 连词悬空："……并且" / "需要在断电 30 秒后重新上电……"（tail_fragment）；
       c) 标题截断 + 块首承接：X 的 heading 改成"3.2 参"，Y 以
          "的默认值见下表……"开头（structure_cut + head_fragment）。
  3. 对注入副本跑扫描：三处必须全部报出，**而且报出的类别要对**，否则自检失败
     （退出码 2）—— 只要求"报出来"不够，报错类别说明判据错位；
  4. 对干净副本跑扫描：打印真实占比，高于 --max-ratio 只警告不改退出码 ——
     干净库本来就脏，这是被测出来的事实，不是脚本坏了。

跑完删掉临时文件。注入只在临时副本上发生，源库只以 mode=ro 打开，不会被改。

这个脚本测不到什么
------------------
写清楚边界，免得把它当全绿通行证：

  * 词级切断只认 WORD_BRIDGE_PAIRS / PHRASE_BRIDGE_PAIRS 里的组合。这是"地板"不是
    "天花板"：没收录的词被劈开时 word_split 不会响，只能靠 tail/head 两条兜。
    要补词就往常量里加，不影响其它判据。
  * 边界正好落在两个完整短句之间（"……完毕。" / "下一步操作……"）算正常切分，
    不报 —— 那确实不是"一句话被切成两半"。
  * 表格文档里两行之间切开（而不是一行内部切开）不报：每行自成一条，检索还能用。
  * 判据是启发式，不做语法分析：没有分词器、没有模型、不联网。误报率靠常量收窄，
    当前库上报出来的每一处都能人工复核（明细里带原片段）。
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------- 判据常量

# 句末标点：出现在块尾算"这句话说完了"
SENTENCE_END_PUNCT = set("。！？.!?")
# 收尾符号：右引号/右括号/百分号，也算说完了
CLOSING_MARKS = set("”’\"'）)】》」』〉%")
# 分句标点：落在块尾说明后面还有话（冒号引出下文、分号引出下一项）
CLAUSE_PUNCT = set("，、；：,;:")

# 悬空虚词：块尾是这些字，话就没说完 —— 的/和/与/把/被/将/对/以/按/于/在/能/要 ……
DANGLING_CHARS = set("的了着和与及或而但并且则即因若虽把被将对从向以按为由于是为能可需应要在")

CLOSED_END = SENTENCE_END_PUNCT | CLOSING_MARKS
TAIL_BROKEN_CHARS = CLAUSE_PUNCT | DANGLING_CHARS

# 出现在块首就一定不是句首：标点、闭括号、破折号、省略号
HEAD_BROKEN_CHARS = set("，。、；：）)】》」』〉”’\"'…-—·%")

# 标题结尾悬空：冒号/分号是标题引出下文的正常写法，不算
HEADING_DANGLING_CHARS = TAIL_BROKEN_CHARS - set("，、；：,;:")

# 以这些字/词开头，只能是承接上文（保守取值：宁漏不误报，不放"在/从/对/因此"这类可起句的词）
CONTINUATION_LEADERS = (
    "的", "了", "着", "和", "与", "及", "或", "而", "但", "并", "并且", "则", "即",
    "所述", "上述", "以上", "以下",
)

# 中文两字词被切开时的"桥"：前块末字 + 后块首字。命中说明这个词被边界劈成两半。
# 只收领域内高频词，宁漏不误报 —— 误报会让人开始怀疑脚本，漏报只是少报几条。
WORD_BRIDGE_PAIRS = {
    ("报", "警"), ("代", "码"), ("配", "置"), ("参", "数"), ("设", "置"), ("故", "障"),
    ("通", "信"), ("数", "据"), ("读", "取"), ("写", "入"), ("地", "址"), ("校", "验"),
    ("波", "特"), ("注", "册"), ("恢", "复"), ("重", "启"), ("异", "常"), ("连", "接"),
    ("断", "开"), ("寄", "存"), ("保", "护"), ("订", "阅"), ("证", "书"), ("诊", "断"),
    ("指", "令"), ("变", "量"), ("模", "块"), ("端", "口"), ("协", "议"), ("利", "息"),
    ("速", "度"), ("位", "置"), ("类", "型"), ("名", "称"), ("信", "号"), ("参", "照"),
    ("周", "期"), ("量", "程"), ("限", "位"), ("从", "站"), ("主", "站"), ("脉", "冲"),
}

# 两个词被接缝拼在一起：前块末 2 字 + 后块首 2 字。命中说明它们本来是一句连续的话，
# 边界把"报警代码""履行辅助人"这类复合说法劈成了两半。同样宁漏不误报。
PHRASE_BRIDGE_PAIRS = {
    ("报警", "代码"), ("履行", "辅助"), ("参数", "说明"), ("通信", "配置"),
    ("故障", "代码"), ("数据", "类型"), ("地址", "映射"), ("建立", "连接"),
    ("断开", "连接"), ("原因", "分析"), ("处理", "措施"), ("上电", "顺序"),
}

# 结构单元的开头：出现在块首/接缝后一行，说明这是"新的一条"，不是半句续接
NEW_UNIT_PATTERNS = (
    re.compile(r"^\d+(\.\d+)*[\s、.．)）]"),          # 1. / 3.2.1
    re.compile(r"^[（(][一二三四五六七八九十百\d]+[)）]"),  # （一） （2）
    re.compile(r"^[一二三四五六七八九十]+[、.．]"),        # 一、
    re.compile(r"^第[一二三四五六七八九十百千零\d]+[章节条款附录]"),  # 第五条
    re.compile(r"^[•·▪◦※*\-◆■]\s*"),                # 项目符号
    re.compile(r"^[A-Za-z][.)）]\s*$"),              # a. / c. 这类列表项
    re.compile(r"^[A-Za-z][A-Za-z0-9_./\-]*\s*[:：]"),  # 表头 Name:
    re.compile(r"^(字段|名称|序号|编号|表头)\s*[:：]"),    # 表头行
    re.compile(r"^名称\s+\S"),                       # "名称 数据类型 描述" 表头
    re.compile(r"^#{1,6}\s"),                        # Markdown 标题
)

# 散文判据：够长、汉字占比够高，才谈得上"一句话被切成两半"
PROSE_MIN_CHARS = 24
PROSE_MIN_CJK_RATIO = 0.5

# 编号还在、标题只剩一个字：`## 3.2 参` 这种半截标题（两个字不收，`10.1 概述` 是正常标题）
HEADING_NUMBERED_STUB = re.compile(
    r"^(?:\d+(?:\.\d+)*|[一二三四五六七八九十]+[、.．]|第[一二三四五六七八九十百千零\d]+[章节条款])"
    r"\s+[\u4e00-\u9fff]$"
)

# 表格行判据：分隔符多，或非汉字符号密度高
TABLE_SEP_RE = re.compile(r"\|")
TABLE_CELL_GAP_RE = re.compile(r"\s{2,}")

CATEGORIES = (
    ("structure_cut", "标题/表格行被切断"),
    ("word_split", "相邻块拼接成词"),
    ("head_fragment", "块首不像句首"),
    ("tail_fragment", "块尾不像句尾"),
)

CONNECTION_TIMEOUT_SECONDS = 5.0


# ---------------------------------------------------------------- 小工具

def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk / len(text)


def is_new_unit(line: str) -> bool:
    line = line.strip()
    return any(p.match(line) for p in NEW_UNIT_PATTERNS)


def is_prose(line: str) -> bool:
    line = line.strip()
    return (
        len(line) >= PROSE_MIN_CHARS
        and cjk_ratio(line) >= PROSE_MIN_CJK_RATIO
        and not is_new_unit(line)
    )


def is_table_row(line: str) -> bool:
    line = line.strip()
    if not line or is_new_unit(line):
        return False  # 带编号/项目符号的行是新的一条，不是表格行
    if TABLE_SEP_RE.search(line) or TABLE_CELL_GAP_RE.search(line):
        return True
    # PDF 表格常被压成"标识符 类型 描述 R/W 地址"单空格分隔：首 token 像标识符且符号密度高
    symbols = sum(1 for ch in line if not ("\u4e00" <= ch <= "\u9fff"))
    if len(line) < 10 or symbols / len(line) < 0.45:
        return False
    first = line.split(" ", 1)[0]
    return bool(re.match(r"^[A-Za-z0-9_\[\.\-/]", first))


def tail_looks_open(line: str) -> bool:
    """末字符不是收尾标点 —— 这一行看起来没说完。"""
    line = line.strip()
    return bool(line) and line[-1] not in CLOSED_END


def tail_definitely_broken(line: str) -> bool:
    """末字符落在悬空集合里 —— 明确是半句话。"""
    line = line.strip()
    if not line:
        return False
    last = line[-1]
    return last in TAIL_BROKEN_CHARS or ("a" <= last <= "z")


def head_definitely_broken(line: str) -> bool:
    """首字符落在碎片集合里 —— 明确不是句首。"""
    line = line.lstrip()
    if not line:
        return False
    first = line[0]
    if first in HEAD_BROKEN_CHARS:
        return True
    if "a" <= first <= "z":
        return True
    return line.startswith(CONTINUATION_LEADERS)


def lines_of(content: str) -> list[str]:
    return [ln.strip() for ln in content.split("\n") if ln.strip()]


def find_line(lines: list[str], needle: str) -> int:
    """在行序列里找 needle，找不到返回 -1。"""
    for i, line in enumerate(lines):
        if line == needle:
            return i
    return -1


def short(text: str, limit: int = 30) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def head_text(content: str, limit: int = 30) -> str:
    return short(content.lstrip(), limit)


def tail_text(content: str, limit: int = 30) -> str:
    """块尾 limit 字；截断时前面补省略号，表示上文被略去。"""
    text = " ".join(content.rstrip().split())
    return text if len(text) <= limit else "…" + text[-limit:]


# ---------------------------------------------------------------- 数据

class Chunk:
    __slots__ = ("document_id", "file_name", "seq", "heading", "content", "lines")

    def __init__(self, document_id, file_name, seq, heading, content):
        self.document_id = document_id
        self.file_name = file_name
        self.seq = seq
        self.heading = heading or ""
        self.content = content
        self.lines = lines_of(content)

    @property
    def head_line(self) -> str:
        return self.lines[0] if self.lines else ""

    @property
    def tail_line(self) -> str:
        return self.lines[-1] if self.lines else ""


def load_chunks(conn: sqlite3.Connection) -> list[Chunk]:
    rows = conn.execute(
        """
        select c.document_id, d.file_name, c.seq, c.heading, c.content
        from chunks c
        join documents d on d.id = c.document_id
        order by c.document_id, c.seq
        """
    ).fetchall()
    return [Chunk(*row) for row in rows]


# ---------------------------------------------------------------- 四类判据

def check_heading(chunk: Chunk) -> list[str]:
    """块级检查：heading 字段本身是不是被从中间切了。"""
    h = chunk.heading.strip()
    if not h:
        return []
    # 冒号/分号结尾是标题引出下文的正常写法（"应提供下列材料："），不算悬空
    if h[-1] in HEADING_DANGLING_CHARS or h.endswith(("，", "、")):
        return [f"heading 结尾悬空：{short(h, 40)!r}"]
    if HEADING_NUMBERED_STUB.match(h):
        # "3.2 参"：编号还在、标题只剩一个字 —— 标题被从中间切了
        return [f"heading 只剩半截标题：{h!r}"]
    if len(h) >= 20 and h[-1] not in CLOSED_END and not is_new_unit(h) and cjk_ratio(h) >= 0.5:
        return [f"heading 是半句：{short(h, 40)!r}"]
    return []


def check_seam(prev: Chunk, nxt: Chunk) -> dict[str, list[str]]:
    """检查 prev -> nxt 这条接缝，返回命中的类别与证据（可能多类同时命中）."""
    hits: dict[str, list[str]] = {}
    tail = prev.tail_line
    head = nxt.head_line
    if not tail or not head:
        return hits

    tail_open = tail_looks_open(tail)
    head_new_unit = is_new_unit(head)
    tail_table = is_table_row(tail)
    head_table = is_table_row(head)

    # --- 3. word_split：前块末 token 与后块首 token 拼成一个词
    reasons: list[str] = []
    last = tail[-1]
    if last.isdigit() and re.match(r"^\d+(?![\s\d])", head):
        # 只有"数字后面直接跟着字/符号"才算切开；"9817 下一行"是新行编号，不算
        reasons.append(f"数字被切开：…{short(tail, 12)[-6:]}|{short(head, 8)}…")
    elif (last, head[0]) in WORD_BRIDGE_PAIRS:
        reasons.append(f"词被劈开：「{last}{head[0]}」")
    elif (tail[-2:], head[:2]) in PHRASE_BRIDGE_PAIRS:
        reasons.append(f"两个词被拼在一起：…{tail[-2:]}|{head[:2]}…")
    elif (
        not tail_table
        and not head_table
        and not head_new_unit
        and re.search(r"[A-Za-z][A-Za-z0-9_.\-]{2,}$", tail)
        and re.match(r"[A-Za-z0-9_.\-]{2,}", head)
    ):
        if head[:1].isdigit():
            reasons.append(f"数值被切开：…{short(tail, 14)[-8:]}|{short(head, 10)}…")
        else:
            reasons.append(f"标识符被切开：…{short(tail, 14)[-8:]}|{short(head, 10)}…")
    if reasons:
        hits["word_split"] = reasons

    # --- 4. structure_cut：表格行被切断（标题截断由 check_heading 按块查）
    reasons = []
    if (
        tail_open
        and tail_table
        and head_table
        and not head_new_unit
        and not re.match(r"^[0-9A-Za-z_][0-9A-Za-z_.\-]*\s", head)
        and tail != head  # 末行与首行是同一行：重叠把整行都给了两块，没切掉内容
    ):
        reasons.append(f"表格行被切断：…{short(tail, 24)}|{short(head, 24)}…")
    if reasons:
        hits["structure_cut"] = reasons

    # --- 1. head_fragment：块首不像句首
    reasons = []
    if not head_new_unit and head_definitely_broken(head):
        reasons.append(f"首字符是碎片：{head[0]!r}")
    if reasons:
        hits["head_fragment"] = reasons

    # --- 2. tail_fragment：块尾不像句尾
    reasons = []
    if tail_definitely_broken(tail):
        reasons.append(f"末字符悬空：{tail[-1]!r}")
    elif tail_open and not head_new_unit:
        # 后块行序列里紧随这一行的那一行如果是散文续接，而不是新的一条/表格行，就是没说完
        i = find_line(nxt.lines, tail)
        if 0 <= i < len(nxt.lines) - 1:
            follow = nxt.lines[i + 1]
            if is_prose(tail) and is_prose(follow) and not is_new_unit(follow):
                reasons.append(f"下一行是续接：…{short(tail, 24)}|{short(follow, 24)}…")
    if reasons:
        hits["tail_fragment"] = reasons

    return hits


class Finding:
    """一条发现：或是一条接缝（prev -> nxt），或是一块自身的问题（prev is nxt）。"""
    __slots__ = ("chunk", "prev", "nxt", "hits")

    def __init__(self, chunk: Chunk, prev: Chunk, nxt: Chunk, hits: dict[str, list[str]]):
        self.chunk = chunk      # 被判定有问题的那一块（接缝归到后一块）
        self.prev = prev
        self.nxt = nxt
        self.hits = hits

    @property
    def is_seam(self) -> bool:
        return self.prev is not self.nxt


def scan(chunks: list[Chunk]) -> tuple[list[Finding], int]:
    """按文档顺序扫接缝 + 逐块查标题，返回（发现列表，接缝总数）。"""
    by_doc: dict[str, list[Chunk]] = {}
    for c in chunks:
        by_doc.setdefault(c.document_id, []).append(c)

    findings: list[Finding] = []
    seams = 0
    for items in by_doc.values():
        items.sort(key=lambda c: c.seq)
        for chunk in items:
            heading_reasons = check_heading(chunk)
            if heading_reasons:
                findings.append(Finding(chunk, chunk, chunk, {"structure_cut": heading_reasons}))
        for prev, nxt in zip(items, items[1:]):
            seams += 1
            hits = check_seam(prev, nxt)
            if hits:
                findings.append(Finding(nxt, prev, nxt, hits))
    return findings, seams


def primary_category(hits: dict[str, list[str]]) -> str:
    for name, _ in CATEGORIES:
        if name in hits:
            return name
    raise AssertionError("hits 为空")


# ---------------------------------------------------------------- 输出

def report(chunks: list[Chunk], findings: list[Finding], seams: int, sample: int,
           max_ratio: float, label: str | None = None) -> float:
    per_category: dict[str, list[Finding]] = {name: [] for name, _ in CATEGORIES}
    for f in findings:
        for name in f.hits:
            per_category[name].append(f)

    cut_chunks = {id(f.chunk) for f in findings}
    total = len(chunks)
    ratio = len(cut_chunks) / total if total else 0.0

    print(f"库：{label or DB_PATH}")
    print(f"块总数 {total}，接缝 {seams} 条")
    print(
        f"疑似切断：发现 {len(findings)} 处，涉及 {len(cut_chunks)} 块，"
        f"比例 {len(cut_chunks)}/{total} = {ratio:.2%}，阈值 {max_ratio:.2%}"
    )
    print()

    for name, cat_label in CATEGORIES:
        rows = per_category[name]
        print(f"[{name}] {cat_label}：{len(rows)} 处")
        limit = len(rows) if sample == 0 else min(len(rows), sample)
        for f in rows[:limit]:
            where = f"接缝 seq={f.prev.seq}->{f.nxt.seq}" if f.is_seam else "本块 heading，无接缝"
            print(f"  {f.chunk.document_id}  {f.chunk.file_name}  seq={f.chunk.seq}  ({where})")
            if f.is_seam:
                print(f"      块首30：{head_text(f.nxt.content)}")
                print(f"      块尾30：{tail_text(f.prev.content)}")
            else:
                print(f"      本块首30：{head_text(f.chunk.content)}")
            for reason in f.hits[name]:
                print(f"      · {reason}")
        if limit < len(rows):
            print(f"  …… 还有 {len(rows) - limit} 处")
        print()

    # 同一处可能多类同时命中，按主类别去重后再报一次总数，避免"加起来对不上"
    primaries = {name: 0 for name, _ in CATEGORIES}
    for f in findings:
        primaries[primary_category(f.hits)] += 1
    print("按主类别去重：" + "，".join(f"{cat_label} {primaries[name]}" for name, cat_label in CATEGORIES))
    return ratio


# ---------------------------------------------------------------- 自检（证伪）

INJECT_SAMPLES = [
    # (前半段, 后半段, 注入块的 heading, 这条接缝必须命中的类别)
    # a) 词内切断：两侧单看都不像碎片，"报警|代码"只有拼接判据能看出来
    ("……客户端超时后先查链路，再查看报警", "代码E-102的说明，确认从站是否仍在扫描周期内。", None, "word_split"),
    # b) 连词悬空：末字符是"并且"，话没说完就截断
    ("……改完参数必须整机断电，并且", "需要在断电 30 秒后重新上电，否则新配置不生效。", None, "tail_fragment"),
    # c) 标题截断 + 块首承接：heading 被切成"3.2 参"，块首以"的"开头
    ("……参数表按量程分段维护，逐条核对", "的默认值见下表，逐项确认后再下发到从站。", "3.2 参", "structure_cut"),
]

INJECT_MARK = "【注入】"


def _backup(src: Path, dst: Path) -> None:
    """用 backup() 复制：WAL 模式下 cp 主库文件可能拿到缺 -wal 内容的副本。"""
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def selftest(max_ratio: float) -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="chunkprobe-"))
    clean = tmpdir / "clean.db"
    dirty = tmpdir / "dirty.db"
    try:
        _backup(DB_PATH, clean)
        print(f"已用 Connection.backup() 复制到临时文件：{clean}")

        # 每组注入要一对相邻块（seq, seq+1），所以只挑有下一块的候选
        conn = sqlite3.connect(f"file:{clean.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute("select document_id, seq from chunks").fetchall()
        finally:
            conn.close()
        existing = set(rows)
        pairs = []
        used_docs = set()
        # 优先挑文档中段的接缝（seq>0 且有下一块），注入出来的场景更像真实切块
        for doc, seq in sorted(existing, key=lambda r: (r[1] == 0, r[0], r[1])):
            if len(pairs) >= len(INJECT_SAMPLES):
                break
            if doc in used_docs or seq == 0:
                continue
            if (doc, seq + 1) in existing:
                pairs.append((doc, seq))
                used_docs.add(doc)
        if len(pairs) < len(INJECT_SAMPLES):
            print("自检失败：库里找不到足够的相邻块来注入", file=sys.stderr)
            return 2

        _backup(clean, dirty)
        injected = []
        conn = sqlite3.connect(str(dirty))
        try:
            for (doc, seq), (first, second, heading, expect) in zip(pairs, INJECT_SAMPLES):
                conn.execute(
                    "update chunks set content = ?, heading = coalesce(?, heading)"
                    " where document_id = ? and seq = ?",
                    (f"{INJECT_MARK}{first}", heading, doc, seq),
                )
                conn.execute(
                    "update chunks set content = ? where document_id = ? and seq = ?",
                    (second, doc, seq + 1),
                )
                injected.append((doc, seq, heading, expect))
            conn.commit()
        finally:
            conn.close()

        print("已注入 3 条故意切断的接缝（原库只以 mode=ro 打开，注入只发生在临时副本上）：")
        for doc, seq, heading, expect in injected:
            print(f"  {doc} seq={seq}->{seq + 1}  heading={heading!r}  期望命中 {expect}")

        conn = sqlite3.connect(f"file:{dirty.as_posix()}?mode=ro", uri=True)
        try:
            dirty_chunks = load_chunks(conn)
        finally:
            conn.close()
        dirty_found, dirty_seams = scan(dirty_chunks)
        # 接缝级发现按 (document_id, 前一块的 seq) 索引；块级发现按自身 seq
        by_seam: dict[tuple[str, int], dict[str, list[str]]] = {}
        for f in dirty_found:
            key = (f.chunk.document_id, f.prev.seq)
            by_seam.setdefault(key, {}).update(f.hits)

        print()
        print("=== 注入库扫描 ===")
        report(dirty_chunks, dirty_found, dirty_seams, 5, max_ratio, label=f"{DB_PATH}（注入副本）")

        ok = True
        for doc, seq, heading, expect in injected:
            hits = by_seam.get((doc, seq))
            if hits is None:
                print(f"  未报出：{doc} seq={seq}->{seq + 1}", file=sys.stderr)
                ok = False
            elif expect not in hits:
                print(f"  报出了但类别不对：{doc} seq={seq} 期望 {expect}，实际 {sorted(hits)}", file=sys.stderr)
                ok = False
            else:
                print(f"  命中：{doc} seq={seq}->{seq + 1} -> {sorted(hits)}")

        conn = sqlite3.connect(f"file:{clean.as_posix()}?mode=ro", uri=True)
        try:
            chunks = load_chunks(conn)
        finally:
            conn.close()
        clean_found, _ = scan(chunks)
        cut = {id(f.chunk) for f in clean_found}
        clean_ratio = len(cut) / len(chunks)
        print()
        print("=== 干净副本 ===")
        print(f"干净库 {len(chunks)} 块，疑似切断 {len(cut)} 块，比例 {clean_ratio:.2%}")
        if clean_ratio > max_ratio:
            print(f"警告：干净库占比 {clean_ratio:.2%} 已高于阈值 {max_ratio:.2%}", file=sys.stderr)
        else:
            print(f"干净库低于阈值 {max_ratio:.2%}")

        print()
        print("自检通过：注入的三处切断全部报出，且类别正确" if ok else "自检未通过")
        return 0 if ok else 2
    finally:
        for path in (clean, dirty):
            for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
                try:
                    p.unlink()
                except OSError:
                    pass
        try:
            tmpdir.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------- 入口

DB_PATH = Path(__file__).resolve().parents[3] / "src/AgentWorkbench.Webapi/data/agentworkbench.db"


def main(argv: list[str] | None = None) -> int:
    global DB_PATH
    parser = argparse.ArgumentParser(description="切块质量抽样：检查块边界是否切断句子")
    parser.add_argument("--db", default=str(DB_PATH), help="sqlite 库路径（只读打开）")
    parser.add_argument("--sample", type=int, default=5, help="每类最多列几条明细，0 表示不限（默认 5）")
    parser.add_argument("--max-ratio", type=float, default=0.05, help="疑似切断块占比阈值（默认 0.05）")
    parser.add_argument("--selftest", action="store_true", help="跑反向检查：备份+注入 3 条切断块，必须全报出")
    args = parser.parse_args(argv)

    DB_PATH = Path(args.db).resolve()
    if not DB_PATH.exists():
        print(f"找不到库：{DB_PATH}", file=sys.stderr)
        return 2

    if args.selftest:
        return selftest(args.max_ratio)

    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=CONNECTION_TIMEOUT_SECONDS)
    try:
        chunks = load_chunks(conn)
    finally:
        conn.close()
    if not chunks:
        print("chunks 表是空的", file=sys.stderr)
        return 2

    found, seams = scan(chunks)
    ratio = report(chunks, found, seams, args.sample, args.max_ratio)
    return 1 if ratio > args.max_ratio else 0


if __name__ == "__main__":
    sys.exit(main())
