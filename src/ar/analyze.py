"""分析 Analyze（M2）：概念清单抽取 + 契约校验 → ``data/<id>/concepts.json``。

产物（DESIGN §4.2，字段逐字不改名）::

    {
      "concepts": [{"id","name","surface","prereq","difficulty_hint","paragraphs","domain","concept_key"}],
      "illustration_candidates": [{"paragraph","kind","why"}],
      "meta": {"model","prompt_version","generated_at", ...}   # 回归基线，见 DESIGN §7
    }

两条纪律：

- ``kind`` 只能是 **流程 / 结构 / 对比 / 数量**；``paragraphs`` 与 ``paragraph`` 只能引用
  ``source.meta.json`` 里真实存在的段 id。写盘前必过 ``validate_inventory()``，不过就不写。
- 模型输出里指向不存在段 id 的引用会被**过滤并记在 ``meta.repairs`` 里**（可见，不静默）；
  过滤后一条都不剩的概念直接丢弃并记账。这样落盘的文件永远满足契约，
  而"模型说了不存在的段"这件事仍然看得见。

``uv run ar analyze <id> --check-only`` 只校验已有 ``concepts.json``，不调模型。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import InputError, ModelUnavailable, ValidationFailed
from .ingest import paragraph_id
from .jsonio import now_iso, read_json, read_json_if_exists, write_json, write_text
from .paths import doc_dir
from .prompts import CONCEPTS_PROMPT_VERSION, build_concepts_messages

__all__ = [
    "CONCEPT_ID_RE",
    "ILLUSTRATION_KINDS",
    "REQUIRED_CONCEPT_FIELDS",
    "REQUIRED_ILLUSTRATION_FIELDS",
    "AnalyzeResult",
    "extract_json_object",
    "normalize_inventory",
    "validate_inventory",
    "run_analyze",
    "check_inventory_file",
]

CONCEPT_ID_RE = re.compile(r"^c\d{2,}$")
ILLUSTRATION_KINDS = ("流程", "结构", "对比", "数量")
REQUIRED_CONCEPT_FIELDS = (
    "id",
    "name",
    "surface",
    "prereq",
    "difficulty_hint",
    "paragraphs",
    "domain",
    "concept_key",
)
REQUIRED_ILLUSTRATION_FIELDS = ("paragraph", "kind", "why")

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


# ------------------------------------------------------------------ JSON 抽取 --
def _first_balanced_object(text: str) -> str | None:
    """从自由文本里抠出第一个花括号平衡的 JSON 对象（跳过字符串内的括号）。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_json_object(text: str) -> dict[str, Any]:
    """从模型输出里取 JSON 对象：容忍代码围栏、前后废话。"""
    text = text or ""
    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    candidates += [m.group(1).strip() for m in _FENCE_RE.finditer(text)]
    balanced = _first_balanced_object(text)
    if balanced:
        candidates.append(balanced)
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise InputError("模型输出里找不到合法的 JSON 对象")


# ------------------------------------------------------------------ 归一化 --
def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _paragraph_ids(meta: dict[str, Any]) -> list[str]:
    return [str(p.get("id")) for p in (meta.get("paragraphs") or []) if p.get("id")]


