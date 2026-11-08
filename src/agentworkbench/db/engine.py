"""连接与事务边界：全项目唯一创建连接的地方。

事务边界在这里定义，别处只能用。索引写入必须是一个事务，
且清理在事务内——删不掉不会报错，只会留下检索能命中的幽灵。

`vec0` 是 sqlite-vec 的虚表模块，连接必须显式 load 扩展才能读写；
忘了 load 时 `select count(*)` 直接报 `no such module: vec0`——这比静默返回 0 好。

来源：C# `Persistence/DbConnectionFactory.cs`、`AppDbContext.cs`
相关决策：ADR 0002（索引写入是一个事务）
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import sqlite_vec  # type: ignore[import-untyped]

from agentworkbench.core.conversation import ConversationKind, MessageRole
from agentworkbench.core.judgment import CitationFirstCheck, CitationSecondCheck
from agentworkbench.core.runs import RunStatus, StepKind
from agentworkbench.core.vectors import EMBEDDING_DIM

# 状态与步骤种类的值集从 core 的枚举生成，不在 DDL 里另抄一份：
# 两份枚举迟早漂移，漂移之后库能写进代码不认识的状态。
_RUN_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in RunStatus)
_STEP_KIND_VALUES = ", ".join(f"'{kind.value}'" for kind in StepKind)
# 同一个理由：role 与两级校验的取值也来自 core。校验结论**必须原样存档**，
# 所以它的取值集要能和现在的枚举对上——重开后界面靠它显示「当时标红的误挂」
_ROLE_VALUES = ", ".join(f"'{role.value}'" for role in MessageRole)
_CONVERSATION_KIND_VALUES = ", ".join(f"'{kind.value}'" for kind in ConversationKind)
_FIRST_CHECK_VALUES = ", ".join(f"'{check.value}'" for check in CitationFirstCheck)
_SECOND_CHECK_VALUES = ", ".join(f"'{check.value}'" for check in CitationSecondCheck)

# 执行记录两张表（F4）。常量单独抽出，因为 alembic 的 0002 迁移也要用它——
# 建表函数与迁移共用同一份 DDL，是 `test_migrations` 那条不变量要求的。
RUNS_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS agent_runs (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    tenant_id         TEXT NOT NULL,
    question          TEXT NOT NULL,
    -- 无默认值：漏传 status 必须报错，不许落成「成功」（A-V9）
    status            TEXT NOT NULL CHECK (status IN ({_RUN_STATUS_VALUES})),
    reason            TEXT,
    model             TEXT,
    prompt_version    TEXT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    -- 用量三列可空：拿不到就写 NULL，写 0 会被当成真实值统计（A-V10）
    duration_ms       INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    -- 引用校验的两个计数（§1.9 t7）：校验不成步，结果落在 run 上。
    -- 独立成列而不是塞一个 JSON：这两个数要被统计（无出处句比例、越界误报率），
    -- 进了 JSON 就只能整篇取出来在 Python 里聚合。
    -- 拒答时两者为 NULL 而不是 0——没有正文可校验，与「校验过、零处问题」不是一回事
    citation_out_of_range INTEGER,
    citation_unsupported  INTEGER,
    error             TEXT,
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE TABLE IF NOT EXISTS agent_steps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    -- 序号由写入口分配，调用方不许传：自己传就等于自己排回放顺序
    seq        INTEGER NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ({_STEP_KIND_VALUES})),
    tool_name  TEXT,
    input      TEXT,
    -- output / error 同时为 NULL = 「已发出、未返回」：进程在调用中途被杀，
    -- 或停止请求落在等返回的那一段。这不是「没有输出」，是「不知道结果」，
    -- 三态必须能分开（A-V6）。失败写 error、不写 output，成败直接查库可判（A-H2）
    output     TEXT,
    error      TEXT,
    model      TEXT,
    duration_ms       INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, seq)
);
"""


