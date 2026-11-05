#!/usr/bin/env python
"""D4 变异测试：给 P0 模块注入缺陷，检查测试能不能红。

    python scripts/mutation.py                # 全量
    python scripts/mutation.py --list         # 只列出注入点
    python scripts/mutation.py --only id-     # 按 id 前缀筛选
    python scripts/mutation.py --dry-run      # 只校验注入点能否定位，不跑测试

**为什么不用 mutmut / cosmic-ray**：那两个工具对全仓生成 mutant，700+ 条测试 ×
数百个 mutant 是小时级，而且大多数 mutant 落在与 P0 无关的分支上。验收计划 D4 的
退出条件是「P0 模块注入故障全被抓」，所以这里是**按缺陷类手挑 20 个 mutant**，
每个都写明「用户会看到什么」并指派最相关的测试子集——分钟级跑完，每条结论
都能被追到具体断言。

**判据不是「测试绿」，而是「测试必须红」**：
- 基线先跑一遍，必须全绿——基线不绿时，后面的红无法归因给 mutant
- 每个 mutant 单独注入 → 跑它指派的测试 → 恢复文件 → 校验恢复后的字节与原文件一致
- 退出码非 0 = killed（测试抓住了）；0 = survived（测试没牙齿，是缺陷）

**为什么在真源码上改而不是复制一份**：本仓是 editable 安装，pytest 从
`src/agentworkbench` 导入；复制一份改掉再跑，测的还是原模块。所以流程是
「快照 → 改 → 跑 → finally 恢复 → 校验字节」。快照而不是 git checkout：
工作区可能有未提交的改动，checkout 会把它们一起冲掉。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
REPORT_DIR: Final[Path] = ROOT / "reports" / "acceptance" / date.today().isoformat()

# 每个 mutant 的责任测试。用文件而不是节点 id：改动测试函数名时不会失效，
# 而「这条 mutant 由哪个文件负责」本来就比「哪一行」更稳定。
IDENTITY_POLICY = ("tests/invariants/test_identity_policy.py",)
IDENTITY_HTTP = ("tests/invariants/test_identity_endpoint.py",)
ISOLATION = ("tests/invariants/test_tenant_isolation.py",)
AUTH_LIFECYCLE = ("tests/acceptance/test_p0_auth_lifecycle.py",)
DELETE = ("tests/invariants/test_delete_compensation.py",)
TRACE = ("tests/invariants/test_recorder.py", "tests/invariants/test_run_state.py")
RETRIEVAL = ("tests/invariants/test_retriever.py",)
GUARD = ("tests/invariants/test_veto.py", "tests/invariants/test_sufficiency.py")
DB_SCHEMA = ("tests/invariants/test_index_consistency.py",)


@dataclass(frozen=True)
class Mutant:
    """一个注入点。`old` 必须**恰好出现一次**——出现零次是选错行，两次是改错地方。"""

    id: str
    area: str
    path: str
    old: str
    new: str
    defect: str
    tests: tuple[str, ...]


# ── 注入点：按验收计划的五个 P0 模块分组 ──────────────────────────────────
#
# 每条只改一处、只改一行语义，且都是「功能看起来照常、边界悄悄破了」的形态——
# 那正是最需要测试有牙齿的地方。简单的 `raise` 改写成 `pass` 不用测：
# 它会让几十条测试一起红，证明不了「这条判据有人守」。
MUTANTS: Final[tuple[Mutant, ...]] = (
    # ── identity：越权与认证生命周期 ──
    Mutant(
        id="id-tenant-scope",
        area="identity",
        path="src/agentworkbench/identity/policy.py",
        old="    if not resource.has_owner or resource.tenant_id != principal.owner.tenant_id:",
        new="    if not resource.has_owner:",
        defect="团队归属判定失效：拿真实 id 可以读写别的团队的文档/会话（越权）",
        tests=IDENTITY_POLICY + ISOLATION + AUTH_LIFECYCLE,
    ),
    Mutant(
        id="id-credential-epoch",
        area="identity",
        path="src/agentworkbench/identity/policy.py",
        old="    if ticket.credential_epoch != account.credential_epoch:",
        new="    if False:",
        defect="改密/重置后旧票据仍可用：另一台设备上被吊销的会话继续有效",
        tests=IDENTITY_POLICY + AUTH_LIFECYCLE + IDENTITY_HTTP,
    ),
    Mutant(
        id="id-disabled-account",
        area="identity",
        path="src/agentworkbench/identity/policy.py",
        old=(
            "    if account.status is AccountStatus.DISABLED:\n"
            "        return _deny(Outcome.UNAUTHENTICATED, ReasonCode.ACCOUNT_DISABLED, principal)"
        ),
        new=(
            "    if False:\n"
            "        return _deny(Outcome.UNAUTHENTICATED, ReasonCode.ACCOUNT_DISABLED, principal)"
        ),
        defect="被停用的账户继续能访问：管理员点了停用，用户照常干活",
        tests=IDENTITY_POLICY + AUTH_LIFECYCLE,
    ),
    Mutant(
        id="id-self-disable",
        area="identity",
        path="src/agentworkbench/identity/policy.py",
        old="    if target_user_id == actor.owner.user_id:",
        new="    if False:",
        defect="管理员可以停用自己/改自己角色：唯一管理员把自己锁在门外",
        tests=IDENTITY_POLICY + IDENTITY_HTTP + AUTH_LIFECYCLE,
    ),
    Mutant(
        id="id-session-ttl",
        area="identity",
        path="src/agentworkbench/identity/sessions.py",
        old="            if expires_at <= self._clock():",
        new="            if False:",
        defect="会话缓存永不过期：停用/改密后旧 cookie 无限期可用，配置窗口形同虚设",
        tests=("tests/invariants/test_global_gate.py", "tests/invariants/test_identity_service.py"),
    ),
    # ── ingest/delete：删除残留 ──
    Mutant(
        id="del-retry-state",
        area="ingest-delete",
        path="src/agentworkbench/ingest/deletion.py",
        old="    if status is not DocStatus.DELETE_PENDING:",
        new="    if status is DocStatus.DELETE_PENDING:",
        defect="重试删除直接抛非法迁移：用户点第二次删除拿到错误，资料永远删不掉",
        tests=DELETE,
    ),
    Mutant(
        id="del-residue-check",
        area="ingest-delete",
        path="src/agentworkbench/ingest/deletion.py",
        old="    if ports.count_fts(doc_id) or ports.count_vectors(doc_id):",
        new="    if False:",
        defect="索引清不干净也继续删记录：列表看不见、检索仍能命中的幽灵资料",
        tests=DELETE,
    ),
    Mutant(
        id="del-vector-clear",
        area="ingest-delete",
        path="src/agentworkbench/ingest/deletion.py",
        old="    ports.clear_vectors(doc_id)",
        new="    pass",
        defect="只清全文不清向量：删掉的资料还能被语义检索命中并引用",
        tests=DELETE + DB_SCHEMA,
    ),
    # ── trace/run：审计与状态机 ──
    Mutant(
        id="trace-terminal-step",
        area="trace-run",
        path="src/agentworkbench/trace/recorder.py",
        old="        if status is not RunStatus.RUNNING:",
        new="        if False:",
        defect="终态之后还能追加步骤：执行记录可以被事后续写，回放不再可信",
        tests=TRACE,
    ),
    Mutant(
        id="trace-double-start",
        area="trace-run",
        path="src/agentworkbench/trace/recorder.py",
        old=(
            "        if self._run_id is not None:\n"
            '            raise RuntimeError("这条 Recorder 已经 start 过：一次执行一个 run")'
        ),
        new=(
            "        if False:\n"
            '            raise RuntimeError("这条 Recorder 已经 start 过：一次执行一个 run")'
        ),
        defect="一次执行能起两个 run：同一次问答在记录里变成两条，对账与用量统计翻倍",
        tests=TRACE,
    ),
    Mutant(
        id="run-terminal-transition",
        area="trace-run",
        path="src/agentworkbench/core/runs.py",
        old="    return target in ALLOWED_TRANSITIONS[source]",
        new="    return True",
        defect="任何状态都能迁到任何状态：failed 可改写成 succeeded，审计结论被篡改",
        tests=(*TRACE, "tests/invariants/test_run_survives_kill.py"),
    ),
    # ── rag/guard：检索越权与拒答 ──
    Mutant(
        id="rag-tenant-filter-text",
        area="rag-guard",
        path="src/agentworkbench/rag/indexer.py",
        old=(
            '        " where chunks_fts match ?"\n'
            '        " and document_id in (select id from documents where tenant_id = ?)"'
        ),
        new=(
            '        " where chunks_fts match ?"\n'
            '        " and document_id in (select id from documents where ? is not null)"'
        ),
        defect="全文路主查询不按团队过滤：检索结果里混进别的团队的正文片段",
        tests=RETRIEVAL + ISOLATION,
    ),
    Mutant(
        id="rag-tenant-filter-like",
        area="rag-guard",
        path="src/agentworkbench/rag/indexer.py",
        old=(
            '        f"select chunk_id, document_id from chunks_fts'
            ' where {clauses}"  # noqa: S608\n'
            '        " and document_id in (select id from documents where tenant_id = ?)"'
        ),
        new=(
            '        f"select chunk_id, document_id from chunks_fts'
            ' where {clauses}"  # noqa: S608\n'
            '        " and document_id in (select id from documents where ? is not null)"'
        ),
        defect="单字兜底路不按团队过滤：搜一个「泵」字就能读到别的团队的片段",
        tests=RETRIEVAL + ISOLATION,
    ),
    Mutant(
        id="rag-tenant-filter-vector",
        area="rag-guard",
        path="src/agentworkbench/rag/retriever.py",
        old='        " join documents d on d.id = v.DocumentId and d.tenant_id = ?"',
        new='        " join documents d on d.id = v.DocumentId and ? is not null"',
        defect="向量路不按团队过滤：语义检索把别的团队的内容喂给模型并引用",
        tests=RETRIEVAL + ISOLATION,
    ),
    Mutant(
        id="guard-never-veto",
        area="rag-guard",
        path="src/agentworkbench/guard/veto.py",
        old='    if result.judgment.verdict.value != "insufficient":',
        new="    if True:",
        defect="资料不足也不拒答：模型拿几段不相关的内容硬编一个答案",
        tests=GUARD,
    ),
    Mutant(
        id="guard-margin-off",
        area="rag-guard",
        path="src/agentworkbench/guard/sufficiency.py",
        old="    if options.refuse_margin is not None and margin < options.refuse_margin:",
        new="    if False:",
        defect="间隔阈值失效：库外问题被判成「资料够」，拒答率回到 0",
        tests=GUARD,
    ),
    # ── db：结构、凭据、备份 ──
    Mutant(
        id="db-foreign-keys",
        area="db",
        path="src/agentworkbench/db/engine.py",
        old='    conn.execute("pragma foreign_keys = on")',
        new='    conn.execute("pragma foreign_keys = off")',
        defect="外键约束不生效：能写进指向不存在租户/run 的行，删除留下孤儿",
        # 只挂约束测试：pragma 是连接级开关，这一条直接断言它的值。
        # 挂上 identity_store 之类的大文件只会把单次运行拖到 5 分钟，不多抓一个缺陷。
        tests=("tests/invariants/test_db_constraints.py",),
    ),
    Mutant(
        id="db-consistency-empty",
        area="db",
        path="src/agentworkbench/db/consistency.py",
        old="    if docs == 0 or searchable == 0:",
        new="    if False:",
        defect="空库一致性检查静默通过：其余五类在空库上全部空转，报告看起来干净",
        tests=DB_SCHEMA,
    ),
    Mutant(
        id="db-missing-secret",
        area="db",
        path="src/agentworkbench/db/crypto.py",
        old="    if not secret:\n        raise MissingSecretKey(MISSING_SECRET)",
        new='    if not secret:\n        return ""',
        defect="缺密钥不再拒绝启动：凭据以可预测密钥加密（等于明文）落库",
        tests=("tests/invariants/test_provider_config.py",),
    ),
    Mutant(
        id="db-restore-unvalidated",
        area="db",
        path="src/agentworkbench/db/backup.py",
        old="    _validate(backup_db)",
        new="    pass",
        defect="恢复不先验备份：损坏备份先把 files/ 拷进目标再失败，用户的好数据被污染",
        tests=("tests/invariants/test_backup.py",),
    ),
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _run_pytest(tests: tuple[str, ...], timeout: float = 600.0) -> tuple[int | None, float, str]:
    """跑一组测试。返回 (退出码, 用时秒, 输出尾部)。超时返回 (None, 用时, 说明)。"""
    started = time.monotonic()
    # `--basetemp` 必须显式给且**每次唯一**：pytest 默认把临时目录堆在系统 %TEMP%，
    # 每次运行还要清理历史垃圾。那一步在带删除守卫的机器上会被拦，进程在
    # 全部测试通过之后才非 0 退出——而「退出码非 0」在本脚本里等于
    # 「mutant 被抓」，正好是最危险的假阳性（把环境问题记成测试有牙齿）。
    #
    # 落在 `reports/`（gitignore）而不是系统盘：一次全量变异会产生 20 个临时目录，
    # 堆在 C 盘上会把盘吃满（踩过一次：C 盘 0 字节，SQLite 直接 OperationalError，
    # 表现为「基线不绿」，看起来像代码问题）。这里不自动删：删除守卫会连
    # 脚本自己的清理一起拦，且 reports 本来就是可丢弃的。
    basetemp = REPORT_DIR / "tmp" / f"run-{os.getpid()}-{int(time.time() * 1000)}"
    basetemp.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *tests,
                "-x",
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
                f"--basetemp={basetemp}",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, time.monotonic() - started, f"超时（>{timeout:.0f}s）"
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-6:]
    return proc.returncode, time.monotonic() - started, "\n".join(tail)


def _classify(code: int | None, output: str) -> str:
    """把一次运行判成 killed / survived / invalid。

    **非 0 不等于 killed**：进程可能被外部杀死（超时、删除守卫、OOM），
    那种情况没有任何测试失败证据。判据是「输出里真的出现了失败」——
    pytest 的失败摘要一定含 `failed` / `ERROR` / `assert`。宁可把一次
    可疑运行记成 invalid 并让报告变红，也不能把环境问题算成测试的功劳。
    """
    if code is None:
        return "invalid"
    if code == 0:
        return "survived"
    lowered = output.lower()
    if "failed" in lowered or "error" in lowered or "assert" in lowered:
        return "killed"
    return "invalid"


def _check_snippet(path: Path, mutant: Mutant) -> str | None:
    """返回错误说明；None = 注入点可用。"""
    if not path.is_file():
        return f"文件不存在：{mutant.path}"
    text = path.read_text(encoding="utf-8")
    count = text.count(mutant.old)
    if count == 1:
        return None
    return f"注入点出现 {count} 次（必须恰好 1 次）：{mutant.id}"


def _apply(path: Path, mutant: Mutant) -> bytes:
    """注入后的**字节**。换行风格按原文件保留。

    不能直接 `write_text`：Windows 上它会把 `\\n` 翻成 `\\r\\n`，于是恢复之后
    文件字节与原来不同（内容一样、eol 不一样），工作区平白多出 5 个「已修改」
    条目——`git diff` 还是空的，看起来像 git 坏了。踩过一次，所以这里全程走字节。
    """
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    crlf = "\r\n" in text
    normalized = text.replace("\r\n", "\n")
    mutated = normalized.replace(mutant.old, mutant.new, 1)
    if crlf:
        mutated = mutated.replace("\n", "\r\n")
    return mutated.encode("utf-8")


def _report(results: list[dict[str, object]], baseline: dict[str, object], dirty: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": date.today().isoformat(),
        "python": sys.version.split()[0],
        "worktree_dirty": dirty,
        "baseline": baseline,
        "results": results,
    }
    json_path = REPORT_DIR / "d4-mutation.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    killed = sum(1 for row in results if row["verdict"] == "killed")
    survived = [row for row in results if row["verdict"] == "survived"]
    lines = [
        "# D4 变异测试报告",
        "",
        f"- 日期：{date.today().isoformat()}",
        f"- Python：{sys.version.split()[0]}",
        f"- 注入点：{len(results)} 个，killed {killed}，survived {len(survived)}",
        f"- 基线：{'全绿' if baseline['ok'] else '不绿（结论不可归因）'}",
        "",
        "判据：注入缺陷后指派的测试**必须红**。绿 = 测试没牙齿，是缺陷不是通过。",
        "",
        "| id | 区域 | 注入的缺陷（用户会看到什么） | 结果 | 用时 |",
        "|---|---|---|---|---|",
    ]
    for row in results:
        verdict = row["verdict"]
        mark = {"killed": "KILLED", "survived": "**SURVIVED**", "invalid": "INVALID"}[verdict]
        lines.append(
            f"| `{row['id']}` | {row['area']} | {row['defect']} | {mark} | {row['seconds']:.1f}s |"
        )
    if survived:
        lines += ["", "## 未被抓住的注入点", ""]
        for row in survived:
            lines += [
                f"### `{row['id']}`",
                "",
                f"- 缺陷：{row['defect']}",
                f"- 注入：`{row['path']}`",
                "- 测试输出尾部：",
                "",
                "```",
                str(row["output"]),
                "```",
                "",
            ]
    md_path = REPORT_DIR / "d4-mutation.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="D4 变异测试：P0 模块注入缺陷，测试必须红")
    parser.add_argument("--list", action="store_true", help="只列出注入点")
    parser.add_argument("--only", default="", help="按 id 前缀筛选（可多次给逗号分隔）")
    parser.add_argument("--dry-run", action="store_true", help="只校验注入点能否定位，不跑测试")
    parser.add_argument("--skip-baseline", action="store_true", help="跳过基线（不推荐）")
    args = parser.parse_args(argv)

    selected = MUTANTS
    if args.only:
        prefixes = tuple(item.strip() for item in args.only.split(",") if item.strip())
        selected = tuple(m for m in MUTANTS if m.id.startswith(prefixes))
        if not selected:
            sys.exit(f"没有匹配的注入点：{args.only}")

    if args.list:
        for mutant in selected:
            print(f"{mutant.id:<24} {mutant.area:<14} {mutant.path}")
        return 0

    # 注入点必须能唯一定位。定位不了是脚本/代码漂移，不是「测试没抓住」。
    problems = {m.id: _check_snippet(ROOT / m.path, m) for m in selected}
    broken = {mid: why for mid, why in problems.items() if why}
    for mid, why in broken.items():
        print(f"[注入点无效] {mid}: {why}")
    if broken and not args.dry_run:
        sys.exit("注入点无效，先修注入点（多半是代码改动后旧片段失效）")
    if args.dry_run:
        print(f"注入点校验：{len(selected) - len(broken)}/{len(selected)} 可用")
        return 1 if broken else 0

    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if dirty:
        print("注意：工作区有未提交改动。本脚本按快照恢复，不会冲掉它们；")
        print("      但运行期间请不要同时编辑被注入的文件。")

    # 快照：恢复时不看 git，只看进内存的那份字节。
    snapshots = {m.id: (ROOT / m.path).read_bytes() for m in selected}

    baseline: dict[str, object] = {"ok": True, "seconds": 0.0, "output": ""}
    if not args.skip_baseline:
        union = tuple(dict.fromkeys(test for m in selected for test in m.tests))
        code, seconds, output = _run_pytest(union)
        baseline_verdict = _classify(code, output)
        baseline = {
            "ok": baseline_verdict == "survived",
            "seconds": round(seconds, 1),
            "output": output,
        }
        print(
            f"基线：{'全绿' if baseline_verdict == 'survived' else '不绿'}"
            f"（{seconds:.1f}s，{len(union)} 个测试路径）"
        )
        if baseline_verdict != "survived":
            print(output)
            reason = (
                "基线进程非 0 但没有任何失败证据（多半是外部杀死/守卫拦截），结果无法归因"
                if baseline_verdict == "invalid"
                else "基线不绿"
            )
            sys.exit(f"{reason}：先修基线，否则任何红都说明不了问题")

    results: list[dict[str, object]] = []
    print(f"\n{'id':<24}{'区域':<14}{'结果':<10}{'用时':>7}")
    print("─" * 60)
    try:
        for mutant in selected:
            path = ROOT / mutant.path
            original = snapshots[mutant.id]
            if _sha(path.read_bytes()) != _sha(original):
                print(f"{mutant.id:<24}{mutant.area:<14}{'跳过':<10}  文件在运行期间被改动")
                results.append(
                    {
                        "id": mutant.id,
                        "area": mutant.area,
                        "path": mutant.path,
                        "defect": mutant.defect,
                        "verdict": "invalid",
                        "seconds": 0.0,
                        "output": "文件在运行期间被外部改动",
                    }
                )
                continue
            path.write_bytes(_apply(path, mutant))
            try:
                code, seconds, output = _run_pytest(mutant.tests)
            finally:
                path.write_bytes(original)
                restored = path.read_bytes()
                if _sha(restored) != _sha(original):
                    sys.exit(f"恢复失败：{mutant.path} 与快照不一致，停止（先手工恢复）")

            verdict = "invalid" if code is None else _classify(code, output)
            results.append(
                {
                    "id": mutant.id,
                    "area": mutant.area,
                    "path": mutant.path,
                    "defect": mutant.defect,
                    "verdict": verdict,
                    "seconds": round(seconds, 1),
                    "output": output,
                }
            )
            mark = {"killed": "KILLED", "survived": "SURVIVED", "invalid": "INVALID"}[verdict]
            print(f"{mutant.id:<24}{mutant.area:<14}{mark:<10}{seconds:>6.1f}s")
    finally:
        # 兜底：无论上面怎么退出，被注入的文件必须回到快照。
        for mutant in selected:
            path = ROOT / mutant.path
            if path.read_bytes() != snapshots[mutant.id]:
                path.write_bytes(snapshots[mutant.id])

    md_path = _report(results, baseline, dirty)
    killed = sum(1 for row in results if row["verdict"] == "killed")
    survived = [row for row in results if row["verdict"] == "survived"]
    invalid = [row for row in results if row["verdict"] == "invalid"]
    print("─" * 60)
    print(f"killed {killed}/{len(results)}；报告：{md_path.relative_to(ROOT)}")
    if survived:
        print("未被抓住：" + ", ".join(str(row["id"]) for row in survived))
        print("这些是**测试的缺口**：要么补断言，要么承认该判据没人守（先记进台账 remaining）。")
    if invalid:
        print("结果不可用：" + ", ".join(str(row["id"]) for row in invalid))
        print("这些运行非 0 但没有失败证据（外部杀死/守卫拦截），不能算 killed。")
    return 1 if (survived or invalid) else 0


if __name__ == "__main__":
    raise SystemExit(main())
