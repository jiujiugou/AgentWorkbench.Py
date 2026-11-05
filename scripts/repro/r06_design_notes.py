"""R-06（低）：错误体一致性已修；三条「行为如此」的产品口径集中复现。

红队 C 的 R-04/R-05、A 的 R-04、B 的 R-03，都不是实现 bug，而是**产品口径没定**。
D9 不替产品拍板，但要把它们稳定复现出来，让拍板的人看见。**其中第 2 条已拍板**
（ADR 0008：删除只对管理员开放），这里从「记录现象」改成「断言挡住」。

1. **未匹配路径的 404**：修复前是框架默认体 `{"detail": "Not Found"}`——没有
   `message`、没有 `request_id`，前端只能把这段 JSON 原文显示给用户。**已修**。
2. **成员能删别人的文档**：已定口径——成员 403，管理员 204（ADR 0008）。
3. **匿名能把任意账号锁一段时间**：失败计数只按账号，没有 IP 维度，也没有提前解锁。
4. **同内容去重会回显别人的文件名**：同团队内上传相同内容，拿到的是先上传者那一行。

跑法：`python scripts/repro/r06_design_notes.py`
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx
from _repro import main
from redteam_env import ADMIN_EMAIL, MEMBER_EMAIL, OWNER_EMAIL, Env


def _record(lines: str) -> None:
    for line in lines.splitlines():
        print(f"  {line}")


def body(report: object) -> None:
    check = report.check  # type: ignore[attr-defined]
    with Env("r06") as env:
        member = env.client(MEMBER_EMAIL)
        admin = env.client(OWNER_EMAIL)

        # ── 1. 未匹配路径的五字段信封（已修） ──
        missing = member.get("/api/nope")
        payload = missing.json() if "json" in missing.headers.get("content-type", "") else {}
        check(
            "未匹配路径也是五字段信封",
            missing.status_code == 404
            and set(payload) == {"code", "http_status", "message", "retryable", "request_id"}
            and missing.headers.get("x-request-id") == payload.get("request_id"),
            f"GET /api/nope → {missing.status_code} {str(payload)[:90]}",
            note='修复前是 {"detail": "Not Found"}，且没有 request_id 可查日志',
        )

        # ── 2. 成员删别人的文档：已定口径，成员被拒（ADR 0008） ──
        created = admin.post(
            "/api/documents",
            files={
                "file": (
                    "admin-owned.docx",
                    io.BytesIO(b"PK\x03\x04 broken"),
                    "application/octet-stream",
                )
            },
        )
        doc_id = created.json().get("id")
        deleted = member.delete(f"/api/documents/{doc_id}")
        still_there = admin.get(f"/api/documents/{doc_id}")
        check(
            "最低权限成员删不掉管理员上传的文档",
            deleted.status_code == 403 and still_there.status_code == 200,
            f"成员 DELETE → {deleted.status_code}；管理员随后 GET → {still_there.status_code}",
            note="修复前是 204（只判团队，不判角色）；现在判定在查行之前，"
            "成员拿别人的 id 与随机 id 得到同一个 403",
        )

        # ── 3. 匿名锁定任意账号 ──
        attempts = 0
        for _ in range(6):
            response = httpx.post(
                f"{env.base}/api/auth/login",
                json={"email": ADMIN_EMAIL, "password": "definitely-wrong"},
                timeout=30,
                trust_env=False,
            )
            attempts += 1
            if response.status_code != 401:
                break
        correct = httpx.post(
            f"{env.base}/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": env.password},
            timeout=30,
            trust_env=False,
        )
        _record(
            f"[记录] 匿名可以用错口令把任意已知邮箱锁一段时间\n"
            f"         连发 {attempts} 次错口令后，用**正确**口令登录 → {correct.status_code}\n"
            "         结论：锁定策略的固有取舍（不按来源 IP、不能提前解锁）。记 U-20；\n"
            "         上线时要确认「平台管理员被锁只能等 15 分钟」这件事可以被接受。"
        )

        # ── 4. 去重回显别人的文件名 ──
        tiny = b"PK\x03\x04 identical-bytes"
        first = member.post(
            "/api/documents",
            files={"file": ("zhao-original.docx", io.BytesIO(tiny), "application/octet-stream")},
        )
        second = admin.post(
            "/api/documents",
            files={"file": ("wang-own-name.docx", io.BytesIO(tiny), "application/octet-stream")},
        )
        _record(
            f"[记录] 同内容去重时回显的是先上传者那一行\n"
            f"         zhao 传 zhao-original.docx → {first.json().get('fileName')}\n"
            f"         wang 传 wang-own-name.docx → {second.json().get('fileName')}"
            f"（同一个 id：{first.json().get('id') == second.json().get('id')}）\n"
            "         结论：同团队的文件名本来就在列表里可见，**不构成越权**；\n"
            "         「我传的名字被静默丢弃」是体验问题。记 U-21。"
        )
        member.close()
        admin.close()


if __name__ == "__main__":
    sys.exit(main("R-06 错误体 + 三条产品口径", body))