# 会话与消息三张表（F5）。同样抽成常量：alembic 的 0003 要执行同一份 DDL。
#
# 三张表回答的是同一件事：**一条回答由哪几块组成**（F5 §1.2）。所以引用不是
# 另存一段文本，而是独立的行——只有行才能被逐条比对（C-V3）、被统计（无出处句比例）。
CONVERSATIONS_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    tenant_id  TEXT NOT NULL,
    -- 答案合同（F7 §3.1）：knowledge = 有证据才答；general = 不检索、不引用。
    -- 默认 knowledge 只为兼容 F7 之前创建的会话；新建入口必须显式传 kind。
    kind       TEXT NOT NULL DEFAULT 'knowledge'
        CHECK (kind IN ({_CONVERSATION_KIND_VALUES})),
    -- 标题：首条用户消息前 18 字，写一次之后不再变（C-V9）。
    -- NOT NULL 而不是可空：可空会催生「没有标题时界面自己编一个」——
    -- 那正好是 C# 的形态（标题只活在前端内存，换设备全变「新会话」）
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (updated_at >= created_at)
);

-- 列表查询的唯一走法：按 owner 过滤 + 确定次序。
-- id 进排序键是为了同一秒创建的两条不换位——否则「换设备一模一样」在测试里
-- 会变成随机红，而随机红最后一定被人用 -x 跳过（C-V1 / C-V10）
CREATE INDEX IF NOT EXISTS ix_conversations_owner_updated
    ON conversations(tenant_id, user_id, updated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ({_ROLE_VALUES})),
    -- 空串不是 NULL：失败/拒答那一轮也要有一行，正文为空但字段在（F5 §1.6 第三条）。
    -- 可空会让展示层分不出「没有正文」与「忘了写」
    content         TEXT NOT NULL,
    -- 一次运行只属于一条回答。user 行没有 run。
    -- ON DELETE RESTRICT：删 run 时若还有回答指着它，宁可报错也不许把答案一起删掉——
    -- 静默丢一条回答，比删不动难查得多
    run_id          TEXT REFERENCES agent_runs(id) ON DELETE RESTRICT,
    created_at      TEXT NOT NULL,
    -- 角色与 run 必须配对。这条 CHECK 是 C-V4 在存储层的下限：
    -- 允许 assistant 行没有 run，就等于允许一条无法回放的回答
    CHECK (
        (role = 'assistant' AND run_id IS NOT NULL)
        OR (role = 'user' AND run_id IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_run
    ON messages(run_id) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_messages_conversation ON messages(conversation_id, id);

-- 引用快照。四个必填字段都是**复制下来的值**，不是外键、不是指针（F5 §1.3）：
-- 重建索引后 chunks 全表换 id，指针要么查不到、要么指向别的内容，而后者不报错。
-- 所以这里刻意没有 chunk_id / document_id 列——不是漏了，是那样做等于回表查。
CREATE TABLE IF NOT EXISTS message_citations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    -- 本次证据里的编号（从 1 起，D-05）。UNIQUE 保证同一编号不出现两次
    no           INTEGER NOT NULL CHECK (no >= 1),
    file_name    TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    excerpt      TEXT NOT NULL,
    -- 当时的校验结论：不存它，重开后「当时标红的误挂」会变成一条干净引用，
    -- 比没有引用更危险——它带着担保（C-V3）
    check_first  TEXT NOT NULL CHECK (check_first IN ({_FIRST_CHECK_VALUES})),
    -- 二级结论。阈值没配时 guard 判的是 'unverified'（判了但判不了），
    -- **不是 'supported'**——把未判定当已核实，界面就会给一句没有担保的话盖上章（D-12）。
    -- 可空只留给「这条没有二级结论」；越界的引用根本不进这张表，所以今天写进来的
    -- 每一行都有值。留着可空是因为 `Citation.second` 的类型本身可空，写入口按原样
    -- 复制，不替它编一个结论
    check_second TEXT CHECK (check_second IS NULL OR check_second IN ({_SECOND_CHECK_VALUES})),
    coverage     REAL,
    -- 引用快照来自哪个知识库。不可访问时只显示占位，不回显文件名/片段（F5 §1.11）。
    kb_id        TEXT,
    UNIQUE (message_id, no)
);
"""


# 供应商与模型两张表（F6 · REQ-22）。**平台级配置：没有 tenant 列**——
# 一套部署一个模型来源是刻意选的（BYOK 永不做，F6 §5.1），加上 tenant 等于
# 把"全局唯一"变成"每个团队各一份"，凭据分散后无法统一审计。
#
# 列名就叫 api_key_encrypted：让「这一列存的是密文」在 schema 里可见——
# C# M006 的注释写着「P2 再加密」，明文在库里躺了两个版本。
PROVIDERS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS providers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    base_url          TEXT NOT NULL,
    -- Fernet 密文。明文不落库、不进任何读响应（S-V6 / S-V8）
    api_key_encrypted TEXT,
    is_active         INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- 唯一激活（S-V7）。应用层「先清 0 再置 1」的事务保证切换过程原子，
-- 这个**全表级**部分唯一索引保证终态：并发或手工 SQL 也造不出两行激活。
-- C# 版只有事务没有索引，后果是 GetActiveProviderAsync 取 id 最小那条，
-- 另一个激活行静默失效，而且没有任何东西会红。
CREATE UNIQUE INDEX IF NOT EXISTS ux_providers_active
    ON providers(is_active) WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS provider_models (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    is_default  INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    created_at  TEXT NOT NULL,
    -- 同一模型不许加两次：加了之后 is_default 落在哪行不可预期
    UNIQUE (provider_id, model)
);

-- 每供应商至多一个默认模型（REQ-22③ 的另一半缺口，C# 也没有这条约束）
CREATE UNIQUE INDEX IF NOT EXISTS ux_provider_models_default
    ON provider_models(provider_id) WHERE is_default = 1;

-- 平台级键值（F6 · S-V10）。目前只放 embedding 配置的同步标记：
--   embedding.model  = 想要的模型（设置接口写入）
--   embedding.synced = 索引实际用的模型（重建完成时写入）
-- 两值不同 = 索引待重建，检索必须报 index_stale，不许返回空冒充「资料里没有」。
-- 单独一张表而不是给 documents 加列：这是**全局**状态，不属于任何一份文档。
CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


IDENTITY_SCHEMA_SQL = """
-- 团队。数据归属的边界（F1 · AUTH-02）
CREATE TABLE IF NOT EXISTS tenants (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 账户。三个正交维度是**三列**，不是一个布尔（F1 §1.4）：
--   status           管理员手动改、直到恢复、必须踢会话
--   lockout_until    系统自动、到期自愈、不许踢会话
--   credential_epoch 改密 / 重置时 +1，让旧票据一次比较就失效
-- 合成 enabled 会同时丢掉「谁改的」与「要不要踢下线」，两种恢复动作也就分不出来。
CREATE TABLE IF NOT EXISTS users (
    id               TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    -- COLLATE NOCASE：邮箱大小写不敏感是**列属性**，不是每次查询的记忆。
    -- 只在应用层 casefold 的写法，会被一次手工 SQL 或一次脚本导入绕过去，
    -- 而绕过之后的表现是「同一个邮箱两个账号，登录时取哪一行看运气」。
    email            TEXT NOT NULL COLLATE NOCASE,
    password_hash    TEXT NOT NULL,
    role             TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    status           TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
    credential_epoch INTEGER NOT NULL DEFAULT 1 CHECK (credential_epoch >= 1),
    lockout_until    TEXT,
    failed_attempts  INTEGER NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
    -- 平台轴，**不在上面那三个维度里**（REQ-AUTH-12）：
    --   role 回答「在我这个团队里能管谁」，这一列回答「能不能改这套安装」。
    -- 两者的范围不同，所以不进 ROLE_RANK——并进等级轴之后，「目标角色高于自己」
    -- 那条判定会把管供应商的人当成「更高的团队角色」，而他在自己团队里可能只是 member。
    --
    -- 只能由引导路径写 1（§1.7 第 t5 步）。**没有任何接口能改它**：
    -- 存在授予入口，团队管理员就能自封，这条列等于不存在。
    is_platform_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_platform_admin IN (0, 1)),
    -- 首登改密标记（REQ-AUTH-11 / I-V19）。它是**凭据轮换标记**，不是账户状态：
    -- 不影响登录、status、lockout_until，也不进 ROLE_RANK。引导/管理员重置置 1，
    -- 本人改密清 0；存量账号默认 0，升级后不被误伤。
    must_change_password INTEGER NOT NULL DEFAULT 0
        CHECK (must_change_password IN (0, 1)),
    -- 账号等级（F1 / REQ-25）：1~5，独立于 role 与 is_platform_admin。
    -- 默认 1 让存量账号升级后仍是普通可见范围；引导管理员由服务层显式升到 5。
    level_rank INTEGER NOT NULL DEFAULT 1 CHECK (level_rank BETWEEN 1 AND 5),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- 邮箱**全局**唯一，不是团队内唯一。
-- 理由在登录流程里：登录只凭 email + 口令，请求里没有团队上下文（用户不知道
-- 自己是第几个团队），所以库里若有两行同 email，取哪一行是任意的——
-- 那等于「同一个人可能登进别人的团队」，而且两个团队各自看都正常。
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email ON users(email COLLATE NOCASE);

-- 票据。库里只有 SHA-256，明文只存在于 cookie（凭证泄露的爆炸半径因此是单会话）
CREATE TABLE IF NOT EXISTS tickets (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- 唯一：允许两行同哈希时，「吊销哪一条」就成了不明确的操作
    token_hash       TEXT NOT NULL UNIQUE,
    -- 签发那一刻账户的版本号。改密后版本 +1，旧票据一次整数比较就失效，
    -- 不需要「遍历所有会话删一遍」——那是一次批量写，漏一条留一条后门
    credential_epoch INTEGER NOT NULL,
    issued_at        TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    -- 非空 = 被吊销（登出 / 停用 / 改密 / 管理员重置）。
    -- 与「过期」区分：过期是时间比较的结果，不写这一列——两者在排查时是两个故事
    revoked_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_tickets_user ON tickets(user_id);

-- 一次性邀请。`accepted_at` 是那个「用过」的标记，接受动作靠一条
-- `update ... where accepted_at is null` 的原子守卫，不靠先查再写
CREATE TABLE IF NOT EXISTS invites (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email       TEXT COLLATE NOCASE,
    role        TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TEXT NOT NULL,
    accepted_at TEXT,
    accepted_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_by  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_invites_tenant ON invites(tenant_id);

-- 拒绝记录（F1 §1.3）。**刻意没有指向 users 的外键**：
-- 账户被删之后，历史拒绝记录仍然要能回答「当时是谁被拒的」——
-- 加了 CASCADE 就等于把审计证据跟着被审计对象一起删掉。
-- 也不记请求体 / 响应体：轨迹里的证据属 F4，这里只留元数据。
CREATE TABLE IF NOT EXISTS identity_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      TEXT NOT NULL UNIQUE,
    occurred_at   TEXT NOT NULL,
    actor_user_id TEXT,
    tenant_id     TEXT,
    -- 方法与路由模板，不记原始 URL：查询串里可能带敏感值
    action        TEXT NOT NULL,
    resource_kind TEXT,
    resource_id   TEXT,
    reason_code   TEXT NOT NULL,
    request_id    TEXT
);
CREATE INDEX IF NOT EXISTS ix_identity_events_time ON identity_events(occurred_at);
CREATE INDEX IF NOT EXISTS ix_identity_events_tenant_time
    ON identity_events(tenant_id, occurred_at);
-- 按 X-Request-Id 反查一次失败，是运维最常见的那一个动作
CREATE INDEX IF NOT EXISTS ix_identity_events_request ON identity_events(request_id);
"""


KNOWLEDGE_SCHEMA_SQL = """
-- 账号等级（F1 / REQ-25）。表本身只给名称与排序；users.level_rank 只存整数。
CREATE TABLE IF NOT EXISTS account_levels (
    rank       INTEGER PRIMARY KEY CHECK (rank BETWEEN 1 AND 5),
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
INSERT OR IGNORE INTO account_levels(rank, name, created_at) VALUES
    (1, 'L1', '1970-01-01T00:00:00'),
    (2, 'L2', '1970-01-01T00:00:00'),
    (3, 'L3', '1970-01-01T00:00:00'),
    (4, 'L4', '1970-01-01T00:00:00'),
    (5, 'L5', '1970-01-01T00:00:00');

-- 知识库（F2 / REQ-24）。刻意没有 tenant 外键：测试与旧库可能存在
-- 只有 documents.tenant_id、没有 tenants 行的数据；外键会让默认库建不出来，
-- 而「没有默认库」会把存量文档全部变成不可见。团队边界仍由查询条件保证。
CREATE TABLE IF NOT EXISTS knowledge_bases (
    id             TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    name           TEXT NOT NULL,
    visibility     TEXT NOT NULL CHECK (visibility IN ('public', 'private')),
    min_level_rank INTEGER CHECK (min_level_rank BETWEEN 1 AND 5),
    is_default     INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    created_by     TEXT,
    archived_at    TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    CHECK (
        (visibility = 'public' AND min_level_rank IS NULL)
        OR (visibility = 'private' AND min_level_rank IS NOT NULL)
    ),
    UNIQUE (tenant_id, name)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_knowledge_bases_default
    ON knowledge_bases(tenant_id) WHERE is_default = 1;
CREATE INDEX IF NOT EXISTS ix_knowledge_bases_tenant
    ON knowledge_bases(tenant_id, archived_at);

-- 管理员操作审计（F1 §1.10）。与 identity_events 分开：后者只记拒绝，
-- 这张表记「谁改了账号/等级/知识库」，两者混在一起会让读侧问错问题。
CREATE TABLE IF NOT EXISTS admin_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT NOT NULL UNIQUE,
    occurred_at    TEXT NOT NULL,
    actor_user_id  TEXT,
    tenant_id      TEXT,
    target_user_id TEXT,
    action         TEXT NOT NULL,
    resource_kind  TEXT,
    resource_id    TEXT,
    old_value      TEXT,
    new_value      TEXT,
    request_id     TEXT
);
CREATE INDEX IF NOT EXISTS ix_admin_events_tenant_time
    ON admin_events(tenant_id, occurred_at);
"""


# 表结构对齐 C# 现状，只做三处增补（都是「现在加免费，以后要迁移」的）：
#   documents.tenant_id      —— 跨团队隔离的前提
#   documents.error_code     —— 失败原因可统计（F2 §三-1）
#   documents.updated_at     —— V-7 的 T 靠它算，否则"卡死"无法判定
SCHEMA_SQL = (
    f"""
CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    file_name   TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    status      TEXT NOT NULL,
    page_count  INTEGER NOT NULL DEFAULT 0,
    error_code  TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    parsed_at   TEXT,
    -- 文档继承知识库可见性（REQ-24）。老库由 ensure_knowledge_schema 回填；
    -- 新写入必须显式带 kb_id，触发器只兜底旧测试夹具与手工 SQL。
    kb_id       TEXT
);
-- 去重/知识库索引在 ensure_knowledge_schema 里建：老库的 documents
-- 还没有 kb_id 列，建表脚本阶段建索引会直接报 no such column。

CREATE TABLE IF NOT EXISTS document_pages (
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no     INTEGER NOT NULL,
    raw_text    TEXT NOT NULL,
    PRIMARY KEY (document_id, page_no)
);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    page_from   INTEGER NOT NULL,
    page_to     INTEGER NOT NULL,
    heading     TEXT,
    content     TEXT NOT NULL,
    char_count  INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content,
    document_id UNINDEXED,
    chunk_id UNINDEXED,
    tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS chunks_vec (
    ChunkId    INTEGER PRIMARY KEY,
    DocumentId TEXT NOT NULL,
    Seq        INTEGER NOT NULL,
    Heading    TEXT,
    PageFrom   INTEGER NOT NULL,
    PageTo     INTEGER NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks_vec USING vec0(
    ChunkId   INTEGER PRIMARY KEY,
    Embedding FLOAT[{EMBEDDING_DIM}] distance_metric=cosine
);
"""
    + RUNS_SCHEMA_SQL
    # 顺序有意义：messages.run_id 外键指向 agent_runs，必须后建
    + CONVERSATIONS_SCHEMA_SQL
    # 供应商与平台键值（F6）。无外键指向前面任何表，顺序只求可读
    + PROVIDERS_SCHEMA_SQL
    # 身份与访问（F1）：tenants 必须最先建（users 指向它），tickets / invites
    # 次之，identity_events 无外键、放最后只为读起来顺
    + IDENTITY_SCHEMA_SQL
    # 账号等级 / 知识库 / 管理员操作审计（REQ-24 / REQ-25）
    + KNOWLEDGE_SCHEMA_SQL
)


WRITE_PROBE_ATTEMPTS = 6
"""拿不到可写连接时最多重开几次。D6 实测：8 线程并发开新库，160 次里 133 次首次
拿到的连接是"只读"的——重试 1~3 次内全部恢复（最长用到 3 次）。"""

WRITE_PROBE_BUSY_MS = 10
"""探测用的 busy_timeout（毫秒）。

**不能沿用 5 秒**：探测要取一次写锁，5 秒会让每条读请求排在任何写者后面。
10 毫秒足够区分三种结果——拿到锁（可写）、被判只读（立刻返回，不等待）、
被别人挡住（BUSY，说明连接本身可写，只是现在轮不到）。
"""


def _is_writable(conn: sqlite3.Connection) -> bool:
    """连接是不是**能写**的。`begin immediate` 是最便宜的问法。

    只读的 WAL 连接在取写锁这一步就报 `SQLITE_READONLY`；被别的写者占着则报
    `SQLITE_BUSY`——后者恰恰证明这条连接**没有**退化，只是现在轮不到它写。
    把两者混为一谈会让「库正忙」变成「反复重开连接」，那才是真的把写堵死。
    """
    conn.execute(f"pragma busy_timeout = {WRITE_PROBE_BUSY_MS}")
    try:
        conn.execute("begin immediate")
    except sqlite3.OperationalError as exc:
        name = getattr(exc, "sqlite_errorname", "")
        if name == "SQLITE_BUSY":
            return True
        if name == "SQLITE_READONLY":
            return False
        raise
    conn.execute("rollback")
    return True


def connect(path: str | Path) -> sqlite3.Connection:
    """开连接：外键打开、WAL、加载 vec0，**并确认这条连接可写**。

    外键必须在这里打开。`ON DELETE CASCADE` 只在 `foreign_keys=ON` 时生效，
    而它是**连接级**开关：谁忘了开就留孤儿行，孤儿行不抛异常，只在检索时
    表现为"删了还能命中"。删除路径因此显式删三张表、不靠级联——但开关仍然要开。

    ## 为什么要探测可写（D6 抓到的真问题）

    WAL 模式要求每条连接把 `-shm` 映射进来。Windows 上多个线程同时开连接时，
    这一步会偶发失败，而 SQLite 的降级方式是**把这条连接当成只读**——不报错、
    不重试，等第一条 `update` 才炸 `SQLITE_READONLY: attempt to write a readonly
    database`。用户看到的是随机 500，且只在并发时出现，单线程跑一万次都复现不了。

    实测（SQLite 3.53.1 / Windows 11，8 线程各写一次）：

    - 全新库、首个连接即并发：**133/160 次失败**
    - `-shm` 已建立的热库：**7/160 次失败**
    - 热库 + 一条常驻连接（`-shm` 一直映射）：**0/160**
    - 本函数这套「探测 + 重开」：**0/160**（其中 117 次用了 1~3 次重试）

    常驻连接也能治，但它要把一条连接挂到进程生命周期上：测试里每个 tmp 库一条，
    进程结束时目录被锁住、清理报错。探测每次多一次 `begin/rollback`（微秒级），
    代价小得多，也不需要谁记得关。
    """
    for attempt in range(WRITE_PROBE_ATTEMPTS):
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        conn.execute("pragma journal_mode = wal")
        # synchronous=normal 是 WAL 下的官方推荐：commit 不再强制 fsync，
        # 掉电最多丢最后一个事务、库不会损坏。FULL 换来的那一层保护，
        # 本产品用不上——单机工具，掉电重传一份文件即可。
        conn.execute("pragma synchronous = normal")
        load_vec(conn)
        if _is_writable(conn):
            conn.execute("pragma busy_timeout = 5000")
            return conn
        conn.close()
        # 退避要短：这不是"库忙"，而是 -shm 映射竞争，下一次几乎必然成功。
        # 太长会把并发请求拖成排队，太短则可能在同一次竞争里连撞几次。
        time.sleep(0.002 * (attempt + 1))
    raise sqlite3.OperationalError(
        f"连接 {path} 连续 {WRITE_PROBE_ATTEMPTS} 次落在只读状态（WAL -shm 映射失败）。"
        "通常是库文件被另一个进程以独占方式打开（同步网盘目录、杀毒软件扫描、"
        "或另一份程序实例）。换一个本地目录再试。"
    )


def load_vec(conn: sqlite3.Connection) -> None:
    """加载 vec0 扩展。迁移脚本复用同一份——两边加载方式不同就有一边建不出表。"""
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def ensure_must_change_password_column(conn: sqlite3.Connection) -> bool:
    """给已存在的 `users` 补上首登改密列。返回是否真的加过。

    当前有一批生产库是 `create_schema` 建的，`alembic_version` 表不存在，
    直接 `alembic upgrade head` 会从 0001 重跑并撞上已存在的表。所以启动期
    只做**向后兼容的加列**：默认 0、不改已有行、重复执行是空操作。删列/改类型
    /数据重写仍然只走 Alembic——这里不是第二条迁移通道。
    """
    columns = {str(row[1]) for row in conn.execute("pragma table_info(users)").fetchall()}
    if "must_change_password" in columns:
        return False
    try:
        conn.execute(
            "alter table users add column must_change_password integer not null default 0"
            " check (must_change_password in (0, 1))"
        )
    except sqlite3.OperationalError as exc:
        # 两个进程同时启动时，另一个已经加过。只有这一种错误可以咽掉：
        # 其他 OperationalError（磁盘满 / 锁死）必须让启动失败，不能假装成功。
        if "duplicate column" not in str(exc).lower():
            raise
        return False
    return True


def ensure_conversation_kind_column(conn: sqlite3.Connection) -> bool:
    """给已有的 `conversations` 补上 `kind` 并确保不可变触发器存在。

    返回是否真的加过列。默认 `knowledge`：F7 之前的会话全部来自知识库问答，
    升级后不能被误标成通用回答。加列与建触发器都幂等；启动期只做这种
    向后兼容变更，真正的结构迁移仍走 Alembic。
    """
    columns = {str(row[1]) for row in conn.execute("pragma table_info(conversations)").fetchall()}
    added = False
    if "kind" not in columns:
        try:
            conn.execute(
                "alter table conversations add column kind text not null default 'knowledge'"
                f" check (kind in ({_CONVERSATION_KIND_VALUES}))"
            )
            added = True
        except sqlite3.OperationalError as exc:
            # 两个进程同时启动时另一个已经加过；其余错误（锁死 / 磁盘满）必须失败。
            if "duplicate column" not in str(exc).lower():
                raise
    conn.execute(
        "create trigger if not exists trg_conversations_kind_immutable"
        " before update of kind on conversations"
        " begin select raise(abort, 'conversations.kind 创建后不可修改'); end"
    )
    return added


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"pragma table_info({table})").fetchall()}


def _ensure_default_kb(conn: sqlite3.Connection, tenant_id: str, *, now: datetime) -> str:
    """确保团队有一个默认公共知识库，返回 id。

    id 用确定性前缀而不是随机值：启动期与迁移都会跑，随机 id 会让同一团队
    每次多出一个「默认库」，而唯一默认索引当场报错。
    """
    existing = conn.execute(
        "select id from knowledge_bases where tenant_id = ? and is_default = 1",
        (tenant_id,),
    ).fetchone()
    if existing is not None:
        return str(existing["id"])
    kb_id = f"kb-default-{tenant_id}"
    moment = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        "insert or ignore into knowledge_bases"
        " (id, tenant_id, name, visibility, min_level_rank, is_default,"
        "  created_by, archived_at, created_at, updated_at)"
        " values (?, ?, '公共知识库', 'public', null, 1, null, null, ?, ?)",
        (kb_id, tenant_id, moment, moment),
    )
    row = conn.execute(
        "select id from knowledge_bases where tenant_id = ? and is_default = 1",
        (tenant_id,),
    ).fetchone()
    return str(row["id"]) if row is not None else kb_id


def ensure_knowledge_schema(conn: sqlite3.Connection) -> bool:
    """补账号等级 / 知识库 / 引用 kb_id，并回填默认公共知识库。

    启动期与 Alembic 0009 共用这一份。**只做向后兼容的加列与建表**：
    真正的结构变更仍走 Alembic；这里重复执行是空操作。

    关键顺序：先补列，再建表/索引，再回填文档，最后替换去重索引。
    如果先建 `(tenant_id, kb_id, sha256)` 唯一索引，旧库里的 null kb_id
    会让「同一团队多份文档」不受约束；回填后再建索引，才能把
    「同一文件在同一个知识库只能一份」变成真正的约束。
    """
    added = False
    if "level_rank" not in _columns(conn, "users"):
        try:
            conn.execute(
                "alter table users add column level_rank integer not null default 1"
                " check (level_rank between 1 and 5)"
            )
            added = True
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    if "kb_id" not in _columns(conn, "documents"):
        try:
            conn.execute("alter table documents add column kb_id text")
            added = True
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    if "kb_id" not in _columns(conn, "message_citations"):
        try:
            conn.execute("alter table message_citations add column kb_id text")
            added = True
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

    conn.executescript(KNOWLEDGE_SCHEMA_SQL)

    now = datetime.now(UTC)
    tenant_rows = conn.execute(
        "select id as tenant_id from tenants"
        " union select distinct tenant_id from users"
        " union select distinct tenant_id from documents"
        " union select distinct tenant_id from knowledge_bases"
    ).fetchall()
    default_by_tenant = {
        str(row["tenant_id"]): _ensure_default_kb(conn, str(row["tenant_id"]), now=now)
        for row in tenant_rows
        if str(row["tenant_id"]).strip()
    }

    for tenant_id, kb_id in default_by_tenant.items():
        conn.execute(
            "update documents set kb_id = ? where tenant_id = ? and kb_id is null",
            (kb_id, tenant_id),
        )
        conn.execute(
            "update message_citations set kb_id = ("
            "  select kb.id from messages m"
            "  join conversations c on c.id = m.conversation_id"
            "  join knowledge_bases kb on kb.tenant_id = c.tenant_id and kb.is_default = 1"
            "  where m.id = message_citations.message_id limit 1"
            ") where kb_id is null and message_id in ("
            "  select m.id from messages m join conversations c on c.id = m.conversation_id"
            "  where c.tenant_id = ?"
            ")",
            (tenant_id,),
        )

    # 新团队自动建默认公共库；新文档漏传 kb_id 时兜底到默认库。
    conn.execute(
        "create trigger if not exists trg_tenant_default_kb after insert on tenants"
        " begin"
        "   insert or ignore into knowledge_bases"
        "     (id, tenant_id, name, visibility, min_level_rank, is_default,"
        "      created_by, archived_at, created_at, updated_at)"
        "   values ('kb-default-' || new.id, new.id, '公共知识库', 'public', null, 1,"
        "           null, null, new.created_at, new.created_at);"
        " end"
    )
    conn.execute(
        "create trigger if not exists trg_documents_default_kb after insert on documents"
        " when new.kb_id is null"
        " begin"
        "   insert or ignore into knowledge_bases"
        "     (id, tenant_id, name, visibility, min_level_rank, is_default,"
        "      created_by, archived_at, created_at, updated_at)"
        "   values ('kb-default-' || new.tenant_id, new.tenant_id, '公共知识库',"
        "           'public', null, 1, null, null, new.created_at, new.created_at);"
        "   update documents set kb_id = ("
        "     select id from knowledge_bases where tenant_id = new.tenant_id"
        "     and is_default = 1 limit 1"
        "   ) where id = new.id and kb_id is null;"
        " end"
    )
    conn.execute(
        "create trigger if not exists trg_message_citations_default_kb"
        " after insert on message_citations"
        " when new.kb_id is null"
        " begin"
        "   update message_citations set kb_id = ("
        "     select kb.id from messages m"
        "     join conversations c on c.id = m.conversation_id"
        "     join knowledge_bases kb on kb.tenant_id = c.tenant_id and kb.is_default = 1"
        "     where m.id = new.message_id limit 1"
        "   ) where id = new.id and kb_id is null;"
        " end"
    )

    # 旧去重索引必须换成按知识库去重；否则私人库/公共库不能各存一份。
    conn.execute("drop index if exists ux_documents_tenant_sha256")
    conn.execute(
        "create unique index if not exists ux_documents_tenant_kb_sha256"
        " on documents(tenant_id, kb_id, sha256)"
    )
    conn.execute("create index if not exists ix_documents_tenant_kb on documents(tenant_id, kb_id)")
    return added


def create_schema(conn: sqlite3.Connection) -> None:
    """建表。幂等，空库起服务时调一次。

    对已存在的库，这里只做**向后兼容的加列**（见 `ensure_must_change_password_column`
    与 `ensure_conversation_kind_column`），不替代 Alembic 的结构迁移。
    """
    conn.executescript(SCHEMA_SQL)
    ensure_must_change_password_column(conn)
    ensure_conversation_kind_column(conn)
    ensure_knowledge_schema(conn)
    conn.commit()
