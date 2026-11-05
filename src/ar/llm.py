"""统一模型调用层（PLAN-llm-tools §3.1）：重试策略、错误码白名单、逐次尝试记账。

四个业务模块与工具层都只经这里调模型：``call``（同步）/ ``acall``（异步）。
**失败不抛**——结果一律是 ``LlmResult``（只有「未配置密钥」在发请求前就返回 ``ok=False``）。

四条纪律（照规格书写死）：

1. **失败不抛**：调用失败是返回值，调用方据 ``failure.code`` 决定降级 / 退出码；
2. **可重试用白名单**：只有 ``policy.retryable_codes`` 里的码才重试
   （唯一例外：``QUOTA_RATE_WINDOW`` = 窗口额度耗尽，**即使显式写进白名单也不重试**）
   （默认 ``EMPTY_RESPONSE / RATE_LIMIT / SERVER / TIMEOUT / TRANSPORT``）；
   拿不到码的 ``UNKNOWN`` 宁可不动，也不猜着重发；
3. **先记账再等待**：``LlmAttempt`` 在 sleep 之前就生成，产物据此能看到「第几次失败、等了多久」；
4. **每次尝试都从原始 ``LlmRequest`` 重建请求**：重试不带着上一次的残留。

多源回退链的换跳**不在这一层**：``ModelClient.arequest_once`` 自己在链上逐跳（见 ``ar.chain``），
本层的一次「尝试」= 整条链走一遍；链上每跳的失败账写在 ``failure.message`` 里（``回退链全部失败（a=AUTH；b=QUOTA）…``）。

失败文案只有一处定义：``failure_message(result)`` 产出「模型调用失败（已试 N 次，末次 <CODE>）：<脱敏消息>」，
四个调用点（``explain`` / ``analyze`` / ``rewrite`` / ``survey``）在失败分支一律用它——
重试花掉的钱必须落在用户看得见的地方（``/api/explain`` 的降级 ``text``、CLI 报错、
``meta.model_errors[].error``），否则「试了 6 次」与「一次就失败」在用户眼里没差别。

退避照 DSH 内置重试（``dsh-llm-retry``）：指数 × 对称抖动，再封顶；
``Retry-After`` 在 ``maxDelayMs`` 界内**优先用它**（``retry_after_used=True``），
超界则 ``normal`` 模式直接放弃重试、不拿本地退避凑。

范本自身的盲点本项目已修掉：宿主在「流没有终止事件就被掐断」时抛的 ``STREAM_CLOSED`` 不在默认可重试
集合里（于是不重试）；本项目把「流意外提前结束」一律归到**可重试**的 ``TRANSPORT``
（归一在 ``ar.model``，见那里的错误码表）。

注入缝（测试用假时钟 / 假随机把等待序列钉死）：``sleep``（收毫秒）、``clock``（返回毫秒）、
``random_value``（返回 ``[0, 1)``）；默认走标准库，业务代码不必传。
"""

from __future__ import annotations

import asyncio
import inspect
import random as _random
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable, Sequence

from .errors import InputError

if TYPE_CHECKING:  # 只为类型标注：运行期不导入 model，避免与 model → llm 形成环
    from .model import ModelClient

__all__ = [
    "DEFAULT_RETRYABLE_CODES",
    "NEVER_RETRY_CODES",
    "UNKNOWN_CODE",
    "UNKNOWN_MESSAGE",
    "WINDOW_QUOTA_CODE",
    "KNOWN_CODES",
    "RETRY_MODES",
    "RETRY_SECTION_KEYS",
    "RETRY_BACKOFF_KEYS",
    "NOT_CONFIGURED_MESSAGE",
    "RetryPolicy",
    "LlmRequest",
    "LlmFailure",
    "LlmAttempt",
    "LlmResult",
    "compute_retry_delay",
    "should_retry",
    "resolve_retry_policy",
    "attempt_records",
    "retry_account",
    "failure_message",
    "not_configured_result",
    "call",
    "acall",
]

