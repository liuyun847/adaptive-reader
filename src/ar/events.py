"""行为事件流：``profiles/<reader>/events.jsonl``（PLAN-actions-feedback §7.1–§7.3 的库侧）。

一行一条 JSON（``ensure_ascii=False``、追加写、结尾换行）。本模块**不发任何模型请求**，
也**不进 ``ar.tools`` 的注册表**：它只做三件事——落盘、按 ``id`` 去重、把事件折算成
``ar.profile.applySignals`` 吃得下的信号。

事件形状（``v`` / ``id`` / ``at`` / ``kind`` / ``source_id`` / ``reader`` 是公共字段，
其余键按 ``kind`` 各自可选）：

```json
{"v":1,"id":"<客户端 uuid>","at":"2026-10-13T18:20:00+08:00","kind":"explain",
 "source_id":"capital-v1-ch01","reader":"local-default","paragraph":"p012",
 "explain_kind":"explain","scope":"paragraph","cached":false,"degraded":false}
```

- ``kind`` 白名单见 :data:`EVENT_KINDS`；**多给的键原样保留**（本模块不认识的新字段不许静默丢掉）。
- ``reader`` 一律**由服务端盖章**（:func:`validate_event`）：请求里给了也不认 —— 一个
  ``events.jsonl`` 只属于一个读者。
- ``id`` 用于幂等去重；缺 ``id`` 时 :func:`event_id` 用 ``(kind, source_id, paragraph, at)`` 复合键兜底。
- 落盘位置与画像同级（``profiles/<reader>/``），但**不参与缓存指纹**：写事件不会换画像指纹，
  讲解 LRU 不受影响（PLAN §1 第 8 条）。

**回流水位线 ``profiles/<reader>/feedback.json``**（2026-10-13 复核修复 F2，照 ``survey.json`` 的先例）：
``ar feedback --apply`` 把本次**看过**的事件 id 记进这份读者级记录文件（保留最近
:data:`FEEDBACK_RECORD_KEEP` 条）+ ``last_at`` 水位线，于是同一份事件重复跑 ``--apply`` 不会重复折算
（没有它，``score`` / ``evidence`` 会一路叠加、把 ``state`` 推过阈值）。这份文件**不进版本库**
（``profiles/`` 已忽略）、**不写 ``profile.yaml`` 的任何新键**、**不参与画像指纹**
（指纹只看 ``profile.yaml`` 的路径 / 字节数 / mtime_ns）。判「已处理」**只看 id 集合**，
不按时间跳过：多材料交错 + 客户端时钟偏移下，按时间水位线跳过会丢事件（``last_at`` 因此是留痕，不是判据）。

**事件 → 信号**（:func:`signals_from_events`）按 PLAN §7.3 的权重草案折算，
信号形状 ``{concept, paragraph, kind, weight, at, detail}``：

===================  ====================================  ========
事件 kind             条件                                    权重
===================  ====================================  ========
``explain``           整段求助（``scope`` 不是 selection）      −1.0
``explain``           ``scope=selection``（选片求助更轻）        −0.5
``compress``          太啰嗦                                   −0.75
``read_complete``     该概念本批**一次都没求助**                 +0.25
``dwell``             ``ms < SHORT_DWELL_MS``（短停留）          0.0
===================  ====================================  ========

短停留**不当负信号**（PLAN §1 第 15 条：快速略过也可能只是「已经懂了」），
只留一条 0 权重的 evidence。``read_abandon`` / ``drawer_open`` / ``selection`` / ``skip``
不产生信号——负信号只来自显式求助。偏好类只对 ``pref_paragraph_len`` 追加 evidence
（信号带 ``preference`` 键、不带 ``concept``）：一次 ``compress``（太啰嗦），
或同一个段 ``LONG_DWELL_MIN_COUNT`` 次长停留（长段反复回看）各给一条。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .errors import InputError
from .jsonio import now_iso, readable_write_error, write_json
from .paths import check_reader_id, check_source_id

__all__ = [
    "EVENT_KINDS",
    "SIGNAL_WEIGHTS",
    "SELECTION_WEIGHT",
    "DWELL_WEIGHT",
    "SHORT_DWELL_MS",
    "LONG_DWELL_MS",
    "LONG_DWELL_MIN_COUNT",
    "PREFERENCE_ID",
    "FEEDBACK_RECORD_KEEP",
    "events_path",
    "feedback_record_path",
    "read_events",
    "append_events",
    "event_id",
    "dedup",
    "validate_event",
    "signals_from_events",
    "read_feedback_record",
    "processed_ids",
    "pending_events",
    "merge_processed",
    "write_feedback_record",
]

#: ``kind`` 白名单（PLAN §7.1）；多给别的值一律丢弃 + 可见降级
EVENT_KINDS = (
    "dwell",  # {ms}
    "read_complete",
    "read_abandon",
    "explain",  # {explain_kind, scope, selection_chars, selection_from, cached, degraded}
    "drawer_open",
    "selection",  # {chars, spans, from}
    "compress",  # {ok, reason?}
    "skip",  # {reason}
)

#: 事件 kind → 信号权重（PLAN §7.3 的草案；未列出的 kind 不产生信号）
SIGNAL_WEIGHTS = {
    "explain": -1.0,  # 没懂 / 再简单点 / 再详细点：都是显式求助
    "compress": -0.75,  # 太啰嗦
    "read_complete": 0.25,  # 读完且该概念本批没求助
}
#: 选片求助（``scope=selection``）比整段求助轻
SELECTION_WEIGHT = -0.5
#: 短停留只留痕，**不当负信号**
DWELL_WEIGHT = 0.0

#: 短于这个时长算「短停留」（只记 evidence）
SHORT_DWELL_MS = 1500
#: 长于这个时长算「反复回看」的候选（配合 ``LONG_DWELL_MIN_COUNT`` 出偏好 evidence）
LONG_DWELL_MS = 15000
#: 同一个段长停留到这个次数 → 追加一条 ``pref_paragraph_len`` evidence
LONG_DWELL_MIN_COUNT = 2

#: 偏好类信号只认这一个偏好 id，绝不自动新建偏好
PREFERENCE_ID = "pref_paragraph_len"

#: ``feedback.json`` 里保留的「已处理事件 id」条数上限（更早的 id 被挤出窗口，见 :func:`merge_processed`）
FEEDBACK_RECORD_KEEP = 500

_EXPLAIN_LABELS = {"explain": "没懂", "simpler": "再简单点", "detail": "再详细点"}


# ------------------------------------------------------------------ 路径 / 落盘 --
def events_path(profiles_dir: Path | str, reader: str) -> Path:
    """``profiles/<reader>/events.jsonl``（与画像同级，不参与缓存指纹）。"""
    return Path(profiles_dir) / check_reader_id(reader) / "events.jsonl"


def read_events(profiles_dir: Path | str, reader: str) -> tuple[list[dict[str, Any]], list[str]]:
    """读事件流，返回 ``(事件列表, 降级说明)``。

    文件不存在 → ``([], [])``（还没读过任何东西不是错误）。坏行（不是 JSON / 不是对象）
    **跳过并说明**，不让一行噪声把整份事件流读废（append 路径与 ``ar feedback`` 都靠它）。
    """
    path = events_path(profiles_dir, reader)
    if not path.is_file():
        return [], []
    events: list[dict[str, Any]] = []
    degraded: list[str] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [], [f"events.jsonl 读不出来（按 0 条处理）：{type(exc).__name__}: {exc}"]
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            degraded.append(f"events.jsonl 第 {number} 行不是合法 JSON：已忽略（{exc.msg}）")
            continue
        if not isinstance(payload, dict):
            degraded.append(f"events.jsonl 第 {number} 行不是 JSON 对象：已忽略")
            continue
        events.append(payload)
    return events, degraded


def append_events(profiles_dir: Path | str, reader: str, events: Iterable[dict[str, Any]]) -> int:
    """把事件**追加**到 ``events.jsonl``，返回写进去的条数（一行一条、结尾换行）。

    只 append，不重写整个文件（读者数据只增不减）。去重是调用方的事（见 :func:`dedup`）：
    本函数是「照单落盘」的机械动作。写盘失败（目录不可写 / 磁盘满 / 路径被占）由调用方
    捕获成**可见降级**（``POST /api/events`` 回 200 + ``ok:false`` + ``written:0`` + ``degraded``，
    见 DESIGN §6「行为事件写不进去」）。

    **失败立刻抛、不重试**，且与其它写盘入口同一口径：``OSError`` 一律翻成
    :class:`~ar.errors.WriteError`（点名目录 + 原始 errno，见
    :func:`ar.jsonio.readable_write_error`）。这里**不用**「临时文件 + replace」：
    事件流是纯追加的日志，整文件重写反而会放大丢数据的风险（2026-10-14 复核登记：
    全项目唯一一处非原子写盘入口，理由如此）。
    """
    items = [item for item in events if isinstance(item, dict)]
    if not items:
        return 0
    path = events_path(profiles_dir, reader)
    text = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError as exc:
        raise readable_write_error(path.parent, exc) from exc
    return len(items)


# ---------------------------------------------------------- 回流水位线（F2） --
def feedback_record_path(profiles_dir: Path | str, reader: str) -> Path:
    """``profiles/<reader>/feedback.json``：``ar feedback`` 的已处理事件水位线。

    读者级记录文件，照 ``profiles/<reader>/survey.json`` 的先例：**不进版本库**
    （``profiles/`` 已忽略）、不写 ``profile.yaml`` 的顶层新键、**不参与画像指纹**。
    """
    return Path(profiles_dir) / check_reader_id(reader) / "feedback.json"


def read_feedback_record(profiles_dir: Path | str, reader: str) -> tuple[dict[str, Any], list[str]]:
    """读水位线文件，返回 ``(记录, 降级说明)``。

    不存在 / 读不出来 / 不是 JSON 对象 → ``({}, 说明)``：调用方按「**全部事件都未处理**」折算
    （第一次跑 ``ar feedback`` 属正常，但**不许静默**——文件损坏时丢掉的是幂等，不是数据，
    所以给一条可读说明、``--apply`` 会用正确内容重写它）。
    """
    path = feedback_record_path(profiles_dir, reader)
    if not path.is_file():
        return {}, [
            f"{path} 还不存在：本次按「全部事件都未处理」折算（第一次跑 ar feedback 属正常，"
            "`--apply` 会把它建起来，之后同一份事件不会重复折算）"
        ]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, [
            f"feedback.json 读不出来（本次按「全部事件都未处理」折算，不静默）：{type(exc).__name__}: {exc}"
        ]
    if not isinstance(payload, dict):
        return {}, [f"feedback.json 不是一个 JSON 对象（本次按「全部事件都未处理」折算，不静默）：{type(payload).__name__}"]
    return payload, []


def processed_ids(record: dict[str, Any]) -> tuple[list[str], list[str]]:
    """从水位线记录里取「已处理事件 id」，返回 ``(id 列表, 降级说明)``（保序、去重）。

    ``processed`` 缺失 → 空列表（照「一条都没处理过」办）；不是数组 / 混进非字符串条目 →
    跳过并说明（幂等从句这一轮起重新建立，不静默）。
    """
    raw = record.get("processed")
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], [f"feedback.json 的 processed 不是数组（按「一条都没处理过」办）：{type(raw).__name__}"]
    cleaned = [item.strip() for item in raw if isinstance(item, str) and item.strip()]
    ids = list(dict.fromkeys(cleaned))
    notes: list[str] = []
    if len(ids) != len(raw):
        notes.append(
            f"feedback.json 的 processed 有 {len(raw) - len(ids)} 条不是非空字符串 / 重复：已忽略"
            "（那几条事件可能被重算一次）"
        )
    return ids, notes


def pending_events(
    events: Iterable[dict[str, Any]], processed: Iterable[str]
) -> list[dict[str, Any]]:
    """筛出**未处理**的事件（按 :func:`event_id` 判，保序）。

    判据只有 id 集合，**不按时间跳过**：多材料交错 + 客户端时钟偏移下，时间水位线会吃掉
    新到的旧时间戳事件（``last_at`` 是留痕，不是判据）。
    """
    done = set(processed)
    return [item for item in events if isinstance(item, dict) and event_id(item) not in done]


def merge_processed(
    existing: Iterable[str], new_ids: Iterable[str], *, keep: int = FEEDBACK_RECORD_KEEP
) -> tuple[list[str], list[str]]:
    """把本次处理过的 id 并进已有集合，返回 ``(id 列表, 降级说明)``。

    保序、去重，**只留最近 ``keep`` 条**（§13 的裁决）。窗口溢出时**不静默**：说明里写清
    被挤出的条数与后果（那几条最早的事件若仍在 ``events.jsonl`` 里，下次会被当成新事件再算一遍）。
    """
    merged = list(dict.fromkeys([*existing, *[str(item).strip() for item in new_ids if str(item).strip()]]))
    dropped = len(merged) - int(keep)
    if dropped <= 0:
        return merged, []
    trimmed = merged[dropped:]
    return trimmed, [
        f"已处理 id 窗口（{keep} 条）已满：最早的 {dropped} 条被挤出；"
        "那几条事件若还在 events.jsonl 里，下次会被当成新事件再折算一遍（要保留就把窗口调大）"
    ]


def write_feedback_record(
    profiles_dir: Path | str,
    reader: str,
    *,
    processed: Iterable[str],
    last_at: str = "",
    at: str | None = None,
) -> Path:
    """写 ``feedback.json``：读者 / 更新时间 / ``last_at`` 水位线 / 已处理 id 列表。

    只在**真的新处理了事件**时调用（内容没变就别写：白换一次 mtime）。
    """
    reader = check_reader_id(reader)
    payload = {
        "reader": reader,
        "updated_at": at or now_iso(),
        "last_at": str(last_at or ""),
        "processed": [str(item) for item in processed],
    }
    return write_json(feedback_record_path(profiles_dir, reader), payload)


# -------------------------------------------------------------------- 去重 --
def event_id(event: dict[str, Any]) -> str:
    """事件的幂等键：有非空 ``id`` 就用它，否则用 ``(kind, source_id, paragraph, at)`` 复合键兜底。"""
    raw = event.get("id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return "|".join(
        str(event.get(field) or "") for field in ("kind", "source_id", "paragraph", "at")
    )


def dedup(
    events: Iterable[dict[str, Any]],
    existing: Iterable[dict[str, Any]] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按 :func:`event_id` 去重，返回 ``(新事件, 重复事件)``。

    ``existing`` 是已经在 ``events.jsonl`` 里的旧事件；批内重复也算重复（先到先留）。
    """
    seen = {event_id(item) for item in existing if isinstance(item, dict)}
    fresh: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for item in events:
        if not isinstance(item, dict):
            continue
        key = event_id(item)
        if key in seen:
            duplicates.append(item)
            continue
        seen.add(key)
        fresh.append(item)
    return fresh, duplicates


