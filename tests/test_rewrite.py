"""M3：逐段决策 + 改写 + 事实锚校验（``ar.rewrite`` / ``ar.fidelity`` / CLI 接线）。

覆盖契约 §5 的 M3 验收：

- ``decisions.json`` 每段都有 ``why``、动作只在枚举内；
- ``rewritten.md`` 段数 / 段序与源一致；
- 骨架未实现的 ``compress`` / ``illustrate`` → 按 ``keep`` 处理且标 ``deferred``；
- ``mode=strict`` 明确报错，**不静默按 free 跑、不写任何产物**；
- 空画像时降级在产物里可见（``meta.profile_used=false`` + ``why.profile`` 写「难度 N（无画像）」）；
- 缺段 / 模型调用失败 → 补 ``keep`` 并记 ``meta.degraded``；
- **负向对照**：往改写稿副本改掉一个数字 / 引文 / 书名 / 删一段 → ``check`` 非零退出并点名段 id。
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ar.errors import EXIT_MODEL, EXIT_NOT_IMPLEMENTED, EXIT_OK, EXIT_USAGE, EXIT_VALIDATION
from ar.fidelity import extract_books, extract_numbers, extract_quotes, split_rewritten_blocks
from ar.ingest import ingest
from ar.prompts import build_rewrite_messages
from ar.rewrite import (
    DEFAULT_MODE,
    IMPLEMENTED_ACTIONS,
    STRICT_NOT_IMPLEMENTED,
    check_fidelity,
    fidelity_problems,
    planDecisions,
    rewrite,
    run_rewrite,
)

PROMPT_PARAGRAPH_RE = re.compile(r"\[(p\d{3})\] ")

#: 假模型给每段的决策：``expand`` 走改写文本，``compress`` / ``illustrate`` 走「越界动作」分支
FAKE_ACTIONS = {
    "p001": "keep",
    "p002": "keep",
    "p003": "keep",
    "p004": "keep",
    "p005": "expand",
    "p006": "compress",
    "p007": "illustrate",
}

# ------------------------------------------------------- 素材 A：自带事实锚的假材料 --
#: 三类事实锚（数字 / 引文 / 书名）**都写在第 5 段**，展开时只能照抄它们
#: （事实锚按段比对，expand 段补出原文别处才有的事实算「多出」，这一点由用例固定）
FAKE_NUMBERS = ("1859", "1696", "27")
FAKE_QUOTE = "庞大的商品堆积"
FAKE_BOOK = "政治经济学批判"

FAKE_SOURCE_BLOCKS = [
    "标题块：这一节要拆开商品。",
    "把这一段讲清楚就够了。",
    "这一段也没有需要展开的东西。",
    "说过一遍就不再重复。",
    "马克思在 1859 年写的《政治经济学批判》里，开头就是“庞大的商品堆积”；"
    "另一处年份 1696 年，页码 27。这一段要展开：为什么先从商品入手。",
    "让模型越界给 compress。",
    "让模型越界给 illustrate。",
]

#: p005 的改写文本：更长，但三类锚点逐字照抄、**出现顺序也与原文一致**
#: （事实锚按段比对，且顺序也算不一致——见 ``ar.fidelity.same_facts``）
FAKE_DOC_TEXT = (
    "马克思在 " + FAKE_NUMBERS[0] + " 年就说过这件事，他写的是《" + FAKE_BOOK + "》，开头一句就是“"
    + FAKE_QUOTE + "”。另一处可以对照的是 " + FAKE_NUMBERS[1] + " 年，见第 " + FAKE_NUMBERS[2]
    + " 页。这一段要展开的是：为什么先从商品入手。"
)

# ------------------------------------------------------- 素材 B：conftest 的通用样例 --
#: ``SAMPLE_DOC`` 六段都是「第 N 段。」，改写稿也就只能照抄原句（不添数字 / 引文 / 书名）
SAMPLE_FAKE_TEXT = "第二段（展开）：这里补一句解释，但不能凭空补出原文没有的数字、引文或书名。"


# --------------------------------------------------------------------- 素材 --
def build_doc(markdown: str, source_id: str = "fake-doc") -> dict:
    """按摄取规则造一份 SourceDoc（不落盘，纯内存）。"""
    from ar.ingest import paragraph_id, text_hash

    blocks = [b.strip() for b in re.split(r"\n[ \t\r]*\n", markdown) if b.strip()]
    return {
        "source_id": source_id,
        "ingest": {"kind": "local_file", "path": f"(内存样材料 {source_id})", "hash": f"sha256:{source_id}", "at": ""},
        "paragraphs": [
            {"id": paragraph_id(i), "index": i, "text": text, "text_hash": text_hash(text)}
            for i, text in enumerate(blocks, start=1)
        ],
    }


def ingest_text(dirs: SimpleNamespace, markdown: str, source_id: str = "fake-doc") -> dict:
    """把一段文本写成 ``data/<id>/`` 的**一致**产物（source.md + source.meta.json）。

    ``ar check`` 会先校验锚点，所以 ``source.md`` 与元数据必须对得上——
    测试要造「内容坏了」的现场时，不能顺手把锚点也弄坏。
    """
    import hashlib

    doc = build_doc(markdown, source_id)
    payload = markdown.encode("utf-8")
    doc["ingest"] = {
        "kind": "local_file",
        "path": f"(测试内存样材料 {source_id})",
        "hash": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "at": "2026-10-05T00:00:00+08:00",
    }
    target = dirs.data / source_id
    target.mkdir(parents=True, exist_ok=True)
    (target / "source.md").write_bytes(payload)
    (target / "source.meta.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return doc


#: 假材料的段落表
FAKE_SOURCE = "\n\n".join(FAKE_SOURCE_BLOCKS)


def fake_handler(*, expand_ids: tuple[str, ...] = ("p005",), expanded_text: str = FAKE_DOC_TEXT):
    """按提示词里的段 id 造一批 JSON 决策（无视调用顺序，因为 ``run_rewrite`` 是并发的）。"""

    def handler(messages: list[dict[str, str]]) -> str:
        prompt = messages[-1]["content"]
        ids = PROMPT_PARAGRAPH_RE.findall(prompt)
        decisions = []
        for pid in ids:
            if pid in expand_ids:
                action = "expand"
            else:
                action = FAKE_ACTIONS.get(pid, "keep")
            decisions.append(
                {
                    "paragraph": pid,
                    "action": action,
                    "why": {"profile": ["c01:unknown"], "note": f"{pid} 的假依据"},
                    "confidence": 0.8,
                    "rewrite": expanded_text if action == "expand" else "",
                }
            )
        return json.dumps({"decisions": decisions}, ensure_ascii=False)

    return handler


def fake_handler_for(doc: dict, *, expand_ids: tuple[str, ...] = ("p005",)) -> object:
    """按某份 SourceDoc 造决策（保留这个入口，方便按材料种类选展开段）。"""
    return fake_handler(expand_ids=expand_ids)


def write_fake_doc(dirs: SimpleNamespace, *, with_concepts: bool = True, profile_hint: int = 4) -> dict:
    """把假材料写进 ``data/fake-doc/``（含可选的概念清单），返回 SourceDoc。"""
    doc = ingest_text(dirs, FAKE_SOURCE)
    target = dirs.data / "fake-doc"
    if with_concepts:
        (target / "concepts.json").write_text(
            json.dumps(
                {
                    "concepts": [
                        {
                            "id": "c01",
                            "name": "假概念",
                            "surface": ["假"],
                            "prereq": [],
                            "difficulty_hint": profile_hint,
                            "paragraphs": ["p002", "p005"],
                            "domain": "测试",
                            "concept_key": "fake",
                        }
                    ],
                    "illustration_candidates": [],
                    "meta": {"source_id": "fake-doc", "model": "fake-model", "prompt_version": "concepts-v1"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return doc


def fake_run(dirs: SimpleNamespace, client) -> object:
    """在 ``data/fake-doc`` 上跑一次完整 rewrite。"""
    return run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", client)


@pytest.fixture
def sample_fake_handler():
    """通用样例（SAMPLE_DOC）用的假模型：把 p002 展开成一段不含任何事实锚的正文。"""

    def handler(messages: list[dict[str, str]]) -> str:
        ids = PROMPT_PARAGRAPH_RE.findall(messages[-1]["content"])
        decisions = [
            {
                "paragraph": pid,
                "action": "expand" if pid == "p002" else "keep",
                "why": {"profile": [], "note": f"{pid} 的假依据"},
                "confidence": 0.75,
                "rewrite": SAMPLE_FAKE_TEXT if pid == "p002" else "",
            }
            for pid in ids
        ]
        return json.dumps({"decisions": decisions}, ensure_ascii=False)

    return handler


@pytest.fixture
def rewrite_env(tmp_path: Path, dirs: SimpleNamespace, sample_md: Path, monkeypatch):
    """一份真摄取过的材料 + 临时 config，供 CLI 用例（把模型换成假客户端）使用。"""
    ingest(sample_md, "sample", dirs.data)
    target = dirs.data / "sample"
    (target / "concepts.json").write_text(
        json.dumps(
            {
                "concepts": [
                    {
                        "id": "c01",
                        "name": "假概念",
                        "surface": ["假"],
                        "prereq": [],
                        "difficulty_hint": 4,
                        "paragraphs": ["p002", "p003", "p005"],
                        "domain": "测试",
                        "concept_key": "fake",
                    }
                ],
                "illustration_candidates": [],
                "meta": {"source_id": "sample", "model": "fake-model", "prompt_version": "concepts-v1"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.local.json"
    # api_key 填的是假值：真正的客户端被 test 换成假客户端，这里只是让「模型已配置」通过门禁
    config_path.write_text(
        json.dumps({"model": "fake-config-model", "max_retries": 0, "api_key": "fake-key-for-test"}), encoding="utf-8"
    )
    monkeypatch.delenv("AR_API_KEY", raising=False)
    return SimpleNamespace(dirs=dirs, target=target, config=config_path)


def run_cli_with_fake(run_cli, env: SimpleNamespace, client, *argv: str) -> tuple[int, str]:
    """把 CLI 的模型客户端换成假客户端后跑一次（不碰真实网络）。"""
    from ar import cli

    cli._make_client = lambda config: client  # type: ignore[assignment]
    return run_cli("--data-dir", str(env.dirs.data), "--config", str(env.config), *argv)


# ------------------------------------------------------- planDecisions / rewrite --
def test_plan_decisions_covers_every_paragraph(fake_chat_client) -> None:
    doc = build_doc(FAKE_SOURCE)
    client = fake_chat_client(handler=fake_handler_for(doc))
    decisions, degraded, meta = planDecisions(doc, None, {"reader": "tester"}, client, source_id="fake-doc")

    assert [d.paragraph for d in decisions] == [p["id"] for p in doc["paragraphs"]]
    for decision in decisions:
        assert decision.action in ("expand", "keep"), decision
        assert isinstance(decision.why.get("note"), str) and decision.why["note"]
        assert 0.0 <= decision.confidence <= 1.0
    assert meta["model"] == "fake-model"
    # rewrite-v2（2026-10-08）：段号与本批正文挪到 user 末尾（前缀缓存），提示词变了 → 版本号跟着升
    assert meta["prompt_version"] == "rewrite-v2"
    assert meta["request_count"] >= 1
    # 模型给的 compress / illustrate 一律落成 keep + deferred，并在 degraded 里点名
    deferred = [d.paragraph for d in decisions if d.deferred]
    assert deferred == ["p006", "p007"]
    assert all("deferred" not in d.to_record() for d in decisions), "deferred 记在 changes.json，不污染 decisions schema"
    assert any("p006" in item for item in degraded)
    assert any("p007" in item for item in degraded)


def test_plan_decisions_merges_paragraphs_into_few_requests(fake_chat_client) -> None:
    """25 段式的材料不能一段一次请求：字符数够小的时候要合并成一批。"""
    doc = build_doc(FAKE_SOURCE)
    client = fake_chat_client(handler=fake_handler_for(doc))
    _decisions, _degraded, meta = planDecisions(
        doc, None, {"reader": "tester"}, client, source_id="fake-doc", max_chars_per_batch=10000
    )
    assert meta["request_count"] == 1, "合并请求失效（每段都单独发了一次？）"
    assert len(client.calls) == 1


def test_plan_decisions_fills_missing_with_visible_degradation(fake_chat_client) -> None:
    doc = build_doc(FAKE_SOURCE)

    def handler(messages: list[dict[str, str]]) -> str:
        ids = PROMPT_PARAGRAPH_RE.findall(messages[-1]["content"])
        return json.dumps(
            {
                "decisions": [
                    {"paragraph": ids[0], "action": "keep", "why": {"profile": [], "note": "只回第一段"}, "confidence": 0.9}
                ]
            },
            ensure_ascii=False,
        )

    client = fake_chat_client(handler=handler)
    decisions, degraded, _meta = planDecisions(doc, None, {"reader": "tester"}, client, source_id="fake-doc")
    assert len(decisions) == len(doc["paragraphs"])
    assert [d.action for d in decisions[1:]] == ["keep"] * (len(decisions) - 1)
    assert all(d.confidence == 0.0 for d in decisions[1:])
    assert sum("没有给出这段的决策" in item for item in degraded) == len(decisions) - 1


def test_plan_decisions_model_failure_degrades_visibly(fake_chat_client) -> None:
    def handler(messages):  # noqa: ANN001
        raise RuntimeError("网关不可用")

    client = fake_chat_client(handler=handler)
    decisions, degraded, _meta = planDecisions(doc := build_doc(FAKE_SOURCE), None, {}, client, source_id="fake-doc")
    assert len(decisions) == len(doc["paragraphs"])
    assert all(d.action == "keep" for d in decisions)
    assert any("模型调用失败" in item for item in degraded)


def test_rewrite_keeps_paragraph_order_and_marks_deferred(fake_chat_client) -> None:
    doc = build_doc(FAKE_SOURCE)
    client = fake_chat_client(handler=fake_handler_for(doc))
    decisions, _degraded, _meta = planDecisions(doc, None, {"reader": "tester"}, client, source_id="fake-doc")

    doc_out, changes = rewrite(doc, decisions, {"reader": "tester"})
    blocks = split_rewritten_blocks(doc_out["markdown"])
    assert len(blocks) == len(doc["paragraphs"]), "段数与源不一致"

    by_id = {change["paragraph"]: change for change in changes}
    assert [change["paragraph"] for change in changes] == [p["id"] for p in doc["paragraphs"]]
    # keep 段原样、expand 段换成改写文本
    assert blocks[0] == doc["paragraphs"][0]["text"]
    assert blocks[4] == FAKE_DOC_TEXT
    assert len(FAKE_DOC_TEXT) > len(doc["paragraphs"][4]["text"]) - 10, "展开段没变长，expand 就成了空操作"
    # 越界动作：动作落成 keep、原文照旧、标 deferred
    assert by_id["p006"]["action"] == "keep" and by_id["p006"]["deferred"] is True
    assert by_id["p007"]["deferred"] is True
    assert blocks[5] == doc["paragraphs"][5]["text"]
    # facts 只含 numbers / quotes（与前端夹具同口径），且三类事实锚都抽到了
    assert set(by_id["p005"]["facts"]) == {"numbers", "quotes"}
    assert set(by_id["p005"]["facts"]["numbers"]) == set(FAKE_NUMBERS)
    assert FAKE_QUOTE in by_id["p005"]["facts"]["quotes"]
    for change in changes:
        assert set(change) <= {"paragraph", "action", "why", "confidence", "facts", "deferred"}


def test_rewrite_translates_implemented_actions_only() -> None:
    assert set(IMPLEMENTED_ACTIONS) == {"expand", "keep"}
    assert DEFAULT_MODE == "free"


def test_plan_decisions_flags_suspicious_expand(fake_chat_client) -> None:
    """「展开」却没变长 / 没以句末标点收尾 → 记进 degraded（疑似截断，别静默接受）。"""
    doc = build_doc(FAKE_SOURCE)
    client = fake_chat_client(
        handler=fake_handler(expand_ids=("p005", "p006"), expanded_text="太短")
    )
    decisions, degraded, _meta = planDecisions(doc, None, {"reader": "tester"}, client, source_id="fake-doc")
    assert len(decisions) == len(doc["paragraphs"])
    assert any("p005" in item and "不比原文" in item for item in degraded), degraded
    assert any("p006" in item and "不比原文" in item for item in degraded), degraded


def test_plan_decisions_flags_truncated_expand(fake_chat_client) -> None:
    """变长了但没以句末标点收尾（截断）→ 也要点名。"""
    doc = build_doc(FAKE_SOURCE)
    long_without_stop = doc["paragraphs"][4]["text"] + "这里补一句解释，但话说到一半就断了"
    client = fake_chat_client(handler=fake_handler(expanded_text=long_without_stop))
    _decisions, degraded, _meta = planDecisions(doc, None, {"reader": "tester"}, client, source_id="fake-doc")
    assert any("p005" in item and "截断" in item for item in degraded), degraded


# ------------------------------------------------- 提示词前缀缓存（rewrite-v2） --
#: 固定块（画像 / 概念清单）在真实材料上的尺寸量级：capital-v1-ch01 实测 73 + 647 字符
REALISTIC_PROFILE_BLOCK = "读者 local-default 自报概念状态：" + "；".join(f"c{i:02d}=unknown" for i in range(1, 7))
REALISTIC_CONCEPT_BLOCK = "概" * 647


def _rewrite_user_contents(
    paragraphs: list[dict[str, Any]], chunk: list[dict[str, Any]], start: int, end: int, **blocks: str
) -> tuple[str, str]:
    """取两个批次的 (system, user) 正文，供前缀比较。"""
    messages = build_rewrite_messages(
        "fake-doc", chunk, start=start, end=end, total=len(paragraphs), **blocks  # type: ignore[arg-type]
    )
    return messages[0]["content"], messages[1]["content"]


def test_rewrite_template_puts_every_varying_part_at_the_end_of_the_user_message() -> None:
    """段号 + 本批正文必须在 user **末尾**：批间逐字相同的前缀才够长（前缀缓存的前提）。

    这条守住 rewrite-v2 的重排：一旦有人把正文挪回固定块中间，共享前缀会从 ~1800 字符
    掉回 ~770 字符，缓存命中率立刻塌掉。
    """
    doc = build_doc(FAKE_SOURCE)
    paragraphs = doc["paragraphs"]
    blocks = {"profile_block": "读者 tester 自报概念状态：c01=unknown", "concept_block": "c01 假概念（难度 3）"}
    system_a, user_a = _rewrite_user_contents(paragraphs, paragraphs[:3], 1, 3, **blocks)
    system_b, user_b = _rewrite_user_contents(paragraphs, paragraphs[3:], 4, len(paragraphs), **blocks)

    # ① 会变的部分在末尾：正文块收尾，段号落在 JSON 示例与规则之后
    assert user_a.rstrip().endswith(f"[p003] {paragraphs[2]['text']}")
    assert user_a.index("本次给你第 1–3 段") > user_a.index("只输出 JSON")
    assert user_a.index("本次给你第 1–3 段") > user_a.index("7. 不要写")
    assert user_b.index("本次给你第 4–7 段") > user_b.index("只输出 JSON")

    # ② 两个批次的**首个差异点必须正好落在段号那一位**上：此前全部逐字相同
    first_diff = next(index for index, (left, right) in enumerate(zip(user_a, user_b)) if left != right)
    assert user_a[first_diff] == "1" and user_b[first_diff] == "4"
    assert user_a[first_diff - len("本次给你第 "):first_diff] == "本次给你第 "
    header_at = user_a.index("原文段落（每段以 [段 id] 开头")
    assert first_diff > header_at, "连段号前的固定标题行都该落在共享前缀里"
    assert first_diff >= 1000, f"共享前缀只有 {first_diff} 字符：变化的内容又被挪到前面了"
    assert system_a == system_b, "system 提示词必须逐字相同"

    # ③ 真实材料尺寸的固定块下，共享前缀量级（capital-v1-ch01 实测 1805 字符）
    real_blocks = {"profile_block": REALISTIC_PROFILE_BLOCK, "concept_block": REALISTIC_CONCEPT_BLOCK}
    real_a = _rewrite_user_contents(paragraphs, paragraphs[:3], 1, 3, **real_blocks)[1]
    real_b = _rewrite_user_contents(paragraphs, paragraphs[3:], 4, len(paragraphs), **real_blocks)[1]
    realistic_prefix = next(
        (index for index, (left, right) in enumerate(zip(real_a, real_b)) if left != right),
        min(len(real_a), len(real_b)),
    )
    assert realistic_prefix >= 1700, f"实际材料尺寸下共享前缀只有 {realistic_prefix} 字符（目标 ~1805）"

    # ④ 固定块里不许有时间戳 / 随机量（否则每轮都建新缓存）。规则与措辞本身不许被这次重排改动。
    assert "generated_at" not in real_a
    assert not re.search(r"\d{4}-\d{2}-\d{2}T", real_a)
    assert "compress / illustrate 本期不实现，不要输出。" in real_a
    assert "7. 不要写「改写说明」「导读」之类的额外段落；只输出 JSON。" in real_a


@dataclass
class _RecordingReply:
    """替身回复：``ar.rewrite`` 只读 text / model / usage 三个字段。"""

    text: str
    model: str = "fake-model"
    usage: dict[str, Any] | None = None


@dataclass
class _RecordingClient:
    """记录每次调用的「开始 / 结束」与并发峰值，用来验证预热是**串行**的。"""

    handler: Any
    delay: float = 0.02
    events: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    max_active: int = 0
    _active: int = 0
    _lock: Any = field(default_factory=threading.Lock)

    def chat(self, messages: Any, **kwargs: Any) -> _RecordingReply:
        ids = tuple(PROMPT_PARAGRAPH_RE.findall(messages[-1]["content"]))
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            self.events.append(("start", ids))
        try:
            time.sleep(self.delay)
            return _RecordingReply(text=self.handler(list(ids)))
        finally:
            with self._lock:
                self.events.append(("end", ids))
                self._active -= 1


def _keep_decisions(ids: list[str], *, note: str = "假依据") -> str:
    return json.dumps(
        {
            "decisions": [
                {"paragraph": pid, "action": "keep", "why": {"profile": [], "note": note}, "confidence": 0.8}
                for pid in ids
            ]
        },
        ensure_ascii=False,
    )


def test_first_batch_warms_the_prefix_serially_before_the_rest_go_parallel() -> None:
    """先串行跑第 1 批（建前缀缓存），再并发跑其余批次（并发上限仍是 4）。"""
    doc = build_doc(FAKE_SOURCE)
    client = _RecordingClient(handler=lambda ids: _keep_decisions(ids))
    decisions, _degraded, meta = planDecisions(
        doc, None, {"reader": "tester"}, client, source_id="fake-doc", max_chars_per_batch=1
    )

    assert meta["request_count"] == len(doc["paragraphs"]) == 7, "每段一批，才看得出顺序与并发"
    starts = [event for event in client.events if event[0] == "start"]
    assert "p001" in starts[0][1], "第 1 批（含 p001）必须最先发——预热批按段序取第一批"

    first_end = client.events.index(("end", starts[0][1]))
    later_start = min(index for index, event in enumerate(client.events) if event[0] == "start" and index > 0)
    assert first_end < later_start, "第 1 批必须**跑完**再发其余批次（否则 6 批同时首发，谁都吃不到新缓存）"
    assert 2 <= client.max_active <= 4, f"其余批次应当并发（≤4），实测峰值 {client.max_active}"
    assert [decision.paragraph for decision in decisions] == [p["id"] for p in doc["paragraphs"]]


def test_warmup_batch_failure_degrades_visibly_without_blocking_the_rest() -> None:
    """预热批失败按既有降级路径处理：只影响它自己那几段，其余批次照跑。"""
    doc = build_doc(FAKE_SOURCE)

    def handler(ids: list[str]) -> str:
        if "p001" in ids:
            raise RuntimeError("网关不可用")
        return _keep_decisions(ids)

    client = _RecordingClient(handler=handler, delay=0.0)
    decisions, degraded, meta = planDecisions(
        doc, None, {"reader": "tester"}, client, source_id="fake-doc", max_chars_per_batch=1
    )

    assert meta["model_errors"] and meta["model_errors"][0]["batch"] == "第 1–1 段"
    assert meta["model_errors"][0]["paragraphs"] == ["p001"]
    assert meta["rewrite_incomplete"] is True
    assert any("模型调用失败" in item for item in degraded)
    # 只有第 1 批那 1 段被迫 keep（confidence 0），其余 6 段是模型给的决策
    assert [d.confidence for d in decisions] == [0.0, 0.8, 0.8, 0.8, 0.8, 0.8, 0.8]


def test_batch_usage_records_cached_tokens_per_request_and_sums_them() -> None:
    """逐批缓存命中量：嵌套字段与扁平字段都认；没上报的批次不写这个键。"""
    doc = build_doc(FAKE_SOURCE)
    usages: dict[str, dict[str, Any] | None] = {
        "p001": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                 "prompt_tokens_details": {"cached_tokens": 0}},
        "p002": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                 "prompt_tokens_details": {"cached_tokens": 640}},
        "p003": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                 "prompt_cache_hit_tokens": 512},
        # p004 起：网关没回 usage（既没有 prompt_tokens 也没有 cached_tokens）
    }

    class _UsageClient:
        def chat(self, messages: Any, **kwargs: Any) -> _RecordingReply:
            ids = PROMPT_PARAGRAPH_RE.findall(messages[-1]["content"])
            return _RecordingReply(text=_keep_decisions(ids), usage=usages.get(ids[0]))

    _decisions, _degraded, meta = planDecisions(
        doc, None, {"reader": "tester"}, _UsageClient(), source_id="fake-doc", max_chars_per_batch=1
    )

    usage = meta["usage"]
    assert usage["requests"] == 7
    assert usage["prompt_tokens"] == 300 and usage["completion_tokens"] == 30 and usage["total_tokens"] == 330
    assert usage["cached_tokens"] == 0 + 640 + 512, "嵌套 + 扁平两种字段都要累加"

    by_batch = {item["batch"]: item for item in meta["batch_usage"]}
    assert by_batch["第 1–1 段"]["cached_tokens"] == 0
    assert by_batch["第 2–2 段"]["cached_tokens"] == 640
    assert by_batch["第 3–3 段"]["cached_tokens"] == 512
    assert "cached_tokens" not in by_batch["第 4–4 段"], "没上报就不写这个键（0 与没上报分得开）"


def test_usage_cached_tokens_is_null_when_gateway_reports_nothing(fake_chat_client) -> None:
    """网关一个字段都没回时：``cached_tokens`` 是 null，不是编造出来的 0。"""
    doc = build_doc(FAKE_SOURCE)
    client = fake_chat_client(handler=fake_handler_for(doc))
    _decisions, _degraded, meta = planDecisions(doc, None, {"reader": "tester"}, client, source_id="fake-doc")
    assert meta["usage"]["cached_tokens"] is None


def test_run_rewrite_writes_cached_tokens_into_both_artifacts(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """产物里能直接看到缓存命中量：decisions.json 与 changes.json 的 meta.usage.cached_tokens。"""
    from ar.jsonio import read_json

    doc = write_fake_doc(dirs)
    client = fake_chat_client(
        handler=fake_handler_for(doc),
        usage={
            "prompt_tokens": 50,
            "completion_tokens": 5,
            "total_tokens": 55,
            "prompt_tokens_details": {"cached_tokens": 40},
        },
    )
    outcome = fake_run(dirs, client)

    decisions_payload = read_json(outcome.decisions_path)
    changes_payload = read_json(outcome.changes_path)
    for payload in (decisions_payload, changes_payload):
        assert payload["meta"]["usage"]["cached_tokens"] == 40
        assert payload["meta"]["usage"]["prompt_tokens"] == 50
        assert payload["meta"]["batch_usage"][0]["cached_tokens"] == 40
    # 命令行上也要看得见（人读的一行账）
    assert "前缀缓存命中 40" in outcome.usage_note


# ------------------------------------------------------------------ run_rewrite --
def test_run_rewrite_writes_three_artifacts(dirs: SimpleNamespace, fake_chat_client) -> None:
    from ar.jsonio import read_json

    doc = write_fake_doc(dirs)
    client = fake_chat_client(handler=fake_handler_for(doc))
    outcome = fake_run(dirs, client)

    assert outcome.decisions_path and outcome.decisions_path.is_file()
    assert outcome.rewritten_path and outcome.rewritten_path.is_file()
    assert outcome.changes_path and outcome.changes_path.is_file()

    decisions_payload = read_json(outcome.decisions_path)
    assert len(decisions_payload["decisions"]) == len(doc["paragraphs"])
    assert decisions_payload["meta"]["profile_used"] is False
    assert "无画像" in decisions_payload["meta"]["profile_note"]
    assert decisions_payload["meta"]["degraded"], "空画像降级必须写在产物里（不静默）"
    # 空画像时 why.profile 写的是「难度 N（无画像）」，不假装有画像
    assert any(
        ref.endswith("（无画像）") for d in decisions_payload["decisions"] for ref in d["why"]["profile"]
    )
    assert decisions_payload["meta"]["model"] == "fake-model"
    # rewrite-v2（2026-10-08）：同上，提示词重排后版本号升到 v2
    assert decisions_payload["meta"]["prompt_version"] == "rewrite-v2"

    changes_payload = read_json(outcome.changes_path)
    # rewrite 阶段事实锚比对**还没跑**：checked 必须是 false、counts 留空（由 `ar check` 写入）
    assert changes_payload["fidelity"]["checked"] is False
    assert changes_payload["fidelity"]["counts"] is None
    assert changes_payload["fidelity"]["mismatches"] == []
    assert "ar check" in changes_payload["fidelity"]["summary"]
    assert changes_payload["fidelity"]["paragraphs_match_source"] is True
    assert changes_payload["fidelity"]["keep_paragraphs_mismatch"] == []
    assert changes_payload["fidelity"]["count_mismatch"] == []
    # 空画像时每一段都能看出「依据从哪来」：有概念就写「难度 N（无画像）」，没概念就在 note 里写明
    for change in changes_payload["changes"]:
        tagged = any(ref.endswith("（无画像）") for ref in change["why"]["profile"])
        assert tagged or "无画像" in change["why"]["note"], change["paragraph"]
    # 「保持」段与原文逐字节一致，「展开」段真的更长
    blocks_now = split_rewritten_blocks((dirs.data / "fake-doc" / "rewritten.md").read_text(encoding="utf-8"))
    for index, paragraph in enumerate(doc["paragraphs"]):
        if paragraph["id"] == "p005":
            assert len(blocks_now[index]) > len(paragraph["text"])
        else:
            assert blocks_now[index] == paragraph["text"], paragraph["id"]
    assert changes_payload["meta"]["profile_used"] is False


def test_run_rewrite_uses_profile_overrides(dirs: SimpleNamespace, fake_chat_client) -> None:
    from ar.jsonio import read_json
    from ar.profile import save_profile

    doc = write_fake_doc(dirs)
    save_profile(
        dirs.profiles,
        "local-default",
        {
            "reader": "local-default",
            "concepts": {"c01": {"state": "known", "confidence": 0.9}},
            "preferences": [],
            "overrides": [{"concept": "c01", "state": "known", "at": "2026-10-05T00:00:00+08:00", "note": "别再解释了"}],
        },
    )
    client = fake_chat_client(handler=fake_handler_for(doc))
    outcome = run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", client)
    payload = read_json(outcome.decisions_path)
    assert payload["meta"]["profile_used"] is True
    assert "1 条概念自评" in payload["meta"]["profile_note"]
    assert "手动推翻" in payload["meta"]["profile_note"]
    # overrides 要真的喂给模型（优先级最高，不得被覆盖）
    prompt = client.calls[0][-1]["content"]
    assert "c01=known" in prompt and "别再解释了" in prompt


def test_run_rewrite_strict_mode_refuses_without_writing(dirs: SimpleNamespace, fake_chat_client) -> None:
    write_fake_doc(dirs, with_concepts=False)
    client = fake_chat_client(handler=fake_handler())

    with pytest.raises(Exception) as excinfo:
        run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", client, mode="strict")
    assert "严格档" in str(excinfo.value)
    assert STRICT_NOT_IMPLEMENTED in str(excinfo.value)
    assert client.calls == [], "strict 未实现时不许发请求"
    assert not (dirs.data / "fake-doc" / "rewritten.md").exists()
    assert not (dirs.data / "fake-doc" / "decisions.json").exists()


# -------------------------------------------------------------------- 事实锚 --
def test_fact_extractors() -> None:
    text = "见《资本论》第 1,859 页与《价值》２８页：“引文甲”和“引文乙”，还有十三。"
    assert extract_numbers(text) == ["1,859", "28"]
    assert extract_books(text) == ["资本论", "价值"]
    assert extract_quotes(text) == ["引文甲", "引文乙"]
    assert extract_quotes("没有成对引号“落单") == []
    assert extract_numbers("全角３．１４与半角3.14") == ["3.14", "3.14"]


def test_compare_blocks_reports_paragraph_missing() -> None:
    doc = build_doc("第一段。\n\n第二段。\n\n第三段。")
    outcome_blocks = split_rewritten_blocks("第一段。\n\n第二段。")
    from ar.fidelity import compare_blocks

    problems, mismatches, counts = compare_blocks(doc["paragraphs"], outcome_blocks)
    assert counts["paragraphs"] == 3
    assert any("p003" in problem for problem in problems)
    assert any(item["kind"] == "paragraph_missing" and item["paragraph"] == "p003" for item in mismatches)


def _copy_doc_dir(source: Path, destination: Path) -> Path:
    """把 ``data/`` 整个复制成**临时副本**，返回副本里的 ``<id>/`` 目录。

    负向对照只在副本上注入缺陷——交付产物本身一个字节都不动
    （``学习/_整理/selfcheck.py`` 的教训：早期直接在交付文件上改再还原，一次还原写错就留在盘上）。
    """
    copied = shutil.copytree(source, destination)
    return copied


def test_check_fidelity_passes_on_untouched_rewrite(dirs: SimpleNamespace, fake_chat_client) -> None:
    doc = write_fake_doc(dirs)
    fake_run(dirs, fake_chat_client(handler=fake_handler_for(doc)))

    outcome = check_fidelity("fake-doc", dirs.data)
    assert outcome.mismatches == []
    assert fidelity_problems(outcome) == []
    assert outcome.counts["paragraphs"] == len(doc["paragraphs"])
    assert outcome.counts["numbers"] == 3
    assert outcome.counts["books"] == 1
    assert outcome.counts["quotes"] == 1


def test_check_fidelity_catches_number_tampering_in_copy(dirs: SimpleNamespace, fake_chat_client) -> None:
    """负向对照：只在**临时副本**上改数字，断言注入生效 + check 报错并点名段 id。"""
    doc = write_fake_doc(dirs)
    fake_run(dirs, fake_chat_client(handler=fake_handler_for(doc)))

    copy = _copy_doc_dir(dirs.data, dirs.tmp / "copy")
    rewritten = copy / "fake-doc" / "rewritten.md"
    original = rewritten.read_text(encoding="utf-8")
    mutated = original.replace(FAKE_NUMBERS[0] + " 年", "1858 年")
    assert mutated != original, "注入未生效（没找到要改的数字）"
    rewritten.write_text(mutated, encoding="utf-8")

    outcome = check_fidelity("fake-doc", copy)
    problems = fidelity_problems(outcome)
    assert problems, "事实锚校验失灵：改了数字却没报错"
    assert any("p005" in problem for problem in problems), problems
    assert any("1859" in problem for problem in problems), problems
    # 失败也要留痕：fidelity.mismatches 写回副本的 changes.json
    payload = json.loads((copy / "fake-doc" / "changes.json").read_text(encoding="utf-8"))
    assert payload["fidelity"]["mismatches"], "校验失败没有写进 changes.json"


def test_check_fidelity_catches_quote_and_book_and_missing_paragraph(dirs: SimpleNamespace, fake_chat_client) -> None:
    doc = write_fake_doc(dirs)
    fake_run(dirs, fake_chat_client(handler=fake_handler_for(doc)))

    # 1) 改引文
    copy = _copy_doc_dir(dirs.data, dirs.tmp / "copy-quote")
    text = (copy / "fake-doc" / "rewritten.md").read_text(encoding="utf-8")
    mutated = text.replace("“" + FAKE_QUOTE + "”", "“庞大的商品积累”")
    assert mutated != text, "注入未生效（引文）"
    (copy / "fake-doc" / "rewritten.md").write_text(mutated, encoding="utf-8")
    problems = fidelity_problems(check_fidelity("fake-doc", copy))
    assert any("p005" in problem and "引号引文" in problem for problem in problems), problems

    # 2) 改书名
    copy = _copy_doc_dir(dirs.data, dirs.tmp / "copy-book")
    text = (copy / "fake-doc" / "rewritten.md").read_text(encoding="utf-8")
    mutated = text.replace("《" + FAKE_BOOK + "》", "《政治经济学批判大纲》")
    assert mutated != text, "注入未生效（书名）"
    (copy / "fake-doc" / "rewritten.md").write_text(mutated, encoding="utf-8")
    problems = fidelity_problems(check_fidelity("fake-doc", copy))
    assert any("p005" in problem and "书名号" in problem for problem in problems), problems

    # 3) 删掉最后一段（p007 被删 → 段数少一段、p007 缺失）
    copy = _copy_doc_dir(dirs.data, dirs.tmp / "copy-missing")
    blocks = split_rewritten_blocks((copy / "fake-doc" / "rewritten.md").read_text(encoding="utf-8"))
    assert blocks[6] == doc["paragraphs"][6]["text"], "第 7 块应当还是 p007 的原文"
    (copy / "fake-doc" / "rewritten.md").write_text("\n\n".join(blocks[:6]) + "\n", encoding="utf-8")
    problems = fidelity_problems(check_fidelity("fake-doc", copy))
    assert any("p007" in problem for problem in problems), problems
    # 4) 多加一段也要检出
    copy = _copy_doc_dir(dirs.data, dirs.tmp / "copy-extra")
    text = (copy / "fake-doc" / "rewritten.md").read_text(encoding="utf-8")
    (copy / "fake-doc" / "rewritten.md").write_text(text + "\n\n凭空多出来的一段。\n", encoding="utf-8")
    problems = fidelity_problems(check_fidelity("fake-doc", copy))
    assert any("多出来" in problem or "多出" in problem for problem in problems), problems


# ------------------------------------------------------------------------ CLI --
def test_cli_rewrite_writes_artifacts_and_check_passes(
    run_cli, rewrite_env: SimpleNamespace, fake_chat_client, sample_fake_handler
) -> None:
    client = fake_chat_client(handler=sample_fake_handler)

    code, output = run_cli_with_fake(run_cli, rewrite_env, client, "rewrite", "sample", "--reader", "local-default")
    assert code == EXIT_OK, output
    assert "decisions.json" in output and "rewritten.md" in output and "changes.json" in output
    assert "展开" in output and "保持" in output
    assert "无画像" in output
    assert "tokens" in output

    # 产物真的落盘，而且主栏要显示的正文就是改写稿（不是原文）
    target = rewrite_env.target
    decisions_payload = json.loads((target / "decisions.json").read_text(encoding="utf-8"))
    assert len(decisions_payload["decisions"]) == 6
    assert decisions_payload["meta"]["implemented_actions"] == ["expand", "keep"]
    rewritten = (target / "rewritten.md").read_text(encoding="utf-8")
    blocks = split_rewritten_blocks(rewritten)
    assert len(blocks) == 6
    assert blocks[1] == SAMPLE_FAKE_TEXT, "展开段没有被替换"

    code, output = run_cli_with_fake(run_cli, rewrite_env, client, "check", "sample")
    assert code == EXIT_OK, output
    assert "锚点校验通过" in output
    assert "事实锚校验通过" in output
    assert "数字" in output and "引号引文" in output and "书名号" in output
    # check 把「真的比了多少」写回 changes.json（防止检查器空跑）
    fidelity = json.loads((target / "changes.json").read_text(encoding="utf-8"))["fidelity"]
    assert fidelity["checked"] is True and fidelity["mismatches"] == []
    assert fidelity["counts"]["paragraphs"] == 6
    assert fidelity["paragraphs_match_source"] is True


def test_cli_check_negative_number_tampering(
    run_cli, rewrite_env: SimpleNamespace, fake_chat_client, sample_fake_handler
) -> None:
    """负向对照（CLI 面）：改掉改写稿副本里一个数字 → check 退出码 4、点名段 id、还原后重新通过。"""
    # 直接把一份第 2 段带数字的材料写成语料（source.md 与 source.meta.json 保持一致，锚点校验才不会先失败）
    blocks = [
        "标题块：这一节要拆开商品。",
        "马克思写于 1859 年。",
        "第三段。",
        "第四段。",
        "第五段。",
        "第六段。",
    ]
    target = rewrite_env.dirs.data / "sample"
    for child in target.iterdir():
        child.unlink()
    ingest_text(rewrite_env.dirs, "\n\n".join(blocks), "sample")

    def handler(messages: list[dict[str, str]]) -> str:
        ids = PROMPT_PARAGRAPH_RE.findall(messages[-1]["content"])
        return json.dumps(
            {
                "decisions": [
                    {
                        "paragraph": pid,
                        "action": "expand" if pid == "p002" else "keep",
                        "why": {"profile": [], "note": "假依据"},
                        "confidence": 0.7,
                        "rewrite": "马克思写于 1859 年，这一年很关键。" if pid == "p002" else "",
                    }
                    for pid in ids
                ]
            },
            ensure_ascii=False,
        )

    client = fake_chat_client(handler=handler)
    code, output = run_cli_with_fake(run_cli, rewrite_env, client, "rewrite", "sample")
    assert code == EXIT_OK, output

    # 只在临时副本上注入缺陷（交付产物本身不动）
    copy = _copy_doc_dir(rewrite_env.dirs.data, rewrite_env.dirs.tmp / "tampered")
    rewritten = copy / "sample" / "rewritten.md"
    original = rewritten.read_text(encoding="utf-8")
    mutated = original.replace("1859", "1860")
    assert mutated != original, "注入未生效（改写稿里没有 1859 可改）"
    rewritten.write_text(mutated, encoding="utf-8")

    code, output = run_cli_with_fake(run_cli, rewrite_env, client, "check", "sample", "--data-dir", str(copy))
    assert code == EXIT_VALIDATION, output
    assert "事实锚校验失败" in output
    assert "p002" in output, output
    assert "1860" in output or "1859" in output, output

    # 还原副本 → 同一命令必须重新通过（证明刚才的失败是注入造成的，不是检查器常年失灵）
    rewritten.write_text(original, encoding="utf-8")
    code, output = run_cli_with_fake(run_cli, rewrite_env, client, "check", "sample", "--data-dir", str(copy))
    assert code == EXIT_OK, output


def test_cli_rewrite_strict_mode_is_not_implemented(run_cli, rewrite_env: SimpleNamespace, fake_chat_client) -> None:
    client = fake_chat_client(handler=lambda messages: "{}")
    code, output = run_cli_with_fake(run_cli, rewrite_env, client, "rewrite", "sample", "--mode", "strict")
    assert code == EXIT_NOT_IMPLEMENTED, output
    assert "严格档" in output
    assert client.calls == []
    assert not (rewrite_env.target / "rewritten.md").exists()


def test_cli_rewrite_bad_mode_is_usage_error(run_cli, rewrite_env: SimpleNamespace) -> None:
    code, output = run_cli("rewrite", "sample", "--mode", "balanced", "--data-dir", str(rewrite_env.dirs.data))
    assert code == EXIT_USAGE, output


def test_cli_check_without_rewrite_asks_for_it(run_cli, dirs: SimpleNamespace, sample_md: Path) -> None:
    ingest(sample_md, "sample", dirs.data)
    code, output = run_cli("--data-dir", str(dirs.data), "check", "sample")
    assert code == EXIT_USAGE, output
    assert "ar rewrite" in output
