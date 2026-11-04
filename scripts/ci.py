#!/usr/bin/env python
"""本地 CI：唯一入口。

    python scripts/ci.py check     # 全部段落（默认），任一失败即停
    python scripts/ci.py fix       # 自动修复能修的（ruff），然后跑 check
    python scripts/ci.py audit     # 每周：依赖卫生与漏洞
    python scripts/ci.py setup     # 首次：建 venv、装依赖、装 pre-commit

顺序不是随便排的：gates 第一，因为它验的是「尺子还在不在」。
尺子被削弱时，后面所有的绿都不作数。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def tool(name: str) -> list[str]:
    """优先用当前虚拟环境里的可执行文件，退回 PATH。"""
    scripts = Path(sys.executable).parent
    for suffix in (".exe", ""):
        candidate = scripts / f"{name}{suffix}"
        if candidate.exists():
            return [str(candidate)]
    found = shutil.which(name)
    if found is None:
        sys.exit(f"找不到 {name}。先跑：python scripts/ci.py setup")
    return [found]


def npm() -> list[str]:
    """npm 的启动前缀。

    Windows 上 `npm` 是 `.cmd` 脚本，`CreateProcess` 不认脚本——直接把它当
    可执行文件传会失败（`WinError 193`）。所以要经 `cmd /c` 走一趟。
    POSIX 上 which 给的是真可执行文件，直接跑。
    """
    found = shutil.which("npm")
    if found is None:
        sys.exit("找不到 npm。先跑：npm ci（在 web/ 里）")
    if os.name == "nt":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", found]
    return [found]


def build_stages() -> list[tuple[str, str, list[str]]]:
    ruff = tool("ruff")
    pytest_bin = tool("pytest")
    return [
        ("gates", "闸门配置未被削弱", [*pytest_bin, "tests/test_gates.py", "-q"]),
        ("format", "格式", [*ruff, "format", "--check", "."]),
        ("lint", "静态检查", [*ruff, "check", "."]),
        ("imports", "依赖边界", tool("lint-imports")),
        # deptry 放进 check 而不是 audit：它管的是"声明与使用是否一致"，
        # 也就是"你定了组件但我没用"这件事，必须每次提交都红，不能每周看一次。
        ("deps", "依赖声明与使用一致", [*tool("deptry"), "."]),
        ("types", "类型", tool("mypy")),
        ("tests", "测试", pytest_bin),
        # 前端渲染断言（vitest）。放在最后：它是最慢的一段，而前面任何一段
        # 失败时，Python 侧的红已经足够说明问题。
        # **不跑它等于没测**：C-V5 标了 pass，靠的就是这一段每次提交都红。
        ("web", "前端测试", [*npm(), "--prefix", "web", "run", "test"]),
        # 前端类型检查。**vitest 不做类型检查**（esbuild 只剥类型），于是
        # 「字段名写错」「导出没写出去」这类错误能一路过测试、到浏览器里才炸。
        # 这不是假设：F1 格 7 里 `messageOf` 被写在注释里、`parseError` 被删了
        # 还有一处引用，两处都是 15 条 vitest 全绿、由 `vue-tsc` 抓出来的。
        ("web-types", "前端类型", [*npm(), "--prefix", "web", "run", "typecheck"]),
    ]


def run_stage(name: str, title: str, cmd: list[str]) -> tuple[bool, float]:
    print(f"\n── {name:<8}{title}")
    print(f"   $ {' '.join(cmd)}")
    start = time.perf_counter()
    code = subprocess.run(cmd, cwd=ROOT, check=False).returncode
    elapsed = time.perf_counter() - start
    print(f"   {'PASS' if code == 0 else 'FAIL'}  {elapsed * 1000:.0f} ms")
    return code == 0, elapsed


def summary(results: list[tuple[str, bool, float]], skipped: list[str]) -> int:
    print("\n" + "─" * 52)
    for name, ok, elapsed in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<8}{elapsed * 1000:>7.0f} ms")
    for name in skipped:
        print(f"  SKIP  {name:<8}（前面已失败）")
    failed = [name for name, ok, _ in results if not ok]
    print("─" * 52)
    print("全部通过" if not failed else f"失败：{', '.join(failed)}")
    return 0 if not failed else 1


def cmd_check() -> int:
    results: list[tuple[str, bool, float]] = []
    stages = build_stages()
    skipped: list[str] = []
    for index, (name, title, cmd) in enumerate(stages):
        ok, elapsed = run_stage(name, title, cmd)
        results.append((name, ok, elapsed))
        if not ok:
            skipped = [s[0] for s in stages[index + 1 :]]
            break
    return summary(results, skipped)


def cmd_fix() -> int:
    ruff = tool("ruff")
    subprocess.run([*ruff, "format", "."], cwd=ROOT, check=False)
    subprocess.run([*ruff, "check", "--fix", "."], cwd=ROOT, check=False)
    print("\n已自动修复，开始校验。")
    return cmd_check()


def cmd_audit() -> int:
    results: list[tuple[str, bool, float]] = []
    for name, title, cmd in (
        ("deps", "依赖卫生（未用/缺失/分层错放）", [*tool("deptry"), "."]),
        ("cve", "已知漏洞", tool("pip-audit")),
    ):
        ok, elapsed = run_stage(name, title, cmd)
        results.append((name, ok, elapsed))
    return summary(results, [])


def cmd_setup() -> int:
    venv = ROOT / ".venv"
    if not venv.exists():
        print("建虚拟环境 .venv")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], cwd=ROOT, check=True)
    python = venv / "Scripts" / "python.exe"
    if not python.exists():
        python = venv / "bin" / "python"
    print("装依赖")
    subprocess.run([str(python), "-m", "pip", "install", "-e", ".[dev]"], cwd=ROOT, check=True)
    print("装 pre-commit 钩子")
    subprocess.run([str(python), "-m", "pre_commit", "install"], cwd=ROOT, check=False)
    print("\n完成。以后直接用：python scripts/ci.py check")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="本地 CI 唯一入口")
    parser.add_argument(
        "command",
        nargs="?",
        default="check",
        choices=["check", "fix", "audit", "setup"],
    )
    args = parser.parse_args()
    handlers = {
        "check": cmd_check,
        "fix": cmd_fix,
        "audit": cmd_audit,
        "setup": cmd_setup,
    }
    return handlers[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
