"""讲解卡片（按需生成）：``POST /api/explain`` 的业务实现。

三种 kind（契约 §4）：``explain``（讲清楚）/ ``simpler``（更简单）/ ``detail``（更深入）。

两种作用域：默认讲**整段**（``scope: "paragraph"``）；请求里带 ``selection`` 时只讲**选中的片段**
（``scope: "selection"``，段落仍然提供上下文）。选中的片段按锚点口径规范化（去首尾空白 +
合并连续空白 + 全角空格 U+3000 转半角），上限 600 字符，超出截断并在响应里如实上报
``selection_truncated``——**降级必须可见，不许静默截断**。

**降级必须可见**（DESIGN §6）：模型未配置、调用失败、段落 id 不存在时，
一律返回 ``degraded: true`` + 一段人话说明，绝不留空白、绝不假装成功。

**缓存**（``ar.cache``）：只缓存成功结果；键与失效策略见 ``ar.cache`` 模块文档。

**token 账**：成功调用时把原始 ``usage`` 原样带进 ``ExplainOutcome.usage``，HTTP 层以追加字段
``usage`` 暴露（含前缀缓存命中 ``prompt_tokens_details.cached_tokens``）——讲解每次都要真金白银，
命中量必须看得见。``usage`` 里不含密钥、不含正文。

**重试账**：``ExplainOutcome.attempts``（追加字段）是这次调用的逐次尝试账，HTTP 层以追加字段
``attempts`` 在**成功与降级响应里都给**。讲解不落盘，这是它唯一能被看见的地方——重试花了几次
必须在页面上留痕（结构 = 产物里的 ``meta.attempts``，见 ``ar.llm.attempt_records``）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import tools
from .cache import ExplainCache, explain_cache_key
from .ingest import normalize_text
from .jsonio import read_json_if_exists
from .paths import DEFAULT_READER, doc_dir
from .profile import load_profile, profile_hint
from .prompts import (
    DEFAULT_SELECTION_FROM,
    EXPLAIN_KINDS,
    EXPLAIN_PROMPT_VERSION,
    SELECTION_MAX_CHARS,
    SELECTION_SOURCES,
)

__all__ = [
    "NOT_CONFIGURED_TEXT",
    "ExplainOutcome",
    "PreparedSelection",
    "prepare_selection",
    "find_paragraph",
    "aexplain",
    "explain",
]

NOT_CONFIGURED_TEXT = (
    "模型未配置，暂时不能生成讲解。设置环境变量 AR_API_KEY，"
    "或在 config.local.json 里填 api_key，再点一次即可（原文与改写稿不受影响）。"
)

SCOPE_PARAGRAPH = "paragraph"
SCOPE_SELECTION = "selection"


@dataclass(frozen=True)
class PreparedSelection:
    """规范化 + 截断后的选中片段；``text`` 为空串表示「这次没选片段」（按整段讲）。"""

    text: str = ""
    truncated: bool = False

    @property
    def scope(self) -> str:
        return SCOPE_SELECTION if self.text else SCOPE_PARAGRAPH


def prepare_selection(raw: Any, *, limit: int = SELECTION_MAX_CHARS) -> PreparedSelection:
    """把请求里的 ``selection`` 变成送模型的有效片段（**唯一的规范化 + 截断入口**）。

    - 非字符串 / 缺失 / 规范化后为空 → ``text=""``（行为与不带 selection 时完全一致）；
    - 规范化口径与锚点一致（``ingest.normalize_text``）；
    - 超过 ``limit`` 字符 → 截到 ``limit`` 并置 ``truncated=True``（调用方负责把它报出去）。
    """
    if not isinstance(raw, str):
        return PreparedSelection()
    text = normalize_text(raw)
    if len(text) <= limit:
        return PreparedSelection(text=text)
    # 截断后再规范化一次：切点可能落在空白上，去掉尾巴免得提示词里多一个空格
    return PreparedSelection(text=normalize_text(text[:limit]), truncated=True)


@dataclass
class ExplainOutcome:
    """一次讲解请求的结果；``degraded=True`` 时 ``text`` 是说明文本，不是模型产出。

    ``usage`` 是这次调用的原始 usage（``{}`` = 没调模型 / 缓存命中 / 网关没回），
    里面含 ``prompt_tokens_details.cached_tokens`` 这类前缀缓存字段——原样带给 HTTP 层，
    不在这里改名或裁剪（口径见 ``ar.model.cached_tokens_of``）。
    """

    text: str
    degraded: bool
    reason: str | None = None
    model: str | None = None
    prompt_version: str = EXPLAIN_PROMPT_VERSION
    scope: str = SCOPE_PARAGRAPH
    cached: bool = False
    selection_truncated: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    #: 追加字段：这次调用的逐次尝试账（结构 = 产物里的 ``meta.attempts``）。
    #: 没发请求时是空数组（未配置 / 缓存命中 / 材料或段落不存在），所以 ``[]`` 与「试了但都失败」
    #: 分得开；HTTP 层把它作为 ``/api/explain`` 的追加响应字段 ``attempts`` 透出去。
    attempts: list[dict[str, Any]] = field(default_factory=list)


def find_paragraph(meta: dict[str, Any], paragraph_id: str) -> dict[str, Any] | None:
    for item in meta.get("paragraphs") or []:
        if isinstance(item, dict) and item.get("id") == paragraph_id:
            return item
    return None


def _neighbor_params(meta: dict[str, Any], paragraph: dict[str, Any], span: int = 1) -> dict[str, str]:
    """相邻段的正文与段 id（提示词里邻段行是 ``[段id] 正文``）。

    缺哪一侧就不给哪一侧的键——与旧实现「只把确实存在的邻段拼进提示词」逐字一致；
    文本由工具层的模板截到 200 字符（与迁移前同一处截断）。
    """
    paragraphs = [p for p in (meta.get("paragraphs") or []) if isinstance(p, dict)]
    try:
        index = paragraphs.index(paragraph)
    except ValueError:
        return {}
    params: dict[str, str] = {}
    for key, offset in (("prev", -span), ("next", span)):
        position = index + offset
        if 0 <= position < len(paragraphs):
            neighbor = paragraphs[position]
            params[key] = str(neighbor.get("text") or "")
            params[f"{key}_id"] = str(neighbor.get("id"))
    return params


async def aexplain(
    source_id: str,
    paragraph_id: str,
    kind: str,
    *,
    data_dir: Path | str,
    profiles_dir: Path | str,
    reader: str = DEFAULT_READER,
    client: Any,
    selection: Any = None,
    selection_from: str = DEFAULT_SELECTION_FROM,
    cache: ExplainCache | None = None,
) -> ExplainOutcome:
    """生成一段讲解；任何失败都转为可见降级（不抛异常给 HTTP 层）。

    ``selection`` 非空时只讲这一段选中的文字（``scope: "selection"``）；``selection_from``
    （``rewritten`` / ``source``）只影响提示词里对片段来源的标注，非法取值退回默认值——
    HTTP 层已经先做过一次严格校验（400）。
    ``cache`` 非空时优先查缓存（只缓存成功结果）。

    模型调用经工具层 ``explain_paragraph``（拼提示词 / 重试 / 记账都在那边），并且**丢到线程里**跑：
    工具的 ``execute`` 是同步入口（内部 ``asyncio.run``），在 HTTP 的事件循环里直接调会嵌套报错。
    """
    if kind not in EXPLAIN_KINDS:
        kind = "explain"
    if selection_from not in SELECTION_SOURCES:
        selection_from = DEFAULT_SELECTION_FROM

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

    prepared = prepare_selection(selection)
    key = (
        explain_cache_key(
            source_id=source_id,
            paragraph_id=paragraph_id,
            kind=kind,
            selection=prepared.text,
            profiles_dir=profiles_dir,
            reader=reader,
        )
        if cache is not None
        else None
    )
    if cache is not None and key is not None:
        hit = cache.get(key)
        if hit is not None:
            # 命中即返回：不读画像、不发请求（缓存里只有成功结果，所以 degraded 恒为 False）
            return ExplainOutcome(
                text=str(hit.get("text") or ""),
                degraded=False,
                model=hit.get("model"),
                prompt_version=str(hit.get("prompt_version") or EXPLAIN_PROMPT_VERSION),
                scope=prepared.scope,
                cached=True,
                selection_truncated=prepared.truncated,
            )

    profile, _notes = load_profile(profiles_dir, reader)
    params: dict[str, Any] = {
        "paragraph": paragraph_id,
        "kind": kind,
        "text": str(paragraph.get("text") or ""),
        "selection": prepared.text,
        "selection_from": selection_from,
        # 空画像时 profile_hint 是 None；工具入参按契约要字符串，空串与 None 在模板里同一条分支
        "profile_hint": str(profile_hint(profile) or ""),
        **_neighbor_params(meta, paragraph),
    }

    if client is None or not getattr(client, "configured", False):
        return ExplainOutcome(
            text=NOT_CONFIGURED_TEXT,
            degraded=True,
            reason="model_not_configured",
            scope=prepared.scope,
            selection_truncated=prepared.truncated,
        )
    # 工具层 ``execute`` 是**同步**的（内部走 ``ar.llm.call`` = ``asyncio.run``）：
    # 本函数跑在 HTTP 的事件循环里，直接调用会让 ``asyncio.run`` 嵌套报错，必须丢到线程里跑。
    run = await asyncio.to_thread(tools.run_tool, "explain_paragraph", params, client=client)
    if not run.ok:
        message = run.failure_message()
        if run.model_replied:
            # HTTP 通了、正文却是空的（网关把空回复当成功回）：与迁移前同一条降级
            return ExplainOutcome(
                text="模型返回了空内容，未生成讲解。",
                degraded=True,
                reason="empty_reply",
                scope=prepared.scope,
                selection_truncated=prepared.truncated,
                attempts=run.attempt_records,
            )
        return ExplainOutcome(
            text=f"未能生成讲解：{message}",
            degraded=True,
            reason="model_error",
            scope=prepared.scope,
            selection_truncated=prepared.truncated,
            attempts=run.attempt_records,
        )
    data = run.data if isinstance(run.data, dict) else {}
    text = str(data.get("text") or "")
    if not text:
        return ExplainOutcome(
            text="模型返回了空内容，未生成讲解。",
            degraded=True,
            reason="empty_reply",
            scope=prepared.scope,
            selection_truncated=prepared.truncated,
            attempts=run.attempt_records,
        )
    outcome = ExplainOutcome(
        text=text,
        degraded=False,
        model=run.model,
        scope=prepared.scope,
        selection_truncated=prepared.truncated,
        usage=dict(run.usage or {}),
        attempts=run.attempt_records,
    )
    if cache is not None and key is not None:
        # 只缓存成功结果：上面每条降级路径都在 return 里走掉了，进不到这里来
        cache.put(
            key,
            {"text": outcome.text, "model": outcome.model, "prompt_version": outcome.prompt_version},
        )
    return outcome


def explain(source_id: str, paragraph_id: str, kind: str = "explain", **kwargs: Any) -> ExplainOutcome:
    """同步包装（CLI / 测试用）。"""
    return asyncio.run(aexplain(source_id, paragraph_id, kind, **kwargs))
