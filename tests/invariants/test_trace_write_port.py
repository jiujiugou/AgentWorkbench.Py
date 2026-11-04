"""不变量：只有 trace 能碰 run 的仓储，「唯一写入口」是事实而不是约定。

失效形态（用户会看到什么）：

**写入点散落。** 有人在 `api` 里直接 `insert into agent_runs`，看起来只是图方便。
后果不是"多写了一行"，而是 §1.9 六格的保证逐条失效：状态可以绕过白名单、
终态可以被写两次、步骤序号可以自己排。而**失效时没有任何告警**——
记录本身看起来完好，只是不再能证明任何事。

这条断言与 import-linter 的分层契约不重复：分层只能约束"api 不能 import db"，
说不出"db 的某一个模块只许一个使用者"。runs 仓储比 `documents` 更严格，
因为它的每一行都是证据。

改坏这个不变量会怎样：在 `api/routes/knowledge.py` 加一行
`from agentworkbench.db.repositories import runs` → 当场红。

服务于：A-V2（无后门）
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

# 能 import run 仓储的模块。**读侧在这个名单里**——它要按 run_id 取步骤。
# 名单变长不等于放松：真正的「唯一写入口」由下面那道写函数扫描守，它只看
# *谁调用了写函数*，不看谁 import 了模块。
ALLOWED_IMPORTERS = frozenset({"agentworkbench.trace.recorder", "agentworkbench.trace.reader"})
WRITER = "agentworkbench.trace.recorder"
WRITE_FUNCTIONS = frozenset({"insert_run", "close_run", "insert_step", "close_step"})
REPOSITORY = "agentworkbench.db.repositories.runs"

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def _module_name(path: Path, src_root: Path) -> str:
    """文件路径 → 模块名。`__init__.py` 归到它所在的包。"""
    parts = list(path.relative_to(src_root).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports_of(module: str, source: str, *, is_package: bool = False) -> set[str]:
    """解析一个模块的全部导入目标（绝对名）。

    相对导入要解析成绝对名，否则 `from .repositories import runs` 就能绕过
    白名单——那正是这条断言最该防的写法。层级算不出来时抛错而不是忽略：
    扫描器静默漏掉一个文件，等于这条断言从那天起只剩装饰。
    """
    tree = ast.parse(source)
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `__init__.py` 的模块名就是包名；普通模块要先退到所在包
                package = module.split(".") if is_package else module.split(".")[:-1]
                drop = node.level - 1
                if drop > len(package):
                    raise ValueError(f"{module}: 相对导入层级超出包深度（{node.level}）")
                base = package[: len(package) - drop] if drop else package
                prefix = ".".join(base)
            else:
                prefix = ""

            if node.module:
                prefix = f"{prefix}.{node.module}" if prefix else node.module
            found.add(prefix)
            found.update(f"{prefix}.{alias.name}" for alias in node.names)

    return found


def scan_importers(src_root: Path, *, target: str = REPOSITORY) -> dict[str, set[str]]:
    """扫出所有导入了 `target` 的模块（模块名 → 导入到的具体名字）。"""
    importers: dict[str, set[str]] = {}
    for path in sorted(src_root.rglob("*.py")):
        module = _module_name(path, src_root)
        source = path.read_text(encoding="utf-8")
        for name in imports_of(module, source, is_package=path.name == "__init__.py"):
            if name == target or name.startswith(f"{target}."):
                importers.setdefault(module, set()).add(name)
    return importers


def _runs_names(tree: ast.AST) -> set[str]:
    """这个文件里指向 runs 模块的本地名字（含 `as` 别名）。

    别名必须解析：`from ...repositories import runs as runs_repo` 是本仓
    recorder 的真实写法，只认字面 `runs` 的话，扫描器对最该看见的那个文件
    是瞎的——自检那条会当场红（写这段时就红了）。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == REPOSITORY:
                    names.add(alias.asname or alias.name.rsplit(".", 1)[-1])
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == "repositories" or node.module.endswith(".repositories"))
        ):
            for alias in node.names:
                if alias.name == "runs":
                    names.add(alias.asname or "runs")
    return names


def write_uses(source: str) -> set[str]:
    """文件里对 runs 仓储写函数的引用。

    两种写法都要认：`runs.close_run(...)`（导入了模块，可能带别名）与
    `from ...runs import close_run`（直接导入函数）。只认字面 `runs.` 的话，
    别名就是一条侧门——而它恰恰是更整洁的写法。
    """
    tree = ast.parse(source)
    aliases = _runs_names(tree)
    found: set[str] = set()

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        ):
            found.add(node.attr)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == "runs" or node.module.endswith(".runs"))
        ):
            found.update(alias.name for alias in node.names)
    return found


