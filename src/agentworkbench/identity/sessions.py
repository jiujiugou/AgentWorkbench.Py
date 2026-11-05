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

**默认值是 0（D9 改）。** 原来默认 60，理由是「几十微秒不值得每次查库」。D9 的红队
把这条默认值的真实代价量出来了：被停用的管理员在这 60 秒内还能改别人的角色，
而**改角色这一步的结果不会随窗口结束而消失**——窗口是临时的，后果是永久的。
同一个缺陷还有第二种形态：`members` 那组路由自己又判了一次库，于是「停用」
在成员管理页立刻生效、在文档页要等一个窗口，用户看到的是**一半生效**。
几十微秒换这两件事，不划算。

## 吊销路径会定点清缓存，但这不是「让旋钮失效」

`forget_ticket` / `forget_user` 只在**动作发生的那一刻**摘掉受影响的条目：登出摘这一张，
停用/改密/改角色摘这个人名下的全部。它保证「刚刚做的事立刻算数」，但不改变
「别人的票最多陈旧 N 秒」这个语义——所以配 0 与配 600 仍然可观察地不同（`I-V10`）。

多进程部署时这个清除只作用于本进程：要跨 worker 即时，只能把 TTL 配 0，
而默认就是 0。别把这条补强当成替代。

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

    def forget_ticket(self, ticket_hash: str) -> None:
        """吊销一张票时把它从缓存里摘掉。

        **为什么需要这个**：`ttl > 0` 时缓存命中就直接返回 `Principal`，不再查库，
        于是「这张票还在不在」这件事被缓存跳过了一个窗口。窗口本身是设计里的取舍
        （`SecurityStampValidationInterval` 那一路），但**语义上不一致**：
        `members` 那组路由自己又判了一次库，于是「停用」在成员管理页立刻生效、
        在文档页要等一个窗口。用户看到的是一半生效。

        定点清除而不是整表清空：一张票登出不该让所有人的下一次请求都多查两次库。
        """
        if not self.enabled:
            return
        with self._lock:
            self._entries.pop(ticket_hash, None)

    def forget_user(self, user_id: str) -> None:
        """停用 / 改密 / 改角色之后，把这个人的所有缓存条目摘掉。

        停用一个管理员时，他名下可能有多张票（多台设备）。全部摘掉之后，
        这些票在**本进程**内的下一个请求就会重新走判定四步——于是「已停用」
        立刻生效，而不是等他再干一次管理操作才发现。

        多进程部署（`--workers N`）时这里只清得掉本进程的表：别的 worker 仍按
        各自的 TTL 陈旧。要跨进程即时，只能把 TTL 配 0（默认就是 0）——
        所以这条是「即使调大了窗口也别把刚做的动作拖后」的补强，不是替代。
        """
        if not self.enabled:
            return
        with self._lock:
            for key in [
                k
                for k, (_, principal) in self._entries.items()
                if principal.owner.user_id == user_id
            ]:
                del self._entries[key]

    def _sweep(self) -> None:
        """调用方必须已持锁。"""
        moment = self._clock()
        for key in [k for k, (expires_at, _) in self._entries.items() if expires_at <= moment]:
            del self._entries[key]
        if len(self._entries) >= MAX_ENTRIES:
            self._entries.clear()
