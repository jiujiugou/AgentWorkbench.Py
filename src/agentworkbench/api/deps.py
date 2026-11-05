"""应用装配：显式构造，不用容器。

`api` 是最上层，只有它知道各包怎么拼。**装配代码只出现在这里**：
哪个解析器、哪个嵌入实现、哪个队列——换任何一个是改这一个文件，
而不是在全项目搜构造函数。

连接策略：**每请求开新连接，不在 Deps 里放连接**。
理由是 SQLite 的连接是线程绑定的（默认 `check_same_thread`），而 FastAPI 的
同步路由跑在线程池——共享连接要么跨线程炸、要么自己管锁。每请求新开的代价
是几毫秒（WAL 已打开，无需迁移），换来的是线程模型简单到不用想。
解析 worker 在自己的线程里也开自己的连接（WAL 支持一写多读）。

来源：C# `Webapi/Program.cs` 的 ConfigureServices
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from agentworkbench.config import Settings
from agentworkbench.core.clock import utc_now
from agentworkbench.core.errors import AppError, ErrorCode
from agentworkbench.db import crypto, engine
from agentworkbench.db.repositories import providers as provider_repo
from agentworkbench.db.repositories import system_state
from agentworkbench.identity import service
from agentworkbench.identity.sessions import SessionCache
from agentworkbench.ingest.queue import InMemoryQueue
from agentworkbench.ingest.storage import LocalStorage
from agentworkbench.ingest.worker import EmbedderPort, ParserPort
from agentworkbench.llm.client import ChatClient, OpenAIChatClient
from agentworkbench.llm.embeddings import EmbeddingEndpoint, HashEmbedder, OpenAIEmbedder
from agentworkbench.llm.providers import ChatEndpoint

logger = logging.getLogger(__name__)

# 时间戳格式的定义在 core/clock.py：trace 层也要写时间，而它不能 import api。
# 这里重新导出，是为了让路由继续 `from agentworkbench.api.deps import Deps, utc_now`——
# 装配面保持一处，格式只有一份。
__all__ = ["ChatFactory", "Deps", "build_deps", "utc_now"]

ChatFactory = Callable[[sqlite3.Connection], tuple[ChatClient, str]]
"""连接 → (客户端, 模型名)。

