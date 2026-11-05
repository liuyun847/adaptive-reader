"""命令行入口：契约 §4 冻结的 5 个子命令 + 只读的 ``tools`` + ``feedback``（本批新增）。

```
uv run ar ingest  <文件路径> --id <source_id>
uv run ar analyze <source_id> [--check-only]
uv run ar rewrite <source_id> --reader local-default [--mode free]
                  [--only p012,p014 | --retry-failed | --from-signals]   # 局部重跑（三者互斥）
                  [--per-paragraph [--conversation [--conversation-digest]]]   # 逐段 / 逐段对话（+上下文瘦身）
                  [--plan-first]                                        # 两遍式：先打动作标、再改写
uv run ar check   <source_id> [--reader local-default]     # 锚点稳定性 + 事实锚（数字/引文/书名+段缺失）
                                                            # 口径由读者画像偏好 pref_fact_anchor 决定
uv run ar baseline <source_id> [--reader local-default]     # 一次性整篇改写基线（只写 _eval/）
uv run ar serve   --port 8765
uv run ar tools   [--json]                              # 模型工具契约（只读，不发请求）
uv run ar feedback <source_id> --reader local-default [--apply]   # 行为信号回流（默认 dry-run）
```

退出码：0 成功 / 2 用法或输入错误（含**校验所需产物缺失 / 不可读 / 为空**）/ 3 尚未实现 /
4 校验失败（含**检查器空跑**）/ 5 模型未配置或调用失败 / 6 模型失败导致**有段落未被改写**。

``--mode strict``（严格档）是**预留插口**：未实现，遇到就明确报错（退出码 3），不静默按 free 跑。

事实锚「可申报 + 可见」（PLAN-actions-feedback §14.1）：模型在 ``changes[i].fact_declarations`` 里
逐条申报本段没有原样保留的事实锚；``ar check`` 按**读者画像里那条偏好**（``pref_fact_anchor``：
``keep_all`` 严 / ``allow_declared_drops`` 松）决定放不放行，放行的条目逐条打印并落
``changes.json`` 的 ``fidelity.waived``。**没申报的缺失照旧以退出码 4 失败**——
这条网只松「举例数据可以丢」，不松「事实漂移」（多出来的锚点与顺序调换任何口径都不放行）。
**申报与实测对不上**（锚点一个没少却申报放弃了某项）不改放行结论与退出码，但逐条打印并落
``fidelity.over_declared``：模型的账要看得见，绝不静默。同一条口径也用在**在线压缩**上
（``POST /api/compress`` → ``compress-v3``），两条路径不会一个放行一个拒写。

局部重跑（PLAN-actions-feedback §6）：``--only`` / ``--retry-failed`` / ``--from-signals``
**三者互斥**（两两或全给 → 退出码 2 + 明确文案）；全缺省时行为与整篇改写**逐字不变**。
目标段为空（例如 ``--retry-failed`` 但上次没有失败段）时只打印「没有需要重跑的段」、
**不写盘、不调模型**，以 0 退出。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .analyze import check_inventory_file, run_analyze
from .anchors import verify_source
from .chain import default_routes_path, order_routes, resolve_api_key
from .config import Config, load_config
from .errors import (
    EXIT_MODEL,
    EXIT_MODEL_PARTIAL,
    EXIT_OK,
    EXIT_USAGE,
    ArError,
    InputError,
    ModelPartiallyFailed,
    ModelUnavailable,
    NotImplementedYet,
    ValidationFailed,
)
from .ingest import ingest
from .jsonio import read_json_if_exists
from .model import ModelClient
from .paths import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_DATA_DIR,
    DEFAULT_PROFILES_DIR,
    DEFAULT_READER,
    DEFAULT_WEB_DIR,
    check_reader_id,
    check_source_id,
    doc_dir,
)
from .rewrite import DEFAULT_CONCURRENCY, DEFAULT_MAX_CHARS_PER_BATCH, DEFAULT_VALIDATION_RETRIES
from .server import build_app

__all__ = ["build_parser", "main", "entry"]

CHECK_NOT_IMPLEMENTED = (
    "check 的事实锚校验（数字 / 引号引文 / 书名号《》 + 段缺失）尚未实现。"
)


def _make_client(config: Config) -> ModelClient:
    """模型客户端工厂。测试 monkeypatch 本函数即可注入固定响应的假客户端。"""
    return ModelClient(config)


def _say(message: str = "") -> None:
    print(message, flush=True)


def _warn(message: str) -> None:
    print(f"⚠ {message}", file=sys.stderr, flush=True)


def _force_utf8_streams() -> None:
    """让 stdout / stderr 走 UTF-8。

    Windows 上 ``uv run ar …`` 的输出流默认用本地代码页（GBK），带 ✓ / ⚠ 的提示会直接抛
    ``UnicodeEncodeError``（真机踩过一次：摄取其实成功了，却因为打印「✓」而报错退出）。
    这里的 ``reconfigure`` 只在流支持时生效，失败就退回 ``errors="replace"``，不让它影响主流程。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):  # pragma: no cover - 交互式重定向的极端情况
            continue


# --------------------------------------------------------------------- ingest --
def _cmd_ingest(args: argparse.Namespace) -> int:
    source_id = check_source_id(args.id)
    result = ingest(Path(args.path), source_id, args.data_dir)
    paragraphs = result.paragraphs
    _say(f"✓ 摄取完成：{source_id}")
    _say(f"  产物：{result.doc_dir / 'source.md'}")
    _say(f"       {result.doc_dir / 'source.meta.json'}")
    _say(f"  段落：{len(paragraphs)} 段（{paragraphs[0]['id']} … {paragraphs[-1]['id']}）")
    _say(f"  来源哈希：{result.meta['ingest']['hash']}")
    if result.previous_present and not result.warnings:
        _say("  锚点稳定：与上一次摄取相比，段落 id 与 text_hash 完全一致")
    for warning in result.warnings:
        _warn(warning)
    return EXIT_OK


# -------------------------------------------------------------------- analyze --
def _cmd_analyze(args: argparse.Namespace) -> int:
    source_id = check_source_id(args.source_id)
    if args.check_only:
        problems = check_inventory_file(args.data_dir, source_id)
        if problems:
            raise ValidationFailed(problems, headline="概念清单契约校验失败")
        _say(f"✓ concepts.json 通过契约校验：{source_id}（段 id 引用都存在，kind 都在四类之内）")
        return EXIT_OK

    config: Config = load_config(args.config)
    if config.unknown_keys:
        _warn(f"配置里有未知字段（已忽略）：{', '.join(config.unknown_keys)}")
    if not config.model_configured:
        raise ModelUnavailable(
            "模型未配置：设置环境变量 AR_API_KEY，或在 config.local.json 里填 api_key。"
            "没有模型就无法抽取概念清单（不静默假装成功）"
        )

    client = _make_client(config)
    result = run_analyze(source_id, args.data_dir, client)
    _say(f"✓ 概念清单已生成：{source_id}")
    _say(f"  产物：{Path(args.data_dir) / source_id / 'concepts.json'}")
    _say(f"  概念 {result.concept_count} 条，配图候选 {result.illustration_count} 条")
    _say(f"  模型：{result.model}；提示词版本：{result.prompt_version}")
    if result.repairs:
        _warn(f"模型输出有 {len(result.repairs)} 处需要整理（已记进 concepts.json 的 meta.repairs）：")
        for line in result.repairs[:10]:
            _warn(f"  · {line}")
        if len(result.repairs) > 10:
            _warn(f"  · …另有 {len(result.repairs) - 10} 处，见产物 meta.repairs")
    return EXIT_OK


