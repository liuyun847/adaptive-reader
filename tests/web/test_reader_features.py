"""2026-10-08 前端改造的无头验证（playwright 驱动本机 Edge，夹具模式 + 真服务各占一部分）。

覆盖四件事：
  ① 讲解粒度：选中文字 → 浮条 → 卡片标明「选中片段 + 段号 + 片段预览」，片段模式隐藏事实锚 chips；
     跨段选区按锚点那一段讲并在卡片里写明跨了几段；空/纯空白/1 个字符不出浮条；滚动收起浮条；
     抽屉（原文）里的选区也能出浮条。
  ② 阅读显示设置：三档背景 × 两档文字 × 四档字号，落 localStorage，刷新后仍在；
     首访跟随 prefers-color-scheme；渲染前就打好属性（不闪白底）；
     **手动组合出读不清的正文（如深色背景 + 深墨文字）时设置面板里出现可见告警**。
  ③ 读完提示改成正文末尾的流内块（不是 position: fixed），语义与调试面沿用旧口径。
  ④ 会话内讲解缓存 + 在飞去重：同一段连点两次只发一次请求，第二次标「本次会话已生成」；
     缓存键带画像指纹（会话内画像变了就不许命中旧答案）。

真服务部分（文件末尾）两条：
  - **不调模型**的那条默认就跑：它只用 `page.route` 拦下 POST /api/explain，核对请求体；
  - **真调模型**的那条要显式开开关 `AR_E2E_BILLABLE=1`，否则 skip（一次点击 = 一次真实计费调用）；
  两条都要求 `ar serve` 在 `http://127.0.0.1:8765` 上跑着，没起就 skip（不是失败）。

就绪只等 `globalThis.__arDebug.ready`，不等 sleep；每个用例跑完由 conftest 统一断言页面无 JS 报错。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import expect

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
FIXTURE_ID = "capital-v1-ch01"
FIXTURE_DIR = PROJECT / "samples" / FIXTURE_ID
REAL_BASE = "http://127.0.0.1:8765"
# 真调模型的开关：只有它等于 "1" 才跑计费用例（默认不跑，免得别人跑一遍白花钱）
BILLABLE_ENV = "AR_E2E_BILLABLE"


@pytest.fixture(scope="module")
def doc() -> dict:
    return json.loads((FIXTURE_DIR / "doc.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def changes(doc) -> dict:
    return {c["paragraph"]: c for c in doc["changes"]["changes"]}


# ------------------------------------------------------------------ 工具

def wait_ready(page, timeout: int = 15000) -> dict:
    page.wait_for_function(
        "() => globalThis.__arDebug && globalThis.__arDebug.ready === true",
        timeout=timeout,
    )
    debug = page.evaluate("() => globalThis.__arDebug")
    assert debug["jsLoaded"] is True, "模块没加载起来（看 __arDebug.errors）"
    return debug


def dbg(page) -> dict:
    return page.evaluate("() => globalThis.__arDebug")


def open_reader(page, ar_server, fixture_id: str = FIXTURE_ID, extra: str = "") -> str:
    """阅读器页：2026-10-15 拆页后是 `web/reader.html`（`web/index.html` 变成了启动页）。"""
    url = f"{ar_server}/web/reader.html?fixture={fixture_id}{cache_bust(extra)}"
    page.goto(url)
    wait_ready(page)
    return url


def cache_bust(extra: str = "") -> str:
    """避免命中静态文件的 ETag 缓存（改完前端立刻要看到新代码）。"""
    return f"{extra}&cb={int(time.time() * 1000)}"


def scroll_center(page, paragraph_id: str) -> None:
    page.evaluate(
        "(pid) => document.querySelector(`#blk-${pid}`).scrollIntoView({block: 'center'})",
        paragraph_id,
    )


def select_in_block(page, paragraph_id: str, start: int = 0, length: int = 8, source: str = "rewritten") -> None:
    """调试入口选中一段文字（模拟拖拽的像素位置太脆，这里直接造 Range）。"""
    scroll_center(page, paragraph_id)
    page.evaluate(
        "(arg) => globalThis.__arDebug.debugSelect(arg)",
        {"paragraph": paragraph_id, "start": start, "length": length, "from": source},
    )
    wait_bar(page)


def wait_bar(page, timeout: int = 5000):
    bar = page.locator('[data-role="select-bar"]')
    expect(bar).to_be_visible(timeout=timeout)
    return bar


def select_bar_kind(page, kind: str):
    return page.locator(f'[data-role="select-explain"][data-kind="{kind}"]')


def card(page):
    return page.locator('[data-role="card"]')


def wait_explain_request(page, ar_inject, timeout: float = 8.0) -> None:
    """等 explain.json 的那次 GET 真的到过服务端（用服务端计数，不靠猜时间）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(p.endswith("/explain.json") for p in ar_inject.hits):
            return
        page.wait_for_timeout(50)
    raise AssertionError(f"没等到 explain 请求；已收到的路径：{ar_inject.hits}")


