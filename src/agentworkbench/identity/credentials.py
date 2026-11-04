"""凭证零件：口令哈希与票据。**唯一允许碰密码学的地方。**

**放**：`hash_password` / `verify_password`（pwdlib[argon2]）；
`issue_ticket` / `hash_ticket`（标准库 `secrets` + `hashlib`）。
**不放**：SQL——票据行的读写属 `store`。

明文只在这里出现一次：签发时生成、写进 cookie；库里存的是它的 SHA-256。
所以「备份泄露」不等于「会话可复用」，这是设计成不透明票据而不是 JWT 的直接收益。

**两处 API 事实**（pwdlib 0.3.1 实测，2026-11-04）：

- 没有 `check_needs_rehash`（那是 passlib 的 API）。平滑重哈希的入口是
  `verify_and_update`，它一次完成「验证 + 需要时重哈希」，比两步更好用。
- `verify` 对**无法识别的哈希**抛 `UnknownHashError`，不返回 False。
  库里有旧格式或脏数据时，不接住它就是 500——而 500 与「口令错」的差别
  本身就是一条信息。

改坏这里会怎样：`verify_password` 在 `hashed is None` 时直接返回 False
而不跑假校验，账号存在与不存在就有几十毫秒的差别，用户看不到任何报错，
但任何人拿一份邮箱列表就能把注册过的账号全部筛出来。
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Final

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pwdlib.hashers.argon2 import Argon2Hasher

TICKET_BYTES: Final[int] = 32
"""票据明文的熵。32 字节 = 256 bit，URL-safe 编码后 43 个字符。"""

_DUMMY_PASSWORD: Final[str] = "timing-equalizer"  # noqa: S105 —— 假串，不是凭据
"""账号不存在时拿去跑校验的假口令。

它存在的唯一目的是**让「账号不存在」和「口令错」耗时接近**。名字里刻意
不含 credential 以外的词，是为了让它被搜出来时一眼能看出用途。
"""

_hasher: Final[PasswordHash] = PasswordHash((Argon2Hasher(),))

_DUMMY_HASH: Final[str] = _hasher.hash(_DUMMY_PASSWORD)
"""模块加载时算一次。argon2 是有意设计成慢的，所以只算一次、全程复用。"""


def hash_password(password: str) -> str:
    """新口令入库前的哈希。返回的串自带算法与参数，`verify` 靠它自解释。"""
    return _hasher.hash(password)


def verify_password(password: str, hashed: str | None) -> tuple[bool, str | None]:
    """校验口令，返回 `(是否通过, 需要写回的新哈希)`。

    `hashed is None`（账号不存在）时**仍然跑一次假校验**，然后无条件返回
    不通过。不这样做的话，「账号不存在」会立刻返回、而「口令错」要走完
    argon2——响应快慢就是一台账号枚举器，而且它不需要任何额外接口。

    第二个返回值非空表示「校验通过，但存量哈希的参数已过时」，
    调用方应当把它写回库。没有这一步，`needs rehash` 永远等不到时机：
    参数调高之后，存量口令会一直停在部署当天的强度上。

    未知格式的哈希按「不通过」处理，不抛：
    库里有旧格式或脏数据时，抛出去就是 500，而 500 与「口令错」的
    差别本身就是一条可被利用的信息。
    """
    if hashed is None:
        try:
            _hasher.verify(password, _DUMMY_HASH)
        except UnknownHashError:  # pragma: no cover —— 自己算出来的哈希不会认不出
            return False, None
        return False, None

    try:
        return _hasher.verify_and_update(password, hashed)
    except UnknownHashError:
        return False, None


def issue_ticket() -> str:
    """签发票据明文。**只在签发这一刻存在**，之后进 cookie、库里只有哈希。"""
    return secrets.token_urlsafe(TICKET_BYTES)


def hash_ticket(plain: str) -> str:
    """票据入库前的哈希。

    用 SHA-256 而不是 argon2：票据是 256 bit 的随机串，没有可枚举的口令空间，
    慢哈希防的那种攻击在这里不成立——而每个请求都要比对一次，argon2 的
    几十毫秒会直接变成每次请求的固定开销。
    """
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()
