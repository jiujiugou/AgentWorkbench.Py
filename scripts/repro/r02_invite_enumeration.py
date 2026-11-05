"""R-02（中）：一张邀请只能换到一次「这个邮箱注册过吗」。

红队 A（R-03）与 B（R-02）各自复现：`400 这个邮箱已经有账号了` 这条失败路径
**不消耗令牌**，于是一张被转发的邀请就能拿一份邮箱名单逐个跑一遍，而且能跨团队
（用华北的邀请问出华东的人存不存在）。

修法不是「换一句文案」——换文案仍然能从 400/201 的差别读出来。修的是**成本**：
问到答案就把令牌烧掉，于是一张邀请最多买到一个答案。

跑法：`python scripts/repro/r02_invite_enumeration.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx
from _repro import main
from redteam_env import ADMIN_EMAIL, OWNER_EMAIL, Env

PASSWORD = "a-real-passphrase"  # noqa: S105 —— 这是建号用的测试口令，不是任何真实凭据


def body(report: object) -> None:
    check = report.check  # type: ignore[attr-defined]
    with Env("r02") as env:
        admin = env.client(OWNER_EMAIL)
        invites = [
            admin.post("/api/members/invites", json={"role": "member"}).json()["token"]
            for _ in range(3)
        ]
        anonymous = httpx.Client(base_url=env.base, timeout=20, trust_env=False)

        first = anonymous.post(
            f"/api/auth/invites/{invites[0]}",
            json={"email": ADMIN_EMAIL, "password": PASSWORD},
        )
        check(
            "第一次探测确实拿到了答案（设计上保留）",
            first.status_code == 400 and "已经有账号" in first.text,
            f"用已注册邮箱 → {first.status_code}「{first.json().get('message', '')}」",
            note="这一半消不掉：全局唯一邮箱 + 允许凭邀请自建号，必然能从成功/失败读出存在性",
        )

        # 关键：令牌已经被烧掉，第二问不再给答案
        again = anonymous.post(
            f"/api/auth/invites/{invites[0]}",
            json={"email": "brand-new@example.com", "password": PASSWORD},
        )
        check(
            "同一张邀请第二次提问被拒（枚举器不成立）",
            again.status_code == 404,
            f"换一个全新邮箱再试 → {again.status_code}",
            note="修复前这里会 201（建号成功），于是一张邀请可以无限次提问",
        )

        # 第二张邀请：换一个团队里的邮箱，仍然是一次一问
        second = anonymous.post(
            f"/api/auth/invites/{invites[1]}",
            json={"email": "warden@example.com", "password": PASSWORD},
        )
        third = anonymous.post(
            f"/api/auth/invites/{invites[1]}",
            json={"email": "another@example.com", "password": PASSWORD},
        )
        check(
            "正常建号不受影响",
            second.status_code == 201 and third.status_code == 404,
            f"全新邮箱 → {second.status_code}；同令牌再用 → {third.status_code}",
            note="这一条防止「为了防枚举把正常注册也弄死」",
        )
        anonymous.close()
        admin.close()


if __name__ == "__main__":
    sys.exit(main("R-02 邀请令牌不是邮箱枚举器", body))
