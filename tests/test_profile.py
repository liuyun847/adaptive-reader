"""读者画像：YAML 读写、自评写入、overrides 追记（DESIGN §4.3 / §5）+ 行为信号合并（PLAN §7.3）。"""

from __future__ import annotations

import copy
import errno
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ar.errors import EXIT_USAGE, InputError, WriteError
from ar.profile import (
    CONFIDENCE_TARGETS,
    DEFAULT_CONFIDENCE,
    HALF_LIFE_DAYS,
    MIN_SAME_DIRECTION_EVIDENCE,
    STATE_THRESHOLD,
    ProfileUpdate,
    affected_paragraphs,
    apply_rating,
    applySignals,
    default_profile,
    load_profile,
    profile_hint,
    save_profile,
)

NOW = "2026-10-13T10:00:00+08:00"
INVENTORY = {
    "concepts": [
        {"id": "c01", "paragraphs": ["p001", "p002"]},
        {"id": "c02", "paragraphs": ["p003"]},
        {"id": "c03", "paragraphs": ["p004"]},
    ]
}


def _signal(concept: str, weight: float, *, paragraph: str = "p001", kind: str = "explain", at: str = NOW) -> dict:
    return {"concept": concept, "paragraph": paragraph, "kind": kind, "weight": weight, "at": at, "detail": "测试信号"}


def _counters(score: float, entries: list[tuple[float, str, str]]) -> dict:
    """把 ``(weight, paragraph, at)`` 列表造成画像里的 ``concepts[cid].signals`` 计数块。"""
    history = [
        {"at": at, "kind": "explain", "paragraph": paragraph, "weight": weight, "detail": "旧证据"}
        for weight, paragraph, at in entries
    ]
    negative = {paragraph or at[:10] for weight, paragraph, at in entries if weight < 0}
    positive = {paragraph or at[:10] for weight, paragraph, at in entries if weight > 0}
    return {
        "score": score,
        "negative": len(negative),
        "positive": len(positive),
        "last_at": entries[-1][2] if entries else NOW,
        "history": history,
    }


def _profile_with(cid: str, *, state: str = "partial", confidence: float = 0.55, counters: dict | None = None) -> dict:
    entry: dict = {"state": state, "confidence": confidence, "evidence": []}
    if counters is not None:
        entry["signals"] = counters
    return {"reader": "local-default", "background": {}, "concepts": {cid: entry}, "preferences": [], "overrides": []}


def test_load_missing_profile_returns_skeleton_with_visible_note(dirs: SimpleNamespace) -> None:
    profile, notes = load_profile(dirs.profiles, "local-default")
    assert profile == {
        "reader": "local-default",
        "background": {},
        "concepts": {},
        "preferences": [],
        "overrides": [],
    }
    assert notes and "不存在" in notes[0]


def test_apply_rating_updates_state_evidence_and_override() -> None:
    updated = apply_rating(default_profile(), "c07", "partial", note="只记得一半", at="2026-10-05T10:00:00+08:00")
    entry = updated["concepts"]["c07"]
    assert entry["state"] == "partial"
    assert entry["confidence"] == DEFAULT_CONFIDENCE
    assert entry["evidence"][0] == {
        "source": "self_rating",
        "at": "2026-10-05T10:00:00+08:00",
        "detail": "只记得一半",
    }
    assert updated["overrides"][0]["concept"] == "c07"
    assert updated["overrides"][0]["state"] == "partial"
    assert updated["overrides"][0]["note"] == "只记得一半"


def test_apply_rating_keeps_higher_existing_confidence_and_appends_evidence() -> None:
    profile = {
        "reader": "local-default",
        "concepts": {"c01": {"state": "unknown", "confidence": 0.9, "evidence": [{"source": "quiz", "detail": "x"}]}},
        "preferences": [],
        "overrides": [{"concept": "c01", "state": "unknown"}],
    }
    updated = apply_rating(profile, "c01", "known")
    entry = updated["concepts"]["c01"]
    assert entry["state"] == "known"
    assert entry["confidence"] == 0.9  # 不降低已有把握度
    assert [e["source"] for e in entry["evidence"]] == ["quiz", "self_rating"]
    assert len(updated["overrides"]) == 2
    # 原对象不被就地改动
    assert profile["concepts"]["c01"]["state"] == "unknown"


