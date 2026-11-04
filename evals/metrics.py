"""指标定义：命中率、平均倒数排名、误拒率、引用核实率、无出处句比例。

定义写在这里而不是散在脚本里，否则两处各算一遍、迟早漂移，
而且漂移时不会报错。

来源：C# `tests/benchmark/rag/` 五个脚本里的统计部分
服务于：REQ-07 / REQ-08 / REQ-09 的度量
"""

from __future__ import annotations
