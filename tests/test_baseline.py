"""一次性基线（oneshot-v1）：写 _eval/、不碰主产物；缺段补位、事实锚照旧比对。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from ar.baseline import EVAL_DIRNAME, ONESHOT_JSON, ONESHOT_MD, run_oneshot
from ar.jsonio import read_json
from test_rewrite import write_fake_doc


def _payload_for(doc: dict[str, Any], overrides: dict[str, str] | None = None, drop: tuple[str, ...] = ()) -> str:
    """按原文逐段造一份基线输出（overrides 换正文，drop 模拟模型漏段）。"""
    changed = overrides or {}
    items = [
        {"paragraph": p["id"], "text": changed.get(p["id"], p["text"])}
        for p in doc["paragraphs"]
        if p["id"] not in drop
    ]
    return json.dumps({"paragraphs": items}, ensure_ascii=False)


def test_oneshot_writes_eval_artifacts_only(dirs: SimpleNamespace, fake_chat_client) -> None:
    """成功的基线只写 _eval/ 两个文件，三份主产物一个都不许出现。"""
    doc = write_fake_doc(dirs)
    second = doc["paragraphs"][1]
    handler = lambda messages: _payload_for(doc, {second["id"]: second["text"] + "（基线补一句）"})  # noqa: E731
    outcome = run_oneshot(
        "fake-doc", dirs.data, dirs.profiles, "local-default", fake_chat_client(handler=handler)
    )
    assert outcome.ok is True
    assert outcome.json_path is not None and outcome.json_path.is_file()
    assert outcome.markdown_path is not None and outcome.markdown_path.is_file()
    payload = read_json(outcome.json_path)
    assert payload["kind"] == "oneshot-baseline"
    assert payload["prompt_version"] == "oneshot-v1"
    assert payload["returned_count"] == len(doc["paragraphs"])
    assert payload["changed_count"] == 1
    assert payload["missing"] == [] and payload["extra"] == []
    assert payload["counts"]["compared"] == len(doc["paragraphs"])
    assert payload["problems"] == []
    target = dirs.data / "fake-doc"
    for name in ("decisions.json", "changes.json", "rewritten.md"):
        assert not (target / name).exists(), f"基线不许写主产物 {name}"


def test_oneshot_reports_fact_anchor_mismatches(dirs: SimpleNamespace, fake_chat_client) -> None:
    """基线同样过事实锚比对：少数字 / 编引文都要点名。"""
    doc = write_fake_doc(dirs)
    target_pid = next(p["id"] for p in doc["paragraphs"] if "1859" in p["text"])
    handler = lambda messages: _payload_for(doc, {target_pid: "换了个说法，一个数字都不留。"})  # noqa: E731
    outcome = run_oneshot(
        "fake-doc", dirs.data, dirs.profiles, "local-default", fake_chat_client(handler=handler)
    )
    assert outcome.ok is True
    assert outcome.problems, "少了事实锚却没有报问题"
    assert any("1859" in item or "数字" in item for item in outcome.problems)
    assert outcome.mismatches, "结构化 mismatches 也要落盘"
    payload = read_json(outcome.json_path)
    assert payload["problems"] == outcome.problems


def test_oneshot_missing_paragraph_falls_back_to_source(dirs: SimpleNamespace, fake_chat_client) -> None:
    """模型漏段：基线稿用原文补位，missing 如实上报。"""
    doc = write_fake_doc(dirs)
    dropped = doc["paragraphs"][2]
    handler = lambda messages: _payload_for(doc, drop=(dropped["id"],))  # noqa: E731
    outcome = run_oneshot(
        "fake-doc", dirs.data, dirs.profiles, "local-default", fake_chat_client(handler=handler)
    )
    assert outcome.ok is True
    assert outcome.missing == [dropped["id"]]
    markdown = outcome.markdown_path.read_text(encoding="utf-8")
    assert dropped["text"] in markdown
    assert any(dropped["id"] in item for item in outcome.problems)


def test_oneshot_bad_json_writes_raw_and_fails_visibly(dirs: SimpleNamespace, fake_chat_client) -> None:
    """输出读不通：ok=false + 原因 + 原始回复留证（不静默、不写 markdown）。"""
    write_fake_doc(dirs)
    handler = lambda messages: "这不是 JSON"  # noqa: E731
    outcome = run_oneshot(
        "fake-doc", dirs.data, dirs.profiles, "local-default", fake_chat_client(handler=handler)
    )
    assert outcome.ok is False
    assert "读不通" in outcome.reason
    eval_dir = dirs.data / "fake-doc" / EVAL_DIRNAME
    assert (eval_dir / "oneshot.raw.txt").is_file()
    assert (eval_dir / ONESHOT_JSON).is_file()
    assert not (eval_dir / ONESHOT_MD).exists()


def test_oneshot_model_failure_is_returned_not_raised(dirs: SimpleNamespace, fake_chat_client) -> None:
    """调用失败（异常）也走返回值：ok=false + 脱敏原因，不抛。"""
    write_fake_doc(dirs)

    def handler(messages):  # noqa: ANN001
        raise RuntimeError("网关抖了")

    outcome = run_oneshot(
        "fake-doc", dirs.data, dirs.profiles, "local-default", fake_chat_client(handler=handler)
    )
    assert outcome.ok is False
    assert "网关抖了" in outcome.reason
    # 失败也留证：一次运行记录（usage / 尝试账 / 原因），不写 markdown
    assert outcome.json_path is not None and outcome.json_path.is_file()
    payload = read_json(outcome.json_path)
    assert payload["ok"] is False and "网关抖了" in payload["reason"]
    assert not (dirs.data / "fake-doc" / "rewritten.md").exists()
