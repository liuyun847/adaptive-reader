"""工具层（``ar.tools``）的用例：注册表、入参契约、五个工具的出参与提示词复用。

全部用假模型客户端：**不发任何真实网络请求**，也不真的等待（假客户端的策略是「不重试」）。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from ar import tools
from ar.errors import InputError
from ar.llm import LlmFailure, LlmRequest, LlmResult, RetryPolicy
from ar.prompts import (
    COMPRESS_PROMPT_VERSION,
    CONCEPTS_PROMPT_VERSION,
    EXPLAIN_PROMPT_VERSION,
    REWRITE_PROMPT_VERSION,
    SURVEY_PROMPT_VERSION,
    build_compress_messages,
    build_concepts_messages,
    build_explain_messages,
    build_rewrite_messages,
    build_survey_messages,
)

ENVELOPE_KEYS = {"ok", "data", "failure", "attempts", "usage", "raw_text", "degraded"}

ANALYZE_JSON = json.dumps(
    {
        "concepts": [
            {
                "id": "c01",
                "name": "商品",
                "surface": ["商品"],
                "prereq": [],
                "difficulty_hint": 2,
                "paragraphs": ["p001"],
                "domain": "政治经济学",
                "concept_key": "commodity",
            }
        ],
        "illustration_candidates": [{"paragraph": "p001", "kind": "流程", "why": "递进关系更适合画图"}],
    },
    ensure_ascii=False,
)

REWRITE_JSON = json.dumps(
    {
        "decisions": [
            {
                "paragraph": "p001",
                "action": "expand",
                "why": {"profile": ["c01:unknown"], "note": "先讲清商品"},
                "confidence": 0.8,
                "rewrite": "第一段原文。再多说一句。",
            },
            {
                "paragraph": "p002",
                "action": "keep",
                "why": {"profile": [], "note": "读者已熟悉"},
                "confidence": 0.6,
                "rewrite": "",
            },
        ]
    },
    ensure_ascii=False,
)

#: illustrate 段（figure 走 ``_normalize_plan_items`` → 出参 schema 的 ``figure``）
REWRITE_WITH_FIGURE_JSON = json.dumps(
    {
        "decisions": [
            {
                "paragraph": "p001",
                "action": "illustrate",
                "why": {"profile": [], "note": "三步递进，纯文字看不出先后"},
                "confidence": 0.8,
                "rewrite": "",
                "figure": {"kind": "mermaid", "source": "flowchart TD\n  A[商品] --> B[货币]", "caption": "三步递进"},
            }
        ]
    },
    ensure_ascii=False,
)

SURVEY_JSON = json.dumps(
    {"questions": [{"q": f"第 {index} 个问题？", "hint": "随便说说"} for index in range(1, 5)]},
    ensure_ascii=False,
)

#: 第五个工具（``compress_paragraph``）的假模型回复：``text`` 前后带空白，考 strip
COMPRESS_JSON = json.dumps(
    {"action": "compress", "text": "  这一段只说一件事。  ", "why_note": "删掉了重复三遍的同义表述"},
    ensure_ascii=False,
)


@dataclass
class ModelStub:
    """假模型客户端：固定回一段文本（或固定失败），记录收到的请求。"""

    text: str = ""
    configured: bool = True
    failure: LlmFailure | None = None
    usage: dict[str, Any] | None = None
    model: str = "fake-model"
    policy: RetryPolicy = field(default_factory=lambda: RetryPolicy(max_retries=0))
    requests: list[LlmRequest] = field(default_factory=list)

    async def arequest_once(self, request: LlmRequest) -> LlmResult:
        self.requests.append(copy.deepcopy(request))
        if self.failure is not None:
            return LlmResult(ok=False, failure=self.failure)
        return LlmResult(ok=True, text=self.text, model=self.model, usage=self.usage, finish_reason="stop")

    def retry_policy(self) -> RetryPolicy:
        return self.policy


PARAGRAPHS = [{"id": "p001", "text": "第一段"}, {"id": "p002", "text": "第二段"}]

MINIMAL_PARAMS: dict[str, dict[str, Any]] = {
    "analyze_concepts": {"paragraphs": PARAGRAPHS},
    "rewrite_batch": {
        "paragraphs": PARAGRAPHS,
        "start": 1,
        "end": 2,
        "profile_block": "读者画像：空",
        "concept_block": "概念清单：无",
        "background_sketch": "未提供",
    },
    "explain_paragraph": {
        "paragraph": "p001",
        "kind": "explain",
        "text": "第一段",
        "selection_from": "rewritten",
        "profile_hint": "",
    },
    "survey_followups": {"sketch": "读者背景：未提供", "answers": {"education": "undergraduate"}},
    "compress_paragraph": {"paragraph": "p001", "text": "这一段太啰嗦：同一个意思说了三遍，说了三遍。"},
}

TOOL_TEXTS = {
    "analyze_concepts": ANALYZE_JSON,
    "rewrite_batch": REWRITE_JSON,
    "explain_paragraph": "这一段在讲商品的两重性。",
    "survey_followups": SURVEY_JSON,
    "compress_paragraph": COMPRESS_JSON,
}


# ------------------------------------------------------------------ 注册表 --
def test_registry_holds_exactly_the_five_named_tools() -> None:
    names = [spec.name for spec in tools.all_tools()]
    assert names == [
        "analyze_concepts",
        "rewrite_batch",
        "explain_paragraph",
        "survey_followups",
        "compress_paragraph",
    ]
    assert len(set(names)) == 5


def test_schemas_are_json_serializable_and_complete() -> None:
    payload = tools.schemas()
    assert isinstance(payload, list) and len(payload) == 5
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload, "必须可被 json.dumps / json.loads 往返"
    for spec in payload:
        assert set(spec) == {"name", "description", "parameters", "output", "prompt_version"}
        assert spec["description"].strip().endswith(".")
        assert spec["parameters"]["type"] == "object" and spec["parameters"]["required"]
        assert spec["output"]["type"] == "object"


def test_prompt_versions_match_the_frozen_prompts() -> None:
    assert tools.get("analyze_concepts").prompt_version == CONCEPTS_PROMPT_VERSION
    assert tools.get("rewrite_batch").prompt_version == REWRITE_PROMPT_VERSION
    assert tools.get("explain_paragraph").prompt_version == EXPLAIN_PROMPT_VERSION
    assert tools.get("survey_followups").prompt_version == SURVEY_PROMPT_VERSION
    assert tools.get("compress_paragraph").prompt_version == COMPRESS_PROMPT_VERSION == "compress-v3"


def test_get_unknown_tool_is_an_error() -> None:
    with pytest.raises(InputError, match="没有这个工具"):
        tools.get("frobnicate")


def test_register_refuses_duplicates_and_non_specs() -> None:
    with pytest.raises(InputError, match="工具名重复"):
        tools.register(tools.ANALYZE_CONCEPTS_SPEC)
    with pytest.raises(InputError, match="ToolSpec"):
        tools.register({"name": "x"})  # type: ignore[arg-type]
    assert len(tools.all_tools()) == 5, "失败的注册不许污染注册表"


def test_ar_tools_cli_lists_the_five_contracts(run_cli) -> None:
    """``ar tools --json`` 的输出随之从四个工具变五个（含 ``compress_paragraph``）。

    ⚠ ``tests/test_cli.py`` 里那几条「恰好四个工具 / rewrite-v3」的断言归 E2 那一路
    （契约 §10 的独占写范围），本批不动它；这里在**自己负责的文件**里守住同一个事实。
    """
    code, output = run_cli("tools", "--json")
    assert code == 0, output
    payload = json.loads(output)
    assert [item["name"] for item in payload] == [
        "analyze_concepts",
        "rewrite_batch",
        "explain_paragraph",
        "survey_followups",
        "compress_paragraph",
    ]
    versions = {item["name"]: item["prompt_version"] for item in payload}
    assert versions["rewrite_batch"] == "rewrite-v7"
    assert versions["compress_paragraph"] == "compress-v3"
    assert versions["analyze_concepts"] == "concepts-v2"


# ------------------------------------------------------------------ 入参契约 --
# 入参不合契约 = 调用方的 bug，但**不抛异常**（2026-10-13 复核修复 P1c）：走与模型失败同一形状的
# 失败信封（``INVALID_REQUEST``）、一个请求都不发。抛 ``InputError`` 会被 HTTP 层当成 400
# （把内部错误伪装成客户端输入错误），其它异常则成 500 —— 与「失败不抛」的契约冲突。
@pytest.mark.parametrize("name", sorted(MINIMAL_PARAMS))
def test_missing_required_field_returns_an_invalid_request_envelope(name: str) -> None:
    spec = tools.get(name)
    params = dict(MINIMAL_PARAMS[name])
    dropped = spec.parameters["required"][-1]
    params.pop(dropped)
    stub = ModelStub(text=TOOL_TEXTS[name])

    envelope = spec.execute(params, client=stub)

    assert set(envelope) == ENVELOPE_KEYS, "失败信封与成功信封同一键集"
    assert envelope["ok"] is False and envelope["data"] is None
    assert envelope["failure"]["code"] == tools.INVALID_PARAMS_CODE == "INVALID_REQUEST"
    assert f"缺必填字段 {dropped}" in envelope["failure"]["message"]
    assert any(dropped in item for item in envelope["degraded"]), "降级要说明是哪个参数不合法"
    assert envelope["attempts"] == [] and envelope["usage"] is None and envelope["raw_text"] == ""
    assert stub.requests == [], "入参不合契约时一个请求都不许发"


@pytest.mark.parametrize("name", sorted(MINIMAL_PARAMS))
def test_unknown_field_is_rejected(name: str) -> None:
    params = {**MINIMAL_PARAMS[name], "nope": 1}
    envelope = tools.get(name).execute(params, client=ModelStub(text=TOOL_TEXTS[name]))

    assert envelope["ok"] is False
    assert envelope["failure"]["code"] == tools.INVALID_PARAMS_CODE
    assert "未知字段 nope" in envelope["failure"]["message"]
    assert any("未知字段 nope" in item for item in envelope["degraded"])


def test_wrong_types_are_rejected() -> None:
    cases: list[tuple[str, Any, str]] = [
        ("analyze_concepts", {"paragraphs": "第一段"}, "类型应为"),
        ("analyze_concepts", {"paragraphs": []}, "至少 1 项"),
        ("explain_paragraph", {**MINIMAL_PARAMS["explain_paragraph"], "kind": "frobnicate"}, "只能是"),
        ("survey_followups", "不是对象", "必须是 JSON 对象"),
    ]
    for name, params, fragment in cases:
        envelope = tools.get(name).execute(params, client=ModelStub(text=TOOL_TEXTS[name]))
        assert envelope["ok"] is False, (name, fragment)
        assert envelope["failure"]["code"] == tools.INVALID_PARAMS_CODE, (name, envelope)
        assert fragment in envelope["failure"]["message"], (name, fragment, envelope)


def test_invalid_params_win_over_not_configured_and_never_raise() -> None:
    """校验先于调用：没传 client 也是「入参不合法」，不是 ``NOT_CONFIGURED``；且不抛异常。"""
    envelope = tools.get("analyze_concepts").execute({"paragraphs": 5})

    assert envelope["failure"]["code"] == tools.INVALID_PARAMS_CODE
    assert envelope["attempts"] == []


def test_schema_problems_flags_nested_violations() -> None:
    schema = tools.get("analyze_concepts").output
    good = json.loads(ANALYZE_JSON)
    assert tools.schema_problems(schema, good) == []
    bad = {"concepts": [{"id": "c01"}], "illustration_candidates": "不是数组"}
    problems = tools.schema_problems(schema, bad)
    assert any("缺必填字段 name" in item for item in problems)
    assert any("illustration_candidates 类型应为 array" in item for item in problems)


# ------------------------------------------------------------------ 五个工具 --
@pytest.mark.parametrize("name", sorted(MINIMAL_PARAMS))
def test_each_tool_returns_a_schema_conforming_envelope(name: str) -> None:
    stub = ModelStub(text=TOOL_TEXTS[name], usage={"prompt_tokens": 7, "completion_tokens": 3})
    spec = tools.get(name)
    envelope = spec.execute(MINIMAL_PARAMS[name], client=stub)

    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["ok"] is True and envelope["failure"] is None
    assert tools.schema_problems(spec.output, envelope["data"], path="data") == []
    assert envelope["usage"] == {"prompt_tokens": 7, "completion_tokens": 3}
    assert envelope["raw_text"] == TOOL_TEXTS[name]
    assert [item["try"] for item in envelope["attempts"]] == [1]
    assert envelope["attempts"][0]["tool"] == name
    assert envelope["attempts"][0]["code"] is None


def test_analyze_concepts_returns_the_parsed_inventory() -> None:
    envelope = tools.get("analyze_concepts").execute({"paragraphs": PARAGRAPHS}, client=ModelStub(text=ANALYZE_JSON))
    data = envelope["data"]
    assert [item["id"] for item in data["concepts"]] == ["c01"]
    assert data["illustration_candidates"][0]["kind"] == "流程"


def test_analyze_concepts_honours_max_concepts_budget() -> None:
    two = json.dumps(
        {"concepts": json.loads(ANALYZE_JSON)["concepts"] * 2, "illustration_candidates": []},
        ensure_ascii=False,
    )
    envelope = tools.get("analyze_concepts").execute(
        {"paragraphs": PARAGRAPHS, "max_concepts": 1}, client=ModelStub(text=two)
    )
    assert len(envelope["data"]["concepts"]) == 1


def test_rewrite_batch_returns_normalized_decisions() -> None:
    envelope = tools.get("rewrite_batch").execute(MINIMAL_PARAMS["rewrite_batch"], client=ModelStub(text=REWRITE_JSON))
    decisions = envelope["data"]["decisions"]
    assert [item["paragraph"] for item in decisions] == ["p001", "p002"]
    assert decisions[0]["action"] == "expand" and decisions[0]["rewrite"].startswith("第一段原文")
    assert decisions[0]["why"] == {"profile": ["c01:unknown"], "note": "先讲清商品"}
    assert envelope["degraded"] == []
    # 模型没给图时不留 figure 键（出参与 schema 逐键一致）
    assert all("figure" not in item for item in decisions)


def test_rewrite_batch_carries_figure_through_normalization() -> None:
    """``figure`` **不许被静默丢弃**：模型输出不经 JSON Schema 校验，全靠 ``_normalize_plan_items`` 带上。"""
    envelope = tools.get("rewrite_batch").execute(
        MINIMAL_PARAMS["rewrite_batch"], client=ModelStub(text=REWRITE_WITH_FIGURE_JSON)
    )
    assert envelope["ok"] is True
    decision = envelope["data"]["decisions"][0]
    assert decision["action"] == "illustrate"
    assert decision["figure"] == {
        "kind": "mermaid",
        "source": "flowchart TD\n  A[商品] --> B[货币]",
        "caption": "三步递进",
    }
    assert tools.schema_problems(tools.get("rewrite_batch").output, envelope["data"], path="data") == []


def test_rewrite_batch_surfaces_out_of_batch_ids_as_visible_degradation() -> None:
    payload = json.dumps(
        {
            "decisions": [
                {"paragraph": "p001", "action": "keep", "why": {"profile": [], "note": ""}, "confidence": 1, "rewrite": ""},
                {"paragraph": "p999", "action": "keep", "why": {"profile": [], "note": ""}, "confidence": 1, "rewrite": ""},
            ]
        },
        ensure_ascii=False,
    )
    envelope = tools.get("rewrite_batch").execute(MINIMAL_PARAMS["rewrite_batch"], client=ModelStub(text=payload))
    assert envelope["ok"] is True
    assert [item["paragraph"] for item in envelope["data"]["decisions"]] == ["p001"]
    assert any("p999" in item for item in envelope["degraded"])


def test_explain_paragraph_passes_selection_and_neighbours() -> None:
    stub = ModelStub(text="  讲解正文。  ")
    params = {
        **MINIMAL_PARAMS["explain_paragraph"],
        "selection": "选中的一句话",
        "prev": "上一段正文",
        "next": "下一段正文",
    }
    envelope = tools.get("explain_paragraph").execute(params, client=stub)

    assert envelope["data"] == {"text": "讲解正文。"}, "讲解正文按 explain.py 的老口径 strip"
    user_message = stub.requests[0].messages[1]["content"]
    assert "选中的一句话" in user_message
    assert "[prev] 上一段正文" in user_message and "[next] 下一段正文" in user_message


def test_survey_followups_returns_three_to_five_questions() -> None:
    envelope = tools.get("survey_followups").execute(MINIMAL_PARAMS["survey_followups"], client=ModelStub(text=SURVEY_JSON))
    assert len(envelope["data"]["questions"]) == 4
    assert set(envelope["data"]["questions"][0]) == {"q", "hint"}


# ------------------------------------------- 第五个工具：单段压缩（compress-v2） --
def test_compress_paragraph_returns_the_compressed_text() -> None:
    """出参形状固定：``{action:"compress", text, why_note?}``；正文按段落口径 strip。"""
    envelope = tools.get("compress_paragraph").execute(MINIMAL_PARAMS["compress_paragraph"], client=ModelStub(text=COMPRESS_JSON))
    assert envelope["ok"] is True and envelope["failure"] is None
    assert envelope["data"] == {
        "action": "compress",
        "text": "这一段只说一件事。",
        "why_note": "删掉了重复三遍的同义表述",
    }
    assert tools.schema_problems(tools.get("compress_paragraph").output, envelope["data"], path="data") == []


def test_compress_paragraph_carries_the_policy_and_returns_the_raw_declaration() -> None:
    """``compress-v2``（§14.1）：口径进提示词的「事实锚口径」块，申报**原样**带回。

    归一与问题清单归 ``ar.fidelity.parse_declarations``（业务层做），工具层只做「是不是对象」的粗筛
    ——与 ``rewrite_batch`` 把 decisions 原样带回同一条分工。
    """
    from ar.prompts import fact_anchor_block

    payload = json.dumps(
        {
            "action": "compress",
            "text": "短了，但少了个数字。",
            "fact_declarations": {"numbers": ["1696"], "note": "举例数据舍弃"},
        },
        ensure_ascii=False,
    )
    stub = ModelStub(text=payload)
    params = {**MINIMAL_PARAMS["compress_paragraph"], "fact_anchor_policy": "allow_declared_drops"}
    envelope = tools.get("compress_paragraph").execute(params, client=stub)

    assert envelope["ok"] is True
    assert envelope["data"]["fact_declarations"] == {"numbers": ["1696"], "note": "举例数据舍弃"}
    assert tools.schema_problems(tools.get("compress_paragraph").output, envelope["data"], path="data") == []
    prompt = stub.requests[0].messages[-1]["content"]
    assert fact_anchor_block("allow_declared_drops", rule_no=6) in prompt
    assert fact_anchor_block("keep_all", rule_no=6) not in prompt
    # 不传口径（老调用方）→ 与「传 keep_all」逐字相同
    stub2 = ModelStub(text=payload)
    tools.get("compress_paragraph").execute(MINIMAL_PARAMS["compress_paragraph"], client=stub2)
    assert fact_anchor_block("keep_all", rule_no=6) in stub2.requests[0].messages[-1]["content"]


def test_compress_paragraph_ignores_an_empty_or_malformed_declaration() -> None:
    """申报不是对象 / 空对象 → 出参里**不出现**这个键（有没有申报由业务层按「键在不在」判）。"""
    for declaration in ({}, [], "数字 1696", None):
        payload = json.dumps(
            {"action": "compress", "text": "短了。", "fact_declarations": declaration}, ensure_ascii=False
        )
        envelope = tools.get("compress_paragraph").execute(
            MINIMAL_PARAMS["compress_paragraph"], client=ModelStub(text=payload)
        )
        assert envelope["ok"] is True
        assert "fact_declarations" not in envelope["data"], declaration


def test_compress_paragraph_why_note_is_optional() -> None:
    payload = json.dumps({"action": "compress", "text": "短了。"}, ensure_ascii=False)
    envelope = tools.get("compress_paragraph").execute(MINIMAL_PARAMS["compress_paragraph"], client=ModelStub(text=payload))
    assert envelope["ok"] is True
    assert envelope["data"] == {"action": "compress", "text": "短了。"}


def test_compress_paragraph_model_action_field_is_not_trusted() -> None:
    """模型把 action 写成别的词也不影响出参：这次调用的意图就是压缩。"""
    payload = json.dumps({"action": "expand", "text": "压缩后的正文。"}, ensure_ascii=False)
    envelope = tools.get("compress_paragraph").execute(MINIMAL_PARAMS["compress_paragraph"], client=ModelStub(text=payload))
    assert envelope["data"]["action"] == "compress"


def test_compress_reply_without_text_fails_with_the_raw_text() -> None:
    payload = json.dumps({"action": "compress", "text": "   "}, ensure_ascii=False)
    envelope = tools.get("compress_paragraph").execute(MINIMAL_PARAMS["compress_paragraph"], client=ModelStub(text=payload))
    assert envelope["ok"] is False and envelope["failure"]["code"] == tools.PARSE_FAILURE_CODE
    assert "没有返回压缩后的正文" in envelope["failure"]["message"]
    assert envelope["raw_text"] == payload


def test_compress_paragraph_schema_declares_all_six_parameters_and_only_two_required() -> None:
    """契约 §5.1 的入参表逐项对上：6 个属性（``compress-v2`` 补了 ``fact_anchor_policy``）、
    只有 ``paragraph`` / ``text`` 必填。"""
    spec = tools.get("compress_paragraph")
    assert set(spec.parameters["properties"]) == {
        "paragraph", "text", "concepts", "profile_block", "instruction", "fact_anchor_policy",
    }
    assert spec.parameters["required"] == ["paragraph", "text"]
    # 缺字段 / 多字段一律回 INVALID_REQUEST 信封（不抛、不发请求）
    stub = ModelStub(text=COMPRESS_JSON)
    for params in ({"paragraph": "p001"}, {"paragraph": "p001", "text": "x", "nope": 1}, "不是对象"):
        envelope = spec.execute(params, client=stub)
        assert envelope["ok"] is False
        assert envelope["failure"]["code"] == tools.INVALID_PARAMS_CODE == "INVALID_REQUEST"
        assert set(envelope) == ENVELOPE_KEYS
    assert stub.requests == []


# ------------------------------------------------------ 失败与未配置的分支 --
def test_model_failure_is_reported_with_its_normalized_code() -> None:
    stub = ModelStub(failure=LlmFailure(code="RATE_LIMIT", message="HTTP 429：太频繁", status=429))
    envelope = tools.get("analyze_concepts").execute({"paragraphs": PARAGRAPHS}, client=stub)

    assert envelope["ok"] is False and envelope["data"] is None
    assert envelope["failure"] == {
        "code": "RATE_LIMIT",
        "message": "HTTP 429：太频繁",
        "status": 429,
        "retry_after_ms": None,
    }
    assert len(stub.requests) == 1, "策略来自客户端（max_retries=0），不许自己加戏"


def test_unparsable_reply_keeps_the_raw_text_and_fails() -> None:
    for name in ("analyze_concepts", "rewrite_batch", "survey_followups", "compress_paragraph"):
        envelope = tools.get(name).execute(MINIMAL_PARAMS[name], client=ModelStub(text="模型说了些废话，没有 JSON"))
        assert envelope["ok"] is False, name
        assert envelope["failure"]["code"] == tools.PARSE_FAILURE_CODE
        assert envelope["raw_text"] == "模型说了些废话，没有 JSON", "原文要留给调用方落 raw.txt"
        assert envelope["data"] is None


def test_survey_reply_with_wrong_question_count_fails() -> None:
    payload = json.dumps({"questions": [{"q": "只有一个问题？", "hint": ""}]}, ensure_ascii=False)
    envelope = tools.get("survey_followups").execute(MINIMAL_PARAMS["survey_followups"], client=ModelStub(text=payload))
    assert envelope["ok"] is False and envelope["failure"]["code"] == tools.PARSE_FAILURE_CODE
    assert "3–5" in envelope["failure"]["message"]


def test_configured_client_is_required() -> None:
    for name in sorted(MINIMAL_PARAMS):
        envelope = tools.get(name).execute(MINIMAL_PARAMS[name])
        assert envelope["ok"] is False and envelope["failure"]["code"] == "NOT_CONFIGURED"
        assert envelope["attempts"] == []

    offline = ModelStub(text=TOOL_TEXTS["analyze_concepts"], configured=False)
    envelope = tools.get("analyze_concepts").execute({"paragraphs": PARAGRAPHS}, client=offline)
    assert envelope["ok"] is False and envelope["failure"]["code"] == "NOT_CONFIGURED"
    assert offline.requests == []


def test_client_policy_governs_retries_and_the_account_is_per_attempt() -> None:
    stub = ModelStub(
        text=ANALYZE_JSON,
        failure=LlmFailure(code="SERVER", message="HTTP 503：网关抖动"),
        policy=RetryPolicy(max_retries=0),
    )
    envelope = tools.get("analyze_concepts").execute({"paragraphs": PARAGRAPHS}, client=stub)
    assert len(stub.requests) == 1 and envelope["attempts"][0]["code"] == "SERVER"

    # 显式传策略（调用方注入）时以注入的为准：这里给「不重试」的策略，仍只试一次
    explicit = tools.get("analyze_concepts").execute(
        {"paragraphs": PARAGRAPHS}, client=ModelStub(text=ANALYZE_JSON), policy=RetryPolicy(max_retries=0)
    )
    assert explicit["ok"] is True


# ------------------------------------------------------ 提示词逐字复用 --
def test_analyze_tool_reuses_the_concepts_prompt_verbatim() -> None:
    stub = ModelStub(text=ANALYZE_JSON)
    tools.get("analyze_concepts").execute({"paragraphs": PARAGRAPHS}, client=stub)
    assert stub.requests[0].messages == tuple(build_concepts_messages({"paragraphs": PARAGRAPHS}))


def test_rewrite_tool_reuses_the_rewrite_prompt_verbatim() -> None:
    stub = ModelStub(text=REWRITE_JSON)
    params = {**MINIMAL_PARAMS["rewrite_batch"], "source_id": "demo", "total": 22}
    tools.get("rewrite_batch").execute(params, client=stub)
    expected = build_rewrite_messages(
        "demo",
        PARAGRAPHS,
        profile_block=params["profile_block"],
        background_block=params["background_sketch"],
        concept_block=params["concept_block"],
        start=1,
        end=2,
        total=22,
    )
    assert stub.requests[0].messages == tuple(expected)
    assert "材料：《demo》，共 22 段" in stub.requests[0].messages[1]["content"]


def test_rewrite_tool_without_source_id_or_total_still_builds_a_prompt() -> None:
    stub = ModelStub(text=REWRITE_JSON)
    tools.get("rewrite_batch").execute(MINIMAL_PARAMS["rewrite_batch"], client=stub)
    assert "共 2 段" in stub.requests[0].messages[1]["content"], "没给总段数时按这批的末位序号写"


def test_rewrite_batch_passes_the_illustration_block_through_and_it_stays_optional() -> None:
    """``illustration_block`` 是**可选**入参（不进 required）：不传时提示词逐字等同缺省版。"""
    spec = tools.get("rewrite_batch")
    assert "illustration_block" in spec.parameters["properties"]
    assert "illustration_block" not in spec.parameters["required"]

    plain = ModelStub(text=REWRITE_JSON)
    spec.execute(MINIMAL_PARAMS["rewrite_batch"], client=plain)
    # ⚠ 规则 2 里也提到「配图候选」四个字，所以这里认的是**块标题**（只有真插了块才有）
    assert "配图候选（analyze 标出" not in plain.requests[0].messages[1]["content"]

    block = "p001 流程：三步递进，纯文字看不出先后"
    with_block = ModelStub(text=REWRITE_JSON)
    params = {**MINIMAL_PARAMS["rewrite_batch"], "illustration_block": block}
    spec.execute(params, client=with_block)
    content = with_block.requests[0].messages[1]["content"]
    assert block in content and content.count("配图候选（analyze 标出") == 1
    assert content.index("材料：《》") < content.index("概念清单：") < content.index(block)
    expected = build_rewrite_messages(
        "",
        PARAGRAPHS,
        profile_block=params["profile_block"],
        background_block=params["background_sketch"],
        concept_block=params["concept_block"],
        illustration_block=block,
        start=1,
        end=2,
        total=2,
    )
    assert with_block.requests[0].messages == tuple(expected)


def test_explain_tool_reuses_the_explain_prompt_verbatim() -> None:
    stub = ModelStub(text="讲解")
    tools.get("explain_paragraph").execute(MINIMAL_PARAMS["explain_paragraph"], client=stub)
    expected = build_explain_messages(
        kind="explain",
        paragraph_id="p001",
        paragraph_text="第一段",
        neighbors=None,
        profile_hint="",
        selection=None,
        selection_from="rewritten",
    )
    assert stub.requests[0].messages == tuple(expected)


def test_survey_tool_reuses_the_survey_prompt_verbatim() -> None:
    stub = ModelStub(text=SURVEY_JSON)
    tools.get("survey_followups").execute(MINIMAL_PARAMS["survey_followups"], client=stub)
    expected = build_survey_messages(sketch="读者背景：未提供", answers={"education": "undergraduate"})
    assert stub.requests[0].messages == tuple(expected)


def test_compress_tool_reuses_the_compress_prompt_verbatim() -> None:
    """第五个工具也逐字复用 ``ar.prompts`` 的模板（工具只管拼参数，一个字都不改）。"""
    stub = ModelStub(text=COMPRESS_JSON)
    params = {
        **MINIMAL_PARAMS["compress_paragraph"],
        "concepts": "c01 假概念（难度 3）",
        "profile_block": "读者 tester 自报概念状态：c01=unknown",
        "instruction": "这段太啰嗦，只留一遍。",
    }
    tools.get("compress_paragraph").execute(params, client=stub)
    expected = build_compress_messages(
        paragraph="p001",
        text=params["text"],
        concepts=params["concepts"],
        profile_block=params["profile_block"],
        instruction=params["instruction"],
    )
    assert stub.requests[0].messages == tuple(expected)
    assert stub.requests[0].prompt_version == COMPRESS_PROMPT_VERSION
    # 会变的部分（本段原文）在末尾，固定块在前（前缀缓存纪律）
    user = stub.requests[0].messages[1]["content"]
    assert user.rstrip().endswith(f"[p001] {params['text']}")
    assert user.index("请输出这样的 JSON") < user.index("规则：") < user.index("[p001] ")


def test_tool_requests_carry_the_tool_name_and_prompt_version() -> None:
    stub = ModelStub(text=ANALYZE_JSON)
    tools.get("analyze_concepts").execute({"paragraphs": PARAGRAPHS}, client=stub)
    request = stub.requests[0]
    assert request.tool == "analyze_concepts"
    assert request.prompt_version == CONCEPTS_PROMPT_VERSION
    assert request.temperature is None and request.max_tokens is None, "温度与 max_tokens 交给客户端配置"


# ------------------------------------------------- 面向用户的失败文案（P1a） --
def test_run_tool_reports_invalid_params_as_an_envelope_instead_of_raising() -> None:
    """``run_tool`` 与 ``execute`` 同一条路：入参不合法也**不抛**（HTTP 路由因此不会 400 / 500）。"""
    stub = ModelStub(text=ANALYZE_JSON)
    run = tools.run_tool("analyze_concepts", {"paragraphs": "不是数组"}, client=stub)

    assert run.ok is False and run.failure_code == tools.INVALID_PARAMS_CODE
    assert run.result is None and run.attempt_records == [] and stub.requests == []
    # 文案照样带「已试 0 次」（一次请求都没发）：调用方不必区分「没发」与「发了失败」
    assert run.failure_message().startswith("模型调用失败（已试 0 次，末次 INVALID_REQUEST）：")


def test_failure_message_carries_the_attempt_count_and_the_last_code() -> None:
    """失败文案 = 「模型调用失败（已试 N 次，末次 <CODE>）：…」：N 是真实请求数，CODE 是末次错误码。"""
    stub = ModelStub(
        failure=LlmFailure(code="SERVER", message="HTTP 503：网关抖动", status=503),
        policy=RetryPolicy(max_retries=1, initial_delay_ms=0, max_delay_ms=0),
    )
    run = tools.run_tool("analyze_concepts", {"paragraphs": PARAGRAPHS}, client=stub)

    assert run.ok is False and len(stub.requests) == 2, "首发失败 + 一次重试"
    assert [item["try"] for item in run.attempt_records] == [1, 2]
    text = run.failure_message()
    assert text.startswith("模型调用失败（已试 2 次，末次 SERVER）：")
    assert "HTTP 503" in text
    # 命令行的报错也要看得见重试：analyze / rewrite / explain / survey 用的都是这一句
    assert "已试 2 次" in text

def test_rewrite_batch_passes_full_text_and_feedback_through_and_they_stay_optional() -> None:
    """rewrite-v7 的两个可选入参: 全文进材料段、反馈只压在 user 末尾。"""
    spec = tools.get("rewrite_batch")
    for key in ("full_text_block", "feedback"):
        assert key in spec.parameters["properties"]
        assert key not in spec.parameters["required"]

    stub = ModelStub(text=REWRITE_JSON)
    params = {
        **MINIMAL_PARAMS["rewrite_batch"],
        "full_text_block": "[p001] 甲\n\n[p002] 乙",
        "feedback": "【校验反馈】少了 1696",
    }
    spec.execute(params, client=stub)
    content = stub.requests[0].messages[1]["content"]
    assert "原文（全文" in content and "[p002] 乙" in content
    assert content.index("规则：") < content.index("原文（全文") < content.index("本次给你第")
    assert content.rstrip().endswith("少了 1696")