def explain_hits(ar_inject) -> int:
    return sum(1 for p in ar_inject.hits if p.endswith("/explain.json"))


def key_prefix(paragraph: str, kind: str) -> str:
    """会话缓存键的前四段（夹具模式第五段=画像指纹，恒为空串）。"""
    return f"{FIXTURE_ID}|{paragraph}|{kind}|"


def real_service_gate() -> None:
    """真服务没起就 skip（不是失败）：末尾两条打的是本机跑着的 `uv run ar serve`。"""
    try:
        with urllib.request.urlopen(f"{REAL_BASE}/api/health", timeout=3) as resp:
            if resp.status != 200:
                pytest.skip(f"真服务 {REAL_BASE} 回了 HTTP {resp.status}，跳过联调用例")
    except (OSError, urllib.error.URLError) as exc:
        pytest.skip(
            f"真服务 {REAL_BASE} 没在跑（{exc}）：先 `uv run ar serve --port 8765`，再跑这两条联调用例"
        )


def require_billable() -> None:
    """真调模型必须显式开开关：不给就会被 skip（默认不产生任何模型费用）。"""
    if os.environ.get(BILLABLE_ENV) != "1":
        pytest.skip(f"默认不跑真调模型（一次点击＝一次真实计费调用）：设 {BILLABLE_ENV}=1 才跑")


# ------------------------------------------------- ① 选中片段 → 浮条 → 卡片

def test_selection_toolbar_appears_and_card_marks_the_fragment(page, ar_server):
    open_reader(page, ar_server)
    article_before = page.locator("#rewritten").text_content()

    select_in_block(page, "p005", start=0, length=10)
    bar = page.locator('[data-role="select-bar"]')
    assert bar.get_attribute("data-state") == "shown"
    assert bar.get_attribute("data-paragraph") == "p005"
    assert bar.get_attribute("data-source") == "rewritten"

    debug = dbg(page)
    assert debug["toolbarOpen"] is True
    assert debug["selection"]["paragraph"] == "p005"
    assert debug["selection"]["chars"] == 10
    assert debug["selection"]["spans"] == 1
    selected_p005 = debug["selection"]["text"]

    # 三个按钮沿用现有文案
    labels = [select_bar_kind(page, k).text_content() for k in ("explain", "simpler", "detail")]
    assert labels == ["没懂", "再简单点", "再详细点"]

    select_bar_kind(page, "explain").click()

    assert dbg(page)["toolbarOpen"] is False, "点了按钮浮条要收起"
    expect(card(page)).to_be_visible()
    assert page.locator('[data-role="card-paragraph"]').text_content() == "p005"
    assert page.locator('[data-role="card-scope"]').is_visible()
    assert "选中片段" in page.locator('[data-role="card-scope"]').text_content()
    pick_meta = page.locator('[data-role="card-pick-meta"]').text_content()
    assert "p005" in pick_meta and "选中片段" in pick_meta
    assert page.locator('[data-role="card-selection-preview"]').text_content() == selected_p005

    last = dbg(page)["lastCard"]
    assert last["scope"] == "selection"
    assert last["selection"] == selected_p005
    assert last["state"] == "ok"

    # 卡片全程不改动正文
    assert page.locator("#rewritten").text_content() == article_before


def test_selection_mode_hides_fact_anchors_but_keeps_concepts(page, ar_server, changes):
    open_reader(page, ar_server)

    # 先确认 p005 整段卡片里本来就有事实锚（否则「隐藏」这条断言是空跑）
    scroll_center(page, "p005")
    page.locator('#blk-p005 [data-role="marker"]').click()
    expect(card(page)).to_be_visible()
    assert changes["p005"]["facts"]["quotes"], "夹具里 p005 应该有一条引文事实锚"
    assert page.locator('[data-role="card-fact"]').count() >= 1
    assert page.locator('[data-role="card-concept"]').count() >= 1
    page.keyboard.press("Escape")
    expect(card(page)).to_be_hidden()

    select_in_block(page, "p005", start=0, length=10)
    select_bar_kind(page, "simpler").click()
    expect(card(page)).to_be_visible()

    assert page.locator('[data-role="card-fact"]').count() == 0, "片段模式不许显示整段口径的事实锚"
    assert page.locator('[data-role="card-concept"]').count() >= 1, "概念 chips 要保留"
    status = page.locator('[data-role="card-status"]').text_content()
    assert "选中片段" in status, status


