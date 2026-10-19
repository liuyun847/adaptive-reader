"""事实锚申报的前端可见面（PLAN-actions-feedback §14.1）。

夹具里留了一个申报样本（`samples/capital-v1-ch01/doc.json` 的 p004）：模型申报「本段不再保留数字 1」，
`fidelity.fact_anchor_policy` 是 `allow_declared_drops`，`fidelity.waived` 里把这条放行了。
本文件守四件事：

  ① 卡片里看得见「本段申报（事实锚）」+ 每一条 + 「已按读者口径放行」徽标；
  ② 悬停该段的动作标记也看得见申报（不点开卡片也能发现「这一段丢了锚点」）；
  ③ 口径不放行时（`keep_all`）改挂「未放行」徽标——**同一份申报，两种口径两种结论**；
  ④ 干净段落与片段模式都不显示这一块（申报是整段口径，与事实锚同一取舍）。

零真实模型调用；就绪只等 `__arDebug.ready`，每个用例跑完由 conftest 统一断言页面无 JS 报错。
"""

from __future__ import annotations

import copy

from playwright.sync_api import expect

from ar_backend import FakeApi, dbg, fixture_doc, open_api, open_fixture

PARAGRAPH = "p004"          # 夹具里唯一带 fact_declarations 的段
CLEAN = "p005"              # 没有申报的段
DECLARED_TEXT = "数字「1」"


def card(page):
    return page.locator('[data-role="card"]')


def scroll_center(page, paragraph_id: str) -> None:
    page.evaluate(
        "(pid) => document.querySelector(`#blk-${pid}`).scrollIntoView({block: 'center'})",
        paragraph_id,
    )


def open_card_for(page, paragraph_id: str) -> None:
    scroll_center(page, paragraph_id)
    page.locator(f'#blk-{paragraph_id} [data-role="marker"]').click()
    expect(card(page)).to_be_visible()


def select_in_block(page, paragraph_id: str, start: int = 0, length: int = 8) -> None:
    scroll_center(page, paragraph_id)
    page.evaluate(
        "(arg) => globalThis.__arDebug.debugSelect(arg)",
        {"paragraph": paragraph_id, "start": start, "length": length, "from": "rewritten"},
    )
    expect(page.locator('[data-role="select-bar"]')).to_be_visible()


def doc_with_policy(policy: str) -> dict:
    """夹具副本：只改口径与放行名单（模拟「同一份申报换了读者口径」）。"""
    payload = copy.deepcopy(fixture_doc())
    fidelity = payload["changes"]["fidelity"]
    fidelity["fact_anchor_policy"] = policy
    fidelity["waived"] = [] if policy == "keep_all" else fidelity["waived"]
    return payload


def doc_with_over_declaration() -> dict:
    """夹具副本：这一段**一个锚点都没少**，模型却申报「放弃了数字 1」（多申报，§14.1 收尾）。

    现场形态与后端 ``fidelity.over_declared`` 一致：放行名单空着（没有缺失可放行），
    多申报的条目单列一条。页面要说得出「多申报」，而不是误报成「未放行」。
    """
    payload = copy.deepcopy(fixture_doc())
    fidelity = payload["changes"]["fidelity"]
    fidelity["fact_anchor_policy"] = "allow_declared_drops"
    fidelity["waived"] = []
    fidelity["over_declared"] = [
        {
            "paragraph": PARAGRAPH,
            "kind": "numbers",
            "declared": ["1"],
            "detail": "数字：本段一个都没少（与原文逐项一致），却申报放弃了「1」——多申报，不影响放行结论",
        }
    ]
    return payload


def test_card_shows_the_declaration_and_the_waiver(page, ar_server) -> None:
    open_fixture(page, ar_server)
    # 页面确实读到了校验块（放行标记的唯一依据），而不只是把 changes 里的申报画出来
    assert dbg(page)["fidelity"]["waived"], "夹具里应当有一条已放行的申报"

    open_card_for(page, PARAGRAPH)

    assert page.locator('[data-role="card-declaration"]').count() == 1
    items = page.locator('[data-role="card-declaration-item"]')
    assert items.count() == 1
    assert items.first.text_content() == DECLARED_TEXT
    assert items.first.get_attribute("data-field") == "numbers"
    assert "本段申报（事实锚）" in page.locator('[data-role="card-body"]').text_content()
    note = page.locator('[data-role="card-declaration-note"]').text_content()
    assert note.startswith("模型说明：") and "编号" in note
    waived = page.locator('[data-role="card-declaration-waived"]')
    assert waived.count() == 1
    assert "已按读者口径放行" in waived.text_content()
    assert "要结论" in waived.text_content(), "放行徽标要写明是哪一档口径放的行"
    assert page.locator('[data-role="card-declaration-blocked"]').count() == 0


