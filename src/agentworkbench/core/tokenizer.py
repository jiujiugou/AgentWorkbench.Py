"""中文分词：按字符类分段，连续汉字两字滑窗。

不用词典——词典多一个「版本」这个隐藏状态，换一版检索数字就漂。
索引与查询必须调同一个函数，否则命中零条且不报错。

为什么不靠 fts5 自己分：`unicode61` 把整句中文当成**一个** token（汉字属于
alphanumeric），所以必须先把文本切成两字片段、用空格隔开再交给它。索引侧和
查询侧都走 `tokenize`，两侧不一致时命中 0 条，而且不报错。

相关决策：D-09
服务于：REQ-07（可用二：答案不能落在缝里）
"""

from __future__ import annotations

import re

_CJK = r"[\u4e00-\u9fff]+"
# 只覆盖基本区。扩展 A 区（\u3400-\u4dbf）不收：那里面出现的多是编码噪声字符，
# 而 C# `BigramTokenizer` 只认基本区——判据按它复刻，范围差一个字就是一条 drift。
# 型号与英文词原样保留：`H5U`、`Modbus`、`E2` 各成一个词，
# 正好覆盖工业手册里最需要精确匹配的那类词。
# 连接符（- / . #）允许出现在词中间：`sqlite-wal`、`0x02`、`Write-Ahead`
# 是一个词——与 C# `BigramTokenizer` 同口径（评测判据按它复刻，两边必须逐 token 一致）。
_TOKEN = re.compile(r"[A-Za-z0-9_#](?:[A-Za-z0-9_#.\-/]*[A-Za-z0-9_#])?|" + _CJK)


def tokenize(text: str) -> list[str]:
    """切成索引项：连续汉字两字滑窗，型号与英文词原样。

    与 C# `BigramTokenizer` 逐 token 对齐，三处细节都是行为契约：
    - **按出现顺序输出**——检查器重算索引文本时按同序逐 token 比对，乱序即漂移
    - **ASCII 词整体保留**（含词中连接符）
    - **单字中文不造词**（两字滑窗无法覆盖它，召回交给相邻 bigram 兜）

    单字（如"泵"）不单独造词，只作为一个 token 存在——不为它做额外索引，
    召回交给别的字段兜底。
    """
    tokens: list[str] = []
    for match in _TOKEN.finditer(text):
        run = match.group()
        if run[0] < "\u0080":
            tokens.append(run)
            continue
        if len(run) == 1:
            continue
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def to_index_text(text: str) -> str:
    """写进 `chunks_fts.content` 的形态：两字片段用空格隔开。

    存的是**切分后的 token 串**，不是原文。任何"删了再去 content 里找这个词"
    的验证都会得出错误结论——原文在 `chunks.content` 里。
    """
    return " ".join(tokenize(text))


def single_han_chars(text: str) -> list[str]:
    """单独成词的汉字（`泵`、`站`）——它们不产生 bigram，只能靠 LIKE 兜底。

    与 C# `BigramTokenizer` 同名函数同口径；评测脚本里也有一个同名的，
    两边必须一致（`test_query_contract.py` 的等价断言守着）。
    """
    return [
        match.group()
        for match in _TOKEN.finditer(text)
        if match.group()[0] >= "\u0080" and len(match.group()) == 1
    ]


def to_match_query(text: str) -> str:
    """查询串：每个 token 加引号后 **OR** 连接——命中任意一个即入选。

    **为什么是 OR（这里踩过坑，别再改回空格）**：中文长问句切出的 bigram 里，
    「是什」「么原」这类跨词组合在任何文档里都不存在。AND 要求全部命中，
    等于让每一句正常提问都必然 0 命中——而且不报错、不告警、测试全绿。
    C# 的 `BigramTokenizer` 用 OR，评测基线（HitRate 0.906）也是 OR 的成绩，
    两边必须是同一个语义。

    **代价要认**：OR 很宽松，几乎必有命中。「拒不拒」不能指望召回层，
    只能靠判定层——F3 的 G3 负控 8 条里 7 条能召回内容，根因就在这里。

    引号双写转义：token 里出现 `"` 会让 MATCH 串语法错（FTS5 约定）。
    """
    tokens = tokenize(text)
    if not tokens:
        return ""
    escaped = [token.replace('"', '""') for token in tokens]
    return " OR ".join(f'"{token}"' for token in escaped)
