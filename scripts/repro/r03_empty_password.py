"""R-03（低）：空口令不能建号、也不能把口令改成空。

红队 A 的 R-02：`POST /api/auth/invites/<t> {"password": ""}` 返回 201，
接着 `POST /api/auth/login {"password": ""}` 返回 200。也就是说任何知道邮箱的人
都能进这个账号，而它在成员列表里与正常账号没有区别。

定级低（需要一张邀请），但危害形态很差：入侵者可以把自己降成空口令再躲着。

跑法：`python scripts/repro/r03_empty_password.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx
from _repro import main
from redteam_env import ADMIN_EMAIL, OWNER_EMAIL, Env


def body(report: object) -> None:
    check = report.check  # type: ignore[attr-defined]
    with Env("r03") as env:
        admin = env.client(OWNER_EMAIL)
        token = admin.post("/api/members/invites", json={"role": "member"}).json()["token"]
        anonymous = httpx.Client(base_url=env.base, timeout=20, trust_env=False)

        empty = anonymous.post(
            f"/api/auth/invites/{token}", json={"email": "empty@example.com", "password": ""}
        )
        check(
            "空口令建号被拒",
            empty.status_code == 400,
            f'POST /api/auth/invites/<t> password="" → {empty.status_code}',
            note="修复前是 201，随后用空口令登录是 200",
        )

        # 邀请不该被这次失败烧掉：格式错误是客户端的问题
        retry = anonymous.post(
            f"/api/auth/invites/{token}",
            json={"email": "empty@example.com", "password": "a-real-passphrase"},
        )
        check(
            "口令格式错误不烧邀请",
            retry.status_code == 201,
            f"同一张邀请换正常口令 → {retry.status_code}",
            note="烧掉会逼被邀请人回去找管理员重发，代价不对等",
        )

        # 改密同样不能改成空
        changed = admin.post(
            "/api/auth/password", json={"oldPassword": env.password, "newPassword": ""}
        )
        check(
            "改密成空被拒",
            changed.status_code == 400,
            f'POST /api/auth/password newPassword="" → {changed.status_code}',
        )

        # 反证：旧口令仍然有效（空口令那次没有偷偷改掉什么）
        fresh = httpx.Client(base_url=env.base, timeout=20, trust_env=False)
        still = fresh.post("/api/auth/login", json={"email": OWNER_EMAIL, "password": env.password})
        check(
            "失败的改密没有改动口令",
            still.status_code == 200,
            f"用原口令重新登录 → {still.status_code}",
        )
        assert ADMIN_EMAIL  # 常量来自环境定义，避免 lint 当成未使用
        anonymous.close()
        fresh.close()
        admin.close()


if __name__ == "__main__":
    sys.exit(main("R-03 空口令", body))