def test_selection_spanning_two_paragraphs_says_which_one_is_used(page, ar_server):
    open_reader(page, ar_server)
    # 顶栏与横幅栏（`[data-role=banners]`）都是 position: fixed 的浮层，横幅栏会盖住视口顶部一段。
    # 拖拽起点必须落在它们下面，否则鼠标先落到横幅上——选中的会是提示文字，不是正文。
    # （夹具里 `p016` 的配图降级横幅就是要挂着的，所以这里按实测的浮层底边定位，不写死像素。）
    overlay_bottom = page.evaluate(
        """() => {
            const topbar = document.querySelector('.topbar');
            const banners = document.querySelector('[data-role=banners]');
            const topbarBottom = topbar ? topbar.getBoundingClientRect().bottom : 0;
            const bannersBottom = banners ? banners.getBoundingClientRect().bottom : 0;
            return Math.max(topbarBottom, bannersBottom);
        }""",
    )
    page.evaluate(
        """(arg) => {
            window.scrollTo(0, window.scrollY + document.querySelector(`#blk-${arg[0]}`).getBoundingClientRect().top - arg[1]);
        }""",
        ["p006", overlay_bottom + 24],
    )
    page.wait_for_timeout(150)

    boxes = page.evaluate(
        """() => ['p006', 'p007'].map((pid) => {
            const p = document.querySelector(`#blk-${pid} [data-role="block-text"] p`);
            const r = p.getBoundingClientRect();
            return { id: pid, left: r.left, right: r.right, top: r.top, bottom: r.bottom };
        })""",
    )
    first, second = boxes
    viewport_h = page.viewport_size["height"]
    if first["bottom"] + 10 >= viewport_h or second["top"] < 60:
        pytest.skip("视口放不下相邻两段，跨段拖拽在本尺寸下没法定点")

    # 真实鼠标拖拽：从 p006 中间那行拖到 p007 的第二行开头
    x_mid = first["left"] + 30
    page.mouse.move(x_mid, (first["top"] + first["bottom"]) / 2)
    page.mouse.down()
    page.mouse.move(x_mid, first["bottom"] - 2, steps=8)
    page.mouse.move(second["left"] + 30, second["top"] + 34, steps=8)
    page.mouse.up()

    bar = wait_bar(page)
    assert bar.get_attribute("data-paragraph") == "p006", "跨段选区要按锚点所在段讲"
    desc = dbg(page)["selection"]
    assert desc and desc["spans"] >= 2, desc

    select_bar_kind(page, "explain").click()
    expect(card(page)).to_be_visible()
    meta = page.locator('[data-role="card-pick-meta"]').text_content()
    assert "跨" in meta and "p006" in meta, meta
    assert "选区跨" in meta, meta


def test_blank_and_one_char_selection_never_show_the_toolbar(page, ar_server):
    open_reader(page, ar_server)
    bar = page.locator('[data-role="select-bar"]')

    # 空选区
    page.evaluate(
        """() => {
            const n = document.querySelector('#blk-p005 [data-role="block-text"]');
            const sel = getSelection();
            const r = document.createRange();
            r.setStart(n, 0);
            r.collapse(true);
            sel.removeAllRanges();
            sel.addRange(r);
            document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
        }""",
    )
    page.wait_for_timeout(120)
    expect(bar).to_be_hidden()
    assert dbg(page)["toolbarOpen"] is False

    # 纯空白
    page.evaluate(
        """() => {
            const n = document.querySelector('#blk-p005 [data-role="block-text"]');
            const span = document.createElement('span');
            span.textContent = '   \\u3000 ';
            n.appendChild(span);
            const sel = getSelection();
            const r = document.createRange();
            r.selectNodeContents(span);
            sel.removeAllRanges();
            sel.addRange(r);
            document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
        }""",
    )
    page.wait_for_timeout(120)
    expect(bar).to_be_hidden()

    # 只有 1 个字符
    page.evaluate(
        """() => {
            const n = document.querySelector('#blk-p005 [data-role="block-text"]');
            const span = document.createElement('span');
            span.textContent = '商';
            n.appendChild(span);
            const sel = getSelection();
            const r = document.createRange();
            r.selectNodeContents(span);
            sel.removeAllRanges();
            sel.addRange(r);
            document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
        }""",
    )
    page.wait_for_timeout(120)
    expect(bar).to_be_hidden()
    assert dbg(page)["selection"] is None