def test_apply_rating_rejects_bad_state() -> None:
    with pytest.raises(InputError, match="state 非法"):
        apply_rating(default_profile(), "c01", "大概知道")
    with pytest.raises(InputError, match="concept_id"):
        apply_rating(default_profile(), "", "known")


def test_save_and_reload_roundtrip(dirs: SimpleNamespace) -> None:
    profile = apply_rating(default_profile(), "c03", "known", note="别再解释了")
    path = save_profile(dirs.profiles, "local-default", profile)
    assert path == dirs.profiles / "local-default" / "profile.yaml"

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["reader"] == "local-default"
    assert raw["concepts"]["c03"]["state"] == "known"
    assert raw["overrides"][0]["note"] == "别再解释了"

    reloaded, notes = load_profile(dirs.profiles, "local-default")
    assert notes == []
    assert reloaded == profile


def test_load_rejects_broken_yaml(dirs: SimpleNamespace) -> None:
    path = dirs.profiles / "local-default" / "profile.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("concepts: [这是数组，不是映射]\n", encoding="utf-8")
    with pytest.raises(InputError, match="concepts"):
        load_profile(dirs.profiles, "local-default")


def test_profile_hint_and_reader_id_guard(dirs: SimpleNamespace) -> None:
    profile = apply_rating(default_profile(), "c01", "unknown")
    assert profile_hint(profile) == "c01=unknown"
    assert profile_hint(default_profile()) is None
    with pytest.raises(InputError, match="reader id 非法"):
        load_profile(dirs.profiles, "../escape")


def test_project_default_profile_file_is_valid() -> None:
    """仓库里那份 profiles/local-default/profile.yaml 必须能被解析成契约形态。"""
    project_root = Path(__file__).resolve().parents[1]
    profile_dir = project_root / "profiles" / "local-default"
    if not (profile_dir / "profile.yaml").is_file():
        pytest.skip("profiles/local-default/profile.yaml 尚未创建")
    profile, notes = load_profile(project_root / "profiles", "local-default")
    assert notes == []
    # 键集同步到「读前背景问卷」落地后的新契约：background 是第五个顶层键
    # （PLAN-background-survey §6.1 的必改点一：漏进白名单的键会被 _normalize 静默丢弃）
    assert set(profile) == {"reader", "background", "concepts", "preferences", "overrides"}
    assert profile["reader"] == "local-default"
    assert isinstance(profile["background"], dict)


# ------------------------------------------------- 行为信号合并（PLAN §7.3） --
def test_apply_signals_decays_existing_score_with_14_day_half_life() -> None:
    """既有 score 先按 14 天半衰期衰减：14 天整 → 乘 0.5（''0.5 ** (Δdays / 14)''）。"""
    assert HALF_LIFE_DAYS == 14.0
    profile = _profile_with(
        "c01",
        counters=_counters(2.0, [(-1.0, "p001", "2026-09-29T10:00:00+08:00")]),
    )
    profile["concepts"]["c01"]["signals"]["last_at"] = "2026-09-29T10:00:00+08:00"
    update = applySignals(profile, [_signal("c01", 0.0, kind="dwell")], now=NOW)
    entry = update.profile["concepts"]["c01"]
    assert entry["signals"]["score"] == 1.0  # 2.0 × 0.5 ** (14/14) + 0.0
    # 1.0 < 阈值 1.2：state / confidence 逐字不变
    assert entry["state"] == "partial" and entry["confidence"] == 0.55
    assert update.changed == [] and update.accumulated[0]["concept"] == "c01"

    # 没有 last_at / 时间戳坏掉 → 不衰减
    fresh = _profile_with("c01", counters={"score": 2.0, "negative": 1, "positive": 0, "history": []})
    assert applySignals(fresh, [_signal("c01", 0.0, kind="dwell")], now=NOW).profile["concepts"]["c01"]["signals"][
        "score"
    ] == 2.0


