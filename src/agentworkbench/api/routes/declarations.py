"""入口声明表：每条 HTTP 路由必须表态「这条会不会产出 run」。

**为什么这张表必须存在于代码里、并且有测试盯着。**
F4 §1.9 的第一格是「入口判定」：`app.routes` 全集 → 逐条「产出 run / 不产出 + 理由」。
这一格防的不是"写错记录"，是"根本没有记录"——C# 的 `POST /api/knowledge/ask`
走独立链路、不留 run，而用户实际在用的就是它，六格做完五格，剩下的那一格正好
是客户在用的那条。**这种漏法不会报错，也不会被人发现**，除非有人专门问
"还有没有别的路"。

所以表的用法是反的：新增一条路由时，作者必须来添加一行。忘加的后果是
`tests/invariants/test_route_declarations.py` 当场红，而不是三个月后
面对一条查无可查的投诉。

**遍历本身是个陷阱。** FastAPI 0.162 起 `include_router` 挂进 `app.routes` 的是
`_IncludedRouter` 包装，没有 `path` / `methods`——按老写法遍历会得到 **0 条业务路由**，
而 0 条意味着"没有一条未声明"，这条不变量从此恒真且全绿。所以 `api_routes()`
要展开包装，并且调用方必须断言扫描结果非空（见测试里的自检）。

服务于：A-V1 / A-V1b（无后门）
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import FastAPI

# 产 run 的路由：路径 → 为什么产出。
PRODUCING: dict[tuple[str, str], str] = {
    ("POST", "/api/knowledge/ask"): "每次作答产出一条 run：判不足也产（refused），"
    "模型失败也产（failed）——判定与生成都在 run 内部发生。**同时开一轮会话**："
    "会话 + 用户消息先落、回答与引用快照在同一个事务里跟着落（F5）",
    ("POST", "/api/runs"): "异步提交一次作答：与 ask 走同一条执行流（answer_flow），"
    "所以同样产出 run 与会话——202 只是受理，执行在后台线程，run 照记不误（REQ-19）。",
    ("POST", "/api/chat"): "通用对话每轮同样产出且只产出一条 run：不检索、不引用，"
    "但模型调用与 assistant 挂点都在 run 内发生（F7 / D-V5）。",
}

# 不产 run 的路由：路径 → 为什么不产出。
# 理由必须具体到"为什么这条路上没有一次作答"，不许写"不需要"。
NOT_PRODUCING: dict[tuple[str, str], str] = {
    ("GET", "/api/health"): "健康检查：没有业务动作，也没有输入",
    ("GET", "/api/documents"): "读列表：只查已入库的元数据",
    ("POST", "/api/documents"): "上传入库：F2 的文档生命周期，产物是 chunks 不是 run",
    ("GET", "/api/documents/{doc_id}"): "读单份元数据：只查 documents 一行，不触发执行",
    ("GET", "/api/documents/{doc_id}/pages"): "读页原文：只查 document_pages，不触发执行",
    ("GET", "/api/documents/{doc_id}/chunks"): "读块，供界面展示切块结果",
    (
        "GET",
        "/api/documents/{doc_id}/file",
    ): "download original: read one stored file with the same tenant/KB filter as content; no run",
    ("POST", "/api/documents/{doc_id}/reparse"): "重解析：产物是 chunks 的新版本，"
    "失败落在 documents.error_code 上（F2 自己的记录），不是一次作答",
    ("DELETE", "/api/documents/{doc_id}"): "删除：产物是这行不存在了，没有可回放的执行过程",
    ("GET", "/api/knowledge/search"): "只检索不生成：没有模型调用，也没有判定放行——"
    "它是 F4 之前就存在的调试入口，不是作答路径（界面的问答走 ask）",
    ("GET", "/api/conversations"): "读列表：只查 conversations，按归属过滤，不触发执行",
    ("GET", "/api/conversations/{conversation_id}/messages"): "读历史消息与引用快照："
    "只读三张表，回放不是重算——读侧拿不到判定与检索模块（C-V6）",
    ("DELETE", "/api/conversations/{conversation_id}"): "删会话：一次五处同事务的删除，"
    "没有作答、没有模型调用，所以不产 run；删掉的正是 run 与步骤本身（C-V12）",
    ("GET", "/api/runs/{run_id}/trace"): "读一次运行的步骤：只查 agent_runs / agent_steps。"
    "它是 run 的读取端，不是另一次作答",
    ("GET", "/api/runs/{run_id}"): "读一次运行的状态与已落库结果：查 agent_runs 与 messages 那行"
    "占位/正文，不触发执行——状态查询不是新一轮作答（REQ-18）",
    ("POST", "/api/runs/cancel"): "停止一次仍在运行的作答：只把 run 收口成 cancelled，"
    "不启动新的作答、不产生新的 run；迟到的模型结果由 close_step 丢弃（REQ-16）。",
    ("GET", "/api/settings/providers"): "读供应商配置列表：白名单 DTO，不触发模型调用",
    ("POST", "/api/settings/providers"): "新建供应商配置：落 providers 一行（api_key 加密后落库），"
    "配置是读和写，不是一次作答",
    (
        "POST",
        "/api/settings/providers/{provider_id}/activate",
    ): "切换激活供应商：一个事务清 0 置 1，不产生任何模型调用",
    (
        "POST",
        "/api/settings/providers/{provider_id}/models",
    ): "给供应商加模型行：落 provider_models 一行，不产生任何模型调用",
    (
        "GET",
        "/api/settings/embedding",
    ): "读 embedding 一组的配置与 stale 位：只读 system_state 两行，不触发检索或重建",
    ("PUT", "/api/settings/embedding"): "改 embedding 模型：写 system_state 并置索引待重建，"
    "重建本身不在这里发生（那是 F2 的重活），所以不产 run",
    # ── 设置页的补口（D7 发现前端调了但后端没有的那几条） ──
    ("GET", "/api/settings"): "设置页的当前配置快照：一次 join 读 providers + provider_models，"
    "密文与明文都不出去，不触发检索或生成",
    ("PUT", "/api/settings/providers/{provider_id}"): "改供应商：改名/地址/凭据，落一行 UPDATE",
    ("DELETE", "/api/settings/providers/{provider_id}"): "删供应商：配置行删除，模型随外键级联；"
    "历史里的引用快照是留档数据，不因此消失",
    ("DELETE", "/api/settings/models/{model_id}"): "删模型行：一次 DELETE，不触发执行",
    (
        "PUT",
        "/api/settings/providers/{provider_id}/models/{model_id}/default",
    ): "设默认模型：清该供应商的默认位再置一行，两个 UPDATE",
    (
        "POST",
        "/api/settings/providers/{provider_id}/test",
    ): "测连通性：拿库里的钥匙打上游 /models，产物是测试结果而不是一次作答；"
    "失败也返回 200（那是结果，不是错误）",
    (
        "POST",
        "/api/settings/providers/{provider_id}/fetch-models",
    ): "拉上游模型清单：一次 GET /models，不落库",
    ("POST", "/api/settings/fetch-models"): "未保存供应商前的试拉：同样一次 GET /models，不落库",
    ("POST", "/api/auth/login"): "登录：校验口令并签发票据。唯一先于身份的写入口，"
    "没有作答、没有模型调用",
    ("POST", "/api/auth/logout"): "登出：把票据行标记吊销并清 cookie，一次 UPDATE",
    ("GET", "/api/auth/me"): "读当前身份：查票据与账户两行，供前端判断要不要跳登录页",
    ("POST", "/api/auth/password"): "本人改密：换哈希 + 版本 +1 + 吊销其余票据，"
    "不触发任何执行；改密不是一次作答",
    ("POST", "/api/auth/invites/{token}"): "接受邀请建号：写 users 与 invites 两行，"
    "邀请令牌本身就是凭证，与作答无关",
    ("GET", "/api/knowledge-bases"): "知识库元数据：按团队/等级过滤，不读文档内容",
    ("POST", "/api/knowledge-bases"): "建知识库：写 knowledge_bases 一行，不是一次作答",
    ("PATCH", "/api/knowledge-bases/{kb_id}"): "改知识库元数据/可见性：一次 UPDATE，不是一次作答",
    (
        "POST",
        "/api/knowledge-bases/{kb_id}/archive",
    ): "archive KB: soft-state update; keeps documents/index/files, no run",
    ("POST", "/api/knowledge-bases/{kb_id}/unarchive"): "unarchive KB: soft-state update; no run",
    ("POST", "/api/members"): "直接建号：写 users 一行，不是一次作答",
    (
        "POST",
        "/api/members/{user_id}/reset-password",
    ): "管理员重置口令：换哈希并吊销票据，不是一次作答",
    ("GET", "/api/members"): "读成员列表：按团队查 users，只读",
    ("POST", "/api/members/invites"): "发邀请：写 invites 一行。它是账号的入口，"
    "不是一次作答——被邀请的人此时还不存在",
    ("GET", "/api/members/invites"): "list pending invites: tenant-scoped read; no execution",
    (
        "DELETE",
        "/api/members/invites/{invite_id}",
    ): "revoke invite: one soft-state UPDATE; no execution",
    ("PATCH", "/api/members/{user_id}"): "改角色：一次 UPDATE，角色下个请求生效",
    ("POST", "/api/members/{user_id}/disable"): "停用成员：置 status 并吊销全部票据，"
    "两次 UPDATE 同一事务，没有执行过程",
    ("POST", "/api/members/{user_id}/enable"): "恢复成员：一次 UPDATE，不碰锁定列",
    (
        "GET",
        "/api/admin/events",
    ): "read admin audit: tenant-scoped admin_events query; no execution",
}

ABSENT: dict[tuple[str, str], str] = {
    ("GET", "/api/auth/forgot-password"): "不做自助找回（§2.1）：第一版由管理员重置口令。"
    "做成路由的话，「忘记密码」这条路上没有任何人能验证身份",
    ("POST", "/api/auth/forgot-password"): "同上：发送重置邮件需要邮件服务，而 §2.1 明确不做邮件",
    ("GET", "/api/auth/verify"): "不做邮箱确认（§2.5）：开了没人验收，等于扩大攻击面",
    ("POST", "/api/auth/verify"): "同上。这三条一起登记，是因为身份框架的默认路由里"
    "它们通常都在——而「框架自带但没人验收」正是本域拒绝整套框架的理由之一（I-V15）",
}
"""**不存在的路由**：登记它们是为了让「不存在」是一个被写下来的决定，不是一次遗漏。

