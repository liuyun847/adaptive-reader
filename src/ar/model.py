"""模型客户端：OpenAI 兼容 ``POST {base_url}/chat/completions``（流式）。

本模块只做「**发一次请求并把它看懂**」：

- 拼 URL 与请求体、发一次、解析 SSE（或网关无视 ``stream`` 直接回 JSON 时兜底解析）、拼增量；
- ``stream_options.include_usage`` 被网关拒绝时**自动关掉该参数重发一次**（参数降级**不占用**重试额度）；
- **错误码归一**（PLAN-llm-tools §3.2）：把 HTTP 状态 / 传输异常 / 流内错误 / 空正文 / 配额文案
  映射成 ``AUTH`` / ``INVALID_REQUEST`` / ``RATE_LIMIT`` / ``SERVER`` / ``TIMEOUT`` / ``TRANSPORT`` /
  ``EMPTY_RESPONSE`` / ``QUOTA`` / ``NOT_CONFIGURED`` / ``UNKNOWN``；**重试不在这里做**——
  重试策略与逐次记账在 ``ar.llm``（``arequest_once`` 是它要的「只发一次、返回 ``LlmResult``、不抛」入口）。

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
from dataclasses import dataclass, replace
from typing import Any, Iterable

import httpx
from httpx_sse import aconnect_sse

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
)

__all__ = [
    "ModelReply",
    "ModelClient",
    "ModelNotConfigured",
    "ModelCallFailed",
    "extract_content",
    "cached_tokens_of",
    "classify_failure",
    "failure_code_for_status",
    "looks_like_quota",
    "parse_retry_after",
]

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
    "余额不足",
    "额度不足",
    "配额不足",
    "额度已用尽",
    "欠费",
)


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

    判定顺序：401/403 先归 AUTH（鉴权就是鉴权）；再看「额度用尽」文案（429 / 402 也可能是配额，
    重试无用）；然后按状态表；状态缺失时归 UNKNOWN。
    """
    if status is not None and int(status) in (401, 403):
        return LlmFailure(code="AUTH", message=message, status=int(status))
    if looks_like_quota(message):
        return LlmFailure(code="QUOTA", message=message, status=None if status is None else int(status))
    if status is None:
        return LlmFailure(code="UNKNOWN", message=message)
    return LlmFailure(code=failure_code_for_status(int(status)), message=message, status=int(status))


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