def test_scroll_and_escape_close_the_toolbar(page, ar_server):
    open_reader(page, ar_server)
    bar = page.locator('[data-role="select-bar"]')

    select_in_block(page, "p005", start=0, length=10)
    page.evaluate("() => window.scrollBy(0, 120)")
    page.wait_for_timeout(150)
    expect(bar).to_be_hidden()
    assert dbg(page)["toolbarOpen"] is False

    select_in_block(page, "p006", start=0, length=10)
    page.keyboard.press("Escape")
    page.wait_for_timeout(120)
    expect(bar).to_be_hidden()


def test_selection_works_in_the_source_drawer(page, ar_server):
    open_reader(page, ar_server)
    page.click('[data-role="drawer-toggle"]')
    page.wait_for_function(
        """() => {
            const d = document.querySelector('[data-role="drawer"]');
            const t = getComputedStyle(d).transform;
            return Math.abs((t === 'none' ? 0 : new DOMMatrixReadOnly(t).m41)) < 0.6;
        }""",
    )

    page.evaluate("() => document.querySelector('.src-para[data-paragraph=\"p009\"]').scrollIntoView({block: 'center'})")
    page.evaluate(
        "(arg) => globalThis.__arDebug.debugSelect(arg)",
        {"paragraph": "p009", "start": 0, "length": 10, "from": "source"},
    )
    bar = wait_bar(page)
    assert bar.get_attribute("data-source") == "source"
    assert bar.get_attribute("data-paragraph") == "p009"

    select_bar_kind(page, "explain").click()
    expect(card(page)).to_be_visible()
    last = dbg(page)["lastCard"]
    assert last["scope"] == "selection"
    assert last["selectionFrom"] == "source", "抽屉里的选区要标 selection_from=source"
    assert page.locator('[data-role="card-pick-meta"]').text_content().find("p009") >= 0


def test_selection_normalization_is_shared_with_the_cache_key(page, ar_server):
    """规范化口径（去首尾空白 / 合并连续空白 / 全角空格转半角）由页面里的同一份实现算。"""
    open_reader(page, ar_server)
    page.evaluate(
        """() => {
            const n = document.querySelector('#blk-p005 [data-role="block-text"]');
            const span = document.createElement('span');
            span.textContent = '  甲\\u3000\\u3000乙   丙  ';
            n.appendChild(span);
            const sel = getSelection();
            const r = document.createRange();
            r.selectNodeContents(span);
            sel.removeAllRanges();
            sel.addRange(r);
            document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
        }""",
    )
    bar = wait_bar(page)
    assert bar.get_attribute("data-selection") == "甲 乙 丙"
    assert dbg(page)["selection"]["text"] == "甲 乙 丙"


# ------------------------------------------------------- ② 阅读显示设置

def test_settings_default_follows_prefers_color_scheme(page, ar_server):
    page.emulate_media(color_scheme="dark")
    open_reader(page, ar_server)

    assert page.locator("body").get_attribute("data-bg") == "dark"
    assert page.locator("body").get_attribute("data-ink") == "light"
    stored = json.loads(page.evaluate("() => localStorage.getItem('ar.settings.v1')"))
    assert stored["bg"] == "dark"
    assert dbg(page)["settings"]["bg"] == "dark"
    page.emulate_media(color_scheme="light")

    page2_url = open_reader(page, ar_server, extra="&cb=second")
    assert page2_url
    # 存档已经写上 dark＝「用户选过」，即使系统报浅色也以存档为准
    assert page.locator("body").get_attribute("data-bg") == "dark"


def test_settings_persist_across_reload(page, ar_server):
    open_reader(page, ar_server)
    assert page.locator("body").get_attribute("data-bg") == "paper"

    page.click('[data-role="settings-toggle"]')
    expect(page.locator('[data-role="settings-panel"]')).to_be_visible()
    assert dbg(page)["settingsPanelOpen"] is True

    page.click('[data-role="settings-bg"][value="dark"]')
    page.click('[data-role="settings-size"][value="19"]')
    expect(page.locator("body")).to_have_attribute("data-bg", "dark")
    expect(page.locator("body")).to_have_attribute("data-ink", "light")
    expect(page.locator("body")).to_have_attribute("data-size", "19")

    article_size = page.eval_on_selector(".article", "el => getComputedStyle(el).fontSize")
    assert article_size == "19px"
    # 抽屉与卡片保持界面字号（只有正文栏跟着 data-size 变）
    drawer_size = page.eval_on_selector(".src-para", "el => getComputedStyle(el).fontSize")
    assert drawer_size == "14.4px", drawer_size

    stored = json.loads(page.evaluate("() => localStorage.getItem('ar.settings.v1')"))
    assert stored == {"bg": "dark", "ink": "light", "size": "19", "inkExplicit": False}

    # 刷新后仍在（而且是在渲染前就打好属性）
    open_reader(page, ar_server, extra="&cb=reload")
    expect(page.locator("body")).to_have_attribute("data-bg", "dark")
    expect(page.locator("body")).to_have_attribute("data-size", "19")
    assert page.eval_on_selector(".article", "el => getComputedStyle(el).fontSize") == "19px"
    assert page.locator("html").get_attribute("data-bg") == "dark"
    assert dbg(page)["settings"] == {"bg": "dark", "ink": "light", "size": "19", "inkExplicit": False}

    # 恢复默认：回到跟随系统的初始状态，存档也写回默认
    page.click('[data-role="settings-toggle"]')
    page.click('[data-role="settings-reset"]')
    expect(page.locator("body")).to_have_attribute("data-bg", "paper")
    expect(page.locator("body")).to_have_attribute("data-size", "16")
    stored = json.loads(page.evaluate("() => localStorage.getItem('ar.settings.v1')"))
    assert stored["bg"] == "paper" and stored["size"] == "16"


