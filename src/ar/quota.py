"""额度探测：从上游自己的接口现查「这家什么时候回血、现在打满没有」（照 DSH 插件 ``lib/quota.js`` 的规矩）。

**为什么要它**：排序要按月度重置时刻排、额度打满要冷到重置 —— 这些时间**不该由人来填**。
插件就是直接问上游的，这里照同一套接口再来一遍：

| provider | 接口 | 取什么 | 打满判据 |
|---|---|---|---|
| `commandcode` | `GET {base}/alpha/billing/credits`（必需）+ `/alpha/billing/subscriptions`（可选） | 套餐 `data.currentPeriodEnd` | `windowLimits.*.exceeded === true`，或余额见底（`belowThreshold`，或 `credits.total` 低于 ``CREDITS_FLOOR_USD``） |
| `opencode-go` | `GET {base}/usage` | `usage.monthly.resetsAt` | 任一档 `status == 'rate-limited'` |

**纪律（逐条照插件，都是踩过的坑）**：

- **排序只看月度档**：5 小时 / 周窗口是速率闸门，按它排会让顺序每几小时抖一次；
- **耗尽判定看全部档**（含 5h / 周 / 余额）：冷却终点取「被耗尽那几档里**最晚**的重置时刻」；
- **取不到就「不知道」，绝不填 0 / 现在**：填"现在"会让它排到最前，填 0 会把「不知道」说成「用光了」；
- **fail-open**：断网 / 超时 / 形状不认 / 401 ⇒ 这一家当「查不到」，**不冷却**、排进「未知档」，
  宁可下次白撞一次，也不误伤一条其实能用的订阅；
- **节流照插件 0.11 的两条闸门**：查到 ⇒ 有效期 = 该家**最早**已知重置时刻（另挂 6 小时上界）；
  查不到 ⇒ ``QUOTA_TTL_MS``（10 分钟）后重试。**不是**固定 10 分钟轮询；
- **失败后补查**：额度类失败当下只查**这一家**、**绕缓存**（``refresh_one``），确认有档位耗尽才冷却；
- **查询本身带超时**；它是 ``await`` 的（不是后台任务）：AR 每次模型调用各自一个事件循环
  （``asyncio.run``），循环一关后台任务就被取消 —— 那等于永远查不成。代价是"该查的那一次"会
  多等一次往返（本机实测冷连接 3.7s / 热连接 2.0s，两家并行），而有效期是"最早重置时刻"（≤6 小时），
  所以一个进程最多等一次；
- **UA 防黑名单**：Command Code 的 Cloudflare 会按 `Python-urllib` 签名拦（`403 + error_code 1010`），
  默认带浏览器 UA —— 这不是伪装，是防误伤；真被拦会把原因如实记下来。

**密钥纪律**：额度接口与推理用同一把 key，这里只在内存里用于请求头，不进日志、不进报错。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .chain import Route, resolve_api_key

__all__ = [
    "CREDITS_FLOOR_USD",
    "QUOTA_TTL_MS",
    "QUOTA_MAX_TTL_MS",
    "QUOTA_TIMEOUT_S",
    "QUOTA_USER_AGENT",
    "QuotaSource",
    "QuotaReport",
    "probe_route",
]

#: 查询**失败**后的重试间隔（插件 ``DEFAULT_ORDERING_RETRY_MS`` 同值）。查到之后不再按它轮询，
#: 而是用「最早已知重置时刻」当有效期（见 ``QUOTA_MAX_TTL_MS``）
QUOTA_TTL_MS = 10 * 60 * 1000
#: 查**到**之后的有效期上界（插件 ``DEFAULT_ORDERING_TTL_MS`` 同值 6 小时）：一个重置时刻都
#: 拿不到时用它兜底，防止缓存永不过期
QUOTA_MAX_TTL_MS = 6 * 60 * 60 * 1000
#: Command Code 的**余额下限**（美元）：``credits.total`` 低于它就算「月度余额打满」。
#:
#: 为什么不能只看 ``belowThreshold``：上游那个开关的阈值是 ``creditThreshold = 0``，只能在余额**恰好
#: 归零**时变真；而它实际上在**余额不够这次请求的预扣**时就拒（2026-10-28 实测：``total = 0.084``、
#: 阈值 0、两档窗口 0/14 与 0/35 都没超，连 ``max_tokens: 1`` 都回 400 ``insufficient credits``）。
#: 于是补查永远「确认不了」⇒ 只留 5 分钟短闸门 ⇒ 每 5 分钟白撞一次。
#:
#: 0.2 与 DSH 插件 0.12.0 是同一个值、同一条理由（报告见 CommandCodeAI/command-code#1081）。
#: **代价**：余额须回到 0.2 以上才自动解冻，否则要等到周期末（本项目还没有「解除」入口）。
CREDITS_FLOOR_USD = 0.2
#: 单次查询超时（插件默认 8s：CC 的 subscriptions 冷连接 1.7~2.3s）
QUOTA_TIMEOUT_S = 8.0
#: 防 Cloudflare 签名黑名单用的浏览器 UA（插件同一条）
QUOTA_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

#: 认识的两家：kind → 默认的额度接口根（链上没写 quota_base 时用它）
QUOTA_BASES = {
    "commandcode": "https://api.commandcode.ai",
    "opencode-go": "https://opencode.ai/zen/go/v1",
}


@dataclass(frozen=True)
class QuotaSource:
    """一家的额度观测结果。``ok=False`` 时只有 ``reason`` / ``message`，其余全空（不编数）。"""

    provider: str
    ok: bool
    reason: str | None = None
    message: str | None = None
    #: 月度重置时刻（epoch 毫秒）；``None`` = 不知道
    monthly_reset_ms: float | None = None
    plan_id: str | None = None
    #: 打满的档位名（人读：`credits` / `fiveHour` / `weekly` / `monthly`）；空 = 没打满
    exhausted_windows: tuple[str, ...] = ()
    #: 被耗尽档位里最晚的重置时刻（epoch 毫秒）；``None`` = 不知道 ⇒ 不冷却
    exhausted_until_ms: float | None = None
    #: 这一家**所有**已知的重置时刻（月度 + 各窗口；epoch 毫秒，升序去重）。
    #: 排序键用月度那一个，缓存有效期用**最早**的那一个（插件 ``earliestResetOf`` 同口径）
    resets_ms: tuple[float, ...] = ()
    fetched_at: float = 0.0

    @property
    def earliest_reset_ms(self) -> float | None:
        """已知重置时刻里最早的那个（一个都没有 ⇒ ``None``：缓存只能靠 6 小时上界兜底）。"""
        return self.resets_ms[0] if self.resets_ms else None

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": "ok" if self.ok else "unavailable",
            "reason": self.reason,
            "message": self.message,
            "monthly_reset_ms": self.monthly_reset_ms,
            "plan_id": self.plan_id,
            "exhausted_windows": list(self.exhausted_windows),
            "exhausted_until_ms": self.exhausted_until_ms,
            "earliest_reset_ms": self.earliest_reset_ms,
        }


def _iso_ms(value: Any) -> float | None:
    """ISO 8601 串 / epoch 毫秒 → epoch 毫秒；认不出来给 ``None``（**绝不填 0**）。"""
    from datetime import datetime

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000.0
        except ValueError:
            return None
    return None


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _unavailable(provider: str, reason: str, message: str) -> QuotaSource:
    return QuotaSource(provider=provider, ok=False, reason=reason, message=message, fetched_at=time.time() * 1000)


async def _get_json(
    client: httpx.AsyncClient, url: str, api_key: str
) -> tuple[Any | None, str | None, str | None]:
    """一次 GET → ``(JSON, 失败原因码, 人读消息)``；任何失败都不抛。"""
    headers = {
        "authorization": f"Bearer {api_key}",
        "accept": "application/json",
        "user-agent": QUOTA_USER_AGENT,
    }
    try:
        response = await client.get(url, headers=headers)
    except httpx.TimeoutException:
        return None, "timeout", f"查询超时（{QUOTA_TIMEOUT_S:.0f}s）"
    except httpx.HTTPError as exc:
        return None, "network", f"网络请求失败：{type(exc).__name__}"
    if response.status_code != 200:
        reason = "auth" if response.status_code in (401, 403) else "http"
        return None, reason, f"上游返回 HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return None, "parse", "上游响应不是合法 JSON"
    if not isinstance(body, dict):
        return None, "parse", "上游响应不是 JSON 对象"
    return body, None, None


async def _probe_commandcode(client: httpx.AsyncClient, route: Route, api_key: str) -> QuotaSource:
    base = (route.quota_base or QUOTA_BASES["commandcode"]).rstrip("/")
    credits_body, reason, message = await _get_json(client, f"{base}/alpha/billing/credits", api_key)
    if credits_body is None:
        return _unavailable(route.provider, reason or "unknown", message or "额度查询失败")
    credits = credits_body.get("credits")
    if not isinstance(credits, dict):
        return _unavailable(route.provider, "parse", "credits 响应里没有 credits 对象")

    # 套餐（次要接口：失败只是拿不到 currentPeriodEnd，不影响"余额够不够"的结论）
    plan: dict[str, Any] = {}
    subs_body, _r, _m = await _get_json(client, f"{base}/alpha/billing/subscriptions", api_key)
    if isinstance(subs_body, dict) and isinstance(subs_body.get("data"), dict):
        plan = subs_body["data"]

    exhausted: list[str] = []
    latest_reset: list[float] = []
    #: 所有已知重置点（窗口自己的 resetAt 不论是否耗尽都收：缓存有效期要"最早的那个"）
    resets: list[float] = []
    window_limits = credits_body.get("windowLimits")
    if isinstance(window_limits, dict):
        for name in ("fiveHour", "weekly"):
            window = window_limits.get(name)
            if not isinstance(window, dict):
                continue
            at = _num(window.get("resetAt"))
            if at:
                resets.append(at)
            if window.get("exceeded") is True:
                exhausted.append(name)
                if at:
                    latest_reset.append(at)

    # 月度额度是「余额」而不是窗口：任一键缺失都算「不知道」，不当 0
    below = credits.get("belowThreshold") is True
    monthly = _num(credits.get("monthlyCredits"))
    purchased = _num(credits.get("purchasedCredits"))
    free = _num(credits.get("freeCredits"))
    total = None if None in (monthly, purchased, free) else monthly + purchased + free
    # 「余额见底」两条判据（与插件 0.12.0 一致）：上游自己说低于阈值，**或**总额低于 ``CREDITS_FLOOR_USD``。
    # 后者是必要的：上游在"余额不够一次预扣"时就拒，而它给的阈值是 0（见 ``CREDITS_FLOOR_USD`` 的注释）。
    if below or (total is not None and total < CREDITS_FLOOR_USD):
        exhausted.append("credits")

    period_end = _iso_ms(plan.get("currentPeriodEnd"))
    if period_end is not None:
        resets.append(period_end)
    # 退一步用套餐周期末**只对已耗尽的档**成立：没耗尽就不该有冷却终点（插件 exhaustedUntil 同一条）
    if exhausted and not latest_reset and period_end is not None:
        latest_reset.append(period_end)
    return QuotaSource(
        provider=route.provider,
        ok=True,
        monthly_reset_ms=period_end,
        plan_id=plan.get("planId") if isinstance(plan.get("planId"), str) else None,
        exhausted_windows=tuple(exhausted),
        exhausted_until_ms=max(latest_reset) if latest_reset else None,
        resets_ms=tuple(sorted(set(resets))),
        fetched_at=time.time() * 1000,
    )


async def _probe_opencode_go(client: httpx.AsyncClient, route: Route, api_key: str) -> QuotaSource:
    base = (route.quota_base or QUOTA_BASES["opencode-go"]).rstrip("/")
    body, reason, message = await _get_json(client, f"{base}/usage", api_key)
    if body is None:
        return _unavailable(route.provider, reason or "unknown", message or "额度查询失败")
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return _unavailable(route.provider, "parse", "usage 响应里没有 usage 对象")

    monthly_reset = _iso_ms((usage.get("monthly") or {}).get("resetsAt") if isinstance(usage.get("monthly"), dict) else None)
    exhausted: list[str] = []
    #: 所有窗口的重置时刻（不论是否耗尽：缓存有效期要用"最早的那个"）
    all_resets: list[float] = []
    #: 被耗尽那些窗口的重置时刻（冷却终点取最晚）
    exhausted_resets: list[float] = []
    for name in ("rolling", "weekly", "monthly"):
        window = usage.get(name)
        if not isinstance(window, dict):
            continue
        at = _iso_ms(window.get("resetsAt"))
        if at:
            all_resets.append(at)
        # 只看 status：上游把 percent 写死 100 但 status 不变时语义不明，宁可漏判（插件同口径）
        if window.get("status") == "rate-limited":
            exhausted.append(name)
            if at:
                exhausted_resets.append(at)
    return QuotaSource(
        provider=route.provider,
        ok=True,
        monthly_reset_ms=monthly_reset,
        exhausted_windows=tuple(exhausted),
        exhausted_until_ms=max(exhausted_resets) if exhausted_resets else monthly_reset,
        resets_ms=tuple(sorted(set(all_resets))),
        fetched_at=time.time() * 1000,
    )


async def probe_route(
    route: Route, *, client: httpx.AsyncClient | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> QuotaSource:
    """查一跳的额度（``route.quota`` 为空 = 不出门，直接当「没有额度接口」）。

    ``transport`` 只给测试 / 宿主已有的连接层用（``QuotaReport.transport`` 会带进来）。
    """
    if not route.quota:
        return _unavailable(route.provider, "no-quota-api", "这一跳没有配置额度接口（按量计费）")
    api_key, _origin = resolve_api_key(route)
    if not api_key:
        return _unavailable(route.provider, "no-credential", "没有可用的密钥，无法查询额度")

    own = client is None
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(QUOTA_TIMEOUT_S, connect=min(5.0, QUOTA_TIMEOUT_S)), transport=transport
    )
    try:
        if route.quota == "commandcode":
            return await _probe_commandcode(http, route, api_key)
        if route.quota == "opencode-go":
            return await _probe_opencode_go(http, route, api_key)
        return _unavailable(route.provider, "unknown-kind", f"不认识的额度接口类型：{route.quota}")
    except Exception as exc:  # noqa: BLE001 - 额度查询永远不许拖垮一次模型调用（fail-open）
        return _unavailable(route.provider, "probe-error", f"额度查询异常：{type(exc).__name__}")
    finally:
        if own:
            await http.aclose()


@dataclass
class QuotaReport:
    """一份额度快照（按 provider）+ 查询节流。``refresh()`` 只补查该查的，``snapshot`` 同步读。

    **节流口径（插件 0.11 的 ``createOrderRefresh`` 同一条）**：

    - 查到（``ok``）⇒ 有效期 = 该家**最早**已知重置时刻，另挂 ``QUOTA_MAX_TTL_MS``（6 小时）上界；
    - 查不到 / 还没查 ⇒ ``QUOTA_TTL_MS``（10 分钟）后再试（"查过"的判据是**这一轮真的拿到**
      ``status: ok``；失败占了长有效期就等于把"没查到"当成"查到了"）；
    - ``ttl_ms = inf`` ⇒ **永不刷新**（测试 / 已手工填好快照时用）：``refresh`` 与 ``refresh_one``
      都不发请求，只读现有快照。
    """

    sources: dict[str, QuotaSource] = field(default_factory=dict)
    #: provider → 这个时刻之前不再问它（查到 = 最早重置时刻 / 6 小时，查不到 = 10 分钟）
    valid_until_ms: dict[str, float] = field(default_factory=dict)
    #: 查不到时的重试间隔（毫秒）；测试可放大 / 调小
    ttl_ms: float = QUOTA_TTL_MS  # float("inf") = 不刷新
    #: 查到之后的有效期上界（毫秒）：一个重置时刻都拿不到时用它兜底
    max_ttl_ms: float = QUOTA_MAX_TTL_MS
    #: 查询用的连接层（``ModelClient`` 会把自己那份传进来；``None`` = 每次现建一个）
    transport: httpx.AsyncBaseTransport | None = None

    def _now(self) -> float:
        return time.time() * 1000

    def forget(self) -> None:
        """把节流全清掉（显式强制刷新用）：下一次 ``refresh`` 一定会问一遍。"""
        self.valid_until_ms.clear()

    def stale(self, routes: tuple[Route, ...]) -> list[Route]:
        """该去问一遍的跳（没配 quota 的不算；还在有效期内的也不算）。"""
        if self.ttl_ms == float("inf"):
            return []
        now = self._now()
        out: list[Route] = []
        for route in routes:
            if not route.quota:
                continue
            until = self.valid_until_ms.get(route.provider)
            if until is None or now >= until:
                out.append(route)
        return out

    def _mark_ok(self, provider: str, now: float) -> None:
        """查到之后的有效期：该家最早的重置时刻（≤6 小时）；没有重置时刻就用 6 小时。"""
        until = now + self.max_ttl_ms
        source = self.sources.get(provider)
        earliest = source.earliest_reset_ms if source is not None and source.ok else None
        if earliest is not None and now < earliest < until:
            until = earliest
        self.valid_until_ms[provider] = until

    def _mark_retry(self, provider: str, now: float) -> None:
        self.valid_until_ms[provider] = now + self.ttl_ms

    async def refresh(self, routes: tuple[Route, ...]) -> None:
        """补查该查的跳（并行、逐跳隔离失败；**永不抛**）。"""
        targets = self.stale(routes)
        if not targets:
            return
        now = self._now()
        for route in targets:
            # 先占短闸门再查：① 同一轮里并发触发的补查只打一次上游 ② 万一没落定，也只占 10 分钟
            self._mark_retry(route.provider, now)
        results = await asyncio.gather(
            *(probe_route(route, transport=self.transport) for route in targets), return_exceptions=True
        )
        for route, result in zip(targets, results):
            if not isinstance(result, QuotaSource):
                continue
            self.sources[route.provider] = result
            if result.ok:  # 失败的那家继续留在查询前占好的短闸门里
                self._mark_ok(route.provider, now)

    async def refresh_one(self, provider: str, routes: tuple[Route, ...]) -> QuotaSource | None:
        """只查这一家、**绕缓存**（额度类失败当下的"补查确认"，插件 0.11.0 第二步同一条）。

        没有额度接口 / 链上没有这家 / ``ttl_ms == inf`` ⇒ 只回现有快照（``None`` = 没查过），不发请求。
        """
        route = next((item for item in routes if item.provider == provider), None)
        if route is None or not route.quota or self.ttl_ms == float("inf"):
            return self.sources.get(provider)
        source = await probe_route(route, transport=self.transport)
        self.sources[provider] = source
        now = self._now()
        if source.ok:
            self._mark_ok(provider, now)
        else:
            self._mark_retry(provider, now)
        return source

    def reset_of(self, provider: str) -> float | None:
        """这一家的月度重置时刻（没查到 / 查失败 → ``None``）。"""
        source = self.sources.get(provider)
        return None if source is None or not source.ok else source.monthly_reset_ms

    def cooldown_until_of(self, provider: str) -> float | None:
        """打满时该冷到什么时候；没打满或不知道终点 → ``None``（不冷却，fail-open）。"""
        source = self.sources.get(provider)
        if source is None or not source.ok or not source.exhausted_windows:
            return None
        return source.exhausted_until_ms

    def public_list(self) -> list[dict[str, Any]]:
        return [source.public_dict() for source in self.sources.values()]
