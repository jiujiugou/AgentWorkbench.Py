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
from contextlib import closing
from typing import cast

from fastapi import APIRouter, Depends, Request
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
       得到的是 403 而不是 422。函数体第一行的版本会先被 pydantic 校验一遍再拒绝，
       于是非所有者能读到这个消息里的字段清单。
       （注意边界：**畸形 JSON** 在 `request.json()` 阶段就失败，比依赖更早，
       那种请求一定 422——这不是本函数能管的，也不该写进判据。）
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
