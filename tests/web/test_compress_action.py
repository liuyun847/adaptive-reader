"""读中「太啰嗦」当场压缩的无头验证（playwright 驱动本机 Edge；`PLAN-actions-feedback.md` §5.2 / §12）。

覆盖：

1. **标记形态**（DESIGN §5 那一行：「不显示 → 点击后触发局部压缩」）：默认不显示（`opacity: 0` +
   `visibility: hidden`），悬停这一段或键盘聚焦到标记上才现身；
2. **点击 → `POST /api/compress`**（请求体 `{source_id, paragraph, reason: "too_verbose"}`）；
3. **成功**：用返回的 `text` 刷新该段正文、动作标记改「压缩」、悬停依据换成本次压缩的说明、
   横幅写「已压缩，请重跑 ar check」；**只改这一段**；
4. **降级（`ok:false`）/ HTTP 失败 / 夹具模式**：**正文一个字符不动** + 可见横幅 + 可重试；
5. **行为事件**：每次点击都写一条 `kind: compress`（带 `ok` / `reason`），而**埋点自己不触发模型请求**；
6. **不重复付费**：成功那一段的标记转「已压缩」且不可再点；
7. **过期的本地标记要跟着作废**（2026-10-14）：产物那一段换成全新记录之后，
   `profile_refs_filled` 那枚「兜底补齐」徽标（悬停行 / 卡片 / 标记属性三处）一起摘掉，
   引用本身照旧——不许出现「产物已无标记、页面还挂着徽标」。

假后端在 `ar_backend.py`（`page.route` 拦 `/api/*`）：**零真实模型调用**。
"""

from __future__ import annotations

import copy
import re

import pytest
from playwright.sync_api import expect

from ar_backend import COMPRESSED_TEXT, FakeApi, banner_texts, dbg, fixture_doc, has_banner, open_api, open_fixture

# 挑一段当前是 expand 的段落：压缩之后动作标记要从「展开」变成「压缩」，一眼看得出刷新过
PARAGRAPH = "p021"


@pytest.fixture(scope="module")
def doc() -> dict:
    return fixture_doc()


def block_text(page, paragraph_id: str) -> str:
    return page.locator(f'#blk-{paragraph_id} [data-role="block-text"]').inner_text()


def hover_block(page, paragraph_id: str) -> None:
    page.evaluate(
        "(pid) => document.querySelector(`#blk-${pid}`).scrollIntoView({block: 'center'})",
        paragraph_id,
    )
    page.hover(f'#blk-{paragraph_id} [data-role="marker"]')


def click_too_verbose(page, paragraph_id: str) -> None:
    hover_block(page, paragraph_id)
    page.click(f'#blk-{paragraph_id} [data-role="too-verbose"]')


def wait_compress_done(page, state: str = "ok", timeout: int = 15000) -> None:
    page.wait_for_function(
        "(want) => globalThis.__arDebug.compress && globalThis.__arDebug.compress.state === want",
        arg=state, timeout=timeout,
    )


def compress_events(page) -> list:
    records = (dbg(page)["events"] or {}).get("records", [])
    return [item for item in records if item["kind"] == "compress"]


# =================================================== 1. 标记默认不显示

def test_too_verbose_marker_is_hidden_until_the_gutter_is_hovered(page, ar_server):
    open_fixture(page, ar_server)
    marker = page.locator(f'#blk-{PARAGRAPH} [data-role="too-verbose"]')

    assert marker.count() == 1, "段落标记区里要有「太啰嗦」这个入口"
    assert marker.text_content() == "太啰嗦"
    assert marker.get_attribute("data-state") == "idle"
    assert marker.get_attribute("aria-label") and "压缩" in marker.get_attribute("aria-label")
    # 默认不显示（读正文时不许到处冒按钮）
    assert marker.evaluate("(el) => getComputedStyle(el).visibility") == "hidden"
    assert marker.evaluate("(el) => getComputedStyle(el).opacity") == "0"

    hover_block(page, PARAGRAPH)
    expect(marker).to_have_css("visibility", "visible")     # 有 0.12s 过渡，等它落定
    expect(marker).to_have_css("opacity", "1")

    # 键盘可达：聚焦到同一条标记槽里的动作标记上，它也该现身
    page.evaluate(f"() => document.querySelector('#blk-{PARAGRAPH} [data-role=marker]').focus()")
    expect(marker).to_have_css("visibility", "visible")


