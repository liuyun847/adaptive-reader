"""读前背景问卷（PLAN-background-survey §12「后端第 1–8 条」）。

覆盖：

1. 题目定义自洽：枚举闭合、条件题规则可复算、版本号存在；
2. 素描渲染：同画像同文本（逐字）、空项措辞固定、跳过与部分作答两种措辞分得开、
   ≤400 字符整项丢弃 + 「已省略 N 项」；
3. 画像读写：写入后读回形状正确、``version`` 保留、老画像（无 ``background``）读取不报错；
4. 答案校验：枚举外 / 六选三不是 3 项 / 超长文本截断标记；
5. 追问端点：模型失败 → 200 + ``degraded``，且**固定题答案不丢**；
6. 提示词布局：素描在固定块、首个差异字符仍在段号位、无时间戳、规则措辞未变；
7. HTTP 三个端点的契约形状（§8）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from ar.config import Config
from ar.errors import InputError
from ar.jsonio import read_json
from ar.prompts import REWRITE_USER_TEMPLATE, build_survey_messages
from ar.server import build_app
from ar.survey import (
    DROPPABLE_ORDER,
    EMPTY_BACKGROUND,
    MAX_SKETCH_CHARS,
    QUESTIONS,
    SKIPPED_TEXT,
    SURVEY_VERSION,
    ANSWER_SLOTS,
    QUESTION_BY_ID,
    answers_of,
    asurvey_followups,
    background_of,
    condition_rules,
    effective_answers,
    enum_labels,
    field_value,
    finalize_background,
    has_background,
    has_fixed_answers,
    merge_followup_answers,
    normalize_background,
    question_applies,
    render_sketch,
    rule_holds,
    save_background,
    sketch_fingerprint,
    survey_record_path,
    validate_answers,
    visible_questions,
)

#: 一份「六题都答了」的背景（形状照契约 §6.1：大学两项**嵌套**在 ``university`` 下）
FULL_BACKGROUND = {
    "version": 1,
    "answered_at": "2026-10-08T16:40:00+08:00",
    "education": "undergraduate",
    "high_school_electives": ["physics", "chemistry", "biology"],
    "university": {"discipline": "economics", "major": "国际经济与贸易"},
    "strengths": ["economics_finance", "programming_computer"],
    "strengths_note": "读过《资本论》第一卷前两章",
    "reading_materials": ["textbook", "papers"],
    "preference": "prerequisites",
    "followups": [{"q": "读过《资本论》原文吗？", "a": "读过前两章", "at": "2026-10-08T16:45:00+08:00"}],
}
#: 同一份作答的**扁平**形态（请求体与 ``GET /api/survey`` 的 ``answers`` 都是这一形态）
FULL_BACKGROUND_FLAT = {
    key: value for key, value in FULL_BACKGROUND.items() if key != "university"
} | {"discipline": "economics", "major": "国际经济与贸易"}
#: 未作答项在素描里的固定措辞
NOT_PROVIDED = "未提供："
#: 提交用的扁平作答（``version`` / ``answered_at`` / ``followups`` 由服务端管，不进请求体）
SUBMITTED_ANSWERS = {
    key: value
    for key, value in FULL_BACKGROUND_FLAT.items()
    if key not in ("version", "answered_at", "followups")
}


@dataclass
class _ConfiguredClient:
    """把假模型包一层，补上 ``configured``（``FakeChatClient`` 没有这个字段）。

    ``ar.survey.asurvey_followups`` / ``ar.explain.aexplain`` 都按 ``configured`` 判「模型可用」，
    而 HTTP 层的 ``client_factory`` 拿到的就是这一层；生产实现 ``ModelClient`` 自带该属性。
    模型调用经工具层走统一调用层，所以还要把 ``arequest_once`` / ``retry_policy`` 转发下去。
    """

    inner: Any
    configured: bool = True

    def chat(self, messages: Any, **kwargs: Any) -> Any:
        return self.inner.chat(messages, **kwargs)

    async def achat(self, messages: Any, **kwargs: Any) -> Any:
        return await self.inner.achat(messages, **kwargs)

    async def arequest_once(self, request: Any) -> Any:
        return await self.inner.arequest_once(request)

    def retry_policy(self) -> Any:
        return self.inner.retry_policy()


def configured(fake: Any) -> _ConfiguredClient:
    return _ConfiguredClient(inner=fake)


def make_client(
    dirs: SimpleNamespace,
    *,
    api_key: str = "",
    client_factory=None,
) -> TestClient:
    """临时 data / profiles / web 目录上的应用（模型默认未配置 = 不发真实请求）。"""
    config = Config(
        base_url="https://gateway.invalid/provider/v1",
        model="fake/model",
        api_key=api_key,
        api_key_origin="test:AR_API_KEY" if api_key else None,
    )
    app = build_app(
        data_dir=dirs.data,
        profiles_dir=dirs.profiles,
        web_dir=dirs.web,
        config=config,
        reader="local-default",
        client_factory=client_factory,
    )
    return TestClient(app)


# --------------------------------------------------- 1. 题目定义自洽（§12.1） --
def test_question_enum_values_are_closed_and_unique() -> None:
    """枚举闭合：每题至少两个选项、value 与 label 都不重复、label 非空。"""
    assert SURVEY_VERSION == 1, "问卷版本号必须存在且是整数（题目或选项一变就升版）"
    assert [q["id"] for q in QUESTIONS] == ["Q1", "Q2", "Q3a", "Q3b", "Q4", "Q4b", "Q5", "Q6"]
    for question in QUESTIONS:
        qid = question["id"]
        assert question["kind"] in ("single", "multi", "text", "textarea"), qid
        assert question["answer_slot"], qid
        values = [item["value"] for item in question["options"]]
        labels = [item["label"] for item in question["options"]]
        if question["kind"] in ("text", "textarea"):
            assert values == [] and question["max_chars"] > 0, f"{qid} 是自由文本，只该有长度上限"
        else:
            assert len(values) >= 2, f"{qid} 的选项太少"
            assert len(set(values)) == len(values), f"{qid} 的枚举值有重复"
            assert len(set(labels)) == len(labels), f"{qid} 的中文标签有重复"
            assert all(value and label for value, label in zip(values, labels)), qid
        assert enum_labels(qid) == {item["value"]: item["label"] for item in question["options"]}


def test_question_payload_carries_the_frontend_view() -> None:
    """题目自带前端渲染需要的等价字段：``qid`` / ``field`` / ``label`` / 条件 / 校验约束。

    前端（``web/js/survey.js``）按这些键渲染与校验；两边同源，就不该靠前端自己的兜底题目。
    ``field`` 必须是后端认识的作答字段名（否则前端提交会被 400 打回）。
    """
    from ar.survey import ANSWER_SLOTS

    valid_fields = set(ANSWER_SLOTS.values())
    for question in QUESTIONS:
        qid = question["id"]
        assert question["qid"] == qid
        assert question["field"] in valid_fields, qid
        assert question["label"] == question["title"] and question["label"]
        assert "questions" not in question  # 不要把整张题表塞进题目对象

    by_slot = {question["field"]: question for question in QUESTIONS}
    assert by_slot["high_school_electives"]["exactly"] == 3
    assert set(by_slot["high_school_electives"]["exclusive"]) == {"not_applicable", "forgotten"}
    assert by_slot["strengths"]["exclusive"] == ["none"]
    assert by_slot["reading_materials"]["exclusive"] == ["rarely"]
    assert by_slot["major"]["maxLength"] == 60 and by_slot["major"]["kind"] == "text"
    assert by_slot["strengths_note"]["maxLength"] == 200 and by_slot["strengths_note"]["kind"] == "textarea"
    # 没有约束的题也给等价默认值（``0`` / ``[]``），前端不必区分「缺键」与「没约束」
    assert by_slot["education"]["exactly"] == 0 and by_slot["education"]["atLeast"] == 0
    assert by_slot["education"]["exclusive"] == []

    # 条件也给了前端可直接求值的等价形态（``requires`` 仍是权威）
    assert by_slot["education"]["when"] == {"kind": "always"}
    assert by_slot["high_school_electives"]["when"] == {
        "kind": "equals",
        "field": "education",
        "values": ["high_school", "college", "undergraduate", "master", "doctor"],
    }
    assert by_slot["major"]["when"]["field"] == "education"
    assert by_slot["major"]["alsoWhen"]["kind"] == "notEquals"
    assert by_slot["major"]["alsoWhen"]["includeEmpty"] is True


def test_backend_questions_supply_every_key_the_frontend_reads() -> None:
    """后端题目必须喂饱前端 ``renderQuestions`` 读的那批键（同一份 payload，两边不各写一份题表）。

    这条守的是前后端接口：前端在 ``web/js/survey.js`` 的归一化块里按 ``raw.<key>`` 取字段，
    这里把那批键读出来逐个对照后端 payload —— 少一个键，前端就退回自己的兜底题目（内容可能与后端不一致）。
    """
    script = Path(__file__).resolve().parents[1] / "web" / "js" / "survey.js"
    if not script.is_file():
        pytest.skip(f"前端文件不存在：{script}")
    source = script.read_text(encoding="utf-8")
    # 前端归一化那段：``...map((raw) => {...})`` 到 ``__surveyQuestions = normalized`` 之间
    match = re.search(r"\.map\(\(raw\)\s*=>\s*\{", source)
    end_marker = source.find("__surveyQuestions = normalized")
    assert match is not None and end_marker > match.start(), "前端归一化块的形状变了，请同步这条守卫"
    block = source[match.start():end_marker]
    # ``raw.<key>`` 只取完整标识符：``raw.answer_slot`` 这类「另一个名字」不算独立键
    keys = {key for key in re.findall(r"raw\.([A-Za-z_][A-Za-z0-9_]*)", block) if "_" not in key}
    assert keys, "前端归一化块里读不到 raw.<key>，请同步这条守卫"

    #: 前端兜底会读的键（可能是「二选一」的两个名字，也可能它自己给默认值）
    alternatives = {
        "qid": ("qid", "id"),
        "label": ("label", "title", "qid"),
        "field": ("field", "answer_slot", "qid"),
    }
    allowed_missing = {"alsoWhen", "atLeast", "placeholder", "hint", "when"}
    for question in QUESTIONS:
        for key in sorted(keys - allowed_missing):
            names = alternatives.get(key, (key,))
            assert any(name in question for name in names), f"{question['id']} 少了前端要的键：{key}"
    # 兜底读的两个「二选一」名字两边都得给全，免得前端内部实现一改就悄悄退化
    assert {"qid", "id", "field", "label", "kind", "options"} <= set(QUESTIONS[0])


def test_conditional_questions_follow_the_contract_rules() -> None:
    """条件题规则可复算（后端独立算，不信任前端）：Q2 看 Q1，Q3a 看 Q1，Q3b 再看 Q3a。"""
    assert visible_questions({}) == ["Q1", "Q4", "Q4b", "Q5", "Q6"]
    assert visible_questions({"education": "junior_or_below"}) == ["Q1", "Q4", "Q4b", "Q5", "Q6"]
    assert visible_questions({"education": "prefer_not"}) == ["Q1", "Q4", "Q4b", "Q5", "Q6"]

    high_school = visible_questions({"education": "high_school"})
    assert "Q2" in high_school and "Q3a" not in high_school and "Q3b" not in high_school
    undergraduate = visible_questions({"education": "undergraduate"})
    assert undergraduate == ["Q1", "Q2", "Q3a", "Q3b", "Q4", "Q4b", "Q5", "Q6"]
    # Q3a 选「没读大学 / 不确定」时 Q3b 不出现
    assert "Q3b" not in visible_questions({"education": "undergraduate", "discipline": "none"})
    assert "Q3b" not in visible_questions({"education": "undergraduate", "discipline": "unsure"})
    assert "Q3b" in visible_questions({"education": "undergraduate", "discipline": "engineering"})


def test_question_definition_matches_the_contract_table() -> None:
    """契约 §3 的题号 / 维度 / 取值逐条对得上（少一个选项就是漏做）。"""
    assert enum_labels("Q1") == {
        "junior_or_below": "初中及以下",
        "high_school": "高中（含中专、职高）",
        "college": "大专",
        "undergraduate": "本科",
        "master": "硕士",
        "doctor": "博士",
        "prefer_not": "不想说",
    }
    assert list(enum_labels("Q2")) == [
        "physics",
        "chemistry",
        "biology",
        "politics",
        "history",
        "geography",
        "not_applicable",
        "forgotten",
    ]
    assert list(enum_labels("Q3a")) == [
        "philosophy",
        "economics",
        "law",
        "education",
        "literature",
        "history",
        "science",
        "engineering",
        "agriculture",
        "medicine",
        "management",
        "arts",
        "military_science",
        "interdisciplinary",
        "none",
        "unsure",
    ]
    assert len(enum_labels("Q4")) == 13 and enum_labels("Q4")["none"] == "都没有"
    assert len(enum_labels("Q5")) == 7 and enum_labels("Q5")["rarely"] == "很少读长文"
    assert list(enum_labels("Q6")) == [
        "conclusion_first",
        "prerequisites",
        "example",
        "simpler_wording",
        "unsure",
    ]


# ------------------------------------------------- 2. 素描渲染（§12.2） --
def test_sketch_is_byte_stable_for_the_same_background() -> None:
    """逐字稳定：同画像必得同文本（前缀缓存与回归基线都靠这条）。"""
    first = render_sketch(FULL_BACKGROUND)
    second = render_sketch(dict(reversed(list(FULL_BACKGROUND.items()))))
    assert first == second
    assert first == render_sketch(FULL_BACKGROUND)
    assert sketch_fingerprint(first) == sketch_fingerprint(second)
    # 渲染顺序照契约 §5.1：学历 → 学科门类 → 专业 → 六选三 → 自报领域 → 常读材料 → 偏好讲法
    # → 补充说明 → 追问摘要（追问摘要永远排最后）
    order = [
        "本科",
        "大学为经济学门类（专业：国际经济与贸易）",
        "高中选考物理、化学、生物",
        "自报熟悉领域：经济与金融、编程与计算机",
        "常读：教材课本、学术论文",
        "遇到看不懂时希望：补前置概念",
        "补充：读过《资本论》第一卷前两章",
        "读过《资本论》原文吗？→读过前两章",
    ]
    positions = [first.index(item) for item in order]
    assert positions == sorted(positions), first
    assert first.endswith("读过前两章。"), "追问摘要排在最后（契约 §5 的字段顺序）"
    # 时间戳 / 段号 / 随机量一律不许进素描（否则每轮都建新缓存）
    assert "2026-10-08" not in first and "answered_at" not in first
    assert not re.search(r"\d{4}-\d{2}-\d{2}T", first)
    assert not re.search(r"p\d{3}", first)
    assert first.endswith("。")


def test_sketch_uses_fixed_wording_for_skipped_and_partial_answers() -> None:
    """跳过 / 完全没答 / 部分作答三种措辞分得开，空项逐项写「未提供：X」。

    素描**不带**「读者背景：」前缀：提示词模板那一行已经写了，带前缀会拼出两行同样的字。
    """
    assert render_sketch({}) == "未提供"
    assert render_sketch(None) == "未提供"
    assert render_sketch({"version": 1}) == "未提供"
    assert render_sketch({"version": 1, "skipped": True}) == SKIPPED_TEXT
    assert SKIPPED_TEXT == "未提供（读者跳过了背景问卷）"
    assert "读者背景：" not in EMPTY_BACKGROUND and "读者背景：" not in SKIPPED_TEXT

    partial = render_sketch({"version": 1, "education": "undergraduate"})
    assert partial.startswith("本科；")
    assert partial.count(NOT_PROVIDED) == 7  # Q2 / Q3a / Q3b / Q4 / Q4b / Q5 / Q6 都没答
    assert "未提供：高中选考科目" in partial
    assert "未提供：常读材料" in partial
    # 空项块内部**也照 §5.1 的次序**（不是 ANSWER_SLOTS 的次序）：
    # 学科门类 → 专业 → 六选三 → 自报领域 → 常读 → 偏好 → 补充说明
    # （上一版用 ANSWER_SLOTS 的次序，把「补充说明」插到了常读/偏好前面）
    tail_order = [
        "未提供：大学学科门类",
        "未提供：大学专业",
        "未提供：高中选考科目",
        "未提供：熟悉领域",
        "未提供：常读材料",
        "未提供：偏好讲法",
        "未提供：补充说明",
    ]
    tail_positions = [partial.index(item) for item in tail_order]
    assert tail_positions == sorted(tail_positions), partial
    # 条件不成立的题不写「未提供」（那不是「没答」，是「压根没问」）
    junior = render_sketch({"version": 1, "education": "junior_or_below"})
    assert "高中选考" not in junior and "大学学科门类" not in junior
    assert junior.count(NOT_PROVIDED) == 4  # Q4 / Q4b / Q5 / Q6
    assert "未提供：熟悉领域" in junior

    # 条件题给了豁免值 / 「没读大学」时，素描不写自相矛盾的话
    assert "大学为" not in render_sketch({"version": 1, "education": "college", "discipline": "none"})
    assert "大学为" not in render_sketch({"version": 1, "education": "undergraduate", "discipline": "unsure"})
    assert "高中选考没上过高中" in render_sketch(
        {"version": 1, "education": "high_school", "high_school_electives": ["not_applicable"]}
    )
    assert "高中选考记不清" in render_sketch(
        {"version": 1, "education": "high_school", "high_school_electives": ["forgotten"]}
    )
    # 只有追问摘要也算「答了一部分」：照样写「未提供：X」，不落到「完全没答」分支
    followup_only = render_sketch({"version": 1, "followups": [{"q": "问", "a": "答", "at": "T"}]})
    assert followup_only.endswith("问→答。") and NOT_PROVIDED in followup_only


def test_followup_summary_is_dropped_by_whole_items_never_truncated() -> None:
    """追问摘要按**整条**处理：短了全留、超预算从最后一条往前整条丢并计数，绝不截半句。"""
    short_core = {"version": 1, "education": "undergraduate"}
    # ① 5 条共 234 字符、素描总长离 400 还远 → **一条都不许丢**（上一版在 200 字符处硬截，第 5 条直接消失）
    five = [{"q": f"问题{i}" + "问" * 20, "a": f"答案{i}" + "答" * 20, "at": "T"} for i in range(1, 6)]
    text = render_sketch(dict(short_core, followups=five))
    assert len(text) <= MAX_SKETCH_CHARS
    assert "追问摘要省略" not in text
    for i in range(1, 6):
        assert f"问题{i}" in text and f"答案{i}" in text, f"第 {i} 条被吞了：{text}"

    # ② 超预算：从最后一条往前整条丢，标记「追问摘要省略 N 条」，且留下的条目**完整**
    long_ones = [{"q": f"Q{i}" + "问" * 60, "a": "答" * 60, "at": "T"} for i in range(1, 6)]
    heavy = dict(
        short_core,
        university={"discipline": "engineering", "major": "机" * 60},
        strengths=["engineering_tech", "programming_computer", "math_stats", "natural_science"],
        high_school_electives=["physics", "chemistry", "biology"],
        reading_materials=["textbook", "papers"],
        preference="simpler_wording",
        strengths_note="补" * 200,
        followups=long_ones,
    )
    heavy_text = render_sketch(heavy)
    assert len(heavy_text) <= MAX_SKETCH_CHARS, len(heavy_text)
    assert "追问摘要省略" in heavy_text, heavy_text
    kept = [i for i in range(1, 6) if f"Q{i}问" in heavy_text]
    omitted = [i for i in range(1, 6) if f"Q{i}问" not in heavy_text]
    assert kept + omitted == [1, 2, 3, 4, 5], heavy_text
    assert kept == list(range(1, len(kept) + 1)), "必须从最后一条往前丢，不许挑着丢"
    assert f"追问摘要省略 {len(omitted)} 条" in heavy_text
    # 留下的每一条都是完整的「问→答」（没有半句）
    for i in kept:
        assert f"Q{i}" + "问" * 60 + "→" + "答" * 60 in heavy_text
    for i in omitted:
        assert "答" * 60 not in heavy_text.split(f"Q{i}")[0][-5:]  # 不残留半条
    assert heavy_text == render_sketch(dict(heavy))  # 逐字稳定


def test_major_without_a_valid_discipline_stays_in_the_core_section() -> None:
    """N6：手改画像里「只有专业、没有有效门类」时，专业也按**核心项**渲染、不参与丢弃。

    两种畸形输入：``{education, major}``（压根没有 university.discipline）与
    ``{university: {discipline: none, major}}``（门类是「没读大学」）。
    接口那条路走不到这两种形态（``validate_answers`` 在 Q3b 条件不成立时会丢掉 major），
    所以这里直接喂背景块——两条路径的渲染行为必须一致，否则会留下一个只在手改时出现的隐藏偏差。
    """
    assert "Q3b" not in DROPPABLE_ORDER, "专业依附门类、整体算核心项，不该留在丢弃列表里"
    malformed = [
        {"version": 1, "education": "master", "major": "国际经济与贸易"},
        {"version": 1, "education": "master", "university": {"discipline": "none", "major": "国际经济与贸易"}},
        {"version": 1, "education": "master", "university": {"discipline": "unsure", "major": "贸易"}},
    ]
    for background in malformed:
        sketch = render_sketch(background)
        assert sketch.startswith("硕士；专业："), sketch  # 专业紧跟学历，落在核心段
        assert "已省略" not in sketch

    # 压力态：核心项满载 + 大量可丢项 + 超长追问 → 被丢的永远是可丢项，专业一律还在
    heavy = dict(
        malformed[0],
        major="机" * 60,
        high_school_electives=["physics", "chemistry", "biology"],
        strengths=["engineering_tech", "programming_computer", "math_stats", "natural_science"],
        reading_materials=["textbook", "papers", "tech_docs", "reports"],
        preference="simpler_wording",
        strengths_note="补" * 200,
        followups=[{"q": "问" * 90, "a": "答" * 100, "at": "T"}],
    )
    stressed = render_sketch(heavy)
    assert len(stressed) <= MAX_SKETCH_CHARS, len(stressed)
    assert "专业：" + "机" * 60 in stressed, "核心段的专业被丢了"
    assert "已省略" in stressed or "追问摘要省略" in stressed, "压力态应该确实丢了可丢项"
    # 门类缺失时「未提供：大学学科门类」照旧出现在空项块里，但专业本身不受影响
    assert "未提供：大学学科门类" in stressed
    assert stressed == render_sketch(dict(heavy))  # 逐字稳定


#: 可丢项在素描里的开头文字（用来判断「这一项到底在不在」）。
#: 专业（Q3b）不在里面：它依附学科门类、整体算核心项，不参与丢弃。
DROPPABLE_MARKS = {"Q5": "常读：", "Q6": "遇到看不懂时希望：", "Q2": "高中选考", "Q4b": "补充："}


def _optional_candidates(background: dict) -> list[str]:
    """这份画像里**真的有内容**的可丢项（按 ``DROPPABLE_ORDER`` 的顺序）。"""
    present = {
        "Q5": bool(background.get("reading_materials")),
        "Q6": bool(background.get("preference")),
        "Q2": bool(background.get("high_school_electives")),
        "Q4b": bool(background.get("strengths_note")),
    }
    return [qid for qid in DROPPABLE_ORDER if present.get(qid)]


def _dropped_items(background: dict) -> list[str]:
    """这次渲染里被丢掉的可选项（按 ``DROPPABLE_ORDER`` 的顺序返回）。"""
    sketch = render_sketch(background)
    return [qid for qid in _optional_candidates(background) if DROPPABLE_MARKS[qid] not in sketch]


def test_sketch_never_exceeds_the_hard_limit_and_drops_in_the_contract_order() -> None:
    """超长时按固定顺序**整项**丢弃（先 Q5 常读 → Q6 偏好 → Q2 六选三 → 最后 Q4b 补充）。

    两条硬纪律：整段永远 ≤ ``MAX_SKETCH_CHARS``（本轮修的是「预算没算未提供行与省略后缀」）；
    丢弃集合永远是 ``DROPPABLE_ORDER`` 的**前缀**——不许跳过前面没丢的项先把后面的丢掉
    （上一版就是 Q4b 第一个出局，正好与契约相反）。
    """
    background = {
        "version": 1,
        "education": "undergraduate",
        "discipline": "engineering",
        "major": "机械设计制造及其自动化（卓越工程师班）",
        "high_school_electives": ["physics", "chemistry", "biology"],
        "strengths": ["engineering_tech"],
        "reading_materials": ["textbook", "papers", "tech_docs", "reports"],
        "preference": "simpler_wording",
        "strengths_note": "补" * 200,
    }
    short = render_sketch(background)
    assert len(short) <= MAX_SKETCH_CHARS
    assert "已省略" not in short
    assert "补充：" + "补" * 200 in short
    assert "专业：机械设计制造及其自动化（卓越工程师班）" in short
    assert _dropped_items(background) == []

    # 扫一遍「补充说明 + 追问摘要」的长度：无论怎么长都 ≤400，且丢弃集合始终是固定顺序的前缀
    for note_len in range(0, 201, 25):
        for followup_len in (0, 60, 120, 200):
            probe = dict(
                background,
                strengths_note="补" * note_len,
                followups=[{"q": "问" * followup_len, "a": "答" * followup_len, "at": "T"}] if followup_len else [],
            )
            # 字段与选项文本不变 ⇒ 同一份画像必须仍得同一份文本
            assert render_sketch(probe) == render_sketch(dict(probe))
            text = render_sketch(probe)
            assert len(text) <= MAX_SKETCH_CHARS, (note_len, followup_len, len(text))
            dropped = _dropped_items(probe)
            expected_prefix = _optional_candidates(probe)[: len(dropped)]
            assert dropped == expected_prefix, (note_len, followup_len, dropped)

    # 只超出一点点：只丢最该丢的 Q5（常读），更该留的 Q4b（补充）必须还在
    just_over = dict(background, strengths_note="补" * 300)
    text = render_sketch(just_over)
    assert len(text) <= MAX_SKETCH_CHARS
    assert _dropped_items(just_over) == ["Q5"]
    assert "已省略 1 项" in text
    assert "常读：" not in text
    assert "补充：" + "补" * 300 in text
    assert "高中选考物理、化学、生物" in text and "遇到看不懂时希望：" in text

    # 再长一点：Q5、Q6 依次出局，Q4b（补充）仍然留到最后一个
    much_longer = dict(background, strengths_note="补" * 400, followups=[{"q": "问" * 90, "a": "答" * 100, "at": "T"}])
    sketch = render_sketch(much_longer)
    assert len(sketch) <= MAX_SKETCH_CHARS, sketch
    dropped = _dropped_items(much_longer)
    assert dropped == _optional_candidates(much_longer)[: len(dropped)], dropped
    assert sketch.startswith("本科；") and "大学为工学门类" in sketch
    assert "自报熟悉领域：工程技术" in sketch
    if "Q4b" not in dropped:
        assert "补充：" in sketch

    # 极端一档：补充说明 200 字 + 追问 q90/a100（复核报的 457 字符那组）必须 ≤400
    worst = dict(background, strengths_note="补" * 200, followups=[{"q": "问" * 90, "a": "答" * 100, "at": "T"}])
    worst_text = render_sketch(worst)
    assert len(worst_text) <= MAX_SKETCH_CHARS, len(worst_text)
    # 逐字稳定：同画像两次渲染一致（含被压缩过的那一支）
    assert worst_text == render_sketch(dict(worst))

    # 丢得多了也绝不把一项截成半句：只许整项消失，剩下的仍是完整片段
    oversized = dict(
        background,
        major="机" * 60,
        strengths=["engineering_tech", "programming_computer", "math_stats", "natural_science"],
        strengths_note="补" * 400,
    )
    bigger = render_sketch(oversized)
    assert len(bigger) <= MAX_SKETCH_CHARS, bigger
    assert "已省略" in bigger
    assert "机" * 60 in bigger  # 专业这项还在，且是完整的 60 个字（没被切半句）
    assert "自报熟悉领域：工程技术、编程与计算机、数学与统计、自然科学" in bigger


# ------------------------------------------------- 3. 画像读写（§12.3） --
def test_save_background_writes_the_contract_shape(dirs: SimpleNamespace) -> None:
    """写入后读回形状正确：version / answered_at / 各题字段都在，其余顶层键不动。"""
    from ar.profile import load_profile

    saved, degraded = save_background(dirs.profiles, "local-default", FULL_BACKGROUND)
    assert degraded and "不存在" in degraded[0]  # 首次提交顺带建档：可见降级

    profile, notes = load_profile(dirs.profiles, "local-default")
    assert notes == []
    assert profile["reader"] == "local-default"
    assert profile["concepts"] == {} and profile["preferences"] == [] and profile["overrides"] == []
    background = background_of(profile)
    assert background == FULL_BACKGROUND  # version 与 followups 原样保留（含 at）
    assert background["version"] == 1
    assert saved["background"] == FULL_BACKGROUND
    assert has_background(profile) is True


def test_old_profile_without_background_loads_without_side_effects(dirs: SimpleNamespace) -> None:
    """老画像（无 background）读取不报错、不写盘；补上后照样能读。"""
    path = dirs.profiles / "local-default" / "profile.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("reader: local-default\nconcepts: {}\npreferences: []\noverrides: []\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    from ar.profile import load_profile

    profile, notes = load_profile(dirs.profiles, "local-default")
    assert notes == []
    assert background_of(profile) == {}
    assert has_background(profile) is False
    assert path.read_text(encoding="utf-8") == before, "读画像不许有副作用"

    saved, _ = save_background(dirs.profiles, "local-default", FULL_BACKGROUND)
    assert saved["background"]["education"] == "undergraduate"
    reloaded, _ = load_profile(dirs.profiles, "local-default")
    assert reloaded["background"] == FULL_BACKGROUND


def test_merge_followup_answers_appends_and_updates_by_position() -> None:
    """追问答案按 ``{q, a, at}`` 追加；同一问题再来一次按位置覆盖，不重复堆叠。"""
    merged = merge_followup_answers(
        {"followups": [{"q": "读过原文吗？", "a": "", "at": "2026-10-08T16:45:00+08:00"}]},
        [{"q": "读过原文吗？", "a": "读过前两章"}],
    )
    assert len(merged) == 1
    assert merged[0]["a"] == "读过前两章"
    assert merged[0]["at"].endswith("+08:00") and merged[0]["at"] != "2026-10-08T16:45:00+08:00"

    added = merge_followup_answers({"followups": merged}, [{"q": "另一问", "a": "答案"}])
    assert [item["q"] for item in added] == ["读过原文吗？", "另一问"]

    # 只给 a 不给 q 时按位置对齐（前端可以只回答案）
    positional = merge_followup_answers({"followups": [{"q": "第一问", "a": ""}]}, [{"a": "按位置"}])
    assert positional[0]["a"] == "按位置"


# ------------------------------------------------- 4. 答案校验（§12.4） --
def test_validate_answers_accepts_a_full_submission() -> None:
    answers, truncated, degraded = validate_answers(
        {
            "education": "undergraduate",
            "high_school_electives": ["physics", "chemistry", "biology"],
            "discipline": "economics",
            "major": "  国际经济与贸易  ",
            "strengths": ["economics_finance", "economics_finance", "programming_computer"],
            "reading_materials": ["textbook"],
            "preference": "example",
            "strengths_note": "读过《资本论》第一卷前两章",
        }
    )
    assert degraded == [] and truncated == {}
    assert answers["major"] == "国际经济与贸易"  # 规范化（去首尾空白）
    assert answers["strengths"] == ["economics_finance", "programming_computer"]  # 去重保序
    assert set(answers) == {
        "education",
        "high_school_electives",
        "discipline",
        "major",
        "strengths",
        "reading_materials",
        "preference",
        "strengths_note",
    }


#: 非法作答的负向对照：枚举外 / 六选三不是 3 项 / 互斥值同选 / 未知字段。
#: 匹配串一律取 **ASCII**（报错文案里本来就点名了字段名），免得中文串在工具链里被转码后误判。
@pytest.mark.parametrize(
    "submitted, message",
    [
        ({"education": "\u5927\u5b66"}, "education"),
        ({"education": "undergraduate", "preference": "\u968f\u4fbf\u8bb2\u8bb2"}, "preference"),
        ({"education": "undergraduate", "discipline": "economy"}, "discipline"),
        ({"education": "undergraduate", "high_school_electives": ["physics", "chemistry"]}, "high_school_electives"),
        ({"education": "high_school", "high_school_electives": []}, "high_school_electives"),
        (
            {"education": "high_school", "high_school_electives": ["physics", "chemistry", "biology", "history"]},
            "high_school_electives",
        ),
        ({"education": "high_school", "high_school_electives": ["forgotten", "physics", "chemistry"]}, "forgotten"),
        ({"education": "undergraduate", "strengths": ["none", "law"]}, "none"),
        ({"education": "undergraduate", "reading_materials": ["rarely", "fiction"]}, "rarely"),
        ({"education": "undergraduate", "\u5168\u9009": True}, "unknown"),
    ],
)
def test_validate_answers_rejects_illegal_values(submitted: dict, message: str) -> None:
    """枚举外 / 六选三不是 3 项 / 互斥值同选 / 未知字段 → ``InputError``（HTTP 400）。"""
    with pytest.raises(InputError) as excinfo:
        validate_answers(submitted)
    text = str(excinfo.value)
    if message == "unknown":
        # 「未知字段」那条报错列的是可用字段，点名不了题号
        assert "high_school_electives" in text or "Q4b" in text, text
    else:
        # 报错必须点名「哪一题/哪个字段」（六选三计数那条点的是字段名，其余点题号 Q*）
        assert message in text, text
        assert "Q" in text or "electives" in text, f"报错必须点名是哪一题：{text}"


def test_validate_answers_truncates_long_text_and_marks_it() -> None:
    """超长自由文本截断并标 ``truncated``（降级可见，不许静默截断），枚举里也要报出来。"""
    answers, truncated, degraded = validate_answers(
        {
            "education": "undergraduate",
            "discipline": "engineering",
            "major": "机" * 61,
            "strengths_note": "读" * 250,
        }
    )
    assert truncated == {"major": True, "strengths_note": True}
    assert len(answers["major"]) == 60 and len(answers["strengths_note"]) == 200
    assert len(degraded) == 2
    assert any("60" in item and "Q3b" in item and "major" in item for item in degraded)
    assert any("200" in item and "Q4b" in item and "strengths_note" in item for item in degraded)
    assert all("truncated" in item for item in degraded), "降级文案里必须点名 truncated（可见降级）"
    # 正好到上限不算截断
    _ok_answers, ok_truncated, ok_degraded = validate_answers(
        {"education": "undergraduate", "discipline": "engineering", "major": "机" * 60}
    )
    assert ok_truncated == {} and ok_degraded == []


def test_validate_answers_drops_conditional_fields_instead_of_writing_them() -> None:
    """条件不成立的字段**不写进画像**（不是写 null），并留下可见说明。"""
    answers, truncated, degraded = validate_answers(
        {"education": "junior_or_below", "high_school_electives": ["physics", "chemistry", "biology"]}
    )
    assert "high_school_electives" not in answers
    assert truncated == {}
    assert any("Q2" in item and "忽略" in item for item in degraded)

    kept = validate_answers({"education": "undergraduate", "discipline": "none", "major": "国际经济与贸易"})
    assert "major" not in kept[0]
    assert any("Q3b" in item for item in kept[2])

    assert effective_answers({"education": "undergraduate", "discipline": "unsure", "major": "x"}) == {
        "education": "undergraduate",
        "discipline": "unsure",
    }

    # Q2 的豁免值不与多选互斥、也不受「正好 3 项」约束（契约 §3）
    exempt = validate_answers({"education": "high_school", "high_school_electives": ["not_applicable"]})
    assert exempt[0]["high_school_electives"] == ["not_applicable"]
    assert exempt[2] == []
    with pytest.raises(InputError):
        validate_answers({"education": "high_school", "high_school_electives": ["forgotten", "physics", "chemistry"]})


# ------------------------------------------------- 5. 追问端点（§12.5） --
FOLLOWUP_JSON = json.dumps(
    {
        "questions": [
            {"q": "你说熟悉经济与金融——读过《资本论》原文吗？", "hint": "读过哪几章 / 只读过二手介绍"},
            {"q": "高中选考物理化学生物，统计课学过吗？", "hint": "学过 / 自学过 / 没学过"},
            {"q": "常读学术论文，读的是哪个领域？", "hint": "经济学 / 其它"},
        ]
    },
    ensure_ascii=False,
)


def test_followup_endpoint_returns_questions_and_records_usage(dirs: SimpleNamespace, fake_chat_client) -> None:
    """成功路径：3–5 个问题带 q/hint，模型与 prompt_version 是产出侧字段。"""
    make_client(dirs).post("/api/survey", json={"answers": dict(SUBMITTED_ANSWERS)})
    fake = fake_chat_client(
        handler=lambda _messages: FOLLOWUP_JSON,
        usage={"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
    )
    client = make_client(dirs, api_key="sk-" + "3" * 40, client_factory=lambda: configured(fake))

    payload = client.post("/api/survey/followup", json={}).json()
    assert payload["degraded"] is False
    assert [item["q"] for item in payload["questions"]][0].startswith("你说熟悉经济与金融")
    assert payload["questions"][0]["hint"]
    assert payload["model"] == "fake-model"
    assert payload["prompt_version"] == "survey-v1"
    assert payload["usage"] == {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33}
    # 逐次尝试账只进 survey.json，**不进响应体**（HTTP 响应键一个不增）
    assert "attempts" not in payload and "retry_account" not in payload

    sent = fake.calls[0][1]["content"]
    assert "本科" in sent and "读过《资本论》第一卷前两章" in sent  # 素描进了提示词
    assert "questions" in sent and "3–5" in sent  # 固定块（JSON 形状 + 规则）在前面
    assert sent.index("请输出这样的 JSON") < sent.index("读者背景（问卷作答的素描")
    assert "材料正文" not in sent  # 追问不读材料正文


def test_followup_endpoint_degrades_visibly_when_model_fails(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """模型调用失败 → 200 + degraded 说明，**固定题答案一个都不丢**。"""
    submitted = dict(SUBMITTED_ANSWERS)
    client = make_client(dirs)
    client.post("/api/survey", json={"answers": submitted})

    def boom(_messages):
        raise RuntimeError("网关 503")

    broken = make_client(
        dirs, api_key="sk-" + "4" * 40, client_factory=lambda: configured(fake_chat_client(handler=boom))
    )
    payload = broken.post("/api/survey/followup", json={}).json()
    assert payload["degraded"] is True
    assert payload["reason"] == "model_error"
    assert payload["questions"] == []
    assert payload["text"].strip() and "503" in payload["text"]
    # 降级文案统一走 ar.llm.failure_message：带尝试次数与末次错误码（策略是「不重试」→ 已试 1 次）
    assert "已试 1 次，末次 UNKNOWN" in payload["text"], payload["text"]
    assert "model" not in payload and "usage" not in payload  # 降级时不给产出侧字段

    after = client.get("/api/survey").json()
    assert after["answers"] == submitted, "固定题答案不受追问失败影响"
    assert (dirs.profiles / "local-default" / "profile.yaml").is_file()


def test_followup_endpoint_degrades_when_model_not_configured_or_reply_is_bad(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """未配置 / 模型给了不合要求的条数 → 都是可见降级，不 500、不假装成功。"""
    make_client(dirs).post("/api/survey", json={"answers": dict(SUBMITTED_ANSWERS)})

    not_configured = make_client(dirs).post("/api/survey/followup", json={}).json()
    assert not_configured["degraded"] is True
    assert not_configured["reason"] == "model_not_configured"
    assert "AR_API_KEY" in not_configured["text"]

    too_few = fake_chat_client(
        handler=lambda _messages: json.dumps({"questions": [{"q": "只有一个问题？", "hint": "x"}]}, ensure_ascii=False)
    )
    awful = make_client(dirs, api_key="sk-" + "5" * 40, client_factory=lambda: configured(too_few))
    payload = awful.post("/api/survey/followup", json={}).json()
    assert payload["degraded"] is True and payload["reason"] == "bad_reply"
    assert "3–5" in payload["text"]


def test_followup_answers_are_merged_into_the_profile_and_the_sketch(dirs: SimpleNamespace) -> None:
    """读者交回的追问答案并进 ``background.followups`` 且**并进素描**（否则追问等于白问）。"""
    client = make_client(dirs)
    client.post("/api/survey", json={"answers": dict(SUBMITTED_ANSWERS)})
    stored = client.get("/api/survey").json()["answers"]
    answered = client.post(
        "/api/survey/followup",
        json={"followups": [{"q": "读过《资本论》原文吗？", "a": "只读过二手介绍"}]},
    ).json()
    assert answered["degraded"] is True  # 模型没配置：问答照样写进画像

    from ar.profile import load_profile

    profile, _notes = load_profile(dirs.profiles, "local-default")
    followups = background_of(profile)["followups"]
    assert followups[0]["q"] == "读过《资本论》原文吗？"
    assert followups[0]["a"] == "只读过二手介绍"
    sketch = render_sketch(background_of(profile))
    assert "只读过二手介绍" in sketch
    assert stored  # 固定题答案还在


def test_followup_requires_a_saved_background(dirs: SimpleNamespace) -> None:
    response = make_client(dirs).post("/api/survey/followup", json={})
    assert response.status_code == 400
    assert "没有背景" in response.json()["error"]


# ------------------------------------------------- 6. 提示词布局（§12.6） --
def test_sketch_sits_in_the_fixed_block_and_the_first_diff_stays_at_the_batch_header() -> None:
    """素描进固定块（紧跟画像块）、首个差异字符仍在段号位、固定块里无时间戳、规则措辞未变。"""
    from ar.prompts import build_rewrite_messages

    # tests/ 不是包（没有 __init__.py）：pytest 的 rootdir 导入路径下直接按模块名引既有素材
    from test_rewrite import FAKE_SOURCE, build_doc

    doc = build_doc(FAKE_SOURCE)
    paragraphs = doc["paragraphs"]
    sketch = render_sketch(FULL_BACKGROUND)
    blocks = {
        "profile_block": "读者 local-default 自报概念状态：c01=unknown",
        "background_block": sketch,
        "concept_block": "c01 假概念（难度 3）",
    }

    def user_of(chunk: list[dict], start: int, end: int) -> str:
        messages = build_rewrite_messages(
            "fake-doc", chunk, start=start, end=end, total=len(paragraphs), **blocks  # type: ignore[arg-type]
        )
        return messages[1]["content"]

    user_a = user_of(paragraphs[:3], 1, 3)
    user_b = user_of(paragraphs[3:], 4, len(paragraphs))

    # ① 素描在固定块里、紧跟画像块（在概念清单与 JSON 示例之前）
    assert user_a.index("读者背景：") > user_a.index("读者画像：")
    assert user_a.index(sketch) < user_a.index("概念清单：")
    assert user_a.index(sketch) < user_a.index("请输出这样的 JSON")
    assert sketch in user_b

    # ② 首个差异字符仍落在段号那一位（素描逐字稳定 ⇒ 不缩短共享前缀）
    first_diff = next(index for index, (left, right) in enumerate(zip(user_a, user_b)) if left != right)
    assert user_a[first_diff] == "1" and user_b[first_diff] == "4"
    assert user_a[first_diff - len("本次给你第 "):first_diff] == "本次给你第 "
    assert first_diff >= 1000, f"共享前缀只有 {first_diff} 字符：变化的内容又被挪到前面了"
    # 加了素描之后前缀只会更长（同一份固定块下比不加素描时多出素描的长度）
    assert first_diff >= len(sketch)

    # ③ 固定块里没有时间戳；rewrite 的规则措辞逐字未变
    assert "2026-10-08" not in user_a
    assert not re.search(r"\d{4}-\d{2}-\d{2}T", user_a)
    assert "{background_block}" in REWRITE_USER_TEMPLATE
    # rewrite-v4：规则 2 改成四动作判据（原来那句「compress / illustrate 本期不实现」已删）
    assert "2. action 只能是 expand / compress / keep / illustrate 四个动作之一：" in user_a
    assert "compress / illustrate 本期不实现，不要输出。" not in user_a
    assert "7. 不要写「改写说明」「导读」之类的额外段落；只输出 JSON。" in user_a


def test_survey_prompt_keeps_the_answers_at_the_end() -> None:
    """追问提示词：固定块（角色 / JSON 形状 / 规则）在前，**作答在末尾**（前缀缓存纪律）。"""
    messages = build_survey_messages(sketch=render_sketch(FULL_BACKGROUND), answers={"education": "master"})
    system, user = messages[0]["content"], messages[1]["content"]
    assert "只输出一个 JSON 对象" in system
    assert user.rstrip().endswith('{"education": "master"}')
    assert user.index("请输出这样的 JSON") < user.index("读者背景（问卷作答的素描")
    assert user.index("读者背景（问卷作答的素描") < user.rstrip().rindex('{"education": "master"}')
    assert "\n\n读者背景（问卷作答的素描" in user, "作答块与固定块之间要空行分隔"
    # 空背景也照样稳定（写「未提供」文案，**不带**「读者背景：」前缀——那一行是固定块自带的），
    # 且提醒模型别问没提供的项
    empty = build_survey_messages(sketch="", answers={})[1]["content"]
    assert "\n\n读者背景（问卷作答的素描；读者没提供的一项就别问）：\n未提供\n" in empty


# ------------------------------------------------- 7. HTTP 契约（§8） --
def test_get_survey_returns_questions_and_current_answers(dirs: SimpleNamespace) -> None:
    # 这里守的是「§4 冻结的键一个都不许少」，**不是**「多一个键就红」：按 README §4 的追加字段纪律
    # （「§4 冻结的键一个不少，前端可以只读自己认识的部分」），新增顶层键是允许的变动 ——
    # 写成精确键集的话，每次追加字段都会让这条用例变红，下一个改的人还得再来删一次。
    required = {"version", "answered_version", "questions", "answers", "skipped", "degraded"}
    client = make_client(dirs)
    fresh = client.get("/api/survey").json()
    assert required <= set(fresh), f"§4 冻结的键少了：{sorted(required - set(fresh))}"
    # 追加字段（2026-10-15）：作答时间。存在与形状单独钉住（缺键 / 被改名 / 编造成非字符串都要红）
    assert "answered_at" in fresh, "§4 的 GET /api/survey 该给 answered_at（没有就写 null）"
    assert fresh["answered_at"] is None or isinstance(fresh["answered_at"], str)
    assert fresh["version"] == SURVEY_VERSION
    assert [q["id"] for q in fresh["questions"]] == [q["id"] for q in QUESTIONS]
    assert fresh["answers"] is None and fresh["skipped"] is False
    assert fresh["answered_version"] is None, "没答过 → answered_version 为 null"
    assert fresh["degraded"] and "不存在" in fresh["degraded"][0]

    submitted = dict(SUBMITTED_ANSWERS)
    client.post("/api/survey", json={"answers": submitted})
    after = client.get("/api/survey").json()
    assert after["answers"] == submitted
    assert after["skipped"] is False
    assert after["answered_version"] == SURVEY_VERSION, "已答且版本相同 → 就是当前版本"
    assert after["degraded"] == []
    # answered_version 是**顶层追加字段**：不许混进 answers（那里的键要与 questions[].field 一一对应）
    assert "answered_version" not in after["answers"]
    assert set(after["answers"]) <= {q["field"] for q in QUESTIONS}


def test_condition_alias_equals_the_authoritative_requires_for_every_value_combination() -> None:
    """别名（``when`` / ``alsoWhen``）与权威 ``requires`` **逐组合等价**，且服务端判定同源。

    两个方向都查：

    1. **集合对账**：别名排除 / 放行的取值集合 == ``requires`` 的 ``not_in`` / ``in`` 集合
       （Q3b 的 ``not_in`` 是 ``["none", "unsure"]``：旧实现只把第一个值写进别名，
       照别名实现的前端会在选「不确定」时仍然显示 Q3b，读者填的专业随后被丢掉）；
    2. **求值对账**：按前端 ``evalWhen`` 语义求值别名，与 ``question_applies`` 的判断逐组合一致
       （覆盖每个被引用题的全部枚举取值 + 「还没答」）。
    """
    # ① 集合对账：题型与取值一个不漏
    for question in QUESTIONS:
        rules = condition_rules(question)
        assert len(rules) == len(question["requires"]), question["id"]
        for requirement, rule in zip(question["requires"], rules):
            field_name = ANSWER_SLOTS[requirement["question"]]
            assert rule["field"] == field_name
            if "in" in requirement:
                assert rule["kind"] == "equals"
                assert rule["values"] == list(requirement["in"])
            else:
                assert rule["kind"] == "notEquals"
                assert rule["values"] == list(requirement["not_in"]), "别名必须带全被排除的值"
                assert rule["value"] == requirement["not_in"][0]
    q3b = condition_rules(QUESTION_BY_ID["Q3b"])
    assert q3b[1]["values"] == ["none", "unsure"], "Q3b 的别名不许只报第一个被排除值"
    # questions[] 里发出去的就是这份（前端读到的是同一个对象）
    assert QUESTION_BY_ID["Q3b"]["alsoWhen"] == q3b[1]

    # ② 求值对账：前端 evalWhen 的语义（在测试里独立实现一遍）vs 服务端 question_applies
    def js_eval_when(cond: dict, answers: dict) -> bool:
        kind = cond.get("kind")
        if kind == "always":
            return True
        value = field_value(answers, cond["field"])
        has_value = bool(value) if isinstance(value, list) else bool(str(value).strip() if value is not None else False)
        if kind == "equals":
            return value in cond["values"]
        if kind == "notEquals":
            if cond.get("includeEmpty") and not has_value:
                return True
            if not has_value:
                return False
            excluded = cond["values"] if cond.get("values") else [cond.get("value")]
            return value not in excluded
        return True

    universe = [None, ""]  # 「还没答」两种写法都算没答
    for question in QUESTIONS:
        for requirement in question["requires"]:
            universe += [item["value"] for item in QUESTION_BY_ID[requirement["question"]]["options"]]
    for question in QUESTIONS:
        answers_options: list[dict] = [{}]
        for requirement in question["requires"]:
            source = QUESTION_BY_ID[requirement["question"]]
            values = [None] + [item["value"] for item in source["options"]]
            field_name = ANSWER_SLOTS[requirement["question"]]
            answers_options = [dict(item, **{field_name: value}) for item in answers_options for value in values]
        for answers in answers_options:
            conds = [question["when"]] + ([question["alsoWhen"]] if "alsoWhen" in question else [])
            assert all(js_eval_when(cond, answers) for cond in conds) == question_applies(
                question["id"], answers
            ), (question["id"], answers)
            # 服务端自己的判定走的就是同一套规则（同源）
            assert all(rule_holds(rule, answers) for rule in condition_rules(question)) == question_applies(
                question["id"], answers
            )
    assert universe  # 取值全集非空（题目的枚举确实被扫到了）


def test_visibility_matches_between_alias_and_backend_including_unsure() -> None:
    """回归那条具体事故：Q3a 选「不确定 / 没读大学」时 Q3b 必须**别名与判定都不出现**。"""
    for discipline in ("none", "unsure"):
        answers = {"education": "undergraduate", "discipline": discipline}
        assert question_applies("Q3b", answers) is False
        assert (
            sum(
                1
                for cond in (QUESTION_BY_ID["Q3b"]["when"], QUESTION_BY_ID["Q3b"]["alsoWhen"])
                if not rule_holds(cond, answers)
            )
            == 1
        ), "别名里必须有一条把 unsure/none 都排除掉"
    assert question_applies("Q3b", {"education": "undergraduate", "discipline": "engineering"}) is True


def test_answered_version_three_states(dirs: SimpleNamespace) -> None:
    """``answered_version`` 三态：没答过 → null；同版 → 当前版本；更旧 → 那个旧版本号。

    契约 §11 的「问卷已更新，可重新填」提示就靠它，所以「整场跳过」也算没答过（null）。
    """
    client = make_client(dirs)
    assert client.get("/api/survey").json()["answered_version"] is None  # ① 没答过

    client.post("/api/survey", json={"answers": {"education": "undergraduate"}})
    assert client.get("/api/survey").json()["answered_version"] == 1  # ② 已答、版本相同

    # ③ 已答、版本更旧：手改一份老版本画像
    path = dirs.profiles / "local-default" / "profile.yaml"
    path.write_text(
        "reader: local-default\nbackground:\n  version: 0\n  education: master\n"
        "concepts: {}\npreferences: []\noverrides: []\n",
        encoding="utf-8",
    )
    older = client.get("/api/survey").json()
    assert older["answered_version"] == 0
    assert older["answers"] == {"education": "master"}, "老答案照旧可读，不丢"
    assert any("问卷已更新" in item for item in older["degraded"])

    # ④ 一份**全新**画像上的「整场跳过」：写了 skipped=true 但一道固定题都没答 → 仍算没答过
    fresh = SimpleNamespace(
        tmp=dirs.tmp / "fresh", data=dirs.tmp / "fresh" / "data", profiles=dirs.tmp / "fresh" / "profiles",
        web=dirs.tmp / "fresh" / "web",
    )
    fresh.data.mkdir(parents=True)
    fresh.profiles.mkdir(parents=True)
    fresh_client = make_client(fresh)
    fresh_client.post("/api/survey", json={"skipped": True})
    assert fresh_client.get("/api/survey").json()["answered_version"] is None


def test_post_survey_skip_keeps_existing_answers(dirs: SimpleNamespace) -> None:
    """已答之后再「整场跳过」**不许抹掉旧答案**（静默删读者数据是最危险的一类行为）。

    跳过只记 ``skipped: true``；响应里要写明「已有背景保持不变」，画像与接口两条路都还在。
    """
    from ar.profile import load_profile

    client = make_client(dirs)
    submitted = dict(SUBMITTED_ANSWERS)
    client.post("/api/survey", json={"answers": submitted})
    before_path = dirs.profiles / "local-default" / "profile.yaml"
    before_background = background_of(load_profile(dirs.profiles, "local-default")[0])

    payload = client.post("/api/survey", json={"skipped": True}).json()
    assert payload["ok"] is True
    assert payload["background"]["skipped"] is True
    assert any("保持不变" in item and "跳过" in item for item in payload["degraded"])

    after = client.get("/api/survey").json()
    assert after["skipped"] is True
    assert after["answers"] == submitted, "旧答案必须逐字不变"
    assert after["answered_version"] == SURVEY_VERSION
    saved = background_of(load_profile(dirs.profiles, "local-default")[0])
    assert {k: v for k, v in saved.items() if k != "skipped"} == {
        k: v for k, v in before_background.items() if k != "skipped"
    }, "除了 skipped 标志，一个字段都不许变"
    # 素描仍然是他填的内容（不是「跳过」文案）
    assert payload["sketch"] == render_sketch(payload["background"])
    assert "未提供（读者跳过了背景问卷）" not in payload["sketch"]
    assert before_path.is_file()


def test_post_survey_accepts_and_persists_followups(dirs: SimpleNamespace) -> None:
    """N1：``{ answers, followups }`` 与「只给 followups」两种提交都要落盘、都能读回来。

    上一版静默吞掉 ``followups``（200 但画像/survey.json 里都没有），只发 followups 还会被
    当成「答案里的未知字段」回 400 —— 读者答案静默丢失。
    """
    from ar.profile import load_profile

    client = make_client(dirs)
    followups = [
        {"q": "读过《资本论》原文吗？", "a": "读过前两章"},
        {"q": "统计课学过吗？", "a": "自学过"},
    ]
    response = client.post("/api/survey", json={"answers": dict(SUBMITTED_ANSWERS), "followups": followups})
    assert response.status_code == 200, response.text
    background = response.json()["background"]
    assert [item["q"] for item in background["followups"]] == ["读过《资本论》原文吗？", "统计课学过吗？"]
    assert all(item["at"] for item in background["followups"]), "每条都要带 at"

    # 画像文件与 GET 两条路都看得到
    saved = background_of(load_profile(dirs.profiles, "local-default")[0])
    assert len(saved["followups"]) == 2 and saved["followups"][1]["a"] == "自学过"
    got = client.get("/api/survey").json()
    assert got["answers"]["education"] == "undergraduate"
    assert len(background_of({"background": saved})["followups"]) == 2
    assert "读过《资本论》原文吗？→读过前两章" in render_sketch(saved)
    record = read_json(survey_record_path(dirs.profiles, "local-default"))
    assert [item["q"] for item in record["background"]["followups"]] == ["读过《资本论》原文吗？", "统计课学过吗？"]
    assert record["answers"] == dict(SUBMITTED_ANSWERS), "固定题的原始作答照旧留着"

    # 只给 followups、不给 answers（先跳过问卷、后来答了追问）→ 也算成功，不许 400
    only = make_client(dirs).post("/api/survey", json={"followups": [{"q": "新问题", "a": "新答案"}]})
    assert only.status_code == 200, only.text
    merged = background_of(load_profile(dirs.profiles, "local-default")[0])
    assert [item["q"] for item in merged["followups"]] == ["读过《资本论》原文吗？", "统计课学过吗？", "新问题"]
    assert merged["education"] == "undergraduate", "已有固定题作答不动"
    assert client.get("/api/survey").json()["answers"]["education"] == "undergraduate"

    # 类型不对 / 空对象：仍然是 400（带 followups 的请求不会被当成未知答案字段）
    assert make_client(dirs).post("/api/survey", json={"followups": "不是数组"}).status_code == 400
    assert make_client(dirs).post("/api/survey", json={}).status_code == 400


def test_post_survey_followups_only_on_a_fresh_profile(dirs: SimpleNamespace) -> None:
    """全新画像上只发 followups：建档 + 算「有背景」（素描里只有追问摘要）。"""
    from ar.profile import load_profile

    response = make_client(dirs).post("/api/survey", json={"followups": [{"q": "问", "a": "答"}]})
    assert response.status_code == 200, response.text
    saved = background_of(load_profile(dirs.profiles, "local-default")[0])
    assert saved["version"] == SURVEY_VERSION and saved["followups"][0]["a"] == "答"
    assert has_background({"background": saved}) is True
    assert has_fixed_answers({"background": saved}) is False
    assert make_client(dirs).get("/api/survey").json()["answers"] is None  # 固定题确实没答


def test_followup_call_accounting_lands_in_survey_json(dirs: SimpleNamespace, fake_chat_client) -> None:
    """追问那次模型调用的 token 账要**同时**进响应体与 ``survey.json``（契约 §4）。"""
    client = make_client(dirs)
    client.post("/api/survey", json={"answers": dict(SUBMITTED_ANSWERS)})
    fake = fake_chat_client(
        handler=lambda _messages: FOLLOWUP_JSON,
        usage={"prompt_tokens": 7, "completion_tokens": 9, "total_tokens": 16},
    )
    answered = make_client(dirs, api_key="sk-" + "8" * 40, client_factory=lambda: configured(fake))
    payload = answered.post("/api/survey/followup", json={}).json()
    assert payload["degraded"] is False

    record = read_json(survey_record_path(dirs.profiles, "local-default"))
    call = record["followup"]
    assert call["event"] == "call"
    assert call["model"] == payload["model"] == "fake-model"
    assert call["prompt_version"] == payload["prompt_version"] == "survey-v1"
    assert call["usage"] == payload["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 9,
        "total_tokens": 16,
    }
    assert call["degraded"] is False and call["question_count"] == 3
    # 逐次尝试账（追加字段）：这次追问只试了一次、成功
    assert [item["try"] for item in call["attempts"]] == [1]
    assert call["attempts"][0]["tool"] == "survey_followups" and call["attempts"][0]["code"] is None
    assert [item["event"] for item in record["followup_history"]] == ["call"]
    # 固定题的原始作答与笔记没被这次调用抹掉
    assert record["answers"] == dict(SUBMITTED_ANSWERS)
    assert record["skipped"] is False
    assert record["background"]["university"]["discipline"] == "economics"

    # 读者交回追问答案那一次也要留痕（两种事件分开记）
    answered.post(
        "/api/survey/followup",
        json={"followups": [{"q": payload["questions"][0]["q"], "a": "读过前两章"}]},
    )
    record = read_json(survey_record_path(dirs.profiles, "local-default"))
    assert [item["event"] for item in record["followup_history"]] == ["call", "answers", "call"]
    assert "读过前两章" in json.dumps(record["background"], ensure_ascii=False)


def test_post_survey_accepts_answers_and_returns_the_sketch(dirs: SimpleNamespace) -> None:
    """顶层铺开与 ``answers`` 包装两种写法都认（契约 §8 与 §3 的字段名并存）。"""
    client = make_client(dirs)
    response = client.post("/api/survey", json={"answers": dict(SUBMITTED_ANSWERS)})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"ok", "background", "sketch", "degraded"}
    assert payload["ok"] is True
    assert payload["background"]["education"] == "undergraduate"
    assert payload["background"]["version"] == 1
    assert payload["sketch"] == render_sketch(payload["background"])
    assert "truncated" not in payload

    flat = client.post(
        "/api/survey",
        json={"education": "high_school", "high_school_electives": ["physics", "chemistry", "biology"]},
    ).json()
    assert flat["background"]["education"] == "high_school"
    assert flat["background"]["high_school_electives"] == ["physics", "chemistry", "biology"]
    assert "discipline" not in flat["background"], "条件题不成立时字段不写进画像（不是写 null）"

    # 提交后画像文件里真的落了 background（老键一个不少）
    from ar.profile import load_profile

    profile, _ = load_profile(dirs.profiles, "local-default")
    assert set(profile) == {"reader", "background", "concepts", "preferences", "overrides"}


def test_post_survey_rejects_illegal_answers_with_400_naming_the_question(dirs: SimpleNamespace) -> None:
    client = make_client(dirs)
    bad_enum = client.post("/api/survey", json={"answers": {"education": "大学"}})
    assert bad_enum.status_code == 400
    assert "Q1" in bad_enum.json()["error"]

    bad_count = client.post(
        "/api/survey",
        json={"answers": {"education": "undergraduate", "high_school_electives": ["physics", "chemistry"]}},
    )
    assert bad_count.status_code == 400
    assert "正好 3 项" in bad_count.json()["error"]
    assert not (dirs.profiles / "local-default" / "profile.yaml").exists(), "400 时不许落盘"


def test_post_survey_marks_truncation_visibly(dirs: SimpleNamespace) -> None:
    payload = make_client(dirs).post(
        "/api/survey",
        json={
            "answers": {
                "education": "undergraduate",
                "discipline": "engineering",
                "major": "机" * 90,
            }
        },
    ).json()
    assert payload["truncated"] == {"major": True}
    assert len(payload["background"]["university"]["major"]) == 60
    assert any("截断" in item for item in payload["degraded"])
    record = read_json(survey_record_path(dirs.profiles, "local-default"))
    assert record["truncated"] == {"major": True}
    assert record["answers"]["major"] == "机" * 90, "survey.json 里留原值（区分「正好 60」与「被截断」）"
    assert record["background"]["university"]["major"] == payload["background"]["university"]["major"]
    assert record["version"] == SURVEY_VERSION


def test_post_survey_skip_writes_skipped_flag_and_visible_banner_text(dirs: SimpleNamespace) -> None:
    payload = make_client(dirs).post("/api/survey", json={"skipped": True}).json()
    assert payload["ok"] is True
    assert payload["background"] == {"version": 1, "answered_at": payload["background"]["answered_at"], "skipped": True}
    assert payload["sketch"] == SKIPPED_TEXT
    assert any("跳过" in item for item in payload["degraded"])

    after = make_client(dirs).get("/api/survey").json()
    assert after["skipped"] is True
    assert after["answers"] is None  # 没有作答：answers 为 null

    from ar.profile import load_profile

    profile, _ = load_profile(dirs.profiles, "local-default")
    assert has_background(profile) is False, "整场跳过不算有背景（按通用读者处理）"


def test_post_survey_without_any_answer_is_rejected_and_keeps_the_old_background(
    dirs: SimpleNamespace,
) -> None:
    """空提交不许把已有答案抹掉（前端被绕过时也要可见降级，不是静默清空）。"""
    client = make_client(dirs)
    client.post("/api/survey", json={"answers": {"education": "undergraduate"}})
    empty = client.post("/api/survey", json={})
    assert empty.status_code == 400
    assert "skipped" in empty.json()["error"]

    after = client.get("/api/survey").json()["answers"]
    assert after == {"education": "undergraduate"}


def test_survey_version_mismatch_is_visible(dirs: SimpleNamespace) -> None:
    """问卷升版后：老答案照旧可读，页面上能看到「问卷已更新」的提示（不强制、不丢旧答案）。"""
    path = dirs.profiles / "local-default" / "profile.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "reader: local-default\nbackground:\n  version: 0\n  education: master\n"
        "concepts: {}\npreferences: []\noverrides: []\n",
        encoding="utf-8",
    )
    payload = make_client(dirs).get("/api/survey").json()
    assert payload["answers"] == {"education": "master"}
    assert any("问卷已更新" in item and "0" in item for item in payload["degraded"])


def test_survey_endpoints_do_not_leak_the_api_key(dirs: SimpleNamespace, fake_chat_client) -> None:
    secret = "sk-" + "7" * 40
    client = make_client(
        dirs,
        api_key=secret,
        client_factory=lambda: configured(fake_chat_client(handler=lambda _messages: FOLLOWUP_JSON)),
    )
    body = client.get("/api/survey").text
    client.post("/api/survey", json={"answers": {"education": "undergraduate"}})
    body += client.post("/api/survey/followup", json={}).text
    assert secret not in body
    assert secret not in (dirs.profiles / "local-default" / "profile.yaml").read_text(encoding="utf-8")
    assert secret not in json.dumps(
        read_json(survey_record_path(dirs.profiles, "local-default")), ensure_ascii=False
    )


# ------------------------------------------------- 追问函数直测（不经 HTTP） --
def test_asurvey_followups_is_degraded_without_a_configured_client() -> None:
    import asyncio

    outcome = asyncio.run(asurvey_followups(FULL_BACKGROUND, client=None))
    assert outcome.degraded is True
    assert outcome.reason == "model_not_configured"
    assert outcome.questions == []
    assert outcome.prompt_version == "survey-v1"


def test_followup_questions_grading() -> None:
    """模型输出的条数与形状都要守住 3–5 条、每条非空 q（不合要求即降级，不半信半疑地收下）。"""
    from ar.survey import followup_questions

    good = json.dumps(
        {"questions": [{"q": f"问题{i}", "hint": "提示" if i else ""} for i in range(1, 6)]}, ensure_ascii=False
    )
    questions = followup_questions(good)
    assert len(questions) == 5
    assert questions[0]["hint"], "模型没给 hint 时要有兜底提示"
    assert all(item["q"] for item in questions)

    for bad in (
        json.dumps({"questions": []}),
        json.dumps({"questions": [{"q": "只有一个"}]}),
        json.dumps({"questions": [{"q": f"问题{i}"} for i in range(1, 7)]}),
        json.dumps({"items": [{"q": "字段名不对"}]}),
        "这不是 JSON",
    ):
        with pytest.raises(InputError):
            followup_questions(bad)


def test_background_and_answers_round_trip() -> None:
    """落画像（嵌套）与接口作答（扁平）互为往返：``answers_of(normalize(finalize(x))) == x``。"""
    picked = dict(SUBMITTED_ANSWERS)
    flat = finalize_background(picked, at="2026-10-08T16:40:00+08:00")
    assert flat["version"] == 1 and flat["answered_at"] == "2026-10-08T16:40:00+08:00"
    assert flat["discipline"] == "economics" and flat["major"] == "国际经济与贸易"  # 拼装阶段是扁平的
    background = normalize_background(flat)
    assert answers_of(background) == picked
    assert answers_of({"skipped": True}) == {}
    assert background_of({"background": "不是映射"}) == {}


def test_university_is_nested_in_the_profile_with_the_contract_key_order() -> None:
    """契约 §6.1 的 YAML 是**嵌套**的：大学两项收在 ``background.university`` 下，键顺序照契约。"""
    background = normalize_background(
        {
            "version": 1,
            "answered_at": "2026-10-08T16:40:00+08:00",
            "education": "undergraduate",
            "high_school_electives": ["physics", "chemistry", "biology"],
            "discipline": "economics",  # 提交时是扁平字段名
            "major": "国际经济与贸易",
            "strengths": ["economics_finance"],
            "strengths_note": "读过《资本论》第一卷前两章",
            "reading_materials": ["textbook"],
            "preference": "prerequisites",
        }
    )
    assert list(background) == [
        "version",
        "answered_at",
        "education",
        "high_school_electives",
        "university",
        "strengths",
        "strengths_note",
        "reading_materials",
        "preference",
    ]
    assert background["university"] == {"discipline": "economics", "major": "国际经济与贸易"}
    assert "discipline" not in background and "major" not in background, "别在顶层再留一份"

    # 手改过的扁平老写法读得出来（读宽容），写盘一律收进 university
    flat_legacy = {"version": 1, "discipline": "economics", "major": "贸易"}
    assert answers_of(flat_legacy) == {"discipline": "economics", "major": "贸易"}
    assert normalize_background(flat_legacy)["university"] == {"discipline": "economics", "major": "贸易"}
    # 只有专业没有门类时 university 里只放 major；两项都没有时整个键不出现
    assert normalize_background({"version": 1, "major": "贸易"})["university"] == {"major": "贸易"}
    assert "university" not in normalize_background({"version": 1, "education": "master"})
