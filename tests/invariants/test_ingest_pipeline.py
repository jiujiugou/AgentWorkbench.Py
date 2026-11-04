"""不变量：上传 → 解析 → 索引这条链，每一步的失败都不能被藏起来。

被改坏时用户会看到：传了文件列表里没有；或者列表显示"可检索"、搜什么都空，
于是去调召回和阈值——方向全错，而且没有任何告警。

这一组覆盖 F2 V-1 / V-2 / V-4 / V-6 / V-10，以及端点"返回时状态必须是 pending"。
V-5（失败文档不进检索）在 `test_no_text_layer_*` 里覆盖；V-3 的判定另在
`test_parse_failure.py`。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentworkbench.core.documents import DocStatus, DocumentErrorCode
from agentworkbench.core.vectors import EMBEDDING_DIM
from agentworkbench.db import engine
from agentworkbench.db.repositories import documents as repo
from agentworkbench.ingest import pipeline, worker
from agentworkbench.ingest.queue import InMemoryQueue, recover_pending
from agentworkbench.ingest.storage import LocalStorage
from agentworkbench.ingest.upload import UploadRejectCode
from agentworkbench.rag.indexer import count_index, search_fts

NOW = "2026-11-02T00:00:00"
MAX_BYTES = 10 * 1024 * 1024
TENANT = "t1"


@dataclass
class Env:
    conn: sqlite3.Connection
    storage: LocalStorage
    queue: InMemoryQueue


@pytest.fixture
def env(tmp_path: Path) -> Env:
    # 内存库：落盘 WAL 在 Windows 上每条要 1 秒多，10 条就是 15 秒。慢测试等于没测试。
    conn = engine.connect(":memory:")
    engine.create_schema(conn)
    storage = LocalStorage(tmp_path / "files")
    storage.root.mkdir(parents=True, exist_ok=True)
    return Env(conn=conn, storage=storage, queue=InMemoryQueue())


def _document(*pages: str) -> bytes:
    """测试用"PDF"：页之间用换页符分隔。解析端口由测试提供，不涉及真 PDF。"""
    return "\f".join(pages).encode("utf-8")


def _parse(data: bytes, suffix: str) -> list[str]:
    _ = suffix  # 端口签名带后缀（分派依据）；桩不分派，测试数据是纯文本
    return data.decode("utf-8").split("\f")


def _embed(texts: list[str]) -> list[list[float]]:
    """确定性假向量。本域要验的是行数与对齐，不是语义相似度。"""
    vectors: list[list[float]] = []
    for text in texts:
        seed = sum(ord(char) for char in text) % 97
        vector = [((seed + index) % 89) / 89.0 for index in range(EMBEDDING_DIM)]
        vectors.append(vector)
    return vectors


def _upload(
    env: Env, data: bytes, *, filename: str = "sample.pdf", doc_id: str = "doc-1"
) -> pipeline.IngestResult:
    return pipeline.ingest(
        env.conn,
        storage=env.storage,
        queue=env.queue,
        tenant_id=TENANT,
        filename=filename,
        data=data,
        now=NOW,
        doc_id=doc_id,
        max_bytes=MAX_BYTES,
    )


def _status(env: Env, doc_id: str) -> str:
    row = repo.get(env.conn, doc_id, tenant_id=TENANT)
    assert row is not None
    return str(row["status"])


def _run_worker(env: Env) -> None:
    worker.drain(env.conn, env.queue, storage=env.storage, parse=_parse, embed=_embed, now=NOW)


# ── V-1 上传 ────────────────────────────────────────────────────────


def test_upload_returns_pending_not_parsed(env: Env) -> None:
    """上传返回时**只承诺 pending**。

    同步解析完再返回也能让后面几条测试变绿，但那样断连容忍、状态可见、
    失败分类全废——所以这一条先钉住。
    """
    result = _upload(env, _document("第一页。"))

    assert result.accepted and result.created
    assert result.document_id is not None
    assert _status(env, result.document_id) == DocStatus.PENDING
    assert len(env.queue) == 1


def test_duplicate_upload_reuses_row_and_one_file_on_disk(env: Env) -> None:
    data = _document("第一页。")

    first = _upload(env, data, doc_id="doc-1")
    second = _upload(env, data, doc_id="doc-2")

    assert second.created is False
    assert second.document_id == first.document_id
    assert env.conn.execute("select count(*) from documents").fetchone()[0] == 1
    assert env.storage.count() == 1, "同一份内容不许在盘上留两份"


def test_unique_index_is_what_actually_dedupes(env: Env) -> None:
    """应用层"先查再插"挡不住并发；真正的约束是数据库上的唯一索引。

    负控：把索引删掉，同样的两次插入必须都能成功——证明这条不变量确实由它守着，
    而不是由上面那个 `find_by_digest` 顺手挡住的。
    """
    kwargs = {
        "tenant_id": TENANT,
        "file_name": "sample.pdf",
        "stored_path": "t1/aa/aa.pdf",
        "sha256": "deadbeef",
        "size_bytes": 10,
        "now": NOW,
    }
    repo.create(env.conn, doc_id="doc-1", **kwargs)  # type: ignore[arg-type]
    env.conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        repo.create(env.conn, doc_id="doc-2", **kwargs)  # type: ignore[arg-type]
    env.conn.rollback()

    env.conn.execute("drop index ux_documents_tenant_sha256")
    repo.create(env.conn, doc_id="doc-2", **kwargs)  # type: ignore[arg-type]
    env.conn.commit()
    assert env.conn.execute("select count(*) from documents").fetchone()[0] == 2


# ── V-2 拒收 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("filename", "data", "expected"),
    [
        (None, b"x", UploadRejectCode.NO_FILE),
        ("empty.pdf", b"", UploadRejectCode.FILE_EMPTY),
        ("note.xls", b"x", UploadRejectCode.FILE_TYPE_UNSUPPORTED),
    ],
)
def test_rejected_upload_leaves_nothing(
    env: Env, filename: str | None, data: bytes, expected: UploadRejectCode
) -> None:
    """拒收的三个"没变"：库没行、盘没文件、队列没任务。

    只断言"返回 400"的实现能过正控，过不了这条。
    """
    result = _upload(env, data, filename=filename)  # type: ignore[arg-type]

    assert result.reject is expected
    assert result.document_id is None
    assert env.conn.execute("select count(*) from documents").fetchone()[0] == 0
    assert env.storage.count() == 0
    assert len(env.queue) == 0


# ── V-4 / V-10 处理完成与索引对齐 ────────────────────────────────────


def test_worker_takes_pending_to_parsed_and_searchable(env: Env) -> None:
    """端到端那一条：`parsed` 必须等于"搜得到"。

    第三句最值钱——它同时防住"写了块没写全文""写了块没写向量"两种不一致，
    那两种用别的办法测都要手工造数据。
    """
    result = _upload(env, _document("第一页内容。", "合同金额为十二万元整。"))
    assert result.document_id is not None

    _run_worker(env)

    assert _status(env, result.document_id) == DocStatus.PARSED
    counts = count_index(env.conn, result.document_id)
    assert counts.aligned, f"三处计数不齐：{counts}"

    hits = search_fts(env.conn, "合同金额", tenant_id=TENANT)
    assert [hit.document_id for hit in hits] == [result.document_id]


def test_no_text_layer_fails_and_is_not_searchable(env: Env) -> None:
    """V-3 / V-5：失败要单独命名，且**不能继续提供依据**。"""
    result = _upload(env, _document("  \n  ", "\n\n"))
    assert result.document_id is not None

    _run_worker(env)

    row = repo.get(env.conn, result.document_id, tenant_id=TENANT)
    assert row is not None
    assert str(row["status"]) == DocStatus.FAILED
    assert str(row["error_code"]) == DocumentErrorCode.NO_TEXT_LAYER
    assert not count_index(env.conn, result.document_id).chunks
    assert search_fts(env.conn, "第一页内容", tenant_id=TENANT) == []


def test_index_misalignment_is_not_reported_as_parsed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写成功了不等于写对了：块数与向量数不等时，`parsed` 就是个谎。

    这条分支在正常路径下永远走不到——正是 C# 版 `SufficiencyGate` 那种
    "函数通、管道通、运行时一次不执行"的形状，所以必须显式注入才能证明它活着。
    """
    result = _upload(env, _document("第一页内容。"))
    assert result.document_id is not None

    import agentworkbench.ingest.worker as worker_module

    original = count_index

    def broken(conn: sqlite3.Connection, document_id: str) -> object:
        counts = original(conn, document_id)
        return type(counts)(chunks=counts.chunks, fts=counts.fts, vectors=0)

    monkeypatch.setattr(worker_module, "count_index", broken)
    _run_worker(env)

    row = repo.get(env.conn, result.document_id, tenant_id=TENANT)
    assert row is not None
    assert str(row["status"]) == DocStatus.FAILED
    assert str(row["error_code"]) == DocumentErrorCode.INDEX_PERMANENT
    assert count_index(env.conn, result.document_id).chunks == 0, "失败必须把索引清干净"


# ── V-6 重启可续 ────────────────────────────────────────────────────


def test_recover_pending_requeues_both_pending_and_parsing(env: Env) -> None:
    """重启后"处理中"不能变成永久态。

    `parsing` 比 `pending` 更危险：它看起来像"正在干活"，所以更要重扫。
    """
    for doc_id, status in (("doc-p", DocStatus.PENDING), ("doc-q", DocStatus.PARSING)):
        repo.create(
            env.conn,
            doc_id=doc_id,
            tenant_id=TENANT,
            file_name=f"{doc_id}.pdf",
            stored_path=f"t1/{doc_id}.pdf",
            sha256=doc_id,
            size_bytes=1,
            now=NOW,
        )
        if status is DocStatus.PARSING:
            repo.set_status(env.conn, doc_id, DocStatus.PARSING, now=NOW)
    env.conn.commit()

    recovered = recover_pending(env.conn, env.queue)

    assert recovered == 2
    assert sorted(env.queue.drain()) == ["doc-p", "doc-q"]
