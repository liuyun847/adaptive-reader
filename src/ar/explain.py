"""讲解卡片（按需生成）：``POST /api/explain`` 的业务实现。

三种 kind（契约 §4）：``explain``（讲清楚）/ ``simpler``（更简单）/ ``detail``（更深入）。

**降级必须可见**（DESIGN §6）：模型未配置、调用失败、段落 id 不存在时，
一律返回 ``degraded: true`` + 一段人话说明，绝不留空白、绝不假装成功。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ModelUnavailable
from .jsonio import read_json_if_exists
from .paths import DEFAULT_READER, doc_dir
from .profile import load_profile, profile_hint
from .prompts import EXPLAIN_KINDS, EXPLAIN_PROMPT_VERSION, build_explain_messages

__all__ = ["ExplainOutcome", "find_paragraph", "aexplain", "explain"]

NOT_CONFIGURED_TEXT = (
    "模型未配置，暂时不能生成讲解。设置环境变量 AR_API_KEY，"
    "或在 config.local.json 里填 api_key，再点一次即可（原文与改写稿不受影响）。"
)


@dataclass
class ExplainOutcome:
    """一次讲解请求的结果；``degraded=True`` 时 ``text`` 是说明文本，不是模型产出。"""

    text: str
    degraded: bool
    reason: str | None = None
    model: str | None = None
    prompt_version: str = EXPLAIN_PROMPT_VERSION


def find_paragraph(meta: dict[str, Any], paragraph_id: str) -> dict[str, Any] | None:
    for item in meta.get("paragraphs") or []:
        if isinstance(item, dict) and item.get("id") == paragraph_id:
            return item
    return None


def _neighbors(meta: dict[str, Any], paragraph: dict[str, Any], span: int = 1) -> list[tuple[str, str]]:
    paragraphs = [p for p in (meta.get("paragraphs") or []) if isinstance(p, dict)]
    try:
        index = paragraphs.index(paragraph)
    except ValueError:
        return []
    picked: list[tuple[str, str]] = []
    for offset in range(-span, span + 1):
        if offset == 0:
            continue
        position = index + offset
        if 0 <= position < len(paragraphs):
            picked.append((str(paragraphs[position].get("id")), str(paragraphs[position].get("text") or "")))
    return picked


async def aexplain(
    source_id: str,
    paragraph_id: str,
    kind: str,
    *,
    data_dir: Path | str,
    profiles_dir: Path | str,
    reader: str = DEFAULT_READER,
    client: Any,
) -> ExplainOutcome:
    """生成一段讲解；任何失败都转为可见降级（不抛异常给 HTTP 层）。"""
    if kind not in EXPLAIN_KINDS:
        kind = "explain"

    meta = read_json_if_exists(doc_dir(data_dir, source_id) / "source.meta.json")
    if not isinstance(meta, dict):
        return ExplainOutcome(
            text=f"找不到材料 {source_id} 的 source.meta.json：请先运行 `ar ingest <文件> --id {source_id}`。",
            degraded=True,
            reason="source_not_found",
        )

    paragraph = find_paragraph(meta, paragraph_id)
    if paragraph is None:
        known = ", ".join(str(p.get("id")) for p in (meta.get("paragraphs") or [])[:5]) or "（无）"
        return ExplainOutcome(
            text=(
                f"材料 {source_id} 里没有段落 {paragraph_id}：材料可能已被重新摄取（锚点变了），"
                f"请刷新页面重新校准。现有段落形如 {known}…"
            ),
            degraded=True,
            reason="paragraph_not_found",
        )

    profile, _notes = load_profile(profiles_dir, reader)
    messages = build_explain_messages(
        kind=kind,
        paragraph_id=paragraph_id,
        paragraph_text=str(paragraph.get("text") or ""),
        neighbors=_neighbors(meta, paragraph),
        profile_hint=profile_hint(profile),
    )

    if client is None or not getattr(client, "configured", False):
        return ExplainOutcome(text=NOT_CONFIGURED_TEXT, degraded=True, reason="model_not_configured")
    try:
        reply = await client.achat(messages, max_tokens=None)
    except ModelUnavailable as exc:
        return ExplainOutcome(
            text=f"模型调用失败，未能生成讲解：{exc}", degraded=True, reason="model_error"
        )
    text = (getattr(reply, "text", "") or "").strip()
    if not text:
        return ExplainOutcome(text="模型返回了空内容，未生成讲解。", degraded=True, reason="empty_reply")
    return ExplainOutcome(text=text, degraded=False, model=getattr(reply, "model", None))


def explain(source_id: str, paragraph_id: str, kind: str = "explain", **kwargs: Any) -> ExplainOutcome:
    """同步包装（CLI / 测试用）。"""
    return asyncio.run(aexplain(source_id, paragraph_id, kind, **kwargs))
