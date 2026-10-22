"""M4 阅读器无头验证（playwright 驱动本机 Edge，夹具模式，不依赖后端）。

覆盖 PLAN-skeleton.md §5 M4 验收点：
  主栏显示改写稿 · 抽屉默认折叠 · 展开后显示**当前段**原文 · 段落标记悬停显示动作与依据 ·
  点「没懂」当场出讲解卡片 · 点外部关闭 · 模型不可用/请求失败时出现**可见**降级文案。

就绪只等页面里的 `globalThis.__arDebug.ready`，不等 sleep；每个测试同时断言页面没有 JS 报错。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
FIXTURE_ID = "capital-v1-ch01"
FIXTURE_DIR = PROJECT / "samples" / FIXTURE_ID


@pytest.fixture(scope="module")
def doc() -> dict:
    return json.loads((FIXTURE_DIR / "doc.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def explain() -> dict:
    return json.loads((FIXTURE_DIR / "explain.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def paragraphs(doc) -> dict:
    return {p["id"]: p["text"] for p in doc["meta"]["paragraphs"]}


@pytest.fixture(scope="module")
def changes(doc) -> dict:
    return {c["paragraph"]: c for c in doc["changes"]["changes"]}


# ------------------------------------------------------------------ 工具

def wait_ready(page, timeout: int = 15000) -> dict:
    """等页面里的显式就绪标志——不许 sleep。"""
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
    url = f"{ar_server}/web/reader.html?fixture={fixture_id}{extra}"
    page.goto(url)
    wait_ready(page)
    return url


def scroll_block_to(page, paragraph_id: str, block: str = "start") -> None:
    page.evaluate(
        "(args) => document.querySelector(args[0]).scrollIntoView({block: args[1]})",
        [f"#blk-{paragraph_id}", block],
    )


def scroll_marker_to_center(page, paragraph_id: str) -> None:
    page.evaluate(
        "(pid) => document.querySelector(`#blk-${pid} [data-role=\"marker\"]`)"
        ".scrollIntoView({block: 'center'})",
        paragraph_id,
    )


def marker(page, paragraph_id: str):
    return page.locator(f'#rewritten .block[data-paragraph="{paragraph_id}"] [data-role="marker"]')


def tip(page, paragraph_id: str):
    return page.locator(f'#rewritten .block[data-paragraph="{paragraph_id}"] [data-role="tip"]')


def drawer_x(page) -> float:
    box = page.locator('[data-role="drawer"]').bounding_box()
    assert box, "抽屉没有布局盒"
    return box["x"]


def wait_drawer_settled(page, open_: bool) -> None:
    """等 CSS 过渡真正走完（读 transform 矩阵，不猜时间）。"""
    page.wait_for_function(
        """(open) => {
            const d = document.querySelector('[data-role="drawer"]');
            const rail = document.querySelector('[data-role="drawer-toggle"]');
            const t = getComputedStyle(d).transform;
            const m41 = (t === 'none') ? 0 : new DOMMatrixReadOnly(t).m41;
            const target = open ? 0 : (d.offsetWidth - rail.offsetWidth);
            return Math.abs(m41 - target) < 0.6;
        }""",
        arg=open_,
    )


def get_attr(locator, name: str) -> str:
    value = locator.get_attribute(name)
    return "" if value is None else value


# --------------------------------------------------------------- 1. 主栏

def test_main_column_renders_rewritten_paragraphs(page, ar_server, ar_errors, doc):
    open_reader(page, ar_server)

    blocks = page.locator("#rewritten .block")
    assert blocks.count() == len(doc["meta"]["paragraphs"]) == 25

    ids = page.eval_on_selector_all(
        "#rewritten .block", "els => els.map(e => e.dataset.paragraph)"
    )
    assert ids == [p["id"] for p in doc["meta"]["paragraphs"]]

    # 每一段都渲染出了东西（分隔线那一块是 <hr>，本来就没有文字）
    assert page.eval_on_selector_all(
        "#rewritten .block",
        "els => els.every(e => e.querySelector('[data-role=block-text]').children.length > 0)",
    )
    empty_blocks = page.eval_on_selector_all(
        "#rewritten .block",
        """els => els
            .filter(e => !e.querySelector('[data-role=block-text]').textContent.trim())
            .map(e => e.dataset.paragraph)""",
    )
    assert empty_blocks == ["p003"], f"空段落只有分隔线那一块才对，实际 {empty_blocks}"
    assert page.eval_on_selector_all(
        "#rewritten .block[data-unrewritten=true]", "els => els.length"
    ) == 0

    # 第一段是标题块（Markdown 的 # 被渲染成标题，不是显示原文里的井号）
    first_md = doc["rewritten"].split("\n\n")[0]
    expected_first = first_md.lstrip("#").strip()
    assert page.locator('#blk-p001 [data-role="block-text"] h2').text_content() == expected_first
    assert "#" not in expected_first

    # 分隔线与结论表都渲染出来了（改写了形态而不是纯文本堆叠）
    assert page.locator("#rewritten hr.rule").count() >= 1
    assert page.locator("#rewritten table").count() == 1
    assert page.locator("#rewritten table tbody tr").count() == 4

    assert dbg(page)["errors"] == []
    assert [e for e in ar_errors if "favicon" not in e] == []


# --------------------------------------------------------- 2. 抽屉默认折叠

def test_drawer_is_collapsed_by_default(page, ar_server):
    open_reader(page, ar_server)
    viewport = page.viewport_size["width"]

    assert page.locator("body").get_attribute("data-drawer") == "collapsed"
    assert page.locator('[data-role="drawer"]').get_attribute("data-state") == "collapsed"
    assert dbg(page)["drawerOpen"] is False

    # 只露出右侧那条窄边：抽屉的左边框几乎贴着视口右边
    x = drawer_x(page)
    assert x > viewport - 60, f"抽屉没折叠（x={x}, viewport={viewport}）"

    # 原文段落整体在视口外——默认真的看不到原文
    first_source = page.locator('[data-role="src-para"]').first.bounding_box()
    assert first_source["x"] >= viewport - 1

    assert page.locator('[data-role="drawer-toggle"]').is_visible()


def test_reader_brand_links_back_to_launcher(page, ar_server):
    """顶栏品牌名 = 回启动页的链接（2026-10-20 新增，此前阅读器页没有任何出口）。

    只在这里验「是个指向 `/` 的链接 + 长得不像默认蓝链接」；点下去的落点由真服务那条路
    （`GET /` → 启动页）看着，本目录的静态服务根目录没有 index.html，点了只会看到目录列表。
    """
    open_reader(page, ar_server)

    brand = page.locator("a.topbar__brand")
    assert brand.text_content() == "自适应阅读器"
    assert brand.get_attribute("href") == "/"
    assert "回到启动页" in (brand.get_attribute("title") or "")

    # 链接默认色/下划线被收掉：颜色跟顶栏（--panel-ink）一致，且不带下划线
    styles = brand.evaluate(
        "el => { const s = getComputedStyle(el);"
        " return {color: s.color, deco: s.textDecorationLine,"
        " topbar: getComputedStyle(el.closest('.topbar')).color}; }",
    )
    assert styles["deco"] == "none"
    assert styles["color"] == styles["topbar"], "品牌名还是浏览器默认的链接蓝"

    # 悬停（或键盘聚焦）有反馈——不然没人知道它能点
    brand.hover()
    assert brand.evaluate("el => getComputedStyle(el).color") != styles["color"]


def test_topbar_badges_are_not_covered_by_the_drawer(page, ar_server):
    """「夹具模式」这类徽标必须一直看得见——抽屉不许把它盖住（回归：曾经被盖住过）。"""
    open_reader(page, ar_server)

    def assert_badges_left_of_drawer():
        edge = drawer_x(page)
        for role in ("source-id", "mode-badge", "progress"):
            box = page.locator(f'[data-role="{role}"]').bounding_box()
            assert box, role
            assert box["x"] + box["width"] <= edge + 1, f"{role} 被抽屉盖住了"

    assert_badges_left_of_drawer()
    assert page.locator('[data-role="mode-badge"]').text_content() == "夹具模式"

    page.click('[data-role="drawer-toggle"]')
    wait_drawer_settled(page, True)
    assert_badges_left_of_drawer()


# ----------------------------------------- 3. 展开抽屉看到当前段的原文

def test_drawer_opens_and_shows_current_paragraph_source(page, ar_server, paragraphs):
    open_reader(page, ar_server)
    viewport = page.viewport_size["width"]

    scroll_block_to(page, "p005")
    page.wait_for_function(
        "() => globalThis.__arDebug.currentParagraph !== null", timeout=5000,
    )

    # 「当前段」的独立核对：它必须是最后一个「顶端越过阅读线（视口 35%）」的段
    geometry = page.evaluate(
        """() => {
            const line = window.innerHeight * 0.35;
            return [...document.querySelectorAll('#rewritten .block')].map((n) => ({
                id: n.dataset.paragraph,
                top: n.getBoundingClientRect().top,
            }));
        }""",
    )
    crossing = [g["id"] for g in geometry if g["top"] <= page.viewport_size["height"] * 0.35]
    current = dbg(page)["currentParagraph"]
    assert current == crossing[-1], f"当前段 {current} 与阅读线规则不符（{crossing[-3:]}）"
    assert int(current[1:]) >= 5, f"滚到 p005 之后当前段却是 {current}"

    page.click('[data-role="drawer-toggle"]')
    wait_drawer_settled(page, True)

    assert page.locator("body").get_attribute("data-drawer") == "open"
    assert dbg(page)["drawerOpen"] is True
    assert drawer_x(page) < viewport - 300, "抽屉没有滑出来"

    # 抽屉里高亮的原文 = 当前段，文字与 doc.json 的 meta 一字不差
    current_nodes = page.eval_on_selector_all(
        '.src-para[data-current="true"]', "els => els.map(e => e.dataset.paragraph)"
    )
    assert current_nodes == [current]
    shown = page.locator('.src-para[data-current="true"] [data-role="src-para-text"]').text_content()
    assert shown == paragraphs[current]
    assert page.locator('[data-role="drawer-current"]').text_content() == f"当前段 {current}"

    # 主栏同一段也被标成当前段
    assert "is-current" in page.locator(f"#blk-{current}").get_attribute("class")


# ------------------------------------------------- 4. 抽屉跟随当前段滚动

def test_drawer_follows_current_paragraph_on_scroll(page, ar_server, paragraphs):
    open_reader(page, ar_server)
    page.click('[data-role="drawer-toggle"]')
    wait_drawer_settled(page, True)

    scroll_block_to(page, "p005")
    page.wait_for_function(
        "() => globalThis.__arDebug.currentParagraph !== null", timeout=5000,
    )
    first = dbg(page)["currentParagraph"]
    assert int(first[1:]) >= 5
    before_scroll_top = page.eval_on_selector('[data-role="drawer-list"]', "el => el.scrollTop")

    scroll_block_to(page, "p018")
    page.wait_for_function(
        "(pid) => globalThis.__arDebug.currentParagraph !== pid", arg=first, timeout=5000,
    )
    current = dbg(page)["currentParagraph"]
    assert int(current[1:]) >= 18, f"滚动到 p018 后当前段是 {current}，太靠前"

    # 高亮的原文跟着换成了新的当前段，且文字对得上
    current_nodes = page.eval_on_selector_all(
        '.src-para[data-current="true"]', "els => els.map(e => e.dataset.paragraph)"
    )
    assert current_nodes == [current]
    assert page.locator('.src-para[data-current="true"] [data-role="src-para-text"]').text_content() \
        == paragraphs[current]

    after_scroll_top = page.eval_on_selector('[data-role="drawer-list"]', "el => el.scrollTop")
    assert after_scroll_top > before_scroll_top, "抽屉列表没有跟着滚"
    assert page.locator("#blk-p005").get_attribute("class").find("is-current") < 0


# -------------------------------------- 5. 段落标记悬停：动作 + 依据摘要

def test_marker_tooltip_shows_action_and_why(page, ar_server, changes):
    open_reader(page, ar_server)

    scroll_marker_to_center(page, "p005")
    marker(page, "p005").hover()
    expect(tip(page, "p005")).to_be_visible()

    assert get_attr(marker(page, "p005"), "data-action") == changes["p005"]["action"] == "expand"
    assert page.locator('#blk-p005 [data-role="tip-action"]').text_content() == "动作：展开"
    assert page.locator('#blk-p005 [data-role="tip-note"]').text_content() \
        == changes["p005"]["why"]["note"]
    assert page.locator('#blk-p005 [data-role="tip-profile"]').text_content() \
        == f'依据画像：{changes["p005"]["why"]["profile"][0]}'

    # 悬停文案与 changes.json 逐字一致（多段落交叉核对）
    for pid in ("p008", "p024"):
        scroll_marker_to_center(page, pid)
        marker(page, pid).hover()
        expect(tip(page, pid)).to_be_visible()
        assert get_attr(marker(page, pid), "data-action") == changes[pid]["action"]
        assert page.locator(f'#blk-{pid} [data-role="tip-note"]').text_content() \
            == changes[pid]["why"]["note"]

    # 2026-10-13 起 compress / illustrate 都实现了：夹具里不再有「未实现」那一行
    assert changes["p024"]["action"] == "illustrate"
    assert "deferred" not in changes["p024"]
    assert page.locator('#blk-p024 [data-role="tip-deferred"]').count() == 0
    # 配图段的图由前端就地插在正文之后（渲染细节见 test_figures.py）
    assert page.locator('#blk-p024 [data-role="figure"]').count() == 1


def test_deferred_note_still_shows_for_a_future_unimplemented_action(
    page, ar_server, ar_inject, doc, changes,
):
    """`deferred`（枚举内、本期未实现）机制仍在 schema 里：真出现时必须显式说明，不许装作做了。

    2026-10-13 之后 compress / illustrate 都实现了，夹具里已经没有这种段，
    所以这里注入一份「将来新增了枚举成员但还没实现」的变更记录来守这一行显示。
    """
    payload = copy.deepcopy(doc)
    for change in payload["changes"]["changes"]:
        if change["paragraph"] == "p017":
            change["action"] = "keep"
            change["deferred"] = True
            change["why"]["note"] = "将来某个新动作：本期未实现"
    ar_inject.doc_payload = payload

    open_reader(page, ar_server)
    scroll_marker_to_center(page, "p017")
    marker(page, "p017").hover()
    expect(tip(page, "p017")).to_be_visible()

    assert page.locator('#blk-p017 [data-role="tip-note"]').text_content() == "将来某个新动作：本期未实现"
    deferred_text = page.locator('#blk-p017 [data-role="tip-deferred"]').text_content()
    assert "未实现" in deferred_text and "保持" in deferred_text

    marker(page, "p017").click()
    expect(page.locator('[data-role="card"]')).to_be_visible()
    assert "未实现" in page.locator('[data-role="card-deferred"]').text_content()
    assert changes["p017"].get("deferred") is None, "夹具本身不该再有 deferred（这条是注入的现场）"


# ------------------------------- 6. 点标记出卡片；点外部关闭；不改正文

def test_marker_click_opens_card_and_outside_click_closes(page, ar_server, changes, ar_errors):
    open_reader(page, ar_server)
    article_before = page.locator("#rewritten").text_content()

    scroll_marker_to_center(page, "p005")
    marker(page, "p005").click()

    card = page.locator('[data-role="card"]')
    expect(card).to_be_visible()
    assert dbg(page)["cardOpen"] is True
    assert page.locator('[data-role="card-paragraph"]').text_content() == "p005"
    assert page.locator('[data-role="card-action"]').text_content() == "展开"
    assert page.locator('[data-role="card-why"]').text_content() == changes["p005"]["why"]["note"]
    assert page.locator('[data-role="card-status"]').get_attribute("data-state") == "empty"

    # 点外部关闭
    page.mouse.click(20, 200)
    expect(card).to_be_hidden()
    debug = dbg(page)
    assert debug["cardOpen"] is False
    assert debug["cardCloses"][-1]["reason"] == "outside"

    # 卡片全程不改动正文
    assert page.locator("#rewritten").text_content() == article_before
    assert [e for e in ar_errors if "favicon" not in e] == []


# ------------------------------------------- 7. 点「没懂」当场出讲解

def test_meidong_generates_explanation_in_card(page, ar_server, explain):
    open_reader(page, ar_server)
    article_before = page.locator("#rewritten").text_content()

    scroll_marker_to_center(page, "p022")
    marker(page, "p022").click()
    expect(page.locator('[data-role="card"]')).to_be_visible()
    page.click('[data-role="explain"][data-kind="explain"]')

    status = page.locator('[data-role="card-status"]')
    expect(status).to_have_attribute("data-state", "ok")
    assert page.locator('[data-role="card-answer"]').text_content() == explain["text"]
    assert "没懂" in page.locator('[data-role="explain"][data-kind="explain"]').text_content()

    debug = dbg(page)
    assert debug["lastCard"]["state"] == "ok"
    assert debug["lastCard"]["paragraph"] == "p022"
    assert debug["lastCard"]["text"] == explain["text"]
    assert debug["cards"][-1]["state"] == "ok"

    # 夹具只有一份讲解，请求其它段时要显式说明它不是本段自己的，不许冒充
    page.mouse.click(20, 200)
    expect(page.locator('[data-role="card"]')).to_be_hidden()
    scroll_marker_to_center(page, "p005")
    marker(page, "p005").click()
    page.click('[data-role="explain"][data-kind="simpler"]')
    expect(status).to_have_attribute("data-state", "ok")
    assert "夹具模式" in status.text_content() and "p022" in status.text_content()
    assert dbg(page)["lastCard"]["kind"] == "simpler"

    assert page.locator("#rewritten").text_content() == article_before


# --------------------------- 8. 请求失败 / 模型不可用：可见降级文案

def test_explain_http_failure_shows_visible_degradation(page, ar_server, ar_inject):
    open_reader(page, ar_server)
    ar_inject.fail_explain = True

    scroll_marker_to_center(page, "p005")
    marker(page, "p005").click()
    page.click('[data-role="explain"][data-kind="explain"]')

    status = page.locator('[data-role="card-status"]')
    expect(status).to_have_attribute("data-state", "error")
    text = status.text_content()
    assert text.strip(), "卡片位置不许静默空白"
    assert "讲解生成失败" in text and "500" in text

    debug = dbg(page)
    assert debug["lastCard"]["state"] == "error"
    assert debug["lastCard"]["errorKind"] == "all-failed"
    assert any(d["kind"] == "explain-error" for d in debug["degradations"])

    # 页面上也留了横幅，不只在卡片里
    banner = page.locator('[data-role="banner"]')
    assert banner.count() >= 1
    assert any("讲解请求失败" in banner.nth(i).text_content() for i in range(banner.count()))


def test_explain_degraded_payload_shows_visible_degradation(page, ar_server, ar_inject):
    open_reader(page, ar_server)
    ar_inject.explain_payload = {"text": "", "degraded": True}

    scroll_marker_to_center(page, "p007")
    marker(page, "p007").click()
    page.click('[data-role="explain"][data-kind="explain"]')

    status = page.locator('[data-role="card-status"]')
    expect(status).to_have_attribute("data-state", "degraded")
    text = status.text_content()
    assert "降级显示" in text and "没有生成" in text
    assert dbg(page)["lastCard"]["state"] == "degraded"


# --------------------------------------- 9. 压缩提示：读中不常驻，读完才出

def test_compression_notice_appears_only_after_reading_to_the_end(page, ar_server):
    open_reader(page, ar_server)

    notice = page.locator('[data-role="compression"]')
    expect(notice).to_be_hidden()
    stats = dbg(page)["compression"]
    assert stats["shown"] is False

    # 2026-10-08：读完提示从「底部浮窗」改成正文末尾的**流内块**——
    # 必须在 #rewritten 之外、在「— 改写稿结束 —」之后，且不能是 position: fixed
    assert page.eval_on_selector(
        '[data-role="compression"]', "el => getComputedStyle(el).position"
    ) == "static", "读完提示不许是浮窗"
    assert page.evaluate(
        """() => {
            const notice = document.querySelector('[data-role="compression"]');
            const end = document.querySelector('[data-role="end-mark"]');
            return !document.querySelector('#rewritten').contains(notice)
                && Boolean(end.compareDocumentPosition(notice) & Node.DOCUMENT_POSITION_FOLLOWING);
        }""",
    ), "读完提示要在「— 改写稿结束 —」之后、#rewritten 之外"

    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    expect(notice).to_be_visible()

    text = notice.text_content()
    stats = dbg(page)["compression"]
    assert stats["shown"] is True
    assert stats["direction"] == "shorter"
    assert 30 <= stats["pct"] <= 60, stats
    assert "比原文短" in text and f"{stats['pct']}%" in text
    assert stats["rwChars"] < stats["srcChars"]


# --------------------------- 10. 缺改写稿的段：显示原文并标「未改写」

def test_missing_rewritten_blocks_fall_back_to_source_with_visible_mark(
    page, ar_server, ar_inject, doc, paragraphs,
):
    payload = copy.deepcopy(doc)
    blocks = [b for b in payload["rewritten"].split("\n\n") if b.strip()]
    payload["rewritten"] = "\n\n".join(blocks[:12])
    ar_inject.doc_payload = payload

    open_reader(page, ar_server)
    debug = dbg(page)

    assert debug["unrewritten"] == [f"p{i:03d}" for i in range(13, 26)]
    assert debug["rewrittenParagraphs"] == [f"p{i:03d}" for i in range(1, 13)]

    fallback = page.locator('#blk-p020')
    assert fallback.get_attribute("data-unrewritten") == "true"
    assert page.locator('#blk-p020 [data-role="unrewritten-tag"]').is_visible()
    assert "未改写" in page.locator('#blk-p020 [data-role="unrewritten-tag"]').text_content()
    assert page.locator('#blk-p020 [data-role="block-text"] p').text_content() == paragraphs["p020"]

    assert page.locator('#blk-p012').get_attribute("data-unrewritten") == "false"
    assert any(d["kind"] == "rewritten-missing" for d in debug["degradations"])


def test_missing_concepts_degrade_visibly(page, ar_server, ar_inject, doc):
    payload = copy.deepcopy(doc)
    payload["concepts"] = {"concepts": [], "illustration_candidates": []}
    ar_inject.doc_payload = payload

    open_reader(page, ar_server)
    assert any(d["kind"] == "no-concepts" for d in dbg(page)["degradations"])

    banners = page.locator('[data-role="banner"]')
    assert any("无画像" in banners.nth(i).text_content() for i in range(banners.count()))

    scroll_marker_to_center(page, "p005")
    marker(page, "p005").click()
    expect(page.locator('[data-role="card"]')).to_be_visible()
    assert page.locator('[data-role="card-concept"]').count() == 0
    # 卡片照常给出动作与依据，只是没有概念区
    assert page.locator('[data-role="card-why"]').text_content().strip()


def test_backend_degraded_list_is_shown_and_no_rewrite_falls_back(
    page, ar_server, ar_inject, doc, paragraphs,
):
    """后端把降级原因放在 doc.degraded（字符串数组）里；前端必须照登，不能吞掉。"""
    payload = copy.deepcopy(doc)
    payload["rewritten"] = ""          # 例：rewritten.md 还没生成
    payload["degraded"] = [
        "rewritten.md 不存在：改写（M3）尚未实现，主栏应显示原文并标「未改写」",
        "concepts.json 不存在：概念清单未生成或抽取失败，当前为「无画像」模式",
    ]
    ar_inject.doc_payload = payload

    open_reader(page, ar_server)
    debug = dbg(page)

    banners = page.locator('[data-role="banner"]')
    texts = [banners.nth(i).text_content() for i in range(banners.count())]
    assert any("后端降级" in t and "rewritten.md 不存在" in t for t in texts), texts
    assert any("概念清单" in t for t in texts), texts
    assert sum(1 for d in debug["degradations"] if d["kind"] == "doc-degraded") == 2

    # 没有改写稿 → 25 段全部显示原文并标「未改写」
    assert debug["unrewritten"] == [f"p{i:03d}" for i in range(1, 26)]
    assert page.locator('#blk-p003 [data-role="unrewritten-tag"]').is_visible()
    assert page.locator('#blk-p003 [data-role="block-text"] p').text_content() == paragraphs["p003"]


# ------------------------------ 11. 载入失败 / 没给材料：也要可见、不能挂住

def test_doc_load_failure_is_visible_and_page_still_ready(page, ar_server, ar_inject):
    ar_inject.fail_doc = True
    page.goto(f"{ar_server}/web/reader.html?fixture={FIXTURE_ID}")
    debug = wait_ready(page)

    assert debug["fatal"] and "材料载入失败" in debug["fatal"]
    banner = page.locator('[data-role="banner"][data-state="error"]')
    expect(banner.first).to_be_visible()
    assert "载入失败" in page.locator('[data-role="mode-badge"]').text_content()
    assert page.locator("#rewritten .block").count() == 0


def test_doc_load_failure_shows_the_backend_error_text(page, ar_server):
    """正式模式载入失败：把后端 `error`（与 `hint`）原文显示出来，不是干巴巴的「HTTP 404」。

    后端 `/api/doc/<id>` 的 404 / 500 一律是 JSON + 可读 `error`（契约），启动页那边早就把它
    原样显示给读者了；这里对齐同一口径。
    """
    def fulfill(route):
        route.fulfill(status=404, content_type="application/json; charset=utf-8",
                      body=json.dumps({
                          "error": "找不到材料 nope：source.meta.json 不存在",
                          "hint": "先运行 `uv run ar ingest <文件> --id nope`",
                          "degraded": ["材料 nope 尚未摄取"],
                      }, ensure_ascii=False))

    page.route("**/api/doc/**", fulfill)
    page.goto(f"{ar_server}/web/reader.html?source=nope")
    debug = wait_ready(page)

    assert debug["fatal"] and "材料载入失败" in debug["fatal"]
    assert "找不到材料 nope" in debug["fatal"], debug["fatal"]
    assert "先运行" in debug["fatal"], debug["fatal"]
    banner = page.locator('[data-role="banner"][data-state="error"]')
    expect(banner.first).to_be_visible()
    assert "找不到材料 nope" in banner.first.inner_text()


def test_api_mode_without_source_id_says_what_is_missing(page, ar_server):
    """`/reader` 不带参数（= 静态站点上的 `web/reader.html`）：给可读提示，不白屏、不崩。"""
    page.goto(f"{ar_server}/web/reader.html")
    debug = wait_ready(page)

    assert debug["mode"] == "api"
    assert debug["fatal"] and "source" in debug["fatal"]
    banner = page.locator('[data-role="banner"][data-state="error"]')
    expect(banner.first).to_be_visible()


def test_fixture_mode_banner_declares_it_is_not_the_model(page, ar_server):
    open_reader(page, ar_server)
    banners = page.locator('[data-role="banner"]')
    texts = [banners.nth(i).text_content() for i in range(banners.count())]
    assert any("夹具模式" in t and "不是模型实时产出" in t for t in texts)
    assert dbg(page)["mode"] == "fixture"