#: 默认白名单（§2）：这些码失败才值得再试一次
DEFAULT_RETRYABLE_CODES = ("EMPTY_RESPONSE", "RATE_LIMIT", "SERVER", "TIMEOUT", "TRANSPORT")
#: 确定性错误：即使被写进 ``retryableCodes`` 也不会在 ``always`` 模式下重试；
#: 默认白名单里没有它们，所以**默认策略下它们永不重试**（§2 / §3.2）
NEVER_RETRY_CODES = ("AUTH", "INVALID_REQUEST", "QUOTA", "NOT_CONFIGURED")
#: 归不到任何已知码时的兜底（§3.2 的「其它」，同样不重试）
UNKNOWN_CODE = "UNKNOWN"
#: 窗口额度耗尽（插件 0.9.1 的修复）：不重试（等不到窗口重置）；冷却由 ``ar.model`` 的两步记账负责
WINDOW_QUOTA_CODE = "QUOTA_RATE_WINDOW"
#: 配置里 ``retryableCodes`` 允许出现的码（写错一个码会让重试静默失效，所以直接报错）
KNOWN_CODES = (*DEFAULT_RETRYABLE_CODES, *NEVER_RETRY_CODES, UNKNOWN_CODE, WINDOW_QUOTA_CODE)

RETRY_MODES = ("normal", "always")
#: ``retry`` 段允许的键（未知键走 ``unknown_keys`` 告警机制，见 ``ar.config``）
RETRY_SECTION_KEYS = ("mode", "maxRetries", "retryableCodes", "backoff")
RETRY_BACKOFF_KEYS = ("initialDelayMs", "maxDelayMs", "jitterRatio")

NOT_CONFIGURED_MESSAGE = (
    "模型未配置：请设置环境变量 AR_API_KEY，或在 config.local.json 里填 api_key"
    "（两者都为空时任何模型调用都会跳过）"
)
#: 失败结果没带原因时的兜底文案（``acall`` 与 ``failure_message`` 共用一份）
UNKNOWN_MESSAGE = "模型调用失败（没有给出原因）"


@dataclass(frozen=True)
class RetryPolicy:
    """重试策略（默认值 = §2 的 DSH 内置口径）。"""

    mode: str = "normal"
    max_retries: int = 5
    retryable_codes: tuple[str, ...] = DEFAULT_RETRYABLE_CODES
    initial_delay_ms: int = 500
    max_delay_ms: int = 10_000
    jitter_ratio: float = 0.1


@dataclass(frozen=True)
class LlmRequest:
    """一次模型调用的输入（``tool`` 只用于记账，不参与提示词）。"""

    tool: str
    messages: tuple[dict[str, str], ...]
    temperature: float | None = None
    max_tokens: int | None = None
    prompt_version: str | None = None


@dataclass(frozen=True)
class LlmFailure:
    """归一后的失败（``message`` 已过 ``redact()``，可安全打印 / 落盘）。"""

    code: str
    message: str
    status: int | None = None
    retry_after_ms: int | None = None
    #: 这次尝试的耗时（毫秒，单调时钟量出来）；``None`` = 没量（例如手动构造的失败）
    elapsed_ms: int | None = None
    #: 重试判定的**覆盖值**（``None`` = 按 ``code`` 判）。整链失败时由链层算出"有没有哪一跳本来就
    #: 值得重试"：稳定的聚合码 ``AUTO_ROUTES_EXHAUSTED`` 不该把瞬时的重试能力一起吞掉
    retryable: bool | None = None


@dataclass(frozen=True)
class LlmAttempt:
    """一次尝试的账目（**先记账再等待**：``delay_ms`` 是这次失败后要等多久）。"""

    try_no: int
    elapsed_ms: int
    code: str | None
    delay_ms: int | None
    retry_after_used: bool = False


@dataclass(frozen=True)
class LlmResult:
    """一次调用的结果（成功 / 失败都走它，不抛异常）。"""

    ok: bool
    text: str = ""
    usage: dict | None = None
    model: str | None = None
    failure: LlmFailure | None = None
    attempts: tuple[LlmAttempt, ...] = ()
    degraded_params: tuple[str, ...] = ()
    #: 追加字段（§3.1 未列）：``ModelReply.finish_reason`` 是既有公开字段，
    #: ``ModelClient.achat`` 经本层构造 ``ModelReply`` 时必须原样带回去，否则外观变了。
    finish_reason: str | None = None
    #: 追加字段：链上**没发请求就跳过**的跳（``ar.model.SkippedRoute``，冷却中 / 本轮短闸门）。
    #: 本层只负责原样透传，不解释它——``ar.routes explain`` 与排查靠它看到「为什么这次绕过了某家」。
    skipped: tuple[Any, ...] = ()


