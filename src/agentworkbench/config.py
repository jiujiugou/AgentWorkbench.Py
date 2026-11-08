"""配置：环境变量与默认值的唯一入口。

只依赖标准库与 pydantic-settings，不 import 本项目任何包。
所有包都可以读它，它不读任何包。

**缺项按类型报错，不变成 `None`**——这是选 pydantic-settings 而不是手写
`os.environ.get` 的唯一理由。手写版在配置读错时不会失败，只会让某个功能
静默用默认值跑，半年后表现为“这个开关好像没生效”。

## 配置地图

- **Storage**（C# `StorageOptions`）：`data_dir`、`max_upload_bytes`、`allowed_suffixes`
  - 环境变量：`AWB_DATA_DIR` 等
  - 消费者：`api/deps.py`、`api/routes/documents.py`
  - 配错：库/上传文件找不到，或上传全拒
- **Auth**（C# `IdentityOptions` / Webapi 认证）：`auth.*`
  - 环境变量：`AWB_AUTH__*`
  - 消费者：`identity/service.py`、`api/deps.py`
  - 配错：锁定、票据、会话失效行为错
- **Knowledge**（C# `KnowledgeOptions`）：`refuse_margin`、`sufficient_distance`、
  `coverage_threshold`、`embedding.*`
  - 环境变量：`AWB_REFUSE_MARGIN` 等
  - 消费者：`api/answer_flow.py`、`api/deps.py`
  - 配错：误拒/误答，或退化成占位嵌入
- **Server**（C# `Webapi`）：`tenant_id`、`cors_origins`、`web_dist`
  - 环境变量：`AWB_CORS_ORIGINS`、`AWB_WEB_DIST`
  - 消费者：`api/app.py`、`cli.py`
  - 配错：跨域失败，或页面 404
- **Security**（C# `Webapi`）：`secret_key`
  - 环境变量：`AWB_SECRET_KEY`
  - 消费者：`api/deps.py`、`api/routes/settings.py`
  - 配错：未配置时拒绝启动

## 读法

先看地图，再按节读 `Settings`；字段旁只留「消费者 / 配错后果」。
完整取舍与标定数据在文末「设计说明」，改阈值、密钥或白名单时再读。

来源：C# `Shared/Options/*.cs` + `appsettings.json`
服务于：REQ-22（配置只有一个入口）
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# 两个子模型先定义：`Settings` 在类体里用 `default_factory` 引用它们。
# ── Auth 子模型 ────────────────────────────────────────────────────
class AuthSettings(BaseModel):
    """身份与访问的五个旋钮（F1 §1.8）。环境变量：`AWB_AUTH__*`。

    约束写在类型上，而不是启动后校验：这几个值配错后的形态都是
    「功能悄悄消失」——`login_max_failures=0` 是永不锁定，
    `ticket_ttl_hours=0` 是票据一签发就过期，两者都不会报错。
    """

    # 0 = 不缓存、吊销立即生效（D9 由 60 改为 0）。
    session_invalidate_seconds: int = Field(default=0, ge=0)
    # 连续失败达到该次数后锁定。
    login_max_failures: int = Field(default=5, ge=1)
    # 锁定时长（秒）。
    lockout_seconds: int = Field(default=900, ge=1)
    # 登录票据有效期（小时）。
    ticket_ttl_hours: int = Field(default=12, ge=1)
    # 生产 HTTPS 部署应置 true。
    cookie_secure: bool = False


# ── Knowledge · Embedding 子模型 ────────────────────────────────────
class EmbeddingSettings(BaseModel):
    """向量化端点（OpenAI 兼容 `/embeddings`）。环境变量：`AWB_EMBEDDING__*`。

    三项全空是合法状态：表示“没配”，装配层退回占位嵌入 `HashEmbedder`，
    并在启动日志里明确写出实际模式。
    """

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_seconds: float = Field(default=20.0, gt=0)
    batch_size: int = Field(default=32, ge=1)

    @property
    def configured(self) -> bool:
        """地址与模型都在才算配了；只有 key 或只有地址都按“没配”处理。"""
        return bool(self.base_url.strip() and self.model.strip())


class Settings(BaseSettings):
    """全项目唯一的配置来源。环境变量前缀 `AWB_`，例如 `AWB_DATA_DIR`。

    下面按 C# 的配置节分段；每个字段旁只写消费者和配错后果。
    """

    # 环境变量前缀 AWB_；子模型用双下划线，例如 AWB_AUTH__LOGIN_MAX_FAILURES。
    # extra="ignore" 会让拼错的键静默忽略，这是排查“配了不生效”的第一现场。
    model_config = SettingsConfigDict(
        env_prefix="AWB_",
        env_file=".env",
        extra="ignore",
        env_nested_delimiter="__",
    )

    # ── Storage：数据根、上传与库文件（C# StorageOptions） ─────────────
    # 数据根：库文件与上传文件都在它下面，换机器只需换这一个值。
    data_dir: Path = Path("data")

    @property
    def db_path(self) -> Path:
        """SQLite 库文件：`<data_dir>/agentworkbench.db`。"""
        return self.data_dir / "agentworkbench.db"

    @property
    def storage_dir(self) -> Path:
        """上传原件目录：`<data_dir>/files`。"""
        return self.data_dir / "files"

    # 与前端 MAX_FILE_BYTES 对齐（30MB）；改这里必须同步改前端。
    max_upload_bytes: int = 30 * 1024 * 1024

    # 上传白名单。这里是生产真源（上传路由读 suffix_set）；
    # ingest/upload.py 的常量只是测试与文档引用点。
    allowed_suffixes: str = ".docx,.pdf,.xlsx"

    @property
    def suffix_set(self) -> frozenset[str]:
        """去空白、统一小写、补前导点；格式写错得到空集，上传会全拒。"""
        items = (item.strip().lower() for item in self.allowed_suffixes.split(","))
        normalized = {item if item.startswith(".") else f".{item}" for item in items if item}
        return frozenset(normalized)

    # ── Auth：身份与访问（C# Identity / Webapi auth） ─────────────────
    # 五个旋钮的约束与默认值见 AuthSettings。
    auth: AuthSettings = Field(default_factory=AuthSettings)

    # ── Knowledge：检索、判定与引用（C# KnowledgeOptions） ────────────
    # 判定阈值。标定依据见文末「设计说明 · 判定阈值」。
    refuse_margin: float = 0.05
    sufficient_distance: float = 0.45
    # None = 没定标：只记录覆盖率，不把引用判成“指向错块”。
    coverage_threshold: float | None = None

    # 向量化端点；为什么凭据走环境变量而不是 providers 表见文末。
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)

    # ── Server：HTTP、CORS 与前端托管（C# Webapi） ───────────────────
    # 阶段 1 的遗留常量：只用于存量数据绑定，不再参与权限判定。
    tenant_id: str = "default"

    # vite dev server 的源；生产同源托管时这段不生效。
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def origin_set(self) -> list[str]:
        """逗号分隔的 CORS 源，去掉空项。"""
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    # None = 不托管前端（纯 API / 开发态）；目录不存在时服务照常启动。
    web_dist: Path | None = None

    # ── Security：凭据加密（C# Webapi） ──────────────────────────────
    # None 是刻意默认：build_deps 会拒绝启动，而不是静默用明文。
    secret_key: str | None = None


# ─────────────────────────────────────────────────────────────
# 设计说明（按需读）
# ─────────────────────────────────────────────────────────────
#
# Auth：
# - session_invalidate_seconds 默认 0（D9 由 60 改为 0）。本地 SQLite 上一次判定
#   是几十微秒，而 60 秒窗口的代价是“被停用的管理员在一个窗口内还能改别人的
#   角色”——窗口是临时的，那次改角色的结果不是。要窗口就显式配，并接受
#   “吊销最多滞后 N 秒”的语义（identity/sessions.py 里写清了）。
# - 这些值配错后的共同形态是“功能悄悄消失”，所以约束写在 Field(ge=...) 上。
#
# Knowledge · 判定阈值：
# - refuse_margin = 第 2~5 名平均距离 − 第一名 的下限；低于它判“资料不足”。
# - sufficient_distance = 第一名距离的上限；够近才算答得上。
# - 2026-11-05（D8）用 28 道真实题在 bge-m3 上标定：可答题首名 0.266~0.406
#   （中位 0.338），库外题 0.381~0.703；两个阈值都无法单独分开两类。
#   0.05 / 0.45 是在“误答优先于误拒”的产品立场下选的，证据见
#   reports/acceptance/2026-11-05/d8-quality.md §阈值扫描。
# - 换嵌入模型必须重新标定：距离分布会整体平移，抄过来的阈值既不报错也不准。
# - coverage_threshold 默认 None 是合法的：表示“没定标、只记数值不判”（D-12）。
#   一直为 None 意味着 support_verdict 永远是 undetermined，界面上“没有出处可核”
#   的警告永远不会出现——定标是 T 系列评测的活。
#
# Knowledge · Embedding：
# - 三项全空是合法状态：装配层退回 HashEmbedder 并写 WARNING。占位嵌入的语义
#   相似度是假的（sha256 派生），语义检索会退化成随机邻居。
# - 只有 key 或只有地址都按“没配”处理：半配置去发请求会让每份文档都标
#   index_transient，看起来像上游挂了，实际是配置少填一半。
# - 为什么凭据走环境变量而不是 providers 表：providers 是聊天供应商的配置面
#   （有一行激活、有默认模型），向量化是单例、全局、建索引时才用。长期该并进
#   容量更大的那张表，不是现在。
# - 为什么维度不在这里：vec0 的维度由 core/vectors.EMBEDDING_DIM 在建表时固定，
#   配一个对不上的数字只会让每次写索引都失败。维度是常量不是旋钮。
#
# Storage · 上传：
# - max_upload_bytes 与前端 MAX_FILE_BYTES 对齐；改这里必须同步改前端，否则
#   用户会在选完文件之后才被拒。
# - allowed_suffixes 是生产真源；ingest/upload.py 的 DEFAULT_ALLOWED_SUFFIXES
#   只是测试与文档的引用点。两处曾各改各的，HTTP 上传仍被拒而测试全绿；一致性
#   由 test_office_parser.py::test_settings_default_matches_the_whitelist 锁。
#
# Server：
# - tenant_id 是阶段 1 的单租户常量。F1 完成后请求侧用登录身份，它只剩“存量
#   数据绑定”一个用途，不再参与任何判定。
# - web_dist=None 表示不托管前端；serve 默认指向 ./web/dist，不存在则日志警告，
#   API 照常启动（F6 §1.11）。
#
# Security：
# - secret_key 默认 None 是刻意的：build_deps 拿到 None 直接拒绝启动——静默用
#   明文正是 C# M006「P2 再加密」注释的下场。生产用 AWB_SECRET_KEY 提供。