def test_state_moves_only_at_threshold_and_needs_two_same_direction_evidence() -> None:
    assert STATE_THRESHOLD == 1.2 and MIN_SAME_DIRECTION_EVIDENCE == 2
    idle = [_signal("c01", 0.0, kind="dwell")]  # 0 权重信号：只触发评估，不改分数
    # 以下计数块的 last_at 都是 NOW ⇒ 不衰减，考的就是阈值与同向证据条数
    # 分数够了（|−1.3| ≥ 1.2）但只有 1 条同向证据 → 不动
    one_evidence = _profile_with("c01", counters=_counters(-1.3, [(-1.3, "p001", NOW)]))
    update = applySignals(one_evidence, idle, now=NOW)
    assert update.changed == [] and update.profile["concepts"]["c01"]["state"] == "partial"

    # 两条负证据但来自同一个段、同一天 → 只算 1 条 → 不动
    same_paragraph = _profile_with("c01", counters=_counters(-1.4, [(-0.7, "p001", NOW), (-0.7, "p001", NOW)]))
    assert applySignals(same_paragraph, idle, now=NOW).changed == []

    # 两条负证据来自两个不同段 → known 一档降到 partial
    two = _profile_with(
        "c01",
        state="known",
        confidence=0.8,
        counters=_counters(-1.4, [(-0.7, "p001", NOW), (-0.7, "p002", NOW)]),
    )
    update = applySignals(two, idle, now=NOW)
    assert [row["concept"] for row in update.changed] == ["c01"]
    assert update.changed[0]["from"] == "known" and update.changed[0]["to"] == "partial"

    # 分数不够（0.6 < 1.2）→ 不动
    low = _profile_with("c01", state="known", counters=_counters(0.6, [(0.25, "p001", NOW), (0.25, "p002", NOW)]))
    assert applySignals(low, idle, now=NOW).changed == []

    # 没出现在 signals 里的概念**根本不被评估**（不会因为时间流逝/衰减自己动）
    untouched = _profile_with("c02", state="known", counters=_counters(-6.0, [(-3.0, "p003", NOW)]))
    update = applySignals(untouched, [_signal("c09", 0.0, kind="dwell")], now=NOW)
    assert update.changed == [] and [row["concept"] for row in update.accumulated] == ["c09"]
    assert update.profile["concepts"]["c02"]["signals"]["score"] == -6.0
    assert update.profile["concepts"]["c02"]["state"] == "known"


def test_state_moves_one_step_at_a_time_and_never_past_the_scale() -> None:
    idle = [_signal("c01", 0.0, kind="dwell")]
    huge = _profile_with(
        "c01",
        state="known",
        counters=_counters(-6.0, [(-2.0, "p001", NOW), (-2.0, "p002", NOW), (-2.0, "p003", NOW)]),
    )
    update = applySignals(huge, idle, now=NOW)
    assert update.changed[0]["to"] == "partial"  # known → partial，不直接跳 unknown

    bottom = _profile_with(
        "c01",
        state="unknown",
        confidence=0.25,
        counters=_counters(-6.0, [(-2.0, "p001", NOW), (-2.0, "p002", NOW)]),
    )
    update = applySignals(bottom, idle, now=NOW)
    assert update.changed == []  # 到底了：累加分数、不动 state / confidence
    assert update.profile["concepts"]["c01"]["state"] == "unknown"
    assert update.profile["concepts"]["c01"]["confidence"] == 0.25
    assert update.profile["concepts"]["c01"]["signals"]["score"] == -6.0


def test_below_threshold_leaves_state_and_confidence_byte_identical() -> None:
    profile = _profile_with("c01", state="partial", confidence=0.55)
    before = json.dumps(profile, ensure_ascii=False, sort_keys=True)
    update = applySignals(profile, [_signal("c01", -0.5), _signal("c01", -0.5, paragraph="p002")], now=NOW)
    entry = update.profile["concepts"]["c01"]
    assert entry["state"] == "partial" and entry["confidence"] == 0.55
    assert entry["signals"]["score"] == -1.0  # 分数记下了，但没到阈值 1.2
    assert json.dumps(profile, ensure_ascii=False, sort_keys=True) == before, "纯函数：原画像一个字节都不动"
    assert update.changed == [] and [row["concept"] for row in update.accumulated] == ["c01"]


