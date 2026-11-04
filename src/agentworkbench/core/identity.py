"""身份记录：租户与用户。

阶段 1 不做身份（只有固定归属的脚手架），但归属参数必须继续带、
不许删签名——阶段 2 只是把固定值换成登录值。

来源：C# `Domain/Identity/IdentityRecords.cs`
服务于：REQ-01 ~ REQ-03、REQ-AUTH-02（阶段 2）
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Owner:
    """一次读写属于谁。**没有默认值、没有"全部"取值。**

    做成一个类型而不是两个字符串参数，是为了让"漏传"在调用点就报错：
    两个独立的 `str` 参数在多次调用中很容易顺序错位，而 SQL 里
    `tenant_id = ?` 与 `user_id = ?` 都是字符串比较——反了不报错，
    只是查不到，查不到又会被当成"这条不存在"。

    空串在构造处就拒绝：`= ''` 不会变成"不过滤"（它匹配不到任何行），
    但每一次读写都会静默失败，而失败现象看起来是"数据没了"。
    """

    user_id: str
    tenant_id: str

    def __post_init__(self) -> None:
        if not self.user_id or not self.tenant_id:
            raise ValueError("Owner 的两个字段都不能为空")
