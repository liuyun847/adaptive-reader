"""统一调用层（``ar.llm``）的用例：重试判据、退避序列、``Retry-After``、记账、请求重建。

三条纪律：**不发任何真实网络请求**（假客户端 + ``httpx.MockTransport``）、
**不真的等待**（假时钟 / 假 sleep 把等待序列钉死）、**不读开发机配置**（用例自带 tmp 配置）。
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from ar.config import Config, load_config
from ar.errors import InputError
from ar.llm import (
    DEFAULT_RETRYABLE_CODES,
    LlmAttempt,
    LlmFailure,
    LlmRequest,
    LlmResult,
    RetryPolicy,
    attempt_records,
    call,
    compute_retry_delay,
    failure_message,
    not_configured_result,
    resolve_retry_policy,
    retry_account,
)
from ar.model import ModelClient

FAKE_KEY = "sk-" + "e" * 40

DELTA_EVENTS: list[dict[str, Any]] = [
    {"model": "fake/model", "choices": [{"delta": {"role": "assistant"}}]},
    {"choices": [{"delta": {"content": "你好"}}]},
    {
        "choices": [{"delta": {"content": "，世界"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5},
    },
]


def sse_bytes(events: list[dict[str, Any]], *, done: bool = True) -> bytes:
    """SSE 响应体；``done=False`` 用来伪造「流被掐断」（没有 ``[DONE]``）。"""
    body = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)
    return (body + ("data: [DONE]\n\n" if done else "")).encode("utf-8")


def sse_response(events: list[dict[str, Any]] | None = None, *, done: bool = True) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=sse_bytes(DELTA_EVENTS if events is None else events, done=done),
    )


# ------------------------------------------------------------------- 假件 --
class FakeTime:
    """假时钟 + 假 sleep：``clock()`` 返回当前毫秒（每次读前进 ``step``），``sleep(ms)`` 推进它。"""

    def __init__(self, step: float = 5.0) -> None:
        self.now = 0.0
        self.step = step
        self.slept: list[int] = []

    def clock(self) -> float:
        value = self.now
        self.now += self.step
        return value

    def sleep(self, delay_ms: int) -> None:
        self.slept.append(int(delay_ms))
        self.now += int(delay_ms)


def _client_config(*, max_retries: int) -> Config:
    """直接构造的 ``Config``：``retry`` 段显式给出（等于 ``load_config`` 对老 ``max_retries`` 的映射）。"""
    return Config(
        base_url="https://gateway.invalid/provider/v1",
        model="fake/model",
        timeout_s=5,
        api_key=FAKE_KEY,
        max_retries=max_retries,
        retry={"maxRetries": max_retries},
    )


def model_failure(code: str, *, retry_after_ms: int | None = None) -> LlmResult:
    return LlmResult(ok=False, failure=LlmFailure(code=code, message=f"{code} 测试失败", retry_after_ms=retry_after_ms))


def model_success(text: str = "正文") -> LlmResult:
    return LlmResult(ok=True, text=text, model="fake/model", usage={"total_tokens": 1}, finish_reason="stop")


@dataclass
class ScriptedClient:
    """假模型客户端：按脚本顺序返回结果（用完就重复最后一个），并记录收到的请求。"""

    results: list[LlmResult]
    configured: bool = True
    mutate: bool = False
    requests: list[LlmRequest] = field(default_factory=list)

    async def arequest_once(self, request: LlmRequest) -> LlmResult:
        # 先留证据：``mutate`` 会改掉收到的那一份，快照必须是改之前的
        self.requests.append(copy.deepcopy(request))
        if self.mutate:
            # 故意污染收到的那份：调用层必须每次重建，原始请求不许被动过
            for message in request.messages:
                message["content"] = "被假客户端改过"
        index = min(len(self.requests) - 1, len(self.results) - 1)
        return self.results[index]


def make_request(text: str = "讲一句话") -> LlmRequest:
    return LlmRequest(tool="test_tool", messages=({"role": "user", "content": text},))


def run(
    client: Any,
    policy: RetryPolicy | None = None,
    *,
    request: LlmRequest | None = None,
    time: FakeTime | None = None,
) -> tuple[LlmResult, FakeTime]:
    """跑一次 ``llm.call``：假时钟 + 假随机（0.5）+ 假 sleep，等待序列完全确定。"""
    clock = time or FakeTime()
    result = call(
        request or make_request(),
        client=client,
        policy=policy or RetryPolicy(),
        sleep=clock.sleep,
        clock=clock.clock,
        random_value=lambda: 0.5,
    )
    return result, clock


# ------------------------------------------------------------------- 退避 --
def test_backoff_sequence_is_exponential_and_capped() -> None:
    policy = RetryPolicy()
    assert [compute_retry_delay(policy, retry, 0.5) for retry in range(1, 9)] == [
        500,
        1000,
        2000,
        4000,
        8000,
        10000,
        10000,
        10000,
    ]


def test_backoff_jitter_is_symmetric_and_capped() -> None:
    policy = RetryPolicy()
    assert compute_retry_delay(policy, 1, 0.0) == 450  # 500 × 0.9
    assert compute_retry_delay(policy, 1, 1.0) == 550  # 500 × 1.1
    assert compute_retry_delay(policy, 20, 1.0) == 10000  # 指数再大也封顶


# ------------------------------------------------------------------- 重试循环 --
def test_retry_loop_pins_the_wait_sequence_and_records_every_attempt() -> None:
    client = ScriptedClient(results=[model_failure("SERVER")] * 5 + [model_success("终于成功")])
    result, clock = run(client)

    assert result.ok is True and result.text == "终于成功"
    assert clock.slept == [500, 1000, 2000, 4000, 8000], "默认策略是 5 次重试、指数退避"
    assert [item.try_no for item in result.attempts] == [1, 2, 3, 4, 5, 6], "尝试条数 = 1 + 实际重试次数"
    assert [item.code for item in result.attempts] == ["SERVER"] * 5 + [None]
    assert [item.delay_ms for item in result.attempts] == [500, 1000, 2000, 4000, 8000, None]
    assert all(item.elapsed_ms == 5 for item in result.attempts), "elapsed_ms 来自注入的时钟"
    assert len(client.requests) == 6


def test_max_retries_zero_tries_exactly_once() -> None:
    client = ScriptedClient(results=[model_failure("RATE_LIMIT")])
    result, clock = run(client, RetryPolicy(max_retries=0))

    assert result.ok is False
    assert len(client.requests) == 1
    assert clock.slept == []
    assert result.attempts[0].delay_ms is None and result.attempts[0].retry_after_used is False


@pytest.mark.parametrize("code", DEFAULT_RETRYABLE_CODES)
def test_whitelisted_codes_are_retried(code: str) -> None:
    client = ScriptedClient(results=[model_failure(code), model_success()])
    result, clock = run(client, RetryPolicy(max_retries=1))

    assert result.ok is True
    assert len(client.requests) == 2 and clock.slept == [500]


@pytest.mark.parametrize("code", ["AUTH", "INVALID_REQUEST", "QUOTA", "UNKNOWN", "NOT_CONFIGURED"])
def test_codes_outside_the_whitelist_stop_after_one_attempt(code: str) -> None:
    """默认策略下这些码一次就停：401/400/配额/拿不到码的，重发只是白花钱。"""
    client = ScriptedClient(results=[model_failure(code), model_success()])
    result, clock = run(client, RetryPolicy())

    assert result.ok is False and result.failure is not None and result.failure.code == code
    assert len(client.requests) == 1
    assert clock.slept == []
    assert result.attempts[0].delay_ms is None


def test_explicit_whitelist_is_authoritative() -> None:
    """白名单是唯一权威：把 AUTH 写进白名单就会重试（默认白名单里没有它 → 默认永不重试）。"""
    auth_client = ScriptedClient(results=[model_failure("AUTH"), model_success()])
    result, clock = run(auth_client, RetryPolicy(max_retries=1, retryable_codes=("AUTH",)))
    assert result.ok is True
    assert len(auth_client.requests) == 2 and clock.slept == [500]

    rate_client = ScriptedClient(results=[model_failure("RATE_LIMIT"), model_success()])
    result, clock = run(rate_client, RetryPolicy(max_retries=1, retryable_codes=("AUTH",)))
    assert result.ok is False
    assert len(rate_client.requests) == 1 and clock.slept == []


def test_always_mode_ignores_the_whitelist_but_never_retries_never_codes() -> None:
    """``always`` 只解析不启用（配置层拒绝），这里钉死它被越过时的语义。"""
    client = ScriptedClient(results=[model_failure("TIMEOUT"), model_success()])
    result, clock = run(client, RetryPolicy(mode="always", max_retries=0))
    assert result.ok is True and len(client.requests) == 2 and clock.slept == [500]

    blocked = ScriptedClient(results=[model_failure("AUTH"), model_success()])
    result, clock = run(blocked, RetryPolicy(mode="always", max_retries=0))
    assert result.ok is False and len(blocked.requests) == 1 and clock.slept == []


# ------------------------------------------------------------ Retry-After --
def test_retry_after_within_bounds_takes_precedence_over_backoff() -> None:
    client = ScriptedClient(results=[model_failure("RATE_LIMIT", retry_after_ms=1500), model_success()])
    result, clock = run(client, RetryPolicy(max_retries=1))

    assert result.ok is True
    assert clock.slept == [1500], "界内用上游给的等待时间，不用本地退避（500）"
    assert result.attempts[0].delay_ms == 1500 and result.attempts[0].retry_after_used is True


def test_retry_after_beyond_bounds_gives_up_in_normal_mode() -> None:
    client = ScriptedClient(results=[model_failure("RATE_LIMIT", retry_after_ms=60_000), model_success()])
    result, clock = run(client, RetryPolicy(max_retries=1))

    assert result.ok is False
    assert len(client.requests) == 1 and clock.slept == []
    assert result.attempts[0].delay_ms is None and result.attempts[0].retry_after_used is False


def test_retry_after_beyond_bounds_falls_back_to_backoff_in_always_mode() -> None:
    client = ScriptedClient(results=[model_failure("RATE_LIMIT", retry_after_ms=60_000), model_success()])
    result, clock = run(client, RetryPolicy(mode="always"))

    assert result.ok is True
    assert clock.slept == [500]
    assert result.attempts[0].retry_after_used is False


def test_retry_after_equal_to_max_delay_is_still_within_bounds() -> None:
    client = ScriptedClient(results=[model_failure("SERVER", retry_after_ms=10_000), model_success()])
    result, clock = run(client, RetryPolicy(max_retries=1))

    assert result.ok is True and clock.slept == [10_000]
    assert result.attempts[0].retry_after_used is True


# ------------------------------------------------------------ 请求重建 --
def test_request_is_rebuilt_for_every_attempt() -> None:
    original = make_request()
    snapshot = original.messages[0]["content"]
    client = ScriptedClient(results=[model_failure("SERVER"), model_success()], mutate=True)
    result, _clock = run(client, RetryPolicy(max_retries=1), request=original)

    assert result.ok is True
    assert len(client.requests) == 2
    assert original.messages[0]["content"] == snapshot, "原始请求（提示词）逐字未变"
    assert client.requests[0] is not client.requests[1]
    assert client.requests[0].messages is not client.requests[1].messages
    assert client.requests[1].messages[0]["content"] == snapshot, "第二次尝试拿到的仍是原始请求"


def test_not_configured_returns_result_without_sending_anything() -> None:
    client = ScriptedClient(results=[model_success()], configured=False)
    result, clock = run(client)

    assert result.ok is False and result.failure is not None and result.failure.code == "NOT_CONFIGURED"
    assert result.attempts == () and client.requests == [] and clock.slept == []

    result_none, _clock = run(None)
    assert result_none.ok is False and result_none.failure is not None
    assert result_none.failure.code == "NOT_CONFIGURED" and "AR_API_KEY" in result_none.failure.message


def test_unexpected_client_exception_becomes_unknown_and_is_not_retried() -> None:
    class Boom:
        configured = True

        async def arequest_once(self, request: LlmRequest) -> LlmResult:
            raise RuntimeError("客户端自己炸了")

    result, clock = run(Boom(), RetryPolicy())

    assert result.ok is False and result.failure is not None and result.failure.code == "UNKNOWN"
    assert "客户端自己炸了" in result.failure.message
    assert clock.slept == [] and len(result.attempts) == 1


# ------------------------------------------------------------ 账目形态 --
def test_attempt_records_and_retry_account_shapes() -> None:
    attempts = (
        LlmAttempt(try_no=1, elapsed_ms=12, code="SERVER", delay_ms=500),
        LlmAttempt(try_no=2, elapsed_ms=9, code="RATE_LIMIT", delay_ms=1000, retry_after_used=True),
        LlmAttempt(try_no=3, elapsed_ms=7, code=None, delay_ms=None),
    )
    records = attempt_records("rewrite_batch", attempts)
    assert records[0] == {
        "tool": "rewrite_batch",
        "try": 1,
        "elapsed_ms": 12,
        "code": "SERVER",
        "delay_ms": 500,
        "retry_after_used": False,
    }
    assert [item["try"] for item in records] == [1, 2, 3]
    assert retry_account("rewrite_batch", attempts) == {
        "tool": "rewrite_batch",
        "requests": 3,
        "retries": 2,
        "codes": ["SERVER", "RATE_LIMIT"],
    }
    assert retry_account("rewrite_batch", ()) == {
        "tool": "rewrite_batch",
        "requests": 0,
        "retries": 0,
        "codes": [],
    }


# ------------------------------------------------- 面向用户的失败文案（P1a） --
def test_failure_message_reports_the_attempt_count_and_the_last_code() -> None:
    """文案 = 「模型调用失败（已试 N 次，末次 <CODE>）：…」：N 是真实请求数、CODE 是末次错误码。

    迁移前这句带次数（``ModelCallFailed`` 拼的），迁移后一度丢了——调用方把它整段贴进
    ``/api/explain`` 的降级 ``text`` / ``ar analyze`` 的报错，于是「重试白花了几次」在用户
    可见的地方看不出来。
    """
    client = ScriptedClient(results=[model_failure("SERVER"), model_failure("RATE_LIMIT")])
    result, _clock = run(client, RetryPolicy(max_retries=1))

    assert result.ok is False and len(client.requests) == 2
    assert failure_message(result) == "模型调用失败（已试 2 次，末次 RATE_LIMIT）：RATE_LIMIT 测试失败"


def test_failure_message_is_one_attempt_when_retries_are_off() -> None:
    client = ScriptedClient(results=[model_failure("AUTH")])
    result, _clock = run(client, RetryPolicy(max_retries=5))
    assert failure_message(result).startswith("模型调用失败（已试 1 次，末次 AUTH）：")


def test_failure_message_says_zero_attempts_when_nothing_was_sent() -> None:
    """未配置：一次请求都没发，如实写「已试 0 次」（与「发了但都失败」分得开）。"""
    text = failure_message(not_configured_result())
    assert text.startswith("模型调用失败（已试 0 次，末次 NOT_CONFIGURED）：")
    assert "AR_API_KEY" in text


def test_failure_message_never_carries_the_key() -> None:
    """文案要进 HTTP 响应与产物：网关把密钥回显在错误正文里也不许漏出去（再兜一次 redact）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"boom: Authorization: Bearer {FAKE_KEY}")

    client = ModelClient(_client_config(max_retries=0), transport=httpx.MockTransport(handler))
    result, _clock = run(client, client.retry_policy())

    assert result.ok is False and result.failure is not None and result.failure.code == "SERVER"
    text = failure_message(result)
    assert text.startswith("模型调用失败（已试 1 次，末次 SERVER）：")
    assert FAKE_KEY not in text and FAKE_KEY[3:] not in text and "Bearer sk-" not in text
    assert "REDACTED" in text, "脱敏痕迹在（说明真的过了一遍 redact）"


