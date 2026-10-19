"""选片讲解（``selection`` / ``selection_from`` / ``scope``）+ 进程内讲解缓存。

覆盖四块：

1. **作用域**：不带 ``selection`` 与旧版逐字段一致（``scope: paragraph``）；带 ``selection`` 时
   ``scope: selection``，且**发往模型的 messages 里确实带上了那段片段**、整段原文降级为上下文；
2. **规范化与截断**：与锚点同口径（去首尾空白 + 合并连续空白 + 全角空格转半角）；
   超 600 字符截断并**可见上报** ``selection_truncated``；
3. **缓存**：同键第二次调用不再碰模型且 ``cached: true``；换 kind / 换片段 / 换材料不串味；
   画像文件一变即失效；失败与降级一律不进缓存；LRU 容量与线程安全；
4. **向后兼容**：旧请求体（没有 selection）正常；三条既有降级路径（``paragraph_not_found`` /
   ``model_not_configured`` / ``model_error``）的原因枚举不变、旧键一个不少（只多请求侧的 ``scope``
   与逐次账 ``attempts``）；
   成功响应另追加 ``usage``（本次调用的原始 token 账，含 ``prompt_tokens_details.cached_tokens``）与
   ``attempts``（逐次尝试账，结构 = 产物里的 ``meta.attempts``；讲解不落盘，这是它唯一的出口）。

所有模型调用都注入假客户端（``fake_client``），不发真实网络请求。
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from ar.cache import DEFAULT_CAPACITY, ExplainCache, explain_cache_key
from ar.config import Config
from ar.explain import prepare_selection
from ar.ingest import ingest
from ar.llm import LlmFailure, LlmResult, RetryPolicy
from ar.model import ModelCallFailed
from ar.server import build_app

FAKE_KEY = "sk-" + "3" * 40


def make_client(
    dirs: SimpleNamespace,
    *,
    api_key: str = "",
    client_factory=None,
    explain_cache: ExplainCache | None = None,
) -> TestClient:
    config = Config(
        base_url="https://gateway.invalid/provider/v1",
        model="fake/model",
        api_key=api_key,
        api_key_origin="env:AR_API_KEY" if api_key else None,
    )
    app = build_app(
        data_dir=dirs.data,
        profiles_dir=dirs.profiles,
        web_dir=dirs.web,
        config=config,
        reader="local-default",
        client_factory=client_factory,
        explain_cache=explain_cache,
    )
    return TestClient(app)


@pytest.fixture
def ingested(dirs: SimpleNamespace, sample_md: Path) -> SimpleNamespace:
    """摄取样例材料：p004 = 带连续空白与全角空格的那一段，p005 / p006 是后两段。"""
    ingest(sample_md, "sample", dirs.data)
    return dirs


def explain_body(**overrides) -> dict:
    body = {"source_id": "sample", "paragraph": "p004", "kind": "explain"}
    body.update(overrides)
    return body


# ------------------------------------------------------------------ 作用域 --
def test_selection_absent_keeps_legacy_shape_and_paragraph_scope(
    ingested: SimpleNamespace, fake_client
) -> None:
    fake = fake_client(text="整段讲解。")
    client = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: fake)
    payload = client.post("/api/explain", json=explain_body()).json()

    assert payload["degraded"] is False
    assert payload["scope"] == "paragraph"
    # 成功响应 = 旧四键 + 追加的 scope / usage / attempts
    # （reason / cached / selection_truncated 都不该出现）。
    # usage（2026-10-08）与 attempts（2026-10-13）都是**追加**字段：原有键逐字不变，
    # 这里把键集写全（不是放宽，是加了两项）。
    assert set(payload) == {"text", "degraded", "model", "prompt_version", "scope", "usage", "attempts"}
    assert "cached" not in payload and "selection_truncated" not in payload
    assert [item["try"] for item in payload["attempts"]] == [1], "成功也是逐次账（这里只试了一次）"

    assert len(fake.calls) == 1
    sent = fake.calls[0][1]["content"]
    assert "这一段原文" in sent and "第一段：带" in sent
    assert "选中的片段" not in sent  # 没选片段就不该出现选片段式版式


def test_explain_success_response_exposes_usage_with_cached_tokens(
    ingested: SimpleNamespace, fake_client
) -> None:
    """``usage`` 是**追加**字段：原样透传这次调用的 token 账（含前缀缓存命中量）。

    讲解每次都要真金白银，命中量必须看得见；缓存命中时这次没调模型，``usage`` 是空对象
    （不把上一次的账再报一遍）。
    """

    class _UsageClient:
        configured = True

        def __init__(self, usage: dict) -> None:
            self.usage = usage
            self.calls: list[list[dict[str, str]]] = []

        async def arequest_once(self, request: Any) -> LlmResult:
            """统一调用层的唯一入口（``ar.explain`` 经工具层调它）。"""
            self.calls.append([dict(message) for message in request.messages])
            return LlmResult(ok=True, text="按画像讲解。", model="fake-model", usage=dict(self.usage))

    usage = {
        "prompt_tokens": 1200,
        "completion_tokens": 300,
        "total_tokens": 1500,
        "prompt_tokens_details": {"cached_tokens": 1024},
    }
    fake = _UsageClient(usage)
    client = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: fake)

    payload = client.post("/api/explain", json=explain_body()).json()
    assert payload["usage"] == usage, "usage 原样透传，不改名、不裁剪"
    assert payload["usage"]["prompt_tokens_details"]["cached_tokens"] == 1024
    # 旧键一个不少（usage 只是追加）
    assert {"text", "degraded", "model", "prompt_version", "scope"} <= set(payload)

    second = client.post("/api/explain", json=explain_body()).json()
    assert second["cached"] is True and second["usage"] == {}, "缓存命中没调模型：usage 不该凭空冒出来"
    assert len(fake.calls) == 1


def test_selection_scope_puts_fragment_in_messages_and_keeps_context(
    ingested: SimpleNamespace, fake_client
) -> None:
    fake = fake_client(text="只讲选中的那一小段。")
    client = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: fake)
    fragment = "连续空白与全角空格"
    payload = client.post("/api/explain", json=explain_body(selection=fragment)).json()

    assert payload["degraded"] is False
    assert payload["scope"] == "selection"
    assert payload["prompt_version"] == "explain-v2"

    sent = fake.calls[0][1]["content"]
    assert fragment in sent, "选中的片段必须真的进了提示词"
    assert "选中的片段" in sent and "讲解重点" in sent
    assert "第一段：带" in sent, "整段原文仍在（只作上下文）"
    assert "[p003]" in sent and "[p005]" in sent, "邻段上下文照旧"
    assert "[prev]" not in sent and "[next]" not in sent, "邻段行必须带**真实段 id**（工具层的 prev_id / next_id）"
    assert "来自改写稿" in sent
    # system 提示词也点明了「重点是选中的片段」
    assert "选中的片段" in fake.calls[0][0]["content"]
    assert fragment not in fake.calls[0][0]["content"]


def test_blank_or_non_string_selection_behaves_like_no_selection(
    ingested: SimpleNamespace, fake_client
) -> None:
    fake = fake_client(text="整段讲解。")
    client = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: fake)

    blank = client.post("/api/explain", json=explain_body(selection="  \u3000  ")).json()
    assert blank["scope"] == "paragraph"
    assert "选中的片段" not in fake.calls[0][1]["content"]

    # 非字符串是**用法错误**（400），不是静默忽略
    assert client.post("/api/explain", json=explain_body(selection=5)).status_code == 400


# ------------------------------------------------------- 规范化与 600 截断 --
@pytest.mark.parametrize(
    ("raw", "text", "truncated"),
    [
        (None, "", False),
        (123, "", False),
        ("", "", False),
        ("  \u3000 \t ", "", False),
        ("  带  连续\u3000空白 的  ", "带 连续 空白 的", False),
        ("甲" * 600, "甲" * 600, False),
        ("甲" * 601, "甲" * 600, True),
        ("甲" * 650, "甲" * 600, True),
    ],
)
def test_prepare_selection_normalizes_and_clips(raw, text: str, truncated: bool) -> None:
    prepared = prepare_selection(raw)
    assert prepared.text == text
    assert prepared.truncated is truncated
    assert prepared.scope == ("selection" if text else "paragraph")


def test_selection_is_normalized_like_anchors_and_shares_the_cache(
    ingested: SimpleNamespace, fake_client
) -> None:
    fake = fake_client(text="规范化后讲解。")
    client = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: fake)

    first = client.post(
        "/api/explain", json=explain_body(selection="  带  连续\u3000空白 的  ")
    ).json()
    assert first["scope"] == "selection"
    sent = fake.calls[0][1]["content"]
    assert "带 连续 空白 的" in sent
    # 送进提示词的是规范化后的片段：原样的多空白 + 全角空格版不会被透传
    assert "带  连续\u3000空白 的" not in sent

    # 只差空白写法的同一段片段 = 同一个缓存键 → 第二次不再调模型
    second = client.post("/api/explain", json=explain_body(selection="带 连续 空白 的")).json()
    assert second["cached"] is True and second["scope"] == "selection"
    assert len(fake.calls) == 1


def test_selection_over_600_chars_is_truncated_and_reported(
    ingested: SimpleNamespace, fake_client
) -> None:
    fake = fake_client(text="截断后讲解。")
    client = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: fake)
    payload = client.post("/api/explain", json=explain_body(selection="丙" * 650)).json()

    assert payload["selection_truncated"] is True  # 降级必须可见：不许静默截断
    assert payload["scope"] == "selection"
    sent = fake.calls[0][1]["content"]
    assert "丙" * 600 in sent and "丙" * 601 not in sent


def test_selection_truncation_is_reported_even_when_degraded(ingested: SimpleNamespace) -> None:
    """截断与作用域都是请求侧事实：模型不可用也照实上报（产出侧的字段仍然不出现）。"""
    client = make_client(ingested)  # 没有密钥 → model_not_configured
    payload = client.post("/api/explain", json=explain_body(selection="丁" * 700)).json()
    assert payload["degraded"] is True and payload["reason"] == "model_not_configured"
    assert payload["selection_truncated"] is True
    assert payload["scope"] == "selection"
    assert "cached" not in payload and "model" not in payload and "prompt_version" not in payload


# ------------------------------------------------------------ selection_from --
def test_explain_prompt_is_byte_identical_to_the_direct_builder(
    ingested: SimpleNamespace, fake_client
) -> None:
    """提示词**逐字不变**：经工具层拼出来的 messages 与业务侧直接拼的逐字相同。

    邻段行在模板里是 ``[段id] 正文``，而 §3.3 给工具层的只有 ``prev`` / ``next`` 两个字符串；
    所以工具层追加了可选入参 ``prev_id`` / ``next_id``（业务侧传真实段 id）——少了它们，
    提示词会退化成 ``[prev]`` / ``[next]``，前缀缓存与回归基线都会跟着变。
    """
    from ar.jsonio import read_json
    from ar.prompts import build_explain_messages

    fake = fake_client(text="讲解。")
    client = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: fake)
    client.post("/api/explain", json=explain_body(selection="连续空白"))  # p004，前后各一段

    paragraphs = read_json(ingested.data / "sample" / "source.meta.json")["paragraphs"]
    index = [p["id"] for p in paragraphs].index("p004")
    expected = build_explain_messages(
        kind="explain",
        paragraph_id="p004",
        paragraph_text=paragraphs[index]["text"],
        neighbors=[
            (paragraphs[index - 1]["id"], paragraphs[index - 1]["text"]),
            (paragraphs[index + 1]["id"], paragraphs[index + 1]["text"]),
        ],
        profile_hint=None,  # 空画像：None 与空串在模板里同一条分支
        selection="连续空白",
        selection_from="rewritten",
    )
    assert fake.calls[0] == expected, "迁移后提示词不再与业务侧直接拼的逐字相同"


def test_out_of_enum_kind_falls_back_softly_instead_of_400(
    ingested: SimpleNamespace, fake_client
) -> None:
    """枚举外的 ``kind``：既有行为是**软回落**到 ``explain``（HTTP 200），迁移后必须逐字不变。

    工具层的 ``kind`` 是**强校验**（JSON Schema 的 ``enum``），直接把它丢进工具会变成
    入参错误（400）——所以业务侧仍要先归一化；这条用例守住那一步不被删掉。
    """
    fake = fake_client(text="兜底讲解。")
    client = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: fake)

    response = client.post("/api/explain", json=explain_body(kind="frobnicate"))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["degraded"] is False and payload["text"] == "兜底讲解。"
    # 回落成 explain：提示词里写的是 explain 的讲法
    assert "把这个段落讲清楚" in fake.calls[0][1]["content"]

    # 非字符串仍是 400（HTTP 层先校验），绝不是 500
    bad = client.post("/api/explain", json=explain_body(paragraph="p005", kind=5))
    assert bad.status_code == 400, bad.text
    assert "kind" in bad.json()["error"]


def test_selection_from_labels_provenance_and_rejects_bad_values(
    ingested: SimpleNamespace, fake_client
) -> None:
    fake = fake_client(text="选片讲解。")
    client = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: fake)

    from_source = client.post(
        "/api/explain", json=explain_body(selection="第二段", selection_from="source")
    ).json()
    assert from_source["scope"] == "selection"
    assert "来自原文" in fake.calls[0][1]["content"]

    # 空串 = 没给这个字段 → 默认 rewritten（换一段，避免撞上缓存）
    defaulted = client.post(
        "/api/explain",
        json=explain_body(paragraph="p005", selection="第三段", selection_from="   "),
    )
    assert defaulted.status_code == 200
    assert "来自改写稿" in fake.calls[1][1]["content"]

    for bad in ("原文", "REWRITTEN", 5, ["source"], True):
        response = client.post(
            "/api/explain", json=explain_body(paragraph="p006", selection="第一段", selection_from=bad)
        )
        assert response.status_code == 400, f"selection_from={bad!r} 必须被拒"
        assert "selection_from" in response.json()["error"]


def test_selection_from_is_not_part_of_the_cache_key(ingested: SimpleNamespace, fake_client) -> None:
    """契约把缓存键冻结成 (source_id, paragraph, kind, selection, 画像指纹)：来源标注不进键。

    同一段文字分别按「改写稿 / 原文」请求，第二次直接命中缓存——这是契约选择的结果，
    这里把它固化成可见行为（片段文字本身不同时仍然各算各的键）。
    """
    fake = fake_client(text="同一段文字的讲解。")
    client = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: fake)

    first = client.post(
        "/api/explain", json=explain_body(selection="第二段", selection_from="rewritten")
    ).json()
    assert "cached" not in first
    second = client.post(
        "/api/explain", json=explain_body(selection="第二段", selection_from="source")
    ).json()
    assert second["cached"] is True
    assert len(fake.calls) == 1


# ------------------------------------------------------------------ 缓存 --
def test_cache_hit_skips_model_call_and_is_visible(ingested: SimpleNamespace, fake_client) -> None:
    fake = fake_client(text="缓存讲解。")
    client = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: fake)
    body = explain_body(selection="连续空白")

    first = client.post("/api/explain", json=body).json()
    assert "cached" not in first and len(fake.calls) == 1

    second = client.post("/api/explain", json=body).json()
    assert second["cached"] is True
    assert second["text"] == first["text"] == "缓存讲解。"
    assert second["scope"] == "selection" and second["prompt_version"] == "explain-v2"
    assert len(fake.calls) == 1, "命中缓存就不该再碰模型"


def test_cache_covers_whole_paragraph_scope_too(ingested: SimpleNamespace, fake_client) -> None:
    fake = fake_client(text="整段缓存讲解。")
    client = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: fake)
    client.post("/api/explain", json=explain_body()).json()
    second = client.post("/api/explain", json=explain_body()).json()
    assert second["cached"] is True and second["scope"] == "paragraph"
    assert len(fake.calls) == 1


def test_cache_keys_do_not_collide_across_kind_or_selection(
    ingested: SimpleNamespace, fake_client
) -> None:
    fake = fake_client(text="讲解。")
    client = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: fake)

    for body in (
        explain_body(kind="explain", selection="甲片段"),
        explain_body(kind="simpler", selection="甲片段"),
        explain_body(kind="explain", selection="乙片段"),
        explain_body(kind="explain", paragraph="p005", selection="甲片段"),
    ):
        payload = client.post("/api/explain", json=body).json()
        assert "cached" not in payload
    assert len(fake.calls) == 4


def test_cache_invalidated_when_profile_file_changes(ingested: SimpleNamespace, fake_client) -> None:
    fake = fake_client(text="按画像讲解。")
    client = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: fake)
    body = explain_body()

    client.post("/api/explain", json=body)
    assert len(fake.calls) == 1

    # ① 自评写回 = 画像文件从「不存在」变成「存在」→ 指纹变 → 缓存失效
    assert client.post("/api/profile/rating", json={"concept_id": "c01", "state": "known"}).status_code == 200
    after_rating = client.post("/api/explain", json=body).json()
    assert "cached" not in after_rating
    assert len(fake.calls) == 2

    # ② 指纹没再动 → 命中（证明确实是按指纹失效，不是「永远不命中」）
    assert client.post("/api/explain", json=body).json()["cached"] is True
    assert len(fake.calls) == 2

    # ③ 手改画像（内容长度变了）→ 再次失效
    profile_path = ingested.profiles / "local-default" / "profile.yaml"
    profile_path.write_text(
        "reader: local-default\nconcepts:\n  c01: {state: partial, confidence: 0.7}\n", encoding="utf-8"
    )
    after_edit = client.post("/api/explain", json=body).json()
    assert "cached" not in after_edit
    assert len(fake.calls) == 3


def test_cache_is_per_app_instance_not_global(ingested: SimpleNamespace, fake_client) -> None:
    """新 app = 新进程的空缓存（缓存不落盘、重启即空）。"""
    fake = fake_client(text="讲解。")
    body = explain_body()
    make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: fake).post("/api/explain", json=body)
    payload = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: fake).post(
        "/api/explain", json=body
    ).json()
    assert "cached" not in payload
    assert len(fake.calls) == 2


def test_injected_cache_instance_is_used(ingested: SimpleNamespace, fake_client) -> None:
    cache = ExplainCache(4)
    fake = fake_client(text="讲解。")
    client = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: fake, explain_cache=cache)
    client.post("/api/explain", json=explain_body(selection="片段"))
    assert len(cache) == 1
    client.post("/api/explain", json=explain_body(selection="片段"))
    assert len(cache) == 1 and len(fake.calls) == 1

    # 清掉缓存 → 下一次重新调模型
    cache.clear()
    client.post("/api/explain", json=explain_body(selection="片段"))
    assert len(fake.calls) == 2


def test_failures_and_degradations_are_never_cached(ingested: SimpleNamespace, fake_client) -> None:
    failing = fake_client(error=ModelCallFailed("模型调用失败（已试 2 次）：HTTP 503"))
    broken = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: failing)
    body = explain_body(selection="片段")

    for expected_calls in (1, 2):
        payload = broken.post("/api/explain", json=body).json()
        assert payload["reason"] == "model_error" and payload["degraded"] is True
        assert "cached" not in payload and "model" not in payload
        assert payload["scope"] == "selection", "scope 是请求侧事实，降级响应也照给"
        assert len(failing.calls) == expected_calls, "失败结果不许进缓存"

    empty = fake_client(text="   ")
    empty_client = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: empty)
    for expected_calls in (1, 2):
        payload = empty_client.post("/api/explain", json=body).json()
        assert payload["reason"] == "empty_reply"
        assert len(empty.calls) == expected_calls, "空回复也算失败，不许进缓存"

    unconfigured = make_client(ingested)  # 没有密钥：连请求都不该发
    payload = unconfigured.post("/api/explain", json=body).json()
    assert payload["reason"] == "model_not_configured" and "cached" not in payload


def test_degraded_explain_responses_keep_the_legacy_keys_and_reasons(
    ingested: SimpleNamespace, fake_client
) -> None:
    """三条既有降级路径：原因枚举不变、旧键一个不少；只多请求侧的 scope 与逐次账 attempts。"""
    legacy_keys = {"text", "degraded", "reason", "scope", "attempts"}
    missing_source = make_client(ingested).post(
        "/api/explain", json={"source_id": "nosuchdoc", "paragraph": "p001"}
    ).json()
    assert missing_source["reason"] == "source_not_found"
    assert set(missing_source) == legacy_keys
    assert missing_source["scope"] == "paragraph"
    assert missing_source["attempts"] == [], "材料不存在：一次请求都没发"

    not_configured = make_client(ingested).post("/api/explain", json=explain_body()).json()
    assert not_configured["reason"] == "model_not_configured"
    assert set(not_configured) == legacy_keys
    assert not_configured["attempts"] == [], "未配置：一次请求都没发"

    not_found = make_client(ingested).post("/api/explain", json=explain_body(paragraph="p404")).json()
    assert not_found["reason"] == "paragraph_not_found" and "p404" in not_found["text"]
    assert set(not_found) == legacy_keys
    assert not_found["attempts"] == [], "段落不存在：一次请求都没发"

    failing = fake_client(error=ModelCallFailed("模型调用失败（已试 2 次）：HTTP 503"))
    broken = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: failing)
    model_error = broken.post("/api/explain", json=explain_body()).json()
    assert model_error["reason"] == "model_error" and "503" in model_error["text"]
    assert set(model_error) == legacy_keys
    # 模型失败：账目留在响应里，看得见「试了几次、末次什么码」
    assert len(model_error["attempts"]) == 1 and model_error["attempts"][0]["code"] == "UNKNOWN"
    assert "已试 1 次，末次 UNKNOWN" in model_error["text"]
    assert FAKE_KEY not in model_error["text"], "失败文案不许带密钥"


def test_explain_attempts_record_every_retry_on_success_and_degradation(
    ingested: SimpleNamespace,
) -> None:
    """``attempts``（2026-10-13 追加字段）：首发失败 + 重试成功 → 两条账，成功响应也给。

    讲解**不落盘**，所以这是它的重试账唯一能被看见的地方；结构 = 产物里的 ``meta.attempts``。
    """

    class _FlakyOnceClient:
        """只实现统一调用层要的三个成员：第一次 503、第二次成功。"""

        configured = True

        def __init__(self) -> None:
            self.requests = 0
            self.messages: list[list[dict[str, str]]] = []

        def retry_policy(self) -> RetryPolicy:
            # 退避 0 秒：用例不该真的等 500 ms
            return RetryPolicy(max_retries=1, initial_delay_ms=0, max_delay_ms=0)

        async def arequest_once(self, request: Any) -> LlmResult:
            self.requests += 1
            self.messages.append([dict(message) for message in request.messages])
            if self.requests == 1:
                return LlmResult(
                    ok=False, failure=LlmFailure(code="SERVER", message="HTTP 503：网关抖动", status=503)
                )
            return LlmResult(ok=True, text="重试后的讲解。", model="fake-model", usage={"total_tokens": 3})

    flaky = _FlakyOnceClient()
    client = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: flaky)
    payload = client.post("/api/explain", json=explain_body()).json()

    assert payload["degraded"] is False and payload["text"] == "重试后的讲解。"
    assert flaky.requests == 2, "重试真的发了第二次请求（真实 HTTP 次数 = 2）"
    assert [item["try"] for item in payload["attempts"]] == [1, 2]
    assert set(payload["attempts"][0]) == {"tool", "try", "elapsed_ms", "code", "delay_ms", "retry_after_used"}
    assert payload["attempts"][0]["tool"] == "explain_paragraph"
    assert payload["attempts"][0]["code"] == "SERVER" and payload["attempts"][1]["code"] is None
    # 第二次尝试的提示词与第一次逐字相同（重试不带上一次的残留）
    assert flaky.messages[0] == flaky.messages[1]


def test_paragraph_not_found_is_not_cached_and_never_calls_the_model(
    ingested: SimpleNamespace, fake_client
) -> None:
    fake = fake_client(text="不该被用到。")
    client = make_client(ingested, api_key=FAKE_KEY, client_factory=lambda: fake)
    for _ in range(2):
        payload = client.post("/api/explain", json=explain_body(paragraph="p404")).json()
        assert payload["reason"] == "paragraph_not_found"
        assert "cached" not in payload
    assert fake.calls == []


# ------------------------------------------------------- 缓存本体（纯单测） --
def test_explain_cache_is_lru_with_contract_capacity() -> None:
    assert DEFAULT_CAPACITY == 128
    assert ExplainCache().capacity == 128

    cache = ExplainCache(2)
    cache.put("a", {"text": "A"})
    cache.put("b", {"text": "B"})
    cache.put("c", {"text": "C"})  # a 最久未用 → 被淘汰
    assert "a" not in cache and "b" in cache and "c" in cache
    assert len(cache) == 2

    assert cache.get("b") == {"text": "B"}  # 命中把 b 移到队尾
    cache.put("d", {"text": "D"})  # 这次淘汰 c
    assert "c" not in cache and "b" in cache and "d" in cache
    assert cache.get("a") is None

    got = cache.get("b")
    got["text"] = "改不动缓存"  # 取到的是副本
    assert cache.get("b") == {"text": "B"}

    cache.clear()
    assert len(cache) == 0
    with pytest.raises(ValueError):
        ExplainCache(0)


def test_explain_cache_survives_concurrent_readers_and_writers() -> None:
    cache = ExplainCache(8)
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(200):
                key = f"k{n}-{i % 20}"
                cache.put(key, {"text": key})
                cache.get(key)
                cache.get("missing")
                assert len(cache) <= 8
        except BaseException as exc:  # noqa: BLE001 - 线程里的异常必须带回主线程
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []


def test_cache_key_is_the_frozen_tuple(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    (profiles / "local-default").mkdir(parents=True)
    profile = profiles / "local-default" / "profile.yaml"
    profile.write_text("reader: local-default\n", encoding="utf-8")

    key = explain_cache_key(
        source_id="s", paragraph_id="p001", kind="explain", selection="片段", profiles_dir=profiles
    )
    assert key[:4] == ("s", "p001", "explain", "片段")
    assert key[4][0] == str(profile) and key[4][1] == profile.stat().st_size
    assert key[4][2] == profile.stat().st_mtime_ns

    profile.write_text("reader: local-default\nconcepts: {c01: {state: known}}\n", encoding="utf-8")
    changed = explain_cache_key(
        source_id="s", paragraph_id="p001", kind="explain", selection="片段", profiles_dir=profiles
    )
    assert changed != key, "画像一变，键就得变"

    missing = explain_cache_key(
        source_id="s", paragraph_id="p001", kind="explain", selection="片段", profiles_dir=profiles, reader="nobody"
    )
    assert missing[4][1:] == (-1, -1), "画像文件不存在时用哨兵指纹（文件被删也算变化）"
