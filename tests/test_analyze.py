"""M2：概念清单抽取与契约校验（含负向对照）。

负向用例照 ``学习/_整理/selfcheck.py`` 的做法：只在**临时副本**上注入缺陷、
断言注入确实生效、然后看退出码。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ar.analyze import (
    ILLUSTRATION_KINDS,
    check_inventory_file,
    extract_json_object,
    format_illustration_block,
    normalize_inventory,
    run_analyze,
    validate_inventory,
)
from ar.errors import EXIT_MODEL, EXIT_OK, EXIT_VALIDATION, InputError, ModelUnavailable, ValidationFailed
from ar.ingest import ingest
from ar.llm import LlmFailure, LlmRequest, LlmResult, RetryPolicy

GOOD_JSON = json.dumps(
    {
        "concepts": [
            {
                "id": "c01",
                "name": "商品",
                "surface": ["商品"],
                "prereq": [],
                "difficulty_hint": 2,
                "paragraphs": ["p001", "p002"],
                "domain": "政治经济学",
                "concept_key": "commodity",
            },
            {
                "id": "c02",
                "name": "使用价值",
                "surface": ["使用价值"],
                "prereq": ["c01"],
                "difficulty_hint": 3,
                "paragraphs": ["p002"],
                "domain": "政治经济学",
                "concept_key": "use_value",
            },
        ],
        "illustration_candidates": [{"paragraph": "p003", "kind": "流程", "why": "三步递进，纯文字看不出先后"}],
    },
    ensure_ascii=False,
)

MESSY_JSON = json.dumps(
    {
        "concepts": [
            {
                "id": "c01",
                "name": "商品",
                "surface": [],
                "prereq": ["c99", "c01"],
                "difficulty_hint": 9,
                "paragraphs": ["p001", "p999"],
                "domain": None,
                "concept_key": None,
            },
            {"id": "c02", "name": "只剩野引用", "paragraphs": ["p777"]},
            {"id": "c01", "name": "重复 id", "paragraphs": ["p002"]},
            {"name": "没有 id"},
            "这不是对象",
        ],
        "illustration_candidates": [
            {"paragraph": "p999", "kind": "流程", "why": "段不存在"},
            {"paragraph": "p001", "kind": "时间", "why": "kind 不在四类内"},
            {"paragraph": "p002", "kind": "对比"},
            {"paragraph": "p003", "kind": "结构", "why": "合法的一条"},
        ],
    },
    ensure_ascii=False,
)


def _meta(paragraph_count: int = 3) -> dict:
    return {"paragraphs": [{"id": f"p{i:03d}", "index": i} for i in range(1, paragraph_count + 1)]}


@dataclass
class _ScriptedClient:
    """按脚本逐次回结果（第 N 次调用用第 N 条，用完了重复最后一条）。

    一条脚本项要么是 ``LlmResult``（成功 / 失败都行），要么是字符串（= 一次成功的正文）。
    ``max_retries=1`` + 退避 0 ms：能考出「第一次 500、第二次成功」，而用例不必真的等。
    """

    replies: list[Any]
    policy: RetryPolicy = field(
        default_factory=lambda: RetryPolicy(max_retries=1, initial_delay_ms=0, max_delay_ms=0)
    )
    configured: bool = True
    calls: int = 0

    def retry_policy(self) -> RetryPolicy:
        return self.policy

    async def arequest_once(self, request: LlmRequest) -> LlmResult:
        index = min(self.calls, len(self.replies) - 1)
        self.calls += 1
        reply = self.replies[index]
        if isinstance(reply, LlmResult):
            return reply
        return LlmResult(ok=True, text=str(reply), model="fake-model", finish_reason="stop")


# ------------------------------------------------------------------ JSON 抽取 --
def test_extract_json_object_variants() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('好的，这是结果：\n{"a": {"b": "}"}}\n希望有用') == {"a": {"b": "}"}}
    with pytest.raises(InputError, match="找不到合法"):
        extract_json_object("完全没有 JSON 的输出")


# -------------------------------------------------------------------- 整理 --
def test_normalize_filters_bad_refs_and_records_repairs() -> None:
    inventory, repairs = normalize_inventory(json.loads(MESSY_JSON), _meta())
    # c01 保留；"只剩野引用"因段引用全无效被丢；重复 id 的那条改成 c03 保留；无 name 的与不是对象的被丢
    assert [c["id"] for c in inventory["concepts"]] == ["c01", "c03"]
    first = inventory["concepts"][0]
    assert first["paragraphs"] == ["p001"]
    assert first["prereq"] == []
    assert first["difficulty_hint"] == 5  # 9 截到上界
    assert first["surface"] == ["商品"]  # name 顶替
    assert first["domain"] == "" and first["concept_key"] == ""
    assert inventory["illustration_candidates"] == [
        {"paragraph": "p003", "kind": "结构", "why": "合法的一条"}
    ]
    joined = " | ".join(repairs)
    assert "p999" in joined and "p777" in joined
    assert "kind '时间' 不在" in joined
    assert "重复" in joined and "改用 c03" in joined
    assert "c99" in joined
    # 整理后必须能过严格校验（落盘的就是这个）
    assert validate_inventory(inventory, _meta()) == []


# -------------------------------------------------------------------- 校验 --
def test_concepts_template_keeps_the_material_at_the_end_for_prefix_cache() -> None:
    """concepts-v2：JSON 示例与规则在前、**材料正文在末尾**——跨材料共享前缀 ~1000 字符。

    这条守住重排：材料正文一旦挪回固定块中间，共享前缀会从 ~878 掉回 152 字符
    （只有 system 与标题行），跨材料重跑就再也吃不到前缀缓存。
    """
    from ar.prompts import build_concepts_messages

    first = build_concepts_messages({"paragraphs": [{"id": "p001", "text": "甲材料的正文。"}]})
    second = build_concepts_messages({"paragraphs": [{"id": "p001", "text": "乙材料的正文，完全不同。"}]})
    user_a, user_b = first[1]["content"], second[1]["content"]

    assert user_a.endswith("[p001] 甲材料的正文。")
    assert user_a.index("8. 拿不准的概念不要列") < user_a.index("[p001]"), "规则必须在正文之前"
    assert first[0]["content"] == second[0]["content"], "system 提示词两材料必须逐字相同"

    shared = next(
        (index for index, (left, right) in enumerate(zip(user_a, user_b)) if left != right),
        min(len(user_a), len(user_b)),
    )
    # 整条提示词的共享前缀 = system + user 里逐字相同的那一段（实测 ~885 字符）
    total_prefix = len(first[0]["content"]) + shared
    assert total_prefix >= 850, f"跨材料共享前缀只有 {total_prefix} 字符（目标 ~885）"


def test_validate_accepts_good_inventory() -> None:
    assert validate_inventory(json.loads(GOOD_JSON), _meta()) == []


def test_validate_rejects_dangling_paragraph_reference() -> None:
    inventory = json.loads(GOOD_JSON)
    inventory["concepts"][0]["paragraphs"] = ["p001", "p999"]
    problems = validate_inventory(inventory, _meta())
    assert problems and any("p999" in p and "c01" in p for p in problems), problems


def test_validate_rejects_kind_outside_four_categories() -> None:
    inventory = json.loads(GOOD_JSON)
    inventory["illustration_candidates"][0]["kind"] = "时间线"
    problems = validate_inventory(inventory, _meta())
    assert problems and any("kind" in p and "时间线" in p for p in problems), problems
    assert "时间线" not in ILLUSTRATION_KINDS


def test_validate_rejects_missing_fields_duplicates_and_prereq_dangling() -> None:
    inventory = {
        "concepts": [
            {"id": "c01", "name": "甲", "surface": ["甲"], "prereq": ["c09"], "paragraphs": ["p001"]},
            {"id": "c01", "name": "乙", "surface": ["乙"], "prereq": [], "paragraphs": ["p002"]},
        ],
        "illustration_candidates": [],
    }
    problems = validate_inventory(inventory, _meta())
    joined = " | ".join(problems)
    assert "缺字段" in joined and "difficulty_hint" in joined
    assert "concept_key" in joined
    assert "重复" in joined
    assert "prereq 指向不存在的概念 'c09'" in joined


# ---------------------------------------------------------------- 配图候选块 --
def test_illustration_block_is_deterministic_and_uses_the_candidate_order() -> None:
    """配图候选块是 ``rewrite-v4`` 提示词的固定块：**逐字稳定**，顺序照候选数组原序。

    它进的是提示词前缀缓存覆盖的那一段，所以任何一个「每次都不一样」的字符都是事故。
    """
    inventory = {
        "concepts": [],
        "illustration_candidates": [
            {"paragraph": "p003", "kind": "流程", "why": "三步递进，纯文字看不出先后"},
            {"paragraph": "p001", "kind": "对比", "why": "两种口径并列更适合画图"},
        ],
    }
    first = format_illustration_block(inventory)
    assert first == "p003 流程：三步递进，纯文字看不出先后\np001 对比：两种口径并列更适合画图"
    assert format_illustration_block(inventory) == first, "同一份清单两次调用必须同文本（否则前缀缓存每轮失效）"
    assert format_illustration_block(json.loads(json.dumps(inventory))) == first, "经 JSON 往返后仍是同文本"


def test_illustration_block_is_empty_when_there_is_nothing_to_show() -> None:
    """没有候选（或没有清单）时是**空串**：提示词那一格一个字符都不出现（与 rewrite-v3 等价）。"""
    assert format_illustration_block(None) == ""
    assert format_illustration_block({}) == ""
    assert format_illustration_block({"illustration_candidates": []}) == ""
    assert format_illustration_block({"illustration_candidates": "不是数组"}) == ""
    # 候选缺段 id 的一律跳过（落盘的清单不会有这种条目，这里守的是调用方直接喂脏数据的情形）
    assert format_illustration_block({"illustration_candidates": [{"kind": "流程", "why": "没有段 id"}]}) == ""


def test_illustration_block_caps_the_list_and_says_how_many_are_left() -> None:
    """超过上限时截断**并说明还有多少条**（省 token，但不静默截断）。"""
    inventory = {
        "illustration_candidates": [
            {"paragraph": f"p{index:03d}", "kind": "流程", "why": "理由"} for index in range(1, 26)
        ]
    }
    block = format_illustration_block(inventory, limit=3)
    assert block.splitlines()[:3] == ["p001 流程：理由", "p002 流程：理由", "p003 流程：理由"]
    assert block.splitlines()[-1] == "…另有 22 条未列出"


# -------------------------------------------------------------------- 主流程 --
def test_run_analyze_writes_contract_file_and_passes_check(dirs: SimpleNamespace, sample_md: Path, fake_client) -> None:
    ingest(sample_md, "sample", dirs.data)
    result = run_analyze("sample", dirs.data, fake_client(text=GOOD_JSON))

    payload = json.loads((dirs.data / "sample" / "concepts.json").read_text(encoding="utf-8"))
    assert set(payload) == {"concepts", "illustration_candidates", "meta"}
    assert result.concept_count == 2 and result.illustration_count == 1
    meta = payload["meta"]
    assert meta["model"] == "fake-model"
    # concepts-v2（2026-10-08）：规则尾提到材料正文之前（正文块挪到 user 末尾，前缀缓存）→ 版本号跟着升
    assert meta["prompt_version"] == "concepts-v2"
    assert meta["source_id"] == "sample" and meta["paragraph_count"] == 6
    assert meta["repairs"] == [] and meta["generated_at"]
    # 记账（PLAN-llm-tools §3.4）：逐次尝试 + 整次调用一条汇总；原文只进 concepts.raw.txt，绝不进 meta
    assert [item["try"] for item in meta["attempts"]] == [1]
    assert meta["attempts"][0]["tool"] == "analyze_concepts"
    assert meta["attempts"][0]["code"] is None
    assert meta["retry_account"] == {
        "tool": "analyze_concepts",
        "requests": 1,
        "retries": 0,
        "codes": [],
    }
    assert "raw_text" not in meta
    for concept in payload["concepts"]:
        assert set(concept) == {
            "id",
            "name",
            "surface",
            "prereq",
            "difficulty_hint",
            "paragraphs",
            "domain",
            "concept_key",
        }
    assert check_inventory_file(dirs.data, "sample") == []


def test_run_analyze_survives_messy_model_output(dirs: SimpleNamespace, sample_md: Path, fake_client) -> None:
    """模型输出有野引用时：过滤 + 记账（可见），落盘文件仍然满足契约。"""
    ingest(sample_md, "sample", dirs.data)
    result = run_analyze("sample", dirs.data, fake_client(text=f"```json\n{MESSY_JSON}\n```"))
    assert result.repairs
    assert check_inventory_file(dirs.data, "sample") == []
    payload = json.loads((dirs.data / "sample" / "concepts.json").read_text(encoding="utf-8"))
    assert payload["meta"]["repairs"]


def test_run_analyze_non_json_output_degrades_visibly(dirs: SimpleNamespace, sample_md: Path, fake_client) -> None:
    ingest(sample_md, "sample", dirs.data)
    with pytest.raises(ModelUnavailable, match="无画像"):
        run_analyze("sample", dirs.data, fake_client(text="这不是 JSON，抱歉"))
    raw = dirs.data / "sample" / "concepts.raw.txt"
    assert raw.read_text(encoding="utf-8") == "这不是 JSON，抱歉"
    assert not (dirs.data / "sample" / "concepts.json").exists()


def test_run_analyze_prompt_is_byte_identical_to_the_direct_builder(
    dirs: SimpleNamespace, sample_md: Path, fake_client
) -> None:
    """提示词**逐字不变**：迁移后发出的 messages == 迁移前 ``build_concepts_messages(meta)`` 的结果。

    工具层只拿到契约形态的段落表（``id`` / ``text``），所以这里连原始 ``meta``（含 ``index`` /
    ``text_hash``）一起比——模板本来只读那两个字段，多出来的键一个字符都不该进提示词。
    """
    from ar.jsonio import read_json
    from ar.prompts import build_concepts_messages

    ingest(sample_md, "sample", dirs.data)
    fake = fake_client(text=GOOD_JSON)
    run_analyze("sample", dirs.data, fake)

    meta = read_json(dirs.data / "sample" / "source.meta.json")
    assert fake.calls[0] == build_concepts_messages(meta)


def test_run_analyze_requires_ingest_first(dirs: SimpleNamespace, fake_client) -> None:
    with pytest.raises(InputError, match="先运行"):
        run_analyze("nosuch", dirs.data, fake_client(text=GOOD_JSON))


# ------------------------------------------------------------ 反向验证：重试账 --
def test_run_analyze_retries_a_server_failure_and_records_both_attempts(
    dirs: SimpleNamespace, sample_md: Path
) -> None:
    """反向验证：第一次 500（SERVER，可重试）、第二次成功 → ``attempts`` 两条、第二条成功。

    同时断言产物**没被写坏**：``attempts`` 记在 meta 里，``concepts.json`` 照样过契约校验，
    成功路径不落 ``concepts.raw.txt``（原文只在解析失败时落盘）。
    """
    ingest(sample_md, "sample", dirs.data)
    client = _ScriptedClient(
        [
            LlmResult(ok=False, failure=LlmFailure(code="SERVER", message="HTTP 503：网关抖动", status=503)),
            GOOD_JSON,
        ]
    )
    result = run_analyze("sample", dirs.data, client)

    assert client.calls == 2, "第一次失败就该再试一次（SERVER 在白名单里）"
    assert result.concept_count == 2

    payload = json.loads((dirs.data / "sample" / "concepts.json").read_text(encoding="utf-8"))
    attempts = payload["meta"]["attempts"]
    assert [item["try"] for item in attempts] == [1, 2], attempts
    assert attempts[0]["code"] == "SERVER" and attempts[0]["delay_ms"] == 0
    assert attempts[1]["code"] is None, "第二条必须是成功的那次"
    assert payload["meta"]["retry_account"] == {
        "tool": "analyze_concepts",
        "requests": 2,
        "retries": 1,
        "codes": ["SERVER"],
    }
    # 产物没被写坏：契约校验通过、原文没有混进 meta、成功路径不留 raw 排查文件
    assert check_inventory_file(dirs.data, "sample") == []
    assert "raw_text" not in payload["meta"] and "HTTP 503" not in json.dumps(payload, ensure_ascii=False)
    assert not (dirs.data / "sample" / "concepts.raw.txt").exists()


def test_run_analyze_deterministic_failure_tries_once_and_leaves_no_raw_file(
    dirs: SimpleNamespace, sample_md: Path
) -> None:
    """确定性错误（401 → ``AUTH``）只试一次，且与迁移前一致：**不落** ``concepts.raw.txt``。"""
    ingest(sample_md, "sample", dirs.data)
    client = _ScriptedClient(
        [LlmResult(ok=False, failure=LlmFailure(code="AUTH", message="HTTP 401：密钥无效", status=401))]
    )
    with pytest.raises(ModelUnavailable, match="无画像"):
        run_analyze("sample", dirs.data, client)

    assert client.calls == 1, "AUTH 不在白名单里：不许白花第二次调用"
    assert not (dirs.data / "sample" / "concepts.json").exists()
    assert not (dirs.data / "sample" / "concepts.raw.txt").exists(), "调用失败没有原文可留"


def test_validate_failure_does_not_write_file(dirs: SimpleNamespace, sample_md: Path, fake_client, monkeypatch) -> None:
    """校验不过就不写盘（绝不落一个坏产物）。"""
    ingest(sample_md, "sample", dirs.data)
    monkeypatch.setattr("ar.analyze.validate_inventory", lambda inventory, meta: ["c01: 人为制造的校验失败"])
    with pytest.raises(ValidationFailed, match="人为制造的校验失败"):
        run_analyze("sample", dirs.data, fake_client(text=GOOD_JSON))
    assert not (dirs.data / "sample" / "concepts.json").exists()


# ---------------------------------------------------------------- CLI 连线 --
def test_cli_analyze_end_to_end_with_fake_model(
    run_cli, dirs: SimpleNamespace, sample_md: Path, fake_client, monkeypatch
) -> None:
    ingest(sample_md, "sample", dirs.data)
    monkeypatch.setenv("AR_API_KEY", "sk-" + "f" * 40)
    monkeypatch.setattr("ar.cli._make_client", lambda config: fake_client(text=GOOD_JSON))
    code, output = run_cli("--data-dir", str(dirs.data), "analyze", "sample")
    assert code == EXIT_OK, output
    assert "概念 2 条" in output
    # concepts-v2（2026-10-08）：提示词重排（规则尾前移）后版本号升到 v2
    assert "concepts-v2" in output

    code, output = run_cli("--data-dir", str(dirs.data), "analyze", "sample", "--check-only")
    assert code == EXIT_OK, output
    assert "通过契约校验" in output


def test_cli_analyze_without_model_exits_5(run_cli, dirs: SimpleNamespace, sample_md: Path, monkeypatch) -> None:
    ingest(sample_md, "sample", dirs.data)
    monkeypatch.delenv("AR_API_KEY", raising=False)
    code, output = run_cli(
        "--data-dir", str(dirs.data), "--config", str(dirs.tmp / "no-config.json"), "analyze", "sample"
    )
    assert code == EXIT_MODEL, output
    assert "AR_API_KEY" in output
    assert not (dirs.data / "sample" / "concepts.json").exists()


def test_negative_injected_dangling_ref_must_fail_check_only(
    run_cli, dirs: SimpleNamespace, sample_md: Path, fake_client
) -> None:
    """验收 4：把某条 paragraphs 改成不存在的 id → 校验必须失败并点名。"""
    ingest(sample_md, "sample", dirs.data)
    run_analyze("sample", dirs.data, fake_client(text=GOOD_JSON))

    # 只在临时副本上注入缺陷
    copy = dirs.tmp / "tampered-concepts"
    shutil.copytree(dirs.data / "sample", copy)
    inventory_path = copy / "concepts.json"
    original = inventory_path.read_text(encoding="utf-8")
    mutated = original.replace('"p001"', '"p404"', 1)
    assert mutated != original, "注入未生效（未找到可改之处）"
    inventory_path.write_text(mutated, encoding="utf-8")
    injected = json.loads(inventory_path.read_text(encoding="utf-8"))
    assert "p404" in injected["concepts"][0]["paragraphs"], "注入没有落到 paragraphs 上"

    code, output = run_cli("--data-dir", str(copy.parent), "analyze", copy.name, "--check-only")
    assert code == EXIT_VALIDATION, output
    assert "p404" in output and "c01" in output, output


def test_check_only_without_file_reports_actionable_error(run_cli, dirs: SimpleNamespace, sample_md: Path) -> None:
    ingest(sample_md, "sample", dirs.data)
    code, output = run_cli("--data-dir", str(dirs.data), "analyze", "sample", "--check-only")
    assert code == EXIT_VALIDATION, output
    assert "请先运行" in output
