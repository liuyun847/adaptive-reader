"""一次性整篇改写基线（``oneshot-v1``，只用于对照，不是主路径）。

为什么单独一个模块：它是**一次调用**吃全文、吐全文的实验分支——没有逐段决策、没有
``fact_declarations` 申报通道、没有校验重试；把它塞进 ``ar rewrite`` 的产物契约
（decisions / changes）只会把两条路的账混在一起。所以基线只写自己的证据文件：

```
data/<source_id>/_eval/oneshot.json   机器可读的一次运行记录（usage / 耗时 / 事实锚比对）
data/<source_id>/_eval/oneshot.md     模型输出的整篇（按段对齐后的 markdown）
data/<source_id>/_eval/oneshot.raw.txt  只在 JSON 解析失败时出现（排查用）
```

**不碰** ``decisions.json`` / ``rewritten.md`` / ``changes.json`` 三份主产物。
与主路径的可比性：同一读者画像、同一读者背景、同一事实锚口径、同一概念清单、同一全文；
区别只有「一次调用出全篇」这一点。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analyze import extract_json_object
from .errors import InputError
from .fidelity import compare_blocks_detailed, normalize_fact_anchor_policy
from .jsonio import now_iso, read_json, read_json_if_exists, write_json, write_text
from .llm import LlmRequest, RetryPolicy, attempt_records, call, retry_account
from .paths import doc_dir
from .profile import load_profile, resolve_fact_anchor_policy
from .prompts import ONESHOT_PROMPT_VERSION, build_oneshot_messages
from .rewrite import _format_concept_block, _format_profile_block
from .survey import render_sketch

__all__ = ["EVAL_DIRNAME", "ONESHOT_JSON", "ONESHOT_MD", "BaselineOutcome", "run_oneshot"]

#: 基线证据目录（挂在 ``data/<source_id>/`` 下，与 ``_verify/`` 同一类「人工验收 / 对照材料」）
EVAL_DIRNAME = "_eval"
ONESHOT_JSON = "oneshot.json"
ONESHOT_MD = "oneshot.md"


@dataclass
class BaselineOutcome:
    """一次基线运行的结果（成功 / 失败都返回，不抛）。"""

    source_id: str
    reader: str
    ok: bool = True
    reason: str = ""
    model: str | None = None
    prompt_version: str = ONESHOT_PROMPT_VERSION
    usage: dict[str, Any] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    retry_account: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    elapsed_ms: int = 0
    paragraph_count: int = 0
    returned_count: int = 0
    changed_count: int = 0
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    counts: dict[str, Any] = field(default_factory=dict)
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    json_path: Path | None = None
    markdown_path: Path | None = None


def _paragraphs_of(meta: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in (meta.get("paragraphs") or []) if isinstance(p, dict) and p.get("id")]


def _parse_output(raw_text: str, paragraphs: list[dict[str, Any]]) -> tuple[dict[str, str], list[str], list[str]]:
    """模型输出 → ``{段 id: 正文}`` + 缺段 + 多余段（读不通抛 ``InputError``）。"""
    data = extract_json_object(raw_text)
    items = data.get("paragraphs") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise InputError("模型输出的 JSON 里没有 paragraphs 数组")
    expected = [str(p.get("id")) for p in paragraphs]
    mapping: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("paragraph") or item.get("id") or "").strip()
        text = item.get("text")
        if pid and isinstance(text, str) and pid not in mapping:
            mapping[pid] = text
    missing = [pid for pid in expected if pid not in mapping]
    extra = [pid for pid in mapping if pid not in expected]
    return mapping, missing, extra


def run_oneshot(
    source_id: str,
    data_dir: Path | str,
    profiles_dir: Path | str,
    reader: str,
    client: Any,
    *,
    max_tokens: int | None = None,
    at: str | None = None,
) -> BaselineOutcome:
    """跑一次「整篇一次调用」基线，写 ``_eval/oneshot.*``，返回结果（失败不抛）。"""
    target = doc_dir(data_dir, source_id)
    meta = read_json(target / "source.meta.json")
    if not isinstance(meta, dict):
        raise InputError(f"source.meta.json 根节点不是对象：{target / 'source.meta.json'}")
    paragraphs = _paragraphs_of(meta)
    if not paragraphs:
        raise InputError("source.meta.json 里没有任何段落，无法做一次性基线")

    inventory = read_json_if_exists(target / "concepts.json")
    inventory = inventory if isinstance(inventory, dict) else None
    profile, profile_notes = load_profile(profiles_dir, reader)
    policy = resolve_fact_anchor_policy(profile)
    background_block = render_sketch(profile.get("background"))
    messages = build_oneshot_messages(
        source_id,
        paragraphs,
        profile_block=_format_profile_block(reader, profile, list(profile.get("overrides") or [])),
        concept_block=_format_concept_block(source_id, inventory),
        background_block=background_block,
        fact_anchor_policy=policy.policy,
        total=len(paragraphs),
    )

    outcome = BaselineOutcome(source_id=source_id, reader=reader, paragraph_count=len(paragraphs))
    outcome.degraded.extend(profile_notes)
    if policy.warning:
        outcome.degraded.append(policy.warning)

    retry_policy = client.retry_policy() if callable(getattr(client, "retry_policy", None)) else RetryPolicy()
    request = LlmRequest(
        tool="oneshot_rewrite",
        messages=tuple(dict(message) for message in messages),
        temperature=None,
        max_tokens=max_tokens,
        prompt_version=ONESHOT_PROMPT_VERSION,
    )
    started = time.monotonic()
    result = call(request, client=client, policy=retry_policy)
    outcome.elapsed_ms = int(round((time.monotonic() - started) * 1000))
    outcome.model = result.model
    outcome.finish_reason = result.finish_reason
    outcome.usage = dict(result.usage or {})
    outcome.attempts = attempt_records("oneshot_rewrite", result.attempts)
    outcome.retry_account = retry_account("oneshot_rewrite", result.attempts)
    outcome.degraded.extend(result.degraded_params)
    if not result.ok:
        outcome.ok = False
        failure = result.failure
        outcome.reason = failure.message if failure is not None else "模型调用失败（没有给出原因）"
        # 失败也留证（usage / 尝试账 / 原因）：对照实验里「这一次花了多少、错在哪」同样要看得见
        eval_dir = target / EVAL_DIRNAME
        outcome.json_path = write_json(
            eval_dir / ONESHOT_JSON, _payload(outcome, source_id, reader, policy.policy, at)
        )
        return outcome

    raw_text = result.text or ""
    try:
        mapping, missing, extra = _parse_output(raw_text, paragraphs)
    except InputError as exc:
        outcome.ok = False
        outcome.reason = f"模型输出读不通：{exc}"
        eval_dir = target / EVAL_DIRNAME
        write_text(eval_dir / "oneshot.raw.txt", raw_text)
        outcome.json_path = write_json(
            eval_dir / ONESHOT_JSON,
            _payload(outcome, source_id, reader, policy.policy, at, raw_text=raw_text),
        )
        return outcome

    blocks: list[str] = []
    changed = 0
    for paragraph in paragraphs:
        pid = str(paragraph.get("id"))
        source_text = str(paragraph.get("text") or "")
        text = mapping.get(pid, source_text)
        blocks.append(text)
        if text != source_text:
            changed += 1
    outcome.returned_count = len(mapping)
    outcome.changed_count = changed
    outcome.missing = missing
    outcome.extra = extra
    outcome.problems.extend(f"{pid}：模型输出里没有这一段（基线稿里用原文补位）" for pid in missing)
    outcome.problems.extend(f"{pid}：模型输出了原文里没有的段 id（已忽略）" for pid in extra)
    comparison = compare_blocks_detailed(
        paragraphs, blocks, policy=normalize_fact_anchor_policy(policy.policy)
    )
    outcome.counts = dict(comparison.counts)
    outcome.mismatches = [dict(item) for item in comparison.mismatches]
    outcome.problems.extend(comparison.problems)

    markdown = "\n\n".join(blocks) + "\n"
    eval_dir = target / EVAL_DIRNAME
    outcome.markdown_path = write_text(eval_dir / ONESHOT_MD, markdown)
    outcome.json_path = write_json(
        eval_dir / ONESHOT_JSON, _payload(outcome, source_id, reader, policy.policy, at, markdown=markdown)
    )
    return outcome


def _payload(
    outcome: BaselineOutcome,
    source_id: str,
    reader: str,
    policy: str,
    at: str | None,
    *,
    markdown: str = "",
    raw_text: str = "",
) -> dict[str, Any]:
    """``oneshot.json`` 的形状（机器可读、与主产物 meta 同口径的字段名）。"""
    return {
        "kind": "oneshot-baseline",
        "source_id": source_id,
        "reader": reader,
        "ok": outcome.ok,
        "reason": outcome.reason,
        "model": outcome.model,
        "prompt_version": outcome.prompt_version,
        "generated_at": at or now_iso(),
        "elapsed_ms": outcome.elapsed_ms,
        "finish_reason": outcome.finish_reason,
        "fact_anchor_policy": policy,
        "usage": outcome.usage,
        "attempts": outcome.attempts,
        "retry_account": outcome.retry_account,
        "paragraph_count": outcome.paragraph_count,
        "returned_count": outcome.returned_count,
        "changed_count": outcome.changed_count,
        "missing": outcome.missing,
        "extra": outcome.extra,
        "counts": outcome.counts,
        "mismatches": outcome.mismatches,
        "problems": outcome.problems,
        "degraded": outcome.degraded,
        "text_chars": len(markdown),
        "raw_text": raw_text[:4000] if raw_text else "",
    }
