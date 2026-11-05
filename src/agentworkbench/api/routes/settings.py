"""供应商设置路由：平台级模型配置的对外入口（F6 · REQ-22）。

**读接口是白名单 DTO**（S-V8）：响应里的每个字段都逐个列名，
`api_key` 根本不在 DTO 里——不是"取全字段再删"。后者每加一个字段就要
重新检查一遍"这个该不该出去"，前者加字段默认不出去，安全侧是默认。

api_key 只在两个方向流动：写入时经 `crypto.encrypt` 变密文落库（S-V6），
读取时**永不**经任何响应体（装配层 `deps._chat_for` 直接喂给客户端）。
界面上"这个供应商配没配钥匙"用 `has_api_key` 布尔表达，明文与密文都不给。

平台级配置：没有归属过滤、没有 tenant 列——REQ-22 是"一套部署一个模型
来源"（BYOK 刻意不做），这四条路由操作的是全局状态。

**正因为它是全局的，写入口只归这套部署的所有者**（REQ-AUTH-12 / `I-V18`）：
四张表（`providers` / `provider_models` / `system_state`）都没有团队列，
所以「谁能改」不可能由任何团队角色回答。`role=admin` 的团队管理员在这里是 403——
他管的是人，不是这套安装。读接口对登录用户开放：界面要显示「当前用哪个模型」，
那不是权力。

服务于：S-V6 / S-V7 / S-V8 / S-H1（smoke 脚本用 HTTP 配置供应商）
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from typing import Final, cast
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from agentworkbench.api import identity_context
from agentworkbench.api.deps import Deps, utc_now
from agentworkbench.api.errors import not_found, validation_failed
from agentworkbench.db import crypto
from agentworkbench.db.repositories import providers as repo
from agentworkbench.db.repositories import system_state
from agentworkbench.identity import policy

router = APIRouter()


def _deps(request: Request) -> Deps:
    return cast(Deps, request.app.state.deps)


def _platform_only(request: Request) -> None:
    """四条写入口的共同依赖。挂成 `dependencies=[...]` 而不是函数体第一行。

    两个理由：

    1. **顺序。** 依赖在**字段校验**之前执行，所以非所有者发一个缺字段的请求
       得到的是 403 而不是 400。函数体第一行的版本会先被 pydantic 校验一遍再拒绝，
       于是非所有者能读到这个消息里的字段清单。
       （注意边界：**畸形 JSON** 在 `request.json()` 阶段就失败，比依赖更早，
       那种请求一定是校验失败（400）——这不是本函数能管的，也不该写进判据。
       422 已不再出现：D6 把框架默认的错误体换成了五字段信封，原因见 `api/app.py`
       的 `on_validation_error`。）
    2. **可见。** 「哪几条是写入口」在装饰器上一眼能数出来。函数体里的版本
       要靠读四个函数的头一行才知道，而漏掉一个的表现是「那条路由照常工作」。
    """
    principal = identity_context.current_principal(request)
    identity_context.principal_of(policy.judge_platform(actor=principal))


class ModelDto(BaseModel):
    """模型列表的一条。没有 api_key，也没有任何凭据的影子。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: int
    model: str
    is_default: bool