模型名与客户端一起返回，因为两者必须同源：run 行要落 `model`（§三-3），
而模型名唯一可信的来源就是解析出来的那个端点。让调用方自己再取一次，
就会出现在"客户端换了、名字没换"的记录。
"""

# ── 这里曾经有 `LOCAL_USER_ID = "local"` 与 `caller_of(deps)` ──
# 它们返回固定的 `Owner("local", settings.tenant_id)`，是阶段 1 单用户脚手架的
# 全部内容。F1 格 5 删掉了，归属改由 `api/identity_context.py::owner_of` 从
# 登录身份取。
#
# 删而不是留一个「返回默认值」的版本：留下的那个会在某个新路由里被顺手调用，
# 而调用点看起来完全正常——它只是把所有人的数据写进同一个团队。
# `I-V5` 的负控说的正是这件事：「把 owner 参数做成可选 → 红」。


def _chat_for(secret: str) -> ChatFactory:
    """按库里的供应商配置建客户端的工厂（闭包持有密钥，签名仍是 `ChatFactory`）。

    解析与解密在这里拼，因为它们跨了两个互不 import 的同层包：
    行级判别（四种未配置形态的报错）在 db 仓储，端点与客户端在 llm，
    **密文 → 明文的唯一一跳在装配层**——这不是绕过分层，分层本来就是
    让这类拼接有且只有一个家。
    """

    def factory(conn: sqlite3.Connection) -> tuple[ChatClient, str]:
        row = provider_repo.resolve_active_row(conn)
        try:
            api_key = crypto.decrypt(secret, row["api_key_encrypted"])
        except ValueError as exc:
            # 换过 AWB_SECRET_KEY（或库来自另一套密钥）时，此前这里一路冒泡成
            # 「500 服务器内部错误 + retryable=True」：用户看到的是「稍后重试」，
            # 而重试一万次都不会好，真正该看到的「重新保存一次凭据」只留在日志里。
            # 这属于配置失效，按 S-V9 的口径是 503 + 不可重试。
            raise AppError(
                ErrorCode.MODEL_NOT_CONFIGURED,
                "供应商凭据无法解密：当前服务密钥与库里的密文不匹配"
                "（密钥换过，或库是另一套密钥加密的）。重新保存一次供应商 API Key 即可恢复。",
                reason="credential_decrypt_failed",
            ) from exc
        endpoint = ChatEndpoint(
            provider_id=int(row["id"]),
            name=str(row["name"]),
            base_url=str(row["base_url"]),
            api_key=api_key,
            model=str(row["model"]),
        )
        return OpenAIChatClient(endpoint), endpoint.model

    return factory


@dataclass
class Deps:
    """路由能碰到的全部外部世界。测试替换其中任意一项即可注入故障。"""

    settings: Settings
    storage: LocalStorage
    # inline 模式：上传后由 BackgroundTasks 立刻跑完解析。huey consumer
    # 独立进程还没接（队列的周期任务同理），inline 让"传完就能看到块"。
    # 代价是上传请求变慢——页面演示没问题，吞吐不是本阶段的目标。
    queue: InMemoryQueue
    parser: ParserPort
    embedder: EmbedderPort
    # 模型客户端，**必填**：它闭包持有加密密钥，没有"默认实现"可言——
    # 一个不知道密钥的默认工厂要么明文落库要么解析不出来，两者都是 S-V6/V9 的负控形态。
    chat_factory: ChatFactory
    # 身份域的两个派生对象，装配期就算好：
    # `auth` 是 `Settings.auth` 的只读视图（每请求现算等于把 pydantic 校验放进热路径）；
    # `session_cache` 每请求都写，必须是**进程级单例**——建在路由里等于没缓存。
    auth: service.AuthConfig
    session_cache: SessionCache
    # False 时上传只入队不处理：V-4 要观测三态，处理被测试线程接管。
    auto_process: bool = True

    def open_conn(self) -> sqlite3.Connection:
        """开一个新连接。谁开谁关——路由用 `closing()` 包住。"""
        return engine.connect(self.settings.db_path)


def _embedder_for(settings: Settings) -> EmbedderPort:
    """按配置选向量化实现。**没配就用占位实现，但要说出来。**

    退回 `HashEmbedder` 时落 WARNING，不落 INFO、不静默：它的语义相似度是假的，
    检索会退化成"随机邻居"，而界面上一切正常——这类"看起来在工作"的降级
    只能靠日志和这条注释拦住下一个人。
    """
    config = settings.embedding
    if not config.configured:
        logger.warning(
            "未配置 AWB_EMBEDDING__BASE_URL / AWB_EMBEDDING__MODEL：使用占位嵌入 HashEmbedder，"
            "语义检索不可用（相似度由 sha256 派生，同义词不会靠近）"
        )
        return HashEmbedder()
    logger.info("向量化端点：%s，模型 %s", config.base_url, config.model)
    return OpenAIEmbedder(
        EmbeddingEndpoint(
            base_url=config.base_url.strip(),
            api_key=config.api_key.strip(),
            model=config.model.strip(),
        ),
        timeout=config.timeout_seconds,
        batch_size=config.batch_size,
    )


def _align_embedding_state(conn: sqlite3.Connection, embedder: EmbedderPort) -> None:
    """让 `system_state` 里的「想要的模型」等于**配置里真在用的**那个。

    不做这一步，设置页显示的是一个值、实际算向量的是另一个——那正是
    `/api/settings/embedding` 此前"配了不读"的形态（写了模型名，没有任何代码读它）。

    `embedding.synced`（索引实际用的）只在**能确定**的时候写：
    - 库是空的 → 没有索引可重建，写成本模型就是事实；
    - 库里已有片段、而 synced 是空的 → **不知道那些向量是谁算的**，写成 `(未配置)`
      让 `index_is_stale` 判真，检索宁可报 `index_stale` 也不要在错的空间里相似度匹配。
      修法是重建索引（全量重解析），不是忽略这条。
    """
    model = getattr(embedder, "model", None)
    if not isinstance(model, str) or not model:
        return  # 占位嵌入没有"模型身份"，不动状态（老库/评测库的形态）
    wanted, synced = system_state.embedding_state(conn)
    if synced is None:
        has_chunks = conn.execute("select 1 from chunks limit 1").fetchone() is not None
        synced = system_state.UNCONFIGURED if has_chunks else model
        if has_chunks:
            logger.warning(
                "库里已有片段，但 `embedding.synced` 为空——无法确定这些向量是哪个模型算的。"
                "已标为待重建：检索会报 index_stale，重建索引后恢复。"
            )
    if wanted == model and system_state.get(conn, system_state.EMBEDDING_SYNCED_KEY) is not None:
        return
    now = utc_now()
    with conn:
        system_state.put(conn, system_state.EMBEDDING_MODEL_KEY, model, now=now)
        system_state.put(conn, system_state.EMBEDDING_SYNCED_KEY, synced, now=now)


def build_deps(settings: Settings) -> Deps:
    """按配置建目录与实现。schema 在这里一次建齐。

    **缺加密密钥在这里拒绝启动**（S-V6）："启动失败"是唯一诚实的失败方式——
    拖到第一个写凭据的请求才炸，前面每个请求都跑在"随时可能明文落库"的进程里。
    """
    secret = crypto.require_secret(settings.secret_key)
    data_dir: Path = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    storage = LocalStorage(settings.storage_dir)
    storage.root.mkdir(parents=True, exist_ok=True)
    # schema 在装配时建齐：`engine.connect` 只创建库文件不建表。
    # create_schema 用 IF NOT EXISTS，对已有库是幂等的；真正的结构变更走 alembic。
    with closing(engine.connect(settings.db_path)) as conn:
        engine.create_schema(conn)
    embedder = _embedder_for(settings)
    with closing(engine.connect(settings.db_path)) as conn:
        _align_embedding_state(conn, embedder)
    return Deps(
        settings=settings,
        storage=storage,
        queue=InMemoryQueue(),
        parser=_default_parser,
        embedder=embedder,  # 结构化满足 EmbedderPort
        chat_factory=_chat_for(secret),
        auth=service.AuthConfig.from_settings(settings),
        session_cache=SessionCache(settings.auth.session_invalidate_seconds),
    )


def _default_parser(data: bytes, suffix: str) -> list[str]:
    from agentworkbench.ingest.parsing.dispatch import parse_document

    return parse_document(data, suffix)