# =================================================== 2. 成功：刷新正文 + 动作标记 + 横幅

def test_click_compresses_the_paragraph_and_refreshes_everything(page, ar_server):
    api = FakeApi(page)
    open_api(page, ar_server)

    before = block_text(page, PARAGRAPH)
    other_before = block_text(page, "p022")
    assert before != COMPRESSED_TEXT

    click_too_verbose(page, PARAGRAPH)
    wait_compress_done(page, "ok")

    # ① 请求形状（契约 §5.2）
    assert len(api.compress_calls) == 1
    body = api.compress_calls[0]
    assert body["source_id"] == "capital-v1-ch01"
    assert body["paragraph"] == PARAGRAPH
    assert body["reason"] == "too_verbose"

    # ② 正文换成返回的 text（走 md.js 渲染：粗体成了 <strong>）
    after = block_text(page, PARAGRAPH)
    assert "这一段只留骨架" in after and after != before
    assert page.locator(f'#blk-{PARAGRAPH} [data-role="block-text"] strong').count() >= 1

    # ③ 动作标记与悬停依据都刷新了
    marker = page.locator(f'#blk-{PARAGRAPH} [data-role="marker"]')
    assert marker.get_attribute("data-action") == "compress"
    hover_block(page, PARAGRAPH)
    expect(page.locator(f'#blk-{PARAGRAPH} [data-role="tip"]')).to_be_visible()
    assert page.locator(f'#blk-{PARAGRAPH} [data-role="tip-action"]').text_content() == "动作：压缩"
    assert "太啰嗦" in page.locator(f'#blk-{PARAGRAPH} [data-role="tip-note"]').text_content()

    # ④ 横幅写「已压缩，请重跑 ar check」（后端给的 hint 原样展示）
    assert has_banner(page, "已压缩，请重跑 ar check"), banner_texts(page)

    # ⑤ 只改这一段
    assert block_text(page, "p022") == other_before

    # ⑥ 调试面 + 行为事件
    record = dbg(page)["compress"]
    assert record["ok"] is True and record["state"] == "ok" and record["paragraph"] == PARAGRAPH
    assert record["cached"] is False and record["model"] == "fake-model"
    assert record["artifactsUpdated"] is True
    events = compress_events(page)
    assert len(events) == 1 and events[0]["ok"] is True and events[0]["paragraph"] == PARAGRAPH

    # ⑦ 标记转「已压缩」且不可再点（同一段不重复付费）
    button = page.locator(f'#blk-{PARAGRAPH} [data-role="too-verbose"]')
    assert button.get_attribute("data-state") == "done"
    assert button.text_content() == "已压缩"
    assert button.is_disabled()
    page.evaluate(
        "(pid) => document.querySelector(`#blk-${pid} [data-role=too-verbose]`)"
        ".dispatchEvent(new MouseEvent('click', {bubbles: true}))",
        PARAGRAPH,
    )
    page.wait_for_timeout(200)
    assert len(api.compress_calls) == 1, "已压缩的段不该再打一次模型"


def test_success_with_degraded_notes_shows_a_warning_banner(page, ar_server):
    """成功路径上的降级也要看得见（2026-10-13 收口）。

    现场：后端压缩**成功**（`ok:true` + 产物写回），但那条行为事件没记上 ⇒ 成功响应的
    `degraded` 非空。要求：正文照常刷新（压缩结果不受影响）、成功横幅照旧，
    **另外**多一条 warn 横幅把降级说明带出来（不藏在 `__arDebug` 里）。
    """
    api = FakeApi(page, compress={
        "ok": True,
        "paragraph": PARAGRAPH,
        "action": "compress",
        "text": COMPRESSED_TEXT,
        "scope": "paragraph",
        "cached": False,
        "usage": {"requests": 1, "prompt_tokens": 120, "completion_tokens": 40},
        "attempts": [],
        "model": "fake-model",
        "prompt_version": "compress-v3",
        "degraded": [
            "这段已压缩，但这次点击没能写进行为事件流 events.jsonl（不影响压缩结果与产物）：OSError: 只读",
        ],
        "artifacts_updated": True,
        "hint": "已压缩，请重跑 ar check。",
    })
    open_api(page, ar_server)

    before = block_text(page, PARAGRAPH)
    click_too_verbose(page, PARAGRAPH)
    wait_compress_done(page, "ok")

    # ① 正文照常刷新（降级不拦住压缩结果），成功横幅照旧
    after = block_text(page, PARAGRAPH)
    assert "这一段只留骨架" in after and after != before
    assert has_banner(page, "已压缩，请重跑 ar check")
    # ② 后端给的降级说明可见，而且是 warn（降级类不自动消失，不是 info）
    assert has_banner(page, "没能写进行为事件流"), banner_texts(page)
    banner = page.locator('[data-role="banner"]', has_text="没能写进行为事件流")
    assert banner.count() == 1
    assert banner.first.get_attribute("data-state") == "warn"
    # ③ 记录层：压缩本身仍是 ok，degraded 原样带进调试面
    record = dbg(page)["compress"]
    assert record["ok"] is True and record["state"] == "ok"
    assert record["artifactsUpdated"] is True
    assert record["degraded"] and "events.jsonl" in record["degraded"][0]
    # ④ 标记照常转「已压缩」、事件照常记、只发一次压缩请求
    assert page.locator(f'#blk-{PARAGRAPH} [data-role="too-verbose"]').get_attribute("data-state") == "done"
    events = compress_events(page)
    assert len(events) == 1 and events[0]["ok"] is True
    assert len(api.compress_calls) == 1