class ProviderDto(BaseModel):
    """供应商的一条。**逐字段白名单**：见模块 docstring。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: int
    name: str
    base_url: str
    is_active: bool
    # 明文与密文都不出去：界面只需要知道"配没配"
    has_api_key: bool
    models: list[ModelDto]


class CreateProviderBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    name: str
    base_url: str
    # 可选：创建时可以只填地址，凭据后补。空串与缺省同义（都是"没配"）
    api_key: str = ""


class AddModelBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    model: str
    is_default: bool = False


def _to_dto(provider: sqlite3.Row, models: list[sqlite3.Row]) -> ProviderDto:
    """密文行 → 白名单 DTO。密文在这里只用来算 `has_api_key`，不进任何字段。"""
    return ProviderDto(
        id=int(provider["id"]),
        name=str(provider["name"]),
        base_url=str(provider["base_url"]),
        is_active=bool(provider["is_active"]),
        has_api_key=bool(provider["api_key_encrypted"]),
        models=[
            ModelDto(id=int(m["id"]), model=str(m["model"]), is_default=bool(m["is_default"]))
            for m in models
        ],
    )


@router.get("/api/settings/providers")
def list_providers(request: Request) -> dict[str, list[ProviderDto]]:
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        pairs = repo.list_providers_with_models(conn)
    return {"providers": [_to_dto(provider, models) for provider, models in pairs]}


@router.post("/api/settings/providers", status_code=201, dependencies=[Depends(_platform_only)])
def create_provider(request: Request, body: CreateProviderBody) -> ProviderDto:
    """新建供应商。api_key 在这里变密文——**路由是明文的最后一站**。"""
    deps = _deps(request)
    if not body.name.strip() or not body.base_url.strip():
        raise validation_failed("name 与 base_url 不能为空。")
    secret = cast(str, deps.settings.secret_key)
    encrypted = crypto.encrypt(secret, body.api_key)
    now = utc_now()
    with closing(deps.open_conn()) as conn, conn:
        provider_id = repo.insert_provider(
            conn,
            name=body.name.strip(),
            base_url=body.base_url.strip(),
            now=now,
            api_key_encrypted=encrypted,
        )
        row = conn.execute("select * from providers where id = ?", (provider_id,)).fetchone()
    return _to_dto(row, [])


@router.post(
    "/api/settings/providers/{provider_id}/activate",
    dependencies=[Depends(_platform_only)],
)
def activate_provider(request: Request, provider_id: int) -> ProviderDto:
    """激活：全表恰好一行 is_active=1（S-V7）。"""
    deps = _deps(request)
    now = utc_now()
    with closing(deps.open_conn()) as conn:
        try:
            repo.set_active(conn, provider_id, now=now)
        except LookupError:
            raise not_found("供应商不存在。") from None
        row = conn.execute("select * from providers where id = ?", (provider_id,)).fetchone()
        models = conn.execute(
            "select * from provider_models where provider_id = ? order by id", (provider_id,)
        ).fetchall()
    return _to_dto(row, list(models))


@router.post(
    "/api/settings/providers/{provider_id}/models",
    status_code=201,
    dependencies=[Depends(_platform_only)],
)
def add_model(request: Request, provider_id: int, body: AddModelBody) -> dict[str, object]:
    """给供应商加模型；可选直接设为默认。"""
    deps = _deps(request)
    if not body.model.strip():
        raise validation_failed("model 不能为空。")
    now = utc_now()
    with closing(deps.open_conn()) as conn, conn:
        exists = conn.execute("select id from providers where id = ?", (provider_id,)).fetchone()
        if exists is None:
            raise not_found("供应商不存在。")
        model_id = repo.insert_model(
            conn,
            provider_id=provider_id,
            model=body.model.strip(),
            is_default=body.is_default,
            now=now,
        )
    return {"id": model_id, "model": body.model.strip(), "isDefault": body.is_default}


# ── embedding 配置：与 chat 供应商**分开的两条路** ──
#
# 负控点名过一种形态："两组配置合并进同一个响应体 → 设置页并排，有人会一起改"。
# chat（providers）与 embedding（system_state 的两个键）在库里就是两处，
# 接口上不许合流：改 chat 不该让索引变脏，改 embedding 也不该碰供应商。


class EmbeddingDto(BaseModel):
    """只描述 embedding 这一组。**字段集固定**——多一个 chat 相关字段就是在合流。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    # 现在配置的模型。None = 从没配过（老库 / 单模型部署）
    model: str | None
    # 索引实际用的模型。None 同上；UNCONFIGURED 表示"改配置时索引还是默认实现建的"
    indexed_model: str | None
    # 两者不同 = 待重建。检索会报 index_stale（S-V10）
    stale: bool


class SetEmbeddingBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    model: str


def _embedding_dto(conn: sqlite3.Connection) -> EmbeddingDto:
    wanted, synced = system_state.embedding_state(conn)
    return EmbeddingDto(
        model=wanted,
        indexed_model=synced,
        stale=system_state.index_is_stale(conn),
    )


@router.get("/api/settings/embedding")
def read_embedding(request: Request) -> EmbeddingDto:
    """读 embedding 一组。**不含任何 chat / 供应商字段**（S-V10 的负控）。"""
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        return _embedding_dto(conn)


