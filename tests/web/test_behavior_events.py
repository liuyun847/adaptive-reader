"""行为埋点的无头验证（playwright 驱动本机 Edge；`PLAN-actions-feedback.md` §7.1 / §7.2）。

覆盖：

1. **缓冲 + 阈值 flush**：≥10 条自动发一批（`POST /api/events`，体是 `{events: [...]}`），
   没到阈值**一条都不发**；15 秒定时那条只看口径暴露值（等 15 秒的用例太慢，不值当）；
2. **离场强刷**：`pagehide` / `visibilitychange`（转隐藏）各刷一次；`pagehide` 且没读完时补一条
   `read_abandon`，切标签页（`visibilitychange`）**不算**放弃；
3. **夹具模式不发请求**：缓冲照记、flush 时丢弃，给**一次性**可见降级「夹具模式不记行为信号」；
4. **失败可见**：后端回 `ok:false` / 带 `degraded` 时留字，**不许静默丢弃**；
5. **埋点自己不触发模型请求**：这条路只发 `/api/events`；
6. **kind 只在 §7.1 白名单里**：卡片开关不在白名单里（不臆造新 kind），兜底记 `__arDebug.cardOpens`；
7. **真交互都埋到**：当前段切换（`dwell`）/ 抽屉（`drawer_open`）/ 选片（`selection`）/ 讲解（`explain`）。

假后端在 `ar_backend.py`（`page.route` 拦 `/api/*`）：**零真实模型调用**。
"""

from __future__ import annotations

import re

from ar_backend import FakeApi, banner_texts, dbg, has_banner, open_api, open_fixture

# 契约 §7.1 的 kind 白名单（前端只许发这几种；这条断言就是「不许臆造 kind」的闸）
EVENT_KINDS = {"dwell", "read_complete", "read_abandon", "explain",
               "drawer_open", "selection", "compress", "skip"}


def events(page) -> dict:
    return dbg(page)["events"] or {}


def records(page) -> list:
    return events(page)["records"]


def kinds(page) -> list:
    return [item["kind"] for item in records(page)]


def flush_reasons(page) -> list:
    return [item["reason"] for item in events(page)["flushes"]]


def sent_events(api: FakeApi) -> list:
    return [item for batch in api.event_batches for item in batch]


def emit(page, kind: str, count: int = 1, **payload) -> None:
    page.evaluate(
        "(arg) => { for (let i = 0; i < arg[1]; i += 1) globalThis.__arDebug.debugEvent(arg[0], arg[2]); }",
        [kind, count, payload],
    )


# =================================================== 1. 缓冲 + 阈值 flush

def test_events_are_buffered_then_flushed_at_the_threshold(page, ar_server):
    api = FakeApi(page)
    open_api(page, ar_server)

    assert events(page)["threshold"] == 10
    assert events(page)["intervalMs"] == 15000
    assert events(page)["buffered"] == 0, "刚打开页面不该有滞留事件"

    emit(page, "dwell", 9, paragraph="p005", ms=1200)
    assert events(page)["buffered"] == 9
    assert api.calls("/api/events") == [], "没到阈值不许发"

    emit(page, "dwell", 1, paragraph="p006", ms=900)
    page.wait_for_function("() => globalThis.__arDebug.events.sent >= 10", timeout=10000)

    assert len(api.event_batches) == 1, api.event_batches
    batch = api.event_batches[0]
    assert len(batch) == 10
    for item in batch:
        assert item["v"] == 1 and item["kind"] == "dwell"
        assert item["source_id"] == "capital-v1-ch01"
        assert isinstance(item["id"], str) and item["id"]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", item["at"]), item["at"]
    assert [item["paragraph"] for item in batch[:9]] == ["p005"] * 9
    assert batch[-1]["paragraph"] == "p006" and batch[-1]["ms"] == 900

    assert events(page)["buffered"] == 0
    assert events(page)["sent"] == 10
    assert flush_reasons(page) == ["threshold"]
    assert kinds(page).count("dwell") == 10
    # 只发 /api/events 这一个端点（埋点不碰讲解 / 压缩）
    assert [item["path"] for item in api.requests if item["method"] == "POST"] == ["/api/events"] * 1


# =================================================== 2. 离场强刷

