"""读者画像：YAML 读写、自评写入、overrides 追记（DESIGN §4.3 / §5）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ar.errors import InputError
from ar.profile import DEFAULT_CONFIDENCE, apply_rating, default_profile, load_profile, profile_hint, save_profile


def test_load_missing_profile_returns_skeleton_with_visible_note(dirs: SimpleNamespace) -> None:
    profile, notes = load_profile(dirs.profiles, "local-default")
    assert profile == {"reader": "local-default", "concepts": {}, "preferences": [], "overrides": []}
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
    assert set(profile) == {"reader", "concepts", "preferences", "overrides"}
    assert profile["reader"] == "local-default"