# ------------------------------------------------------------ 策略解析 --
def test_resolve_retry_policy_defaults() -> None:
    policy = resolve_retry_policy(None)
    assert policy == RetryPolicy()
    assert policy.max_retries == 5
    assert policy.retryable_codes == DEFAULT_RETRYABLE_CODES
    assert (policy.initial_delay_ms, policy.max_delay_ms, policy.jitter_ratio) == (500, 10_000, 0.1)


def test_resolve_retry_policy_reads_the_configured_section() -> None:
    policy = resolve_retry_policy(
        {
            "mode": "normal",
            "maxRetries": 2,
            "retryableCodes": ["SERVER", "rate_limit"],
            "backoff": {"initialDelayMs": 100, "maxDelayMs": 2000, "jitterRatio": 0},
        }
    )
    assert policy.max_retries == 2
    assert policy.retryable_codes == ("SERVER", "RATE_LIMIT")  # 码统一成大写、去重保序
    assert (policy.initial_delay_ms, policy.max_delay_ms, policy.jitter_ratio) == (100, 2000, 0.0)


def test_resolve_retry_policy_partial_section_keeps_other_defaults() -> None:
    policy = resolve_retry_policy({"maxRetries": 0})
    assert policy.max_retries == 0
    assert policy.retryable_codes == DEFAULT_RETRYABLE_CODES
    assert policy.max_delay_ms == 10_000


