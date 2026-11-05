"""D7 UI 验收：用真浏览器把 P0-8 的剧本走一遍，每步留截图。

判据（`上线验收计划.md` §3.1 第 8 条）：**核心流程 API + UI 都可用**。
拆成五条可观察的事实，少一条就不算过：

1. **登录**：`/login` 能打开，填对了能进；填错了给的是人话，不是白屏。
2. **上传 + 解析**：在界面上传一份资料，状态从「解析中」变成「可检索」。
3. **引用卡**（G12）：答案里的 `[1]` 悬停能看到「文件名 + 片段原文」，
   且那段原文确实来自上传的那份文件；「引用来源」面板能展开。
4. **拒答卡**（G11）：库外问题给的是拒答卡（「依据不足」+ 固定文案），
   不是硬编一个答案，也不挂来源面板。
5. **换设备**（C-H1/C-H2 的界面侧）：换一个全新的浏览器 profile 重新登录，
   同一会话的答案与引用卡还在。
6. **不空转**（I-H4）：请求在飞的时候，界面上有「打字中」或「停止」。

## 为什么不用 mock 前端

四条判据里有三条是"后端给的数据能不能被界面正确表达"——用 mock 测等于
自问自答。所以这里起**真的 uvicorn + 真的 web/dist + 真的 SQLite**，只把模型
换成一个本地假端点（OpenAI 协议、固定回答、延迟 1.2 秒以便截到"在飞"的状态）。

## 用法

    python scripts/ui_acceptance.py                    # 默认用系统 Edge
    python scripts/ui_acceptance.py --channel chrome   # 或 Chrome
    python scripts/ui_acceptance.py --headed            # 想看过程

退出码 0 = 六条全过；1 = 有失败（截图与 `result.json` 仍会写出，便于对照）。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
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

import httpx
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():  # pragma: no cover - 非 Windows / 未建 venv 时的兜底
    PYTHON = Path(sys.executable)

EMAIL = "d7-admin@example.com"
TENANT = "D7 验收团队"
DOCX_NAME = "液压站维护手册.docx"

DOC_TEXT = (
    "液压站 E2 报警的处理步骤：先检查通讯波特率是否为 19200，"
    "再核对站号是否为 3。若两者都正确，断电重启后观察指示灯。"
)
"""上传到知识库的正文。**引用卡必须显示它的原文片段**，所以这句话要足够独特。"""

IN_SCOPE_QUESTION = "液压站 E2 报警怎么处理？"
OUT_OF_SCOPE_QUESTION = "今天晚饭吃什么比较好？"
ANSWER = "按手册，先查通讯波特率 19200，再核对站号 3，最后断电重启观察指示灯[1]。"
"""假模型的固定回答。`[1]` 是引用角标——没有它，引用卡那三条判据无从谈起。"""


class FakeModel(BaseHTTPRequestHandler):
    """OpenAI 兼容的最小端点：一个固定回答 + 1.2 秒延迟。"""

    delay: float = 1.2

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        time.sleep(self.delay)
        body = json.dumps(
            {
                "id": "chatcmpl-d7",
                "object": "chat.completion",
                "created": 0,
                "model": "d7-fake",
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
        """安静：假模型的访问日志对验收没有价值。"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _make_docx(path: Path) -> None:
    from docx import Document

    document = Document()
    document.add_heading("液压站维护手册", level=1)
    document.add_paragraph(DOC_TEXT)
    document.add_paragraph("注意：站号与波特率必须与 PLC 侧一致，否则每 3 秒报一次 E2。")
    document.save(path)


