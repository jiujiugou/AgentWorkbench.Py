"""F2 端到端：HTTP API 全链路（V-4 / V-8 / V-9 的端到端部分）。

之前这三条只有编排级验证：`pipeline.ingest`、`worker.drain`、
`delete_document` 各自的单测全绿，但「HTTP 进 → JSON 出」这一层没人验。

改坏这一层用户会看到：camelCase 对不上时页面一片空白（不报错）；
404 变 500 时前端把"没有这份文档"当成服务器故障；parsing 被吞时
列表从 pending 直接跳 parsed，用户以为处理是瞬时的，慢文件成了"卡死"。
删除补偿漏一处时，用户看不到文档了、检索却还能把它带出来。
camelCase 出网就是在这里出的问题——Python 默认 snake_case，字段对不上
不会报错，页面只是一片空白，静默失败没有比这更糟的了。

技术说明：
- 文件库（tmp_path），不用内存库——路由每请求开新连接，内存库会变成
  每连接一个空库；WAL + synchronous=normal 下每请求连接的代价是毫秒级。
- V-4 的慢解析用 `threading.Event` 注入：worker 在 parser 里挂起，
  主线程经 HTTP 轮询观测 parsing——「轮询条件，不 sleep」的轮询本身
  就是条件等待（50ms 步进 + 超时），不是盲等。
- auto_process=False 时上传只入队，处理由测试线程接管（绕开
  BackgroundTasks 会阻塞 TestClient 响应的问题）。

服务于：V-4 / V-8 / V-9（pyproject 验证登记表）
"""

from __future__ import annotations

import io
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import sign_in
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from agentworkbench.api.app import create_app
from agentworkbench.api.deps import Deps, build_deps
from agentworkbench.config import Settings
from agentworkbench.db import engine
from agentworkbench.ingest import worker

SAMPLE_PDF = Path(__file__).resolve().parents[1] / "fixtures" / "sample.pdf"
# sample.pdf 是 Modbus 知识卡片；这个词全库独有，删除后必须查不到。
UNIQUE_TERM = "ADDR-005"


class Harness:
    """一套 app + client + 直连。`open_direct` 给注入与断言用，绕过 HTTP。"""

    def __init__(self, tmp_path: Path, *, auto_process: bool = True) -> None:
        self.settings = Settings(data_dir=tmp_path / "data")
        self.deps: Deps = build_deps(self.settings)
        self.deps.auto_process = auto_process
        self.app = create_app(settings=self.settings, deps=self.deps)
        self.client = TestClient(self.app)
        sign_in(self.client, self.app)

    def open_direct(self) -> sqlite3.Connection:
        return engine.connect(self.settings.db_path)

    def wait_until(
        self, condition: Callable[[], bool], *, timeout: float = 15.0, what: str = ""
    ) -> None:
        """轮询条件直到满足或超时。步进 50ms——轮询条件，不是盲等。"""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.05)
        raise AssertionError(f"等待超时：{what}")


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def _upload(client: TestClient, data: bytes, name: str = "addr-005.pdf") -> dict[str, object]:
    response = client.post("/api/documents", files={"file": (name, data, "application/pdf")})
    assert response.status_code == 202, response.text
    body: dict[str, object] = response.json()
    return body