def test_clean_paragraph_has_no_declaration_block(page, ar_server) -> None:
    open_fixture(page, ar_server)
    open_card_for(page, CLEAN)

    assert page.locator('[data-role="card-declaration"]').count() == 0, "没申报的段不许凭空出现这一块"
    assert page.locator('[data-role="card-declaration-item"]').count() == 0


def test_tooltip_repeats_the_declaration_without_opening_the_card(page, ar_server) -> None:
    open_fixture(page, ar_server)

    tip = page.locator(f'#blk-{PARAGRAPH} [data-role="tip-declared"]')
    assert tip.count() == 1, "悬停说明里要有申报这一行"
    text = tip.text_content()
    assert "事实锚申报" in text and DECLARED_TEXT in text and "已按读者口径放行" in text
    # 干净段没有这一行（不申报就不显示）
    assert page.locator(f'#blk-{CLEAN} [data-role="tip-declared"]').count() == 0
    # 标记上留了可断言的属性
    assert page.locator(f'#blk-{PARAGRAPH} [data-role="marker"]').get_attribute("data-declared") == "true"
    assert page.locator(f'#blk-{CLEAN} [data-role="marker"]').get_attribute("data-declared") == "false"


def test_same_declaration_is_marked_blocked_under_the_strict_policy(page, ar_server) -> None:
    """同一份申报、严口径：卡片改挂「未放行」徽标（两种口径两种结论，都在页面上看得见）。"""
    FakeApi(page, doc=doc_with_policy("keep_all"))
    open_api(page, ar_server)

    assert dbg(page)["fidelity"]["fact_anchor_policy"] == "keep_all"
    open_card_for(page, PARAGRAPH)

    assert page.locator('[data-role="card-declaration-item"]').count() == 1
    blocked = page.locator('[data-role="card-declaration-blocked"]')
    assert blocked.count() == 1
    assert "未放行" in blocked.text_content()
    assert page.locator('[data-role="card-declaration-waived"]').count() == 0
    tip = page.locator(f'#blk-{PARAGRAPH} [data-role="tip-declared"]').text_content()
    assert "未放行" in tip


def test_over_declaration_says_so_instead_of_blocked(page, ar_server) -> None:
    """乱申报（锚点一个没少却申报放弃了某项）：页面说「多申报」，不说「未放行」（2026-10-14 收尾）。"""
    FakeApi(page, doc=doc_with_over_declaration())
    open_api(page, ar_server)

    assert dbg(page)["fidelity"]["over_declared"], "夹具副本里应当有一条多申报"
    open_card_for(page, PARAGRAPH)

    over = page.locator('[data-role="card-declaration-over"]')
    assert over.count() == 1
    assert "一个锚点都没少" in over.text_content() and "多申报" in over.text_content()
    # 既不是「已放行」（没有缺失可放行），也不是「未放行」（会让人以为真丢了锚点）
    assert page.locator('[data-role="card-declaration-waived"]').count() == 0
    assert page.locator('[data-role="card-declaration-blocked"]').count() == 0
    tip = page.locator(f'#blk-{PARAGRAPH} [data-role="tip-declared"]')
    assert tip.count() == 1
    assert "多申报" in tip.text_content() and tip.get_attribute("data-over-declared") == "true"


def test_selection_scope_hides_the_declaration(page, ar_server) -> None:
    """片段模式不显示整段口径的申报（与事实锚同一取舍）。"""
    open_fixture(page, ar_server)

    select_in_block(page, PARAGRAPH, start=0, length=6)
    page.locator('[data-role="select-explain"][data-kind="simpler"]').click()
    expect(card(page)).to_be_visible()

    assert page.locator('[data-role="card-declaration"]').count() == 0
    assert page.locator('[data-role="card-fact"]').count() == 0, "事实锚在片段模式下也不显示（同一口径）"
    assert "选中片段" in page.locator('[data-role="card-scope"]').text_content()
