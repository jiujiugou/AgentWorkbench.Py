"""会话与消息的词汇层：枚举、标题规则、引用快照的形状。

为什么单独一个文件：`messages.role` 的取值集要从这里生成 CHECK 约束
（同 `core/runs.py` 的 `RunStatus`）。在 DDL 里另抄一份字符串，两份迟早漂——
漂移之后库能写进代码不认识的取值，而展示层按 role 分支，遇到不认识的值
不会报错，只会安静地渲染成一条空消息。

来源：C# `Persistence/ConversationRepository.cs` 的 role 字符串
服务于：REQ-04（历史会话）/ REQ-21（三块一起保存）
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from agentworkbench.core.judgment import CitationFirstCheck, CitationSecondCheck


class MessageRole(StrEnum):
    """一条消息是谁说的。

    只有两种。**没有 `system`**：系统提示词属于一次运行的输入（F4 的步骤里），
    不是会话里给人看的一行——混进来会让"历史消息"里出现用户从没见过的文字，
    而导出/展示都会把它当对话内容。
    """

    USER = "user"
    ASSISTANT = "assistant"


class ConversationKind(StrEnum):
    """一条会话属于哪种答案合同（F7 §3.1）。

    创建时二选一，之后不可改；切换模式必须新建会话。这个枚举是 SQL CHECK
    与仓储、DTO 的唯一取值来源——在别处抄字符串，第三种类型出现时就会有
    一条路径悄悄放行。
    """

    KNOWLEDGE = "knowledge"
    GENERAL = "general"


TITLE_MAX_CHARS = 18
"""标题长度。写在这里而不是散在调用点：它是**判据的一部分**（换设备后标题必须相同），
不是装饰性的截断。18 与前端现有显示宽度对齐——两边取不同的数，同一个会话
在两个界面里的标题就会不一样，而"一模一样"这条判据立刻不成立。"""


def derive_title(question: str) -> str:
    """会话标题 = 首条问题前 18 字，**只写一次**。

    折叠空白是必需的：问题里带换行（从别处粘贴的多行文本）时，
    标题会在列表里折成两行，而列表是按单行高度排的。

    空问题抛错而不是返回"新会话"：空问题在入口就被拒了，走到这里说明
    上游漏了校验；给它编一个默认标题，等于把"校验漏了"变成一条看起来
    正常的历史记录。
    """
    collapsed = " ".join(question.split())
    if not collapsed:
        raise ValueError("空问题派生不出标题")
    return collapsed[:TITLE_MAX_CHARS]


EXCERPT_MAX_CHARS = 120
"""引用快照里存的原文长度。

数字来源是前端卡片的截断长度（`citations.ts` 取 `content.slice(0, 120)`），
不是 D-06 定的——D-06 只把"回原文"降级为片段卡片、把页码退出判据。

**库里存的和界面显示的必须是同一个数**，否则"与当时一致"要按哪一份算
就说不清了：换了截断长度之后，库里的快照与界面切出来的片段长度不同，
同一条引用在两个地方看起来不一样，而没有任何断言会红。
"""


@dataclass(frozen=True, slots=True)
class CitationSnapshot:
    """一条引用落库时的完整内容。

    四个必填字段都是**复制下来的值**，不是外键、不是指针：重建索引后
    `chunks` 全表换 id，指针要么查不到（空卡片），要么指向别的内容
    ——后者不报错，而用户正是来看原文的。

    `check_second` 为 `None` 表示**这条没有二级结论**，并不是"阈值没配"——
    阈值没配时 guard 判的是 `UNVERIFIED`（判了但判不了），那也是一个结论。
    写入口按原样复制，不替它编一个值：编成 `SUPPORTED` 就是给一句没有担保的话
    盖章（D-12），编成 `UNVERIFIED` 会把"没判"说成"判不了"。
    """

    no: int
    file_name: str
    seq: int
    excerpt: str
    check_first: CitationFirstCheck
    check_second: CitationSecondCheck | None = None
    coverage: float | None = None
    kb_id: str | None = None