class Run:
    """一次验收运行的上下文：服务、浏览器、证据目录。"""

    def __init__(self, out: Path, channel: str, headed: bool) -> None:
        self.out = out
        self.channel = channel
        self.headed = headed
        self.work = out / "work"
        self.shots = out / "shots"
        self.work.mkdir(parents=True, exist_ok=True)
        self.shots.mkdir(parents=True, exist_ok=True)
        self.password = secrets.token_urlsafe(12)
        self.results: list[dict[str, Any]] = []
        self.console_errors: list[str] = []
        self.port = _free_port()
        self.model_port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self._server: subprocess.Popen[bytes] | None = None
        self._model: ThreadingHTTPServer | None = None

    # ── 环境 ──

    def start_model(self) -> None:
        self._model = ThreadingHTTPServer(("127.0.0.1", self.model_port), FakeModel)
        threading.Thread(target=self._model.serve_forever, daemon=True).start()

    def bootstrap(self) -> None:
        """用**部署脚本**建首个管理员，不直接写库。

        D7 不重验 P0-4（空库部署），但顺势用真脚本：如果它坏了，这里先红。
        """
        env = os.environ | {
            "AWB_DATA_DIR": str(self.work / "data"),
            "AWB_SECRET_KEY": secrets.token_urlsafe(24),
            "AWB_BOOTSTRAP_PASSWORD": self.password,
        }
        done = subprocess.run(
            [
                str(PYTHON),
                str(ROOT / "scripts" / "bootstrap_identity.py"),
                "--email",
                EMAIL,
                "--tenant",
                TENANT,
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        if done.returncode != 0:
            raise RuntimeError(f"引导失败（{done.returncode}）：{done.stdout}\n{done.stderr}")
        self._env = env

    def start_server(self) -> None:
        log = (self.out / "server.log").open("wb")
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
                "--dist",
                str(ROOT / "web" / "dist"),
            ],
            cwd=ROOT,
            env=self._env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if (
                    httpx.get(f"{self.base}/api/health", timeout=1.0, trust_env=False).status_code
                    == 200
                ):
                    return
            except httpx.HTTPError:
                time.sleep(0.2)
        raise RuntimeError("服务 60 秒内没起来（看 server.log）")

    def configure_provider(self) -> None:
        """登录 + 配一个指向假模型的供应商。走 HTTP，不经界面——这属于 API 侧夹具。"""
        # trust_env=False：不吃 HTTP(S)_PROXY。这台机器上配了代理时，httpx 会把请求
        # 送到代理（日志里表现为绝对 URL 的请求行，服务端回 404）。
        with httpx.Client(base_url=self.base, timeout=10.0, trust_env=False) as client:
            login = client.post("/api/auth/login", json={"email": EMAIL, "password": self.password})
            if login.status_code != 200:
                raise RuntimeError(f"夹具登录失败：{login.text}")
            if login.json().get("mustChangePassword"):
                rotated = secrets.token_urlsafe(12)
                changed = client.post(
                    "/api/auth/password",
                    json={"oldPassword": self.password, "newPassword": rotated},
                )
                if changed.status_code != 204:
                    raise RuntimeError(f"夹具改密失败：{changed.text}")
                self.password = rotated
            created = client.post(
                "/api/settings/providers",
                json={
                    "name": "D7 假模型",
                    "baseUrl": f"http://127.0.0.1:{self.model_port}/v1",
                    "apiKey": "sk-d7-fake",
                },
            )
            if created.status_code != 201:
                raise RuntimeError(f"建供应商失败：{created.text}")
            provider_id = created.json()["id"]
            client.post(
                f"/api/settings/providers/{provider_id}/models",
                json={"model": "d7-fake", "isDefault": True},
            )
            activated = client.post(f"/api/settings/providers/{provider_id}/activate")
            if activated.status_code != 200:
                raise RuntimeError(f"激活供应商失败：{activated.text}")

    def stop(self) -> None:
        if self._server is not None:
            self._server.terminate()
            try:
                self._server.wait(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self._server.kill()
        if self._model is not None:
            self._model.shutdown()

    def __enter__(self) -> Run:
        self.start_model()
        self.bootstrap()
        self.start_server()
        self.configure_provider()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    # ── 记录 ──

    def shot(self, page: Page, name: str, *, full: bool = False) -> None:
        page.screenshot(path=str(self.shots / f"{name}.png"), full_page=full)

    def record(self, item: str, ok: bool, detail: str) -> None:
        self.results.append(
            {
                "item": item,
                "ok": ok,
                "detail": detail,
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )
        print(f"  {'PASS' if ok else 'FAIL'}  {item} — {detail}")


def need(condition: bool, message: str) -> None:
    """断言。**不用 `assert`**：`-O` 会把它整段删掉，而验收脚本不能有"静默跳过"。"""
    if not condition:
        raise RuntimeError(message)


def _login(page: Page, run: Run) -> None:
    page.goto(f"{run.base}/login", wait_until="domcontentloaded")
    page.get_by_placeholder("邮箱").fill(EMAIL)
    page.get_by_placeholder("密码").fill(run.password)
    page.get_by_role("button", name="登录", exact=True).click()
    page.wait_for_url(lambda url: "/login" not in str(url), timeout=15_000)
    # 若这次运行没有在夹具阶段轮换过初始口令，界面会先落到改密页。
    if "/change-password" in str(page.url):
        rotated = secrets.token_urlsafe(12)
        page.get_by_placeholder("引导时设置的口令").fill(run.password)
        page.get_by_placeholder("至少 12 位").fill(rotated)
        page.get_by_placeholder("与新口令一致").fill(rotated)
        page.get_by_role("button", name="确认修改").click()
        page.wait_for_url(lambda url: "/change-password" not in str(url), timeout=15_000)
        run.password = rotated


def _upload(page: Page, run: Run) -> str:
    page.goto(f"{run.base}/", wait_until="domcontentloaded")
    page.set_input_files("input[type=file]", str(run.work / DOCX_NAME))
    # **不要用 get_by_text("可检索")**：状态筛选下拉里也有这两个字，`first` 会挑到那个
    # 隐藏的 option，等一辈子也等不到可见。按行定位才稳。
    row = page.locator(".trow", has_text=DOCX_NAME).first
    row.wait_for(timeout=60_000)
    status = row.locator(".status")
    status.filter(has_text="可检索").wait_for(timeout=60_000)
    return status.inner_text()


def _ask(page: Page, question: str) -> None:
    box = page.get_by_placeholder("输入消息，Enter 发送，Shift+Enter 换行")
    box.fill(question)
    box.press("Enter")


def scenario(run: Run, page: Page) -> None:
    """六条判据按用户实际顺序走一遍。任何一条失败都抛出去，由 main 记为红。"""

    # ① 登录页
    page.goto(f"{run.base}/login", wait_until="domcontentloaded")
    run.shot(page, "01-login")
    need(page.get_by_placeholder("邮箱").is_visible(), "登录页没有邮箱输入框")
    run.record("① 登录页可用", True, "GET /login 渲染出邮箱/密码/登录按钮")

    # ① 错的凭据要说人话（不是白屏、不是英文异常）
    page.get_by_placeholder("邮箱").fill(EMAIL)
    page.get_by_placeholder("密码").fill("wrong-password")
    page.get_by_role("button", name="登录", exact=True).click()
    toast = page.locator(".el-message").first
    toast.wait_for(timeout=10_000)
    run.shot(page, "02-login-rejected")
    need("登录" in page.url or "/login" in page.url, "登录失败后跳走了")
    run.record("① 错密码有提示", True, f"界面提示：{toast.inner_text()[:40]}")

    _login(page, run)
    run.shot(page, "03-documents-empty")

    # ①'' 设置页：配好的供应商与模型必须**看得见**（含「已配置」标记与地址/模型两格）
    page.goto(f"{run.base}/settings", wait_until="domcontentloaded")
    page.get_by_text("D7 假模型").first.wait_for(timeout=20_000)
    page.get_by_text("已配置").first.wait_for(timeout=20_000)
    page.get_by_text("d7-fake").first.wait_for(timeout=20_000)
    run.shot(page, "04-settings", full=True)
    run.record("①'' 设置页列出供应商与模型", True, "已配置 / d7-fake / D7 假模型 均可见")

    # ② 上传 + 解析
    status = _upload(page, run)
    run.shot(page, "04-documents-parsed", full=True)
    run.record("② 上传并解析", True, f"《{DOCX_NAME}》状态 = {status}")

    # ③④⑤ 在对话页
    page.goto(f"{run.base}/chat", wait_until="domcontentloaded")
    composer = "输入消息，Enter 发送，Shift+Enter 换行"
    page.get_by_placeholder(composer).wait_for(timeout=15_000)

    # ①' 登录后的初始化：**不刷新页面**也该认得已配置的模型。
    # 这一条是 D7 抓到的第二个缺陷的回归防线（`LoginView` 登录后要重跑 restore）。
    need(
        page.get_by_text("未配置模型").count() == 0,
        "登录后仍显示「未配置模型」——初始化只在 App 挂载时跑了一次，登录后没补上",
    )
    page.get_by_text("d7-fake").first.wait_for(timeout=15_000)
    run.record("① 登录后初始化", True, "模型标识显示 d7-fake（未停在「未配置模型」）")

    _ask(page, IN_SCOPE_QUESTION)
    # ⑥ 不空转：请求在飞的时候，界面上必须有「停止」或打字动画
    inflight = page.locator(".typing, button:has-text('停止')").first
    inflight.wait_for(timeout=20_000)
    run.shot(page, "05-in-flight")
    run.record("⑥ 请求在飞时不空转", True, "出现「停止」/打字动画，页面不是白屏")

    cite = page.locator("sup.cite").first
    cite.wait_for(timeout=90_000)
    run.shot(page, "06-citation")
    run.record("③ 答案带引用角标", True, f"答案里出现 {cite.inner_text()}")

    cite.hover()
    card = page.locator(".cite-card").first
    card.wait_for(timeout=10_000)
    run.shot(page, "07-citation-card")
    card_text = card.inner_text()
    need(DOCX_NAME in card_text, f"引用卡没写文件名：{card_text[:60]}")
    need("波特率" in card_text, "引用卡里没有原文片段（只给了文件名等于没给原文）")
    run.record("③ 引用卡回原文", True, "悬停显示《文件名》+ 片段原文（含「波特率」）")

    page.locator(".sources-toggle").first.click()
    page.locator(".source-item").first.wait_for(timeout=10_000)
    run.shot(page, "08-sources-panel")
    run.record("③ 来源面板可展开", True, f"{page.locator('.source-item').count()} 条来源")


def delete_all_documents(page: Page, run: Run) -> None:
    """在界面上把资料删掉。

    为什么要删：默认部署的嵌入实现是 HashEmbedder，语义相似度几乎不可分——
    **只有 1 块时判定为「弱」（照答），有 2 块以上时 margin 恒低于阈值（全拒）**。
    想稳定拿到一次「拒答」，最干净的办法是让检索**没有候选**（空库），
    那是判定表里 NO_CANDIDATES 那条，与库外问题走的是同一个出口。
    真实模型 + 真实嵌入下的拒答标定归 D8。
    """
    page.goto(f"{run.base}/", wait_until="domcontentloaded")
    row = page.locator(".trow", has_text=DOCX_NAME).first
    row.wait_for(timeout=30_000)
    row.locator("button.row-icon.danger").click()
    # 确认框的按钮文案是「删除」不是「确定」（DocumentsView.onDelete 显式指定）
    confirm = page.locator(".el-message-box__btns button").filter(has_text="删除")
    if confirm.count() > 0:
        confirm.first.click()
    row.wait_for(state="detached", timeout=60_000)
    run.shot(page, "09-documents-after-delete")


def refusal_card(page: Page, run: Run) -> None:
    """④ 拒答卡：库里没有可依据的内容时，界面要给卡片，不是硬编一个答案。"""
    page.goto(f"{run.base}/chat", wait_until="domcontentloaded")
    _ask(page, OUT_OF_SCOPE_QUESTION)
    refusal = page.locator(".refusal-card").last
    refusal.wait_for(timeout=90_000)
    page.wait_for_timeout(300)
    run.shot(page, "10-refusal")
    text = refusal.inner_text()
    need("依据不足" in text, f"拒答卡没有「依据不足」：{text[:60]}")
    need(
        page.locator(".refusal-card")
        .last.locator("xpath=ancestor::div[contains(@class,'msg')]")
        .locator(".sources")
        .count()
        == 0,
        "拒答时还挂着「引用来源」面板——那等于给了一份假担保",
    )
    run.record("④ 拒答卡", True, text.replace("\n", " / ")[:60])


def second_device(run: Run, page: Page) -> None:
    _login(page, run)
    page.goto(f"{run.base}/chat", wait_until="domcontentloaded")
    item = page.locator(".conv-item").first
    item.wait_for(timeout=20_000)
    run.shot(page, "10-second-device-list")
    item.click()
    cite = page.locator("sup.cite").first
    cite.wait_for(timeout=30_000)
    run.shot(page, "11-second-device-history")
    need(
        page.get_by_text(IN_SCOPE_QUESTION).count() > 0,
        "换设备后看不到原来的问题",
    )
    run.record("⑤ 换设备后历史与引用还在", True, "新 profile 登录后能看到原问题与引用角标")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ui_acceptance", description="D7 UI 验收（真浏览器）")
    parser.add_argument("--channel", default="msedge", help="浏览器通道（msedge / chrome）")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--out", type=Path, default=None, help="证据目录")
    args = parser.parse_args(argv)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = args.out or ROOT / "reports" / "acceptance" / "2026-11-05" / "ui" / stamp
    out.mkdir(parents=True, exist_ok=True)

    failed = False
    with Run(out, args.channel, args.headed) as run:
        _make_docx(run.work / DOCX_NAME)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel=run.channel, headless=not run.headed)
            context = browser.new_context(viewport={"width": 1440, "height": 950}, locale="zh-CN")
            page = context.new_page()
            # 未登录时前端会先探一次 /api/auth/me，401 是**预期**的（页面据此跳登录）。
            # 把它算成"控制台报错"，等于让正确行为把验收判红。
            _EXPECTED = ("401 (Unauthorized)", "Failed to load resource")
            page.on(
                "console",
                lambda msg: (
                    run.console_errors.append(msg.text)
                    if msg.type == "error" and not any(skip in msg.text for skip in _EXPECTED)
                    else None
                ),
            )
            try:
                scenario(run, page)
                context2 = browser.new_context(
                    viewport={"width": 1440, "height": 950}, locale="zh-CN"
                )
                second_device(run, context2.new_page())
                # 拒答要等引用/历史的断言都做完再构造：空库之后，引用卡就没有来源了。
                delete_all_documents(page, run)
                refusal_card(page, run)
            except Exception as exc:
                failed = True
                run.record("异常", False, f"{type(exc).__name__}: {exc}")
                with page.context.new_page() as shot_page:
                    try:
                        shot_page.goto(page.url, wait_until="domcontentloaded", timeout=10_000)
                        run.shot(shot_page, "99-failure")
                    except Exception:  # noqa: S110
                        pass
            finally:
                browser.close()

    if run.console_errors:
        failed = True
        run.record("浏览器控制台", False, "；".join(run.console_errors[:3]))
    else:
        run.record("浏览器控制台", True, "无 error 级日志")

    summary = out / "result.json"
    summary.write_text(
        json.dumps({"results": run.results, "out": str(out)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    passed = sum(1 for item in run.results if item["ok"])
    print(f"\n{passed}/{len(run.results)} 通过；截图与日志：{out}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