def test_resolve_retry_policy_ignores_unknown_keys() -> None:
    """未知键由 ``ar.config`` 收进 ``unknown_keys`` 告警；解析器本身只认它认识的键。"""
    assert resolve_retry_policy({"nope": 1, "_note": "注释"}) == RetryPolicy()


@pytest.mark.parametrize(
    "raw",
    [
        "normal",  # 不是对象
        {"mode": "weird"},
        {"mode": "always"},  # 无上限重试本项目不实现
        {"maxRetries": -1},
        {"maxRetries": "5"},
        {"maxRetries": True},
        {"retryableCodes": "AUTH"},
        {"retryableCodes": [1]},
        {"retryableCodes": ["NOPE"]},
        {"retryableCodes": []},  # 空白名单：合法（= 永不重试），但写法要显式才算数
        {"backoff": 5},
        {"backoff": {"initialDelayMs": -1}},
        {"backoff": {"initialDelayMs": 5000, "maxDelayMs": 1000}},
        {"backoff": {"jitterRatio": 1.5}},
        {"backoff": {"jitterRatio": -0.1}},
    ],
)
def test_resolve_retry_policy_rejects_illegal_values(raw: Any) -> None:
    if raw == {"retryableCodes": []}:
        assert resolve_retry_policy(raw).retryable_codes == ()
        return
    with pytest.raises(InputError):
        resolve_retry_policy(raw)