class ModelClient:
    """同步 / 异步两种用法共用一个实现（异步为核心，同步只是 ``asyncio.run`` 包装）。"""

    def __init__(
        self,
        config: Config,
        *,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._api_key = config.api_key if api_key is None else api_key
        self._transport = transport
        self._use_stream_options = True
        # 重试策略在这里就解析好：配置非法要早点报（CLI 退出码 2），而不是等第一次调用
        self._policy = resolve_retry_policy(config.retry)

    # ---------------------------------------------------------------- 状态 --
    @property
    def configured(self) -> bool:
        return bool(self._api_key) and bool(self.config.base_url) and bool(self.config.model)

    @property
    def stream_options_enabled(self) -> bool:
        """``stream_options.include_usage`` 是否仍然开启（被网关拒绝后永久关掉）。"""
        return self._use_stream_options

    def retry_policy(self) -> RetryPolicy:
        """本次客户端的重试策略（配置的 ``retry`` 段；没配就是 §2 的默认值）。"""
        return self._policy

    def _redact(self, text: Any) -> str:
        return redact(text, secrets=[self._api_key])

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }

    def _url(self) -> str:
        return self.config.base_url.rstrip("/") + "/chat/completions"

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

    async def arequest_once(self, request: LlmRequest) -> LlmResult:
        """**只发一次**（不重试、不抛）：``stream_options`` 被拒时关掉该参数重发一次。

        ``ar.llm.acall`` 的唯一调用入口；参数降级（关 ``stream_options``）**不占重试额度**，
        降级这件事写进 ``LlmResult.degraded_params``，由调用方决定怎么记账。
        """
        if not self.configured:
            return not_configured_result()
        # 每次尝试都从原始请求重建消息（重发不带上一次的残留）
        result = await self._request_once([dict(message) for message in request.messages], request)
        if not self._stream_options_rejected(result) or not self._use_stream_options:
            return result

        self._use_stream_options = False
        note = "stream_options.include_usage 被网关拒绝，已关闭该参数重试"
        again = await self._request_once([dict(message) for message in request.messages], request)
        if again.ok:
            return replace(again, degraded_params=(note,))
        failure = again.failure or LlmFailure(code="UNKNOWN", message="模型调用失败（没有给出原因）")
        return replace(again, failure=replace(failure, message=f"{note}；重发仍失败：{failure.message}"))

    # -------------------------------------------------------------- 单次请求 --
    async def _request_once(self, messages: list[dict[str, str]], request: LlmRequest) -> LlmResult:
        """一次 HTTP 往返：**任何失败都变成 ``LlmResult.failure``，不抛**。"""
        payload: dict[str, Any] = {"model": self.config.model, "messages": messages, "stream": True}
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
                    client, "POST", self._url(), json=payload, headers=self._headers()
                ) as source:
                    response = source.response
                    if response.status_code >= 400:
                        body = await response.aread()
                        message = f"HTTP {response.status_code}：{self._clip(body)}"
                        failure = classify_failure(response.status_code, message)
                        return LlmResult(
                            ok=False,
                            failure=replace(
                                failure, retry_after_ms=parse_retry_after(response.headers.get("retry-after"))
                            ),
                        )
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/event-stream" not in content_type:
                        body = await response.aread()
                        return self._parse_buffered(body)
                    return await self._collect_stream(source)
        except httpx.HTTPError as exc:
            # 超时 / 连接失败 / 协议错误 / 连接被掐断
            return LlmResult(
                ok=False,
                failure=classify_exception(exc, f"传输失败（{type(exc).__name__}）：{self._clip(exc)}"),
            )
        except Exception as exc:  # noqa: BLE001 - 「只发一次、不抛」是本方法对 ar.llm 的承诺
            return LlmResult(
                ok=False,
                failure=LlmFailure(code="UNKNOWN", message=f"模型调用异常（{type(exc).__name__}）：{self._clip(exc)}"),
            )

    async def _collect_stream(self, source: Any) -> LlmResult:
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
                return LlmResult(ok=False, failure=self._stream_failure(event["error"]))
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
            model=model_name or self.config.model,
            finish_reason=finish_reason,
            usage=usage,
        )

    def _stream_failure(self, error: Any) -> LlmFailure:
        """流内 / 响应体里的 ``error`` 对象 → 归一后的失败（有 status 按表，否则 SERVER）。"""
        message = f"流内错误：{self._clip(json.dumps(error, ensure_ascii=False))}"
        status = _status_of(error)
        if status is None:
            if looks_like_quota(message):
                return LlmFailure(code="QUOTA", message=message)
            return LlmFailure(code="SERVER", message=message)
        return classify_failure(status, message)

    def _parse_buffered(self, body: bytes) -> LlmResult:
        """网关无视 ``stream:true``、直接返回 JSON 时的兜底解析。"""
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            # 既不是 SSE 也不是 JSON：协议错误（可重试）
            return LlmResult(
                ok=False,
                failure=LlmFailure(
                    code="TRANSPORT", message=f"响应既不是 SSE 也不是 JSON（{exc}）：{self._clip(body)}"
                ),
            )
        if isinstance(data, dict) and data.get("error"):
            failure = self._stream_failure(data["error"])
            return LlmResult(
                ok=False,
                failure=replace(
                    failure, message=f"网关返回错误：{self._clip(json.dumps(data['error'], ensure_ascii=False))}"
                ),
            )
        choices = (data or {}).get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        text = extract_content(choice.get("message") or {}) or extract_content(choice.get("delta"))
        if not text.strip():
            return LlmResult(
                ok=False,
                failure=LlmFailure(code="EMPTY_RESPONSE", message=f"响应里没有正文：{self._clip(body)}"),
            )
        return LlmResult(
            ok=True,
            text=text,
            model=(data or {}).get("model") or self.config.model,
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

    def _clip(self, payload: Any, limit: int = 400) -> str:
        text = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
        text = " ".join(text.split())
        return self._redact(text[:limit])
