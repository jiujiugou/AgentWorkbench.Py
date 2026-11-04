"""中文分词的 re-export 薄壳。

实现下沉到 `core/tokenizer.py`：引用校验（guard/citation）的字符覆盖率
必须用与索引同一套分词——两套规则会让「检索判定相关」和「引用判定
对得上」给出互相矛盾的结论（C# CitationVerifier 同理由）。guard 与 rag
同层禁互引，共享词汇只能放 core。

已有调用方（indexer / worker / 评测脚本）的 import 路径不变。
"""

from agentworkbench.core.tokenizer import single_han_chars as single_han_chars
from agentworkbench.core.tokenizer import to_index_text as to_index_text
from agentworkbench.core.tokenizer import to_match_query as to_match_query
from agentworkbench.core.tokenizer import tokenize as tokenize

__all__ = ["single_han_chars", "to_index_text", "to_match_query", "tokenize"]
