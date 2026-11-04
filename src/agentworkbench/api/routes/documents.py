"""知识资料路由：`/api/documents`。

这一层只做协议转换：**出现业务判断说明它漏上来了**。唯一的例外是
"这个 id 存不存在"——404 是协议的一部分，不是业务。

返回形状对齐 C# 版与前端 `web/src/api/documents.ts`：
- 列表是**裸数组**（前端注释原话：无 items 包装）
- 错误体是 `{code, message, retryable}`（前端 parseError 只认 message）
- 上传成功是 **202**（不是 201）：202 的语义是"已受理、处理未完"，
  正是"返回时状态必须是 pending"的 HTTP 表达

auto_process=False 时（V-4 的观测模式）上传只入队；处理由测试线程接管。
BackgroundTasks 在 TestClient 下会阻塞到响应返回之后才跑完——
所以慢解析器注入必须绕开它，不能靠它造"卡在 parsing"。

来源：C# `Webapi/Controllers/DocumentsController.cs`
服务于：REQ-05 / REQ-06 / REQ-10
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from typing import Annotated, cast
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from agentworkbench.api.deps import Deps, utc_now
from agentworkbench.api.errors import UploadRejected, not_found, validation_failed
from agentworkbench.api.identity_context import owner_of
from agentworkbench.api.schemas import ChunkDto, DocumentDto, ErrorDto, PageDto
from agentworkbench.core.documents import DocStatus
from agentworkbench.db.repositories import documents as repo
from agentworkbench.ingest import pipeline, worker
from agentworkbench.ingest.delete_ports import SqliteDeletePorts
from agentworkbench.ingest.deletion import DeleteOutcome, delete_document

router = APIRouter(prefix="/api/documents")

DOC_MISSING = "文档不存在。"
"""404 的**唯一**文案：不回显 `doc_id`。