def test_confidence_moves_toward_target_and_clamps_into_domain() -> None:
    assert CONFIDENCE_TARGETS == {"known": 0.8, "partial": 0.55, "unknown": 0.25}
    idle = [_signal("c01", 0.0, kind="dwell")]
    profile = _profile_with(
        "c01",
        state="known",
        confidence=0.9,
        counters=_counters(-1.5, [(-0.75, "p001", NOW), (-0.75, "p002", NOW)]),
    )
    update = applySignals(profile, idle, now=NOW)
    entry = update.profile["concepts"]["c01"]
    # partial 目标 0.55；步长 = clamp(0.15 × 1.5, 0.05, 0.2) = 0.2 → 0.9 − 0.2
    assert entry["state"] == "partial" and entry["confidence"] == 0.7
    assert update.changed[0]["confidence_from"] == 0.9 and update.changed[0]["confidence_to"] == 0.7

    # 越界的历史值不许把 confidence 带出 [0.05, 0.95]
    weird = _profile_with(
        "c01",
        state="partial",
        confidence=-5.0,
        counters=_counters(1.6, [(0.8, "p001", NOW), (0.8, "p002", NOW)]),
    )
    assert applySignals(weird, idle, now=NOW).profile["concepts"]["c01"]["confidence"] == 0.05


def test_counts_stay_in_the_five_top_level_keys_and_inside_concepts() -> None:
    update = applySignals(default_profile("local-default"), [_signal("c09", -0.75, kind="compress")], now=NOW)
    assert set(update.profile) == {"reader", "background", "concepts", "preferences", "overrides"}
    entry = update.profile["concepts"]["c09"]
    assert set(entry["signals"]) == {"score", "negative", "positive", "last_at", "history"}
    assert entry["signals"]["score"] == -0.75 and entry["signals"]["negative"] == 1
    assert entry["signals"]["positive"] == 0 and entry["signals"]["last_at"] == NOW
    assert entry["evidence"] == [{"source": "behavior", "at": NOW, "detail": "测试信号"}]
    assert "state" not in entry and "confidence" not in entry  # 未达阈值：一个新键都不多写
    assert update.evidence_added == 1


def test_overrides_protect_concepts_and_are_never_touched() -> None:
    overrides = [{"concept": "c01", "state": "known", "at": "2026-09-01T10:00:00+08:00", "note": "别再解释了"}]
    profile = {
        "reader": "local-default",
        "background": {},
        "concepts": {"c01": {"state": "known", "confidence": 0.8, "evidence": []}},
        "preferences": [],
        "overrides": copy.deepcopy(overrides),
    }
    update = applySignals(profile, [_signal("c01", -1.0), _signal("c01", -1.0, paragraph="p002")], now=NOW)
    assert update.skipped_overrides == [{"concept": "c01", "override_state": "known"}]
    assert update.profile["overrides"] == overrides, "overrides 数组必须逐字不变"
    assert update.profile["concepts"]["c01"] == {"state": "known", "confidence": 0.8, "evidence": []}
    assert update.changed == [] and update.accumulated == [] and update.evidence_added == 0
    assert update.degraded == []  # 保护是设计，不是降级

    # 没被 override 的概念照常演化
    mixed = applySignals(profile, [_signal("c01", -1.0), _signal("c02", -0.7, paragraph="p003")], now=NOW)
    assert [row["concept"] for row in mixed.skipped_overrides] == ["c01"]
    assert mixed.profile["concepts"]["c02"]["signals"]["score"] == -0.7
    assert mixed.profile["overrides"] == overrides


