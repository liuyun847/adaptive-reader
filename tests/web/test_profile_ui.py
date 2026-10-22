"""阅读器页的「画像」入口：概念自评 + 事实锚口径（2026-10-22）。

为什么单列一个文件：这两件事的后端端点（`POST /api/profile/rating`、
`POST /api/profile/preference`）早就可用，但此前页面上没有任何入口——自评只能手改
`profile.yaml`，偏好只能 POST 或手改；README 的「尚未实现」表和 §8 第 22 条都把这件事
记成缺口。本文件守五件事：

  ① 入口在阅读器页顶栏，点开才读 `GET /api/profile`（不挡正文）；
  ② 概念列表来自当前材料的 `concepts.json`，当前自评档照画像显示；
  ③ 点自评 → `POST /api/profile/rating`（请求体是 `{concept_id, state}`），成功后
     面板就地刷新、画像指纹变化（会话讲解缓存随之失效）、横幅可见；
  ④ 改「事实锚口径」→ `POST /api/profile/preference`（`{id, value}`），同上；
  ⑤ 夹具模式不请求任何 `/api/*`，面板明写「不能保存」且控件禁用；读 / 写失败都
     **只显示失败、不先把界面改成成功**。

零真实模型调用（假后端只回固定 JSON）；就绪只等 `__arDebug.ready`，每个用例跑完由
conftest 统一断言页面无 JS 报错。
"""

from __future__ import annotations

from playwright.sync_api import expect

from ar_backend import FakeApi, dbg, fixture_doc, has_banner, open_api, open_fixture

FACT_ANCHOR_STATEMENT = "要结论：允许在申报后丢掉纯举例性的数据、把引文改成转述"


def profile_payload() -> dict:
    """一份带 c01 自评 + 松口径偏好的画像（形状 = `GET /api/profile` 的响应体）。"""
    return {
        "profile": {
            "reader": "local-default",
            "background": {},
            "concepts": {"c01": {"state": "partial", "confidence": 0.7}},
            "preferences": [{
                "id": "pref_fact_anchor",
                "value": "allow_declared_drops",
                "statement": FACT_ANCHOR_STATEMENT,
            }],
            "overrides": [{"concept": "c01", "state": "partial"}],
        },
        "degraded": [],
    }


def open_panel(page) -> None:
    page.locator('[data-role="profile-toggle"]').click()
    expect(page.locator('[data-role="profile-panel"]')).to_be_visible()
    expect(page.locator('[data-role="profile-status"]')).to_have_attribute("data-state", "ready")


def concept_count() -> int:
    return len(fixture_doc()["concepts"]["concepts"])


def test_panel_lists_material_concepts_and_current_states(page, ar_server) -> None:
    """入口点开：概念数 = 材料概念数；c01 显示 partial；偏好显示松口径。"""
    api = FakeApi(page, profile=profile_payload())
    open_api(page, ar_server)

    assert page.locator('[data-role="profile-toggle"]').count() == 1, "阅读器页顶栏要有「画像」入口"
    assert api.calls("/api/profile") == [], "画像只在点开面板时读，不挡正文"
    open_panel(page)
    assert len(api.calls("/api/profile")) == 1

    rows = page.locator('[data-role="profile-concept"]')
    assert rows.count() == concept_count(), "概念列表只列当前材料的概念"
    c01 = page.locator('[data-role="profile-concept"][data-concept="c01"]')
    assert c01.get_attribute("data-state") == "partial"
    assert c01.locator('[data-role="profile-rate"][data-state="partial"]').get_attribute(
        "aria-pressed"
    ) == "true"
    assert c01.locator('[data-role="profile-concept-state"]').text_content() == "知道一点"

    checked = page.locator('[data-role="profile-preference"][value="allow_declared_drops"]')
    assert checked.is_checked(), "画像里那条偏好要显示成选中"
    assert page.locator('[data-role="profile-preference"][value="keep_all"]').is_checked() is False

    panel = dbg(page)["profilePanel"]
    assert panel["state"] == "ready"
    assert panel["mode"] == "api"
    assert panel["preference"]["value"] == "allow_declared_drops"
    assert len(panel["concepts"]) == concept_count()


