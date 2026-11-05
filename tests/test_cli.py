"""CLI 面：7 个子命令都在（含 ``feedback``）、全局选项两个位置都生效、退出码约定。

（各子命令的业务行为分别在 test_ingest / test_analyze / test_rewrite 里测。）
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from ar.cli import build_parser
from ar.errors import EXIT_OK, EXIT_USAGE
from ar.events import events_path
from ar.paths import profile_path
from ar.server import build_app
from test_rewrite import write_fake_doc

SUBCOMMANDS = ("ingest", "analyze", "rewrite", "check", "serve", "tools", "feedback", "routes")


def test_help_lists_all_frozen_subcommands(run_cli) -> None:
    code, output = run_cli("--help")
    assert code == EXIT_OK
    for name in SUBCOMMANDS:
        assert name in output, name
    assert "ingest" in output and "--id" not in output  # --id 是子命令选项


def test_routes_show_reports_single_source_mode(run_cli, tmp_path: Path) -> None:
    """没配链时 ``ar routes show`` 必须说清「单源模式」并给出一条开启命令（不是空输出）。"""
    config = tmp_path / "config.local.json"
    config.write_text('{"base_url": "https://single.invalid/v1", "model": "fake/model"}', encoding="utf-8")
    code, output = run_cli("routes", "show", "--json", "--config", str(config))
    assert code == EXIT_OK, output
    payload = json.loads(output)
    assert payload["mode"] == "single" and payload["routes"] == []


def test_routes_import_writes_file_and_refuses_overwrite(run_cli, tmp_path: Path) -> None:
    target = tmp_path / "routes.yaml"
    code, output = run_cli("routes", "import", "--default", "--out", str(target))
    assert code == EXIT_OK, output
    assert target.is_file() and "commandcode" in target.read_text(encoding="utf-8")
    code, output = run_cli("routes", "import", "--default", "--out", str(target))
    assert code == EXIT_USAGE and "已存在" in output


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


def test_serve_handler_wires_app_and_uvicorn(
    run_cli, dirs: SimpleNamespace, monkeypatch, nokey_config: Path
) -> None:
    """把 serve 走到「启动 uvicorn」为止（不打真实端口），防止接线错误（漏 import 之类）。

    `--config` 必须显式给一份空配置：serve 的启动横幅要写「模型未配置」，
    不传就会去读开发机真实的 `config.local.json`（见 conftest 的 `nokey_config`）。
    """
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
        "--config",
        str(nokey_config),
        "--port",
        "8765",
    )
    assert code == EXIT_OK, output
    assert captured["kwargs"]["port"] == 8765
    assert captured["kwargs"]["host"] == "127.0.0.1"
    assert captured["app"] is not None
    assert "http://127.0.0.1:8765/" in output
    assert "未配置" in output  # 模型未配置要显式说出来


def test_cli_check_and_rewrite_report_their_exit_codes(
    run_cli, sample_md: Path, dirs: SimpleNamespace, nokey_config: Path
) -> None:
    """rewrite 没配模型 → 5；check 对不存在的材料 → 4；strict 档接口预留 → 3。"""
    # 「没配模型」由用例自己保证：显式传一份空配置，不读开发机的 config.local.json
    code, output = run_cli("--data-dir", str(dirs.data), "--config", str(nokey_config),
                           "rewrite", "sample")
    assert code == 5, output
    code, output = run_cli("--data-dir", str(dirs.data), "check", "nosuchdoc")
    assert code == 4, output
    code, output = run_cli("--data-dir", str(dirs.data), "rewrite", "sample", "--mode", "strict")
    assert code == 3, output
    assert "严格档" in output and "没有写入" in output


# ------------------------------------------------------------------ ar tools --
#: 六个模型工具（第五个是 PLAN-actions-feedback §5.1 的 compress_paragraph，
#: 第六个是两遍式的第一遍 plan_actions（plan-v2，2026-10-31 起两遍共享同一段前缀））
TOOL_NAMES = (
    "analyze_concepts",
    "rewrite_batch",
    "plan_actions",
    "explain_paragraph",
    "survey_followups",
    "compress_paragraph",
)


def test_tools_subcommand_prints_the_six_contracts(run_cli) -> None:
    """人类可读输出：六个工具的名称、描述、入参/出参 schema 都要在。"""
    code, output = run_cli("tools")
    assert code == EXIT_OK, output
    for name in TOOL_NAMES:
        assert name in output, name
    assert "描述：" in output
    assert output.count("入参（JSON Schema）：") == 6
    assert output.count("出参（JSON Schema）：") == 6
    # 提示词版本：rewrite 仍是 v7（计划块为空时那一格零字符插入，所以**不升版**），
    # compress 仍是 v3（这一批没动它），两遍式的第一遍单列 plan-v1
    assert "concepts-v2" in output and "rewrite-v7" in output and "explain-v2" in output and "survey-v1" in output
    assert "compress-v3" in output
    assert "plan-v2" in output


def test_tools_json_output_is_a_parseable_array(run_cli) -> None:
    code, output = run_cli("tools", "--json")
    assert code == EXIT_OK, output
    payload = json.loads(output)
    assert isinstance(payload, list) and [item["name"] for item in payload] == list(TOOL_NAMES)
    for item in payload:
        assert set(item) == {"name", "description", "parameters", "output", "prompt_version"}
        assert item["parameters"]["type"] == "object" and item["output"]["type"] == "object"


def test_tools_is_read_only_and_needs_no_config_or_model(run_cli, tmp_path: Path) -> None:
    """只读子命令：不读配置、不调模型；配置文件不存在也照样列契约。"""
    code, output = run_cli("tools", "--json", "--config", str(tmp_path / "nope.json"))
    assert code == EXIT_OK, output
    assert len(json.loads(output)) == len(TOOL_NAMES)
    assert json.loads(output) == json.loads(run_cli("tools", "--json")[1])


def test_tools_has_a_json_flag() -> None:
    parser = build_parser()
    assert parser.parse_args(["tools"]).command == "tools"
    assert parser.parse_args(["tools"]).json is False
    assert parser.parse_args(["tools", "--json"]).json is True


# ------------------------------------------------- 局部重跑的 CLI 形态（§6.1） --
def test_rewrite_rerun_flags_default_off_and_parse() -> None:
    """三个重跑参数都是**追加**的可选参数：缺省时行为与今天逐字不变。"""
    parser = build_parser()
    plain = parser.parse_args(["rewrite", "doc"])
    assert plain.only is None and plain.retry_failed is False and plain.from_signals is False
    assert parser.parse_args(["rewrite", "doc", "--only", "p012,p014"]).only == "p012,p014"
    assert parser.parse_args(["rewrite", "doc", "--retry-failed"]).retry_failed is True
    assert parser.parse_args(["rewrite", "doc", "--from-signals"]).from_signals is True


def test_rewrite_help_lists_the_three_rerun_flags(run_cli) -> None:
    code, output = run_cli("rewrite", "--help")
    assert code == EXIT_OK, output
    for flag in ("--only", "--retry-failed", "--from-signals"):
        assert flag in output, output


def test_feedback_is_a_new_subcommand_with_dry_run_default(run_cli) -> None:
    args = build_parser().parse_args(["feedback", "doc"])
    assert args.command == "feedback" and args.reader == "local-default" and args.apply is False
    assert build_parser().parse_args(["feedback", "doc", "--apply"]).apply is True
    code, output = run_cli("feedback", "--help")
    assert code == EXIT_OK, output
    assert "--apply" in output and "--reader" in output


def test_feedback_without_ingested_material_is_usage_error(run_cli, dirs: SimpleNamespace) -> None:
    code, output = run_cli("--data-dir", str(dirs.data), "feedback", "nosuchdoc")
    assert code == EXIT_USAGE, output
    assert "ingest" in output


# --------------------------------------------------------- ar feedback（§7.4） --
def _feedback_setup(dirs: SimpleNamespace, *, profile_state: str = "known") -> Path:
    """造「已摄取 + 有概念清单 + 有画像 + 有两条求助事件」的现场（**零模型调用**）。

    两条 ``explain`` 落在 c01 的两个段上 ⇒ score −2.0、同向证据 2 条 ⇒ 过了阈值，
    ``known`` 会降一档到 ``partial``（一次只升降一档）。
    """
    from ar.profile import save_profile

    write_fake_doc(dirs)
    save_profile(
        dirs.profiles,
        "local-default",
        {
            "reader": "local-default",
            "background": {},
            "concepts": {"c01": {"state": profile_state, "confidence": 0.9}},
            "preferences": [],
            "overrides": [],
        },
    )
    events = [
        {
            "v": 1,
            "id": f"ev-{pid}",
            "at": "2026-10-13T18:00:00+08:00",
            "kind": "explain",
            "source_id": "fake-doc",
            "reader": "local-default",
            "paragraph": pid,
            "explain_kind": "explain",
            "scope": "paragraph",
            "cached": False,
            "degraded": False,
        }
        for pid in ("p002", "p005")
    ]
    path = events_path(dirs.profiles, "local-default")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events), encoding="utf-8"
    )
    return profile_path(dirs.profiles, "local-default")


def _read_profile(dirs: SimpleNamespace) -> dict:
    import yaml

    return yaml.safe_load(profile_path(dirs.profiles, "local-default").read_text(encoding="utf-8"))


def test_feedback_dry_run_prints_the_changes_without_writing(
    run_cli, dirs: SimpleNamespace, monkeypatch
) -> None:
    path = _feedback_setup(dirs)
    before = path.read_bytes()
    monkeypatch.delenv("AR_API_KEY", raising=False)

    code, output = run_cli("--data-dir", str(dirs.data), "--profiles-dir", str(dirs.profiles),
                           "feedback", "fake-doc", "--reader", "local-default")
    assert code == EXIT_OK, output
    assert "c01：known → partial" in output, output
    assert "追加证据" in output
    assert "受影响段" in output and "p002" in output and "p005" in output
    assert "dry-run" in output and "没有写盘" in output
    assert path.read_bytes() == before, "dry-run 动了 profile.yaml（契约 §7.4：默认只打印）"

    # 同一个现场跑 --apply：这次真的写盘（state / confidence / evidence / signals 都动过）
    code, output = run_cli("--data-dir", str(dirs.data), "--profiles-dir", str(dirs.profiles),
                           "feedback", "fake-doc", "--reader", "local-default", "--apply")
    assert code == EXIT_OK, output
    assert "画像已写回" in output
    saved = _read_profile(dirs)
    assert saved["concepts"]["c01"]["state"] == "partial"
    assert saved["concepts"]["c01"]["evidence"], "证据没有被追加"
    assert saved["concepts"]["c01"]["signals"]["score"] == -2.0
    assert path.read_bytes() != before, "--apply 没有改动画像文件"


def test_feedback_apply_does_not_rewrite_identical_content(
    run_cli, dirs: SimpleNamespace, monkeypatch
) -> None:
    """内容没变就不写盘（画像指纹 = 路径 / 字节数 / mtime_ns，无谓写盘会让讲解缓存全失效）。"""
    import os

    from ar.profile import save_profile

    write_fake_doc(dirs)
    save_profile(
        dirs.profiles,
        "local-default",
        {
            "reader": "local-default",
            "background": {},
            "concepts": {"c01": {"state": "known", "confidence": 0.9}},
            "preferences": [],
            "overrides": [],
        },
    )
    # 事件里没有任何会产生信号的 kind（read_abandon 按契约不产生信号）⇒ 画像一个字符都不变
    path = events_path(dirs.profiles, "local-default")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "v": 1,
                "id": "ev-abandon",
                "at": "2026-10-13T18:00:00+08:00",
                "kind": "read_abandon",
                "source_id": "fake-doc",
                "reader": "local-default",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    profile_file = profile_path(dirs.profiles, "local-default")
    sentinel = 1_600_000_000_000_000_000  # 固定的过去时间：写盘就一定会把它改掉
    os.utime(profile_file, ns=(sentinel, sentinel))
    monkeypatch.delenv("AR_API_KEY", raising=False)

    code, output = run_cli("--data-dir", str(dirs.data), "--profiles-dir", str(dirs.profiles),
                           "feedback", "fake-doc", "--reader", "local-default", "--apply")
    assert code == EXIT_OK, output
    assert "状态变更：0 个概念" in output, output
    assert profile_file.stat().st_mtime_ns == sentinel, "内容逐字相同却重新写盘了（白换一次指纹）"


# ------------------------------------------------- ar feedback 的幂等（F2） --
def _apply_feedback(run_cli, dirs: SimpleNamespace):  # noqa: ANN202 - 用例里的小工具
    return run_cli(
        "--data-dir", str(dirs.data), "--profiles-dir", str(dirs.profiles),
        "feedback", "fake-doc", "--reader", "local-default", "--apply",
    )


def test_feedback_apply_is_idempotent(run_cli, dirs: SimpleNamespace, monkeypatch) -> None:
    """**F2**：同一份事件连跑三次 ``--apply``，第二次起不再叠加（``feedback.json`` 水位线）。

    没有水位线时每跑一次都会重读全部事件、重复折算：``score`` −1.0 → −2.0 → −3.0，
    ``signals.history`` / ``evidence`` 条数 1 → 2 → 3，最终把 ``state`` 推过阈值。
    """
    _feedback_setup(dirs)
    monkeypatch.delenv("AR_API_KEY", raising=False)
    feedback_file = dirs.profiles / "local-default" / "feedback.json"

    code, output = _apply_feedback(run_cli, dirs)
    assert code == EXIT_OK, output
    assert feedback_file.is_file(), "--apply 没有写回流水位线"
    record = json.loads(feedback_file.read_text(encoding="utf-8"))
    assert record["processed"] == ["ev-p002", "ev-p005"]
    assert record["last_at"] == "2026-10-13T18:00:00+08:00"
    assert record["reader"] == "local-default" and record["updated_at"]

    entry = _read_profile(dirs)["concepts"]["c01"]
    assert entry["state"] == "partial" and entry["signals"]["score"] == -2.0
    history, evidence = len(entry["signals"]["history"]), len(entry["evidence"])
    sentinel = feedback_file.stat().st_mtime_ns

    for _ in range(2):  # 第二、三次：一个事件都不该再折算
        code, output = _apply_feedback(run_cli, dirs)
        assert code == EXIT_OK, output
        assert "状态变更：0 个概念" in output, output
        assert "没有未处理的事件" in output, output
        current = _read_profile(dirs)["concepts"]["c01"]
        assert current["signals"]["score"] == -2.0, "重复 --apply 又叠加了 score"
        assert current["state"] == "partial", "重复 --apply 把 state 推过了阈值"
        assert len(current["signals"]["history"]) == history, "history 又长了一条"
        assert len(current["evidence"]) == evidence, "evidence 又多了一条"
    assert feedback_file.stat().st_mtime_ns == sentinel, "没有新事件却重写了 feedback.json"


def test_feedback_apply_counts_new_events_only(run_cli, dirs: SimpleNamespace, monkeypatch) -> None:
    """新事件（**新的 id**）仍然会被计入：水位线不是「从此闭嘴」。"""
    _feedback_setup(dirs)
    monkeypatch.delenv("AR_API_KEY", raising=False)
    assert _apply_feedback(run_cli, dirs)[0] == EXIT_OK
    assert _read_profile(dirs)["concepts"]["c01"]["signals"]["score"] == -2.0

    path = events_path(dirs.profiles, "local-default")
    fresh = {
        "v": 1, "id": "ev-p002-again", "at": "2026-10-13T18:30:00+08:00", "kind": "explain",
        "source_id": "fake-doc", "reader": "local-default", "paragraph": "p002",
        "explain_kind": "explain", "scope": "paragraph", "cached": False, "degraded": False,
    }
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(fresh, ensure_ascii=False) + "\n")

    code, output = _apply_feedback(run_cli, dirs)
    assert code == EXIT_OK, output
    assert "本次折算 1 条" in output, output
    entry = _read_profile(dirs)["concepts"]["c01"]
    assert entry["signals"]["score"] == -3.0, "新事件没有被计入"
    assert len(entry["signals"]["history"]) == 3
    assert json.loads((dirs.profiles / "local-default" / "feedback.json").read_text(encoding="utf-8"))[
        "processed"
    ] == ["ev-p002", "ev-p005", "ev-p002-again"]


def test_feedback_missing_or_broken_waterline_is_visible(run_cli, dirs: SimpleNamespace, monkeypatch) -> None:
    """``feedback.json`` 缺失 / 损坏 → 按「全部事件都未处理」处理，但**不许静默**（各给一条降级）。"""
    _feedback_setup(dirs)
    monkeypatch.delenv("AR_API_KEY", raising=False)
    feedback_file = dirs.profiles / "local-default" / "feedback.json"

    # ① 缺失（第一次跑）：照常折算 + 一条说明
    code, output = run_cli("--data-dir", str(dirs.data), "--profiles-dir", str(dirs.profiles),
                           "feedback", "fake-doc", "--reader", "local-default")
    assert code == EXIT_OK, output
    assert "还不存在" in output and "全部事件都未处理" in output, output
    assert "c01：known → partial" in output, output
    assert not feedback_file.exists(), "dry-run 不该建 feedback.json"

    # ② 损坏（不是 JSON / 不是对象 / processed 不是数组）：按全部未处理办，并说明
    feedback_file.write_text("{ 这不是 JSON", encoding="utf-8")
    code, output = run_cli("--data-dir", str(dirs.data), "--profiles-dir", str(dirs.profiles),
                           "feedback", "fake-doc", "--reader", "local-default", "--apply")
    assert code == EXIT_OK, output
    assert "读不出来" in output and "全部事件都未处理" in output, output
    assert json.loads(feedback_file.read_text(encoding="utf-8"))["processed"] == ["ev-p002", "ev-p005"], \
        "--apply 该用正确内容把坏文件重写回来"

    feedback_file.write_text("[]", encoding="utf-8")
    code, output = run_cli("--data-dir", str(dirs.data), "--profiles-dir", str(dirs.profiles),
                           "feedback", "fake-doc", "--reader", "local-default")
    assert code == EXIT_OK, output
    assert "不是一个 JSON 对象" in output, output

    feedback_file.write_text('{"processed": "ev-p002"}', encoding="utf-8")
    code, output = run_cli("--data-dir", str(dirs.data), "--profiles-dir", str(dirs.profiles),
                           "feedback", "fake-doc", "--reader", "local-default")
    assert code == EXIT_OK, output
    assert "processed 不是数组" in output, output
