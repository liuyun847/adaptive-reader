"""命令行入口：契约 §4 冻结的 5 个子命令。

```
uv run ar ingest  <文件路径> --id <source_id>
uv run ar analyze <source_id> [--check-only]
uv run ar rewrite <source_id> --reader local-default [--mode free]
uv run ar check   <source_id>                           # 锚点稳定性 + 事实锚（数字/引文/书名+段缺失）
uv run ar serve   --port 8765
```

退出码：0 成功 / 2 用法或输入错误（含**校验所需产物缺失 / 不可读 / 为空**）/ 3 尚未实现 /
4 校验失败（含**检查器空跑**）/ 5 模型未配置或调用失败 / 6 模型失败导致**有段落未被改写**。

``--mode strict``（严格档）是**预留插口**：未实现，遇到就明确报错（退出码 3），不静默按 free 跑。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .analyze import check_inventory_file, run_analyze
from .anchors import verify_source
from .config import Config, load_config
from .errors import (
    EXIT_MODEL_PARTIAL,
    EXIT_NOT_IMPLEMENTED,
    EXIT_OK,
    ArError,
    InputError,
    ModelPartiallyFailed,
    ModelUnavailable,
    NotImplementedYet,
    ValidationFailed,
)
from .ingest import ingest
from .model import ModelClient
from .paths import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_DATA_DIR,
    DEFAULT_PROFILES_DIR,
    DEFAULT_READER,
    DEFAULT_WEB_DIR,
    check_reader_id,
    check_source_id,
)
from .rewrite import DEFAULT_MAX_CHARS_PER_BATCH
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


# -------------------------------------------------------------------- rewrite --
def _cmd_rewrite(args: argparse.Namespace) -> int:
    from .rewrite import (
        DEFAULT_MODE,
        STRICT_NOT_IMPLEMENTED,
        actions_label,
        incomplete_reasons,
        run_rewrite,
    )

    source_id = check_source_id(args.source_id)
    reader = check_reader_id(args.reader)
    mode = args.mode or DEFAULT_MODE
    if mode == "strict":
        # 严格档接口预留：未实现就明确报错，绝不静默按 free 跑（用户 2026-10-05 拍板）
        raise NotImplementedYet(STRICT_NOT_IMPLEMENTED)

    config: Config = load_config(args.config)
    if config.unknown_keys:
        _warn(f"配置里有未知字段（已忽略）：{', '.join(config.unknown_keys)}")
    if not config.model_configured:
        raise ModelUnavailable(
            "模型未配置：设置环境变量 AR_API_KEY，或在 config.local.json 里填 api_key。"
            "没有模型就无法逐段决策与改写（不静默假装成功）"
        )

    client = _make_client(config)
    outcome = run_rewrite(
        source_id,
        args.data_dir,
        args.profiles_dir,
        reader,
        client,
        mode=mode,
        max_chars_per_batch=args.max_chars_per_batch,
    )

    counts = outcome.action_counts()
    # 有段落因模型问题没被改写（不是模型主动「保持」）⇒ 不许打印「✓ 改写完成」
    incomplete = outcome.incomplete
    if incomplete:
        _say(f"⚠ 改写不完整：{source_id}（读者 {reader}，mode={outcome.mode}）")
    else:
        _say(f"✓ 改写完成：{source_id}（读者 {reader}，mode={outcome.mode}）")
    _say(f"  产物：{outcome.decisions_path}")
    _say(f"       {outcome.rewritten_path}")
    _say(f"       {outcome.changes_path}")
    _say(f"  决策：{len(outcome.decisions)} 段（展开 {counts['expand']} / 保持 {counts['keep']}）")
    implemented = " / ".join(actions_label(action) for action in ("expand", "keep"))
    _say(
        f"  动作实现范围：{implemented}；压缩 / 配图（枚举内的未实现动作）遇到按保持处理并标 deferred；"
        "枚举外的动作（含模型没给 action）按保持处理、不标 deferred"
    )
    if outcome.deferred_count:
        deferred_ids = "、".join(d.paragraph for d in outcome.decisions if d.deferred)
        _say(f"  按保持处理的未实现动作：{outcome.deferred_count} 段（{deferred_ids}）")
    _say(f"  画像：{outcome.profile_note}")
    _say(f"  模型：{outcome.model}；提示词版本：{outcome.prompt_version}")
    _say(f"  {outcome.usage_note}（{outcome.request_count} 次请求合并了 {len(outcome.decisions)} 段）")
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
        _say("  模型没有给出决策的段（按保持处理、未改写）：" + "、".join(outcome.unplanned_paragraphs))
    _say(
        "  退出码 6：事实锚只能证明「没改坏」，证明不了「真的改写了」——"
        "有段落一个字没改就必须报出来"
    )
    _say(f"  下一步：`uv run ar rewrite {source_id} --reader {reader}` 重跑补齐（产物已落盘，可直接重跑覆盖）")
    _say(f"        `uv run ar check {source_id}` 也会以退出码 {EXIT_MODEL_PARTIAL} 反映这件事")
    return EXIT_MODEL_PARTIAL


# ---------------------------------------------------------------------- check --
def _cmd_check(args: argparse.Namespace) -> int:
    from .rewrite import check_fidelity, fidelity_problems, incomplete_reasons

    source_id = check_source_id(args.source_id)
    problems = verify_source(args.data_dir, source_id)
    if problems:
        raise ValidationFailed(problems, headline="锚点校验失败（source.meta.json 与 source.md 不一致）")
    _say(f"✓ 锚点校验通过：{source_id}（段落 id / text_hash / text 与 source.md 一致）")

    outcome = check_fidelity(source_id, args.data_dir)
    counts = outcome.counts
    if outcome.problems:
        # 产物缺失 / 不可读 / 为空：没得比就不许报通过（沿用「rewritten.md 缺失」的退出码语义：2）
        raise InputError(
            "事实锚校验所需的产物缺失 / 不可读 / 为空，无法证明保真（不静默通过）：\n"
            + "\n".join(f"  - {problem}" for problem in outcome.problems)
        )
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
    _say(
        f"✓ 事实锚校验通过：逐段比对 {compared} 段——"
        f"数字 {counts.get('numbers', 0)} 项 / 引号引文 {counts.get('quotes', 0)} 项 / "
        f"书名号《》{counts.get('books', 0)} 项，全部与原文一致；没有段缺失"
    )
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

    p_ingest = add_subcommand("ingest", "摄取本地 UTF-8 文本 → source.md + source.meta.json")
    p_ingest.add_argument("path", help="输入文件（Markdown / TXT，UTF-8）")
    p_ingest.add_argument("--id", required=True, help="source_id（同时是 data/<id>/ 目录名）")
    p_ingest.set_defaults(handler=_cmd_ingest)

    p_analyze = add_subcommand("analyze", "调模型抽概念清单 → concepts.json")
    p_analyze.add_argument("source_id")
    p_analyze.add_argument("--check-only", action="store_true", help="只校验已有 concepts.json（不调模型）")
    p_analyze.set_defaults(handler=_cmd_analyze)

    p_rewrite = add_subcommand("rewrite", "逐段决策 + 改写（expand / keep）→ decisions.json + rewritten.md + changes.json")
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
    p_rewrite.set_defaults(handler=_cmd_rewrite)

    p_check = add_subcommand("check", "锚点校验 + 事实锚校验（数字 / 引号引文 / 书名号《》+ 段缺失）")
    p_check.add_argument("source_id")
    p_check.set_defaults(handler=_cmd_check)

    p_serve = add_subcommand("serve", "启动本地服务（/api/* + 托管 web/）")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--reader", default=DEFAULT_READER, help=f"默认读者 id（默认 {DEFAULT_READER}）")
    p_serve.add_argument("--web-dir", default=str(DEFAULT_WEB_DIR), help="前端静态目录（默认 <项目根>/web）")
    p_serve.add_argument("--log-level", default="info")
    p_serve.set_defaults(handler=_cmd_serve)

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