def test_apply_rating_override_wins_over_later_behavior_signals() -> None:
    """自评（``apply_rating``）写的是 override ⇒ 之后的行为信号整条跳过（优先级钉住）。"""
    profile = apply_rating(default_profile("local-default"), "c07", "known", note="别再解释了")
    update = applySignals(profile, [_signal("c07", -1.0), _signal("c07", -1.0, paragraph="p002")], now=NOW)
    assert update.skipped_overrides == [{"concept": "c07", "override_state": "known"}]
    entry = update.profile["concepts"]["c07"]
    assert entry["state"] == "known" and entry["confidence"] == DEFAULT_CONFIDENCE == 0.7
    assert all(item["source"] == "self_rating" for item in entry["evidence"])


def test_preference_signals_only_append_evidence_and_never_create_preferences() -> None:
    profile = {
        "reader": "local-default",
        "background": {},
        "concepts": {},
        "preferences": [
            {"id": "pref_paragraph_len", "statement": "偏好短段落", "confidence": 0.4, "evidence": []},
            {"id": "pref_other", "statement": "别的偏好", "confidence": 0.4, "evidence": []},
        ],
        "overrides": [],
    }
    signal = {
        "preference": "pref_paragraph_len",
        "paragraph": "p012",
        "kind": "compress",
        "weight": -0.75,
        "at": NOW,
        "detail": "段 p012 被读者标为太啰嗦",
    }
    update = applySignals(profile, [signal], now=NOW)
    assert update.profile["preferences"][0]["evidence"] == [
        {"source": "behavior", "at": NOW, "detail": "段 p012 被读者标为太啰嗦"}
    ]
    assert update.profile["preferences"][1]["evidence"] == []
    assert update.evidence_added == 1 and update.changed == [] and update.profile["concepts"] == {}

    empty = {"reader": "local-default", "concepts": {}, "preferences": [], "overrides": []}
    missing = applySignals(empty, [signal], now=NOW)
    assert missing.profile["preferences"] == []
    assert any("绝不自动新建偏好" in note for note in missing.degraded)


def test_apply_signals_skips_malformed_signals_with_visible_notes() -> None:
    update = applySignals(default_profile("local-default"), ["不是对象", {"weight": -1.0}], now=NOW)
    assert update.profile["concepts"] == {}
    joined = " / ".join(update.degraded)
    assert "不是 JSON 对象" in joined and "缺 concept" in joined


def test_signal_detail_is_truncated_and_gets_a_default_wording() -> None:
    long_detail = "啰" * 500
    update = applySignals(
        default_profile("local-default"),
        [
            {"concept": "c01", "paragraph": "p001", "kind": "explain", "weight": -1.0, "at": NOW, "detail": long_detail},
            {"concept": "c01", "paragraph": "p002", "kind": "explain", "weight": -1.0, "at": NOW},
        ],
        now=NOW,
    )
    entry = update.profile["concepts"]["c01"]
    assert len(entry["evidence"][0]["detail"]) == 200
    assert len(entry["signals"]["history"][0]["detail"]) == 200
    assert entry["evidence"][1]["detail"] == "行为信号：explain"  # 没给 detail 也能读


def test_apply_signals_survives_save_load_roundtrip(dirs: SimpleNamespace) -> None:
    update = applySignals(default_profile("local-default"), [_signal("c01", -0.75)], now=NOW)
    save_profile(dirs.profiles, "local-default", update.profile)
    reloaded, notes = load_profile(dirs.profiles, "local-default")
    assert notes == []
    assert reloaded == update.profile


def test_affected_paragraphs_reverse_lookup_from_inventory() -> None:
    update = ProfileUpdate(
        profile={},
        changed=[{"concept": "c01"}, {"concept": "c02"}],
        skipped_overrides=[],
        accumulated=[{"concept": "c03"}],  # 没到阈值的不算「受影响」
    )
    assert affected_paragraphs(update, INVENTORY) == {"p001", "p002", "p003"}
    assert affected_paragraphs({"changed": [{"concept": "c03"}]}, INVENTORY) == {"p004"}
    assert affected_paragraphs(ProfileUpdate(profile={}), INVENTORY) == set()
    assert affected_paragraphs(update, None) == set()


