"""会话判定的短期复用：`session_invalidate_seconds` 的**唯一消费者**。

## 它是什么

每个受保护请求都要「cookie 摘要 → 查 `tickets` → 查 `users` → 比较四步」。这在本地
SQLite 上是几十微秒，所以性能不是理由。真正的理由是那个旋钮本身：它同时决定了
**一次吊销要多久才生效**。ASP.NET Identity 的同名旋钮叫 `SecurityStampValidationInterval`，
默认 30 分钟——也就是说「改了密码，另一台设备最多还能用半小时」是行业里的**默认行为**，
而不是 bug。

我们把它显式成一个配置键，而不是「反正很快，每次都查」。两种做法在功能上等价，
区别在于**哪一种在出事后能回答「多久之内还算有效」**。

## 语义是「最大陈旧度」

- 配 `0` → 不缓存。吊销 / 停用 / 改密的**下一个请求**即 401（F1 §1.5：测试拿确定性靠它）。
- 配 `N` → 一次判定最多被复用 `N` 秒。于是停用之后**最多** `N` 秒内旧 cookie 仍可用。

判据 `I-V6` 要的是「一个配置窗口之内一定失效」，`I-V10` 要的是「改配置行为真的变」。
两者合起来的读法就是上面这两句——它不是「立刻失效」的同义词，除非配 0。

## 这层缓存**不主动失效**，这是刻意的

如果每条吊销路径都回头清一遍缓存，这个键就没有任何可观察的效果了——配 0 和配 600
行为一致，而「配了不生效的旋钮」正是 §1.8 列在拒绝项里的东西。代价写在明处：
**要「立即生效」就配 0**，别指望这里再补一套失效通知（那等于把已删除的语义偷偷加回来）。

## 存的是 `Principal` 不是 `Decision`

`Decision` 带 `reason`、带 `outcome`，而能进缓存的只有 `ALLOW`——拒绝不进缓存：
一个刚被拒的人立刻重试，应该重新判一次，而不是复用一次「刚才不行」。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from agentworkbench.identity.models import Principal

__all__ = ["MAX_ENTRIES", "SessionCache"]

MAX_ENTRIES: int = 1024
"""条目上限。**不是为了省内存，是为了防一条无界增长**：每个登录过的票据都会进来一条，
而票据在过期之前不会自己消失。到上限时先清已过期的，清完还满就整体清空——
清空的代价只是下一次多查两次库，而无限增长的代价是进程慢慢吃满内存。"""


class SessionCache:
    """票据摘要 → 已验过的身份，带过期时间。

    `clock` 可注入（单调时钟，不是墙上时间）：判据里的「窗口过了」不该靠
    `sleep(60)` 来验，那是代码规范 §6.4 明令禁止的。单调时钟还避开了一次
    校时把所有人踢下线的经典事故。
    """

    def __init__(
        self,
        ttl_seconds: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, tuple[float, Principal]] = {}
        # 路由跑在线程池里，两个请求同时读写这个 dict 是常态。
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """`False` 表示「每次都重新判」。0 是合法配置，不是「关掉功能」。"""
        return self._ttl > 0

    def get(self, ticket_hash: str) -> Principal | None:
        if not self.enabled:
            return None
        with self._lock:
            entry = self._entries.get(ticket_hash)
            if entry is None:
                return None
            expires_at, principal = entry
            if expires_at <= self._clock():
                # 顺手删：过期条目留着不会出错，但会让上限提前触发
                del self._entries[ticket_hash]
                return None
            return principal

    def put(self, ticket_hash: str, principal: Principal) -> None:
        if not self.enabled:
            return
        with self._lock:
            if len(self._entries) >= MAX_ENTRIES:
                self._sweep()
            self._entries[ticket_hash] = (self._clock() + self._ttl, principal)

    def clear(self) -> None:
        """整表清空。生产上只有一处会调（进程启动），测试里用来隔离用例。"""
        with self._lock:
            self._entries.clear()

    def _sweep(self) -> None:
        """调用方必须已持锁。"""
        moment = self._clock()
        for key in [k for k, (expires_at, _) in self._entries.items() if expires_at <= moment]:
            del self._entries[key]
        if len(self._entries) >= MAX_ENTRIES:
            self._entries.clear()
