"""知识问答的判定与引用词汇表：从《F3-知识问答》§1.3 / §1.4 / §1.5 提取。

这里只放「文档定死、机器可查」的部分：值的集合、每个值之后发生什么、
每个值的界面呈现为什么必须互异。判定算术（怎么从距离算出四值）在
guard/sufficiency.py，还没有实现——词汇表先行是为了让实现没有自由发挥的空间：
先有测试钉死的值集与呈现规则，实现只能往里填算术。

系统失败不在判定值里：那是运行状态（core.runs.RunStatus.FAILED），
不是判定结论。混进来之后「未判定」和「系统挂了」就分不开，
而文档 §1.3 原话：判定输出四个取值加一个原因，系统失败不在其中。

来源：F3-知识问答 §1.3（判定必须在生成之前）、§1.4（两级校验）、
§1.5（不编三档）、§1.6（判定必须可见）；D-05、D-11、D-12
服务于：REQ-08 / REQ-09（G9 / G14 / G7 / G8）
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class JudgmentValue(StrEnum):
    """判定结论恰好四值。§1.3：判定输出四个取值 + 原因，加一个原因。

    「失败」不是第五个值：它是 run 的运行状态，混进判定值之后，
    「系统没能力判」和「系统挂了」在界面上就会长得一样——前者要求
    诚实说出来（must_disclose），后者走错误态，两条处理路径完全不同。
    """

    ANSWERED = "answered"  # 已回答：有明确赢家且足够近，放行，答案挂出处
    WEAK = "weak"  # 依据较弱：够近但没有赢家，放行 + 建议核对原文
    INSUFFICIENT = "insufficient"  # 资料不足：判不足，终止生成
    UNDETERMINED = "undetermined"  # 未判定：系统没有能力判断（缺向量/阈值缺失）


@dataclass(frozen=True)
class JudgmentPresentation:
    """一个判定值之后发生什么、界面上长什么样（§1.3 表格的机器可读版）。

    四个值的呈现必须互异（§1.6 判定必须可见）：判了但用户看不出，
    判定等于没做；最危险的「有答案、有引用、但引用全对不上」
    会混进正常答案里。
    """

    generates: bool  # 是否放行生成段（③④）
    user_visible: str  # 界面上必须出现的语义；四个值互不相同
    hides_sources: bool = False  # 不显示来源列表与句级摘要
    requires_fixed_copy: bool = False  # 必须用固定文案，不用模型生成
    verify_hint: bool = False  # 附加「建议核对原文」
    must_disclose: bool = False  # 放行但必须说出来（长得和已回答不一样）


JUDGMENT_PRESENTATIONS: dict[JudgmentValue, JudgmentPresentation] = {
    JudgmentValue.ANSWERED: JudgmentPresentation(
        generates=True,
        user_visible="正常回答 + 角标",
    ),
    JudgmentValue.WEAK: JudgmentPresentation(
        generates=True,
        user_visible="正常回答 + 核对提示",
        verify_hint=True,
    ),
    JudgmentValue.INSUFFICIENT: JudgmentPresentation(
        generates=False,
        user_visible="拒答卡（独立样式）",
        hides_sources=True,
        requires_fixed_copy=True,
    ),
    JudgmentValue.UNDETERMINED: JudgmentPresentation(
        generates=True,
        user_visible="独立提示（与已回答不同样式）",
        must_disclose=True,
    ),
}


class RefusalTier(StrEnum):
    """无证据题的三档（§1.5）。混成一档「负例」，误拒率和拒答准确率同时变假。

    每档的「正确行为」和「计进哪个数」都不同，这是分档的全部理由：
    库外要拒；同主题缺答案库里有手册但没那句话，正确行为是承认不是拒；
    超出检索能力的资料其实在库里，拒答是假通过。
    """

    OUT_OF_CORPUS = "out_of_corpus"  # 完全库外：拒答，来源为空
    SAME_TOPIC_GAP = "same_topic_gap"  # 同主题缺答案：不拒，承认资料里没有
    BEYOND_RETRIEVAL = "beyond_retrieval"  # 超出检索能力：拒答是假通过（D-11）


TIER_METRIC_BUCKETS: dict[RefusalTier, str] = {
    # 每档计进哪个数。值是指标桶名；"none" 表示不进任何比率（D-11：只做统计脚本）。
    RefusalTier.OUT_OF_CORPUS: "refusal_accuracy",  # 拒答准确率：分子分母都是它
    RefusalTier.SAME_TOPIC_GAP: "acknowledgment",  # 承认率：单列，不进误拒率
    RefusalTier.BEYOND_RETRIEVAL: "none",
}


class CitationFirstCheck(StrEnum):
    """一级校验结论（§1.4）：角标 [n] 指向的块存在且属于本次检索。

    封闭两值。一级便宜、全自动、错误可剥离（STRIPPED 直接摘掉），
    但它只证明「编号在范围内」，不证明「这句话来自这块」——那是二级的事。
    """

    KEPT = "kept"  # 编号合法，保留
    STRIPPED = "stripped"  # 越界 / 指向本次之外，剥离


class CitationSecondCheck(StrEnum):
    """二级校验结论（§1.4 / D-04）：块内容真的支持这句话。恰好三值。

    SUPPORTED = 覆盖率达标；UNSUPPORTED = 覆盖率低于阈值——号码对、内容
    对不上（误挂），同样假且更隐蔽：用户看到角标、打开原文、发现那句话
    根本不在那里。UNVERIFIED = 阈值未配置（D-12：只记录覆盖率数值，
    不判定）或判别力不足。判别力不足时只能标未核实，不许标已核实。

    不做第四种「已核实但不太确定」：那是引用可信度评分（§5.2 明确不做），
    一个 0.87 只制造新的误读——分级结论不是连续评分。
    """

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNVERIFIED = "unverified"


CITATION_SEQ_START = 1
"""引用编号从 1 起算（§1.6 编号空间封闭：候选取自本次证据）。