def test_only_the_recorder_calls_the_write_functions() -> None:
    """写函数只许 `trace.recorder` 调用——读侧可以 import 这个模块，但调不动写。

    这条比"只许一个模块 import"更准：读侧本来就要 import 它才能取步骤，
    按模块名拦会把读侧逼去自己写 SQL，那反而是第二个访问路径。
    """
    offenders: dict[str, set[str]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        module = _module_name(path, SRC_ROOT)
        if module == WRITER:
            continue
        used = write_uses(path.read_text(encoding="utf-8")) & WRITE_FUNCTIONS
        if used:
            offenders[module] = used

    assert not offenders, (
        f"出现了第二个写入者；写入路径一旦不止一条，「每条操作都留痕」就不再成立：{offenders}"
    )


def test_the_write_scan_actually_sees_the_recorder() -> None:
    """自检：扫描器坏掉时，上面那条会因为空集而通过。"""
    source = (SRC_ROOT / "agentworkbench" / "trace" / "recorder.py").read_text(encoding="utf-8")
    assert write_uses(source) & WRITE_FUNCTIONS, "扫描器连 recorder 的写调用都没看见"


def test_only_trace_imports_the_run_repository() -> None:
    importers = scan_importers(SRC_ROOT)
    assert set(importers) == set(ALLOWED_IMPORTERS), (
        "run 仓储出现了 trace 之外的使用者；记录一旦能被别处直接读写，"
        f"「每条操作都留痕」就不再成立：{sorted(importers)}"
    )


def test_scanner_actually_sees_the_recorder() -> None:
    """自检：扫描器自己坏掉时，上面那条会被空集蒙过去。

    路径写错、rglob 扫不到、ast 解析全失败——三种坏法都会让 `== set()` 变成
    「恰好相等」或「一处都没扫到」。所以必须证明扫描器真的能看见东西。
    """
    scanned = list(SRC_ROOT.rglob("*.py"))
    assert len(scanned) > 20, f"只扫到 {len(scanned)} 个文件，路径八成错了"
    assert "agentworkbench.trace.recorder" in scan_importers(SRC_ROOT)


@pytest.mark.parametrize(
    ("module", "source", "expected"),
    [
        ("agentworkbench.api.bad", "import agentworkbench.db.repositories.runs", REPOSITORY),
        (
            "agentworkbench.api.bad",
            "from agentworkbench.db.repositories import runs",
            REPOSITORY,
        ),
        (
            "agentworkbench.api.bad",
            "from agentworkbench.db.repositories.runs import insert_run",
            f"{REPOSITORY}.insert_run",
        ),
        # 相对导入：绕过白名单最省事的写法
        ("agentworkbench.api.bad", "from ..db.repositories import runs", REPOSITORY),
        ("agentworkbench.db.repositories.bad", "from . import runs", REPOSITORY),
        (
            "agentworkbench.db.repositories.bad",
            "from .runs import close_run",
            f"{REPOSITORY}.close_run",
        ),
    ],
)
def test_import_forms_are_all_resolved(module: str, source: str, expected: str) -> None:
    """六种写法都要认出来。少认一种，那条白名单就有一条侧门。"""
    assert expected in imports_of(module, source)


def test_a_new_writer_anywhere_turns_it_red(tmp_path: Path) -> None:
    """负控的本质：真的建一个第二写入者，断言必须抓到。

    不改本仓的源码（那会让测试自己违反白名单），而是在临时目录里造一份
    最小结构——扫描逻辑对路径无假设，所以这等价于"有人在 api 里加了一行"。
    """
    package = tmp_path / "src" / "agentworkbench"
    (package / "api").mkdir(parents=True)
    (package / "trace").mkdir(parents=True)
    (package / "trace" / "recorder.py").write_text(
        "from agentworkbench.db.repositories import runs\n", encoding="utf-8"
    )
    (package / "api" / "leak.py").write_text(
        "from agentworkbench.db.repositories.runs import insert_run\n", encoding="utf-8"
    )

    importers = scan_importers(tmp_path / "src")

    assert set(importers) == {"agentworkbench.trace.recorder", "agentworkbench.api.leak"}
    assert set(importers) != set(ALLOWED_IMPORTERS), "第二使用者必须让白名单断言失败"
    # 而且那条写函数扫描也要抓到它：它导的是 `insert_run` 这个写函数
    leak_source = (package / "api" / "leak.py").read_text(encoding="utf-8")
    assert write_uses(leak_source) & WRITE_FUNCTIONS == {"insert_run"}


def test_python_version_supports_returning() -> None:
    """`insert_step` 用 RETURNING 取 seq。3.35 以下会静默返回 None → 崩在这条上。"""
    import sqlite3

    major, minor, *_ = sqlite3.sqlite_version.split(".")
    assert (int(major), int(minor)) >= (3, 35), f"SQLite {sqlite3.sqlite_version} 不支持 RETURNING"
    assert sys.version_info >= (3, 12)
