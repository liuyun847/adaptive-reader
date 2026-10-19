"""模型客户端：全部用 httpx 的假 transport，不发任何真实网络请求。

覆盖：流式拼装、``stream_options`` 被网关拒绝时自动降级且不占重试额度、
错误码归一（§3.2）、``arequest_once`` 只发一次且不抛、超时/HTTP 错误重试、错误信息脱敏、未配置时的报错。

重试策略已移到 ``ar.llm``（用例见 ``tests/test_llm.py``）：这里的 ``max_retries`` 是**老的顶层键**，
按 ``ar.config.load_config`` 的同一口径映射进 ``retry`` 段，等待序列在那边用假时钟钉死。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from ar.config import Config
from ar.llm import LlmRequest, LlmResult
from ar.model import (
    ModelCallFailed,
    ModelClient,
    ModelNotConfigured,
    failure_code_for_status,
    parse_retry_after,
)

FAKE_KEY = "sk-" + "e" * 40

DELTA_EVENTS: list[dict[str, Any]] = [
    {"model": "fake/model", "choices": [{"delta": {"role": "assistant"}}]},
    {"choices": [{"delta": {"content": "你好"}}]},
    {
        "choices": [{"delta": {"content": "，世界"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5},
    },
]


def sse_bytes(events: list[dict[str, Any]]) -> bytes:
    body = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)
    return (body + "data: [DONE]\n\n").encode("utf-8")


def sse_response(events: list[dict[str, Any]] | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=sse_bytes(events if events is not None else DELTA_EVENTS),
    )


def make_client(handler: Any, *, max_retries: int = 0, **overrides: Any) -> ModelClient:
    """``max_retries`` 是老的顶层键：这里显式映射成 ``retry`` 段（与 ``load_config`` 同一口径）。

    直接构造 ``Config`` 时 ``retry`` 缺省是 ``None``（= 内置默认：5 次重试），
    所以用例必须自己把想试的次数写进 ``retry`` 段——否则一失败就要等 15 秒退避。
    """
    kwargs: dict[str, Any] = {
        "base_url": "https://gateway.invalid/provider/v1",
        "model": "fake/model",
        "timeout_s": 5,
        "max_retries": max_retries,
        "api_key": FAKE_KEY,
        "retry": {"maxRetries": max_retries},
    }
    kwargs.update(overrides)
    config = Config(**kwargs)
    return ModelClient(config, transport=httpx.MockTransport(handler))


def call(client: ModelClient) -> Any:
    return asyncio.run(client.achat([{"role": "user", "content": "讲一句话"}]))


def request_once(client: ModelClient, content: str = "讲一句话") -> LlmResult:
    """走「只发一次、不抛」的入口（``ar.llm.acall`` 用的就是它）。"""
    return asyncio.run(client.arequest_once(LlmRequest(tool="test", messages=({"role": "user", "content": content},))))


# --------------------------------------------------------------------- 正常路径 --
def test_stream_collects_text_model_usage_and_request_shape() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return sse_response()

    client = make_client(handler)
    reply = call(client)
    assert reply.text == "你好，世界"
    assert reply.model == "fake/model"
    assert reply.finish_reason == "stop"
    assert reply.usage == {"prompt_tokens": 3, "completion_tokens": 5}
    assert seen["url"] == "https://gateway.invalid/provider/v1/chat/completions"
    assert seen["auth"] == f"Bearer {FAKE_KEY}"
    assert seen["payload"]["stream"] is True
    assert seen["payload"]["stream_options"] == {"include_usage": True}
    assert "max_tokens" not in seen["payload"]  # 默认 None = 不带该字段
    assert seen["payload"]["temperature"] == 0.6


def test_stream_options_rejection_degrades_without_spending_retry() -> None:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        payloads.append(payload)
        if len(payloads) == 1:
            return httpx.Response(
                400,
                headers={"content-type": "application/json"},
                content=json.dumps({"error": {"message": "Unsupported parameter: stream_options"}}).encode(),
            )
        return sse_response()

    client = make_client(handler, max_retries=0)
    reply = call(client)
    assert reply.text == "你好，世界"
    assert len(payloads) == 2, "参数降级应当自动重试一次"
    assert "stream_options" in payloads[0]
    assert "stream_options" not in payloads[1]
    assert client.stream_options_enabled is False
    assert reply.degraded_params and "stream_options" in reply.degraded_params[0]


def test_non_sse_json_response_is_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(
                {"model": "fake/model", "choices": [{"message": {"role": "assistant", "content": "兜底正文"}}]}
            ).encode("utf-8"),
        )

    reply = call(make_client(handler))
    assert reply.text == "兜底正文"


# --------------------------------------------------------------------- 失败路径 --
def test_not_configured_raises_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调用
        raise AssertionError("未配置时不应发出请求")

    client = make_client(handler, api_key="")
    with pytest.raises(ModelNotConfigured, match="AR_API_KEY"):
        call(client)


def test_http_error_message_is_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            headers={"content-type": "application/json"},
            content=json.dumps({"error": f"invalid api key: Bearer {FAKE_KEY}"}).encode("utf-8"),
        )

    client = make_client(handler)
    with pytest.raises(ModelCallFailed) as excinfo:
        call(client)
    message = str(excinfo.value)
    assert FAKE_KEY not in message
    assert "401" in message and "REDACTED" in message


def test_retry_once_then_success() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(500, content=b"boom")
        return sse_response()

    reply = call(make_client(handler, max_retries=1))
    assert reply.text == "你好，世界"
    assert len(attempts) == 2


def test_all_attempts_fail_reports_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"gateway down")

    with pytest.raises(ModelCallFailed, match="已试 2 次"):
        call(make_client(handler, max_retries=1))


def test_empty_reply_is_an_error_not_silent_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response([{"model": "fake/model", "choices": [{"delta": {"role": "assistant"}}]}])

    with pytest.raises(ModelCallFailed, match="没有返回正文"):
        call(make_client(handler))


def test_in_stream_error_is_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response([{"error": {"message": "upstream exploded"}}])

    with pytest.raises(ModelCallFailed, match="流内错误"):
        call(make_client(handler))


def test_timeout_is_reported_as_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(ModelCallFailed, match="模型调用失败"):
        call(make_client(handler))


# ------------------------------------------------------ 前缀缓存用量字段解析 --
@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        # OpenAI 风格（本机网关实测回的就是它）
        ({"prompt_tokens_details": {"cached_tokens": 3456}}, 3456),
        # 扁平字段（DeepSeek 原生字段名）
        ({"prompt_cache_hit_tokens": 512}, 512),
        ({"cached_tokens": 7}, 7),
        # 嵌套优先：两个都在时听嵌套的
        ({"prompt_tokens_details": {"cached_tokens": 3}, "prompt_cache_hit_tokens": 99}, 3),
        # 上报了但没命中：0 与「没上报」必须分得开
        ({"prompt_tokens_details": {"cached_tokens": 0}}, 0),
        # 没上报 / 形态不对 → None（不许编造成 0）
        ({}, None),
        ({"prompt_tokens_details": {}}, None),
        ({"prompt_tokens_details": {"cached_tokens": None}}, None),
        ({"prompt_tokens_details": {"cached_tokens": "3456"}}, None),
        ({"prompt_tokens_details": {"cached_tokens": True}}, None),
        ({"prompt_cache_hit_tokens": 3.5}, None),
        (None, None),
        ("usage", None),
    ],
)
def test_cached_tokens_of_reads_both_gateway_shapes(usage: Any, expected: int | None) -> None:
    from ar.model import cached_tokens_of

    assert cached_tokens_of(usage) == expected


# ------------------------------------------------------ 错误码归一（§3.2） --
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, "invalid key", "AUTH"),
        (403, "forbidden", "AUTH"),
        (400, "bad request", "INVALID_REQUEST"),
        (413, "too large", "INVALID_REQUEST"),
        (422, "unprocessable", "INVALID_REQUEST"),
        (429, "slow down", "RATE_LIMIT"),
        (500, "boom", "SERVER"),
        (503, "gateway down", "SERVER"),
        (418, "teapot", "UNKNOWN"),
        # 配额文案优先归 QUOTA（402 / 403 也可能是额度用尽，重试无用）
        (402, "Insufficient Balance", "QUOTA"),
        (429, "you exceeded your current quota", "QUOTA"),
        (400, "账户余额不足，请充值", "QUOTA"),
        # 401 就是鉴权，不许被文案带走
        (401, "insufficient quota", "AUTH"),
    ],
)
def test_http_status_is_normalized_into_an_error_code(status: int, body: str, expected: str) -> None:
    client = make_client(lambda request: httpx.Response(status, content=body.encode("utf-8")))
    result = request_once(client)

    assert result.ok is False and result.failure is not None
    assert result.failure.code == expected
    assert result.failure.status == status
    assert f"HTTP {status}" in result.failure.message


def test_failure_code_for_status_covers_the_table() -> None:
    assert failure_code_for_status(401) == "AUTH"
    assert failure_code_for_status(404) == "UNKNOWN"
    assert failure_code_for_status(502) == "SERVER"


@pytest.mark.parametrize(
    ("header", "expected"),
    [("2", 2000), ("0.5", 500), ("0", 0), ("abc", None), ("-1", None), ("", None), (None, None)],
)
def test_retry_after_header_is_read_in_seconds(header: str | None, expected: int | None) -> None:
    assert parse_retry_after(header) == expected


def test_retry_after_header_lands_on_the_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "3"}, content=b"slow down")

    result = request_once(make_client(handler))
    assert result.failure is not None
    assert (result.failure.code, result.failure.retry_after_ms) == ("RATE_LIMIT", 3000)


def test_timeout_is_normalized_as_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    result = request_once(make_client(handler))
    assert result.failure is not None and result.failure.code == "TIMEOUT"


def test_connection_failure_is_normalized_as_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = request_once(make_client(handler))
    assert result.failure is not None and result.failure.code == "TRANSPORT"


def test_empty_stream_is_normalized_as_empty_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response([{"model": "fake/model", "choices": [{"delta": {"role": "assistant"}}]}])

    result = request_once(make_client(handler))
    assert result.failure is not None and result.failure.code == "EMPTY_RESPONSE"
    assert "没有返回正文" in result.failure.message


def test_in_stream_error_with_status_uses_the_status_table() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response([{"error": {"message": "rate limited", "status": 429}}])

    result = request_once(make_client(handler))
    assert result.failure is not None and result.failure.code == "RATE_LIMIT"


def test_in_stream_error_without_status_is_server() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response([{"error": {"message": "upstream exploded"}}])

    result = request_once(make_client(handler))
    assert result.failure is not None and result.failure.code == "SERVER"
    assert "流内错误" in result.failure.message


def test_buffered_non_json_body_is_a_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>proxy error</html>")

    result = request_once(make_client(handler))
    assert result.failure is not None and result.failure.code == "TRANSPORT"


def test_arequest_once_never_raises_and_reports_not_configured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调用
        raise AssertionError("未配置时不应发出请求")

    result = request_once(make_client(handler, api_key=""))
    assert result.ok is False and result.failure is not None
    assert result.failure.code == "NOT_CONFIGURED" and result.attempts == ()


def test_arequest_once_reports_call_failure_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    result = request_once(make_client(handler))
    assert result.ok is False and result.attempts == (), "单次尝试的入口不记账，记账在 ar.llm"


def test_arequest_once_returns_text_usage_and_finish_reason() -> None:
    result = request_once(make_client(lambda request: sse_response()))
    assert result.ok is True
    assert result.text == "你好，世界"
    assert result.model == "fake/model"
    assert result.finish_reason == "stop"
    assert result.usage == {"prompt_tokens": 3, "completion_tokens": 5}
    assert result.degraded_params == ()


def test_stream_options_degradation_is_reported_on_the_single_request_entry() -> None:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content.decode("utf-8")))
        if len(payloads) == 1:
            return httpx.Response(
                400,
                headers={"content-type": "application/json"},
                content=json.dumps({"error": {"message": "Unsupported parameter: stream_options"}}).encode(),
            )
        return sse_response()

    client = make_client(handler, max_retries=0)
    result = request_once(client)

    assert result.ok is True and len(payloads) == 2
    assert client.stream_options_enabled is False
    assert result.degraded_params and "stream_options" in result.degraded_params[0]


def test_retry_policy_comes_from_config() -> None:
    """``retry`` 段决定策略（老的顶层 ``max_retries`` 由 ``load_config`` 映射进来）。"""
    client = make_client(lambda request: sse_response(), max_retries=3)
    assert client.retry_policy().max_retries == 3
    default_client = ModelClient(
        Config(base_url="https://gateway.invalid/provider/v1", model="fake/model", api_key=FAKE_KEY),
        transport=httpx.MockTransport(lambda request: sse_response()),
    )
    assert default_client.retry_policy().max_retries == 5, "没配 retry 段 = §2 内置默认值"
