"""读前背景问卷：题目定义、答案校验、读者素描渲染、模型追问（PLAN-background-survey）。

三层职责：

1. **固定问卷 v1**（契约 §3）：``QUESTIONS`` 六题 + 条件题规则。枚举值一律用英文稳定串
   （与 ``concepts[].state`` 同惯例），中文只在展示层出现；题目或选项一变就升 ``SURVEY_VERSION``。
   条件题规则**同源**：``condition_rules()`` 从权威的 ``requires`` 派生出一套规则，
   ``question_applies()`` 求的就是它，``questions[].when`` / ``alsoWhen`` 是同一套规则的别名
   （多值排除 ``values`` 带全，不含「只说第一个值」的漏项）。**不信任前端**：
   条件不满足的字段不写进画像（不是写 null），这样「没问过」与「问了没答」不会混在一起。
2. **读者素描**（契约 §5）：``render_sketch()`` 把 ``background`` 渲染成一段中文文本，
   **同画像必得同文本**（``SKETCH_ORDER`` 的字段顺序固定、空项措辞固定、无时间戳 / 随机数 / 段号），
   整段**始终** ≤ ``MAX_SKETCH_CHARS`` 字符：装不下时按 ``DROPPABLE_ORDER``（先 Q5 常读 → Q6 偏好讲法
   → Q2 六选三 → 最后 Q4b 补充说明）整项丢弃并写「…（已省略 N 项）」，绝不把一项截成半句；
   追问摘要按整条从后往前丢（标记「追问摘要省略 N 条」）。
   核心项不参与丢弃：学历 / 学科门类（含专业）/ 专业（门类缺失时单独成项）/ 自报领域。
   两种缺省措辞分得开：整场跳过 → 「未提供（读者跳过了背景问卷）」；
   答了一部分 → 逐项写「未提供：X」。
3. **可选模型追问**（契约 §4）：``asurvey_followups()`` 只读固定题作答 + 素描草稿（**不读材料正文**），
   让模型追问 3–5 个细节；失败 / 未配置 / 超时都返回 ``degraded=True`` + 说明，
   **固定题答案不受影响**（保存与追问是两件事，见 ``ar.server`` 的两个端点）。
   调用经工具层 ``survey_followups``（拼提示词 / 重试 / 逐次记账都在 ``ar.tools`` + ``ar.llm``），
   逐次尝试账由 HTTP 层写进 ``survey.json`` 的追问记账（``attempts``）。

磁盘上只有一处写入：``profiles/<reader>/profile.yaml`` 的顶层键 ``background``
（形态见契约 §6.1）；``profiles/<reader>/survey.json`` 记原始问答与追问的调用元数据，
**不参与缓存指纹**（两处缓存失效信号都只看 ``profile.yaml``）。

**两种形态只差 ``university`` 这一层**（容易踩，写在这里免得两边各改一半）：

- **作答形态（扁平）**：请求体的 ``answers``、``GET /api/survey`` 的 ``answers``、校验与素描内部的
  中间值都是 ``discipline`` / ``major`` 两个平铺字段；
- **画像形态（嵌套）**：落 ``profile.yaml`` 时长这样（契约 §6.1 的 YAML）：

  ```yaml
  background:
    version: 1
    answered_at: "..."
    education: undergraduate
    high_school_electives: [physics, chemistry, biology]
    university:
      discipline: economics
      major: "国际经济与贸易"
    strengths: [economics_finance]
  ```

  收纳与键顺序只由 ``normalize_background()`` 决定（``ar.server.post_survey`` 写盘前调它），
  取值统一走 ``field_value()``（两种形态都认）；读的时候也容忍手改过的扁平老写法，
  但写盘一律收进 ``university``。
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import tools
from .errors import InputError
from .ingest import normalize_text
from .jsonio import now_iso, read_json_if_exists, write_json
from .paths import DEFAULT_READER
from .profile import background_of, load_profile, save_profile
from .prompts import SURVEY_PROMPT_VERSION

__all__ = [
    "SURVEY_VERSION",
    "MAX_SKETCH_CHARS",
    "SKIPPED_TEXT",
    "EMPTY_BACKGROUND",
    "QUESTIONS",
    "QUESTION_BY_ID",
    "DISCIPLINES",
    "HIGH_SCHOOL_ELECTIVES",
    "STRENGTH_OPTIONS",
    "READING_MATERIALS",
    "ANSWER_SLOTS",
    "MULTI_ANSWER_FIELDS",
    "UNIVERSITY_FIELDS",
    "MAX_TEXT_CHARS",
    "question_by_id",
    "question_applies",
    "visible_questions",
    "enum_labels",
    "field_value",
    "effective_answers",
    "validate_answers",
    "render_sketch",
    "sketch_fingerprint",
    "normalize_background",
    "background_of",
    "has_background",
    "has_fixed_answers",
    "has_followups",
    "answered_version",
    "has_concept_signals",
    "condition_rules",
    "rule_holds",
    "answers_of",
    "finalize_background",
    "merge_followup_answers",
    "merge_followups_into",
    "save_background",
    "read_survey_record",
    "write_survey_record",
    "followup_questions",
    "outcome_from_tool_run",
    "SurveyOutcome",
    "asurvey_followups",
]

# 问卷版本：题目或选项一变就升版（老答案照样可读，页面提示「问卷已更新」）
SURVEY_VERSION = 1

#: 素描整段上限（契约 §5.2）：超长时按固定顺序**整项**丢弃
MAX_SKETCH_CHARS = 400
#: 自由文本单行 / 多行上限（契约 §3 校验）：超出**截断并标记 truncated**，不静默
MAX_TEXT_CHARS = {"major": 60, "strengths_note": 200}
#: 素描里落进「（已省略 N 项）」的丢弃顺序（契约 §5.2）：越靠前越先丢。
#: **专业（Q3b）不在这个列表里**：它依附学科门类、整体算核心项（契约 §5.2 的裁决：
#: 接口落盘与手改画像两条路径必须一致，不许出现「只有手改时才会被丢」的隐藏偏差）。
#: 追问摘要也不在这个列表里：它按**整条**从后往前丢，标记是「追问摘要省略 N 条」（见 ``render_sketch``）。
DROPPABLE_ORDER = ("Q5", "Q6", "Q2", "Q4b")

#: 整场跳过时的素描（契约 §5.3 的「完全没答」分支）。
#: **不带「读者背景：」前缀**：模板里那一行已经写了，素描自己再带一次会拼出两行同样的字。
SKIPPED_TEXT = "未提供（读者跳过了背景问卷）"
#: 空 ``background``：与「整场跳过」分开——老画像没有该键、或只写了版本号时是这一支（同样不带前缀）
EMPTY_BACKGROUND = "未提供"

# ------------------------------------------------------------------ 选项枚举 --
#: 中文只在展示层出现；这里的 value 是落画像的英文稳定串
EDUCATION_OPTIONS: list[tuple[str, str]] = [
    ("junior_or_below", "初中及以下"),
    ("high_school", "高中（含中专、职高）"),
    ("college", "大专"),
    ("undergraduate", "本科"),
    ("master", "硕士"),
    ("doctor", "博士"),
    ("prefer_not", "不想说"),
]
#: 六选三：多选，**正好 3 项**；``not_applicable`` / ``forgotten`` 与多选互斥
HIGH_SCHOOL_ELECTIVES: list[tuple[str, str]] = [
    ("physics", "物理"),
    ("chemistry", "化学"),
    ("biology", "生物"),
    ("politics", "政治"),
    ("history", "历史"),
    ("geography", "地理"),
]
ELECTIVE_EXEMPT = ("not_applicable", "forgotten")
ELECTIVE_EXEMPT_LABELS: dict[str, str] = {"not_applicable": "没上过高中", "forgotten": "记不清"}
#: 大学学科门类（契约 §3 只给了中文；落画像的稳定串取拼音，与其它题的英文串同规矩）
DISCIPLINES: list[tuple[str, str]] = [
    ("philosophy", "哲学"),
    ("economics", "经济学"),
    ("law", "法学"),
    ("education", "教育学"),
    ("literature", "文学"),
    ("history", "历史学"),
    ("science", "理学"),
    ("engineering", "工学"),
    ("agriculture", "农学"),
    ("medicine", "医学"),
    ("management", "管理学"),
    ("arts", "艺术学"),
    ("military_science", "军事学"),
    ("interdisciplinary", "交叉学科"),
]
DISCIPLINE_NONE = "none"
DISCIPLINE_UNSURE = "unsure"
DISCIPLINE_EXTRA_LABELS: dict[str, str] = {DISCIPLINE_NONE: "没读大学", DISCIPLINE_UNSURE: "不确定"}
#: 自报特长 / 熟悉领域：多选 + 自由填写；``none`` 与多选互斥
STRENGTH_OPTIONS: list[tuple[str, str]] = [
    ("economics_finance", "经济与金融"),
    ("math_stats", "数学与统计"),
    ("programming_computer", "编程与计算机"),
    ("natural_science", "自然科学"),
    ("medicine_health", "医学与健康"),
    ("history_humanities", "历史与人文"),
    ("philosophy_thought", "哲学与思想"),
    ("law", "法律"),
    ("engineering_tech", "工程技术"),
    ("art_design", "艺术与设计"),
    ("language_literature", "语言与文学"),
    ("education_psychology", "教育心理"),
]
STRENGTH_NONE = "none"
STRENGTH_EXTRA_LABELS: dict[str, str] = {STRENGTH_NONE: "都没有"}
#: 常读材料类型：多选；``rarely`` 与多选互斥
READING_MATERIALS: list[tuple[str, str]] = [
    ("textbook", "教材课本"),
    ("papers", "学术论文"),
    ("popular_news", "通俗读物与新闻评论"),
    ("tech_docs", "技术文档"),
    ("reports", "报告与公文"),
    ("fiction", "小说文学"),
]
READING_RARELY = "rarely"
READING_EXTRA_LABELS: dict[str, str] = {READING_RARELY: "很少读长文"}
#: 遇到看不懂时更希望怎么讲：单选
PREFERENCE_OPTIONS: list[tuple[str, str]] = [
    ("conclusion_first", "直接讲结论"),
    ("prerequisites", "补前置概念"),
    ("example", "举个例子"),
    ("simpler_wording", "换成更简单的话"),
    ("unsure", "说不清"),
]

#: 题号 → 画像里的答案字段（一个题号一个槽位）
ANSWER_SLOTS: dict[str, str] = {
    "Q1": "education",
    "Q2": "high_school_electives",
    "Q3a": "discipline",
    "Q3b": "major",
    "Q4": "strengths",
    "Q4b": "strengths_note",
    "Q5": "reading_materials",
    "Q6": "preference",
}
#: 多选槽位对应的画像字段名（值为字符串数组；判类型时用字段名，别拿题号去比字段名）
MULTI_ANSWER_FIELDS = ("high_school_electives", "strengths", "reading_materials")
#: 画像里收在 ``background.university`` 下的两个槽位（契约 §6.1 的 YAML 是嵌套的）：
#: 作答（``answers``）里是扁平字段名，落画像时收进 ``university``，读回来再摊平给接口。
UNIVERSITY_FIELDS = ("discipline", "major")
#: Q2 / Q4 / Q5 的互斥值：选它就不许再选别的
MUTUALLY_EXCLUSIVE: dict[str, tuple[str, ...]] = {
    "Q2": ELECTIVE_EXEMPT,
    "Q4": (STRENGTH_NONE,),
    "Q5": (READING_RARELY,),
}
#: 素描里的空项措辞：``未提供：X``，X 取题目短标签
SHORT_LABELS: dict[str, str] = {
    "Q1": "受教育程度",
    "Q2": "高中选考科目",
    "Q3a": "大学学科门类",
    "Q3b": "大学专业",
    "Q4": "熟悉领域",
    "Q4b": "补充说明",
    "Q5": "常读材料",
    "Q6": "偏好讲法",
    "followups": "模型追问",
}


def _option_question(
    qid: str,
    *,
    title: str,
    kind: str,
    options: list[tuple[str, str]],
    requires: list[dict[str, Any]] | None = None,
    note: str = "",
    answer_slot: str | None = None,
) -> dict[str, Any]:
    question: dict[str, Any] = {
        "id": qid,
        "title": title,
        "kind": kind,
        "options": [{"value": value, "label": label} for value, label in options],
        "requires": list(requires or []),
        "answer_slot": answer_slot or ANSWER_SLOTS.get(qid, ""),
    }
    if note:
        question["note"] = note
    return question


#: 前端直接拿 ``questions[]`` 渲染时要用的**等价别名**（追加字段，不替代上面的契约键）。
#: 前端的 ``renderQuestions`` 读 ``qid`` / ``field`` / ``label`` / ``exactly`` / ``exclusive`` /
#: ``maxLength`` 这些键；两边同源，这里给一份，前端渲染后端题目时不必回退到本地兜底题目。
def _with_frontend_view(question: dict[str, Any]) -> dict[str, Any]:
    """给题目对象补上前端渲染用的等价别名（``qid`` / ``field`` / ``label`` / 条件 / 校验约束）。

    ``requires``（本契约的条件题规则）仍是**权威**；``when`` / ``alsoWhen`` 由
    ``condition_rules`` 从同一份 ``requires`` 派生，多值排除**带全**，
    服务端自己的判定（``question_applies``）也求这套规则 —— 三处同源，不会各走一半。
    """
    qid = str(question["id"])
    slot = str(question.get("answer_slot") or "")
    question["qid"] = qid
    question["field"] = slot
    question["label"] = question["title"]
    if question["kind"] == "text":
        question["kind"] = "textarea" if question.get("multiline") else "text"
    rules = condition_rules(question)
    question["when"] = rules[0] if rules else {"kind": "always"}
    if len(rules) > 1:
        question["alsoWhen"] = rules[1]
    if qid == "Q2":
        question["exactly"] = 3
    question.setdefault("atLeast", 0)
    question.setdefault("exactly", 0)
    # 互斥值：没有互斥规则时给空数组（前端按数组读，形态统一）
    question.setdefault("exclusive", list(MUTUALLY_EXCLUSIVE.get(qid, ())))
    question.setdefault("maxLength", question.get("max_chars", 0))
    question.setdefault("placeholder", "")
    return question


def condition_rules(question: dict[str, Any]) -> list[dict[str, Any]]:
    """题目的 ``requires`` → 条件规则列表（**服务端判定与前端别名共用这一份**）。

    规则形态（与前端 ``evalWhen`` 同形）：

    - ``in``      → ``{"kind": "equals", "field": …, "values": [...]}``
    - ``not_in``  → ``{"kind": "notEquals", "field": …, "value": <第一个>, "values": [全部被排除值],
      "includeEmpty": true}``

    ``values`` 必须带**全部**被排除值：Q3b 的 ``not_in`` 是 ``["none", "unsure"]``，
    只报第一个值会让照别名实现的一方在选「不确定」时仍然显示 Q3b
    （读者填了专业，随后被 ``question_applies`` 判为条件不成立而丢掉）。
    ``value`` 只为「只认单值的老读者」保留，语义与 ``values[0]`` 相同。
    """
    rules: list[dict[str, Any]] = []
    for requirement in question.get("requires") or []:
        if not isinstance(requirement, dict):
            continue
        field_name = ANSWER_SLOTS.get(str(requirement.get("question") or ""), "")
        if "in" in requirement:
            rules.append(
                {"kind": "equals", "field": field_name, "values": [str(v) for v in requirement["in"]]}
            )
            continue
        excluded = [str(v) for v in (requirement.get("not_in") or [])]
        if not excluded:
            continue
        rules.append(
            {
                "kind": "notEquals",
                "field": field_name,
                "value": excluded[0],
                "values": excluded,
                # 前端语义：条件字段「还没答」时也算满足（Q3b 与 Q3a 同时出现，选了「没读大学」才收起）
                "includeEmpty": True,
            }
        )
    return rules


def _has_value(value: Any) -> bool:
    """前端 ``hasValue`` 的同一套口径（空数组 / 空串 / None 都算「没答」）。"""
    if isinstance(value, list):
        return bool(value)
    if value is None:
        return False
    return bool(str(value).strip())


def rule_holds(rule: dict[str, Any], answers: Any) -> bool:
    """按前端 ``evalWhen`` 的同一套语义求值一条条件规则。"""
    kind = rule.get("kind")
    if kind == "always":
        return True
    value = field_value(answers, str(rule.get("field") or ""))
    if kind == "equals":
        return value in (rule.get("values") or [])
    if kind == "notEquals":
        if not _has_value(value):
            return bool(rule.get("includeEmpty"))
        excluded = rule.get("values") or [rule.get("value")]
        return value not in excluded
    return True


def _with_hints(question: dict[str, Any]) -> dict[str, Any]:
    """给题目补一句给读者看的提示（``hint``）与输入框占位符（``placeholder``）。"""
    hints = {
        "Q2": "正好选 3 项；也可以选「没上过高中」或「记不清」。",
        "Q3b": "没读大学或不确定时这一题不出现。",
        "Q4": "可多选；也可以选「都没有」。",
        "Q5": "可多选；也可以选「很少读长文」。",
    }
    placeholders = {"Q3b": "例：国际经济与贸易", "Q4b": "例：读过《资本论》第一卷前两章"}
    if question["id"] in hints:
        question["hint"] = hints[question["id"]]
    if question["id"] in placeholders:
        question["placeholder"] = placeholders[question["id"]]
    return question


#: 固定问卷 v1（契约 §3 的六题九槽位，逐条对得上）；末尾统一补上渲染 / 校验用的别名
_QUESTION_DEFS: list[dict[str, Any]] = [
    _option_question("Q1", title="受教育程度", kind="single", options=EDUCATION_OPTIONS),
    _option_question(
        "Q2",
        title="高中选考科目（六选三）",
        kind="multi",
        options=HIGH_SCHOOL_ELECTIVES + [(v, ELECTIVE_EXEMPT_LABELS[v]) for v in ELECTIVE_EXEMPT],
        requires=[{"question": "Q1", "in": ["high_school", "college", "undergraduate", "master", "doctor"]}],
        note="正好 3 项；没上过高中 / 记不清与多选互斥",
    ),
    _option_question(
        "Q3a",
        title="大学学科门类",
        kind="single",
        options=DISCIPLINES + [(v, DISCIPLINE_EXTRA_LABELS[v]) for v in (DISCIPLINE_NONE, DISCIPLINE_UNSURE)],
        requires=[{"question": "Q1", "in": ["college", "undergraduate", "master", "doctor"]}],
    ),
    {
        "id": "Q3b",
        "title": "具体专业",
        "kind": "text",
        "options": [],
        "requires": [
            {"question": "Q1", "in": ["college", "undergraduate", "master", "doctor"]},
            {"question": "Q3a", "not_in": [DISCIPLINE_NONE, DISCIPLINE_UNSURE]},
        ],
        "answer_slot": ANSWER_SLOTS["Q3b"],
        "max_chars": MAX_TEXT_CHARS["major"],
        "note": "单行自由文本，可空",
    },
    _option_question(
        "Q4",
        title="自报特长 / 熟悉领域",
        kind="multi",
        options=STRENGTH_OPTIONS + [(STRENGTH_NONE, STRENGTH_EXTRA_LABELS[STRENGTH_NONE])],
        note="可多选；「都没有」与多选互斥",
    ),
    {
        "id": "Q4b",
        "title": "补充说明",
        "kind": "text",
        "options": [],
        "requires": [],
        "answer_slot": ANSWER_SLOTS["Q4b"],
        "multiline": True,
        "max_chars": MAX_TEXT_CHARS["strengths_note"],
        "note": "多行自由文本，可空",
    },
    _option_question(
        "Q5",
        title="常读材料类型",
        kind="multi",
        options=READING_MATERIALS + [(READING_RARELY, READING_EXTRA_LABELS[READING_RARELY])],
        note="可多选；「很少读长文」与多选互斥",
    ),
    _option_question("Q6", title="遇到看不懂时更希望怎么讲", kind="single", options=PREFERENCE_OPTIONS),
]

#: 固定问卷 v1（契约 §3 的六题八槽位，逐条对得上）：
#: 每题的派生字段（``qid`` / ``field`` / ``label`` / ``when`` / ``alsoWhen`` / 校验约束）都在这里统一补，
#: 前端直接拿 ``questions[]`` 就能渲染（不必回退到它自己的兜底题目）。
QUESTIONS: list[dict[str, Any]] = [
    _with_hints(_with_frontend_view(dict(question))) for question in _QUESTION_DEFS
]

QUESTION_BY_ID: dict[str, dict[str, Any]] = {q["id"]: q for q in QUESTIONS}


def question_by_id(qid: str) -> dict[str, Any] | None:
    """按题号取题目（未知题号返回 ``None``）。"""
    return QUESTION_BY_ID.get(qid)


def enum_labels(qid: str) -> dict[str, str]:
    """某题的 ``value → 中文标签``（素描与前端展示共用一份口径）。"""
    question = QUESTION_BY_ID.get(qid)
    if question is None:
        return {}
    return {item["value"]: item["label"] for item in question["options"]}


def field_value(answers: Any, field_name: str) -> Any:
    """从**扁平**作答或**画像形态**的背景块里取一个字段值。

    两种形态只差 ``university`` 这一层：作答（请求体 / 校验）里是 ``discipline``、``major``，
    落画像后收进 ``background.university``（契约 §6.1）。取值口径只写这一处，
    免得条件题规则与素描渲染各写一遍、改一层时漏掉一处。
    """
    if not isinstance(answers, dict):
        return None
    if field_name in UNIVERSITY_FIELDS:
        value = answers.get(field_name)
        if isinstance(value, str) and value.strip():
            return value  # 扁平写法（作答）
        return _university_of(answers).get(field_name)  # 画像形态（嵌套）
    return answers.get(field_name)


# ---------------------------------------------------------------- 条件题规则 --
def question_applies(qid: str, answers: dict[str, Any]) -> bool:
    """条件题规则**可复算**：答案满足 ``requires`` 才出现（后端独立算，不信任前端）。

    求的就是 ``condition_rules`` 从同一份 ``requires`` 派生的那套规则
    （``questions[].when`` / ``alsoWhen`` 是它的等价形态）——服务端判定与别名同源，
    不会出现「别名说有、判定说没有」。

    答案之间的依赖按**固定点**求：Q3a 在 Q1 上、Q3b 在 Q3a 上，所以从前往后扫一遍即可。
    """
    question = QUESTION_BY_ID.get(qid)
    if question is None:
        return False
    return all(rule_holds(rule, answers) for rule in condition_rules(question))


def visible_questions(answers: dict[str, Any]) -> list[str]:
    """当前作答下应当出现的题号（前端即时显隐的同一份规则）。"""
    return [q["id"] for q in QUESTIONS if question_applies(q["id"], answers)]


# -------------------------------------------------------------- 答案校验 --
def _normalize_multi(value: Any, qid: str, field_name: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise InputError(f"答案 {field_name} 必须是数组")
    picked: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise InputError(f"答案 {field_name} 里有非字符串项 {item!r}")
        item = item.strip()
        if item and item not in picked:
            picked.append(item)
    return picked


def _read_text(value: Any, field_name: str, limit: int, truncated: dict[str, bool]) -> str:
    if isinstance(value, str):
        text = normalize_text(value)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        text = str(value)
    else:
        raise InputError(f"答案 {field_name} 必须是字符串")
    if len(text) > limit:
        # 截断必须可见（契约 §11）：记进 truncated，并写进 survey.json 与响应体
        truncated[field_name] = True
        text = normalize_text(text[:limit])
    return text


def effective_answers(submitted: dict[str, Any]) -> dict[str, Any]:
    """挑出「条件成立」的槽位（条件不成立的一律丢掉，不报错、不写 null）。"""
    picked: dict[str, Any] = {}
    for field_name in dict.fromkeys(ANSWER_SLOTS.values()):
        if field_name in submitted and question_applies(field_name_to_question(field_name), submitted):
            picked[field_name] = submitted[field_name]
    return picked


def field_name_to_question(field_name: str) -> str:
    """反查：画像字段 → 题号（``strengths_note`` → ``Q4b``）。"""
    for qid, slot in ANSWER_SLOTS.items():
        if slot == field_name:
            return qid
    raise KeyError(field_name)


def validate_answers(
    submitted: Any,
) -> tuple[dict[str, Any], dict[str, bool], list[str]]:
    """校验一份提交的固定题作答 → ``(落画像的 background 字段, 截断标记, 降级说明)``。

    校验口径（契约 §3）：枚举闭合（非法即 ``InputError`` → HTTP 400）、Q2 除豁免值外**正好 3 项**、
    互斥值不许与别的选项同选、自由文本超长截断并标 ``truncated``（降级可见，不许静默截断）。
    条件不满足的字段直接丢掉（前端被绕过时不至于把没问过的字段写进画像）。
    """
    if not isinstance(submitted, dict):
        raise InputError("答案（answers）必须是 JSON 对象")
    unknown = [key for key in submitted if key not in ANSWER_SLOTS.values()]
    if unknown:
        raise InputError(f"答案里有未知字段 {'、'.join(sorted(unknown))}（可用字段：{'、'.join(ANSWER_SLOTS.values())}）")

    degraded: list[str] = []
    truncated: dict[str, bool] = {}
    answers: dict[str, Any] = {}
    for field_name in dict.fromkeys(ANSWER_SLOTS.values()):
        if field_name not in submitted:
            continue
        value = submitted[field_name]
        if value is None:
            continue
        qid = field_name_to_question(field_name)
        # 条件题：条件不成立时该字段不写进画像（不是写 null），也不报错
        if not question_applies(qid, submitted):
            degraded.append(f"{qid}（{SHORT_LABELS[qid]}）的作答已忽略：这道题在当前作答下不成立，不写进画像")
            continue
        if qid in MUTUALLY_EXCLUSIVE:
            picked = _normalize_multi(value, qid, field_name)
            labels = enum_labels(qid)
            for item in picked:
                if item not in labels:
                    raise InputError(
                        f"答案 {field_name} 里的 {item!r} 不在 {qid} 的选项内（只能是 {' / '.join(labels)}）"
                    )
            exclusive = [item for item in picked if item in MUTUALLY_EXCLUSIVE[qid]]
            if exclusive and len(picked) > 1:
                raise InputError(f"答案 {field_name} 里 {exclusive[0]!r} 不能与其它选项同时选（{qid}）")
            if qid == "Q2" and not exclusive and len(picked) != 3:
                # 选了豁免值（没上过高中 / 记不清）时不再要求正好 3 项（契约 §3）
                raise InputError(f"答案 {field_name} 必须正好 3 项（六选三），实际 {len(picked)} 项")
            if picked:
                answers[field_name] = picked
            continue
        if qid in ("Q3b", "Q4b"):
            text = _read_text(value, field_name, MAX_TEXT_CHARS[field_name], truncated)
            if text:
                answers[field_name] = text
            continue
        if not isinstance(value, str) or value.strip() not in enum_labels(qid):
            raise InputError(f"答案 {field_name}={value!r} 不在 {qid} 的选项内（只能是 {' / '.join(enum_labels(qid))}）")
        answers[field_name] = value.strip()

    for field_name in truncated:
        qid = field_name_to_question(field_name)
        # 报错要点名「哪一题 + 哪个字段」（字段名是英文稳定串，机器可读）
        degraded.append(
            f"{qid}（{field_name}）超过 {MAX_TEXT_CHARS[field_name]} 字符，已截断（truncated）"
        )
    return answers, truncated, degraded


# ------------------------------------------------------------------ 素描渲染 --
def _single_label(qid: str, value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return enum_labels(qid).get(value)


def _list_labels(qid: str, value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    labels = enum_labels(qid)
    return [labels[item] for item in value if isinstance(item, str) and item in labels]


def _followup_lines(followups: Any) -> list[str]:
    """追问摘要的**逐条**文本（``问题→答案``，未答写「未答」）。

    这里**不截断**：超预算时由 ``render_sketch`` 从最后一条往前**整条**丢（契约 §5.2：
    不许把一项截成半句），并写「追问摘要省略 N 条」。
    """
    if not isinstance(followups, list):
        return []
    lines: list[str] = []
    for item in followups:
        if not isinstance(item, dict):
            continue
        q = str(item.get("q") or "").strip()
        if not q:
            continue
        a = str(item.get("a") or "").strip()
        lines.append(f"{q}→{a or '未答'}")
    return lines


#: 素描里各段的**渲染顺序**（契约 §5.1：学历 → 学科门类 → 专业 → 六选三 → 自报领域 →
#: 常读材料 → 偏好讲法 → 补充说明），追问摘要与「未提供：X」排在最后。
SKETCH_ORDER = ("Q1", "Q3a", "Q3b", "Q2", "Q4", "Q5", "Q6", "Q4b")


def render_sketch(background: Any) -> str:
    """把 ``background`` 渲染成读者素描（逐字稳定：同画像必得同文本）。

    纪律（契约 §5）：

    - **渲染顺序固定**（``SKETCH_ORDER``）：学历 → 学科门类 → 专业 → 六选三 → 自报领域 →
      常读材料 → 偏好讲法 → 补充说明 → 「未提供：X」逐项 → 追问摘要。
      空项选了**统一收尾部**这一支：答过的项按 ``SKETCH_ORDER`` 排完再把空项接在后面，
      但空项块内部**也照 §5.1 的次序**（用 ``SKETCH_ORDER`` 而不是 ``ANSWER_SLOTS``，
      否则「未提供：补充说明」会插到「未提供：常读材料 / 偏好讲法」前面）；
    - **空项措辞固定**：``未提供：X``；整场跳过 → 「未提供（读者跳过了背景问卷）」；
    - **整段 ≤ ``MAX_SKETCH_CHARS`` 字符**，两级让步：
      ① 按 ``DROPPABLE_ORDER``（先 Q5 常读 → Q6 偏好讲法 → Q2 六选三 → 最后 Q4b 补充说明）
      **整项**丢弃，标记「…（已省略 N 项）」，丢弃集合一定是它的**前缀**；
      ② 还装不下就把追问摘要**从最后一条往前整条**丢，标记「…（追问摘要省略 N 条）」——
      追问摘要**不按字符截断**（截半句会让「同画像同文本」失效）；
      两条都让完了还超（核心项自己就超）才算 bug，直接报错。
      核心项 = 学历 / 学科门类（含专业）/ 专业（门类缺失或为「没读大学、不确定」时单独成项）/
      自报领域 —— 专业的两种形态都不参与丢弃；
    - 不含时间戳 / 随机数 / 段号（否则前缀缓存与回归基线都守不住）。
    """
    if not isinstance(background, dict) or not background:
        return EMPTY_BACKGROUND
    university = _university_of(background)

    # ---------------------------------------------------------- 各段文本 --
    education = _single_label("Q1", background.get("education"))
    discipline_value = university.get("discipline")
    # 「没读大学 / 不确定」不是门类：不写成「大学为没读大学门类」（Q3b 按条件题规则也不该有）
    discipline = (
        None
        if discipline_value in (DISCIPLINE_NONE, DISCIPLINE_UNSURE)
        else _single_label("Q3a", discipline_value)
    )
    major_text = university.get("major")
    major = normalize_text(major_text) if isinstance(major_text, str) and major_text.strip() else ""
    if discipline and major:
        # 门类与专业都有：合成一项（契约 §5 的示例形态），位置仍是「学科门类 → 专业」
        discipline_text = f"大学为{discipline}门类（专业：{major}）"
        major_own_text = ""
    else:
        discipline_text = f"大学为{discipline}门类" if discipline else ""
        major_own_text = f"专业：{major}" if major else ""
    strengths = _list_labels("Q4", background.get("strengths"))
    strengths_text = "自报熟悉领域：" + "、".join(strengths) if strengths else ""
    electives = _list_labels("Q2", background.get("high_school_electives"))
    exempt = [
        ELECTIVE_EXEMPT_LABELS[item]
        for item in (background.get("high_school_electives") or [])
        if isinstance(item, str) and item in ELECTIVE_EXEMPT_LABELS
    ]
    if electives:
        electives_text = "高中选考" + "、".join(electives)
    elif exempt:
        electives_text = "高中选考：" + "、".join(exempt)
    else:
        electives_text = ""
    materials = _list_labels("Q5", background.get("reading_materials"))
    materials_text = "常读：" + "、".join(materials) if materials else ""
    preference = _single_label("Q6", background.get("preference"))
    preference_text = f"遇到看不懂时希望：{preference}" if preference else ""
    note = normalize_text(background.get("strengths_note")) if background.get("strengths_note") else ""
    note_text = f"补充：{note}" if note else ""

    # 可丢项（``DROPPABLE_ORDER`` 就是丢弃优先级，先丢 Q5）
    optional: dict[str, str] = {
        "Q2": electives_text,
        "Q5": materials_text,
        "Q6": preference_text,
        "Q4b": note_text,
    }
    optional = {qid: text for qid, text in optional.items() if text}
    # 核心项（不参与丢弃）：学历 / 学科门类（含专业）/ **专业（门类缺失时单独成项）** / 自报领域
    core: dict[str, str] = {
        "Q1": education or "",
        "Q3a": discipline_text,
        "Q3b": major_own_text,
        "Q4": strengths_text,
    }
    followup_lines = _followup_lines(background.get("followups"))
    missing = [f"未提供：{SHORT_LABELS[qid]}" for qid in SKETCH_ORDER if _should_advertise(qid, background)]

    # 完全没答：整场跳过与「空背景（老画像 / 只有版本号）」两种措辞分得开
    if not any(core.values()) and not optional and not followup_lines:
        return SKIPPED_TEXT if background.get("skipped") else EMPTY_BACKGROUND

    def summary_piece(lines: list[str], omitted: int) -> str | None:
        """追问摘要这一块：留下的整条 + （丢了就写）「追问摘要省略 N 条」。"""
        text = "；".join(lines)
        if omitted:
            marker = f"…（追问摘要省略 {omitted} 条）"
            return f"{text}；{marker}" if text else marker
        return text or None

    def assemble(kept: set[str], lines: list[str], omitted: int) -> str:
        # 一趟走完 SKETCH_ORDER：核心项与保留的可选项都按契约的字段顺序落位
        pieces: list[str] = []
        for qid in SKETCH_ORDER:
            if core.get(qid):
                pieces.append(core[qid])
            elif qid in kept and optional.get(qid):
                pieces.append(optional[qid])
        pieces += missing
        piece = summary_piece(lines, omitted)
        if piece:
            pieces.append(piece)
        dropped = sum(1 for qid in DROPPABLE_ORDER if qid in optional and qid not in kept)
        text = "；".join(pieces)
        if dropped:
            text += f"；…（已省略 {dropped} 项）"
        return text + "。"

    # ① 先按契约顺序决定丢弃集合：装不下就从 Q5 起一项一项丢，丢到装得下为止
    kept = set(optional)
    lines = list(followup_lines)
    omitted = 0
    text = assemble(kept, lines, omitted)
    for qid in DROPPABLE_ORDER:
        if len(text) <= MAX_SKETCH_CHARS:
            break
        if qid in kept:
            kept.discard(qid)
            text = assemble(kept, lines, omitted)
    # ② 还装不下就把追问摘要**从最后一条往前整条**丢（不许截半句）
    while len(text) > MAX_SKETCH_CHARS and lines:
        lines.pop()
        omitted += 1
        text = assemble(kept, lines, omitted)
    if len(text) > MAX_SKETCH_CHARS:
        # 走不到这里（核心项 + 「未提供」行 + 省略标记的上限远小于 400）：
        # 真到了这一步就是「越界即 bug」，绝不悄悄超上限。
        raise ValueError(
            f"读者素描超出上限：{len(text)} > {MAX_SKETCH_CHARS}（当前选项文本下不应发生，属实现缺陷）"
        )
    return text


def _should_advertise(qid: str, background: dict[str, Any]) -> bool:
    """该题要不要在素描里写「未提供：X」。

    条件题没出现（没上过高中、Q3a 选了「没读大学」）时**不写**——那不是「读者没答」，
    是「这道题按规则压根没问」；写上去会与作答自相矛盾。
    """
    if not question_applies(qid, background):
        return False
    slot = ANSWER_SLOTS[qid]
    value = field_value(background, slot)
    if slot in MULTI_ANSWER_FIELDS:
        return not (isinstance(value, list) and value)
    return not (isinstance(value, str) and value.strip())


def sketch_fingerprint(sketch: str) -> str:
    """素描文本的 8 位 sha256 哈希（``meta.background_fingerprint``）。"""
    return hashlib.sha256(sketch.encode("utf-8")).hexdigest()[:8]


# ------------------------------------------------------- 画像里的 background --
def has_fixed_answers(profile: Any) -> bool:
    """固定题有没有作答（条件不成立的字段不算：前端被绕过时也不会把没问过的字段当信号）。"""
    return bool(effective_answers(background_of(profile)))


def has_followups(profile: Any) -> bool:
    """有没有**非空**的追问摘要（问过但全留空不算）。"""
    return bool(_followup_lines(background_of(profile).get("followups")))


def has_background(profile: Any) -> bool:
    """有没有可用的背景信号（契约 §6.4：背景非空即视为「有画像」）。

    固定题答过**或**有非空追问摘要都算：只认固定题会让「跳过问卷、但答了模型追问」的读者
    素描进了提示词、产物却写 ``profile_used: false`` + 「无画像模式」——正是 §6.4 要防的那类
    「看起来做了其实没做」。
    ``skipped: true`` 本身不否决（它只描述「读者跳过了固定问卷」这个事实），
    但跳过后既没答固定题、又没有追问摘要时仍然算「没有背景」（``effective_answers`` 为空）。
    """
    background = background_of(profile)
    if not background:
        return False
    return has_fixed_answers(profile) or has_followups(profile)


def answered_version(profile: Any) -> int | None:
    """画像里「读者上次答题时的问卷版本」；没答过 / 画像为空 / 整场跳过 → ``None``。

    给 ``GET /api/survey`` 的顶层 ``answered_version`` 用（契约 §11 的「问卷已更新」提示靠它）：
    它描述的是**读者提交过的答案**，所以「只有 skipped 标志」（没答任何固定题）也算没答过。
    """
    background = background_of(profile)
    if not background or not has_fixed_answers(profile):
        return None
    version = background.get("version")
    if isinstance(version, bool):
        return None
    try:
        return int(version)  # 手写成字符串 "1" 也认
    except (TypeError, ValueError):
        return None


def has_concept_signals(profile: Any) -> bool:
    """有没有概念自评 / 手动推翻（``profile_used`` 的旧口径）。"""
    if not isinstance(profile, dict):
        return False
    return bool(profile.get("concepts") or profile.get("overrides"))


def normalize_background(value: Any) -> dict[str, Any]:
    """把任意输入整理成背景块（非法输入 → 空字典；不抛错，用于读取路径）。

    键顺序按契约 §6.1：``version`` / ``answered_at`` / ``education`` / ``high_school_electives`` /
    ``university``（嵌套 discipline + major）/ ``strengths`` / ``strengths_note`` /
    ``reading_materials`` / ``preference`` / ``followups`` / ``skipped``。
    """
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, Any] = {}
    version = value.get("version")
    normalized["version"] = int(version) if isinstance(version, int) and not isinstance(version, bool) else SURVEY_VERSION
    answered_at = value.get("answered_at")
    if isinstance(answered_at, str) and answered_at.strip():
        normalized["answered_at"] = answered_at.strip()
    education = value.get("education")
    if isinstance(education, str) and education.strip():
        normalized["education"] = education.strip()
    electives = value.get("high_school_electives")
    if isinstance(electives, list):
        kept_electives = [item.strip() for item in electives if isinstance(item, str) and item.strip()]
        if kept_electives:
            normalized["high_school_electives"] = kept_electives
    # 大学两项收在 ``university`` 下（契约 §6.1 的 YAML 是嵌套的）
    university = _university_of(value)
    nested: dict[str, Any] = {}
    discipline = university.get("discipline")
    if isinstance(discipline, str) and discipline.strip():
        nested["discipline"] = discipline.strip()
    major = university.get("major")
    if isinstance(major, str) and major.strip():
        nested["major"] = normalize_text(major)
    if nested:
        normalized["university"] = nested
    strengths = value.get("strengths")
    if isinstance(strengths, list):
        kept_strengths = [item.strip() for item in strengths if isinstance(item, str) and item.strip()]
        if kept_strengths:
            normalized["strengths"] = kept_strengths
    strengths_note = value.get("strengths_note")
    if isinstance(strengths_note, str) and strengths_note.strip():
        normalized["strengths_note"] = normalize_text(strengths_note)
    reading_materials = value.get("reading_materials")
    if isinstance(reading_materials, list):
        kept_materials = [item.strip() for item in reading_materials if isinstance(item, str) and item.strip()]
        if kept_materials:
            normalized["reading_materials"] = kept_materials
    preference = value.get("preference")
    if isinstance(preference, str) and preference.strip():
        normalized["preference"] = preference.strip()
    followups = value.get("followups")
    if isinstance(followups, list):
        kept_followups = [
            {
                "q": str(item.get("q") or "").strip(),
                "a": str(item.get("a") or "").strip(),
                "at": str(item.get("at") or ""),
            }
            for item in followups
            if isinstance(item, dict) and str(item.get("q") or "").strip()
        ]
        if kept_followups:
            normalized["followups"] = kept_followups
    if value.get("skipped"):
        normalized["skipped"] = True
    return normalized


def _university_of(background: Any) -> dict[str, Any]:
    """取 ``background.university``（缺 / 不是映射 → 空字典）。

    兜底认**扁平**写法（``background.discipline`` / ``background.major``）：契约 §6.1 是嵌套的，
    但 ``finalize_background`` 拼出来的中间形态、以及手改过的老文件都可能是扁平的，
    读的时候一起认，写盘一律收进 ``university``。
    """
    if not isinstance(background, dict):
        return {}
    value = background.get("university")
    nested = dict(value) if isinstance(value, dict) else {}
    for field_name in UNIVERSITY_FIELDS:
        if field_name not in nested:
            fallback = background.get(field_name)
            if isinstance(fallback, str) and fallback.strip():
                nested[field_name] = fallback.strip()
    return nested


def answers_of(background: Any) -> dict[str, Any]:
    """把背景块翻回「题号视角」的作答（`GET /api/survey` 的 ``answers``）。

    对外的作答一直是**扁平**字段名（``discipline`` / ``major``）；``university`` 只是画像里的
    收纳层，读回来时摊平。
    """
    background = background if isinstance(background, dict) else {}
    answers: dict[str, Any] = {}
    for field_name in dict.fromkeys(ANSWER_SLOTS.values()):
        value = field_value(background, field_name)
        if value in (None, "", []):
            continue
        answers[field_name] = value
    return answers


def finalize_background(
    answers: dict[str, Any],
    *,
    skipped: bool = False,
    followups: list[dict[str, Any]] | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    """拼出落画像的 ``background``：**扁平**作答 → 扁平背景块（``version`` / ``answered_at`` 一定在）。

    这里只管「哪些字段有值」，不管收纳层级——``university`` 的嵌套与键顺序统一交给
    ``normalize_background``（服务端写盘前会调它，见 ``ar.server.post_survey``），
    这样「拼装」与「落画像形状」只有一个地方定义。
    """
    background: dict[str, Any] = {"version": SURVEY_VERSION, "answered_at": at or now_iso()}
    if skipped:
        background["skipped"] = True
        return background
    for field_name in dict.fromkeys(ANSWER_SLOTS.values()):
        value = answers.get(field_name)
        if value in (None, "", []):
            continue
        if field_name in MULTI_ANSWER_FIELDS:
            background[field_name] = list(value)
            continue
        background[field_name] = str(value)
    if followups:
        background["followups"] = [
            {"q": str(item.get("q") or ""), "a": str(item.get("a") or ""), "at": str(item.get("at") or "")}
            for item in followups
            if isinstance(item, dict) and str(item.get("q") or "").strip()
        ]
    return background


def merge_followup_answers(background: dict[str, Any], followups: Any) -> list[dict[str, Any]]:
    """把读者对追问的作答按 ``{q, a, at}`` 并回 ``background.followups``（q 相同的按位置覆盖答案）。"""
    existing = [dict(item) for item in (background.get("followups") or []) if isinstance(item, dict)]
    if not isinstance(followups, list):
        return existing
    stamp = now_iso()
    for position, item in enumerate(followups):
        if not isinstance(item, dict):
            continue
        q = str(item.get("q") or (existing[position].get("q") if position < len(existing) else "")).strip()
        if not q:
            continue
        a = str(item.get("a") or "").strip()
        at = str(item.get("at") or stamp)
        entry = {"q": q, "a": a, "at": at}
        if position < len(existing) and existing[position].get("q") == q:
            existing[position] = entry
        else:
            same = next((i for i, old in enumerate(existing) if old.get("q") == q), None)
            if same is None:
                existing.append(entry)
            else:
                existing[same] = entry
    return existing


def merge_followups_into(background: dict[str, Any], followups: Any) -> dict[str, Any]:
    """把追问问答并进背景块，返回**重新归一**过的背景（键顺序仍照契约 §6.1）。

    ``POST /api/survey`` 的 ``followups`` 与 ``POST /api/survey/followup`` 都走这里：
    固定题作答与追问共用同一个 ``background``，谁都不许把对方抹掉。
    """
    merged = dict(background)
    merged["followups"] = merge_followup_answers(background, followups)
    return normalize_background(merged)


# ------------------------------------------------------------------ 落盘 --
def survey_record_path(profiles_dir: Path | str, reader: str) -> Path:
    """``profiles/<reader>/survey.json``（不进版本库，也不参与缓存指纹）。"""
    from .paths import check_reader_id

    return Path(profiles_dir) / check_reader_id(reader) / "survey.json"


def read_survey_record(profiles_dir: Path | str, reader: str) -> dict[str, Any]:
    """读问卷记录文件（不存在 / 不是对象 → 空字典）。"""
    payload = read_json_if_exists(survey_record_path(profiles_dir, reader))
    return payload if isinstance(payload, dict) else {}


def write_survey_record(
    profiles_dir: Path | str,
    reader: str,
    *,
    answers: dict[str, Any] | None = None,
    background: dict[str, Any],
    truncated: dict[str, bool] | None = None,
    skipped: bool | None = None,
    followup_meta: dict[str, Any] | None = None,
    at: str | None = None,
) -> Path:
    """写 ``survey.json``：问卷版本 / 原始作答 / 落画像后的背景 / 追问的问答与调用元数据。

    ``answers`` 是**提交的原值**（截断前的自由文本也在），这样「正好 60 字符」与
    「被截断到 60 字符」在文件里分得开；落画像的形态另存 ``background``。
    ``answers`` / ``truncated`` / ``skipped`` 传 ``None`` 表示**本次不改这几项**（例如只记一次
    追问调用的 token 账时，不该把固定题的原始作答与截断标记抹掉）。
    """
    record = read_survey_record(profiles_dir, reader)
    effective_skipped = bool(background.get("skipped")) if skipped is None else (bool(skipped) or bool(background.get("skipped")))
    if answers is not None and not answers and not effective_skipped:
        # 空提交（前端被绕过 / 直接 POST {}）不许把已有答案抹掉
        raise InputError("answers 是空的：要么给出固定题作答，要么显式 skipped=true（不会清空已有背景）")
    record.update(
        {
            "reader": reader,
            "version": SURVEY_VERSION,
            "updated_at": at or now_iso(),
            "background": background,
        }
    )
    if skipped is not None:
        record["skipped"] = effective_skipped
    if answers is not None:
        record["answers"] = answers
    if truncated is not None:
        record["truncated"] = dict(truncated)
    if followup_meta:
        history = [item for item in (record.get("followup_history") or []) if isinstance(item, dict)]
        history.append(dict(followup_meta))
        record["followup_history"] = history
        record["followup"] = dict(followup_meta)
    return write_json(survey_record_path(profiles_dir, reader), record)


def save_background(
    profiles_dir: Path | str,
    reader: str,
    background: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """把背景块写进 ``profiles/<reader>/profile.yaml`` 的顶层键 ``background``。

    画像文件不存在时按空骨架创建（首次提交即建档）；其余顶层键一个不动。
    返回 ``(写回后的画像, 降级说明)``。
    """
    profile, notes = load_profile(profiles_dir, reader)
    degraded: list[str] = []
    if notes:
        # 只说「文件不存在」，不把机器上的绝对路径写进产物（产物要能跨机器对照）
        degraded.append(f"画像文件 profiles/{reader}/profile.yaml 原本不存在：本次提交顺带创建了它")
    updated = dict(profile)
    updated["background"] = background
    save_profile(profiles_dir, reader, updated)
    return updated, degraded


def append_followups(
    profiles_dir: Path | str,
    reader: str,
    followups: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """把追问的问答并进已有背景块（``answered_at`` 不动，避免每答一次就换指纹）。"""
    profile, notes = load_profile(profiles_dir, reader)
    background = dict(background_of(profile))
    if not background:
        raise InputError(
            f"profiles/{reader}/profile.yaml 里没有背景（background）："
            "请先提交固定问卷，再让模型追问（POST /api/survey）"
        )
    background["followups"] = merge_followup_answers(background, followups)
    updated = dict(profile)
    updated["background"] = background
    save_profile(profiles_dir, reader, updated)
    return updated, list(notes)


# ---------------------------------------------------------------- 模型追问 --
@dataclass
class SurveyOutcome:
    """一次「模型追问」的结果；``degraded=True`` 时 ``questions`` 为空、``reason`` 说明原因。"""

    questions: list[dict[str, str]] = field(default_factory=list)
    degraded: bool = False
    reason: str | None = None
    text: str = ""
    model: str | None = None
    prompt_version: str = SURVEY_PROMPT_VERSION
    usage: dict[str, Any] = field(default_factory=dict)
    #: 这次调用的逐次尝试账（追加字段）：HTTP 层把它写进 ``survey.json`` 的追问记账里
    attempts: list[dict[str, Any]] = field(default_factory=list)


def _clean_question(item: Any) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    q = str(item.get("q") or "").strip()
    hint = str(item.get("hint") or "").strip()
    if not q:
        return None
    return {"q": q[:200], "hint": hint[:200] or "一句话说说你的情况就行"}


def followup_questions(raw_text: str) -> list[dict[str, str]]:
    """把模型输出整理成 ``[{q, hint}]``；不是 3–5 条就抛 ``InputError``（调用方转可见降级）。"""
    from .analyze import extract_json_object

    data = extract_json_object(raw_text)
    items = data.get("questions")
    if not isinstance(items, list):
        raise InputError("模型输出里 questions 不是数组")
    questions = [item for item in (_clean_question(raw) for raw in items) if item]
    if not 3 <= len(questions) <= 5:
        raise InputError(f"模型给了 {len(questions)} 个问题，要求 3–5 个")
    return questions


def outcome_from_tool_run(run: Any) -> SurveyOutcome:
    """把工具层的一次调用（``ar.tools.ToolRun``）整理成 ``SurveyOutcome``。

    「模型没答上来」与「答了但输出不合要求」是两条不同的降级：前者 ``model_error``、
    后者 ``bad_reply``（判据是底层结果 ``ok``，见 ``ToolRun.model_replied``，不靠文案猜）。
    """
    attempts = list(run.attempt_records)
    if not run.ok:
        # 面向用户的失败文案统一走 ``ar.llm.failure_message``（带尝试次数与末次错误码）
        message = run.failure_message()
        if run.model_replied:
            return SurveyOutcome(
                questions=[],
                degraded=True,
                reason="bad_reply",
                text=f"模型返回的追问格式不合要求：{message}；固定题答案不受影响，可稍后再试。",
                model=run.model,
                attempts=attempts,
            )
        return SurveyOutcome(
            questions=[],
            degraded=True,
            reason="model_error",
            text=f"未能生成追问：{message}；固定题答案已保存，不受影响。",
            attempts=attempts,
        )
    data = run.data if isinstance(run.data, dict) else {}
    questions = [dict(item) for item in (data.get("questions") or []) if isinstance(item, dict)]
    if not questions:
        return SurveyOutcome(
            questions=[],
            degraded=True,
            reason="empty_reply",
            text="模型没有给出追问，固定题答案已保存，不受影响。",
            model=run.model,
            attempts=attempts,
        )
    return SurveyOutcome(
        questions=questions,
        model=run.model,
        usage=dict(run.usage or {}),
        attempts=attempts,
    )


async def asurvey_followups(
    background: dict[str, Any],
    *,
    client: Any,
    reader: str = DEFAULT_READER,
) -> SurveyOutcome:
    """让模型按固定题作答追问 3–5 个细节（**不读材料正文**）。

    失败 / 超时 / 未配置一律返回 ``degraded=True`` + 说明文本，绝不抛给 HTTP 层；
    固定题答案的保存完全不受这里影响（契约 §4 / §11）。

    模型调用经工具层 ``survey_followups``（拼提示词 / 重试 / 记账都在那边），并且**丢到线程里**跑：
    工具的 ``execute`` 是同步入口（内部 ``asyncio.run``），在本函数所在的事件循环里直接调会嵌套报错。
    """
    if client is None or not getattr(client, "configured", False):
        return SurveyOutcome(
            questions=[],
            degraded=True,
            reason="model_not_configured",
            text=(
                "模型未配置，暂时不能生成追问。设置环境变量 AR_API_KEY，或在 config.local.json 里填 "
                "api_key，再点一次即可（背景已保存，不受影响）。"
            ),
        )
    sketch = render_sketch(background)
    params = {"sketch": sketch, "answers": answers_of(background)}
    run = await asyncio.to_thread(tools.run_tool, "survey_followups", params, client=client)
    return outcome_from_tool_run(run)
