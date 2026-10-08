"""tests/web 的无头验证夹具（W2 自用，不依赖后端）。

做法：
  - **自建静态服务**：把 `adaptive-reader/` 当根目录起一个 `http.server`，于是
    `/web/index.html` 与 `/samples/<id>/*.json` 同源可达，正好复现「夹具模式读同源静态文件」
    的条件，不需要 W1 的 `/api/*` 就能跑完整交互。
  - **本机浏览器**：先试 `channel="msedge"`（本机已装 Edge 154），失败再退到已缓存内核
    `chromium-1084`（playwright 1.39.0 自带）。**任何情况下都不触发内核下载。**
  - **就绪靠页面里的显式标志** `globalThis.__arDebug.ready`，不 sleep。
  - 注入开关（`INJECT`）用来造「请求失败 / 响应降级 / 改写稿缺段 / 没有概念清单」这些负向现场，
    全部在服务端做，页面代码里没有测试后门。
"""

from __future__ import annotations

import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

import pytest

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent          # adaptive-reader/
FIXTURE_ID = "capital-v1-ch01"
FIXTURE_DIR = PROJECT_ROOT / "samples" / FIXTURE_ID

# 每次测试前重置；测试里改它来造负向现场
INJECT = SimpleNamespace(
    fail_doc=False,
    fail_explain=False,
    doc_payload=None,
    explain_payload=None,
    hits=[],
)


def _reset_inject_state() -> None:
    INJECT.fail_doc = False
    INJECT.fail_explain = False
    INJECT.doc_payload = None
    INJECT.explain_payload = None
    INJECT.hits = []


class _Handler(SimpleHTTPRequestHandler):
    """静默的静态文件服务 + 两个注入开关。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_ROOT), **kwargs)

    def log_message(self, *args):  # 别把 HTTP 日志刷进 pytest 输出
        pass

    def _send_json(self, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - http.server 的接口名
        path = unquote(urlparse(self.path).path)
        INJECT.hits.append(path)
        if path.endswith("/explain.json") and path.startswith("/samples/"):
            if INJECT.fail_explain:
                self.send_error(500, "injected explain failure")
                return
            if INJECT.explain_payload is not None:
                self._send_json(INJECT.explain_payload)
                return
        if path.endswith("/doc.json") and path.startswith("/samples/"):
            if INJECT.fail_doc:
                self.send_error(500, "injected doc failure")
                return
            if INJECT.doc_payload is not None:
                self._send_json(INJECT.doc_payload)
                return
        super().do_GET()


@pytest.fixture(autouse=True)
def _reset_inject():
    """每个测试前把注入开关清干净（负向现场不能串场）。"""
    _reset_inject_state()
    yield
    _reset_inject_state()


@pytest.fixture
def ar_inject():
    """服务端注入开关：fail_doc / fail_explain / doc_payload / explain_payload。"""
    return INJECT


@pytest.fixture(scope="session")
def ar_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(scope="session")
def ar_launch_info():
    return {}


@pytest.fixture(scope="session")
def ar_browser(ar_launch_info):
    """本机浏览器。先 Edge，再已缓存内核；两条都不通就明确失败（绝不下载）。"""
    playwright = pytest.importorskip("playwright.sync_api", reason="没装 playwright")
    attempts = []
    with playwright.sync_playwright() as pw:
        browser = None
        for label, kwargs in (("msedge(channel)", {"channel": "msedge"}), ("chromium-1084(bundled)", {})):
            try:
                browser = pw.chromium.launch(headless=True, **kwargs)
                ar_launch_info["label"] = label
                ar_launch_info["version"] = browser.version
                break
            except Exception as exc:  # noqa: BLE001 - 要把失败原因原样报出来
                attempts.append(f"{label} → {type(exc).__name__}: {exc}")
        if browser is None:
            pytest.fail(
                "没有可用的本机浏览器内核（本测试禁止下载内核）：\n" + "\n".join(attempts),
            )
        yield browser
        browser.close()


@pytest.fixture
def ar_errors():
    """页面内的 JS 报错收集器（断言「没有静默错误」）。"""
    return []


# 浏览器把「资源加载失败」也记成 console.error，但那是被我们故意打断的请求（负向现场），
# 不是没处理的 JS 异常。这两类噪音排除掉，剩下的才算页面自己的错。
PAGE_ERROR_NOISE = ("favicon", "Failed to load resource")


@pytest.fixture(autouse=True)
def _assert_no_page_errors(ar_errors):
    """全目录统一纪律：跑完每个用例，页面不许留下没处理的 JS 报错。"""
    yield
    unexpected = [e for e in ar_errors if not any(n in e for n in PAGE_ERROR_NOISE)]
    assert unexpected == [], f"页面有 JS 报错：{unexpected}"


@pytest.fixture
def page(ar_browser, ar_errors):
    """覆盖 pytest-playwright 自带的 `page`：
    用它的话浏览器由 --browser 决定（默认 bundled chromium），本机要求的 Edge 就用不上了。
    tests/web/conftest.py 比插件更靠近测试，同名 fixture 以这里为准。"""
    context = ar_browser.new_context(viewport={"width": 1400, "height": 900}, locale="zh-CN")
    page = context.new_page()
    page.on("pageerror", lambda exc: ar_errors.append(f"pageerror: {exc}"))

    def _on_console(msg):
        if msg.type != "error":
            return
        location = msg.location or {}
        url = location.get("url", "") if isinstance(location, dict) else ""
        ar_errors.append(f"console.error: {msg.text} @ {url}")

    page.on("console", _on_console)
    yield page
    context.close()