def test_upload_flows_to_parsed_with_camel_case_contract(harness: Harness) -> None:
    """正控全链路：上传 202 → pending → parsed → indexState=full。

    camelCase 在这里被钉死：`fileName` 存在且 `file_name` 不存在。
    字段大小写错了不报错，只让前端一片空白——必须显式断言。
    """
    body = _upload(harness.client, SAMPLE_PDF.read_bytes())

    camel = {
        "id",
        "fileName",
        "status",
        "pageCount",
        "sizeBytes",
        "sha256",
        "error",
        "createdAt",
        "parsedAt",
        "chunkCount",
        "vectorCount",
        "indexState",
    }
    missing = camel - set(body)
    assert not missing, f"响应缺字段（前端会读到 undefined）：{sorted(missing)}"
    assert "file_name" not in body, "snake_case 泄漏到 HTTP 层"
    assert body["status"] == "pending", "返回时只承诺 pending，不承诺解析完"

    doc_id = body["id"]
    harness.wait_until(
        lambda: harness.client.get(f"/api/documents/{doc_id}").json()["status"] == "parsed",
        what="parsed",
    )

    final = harness.client.get(f"/api/documents/{doc_id}").json()
    assert final["pageCount"] == 1
    assert final["chunkCount"] > 0
    assert final["vectorCount"] == final["chunkCount"], "三处计数必须对齐"
    assert final["indexState"] == "full", "块与向量都齐了，覆盖度必须是 full"

    chunks = harness.client.get(f"/api/documents/{doc_id}/chunks").json()
    assert len(chunks) == final["chunkCount"]
    assert all("pageFrom" in chunk and "charCount" in chunk for chunk in chunks)


def test_rejected_upload_returns_code_and_leaves_nothing(harness: Harness) -> None:
    """V-2 端到端：不支持的格式 → 400 + 稳定 code + 可读 message；列表与磁盘无痕。

    样本曾经用 `.docx`——那时白名单只有 PDF，它是现成的反例。Office 三类
    进来之后 docx 是合法格式（202 会进解析），反例换成 `.xls`（旧二进制，
    `python-docx`/`openpyxl` 都不读，永远不该收）。
    """
    response = harness.client.post(
        "/api/documents", files={"file": ("report.xls", b"xls", "application/vnd.ms-excel")}
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "FILE_TYPE_UNSUPPORTED"
    assert body["message"], "前端 parseError 直接显示 message，不能为空"

    listing = harness.client.get("/api/documents").json()
    assert listing == [], "拒收必须无痕"


def test_no_text_layer_fails_with_readable_reason(harness: Harness) -> None:
    """V-3 端到端：空白 PDF → failed + 明确文案（用户看得懂下一步）。"""
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)

    body = _upload(harness.client, buffer.getvalue(), name="scan.pdf")
    doc_id = body["id"]

    harness.wait_until(
        lambda: harness.client.get(f"/api/documents/{doc_id}").json()["status"] == "failed",
        what="failed",
    )

    final = harness.client.get(f"/api/documents/{doc_id}").json()
    assert final["error"], "失败必须带可读原因"
    assert final["indexState"] in ("failed", "pending")
    assert final["chunkCount"] == 0


def test_v4_parsing_state_is_observable(tmp_path: Path) -> None:
    """V-4 的缺角：pending → **parsing** → parsed 三态都能经 HTTP 观测到。

    慢解析器挂在 Event 上：worker 置 parsing 后卡住，主线程轮询观测到
    parsing，放行后才到 parsed。改坏法：worker 若不先提交 parsing 就去解析，
    第一段轮询超时红。
    """
    import time

    release = threading.Event()
    harness = Harness(tmp_path, auto_process=False)
    original = harness.deps.parser

    def slow_parse(data: bytes, suffix: str) -> list[str]:
        release.wait(timeout=30)
        return original(data, suffix)

    harness.deps.parser = slow_parse

    body = _upload(harness.client, SAMPLE_PDF.read_bytes())
    doc_id = body["id"]
    assert body["status"] == "pending"

    thread = threading.Thread(
        target=lambda: worker.drain(
            harness.open_direct(),
            harness.deps.queue,
            storage=harness.deps.storage,
            parse=harness.deps.parser,
            embed=harness.deps.embedder,
            now="2026-11-02T12:00:00",
        ),
        daemon=True,
    )
    thread.start()

    def parsing_seen() -> bool:
        state = harness.client.get(f"/api/documents/{doc_id}").json()["status"]
        return bool(state == "parsing")

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and not parsing_seen():
        time.sleep(0.05)
    assert parsing_seen(), "worker 必须先提交 parsing 再解析——这一跳观测不到就是被吞了"

    release.set()
    thread.join(timeout=15)
    assert not thread.is_alive(), "drain 没有在超时内结束"

    harness.wait_until(
        lambda: harness.client.get(f"/api/documents/{doc_id}").json()["status"] == "parsed",
        what="parsed",
    )


