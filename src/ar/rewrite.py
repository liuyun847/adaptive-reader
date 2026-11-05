"""逐段决策 + 改写（M3，契约 §2.4 / §4）+ 局部重跑与逐段合并（PLAN-actions-feedback §6）。

产物（DESIGN §4.4，字段逐字不改名）::

    data/<id>/decisions.json   [{"paragraph","action","why{"profile","note"},"confidence"}] + meta
    data/<id>/rewritten.md     改写稿（段序与源一致、段间空行分隔、段首不写 id；**图不进这里**）
    data/<id>/changes.json     {"changes":[…含 facts / figure / figure_error / deferred /
                                fact_declarations…],"fidelity":{"checked","mismatches",…},"meta"}

八条纪律：
1. **四个动作都已实现**（2026-10-13 起）：``expand`` / ``compress`` 用模型给的改写稿替换正文，
   ``keep`` / ``illustrate`` 用原文。``deferred`` 机制**保留但不再有对象**——它只表示
   「枚举内、本期未实现的动作」，现在四个动作全实现了，所以它恒为 false（字段与文案都不删）。
   枚举**外**的动作（含模型没给 ``action``）同样按 ``keep`` 处理，但**不标 deferred**
   ——文案与产物必须一致。
   ``illustrate`` 的图定义放在 ``changes[i].figure``（**不进** ``rewritten.md``：该段正文与原文
   逐字节相同，图由前端就地渲染），生成 / 清洗失败则退回 ``keep`` + ``figure_error`` + 可见降级。
2. **省 token（默认）**：25 段不逐段发请求，按 ``max_chars_per_batch`` 合并成 4–6 次请求；
   但 ``decisions`` 仍然**每段一条**。缺哪段的决策就补一条可见降级的 ``keep``，绝不假装成功。
   ``rewrite-v7`` 起另有**逐段循环**（``per_paragraph=True`` / CLI ``--per-paragraph`）：
   一段一请求、共用同一份全文前缀，校验不过时带反馈重试；请求数 ×25，账单靠缓存命中摊薄。
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
   的「前缀缓存纪律」），且**先跑完第 1 批预热前缀、再并发其余批次**
   （``_plan_batches_warmup``）——6 批同时首发谁都吃不到刚建好的缓存。
   命中量逐批记在 ``meta.batch_usage[].cached_tokens``、合计记在 ``meta.usage.cached_tokens``
   （网关没上报该字段时为 ``null``，不编造成 0）。
   ``rewrite-v7`` 起**全文**也进固定上下文（``{full_text_block}``）：同一材料的 6 批 / 25 段
   共享同一份「通用块 + 材料块 + 全文」前缀（capital-v1-ch01 实测 9889 字符 / 5364 token），
   逐段模式沿用同一套提示词与同一份全文；校验反馈只追加在 user 末尾，不破坏前缀。
   **所有批次（含第 1 批预热批）都在工作线程里跑**：调用链最终落到 ``asyncio.run``
   （``ar.llm.call``），在已有运行中事件循环的调用方里就地调用会抛
   ``RuntimeError: asyncio.run() cannot be called from a running event loop``。
   线程里没有运行中的事件循环，于是 async 调用方与 CLI 走同一条路（与 ``ar.explain`` /
   ``ar.survey`` 在 async 路由里 ``await asyncio.to_thread(tools.run_tool, …)`` 同一条纪律）。
8. **读者背景算画像**（PLAN-background-survey §2 拍板 2 / §6.4）：``profiles/<reader>/profile.yaml``
   的顶层键 ``background``（读前背景问卷）非空即 ``profile_used: true``，``profile_note`` 把
   「只有概念 / 只有背景 / 两者都有」三种情况分开写；素描（``ar.survey.render_sketch``，逐字稳定、
   ≤400 字符）进提示词的「读者背景」固定块，产物 meta 追加 ``background_used`` /
   ``background_fingerprint``；每段的背景依据只写在 ``why.note`` 里，``why.profile`` 的元素格式不变。
   背景一变 ``background_fingerprint`` 就变——旧产物与新产物不可直接比（契约 §14）。
9. **局部重跑**（PLAN-actions-feedback §6）：``run_rewrite(..., targets=…)`` 只对目标段发请求，
   每批与目标段取交集、空交集**不调模型**；结果由 :func:`merge_artifacts` **逐段 patch-in-place**
   合进既有三份产物（未重跑段逐字节保留、``fidelity`` 重置为未校验、账记本次、
   ``meta.rerun`` / ``meta.rerun_history[]`` 留痕、``model_errors`` / ``unplanned_paragraphs`` /
   ``rewrite_incomplete`` 按合并后的真实状态重算）。前置校验不过（产物缺一份、条数对不上）
   一律 ``InputError``（退出码 2）：**绝不做半合并**。
10. **逐段合并器只有一个实现**（:func:`merge_artifacts`）：CLI 局部重跑与在线压缩
   （``POST /api/compress`` → :func:`acompress_paragraph`）走同一条路，合并口径不会分叉。
11. **事实锚「可申报 + 可见」**（2026-10-14，PLAN-actions-feedback §14.1）：模型在每段的
   ``fact_declarations`` 里逐条申报「本段没有原样保留哪几项事实锚」（只进 ``changes.json``）；
   放不放行由**读者画像里那条偏好**（``pref_fact_anchor``：``keep_all`` 严 / ``allow_declared_drops`` 松）
   在 ``ar check`` 时决定（:func:`check_fidelity` 用 ``ar.fidelity.compare_blocks_detailed`），
   口径与放行条目分别落 ``meta.fact_anchor_policy`` / ``fidelity.fact_anchor_policy`` 与
   ``fidelity.waived[]``。**没申报的缺失仍然算违规（退出码 4）**，多出来的事实锚与顺序调换
   任何口径下都不放行——这条网只松「举例数据可以丢」，不松「事实漂移」。
   **在线压缩（``/api/compress``）走同一套口径**（2026-10-14 收尾）：``compress-v3`` 提示词让模型
   逐条申报本次压缩放弃了哪几项锚点，写盘前用**同一个** ``compare_blocks_detailed`` 按读者画像
   当前口径判——少了且已申报 ⇒ 松口径放行（条目落 ``changes[i].fact_declarations``，重跑
   ``ar check`` 后进 ``fidelity.waived``）；没申报的缺失、多出来的锚点、顺序调换、申报与实测
   对不上**照旧拒绝写入**。两条路径的口径因此不会分叉。
   **申报与实测对不上**（锚点一个没少却申报放弃了它）**不改放行结论与退出码**，但逐条落
   ``fidelity.over_declared[]`` 并打印到 CLI——绝不静默。
12. **``why.profile``：模型给的引用逐条保留，漏给不静默**（2026-10-14 复核修复，
   ``rewrite-v6``）：元素形态是 ``c03:unknown``（概念 id + 读者自评状态，契约 §4.4），
   语义与「读者有没有画像」是两件事——背景非空即算「有画像」（契约 §6.4），
   但背景依据只写在 ``why.note`` 里（拍板 3），所以「只有背景、没有概念自评」的读者，
   ``why.profile`` 是空数组**且这是合法结果**。三处口径一致：
   提示词的「读者画像」块只说「读者没有提供任何概念自评」（**不许说「画像为空」**，
   旧措辞既与背景自相矛盾、又直接指示模型写空数组）、规则 11 交代 ``why.profile`` 该写什么、
   ``ar.tools`` 的 ``_DECISION`` 在字段语义里写明「有自评时必须逐条保留模型给的引用」。
   产出链路的兜底（:data:`RewriteOutcome.profile_refs_filled`）：模型**漏键 / 给错形状 /
   该给引用却给空数组**时，逐条记进 ``meta.degraded``（可见：形状问题由 ``_normalize_plan_items``
   的第二个返回值经 ``_parse_batch`` → 工具信封 ``degraded`` 上传，漏给的引用由 ``run_rewrite``
   在拼装决策时判），并**只照读者自己的自评**兜底补齐 ``why.profile`` + 在 ``why.note`` 里留下
   补齐的出处；读者本来就没有概念自评时**不误报、不补齐**。CLI 另打一行 ``why.profile`` 的账
   （几段有引用 / 几段空 / 哪几段是补齐的）。
13. **逐段循环 / 逐段对话 + 校验回灌**（``rewrite-v7``，opt-in）：``per_paragraph=True`` 时一段一请求
   （``conversation=True`` 则改成同一份 messages 里累积原文与已改内容、强制串行），
   先做**硬校验**（决策存在、动作在四类内、压缩更短 / 展开更长、句末标点、事实锚按读者口径比对），
   不过就把具体差异写进 user 末尾重试（``validation_retries`` 次上限）；
   次数用尽仍不过 ⇒ 这一段**不写模型稿**、按保持原文 + ``unplanned``（退出码 6）。
   并发 ``concurrency<=1`` 串行、否则并发上限；产物结构与分批模式一致
   （``batch_usage`` 每段一条、``meta.planning_mode`` 标明调度方式）。
14. **两遍式改写**（``--plan-first``，``plan-v2``，opt-in）：**第一遍一次调用只打动作标**
    （每段一个 expand / compress / keep / illustrate，**不写任何正文**），这份计划作为**固定上下文**
    的一部分（``{plan_section}``，排在全文块之后、本次段号之前）进第二遍，第二遍仍按原模式
    （分批 / 逐段 / 逐段对话）改写。不写正文时模型没有「别把原文改坏」的压力，动作决定更敢下手。
    产物 ``meta`` **追加** ``plan_first` / ``plan` / ``plan_missing` / ``plan_usage` /
    ``plan_request_count` / ``plan_attempts` / ``plan_retry_account` / ``plan_prompt_version`；
    某段的计划动作与最终动作不同时，``changes[i].plan_action`（**为真才出现**）留下计划里那一个。
    **退出码语义不变**：``plan_missing``（第一遍漏了哪几段）**不算** ``unplanned_paragraphs``，
    计划与执行不一致同样不触发退出码 6——它们是「计划不完整 / 改主意了」的可见记录，不是「没改写」。
    ``plan-v2``（2026-10-31）把第一遍的提示词改成「改写那份的公共头 + 打标尾巴」，两遍共享
    9889 字符（5364 token）前缀：第一遍建好的缓存被第二遍第 1 批直接命中（v1 时公共前缀 0 字符）。
    第二遍的提示词与 ``rewrite-v7`` **一个字都没变**。
    越界动作（例如 ``frobnicate``）按 ``keep`` 处理并记一条可见降级（不静默）。
    ``--only`` / ``--retry-failed`` / ``--from-signals`` 与 ``--plan-first`` 同时给时，**旧产物里已有
    计划**（且读者 / 背景指纹 / 事实锚口径对得上）就复用它、不重新打标（``meta.plan_reused``）；
    旧产物没有计划或口径对不上 ⇒ 重新全篇打标（多一次请求）。**不带** ``--plan-first`` 的局部重跑
    也按旧计划执行那几段（一致性优先）；旧计划里没有某段的条目 / 口径对不上时代码会把它记进
    ``meta.plan_not_applied`` 并在命令行点名——**不许静默降级成单遍改写**。
15. **逐段对话的上下文瘦身**（``--conversation-digest``，opt-in）：逐段对话本来把上一轮的**整段
    JSON** 原样塞回 transcript（模型容易照着上一轮的外壳抄、也白烧 prompt token）；开了这个开关就
    改塞一条紧凑摘要——每行形如 ``p001 keep`` / ``p002 expand：<改写后正文>``，``why`` /
    ``confidence`` 这些外壳一个字符都不进上下文。**原始 JSON 仍用于本轮校验解析**（摘要只是
    「给模型看的上下文」，不参与判定）。开关缺省关，关时 transcript 与现状**逐字一致**；
    开了才在 ``meta`` 追加 ``conversation_digest: true``（**只在开时为 true**）。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence

from . import tools
from .analyze import format_illustration_block
from .cache import ExplainCache, compress_cache_key
from .errors import InputError, NotImplementedYet
from .fidelity import (
    DEFAULT_FACT_ANCHOR_POLICY,
    FACT_ANCHOR_POLICIES,
    compare_blocks_detailed,
    facts_of,
    normalize_fact_anchor_policy,
    parse_declarations,
    split_rewritten_blocks,
)
from .ingest import text_hash
from .llm import LlmRequest, RetryPolicy, attempt_records, retry_account
from .llm import call as llm_call
from .jsonio import now_iso, read_json, read_json_if_exists, write_json, write_text
from .model import cached_tokens_of
from .paths import doc_dir
from .prompts import (
    COMPRESS_PROMPT_VERSION,
    PLAN_PROMPT_VERSION,
    REWRITE_PROMPT_VERSION,
    build_rewrite_messages,
    format_paragraph_block,
)
from .survey import has_background, has_concept_signals, has_fixed_answers, render_sketch, sketch_fingerprint

__all__ = [
    "ACTIONS",
    "IMPLEMENTED_ACTIONS",
    "UNIMPLEMENTED_ACTIONS",
    "TEXT_ACTIONS",
    "FIGURE_KINDS",
    "MODES",
    "DEFAULT_MODE",
    "STRICT_NOT_IMPLEMENTED",
    "DEFAULT_MAX_CHARS_PER_BATCH",
    "RERUN_REASONS",
    "COMPRESS_SCOPE",
    "COMPRESS_REASONS",
    "COMPRESS_HINT",
    "COMPRESS_NO_ANCHOR_NOTE",
    "DEFAULT_COMPRESS_REASON",
    "Decision",
    "PlanEntry",
    "PlanItem",
    "RewriteOutcome",
    "BatchPlan",
    "CheckOutcome",
    "RerunTargets",
    "ParagraphUpdate",
    "MergeBase",
    "MergeOutcome",
    "CompressOutcome",
    "actions_label",
    "resolve_rerun_targets",
    "merge_artifacts",
    "planDecisions",
    "plan_actions",
    "rewrite",
    "run_rewrite",
    "acompress_paragraph",
    "check_fidelity",
    "fidelity_problems",
    "incomplete_reasons",
]

#: 契约 §4 的四个动作；**四个都已实现**（2026-10-13 起）
ACTIONS = ("expand", "compress", "keep", "illustrate")
IMPLEMENTED_ACTIONS = ("expand", "compress", "keep", "illustrate")
#: 会用模型给的改写稿替换正文的动作（``keep`` / ``illustrate`` 一律用原文；
#: illustrate 的图在 ``changes[i].figure`` 里，**不进** ``rewritten.md``）
TEXT_ACTIONS = ("expand", "compress")
#: 枚举里存在、但**没有实现**的动作：遇到按 ``keep`` 处理并标 ``deferred``。
#: 四个动作都实现之后这里是空的——``deferred`` 机制与文案都保留，只是不再有对象。
UNIMPLEMENTED_ACTIONS: tuple[str, ...] = ()
MODES = ("free", "strict")
DEFAULT_MODE = "free"

#: 合并请求的粒度（省 token 与「单次请求别太大」之间的折中）：25 段 → 4 次请求
DEFAULT_MAX_CHARS_PER_BATCH = 1100

#: 逐段循环（``--per-paragraph``）的并发上限（与分批模式的 4 一致；串行 = 1）
DEFAULT_CONCURRENCY = 4
#: 逐段循环里「校验没通过 → 带反馈重试」的次数上限（0 = 只试一次）
DEFAULT_VALIDATION_RETRIES = 2
#: 逐段对话（``--conversation``）的第一段提示：把「每轮只回一段」的协议说在前面
CONVERSATION_START_NOTE = (
    "本轮起是**逐段对话**：每轮只处理我指定的一段，只输出这一段的 decisions（数组里一个元素）；"
    "已经处理过的段不要重复输出——后面每轮我会把下一段的原文给你。"
    "**每一段的动作都要独立判断**：前面的决定只作参考，不要因为上一段选了保持就跟着保持；"
    "这一段如果确实该展开 / 压缩 / 配图，就照规则改。"
)
#: 改写正文允许的句末标点（截断判据；``_finalize_items`` 与逐段校验共用一份）
SENTENCE_ENDINGS = ("。", "！", "？", "”", "」", "）", "〕", "》", "…")

#: ``figure.kind`` 只收这两类（契约 §4.1）：mermaid 源码 / 自包含 SVG 源码
FIGURE_KINDS = ("mermaid", "svg")
#: mermaid 源码的**首词**必须是图类型关键字（模型偶尔会在前面写一句解释，这里不当成合法）
MERMAID_KEYWORDS = (
    "flowchart",
    "graph",
    "sequenceDiagram",
    "stateDiagram-v2",
    "classDiagram",
    "erDiagram",
    "gantt",
    "pie",
    "mindmap",
    "timeline",
)
MAX_MERMAID_LINES = 40
MAX_MERMAID_CHARS = 2000
#: ``figure.caption`` 上限（超出就截断，并记一条可见降级——不许静默截断）
MAX_CAPTION_CHARS = 40
#: SVG 清洗白名单里要删的元素（``local name``，忽略命名空间）
SVG_FORBIDDEN_TAGS = ("script", "foreignObject")
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

# ------------------------------------------------------- 局部重跑 / 在线压缩 --
#: ``meta.rerun.reason`` 的三个取值，与 CLI 的三个互斥参数一一对应（§6.1）
RERUN_REASONS = ("only", "retry-failed", "from-signals")

#: 「有段落没被改写」那条降级说明的固定前缀（run 级；合并器据此丢掉**可能已不成立**的旧句子，
#: 见 :func:`merge_artifacts`：这条状态由 ``rewrite_incomplete`` / ``model_errors`` 字段重算）
INCOMPLETE_DEGRADED_PREFIX = "改写不完整："

#: 在线压缩的作用域恒为整段（契约 §5.2 的 ``scope: "paragraph"``）
COMPRESS_SCOPE = "paragraph"
#: 在线压缩请求里 ``reason`` 的缺省值（读者点的那颗「太啰嗦」）
DEFAULT_COMPRESS_REASON = "too_verbose"
#: 在线压缩的降级 ``reason`` 枚举（契约 §5.2，**冻结**：前端按这套取值分流）
COMPRESS_REASONS = (
    "model_not_configured",
    "model_error",
    "bad_reply",
    "fact_anchor_mismatch",
    "no_artifacts",
    "paragraph_not_found",
    "compress_not_shorter",
)
#: 写盘成功后带回前端的一句提示（前端直接展示；文案里含「已压缩，请重跑 ar check」）
COMPRESS_HINT = "已压缩，请重跑 ar check 复核（fidelity 已重置为未校验）"
#: 这一段三类事实锚（数字 / 引号引文 / 书名号）合计为 0 时的可见说明（2026-10-13 复核修复 F1）。
#: **不拒绝写入**（没有事实可毁的段落压缩本来无害），但「0 比对项 = 校验空跑」这件事必须看得见 ——
#: 口径与 ``ar.fidelity`` 顶部那两条推论一致：那一段的压缩稿**未经事实锚校验**，任何更短的文本都能过关。
COMPRESS_NO_ANCHOR_NOTE = (
    "本段没有可比对的事实锚（数字 / 引号引文 / 书名号）：本次压缩未经事实锚校验"
    "（0 比对项 = 检查器空跑，不代表压得干净）"
)

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
    """模型客户端的结构标注（真实实现在 ``ar.model``，测试用假客户端）。

    ``planDecisions`` **不直接调它**：``client`` 一路转交给工具层（``ar.tools.run_tool``），
    由统一调用层（``ar.llm``）按重试策略调 ``arequest_once`` 并逐次记账。
    """

    async def arequest_once(self, request: Any) -> Any: ...


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
    #: ``illustrate`` 成功时的图定义（``{kind, source, caption?}``；只进 ``changes.json``，
    #: **不进** ``decisions.json``、也**不进** ``rewritten.md``）
    figure: dict[str, Any] | None = None
    #: ``illustrate`` 失败的原因（一句话）；失败时 ``action`` 退回 ``keep``，正文照旧
    figure_error: str = ""
    #: 模型对本段事实锚的**逐条申报**（§14.1）：``{numbers:[…], quotes:[…], books:[…], note?}``——
    #: 「本段没有原样保留哪几项事实锚」。与 ``figure`` 同口径：只进 ``changes.json``
    #: （``changes[i].fact_declarations``），**不进** ``decisions.json``；没有申报时是 ``None``。
    fact_declarations: dict[str, Any] | None = None
    #: 这一段 ``why.profile`` 是**骨架照读者自评兜底补齐**的（模型漏键 / 给错形状 / 该给引用却给空数组）。
    #: 与 ``figure`` / ``deferred`` 同口径：**只在为真时**写进 ``changes.json``
    #: （``changes[i].profile_refs_filled``），**不进** ``decisions.json``。
    #: 它是页面那枚「兜底补齐」徽标的**唯一判据**——前端不许拿正则去猜 ``why.note`` 的文案。
    profile_refs_filled: bool = False
    #: 第一遍（``--plan-first``，``plan-v2``）给这一段打的**计划动作**；与最终动作不同时落
    #: ``changes[i].plan_action``（**为真才出现**，照 ``fact_declarations`` 的先例）。
    #: ``None`` = 没有计划，或计划与执行一致（一致时不必多写一个键）。
    plan_action: str | None = None

    @property
    def deferred(self) -> bool:
        """「枚举内、但本期未实现」的动作已按 ``keep`` 处理。

        四个动作现在都已实现，所以它恒为 ``False``——**字段与文案都保留**（老产物的口径不变），
        只是不再有对象。
        """
        return self.action == "keep" and self.requested_action in UNIMPLEMENTED_ACTIONS

    def to_record(self) -> dict[str, Any]:
        """落 ``decisions.json`` 的形态（``rewrite`` 不落盘：改写稿另有 ``rewritten.md``；
        ``figure`` 同理——图定义只进 ``changes.json``，与 ``deferred`` 同口径）。
        """
        return {
            "paragraph": self.paragraph,
            "action": self.action,
            "why": {"profile": list(self.why.get("profile") or []), "note": str(self.why.get("note") or "")},
            "confidence": self.confidence,
        }


@dataclass
class PlanEntry:
    """第一遍（``--plan-first``）给一段打的动作标（``plan-v2``）。

    ``action`` 已经过 :func:`_parse_plan` 归内：枚举内的四个动作原样保留，**枚举外的按 ``keep``**
    并记了一条可见降级（原始取值不再传播，降级文案里会写清模型原本给的是什么）。
    ``note`` 是模型给的一句话依据（第一遍提示词要求留空即降级）。
    """

    paragraph: str
    action: str
    note: str = ""

    def to_record(self) -> dict[str, Any]:
        """落 ``meta.plan`` 的形态（``{paragraph, action, note}``；缺段 / 越界另有 meta 字段说明）。"""
        return {"paragraph": self.paragraph, "action": self.action, "note": self.note}


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
    #: 调度方式（rewrite-v7）：batch = 合并请求；per-paragraph = 一段一请求（可带校验重试）
    planning_mode: str = "batch"
    concurrency: int = 4
    validation_retries: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    request_count: int = 0
    batch_count: int = 0
    profile_used: bool = False
    profile_note: str = ""
    #: 这次改写有没有拿到非空素描（读前背景问卷的产物；契约 §6.3）
    background_used: bool = False
    #: 素描文本的 8 位哈希：判断「这次用的是哪版背景」的唯一依据
    background_fingerprint: str = ""
    #: 本次生效的事实锚口径（§14.1：读者画像偏好 ``pref_fact_anchor`` 的两档取值之一）
    fact_anchor_policy: str = DEFAULT_FACT_ANCHOR_POLICY
    #: 那档口径从哪里来的（人读来源说明，CLI 打印）
    fact_anchor_policy_source: str = ""
    deferred_count: int = 0
    usage_note: str = ""
    decisions_path: Path | None = None
    rewritten_path: Path | None = None
    changes_path: Path | None = None
    #: 局部重跑留痕（§6.2 第 7 条）：``None`` = 整篇改写。形状与产物 ``meta.rerun`` 一致
    rerun: dict[str, Any] | None = None
    #: 本次重跑的目标段（全篇段序）；整篇改写时是空列表
    rerun_targets: list[str] = field(default_factory=list)
    #: 模型没给 ``why.profile`` 引用、而这一段涉及的概念读者有自评 ⇒ 骨架**兜底补齐**了的段 id
    #: （可见面：CLI 单独打一行，``meta.degraded`` 里也有逐段的说明）
    profile_refs_filled: list[str] = field(default_factory=list)
    #: 合并后的 ``meta.rerun_history``（每次一条，含该次 ``usage`` / ``retry_account``）
    rerun_history: list[dict[str, Any]] = field(default_factory=list)
    #: 这次是不是走了两遍式（``--plan-first``）。``False`` 时下面几栏**一个都不写进产物**
    plan_first: bool = False
    #: 第一遍的计划（按全篇段序）；CLI 据此打「第一遍打标：N 段（展开 x / 压缩 y / 保持 z）」那一行
    plan: list[PlanEntry] = field(default_factory=list)
    #: 第一遍漏了哪几段（``meta.plan_missing``）：**不算 unplanned、不改退出码**
    plan_missing: list[str] = field(default_factory=list)
    plan_usage: dict[str, Any] = field(default_factory=dict)
    plan_request_count: int = 0
    plan_attempts: list[dict[str, Any]] = field(default_factory=list)
    plan_retry_account: dict[str, Any] = field(default_factory=dict)
    plan_prompt_version: str = ""
    #: 这一轮**有没有沿用**旧产物里第一遍的计划（局部重跑 + 旧产物有计划时才可能为真）：
    #: 为真 ⇒ 0 次打标请求，CLI 那行账写「复用旧计划，0 次请求」
    plan_reused: bool = False
    #: 目标段里**没能注入旧计划**的那几段（旧计划没有它的条目 / 口径对不上）：
    #: 非空时 CLI 点名打一行、产物 ``meta.plan_not_applied`` 也记着（不许静默降级成单遍改写）
    plan_not_applied: list[str] = field(default_factory=list)
    #: 逐段对话是否用了「上下文瘦身」（``--conversation-digest``）：**只在开时为 true**
    conversation_digest: bool = False

    def action_counts(self) -> dict[str, int]:
        counts = {action: 0 for action in ACTIONS}
        for decision in self.decisions:
            counts[decision.action] = counts.get(decision.action, 0) + 1
        return counts

    @property
    def incomplete(self) -> bool:
        """是否有段落因模型问题**没被改写**（不是模型主动选择「保持」）。"""
        return bool(self.model_errors or self.unplanned_paragraphs)


def _apply_plan_fields(
    outcome: RewriteOutcome,
    *,
    plan_first: bool,
    entries: list[PlanEntry],
    missing: list[str],
    extra: dict[str, Any],
    conversation_digest: bool,
) -> None:
    """把两遍式 / 对话瘦身的**可见字段**挂到 outcome 上（CLI 据此打第一遍那一行摘要）。

    只在开关真的打开时挂：默认路径的 outcome 与改动前**一个字段都不多**（``plan_first=False``、
    其余默认空）。产物里的同名键由 ``meta`` 的拼装负责（见 ``run_rewrite` / ``merge_artifacts``）。
    """
    if plan_first:
        outcome.plan_first = True
        outcome.plan = list(entries)
        outcome.plan_missing = list(missing)
        outcome.plan_usage = dict(extra.get("plan_usage") or {})
        outcome.plan_request_count = int(extra.get("plan_request_count") or 0)
        outcome.plan_attempts = [
            dict(item) for item in (extra.get("plan_attempts") or []) if isinstance(item, dict)
        ]
        outcome.plan_retry_account = dict(extra.get("plan_retry_account") or {})
        outcome.plan_prompt_version = str(extra.get("plan_prompt_version") or PLAN_PROMPT_VERSION)
    if conversation_digest:
        outcome.conversation_digest = True


@dataclass
class PlanItem:
    """一批段落里的一段：决策 + 该段要不要真的改写。"""

    paragraph: dict[str, Any]
    decision: Decision

    @property
    def needs_rewrite(self) -> bool:
        """这一段要不要用模型给的改写稿（``expand`` / ``compress``；其余一律用原文）。"""
        return self.decision.action in TEXT_ACTIONS


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
    #: 这一批的逐次尝试账（工具层统一信封里的 ``attempts``，落 ``meta.batch_usage[].attempts``）
    attempts: list[dict[str, Any]] = field(default_factory=list)
    #: 这一批的汇总账（``{tool, requests, retries, codes}``；整批都没发出去时是空字典）
    account: dict[str, Any] = field(default_factory=dict)

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
    **段内空行并成单换行**（2026-10-18）：``rewritten.md`` 按空行切段，改写真身再带一段
    「另起一段的解释」会被切成两段，``ar check`` 的段数就对不上；并成单行可读性略降但结构正确，
    这次合并由 :func:`_normalize_plan_items` 记一条可见降级。
    """
    if not isinstance(text, str):
        return ""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _INDENT_RE.sub("", cleaned)
    cleaned = _BLANK_LINE_RE.sub("\n", cleaned)
    lines = [line.rstrip() for line in cleaned.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _normalize_plan_items(raw: Any) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """把一批模型输出整理成 ``(条目, {段 id: 问题清单})``（只做类型整理，越界动作与缺段留给上层记账）。

    ⚠ **模型输出不经 JSON Schema 校验**（``ar.tools`` 的 schema 只校验调用方入参）：
    这里挑哪些键、落下哪些键就是最终结果，漏掉一个键等于**静默丢弃**。
    所以 ``figure``（``illustrate`` 的图定义）与 ``fact_declarations``（事实锚申报，§14.1）
    也必须在这里同步带上。

    ``why`` / ``why.profile`` 的形状问题（``why`` 不是对象、``profile`` 漏键 / 不是数组 /
    里面有非字符串项）与 ``fact_declarations`` 不是对象，都收进**第二个返回值**，
    由 :func:`_parse_batch` 交给上层记进 ``meta.degraded``——「漏一个键」不等于「静默丢一次引用」。
    问题清单**不放进条目**：条目要满足 ``ar.tools`` 出参 schema（``additionalProperties: False``），
    多一个内部键就会破坏它（有专门用例逐键核对）。
    """
    if not isinstance(raw, list):
        return [], {}
    items: list[dict[str, Any]] = []
    notes: dict[str, list[str]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("paragraph") or entry.get("id")
        if not isinstance(pid, str) or not pid.strip():
            continue
        pid = pid.strip()
        action = entry.get("action")
        action = action.strip().lower() if isinstance(action, str) else ""
        issues: list[str] = []
        why_raw = entry.get("why")
        if not isinstance(why_raw, dict):
            issues.append(
                f"why 不是对象（实际 {type(why_raw).__name__}）：这一段的依据读不通，"
                "why.profile 按空数组处理"
            )
            why_raw = {}
        profile_raw = why_raw.get("profile")
        profile: list[str] = []
        if profile_raw is None:
            issues.append("why.profile 缺失（模型没有给这一段的画像引用）")
        elif not isinstance(profile_raw, list):
            issues.append(
                f"why.profile 不是数组（实际 {type(profile_raw).__name__}）："
                "里面的引用读不通，按空数组处理"
            )
        else:
            # 只接受字符串（2026-10-14 收紧）：这里此前是 ``isinstance(item, (str, int))`` 后再
            # ``str(item)``——模型给 ``[42]`` 会被**静默**写成 ``"42"`` 留下来，既不报也不补。
            # 那个 ``int`` 分支没有任何合法用途，三条依据一起说明它收得动：
            #   ① 引用的形态是 **概念 id : 读者自评状态**（``c03:unknown``），概念 id 必须形如
            #      ``c01``（``ar.analyze.validate_inventory`` 逐条校验），整数在任何输入下都不是
            #      合法引用；
            #   ② ``ar.tools._DECISION`` 的出参 schema 本来就写着
            #      ``profile.items: {"type": "string"}``——实现比契约松，正是「模型输出不经 schema
            #      校验、所以实现挑什么就是什么」的典型口子；
            #   ③ 全仓没有一处测试或调用方依赖整数被接受（``git show HEAD`` 里这行是历史遗留，
            #      不是本轮引入的）。
            # 现在非字符串项与「数组里混进对象」走**同一档可见说明**（下面的 ``issues``），
            # 由上层记进 ``meta.degraded``，并照读者自评兜底补齐——不再是静默转换。
            profile = [item for item in profile_raw if isinstance(item, str)]
            dropped = len(profile_raw) - len(profile)
            if dropped:
                issues.append(
                    f"why.profile 里有 {dropped} 项不是字符串：已忽略（引用读不通；"
                    "引用只能是字符串，形如 c03:unknown——概念 id + 读者自评状态）"
                )
        note = why_raw.get("note")
        figure = entry.get("figure")
        declarations = entry.get("fact_declarations")
        if declarations is not None and not isinstance(declarations, dict):
            # 非对象（模型偶尔给一个字符串）**不许静默丢**：否则业务层只看到「没申报」，
            # 用户看不到任何「申报读不通」的说明（对象形状但字段类型不对那条路本来就有说明）
            issues.append(
                f"fact_declarations 不是对象（实际 {type(declarations).__name__}）：按「没有申报」处理"
            )
        rewrite_raw = entry.get("rewrite") or entry.get("text") or ""
        if isinstance(rewrite_raw, str) and _BLANK_LINE_RE.search(rewrite_raw):
            issues.append("改写正文里有空行：已并成同一段（否则 rewritten.md 会被切成多段、段数对不上）")
        if issues:
            notes[pid] = issues
        items.append(
            {
                "paragraph": pid,
                "action": action,
                "why": {"profile": profile, "note": note.strip() if isinstance(note, str) else ""},
                "confidence": _as_probability(entry.get("confidence")),
                "rewrite": _clean_rewrite(rewrite_raw),
                # 非对象一律当「没给」：失败原因由 _prepare_figure 写成人话
                "figure": figure if isinstance(figure, dict) else None,
                # 申报的归一化交给 ar.fidelity.parse_declarations（与抽取口径同一套归一化），
                # 这里只保留原始形态：非对象一律当「没给」（问题清单已进第二个返回值），
                # 读不通的地方由 _finalize_items 记降级。
                "fact_declarations": declarations if isinstance(declarations, dict) else None,
            }
        )
    return items, notes


# --------------------------------------------------------- 配图（illustrate，§4.2） --
def _local_name(tag: Any) -> str:
    """取元素 / 属性的 local name（去掉 ``{namespace}`` 前缀）。"""
    return str(tag).rsplit("}", 1)[-1]


def _clean_svg(source: str) -> tuple[str | None, list[str]]:
    """按白名单校验自包含 SVG；返回 ``(可用的源码, 问题清单)``，不合法时源码是 ``None``。

    白名单之外的内容（``<script>`` / 一切 ``on*`` 属性 / ``foreignObject`` / 引用外部资源的
    ``href``、``xlink:href``）**一律拒绝**（不渲染该图，由调用方退回 ``keep`` + ``figure_error``
    + 可见降级）；一个都没碰到的 SVG **原样返回**（不做序列化往返，免得平白改动模型的源码）。

    ⚠ 与契约 §4.2 的措辞有一处出入，这里按 §11.2 的负向对照实现（它更严、也更安全）：
    §4.2 说这些节点「删」掉即可接着用，而 §11.2 与验收都要求「带 ``<script>`` 的 SVG →
    退回 ``keep`` + ``figure_error`` + 可见降级」。清洗完接着画等于**悄悄换个东西渲染**，
    与本项目「不许静默改写」的底线冲突；错误信息里点名违规项，前端因此能给出可读的降级文案。
    """
    if not source.strip():
        return None, ["SVG 源码为空"]
    try:
        root = ElementTree.fromstring(source)
    except ElementTree.ParseError as exc:
        return None, [f"SVG 解析失败（{exc}）"]
    if _local_name(root.tag) != "svg":
        return None, [f"SVG 根元素是 <{_local_name(root.tag)}>，不是 <svg>"]

    violations: list[str] = []
    for element in root.iter():
        name = _local_name(element.tag)
        if name in SVG_FORBIDDEN_TAGS:
            violations.append(f"<{name}> 元素")
            continue
        for attribute in element.attrib:
            key = _local_name(attribute)
            if key.lower().startswith("on"):
                violations.append(f"on* 事件属性 {key}")
            elif key == "href" and not str(element.attrib[attribute]).strip().startswith("#"):
                violations.append("指向外部资源的 href")
    if violations:
        listed = "、".join(dict.fromkeys(violations))
        return None, [f"SVG 里有不在白名单内的 {listed}（不渲染该图）"]
    return source, []


def _mermaid_problem(source: str) -> str | None:
    """mermaid 源码的合法性检查；返回人读原因（``None`` = 通过）。"""
    text = source.strip()
    if not text:
        return "mermaid 源码为空"
    if "```" in text:
        return "mermaid 源码里带了 Markdown 代码围栏"
    first = re.split(r"[\s:;]", text, maxsplit=1)[0]
    if first not in MERMAID_KEYWORDS:
        return f"mermaid 源码的首词是 {first!r}，不是图类型关键字（{' / '.join(MERMAID_KEYWORDS[:4])} 等）"
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > MAX_MERMAID_LINES:
        return f"mermaid 源码有 {len(lines)} 行（上限 {MAX_MERMAID_LINES} 行）"
    if len(text) > MAX_MERMAID_CHARS:
        return f"mermaid 源码有 {len(text)} 字符（上限 {MAX_MERMAID_CHARS} 字符）"
    return None


