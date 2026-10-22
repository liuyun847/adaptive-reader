"""tests/web 的假后端（`/api/*` 在浏览器侧用 `page.route` 拦下）：配图 / 「太啰嗦」压缩 / 行为埋点
这三批新用例共用的一套桩。

🔁 2026-10-15 拆页后：本文件的 `open_fixture` / `open_api` 指向 **`web/reader.html`**（阅读器页），
不再是 `web/index.html`（那个路径现在是**启动页**，见 `test_launcher.py`）。

做法与 `test_survey_ui.py::fake_backend` 一致（逐条分流 + 记请求清单），只覆盖新用例要的那几个端点：

  GET  /api/doc/<id>   载入夹具 doc（`doc` 可换成注入版）；响应体与 `/api/doc` 同形
  GET  /api/profile    画像指纹（会话缓存键的第五段）
  GET  /api/survey     已经「跳过过」——免得问卷弹出来挡事（问卷不是这批用例的对象）
  POST /api/explain    固定的一段讲解（只给「埋点不额外打模型」这类断言用）
  POST /api/compress   `PLAN-actions-feedback.md §5.2` 的两种形状：成功 / 降级（一律 200）
  POST /api/events     `§7.2` 的批量上报：`{ok, written, duplicates, total, degraded[]}`

**零真实模型调用**：所有响应都是本文件里的固定 JSON，一个模型请求都不发。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent            # adaptive-reader/
FIXTURE_ID = "capital-v1-ch01"
FIXTURE_DIR = PROJECT / "samples" / FIXTURE_ID

# 假后端回的压缩稿：比原文短、带一点 Markdown（确认刷新正文走的是 md.js 的渲染）
COMPRESSED_TEXT = "**压缩稿**：这一段只留骨架——共同东西不是商品的天然属性，交换时使用价值已被抽去。"

# 假 mermaid：形状照 mermaid 12 的 `render()`（返回 Promise<{svg}>），不下载 5.3 MiB 的真库
FAKE_MERMAID_JS = (
    "window.mermaid = {"
    "  initialize: function () {}," 
    "  render: function (id, code) {"
    "    return Promise.resolve({ svg: '<svg id=\"' + id + '\" xmlns=\"http://www.w3.org/2000/svg\"'"
    "      + ' width=\"20\" height=\"10\"><rect width=\"20\" height=\"10\"/></svg>' });"
    "  }"
    "};"
)

# 会抛错的假 mermaid：用来造「render() 抛错 → 退回文字」那条现场（真库遇到畸形源码也是抛）
FAKE_MERMAID_THROWS_JS = (
    "window.mermaid = {"
    "  initialize: function () {}," 
    "  render: function () { throw new Error('Parse error on line 3'); }"
    "};"
)

# 渲染成功但返回空串（真库偶尔这样）：也必须退回文字，不许静默空白
FAKE_MERMAID_EMPTY_JS = (
    "window.mermaid = {"
    "  initialize: function () {}," 
    "  render: function () { return Promise.resolve({ svg: '   ' }); }"
    "};"
)


def fixture_doc() -> dict:
    return json.loads((FIXTURE_DIR / "doc.json").read_text(encoding="utf-8"))


def wait_ready(page, timeout: int = 15000) -> dict:
    """等页面里的显式就绪标志（沿用 test_reader_ui.py 的口径：不 sleep）。"""
    page.wait_for_function(
        "() => globalThis.__arDebug && globalThis.__arDebug.ready === true",
        timeout=timeout,
    )
    debug = page.evaluate("() => globalThis.__arDebug")
    assert debug["jsLoaded"] is True, "模块没加载起来（看 __arDebug.errors）"
    return debug


def dbg(page) -> dict:
    return page.evaluate("() => globalThis.__arDebug")


def open_fixture(page, ar_server, extra: str = "") -> str:
    """夹具模式打开**阅读器页**（2026-10-15 拆页后是 `web/reader.html`，不再叫 index.html）。"""
    url = f"{ar_server}/web/reader.html?fixture={FIXTURE_ID}{extra}"
    page.goto(url)
    wait_ready(page)
    return url


def open_api(page, ar_server, extra: str = "") -> str:
    """正式模式：走 `page.route` 挂的假后端（调用方先挂好 `FakeApi`）。"""
    url = f"{ar_server}/web/reader.html?source={FIXTURE_ID}{extra}"
    page.goto(url)
    wait_ready(page)
    return url


def banner_texts(page) -> list:
    banners = page.locator('[data-role="banner"]')
    return [banners.nth(i).text_content() for i in range(banners.count())]


def has_banner(page, needle: str) -> bool:
    return any(needle in text for text in banner_texts(page))


class FakeApi:
    """一层路由级的假后端；`requests` 记下前端到底发了什么（断言用）。"""

    def __init__(self, page, doc=None, *, compress=None, compress_status: int = 200,
                 events=None, events_status: int = 200, profile=None, explain=None,
                 profile_status: int = 200, rating_status: int = 200,
                 preference_status: int = 200):
        self.page = page
        self.doc = doc if doc is not None else fixture_doc()
        self.compress_payload = compress          # None = 成功响应（回显段号 + COMPRESSED_TEXT）
        self.compress_status = compress_status
        self.events_payload = events              # None = {"ok": True, ...}
        self.events_status = events_status
        self.explain_payload = explain            # None = 一段固定的假讲解
        # 画像：GET /api/profile 回它，写端点会就地改它再回整份（模拟后端写回后的画像）
        self.profile = copy.deepcopy(profile) if profile is not None else None
        self.profile_status = profile_status
        self.rating_status = rating_status
        self.preference_status = preference_status
        self.requests: list = []                  # {url, method, path, body, json}
        self.compress_calls: list = []
        self.event_batches: list = []
        self.rating_calls: list = []              # POST /api/profile/rating 的请求体
        self.preference_calls: list = []          # POST /api/profile/preference 的请求体
        page.route("**/api/**", self._handle)

    # ---------------------------------------------------------------- 内部分流
    def _handle(self, route, request):
        url = request.url
        method = request.method
        path = urlparse(url).path
        raw = request.post_data if method == "POST" else None
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        self.requests.append({"url": url, "method": method, "path": path, "body": raw, "json": parsed})

        if path.startswith("/api/doc/"):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(self.doc, ensure_ascii=False))
            return
        if path == "/api/profile":
            if self.profile_status != 200:
                route.fulfill(status=self.profile_status, content_type="application/json",
                              body=json.dumps({"error": "画像读不出来（假后端注入）"}, ensure_ascii=False))
                return
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(self.profile
                                          or {"profile": {"reader": "local-default"}, "degraded": []},
                                          ensure_ascii=False))
            return
        if path == "/api/profile/rating":
            self.rating_calls.append(parsed or {})
            if self.rating_status != 200:
                route.fulfill(status=self.rating_status, content_type="application/json",
                              body=json.dumps({"error": "画像写不进去（假后端注入）"}, ensure_ascii=False))
                return
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(self._apply_rating(parsed or {}), ensure_ascii=False))
            return
        if path == "/api/profile/preference":
            self.preference_calls.append(parsed or {})
            if self.preference_status != 200:
                route.fulfill(status=self.preference_status, content_type="application/json",
                              body=json.dumps({"error": "偏好写不进去（假后端注入）"}, ensure_ascii=False))
                return
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(self._apply_preference(parsed or {}), ensure_ascii=False))
            return
        if path == "/api/survey":
            # 读者此前跳过过：问卷不再弹（这批用例的对象不是问卷）
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"version": 1, "answered_version": None, "questions": [],
                                           "answers": None, "skipped": True, "degraded": []},
                                          ensure_ascii=False))
            return
        if path == "/api/explain":
            selection = (parsed or {}).get("selection")
            payload = self.explain_payload or {
                "text": "假讲解：这一段在讲使用价值与交换价值的区分。",
                "degraded": False,
                "scope": "selection" if selection else "paragraph",
                "model": "fake-model",
                "prompt_version": "explain-v2",
            }
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(payload, ensure_ascii=False))
            return
        if path == "/api/compress":
            self.compress_calls.append(parsed or {})
            if self.compress_status != 200:
                route.fulfill(status=self.compress_status, content_type="application/json", body="{}")
                return
            payload = self.compress_payload
            if payload is None:
                payload = {
                    "ok": True,
                    "paragraph": (parsed or {}).get("paragraph"),
                    "action": "compress",
                    "text": COMPRESSED_TEXT,
                    "scope": "paragraph",
                    "cached": False,
                    "usage": {"requests": 1, "prompt_tokens": 120, "completion_tokens": 40},
                    "attempts": [],
                    "model": "fake-model",
                    "prompt_version": "compress-v3",
                    "degraded": [],
                    "artifacts_updated": True,
                    "hint": "已压缩，请重跑 ar check。",
                    # 事实锚申报的可见面（§14.1，compress-v3）：默认现场**没有申报**（这一段没丢锚点）
                    "fact_declarations": {},
                    "waived": [],
                    "over_declared": [],
                    "fact_anchor_policy": "keep_all",
                }
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(payload, ensure_ascii=False))
            return
        if path == "/api/events":
            batch = (parsed or {}).get("events") if isinstance(parsed, dict) else None
            batch = batch if isinstance(batch, list) else [parsed]
            self.event_batches.append(batch)
            if self.events_status != 200:
                route.fulfill(status=self.events_status, content_type="application/json", body="{}")
                return
            payload = self.events_payload
            if payload is None:
                payload = {"ok": True, "written": len(batch), "duplicates": 0,
                           "total": len(batch), "degraded": []}
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(payload, ensure_ascii=False))
            return
        route.fulfill(status=404, content_type="application/json", body="{}")

    # ------------------------------------------------------- 画像写回（模拟后端）
    def _profile_payload(self) -> dict:
        if self.profile is None:
            self.profile = {"profile": {"reader": "local-default", "concepts": {},
                                        "preferences": [], "overrides": []}, "degraded": []}
        return self.profile

    def _apply_rating(self, body: dict) -> dict:
        payload = self._profile_payload()
        profile = payload.setdefault("profile", {})
        concepts = profile.setdefault("concepts", {})
        cid = body.get("concept_id")
        state = body.get("state")
        concepts[cid] = {**(concepts.get(cid) or {}), "state": state}
        profile.setdefault("overrides", []).append({"concept": cid, "state": state})
        return {"ok": True, "profile": profile, "degraded": []}

    def _apply_preference(self, body: dict) -> dict:
        payload = self._profile_payload()
        profile = payload.setdefault("profile", {})
        preferences = profile.setdefault("preferences", [])
        pid = body.get("id")
        value = body.get("value")
        entry = next((item for item in preferences
                      if isinstance(item, dict) and item.get("id") == pid), None)
        if entry is None:
            entry = {"id": pid, "confidence": 0.7, "evidence": [], "value": value}
            preferences.append(entry)
        entry["value"] = value
        entry["statement"] = {
            "keep_all": "事实锚全保留（要素材）：数字 / 引号引文 / 书名号一个都不许少",
            "allow_declared_drops": "要结论：允许在申报后丢掉纯举例性的数据、把引文改成转述",
        }.get(value, "")
        return {"ok": True, "profile": profile, "degraded": []}

    # ---------------------------------------------------------------- 断言助手
    def calls(self, path: str) -> list:
        return [item for item in self.requests if item["path"] == path]

    def model_calls(self) -> list:
        """打模型的那些端点（这批用例里必须一个都没有 / 只有显式点出来的那几次）。"""
        return [item for item in self.requests
                if item["path"] in ("/api/explain", "/api/compress")]