def test_pagehide_flushes_and_records_read_abandon(page, ar_server):
    api = FakeApi(page)
    open_api(page, ar_server)

    # 真交互：滚一段（记 dwell）、开抽屉（记 drawer_open）
    page.evaluate("() => document.querySelector('#blk-p005').scrollIntoView({block: 'center'})")
    page.wait_for_function("() => globalThis.__arDebug.currentParagraph !== null")
    page.click('[data-role="drawer-toggle"]')
    page.wait_for_function("() => globalThis.__arDebug.events.buffered >= 1")

    assert events(page)["readComplete"] is False
    page.evaluate("() => window.dispatchEvent(new Event('pagehide'))")
    page.wait_for_function(
        "() => globalThis.__arDebug.events.flushes.some((f) => f.reason === 'pagehide')", timeout=10000,
    )

    sent = sent_events(api)
    sent_kinds = [item["kind"] for item in sent]
    assert "drawer_open" in sent_kinds, sent_kinds
    assert "dwell" in sent_kinds, sent_kinds
    assert "read_abandon" in sent_kinds, f"没读完就离场要记 read_abandon：{sent_kinds}"
    assert "read_complete" not in sent_kinds

    abandon = [item for item in sent if item["kind"] == "read_abandon"][0]
    assert abandon["paragraph"] == dbg(page)["currentParagraph"]
    assert abandon["read_paragraphs"] >= 1
    assert "pagehide" in flush_reasons(page)
    assert events(page)["buffered"] == 0


def test_visibilitychange_hidden_flushes_without_calling_it_abandon(page, ar_server):
    api = FakeApi(page)
    open_api(page, ar_server)

    emit(page, "selection", 1, paragraph="p005", chars=12, spans=1, **{"from": "rewritten"})
    assert events(page)["buffered"] == 1

    page.evaluate(
        """() => {
            Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
            document.dispatchEvent(new Event('visibilitychange'));
        }""",
    )
    page.wait_for_function(
        "() => globalThis.__arDebug.events.flushes.some((f) => f.reason === 'visibilitychange')",
        timeout=10000,
    )

    sent_kinds = [item["kind"] for item in sent_events(api)]
    assert "selection" in sent_kinds
    assert "read_abandon" not in sent_kinds, "切标签页不算放弃阅读（读者会回来）"


def test_read_complete_replaces_read_abandon(page, ar_server):
    """滚到底读完之后再离场：只记 `read_complete`，不记 `read_abandon`。"""
    api = FakeApi(page)
    open_api(page, ar_server)

    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_function("() => globalThis.__arDebug.events.readComplete === true", timeout=10000)

    page.evaluate("() => window.dispatchEvent(new Event('pagehide'))")
    page.wait_for_function(
        "() => globalThis.__arDebug.events.flushes.some((f) => f.reason === 'pagehide')", timeout=10000,
    )
    sent_kinds = [item["kind"] for item in sent_events(api)]
    assert "read_complete" in sent_kinds
    assert "read_abandon" not in sent_kinds


# =================================================== 3. 夹具模式