它们不在 `app.routes` 里（没有注册），所以 `undeclared()` 看不到它们；
`I-V15` 的测试自己遍历这张表断言 404。写成一张表而不是注释，是因为
「哪天有人为了省事把 `/api/auth/forgot-password` 加回来」时，
这张表是唯一会挡在路上的东西。
"""


def absent_paths() -> list[tuple[str, str]]:
    return sorted(ABSENT)


FRAMEWORK_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})
"""框架自带的文档路由。它们不是业务入口，不参与声明，但也不许悄悄多出来。"""


def declared() -> dict[tuple[str, str], bool]:
    """声明表：路径 → 是否产 run。两张表合并，重复登记算错（见测试）。"""
    table = {key: True for key in PRODUCING}
    table.update({key: False for key in NOT_PRODUCING})
    return table


def api_routes(app: FastAPI) -> list[tuple[str, str]]:
    """列出所有业务路由的 `(方法, 路径)`。

    展开 `_IncludedRouter`（见模块 docstring）。`HEAD` 过滤掉——它是 GET 的
    自动伴生，不值得在声明表里占一行，而"有 GET 没声明 HEAD"会变成噪音。

    框架路径（`/docs` 等）在这里一并返回，由调用方用 `FRAMEWORK_PATHS` 排除：
    排除这件事要看得见，藏在遍历里就没人知道它排除了什么。
    """
    found: list[tuple[str, str]] = []
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if methods and path:
            found.extend((method, path) for method in sorted(methods) if method != "HEAD")
            continue
        inner = getattr(route, "original_router", None)
        if inner is not None:
            stack.extend(getattr(inner, "routes", []))
            continue
        # 既没有 path/methods 也不是可展开的路由组：Mount（静态文件）、
        # WebSocket 之类。**不静默跳过**——将来加了 `app.mount("/")`，
        # 它下面的所有路径都不再被这条不变量覆盖，而那是察觉不到的。
        raise TypeError(f"无法解析的路由类型：{type(route).__name__}")
    return found


def undeclared(app: FastAPI) -> set[tuple[str, str]]:
    """既不在 PRODUCING 也不在 NOT_PRODUCING 里的业务路由。"""
    table = declared()
    return {
        route for route in api_routes(app) if route not in table and route[1] not in FRAMEWORK_PATHS
    }


def routes_that_produce(app: FastAPI) -> Iterator[tuple[str, str]]:
    """真正产 run 的路由（声明 + 实际存在）。表里有、代码里没有的单独由测试查。"""
    present = set(api_routes(app))
    yield from (route for route in PRODUCING if route in present)
