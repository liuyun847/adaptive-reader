"""顶栏「通知」按钮 + 通知中心面板的无头验证（playwright 驱动本机 Edge，2026-10-24）。

用户原话：「顶部弹窗添加失效时间，新建顶栏通知按钮，收起到它」。失效时间那一半在
`tests/web/test_banner_lifecycle.py`（`span.banner__expiry` / `data-expires-at`），
本文件管**另一半**：按钮、未读徽标、面板、历史、重弹。

覆盖的契约（实现：`web/js/notify.js` + `web/js/view.js` 的 `addBanner`）：

1. **入口在两个页面的顶栏里**，`data-role="notify-toggle"`——**不能**叫 `status-toggle`
   （`test_status_panel.py` 断言阅读器页那个 role 的计数为 0），启动页那颗也**不包进**
   `data-role="launcher-block"`（`test_launcher.py` 把启动页的块数钉死在 4）；
2. **面板开关**：点开 → `aria-expanded="true"`，再点 → `false`，关掉后整层从 DOM 移除（不留残影）；
3. **未读徽标**：横幅收起（自动消失 / 手动关闭）后未读数上涨、徽标显形；打开面板即清零（`data-unread="0"` 时 CSS 隐藏）；
4. **`Esc` 关闭且焦点回到入口**；点面板之外也关；
5. **历史**：条目留在面板里（纯内存，刷新即清空）；点条目能把那条横幅**原样重弹一次**
   （颜色 / 文字一致，且不会在历史里翻倍）；
6. **空历史有话说**：「还没有通知。收起的提示会留在这里。」——不留空白；
7. **窄屏顶栏装得下**（2026-10-24 回归，见文件末尾）：≤430px（430 / 390 / 360 实测）顶栏仍是
   一行、`.topbar__side` 不横向溢出、「通知 / 画像 / 显示」三颗按钮都点得到，段号只做 sr-only
   视觉隐藏（文字仍在无障碍树里）。新按钮刚加时 390px 下把「Aa 显示」挤进了抽屉窄边。

本文件复用现成助手：`test_survey_ui.py` 的夹具页 / 调试面（`open_fixture` / `dbg`），
`test_status_panel.py` 的启动页开启与假后端（`open_launcher_page` / `stub_launcher_api`）。
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from test_status_panel import open_launcher_page, stub_launcher_api  # noqa: E402
from test_survey_ui import dbg, open_fixture  # noqa: E402

FIXTURE_HINT = "夹具模式"          # 阅读器页夹具模式那条 warn 横幅的独有片段
EMPTY_TEXT = "还没有通知。收起的提示会留在这里。"


# ------------------------------------------------------------------ 工具

def open_reader(page, ar_server) -> str:
    """夹具模式打开**阅读器页**：加载时就挂一条「夹具模式」warn 横幅，正好拿来做历史。"""
    return open_fixture(page, ar_server, extra="")


def toggle(page):
    return page.locator('[data-role="notify-toggle"]')


def panel(page):
    return page.locator('[data-role="notify-panel"]')


def count_badge(page):
    return page.locator('[data-role="notify-count"]')


def docs(page) -> dict:
    """`__arDebug.notifications` 的快照：`{open, unread, items}`。"""
    return dbg(page)["notifications"]


def banners(page):
    return page.locator('[data-role="banner"]')


def close_banner(page, needle: str = "") -> str:
    """关掉（`×`）页面上第一条（或含 `needle` 的那条）横幅，返回它的 `data-text`。"""
    target = page.locator('[data-role="banner"]', has_text=needle).first if needle else banners(page).first
    text = target.get_attribute("data-text")
    target.locator('[data-role="banner-close"]').click()   # 点击处理是同步的：返回时节点已摘掉
    return text


def wait_open(page) -> None:
    page.wait_for_function("() => globalThis.__arDebug.notifications.open === true", timeout=5000)


def wait_closed(page) -> None:
    page.wait_for_function("() => globalThis.__arDebug.notifications.open === false", timeout=5000)


def open_center(page) -> None:
    toggle(page).click()
    wait_open(page)
    expect(panel(page)).to_be_visible()


# =================================================== 1. 入口在两个页面的顶栏里

def test_entry_is_a_topbar_button_on_both_pages(page, ar_server):
    """阅读器页与启动页顶栏各有一颗「通知」，role 不是 status-toggle、启动页那颗也不在 launcher-block 里。"""
    open_reader(page, ar_server)
    entry = toggle(page)
    expect(entry).to_be_visible()
    assert entry.get_attribute("data-role") == "notify-toggle"
    assert entry.get_attribute("aria-controls") == "notify-panel"
    assert entry.get_attribute("aria-expanded") == "false"
    assert page.locator('[data-role="status-toggle"]').count() == 0, "阅读器页不该有状态入口（role 不许撞车）"
    assert page.evaluate(
        "() => Boolean(document.querySelector('[data-role=\"notify-toggle\"]').closest('.topbar'))"
    ), "通知入口不在阅读器顶栏里"
    assert docs(page)["open"] is False
    assert page.locator('[data-role="notify-layer"]').count() == 0, "不该自动打开面板"

    # 启动页：同样在顶栏，但**不**包进 launcher-block（四块的契约不许被撬动）
    stub_launcher_api(page)
    open_launcher_page(page, ar_server)
    entry = toggle(page)
    expect(entry).to_be_visible()
    assert page.evaluate(
        "() => Boolean(document.querySelector('[data-role=\"notify-toggle\"]').closest('.topbar'))"
    ), "通知入口不在启动页顶栏里"
    assert entry.evaluate(
        "el => Boolean(el.closest('[data-role=\"launcher-block\"]'))"
    ) is False, "通知按钮不许包进 launcher-block（启动页应该是四块）"
    assert page.locator('[data-role="launcher-block"]').count() == 4, "启动页应该是四块"
    assert page.locator('[data-role="notify-host"]').count() == 1, "通知宿主要在页面上（body 层）"


# =================================================== 2. 开关 / 未读 / 收起

def test_collapsed_banner_raises_the_unread_badge_and_opening_clears_it(page, ar_server):
    """关掉一条横幅 → 未读 1、徽标显形；点开面板 → 未读清零、徽标隐藏、历史里能看到那条。"""
    open_reader(page, ar_server)

    # 还挂在页面上的横幅不算未读（它就在读者眼前）
    assert docs(page)["unread"] == 0
    assert count_badge(page).get_attribute("data-unread") == "0"
    expect(count_badge(page)).to_be_hidden()

    text = close_banner(page, FIXTURE_HINT)
    page.wait_for_function("() => globalThis.__arDebug.notifications.unread === 1", timeout=5000)
    expect(count_badge(page)).to_be_visible()
    assert count_badge(page).text_content().strip() == "1"
    assert count_badge(page).get_attribute("data-unread") == "1"

    open_center(page)
    assert toggle(page).get_attribute("aria-expanded") == "true"
    assert docs(page)["open"] is True
    assert docs(page)["unread"] == 0, "打开面板即读：未读要清零"
    expect(count_badge(page)).to_be_hidden()
    assert count_badge(page).get_attribute("data-unread") == "0"

    items = page.locator('[data-role="notify-item"]')
    assert items.count() == 1, "历史里应该正好一条（刚收起的那条）"
    assert text in items.first.inner_text()
    assert items.first.get_attribute("data-state") == "warn"
    assert items.first.get_attribute("data-collapsed-by") == "close"
    foot = page.locator('[data-role="notify-foot"]').inner_text()
    assert "已收起 1 条" in foot, foot

    # 再点一次入口：面板收起来、节点不留在 DOM 里
    toggle(page).click()
    wait_closed(page)
    assert toggle(page).get_attribute("aria-expanded") == "false"
    assert page.locator('[data-role="notify-layer"]').count() == 0
    assert page.locator('#notify-host > *').count() == 0, "宿主里不许留残影"
    assert page.evaluate("() => document.body.dataset.notify") == "closed"


# =================================================== 3. Esc / 点外部 / 焦点

def test_escape_closes_the_panel_and_returns_focus_to_the_entry(page, ar_server):
    """`Esc` 关面板（文档级监听），焦点回到顶栏那颗按钮，`aria-expanded` 跟随。"""
    open_reader(page, ar_server)
    close_banner(page, FIXTURE_HINT)
    open_center(page)

    page.keyboard.press("Escape")
    wait_closed(page)

    assert page.locator('[data-role="notify-panel"]').count() == 0
    assert page.locator('[data-role="notify-layer"]').count() == 0
    assert toggle(page).get_attribute("aria-expanded") == "false"
    assert page.evaluate(
        "() => document.activeElement && document.activeElement.dataset.role"
    ) == "notify-toggle", "关闭后焦点要回到入口"


def test_close_button_and_outside_click_both_close_the_panel(page, ar_server):
    """`×` 与「点面板之外」都能关（两条与画像 / 显示面板同一口径的出口）。"""
    open_reader(page, ar_server)
    close_banner(page, FIXTURE_HINT)

    open_center(page)
    page.click('[data-role="notify-close"]')
    wait_closed(page)
    assert page.locator('[data-role="notify-layer"]').count() == 0

    open_center(page)
    page.locator('[data-role="rewritten"]').click(position={"x": 80, "y": 120})
    wait_closed(page)
    assert page.locator('[data-role="notify-panel"]').count() == 0
    assert page.locator('[data-role="notify-layer"]').count() == 0


# =================================================== 4. 历史：回看与重弹

def test_history_item_replays_the_banner_once_without_duplicating_history(page, ar_server):
    """点历史条目：面板让开、那条横幅原样弹回来（同文字 / 同颜色），历史里不会翻倍。"""
    open_reader(page, ar_server)
    text = close_banner(page, FIXTURE_HINT)
    open_center(page)

    before_items = docs(page)["items"]
    assert len([item for item in before_items if item["collapsed"]]) == 1

    page.locator('[data-role="notify-item"]').first.click()
    wait_closed(page)

    replayed = page.locator('[data-role="banner"]', has_text=FIXTURE_HINT)
    expect(replayed).to_have_count(1)
    assert replayed.first.get_attribute("data-text") == text
    assert replayed.first.get_attribute("data-state") == "warn"
    # 重弹的横幅照旧带失效时间与倒计时（warn 与 info 同一套呈现，20 秒那条线）
    expires_at = int(replayed.first.get_attribute("data-expires-at"))
    delta = expires_at - page.evaluate("() => Date.now()")
    assert 20000 - 2000 <= delta <= 20000 + 2000, f"重弹的 warn 横幅失效时刻差 {delta} ms"
    assert re.search(r"还剩 \d+ 秒", replayed.first.locator('[data-role="banner-expiry"]').inner_text())

    after = docs(page)
    history_ids = [item["id"] for item in after["items"] if item["collapsed"]]
    assert len(history_ids) == 1, "重弹不该在历史里再记一条"
    assert after["unread"] == 0, "重弹的那条还挂在页面上，不算未读"

    # 重弹的横幅是 `record: false`（**没**进 store），所以它被关掉时 `removeBanner` 会拿一个
    # 未知 id 来 `markCollapsed`：这条路径必须是安全的 no-op —— 不抛错（conftest 的 autouse
    # 断言页面没有 JS 报错）、不在历史里凭空多一条、不动未读。
    replayed.first.locator('[data-role="banner-close"]').click()
    expect(replayed).to_have_count(0)
    final = docs(page)
    assert [item["id"] for item in final["items"] if item["collapsed"]] == history_ids
    assert len(final["items"]) == len(after["items"])
    assert final["unread"] == 0

    # 直接问一次 store 的契约：未知 id → null（不抛）
    assert page.evaluate(
        "async () => { const m = await import('/web/js/notify.js');"
        " return m.markCollapsed('这条 id 不存在', 'close'); }"
    ) is None


def test_empty_history_says_so_instead_of_leaving_a_blank(page, ar_server):
    """一条历史都没有时：列表空着，但写着「还没有通知…」（不留空白、不留一条空分隔线）。"""
    open_reader(page, ar_server)      # 横幅都在页面上，还没收起过任何一条
    open_center(page)

    assert page.locator('[data-role="notify-item"]').count() == 0
    empty = page.locator('[data-role="notify-empty"]')
    expect(empty).to_be_visible()
    assert empty.inner_text() == EMPTY_TEXT
    assert page.locator('[data-role="notify-foot"]').inner_text().strip() == ""
    assert docs(page)["items"] and all(not item["collapsed"] for item in docs(page)["items"])


def test_history_lives_in_memory_only(page, ar_server):
    """历史不落 localStorage（只存内存）：刷新之后清空是**预期**，不是缺陷。"""
    open_reader(page, ar_server)
    close_banner(page, FIXTURE_HINT)
    page.wait_for_function("() => globalThis.__arDebug.notifications.unread === 1", timeout=5000)
    keys = page.evaluate(
        "() => { const out = [];"
        " for (let i = 0; i < localStorage.length; i += 1) out.push(localStorage.key(i));"
        " return out; }"
    )
    assert not [key for key in keys if "notify" in key.lower()], f"通知历史不该落 localStorage：{keys}"

    open_reader(page, ar_server)
    # 刷新后：上一轮那条「已收起」的历史没了（新页面只有自己加载时挂上的那几条，都还没收起）
    assert [item for item in docs(page)["items"] if item["collapsed"]] == [], "刷新之后历史清空（纯内存）"
    assert docs(page)["unread"] == 0
    assert count_badge(page).get_attribute("data-unread") == "0"


# =================================================== 5. 启动页同一套

def test_launcher_page_has_the_same_center(page, ar_server):
    """启动页：点开是空历史（写着「还没有通知…」），`Esc` 关掉后焦点回入口。"""
    stub_launcher_api(page)
    open_launcher_page(page, ar_server)

    assert docs(page)["open"] is False
    open_center(page)
    expect(page.locator('[data-role="notify-empty"]')).to_be_visible()
    assert page.locator('[data-role="notify-item"]').count() == 0

    page.keyboard.press("Escape")
    wait_closed(page)
    assert page.locator('[data-role="notify-layer"]').count() == 0
    assert page.evaluate(
        "() => document.activeElement && document.activeElement.dataset.role"
    ) == "notify-toggle"
    assert page.locator('[data-role="launcher-block"]').count() == 4, "面板开关不影响四块"


def test_launcher_collapsed_banner_lands_in_history_too(page, ar_server):
    """启动页的降级横幅收起后同样进历史（两页共用 `addBanner` + `notify.js` 那一份实现）。"""
    open_launcher_page(page, ar_server)          # 不给 /api/* 挂桩：静态服务 404 → 页面挂降级横幅
    expect(banners(page).first).to_be_visible()
    text = banners(page).first.get_attribute("data-text")
    banners(page).first.locator('[data-role="banner-close"]').click()
    page.wait_for_function("() => globalThis.__arDebug.notifications.unread >= 1", timeout=5000)

    open_center(page)
    items = page.locator('[data-role="notify-item"]')
    assert items.count() >= 1
    assert text in items.first.inner_text()


# =================================================== 6. 窄屏顶栏装得下（2026-10-24 回归）

# 三颗按钮（通知 / 画像 / 显示）都得点得到，顶栏还得是一行。430 是 CSS 断点，390 / 360 是真机宽度。
NARROW_WIDTHS = (430, 390, 360)


@pytest.mark.parametrize("width", NARROW_WIDTHS)
def test_topbar_fits_in_one_line_at_narrow_widths(page, ar_server, width):
    """≤430px：`.topbar__side` 不横向溢出、顶栏仍 48px、三颗按钮都点得到、段号只在视觉上隐藏。

    缺陷现场（2026-10-24 本批引入）：新加的「通知」按钮让 390px 下的顶栏内容比可用宽度多 34px
    ——「Aa 显示」被挤进右侧抽屉窄边；同一宽度下徽标还会折成两三行，而行高是写死的 48px。
    修法只在 CSS（`css/reader.css` 的 `@media (max-width: 430px)`）：品牌名与徽标允许省略号
    （先截材料 id），段号做 sr-only——「第 N / M 段 · pNNN」的文字仍留在无障碍树里，读屏照读。
    """
    page.set_viewport_size({"width": width, "height": 900})
    open_reader(page, ar_server)

    metrics = page.evaluate(
        """() => {
            const bar = document.querySelector('.topbar');
            const side = document.querySelector('.topbar__side');
            const progress = document.querySelector('[data-role="progress"]');
            const roles = ['notify-toggle', 'profile-toggle', 'settings-toggle'];
            const hits = {};
            roles.forEach((role) => {
                const el = document.querySelector('[data-role="' + role + '"]');
                const r = el.getBoundingClientRect();
                const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                hits[role] = {
                    w: Math.round(r.width),
                    right: Math.round(r.right),
                    hit: Boolean(hit && (hit === el || el.contains(hit))),
                };
            });
            return {
                sideOverflow: side.scrollWidth - side.clientWidth,
                barHeight: Math.round(bar.getBoundingClientRect().height),
                docOverflow: document.documentElement.scrollWidth - window.innerWidth,
                progressW: Math.round(progress.getBoundingClientRect().width),
                progressText: progress.textContent.trim(),
                hits,
            };
        }"""
    )

    assert metrics["sideOverflow"] <= 0, f"{width}px 顶栏右侧溢出 {metrics['sideOverflow']}px"
    assert metrics["docOverflow"] <= 0, f"{width}px 文档横向滚动 {metrics['docOverflow']}px"
    assert metrics["barHeight"] == 48, f"{width}px 顶栏被内容撑高了：{metrics['barHeight']}px"
    for role, info in metrics["hits"].items():
        assert info["w"] > 0, f"{width}px 下 {role} 被压没了"
        assert info["right"] <= width, f"{width}px 下 {role} 跑出视口（right={info['right']}）"
        assert info["hit"], f"{width}px 下 {role} 点不到（被谁盖住了？）"
    # 段号只是视觉隐藏（sr-only）：文字还在，读屏照样读得到进度
    assert metrics["progressW"] <= 2, f"{width}px 下段号仍占位（{metrics['progressW']}px）"
    assert "段" in metrics["progressText"], metrics["progressText"]

