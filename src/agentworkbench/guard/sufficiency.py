"""充分性判定：这组命中够不够，不够就别答。

纯函数：不进 DI（引入状态）、不碰数据库（引入时间），两者都会让它无法复算。
规格是文档里的「判定用例表」，代码只是它的翻译——规则改动时先改表，再改这里。

判定用「间隔」不用绝对距离（D-01）：融合分第一名恒为 1/61，不表达"有多相关"；
绝对距离换模型就作废。间隔 = 第 2~5 名平均距离 − 第一名：库内有答案时正确那块
明显赢，库外问题时前五挤成一团、间隔接近零。间隔判不了"够不够近"，
所以还要一个绝对距离阈值——两个必须同时在场，缺一个只能落「未判定」。

只输出结论，不输出数据。四值之一加一个原因：已回答 / 依据较弱 / 资料不足 / 未判定。
阈值可空——空的时候结论落在「未判定」，不能用 0 假装配置过（D-12）。
候选过滤与拒答阈值不能共用一个数。

相关决策：D-01（用间隔）、D-03（只算一次）、D-12（阈值可空）
来源：C# `Knowledge.Local/Sufficiency/SufficiencyGate.cs`
服务于：REQ-09（不编）、G9
"""

from __future__ import annotations

from collections.abc import Sequence
from math import isfinite

from agentworkbench.core.judgment import (
    Judgment,
    JudgmentReason,
    JudgmentValue,
    SufficiencyOptions,
)

MARGIN_WINDOW = 5
"""参与间隔计算的命中条数上限：第六名往后进不了上下文，不该影响判定。"""


def judge(candidates: Sequence[float | None], options: SufficiencyOptions) -> Judgment:
    """按判定用例表判本次检索够不够。

    `candidates` 是每个候选到问题的向量距离（None = 这条没有距离，比如来自
    全文路）；顺序不可信，内部自己排。距离越小越相关。
    """
    if not candidates:
        # 检索为空不是"没找到"，是"资料不足"——由判定段说，不由召回说（§1.7 总表）。
        return Judgment(JudgmentValue.INSUFFICIENT, None, None, JudgmentReason.NO_CANDIDATES)

    # 只取带距离的、有限的、最小的那几条。空距离与非有限值一律视同"这条没有距离"：
    # 当零会把间隔算小（误判不足），按总条数除会把间隔算大（误判已回答），
    # 两种错都长得像正常结果。
    distances = sorted(d for d in candidates if d is not None and isfinite(d))[:MARGIN_WINDOW]

    if not distances:
        return Judgment(JudgmentValue.UNDETERMINED, None, None, JudgmentReason.NO_VECTOR)

    # 第一名取"集合里最小的距离"，不看它排第几：排序是融合算的，
    # 排第一的块可能来自全文路（没有距离）。
    top = distances[0]

    if len(distances) == 1:
        return Judgment(JudgmentValue.WEAK, top, None, JudgmentReason.SINGLE_HIT)

    margin = sum(distances[1:]) / (len(distances) - 1) - top

    # 比较一律用严格小于：等于阈值不拒、等于充分距离不算够近。
    # 改成 <= 会让边界行为漂移，而两边都能自证合理。
    if options.refuse_margin is not None and margin < options.refuse_margin:
        return Judgment(JudgmentValue.INSUFFICIENT, top, margin, JudgmentReason.NO_WINNER)

    if (
        options.is_complete
        and options.sufficient_distance is not None
        and top < options.sufficient_distance
    ):
        return Judgment(JudgmentValue.ANSWERED, top, margin, None)

    if not options.is_complete:
        return Judgment(JudgmentValue.UNDETERMINED, top, margin, JudgmentReason.THRESHOLD_UNSET)

    return Judgment(JudgmentValue.WEAK, top, margin, JudgmentReason.TOO_FAR)
