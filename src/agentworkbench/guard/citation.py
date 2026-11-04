"""引用两级校验：一级看编号是否存在，二级看内容对不上不上（G7/G8）。

「有出处」本身是一种担保，指向错块比没有出处更危险。所以引用候选
只能取本次证据，编号空间封闭。二级校验用可复算的字符覆盖，不请模型
当裁判——但它在同主题邻块上判别力接近零，这是已知缺陷，不是没配阈值。

一级只保证「号码存在」，二级才防「号码存在但内容对不上」——后者不是
编造，是**误挂**：用户看到角标、打开原文、发现那句话根本不在那里。
一级便宜、全自动、错误可剥离（越界标注直接摘掉——越界编号指向
不存在的东西，留着就是假担保）。

二级用字符覆盖率而不是让模型自评（D-04）：模型当裁判不可复算，同一个
答案跑两次可能得出两个结论；覆盖率完全由输入决定。它用与索引同一套
分词（core.tokenizer）——两套规则会让「检索判定相关」和「引用判定
对得上」互相矛盾。模型用自己的话复述时覆盖率天然偏低，这个数只适合
横向对比，不适合当绝对值判据（判别力问题见 G8 remaining）。

阈值可空（D-12）：未配置时只记录覆盖率数值，判 UNVERIFIED 不标黄——
用 0 表示「未配置」会让「没判定」和「判定通过」长得完全一样。

标注归属规则（CitationParser.LocateSentence 的存在理由）：标注挂到
**它前面那一句**。模型既可能写「……SD 卡[2]。」也可能写「……SD 卡。[2]」，
两种都是「这句有出处」；按位置归属会把第二种算进下一句，句级统计
整整错一位，每个数字都反着来。

切句只认标点：中文句末标点与换行算边界，连续标点（？！）并入同一句；
**英文句点不算**——工业手册里 192.168.1.88、V4.2 到处都是，按点切
会把地址切碎。

来源：C# `Knowledge.Local/Citations/CitationParser.cs` / `CitationVerifier.cs`
服务于：REQ-08（可核二/三）、G7 / G8
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from agentworkbench.core.judgment import (
    CitationFirstCheck,
    CitationSecondCheck,
    NumberedHit,
    SupportVerdict,
)
from agentworkbench.core.tokenizer import tokenize

_CITATION_PATTERN = re.compile(r"\[\s*(\d+(?:\s*[,，]\s*\d+)*)\s*\]")
_SENTENCE_ENDS = "。！？；!?;\n"


@dataclass(frozen=True)
class ParsedAnswer:
    """切好的句子 + 尚未校验的原始标注（所属句序号从 0 起，编号从 1 起）。"""

    sentences: tuple[str, ...]
    raw: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class Citation:
    """一条引用标注的校验结论（一句可以挂多处）。

    first 是一级（编号在本次证据范围内？越界即 STRIPPED）；
    second 是二级（内容支持？）；越界时 second 与 coverage 均为 None——
    一级都没过，二级无从谈起。
    """

    sentence_index: int
    evidence_no: int
    document_id: str
    # 出处文件名：界面靠它写「《手册》P12」，不必再查一次库（§1.6）。
    # 越界（STRIPPED）时为空——那个编号根本没有对应的块，也就没有文件名
    file_name: str
    seq: int
    chunk_id: int
    first: CitationFirstCheck
    second: CitationSecondCheck | None
    coverage: float | None


@dataclass(frozen=True)
class CitationReport:
    """一次作答的引用体检报告：句级统计 + 逐条明细 + 剥离越界后的正文。

    报告只给界面，**不喂下一步**（§1.3）——它不改变判定，只改变显示。
    """

    sentence_count: int
    cited_sentence_count: int
    uncited_sentence_count: int
    out_of_range_count: int
    unsupported_count: int
    cleaned_answer: str
    citations: tuple[Citation, ...]
    # 整段结论，由 citations 聚合而来（不是各条结论的复制）：
    # 「一句出处都没有」与「有出处但挂错块」都算 unsupported，但它们要显示的话不同，
    # 所以两个计数都留着，界面自己决定说哪一句。
    support_verdict: SupportVerdict
    # 无出处句占比。除在这里而不是让界面除：界面除一次，就多一处可能除错的地方，
    # 而 0/0 在 JS 里是 NaN、在 Python 里会抛——两边表现不同，展示就对不上了。
    uncited_ratio: float

    @staticmethod
    def empty(answer: str = "") -> CitationReport:
        """没有任何出处的空报告（非知识问答、或答案为空时使用）。

        空报告判 `UNDETERMINED` 而不是 `UNSUPPORTED`：没有内容可判，
        与「有内容但没出处」是两件事——后者才是要警告的。
        """
        return CitationReport(0, 0, 0, 0, 0, answer, (), SupportVerdict.UNDETERMINED, 0.0)


def parse(answer: str | None) -> ParsedAnswer:
    """切句并抽取标注。空答案返回空结果。"""
    if not answer or not answer.strip():
        return ParsedAnswer((), ())

    ranges = _sentence_ranges(answer)
    sentences = tuple(answer[start:end] for start, end in ranges)

    raw: list[tuple[int, int]] = []
    for match in _CITATION_PATTERN.finditer(answer):
        sentence_index = _locate_sentence(answer, ranges, match.start())
        for part in re.split(r"[,，]", match.group(1)):
            part = part.strip()
            if part.isdigit() and int(part) > 0:
                raw.append((sentence_index, int(part)))

    return ParsedAnswer(sentences, tuple(raw))


def strip_citations(text: str | None) -> str:
    """抹掉所有编号标注，得到纯正文。

    算覆盖率前必须调用：[2] 在分词器眼里是一个 ASCII 词 2，
    留在句子里会平白拉低覆盖率，把好引用判成「指向错块」。
    """
    if not text:
        return ""
    return _CITATION_PATTERN.sub("", text)


def keep_citations(text: str | None, keep: Callable[[int], bool]) -> str:
    """只保留满足 keep 的编号，重写标注；返回新正文。

    [2,9] 里若只有 9 越界，结果是 [2] 而不是整段删掉——
    越界的是那个编号，不是这句话的出处。
    """
    if not text:
        return ""

    def _rewrite(match: re.Match[str]) -> str:
        kept = [
            part.strip()
            for part in re.split(r"[,，]", match.group(1))
            if part.strip().isdigit() and int(part.strip()) > 0 and keep(int(part.strip()))
        ]
        return "[" + ",".join(kept) + "]" if kept else ""

    return _CITATION_PATTERN.sub(_rewrite, text)


def verify(
    answer: str | None,
    evidence: Sequence[NumberedHit] | None,
    coverage_threshold: float | None = None,
) -> CitationReport:
    """校验一次作答。

    `evidence` 是装填好的证据：no（= 下标 + 1）就是模型看到的编号；
    为空表示没有可用证据。`coverage_threshold` 为 None 表示二级未定标：
    只记录覆盖率，判 UNVERIFIED（D-12）。
    """
    if not answer or not answer.strip():
        return CitationReport.empty(answer or "")

    parsed = parse(answer)
    pool = list(evidence) if evidence else []
    pool_by_no = {hit.no: hit for hit in pool}
    coverage_cache: dict[int, frozenset[str]] = {}

    citations: list[Citation] = []
    for sentence_index, evidence_no in parsed.raw:
        hit = pool_by_no.get(evidence_no)
        if hit is None:
            citations.append(
                Citation(
                    sentence_index,
                    evidence_no,
                    "",
                    "",
                    0,
                    0,
                    CitationFirstCheck.STRIPPED,
                    None,
                    None,
                )
            )
            continue

        coverage = _coverage(parsed.sentences[sentence_index], hit, coverage_cache)
        if coverage_threshold is None:
            second: CitationSecondCheck | None = CitationSecondCheck.UNVERIFIED
        elif coverage < coverage_threshold:
            second = CitationSecondCheck.UNSUPPORTED
        else:
            second = CitationSecondCheck.SUPPORTED

        citations.append(
            Citation(
                sentence_index,
                evidence_no,
                hit.document_id,
                hit.file_name,
                hit.seq,
                hit.chunk_id,
                CitationFirstCheck.KEPT,
                second,
                coverage,
            )
        )

    cited = {c.sentence_index for c in citations if c.first is CitationFirstCheck.KEPT}
    cleaned = keep_citations(answer, lambda no: 1 <= no <= len(pool_by_no))

    sentence_count = len(parsed.sentences)
    uncited = sentence_count - len(cited)
    return CitationReport(
        sentence_count=sentence_count,
        cited_sentence_count=len(cited),
        uncited_sentence_count=uncited,
        out_of_range_count=sum(1 for c in citations if c.first is CitationFirstCheck.STRIPPED),
        unsupported_count=sum(1 for c in citations if c.second is CitationSecondCheck.UNSUPPORTED),
        cleaned_answer=cleaned,
        citations=tuple(citations),
        support_verdict=_support_verdict(citations, sentence_count),
        uncited_ratio=(uncited / sentence_count) if sentence_count else 0.0,
    )


def _support_verdict(citations: list[Citation], sentence_count: int) -> SupportVerdict:
    """整段结论。优先级从"最严重"往"最保守"排，不看条数。

    顺序是有意的：

    1. 一句出处都没有 → `UNSUPPORTED`。这是 F3 run 18 的形态，也是唯一
       一种"整段都是编的"能被自动抓住的情况——它必须先于其它判断。
    2. 有引用但有一条挂错块 → `UNSUPPORTED`。一条错挂足以让整段不可引用：
       读者看到角标就会去打开原文，打开发现没有那句话，整段的可信度归零。
    3. 有引用但有一条判不了（阈值未配置）→ `UNDETERMINED`。
       **不许把未判定当已核实**（D-12）。
    4. 其余 → `SUPPORTED`。
    """
    kept = [c for c in citations if c.first is CitationFirstCheck.KEPT]
    if not kept:
        return SupportVerdict.UNSUPPORTED if sentence_count else SupportVerdict.UNDETERMINED
    seconds = {c.second for c in kept}
    if CitationSecondCheck.UNSUPPORTED in seconds:
        return SupportVerdict.UNSUPPORTED
    if None in seconds or CitationSecondCheck.UNVERIFIED in seconds:
        return SupportVerdict.UNDETERMINED
    return SupportVerdict.SUPPORTED


def _coverage(sentence: str, hit: NumberedHit, cache: dict[int, frozenset[str]]) -> float:
    """一句与一块的字符覆盖率：该句的两字组合有多少出现在这一块里。"""
    tokens = tokenize(strip_citations(sentence))
    if not tokens:
        return 0.0

    if hit.chunk_id not in cache:
        cache[hit.chunk_id] = frozenset(tokenize(hit.content))

    matched = sum(1 for token in tokens if token in cache[hit.chunk_id])
    return matched / len(tokens)


def _sentence_ranges(text: str) -> list[tuple[int, int]]:
    """按句末标点切出区间。parse 与覆盖率统计共用同一套边界。"""
    ranges: list[tuple[int, int]] = []
    start = 0
    i = 0
    while i < len(text):
        if text[i] not in _SENTENCE_ENDS:
            i += 1
            continue
        # 连续标点算同一句的结尾（？！、。。。）：边界推到这一串之后
        end = i + 1
        while end < len(text) and text[end] in _SENTENCE_ENDS:
            end += 1
        if not _is_blank(text, start, end):
            ranges.append((start, end))
        start = end
        i = end
    if start < len(text) and not _is_blank(text, start, len(text)):
        ranges.append((start, len(text)))
    return ranges


def _locate_sentence(text: str, ranges: list[tuple[int, int]], position: int) -> int:
    """标注落在哪一句：句内就是本句；紧跟在句末标点之后（中间只有空白）仍归前一句。

    后一条是这个函数存在的理由。模型常写「……没有插好。[1] 请重新插拔。」——
    按位置归属会把它算成「第二句有出处、第一句是结论没依据」，整整错一位，
    于是每个句级统计都反着来。
    """
    if not ranges:
        return 0

    # 往回跳过空白，看是否紧贴着句末标点
    previous = position - 1
    while previous >= 0 and text[previous].isspace():
        previous -= 1
    if previous >= 0 and text[previous] in _SENTENCE_ENDS:
        for i, (start, end) in enumerate(ranges):
            if start <= previous < end:
                return i

    for i, (_, end) in enumerate(ranges):
        if position < end:
            return i  # 在句内
        if i + 1 < len(ranges) and position < ranges[i + 1][0]:
            return i  # 落在两句之间
    return len(ranges) - 1  # 尾部的标注归最后一句


def _is_blank(text: str, start: int, end: int) -> bool:
    return all(text[i].isspace() for i in range(start, end))
