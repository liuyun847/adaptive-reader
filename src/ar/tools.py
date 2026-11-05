"""模型工具层（PLAN-llm-tools §3.3 + PLAN-actions-feedback §8）：六个具名工具的注册表与执行。

工具把「拼 messages → 调模型（含重试与记账） → 解析成结构」收成一步；
**校验与落盘仍留在原业务模块**（``validate_inventory`` / ``decisions.json`` 之类一概不搬）。

- 提示词**逐字复用** ``ar.prompts`` 的六份模板与六个版本号（一个字都不改），
  工具只负责把参数拼成 messages；
- 工具的入参 / 出参是 JSON Schema，``ar tools``（CLI）直接列举它们；
- 统一信封（所有 ``execute`` 的返回）::

      {"ok": bool,
       "data": {...} | None,          # ok=true 时是出参 schema 描述的结构
       "failure": {...} | None,       # code / message / status / retry_after_ms
       "attempts": [{tool, try, elapsed_ms, code, delay_ms, retry_after_used}, ...],
       "usage": {...} | None,
       "raw_text": str,               # 模型原文：解析失败时调用方要落 concepts.raw.txt 之类
       "degraded": [str, ...]}        # 人读的降级说明（含「stream_options 被拒」这类参数降级）

  后两个键是**追加字段**（§3.3 只列了前五个）：``raw_text`` 是 ``analyze`` 迁移后仍要落
  ``concepts.raw.txt`` 的唯一来源；``degraded`` 是 ``rewrite`` 迁移后记 ``meta.degraded`` 的来源。

**入参不合契约不抛异常**（2026-10-13 复核修复）：缺必填字段 / 有未知字段 / 类型不对一律返回
**同一形状的失败信封**，码用 ``INVALID_REQUEST``、``degraded`` 点名是哪个参数不合法。
抛 ``InputError`` 会让 HTTP 层把「调用方的 bug」报成 400（把内部错误伪装成客户端输入错误）、
其它异常报成 500，与「失败不抛」的契约冲突；现在与其他失败一样走返回值。
调用方要面向用户的失败文案时用 ``failure_message()``（唯一实现在 ``ar.llm.failure_message``）。

调用方式：``get("analyze_concepts").execute(params, client=client, policy=policy)``。
``client`` / ``policy`` 是**关键字注入**（规格书写的是 ``Callable[[dict], dict]``，
这里放宽成 ``Callable[..., dict]``）：业务模块本来就把 ``client`` 一路传进来，
显式注入既不用全局可变状态，也不会在同进程并发跑多批改写时串味；
不传 ``client`` 时返回 ``NOT_CONFIGURED`` 的失败信封（不发请求）。

需要 ``meta.model`` / ``attempts`` 这类**信封里装不下**的底层字段时用 ``run_tool(name, params, ...)``：
它与 ``execute`` 同一次调用，只是额外把 ``LlmResult`` 带出来（见 ``ToolRun``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .errors import InputError
from .fidelity import DECLARATION_FIELD, DEFAULT_FACT_ANCHOR_POLICY
from .llm import (
    UNKNOWN_CODE,
    UNKNOWN_MESSAGE,
    LlmAttempt,
    LlmFailure,
    LlmRequest,
    LlmResult,
    RetryPolicy,
    attempt_records,
    not_configured_result,
    retry_account,
)
from .llm import call as llm_call
from .llm import failure_message as llm_failure_message
from .prompts import (
    COMPRESS_PROMPT_VERSION,
    CONCEPTS_PROMPT_VERSION,
    DEFAULT_SELECTION_FROM,
    EXPLAIN_KINDS,
    EXPLAIN_PROMPT_VERSION,
    PLAN_PROMPT_VERSION,
    REWRITE_PROMPT_VERSION,
    SELECTION_SOURCES,
    SURVEY_PROMPT_VERSION,
    build_compress_messages,
    build_concepts_messages,
    build_explain_messages,
    build_plan_messages,
    build_rewrite_messages,
    build_survey_messages,
)

__all__ = [
    "PARSE_FAILURE_CODE",
    "INVALID_PARAMS_CODE",
    "ToolSpec",
    "ToolRun",
    "ANALYZE_CONCEPTS_SPEC",
    "REWRITE_BATCH_SPEC",
    "PLAN_ACTIONS_SPEC",
    "EXPLAIN_PARAGRAPH_SPEC",
    "SURVEY_FOLLOWUPS_SPEC",
    "COMPRESS_PARAGRAPH_SPEC",
    "register",
    "get",
    "all_tools",
    "schemas",
    "schema_problems",
    "run_tool",
]

#: 工具内部解析失败时用的错误码：``UNKNOWN`` 是 §3.2 表里的「其它」，**不重试**
#: （业务模块按自己的路径处理：analyze 落 raw + 退出码 5、rewrite 记 model_errors + 6、survey 降级）。
#: §3.2 的表里没有「输出不合法」这个码，所以不另造码，直接用兜底的 ``UNKNOWN``。
PARSE_FAILURE_CODE = UNKNOWN_CODE
#: 入参不合契约时用的错误码（§3.2 表里的「调用方给的参数不合法」，**不重试**）：
#: 与「模型失败」区分得开，又不必新造码——形状与其他失败信封完全一致。
INVALID_PARAMS_CODE = "INVALID_REQUEST"


# ------------------------------------------------------------------ 入参/出参 --
_PARAGRAPH_LIST = {
    "type": "array",
    "minItems": 1,
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "text"],
        "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
    },
}

_CONCEPT = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id",
        "name",
        "surface",
        "prereq",
        "difficulty_hint",
        "paragraphs",
        "domain",
        "concept_key",
    ],
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "surface": {"type": "array", "items": {"type": "string"}},
        "prereq": {"type": "array", "items": {"type": "string"}},
        "difficulty_hint": {"type": ["integer", "null"], "minimum": 1},
        "paragraphs": {"type": "array", "items": {"type": "string"}},
        "domain": {"type": "string"},
        "concept_key": {"type": "string"},
    },
}

_ILLUSTRATION = {
    "type": "object",
    "additionalProperties": False,
    "required": ["paragraph", "kind", "why"],
    "properties": {"paragraph": {"type": "string"}, "kind": {"type": "string"}, "why": {"type": "string"}},
}

#: ``illustrate`` 段的图定义（PLAN-actions-feedback §4.1）：``kind`` + ``source`` 必填，``caption`` 可缺
_FIGURE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "source"],
    "properties": {
        "kind": {"type": "string", "enum": ["mermaid", "svg"]},
        "source": {"type": "string"},
        "caption": {"type": "string"},
    },
}

#: 事实锚申报（§14.1）：``{numbers, quotes, books, note?}``——每段可选，只在真的放弃了锚点时才给。
#: ⚠ 与 ``_FIGURE`` 同一条口径：模型输出**不经**这里的 schema 校验（它只校验调用方入参），
#: 所以 ``ar.rewrite._normalize_plan_items`` 必须同步带上 ``fact_declarations``，否则静默丢弃。
_FACT_DECLARATIONS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "numbers": {"type": "array", "items": {"type": "string"}},
        "quotes": {"type": "array", "items": {"type": "string"}},
        "books": {"type": "array", "items": {"type": "string"}},
        "note": {"type": "string"},
    },
}

_DECISION = {
    "type": "object",
    "additionalProperties": False,
    "required": ["paragraph", "action", "why", "confidence", "rewrite"],
    "properties": {
        "paragraph": {"type": "string"},
        "action": {"type": "string"},
        "why": {
            "type": "object",
            "additionalProperties": False,
            "required": ["profile", "note"],
            "description": (
                "这一段的依据。profile 是**引用**、note 是**说明**："
                "profile 有自评时必须逐条保留模型给的引用（形如 c03:unknown）；"
                "没有自评可引用时空数组是合法值；读者背景的依据只写 note，不写进 profile。"
            ),
            "properties": {
                "profile": {
                    "type": "array",
                    "items": {"type": "string"},
                    # 字段语义（rewrite-v6 起，2026-10-14 复核修复）：这条不是文档装饰——
                    # 产出链路按同一口径校验 / 兜底（ar.rewrite.run_rewrite），
                    # 「模型漏键 / 给错形状 / 该给引用却给空数组」一律可见，不静默丢。
                    "description": (
                        "读者自评 / 画像的**逐条引用**，形如 concept_id:state（如 c03:unknown），"
                        "取值逐字来自提示词「读者画像」那一节。"
                        "**读者有自评时必须逐条保留模型给的引用**（既不许清空、也不许改写）；"
                        "读者没有提供任何概念自评时可给空数组 []（只有背景的读者就是这一档，"
                        "背景依据写在 note 里）。"
                    ),
                },
                "note": {
                    "type": "string",
                    "description": (
                        "一句话说明依据（用了读者背景就写在 note 里，**不要**写进 profile 冒充自评）；"
                        "空 note 会被记成「模型没有写依据」的可见降级。"
                    ),
                },
            },
        },
        "confidence": {"type": "number"},
        "rewrite": {"type": "string"},
        # 追加的可选字段（PLAN §8）：只有 illustrate 段才给。
        # ⚠ 模型输出**不经 JSON Schema 校验**，所以 ``ar.rewrite._normalize_plan_items``
        # 的 dict 字面量也必须同步带上 ``figure``，否则静默丢弃。
        "figure": _FIGURE,
        # 追加的可选字段（§14.1）：逐段申报「本段没有原样保留哪几项事实锚」；没有申报时不给这个键
        "fact_declarations": _FACT_DECLARATIONS,
    },
}

ANALYZE_CONCEPTS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["paragraphs"],
    "properties": {
        "paragraphs": _PARAGRAPH_LIST,
        # 条数预算：模型给多了就按顺序截断（调用方自己要的上限，不算降级）
        "max_concepts": {"type": "integer", "minimum": 1},
    },
}
ANALYZE_CONCEPTS_OUTPUT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["concepts", "illustration_candidates"],
    "properties": {
        "concepts": {"type": "array", "items": _CONCEPT},
        "illustration_candidates": {"type": "array", "items": _ILLUSTRATION},
    },
}

REWRITE_BATCH_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["paragraphs", "start", "end", "profile_block", "concept_block", "background_sketch"],
    "properties": {
        "paragraphs": _PARAGRAPH_LIST,
        "start": {"type": "integer", "minimum": 1},
        "end": {"type": "integer", "minimum": 1},
        "profile_block": {"type": "string"},
        "concept_block": {"type": "string"},
        "background_sketch": {"type": "string"},
        # 追加的两个可选入参（§3.3 的表里没有）：提示词模板里有一行「材料：《{source_id}》，共 {total} 段」，
        # 不传就退化成「材料：《》，共 <end> 段」——调用方（rewrite.py）本来就有这两个值，传了才逐字等价
        "source_id": {"type": "string"},
        "total": {"type": "integer", "minimum": 1},
        # 追加的可选入参（PLAN-actions-feedback §8）：配图候选素描（``ar.analyze.format_illustration_block``），
        # 进 rewrite-v5 提示词的固定块。**必须可选**：不传（空串）时那一格零字符插入
        # （空块 == 缺省 == 有块与无块的差异只在那一格，**不是**「提示词与 rewrite-v3 逐字相同」）。
        "illustration_block": {"type": "string"},
        # 追加的可选入参（§14.1，rewrite-v5）：本读者的事实锚口径（``keep_all`` / ``allow_declared_drops``），
        # 进提示词的「事实锚口径」固定块。**必须可选**：不传时按严口径写（``ar.fidelity`` 的默认值），
        # 于是老调用方（不传）拿到的提示词与「传 keep_all」逐字相同。
        "fact_anchor_policy": {"type": "string"},
        # 追加的可选入参（rewrite-v7）：同一份材料的全文（``format_paragraph_block`` 的产物），
        # 进「按材料变」的那一段、同一材料的批 / 段共享。**必须可选**：不传时零字符插入。
        "full_text_block": {"type": "string"},
        # 追加的可选入参（rewrite-v7）：逐段循环的校验回灌，只追加在 user 末尾（空串零字符插入）。
        "feedback": {"type": "string"},
        # 追加的可选入参（plan-v2，两遍式的第二遍）：第一遍打好的计划块
        # （``ar.rewrite._plan_block`` 的产物），进全文块之后。**必须可选**：不传时零字符插入
        # （不带 ``--plan-first`` 的调用方拿到的提示词与加这一格之前逐字相同）。
        "plan_block": {"type": "string"},
    },
}
REWRITE_BATCH_OUTPUT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decisions"],
    "properties": {"decisions": {"type": "array", "items": _DECISION}},
}

#: 第六个工具：两遍式的**第一遍**（提示词版本 ``plan-v2``）。一次吃全篇、每段只回动作，
#: **不写正文**；入参比 ``rewrite_batch`` 少 ``start`` / ``end``（它不分批）。
PLAN_ACTIONS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["paragraphs", "profile_block", "concept_block", "background_sketch"],
    "properties": {
        "paragraphs": _PARAGRAPH_LIST,
        "profile_block": {"type": "string"},
        "concept_block": {"type": "string"},
        "background_sketch": {"type": "string"},
        # 可选入参：材料行（提示词里「材料：《{source_id}》，共 {total} 段」）与配图候选素描，
        # 与 ``rewrite_batch`` 同一口径——不传就退化成空材料名 / 零字符插入。
        "source_id": {"type": "string"},
        "total": {"type": "integer", "minimum": 1},
        "illustration_block": {"type": "string"},
        # 追加的可选入参（plan-v2）：事实锚口径与全文。两遍**共用同一段前缀**，这两块都在前缀里，
        # 所以第一遍建立的缓存才能被第二遍命中——不传时按严口径写 / 由 paragraphs 自己拼
        # （与 ``rewrite_batch` 的「不传就零字符插入」有意不同：第一遍看不见正文就没法打标）。
        "fact_anchor_policy": {"type": "string"},
        "full_text_block": {"type": "string"},
    },
}
PLAN_ACTIONS_OUTPUT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["plan"],
    "properties": {
        "plan": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["paragraph", "action", "note"],
                "properties": {
                    # 动作**不做 enum 约束**：枚举外的取值不是「解析失败」，而是**可见降级**
                    # （``ar.rewrite._parse_plan`` 把它按 keep 处理并记一条降级说明）。
                    "paragraph": {"type": "string"},
                    "action": {"type": "string"},
                    "note": {"type": "string"},
                },
            },
        }
    },
}

EXPLAIN_PARAGRAPH_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["paragraph", "kind", "text", "selection_from", "profile_hint"],
    "properties": {
        "paragraph": {"type": "string"},
        "kind": {"type": "string", "enum": list(EXPLAIN_KINDS)},
        "text": {"type": "string"},
        "selection": {"type": "string"},
        "selection_from": {"type": "string", "enum": list(SELECTION_SOURCES)},
        # prev / next 是**相邻段的正文**（§3.3 只给了字符串）：模板里邻段行是 ``[段id] 正文``，
        # 而相邻段 id 不在入参里，所以 id 位写 prev / next —— 那两行本来就只作上下文（「仅供定位」）
        "prev": {"type": "string"},
        "next": {"type": "string"},
        # 追加的两个可选入参（§3.3 的表里没有）：邻段在提示词里是 ``[段id] 正文``，
        # 调用方（explain.py）手里有真实段 id，传了才与迁移前的提示词**逐字相同**；
        # 不传就沿用 ``prev`` / ``next`` 两个占位 id（阶段 1 的既有行为）。
        "prev_id": {"type": "string"},
        "next_id": {"type": "string"},
        "profile_hint": {"type": "string"},
    },
}
EXPLAIN_PARAGRAPH_OUTPUT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text"],
    "properties": {"text": {"type": "string"}},
}

SURVEY_FOLLOWUPS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sketch", "answers"],
    "properties": {"sketch": {"type": "string"}, "answers": {"type": "object"}},
}
SURVEY_FOLLOWUPS_OUTPUT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["questions"],
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["q", "hint"],
                "properties": {"q": {"type": "string"}, "hint": {"type": "string"}},
            },
        }
    },
}

#: 第五个工具：单段压缩（PLAN-actions-feedback §5.1，提示词版本 ``compress-v3``）。
#: 入参里 ``paragraph`` / ``text`` 必填，其余可选；出参固定 ``action="compress"``。
COMPRESS_PARAGRAPH_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["paragraph", "text"],
    "properties": {
        "paragraph": {"type": "string"},
        "text": {"type": "string"},
        # 概念清单素描（与 rewrite_batch 的 concept_block 同一形态的**字符串**，不是结构化清单）
        "concepts": {"type": "string"},
        "profile_block": {"type": "string"},
        "instruction": {"type": "string"},
        # 追加的可选入参（§14.1，compress-v2）：本读者的事实锚口径（``keep_all`` /
        # ``allow_declared_drops``），进提示词的「事实锚口径」固定块。**必须可选**：不传时按严口径写
        # （``ar.fidelity`` 的默认值），于是老调用方（不传）拿到的提示词与「传 keep_all」逐字相同。
        "fact_anchor_policy": {"type": "string"},
    },
}
COMPRESS_PARAGRAPH_OUTPUT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "text"],
    "properties": {
        "action": {"type": "string", "enum": ["compress"]},
        "text": {"type": "string"},
        "why_note": {"type": "string"},
        # 追加的可选字段（§14.1，compress-v2）：逐条申报「本次压缩没有原样保留哪几项事实锚」；
        # 没有申报时不给这个键。⚠ 与 ``_FIGURE`` 同一条口径：模型输出**不经** schema 校验，
        # 所以 ``ar.rewrite.acompress_paragraph`` 必须自己取这个键，否则静默丢弃。
        "fact_declarations": _FACT_DECLARATIONS,
    },
}


# ------------------------------------------------------------------ Schema 自检 --
_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def _type_name(value: Any) -> str:
    return "null" if value is None else type(value).__name__


def schema_problems(schema: Any, value: Any, *, path: str = "值") -> list[str]:
    """按 JSON Schema 的一个子集自检值；返回人读的问题清单（空 = 通过）。

    支持 ``type`` / ``enum`` / ``required`` / ``properties`` / ``additionalProperties`` /
    ``items`` / ``minItems`` / ``minimum``——够本项目的工具契约用，不引第三方依赖。
    两处用途：① ``execute`` 校验入参（缺字段 / 多余字段 / 类型不对都**返回失败信封**
    ``INVALID_REQUEST``，不静默、也不抛异常）；② 测试核对出参与声明的 schema 一致。
    """
    problems: list[str] = []
    if not isinstance(schema, dict):
        return problems

    kinds = schema.get("type")
    if kinds is not None:
        allowed = (kinds,) if isinstance(kinds, str) else tuple(kinds)
        if not any(_TYPE_CHECKS.get(kind, lambda _v: True)(value) for kind in allowed):
            problems.append(f"{path} 类型应为 {' / '.join(allowed)}，实际是 {_type_name(value)}")
            return problems  # 类型都不对，不再往下挑字段

    if "enum" in schema and value not in (schema["enum"] or []):
        allowed = " / ".join(repr(item) for item in schema["enum"])
        problems.append(f"{path} 只能是 {allowed}，实际是 {value!r}")

    if isinstance(value, dict):
        for key in schema.get("required") or ():
            if key not in value:
                problems.append(f"{path} 缺必填字段 {key}")
        properties = schema.get("properties") or {}
        for key, sub_schema in properties.items():
            if key in value:
                problems += schema_problems(sub_schema, value[key], path=f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            extra = sorted(key for key in value if key not in properties)
            if extra:
                problems.append(f"{path} 有未知字段 {'、'.join(extra)}")

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            problems.append(f"{path} 至少 {minimum_items} 项，实际 {len(value)} 项")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                problems += schema_problems(items, item, path=f"{path}[{index}]")

    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value < minimum:
            problems.append(f"{path} 不能小于 {minimum}，实际是 {value}")

    return problems


# ------------------------------------------------------------------ 工具注册表 --
@dataclass(frozen=True)
class ToolSpec:
    """一个具名工具的契约（``execute`` 额外接受关键字注入的 ``client`` / ``policy``）。"""

    name: str
    description: str
    parameters: dict
    output: dict
    prompt_version: str
    execute: Callable[..., dict]


_REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    """登记一个工具（名字重复直接报错，不做「后注册覆盖前注册」这种静默行为）。"""
    if not isinstance(spec, ToolSpec):
        raise InputError(f"register 只接受 ToolSpec，实际是 {type(spec).__name__}")
    if spec.name in _REGISTRY:
        raise InputError(f"工具名重复：{spec.name}（已注册：{'、'.join(_REGISTRY)}）")
    _REGISTRY[spec.name] = spec
    return spec


def get(name: str) -> ToolSpec:
    """按名字取工具契约；没有就报错并列出可用的名字。"""
    try:
        return _REGISTRY[str(name)]
    except KeyError as exc:
        raise InputError(f"没有这个工具：{name!r}（可用：{'、'.join(_REGISTRY)}）") from exc


def all_tools() -> list[ToolSpec]:
    """全部工具（注册顺序）。"""
    return list(_REGISTRY.values())


def schemas() -> list[dict[str, Any]]:
    """可 JSON 序列化的工具契约列表（``ar tools --json`` 直接打印它）。"""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
            "output": spec.output,
            "prompt_version": spec.prompt_version,
        }
        for spec in _REGISTRY.values()
    ]


# ------------------------------------------------------------------ 信封与调用 --
def _failure_dict(failure: LlmFailure | None) -> dict[str, Any] | None:
    if failure is None:
        return None
    return {
        "code": failure.code,
        "message": failure.message,
        "status": failure.status,
        "retry_after_ms": failure.retry_after_ms,
    }


def _envelope(
    tool: str,
    result: LlmResult,
    *,
    ok: bool,
    data: Any = None,
    failure: LlmFailure | None = None,
    raw_text: str = "",
    degraded: Sequence[str] = (),
) -> dict[str, Any]:
    """工具层统一信封（字段与语义见模块文档）。"""
    return {
        "ok": bool(ok),
        "data": data,
        "failure": _failure_dict(failure),
        "attempts": attempt_records(tool, result.attempts),
        "usage": result.usage,
        "raw_text": raw_text,
        "degraded": [*result.degraded_params, *(str(item) for item in degraded)],
    }


def _failure_envelope(tool: str, result: LlmResult, *, raw_text: str = "", degraded: Sequence[str] = ()) -> dict:
    """模型调用失败（或未配置）：把归一后的失败原样交给调用方。"""
    failure = result.failure or LlmFailure(code=UNKNOWN_CODE, message=UNKNOWN_MESSAGE)
    return _envelope(tool, result, ok=False, failure=failure, raw_text=raw_text, degraded=degraded)


def _parse_envelope(tool: str, result: LlmResult, exc: BaseException, raw_text: str) -> dict[str, Any]:
    """模型回来了但输出不成结构：``data=None`` + ``UNKNOWN`` 失败，**原文一律带回**。"""
    return _envelope(
        tool,
        result,
        ok=False,
        failure=LlmFailure(code=PARSE_FAILURE_CODE, message=f"模型输出无法解析：{exc}"),
        raw_text=raw_text,
    )


def _policy_for(client: Any, policy: RetryPolicy | None) -> RetryPolicy:
    """重试策略：显式传的优先；否则问客户端（``ModelClient.retry_policy``）；再否则用默认值。"""
    if isinstance(policy, RetryPolicy):
        return policy
    getter = getattr(client, "retry_policy", None)
    if callable(getter):
        resolved = getter()
        if isinstance(resolved, RetryPolicy):
            return resolved
    return RetryPolicy()


def _ask(
    tool: str,
    messages: list[dict[str, str]],
    prompt_version: str,
    *,
    client: Any,
    policy: RetryPolicy | None,
) -> LlmResult:
    """经统一调用层发一次（含重试与逐次记账）。"""
    request = LlmRequest(
        tool=tool,
        messages=tuple(dict(message) for message in messages),
        temperature=None,
        max_tokens=None,
        prompt_version=prompt_version,
    )
    return llm_call(request, client=client, policy=_policy_for(client, policy))


def _params_problems(name: str, parameters: dict, params: Any) -> list[str]:
    """入参自检：缺必填字段 / 有未知字段 / 类型不对 → 人读问题清单（空 = 通过）。

    **不抛异常**（2026-10-13 复核修复）：入参不合契约也是「失败」，走与模型失败同一形状的
    失败信封（``INVALID_PARAMS_CODE``）——抛 ``InputError`` 会被 HTTP 层当成 400
    （调用方的 bug 伪装成客户端输入错误），其它异常则成 500，与「失败不抛」的契约冲突。
    """
    if not isinstance(params, dict):
        return [f"工具 {name} 的入参必须是 JSON 对象，实际是 {_type_name(params)}"]
    return schema_problems(parameters, params, path=f"{name} 入参")


def _invalid_params_envelope(name: str, problems: Sequence[str]) -> dict[str, Any]:
    """入参不合契约：返回失败信封（**不抛异常、不发任何请求**），形状与模型失败逐键一致。

    ``degraded`` 逐条点名是哪个参数不合法（``schema_problems`` 的路径已写成 ``工具名 入参.字段``），
    消息文本与迁移前的 ``InputError`` 文案同源，读起来还是那句话。
    """
    failure = LlmFailure(
        code=INVALID_PARAMS_CODE,
        message=f"工具 {name} 的入参不合契约：\n" + "\n".join(f"  - {item}" for item in problems),
    )
    return _envelope(
        name,
        LlmResult(ok=False, failure=failure),
        ok=False,
        failure=failure,
        degraded=[f"入参不合法（调用方的 bug，未发任何请求）：{item}" for item in problems],
    )


def _tool_executor(name: str, parameters: dict, runner: Callable[..., dict]) -> Callable[..., dict]:
    """把「校验入参 + 执行」绑成 ``spec.execute``（``client`` / ``policy`` 关键字注入）。

    入参不合契约时**直接回失败信封**，不进 runner、不发请求（四个调用点构造的入参本来就合规，
    所以正常路径的行为一个字没变）。
    """

    def execute(params: dict, *, client: Any = None, policy: RetryPolicy | None = None) -> dict:
        problems = _params_problems(name, parameters, params)
        if problems:
            return _invalid_params_envelope(name, problems)
        return runner(params, client=client, policy=policy)

    execute.__name__ = f"execute_{name}"
    execute.__doc__ = f"{name}：先按 JSON Schema 校验入参，再执行（入参不合法回失败信封，不抛）。"
    return execute


# ------------------------------------------------------------------ 六个工具 --
def _run_analyze_concepts(params: dict, *, client: Any = None, policy: RetryPolicy | None = None) -> dict:
    """概念清单抽取：拼 ``concepts-v2`` 提示词 → 调模型 → 抠出 JSON 对象。"""
    from .analyze import extract_json_object  # 延迟导入：避免 api 层与业务模块的导入顺序耦合

    tool = "analyze_concepts"
    messages = build_concepts_messages({"paragraphs": params["paragraphs"]})
    result = _ask(tool, messages, CONCEPTS_PROMPT_VERSION, client=client, policy=policy)
    if not result.ok:
        return _failure_envelope(tool, result)

    raw_text = result.text or ""
    try:
        data = extract_json_object(raw_text)
    except InputError as exc:
        return _parse_envelope(tool, result, exc, raw_text)

    max_concepts = params.get("max_concepts")
    concepts = data.get("concepts")
    if isinstance(max_concepts, int) and not isinstance(max_concepts, bool) and isinstance(concepts, list):
        if len(concepts) > max_concepts:
            data = {**data, "concepts": concepts[:max_concepts]}
    return _envelope(tool, result, ok=True, data=data, raw_text=raw_text)


def _run_rewrite_batch(params: dict, *, client: Any = None, policy: RetryPolicy | None = None) -> dict:
    """一批段落的决策 + 改写：拼 ``rewrite-v7`` 提示词 → 调模型 → 归一化成条目。"""
    from .analyze import extract_json_object
    from .rewrite import _parse_batch

    tool = "rewrite_batch"
    paragraphs = params["paragraphs"]
    start = int(params["start"])
    end = int(params["end"])
    total = params.get("total")
    if not isinstance(total, int) or isinstance(total, bool):
        # 没传总段数就按这批的末位序号写（提示词那一行只作上下文，不影响事实锚）
        total = end
    messages = build_rewrite_messages(
        str(params.get("source_id") or ""),
        paragraphs,
        profile_block=params["profile_block"],
        background_block=params["background_sketch"],
        concept_block=params["concept_block"],
        # 可选入参：不传（空串）时那一格零字符插入（空块 == 缺省，有块与无块的差异只在那一格）
        illustration_block=str(params.get("illustration_block") or ""),
        # 可选入参（§14.1）：不传时按严口径写，与「传 keep_all」逐字相同
        fact_anchor_policy=str(params.get("fact_anchor_policy") or DEFAULT_FACT_ANCHOR_POLICY),
        # 可选入参（rewrite-v7）：全文进「按材料变」的那一段；不传时零字符插入
        full_text_block=str(params.get("full_text_block") or ""),
        # 可选入参（rewrite-v7）：逐段循环的校验回灌，只追加在 user 末尾
        feedback=str(params.get("feedback") or ""),
        # 可选入参（plan-v2）：第一遍的计划块；不传时零字符插入（提示词逐字不变）
        plan_block=str(params.get("plan_block") or ""),
        start=start,
        end=end,
        total=total,
    )
    result = _ask(tool, messages, REWRITE_PROMPT_VERSION, client=client, policy=policy)
    if not result.ok:
        return _failure_envelope(tool, result)

    raw_text = result.text or ""
    try:
        extract_json_object(raw_text)
    except InputError as exc:
        return _parse_envelope(tool, result, exc, raw_text)
    items, issues = _parse_batch(raw_text, paragraphs, start, end, extract_json_object=extract_json_object)
    # issues（多余段 id / 重复段 id）是可见降级，不是失败：原模块把它记进 meta.degraded
    return _envelope(tool, result, ok=True, data={"decisions": items}, raw_text=raw_text, degraded=issues)


def _run_plan_actions(params: dict, *, client: Any = None, policy: RetryPolicy | None = None) -> dict:
    """两遍式的第一遍：拼 ``plan-v2`` 提示词 → 调模型 → 抠出「每段一个动作」的计划。

    **只出动作、不出正文**：模型多给的 ``rewrite` / ``figure` 键在这里就丢掉（出参 schema 不许有），
    并在 ``degraded`` 里留一条可见说明——不是静默忽略。
    段 id 读不通 / 重复的条目同样进 ``degraded``；**缺段不在这里判**（越界动作与缺段由
    ``ar.rewrite._parse_plan`` 按「能不能用」归一，那边才知道全篇该有哪几段）。
    """
    from .analyze import extract_json_object

    tool = "plan_actions"
    paragraphs = params["paragraphs"]
    total = params.get("total")
    if not isinstance(total, int) or isinstance(total, bool):
        total = len(paragraphs)
    # plan-v2：这两格进的是**两遍共用的前缀**（第二遍由 ``rewrite_batch`` 拿到同一份），
    # 于是打标那一遍建好的缓存能被第二遍第 1 批命中。``full_text_block`` 不传（None）时由 paragraphs 自己拼。
    raw_full_text = params.get("full_text_block")
    messages = build_plan_messages(
        str(params.get("source_id") or ""),
        paragraphs,
        profile_block=params["profile_block"],
        background_block=params["background_sketch"],
        concept_block=params["concept_block"],
        illustration_block=str(params.get("illustration_block") or ""),
        total=total,
        fact_anchor_policy=str(params.get("fact_anchor_policy") or DEFAULT_FACT_ANCHOR_POLICY),
        full_text_block=raw_full_text if isinstance(raw_full_text, str) else None,
    )
    result = _ask(tool, messages, PLAN_PROMPT_VERSION, client=client, policy=policy)
    if not result.ok:
        return _failure_envelope(tool, result)

    raw_text = result.text or ""
    try:
        data = extract_json_object(raw_text)
    except InputError as exc:
        return _parse_envelope(tool, result, exc, raw_text)

    raw_plan = data.get("plan")
    if not isinstance(raw_plan, list):
        return _parse_envelope(tool, result, InputError("模型输出里没有 plan 数组"), raw_text)
    entries: list[dict[str, Any]] = []
    notes: list[str] = []
    seen: set[str] = set()
    for item in raw_plan:
        if not isinstance(item, dict):
            notes.append("模型给的 plan 里有不是对象的条目，已忽略")
            continue
        pid = item.get("paragraph")
        if not isinstance(pid, str) or not pid.strip():
            notes.append("模型给的 plan 里有缺 paragraph 的条目，已忽略")
            continue
        pid = pid.strip()
        if pid in seen:
            notes.append(f"{pid}：模型在 plan 里重复返回了这一段的动作，只取第一条")
            continue
        seen.add(pid)
        if item.get("rewrite") or item.get("figure"):
            notes.append(f"{pid}：第一遍只要动作，模型多给了 rewrite / figure，已忽略（正文只由第二遍产出）")
        entries.append(
            {
                "paragraph": pid,
                "action": str(item.get("action") or ""),
                "note": str(item.get("note") or ""),
            }
        )
    return _envelope(tool, result, ok=True, data={"plan": entries}, raw_text=raw_text, degraded=notes)


def _run_explain_paragraph(params: dict, *, client: Any = None, policy: RetryPolicy | None = None) -> dict:
    """一段（或一段里的选中片段）的讲解卡片：拼 ``explain-v2`` 提示词 → 调模型。"""
    tool = "explain_paragraph"
    neighbors: list[tuple[str, str]] = []
    for key in ("prev", "next"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            # 段 id 位优先用调用方给的 ``prev_id`` / ``next_id``（提示词里是 ``[段id] 正文``）；
            # 没给就沿用 ``prev`` / ``next`` 这两个占位名
            neighbors.append((str(params.get(f"{key}_id") or key), value))
    messages = build_explain_messages(
        kind=str(params["kind"]),
        paragraph_id=str(params["paragraph"]),
        paragraph_text=str(params["text"]),
        neighbors=neighbors or None,
        profile_hint=str(params["profile_hint"]),
        selection=params.get("selection"),
        selection_from=str(params["selection_from"] or DEFAULT_SELECTION_FROM),
    )
    result = _ask(tool, messages, EXPLAIN_PROMPT_VERSION, client=client, policy=policy)
    if not result.ok:
        return _failure_envelope(tool, result)
    text = (result.text or "").strip()
    if not text:  # 模型层已把空正文归成 EMPTY_RESPONSE，这里只是兜底
        return _parse_envelope(tool, result, InputError("模型没有返回正文"), result.text or "")
    return _envelope(tool, result, ok=True, data={"text": text}, raw_text=result.text or "")


def _run_survey_followups(params: dict, *, client: Any = None, policy: RetryPolicy | None = None) -> dict:
    """读前背景问卷的模型追问：拼 ``survey-v1`` 提示词 → 调模型 → 整理成 3–5 个问题。"""
    from .survey import followup_questions

    tool = "survey_followups"
    messages = build_survey_messages(sketch=str(params["sketch"]), answers=params["answers"])
    result = _ask(tool, messages, SURVEY_PROMPT_VERSION, client=client, policy=policy)
    if not result.ok:
        return _failure_envelope(tool, result)

    raw_text = result.text or ""
    try:
        questions = followup_questions(raw_text)
    except InputError as exc:
        return _parse_envelope(tool, result, exc, raw_text)
    return _envelope(tool, result, ok=True, data={"questions": questions}, raw_text=raw_text)


def _run_compress_paragraph(params: dict, *, client: Any = None, policy: RetryPolicy | None = None) -> dict:
    """单段压缩（读者点「太啰嗦」）：拼 ``compress-v3`` 提示词 → 调模型 → 取压缩后的正文。

    出参固定 ``action="compress"`` + ``text``（``why_note`` / ``fact_declarations`` 可缺）。
    模型给的 ``action`` 字段不采信——这次调用的意图就是压缩，产物里的动作必须是 ``compress``
    （省得模型改个词就让上层判错）。
    ``fact_declarations`` **原样带回**（只做「是不是对象」的粗筛）：归一与问题清单由
    ``ar.fidelity.parse_declarations`` 负责，交给业务层（与 ``rewrite_batch`` 把 decisions 原样带回同一条分工）。
    ⚠ 非对象形状（模型偶尔只给一个字符串）**不许静默丢**：出参里照样不带这个键（schema 说的是对象），
    但信封 ``degraded`` 里留一条「申报读不通」的可读说明（调用方并进响应与产物，见
    ``ar.rewrite.acompress_paragraph``）——否则业务层只看到「没申报」，用户什么都看不到。
    失败一律走失败信封（**不抛**）；模型答了但抠不出正文时落 ``PARSE_FAILURE_CODE`` 并带回原文。
    """
    from .analyze import extract_json_object

    tool = "compress_paragraph"
    messages = build_compress_messages(
        paragraph=str(params["paragraph"]),
        text=str(params["text"]),
        concepts=str(params.get("concepts") or ""),
        profile_block=str(params.get("profile_block") or ""),
        instruction=str(params.get("instruction") or ""),
        fact_anchor_policy=str(params.get("fact_anchor_policy") or ""),
    )
    result = _ask(tool, messages, COMPRESS_PROMPT_VERSION, client=client, policy=policy)
    if not result.ok:
        return _failure_envelope(tool, result)

    raw_text = result.text or ""
    try:
        data = extract_json_object(raw_text)
    except InputError as exc:
        return _parse_envelope(tool, result, exc, raw_text)

    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return _parse_envelope(tool, result, InputError("模型没有返回压缩后的正文"), raw_text)
    payload: dict[str, Any] = {"action": "compress", "text": text.strip()}
    why_note = data.get("why_note")
    if isinstance(why_note, str) and why_note.strip():
        payload["why_note"] = why_note.strip()
    notes: list[str] = []
    declarations = data.get("fact_declarations")
    if isinstance(declarations, dict) and declarations:
        payload["fact_declarations"] = dict(declarations)
    elif declarations is not None and not isinstance(declarations, dict):
        # 非对象（字符串 / 数组 / 数字…）：按「没有申报」处理（安全方向不变：缺失照旧不放行），
        # 但**不静默**——出参 schema 只认对象，所以说明走 degraded（调用方并进响应与产物）
        notes.append(
            f"模型给的 {DECLARATION_FIELD} 不是对象（实际 {_type_name(declarations)}）："
            "整条按「没有申报」处理（这一段因此没有可放行的申报）"
        )
    return _envelope(tool, result, ok=True, data=payload, raw_text=raw_text, degraded=notes)


#: 六个工具的契约（名称 / 描述 / 入参出参 schema / 提示词版本，§3.3 的表逐条对得上）
ANALYZE_CONCEPTS_SPEC = ToolSpec(
    name="analyze_concepts",
    description="Extract the concept inventory of a whole document.",
    parameters=ANALYZE_CONCEPTS_PARAMETERS,
    output=ANALYZE_CONCEPTS_OUTPUT,
    prompt_version=CONCEPTS_PROMPT_VERSION,
    execute=_tool_executor("analyze_concepts", ANALYZE_CONCEPTS_PARAMETERS, _run_analyze_concepts),
)

REWRITE_BATCH_SPEC = ToolSpec(
    name="rewrite_batch",
    description="Decide and rewrite a batch of paragraphs for one reader.",
    parameters=REWRITE_BATCH_PARAMETERS,
    output=REWRITE_BATCH_OUTPUT,
    prompt_version=REWRITE_PROMPT_VERSION,
    execute=_tool_executor("rewrite_batch", REWRITE_BATCH_PARAMETERS, _run_rewrite_batch),
)

#: 两遍式的第一遍（``plan-v2``，2026-10-31 起与第二遍共享同一段前缀）：一次调用给全篇每段打动作标，**不写正文**。
PLAN_ACTIONS_SPEC = ToolSpec(
    name="plan_actions",
    description="Label one action (expand / compress / keep / illustrate) per paragraph, no prose.",
    parameters=PLAN_ACTIONS_PARAMETERS,
    output=PLAN_ACTIONS_OUTPUT,
    prompt_version=PLAN_PROMPT_VERSION,
    execute=_tool_executor("plan_actions", PLAN_ACTIONS_PARAMETERS, _run_plan_actions),
)

EXPLAIN_PARAGRAPH_SPEC = ToolSpec(
    name="explain_paragraph",
    description="Explain one paragraph or a selected excerpt.",
    parameters=EXPLAIN_PARAGRAPH_PARAMETERS,
    output=EXPLAIN_PARAGRAPH_OUTPUT,
    prompt_version=EXPLAIN_PROMPT_VERSION,
    execute=_tool_executor("explain_paragraph", EXPLAIN_PARAGRAPH_PARAMETERS, _run_explain_paragraph),
)

SURVEY_FOLLOWUPS_SPEC = ToolSpec(
    name="survey_followups",
    description="Generate follow-up questions from a reader's background.",
    parameters=SURVEY_FOLLOWUPS_PARAMETERS,
    output=SURVEY_FOLLOWUPS_OUTPUT,
    prompt_version=SURVEY_PROMPT_VERSION,
    execute=_tool_executor("survey_followups", SURVEY_FOLLOWUPS_PARAMETERS, _run_survey_followups),
)

COMPRESS_PARAGRAPH_SPEC = ToolSpec(
    name="compress_paragraph",
    description="Compress one paragraph, declaring any dropped fact anchor.",
    parameters=COMPRESS_PARAGRAPH_PARAMETERS,
    output=COMPRESS_PARAGRAPH_OUTPUT,
    prompt_version=COMPRESS_PROMPT_VERSION,
    execute=_tool_executor("compress_paragraph", COMPRESS_PARAGRAPH_PARAMETERS, _run_compress_paragraph),
)

for _spec in (
    ANALYZE_CONCEPTS_SPEC,
    REWRITE_BATCH_SPEC,
    PLAN_ACTIONS_SPEC,
    EXPLAIN_PARAGRAPH_SPEC,
    SURVEY_FOLLOWUPS_SPEC,
    COMPRESS_PARAGRAPH_SPEC,
):
    register(_spec)


# --------------------------------------------------- 带底层结果的一次调用（追加） --
@dataclass(frozen=True)
class ToolRun:
    """一次工具调用的**完整**结果：统一信封 + 底层 ``LlmResult``（信封键集不变）。

    为什么需要它：信封的键集是**冻结的**（``tests/test_tools.py`` 逐键核对），而业务产物里
    有些既有字段只在底层结果上——``meta.model``（哪个模型答的）与 ``attempts``（逐次尝试）。
    四个调用点迁移后不再直接碰 ``ModelClient``，所以由这里把它们带出来：

    - ``envelope``：与 ``spec.execute(...)`` 的返回**逐字相同**（校验、重试、记账都在工具层）；
    - ``result``：底层一次调用的结果（``model`` / ``attempts`` / ``finish_reason``）；
      ``None`` 只在「请求根本没发出去」（未配置）时出现。
    """

    name: str
    envelope: dict[str, Any]
    result: LlmResult | None = None

    @property
    def ok(self) -> bool:
        return bool(self.envelope.get("ok"))

    @property
    def data(self) -> Any:
        return self.envelope.get("data")

    @property
    def failure(self) -> dict[str, Any] | None:
        failure = self.envelope.get("failure")
        return dict(failure) if isinstance(failure, dict) else None

    @property
    def failure_code(self) -> str | None:
        failure = self.failure
        return str(failure["code"]) if failure and failure.get("code") else None

    @property
    def model(self) -> str | None:
        """哪个模型答的（产物 ``meta.model`` 的来源）。"""
        return None if self.result is None else self.result.model

    @property
    def usage(self) -> dict[str, Any] | None:
        usage = self.envelope.get("usage")
        return dict(usage) if isinstance(usage, dict) else None

    @property
    def raw_text(self) -> str:
        return str(self.envelope.get("raw_text") or "")

    @property
    def degraded(self) -> list[str]:
        return [str(item) for item in (self.envelope.get("degraded") or [])]

    @property
    def attempt_records(self) -> list[dict[str, Any]]:
        """逐次尝试的账目（落产物 ``meta.attempts``，结构照 §3.1 规则 7）。"""
        records = self.envelope.get("attempts")
        return [dict(item) for item in records] if isinstance(records, list) else []

    @property
    def attempts(self) -> tuple[LlmAttempt, ...]:
        """逐次尝试（**以信封里的账为准**，不从 ``result`` 取）。

        ``ar.llm`` 是把 ``attempts`` 附加在**最终**返回值上的，而客户端那一层的
        ``arequest_once`` 只看得见单次尝试的结果（它的 ``attempts`` 恒为空），
        所以这里把信封里的账目还原成 ``LlmAttempt``——``account()`` 因此仍由
        ``ar.llm.retry_account`` 算（账目形状只有一处定义）。
        """
        return tuple(
            LlmAttempt(
                try_no=int(item.get("try") or 0),
                elapsed_ms=int(item.get("elapsed_ms") or 0),
                code=item.get("code"),
                delay_ms=item.get("delay_ms"),
                retry_after_used=bool(item.get("retry_after_used")),
            )
            for item in self.attempt_records
        )

    @property
    def model_replied(self) -> bool:
        """模型**答了**（HTTP 通了、有正文），只是输出不成结构。

        「模型没答上来」与「答了但没法用」是两条不同的降级路径（前者退出码 5 / 批次失败，
        后者走各自的解析降级），调用方据此分流——比拿错误码或文案去猜可靠。
        """
        return self.result is not None and self.result.ok

    def account(self) -> dict[str, Any]:
        """整次调用一条汇总（``{tool, requests, retries, codes}``，与 ``ar.llm.retry_account`` 同形）。"""
        return retry_account(self.name, self.attempts)

    def failure_message(self) -> str:
        """失败的人读文案：``模型调用失败（已试 N 次，末次 <CODE>）：<脱敏消息>``。

        唯一实现在 ``ar.llm.failure_message``：这里把信封里的失败与尝试账复原成一个
        ``LlmResult`` 再交给它算——存量账目只有一处口径，调用方（``explain`` / ``analyze`` /
        ``rewrite`` / ``survey``）在失败分支都用这一句面向用户的文案。
        """
        failure = self.failure or {}
        return llm_failure_message(
            LlmResult(
                ok=False,
                failure=LlmFailure(
                    code=str(failure.get("code") or UNKNOWN_CODE),
                    message=str(failure.get("message") or UNKNOWN_MESSAGE),
                ),
                attempts=self.attempts,
            )
        )


class _ResultKeeper:
    """把客户端包一层：转发 ``arequest_once`` / ``retry_policy`` / ``configured``，顺手记下最后一次结果。

    ``ar.llm.acall`` 只认这三个成员，所以包一层对它是透明的；唯一的副作用是把 ``LlmResult``
    留了下来（``run_tool`` 用它取 ``model`` 与 ``attempts``）。
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.result: LlmResult | None = None

    @property
    def configured(self) -> bool:
        # 与 ``ar.llm.acall`` 的判据逐字一致：没传客户端 = 未配置；传了但没这个属性 = 按已配置处理
        if self._client is None:
            return False
        return bool(getattr(self._client, "configured", True))

    def retry_policy(self) -> RetryPolicy:
        return _policy_for(self._client, None)

    async def arequest_once(self, request: LlmRequest) -> LlmResult:
        result = await self._client.arequest_once(request)
        self.result = result if isinstance(result, LlmResult) else None
        return result


