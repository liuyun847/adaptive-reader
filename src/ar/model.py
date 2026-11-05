"""模型客户端：OpenAI 兼容 ``POST {base_url}/chat/completions``（流式）。

本模块只做「**发一次请求并把它看懂**」：

- 拼 URL 与请求体、发一次、解析 SSE（或网关无视 ``stream`` 直接回 JSON 时兜底解析）、拼增量；
- **多源回退链**（配置了 ``routes`` 时）：``arequest_once`` 按链逐跳尝试——某跳失败就换下一跳
  （唯一不换的是「一个内容分片都还没交出去」之外的调用方取消，见 ``ar.chain`` 的换跳判据），
  冷却中的跳默认跳过、全冷时照链硬走；每一跳的密钥在发请求那一刻现读；
- ``stream_options.include_usage`` 被网关拒绝时**自动关掉该参数重发一次**（参数降级**不占用**重试额度）；
- **错误码归一**（PLAN-llm-tools §3.2）：把 HTTP 状态 / 传输异常 / 流内错误 / 空正文 / 配额文案
  映射成 ``AUTH`` / ``INVALID_REQUEST`` / ``RATE_LIMIT`` / ``SERVER`` / ``TIMEOUT`` / ``TRANSPORT`` /
  ``EMPTY_RESPONSE`` / ``QUOTA`` / ``NOT_CONFIGURED`` / ``UNKNOWN``；**重试不在这里做**——
  重试策略与逐次记账在 ``ar.llm``（``arequest_once`` 是它要的「只发一次、返回 ``LlmResult``、不抛」入口）。
  另有 ``QUOTA_RATE_WINDOW``（窗口额度耗尽，插件 0.9.1 口径）：链上**不原地重试、直接换下一跳**，
  并照插件 0.11 的顺序冷却（先 5 分钟短闸门，再补查确认，确认则冷到该档重置）；单源模式靠
  ``ar.llm`` 对它无条件不重试。整条链都失败时错误码固定为 ``AUTO_ROUTES_EXHAUSTED``。

对外外观保持不变（四个业务模块还在用）：

- ``chat()`` / ``achat()``：经 ``ar.llm.acall`` 按策略重试，**失败抛 ``ModelCallFailed``**（消息已脱敏）、
  未配置抛 ``ModelNotConfigured``；``ModelReply`` 的字段一个不少。

参数面照 ``test_api/ttft-bench/bench.mjs`` 的实测口径：``stream: true`` +
``stream_options.include_usage`` + 单次超时。

**密钥纪律**：密钥只在运行时从 ``AR_API_KEY`` 或 ``config.local.json`` 读入内存；
本模块任何异常消息都过 ``redact()``，不含密钥值，可安全打印 / 落盘 / 进 HTTP 响应。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Iterable

import httpx
from httpx_sse import aconnect_sse

from .chain import (
    DEFAULT_COOLDOWN_MS,
    DEFAULT_GATE_MS,
    ORDERING_MODES,
    QUOTA_CODES,
    SHORT_GATE_MS,
    CooldownTable,
    OrderedRoute,
    cooldown_file_path,
    Route,
    order_routes,
    resolve_api_key,
)
from .quota import QuotaReport
from .config import Config, redact
from .errors import ModelUnavailable
from .llm import (
    LlmFailure,
    LlmRequest,
    LlmResult,
    RetryPolicy,
    acall,
    not_configured_result,
    resolve_retry_policy,
    should_retry,
)

__all__ = [
    "ModelReply",
    "ModelClient",
    "RouteOutcome",
    "SkippedRoute",
    "ModelNotConfigured",
    "ModelCallFailed",
    "extract_content",
    "strip_reasoning",
    "cached_tokens_of",
    "classify_failure",
    "failure_code_for_status",
    "looks_like_quota",
    "looks_like_window_quota",
    "parse_retry_after",
    "CHAIN_EXHAUSTED_CODE",
]

#: 整条回退链都失败时的错误码（插件 ``EXHAUSTED_CODE`` 同值）：稳定的、可路由的码。
#: 具体哪一跳怎么败的写进 ``failure.message``（逐跳一行）—— 不把末跳的码当成整条链的结论。
CHAIN_EXHAUSTED_CODE = "AUTO_ROUTES_EXHAUSTED"

_UNSET: Any = object()
_REJECTION_MARKERS = ("stream_options", "include_usage")

#: HTTP 状态 → 错误码（§3.2 的表）；表外由 ``failure_code_for_status`` 收尾
_STATUS_CODES: dict[int, str] = {
    400: "INVALID_REQUEST",
    401: "AUTH",
    403: "AUTH",
    413: "INVALID_REQUEST",
    422: "INVALID_REQUEST",
    429: "RATE_LIMIT",
}

#: 网关明确报「额度用尽」的文案（配额类**重试无用**，必须与限速区分开）
_QUOTA_MARKERS = (
    "insufficient quota",
    "quota exceeded",
    "exceeded your current quota",
    "insufficient_balance",
    "insufficient balance",
    "billing hard limit",
    # 订阅式上游的实际文案（2026-10-26 实测）："You've reached your weekly usage limit for your plan"
    "usage limit",
    "usage_limit",
    "quota exhausted",
    # 余额型文案（commandcode 实测：insufficient credits to make this request）
    "insufficient credits",
    "credits to make this request",
    "余额不足",
    "额度不足",
    "配额不足",
    "额度已用尽",
    "欠费",
)


#: 「某个窗口的额度用完了」的判据（照插件 ``lib/errors.js`` 的 ``isQuotaWindowMessage``，同一套词表）：
#: ① ``insufficient quota/balance``；② ``(quota|usage limit) exceeded/exhausted/reached``；
#: ③ **窗口词 + 额度词 + 耗尽/重置词三者同时成立**（词序不限）—— 覆盖 Command Code 的实际文案
#: ``You've reached your weekly usage limit for your plan. Your limit resets at …``。
#: 刻意要求窗口语义：``429 rate limit exceeded, retry after 5s`` 这类瞬时速率限制不算。
#: 与余额类（``insufficient credits``）分界：后者是真打满，归 ``QUOTA``（要补查、要冷却）。
_QUOTA_WINDOW_RE = re.compile(
    r"(?:daily|weekly|monthly|hourly|rolling|5[-_ ]?hour|five[-_ ]?hour)", re.I
)
_QUOTA_AMOUNT_RE = re.compile(r"(?:quota|usage[-_ ]?limit|\blimits?\b|credits?)", re.I)
_QUOTA_SPENT_RE = re.compile(
    r"(?:reached|exceeded|exhausted|\bhit\b|used up|no (?:remaining|left)|resets? (?:at|in)\b)",
    re.I,
)

#: 历史消息里的「思考」字段名（DeepSeek 原生是 ``reasoning_content``，OpenAI 风格是 ``reasoning``）
_REASONING_KEYS = ("reasoning_content", "reasoning")


def strip_reasoning(messages: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """摘掉历史消息里的思考字段（换跳前用）：别的模型生成的思考喂给下一家，会被摊成正文或直接 400。"""
    cleaned: list[dict[str, str]] = []
    for message in messages:
        cleaned.append({key: value for key, value in message.items() if key not in _REASONING_KEYS})
    return cleaned


def cached_tokens_of(usage: Any) -> int | None:
    """从一次调用的 ``usage`` 里取「缓存命中的 prompt token 数」；**取不到返回 ``None``**。

    网关字段形态不止一种，按优先级取第一个能用的：

    1. OpenAI 风格 ``usage.prompt_tokens_details.cached_tokens``（本机网关实测回的就是它）；
    2. 扁平字段 ``usage.prompt_cache_hit_tokens``（DeepSeek 原生字段名）；
    3. 扁平字段 ``usage.cached_tokens``。

    ``None`` 与 ``0`` 必须分得开：``None`` = 这次没上报（不能编造成 0），``0`` = 上报了但没命中。
    """
    if not isinstance(usage, dict):
        return None
    details = usage.get("prompt_tokens_details")
    value = details.get("cached_tokens") if isinstance(details, dict) else None
    if not isinstance(value, int) or isinstance(value, bool):
        value = None
        for key in ("prompt_cache_hit_tokens", "cached_tokens"):
            candidate = usage.get(key)
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                value = candidate
                break
    return value


class ModelNotConfigured(ModelUnavailable):
    """没有密钥（或 base_url/model 为空）：调用前就会抛出，不产生真实请求。"""


class ModelCallFailed(ModelUnavailable):
    """调用失败（超时 / HTTP 错误 / 流内错误 / 空回复）。消息已脱敏。"""


@dataclass(frozen=True)
class ModelReply:
    """一次模型调用的结果（只保留业务需要的字段）。"""

    text: str
    model: str | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    degraded_params: tuple[str, ...] = ()


def extract_content(payload: Any) -> str:
    """从 ``delta`` 或 ``message`` 里取正文：支持字符串与 parts 数组两种形态。"""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("content")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            part.get("text", "") for part in value if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


# ------------------------------------------------------------------ 错误码归一 --
def looks_like_quota(message: str) -> bool:
    """这段（已脱敏的）文案是不是「额度用尽」类（配额，重试无用）。"""
    lowered = (message or "").lower()
    return any(marker in lowered for marker in _QUOTA_MARKERS)


def looks_like_window_quota(message: str) -> bool:
    """这段（已脱敏的）文案是不是在说**某个窗口的额度用完了**（插件 isQuotaWindowMessage 同口径）。

    与 looks_like_quota 的区别：这里只认**带窗口语义**的写法（或那两条固定句式），
    瞬时速率限制（rate limit exceeded, retry after 5s，一个窗口词都没有）不算。
    两个下游各用一次：

    - classify_failure：命中就归 QUOTA_RATE_WINDOW（不重试；**冷却**，且按被耗尽那几档的重置时刻冷）；
    - ModelClient._request_once：链模式下**不原地重试**（重试 5 次也等不到窗口重置，
      纯粹白撞上游 + 白等退避——插件 0.9.1 的修复）。
    """
    text = message or ""
    if not text:
        return False
    # 插件 errors.js 的 ① 条：「insufficient quota / balance」= 余额级（与 looks_like_quota 同源）。
    # 「insufficient credits」刻意**不算**窗口级：它是余额打满（归 QUOTA，要补查与冷却）。
    lowered = text.lower()
    if "insufficient quota" in lowered or "insufficient balance" in lowered:
        return True
    if re.search(
        r"(?:quota|usage[-_ ]?limit) (?:exceeded|exhausted|reached)", text, re.I
    ):
        return True
    return bool(
        _QUOTA_WINDOW_RE.search(text)
        and _QUOTA_AMOUNT_RE.search(text)
        and _QUOTA_SPENT_RE.search(text)
    )


def failure_code_for_status(status: int) -> str:
    """HTTP 状态 → 错误码（§3.2 的表）：401/403 → AUTH，400/413/422 → INVALID_REQUEST，
    429 → RATE_LIMIT，5xx → SERVER，其余 → UNKNOWN（**刻意不重试**：拿不到码宁可不动）。"""
    mapped = _STATUS_CODES.get(int(status))
    if mapped is not None:
        return mapped
    if 500 <= int(status) <= 599:
        return "SERVER"
    return "UNKNOWN"


def classify_failure(status: int | None, message: str) -> LlmFailure:
    """HTTP 状态 + 已脱敏的响应摘要 → 归一后的失败。

    判定顺序：401/403 先归 AUTH（鉴权就是鉴权）；「额度用尽」文案归 QUOTA（**独立于状态码、先于
    状态表判**：本机实测 commandcode 余额耗尽回 400 invalid_request_error 加 "insufficient credits"，
    opencode-go 额度用完回 429——只看状态码会把它当参数错误 / 限速，在多源链上照样白撞）；
    然后按状态表；状态缺失时归 UNKNOWN。「窗口额度耗尽」（looks_like_window_quota）另归
    ``QUOTA_RATE_WINDOW``：不重试，但**照旧冷却**（见 ``ar.chain.QUOTA_CODES``）。
    """
    if status is not None and int(status) in (401, 403):
        return LlmFailure(code="AUTH", message=message, status=int(status))
    if looks_like_quota(message):
        # 余额级（真正打满）：要补查确认、要冷却——但它含窗口写法时换跳不重试，见 _request_over_chain。
        # 余额类与窗口类的先后顺序刻意是「余额先判」：`insufficient credits` 这类是余额，归 QUOTA。
        return LlmFailure(code="QUOTA", message=message, status=None if status is None else int(status))
    if looks_like_window_quota(message):
        # 窗口额度耗尽单列一码：**不重试**（等不到窗口重置）；冷却与余额类一样（先短闸门、再补查确认、
        # 确认则冷到该档重置）。注意 CC 的真实周窗文案 ``You've reached your weekly usage limit`` 含
        # "usage limit"，会被上面的 looks_like_quota 认领成 QUOTA —— 两条路现在冷却行为相同，不影响结果。
        # 顺序：余额在前、窗口在后；401/403 更在前（鉴权就是鉴权）。
        return LlmFailure(
            code="QUOTA_RATE_WINDOW",
            message=message,
            status=None if status is None else int(status),
        )
    if status is None:
        return LlmFailure(code="UNKNOWN", message=message)
    return LlmFailure(
        code=failure_code_for_status(int(status)), message=message, status=int(status)
    )


def classify_exception(exc: BaseException, message: str) -> LlmFailure:
    """传输层异常 → 归一后的失败：超时 → TIMEOUT，连接 / 协议错误 → TRANSPORT，其余 UNKNOWN。"""
    if isinstance(exc, httpx.TimeoutException):
        return LlmFailure(code="TIMEOUT", message=message)
    if isinstance(exc, httpx.HTTPError):
        return LlmFailure(code="TRANSPORT", message=message)
    return LlmFailure(code="UNKNOWN", message=message)


def parse_retry_after(value: str | None) -> int | None:
    """``Retry-After`` 响应头 → 毫秒（认不出来返回 ``None``）。

    标准头是**秒**（可带小数），本项目统一用毫秒（与 ``LlmFailure.retry_after_ms`` 口径一致）；
    HTTP-date 形态不解析（返回 ``None``，退回本地退避）。
    """
    if not value:
        return None
    try:
        seconds = float(str(value).strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return int(round(seconds * 1000))


def _status_of(error: Any) -> int | None:
    """从流内 / 响应体里的 ``error`` 对象上找 HTTP 状态（找不到返回 ``None``）。"""
    if not isinstance(error, dict):
        return None
    for key in ("status", "status_code", "http_status", "code"):
        value = error.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 400 <= value <= 599:
            return value
        if isinstance(value, str) and value.strip().isdigit() and 400 <= int(value.strip()) <= 599:
            return int(value.strip())
    return None


@dataclass(frozen=True)
class SkippedRoute:
    """链上**一次请求都没发**就被跳过的一跳（``ModelClient.skipped``；不含密钥）。

    ``reason`` 就是跳过它的那条记账的理由：``QUOTA`` / ``NOT_CONFIGURED``（冷却中）或
    ``gate``（同一次调用里刚被 429 短闸门挡下）。
    """

    provider: str
    model: str
    reason: str


@dataclass(frozen=True)
class RouteOutcome:
    """链上一次失败的账（``route_outcomes()`` 用；不含密钥）。"""

    provider: str
    model: str
    code: str
    message: str
    #: 这一次跳**最后一试**的耗时（毫秒；不是整条链的累计——插件 ``elapsedMs`` 同一口径）
    elapsed_ms: int | None = None
    #: 这一跳在本次调用里一共发了几次（正常 1；被外层重试时由 ``_note_failure`` 累加）
    tries: int = 1


class ModelClient:
    """同步 / 异步两种用法共用一个实现（异步为核心，同步只是 ``asyncio.run`` 包装）。

    配置了 ``routes`` 就是**多源回退链**模式：``arequest_once`` 自己按链逐跳尝试，
    外层 ``ar.llm.acall`` 的路由内重试只管「整条链都试完还失败」之后的再次出发。
    """

    def __init__(
        self,
        config: Config,
        *,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        cooldowns: CooldownTable | None = None,
        cooldown_ms: int = DEFAULT_COOLDOWN_MS,
        gate_ms: int = DEFAULT_GATE_MS,
        quota_report: QuotaReport | None = None,
        persist_cooldowns: bool = True,
    ) -> None:
        self.config = config
        # 单源模式的密钥；多源模式下这个字段只用于「客户端层面的整体可用性」判断，不参与发请求
        self._api_key = config.api_key if api_key is None else api_key
        self._transport = transport
        self._use_stream_options = True
        # 重试策略在这里就解析好：配置非法要早点报（CLI 退出码 2），而不是等第一次调用
        self._policy = resolve_retry_policy(config.retry)
        # 回退链的冷却表：同一份配置对象下复用同一个客户端实例时，冷却才跨调用生效（CLI / serve）。
        # 默认落盘（``~/.dsh/storages/adaptive_reader_cooldowns.json``）：重启后接着冷，不再白撞一次；
        # 落盘失败只记 ``persist_error``，不影响路由（照插件 0.11 的 fail-open）。
        self._cooldowns = (
            cooldowns
            if cooldowns is not None
            else CooldownTable(persist_path=cooldown_file_path() if persist_cooldowns else None)
        )
        # 额度快照（排序键与「冷到重置」都来自它）；同一实例跨调用复用，才有那份查询节流。
        # 连接层跟着客户端走：额度查询与推理用同一套 transport（测试的 MockTransport 因此也能盖住补查）
        self._quota = quota_report if quota_report is not None else QuotaReport()
        if self._quota.transport is None:
            self._quota.transport = transport
        self._cooldown_ms = max(0, int(cooldown_ms))
        self._gate_ms = max(0, int(gate_ms))
        #: 最近一次 ``arequest_once`` 里每跳失败的账（成功的那跳不记；只用于报错文案与排查）
        self._route_failures: list[RouteOutcome] = []
        #: 最近一次 ``arequest_once`` 里**没发请求就跳过**的跳（冷却中 / 本轮短闸门），见 ``SkippedRoute``
        self.skipped = []
        #: 最近一次 ``arequest_once`` 里通过排序的行（=``self.skipped`` 之外的候选 + 冷却已到期的兜底）
        self.attempted: list[str] = []

    # ---------------------------------------------------------------- 状态 --
    @property
    def routes(self) -> tuple[Route, ...]:
        return self.config.routes

    @property
    def configured(self) -> bool:
        if self.config.routes:
            # 链上至少一跳配了密钥名就算可用：具体哪跳能通要发了才知道（不该因为某家没密钥就不发）
            return bool(self.config.model_configured) and any(route.api_key_env for route in self.config.routes)
        return bool(self._api_key) and bool(self.config.base_url) and bool(self.config.model)

    @property
    def cooldowns(self) -> CooldownTable:
        """冷却表（``/api/health`` 与排查用）。

        进链时会把落盘的那份读回来（见 ``_request_over_chain``），所以它**不是**进程内的临时状态：
        重启后第一条请求就吃得到上一次的冷却。
        """
        return self._cooldowns

    def now_ms(self) -> float:
        """与冷却表同一口径的当前时刻（毫秒单调时钟）。"""
        return self._now()

    def route_outcomes(self) -> tuple[RouteOutcome, ...]:
        """最近一次多源调用里失败的跳（按尝试顺序；单源模式恒为空）。"""
        return tuple(self._route_failures)

    @property
    def stream_options_enabled(self) -> bool:
        """``stream_options.include_usage`` 是否仍然开启（被网关拒绝后永久关掉）。"""
        return self._use_stream_options

    def retry_policy(self) -> RetryPolicy:
        """本次客户端的重试策略（配置的 ``retry`` 段；没配就是 §2 的默认值）。"""
        return self._policy

    def _redact(self, text: Any, secrets: list[str] | None = None) -> str:
        return redact(text, secrets=[self._api_key, *(secrets or [])])

    def _headers(self, route: Route | None = None) -> dict[str, str]:
        """请求头：多源模式下 ``route`` 给那一跳的密钥与自定义头，单源模式用 ``config.api_key``。"""
        if route is None:
            return {
                "authorization": f"Bearer {self._api_key}",
                "content-type": "application/json",
                "accept": "text/event-stream",
            }
        api_key, _ = resolve_api_key(route)
        headers = {
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        headers.update(dict(route.headers))
        return headers

    def _url(self, route: Route | None = None) -> str:
        root = route.base_url if route is not None else self.config.base_url
        return str(root or "").rstrip("/") + "/chat/completions"

    def _model(self, route: Route | None = None) -> str:
        return route.model if route is not None else self.config.model

    # ---------------------------------------------------------------- 调用 --
    def chat(self, messages: Iterable[dict[str, str]], **kwargs: Any) -> ModelReply:
        """同步包装（CLI 用）；不要在已有事件循环里调用。"""
        return asyncio.run(self.achat(messages, **kwargs))

    async def achat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = _UNSET,
    ) -> ModelReply:
        """发一次对话请求（内部按策略重试）；**失败抛 ``ModelCallFailed``**（消息已脱敏）。

        ``max_tokens`` 缺省（``_UNSET``）时用配置里的值；显式传 ``None`` = 请求体不带该字段
        （四个业务模块就是这么调的）。
        """
        limit = self.config.max_tokens if max_tokens is _UNSET else max_tokens
        request = LlmRequest(
            tool="chat",
            messages=tuple(dict(message) for message in messages),
            temperature=temperature,
            max_tokens=limit,
        )
        result = await acall(request, client=self, policy=self.retry_policy())
        if result.ok:
            return ModelReply(
                text=result.text,
                model=result.model,
                finish_reason=result.finish_reason,
                usage=result.usage,
                degraded_params=result.degraded_params,
            )
        failure = result.failure or LlmFailure(code="UNKNOWN", message="模型调用失败（没有给出原因）")
        if failure.code == "NOT_CONFIGURED":
            raise ModelNotConfigured(failure.message)
        raise ModelCallFailed(f"模型调用失败（已试 {max(1, len(result.attempts))} 次）：{failure.message}")

    def _single_source_window_quota(self, result: LlmResult) -> LlmResult:
        """单源模式下的「窗口额度耗尽不重试」（与链模式同一判据、同一理由，见 ``_request_over_chain``）。

        只对**窗口级**（``looks_like_window_quota`` 为真）的失败动手：把码换成 ``QUOTA_RATE_WINDOW``，
        它不在默认重试白名单里，且 ``ar.llm`` 的 should_retry 对它无条件返回 False。余额级（``QUOTA``）
        与鉴权（``AUTH``）原样放行。文案照旧原样给出（脱敏已在上游做过）。
        """
        failure = result.failure
        if result.ok or failure is None:
            # 这次成功了：没什么可归一
            return result
        # 先判码，再判文案：只有 QUOTA_RATE_WINDOW / RATE_LIMIT 这两种有可能是「窗口额度耗尽」，
        # QUOTA（余额打满）与 AUTH 一律原样放行——这是这条规则的分界线。
        if failure.code not in ("QUOTA_RATE_WINDOW", "RATE_LIMIT"):
            return result
        if not looks_like_window_quota(failure.message):
            return result
        return replace(
            result,
            failure=replace(failure, code="QUOTA_RATE_WINDOW", message=f"{failure.message}（窗口额度耗尽，不重试）"),
        )

    def _failures_reset(self) -> None:
        """一轮调用开始时清空三本账：失败账、跳过账、本轮排上的跳。

        三本账集中在一处重建：调用方每轮开头调一次，别处只追加、不再重新绑定（重新绑定会让
        已经指向旧列表的引用继续往旧列表里写，账就分叉了）。
        """
        self._route_failures = []
        self.skipped = []
        self.attempted = []

    async def arequest_once(self, request: LlmRequest) -> LlmResult:
        """**只发一次**（不重试、不抛）：``stream_options`` 被拒时关掉该参数重发一次。

        ``ar.llm.acall`` 的唯一调用入口；参数降级（关 ``stream_options``）**不占重试额度**，
        降级这件事写进 ``LlmResult.degraded_params``，由调用方决定怎么记账。
        """
        # 账本只在**一轮调用**开始时清空（列表整体重建）。
        self._failures_reset()
        if not self.configured:
            if self.config.routes:
                return not_configured_result(
                    f"回退链上没有一跳可用：{[route.label for route in self.config.routes]} 都缺密钥名"
                )
            return not_configured_result()
        if self.config.routes:
            result = await self._request_over_chain([dict(message) for message in request.messages], request)
        else:
            # 每次尝试都从原始请求重建消息（重发不带上一次的残留）
            result = await self._request_once([dict(message) for message in request.messages], request)
        # 只有单源需要这道归一（链模式里由 _request_over_chain 自己处理窗口额度）；
        # 链模式的结果带 skipped，原样返回即可。
        if not self.config.routes:
            result = self._single_source_window_quota(result)  # 单源专有：链模式里已由换跳逻辑处理
        if not self._stream_options_rejected(result) or not self._use_stream_options:
            return result
        # 走到这里说明 stream_options 被网关拒绝：关掉它再发一次（参数降级，不占重试额度）
        self._use_stream_options = False
        note = "stream_options.include_usage 被网关拒绝，已关闭该参数重试"
        if self.config.routes:
            again = await self._request_over_chain([dict(message) for message in request.messages], request)
        else:
            again = await self._request_once([dict(message) for message in request.messages], request)
            again = self._single_source_window_quota(again)
        if again.ok:
            return replace(again, degraded_params=(note,))
        failure = again.failure or LlmFailure(code="UNKNOWN", message="模型调用失败（没有给出原因）")
        return replace(again, failure=replace(failure, message=f"{note}；重发仍失败：{failure.message}"))

    # ---------------------------------------------------------------- 回退链 --
    @property
    def ordering_mode(self) -> str:
        """排序模式：``auto``（默认，插件口径）或 ``manual`（严格按配置顺序、不跳过任何一家）。"""
        mode = str(self.config.ordering or "auto").strip().lower()
        return mode if mode in ORDERING_MODES else "auto"

    @property
    def quota_report(self) -> QuotaReport:
        """额度快照（``/api/health``、``ar routes show`` 读它）。"""
        return self._quota

    async def refresh_quota(self, *, force: bool = False) -> None:
        """补查该查的额度（有节流，不会每次请求都打上游；**永不抛**）。

        节流口径见 ``ar.quota.QuotaReport``：查到就按"最早已知重置时刻"（≤6 小时）再问，
        查不到才 10 分钟后再试 —— 不是固定频率轮询。

        ``force=True`` 只给显式命令（``ar routes show``）用：那是人在看，值得多打一次上游。
        """
        if not self.config.routes:
            return
        if force:
            # 绕节流：把有效期清掉，刷新就一定会问一遍（失败也只覆盖成 unavailable，不影响 fail-open）
            self._quota.forget()
        await self._quota.refresh(self.config.routes)

    def chain_order(self) -> list[OrderedRoute]:
        """这次请求要按什么顺序试（**只排序，不丢跳**；``cooling`` 标出冷却中的条目）。"""
        if self.ordering_mode == "manual":
            return [
                OrderedRoute(route=route, reason="unknown", reset_at_ms=self._quota.reset_of(route.provider))
                for route in self.config.routes
            ]
        return order_routes(self.config.routes, self._quota, set(self._cooldowns.active()))

    def _chain_order(self) -> list[Route]:
        """真正会发请求的顺序：``manual`` 严格按配置；``auto`` 跳过冷却中的家，
        但**全冷时忽略冷却照链硬走**（兜底：避免假故障「链已用尽」）。"""
        ordered = self.chain_order()
        if self.ordering_mode == "manual":
            return [item.route for item in ordered]
        # 记账与过滤共用同一份数据：被冷却挡下的跳在这里就落账（reason 取冷却表里那条记录的理由，
        # 如 ``QUOTA`` / ``NOT_CONFIGURED``），不依赖循环里再判一次。
        cooling = self._cooldowns.active()
        for item in ordered:
            if item.cooling:
                entry = cooling.get(item.route.provider)
                reason = entry.reason if entry is not None else "cooldown"
                self.skipped.append(
                    SkippedRoute(provider=item.route.provider, model=item.route.model, reason=reason)
                )
        usable = [item.route for item in ordered if not item.cooling]
        return usable or [item.route for item in ordered]

    def _note_failure(self, route: Route, failure: LlmFailure) -> None:
        """记一笔跳失败（**只记账，不碰冷却表**：冷却是 ``_apply_cooldown`` / ``_confirm_quota_failure`` 的事）。

        同一跳可能失败多次（本次调用里换跳重来，或外层重试又走一遍）：只占一行、次数累加、
        耗时取最后一次。账放在客户端实例上（跨多次 arequest_once 累积），由下一次调用开头清空。
        """
        for index, existing in enumerate(self._route_failures):
            if existing.provider == route.provider and existing.model == route.model:
                self._route_failures[index] = RouteOutcome(
                    provider=existing.provider,
                    model=existing.model,
                    code=failure.code,
                    message=failure.message,
                    elapsed_ms=failure.elapsed_ms,
                    tries=existing.tries + 1,
                )
                break
        # 用 for/else：只有「循环没被 break」才说明这一跳是第一次失败（别再 any() 重判一遍——
        # 上次就是这里被判穿，同一跳的账死活累加不上，tries 永远停在 1）
        else:
            self._route_failures.append(
                RouteOutcome(
                    provider=route.provider,
                    model=route.model,
                    code=failure.code,
                    message=failure.message,
                    elapsed_ms=failure.elapsed_ms,
                    tries=1,
                )
            )

    @property
    def cooldown_persist_error(self) -> str | None:
        """冷却落盘最近一次失败的原因（``None`` = 正常；只给人看，不参与路由）。"""
        return self._cooldowns.persist_error

    def _thaw_healthy(self) -> None:
        """额度恢复了就解冻（插件 0.11 的 ``thawIfHealthy``；只挂在**周期补查**上）。

        只解**额度类**冷却，而且要求这一轮**真的查到**（``status: ok``）且没有任何耗尽档：

        - 充值 / 换套餐后不必等到周期末（现在余额判据有 ``CREDITS_FLOOR_USD`` 下限，冷一次可以是一周）；
        - 查不到（断网 / 401 / 形状不认）**不算恢复证据**，宁可继续冷着；
        - 没密钥（``NOT_CONFIGURED``）不会被这条误放行：那不是额度问题。
        """
        for provider, entry in self._cooldowns.active().items():
            if entry.reason not in QUOTA_CODES:
                continue
            source = self._quota.sources.get(provider)
            if source is None or not source.ok or source.exhausted_windows:
                continue
            self._cooldowns.clear(provider)

    def _apply_cooldown(self, route: Route, failure: LlmFailure) -> None:
        """**不需要补查**的冷却：没密钥（``NOT_CONFIGURED``）按默认时长冷。

        ``AUTH`` 不冷（插件 0.11 口径：key 改好就该立刻可用）；额度类是
        ``_confirm_quota_failure`` 的事（要先补查确认，不能拿旧快照猜终点）。
        """
        if failure.code == "NOT_CONFIGURED":
            self._cooldowns.mark(route.provider, float(self._cooldown_ms), failure.code)

    async def _confirm_quota_failure(self, route: Route, failure: LlmFailure) -> None:
        """额度类失败的两步记账（插件 0.11.0 的顺序，一步步照抄）。

        ① **同步**先记 5 分钟短闸门：补查要一次 HTTPS 往返，这期间**已经排好序**的并发批
           （rewrite 默认 4）会照旧白撞同一家（插件 2026-10-12 实测：两条请求差 1 秒、连撞两次）；
        ② 只查**这一家**、**绕缓存**（``refresh_one``）：确认有档位耗尽就把终点换成"被耗尽那几档
           里最晚的重置时刻"；确认不了（断网 / 没额度接口 / 没耗尽）就只留那条短闸门，到点自己解除。

        与插件的差别只有一处：插件是 fire-and-forget，这里是 ``await``。理由是 AR 多半一次性命令，
        后台确认可能在进程退出时丢掉 —— 宁可让这一次失败的多等一次 HTTPS，也要把终点算准。
        """
        self._cooldowns.mark(route.provider, float(SHORT_GATE_MS), failure.code)
        await self._quota.refresh_one(route.provider, self.config.routes)
        until = self._quota.cooldown_until_of(route.provider)
        if until is None:  # fail-open：短闸门到点自己解除，下次白撞一次而已
            return
        # 上游给的是墙钟毫秒，冷却表用单调时钟：差值必须拿墙钟算
        self._cooldowns.mark(route.provider, max(0.0, until - time.time() * 1000.0), failure.code)

    async def _request_over_chain(self, messages: list[dict[str, str]], request: LlmRequest) -> LlmResult:
        """按链逐跳尝试，**任一跳成功即返回**；全败返回末跳失败 + 每跳的账。

        - 没密钥的跳**不发请求**直接跳过（记 ``NOT_CONFIGURED``，并给这家记冷却——没密钥不会自己好）；
        - **不发请求就跳过的跳都记进 ``self.skipped``**（冷却中的家 / 本轮短闸门）：并发批次里
          调用方据此知道「这一次为什么没试第一家」，排查时不必猜；
        - 同一次调用里打过 429 的跳，其余 trip 在 ``gate_ms`` 内也跳过（短闸门，插件同一思路）；
        - **换跳前摘掉历史里的思考**（``reasoning_content`` 等）：别的模型生成的思考喂给下一家，
          轻则被摊成正文、重则 400。
        """
        # 每次进链先读一次落盘的冷却（有路径才读；坏文件 / 读不到都不抛，只记 persist_error）。
        # 放在这里而不是 __init__：驱动器可能在构造之后才挂上存储，且这样「新进程第一次请求」
        # 就吃得到上次的冷却——插件 0.11 「重启后第一条白撞」的修复同一条思路。
        if self._cooldowns.persist_error is None:
            self._cooldowns.load()
        # 排序前补查一次额度（闸门内直接返回；查失败只会让某家落进「未知档」，不会导致失败）
        await self.refresh_quota()
        # 补查回来是健康的 ⇒ 解除那家额度类冷却（充值 / 换套餐后不必等到周期末）
        self._thaw_healthy()
        # 冷却过滤与记账都在 _chain_order 里做（它给冷却中的家标 cooling 并记进 skipped）；
        # 循环里再判一次只是为了挡住「两次读之间被并发批次记上」的那种插队。
        # ⚠ 不要在这里重置 self.skipped / self.attempted：_chain_order 可能刚记过账。
        self.skipped = []
        self.attempted = []
        routes = self._chain_order()
        gated: dict[str, int] = {}
        # 冷却快照只取一次：auto 排序已经按它滤过一遍，循环里不能再读一次时钟（见下方注释）
        cooling_now = self._cooldowns.active()
        last: LlmResult | None = None
        for route in routes:
            self.attempted.append(route.provider)
            gated_until = gated.get(route.provider, 0)
            if gated_until and int(self._now()) < gated_until:  # 同一次调用里刚被 429 挡下
                self.skipped.append(SkippedRoute(provider=route.provider, model=route.model, reason="gate"))
                continue
            # 冷却判定与 ``auto`` 排序共用**同一份**快照（读得太早会让 auto 已滤掉的跳在这里「复活」：
            # 冷却刚到期的一瞬间，两次读时钟差出几毫秒就够）。这一步是为了 manual（不排序、只跳过）；
            # 也顺带挡掉「两次读之间被并发批次记上」——插件 0.11：失败当下先同步记一笔短闸门，
            # 并发批次（rewrite 上限 4）不该在补查确认回来之前继续撞同一家。
            cooling = cooling_now.get(route.provider)  # 同一份快照，不重读时钟
            if cooling is not None:  # 冷却中：只过滤，不发请求；跳过这件事记进 skipped
                self.skipped.append(
                    SkippedRoute(provider=route.provider, model=route.model, reason=cooling.reason)
                )
                continue
            api_key, _origin = resolve_api_key(route)
            if not api_key:
                missing = LlmFailure(
                    code="NOT_CONFIGURED",
                    message=(
                        f"{route.label} 没有可用的密钥（{route.api_key_env or '未声明 api_key_env'}）："
                        "环境变量与该名字在 ~/.dsh/.credentials.yaml 的 refs 里都没取到，跳过这一跳"
                    ),
                )
                self._note_failure(route, missing)
                last = LlmResult(ok=False, failure=missing)
                continue

            attempt_messages = messages
            if self._route_failures or last is not None:
                attempt_messages = strip_reasoning(list(messages))
            result = await self._request_once(attempt_messages, request, route=route, secrets=[api_key])
            # 结果带上「这次跳过了谁」（成功的也带）：排查时不必猜为什么没先试第一家
            result = replace(result, skipped=tuple(self.skipped))
            if result.ok:
                return result
            failure = result.failure or LlmFailure(code="UNKNOWN", message="模型调用失败（没有给出原因）")
            self._note_failure(route, failure)
            if failure.code in QUOTA_CODES:
                # 额度类：先同步记 5 分钟短闸门、再只补查这一家；确认有档位耗尽就冷到重置时刻。
                # 窗口额度耗尽（QUOTA_RATE_WINDOW）同样走这条路：插件 0.9.1 只是**不原地重试**，
                # 冷却照旧（否则每次调用都要先白撞一次，直到窗口重置）。
                await self._confirm_quota_failure(route, failure)
            else:
                self._apply_cooldown(route, failure)
            if failure.code == "RATE_LIMIT":
                gated[route.provider] = int(self._now()) + self._gate_ms
            last = result

        if last is None:  # pragma: no cover - 链非空就至少会有一跳被尝试
            return LlmResult(ok=False, failure=LlmFailure(code="UNKNOWN", message="回退链是空的"))
        failure = last.failure or LlmFailure(code="UNKNOWN", message="模型调用失败（没有给出原因）")
        # 整条链的结论用一个**稳定码**（插件 AUTO_ROUTES_EXHAUSTED 同值）：调用方据此区分
        # 「链跑完都失败」与「末跳自己失败」；逐跳的账在 message 里。
        # ⚠ 重试能力不能被这个码吞掉：链上**只要有一跳本来值得重试**（瞬时类），外层就该再来一轮；
        # 全是确定性失败（AUTH / QUOTA / 没密钥）则一次就停 —— 逐跳判、不拿末跳的码代表整条链。
        retryable = any(should_retry(self._policy, item.code, 1) for item in self._route_failures)
        return replace(
            last,
            failure=LlmFailure(
                code=CHAIN_EXHAUSTED_CODE,
                message=self._chain_message(failure.message),
                status=failure.status,
                retryable=retryable,
            ),
        )

    def _chain_message(self, last_message: str) -> str:
        """整条链都失败时的文案（插件「全部失败」那一块的等价物）：**逐跳一行**、名和账分开写。

        形如::

            回退链全部失败（2 跳）：
              1) a/m-a 没有可用的密钥（CMD_API_KEY）：…（0 ms，已试 1 次）
              2) b/m-b → QUOTA：HTTP 402：Insufficient Balance（12 ms，已试 1 次）

        与旧版的一行摘要（``回退链全部失败（a=QUOTA；b=SERVER）：…``）相比，这里额外给出**耗时**、
        **每跳试了几次**和**每跳自己的原因**——排查时不用再去翻日志。
        """
        if not self._route_failures:
            return last_message
        lines = [f"回退链全部失败（{len(self._route_failures)} 跳）："]
        for index, item in enumerate(self._route_failures, start=1):
            detail = " ".join(str(item.message).split())[:300]
            elapsed = item.elapsed_ms if item.elapsed_ms is not None else 0
            lines.append(
                f"  {index}) {item.provider}/{item.model} → {item.code}：{detail}"
                f"（{elapsed} ms，已试 {max(1, item.tries)} 次）"
            )
        return "\n".join(lines)

    def _now(self) -> float:
        return time.monotonic() * 1000.0

    # -------------------------------------------------------------- 单次请求 --
    async def _request_once(
        self,
        messages: list[dict[str, str]],
        request: LlmRequest,
        *,
        route: Route | None = None,
        secrets: list[str] | None = None,
    ) -> LlmResult:
        """一次 HTTP 往返：**任何失败都变成 ``LlmResult.failure``，不抛**。"""
        payload: dict[str, Any] = {"model": self._model(route), "messages": messages, "stream": True}
        # 推理档位（插件 0.10 同口径）：链模式按**这一跳自己的表**解析，认不出的档位名整键不写，
        # 绝不把陌生值硬塞给上游；单源模式没有 Route，请求体保持现状（不新增任何键）。
        if route is not None:
            wanted, value = route.reasoning_value()
            if wanted and value is not None:
                payload["reasoning_effort"] = value
        temp = self.config.temperature if request.temperature is None else request.temperature
        if temp is not None:
            payload["temperature"] = temp
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if self._use_stream_options:
            payload["stream_options"] = {"include_usage": True}

        timeout = httpx.Timeout(self.config.timeout_s, connect=min(30.0, self.config.timeout_s))
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
                async with aconnect_sse(
                    client, "POST", self._url(route), json=payload, headers=self._headers(route)
                ) as source:
                    response = source.response
                    if response.status_code >= 400:
                        body = await response.aread()
                        message = f"HTTP {response.status_code}：{self._clip(body, secrets=secrets)}"
                        failure = classify_failure(response.status_code, message)  # 归一：见 classify_failure
                        return LlmResult(
                            ok=False,
                            failure=replace(
                                failure, retry_after_ms=parse_retry_after(response.headers.get("retry-after"))
                            ),
                        )
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/event-stream" not in content_type:
                        body = await response.aread()
                        return self._parse_buffered(body, route=route, secrets=secrets)
                    return await self._collect_stream(source, route=route, secrets=secrets)
        except httpx.HTTPError as exc:
            # 超时 / 连接失败 / 协议错误 / 连接被掐断
            return LlmResult(
                ok=False,
                failure=classify_exception(
                    exc, f"传输失败（{type(exc).__name__}）：{self._clip(exc, secrets=secrets)}"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 「只发一次、不抛」是本方法对 ar.llm 的承诺
            return LlmResult(
                ok=False,
                failure=LlmFailure(
                    code="UNKNOWN",
                    message=f"模型调用异常（{type(exc).__name__}）：{self._clip(exc, secrets=secrets)}",
                ),
            )

    async def _collect_stream(
        self, source: Any, *, route: Route | None = None, secrets: list[str] | None = None
    ) -> LlmResult:
        """把 SSE 事件流收成一次结果（含「流意外提前结束」的归一）。"""
        parts: list[str] = []
        usage: dict[str, Any] | None = None
        finish_reason: str | None = None
        model_name: str | None = None
        done = False
        async for sse in source.aiter_sse():
            if sse.data.strip() == "[DONE]":
                done = True
                break
            try:
                event = json.loads(sse.data)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("error"):
                return LlmResult(ok=False, failure=self._stream_failure(event["error"], secrets=secrets))
            model_name = event.get("model") or model_name
            choices = event.get("choices") or []
            choice = choices[0] if choices and isinstance(choices[0], dict) else {}
            text = extract_content(choice.get("delta"))
            if text:
                parts.append(text)
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]

        text = "".join(parts)
        if not done and finish_reason is None:
            # 范本的盲点：没有终止事件的流被掐断时必须**可重试**，不能当成正常收尾。
            # 已收到的片段一律丢弃（重试会从头再来），partial 正文当成功用才是真危险。
            return LlmResult(
                ok=False,
                failure=LlmFailure(
                    code="TRANSPORT",
                    message=(
                        f"流意外提前结束（既没有 [DONE] 也没有 finish_reason）：已收到 {len(text)} 字符，"
                        "按传输失败处理（可重试）"
                    ),
                ),
            )
        if not text.strip():
            return LlmResult(
                ok=False,
                failure=LlmFailure(
                    code="EMPTY_RESPONSE",
                    message=(
                        "模型没有返回正文（可能 reasoning 吃光 max_tokens，或该档位不吐正文）；"
                        "已生成的部分照常可用，本段按「未改写」处理"
                    ),
                ),
            )
        return LlmResult(
            ok=True,
            text=text,
            model=model_name or self._model(route),
            finish_reason=finish_reason,
            usage=usage,
        )

    def _stream_failure(self, error: Any, *, secrets: list[str] | None = None) -> LlmFailure:
        """流内 / 响应体里的 ``error`` 对象 → 归一后的失败（有 status 按表，否则 SERVER）。"""
        message = f"流内错误：{self._clip(json.dumps(error, ensure_ascii=False), secrets=secrets)}"
        status = _status_of(error)
        if status is None:
            if looks_like_quota(message):
                return LlmFailure(code="QUOTA", message=message)
            return LlmFailure(code="SERVER", message=message)
        return classify_failure(status, message)

    def _parse_buffered(
        self, body: bytes, *, route: Route | None = None, secrets: list[str] | None = None
    ) -> LlmResult:
        """网关无视 ``stream:true``、直接返回 JSON 时的兜底解析。"""
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            # 既不是 SSE 也不是 JSON：协议错误（可重试）
            return LlmResult(
                ok=False,
                failure=LlmFailure(
                    code="TRANSPORT",
                    message=f"响应既不是 SSE 也不是 JSON（{exc}）：{self._clip(body, secrets=secrets)}",
                ),
            )
        if isinstance(data, dict) and data.get("error"):
            failure = self._stream_failure(data["error"], secrets=secrets)
            return LlmResult(
                ok=False,
                failure=replace(
                    failure,
                    message="网关返回错误："
                    + self._clip(json.dumps(data["error"], ensure_ascii=False), secrets=secrets),
                ),
            )
        choices = (data or {}).get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        text = extract_content(choice.get("message") or {}) or extract_content(choice.get("delta"))
        if not text.strip():
            return LlmResult(
                ok=False,
                failure=LlmFailure(
                    code="EMPTY_RESPONSE", message=f"响应里没有正文：{self._clip(body, secrets=secrets)}"
                ),
            )
        return LlmResult(
            ok=True,
            text=text,
            model=(data or {}).get("model") or self._model(route),
            finish_reason=choice.get("finish_reason"),
            usage=(data or {}).get("usage"),
        )

    # ------------------------------------------------------------- 解析辅助 --
    def _stream_options_rejected(self, result: LlmResult) -> bool:
        """这次失败是不是「网关不认 ``stream_options``」。"""
        failure = result.failure
        if failure is None:
            return False
        return any(marker in failure.message for marker in _REJECTION_MARKERS)

    def _clip(self, payload: Any, limit: int = 400, *, secrets: list[str] | None = None) -> str:
        text = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
        text = " ".join(text.split())
        return self._redact(text[:limit], secrets=secrets)
