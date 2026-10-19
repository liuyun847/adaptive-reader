"""行为事件流 ``profiles/<reader>/events.jsonl``（PLAN-actions-feedback §7.1–§7.3 的库侧）。

覆盖：落盘形态（一行一条 / ``ensure_ascii=False`` / 追加写 / 结尾换行）、坏行可见、幂等键与去重、
服务端盖章、「事件 → 信号」的折算（权重表 / 短停留不当负信号 / 偏好 evidence），
以及回流水位线 ``feedback.json`` 的读写与「已处理」判定（2026-10-13 复核修复 F2 的幂等口径）。
**全程不发任何模型请求**：本模块压根没有模型调用。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ar.errors import EXIT_USAGE, InputError, WriteError
from ar.events import (
    EVENT_KINDS,
    LONG_DWELL_MS,
    PREFERENCE_ID,
    SELECTION_WEIGHT,
    SHORT_DWELL_MS,
    SIGNAL_WEIGHTS,
    append_events,
    dedup,
    event_id,
    events_path,
    feedback_record_path,
    merge_processed,
    pending_events,
    processed_ids,
    read_events,
    read_feedback_record,
    signals_from_events,
    validate_event,
    write_feedback_record,
)

AT = "2026-10-13T18:20:00+08:00"

INVENTORY = {
    "concepts": [
        {"id": "c01", "name": "商品", "paragraphs": ["p001", "p002"]},
        {"id": "c02", "name": "价值", "paragraphs": ["p002"]},
    ]
}


def _signal_map(signals: list[dict]) -> dict[tuple[str, str, str], float]:
    """``(concept, kind, paragraph) → weight``：只看权重表，不看顺序。"""
    return {(item["concept"], item["kind"], item["paragraph"]): item["weight"] for item in signals}


# ------------------------------------------------------------------ 路径 / 落盘 --
def test_events_path_sits_next_to_profile(dirs: SimpleNamespace) -> None:
    assert events_path(dirs.profiles, "local-default") == dirs.profiles / "local-default" / "events.jsonl"
    with pytest.raises(InputError, match="reader id 非法"):
        events_path(dirs.profiles, "../escape")


def test_append_then_read_roundtrip_writes_one_json_per_line(dirs: SimpleNamespace) -> None:
    first = {"v": 1, "id": "e1", "at": AT, "kind": "explain", "source_id": "doc", "reader": "r", "paragraph": "p001"}
    second = {"v": 1, "id": "e2", "at": AT, "kind": "skip", "source_id": "doc", "reader": "r", "reason": "太啰嗦，跳过"}
    assert append_events(dirs.profiles, "r", [first]) == 1
    assert append_events(dirs.profiles, "r", [second]) == 1  # 追加，不重写

    path = events_path(dirs.profiles, "r")
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n") and text.count("\n") == 2
    # ensure_ascii=False：中文原样落盘，不是 \uXXXX 转义
    assert "太啰嗦，跳过" in text and "\\u" not in text

    events, notes = read_events(dirs.profiles, "r")
    assert notes == []
    assert [item["id"] for item in events] == ["e1", "e2"]
    assert events == [first, second]


def test_append_events_creates_reader_dir_and_tolerates_nothing_to_write(dirs: SimpleNamespace) -> None:
    assert append_events(dirs.profiles, "fresh-reader", []) == 0
    assert not events_path(dirs.profiles, "fresh-reader").exists()
    assert append_events(dirs.profiles, "fresh-reader", [{"id": "x", "kind": "read_complete"}]) == 1
    assert events_path(dirs.profiles, "fresh-reader").is_file()


def test_append_events_unwritable_target_fails_fast_with_a_readable_error(
    dirs: SimpleNamespace,
) -> None:
    """事件流写不进去 ⇒ 立刻抛 `WriteError`（点名目录 + 原始 errno），与其它写盘入口同一口径。

    **不打桩**的真实坏路径：把 `profiles/<reader>` 造成一个同名**文件**，`mkdir(parents=True)`
    必然失败（`EEXIST` / `ENOTDIR` 一类），不需要 `icacls`。

    这里另有一条**契约边界**要钉住：事件流是「尽力而为」的采集（DESIGN §6），
    所以 HTTP 层必须照旧回 200 + `ok:false` + `written:0` + `degraded`（见
    `tests/test_server.py::test_events_write_failure_is_visible_and_never_faked`），
    而不能因为换成 `WriteError` 就退化成 500——`ar.server` 的 except 里因此同时列了
    `WriteError` 与 `OSError`。
    """
    blocker = dirs.profiles / "r"
    blocker.write_text("我不是目录", encoding="utf-8")

    with pytest.raises(WriteError) as excinfo:
        append_events(dirs.profiles, "r", [{"id": "x", "kind": "read_complete"}])

    message = str(excinfo.value)
    assert str(blocker) in message, f"错误里必须点名是哪个目录：{message}"
    assert "errno" in message, f"错误里必须带原始 errno：{message}"
    assert excinfo.value.exit_code == EXIT_USAGE, "CLI 上归「用法 / 输入错误」那一档（退出码 2）"
    assert blocker.is_file(), "坏路径不许被改写（那个文件还是文件）"


def test_append_events_opens_the_file_exactly_once_and_never_retries(
    dirs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """追加写**只开一次文件**（没有重试循环）：失败立刻抛，绝不空转。

    事件流是唯一不用「临时文件 + replace」的写盘入口（纯追加的日志不该整文件重写，
    见 `ar.events.append_events` 的说明），所以这里数的是 `open` 的调用次数而不是
    `os.open`——它走的是内置 `open`。`monkeypatch` 负责把内置 `open` 换回来。
    """
    import builtins

    real_open = builtins.open
    calls: list[str] = []
    target = events_path(dirs.profiles, "r")

    def counting_open(file, *args, **kwargs):
        if str(file) == str(target):
            calls.append(str(file))
            raise PermissionError(13, "Permission denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)

    with pytest.raises(WriteError) as excinfo:
        append_events(dirs.profiles, "r", [{"id": "x", "kind": "read_complete"}])

    assert len(calls) == 1, f"追加写必须一次就失败，实际开了 {len(calls)} 次"
    assert str(target.parent) in str(excinfo.value)


def test_read_events_reports_broken_lines_instead_of_losing_the_stream(dirs: SimpleNamespace) -> None:
    path = events_path(dirs.profiles, "r")
    path.parent.mkdir(parents=True)
    good = json.dumps({"id": "e1", "kind": "dwell", "source_id": "doc", "at": AT}, ensure_ascii=False)
    path.write_text(f"{good}\n{{不是 JSON\n[1, 2]\n\n", encoding="utf-8")

    events, notes = read_events(dirs.profiles, "r")
    assert [item["id"] for item in events] == ["e1"]
    joined = " / ".join(notes)
    assert "第 2 行" in joined and "第 3 行" in joined


def test_read_events_on_missing_file_is_empty_not_an_error(dirs: SimpleNamespace) -> None:
    assert read_events(dirs.profiles, "nobody") == ([], [])


# ------------------------------------------------------ 回流水位线（F2，§7.4） --
def test_feedback_record_sits_next_to_events_and_roundtrips(dirs: SimpleNamespace) -> None:
    assert feedback_record_path(dirs.profiles, "r") == dirs.profiles / "r" / "feedback.json"
    with pytest.raises(InputError, match="reader id 非法"):
        feedback_record_path(dirs.profiles, "../escape")

    # 缺失：按「全部事件都未处理」办 + 一条说明（第一次跑属正常，但不静默）
    record, notes = read_feedback_record(dirs.profiles, "r")
    assert record == {} and len(notes) == 1 and "还不存在" in notes[0]

    path = write_feedback_record(dirs.profiles, "r", processed=["e1", "e2"], last_at=AT, at=AT)
    assert path == feedback_record_path(dirs.profiles, "r")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == {"reader": "r", "updated_at": AT, "last_at": AT, "processed": ["e1", "e2"]}
    record, notes = read_feedback_record(dirs.profiles, "r")
    assert notes == [] and processed_ids(record) == (["e1", "e2"], [])


def test_broken_feedback_record_is_visible_and_treated_as_all_pending(dirs: SimpleNamespace) -> None:
    path = feedback_record_path(dirs.profiles, "r")
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text("{ 不是 JSON", encoding="utf-8")
    assert read_feedback_record(dirs.profiles, "r")[0] == {}
    assert "读不出来" in read_feedback_record(dirs.profiles, "r")[1][0]

    path.write_text("[1, 2]", encoding="utf-8")
    assert "不是一个 JSON 对象" in read_feedback_record(dirs.profiles, "r")[1][0]

    path.write_text(json.dumps({"processed": "e1"}), encoding="utf-8")
    assert "processed 不是数组" in processed_ids(read_feedback_record(dirs.profiles, "r")[0])[1][0]

    path.write_text(json.dumps({"processed": ["e1", "e1", "", 7, None]}), encoding="utf-8")
    ids, notes = processed_ids(read_feedback_record(dirs.profiles, "r")[0])
    assert ids == ["e1"] and "已忽略" in notes[0]


def test_pending_events_skips_processed_ids_and_dedups_the_window() -> None:
    events = [
        {"id": "e1", "kind": "explain", "source_id": "doc", "at": AT},
        {"id": "e2", "kind": "explain", "source_id": "doc", "at": AT},
        {"kind": "dwell", "source_id": "doc", "paragraph": "p001", "at": AT},  # 复合键兜底
    ]
    assert [event_id(item) for item in pending_events(events, ["e1"])] == ["e2", f"dwell|doc|p001|{AT}"]
    assert pending_events(events, ["e1", "e2", f"dwell|doc|p001|{AT}"]) == []

    # 窗口溢出：保序去重、只留最近 keep 条，且**不静默**（说明里写清被挤出的后果）
    merged, notes = merge_processed(["e1", "e2"], ["e2", "e3", "e4"], keep=3)
    assert merged == ["e2", "e3", "e4"] and "窗口" in notes[0] and "1 条被挤出" in notes[0]
    assert merge_processed(["e1"], ["e2"], keep=3) == (["e1", "e2"], [])


# ------------------------------------------------------------------ 幂等键 --
def test_event_id_prefers_client_id_then_falls_back_to_composite_key() -> None:
    assert event_id({"id": "  e1 ", "kind": "explain"}) == "e1"
    fallback = event_id({"kind": "explain", "source_id": "doc", "paragraph": "p001", "at": AT})
    assert fallback == f"explain|doc|p001|{AT}"
    assert event_id({"kind": "read_complete", "source_id": "doc"}) == "read_complete|doc||"


def test_dedup_by_id_and_by_composite_key_including_intra_batch() -> None:
    existing = [{"id": "e1", "kind": "explain", "source_id": "doc", "at": AT}]
    incoming = [
        {"id": "e1", "kind": "explain", "source_id": "doc", "at": AT},  # 与文件里重复
        {"id": "e2", "kind": "compress", "source_id": "doc", "at": AT},
        {"id": "e2", "kind": "compress", "source_id": "doc", "at": AT},  # 批内重复
        {"kind": "dwell", "source_id": "doc", "paragraph": "p001", "at": AT},  # 复合键兜底
        {"kind": "dwell", "source_id": "doc", "paragraph": "p001", "at": AT},  # 复合键重复
    ]
    fresh, duplicates = dedup(incoming, existing)
    assert [item.get("id") for item in fresh] == ["e2", None]
    assert len(duplicates) == 3


# ------------------------------------------------------------------ 规范化 --
def test_validate_event_stamps_reader_and_defaults() -> None:
    event, reason = validate_event(
        {"kind": "explain", "source_id": "doc", "reader": "别人", "paragraph": "p001", "scope": "selection"},
        reader="local-default",
        now=AT,
    )
    assert reason is None and event is not None
    assert event["reader"] == "local-default"  # 请求里给了也不认
    assert event["v"] == 1 and event["at"] == AT
    assert event["id"] == f"explain|doc|p001|{AT}"  # 复合键兜底
    assert event["scope"] == "selection"  # 不认识的键原样保留
    assert list(event)[:6] == ["v", "id", "at", "kind", "source_id", "reader"]


def test_validate_event_discards_unknown_kind_and_rejects_illegal_source_id() -> None:
    assert validate_event({"kind": "没这个", "source_id": "doc"}, reader="r")[0] is None
    reason = validate_event({"kind": "没这个", "source_id": "doc"}, reader="r")[1]
    assert reason and "白名单" in reason
    assert validate_event("不是对象", reader="r")[0] is None
    for bad in (None, "", "   ", 7, "bad..id", "有空格 的"):
        with pytest.raises(InputError):
            validate_event({"kind": "read_complete", "source_id": bad}, reader="r")


def test_event_kinds_whitelist_matches_contract() -> None:
    assert set(EVENT_KINDS) == {
        "dwell",
        "read_complete",
        "read_abandon",
        "explain",
        "drawer_open",
        "selection",
        "compress",
        "skip",
    }


# -------------------------------------------------------------- 事件 → 信号 --
def test_signals_weights_follow_the_contract_table() -> None:
    events = [
        {"kind": "explain", "source_id": "doc", "paragraph": "p001", "scope": "paragraph", "at": AT},
        {
            "kind": "explain",
            "source_id": "doc",
            "paragraph": "p002",
            "scope": "selection",
            "selection_chars": 12,
            "at": AT,
        },
        {"kind": "compress", "source_id": "doc", "paragraph": "p001", "ok": True, "at": AT},
        {"kind": "dwell", "source_id": "doc", "paragraph": "p002", "ms": SHORT_DWELL_MS - 1, "at": AT},
    ]
    signals, notes = signals_from_events(events, INVENTORY, source_id="doc", now=AT)
    mapping = _signal_map([item for item in signals if "concept" in item])
    assert mapping[("c01", "explain", "p001")] == SIGNAL_WEIGHTS["explain"] == -1.0
    assert mapping[("c01", "explain", "p002")] == SELECTION_WEIGHT == -0.5
    assert mapping[("c02", "explain", "p002")] == SELECTION_WEIGHT
    assert mapping[("c01", "compress", "p001")] == SIGNAL_WEIGHTS["compress"] == -0.75
    assert mapping[("c01", "dwell", "p002")] == 0.0  # 短停留：只留痕
    assert mapping[("c02", "dwell", "p002")] == 0.0
    assert notes == []


def test_short_dwell_never_becomes_a_negative_signal() -> None:
    events = [{"kind": "dwell", "source_id": "doc", "paragraph": "p001", "ms": 300, "at": AT}]
    signals, _notes = signals_from_events(events, INVENTORY, source_id="doc", now=AT)
    assert [item["weight"] for item in signals] == [0.0]
    assert "不当负信号" in signals[0]["detail"]
    assert not any(item["weight"] < 0 for item in signals)


def test_read_complete_gives_positive_weight_only_to_concepts_without_help() -> None:
    only_read, _ = signals_from_events(
        [{"kind": "read_complete", "source_id": "doc", "at": AT}], INVENTORY, source_id="doc", now=AT
    )
    assert {item["concept"] for item in only_read} == {"c01", "c02"}
    assert {item["weight"] for item in only_read} == {SIGNAL_WEIGHTS["read_complete"]}
    assert {item["paragraph"] for item in only_read} == {""}

    helped, _ = signals_from_events(
        [
            {"kind": "explain", "source_id": "doc", "paragraph": "p002", "at": AT},  # 命中 c01 + c02
            {"kind": "read_complete", "source_id": "doc", "at": AT},
        ],
        INVENTORY,
        source_id="doc",
        now=AT,
    )
    assert [item["kind"] for item in helped] == ["explain", "explain"]  # 一个正向都不给


def test_signals_skip_unmapped_paragraphs_and_unknown_kinds_with_visible_notes() -> None:
    events = [
        {"kind": "explain", "source_id": "doc", "paragraph": "p999", "at": AT},
        {"kind": "没这个", "source_id": "doc", "paragraph": "p001", "at": AT},
        {"kind": "dwell", "source_id": "doc", "paragraph": "p001", "at": AT},  # 缺 ms
    ]
    signals, notes = signals_from_events(events, INVENTORY, source_id="doc", now=AT)
    assert signals == []
    joined = " / ".join(notes)
    assert "p999" in joined and "没这个" in joined and "ms" in joined


def test_signals_filter_by_source_id_and_warn_on_mixed_sources() -> None:
    events = [
        {"kind": "explain", "source_id": "doc", "paragraph": "p001", "at": AT},
        {"kind": "explain", "source_id": "other", "paragraph": "p002", "at": AT},
    ]
    filtered, notes = signals_from_events(events, INVENTORY, source_id="doc", now=AT)
    assert [item["concept"] for item in filtered] == ["c01"]
    assert notes == []

    mixed, notes = signals_from_events(events, INVENTORY, now=AT)
    assert len(mixed) == 3  # c01 + (c01, c02)
    assert any("多个材料" in note for note in notes)


def test_preference_signals_cover_verbose_and_repeated_long_dwells() -> None:
    events = [
        {"kind": "compress", "source_id": "doc", "paragraph": "p001", "ok": False, "reason": "model_error", "at": AT},
        {"kind": "dwell", "source_id": "doc", "paragraph": "p002", "ms": LONG_DWELL_MS, "at": AT},
        {"kind": "dwell", "source_id": "doc", "paragraph": "p002", "ms": LONG_DWELL_MS + 5000, "at": AT},
    ]
    signals, notes = signals_from_events(events, INVENTORY, source_id="doc", now=AT)
    preferences = [item for item in signals if "preference" in item]
    assert [item["preference"] for item in preferences] == [PREFERENCE_ID, PREFERENCE_ID]
    assert [item["paragraph"] for item in preferences] == ["p001", "p002"]
    assert "太啰嗦" in preferences[0]["detail"]
    assert "长段反复回看" in preferences[1]["detail"] and "共 2 次" in preferences[1]["detail"]
    # 压缩失败照样是求助信号：正文没变不代表读者没说「太啰嗦」
    assert "model_error" in next(item for item in signals if item.get("kind") == "compress")["detail"]
    assert notes == []

    once, _ = signals_from_events(
        [{"kind": "dwell", "source_id": "doc", "paragraph": "p002", "ms": LONG_DWELL_MS, "at": AT}],
        INVENTORY,
        source_id="doc",
        now=AT,
    )
    assert [item for item in once if "preference" in item] == []  # 一次不算「反复回看」


def test_events_without_paragraph_and_neutral_kinds_produce_no_signals() -> None:
    events = [
        {"kind": "read_abandon", "source_id": "doc", "at": AT},
        {"kind": "drawer_open", "source_id": "doc", "at": AT},
        {"kind": "selection", "source_id": "doc", "chars": 30, "at": AT},
        {"kind": "skip", "source_id": "doc", "paragraph": "p001", "reason": "不想看", "at": AT},
    ]
    signals, notes = signals_from_events(events, INVENTORY, source_id="doc", now=AT)
    assert signals == [] and notes == []
