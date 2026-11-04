"""本地演示后端：假模型 + 种子语料，不需要 API key。

    python -m uvicorn scripts.demo_app:app --port 8080

用途只有一个：让前端在**没有供应商配置**的情况下也能看到真实渲染
（答案、角标、来源面板、无出处警告）。它不改任何产品代码，`Deps.chat_factory`
是为此留的注入点。

**不要拿它当"跑通了"的证据。** 真链路要过真模型：模型会不会按 [n] 标注、
标注准不准、覆盖率高不高，假模型一个都证明不了。这里的假回答是手写的。

种子语料故意做成"三层可判"：
- 块 1/2/3 覆盖 Modbus 线圈、H5U 报警、保养
- 演示问题命中块 2
- 假回答里 `[2]` 有出处、`[9]` 越界 → 界面上能看到角标被剥掉的效果
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import ClassVar

from agentworkbench.api.app import create_app
from agentworkbench.api.deps import build_deps
from agentworkbench.config import Settings
from agentworkbench.core.documents import ChunkDraft
from agentworkbench.db import engine
from agentworkbench.rag.indexer import write_index

DOC_ID = "demo-doc"
FILE_NAME = "汇川H5U系列PLC用户手册.pdf"

CHUNKS = [
    "FC01 读线圈功能码用于读取输出继电器的状态，地址范围 0x0000-0xFFFF。",
    "H5U 的 E2 报警是通讯超时，先检查波特率与站号设置，再看终端电阻。",
    "维护保养：每季度清洁散热风道并紧固接线端子，环境温度不超过 55℃。",
]


class DemoChat:
    """手写回答。`last_usage` 故意给一组像样的数字，让界面能显示用量。"""

    last_usage: ClassVar[dict[str, int | None]] = {
        "prompt_tokens": 912,
        "completion_tokens": 48,
        "total_tokens": 960,
    }

    def complete(self, system: str, user: str) -> str:
        # 编号要对着证据：E2 报警那块是 [1]（问题与它同一方向，排第一）。
        # 写错编号不是笔误——二级校验会当场判 unsupported 并让整段变成
        # "没有资料支撑"，这正是它该做的事。
        return (
            "E2 报警表示通讯超时，按下面顺序排查[1]。"
            "先确认波特率与站号是否和主站一致，再检查终端电阻[1]。"
            "如果参数都对，可以量一下 A/B 线之间的电压[9]。"
        )


def seed(conn: sqlite3.Connection) -> None:
    """建一份文档 + 三块语料。已存在就跳过——重复起服务不该重复插。"""
    if conn.execute("select 1 from documents where id = ?", (DOC_ID,)).fetchone():
        return

    now = "2026-11-02T00:00:00"
    conn.execute(
        "insert into documents(id, tenant_id, file_name, stored_path, sha256, size_bytes,"
        " status, page_count, created_at, updated_at)"
        " values (?,?,?,?,?,?,?,?,?,?)",
        (DOC_ID, "default", FILE_NAME, "seed/h5u.pdf", "sha-demo", 20480, "parsed", 3, now, now),
    )

    class Vec:
        """确定性假向量：第 i 块指向第 i 个正交基，问题用第 1 块的方向。"""

        def __call__(self, texts: list[str]) -> list[list[float]]:
            out = []
            for text in texts:
                idx = next((i for i, c in enumerate(CHUNKS) if c == text), 0)
                vec = [0.0] * 1024
                vec[idx] = 1.0
                out.append(vec)
            return out

    drafts = [
        ChunkDraft(seq=i, page_from=i + 1, page_to=i + 1, content=text)
        for i, text in enumerate(CHUNKS)
    ]
    write_index(conn, document_id=DOC_ID, drafts=drafts, embeddings=Vec()(CHUNKS), now=now)
    conn.commit()


class DemoEmbedder:
    """问题固定落在第 2 块的方向上（`CHUNKS[1]` 讲 E2 报警）。"""

    def __call__(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 1.0] + [0.0] * 1022 for _ in texts]


SETTINGS = Settings(
    # 独立目录，不复用 `data/`：那边可能是旧 schema 的库（少一列就插不进去），
    # 而演示数据本来也该和真数据分开。
    data_dir=Path("data/demo"),
    # 演示用：把二级阈值定到 0.2，让 support_verdict 能判成 supported。
    # 真实定标要跑评测，这里的数字只为把界面点亮。
    coverage_threshold=0.2,
    # S-V6：缺密钥启动失败是生产行为，演示脚本也不能豁免——但演示是本机单人场景，
    # 给一把固定演示密钥（真实部署必须用环境变量 AWB_SECRET_KEY，绝不写进代码）。
    secret_key="demo-local-secret-key",  # noqa: S106 —— 演示专用假密钥，不是真实凭据
)

DEPS = build_deps(SETTINGS)

with engine.connect(SETTINGS.db_path) as _conn:
    seed(_conn)

DEPS.embedder = DemoEmbedder()  # type: ignore[assignment]
DEPS.chat_factory = lambda _conn: (DemoChat(), "demo-model")

app = create_app(settings=SETTINGS, deps=DEPS)


__all__: list[str] = ["app"]