# ---------------------------------------------------------------- baseline --
def _cmd_baseline(args: argparse.Namespace) -> int:
    """一次性整篇改写基线：单次调用、全文进、全篇出；只写 data/<id>/_eval/。"""
    from .baseline import run_oneshot

    source_id = check_source_id(args.source_id)
    reader = check_reader_id(args.reader)
    if args.max_tokens is not None and args.max_tokens < 1:
        raise InputError(f"--max-tokens 必须 ≥ 1，实际是 {args.max_tokens}")
    config: Config = load_config(args.config)
    if config.unknown_keys:
        _warn(f"配置里有未知字段（已忽略）：{', '.join(config.unknown_keys)}")
    if not config.model_configured:
        raise ModelUnavailable(
            "模型未配置：设置环境变量 AR_API_KEY，或在 config.local.json 里填 api_key。"
            "没有模型就无法做一次性基线（不静默假装成功）"
        )
    client = _make_client(config)
    outcome = run_oneshot(
        source_id,
        args.data_dir,
        args.profiles_dir,
        reader,
        client,
        max_tokens=args.max_tokens,
    )
    if not outcome.ok:
        _say(f"✗ 一次性基线失败：{source_id}")
        _say(f"  原因：{outcome.reason}")
        _say("  没有写 oneshot.md；这次的原因与 token 账在 _eval/oneshot.json")
        return EXIT_MODEL

    counts = outcome.counts or {}
    _say(f"✓ 一次性基线完成：{source_id}（读者 {reader}）")
    _say(f"  产物：{outcome.json_path}")
    _say(f"       {outcome.markdown_path}")
    _say(f"  模型：{outcome.model}；提示词版本：{outcome.prompt_version}")
    _say(
        f"  段数：{outcome.paragraph_count}（模型返回 {outcome.returned_count}，"
        f"改写 {outcome.changed_count}，缺 {len(outcome.missing)}）"
    )
    if outcome.counts:
        _say(
            f"  事实锚：比对 {counts.get('compared', 0)} 段；"
            f"数字 {counts.get('numbers', 0)} / 引号引文 {counts.get('quotes', 0)} / "
            f"书名号 {counts.get('books', 0)}"
        )
    cached = outcome.usage.get("cached_tokens") if isinstance(outcome.usage, dict) else None
    cache_note = "未上报" if cached is None else str(cached)
    _say(
        f"  耗时 {outcome.elapsed_ms / 1000:.1f}s；finish_reason={outcome.finish_reason}；"
        f"tokens：prompt {outcome.usage.get('prompt_tokens', 0)} / completion "
        f"{outcome.usage.get('completion_tokens', 0)} / 合计 {outcome.usage.get('total_tokens', 0)}；"
        f"前缀缓存命中 {cache_note}"
    )
    for item in outcome.problems[:10]:
        _warn(item)
    if len(outcome.problems) > 10:
        _warn(f"…另有 {len(outcome.problems) - 10} 条事实锚问题，见 _eval/oneshot.json")
    return EXIT_OK