def test_compress_updates_the_open_card_in_place(page, ar_server):
    """卡片正开着这一段时，动作与依据要就地换成新的（答案与缓存不动）。"""
    api = FakeApi(page)
    open_api(page, ar_server)

    hover_block(page, PARAGRAPH)
    page.click(f'#blk-{PARAGRAPH} [data-role="marker"]')
    expect(page.locator('[data-role="card"]')).to_be_visible()
    assert page.locator('[data-role="card-action"]').text_content() == "展开"

    click_too_verbose(page, PARAGRAPH)
    wait_compress_done(page, "ok")

    assert page.locator('[data-role="card-action"]').text_content() == "压缩"
    assert page.locator('[data-role="card-action"]').get_attribute("data-action") == "compress"
    assert "太啰嗦" in page.locator('[data-role="card-why"]').text_content()
    assert api.calls("/api/explain") == [], "压缩不该顺手打一次讲解"


def test_compress_clears_the_stale_profile_refs_filled_badge(page, ar_server):
    """压缩之后那份「骨架兜底补齐」标记要跟着作废（2026-10-14 修）。

    现场：在线压缩把这一段在产物里换成**全新**的变更记录（`why.profile` 清空、`why.note`
    换成压缩说明），后端那一段于是**没有** `profile_refs_filled` 这个键了；而前端本地重画
    是拿压前那一份 `...previous` 展开的，此前只删了 `deferred` / `figure` / `figure_error` /
    `fact_declarations`——漏了这一个，于是实测「产物已无标记、页面还挂着徽标」。

    这里注入现场（夹具里 p021 没有这个键）：压之前徽标 + 悬停出处 + `data-profile-filled`
    三处都在；压之后三处一起消失，而**引用本身照旧**（口径没变，只摘徽标）。
    """
    doc = copy.deepcopy(fixture_doc())
    row = next(c for c in doc["changes"]["changes"] if c["paragraph"] == PARAGRAPH)
    assert row["why"]["profile"], "现场没造对：p021 本来要有自评引用，才谈得上「弃用一份补齐过的依据」"
    assert "profile_refs_filled" not in row, "夹具本来没有这个键，注入才看得出「压后有没有摘掉」"
    row["profile_refs_filled"] = True

    api = FakeApi(page, doc=doc)
    open_api(page, ar_server)

    # 压之前：悬停那一行有出处说明，标记上留可断言属性，卡片里有徽标
    assert page.locator(f'#blk-{PARAGRAPH} [data-role="tip-profile-filled"]').count() == 1
    assert page.locator(f'#blk-{PARAGRAPH} [data-role="marker"]').get_attribute(
        "data-profile-filled"
    ) == "true"

    click_too_verbose(page, PARAGRAPH)
    wait_compress_done(page, "ok")

    assert len(api.compress_calls) == 1
    # ① 悬停那一行与结构化属性都归零（判据是产物那一段没有这个键了）
    assert page.locator(f'#blk-{PARAGRAPH} [data-role="tip-profile-filled"]').count() == 0, \
        "产物里这一段已经没有 profile_refs_filled 了，页面不许再挂「兜底补齐」出处"
    marker = page.locator(f'#blk-{PARAGRAPH} [data-role="marker"]')
    assert marker.get_attribute("data-profile-filled") == "false"
    tip = page.locator(f'#blk-{PARAGRAPH} [data-role="tip-profile"]')
    assert tip.get_attribute("data-filled") is None, "过期的 data-filled 不许留"
    # ② 引用本身照旧逐条渲染（只摘徽标，不动依据的显示口径）
    assert tip.text_content() == f"依据画像：{row['why']['profile'][0]}"

    # ③ 卡片：徽标消失、引用 chips 仍在；「无自评引用」那句也不许冒出来（这一段确实有引用）
    page.click(f'#blk-{PARAGRAPH} [data-role="marker"]')
    expect(page.locator('[data-role="card"]')).to_be_visible()
    assert page.locator('[data-role="card-profile-filled"]').count() == 0, "过期的保底徽标不许留在卡片里"
    assert page.locator('[data-role="card-profile-chip"]').count() == len(row["why"]["profile"])
    assert page.locator('[data-role="card-profile-empty"]').count() == 0