def test_rating_click_writes_back_and_updates_the_panel(page, ar_server) -> None:
    """点「知道」→ POST 请求体准确；行、状态行、横幅、画像指纹都跟着变。"""
    api = FakeApi(page, profile=profile_payload())
    open_api(page, ar_server)
    before_signal = dbg(page)["profileSignal"]
    open_panel(page)

    row = page.locator('[data-role="profile-concept"][data-concept="c02"]')
    assert row.get_attribute("data-state") == "unrated", "夹具里 c02 本来没有自评"
    page.locator('[data-role="profile-rate"][data-concept="c02"][data-state="known"]').click()

    expect(row).to_have_attribute("data-state", "known")
    assert row.locator('[data-role="profile-concept-state"]').text_content() == "知道"
    assert api.rating_calls == [{"concept_id": "c02", "state": "known"}], api.rating_calls
    assert has_banner(page, "已记下自评")
    # 同一档再点一次：不重复写盘（自评端点不幂等）
    page.locator('[data-role="profile-rate"][data-concept="c02"][data-state="known"]').click()
    expect(page.locator('[data-role="profile-status"]')).to_contain_text("已经是")
    assert len(api.rating_calls) == 1, "同档重复点不该再发请求"
    panel = dbg(page)["profilePanel"]
    assert panel["lastWrite"]["kind"] == "rating"
    assert panel["lastWrite"]["concept"] == "c02"
    assert panel["error"] is None
    assert dbg(page)["profileSignal"] != before_signal, "画像变了，会话讲解缓存的键要跟着变"


def test_preference_change_writes_back_and_updates_the_panel(page, ar_server) -> None:
    """改「事实锚口径」→ POST 请求体准确；选中项、状态行、横幅都跟着变。"""
    api = FakeApi(page, profile=profile_payload())
    open_api(page, ar_server)
    open_panel(page)

    page.locator('[data-role="profile-preference"][value="keep_all"]').check()
    expect(page.locator('[data-role="profile-preference"][value="keep_all"]')).to_be_checked()

    assert api.preference_calls == [{"id": "pref_fact_anchor", "value": "keep_all"}],         api.preference_calls
    assert "事实锚口径已改为" in page.locator('[data-role="profile-status"]').text_content()
    assert has_banner(page, "事实锚口径已改为")
    panel = dbg(page)["profilePanel"]
    assert panel["preference"]["value"] == "keep_all"
    assert panel["lastWrite"]["kind"] == "preference"


def test_fixture_mode_explains_why_it_cannot_save_and_makes_no_api_calls(page, ar_server) -> None:
    """夹具模式：不请求 /api/*，面板写明原因，自评与偏好控件一律禁用。"""
    calls: list[str] = []
    page.on("request", lambda request: calls.append(request.url) if "/api/" in request.url else None)
    open_fixture(page, ar_server)

    page.locator('[data-role="profile-toggle"]').click()
    expect(page.locator('[data-role="profile-panel"]')).to_be_visible()

    status = page.locator('[data-role="profile-status"]')
    assert status.get_attribute("data-state") == "fixture"
    assert "夹具模式" in status.text_content() and "不能保存" in status.text_content()
    assert calls == [], f"夹具模式不该请求画像端点：{calls}"

    buttons = page.locator('[data-role="profile-rate"]')
    assert buttons.count() > 0
    assert all(buttons.nth(i).is_disabled() for i in range(buttons.count()))
    inputs = page.locator('[data-role="profile-preference"]')
    assert inputs.count() == 2
    assert all(inputs.nth(i).is_disabled() for i in range(inputs.count()))
    assert dbg(page)["profilePanel"]["state"] == "fixture"


def test_load_failure_is_visible_and_never_pretends_success(page, ar_server) -> None:
    """读画像失败：状态行与横幅都说出来，概念列表不显示（没有数据就不装作有）。"""
    FakeApi(page, profile_status=500)
    open_api(page, ar_server)
    page.locator('[data-role="profile-toggle"]').click()

    status = page.locator('[data-role="profile-status"]')
    expect(status).to_have_attribute("data-state", "error")
    assert "画像读不出来" in status.text_content()
    assert has_banner(page, "读不到读者画像")
    assert page.locator('[data-role="profile-concept"]').count() == 0
    panel = dbg(page)["profilePanel"]
    assert panel["state"] == "error"
    assert panel["locked"] is True


def test_rating_failure_keeps_the_old_state_and_does_not_claim_success(page, ar_server) -> None:
    """写自评失败：状态行与横幅可见；该行仍是「未自评」；lastWrite 不出现。"""
    api = FakeApi(page, profile=profile_payload(), rating_status=500)
    open_api(page, ar_server)
    open_panel(page)
    before_signal = dbg(page)["profileSignal"]

    page.locator('[data-role="profile-rate"][data-concept="c02"][data-state="known"]').click()
    status = page.locator('[data-role="profile-status"]')
    expect(status).to_have_attribute("data-state", "write-error")
    assert "自评没写上" in status.text_content()
    assert has_banner(page, "自评没写上")
    row = page.locator('[data-role="profile-concept"][data-concept="c02"]')
    assert row.get_attribute("data-state") == "unrated", "失败不许先在界面上写成成功"
    assert api.rating_calls == [{"concept_id": "c02", "state": "known"}]
    panel = dbg(page)["profilePanel"]
    assert panel["lastWrite"] is None
    assert panel["locked"] is False, "画像没变，读者可以重试"
    assert dbg(page)["profileSignal"] == before_signal
