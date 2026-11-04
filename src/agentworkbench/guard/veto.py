"""结果否决：工具返回之后问一句「这结果能不能作为依据」，不能就终止这一轮。

落点在工具返回之后、下一次模型请求之前（D-02）：拒答靠代码否决，
不靠提示词——模型只看到几段文字，手里没有任何数字，要它拒答等于要它猜
「这几段够不够」。给模型申诉权的闸等于没有闸；误拒率高时的出路是
放宽阈值，不是让它把结论改回来。

执行管道不认识「资料不足」，只认识「有人要求停」——判定内容由本包提供，
管道只读结论。

判定不在这里重算（D-03）：它读装填好的 Evidence——那份判定由
guard.sufficiency 在检索时算好。拒答闸最容易做歪的地方就是
「两处各判一遍」，两个实现迟早漂移且不报错。本函数签名没有距离参数，
重算在类型上不可能。

保守方向在这条链路上是单向的：非本工具、空结果一律**不否决**——
格式变了应该让来源面板少一块，不该把正常对话全部拒掉。
宁可放过，不可误杀（C# remarks 原话）。

证据必须先落库，再执行终止：拒答时那份命中也是证据（可核六 / REQ-21，
引用记录落库）——落库动作在管道接线时做，本函数只产生终止决定。

来源：C# `Knowledge.Local/KnowledgeInsufficiencyVeto.cs`、`KnowledgeRefusal.cs`
服务于：REQ-09（不编二：判了要拦住）、G10
"""

from __future__ import annotations

from dataclasses import dataclass

from agentworkbench.core.judgment import Evidence
from agentworkbench.core.runs import RunReasonCode, RunStatus

SEARCH_KNOWLEDGE_TOOL = "search_knowledge"
"""知识检索的工具名。执行管道只认这个名字，不认识知识库（D-02）。
将来工具注册表落地时挪过去，名字不变——轨迹里已按它记录。"""

REFUSAL_COPY = "资料不足以回答该问题。知识库里没有找到能支撑回答的内容。"
"""固定拒答文案（判据里那句）。不用模型生成；拒答卡（G11）的文案源。"""


@dataclass(frozen=True)
class AgentRunStop:
    """一次终止决定：以什么状态收口、为什么、界面说什么。"""

    status: RunStatus
    code: RunReasonCode
    message: str


def inspect(tool_name: str, result: Evidence | None) -> AgentRunStop | None:
    """看一次工具结果要不要终止本轮。返回 None = 放行。

    与 C# 的差异写明：C# 的钩子拿 JSON 文本、解析失败走「不否决」；
    Python 侧管道传类型化的 Evidence，解析失败路径由 None 输入表达，
    保守语义不变。
    """
    if tool_name != SEARCH_KNOWLEDGE_TOOL or result is None:
        return None
    if result.judgment.verdict.value != "insufficient":
        return None
    return AgentRunStop(
        status=RunStatus.REFUSED,
        code=RunReasonCode.KNOWLEDGE_INSUFFICIENT,
        message=REFUSAL_COPY,
    )
