#!/usr/bin/env python
"""存量归属绑定：把阶段 1 的数据挪进一个真实团队（`I-V5` 的最后一段）。

    python scripts/bind_tenant.py --to t-xxxxxxxx          # 预演，一个字节都不写
    python scripts/bind_tenant.py --to t-xxxxxxxx --apply  # 真跑

阶段 1 的每一行都写在 `tenant_id = "default"`（`Settings.tenant_id` 的默认值）下，
而引导建出来的管理员属于 `t-xxxx`。于是旧数据**不属于任何真实团队**——
按 `AUTH-06` 对所有人 404。这条命令就是回答「那批行归谁」。

**默认是预演，`--apply` 才写。** 这个顺序是刻意的：搬迁类命令里，
「先看一眼会发生什么」必须是最省事的那个动作，否则人会直接加上 `--apply`。

退出码：

| 码 | 含义 | 此时库的形态 |
|---|---|---|
| 0 | 完成（含「没有可绑定的行」） | 预演模式下**一行都没变** |
| 2 | 参数不对 / 目标团队不存在 / 有 sha256 冲突 | **一行都没变** |

「目标团队不存在」单独成一档，因为它是最容易犯的错：从别的环境的日志里抄一个
团队 id 过来，命令会报成功，而那批行绑过去仍然对所有人 404——一个
「看起来成功了、其实什么都没发生」的结果，比失败更难查。

**不做自动绑定**：那需要一个默认值，而默认值意味着任何新建团队都可能继承
上一批数据——它不报错，只让新团队一登录就看见别人的文件。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# 允许不经安装直接跑（见 bootstrap_identity.py 的同款理由）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentworkbench.config import Settings
from agentworkbench.db import engine
from agentworkbench.db.rebind import DATA_TABLES, BindingConflict, BindReport, bind

EXIT_OK = 0
EXIT_REFUSED = 2


def _print_report(report: BindReport) -> None:
    print(f"源团队：  {report.source}")
    print(f"目标团队：{report.target}")
    for table in DATA_TABLES:
        print(f"  {table:<14}{report.counts[table]:>8} 行")
    print(f"  {'合计':<12}{report.total:>8} 行")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bind_tenant",
        description="把阶段 1 遗留的 default 租户数据绑定到一个真实团队。默认只预演。",
    )
    parser.add_argument("--to", required=True, help="目标团队 id（tenants.id，t- 开头）")
    parser.add_argument(
        "--from",
        dest="source",
        default=None,
        help="源租户 id，默认取配置里的 tenant_id（阶段 1 的固定值）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真的写库；不加这个参数只打印「会发生什么」",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    source = str(args.source).strip() if args.source else settings.tenant_id
    target = str(args.to).strip()

    conn = engine.connect(settings.db_path)
    try:
        try:
            report = bind(conn, source=source, target=target, apply=bool(args.apply))
        except sqlite3.OperationalError as exc:
            # 典型是 `no such table: tenants`：库还没建过。报原文而不是猜。
            print(f"库读不了：{exc}（先跑 scripts/bootstrap_identity.py）", file=sys.stderr)
            return EXIT_REFUSED
        except BindingConflict as exc:
            print(f"拒绝绑定：{exc}", file=sys.stderr)
            print("目标团队里已有同一份文件（sha256 相同），先决定留哪边：", file=sys.stderr)
            for doc_id, file_name in exc.conflicts:
                print(f"  - {file_name}  ({doc_id})", file=sys.stderr)
            print("库里一行都没变。", file=sys.stderr)
            return EXIT_REFUSED
        except ValueError as exc:
            print(f"拒绝绑定：{exc}", file=sys.stderr)
            return EXIT_REFUSED
    finally:
        conn.close()

    _print_report(report)

    if report.applied:
        print("已提交。" if report.total else "没有可绑定的行，库未改动。")
        return EXIT_OK

    if report.conflicts:
        print(f"\n注意：{len(report.conflicts)} 份文档在目标团队里已存在，加 --apply 会被拒绝：")
        for doc_id, file_name in report.conflicts:
            print(f"  - {file_name}  ({doc_id})")
    print("\n这是预演，一个字节都没写。确认无误后加 --apply。")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