def test_settings_options_and_panel_close(page, ar_server):
    open_reader(page, ar_server)
    page.click('[data-role="settings-toggle"]')

    assert page.locator('[data-role="settings-bg"]').count() == 3
    assert page.locator('[data-role="settings-ink"]').count() == 2
    assert page.locator('[data-role="settings-size"]').count() == 4
    size_values = page.eval_on_selector_all(
        '[data-role="settings-size"]', "els => els.map(e => e.value)"
    )
    assert size_values == ["15", "16", "17", "19"]

    # 手动指定文字颜色后跟着背景走的那条不再生效（inkExplicit）
    page.click('[data-role="settings-ink"][value="light"]')
    assert dbg(page)["settings"]["inkExplicit"] is True
    page.click('[data-role="settings-bg"][value="warm"]')
    expect(page.locator("body")).to_have_attribute("data-bg", "warm")
    expect(page.locator("body")).to_have_attribute("data-ink", "light")

    # Esc 关闭设置面板
    page.keyboard.press("Escape")
    expect(page.locator('[data-role="settings-panel"]')).to_be_hidden()
    assert dbg(page)["settingsPanelOpen"] is False


def test_low_contrast_manual_combination_is_flagged_in_settings(page, ar_server):
    """手动组合出读不清的正文时必须说出来（不拦用户，但设置面板里要有可见告警）。

    实测对比度由 `measureInkContrast()` 从**算好的样式**里量（不是把 CSS 取值抄一遍），
    同时落在 `__arDebug.inkContrast` / `inkContrastLow` 上，所以这条断言不靠猜。
    """
    open_reader(page, ar_server)
    page.click('[data-role="settings-toggle"]')
    warn = page.locator('[data-role="settings-warning"]')

    # 默认组合（纸白 + 深墨）读得清：没有告警，实测值在 AA 门槛之上
    expect(warn).to_be_hidden()
    ratio = dbg(page)["inkContrast"]
    assert isinstance(ratio, (int, float)) and ratio >= 4.5, ratio
    assert dbg(page)["inkContrastLow"] is False

    # 手动组合出「深色背景 + 深墨文字」：正文对比度塌到 1.2 上下，告警必须出现
    page.click('[data-role="settings-bg"][value="dark"]')
    page.click('[data-role="settings-ink"][value="ink"]')
    expect(warn).to_be_visible()
    text = warn.text_content()
    assert "读不清" in text and "4.5" in text and "恢复默认" in text, text
    low = dbg(page)["inkContrast"]
    assert isinstance(low, (int, float)) and low < 4.5, low
    assert dbg(page)["inkContrastLow"] is True

    # 反方向也一样：纸白背景 + 浅灰文字
    page.click('[data-role="settings-reset"]')
    expect(warn).to_be_hidden()
    page.click('[data-role="settings-ink"][value="light"]')
    expect(warn).to_be_visible()
    assert dbg(page)["inkContrast"] < 4.5

    # 恢复默认回到「跟随背景」，告警消失、正文重新读得清
    page.click('[data-role="settings-reset"]')
    expect(warn).to_be_hidden()
    expect(page.locator("body")).to_have_attribute("data-bg", "paper")
    expect(page.locator("body")).to_have_attribute("data-ink", "ink")
    assert dbg(page)["inkContrast"] >= 4.5
    assert dbg(page)["inkContrastLow"] is False


# --------------------------------------------- ③ 读完提示＝正文末尾流内块

