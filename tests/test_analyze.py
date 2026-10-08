"""M2：概念清单抽取与契约校验（含负向对照）。

负向用例照 ``学习/_整理/selfcheck.py`` 的做法：只在**临时副本**上注入缺陷、
断言注入确实生效、然后看退出码。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from ar.analyze import (
    ILLUSTRATION_KINDS,
    check_inventory_file,
    extract_json_object,
    normalize_inventory,
    run_analyze,
    validate_inventory,
)
from ar.errors import EXIT_MODEL, EXIT_OK, EXIT_VALIDATION, InputError, ModelUnavailable, ValidationFailed
from ar.ingest import ingest

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


# -------------------------------------------------------------------- 主流程 --
def test_run_analyze_writes_contract_file_and_passes_check(dirs: SimpleNamespace, sample_md: Path, fake_client) -> None:
    ingest(sample_md, "sample", dirs.data)
    result = run_analyze("sample", dirs.data, fake_client(text=GOOD_JSON))

    payload = json.loads((dirs.data / "sample" / "concepts.json").read_text(encoding="utf-8"))
    assert set(payload) == {"concepts", "illustration_candidates", "meta"}
    assert result.concept_count == 2 and result.illustration_count == 1
    meta = payload["meta"]
    assert meta["model"] == "fake-model"
    assert meta["prompt_version"] == "concepts-v1"
    assert meta["source_id"] == "sample" and meta["paragraph_count"] == 6
    assert meta["repairs"] == [] and meta["generated_at"]
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


def test_run_analyze_requires_ingest_first(dirs: SimpleNamespace, fake_client) -> None:
    with pytest.raises(InputError, match="先运行"):
        run_analyze("nosuch", dirs.data, fake_client(text=GOOD_JSON))


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
    assert "concepts-v1" in output

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