def test_compress_clears_the_badge_in_an_open_card_without_reordering_it(page, ar_server):
    """卡片**正开着**这一段时也一样：徽标就地摘掉，且「依据画像」那一块不换位置。

    与上一条的区别在时序：那次是压完才开卡片（走 `fillCard`），这一次卡片一直开着
    （走 `renderCardProfile` 的**原地替换**分支）。位置断言防的是「顺便追加到卡片末尾」
    那种改法——它会让压缩后卡片的段落顺序变样。
    """
    doc = copy.deepcopy(fixture_doc())
    row = next(c for c in doc["changes"]["changes"] if c["paragraph"] == PARAGRAPH)
    row["profile_refs_filled"] = True

    FakeApi(page, doc=doc)
    open_api(page, ar_server)

    hover_block(page, PARAGRAPH)
    page.click(f'#blk-{PARAGRAPH} [data-role="marker"]')
    expect(page.locator('[data-role="card"]')).to_be_visible()
    assert page.locator('[data-role="card-profile-filled"]').count() == 1

    before = page.evaluate(
        "() => Array.from(document.querySelector('[data-role=\"card-body\"]').children)"
        ".map((el) => el.dataset.role || el.className)"
    )

    click_too_verbose(page, PARAGRAPH)
    wait_compress_done(page, "ok")

    assert page.locator('[data-role="card-profile-filled"]').count() == 0
    after = page.evaluate(
        "() => Array.from(document.querySelector('[data-role=\"card-body\"]').children)"
        ".map((el) => el.dataset.role || el.className)"
    )
    # 除了「本段申报」那一块可能多出来（这次压缩没有申报 ⇒ 其实也没有），骨架顺序一个字没变
    assert after[: len(before)] == before, f"「依据画像」那一块被挪位了：{before} → {after}"
    assert page.locator('[data-role="card-profile-section"]').count() == 1, "就地替换，不是追加出第二节"


