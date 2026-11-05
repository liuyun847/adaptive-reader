"""额度探测（``ar.quota``）：解析上游真实返回的形状，并且**永远 fail-open**。

夹具里的 JSON 是 2026-10-26 用本机凭据实测拿到的**裁剪版**（字段名与嵌套逐字保留），
上游改形状时这些用例会先红，而不是静默把顺序排错。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from ar.chain import Route
from ar.quota import QUOTA_MAX_TTL_MS, QUOTA_TTL_MS, QuotaReport, QuotaSource, probe_route

CC_BASE = "https://cc.invalid"
GO_BASE = "https://go.invalid/v1"

#: 2026-10-26 实测形状（Command Code）；金额换成**健康余额**（其余字段逐字保留）——
#: 真·见底那份在下面 ``CC_CREDITS_LOW``，两份一起钉住"余额下限"这条判据。
CC_CREDITS = {
    "credits": {
        "belowThreshold": False,
        "creditThreshold": 0,
        "monthlyCredits": 12.5,
        "purchasedCredits": 0,
        "freeCredits": 0,
    },
    "windowLimits": {
        "limited": True,
        "exceeded": None,
        "fiveHour": {"used": 0, "cap": 14, "exceeded": False, "resetAt": 0},
        "weekly": {"used": 0, "cap": 35, "exceeded": False, "resetAt": 0},
    },
}
#: 2026-10-28 实测的**真·见底**快照：all three credits ≈ 0.084、阈值 0、两档窗口都没超，
#: 但上游连 ``max_tokens: 1`` 都回 400 ``insufficient credits``（见 ``CREDITS_FLOOR_USD``）。
CC_CREDITS_LOW = {
    **CC_CREDITS,
    "credits": {
        "belowThreshold": False,
        "creditThreshold": 0,
        "monthlyCredits": 0.0840296077,
        "purchasedCredits": 0,
        "freeCredits": 0,
    },
}
CC_SUBS = {
    "success": True,
    "data": {
        "status": "active",
        "currentPeriodStart": "2026-10-04T14:00:31.000Z",
        "currentPeriodEnd": "2026-11-04T14:00:31.000Z",
        "planId": "individual-goat",
    },
}
#: 2026-10-26 实测形状（OpenCode Go）：三档窗口都是 ok
GO_USAGE = {
    "usage": {
        "rolling": {"status": "ok", "percent": 2, "resetsAt": "2026-10-26T15:15:16.000Z"},
        "weekly": {"status": "ok", "percent": 1, "resetsAt": "2026-11-02T00:00:00.000Z"},
        "monthly": {"status": "ok", "percent": 2, "resetsAt": "2026-11-24T07:48:38.000Z"},
    }
}


def cc_route() -> Route:
    return Route("commandcode", "m", "https://cc.invalid/v1", "CMD_API_KEY", quota="commandcode", quota_base=CC_BASE)


def go_route() -> Route:
    return Route("opencode-go", "m", "https://go.invalid/v1", "OPENCODE_API_KEY", quota="opencode-go", quota_base=GO_BASE)


def probe(route: Route, handler: Any) -> QuotaSource:
    return asyncio.run(probe_route(route, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))


@pytest.fixture(autouse=True)
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMD_API_KEY", "cmd-" + "a" * 20)
    monkeypatch.setenv("OPENCODE_API_KEY", "oc-" + "b" * 20)


def test_commandcode_reads_monthly_reset_from_the_plan(keys: None) -> None:
    """月度重置时刻 = 套餐的 ``currentPeriodEnd``（**不是** 5h/周窗口的 resetAt：那是速率闸门）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/alpha/billing/credits"):
            return httpx.Response(200, json=CC_CREDITS)
        if request.url.path.endswith("/alpha/billing/subscriptions"):
            return httpx.Response(200, json=CC_SUBS)
        return httpx.Response(404)

    source = probe(cc_route(), handler)
    assert source.ok
    assert source.plan_id == "individual-goat"
    assert source.monthly_reset_ms == 1_793_800_831_000  # 2026-11-04T14:00:31Z
    assert source.exhausted_windows == () and source.exhausted_until_ms is None


