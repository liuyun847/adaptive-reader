"""读者画像 Profile：``profiles/<reader>/profile.yaml``（DESIGN §4.3 的字段原样）。

```yaml
reader: local-default
background:                     # 读前背景问卷的答案块（可缺；形态见 PLAN-background-survey §6.1）
  version: 1
  answered_at: "..."
  education: undergraduate
  high_school_electives: [physics, chemistry, biology]
  university:                   # 大学两项**嵌套**（契约 §6.1）
    discipline: economics
    major: "国际经济与贸易"
  strengths: [economics_finance, programming_computer]
  strengths_note: "读过《资本论》第一卷前两章"
  followups: [{q: "...", a: "...", at: "..."}]
concepts:
  c07: {state: partial, confidence: 0.62, evidence: [{source: quiz, at: ..., detail: ...}]}
  c09:                          # 行为信号只往 concepts[cid] **内层**加计数器（顶层键集不变）
    state: unknown
    confidence: 0.25
    signals: {score: -1.5, negative: 2, positive: 0, last_at: "...", history: [...]}
preferences:
  - {id: pref_paragraph_len, statement: "偏好短段落", confidence: 0.40, evidence: [...]}
  - id: pref_fact_anchor                     # 事实锚口径（PLAN-actions-feedback §14.1）
    statement: "要结论：允许在申报后丢掉纯举例性的数据、把引文改成转述"
    value: allow_declared_drops              # keep_all | allow_declared_drops（唯一来源在 ar.fidelity）
    confidence: 0.7
    evidence: [{source: manual, at: ..., detail: "读者把事实锚口径设为 allow_declared_drops"}]
overrides:                      # 用户手动推翻，优先级最高，行为信号不得覆盖
  - {concept: c01, state: known, at: ..., note: "别再解释了"}
```

画像有三个写入来源：「自评勾选」（``apply_rating``）、「读前背景问卷」
（``ar.survey.save_background``）与「行为信号回流」（``applySignals``，PLAN-actions-feedback §7.3）。
自评只反映「以为知道」，所以给的把握度是 ``DEFAULT_CONFIDENCE``（不是 1.0）。
第四个写入来源是**显式偏好设置**（``set_preference``）：它由调用方（``POST /api/profile/preference``）
**明确要求**才写，是「事实锚口径」这类偏好的**唯一创建路径**；
``applySignals`` 的偏好分支仍然**只给已有偏好追加 evidence、绝不自动新建偏好**——两条路泾渭分明。

``preferences[]`` 里的条目是**原样保留**的（``_normalize`` 只保证它是 dict 列表），
所以往里加内层键（``value``）不需要动顶层白名单；``pref_fact_anchor`` 的取值由
``ar.fidelity.FACT_ANCHOR_POLICIES`` 兜底校验，取值读坏了**一律退到严口径**
（``resolve_fact_anchor_policy`` 会同时给出人读来源与一条可见降级，绝不静默放宽）。

``applySignals`` 的计数器放在 **``concepts[cid]`` 内层**（``signals.{score,negative,positive,last_at,history[]}``），
**不动顶层键白名单**：动顶层就要同步 ``default_profile`` / ``_normalize`` / ``__all__``
与 ``tests/test_profile.py`` 的键集断言。它也不写盘（纯函数），写盘是调用方的事。

``background`` 必须在顶层键白名单里：``_normalize`` 是**白名单重建**，
漏掉的键会被静默丢弃（不报错、不进 ``degraded``）；老画像没有该键时补空字典，不报错。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

from .errors import InputError
from .fidelity import DEFAULT_FACT_ANCHOR_POLICY, FACT_ANCHOR_POLICIES, normalize_fact_anchor_policy
from .jsonio import now_iso, write_text
from .paths import DEFAULT_READER, profile_path

__all__ = [
    "STATES",
    "DEFAULT_CONFIDENCE",
    "HALF_LIFE_DAYS",
    "STATE_THRESHOLD",
    "MIN_SAME_DIRECTION_EVIDENCE",
    "CONFIDENCE_TARGETS",
    "DEFAULT_READER_CONFIDENCE",
    "FACT_ANCHOR_PREFERENCE_ID",
    "FACT_ANCHOR_POLICY_VALUES",
    "FACT_ANCHOR_STATEMENTS",
    "PREFERENCE_VALUES",
    "FactAnchorPolicy",
    "ProfileUpdate",
    "default_profile",
    "load_profile",
    "save_profile",
    "apply_rating",
    "set_preference",
    "preference_entry",
    "fact_anchor_policy",
    "resolve_fact_anchor_policy",
    "applySignals",
    "affected_paragraphs",
    "profile_hint",
    "background_of",
]

STATES = ("known", "partial", "unknown")

# 自评是显式信号，但「自评只反映以为知道」（DESIGN §5），所以不给满分
DEFAULT_CONFIDENCE = 0.7

# ------------------------------------------------- 偏好：事实锚口径（§14.1） --
#: 「事实锚口径」这条偏好的 id。它是**读者画像里的一条偏好**，不是全局开关、也不是一次性 CLI flag：
#: 画像一变只重跑受影响的段（同一条链路见 ``--from-signals``）。
FACT_ANCHOR_PREFERENCE_ID = "pref_fact_anchor"
#: 该偏好的可选值（唯一来源在 ``ar.fidelity``：这两档就是校验口径本身）
FACT_ANCHOR_POLICY_VALUES = FACT_ANCHOR_POLICIES
#: 取值 → 写进 ``preferences[].statement`` 的固定说法（口径一变，说法跟着变，不允许两者打架）
FACT_ANCHOR_STATEMENTS = {
    "keep_all": "事实锚全保留（要素材）：数字 / 引号引文 / 书名号一个都不许少",
    "allow_declared_drops": "要结论：允许在申报后丢掉纯举例性的数据、把引文改成转述",
}
#: **可写偏好 → 可选值表**（``set_preference`` 与 ``POST /api/profile/preference`` 的唯一判据）。
#: 没列在这里的偏好（例如行为回流用的 ``pref_paragraph_len``）没有可选值，也就不接受写入。
PREFERENCE_VALUES: dict[str, tuple[str, ...]] = {FACT_ANCHOR_PREFERENCE_ID: FACT_ANCHOR_POLICY_VALUES}

# --------------------------------------------------------- 行为回流（PLAN §7.3） --
#: 既有 score 的半衰期（天）：合并时乘 ``0.5 ** (Δdays / 14)``
HALF_LIFE_DAYS = 14.0
#: 改 state 需要的总分数（绝对值）
STATE_THRESHOLD = 1.2
#: 改 state 需要的**同向**证据条数（来自 ≥2 个不同段或不同日期）
MIN_SAME_DIRECTION_EVIDENCE = 2
#: 每个状态的目标把握度
CONFIDENCE_TARGETS = {"known": 0.8, "partial": 0.55, "unknown": 0.25}
#: 没有 confidence 的概念被行为信号推动时，从目标值起步
DEFAULT_READER_CONFIDENCE = CONFIDENCE_TARGETS["partial"]
#: 把握度每 1.0 分移动的幅度，以及单次移动的上下限、总域
CONFIDENCE_STEP_PER_POINT = 0.15
CONFIDENCE_STEP_MIN = 0.05
CONFIDENCE_STEP_MAX = 0.2
CONFIDENCE_FLOOR = 0.05
CONFIDENCE_CEILING = 0.95
#: 行为信号写进 evidence / history 的 detail 长度上限（防产物被长文本撑爆）
DETAIL_LIMIT = 200


def default_profile(reader: str = DEFAULT_READER) -> dict[str, Any]:
    """空画像骨架（五个顶层键与 DESIGN §4.3 + 读前背景问卷 §6.1 一致）。

    ``background`` 是「读前背景问卷」的答案块（可缺）：得先在白名单里，
    否则 ``_normalize`` 重建画像时会把它**静默丢掉**（新字段一个都存不下来）。
    """
    return {"reader": reader, "background": {}, "concepts": {}, "preferences": [], "overrides": []}


def _normalize(data: Any, reader: str) -> dict[str, Any]:
    if data is None:
        return default_profile(reader)
    if not isinstance(data, dict):
        raise InputError("profile.yaml 根节点必须是映射")
    profile = default_profile(reader)
    profile["reader"] = str(data.get("reader") or reader)
    background = data.get("background")
    if isinstance(background, dict):
        profile["background"] = _normalize_background(background)
    concepts = data.get("concepts") or {}
    if not isinstance(concepts, dict):
        raise InputError("profile.yaml 的 concepts 必须是映射（概念 id → 状态）")
    profile["concepts"] = {str(k): v if isinstance(v, dict) else {} for k, v in concepts.items()}
    for field_name in ("preferences", "overrides"):
        value = data.get(field_name) or []
        if not isinstance(value, list):
            raise InputError(f"profile.yaml 的 {field_name} 必须是数组")
        profile[field_name] = [item for item in value if isinstance(item, dict)]
    return profile


def _normalize_background(value: Any) -> dict[str, Any]:
    """背景块**原样**保留（只做类型兜底），删除键的工作交给 ``ar.survey``。

    这里刻意不复刻问卷的枚举 / 条件题规则：画像模块不认识问卷版本，
    硬校验会让老画像（问卷升版后）读不出来。``ar.survey`` 是唯一的语义入口。
    """
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


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
    """写回画像（走 ``ar.jsonio`` 的原子写：临时文件 + ``replace``）。

    **序列化结果与现有文件逐字相同就不写盘**（PLAN §1 第 9 条）：画像指纹是
    ``(路径, 字节数, mtime_ns)``，一次无谓的写盘会换指纹 ⇒ 讲解 LRU 全失效 + 前端会话键变化。
    内容变了才写（临时文件 + ``replace`` 的原子写照旧）。

    写盘走 :func:`ar.jsonio.write_text`（**全项目唯一一份写盘实现**，2026-10-14 复核补漏）：
    此前是本模块手写的「固定临时名 ``profile.yaml.tmp`` + 裸 ``write_text`` / ``replace``」，
    目录不可写时抛的是**裸 ``PermissionError``** ⇒ CLI 直接 traceback、退出码 1，
    HTTP 上只有一句「服务内部错误：PermissionError」。现在与其它写盘失败同一口径：
    立刻抛 :class:`~ar.errors.WriteError`（点名目录与原始 errno，CLI 退出码 2、
    HTTP 500 + 可读消息），失败时临时文件一定清干净。
    """
    path = profile_path(profiles_dir, reader)
    text = yaml.safe_dump(profile, allow_unicode=True, sort_keys=False, default_flow_style=False)
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == text:
                return path
        except (OSError, UnicodeDecodeError):
            pass  # 读不出来就当内容不同，照常写
    return write_text(path, text)


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


# ------------------------------------------------- 偏好：读 / 写 / 创建路径 --
def preference_entry(profile: Any, preference_id: str) -> dict[str, Any] | None:
    """取画像里某条偏好（``preferences[]`` 里 ``id`` 命中的第一条）；没有就返回 ``None``。"""
    if not isinstance(profile, dict):
        return None
    for item in profile.get("preferences") or []:
        if isinstance(item, dict) and str(item.get("id") or "").strip() == preference_id:
            return item
    return None


@dataclass(frozen=True)
class FactAnchorPolicy:
    """画像里「事实锚口径」那条偏好的解析结果（§14.1）。

    - ``policy``：生效口径（``ar.fidelity.FACT_ANCHOR_POLICIES`` 之一）；
    - ``source``：人读的来源说明（CLI 直接打印，读者一眼看得出这次凭什么口径校验）；
    - ``warning``：偏好写坏了（没有 ``value`` / 取值不认识）时的可见降级文案，``None`` = 正常。

    **缺省与读坏一律退到严口径**（``keep_all``）：口径读不通只会更严格，绝不悄悄放宽事实锚。
    """

    policy: str
    source: str
    warning: str | None = None


def resolve_fact_anchor_policy(profile: Any) -> FactAnchorPolicy:
    """解析读者的「事实锚口径」偏好（缺省 / 读坏 → 严口径 + 可见降级说明）。"""
    entry = preference_entry(profile, FACT_ANCHOR_PREFERENCE_ID)
    if entry is None:
        return FactAnchorPolicy(
            policy=DEFAULT_FACT_ANCHOR_POLICY,
            source=(
                f"画像里没有偏好 {FACT_ANCHOR_PREFERENCE_ID}：按默认 {DEFAULT_FACT_ANCHOR_POLICY}"
                "（严口径，一个都不许少）办"
            ),
        )
    raw = entry.get("value")
    if not isinstance(raw, str) or not raw.strip():
        return FactAnchorPolicy(
            policy=DEFAULT_FACT_ANCHOR_POLICY,
            source=f"偏好 {FACT_ANCHOR_PREFERENCE_ID} 没有 value：按默认 {DEFAULT_FACT_ANCHOR_POLICY} 办",
            warning=(
                f"偏好 {FACT_ANCHOR_PREFERENCE_ID} 没有 value（只能是 "
                f"{' / '.join(FACT_ANCHOR_POLICY_VALUES)}）：已按严口径 {DEFAULT_FACT_ANCHOR_POLICY} 处理"
            ),
        )
    policy = normalize_fact_anchor_policy(raw)
    if policy != raw.strip():
        return FactAnchorPolicy(
            policy=policy,
            source=f"偏好 {FACT_ANCHOR_PREFERENCE_ID} 的取值 {raw.strip()!r} 不认识：按严口径 {policy} 办",
            warning=(
                f"偏好 {FACT_ANCHOR_PREFERENCE_ID} 的取值 {raw.strip()!r} 不认识（只能是 "
                f"{' / '.join(FACT_ANCHOR_POLICY_VALUES)}）：已按严口径 {policy} 处理（不静默放宽）"
            ),
        )
    return FactAnchorPolicy(
        policy=policy, source=f"画像偏好 {FACT_ANCHOR_PREFERENCE_ID} = {policy}"
    )


def fact_anchor_policy(profile: Any) -> str:
    """读者当前生效的事实锚口径（``keep_all`` / ``allow_declared_drops``）。

    缺省、偏好写坏、取值不认识一律退到 :data:`ar.fidelity.DEFAULT_FACT_ANCHOR_POLICY`（严）。
    要「为什么是这个口径」的人读说明与可见降级，用 :func:`resolve_fact_anchor_policy`。
    """
    return resolve_fact_anchor_policy(profile).policy


def set_preference(
    profile: dict[str, Any],
    preference_id: str,
    value: str,
    note: str | None = None,
    *,
    at: str | None = None,
) -> dict[str, Any]:
    """**显式**写入一条偏好（纯函数，写盘是调用方的事）：没有就**创建**，有就改 ``value`` + 追记证据。

    这是偏好（尤其「事实锚口径」``pref_fact_anchor``）的**唯一创建路径**：
    ``applySignals`` 的偏好分支只给已有偏好追加 evidence、绝不自动新建偏好，两条路分工明确
    （行为信号是「猜」出来的，偏好口径是读者**明确设**的，不能让前者凭空造出后者）。

    - ``preference_id`` 必须在 :data:`PREFERENCE_VALUES` 里（否则 ``InputError``）；
    - ``value`` 必须是该偏好的可选值之一（否则 ``InputError`` 并点名合法取值）；
    - 内容没变（同 id 同 value）时**不追加证据、不换时间戳**，返回的画像与传入的逐字相同
      ——调用方据此少写一次盘（画像指纹是 ``(路径, 字节数, mtime_ns)``，白写一次讲解缓存全失效）；
    - 原画像一个字节都不动（先 ``deepcopy`` 再改），返回**新的**画像。
    """
    allowed = PREFERENCE_VALUES.get(preference_id)
    if allowed is None:
        raise InputError(
            f"偏好 {preference_id!r} 不接受写入：可写的偏好只有 {' / '.join(PREFERENCE_VALUES)}"
        )
    if not isinstance(value, str) or value.strip() not in allowed:
        raise InputError(
            f"偏好 {preference_id} 的取值非法：{value!r}（只能是 {' / '.join(allowed)}）"
        )
    wanted = value.strip()
    updated = _normalize(copy.deepcopy(profile), str((profile or {}).get("reader") or DEFAULT_READER))
    entries = [item for item in updated.get("preferences") or [] if isinstance(item, dict)]
    target = next((item for item in entries if str(item.get("id") or "").strip() == preference_id), None)
    if target is not None and str(target.get("value") or "").strip() == wanted:
        return updated  # 同 id 同取值：不追加证据、不换时间戳（幂等，调用方据此不写盘）
    timestamp = at or now_iso()
    if target is None:
        target = {"id": preference_id, "confidence": DEFAULT_CONFIDENCE, "evidence": []}
        entries.append(target)
    target["value"] = wanted
    target["statement"] = FACT_ANCHOR_STATEMENTS.get(wanted, str(target.get("statement") or ""))
    detail = note.strip() if isinstance(note, str) and note.strip() else f"读者把偏好 {preference_id} 设为 {wanted}"
    evidence = [item for item in (target.get("evidence") or []) if isinstance(item, dict)]
    evidence.append({"source": "manual", "at": timestamp, "detail": detail})
    target["evidence"] = evidence
    updated["preferences"] = entries
    return updated


# --------------------------------------------------------------- 行为信号合并 --
@dataclass(frozen=True)
class ProfileUpdate:
    """``applySignals`` 的结果（PLAN §7.3）。

    - ``profile``：**新的**画像（原对象一个字节都不动，纯函数）；
    - ``changed``：真的动了 ``state`` 的概念，``{concept, from, to, confidence_from, confidence_to, score, evidence}``
      —— 只有这一组会改变改写提示词的可见面（``cid=state``），所以 ``--from-signals`` 只看它；
    - ``skipped_overrides``：被 ``overrides`` 保护、整条跳过的概念（``{concept, override_state}``）；
    - ``accumulated``：只累加了 ``score`` / ``evidence``、没到阈值的概念（``state`` / ``confidence`` 逐字不变）；
    - ``evidence_added``：这次往 ``concepts[].evidence`` / ``preferences[].evidence`` 里追加的条数；
    - ``degraded``：可见降级说明（信号不合法、偏好不存在 …）。
    """

    profile: dict[str, Any]
    changed: list[dict[str, Any]] = field(default_factory=list)
    skipped_overrides: list[dict[str, Any]] = field(default_factory=list)
    accumulated: list[dict[str, Any]] = field(default_factory=list)
    evidence_added: int = 0
    degraded: list[str] = field(default_factory=list)


def _as_float(value: Any, default: float | None = 0.0) -> float | None:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _decay_factor(last_at: Any, now: datetime | None) -> float:
    """``0.5 ** (Δdays / 14)``；没有 ``last_at`` / 时间戳不合法 / 时间倒流 → 不衰减。"""
    older = _parse_ts(last_at)
    if older is None or now is None:
        return 1.0
    if older.tzinfo is None and now.tzinfo is not None:
        older = older.replace(tzinfo=now.tzinfo)
    elif now.tzinfo is None and older.tzinfo is not None:
        now = now.replace(tzinfo=older.tzinfo)
    days = (now - older).total_seconds() / 86400.0
    if days <= 0:
        return 1.0
    return 0.5 ** (days / HALF_LIFE_DAYS)


def _evidence_key(entry: dict[str, Any]) -> str:
    """同向证据的「不同来源」键：优先段 id，没有段就用日期（读完类事件没有段）。"""
    paragraph = str(entry.get("paragraph") or "").strip()
    if paragraph:
        return f"p:{paragraph}"
    return f"d:{str(entry.get('at') or '')[:10]}"


def _next_state(state: str, score: float, negative: int, positive: int) -> tuple[str, bool]:
    """按阈值 + 同向证据条数决定新状态；一次只升降一档（``known ↔ partial ↔ unknown``）。"""
    if abs(score) < STATE_THRESHOLD:
        return state, False
    if score < 0:
        if negative < MIN_SAME_DIRECTION_EVIDENCE:
            return state, False
        step = 1  # 往「更不知道」走
    else:
        if positive < MIN_SAME_DIRECTION_EVIDENCE:
            return state, False
        step = -1  # 往「更知道」走
    index = STATES.index(state if state in STATES else "unknown")
    moved_index = min(max(index + step, 0), len(STATES) - 1)
    return STATES[moved_index], moved_index != index


def _move_confidence(current: float, target: float, score: float) -> float:
    """接受改动时朝目标移动 ``clamp(0.15 * |score|, 0.05, 0.2)``，总域 clamp 到 ``[0.05, 0.95]``。"""
    step = min(CONFIDENCE_STEP_MAX, max(CONFIDENCE_STEP_MIN, CONFIDENCE_STEP_PER_POINT * abs(score)))
    if target > current:
        moved = min(target, current + step)
    elif target < current:
        moved = max(target, current - step)
    else:
        moved = current
    return round(min(CONFIDENCE_CEILING, max(CONFIDENCE_FLOOR, moved)), 4)


def applySignals(
    profile: dict[str, Any],
    signals: Iterable[dict[str, Any]],
    *,
    now: str | None = None,
) -> ProfileUpdate:
    """把行为信号合并进画像，返回 :class:`ProfileUpdate`（**纯函数，不碰磁盘**）。

    信号形状 ``{concept, paragraph, kind, weight, at, detail}``（见 ``ar.events.signals_from_events``）；
    带 ``preference`` 键的信号走偏好分支（只追加 ``pref_paragraph_len`` 的 evidence）。

    规则（PLAN §7.3，逐条钉死）：

    1. 既有 ``score`` 先按 14 天半衰期衰减（:data:`HALF_LIFE_DAYS`），再加上本次权重；
    2. ``|score| ≥ 1.2`` **且同向证据 ≥2 条**（来自 ≥2 个不同段或不同日期）才允许改 ``state``；
    3. 一次只升降一档；``confidence`` 朝目标移动，总域 clamp 到 ``[0.05, 0.95]``；
    4. **未达阈值完全不动 ``state`` 与 ``confidence``**（改写提示词的可见面逐字不变，前缀缓存不受影响）；
    5. ``overrides`` 命中的概念**整条跳过**（不累加、不记 evidence），返回 ``skipped_overrides``，
       ``overrides`` 数组**一个字节都不改**；
    6. 计数器写在 ``concepts[cid].signals`` 内层，顶层键集不变。

    **只有出现在 ``signals`` 里的概念会被评估**（衰减 + 阈值判定都只对这些概念做）：
    没收到行为信号的概念一个字节都不动，画像不会因为「时间流逝」自己变。
    """
    stamp = now or now_iso()
    now_dt = _parse_ts(stamp)
    source = profile if isinstance(profile, dict) else {}
    updated = _normalize(copy.deepcopy(source), str(source.get("reader") or DEFAULT_READER))

    protected: dict[str, str] = {}
    for override in updated.get("overrides") or []:
        if not isinstance(override, dict):
            continue
        concept = override.get("concept")
        state = override.get("state")
        if isinstance(concept, str) and concept.strip() and state in STATES:
            protected[concept.strip()] = str(state)
    overrides_snapshot = copy.deepcopy(updated.get("overrides") or [])

    grouped: dict[str, list[dict[str, Any]]] = {}
    preference_signals: list[dict[str, Any]] = []
    degraded: list[str] = []
    for raw in signals or []:
        if not isinstance(raw, dict):
            degraded.append("信号不是 JSON 对象：已跳过")
            continue
        preference = raw.get("preference")
        if isinstance(preference, str) and preference.strip():
            preference_signals.append(raw)
            continue
        concept = raw.get("concept")
        if not isinstance(concept, str) or not concept.strip():
            degraded.append("信号缺 concept：已跳过")
            continue
        grouped.setdefault(concept.strip(), []).append(raw)

    changed: list[dict[str, Any]] = []
    accumulated: list[dict[str, Any]] = []
    skipped_overrides: list[dict[str, Any]] = []
    evidence_added = 0

    for concept, items in grouped.items():
        if concept in protected:
            # overrides 是「用户手动推翻」，优先级最高：整条跳过，连 evidence 都不记（PLAN §7.3）
            skipped_overrides.append({"concept": concept, "override_state": protected[concept]})
            continue

        entry = dict(updated["concepts"].get(concept) or {})
        counters = entry.get("signals") if isinstance(entry.get("signals"), dict) else {}
        score = _as_float(counters.get("score"), 0.0) or 0.0
        score = round(score * _decay_factor(counters.get("last_at"), now_dt), 4)
        history = [item for item in (counters.get("history") or []) if isinstance(item, dict)]
        evidence = [item for item in (entry.get("evidence") or []) if isinstance(item, dict)]

        new_evidence: list[dict[str, Any]] = []
        for raw in items:
            weight = _as_float(raw.get("weight"), 0.0) or 0.0
            at = str(raw.get("at") or stamp)
            detail = str(raw.get("detail") or "").strip() or f"行为信号：{raw.get('kind') or '未知'}"
            detail = detail[:DETAIL_LIMIT]
            history.append(
                {
                    "at": at,
                    "kind": str(raw.get("kind") or ""),
                    "paragraph": str(raw.get("paragraph") or ""),
                    "weight": round(weight, 4),
                    "detail": detail,
                }
            )
            new_evidence.append({"source": "behavior", "at": at, "detail": detail})
            score = round(score + weight, 4)

        negative = {_evidence_key(item) for item in history if (_as_float(item.get("weight"), 0.0) or 0.0) < 0}
        positive = {_evidence_key(item) for item in history if (_as_float(item.get("weight"), 0.0) or 0.0) > 0}
        declared = entry.get("state") if entry.get("state") in STATES else None
        base_state = declared or "unknown"
        new_state, moved = _next_state(base_state, score, len(negative), len(positive))

        entry["signals"] = {
            "score": score,
            "negative": len(negative),
            "positive": len(positive),
            "last_at": stamp,
            "history": history,
        }
        entry["evidence"] = evidence + new_evidence
        evidence_added += len(new_evidence)

        detail_row = {
            "concept": concept,
            "score": score,
            "negative": len(negative),
            "positive": len(positive),
            "evidence": len(new_evidence),
        }
        if moved:
            confidence_before = _as_float(entry.get("confidence"), None)
            if confidence_before is None:
                confidence_before = DEFAULT_READER_CONFIDENCE
            confidence_after = _move_confidence(confidence_before, CONFIDENCE_TARGETS[new_state], score)
            entry["state"] = new_state
            entry["confidence"] = confidence_after
            detail_row.update(
                {
                    "from": base_state if declared else None,
                    "to": new_state,
                    "confidence_from": confidence_before if declared else None,
                    "confidence_to": confidence_after,
                }
            )
            changed.append(detail_row)
        else:
            accumulated.append(detail_row)
        updated["concepts"][concept] = entry

    preference_updates: list[dict[str, Any]] = []
    for raw in preference_signals:
        pid = str(raw.get("preference")).strip()
        target_entry = next(
            (
                item
                for item in updated.get("preferences") or []
                if isinstance(item, dict) and item.get("id") == pid
            ),
            None,
        )
        if target_entry is None:
            degraded.append(
                f"画像里没有偏好 {pid}：行为信号只给已有偏好追加 evidence，绝不自动新建偏好（该信号已跳过）"
            )
            continue
        at = str(raw.get("at") or stamp)
        detail = str(raw.get("detail") or "").strip() or f"行为信号：{raw.get('kind') or '未知'}"
        detail = detail[:DETAIL_LIMIT]
        existing = [item for item in (target_entry.get("evidence") or []) if isinstance(item, dict)]
        target_entry["evidence"] = existing + [{"source": "behavior", "at": at, "detail": detail}]
        evidence_added += 1
        preference_updates.append({"preference": pid, "paragraph": str(raw.get("paragraph") or ""), "detail": detail})

    if updated.get("overrides") != overrides_snapshot:  # pragma: no cover - 契约防线：这行不该发生
        raise AssertionError("applySignals 不许改动 overrides")

    return ProfileUpdate(
        profile=updated,
        changed=changed,
        skipped_overrides=skipped_overrides,
        accumulated=accumulated,
        evidence_added=evidence_added,
        degraded=degraded,
    )


def affected_paragraphs(update: Any, inventory: Any) -> set[str]:
    """``changed[].concept`` 经 ``concepts.json`` 的 ``concepts[].paragraphs`` 反查成段 id 集合。

    给 ``ar rewrite --from-signals`` 用：只重跑**状态真的变了**的概念所在段。
    ``update`` 收 :class:`ProfileUpdate`（也容忍同形的字典）。
    """
    changed = getattr(update, "changed", None)
    if changed is None and isinstance(update, dict):
        changed = update.get("changed")
    concept_ids = {
        str(item.get("concept"))
        for item in (changed or [])
        if isinstance(item, dict) and item.get("concept")
    }
    paragraphs: set[str] = set()
    items = inventory.get("concepts") if isinstance(inventory, dict) else None
    for item in items or []:
        if not isinstance(item, dict) or str(item.get("id")) not in concept_ids:
            continue
        for paragraph in item.get("paragraphs") or []:
            if isinstance(paragraph, str) and paragraph.strip():
                paragraphs.add(paragraph.strip())
    return paragraphs


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


def background_of(profile: Any) -> dict[str, Any]:
    """取画像的 ``background``（老画像没有该键 / 不是映射 → 空字典，不报错）。

    读取路径上**不解析语义**（枚举 / 条件题规则在 ``ar.survey``）：老画像照样读得出来。
    「这次到底算不算有背景」由 ``ar.survey.has_background`` 判定。
    """
    if not isinstance(profile, dict):
        return {}
    value = profile.get("background")
    return value if isinstance(value, dict) else {}
