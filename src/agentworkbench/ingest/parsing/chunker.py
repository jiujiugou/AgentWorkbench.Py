"""切块：把页文本切成可检索、可引用的块。

**当前实现 = LlamaIndex `SentenceSplitter`（T-002 接入，T-001 定案）**：
800 字上限、120 字重叠、按字符计数。接缝率 7.52%，是 11 组候选里唯一
追平 C# 基线（7.29%）的方案；被替换掉的自写版实测 11.66%——它没有重叠，
而 C# 的 120 字重叠让接缝两侧共享整行（判据「末行 == 首行」不算切断），
那一半优势是参数给的，不是算法给的。完整数据：`tasks/T-001-切块器选型.md`。

换实现后块边界全变（439 → 532 块，`seq` 重排），评测题库 gold 已按
内容对齐重标（`evals/self/questions-v2.json`，工具 `evals/relabel_gold.py`）。

输入输出契约不变：页文本进（页码 1-based）、`ChunkDraft` 出（`seq` 全文档
连续）。worker 与索引层不感知这次替换。

来源：C# `Documents/Parsing/Chunker.cs`（同参数：800 flush / 120 重叠）
服务于：REQ-07（可用二）
"""

from __future__ import annotations

from llama_index.core import Document
from llama_index.core.node_parser import SentenceSplitter

from agentworkbench.core.documents import ChunkDraft

# 与 C# 版同参数：800 字 flush、120 字重叠。tokenizer 按字符计数——
# 这批文档全是中文，BPE token 窗口会让"800"变成约 500 汉字，口径就不齐了。
_CHARS_PER_CHUNK = 800
_OVERLAP_CHARS = 120


def _chars(text: str) -> list[str]:
    """字符计数：chunk_size 的口径是字符，不是 BPE token。"""
    return list(text)


_SPLITTER = SentenceSplitter(
    chunk_size=_CHARS_PER_CHUNK,
    chunk_overlap=_OVERLAP_CHARS,
    tokenizer=_chars,
)


def chunk_pages(pages: list[str]) -> list[ChunkDraft]:
    """页文本 → 块列表。`seq` 在整个文档内连续，页码 1-based。

    空页不算失败：整篇都没文字是 `no_text_layer`（在解析阶段判），
    有文字但切不出块是 `chunk_empty`（在 worker 里判）。两者分开，因为用户动作不同。
    """
    drafts: list[ChunkDraft] = []
    seq = 0
    for page_no, text in enumerate(pages, start=1):
        body = text.strip()
        if not body:
            continue
        for piece in _split(body):
            content = piece.strip()
            if not content:
                continue
            drafts.append(ChunkDraft(seq=seq, page_from=page_no, page_to=page_no, content=content))
            seq += 1
    return drafts


def _split(text: str) -> list[str]:
    """一页 → 块列表。SentenceSplitter 按句累积，重叠 120 字。"""
    nodes = _SPLITTER.get_nodes_from_documents([Document(text=text)])
    return [node.get_content() for node in nodes]
