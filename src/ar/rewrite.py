"""逐段决策 + 改写（M3，契约 §2.4 / §4）。

产物（DESIGN §4.4，字段逐字不改名）::

    data/<id>/decisions.json   [{"paragraph","action","why{"profile","note"},"confidence"}] + meta
    data/<id>/rewritten.md     改写稿（段序与源一致、段间空行分隔、段首不写 id）
    data/<id>/changes.json     {"changes":[…含 facts / deferred…],"fidelity":{"checked","mismatches"},"meta"}

四条纪律：

1. **骨架只实现 expand / keep**。``compress`` / ``illustrate`` 只留枚举位：模型若输出它们，
   一律按 ``keep`` 处理并在该段标 ``"deferred": true``（与前端夹具同一口径，页面会显示
   「骨架未实现该动作，按保持处理」）。枚举**外**的动作（含模型没给 ``action``）同样按
   ``keep`` 处理，但**不标 deferred**——文案与产物必须一致。
2. **省 token**：25 段不逐段发请求，按 ``max_chars_per_batch`` 合并成 4 次左右请求；
   但 ``decisions`` 仍然**每段一条**。缺哪段的决策就补一条可见降级的 ``keep``，绝不假装成功。
3. **降级必须可见**（DESIGN §6）：空画像 / 决策缺失 / 模型动作越界 / 调用失败，全部记进
   ``decisions.json`` 与 ``changes.json`` 的 ``meta.degraded``，并打印到 CLI。
   **批次失败或模型没给决策 ⇒ 有段落根本没被改写**：meta 里记 ``model_errors``（批次 / 受影响段 /
   错误）与 ``unplanned_paragraphs``，CLI 以退出码 6 结束（``ar check`` 同码），绝不写成成功。
4. ``mode`` 默认 ``free``（自由重述 + 事实锚）。``strict`` 是预留插口：**未实现即报错**，
   不静默按 free 跑。
5. ``fidelity.checked`` 只在 ``ar check`` 通过后为 ``true``：``rewrite`` 阶段写 ``false``
   （逐段事实锚比对还没跑），避免只看 rewrite 产物的人误读成「已查过、干净」。
6. **0 比对项 = 检查器空跑 = 失败**：事实锚比对一项都没比（空文档）时不许报通过。
7. **前缀缓存（prompt caching）**：提示词里会变的部分全部排在 user 末尾（见 ``ar.prompts``
   的「前缀缓存纪律」），且**先串行跑第 1 批预热前缀、再并发其余批次**
   （``_plan_batches_warmup``）——6 批同时首发谁都吃不到刚建好的缓存。
   命中量逐批记在 ``meta.batch_usage[].cached_tokens``、合计记在 ``meta.usage.cached_tokens``
   （网关没上报该字段时为 ``null``，不编造成 0）。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol

from .errors import InputError, NotImplementedYet
from .fidelity import facts_of, split_rewritten_blocks
from .ingest import text_hash
from .jsonio import now_iso, read_json, read_json_if_exists, write_json, write_text
from .model import cached_tokens_of
from .paths import doc_dir
from .prompts import REWRITE_ACTIONS, REWRITE_PROMPT_VERSION, build_rewrite_messages

__all__ = [
    "ACTIONS",
    "IMPLEMENTED_ACTIONS",
    "UNIMPLEMENTED_ACTIONS",
    "MODES",
    "DEFAULT_MODE",
    "STRICT_NOT_IMPLEMENTED",
    "DEFAULT_MAX_CHARS_PER_BATCH",
    "Decision",
    "PlanItem",
    "RewriteOutcome",
    "BatchPlan",
    "CheckOutcome",
    "actions_label",
    "planDecisions",
    "rewrite",
    "run_rewrite",
    "check_fidelity",
    "fidelity_problems",
    "incomplete_reasons",
]

#: 契约 §4 的四个动作；骨架只实现前两个
ACTIONS = ("expand", "compress", "keep", "illustrate")
IMPLEMENTED_ACTIONS = ("expand", "keep")
#: 枚举里存在、但骨架没实现的动作：遇到按 ``keep`` 处理并标 ``deferred``（只有这两个标 deferred）
UNIMPLEMENTED_ACTIONS = ("compress", "illustrate")
MODES = ("free", "strict")
DEFAULT_MODE = "free"

#: 合并请求的粒度（省 token 与「单次请求别太大」之间的折中）：25 段 → 4 次请求
DEFAULT_MAX_CHARS_PER_BATCH = 1100

STRICT_NOT_IMPLEMENTED = (
    "严格档（mode=strict，逐句可回溯规格）一期不做，只预留接口：本期只有 free（自由重述 + 事实锚）。"
    "改写稿与变更记录都没有写入（不会静默按 free 跑）。"
)

_ACTION_LABELS = {"expand": "展开", "compress": "压缩", "keep": "保持", "illustrate": "配图"}
_BLANK_LINE_RE = re.compile(r"\n[ \t\r]*\n")
_INDENT_RE = re.compile(r"^[ \t]+", re.M)


def actions_label(action: str) -> str:
    """动作枚举 → 中文名（与前端 ``web/js/schema.js`` 的 ACTION_LABEL 同口径）。"""
    return _ACTION_LABELS.get(action, action)


class ChatClient(Protocol):
    """``planDecisions`` 需要的模型客户端（真实实现 ``ar.model.ModelClient``）。"""

    def chat(self, messages: Iterable[dict[str, str]], **kwargs: Any) -> Any: ...


@dataclass
class Decision:
    """一段的动作决策（契约 §4 的 ``decisions.json`` 条目）。"""

    paragraph: str
    action: str
    why: dict[str, Any]
    confidence: float
    rewrite: str = ""
    #: 模型给出的原始动作（越界时留证；正常时与 ``action`` 相同）
    requested_action: str | None = None

    @property
    def deferred(self) -> bool:
        """骨架未实现该动作、已按 ``keep`` 处理。"""
        return self.action == "keep" and self.requested_action in ("compress", "illustrate")

    def to_record(self) -> dict[str, Any]:
        """落 ``decisions.json`` 的形态（``rewrite`` 不落盘：改写稿另有 ``rewritten.md``）。"""
        return {
            "paragraph": self.paragraph,
            "action": self.action,
            "why": {"profile": list(self.why.get("profile") or []), "note": str(self.why.get("note") or "")},
            "confidence": self.confidence,
        }


@dataclass
class RewriteOutcome:
    """一次 ``rewrite`` 的结果（就绪的 decisions + 写盘路径 + 可见降级清单）。"""

    source_id: str
    reader: str
    mode: str
    decisions: list[Decision] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    fidelity: dict[str, Any] = field(default_factory=dict)
    rewritten_markdown: str = ""
    degraded: list[str] = field(default_factory=list)
    #: 模型批次调用失败：[{"batch","paragraphs","error"}]（这些段保持原文、没被改写）
    model_errors: list[dict[str, Any]] = field(default_factory=list)
    #: 模型没给出决策的段 id（同样是被迫保持原文，不是主动 keep）
    unplanned_paragraphs: list[str] = field(default_factory=list)
    model: str | None = None
    prompt_version: str = REWRITE_PROMPT_VERSION
    usage: dict[str, Any] = field(default_factory=dict)
    request_count: int = 0
    batch_count: int = 0
    profile_used: bool = False
    profile_note: str = ""
    deferred_count: int = 0
    usage_note: str = ""
    decisions_path: Path | None = None
    rewritten_path: Path | None = None
    changes_path: Path | None = None

    def action_counts(self) -> dict[str, int]:
        counts = {action: 0 for action in ACTIONS}
        for decision in self.decisions:
            counts[decision.action] = counts.get(decision.action, 0) + 1
        return counts

    @property
    def incomplete(self) -> bool:
        """是否有段落因模型问题**没被改写**（不是模型主动选择「保持」）。"""
        return bool(self.model_errors or self.unplanned_paragraphs)


@dataclass
class PlanItem:
    """一批段落里的一段：决策 + 该段要不要真的改写。"""

    paragraph: dict[str, Any]
    decision: Decision

    @property
    def needs_rewrite(self) -> bool:
        return self.decision.action == "expand"


@dataclass
class BatchPlan:
    """一批段落的模型产出（parse 后、尚未拼装成改写稿）。"""

    start: int
    end: int
    items: list[PlanItem] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    error: str | None = None
    #: 这一批覆盖的段 id（调用失败时用来点名「哪几段没被改写」）
    paragraphs: list[str] = field(default_factory=list)
    #: 这一批里模型没给出决策的段 id
    unplanned: list[str] = field(default_factory=list)

    @property
    def batch_label(self) -> str:
        """人读的批次名：``第 1–6 段``。"""
        return f"第 {self.start}–{self.end} 段"


# ------------------------------------------------------------------ 解析与归一 --
def _as_probability(value: Any, default: float = 0.5) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0.0, min(1.0, float(value)))


def _clean_rewrite(text: Any) -> str:
    """整理模型给的改写正文：统一换行、剥掉行首缩进（提示词要求原样，模型常顺手缩进）。

    只动行首空白——正文里的引号、数字、标点一个字符都不许碰（事实锚靠它们）。
    """
    if not isinstance(text, str):
        return ""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _INDENT_RE.sub("", cleaned)
    lines = [line.rstrip() for line in cleaned.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _normalize_plan_items(raw: Any) -> list[dict[str, Any]]:
    """把一批模型输出整理成条目（只做类型整理，越界动作与缺段留给上层记账）。"""
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("paragraph") or entry.get("id")
        if not isinstance(pid, str) or not pid.strip():
            continue
        action = entry.get("action")
        action = action.strip().lower() if isinstance(action, str) else ""
        why_raw = entry.get("why")
        why_raw = why_raw if isinstance(why_raw, dict) else {}
        profile = why_raw.get("profile")
        profile = [str(item) for item in profile if isinstance(item, (str, int))] if isinstance(profile, list) else []
        note = why_raw.get("note")
        items.append(
            {
                "paragraph": pid.strip(),
                "action": action,
                "why": {"profile": profile, "note": note.strip() if isinstance(note, str) else ""},
                "confidence": _as_probability(entry.get("confidence")),
                "rewrite": _clean_rewrite(entry.get("rewrite") or entry.get("text") or ""),
            }
        )
    return items


def _finalize_items(
    paragraph: dict[str, Any],
    raw_item: dict[str, Any] | None,
    *,
    degraded: list[str],
    unplanned: list[str] | None = None,
) -> PlanItem:
    """把一条（可能缺失 / 越界的）模型条目落成契约里的决策。

    ``unplanned`` 非空时，收集「模型没给出决策」的段 id（这些段是被迫保持原文的，
    不是因为模型主动选择 keep）——上层据此以退出码 6 结束。
    """
    pid = str(paragraph.get("id"))
    if raw_item is None:
        degraded.append(f"{pid}：模型没有给出这段的决策，已按「保持」处理并标低置信度")
        if unplanned is not None:
            unplanned.append(pid)
        decision = Decision(
            paragraph=pid,
            action="keep",
            why={"profile": [], "note": "模型未给出这一段的决策（降级为保持原文，未改写）"},
            confidence=0.0,
            requested_action=None,
        )
        return PlanItem(paragraph=paragraph, decision=decision)

    requested = raw_item["action"]
    why = dict(raw_item["why"])
    note = why.get("note") or "模型没有写依据（why.note 为空）"
    why["note"] = note
    confidence = float(raw_item["confidence"])
    action = requested
    if requested in UNIMPLEMENTED_ACTIONS:
        # compress / illustrate：留枚举位，按 keep 处理并标 deferred（可见，不静默）
        action = "keep"
        why["note"] = f"{note}（模型建议「{actions_label(requested)}」，骨架未实现该动作，按保持处理）"
        degraded.append(f"{pid}：模型给出未实现的动作「{actions_label(requested)}」，已按「保持」处理并标 deferred")
    elif requested not in IMPLEMENTED_ACTIONS:
        # 空动作 / 枚举外的动作（模型给了 "frobnicate" 或干脆没给 action）：同样按 keep 处理，
        # 但**不标 deferred**——deferred 只表示「枚举内的未实现动作」。文案必须与产物一致：
        # 早期版本这里也写「并标 deferred」，而 deferred 实际为 false（复核实测抓到的 P5）。
        action = "keep"
        label = f"「{actions_label(requested)}」" if requested else "（空）"
        why["note"] = f"{note}（模型给的动作 {label} 不在四类之内，按保持处理；未标 deferred）"
        degraded.append(
            f"{pid}：模型给的动作 {label} 不在 expand / compress / keep / illustrate 四类之内，"
            "已按「保持」处理（未标 deferred：deferred 只表示枚举内的未实现动作）"
        )
    elif requested == "keep" and raw_item["rewrite"]:
        # keep 段多给了改写正文：按 keep 处理（改写稿用原文），但把这件事记下来
        degraded.append(f"{pid}：模型对 keep 段也给了改写正文，已忽略（骨架以「保持原文」为准）")

    source_length = len(str(paragraph.get("text") or ""))
    rewrite_text = raw_item["rewrite"] if action == "expand" else ""
    if action == "expand":
        if len(rewrite_text) <= source_length:
            # 「展开」却没变长：多半只回了正文的一部分（截断），必须可见
            degraded.append(
                f"{pid}：模型给的展开正文（{len(rewrite_text)} 字）不比原文（{source_length} 字）长，"
                "疑似截断或没有原样保留全文，事实锚校验会看得出来"
            )
        elif not rewrite_text.rstrip().endswith(("。", "！", "？", "”", "」", "）", "〕", "》", "…")):
            degraded.append(f"{pid}：展开正文没有以句末标点收尾，疑似被截断")

    return PlanItem(
        paragraph=paragraph,
        decision=Decision(
            paragraph=pid,
            action=action,
            why=why,
            confidence=confidence,
            rewrite=rewrite_text,
            requested_action=requested,
        ),
    )


def _parse_batch(
    raw_text: str,
    batch: list[dict[str, Any]],
    start: int,
    end: int,
    *,
    extract_json_object: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """解析一批输出 → 归一化条目 + 问题清单（多余段 id / 重复段 id 都点名）。"""
    issues: list[str] = []
    try:
        data = extract_json_object(raw_text or "")
    except InputError as exc:
        return [], [f"第 {start}–{end} 段：{exc}"]
    raw_items = data.get("decisions")
    if raw_items is None:
        raw_items = data.get("items")
    items = _normalize_plan_items(raw_items)
    expected = {str(p.get("id")) for p in batch}
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        pid = item["paragraph"]
        if pid not in expected:
            issues.append(f"第 {start}–{end} 段：模型返回了不属于这一批的段 id {pid}，已忽略")
            continue
        if pid in seen:
            issues.append(f"{pid}：模型重复返回了这一段的决策，只取第一条")
            continue
        seen.add(pid)
        kept.append(item)
    return kept, issues


# ------------------------------------------------------------------ 合并请求 --
def _batch_ranges(paragraphs: list[dict[str, Any]], max_chars_per_batch: int) -> list[list[int]]:
    """把段落按字符数切成若干批（返回每批的下标区间，段序不变、不跨批重排）。"""
    batches: list[list[int]] = []
    current: list[int] = []
    size = 0
    for index, paragraph in enumerate(paragraphs):
        length = len(str(paragraph.get("text") or ""))
        if current and size + length > max_chars_per_batch:
            batches.append(current)
            current, size = [], 0
        current.append(index)
        size += length
    if current:
        batches.append(current)
    return batches


def _format_profile_block(reader: str, profile: dict[str, Any], overrides: list[dict[str, Any]]) -> str:
    """画像素描（只给人话，不泄漏内部结构）；空画像时把降级写在提示词里。"""
    concepts = profile.get("concepts") or {}
    lines: list[str] = []
    for cid, entry in concepts.items():
        entry = entry if isinstance(entry, dict) else {}
        lines.append(str(cid) + "=" + str(entry.get("state") or "unknown"))
    if lines:
        header = "读者 " + reader + " 自报概念状态：" + "；".join(lines)
    else:
        header = (
            "读者 " + reader + " 没有提供任何概念自评（画像为空）："
            "请只依据下面的概念难度提示判断，并在 why.profile 里写空数组 []"
        )
    if overrides:
        notes = []
        for override in overrides:
            if not isinstance(override, dict):
                continue
            note = str(override.get("note") or "").strip()
            notes.append(f"{override.get('concept')}={override.get('state')}" + (f"（{note}）" if note else ""))
        if notes:
            header += "\n读者手动推翻的结论（优先级最高，不得覆盖）：" + "；".join(notes)
    return header


def _format_concept_block(source_id: str, inventory: dict[str, Any] | None) -> str:
    """概念清单素描：概念名 + 难度 + 出现段 + 前置（只给重写需要的部分，省 token）。"""
    if not inventory:
        return "（没有概念清单：概念抽取失败或未运行 analyze。只依据画像决策，拿不准就保持原文）"
    concepts = inventory.get("concepts") or []
    if not concepts:
        return "（概念清单为空：等同没有概念清单）"
    lines: list[str] = []
    for concept in concepts[:40]:
        cid = concept.get("id")
        name = concept.get("name")
        hint = concept.get("difficulty_hint")
        paragraphs = "、".join(str(ref) for ref in (concept.get("paragraphs") or [])[:8])
        prereq = "、".join(str(ref) for ref in (concept.get("prereq") or []))
        surface = "／".join(str(item) for item in (concept.get("surface") or [])[:3])
        line = f"{cid} {name}（难度 {hint if hint is not None else '未知'}；出现于 {paragraphs}"
        if prereq:
            line += f"；前置 {prereq}"
        if surface:
            line += f"；原文用词 {surface}"
        lines.append(line + "）")
    if len(concepts) > 40:
        lines.append(f"…另有 {len(concepts) - 40} 条概念未列出")
    return "\n".join(lines)


def _extract_json(raw_text: str) -> dict[str, Any]:
    from .analyze import extract_json_object

    return extract_json_object(raw_text)


def _plan_one_batch(
    client: ChatClient,
    ranges: list[int],
    paragraphs: list[dict[str, Any]],
    context: dict[str, Any],
) -> BatchPlan:
    """一次请求吃一批段落。"""
    start, end = ranges[0] + 1, ranges[-1] + 1
    batch = [paragraphs[index] for index in ranges]
    plan = BatchPlan(start=start, end=end, paragraphs=[str(p.get("id")) for p in batch])
    messages = build_rewrite_messages(
        context["source_id"],
        batch,
        profile_block=context["profile_block"],
        concept_block=context["concept_block"],
        start=start,
        end=end,
        total=context["total"],
    )
    try:
        reply = client.chat(messages, max_tokens=None)
    except Exception as exc:  # noqa: BLE001 - 调用失败要变成可见降级，不能让整篇崩掉
        plan.error = f"第 {start}–{end} 段：模型调用失败（{type(exc).__name__}）：{exc}"
        plan.degraded.append(plan.error)
        plan.items = [
            _finalize_items(paragraph, None, degraded=plan.degraded, unplanned=plan.unplanned)
            for paragraph in batch
        ]
        return plan

    plan.model = getattr(reply, "model", None)
    usage = getattr(reply, "usage", None)
    plan.usage = dict(usage) if isinstance(usage, dict) else {}
    raw_items, issues = _parse_batch(
        getattr(reply, "text", "") or "", batch, start, end, extract_json_object=_extract_json
    )
    plan.degraded.extend(issues)
    by_id = {item["paragraph"]: item for item in raw_items}
    plan.items = [
        _finalize_items(paragraph, by_id.get(str(paragraph.get("id"))), degraded=plan.degraded, unplanned=plan.unplanned)
        for paragraph in batch
    ]
    return plan


def _plan_batches_parallel(
    client: ChatClient,
    ranges: list[list[int]],
    paragraphs: list[dict[str, Any]],
    context: dict[str, Any],
) -> list[BatchPlan]:
    """并发发完这些请求（各批互相独立，结果按下标对回原序），并发上限 4。"""
    cores = max(1, min(len(ranges), 4))
    with concurrent.futures.ThreadPoolExecutor(max_workers=cores) as pool:
        futures = [
            pool.submit(_plan_one_batch, client, batch_ranges, paragraphs, context) for batch_ranges in ranges
        ]
        return [future.result() for future in futures]


def _plan_batches_warmup(
    client: ChatClient,
    ranges: list[list[int]],
    paragraphs: list[dict[str, Any]],
    context: dict[str, Any],
) -> list[BatchPlan]:
    """**先串行跑第 1 批，再并发跑其余批次**（顺序 = 段序，结果按下标对回）。

    为什么：提示词前缀缓存（prompt caching）按「从头逐字相同的部分」命中，而缓存构建要几秒。
    6 批同时首发时谁都吃不到刚建好的缓存（全部按未命中计费）；先让第 1 批把固定前缀
    （画像 + 概念清单 + JSON 示例 + 规则，约 2007 字符）在模型侧建起来，后面的批次才可能命中。

    第 1 批失败**不影响**其余批次：``_plan_one_batch`` 把调用失败变成带 ``error`` 的可见降级
    （该批段落按 keep 处理并进 ``meta.model_errors``），其余批次照常发。
    """
    if not ranges:
        return []
    first = _plan_one_batch(client, ranges[0], paragraphs, context)
    rest = ranges[1:]
    if not rest:
        return [first]
    return [first, *_plan_batches_parallel(client, rest, paragraphs, context)]


def _plan_batches_serial(
    client: ChatClient,
    ranges: list[list[int]],
    paragraphs: list[dict[str, Any]],
    context: dict[str, Any],
) -> list[BatchPlan]:
    return [_plan_one_batch(client, batch_ranges, paragraphs, context) for batch_ranges in ranges]


# ------------------------------------------------------------------ 对外接口 --
def planDecisions(
    source_doc: dict[str, Any],
    inventory: dict[str, Any] | None,
    profile: dict[str, Any],
    client: ChatClient,
    *,
    source_id: str = "",
    reader: str = "",
    max_chars_per_batch: int = DEFAULT_MAX_CHARS_PER_BATCH,
) -> tuple[list[Decision], list[str], dict[str, Any]]:
    """逐段决策：``(decisions, degraded, meta)``。

    ``decisions`` **每段一条**（缺的补 keep + 可见降级）；``meta`` 里带 ``usage`` / 请求次数 /
    模型名 / 提示词版本，供回归基线与 token 结算，另有 ``model_errors``（批次失败：
    批次名 / 受影响段 id / 错误）、``unplanned_paragraphs``（模型没给决策的段）与
    ``rewrite_incomplete``——这三样是「有段落没被改写」的机器可读证据（CLI 据此以退出码 6 结束）。

    ``usage`` 的 3 个旧字段（``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``）口径不变，
    追加 ``cached_tokens``（前缀缓存命中的 prompt token 合计，网关没上报时为 ``null``）；
    ``meta.batch_usage`` 给逐批明细，用来判断预热是否真的生效。
    """
    paragraphs = [p for p in (source_doc.get("paragraphs") or []) if isinstance(p, dict) and p.get("id")]
    if not paragraphs:
        raise InputError("source.meta.json 里没有任何段落，无法逐段决策")

    sid = source_id or str(source_doc.get("source_id") or "")
    reader_id = reader or str(profile.get("reader") or "")
    context = {
        "source_id": sid,
        "total": len(paragraphs),
        "profile_block": _format_profile_block(reader_id, profile, list(profile.get("overrides") or [])),
        "concept_block": _format_concept_block(sid, inventory),
    }
    ranges = _batch_ranges(paragraphs, max(1, int(max_chars_per_batch)))
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 先串行第 1 批做前缀预热，再并发其余批次（见 ``_plan_batches_warmup``）
        plans = _plan_batches_warmup(client, ranges, paragraphs, context)
    else:  # pragma: no cover - 已有事件循环时（HTTP 层内）退回串行，避免嵌套
        plans = _plan_batches_serial(client, ranges, paragraphs, context)

    decisions: list[Decision] = []
    degraded: list[str] = []
    model_errors: list[dict[str, Any]] = []
    unplanned: list[str] = []
    usage: dict[str, Any] = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    model: str | None = None
    usage_fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    cached_total = 0
    cached_reported = False
    batch_usage: list[dict[str, Any]] = []
    for plan in plans:
        decisions.extend(item.decision for item in plan.items)
        degraded.extend(plan.degraded)
        unplanned.extend(plan.unplanned)
        usage["requests"] += 1
        model = plan.model or model
        for field_name in usage_fields:
            value = plan.usage.get(field_name)
            if isinstance(value, int):
                usage[field_name] += value
        # 前缀缓存命中量：网关可能在 prompt_tokens_details.cached_tokens 或扁平字段里回
        # （见 ``model.cached_tokens_of``）；没上报就是 None，不许编造成 0。
        cached = cached_tokens_of(plan.usage)
        if cached is not None:
            cached_reported = True
            cached_total += cached
        batch_entry: dict[str, Any] = {"batch": plan.batch_label, "paragraph_count": len(plan.paragraphs)}
        for field_name in usage_fields:
            value = plan.usage.get(field_name)
            if isinstance(value, int):
                batch_entry[field_name] = value
        if cached is not None:
            batch_entry["cached_tokens"] = cached
        if plan.error:
            batch_entry["failed"] = True
        batch_usage.append(batch_entry)
        if plan.error:
            # 结构化的失败证据：哪一批、哪几段、什么错（只进产物，不进提示词）。
            # error 里不再重复批次名——批次名单独放在 batch 字段里。
            prefix = f"{plan.batch_label}："
            error = plan.error[len(prefix):] if plan.error.startswith(prefix) else plan.error
            model_errors.append(
                {"batch": plan.batch_label, "paragraphs": list(plan.paragraphs), "error": error}
            )
    # 至少有一批上报过缓存命中时才写这个字段（null 与 0 分得开：null = 网关没上报）
    usage["cached_tokens"] = cached_total if cached_reported else None

    if model_errors or unplanned:
        # 事实锚只能证明「没改坏」，不能区分「忠实改写」与「一个字没改」：
        # 有段落没被改写这件事必须一路传到退出码（CLI 用 6）。
        degraded.append(
            "改写不完整："
            + f"{len(model_errors)} 个批次模型调用失败、{len(unplanned)} 段模型没有给出决策；"
            "这些段落保持原文（未被改写），重跑 `ar rewrite` 可补齐（退出码 6）"
        )

    meta: dict[str, Any] = {
        "model": model,
        "prompt_version": REWRITE_PROMPT_VERSION,
        "request_count": len(ranges),
        "batch_count": len(ranges),
        "usage": usage,
        # 逐批的 token 与缓存命中（证明「预热到底有没有生效」的原始证据：第 1 批通常 0 命中，
        # 后面几批才有）。字段名与 ``usage`` 的 3 个旧字段一致，便于对照。
        "batch_usage": batch_usage,
        "model_errors": model_errors,
        "unplanned_paragraphs": unplanned,
        "rewrite_incomplete": bool(model_errors or unplanned),
    }
    return decisions, degraded, meta


def _markdown_from(paragraphs: list[dict[str, Any]], items_by_id: dict[str, PlanItem]) -> str:
    """逐段拼改写稿：段序与源一致、段间空行分隔、段首不写 id。"""
    blocks: list[str] = []
    for paragraph in paragraphs:
        pid = str(paragraph.get("id"))
        item = items_by_id.get(pid)
        text = item.decision.rewrite if item and item.needs_rewrite else ""
        blocks.append(text if text.strip() else str(paragraph.get("text") or ""))
    return "\n\n".join(blocks) + "\n"


def rewrite(
    source_doc: dict[str, Any],
    decisions: list[Decision],
    profile: dict[str, Any],
    *,
    mode: str = DEFAULT_MODE,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """按决策拼出 ``RewrittenDoc``（``{"markdown","paragraphs"}``）与 ``Changes``（``changes[]``）。

    纯函数：不发请求、不写盘，方便测试与重跑。
    """
    if mode == "strict":  # pragma: no cover - CLI 在更早的位置就报错了，这里兜底
        raise NotImplementedYet(STRICT_NOT_IMPLEMENTED)
    paragraphs = [p for p in (source_doc.get("paragraphs") or []) if isinstance(p, dict) and p.get("id")]
    by_id = {decision.paragraph: decision for decision in decisions}
    items_by_id = {
        str(paragraph.get("id")): PlanItem(paragraph=paragraph, decision=by_id[str(paragraph.get("id"))])
        for paragraph in paragraphs
        if str(paragraph.get("id")) in by_id
    }

    blocks: list[str] = []
    changes: list[dict[str, Any]] = []
    for paragraph in paragraphs:
        pid = str(paragraph.get("id"))
        item = items_by_id.get(pid)
        if item is None:
            # 没有决策的段：按 keep 处理（可见降级由上层记账）
            blocks.append(str(paragraph.get("text") or ""))
            continue
        decision = item.decision
        text = decision.rewrite if item.needs_rewrite else ""
        source_text = str(paragraph.get("text") or "")
        blocks.append(text if text.strip() else source_text)
        facts = facts_of(text if text.strip() else source_text)
        change: dict[str, Any] = {
            "paragraph": pid,
            "action": decision.action,
            "why": {"profile": list(decision.why.get("profile") or []), "note": str(decision.why.get("note") or "")},
            "confidence": decision.confidence,
            # 契约 fixture 逐字：facts 只含 numbers / quotes；书名号《》由 ar check 另行比对
            "facts": {"numbers": facts.numbers, "quotes": facts.quotes},
        }
        if decision.deferred:
            change["deferred"] = True
        changes.append(change)

    doc = {"markdown": "\n\n".join(blocks) + "\n", "paragraphs": blocks}
    return doc, changes


def _load_meta(data_dir: Path | str, source_id: str) -> dict[str, Any]:
    meta_path = doc_dir(data_dir, source_id) / "source.meta.json"
    if not meta_path.is_file():
        raise InputError(f"{meta_path} 不存在：请先运行 `ar ingest <文件> --id {source_id}`")
    meta = read_json(meta_path)
    if not isinstance(meta, dict):
        raise InputError(f"{meta_path} 根节点不是 JSON 对象")
    return meta


def _usage_summary(usage: dict[str, Any]) -> str:
    """人读的一行 token 账（含前缀缓存命中量；网关没上报就写「未上报」，不编造 0）。"""
    line = (
        f"请求 {usage.get('requests', 0)} 次；"
        f"tokens：prompt {usage.get('prompt_tokens', 0)} / completion {usage.get('completion_tokens', 0)} / "
        f"合计 {usage.get('total_tokens', 0)}"
    )
    cached = usage.get("cached_tokens")
    if isinstance(cached, int):
        prompt = int(usage.get("prompt_tokens") or 0)
        ratio = f"，占 prompt 的 {cached / prompt:.0%}" if prompt else ""
        line += f"；前缀缓存命中 {cached}{ratio}"
    else:
        line += "；前缀缓存命中：网关未上报"
    return line


@dataclass
class CheckOutcome:
    """``ar check`` 的事实锚结果（``counts`` 用来说明校验真的跑了多少项）。

    - ``problems``：**校验所需的产物缺失 / 不可读 / 为空**（``changes.json`` 没了之类）。
      调用方按输入错误的语义处理（退出码 2），不能因为「没得比」就报通过；
    - ``mismatches``：事实锚不一致 + 段缺失 + ``empty_check``（0 比对项 = 检查器空跑，退出码 4）；
    - ``fact_problems``：``compare_blocks`` 直接给出的人读清单（CLI 打印的就是它，不是算完就丢）；
    - ``model_errors`` / ``unplanned_paragraphs``：改写阶段就有段落没被改写（退出码 6）。
    """

    source_id: str
    counts: dict[str, int]
    mismatches: list[dict[str, Any]]
    problems: list[str] = field(default_factory=list)
    #: ``compare_blocks`` 直接产出的人读问题清单（与 ``mismatches`` 同一次比对，不另算一遍）
    fact_problems: list[str] = field(default_factory=list)
    model_errors: list[dict[str, Any]] = field(default_factory=list)
    unplanned_paragraphs: list[str] = field(default_factory=list)
    checked: bool = False
    paths: dict[str, Path] = field(default_factory=dict)


def check_fidelity(
    source_id: str,
    data_dir: Path | str,
    *,
    at: str | None = None,
    write_back: bool = True,
) -> CheckOutcome:
    """事实锚校验：读 ``rewritten.md`` 与 ``source.meta.json`` 逐段比对三类锚点。

    - 段缺失 / 段数对不上 → 点名段 id；
    - 数字 / 引号引文 / 书名号不一致 → 点名段 id 与具体差异；
    - **0 比对项 = 检查器空跑**（空文档）：照样算失败，``checked`` 保持 false；
    - ``changes.json`` 缺失 / 不是对象 / ``changes`` 不是数组、为空、或条数与材料段数不一致
      → 记进 ``problems``（调用方按输入错误处理，退出码 2）：缺了逐段变更记录就没法追责，
      不能静默通过；
    - 结果写回 ``changes.json`` 的 ``fidelity``（``mismatches`` + ``counts`` + ``checked``），
      失败时也写——「校验跑过、结果是坏的」要留痕，不能只有退出码。
      ``checked`` 只有在**本次比对项 > 0 且没有任何不一致**时才为 ``true``。

    返回的 ``counts`` 供 CLI 打印；问题清单由调用方分别交给 ``InputError``（2）/
    ``ValidationFailed``（4）/ ``ModelPartiallyFailed``（6）。
    """
    from .fidelity import compare_blocks, render_fact_report  # noqa: PLC0415 - 同包内避免顶层循环

    meta = _load_meta(data_dir, source_id)
    target = doc_dir(data_dir, source_id)
    rewritten_path = target / "rewritten.md"
    changes_path = target / "changes.json"
    if not rewritten_path.is_file():
        raise InputError(
            f"{rewritten_path} 不存在：请先运行 `ar rewrite {source_id} --reader <读者>`（没有改写稿就无从校验事实锚）"
        )

    markdown = rewritten_path.read_text(encoding="utf-8")
    blocks = split_rewritten_blocks(markdown)
    source_paragraphs = [p for p in (meta.get("paragraphs") or []) if isinstance(p, dict) and p.get("id")]
    fact_problems, mismatches, counts = compare_blocks(source_paragraphs, blocks)

    artifact_problems: list[str] = []
    model_errors: list[dict[str, Any]] = []
    unplanned: list[str] = []
    changes = read_json_if_exists(changes_path)
    if not isinstance(changes, dict):
        artifact_problems.append(
            f"{changes_path} 不存在或不是 JSON 对象：没有逐段变更记录（哪一段被怎么改、依据是什么），"
            f"无法追责也没法写回校验结果——先运行 `ar rewrite {source_id}`"
        )
    else:
        records = changes.get("changes")
        if not isinstance(records, list):
            artifact_problems.append(
                f"{changes_path} 的 changes 不是数组（实际 {type(records).__name__}）：逐段变更记录不可读"
            )
        elif not records:
            artifact_problems.append(
                f"{changes_path} 的 changes 是空数组：0 条逐段变更记录，等于没有变更账可对（先运行 `ar rewrite`）"
            )
        elif len(records) != len(source_paragraphs):
            # run_rewrite 保证「每段一条」（缺决策的段也会补一条可见降级的 keep），
            # 所以条数对不上只可能是产物被截断 / 与材料版本不匹配——同样是「没得比」。
            artifact_problems.append(
                f"{changes_path} 有 {len(records)} 条逐段变更记录，而 source.meta.json 有 "
                f"{len(source_paragraphs)} 段：变更账不完整（产物被截断或与当前材料不是同一次改写），"
                "无法逐段追责（重跑 `ar rewrite`）"
            )
        meta_block = changes.get("meta")
        meta_block = meta_block if isinstance(meta_block, dict) else {}
        model_errors = [item for item in (meta_block.get("model_errors") or []) if isinstance(item, dict)]
        unplanned = [str(pid) for pid in (meta_block.get("unplanned_paragraphs") or []) if str(pid).strip()]

        fidelity = changes.get("fidelity")
        fidelity = fidelity if isinstance(fidelity, dict) else {}
        # checked 的语义 = 「本次逐段事实锚比对跑过、且没有任何不一致」；
        # 0 比对项（空文档）与任何 mismatch 都不许写成「已查过、干净」。
        checked = bool(counts.get("compared", 0)) and not mismatches
        fidelity.update(
            {
                "checked": checked,
                "checked_at": at or now_iso(),
                "mismatches": mismatches,
                "counts": counts,
                "summary": render_fact_report(counts),
                "checked_by": "ar check（数字 / 引号引文 / 书名号《》三类 + 段缺失）",
            }
        )
        fidelity["paragraphs_match_source"] = not any(
            item.get("kind") in ("paragraph_missing", "paragraph_extra") for item in mismatches
        )
        changes["fidelity"] = fidelity
        if write_back:
            write_json(changes_path, changes)

    return CheckOutcome(
        source_id=source_id,
        counts=counts,
        mismatches=mismatches,
        problems=artifact_problems,
        fact_problems=fact_problems,
        model_errors=model_errors,
        unplanned_paragraphs=unplanned,
        checked=bool(counts.get("compared", 0)) and not mismatches,
        paths={"rewritten": rewritten_path, "changes": changes_path, "meta": target / "source.meta.json"},
    )


def fidelity_problems(outcome: CheckOutcome) -> list[str]:
    """把一次 ``check_fidelity`` 的结果翻成人看的违规清单（每条点名段 id）。

    优先用 ``compare_blocks`` 直接给出的 ``fact_problems``（同一次比对，不重算、不丢）；
    只有手工构造的 ``CheckOutcome``（没有 ``fact_problems``）才退回按 ``mismatches`` 渲染。
    """
    if outcome.fact_problems:
        return list(outcome.fact_problems)
    problems: list[str] = []
    for item in outcome.mismatches:
        pid = item.get("paragraph") or "（段 id 未知）"
        kind = item.get("kind")
        if kind == "empty_check":
            problems.append(f"检查器空跑：{item.get('detail') or '0 比对项'}——0 比对项不算通过")
            continue
        if kind == "paragraph_missing":
            problems.append(f"{pid}：改写稿里没有对应的段落（段缺失）")
            continue
        if kind == "paragraph_extra":
            problems.append(f"{pid}：改写稿多出了原文没有的段落")
            continue
        label = {"numbers": "数字", "quotes": "引号引文", "books": "书名号书名"}.get(str(kind), str(kind))
        parts: list[str] = []
        if item.get("missing"):
            parts.append("改写稿里少了 " + "、".join(f"「{x}」" for x in item["missing"][:3]))
        if item.get("extra"):
            parts.append("改写稿里多出 " + "、".join(f"「{x}」" for x in item["extra"][:3]))
        problems.append(f"{pid}：{label}与原文不一致（{'；'.join(parts) or item.get('detail') or ''}）")
    return problems


def incomplete_reasons(outcome: CheckOutcome | RewriteOutcome) -> list[str]:
    """把「有段落没被改写」翻成人看的明细（批次 / 受影响段 id / 错误）。"""
    lines: list[str] = []
    for item in outcome.model_errors:
        paragraphs = "、".join(str(pid) for pid in (item.get("paragraphs") or [])) or "（段 id 未知）"
        lines.append(f"{item.get('batch') or '（批次未知）'}：{item.get('error')}；受影响段：{paragraphs}")
    if outcome.unplanned_paragraphs:
        lines.append("模型没有给出决策的段：" + "、".join(outcome.unplanned_paragraphs))
    return lines


def run_rewrite(
    source_id: str,
    data_dir: Path | str,
    profiles_dir: Path | str,
    reader: str,
    client: ChatClient,
    *,
    mode: str = DEFAULT_MODE,
    max_chars_per_batch: int = DEFAULT_MAX_CHARS_PER_BATCH,
    at: str | None = None,
) -> RewriteOutcome:
    """``ar rewrite`` 的实现：决策 → 改写 → 写三份产物，并把降级全部记下来。"""
    if mode not in MODES:
        raise InputError(f"mode 非法：{mode!r}（只能是 {' / '.join(MODES)}）")
    if mode == "strict":
        raise NotImplementedYet(STRICT_NOT_IMPLEMENTED)

    from .profile import load_profile

    meta = _load_meta(data_dir, source_id)
    target = doc_dir(data_dir, source_id)
    inventory = read_json_if_exists(target / "concepts.json")
    inventory = inventory if isinstance(inventory, dict) else None
    profile, profile_notes = load_profile(profiles_dir, reader)

    degraded: list[str] = list(profile_notes)
    profile_used = bool(profile.get("concepts") or profile.get("overrides"))
    profile_note = (
        f"依据画像 {reader}：{len(profile.get('concepts') or {})} 条概念自评"
        + (f"，{len(profile.get('overrides') or [])} 条手动推翻（优先级最高）" if profile.get("overrides") else "")
        if profile_used
        else f"无画像：profiles/{reader}/profile.yaml 里没有任何概念自评，本次只按概念清单的难度提示决策（已在产物里标注）"
    )
    if not profile_used:
        degraded.append(
            "无画像模式："
            + profile_note
            + "；每段 why.profile 只能引用概念清单里的 difficulty（前端显示为「难度 N（无画像）」）"
        )
    if inventory is None:
        degraded.append(
            "concepts.json 不存在：没有概念清单，决策只能依据画像（可能全篇保持原文）。"
            f"先运行 `ar analyze {source_id}`（需要模型）"
        )

    decisions, plan_degraded, plan_meta = planDecisions(
        meta,
        inventory,
        profile,
        client,
        source_id=source_id,
        reader=reader,
        max_chars_per_batch=max_chars_per_batch,
    )
    degraded.extend(plan_degraded)

    # 空画像：why.profile 必须看得见地说明「依据来自哪里」。
    # 有画像时保留模型给的 profile 引用（提示词只允许引用画像与概念清单里的 id）。
    concept_paragraphs: dict[str, list[str]] = {}
    concept_difficulty: dict[str, Any] = {}
    for concept in (inventory or {}).get("concepts") or []:
        cid = str(concept.get("id"))
        concept_paragraphs[cid] = [str(ref) for ref in (concept.get("paragraphs") or [])]
        concept_difficulty[cid] = concept.get("difficulty_hint")
    for decision in decisions:
        if not profile_used:
            refs = [cid for cid, refs_ in concept_paragraphs.items() if decision.paragraph in refs_]
            hint = concept_difficulty.get(refs[0]) if refs else None
            if refs and hint is not None:
                decision.why["profile"] = [f"难度 {hint}（无画像）"]
            else:
                decision.why["profile"] = []
                decision.why["note"] = str(decision.why.get("note") or "") + "（无画像：本段没有可依据的读者信号）"
        else:
            decision.why["profile"] = [str(item) for item in (decision.why.get("profile") or [])]

    doc, changes = rewrite(meta, decisions, profile, mode=mode)
    markdown = str(doc["markdown"])
    blocks = split_rewritten_blocks(markdown)
    source_paragraphs = [p for p in (meta.get("paragraphs") or []) if isinstance(p, dict) and p.get("id")]
    by_decision = {decision.paragraph: decision for decision in decisions}
    # 段数 / 段序与源一致；**保持**的段落必须与原文逐字节一致（原文由摄取时算的 text_hash 背书）
    count_mismatch: list[str] = []
    if len(blocks) != len(source_paragraphs):
        count_mismatch.append(f"改写稿有 {len(blocks)} 段，原文有 {len(source_paragraphs)} 段")
    keep_mismatch: list[str] = []
    for paragraph, block in zip(source_paragraphs, blocks):
        pid = str(paragraph.get("id"))
        decision = by_decision.get(pid)
        if decision is not None and decision.action == "expand":
            continue  # 展开段本来就会变，事实锚由 ar check 逐段比对
        source_text = str(paragraph.get("text") or "")
        if text_hash(source_text) != text_hash(block):
            keep_mismatch.append(pid)
    if keep_mismatch:
        degraded.append(
            "以下「保持」段落的正文与原文不一致（原文锚点 text_hash 对不上）：" + "、".join(keep_mismatch)
        )
    paragraphs_match_source = not count_mismatch and not keep_mismatch
    # 事实锚的逐段比对由 `ar check` 负责，并把结果写回这里（changes.json 的 fidelity.*）。
    # **rewrite 阶段不许写 checked: true**：那时比对还没跑，写成 true 会被误读成「已查过、干净」
    # （复核实测抓到的 P4）。counts 同理留空，等 check 填。
    fidelity: dict[str, Any] = {
        "checked": False,
        "checked_at": None,
        "mismatches": [],
        "counts": None,
        "summary": "尚未校验：逐段事实锚比对由 `ar check <source_id>` 执行并回写本字段",
        "paragraphs_match_source": paragraphs_match_source,
        "count_mismatch": count_mismatch,
        "keep_paragraphs_mismatch": keep_mismatch,
        "note": "facts 与 fidelity 的逐段事实锚比对结果由 `ar check <source_id>` 复核（含数字 / 引文 / 书名号）",
    }

    counts = {action: 0 for action in ACTIONS}
    for decision in decisions:
        counts[decision.action] = counts.get(decision.action, 0) + 1
    deferred_count = sum(1 for decision in decisions if decision.deferred)
    usage = dict(plan_meta.get("usage") or {})
    batch_usage = [dict(item) for item in (plan_meta.get("batch_usage") or []) if isinstance(item, dict)]
    model_errors = [
        dict(item) for item in (plan_meta.get("model_errors") or []) if isinstance(item, dict)
    ]
    unplanned = [str(pid) for pid in (plan_meta.get("unplanned_paragraphs") or []) if str(pid).strip()]

    outcome = RewriteOutcome(
        source_id=source_id,
        reader=reader,
        mode=mode,
        decisions=decisions,
        changes=changes,
        fidelity=fidelity,
        rewritten_markdown=markdown,
        degraded=degraded,
        model_errors=model_errors,
        unplanned_paragraphs=unplanned,
        model=plan_meta.get("model"),
        usage=usage,
        request_count=int(plan_meta.get("request_count") or 0),
        batch_count=int(plan_meta.get("batch_count") or 0),
        profile_used=profile_used,
        profile_note=profile_note,
        deferred_count=deferred_count,
    )

    generated_at = at or now_iso()
    decisions_payload: dict[str, Any] = {
        "decisions": [decision.to_record() for decision in decisions],
        "meta": {
            "source_id": source_id,
            "reader": reader,
            "mode": mode,
            "model": outcome.model,
            "prompt_version": REWRITE_PROMPT_VERSION,
            "generated_at": generated_at,
            "paragraph_count": len(meta.get("paragraphs") or []),
            "action_counts": counts,
            "implemented_actions": list(IMPLEMENTED_ACTIONS),
            "deferred_count": deferred_count,
            "profile_used": profile_used,
            "profile_note": profile_note,
            "request_count": outcome.request_count,
            "usage": usage,
            "batch_usage": batch_usage,
            "model_errors": model_errors,
            "unplanned_paragraphs": unplanned,
            "rewrite_incomplete": outcome.incomplete,
            "degraded": list(degraded),
        },
    }
    changes_payload: dict[str, Any] = {
        "changes": changes,
        "fidelity": fidelity,
        "meta": {
            "source_id": source_id,
            "reader": reader,
            "mode": mode,
            "model": outcome.model,
            "prompt_version": REWRITE_PROMPT_VERSION,
            "generated_at": generated_at,
            "action_counts": counts,
            "deferred_count": deferred_count,
            "facts_note": "facts 只含数字与引号引文（与前端夹具同口径）；书名号《》由 `ar check` 另行比对",
            "profile_used": profile_used,
            "profile_note": profile_note,
            "usage": usage,
            "batch_usage": batch_usage,
            "model_errors": model_errors,
            "unplanned_paragraphs": unplanned,
            "rewrite_incomplete": outcome.incomplete,
            "degraded": list(degraded),
        },
    }
    outcome.decisions_path = write_json(target / "decisions.json", decisions_payload)
    outcome.changes_path = write_json(target / "changes.json", changes_payload)
    outcome.rewritten_path = write_text(target / "rewritten.md", markdown)
    outcome.usage_note = _usage_summary(usage)
    return outcome