# ------------------------------------------------------------------ 退避与判据 --
def compute_retry_delay(policy: RetryPolicy, retry: int, random_value: float) -> int:
    """第 ``retry`` 次重试（1 起）要等多少毫秒：指数 × 对称抖动，再封顶。

    ``exponential = min(initial * 2 ** (retry - 1), max_delay)``；
    ``jitter = 1 - ratio + 2 * ratio * random_value``；``delay = min(exponential * jitter, max_delay)``。
    ``random_value = 0.5`` 时抖动为 1，序列正好是 500 / 1000 / 2000 / 4000 / 8000 / 10000（封顶）。
    """
    exponent = min(max(int(retry), 1) - 1, 1024)
    exponential = min(policy.initial_delay_ms * 2**exponent, policy.max_delay_ms)
    jitter = 1 - policy.jitter_ratio + 2 * policy.jitter_ratio * float(random_value)
    return int(min(exponential * jitter, policy.max_delay_ms))


def should_retry(
    policy: RetryPolicy, code: str | None, attempt_no: int, *, retryable: bool | None = None
) -> bool:
    """要不要为这次的失败再来一次（§3.1 规则 2）。

    ``retryable`` 是链层给的**覆盖值**（见 ``LlmFailure.retryable``）：``False`` 一律不重试；
    ``True`` = 链上至少有一跳本来就值得重试 ⇒ 跳过大白名单（仍受次数上限约束）。

    - ``normal``：**白名单是唯一权威**——不在 ``retryable_codes`` 里的一次就停
      （默认白名单不含 ``NEVER_RETRY_CODES``，所以默认策略下 AUTH / INVALID_REQUEST /
      QUOTA / NOT_CONFIGURED / UNKNOWN 一律只试一次）；
    - ``always``：忽略白名单、不限次数，但 ``NEVER_RETRY_CODES`` 仍然挡住
      （§7：``always`` 只解析不启用，配置层直接拒绝，见 ``resolve_retry_policy``）。

    ⚠ 规格书内部有冲突，这里按「白名单权威」实现，理由与证据写在 §6 验收 4 上：

    - §3.1 规则 2 字面看是「``code not in NEVER_RETRY_CODES``」——那 ``retryableCodes=["AUTH"]``
      时 401 也不会重试；
    - §6 验收 4 要求「把 ``retryableCodes`` 改成 ``["AUTH"]``，断言 401 会重试而 429 不会」。
    - 两条不可能同时成立。选后者的原因：① 它是可执行的验收条目；② ``NEVER_RETRY_CODES``
      的实质作用是「默认白名单里排除这些码」与「``always`` 模式下的兜底」，
      而**显式**把某个码写进白名单是调用方的明确要求，不该被静默否决；
      ③ 默认策略下 401 / 400 / 配额 / UNKNOWN 仍然一次就停（有测试与实测证据）。
      若要改回字面版，只需在本函数开头加一行 ``if name in NEVER_RETRY_CODES: return False``。
    """
    name = str(code or UNKNOWN_CODE)
    if retryable is False:
        return False
    if name == WINDOW_QUOTA_CODE:
        # 窗口额度耗尽：**任何模式都不重试**（插件 0.9.1 的硬口径）。哪怕调用方把它写进
        # retryableCodes 也不重试——等不到窗口重置的重试只是白撞上游、白等退避。
        return False
    if retryable is True:
        # 链层已按"每一跳各自值不值得重试"算过；这里只守次数上限（always 模式不限次数）
        return policy.mode == "always" or attempt_no <= policy.max_retries
    if policy.mode == "always":
        return name not in NEVER_RETRY_CODES
    if attempt_no > policy.max_retries:
        return False
    return name in policy.retryable_codes


