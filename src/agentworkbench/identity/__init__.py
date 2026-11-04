"""身份与访问控制域。

**这个包是纵向切的**（2026-11-04 定）：别的域按层横向散在 `core` / `db` / `api` 里，
`identity` 横切每一个请求，所以让「一个域一个目录」。包内仍然单向，
由 `pyproject.toml` 的第二条 importlinter 契约守着：

    service → store / credentials → policy / models

`models` 与 `policy` 不知道有没有数据库；`credentials` 不知道用户表长什么样；
`store` 只写 SQL、不写「能不能」。这条链反过来即违规。

`Owner` **不在这里**，它在 `core/identity.py`：它是所有层共用的值对象，
放在最底层才不会被依赖方向绑住。把它搬进本包会让 `db` 与 `chat`
从下层反向依赖能力层。
"""

from __future__ import annotations

__all__: list[str] = []