def test_compress_declaration_is_shown_in_the_card_tip_and_record(page, ar_server):
    """``compress-v3`` 的申报通道（§14.1）：压缩后这一段的申报与「已放行」徽标要就地看得见。

    现场：后端按**松口径**（`allow_declared_drops`）放行了一条已申报的缺失 ⇒ 响应带
    `fact_declarations` / `waived` / `fact_anchor_policy`。要求三处可见：悬停那一行（tip）、
    卡片里的「本段申报（事实锚）」+ 放行徽标、以及 `__arDebug.compress` 记录。
    """
    declaration = {"numbers": ["1696"], "note": "举例数据只为给出定性结论"}
    waived_row = {
        "paragraph": PARAGRAPH,
        "kind": "numbers",
        "source": ["1859", "1696"],
        "rewritten": ["1859"],
        "missing": ["1696"],
        "declared": ["1696"],
        "unused": [],
        "detail": "数字：申报 1 项，按 allow_declared_drops 放行",
    }
    api = FakeApi(page, compress={
        "ok": True,
        "paragraph": PARAGRAPH,
        "action": "compress",
        "text": COMPRESSED_TEXT,
        "scope": "paragraph",
        "cached": False,
        "usage": {"requests": 1, "prompt_tokens": 120, "completion_tokens": 40},
        "attempts": [],
        "model": "fake-model",
        "prompt_version": "compress-v3",
        "degraded": ["本段申报放弃了 1 项事实锚，已按读者口径 allow_declared_drops 放行：数字：申报 1 项"],
        "artifacts_updated": True,
        "hint": "已压缩，请重跑 ar check 复核（fidelity 已重置为未校验）；本段申报放弃了 1 项事实锚（已按读者口径 allow_declared_drops 放行）",
        "fact_declarations": declaration,
        "waived": [waived_row],
        "over_declared": [],
        "fact_anchor_policy": "allow_declared_drops",
    })
    open_api(page, ar_server)

    click_too_verbose(page, PARAGRAPH)
    wait_compress_done(page, "ok")

    # ① 悬停那一行：说得出「放弃了哪几项」，并标明已按读者口径放行
    hover_block(page, PARAGRAPH)
    expect(page.locator(f'#blk-{PARAGRAPH} [data-role="tip"]')).to_be_visible()
    tip = page.locator(f'#blk-{PARAGRAPH} [data-role="tip-declared"]')
    assert tip.count() == 1, "压缩后的申报要出现在悬停说明里"
    assert "1696" in tip.text_content() and "已按读者口径放行" in tip.text_content()
    assert tip.get_attribute("data-waived") == "true"
    assert page.locator(f'#blk-{PARAGRAPH} [data-role="marker"]').get_attribute("data-declared") == "true"

    # ② 卡片：「本段申报（事实锚）」+ chip + 放行徽标 + 模型说明
    page.click(f'#blk-{PARAGRAPH} [data-role="marker"]')
    expect(page.locator('[data-role="card"]')).to_be_visible()
    chips = page.locator('[data-role="card-declaration-item"]')
    assert chips.count() == 1 and "1696" in chips.first.text_content()
    badge = page.locator('[data-role="card-declaration-waived"]')
    assert badge.count() == 1 and "已按读者口径放行" in badge.text_content()
    assert "允许申报后丢弃" in badge.text_content(), "徽标里要写明是哪一档口径"
    assert page.locator('[data-role="card-declaration-blocked"]').count() == 0, "放行了就不该说「未放行」"
    assert "举例数据只为给出定性结论" in page.locator('[data-role="card-declaration-note"]').text_content()

    # ③ 横幅与调试面：放行这件事在成功路径上也看得见
    assert has_banner(page, "本段申报放弃了 1 项事实锚"), banner_texts(page)
    record = dbg(page)["compress"]
    assert record["factDeclarations"] == declaration
    assert [row["kind"] for row in record["waived"]] == ["numbers"]
    assert record["factAnchorPolicy"] == "allow_declared_drops"
    assert api.calls("/api/explain") == [], "压缩不该顺手打一次讲解"


def test_compress_without_a_declaration_clears_the_previous_one(page, ar_server):
    """本次压缩没有申报 ⇒ 旧申报不能留（那是上一版的账）：悬停那一行与卡片里那一块都要消失。"""
    FakeApi(page)
    open_api(page, ar_server)

    # 夹具里 p004 本来就带一份申报（原样照抄式的样本）
    assert page.locator('#blk-p004 [data-role="tip-declared"]').count() == 1

    # p004 在正文最前面，滚不到「悬停才现身」的位置，用事件直投（本文件已有同款写法）
    page.evaluate(
        "() => document.querySelector('#blk-p004 [data-role=\"too-verbose\"]')"
        ".dispatchEvent(new MouseEvent('click', {bubbles: true}))"
    )
    wait_compress_done(page, "ok")

    assert page.locator('#blk-p004 [data-role="tip-declared"]').count() == 0, "过期的申报不许留在悬停说明里"
    assert page.locator('#blk-p004 [data-role="marker"]').get_attribute("data-declared") == "false"

    # 卡片在压缩之后才打开：它按**本次**的变更记录画，这一块整段不出现
    page.evaluate("() => document.querySelector('#blk-p004 [data-role=marker]').click()")
    expect(page.locator('[data-role="card"]')).to_be_visible()
    assert page.locator('[data-role="card-declaration"]').count() == 0, "过期的申报不许留在卡片里"