回显看起来更友好，但它让「别人的 id」与「随机的 id」拿到两个不同的响应，
而判据 `I-V2` 要求这两者剥掉 request_id 后**逐字段相同**。用户传进来的
那个 id 本来就在他自己的请求里，回显不多给任何信息——真正的代价是
响应从此携带「这个 id 存在」的证明。
"""


def _deps(request: Request) -> Deps:
    """依赖都在 app.state 上，组合根（create_app）负责装配。"""
    return cast(Deps, request.app.state.deps)


def _drain_runner(deps: Deps) -> Callable[[], None]:
    """ "把队列跑空"的可调用。BackgroundTasks 在响应返回后执行它。
    自己开自己的连接——路由的连接已经关了，worker 不许复用别人的。"""

    def run() -> None:
        with closing(deps.open_conn()) as conn:
            worker.drain(
                conn,
                deps.queue,
                storage=deps.storage,
                parse=deps.parser,
                embed=deps.embedder,
                now=utc_now(),
            )

    return run


@router.post("", status_code=202)
def upload(
    request: Request,
    background: BackgroundTasks,
    file: Annotated[UploadFile | None, None] = None,
) -> DocumentDto:
    """收一份文件。返回时只承诺 pending——**同步解析完再返回是错的**：
    那会让断连容忍、状态可见、失败分类全废。"""
    deps = _deps(request)
    data = file.file.read() if file is not None else b""
    filename = file.filename if file is not None else None
    with closing(deps.open_conn()) as conn:
        # 归属来自登录身份，不再是 `settings.tenant_id` 这个单租户常量。
        # 这一行是 I-V2 的「上传」那一路：常量写在这里时，任何登录用户
        # 传的文件都落在同一个团队，而两个团队各自看都正常。
        owner = owner_of(conn, request)
        result = pipeline.ingest(
            conn,
            storage=deps.storage,
            queue=deps.queue,
            tenant_id=owner.tenant_id,
            filename=filename,
            data=data,
            now=utc_now(),
            doc_id=f"doc-{uuid4().hex[:12]}",
            max_bytes=deps.settings.max_upload_bytes,
            allowed_suffixes=deps.settings.suffix_set,
        )
        if result.reject is not None:
            raise UploadRejected(result.reject)
        doc_id = cast(str, result.document_id)  # reject 为空时必有 id（pipeline 契约）
        row = repo.get(conn, doc_id, tenant_id=owner.tenant_id)
        # 刚插入的行必须读得到；cast 只做类型收窄，不做运行时检查。
        chunks, vectors = repo.count_chunks_and_vectors(conn, doc_id)
        dto = DocumentDto.from_row(
            cast("sqlite3.Row", row), chunk_count=chunks, vector_count=vectors
        )
    if deps.auto_process:
        background.add_task(_drain_runner(deps))
    return dto


@router.get("")
def list_documents(request: Request) -> list[DocumentDto]:
    """裸数组。新的在前——刚传的文件要立刻看见。"""
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        tenant_id = owner_of(conn, request).tenant_id
        rows = repo.list_all(conn, tenant_id=tenant_id)
        out: list[DocumentDto] = []
        for row in rows:
            chunks, vectors = repo.count_chunks_and_vectors(conn, str(row["id"]))
            out.append(DocumentDto.from_row(row, chunk_count=chunks, vector_count=vectors))
        return out


@router.get("/{doc_id}")
def get_document(request: Request, doc_id: str) -> DocumentDto:
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        tenant_id = owner_of(conn, request).tenant_id
        row = repo.get(conn, doc_id, tenant_id=tenant_id)
        if row is None:
            raise not_found(DOC_MISSING)
        chunks, vectors = repo.count_chunks_and_vectors(conn, doc_id)
        return DocumentDto.from_row(row, chunk_count=chunks, vector_count=vectors)


@router.get("/{doc_id}/pages")
def get_pages(request: Request, doc_id: str) -> list[PageDto]:
    with closing(_deps(request).open_conn()) as conn:
        tenant_id = owner_of(conn, request).tenant_id
        if repo.get(conn, doc_id, tenant_id=tenant_id) is None:
            raise not_found(DOC_MISSING)
        return [
            PageDto(page_no=int(row["page_no"]), raw_text=str(row["raw_text"]), cleaned_text=None)
            for row in repo.list_pages(conn, doc_id, tenant_id=tenant_id)
        ]


@router.get("/{doc_id}/chunks")
def get_chunks(request: Request, doc_id: str) -> list[ChunkDto]:
    with closing(_deps(request).open_conn()) as conn:
        tenant_id = owner_of(conn, request).tenant_id
        if repo.get(conn, doc_id, tenant_id=tenant_id) is None:
            raise not_found(DOC_MISSING)
        return [
            ChunkDto(
                seq=int(row["seq"]),
                page_from=int(row["page_from"]),
                page_to=int(row["page_to"]),
                heading=cast("str | None", row["heading"]),
                content=str(row["content"]),
                char_count=int(row["char_count"]),
            )
            for row in repo.list_chunks(conn, doc_id, tenant_id=tenant_id)
        ]


@router.post("/{doc_id}/reparse", status_code=202)
def reparse(request: Request, doc_id: str, background: BackgroundTasks) -> Response:
    """只有 failed 允许重试。parsed → pending 不是状态机里的合法迁移，
    也不是用户语义："重解析一份成功的"应该走删除重传。"""
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        tenant_id = owner_of(conn, request).tenant_id
        row = repo.get(conn, doc_id, tenant_id=tenant_id)
        if row is None:
            raise not_found(DOC_MISSING)
        if DocStatus(str(row["status"])) is not DocStatus.FAILED:
            raise validation_failed("只有失败的文档可以重新解析")
        repo.set_status(conn, doc_id, DocStatus.PENDING, now=utc_now())
        conn.commit()
        deps.queue.enqueue(doc_id)
    if deps.auto_process:
        background.add_task(_drain_runner(deps))
    return Response(status_code=202)


@router.delete("/{doc_id}")
def remove(request: Request, doc_id: str) -> Response:
    """删除补偿的四步编排见 `ingest/deletion.py`；这里只翻译结果码。

    RETRY_LATER 返回 202：删除没完成，记录还在（delete_pending），
    客户端可以原样重试——这是幂等入口，不是错误。"""
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        # 判定先于删除编排：`delete_document` 按 id 删五处表，它不认识团队。
        # 前面这道读是「这一行归不归我」，由它决定要不要进编排——
        # 越权在这里就 404 了，编排一行都不会跑（I-V3 的文档那一路）。
        tenant_id = owner_of(conn, request).tenant_id
        if repo.get(conn, doc_id, tenant_id=tenant_id) is None:
            raise not_found(DOC_MISSING)
        outcome = delete_document(conn, doc_id, SqliteDeletePorts(conn, deps.storage))
        if outcome is DeleteOutcome.ABORTED:
            raise not_found(DOC_MISSING)
        if outcome is DeleteOutcome.RETRY_LATER:
            return JSONResponse(
                status_code=202,
                content=ErrorDto(
                    code="delete_retry_later",
                    message="索引清理未完成，已标记删除中，稍后自动重试。",
                ).model_dump(by_alias=True),
            )
    return Response(status_code=204)
