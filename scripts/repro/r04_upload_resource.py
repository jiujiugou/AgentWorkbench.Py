"""R-04（中）：上传面的两处资源放大。

红队 C 的两条，同一个落点——**一个普通成员就能让服务端为一小块输入付出大代价**：

1. **R-02**：`max_bytes` 的判定发生在整份读完**之后**。100MB 的请求体让服务端
   RSS 从 21MB 涨到 128MB 才回 400「文件超过大小上限」。
2. **R-03**：一个 51KB 的 xlsx（合并区 `A2001:Z20000`）让解析线程烧 12.6 秒 CPU。
   `MAX_ROWS_PER_SHEET` 只截「读多少行」，截不住「填多少格子」。

跑法：`python scripts/repro/r04_upload_resource.py`（约 30 秒）
"""

from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import psutil
from _repro import main
from redteam_env import MEMBER_EMAIL, Env

BIG_MB = 100
PEEK_BYTES = 60 * 1024 * 1024
"""内存涨幅的通过线。修复前实测 +107MB，修复后应该在"上限 + 一块"的量级。"""

MERGE_SECONDS = 6.0
"""合并区解析的通过线。修复前实测 12.6 秒（同一台机器、同一个文件）。"""


def _peak_rss(pid: int, stop: threading.Event, out: list[int], baseline: int) -> None:
    """采样服务端 RSS 相对基线的峰值。`out[1]` 记采样次数。

    **采样次数必须回传**：第一次写这个脚本时 psutil 拿不到进程就直接 return，
    `out[0]` 停在 0，于是"内存涨幅 +0MB"看起来完美通过——一个什么都没测的空断言。
    次数为 0 一律判失败（见下面的 `samples` 检查）。
    """
    try:
        process = psutil.Process(pid)
    except psutil.Error:
        return
    while not stop.is_set():
        try:
            out[0] = max(out[0], process.memory_info().rss - baseline)
            out[1] += 1
        except psutil.Error:  # pragma: no cover - 进程没了就结束采样
            return
        time.sleep(0.02)


def _merge_bomb() -> bytes:
    """51KB 左右、合并区跨在读窗口之外的 xlsx——红队用的就是这个形状。"""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "参数"
    sheet.merge_cells("A2001:Z20000")
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def body(report: object) -> None:
    check = report.check  # type: ignore[attr-defined]
    with Env("r04") as env:
        member = env.client(MEMBER_EMAIL)
        baseline = psutil.Process(env.pid).memory_info().rss

        peak = [0, 0]
        stop = threading.Event()
        sampler = threading.Thread(target=_peak_rss, args=(env.pid, stop, peak, baseline))
        sampler.start()
        try:
            payload = b"%" + b"x" * (BIG_MB * 1024 * 1024)
            response = member.post(
                "/api/documents",
                files={"file": ("huge.pdf", io.BytesIO(payload), "application/pdf")},
                timeout=180,
            )
        finally:
            stop.set()
            sampler.join()

        check(
            f"{BIG_MB}MB 请求体的内存涨幅",
            peak[1] > 20 and peak[0] < PEEK_BYTES,
            f"{response.status_code}，RSS +{peak[0] / 1024 / 1024:.0f}MB（{peak[1]} 次采样）",
            note="修复前 +107MB；采样次数为 0 也算失败——那说明这条根本没测到",
        )
        check(
            "超限请求仍然给出可读的拒收理由",
            response.status_code == 400 and "大小上限" in response.text,
            f"{response.status_code} {response.text[:80]}",
            note="修内存不能改变协议：这条仍是 400 + FILE_TOO_LARGE",
        )

        # ── 合并区炸弹 ──
        bomb = _merge_bomb()
        started = time.monotonic()
        uploaded = member.post(
            "/api/documents",
            files={
                "file": (
                    "bomb.xlsx",
                    io.BytesIO(bomb),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
            timeout=60,
        )
        doc_id = uploaded.json().get("id")
        status, elapsed = "pending", 0.0
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            detail = member.get(f"/api/documents/{doc_id}")
            status = detail.json().get("status", "?")
            if status in ("parsed", "failed"):
                elapsed = time.monotonic() - started
                break
            time.sleep(0.2)

        check(
            f"{len(bomb) / 1024:.0f}KB 的 xlsx 解析耗时",
            status == "parsed" and elapsed < MERGE_SECONDS,
            f"状态 {status}，{elapsed:.1f} 秒",
            note="修复前 12.6 秒；按线性外推 A2001:Z200000 是 3~4 分钟，文件仍只有几百 KB",
        )
        member.close()


if __name__ == "__main__":
    sys.exit(main("R-04 上传面的资源放大", body))
