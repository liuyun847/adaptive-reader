"""**启动页**「状态」块（①）的无头验证（playwright 驱动本机 Edge）。

🔁 2026-10-15 拆页后：状态面板从阅读器顶栏搬到了**启动页**（`GET /` → `web/index.html`），
所以本文件的用例一律打开启动页；阅读器页（`web/reader.html`）里**没有**这个入口
（`test_reader_page_has_no_status_entry` 守着这一点）。

覆盖的契约：

1. **入口在启动页的①状态块里**（不再是阅读器顶栏那条）；点开显示两块内容——① 测试结论 ② 本次修改；
2. **数据源是机器生成的同源静态文件** `/status.json`（由 `tools/gen_status.py` 产出），
   前端不硬编码任何数字，也不写死「通过」；
3. **降级可见**（`web/README.md` 的「降级纪律」）：报告取不到 / 坏掉 / 字段不认识时，
   面板里写明「尚未生成：在项目根跑 `uv run python tools/gen_status.py`」——不许空白、不许假装成功；
4. **`Esc` 关闭、关闭后整层移除不留残影、打开时焦点进面板、`focus-visible` 有可见轮廓**；
5. **滚动纪律**：面板自己的滚动区吃掉滚轮，不许像问卷那样穿透到背后正文
   （口径同 `web/js/survey.js` 的 `handleWheel`：文档捕获阶段 + 坐标命中判定 + `preventDefault()`，
   滚动区另有 `overscroll-behavior: contain` 作第二道闸）；
6. **启动页上入口照常可用**：它就取同源的 `/status.json`；
7. **真服务（`http://127.0.0.1:8765`）上的复验**（末尾那条）：`/` 就是启动页，页面上的数字与磁盘上的
   `web/status.json` 逐项一致；**一个模型调用都不发**（断言没有 `/api/explain`、
   `/api/survey/followup` 请求，也没有任何 POST）；服务没起就 skip（不是失败，口径同
   `test_reader_features.py` 的两条联调用例）。

本文件**不改动** `tests/web/conftest.py`：报告用 `page.route` 在浏览器侧喂进页面
（与 `test_survey_ui.py` 挂假后端同一手法）；启动页另外那两块（材料清单 / 问卷状态）在这里用
`stub_launcher_api` 喂成「空的但正常」，免得它们的降级横幅串进状态面板的用例；
`test_launcher_without_the_artifact_degrades_visibly` 专门用静态服务的**真实 404**
（那个服务把项目根当站点，根下没有 `status.json`，只有 `web/status.json`）验降级文案。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import expect

from test_survey_ui import FIXTURE_ID, dbg  # noqa: E402  —— 复用既有的等就绪 / 调试面助手

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
ARTIFACT = PROJECT / "web" / "status.json"
REAL_BASE = "http://127.0.0.1:8765"

# 启动页另外两块的后端端点（材料清单 / 问卷状态）：喂成「空的但正常」，
# 免得它们的降级横幅串进状态面板的用例。状态面板自己那条 `/status.json` 由各用例单独喂。
SOURCES_ROUTE = re.compile(r"/api/sources$")
SURVEY_ROUTE = re.compile(r"/api/survey$")

# `/status.json` 的 URL 匹配式（用正则：glob 的 `**/status.json` 语义在 playwright 里够用，
# 但正则更直白，也避免与将来别的 `*status.json` 撞车）。
STATUS_ROUTE = re.compile(r"/status\.json$")

GENERATED_AT = "2026-10-09T09:40:00+08:00"
BACKEND_SUMMARY = "248 passed, 1 warning in 2.46s"
WEB_SUMMARY = "84 passed, 1 skipped, 1 warning in 65.03s"

SAMPLE_FILES = [
    {"status": "M", "path": "web/js/view.js", "insertions": 60, "deletions": 5},
    {"status": "M", "path": "web/index.html", "insertions": 12, "deletions": 1},
    {"status": "??", "path": "tools/gen_status.py", "insertions": 118, "deletions": 0, "untracked": True},
]


def report(*, note="给阅读页加状态入口", files=None, totals=None,
           backend=None, web=None, generated_at=GENERATED_AT) -> dict:
    """一份形状与 `tools/gen_status.py` 产物一致的报告（数字都是假的，只用来验渲染）。"""
    rows = SAMPLE_FILES if files is None else files
    return {
        "generated_at": generated_at,
        "tests": {
            "backend": backend if backend is not None else {
                "command": "uv run pytest tests -q", "ran": True, "ok": True, "exit_code": 0,
                "passed": 248, "failed": 0, "skipped": 0, "seconds": 2.5, "summary": BACKEND_SUMMARY,
            },
            "web": web if web is not None else {
                "command": "python -m pytest tests/web -q", "ran": True, "ok": True, "exit_code": 0,
                "passed": 84, "failed": 0, "skipped": 1, "seconds": 65.0, "summary": WEB_SUMMARY,
            },
        },
        "changes": {
            "head": "94e9917 perf: prompt 前缀重排 + 首批预热 + 缓存命中可视",
            "note": note,
            "files": rows,
            "totals": totals if totals is not None else {
                "files": len(rows),
                "insertions": sum(r.get("insertions") or 0 for r in rows),
                "deletions": sum(r.get("deletions") or 0 for r in rows),
            },
        },
    }


def many_files(count: int = 60) -> list:
    return [
        {"status": "M", "path": f"web/js/file_{i:03d}.js", "insertions": i + 1, "deletions": 1}
        for i in range(count)
    ]


# ------------------------------------------------------------------ 工具

def stub_report(page, payload: dict) -> None:
    """把 `/status.json` 喂成给定的报告（浏览器侧拦截，不碰磁盘上的真产物）。"""
    body = json.dumps(payload, ensure_ascii=False)
    page.route(STATUS_ROUTE, lambda route: route.fulfill(
        status=200, content_type="application/json; charset=utf-8", body=body,
    ))


def stub_report_missing(page, status: int = 404) -> None:
    page.route(STATUS_ROUTE, lambda route: route.fulfill(
        status=status, content_type="text/plain; charset=utf-8", body="Not Found",
    ))


def stub_launcher_api(page) -> None:
    """启动页另外两块（③材料清单 / ②问卷卡）的假后端：空的但正常，不带任何降级横幅。

    不给它们喂响应的话，静态服务会回 404，页面上会挂两条降级横幅——那会把状态面板的用例
    搅成「在测别的东西」。这两块自己的行为由 `test_launcher.py` 负责。
    """
    page.route(SOURCES_ROUTE, lambda route: route.fulfill(
        status=200, content_type="application/json; charset=utf-8",
        body=json.dumps({"sources": [], "degraded": []}, ensure_ascii=False),
    ))
    page.route(SURVEY_ROUTE, lambda route: route.fulfill(
        status=200, content_type="application/json; charset=utf-8",
        body=json.dumps({
            "version": 1, "answered_version": None, "questions": [],
            "answers": None, "skipped": False, "degraded": [],
        }, ensure_ascii=False),
    ))


def wait_launcher_ready(page, timeout: int = 15000) -> dict:
    """等启动页的显式就绪标志（`ready` + `jsLoaded`），不 sleep（与阅读器同一口径）。"""
    page.wait_for_function(
        "() => globalThis.__arDebug && globalThis.__arDebug.ready === true"
        " && globalThis.__arDebug.jsLoaded === true",
        timeout=timeout,
    )
    return dbg(page)


def open_launcher_page(page, ar_server) -> str:
    """打开启动页（`GET /` → `web/index.html`）：静态服务把项目根当站点，所以是 `/web/index.html`。

    与拆页前的区别：那条路原来是阅读器页（`/web/index.html?fixture=…`），现在是启动页本身。
    """
    url = f"{ar_server}/web/index.html"
    page.goto(url)
    wait_launcher_ready(page)
    return url


def toggle(page):
    return page.locator('[data-role="status-toggle"]')


def card(page):
    return page.locator('[data-role="status-card"]')


def body(page):
    return page.locator('[data-role="status-body"]')


def suite(page, name: str):
    return page.locator(f'[data-role="status-suite"][data-suite="{name}"]')


def open_panel(page) -> None:
    """点入口并等面板真的渲染完（等显式状态，不 sleep）。"""
    toggle(page).click()
    expect(card(page)).to_be_visible()
    page.wait_for_function(
        "() => { const d = globalThis.__arDebug;"
        " return d.statusOpen === true && d.statusState !== 'loading'; }",
        timeout=10000,
    )


def close_panel(page) -> None:
    page.keyboard.press("Escape")
    page.wait_for_function("() => globalThis.__arDebug.statusOpen === false", timeout=5000)


# =================================================== 1. 入口在启动页①状态块、能开、焦点进面板

def test_entry_sits_in_the_launcher_status_block_and_opens_the_panel_with_focus_inside(page, ar_server):
    """入口在启动页的①状态块里（`data-block="status"`），点开后焦点进面板、`aria-expanded` 跟随。"""
    stub_report(page, report())
    stub_launcher_api(page)
    open_launcher_page(page, ar_server)

    entry = toggle(page)
    expect(entry).to_be_visible()
    # 拆页前这一条断言的是「与「Aa 显示」并排在顶栏里」；入口搬进启动页的①块之后，
    # 改断言「它确实在那个块里」——那才是这次改造要守住的位置。
    assert page.evaluate(
        "() => { const b = document.querySelector('[data-role=\"status-toggle\"]');"
        " const block = b && b.closest('[data-role=\"launcher-block\"]');"
        " return Boolean(block && block.dataset.block === 'status' && b.closest('.lcard')); }"
    ), "状态入口不在启动页的①状态块里"
    assert page.locator('[data-role="launcher-block"]').count() == 4, "启动页应该是四块"
    status_block = page.locator('[data-role="launcher-block"][data-block="status"]')
    assert entry.bounding_box()["y"] >= status_block.bounding_box()["y"] - 1

    assert dbg(page)["statusOpen"] is False
    expect(card(page)).to_have_count(0)

    entry.click()
    expect(card(page)).to_be_visible()
    assert dbg(page)["statusOpen"] is True
    assert entry.get_attribute("aria-expanded") == "true"

    inside = page.evaluate(
        "() => Boolean(document.activeElement"
        " && document.activeElement.closest('[data-role=\"status-card\"]'))"
    )
    assert inside, "打开面板时焦点要进入面板"


def test_reader_page_has_no_status_entry(page, ar_server):
    """拆页纪律：阅读器页（`web/reader.html`）里不加载启动页的模块，也没有状态入口与宿主。"""
    page.goto(f"{ar_server}/web/reader.html?fixture={FIXTURE_ID}")
    page.wait_for_function(
        "() => globalThis.__arDebug && globalThis.__arDebug.ready === true", timeout=15000,
    )
    assert page.locator('[data-role="status-toggle"]').count() == 0, "阅读器页不该有状态入口"
    assert page.locator('[data-role="status-host"]').count() == 0, "阅读器页不该有状态宿主"
    assert page.locator('[data-role="launcher-block"]').count() == 0, "阅读器页不该有启动页的块"
    assert dbg(page)["statusEvents"] == [], "阅读器页不该碰状态面板那条路"


# =================================================== 2. 两块内容按报告渲染

def test_panel_renders_tests_and_changes_from_the_report(page, ar_server):
    """① 测试结论：两套的 passed / failed / skipped + 耗时 + 摘要行；② 本次修改：note / 汇总 / 清单。"""
    stub_report(page, report())
    stub_launcher_api(page)
    open_launcher_page(page, ar_server)
    open_panel(page)

    assert card(page).get_attribute("data-state") == "ok"
    assert GENERATED_AT in page.locator('[data-role="status-generated-at"]').inner_text()

    # ① 两块都在
    expect(page.locator('[data-role="status-tests"]')).to_be_visible()
    expect(page.locator('[data-role="status-changes"]')).to_be_visible()
    assert "测试结论" in page.locator('[data-role="status-tests"]').inner_text()
    assert "本次修改" in page.locator('[data-role="status-changes"]').inner_text()

    backend = suite(page, "backend")
    text = backend.inner_text()
    assert backend.get_attribute("data-state") == "ok"
    for needle in ("248", "0", "2.5", "uv run pytest tests -q"):
        assert needle in text, f"后端结论缺少 {needle!r}：{text}"
    assert "通过" in text and "失败" in text and "跳过" in text
    assert BACKEND_SUMMARY in backend.locator('[data-role="status-suite-summary"]').inner_text()

    web = suite(page, "web")
    web_text = web.inner_text()
    assert web.get_attribute("data-state") == "ok"
    for needle in ("84", "1", "65.0", "python -m pytest tests/web -q"):
        assert needle in web_text, f"前端结论缺少 {needle!r}：{web_text}"

    # ② 本次修改
    assert "给阅读页加状态入口" in page.locator('[data-role="status-note"]').inner_text()
    totals = page.locator('[data-role="status-totals"]').inner_text()
    assert "3 个文件" in totals and "+190" in totals and "−6" in totals, totals

    rows = page.locator('[data-role="status-file"]')
    assert rows.count() == len(SAMPLE_FILES)
    first = rows.first
    assert first.get_attribute("data-status") == "M"
    first_text = first.inner_text()
    assert "web/js/view.js" in first_text and "+60" in first_text and "−5" in first_text
    assert "改动" in first_text

    untracked = page.locator('[data-role="status-file"][data-status="??"]')
    assert untracked.count() == 1
    untracked_text = untracked.first.inner_text()
    assert "tools/gen_status.py" in untracked_text
    assert "新文件" in untracked_text, "未跟踪文件要标出来"
    assert "+118" in untracked_text, "未跟踪文件也要给行数"

    # 报告里的东西不许被前端改口径：路径逐条对上
    listed = rows.all_inner_texts()
    for row in SAMPLE_FILES:
        assert any(row["path"] in text for text in listed), f"{row['path']} 没出现在清单里"


def test_no_changes_is_stated_not_left_blank(page, ar_server):
    """干净工作区：清单区写明「没有未提交的改动」，不留空白。"""
    stub_report(page, report(note="", files=[], totals={"files": 0, "insertions": 0, "deletions": 0}))
    stub_launcher_api(page)
    open_launcher_page(page, ar_server)
    open_panel(page)

    assert page.locator('[data-role="status-note"]').count() == 0, "没有 note 时不该渲染这一行"
    changes = page.locator('[data-role="status-changes"]').inner_text()
    assert "没有未提交的改动" in changes, changes
    assert "0 个文件" in page.locator('[data-role="status-totals"]').inner_text()


def test_untracked_and_error_color_are_reported_for_failed_suites(page, ar_server):
    """`failed > 0` 或调用失败：结论行用醒目色（--err-*），不许与「全绿」长得一样。"""
    stub_report(page, report(backend={
        "command": "uv run pytest tests -q", "ran": True, "ok": False, "exit_code": 1,
        "passed": 246, "failed": 2, "skipped": 0, "seconds": 3.1,
        "summary": "246 passed, 2 failed in 3.05s",
    }))
    stub_launcher_api(page)
    open_launcher_page(page, ar_server)
    open_panel(page)

    backend = suite(page, "backend")
    assert backend.get_attribute("data-state") == "error"
    counts = backend.locator('[data-role="status-suite-counts"]')
    assert "2" in counts.inner_text() and "失败" in counts.inner_text()

    expected = page.evaluate(
        "() => { const probe = document.createElement('span');"
        " probe.style.color = 'var(--err-ink)'; document.body.appendChild(probe);"
        " const c = getComputedStyle(probe).color; probe.remove(); return c; }"
    )
    actual = counts.evaluate("el => getComputedStyle(el).color")
    assert actual == expected, f"失败结论没有用醒目色：{actual} != {expected}"


def test_suite_that_did_not_run_is_shown_as_not_run(page, ar_server):
    """`--skip-web`（或那次没跑起来）：写「本次没跑」，不许拿假数字冒充。"""
    stub_report(page, report(web={
        "command": "python -m pytest tests/web -q", "ran": False, "reason": "--skip-web",
    }))
    stub_launcher_api(page)
    open_launcher_page(page, ar_server)
    open_panel(page)

    web = suite(page, "web")
    assert web.get_attribute("data-state") == "skip"
    text = web.inner_text()
    assert "本次没跑" in text and "--skip-web" in text, text


# =================================================== 3. 降级可见

def test_missing_report_shows_visible_degradation(page, ar_server):
    """`/status.json` 取不到（404）：面板写明「尚未生成」+ 生成命令，不空白、不假装成功。"""
    stub_report_missing(page)
    stub_launcher_api(page)
    open_launcher_page(page, ar_server)
    open_panel(page)

    assert card(page).get_attribute("data-state") == "degraded"
    missing = page.locator('[data-role="status-missing"]')
    expect(missing).to_be_visible()
    text = missing.inner_text()
    assert "尚未生成" in text, text
    assert "uv run python tools/gen_status.py" in text, text
    # 降级时不许出现假的测试结论
    assert page.locator('[data-role="status-suite"]').count() == 0
    assert "通过" not in body(page).inner_text()

    debug = dbg(page)
    assert debug["statusState"] == "degraded"
    assert debug["statusPayload"] is None
    assert any("404" in d["message"] for d in debug["statusDegraded"]), debug["statusDegraded"]


def test_broken_report_shows_degradation_instead_of_fake_success(page, ar_server):
    """`/status.json` 是坏 JSON（半截文件 / 手写坏掉）：同样走「尚未生成」降级路径。"""
    page.route(STATUS_ROUTE, lambda route: route.fulfill(
        status=200, content_type="application/json; charset=utf-8", body='{"generated_at": "2026-',
    ))
    stub_launcher_api(page)
    open_launcher_page(page, ar_server)
    open_panel(page)

    assert card(page).get_attribute("data-state") == "degraded"
    missing = page.locator('[data-role="status-missing"]').inner_text()
    assert "尚未生成" in missing and "uv run python tools/gen_status.py" in missing, missing
    debug = dbg(page)
    assert debug["statusPayload"] is None
    assert debug["statusDegraded"], "坏报告必须在 __arDebug 里留下可断言的降级记录"


def test_launcher_without_the_artifact_degrades_visibly(page, ar_server):
    """启动页走静态服务的**真实** 404（站点根下没有 status.json，只有 web/status.json）。"""
    stub_launcher_api(page)
    open_launcher_page(page, ar_server)          # 不给 /status.json 挂 route：让请求真的落到静态服务上
    open_panel(page)

    assert card(page).get_attribute("data-state") == "degraded"
    assert "尚未生成" in page.locator('[data-role="status-missing"]').inner_text()
    assert dbg(page)["statusUrl"] and dbg(page)["statusUrl"].endswith("/status.json")


# =================================================== 4. 关闭：Esc / × 、不留残影、焦点归还

def test_escape_closes_the_layer_without_residue_and_restores_focus(page, ar_server):
    stub_report(page, report())
    stub_launcher_api(page)
    open_launcher_page(page, ar_server)
    open_panel(page)

    close_panel(page)

    assert page.locator('[data-role="status-layer"]').count() == 0
    assert page.locator('[data-role="status-card"]').count() == 0, "关掉后卡片节点不许留在 DOM 里"
    assert page.locator('#status-host > *').count() == 0, "宿主里不许留残影"
    assert page.evaluate("() => document.body.dataset.status") == "closed"
    assert toggle(page).get_attribute("aria-expanded") == "false"
    assert page.evaluate(
        "() => document.activeElement && document.activeElement.dataset.role"
    ) == "status-toggle", "关闭后焦点要回到入口"
    # 页面照常可用（面板不碰页面内容）：启动页四块都还在
    assert page.locator('[data-role="launcher-block"]').count() == 4
    expect(page.locator('[data-role="launcher-survey-open"]')).to_be_visible()


def test_close_button_removes_the_layer(page, ar_server):
    stub_report(page, report())
    stub_launcher_api(page)
    open_launcher_page(page, ar_server)
    open_panel(page)

    page.click('[data-role="status-close"]')
    page.wait_for_function("() => globalThis.__arDebug.statusOpen === false", timeout=5000)

    assert page.locator('[data-role="status-layer"]').count() == 0
    assert dbg(page)["statusEvents"][-1]["type"] == "close"
    assert dbg(page)["statusEvents"][-1]["reason"] == "close-button"


def test_entry_is_keyboard_reachable_with_a_visible_focus_ring(page, ar_server):
    """`Tab` 能走到入口上、`:focus-visible` 有轮廓、`Enter` 能开、`Esc` 能关。"""
    stub_report(page, report())
    stub_launcher_api(page)
    open_launcher_page(page, ar_server)

    focused = None
    for _ in range(40):
        page.keyboard.press("Tab")
        focused = page.evaluate(
            "() => { const el = document.activeElement;"
            " return el && el.dataset ? el.dataset.role : null; }"
        )
        if focused == "status-toggle":
            break
    assert focused == "status-toggle", f"Tab 走不到状态入口（停在 {focused}）"

    outline = page.evaluate(
        "() => { const s = getComputedStyle(document.activeElement);"
        " return { style: s.outlineStyle, width: s.outlineWidth }; }"
    )
    assert outline["style"] == "solid" and outline["width"] not in ("0px", ""), \
        f"键盘聚焦时看不到轮廓：{outline}"

    page.keyboard.press("Enter")
    expect(card(page)).to_be_visible()
    page.wait_for_function("() => globalThis.__arDebug.statusState !== 'loading'", timeout=10000)

    page.keyboard.press("Escape")
    page.wait_for_function("() => globalThis.__arDebug.statusOpen === false", timeout=5000)
    assert page.locator('[data-role="status-card"]').count() == 0


# =================================================== 5. 滚动：面板自己吃掉滚轮

def test_panel_scroll_does_not_leak_to_the_page(page, ar_server):
    """指针停在面板里滚动：面板自己滚，背后正文一动不动（滚到边界也不放给页面）。"""
    stub_report(page, report(files=many_files(60)))
    stub_launcher_api(page)
    open_launcher_page(page, ar_server)
    open_panel(page)

    scroll_area = body(page)
    contain = scroll_area.evaluate("el => getComputedStyle(el).overscrollBehaviorY")
    assert contain == "contain", f"滚动区缺少第二道闸（overscroll-behavior: contain）：{contain}"

    box = scroll_area.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, 420)

    scrolled = scroll_area.evaluate("el => el.scrollTop")
    assert scrolled > 0, "指针在面板里滚，面板自己没滚（滚轮被谁吃了？）"
    assert page.evaluate("() => window.scrollY") == 0, "滚轮穿透到背后正文了"

    # 滚到底之后再滚一次：仍然不许带动正文（题区滚到边界不放给页面的同一条口径）
    page.mouse.wheel(0, 40000)
    assert scroll_area.evaluate("el => el.scrollTop") == pytest.approx(
        scroll_area.evaluate("el => el.scrollHeight - el.clientHeight"), abs=2,
    )
    assert page.evaluate("() => window.scrollY") == 0, "滚到边界后滚轮穿透到正文了"


# =================================================== 6. 启动页 + 真产物

def test_launcher_renders_the_generated_artifact(page, ar_server):
    """启动页的①状态块照常可用，且页面上的数字与 `web/status.json` 逐项一致（不是手写的）。"""
    if not ARTIFACT.exists():
        pytest.skip("web/status.json 还没生成：先在项目根跑 uv run python tools/gen_status.py")
    raw = ARTIFACT.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    page.route(STATUS_ROUTE, lambda route: route.fulfill(
        status=200, content_type="application/json; charset=utf-8", body=raw.decode("utf-8"),
    ))
    stub_launcher_api(page)
    open_launcher_page(page, ar_server)
    open_panel(page)

    # 真产物两套都该是全绿 ⇒ ok；哪套红了就是 error（都算「读到了报告」，不许是 degraded）
    assert card(page).get_attribute("data-state") in ("ok", "error")
    assert payload["generated_at"] in page.locator('[data-role="status-generated-at"]').inner_text()

    for name in ("backend", "web"):
        suite_payload = payload["tests"].get(name)
        row = suite(page, name)
        if not suite_payload or not suite_payload.get("ran"):
            assert row.get_attribute("data-state") == "skip"
            continue
        text = row.inner_text()
        assert str(suite_payload["passed"]) in text, f"{name} 的 passed 与产物不一致：{text}"
        seconds = f"{float(suite_payload['seconds']):.1f}"
        assert seconds in text, f"{name} 的耗时与产物不一致（页面写 {seconds} 秒）：{text}"

    totals = payload["changes"]["totals"]
    totals_text = page.locator('[data-role="status-totals"]').inner_text()
    assert f"{totals['files']} 个文件" in totals_text
    assert f"+{totals['insertions']}" in totals_text
    assert f"−{totals['deletions']}" in totals_text

    rows = page.locator('[data-role="status-file"]')
    assert rows.count() == len(payload["changes"]["files"])
    rendered = page.locator('[data-role="status-body"]').inner_text()
    for row in payload["changes"]["files"]:
        assert row["path"] in rendered, f"{row['path']} 不在页面清单里"


# =================================================== 7. 真服务（:8765）复验

def real_service_gate() -> None:
    """真服务没起就 skip（不是失败）：口径同 `test_reader_features.py` 的两条联调用例。"""
    try:
        with urllib.request.urlopen(f"{REAL_BASE}/api/health", timeout=3) as resp:
            if resp.status != 200:
                pytest.skip(f"真服务 {REAL_BASE} 回了 HTTP {resp.status}，跳过联调用例")
    except (OSError, urllib.error.URLError) as exc:
        pytest.skip(
            f"真服务 {REAL_BASE} 没在跑（{exc}）：先 `uv run ar serve --port 8765`，再跑这条联调用例"
        )


def test_real_service_status_panel_matches_the_artifact_without_a_model_call(page):
    """真服务上点开「状态」：数字与磁盘上的 `web/status.json` 逐项一致，且**不发任何模型调用**。

    与 `test_launcher_renders_the_generated_artifact` 的区别：那条走静态服务 + `page.route`，
    这条打真服务、读真产物（真服务把 `web/` 挂在 `/` 上，所以 `/status.json` 就是 `web/status.json`）。
    🔁 2026-10-15 起入口在**启动页**：所以这里直接开 `{REAL_BASE}/`（不再是 `/?source=…`，
    那条链接现在会 302 到阅读器页，那边没有状态面板）。
    断言「一个模型调用都没发」用**请求清单**兜：只要碰到 `/api/explain` 或 `/api/survey/followup`
    就算失败——这条用例永远不点讲解、也不点问卷追问；启动页自己只发 GET（材料清单 + 问卷状态）。
    """
    if not ARTIFACT.exists():
        pytest.skip("web/status.json 还没生成：先在项目根跑 uv run python tools/gen_status.py")
    real_service_gate()
    payload = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    requests: list[tuple[str, str]] = []
    page.on("request", lambda req: requests.append((req.method, req.url)))

    page.goto(f"{REAL_BASE}/", wait_until="load")
    page.wait_for_function(
        "() => globalThis.__arDebug && globalThis.__arDebug.ready === true", timeout=20000,
    )
    assert dbg(page)["page"] == "launcher", "真服务的 `/` 应该是启动页"
    toggle(page).click()
    page.wait_for_function("() => globalThis.__arDebug.statusState !== 'loading'", timeout=15000)

    assert card(page).get_attribute("data-state") in ("ok", "error")
    assert dbg(page)["statusUrl"] == f"{REAL_BASE}/status.json", "真服务上要取同源的 /status.json"
    assert payload["generated_at"] in page.locator('[data-role="status-generated-at"]').inner_text()

    for name in ("backend", "web"):
        suite_payload = payload["tests"].get(name)
        row = suite(page, name)
        assert suite_payload and suite_payload.get("ran"), f"{name} 在产物里没跑"
        text = row.inner_text()
        assert str(suite_payload["passed"]) in text, f"{name} 的 passed 与产物不一致：{text}"
        assert f"{float(suite_payload['seconds']):.1f}" in text, f"{name} 的耗时与产物不一致：{text}"
        assert suite_payload["summary"] in text, f"{name} 的摘要行与产物不一致：{text}"

    totals = payload["changes"]["totals"]
    totals_text = page.locator('[data-role="status-totals"]').inner_text()
    assert f"{totals['files']} 个文件" in totals_text
    assert f"+{totals['insertions']}" in totals_text and f"−{totals['deletions']}" in totals_text
    assert page.locator('[data-role="status-file"]').count() == len(payload["changes"]["files"])

    # 关闭：问卷也开着时 Esc 归问卷（那是它的「跳过」，会写画像），所以这里改用 ×——
    # 本用例绝不碰问卷的提交/跳过路径；问卷没开时按正常口径用 Esc。
    if dbg(page)["surveyOpen"]:
        page.click('[data-role="status-close"]')
    else:
        page.keyboard.press("Escape")
    page.wait_for_function("() => globalThis.__arDebug.statusOpen === false", timeout=5000)
    assert page.locator('[data-role="status-layer"]').count() == 0
    assert page.locator('[data-role="status-card"]').count() == 0

    model_hits = [url for _, url in requests if "/api/explain" in url or "/api/survey/followup" in url]
    assert model_hits == [], f"这条用例不许发模型调用，实际打了：{model_hits}"
    assert [url for method, url in requests if method == "POST"] == [], "这条用例不写任何东西"
    assert dbg(page)["errors"] == [], dbg(page)["errors"]