@router.put("/api/settings/embedding", dependencies=[Depends(_platform_only)])
def write_embedding(request: Request, body: SetEmbeddingBody) -> EmbeddingDto:
    """改 embedding 模型 → 索引立即变脏（stale），检索开始报 index_stale。

    这是刻意的：换了模型，旧向量和查询向量就不在同一个空间里了，
    继续检索得到的相似度是噪声——比"没有结果"更糟，因为看起来像有。
    """
    deps = _deps(request)
    model = body.model.strip()
    if not model:
        raise validation_failed("model 不能为空。")
    now = utc_now()
    with closing(deps.open_conn()) as conn:
        system_state.set_embedding_model(conn, model, now=now)
        return _embedding_dto(conn)


# ── 设置页真正用到的几条：预览 / 改 / 删 / 默认模型 / 连通性 ──
#
# D7 UI 验收发现这一整组此前**只有 GET /providers 存在**，而前端按 C# 那版的契约
# 调了 8 条：列表形状不对（后端给 `{providers: [...]}`，前端按裸数组解析）、改/删/
# 设默认/测连通/拉模型列表全是 404。表现是：配好的模型在界面上永远显示「未配置模型」，
# 设置页点任何按钮都报错。这里按前端的契约补齐，路由仍然只归部署所有者。


