"""启动页（`GET /` → `web/index.html`）四块的无头验证（playwright 驱动本机 Edge）。

🔁 2026-10-15 拆页：阅读器页从 `web/index.html` 改名为 `web/reader.html`，`web/index.html` 变成**启动页**。
本文件是新页面的用例（阅读器那几套见 `test_reader_ui.py` / `test_reader_features.py` 等）。

覆盖**启动页四块**（顺序即页面顺序，见 `data-block`）：

1. ① 状态：只验「四块都在 + 入口在①块里」（面板自己的行为在 `test_status_panel.py` 里逐条测）；
2. ② 做测试（读前背景问卷）：卡片三态（未答 / 已答 / 已跳过）+ 读不到时的可见降级；
   **默认绝不自动弹**；点「填写问卷」就地打开**同一张**问卷，答完 / 关闭后卡片状态刷新；
3. ③ 历史记录：有材料 / 空列表 / 接口 404 / 结构不符四种现场；每条显示 source_id、段数、
   最后动过的时间（本地可读格式）、动作统计、校验状态、缺哪份产物；点一条进 `/reader?source=<id>`；
4. ④ 输入文件：上传成功 / 400 / 409 三条路径的文案与按钮状态；前端粗判挡住明显的错（不发请求）。

假后端沿用 `tests/web/ar_backend.py` 那套写法（`page.route` 路由级拦截 + 记请求清单 + 固定 JSON），
**零真实模型调用**；就绪只等页面里的显式状态（`__arDebug.ready`），不 sleep。
每个用例的 autouse「无 JS 报错」断言（`conftest.py`）必须通过——启动页也算在内。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from ar_backend import banner_texts, dbg, has_banner, wait_ready  # noqa: E402  —— 复用既有助手

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
FIXTURE_ID = "capital-v1-ch01"
QUESTIONS_PATH = PROJECT / "samples" / FIXTURE_ID / "survey-questions.json"

# 启动页打的那四个端点（都用正则：glob 的语义容易与别的 `*.json` 撞车）
SOURCES_ROUTE = re.compile(r"/api/sources$")
SURVEY_ROUTE = re.compile(r"/api/survey$")
INGEST_ROUTE = re.compile(r"/api/ingest$")

# 一份与后端真实响应对齐的清单条目（数字取自 `GET /api/sources` 的契约示例）
MATERIAL_ENTRY = {
    "source_id": "capital-v1-ch01",
    "paragraphs": 25,
    "ingested_at": "2026-10-05T20:11:03+08:00",
    "mtime": "2026-10-13T12:27:42+08:00",
    "concepts": {"present": True, "count": 10, "generated_at": "2026-10-08T16:34:00+08:00"},
    "rewrite": {
        "present": True, "generated_at": "2026-10-13T12:27:42+08:00",
        # 真服务现在回的四个键都在（compress / illustrate 是 0）：页面只列非 0 的，别把关键数字淹掉
        "action_counts": {"expand": 10, "compress": 0, "keep": 15, "illustrate": 0},
        "degraded_count": 1, "paragraph_count": 25,
    },
    "fidelity": {
        "present": True, "checked": True, "ok": True, "mismatch_count": 0,
        "counts": {"paragraphs": 25, "compared": 25, "numbers": 34, "quotes": 11, "books": 13},
    },
}

# 「只摄取、还没改写」那一档：三块产物都是 null（缺产物时整块是 null，不是缺键）
INGEST_ONLY_ENTRY = {
    "source_id": "draft-notes",
    "paragraphs": 4,
    "ingested_at": "2026-10-14T09:05:00+08:00",
    "mtime": "2026-10-14T09:05:11+08:00",
    "concepts": None, "rewrite": None, "fidelity": None,
}

# 校验没通过那一档：checked 为真 + 有 N 处不一致
FAILED_CHECK_ENTRY = {
    "source_id": "half-done",
    "paragraphs": 7,
    "ingested_at": "2026-10-14T10:00:00+08:00",
    "mtime": "2026-10-14T10:02:00+08:00",
    "concepts": {"present": True, "count": 3, "generated_at": None},
    "rewrite": {"present": True, "generated_at": None, "action_counts": {"compress": 2}, "degraded_count": 0,
                "paragraph_count": 7},
    "fidelity": {"present": True, "checked": True, "ok": False, "mismatch_count": 2, "counts": None},
}


# 四个动作全是 0（空材料）：明写「各动作都是 0」，不许写一片空白；校验也还没跑过
ZERO_ACTIONS_ENTRY = {
    "source_id": "no-actions",
    "paragraphs": 2,
    "ingested_at": "2026-10-14T11:00:00+08:00",
    "mtime": "2026-10-14T11:01:00+08:00",
    "concepts": {"present": True, "count": 0, "generated_at": None},
    "rewrite": {"present": True, "generated_at": None, "action_counts": {"expand": 0, "keep": 0},
                "degraded_count": 0, "paragraph_count": 2},
    "fidelity": {"present": True, "checked": False, "ok": False, "mismatch_count": None, "counts": None},
}


def questions() -> list:
    """夹具题目（= 后端 `ar.survey.QUESTIONS` 的导出，与阅读器用的是同一份）。"""
    return json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))["questions"]


def survey_payload(*, answered=None, skipped=False, version=1, answered_version=None,
                   answered_at=None, degraded=None, with_questions=True) -> dict:
    body = {
        "version": version,
        "answered_version": answered_version,
        "questions": questions() if with_questions else [],
        "answers": answered,
        "skipped": skipped,
        "degraded": degraded if degraded is not None else [],
    }
    if answered_at is not None:
        body["answered_at"] = answered_at
    return body


def sources_payload(entries, degraded=None) -> dict:
    return {"sources": list(entries), "degraded": list(degraded or [])}


# ------------------------------------------------------------------ 假后端

class LauncherApi:
    """启动页那四个端点的路由级假后端；`requests` 记下前端到底发了什么（断言用）。

    - `GET /api/sources`：`sources_payload`（可整体换成结构不符的形状）；
    - `GET /api/survey`：`survey_payload`；`POST /api/survey` 回成功并把作答写回上面那份
      （于是「答完 → 卡片变已答」这条链路在无头环境里也是真的）；
    - `POST /api/ingest`：`ingest_status` + `ingest_payload`；成功时把新条目并进清单
      （模拟「摄取完材料就出现在历史列表里」）。`hold_ingest=True` 时**先不回应**，
      把 route 留在 `held_routes` 里，由用例在断言完「上传中按钮禁用」之后自己放行。
    """

    def __init__(self, page, *, sources=None, sources_status=200, sources_body=None,
                 survey=None, survey_status=200, ingest_payload=None, ingest_status=200,
                 hold_ingest=False):
        self.page = page
        self.sources = sources if sources is not None else sources_payload([])
        self.sources_status = sources_status
        self.sources_body = sources_body            # 非 None 时原样回它（用来造结构不符 / 坏 JSON）
        self.survey = survey if survey is not None else survey_payload()
        self.survey_status = survey_status
        self.ingest_payload = ingest_payload
        self.ingest_status = ingest_status
        self.hold_ingest = hold_ingest
        self.requests: list = []
        self.held_routes: list = []                 # 被 hold 住的 ingest route（用例自己放行）
        page.route(SOURCES_ROUTE, self._handle)
        page.route(SURVEY_ROUTE, self._handle)
        page.route(INGEST_ROUTE, self._handle)

    # ---------------------------------------------------------------- 内部分流
    def _json(self, route, payload, status=200):
        route.fulfill(status=status, content_type="application/json; charset=utf-8",
                      body=json.dumps(payload, ensure_ascii=False))

    def _handle(self, route, request):
        path = re.sub(r"^.*?(/api/)", r"\1", request.url.split("?")[0])
        raw = request.post_data if request.method == "POST" else None
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        self.requests.append({"url": request.url, "method": request.method, "path": path,
                              "body": raw, "json": parsed})

        if path == "/api/sources":
            if self.sources_status != 200:
                # 真实的 404 不会带 JSON `error`（那是别的端点/别的服务的形状）：
                # 用纯文本回，好让「HTTP 404」这条状态码成为页面上唯一能说的原因
                route.fulfill(status=self.sources_status, content_type="text/plain; charset=utf-8",
                              body="Not Found")
                return
            if self.sources_body is not None:
                route.fulfill(status=200, content_type="application/json; charset=utf-8",
                              body=self.sources_body)
                return
            self._json(route, self.sources)
            return

        if path == "/api/survey" and request.method == "GET":
            if self.survey_status != 200:
                self._json(route, {"error": "问卷状态读不出来（注入的失败）"}, self.survey_status)
                return
            self._json(route, self.survey)
            return

        if path == "/api/survey" and request.method == "POST":
            body = parsed or {}
            if body.get("skipped"):
                self.survey["skipped"] = True
            if isinstance(body.get("answers"), dict) and body["answers"]:
                self.survey["answers"] = dict(body["answers"])
            self._json(route, {"ok": True, "background": {"version": 1},
                               "sketch": "读者背景：…", "degraded": []})
            return

        if path == "/api/ingest" and request.method == "POST":
            if self.hold_ingest:
                self.held_routes.append(route)      # 先不回：用例要在这里断言「上传中」
                return
            self._respond_ingest(route, parsed)
            return

        self._json(route, {"error": f"假后端没有这个端点：{path}"}, 404)

    def _respond_ingest(self, route, parsed):
        if self.ingest_status != 200:
            payload = self.ingest_payload or {"error": f"注入的失败（HTTP {self.ingest_status}）"}
            self._json(route, payload, self.ingest_status)
            return
        payload = self.ingest_payload or {
            "source_id": "upload-demo",
            "paragraphs": 3,
            "ingested_at": "2026-10-15T21:00:00+08:00",
            "anchors_stable": True,
            "overwritten": False,
            "degraded": [],
        }
        if payload.get("source_id") and not payload.get("_no_history"):
            entries = self.sources.setdefault("sources", [])
            if not any(item.get("source_id") == payload["source_id"] for item in entries):
                entries.insert(0, {
                    "source_id": payload["source_id"],
                    "paragraphs": payload.get("paragraphs"),
                    "ingested_at": payload.get("ingested_at"),
                    "mtime": payload.get("ingested_at"),
                    "concepts": None, "rewrite": None, "fidelity": None,
                })
        self._json(route, payload)

    # ---------------------------------------------------------------- 用例助手
    def release_ingest(self):
        """放行被 hold 住的那次摄取（模拟后端迟迟不回之后终于回了）。"""
        held, self.held_routes = self.held_routes, []
        for route in held:
            self._respond_ingest(route, None)

    def calls(self, path: str) -> list:
        return [item for item in self.requests if item["path"] == path]

    def posts(self) -> list:
        return [item for item in self.requests if item["method"] == "POST"]


# ------------------------------------------------------------------ 开页与工具

def wait_launcher_ready(page, timeout: int = 15000) -> dict:
    """等启动页的显式就绪标志（`ready` + `jsLoaded`），不 sleep。"""
    page.wait_for_function(
        "() => globalThis.__arDebug && globalThis.__arDebug.ready === true",
        timeout=timeout,
    )
    debug = dbg(page)
    assert debug["jsLoaded"] is True, "启动页模块没加载起来（看 __arDebug.errors）"
    return debug


def open_launcher(page, ar_server) -> str:
    """打开启动页：静态服务把项目根当站点，所以是 `/web/index.html`（真服务上是 `/`）。"""
    url = f"{ar_server}/web/index.html"
    page.goto(url)
    wait_launcher_ready(page)
    return url


def launcher_debug(page) -> dict:
    return dbg(page)["launcher"]


def block(page, name: str):
    return page.locator(f'[data-role="launcher-block"][data-block="{name}"]')


def history_rows(page):
    return page.locator('[data-role="history-item"]')


def row_text(page, index: int = 0) -> str:
    return history_rows(page).nth(index).inner_text()


def set_file(page, name: str, content: str, mime: str = "text/markdown") -> None:
    page.set_input_files('[data-role="upload-file"]',
                         {"name": name, "mimeType": mime, "buffer": content.encode("utf-8")})


def set_file_bytes(page, name: str, data: bytes, mime: str = "text/markdown") -> None:
    """按**原始字节**选文件（造非 UTF-8 / 二进制的现场用，`set_file` 只能给 UTF-8 文本）。"""
    page.set_input_files('[data-role="upload-file"]',
                         {"name": name, "mimeType": mime, "buffer": data})


def upload_debug(page) -> dict:
    return launcher_debug(page)["upload"]


def click_submit(page, times: int = 1) -> None:
    """按 / 连点「摄取」。`times > 1` 时在**同一次事件循环**里派发多次点击（真人连点的量级），
    这样「读文件也是异步的」那条路才会被真正压到——分次 `page.click` 中间留着往返空隙，压不出来。"""
    if times == 1:
        page.click('[data-role="upload-submit"]')
        return
    page.evaluate(
        """(times) => {
             const btn = document.querySelector('[data-role="upload-submit"]');
             for (let i = 0; i < times; i += 1) btn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
           }""",
        times,
    )


# ---------------------------------------------------------------- 显示设置（B1）

SETTINGS_KEY = "ar.settings.v1"

# 背景 × 文字 6 种组合（与阅读器页设置面板同口径：背景 3 档 × 文字 2 档）
SETTINGS_COMBOS = [
    ("paper", "ink"), ("paper", "light"),
    ("warm", "ink"), ("warm", "light"),
    ("dark", "ink"), ("dark", "light"),
]

# 三档背景的实测底色（对答案用；断言读的是**算好的样式**，不是把 CSS 抄一遍）
BG_RGB = {"paper": "rgb(251, 250, 247)", "warm": "rgb(246, 236, 217)", "dark": "rgb(27, 31, 35)"}

# 正文类文字的对比度门槛（WCAG 2.1 AA，与 js/settings.js 的 MIN_CONTRAST 同一档）
MIN_CONTRAST = 4.5


def _channel_to_linear(value: float) -> float:
    v = min(255.0, max(0.0, value)) / 255.0
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def _luminance(color: str) -> float:
    parts = [float(x) for x in re.findall(r"[\d.]+", color)[:3]]
    assert len(parts) == 3, f"读不出颜色：{color}"
    r, g, b = (_channel_to_linear(p) for p in parts)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(color_a: str, color_b: str) -> float:
    """WCAG 2.1 相对比度（与 js/settings.js 的算法同口径的 Python 版）。"""
    a, b = _luminance(color_a), _luminance(color_b)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def store_settings(page, payload: dict) -> None:
    """把显示设置存档写进 localStorage（同源页面已经打开过才写得进去）。"""
    page.evaluate("([key, raw]) => localStorage.setItem(key, raw)",
                  [SETTINGS_KEY, json.dumps(payload, ensure_ascii=False)])


def seed_settings(page, ar_server, payload: dict) -> None:
    """先在同源页面上落好存档，再打开启动页——模拟「读者上次手动选过」的复访。

    存档必须在启动页那段「渲染前应用显示设置」的同步脚本跑之前就位，所以不能先开启动页再写。
    """
    page.goto(f"{ar_server}/")
    store_settings(page, payload)


# =================================================== 1. 四块都在、无 JS 报错

def test_launcher_has_four_blocks_and_no_js_errors(page, ar_server):
    """四块按 ①状态 ②做测试 ③历史记录 ④输入文件 的顺序渲染；页面没有 JS 报错。"""
    api = LauncherApi(page, sources=sources_payload([MATERIAL_ENTRY]),
                      survey=survey_payload())
    open_launcher(page, ar_server)

    blocks = page.locator('[data-role="launcher-block"]')
    assert blocks.count() == 4, "启动页应该是四块"
    assert page.locator('[data-role="launcher-block"]').evaluate_all(
        "els => els.map(el => el.dataset.block)"
    ) == ["status", "survey", "history", "upload"]

    for name, title in (("status", "① 状态"), ("survey", "② 做测试"),
                        ("history", "③ 历史记录"), ("upload", "④ 输入文件")):
        expect(block(page, name)).to_be_visible()
        assert title in block(page, name).inner_text(), f"{name} 块的标题不对"

    debug = dbg(page)
    assert debug["page"] == "launcher"
    assert debug["ready"] is True
    assert debug["errors"] == [], debug["errors"]
    # 一切正常时不该有降级（横幅区是空的、degradations 是空的）
    assert debug["degradations"] == [], debug["degradations"]
    assert banner_texts(page) == []
    assert launcher_debug(page)["sources"]["state"] == "ok"
    assert launcher_debug(page)["sources"]["count"] == 1
    assert launcher_debug(page)["survey"]["state"] == "unanswered"
    assert api.calls("/api/sources") and api.calls("/api/survey")
    assert api.posts() == [], "打开启动页不该写任何东西"


def test_status_entry_lives_in_the_first_block(page, ar_server):
    """① 状态块里就是原来阅读器顶栏那个入口（面板行为由 test_status_panel.py 逐条测）。"""
    LauncherApi(page, sources=sources_payload([]), survey=survey_payload())
    open_launcher(page, ar_server)

    entry = page.locator('[data-role="status-toggle"]')
    expect(entry).to_be_visible()
    assert entry.evaluate(
        "el => { const b = el.closest('[data-role=\"launcher-block\"]');"
        " return b ? b.dataset.block : null; }"
    ) == "status"
    assert page.locator('[data-role="status-host"]').count() == 1
    assert dbg(page)["statusOpen"] is False
    assert page.locator('[data-role="status-card"]').count() == 0, "启动页不该自动弹出状态面板"


# =================================================== 2. ③ 历史记录

def test_history_renders_every_field_for_each_material(page, ar_server):
    """有材料：source_id / 段数 / mtime（本地可读）/ 动作统计 / 校验状态 / 缺哪份产物，逐条都在。"""
    LauncherApi(page, sources=sources_payload(
        [MATERIAL_ENTRY, INGEST_ONLY_ENTRY, FAILED_CHECK_ENTRY, ZERO_ACTIONS_ENTRY]),
        survey=survey_payload())
    open_launcher(page, ar_server)

    assert history_rows(page).count() == 4
    assert launcher_debug(page)["sources"]["count"] == 4
    assert "共 4 份材料" in page.locator('[data-role="launcher-history-state"]').inner_text()

    first = history_rows(page).nth(0)
    assert first.get_attribute("data-source-id") == "capital-v1-ch01"
    assert first.locator('[data-role="history-id"]').inner_text() == "capital-v1-ch01"
    assert "25 段" in first.locator('[data-role="history-paragraphs"]').inner_text()
    # 时间按本地可读格式显示（后端给的是带 +08:00 的本地时间戳，原样保留钟点）
    assert first.locator('[data-role="history-mtime"]').inner_text() == "2026-10-13 12:27:42 +08:00"
    assert first.locator('[data-role="history-actions"]').inner_text() == "展开 10 / 保持 15"
    badge = first.locator('[data-role="history-fidelity"]')
    assert badge.inner_text() == "已校验通过"
    assert badge.get_attribute("data-state") == "ok"
    assert "三份产物齐" in first.locator('[data-role="history-missing"]').inner_text()
    # 点一条 → 进 /reader?source=<id>
    assert first.locator('[data-role="history-link"]').get_attribute("href") == \
        "/reader?source=capital-v1-ch01"

    only_ingested = history_rows(page).nth(1)
    assert "4 段" in only_ingested.locator('[data-role="history-paragraphs"]').inner_text()
    missing = only_ingested.locator('[data-role="history-missing"]').inner_text()
    # 「只摄取」那一档：三份产物全缺，逐项点名（不是写一句「缺产物」了事）
    assert "只摄取" in missing, missing
    for needle in ("概念清单", "逐段改写", "事实锚"):
        assert needle in missing, f"缺产物说明里少了 {needle}：{missing}"
    assert only_ingested.locator('[data-role="history-fidelity"]').inner_text().startswith("未校验")
    assert only_ingested.locator('[data-role="history-actions"]').inner_text() == "动作统计未提供"

    failed = history_rows(page).nth(2)
    failed_badge = failed.locator('[data-role="history-fidelity"]')
    assert failed_badge.inner_text() == "有 2 处不一致"
    assert failed_badge.get_attribute("data-state") == "error"
    # 新动作键也要照原样打出来（压缩 2），不许静默丢
    assert failed.locator('[data-role="history-actions"]').inner_text() == "压缩 2"

    zero = history_rows(page).nth(3)
    assert zero.locator('[data-role="history-actions"]').inner_text() == "动作统计为空（各动作都是 0）"
    assert zero.locator('[data-role="history-fidelity"]').inner_text() == "未校验（还没跑过 ar check）"


def test_history_shows_the_backend_degraded_notes(page, ar_server):
    """后端顶层 `degraded[]`（缺哪份产物这类说明）也要在页面上看得见。"""
    note = "draft-notes：还没有 concepts.json，概念清单未抽取"
    LauncherApi(page, sources=sources_payload([INGEST_ONLY_ENTRY], degraded=[note]),
                survey=survey_payload())
    open_launcher(page, ar_server)

    notes = page.locator('[data-role="launcher-history-degraded"]')
    expect(notes).to_be_visible()
    assert note in notes.inner_text()
    assert note in launcher_debug(page)["sources"]["degraded"]


def test_history_empty_list_points_at_the_upload_block(page, ar_server):
    """空列表：给可读文案（不让读者以为是页面坏了），不报错、不打降级横幅。"""
    LauncherApi(page, sources=sources_payload([]), survey=survey_payload())
    open_launcher(page, ar_server)

    status = page.locator('[data-role="launcher-history-state"]')
    assert status.get_attribute("data-state") == "empty"
    assert "data/ 下还没有材料" in status.inner_text()
    assert "上传" in status.inner_text()
    assert history_rows(page).count() == 0
    assert launcher_debug(page)["sources"]["state"] == "empty"
    assert banner_texts(page) == [], "空列表不是失败，不该有降级横幅"


def test_history_http_404_degrades_visibly(page, ar_server):
    """接口 404：块内写明原因 + 一条降级横幅 + `__arDebug.launcher.sources`，页面不崩。"""
    LauncherApi(page, sources_status=404, survey=survey_payload())
    open_launcher(page, ar_server)

    status = page.locator('[data-role="launcher-history-state"]')
    assert status.get_attribute("data-state") == "degraded"
    text = status.inner_text()
    assert "材料清单没能读到" in text and "404" in text, text
    assert has_banner(page, "读不到材料清单")
    sources = launcher_debug(page)["sources"]
    assert sources["state"] == "degraded"
    assert "404" in sources["error"]
    assert any(d["kind"] == "sources-load" for d in dbg(page)["degradations"])
    # 页面照常可用：四块都在，别的块不受影响
    assert page.locator('[data-role="launcher-block"]').count() == 4
    assert launcher_debug(page)["survey"]["state"] == "unanswered"


def test_history_shape_mismatch_is_visible_and_broken_rows_are_named(page, ar_server):
    """结构不符①：`sources` 不是数组 → 可见降级；②：数组里混进坏条目 → 点名跳过，不静默丢。"""
    LauncherApi(page, sources_body=json.dumps({"sources": {"capital-v1-ch01": {}}, "degraded": []},
                                             ensure_ascii=False),
                survey=survey_payload())
    open_launcher(page, ar_server)
    status = page.locator('[data-role="launcher-history-state"]')
    assert status.get_attribute("data-state") == "degraded"
    text = status.inner_text()
    assert "结构不认识" in text and "sources 不是数组" in text, text
    assert "object" in launcher_debug(page)["sources"]["error"]
    assert has_banner(page, "结构不认识")

    # 换一页：数组里有坏条目（一条好的 + 一条没有 source_id 的）
    api = LauncherApi(page, sources=sources_payload([MATERIAL_ENTRY, {"paragraphs": 3}]),
                      survey=survey_payload())
    open_launcher(page, ar_server)
    assert history_rows(page).count() == 1, "坏条目不该被渲染，但也不许把它算成一份材料"
    notes = page.locator('[data-role="launcher-history-degraded"]').inner_text()
    assert "第 2 条没有可用的 source_id" in notes, notes
    assert any(d["kind"] == "sources-shape" for d in dbg(page)["degradations"])
    assert has_banner(page, "结构不符")
    assert api.calls("/api/sources")


# =================================================== 3. ④ 输入文件

def test_upload_button_is_disabled_until_a_whitelisted_file_is_chosen(page, ar_server):
    """未选文件 / 扩展名不在白名单 / source_id 非法：按钮禁用 + 可读提示，且**一个请求都不发**。"""
    api = LauncherApi(page, sources=sources_payload([]), survey=survey_payload())
    open_launcher(page, ar_server)

    submit = page.locator('[data-role="upload-submit"]')
    status = page.locator('[data-role="upload-status"]')
    assert submit.is_disabled(), "还没选文件时「摄取」按钮就该是禁用的"
    assert status.get_attribute("data-state") == "idle"
    assert "还没有选文件" in status.inner_text()

    # 扩展名不在白名单：前端就挡住（后端才是权威，但明显的错不该发出去）
    set_file(page, "note.pdf", "PDF 不是文本", mime="application/pdf")
    assert submit.is_disabled()
    assert status.get_attribute("data-state") == "invalid"
    assert ".md" in status.inner_text() and ".txt" in status.inner_text()

    # 白名单内的文件：按钮可用
    set_file(page, "note.md", "# 标题\n\n第一段。\n\n第二段。\n")
    assert not submit.is_disabled()
    assert status.get_attribute("data-state") == "ready"
    assert "note.md" in status.inner_text()

    # source_id 非法（含 ".." 与斜杠）：又禁用，并说清合法形状
    page.fill('[data-role="upload-source-id"]', "../escape")
    assert submit.is_disabled()
    assert status.get_attribute("data-state") == "invalid"
    assert "source_id 不合法" in status.inner_text()

    assert api.calls("/api/ingest") == [], "前端粗判没过时不许发请求"


def test_upload_success_shows_result_and_refreshes_history(page, ar_server):
    """上传成功：显示 source_id / 段数 / 是否覆盖 + 「去阅读」（`/reader?source=<id>`），并刷新清单。"""
    api = LauncherApi(page, sources=sources_payload([MATERIAL_ENTRY]), survey=survey_payload(),
                      ingest_payload={
                          "source_id": "upload-demo", "paragraphs": 3,
                          "ingested_at": "2026-10-15T21:00:00+08:00",
                          "anchors_stable": True, "overwritten": False, "degraded": [],
                      })
    open_launcher(page, ar_server)
    assert history_rows(page).count() == 1

    set_file(page, "upload-demo.md", "# 演示\n\n第一段。\n\n第二段。\n\n第三段。\n")
    page.click('[data-role="upload-submit"]')
    page.wait_for_function(
        "() => globalThis.__arDebug.launcher.upload.state === 'ok'", timeout=10000,
    )

    status = page.locator('[data-role="upload-status"]')
    assert status.get_attribute("data-state") == "ok"
    text = status.inner_text()
    assert "已摄取" in text and "upload-demo" in text and "3 段" in text and "新建" in text, text

    result = page.locator('[data-role="upload-result"]')
    expect(result).to_be_visible()
    assert "upload-demo" in result.inner_text()
    assert "3 段" in result.inner_text()
    assert "新建" in result.inner_text()
    assert page.locator('[data-role="upload-reader-link"]').get_attribute("href") == \
        "/reader?source=upload-demo"

    # 历史列表刷新了（新材料出现在最前面）
    page.wait_for_function(
        "() => globalThis.__arDebug.launcher.sources.count === 2", timeout=10000,
    )
    assert history_rows(page).count() == 2
    assert history_rows(page).nth(0).get_attribute("data-source-id") == "upload-demo"

    # 请求体是契约形状：filename / content / overwrite（没给 source_id 就不传）
    sent = api.calls("/api/ingest")[0]["json"]
    assert sent["filename"] == "upload-demo.md"
    assert sent["content"].startswith("# 演示")
    assert sent["overwrite"] is False
    assert "source_id" not in sent
    assert launcher_debug(page)["upload"]["paragraphs"] == 3
    assert launcher_debug(page)["upload"]["overwritten"] is False


def test_upload_with_explicit_source_id_and_overwrite_is_sent(page, ar_server):
    """显式 source_id + 覆盖勾选：请求体带上它们；成功文案写「覆盖了原有材料」。"""
    api = LauncherApi(page, sources=sources_payload([MATERIAL_ENTRY]), survey=survey_payload(),
                      ingest_payload={
                          "source_id": "capital-v1-ch01", "paragraphs": 25,
                          "ingested_at": "2026-10-15T21:05:00+08:00",
                          "anchors_stable": False, "overwritten": True,
                          "degraded": ["材料变了，需要重新校准画像"],
                      })
    open_launcher(page, ar_server)

    set_file(page, "whatever.md", "改写后的底本。\n")
    page.fill('[data-role="upload-source-id"]', "capital-v1-ch01")
    page.check('[data-role="upload-overwrite"]')
    page.click('[data-role="upload-submit"]')
    page.wait_for_function(
        "() => globalThis.__arDebug.launcher.upload.state === 'ok'", timeout=10000,
    )

    sent = api.calls("/api/ingest")[0]["json"]
    assert sent["source_id"] == "capital-v1-ch01" and sent["overwrite"] is True
    text = page.locator('[data-role="upload-status"]').inner_text()
    assert "覆盖了原有材料" in text
    # 锚点不稳 + 后端 degraded 都要看得见（不许静默）
    assert "段锚点与上一次摄取不一致" in text
    assert "材料变了，需要重新校准画像" in text


def test_upload_400_shows_the_backend_error_text(page, ar_server):
    """400：把后端的 `error` 原文显示出来（前端粗判放过的错，后端说了算）。"""
    api = LauncherApi(page, sources=sources_payload([]), survey=survey_payload(),
                      ingest_status=400,
                      ingest_payload={"error": "content 必须是非空字符串（上传的文本按 UTF-8 文本处理）"})
    open_launcher(page, ar_server)

    # 一个纯空白文件：前端不该替后端判它是不是「空」（content === "" 才算空），所以请求发得出去
    set_file(page, "blank.md", "   \n")
    page.click('[data-role="upload-submit"]')
    page.wait_for_function(
        "() => globalThis.__arDebug.launcher.upload.state === 'error'", timeout=10000,
    )

    status = page.locator('[data-role="upload-status"]')
    assert status.get_attribute("data-state") == "error"
    text = status.inner_text()
    assert "摄取失败（HTTP 400）" in text
    assert "content 必须是非空字符串" in text, text
    assert has_banner(page, "上传没能摄取")
    upload = launcher_debug(page)["upload"]
    assert upload["httpStatus"] == 400
    assert "content 必须是非空字符串" in upload["error"]
    assert page.locator('[data-role="upload-result"]').is_hidden()
    assert len(api.calls("/api/ingest")) == 1
    # 失败不刷新清单（那次请求什么都没写）
    assert len(api.calls("/api/sources")) == 1


def test_upload_409_shows_the_backend_error_and_the_hint(page, ar_server):
    """409：把后端的 `error`（已存在）与 `hint`（怎么覆盖）都显示出来。"""
    LauncherApi(page, sources=sources_payload([MATERIAL_ENTRY]), survey=survey_payload(),
                ingest_status=409,
                ingest_payload={
                    "error": "材料 capital-v1-ch01 已经存在，本次没有写任何东西："
                             "…\\data\\capital-v1-ch01\\source.md 已存在",
                    "status": 409,
                    "source_id": "capital-v1-ch01",
                    "hint": "要覆盖就带 overwrite: true（JSON 布尔 true）。",
                })
    open_launcher(page, ar_server)

    set_file(page, "capital-v1-ch01.md", "又一份底本。\n")
    page.fill('[data-role="upload-source-id"]', "capital-v1-ch01")
    page.click('[data-role="upload-submit"]')
    page.wait_for_function(
        "() => globalThis.__arDebug.launcher.upload.state === 'error'", timeout=10000,
    )

    text = page.locator('[data-role="upload-status"]').inner_text()
    assert "摄取失败（HTTP 409）" in text
    assert "已经存在，本次没有写任何东西" in text, text
    assert "要覆盖就带 overwrite: true" in text, text
    upload = launcher_debug(page)["upload"]
    assert upload["httpStatus"] == 409
    assert upload["hint"] and "overwrite" in upload["hint"]
    # 覆盖勾选没勾时失败：勾上之后按钮仍然可用（读者可以立刻重试）
    assert not page.locator('[data-role="upload-submit"]').is_disabled()
    assert history_rows(page).count() == 1


def test_upload_button_is_disabled_while_the_request_is_in_flight(page, ar_server):
    """上传中：按钮禁用（一次只跑一个请求），回包之后放开。"""
    api = LauncherApi(page, sources=sources_payload([]), survey=survey_payload(), hold_ingest=True)
    open_launcher(page, ar_server)

    set_file(page, "slow.md", "慢慢来。\n")
    page.click('[data-role="upload-submit"]')
    page.wait_for_function(
        "() => globalThis.__arDebug.launcher.upload.state === 'uploading'", timeout=10000,
    )
    assert page.locator('[data-role="upload-submit"]').is_disabled(), "上传中按钮要禁用"
    assert page.locator('[data-role="upload-status"]').get_attribute("data-state") == "uploading"
    assert api.held_routes, "假后端应该把这次请求 hold 住"

    api.release_ingest()
    page.wait_for_function(
        "() => globalThis.__arDebug.launcher.upload.state === 'ok'", timeout=10000,
    )
    assert not page.locator('[data-role="upload-submit"]').is_disabled()


# =================================================== 4. ② 做测试（问卷）

@pytest.mark.parametrize("kind,payload,expected_state,expected_text,button_label", [
    ("unanswered", survey_payload(), "unanswered", "还没答过", "填写问卷"),
    ("answered-with-time", survey_payload(answered={"education": "undergraduate"},
                                          answered_at="2026-10-09T16:40:00+08:00"),
     "answered", "已答（2026-10-09 16:40:00 +08:00）", "重填问卷"),
    ("answered-without-time", survey_payload(answered={"education": "undergraduate"}),
     "answered", "后端没给作答时间", "重填问卷"),
    ("skipped", survey_payload(skipped=True), "skipped", "已跳过", "重填问卷"),
])
def test_survey_card_shows_the_current_state(page, ar_server, kind, payload,
                                             expected_state, expected_text, button_label):
    """问卷卡三态（未答 / 已答 / 已跳过）：文案 + 按钮字样 + 可断言的调试面。"""
    LauncherApi(page, sources=sources_payload([]), survey=payload)
    open_launcher(page, ar_server)

    status = page.locator('[data-role="launcher-survey-state"]')
    assert status.get_attribute("data-state") == expected_state, kind
    assert expected_text in status.inner_text(), status.inner_text()
    assert page.locator('[data-role="launcher-survey-open"]').inner_text().strip() == button_label

    survey = launcher_debug(page)["survey"]
    assert survey["state"] == expected_state
    assert survey["answered"] is (expected_state == "answered")
    assert survey["skipped"] is (expected_state == "skipped")
    # 默认绝不自动弹（读者刚要求过删掉自动打扰的东西）
    assert dbg(page)["surveyOpen"] is False
    assert page.locator('[data-role="survey-card"]').count() == 0
    assert dbg(page)["surveyBootReason"] == "launcher-no-auto-popup"


def test_survey_card_shows_version_and_backend_notes(page, ar_server):
    """后端 `degraded[]` 与「问卷已更新」提示都要在卡片里看得见（说明不是失败，所以不打横幅）。"""
    LauncherApi(page, sources=sources_payload([]),
                survey=survey_payload(answered={"education": "undergraduate"},
                                      version=2, answered_version=1,
                                      degraded=["画像文件读不出来，按未提供处理"]))
    open_launcher(page, ar_server)

    notes = page.locator('[data-role="launcher-survey-degraded"]')
    expect(notes).to_be_visible()
    text = notes.inner_text()
    assert "画像文件读不出来" in text
    assert "问卷已更新" in text and "v1" in text and "v2" in text, text
    assert launcher_debug(page)["survey"]["answeredVersion"] == 1


def test_survey_card_degrades_visibly_when_the_endpoint_fails(page, ar_server):
    """`GET /api/survey` 失败：卡片写原因 + 降级横幅，按钮照旧可点（读者可以再试）。"""
    LauncherApi(page, sources=sources_payload([]), survey_status=404)
    open_launcher(page, ar_server)

    status = page.locator('[data-role="launcher-survey-state"]')
    assert status.get_attribute("data-state") == "degraded"
    assert "问卷状态读不到" in status.inner_text()
    assert "404" in status.inner_text()
    assert has_banner(page, "读不到问卷状态")
    survey = launcher_debug(page)["survey"]
    assert survey["state"] == "degraded" and "404" in survey["error"]
    assert not page.locator('[data-role="launcher-survey-open"]').is_disabled()


def test_clicking_the_button_opens_the_same_form_and_submit_refreshes_the_card(page, ar_server):
    """点「填写问卷」就地打开同一张问卷（8 题、与 `?survey=1` 同一份题目）；
    答完（提交成功）之后卡片立刻变成「已答」，关掉之后按钮放开、状态再刷一次。"""
    api = LauncherApi(page, sources=sources_payload([]), survey=survey_payload())
    open_launcher(page, ar_server)
    assert page.locator('[data-role="launcher-survey-open"]').inner_text().strip() == "填写问卷"

    page.click('[data-role="launcher-survey-open"]')
    page.wait_for_function("() => globalThis.__arDebug.surveyOpen === true", timeout=10000)
    card = page.locator('[data-role="survey-card"]')
    expect(card).to_be_visible()
    assert page.locator('[data-role="survey-question"]').count() == 8, "同一份题目（八道）"
    assert launcher_debug(page)["survey"]["openReason"] == "launcher"
    assert launcher_debug(page)["survey"]["lastOpen"]["opened"] is True
    # 打开期间启动页那颗按钮禁用（一次只开一张表单）
    assert page.locator('[data-role="launcher-survey-open"]').is_disabled()

    # 答一题（Q1「不想说」→ 条件题 Q2 / Q3a / Q3b 收起，提交体仍是合法的非空作答）
    page.locator('[data-qid="Q1"][data-value="prefer_not"]').check()
    page.click('[data-role="survey-submit"]')
    page.wait_for_function("() => globalThis.__arDebug.surveyPhase === 'done'", timeout=10000)

    # 提交走的是既有的 POST /api/survey（不带 version；只发八个作答槽位）
    posts = [item for item in api.calls("/api/survey") if item["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["json"] == {"answers": {"education": "prefer_not"}}

    # 「答完」之后卡片刷新成已答（不等读者点「完成」）
    page.wait_for_function(
        "() => globalThis.__arDebug.launcher.survey.state === 'answered'", timeout=10000,
    )
    status = page.locator('[data-role="launcher-survey-state"]')
    assert "已答" in status.inner_text()
    assert page.locator('[data-role="launcher-survey-open"]').inner_text().strip() == "重填问卷"

    # 关掉（完成）之后：按钮放开、状态再刷一次（仍按后端为准）
    page.click('[data-role="survey-finish"]')
    page.wait_for_function("() => globalThis.__arDebug.surveyOpen === false", timeout=10000)
    assert page.locator('[data-role="survey-card"]').count() == 0, "关掉后不留残影"
    page.wait_for_function(
        "() => !document.querySelector('[data-role=\"launcher-survey-open\"]').disabled", timeout=5000,
    )
    assert "已答" in page.locator('[data-role="launcher-survey-state"]').inner_text()
    assert launcher_debug(page)["survey"]["answered"] is True


def test_skipping_the_survey_refreshes_the_card_to_skipped(page, ar_server):
    """跳过（Esc 与「跳过」同效）也走同一条刷新：卡片变成「已跳过」，按钮回到「重填问卷」。"""
    LauncherApi(page, sources=sources_payload([]), survey=survey_payload())
    open_launcher(page, ar_server)

    page.click('[data-role="launcher-survey-open"]')
    page.wait_for_function("() => globalThis.__arDebug.surveyOpen === true", timeout=10000)
    page.keyboard.press("Escape")                      # Esc = 跳过（问卷自己的既有语义）
    page.wait_for_function("() => globalThis.__arDebug.surveyOpen === false", timeout=10000)
    page.wait_for_function(
        "() => globalThis.__arDebug.launcher.survey.state === 'skipped'", timeout=10000,
    )
    status = page.locator('[data-role="launcher-survey-state"]')
    assert status.get_attribute("data-state") == "skipped"
    assert "已跳过" in status.inner_text()
    assert page.locator('[data-role="launcher-survey-open"]').inner_text().strip() == "重填问卷"


def test_survey_manual_open_without_questions_degrades_instead_of_opening_an_empty_form(page, ar_server):
    """后端回了空 `questions[]`：不打开空壳表单，只留可见降级（启动页不读夹具题目）。"""
    LauncherApi(page, sources=sources_payload([]), survey=survey_payload(with_questions=False))
    open_launcher(page, ar_server)

    page.click('[data-role="launcher-survey-open"]')
    page.wait_for_function(
        "() => { const d = globalThis.__arDebug;"
        " return d.launcher.survey.lastOpen && d.launcher.survey.lastOpen.opened === false; }",
        timeout=10000,
    )
    assert launcher_debug(page)["survey"]["lastOpen"]["reason"] == "no-questions"
    assert dbg(page)["surveyOpen"] is False
    assert page.locator('[data-role="survey-card"]').count() == 0
    assert any(d["kind"] == "survey-questions-empty" for d in dbg(page)["degradations"])
    assert has_banner(page, "questions[] 为空")


# =================================================== 5. 显示设置（启动页也要吃同一份）

def test_launcher_body_takes_the_stored_display_settings(page, ar_server):
    """存档里选了深色 + 浅字 + 特大号：启动页的 `<body>` 必须照它来，不是恒定的纸白 16。

    修前（B1）：`<html>` 上是 dark / light / 19（那段同步脚本只打 `documentElement`），
    而 `<body>` 还是标记里写死的 paper / ink / 16 —— 实测 `getComputedStyle(body).backgroundColor`
    仍是 `rgb(251, 250, 247)`，同一份设置下阅读器页是 `rgb(27, 31, 35)`。
    """
    LauncherApi(page, sources=sources_payload([MATERIAL_ENTRY]), survey=survey_payload())
    seed_settings(page, ar_server, {"bg": "dark", "ink": "light", "size": "19"})
    open_launcher(page, ar_server)

    body = page.locator("body")
    assert body.get_attribute("data-bg") == "dark", "启动页的 <body> 没吃存档里的背景档"
    assert body.get_attribute("data-ink") == "light"
    assert body.get_attribute("data-size") == "19"
    for key in ("data-bg", "data-ink", "data-size"):
        assert page.locator("html").get_attribute(key) == body.get_attribute(key), \
            f"{key} 在 <html> 与 <body> 上不一致（两处必须是同一份设置）"

    assert page.eval_on_selector("body", "el => getComputedStyle(el).backgroundColor") == BG_RGB["dark"]
    assert page.evaluate(
        "() => getComputedStyle(document.body).getPropertyValue('--reading-size').trim()"
    ) == "19px"
    # 复访不该把读者的存档改回去
    assert json.loads(page.evaluate("k => localStorage.getItem(k)", SETTINGS_KEY))["bg"] == "dark"


def test_launcher_first_visit_follows_the_system_dark_scheme(page, ar_server):
    """空 localStorage + 系统深色：首访就该是深色底（不是纸白），并按阅读器同一口径落盘。"""
    page.emulate_media(color_scheme="dark")
    LauncherApi(page, sources=sources_payload([]), survey=survey_payload())
    open_launcher(page, ar_server)

    body = page.locator("body")
    assert body.get_attribute("data-bg") == "dark"
    assert body.get_attribute("data-ink") == "light"
    assert page.eval_on_selector("body", "el => getComputedStyle(el).backgroundColor") == BG_RGB["dark"]
    stored = json.loads(page.evaluate("k => localStorage.getItem(k)", SETTINGS_KEY))
    assert stored["bg"] == "dark" and stored["ink"] == "light"
    page.emulate_media(color_scheme="light")


def test_launcher_and_reader_agree_on_the_background(page, ar_server):
    """同一份存档下两页的底色必须一样（修前：启动页纸白、阅读器页深色，跳页就换一套底色）。"""
    LauncherApi(page, sources=sources_payload([]), survey=survey_payload())
    seed_settings(page, ar_server, {"bg": "dark", "ink": "light", "size": "19"})
    open_launcher(page, ar_server)
    launcher_bg = page.eval_on_selector("body", "el => getComputedStyle(el).backgroundColor")

    page.goto(f"{ar_server}/web/reader.html?fixture={FIXTURE_ID}")
    wait_ready(page)
    reader_bg = page.eval_on_selector("body", "el => getComputedStyle(el).backgroundColor")

    assert launcher_bg == reader_bg == BG_RGB["dark"], (launcher_bg, reader_bg)


def test_launcher_lead_text_is_readable_in_all_six_combinations(page, ar_server):
    """启动页那句导语在 6 种背景 × 文字组合下都要读得清（这一页没有设置面板可以放对比度告警，
    所以只能把颜色取成跟背景成对的那一档；修前用的是跟 `data-ink` 走的 `--muted`，
    「纸白 + 浅灰」这类手动组合下只有 1.84:1）。"""
    LauncherApi(page, sources=sources_payload([]), survey=survey_payload())
    open_launcher(page, ar_server)

    problems = []
    ratios = {}
    for bg, ink in SETTINGS_COMBOS:
        store_settings(page, {"bg": bg, "ink": ink, "size": "16", "inkExplicit": True})
        page.reload()
        wait_launcher_ready(page)
        lead = page.locator('[data-role="launcher-lead"]')
        expect(lead).to_be_visible()
        foreground = lead.evaluate("el => getComputedStyle(el).color")
        background = page.eval_on_selector("body", "el => getComputedStyle(el).backgroundColor")
        assert background == BG_RGB[bg], f"{bg} 档的页面底色不对：{background}"
        ratio = contrast_ratio(foreground, background)
        ratios[f"{bg}+{ink}"] = round(ratio, 2)
        if ratio < MIN_CONTRAST:
            problems.append(f"{bg}+{ink} 只有 {ratio:.2f}:1（{foreground} on {background}）")

    assert problems == [], "启动页导语读不清：\n- " + "\n- ".join(problems) + f"\n实测：{ratios}"


# =================================================== 6. 连点 / 非 UTF-8 / invalid 档

def test_upload_rapid_clicks_send_exactly_one_request(page, ar_server):
    """同一个任务连点 3 次：只发 1 个 `POST /api/ingest`，最后显示的是**成功**。

    修前（B2）：`uploadPending` 在 `await file.text()` **之后**才置位，三次同步点击各自读到文件、
    各发一个请求（实测 3 个 POST）：第一次 200 已写盘，第 2/3 次被后端 409 拒掉，
    页面最后红字写着「摄取失败（HTTP 409）…已经存在」——而它明明已经摄取成功了。
    """
    api = LauncherApi(page, sources=sources_payload([]), survey=survey_payload(), hold_ingest=True)
    open_launcher(page, ar_server)

    set_file(page, "rapid.md", "# 演示\n\n第一段。\n")
    click_submit(page, times=3)
    page.wait_for_function(
        "() => globalThis.__arDebug.launcher.upload.state === 'uploading'", timeout=10000,
    )

    assert len(api.calls("/api/ingest")) == 1, \
        f"连点 3 次该只发一个请求，实际发了 {len(api.calls('/api/ingest'))} 个"
    assert len(api.held_routes) == 1
    # 请求在飞时按钮是禁用的（第二次点击连入口都进不去）
    assert page.locator('[data-role="upload-submit"]').is_disabled()

    api.release_ingest()
    page.wait_for_function(
        "() => globalThis.__arDebug.launcher.upload.state === 'ok'", timeout=10000,
    )

    assert len(api.calls("/api/ingest")) == 1
    text = page.locator('[data-role="upload-status"]').inner_text()
    assert "已摄取" in text and "409" not in text and "失败" not in text, text
    assert upload_debug(page)["httpStatus"] == 200
    assert upload_debug(page)["error"] is None


def test_non_utf8_upload_is_ingested_with_a_visible_note(page, ar_server):
    """GBK 字节（不是 UTF-8）：后端是权威、照常摄取，但页面必须说出来「正文可能乱码」。

    修前（B3）：`file.text()` 按 UTF-8 解码，坏字节只会变成 U+FFFD、永不 reject，
    页面显示「已摄取：gbk-src（2 段，新建）」，既没有横幅也没有任何说明——读者以为一切正常。
    """
    api = LauncherApi(page, sources=sources_payload([]), survey=survey_payload(),
                      ingest_payload={
                          "source_id": "gbk-src", "paragraphs": 2,
                          "ingested_at": "2026-10-15T21:30:00+08:00",
                          "anchors_stable": True, "overwritten": False, "degraded": [],
                      })
    open_launcher(page, ar_server)

    gbk_bytes = "这是一份 GBK 编码的中文材料。\n\n第二段也还是中文。\n".encode("gbk")
    assert b"\xef\xbf\xbd" not in gbk_bytes, "造的文件本身不该含替换字符"
    set_file_bytes(page, "gbk-src.md", gbk_bytes)
    click_submit(page)
    page.wait_for_function(
        "() => globalThis.__arDebug.launcher.upload.state === 'ok'", timeout=10000,
    )

    # 摄取照样发出去了（前端不拦，后端才是权威），但说明必须看得见
    assert len(api.calls("/api/ingest")) == 1
    status = page.locator('[data-role="upload-status"]')
    assert "不是 UTF-8" in status.inner_text(), status.inner_text()
    result = page.locator('[data-role="upload-result"]')
    expect(result).to_be_visible()
    assert "不是 UTF-8" in result.inner_text(), result.inner_text()
    assert has_banner(page, "不是 UTF-8"), banner_texts(page)

    upload = upload_debug(page)
    assert upload["state"] == "ok"
    assert upload["encoding"] == "utf-8-lossy", upload
    assert "不是 UTF-8" in (upload["encodingNote"] or ""), upload
    assert any(d["kind"] == "upload-encoding" for d in dbg(page)["degradations"]), \
        dbg(page)["degradations"]


def test_upload_binary_bytes_are_ingested_with_a_visible_note(page, ar_server):
    """二进制字节（0..255，含 NUL）：同样照常摄取，但状态行要说清「这份文件不是 UTF-8 文本」。"""
    LauncherApi(page, sources=sources_payload([]), survey=survey_payload(),
                ingest_payload={
                    "source_id": "bin-src", "paragraphs": 1,
                    "ingested_at": "2026-10-15T21:35:00+08:00",
                    "anchors_stable": True, "overwritten": False, "degraded": [],
                })
    open_launcher(page, ar_server)

    set_file_bytes(page, "bin-src.md", bytes(range(256)), mime="application/octet-stream")
    click_submit(page)
    page.wait_for_function(
        "() => globalThis.__arDebug.launcher.upload.state === 'ok'", timeout=10000,
    )

    text = page.locator('[data-role="upload-status"]').inner_text()
    assert "不是 UTF-8" in text, text
    assert has_banner(page, "不是 UTF-8")
    assert upload_debug(page)["encoding"] == "utf-8-lossy"


@pytest.mark.parametrize("kind", ["empty-file", "bad-extension", "bad-source-id"])
def test_upload_invalid_states_carry_a_reason_and_clear_the_previous_result(page, ar_server, kind):
    """`invalid` 三档：`error` 写清原因，上一轮的 sourceId / paragraphs / overwritten /
    httpStatus / url 一律清成 `null`。

    修前（B4）：先成功上传一次（`httpStatus: 200`、`paragraphs: 3`），再触发 invalid，
    `launcher.upload` 里上一轮的值全留着、`error` 还是 `null`——调试面在说假话。
    """
    api = LauncherApi(page, sources=sources_payload([]), survey=survey_payload(),
                      ingest_payload={
                          "source_id": "first-ok", "paragraphs": 3,
                          "ingested_at": "2026-10-15T21:40:00+08:00",
                          "anchors_stable": True, "overwritten": False, "degraded": [],
                      })
    open_launcher(page, ar_server)

    set_file(page, "first-ok.md", "# 先成功一次\n\n一段。\n")
    click_submit(page)
    page.wait_for_function(
        "() => globalThis.__arDebug.launcher.upload.state === 'ok'", timeout=10000,
    )
    before = upload_debug(page)
    assert before["httpStatus"] == 200 and before["paragraphs"] == 3, before
    assert len(api.calls("/api/ingest")) == 1

    if kind == "empty-file":
        set_file(page, "empty.md", "")
        click_submit(page)
    elif kind == "bad-extension":
        set_file(page, "note.pdf", "PDF 不是文本", mime="application/pdf")
    else:
        page.fill('[data-role="upload-source-id"]', "../escape")

    page.wait_for_function(
        "() => globalThis.__arDebug.launcher.upload.state === 'invalid'", timeout=10000,
    )
    upload = upload_debug(page)
    assert upload["error"], f"{kind}：invalid 档必须写清原因，实际 error={upload['error']!r}"
    for field in ("sourceId", "paragraphs", "overwritten", "httpStatus", "url",
                  "hint", "encoding", "encodingNote"):
        assert upload[field] is None, f"{kind}：{field} 该清成 null，实际 {upload[field]!r}"
    # 状态行照旧把原因写在脸上（清字段不许把文案也清掉）
    status = page.locator('[data-role="upload-status"]')
    assert status.get_attribute("data-state") == "invalid"
    assert status.inner_text().strip(), "invalid 档的状态行不该是空的"
    assert len(api.calls("/api/ingest")) == 1, "invalid 档不许发请求"


# =================================================== 7. 结构不符的 url / JSON null（口径）

def test_sources_shape_mismatch_reports_the_url_it_actually_hit(page, ar_server):
    """结构不符那两条也要带上**实际打到的地址**（`launcher.sources.url` 不许是 null）。"""
    LauncherApi(page,
                sources_body=json.dumps({"sources": {"capital-v1-ch01": {}}, "degraded": []},
                                        ensure_ascii=False),
                survey=survey_payload())
    open_launcher(page, ar_server)

    sources = launcher_debug(page)["sources"]
    assert sources["state"] == "degraded"
    assert sources["url"] and sources["url"].endswith("/api/sources"), sources


def test_json_null_response_is_described_truthfully(page, ar_server):
    """后端回的是合法 JSON `null`：不许说成「响应不是合法 JSON（未知原因）」（那是假话）。"""
    LauncherApi(page, sources_body="null", survey=survey_payload())
    open_launcher(page, ar_server)

    text = page.locator('[data-role="launcher-history-state"]').inner_text()
    assert "不是合法 JSON" not in text, text
    assert "null" in text, text
    error = launcher_debug(page)["sources"]["error"]
    assert "null" in error and "合法 JSON" not in error.replace("不是合法 JSON", ""), error
