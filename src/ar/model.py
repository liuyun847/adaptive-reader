"""模型客户端：OpenAI 兼容 ``POST {base_url}/chat/completions``（流式）。

参数面照 ``test_api/ttft-bench/bench.mjs`` 的实测口径：``stream: true`` +
``stream_options.include_usage``（网关拒绝该字段时自动关掉它重试，且**不占用**重试额度）+
单次超时 + 最多 ``max_retries`` 次重试（默认 1）。

**密钥纪律**：密钥只在运行时从 ``AR_API_KEY`` 或 ``config.local.json`` 读入内存；
本模块任何异常消息都过 ``redact()``，不含密钥值，可安全打印 / 落盘 / 进 HTTP 响应。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Iterable

import httpx
from httpx_sse import aconnect_sse

from .config import Config, redact
from .errors import ModelUnavailable

__all__ = [
    "ModelReply",
    "ModelClient",
    "ModelNotConfigured",
    "ModelCallFailed",
    "extract_content",
    "cached_tokens_of",
]

_UNSET: Any = object()
_REJECTION_MARKERS = ("stream_options", "include_usage")


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


class _ParameterRejected(ModelCallFailed):
    """网关不认某个可选参数（目前只有 stream_options）。"""


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

    # ---------------------------------------------------------------- 状态 --
    @property
    def configured(self) -> bool:
        return bool(self._api_key) and bool(self.config.base_url) and bool(self.config.model)

    @property
    def stream_options_enabled(self) -> bool:
        """``stream_options.include_usage`` 是否仍然开启（被网关拒绝后永久关掉）。"""
        return self._use_stream_options

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
        """发一次对话请求；失败抛 ``ModelCallFailed``（消息已脱敏）。"""
        if not self.configured:
            raise ModelNotConfigured(
                "模型未配置：请设置环境变量 AR_API_KEY，或在 config.local.json 里填 api_key"
                "（两者都为空时任何模型调用都会跳过）"
            )
        payload_messages = list(messages)
        attempts = 1 + max(0, int(self.config.max_retries))
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await self._attempt(payload_messages, temperature=temperature, max_tokens=max_tokens)
            except (httpx.HTTPError, ModelCallFailed) as exc:
                last_error = exc
                if attempt >= attempts:
                    break
        raise ModelCallFailed(f"模型调用失败（已试 {attempts} 次）：{last_error}") from last_error

    async def _attempt(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None,
        max_tokens: Any,
    ) -> ModelReply:
        """一次尝试；``stream_options`` 被拒时自动降级重试（参数降级不占重试额度）。"""
        try:
            return await self._request_once(messages, temperature=temperature, max_tokens=max_tokens)
        except _ParameterRejected as exc:
            if not self._use_stream_options:
                raise
            self._use_stream_options = False
            degraded = ("stream_options.include_usage 被网关拒绝，已关闭该参数重试",)
            try:
                reply = await self._request_once(messages, temperature=temperature, max_tokens=max_tokens)
            except (httpx.HTTPError, ModelCallFailed) as exc2:
                raise ModelCallFailed(f"{degraded[0]}；重试仍失败：{exc2}") from exc2
            return ModelReply(
                text=reply.text,
                model=reply.model,
                finish_reason=reply.finish_reason,
                usage=reply.usage,
                degraded_params=degraded,
            )

    async def _request_once(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None,
        max_tokens: Any,
    ) -> ModelReply:
        payload: dict[str, Any] = {"model": self.config.model, "messages": messages, "stream": True}
        temp = self.config.temperature if temperature is None else temperature
        if temp is not None:
            payload["temperature"] = temp
        limit = self.config.max_tokens if max_tokens is _UNSET else max_tokens
        if limit is not None:
            payload["max_tokens"] = limit
        if self._use_stream_options:
            payload["stream_options"] = {"include_usage": True}

        timeout = httpx.Timeout(self.config.timeout_s, connect=min(30.0, self.config.timeout_s))
        async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
            async with aconnect_sse(client, "POST", self._url(), json=payload, headers=self._headers()) as source:
                response = source.response
                content_type = response.headers.get("content-type", "").lower()
                if response.status_code >= 400:
                    body = await response.aread()
                    raise self._http_error(response.status_code, body)
                if "text/event-stream" not in content_type:
                    body = await response.aread()
                    return self._parse_buffered(body)

                parts: list[str] = []
                usage: dict[str, Any] | None = None
                finish_reason: str | None = None
                model_name: str | None = None
                async for sse in source.aiter_sse():
                    if sse.data.strip() == "[DONE]":
                        break
                    try:
                        event = json.loads(sse.data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("error"):
                        raise ModelCallFailed(f"流内错误：{self._clip(json.dumps(event['error'], ensure_ascii=False))}")
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
                if not text.strip():
                    raise ModelCallFailed(
                        "模型没有返回正文（可能 reasoning 吃光 max_tokens，或该档位不吐正文）；"
                        "已生成的部分照常可用，本段按「未改写」处理"
                    )
                return ModelReply(
                    text=text, model=model_name or self.config.model, finish_reason=finish_reason, usage=usage
                )

    # ------------------------------------------------------------- 解析辅助 --
    def _parse_buffered(self, body: bytes) -> ModelReply:
        """网关无视 ``stream:true``、直接返回 JSON 时的兜底解析。"""
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise ModelCallFailed(f"响应既不是 SSE 也不是 JSON（{exc}）：{self._clip(body)}") from exc
        if isinstance(data, dict) and data.get("error"):
            raise ModelCallFailed(f"网关返回错误：{self._clip(json.dumps(data['error'], ensure_ascii=False))}")
        choices = (data or {}).get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        text = extract_content(choice.get("message") or {}) or extract_content(choice.get("delta"))
        if not text.strip():
            raise ModelCallFailed(f"响应里没有正文：{self._clip(body)}")
        return ModelReply(
            text=text,
            model=(data or {}).get("model") or self.config.model,
            finish_reason=choice.get("finish_reason"),
            usage=(data or {}).get("usage"),
        )

    def _http_error(self, status: int, body: bytes) -> ModelCallFailed:
        message = f"HTTP {status}：{self._clip(body)}"
        if any(marker in message for marker in _REJECTION_MARKERS):
            return _ParameterRejected(message)
        return ModelCallFailed(message)

    def _clip(self, payload: Any, limit: int = 400) -> str:
        text = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
        text = " ".join(text.split())
        return self._redact(text[:limit])