def test_read_complete_notice_is_a_flow_block_at_the_end(page, ar_server):
    open_reader(page, ar_server)

    notice = page.locator('[data-role="compression"]')
    expect(notice).to_be_hidden()
    assert dbg(page)["compression"]["shown"] is False

    # 结构：在 #rewritten 之外，紧跟「— 改写稿结束 —」（后面只隔一个读完哨兵）
    assert page.evaluate(
        """() => {
            const notice = document.querySelector('[data-role="compression"]');
            const article = document.querySelector('#rewritten');
            const end = document.querySelector('[data-role="end-mark"]');
            const order = [...end.parentElement.children];
            return !article.contains(notice)
                && end.compareDocumentPosition(notice) & Node.DOCUMENT_POSITION_FOLLOWING
                && order.indexOf(notice) - order.indexOf(end) === 2;
        }""",
    ), "读完块必须在 #rewritten 之外、且在「— 改写稿结束 —」之后"

    assert page.eval_on_selector(
        '[data-role="compression"]', "el => getComputedStyle(el).position"
    ) == "static", "读完提示不许是 position: fixed 的浮窗"

    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    expect(notice).to_be_visible()
    assert notice.get_attribute("data-state") == "shown"

    text = notice.text_content()
    stats = dbg(page)["compression"]
    assert stats["shown"] is True
    assert stats["direction"] == "shorter"
    assert 30 <= stats["pct"] <= 60, stats
    assert "比原文短" in text and f"{stats['pct']}%" in text
    assert stats["rwChars"] < stats["srcChars"]
    assert stats["text"] == text


# ------------------------------------- ④ 会话内缓存 + 在飞去重（不重复付费）

def test_repeated_explain_is_served_from_the_session_cache(page, ar_server, ar_inject):
    open_reader(page, ar_server)

    scroll_center(page, "p022")
    page.locator('#blk-p022 [data-role="marker"]').click()
    page.click('[data-role="explain"][data-kind="explain"]')
    expect(page.locator('[data-role="card-status"]')).to_have_attribute("data-state", "ok")
    wait_explain_request(page, ar_inject)
    assert explain_hits(ar_inject) == 1

    # 第二次点同一个按钮：命中会话缓存，不再发请求，并留一个轻标记
    page.click('[data-role="explain"][data-kind="explain"]')
    status = page.locator('[data-role="card-status"]').text_content()
    expect(page.locator('[data-role="card-meta"]')).to_be_visible()
    assert "本次会话已生成" in page.locator('[data-role="card-meta"]').text_content(), \
        f"缓存标记没出来；状态行={status!r} cache={dbg(page)['explainCache']}"
    page.wait_for_timeout(250)
    assert explain_hits(ar_inject) == 1, f"重复请求又打了一次服务端：{ar_inject.hits}"

    cache = dbg(page)["explainCache"]
    assert cache["size"] == 1
    # 第五段是画像指纹；夹具模式没有后端、也没有画像，所以留空（键形如 …|p022|explain||）
    assert cache["keys"][0] == f"{FIXTURE_ID}|p022|explain||", cache["keys"]
    assert page.locator('[data-role="card-answer"]').text_content()


def test_inflight_duplicate_reuses_one_request(page, ar_server, ar_inject):
    open_reader(page, ar_server)
    scroll_center(page, "p022")
    page.locator('#blk-p022 [data-role="marker"]').click()

    # 同一个任务里连点两次：第二次必须复用同一个在飞请求，而不是再发一次。
    # 用 dispatchEvent 而不是 button.click()：按钮在请求期间是 disabled 的，
    # 而 `disabled` 恰好也会挡住原生点击——那是浏览器的防重，不是我们这条链路要验的东西。
    page.evaluate(
        """() => {
            const b = document.querySelector('[data-role="explain"][data-kind="explain"]');
            b.dispatchEvent(new MouseEvent('click', {bubbles: true}));
            b.dispatchEvent(new MouseEvent('click', {bubbles: true}));
        }""",
    )
    expect(page.locator('[data-role="card-status"]')).to_have_attribute("data-state", "ok")
    wait_explain_request(page, ar_inject)
    page.wait_for_timeout(200)

    assert explain_hits(ar_inject) == 1, f"在飞去重失效：{ar_inject.hits}"
    debug = dbg(page)
    assert debug["explainDeduped"] == 1
    assert debug["explainCache"]["size"] == 1
    assert debug["explainCache"]["inflight"] == 0