# ------------------------------------------------------------ 配置接线 --
def _write_config(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_missing_retry_section_means_builtin_defaults(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, {"api_key": ""}), env={})
    assert config.retry is None
    assert resolve_retry_policy(config.retry).max_retries == 5


def test_legacy_top_level_max_retries_is_mapped_with_a_deprecation_note(tmp_path: Path) -> None:
    path = _write_config(tmp_path, {"api_key": "", "max_retries": 0})
    with pytest.warns(UserWarning, match="max_retries"):
        config = load_config(path, env={})
    assert config.retry == {"maxRetries": 0}
    assert config.retry_notes and "废弃" in config.retry_notes[0]
    assert resolve_retry_policy(config.retry).max_retries == 0


def test_retry_section_wins_over_legacy_key(tmp_path: Path) -> None:
    path = _write_config(tmp_path, {"api_key": "", "max_retries": 3, "retry": {"maxRetries": 7}})
    with pytest.warns(UserWarning, match="以 retry 段为准"):
        config = load_config(path, env={})
    assert resolve_retry_policy(config.retry).max_retries == 7


def test_unknown_retry_keys_go_to_the_unknown_key_warning(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {"api_key": "", "retry": {"maxRetries": 1, "nope": 1, "_note": "注释", "backoff": {"oops": 1}}},
    )
    config = load_config(path, env={})
    assert config.unknown_keys == ("retry.backoff.oops", "retry.nope"), "下划线键是注释，不进告警"