def _plan_delay(
    policy: RetryPolicy,
    failure: LlmFailure,
    attempt_no: int,
    random_value: Callable[[], float],
) -> tuple[int | None, bool]:
    """这次失败之后等多久：``(delay_ms, retry_after_used)``；不重试时 ``delay_ms=None``。

    ``random_value`` 只在真的要算本地退避时才取值：用上游 ``Retry-After`` 时不消耗随机数，
    注入的假随机会因此与等待序列一一对应（测试能把序列钉死）。
    """
    if not should_retry(policy, failure.code, attempt_no, retryable=failure.retryable):
        return None, False
    retry_after = failure.retry_after_ms
    if retry_after is not None:
        if retry_after <= policy.max_delay_ms:
            # 界内：听上游的（比自己猜的退避更准）
            return max(0, int(retry_after)), True
        if policy.mode == "normal":
            # 界外：normal 模式直接放弃重试（照范本，不拿本地退避凑）
            return None, False
    return compute_retry_delay(policy, attempt_no, random_value()), False


# ---------------------------------------------------------------- 配置解析 --
def _int_value(raw: dict[str, Any], key: str, default: int, *, minimum: int) -> int:
    value = raw.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"配置项 retry.{key} 必须是整数，实际是 {type(value).__name__}")
    if value < minimum:
        raise InputError(f"配置项 retry.{key} 不能小于 {minimum}，实际是 {value}")
    return value


def _float_value(raw: dict[str, Any], key: str, default: float, *, low: float, high: float) -> float:
    value = raw.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"配置项 retry.{key} 必须是数字，实际是 {type(value).__name__}")
    number = float(value)
    if not low <= number <= high:
        raise InputError(f"配置项 retry.{key} 必须在 {low}–{high} 之间，实际是 {value}")
    return number


def resolve_retry_policy(raw: dict | None) -> RetryPolicy:
    """解析配置里的 ``retry`` 段（``None`` = 没配 → §2 默认值）。

    非法值**一律报错**（``InputError`` → CLI 退出码 2），不静默回落到默认值：
    负数、``initialDelayMs > maxDelayMs``、``jitterRatio`` 越界、``retryableCodes`` 里有未知码、
    ``mode=always``（本项目不实现无上限重试）。未知键由 ``ar.config`` 收进 ``unknown_keys`` 告警。
    """
    if raw is None:
        return RetryPolicy()
    if not isinstance(raw, dict):
        raise InputError(f"配置项 retry 必须是 JSON 对象，实际是 {type(raw).__name__}")

    mode = raw.get("mode", "normal")
    if mode not in RETRY_MODES:
        raise InputError(f"配置项 retry.mode 只能是 {' / '.join(RETRY_MODES)}，实际是 {mode!r}")
    if mode == "always":
        # §2 / §7：always 是无上限重试，本项目不实现；明确拒绝，不静默按 normal 跑
        raise InputError("配置项 retry.mode=always（无上限重试）本项目不实现：请用 normal 并配 maxRetries")

    max_retries = _int_value(raw, "maxRetries", 5, minimum=0)

    codes_raw = raw.get("retryableCodes", DEFAULT_RETRYABLE_CODES)
    if not isinstance(codes_raw, (list, tuple)) or isinstance(codes_raw, (str, bytes)):
        raise InputError("配置项 retry.retryableCodes 必须是错误码数组")
    codes: list[str] = []
    for item in codes_raw:
        if not isinstance(item, str) or not item.strip():
            raise InputError(f"配置项 retry.retryableCodes 里有非法项 {item!r}（只能是非空字符串）")
        code = item.strip().upper()
        if code not in KNOWN_CODES:
            raise InputError(
                f"配置项 retry.retryableCodes 里有未知错误码 {item!r}（可用：{'、'.join(KNOWN_CODES)}）"
            )
        if code not in codes:
            codes.append(code)

    backoff_raw = raw.get("backoff")
    if backoff_raw is None:
        backoff_raw = {}
    if not isinstance(backoff_raw, dict):
        raise InputError(f"配置项 retry.backoff 必须是 JSON 对象，实际是 {type(backoff_raw).__name__}")
    initial = _int_value(backoff_raw, "initialDelayMs", 500, minimum=0)
    max_delay = _int_value(backoff_raw, "maxDelayMs", 10_000, minimum=0)
    if initial > max_delay:
        raise InputError(
            f"配置项 retry.backoff.initialDelayMs（{initial}）不能大于 maxDelayMs（{max_delay}）"
        )
    jitter = _float_value(backoff_raw, "jitterRatio", 0.1, low=0.0, high=1.0)

    return RetryPolicy(
        mode=mode,
        max_retries=max_retries,
        retryable_codes=tuple(codes),
        initial_delay_ms=initial,
        max_delay_ms=max_delay,
        jitter_ratio=jitter,
    )