def run_tool(name: str, params: dict, *, client: Any = None, policy: RetryPolicy | None = None) -> ToolRun:
    """与 ``get(name).execute(...)`` **同一次调用**，额外把底层 ``LlmResult`` 带出来。

    信封、入参校验、重试与记账全部照旧（既有的 ``execute`` 一行没动）；
    业务模块用返回值里的 ``model`` / ``attempts`` / ``model_replied`` 落产物与分流降级。

    ⚠ 与 ``execute`` 同一条纪律：同步入口（内部走 ``ar.llm.call``，即 ``asyncio.run``），
    **已有事件循环的路径（HTTP 的 async 路由）必须 ``await asyncio.to_thread(run_tool, ...)``**。
    """
    spec = get(name)
    keeper = _ResultKeeper(client)
    envelope = spec.execute(params, client=keeper, policy=policy)
    result = keeper.result
    failure = envelope.get("failure")
    if result is None and isinstance(failure, dict) and failure.get("code") == "NOT_CONFIGURED":
        # 未配置：一次请求都没发（``ar.llm`` 直接返回 NOT_CONFIGURED），补一条零请求的结果，
        # 免得调用方把「没发请求」与「发了但没结果」混起来
        result = not_configured_result()
    return ToolRun(name=name, envelope=envelope, result=result)
