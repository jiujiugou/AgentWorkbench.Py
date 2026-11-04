"""工作线程：把一条 `pending` 推到终态。

    ⑦ 解析 → ⑧ 切块 → ⑨ 写索引三处 → `parsed`

三条判断：

1. **失败必须落到具体的码上，不能一句 `extract_error` 打发。**
   `no_text_layer` 和 `corrupt` 的可重试性相反，混成一个码之后，
   "这个月失败里多少是扫描件"就再也统计不出来——而那是要不要做 OCR 的唯一依据。
2. **失败时要把索引清干净。** 写索引写了一半失败，全文行还在，检索就能命中一份
   `failed` 文档。C# 版正是这样（先 `ReplaceChunks` 再 `IndexDocument`），
   本域 §1.3 ① 就是它。
3. **结束前数一遍三处计数。** 写成功了不等于写对了：块数与向量数不等时，
   `parsed` 就是个谎。宁可标 `index_permanent`，也不能让列表显示"可检索"。

来源：C# `Documents/DocumentParseWorker.cs`
服务于：REQ-06
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Protocol

from agentworkbench.core.documents import DocStatus, DocumentErrorCode
from agentworkbench.core.vectors import EMBEDDING_DIM
from agentworkbench.db.repositories import documents as repo
from agentworkbench.ingest.parsing.chunker import chunk_pages
from agentworkbench.ingest.parsing.dispatch import UnsupportedFormat
from agentworkbench.ingest.parsing.errors import ParseError
from agentworkbench.ingest.parsing.pdf import classify_parse_failure
from agentworkbench.ingest.queue import InMemoryQueue
from agentworkbench.ingest.storage import LocalStorage
from agentworkbench.rag.indexer import clear_index, count_index, write_index


class ParserPort(Protocol):
    """解析器的适配点。只吐「段」，不碰库、不碰切块。

    `suffix` 是分派依据，从 `file_name` 取。**不再校验白名单**——上传时校验过，
    这里重复校验会造出第二个真源：白名单改了这里忘了改，就会出现
    "上传收了、解析报不支持"的中间态，而那个状态在两张表上都查不到原因。
    """

    def __call__(self, data: bytes, suffix: str) -> list[str]: ...


class EmbedderPort(Protocol):
    """向量化。生产接 bge-m3，测试用确定性假向量。

    本域的验证项没有一条需要"向量语义正确"——它们需要的是行数对不对、
    失败时有没有脏行。所以假向量完全够测，而且能离线跑。
    """

    def __call__(self, texts: list[str]) -> list[list[float]]: ...


def process_document(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    storage: LocalStorage,
    parse: ParserPort,
    embed: EmbedderPort,
    now: str,
) -> DocStatus:
    """跑一遍。返回终态；任何分支都保证"终态与实际做成的程度一致"。"""
    row = repo.get_unscoped(conn, document_id)
    if row is None:
        raise KeyError(f"文档不存在：{document_id}")

    status = DocStatus(str(row["status"]))
    if status is DocStatus.DELETE_PENDING:
        # 解析过程中被要求删除：不再推进，让删除补偿接手。
        return status
    repo.set_status(conn, document_id, DocStatus.PARSING, now=now)
    conn.commit()

    try:
        suffix = Path(str(row["file_name"])).suffix
        pages = parse(storage.read(str(row["stored_path"])), suffix)
    except UnsupportedFormat as exc:
        # 后缀绕过了上传校验才会到这里（防御路径）。归因必须对：
        # 这是"格式不支持"，不是"内容损坏"——用户动作是转格式，不是重新导出。
        return _fail(conn, document_id, DocumentErrorCode.UNSUPPORTED_FORMAT, str(exc), now)
    except ParseError as exc:
        # 加密 / 损坏 / 抽取失败由解析器定性——它知道格式内部信息，这里不知道。
        return _fail(conn, document_id, exc.code, exc.detail, now)
    except Exception as exc:
        return _fail(conn, document_id, DocumentErrorCode.EXTRACT_ERROR, str(exc), now)

    failure = classify_parse_failure(pages)
    if failure is not None:
        return _fail(conn, document_id, DocumentErrorCode(failure.value), "无文本层", now)

    repo.write_pages(conn, document_id, pages)
    drafts = chunk_pages(pages)
    if not drafts:
        return _fail(conn, document_id, DocumentErrorCode.CHUNK_EMPTY, "切不出块", now)

    try:
        embeddings = embed([draft.content for draft in drafts])
        if any(len(vector) != EMBEDDING_DIM for vector in embeddings):
            raise ValueError(f"向量维度必须是 {EMBEDDING_DIM}")
        write_index(conn, document_id=document_id, drafts=drafts, embeddings=embeddings, now=now)
    except Exception as exc:
        conn.rollback()
        clear_index(conn, document_id)
        conn.commit()
        return _fail(conn, document_id, DocumentErrorCode.INDEX_PERMANENT, str(exc), now)

    counts = count_index(conn, document_id)
    if not counts.aligned:
        clear_index(conn, document_id)
        conn.commit()
        return _fail(
            conn,
            document_id,
            DocumentErrorCode.INDEX_PERMANENT,
            f"三处计数不齐：{counts.chunks}/{counts.fts}/{counts.vectors}",
            now,
        )

    repo.set_status(
        conn,
        document_id,
        DocStatus.PARSED,
        now=now,
        page_count=len(pages),
        parsed_at=now,
    )
    conn.commit()
    return DocStatus.PARSED


def _fail(
    conn: sqlite3.Connection,
    document_id: str,
    code: DocumentErrorCode,
    detail: str,
    now: str,
) -> DocStatus:
    """落失败。索引必须一起清——`failed` 却可命中的文档会进答案和引用。"""
    clear_index(conn, document_id)
    repo.set_status(
        conn,
        document_id,
        DocStatus.FAILED,
        now=now,
        error_code=str(code),
        error=detail[:500],
    )
    conn.commit()
    return DocStatus.FAILED


def drain(
    conn: sqlite3.Connection,
    queue: InMemoryQueue,
    *,
    storage: LocalStorage,
    parse: ParserPort,
    embed: EmbedderPort,
    now: str,
) -> list[DocStatus]:
    """把队列跑空一次。测试与单进程模式用；生产由 worker 循环调 `process_document`。"""
    outcomes: list[DocStatus] = []
    for document_id in queue.drain():
        outcomes.append(
            process_document(
                conn,
                document_id=document_id,
                storage=storage,
                parse=parse,
                embed=embed,
                now=now,
            )
        )
    return outcomes
