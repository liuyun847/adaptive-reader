"""页面上的「依据画像」：空 `why.profile` 也要显式说明 + 兜底补齐要看得见（2026-10-14）。

为什么单列一个文件：`why.profile` 为空**不等于「画像为空」**——读者可能填了读前背景问卷
（背景非空即算「有画像」，契约 §6.4），也可能这一段涉及的概念他本来就没自评；
而背景依据按拍板 3 **只写在 `why.note` 里**，所以空数组是合法结果。
旧页面在空数组时什么都不显示，沉默会被读成「没有画像」——那正是 2026-10-13 真机产物
里「`why.note` 自称画像为空、同一句又引用背景」那类自相矛盾的另一半。

本文件守三件事：
  ① 空数组：悬停行与卡片都写「依据画像：无自评引用」（一句如实的话，不是「画像为空」）；
  ② 非空：照旧逐条渲染 chips（这次修复不许把好好的引用显示弄丢）；
  ③ 兜底补齐（同日复核修复的可见面）：产物里带 `changes[i].profile_refs_filled` 的段，
     卡片上挂一枚可见徽标、悬停多一行出处说明——**判据只认这个结构化字段，不认 `why.note` 的文案**
     （第 ③ 条有负向对照：现场那段 `why.note` 里一个字都不含「兜底补齐」）。

零真实模型调用；就绪只等 `__arDebug.ready`，每个用例跑完由 conftest 统一断言页面无 JS 报错。
"""

from __future__ import annotations

import copy

from playwright.sync_api import expect

from ar_backend import FakeApi, fixture_doc, open_api, open_fixture

#: 夹具里 `why.profile` 非空的那一段（`["c01:unknown"]`）
RATED = "p005"
#: 夹具里 `why.profile` 为空的那一段（标题段）
EMPTY = "p004"
NO_REFS_LABEL = "依据画像：无自评引用"
#: 兜底补齐那两处文案（与 `web/js/schema.js` 的两个常量逐字一致）
FILLED_BADGE = "骨架兜底补齐（模型没给引用）"
FILLED_TIP = "依据画像由骨架兜底补齐：模型没有给 why.profile 引用，这里照读者自评逐条列出"


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


def doc_with_empty_refs() -> dict:
    """夹具副本：把 p004 的 `why.profile` 清空（页面侧的「无自评引用」现场）。"""
    payload = copy.deepcopy(fixture_doc())
    for change in payload["changes"]["changes"]:
        if change["paragraph"] == EMPTY:
            change["why"]["profile"] = []
    return payload


def doc_with_filled_refs() -> dict:
    """夹具副本：把 p005 标成「这一段 `why.profile` 是骨架照读者自评兜底补齐的」。

    `why.note` **故意改写成一句不含「兜底补齐」四个字的散文**——这是这条用例的负向对照：
    页面若拿正则去猜文案，标记就挂不出来（判据只能是 `profile_refs_filled` 这个结构化字段）。
    """
    payload = copy.deepcopy(fixture_doc())
    for change in payload["changes"]["changes"]:
        if change["paragraph"] == RATED:
            change["profile_refs_filled"] = True
            change["why"]["note"] = "这一段涉及的概念读者只自评过 c01，其余没有信号"
    return payload


def test_empty_profile_refs_are_labelled_instead_of_silent(page, ar_server) -> None:
    """空数组：悬停行与卡片都写「无自评引用」，且页面不许出现「画像为空」。"""
    FakeApi(page, doc=doc_with_empty_refs())
    open_api(page, ar_server)

    tip = page.locator(f'#blk-{EMPTY} [data-role="tip-profile"]')
    assert tip.count() == 1, "空 why.profile 也要有一行说明，不能什么都不显示"
    assert tip.text_content() == NO_REFS_LABEL
    assert tip.get_attribute("data-empty") == "true"

    open_card_for(page, EMPTY)
    empty = page.locator('[data-role="card-profile-empty"]')
    assert empty.count() == 1
    assert empty.text_content() == NO_REFS_LABEL
    body = page.locator('[data-role="card-body"]').text_content()
    assert "依据画像" in body and "无自评引用" in body
    assert "画像为空" not in body, "「没有自评引用」不是「画像为空」"
    assert page.locator('[data-role="card-profile-chip"]').count() == 0


