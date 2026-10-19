"""夹具自身的契约自洽性（纯 Python，不启浏览器）。

守两条：
  1. `samples/<id>/doc.json` 的字段形状符合 PLAN-skeleton.md §4 的冻结 schema
     （2026-10-13 起含 PLAN-actions-feedback.md §4.1 的 `figure` / `figure_error`）；
  2. 锚点是**从底本真算出来的**，不是手抄的——`text_hash` 与段落文本都能由 `00-底本.md` 重算出来。

配图那几条照 §4.2 的后端校验口径写：mermaid 首词必须是图类型关键字、行数/字符数有上限；
SVG 必须能被 XML 解析且自包含（禁 `<script>` / `on*` / `foreignObject` / 外部 href）。
**夹具与 `/api/doc` 同形是纪律**，所以这些形状得由测试守住，而不是靠手写时小心。
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent                     # adaptive-reader/
FIXTURE = PROJECT / "samples" / "capital-v1-ch01"
SOURCE = PROJECT.parent / "学习" / "_第1章第1节三版对照" / "00-底本.md"

ACTION_ENUM = {"expand", "compress", "keep", "illustrate"}
ILLUSTRATION_KINDS = {"流程", "结构", "对比", "数量"}
FIGURE_KINDS = {"mermaid", "svg"}
# §14.1 的事实锚口径（唯一来源是 ar.fidelity.FACT_ANCHOR_POLICIES，这里照抄一份当契约）
FACT_ANCHOR_POLICIES = {"keep_all", "allow_declared_drops"}
# §4.2：mermaid 源码的首词必须是图类型关键字（首词就是指图类型，别拿 %% 注释开头）
MERMAID_KEYWORDS = {
    "flowchart", "graph", "sequenceDiagram", "stateDiagram-v2", "classDiagram",
    "erDiagram", "gantt", "pie", "mindmap", "timeline",
}
MERMAID_MAX_LINES = 40
MERMAID_MAX_CHARS = 2000
CAPTION_MAX_CHARS = 40
SVG_NS = "{http://www.w3.org/2000/svg}"


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
    # 夹具的 fidelity 是**形状样本**（手造样例，不是真跑过校验的产物）：口径 + 放行条目各留一个样本
    fidelity = doc["changes"]["fidelity"]
    assert fidelity["checked"] is True
    assert fidelity["mismatches"] == []
    assert fidelity["fact_anchor_policy"] in FACT_ANCHOR_POLICIES
    assert isinstance(fidelity["waived"], list)
    assert isinstance(doc["rewritten"], str)


def test_fact_anchor_declaration_sample_is_consistent_with_the_source(doc):
    """事实锚申报样本（§14.1）：申报的每一项都得「底本有、改写稿没有」，放行条目与它对得上。"""
    paragraphs = {p["id"]: p["text"] for p in doc["meta"]["paragraphs"]}
    blocks = [b for b in re.split(r"\n[ \t]*\n", doc["rewritten"]) if b.strip()]
    per_block = dict(zip(paragraphs, blocks))
    declared = [c for c in doc["changes"]["changes"] if "fact_declarations" in c]
    assert declared, "夹具要留一个「申报了放弃某项事实锚」的样本给前端跑申报渲染"
    declared_ids = {change["paragraph"] for change in declared}
    fidelity = doc["changes"]["fidelity"]
    waived = {row["paragraph"] for row in fidelity["waived"]}
    policy = fidelity["fact_anchor_policy"]
    for change in declared:
        pid = change["paragraph"]
        declaration = change["fact_declarations"]
        assert set(declaration) <= {"numbers", "quotes", "books", "note"}, declaration
        assert any(declaration.get(field) for field in ("numbers", "quotes", "books")), declaration
        for number in declaration.get("numbers") or []:
            assert number in paragraphs[pid], f"{pid} 申报的数字 {number!r} 在底本里没有"
            assert number not in per_block[pid], f"{pid} 申报放弃了 {number!r}，改写稿里却还有它"
        if policy == "allow_declared_drops":
            assert pid in waived, f"{pid} 的申报在松口径下应当被放行（fidelity.waived 里要有它）"
        else:
            assert pid not in waived, f"{pid} 在严口径下不许出现在 waive 名单里"
    assert waived <= declared_ids, "放行条目必须来自一条申报"


def test_profile_refs_filled_sample_is_well_formed(doc):
    """兜底补齐样本（2026-10-14）：夹具要留一个「这一段 `why.profile` 是骨架照读者自评补的」样本。

    形状守约（照 `figure` / `deferred` 的「为真才出现」先例）：**只在为真时出现**、值必须是 `true`，
    且那一段确实带着引用——补齐出来的引用不可能是空数组（空数组 = 读者本来就没有自评可引用）。
    """
    rows = [c for c in doc["changes"]["changes"] if "profile_refs_filled" in c]
    assert len(rows) == 1, "夹具要留一个样本给前端跑「兜底补齐」那条渲染分支"
    change = rows[0]
    assert change["profile_refs_filled"] is True
    assert change["why"]["profile"], "补齐出来的引用不可能是空数组"


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
        # 2026-10-13 起 compress / illustrate 都实现了：夹具里不再有 `deferred`
        # （`deferred` 机制留在 schema 里给将来的枚举成员，页面仍在渲染那一行——由
        #  test_reader_ui.py::test_deferred_note_still_shows_for_a_future_unimplemented_action 守）
        assert "deferred" not in change, change
        # 图只在**成功**时出现（照 `deferred` 的「为真才出现」先例）：有图 ⇒ 动作就是 illustrate
        assert ("figure" in change) == (change["action"] == "illustrate"), change
        if "figure_error" in change:
            # §4.1：配图失败时正文照旧（动作记 keep），失败原因一句话
            assert change["action"] == "keep", change
            assert "figure" not in change, change
            assert isinstance(change["figure_error"], str) and change["figure_error"].strip()


def test_actions_are_consistent_with_length_ratio(doc):
    """手写 action 与长度比一致（比例带 [0.85,1.15]；不足 60 字符的结构短块不受约束）。

    **配图段例外**：图不进 `rewritten.md`（§1 第 2 条），illustrate 段（以及「想配图但失败」
    退回 keep 的那一段）正文与底本**逐字节相同**，比例带对它不适用——这里直接断言这条更硬的口径。
    """
    paragraphs = doc["meta"]["paragraphs"]
    blocks = [b for b in re.split(r"\n[ \t]*\n", doc["rewritten"]) if b.strip()]
    for paragraph, block, change in zip(paragraphs, blocks, doc["changes"]["changes"]):
        if change["action"] == "illustrate" or "figure_error" in change:
            assert block == paragraph["text"], \
                f"{paragraph['id']} 是配图段：正文必须与底本逐字节相同（图不进 md）"
            continue
        src_len = len(paragraph["text"])
        if src_len < 60:
            continue
        ratio = len(block) / src_len
        expected = "expand" if ratio > 1.15 else ("compress" if ratio < 0.85 else "keep")
        assert change["action"] == expected, f"{paragraph['id']} 比例 {ratio:.2f} 与 {change['action']} 不符"


def svg_root(source: str) -> ET.Element:
    """解析 SVG 源码（`§4.2` 用的就是标准库 ElementTree，这里同一口径）。"""
    return ET.fromstring(source)


def test_figure_samples_are_inline_and_pass_the_backend_rules(doc):
    """配图样本的形状（§4.1 / §4.2）：内联在 changes 里、两个 kind 都有样本、源码过白名单。"""
    changes = doc["changes"]["changes"]
    figures = [c for c in changes if "figure" in c]
    assert {c["paragraph"] for c in figures} == {"p013", "p024"}, \
        "夹具要同时给出 mermaid 与 svg 两条渲染分支的样本"
    assert {c["figure"]["kind"] for c in figures} == FIGURE_KINDS

    for change in figures:
        figure = change["figure"]
        assert figure["kind"] in FIGURE_KINDS, figure
        assert set(figure) <= {"kind", "source", "caption"}, figure   # §4.1 只有这三个键
        source = figure["source"]
        assert isinstance(source, str) and source.strip()
        assert len(source) <= MERMAID_MAX_CHARS, change["paragraph"]
        caption = figure.get("caption")
        if caption is not None:
            assert isinstance(caption, str) and 0 < len(caption) <= CAPTION_MAX_CHARS, caption

        if figure["kind"] == "mermaid":
            assert "```" not in source, "mermaid 源码里不许带 ``` 围栏"
            assert len(source.splitlines()) <= MERMAID_MAX_LINES, source
            assert source.split()[0] in MERMAID_KEYWORDS, \
                f"mermaid 源码首词必须是图类型关键字：{source.split()[0]!r}"
        else:
            root = svg_root(source)
            assert root.tag == f"{SVG_NS}svg", root.tag
            for node in root.iter():
                local = node.tag.split("}")[-1]
                assert local not in {"script", "foreignObject"}, node.tag
                for name, value in node.attrib.items():
                    attr = name.split("}")[-1].lower()
                    assert not attr.startswith("on"), (name, value)
                    if attr == "href":
                        assert str(value).startswith("#"), f"SVG 不许引用外部资源：{value}"


def test_figure_error_sample_is_a_failed_illustration(doc):
    """配图失败样本（§4.1）：动作记 keep、正文照旧、给一句 `figure_error`，且没有 `figure`。"""
    failed = [c for c in doc["changes"]["changes"] if "figure_error" in c]
    assert len(failed) == 1, "夹具要留一个「想配图但过不了校验」的样本给前端跑退回文字那条路"
    change = failed[0]
    assert change["action"] == "keep"
    assert isinstance(change["figure_error"], str) and change["figure_error"].strip()
    assert "figure" not in change
    # 这条样本也得在配图候选里（说明当初为什么想给它配图）
    candidates = {c["paragraph"] for c in doc["concepts"]["illustration_candidates"]}
    assert change["paragraph"] in candidates


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
