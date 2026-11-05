"""repro 脚本的共用件：一条断言一行结果，退出码即结论。

约定（所有 `rNN_*.py` 一致）：

- 每条 `check(...)` 打印 `通过 / 不通过`，**通过 = 系统挡住了**。
- 退出码 0 = 全部挡住；1 = 至少一条能复现攻击；2 = 环境没起来。
- 每个脚本自己起一台实例（`redteam_env.Env`），跑完即弃，不碰生产 `data/`。

为什么要有这一层：D9 的退出条件是「每个 repro 你亲手跑过」，所以每条发现都得有
一个**不需要读报告、不需要懂代码**就能执行的入口。脚本自己准备环境和账号，
输出是给人看的中文，而不是 pytest 的缩写节点名。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


@dataclass
class Outcome:
    label: str
    ok: bool
    detail: str
    note: str = ""


@dataclass
class Report:
    title: str
    outcomes: list[Outcome] = field(default_factory=list)

    def check(self, label: str, ok: bool, detail: str, *, note: str = "") -> bool:
        self.outcomes.append(Outcome(label, ok, detail, note))
        mark = "通过" if ok else "不通过"
        print(f"  [{mark}] {label}\n           {detail}")
        if note:
            print(f"           注：{note}")
        return ok

    def finish(self) -> int:
        bad = [item for item in self.outcomes if not item.ok]
        print(f"\n{self.title}：{len(self.outcomes) - len(bad)}/{len(self.outcomes)} 挡住")
        if bad:
            print("仍然能复现的：")
            for item in bad:
                print(f"  - {item.label}：{item.detail}")
            return 1
        print("全部挡住（这一条不再是缺陷）。")
        return 0


def main(title: str, body: object) -> int:
    """跑 `body(report)`，异常也转成结论而不是栈。"""
    report = Report(title)
    try:
        body(report)  # type: ignore[operator]
    except Exception as exc:
        print(f"\n环境没起来或脚本出错：{type(exc).__name__}: {exc}")
        return 2
    return report.finish()