def test_illegal_retry_section_fails_the_load(tmp_path: Path) -> None:
    path = _write_config(tmp_path, {"api_key": "", "retry": {"backoff": {"initialDelayMs": 9000, "maxDelayMs": 100}}})
    with pytest.raises(InputError, match="initialDelayMs"):
        load_config(path, env={})


def test_model_client_uses_the_configured_policy(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, {"api_key": FAKE_KEY, "retry": {"maxRetries": 2}}), env={})
    client = ModelClient(config, transport=httpx.MockTransport(lambda request: sse_response()))
    assert client.retry_policy().max_retries == 2
    assert client.configured is True


# ------------------------------------------------- 流提前结束（范本盲点对照） --
def test_stream_cut_without_done_is_transport_and_is_retried() -> None:
    """范本对「没有终止事件就被掐断」不重试（STREAM_CLOSED 不在白名单）；本项目把它归 TRANSPORT 并重试。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            # 有正文但没有 [DONE]、也没有 finish_reason：流被掐断
            return sse_response([{"model": "fake/model", "choices": [{"delta": {"content": "半截话"}}]}], done=False)
        return sse_response()

    client = ModelClient(_client_config(max_retries=1), transport=httpx.MockTransport(handler))
    result, clock = run(client, client.retry_policy())

    assert result.ok is True and result.text == "你好，世界", "重试后拿到完整正文"
    assert len(calls) == 2 and clock.slept == [500]
    assert result.attempts[0].code == "TRANSPORT"


def test_stream_cut_without_retries_is_a_failure_not_a_partial_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response([{"model": "fake/model", "choices": [{"delta": {"content": "半截话"}}]}], done=False)

    client = ModelClient(_client_config(max_retries=0), transport=httpx.MockTransport(handler))
    result, clock = run(client, client.retry_policy())

    assert result.ok is False
    assert result.failure is not None and result.failure.code == "TRANSPORT"
    assert result.text == "", "半截正文不许当成功用"
    assert clock.slept == []


def test_stream_with_finish_reason_but_no_done_marker_is_still_ok() -> None:
    events = [
        {"model": "fake/model", "choices": [{"delta": {"content": "收尾"}, "finish_reason": "stop"}]},
    ]
    client = ModelClient(
        _client_config(max_retries=0),
        transport=httpx.MockTransport(lambda request: sse_response(events, done=False)),
    )
    result, _clock = run(client, client.retry_policy())

    assert result.ok is True and result.text == "收尾" and result.finish_reason == "stop"


def test_call_is_sync_and_acall_is_async() -> None:
    """同步 ``call`` 内部就是 ``asyncio.run(acall(...))``（两者行为一致）。"""
    from ar.llm import acall

    client = ScriptedClient(results=[model_failure("SERVER"), model_success("好了")])
    clock = FakeTime()
    result = asyncio.run(
        acall(
            make_request(),
            client=client,
            policy=RetryPolicy(max_retries=1),
            sleep=clock.sleep,
            clock=clock.clock,
            random_value=lambda: 0.5,
        )
    )
    assert result.ok is True and result.text == "好了" and clock.slept == [500]
