"""凭据列加密：api_key 只以密文落库。

为什么是 Fernet 而不是手搓：XOR 流密码 + SHA-256 派生也能写出"看起来加密了"
的东西，但把「备份文件被拷走」这条防线建立在自己写的密码学上，正是
组件表存在的理由（"不许自己搓"的典型情形）。

密钥从 `AWB_SECRET_KEY`（环境变量）派生：SHA-256 → 32 字节 → Fernet 要求的
url-safe base64。**不存密钥、不存派生盐**——派生是确定性的，同一把密钥
任何时候都能解开同一列；代价是换密钥 = 全部密文作废（F6 §5.2：轮换是
第二次真实备份演练时的活，这里只解决"文件被拷走"）。

来源：C# `M006_CreateProviders.cs`（注释原话「P2 再加密」——这一版把它做完）
服务于：S-V6（REQ-22①）
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

# 这是给「拒绝启动」的文案，不是凭据——S105 的模式匹配只认变量名，显式豁免。
MISSING_SECRET = (
    "AWB_SECRET_KEY 未配置：api_key 将以明文落库，拒绝启动。"  # noqa: S105
    "请设置环境变量后重试（这是刻意失败，不是遗漏——静默用明文正是要消灭的形态）。"
)


class MissingSecretKey(RuntimeError):
    """加密密钥未配置。**启动时抛**，不是第一个写凭据的请求才炸。"""


def require_secret(secret: str | None) -> str:
    """空密钥 = 明文落库 = 判据 S-V6 的负控形态。启动路径必须先过这一关。"""
    if not secret:
        raise MissingSecretKey(MISSING_SECRET)
    return secret


def _fernet(secret: str) -> Fernet:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(secret: str, plaintext: str) -> str:
    """明文 → 密文。空串按空处理：本地端点允许"没有钥匙"，不该加密空串。"""
    if not plaintext:
        return ""
    return _fernet(secret).encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(secret: str, ciphertext: str | None) -> str:
    """密文 → 明文。解不开（密钥换过 / 库被换过）必须报错，不许返回空串冒充"没配"。

    返回空串与解密失败是两件事：前者是"这个端点不需要钥匙"（合法），
    后者是"这份密文不是这把密钥加密的"（库或密钥被动过）。混成一个 None
    会让第二种情况静默变成第一种，用户只会看到一次神秘 503。
    """
    if not ciphertext:
        return ""
    try:
        return _fernet(secret).decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError(
            "api_key 解密失败：密文与当前 AWB_SECRET_KEY 不匹配"
            "（密钥换过，或库不是这份密钥加密的）。重新保存一次供应商凭据即可恢复。"
        ) from exc