def _extra_anchor_note(pid: str, figure_source: str, source_text: str) -> str | None:
    """图源码里出现原文没有的事实锚点 → 一条可见降级（**不阻断**，契约 §1 第 3 条）。

    图不参与事实锚校验（SVG 的坐标数字无法避免），但「图里多了原文没有的数字 / 引文 / 书名」
    这件事要看得见——一条降级说清多在哪一类、有几项。
    """
    source_facts = facts_of(source_text)
    figure_facts = facts_of(figure_source)
    extra_parts: list[str] = []
    for field_name, label in (("numbers", "数字"), ("quotes", "引号引文"), ("books", "书名号")):
        known = set(getattr(source_facts, field_name))
        extra = [item for item in getattr(figure_facts, field_name) if item not in known]
        if extra:
            extra_parts.append(f"{label} {len(extra)} 项")
    if not extra_parts:
        return None
    return (
        f"{pid}：配图源码里有原文没有的事实锚点（{'、'.join(extra_parts)}）；"
        "图不参与事实锚校验（SVG 坐标数字无法避免），这里只记一条降级、不阻断"
    )


def _prepare_figure(
    raw: Any,
    source_text: str,
    *,
    pid: str,
    degraded: list[str],
) -> tuple[dict[str, Any] | None, str]:
    """校验模型给的 ``figure``（mermaid 形状 / SVG 白名单）；返回 ``(figure, figure_error)``。

    两者最多一个非空：通过时给 ``figure``，失败时给一句人读的 ``figure_error``。
    失败的**每一步**都写进 ``degraded``（人读文案）：调用方据此把该段退回 ``keep`` 并带上
    ``figure_error``——「配图失败」绝不能是静默的。
    """
    if raw is None:
        return None, "模型给了 illustrate 但没有给 figure（图定义缺失）"
    if not isinstance(raw, dict):
        return None, f"figure 不是对象（实际 {type(raw).__name__}）"
    kind = raw.get("kind")
    kind = kind.strip().lower() if isinstance(kind, str) else ""
    if kind not in FIGURE_KINDS:
        return None, f"figure.kind {raw.get('kind')!r} 不在 {' / '.join(FIGURE_KINDS)} 之内"
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        return None, "figure.source 不是非空字符串"

    cleaned: str
    if kind == "mermaid":
        problem = _mermaid_problem(source)
        if problem:
            return None, problem
        cleaned = source.strip()
    else:
        svg, notes = _clean_svg(source)
        if svg is None:
            return None, notes[0] if notes else "SVG 不在白名单内"
        cleaned = svg

    figure: dict[str, Any] = {"kind": kind, "source": cleaned}
    caption = raw.get("caption")
    if isinstance(caption, str) and caption.strip():
        text = caption.strip()
        if len(text) > MAX_CAPTION_CHARS:
            degraded.append(
                f"{pid}：figure.caption 超过 {MAX_CAPTION_CHARS} 字（{len(text)} 字），已截断"
            )
            text = text[:MAX_CAPTION_CHARS]
        figure["caption"] = text

    note = _extra_anchor_note(pid, cleaned, source_text)
    if note:
        degraded.append(note)
    return figure, ""


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
    figure: dict[str, Any] | None = None
    figure_error = ""
    source_text = str(paragraph.get("text") or "")
    if requested in UNIMPLEMENTED_ACTIONS:  # pragma: no cover - 四个动作都实现后这里是空的
        # 留枚举位：枚举内未实现的动作按 keep 处理并标 deferred（可见，不静默）
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
        degraded.append(f"{pid}：模型对 keep 段也给了改写正文，已忽略（以「保持原文」为准）")
    elif requested == "illustrate":
        # 图不进 md：该段正文与原文逐字节相同；失败一律退回 keep + figure_error（可见）
        figure, figure_error = _prepare_figure(
            raw_item.get("figure"), source_text, pid=pid, degraded=degraded
        )
        if figure_error:
            action = "keep"
            why["note"] = f"{note}（配图失败，按保持处理：{figure_error}）"
            degraded.append(f"{pid}：配图失败，已退回「保持」（正文用原文）：{figure_error}")
        elif raw_item["rewrite"]:
            degraded.append(f"{pid}：illustrate 段也给了改写正文，已忽略（图不进正文，正文保持原文）")

    source_length = len(source_text)
    rewrite_text = raw_item["rewrite"] if action in TEXT_ACTIONS else ""
    if action == "expand":
        if len(rewrite_text) <= source_length:
            # 「展开」却没变长：多半只回了正文的一部分（截断），必须可见
            degraded.append(
                f"{pid}：模型给的展开正文（{len(rewrite_text)} 字）不比原文（{source_length} 字）长，"
                "疑似截断或没有原样保留全文，事实锚校验会看得出来"
            )
        elif not rewrite_text.rstrip().endswith(SENTENCE_ENDINGS):
            degraded.append(f"{pid}：展开正文没有以句末标点收尾，疑似被截断")
    elif action == "compress":
        # 截断判据与 expand **方向相反**（同一句话对 compress 是反的，所以拆成两条）：
        # 压缩求「变短 + 句末标点收尾」
        if not rewrite_text:
            # 空稿 = 什么都没压：退回 keep（留一个「compress 但正文一个字没变」的动作是假成功）
            action = "keep"
            why["note"] = f"{note}（模型给的动作是「压缩」但没给压缩稿，按保持处理）"
            degraded.append(f"{pid}：模型给了 compress 却没有给压缩稿，已按「保持原文」处理（未压缩）")
        elif len(rewrite_text) >= source_length:
            degraded.append(
                f"{pid}：模型给的压缩稿（{len(rewrite_text)} 字）不比原文（{source_length} 字）短，"
                "疑似没压缩或只是换了个说法，事实锚校验会看得出来"
            )
        elif not rewrite_text.rstrip().endswith(SENTENCE_ENDINGS):
            degraded.append(f"{pid}：压缩稿没有以句末标点收尾，疑似被截断")

    return PlanItem(
        paragraph=paragraph,
        decision=Decision(
            paragraph=pid,
            action=action,
            why=why,
            confidence=confidence,
            rewrite=rewrite_text,
            requested_action=requested,
            figure=figure,
            figure_error=figure_error,
            fact_declarations=_prepare_declarations(raw_item.get("fact_declarations"), pid=pid, degraded=degraded),
        ),
    )


def _prepare_declarations(
    raw: Any, *, pid: str, degraded: list[str]
) -> dict[str, Any] | None:
    """归一化模型对**这一段**事实锚的申报（§14.1）；返回 ``None`` = 本段没有申报。

    读不通的地方（结构不对 / 值不是字符串 / 不认识的键 / note 超长）**一条都不静默**
    ——逐条写进 ``degraded``（``parse_declarations`` 的第二个返回值）。
    申报本身**不改动作、不改正文**：它只是「说清我动了什么」，放不放行由 ``ar check`` 按读者口径判
    （``keep_all`` 严 / ``allow_declared_drops`` 松，见 ``ar.fidelity.compare_blocks_detailed``）。
    """
    from .fidelity import parse_declarations  # noqa: PLC0415 - 与顶层 ``facts_of`` 同一模块，这里只为就近

    parsed, notes = parse_declarations(raw)
    for note in notes:
        degraded.append(f"{pid}：{note}")
    return parsed or None