# -------------------------------------------------------------------- rewrite --
def _cmd_rewrite(args: argparse.Namespace) -> int:
    from .rewrite import (
        ACTIONS,
        DEFAULT_MODE,
        IMPLEMENTED_ACTIONS,
        STRICT_NOT_IMPLEMENTED,
        UNIMPLEMENTED_ACTIONS,
        actions_label,
        resolve_rerun_targets,
        run_rewrite,
    )

    source_id = check_source_id(args.source_id)
    reader = check_reader_id(args.reader)
    mode = args.mode or DEFAULT_MODE
    if mode == "strict":
        # 严格档接口预留：未实现就明确报错，绝不静默按 free 跑（用户 2026-10-05 拍板）
        raise NotImplementedYet(STRICT_NOT_IMPLEMENTED)

    # 局部重跑（§6.1）：三个参数互斥；目标段解析**早于**模型门禁 —— 目标为空时不该因为
    # 「模型没配」而报 5（那次运行本来什么都不用做，也不写盘）。
    rerun_flags = [
        name
        for name, given in (
            ("--only", args.only is not None),
            ("--retry-failed", args.retry_failed),
            ("--from-signals", args.from_signals),
        )
        if given
    ]
    if len(rerun_flags) > 1:
        raise InputError(
            "局部重跑的 --only / --retry-failed / --from-signals 三者互斥，一次只能给一个"
            f"（收到了 {len(rerun_flags)} 个：{'、'.join(rerun_flags)}）"
        )
    targets = None
    if rerun_flags:
        targets = resolve_rerun_targets(
            source_id,
            args.data_dir,
            args.profiles_dir,
            reader,
            only=args.only,
            retry_failed=args.retry_failed,
            from_signals=args.from_signals,
        )
        for note in targets.degraded:
            _warn(note)
        if targets.empty:
            _say(f"没有需要重跑的段（{targets.label()}）：{source_id}")
            _say("  没有写盘、也没有调用模型（目标段为空）")
            return EXIT_OK

    config: Config = load_config(args.config)
    if config.unknown_keys:
        _warn(f"配置里有未知字段（已忽略）：{', '.join(config.unknown_keys)}")
    if not config.model_configured:
        raise ModelUnavailable(
            "模型未配置：设置环境变量 AR_API_KEY，或在 config.local.json 里填 api_key。"
            "没有模型就无法逐段决策与改写（不静默假装成功）"
        )

    if args.conversation and not args.per_paragraph:
        raise InputError("--conversation 必须与 --per-paragraph 一起用（同一上下文里逐段推进）")
    if args.conversation_digest and not args.conversation:
        # 摘要只改「逐段对话里塞回上下文的东西」：没有对话就没有 transcript 可瘦身（不静默忽略）
        raise InputError("--conversation-digest 必须与 --per-paragraph --conversation 一起用（同一上下文里逐段推进）")
    # 对话本身有顺序：显式给了 !=1 的并发才提醒，缺省不算「用户要求并发」
    if args.conversation and args.concurrency not in (None, 1):
        _warn("--conversation 是串行的；--concurrency 已按 1 处理")
    if args.conversation:
        args.concurrency = 1
    if args.concurrency is None:
        args.concurrency = DEFAULT_CONCURRENCY
    if args.concurrency < 1:
        raise InputError(f"--concurrency 必须 ≥ 1（1 = 串行），实际是 {args.concurrency}")
    if args.validation_retries < 0:
        raise InputError(f"--validation-retries 不能小于 0，实际是 {args.validation_retries}")
    client = _make_client(config)
    outcome = run_rewrite(
        source_id,
        args.data_dir,
        args.profiles_dir,
        reader,
        client,
        mode=mode,
        max_chars_per_batch=args.max_chars_per_batch,
        targets=targets,
        per_paragraph=args.per_paragraph,
        concurrency=args.concurrency,
        validation_retries=args.validation_retries,
        conversation=args.conversation,
        plan_first=args.plan_first,
        conversation_digest=args.conversation_digest,
    )

    counts = outcome.action_counts()
    # 有段落因模型问题没被改写（不是模型主动「保持」）⇒ 不许打印「✓ 改写完成」
    incomplete = outcome.incomplete
    if incomplete:
        _say(f"⚠ 改写不完整：{source_id}（读者 {reader}，mode={outcome.mode}）")
    else:
        _say(f"✓ 改写完成：{source_id}（读者 {reader}，mode={outcome.mode}）")
    if outcome.planning_mode == "conversation":
        _say(
            f"  调度：逐段对话（同一上下文累积，串行；校验重试上限 {outcome.validation_retries} 次）"
        )
    elif outcome.planning_mode == "per-paragraph":
        _say(
            f"  调度：逐段（并发 {outcome.concurrency}；校验重试上限 {outcome.validation_retries} 次）"
        )
    if outcome.plan_first:
        # 两遍式的第一遍：几段、动作怎么分、花了多少次请求（计划本身也落产物 meta.plan）。
        # 复用旧计划的重跑**一次打标请求都没发**，这行就必须写「0 次请求」——不许照抄旧产物的记账
        # （2026-10-19 复核修复：原先一律打 plan_request_count，复用时会照样报「1 次请求」）。
        plan_counts = {action: 0 for action in ACTIONS}
        for entry in outcome.plan:
            plan_counts[entry.action] = plan_counts.get(entry.action, 0) + 1
        plan_breakdown = " / ".join(
            f"{actions_label(action)} {plan_counts.get(action, 0)}"
            for action in ACTIONS
            if plan_counts.get(action, 0)
        )
        head = f"  第一遍打标：{len(outcome.plan)} 段（{plan_breakdown or '—'}）；"
        _say(head + ("复用旧计划，0 次请求（未重新打标）" if outcome.plan_reused else f"{outcome.plan_request_count} 次请求"))
        if outcome.plan_missing:
            _warn(
                "第一遍打标漏了这些段（第二遍照常逐段改写，不计入 unplanned、不改退出码）："
                + "、".join(outcome.plan_missing)
            )
        if outcome.plan_usage:
            # 第一遍的 token 账单独一行：``_usage_summary`` 汇总的是第二遍，混在一起就说不清
            label = "（复用旧计划那次，非本次）" if outcome.plan_reused else ""
            _say(
                f"  第一遍打标账{label}："
                f"prompt {outcome.plan_usage.get('prompt_tokens', 0)} / "
                f"completion {outcome.plan_usage.get('completion_tokens', 0)} / "
                f"合计 {outcome.plan_usage.get('total_tokens', 0)}"
            )
    elif outcome.plan_reused:
        # 不带 --plan-first 的局部重跑：旧产物是按计划跑出来的，这一段照旧按那份计划改写
        _say(
            f"  计划：沿用既有产物里第一遍的计划照旧注入第二遍（{len(outcome.plan)} 段；"
            "本次未重新打标，0 次打标请求）"
        )
    if outcome.plan_not_applied:
        _warn(
            "以下目标段没能注入既有计划（本次按单遍改写；产物 meta.plan_not_applied 有记录）："
            + "、".join(outcome.plan_not_applied)
        )
    if outcome.conversation_digest:
        _say(
            "  上下文瘦身：上一轮只回填一行摘要（p001 keep / p002 expand：正文）；"
            "why / confidence 等外壳不进上下文（原始 JSON 仍用于本轮校验）"
        )
    _say(f"  产物：{outcome.decisions_path}")
    _say(f"       {outcome.rewritten_path}")
    _say(f"       {outcome.changes_path}")
    if outcome.rerun:
        # 局部重跑：说清「哪几段真的重跑了、其余段落没动、本次花了多少请求」
        rerun = outcome.rerun
        requested = rerun.get("requested")
        requested = requested if isinstance(requested, dict) else {}
        detail = f"--only {','.join(requested.get('only') or [])}" if requested.get("only") else f"--{rerun.get('reason')}"
        targets_label = "、".join(outcome.rerun_targets) or "（无）"
        _say(f"  本次局部重跑：{detail}；目标段 {len(outcome.rerun_targets)} 段（{targets_label}）")
        # 合并后 meta 里已有真实的调度方式（2026-10-19 复核第 9 条）：逐段对话也是「一段一次请求」
        units = "段（逐段）" if outcome.planning_mode in ("per-paragraph", "conversation") else "批"
        _say(f"      本次发出 {outcome.batch_count} {units}；其余段落沿用既有产物（未重跑段逐字节不变）")
        _say("      fidelity 已重置为未校验（改写阶段的 checked 恒为 false）")
    # 动作计数与实现范围都从枚举现算（四个动作：展开 / 压缩 / 保持 / 配图）——不许再硬编码 expand/keep
    breakdown = " / ".join(f"{actions_label(action)} {counts.get(action, 0)}" for action in ACTIONS)
    _say(f"  决策：{len(outcome.decisions)} 段（{breakdown}）")
    implemented = " / ".join(actions_label(action) for action in IMPLEMENTED_ACTIONS)
    scope = f"  动作实现范围：{implemented}"
    if UNIMPLEMENTED_ACTIONS:
        # deferred 的文案一个字不改：只有「枚举内的未实现动作」才走这条路（当前四个动作都实现了，
        # 所以这段平时不打印；将来真加了枚举内的未实现动作，它照旧出现）
        scope += (
            f"；{' / '.join(actions_label(action) for action in UNIMPLEMENTED_ACTIONS)}"
            "（枚举内的未实现动作）遇到按保持处理并标 deferred"
        )
    _say(scope + "；枚举外的动作（含模型没给 action）按保持处理、不标 deferred")
    if outcome.deferred_count:
        deferred_ids = "、".join(d.paragraph for d in outcome.decisions if d.deferred)
        _say(f"  按保持处理的未实现动作：{outcome.deferred_count} 段（{deferred_ids}）")
    # 「画像：」这个前缀是改动前就有的输出文案（本批只授权动作计数那段动态化），逐字保留
    _say(f"  画像：{outcome.profile_note}")
    # why.profile 的账（2026-10-14 复核修复）：几段有引用、几段空、哪几段是骨架兜底补齐的。
    # 「没有自评引用」与「画像为空」是两回事（背景非空即算有画像），所以这里只按引用本身说事，
    # 不许写成「画像为空」——产物 / 页面 / 命令行三处口径一致。
    with_refs = [d.paragraph for d in outcome.decisions if d.why.get("profile")]
    empty_refs = [d.paragraph for d in outcome.decisions if not d.why.get("profile")]
    refs_line = f"  why.profile：{len(with_refs)} 段有引用 / {len(empty_refs)} 段空"
    if not outcome.profile_used:
        refs_line += "（空画像模式：写的是「难度 N（无画像）」或在 why.note 里说明）"
    filled = list(outcome.profile_refs_filled)
    if filled:
        refs_line += f"；其中 {len(filled)} 段模型没给引用而读者有自评，已按读者自评兜底补齐（{'、'.join(filled)}）"
    elif outcome.profile_used and empty_refs:
        refs_line += "（空数组是合法结果：读者没有这些概念的自评，或依据来自读者背景）"
    _say(refs_line)
    # 事实锚口径（§14.1）：这一次按哪一档校验，以及哪几段申报了什么——都在命令行上看得见
    _say(f"  事实锚口径：{outcome.fact_anchor_policy}（{outcome.fact_anchor_policy_source}）")
    declared = [d for d in outcome.decisions if d.fact_declarations]
    if declared:
        _say(f"  事实锚申报：{len(declared)} 段（放不放行由 `ar check` 按口径判）")
        for decision in declared:
            _say(f"    · {decision.paragraph}：{_declaration_label(decision.fact_declarations)}")
    else:
        _say("  事实锚申报：0 段（模型没有申报放弃任何事实锚）")
    _say(f"  模型：{outcome.model}；提示词版本：{outcome.prompt_version}")
    # usage_note 里已经写了「真实请求数（批数；其中重试 K 次）」，这里别再拿批数当「请求数」说一遍
    if outcome.planning_mode in ("per-paragraph", "conversation"):
        _say(f"  {outcome.usage_note}（逐段 {outcome.batch_count} 次请求，合并了 {len(outcome.decisions)} 段）")
    elif outcome.rerun:
        _say(f"  {outcome.usage_note}（本次 {outcome.batch_count} 批；合并后全篇 {len(outcome.decisions)} 段）")
    else:
        _say(f"  {outcome.usage_note}（{outcome.batch_count} 批合并了 {len(outcome.decisions)} 段）")
    _say(
        "  段序：与原文一致"
        if outcome.fidelity.get("paragraphs_match_source")
        else "  ⚠ 改写稿段数与原文不一致（请跑 `ar check` 看是哪几段）"
    )
    for item in outcome.degraded:
        _warn(item)
    if incomplete:
        return _report_incomplete(source_id, reader, outcome)
    _say(f"  下一步：`uv run ar check {source_id}` 校验事实锚")
    return EXIT_OK