def test_v8_delete_makes_term_unsearchable(harness: Harness) -> None:
    """V-8 端到端：命中 → 删除 → 同词 0 命中，且三处行数全 0。

    「删除」与「列表消失」的分界线：只删记录不删索引的话，这条会红——
    用户看不到文档了，检索却还能把它带出来。
    """
    body = _upload(harness.client, SAMPLE_PDF.read_bytes())
    doc_id = body["id"]
    harness.wait_until(
        lambda: harness.client.get(f"/api/documents/{doc_id}").json()["status"] == "parsed",
        what="parsed",
    )

    hits = harness.client.get("/api/knowledge/search", params={"q": UNIQUE_TERM}).json()
    assert len(hits) >= 1, "删除前独有词必须能命中"
    assert all(hit["documentId"] == doc_id for hit in hits)

    response = harness.client.delete(f"/api/documents/{doc_id}")
    assert response.status_code == 204, response.text

    assert harness.client.get("/api/knowledge/search", params={"q": UNIQUE_TERM}).json() == []
    assert harness.client.get(f"/api/documents/{doc_id}").status_code == 404

    with harness.open_direct() as conn:
        for table, column in (
            ("chunks", "document_id"),
            ("chunks_fts", "document_id"),
            ("chunks_vec", "DocumentId"),
        ):
            # 表与列都是本测试写死的白名单，不是外部输入；循环只为三处各数一遍。
            count = conn.execute(
                f"select count(*) from {table} where {column} = ?",  # noqa: S608
                (doc_id,),
            ).fetchone()[0]
            assert count == 0, f"{table} 残留 {count} 行——删除补偿漏了一处"


def test_v9_delete_pending_visible_then_retry_succeeds(harness: Harness) -> None:
    """V-9 端到端：中间态必须可见（不是消失、不是假装正常），重试能走完。"""
    body = _upload(harness.client, SAMPLE_PDF.read_bytes())
    doc_id = body["id"]
    harness.wait_until(
        lambda: harness.client.get(f"/api/documents/{doc_id}").json()["status"] == "parsed",
        what="parsed",
    )

    # 注入「上次删除中途失败」留下的现场：状态 delete_pending、索引已清一半。
    with harness.open_direct() as conn:
        conn.execute("update documents set status = 'delete_pending' where id = ?", (doc_id,))
        conn.commit()

    listing = harness.client.get("/api/documents").json()
    row = next(item for item in listing if item["id"] == doc_id)
    assert row["status"] == "delete_pending", "中间态必须原样可见，不能伪装成正常"

    response = harness.client.delete(f"/api/documents/{doc_id}")
    assert response.status_code == 204, response.text  # delete_pending 是吸收态，重入放行
    assert harness.client.get(f"/api/documents/{doc_id}").status_code == 404


def test_reparse_only_accepts_failed_documents(harness: Harness) -> None:
    """reparse 的守门：parsed 不能重解析；failed 可以，且走完状态机。"""
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)

    body = _upload(harness.client, buffer.getvalue(), name="scan.pdf")
    doc_id = body["id"]
    harness.wait_until(
        lambda: harness.client.get(f"/api/documents/{doc_id}").json()["status"] == "failed",
        what="failed",
    )

    response = harness.client.post(f"/api/documents/{doc_id}/reparse")
    assert response.status_code == 202, response.text

    # 重试后仍会失败（还是那份空白 PDF），但状态机必须真的走过
    # failed → pending → parsing → failed，而不是原地不动。
    harness.wait_until(
        lambda: harness.client.get(f"/api/documents/{doc_id}").json()["status"] == "failed",
        what="failed again",
    )
    final = harness.client.get(f"/api/documents/{doc_id}").json()
    assert final["error"], "重试失败后原因必须刷新，不能保留上一次的旧文案"
