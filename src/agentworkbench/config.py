"""配置：环境变量与默认值的唯一入口。

只依赖标准库与 pydantic-settings，不 import 本项目任何包。
所有包都可以读它，它不读任何包。

**缺项按类型报错，不变成 `None`**——这是选 pydantic-settings 而不是手写
`os.environ.get` 的唯一理由。手写版在配置读错时不会失败，只会让某个功能
静默用默认值跑，半年后表现为"这个开关好像没生效"。

来源：C# `Shared/Options/*.cs` + `appsettings.json`
服务于：REQ-22（配置只有一个入口）
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthSettings(BaseModel):
    """身份与访问的五个旋钮（F1 §1.8）。**每一项都有消费者**，没有纯摆设的键。

    放在一个子模型里而不是平铺：这五项全部只在身份域被读，
    平铺之后 `Settings` 会变成一张看不出归属的清单，而下一个域的
    `max_*` 还会跟这里的撞名。环境变量用双下划线：
    `AWB_AUTH__LOGIN_MAX_FAILURES`。

    约束写在类型上（`ge=`）而不是启动后校验：这几个值的错误形态都是
    「配成 0 之后功能悄悄消失」——`login_max_failures=0` 是永不锁定，
    `ticket_ttl_hours=0` 是票据一签发就过期，两者都不会报错。
    `session_invalidate_seconds` 允许 0，那是「不缓存、吊销立即生效」，
    测试拿确定性正是靠它（§1.5）。

    **`session_invalidate_seconds` 的默认值是 0（D9 由 60 改为 0）**：本地 SQLite 上
    一次判定是几十微秒，而 60 秒窗口的代价是「被停用的管理员在一个窗口内还能改
    别人的角色」——窗口是临时的，那次改角色的结果不是。要窗口就显式配，
    并接受「吊销最多滞后 N 秒」这个语义（`identity/sessions.py` 里写清了）。
    """

    session_invalidate_seconds: int = Field(default=0, ge=0)
    login_max_failures: int = Field(default=5, ge=1)
    lockout_seconds: int = Field(default=900, ge=1)
    ticket_ttl_hours: int = Field(default=12, ge=1)
    cookie_secure: bool = False


class EmbeddingSettings(BaseModel):
    """向量化端点的三要素 + 两个批次旋钮。环境变量 `AWB_EMBEDDING__BASE_URL` 等。

    **三项全空是合法状态**：那就是"没配"，装配层退回占位嵌入 `HashEmbedder`
    并在启动日志里明确写出来。它合法但不等于没差别——占位嵌入的语义相似度是假的
    （sha256 派生），语义检索会退化成随机邻居。所以装配层不是静默退回，
    而是落一条 WARNING 并让 `/api/settings/embedding` 显示实际的模式。
    """

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_seconds: float = Field(default=20.0, gt=0)
    batch_size: int = Field(default=32, ge=1)

    @property
    def configured(self) -> bool:
        """地址与模型都在才算配了。只有 key 或只有地址都按"没配"处理——
        半配置去发请求的失败形态是每份文档都标 `index_transient`，
        看起来像"上游挂了"，实际是配置少填一半。"""
        return bool(self.base_url.strip() and self.model.strip())


class Settings(BaseSettings):
    """全项目唯一的配置来源。环境变量前缀 `AWB_`，例如 `AWB_DATA_DIR`。"""

    # `env_nested_delimiter` 不是可选项：`AuthSettings` 的 docstring 从格 3 起
    # 就写着「环境变量用双下划线」，而没有这一行时 `AWB_AUTH__LOGIN_MAX_FAILURES=9`
    # **静默无效**——pydantic 只认 `AWB_AUTH='{"...": 9}'` 那种 JSON 形态。
    # 静默无效比报错难查：运维改了环境变量、重启、`Settings()` 打印出来还是默认值，
    # 唯一的现象是「配了不生效」。格 6 起 `session_invalidate_seconds` 也有消费者了，
    # 这个键必须真的能从环境变量进来。
    model_config = SettingsConfigDict(
        env_prefix="AWB_",
        env_file=".env",
        extra="ignore",
        env_nested_delimiter="__",
    )

    # 数据根：库文件与上传文件都在它下面，换机器只需换这一个值。
    data_dir: Path = Path("data")

    # 单租户常量。F1（身份与访问）已完成判定与存储两层；请求侧接入在格 5/6，
    # 那之后这个键只剩「存量数据绑定」一个用途，不再参与任何判定。
    tenant_id: str = "default"

    # 身份与访问（F1 §1.8）。前三个由 `identity/service.py` 读（登录与签发），
    # 后两个等路由层落地后读（cookie 属性 / 会话校验缓存）。
    auth: AuthSettings = Field(default_factory=AuthSettings)

    # 与前端 `MAX_FILE_BYTES` 对齐（30MB）。改这里必须同步改前端，否则
    # 用户会在选完文件之后才被拒。
    max_upload_bytes: int = 30 * 1024 * 1024

    # 逗号分隔。默认必须与 `ingest/upload.py` 的 `DEFAULT_ALLOWED_SUFFIXES`
    # 一致——**这里是生产真源**（上传路由读 `settings.suffix_set`），常量只是
    # 测试与文档的引用点。两处曾各改各的：常量加了 .docx/.xlsx，这里还是 .pdf，
    # HTTP 上传仍被拒而测试全绿（judge_upload 的默认参数走的是常量）。
    # 一致性由 `test_office_parser.py::test_settings_default_matches_the_whitelist` 锁。
    allowed_suffixes: str = ".docx,.pdf,.xlsx"

    # 判定阈值（F3 D-01：用间隔，不用绝对距离）。
    # `refuse_margin` = 「第 2~5 名平均距离 − 第一名」的下限，低于它就判不足；
    # `sufficient_distance` = 第一名距离的上限，够近才算答得上。
    #
    # 2026-11-05（D8）用 28 道真实题在 bge-m3 上标定过一次，取值与依据：
    # - 可答题首名距离 0.266~0.406（中位 0.338），间隔 0.012~0.154；
    # - 库外题首名 0.381~0.703，间隔 −0.021~0.092；
    # - **两个阈值都无法单独分开两类**（相邻领域的库外题比换个说法的可答题更近），
    #   网格扫描里没有「误拒 0 且误答 0」的格子。
    # 0.05 / 0.45 是在「误答优先于误拒」这一产品立场下选的：19 道可答题答出 13 道
    # （引用全部命中），9 道应拒答拦住 8 道，唯一漏过的那道模型自己也说了「资料不足」。
    # 证据 `reports/acceptance/2026-11-05/d8-quality.md` §阈值扫描。
    #
    # ⚠️ 换嵌入模型必须重新标定：这两个数在余弦距离尺度上有意义，换模型（甚至换
    # 维度）后距离分布整体平移，抄过来的阈值既不报错也不准。
    refuse_margin: float = 0.05
    sufficient_distance: float = 0.45

    # 二级校验（引用内容真的支持这句话吗）的覆盖率阈值。
    # **默认 None 是合法的**：表示"没定标、只记数值不判"（D-12）。
    # 但一直为 None 意味着 `support_verdict` 永远是 undetermined——
    # 界面上"这段话没有出处可核"的警告永远不会出现，而那正是产品最该说的话。
    # 定标是 T 系列评测的活；先让它可配，别让它看起来"已经判了"。
    coverage_threshold: float | None = None

    # vite dev server 的源。生产由同一个进程托管静态文件，不需要跨域。
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # 向量化端点（OpenAI 兼容 `/embeddings`）。
    #
    # 为什么凭据走环境变量而不是库里的 `providers` 表：`providers` 是**聊天**供应商
    # 的配置面（有一行激活、有默认模型），向量化没这些形态——它是单例、全局、
    # 建索引时才用。硬塞进同一张表要加 `kind` 列并重做「唯一激活」索引，
    # 而它换来的只是省一个环境变量。长期该并进容量更大的那张表，不是现在。
    #
    # 为什么**维度不在**这里：`vec0` 的维度由 `core/vectors.EMBEDDING_DIM` 在建表时
    # 固定，配一个和它对不上的数字只会让每次写索引都失败。维度是常量不是旋钮——
    # 换模型换维度 = 改常量 + 重建索引，两件事都得做，配成可改反而像能单独改。
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)

    # 前端构建产物目录。None = 不托管（纯 API / 开发态）；`serve` 命令默认
    # 指向 `./web/dist`（不存在则日志警告、API 照常）。托管是同源的，
    # CORS 那一段在这个形态下不生效——那是给 vite dev server 用的。
    # 目录不存在时服务照常启动（F6 §1.11）：后端调试不需要前端产物。
    web_dist: Path | None = None

    # api_key 列加密密钥（S-V6）。**默认 None 是刻意的**：`build_deps` 拿到 None
    # 直接拒绝启动——静默用明文正是 C# M006「P2 再加密」注释的下场。
    # 生产用环境变量 AWB_SECRET_KEY 提供；测试默认值由 tests/conftest.py 设。
    secret_key: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agentworkbench.db"

    @property
    def storage_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def suffix_set(self) -> frozenset[str]:
        """去空白、统一小写、补前导点。写错格式时得到空集，上传会全拒——这是安全的一侧。"""
        items = (item.strip().lower() for item in self.allowed_suffixes.split(","))
        normalized = {item if item.startswith(".") else f".{item}" for item in items if item}
        return frozenset(normalized)

    @property
    def origin_set(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]