def _declaration_label(declaration: Any) -> str:
    """一条事实锚申报 → 人读的一行（``数字 2 项（1696、2）/ 引号引文 1 项（…）``）。"""
    labels = (("numbers", "数字"), ("quotes", "引号引文"), ("books", "书名号"))
    parts: list[str] = []
    for field_name, label in labels:
        items = [x for x in (declaration.get(field_name) or []) if isinstance(x, str)]
        if not items:
            continue
        shown = "、".join(f"「{x}」" for x in items[:2])
        parts.append(f"{label} {len(items)} 项（{shown}{'…' if len(items) > 2 else ''}）")
    note = str(declaration.get("note") or "").strip()
    if note:
        parts.append(f"说明：{note}")
    return "；".join(parts) if parts else "（空申报）"


def _report_incomplete(source_id: str, reader: str, outcome: Any) -> int:
    """把「有段落没被改写」的明细打到 stdout 并返回退出码 6。"""
    _say("")
    _say(f"✗ 有段落因模型问题未被改写（保持原文，未改写）：{source_id}")
    if outcome.model_errors:
        _say(f"  失败批次 {len(outcome.model_errors)} 个：")
        for item in outcome.model_errors:
            paragraphs = "、".join(str(pid) for pid in (item.get("paragraphs") or [])) or "（段 id 未知）"
            _say(f"    · {item.get('batch') or '（批次未知）'}：{item.get('error')}")
            _say(f"      受影响段：{paragraphs}")
    if outcome.unplanned_paragraphs:
        _say("  没有拿到可用决策的段（按保持处理、未改写）：" + "、".join(outcome.unplanned_paragraphs))
    _say(
        "  退出码 6：事实锚只能证明「没改坏」，证明不了「真的改写了」——"
        "有段落一个字没改就必须报出来"
    )
    _say(f"  下一步：`uv run ar rewrite {source_id} --reader {reader}` 重跑补齐（产物已落盘，可直接重跑覆盖）")
    _say(f"        `uv run ar check {source_id}` 也会以退出码 {EXIT_MODEL_PARTIAL} 反映这件事")
    return EXIT_MODEL_PARTIAL


