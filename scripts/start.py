#!/usr/bin/env python
"""一条命令启动：读本地密钥 → 检查账号库 → 起服务。

    .venv\\Scripts\\python scripts\\start.py
    .venv\\Scripts\\python scripts\\start.py --host 0.0.0.0 --port 8080

（根目录的 `start.bat` 就是调它，双击也行。）

为什么要有它：`python -m agentworkbench serve` 之前必须先设 `AWB_SECRET_KEY`，
漏了会**直接拒绝启动**（`db/crypto.py` 的刻意失败，不是遗漏）。把那一步收进这里，
「启动」才重新是一条命令。

密钥来源顺序：环境变量 → `data/.secret_key`。
**它不替你生成密钥**：换密钥等于库里已有的供应商凭据全部作废（问答会 503），
那不是启动脚本该做的决定——`reset_password.py` 也不做这种事。

中文提示放在这里而不是 `start.bat`：`.bat` 按系统代码页逐字节解析，
UTF-8 的中文注释会把命令行切断（实测把 `rem` 拆成了「不是内部或外部命令」）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET_FILE = ROOT / "data" / ".secret_key"
DB_FILE = ROOT / "data" / "agentworkbench.db"

HOW_TO_MAKE_KEY = (
    '    .venv\\Scripts\\python -c "import secrets;'
    'print(secrets.token_urlsafe(32))" > data\\.secret_key'
)


def _secret() -> str | None:
    from_env = os.environ.get("AWB_SECRET_KEY", "").strip()
    if from_env:
        return from_env
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text(encoding="utf-8").strip() or None
    return None


def main(argv: list[str]) -> int:
    # 切到项目根：`serve` 的默认前端目录是 `Path.cwd()/web/dist`，
    # 而 `data/` 也是相对路径。不切的话从别的目录调起来会「服务起来了但页面 404」。
    os.chdir(ROOT)

    secret = _secret()
    if secret is None:
        print("[x] 缺少 AWB_SECRET_KEY，也没有 data/.secret_key。", file=sys.stderr)
        print("    生成一把并写进文件：", file=sys.stderr)
        print(HOW_TO_MAKE_KEY, file=sys.stderr)
        return 2
    os.environ["AWB_SECRET_KEY"] = secret

    if not DB_FILE.exists():
        print("[!] 库里还没有账号，先跑一次引导（只能成功一次）：", file=sys.stderr)
        print("    set AWB_BOOTSTRAP_PASSWORD=你的管理员口令", file=sys.stderr)
        print(
            "    .venv\\Scripts\\python scripts\\bootstrap_identity.py"
            " --email you@example.com --tenant 我的团队",
            file=sys.stderr,
        )
        return 1

    try:
        from agentworkbench.cli import main as cli_main
    except ModuleNotFoundError:
        print("[x] 包没装进这个解释器。先跑：", file=sys.stderr)
        print("    .venv\\Scripts\\pip install -e .", file=sys.stderr)
        return 2

    return cli_main(["serve", *argv])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
