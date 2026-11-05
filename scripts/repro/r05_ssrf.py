"""R-05（中）：设置页的「获取模型列表」不能被当成内网探测器。

红队 C 的 R-01：`POST /api/settings/fetch-models` 直接拿用户给的 `base_url` 去
`GET {base_url}/models`，于是能探本机端口、能用 `?` 劫持掉 `/models` 后缀打任意
path、还能把 Bearer 一起发出去。

这个端点**本来就是**「打一个用户给的地址」，所以不能一刀切禁内网——本产品的主场景
就是模型跑在 `127.0.0.1:11434`（Ollama）。修的是**永远不合法**的那三类：
非 http(s)、云元数据地址、URL 里带用户名口令。

跑法：`python scripts/repro/r05_ssrf.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx
from _repro import main
from redteam_env import ADMIN_EMAIL, Env


def body(report: object) -> None:
    check = report.check  # type: ignore[attr-defined]
    with Env("r05") as env:
        admin = env.client(ADMIN_EMAIL)

        cases = [
            ("file:///C:/Windows/win.ini", "非 http(s)"),
            ("http://169.254.169.254/latest/meta-data/", "云元数据"),
            ("http://user:pass@127.0.0.1:9/", "带凭据的 URL"),
        ]
        for url, label in cases:
            response = admin.post(
                "/api/settings/fetch-models", json={"baseUrl": url, "apiKey": "sk-attacker"}
            )
            body_text = response.text
            blocked = response.status_code == 400 and (
                "只支持 http" in body_text or "元数据" in body_text or "用户名口令" in body_text
            )
            check(
                f"拦下{label}",
                blocked,
                f"{url} → {response.status_code} {body_text[:90]}",
                note="修复前 file:// 报'连不上：UnsupportedProtocol'，元数据地址会被真的打出去",
            )

        # 反证：本地模型端点仍然可用（不能因为防 SSRF 把主场景弄坏）
        local = admin.post(
            "/api/settings/fetch-models", json={"baseUrl": "http://127.0.0.1:9/v1", "apiKey": "k"}
        )
        check(
            "环回地址仍然可以试（本地 Ollama 的主场景）",
            local.status_code == 400 and "连不上" in local.text,
            f"http://127.0.0.1:9/v1 → {local.status_code} {local.text[:70]}",
            note="「连不上」说明请求真的发出去了，只是端口没开——这是允许的",
        )

        # 已接受的风险，如实复现一次：平台管理员仍然可以把上游指向内网
        probe = admin.post("/api/settings/fetch-models", json={"baseUrl": "http://127.0.0.1:1/x?"})
        print(
            "  [记录] 平台管理员仍可指向任意内网地址（含用 ? 劫持路径）\n"
            f"           http://127.0.0.1:1/x? → {probe.status_code} {probe.text[:60]}\n"
            "           结论：这是该账号的既有能力（他能改这台机器的全部配置），"
            "记为已接受风险 U-16，靠网络出口策略而不是应用层"
        )
        admin.close()
        assert httpx  # 保持导入自洽（其余用例都通过 client 发）


if __name__ == "__main__":
    sys.exit(main("R-05 上游探测的地址校验", body))