def normalize_inventory(raw: dict[str, Any], meta: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """把模型输出整理成契约形态，并记录所有改动（repairs）。"""
    valid_paragraphs = set(_paragraph_ids(meta))
    repairs: list[str] = []
    concepts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    raw_concepts = raw.get("concepts")
    if not isinstance(raw_concepts, list):
        raise InputError("模型输出里 concepts 不是数组（无法整理成概念清单）")

    for position, item in enumerate(raw_concepts, start=1):
        if not isinstance(item, dict):
            repairs.append(f"第 {position} 条概念不是对象，已丢弃")
            continue
        auto_id = f"c{position:02d}"
        cid = item.get("id")
        if not isinstance(cid, str) or not CONCEPT_ID_RE.match(cid.strip()):
            repairs.append(f"第 {position} 条概念的 id {cid!r} 不合规，改用 {auto_id}")
            cid = auto_id
        cid = cid.strip()
        if cid in seen_ids:
            repairs.append(f"概念 id {cid} 重复，第 {position} 条改用 {auto_id}")
            cid = auto_id
            if cid in seen_ids:
                repairs.append(f"第 {position} 条概念 id 仍冲突，已丢弃")
                continue

        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            repairs.append(f"{cid}: 缺 name，已丢弃")
            continue

        surface = _as_str_list(item.get("surface"))
        if not surface:
            repairs.append(f"{cid}: surface 为空，已用 name 顶替")
            surface = [name.strip()]

        prereq = _as_str_list(item.get("prereq"))
        kept_prereq: list[str] = []
        for ref in prereq:
            ref = ref.strip()
            if not CONCEPT_ID_RE.match(ref) or ref == cid:
                repairs.append(f"{cid}: prereq 里的 {ref!r} 不合规，已去掉")
                continue
            kept_prereq.append(ref)

        difficulty = item.get("difficulty_hint")
        if isinstance(difficulty, bool) or not isinstance(difficulty, (int, float)):
            if difficulty is not None:
                repairs.append(f"{cid}: difficulty_hint {difficulty!r} 不是数字，已置空")
            difficulty = None
        elif not 1 <= int(difficulty) <= 5:
            repairs.append(f"{cid}: difficulty_hint {difficulty} 超出 1–5，已截到边界")
            difficulty = max(1, min(5, int(difficulty)))
        else:
            difficulty = int(difficulty)

        refs = [str(r).strip() for r in _as_str_list(item.get("paragraphs"))]
        kept_refs: list[str] = []
        for ref in refs:
            if ref in valid_paragraphs:
                if ref not in kept_refs:
                    kept_refs.append(ref)
            else:
                repairs.append(f"{cid}: paragraphs 引用了不存在的段 id {ref!r}，已去掉")
        if not kept_refs:
            repairs.append(f"{cid}: 过滤后没有任何有效段落引用，已丢弃该概念")
            continue

        domain = item.get("domain")
        concept_key = item.get("concept_key")
        if not isinstance(domain, str):
            if domain is not None:
                repairs.append(f"{cid}: domain 不是字符串，已置空")
            domain = ""
        if not isinstance(concept_key, str):
            if concept_key is not None:
                repairs.append(f"{cid}: concept_key 不是字符串，已置空")
            concept_key = ""

        seen_ids.add(cid)
        concepts.append(
            {
                "id": cid,
                "name": name.strip(),
                "surface": surface,
                "prereq": kept_prereq,
                "difficulty_hint": difficulty,
                "paragraphs": kept_refs,
                "domain": domain.strip(),
                "concept_key": concept_key.strip(),
            }
        )

    # prereq 只能指向本次输出里真实存在的概念
    existing = {c["id"] for c in concepts}
    for concept in concepts:
        kept = [ref for ref in concept["prereq"] if ref in existing]
        dropped = [ref for ref in concept["prereq"] if ref not in existing]
        if dropped:
            repairs.append(f"{concept['id']}: prereq 指向本次输出里不存在的概念 {', '.join(dropped)}，已去掉")
        concept["prereq"] = kept

    # 配图候选：段 id 必须存在、kind 必须在四类之内、why 必须非空，否则丢弃并记账
    candidates: list[dict[str, Any]] = []
    raw_illustrations = raw.get("illustration_candidates")
    if not isinstance(raw_illustrations, list):
        if raw_illustrations is not None:
            repairs.append("illustration_candidates 不是数组，已置空")
        raw_illustrations = []
    for position, item in enumerate(raw_illustrations, start=1):
        if not isinstance(item, dict):
            repairs.append(f"第 {position} 条配图候选不是对象，已丢弃")
            continue
        ref = item.get("paragraph")
        kind = item.get("kind")
        why = item.get("why")
        if not isinstance(ref, str) or ref.strip() not in valid_paragraphs:
            repairs.append(f"配图候选 {ref!r} 引用了不存在的段 id，已丢弃")
            continue
        if kind not in ILLUSTRATION_KINDS:
            repairs.append(f"配图候选 {ref}: kind {kind!r} 不在 {'/'.join(ILLUSTRATION_KINDS)} 之内，已丢弃")
            continue
        if not isinstance(why, str) or not why.strip():
            repairs.append(f"配图候选 {ref}: 缺 why，已丢弃")
            continue
        candidates.append({"paragraph": ref.strip(), "kind": kind, "why": why.strip()})

    return {"concepts": concepts, "illustration_candidates": candidates}, repairs


# ------------------------------------------------------------------ 校验 --
def validate_inventory(inventory: Any, meta: dict[str, Any]) -> list[str]:
    """严格校验概念清单契约；返回问题清单（空 = 通过）。

    负向用途：手工把某条 ``paragraphs`` 改成不存在的段 id、或把 ``kind`` 改成四类之外，
    这里必须报出来（CLI 以退出码 4 结束）。
    """
    problems: list[str] = []
    if not isinstance(inventory, dict):
        return ["concepts.json 根节点不是 JSON 对象"]

    valid_paragraphs = set(_paragraph_ids(meta))
    if not valid_paragraphs:
        problems.append("source.meta.json 里没有任何段落，无法校验引用")

    concepts = inventory.get("concepts")
    if not isinstance(concepts, list):
        return problems + ["concepts 不是数组"]
    if not concepts:
        problems.append("concepts 是空数组（没有抽出任何概念，等同降级为无画像模式，不应当作成功）")

    ids: list[str] = []
    for position, concept in enumerate(concepts, start=1):
        if not isinstance(concept, dict):
            problems.append(f"第 {position} 条概念不是对象")
            continue
        cid = concept.get("id")
        label = cid if isinstance(cid, str) and cid else f"第 {position} 条概念"
        if not isinstance(cid, str) or not CONCEPT_ID_RE.match(cid):
            problems.append(f"{label}: id 缺失或不合规（应形如 c01）")
        else:
            ids.append(cid)
        for field_name in REQUIRED_CONCEPT_FIELDS:
            if field_name not in concept:
                problems.append(f"{label}: 缺字段 {field_name}")
        name = concept.get("name")
        if not isinstance(name, str) or not name.strip():
            problems.append(f"{label}: name 必须是非空字符串")
        surface = concept.get("surface")
        if not isinstance(surface, list) or not all(isinstance(s, str) for s in surface) or not surface:
            problems.append(f"{label}: surface 必须是非空字符串数组")
        prereq = concept.get("prereq")
        if not isinstance(prereq, list) or not all(isinstance(s, str) for s in prereq):
            problems.append(f"{label}: prereq 必须是字符串数组")
        hint = concept.get("difficulty_hint")
        if hint is not None and (isinstance(hint, bool) or not isinstance(hint, int) or not 1 <= hint <= 5):
            problems.append(f"{label}: difficulty_hint 必须是 1–5 的整数或 null（实际 {hint!r}）")
        refs = concept.get("paragraphs")
        if not isinstance(refs, list) or not refs:
            problems.append(f"{label}: paragraphs 必须是非空数组")
        else:
            for ref in refs:
                if not isinstance(ref, str):
                    problems.append(f"{label}: paragraphs 里有非字符串项 {ref!r}")
                elif ref not in valid_paragraphs:
                    problems.append(f"{label}: paragraphs 引用了不存在的段 id {ref!r}")
        for field_name in ("domain", "concept_key"):
            if not isinstance(concept.get(field_name), str):
                problems.append(f"{label}: {field_name} 必须是字符串（可以为空串）")

    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    for dup in duplicates:
        problems.append(f"{dup}: 概念 id 重复出现")
    known_ids = set(ids)
    for concept in concepts:
        if not isinstance(concept, dict):
            continue
        for ref in _as_str_list(concept.get("prereq")):
            if ref not in known_ids:
                problems.append(f"{concept.get('id')}: prereq 指向不存在的概念 {ref!r}")
            if ref == concept.get("id"):
                problems.append(f"{concept.get('id')}: prereq 指向了自己")

    illustrations = inventory.get("illustration_candidates")
    if not isinstance(illustrations, list):
        problems.append("illustration_candidates 不是数组")
    else:
        for position, item in enumerate(illustrations, start=1):
            if not isinstance(item, dict):
                problems.append(f"第 {position} 条配图候选不是对象")
                continue
            for field_name in REQUIRED_ILLUSTRATION_FIELDS:
                if field_name not in item:
                    problems.append(f"第 {position} 条配图候选缺字段 {field_name}")
            ref = item.get("paragraph")
            if not isinstance(ref, str) or ref not in valid_paragraphs:
                problems.append(f"第 {position} 条配图候选项引用了不存在的段 id {ref!r}")
            kind = item.get("kind")
            if kind not in ILLUSTRATION_KINDS:
                problems.append(
                    f"第 {position} 条配图候选（{ref}）的 kind {kind!r} 不在 {'/'.join(ILLUSTRATION_KINDS)} 之内"
                )
            why = item.get("why")
            if not isinstance(why, str) or not why.strip():
                problems.append(f"第 {position} 条配图候选（{ref}）的 why 必须是非空字符串")
    return problems


# ------------------------------------------------------------------ 主流程 --
@dataclass
class AnalyzeResult:
    """抽取结果（落盘 payload + 供 CLI 打印的摘要）。"""

    source_id: str
    inventory: dict[str, Any]
    repairs: list[str] = field(default_factory=list)
    model: str | None = None
    prompt_version: str = CONCEPTS_PROMPT_VERSION
    raw_text: str = ""

    @property
    def concept_count(self) -> int:
        return len(self.inventory.get("concepts") or [])

    @property
    def illustration_count(self) -> int:
        return len(self.inventory.get("illustration_candidates") or [])


def _load_meta(data_dir: Path | str, source_id: str) -> dict[str, Any]:
    meta_path = doc_dir(data_dir, source_id) / "source.meta.json"
    if not meta_path.is_file():
        raise InputError(f"{meta_path} 不存在：请先运行 `ar ingest <文件> --id {source_id}`")
    meta = read_json(meta_path)
    if not isinstance(meta, dict):
        raise InputError(f"{meta_path} 根节点不是 JSON 对象")
    return meta


def build_payload(result: AnalyzeResult, meta: dict[str, Any], *, at: str | None = None, extra_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """拼出写盘的 ``concepts.json``（含回归基线用的元数据）。"""
    payload_meta: dict[str, Any] = {
        "source_id": result.source_id,
        "model": result.model,
        "prompt_version": result.prompt_version,
        "generated_at": at or now_iso(),
        "paragraph_count": len(meta.get("paragraphs") or []),
        "concept_count": result.concept_count,
        "illustration_count": result.illustration_count,
        "repairs": list(result.repairs),
    }
    if extra_meta:
        payload_meta.update(extra_meta)
    return {
        "concepts": result.inventory["concepts"],
        "illustration_candidates": result.inventory["illustration_candidates"],
        "meta": payload_meta,
    }


def run_analyze(
    source_id: str,
    data_dir: Path | str,
    client: Any,
    *,
    at: str | None = None,
) -> AnalyzeResult:
    """调模型抽概念清单，校验通过后写 ``data/<id>/concepts.json``。

    ``client`` 需要提供 ``chat(messages, *, max_tokens=None) -> ModelReply``
    （真实实现是 ``ModelClient``，测试用固定响应的假客户端）。
    """
    meta = _load_meta(data_dir, source_id)
    messages = build_concepts_messages(meta)
    reply = client.chat(messages, max_tokens=None)
    raw_text = getattr(reply, "text", "") or ""

    try:
        data = extract_json_object(raw_text)
    except InputError as exc:
        raw_path = doc_dir(data_dir, source_id) / "concepts.raw.txt"
        write_text(raw_path, raw_text)
        raise ModelUnavailable(
            f"概念清单抽取失败：{exc}。模型原始输出已存到 {raw_path} 供排查。"
            "已降级为「无画像」模式：本材料没有 concepts.json，可继续按原文阅读（不静默假装成功）"
        ) from exc

    inventory, repairs = normalize_inventory(data, meta)
    problems = validate_inventory(inventory, meta)
    if problems:
        raise ValidationFailed(problems, headline="概念清单未通过契约校验，未写入 concepts.json")

    result = AnalyzeResult(
        source_id=source_id,
        inventory=inventory,
        repairs=repairs,
        model=getattr(reply, "model", None),
        raw_text=raw_text,
    )
    extra: dict[str, Any] = {}
    usage = getattr(reply, "usage", None)
    if isinstance(usage, dict):
        extra["usage"] = usage
    degraded = getattr(reply, "degraded_params", ())
    if degraded:
        extra["degraded"] = list(degraded)
    payload = build_payload(result, meta, at=at, extra_meta=extra)
    write_json(doc_dir(data_dir, source_id) / "concepts.json", payload)
    return result


def check_inventory_file(data_dir: Path | str, source_id: str) -> list[str]:
    """``ar analyze <id> --check-only`` 的实现：只校验已有产物，不调模型。"""
    meta = _load_meta(data_dir, source_id)
    inventory_path = doc_dir(data_dir, source_id) / "concepts.json"
    inventory = read_json_if_exists(inventory_path)
    if inventory is None:
        return [f"{inventory_path} 不存在：请先运行 `ar analyze {source_id}`（需要模型）"]
    return validate_inventory(inventory, meta)