def test_different_kind_and_selection_have_different_cache_keys(page, ar_server, ar_inject):
    open_reader(page, ar_server)
    scroll_center(page, "p022")
    page.locator('#blk-p022 [data-role="marker"]').click()

    page.click('[data-role="explain"][data-kind="explain"]')
    expect(page.locator('[data-role="card-status"]')).to_have_attribute("data-state", "ok")
    page.click('[data-role="explain"][data-kind="simpler"]')
    expect(page.locator('[data-role="card-status"]')).to_have_attribute("data-state", "ok")
    wait_explain_request(page, ar_inject)
    page.wait_for_timeout(200)

    cache = dbg(page)["explainCache"]
    assert cache["size"] == 2, cache["keys"]
    assert any(k.startswith(key_prefix("p022", "explain")) for k in cache["keys"])
    assert any(k.startswith(key_prefix("p022", "simpler")) for k in cache["keys"])

    # 选中片段的键里带规范化后的选区
    select_in_block(page, "p005", start=0, length=6)
    select_bar_kind(page, "explain").click()
    expect(card(page)).to_be_visible()
    page.wait_for_timeout(300)
    keys = dbg(page)["explainCache"]["keys"]
    assert any(k.startswith(key_prefix("p005", "explain")) and k.split("|")[3] for k in keys), keys


def test_profile_signal_changes_with_the_profile_and_shows_up_in_the_cache_key(page, ar_server):
    """会话缓存键的第五段＝画像指纹：画像内容一变，信号（因而键）就变。

    这里直接调页面里的同一份实现（动态 import 模块），不依赖任何后端；
    夹具模式没有画像 → 信号为空串（键里留空，不请求 /api/profile）。
    """
    open_reader(page, ar_server)
    result = page.evaluate(
        """async () => {
            const api = await import('./js/api.js');
            const sel = await import('./js/selection.js');
            const base = { reader: 'local-default', concepts: {}, preferences: [], overrides: [] };
            const rated = { ...base, overrides: [{ concept: 'c01', state: 'known', at: '2026-10-08T10:00:00' }] };
            return {
                stable: api.profileSignalOf({ profile: base }) === api.profileSignalOf({ profile: base }),
                changes: api.profileSignalOf({ profile: base }) !== api.profileSignalOf({ profile: rated }),
                shape: /^[0-9a-f]{8}$/.test(api.profileSignalOf({ profile: base })),
                keyWithout: sel.cacheKey('s1', 'p005', 'explain', '甲  乙', ''),
                keyWith: sel.cacheKey('s1', 'p005', 'explain', '甲  乙', api.profileSignalOf({ profile: rated })),
                pageSignal: globalThis.__arDebug.profileSignal,
            };
        }""",
    )
    assert result["stable"], "同样的画像内容必须给同样的信号"
    assert result["changes"], "画像内容变了，信号必须变（否则会话缓存会命中旧答案）"
    assert result["shape"], result
    assert result["keyWithout"] == "s1|p005|explain|甲 乙|", result
    assert result["keyWith"].startswith("s1|p005|explain|甲 乙|") and result["keyWith"].split("|")[4], result
    assert result["keyWith"] != result["keyWithout"]
    assert result["pageSignal"] == "", "夹具模式没有画像：信号应为空串"


def test_card_status_stays_visible_under_a_long_answer(page, ar_server, ar_inject):
    """长答案下「后端缓存命中 / 片段被截断」这类状态行也必须一直看得见。

    2026-10-08 复核实测：状态行原来在卡片滚动区末尾，长答案时必须滚卡片才看得见
    （top=1985 vs 内容区 bottom=838）。现在它在底部固定区（.card__foot），
    这条用例断言：内容区确实滚起来了、状态行不在滚动区里、位置不随滚动变化、且在卡片可视范围内。
    """
    ar_inject.explain_payload = {
        "text": "社会必要劳动时间" * 400,     # 长答案：内容区一定会滚
        "degraded": False,
        "scope": "selection",
        "selection_truncated": True,          # 截断提示
        "cached": True,                       # 后端缓存命中提示
    }
    open_reader(page, ar_server)
    select_in_block(page, "p005", start=0, length=10)
    select_bar_kind(page, "explain").click()

    status = page.locator('[data-role="card-status"]')
    expect(status).to_be_visible()
    expect(status).to_have_attribute("data-state", "ok")
    text = status.text_content()
    assert "片段超过 600 字" in text and "后端缓存命中" in text, text

    # 这是真·长答案：内容区确实滚得动，否则下面几条断言等于空跑
    content_metrics = page.eval_on_selector(
        '[data-role="card-content"]', "el => ({scroll: el.scrollHeight, client: el.clientHeight})"
    )
    assert content_metrics["scroll"] > content_metrics["client"] + 50, content_metrics

    # 状态行不在滚动区里，且落在卡片可视范围内
    assert page.evaluate(
        """() => !document.querySelector('[data-role="card-content"]')
            .contains(document.querySelector('[data-role="card-status"]'))"""
    ), "状态行不许待在内容滚动区里"
    card_box = card(page).bounding_box()
    before = status.bounding_box()
    assert before and card_box
    assert before["y"] >= card_box["y"] - 1
    assert before["y"] + before["height"] <= card_box["y"] + card_box["height"] + 1, (before, card_box)

    # 把内容区滚到底：状态行的位置不许动
    page.eval_on_selector('[data-role="card-content"]', "el => { el.scrollTop = el.scrollHeight; }")
    after = status.bounding_box()
    assert after["y"] == before["y"], (before, after)
    expect(status).to_be_visible()