# -------------------------------------------------------------------- feedback --
def _cmd_feedback(args: argparse.Namespace) -> int:
    """``ar feedback``：把行为事件折算成画像变更（PLAN-actions-feedback §7.4）。

    读 ``profiles/<reader>/events.jsonl`` → ``events.signals_from_events`` → ``profile.applySignals``，
    打印 ``changed`` / ``skipped_overrides`` / ``evidence_added`` / ``degraded``。
    **默认 dry-run**（只打印、不写盘）；``--apply`` 才写 ``profile.yaml``，且写盘前若序列化文本与
    现有文件逐字相同就**不写**（``save_profile`` 的口径：一次无谓写盘会换画像指纹 ⇒ 讲解缓存全失效）。

    **幂等**（2026-10-13 复核修复 F2）：只折算 ``profiles/<reader>/feedback.json`` 里还没记过的事件
    （按事件 id 判），``--apply`` 成功后把本次**看过**的该材料事件 id 写回该文件
    （保留最近 ``FEEDBACK_RECORD_KEEP`` 条）+ ``last_at`` 水位线 ⇒ 同一份事件重复跑 ``--apply``
    不再叠加 ``score`` / ``evidence``。dry-run 一个字节都不写；水位线文件缺失 / 损坏按
    「全部事件都未处理」处理，并给一条可见降级（不静默）。

    退出码：0 成功（含「无变更」）/ 2 用法或输入错误（材料未摄取、``concepts.json`` 缺失等产物缺失）。
    """
    from .events import (  # noqa: PLC0415 - 同包内避免顶层循环
        event_id,
        merge_processed,
        pending_events,
        processed_ids,
        read_events,
        read_feedback_record,
        signals_from_events,
        write_feedback_record,
    )
    from .profile import affected_paragraphs, applySignals, load_profile, save_profile

    source_id = check_source_id(args.source_id)
    reader = check_reader_id(args.reader)
    target = doc_dir(args.data_dir, source_id)
    meta_path = target / "source.meta.json"
    if not meta_path.is_file():
        raise InputError(
            f"{meta_path} 不存在：先运行 `ar ingest <文件> --id {source_id}`（行为信号要按材料折算成概念）"
        )
    inventory = read_json_if_exists(target / "concepts.json")
    if not isinstance(inventory, dict):
        raise InputError(
            f"{target / 'concepts.json'} 不存在：把段反查成概念要用它（先运行 `ar analyze {source_id}`）"
        )

    events, event_notes = read_events(args.profiles_dir, reader)
    record, record_notes = read_feedback_record(args.profiles_dir, reader)
    done, done_notes = processed_ids(record)
    pending = pending_events(events, done)
    # 只折算**这份材料**的未处理事件；别的材料的事件留着（它们要按自己的 source_id 折算）
    mine = [item for item in pending if str(item.get("source_id") or "") == source_id]
    new_ids = [event_id(item) for item in mine]
    merged_ids, trim_notes = merge_processed(done, new_ids) if new_ids else (done, [])
    profile, profile_notes = load_profile(args.profiles_dir, reader)
    signals, signal_notes = signals_from_events(mine, inventory, source_id=source_id)
    update = applySignals(profile, signals)
    affected = sorted(affected_paragraphs(update, inventory))

    _say(f"行为信号回流：{source_id}（读者 {reader}）")
    _say(
        f"  事件：{len(events)} 条（{reader} 的已处理 {len(events) - len(pending)} 条；"
        f"本次折算 {len(mine)} 条）→ 信号：{len(signals)} 条"
    )
    if update.changed:
        _say(f"  状态变更 {len(update.changed)} 个概念：")
        for item in update.changed:
            before = item.get("from") or "（无）"
            _say(
                f"    · {item.get('concept')}：{before} → {item.get('to')}"
                f"（把握度 {item.get('confidence_from')} → {item.get('confidence_to')}，"
                f"score {item.get('score')}，同向证据 {item.get('negative')} 负 / {item.get('positive')} 正）"
            )
    else:
        _say("  状态变更：0 个概念（未达阈值：只累加 score / evidence，state 与 confidence 一个字符不动）")
    if update.accumulated:
        _say(
            "  只累加、未改状态 "
            f"{len(update.accumulated)} 个概念：" + "、".join(str(item.get("concept")) for item in update.accumulated)
        )
    if update.skipped_overrides:
        _say(
            "  被 overrides 保护、整条跳过 "
            f"{len(update.skipped_overrides)} 个概念："
            + "、".join(f"{item.get('concept')}（手动结论 {item.get('override_state')}）" for item in update.skipped_overrides)
        )
    _say(f"  追加证据 {update.evidence_added} 条")
    _say(f"  受影响段（供 `ar rewrite {source_id} --from-signals` 用）：{len(affected)} 段"
         + (f"（{'、'.join(affected)}）" if affected else ""))
    for note in [
        *event_notes,
        *record_notes,
        *done_notes,
        *trim_notes,
        *signal_notes,
        *profile_notes,
        *update.degraded,
    ]:
        _warn(note)

    if not args.apply:
        _say("  dry-run：**没有写盘**（想固化请加 --apply；dry-run 也不动 feedback.json）")
        return EXIT_OK
    path = save_profile(args.profiles_dir, reader, update.profile)
    _say(f"  ✓ 画像已写回：{path}（内容与现有文件逐字相同则不写盘）")
    if new_ids:
        newest = max([str(item.get("at") or "") for item in mine] + [str(record.get("last_at") or "")])
        record_path = write_feedback_record(
            args.profiles_dir, reader, processed=merged_ids, last_at=newest
        )
        _say(
            f"  ✓ 回流水位线已写回：{record_path}"
            f"（本次 {len(new_ids)} 条事件标为已处理，窗口里共 {len(merged_ids)} 条）"
        )
        _say("  幂等：同一份事件再跑 --apply 不会重复折算（已处理按事件 id 判，不按时间）")
    else:
        _say("  没有未处理的事件：feedback.json 未改动（同一份事件重复跑 --apply 不再叠加 score / evidence）")
    if affected:
        _say(f"  下一步：`uv run ar rewrite {source_id} --reader {reader} --from-signals` 只重跑这些段")
    return EXIT_OK


# ---------------------------------------------------------------------- check --
def _cmd_check(args: argparse.Namespace) -> int:
    from .rewrite import check_fidelity, fidelity_problems, incomplete_reasons

    source_id = check_source_id(args.source_id)
    problems = verify_source(args.data_dir, source_id)
    if problems:
        raise ValidationFailed(problems, headline="锚点校验失败（source.meta.json 与 source.md 不一致）")
    _say(f"✓ 锚点校验通过：{source_id}（段落 id / text_hash / text 与 source.md 一致）")

    reader = check_reader_id(getattr(args, "reader", "") or DEFAULT_READER)
    outcome = check_fidelity(
        source_id, args.data_dir, profiles_dir=args.profiles_dir, reader=reader
    )
    counts = outcome.counts
    # 口径（§14.1）与来源**每次都打**：读者看得见这次凭什么标准校验，不靠猜
    _say(f"  事实锚口径：{outcome.policy}（{outcome.policy_source}）")
    if outcome.policy_note:
        _warn(outcome.policy_note)
    for note in outcome.declaration_notes:
        _warn(note)
    if outcome.problems:
        # 产物缺失 / 不可读 / 为空：没得比就不许报通过（沿用「rewritten.md 缺失」的退出码语义：2）
        raise InputError(
            "事实锚校验所需的产物缺失 / 不可读 / 为空，无法证明保真（不静默通过）：\n"
            + "\n".join(f"  - {problem}" for problem in outcome.problems)
        )
    # 已申报并被口径放行的缺失：逐条打出来（放行必须看得见，不能只在产物里）
    if outcome.waived:
        _say(
            f"  已申报并放行 {len(outcome.waived)} 项（口径 {outcome.policy}）："
        )
        for item in outcome.waived:
            _say(f"    · {item.get('paragraph')}：{item.get('detail')}")
            _say(f"      申报放弃：{'、'.join(f'「{x}」' for x in (item.get('missing') or [])[:3])}")
    # 申报与实测对不上（锚点一个没少却申报放弃了某项）：**不改放行结论与退出码**（没有缺失可放行），
    # 但这是模型的账，必须看得见（与 waived 同一批可见面：CLI + fidelity.over_declared + 页面）
    for item in outcome.over_declared:
        _warn(f"事实锚多申报：{item.get('detail') or item}")
    if outcome.mismatches:
        raise ValidationFailed(
            fidelity_problems(outcome), headline=f"事实锚校验失败（{source_id}）"
        )
    if outcome.model_errors or outcome.unplanned_paragraphs:
        # 事实锚全对，但有段落根本没被改写（模型失败）——「通过」在这里等于假装成功
        raise ModelPartiallyFailed(
            f"事实锚比对通过，但改写不完整（{source_id}）：事实锚只能证明「没改坏」，"
            "区分不了「忠实改写」与「一个字没改」。以下段落保持原文、未被改写：\n"
            + "\n".join(f"  - {line}" for line in incomplete_reasons(outcome))
            + f"\n  重跑 `ar rewrite {source_id} --reader <读者>` 补齐后再 check（退出码 {EXIT_MODEL_PARTIAL}）"
        )
    compared = int(counts.get("compared", counts.get("paragraphs", 0)) or 0)
    waived_note = f"；其中 {len(outcome.waived)} 项已申报放行" if outcome.waived else ""
    _say(
        f"✓ 事实锚校验通过：逐段比对 {compared} 段——"
        f"数字 {counts.get('numbers', 0)} 项 / 引号引文 {counts.get('quotes', 0)} 项 / "
        f"书名号《》{counts.get('books', 0)} 项，全部与原文一致；没有段缺失{waived_note}"
    )
    return EXIT_OK


