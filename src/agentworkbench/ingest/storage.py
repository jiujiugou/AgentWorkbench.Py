"""文件落盘：本地目录，内容寻址。

路径是 `{tenant}/{sha256[:2]}/{sha256}{suffix}`，**由内容决定**。这一条同时解决三件事：

1. 同一份文件传两次，写的还是同一个路径——盘上不可能出现两份（F2 V-1 的负控）。
2. 并发两个请求同传一份，也只会写同一个路径，谁都不产生孤儿。
3. 删除时路径可重算，不依赖库里的记录还能不能读到。

代价是"同内容不同文件名"会覆盖成同一份。这在本域是想要的（去重），
但意味着 `file_name` 只能信库里的列，不能从路径反推。

来源：C# `Documents/FileStorage`（绝对路径，换机器就断；Python 版改相对路径）
服务于：REQ-05 / REQ-10
"""

from __future__ import annotations

import hashlib
from pathlib import Path


class LocalStorage:
    """一个存储根下的读写。相对路径入库，换机器只需换根。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def digest_of(data: bytes) -> str:
        """sha256 在**内存字节**上算，不需要先落盘。

        先落盘再算，去重命中时就会在盘上多留一份孤儿文件——而 V-1 要求"磁盘恰好 1 份"。
        """
        return hashlib.sha256(data).hexdigest()

    def relative_path(self, *, tenant_id: str, digest: str, suffix: str) -> str:
        return f"{tenant_id}/{digest[:2]}/{digest}{suffix.lower()}"

    def put(self, *, tenant_id: str, digest: str, suffix: str, data: bytes) -> str:
        """写盘并返回相对路径。同内容重复写是幂等的。"""
        relative = self.relative_path(tenant_id=tenant_id, digest=digest, suffix=suffix)
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
        return relative

    def read(self, relative: str) -> bytes:
        return (self.root / relative).read_bytes()

    def delete(self, relative: str) -> None:
        """删文件。文件不在不算错——删两次是允许的（补偿流程会重入）。"""
        target = self.root / relative
        target.unlink(missing_ok=True)

    def count(self) -> int:
        """盘上文件数。拒收的负控要它——"拒收后盘上文件数不变"。"""
        return sum(1 for path in self.root.rglob("*") if path.is_file())