def test_compress_drops_the_figure_that_no_longer_matches_the_text(page, ar_server):
    """配图段被压短之后，正文与那张图不再配套：图就地摘掉并在调试面留痕。"""
    api = FakeApi(page)
    open_api(page, ar_server)

    assert page.locator('#blk-p013 [data-role="figure"]').count() == 1
    click_too_verbose(page, "p013")
    wait_compress_done(page, "ok")

    assert page.locator('#blk-p013 [data-role="figure"]').count() == 0
    record = [f for f in dbg(page)["figures"] if f["paragraph"] == "p013"][0]
    assert record["state"] == "removed"
    assert page.locator('#blk-p013 [data-role="marker"]').get_attribute("data-action") == "compress"
    assert api.calls("/api/explain") == []


# =================================================== 3. 降级 / 失败：正文不动

def test_degraded_compress_keeps_the_text_and_shows_a_banner(page, ar_server):
    api = FakeApi(page, compress={
        "ok": False,
        "reason": "fact_anchor_mismatch",
        "text": "压缩稿把 8 蒲式耳改成了 6 蒲式耳",
        "degraded": ["事实锚对不上：数字 8"],
    })
    open_api(page, ar_server)

    before = block_text(page, PARAGRAPH)
    click_too_verbose(page, PARAGRAPH)
    wait_compress_done(page, "degraded")

    # 正文一个字符都没动（契约 §12 第 12 条：事实锚不过就拒绝写入）
    assert block_text(page, PARAGRAPH) == before
    record = dbg(page)["compress"]
    assert record["ok"] is False and record["reason"] == "fact_anchor_mismatch"
    assert has_banner(page, "压缩降级") and has_banner(page, "fact_anchor_mismatch")
    assert has_banner(page, "数字 8"), "后端的降级说明要原样带出来"
    assert not has_banner(page, "已压缩，请重跑 ar check")

    events = compress_events(page)
    assert len(events) == 1
    assert events[0]["ok"] is False and events[0]["reason"] == "fact_anchor_mismatch"
    # 降级可以重试：标记回到 idle
    assert page.locator(f'#blk-{PARAGRAPH} [data-role="too-verbose"]').get_attribute("data-state") == "idle"
    assert len(api.compress_calls) == 1


def test_http_failure_keeps_the_text_and_is_visible(page, ar_server):
    api = FakeApi(page, compress_status=500)
    open_api(page, ar_server)

    before = block_text(page, PARAGRAPH)
    click_too_verbose(page, PARAGRAPH)
    wait_compress_done(page, "error")

    assert block_text(page, PARAGRAPH) == before
    assert has_banner(page, "压缩请求失败") and has_banner(page, "500")
    assert dbg(page)["compress"]["reason"] == "request_failed"
    events = compress_events(page)
    assert len(events) == 1 and events[0]["ok"] is False and events[0]["reason"] == "request_failed"
    assert page.locator(f'#blk-{PARAGRAPH} [data-role="too-verbose"]').get_attribute("data-state") == "idle"
    assert len(api.compress_calls) == 1


def test_fixture_mode_says_there_is_no_backend_and_keeps_the_text(page, ar_server):
    """夹具模式没有 `/api/compress`：说清楚，不假装压过了。"""
    calls: list = []

    def record(route, request):
        calls.append(request.url)
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/**", record)
    open_fixture(page, ar_server)

    before = block_text(page, PARAGRAPH)
    click_too_verbose(page, PARAGRAPH)
    wait_compress_done(page, "fixture")

    assert block_text(page, PARAGRAPH) == before
    assert has_banner(page, "夹具模式没有压缩后端")
    assert calls == [], f"夹具模式不该发任何 /api 请求：{calls}"
    assert dbg(page)["compress"]["reason"] == "fixture"
    # 事件照样记在本地（夹具模式不发送，见 test_behavior_events.py）
    assert len(compress_events(page)) == 1


# =================================================== 4. 埋点不额外打模型

def test_compress_instrumentation_sends_only_events(page, ar_server):
    api = FakeApi(page)
    open_api(page, ar_server)

    click_too_verbose(page, PARAGRAPH)
    wait_compress_done(page, "ok")

    posts = [item for item in api.requests if item["method"] == "POST"]
    paths = sorted({item["path"] for item in posts})
    assert paths == ["/api/compress"], f"这次点击只该发压缩请求（事件还在缓冲里）：{paths}"
    # 事件里那条 compress 的时间戳/来源都对得上（形状与 §7.1 一致）
    event = compress_events(page)[0]
    assert event["v"] == 1 and event["source_id"] == "capital-v1-ch01"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", event["at"])
