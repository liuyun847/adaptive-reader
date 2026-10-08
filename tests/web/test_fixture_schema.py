"""夹具自身的契约自洽性（纯 Python，不启浏览器）。

守两条：
  1. `samples/<id>/doc.json` 的字段形状符合 PLAN-skeleton.md §4 的冻结 schema；
  2. 锚点是**从底本真算出来的**，不是手抄的——`text_hash` 与段落文本都能由 `00-底本.md` 重算出来。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent                     # adaptive-reader/
FIXTURE = PROJECT / "samples" / "capital-v1-ch01"
SOURCE = PROJECT.parent / "学习" / "_第1章第1节三版对照" / "00-底本.md"

ACTION_ENUM = {"expand", "compress", "keep", "illustrate"}
ILLUSTRATION_KINDS = {"流程", "结构", "对比", "数量"}


def normalize(text: str) -> str:
    """契约 §4：去首尾空白 + 合并连续空白 + 全角空格转半角。"""
    return re.sub(r"\s+", " ", text.replace("\u3000", " ")).strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:8]


def split_paragraphs(raw: str) -> list[str]:
    blocks = re.split(r"\n[ \t]*\n", raw.replace("\r\n", "\n").replace("\r", "\n"))
    return [b.strip() for b in blocks if b.strip()]


@pytest.fixture(scope="module")
def doc() -> dict:
    return json.loads((FIXTURE / "doc.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def explain() -> dict:
    return json.loads((FIXTURE / "explain.json").read_text(encoding="utf-8"))


def test_doc_has_frozen_top_level_shape(doc):
    assert set(doc) == {"meta", "rewritten", "changes", "concepts"}
    assert set(doc["meta"]) == {"source_id", "ingest", "paragraphs"}
    assert set(doc["meta"]["ingest"]) == {"kind", "path", "hash", "at"}
    assert doc["meta"]["ingest"]["kind"] == "local_file"
    assert set(doc["changes"]) == {"changes", "fidelity"}
    assert doc["changes"]["fidelity"] == {"checked": True, "mismatches": []}
    assert isinstance(doc["rewritten"], str)


def test_paragraph_ids_are_three_digit_and_sequential(doc):
    ids = [p["id"] for p in doc["meta"]["paragraphs"]]
    assert ids == [f"p{i:03d}" for i in range(1, len(ids) + 1)]
    assert [p["index"] for p in doc["meta"]["paragraphs"]] == list(range(1, len(ids) + 1))


def test_text_hash_is_recomputed_from_source_file(doc):
    """锚点必须能由底本重算：段落文本 = 底本空行切分，text_hash = sha256(规范化)[:8]。"""
    source_paragraphs = split_paragraphs(SOURCE.read_text(encoding="utf-8"))
    fixture_paragraphs = doc["meta"]["paragraphs"]
    assert [p["text"] for p in fixture_paragraphs] == source_paragraphs
    for paragraph in fixture_paragraphs:
        assert paragraph["text_hash"] == text_hash(paragraph["text"]), paragraph["id"]


def test_rewritten_paragraph_count_matches_source(doc):
    """M3：改写稿段数与段序必须与源一致（前端就是按这个把改写段对到原文段的）。"""
    blocks = [b for b in re.split(r"\n[ \t]*\n", doc["rewritten"]) if b.strip()]
    assert len(blocks) == len(doc["meta"]["paragraphs"])


def test_every_paragraph_has_a_change_record_with_why(doc):
    changes = doc["changes"]["changes"]
    paragraphs = [p["id"] for p in doc["meta"]["paragraphs"]]
    assert [c["paragraph"] for c in changes] == paragraphs
    for change in changes:
        assert change["action"] in ACTION_ENUM, change
        assert isinstance(change["why"]["note"], str) and change["why"]["note"].strip()
        assert isinstance(change["why"]["profile"], list)
        assert 0.0 <= change["confidence"] <= 1.0
        assert set(change["facts"]) == {"numbers", "quotes"}
        # 骨架只实现 expand/keep；compress/illustrate 必须显式标 deferred
        assert ("deferred" in change) == (change["action"] in {"compress", "illustrate"})


def test_actions_are_consistent_with_length_ratio(doc):
    """手写 action 与长度比一致（比例带 [0.85,1.15]；不足 60 字符的结构短块不受约束）。"""
    paragraphs = doc["meta"]["paragraphs"]
    blocks = [b for b in re.split(r"\n[ \t]*\n", doc["rewritten"]) if b.strip()]
    for paragraph, block, change in zip(paragraphs, blocks, doc["changes"]["changes"]):
        src_len = len(paragraph["text"])
        if src_len < 60:
            continue
        ratio = len(block) / src_len
        expected = "expand" if ratio > 1.15 else ("compress" if ratio < 0.85 else "keep")
        assert change["action"] == expected, f"{paragraph['id']} 比例 {ratio:.2f} 与 {change['action']} 不符"


def test_concepts_reference_existing_paragraphs(doc):
    paragraph_ids = {p["id"] for p in doc["meta"]["paragraphs"]}
    concepts = doc["concepts"]["concepts"]
    assert concepts, "夹具至少要有几个概念，否则卡片里看不到概念区"
    by_id = {c["id"]: c for c in concepts}
    for concept in concepts:
        assert concept["paragraphs"], concept["id"]
        assert set(concept["paragraphs"]) <= paragraph_ids, concept["id"]
        assert set(concept["prereq"]) <= set(by_id), concept["id"]
        assert concept["difficulty_hint"] in (1, 2, 3, 4, 5)
    for candidate in doc["concepts"]["illustration_candidates"]:
        assert candidate["paragraph"] in paragraph_ids
        assert candidate["kind"] in ILLUSTRATION_KINDS


def test_explain_fixture_is_a_response_body(explain):
    assert explain["degraded"] is False
    assert isinstance(explain["text"], str) and len(explain["text"]) > 40
    # 夹具只有一份讲解，用 paragraph / kind 说明它对应哪一段、哪种 kind
    # （这两个键是**夹具专用**的信息性字段，后端 /api/explain 不返回；前端会据此显示提示）
    assert explain["paragraph"] == "p022"
    assert explain["kind"] == "simpler"


def test_explain_fixture_matches_the_real_response_shape(explain):
    """夹具必须长成**真实响应**的样子（口径以后端 `server.post_explain` 为准）。

    2026-10-08 复核修的两处：
      - `selection_from` 是**请求**字段，不属于响应（曾经被写进夹具，还被当契约断言）；
      - `cached` / `selection_truncated` **为真才出现**，后端从不返回 `false` 版本。
    另外 `scope` 每次响应都给（片段 / 整段作用域），这里也得在。
    """
    assert explain["scope"] in {"selection", "paragraph"}
    assert "selection_from" not in explain, "selection_from 是请求字段，不该出现在响应夹具里"
    assert "cached" not in explain, "cached=false 不是后端会返回的形状（为真才出现）"
    assert "selection_truncated" not in explain, "同上：selection_truncated 只在被截断时出现"
    # 夹具是手造样例，不冒充模型产出：体现「这次产出了什么」的键一个都没有
    assert "model" not in explain and "prompt_version" not in explain
    assert "reason" not in explain, "reason 只在降级响应里出现"


def test_fixture_is_declared_handmade():
    """样本目录必须写清楚这是手造夹具，不许被当成模型产出。"""
    readme = (PROJECT / "samples" / "README.md").read_text(encoding="utf-8")
    assert "手造样例，不是模型产出" in readme
