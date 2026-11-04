"""检索侧的数据类型：命中、索引结果、覆盖度取值。

这三个结构比「知识引擎接口」值钱：接口只有一个实现，属间接；
而「块数 vs 向量数」判覆盖度的规则，是拿数据说话的那一条。

来源：C# `Knowledge.Abstractions/`（接口与 registry 已删，只留这三个）
服务于：REQ-07
"""

from __future__ import annotations