# --------------------------------------------- 真服务联调（?source=，非夹具）
#
# 两条都要求 `ar serve` 在 :8765 上跑着（跑之前先起；没起就 skip，不是失败）：
#   1) 不调模型的那条**默认就跑**：用 page.route 把 POST /api/explain 拦下，只核对请求体；
#   2) 真调模型的那条要 `AR_E2E_BILLABLE=1` 才跑——一次点击＝一次真实计费调用。

def open_real_service(page, extra: str = "") -> None:
    page.goto(f"{REAL_BASE}/?source={FIXTURE_ID}{cache_bust(extra)}", wait_until="domcontentloaded")
    wait_ready(page, timeout=30000)
    assert dbg(page)["mode"] == "api"


def select_p005(page) -> str:
    scroll_center(page, "p005")
    page.evaluate(
        "(arg) => globalThis.__arDebug.debugSelect(arg)",
        {"paragraph": "p005", "start": 0, "length": 10},
    )
    bar = wait_bar(page, timeout=8000)
    assert bar.get_attribute("data-paragraph") == "p005"
    selected = dbg(page)["selection"]["text"]
    assert selected
    return selected


def test_real_service_page_and_explain_payload_without_calling_the_model(page):
    """真服务联调（**不调模型、默认就跑**）：页面加载 + POST 请求体 + 画像指纹取数。

    做法：`page.route` 把 `/api/explain` 拦下直接 abort——请求体照样能抓到，但一个模型调用都不发。
    页面必须把这次失败**可见地**报出来（卡片状态区红字），同时 `/api/profile` 要被请求过。
    """
    real_service_gate()
    requests: list[dict] = []
    seen: list[str] = []

    def capture(route):
        requests.append({"url": route.request.url, "body": route.request.post_data})
        route.abort()

    page.route("**/api/explain", capture)
    page.on("request", lambda r: seen.append(r.url))

    open_real_service(page)
    assert dbg(page)["paragraphs"], "真服务的 /api/doc 没给出段落"
    selected = select_p005(page)

    select_bar_kind(page, "explain").click()
    expect(card(page)).to_be_visible()
    status = page.locator('[data-role="card-status"]')
    # 拦下来的请求必然失败：卡片位置不许静默空白，要写明失败原因
    expect(status).to_have_attribute("data-state", "error", timeout=15000)
    assert "讲解生成失败" in status.text_content(), status.text_content()

    assert requests, "没抓到 /api/explain 的请求"
    body = json.loads(requests[-1]["body"])
    assert body["source_id"] == FIXTURE_ID
    assert body["paragraph"] == "p005"
    assert body["kind"] == "explain"
    assert body["selection"] == selected
    assert body["selection_from"] == "rewritten"

    # 会话缓存键的第五段＝画像指纹：真服务能取到画像，所以这里必须是 8 位十六进制
    signal = dbg(page)["profileSignal"]
    assert re.fullmatch(r"[0-9a-f]{8}", signal or ""), signal
    assert any(u.endswith("/api/profile") for u in seen), "没请求 /api/profile，画像指纹取不到"
    assert dbg(page)["lastCard"]["state"] == "error"


def test_real_service_explain_end_to_end_is_billable(page):
    """真服务 + **真模型**（一次计费调用；默认 skip，`AR_E2E_BILLABLE=1` 才跑）。

    跑法：`$env:AR_E2E_BILLABLE = "1"; python -m pytest tests/web -q -k billable`
    代价：一次 `/api/explain` 真实模型调用（后端缓存命中时不再调模型，状态行会标「后端缓存命中」）。
    """
    real_service_gate()
    require_billable()

    open_real_service(page)
    selected = select_p005(page)

    select_bar_kind(page, "explain").click()
    expect(card(page)).to_be_visible()
    # 真实请求要真的回来（后端此时可能还没实现 selection，那也只是状态行多一句说明，不是错误）
    status = page.locator('[data-role="card-status"]')
    expect(status).to_have_attribute("data-state", "ok", timeout=120000)
    status_text = status.text_content()
    assert "选中片段" in status_text, status_text   # 状态行要如实说明「这一段是按选中片段讲的」

    last = dbg(page)["lastCard"]
    assert last["scope"] == "selection"
    assert last["selection"] == selected
    assert last["state"] == "ok"
    assert last["text"]