def test_non_empty_profile_refs_still_render_chips(page, ar_server) -> None:
    """非空：悬停行与卡片 chips 逐条照旧（这次修复不许把已有的显示弄丢）。"""
    doc = fixture_doc()
    row = next(c for c in doc["changes"]["changes"] if c["paragraph"] == RATED)
    refs = row["why"]["profile"]
    assert refs, "夹具里这一段应当有自评引用——不然这条用例守不住任何东西"

    open_fixture(page, ar_server)

    tip = page.locator(f'#blk-{RATED} [data-role="tip-profile"]')
    assert tip.text_content() == f"依据画像：{refs[0]}"
    assert tip.get_attribute("data-empty") == "false"

    open_card_for(page, RATED)
    chips = page.locator('[data-role="card-profile-chip"]')
    assert chips.count() == len(refs)
    assert chips.first.text_content() == refs[0]
    assert page.locator('[data-role="card-profile-empty"]').count() == 0


def test_filled_profile_refs_are_marked_on_the_card_and_in_the_tip(page, ar_server) -> None:
    """兜底补齐：卡片一枚可见徽标 + 悬停一行出处说明，判据是结构化字段（不是 `why.note` 的文案）。

    现场用夹具副本注入：① 段上带 `profile_refs_filled: true`；② 该段 `why.note` 里
    **一个字都不含「兜底补齐」**——页面若改用正则猜文案，这条用例当场变红。
    """
    doc = doc_with_filled_refs()
    row = next(c for c in doc["changes"]["changes"] if c["paragraph"] == RATED)
    assert row["profile_refs_filled"] is True and row["why"]["profile"], "现场没造对"
    assert "兜底补齐" not in row["why"]["note"], "负向对照：文案里不许出现「兜底补齐」四个字"

    FakeApi(page, doc=doc)
    open_api(page, ar_server)

    # 悬停：引用照旧一行，多一行出处说明；标记上留可断言的属性
    tip = page.locator(f'#blk-{RATED} [data-role="tip-profile"]')
    assert tip.text_content() == f"依据画像：{row['why']['profile'][0]}"
    assert tip.get_attribute("data-filled") == "true"
    filled_tip = page.locator(f'#blk-{RATED} [data-role="tip-profile-filled"]')
    assert filled_tip.count() == 1
    assert filled_tip.text_content() == FILLED_TIP
    marker = page.locator(f'#blk-{RATED} [data-role="marker"]')
    assert marker.get_attribute("data-profile-filled") == "true"

    # 卡片：引用 chips 照旧，另加一枚可见徽标；「无自评引用」那一句不许出现（这一段有引用）
    open_card_for(page, RATED)
    badge = page.locator('[data-role="card-profile-filled"]')
    assert badge.count() == 1
    assert badge.text_content() == FILLED_BADGE
    assert page.locator('[data-role="card-profile-chip"]').count() == len(row["why"]["profile"])
    assert page.locator('[data-role="card-profile-empty"]').count() == 0


def test_paragraphs_without_the_flag_carry_no_badge(page, ar_server) -> None:
    """没有这个结构化字段的段（老产物 / 本来就不是补齐的）不许挂标记。

    夹具里 p004 的 `why.profile` 为空、且没有 `profile_refs_filled`：它只该显示「无自评引用」，
    「骨架兜底补齐」那两处一个字都不许出现（**不误报**是后端那条纪律在页面上的那半边）。
    """
    open_fixture(page, ar_server)

    for pid in (EMPTY, "p017"):
        assert page.locator(f'#blk-{pid} [data-role="tip-profile-filled"]').count() == 0, pid
        assert page.locator(f'#blk-{pid} [data-role="marker"]').get_attribute(
            "data-profile-filled"
        ) == "false", pid

    open_card_for(page, EMPTY)
    assert page.locator('[data-role="card-profile-filled"]').count() == 0
    assert page.locator('[data-role="card-profile-empty"]').count() == 1