def test_exhausted_window_cools_until_its_own_reset(keys: None) -> None:
    """某一档打满时，冷到**那一档**的重置时刻（优先级高于套餐周期末）。"""
    body = {
        **CC_CREDITS,
        "windowLimits": {
            "fiveHour": {"used": 14, "cap": 14, "exceeded": True, "resetAt": 1_793_010_000_000},
            "weekly": {"used": 35, "cap": 35, "exceeded": True, "resetAt": 1_793_100_000_000},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/alpha/billing/credits"):
            return httpx.Response(200, json=body)
        return httpx.Response(500)

    source = probe(cc_route(), handler)
    assert source.exhausted_windows == ("fiveHour", "weekly")
    assert source.exhausted_until_ms == 1_793_100_000_000  # 取最晚的


def test_credits_below_threshold_counts_as_exhausted(keys: None) -> None:
    body = {**CC_CREDITS, "credits": {**CC_CREDITS["credits"], "belowThreshold": True}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/alpha/billing/credits"):
            return httpx.Response(200, json=body)
        return httpx.Response(200, json=CC_SUBS)

    source = probe(cc_route(), handler)
    assert "credits" in source.exhausted_windows
    assert source.exhausted_until_ms == source.monthly_reset_ms  # 余额档没有自己的 resetAt ⇒ 退到套餐周期末


def test_credits_floor_confirms_exhaustion_and_cools_until_the_period_end(keys: None) -> None:
    """余额低于 ``CREDITS_FLOOR_USD``（实态 0.084）⇒ 确认打满，冷却终点 = 套餐周期末。

    这是插件排查出来的那个坑：上游在"余额不够一次预扣"时就拒，而 ``belowThreshold`` 的阈值是 0，
    永远等不到它变真 —— 只看它，补查就永远"未确认"，只剩 5 分钟短闸门，每 5 分钟白撞一次。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/alpha/billing/credits"):
            return httpx.Response(200, json=CC_CREDITS_LOW)
        return httpx.Response(200, json=CC_SUBS)

    source = probe(cc_route(), handler)
    assert source.exhausted_windows == ("credits",)
    assert source.exhausted_until_ms == source.monthly_reset_ms == 1_793_800_831_000  # 11-04 22:00 北京


@pytest.mark.parametrize(
    ("monthly", "exhausted"),
    [(0.0, True), (0.0840296077, True), (0.199, True), (0.2, False), (12.5, False)],
)
def test_credits_floor_boundary(keys: None, monthly: float, exhausted: bool) -> None:
    """下限是 ``< 0.2``：0.199 算打满、0.2 整不算（与插件 0.12.0 的边界一致）。"""
    body = {**CC_CREDITS, "credits": {**CC_CREDITS["credits"], "monthlyCredits": monthly}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/alpha/billing/credits"):
            return httpx.Response(200, json=body)
        return httpx.Response(200, json=CC_SUBS)

    source = probe(cc_route(), handler)
    assert ("credits" in source.exhausted_windows) is exhausted


def test_missing_credit_fields_is_unknown_not_zero(keys: None) -> None:
    """字段缺失 = 不知道，**绝不能**当成 0（那会把一家其实能用的订阅白冷掉）。"""
    body = {"credits": {"belowThreshold": False}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/alpha/billing/credits"):
            return httpx.Response(200, json=body)
        return httpx.Response(200, json=CC_SUBS)

    assert probe(cc_route(), handler).exhausted_windows == ()


def test_opencode_go_uses_the_monthly_window(keys: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/usage")
        return httpx.Response(200, json=GO_USAGE)

    source = probe(go_route(), handler)
    assert source.ok
    assert source.monthly_reset_ms == 1_795_506_518_000  # 2026-11-24T07:48:38Z（见下方校验式）
    assert source.exhausted_windows == ()


def test_opencode_go_rate_limited_window_cools_until_its_reset(keys: None) -> None:
    body = json.loads(json.dumps(GO_USAGE))
    body["usage"]["monthly"]["status"] = "rate-limited"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    source = probe(go_route(), handler)
    assert source.exhausted_windows == ("monthly",)
    assert source.exhausted_until_ms == source.monthly_reset_ms


@pytest.mark.parametrize(
    ("status", "reason"),
    [(401, "auth"), (500, "http")],
)
def test_http_errors_are_recorded_not_raised(keys: None, status: int, reason: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "nope"})

    source = probe(cc_route(), handler)
    assert not source.ok and source.reason == reason
    assert source.monthly_reset_ms is None  # 查不到就是查不到，不编数


def test_bad_shape_and_network_errors_fail_open(keys: None) -> None:
    def bad_shape(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    assert probe(cc_route(), bad_shape).reason == "parse"

    def network(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert probe(cc_route(), network).reason == "network"


def test_route_without_quota_never_leaves_the_process(keys: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调用
        raise AssertionError("没有额度接口的一跳不应该发请求")

    source = probe(Route("ju-liu", "m", "https://j.invalid/v1", "CMD_API_KEY"), handler)
    assert source.reason == "no-quota-api"


def test_report_throttle_uses_the_earliest_reset_then_the_six_hour_cap(keys: None) -> None:
    """节流照插件 0.11 的两条闸门：查到 ⇒ 最早重置时刻（≤6 小时）；查不到 ⇒ 10 分钟。"""
    report = QuotaReport()
    routes = (cc_route(),)
    assert [route.provider for route in report.stale(routes)] == ["commandcode"]
    now = report._now()
    report._mark_retry("commandcode", now)
    assert report.stale(routes) == []  # 查不到：10 分钟内不再问
    assert report.valid_until_ms["commandcode"] == pytest.approx(now + QUOTA_TTL_MS)

    # 查到、且上游给了 5 小时后的窗口重置 ⇒ 有效期就是它（早于 6 小时上界）
    report.sources["commandcode"] = QuotaSource(provider="commandcode", ok=True, resets_ms=(now + 5 * 3_600_000,))
    report._mark_ok("commandcode", now)
    assert report.valid_until_ms["commandcode"] == pytest.approx(now + 5 * 3_600_000)

    # 一个重置时刻都拿不到 ⇒ 6 小时上界兜底（缓存不会永不过期）
    report.sources["commandcode"] = QuotaSource(provider="commandcode", ok=True)
    report._mark_ok("commandcode", now)
    assert report.valid_until_ms["commandcode"] == pytest.approx(now + QUOTA_MAX_TTL_MS)

    # inf = 永不刷新：连节流表都不看
    report.ttl_ms = float("inf")
    report.valid_until_ms.clear()
    assert report.stale(routes) == []


def test_refresh_one_bypasses_the_throttle_and_confirms_the_reset(keys: None) -> None:
    """额度类失败当下的「补查确认」只查这一家、绕缓存（插件 0.11.0 第二步同一条）。"""
    reset = 1_793_100_000_000
    body = {
        **CC_CREDITS,
        "windowLimits": {"weekly": {"used": 35, "cap": 35, "exceeded": True, "resetAt": reset}},
    }
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/alpha/billing/credits"):
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    report = QuotaReport(transport=httpx.MockTransport(handler))
    asyncio.run(report.refresh((cc_route(),)))
    assert calls.count("/alpha/billing/credits") == 1
    assert report.stale((cc_route(),)) == []  # 还在有效期内

    source = asyncio.run(report.refresh_one("commandcode", (cc_route(),)))
    assert source is not None and source.ok
    assert calls.count("/alpha/billing/credits") == 2, "refresh_one 必须绕过节流"
    assert report.cooldown_until_of("commandcode") == reset

    # 没有额度接口 / 不认识的 provider：不发请求，只回现有快照
    before = len(calls)
    assert asyncio.run(report.refresh_one("ju-liu", (cc_route(),))) is None
    assert len(calls) == before