def test_fixture_mode_never_posts_events_and_says_so_once(page, ar_server):
    calls: list = []

    def record(route, request):
        calls.append((request.method, request.url))
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/**", record)
    open_fixture(page, ar_server)

    emit(page, "dwell", 10, paragraph="p005", ms=1000)
    page.wait_for_function("() => globalThis.__arDebug.events.dropped >= 10", timeout=10000)

    assert calls == [], f"夹具模式不该发任何 /api 请求：{calls}"
    assert events(page)["fixtureNoted"] is True
    # `flushes[].reason` 是「这次 flush 是被什么触发的」，`status` 才是「送出去没有」
    assert events(page)["flushes"][-1]["status"] == "fixture"
    assert flush_reasons(page)[-1] == "threshold"
    assert has_banner(page, "夹具模式不记行为信号"), banner_texts(page)

    # 一次性：再来一批也只留同一条横幅（不新增、也不靠重复计数堆字）
    banner = page.locator('[data-role="banner"]', has_text="夹具模式不记行为信号")
    assert banner.count() == 1
    emit(page, "dwell", 10, paragraph="p006", ms=1000)
    page.wait_for_function("() => globalThis.__arDebug.events.dropped >= 20", timeout=10000)
    assert banner.count() == 1
    assert banner.first.get_attribute("data-repeats") == "1"
    assert len([d for d in dbg(page)["degradations"] if d["kind"] == "events-fixture"]) == 1


# =================================================== 4. 失败可见

def test_events_write_failure_is_visible_and_never_touches_the_model(page, ar_server):
    api = FakeApi(page, events={
        "ok": False, "written": 0, "duplicates": 0, "total": 0,
        "degraded": ["events.jsonl 写不进去（磁盘只读）"],
    })
    open_api(page, ar_server)

    emit(page, "dwell", 10, paragraph="p005", ms=1000)
    page.wait_for_function(
        "() => globalThis.__arDebug.events.flushes.some((f) => f.status === 'failed')", timeout=10000,
    )

    assert has_banner(page, "行为信号没能记下"), banner_texts(page)
    assert has_banner(page, "磁盘只读"), "后端的降级说明要原样带出来"
    assert events(page)["failed"] == 10
    assert events(page)["sent"] == 0
    # 埋点这条路不碰模型：一个 explain / compress 请求都没有
    assert api.model_calls() == [], api.model_calls()
    assert dbg(page)["errors"] == []
    # 阅读照常：正文还在、还能继续记事件
    assert page.locator("#rewritten .block").count() == 25
    emit(page, "selection", 1, paragraph="p005", chars=5, spans=1, **{"from": "rewritten"})
    assert events(page)["buffered"] == 1


def test_events_http_failure_is_visible(page, ar_server):
    api = FakeApi(page, events_status=500)
    open_api(page, ar_server)

    emit(page, "dwell", 10, paragraph="p005", ms=1000)
    page.wait_for_function(
        "() => globalThis.__arDebug.events.flushes.some((f) => f.status === 'error')", timeout=10000,
    )
    assert has_banner(page, "行为信号没能送出去") and has_banner(page, "500")
    assert events(page)["failed"] == 10
    assert api.model_calls() == []


# =================================================== 5. 真交互都埋到 + 不臆造 kind

def test_instrumentation_covers_the_real_interactions(page, ar_server):
    api = FakeApi(page)
    open_api(page, ar_server)

    # 当前段切换（结算上一段的 dwell）
    page.evaluate("() => document.querySelector('#blk-p005').scrollIntoView({block: 'center'})")
    page.wait_for_function("() => globalThis.__arDebug.currentParagraph !== null")
    page.evaluate("() => document.querySelector('#blk-p018').scrollIntoView({block: 'center'})")
    page.wait_for_function("() => globalThis.__arDebug.currentParagraph !== null"
                           " && globalThis.__arDebug.currentParagraph !== 'p005'")
    # 抽屉
    page.click('[data-role="drawer-toggle"]')
    # 选片浮条 + 按钮
    page.evaluate(
        "(arg) => globalThis.__arDebug.debugSelect(arg)",
        {"paragraph": "p005", "start": 0, "length": 10, "from": "rewritten"},
    )
    page.wait_for_function("() => globalThis.__arDebug.toolbarOpen === true")
    page.click('[data-role="select-explain"][data-kind="explain"]')
    page.wait_for_function("() => globalThis.__arDebug.lastCard && globalThis.__arDebug.lastCard.state")

    page.wait_for_function("() => (globalThis.__arDebug.events.records || [])"
                           ".some((e) => e.kind === 'explain')", timeout=10000)
    seen = set(kinds(page))
    assert {"dwell", "drawer_open", "selection", "explain"} <= seen, seen
    assert seen <= EVENT_KINDS, f"发出去的 kind 必须在白名单里：{seen - EVENT_KINDS}"

    dwell = [item for item in records(page) if item["kind"] == "dwell"][0]
    assert dwell["paragraph"] == "p005" and dwell["ms"] >= 0
    selection = [item for item in records(page) if item["kind"] == "selection"][0]
    assert selection["chars"] == 10 and selection["spans"] == 1 and selection["from"] == "rewritten"
    explain = [item for item in records(page) if item["kind"] == "explain"][0]
    assert explain["explain_kind"] == "explain"
    assert explain["scope"] == "selection" and explain["selection_chars"] == 10
    assert explain["selection_from"] == "rewritten"
    assert explain["cached"] is False and explain["degraded"] is False

    # 埋点没有额外打模型：只有那一次显式点的讲解
    assert len(api.calls("/api/explain")) == 1
    assert api.calls("/api/compress") == []


def test_card_open_and_close_stay_out_of_the_signal_stream(page, ar_server):
    """卡片开关不进行为流：§7.1 的 kind 白名单里没有这一类，前端不臆造新 kind。

    它只落在 `__arDebug.cardOpens` / `cardCloses` 上（可断言的调试面）。
    若将来要给「打开卡片」也记信号，得先往契约的白名单里加一个 kind，再让服务端认它。
    """
    api = FakeApi(page)
    open_api(page, ar_server)

    page.evaluate("() => document.querySelector('#blk-p005 [data-role=marker]').click()")
    page.wait_for_function("() => globalThis.__arDebug.cardOpen === true")
    page.keyboard.press("Escape")
    page.wait_for_function("() => globalThis.__arDebug.cardOpen === false")

    assert len(dbg(page)["cardOpens"]) == 1
    assert dbg(page)["cardOpens"][0]["paragraph"] == "p005"
    assert len(dbg(page)["cardCloses"]) >= 1
    assert events(page)["rejected"] == [], "不该出现白名单外的 kind"
    assert set(kinds(page)) <= EVENT_KINDS, kinds(page)
    assert api.calls("/api/events") == [], "没到阈值就不发（卡片开关本身也不产生事件）"