# ------------------------------------------------------------------ 账目形态 --
def attempt_records(tool: str, attempts: Sequence[LlmAttempt]) -> list[dict[str, Any]]:
    """逐次尝试的账目（落产物的 ``meta.attempts``，结构照 §3.1 规则 7）。"""
    return [
        {
            "tool": tool,
            "try": item.try_no,
            "elapsed_ms": item.elapsed_ms,
            "code": item.code,
            "delay_ms": item.delay_ms,
            "retry_after_used": item.retry_after_used,
        }
        for item in attempts
    ]


def retry_account(tool: str, attempts: Sequence[LlmAttempt]) -> dict[str, Any]:
    """整次调用一条汇总：``{tool, requests, retries, codes}``（``codes`` = 失败过的码，去重保序）。"""
    codes: list[str] = []
    for item in attempts:
        if item.code and item.code not in codes:
            codes.append(item.code)
    return {
        "tool": tool,
        "requests": len(attempts),
        "retries": max(0, len(attempts) - 1),
        "codes": codes,
    }


def _redacted(text: str) -> str:
    """本层自己的脱敏兜底（``ar.config`` 在模块级导入本模块，顶层导入会成环，只能延迟导入）。"""
    from .config import redact  # noqa: PLC0415 - 避免 config ↔ llm 的模块级循环导入

    return redact(text)


def failure_message(result: LlmResult) -> str:
    """失败结果的人读文案：``模型调用失败（已试 N 次，末次 <CODE>）：<脱敏消息>``。

    **四个调用点（``explain`` / ``analyze`` / ``rewrite`` / ``survey``）在失败分支一律用它**：
    迁移前那句「模型调用失败（已试 N 次）：…」是面向用户文本的一部分（``/api/explain`` 的降级
    ``text``、``ar analyze`` / ``ar rewrite`` 的报错、``meta.model_errors[].error``、追问的降级说明），
    少了次数就看不出「重试白花了几次」。

    - ``N`` = 这次调用**实际发出的请求数**（``len(result.attempts)``，含首发与每次重试）；
      未配置密钥时是 ``0``（一次请求都没发）；
    - ``<CODE>`` = **末次**失败的错误码（逐次账里最后一条的 ``code``；没有账目时取
      ``result.failure.code``）；
    - 消息取自 ``result.failure.message``（``ar.model`` 已过 ``redact()``），拼好后再整段过一遍
      ``redact()`` 兜底：文案要进 HTTP 响应与产物，**密钥一个字符都不许出现**。
    """
    failure = result.failure or LlmFailure(code=UNKNOWN_CODE, message=UNKNOWN_MESSAGE)
    attempts = tuple(result.attempts or ())
    last_code = attempts[-1].code if attempts else None
    code = str(last_code or failure.code or UNKNOWN_CODE)
    return _redacted(f"模型调用失败（已试 {len(attempts)} 次，末次 {code}）：{failure.message}")


def not_configured_result(message: str | None = None) -> LlmResult:
    """未配置密钥时的结果（**不发请求、不重试**）；``message`` 给了就用它（多源模式的链上全缺密钥）。"""
    return LlmResult(ok=False, failure=LlmFailure(code="NOT_CONFIGURED", message=message or NOT_CONFIGURED_MESSAGE))


# -------------------------------------------------------------------- 调用 --
def _clock_ms() -> float:
    """默认时钟：单调时钟，单位毫秒（``elapsed_ms`` 由两次读数相减得到）。"""
    return time.monotonic() * 1000.0


async def _sleep_ms(delay_ms: int) -> None:
    """默认等待：毫秒进、秒出（测试注入的假 sleep 收毫秒、同步即可）。"""
    await asyncio.sleep(max(0, int(delay_ms)) / 1000)


def _rebuild(request: LlmRequest) -> LlmRequest:
    """从原始请求重建一份（每次尝试都调用它，重试不带上一次的残留）。"""
    return LlmRequest(
        tool=request.tool,
        messages=tuple(dict(message) for message in request.messages),
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        prompt_version=request.prompt_version,
    )


