"""CLI 面：5 个子命令都在、全局选项两个位置都生效、退出码约定。

（各子命令的业务行为分别在 test_ingest / test_analyze 里测。）
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ar.cli import build_parser
from ar.errors import EXIT_OK, EXIT_USAGE
from ar.server import build_app

SUBCOMMANDS = ("ingest", "analyze", "rewrite", "check", "serve")


def test_help_lists_all_frozen_subcommands(run_cli) -> None:
    code, output = run_cli("--help")
    assert code == EXIT_OK
    for name in SUBCOMMANDS:
        assert name in output, name
    assert "ingest" in output and "--id" not in output  # --id 是子命令选项


def test_version_flag(run_cli) -> None:
    code, output = run_cli("--version")
    assert code == EXIT_OK
    assert "adaptive-reader" in output


def test_missing_subcommand_is_usage_error(run_cli) -> None:
    code, output = run_cli()
    assert code == EXIT_USAGE, output


def test_unknown_subcommand_is_usage_error(run_cli) -> None:
    code, output = run_cli("frobnicate")
    assert code == EXIT_USAGE, output


def test_analyze_has_check_only_flag() -> None:
    parser = build_parser()
    args = parser.parse_args(["analyze", "sample", "--check-only"])
    assert args.check_only is True
    assert args.data_dir  # 全局默认值来自主解析器（SUPPRESS 只用在子解析器上）


def test_global_options_work_before_and_after_subcommand() -> None:
    parser = build_parser()
    before = parser.parse_args(["--data-dir", "A", "check", "doc"])
    after = parser.parse_args(["check", "doc", "--data-dir", "A"])
    assert before.data_dir == after.data_dir == "A"
    default = parser.parse_args(["check", "doc"])
    assert default.data_dir.endswith("data")
    assert default.profiles_dir.endswith("profiles")
    assert default.config.endswith("config.local.json")


def test_serve_defaults() -> None:
    args = build_parser().parse_args(["serve"])
    assert args.port == 8765
    assert args.host == "127.0.0.1"
    assert args.reader == "local-default"


def test_serve_help_mentions_web_dir_and_reader(run_cli) -> None:
    code, output = run_cli("serve", "--help")
    assert code == EXIT_OK
    assert "--web-dir" in output and "--reader" in output


def test_serve_app_builds_with_missing_web_dir(dirs: SimpleNamespace) -> None:
    """``ar serve`` 用的同一个 build_app：web/ 不存在也能构造，不崩。"""
    app = build_app(data_dir=dirs.data, profiles_dir=dirs.profiles, web_dir=dirs.web)
    assert app is not None
    assert not dirs.web.exists()


def test_serve_handler_wires_app_and_uvicorn(run_cli, dirs: SimpleNamespace, monkeypatch) -> None:
    """把 serve 走到「启动 uvicorn」为止（不打真实端口），防止接线错误（漏 import 之类）。"""
    import uvicorn

    captured: dict = {}

    def fake_run(app, **kwargs):  # noqa: ANN001, ANN003
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    code, output = run_cli(
        "serve",
        "--data-dir",
        str(dirs.data),
        "--profiles-dir",
        str(dirs.profiles),
        "--web-dir",
        str(dirs.web),
        "--port",
        "8765",
    )
    assert code == EXIT_OK, output
    assert captured["kwargs"]["port"] == 8765
    assert captured["kwargs"]["host"] == "127.0.0.1"
    assert captured["app"] is not None
    assert "http://127.0.0.1:8765/" in output
    assert "未配置" in output  # 模型未配置要显式说出来


def test_cli_check_and_rewrite_report_their_exit_codes(run_cli, sample_md: Path, dirs: SimpleNamespace) -> None:
    """rewrite 没配模型 → 5；check 对不存在的材料 → 4；strict 档接口预留 → 3。"""
    code, output = run_cli("--data-dir", str(dirs.data), "rewrite", "sample")
    assert code == 5, output
    code, output = run_cli("--data-dir", str(dirs.data), "check", "nosuchdoc")
    assert code == 4, output
    code, output = run_cli("--data-dir", str(dirs.data), "rewrite", "sample", "--mode", "strict")
    assert code == 3, output
    assert "严格档" in output and "没有写入" in output