def test_save_profile_skips_write_when_serialized_text_is_identical(dirs: SimpleNamespace) -> None:
    profile = apply_rating(default_profile(), "c03", "known", note="别再解释了")
    path = save_profile(dirs.profiles, "local-default", profile)
    text = path.read_text(encoding="utf-8")
    stat = path.stat()

    assert save_profile(dirs.profiles, "local-default", profile) == path
    assert path.stat().st_mtime_ns == stat.st_mtime_ns, "内容逐字相同就不写盘（mtime 不许变）"
    assert path.read_text(encoding="utf-8") == text
    assert _tmp_names(path.parent) == [], "原子写的候选临时文件必须清干净"
    assert not (dirs.profiles / "local-default" / "profile.yaml.tmp").exists()

    changed = apply_rating(profile, "c04", "unknown")
    save_profile(dirs.profiles, "local-default", changed)
    assert path.read_text(encoding="utf-8") != text
    assert load_profile(dirs.profiles, "local-default")[0] == changed


def _tmp_names(directory: Path) -> list[str]:
    """目录里的临时文件（正常路径跑完必须是空的——原子写不许留残骸）。"""
    return sorted(p.name for p in directory.iterdir() if p.name.startswith(".tmp-"))


def test_save_profile_on_unwritable_dir_fails_fast_with_a_readable_error(
    dirs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """画像目录不可写 ⇒ 与其它写盘入口同一口径：**立刻** `WriteError`、点名目录与 errno、退出码 2。

    2026-10-14 复核揪出的第三个漏网入口：此前是本模块手写的「固定临时名 ``profile.yaml.tmp`` +
    裸 ``write_text`` / ``replace``」，坏目录上抛**裸 ``PermissionError``** ⇒
    `ar feedback --apply --profiles-dir <坏目录>` 直接 traceback、退出码 1；
    `POST /api/profile/rating` 只有一句「服务内部错误：PermissionError」。
    """
    reader_dir = dirs.profiles / "local-default"
    reader_dir.mkdir(parents=True)  # 目录在、但不可写
    real_open = os.open
    attempts: list[str] = []

    def fake_open(path, flags, mode=0o777):
        if Path(path).parent == reader_dir:
            attempts.append(str(path))
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_open(path, flags, mode)

    monkeypatch.setattr("ar.jsonio.os.open", fake_open)

    with pytest.raises(WriteError) as excinfo:
        save_profile(dirs.profiles, "local-default", default_profile())

    message = str(excinfo.value)
    assert str(reader_dir) in message, f"错误里必须点名是哪个目录：{message}"
    assert f"errno {errno.EACCES}" in message, f"错误里必须带原始 errno：{message}"
    assert len(attempts) == 1, f"目录不可写必须一次就失败，实测试了 {len(attempts)} 次"
    assert excinfo.value.exit_code == EXIT_USAGE, "CLI 上归「用法 / 输入错误」那一档（退出码 2）"
    assert not (reader_dir / "profile.yaml").exists(), "没写成就绝不许留下半个目标文件"
    assert not (reader_dir / "profile.yaml.tmp").exists(), "旧写法的固定临时名一个字都不该再出现"
    assert _tmp_names(reader_dir) == [], "临时文件要清掉"


def test_save_profile_replace_failure_cleans_the_temp_file(
    dirs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """改名那一步才失败（临时文件已经建出来了）：清残骸 + 可读错误，不留半个 profile.yaml。"""

    def fake_replace(src, dst):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr("ar.jsonio.os.replace", fake_replace)
    reader_dir = dirs.profiles / "local-default"

    with pytest.raises(WriteError) as excinfo:
        save_profile(dirs.profiles, "local-default", default_profile())

    assert f"errno {errno.EACCES}" in str(excinfo.value)
    assert str(reader_dir) in str(excinfo.value)
    assert not (reader_dir / "profile.yaml").exists()
    assert _tmp_names(reader_dir) == []
    assert not (reader_dir / "profile.yaml.tmp").exists()

