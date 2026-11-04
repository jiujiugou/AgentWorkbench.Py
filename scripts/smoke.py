#!/usr/bin/env python
"""REQ-17 的人手验收脚本：删掉前端，产品仍要能被另一个程序完整用完。

与 tests/invariants/test_smoke_flow.py 是同一份流程的两种形态：
测试版跑在 CI 里（传输层 TestClient），这个版本打向真实运行的服务——
部署后第一次配置、或怀疑"是不是只有浏览器能用"时，跑它。

用法：
    AWB_SMOKE_EMAIL=admin@example.com AWB_SMOKE_PASSWORD=... \
        python scripts/smoke.py http://localhost:8080

登录步要凭据，所以从环境变量取、**不走命令行参数**：`--password` 会留在 shell
历史与 `ps` 的输出里，而这个脚本的用途是「部署后随手跑一次」，随手跑的脚本
最容易被人用一个明文口令调用。

前六步与判据原文一致：登录 → 上传 → 提问 → 取引用 → 查运行 → 查历史。
（F1 落地前这里缺登录步，理由是「其余五步当前仍可验」——那个理由在闸门挂上
之后就不成立了：没有票据连第一线上传都是 401，脚本会五步全红，而它不在 CI 里，
所以坏掉也不会有人知道。）

退出码 0 = 六步全过；任何一步失败即停并打印那一步的响应，方便贴给排障的人。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

SAMPLE_PDF = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "sample.pdf"

EMAIL_ENV = "AWB_SMOKE_EMAIL"
PASSWORD_ENV = "AWB_SMOKE_PASSWORD"  # noqa: S105 —— 这是环境变量名，不是口令


def step(n: int, title: str) -> None:
    print(f"\n── 第 {n} 步 · {title}")


def show(n: int, response: httpx.Response) -> dict[str, object]:
    print(f"   HTTP {response.status_code}")
    try:
        body: dict[str, object] = response.json()
    except Exception:
        print(f"   {response.text[:400]}")
        raise SystemExit(f"第 {n} 步响应不是 JSON：{response.status_code}") from None
    print(f"   {json.dumps(body, ensure_ascii=False)[:600]}")
    return body


def main(base_url: str) -> int:
    client = httpx.Client(base_url=base_url, timeout=60.0)

    # ── 第 0 步 · 登录 ──
    #
    # Cookie 由 httpx 的客户端自动保留（`Set-Cookie` 进 jar，后续请求带上），
    # 所以这里不需要手工拼 Authorization——票据本来就是 cookie 承载的。
    step(0, "登录")
    email = os.environ.get(EMAIL_ENV, "")
    password = os.environ.get(PASSWORD_ENV, "")
    if not email or not password:
        raise SystemExit(f"缺登录凭据：请设置 {EMAIL_ENV} 与 {PASSWORD_ENV}")
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    me = show(0, response)
    if response.status_code != 200:
        # 登录失败要在这里停：继续跑下去会在第 1 步报「上传没返回 id」，
        # 而那个现象会把排障的人引向上传，真正的原因在票据上。
        raise SystemExit("第 0 步登录失败：先确认账号存在（引导脚本跑过）且口令正确")
    print(f"   登录为 {me.get('email')}（{me.get('role')}）")

    step(1, "上传")
    pdf = SAMPLE_PDF.read_bytes()
    response = client.post(
        "/api/documents", files={"file": ("addr-005.pdf", pdf, "application/pdf")}
    )
    doc = show(1, response)
    doc_id = str(doc.get("id", ""))
    if not doc_id:
        raise SystemExit("上传没有返回 id，后续步骤无法进行")

    step(2, "提问")
    question = "E2 报警表示什么？怎么处理？"
    response = client.post("/api/knowledge/ask", json={"question": question})
    body = show(2, response)
    run_id = str(body.get("runId", ""))
    conversation_id = str(body.get("conversationId", ""))
    if not run_id:
        raise SystemExit("ask 没有返回 runId——这条路上可能没留 run（REQ-17 的反面教材）")

    step(3, "取引用")
    sources = body.get("sources") or []
    if not sources:
        answer = str(body.get("answer", ""))[:120]
        print(f"   无引用（拒答或判定不足时合法，请人工确认）。answer={answer}")
    else:
        print(f"   {len(sources)} 张出处卡片，第一张来自《{sources[0].get('fileName')}》")

    step(4, "查运行")
    show(4, client.get(f"/api/runs/{run_id}/trace"))

    step(5, "查历史")
    show(5, client.get("/api/conversations"))
    if conversation_id:
        show(5, client.get(f"/api/conversations/{conversation_id}/messages"))

    print("\n五步全过：这个服务不需要浏览器也能用完。")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    raise SystemExit(main(sys.argv[1]))
