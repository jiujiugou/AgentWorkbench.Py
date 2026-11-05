#!/usr/bin/env python
"""运维：重置某个账号的口令（这是唯一的人手入口，产品里没有「忘记密码」）。

    set AWB_RESET_PASSWORD=新口令
    .venv\\Scripts\\python scripts/reset_password.py --email wang@migrated.local

**为什么是脚本不是接口**：REQ 明确第一版关闭「自助找回密码」；而「管理员重置」
需要另一个管理员先登录——在「平台管理员自己也进不去」的场景里，那条路正好不可用。
能在这台机器上执行命令的人本来就能改库，脚本只是把这件事从「手写 SQL、忘了
`bump_credential_epoch`」变成一条有判据的命令。

**口令走环境变量或交互输入，不收 `--password`**：同一个理由，命令行参数会留在
shell 历史与进程列表（`ps aux` 对同机任何用户可见），而它是一个长期有效的口令。

它会做什么（同一事务）：改 `password_hash` → `credential_epoch + 1` → 吊销该用户
**全部**票据。后者是重置与本人改密的唯一差别：重置的前提通常是「这个人进不去了」
或「凭据可能泄露」，保留任何一条会话都是在赌。
它不会做什么：不改角色、不改状态、不动任何文档与会话记录。

退出码：

| 码 | 含义 |
|---|---|
| 0 | 已重置（旧会话全部失效） |
| 1 | 邮箱不存在 |
| 2 | 参数或环境不对（缺口令、口令太短） |
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# 允许不经安装直接跑（同 bootstrap_identity.py）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentworkbench.config import Settings
from agentworkbench.db import engine
from agentworkbench.identity import service, store

PASSWORD_ENV = "AWB_RESET_PASSWORD"  # noqa: S105 —— 环境变量名，不是口令本身
MIN_LENGTH = 12
"""与引导/改密同门槛。**不是安全策略，是防手滑**：
真正的强度约束属于需求（REQ 里没有），这里只拦「敲了个回车」和「拼音首字母」。"""


def _read_password() -> str | None:
    from_env = os.environ.get(PASSWORD_ENV, "")
    if from_env:
        return from_env
    if not sys.stdin.isatty():
        print(
            f"非交互环境必须用环境变量 {PASSWORD_ENV} 提供新口令"
            "（不用 --password：参数会留在 shell 历史与进程列表里）。",
            file=sys.stderr,
        )
        return None
    password = getpass.getpass("新口令：")
    if password != getpass.getpass("再输一次："):
        print("两次输入不一致。", file=sys.stderr)
        return None
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reset_password",
        description="重置某个账号的口令，并让它现有的全部会话失效。",
    )
    parser.add_argument("--email", required=True, help="要重置的账号邮箱")
    args = parser.parse_args(argv)

    email = str(args.email).strip()
    if not email:
        print("--email 不能为空。", file=sys.stderr)
        return 2

    password = _read_password()
    if not password or not password.strip():
        print("新口令不能为空。", file=sys.stderr)
        return 2
    if len(password) < MIN_LENGTH:
        print(f"新口令至少 {MIN_LENGTH} 位。", file=sys.stderr)
        return 2

    settings = Settings()
    conn = engine.connect(settings.db_path)
    try:
        # 老库可能还没跑过新版本的启动路径；先补列，再做查询/重置。
        engine.create_schema(conn)
        account = store.get_user_by_email(conn, email)
        if account is None:
            print(f"邮箱不存在：{email}", file=sys.stderr)
            print("用 `sqlite3 data/agentworkbench.db 'select email from users'` 看有哪些账号。")
            return 1
        service.reset_password(
            conn, user_id=account.user_id, new_password=password, now=datetime.now(UTC)
        )
    finally:
        conn.close()

    # 输出不带口令：终端输出会进 CI 日志与运维的复制粘贴历史。
    print("已重置")
    print(f"  账号：  {account.email}（{account.role}，团队 {account.tenant_id}）")
    print("  该账号此前签发的全部会话已失效，需要用新口令重新登录。")
    print("  登录后必须先修改这个临时口令。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
