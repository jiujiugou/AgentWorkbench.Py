"""HTTP 契约的载体：DTO 的形状就是前端 `types.ts` 的形状。

**这一层是唯一允许 pydantic 出现的地方。** `core` 用 dataclass + 枚举，
`api` 用 pydantic——校验库不进核心层，核心层才换得掉 Web 框架。

命名是这里最容易出事的一处：前端 TS 直接读 `p.fileName`、`p.vectorScore`，
**Python 默认吐 snake_case，对不上时不会报错，页面只是一片空白**。
所以每个模型都开 `alias_generator=to_camel`，且 `populate_by_name=True`
（内部按 python 名构造，出网按别名序列化）。

来源：C# `Webapi/Controllers/*.cs` 里的匿名对象 + `web/src/api/documents.ts`
服务于：REQ-17（可只经 HTTP 完成）
"""

from __future__ import annotations

import sqlite3
from typing import Any, cast

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from agentworkbench.core.documents import DocStatus, IndexState, describe_index_state


class ApiModel(BaseModel):
    """所有 DTO 的基类。出网一律 camelCase，入参两种写法都收。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class UserDto(ApiModel):
    """一个账户的对外形状（F1）。

    **逐字段白名单**：口令哈希在 `Account` 上就没有，`credential_epoch` 与
    `lockout_until` 不进这里。前者是内部版本号（泄露它等于告诉攻击者
    「这张票是哪一代」），后者是「谁被系统锁了」——而成员列表**每个成员都能看**，
    把锁定状态放进去等于让同事知道「他刚才输错了几次密码」。

    没有 `hasPassword` 之类：这个域没有「有没有设口令」这种状态，
    建号必须有口令（`password_hash` NOT NULL）。
    """

    id: str
    email: str
    role: str
    status: str
    tenant_id: str


class AuthUserDto(UserDto):
    """登录 / me 的返回。**只在身份端点加这一列**。

    成员列表仍用 `UserDto`：谁还没改初始口令不是同事需要知道的信息，
    而且那会让“成员列表”的形状随身份状态变化。
    """

    must_change_password: bool


class DocumentDto(ApiModel):
    id: str
    file_name: str
    status: str
    page_count: int
    size_bytes: int
    sha256: str
    error: str | None = None
    created_at: str
    parsed_at: str | None = None
    # 这两个数与 `index_state` 一起回答"能不能检索到"，不看 status 列。
    chunk_count: int = 0
    vector_count: int = 0
    index_state: str = IndexState.PENDING

    @classmethod
    def from_row(cls, row: sqlite3.Row, *, chunk_count: int, vector_count: int) -> DocumentDto:
        status = DocStatus(str(row["status"]))
        return cls(
            id=str(row["id"]),
            file_name=str(row["file_name"]),
            status=str(status),
            page_count=int(row["page_count"]),
            size_bytes=int(row["size_bytes"]),
            sha256=str(row["sha256"]),
            error=_opt_str(row["error"]),
            created_at=str(row["created_at"]),
            parsed_at=_opt_str(row["parsed_at"]),
            chunk_count=chunk_count,
            vector_count=vector_count,
            index_state=str(describe_index_state(status, chunk_count, vector_count)),
        )


class PageDto(ApiModel):
    page_no: int
    raw_text: str
    # Python 版没有独立存清理后文本（清理在切块时做，不单独留一份）。
    # 前端是 `p.cleanedText || p.rawText`，null 会自然回落到原文。
    cleaned_text: str | None = None


class ChunkDto(ApiModel):
    seq: int
    page_from: int
    page_to: int
    heading: str | None
    content: str
    char_count: int


class ErrorDto(ApiModel):
    """错误体。**`message` 是给前端直接显示的那一句**（前端 `parseError` 只认它），
    `code` 是给程序判断的，`retryable` 是给调用方决定要不要重试的。"""

    code: str
    message: str
    retryable: bool = False


def _opt_str(value: Any) -> str | None:
    """`sqlite3.Row` 的列是 `Any`。这里收敛成 `str | None`，不让 Any 漏进 DTO。"""
    return None if value is None else cast(str, value)
