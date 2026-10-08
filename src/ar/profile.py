"""读者画像 Profile：``profiles/<reader>/profile.yaml``（DESIGN §4.3 的字段原样）。

```yaml
reader: local-default
concepts:
  c07: {state: partial, confidence: 0.62, evidence: [{source: quiz, at: ..., detail: ...}]}
preferences:
  - {id: pref_paragraph_len, statement: "偏好短段落", confidence: 0.40, evidence: [...]}
overrides:                      # 用户手动推翻，优先级最高，行为信号不得覆盖
  - {concept: c01, state: known, at: ..., note: "别再解释了"}
```

骨架阶段只有「自评勾选」这一个写入来源（``apply_rating``），不做自动出题、不做行为回流。
自评只反映「以为知道」，所以给的把握度是 ``DEFAULT_CONFIDENCE``（不是 1.0）；
画像合并规则（阈值、衰减、overrides 优先）留待后续里程碑，本模块只保证写入形态正确。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .errors import InputError
from .jsonio import now_iso
from .paths import DEFAULT_READER, profile_path

__all__ = [
    "STATES",
    "DEFAULT_CONFIDENCE",
    "default_profile",
    "load_profile",
    "save_profile",
    "apply_rating",
    "profile_hint",
]

STATES = ("known", "partial", "unknown")

# 自评是显式信号，但「自评只反映以为知道」（DESIGN §5），所以不给满分
DEFAULT_CONFIDENCE = 0.7


def default_profile(reader: str = DEFAULT_READER) -> dict[str, Any]:
    """空画像骨架（四个顶层键与 DESIGN §4.3 一致）。"""
    return {"reader": reader, "concepts": {}, "preferences": [], "overrides": []}


def _normalize(data: Any, reader: str) -> dict[str, Any]:
    if data is None:
        return default_profile(reader)
    if not isinstance(data, dict):
        raise InputError("profile.yaml 根节点必须是映射")
    profile = default_profile(reader)
    profile["reader"] = str(data.get("reader") or reader)
    concepts = data.get("concepts") or {}
    if not isinstance(concepts, dict):
        raise InputError("profile.yaml 的 concepts 必须是映射（概念 id → 状态）")
    profile["concepts"] = {str(k): v if isinstance(v, dict) else {} for k, v in concepts.items()}
    for field in ("preferences", "overrides"):
        value = data.get(field) or []
        if not isinstance(value, list):
            raise InputError(f"profile.yaml 的 {field} 必须是数组")
        profile[field] = [item for item in value if isinstance(item, dict)]
    return profile


def load_profile(profiles_dir: Path | str, reader: str = DEFAULT_READER) -> tuple[dict[str, Any], list[str]]:
    """读画像；文件不存在时返回空骨架 + 一条**可见**的降级说明。"""
    path = profile_path(profiles_dir, reader)
    if not path.is_file():
        return default_profile(reader), [f"{path} 不存在：当前按「无画像」模式处理（所有概念按可能不知道）"]
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise InputError(f"profile.yaml 解析失败：{path}（{exc}）") from exc
    return _normalize(raw, reader), []


def save_profile(profiles_dir: Path | str, reader: str, profile: dict[str, Any]) -> Path:
    """写回画像（原子写）。"""
    path = profile_path(profiles_dir, reader)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(profile, allow_unicode=True, sort_keys=False, default_flow_style=False)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)
    return path


def apply_rating(
    profile: dict[str, Any],
    concept_id: str,
    state: str,
    note: str | None = None,
    *,
    at: str | None = None,
) -> dict[str, Any]:
    """写入一条自评：更新 ``concepts`` 状态 + 追记 ``overrides``（手动结论优先级最高）。"""
    if state not in STATES:
        raise InputError(f"state 非法：{state!r}（只能是 {' / '.join(STATES)}）")
    if not concept_id or not isinstance(concept_id, str):
        raise InputError("concept_id 必须是非空字符串")

    updated = _normalize(profile, profile.get("reader") or DEFAULT_READER)
    timestamp = at or now_iso()
    entry = dict(updated["concepts"].get(concept_id) or {})
    entry["state"] = state
    try:
        current_confidence = float(entry.get("confidence") or 0.0)
    except (TypeError, ValueError):
        current_confidence = 0.0
    entry["confidence"] = max(current_confidence, DEFAULT_CONFIDENCE)
    evidence = [e for e in (entry.get("evidence") or []) if isinstance(e, dict)]
    evidence.append(
        {"source": "self_rating", "at": timestamp, "detail": note.strip() if note else f"读者自评：{state}"}
    )
    entry["evidence"] = evidence
    updated["concepts"][concept_id] = entry

    overrides = [o for o in updated.get("overrides") or [] if isinstance(o, dict)]
    override: dict[str, Any] = {"concept": concept_id, "state": state, "at": timestamp}
    if note:
        override["note"] = note.strip()
    overrides.append(override)
    updated["overrides"] = overrides
    return updated


def profile_hint(profile: dict[str, Any], concept_ids: list[str] | None = None) -> str | None:
    """给讲解提示词用的画像素描（只取状态，不泄漏任何内部细节）。"""
    concepts = profile.get("concepts") or {}
    lines: list[str] = []
    for cid, entry in concepts.items():
        if concept_ids is not None and cid not in concept_ids:
            continue
        state = (entry or {}).get("state")
        if state in STATES:
            lines.append(f"{cid}={state}")
    return "；".join(lines) if lines else None
