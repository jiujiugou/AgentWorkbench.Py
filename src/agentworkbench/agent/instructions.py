"""指令注入：本轮的约束（人格、引用规范）只往模型请求前插一条系统消息。

不写进会话历史，否则同一段指令会随每轮对话重复累积。

与 C# 版的形态差异（F3 §1.2 定案）：C# 是「模型主动调 search_knowledge」
的工具循环，指令第一条是「回答前必须先调用工具」；Python 版用**系统预检索**——
证据由代码先取好、连编号一起放进用户消息，模型没有工具可调。改写来自实测
反证（A-02：同一模型四次真跑一次零检索，q03 凭常识编六条建议）——prompt 里的
约束是请求不是保证，检索必须发生这件事由结构保证，不由指令保证。

引用格式是 [n]，n 取自证据编号（D-05）：换成编号而不是「文件名 + 片段序号」，
是因为后者要靠文本匹配回指——模型缩写一次文件名（《H5U手册》对《汇川H5U系列
…手册》），引用就会被判成越界，把真出处误杀。编号是封闭空间里的一次选择。

来源：C# `KnowledgeInstructions.cs`
服务于：REQ-08（可核）、G5（预检索）、G6（引用生成）
"""

from __future__ import annotations

import hashlib

from agentworkbench.core.judgment import NumberedHit

ANSWER_WITH_CITATIONS = (
    "你是严谨的资料问答助手。\n"
    "只能依据下面给出的【资料】作答，不得使用资料之外的知识，也不得编造。\n"
    "每个结论后用方括号编号标注出处，编号取自资料条目前方括号里的号（例如 [2]；多条写作 [1,3]）。\n"
    "只能引用资料里出现过的编号，不要自己编编号。\n"
    "若资料不足以回答，直接回答「资料不足以回答该问题」，不要凭猜测作答。\n"
    "若资料与问题有关、但里面没有问的那个具体信息（例如问保修期，手册只讲接线），"
    "要明确写出「资料中未提及这一项」。不要用资料以外的常识补足——补出来的句子没有出处，"
    "读者会把它当作有据可查的内容。"
)


# 用户消息的三个模板片段。抽成常量不是为了复用，是为了让 prompt 版本号能覆盖它们——
# 版本号只哈希系统指令的话，改一处证据块格式（比如把页码挪到括号前）不会让版本变，
# 而那足以改变模型的引用行为。
USER_PROMPT_TEMPLATE = "【问题】\n{question}\n\n【资料】\n{blocks}"
USER_PROMPT_NO_EVIDENCE = "【问题】\n{question}\n\n【资料】\n（无）"
EVIDENCE_LINE = "[{no}] 片段{seq}（第 {page_from} 页）\n{content}"


def _prompt_fingerprint() -> str:
    """prompt 内容的指纹，12 位十六进制。改了任何一个字都会变。"""
    payload = "\x1f".join(
        (ANSWER_WITH_CITATIONS, USER_PROMPT_TEMPLATE, USER_PROMPT_NO_EVIDENCE, EVIDENCE_LINE)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


PROMPT_VERSION = _prompt_fingerprint()
"""落库用的 prompt 版本（F4 §三-3）。

**从代码派生，不手填。** 手填的版本号必然在某次改 prompt 时忘记递增，而忘了
不会报错——库里的记录只会一直说"这批答案都是 v3"，直到有人拿两个不同行为的
回答来对账。派生版本号的代价是一次 sha256（模块导入时算一次）。

改 prompt 时不用做任何事，版本自己会变；这也意味着**不要把它当语义版本用**——
`v2` 到 `v3` 之间不代表"改得更多"，只代表"不一样了"。
"""


def build_user_prompt(question: str, evidence: tuple[NumberedHit, ...] | list[NumberedHit]) -> str:
    """问题 + 编号资料段。编号即 NumberedHit.no，引用校验按同一空间核对（D-05）。"""
    if not evidence:
        return USER_PROMPT_NO_EVIDENCE.format(question=question)
    blocks = "\n".join(
        EVIDENCE_LINE.format(no=h.no, seq=h.seq, page_from=h.page_from, content=h.content)
        for h in evidence
    )
    return USER_PROMPT_TEMPLATE.format(question=question, blocks=blocks)


# ── F7 通用对话 ─────────────────────────────────────────────
# 通用对话的合同与知识库问答不同：它允许模型凭通用知识回答，但不许声称
# 答案来自企业资料，也不许产生引用角标。提示词只能“请求”模型不编造来源，
# 真正的保证仍在结构上：这条链路不检索、不塞证据、落库不写引用快照。
GENERAL_CHAT_SYSTEM = (
    "你是通用对话助手。你可以使用自己的通用知识回答。\n"
    "本次对话没有检索企业资料，回答中不得声称内容来自企业文档、知识库或内部资料。\n"
    "不要使用 [1] 这类引用编号，也不要编造文件名、页码或原文。\n"
    "如果不知道，就明确说不知道；不要为了显得确定而编造事实。\n"
    "回答使用简洁的中文，除非用户要求其他语言。"
)

GENERAL_USER_TEMPLATE = "【对话历史】\n{history}\n\n【当前问题】\n{question}"

GENERAL_PROMPT_VERSION = hashlib.sha256(
    (GENERAL_CHAT_SYSTEM + "\x1f" + GENERAL_USER_TEMPLATE).encode("utf-8")
).hexdigest()[:12]
"""通用对话提示词版本。与 F3 的 PROMPT_VERSION 分开：两套合同各自变更。"""


def build_general_user_prompt(history: str, question: str) -> str:
    """把同一条 general 会话的历史与当前问题交给模型。

    历史只来自同 kind 的会话（类型不匹配时 `start_turn` 已新建），所以这里
    不会把知识库问答的引用历史混进通用回答。
    """
    return GENERAL_USER_TEMPLATE.format(history=history or "（无）", question=question)
