"""R-01（高）：登出 / 停用之后，旧会话必须**下一个请求**就失效。

红队在 D9 里独立撞了两次这条（`redteam-A.md` R-01、`redteam-B.md` R-01）：
`SessionCache` 命中就直接返回身份，不再查库；三条吊销路径只写库、不清缓存，
于是「登出」「停用」「改密」在**一个窗口**内全不生效——默认窗口当时是 60 秒。

真正的危险不是那 60 秒本身，是窗口内**能做的事**：被停用的管理员还是管理员，
还能改别人的角色，而那次改动不随窗口结束而消失。

跑法：

    python scripts/repro/r01_session_revocation.py

退出码 0 = 都挡住了；1 = 还能复现。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx
from _repro import main
from redteam_env import MEMBER_EMAIL, OWNER_EMAIL, Env

COOKIE = "awb_session"


def body(report: object) -> None:
    check = report.check  # type: ignore[attr-defined]
    with Env("r01") as env:
        zhao = next(u for u in env.info["users"] if u["email"] == MEMBER_EMAIL)

        # ── A. 登出后重放旧 cookie ──
        member = env.client(MEMBER_EMAIL)
        assert member.get("/api/documents").status_code == 200, "预热失败"
        ticket = member.cookies.get(COOKIE)
        assert ticket, "登录后没有 awb_session"
        assert member.post("/api/auth/logout").status_code == 204
        replay = httpx.get(
            f"{env.base}/api/documents",
            headers={"cookie": f"{COOKIE}={ticket}"},
            timeout=15,
            trust_env=False,
        )
        check(
            "登出后重放同一个 cookie",
            replay.status_code == 401,
            f"GET /api/documents → {replay.status_code}",
            note="修复前是 200，并且返回完整的团队文档列表",
        )
        member.close()

        # ── B. 停用后还能不能继续用 ──
        victim = env.client(MEMBER_EMAIL)
        assert victim.get("/api/documents").status_code == 200, "预热失败"
        admin = env.client(OWNER_EMAIL)
        disabled = admin.post(f"/api/members/{zhao['id']}/disable")
        after = victim.get("/api/documents")
        check(
            "被停用后的下一个请求",
            after.status_code == 401,
            f"disable → {disabled.status_code}；随后 GET /documents → {after.status_code}",
            note="修复前 50 秒内仍然是 200，而且这期间还能提问、删文档",
        )
        me = victim.get("/api/auth/me")
        check(
            "同一张票读自己的身份",
            me.status_code == 401,
            f"GET /api/auth/me → {me.status_code}",
            note="修复前是 200，响应体里写着 status=disabled——一边说停用一边照常授权",
        )
        victim.close()
        admin.close()


if __name__ == "__main__":
    sys.exit(main("R-01 会话吊销的即时性", body))