# ------------------------------------------------------------------ 规范化 --
def validate_event(
    raw: Any, *, reader: str, now: str | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    """把客户端给的一条原始事件规范化成落盘形态，返回 ``(事件, 丢弃原因)``。

    - ``reader`` 一律由服务端盖章（请求里给了也不认）；
    - ``kind`` 不在 :data:`EVENT_KINDS` 白名单 → ``(None, 原因)``，由调用方记进 ``degraded``；
    - ``id`` 缺失 / 空串 → 复合键兜底；``at`` 缺失 → 服务端时间；``v`` 缺失 → 1；
    - 其余键**原样保留**，顺序照请求（``v`` / ``id`` / ``at`` / ``kind`` / ``source_id`` / ``reader`` 先写）；
    - ``source_id`` 非法（缺失 / 空 / 含非法字符）→ 抛 ``InputError``：
      HTTP 层要回 400，这与「丢弃 + degraded」是两种处置，不能混。
    """
    if not isinstance(raw, dict):
        return None, "事件必须是 JSON 对象"
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind.strip() not in EVENT_KINDS:
        return None, f"kind={kind!r} 不在白名单（{'/'.join(EVENT_KINDS)}）里"
    source_id = raw.get("source_id")
    if not isinstance(source_id, str) or not source_id.strip():
        raise InputError("source_id 必须是非空字符串（事件要写明来自哪份材料）")
    stamp = now or now_iso()
    at = raw.get("at")
    event: dict[str, Any] = {
        "v": raw.get("v") if isinstance(raw.get("v"), int) else 1,
        "id": "",
        "at": at.strip() if isinstance(at, str) and at.strip() else stamp,
        "kind": kind.strip(),
        "source_id": check_source_id(source_id.strip()),  # 非法 → InputError（HTTP 400）
        "reader": check_reader_id(reader),
    }
    for key, value in raw.items():
        if key in ("v", "id", "at", "kind", "source_id", "reader"):
            continue
        event[key] = value
    given = raw.get("id")
    event["id"] = given.strip() if isinstance(given, str) and given.strip() else event_id(event)
    return event, None


# -------------------------------------------------------------- 事件 → 信号 --
def _concept_index(inventory: Any) -> tuple[dict[str, list[str]], list[str]]:
    """``concepts.json`` → ``(段 id → 概念 id 列表, 概念 id 列表)``。"""
    by_paragraph: dict[str, list[str]] = {}
    concept_ids: list[str] = []
    items = inventory.get("concepts") if isinstance(inventory, dict) else None
    if not isinstance(items, list):
        return by_paragraph, concept_ids
    for item in items:
        if not isinstance(item, dict):
            continue
        cid = item.get("id")
        if not isinstance(cid, str) or not cid.strip():
            continue
        cid = cid.strip()
        concept_ids.append(cid)
        for paragraph in item.get("paragraphs") or []:
            if isinstance(paragraph, str) and paragraph.strip():
                by_paragraph.setdefault(paragraph.strip(), []).append(cid)
    return by_paragraph, concept_ids


def _at_of(event: dict[str, Any], fallback: str) -> str:
    at = event.get("at")
    return at.strip() if isinstance(at, str) and at.strip() else fallback


def signals_from_events(
    events: Iterable[dict[str, Any]],
    inventory: Any,
    *,
    source_id: str | None = None,
    now: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把事件折算成画像合并规则吃得下的信号，返回 ``(信号列表, 降级说明)``。

    ``inventory`` 是该材料的 ``concepts.json``（段 → 概念的反查表来自
    ``concepts[].paragraphs``）。``source_id`` 给了就只折算该材料的事件
    （``ar feedback <source_id>`` 的口径）；没给则折算全部，并在事件来自多个材料时
    提示「段 → 概念反查可能串味」。

    **纯函数**：不读盘、不写盘、不发请求（事件与概念清单都由调用方读好传进来）。
    未被折算的事件（未知 kind / 段不在概念清单里）一律进降级说明，不静默丢。
    """
    by_paragraph, concept_ids = _concept_index(inventory)
    notes: list[str] = []
    seen_notes: set[str] = set()

    def note(message: str) -> None:
        if message not in seen_notes:
            seen_notes.add(message)
            notes.append(message)

    stamp = now or now_iso()
    usable: list[dict[str, Any]] = []
    sources: set[str] = set()
    for raw in events or []:
        if not isinstance(raw, dict):
            note("事件不是 JSON 对象：未折算（跳过）")
            continue
        kind = raw.get("kind")
        if not isinstance(kind, str) or kind not in EVENT_KINDS:
            note(f"未知事件类型 kind={kind!r}：未折算（跳过）")
            continue
        event_source = raw.get("source_id")
        if source_id is not None and event_source != source_id:
            continue
        if isinstance(event_source, str) and event_source:
            sources.add(event_source)
        usable.append(raw)
    if source_id is None and len(sources) > 1:
        note(
            f"事件来自多个材料（{len(sources)} 份）：段 → 概念只用一份概念清单反查，可能串味；"
            "建议按 source_id 过滤（`ar feedback <source_id>`）"
        )

    signals: list[dict[str, Any]] = []
    helped: set[str] = set()  # 本批里求助过的概念（read_complete 的正向证据要排除它们）
    read_completes: list[str] = []
    long_dwells: dict[str, int] = {}
    last_long_dwell_at: dict[str, str] = {}
    verbose_paragraphs: dict[str, str] = {}

    def emit(paragraph: str, weight: float, detail: str, kind: str, at: str) -> None:
        cids = by_paragraph.get(paragraph)
        if not cids:
            note(f"段 {paragraph or '(无)'} 不在概念清单的任何 concepts[].paragraphs 里：该类事件未折算（跳过）")
            return
        for cid in cids:
            signals.append(
                {"concept": cid, "paragraph": paragraph, "kind": kind, "weight": weight, "at": at, "detail": detail}
            )
            if weight < 0:
                helped.add(cid)

    for event in usable:
        kind = event["kind"]
        at = _at_of(event, stamp)
        paragraph = event.get("paragraph")
        paragraph = paragraph.strip() if isinstance(paragraph, str) and paragraph.strip() else ""

        if kind == "explain":
            scope = event.get("scope")
            scope = scope.strip() if isinstance(scope, str) and scope.strip() else "paragraph"
            explain_kind = event.get("explain_kind")
            label = _EXPLAIN_LABELS.get(str(explain_kind), "求助讲解")
            if scope == "selection":
                delta = int(event.get("selection_chars") or 0)
                detail = f"选片求助（{label}，选中 {delta} 字）"
                emit(paragraph, SELECTION_WEIGHT, detail, kind, at)
            else:
                emit(paragraph, SIGNAL_WEIGHTS["explain"], f"整段求助（{label}）", kind, at)
            continue

        if kind == "compress":
            ok = event.get("ok")
            reason = event.get("reason")
            suffix = f"（压缩未成功：{reason}）" if ok is False and reason else ""
            detail = f"太啰嗦：请求当场压缩该段{suffix}"
            emit(paragraph, SIGNAL_WEIGHTS["compress"], detail, kind, at)
            if paragraph:
                verbose_paragraphs.setdefault(paragraph, at)
            continue

        if kind == "dwell":
            milliseconds = event.get("ms")
            if not isinstance(milliseconds, (int, float)) or isinstance(milliseconds, bool):
                note("dwell 事件缺 ms（或 ms 不是数字）：未折算（跳过）")
                continue
            if milliseconds < SHORT_DWELL_MS:
                emit(
                    paragraph,
                    DWELL_WEIGHT,
                    f"短停留 {int(milliseconds)} ms：只留痕，不当负信号（快速略过也可能是已经懂了）",
                    kind,
                    at,
                )
            elif milliseconds >= LONG_DWELL_MS and paragraph:
                long_dwells[paragraph] = long_dwells.get(paragraph, 0) + 1
                last_long_dwell_at[paragraph] = at
            continue

        if kind == "read_complete":
            read_completes.append(at)
            continue

        # read_abandon / drawer_open / selection / skip：按契约不产生信号（负信号只来自显式求助）

    for at in read_completes:
        for cid in concept_ids:
            if cid in helped:
                continue
            signals.append(
                {
                    "concept": cid,
                    "paragraph": "",
                    "kind": "read_complete",
                    "weight": SIGNAL_WEIGHTS["read_complete"],
                    "at": at,
                    "detail": "读完材料，且该概念本批一次都没求助",
                }
            )

    # 偏好类：只对 pref_paragraph_len 追加 evidence，绝不自动新建偏好（PLAN §7.3）
    for paragraph, at in verbose_paragraphs.items():
        signals.append(
            {
                "preference": PREFERENCE_ID,
                "paragraph": paragraph,
                "kind": "compress",
                "weight": SIGNAL_WEIGHTS["compress"],
                "at": at,
                "detail": f"段 {paragraph} 被读者标为太啰嗦：偏好短段落",
            }
        )
    for paragraph, count in long_dwells.items():
        if count < LONG_DWELL_MIN_COUNT:
            continue
        signals.append(
            {
                "preference": PREFERENCE_ID,
                "paragraph": paragraph,
                "kind": "dwell",
                "weight": DWELL_WEIGHT,
                "at": last_long_dwell_at.get(paragraph, stamp),
                "detail": f"长段反复回看：段 {paragraph} 单次停留 ≥{LONG_DWELL_MS} ms 共 {count} 次",
            }
        )

    return signals, notes
