"""D9 红队环境：一台**真的能连**的部署实例，供独立红队与 repro 脚本共用。

为什么不是 TestClient
---------------------
`TestClient` 走的是 ASGI 直连：不经过 uvicorn 的 header 解析、不经过真实的
socket、也没有多进程/多线程的真实交错。红队要找的正是"框架层与中间件层之间
的缝"，那些缝在 TestClient 里根本不出现。

所以这里的形态与 D7 的 UI 验收一致：**真 uvicorn + 真 SQLite + 真文件目录**，
只把模型端点换成本地假服务（否则红队每敲一次都会走真网络、花真钱）。

两种数据形态
------------
- 默认：复制 `data/` 的**已迁移生产库**（2 个团队 / 3 个账号 / 61 份文档）。
  跨团队、IDOR、审计这类攻击需要"隔壁真的有人有资料"才有意义。
- `--fresh`：空库 + 走部署脚本建首个管理员。用来验"Do it yourself"那条路
  （能不能在没有手工 SQL 的情况下把系统用起来）。

用法
----
    python scripts/redteam_env.py up --name rt1         # 起一个实例并阻塞（Ctrl-C 停）

起好后把 `env.json` 的路径与内容交给红队：
`reports/acceptance/2026-11-05/d9/env-rt1/env.json`。

repro 脚本里用它当上下文管理器：

    with Env("r01") as env:
        r = env.client("wang@migrated.local").get("/api/documents")
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():  # pragma: no cover - 非 Windows / 未建 venv
    PYTHON = Path(sys.executable)

EVIDENCE = ROOT / "reports" / "acceptance" / "2026-11-05" / "d9"
SOURCE_DATA = ROOT / "data"

ADMIN_EMAIL = "li@migrated.local"  # 平台管理员（is_platform_admin=1，华东自动化）
OWNER_EMAIL = "wang@migrated.local"  # 华北智造 管理员
MEMBER_EMAIL = "zhao@migrated.local"  # 华北智造 成员（普通账号）

ANSWER = "按手册，先查通讯波特率 19200，再核对站号 3，最后断电重启观察指示灯[1]。"
"""假模型的固定回答。带一个 `[1]` 角标，好让引用链在红队里也走完整路径。"""


class _FakeModel(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"data": [{"id": "redteam-fake"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        body = json.dumps(
            {
                "id": "chatcmpl-rt",
                "object": "chat.completion",
                "created": 0,
                "model": "redteam-fake",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": ANSWER},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 800, "completion_tokens": 40, "total_tokens": 840},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """安静：假模型的访问日志对红队没有价值。"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Env:
    """一次红队运行的环境。用完即弃：每个实例一个唯一目录，不删别人。"""

    def __init__(self, name: str, *, fresh: bool = False, quiet: bool = False) -> None:
        stamp = datetime.now(UTC).strftime("%H%M%S")
        self.name = name
        self.fresh = fresh
        self.quiet = quiet
        self.dir = EVIDENCE / f"env-{name}-{stamp}"
        self.data = self.dir / "data"
        self.password = secrets.token_urlsafe(12)
        self.secret = secrets.token_urlsafe(24)
        self.port = _free_port()
        self.model_port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self._server: subprocess.Popen[bytes] | None = None
        self._model: ThreadingHTTPServer | None = None
        self.info: dict[str, Any] = {}

    # ── 生命周期 ──

    def __enter__(self) -> Env:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(parents=True, exist_ok=True)
        if self.fresh:
            self._bootstrap()
        else:
            self._clone_production()
        self._start_model()
        self._reset_credentials()
        self._start_server()
        self._write_env()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def stop(self) -> None:
        if self._server is not None:
            self._server.terminate()
            try:
                self._server.wait(timeout=20)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self._server.kill()
            self._server = None
        if self._model is not None:
            self._model.shutdown()
            self._model.server_close()
            self._model = None

    # ── 准备 ──

    def _clone_production(self) -> None:
        """复制生产库与文件目录。

        **复制而不是引用**：红队会删文档、改口令、锁账号，那些必须只落在副本上。
        目录名带时间戳，所以不必也不再做任何删除（这台机器上有删除守卫）。
        """
        source_db = SOURCE_DATA / "agentworkbench.db"
        if not source_db.exists():
            raise SystemExit(f"没有可复制的数据：{source_db}")
        shutil.copy2(source_db, self.data / "agentworkbench.db")
        for suffix in ("-wal", "-shm"):
            leftover = Path(str(source_db) + suffix)
            if leftover.exists():
                shutil.copy2(leftover, Path(str(self.data / "agentworkbench.db") + suffix))
        if (SOURCE_DATA / "files").exists():
            shutil.copytree(SOURCE_DATA / "files", self.data / "files")

    def _bootstrap(self) -> None:
        env = os.environ | {
            "AWB_DATA_DIR": str(self.data),
            "AWB_SECRET_KEY": self.secret,
            "AWB_BOOTSTRAP_PASSWORD": self.password,
        }
        done = subprocess.run(
            [
                str(PYTHON),
                str(ROOT / "scripts" / "bootstrap_identity.py"),
                "--email",
                "admin@redteam.local",
                "--tenant",
                "红队空库团队",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        if done.returncode != 0:
            raise SystemExit(f"空库引导失败（{done.returncode}）：{done.stdout}\n{done.stderr}")

    def _start_model(self) -> None:
        self._model = ThreadingHTTPServer(("127.0.0.1", self.model_port), _FakeModel)
        threading.Thread(target=self._model.serve_forever, daemon=True).start()

    def _reset_credentials(self) -> None:
        """把三个账号的口令统一成本次运行的随机值，并把供应商指向本地假模型。

        口令统一是为了让红队脚本可复现（生产口令只存在于迁移记录里，不该被红队依赖）。
        供应商改指向是必须的：否则红队每点一次"提问"都会打真上游、花真钱。
        """
        from agentworkbench.db import crypto, engine
        from agentworkbench.identity import service

        conn = engine.connect(self.data / "agentworkbench.db")
        now = datetime.now(UTC)
        users = [
            {"email": str(row["email"]), "id": str(row["id"]), "role": str(row["role"])}
            for row in conn.execute("select id, email, role from users")
        ]
        for user in users:
            # 红队夹具要直接打业务面，所以显式模拟“已经轮换过初始口令”。
            service.reset_password(
                conn,
                user_id=user["id"],
                new_password=self.password,
                now=now,
                must_change_password=False,
            )
        with conn:
            # 钥匙也得是真密文：D6 之后"解不开的密文"会被翻译成 503 不可重试的
            # 配置错误，红队就看不到真正的问答路径了（要测注入就得先能问到）。
            conn.execute(
                "update providers set base_url = ?, api_key_encrypted = ?",
                (
                    f"http://127.0.0.1:{self.model_port}/v1",
                    crypto.encrypt(self.secret, "sk-redteam-fake"),
                ),
            )
        self.info["users"] = users
        conn.close()

    def _embedding_env(self) -> dict[str, str]:
        """给服务配上真实的查询侧嵌入。

        为什么非要真嵌入：库里的 437 条片段向量是 `bge-m3` 算的，查询侧若退回
        `HashEmbedder`（假向量），检索命中就是随机的，判定层会把每一问都判成
        "依据不足"——红队就永远看不到**带引用的回答**，而引用注入、编号越界、
        答案里的 HTML 这些攻击全都要先有回答才能试。

        key 从生产库里现取（用迁移时那把密钥解）：它在 `data/` 里本来就是密文，
        这里只在进程内解开、只经环境变量传给子进程，不落盘、不打印。
        取不到就退回占位嵌入并**明确警告**——那种环境下"问不出答案"不是缺陷。
        """
        from agentworkbench.db import crypto

        key = os.environ.get("AWB_D9_EMBEDDING_KEY", "")
        if not key:
            source = SOURCE_DATA / "agentworkbench.db"
            secret = os.environ.get("AWB_D9_SOURCE_SECRET", "")
            if not secret:
                print(
                    "未设置 AWB_D9_SOURCE_SECRET，无法从源库解出 embedding key；"
                    "退回占位嵌入（不落盘、不打印任何密钥）。",
                    file=sys.stderr,
                    flush=True,
                )
                return {}
            if source.exists():
                import sqlite3

                conn = sqlite3.connect(source)
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "select api_key_encrypted from providers where name = 'siliconflow'"
                ).fetchone()
                conn.close()
                if row is not None:
                    try:
                        key = crypto.decrypt(secret, str(row["api_key_encrypted"]))
                    except ValueError:
                        key = ""
        if not key:
            print(
                "警告：没有可用的嵌入 key（设 AWB_D9_EMBEDDING_KEY 或 AWB_D9_SOURCE_SECRET），"
                "本实例用占位嵌入——问答会大量判『依据不足』，不要把这当成缺陷。",
                file=sys.stderr,
                flush=True,
            )
            return {}
        return {
            "AWB_EMBEDDING__BASE_URL": "https://api.siliconflow.cn/v1",
            "AWB_EMBEDDING__API_KEY": key,
            "AWB_EMBEDDING__MODEL": "BAAI/bge-m3",
        }

    def _start_server(self) -> None:
        env = os.environ | {"AWB_DATA_DIR": str(self.data), "AWB_SECRET_KEY": self.secret}
        env |= self._embedding_env()
        log = (self.dir / "server.log").open("wb")
        self._server = subprocess.Popen(
            [
                str(PYTHON),
                "-m",
                "agentworkbench.cli",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                if (
                    httpx.get(f"{self.base}/api/health", timeout=1.0, trust_env=False).status_code
                    == 200
                ):
                    return
            except httpx.HTTPError:
                time.sleep(0.2)
        raise SystemExit(f"服务 90 秒内没起来，看 {self.dir / 'server.log'}")

    def _write_env(self) -> None:
        from agentworkbench.db import engine

        conn = engine.connect(self.data / "agentworkbench.db")
        tenants = [
            {"id": str(r["id"]), "name": str(r["name"])}
            for r in conn.execute("select id, name from tenants")
        ]
        docs = [
            {"id": str(r["id"]), "fileName": str(r["file_name"]), "tenantId": str(r["tenant_id"])}
            for r in conn.execute("select id, file_name, tenant_id from documents")
        ]
        users = [
            {
                "email": str(r["email"]),
                "id": str(r["id"]),
                "role": str(r["role"]),
                "tenantId": str(r["tenant_id"]),
                "isPlatformAdmin": bool(r["is_platform_admin"]),
            }
            for r in conn.execute("select id, email, role, tenant_id, is_platform_admin from users")
        ]
        conn.close()

        self.info |= {
            "base": self.base,
            "password": self.password,
            "users": users,
            "tenants": tenants,
            "documents": docs[:20],
            "documentCount": len(docs),
            "dataDir": str(self.data),
            "db": str(self.data / "agentworkbench.db"),
            "serverLog": str(self.dir / "server.log"),
        }
        (self.dir / "env.json").write_text(
            json.dumps(self.info, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ── 给攻击方用的小工具 ──

    @property
    def pid(self) -> int:
        """服务进程号。repro 里量内存/CPU 要用它（`psutil` 按 pid 取）。"""
        if self._server is None:
            raise RuntimeError("服务还没起来")
        return self._server.pid

    def client(self, email: str) -> httpx.Client:
        """登录并返回一个带 cookie 的客户端。"""
        client = httpx.Client(base_url=self.base, timeout=30.0, trust_env=False)
        response = client.post("/api/auth/login", json={"email": email, "password": self.password})
        if response.status_code != 200:
            client.close()
            raise RuntimeError(f"{email} 登录失败：{response.status_code} {response.text[:200]}")
        return client


def main() -> int:
    parser = argparse.ArgumentParser(description="起一台红队用的真实实例")
    parser.add_argument("up" if len(sys.argv) < 2 else sys.argv[1], nargs="?")
    parser.add_argument("--name", default="rt")
    parser.add_argument("--fresh", action="store_true", help="空库 + 部署脚本引导")
    args = parser.parse_args()

    with Env(args.name, fresh=args.fresh) as env:
        print(json.dumps(env.info, ensure_ascii=False, indent=2), flush=True)
        print(f"\nREADY {env.base}   env.json: {env.dir / 'env.json'}", flush=True)
        print("Ctrl-C 停止。", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
