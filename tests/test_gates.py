"""守守卫：断言闸门配置本身没有被削弱。

这些测试不检查代码质量，检查的是「衡量代码质量的那把尺子还在不在」。

为什么需要它：C# 的编译器关不掉，Python 的每一道闸都能关掉——
改一行配置、加一个 noqa、把目录塞进 exclude，所有检查依然全绿。
这个文件是唯一能发现「标准被悄悄下调」的机制。

因此：**改 pyproject.toml 里的规则，这里必须跟着改**。那正是它存在的理由。
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# ── 基线：这些是「我们说好了要守的」，动摇它们必须是一次有理由的提交 ──
REQUIRED_RULE_SETS = {"E", "F", "I", "B", "UP", "SIM", "PGH", "S", "RUF", "ASYNC", "T20"}
ALLOWED_GLOBAL_IGNORES = {"RUF001", "RUF002", "RUF003"}  # 中文全角标点不是歧义字符
ALLOWED_PER_FILE_IGNORES = {
    "tests/*": {"S101"},
    "scripts/*": {"S603", "S607", "T20"},
    "evals/*": {"T20", "S603"},
    # vendored 副本（与 self 原版只差题库路径）：豁免随原件，S608 是原件里的 LIKE 拼接
    "evals/run_rag_eval_v2.py": {"T20", "S608"},
    # CLI 的启动横幅打在 stdout 是本职（uvicorn 自己也这么干）；只放 T20，
    # 别的规则（比如 S603）在这里仍然是错。
    "src/agentworkbench/cli.py": {"T20"},
}
# 原样搬迁、不再维护的代码目录。豁免只能落在这里，不能落在自己写的代码上。
VENDORED_DIRS = ("evals/self", "evals/public/rgb")
# vendored 副本：与 self 原件只差题库路径，豁免口径随原件（T-002）
VENDORED_FILES = ("evals/run_rag_eval_v2.py",)
# 不会被 import 的目录：数据副本、逐次输出、按脚本方式运行的评测工具。
# 给它们加 __init__.py 反而误导——那会让人以为可以 import。
NON_PACKAGE_DIRS = (
    "evals/self",
    "evals/fixtures",
    "evals/public",
    "evals/reports",
    "evals/spikes",
)
ALLOWED_RELAXED_MODULES = ("tests.", "evals.")
MIN_LAYERS = 5
MIN_CONTRACTS = 2
SCAN_DIRS = ("src", "evals")
FORBIDDEN_CONFIG_FILES = (
    "setup.cfg",
    ".flake8",
    "tox.ini",
    "mypy.ini",
    "pylintrc",
    ".pylintrc",
    "ruff.toml",
    ".ruff.toml",
)


def _load() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _section(*path: str) -> dict[str, Any]:
    node: Any = _load()
    for key in path:
        assert isinstance(node, dict), f"pyproject.toml 缺少 [{'.'.join(path)}]"
        node = node.get(key, {})
    assert isinstance(node, dict)
    return node


def test_ruff_rule_sets_intact() -> None:
    """规则集只能加，不能删。删一条 = 悄悄放走一类缺陷。"""
    selected = set(_section("tool", "ruff", "lint").get("select", []))
    missing = REQUIRED_RULE_SETS - selected
    assert not missing, f"ruff select 缺少规则集：{sorted(missing)}"


def test_ruff_global_ignores_are_empty() -> None:
    """全局关规则是最容易的偷懒，所以一条都不许。豁免要落到具体文件上。"""
    ignored = set(_section("tool", "ruff", "lint").get("ignore", []))
    assert ignored <= ALLOWED_GLOBAL_IGNORES, f"出现了全局 ignore：{sorted(ignored)}"


def test_ruff_per_file_ignores_are_whitelisted() -> None:
    """per-file 豁免必须写明归属，且只能落在约定的路径上。"""
    per_file = _section("tool", "ruff", "lint", "per-file-ignores")
    for pattern, codes in per_file.items():
        assert pattern in ALLOWED_PER_FILE_IGNORES, f"未登记的豁免路径：{pattern}"
        extra = set(codes) - ALLOWED_PER_FILE_IGNORES[pattern]
        assert not extra, f"{pattern} 多关了规则：{sorted(extra)}"


def test_ruff_has_no_excludes() -> None:
    """exclude 是后门：把目录排除掉，所有规则对它失效。"""
    excluded = _section("tool", "ruff").get("exclude", [])
    assert not excluded, f"ruff exclude 应为空，实际：{excluded}"


def test_mypy_is_strict() -> None:
    mypy = _section("tool", "mypy")
    assert mypy.get("strict") is True, "mypy strict 必须是 true"


def test_mypy_warns_on_unused_ignores() -> None:
    """写了没用也报错——否则 ignore 会越积越多且没人清理。"""
    assert _section("tool", "mypy").get("warn_unused_ignores") is True


def test_mypy_overrides_do_not_silence_errors() -> None:
    """只允许放松「必须写注解」，不允许 ignore_errors（那是整块静音）。"""
    overrides = _section("tool", "mypy").get("overrides", [])
    assert isinstance(overrides, list)
    for override in overrides:
        assert not override.get("ignore_errors"), f"禁止 ignore_errors：{override}"
        for module in override.get("module", []):
            assert module.startswith(ALLOWED_RELAXED_MODULES), f"未登记的放松模块：{module}"


def test_mypy_exclude_only_covers_vendored() -> None:
    """exclude 等于让整块代码从类型检查里消失，和 ignore_errors 同性质。

    只允许指向原样搬迁、不再维护的目录。想把自己写的代码 exclude 掉，这里会红。
    """
    excluded = _section("tool", "mypy").get("exclude", [])
    assert isinstance(excluded, list)
    for pattern in excluded:
        vendored = any(d in pattern for d in VENDORED_DIRS) or any(
            f in pattern for f in VENDORED_FILES
        )
        assert vendored, f"未登记的 mypy exclude：{pattern}"


def test_ruff_format_exclude_only_covers_vendored() -> None:
    """目录级排除是最后一道后门：被排除的代码，lint 和 format 一起消失。

    只允许指向原样搬迁的目录。想排除自己写的代码，这里会红。
    """
    extended = _section("tool", "ruff").get("extend-exclude", [])
    assert isinstance(extended, list)
    for pattern in extended:
        assert any(d in pattern for d in VENDORED_DIRS), f"未登记的 extend-exclude：{pattern}"


def test_importlinter_keeps_layers_contract() -> None:
    """依赖边界是架构本身。契约条数变少或层数变浅，都意味着边界被放宽。"""
    contracts = _section("tool", "importlinter").get("contracts", [])
    assert isinstance(contracts, list)
    assert len(contracts) >= MIN_CONTRACTS, f"契约只剩 {len(contracts)} 条"
    layers = [c for c in contracts if c.get("type") == "layers"]
    assert layers, "缺少 layers 契约：依赖方向不再被强制"
    assert len(layers[0].get("layers", [])) >= MIN_LAYERS, "分层数量变少"


def test_importlinter_has_no_ignore_imports() -> None:
    """ignore_imports 等于在边界上开洞，一条都不许。"""
    for contract in _section("tool", "importlinter").get("contracts", []):
        assert not contract.get("ignore_imports"), f"{contract.get('name')} 开了洞"


def test_no_bare_suppressions() -> None:
    """`# type: ignore` / `# noqa` 必须带理由（错误码），裸豁免一律禁止。"""
    patterns = (
        re.compile(r"#\s*type:\s*ignore(?!\[)"),
        re.compile(r"#\s*noqa(?!\s*:)"),
    )
    offenders: list[str] = []
    for directory in SCAN_DIRS:
        for path in (ROOT / directory).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            offenders.extend(
                f"{path.relative_to(ROOT)}:{line_no}"
                for line_no, line in enumerate(text.splitlines(), start=1)
                if any(p.search(line) for p in patterns)
            )
    assert not offenders, f"裸豁免：{offenders}"


def test_no_bypass_config_files() -> None:
    """同一套规则不允许在别处再写一份——两份配置必然打架，且没人知道该信哪个。"""
    found = [name for name in FORBIDDEN_CONFIG_FILES if (ROOT / name).exists()]
    assert not found, f"发现旁路配置文件：{found}"


def test_precommit_covers_ruff() -> None:
    """提交前那道闸可以薄，但不能没有。"""
    path = ROOT / ".pre-commit-config.yaml"
    assert path.exists(), "缺少 .pre-commit-config.yaml"
    text = path.read_text(encoding="utf-8")
    assert "astral-sh/ruff-pre-commit" in text, "pre-commit 未挂 ruff"
    assert "id: ruff" in text and "id: ruff-format" in text, "ruff 的 check 与 format 都要挂"


def test_every_package_has_init() -> None:
    """每个目录都是一个包边界，缺 __init__.py 会让 import 行为不可预期。"""
    missing: list[str] = []
    for directory in SCAN_DIRS:
        for path in (ROOT / directory).rglob("*"):
            if not path.is_dir() or _is_artifact(path) or _is_not_package(path):
                continue
            if not (path / "__init__.py").exists():
                missing.append(str(path.relative_to(ROOT)))
    assert not missing, f"缺少 __init__.py：{missing}"


def test_component_table_matches_dependencies() -> None:
    """组件决定表与依赖清单必须互相证成——这是「你定了、我不用」的唯一防线。

    为什么需要它：组件表写在文档里，人读得懂，机器读不懂。所以「文档说用 huey、
    代码里自己写了 30 行」这件事没有任何东西会发现。把决定搬进 `pyproject.toml`
    之后，三条规则可以机械执行：

    ① `adopted` 的包必须在 `dependencies` 里 —— 定了不用
    ② `dependencies` 里的包必须在这里且是 `adopted` —— 用了没定（偷偷加）
    ③ `rejected` 的包在 `src/` 里不许 import —— 偷偷用被否掉的东西
    ④ `rejected` 的条目必须写出重开条件 —— 没有重开条件的拒绝不是决定，是借口

    `planned` 是「批准了还没接」：既不许进依赖，也不许 import。
    改这张表等于改一个已经做过的决定，会出现在 diff 里——那正是它该有的成本。

    ④ 这条的理由：`rejected` 是三种状态里唯一**会随时间变成错**的一种。
    "当时评估过、当时没选"与"以后也不许选"是两件事；不写出触发条件，半年后
    没人敢动它，也没人知道该不该动——最后要么一直不用，要么被人悄悄加回来。
    """
    rows = _components()
    assert rows, "组件决定表是空的：[[tool.agentworkbench.component]] 至少要有 adopted 项"

    no_condition = [
        str(row["package"])
        for row in rows
        if row["status"] == "rejected" and "重开条件" not in str(row.get("reason", ""))
    ]
    assert not no_condition, (
        f"rejected 但没写重开条件：{no_condition}——"
        "「为什么不用」必须配一个「什么条件下重新考虑」，否则这条拒绝无法被复核"
    )

    declared = {_normalize(spec) for spec in _dependency_names()}
    adopted = {_normalize(str(row["package"])) for row in rows if row["status"] == "adopted"}
    not_adopted = {
        _normalize(str(row["package"])): str(row["status"])
        for row in rows
        if row["status"] != "adopted"
    }

    missing = adopted - declared
    assert not missing, f"定了但没写进依赖（定了不用）：{sorted(missing)}"

    unapproved = declared - adopted
    assert not unapproved, f"依赖里有未登记为 adopted 的包（用了没定）：{sorted(unapproved)}"

    leaked = declared & set(not_adopted)
    assert not leaked, f"未接/已否的包进了依赖：{dict.fromkeys(sorted(leaked))}"

    forbidden_modules = {
        _module_root(str(module))
        for row in rows
        if row["status"] != "adopted"
        for module in row.get("modules", [])
    }
    used_forbidden = sorted(_imported_roots() & forbidden_modules)
    assert not used_forbidden, f"src/ 里 import 了未接或已否的包：{used_forbidden}"


def _components() -> list[dict[str, Any]]:
    node = _section("tool", "agentworkbench").get("component", [])
    assert isinstance(node, list), "组件决定表格式不对"
    for row in node:
        for field in ("capability", "package", "status"):
            assert field in row, f"组件条目缺 {field}：{row}"
        assert row["status"] in {"adopted", "planned", "rejected"}, f"未知状态：{row}"
    return [row for row in node if isinstance(row, dict)]


# ── 验证登记表：F2 §四 的机器可读版 ──
# 文档会漂、会重写、会被删；pyproject 里的表改一行就是一次 diff。
# 这道闸保证三件事：条目一条不少、pass 的条目背后真有测试、
# partial/fail/manual 必须写明差在哪、为什么、怎么人工验。
REQUIRED_VERIFICATION_IDS = (
    # F2：知识资料。V-13~V-18 是 Office 格式（2026-11-04 补）——
    # 判据先于实现存在（TDD），所以它们此刻是 partial；闸门只检查「登记了
    # 就必须有对应测试」，不检查测试是不是绿的。
    {f"V-{index}" for index in range(1, 19)}
    | {f"G{index}" for index in range(1, 16)}  # F3：知识问答（功能验证 §4.2）
    # F4：执行与审计（§4.2）。id 用 `A-` 前缀 + 文档原编号，A-V1b 就是 V1b
    | {f"A-V{n}" for n in ("1", "1b", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11")}
    | {"A-H1", "A-H2"}  # F4 人工项（§4.3）
    # F5：会话与记录。`C-` = Conversation。F5 文档只写功能，这里是唯一的判据登记处
    # C-V12 删除的对外入口、C-V13 拒答在历史里的标记（2026-11-03 补）
    | {f"C-V{n}" for n in range(1, 14)}
    | {"C-H1", "C-H2"}  # F5 人工项（§4.3）
    # F6：服务端能力。`S-` = Server。文档 §四 的 V-1~V-10 与 F2 撞号，登记处换前缀。
    # S-V11（2026-11-04）是部署入口：不来自原 REQ 表，来自「别人能装起来」那句话。
    # S-V12（2026-11-04）是备份恢复（NFR-08）：同 S-V11，来自 MVP 第 4 句的后两件事。
    | {f"S-V{n}" for n in range(1, 13)}
    | {"S-H1", "S-H2"}  # F6 人工项（无前端部署 / 日志可查性）
    # F1：身份与访问。`I-` = Identity。文档 §四 原来只有"序 1~16"，没有稳定编号，
    # 2026-11-04 换成 I-V1~I-V16，判据整体搬进本表（文档只留功能，同 F5/F6）。
    # I-V17 是界面那一侧（格 7 补）：本域只要求界面做三件事，多一件都属于 F5。
    # I-V18 是平台级配置的写权限（2026-11-04 补）：这一条不来自任何表格，
    # 来自一次实测——`settings` 六条端点当时只要求登录，而 `providers` 是全局表。
    | {f"I-V{n}" for n in range(1, 19)}
    | {f"I-H{n}" for n in range(1, 6)}  # F1 人工项（空库部署 / 文案 / 运维能答 / 界面 / 隔离）
)
VERIFICATION_STATUSES = {"pass", "partial", "fail", "manual"}
# F4 §1.3 的四条不变量。抄在这里是当尺子用（同 REQUIRED_VERIFICATION_IDS 抄文档编号）：
# 改了文档必须改这里，改不动就是不想改。
F4_INVARIANTS = {"有", "全", "对", "无后门"}
# F5 §1.8 的四条：存（三块一起落库）/ 同（换设备逐字段相同）/
# 隔（他人会话连存在都不可推断）/ 清（删会话删干净）。
F5_INVARIANTS = {"存", "同", "隔", "清"}
# F6 的五条：通（REQ-17 只经 HTTP 可走完）/ 查（REQ-18 按 id 可查）/
# 离（REQ-19 断连不丢任务）/ 稳（REQ-20 错误稳定不泄露）/
# 一（REQ-22 模型配置平台级全局唯一）。五词对应五条 REQ——
# F6 文档 §1.2 论证过这五条各自都是不变量，少一个域就有缺口。
F6_INVARIANTS = {"通", "查", "离", "稳", "一"}
# 前缀 → 该域的不变量词表。两套词表刻意不共用：F5 若借用 F4 的「有」，
# "有"在记录域是"记录留下来了"、在会话域是"三块都在"，同一个字两种判据，
# 半年后没人分得清。前缀表同时决定"哪条不变量必须被覆盖"。
# F1 的五条：认（不认就不给）/ 隔（团队边界）/ 属（资源归属）/
# 记（拒绝留痕）/ 界（账户与权限的上限）。
# 为什么是五条不是四条：把"隔"和"属"合成一个词之后，"别人的会话读不到"与
# "别人的文档搜不到"会互相遮住覆盖度——一条绿了，另一条可以是空的。
F1_INVARIANTS = {"认", "隔", "属", "记", "界"}
INVARIANTS_BY_PREFIX: dict[str, set[str]] = {
    "A-": F4_INVARIANTS,
    "C-": F5_INVARIANTS,
    "S-": F6_INVARIANTS,
    "I-": F1_INVARIANTS,
}


def test_verification_table_matches_tests() -> None:
    """验证登记表与测试文件必须互相证成——「说验过但测试没了」的唯一防线。

    四条规则：
    ① F2 的 V-1~V-18、F3 的 G1~G15、F4 的 A-V1~A-V11（含 V1b）与 A-H1/H2、
       F5 的 C-V1~C-V13 与 C-H1/H2、F6 的 S-V1~S-V10 与 S-H1/H2、
       F1 的 I-V1~I-V16 与 I-H1~H5 一条不能少——少一条等于漏验，而且是不留痕迹的漏验。
       G1/V-10、G2/V-11、A-V3/V3 是同一验证对象在不同域文档里的编号，
       两边都强制登记，删哪边都红。
    ② status=pass 的条目，tests 列的每个 `文件::函数` 必须真实存在：
       测试被删、被改名、被整文件移除，这里当场红，而不是等评审时靠人发现。
    ③ partial 必须写 remaining、fail 必须写 reason、manual 必须写 evidence——
       「没验完」可以，但必须说清差在哪，不许留一句含糊的状态词。
    ④ 每条必须有 claim 与 negative——F2 §四的原话：写不出反例的断言不要写。
    ⑤ 带域前缀的条目必须挂该域的 invariant（F4：有/全/对/无后门；F5：存/同/隔/清），
       且每个域的四条各至少有一条验证——防「某条不变量一条验证都没有」：那相当于
       这条不变量只在文档里存在。覆盖度按域分开算，因为两套词表刻意不共用。
    """
    rows = _verifications()
    assert rows, "验证登记表是空的：[[tool.agentworkbench.verification]]"

    ids = [str(row["id"]) for row in rows]
    duplicated = sorted({vid for vid in ids if ids.count(vid) > 1})
    assert not duplicated, f"重复登记的验证条目：{duplicated}"
    missing_or_extra = REQUIRED_VERIFICATION_IDS.symmetric_difference(ids)
    assert not missing_or_extra, f"验证条目缺失或多出：{sorted(missing_or_extra)}"

    for row in rows:
        vid = str(row["id"])
        status = str(row["status"])
        assert status in VERIFICATION_STATUSES, f"{vid}: 非法 status：{status}"
        assert str(row.get("claim", "")).strip(), f"{vid}: 缺 claim（正控判据）"
        assert str(row.get("negative", "")).strip(), f"{vid}: 缺 negative（负控判据）"

        tests = row.get("tests", [])
        assert isinstance(tests, list), f"{vid}: tests 必须是数组"
        if status == "pass":
            assert tests, f"{vid}: 标了 pass 却没有任何测试支撑"
        for ref in tests:
            _assert_test_exists(vid, str(ref))

        if status == "partial":
            assert str(row.get("remaining", "")).strip(), (
                f"{vid}: partial 必须写 remaining（还差什么）"
            )
        elif status == "fail":
            assert str(row.get("reason", "")).strip(), f"{vid}: fail 必须写 reason（为什么没过）"
        elif status == "manual":
            assert str(row.get("evidence", "")).strip(), (
                f"{vid}: manual 必须写 evidence（人工验到哪）"
            )

        invariant = str(row.get("invariant", "")).strip()
        domain = next((p for p in INVARIANTS_BY_PREFIX if vid.startswith(p)), None)
        if domain:
            allowed = sorted(INVARIANTS_BY_PREFIX[domain])
            assert invariant, f"{vid}: {domain} 域的条目必须挂 invariant（{allowed}）"
            # 只许用本域的词。跨域借用不算"未知"，但会让两个域的覆盖度互相遮住：
            # C 条目挂上「有」，F5 的某一条就少了验证，而检查仍然绿。
            assert invariant in INVARIANTS_BY_PREFIX[domain], (
                f"{vid}: {domain} 域不许用外域不变量：{invariant}（本域：{allowed}）"
            )
        elif invariant:
            known = set().union(*INVARIANTS_BY_PREFIX.values())
            assert invariant in known, f"{vid}: 未知不变量：{invariant}"

    # 覆盖度按域分别算：F4 的四条不许有一条空着，F5 的四条同理。
    # 合起来算会漏——F5 只要占用了 F4 的某个词，两个域的缺口就互相遮住。
    for domain, names in INVARIANTS_BY_PREFIX.items():
        covered = {
            str(row.get("invariant", "")).strip()
            for row in rows
            if str(row["id"]).startswith(domain)
        } & names
        uncovered = names - covered
        assert not uncovered, f"{domain} 域这些不变量一条验证都没有：{sorted(uncovered)}"


def test_state_machines_are_registered() -> None:
    """状态机登记表：每台机器有名字、状态集、归属域与真源位置。

    **合法迁移不在登记表里抄第二份**——两份迁移表必然漂移，而且漂移的表现是
    "测试执行的那份是旧的"。登记表记「真源在哪个文件」；迁移白名单由测试
    直接执行真源（run_lifecycle 的做法）。这张表的价值是：想找状态机的人
    不用翻代码，想偷偷加一台状态机的人必须来这里留名。
    """
    machines = _section("tool", "agentworkbench").get("state_machine", [])
    assert machines, "状态机登记表是空的：[[tool.agentworkbench.state_machine]]"
    assert isinstance(machines, list)
    names = [str(row["name"]) for row in machines]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    assert not duplicated, f"重复登记的状态机：{duplicated}"
    for row in machines:
        for field in ("name", "states", "owner", "source"):
            assert field in row, f"状态机 {row.get('name', '?')} 缺 {field}"
        # 状态集至少两个状态、且不是"迁移步骤清单"——状态机登记的是状态，
        # 迁移归代码里的白名单。写"step1 -> step2 -> step3"这种流水线进来，
        # 说明作者想登记的是流程图，不是状态机。
        states = [item.strip() for item in str(row["states"]).split(",")]
        assert len(states) >= 2, f"{row['name']}: 状态至少两个，否则不构成状态机"
        assert not any("->" in item for item in states), (
            f"{row['name']}: states 里不许写迁移（->），迁移在代码的真源里"
        )


def _assert_test_exists(vid: str, ref: str) -> None:
    """`路径::节点` 必须真实存在。Python 用 ast、前端用文本包含——快且零副作用。

    前端这条是 2026-11-03 加的（C-V5）：渲染层的断言也是断言，"说验过但用例
    没了"同样要当场红。用文本包含而不是 ast，是因为 ast 只认 Python；用例名
    **同样要求 `test_` 前缀**，两边一把尺子，免得一条判据在一处严格、一处宽松。
    """
    path_str, separator, node = ref.partition("::")
    assert separator, f"{vid}: tests 条目必须是 `文件路径::测试函数` 形式：{ref}"
    assert node.startswith("test_"), f"{vid}: 登记的不是测试函数：{ref}"
    path = ROOT / path_str
    assert path.exists(), f"{vid}: 登记的测试文件不存在：{path_str}"
    source = path.read_text(encoding="utf-8")

    if path.suffix in {".ts", ".tsx"}:
        quoted = (f"'{node}'", f'"{node}"')
        assert any(item in source for item in quoted), (
            f"{vid}: {path_str} 里没有用例 {node}（被删了还是改名了？）"
        )
        return

    tree = ast.parse(source)
    defined = {
        item.name
        for item in ast.walk(tree)
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert node in defined, f"{vid}: {path_str} 里没有函数 {node}（被删了还是改名了？）"


def _verifications() -> list[dict[str, Any]]:
    node = _section("tool", "agentworkbench").get("verification", [])
    assert isinstance(node, list), "验证登记表格式不对"
    for row in node:
        for field in ("id", "status", "claim", "negative"):
            assert field in row, f"验证条目缺 {field}：{row}"
    return [row for row in node if isinstance(row, dict)]


def _dependency_names() -> list[str]:
    deps = _section("project").get("dependencies", [])
    assert isinstance(deps, list)
    return [str(item) for item in deps]


def _normalize(name: str) -> str:
    """包名归一：`sqlite-vec>=0.1.9` 与 `sqlite_vec` 是同一个东西。"""
    base = re.split(r"[<>=!~\[; ]", name, maxsplit=1)[0]
    return re.sub(r"[-_.]+", "_", base.strip().lower())


def _module_root(module: str) -> str:
    return module.split(".")[0]


def _imported_roots() -> set[str]:
    """`src/` 里实际 import 的顶层模块。只看第一段——`from a.b import c` 记 `a`。"""
    roots: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                roots.add(node.module.split(".")[0])
    return roots


def test_scoped_components_are_only_imported_where_allowed() -> None:
    """带 `allowed_in` 的 adopted 组件，只许出现在指定路径下。

    为什么需要这条：`sqlalchemy` 已经在依赖里（alembic 的 env.py 要一个连接对象），
    于是「业务路径不要用它」这句话**没有任何东西守着**——依赖已声明，import 不会被
    拦，规则 ③（rejected 不许 import）也管不到它。而一旦有人在仓储里 `from sqlalchemy
    import select`，项目就有了两套存储习语：有的表走映射、有的表走手写 SQL，
    读代码的人得先判断这个文件属于哪一派。

    改坏后会怎样：在 `db/repositories/conversations.py` 里加一行 `import sqlalchemy`，
    这条当场红，并指出是哪个文件——而不是等到半年后有人发现两张表的事务写法不一样。
    """
    rows = _components()
    scoped = {
        _module_root(str(module)): [str(p) for p in row["allowed_in"]]
        for row in rows
        if row["status"] == "adopted" and row.get("allowed_in")
        for module in row.get("modules", [])
    }

    violations: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        for module in imported & scoped.keys():
            if not any(relative.startswith(prefix) for prefix in scoped[module]):
                violations.append(f"{relative} 用了 {module}（只许在 {scoped[module]} 下）")

    assert not violations, "限定作用域的组件被用到了别处：" + "；".join(violations)


def test_every_invariant_states_what_breaks() -> None:
    """每条不变量必须写明「被改坏时用户会看到什么」。

    流程文档里「写断言前先写反例」这一条，靠自觉是守不住的：新加一个不变量文件时，
    最省事的写法是直接堆 assert，然后在心里知道它防什么。半年后没人记得心里想的是什么，
    而这条断言只剩一个不会被人质疑的绿色勾。

    所以这里要求文件 docstring 里出现「改坏」和「用户」——不是为了措辞好看，
    是为了强制作者写下**失效形态**。写不出失效形态的断言，本来就不该存在。
    """
    violations: list[str] = []
    for path in (ROOT / "tests" / "invariants").glob("test_*.py"):
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
        missing = [word for word in ("改坏", "用户") if word not in doc]
        if missing or len(doc) < 40:
            violations.append(f"{path.name}（缺 {missing or '内容过短'}）")
    assert not violations, f"不变量文件未写明失效形态：{violations}"


def _is_artifact(path: Path) -> bool:
    """构建产物与环境目录不是包：隐藏目录、缓存目录、egg-info。"""
    return path.name.startswith((".", "__pycache__")) or path.name.endswith(".egg-info")


def _is_not_package(path: Path) -> bool:
    rel = str(path.relative_to(ROOT)).replace("\\", "/")
    return any(rel == d or rel.startswith(d + "/") for d in NON_PACKAGE_DIRS)