# ---------------------------------------------------------------------- tools --
def _cmd_tools(args: argparse.Namespace) -> int:
    """列出六个模型工具的契约（**只读**：不读配置、不调模型、不发请求）。

    ``--json`` 输出可被 ``json.loads`` 解析的数组；不带该选项时给人读的排版。
    """
    from .tools import schemas

    payload = schemas()
    if args.json:
        _say(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK
    _say(f"模型工具契约（{len(payload)} 个；只读列举，不读配置、不调模型）：")
    for spec in payload:
        _say("")
        _say(f"{spec['name']}（提示词版本 {spec['prompt_version']}）")
        _say(f"  描述：{spec['description']}")
        _say("  入参（JSON Schema）：")
        _say(json.dumps(spec["parameters"], ensure_ascii=False, indent=2))
        _say("  出参（JSON Schema）：")
        _say(json.dumps(spec["output"], ensure_ascii=False, indent=2))
    return EXIT_OK


# ---------------------------------------------------------------------- routes --
def _cmd_routes_show(args: argparse.Namespace) -> int:
    """``ar routes show``：打印当前生效的链——每跳的地址、密钥**来源**（从不给值）、以及有没有能力发请求。

    冷却记录**落盘**（``~/.dsh/storages/adaptive_reader_cooldowns.json``，重启后接着冷），但本命令
    不进链、不读盘，所以这里看不到它们；服务进程里的实时冷却看 ``/api/health`` 的 ``routes`` 字段。
    """
    config: Config = load_config(args.config)
    client = ModelClient(config)
    # 现查一次额度（``ar routes show`` 是显式命令，不受查询闸门约束；查不到就按「未知档」显示）
    asyncio.run(client.refresh_quota(force=True))
    rows = []
    for route in config.routes:
        _key, origin = resolve_api_key(route)
        rows.append(
            {
                "provider": route.provider,
                "model": route.model,
                "base_url": route.base_url,
                "api_key_env": route.api_key_env,
                "api_key_origin": origin,
                "usable": bool(origin),
                "headers": dict(route.headers),
            }
        )
    # 排序：auto（默认）/ manual。auto 下「配置顺序」与「生效顺序」不是一回事，两个都列出来
    cooling = set(client.cooldowns.active()) if config.routes else set()
    ordered = order_routes(config.routes, client.quota_report, cooling)
    payload = {
        "mode": "chain" if config.routes else "single",
        "ordering": config.ordering,
        "chain": [route.label for route in config.routes],
        "effective": [
            {
                "label": item.route.label,
                "reason": item.reason,
                "cooling": item.cooling,
                "monthly_reset_ms": item.reset_at_ms,
            }
            for item in ordered
        ],
        "quota": client.quota_report.public_list(),
        "routes": rows,
        "single_source": {"base_url": config.base_url, "model": config.model} if config.single_source else None,
        "config_path": config.source_path,
    }
    if args.json:
        _say(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK
    if not config.routes:
        _say(f"单源模式（没有配置 routes）：{config.model} @ {config.base_url}")
        _say("  要看多源回退链：uv run ar routes import --default（生成 config.routes.yaml）")
        return EXIT_OK
    by_provider = {row["provider"]: row for row in rows}
    if config.ordering == "auto":
        _say(f"多源回退链（{len(config.routes)} 跳 · 自动排序：按月重置升序 → 额度查不到 → 按量垫底）：")
        for index, item in enumerate(ordered, 1):
            row = by_provider[item.route.provider]
            origin = row["api_key_origin"] or "没有可用密钥（这一跳会被跳过）"
            reset = ""
            if item.reset_at_ms is not None:
                reset = "，月度重置 " + datetime.fromtimestamp(item.reset_at_ms / 1000).strftime("%Y-%m-%d %H:%M")
            _say(f"  {index}. {row['provider']}/{row['model']}  [{item.reason}{reset}]")
            _say(f"     地址：{row['base_url']}")
            _say(f"     密钥：{origin}")
        _say("额度（现查，按月重置那几家的排序键就来自这里）：")
        for source in client.quota_report.public_list():
            if not source["status"] == "ok":
                _say(f"  · {source['provider']}：查不到（{source['reason']}）—— {source['message']}")
                continue
            parts = []
            if source["monthly_reset_ms"]:
                parts.append(
                    "月度重置 " + datetime.fromtimestamp(source["monthly_reset_ms"] / 1000).strftime("%Y-%m-%d %H:%M")
                )
            if source["plan_id"]:
                parts.append("套餐 " + source["plan_id"])
            parts.append("已耗尽：" + ("、".join(source["exhausted_windows"]) if source["exhausted_windows"] else "无"))
            _say(f"  · {source['provider']}：" + "；".join(parts))
    else:
        _say(f"多源回退链（{len(config.routes)} 跳 · 手动排序：严格按下面的配置顺序，不跳过任何一家）：")
        for index, row in enumerate(rows, 1):
            origin = row["api_key_origin"] or "没有可用密钥（这一跳会被跳过）"
            _say(f"  {index}. {row['provider']}/{row['model']}")
            _say(f"     地址：{row['base_url']}")
            _say(f"     密钥：{origin}")
    _say(
        "冷却按 provider 记账并落盘（额度类：5 分钟短闸门 → 补查确认 → 冷到该档重置；"
        "没密钥 10 分钟；AUTH 不冷却）。"
    )
    _say("排序方式改配置里的 ordering（auto / manual）；/api/health 能看到当下的链与冷却。")
    return EXIT_OK


def _cmd_routes_import(args: argparse.Namespace) -> int:
    """``ar routes import``：把 YAML 链片段写成 ``config.routes.yaml``（**只写这份文件，不碰 config.local.json**）。"""
    from .chain import DEFAULT_ROUTES_YAML, load_routes_file, parse_routes

    source_text = DEFAULT_ROUTES_YAML
    if args.path and not args.default:
        source_path = Path(args.path)
        if not source_path.is_file():
            print(f"错误：链文件不存在：{source_path}", file=sys.stderr, flush=True)
            return EXIT_USAGE
        routes = load_routes_file(source_path)
        source_text = source_path.read_text(encoding="utf-8")
    else:
        import yaml

        routes = parse_routes(yaml.safe_load(source_text))
    if not routes:
        print("错误：链文件里没有一跳可用的路由（每项都要 provider / model / base_url / api_key_env）",
              file=sys.stderr, flush=True)
        return EXIT_USAGE

    target = Path(args.out) if args.out else default_routes_path()
    if target.exists() and not args.force:
        print(
            f"错误：{target} 已存在（要覆盖请加 --force）。先 ``uv run ar routes show`` 看看现在生效的是哪条链。",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_USAGE
    target.write_text(source_text, encoding="utf-8")
    _say(f"已写入 {target}（{len(routes)} 跳）：" + " → ".join(route.label for route in routes))
    _say("  这份文件不会自己生效：要么设 AR_ROUTES_FILE=" + str(target) + "，")
    _say("  要么把里面那几行抄进 config.local.json 的 routes 数组（配置里的 routes 优先级更高）。")
    _say("  ar serve 只在启动时读配置，改完请重启服务。")
    return EXIT_OK


# ---------------------------------------------------------------------- serve --
def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    config: Config = load_config(args.config)
    if config.unknown_keys:
        _warn(f"配置里有未知字段（已忽略）：{', '.join(config.unknown_keys)}")
    web_dir = Path(args.web_dir)
    app = build_app(
        data_dir=args.data_dir,
        profiles_dir=args.profiles_dir,
        web_dir=web_dir,
        config=config,
        reader=args.reader,
    )
    index_exists = (web_dir / "index.html").is_file()
    _say(f"服务启动：http://{args.host}:{args.port}/")
    _say(f"  数据目录：{Path(args.data_dir)}")
    _say(f"  画像目录：{Path(args.profiles_dir)}（读者 {args.reader}）")
    _say(f"  前端目录：{web_dir}（index.html {'存在' if index_exists else '不存在 —— / 会回写明原因的 404'}）")
    if config.routes:
        _say(f"  模型：多源回退链 {len(config.routes)} 跳 —— {config.model_label}")
        for route in config.routes:
            _key, origin = resolve_api_key(route)
            _say(f"    · {route.label}：{'密钥 ' + origin if origin else '没有可用密钥（这一跳会被跳过）'}")
    else:
        _say(
            "  模型："
            + config.model
            + ("（已配置）" if config.model_configured else "（未配置：/api/explain 会返回可见降级文案）")
        )
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return EXIT_OK


# ----------------------------------------------------------------------- 解析 --
def _add_common_options(parser: argparse.ArgumentParser, *, in_subcommand: bool) -> None:
    """全局选项：``--data-dir`` / ``--profiles-dir`` / ``--config``。

    子命令里的同名选项必须用 ``SUPPRESS`` 默认值：argparse 的子解析器会把
    自己的命名空间**整份覆盖**回主命名空间（默认值也算），否则 ``--data-dir``
    写在子命令前面会被子命令的默认值悄悄改回项目默认目录。
    """
    default = argparse.SUPPRESS if in_subcommand else str(DEFAULT_DATA_DIR)
    parser.add_argument(
        "--data-dir",
        default=default,
        help="产物目录（默认 <项目根>/data；写在子命令前或后都可以）",
    )
    parser.add_argument(
        "--profiles-dir",
        default=argparse.SUPPRESS if in_subcommand else str(DEFAULT_PROFILES_DIR),
        help="画像目录（默认 <项目根>/profiles）",
    )
    parser.add_argument(
        "--config",
        default=argparse.SUPPRESS if in_subcommand else str(DEFAULT_CONFIG_PATH),
        help="配置文件（默认 config.local.json）",
    )


def build_parser() -> argparse.ArgumentParser:
    """构造 CLI。全局选项既可写在子命令前，也可写在子命令后。"""
    parser = argparse.ArgumentParser(
        prog="ar",
        description="阅读文本自适应重写工具 · 后端命令行",
    )
    _add_common_options(parser, in_subcommand=False)
    parser.add_argument("--version", action="version", version=f"adaptive-reader {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_subcommand(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        _add_common_options(sub, in_subcommand=True)
        return sub

    def add_nested(name: str, group: argparse._SubParsersAction, help_text: str) -> argparse.ArgumentParser:
        """二级子命令：同样挂全局选项（``ar routes show --config x`` 这样写才不报错）。"""
        sub = group.add_parser(name, help=help_text)
        _add_common_options(sub, in_subcommand=True)
        return sub

    p_ingest = add_subcommand("ingest", "摄取本地 UTF-8 文本 → source.md + source.meta.json")
    p_ingest.add_argument("path", help="输入文件（Markdown / TXT，UTF-8）")
    p_ingest.add_argument("--id", required=True, help="source_id（同时是 data/<id>/ 目录名）")
    p_ingest.set_defaults(handler=_cmd_ingest)

    p_analyze = add_subcommand("analyze", "调模型抽概念清单 → concepts.json")
    p_analyze.add_argument("source_id")
    p_analyze.add_argument("--check-only", action="store_true", help="只校验已有 concepts.json（不调模型）")
    p_analyze.set_defaults(handler=_cmd_analyze)

    p_rewrite = add_subcommand(
        "rewrite",
        "逐段决策 + 改写（expand / compress / keep / illustrate）→ decisions.json + rewritten.md + changes.json"
        "；--only / --retry-failed / --from-signals 只重跑指定段（三者互斥）",
    )
    p_rewrite.add_argument("source_id")
    p_rewrite.add_argument("--reader", default=DEFAULT_READER, help=f"读者 id（默认 {DEFAULT_READER}）")
    p_rewrite.add_argument(
        "--mode",
        choices=("free", "strict"),
        default="free",
        help="改写档位：free（默认，自由重述 + 事实锚）；strict（严格档，接口预留、未实现，遇到即报错）",
    )
    p_rewrite.add_argument(
        "--max-chars-per-batch",
        type=int,
        default=DEFAULT_MAX_CHARS_PER_BATCH,
        help=f"合并请求的粒度：每批最多多少字符的原文（默认 {DEFAULT_MAX_CHARS_PER_BATCH}，越小请求越多、越贵）",
    )
    # 逐段循环（rewrite-v7）：一段一请求 + 校验回灌重试；--concurrency 1 = 串行
    p_rewrite.add_argument(
        "--per-paragraph",
        action="store_true",
        help="逐段循环：一段一请求（全文仍在固定上下文里），校验不过时把具体差异回灌重试",
    )
    p_rewrite.add_argument(
        "--conversation",
        action="store_true",
        help="逐段对话：同一上下文里累积原文与已改内容（必须与 --per-paragraph 一起用，强制串行）",
    )
    # 两遍式（plan-v2）：第一遍只打动作标、不写正文；第二遍把计划当固定上下文
    p_rewrite.add_argument(
        "--plan-first",
        dest="plan_first",
        action="store_true",
        help="两遍式：先一次调用给全篇每段打动作标（展开 / 压缩 / 保持 / 配图，不写正文），"
        "再按原模式（分批 / 逐段）照这份计划改写；与 --only / --retry-failed / --from-signals 同用时，"
        "旧产物里已有计划才复用（读者 / 背景指纹 / 事实锚口径都对得上，0 次打标请求），"
        "否则重新全篇打标（多一次请求）",
    )
    # 逐段对话的上下文瘦身：只改「上一轮塞回 transcript 的东西」
    p_rewrite.add_argument(
        "--conversation-digest",
        dest="conversation_digest",
        action="store_true",
        help="逐段对话的上下文瘦身：把上一轮的整段 JSON 换成一行摘要（形如 p001 keep / p002 expand：正文）；"
        "必须与 --per-paragraph --conversation 一起用",
    )
    p_rewrite.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=f"逐段循环的并发上限（默认 {DEFAULT_CONCURRENCY}；1 = 串行；只在 --per-paragraph 下生效）",
    )
    p_rewrite.add_argument(
        "--validation-retries",
        type=int,
        default=DEFAULT_VALIDATION_RETRIES,
        help=f"逐段循环里「校验不过 → 带反馈重试」的次数上限（默认 {DEFAULT_VALIDATION_RETRIES}；0 = 只试一次）",
    )
    # 局部重跑（PLAN-actions-feedback §6.1）：三个参数互斥，交给 _cmd_rewrite 自己校验
    # （argparse 的互斥组报的是英文 usage 错误，这里要的是点名「三者互斥」的中文文案 + 退出码 2）
    p_rewrite.add_argument(
        "--only",
        default=None,
        metavar="p012,p014",
        help="只重跑这些段（逗号分隔的段 id；段 id 格式非法或不在材料里 → 退出码 2）；与另两个重跑参数互斥",
    )
    p_rewrite.add_argument(
        "--retry-failed",
        action="store_true",
        help="只重跑上次没被改写的段（既有 decisions.json 的 meta.model_errors ∪ unplanned_paragraphs）；"
        "为空则打印「没有需要重跑的段」+ 退出码 0、不写盘",
    )
    p_rewrite.add_argument(
        "--from-signals",
        action="store_true",
        help="只重跑行为信号算出的受影响段（events.jsonl → applySignals → affected_paragraphs；不写 profile.yaml）",
    )
    p_rewrite.set_defaults(handler=_cmd_rewrite)

    p_baseline = add_subcommand(
        "baseline",
        "一次性整篇改写基线（对比用：单次调用吃全文；只写 data/<id>/_eval/，不动三份主产物）",
    )
    p_baseline.add_argument("source_id")
    p_baseline.add_argument("--reader", default=DEFAULT_READER, help=f"读者 id（默认 {DEFAULT_READER}）")
    p_baseline.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="覆盖本次请求的 max_tokens（缺省不传；整篇输出较长时可显式给）",
    )
    p_baseline.set_defaults(handler=_cmd_baseline)

    p_check = add_subcommand("check", "锚点校验 + 事实锚校验（数字 / 引号引文 / 书名号《》+ 段缺失）")
    p_check.add_argument("source_id")
    # 事实锚口径来自读者画像（§14.1）：追加的**可选**参数，**缺省就是 DEFAULT_READER**（与
    # `ar rewrite --reader` 的缺省一致）。CLI 恒按这个读者判，**不回退**产物 meta.reader ——
    # 那条回退只对直接调 `check_fidelity(reader=None)` 的调用方生效（README §2）。
    p_check.add_argument(
        "--reader",
        default=DEFAULT_READER,
        help=f"读者 id：按他的画像偏好 pref_fact_anchor 决定事实锚口径（默认 {DEFAULT_READER}）",
    )
    p_check.set_defaults(handler=_cmd_check)

    p_serve = add_subcommand("serve", "启动本地服务（/api/* + 托管 web/）")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--reader", default=DEFAULT_READER, help=f"默认读者 id（默认 {DEFAULT_READER}）")
    p_serve.add_argument("--web-dir", default=str(DEFAULT_WEB_DIR), help="前端静态目录（默认 <项目根>/web）")
    p_serve.add_argument("--log-level", default="info")
    p_serve.set_defaults(handler=_cmd_serve)

    p_tools = add_subcommand("tools", "列出模型工具契约（只读：名称 / 描述 / 入参出参 schema）")
    p_tools.add_argument("--json", action="store_true", help="输出 JSON 数组（可被 json.loads 解析）")
    p_tools.set_defaults(handler=_cmd_tools)

    p_feedback = add_subcommand(
        "feedback",
        "行为信号回流：events.jsonl → 画像变更（打印将发生的变更；默认 dry-run，--apply 才写 profile.yaml；"
        "只折算 feedback.json 里没记过的事件，重复跑不叠加）",
    )
    p_feedback.add_argument("source_id")
    p_feedback.add_argument("--reader", default=DEFAULT_READER, help=f"读者 id（默认 {DEFAULT_READER}）")
    p_feedback.add_argument(
        "--apply",
        action="store_true",
        help="真的写 profile.yaml（缺省只打印将发生的变更；内容与现有文件逐字相同则不写盘）；"
        "同时把本次看过的事件 id 写进 profiles/<reader>/feedback.json（幂等水位线）",
    )
    p_feedback.set_defaults(handler=_cmd_feedback)

    p_routes = add_subcommand(
        "routes",
        "多源回退链：show 看当前链（各跳密钥来源 / 冷却状态）；import 从 YAML 生成 config.routes.yaml",
    )
    routes_sub = p_routes.add_subparsers(dest="routes_command", required=True)
    p_routes_show = add_nested("show", routes_sub, "打印当前生效的链（密钥只给来源，不给值）")
    p_routes_show.add_argument("--json", action="store_true", help="输出 JSON（可被 json.loads 解析）")
    p_routes_show.set_defaults(handler=_cmd_routes_show)
    p_routes_import = add_nested("import", routes_sub, "把一份 YAML 链片段写成 config.routes.yaml（覆盖前要求 --force）")
    p_routes_import.add_argument("path", nargs="?", help="YAML 文件；省略 = 用内置默认片段（= 本机 llm-auto 链）")
    p_routes_import.add_argument("--default", action="store_true", help="使用内置默认片段（与省略 path 等价）")
    p_routes_import.add_argument("--out", help="输出路径（默认 <项目根>/config.routes.yaml）")
    p_routes_import.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    p_routes_import.set_defaults(handler=_cmd_routes_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    """执行一次 CLI，返回退出码（不调用 ``sys.exit``，方便测试）。"""
    _force_utf8_streams()
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except ArError as exc:
        print(f"错误：{exc}", file=sys.stderr, flush=True)
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover - 交互场景
        print("已中断", file=sys.stderr)
        return 130


def entry() -> None:
    """console script 入口（``pyproject.toml`` 的 ``ar``）。"""
    raise SystemExit(main())
