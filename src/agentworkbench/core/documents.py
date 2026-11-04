"""文档记录：文档、页、片段，以及处理状态与失败码。

「处理中 / 成功 / 失败」三态必须对用户可见——失败被伪装成可用，
用户会以为检索坏了，去查阈值，方向全错。

来源：C# `Domain/Documents/DocumentRecords.cs`、`DocumentErrorCodes.cs`
服务于：REQ-05 / REQ-06 / REQ-10（F2）
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DocStatus(StrEnum):
    """一份资料从入库到消失经过的状态。

    `PARSED` 只表示"全文已抽出"，**不表示可检索**：索引要写三处（块、全文、向量），
    任一缺失都会让列表显示"可检索"而检索全空。所以"可检索"的判据永远是计数，
    不是这一列（见 `db/consistency.py`）。
    """

    PENDING = "pending"
    PARSING = "parsing"
    PARSED = "parsed"
    FAILED = "failed"
    DELETE_PENDING = "delete_pending"


ALLOWED_TRANSITIONS: dict[DocStatus, frozenset[DocStatus]] = {
    DocStatus.PENDING: frozenset({DocStatus.PARSING, DocStatus.FAILED, DocStatus.DELETE_PENDING}),
    DocStatus.PARSING: frozenset({DocStatus.PARSED, DocStatus.FAILED, DocStatus.DELETE_PENDING}),
    # PARSED 还能退回 FAILED：全文写成功后索引永久失败，就是"块在、检索不到"的半成品。
    DocStatus.PARSED: frozenset({DocStatus.FAILED, DocStatus.DELETE_PENDING}),
    # FAILED → PENDING 是唯一允许的倒退，代表重试。
    DocStatus.FAILED: frozenset({DocStatus.PENDING, DocStatus.DELETE_PENDING}),
    # 删除中之后行会被删掉，没有"已删除"这个状态；回到任何可用状态都等于撤销被吞。
    DocStatus.DELETE_PENDING: frozenset(),
}

TERMINAL_STATUSES: frozenset[DocStatus] = frozenset(
    status for status, targets in ALLOWED_TRANSITIONS.items() if not targets
)


def require_transition(src: DocStatus, dst: DocStatus) -> None:
    """非法迁移当场抛错，不做"尽力而为"的兜底。

    静默接受非法迁移的后果是记录自相矛盾（失败之后又出现成功），
    而自相矛盾的记录比没有记录更糟——它会被当成证据。
    """
    if dst not in ALLOWED_TRANSITIONS[src]:
        raise ValueError(f"非法状态迁移：{src} → {dst}")


class DocumentErrorCode(StrEnum):
    """失败原因。对外是契约：只能加，不能改已有语义。"""

    NO_TEXT_LAYER = "no_text_layer"
    ENCRYPTED = "encrypted"
    TOO_MANY_PAGES = "too_many_pages"
    CORRUPT = "corrupt"
    EXTRACT_ERROR = "extract_error"
    UNSUPPORTED_FORMAT = "unsupported_format"
    CHUNK_EMPTY = "chunk_empty"
    INDEX_PERMANENT = "index_permanent"
    INDEX_TRANSIENT = "index_transient"
    PARSE_STALLED = "parse_stalled"


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    """切块器的产物，也是索引器的输入。

    两边共用同一个结构，是为了让"切块器换了"这件事只影响一个模块：
    只要它还能吐出 `ChunkDraft`，索引与检索都不用动。

    `seq` 是**块在该文档内的序号**，不是全局 id：题库的 gold 写成
    `(文件名, seq)`，不写 `chunks.id`——后者自增，重解析就全变且不报错。
    """

    seq: int
    page_from: int
    page_to: int
    content: str
    heading: str | None = None


@dataclass(frozen=True, slots=True)
class DocErrorSpec:
    """`retryable` 说的是"再试一次会不会变好"，不是"系统会不会自动重试"。"""

    retryable: bool
    user_hint: str


DOC_ERROR_SPECS: dict[DocumentErrorCode, DocErrorSpec] = {
    DocumentErrorCode.NO_TEXT_LAYER: DocErrorSpec(
        retryable=False, user_hint="这份文件里没有可读的文字，可能是扫描件。"
    ),
    DocumentErrorCode.ENCRYPTED: DocErrorSpec(
        retryable=False, user_hint="这份文件有密码保护，请解除后重新上传。"
    ),
    DocumentErrorCode.TOO_MANY_PAGES: DocErrorSpec(
        retryable=False, user_hint="页数超出上限，请拆分后再上传。"
    ),
    DocumentErrorCode.CORRUPT: DocErrorSpec(
        retryable=False, user_hint="文件已损坏，请从原始来源重新导出。"
    ),
    DocumentErrorCode.EXTRACT_ERROR: DocErrorSpec(
        retryable=True, user_hint="抽取文字时出错，正在自动重试。"
    ),
    DocumentErrorCode.UNSUPPORTED_FORMAT: DocErrorSpec(
        retryable=False, user_hint="暂不支持这种格式，请转成 PDF / Word / Excel 再上传。"
    ),
    DocumentErrorCode.CHUNK_EMPTY: DocErrorSpec(
        retryable=False, user_hint="文字太少，切不出可检索的片段。"
    ),
    DocumentErrorCode.INDEX_PERMANENT: DocErrorSpec(
        retryable=False, user_hint="建立索引时出错，请联系管理员。"
    ),
    DocumentErrorCode.INDEX_TRANSIENT: DocErrorSpec(
        retryable=True, user_hint="建立索引暂时失败，正在自动重试。"
    ),
    DocumentErrorCode.PARSE_STALLED: DocErrorSpec(
        retryable=True, user_hint="处理超时，正在自动重试。"
    ),
}


class IndexState(StrEnum):
    """索引覆盖度。**API 契约，前端按它决定按钮是否可用。**

    和 `DocStatus` 不是一回事：`DocStatus` 记的是流程走到哪，
    `IndexState` 记的是"检索到底能不能覆盖这篇"。两者会打架——
    状态 `parsed` 而块数为 0 的文档，就是这个产品最坑的一种坏数据。
    """

    FULL = "full"
    TEXT_ONLY = "text_only"
    PARTIAL = "partial"
    EMPTY = "empty"
    PENDING = "pending"
    FAILED = "failed"


def describe_index_state(status: DocStatus, chunk_count: int, vector_count: int) -> IndexState:
    """由「块数 vs 向量数」判定覆盖度，**不读状态列当判据**。

    判据取自实际数据的好处：历史数据的向量缺口无需回填即可现形，
    也不会出现"状态说已索引、数据却是空的"这种字段与事实打架的情形。

    状态只用来区分"还没到时候"（pending）与"已经废了"（failed）这两种
    数据说明不了的情况——它们是流程信息，不是覆盖度信息。
    """
    if status is DocStatus.FAILED:
        return IndexState.FAILED
    if status is not DocStatus.PARSED:
        return IndexState.PENDING
    if chunk_count == 0:
        return IndexState.EMPTY
    if vector_count >= chunk_count:
        return IndexState.FULL
    return IndexState.TEXT_ONLY if vector_count == 0 else IndexState.PARTIAL