写成常量而不是散落的字面量，是因为「每次从 1 起算」一旦在不同实现里
漂成 0 起算或延续上一轮，编号就能指向本次证据之外——
等于给出一句无担保的担保，而且不报错。
"""


class SupportVerdict(StrEnum):
    """整段答案有没有资料支撑（三值）。与「几条引用指向错块」不是一回事。

    这是**整段**的结论，不是引用条目的结论：`unsupported_count` 数的是
    "有几条角标挂错了"，这里答的是"这段话整体能不能当有据可查的内容读"。

    最要紧的是 `UNSUPPORTED` 的第一种来源——**一句出处都没有**。
    F3 §四 的 run 18 就是这个形态：模型一次没检索，凭常识列了六条排查建议，
    读起来很像手册里的内容，`cited_sentence_count=0`。这种答案比空答案更危险，
    因为它把编造包装过了。界面据此加一句"下面这些话没有出处可核"。

    `UNDETERMINED` 不是"大概有支撑"：它是"没能力判"（阈值未配置，D-12）。
    把未判定显示成已核实，是这套判据里最不能容忍的一步。
    """

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNDETERMINED = "undetermined"


class JudgmentReason(StrEnum):
    """判定结论的原因码（C# SufficiencyReason 同语义）。

    只在 weak / insufficient / undetermined 上有值，「已回答」没有原因——
    它不是需要解释的例外。界面文案要区分「没配阈值」（threshold_unset）
    和「配了但判不了」（no_vector），只靠结论值区分不出来：两种都是未判定。
    """

    NO_CANDIDATES = "no_candidates"  # 一条候选都没有 → 资料不足（§1.7：由判定段说）
    NO_VECTOR = "no_vector"  # 候选全来自全文路，没有距离可用
    SINGLE_HIT = "single_hit"  # 只有一条带距离的参照，算不出间隔
    NO_WINNER = "no_winner"  # 间隔未达拒答阈值：前几名挤在一起，没有赢家
    TOO_FAR = "too_far"  # 有赢家但第一名不够近
    THRESHOLD_UNSET = "threshold_unset"  # 两个阈值没有同时配置，什么都判不了


@dataclass(frozen=True)
class SufficiencyOptions:
    """判定的两个旋钮，都可空（D-12）：空 = 没有能力判断，不是判定通过。

    用 None 表示未配置而不是 0——写了 0 就是 0，系统不替人猜
    「这是没配吧」。两个必须同时在场才判得出「已回答」：
    只配充分距离时，「挤在一起但恰好都很近」会被判成已回答，
    而那正是没有赢家的形状（D-01 代价原话）。
    """

    refuse_margin: float | None  # 间隔低于它判不足
    sufficient_distance: float | None  # 第一名距离低于它才算够近

    @property
    def is_complete(self) -> bool:
        return self.refuse_margin is not None and self.sufficient_distance is not None


@dataclass(frozen=True)
class Judgment:
    """一次充分性判定的结果（D-03：只算一次，写进工具输出，否决层只读）。

    frozen 是契约不是风格：否决层读到的必须是算出来的那个，
    中间任何一层都不许「顺手改个结论」。
    """

    verdict: JudgmentValue
    top_distance: float | None  # 集合里最小的距离；没有带距离的命中时为 None
    margin: float | None  # 第 2~5 名平均距离 − 第一名；带距离的命中不足两条时为 None
    reason: JudgmentReason | None  # 已回答为 None


@dataclass(frozen=True)
class NumberedHit:
    """带编号的命中；编号是模型写 [n] 的依据，也是服务端还原的顺序（D-05）。

    引用卡片四字段（文件名/片段序号/页码/原文）从这里出——界面层
    不回库查第二遍（证据只取一次，§1.6）。判定、否决、生成、引用校验、
    界面五方共享，所以放 core：它是词汇，不是某个包的私有形状。

    `file_name` 是"只取一次"的硬性要求，不是显示上的方便：界面要写
    「《汇川H5U用户手册》第 12 页」，而 `document_id` 是一串 uuid。
    让界面拿 id 再查一次文件名，等于把证据拆成两份——两份之间文档被删或改名，
    界面显示的出处就和模型当时看到的那份对不上了。
    """

    no: int
    chunk_id: int
    document_id: str
    file_name: str
    content: str
    seq: int
    page_from: int
    page_to: int
    heading: str | None
    vector_distance: float | None
    kb_id: str | None = None


@dataclass(frozen=True)
class Evidence:
    """一次检索的完整输出：编号命中 + 字符数 + 判定，三段同源（D-03）。

    frozen 是「证据只取一次」的物理形态：分发给模型、来源面板、引用校验的
    必须是同一份，任何一层都不许改写后再传。
    """

    hits: tuple[NumberedHit, ...]
    context_chars: int
    judgment: Judgment
