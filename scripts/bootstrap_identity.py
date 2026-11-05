#!/usr/bin/env python
"""空库引导：建首个团队 + 首个管理员（REQ-AUTH-11 / `I-V12`）。

    python scripts/bootstrap_identity.py --email admin@example.com --tenant "我们团队"

口令从环境变量 `AWB_BOOTSTRAP_PASSWORD` 取；交互终端里没设就提示输入。
**不接受 `--password` 参数**：命令行参数会留在 shell 历史与进程列表里
（`ps aux` 对同机任何用户可见），而那是一个长期有效的管理员口令。

退出码：

| 码 | 含义 | 此时库的形态 |
|---|---|---|
| 0 | 建成 | 一个团队 + 一个管理员 |
| 1 | 引导已关闭（库里已有用户） | **一行都没变** |
| 2 | 参数或环境不对（缺口令、口令为空或太短） | 一行都没变 |

1 与 2 分开，是因为运维要区分「我已经建过了」和「我命令敲错了」——
两者的下一步动作完全相反。两种都是非零，都不会留下半个团队。

这条命令只跑一次，**不留第二次的机会**（`I-V12`）：判据是「库里有没有用户」，
不是「有没有跑过这个命令」。后者需要一张记状态的表，而那张表本身就是一个
可以被重置的开关——留着可用的引导路径就是永久后门，而且没人会记得关。
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# 允许不经安装直接跑（`python scripts/bootstrap_identity.py`）。
# 装在环境里时这条是空操作；没装时它决定「能不能跑起来」。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentworkbench.config import Settings
from agentworkbench.db import engine
from agentworkbench.identity import service

PASSWORD_ENV = "AWB_BOOTSTRAP_PASSWORD"  # noqa: S105 —— 环境变量名，不是口令本身
MIN_PASSWORD_LENGTH = 12


def _read_password() -> str | None:
    """从环境变量或交互终端取口令。拿不到返回 None，由调用方决定退出码。"""
    from_env = os.environ.get(PASSWORD_ENV, "")
    if from_env:
        return from_env
    if not sys.stdin.isatty():
        print(
            f"非交互环境必须用环境变量 {PASSWORD_ENV} 提供管理员口令"
            "（不用 --password：参数会留在 shell 历史与进程列表里）。",
            file=sys.stderr,
        )
        return None
    password = getpass.getpass("管理员初始口令：")
    if password != getpass.getpass("再输一次："):
        print("两次输入不一致。", file=sys.stderr)
        return None
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bootstrap_identity",
        description="空库引导：建首个团队与首个管理员。只能执行一次。",
    )
    parser.add_argument("--email", required=True, help="首个管理员的登录邮箱")
    parser.add_argument("--tenant", required=True, help="首个团队的名字")
    args = parser.parse_args(argv)

    email = str(args.email).strip()
    tenant_name = str(args.tenant).strip()
    if not email or not tenant_name:
        print("--email 与 --tenant 都不能为空。", file=sys.stderr)
        return 2

    password = _read_password()
    if not password or not password.strip():
        print("管理员口令不能为空。", file=sys.stderr)
        return 2
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"管理员初始口令至少 {MIN_PASSWORD_LENGTH} 位。", file=sys.stderr)
        return 2

    settings = Settings()
    # 库文件在 data_dir 下；目录不存在时 sqlite 报「unable to open database file」,
    # 而那句话看起来像权限问题，实际只是少了一个 mkdir。
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    conn = engine.connect(settings.db_path)
    try:
        # 幂等：空库建表，已有表什么都不做。引导本身不幂等，这一句不会改变它。
        engine.create_schema(conn)
        result = service.bootstrap(
            conn,
            email=email,
            password=password,
            tenant_name=tenant_name,
            now=datetime.now(UTC),
            must_change_password=True,
        )
    except service.BootstrapClosed as exc:
        print(f"引导已关闭：{exc}", file=sys.stderr)
        print("库里已有用户，这条命令不会修改任何一行。", file=sys.stderr)
        return 1
    finally:
        conn.close()

    # 输出里**没有口令、没有票据**：终端输出会进 CI 日志和运维的复制粘贴历史。
    print("引导完成")
    print(f"  团队：    {result.tenant_id}（{tenant_name}）")
    print(f"  管理员：  {result.user_id}（{email}）")
    print(f"  数据库：  {settings.db_path}")
    print("下一步：用这个邮箱和初始口令登录；首次登录必须先修改口令。")
    print("邀请其他成员由管理员发起。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
