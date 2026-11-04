"""命令行入口：`python -m agentworkbench <command>`。

三个命令：`serve` / `backup` / `restore`。部署路径（F6 §1.11 / §1.12）刻意
保持最短：非 Python 的人拿到 README 能照着走，命令面每多一个旗标，安装文档
就多一段「什么时候用哪个」——而那一段没人会读。

uvicorn 只在这里被 import（运行时），业务代码不碰它——
换 ASGI 运行时（或改成 gunicorn+worker）时只动这个文件。

backup / restore 不要求 `AWB_SECRET_KEY`：备份的是密文，配置本身不需要解开；
但**恢复到另一台机器时必须带上同一个密钥**，否则库里的供应商凭据解不开——
这条提示印在 `--help` 与 README 里，不在代码里强行校验（校验也校验不了：
密钥对不对只有解密那一刻才知道）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Final

from agentworkbench.config import Settings
from agentworkbench.db.backup import BackupError, backup, restore

_DEFAULT_DIST: Final[Path] = Path("web") / "dist"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentworkbench",
        description="AgentWorkbench：企业知识检索问答（私有化部署）。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动服务（API + 前端静态托管，同源）")
    serve.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    serve.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    serve.add_argument(
        "--dist",
        type=Path,
        default=None,
        help=f"前端构建产物目录（默认 ./{_DEFAULT_DIST}，不存在则只起 API）",
    )

    backup_cmd = sub.add_parser("backup", help="热备：库的一致副本 + 上传文件 → 目标目录")
    backup_cmd.add_argument(
        "--to", type=Path, required=True, help="备份输出目录（必须不存在或为空）"
    )
    backup_cmd.add_argument(
        "--data-dir", type=Path, default=None, help="数据目录（默认读 AWB_DATA_DIR，再默认 ./data）"
    )

    restore_cmd = sub.add_parser("restore", help="从备份目录恢复（必须先停服）")
    restore_cmd.add_argument("--from", dest="from_dir", type=Path, required=True, help="备份目录")
    restore_cmd.add_argument(
        "--data-dir", type=Path, default=None, help="数据目录（默认读 AWB_DATA_DIR，再默认 ./data）"
    )

    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    if args.command == "backup":
        return _backup(args)
    if args.command == "restore":
        return _restore(args)
    parser.error(f"未知命令：{args.command}")
    return 2  # pragma: no cover


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    dist = args.dist if args.dist is not None else Path.cwd() / _DEFAULT_DIST
    # 经环境变量传给 Settings（factory 延迟建 app，参数传不进去）。
    # create_app 对不存在的目录只警告不失败——判定与提示都在那一处。
    os.environ["AWB_WEB_DIST"] = str(dist)

    print(f"AgentWorkbench → http://{args.host}:{args.port}")
    print("首次部署：先执行 python scripts/bootstrap_identity.py 建管理员（只能一次）。")
    uvicorn.run(
        "agentworkbench.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
    )
    return 0


def _resolve_data_dir(args: argparse.Namespace) -> Path:
    """`--data-dir` 旗标优先，缺了走 Settings（环境变量 / .env / 默认 ./data）。"""
    if args.data_dir is not None:
        return Path(args.data_dir)
    return Path(Settings().data_dir)


def _backup(args: argparse.Namespace) -> int:
    data_dir = _resolve_data_dir(args)
    try:
        dest_db = backup(data_dir, args.to)
    except BackupError as exc:
        print(f"备份失败：{exc}", file=sys.stderr)
        return 1
    print(f"备份完成：{args.to}")
    print(f"  库：{dest_db}")
    print(f"  上传文件：{args.to / 'files'}（不存在该目录 = 没有上传过文件）")
    return 0


def _restore(args: argparse.Namespace) -> int:
    data_dir = _resolve_data_dir(args)
    print("注意：恢复前必须停服；换机器恢复需设置同一个 AWB_SECRET_KEY，否则供应商凭据解不开。")
    try:
        target_db = restore(args.from_dir, data_dir)
    except BackupError as exc:
        print(f"恢复失败（目标数据未动）：{exc}", file=sys.stderr)
        return 1
    print(f"恢复完成：{target_db}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
