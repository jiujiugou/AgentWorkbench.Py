"""迁移脚本。每条一个文件，`down_revision` 串成链。

命名：`NNNN_动词短语.py`。**不要改已发布的迁移**——它已经在某个客户的库里跑过了，
改它等于让两台机器的 schema 分叉，而分叉时不报错。

`0001_initial` 不写 DDL，直接执行 `db.engine.SCHEMA_SQL`：schema 只能有一个来源，
迁移脚本与 `create_schema()` 各写一份，一定会在某次改动后分叉。
"""