class SettingsPreviewDto(BaseModel):
    """设置页顶部的「现在这套装的是什么」。**只报事实，不报愿望。**"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    base_url: str | None
    model: str | None
    embedding_model: str | None
    # Python 版没有 temperature 这个旋钮（llm/client.py 不传它）。
    # 恒 None 而不是填 0 或 0.7：填个数会让设置页显示一个并不存在、也改不了的参数。
    temperature: float | None
    system_prompt: str
    api_key_configured: bool
    configured: bool
    active_provider_id: int | None


class UpdateProviderBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    name: str
    base_url: str
    # 空串 = 这次不改凭据（与「清空钥匙」区分：那是重新保存一个空 key，不是留空不填）
    api_key: str = ""


class TestResultDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    ok: bool
    latency_ms: int
    count: int | None = None
    error: str | None = None


class FetchModelsBody(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    base_url: str
    api_key: str = ""


_BLOCKED_HOSTS: Final[frozenset[str]] = frozenset(
    {
        # 云厂商元数据服务的固定地址。**它们永远不是模型端点**，被指向这里的唯一
        # 解释是有人想拿这一条当 SSRF 用（D9 红队 R-01 实测这条端点能探端口、
        # 能打任意 path、还能把 Bearer 一起发出去）。
        "169.254.169.254",  # AWS / GCP / Azure / 阿里云的常见形态
        "100.100.100.200",  # 阿里云
        "metadata.google.internal",
        "metadata",
    }
)


def _blocked_reason(base_url: str) -> str | None:
    """这个地址为什么不能打；能打返回 `None`。

    拦的是**永远不合法**的三类，不是「内网」：

    1. 非 http(s)（`file://`、`ftp://`、`gopher://`）；
    2. 云元数据地址（见 `_BLOCKED_HOSTS`）；
    3. URL 里带用户名口令（`http://u:p@host/`）——那会让 httpx 把 Basic 认证
       头发给目标，而用户以为自己只是"填了个地址"。

    **故意不拦环回与私网**：本产品的主场景就是单机部署，模型跑在
    `http://127.0.0.1:11434/v1`（Ollama）或局域网内的推理机上，拦掉它等于
    把这个功能废掉一半。剩下的「平台管理员可以把它当内网探测器」记在
    `docs/验收/未解决问题.md`（U-16）里当作已接受的风险——那个账号本来就能
    改这台机器的全部配置，应用层挡不住它，该挡的是网络出口策略。
    """
    try:
        parsed = urlsplit(base_url)
    except ValueError:
        return "地址格式不对。"
    if parsed.scheme not in ("http", "https"):
        return f"只支持 http 与 https，收到的是「{parsed.scheme or '空'}」。"
    if not parsed.hostname:
        return "地址里没有主机名。"
    if parsed.username or parsed.password:
        return "地址里不要带用户名口令，凭据请填在 API Key 那一格。"
    if parsed.hostname.lower() in _BLOCKED_HOSTS:
        return "这个地址是云厂商的元数据服务，不允许作为模型端点。"
    return None


def _probe_upstream(base_url: str, api_key: str) -> tuple[list[str] | None, str | None, int]:
    """打一次上游的 `GET {base_url}/models`，返回（模型列表, 错误文案, 耗时毫秒）。

    `trust_env=False`：这台机器上配了代理时 httpx 会把本地请求也发给代理，
    于是「测试连通性」测的是代理通不通。失败文案**不带 key**（S-V5）。
    """
    blocked = _blocked_reason(base_url)
    if blocked is not None:
        # 当作「测试结果」返回而不是抛异常：这个端点的产物是结果，连不上也是结果。
        # 抛异常会让设置页把"地址写错了"显示成"服务器错误"。
        return None, blocked, 0
    url = base_url.rstrip("/") + "/models"
    headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
    started = time.monotonic()
    try:
        response = httpx.get(url, headers=headers, timeout=8.0, trust_env=False)
    except (httpx.HTTPError, OSError) as exc:
        # OSError 也要接：DNS/套接字层失败有时不走 httpx 自己的异常族
        # （被拦截的端口、UNC 路径的主机名都是这样），漏掉就变成 500，
        # 而设置页此时该看到的是「连不上」而不是「服务器内部错误」。
        return None, f"连不上：{type(exc).__name__}", int((time.monotonic() - started) * 1000)
    elapsed = int((time.monotonic() - started) * 1000)
    if response.status_code != 200:
        return None, f"上游返回 HTTP {response.status_code}", elapsed
    try:
        payload = response.json()
    except ValueError:
        return None, "上游返回的不是 JSON（base_url 是否少了 /v1？）", elapsed
    items = payload.get("data") if isinstance(payload, dict) else None
    models = [str(item["id"]) for item in items or [] if isinstance(item, dict) and item.get("id")]
    return models, None, elapsed


def _active_snapshot(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """激活供应商 + 它的默认模型（一次 join，缺哪边都不报错——预览要能显示「未配置」）。"""
    row = conn.execute(
        "select p.id as provider_id, p.base_url, p.api_key_encrypted, m.model"
        " from providers p left join provider_models m"
        "   on m.provider_id = p.id and m.is_default = 1"
        " where p.is_active = 1"
    ).fetchone()
    return cast(sqlite3.Row | None, row)


@router.get("/api/settings")
def read_settings(request: Request) -> SettingsPreviewDto:
    """设置页的「当前配置」快照。**没过期的密文与明文都不出去**，只给"配没配"。"""
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        active = _active_snapshot(conn)
        wanted, _ = system_state.embedding_state(conn)
    if active is None:
        return SettingsPreviewDto(
            base_url=None,
            model=None,
            embedding_model=wanted,
            temperature=None,
            system_prompt="",
            api_key_configured=False,
            configured=False,
            active_provider_id=None,
        )
    return SettingsPreviewDto(
        base_url=str(active["base_url"]),
        model=str(active["model"]) if active["model"] is not None else None,
        embedding_model=wanted,
        temperature=None,
        system_prompt="",
        api_key_configured=bool(active["api_key_encrypted"]),
        configured=active["model"] is not None,
        active_provider_id=int(active["provider_id"]),
    )


@router.put(
    "/api/settings/providers/{provider_id}",
    dependencies=[Depends(_platform_only)],
)
def update_provider(request: Request, provider_id: int, body: UpdateProviderBody) -> ProviderDto:
    """改名称 / 地址 / 凭据。留空 api_key 表示不改凭据。"""
    deps = _deps(request)
    if not body.name.strip() or not body.base_url.strip():
        raise validation_failed("name 与 base_url 不能为空。")
    encrypted = ""
    if body.api_key:
        encrypted = crypto.encrypt(cast(str, deps.settings.secret_key), body.api_key)
    now = utc_now()
    with closing(deps.open_conn()) as conn:
        changed = repo.update_provider(
            conn,
            provider_id=provider_id,
            name=body.name.strip(),
            base_url=body.base_url.strip(),
            api_key_encrypted=encrypted,
            now=now,
        )
        if not changed:
            raise not_found("供应商不存在。")
        row = repo.get_provider(conn, provider_id)
        if row is None:  # 刚 update 成功又被删掉：并发情形的唯一解释
            raise not_found("供应商不存在。")
        return _to_dto(row, repo.list_models(conn, provider_id))


@router.delete(
    "/api/settings/providers/{provider_id}", status_code=204, dependencies=[Depends(_platform_only)]
)
def delete_provider(request: Request, provider_id: int) -> Response:
    """删供应商，模型随外键级联删除。

    **删掉的是配置，不是历史**：已经答过的引用快照里存的是文件名与片段原文，
    与会话一起留档——把配置删了不该让昨天的答案变成空白。
    """
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        if not repo.delete_provider(conn, provider_id):
            raise not_found("供应商不存在。")
    return Response(status_code=204)


@router.delete(
    "/api/settings/models/{model_id}", status_code=204, dependencies=[Depends(_platform_only)]
)
def delete_model(request: Request, model_id: int) -> Response:
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        if not repo.delete_model(conn, model_id):
            raise not_found("模型不存在。")
    return Response(status_code=204)


@router.put(
    "/api/settings/providers/{provider_id}/models/{model_id}/default",
    dependencies=[Depends(_platform_only)],
)
def set_default_model(request: Request, provider_id: int, model_id: int) -> ProviderDto:
    """把某个模型设为该供应商的默认（全供应商至多一个默认，S-V8）。"""
    deps = _deps(request)
    now = utc_now()
    with closing(deps.open_conn()) as conn:
        try:
            repo.set_default_model(conn, provider_id, model_id, now=now)
        except LookupError:
            raise not_found("模型不存在或不属于该供应商。") from None
        row = repo.get_provider(conn, provider_id)
        if row is None:
            raise not_found("供应商不存在。")
        return _to_dto(row, repo.list_models(conn, provider_id))


@router.post(
    "/api/settings/providers/{provider_id}/test",
    dependencies=[Depends(_platform_only)],
)
def test_provider(request: Request, provider_id: int) -> TestResultDto:
    """连通性探测：拿库里的钥匙打一次上游 `/models`。

    **失败也返回 200**：这个端点的产物是"测试结果"，不是"测试成功"。
    把连不上表达成 5xx 会让设置页把"地址写错了"显示成"服务器错误"。
    """
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        row = repo.get_provider(conn, provider_id)
    if row is None:
        raise not_found("供应商不存在。")
    key = crypto.decrypt(cast(str, deps.settings.secret_key), row["api_key_encrypted"])
    models, error, elapsed = _probe_upstream(str(row["base_url"]), key)
    return TestResultDto(
        ok=error is None,
        latency_ms=elapsed,
        count=len(models) if models is not None else None,
        error=error,
    )


@router.post(
    "/api/settings/providers/{provider_id}/fetch-models",
    dependencies=[Depends(_platform_only)],
)
def fetch_models_of_provider(request: Request, provider_id: int) -> dict[str, list[str]]:
    """拉上游的模型清单（设置页的「获取模型列表」）。"""
    deps = _deps(request)
    with closing(deps.open_conn()) as conn:
        row = repo.get_provider(conn, provider_id)
    if row is None:
        raise not_found("供应商不存在。")
    key = crypto.decrypt(cast(str, deps.settings.secret_key), row["api_key_encrypted"])
    models, error, _ = _probe_upstream(str(row["base_url"]), key)
    if models is None:
        raise validation_failed(f"拉取模型列表失败：{error}")
    return {"models": models}


@router.post("/api/settings/fetch-models", dependencies=[Depends(_platform_only)])
def fetch_models(request: Request, body: FetchModelsBody) -> dict[str, list[str]]:
    """还没保存供应商时先试一把：给地址和钥匙，返回上游有哪些模型。"""
    if not body.base_url.strip():
        raise validation_failed("base_url 不能为空。")
    models, error, _ = _probe_upstream(body.base_url.strip(), body.api_key)
    if models is None:
        raise validation_failed(f"拉取模型列表失败：{error}")
    return {"models": models}