def _parse_batch(
    raw_text: str,
    batch: list[dict[str, Any]],
    start: int,
    end: int,
    *,
    extract_json_object: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """解析一批输出 → 归一化条目 + 问题清单（多余段 id / 重复段 id / 形状读不通都点名）。

    问题清单里的形状问题来自 :func:`_normalize_plan_items` 的第二个返回值（``why`` /
    ``why.profile`` / ``fact_declarations`` 漏键或类型不对）；**被丢掉的那些条目**
    （不属于这一批 / 重复返回）不再为它们报形状问题——它们本来就没进产物。
    """
    issues: list[str] = []
    try:
        data = extract_json_object(raw_text or "")
    except InputError as exc:
        return [], [f"第 {start}–{end} 段：{exc}"]
    raw_items = data.get("decisions")
    if raw_items is None:
        raw_items = data.get("items")
    items, item_notes = _normalize_plan_items(raw_items)
    # 模型没给图 / 没申报时，出参里**不留这两个键**（与 ``_DECISION`` 声明的 schema 逐键一致；
    # 上层用 ``.get(...)`` 取值，两种形态等价）。
    items = [
        {
            key: value
            for key, value in item.items()
            if (key != "figure" or isinstance(value, dict))
            and (key != "fact_declarations" or isinstance(value, dict))
        }
        for item in items
    ]
    expected = {str(p.get("id")) for p in batch}
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    out_of_batch: list[str] = []
    for item in items:
        pid = item["paragraph"]
        if pid not in expected:
            # 全文进了固定上下文之后，模型偶尔会把「看到的所有段」都答一遍；
            # 逐条记会刷屏，聚合成一条（仍是可见降级，不是静默丢弃）。
            if pid not in out_of_batch:
                out_of_batch.append(pid)
            continue
        if pid in seen:
            issues.append(f"{pid}：模型重复返回了这一段的决策，只取第一条")
            continue
        seen.add(pid)
        kept.append(item)
        issues.extend(f"{pid}：{note}" for note in item_notes.get(pid, ()))
    if out_of_batch:
        shown = "、".join(out_of_batch[:6]) + ("…" if len(out_of_batch) > 6 else "")
        issues.insert(
            0,
            f"第 {start}–{end} 段：模型返回了不属于这一批的段 id {shown}"
            f"（共 {len(out_of_batch)} 个），已忽略",
        )
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


def _empty_self_rating_note(reader: str, profile: dict[str, Any]) -> str:
    """「空自评」那一句的措辞（**逐字稳定**：同一画像必得同一段文本 ⇒ 前缀缓存不受影响）。

    ⚠ 措辞纪律（2026-10-14 复核修复）：**不许写「画像为空」**。读者没有概念自评，
    与「读者没有画像」是两件事——背景非空即算「有画像」（契约 §6.4），而背景依据按拍板 3
    只写在 ``why.note`` 里。旧文案（「…画像为空：请只依据下面的概念难度提示判断，
    并在 why.profile 里写空数组 []」）两头都错：既不承认背景是依据，又直接指示模型把
    ``why.profile`` 写成空数组——2026-10-13 的真机产物里 25 段 ``why.profile`` 全是空数组、
    7 段的 ``why.note`` 自称「画像为空」，其中 4 段同一句里又引用了背景（自相矛盾）。

    这里只**如实描述**「读者有没有提供概念自评」；``why.profile`` 怎么写由
    ``REWRITE_USER_TEMPLATE`` 的规则 11 交代（单段压缩那份提示词没有这个字段，别在这里写）。
    """
    if has_background(profile):
        return (
            "读者 " + reader + " 没有提供任何概念自评（只有读前背景问卷：「读者背景」那一节是背景，"
            "不是概念自评，也不等于「没有画像」）"
        )
    return "读者 " + reader + " 没有提供任何概念自评，也没有读前背景（本次没有任何读者信号）"


def _format_profile_block(reader: str, profile: dict[str, Any], overrides: list[dict[str, Any]]) -> str:
    """画像素描（只给人话，不泄漏内部结构）；空自评时如实说明是哪一种「空」。

    有自评时仍是 ``c01=unknown`` 那一行（元素格式不变，前端 chips 与 ``why.profile`` 的
    ``c01:unknown`` 是两处口径，见契约 §4.4）；空自评那一段的措辞见 :func:`_empty_self_rating_note`。
    """
    concepts = profile.get("concepts") or {}
    lines: list[str] = []
    for cid, entry in concepts.items():
        entry = entry if isinstance(entry, dict) else {}
        lines.append(str(cid) + "=" + str(entry.get("state") or "unknown"))
    if lines:
        header = "读者 " + reader + " 自报概念状态：" + "；".join(lines)
    else:
        header = _empty_self_rating_note(reader, profile)
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


def _self_rating_states(profile: dict[str, Any]) -> dict[str, str]:
    """读者自己的概念自评 ``{概念 id: 状态}``：``overrides``（手动结论，优先级最高）压过 ``concepts``。

    这是兜底补齐 ``why.profile`` 时**唯一允许的依据**：只用读者自己写下的东西
    （``c03=unknown`` 与 ``c03:unknown`` 是同一件事的两种写法，见契约 §4.4），
    绝不拿概念难度提示去编一条「自评」。
    """
    states: dict[str, str] = {}
    concepts = profile.get("concepts")
    if isinstance(concepts, dict):
        for cid, entry in concepts.items():
            entry = entry if isinstance(entry, dict) else {}
            state = str(entry.get("state") or "").strip()
            if state:
                states[str(cid)] = state
    overrides = profile.get("overrides")
    if isinstance(overrides, list):
        for override in overrides:
            if not isinstance(override, dict):
                continue
            cid = str(override.get("concept") or "").strip()
            state = str(override.get("state") or "").strip()
            if cid and state:
                states[cid] = state
    return states


def _self_rating_refs(
    paragraph_id: str, states: dict[str, str], concept_paragraphs: dict[str, list[str]]
) -> list[str]:
    """本段涉及的概念里**读者有自评**的那些 → ``["c03:unknown", …]``（按概念清单顺序）。

    模型没给 ``why.profile`` 时的兜底补齐就用它。本段涉及的概念读者一个自评都没有时返回空列表
    ——那时空数组是**合法结果**（模型依据的是难度提示或背景），不许误报：无自评 ≠ 模型偷懒。
    """
    refs: list[str] = []
    for cid, paragraphs in concept_paragraphs.items():
        if paragraph_id in paragraphs and cid in states:
            refs.append(f"{cid}:{states[cid]}")
    return refs


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


def _merge_accounts(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    """把逐批的汇总账合成**整次改写**的一条（形状与 ``ar.llm.retry_account`` 逐字一致）。

    ``requests`` = 全部批次的尝试总数、``retries`` = 全部批次的**重试**总数
    （每批的第一次尝试不算重试）、``codes`` = 失败过的码去重保序。
    单批时与 ``ar.llm.retry_account`` 的结果完全相同（``requests = len(attempts)``、
    ``retries = len(attempts) - 1``）；多批时直接对它取 ``len - 1`` 会把「第一批的首次尝试」
    也算成重试，所以这里逐批累加。
    """
    codes: list[str] = []
    requests = 0
    retries = 0
    for account in accounts:
        requests += int(account.get("requests") or 0)
        retries += int(account.get("retries") or 0)
        for code in account.get("codes") or []:
            if str(code) not in codes:
                codes.append(str(code))
    return {"tool": tools.REWRITE_BATCH_SPEC.name, "requests": requests, "retries": retries, "codes": codes}


def _plan_one_batch(
    client: ChatClient,
    ranges: list[int],
    paragraphs: list[dict[str, Any]],
    context: dict[str, Any],
) -> BatchPlan:
    """一次请求吃一批段落（经工具层 ``rewrite_batch``：拼提示词 / 调模型 / 解析都在那边）。"""
    start, end = ranges[0] + 1, ranges[-1] + 1
    batch = [paragraphs[index] for index in ranges]
    plan = BatchPlan(start=start, end=end, paragraphs=[str(p.get("id")) for p in batch])
    params = {
        # 工具入参是契约形态的段落表（只要 id 与 text）：source.meta.json 的段还带 index / text_hash
        "paragraphs": [{"id": str(p.get("id")), "text": str(p.get("text") or "")} for p in batch],
        "start": start,
        "end": end,
        "profile_block": context["profile_block"],
        "concept_block": context["concept_block"],
        "background_sketch": context.get("background_block") or "",
        # 配图候选素描（rewrite-v4 起进固定块，rewrite-v5 沿旧）：空串时那一格零字符插入（空块 == 缺省）
        "illustration_block": context.get("illustration_block") or "",
        # 事实锚口径（rewrite-v5 的固定块，§14.1）：逐字稳定，同一读者重跑必得同一段文案
        "fact_anchor_policy": normalize_fact_anchor_policy(context.get("fact_anchor_policy")),
        # 全文（rewrite-v7）：同一材料的所有批 / 所有段共享，进「按材料变」的那一段
        "full_text_block": context.get("full_text_block") or "",
        # 第一遍的计划块（plan-v2）：同一材料的所有批共享；没开 --plan-first 时是空串（零字符插入）
        "plan_block": context.get("plan_block") or "",
        # 提示词里「材料：《{source_id}》，共 {total} 段」那一行：不传会退化成空材料名，逐字不等价
        "source_id": str(context.get("source_id") or ""),
        "total": int(context.get("total") or end),
    }
    try:
        run = tools.run_tool(tools.REWRITE_BATCH_SPEC.name, params, client=client)
    except Exception as exc:  # noqa: BLE001 - 调用失败要变成可见降级，不能让整篇崩掉
        plan.error = f"第 {start}–{end} 段：模型调用失败（{type(exc).__name__}）：{exc}"
        plan.degraded.append(plan.error)
        plan.items = [
            _finalize_items(paragraph, None, degraded=plan.degraded, unplanned=plan.unplanned)
            for paragraph in batch
        ]
        return plan

    plan.attempts = run.attempt_records
    plan.account = run.account()
    plan.model = run.model
    plan.usage = dict(run.usage or {})
    if not run.ok:
        # 面向用户的失败文案统一走 ``ar.llm.failure_message``：带尝试次数与末次错误码
        message = run.failure_message()
        if run.model_replied:
            # 模型答了、但输出不成结构：与迁移前同口径——**不算批次失败**（不进 model_errors），
            # 只记可见降级；这些段因为拿不到决策，会走「模型没有给出这段的决策」那条路
            plan.degraded.append(f"第 {start}–{end} 段：{message}")
        else:
            plan.error = f"第 {start}–{end} 段：{message}"
            plan.degraded.append(plan.error)
        plan.items = [
            _finalize_items(paragraph, None, degraded=plan.degraded, unplanned=plan.unplanned)
            for paragraph in batch
        ]
        return plan

    plan.degraded.extend(run.degraded)
    items = run.data.get("decisions") if isinstance(run.data, dict) else []
    by_id = {item["paragraph"]: item for item in items if isinstance(item, dict)}
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
    """**先跑完第 1 批（预热前缀），再并发跑其余批次**（顺序 = 段序，结果按下标对回）。

    为什么：提示词前缀缓存（prompt caching）按「从头逐字相同的部分」命中，而缓存构建要几秒。
    6 批同时首发时谁都吃不到刚建好的缓存（全部按未命中计费）；先让第 1 批把固定前缀
    （画像 + 概念清单 + JSON 示例 + 规则，约 2007 字符）在模型侧建起来，后面的批次才可能命中。

    **第 1 批也丢进工作线程跑**（`.result()` 等它结束再发其余批次）：预热批曾走
    「调用线程里直接调」的老路，而 ``_plan_one_batch`` → ``tools.run_tool`` → ``ar.llm.call``
    最终是 ``asyncio.run``，在已有运行中事件循环的调用方（async 调用方 / 将来的 HTTP 路由）里
    立刻抛 ``RuntimeError: asyncio.run() cannot be called from a running event loop``，
    全篇降级。工作线程里没有运行中的事件循环，CLI 与 async 调用方因此走同一条路。

    第 1 批失败**不影响**其余批次：``_plan_one_batch`` 把调用失败变成带 ``error`` 的可见降级
    （该批段落按 keep 处理并进 ``meta.model_errors``），其余批次照常发。
    """
    if not ranges:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(_plan_one_batch, client, ranges[0], paragraphs, context).result()
    rest = ranges[1:]
    if not rest:
        return [first]
    return [first, *_plan_batches_parallel(client, rest, paragraphs, context)]


# ----------------------------------------------- 两遍式改写的第一遍（plan-v2） --
def _one_line(text: Any) -> str:
    """自由文本 → 单行（换行 / 连续空白折叠成一个空格，首尾空白去掉）。

    :func:`_plan_block` 的格式契约靠它兜底：计划块**每行一段**「段 id 动作：依据」，
    note 里带换行会把一行劈成两行，第二遍的模型就会把后半截当成另一段的计划。
    """
    return " ".join(str(text or "").split())


def _plan_block(entries: list[PlanEntry]) -> str:
    """计划 → 第二遍提示词里的固定计划块（**逐字稳定**：同一份计划永远得到同一段文本）。

    每行形如 ``p001 keep`` / ``p002 expand：<一句话依据>``，段序按传入顺序（调用方已按全篇段序
    排好）。**格式契约：一行一段**——note 里的换行 / 连续空白由 :func:`_one_line` 压平成单行
    （模型给了多行依据也劈不开这个块，2026-10-19 复核修复）。空计划返回空串：第二遍那一格
    **零字符插入**，于是「第一遍失败 / 没有计划」时第二遍拿到的提示词与不开 ``--plan-first`` 时
    **逐字相同**。
    """
    lines: list[str] = []
    for entry in entries:
        note = _one_line(entry.note)
        lines.append(f"{entry.paragraph} {entry.action}" + (f"：{note}" if note else ""))
    return "\n".join(lines)


@dataclass
class PlanReuse:
    """旧产物里第一遍计划的**复用判定**（局部重跑那一路；见 :func:`_reuse_plan`）。

    ``ok=True`` = 这份计划可以照旧注入第二遍（口径也对得上）；``ok=False`` = 旧计划还在，但
    **不许沿用**，``reason`` 说明是哪一项对不上（读者 / 背景指纹 / 事实锚口径），调用方据此
    重新打标或把目标段记进 ``plan_not_applied``——两种情况都不许静默。
    """

    entries: list[PlanEntry] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    ok: bool = False
    reason: str = ""
    #: 复用过程中产生的可见降级（越界动作按 keep 处理之类；调用方原样并进 ``degraded``）
    notes: list[str] = field(default_factory=list)


def _reuse_plan(
    base: MergeBase | None,
    *,
    reader: str = "",
    background_fingerprint: str = "",
    fact_anchor_policy: str = DEFAULT_FACT_ANCHOR_POLICY,
) -> PlanReuse | None:
    """既有产物里的第一遍计划（局部重跑**优先照原样复用**，不重新打标）。

    ``--plan-first`` 与 ``--only`` / ``--retry-failed`` / ``--from-signals`` 同时给时走这条：
    局部重跑的口径是「只重跑这几段」，第一遍的计划本来就还在 ``decisions.json`` 的 ``meta`` 里
    （``MergeBase`` 带着整份 ``decisions_payload``）。旧产物没开过 ``--plan-first`` 时返回 ``None``，
    这次照常打标——不假装「有旧计划」。

    返回 ``ok=False`` 只有一种情况：**口径对不上**。旧计划是按「另一个读者 / 另一版背景 / 另一档
    事实锚口径」打的标，照旧注入就会产出「B 读者的产物 + A 读者的计划」这种自相矛盾的现场
    （2026-10-19 复核修复）。缺键按默认口径算：旧产物早于这几格时，读者必须相同、指纹两侧都为空、
    口径两侧都归一成默认，否则不复用。

    越界动作（旧产物被手改过 / 旧版本写的）按 ``keep`` 处理，但**留一条可见降级**——与
    :func:`_parse_plan` 同一条口径，不静默改写历史。
    """
    if base is None:
        return None
    meta = base.decisions_payload.get("meta")
    if not isinstance(meta, dict) or not meta.get("plan_first"):
        return None
    raw = meta.get("plan")
    if not isinstance(raw, list):
        return None
    entries: list[PlanEntry] = []
    notes: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = item.get("paragraph")
        if not isinstance(pid, str) or not pid.strip():
            continue
        action = str(item.get("action") or "")
        if action not in ACTIONS:
            # 与 _parse_plan 同口径：越界动作按 keep 处理，但降级必须看得见（不许静默改历史）
            label = f"「{action}」" if action else "（空）"
            notes.append(
                f"{pid.strip()}：旧计划里的动作 {label} 不在 {' / '.join(ACTIONS)} 之内，"
                "已按「保持」处理（复用计划时记 keep）"
            )
        entries.append(
            PlanEntry(
                paragraph=pid.strip(),
                action=action if action in ACTIONS else "keep",
                note=str(item.get("note") or ""),
            )
        )
    if not entries:
        return None
    missing = [str(pid) for pid in (meta.get("plan_missing") or []) if str(pid).strip()]
    extra: dict[str, Any] = {
        "plan_first": True,
        "plan_prompt_version": str(meta.get("plan_prompt_version") or PLAN_PROMPT_VERSION),
        "plan_usage": dict(meta.get("plan_usage") or {}),
        "plan_request_count": int(meta.get("plan_request_count") or 0),
        "plan_attempts": [
            dict(item) for item in (meta.get("plan_attempts") or []) if isinstance(item, dict)
        ],
        "plan_retry_account": dict(meta.get("plan_retry_account") or {}),
    }
    reuse = PlanReuse(entries=entries, missing=missing, extra=extra, notes=notes)
    # 口径复核对齐（2026-10-19 复核修复）：换读者后把 A 读者的计划注入 B 读者的提示词，
    # 产物会是「B 的 reader + A 的 plan」，比重新打标一次贵得多（错的东西看起来是对的）。
    mismatched: list[str] = []
    old_reader = str(meta.get("reader") or "")
    if old_reader != str(reader or ""):
        mismatched.append(f"读者 {old_reader or '（空）'} → {reader or '（空）'}")
    old_fingerprint = str(meta.get("background_fingerprint") or "")
    if old_fingerprint != str(background_fingerprint or ""):
        mismatched.append(
            f"读者背景指纹 {old_fingerprint or '（空）'} → {background_fingerprint or '（空）'}"
        )
    old_policy = normalize_fact_anchor_policy(meta.get("fact_anchor_policy"))
    new_policy = normalize_fact_anchor_policy(fact_anchor_policy)
    if old_policy != new_policy:
        mismatched.append(f"事实锚口径 {old_policy} → {new_policy}")
    if mismatched:
        reuse.ok = False
        reuse.reason = "、".join(mismatched)
        return reuse
    reuse.ok = True
    return reuse


def _parse_plan(
    raw: str, paragraphs: list[dict[str, Any]]
) -> tuple[list[PlanEntry], list[str], list[str]]:
    """第一遍的模型输出 → ``(按全篇段序排好的计划, 缺段 id, 降级说明)``。

    - **枚举外的动作**（模型给 ``frobnicate``，或压根没给 action）按 ``keep`` 处理并**记降级**；
    - 不属于这份材料的段 id / 重复条目一律点名忽略（不静默丢）；
    - **缺段不阻断**：缺的那几段进 ``plan_missing``，第二遍照样逐段改写，只是它们没有计划
      ——所以 ``plan_missing`` **不算 unplanned、不改退出码**。
    """
    from .analyze import extract_json_object  # noqa: PLC0415 - 与 _parse_batch 的解析口径同源

    wanted = [str(paragraph.get("id")) for paragraph in paragraphs]
    degraded: list[str] = []
    try:
        data = extract_json_object(raw or "")
    except InputError as exc:
        return [], list(wanted), [f"第一遍打标：{exc}；第二遍不带计划照常改写"]
    raw_plan = data.get("plan")
    if not isinstance(raw_plan, list):
        return [], list(wanted), ["第一遍打标：模型输出里没有 plan 数组；第二遍不带计划照常改写"]

    known = set(wanted)
    by_id: dict[str, dict[str, Any]] = {}
    unknown: list[str] = []
    for item in raw_plan:
        if not isinstance(item, dict):
            degraded.append("第一遍打标：plan 里有不是对象的条目，已忽略")
            continue
        pid = item.get("paragraph")
        if not isinstance(pid, str) or not pid.strip():
            degraded.append("第一遍打标：plan 里有缺 paragraph 的条目，已忽略")
            continue
        pid = pid.strip()
        if pid not in known:
            if pid not in unknown:
                unknown.append(pid)
            continue
        if pid in by_id:
            degraded.append(f"{pid}：第一遍的计划里重复出现，只取第一条")
            continue
        by_id[pid] = item
    if unknown:
        shown = "、".join(unknown[:6]) + ("…" if len(unknown) > 6 else "")
        degraded.append(
            f"第一遍打标：模型给了不属于这份材料的段 id {shown}（共 {len(unknown)} 个），已忽略"
        )

    entries: list[PlanEntry] = []
    missing: list[str] = []
    for pid in wanted:
        item = by_id.get(pid)
        if item is None:
            missing.append(pid)
            continue
        requested = str(item.get("action") or "").strip()
        action = requested if requested in ACTIONS else "keep"
        if action != requested:
            label = f"「{requested}」" if requested else "（空）"
            degraded.append(
                f"{pid}：第一遍给的动作 {label} 不在 expand / compress / keep / illustrate 四类之内，"
                "已按「保持」处理（计划里记 keep）"
            )
        note = str(item.get("note") or "").strip()
        if not note:
            degraded.append(f"{pid}：第一遍没有写依据（note 为空），计划里只留动作")
        entries.append(PlanEntry(paragraph=pid, action=action, note=note))
    if missing:
        shown = "、".join(missing[:8]) + ("…" if len(missing) > 8 else "")
        degraded.append(
            f"第一遍打标漏了 {len(missing)} 段（{shown}）：第二遍照常逐段改写，只是这几段没有计划；"
            "**不计入 unplanned_paragraphs、不改退出码**"
        )
    return entries, missing, degraded


def plan_actions(
    source_doc: dict[str, Any],
    inventory: dict[str, Any] | None,
    profile: dict[str, Any],
    client: ChatClient,
    *,
    source_id: str = "",
    reader: str = "",
    background_block: str = "",
    fact_anchor_policy: str = DEFAULT_FACT_ANCHOR_POLICY,
) -> tuple[list[PlanEntry], list[str], dict[str, Any]]:
    """两遍式的**第一遍**（``--plan-first``，``plan-v2``）：一次调用给全篇每段打动作标。

    返回 ``(计划, 降级说明, meta)``；``meta`` 里的键就是产物 ``meta`` 追加的那几格
    （``plan_first` / ``plan` / ``plan_missing` / ``plan_usage` / ``plan_request_count` /
    ``plan_attempts` / ``plan_retry_account` / ``plan_prompt_version`）。

    **失败不阻断**：调用失败 / 输出读不通时返回空计划 + 全部段进 ``plan_missing`` + 一条可见降级，
    第二遍照样按原模式跑（那一格的计划块是空串 ⇒ 提示词与不开这个开关时逐字相同）。
    **两遍共享同一段前缀**（``plan-v2``，2026-10-31）：第一遍的提示词是「改写那份的公共头
    （画像 / 背景 / 事实锚口径 / JSON 示例 / 规则 / 材料行 / 概念 / 配图 / 全文）+ 打标尾巴」，
    system 也直接用 ``REWRITE_SYSTEM_PROMPT`` ⇒ 两遍的公共前缀 = 9889 字符（5364 token），
    第一遍建好的缓存能被第二遍第 1 批直接命中。``plan-v1`` 时两遍的公共前缀是 **0 字符**
    （system 不同、user 首字符就分岔），打标的钱全价——这条 2026-10-19 的复核结论是按 v1 说的，
    已按 v2 作废。第二遍自己的批之间照旧命中（前缀里还有「计划」块）。
    """
    paragraphs = [p for p in (source_doc.get("paragraphs") or []) if isinstance(p, dict) and p.get("id")]
    if not paragraphs:
        raise InputError("source.meta.json 里没有任何段落，无法给全篇打动作标")
    sid = source_id or str(source_doc.get("source_id") or "")
    reader_id = reader or str(profile.get("reader") or "")
    params = {
        "paragraphs": [{"id": str(p.get("id")), "text": str(p.get("text") or "")} for p in paragraphs],
        "source_id": sid,
        "total": len(paragraphs),
        "profile_block": _format_profile_block(reader_id, profile, list(profile.get("overrides") or [])),
        "concept_block": _format_concept_block(sid, inventory),
        "background_sketch": background_block or "",
        # 配图候选（与 rewrite 同一个块）：没有候选时是空串，模板那一格一个字符都不出现
        "illustration_block": format_illustration_block(inventory),
        # 全文与事实锚口径（plan-v2）：这两格在**两遍共用的前缀**里，第一遍建好的缓存才能被
        # 第二遍第 1 批命中——拼法与 rewrite 那条路径**逐字同一套**（format_paragraph_block +
        # normalize_fact_anchor_policy），否则前缀就从这里分岔、缓存一个 token 都吃不到
        "full_text_block": format_paragraph_block(paragraphs),
        "fact_anchor_policy": normalize_fact_anchor_policy(fact_anchor_policy),
    }
    empty_account = {"tool": tools.PLAN_ACTIONS_SPEC.name, "requests": 0, "retries": 0, "codes": []}
    meta: dict[str, Any] = {
        "plan_first": True,
        "plan_prompt_version": PLAN_PROMPT_VERSION,
        "plan_request_count": 0,
        "plan_usage": {},
        "plan_attempts": [],
        "plan_retry_account": empty_account,
        "plan_missing": [],
        "plan": [],
    }
    all_ids = [str(p.get("id")) for p in paragraphs]
    degraded: list[str] = []
    try:
        run = tools.run_tool(tools.PLAN_ACTIONS_SPEC.name, params, client=client)
    except Exception as exc:  # noqa: BLE001 - 与逐批改写同一条降级路径：不能让整篇崩掉
        degraded.append(
            f"第一遍打标调用失败（{type(exc).__name__}）：{exc}；第二遍不带计划照常改写"
        )
        meta["plan_missing"] = all_ids
        return [], degraded, meta

    meta["plan_request_count"] = 1
    meta["plan_usage"] = dict(run.usage or {})
    meta["plan_attempts"] = [dict(item) for item in run.attempt_records]
    meta["plan_retry_account"] = run.account()
    if not run.ok:
        degraded.append(f"第一遍打标失败：{run.failure_message()}；第二遍不带计划照常改写")
        meta["plan_missing"] = all_ids
        return [], degraded, meta

    degraded.extend(run.degraded)
    entries, missing, notes = _parse_plan(run.raw_text, paragraphs)
    degraded.extend(notes)
    meta["plan"] = [entry.to_record() for entry in entries]
    meta["plan_missing"] = missing
    return entries, degraded, meta


# ------------------------------------------------------------------ 逐段循环 --
def _usage_snapshot(totals: dict[str, int], cached_total: int, cached_reported: bool) -> dict[str, Any]:
    """逐段循环的 usage 汇总（与 usage 的 3 个旧字段同形；网关没上报缓存就不写 cached_tokens）。"""
    usage: dict[str, Any] = dict(totals)
    if cached_reported:
        usage["cached_tokens"] = cached_total
    return usage


def _paragraph_problems(
    paragraph: dict[str, Any], raw_item: dict[str, Any] | None, *, policy: str
) -> list[str]:
    """逐段循环的**硬校验**：返回空列表 = 这一段的输出可以直接落盘。

    只看「能不能用」这一件事（与 ar check / 在线压缩同一套事实锚判据）：
    - 必须给出这一段的决策，动作必须在四类之内；
    - expand / compress 必须有正文，压缩**必须更短**、展开**必须更长**，且以句末标点收尾；
    - 事实锚按读者口径比对（申报覆盖的缺失可放行，多出来的 / 顺序调换一律不放行）。

    软性降级（confidence 缺失、why 形状读不通、figure 不合法等）仍由 _finalize_items 记账，
    不在这里重试——重试只花在「改完还是不能写」的段上。
    """
    pid = str(paragraph.get("id"))
    if raw_item is None:
        return [f"{pid}：模型没有给出这一段的决策"]
    action = str(raw_item.get("action") or "")
    if action not in IMPLEMENTED_ACTIONS:
        return [f"{pid}：动作「{action or '（空）'}」不在 expand / compress / keep / illustrate 之内"]
    source_text = str(paragraph.get("text") or "")
    rewrite_text = _clean_rewrite(raw_item.get("rewrite"))
    if action not in TEXT_ACTIONS:
        return []
    problems: list[str] = []
    if not rewrite_text:
        return [f"{pid}：动作是「{actions_label(action)}」但没有给改写正文"]
    if action == "compress" and len(rewrite_text) >= len(source_text):
        problems.append(
            f"{pid}：压缩稿（{len(rewrite_text)} 字）不比原文（{len(source_text)} 字）短"
        )
    if action == "expand" and len(rewrite_text) <= len(source_text):
        problems.append(
            f"{pid}：展开稿（{len(rewrite_text)} 字）不比原文（{len(source_text)} 字）长，疑似只回了片段"
        )
    if not rewrite_text.rstrip().endswith(SENTENCE_ENDINGS):
        problems.append(f"{pid}：改写正文没有以句末标点收尾，疑似被截断")
    declarations, _notes = parse_declarations(raw_item.get("fact_declarations"))
    comparison = compare_blocks_detailed(
        [{"id": pid, "text": source_text}],
        [rewrite_text],
        declarations={pid: declarations} if declarations else None,
        policy=policy,
    )
    problems.extend(comparison.problems)
    return problems


def _feedback_text(pid: str, problems: list[str], attempt_no: int) -> str:
    """校验回灌文案（追加在 user 末尾）：必须说清「上一次哪里没通过」，模型才能改。"""
    detail = "\n".join(f"- {item}" for item in problems)
    return (
        f"【校验反馈·第 {attempt_no} 次】上一版对 {pid} 的输出没有通过校验：\n{detail}\n"
        f"请重新给出 {pid} 这一段的 decisions（只输出这一段的 JSON，其余段不要给）；"
        "改写必须满足上面的全部规则，尤其不许丢事实锚。"
    )


def _plan_one_paragraph(
    client: ChatClient,
    index: int,
    paragraphs: list[dict[str, Any]],
    context: dict[str, Any],
    *,
    policy: str,
    validation_retries: int,
) -> BatchPlan:
    """逐段循环的一次「一段」：发请求 → 解析 → 硬校验 → 不过就带反馈重试（次数有上限）。

    与分批模式共用同一份提示词（rewrite-v7）、同一套工具契约与解析；
    区别只在「一次只问一段」与「校验不过会回灌具体差异再问」。
    """
    paragraph = paragraphs[index]
    pid = str(paragraph.get("id"))
    start = end = index + 1
    plan = BatchPlan(start=start, end=end, paragraphs=[pid])
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    cached_total = 0
    cached_reported = False
    accounts: list[dict[str, Any]] = []
    feedback = ""
    rounds = max(0, int(validation_retries))

    for round_no in range(rounds + 1):
        params = {
            "paragraphs": [{"id": pid, "text": str(paragraph.get("text") or "")}],
            "start": start,
            "end": end,
            "profile_block": context["profile_block"],
            "concept_block": context["concept_block"],
            "background_sketch": context.get("background_block") or "",
            "illustration_block": context.get("illustration_block") or "",
            "fact_anchor_policy": normalize_fact_anchor_policy(context.get("fact_anchor_policy")),
            "full_text_block": context.get("full_text_block") or "",
            # 第一遍的计划块（plan-v2）：逐段模式同样共享；没开 --plan-first 时零字符插入
            "plan_block": context.get("plan_block") or "",
            "source_id": str(context.get("source_id") or ""),
            "total": int(context.get("total") or end),
            "feedback": feedback,
        }
        try:
            run = tools.run_tool(tools.REWRITE_BATCH_SPEC.name, params, client=client)
        except Exception as exc:  # noqa: BLE001 - 与分批模式同一条降级路径
            plan.error = f"第 {start}–{end} 段：模型调用失败（{type(exc).__name__}）：{exc}"
            plan.degraded.append(plan.error)
            plan.items = [_finalize_items(paragraph, None, degraded=plan.degraded, unplanned=plan.unplanned)]
            plan.usage = _usage_snapshot(totals, cached_total, cached_reported)
            plan.account = _merge_accounts(accounts)
            return plan

        plan.model = run.model or plan.model
        plan.attempts.extend(dict(item) for item in run.attempt_records)
        if isinstance(run.account(), dict) and run.account():
            accounts.append(run.account())
        for field_name in totals:
            value = (run.usage or {}).get(field_name)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[field_name] += value
        cached = cached_tokens_of(run.usage)
        if cached is not None:
            cached_reported = True
            cached_total += cached
        plan.degraded.extend(run.degraded)

        if not run.ok:
            message = run.failure_message()
            plan.error = f"第 {start}–{end} 段：{message}"
            plan.degraded.append(plan.error)
            plan.items = [_finalize_items(paragraph, None, degraded=plan.degraded, unplanned=plan.unplanned)]
            plan.usage = _usage_snapshot(totals, cached_total, cached_reported)
            plan.account = _merge_accounts(accounts)
            return plan

        data = run.data if isinstance(run.data, dict) else {}
        items = data.get("decisions") if isinstance(data.get("decisions"), list) else []
        raw_item = next(
            (item for item in items if isinstance(item, dict) and str(item.get("paragraph")) == pid),
            None,
        )
        problems = _paragraph_problems(paragraph, raw_item, policy=policy)
        if not problems:
            plan.items = [_finalize_items(paragraph, raw_item, degraded=plan.degraded)]
            plan.usage = _usage_snapshot(totals, cached_total, cached_reported)
            plan.account = _merge_accounts(accounts)
            return plan

        if round_no < rounds:
            plan.degraded.append(
                f"{pid}：第 {round_no + 1} 次输出未通过校验，已带反馈重试：" + "；".join(problems)
            )
            feedback = _feedback_text(pid, problems, round_no + 2)
            continue

        # 次数用尽：**不把没通过校验的稿子写进产物**，这一段按保持原文处理并进 unplanned（退出码 6）
        plan.degraded.append(
            f"{pid}：连续 {round_no + 1} 次输出都没通过校验，本段按「保持原文」处理（未改写）："
            + "；".join(problems)
        )
        plan.unplanned.append(pid)
        plan.items = [
            PlanItem(
                paragraph=paragraph,
                decision=Decision(
                    paragraph=pid,
                    action="keep",
                    why={
                        "profile": [],
                        "note": "逐段循环的多次输出都没通过校验（事实锚 / 长度 / 结构），降级为保持原文，未改写",
                    },
                    confidence=0.0,
                ),
            )
        ]
        plan.usage = _usage_snapshot(totals, cached_total, cached_reported)
        plan.account = _merge_accounts(accounts)
        return plan

    # 理论不可达：rounds >= 0 时循环至少进一次并在内部返回
    return plan  # pragma: no cover


def _digest_turn(item: dict[str, Any] | None, *, pid: str = "") -> str:
    """一轮决策 → 逐段对话里的**紧凑摘要**（``--conversation-digest``）。

    形状（一行一段）：``p001 keep`` / ``p002 expand：<改写后的正文>``。
    ``why`` / ``confidence` 这些外壳**一个字符都不进上下文**——它们本来就不影响下一段的判断，
    却会让模型照着上一轮的 JSON 外壳抄、还白烧 prompt token。**原始 JSON 仍用于本轮校验解析**，
    摘要只是「给模型看的上下文」，不参与判定。
    读不通（没给决策 / 形状不对）时给一句可读说明，不静默塞空行。
    """
    if not isinstance(item, dict):
        if pid:
            return f"{pid} 没有给出这一段的决策（按保持原文处理，继续下一段）"
        return "（这一段没有给出决策）"
    paragraph = str(item.get("paragraph") or pid or "")
    action = str(item.get("action") or "")
    text = _clean_rewrite(item.get("rewrite"))
    if action in TEXT_ACTIONS and text:
        return f"{paragraph} {action}：{text}"
    return f"{paragraph} {action or 'keep'}"


def _conversation_turn_text(index: int, paragraphs: list[dict[str, Any]]) -> str:
    """逐段对话里「下一段」的 user 消息（短，只带当前段与输出约束）。"""
    paragraph = paragraphs[index]
    return (
        f"继续。现在处理第 {index + 1} 段（{paragraph.get('id')}）：\n\n"
        f"{format_paragraph_block([paragraph])}\n\n"
        "只输出这一段的 decisions（数组里一个元素）；不要重复已经处理过的段。"
        "请独立判断这一段（不要因为前面选了保持就跟着保持）。"
    )


def _plan_conversation(
    client: ChatClient,
    indices: list[int],
    paragraphs: list[dict[str, Any]],
    context: dict[str, Any],
    *,
    policy: str,
    validation_retries: int,
    digest: bool = False,
) -> list[BatchPlan]:
    """逐段对话（2026-10-18）：**同一条对话**里累积原文与已改内容，每轮只处理当前段。

    与独立逐段的区别：初始 user 消息与 ``rewrite-v7`` 完全相同（全文 / 规则都在），
    之后每轮把上一轮的 assistant 决策与当前段的 user 指令追加进 ``messages``——模型因此能看到
    「前面几段已经怎么改、为什么这么改」。校验不过时把差异追加成新的 user 消息重问（同一段），
    上一版错误答案仍留在上下文里。

    代价：每轮 prompt 都比上一轮长（线性累积），绝对 prompt token 比独立逐段高；缓存命中后
    新增部分才按全价计。串行是硬约束（对话本身有顺序），由调用方保证。

    ``digest=True``（``--conversation-digest``）只改「上一轮的 assistant 消息塞什么」：
    不再塞整段 JSON，改塞 :func:`_digest_turn` 的紧凑摘要。缺省（关）时 transcript 与现状逐字一致。
    """
    from .analyze import extract_json_object  # noqa: PLC0415 - 与 _plan_one_batch 的解析口径同源

    if not indices:
        return []
    retry_policy = (
        client.retry_policy() if callable(getattr(client, "retry_policy", None)) else RetryPolicy()
    )
    first = indices[0]
    initial = build_rewrite_messages(
        str(context.get("source_id") or ""),
        [paragraphs[first]],
        profile_block=context["profile_block"],
        concept_block=context["concept_block"],
        start=first + 1,
        end=first + 1,
        total=int(context.get("total") or len(paragraphs)),
        background_block=context.get("background_block") or "",
        illustration_block=context.get("illustration_block") or "",
        fact_anchor_policy=normalize_fact_anchor_policy(context.get("fact_anchor_policy")),
        full_text_block=context.get("full_text_block") or "",
        # 第一遍的计划块（plan-v2）：对话模式同样在固定上下文里（没开 --plan-first 时零字符插入）
        plan_block=context.get("plan_block") or "",
        feedback=CONVERSATION_START_NOTE,
    )
    transcript: list[dict[str, str]] = [dict(message) for message in initial]
    plans: list[BatchPlan] = []
    rounds = max(0, int(validation_retries))

    for position, index in enumerate(indices):
        paragraph = paragraphs[index]
        pid = str(paragraph.get("id"))
        start = end = index + 1
        plan = BatchPlan(start=start, end=end, paragraphs=[pid])
        if position > 0:
            transcript.append({"role": "user", "content": _conversation_turn_text(index, paragraphs)})
        totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        cached_total = 0
        cached_reported = False
        accounts: list[dict[str, Any]] = []

        for round_no in range(rounds + 1):
            request = LlmRequest(
                tool="rewrite_batch",
                messages=tuple(dict(message) for message in transcript),
                prompt_version=REWRITE_PROMPT_VERSION,
            )
            result = llm_call(request, client=client, policy=retry_policy)
            plan.model = result.model or plan.model
            plan.attempts.extend(attempt_records("rewrite_batch", result.attempts))
            if isinstance(result.usage, dict):
                for field_name in totals:
                    value = result.usage.get(field_name)
                    if isinstance(value, int) and not isinstance(value, bool):
                        totals[field_name] += value
                cached = cached_tokens_of(result.usage)
                if cached is not None:
                    cached_reported = True
                    cached_total += cached
            account = retry_account("rewrite_batch", result.attempts)
            if result.attempts:
                accounts.append(account)
            plan.degraded.extend(result.degraded_params)

            if not result.ok:
                failure = result.failure
                message = failure.message if failure is not None else "模型调用失败（没有给出原因）"
                plan.error = f"第 {start}–{end} 段：{message}"
                plan.degraded.append(plan.error)
                plan.items = [
                    _finalize_items(paragraph, None, degraded=plan.degraded, unplanned=plan.unplanned)
                ]
                transcript.append(
                    {
                        "role": "user",
                        "content": f"{pid} 这一段调用失败，先按保持原文处理并跳过；继续下一段。",
                    }
                )
                break

            raw_text = result.text or ""
            kept, issues = _parse_batch(
                raw_text, [paragraph], start, end, extract_json_object=extract_json_object
            )
            plan.degraded.extend(issues)
            raw_item = kept[0] if kept else None
            # 塞回上下文的是「模型原文」（现状，逐字不变）还是「紧凑摘要」（--conversation-digest）：
            # 解析仍用原文（上面那次 _parse_batch），摘要只影响模型下一轮看到什么。
            transcript.append(
                {
                    "role": "assistant",
                    "content": _digest_turn(raw_item, pid=pid) if digest else raw_text,
                }
            )
            problems = _paragraph_problems(paragraph, raw_item, policy=policy)
            if not problems:
                plan.items = [_finalize_items(paragraph, raw_item, degraded=plan.degraded)]
                break

            if round_no < rounds:
                plan.degraded.append(
                    f"{pid}：第 {round_no + 1} 次输出未通过校验，已带反馈重试：" + "；".join(problems)
                )
                transcript.append(
                    {"role": "user", "content": _feedback_text(pid, problems, round_no + 2)}
                )
                continue

            plan.degraded.append(
                f"{pid}：连续 {round_no + 1} 次输出都没通过校验，本段按「保持原文」处理（未改写）："
                + "；".join(problems)
            )
            plan.unplanned.append(pid)
            plan.items = [
                PlanItem(
                    paragraph=paragraph,
                    decision=Decision(
                        paragraph=pid,
                        action="keep",
                        why={
                            "profile": [],
                            "note": "逐段对话的多次输出都没通过校验（事实锚 / 长度 / 结构），降级为保持原文，未改写",
                        },
                        confidence=0.0,
                    ),
                )
            ]
            transcript.append(
                {"role": "user", "content": f"{pid} 这一段先按保持原文处理并跳过；继续下一段。"}
            )
            break

        plan.usage = _usage_snapshot(totals, cached_total, cached_reported)
        plan.account = _merge_accounts(accounts)
        plans.append(plan)

    return plans


def _plan_paragraphs(
    client: ChatClient,
    indices: list[int],
    paragraphs: list[dict[str, Any]],
    context: dict[str, Any],
    *,
    concurrency: int,
    policy: str,
    validation_retries: int,
) -> list[BatchPlan]:
    """逐段循环的调度：concurrency<=1 串行、否则固定上限的并发（结果按下标对回）。

    串行也走**工作线程**（max_workers=1）：调用链最终是 ``llm.call`` → ``asyncio.run``，
    在已有运行中事件循环的调用方里就地调用会抛 ``RuntimeError``（与 ``_plan_batches_warmup`` 同一条纪律）。
    """
    if not indices:
        return []
    cores = max(1, min(len(indices), max(1, int(concurrency))))
    with concurrent.futures.ThreadPoolExecutor(max_workers=cores) as pool:
        futures = [
            pool.submit(
                _plan_one_paragraph,
                client,
                index,
                paragraphs,
                context,
                policy=policy,
                validation_retries=validation_retries,
            )
            for index in indices
        ]
        return [future.result() for future in futures]


# ------------------------------------------------------------------ 对外接口 --

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
    background_block: str = "",
    fact_anchor_policy: str = DEFAULT_FACT_ANCHOR_POLICY,
    targets: set[str] | None = None,
    per_paragraph: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
    validation_retries: int = DEFAULT_VALIDATION_RETRIES,
    conversation: bool = False,
    plan_block: str = "",
    conversation_digest: bool = False,
) -> tuple[list[Decision], list[str], dict[str, Any]]:
    """逐段决策：``(decisions, degraded, meta)``。

    ``decisions`` **每段一条**（缺的补 keep + 可见降级）；``meta`` 里带 ``usage`` / 请求次数 /
    模型名 / 提示词版本，供回归基线与 token 结算，另有 ``model_errors``（批次失败：
    批次名 / 受影响段 id / 错误）、``unplanned_paragraphs``（模型没给决策的段）与
    ``rewrite_incomplete``——这三样是「有段落没被改写」的机器可读证据（CLI 据此以退出码 6 结束）。

    ``fact_anchor_policy`` 是**本读者生效的事实锚口径**（§14.1）：它既进提示词的「事实锚口径」
    固定块（``rewrite-v5``，模型据此决定能不能丢举例数据），也由 ``run_rewrite`` 记进产物 ``meta``；
    每段的申报（``Decision.fact_declarations``）照旧逐段收。

    ``targets`` 非空时只做**局部重跑**（§6.2）：批次仍按**全篇下标空间**切（``start`` / ``end`` 仍是
    全篇序号、``total`` 仍是全篇段数），每批再与目标段取交集，**空交集不调模型**；
    ``decisions`` 因此只覆盖目标段（上层用 :func:`merge_artifacts` 合进既有产物）。

    ``per_paragraph=True``（``rewrite-v7`` 新增，逐段循环）：每段单独一次请求，
    提示词与分批模式相同（全文在固定上下文里），校验不过时把具体差异回灌到 user 末尾重试
    （``validation_retries`` 次上限）；``concurrency<=1`` 串行、否则并发上限
    ``concurrency``（默认 4）。这一模式只改「怎么调度 / 怎么校验」，产物结构与分批模式一致：
    ``batch_usage`` 每段一条、``usage.requests`` 仍是发出的请求批数（此时 = 段数）。

    ``conversation=True``（与 ``per_paragraph`` 同时用，2026-10-18）：**同一条对话**里累积
    原文与已改内容——第一轮的 user 消息与分批模式逐字相同（v7 全文 / 规则），之后每轮把上一轮的
    assistant 决策与当前段指令追加进 ``messages``；校验差异也追加成新的 user 消息。
    始终串行（``concurrency`` 被强制为 1）。产物结构不变，``meta.planning_mode`` 记为 ``conversation``。
    ``conversation_digest=True``（只与 ``conversation`` 同用）把「上一轮的 assistant 消息」从整段 JSON
    换成一行紧凑摘要（见 :func:`_digest_turn`）；关着时 transcript 与现状**逐字一致**，
    开了才在 ``meta`` 追加 ``conversation_digest: true``。

    ``plan_block``（``plan-v2``，两遍式的第一遍计划）进**三条路径共用的固定上下文**：
    三条路径的提示词里它都排在全文块之后、本次段号之前，空串时零字符插入
    ——所以没开 ``--plan-first`` 时提示词逐字不变。

    ``usage`` 的 3 个旧字段（``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``）口径不变，
    追加 ``cached_tokens``（前缀缓存命中的 prompt token 合计，网关没上报时为 ``null``）；
    ``meta.batch_usage`` 给逐批明细，用来判断预热是否真的生效。

    ⚠ ``usage.requests`` 是**批数**（既有字段语义，不改）；**真实 HTTP 次数**（含重试）在
    ``meta.retry_account.requests`` 里，两者最多差 6 倍（默认 5 次重试）——命令行两个都打（见
    ``_usage_summary``），别把批数当请求数读。
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
        # 读者素描（读前背景问卷）：逐字稳定的固定块，缺省时模板自己写「未提供」文案
        "background_block": background_block,
        "concept_block": _format_concept_block(sid, inventory),
        # 配图候选（rewrite-v4 起进固定块）：没有候选时是**空串**，模板那一格一个字符都不出现
        "illustration_block": format_illustration_block(inventory),
        # 事实锚口径（rewrite-v5 的固定块）：逐字稳定，同一读者重跑必得逐字相同的提示词
        "fact_anchor_policy": normalize_fact_anchor_policy(fact_anchor_policy),
        # 全文（rewrite-v7）：同一材料的所有批共享；拼法与批内段落同一套（[pNNN] 正文）
        "full_text_block": format_paragraph_block(paragraphs),
        # 第一遍的计划块（plan-v2）：三条调度路径共用；没开 --plan-first 时是空串（零字符插入）
        "plan_block": plan_block or "",
    }
    ranges = _batch_ranges(paragraphs, max(1, int(max_chars_per_batch)))
    if targets is not None:
        # 局部重跑（§6.2）：批次在全篇下标空间切好后与目标段**取交集**，空交集整批不发请求。
        # ``start`` / ``end`` 仍然是全篇序号（交集的首末下标），``total`` 仍是全篇段数——
        # 段号不重编，产物里的「第 N–M 段」永远能在全篇里对上。
        wanted = {str(item) for item in targets}
        ranges = [
            [index for index in batch if str(paragraphs[index].get("id")) in wanted] for batch in ranges
        ]
        ranges = [batch for batch in ranges if batch]
    if conversation and not per_paragraph:
        raise InputError("逐段对话（conversation）只能与逐段模式（per_paragraph）一起用")
    if conversation_digest and not conversation:
        # 摘要只改「逐段对话里塞回上下文的东西」：没有对话就没有 transcript 可瘦身（不静默忽略）
        raise InputError("对话上下文瘦身（conversation_digest）只能与逐段对话（conversation）一起用")
    if conversation:
        # 逐段对话：同一条 messages 里累积原文与已改内容，必须串行；整个循环放工作线程
        # （llm.call 是 asyncio.run，不能在已有事件循环的线程里就地调用）
        policy_name = normalize_fact_anchor_policy(fact_anchor_policy)
        conversation_indices = [index for batch in ranges for index in batch]
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            plans = pool.submit(
                _plan_conversation,
                client,
                conversation_indices,
                paragraphs,
                context,
                policy=policy_name,
                validation_retries=max(0, int(validation_retries)),
                digest=bool(conversation_digest),
            ).result()
    elif per_paragraph:
        # 逐段循环：批次下标展开成「一段一批」，并发度由调用方定（1 = 串行）
        policy_name = normalize_fact_anchor_policy(fact_anchor_policy)
        plans = _plan_paragraphs(
            client,
            [index for batch in ranges for index in batch],
            paragraphs,
            context,
            concurrency=max(1, int(concurrency)),
            policy=policy_name,
            validation_retries=max(0, int(validation_retries)),
        )
    else:
        # 先跑完第 1 批做前缀预热，再并发其余批次（见 ``_plan_batches_warmup``）：
        # 所有批次都在工作线程里跑，所以本函数在同步调用方与 async 调用方里行为一致。
        plans = _plan_batches_warmup(client, ranges, paragraphs, context)

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
    attempts: list[dict[str, Any]] = []
    accounts: list[dict[str, Any]] = []
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
        # 逐批的尝试账（这一批第几次尝试失败、等了多久；失败批次也留证）
        batch_entry["attempts"] = [dict(item) for item in plan.attempts]
        batch_usage.append(batch_entry)
        attempts.extend(dict(item) for item in plan.attempts)
        if plan.account:
            accounts.append(plan.account)
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
            INCOMPLETE_DEGRADED_PREFIX
            + f"{len(model_errors)} 个请求失败、{len(unplanned)} 段没有拿到可用决策；"
            "这些段落保持原文（未被改写），重跑 `ar rewrite` 可补齐（退出码 6）"
        )

    meta: dict[str, Any] = {
        "model": model,
        "prompt_version": REWRITE_PROMPT_VERSION,
        # 调度方式（rewrite-v7）：batch = 合并请求；per-paragraph = 一段一请求；
        # conversation = 逐段对话（同一上下文累积，强制串行）
        "planning_mode": (
            "conversation" if conversation else ("per-paragraph" if per_paragraph else "batch")
        ),
        "concurrency": 1 if conversation else (max(1, int(concurrency)) if per_paragraph else 4),
        "validation_retries": max(0, int(validation_retries)) if per_paragraph else 0,
        # 逐段对话的上下文瘦身（--conversation-digest）：**只在开时为 true**——关着时这个键不出现，
        # 默认路径的产物一个键都不多（有测试钉住）
        **({"conversation_digest": True} if conversation_digest and conversation else {}),
        # 批数 / 请求数按**实际发出的计划**数（逐段模式 = 段数；分批模式与 ranges 相同）
        "request_count": len(plans),
        "batch_count": len(plans),
        "usage": usage,
        # 逐批（逐段模式下是逐段）的 token 与缓存命中：证明「预热 / 全文前缀到底有没有生效」的
        # 原始证据。字段名与 ``usage`` 的 3 个旧字段一致，便于对照。
        "batch_usage": batch_usage,
        "model_errors": model_errors,
        "unplanned_paragraphs": unplanned,
        "rewrite_incomplete": bool(model_errors or unplanned),
        # 记账（追加字段）：全部批次的逐次尝试 + 一条汇总（形状与 ``ar.llm.retry_account`` 一致）
        "attempts": attempts,
        "retry_account": _merge_accounts(accounts),
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
        # 图定义只在这里出现（不进 decisions.json、不进 rewritten.md）；失败原因同样可见
        if decision.figure:
            change["figure"] = dict(decision.figure)
        if decision.figure_error:
            change["figure_error"] = decision.figure_error
        # 事实锚申报（§14.1）：**只在模型真的申报了东西时出现**（照 figure / deferred 的
        # 「为真才出现」先例）；它同时是 ar check 按口径放行的依据，也是页面 / CLI 的可见面。
        if decision.fact_declarations:
            change["fact_declarations"] = dict(decision.fact_declarations)
        # 两遍式（``plan-v2``）：第一遍的计划动作与最终动作**不同**时留下计划里那一个
        # （为真才出现；一致时不写，免得每段都多一个永远相同的键）。
        if decision.plan_action and decision.plan_action != decision.action:
            change["plan_action"] = decision.plan_action
        # 兜底补齐（2026-10-14 复核修复）：这一段 why.profile 是骨架照读者自评补的 ⇒ 记一个
        # **结构化**标记（同样「为真才出现」），页面据此挂可见徽标 —— 不许靠正则去猜 why.note 的文案。
        if decision.profile_refs_filled:
            change["profile_refs_filled"] = True
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


def _source_paragraphs(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """``source.meta.json`` 里可用的段落表（没有 id 的条目一律不算）。"""
    return [p for p in (meta.get("paragraphs") or []) if isinstance(p, dict) and p.get("id")]


# --------------------------------------------------------------- 局部重跑（§6） --
@dataclass
class RerunTargets:
    """一次局部重跑的目标段（§6.1）：``reason`` 说明这个段集是怎么算出来的。

    - ``only``：CLI ``--only p012,p014`` 显式给的那几段；
    - ``retry-failed``：既有 ``decisions.json`` 的 ``meta.model_errors[].paragraphs`` ∪
      ``meta.unplanned_paragraphs``（上次因模型问题没被改写的段）；
    - ``from-signals``：行为信号算出来的「受影响段」（``profile.affected_paragraphs`` 反查）。

    ``paragraphs`` 一律按**全篇段序**排列（批次切分、合并与 ``meta.rerun.paragraphs`` 都依赖它）；
    ``empty`` 为真时调用方只打印「没有需要重跑的段」、以 0 退出并**不写盘**。
    """

    reason: str
    paragraphs: list[str] = field(default_factory=list)
    requested: dict[str, Any] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.paragraphs

    def label(self) -> str:
        """人读的参数名（CLI 打印用）。"""
        return f"--{self.reason}"


def resolve_rerun_targets(
    source_id: str,
    data_dir: Path | str,
    profiles_dir: Path | str,
    reader: str,
    *,
    only: str | Sequence[str] | None = None,
    retry_failed: bool = False,
    from_signals: bool = False,
) -> RerunTargets:
    """把 ``--only`` / ``--retry-failed`` / ``--from-signals`` 解析成目标段集（§6.1）。

    **不调模型、不写盘**：``--from-signals`` 折算信号也是纯函数（``profile.yaml`` 一个字节都不动，
    要固化画像得显式跑 ``ar feedback <source_id> --reader <读者> --apply``）。
    解析失败（三个参数同给 / 段 id 非法或不存在 / 需要的产物缺失）一律 ``InputError`` → 退出码 2。

    ``--from-signals`` 的段集口径：读 ``events.jsonl`` → ``events.signals_from_events`` →
    ``profile.applySignals`` → ``profile.affected_paragraphs``。只有 ``update.changed``
    （即**状态真的变了**的概念）所在段才算——没到阈值、只累加 ``score`` / ``evidence``
    的概念不触发任何重跑（改写提示词的可见面也就不会变）。
    """
    given = [
        name
        for name, flag in (("only", only is not None), ("retry_failed", retry_failed), ("from_signals", from_signals))
        if flag
    ]
    if len(given) > 1:
        raise InputError(
            "局部重跑的 --only / --retry-failed / --from-signals 三者互斥，一次只能给一个"
            f"（收到了 {len(given)} 个：{'、'.join('--' + name.replace('_', '-') for name in given)}）"
        )
    if not given:
        raise InputError("至少要给一个目标来源：--only / --retry-failed / --from-signals")
    requested: dict[str, Any] = {
        "only": None,
        "retry_failed": bool(retry_failed),
        "from_signals": bool(from_signals),
    }

    meta = _load_meta(data_dir, source_id)
    order = [str(p.get("id")) for p in _source_paragraphs(meta)]
    if not order:
        raise InputError(f"source.meta.json 里没有任何段落：{source_id} 的材料可能被截断了")
    known = set(order)

    if only is not None:
        raw = [only] if isinstance(only, str) else list(only)
        tokens: list[str] = []
        for item in raw:
            tokens.extend(str(item).split(","))
        wanted: list[str] = []
        unknown: list[str] = []
        for token in tokens:
            pid = token.strip()
            if not pid:
                raise InputError(f"--only 里有空的段 id（收到 {token!r}）：写法形如 --only p012,p014")
            if not re.fullmatch(r"p\d+", pid):
                raise InputError(f"--only 里的 {pid!r} 不是合法的段 id（形如 p012）：写法 --only p012,p014")
            if pid not in known:
                unknown.append(pid)
                continue
            if pid not in wanted:
                wanted.append(pid)
        if unknown:
            raise InputError(
                f"--only 里的段不存在于材料 {source_id}：{'、'.join(sorted(set(unknown)))}"
                f"（现有段 {order[0]} … {order[-1]}，共 {len(order)} 段）"
            )
        requested["only"] = list(wanted)
        return RerunTargets("only", [pid for pid in order if pid in set(wanted)], requested)

    if retry_failed:
        decisions_path = doc_dir(data_dir, source_id) / "decisions.json"
        payload = read_json_if_exists(decisions_path)
        if not isinstance(payload, dict):
            raise InputError(
                f"{decisions_path} 不存在或不是 JSON 对象：--retry-failed 要先有一份整篇改写产物"
                f"（先运行 `ar rewrite {source_id} --reader {reader}`）"
            )
        block = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        wanted_ids: set[str] = set()
        for item in block.get("model_errors") or []:
            if isinstance(item, dict):
                wanted_ids.update(str(pid) for pid in (item.get("paragraphs") or []) if str(pid).strip())
        wanted_ids.update(str(pid) for pid in (block.get("unplanned_paragraphs") or []) if str(pid).strip())
        degraded: list[str] = []
        dropped = sorted(wanted_ids - known)
        if dropped:
            degraded.append(
                "上次的失败清单里有已不在材料里的段（材料重新摄取过？）：" + "、".join(dropped) + "；已跳过"
            )
        return RerunTargets("retry-failed", [pid for pid in order if pid in wanted_ids], requested, degraded)

    # --from-signals
    from .events import read_events, signals_from_events  # noqa: PLC0415 - 同包内避免顶层循环
    from .profile import affected_paragraphs, applySignals, load_profile  # noqa: PLC0415

    target = doc_dir(data_dir, source_id)
    inventory = read_json_if_exists(target / "concepts.json")
    if not isinstance(inventory, dict):
        raise InputError(
            f"{target / 'concepts.json'} 不存在：--from-signals 要用它把概念反查成段 id"
            f"（先运行 `ar analyze {source_id}`）"
        )
    events, event_notes = read_events(profiles_dir, reader)
    signals, signal_notes = signals_from_events(events, inventory, source_id=source_id)
    profile, profile_notes = load_profile(profiles_dir, reader)
    update = applySignals(profile, signals)
    affected = affected_paragraphs(update, inventory)
    degraded = [*event_notes, *signal_notes, *profile_notes, *update.degraded]
    degraded.append(
        f"本次按行为信号反查（只读）：{len(events)} 条事件 → {len(signals)} 条信号 → "
        f"{len(update.changed)} 个概念状态真的变了 → {len(affected)} 段受影响；"
        f"profile.yaml **未写盘**（要固化画像先跑 `ar feedback {source_id} --reader {reader} --apply`）"
    )
    targets = [pid for pid in order if pid in affected]
    if not targets:
        degraded.append("没有任何段受影响：不写盘（先跑 `ar feedback` 看信号折算与阈值结果）")
    return RerunTargets("from-signals", targets, requested, degraded)


# --------------------------------------------------------- 逐段合并器（§6.2） --
@dataclass
class ParagraphUpdate:
    """合并器吃的一段新结果：决策（供计数）+ 变更记录（落 ``changes.json``）+ 正文。"""

    decision: Decision
    change: dict[str, Any]
    text: str


@dataclass
class MergeBase:
    """合并前既有产物的快照（:func:`load_merge_base` 的前置校验通过后才构造）。"""

    source_paragraphs: list[dict[str, Any]]
    decisions_payload: dict[str, Any]
    changes_payload: dict[str, Any]
    blocks: list[str]
    target: Path


@dataclass
class MergeOutcome:
    """一次逐段合并的结果（三份产物的新内容 + 落盘路径）。"""

    decisions: list[dict[str, Any]]
    changes: list[dict[str, Any]]
    blocks: list[str]
    markdown: str
    fidelity: dict[str, Any]
    decisions_meta: dict[str, Any]
    changes_meta: dict[str, Any]
    model_errors: list[dict[str, Any]]
    unplanned: list[str]
    action_counts: dict[str, int]
    deferred_count: int
    rerun: dict[str, Any] | None
    rerun_history: list[dict[str, Any]]
    paths: dict[str, Path]


def load_merge_base(
    source_id: str, data_dir: Path | str, *, meta: dict[str, Any] | None = None
) -> MergeBase:
    """前置校验（§6.2 第 1 条）：三份产物齐备，且逐段账与材料的**段数 / 段序**一一对上。

    不合格一律 ``InputError``（CLI 退出码 2；在线压缩把它映射成 ``no_artifacts`` 的可见降级）：
    **绝不做半合并**——条数或段序对不上时按段号替换会把「第几段」整体挪位，
    比直接报错危险得多。
    """
    doc = meta if isinstance(meta, dict) else _load_meta(data_dir, source_id)
    source_paragraphs = _source_paragraphs(doc)
    if not source_paragraphs:
        raise InputError("source.meta.json 里没有任何段落：无法逐段合并（材料可能被截断了）")
    target = doc_dir(data_dir, source_id)
    decisions_path = target / "decisions.json"
    changes_path = target / "changes.json"
    rewritten_path = target / "rewritten.md"
    missing = [str(path) for path in (decisions_path, changes_path, rewritten_path) if not path.is_file()]
    if missing:
        raise InputError(
            "局部重跑 / 在线压缩需要既有的三份产物（decisions.json + changes.json + rewritten.md），"
            f"现在缺：{'、'.join(missing)}；先整篇运行 `ar rewrite {source_id} --reader <读者>`"
        )
    decisions_payload = read_json(decisions_path)
    changes_payload = read_json(changes_path)
    if not isinstance(decisions_payload, dict) or not isinstance(changes_payload, dict):
        raise InputError("decisions.json / changes.json 的根节点不是 JSON 对象：产物不可读，先整篇重跑")
    decisions = decisions_payload.get("decisions")
    changes = changes_payload.get("changes")
    if not isinstance(decisions, list) or not isinstance(changes, list):
        raise InputError("decisions.json 的 decisions / changes.json 的 changes 不是数组：先整篇重跑")
    order = [str(p.get("id")) for p in source_paragraphs]
    for name, records in (("decisions.json 的 decisions", decisions), ("changes.json 的 changes", changes)):
        if len(records) != len(order):
            raise InputError(
                f"{name} 有 {len(records)} 条，而 source.meta.json 有 {len(order)} 段：逐段账与材料对不上"
                f"（产物被截断，或与当前材料不是同一次改写）——先整篇运行 `ar rewrite {source_id}`，"
                "不许半合并"
            )
        got = [str(item.get("paragraph")) if isinstance(item, dict) else "" for item in records]
        if got != order:
            raise InputError(
                f"{name} 的段序与材料不一致（前几段 {got[:3]} vs {order[:3]}）："
                f"先整篇运行 `ar rewrite {source_id}`，不许半合并"
            )
    blocks = split_rewritten_blocks(rewritten_path.read_text(encoding="utf-8"))
    if len(blocks) != len(order):
        raise InputError(
            f"rewritten.md 有 {len(blocks)} 段，而 source.meta.json 有 {len(order)} 段：逐段对位不成立，"
            f"先整篇运行 `ar rewrite {source_id}`，不许半合并"
        )
    return MergeBase(
        source_paragraphs=source_paragraphs,
        decisions_payload=decisions_payload,
        changes_payload=changes_payload,
        blocks=blocks,
        target=target,
    )


def _fidelity_block(
    source_paragraphs: list[dict[str, Any]],
    blocks: list[str],
    action_by_id: dict[str, str],
    *,
    degraded: list[str] | None = None,
    policy: str = DEFAULT_FACT_ANCHOR_POLICY,
) -> dict[str, Any]:
    """``changes.json`` 的 ``fidelity``（**未校验**口径）：段数 / 段序对位 + 「保持」段与原文逐字节一致。

    ``checked`` 恒为 ``false``、``counts`` 恒为 ``null``：逐段事实锚比对由 ``ar check`` 执行并回写。
    ``rewrite`` 与**任何一次合并之后**都必须重置成这个形态（绝不许把上次的 ``checked: true`` 带过来）。
    ``fact_anchor_policy`` 记**改写时**生效的口径（§14.1）；``fidelity.waived`` 留空——
    放行条目是 ``ar check`` 按口径比对之后才有的，改写阶段一条都不许预写。
    ``fidelity.over_declared`` 同理留空（「申报与实测对不上」也是 ``ar check`` 才算得出的）。
    ``fact_declarations``（事实锚申报，仅当非空时出现）：
    ``{"numbers": [...], "quotes": [...], "books": [...], "note": "..."}``，
    ``ar check`` 据此按读者口径放行已申报的缺失（§14.1）；没有申报时这一类键一个都不出现。
    ``over_declared`` 与 ``waived`` 一样**由 ``ar check`` 填**（改写 / 压缩阶段一条都不许预写）。
    """
    count_mismatch: list[str] = []
    if len(blocks) != len(source_paragraphs):
        count_mismatch.append(f"改写稿有 {len(blocks)} 段，原文有 {len(source_paragraphs)} 段")
    keep_mismatch: list[str] = []
    for paragraph, block in zip(source_paragraphs, blocks):
        pid = str(paragraph.get("id"))
        if action_by_id.get(pid) in TEXT_ACTIONS:
            # expand / compress 段本来就会变，事实锚由 ar check 逐段比对。
            # **compress 必须在这个名单里**：否则拿原文比压缩稿，必然误报 keep_mismatch。
            continue
        if text_hash(str(paragraph.get("text") or "")) != text_hash(block):
            keep_mismatch.append(pid)
    if keep_mismatch and degraded is not None:
        degraded.append(
            "以下「保持」段落的正文与原文不一致（原文锚点 text_hash 对不上）：" + "、".join(keep_mismatch)
        )
    return {
        "checked": False,
        "checked_at": None,
        "mismatches": [],
        "counts": None,
        "fact_anchor_policy": normalize_fact_anchor_policy(policy),
        "waived": [],
        # 「申报与实测对不上」（锚点一个没少却申报放弃了它）：只有 ar check 才判得出，这里留空
        "over_declared": [],
        "summary": "尚未校验：逐段事实锚比对由 `ar check <source_id>` 执行并回写本字段",
        "paragraphs_match_source": not count_mismatch and not keep_mismatch,
        "count_mismatch": count_mismatch,
        "keep_paragraphs_mismatch": keep_mismatch,
        "note": (
            "facts 与 fidelity 的逐段事实锚比对结果由 `ar check <source_id>` 复核"
            "（含数字 / 引文 / 书名号；已申报的缺失按读者口径可能放行，见 fact_anchor_policy / waived）"
        ),
    }


def merge_artifacts(
    source_id: str,
    data_dir: Path | str,
    *,
    updates: dict[str, ParagraphUpdate],
    run_meta: dict[str, Any],
    degraded: list[str] | None = None,
    shared_meta: dict[str, Any] | None = None,
    rerun: dict[str, Any] | None = None,
    base: MergeBase | None = None,
    at: str | None = None,
    write: bool = True,
) -> MergeOutcome:
    """把若干段的**新结果**逐段合进既有三份产物（§6.2 的合并口径；CLI 与在线压缩共用）。

    - ``updates``：``{段 id: ParagraphUpdate}``，每一段都必须在材料里（否则 ``InputError``）；
    - ``run_meta``：**本次**调用的账（``model`` / ``usage`` / ``batch_usage`` / ``attempts`` /
      ``retry_account`` / ``request_count`` / ``batch_count`` / ``model_errors`` /
      ``unplanned_paragraphs``），原样落进合并后的 ``meta``；
    - ``shared_meta``：读者 / 档位 / 画像那几栏；缺省沿用既有产物的值（在线压缩不动这几栏）；
    - ``rerun``：``meta.rerun`` 的内容（``None`` = 不追加这一栏）。

    合并口径逐条对应 §6.2：未更新段**逐字节保留**（含未重跑段的正文）；段序恒等于材料段序；
    ``fidelity`` **重置为未校验**；``action_counts`` / ``deferred_count`` 按合并后的全量记录重算；
    账记**本次**；``model_errors`` / ``unplanned_paragraphs`` / ``rewrite_incomplete`` 按合并后的
    真实状态重算（目标段的旧条目先清掉、非目标段的历史条目保留、与目标段部分重叠的历史条目
    拆成「仍失败的段」、空条目删除）。退出码 6 只反映合并后的真实状态——补齐后应当回到 0。
    """
    base = base if base is not None else load_merge_base(source_id, data_dir)
    source_paragraphs = base.source_paragraphs
    order = [str(paragraph.get("id")) for paragraph in source_paragraphs]
    unknown = sorted(pid for pid in updates if pid not in set(order))
    if unknown:
        raise InputError(f"要合并的段不在材料 {source_id} 里：{'、'.join(unknown)}")
    old_decisions = list(base.decisions_payload.get("decisions") or [])
    old_changes = list(base.changes_payload.get("changes") or [])
    old_meta_raw = base.decisions_payload.get("meta")
    old_meta: dict[str, Any] = old_meta_raw if isinstance(old_meta_raw, dict) else {}

    merged_decisions: list[dict[str, Any]] = []
    merged_changes: list[dict[str, Any]] = []
    merged_blocks: list[str] = []
    action_by_id: dict[str, str] = {}
    for index, paragraph in enumerate(source_paragraphs):
        pid = str(paragraph.get("id"))
        update = updates.get(pid)
        if update is None:
            old_decision = old_decisions[index] if isinstance(old_decisions[index], dict) else {}
            merged_decisions.append(dict(old_decision))
            merged_changes.append(dict(old_changes[index]))
            merged_blocks.append(base.blocks[index])
            action_by_id[pid] = str(old_decision.get("action") or "")
        else:
            merged_decisions.append(update.decision.to_record())
            merged_changes.append(dict(update.change))
            merged_blocks.append(update.text)
            action_by_id[pid] = update.decision.action
    markdown = "\n\n".join(merged_blocks) + "\n"

    targets = set(updates)
    merged_degraded = [str(item) for item in (degraded or [])]
    # 旧产物的降级说明里，**仍然成立**的那些继续留着（降级必须可见，不能因为一次局部重跑就整批丢）：
    # 丢掉两类 —— ①「有段落没被改写」那条 run 级句子（可能已经不成立，状态由 rewrite_incomplete /
    # model_errors 字段重算）；② 以 ``<目标段>：`` 开头的逐段条目（那一段已被本次结果替换）。
    for item in old_meta.get("degraded") or []:
        text = str(item)
        if not text.strip() or text in merged_degraded or text.startswith(INCOMPLETE_DEGRADED_PREFIX):
            continue
        if any(text.startswith(f"{pid}：") for pid in targets):
            continue
        merged_degraded.append(text)
    # 事实锚口径（§14.1）：本次合并后产物该记哪一档——调用方（run_rewrite）给的那一档优先，
    # 在线压缩那一路不传 shared_meta，沿用既有产物记的口径（读坏 / 没记一律退到严口径）
    policy = normalize_fact_anchor_policy(
        (shared_meta or {}).get("fact_anchor_policy") or old_meta.get("fact_anchor_policy")
    )
    fidelity = _fidelity_block(
        source_paragraphs, merged_blocks, action_by_id, degraded=merged_degraded, policy=policy
    )

    counts = {action: 0 for action in ACTIONS}
    for record in merged_decisions:
        action = str(record.get("action") or "")
        counts[action] = counts.get(action, 0) + 1
    deferred_count = sum(1 for record in merged_changes if record.get("deferred") is True)

    run_errors = [dict(item) for item in (run_meta.get("model_errors") or []) if isinstance(item, dict)]
    run_unplanned = [str(pid) for pid in (run_meta.get("unplanned_paragraphs") or []) if str(pid).strip()]
    model_errors: list[dict[str, Any]] = []
    for item in old_meta.get("model_errors") or []:
        if not isinstance(item, dict):
            continue
        remaining = [
            str(pid) for pid in (item.get("paragraphs") or []) if str(pid).strip() and str(pid) not in targets
        ]
        if not remaining:
            continue  # 空条目删除：这些段这次都重跑过了
        row = dict(item)
        row["paragraphs"] = remaining
        model_errors.append(row)
    model_errors.extend(run_errors)
    unplanned = [
        str(pid)
        for pid in (old_meta.get("unplanned_paragraphs") or [])
        if str(pid).strip() and str(pid) not in targets
    ]
    for pid in run_unplanned:
        if pid not in unplanned:
            unplanned.append(pid)
    # 一律按全篇段序排列（旧条目 + 本次条目混在一起，靠顺序才对得上「第几段」）
    position = {pid: index for index, pid in enumerate(order)}
    unplanned.sort(key=lambda pid: (position.get(pid, len(order)), pid))

    usage = dict(run_meta.get("usage") or {})
    batch_usage = [dict(item) for item in (run_meta.get("batch_usage") or []) if isinstance(item, dict)]
    attempts = [dict(item) for item in (run_meta.get("attempts") or []) if isinstance(item, dict)]
    account = run_meta.get("retry_account")
    retry_account = dict(account) if isinstance(account, dict) else _merge_accounts([])
    model = run_meta.get("model") or old_meta.get("model")
    generated_at = at or now_iso()

    shared = (
        dict(shared_meta)
        if shared_meta is not None
        else {
            "reader": old_meta.get("reader"),
            "mode": old_meta.get("mode"),
            "profile_used": bool(old_meta.get("profile_used")),
            "profile_note": str(old_meta.get("profile_note") or ""),
            "background_used": bool(old_meta.get("background_used")),
            "background_fingerprint": str(old_meta.get("background_fingerprint") or ""),
            # 事实锚口径（§14.1）：在线压缩那一路不传 shared_meta，沿用既有产物的取值
            "fact_anchor_policy": policy,
        }
    )
    history = [dict(item) for item in (old_meta.get("rerun_history") or []) if isinstance(item, dict)]
    rerun_block: dict[str, Any] | None = None
    if rerun is not None:
        rerun_block = {
            "at": str(rerun.get("at") or generated_at),
            "reason": str(rerun.get("reason") or ""),
            "requested": dict(rerun.get("requested") or {}),
            "paragraphs": [str(pid) for pid in (rerun.get("paragraphs") or [])],
            "batches": int(rerun.get("batches") or 0),
            "previous_generated_at": old_meta.get("generated_at"),
        }
        history.append({**rerun_block, "usage": usage, "retry_account": retry_account})

    head: dict[str, Any] = {
        "source_id": source_id,
        "reader": shared.get("reader"),
        "mode": shared.get("mode"),
        "model": model,
        "prompt_version": REWRITE_PROMPT_VERSION,
        "generated_at": generated_at,
    }
    profile_fields: dict[str, Any] = {
        "profile_used": bool(shared.get("profile_used")),
        "profile_note": str(shared.get("profile_note") or ""),
        "background_used": bool(shared.get("background_used")),
        "background_fingerprint": str(shared.get("background_fingerprint") or ""),
        # 事实锚口径（§14.1）：**改写时**生效的那一档；放行条目（fidelity.waived）由 ar check 写
        "fact_anchor_policy": policy,
    }
    # 调度方式与并发（rewrite-v7）：合并器重建 meta 时**不许丢**（既有缺陷，2026-10-19 复核补）——
    # 丢掉之后任何一次局部重跑都让产物看不出「这一篇是逐段 / 逐段对话跑出来的」，实验产物没法判读。
    # 本次给了就写本次的（run_meta 里恒有这三格），没给（在线压缩那一路）沿用旧产物。
    schedule_meta = run_meta if run_meta.get("planning_mode") else old_meta
    schedule_fields: dict[str, Any] = {
        "planning_mode": str(schedule_meta.get("planning_mode") or "batch"),
        "concurrency": int(schedule_meta.get("concurrency") or DEFAULT_CONCURRENCY),
        "validation_retries": int(schedule_meta.get("validation_retries") or 0),
    }
    # 两个开关的可见面（两遍式 plan-v2 / 对话瘦身），全是**追加字段**：
    # 本次给了就写本次的；没给（例如不带 --plan-first 的局部重跑）就**沿用旧产物里那一份**
    # ——否则一次局部重跑就把「这一篇是按计划跑出来的」连同计划整体丢掉（合并器只重算它认识的键）。
    mode_fields: dict[str, Any] = {}
    if bool(run_meta.get("plan_first")) or bool(old_meta.get("plan_first")):
        source = run_meta if bool(run_meta.get("plan_first")) else old_meta
        mode_fields = {
            "plan_first": True,
            "plan": [dict(item) for item in (source.get("plan") or []) if isinstance(item, dict)],
            "plan_missing": [str(pid) for pid in (source.get("plan_missing") or []) if str(pid).strip()],
            "plan_usage": dict(source.get("plan_usage") or {}),
            "plan_request_count": int(source.get("plan_request_count") or 0),
            "plan_attempts": [
                dict(item) for item in (source.get("plan_attempts") or []) if isinstance(item, dict)
            ],
            "plan_retry_account": dict(source.get("plan_retry_account") or {}),
            "plan_prompt_version": str(source.get("plan_prompt_version") or PLAN_PROMPT_VERSION),
        }
        # 这一轮的计划复用账（不带 --plan-first 的局部重跑同样要留痕，2026-10-19 复核修复）：
        # plan_reused 恒写 true / false（问过这个问题就要答）；plan_not_applied 只在有点名时落产物
        # ——但 run_meta 里**恒有键**（空列表也算），靠「键在不在」区分「这一轮查过、是干净的」与
        # 「这一轮没查」（在线压缩那一路），后者才沿用旧痕迹：旧值不许被无声抹掉。
        if "plan_reused" in run_meta:
            mode_fields["plan_reused"] = bool(run_meta.get("plan_reused"))
        elif old_meta.get("plan_reused") is True:
            mode_fields["plan_reused"] = True
        if "plan_not_applied" in run_meta:
            not_applied = [
                str(pid) for pid in (run_meta.get("plan_not_applied") or []) if str(pid).strip()
            ]
            if not_applied:
                mode_fields["plan_not_applied"] = not_applied
        else:
            old_not_applied = [
                str(pid) for pid in (old_meta.get("plan_not_applied") or []) if str(pid).strip()
            ]
            if old_not_applied:
                mode_fields["plan_not_applied"] = old_not_applied
    # 对话瘦身：**只在开时为 true**（关着时这个键不出现——默认路径的产物一个键都不多）
    if bool(run_meta.get("conversation_digest") or old_meta.get("conversation_digest")):
        mode_fields["conversation_digest"] = True
    accounting: dict[str, Any] = {
        "request_count": int(run_meta.get("request_count") or 0),
        "usage": usage,
        "batch_usage": batch_usage,
        "model_errors": model_errors,
        "unplanned_paragraphs": unplanned,
        "rewrite_incomplete": bool(model_errors or unplanned),
        "degraded": merged_degraded,
        "attempts": attempts,
        "retry_account": retry_account,
    }
    decisions_meta: dict[str, Any] = {
        **head,
        "paragraph_count": len(source_paragraphs),
        "action_counts": counts,
        "implemented_actions": list(IMPLEMENTED_ACTIONS),
        "deferred_count": deferred_count,
        **profile_fields,
        **accounting,
        **schedule_fields,
        **mode_fields,
    }
    changes_meta: dict[str, Any] = {
        **head,
        # 与 decisions.json 的 meta 对齐（2026-10-13 复核修复 F5）：整篇与局部重跑两条路径都写
        "paragraph_count": len(source_paragraphs),
        "action_counts": counts,
        "deferred_count": deferred_count,
        "facts_note": "facts 只含数字与引号引文（与前端夹具同口径）；书名号《》由 `ar check` 另行比对",
        **profile_fields,
        **accounting,
        **schedule_fields,
        **mode_fields,
    }
    if rerun_block is not None:
        decisions_meta["rerun"] = rerun_block
        decisions_meta["rerun_history"] = history
        changes_meta["rerun"] = rerun_block
        changes_meta["rerun_history"] = history

    paths = {
        "decisions": base.target / "decisions.json",
        "changes": base.target / "changes.json",
        "rewritten": base.target / "rewritten.md",
    }
    if write:
        write_json(paths["decisions"], {"decisions": merged_decisions, "meta": decisions_meta})
        write_json(paths["changes"], {"changes": merged_changes, "fidelity": fidelity, "meta": changes_meta})
        write_text(paths["rewritten"], markdown)
    return MergeOutcome(
        decisions=merged_decisions,
        changes=merged_changes,
        blocks=merged_blocks,
        markdown=markdown,
        fidelity=fidelity,
        decisions_meta=decisions_meta,
        changes_meta=changes_meta,
        model_errors=model_errors,
        unplanned=unplanned,
        action_counts=counts,
        deferred_count=deferred_count,
        rerun=rerun_block,
        rerun_history=history,
        paths=paths,
    )


def _usage_summary(
    usage: dict[str, Any], retry_account: dict[str, Any] | None = None, *, unit: str = "批"
) -> str:
    """人读的一行 token 账（含**真实请求数**与前缀缓存命中量；网关没上报就写「未上报」，不编造 0）。

    ⚠ 两个「请求数」不是一回事，命令行上必须分得开（2026-10-13 复核修复 P1b）：

    - ``usage.requests`` 是**批数**（既有字段语义，一个批次一次首发的意思，不许改）；
    - ``retry_account["requests"]`` 才是**真实 HTTP 次数**（含每次重试，默认最多 6 倍）。
      迁移前只打了批数，重试花掉的请求在命令行上完全看不见。
    """
    batches = int(usage.get("requests") or 0)
    account = retry_account if isinstance(retry_account, dict) else {}
    total = int(account.get("requests") or 0)
    retries = int(account.get("retries") or 0)
    if total > 0:
        head = f"请求 {total} 次（{batches} {unit}；其中重试 {retries} 次）"
    else:
        # 没有逐次记账（例如外部手工拼的 usage）：不敢把批数当请求数报，明说「不可知」
        head = f"请求数未记账（{batches} {unit}）"
    line = (
        f"{head}；"
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
    - ``model_errors`` / ``unplanned_paragraphs``：改写阶段就有段落没被改写（退出码 6）；
    - ``policy`` / ``policy_source``：本次**实际采用**的事实锚口径与它的来源（§14.1，CLI 打印）；
      产物 ``meta.fact_anchor_policy`` 与它不一致时，``policy_note`` 里有一条可见说明；
    - ``waived``：已申报并被口径放行的缺失（落 ``fidelity.waived``，CLI 逐条打印）；
    - ``over_declared``：**申报与实测对不上**的条目（锚点一个没少却申报放弃了它；落
      ``fidelity.over_declared``，CLI 逐条打印）——**不影响退出码与放行结论**（本来就没有缺失可放行）；
    - ``declaration_notes``：申报本身读不通的地方（记进 CLI 的降级输出，不静默）。
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
    #: 本次实际采用的事实锚口径（§14.1）与它的来源说明
    policy: str = DEFAULT_FACT_ANCHOR_POLICY
    policy_source: str = ""
    #: 产物记的口径与本次采用的口径不一致时的可见说明（``None`` = 一致 / 产物没记）
    policy_note: str | None = None
    #: 已申报并被口径放行的缺失（``fidelity.waived`` 的内容）
    waived: list[dict[str, Any]] = field(default_factory=list)
    #: 申报与实测对不上的条目（``fidelity.over_declared`` 的内容；不影响退出码与放行结论）
    over_declared: list[dict[str, Any]] = field(default_factory=list)
    #: 申报本身读不通的地方（结构不对 / 值不是字符串 / 不认识的键）
    declaration_notes: list[str] = field(default_factory=list)


def _policy_for_check(
    recorded: Any,
    profiles_dir: Path | str | None,
    reader: str | None,
    recorded_reader: Any = None,
) -> tuple[str, str, str | None]:
    """解析 ``ar check`` 该用哪一档事实锚口径（§14.1）：返回 ``(口径, 来源, 可见说明)``。

    两条路，**活的读者画像优先**：

    1. 给了 ``profiles_dir``（CLI 就是这条）⇒ 按 ``profiles/<reader>`` 画像里那条偏好
       ``pref_fact_anchor`` 判；``reader`` 缺省时用产物 ``meta.reader``，再缺省用默认读者。
       产物记的口径与它不一致时给一条可见说明（口径是「现在的标准」，不是「当时的标准」）。
    2. 没给 ``profiles_dir``（老调用方 / 单测直调）⇒ 用产物自己记的 ``meta.fact_anchor_policy``，
       没记就按严口径——**与改动前的行为逐字一致**（那时没有申报机制，一律严格比对）。
    """
    from .profile import DEFAULT_READER, load_profile, resolve_fact_anchor_policy

    recorded_text = recorded.strip() if isinstance(recorded, str) else ""
    recorded_policy = normalize_fact_anchor_policy(recorded)
    if profiles_dir is None:
        if recorded_text in FACT_ANCHOR_POLICIES:
            return (
                recorded_policy,
                f"产物 meta.fact_anchor_policy = {recorded_policy}（没给画像目录，按产物记的口径）",
                None,
            )
        return (
            DEFAULT_FACT_ANCHOR_POLICY,
            f"产物没记口径、也没给画像目录：按默认 {DEFAULT_FACT_ANCHOR_POLICY}（严口径）办",
            None,
        )
    effective_reader = (
        (reader or "").strip()
        or (recorded_reader.strip() if isinstance(recorded_reader, str) else "")
        or DEFAULT_READER
    )
    profile, notes = load_profile(profiles_dir, effective_reader)
    resolved = resolve_fact_anchor_policy(profile)
    note = notes[0] if notes else None
    if resolved.warning:
        note = f"{note}；{resolved.warning}" if note else resolved.warning
    if recorded_text in FACT_ANCHOR_POLICIES and recorded_policy != resolved.policy:
        mismatch = (
            f"产物是按 {recorded_policy} 改写的，本次按读者 {effective_reader} 当前的口径 "
            f"{resolved.policy} 校验（口径看的是「现在的标准」；要一致就重跑 `ar rewrite`）"
        )
        note = f"{note}；{mismatch}" if note else mismatch
    return resolved.policy, f"profile:{effective_reader}", note


def check_fidelity(
    source_id: str,
    data_dir: Path | str,
    *,
    profiles_dir: Path | str | None = None,
    reader: str | None = None,
    at: str | None = None,
    write_back: bool = True,
) -> CheckOutcome:
    """事实锚校验：读 ``rewritten.md`` 与 ``source.meta.json`` 逐段比对三类锚点。

    - 段缺失 / 段数对不上 → 点名段 id；
    - 数字 / 引号引文 / 书名号不一致 → 点名段 id 与具体差异；
    - **已申报的缺失按读者口径放行**（§14.1）：口径 = ``profiles/<reader>/profile.yaml`` 里
      ``pref_fact_anchor`` 那条偏好（``keep_all`` 严 / ``allow_declared_drops`` 松），
      申报在 ``changes[i].fact_declarations`` 里；放行的条目记进 ``fidelity.waived`` 并逐条打印。
      没申报的缺失、多出来的事实锚、顺序调换**任何口径下都算违规**；
    - **申报与实测对不上**（这一段根本没丢却申报放弃了某项）：放行结论与退出码**都不变**，
      只逐条记进 ``fidelity.over_declared`` 并由 CLI 打印（绝不静默）；
    - **0 比对项 = 检查器空跑**（空文档）：照样算失败，``checked`` 保持 false；
    - ``changes.json`` 缺失 / 不是对象 / ``changes`` 不是数组、为空、或条数与材料段数不一致
      → 记进 ``problems``（调用方按输入错误处理，退出码 2）：缺了逐段变更记录就没法追责，
      不能静默通过；
    - 结果写回 ``changes.json`` 的 ``fidelity``（``mismatches`` + ``counts`` + ``checked`` +
      ``fact_anchor_policy`` + ``waived``），失败时也写——「校验跑过、结果是坏的」要留痕。
      ``checked`` 只有在**本次比对项 > 0 且没有任何不一致**时才为 ``true``。

    ``profiles_dir`` 给了就按**活的读者画像**判口径（CLI 是这条），没给就用产物自己记的口径
    （``meta.fact_anchor_policy``；没记 → 严口径）：老调用方的行为与改动前逐字一致。
    返回的 ``counts`` 供 CLI 打印；问题清单由调用方分别交给 ``InputError``（2）/
    ``ValidationFailed``（4）/ ``ModelPartiallyFailed``（6）。
    """
    from .fidelity import compare_blocks_detailed, render_fact_report  # noqa: PLC0415 - 同包内避免顶层循环

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

    artifact_problems: list[str] = []
    model_errors: list[dict[str, Any]] = []
    unplanned: list[str] = []
    declarations: dict[str, Any] = {}
    changes = read_json_if_exists(changes_path)
    meta_block: dict[str, Any] = {}
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
        else:
            # 逐段申报（§14.1）：只在产物结构可信时取用（条数与段数对得上）
            for record in records:
                if not isinstance(record, dict):
                    continue
                pid = record.get("paragraph")
                raw = record.get("fact_declarations")
                if isinstance(pid, str) and raw:
                    declarations[pid] = raw
        meta_block = changes.get("meta")
        meta_block = meta_block if isinstance(meta_block, dict) else {}
        model_errors = [item for item in (meta_block.get("model_errors") or []) if isinstance(item, dict)]
        unplanned = [str(pid) for pid in (meta_block.get("unplanned_paragraphs") or []) if str(pid).strip()]

    resolved, policy_source, policy_note = _policy_for_check(
        meta_block.get("fact_anchor_policy"), profiles_dir, reader, meta_block.get("reader")
    )
    comparison = compare_blocks_detailed(
        source_paragraphs, blocks, declarations=declarations, policy=resolved
    )
    fact_problems = comparison.problems
    mismatches = comparison.mismatches
    counts = comparison.counts
    waived = comparison.waived
    over_declared = comparison.over_declared
    declaration_notes = comparison.declaration_notes

    if isinstance(changes, dict):
        fidelity = changes.get("fidelity")
        fidelity = fidelity if isinstance(fidelity, dict) else {}
        # checked 的语义 = 「本次逐段事实锚比对跑过、且没有任何不一致」；
        # 0 比对项（空文档）与任何 mismatch 都不许写成「已查过、干净」。
        # 已申报放行的条目不算 mismatch（它们进 waived），所以松口径下确实能到 checked: true。
        checked = bool(counts.get("compared", 0)) and not mismatches
        fidelity.update(
            {
                "checked": checked,
                "checked_at": at or now_iso(),
                "mismatches": mismatches,
                "counts": counts,
                # 本次**实际采用**的口径与放行条目（改写阶段写的 fact_anchor_policy 在这里被覆盖成
                # 「这次比对用的那一档」，两边不一致时 policy_note 说明了原因）
                "fact_anchor_policy": resolved,
                "waived": waived,
                # 申报与实测对不上（锚点一个没少却申报放弃了某项）：只作可见说明，不改放行结论
                "over_declared": over_declared,
                "summary": render_fact_report(counts),
                "checked_by": (
                    "ar check（数字 / 引号引文 / 书名号《》三类 + 段缺失；"
                    f"已申报的缺失按 {resolved} 口径处理）"
                ),
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
        policy=resolved,
        policy_source=policy_source,
        policy_note=policy_note,
        waived=waived,
        over_declared=over_declared,
        declaration_notes=declaration_notes,
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
        lines.append("没有拿到可用决策的段：" + "、".join(outcome.unplanned_paragraphs))
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
    targets: RerunTargets | None = None,
    per_paragraph: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
    validation_retries: int = DEFAULT_VALIDATION_RETRIES,
    conversation: bool = False,
    plan_first: bool = False,
    conversation_digest: bool = False,
) -> RewriteOutcome:
    """``ar rewrite`` 的实现：决策 → 改写 → 写三份产物，并把降级全部记下来。

    ``targets`` 为 ``None``（缺省）= 今天的整篇行为，**逐字不变**；给了 :class:`RerunTargets`
    就走局部重跑（§6.2）：批次按全篇下标空间切、每批与目标段取交集（空交集不调模型），
    结果由 :func:`merge_artifacts` 逐段合进既有三份产物（前置校验不过 → ``InputError``，
    **在发任何模型请求之前**就报错）。

    ``plan_first=True``（``--plan-first``，两遍式）：先跑一遍 :func:`plan_actions` 给全篇每段打动作标，
    再把它作为固定上下文（``{plan_section}``）进第二遍的原有模式。与 ``targets`` 同时给时
    **旧产物里已有计划就复用**（:func:`_reuse_plan`，0 次打标请求）：读者 / 背景指纹 / 事实锚口径
    任一对不上则**不复用**，重新全篇打标（多一次请求），并把 ``meta.plan_reused`` 记成 ``false``。
    ``plan_first=False`` 但给了 ``targets`` 时：旧产物里**有计划就照旧注入**那一段（同一份产物始终
    按同一份计划执行）；旧计划里没有这一段的条目 / 口径对不上 ⇒ 目标段进 ``meta.plan_not_applied``
    并在命令行点名——不许静默降级成单遍改写。
    ``conversation_digest=True``（``--conversation-digest``，只与 ``conversation`` 同用）把逐段对话里
    塞回上下文的「上一轮整段 JSON」换成一行紧凑摘要。
    两个开关**互不冲突**（可以同时开），也都可以与分批 / 逐段 / 逐段对话任意组合；
    都关着时这一次调用的请求数、提示词与产物键集与改动前**逐字相同**。
    """
    if mode not in MODES:
        raise InputError(f"mode 非法：{mode!r}（只能是 {' / '.join(MODES)}）")
    if mode == "strict":
        raise NotImplementedYet(STRICT_NOT_IMPLEMENTED)
    # 参数合法性必须**早于任何模型请求**（含两遍式的第一遍）：先花钱再报退出码 2 是最贵的错误。
    # 这条校验原先只在 planDecisions 里，``--plan-first --conversation-digest``（缺 conversation）时
    # 第一遍打标的钱已经花掉了才报错（2026-10-19 复核修复）。
    if conversation_digest and not (per_paragraph and conversation):
        raise InputError(
            "对话上下文瘦身（conversation_digest）必须与逐段对话同用（per_paragraph=True 且 "
            "conversation=True）：没有逐段对话就没有 transcript 可瘦身；本次一个请求都没发"
        )

    from .profile import load_profile, resolve_fact_anchor_policy

    meta = _load_meta(data_dir, source_id)
    target = doc_dir(data_dir, source_id)
    # 局部重跑的前置校验要**早于**任何模型调用（§6.2 第 1 条：不许半合并、不许白花钱）
    base = load_merge_base(source_id, data_dir, meta=meta) if targets is not None else None
    inventory = read_json_if_exists(target / "concepts.json")
    inventory = inventory if isinstance(inventory, dict) else None
    profile, profile_notes = load_profile(profiles_dir, reader)

    degraded: list[str] = list(profile_notes)
    # 事实锚口径（§14.1）：**读者画像里那条偏好**说了算（不是全局开关、也不是一次性 CLI flag）。
    # 它进提示词的固定块（模型据此决定能不能丢举例数据），也记进两份产物的 meta；
    # 偏好缺省 / 写坏一律退到严口径，并在这里给一条可见降级（不静默放宽）。
    policy = resolve_fact_anchor_policy(profile)
    if policy.warning:
        degraded.append(policy.warning)
    # 素描只由 background 决定，**逐字稳定**（同画像必得同文本）——它是提示词固定块的一部分
    background_block = render_sketch(profile.get("background"))
    background_used = has_background(profile)
    background_fingerprint = sketch_fingerprint(background_block)
    concept_signals = has_concept_signals(profile)
    # 契约 §6.4：背景非空即算「有画像」；三种情况用 profile_note 分开说清楚
    profile_used = concept_signals or background_used
    rating_count = len(profile.get("concepts") or {})
    override_count = len(profile.get("overrides") or [])
    if rating_count:
        profile_note = f"依据画像 {reader}：{rating_count} 条概念自评"
        if override_count:
            profile_note += f"，{override_count} 条手动推翻（优先级最高）"
        if background_used:
            profile_note += "；另有读前背景问卷的读者素描（已注入提示词的「读者背景」块）"
    elif override_count:
        profile_note = f"依据画像 {reader}：0 条概念自评，{override_count} 条手动推翻（优先级最高）"
        if background_used:
            profile_note += "；另有读前背景问卷的读者素描（已注入提示词的「读者背景」块）"
    elif background_used:
        if has_fixed_answers(profile):
            profile_note = (
                f"依据画像 {reader}：仅背景，无概念自评——"
                "本次只有读前背景问卷的读者素描可用（已注入提示词的「读者背景」块），"
                "why.profile 保留模型给的引用（只有 background_fingerprint 能说明用的是哪版背景）"
            )
        else:
            # 读者跳过了固定问卷、但答了模型追问：素描里只有追问摘要（有非空追问摘要就算有背景）
            profile_note = (
                f"依据画像 {reader}：仅追问摘要（没有概念自评，固定问卷也跳过了）——"
                "本次可用的是模型追问的问答摘要（已注入提示词的「读者背景」块），"
                "why.profile 保留模型给的引用（只有 background_fingerprint 能说明用的是哪版背景）"
            )
    else:
        profile_note = (
            f"无画像：profiles/{reader}/profile.yaml 里没有任何概念自评，"
            "本次只按概念清单的难度提示决策（已在产物里标注）"
        )
    if not profile_used:
        degraded.append(
            "无画像模式："
            + profile_note
            + "；每段 why.profile 只能引用概念清单里的 difficulty（前端显示为「难度 N（无画像）」）"
        )
    elif background_used and not concept_signals:
        degraded.append(
            "背景模式（没有降级）："
            + profile_note
            + "；本次没有概念自评，因此不往 why.profile 里写「（无画像）」标记"
        )
    if inventory is None:
        degraded.append(
            "concepts.json 不存在：没有概念清单，决策只能依据画像（可能全篇保持原文）。"
            f"先运行 `ar analyze {source_id}`（需要模型）"
        )

    # 两遍式的**第一遍**（--plan-first）：一次调用给全篇每段打动作标，**不写正文**。
    # 两遍**共用同一段前缀**（plan-v2）：第一遍 = 改写那份的公共头（REWRITE_USER_HEAD）+ 打标尾巴，
    # system 也直接用 REWRITE_SYSTEM_PROMPT ⇒ 两遍公共前缀 9889 字符 / 5364 token，
    # 第一遍建好的缓存被第二遍第 1 批直接命中。（plan-v1 时这里写的是「公共前缀 0 字符」——
    # 那是 v1 的事实：system 不同 + user 首字符就分岔；2026-10-19 的复核结论按 v1 记，v2 起作废。）
    # 第一遍失败不阻断：计划为空 ⇒ 那一格零字符插入。
    plan_first_enabled = bool(plan_first)
    plan_entries: list[PlanEntry] = []
    plan_block = ""
    plan_missing: list[str] = []
    plan_extra: dict[str, Any] = {}
    # 旧产物里的计划这一轮到底复用了没有 / 哪几段没能注入（「一致性优先」，两种都要可见）：
    # plan_reused 只在「局部重跑 + 旧产物里确实有计划」时才有意义；plan_not_applied 是
    # 「目标段在旧计划里找不到条目」的点名清单（不许静默降级成单遍改写）。
    plan_reused = False
    plan_not_applied: list[str] = []
    reuse = (
        _reuse_plan(
            base,
            reader=reader,
            background_fingerprint=background_fingerprint,
            fact_anchor_policy=policy.policy,
        )
        if targets is not None
        else None
    )
    if plan_first_enabled:
        # 局部重跑复用既有产物里的计划：--only / --retry-failed / --from-signals 的语义是
        # 「只重跑这几段」，第一遍的计划本来就还在 decisions.json 的 meta 里，不必重新打标
        if reuse is not None and reuse.ok:
            plan_entries = list(reuse.entries)
            plan_missing = list(reuse.missing)
            plan_extra = dict(reuse.extra)
            plan_reused = True
            planned_ids = {entry.paragraph for entry in plan_entries}
            plan_not_applied = [pid for pid in targets.paragraphs if pid not in planned_ids]
            degraded.append(
                "两遍式（--plan-first）：复用既有产物里第一遍的计划，不重新打标"
                f"（0 次请求；{len(plan_entries)} 段；局部重跑只对目标段发请求）"
            )
            degraded.extend(reuse.notes)
            if plan_not_applied:
                degraded.append(
                    "⚠ 以下目标段没有可注入的计划（旧产物里没有这几段的计划条目）："
                    + "、".join(plan_not_applied)
                    + "；这几段本次按单遍改写，产物 meta.plan_not_applied 记着这一笔"
                )
        else:
            if reuse is not None:
                # 旧计划还在，但口径对不上（换读者 / 换背景 / 换事实锚口径）：不许沿用，重新打标
                degraded.append(
                    "两遍式（--plan-first）：旧产物里有计划，但**不复用**（"
                    f"{reuse.reason}）——本次重新全篇打标（多一次请求）"
                )
            plan_entries, plan_degraded, plan_extra = plan_actions(
                meta,
                inventory,
                profile,
                client,
                source_id=source_id,
                reader=reader,
                background_block=background_block,
                # 口径要进**两遍共用的前缀**（plan-v2）：第二遍按同一档口径拼那一段，
                # 两遍的提示词才逐字同前 9889 字符；否则第一遍建的缓存白建
                fact_anchor_policy=policy.policy,
            )
            degraded.extend(plan_degraded)
            # 第一遍漏了哪几段（plan_missing）：只是「计划不完整」的可见记录，
            # **不进 unplanned、不影响退出码**——第二遍照常逐段改写
            plan_missing = [
                str(pid) for pid in (plan_extra.get("plan_missing") or []) if str(pid).strip()
            ]
        plan_block = _plan_block(plan_entries)
    elif reuse is not None:
        # **不带 --plan-first 的局部重跑**（2026-10-19 复核修复）：旧产物是按计划跑出来的，
        # 这一段就照旧计划改写——同一份产物始终按同一份计划执行，不因为一次局部重跑就偷偷换成
        # 单遍（否则产物看起来「按计划跑且计划与执行一致」，实际根本不是）。
        if reuse.ok:
            plan_entries = list(reuse.entries)
            plan_missing = list(reuse.missing)
            plan_reused = True
            plan_block = _plan_block(plan_entries)
            planned_ids = {entry.paragraph for entry in plan_entries}
            plan_not_applied = [pid for pid in targets.paragraphs if pid not in planned_ids]
            degraded.append(
                "两遍式（本次没带 --plan-first）：沿用既有产物里第一遍的计划，照旧注入第二遍"
                f"（0 次打标请求；{len(plan_entries)} 段；本次只对目标段发请求）"
            )
            degraded.extend(reuse.notes)
        else:
            # 口径对不上就不能注入：那几段只能单遍改写，但**不静默**——meta 与命令行都点名
            plan_not_applied = list(targets.paragraphs)
        if plan_not_applied:
            why = reuse.reason if not reuse.ok else "旧产物里没有这几段的计划条目"
            degraded.append(
                f"⚠ 以下目标段没有可注入的计划（{why}）："
                + "、".join(plan_not_applied)
                + "；这几段本次按单遍改写，产物 meta.plan_not_applied 记着这一笔"
            )

    decisions, plan_degraded, plan_meta = planDecisions(
        meta,
        inventory,
        profile,
        client,
        source_id=source_id,
        reader=reader,
        max_chars_per_batch=max_chars_per_batch,
        background_block=background_block,
        fact_anchor_policy=policy.policy,
        targets=set(targets.paragraphs) if targets is not None else None,
        per_paragraph=per_paragraph,
        concurrency=concurrency,
        validation_retries=validation_retries,
        conversation=conversation,
        plan_block=plan_block,
        conversation_digest=conversation_digest,
    )
    degraded.extend(plan_degraded)

    # 两遍式：第一遍的计划动作挂到每段决策上——**只在它与最终动作不同时**才落 changes[i].plan_action
    # （照 fact_declarations 的「为真才出现」先例；计划与执行一致时产物不多一个永远相同的键）。
    if plan_entries:
        planned_by_id = {entry.paragraph: entry.action for entry in plan_entries}
        for decision in decisions:
            planned = planned_by_id.get(decision.paragraph)
            if planned and planned != decision.action:
                decision.plan_action = planned

    # 产物 meta 里那几格（只在开了 --plan-first 时非空 ⇒ 默认路径的产物键集一个都不多）
    plan_meta_block: dict[str, Any] = {}
    if plan_first_enabled:
        plan_meta_block = {
            "plan_first": True,
            "plan": [entry.to_record() for entry in plan_entries],
            "plan_missing": list(plan_missing),
            "plan_usage": dict(plan_extra.get("plan_usage") or {}),
            "plan_request_count": int(plan_extra.get("plan_request_count") or 0),
            "plan_attempts": [
                dict(item) for item in (plan_extra.get("plan_attempts") or []) if isinstance(item, dict)
            ],
            "plan_retry_account": dict(plan_extra.get("plan_retry_account") or {}),
            "plan_prompt_version": str(plan_extra.get("plan_prompt_version") or PLAN_PROMPT_VERSION),
        }
    if reuse is not None:
        # 「旧计划这一轮复用了没有」必须落进产物：只有「局部重跑 + 旧产物里确实有计划」时才问这个问题。
        # 问过就**恒写**（plan_reused 的 true / false、plan_not_applied 的空列表都要写进 run_meta）：
        # 合并器靠「键在不在」区分「这一轮查过、是干净的」与「这一轮没查」——否则上一轮的点名会留成假账。
        plan_meta_block["plan_reused"] = plan_reused
        plan_meta_block["plan_not_applied"] = list(plan_not_applied)

    # 空画像：why.profile 必须看得见地说明「依据来自哪里」。
    # 有画像（含「只有背景」——契约 §6.4 拍板：背景非空即算有画像）时**逐条保留模型给的引用**
    # （提示词规则 11 的口径：引用形如 c03:unknown）；每段的背景依据只写在 why.note 里，
    # 不动元素格式。
    # **模型漏给引用时不静默**（2026-10-14 复核修复）：这一段涉及的概念读者有自评、而模型给了
    # 空数组 / 漏键 / 给错形状时，记一条可见降级并照读者自己的自评**兜底补齐**；
    # 「读者本来就没有概念自评」那一档**不误报**（空数组是契约允许的结果，见契约 §6.4）。
    concept_paragraphs: dict[str, list[str]] = {}
    concept_difficulty: dict[str, Any] = {}
    for concept in (inventory or {}).get("concepts") or []:
        cid = str(concept.get("id"))
        concept_paragraphs[cid] = [str(ref) for ref in (concept.get("paragraphs") or [])]
        concept_difficulty[cid] = concept.get("difficulty_hint")
    self_rating_states = _self_rating_states(profile)
    profile_refs_filled: list[str] = []
    for decision in decisions:
        if not profile_used:
            refs = [cid for cid, refs_ in concept_paragraphs.items() if decision.paragraph in refs_]
            hint = concept_difficulty.get(refs[0]) if refs else None
            if refs and hint is not None:
                decision.why["profile"] = [f"难度 {hint}（无画像）"]
            else:
                decision.why["profile"] = []
                decision.why["note"] = str(decision.why.get("note") or "") + "（无画像：本段没有可依据的读者信号）"
            continue
        refs = [str(item) for item in (decision.why.get("profile") or [])]
        decision.why["profile"] = refs
        if refs or not concept_signals:
            continue
        fallback = _self_rating_refs(decision.paragraph, self_rating_states, concept_paragraphs)
        if not fallback:
            # 本段涉及的概念读者都没自评：空数组是合法结果（依据来自难度提示 / 背景），不误报
            continue
        decision.why["profile"] = fallback
        decision.why["note"] = str(decision.why.get("note") or "") + (
            "（依据画像由骨架兜底补齐：模型没有给 why.profile 引用，这里照读者自评逐条列出）"
        )
        # 结构化标记（进 changes[i].profile_refs_filled）：页面那枚徽标只认它，不去猜 why.note 的文案
        decision.profile_refs_filled = True
        profile_refs_filled.append(decision.paragraph)
        degraded.append(
            f"{decision.paragraph}：模型没有给 why.profile 引用，而这一段涉及的概念读者有自评"
            f"（{'、'.join(fallback)}）——已按读者画像兜底补齐，请核对 why.note"
        )

    doc, changes = rewrite(meta, decisions, profile, mode=mode)
    markdown = str(doc["markdown"])
    blocks = split_rewritten_blocks(markdown)
    source_paragraphs = _source_paragraphs(meta)
    by_decision = {decision.paragraph: decision for decision in decisions}

    if targets is not None:
        # 局部重跑（§6.2）：本次只拿到目标段的结果，其余段落由合并器沿用既有产物。
        # 计划那几格并进 run_meta：合并器要按它们**重算**产物 meta（否则局部重跑后整体丢失）。
        outcome = _rerun_outcome(
            source_id=source_id,
            reader=reader,
            mode=mode,
            targets=targets,
            base=base,
            data_dir=data_dir,
            source_paragraphs=source_paragraphs,
            doc=doc,
            changes=changes,
            by_decision=by_decision,
            plan_meta={**plan_meta, **plan_meta_block},
            profile_used=profile_used,
            profile_note=profile_note,
            background_used=background_used,
            background_fingerprint=background_fingerprint,
            fact_anchor_policy=policy.policy,
            fact_anchor_policy_source=policy.source,
            degraded=degraded,
            at=at,
            profile_refs_filled=profile_refs_filled,
        )
        _apply_plan_fields(
            outcome,
            plan_first=plan_first_enabled,
            entries=plan_entries,
            missing=plan_missing,
            extra=plan_extra,
            conversation_digest=conversation_digest,
        )
        # 计划复用的账：**不带 --plan-first 的那次重跑也要看得见**（此时 plan_first 仍是 False）
        outcome.plan_reused = plan_reused
        outcome.plan_not_applied = list(plan_not_applied)
        # 「这一轮照的是哪份计划」带不带 flag 都写进 outcome（CLI 的计划那行靠它说清段数与动作分布）
        outcome.plan = list(plan_entries)
        outcome.plan_missing = list(plan_missing)
        return outcome

    # 段数 / 段序与源一致；**保持**的段落必须与原文逐字节一致（原文由摄取时算的 text_hash 背书）。
    # 事实锚的逐段比对由 `ar check` 负责，并把结果写回 `changes.json` 的 `fidelity.*`：
    # **rewrite 阶段不许写 checked: true**（那时比对还没跑，写成 true 会被误读成「已查过、干净」）。
    fidelity = _fidelity_block(
        source_paragraphs,
        blocks,
        {pid: d.action for pid, d in by_decision.items()},
        degraded=degraded,
        policy=policy.policy,
    )

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
    # 记账（追加字段）：全部批次的逐次尝试 + 一条汇总；两份产物写的是同一份账
    attempts = [dict(item) for item in (plan_meta.get("attempts") or []) if isinstance(item, dict)]
    retry_account_summary = plan_meta.get("retry_account")
    retry_account_summary = (
        dict(retry_account_summary) if isinstance(retry_account_summary, dict) else _merge_accounts([])
    )

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
        planning_mode=str(plan_meta.get("planning_mode") or "batch"),
        concurrency=int(plan_meta.get("concurrency") or 4),
        validation_retries=int(plan_meta.get("validation_retries") or 0),
        usage=usage,
        request_count=int(plan_meta.get("request_count") or 0),
        batch_count=int(plan_meta.get("batch_count") or 0),
        profile_used=profile_used,
        profile_note=profile_note,
        background_used=background_used,
        background_fingerprint=background_fingerprint,
        fact_anchor_policy=policy.policy,
        fact_anchor_policy_source=policy.source,
        deferred_count=deferred_count,
        profile_refs_filled=profile_refs_filled,
    )
    _apply_plan_fields(
        outcome,
        plan_first=plan_first_enabled,
        entries=plan_entries,
        missing=plan_missing,
        extra=plan_extra,
        conversation_digest=conversation_digest,
    )
    outcome.plan_reused = plan_reused
    outcome.plan_not_applied = list(plan_not_applied)

    generated_at = at or now_iso()
    decisions_payload: dict[str, Any] = {
        "decisions": [decision.to_record() for decision in decisions],
        "meta": {
            "source_id": source_id,
            "reader": reader,
            "mode": mode,
            "model": outcome.model,
            "prompt_version": REWRITE_PROMPT_VERSION,
            # 调度方式与并发（rewrite-v7）：逐段模式才有意义，batch 模式恒为 4 / 0
            "planning_mode": outcome.planning_mode,
            "concurrency": outcome.concurrency,
            "validation_retries": outcome.validation_retries,
            "generated_at": generated_at,
            "paragraph_count": len(meta.get("paragraphs") or []),
            "action_counts": counts,
            "implemented_actions": list(IMPLEMENTED_ACTIONS),
            "deferred_count": deferred_count,
            "profile_used": profile_used,
            "profile_note": profile_note,
            "background_used": background_used,
            "background_fingerprint": background_fingerprint,
            # 事实锚口径（§14.1，2026-10-14）：**本次改写**生效的那一档（读者画像偏好决定）
            "fact_anchor_policy": policy.policy,
            "fact_anchor_policy_source": policy.source,
            "request_count": outcome.request_count,
            "usage": usage,
            "batch_usage": batch_usage,
            "model_errors": model_errors,
            "unplanned_paragraphs": unplanned,
            "rewrite_incomplete": outcome.incomplete,
            "degraded": list(degraded),
            "attempts": attempts,
            "retry_account": retry_account_summary,
            # 逐段对话的上下文瘦身：**只在开时为 true**（默认路径一个键都不多）
            **({"conversation_digest": True} if plan_meta.get("conversation_digest") else {}),
            # 两遍式（plan-v2）：只在开了 --plan-first 时有这几格（默认路径一个键都不多）
            **plan_meta_block,
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
            "planning_mode": outcome.planning_mode,
            "concurrency": outcome.concurrency,
            "validation_retries": outcome.validation_retries,
            "generated_at": generated_at,
            # 与 decisions.json 的 meta 对齐（2026-10-13 复核修复 F5）：两边都写全篇段数
            "paragraph_count": len(meta.get("paragraphs") or []),
            "action_counts": counts,
            "deferred_count": deferred_count,
            "facts_note": "facts 只含数字与引号引文（与前端夹具同口径）；书名号《》由 `ar check` 另行比对",
            "profile_used": profile_used,
            "profile_note": profile_note,
            "background_used": background_used,
            "background_fingerprint": background_fingerprint,
            # 与 decisions.json 的 meta 对齐：事实锚口径（§14.1）两边都写
            "fact_anchor_policy": policy.policy,
            "fact_anchor_policy_source": policy.source,
            "usage": usage,
            "batch_usage": batch_usage,
            "model_errors": model_errors,
            "unplanned_paragraphs": unplanned,
            "rewrite_incomplete": outcome.incomplete,
            "degraded": list(degraded),
            "attempts": attempts,
            "retry_account": retry_account_summary,
            # 与 decisions.json 的 meta 对齐：对话瘦身 + 两遍式那几格（默认路径一个键都不多）
            **({"conversation_digest": True} if plan_meta.get("conversation_digest") else {}),
            **plan_meta_block,
        },
    }
    outcome.decisions_path = write_json(target / "decisions.json", decisions_payload)
    outcome.changes_path = write_json(target / "changes.json", changes_payload)
    outcome.rewritten_path = write_text(target / "rewritten.md", markdown)
    outcome.usage_note = _usage_summary(
        usage, retry_account_summary, unit="段" if outcome.planning_mode in ("per-paragraph", "conversation") else "批"
    )
    return outcome


def _rerun_outcome(
    *,
    source_id: str,
    reader: str,
    mode: str,
    targets: RerunTargets,
    base: MergeBase | None,
    data_dir: Path | str,
    source_paragraphs: list[dict[str, Any]],
    doc: dict[str, Any],
    changes: list[dict[str, Any]],
    by_decision: dict[str, Decision],
    plan_meta: dict[str, Any],
    profile_used: bool,
    profile_note: str,
    background_used: bool,
    background_fingerprint: str,
    fact_anchor_policy: str,
    fact_anchor_policy_source: str,
    degraded: list[str],
    at: str | None,
    profile_refs_filled: list[str] | None = None,
) -> RewriteOutcome:
    """局部重跑的收尾：把本次目标段的结果逐段合进既有产物（§6.2）。

    ``decisions`` / ``changes`` 只覆盖目标段（``planDecisions(targets=…)`` 只发了交集），
    所以这里把「本次结果」交给 :func:`merge_artifacts`，未重跑段由它逐字节搬旧条目。
    """
    assert base is not None  # 调用方（run_rewrite）在发请求之前就加载好了
    # 本次每段的正文：``rewrite()`` 已按段序给出 blocks（与 source_paragraphs 一一对应）
    blocks = [str(item) for item in (doc.get("paragraphs") or [])]
    change_by_id = {
        str(item.get("paragraph")): item for item in changes if isinstance(item, dict) and item.get("paragraph")
    }
    updates: dict[str, ParagraphUpdate] = {}
    missing: list[str] = []
    for index, paragraph in enumerate(source_paragraphs):
        pid = str(paragraph.get("id"))
        decision = by_decision.get(pid)
        if decision is None or pid not in change_by_id or index >= len(blocks):
            if pid in set(targets.paragraphs):
                missing.append(pid)
            continue
        updates[pid] = ParagraphUpdate(decision=decision, change=change_by_id[pid], text=blocks[index])
    merged_degraded = list(degraded)
    if missing:
        # 理论上到不了这里（批次按交集发、每段必有一条决策）：真到了就必须说出来，
        # 不能让「这次重跑了它」的账与产物对不上
        merged_degraded.append(
            "⚠ 以下目标段没有拿到本次的决策，仍沿用既有产物：" + "、".join(missing)
        )
    merged_degraded.append(
        f"局部重跑（{targets.label()}）：本次只对 {len(targets.paragraphs)} 段发请求"
        f"（{'、'.join(targets.paragraphs)}），其余段落沿用既有产物（逐字节不变）；"
        "fidelity 已重置为未校验，请重跑 `ar check` 复核"
    )
    rerun = {
        "at": at,
        "reason": targets.reason,
        "requested": targets.requested,
        "paragraphs": list(targets.paragraphs),
        "batches": int(plan_meta.get("batch_count") or 0),
    }
    merged = merge_artifacts(
        source_id,
        data_dir,
        base=base,
        updates=updates,
        run_meta=plan_meta,
        degraded=merged_degraded,
        shared_meta={
            "reader": reader,
            "mode": mode,
            "profile_used": profile_used,
            "profile_note": profile_note,
            "background_used": background_used,
            "background_fingerprint": background_fingerprint,
            # 事实锚口径（§14.1）：重跑之后产物的 meta / fidelity 记的仍是**本次读者**的口径
            "fact_anchor_policy": normalize_fact_anchor_policy(fact_anchor_policy),
        },
        rerun=rerun,
        at=at,
    )
    # 命令行看到的计数是**合并后全量**的真实状态（退出码 6 也按它算）
    merged_decisions = [
        Decision(
            paragraph=str(record.get("paragraph") or ""),
            action=str(record.get("action") or ""),
            why=record.get("why") if isinstance(record.get("why"), dict) else {"profile": [], "note": ""},
            confidence=float(record.get("confidence") or 0.0),
        )
        for record in merged.decisions
    ]
    usage = dict(merged.decisions_meta.get("usage") or {})
    outcome = RewriteOutcome(
        source_id=source_id,
        reader=reader,
        mode=mode,
        decisions=merged_decisions,
        changes=merged.changes,
        fidelity=merged.fidelity,
        rewritten_markdown=merged.markdown,
        degraded=merged_degraded,
        model_errors=merged.model_errors,
        unplanned_paragraphs=merged.unplanned,
        model=merged.decisions_meta.get("model"),
        # 调度方式与并发（rewrite-v7）：合并后的 meta 里现在有了，CLI 的「调度」/ 单位那几行才说得对
        planning_mode=str(merged.decisions_meta.get("planning_mode") or "batch"),
        concurrency=int(merged.decisions_meta.get("concurrency") or DEFAULT_CONCURRENCY),
        validation_retries=int(merged.decisions_meta.get("validation_retries") or 0),
        usage=usage,
        request_count=int(merged.decisions_meta.get("request_count") or 0),
        batch_count=int(plan_meta.get("batch_count") or 0),
        profile_used=profile_used,
        profile_note=profile_note,
        background_used=background_used,
        background_fingerprint=background_fingerprint,
        fact_anchor_policy=normalize_fact_anchor_policy(fact_anchor_policy),
        fact_anchor_policy_source=fact_anchor_policy_source,
        deferred_count=merged.deferred_count,
        rerun=merged.rerun,
        rerun_targets=list(targets.paragraphs),
        rerun_history=merged.rerun_history,
        # 兜底补齐的段：本次只可能是目标段（其余段落沿用既有产物，不重算）
        profile_refs_filled=list(profile_refs_filled or []),
    )
    outcome.decisions_path = merged.paths["decisions"]
    outcome.changes_path = merged.paths["changes"]
    outcome.rewritten_path = merged.paths["rewritten"]
    rerun_meta = merged.decisions_meta
    unit = "段" if str(rerun_meta.get("planning_mode") or "") in ("per-paragraph", "conversation") else "批"
    outcome.usage_note = _usage_summary(usage, rerun_meta.get("retry_account"), unit=unit)
    return outcome


# ------------------------------------------- 在线压缩（「太啰嗦」，§5.1 / §5.2） --
@dataclass
class CompressOutcome:
    """一次在线压缩请求的结果（HTTP 层据此组装响应，**成功与降级都带可读文案**）。

    ``ok=False`` 时 ``reason`` 取 :data:`COMPRESS_REASONS` 里的值、``degraded`` 是人读说明；
    ``ok=True`` 时 ``usage`` / ``attempts`` / ``model`` / ``prompt_version`` 描述这次真的产出了什么
    （缓存命中时 ``usage={}`` / ``attempts=[]``：这次没有发请求）。
    **成功路径的 ``degraded`` 平时是空的**，会非空的情况只有三类：① 这一段三类事实锚合计为 0 ——
    校验空跑（:data:`COMPRESS_NO_ANCHOR_NOTE`，2026-10-13 复核修复 F1）；② 申报本身读不通
    （**整条不是对象** / 结构不对 / 值不是字符串 / 不认识的键；整条不是对象那一档由工具层
    ``ar.tools`` 给出，见 ``tools._run_compress_paragraph``）；③ 行为事件没记上（由 HTTP 层并进去）。
    三者都**不影响写盘与 ``ok``**。
    ``fact_declarations`` / ``waived`` / ``policy`` 是 2026-10-14（§14.1）新增的可见面：
    这次压缩模型申报了什么、按哪一档口径放行了哪几项——``fact_declarations`` 同时落进
    ``changes[i].fact_declarations``，重跑 ``ar check`` 后同一批条目进 ``fidelity.waived``。
    """

    ok: bool
    text: str
    paragraph: str = ""
    reason: str | None = None
    cached: bool = False
    artifacts_updated: bool = False
    model: str | None = None
    prompt_version: str = COMPRESS_PROMPT_VERSION
    usage: dict[str, Any] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    hint: str = ""
    #: 本次压缩写进 ``changes[i].fact_declarations`` 的申报（归一化后；没有申报时是空字典）
    fact_declarations: dict[str, Any] = field(default_factory=dict)
    #: 本次压缩按读者口径放行的条目（结构同 ``fidelity.waived`` 的一行；没有就是空列表）
    waived: list[dict[str, Any]] = field(default_factory=list)
    #: 本次压缩里「申报与实测对不上」的条目（锚点一个没少却申报放弃了它；**不影响放行结论**，
    #: 结构同 ``fidelity.over_declared`` 的一行）
    over_declared: list[dict[str, Any]] = field(default_factory=list)
    #: 本次压缩采用的读者口径（``keep_all`` / ``allow_declared_drops``）
    policy: str = DEFAULT_FACT_ANCHOR_POLICY


def _find_paragraph(meta: dict[str, Any], paragraph_id: str) -> dict[str, Any] | None:
    for item in _source_paragraphs(meta):
        if str(item.get("id")) == paragraph_id:
            return item
    return None


def _anchor_comparison(
    paragraph: dict[str, Any],
    compressed: str,
    *,
    declaration: dict[str, Any] | None = None,
    policy: str = DEFAULT_FACT_ANCHOR_POLICY,
):
    """单段事实锚比对（数字 / 引号引文 / 书名号），返回 :class:`ar.fidelity.FactComparison`。

    复用 ``ar.fidelity.compare_blocks_detailed``——与 ``ar check`` **同一套判据**：少了且已申报的
    在松口径下放行，没申报的缺失 / 多出来的锚点 / 顺序调换一律进 ``problems``（调用方据此拒绝写入）。
    ``declaration`` 已经过 ``parse_declarations`` 归一（再传一遍是幂等的，不会重复出问题清单）。

    为什么把 ``counts`` 一并交出去（2026-10-13 复核修复 F1）：这一段三类锚点合计为 0 时，
    ``problems`` 必然是空的——「0 比对项」在这里意味着**校验空跑**，与 ``ar.fidelity`` 顶部那两条
    推论同口径（「最干净的报告」恰恰是最没跑过的那种），不能当成「压得干净」。
    调用方据此在成功响应里留一条可见说明；**不拒绝写入**（没有事实可毁的段落压缩本来无害）。
    """
    from .fidelity import compare_blocks_detailed  # noqa: PLC0415 - 同包内避免顶层循环

    return compare_blocks_detailed(
        [paragraph],
        [compressed],
        declarations={str(paragraph.get("id")): declaration} if declaration else None,
        policy=policy,
    )


def _anchor_total(counts: dict[str, Any]) -> int:
    """``counts`` 里三类事实锚的合计条数（数字 + 引号引文 + 书名号）。"""
    return sum(int(counts.get(field) or 0) for field in ("numbers", "quotes", "books"))


def _compress_hint(waived: list[dict[str, Any]], policy: str) -> str:
    """写盘成功带回前端的那句提示（§5.2 的 ``hint``）。

    有放行条目时把「丢了几项、按哪一档口径放行」补进去——读者点的那颗「太啰嗦」按钮旁边
    就该说得出这件事（可见面之一，另两处是卡片的申报块与重跑 ``ar check`` 的 CLI 输出）。
    没有放行时就是 :data:`COMPRESS_HINT` 逐字原文（前端缺省回退文案与它同源）。
    """
    if not waived:
        return COMPRESS_HINT
    return (
        f"{COMPRESS_HINT}；本段申报放弃了 {len(waived)} 项事实锚"
        f"（已按读者口径 {normalize_fact_anchor_policy(policy)} 放行）"
    )


async def acompress_paragraph(
    source_id: str,
    paragraph_id: str,
    *,
    data_dir: Path | str,
    profiles_dir: Path | str,
    reader: str,
    client: Any,
    cache: ExplainCache | None = None,
    reason: str = DEFAULT_COMPRESS_REASON,
    at: str | None = None,
) -> CompressOutcome:
    """读者点「太啰嗦」：当场把这一段压短并**写回三份产物**（§5.2）。

    任何失败都变成可见降级（不抛给 HTTP 层），``reason`` 取 :data:`COMPRESS_REASONS`。
    流程：找段 → 前置校验（三份产物齐备、条数对得上，不合格 → ``no_artifacts``）→ 查缓存 →
    调 ``compress_paragraph`` 工具（``compress-v3`` 提示词，喂**该段当前正文**，并把本读者当前
    的事实锚口径传进提示词的固定块）→ 解析这次压缩的**事实锚申报**（``fact_declarations``）→
    **单段事实锚比对**（按**原文**、按读者口径：少了且已申报 ⇒ 松口径放行；其余一律不写盘）→
    「必须比当前正文短」→ :func:`merge_artifacts` 逐段合进既有产物（``fidelity`` 重置、账记本次）。

    这一段三类事实锚合计为 0 时（``counts`` 里 numbers / quotes / books 全 0）**不拒绝写入**，
    但在成功结果的 ``degraded`` 里留一条 :data:`COMPRESS_NO_ANCHOR_NOTE`：没有事实可毁 ≠ 校验过，
    那是「0 比对项 = 检查器空跑」（2026-10-13 复核修复 F1）。

    两条长度 / 保真口径分得开：**压的是读者眼前的那段正文**（可能是展开稿），而**事实锚一律按
    ``source.md`` 的原文比对**（与 ``ar check`` **同一套判据**：``compare_blocks_detailed``）。

    **申报通道**（2026-10-14，§14.1）：模型在 ``fact_declarations`` 里逐条申报本次压缩放弃了哪几项
    事实锚；口径取 ``profiles/<reader>/profile.yaml`` 的偏好 ``pref_fact_anchor``
    （缺省 / 读坏一律退到严口径 ``keep_all``，与 ``ar check`` 同一条口径解析路径）。
    - 松口径 ``allow_declared_drops`` + **申报覆盖了全部缺失** ⇒ 放行，条目进 ``changes[i].fact_declarations``
      与本次结果的 ``waived``（重跑 ``ar check`` 后同一批条目进 ``fidelity.waived``）；
    - **没申报的缺失、多出来的锚点、顺序调换、申报与实测对不上 ⇒ 一律拒绝写入**（``fact_anchor_mismatch``），
      与 ``ar check`` 的底线完全一致；
    - 申报本身读不通（结构不对 / 值不是字符串 / 不认识的键）不会静默：逐条进本次结果的 ``degraded``。

    ``cache`` 非空时命中即返回（``cached: true``、``usage: {}``、不发请求、不写盘）；
    缓存键含画像指纹（:func:`ar.cache.compress_cache_key`）。
    """
    from .fidelity import parse_declarations  # noqa: PLC0415 - 同包内避免顶层循环
    from .profile import load_profile, resolve_fact_anchor_policy  # noqa: PLC0415 - 同上

    target = doc_dir(data_dir, source_id)
    meta = read_json_if_exists(target / "source.meta.json")
    if not isinstance(meta, dict):
        return CompressOutcome(
            ok=False,
            paragraph=paragraph_id,
            reason="no_artifacts",
            text=(
                f"找不到材料 {source_id} 的 source.meta.json：请先运行 "
                f"`ar ingest <文件> --id {source_id}`（再做整篇改写）。"
            ),
            degraded=[f"材料 {source_id} 尚未摄取：没有可改写的产物，也没有写盘"],
        )
    paragraph = _find_paragraph(meta, paragraph_id)
    if paragraph is None:
        known = "、".join(str(p.get("id")) for p in _source_paragraphs(meta)[:5]) or "（无）"
        return CompressOutcome(
            ok=False,
            paragraph=paragraph_id,
            reason="paragraph_not_found",
            text=(
                f"材料 {source_id} 里没有段落 {paragraph_id}：材料可能已被重新摄取（锚点变了），"
                f"请刷新页面重新校准。现有段落形如 {known}…"
            ),
            degraded=[f"段 {paragraph_id} 不在 source.meta.json 里：未压缩、未写盘"],
        )
    try:
        base = load_merge_base(source_id, data_dir, meta=meta)
    except InputError as exc:
        # 产物缺一份 / 条数对不上：不写盘（也不许半合并），把原因原样说给用户
        return CompressOutcome(
            ok=False,
            paragraph=paragraph_id,
            reason="no_artifacts",
            text=str(exc),
            degraded=[f"局部压缩需要既有的三份产物：{exc}"],
        )

    key = (
        compress_cache_key(
            source_id=source_id, paragraph_id=paragraph_id, profiles_dir=profiles_dir, reader=reader
        )
        if cache is not None
        else None
    )
    if cache is not None and key is not None:
        hit = cache.get(key)
        if hit is not None:
            # 命中即返回：不发请求、不写盘（缓存里只有成功结果，所以 ok 恒为 True）
            hit_waived = [dict(item) for item in (hit.get("waived") or []) if isinstance(item, dict)]
            return CompressOutcome(
                ok=True,
                paragraph=paragraph_id,
                text=str(hit.get("text") or ""),
                cached=True,
                artifacts_updated=False,
                model=hit.get("model"),
                prompt_version=str(hit.get("prompt_version") or COMPRESS_PROMPT_VERSION),
                usage={},
                attempts=[],
                # 首次成功时算出的可见说明（例如「这一段没有事实锚」「申报后放行了几项」）跟着缓存一起回
                degraded=[str(item) for item in (hit.get("degraded") or [])],
                hint=_compress_hint(hit_waived, str(hit.get("policy") or "")),
                fact_declarations=(
                    dict(hit.get("fact_declarations"))
                    if isinstance(hit.get("fact_declarations"), dict)
                    else {}
                ),
                waived=hit_waived,
                over_declared=[
                    dict(item) for item in (hit.get("over_declared") or []) if isinstance(item, dict)
                ],
                policy=normalize_fact_anchor_policy(hit.get("policy")),
            )

    if client is None or not getattr(client, "configured", False):
        return CompressOutcome(
            ok=False,
            paragraph=paragraph_id,
            reason="model_not_configured",
            text=(
                "模型未配置，暂时不能压缩这一段。设置环境变量 AR_API_KEY，或在 config.local.json 里"
                "填 api_key，再点一次即可（原文与产物不受影响）。"
            ),
            degraded=["模型未配置：这一段的正文与产物一个字节都没动"],
        )

    # 压的是**读者正在看的那一段**（rewritten.md 里的当前正文：可能是展开稿 / 上一次的压缩稿），
    # 不是 source.md 的原文 —— 读者点「太啰嗦」说的是他眼前这段文字。
    # 事实锚仍然按**原文**比对（``ar check`` 的口径），两条口径各管一件事。
    order = [str(item.get("id")) for item in base.source_paragraphs]
    current_text = base.blocks[order.index(paragraph_id)]
    inventory = read_json_if_exists(target / "concepts.json")
    inventory = inventory if isinstance(inventory, dict) else None
    profile, _profile_notes = load_profile(profiles_dir, reader)
    # 事实锚口径（§14.1）：与 `ar check` 走**同一条解析路径**（缺省 / 读坏一律退到严口径 + 可见说明）
    anchor_policy = resolve_fact_anchor_policy(profile)
    params = {
        "paragraph": paragraph_id,
        "text": current_text,
        "concepts": _format_concept_block(source_id, inventory),
        "profile_block": _format_profile_block(reader, profile, list(profile.get("overrides") or [])),
        # 读者的补充要求按契约不加（提示词缺省就是「太啰嗦」那条固定文案，逐字稳定 ⇒ 前缀缓存友好）
        "instruction": "",
        # 本读者当前的事实锚口径进提示词固定块（compress-v3）：模型才知道这一轮能不能丢举例数据
        "fact_anchor_policy": anchor_policy.policy,
    }
    # 工具层 ``execute`` 是同步的（内部 ``asyncio.run``）：本函数跑在 HTTP 事件循环里，必须丢线程
    run = await asyncio.to_thread(tools.run_tool, tools.COMPRESS_PARAGRAPH_SPEC.name, params, client=client)
    if not run.ok:
        message = run.failure_message()
        if run.model_replied:
            # HTTP 通了、抠不出压缩稿（输出不成结构 / 空正文）：与「没答上来」分开报
            return CompressOutcome(
                ok=False,
                paragraph=paragraph_id,
                reason="bad_reply",
                text=f"模型返回的内容用不了，未压缩这一段：{message}",
                degraded=[f"模型答了但输出不成结构（{message}）：未压缩、未写盘"],
                attempts=run.attempt_records,
                usage=dict(run.usage or {}),
            )
        return CompressOutcome(
            ok=False,
            paragraph=paragraph_id,
            reason="model_error",
            text=f"未能压缩这一段：{message}",
            degraded=[f"模型调用失败（{message}）：未压缩、未写盘"],
            attempts=run.attempt_records,
            usage=dict(run.usage or {}),
        )

    data = run.data if isinstance(run.data, dict) else {}
    # 工具层的可见说明（例如「模型给的 fact_declarations 不是对象（实际 str）：按没有申报处理」）：
    # 一律并进本次结果的 degraded（成功路径也不吞）——业务层不接住它，用户就看不到「申报读不通」。
    tool_notes = list(run.degraded)
    compressed = _clean_rewrite(data.get("text"))
    if not compressed:
        return CompressOutcome(
            ok=False,
            paragraph=paragraph_id,
            reason="bad_reply",
            text="模型返回了空的压缩稿，未压缩这一段。",
            degraded=["模型给的压缩稿是空的：未压缩、未写盘", *tool_notes],
            attempts=run.attempt_records,
            usage=dict(run.usage or {}),
        )
    if len(compressed) >= len(current_text):
        return CompressOutcome(
            ok=False,
            paragraph=paragraph_id,
            reason="compress_not_shorter",
            text=(
                f"模型给的压缩稿（{len(compressed)} 字）不比这一段现在的正文（{len(current_text)} 字）短："
                "按「没有真的压缩」处理，正文保持原样。"
            ),
            degraded=[
                f"段 {paragraph_id}：压缩稿不比当前正文短，拒绝写入（正文一个字节没动）",
                *tool_notes,
            ],
            attempts=run.attempt_records,
            usage=dict(run.usage or {}),
        )
    # 申报（§14.1）：解析一次，问题清单一律可见（结构不对 / 值不是字符串 / 不认识的键、note 截断）
    declared, declaration_notes = parse_declarations(data.get("fact_declarations"))
    comparison = _anchor_comparison(
        paragraph, compressed, declaration=declared or None, policy=anchor_policy.policy
    )
    problems = list(comparison.problems)
    counts = dict(comparison.counts)
    over_declared = [dict(item) for item in comparison.over_declared]
    if problems:
        listed = "；".join(problems)
        return CompressOutcome(
            ok=False,
            paragraph=paragraph_id,
            reason="fact_anchor_mismatch",
            text=f"压缩稿的事实锚与原文对不上，没有写入（正文保持原样）：{listed}",
            # 每条问题本身就以「段 id：哪一类锚点对不上（少/多哪些）+ 申报了没有」开头，不再重复加前缀；
            # 申报读不通的那几条、以及「多申报」的几条也一并带上（同一批可见说明，不分两处）
            degraded=[
                *problems,
                *declaration_notes,
                *(f"事实锚多申报：{item.get('detail') or item}" for item in over_declared),
                *tool_notes,
            ],
            attempts=run.attempt_records,
            usage=dict(run.usage or {}),
            fact_declarations=declared,
            over_declared=over_declared,
            policy=anchor_policy.policy,
        )
    # 这一段一个锚点都没有（数字 / 引号引文 / 书名号合计 0）：校验**空跑** ⇒ 不拒绝写入，但要可见
    # （2026-10-13 复核修复 F1；口径同 ``fidelity.py`` 顶部「0 比对项 = 检查器空跑」）
    anchor_notes = [COMPRESS_NO_ANCHOR_NOTE] if _anchor_total(counts) == 0 else []
    # 放行条目（松口径 + 已申报）：本次压缩的可见面 —— 卡片 / 悬停 / 横幅都要说得出「丢了什么、谁放行的」
    waived = [dict(item) for item in comparison.waived]
    if waived:
        anchor_notes.append(
            f"本段申报放弃了 {len(waived)} 项事实锚，已按读者口径 {anchor_policy.policy} 放行："
            + "；".join(str(item.get("detail") or "") for item in waived)
        )
    for item in over_declared:
        anchor_notes.append(f"事实锚多申报：{item.get('detail') or item}")

    # 事实锚过了才构造变更记录：压缩后正文与原文不同 ⇒ 原来那张图不再配套，**不带 figure**。
    # 申报（§14.1）按**本次压缩**的实测结果写：模型申报了才带这个键（没有申报时整个键不出现），
    # 它是重跑 `ar check` 时按同一档口径放行的依据，也是页面 / CLI 的可见面。
    why_note = str(data.get("why_note") or "").strip()
    note = (
        f"读者点击「太啰嗦」当场压缩该段（reason={reason}）；"
        "压的是阅读器里显示的正文，事实锚按原文核对（数字 / 引号引文 / 书名号）"
    ) + (f"：{why_note}" if why_note else "")
    decision = Decision(
        paragraph=paragraph_id,
        action="compress",
        why={"profile": [], "note": note},
        # 单段压缩工具的出参里没有 confidence（只有 text / why_note），这里给中性值 0.5，
        # 不假装是模型给的把握度
        confidence=0.5,
        rewrite=compressed,
        requested_action="compress",
        fact_declarations=dict(declared) if declared else None,
    )
    _sub_doc, new_changes = rewrite({"paragraphs": [paragraph]}, [decision], profile)
    change = new_changes[0] if new_changes else {}
    run_meta = {
        "model": run.model,
        "usage": dict(run.usage or {}),
        "batch_usage": [],
        "attempts": run.attempt_records,
        "retry_account": run.account(),
        "request_count": 1,
        "batch_count": 1,
        "model_errors": [],
        "unplanned_paragraphs": [],
    }
    merge_artifacts(
        source_id,
        data_dir,
        base=base,
        updates={paragraph_id: ParagraphUpdate(decision=decision, change=change, text=compressed)},
        run_meta=run_meta,
        degraded=[
            f"在线压缩：段 {paragraph_id} 的正文与 changes.json / decisions.json 已当场改写"
            "（只改这一段，段序不变）；fidelity 已重置为未校验，请重跑 `ar check` 复核",
            # 申报读不通的那几条（结构不对 / 整条不是对象 / 不认识的键…）：进产物的可见降级，不静默
            *declaration_notes,
            # 工具层给出的说明（例如「整条 fact_declarations 不是对象」）：产物里同样留痕
            *tool_notes,
        ],
        at=at,
    )
    if cache is not None and key is not None:
        # 只缓存成功结果（上面每条降级路径都在 return 里走掉了）
        cache.put(
            key,
            {
                "text": compressed,
                "model": run.model,
                "prompt_version": COMPRESS_PROMPT_VERSION,
                # 命中缓存时也要能看见首次成功时那几条说明（没有事实锚 / 申报后放行 / 申报读不通）：
                # 口径与首次成功一致
                "degraded": [*anchor_notes, *tool_notes],
                # 本次压缩的申报与放行条目跟着缓存一起回：命中时页面照样显示得出「本段申报」
                "fact_declarations": dict(declared),
                "waived": [dict(item) for item in waived],
                "over_declared": [dict(item) for item in over_declared],
                "policy": anchor_policy.policy,
            },
        )
    return CompressOutcome(
        ok=True,
        paragraph=paragraph_id,
        text=compressed,
        cached=False,
        artifacts_updated=True,
        model=run.model,
        usage=dict(run.usage or {}),
        attempts=run.attempt_records,
        # 成功路径上的可见说明（空列表 = 一切正常）；HTTP 层把它并进响应的 degraded
        degraded=[*anchor_notes, *declaration_notes, *tool_notes],
        hint=_compress_hint(waived, anchor_policy.policy),
        fact_declarations=dict(declared),
        waived=waived,
        over_declared=over_declared,
        policy=anchor_policy.policy,
    )
