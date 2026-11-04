"""按后缀分派到解析器。上传白名单的后缀必须在这里全部有主。

**分派按后缀，不嗅探魔数。** docx 与 xlsx 都是 zip，真正的判别在
`[Content_Types].xml` 里——为了区分这两者去解一层 zip，得到的结论与后缀一致，
而代价是多一条"后缀与内容不符"时的第二套归因路径。后缀在上传时已经被白名单
校验过，是可信号；不可信的从来不是它声称的格式，而是内容——内容不对会由
解析器自己抛 `corrupt`，那才是正确的归因层。

**未注册后缀必须抛 `UnsupportedFormat`，绝不回落到任何一个解析器。**
回落的版本会把 `.txt` 报成 `corrupt`——用户看到"文件损坏"，实际是
"这个格式不支持"，他会按错的方向去修文件。

服务于：REQ-05 / 06；判据 V-17（`pyproject.toml`）。
"""

from __future__ import annotations

from collections.abc import Callable

from agentworkbench.core.documents import DocumentErrorCode
from agentworkbench.ingest.parsing.errors import ParseError
from agentworkbench.ingest.parsing.office import parse_docx, parse_xlsx
from agentworkbench.ingest.parsing.pdf import parse_pdf

_PARSERS: dict[str, Callable[[bytes], list[str]]] = {
    ".pdf": parse_pdf,
    ".docx": parse_docx,
    ".xlsx": parse_xlsx,
}


class UnsupportedFormat(Exception):
    """后缀不在分派表里。这只在绕过上传校验时发生（防御，不是业务分支）。"""


def parse_document(data: bytes, suffix: str) -> list[str]:
    """后缀 → 解析器。所有解析器共用同一个输出契约：段的列表。"""
    parser = _PARSERS.get(suffix.strip().lower())
    if parser is None:
        supported = " / ".join(sorted(_PARSERS))
        raise UnsupportedFormat(f"不支持的格式：{suffix}（支持 {supported}）")
    try:
        return parser(data)
    except ParseError:
        raise
    except Exception as exc:
        # 解析器自己没接住的异常兜到这里。归 corrupt 而不是 extract_error：
        # 走到这一步的输入已经过了上传校验，最可能的解释是"内容不是它自称
        # 的格式"，而不是"读某一页时失败了"——两者的用户动作不同。
        raise ParseError(
            DocumentErrorCode.CORRUPT, f"文件内容与格式不符：{type(exc).__name__}"
        ) from exc
