"""多源回退链（``ar.chain`` + ``ModelClient` 的链上换跳）。

固定这些预期：客户端层与配置层的**老行为逐字不变**（链是可选的），链上则：

1. 按配置顺序逐跳试，任一跳成功即返回，且**成功后才不再往后走**；
2. 某跳失败就换下一跳（唯一不换：调用方取消——本项目目前没有取消路径，所以是「一律换」）；
3. 额度/鉴权类失败给这一家记冷却，冷却中的跳随后被跳过；**全冷时照链硬走**；
4. 换跳前摘掉历史里的思考字段；
5. 密钥现读环境变量 > ``~/.dsh/.credentials.yaml`` 的 ``refs``，**永远不落盘、不进报错**；
6. 整条链都失败时，报错点名每一个试过的跳。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from ar.chain import (
    DEFAULT_ROUTES_YAML,
    CooldownTable,
    Route,
    load_routes_file,
    order_routes,
    parse_reset_at,
    parse_routes,
    resolve_api_key,
)
from ar.quota import QuotaReport, QuotaSource
from ar.config import Config
from ar.errors import InputError
from ar.llm import LlmFailure, LlmRequest, LlmResult
from ar.model import ModelCallFailed, ModelClient

FAKE_KEY_A = "cmd-key-" + "a" * 20
FAKE_KEY_B = "oc-key-" + "b" * 20


def route(provider: str, model: str, env_name: str) -> Route:
    return Route(provider=provider, model=model, base_url=f"https://{provider}.invalid/v1", api_key_env=env_name)


def iso_ms(ms: int | float) -> str:
    """epoch 毫秒 → 带 ``Z`` 的 ISO 串（额度接口的 ``currentPeriodEnd`` 就长这样）。"""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(float(ms) / 1000.0, timezone.utc).isoformat().replace("+00:00", "Z")


def sse_ok(text: str) -> httpx.Response:
    events = [
        {"model": "fake/model", "choices": [{"delta": {"content": text}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    ]
    body = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode("utf-8"))


def sse_error(message: str) -> httpx.Response:
    payload = {"error": {"message": message, "code": "x"}}
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
        f"data: {json.dumps(payload)}\n\n".encode("utf-8")
    ))


def make_client(
    handler: Any, routes: tuple[Route, ...], *, config: dict[str, Any] | None = None, **client_kwargs: Any
) -> ModelClient:
    """链模式的客户端：``config`` 覆盖 ``Config` 字段，其余关键字直接给 ``ModelClient``（冷却/闸门）。"""
    kwargs: dict[str, Any] = {"base_url": "", "model": "", "timeout_s": 5, "retry": {"maxRetries": 0}, "routes": routes}
    kwargs.update(config or {})
    # 不读写真实的 ``~/.dsh/storages/adaptive_reader_cooldowns.json``（用例要能独立复跑；
    # 要测落盘的用例自己传 ``cooldowns=CooldownTable(persist_path=...)``）
    client_kwargs.setdefault("persist_cooldowns", False)
    return ModelClient(Config(**kwargs), transport=httpx.MockTransport(handler), **client_kwargs)


def request_once(client: ModelClient, content: str = "讲一句话") -> LlmResult:
    return asyncio.run(client.arequest_once(LlmRequest(tool="test", messages=({"role": "user", "content": content},))))


@pytest.fixture()
def keys(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """链上每一跳的密钥放进环境变量（不碰真实凭据文件）。"""
    monkeypatch.setenv("CMD_API_KEY", FAKE_KEY_A)
    monkeypatch.setenv("OPENCODE_API_KEY", FAKE_KEY_B)
    return {"CMD_API_KEY": FAKE_KEY_A, "OPENCODE_API_KEY": FAKE_KEY_B}


# ------------------------------------------------------------------ 配置解析 --
def test_parse_routes_requires_base_url_and_key_name() -> None:
    routes = parse_routes(
        [
            {"provider": "a", "model": "m", "base_url": "https://a.invalid/v1", "api_key_env": "A_KEY"},
            {"provider": "b", "model": "m", "_note": "没写 base_url：这一项不算数（注释键照旧忽略）"},
        ]
    )
    assert [item.provider for item in routes] == ["a"]  # 没有直连地址的那一项不算数
    assert routes[0].label == "a/m"


def test_parse_routes_rejects_unknown_keys_secret_and_duplicates() -> None:
    base = {"provider": "a", "model": "m", "base_url": "https://a.invalid/v1", "api_key_env": "A_KEY"}
    with pytest.raises(InputError, match="未知键"):
        parse_routes([{**base, "api_key": "sk-should-not-be-here"}])
    with pytest.raises(InputError, match="api_key_env"):
        parse_routes([{"provider": "a", "model": "m", "base_url": "https://a.invalid/v1"}])
    with pytest.raises(InputError, match="出现了多次"):
        parse_routes([base, {**base, "model": "m2"}])


def test_default_routes_yaml_is_usable_and_has_no_secret() -> None:
    routes = parse_routes(__import__("yaml").safe_load(DEFAULT_ROUTES_YAML))
    assert [item.provider for item in routes] == ["commandcode", "opencode-go", "ju-liu", "deepseek-official"]
    assert all(item.api_key_env for item in routes)
    # 两家写了额度接口（排序靠现查），另两家按量（永远最后）；**没有任何手填的重置时刻**
    assert [item.quota for item in routes] == ["commandcode", "opencode-go", None, None]
    assert all(item.reset_at_override_ms is None for item in routes)
    assert DEFAULT_ROUTES_YAML.count("sk-") == 0


def test_resolve_api_key_prefers_env_then_dsh_refs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    creds = tmp_path / ".credentials.yaml"
    creds.write_text("version: 1\nrefs:\n  FILE_KEY: from-file\n", encoding="utf-8")
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    target = route("a", "m", "FILE_KEY")
    assert resolve_api_key(target, env={"FILE_KEY": "from-env"}) == ("from-env", "env:FILE_KEY")
    assert resolve_api_key(target, env={}) == ("from-file", "file:FILE_KEY")
    assert resolve_api_key(route("a", "m", "MISSING_KEY"), env={}) == ("", None)


def test_load_routes_file_accepts_list_or_routes_key(tmp_path: Path) -> None:
    path = tmp_path / "routes.yaml"
    path.write_text(
        "- { provider: a, model: m, base_url: 'https://a.invalid/v1', api_key_env: A_KEY }\n",
        encoding="utf-8",
    )
    assert [item.provider for item in load_routes_file(path)] == ["a"]


# -------------------------------------------------------------------- 换跳 --
def test_falls_back_to_next_route_and_returns_its_answer(keys: dict[str, str]) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "url": str(request.url),
                "auth": request.headers.get("authorization"),
                "model": json.loads(request.content)["model"],
            }
        )
        if "a.invalid" in str(request.url):
            return httpx.Response(401, json={"error": {"message": "invalid key"}})
        return sse_ok("换跳之后的回答")

    client = make_client(handler, (route("a", "m-a", "CMD_API_KEY"), route("b", "m-b", "OPENCODE_API_KEY")))
    result = request_once(client)
    assert result.ok and result.text == "换跳之后的回答"
    assert [item["url"] for item in seen] == [
        "https://a.invalid/v1/chat/completions",
        "https://b.invalid/v1/chat/completions",
    ]
    assert seen[0]["auth"] == f"Bearer {FAKE_KEY_A}" and seen[1]["auth"] == f"Bearer {FAKE_KEY_B}"
    assert [item["model"] for item in seen] == ["m-a", "m-b"]
    assert client.route_outcomes()[0].provider == "a"


def test_success_on_first_route_never_touches_the_rest(keys: dict[str, str]) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return sse_ok("ok")

    client = make_client(handler, (route("a", "m-a", "CMD_API_KEY"), route("b", "m-b", "OPENCODE_API_KEY")))
    assert request_once(client).ok
    assert len(calls) == 1


def test_quota_failure_cools_the_provider_and_skips_it_next_time(keys: dict[str, str]) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "a.invalid" in str(request.url):
            # 余额级额度耗尽（= QUOTA，插件「真打满 ⇒ 补查确认 ⇒ 冷到重置」那条路）
            return httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})
        return sse_ok("ok")

    client = make_client(handler, (route("a", "m-a", "CMD_API_KEY"), route("b", "m-b", "OPENCODE_API_KEY")))
    assert request_once(client).ok
    assert calls == ["https://a.invalid/v1/chat/completions", "https://b.invalid/v1/chat/completions"]

    calls.clear()
    second = request_once(client)
    assert second.ok
    # 第二遍直接走 b：a 还在冷却里，且这次**一个请求都没发给它**
    assert calls == ["https://b.invalid/v1/chat/completions"]
    assert client._cooldowns.remaining_ms("a") > 0
    # 第二遍的账落在返回的结果上（``/api/health`` 与排查读的就是它）：a 被跳过、一个请求都没发
    assert [(item.provider, item.reason) for item in second.skipped] == [("a", "QUOTA")]


def test_all_cooling_falls_back_to_the_chain_instead_of_failing(keys: dict[str, str]) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return sse_ok("ok")

    # 先让两家真冷上，再把时钟拨到「冷却刚结束」：auto 过滤器把两跳都滤掉 ⇒ 一跳不剩，
    # 兜底是「忽略冷却」照链硬走（要钉的就是这条兜底，而不是「还在冷却时跳过」——那是下一个用例）。
    clock = {"now": 1_000_000.0}
    table = CooldownTable(clock=lambda: clock["now"])
    for provider in ("a", "b"):
        table.mark(provider, 60_000, "QUOTA")
    clock["now"] = 1_060_500.0
    assert table.active() == {}  # 冷却已到期
    client = make_client(
        handler,
        (route("a", "m-a", "CMD_API_KEY"), route("b", "m-b", "OPENCODE_API_KEY")),
        cooldowns=table,
    )
    assert request_once(client).ok
    assert calls == ["https://a.invalid/v1/chat/completions"]  # 全冷 ⇒ 忽略冷却，照链硬走


def test_in_cooldown_provider_is_skipped_entirely(keys: dict[str, str]) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return sse_ok("ok")

    table = CooldownTable()
    table.mark("a", 60_000, "AUTH")
    client = make_client(
        handler,
        (route("a", "m-a", "CMD_API_KEY"), route("b", "m-b", "OPENCODE_API_KEY")),
        cooldowns=table,
    )
    assert request_once(client).ok
    assert calls == ["https://b.invalid/v1/chat/completions"]


def test_cooldown_survives_a_restart_via_the_persisted_medium(keys: dict[str, str], tmp_path: Path) -> None:
    """冷却落盘：重启（新客户端 + 新冷却表）后第一条请求就绕开已打满那家，不再白撞一次。

    介质形状照插件 0.11：只记**剩余毫秒**与理由（单调时钟重启归零，绝对时刻没有跨进程意义）。
    """
    path = tmp_path / "storages" / "cooldowns.json"

    def handler(request: httpx.Request) -> httpx.Response:
        if "a.invalid" in str(request.url):
            return httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})
        return sse_ok("ok")

    routes = (route("a", "m-a", "CMD_API_KEY"), route("b", "m-b", "OPENCODE_API_KEY"))
    first = make_client(handler, routes, cooldowns=CooldownTable(persist_path=path))
    assert request_once(first).ok
    assert first.cooldowns.remaining_ms("a") > 0

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["cooldowns"]["a"]["reason"] == "QUOTA"
    # 链上没有额度接口（这两跳没写 quota）⇒ 补查确认不了 ⇒ 只留 5 分钟短闸门（插件 0.11 的第二步）
    assert 290_000 < payload["cooldowns"]["a"]["remaining_ms"] <= 300_000

    calls: list[str] = []

    def restarted(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return sse_ok("ok")

    second = make_client(restarted, routes, cooldowns=CooldownTable(persist_path=path))
    result = request_once(second)
    assert result.ok
    assert calls == ["https://b.invalid/v1/chat/completions"], "重启后第一条请求就该绕开 a"
    assert [(item.provider, item.reason) for item in result.skipped] == [("a", "QUOTA")]


def test_corrupt_cooldown_file_is_ignored_and_does_not_break_routing(keys: dict[str, str], tmp_path: Path) -> None:
    """介质坏了 / 读不到：只记 persist_error，**照常按链走**（fail-open，不因为读不出冷却就报错）。"""
    path = tmp_path / "cooldowns.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return sse_ok("ok")

    client = make_client(
        handler,
        (route("a", "m-a", "CMD_API_KEY"), route("b", "m-b", "OPENCODE_API_KEY")),
        cooldowns=CooldownTable(persist_path=path),
    )
    assert request_once(client).ok
    assert calls == ["https://a.invalid/v1/chat/completions"]
    assert client.cooldowns.persist_error is not None


def test_window_quota_switches_without_retry(keys: dict[str, str]) -> None:
    """窗口额度耗尽（如 Command Code 的「weekly usage limit」）：链上**不原地重试**、直接换下一跳。

    「重试」是 `ar.llm.acall` 那一层的账：这里只用 arequest_once（换跳本体），断言整次调用只发
    2 个请求（a 一次 + b 一次）——若 a 的失败落到冷却里，第二次调用会绕开它；这里只钉「一次就切」。
    冷却与否按错误码：窗口类文案先被余额判据认领时归 QUOTA（按额度冷却），未被认领的窗口级才进
    QUOTA_RATE_WINDOW（不冷却）。分类本身见 tests/test_model.py。
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "a.invalid" in str(request.url):
            return httpx.Response(429, json={"error": {"message": "You've reached your weekly usage limit"}})
        return sse_ok("ok")

    client = make_client(
        handler,
        (route("a", "m-a", "CMD_API_KEY"), route("b", "m-b", "OPENCODE_API_KEY")),
        config={"retry": {"maxRetries": 1}},
    )
    result = request_once(client)
    assert result.ok and result.text == "ok"
    assert calls == ["https://a.invalid/v1/chat/completions", "https://b.invalid/v1/chat/completions"]


def test_rate_limit_gates_that_route_for_the_rest_of_the_call(keys: dict[str, str]) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "a.invalid" in str(request.url):
            return httpx.Response(429, json={"error": {"message": "rate limit exceeded"}})
        return sse_ok("ok")

    client = make_client(
        handler,
        (route("a", "m-a", "CMD_API_KEY"), route("b", "m-b", "OPENCODE_API_KEY")),
        gate_ms=60_000,
    )
    assert request_once(client).ok
    assert calls == ["https://a.invalid/v1/chat/completions", "https://b.invalid/v1/chat/completions"]
    # 瞬时限速不冷却（这是 429 但报文里没有窗口语义 ⇒ RATE_LIMIT）：a 下一次还能再试
    assert client._cooldowns.remaining_ms("a") == 0


def test_insufficient_credits_400_is_quota_not_bad_request(keys: dict[str, str]) -> None:
    """真机实测：commandcode 余额耗尽回 400 invalid_request_error + insufficient credits。

    只看状态码会判成 INVALID_REQUEST ⇒ 不冷却 ⇒ 每条请求都先去白撞一次。这里钉死「按文案判额度」，
    并且余额类归 QUOTA（真打满：补查确认后冷到重置），与「窗口额度耗尽」的 QUOTA_RATE_WINDOW 分开。
    窗口类错误走哪条路见 ``test_rate_limit_gates_that_route_for_the_rest_of_the_call`` 旁边的用例。
    """
    body = {
        "error": {
            "message": "You have insufficient credits to make this request. Please purchase more credits",
            "type": "invalid_request_error",
            "code": "BAD_REQUEST",
        }
    }
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "a.invalid" in str(request.url):
            return httpx.Response(400, json=body)
        return sse_ok("ok")

    client = make_client(handler, (route("a", "m-a", "CMD_API_KEY"), route("b", "m-b", "OPENCODE_API_KEY")))
    assert request_once(client).ok
    # 「insufficient credits」是**余额**打满：归 QUOTA（要补查确认、要冷却），不是参数错误
    assert client.route_outcomes()[0].code == "QUOTA"
    assert client._cooldowns.remaining_ms("a") > 0

    calls.clear()
    second = request_once(client)
    assert second.ok
    # 第二遍直接走 b（a 在冷却里，这次一个请求都没发给它）
    assert calls == ["https://b.invalid/v1/chat/completions"]
    assert [(item.provider, item.reason) for item in second.skipped] == [("a", "QUOTA")]


def test_reasoning_effort_is_sent_only_when_this_route_declares_it(keys: dict[str, str]) -> None:
    """推理档位按**这一跳**的表走（插件 0.10 的 route 级 `reasoningEfforts`）：

    - 不按这一跳声明 ⇒ 请求体里**不出现** reasoning_effort（单源模式也不受影响）；
    - 声明了默认档 ⇒ 照声明写；
    - 声明成 null ⇒ 同样不写该键（OpenAI 风格里「写了 null」与「不写」不是一回事）。
    """
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return sse_ok("ok")

    plain = Route("a", "m-a", "https://a.invalid/v1", "CMD_API_KEY")
    declared = Route("b", "m-b", "https://b.invalid/v1", "OPENCODE_API_KEY", reasoning_effort="high")
    nulled = Route("c", "m-c", "https://c.invalid/v1", "CMD_API_KEY", reasoning_effort=None)
    for target in (plain, declared, nulled):
        client = make_client(handler, (target,))
        payloads.clear()
        assert request_once(client).ok
        assert ("reasoning_effort" in payloads[0]) == (target is declared), target.label
        if target is declared:
            assert payloads[0]["reasoning_effort"] == "high"


def test_reasoning_effort_table_ignores_unknown_names(keys: dict[str, str]) -> None:
    """档位表认不出调用方给的名字 ⇒ **整键不写**（绝不把陌生档位硬塞给上游）。"""
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return sse_ok("ok")

    target = Route("a", "m-a", "https://a.invalid/v1", "CMD_API_KEY")
    assert target.reasoning_value("max") == (False, None)
    assert target.reasoning_value() == (False, None)
    table = Route("a", "m-a", "https://a.invalid/v1", "CMD_API_KEY", reasoning_efforts=(("max", "max"), ("off", None)))
    assert table.reasoning_value("max") == (True, "max")
    assert table.reasoning_value("off") == (True, None)
    client = make_client(handler, (table,))
    assert request_once(client).ok
    assert "reasoning_effort" not in payloads[0]


def test_route_headers_are_sent(keys: dict[str, str]) -> None:
    """opencode-go 不带 x-opencode-session 会回 400 MissingSessionID：链上的自定义头必须真的发出去。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return sse_ok("ok")

    custom = Route(
        provider="a",
        model="m-a",
        base_url="https://a.invalid/v1",
        api_key_env="CMD_API_KEY",
        headers=(("x-opencode-session", "session-123"),),
    )
    client = make_client(handler, (custom,))
    assert request_once(client).ok
    assert seen.get("x-opencode-session") == "session-123"


def test_history_reasoning_is_stripped_before_switching(keys: dict[str, str]) -> None:
    payloads: list[list[dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content)["messages"])
        if "a.invalid" in str(request.url):
            return httpx.Response(500, text="boom")
        return sse_ok("ok")

    client = make_client(handler, (route("a", "m-a", "CMD_API_KEY"), route("b", "m-b", "OPENCODE_API_KEY")))
    messages = (
        {"role": "assistant", "content": "上一条回答", "reasoning_content": "别的模型的思考"},
        {"role": "user", "content": "继续"},
    )
    assert asyncio.run(client.arequest_once(LlmRequest(tool="test", messages=messages))).ok
    assert payloads[0][0]["reasoning_content"] == "别的模型的思考"  # 第一跳原样带上
    assert "reasoning_content" not in payloads[1][0]  # 换跳前摘掉


def test_all_routes_failing_names_every_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMD_API_KEY", FAKE_KEY_A)
    monkeypatch.setenv("OPENCODE_API_KEY", FAKE_KEY_B)

    def handler(request: httpx.Request) -> httpx.Response:
        if "a.invalid" in str(request.url):
            return httpx.Response(401, json={"error": {"message": "nope"}})
        return httpx.Response(500, text="down")

    client = make_client(handler, (route("a", "m-a", "CMD_API_KEY"), route("b", "m-b", "OPENCODE_API_KEY")))
    result = request_once(client)
    assert not result.ok
    assert result.failure is not None
    message = result.failure.message
    # 逐跳一行：序号 + provider/model + 错误码 + 原因 + 耗时/次数（插件「全部失败」那块的同款信息量）
    assert message.startswith("回退链全部失败（2 跳）："), message
    assert "1) a/m-a → AUTH：" in message, message
    assert "2) b/m-b → SERVER：" in message, message
    assert message.count("已试 1 次") == 2, message
    assert "ms" in message
    # 稳定的整链码（插件 AUTO_ROUTES_EXHAUSTED 同值）：不把末跳的码当成整条链的结论
    assert result.failure.code == "AUTO_ROUTES_EXHAUSTED"


def test_chain_retry_is_decided_per_hop_not_by_the_aggregate_code(keys: dict[str, str]) -> None:
    """整链失败码稳定为 ``AUTO_ROUTES_EXHAUSTED``，但**要不要再来一轮**仍看逐跳：

    瞬时类（``SERVER``）⇒ 外层重试；确定性类（``AUTH`` / 额度 / 没密钥）⇒ 一次就停。
    这一条钉住"稳定码没有把重试能力一起吞掉"（否则网络抖一下整批结果就废了）。
    """
    for status, expected in ((500, 2), (401, 1)):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(status, text="boom")

        client = make_client(
            handler, (route("a", "m-a", "CMD_API_KEY"),), config={"retry": {"maxRetries": 1}}
        )
        with pytest.raises(ModelCallFailed) as excinfo:
            client.chat([{"role": "user", "content": "hi"}])
        assert f"已试 {expected} 次" in str(excinfo.value), (status, str(excinfo.value))
        assert len(calls) == expected


def test_repeated_failure_of_one_hop_stays_one_line_with_a_tries_count(keys: dict[str, str]) -> None:
    """同一跳失败两次：聚合报错里**只占一行**、次数累加（不是两行同名）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    client = make_client(handler, (route("a", "m-a", "CMD_API_KEY"),))
    failure = LlmFailure(code="SERVER", message="HTTP 500：down")
    target = route("a", "m-a", "CMD_API_KEY")
    client._note_failure(target, failure)
    client._note_failure(target, failure)
    message = client._chain_message("末条")
    assert message.count("1) a/m-a → SERVER：") == 1, message
    assert "已试 2 次" in message, message


def test_missing_key_route_is_skipped_without_a_request(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DSH_HOME", str(tmp_path))  # 没有凭据文件
    monkeypatch.delenv("CMD_API_KEY", raising=False)
    monkeypatch.setenv("OPENCODE_API_KEY", FAKE_KEY_B)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return sse_ok("ok")

    client = make_client(handler, (route("a", "m-a", "CMD_API_KEY"), route("b", "m-b", "OPENCODE_API_KEY")))
    assert request_once(client).ok
    assert calls == ["https://b.invalid/v1/chat/completions"]
    assert client.route_outcomes()[0].code == "NOT_CONFIGURED"


def test_secret_never_appears_in_failure_text(keys: dict[str, str]) -> None:
    """上游把密钥原样回显时，报错文案里也不能有它。"""

    def handler(request: httpx.Request) -> httpx.Response:
        echoed = request.headers.get("authorization", "").removeprefix("Bearer ")
        return httpx.Response(401, text=f"invalid token {echoed}")

    client = make_client(handler, (route("a", "m-a", "CMD_API_KEY"),))
    result = request_once(client)
    assert not result.ok and result.failure is not None
    assert FAKE_KEY_A not in result.failure.message
    assert "REDACTED" in result.failure.message


# ------------------------------------------------------------------ 自动排序 --
def quota_route(provider: str, kind: str = "commandcode") -> Route:
    """配了额度接口的一跳（排序时会去探 ``report``，探不到就落「未知」档）。"""
    return Route(
        provider=provider,
        model="m",
        base_url=f"https://{provider}.invalid/v1",
        api_key_env="CMD_API_KEY",
        quota=kind,
        quota_base=f"https://{provider}.invalid",
    )


def billed_route(provider: str) -> Route:
    return Route(provider=provider, model="m", base_url=f"https://{provider}.invalid/v1", api_key_env="CMD_API_KEY")


def report_with(**resets: float) -> QuotaReport:
    """造一份「已经查过」的额度快照：``reset_of(p)`` 给 ``resets[p]``。"""
    report = QuotaReport()
    for provider, reset in resets.items():
        report.sources[provider] = QuotaSource(provider=provider, ok=True, monthly_reset_ms=reset)
    return report


def test_order_sorts_by_monthly_reset_then_unknown_then_billed() -> None:
    """插件 auto 的三桶：``重置早的 → 额度查不到的 → 按量兜底``，桶内保持配置顺序。"""
    routes = (quota_route("late"), billed_route("pay"), quota_route("unknown"), quota_route("early"))
    ordered = order_routes(routes, report_with(late=5_000, early=1_000))
    assert [item.route.provider for item in ordered] == ["early", "late", "unknown", "pay"]
    assert [item.reason for item in ordered] == [
        "monthly-reset-asc",
        "monthly-reset-asc",
        "subscription-reset-unknown",
        "unknown",
    ]


def test_order_marks_cooling_without_dropping_or_reshuffling() -> None:
    ordered = order_routes((quota_route("a"), quota_route("b")), QuotaReport(), cooling={"a"})
    assert [item.route.provider for item in ordered] == ["a", "b"]
    assert [item.cooling for item in ordered] == [True, False]


def test_manual_reset_override_is_used_when_probe_cannot_answer() -> None:
    """探测查不到时，配置里的 ``reset_at`` 才是逃生口（正常情况下一个都不用填）。"""
    routes = (Route("a", "m", "https://a.invalid/v1", "CMD_API_KEY", quota="commandcode", reset_at_override_ms=2_000),)
    ordered = order_routes(routes, QuotaReport())
    assert ordered[0].reason == "monthly-reset-asc" and ordered[0].reset_at_ms == 2_000


def test_parse_reset_at_requires_a_timezone() -> None:
    assert parse_reset_at(1_700_000_000_000) == 1_700_000_000_000
    assert parse_reset_at("2026-11-01T00:00:00+08:00") == 1_793_462_400_000
    with pytest.raises(InputError, match="缺少时区"):
        parse_reset_at("2026-11-01T00:00:00")


def test_auto_mode_skips_cooling_route_and_manual_mode_does_not(monkeypatch: Any) -> None:
    """同一条链、同一份冷却表：``auto`` 跳过冷的家，``manual`` 照旧按配置顺序硬撞。"""
    monkeypatch.setenv("CMD_API_KEY", FAKE_KEY_A)
    routes = (quota_route("a"), quota_route("b"))
    for mode, expected in (("auto", ["b"]), ("manual", ["a", "b"])):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if mode == "manual" and "a.invalid" in str(request.url):
                return httpx.Response(500, text="boom")  # 手动模式第一跳失败 ⇒ 继续撞第二跳
            return sse_ok("ok")

        table = CooldownTable()
        table.mark("a", 60_000, "QUOTA")
        if mode == "manual":
            # 这条用例钉的是「auto 跳过 / manual 硬撞」这条分支；manual 下**还会**再过一道「跳过冷却中的家」
            # （那是冷却本身的语义，单独有用例）⇒ 这里把这条冷却作废掉，只留「按配置顺序」这一个变量
            table.clear()
            table.mark("a", -1, "QUOTA")
        client = make_client(handler, routes, config={"ordering": mode}, cooldowns=table)
        assert request_once(client).ok
        # 只比推理请求：这两条路由配了额度接口，进链前的补查也会落到同一个 handler 上
        assert [url for url in calls if url.endswith("/chat/completions")] == [
            f"https://{provider}.invalid/v1/chat/completions" for provider in expected
        ], mode


def test_quota_cooldown_runs_until_the_probed_reset(monkeypatch: Any) -> None:
    """额度打满就冷到**探测到的**重置时刻（额度不会提前回来），不需要任何人填时间。"""
    monkeypatch.setenv("CMD_API_KEY", FAKE_KEY_A)
    reset_at = int(time.time() * 1000) + 3_600_000  # 一小时后重置
    report = report_with()
    report.sources["a"] = QuotaSource(
        provider="a", ok=True, monthly_reset_ms=reset_at, exhausted_windows=("credits",), exhausted_until_ms=reset_at
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # 余额级（QUOTA）才走「冷到探测到的重置时刻」；窗口级错误不冷却，见下一个用例
        return httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})

    # 把查询闸门设成"永远刚查过"：这份 report 是手工造的，不许刷新真的去问上游覆盖掉它
    report.ttl_ms = float("inf")
    client = make_client(handler, (quota_route("a"),), quota_report=report)
    request_once(client)
    remaining = client.cooldowns.remaining_ms("a") / 1000
    assert 3_500 < remaining <= 3_600, remaining


def test_auth_failure_does_not_cool_until_reset(monkeypatch: Any) -> None:
    """鉴权错跟额度无关：拿月度重置时刻去冷它，会把「key 填错了」拖到一个月后。"""
    monkeypatch.setenv("CMD_API_KEY", FAKE_KEY_A)
    reset_at = int(time.time() * 1000) + 30 * 24 * 3_600_000
    report = report_with(a=reset_at)
    report.ttl_ms = float("inf")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    client = make_client(handler, (quota_route("a"),), quota_report=report)
    request_once(client)
    assert client.cooldowns.remaining_ms("a") / 1000 <= 600


def test_window_quota_cools_until_that_window_resets(keys: dict[str, str]) -> None:
    """窗口额度耗尽（``QUOTA_RATE_WINDOW``）：不原地重试，**并且**冷到该窗口的重置时刻。

    插件 0.9.1 只规定"不原地重试"，冷却照旧（0.8.0 起就按档位重置时刻冷）。AR 一个 provider
    只有一条 route，"只停那一个通道"在这里等于"什么都停不住"——不冷就是每次调用都先白撞一次。
    """
    reset = int(time.time() * 1000) + 3 * 24 * 3_600_000
    credits = {
        "credits": {"belowThreshold": False, "monthlyCredits": 5, "purchasedCredits": 0, "freeCredits": 0},
        "windowLimits": {"weekly": {"exceeded": True, "resetAt": reset}},
    }
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/alpha/billing/credits"):
            return httpx.Response(200, json=credits)
        if "a.invalid" in str(request.url):
            # 窗口语义但**不含**余额文案（``insufficient credits`` 之类）⇒ 归 QUOTA_RATE_WINDOW
            return httpx.Response(429, json={"error": {"message": "Your weekly credits are used up, resets in 3 days"}})
        return sse_ok("ok")

    client = make_client(handler, (quota_route("a"), route("b", "m-b", "OPENCODE_API_KEY")))
    assert request_once(client).ok
    assert client.route_outcomes()[0].code == "QUOTA_RATE_WINDOW"
    assert client.cooldowns.remaining_ms("a") > 2 * 24 * 3_600_000  # 冷到周窗重置，不是 5 分钟

    calls.clear()
    second = request_once(client)
    assert second.ok
    assert [(item.provider, item.reason) for item in second.skipped] == [("a", "QUOTA_RATE_WINDOW")]
    assert not any("a.invalid" in url for url in calls), "冷却中的 a 一个请求都不该收到"


def test_quota_failure_reprobes_that_provider_and_uses_the_confirmed_reset(keys: dict[str, str]) -> None:
    """额度类失败当下**只补查这一家**（绕缓存）：确认有档位耗尽就把 5 分钟短闸门换成重置时刻。

    链开始前那一查是"健康的"（额度还没打满），失败后的补查才是"确认"——这正是插件 0.11.0 的
    第二步：不能用进链前的旧快照去猜冷却终点。
    """
    reset = int(time.time() * 1000) + 3_600_000
    state = {"exhausted": False, "credits_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/alpha/billing/credits"):
            state["credits_calls"] += 1
            weekly = {"used": 0, "cap": 35, "exceeded": state["exhausted"], "resetAt": reset}
            return httpx.Response(
                200,
                json={
                    "credits": {"belowThreshold": False, "monthlyCredits": 5, "purchasedCredits": 0, "freeCredits": 0},
                    "windowLimits": {"weekly": weekly},
                },
            )
        if "a.invalid" in str(request.url):
            state["exhausted"] = True  # 这一下之后补查就能"确认"
            return httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})
        return sse_ok("ok")

    client = make_client(handler, (quota_route("a"), route("b", "m-b", "OPENCODE_API_KEY")))
    assert request_once(client).ok
    assert state["credits_calls"] == 2, "进链前一次 + 失败后确认一次（只查这一家）"
    remaining = client.cooldowns.remaining_ms("a") / 1000
    assert 3_500 < remaining <= 3_600, remaining


def test_recovered_quota_thaws_the_cooldown_on_the_next_probe(keys: dict[str, str]) -> None:
    """额度恢复（充值 / 换套餐）⇒ 周期补查发现「查到且没有耗尽档」就解冻（插件 thawIfHealthy）。

    没有这条，``CREDITS_FLOOR_USD`` 那种「余额见底就冷到周期末」的判定会在充值之后继续拦一周。
    """
    reset = int(time.time() * 1000) + 7 * 24 * 3_600_000
    state = {"total": 0.084}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/alpha/billing/credits"):
            return httpx.Response(
                200,
                json={
                    "credits": {
                        "belowThreshold": False,
                        "monthlyCredits": state["total"],
                        "purchasedCredits": 0,
                        "freeCredits": 0,
                    },
                    "windowLimits": {},
                },
            )
        if request.url.path.endswith("/alpha/billing/subscriptions"):
            return httpx.Response(
                200,
                json={"data": {"planId": "individual-goat", "currentPeriodEnd": iso_ms(reset)}},
            )
        if "a.invalid" in str(request.url):
            if state["total"] >= 0.2:
                return sse_ok("充值之后就能用了")
            return httpx.Response(400, json={"error": {"message": "You have insufficient credits to make this request"}})
        return sse_ok("ok")

    client = make_client(handler, (quota_route("a"), route("b", "m-b", "OPENCODE_API_KEY")))
    assert request_once(client).ok
    assert client.cooldowns.remaining_ms("a") > 6 * 24 * 3_600_000  # 冷到周期末（余额 0.084 < 0.2）

    state["total"] = 5.0  # 充值
    client.quota_report.forget()  # 等价于周期补查到期（有效期到点）
    result = request_once(client)
    assert result.ok and result.text == "充值之后就能用了"
    assert client.route_outcomes() == ()  # 第一跳就是 a，没有跳过、没有失败
    assert client.cooldowns.remaining_ms("a") == 0


def test_healthy_probe_does_not_thaw_a_missing_credential_cooldown(keys: dict[str, str]) -> None:
    """自愈只解额度类冷却：``NOT_CONFIGURED``（没密钥）不是额度问题，别被一条健康的额度快照放行。"""
    report = QuotaReport()
    report.sources["a"] = QuotaSource(provider="a", ok=True)
    table = CooldownTable()
    table.mark("a", 60_000, "NOT_CONFIGURED")
    client = make_client(lambda request: sse_ok("ok"), (quota_route("a"),), cooldowns=table, quota_report=report)
    client._thaw_healthy()
    assert client.cooldowns.remaining_ms("a") > 0


def test_unconfirmed_quota_failure_keeps_only_the_short_gate(keys: dict[str, str]) -> None:
    """补查确认不了（上游 500 / 断网）⇒ 只留 5 分钟短闸门，不拿猜出来的终点冷一家。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/alpha/billing/credits"):
            return httpx.Response(500, text="down")
        if "a.invalid" in str(request.url):
            return httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})
        return sse_ok("ok")

    client = make_client(handler, (quota_route("a"), route("b", "m-b", "OPENCODE_API_KEY")))
    assert request_once(client).ok
    remaining = client.cooldowns.remaining_ms("a")
    assert 240_000 < remaining <= 300_000, remaining  # 5 分钟闸门（不是 10 分钟、也不是"冷到重置"）


# ------------------------------------------------------- 单源模式行为不变 --
def test_single_source_mode_still_uses_config_fields_and_one_trip() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return sse_ok("单源")

    config = Config(
        base_url="https://single.invalid/v1",
        model="fake/model",
        api_key="sk-" + "s" * 30,
        retry={"maxRetries": 0},
    )
    client = ModelClient(config, transport=httpx.MockTransport(handler))
    assert request_once(client).text == "单源"
    assert calls == ["https://single.invalid/v1/chat/completions"]
    assert client.routes == () and client.route_outcomes() == ()
