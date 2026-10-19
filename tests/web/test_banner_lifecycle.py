"""横幅（页头降级提示）显示层的无头验证：同文本去重 / 逐条可关 / 成功类自动消失。

缺陷现场（2026-10-09 用户实测 + 截图）：读前背景问卷提交后，顶部同时挂着三条横幅——
「背景已保存」+ **两条一字不差**的「模型未配置」，既不会消失也关不掉。

修复后的四条判据（实现都在 `web/js/view.js` 的 `addBanner`，`main.js` 只负责传状态）：

1. **同文本只留一条**：再出现只把 `data-repeats` 加一，不新增节点
   （重复触发照旧各记一条 `__arDebug.degradations` —— 去重属于显示层，不是记录层）；
2. **每条都能手动关**：`×` 按钮，鼠标与键盘都能用，焦点可达、`:focus-visible` 有可见轮廓，
   关掉一条不影响别的横幅；
3. **成功类自动消失**：`data-state="info"`（如「背景已保存」）`BANNER_AUTO_DISMISS_MS` 后自己走；
4. **降级类不自动消失**：`data-state="warn"` / `"error"` 永远留着等手动关
   —— `web/README.md` 的「降级纪律（DESIGN §6）」要求降级可见，`__arDebug` 的语义一个字没动。

本文件复用 `tests/web/test_survey_ui.py` 的假后端与问卷助手：那套桩已经覆盖
「提交固定题 / 追问降级」两个端点，这里不再抄一份（tests/web 没有 `__init__.py`，
pytest 会把本目录放进 `sys.path`，所以可以直接 import）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

FIXTURE_ID = "capital-v1-ch01"
FIXTURE_DIR = Path(__file__).resolve().parent.parent.parent / "samples" / FIXTURE_ID

# 与 web/js/view.js 的 BANNER_AUTO_DISMISS_MS 对齐（改一边必须改另一边）；
# 末尾留一点余量，免得机器慢的时候把「已经自己走了」判成「还没走」。
AUTO_DISMISS_MS = 8000
TIMEOUT_MARGIN_MS = 4000

# 真服务/假后端这一刻回的降级文案（与后端 ar/survey.py 的 model_not_configured 文案同源）。
DEGRADE_TEXT = ("模型未配置，暂时不能生成追问。设置环境变量 AR_API_KEY，"
                "或在 config.local.json 里填 api_key，再点一次即可（背景已保存，不受影响）。")

# 「背景已保存」那条成功横幅的**独有**片段：降级文案里也有「背景已保存，不受影响」四个字，
# 按「背景已保存」找会把两条都命中（`has_text` 是子串匹配）。
SAVED_TEXT_HINT = "改写产物要重跑"

# 关掉一条横幅不许动的那些可断言标志（`__arDebug` 全套语义不变；
# 2026-10-13 起把配图 / 压缩 / 卡片开关这三组也纳进来——横幅是显示层，不许碰它们）。
FLAG_KEYS = (
    "ready", "fatal", "mode", "sourceId", "errors", "degradations", "cards", "lastCard",
    "compression", "surveyOpen", "surveyPhase", "surveyBootReason", "surveyDegraded",
    "surveyEvents", "surveyAnswers", "surveyFollowup",
    "figures", "figureScript", "compress", "compressEvents", "cardOpens", "events",
)

from test_survey_ui import (  # noqa: E402  —— 见文件头：复用假后端与问卷助手
    banner_texts, dbg, fake_backend, open_api, open_fixture, pick, survey_payload,
    wait_survey_open,
)


# 假后端要的夹具（test_survey_ui.py 里的那两个是模块级 fixture，跨文件不共享，这里各自读一次）。
@pytest.fixture(scope="module")
def fixture_doc() -> dict:
    return json.loads((FIXTURE_DIR / "doc.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fixture_questions() -> dict:
    return json.loads((FIXTURE_DIR / "survey-questions.json").read_text(encoding="utf-8"))


def all_banners(page):
    return page.locator('[data-role="banner"]')


def banners_with(page, needle: str):
    return page.locator('[data-role="banner"]', has_text=needle)


def flags(page) -> dict:
    debug = dbg(page)
    return {key: debug[key] for key in FLAG_KEYS}


def saved_then_degraded_followup(page, ar_server, fixture_doc, fixture_questions) -> int:
    """逼出用户截图里的那一幕：背景保存成功（成功横幅）+ 追问两次都失败（同文本降级横幅）。

    「再点一次即可」是降级文案自己教用户做的动作，所以第二次点击必须复现；
    追问按钮在降级后仍留在卡片上（不渲染新阶段），两次点击＝两次请求。

    **返回打开页面时的横幅条数**（基线）：夹具里 `p016` 是「后端判定配图失败」的样本，
    它在加载时就挂一条降级横幅，所以后面的条数断言一律按「基线 + N」写，不写死绝对值。
    """
    fake_backend(
        page, fixture_doc,
        survey=survey_payload(fixture_questions["questions"]),
        survey_post={"ok": True, "background": {}, "sketch": "", "degraded": []},
        followup_post={"questions": [], "text": DEGRADE_TEXT, "degraded": True,
                       "reason": "model_not_configured", "prompt_version": "survey-v1"},
    )
    open_api(page, ar_server)
    wait_survey_open(page)
    baseline = all_banners(page).count()

    pick(page, "Q1", "master")
    pick(page, "Q4", "none")
    page.click('[data-role="survey-submit"]')
    # 成功横幅落地：这里只等**文字**（它该是哪种 data-state 由各用例断言，
    # 修好前 `main.js` 把 info 压成了 warn——夹具把它压平的话，「成功类自动消失」就无从谈起）
    expect(banners_with(page, SAVED_TEXT_HINT)).to_have_count(1)

    start = page.locator('[data-role="survey-followup-start"]')
    start.click()
    # 第一次降级落地（按文字等，不预设它的 data-state：那是 main.js 的判据，修好前 info 被压成 warn）
    expect(banners_with(page, "模型未配置")).to_have_count(1)
    start.click()
    # 第二次请求也回来了（`__arDebug` 是记录层，修复前后都是两条）：显示层怎么处理由各用例断言
    page.wait_for_function(
        "() => globalThis.__arDebug.degradations"
        ".filter((d) => d.kind === 'survey-followup').length === 2",
        timeout=10000,
    )
    return baseline


# =================================================== 1. 同文本不叠

def test_same_text_banner_is_not_stacked(page, ar_server, fixture_doc, fixture_questions):
    """两条一字不差的降级横幅只留一条：第二条只把计数加到 2，`__arDebug` 记录照旧两条。"""
    baseline = saved_then_degraded_followup(page, ar_server, fixture_doc, fixture_questions)

    degrade = banners_with(page, "模型未配置")
    expect(degrade).to_have_count(1)
    assert all_banners(page).count() == baseline + 2, banner_texts(page)   # 成功那条 + 降级那条

    only = degrade.first
    assert only.get_attribute("data-state") == "warn"
    assert only.get_attribute("data-repeats") == "2"
    counter = only.locator('[data-role="banner-repeat"]')
    assert counter.count() == 1, "重复触发要留下可见/可断言的计数"
    assert not counter.is_hidden()
    assert "2" in counter.text_content()

    # 去重只是显示层：两次触发在 __arDebug 里各记一条（降级不许被吞掉）
    assert len([d for d in dbg(page)["degradations"] if d["kind"] == "survey-followup"]) == 2
    assert len([e for e in dbg(page)["surveyEvents"] if e["type"] == "followup-done"]) == 2
    assert dbg(page)["surveyFollowup"]["reason"] == "model_not_configured"


# =================================================== 2. 可手动关（鼠标）

def test_close_button_removes_only_that_banner_and_keeps_debug_flags(
    page, ar_server, fixture_doc, fixture_questions,
):
    """`×` 关掉降级那条：它消失、成功那条还在，`__arDebug` 的标志一个都不变。"""
    baseline = saved_then_degraded_followup(page, ar_server, fixture_doc, fixture_questions)
    degrade = banners_with(page, "模型未配置")
    expect(degrade).to_have_count(1)
    assert all_banners(page).count() == baseline + 2, banner_texts(page)   # 成功那条 + 降级那条
    # 两条都带关闭按钮（成功那条自己会走，但手动关也得能关）
    assert banners_with(page, SAVED_TEXT_HINT).locator('[data-role="banner-close"]').count() == 1

    close = degrade.first.locator('[data-role="banner-close"]')
    assert close.count() == 1, "横幅上没有关闭按钮"
    assert close.get_attribute("aria-label"), "关闭按钮要有可读的名字（键盘/读屏用）"

    before_flags = flags(page)
    count_before = all_banners(page).count()
    close.click()

    expect(banners_with(page, "模型未配置")).to_have_count(0)
    assert all_banners(page).count() == count_before - 1, banner_texts(page)
    assert "背景已保存" in banners_with(page, SAVED_TEXT_HINT).first.text_content()
    assert flags(page) == before_flags, "关横幅只该动显示层，__arDebug 不许变"


# =================================================== 3. 成功类自动消失 / 降级类不消失

def test_success_banner_auto_dismisses_while_degrade_stays(
    page, ar_server, fixture_doc, fixture_questions,
):
    """成功类（info）到点自己走；同一时刻的降级类（warn）必须还在原地等着被手动关。"""
    baseline = saved_then_degraded_followup(page, ar_server, fixture_doc, fixture_questions)
    # 先钉住「成功类」的判据：「背景已保存」必须是 info（只有 info 才自动消失）
    info = page.locator('[data-role="banner"][data-state="info"]')
    expect(info).to_have_count(1)
    assert "背景已保存" in info.first.text_content()

    page.wait_for_timeout(200)          # 让「不消失」这件事有观察窗，而不是加载完就断言
    expect(info).to_have_count(1)
    before = flags(page)

    # 到点自己走（等待上限＝时长 + 余量；超时还没走就是「没自动消失」）
    expect(info).to_have_count(0, timeout=AUTO_DISMISS_MS + TIMEOUT_MARGIN_MS)

    degrade = banners_with(page, "模型未配置")
    assert degrade.count() == 1, f"降级横幅不该自动消失：{banner_texts(page)}"
    assert degrade.first.get_attribute("data-state") == "warn"
    assert all_banners(page).count() == baseline + 1, banner_texts(page)
    # 自动消失同样只动显示层
    after = flags(page)
    assert after["degradations"] == before["degradations"]
    assert after["surveyEvents"] == before["surveyEvents"]


def test_warn_banner_survives_the_success_timeout_alone(page, ar_server):
    """页面只有降级横幅时，等过成功类的那段时间它也一条都不许少（夹具模式横幅）。"""
    open_fixture(page, ar_server, extra="")
    fixture_banner = banners_with(page, "夹具模式")
    assert fixture_banner.count() == 1
    count_before = all_banners(page).count()

    page.wait_for_timeout(AUTO_DISMISS_MS + 800)     # 等到过了成功类的那条时间线

    assert all_banners(page).count() == count_before, banner_texts(page)
    expect(fixture_banner).to_have_count(1)
    assert fixture_banner.first.get_attribute("data-state") == "warn"


# =================================================== 4. 可手动关（键盘 + 焦点轮廓）

def test_close_button_is_keyboard_reachable_with_a_visible_focus_ring(page, ar_server):
    """`Tab` 能走到 `×` 上、`:focus-visible` 有轮廓、`Enter` 能关掉它。"""
    open_fixture(page, ar_server, extra="")
    assert all_banners(page).count() >= 1

    focused = None
    for _ in range(40):
        page.keyboard.press("Tab")
        focused = page.evaluate(
            "() => { const el = document.activeElement;"
            " return el && el.dataset ? el.dataset.role : null; }"
        )
        if focused == "banner-close":
            break
    assert focused == "banner-close", f"Tab 走不到横幅的关闭按钮上（停在 {focused}）"

    outline = page.evaluate(
        "() => { const s = getComputedStyle(document.activeElement);"
        " return { style: s.outlineStyle, width: s.outlineWidth, color: s.outlineColor }; }"
    )
    assert outline["style"] == "solid" and outline["width"] not in ("0px", ""), \
        f"键盘聚焦时看不到轮廓：{outline}"

    count_before = all_banners(page).count()
    page.keyboard.press("Enter")
    expect(all_banners(page)).to_have_count(count_before - 1)
    assert all_banners(page).count() == count_before - 1, banner_texts(page)