async def _request_once(client: Any, request: LlmRequest) -> LlmResult:
    """让客户端发一次请求，并把意外异常也收成 ``LlmResult``（本层契约是「失败不抛」）。"""
    try:
        result = await client.arequest_once(request)
    except Exception as exc:  # noqa: BLE001 - 「失败不抛」是统一调用层的契约
        return LlmResult(
            ok=False,
            failure=LlmFailure(code=UNKNOWN_CODE, message=f"模型调用异常（{type(exc).__name__}）：{exc}"),
        )
    if not isinstance(result, LlmResult):
        return LlmResult(
            ok=False,
            failure=LlmFailure(
                code=UNKNOWN_CODE, message=f"模型客户端返回了非 LlmResult 的结果（{type(result).__name__}）"
            ),
        )
    return result


async def acall(
    request: LlmRequest,
    *,
    client: "ModelClient | None",
    policy: RetryPolicy,
    sleep: Callable[[int], Any] | None = None,
    clock: Callable[[], float] | None = None,
    random_value: Callable[[], float] | None = None,
) -> LlmResult:
    """按策略调一次模型；**不抛**（未配置除外——它在发请求前就返回 ``ok=False`` 的结果）。

    ``sleep`` 收毫秒（默认 ``asyncio.sleep``，注入的同步假实现也认）；``clock`` 返回毫秒；
    ``random_value`` 返回 ``[0, 1)``。三者都只为测试钉死等待序列。
    """
    do_sleep = sleep or _sleep_ms
    now = clock or _clock_ms
    rng = random_value or _random.random

    if client is None or not getattr(client, "configured", True):
        routes = getattr(getattr(client, "config", None), "routes", ()) or ()
        if routes:
            labels = "、".join(route.label for route in routes)
            return not_configured_result(
                f"模型未配置：回退链（{labels}）上没有任何一跳声明了可用的密钥（api_key_env）；"
                "设置对应环境变量，或在 ~/.dsh/.credentials.yaml 的 refs 里补上同名凭据"
            )
        return not_configured_result()
    if policy.mode not in RETRY_MODES:  # pragma: no cover - 配置层已挡住，手工构造策略才可能撞上
        raise InputError(f"retry.mode 非法：{policy.mode!r}（只能是 {' / '.join(RETRY_MODES)}）")

    attempts: list[LlmAttempt] = []
    try_no = 0
    while True:
        try_no += 1
        started = now()
        result = await _request_once(client, _rebuild(request))
        elapsed_ms = int(round(now() - started))
        result = replace(result, failure=(replace(result.failure, elapsed_ms=elapsed_ms) if result.failure else None))

        if result.ok:
            attempts.append(LlmAttempt(try_no=try_no, elapsed_ms=elapsed_ms, code=None, delay_ms=None))
            return replace(result, attempts=tuple(attempts), skipped=tuple(result.skipped))

        failure = result.failure or LlmFailure(code=UNKNOWN_CODE, message=UNKNOWN_MESSAGE)
        delay_ms, retry_after_used = _plan_delay(policy, failure, try_no, rng)
        # 先记账再等待：产物里能看到「第几次失败、等了多久」，哪怕进程在 sleep 中被掐断
        attempts.append(
            LlmAttempt(
                try_no=try_no,
                elapsed_ms=elapsed_ms,
                code=failure.code,
                delay_ms=delay_ms,
                retry_after_used=retry_after_used,
            )
        )
        if delay_ms is None:
            return replace(result, failure=failure, attempts=tuple(attempts), skipped=tuple(result.skipped))
        waited = do_sleep(delay_ms)
        if inspect.isawaitable(waited):
            await waited


def call(
    request: LlmRequest,
    *,
    client: "ModelClient | None",
    policy: RetryPolicy,
    sleep: Callable[[int], Any] | None = None,
    clock: Callable[[], float] | None = None,
    random_value: Callable[[], float] | None = None,
) -> LlmResult:
    """同步包装（CLI / 工具层用）；不要在已有事件循环里调用。"""
    return asyncio.run(
        acall(request, client=client, policy=policy, sleep=sleep, clock=clock, random_value=random_value)
    )
