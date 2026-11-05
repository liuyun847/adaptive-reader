"""M3：逐段决策 + 改写 + 事实锚校验（``ar.rewrite`` / ``ar.fidelity`` / CLI 接线）。

覆盖契约 §5 的 M3 验收：

- ``decisions.json`` 每段都有 ``why``、动作只在枚举内；
- ``rewritten.md`` 段数 / 段序与源一致；
- 四个动作都已实现（``expand`` / ``compress`` 换正文，``keep`` / ``illustrate`` 用原文）；
- ``illustrate`` 的图定义只在 ``changes[i].figure`` 里，该段正文与原文逐字节相同；
- ``mode=strict`` 明确报错，**不静默按 free 跑、不写任何产物**；
- 空画像时降级在产物里可见（``meta.profile_used=false`` + ``why.profile`` 写「难度 N（无画像）」）；
- 缺段 / 模型调用失败 → 补 ``keep`` 并记 ``meta.degraded``；
- **负向对照**：往改写稿副本改掉一个数字 / 引文 / 书名 / 删一段 → ``check`` 非零退出并点名段 id。
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import threading
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ar.errors import EXIT_MODEL_PARTIAL, EXIT_NOT_IMPLEMENTED, EXIT_OK, EXIT_USAGE, EXIT_VALIDATION, InputError
from ar.fidelity import extract_books, extract_numbers, extract_quotes, split_rewritten_blocks
from ar.ingest import ingest
from ar.llm import LlmFailure, LlmRequest, LlmResult, RetryPolicy
from ar.prompts import PLAN_PROMPT_VERSION, build_rewrite_messages
from ar.rewrite import (
    ACTIONS,
    DEFAULT_MODE,
    IMPLEMENTED_ACTIONS,
    STRICT_NOT_IMPLEMENTED,
    UNIMPLEMENTED_ACTIONS,
    Decision,
    MergeBase,
    ParagraphUpdate,
    PlanEntry,
    RerunTargets,
    _plan_block,
    _reuse_plan,
    check_fidelity,
    fidelity_problems,
    load_merge_base,
    merge_artifacts,
    planDecisions,
    resolve_rerun_targets,
    rewrite,
    run_rewrite,
)
from ar.survey import EMPTY_BACKGROUND, sketch_fingerprint
from conftest import FakeChatClient, FakeOnceMixin  # noqa: I001 - 本目录的假客户端（与 conftest 同一份口径）

PROMPT_PARAGRAPH_RE = re.compile(r"\[(p\d{3})\] ")


def batch_text(prompt: str) -> str:
    """只取「本次给你第 N–M 段」之后的那一块。

    rewrite-v7 起提示词里还有「原文（全文）」块，那里同样有 [pNNN] 前缀；
    假模型 / 计数用例必须只看**本次要处理的段**，不能拿整段提示词扫 id。
    """
    return prompt.rsplit("本次给你第 ", 1)[-1]


def prompt_ids(prompt: str) -> list[str]:
    """本次这一批的段落 id（从批头之后扫，全文块不算）。"""
    return PROMPT_PARAGRAPH_RE.findall(batch_text(prompt))

#: 假模型给每段的决策：``expand`` / ``compress`` 走改写正文，``illustrate`` 走 ``figure``
FAKE_ACTIONS = {
    "p001": "keep",
    "p002": "keep",
    "p003": "keep",
    "p004": "keep",
    "p005": "expand",
    "p006": "compress",
    "p007": "illustrate",
}

#: p006 的压缩稿：**更短**、以句末标点收尾、且原文里没有任何事实锚（压缩不许丢事实）
FAKE_COMPRESSED = "这一段很啰嗦：同一个意思说了三遍。"
#: p007 的图定义：mermaid 源码里没有数字 / 引文 / 书名（不触发「图里多出锚点」那条降级）
FAKE_FIGURE = {
    "kind": "mermaid",
    "source": "flowchart TD\n  A[第一步] --> B[第二步]",
    "caption": "三步递进",
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
    "这一段很啰嗦：同一个意思来回说了三遍，说三遍，还是同一件事。",
    "让模型给这一段配图：三步递进，纯文字看不出先后。",
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


#: 「用默认的 FAKE_FIGURE」与「模型根本没给 figure」在传参上必须分得开（``None`` = 没给）
_DEFAULT_FIGURE = object()


def fake_handler(
    *,
    expand_ids: tuple[str, ...] = ("p005",),
    expanded_text: str = FAKE_DOC_TEXT,
    compressed_text: str = FAKE_COMPRESSED,
    figure: Any = _DEFAULT_FIGURE,
    actions: dict[str, str] | None = None,
):
    """按提示词里的段 id 造一批 JSON 决策（无视调用顺序，因为 ``run_rewrite`` 是并发的）。

    ``compress`` 段带上更短的压缩稿，``illustrate`` 段带上 ``figure``——两个动作现在都真实现了，
    空 ``rewrite`` 的 compress 会被当作「没压缩」记降级（那是另一条用例的现场）。
    ``figure=None`` 表示模型**没给** figure 字段（另一条失败路径）。
    """
    table = dict(FAKE_ACTIONS if actions is None else actions)
    figure_payload = FAKE_FIGURE if figure is _DEFAULT_FIGURE else figure

    def handler(messages: list[dict[str, str]]) -> str:
        prompt = messages[-1]["content"]
        ids = prompt_ids(prompt)
        decisions = []
        for pid in ids:
            action = "expand" if pid in expand_ids else table.get(pid, "keep")
            entry: dict[str, Any] = {
                "paragraph": pid,
                "action": action,
                "why": {"profile": ["c01:unknown"], "note": f"{pid} 的假依据"},
                "confidence": 0.8,
                "rewrite": expanded_text if action == "expand" else "",
            }
            if action == "compress":
                entry["rewrite"] = compressed_text
            elif action == "illustrate" and figure_payload is not None:
                entry["figure"] = dict(figure_payload) if isinstance(figure_payload, dict) else figure_payload
            decisions.append(entry)
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
        ids = prompt_ids(messages[-1]["content"])
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
    # api_key 填的是假值：真正的客户端被 test 换成假客户端，这里只是让「模型已配置」通过门禁；
    # 重试写成 ``retry`` 段（顶层 ``max_retries`` 已废弃，用它会在每次 load_config 时发废弃告警）
    config_path.write_text(
        json.dumps(
            {
                "model": "fake-config-model",
                "retry": {"maxRetries": 0},
                "api_key": "fake-key-for-test",
            }
        ),
        encoding="utf-8",
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
        assert decision.action in ACTIONS, decision
        assert isinstance(decision.why.get("note"), str) and decision.why["note"]
        assert 0.0 <= decision.confidence <= 1.0
    assert meta["model"] == "fake-model"
    # rewrite-v4（2026-10-13）：规则 2 改成四动作判据、加配图候选块；
    # rewrite-v5（2026-10-14）：加「事实锚口径」固定块与规则 10（逐段申报 fact_declarations）
    # rewrite-v6（2026-10-14 复核修复）：加规则 11（why.profile 保留模型引用的语义），
    # 「读者画像」块的空自评措辞改精确（不再说「画像为空」）
    assert meta["prompt_version"] == "rewrite-v7"
    assert meta["request_count"] >= 1
    # 四个动作都已实现：compress 换成压缩稿、illustrate 拿 figure，**都不再标 deferred**
    by_id = {d.paragraph: d for d in decisions}
    assert by_id["p006"].action == "compress" and by_id["p006"].rewrite == FAKE_COMPRESSED
    assert by_id["p007"].action == "illustrate" and by_id["p007"].figure == FAKE_FIGURE
    assert [d.paragraph for d in decisions if d.deferred] == []
    assert not [item for item in degraded if "未实现" in item], degraded
    assert all("deferred" not in d.to_record() for d in decisions), "deferred 记在 changes.json，不污染 decisions schema"


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
        ids = prompt_ids(messages[-1]["content"])
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
    # 面向用户的文案带尝试次数与末次错误码（这里策略是「不重试」→ 已试 1 次）
    assert any("已试 1 次，末次 UNKNOWN" in item for item in degraded), degraded


def test_batch_with_unparsable_output_is_degraded_not_a_batch_failure(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """模型**答了**但输出不成结构：与迁移前同口径——不算批次失败，但那些段确实没被改写。

    「模型没答上来」（批次调用失败 → ``model_errors``）与「答了但没法用」（解析失败 →
    只记可见降级 + ``unplanned_paragraphs``）是两条不同的路；后者同样以退出码 6 收尾。
    """
    doc = write_fake_doc(dirs)
    client = fake_chat_client(handler=lambda _messages: "模型说了些废话，没有 JSON")
    outcome = fake_run(dirs, client)

    assert outcome.model_errors == [], "解析失败不是「批次调用失败」，不该进 model_errors"
    assert outcome.unplanned_paragraphs == [p["id"] for p in doc["paragraphs"]]
    assert any("无法解析" in item for item in outcome.degraded), outcome.degraded
    assert any("没有给出这段的决策" in item for item in outcome.degraded)
    assert outcome.incomplete is True
    # 这次调用确实发生了（账目在），只是输出没法用
    assert len(outcome.decisions) == len(doc["paragraphs"])


def test_rewrite_keeps_paragraph_order_and_inlines_figures(fake_chat_client) -> None:
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
    # compress 段用压缩稿（确实更短），illustrate 段**正文与原文逐字节相同**（图不进 md）
    assert by_id["p006"]["action"] == "compress"
    assert blocks[5] == FAKE_COMPRESSED
    assert len(FAKE_COMPRESSED) < len(doc["paragraphs"][5]["text"])
    assert by_id["p007"]["action"] == "illustrate"
    assert blocks[6] == doc["paragraphs"][6]["text"]
    assert by_id["p007"]["figure"] == FAKE_FIGURE
    # 四个动作都实现了：产物里**没有** deferred
    assert all("deferred" not in change for change in changes)
    # facts 只含 numbers / quotes（与前端夹具同口径），且三类事实锚都抽到了
    assert set(by_id["p005"]["facts"]) == {"numbers", "quotes"}
    assert set(by_id["p005"]["facts"]["numbers"]) == set(FAKE_NUMBERS)
    assert FAKE_QUOTE in by_id["p005"]["facts"]["quotes"]
    for change in changes:
        assert set(change) <= {
            "paragraph",
            "action",
            "why",
            "confidence",
            "facts",
            "figure",
            "figure_error",
            "deferred",
        }


def test_rewrite_translates_implemented_actions_only() -> None:
    # 四个动作现在都已实现（deferred 机制保留，但不再有对象）
    assert set(IMPLEMENTED_ACTIONS) == {"expand", "compress", "keep", "illustrate"}
    assert set(ACTIONS) == set(IMPLEMENTED_ACTIONS)
    assert UNIMPLEMENTED_ACTIONS == ()
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


def test_rewrite_prompt_is_byte_identical_to_the_direct_builder(fake_chat_client) -> None:
    """提示词**逐字不变**：迁移后经工具层拼的 messages == 迁移前业务侧直接拼的 messages。

    固定块（画像 / 概念清单 / 材料名）由 ``planDecisions`` 现算，所以这里调同一对格式化函数；
    段落表一律用**原始的**段落对象（``index`` / ``text_hash`` 都在）——模板只读 ``id`` / ``text``。
    """
    from ar.prompts import build_rewrite_messages, format_paragraph_block
    from ar.rewrite import _format_concept_block, _format_profile_block

    doc = build_doc(FAKE_SOURCE)
    client = fake_chat_client(handler=fake_handler_for(doc))
    # 合并成一批，一次比对整篇
    planDecisions(doc, None, {"reader": "tester"}, client, source_id="fake-doc", max_chars_per_batch=10000)

    expected = build_rewrite_messages(
        "fake-doc",
        doc["paragraphs"],
        profile_block=_format_profile_block("tester", {"reader": "tester"}, []),
        background_block="",
        concept_block=_format_concept_block("fake-doc", None),
        # rewrite-v7：正文全文进「按材料变」的那一段，planDecisions 会带上它
        full_text_block=format_paragraph_block(doc["paragraphs"]),
        start=1,
        end=len(doc["paragraphs"]),
        total=len(doc["paragraphs"]),
    )
    assert client.calls[0] == expected, "迁移后提示词不再与业务侧直接拼的逐字相同"


# ------------------------------------------------ 提示词前缀缓存（rewrite-v7） --
#: 固定块（画像 / 概念清单）在真实材料上的尺寸量级：capital-v1-ch01 实测 73 + 647 字符
REALISTIC_PROFILE_BLOCK = "读者 local-default 自报概念状态：" + "；".join(f"c{i:02d}=unknown" for i in range(1, 7))
REALISTIC_CONCEPT_BLOCK = "概" * 647
#: 全文块的量级：capital-v1-ch01 实测 5463 字符（这里取同量级）
REALISTIC_FULL_TEXT = "[p001] " + "正" * 900 + "\n\n[p002] " + "文" * 900


def _rewrite_user_contents(
    paragraphs: list[dict[str, Any]], chunk: list[dict[str, Any]], start: int, end: int, **blocks: str
) -> tuple[str, str]:
    """取两个批次的 (system, user) 正文，供前缀比较。"""
    messages = build_rewrite_messages(
        "fake-doc", chunk, start=start, end=end, total=len(paragraphs), **blocks  # type: ignore[arg-type]
    )
    return messages[0]["content"], messages[1]["content"]


def test_rewrite_template_puts_every_varying_part_at_the_end_of_the_user_message() -> None:
    """材料块 / 全文 / 段号 / 正文的顺序：批间逐字相同的前缀才够长（前缀缓存的前提）。

    rewrite-v7 的布局：通用块（画像 / 背景 / 口径 / JSON / 规则）在最前，材料块（材料行 /
    概念清单 / 配图候选 / 全文）在规则之后，本次段号 + 正文（+ 校验反馈）永远收尾。
    一旦有人把材料块或全文挪回通用块中间，通用前缀会塌回几百字符。
    """
    doc = build_doc(FAKE_SOURCE)
    paragraphs = doc["paragraphs"]
    blocks = {
        "profile_block": "读者 tester 自报概念状态：c01=unknown",
        "concept_block": "c01 假概念（难度 3）",
        "full_text_block": REALISTIC_FULL_TEXT,
    }
    system_a, user_a = _rewrite_user_contents(paragraphs, paragraphs[:3], 1, 3, **blocks)
    system_b, user_b = _rewrite_user_contents(paragraphs, paragraphs[3:], 4, len(paragraphs), **blocks)

    # ① 会变的部分在末尾：正文块收尾，段号落在 JSON 示例与规则之后
    assert user_a.rstrip().endswith(f"[p003] {paragraphs[2]['text']}")
    assert user_a.index("本次给你第 1–3 段") > user_a.index("只输出 JSON")
    assert user_a.index("本次给你第 1–3 段") > user_a.index("7. 不要写")
    assert user_b.index("本次给你第 4–7 段") > user_b.index("只输出 JSON")
    # v7 的顺序：规则 < 材料行 < 概念清单 < 全文 < 本次段号
    assert user_a.index("规则：") < user_a.index("材料：《fake-doc》") < user_a.index("概念清单：")
    assert user_a.index("原文（全文") < user_a.index("本次给你第 1–3 段")

    # ② 两个批次的**首个差异点必须正好落在段号那一位**上：此前全部逐字相同
    first_diff = next(index for index, (left, right) in enumerate(zip(user_a, user_b)) if left != right)
    assert user_a[first_diff] == "1" and user_b[first_diff] == "4"
    assert user_a[first_diff - len("本次给你第 "):first_diff] == "本次给你第 "
    assert first_diff > user_a.index("原文（全文"), "全文块也该落在共享前缀里"
    assert first_diff > user_a.index("规则：")
    assert first_diff >= 1000, f"共享前缀只有 {first_diff} 字符：变化的内容又被挪到前面了"
    assert system_a == system_b, "system 提示词必须逐字相同"

    # ③ 真实材料尺寸的固定块下，共享前缀量级（capital-v1-ch01 实测 1805 字符）
    real_blocks = {
        "profile_block": REALISTIC_PROFILE_BLOCK,
        "concept_block": REALISTIC_CONCEPT_BLOCK,
        "full_text_block": REALISTIC_FULL_TEXT,
    }
    real_a = _rewrite_user_contents(paragraphs, paragraphs[:3], 1, 3, **real_blocks)[1]
    real_b = _rewrite_user_contents(paragraphs, paragraphs[3:], 4, len(paragraphs), **real_blocks)[1]
    realistic_prefix = next(
        (index for index, (left, right) in enumerate(zip(real_a, real_b)) if left != right),
        min(len(real_a), len(real_b)),
    )
    assert realistic_prefix >= 5000, f"实际材料尺寸下共享前缀只有 {realistic_prefix} 字符（目标 ~6000）"

    # ④ 固定块里不许有时间戳 / 随机量（否则每轮都建新缓存）。
    #    规则 1–7 的措辞不许被重排改动；规则 5 与新增的规则 8 是**有意**补的「读者背景当依据」那条
    #    （契约 §2 拍板 3：背景只进 why.note，不进 why.profile）——改它们要走同一条守卫。
    assert "generated_at" not in real_a
    assert not re.search(r"\d{4}-\d{2}-\d{2}T", real_a)
    # rewrite-v4：规则 2 从「只能是 expand 或 keep」改成四动作判据，并删掉「本期不实现，不要输出」
    assert "2. action 只能是 expand / compress / keep / illustrate 四个动作之一：" in real_a
    assert "compress / illustrate 本期不实现，不要输出。" not in real_a
    assert "9. illustrate 的 figure：kind 只能是 mermaid 或 svg；" in real_a
    assert "7. 不要写「改写说明」「导读」之类的额外段落；只输出 JSON。" in real_a
    assert "依据必须来自上面给的读者画像与读者背景、以及下面给的概念清单" in real_a
    assert "8. **读者背景是展开 / 保持的依据之一**" in real_a
    assert "用了读者背景就把依据写进该段的 why.note" in real_a
    assert "背景不要写进 why.profile，也不要在正文里提读者本人" in real_a
    assert user_a.index("8. **读者背景是展开 / 保持的依据之一**") < user_a.index("材料：《fake-doc》")


# ------------------------------------------------ 配图候选块（rewrite-v4，§8） --
ILLUSTRATION_BLOCK = "p034 流程：三步递进，纯文字看不出先后"
#: 块标题（只有真插了块才出现；规则 2 里提到「配图候选」四个字不算）
ILLUSTRATION_HEAD = "配图候选（analyze 标出的"
#: 插进提示词的**整段**（含它自己的空行与标题）——逐字写死在用例里，改动提示词布局必须同步改这里
ILLUSTRATION_SECTION = (
    "\n\n"
    "配图候选（analyze 标出的「图比文字省力」的位置，逐条是「段 id 类型：为什么」）：\n" + ILLUSTRATION_BLOCK
)


def _rewrite_user(**blocks: Any) -> str:
    doc = build_doc(FAKE_SOURCE)
    return build_rewrite_messages(
        "fake-doc",
        doc["paragraphs"][:3],
        profile_block="读者 tester 自报概念状态：c01=unknown",
        concept_block="c01 假概念（难度 3）",
        start=1,
        end=3,
        total=len(doc["paragraphs"]),
        **blocks,
    )[1]["content"]


def test_empty_illustration_block_adds_no_characters_and_keeps_the_v3_layout() -> None:
    """``illustration_block`` 为空（或缺省）时，提示词**一个字符都不多**（契约 §8）。

    rewrite-v4 的四个动作判据本身是本次有意改的（版本号随之升到 v4），这里钉住的是
    **配图这一格为空时不许改变任何布局**：那一格紧跟在概念清单之后、空串接上去不多一个换行，
    所以既有材料的前缀缓存结论（首个差异字符仍在段号位）不被无谓打破。
    """
    empty = _rewrite_user(illustration_block="")
    absent = _rewrite_user()  # 缺省（老调用方不传这个形参）
    assert empty == absent, "空块与不传形参必须逐字相同"
    assert ILLUSTRATION_HEAD not in empty
    # v7 的材料段相邻关系：材料行 → 概念清单 → 本次段号，中间不多任何东西
    assert "\n\n材料：《fake-doc》，共 7 段。\n\n概念清单：\nc01 假概念（难度 3）\n\n本次给你第 1–3 段" in empty

    # 非空：只在那一格插入一整段，别处逐字不变（位置在概念清单之后、材料行之前）
    with_block = _rewrite_user(illustration_block=ILLUSTRATION_BLOCK)
    assert with_block.count(ILLUSTRATION_SECTION) == 1
    assert with_block.replace(ILLUSTRATION_SECTION, "") == empty, "配图块不是「只在那一格插入」——别处也被改了"
    assert (
        with_block.index("材料：《fake-doc》")
        < with_block.index("概念清单：")
        < with_block.index(ILLUSTRATION_SECTION)
        < with_block.index("本次给你第 1–3 段")
    )
    assert with_block.rstrip().endswith("[p003] " + build_doc(FAKE_SOURCE)["paragraphs"][2]["text"])

    # 逐字稳定：没有时间戳 / 随机量，同参数两次渲染完全相同
    assert _rewrite_user(illustration_block=ILLUSTRATION_BLOCK) == with_block
    assert "generated_at" not in with_block and not re.search(r"\d{4}-\d{2}-\d{2}T", with_block)


def test_illustration_block_reaches_the_prompt_from_the_inventory(fake_chat_client) -> None:
    """``planDecisions`` 把 ``concepts.json`` 的配图候选**组进固定块**（逐字稳定），没有候选时是空串。"""
    from ar.analyze import format_illustration_block

    doc = build_doc(FAKE_SOURCE)
    inventory = {
        "concepts": [],
        "illustration_candidates": [
            {"paragraph": "p007", "kind": "流程", "why": "三步递进，纯文字看不出先后"},
            {"paragraph": "p004", "kind": "对比", "why": "两种口径并列更适合画图"},
        ],
    }
    assert format_illustration_block(inventory) == (
        "p007 流程：三步递进，纯文字看不出先后\np004 对比：两种口径并列更适合画图"
    )
    # 逐字稳定：同一份清单两次调用同文本；没有候选 / 没有清单 → 空串（模板那一格零字符插入）
    assert format_illustration_block(inventory) == format_illustration_block(dict(inventory))
    assert format_illustration_block({"illustration_candidates": []}) == ""
    assert format_illustration_block(None) == ""

    expected_section = (
        "\n\n"
        "配图候选（analyze 标出的「图比文字省力」的位置，逐条是「段 id 类型：为什么」）：\n"
        "p007 流程：三步递进，纯文字看不出先后\n"
        "p004 对比：两种口径并列更适合画图"
    )
    client = fake_chat_client(handler=fake_handler_for(doc))
    planDecisions(doc, inventory, {"reader": "tester"}, client, source_id="fake-doc", max_chars_per_batch=10000)
    prompt = client.calls[0][-1]["content"]
    assert expected_section in prompt
    assert prompt.index("材料：《fake-doc》") < prompt.index("概念清单：") < prompt.index(expected_section)

    # 没有候选（如空清单）时不插块：与不传该形参逐字相同
    bare = fake_chat_client(handler=fake_handler_for(doc))
    planDecisions(doc, {"concepts": [], "illustration_candidates": []}, {"reader": "tester"}, bare,
                  source_id="fake-doc", max_chars_per_batch=10000)
    assert ILLUSTRATION_HEAD not in bare.calls[0][-1]["content"]


@dataclass
class _RecordingReply:
    """替身回复：``ar.rewrite`` 只读 text / model / usage 三个字段（经工具层落产物）。"""

    text: str
    model: str = "fake-model"
    usage: dict[str, Any] | None = None


@dataclass
class _RecordingClient(FakeOnceMixin):
    """记录每次调用的「开始 / 结束」与并发峰值，用来验证预热是**串行**的。"""

    handler: Any
    delay: float = 0.02
    events: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    max_active: int = 0
    _active: int = 0
    _lock: Any = field(default_factory=threading.Lock)

    def chat(self, messages: Any, **kwargs: Any) -> _RecordingReply:
        ids = tuple(prompt_ids(messages[-1]["content"]))
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


@dataclass
class _FlakyChatClient(FakeChatClient):
    """前 ``failures`` 次调用先回 503（``SERVER``，可重试），之后照常按 ``handler`` 回。

    策略给「1 次重试 + 退避 0 ms」：能考出逐批的重试账，而用例不必真的等退避。
    """

    failures: int = 1
    tries: int = 0

    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(max_retries=1, initial_delay_ms=0, max_delay_ms=0)

    async def arequest_once(self, request: LlmRequest) -> LlmResult:
        self.tries += 1
        if self.tries <= self.failures:
            return LlmResult(
                ok=False, failure=LlmFailure(code="SERVER", message="HTTP 503：网关抖动", status=503)
            )
        return await super().arequest_once(request)


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
    # 逐批尝试账 + 多批汇总：7 批各一次、没有重试
    assert len(meta["attempts"]) == 7, meta["attempts"]
    assert all(len(entry["attempts"]) == 1 for entry in meta["batch_usage"])
    assert meta["retry_account"] == {"tool": "rewrite_batch", "requests": 7, "retries": 0, "codes": []}


def test_plan_decisions_works_inside_a_running_event_loop() -> None:
    """在**运行中的事件循环**里调用同步的 ``planDecisions``：正常完成，不出现 ``asyncio.run`` 降级。

    写法就是将来 async 调用方（HTTP 路由）的样子：同步用例里 ``asyncio.run(_scenario())``，
    ``_scenario`` 内部直接调同步的 ``planDecisions``。

    修复前（预热批与「检测到运行中事件循环 → 退回全串行」的老分支）这里 7 批**全部**降级成
    ``RuntimeError: asyncio.run() cannot be called from a running event loop``：
    ``meta.model_errors`` 写满 7 条、7 段全进 ``meta.unplanned_paragraphs``、置信度全 0、
    ``retry_account.requests`` 是 0（一个请求都没发出去）、CLI 以退出码 6 结束，
    还伴随 7 条 ``RuntimeWarning: coroutine 'acall' was never awaited``。
    """
    doc = build_doc(FAKE_SOURCE)
    inner = fake_handler_for(doc)
    caller_thread = threading.get_ident()
    calls: list[tuple[int, tuple[str, ...]]] = []
    lock = threading.Lock()

    def handler(messages: list[dict[str, str]]) -> str:
        with lock:
            calls.append((threading.get_ident(), tuple(prompt_ids(messages[-1]["content"]))))
        return inner(messages)

    client = FakeChatClient(handler=handler)

    async def _scenario():
        assert asyncio.get_running_loop() is not None, "这条用例的意义就在于「调用方已有事件循环」"
        return planDecisions(doc, None, {"reader": "tester"}, client, source_id="fake-doc", max_chars_per_batch=1)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        decisions, degraded, meta = asyncio.run(_scenario())

    # ① 没有「在事件循环里跑 asyncio.run」这类降级
    assert not any("asyncio.run() cannot be called" in item for item in degraded), degraded
    assert not any("RuntimeError" in item for item in degraded), degraded
    assert not [item for item in caught if "never awaited" in str(item.message)], "照旧会有没 await 的协程被丢掉"
    # ② 全部段落都拿到了模型决策：没有批次失败、没有漏段
    assert meta["model_errors"] == [], meta["model_errors"]
    assert meta["unplanned_paragraphs"] == []
    assert meta["rewrite_incomplete"] is False
    assert [decision.paragraph for decision in decisions] == [p["id"] for p in doc["paragraphs"]]
    assert [decision.confidence for decision in decisions] == [0.8] * 7, "每段都拿到模型给的决策（不是降级补的 keep）"
    # ③ 账目与同步路径逐字一致：7 批各一次首发的尝试账 + 一条汇总，一个请求都没丢
    assert len(meta["attempts"]) == 7 and len(meta["batch_usage"]) == 7
    assert all(len(entry["attempts"]) == 1 for entry in meta["batch_usage"])
    assert meta["retry_account"] == {"tool": "rewrite_batch", "requests": 7, "retries": 0, "codes": []}
    # ④ 所有批次（含第 1 批预热）都在工作线程里跑：调用线程里没有运行中的事件循环，asyncio.run 才成立
    assert len(calls) == 7
    assert "p001" in calls[0][1], "预热批仍是最先发的那一批"
    assert all(ident != caller_thread for ident, _ids in calls), "有批次跑在调用线程上——事件循环内调用会立刻退化成降级"
    assert len({ident for ident, ids in calls if "p001" not in ids}) <= 4, "其余批次的线程数不超过并发上限 4"


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

    class _UsageClient(FakeOnceMixin):
        def chat(self, messages: Any, **kwargs: Any) -> _RecordingReply:
            ids = prompt_ids(messages[-1]["content"])
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


# ------------------------------------------------------------ 反向验证：重试账 --
def test_batch_retry_is_accounted_per_batch_and_in_the_summary(fake_chat_client) -> None:
    """反向验证（改写侧）：第 1 次 503、第 2 次成功 → 逐批两条、汇总 requests=2 / retries=1。

    合并成一批（``max_chars_per_batch=10000``）才有确定的「第 1 次调用」。
    """
    doc = build_doc(FAKE_SOURCE)
    client = _FlakyChatClient(handler=fake_handler_for(doc), failures=1)
    decisions, _degraded, meta = planDecisions(
        doc, None, {"reader": "tester"}, client, source_id="fake-doc", max_chars_per_batch=10000
    )

    assert client.tries == 2, "第一次 503 就该再试一次"
    assert len(meta["batch_usage"]) == 1
    attempts = meta["batch_usage"][0]["attempts"]
    assert [item["try"] for item in attempts] == [1, 2]
    assert attempts[0]["code"] == "SERVER" and attempts[0]["delay_ms"] == 0
    assert attempts[1]["code"] is None
    assert meta["attempts"] == attempts, "meta.attempts 是逐批账的拼接"
    assert meta["retry_account"] == {"tool": "rewrite_batch", "requests": 2, "retries": 1, "codes": ["SERVER"]}
    # 重试没有把产物写坏：决策照样每段一条（这一批全部 keep）
    assert [decision.paragraph for decision in decisions] == [p["id"] for p in doc["paragraphs"]]
    assert meta["model_errors"] == [] and meta["unplanned_paragraphs"] == []


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
    # rewrite-v4（2026-10-13）：四个动作的判据进提示词 + 配图候选块；
    # rewrite-v5（2026-10-14）：事实锚口径块 + 规则 10（逐段申报）→ 版本号升到 v5
    # rewrite-v6（2026-10-14 复核修复）：规则 11（why.profile 语义）+ 空自评措辞改精确
    assert decisions_payload["meta"]["prompt_version"] == "rewrite-v7"
    # 没有 background 键的画像 = 没有背景信号：background_used 为 false，指纹按「未提供」文案算
    assert decisions_payload["meta"]["background_used"] is False
    assert decisions_payload["meta"]["background_fingerprint"] == sketch_fingerprint(EMPTY_BACKGROUND)

    changes_payload = read_json(outcome.changes_path)
    # 事实锚口径（§14.1）：两份产物的 meta 都记了这次生效的那一档 + 它从哪来
    for payload in (decisions_payload, changes_payload):
        assert payload["meta"]["fact_anchor_policy"] == "keep_all"
        assert "pref_fact_anchor" in payload["meta"]["fact_anchor_policy_source"]
    # 改写阶段的 fidelity 记口径、waived 留空（放行条目只有 ar check 才写）
    assert changes_payload["fidelity"]["fact_anchor_policy"] == "keep_all"
    assert changes_payload["fidelity"]["waived"] == []
    # 段数记在**两份**产物的 meta 里（2026-10-13 复核修复 F5：changes.json 之前缺这一格）
    assert decisions_payload["meta"]["paragraph_count"] == len(doc["paragraphs"])
    assert changes_payload["meta"]["paragraph_count"] == len(doc["paragraphs"])
    # 记账落两份产物（PLAN-llm-tools §3.4）：逐次尝试 + 汇总，逐批明细里也有这一批的账
    for payload in (decisions_payload, changes_payload):
        meta = payload["meta"]
        assert [item["tool"] for item in meta["attempts"]] == ["rewrite_batch"]
        assert meta["attempts"][0]["code"] is None
        assert meta["retry_account"] == {
            "tool": "rewrite_batch",
            "requests": 1,
            "retries": 0,
            "codes": [],
        }
        assert [item["try"] for item in meta["batch_usage"][0]["attempts"]] == [1]
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
    # 「保持」/「配图」段与原文逐字节一致，「展开」段真的更长，「压缩」段真的更短（图不进 md）
    blocks_now = split_rewritten_blocks((dirs.data / "fake-doc" / "rewritten.md").read_text(encoding="utf-8"))
    for index, paragraph in enumerate(doc["paragraphs"]):
        if paragraph["id"] == "p005":
            assert len(blocks_now[index]) > len(paragraph["text"])
        elif paragraph["id"] == "p006":
            assert blocks_now[index] == FAKE_COMPRESSED
            assert len(blocks_now[index]) < len(paragraph["text"])
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


# ---------------------------- 压缩动作（compress，PLAN-actions-feedback §3） --
def test_compress_action_shortens_the_paragraph_and_never_marks_deferred(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """① 假模型给 ``compress`` → 改写稿确实变短、``action == "compress"``、**没有** ``deferred``。"""
    from ar.jsonio import read_json

    doc = write_fake_doc(dirs)
    client = fake_chat_client(handler=fake_handler_for(doc))
    outcome = fake_run(dirs, client)

    assert [d.action for d in outcome.decisions] == ["keep"] * 4 + ["expand", "compress", "illustrate"]
    blocks = split_rewritten_blocks(outcome.rewritten_markdown)
    assert blocks[5] == FAKE_COMPRESSED
    assert len(blocks[5]) < len(doc["paragraphs"][5]["text"]), "压缩段没变短，compress 就成了空操作"
    assert blocks[5].rstrip().endswith("。")

    changes_payload = read_json(outcome.changes_path)
    comp = {c["paragraph"]: c for c in changes_payload["changes"]}["p006"]
    assert comp["action"] == "compress"
    assert "deferred" not in comp, "四个动作都已实现，compress 不该再标 deferred"
    assert "figure" not in comp and "figure_error" not in comp
    assert changes_payload["meta"]["action_counts"]["compress"] == 1
    assert changes_payload["meta"]["deferred_count"] == 0
    # 决策记录里不许出现 figure / deferred（它们只进 changes.json）
    decisions_payload = read_json(outcome.decisions_path)
    assert decisions_payload["meta"]["implemented_actions"] == ["expand", "compress", "keep", "illustrate"]
    assert decisions_payload["meta"]["action_counts"]["compress"] == 1
    day6 = [d for d in decisions_payload["decisions"] if d["paragraph"] == "p006"][0]
    assert day6["action"] == "compress"
    assert set(day6) == {"paragraph", "action", "why", "confidence"}
    # 压缩稿没丢事实锚：check 通过
    checked = check_fidelity("fake-doc", dirs.data)
    assert checked.mismatches == [], fidelity_problems(checked)
    assert checked.checked is True


def test_compress_paragraph_does_not_trip_keep_mismatch(dirs: SimpleNamespace, fake_chat_client) -> None:
    """③ 压缩段**不误报** ``keep_mismatch``，``paragraphs_match_source`` 不被污染。

    这是本项最容易漏的一处：逐段保真的跳过名单原来只认 ``expand``，compress 段会被拿原文比压缩稿，
    必然「不一致」，连带 ``paragraphs_match_source: false`` 与 CLI 假警告。
    """
    doc = write_fake_doc(dirs)
    outcome = fake_run(dirs, fake_chat_client(handler=fake_handler_for(doc)))

    assert outcome.fidelity["keep_paragraphs_mismatch"] == []
    assert outcome.fidelity["count_mismatch"] == []
    assert outcome.fidelity["paragraphs_match_source"] is True
    assert not [line for line in outcome.degraded if "与原文不一致" in line], outcome.degraded
    assert not [line for line in outcome.degraded if line.startswith("p006：")], outcome.degraded
    assert len(split_rewritten_blocks(outcome.rewritten_markdown)) == len(doc["paragraphs"])


@pytest.mark.parametrize(
    "compressed, fragment",
    [
        # 没变短：疑似没压缩或只是换了个说法
        ("这一段很啰嗦：同一个意思来回说了三遍，说三遍，还是同一件事，真的同一件事。", "不比原文"),
        # 短了但没以句末标点收尾：疑似被截断
        ("这一段很啰嗦：同一个意思说了三遍", "没有以句末标点收尾"),
    ],
)
def test_compress_that_is_not_shorter_or_is_truncated_is_degraded(
    dirs: SimpleNamespace, fake_chat_client, compressed: str, fragment: str
) -> None:
    """② compress 的截断判据方向与 expand **相反**（变短 + 句末标点收尾），两条都可见。"""
    doc = write_fake_doc(dirs)
    source = doc["paragraphs"][5]["text"]
    assert (len(compressed) < len(source)) == (fragment == "没有以句末标点收尾"), "用例素材与判据要对得上"
    client = fake_chat_client(handler=fake_handler(compressed_text=compressed))
    outcome = fake_run(dirs, client)

    lines = [line for line in outcome.degraded if line.startswith("p006：")]
    assert lines and any(fragment in line for line in lines), outcome.degraded
    # 降级可见、产物照旧可用：段数与段序不变（LLM 给的正文仍然按 compress 落稿）
    assert len(split_rewritten_blocks(outcome.rewritten_markdown)) == len(doc["paragraphs"])
    assert outcome.fidelity["checked"] is False, "rewrite 阶段不许写 checked: true"


def test_compress_without_a_rewrite_text_falls_back_to_keep(dirs: SimpleNamespace, fake_chat_client) -> None:
    """模型说 compress 却没给压缩稿 → 退回 ``keep`` + 可见降级（不留「compress 但正文没变」的假动作）。"""
    doc = write_fake_doc(dirs)
    outcome = fake_run(dirs, fake_chat_client(handler=fake_handler(compressed_text="")))

    by_id = {c["paragraph"]: c for c in outcome.changes}
    assert by_id["p006"]["action"] == "keep"
    assert "deferred" not in by_id["p006"], "这不是「枚举内未实现」的 deferred 情形"
    assert len(split_rewritten_blocks(outcome.rewritten_markdown)) == len(doc["paragraphs"])
    blocks = split_rewritten_blocks(outcome.rewritten_markdown)
    assert blocks[5] == doc["paragraphs"][5]["text"], "退回 keep 后正文就是原文"
    assert any(line.startswith("p006：") and "没有给压缩稿" in line for line in outcome.degraded), outcome.degraded
    assert outcome.fidelity["paragraphs_match_source"] is True


def test_compress_tampering_a_fact_anchor_is_caught_by_check(dirs: SimpleNamespace, fake_chat_client) -> None:
    """负向对照（契约 §11.2）：压缩稿把数字改坏 → 事实锚必须报错并点名段 id。

    压缩**不放宽**任何校验：走现成的 ``check_fidelity``，与 expand 同一条底线。
    """
    write_fake_doc(dirs, with_concepts=False)
    mangled = "马克思在 1858 年写的《政治经济学批判》里，开头就是“庞大的商品堆积”。"
    source = FAKE_SOURCE_BLOCKS[4]
    assert len(mangled) < len(source), "压缩稿要真的更短（否则降级理由会混进这条用例）"
    assert FAKE_NUMBERS[0] in source and FAKE_NUMBERS[0] not in mangled, "注入未生效（数字没被改坏）"

    client = fake_chat_client(
        handler=fake_handler(actions={"p005": "compress"}, compressed_text=mangled, expand_ids=())
    )
    outcome = fake_run(dirs, client)
    assert {c["paragraph"]: c["action"] for c in outcome.changes}["p005"] == "compress"

    written = (dirs.data / "fake-doc" / "rewritten.md").read_text(encoding="utf-8")
    assert "1858" in written and "1859" not in written, "注入未落到改写稿上"

    checked = check_fidelity("fake-doc", dirs.data)
    problems = fidelity_problems(checked)
    assert problems, "压缩稿改了数字却没报错"
    assert any("p005" in problem for problem in problems), problems
    assert any("1859" in problem for problem in problems), problems
    assert checked.checked is False


# ------------------------------- 配图动作（illustrate，同上 §4.1 / §4.2） --
def test_illustrate_inlines_the_figure_and_keeps_the_body_byte_identical(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """④ illustrate 成功：``changes[i].figure`` 形状正确、该段正文与原文逐字节相同、
    ``decisions.json`` 不带 ``figure``。"""
    from ar.jsonio import read_json

    doc = write_fake_doc(dirs)
    outcome = fake_run(dirs, fake_chat_client(handler=fake_handler_for(doc)))

    changes_payload = read_json(outcome.changes_path)
    by_id = {c["paragraph"]: c for c in changes_payload["changes"]}
    illustrated = by_id["p007"]
    assert illustrated["action"] == "illustrate"
    assert set(illustrated["figure"]) == {"kind", "source", "caption"}
    assert illustrated["figure"]["kind"] == "mermaid"
    assert illustrated["figure"]["source"].strip().split()[0] == "flowchart"
    assert illustrated["figure"]["caption"] == "三步递进"
    assert "figure_error" not in illustrated
    assert "deferred" not in illustrated
    # 图不进 md：该段正文与原文**逐字节**相同
    blocks = split_rewritten_blocks((dirs.data / "fake-doc" / "rewritten.md").read_text(encoding="utf-8"))
    assert blocks[6] == doc["paragraphs"][6]["text"]
    assert "flowchart" not in outcome.rewritten_markdown
    # decisions.json 不带 figure（与不带 deferred 同口径）
    decisions_payload = read_json(outcome.decisions_path)
    record = [d for d in decisions_payload["decisions"] if d["paragraph"] == "p007"][0]
    assert "figure" not in record and set(record) == {"paragraph", "action", "why", "confidence"}
    # 成功路径不该有该段的降级条目
    assert not [line for line in outcome.degraded if line.startswith("p007：")], outcome.degraded
    assert changes_payload["meta"]["action_counts"]["illustrate"] == 1
    # 事实锚照样通过（图不参与，正文与原文一致）
    assert check_fidelity("fake-doc", dirs.data).mismatches == []


SAFE_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><circle cx="5" cy="5" r="4"/></svg>'


@pytest.mark.parametrize(
    "figure, fragment",
    [
        # 畸形 mermaid：首词不是图类型关键字
        ({"kind": "mermaid", "source": "这不是图\n  A --> B"}, "首词"),
        # 畸形 mermaid：带 Markdown 围栏
        ({"kind": "mermaid", "source": "```mermaid\nflowchart TD\n  A --> B\n```"}, "围栏"),
        # kind 不在两类之内
        ({"kind": "plantuml", "source": "@startuml\n@enduml"}, "kind"),
        # 带 <script> 的 SVG
        (
            {"kind": "svg", "source": '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'},
            "<script>",
        ),
        # 带 on* 事件属性的 SVG
        ({"kind": "svg", "source": '<svg xmlns="http://www.w3.org/2000/svg"><rect onload="x()"/></svg>'}, "on*"),
        # 引用外部资源的 href
        (
            {"kind": "svg", "source": '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://x/y.png"/></svg>'},
            "href",
        ),
        # 外部资源的 xlink:href（命名空间属性）
        (
            {
                "kind": "svg",
                "source": '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
                '<image xlink:href="https://x/y.png"/></svg>',
            },
            "href",
        ),
        # foreignObject（可塞进任意 HTML）
        (
            {"kind": "svg", "source": '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>'},
            "foreignObject",
        ),
        # XML 都解析不了
        ({"kind": "svg", "source": "<svg><rect></svg>"}, "解析失败"),
        # 根本没给 figure
        (None, "没有给 figure"),
    ],
)
def test_bad_figures_fall_back_to_keep_with_a_visible_error(
    dirs: SimpleNamespace, fake_chat_client, figure: dict[str, Any] | None, fragment: str
) -> None:
    """⑤ 畸形 mermaid / 带 ``<script>`` 的 SVG → ``action: "keep"`` + ``figure_error`` + 可见降级。"""
    doc = write_fake_doc(dirs)
    client = fake_chat_client(handler=fake_handler(figure=figure))
    outcome = fake_run(dirs, client)

    by_id = {c["paragraph"]: c for c in outcome.changes}
    fallback = by_id["p007"]
    assert fallback["action"] == "keep", fallback
    assert "figure" not in fallback, "配图失败时不许留下 figure"
    assert fragment in fallback["figure_error"], fallback["figure_error"]
    # 降级必须可见（meta.degraded 里点名段 id）
    assert any(line.startswith("p007：") and fragment in line for line in outcome.degraded), outcome.degraded
    # 正文照旧（退回 keep 后与原文逐字节相同），段数 / 段序不变
    blocks = split_rewritten_blocks(outcome.rewritten_markdown)
    assert len(blocks) == len(doc["paragraphs"])
    assert blocks[6] == doc["paragraphs"][6]["text"]
    assert outcome.fidelity["paragraphs_match_source"] is True
    assert not [line for line in outcome.degraded if "与原文不一致" in line], outcome.degraded


def test_wellformed_svg_with_internal_anchor_passes_the_whitelist(dirs: SimpleNamespace, fake_chat_client) -> None:
    """白名单之内（含 ``#`` 内部锚点）的 SVG 原样使用：源码一个字都不改。"""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><defs><path id="p"/></defs>'
        '<use href="#p"/><circle cx="1" cy="2" r="3"/></svg>'
    )
    write_fake_doc(dirs)
    outcome = fake_run(dirs, fake_chat_client(handler=fake_handler(figure={"kind": "svg", "source": svg})))
    assert {c["paragraph"]: c for c in outcome.changes}["p007"]["figure"] == {"kind": "svg", "source": svg}


def test_figure_source_with_extra_fact_anchors_is_degraded_but_not_blocked(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """图里出现原文没有的数字 → **不阻断**，只记一条降级（契约 §1 第 3 条）。"""
    write_fake_doc(dirs)
    figure = {"kind": "mermaid", "source": "flowchart TD\n  A[第一步 3 项] --> B[第二步 1859]"}
    outcome = fake_run(dirs, fake_chat_client(handler=fake_handler(figure=figure)))

    illustrated = {c["paragraph"]: c for c in outcome.changes}["p007"]
    assert illustrated["action"] == "illustrate" and illustrated["figure"] == figure
    lines = [line for line in outcome.degraded if line.startswith("p007：")]
    assert any("事实锚点" in line and "数字" in line for line in lines), outcome.degraded
    # 图不进 md ⇒ 事实锚照样通过
    assert check_fidelity("fake-doc", dirs.data).mismatches == []


def test_overlong_caption_is_truncated_with_a_visible_note(dirs: SimpleNamespace, fake_chat_client) -> None:
    """``caption`` 超过 40 字 → 截断（**不静默**：记一条降级）。"""
    write_fake_doc(dirs)
    caption = "这是一句特别长的图注" * 6  # 60 字
    outcome = fake_run(
        dirs,
        fake_chat_client(
            handler=fake_handler(figure={"kind": "mermaid", "source": FAKE_FIGURE["source"], "caption": caption})
        ),
    )
    illustrated = {c["paragraph"]: c for c in outcome.changes}["p007"]
    assert illustrated["action"] == "illustrate"
    assert len(illustrated["figure"]["caption"]) == 40
    assert any("caption" in line and "截断" in line for line in outcome.degraded), outcome.degraded


def test_illustrate_paragraph_that_also_gives_a_rewrite_keeps_the_source_body(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """illustrate 段多给了 ``rewrite``：图不进正文，正文保持原文，多出来的改写稿**不被静默吞掉**。"""
    doc = write_fake_doc(dirs)
    handler = fake_handler()
    original = handler

    def wrapped(messages: list[dict[str, str]]) -> str:
        payload = json.loads(original(messages))
        for entry in payload["decisions"]:
            if entry["action"] == "illustrate":
                entry["rewrite"] = "这段本来不该被改写。"
        return json.dumps(payload, ensure_ascii=False)

    outcome = fake_run(dirs, fake_chat_client(handler=wrapped))
    blocks = split_rewritten_blocks(outcome.rewritten_markdown)
    assert blocks[6] == doc["paragraphs"][6]["text"], "illustrate 段的正文必须与原文逐字节相同"
    illustrated = {c["paragraph"]: c for c in outcome.changes}["p007"]
    assert illustrated["action"] == "illustrate" and illustrated["figure"] == FAKE_FIGURE
    assert any("已忽略" in line and line.startswith("p007：") for line in outcome.degraded), outcome.degraded


# ------------------------------------------------- 读者背景（PLAN §6.3 / §6.4） --
#: 读者素描（读前背景问卷）的样例：与 ``tests/test_survey.py`` 的 FULL_BACKGROUND 同形
#: （大学两项收在 ``university`` 下——契约 §6.1 的 YAML 是嵌套的）
SAMPLE_BACKGROUND = {
    "version": 1,
    "answered_at": "2026-10-08T16:40:00+08:00",
    "education": "undergraduate",
    "university": {"discipline": "economics", "major": "国际经济与贸易"},
    "strengths": ["economics_finance"],
}

CONCEPT_PROFILE = {
    "reader": "local-default",
    "concepts": {"c01": {"state": "unknown", "confidence": 0.7}},
    "preferences": [],
    "overrides": [],
}


def _write_profile(dirs: SimpleNamespace, profile: dict) -> None:
    from ar.profile import save_profile

    save_profile(dirs.profiles, "local-default", profile)


@pytest.mark.parametrize(
    "concepts, background, expect_used, expect_background_used, note_marker",
    [
        # 只有背景：本次拍板的核心场景——背景进了提示词，产物必须说「用了画像」
        (False, True, True, True, "仅背景，无概念自评"),
        # 只有概念：背景为空，素描取「未提供」分支
        (True, False, True, False, "1 条概念自评"),
        # 都有
        (True, True, True, True, "读者素描"),
        # 都没有：既有降级路径不变
        (False, False, False, False, "无画像"),
    ],
)
def test_profile_used_and_background_used_four_combinations(
    dirs: SimpleNamespace,
    fake_chat_client,
    concepts: bool,
    background: bool,
    expect_used: bool,
    expect_background_used: bool,
    note_marker: str,
) -> None:
    """``profile_used`` / ``background_used`` 的四种组合逐一断言（契约 §12.7）。

    背景进了提示词、产物却声称「无画像」是本方案最容易出的事故（§6.4），这里专门守它。
    """
    from ar.jsonio import read_json
    from ar.profile import default_profile

    doc = write_fake_doc(dirs)
    profile = dict(default_profile("local-default"))
    if concepts:
        profile["concepts"] = dict(CONCEPT_PROFILE["concepts"])
    if background:
        profile["background"] = dict(SAMPLE_BACKGROUND)
    _write_profile(dirs, profile)

    client = fake_chat_client(handler=fake_handler_for(doc))
    outcome = run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", client)

    decisions_payload = read_json(outcome.decisions_path)
    changes_payload = read_json(outcome.changes_path)
    for meta in (decisions_payload["meta"], changes_payload["meta"]):
        assert meta["profile_used"] is expect_used
        assert meta["background_used"] is expect_background_used
        assert note_marker in meta["profile_note"]
        assert len(meta["background_fingerprint"]) == 8

    if expect_used and not concepts:
        # 只有背景：素描真的进了提示词，且 why.profile 不被误覆盖成「难度 N（无画像）」
        prompt = client.calls[0][-1]["content"]
        assert "读者背景：" in prompt and "大学为经济学门类（专业：国际经济与贸易）" in prompt
        assert not any(
            ref.endswith("（无画像）") for d in decisions_payload["decisions"] for ref in d["why"]["profile"]
        )
        assert all("（无画像" not in str(d["why"].get("note") or "") for d in decisions_payload["decisions"])
    if not expect_used:
        # 都没有：既有降级路径不变（每段都能看出依据从哪来）
        assert any(
            ref.endswith("（无画像）") for d in decisions_payload["decisions"] for ref in d["why"]["profile"]
        )
        assert any("无画像" in item for item in decisions_payload["meta"]["degraded"])
        assert "未提供" in client.calls[0][-1]["content"]
    if not background:
        assert "未提供" in client.calls[0][-1]["content"]
        # 素描块自己不许再带一次「读者背景：」前缀（模板那一行已经有了）
        prompt = client.calls[0][-1]["content"]
        assert prompt.count("读者背景：") == 1


def test_background_fingerprint_changes_when_the_background_changes(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """背景变则 ``background_fingerprint`` 变（旧产物与新产物不可直接比，契约 §14）。"""
    from ar.jsonio import read_json
    from ar.profile import default_profile

    doc = write_fake_doc(dirs)
    fingerprints: list[str] = []
    for education in ("undergraduate", "master"):
        profile = dict(default_profile("local-default"))
        profile["background"] = dict(SAMPLE_BACKGROUND, education=education)
        _write_profile(dirs, profile)
        outcome = run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", fake_chat_client(handler=fake_handler_for(doc)))
        payload = read_json(outcome.decisions_path)
        assert payload["meta"]["background_used"] is True
        fingerprints.append(payload["meta"]["background_fingerprint"])
    assert fingerprints[0] != fingerprints[1]

    # 只改 followups 的 ``at``（问答本身没变）不该换指纹：素描同画像同文本
    profile = dict(default_profile("local-default"))
    profile["background"] = dict(SAMPLE_BACKGROUND, followups=[{"q": "问", "a": "答", "at": "2026-10-08T16:00:00+08:00"}])
    _write_profile(dirs, profile)
    first = read_json(run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", fake_chat_client(handler=fake_handler_for(doc))).decisions_path)
    profile["background"] = dict(
        SAMPLE_BACKGROUND, followups=[{"q": "问", "a": "答", "at": "2026-10-09T09:00:00+08:00"}]
    )
    _write_profile(dirs, profile)
    second = read_json(run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", fake_chat_client(handler=fake_handler_for(doc))).decisions_path)
    assert first["meta"]["background_fingerprint"] == second["meta"]["background_fingerprint"]


def test_skipped_background_counts_as_no_profile(dirs: SimpleNamespace, fake_chat_client) -> None:
    """整场跳过（``skipped: true``）且**没有任何追问摘要**时按「没有背景」处理：既有无画像降级路径不变。"""
    from ar.jsonio import read_json
    from ar.profile import default_profile

    doc = write_fake_doc(dirs)
    profile = dict(default_profile("local-default"))
    profile["background"] = {"version": 1, "answered_at": "2026-10-08T16:40:00+08:00", "skipped": True}
    _write_profile(dirs, profile)

    client = fake_chat_client(handler=fake_handler_for(doc))
    payload = read_json(run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", client).decisions_path)
    assert payload["meta"]["profile_used"] is False
    assert payload["meta"]["background_used"] is False
    assert "未提供（读者跳过了背景问卷）" in client.calls[0][-1]["content"]
    assert client.calls[0][-1]["content"].count("读者背景：") == 1


def test_prompt_has_exactly_one_background_label_for_empty_and_skipped_sketches() -> None:
    """提示词里「读者背景：」只能出现一次（模板那一行）；素描自己不许再带一遍同名前缀。

    上一版 ``SKIPPED_TEXT`` / ``EMPTY_BACKGROUND`` 都自带「读者背景：」，拼出来是两行同样的字。
    """
    from ar.prompts import EMPTY_BACKGROUND_SKETCH, build_rewrite_messages
    from ar.survey import EMPTY_BACKGROUND, SKIPPED_TEXT, render_sketch

    assert EMPTY_BACKGROUND_SKETCH == EMPTY_BACKGROUND, "两份常量必须逐字一致（有且只有一处在用）"
    assert render_sketch({"version": 1, "skipped": True}) == SKIPPED_TEXT
    doc = build_doc(FAKE_SOURCE)
    for sketch in ("", None, EMPTY_BACKGROUND, SKIPPED_TEXT):
        messages = build_rewrite_messages(
            "fake-doc",
            doc["paragraphs"][:2],
            profile_block="读者 tester 自报概念状态：c01=unknown",
            background_block=sketch or "",
            concept_block="c01 假概念（难度 3）",
            start=1,
            end=2,
            total=len(doc["paragraphs"]),
        )
        content = messages[1]["content"]
        # 标签只出现一次（「读者背景：」这个**带冒号的标签行**；规则 8 里提到「读者背景」四个字不算）
        assert content.count("读者背景：") == 1, content
        assert "\n读者背景：\n未提供" in content, content
    # 有素描时当然也只带一次（素描文本里没有这个前缀）
    real = build_rewrite_messages(
        "fake-doc",
        doc["paragraphs"][:2],
        profile_block="读者 tester 自报概念状态：c01=unknown",
        background_block=render_sketch({"version": 1, "education": "undergraduate"}),
        concept_block="c01 假概念（难度 3）",
        start=1,
        end=2,
        total=len(doc["paragraphs"]),
    )[1]["content"]
    assert real.count("读者背景：") == 1 and "本科" in real


def test_skipped_then_followups_still_counts_as_background(dirs: SimpleNamespace, fake_chat_client) -> None:
    """跳过固定问卷但答了模型追问：素描非空 ⇒ 必须算「有背景」。

    只认固定题的话会写出 ``profile_used: false`` + 「无画像模式」，而素描明明已经注入了提示词
    —— 正是契约 §6.4 要防的那类「看起来做了其实没做」。
    """
    from ar.jsonio import read_json
    from ar.profile import default_profile

    doc = write_fake_doc(dirs)
    profile = dict(default_profile("local-default"))
    profile["background"] = {
        "version": 1,
        "answered_at": "2026-10-08T16:40:00+08:00",
        "skipped": True,
        "followups": [{"q": "读过《资本论》原文吗？", "a": "只读过二手介绍", "at": "2026-10-08T16:45:00+08:00"}],
    }
    _write_profile(dirs, profile)

    client = fake_chat_client(handler=fake_handler_for(doc))
    outcome = run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", client)
    payload = read_json(outcome.decisions_path)
    assert payload["meta"]["background_used"] is True
    assert payload["meta"]["profile_used"] is True, "背景进了提示词，产物就必须说用了画像"
    assert payload["meta"]["profile_note"].count("追问摘要") == 1
    assert "无画像" not in payload["meta"]["profile_note"]
    assert not any(item.startswith("无画像模式") for item in payload["meta"]["degraded"])
    # skipped 标志本身保持原样（它只描述「读者跳过了固定问卷」这个事实）
    from ar.profile import load_profile

    saved, _notes = load_profile(dirs.profiles, "local-default")
    assert saved["background"]["skipped"] is True
    assert "未提供（读者跳过了背景问卷）" not in client.calls[0][-1]["content"]
    assert "只读过二手介绍" in client.calls[0][-1]["content"]
    assert not any(
        ref.endswith("（无画像）") for d in payload["decisions"] for ref in d["why"]["profile"]
    )


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
def test_usage_summary_prints_batches_and_the_real_request_count() -> None:
    """命令行那行账同时给出**批数**与**真实请求数 / 重试次数**（2026-10-13 复核修复 P1b）。

    ``usage.requests`` 是**批数**（既有字段语义，不许改）；真相在 ``retry_account.requests``
    （含每次重试，默认最多 6 倍）。只打批数会把真实请求数严重低估。
    """
    from ar.rewrite import _usage_summary

    line = _usage_summary(
        {
            "requests": 3,
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "cached_tokens": None,
        },
        {"tool": "rewrite_batch", "requests": 6, "retries": 3, "codes": ["SERVER"]},
    )
    assert line.startswith("请求 6 次（3 批；其中重试 3 次）；"), line
    assert "tokens：prompt 10 / completion 20 / 合计 30" in line

    # 没有重试时两个数相等，也要把「批数」说清楚（免得被读成 6 次 HTTP）
    plain = _usage_summary({"requests": 2, "total_tokens": 5}, {"requests": 2, "retries": 0, "codes": []})
    assert plain.startswith("请求 2 次（2 批；其中重试 0 次）；")

    # 没有逐次记账（外部手工拼的 usage）时**不许**把批数当请求数报
    fallback = _usage_summary({"requests": 3})
    assert "未记账" in fallback and "3 批" in fallback


def test_cli_rewrite_line_shows_batches_and_real_requests_on_a_retry(
    run_cli, rewrite_env: SimpleNamespace, sample_fake_handler
) -> None:
    """端到端一次：首发 503 + 重试成功 → CLI 打「请求 2 次（1 批；其中重试 1 次）」。

    产物里 ``usage.requests`` 仍是**批数**（1），``retry_account.requests`` 才是真实 HTTP 次数（2）。
    """
    client = _FlakyChatClient(handler=sample_fake_handler, failures=1)
    code, output = run_cli_with_fake(
        run_cli, rewrite_env, client, "rewrite", "sample", "--reader", "local-default"
    )

    assert code == EXIT_OK, output
    assert "请求 2 次（1 批；其中重试 1 次）" in output, output
    assert client.tries == 2, "重试确实发生了（否则这条断言没意义）"

    payload = json.loads((rewrite_env.target / "decisions.json").read_text(encoding="utf-8"))
    assert payload["meta"]["usage"]["requests"] == 1, "usage.requests 仍是批数（既有语义不动）"
    assert payload["meta"]["retry_account"]["requests"] == 2
    assert payload["meta"]["retry_account"]["retries"] == 1


def test_cli_rewrite_writes_artifacts_and_check_passes(
    run_cli, rewrite_env: SimpleNamespace, fake_chat_client, sample_fake_handler
) -> None:
    client = fake_chat_client(handler=sample_fake_handler)

    code, output = run_cli_with_fake(run_cli, rewrite_env, client, "rewrite", "sample", "--reader", "local-default")
    assert code == EXIT_OK, output
    assert "decisions.json" in output and "rewritten.md" in output and "changes.json" in output
    assert "展开" in output and "保持" in output
    # 动作计数与实现范围从枚举现算（原来硬编码 expand/keep，四个动作实现后必须动态化）
    assert "决策：6 段（展开 1 / 压缩 0 / 保持 5 / 配图 0）" in output, output
    assert "动作实现范围：展开 / 压缩 / 保持 / 配图" in output, output
    assert "枚举内的未实现动作" not in output, "四个动作都实现了，不该再打那句未实现动作的话"
    assert "无画像" in output
    assert "tokens" in output

    # 产物真的落盘，而且主栏要显示的正文就是改写稿（不是原文）
    target = rewrite_env.target
    decisions_payload = json.loads((target / "decisions.json").read_text(encoding="utf-8"))
    assert len(decisions_payload["decisions"]) == 6
    assert decisions_payload["meta"]["implemented_actions"] == ["expand", "compress", "keep", "illustrate"]
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
        ids = prompt_ids(messages[-1]["content"])
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


# ============================================================ 局部重跑（§6） ==
def _prompt_ids(calls: list) -> list[str]:
    """把每次调用收到的段 id 抠出来（提示词里的 ``[pNNN] `` 只出现在段落块里）。"""
    ids: list[str] = []
    for call in calls:
        ids.extend(prompt_ids(call[-1]["content"]))
    return ids


def _blocks_of(dirs: SimpleNamespace, source_id: str = "fake-doc") -> list[str]:
    return split_rewritten_blocks((dirs.data / source_id / "rewritten.md").read_text(encoding="utf-8"))


def _artifacts(dirs: SimpleNamespace, source_id: str = "fake-doc") -> dict[str, bytes]:
    target = dirs.data / source_id
    return {name: (target / name).read_bytes() for name in ("decisions.json", "changes.json", "rewritten.md")}


def _read_payload(dirs: SimpleNamespace, name: str, source_id: str = "fake-doc") -> dict:
    return json.loads((dirs.data / source_id / name).read_text(encoding="utf-8"))


def _write_payload(dirs: SimpleNamespace, name: str, payload: dict, source_id: str = "fake-doc") -> None:
    (dirs.data / source_id / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_rerun_only_touches_the_target_paragraphs(dirs: SimpleNamespace, fake_chat_client) -> None:
    """``--only``：只对目标段发请求、只替换目标段，其余段落逐字节不变（§6.2）。"""
    doc = write_fake_doc(dirs)
    fake_run(dirs, fake_chat_client(handler=fake_handler_for(doc)))
    first_meta = _read_payload(dirs, "decisions.json")["meta"]
    before_blocks = _blocks_of(dirs)
    order = [p["id"] for p in doc["paragraphs"]]

    client = fake_chat_client(handler=fake_handler_for(doc))
    targets = resolve_rerun_targets("fake-doc", dirs.data, dirs.profiles, "local-default", only="p002,p006")
    assert targets.reason == "only" and targets.paragraphs == ["p002", "p006"]
    outcome = run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", client, targets=targets)

    # ① 假模型只收到目标段，一个批次（段号仍是全篇序号：交集的首末下标 2–6）
    assert len(client.calls) == 1
    assert sorted(_prompt_ids(client.calls)) == ["p002", "p006"]
    assert "本次给你第 2–6 段" in client.calls[0][-1]["content"]

    # ② 未重跑段逐字节不变；目标段按本次结果落盘
    blocks = _blocks_of(dirs)
    for pid in ("p001", "p003", "p004", "p005", "p007"):
        assert blocks[order.index(pid)] == before_blocks[order.index(pid)], pid
    assert blocks[order.index("p006")] == FAKE_COMPRESSED
    assert blocks[order.index("p005")] == before_blocks[order.index("p005")]

    # ③ fidelity 重置为未校验（绝不许把上次的 checked: true 带过来）
    changes = _read_payload(dirs, "changes.json")
    assert changes["fidelity"]["checked"] is False
    assert changes["fidelity"]["counts"] is None
    assert changes["fidelity"]["mismatches"] == []
    assert changes["fidelity"]["summary"].startswith("尚未校验")

    # ④ 段序不变、条数不变、动作与变更记录一一对应
    decisions = _read_payload(dirs, "decisions.json")["decisions"]
    assert [item["paragraph"] for item in decisions] == order
    assert [item["paragraph"] for item in changes["changes"]] == order
    assert changes["changes"][order.index("p006")]["action"] == "compress"

    # ⑤ meta.rerun / rerun_history 的形状
    meta = _read_payload(dirs, "decisions.json")["meta"]
    rerun = meta["rerun"]
    assert set(rerun) == {
        "at",
        "reason",
        "requested",
        "paragraphs",
        "batches",
        "previous_generated_at",
    }
    assert rerun["reason"] == "only"
    assert rerun["requested"] == {"only": ["p002", "p006"], "retry_failed": False, "from_signals": False}
    assert rerun["paragraphs"] == ["p002", "p006"]
    assert rerun["batches"] == 1
    assert rerun["previous_generated_at"] == first_meta["generated_at"]
    assert meta["rerun"] == _read_payload(dirs, "changes.json")["meta"]["rerun"], "两份产物的重跑留痕要一致"
    history = meta["rerun_history"]
    assert len(history) == 1
    assert history[0]["usage"]["requests"] == 1, "账记本次"
    assert history[0]["retry_account"] == {"tool": "rewrite_batch", "requests": 1, "retries": 0, "codes": []}
    # ⑥ 账记本次；计数按合并后全量重算
    assert meta["usage"]["requests"] == 1 and meta["batch_usage"][0]["batch"] == "第 2–6 段"
    assert meta["action_counts"] == {"expand": 1, "compress": 1, "keep": 4, "illustrate": 1}
    assert meta["paragraph_count"] == len(order)
    # 局部重跑路径同样要在 changes.json 的 meta 里写上段数（F5：整篇与局部两条路径口径一致）
    assert _read_payload(dirs, "changes.json")["meta"]["paragraph_count"] == len(order)
    assert outcome.rerun_targets == ["p002", "p006"]
    assert outcome.batch_count == 1


def test_rerun_appends_one_history_entry_per_run(dirs: SimpleNamespace, fake_chat_client) -> None:
    doc = write_fake_doc(dirs)
    fake_run(dirs, fake_chat_client(handler=fake_handler_for(doc)))
    for pid in ("p002", "p003"):
        client = fake_chat_client(handler=fake_handler_for(doc))
        targets = resolve_rerun_targets("fake-doc", dirs.data, dirs.profiles, "local-default", only=pid)
        run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", client, targets=targets)
    meta = _read_payload(dirs, "decisions.json")["meta"]
    assert [item["paragraphs"] for item in meta["rerun_history"]] == [["p002"], ["p003"]]
    assert meta["rerun"]["paragraphs"] == ["p003"], "meta.rerun 记的是最近一次"

def test_rerun_keeps_the_scheduling_fields_in_the_merged_meta(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """``planning_mode`` / ``concurrency`` / ``validation_retries`` 不许被局部重跑抹掉（复核第 9 条）。

    这三格原先在合并器重建 meta 时整体消失，实验产物因此判读不出「这一篇是逐段跑出来的」。
    """
    doc = write_fake_doc(dirs)
    run_rewrite(
        "fake-doc",
        dirs.data,
        dirs.profiles,
        "local-default",
        fake_chat_client(handler=fake_handler_for(doc)),
        per_paragraph=True,
        concurrency=1,
        validation_retries=2,
    )
    targets = resolve_rerun_targets("fake-doc", dirs.data, dirs.profiles, "local-default", only="p002")
    run_rewrite(
        "fake-doc",
        dirs.data,
        dirs.profiles,
        "local-default",
        fake_chat_client(handler=fake_handler_for(doc)),
        targets=targets,
        per_paragraph=True,
        concurrency=1,
        validation_retries=2,
    )
    for name in ("decisions.json", "changes.json"):
        meta = _read_payload(dirs, name)["meta"]
        assert meta["planning_mode"] == "per-paragraph", name
        assert meta["concurrency"] == 1, name
        assert meta["validation_retries"] == 2, name


def test_merge_artifacts_replaces_one_paragraph_in_place(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """合并器单独用（在线压缩也走同一个函数）：逐段替换、段序不变、fidelity 重置、账记本次。"""
    doc = write_fake_doc(dirs)
    fake_run(dirs, fake_chat_client(handler=fake_handler_for(doc)))
    order = [p["id"] for p in doc["paragraphs"]]
    before_blocks = _blocks_of(dirs)
    base = load_merge_base("fake-doc", dirs.data)
    assert isinstance(base, MergeBase) and len(base.blocks) == len(order)

    compressed = "只剩一句。"
    decision = Decision(
        paragraph="p003",
        action="compress",
        why={"profile": [], "note": "手写的压缩决策"},
        confidence=0.5,
        rewrite=compressed,
        requested_action="compress",
    )
    _, new_changes = rewrite(
        {"paragraphs": [p for p in doc["paragraphs"] if p["id"] == "p003"]}, [decision], {}
    )
    merged = merge_artifacts(
        "fake-doc",
        dirs.data,
        base=base,
        updates={"p003": ParagraphUpdate(decision=decision, change=new_changes[0], text=compressed)},
        run_meta={
            "model": "fake-model",
            "usage": {"requests": 1, "prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            "batch_usage": [],
            "attempts": [],
            "retry_account": {"tool": "compress_paragraph", "requests": 1, "retries": 0, "codes": []},
            "request_count": 1,
            "batch_count": 1,
        },
        degraded=["本次只合并一段"],
        rerun=None,
    )

    assert merged.blocks[order.index("p003")] == compressed
    for pid in ("p001", "p002", "p004", "p005", "p006", "p007"):
        assert merged.blocks[order.index(pid)] == before_blocks[order.index(pid)], pid
    assert merged.markdown == "\n\n".join(merged.blocks) + "\n"
    assert merged.fidelity["checked"] is False and merged.fidelity["counts"] is None
    assert merged.action_counts == {"expand": 1, "compress": 2, "keep": 3, "illustrate": 1}
    assert merged.decisions_meta["usage"]["requests"] == 1, "账记本次"
    assert "rerun" not in merged.decisions_meta, "rerun=None 时不追加那一栏"
    # 本次的说明在前；旧产物里仍成立的降级条目继续留着（p003 自己的旧条目已被替换掉）
    assert merged.decisions_meta["degraded"][0] == "本次只合并一段"
    assert any("无画像模式" in item for item in merged.decisions_meta["degraded"])
    assert not any(item.startswith("p003：") for item in merged.decisions_meta["degraded"])
    # 落盘结果与返回值一致（三份产物都被写回）
    assert _blocks_of(dirs) == merged.blocks
    assert _read_payload(dirs, "changes.json")["fidelity"]["checked"] is False
    assert _read_payload(dirs, "decisions.json")["meta"]["usage"]["requests"] == 1


def test_rerun_requires_the_three_artifacts_before_any_model_call(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """三份产物缺一份 → 退出码 2 的语义，且**早于**任何模型调用（不许白花钱、不许半合并）。"""
    doc = write_fake_doc(dirs)
    client = fake_chat_client(handler=fake_handler_for(doc))
    targets = RerunTargets(reason="only", paragraphs=["p002"], requested={"only": ["p002"]})
    with pytest.raises(InputError) as excinfo:
        run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", client, targets=targets)
    assert "decisions.json" in str(excinfo.value) and "ar rewrite" in str(excinfo.value)
    assert client.calls == [], "前置校验必须在任何模型调用之前"


def test_rerun_refuses_when_the_records_do_not_match_the_material(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    doc = write_fake_doc(dirs)
    fake_run(dirs, fake_chat_client(handler=fake_handler_for(doc)))
    payload = _read_payload(dirs, "changes.json")
    payload["changes"] = payload["changes"][:-1]
    _write_payload(dirs, "changes.json", payload)

    client = fake_chat_client(handler=fake_handler_for(doc))
    targets = RerunTargets(reason="only", paragraphs=["p002"], requested={"only": ["p002"]})
    with pytest.raises(InputError) as excinfo:
        run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", client, targets=targets)
    assert "不许半合并" in str(excinfo.value)
    assert client.calls == []
    # 产物原封不动（没被写坏）
    assert len(_read_payload(dirs, "changes.json")["changes"]) == len(doc["paragraphs"]) - 1


def test_merge_keeps_historical_failures_and_clears_the_retried_ones(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """``model_errors`` / ``unplanned_paragraphs`` 按合并后的真实状态重算（§6.2 第 8 条）。"""
    doc = write_fake_doc(dirs)
    fake_run(dirs, fake_chat_client(handler=fake_handler_for(doc)))
    before_blocks = _blocks_of(dirs)
    payload = _read_payload(dirs, "decisions.json")
    payload["meta"]["model_errors"] = [
        {"batch": "第 1–3 段", "paragraphs": ["p001", "p002", "p003"], "error": "注入的历史失败"}
    ]
    payload["meta"]["unplanned_paragraphs"] = ["p001", "p004"]
    _write_payload(dirs, "decisions.json", payload)

    def failing(messages):  # noqa: ANN001
        raise RuntimeError("这次重跑也失败了（回归用例假模型）")

    client = fake_chat_client(handler=failing)
    targets = resolve_rerun_targets("fake-doc", dirs.data, dirs.profiles, "local-default", only="p002")
    outcome = run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", client, targets=targets)

    meta = _read_payload(dirs, "decisions.json")["meta"]
    # 与目标段重叠的历史条目拆成「仍失败的段」；目标段自己的旧条目被清掉；本次的失败记回来
    assert [item["paragraphs"] for item in meta["model_errors"]] == [["p001", "p003"], ["p002"]]
    assert meta["unplanned_paragraphs"] == ["p001", "p002", "p004"]
    assert meta["rewrite_incomplete"] is True and outcome.incomplete is True
    assert any(item.startswith("改写不完整：") for item in meta["degraded"])
    assert _blocks_of(dirs) == before_blocks, "失败的重跑不许把正文改坏"


def test_retry_failed_takes_the_union_in_document_order(dirs: SimpleNamespace, fake_chat_client) -> None:
    doc = write_fake_doc(dirs)
    fake_run(dirs, fake_chat_client(handler=fake_handler_for(doc)))
    payload = _read_payload(dirs, "decisions.json")
    payload["meta"]["model_errors"] = [
        {"batch": "第 1–2 段", "paragraphs": ["p002", "p001"], "error": "注入"},
        {"batch": "第 7 段", "paragraphs": [], "error": "注入（段落未知）"},
    ]
    payload["meta"]["unplanned_paragraphs"] = ["p003", "p001", "p099"]
    _write_payload(dirs, "decisions.json", payload)

    targets = resolve_rerun_targets("fake-doc", dirs.data, dirs.profiles, "local-default", retry_failed=True)
    # 并集 + 去重 + 按全篇段序；已不在材料里的 p099 被跳过并留下可见说明
    assert targets.paragraphs == ["p001", "p002", "p003"]
    assert targets.reason == "retry-failed"
    assert any("p099" in note for note in targets.degraded)


def test_from_signals_targets_only_the_paragraphs_of_changed_concepts(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """``--from-signals`` 走 ``affected_paragraphs``：只认状态真的变了的概念，且**不写画像**。"""
    doc = write_fake_doc(dirs)
    fake_run(dirs, fake_chat_client(handler=fake_handler_for(doc)))
    profile_file = _signals_setup(dirs)
    before_profile = profile_file.read_bytes()

    targets = resolve_rerun_targets("fake-doc", dirs.data, dirs.profiles, "local-default", from_signals=True)
    assert targets.reason == "from-signals"
    assert targets.paragraphs == ["p002", "p005"]  # c01 覆盖的两个段
    assert profile_file.read_bytes() == before_profile, "--from-signals 不许写 profile.yaml"
    assert any("未写盘" in note for note in targets.degraded)

    client = fake_chat_client(handler=fake_handler_for(doc))
    outcome = run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", client, targets=targets)
    assert sorted(_prompt_ids(client.calls)) == ["p002", "p005"]
    assert _read_payload(dirs, "decisions.json")["meta"]["rerun"]["reason"] == "from-signals"
    assert outcome.incomplete is False
    assert profile_file.read_bytes() == before_profile


def test_from_signals_is_empty_when_no_concept_state_changed(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """未达阈值（只累加 score / evidence）的概念不触发重跑：目标段集为空。"""
    doc = write_fake_doc(dirs)
    fake_run(dirs, fake_chat_client(handler=fake_handler_for(doc)))
    _signals_setup(dirs, state="unknown")  # 已经是最不知道的一档：负信号推不动 state

    targets = resolve_rerun_targets("fake-doc", dirs.data, dirs.profiles, "local-default", from_signals=True)
    assert targets.empty and targets.paragraphs == []
    assert any("没有任何段受影响" in note for note in targets.degraded)


def _signals_setup(dirs: SimpleNamespace, *, state: str = "known") -> Path:
    """画像（c01 = ``state``）+ 两条求助事件（p002 / p005，同属 c01）。

    两条 ``explain`` ⇒ score −2.0、同向证据 2 条 ⇒ 过阈值；``state="known"`` 时降到 ``partial``，
    ``state="unknown"`` 时已经到底、只能累加。
    """
    from ar.events import events_path
    from ar.profile import save_profile

    save_profile(
        dirs.profiles,
        "local-default",
        {
            "reader": "local-default",
            "background": {},
            "concepts": {"c01": {"state": state, "confidence": 0.9}},
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
        }
        for pid in ("p002", "p005")
    ]
    path = events_path(dirs.profiles, "local-default")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events), encoding="utf-8"
    )
    from ar.paths import profile_path

    return profile_path(dirs.profiles, "local-default")


def test_rerun_resets_fidelity_after_a_passing_check(
    run_cli, rewrite_env: SimpleNamespace, fake_chat_client, sample_fake_handler
) -> None:
    """负向对照（契约 §11.2）：``check`` 把 ``checked`` 写成 true 后，局部重跑必须重置为 false。"""
    client = fake_chat_client(handler=sample_fake_handler)
    code, output = run_cli_with_fake(run_cli, rewrite_env, client, "rewrite", "sample")
    assert code == EXIT_OK, output
    code, output = run_cli_with_fake(run_cli, rewrite_env, client, "check", "sample")
    assert code == EXIT_OK, output
    before_blocks = _blocks_of(rewrite_env.dirs, "sample")
    assert json.loads((rewrite_env.target / "changes.json").read_text(encoding="utf-8"))["fidelity"]["checked"] is True

    code, output = run_cli_with_fake(run_cli, rewrite_env, client, "rewrite", "sample", "--only", "p002")
    assert code == EXIT_OK, output
    assert "局部重跑" in output and "p002" in output
    fidelity = json.loads((rewrite_env.target / "changes.json").read_text(encoding="utf-8"))["fidelity"]
    assert fidelity["checked"] is False, "重跑后把上次的 checked: true 带过来了"
    assert fidelity["counts"] is None and fidelity["mismatches"] == []
    assert _blocks_of(rewrite_env.dirs, "sample") == before_blocks  # 同一次假模型 → 正文不变


def test_cli_rerun_flags_are_mutually_exclusive(run_cli, rewrite_env: SimpleNamespace) -> None:
    """两两或三个全给 → 退出码 2 + 明确文案（§6.1）。"""
    combinations = (
        ("--only", "p002", "--retry-failed"),
        ("--only", "p002", "--from-signals"),
        ("--retry-failed", "--from-signals"),
        ("--only", "p002", "--retry-failed", "--from-signals"),
    )
    for flags in combinations:
        code, output = run_cli(
            "--data-dir", str(rewrite_env.dirs.data), "rewrite", "sample", *flags
        )
        assert code == EXIT_USAGE, (flags, output)
        assert "互斥" in output, (flags, output)


def test_cli_only_rejects_bad_paragraph_ids(run_cli, rewrite_env: SimpleNamespace) -> None:
    code, output = run_cli(
        "--data-dir", str(rewrite_env.dirs.data), "rewrite", "sample", "--only", "p099"
    )
    assert code == EXIT_USAGE, output
    assert "p099" in output and "不存在" in output

    code, output = run_cli(
        "--data-dir", str(rewrite_env.dirs.data), "rewrite", "sample", "--only", "banana"
    )
    assert code == EXIT_USAGE, output
    assert "段 id" in output


def test_cli_retry_failed_with_nothing_to_do_writes_nothing(
    run_cli, rewrite_env: SimpleNamespace, fake_chat_client, sample_fake_handler, nokey_config: Path
) -> None:
    """``--retry-failed`` 目标为空：打印 + 退出码 0 + **不写盘、不调模型**（连配置都不需要）。"""
    client = fake_chat_client(handler=sample_fake_handler)
    code, output = run_cli_with_fake(run_cli, rewrite_env, client, "rewrite", "sample")
    assert code == EXIT_OK, output
    before = _artifacts(rewrite_env.dirs, "sample")
    calls_before = len(client.calls)

    # 显式给一份「未配置模型」的配置：目标为空时不该走到模型门禁（退出码 0 而不是 5）
    code, output = run_cli(
        "--data-dir", str(rewrite_env.dirs.data), "--config", str(nokey_config),
        "rewrite", "sample", "--retry-failed",
    )
    assert code == EXIT_OK, output
    assert "没有需要重跑的段" in output
    assert "没有写盘" in output
    assert _artifacts(rewrite_env.dirs, "sample") == before, "空目标段却写了盘"
    assert len(client.calls) == calls_before


def test_cli_retry_failed_clears_the_failures_and_the_exit_code_returns_to_zero(
    run_cli, rewrite_env: SimpleNamespace, fake_chat_client, sample_fake_handler
) -> None:
    """退出码 6 只反映**合并后**的真实状态：把失败段补齐后回到 0（§6.2 第 8 条）。"""

    def failing(messages):  # noqa: ANN001
        if "[p001] " in batch_text(messages[-1]["content"]):
            raise RuntimeError("这一批的网关抖了（回归用例假模型）")
        return sample_fake_handler(messages)

    code, output = run_cli_with_fake(
        run_cli, rewrite_env, fake_chat_client(handler=failing),
        "rewrite", "sample", "--max-chars-per-batch", "40",
    )
    assert code == EXIT_MODEL_PARTIAL, output
    meta = json.loads((rewrite_env.target / "decisions.json").read_text(encoding="utf-8"))["meta"]
    failed = sorted(
        {pid for item in meta["model_errors"] for pid in item["paragraphs"]} | set(meta["unplanned_paragraphs"])
    )
    assert failed, "这个用例需要真实的失败现场"
    assert meta["rewrite_incomplete"] is True

    client = fake_chat_client(handler=sample_fake_handler)
    code, output = run_cli_with_fake(
        run_cli, rewrite_env, client, "rewrite", "sample", "--retry-failed", "--max-chars-per-batch", "40"
    )
    assert code == EXIT_OK, output
    assert "局部重跑" in output and "改写完成" in output
    assert sorted(set(_prompt_ids(client.calls))) == failed, "重跑的段集不等于上次失败的段集"

    meta = json.loads((rewrite_env.target / "decisions.json").read_text(encoding="utf-8"))["meta"]
    assert meta["model_errors"] == [] and meta["unplanned_paragraphs"] == []
    assert meta["rewrite_incomplete"] is False
    assert meta["rerun"]["reason"] == "retry-failed"
    # 补齐之后，旧产物里那条「有段落没被改写」的降级说明**不许留在产物里**（它已经不成立了）
    assert not any(item.startswith("改写不完整：") for item in meta["degraded"]), meta["degraded"]


    # 补齐后 check 也必须回到 0（产物里的口径与退出码一致）
    code, output = run_cli_with_fake(run_cli, rewrite_env, client, "check", "sample")
    assert code == EXIT_OK, output

# ------------------------------------------------------------ 逐段循环（v7） --
def test_rewrite_with_internal_blank_lines_stays_one_block(fake_chat_client, dirs: SimpleNamespace) -> None:
    """改写真身带空行时并成同一段：rewritten.md 的块数必须与原文一致（2026-10-18 实测踩到）。"""
    write_fake_doc(dirs)
    doc = build_doc(FAKE_SOURCE)

    def handler(messages):
        pid = prompt_ids(messages[-1]["content"])[0]
        text = doc["paragraphs"][1]["text"] + "\n\n这里另起一段补一句解释。"
        return json.dumps(
            {"decisions": [{"paragraph": pid, "action": "expand", "why": {"profile": [], "note": "补解释"}, "confidence": 0.8, "rewrite": text}]},
            ensure_ascii=False,
        )

    outcome = run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", fake_chat_client(handler=handler))
    blocks = split_rewritten_blocks(outcome.rewritten_markdown)
    assert len(blocks) == len(doc["paragraphs"]), "段内空行把改写稿切成了多段"
    assert any("改写正文里有空行" in note for note in outcome.degraded)


def test_full_text_sits_after_the_rules_and_before_the_batch() -> None:
    """全文块（rewrite-v7）在规则之后、本次段号之前；缺省时零字符插入。"""
    full = "[p001] 甲\n\n[p002] 乙"
    user = _rewrite_user(full_text_block=full)
    assert user.index("规则：") < user.index("原文（全文") < user.index("本次给你第")
    assert user.count("原文（全文") == 1
    assert "[p001] 甲" in user and "[p002] 乙" in user
    assert "原文（全文" not in _rewrite_user()


def test_feedback_is_appended_at_the_very_end_and_empty_adds_nothing() -> None:
    """校验回灌只动尾巴：空串与缺省逐字相同，非空时紧跟在正文之后。"""
    base = _rewrite_user(full_text_block="[p001] 甲")
    assert _rewrite_user(full_text_block="[p001] 甲", feedback="") == base
    with_feedback = _rewrite_user(full_text_block="[p001] 甲", feedback="【校验反馈】少了数字 1696")
    assert with_feedback.startswith(base)
    assert with_feedback.rstrip().endswith("少了数字 1696")
    assert "【校验反馈】" in with_feedback


def test_per_paragraph_serial_sends_one_request_per_paragraph_in_order(fake_chat_client) -> None:
    """逐段串行：一次一段、按段序发；batch_usage 每段一条。"""
    doc = build_doc(FAKE_SOURCE)
    order: list[str] = []

    def handler(messages):
        ids = prompt_ids(messages[-1]["content"])
        assert len(ids) == 1, "逐段模式一次只能问一段"
        order.append(ids[0])
        return json.dumps(
            {"decisions": [{"paragraph": ids[0], "action": "keep", "why": {"profile": [], "note": "逐段假依据"}, "confidence": 0.8}]},
            ensure_ascii=False,
        )

    decisions, _degraded, meta = planDecisions(
        doc, None, {"reader": "tester"}, fake_chat_client(handler=handler),
        source_id="fake-doc", per_paragraph=True, concurrency=1,
    )
    assert order == [p["id"] for p in doc["paragraphs"]]
    assert [d.paragraph for d in decisions] == [p["id"] for p in doc["paragraphs"]]
    assert meta["planning_mode"] == "per-paragraph"
    assert meta["concurrency"] == 1
    assert len(meta["batch_usage"]) == len(doc["paragraphs"])
    assert [entry["batch"] for entry in meta["batch_usage"]] == [
        f"第 {i}–{i} 段" for i in range(1, len(doc["paragraphs"]) + 1)
    ]
    assert meta["usage"]["requests"] == len(doc["paragraphs"])


def test_per_paragraph_parallel_actually_overlaps(fake_chat_client) -> None:
    """逐段并发：并发上限 >1 时请求真的重叠（峰值 >1），结果仍按段序对回。"""
    doc = build_doc(FAKE_SOURCE)

    def handler(messages):
        ids = prompt_ids(messages[-1]["content"])
        return json.dumps(
            {"decisions": [{"paragraph": ids[0], "action": "keep", "why": {"profile": [], "note": "逐段"}, "confidence": 0.8}]},
            ensure_ascii=False,
        )

    client = _RecordingClient(handler=handler, delay=0.02)
    decisions, _degraded, meta = planDecisions(
        doc, None, {"reader": "tester"}, client,
        source_id="fake-doc", per_paragraph=True, concurrency=3,
    )
    assert client.max_active > 1, "并发没有生效"
    assert [d.paragraph for d in decisions] == [p["id"] for p in doc["paragraphs"]]
    assert meta["concurrency"] == 3


def test_per_paragraph_validation_retry_feeds_the_mismatch_back(fake_chat_client) -> None:
    """校验不过 → 把具体差异回灌成 user 末尾的新请求；通过后只落一段。"""
    doc = build_doc("第一段有 1696 年的数字。")
    prompts: list[str] = []

    def handler(messages):
        prompt = messages[-1]["content"]
        prompts.append(prompt)
        pid = prompt_ids(prompt)[0]
        if "【校验反馈" in prompt:
            text = "这一段有 1696 年的数字。这里补一句解释。"
            return json.dumps(
                {"decisions": [{"paragraph": pid, "action": "expand", "why": {"profile": [], "note": "补解释"}, "confidence": 0.8, "rewrite": text}]},
                ensure_ascii=False,
            )
        return json.dumps(
            {"decisions": [{"paragraph": pid, "action": "expand", "why": {"profile": [], "note": "换说法"}, "confidence": 0.8, "rewrite": "换个说法。"}]},
            ensure_ascii=False,
        )

    decisions, degraded, meta = planDecisions(
        doc, None, {"reader": "tester"}, fake_chat_client(handler=handler),
        source_id="fake-doc", per_paragraph=True, concurrency=1, validation_retries=1,
    )
    assert len(prompts) == 2, "首发 + 1 次校验重试"
    assert "【校验反馈" in prompts[1] and "1696" in prompts[1]
    assert decisions[0].action == "expand" and "1696" in decisions[0].rewrite
    # usage.requests 是「批数」（逐段模式 = 段数）；真实 HTTP 次数（含校验重试）在 retry_account
    assert meta["usage"]["requests"] == 1
    assert meta["retry_account"]["requests"] == 2
    assert len(meta["batch_usage"][0]["attempts"]) == 2
    assert any("未通过校验" in note for note in degraded)


def test_per_paragraph_exhausted_validation_keeps_source(fake_chat_client, dirs: SimpleNamespace) -> None:
    """重试用尽仍不过：这一段不写模型稿，按保持原文 + unplanned（退出码 6 的证据）。"""
    write_fake_doc(dirs)

    def handler(messages):
        pid = prompt_ids(messages[-1]["content"])[0]
        return json.dumps(
            {"decisions": [{"paragraph": pid, "action": "expand", "why": {"profile": [], "note": "换说法"}, "confidence": 0.8, "rewrite": "换个说法。"}]},
            ensure_ascii=False,
        )

    outcome = run_rewrite(
        "fake-doc", dirs.data, dirs.profiles, "local-default", fake_chat_client(handler=handler),
        per_paragraph=True, concurrency=1, validation_retries=1,
    )
    assert outcome.incomplete is True
    assert outcome.unplanned_paragraphs, "没通过校验的段必须进 unplanned"
    assert all(decision.action == "keep" for decision in outcome.decisions)
    source_blocks = [p["text"] for p in build_doc(FAKE_SOURCE)["paragraphs"]]
    assert outcome.rewritten_markdown == "\n\n".join(source_blocks) + "\n", "没过校验的稿子不许写进正文"
    meta = json.loads(outcome.decisions_path.read_text(encoding="utf-8"))["meta"]
    assert meta["planning_mode"] == "per-paragraph"
    assert meta["validation_retries"] == 1


# --------------------------------------------------------- 逐段对话（v7） --
def test_conversation_accumulates_previous_decisions_in_the_transcript(fake_chat_client) -> None:
    """同一份对话里逐段推进：上一轮的 assistant 决策 + 当前段 user 指令都在 messages 里。"""
    doc = build_doc(FAKE_SOURCE)
    calls: list[list[dict[str, str]]] = []

    def handler(messages):
        calls.append([dict(message) for message in messages])
        pid = prompt_ids(messages[-1]["content"])[0]
        return json.dumps(
            {"decisions": [{"paragraph": pid, "action": "keep", "why": {"profile": [], "note": "对话假依据"}, "confidence": 0.8}]},
            ensure_ascii=False,
        )

    decisions, _degraded, meta = planDecisions(
        doc, None, {"reader": "tester"}, fake_chat_client(handler=handler),
        source_id="fake-doc", per_paragraph=True, conversation=True, concurrency=1,
        validation_retries=0,
    )
    assert [d.paragraph for d in decisions] == [p["id"] for p in doc["paragraphs"]]
    assert meta["planning_mode"] == "conversation" and meta["concurrency"] == 1
    assert len(calls) == len(doc["paragraphs"])
    # 第 2 轮：system + 初始 user + 上一轮 assistant + 本轮 user
    assert [m["role"] for m in calls[1]] == ["system", "user", "assistant", "user"]
    assert "p001" in calls[1][2]["content"]
    assert "[p002] " in calls[1][3]["content"]
    # 每轮都追加，而不是重新发一份（prompt 字符数单调增长）
    sizes = [sum(len(m["content"]) for m in call) for call in calls]
    assert sizes == sorted(sizes) and sizes[-1] > sizes[0] + 500


def test_conversation_retry_keeps_the_bad_answer_and_feedback_in_context(fake_chat_client) -> None:
    """校验不过：上一版错误答案留在上下文里，反馈作为新一轮 user 消息追问同一段。"""
    doc = build_doc("第一段有 1696 年的数字。")
    calls: list[list[dict[str, str]]] = []

    def handler(messages):
        calls.append([dict(message) for message in messages])
        content = messages[-1]["content"]
        ids = re.findall(r"\[(p\d{3})\]", content) or re.findall(r"(p\d{3})", content)
        pid = ids[-1]
        if "【校验反馈" in content:
            text = "这一段有 1696 年的数字。这里补一句解释。"
        else:
            text = "换个说法。"
        return json.dumps(
            {"decisions": [{"paragraph": pid, "action": "expand", "why": {"profile": [], "note": "x"}, "confidence": 0.8, "rewrite": text}]},
            ensure_ascii=False,
        )

    decisions, _degraded, meta = planDecisions(
        doc, None, {"reader": "tester"}, fake_chat_client(handler=handler),
        source_id="fake-doc", per_paragraph=True, conversation=True, concurrency=1,
        validation_retries=1,
    )
    assert len(calls) == 2
    second = calls[1]
    assert second[-2]["role"] == "assistant" and "换个说法" in second[-2]["content"]
    assert second[-1]["role"] == "user" and "【校验反馈" in second[-1]["content"]
    assert decisions[0].action == "expand" and "1696" in decisions[0].rewrite
    assert meta["retry_account"]["requests"] == 2

# ------------------------------------------ 两遍式（--plan-first，plan-v1） --
#: 第一遍打标的**固定判别标记**（``ar.prompts.PLAN_USER_TAIL`` 的首行，plan-v2 起它**在尾巴里**）：
#: 第一遍的提示词没有 rewrite 的「本次给你第 N–M 段」分隔线，所以假客户端只能靠它分流——
#: 不分流的话，第一遍全文块里的 25 个 id 会被误当成「本批要处理的段」。
PLAN_MARK = "【动作打标·第一遍】"
#: 计划块插进第二遍提示词的**整段**（含它自己的空行与标题）——逐字写死在用例里，
#: 改提示词布局必须同步改这里（照 ``ILLUSTRATION_SECTION`` 的先例）。
PLAN_SECTION = (
    "\n\n第一遍的动作计划（每行「段 id 动作：一句话依据」；照这份计划执行——"
    "与计划不一致时，就在该段的 why.note 里写明为什么改主意）：\np001 keep"
)


def is_plan_prompt(prompt: str) -> bool:
    """这份提示词是第一遍打标吗（靠固定判别标记分流）。"""
    return PLAN_MARK in prompt


def plan_prompt_ids(prompt: str) -> list[str]:
    """第一遍提示词里的段 id（它一次吃全篇，全文块就是唯一来源）。"""
    return PROMPT_PARAGRAPH_RE.findall(prompt)


def plan_handler(
    actions: dict[str, str] | None = None,
    *,
    missing: tuple[str, ...] = (),
    note: str = "假计划依据",
):
    """第一遍的假模型：全篇每段一条动作；``missing`` 里的段**故意不给**（考 plan_missing）。"""

    def handler(prompt: str) -> str:
        table = dict(actions or {})
        skipped = set(missing)
        plan = [
            {"paragraph": pid, "action": table.get(pid, "keep"), "note": f"{pid} {note}"}
            for pid in plan_prompt_ids(prompt)
            if pid not in skipped
        ]
        return json.dumps({"plan": plan}, ensure_ascii=False)

    return handler


def two_pass_handler(plan, rewrite_handler):
    """按判别标记分流：第一遍拿 prompt，第二遍拿 messages（两种提示词各自的老写法都不变）。"""

    def handler(messages: list[dict[str, str]]) -> str:
        prompt = messages[-1]["content"]
        if is_plan_prompt(prompt):
            return plan(prompt)
        return rewrite_handler(messages)

    return handler


def test_plan_first_labels_every_paragraph_without_writing_any_prose(
    fake_chat_client, dirs: SimpleNamespace
) -> None:
    """第一遍：**一次调用**、每段一条动作、**不写任何正文**；``meta.plan_first`` 为 true。

    ``plan_action`` 只在计划动作与最终动作**不同**时出现在 ``changes[i]`` 上（为真才出现）。
    """
    write_fake_doc(dirs)
    doc = build_doc(FAKE_SOURCE)
    plan_prompts: list[str] = []

    def plan(prompt: str) -> str:
        plan_prompts.append(prompt)
        return plan_handler()(prompt)

    client = fake_chat_client(handler=two_pass_handler(plan, fake_handler_for(doc)))
    outcome = run_rewrite(
        "fake-doc", dirs.data, dirs.profiles, "local-default", client, plan_first=True
    )

    # ① 第一遍只发一次，吃的是全篇；它的输出里只有动作（没有任何正文）
    assert len(plan_prompts) == 1, "第一遍只该发一次请求"
    assert plan_prompt_ids(plan_prompts[0]) == [p["id"] for p in doc["paragraphs"]]
    payload = json.loads(outcome.decisions_path.read_text(encoding="utf-8"))
    meta = payload["meta"]
    assert meta["plan_first"] is True
    assert [item["paragraph"] for item in meta["plan"]] == [p["id"] for p in doc["paragraphs"]]
    assert all(set(item) == {"paragraph", "action", "note"} for item in meta["plan"]), "计划里不许有正文"
    assert meta["plan_prompt_version"] == PLAN_PROMPT_VERSION == "plan-v2"
    assert meta["plan_request_count"] == 1
    assert meta["plan_missing"] == []
    assert meta["unplanned_paragraphs"] == []
    assert outcome.plan_first is True and len(outcome.plan) == len(doc["paragraphs"])

    # ② 计划进了第二遍的固定上下文（同一份计划文本，逐字稳定）
    second_pass = [call for call in client.calls if not is_plan_prompt(call[-1]["content"])]
    assert second_pass, "第二遍的请求没有发出去"
    assert "第一遍的动作计划" in second_pass[0][-1]["content"]

    # ③ 计划与执行不一致的段才写 plan_action：p005 计划 keep、执行 expand
    changes = json.loads(outcome.changes_path.read_text(encoding="utf-8"))["changes"]
    by_id = {item["paragraph"]: item for item in changes}
    assert by_id["p005"]["action"] == "expand" and by_id["p005"]["plan_action"] == "keep"
    assert "plan_action" not in by_id["p001"], "计划与执行一致时不该多出这个键"


def test_plan_block_sits_in_the_shared_prefix_and_the_first_diff_stays_at_the_batch_header() -> None:
    """计划块落在第二批共享前缀里；两批的**首个差异字符仍然在段号位**（前缀缓存的前提）。"""
    doc = build_doc(FAKE_SOURCE)
    paragraphs = doc["paragraphs"]
    blocks = {
        "profile_block": "读者 tester 自报概念状态：c01=unknown",
        "concept_block": "c01 假概念（难度 3）",
        "full_text_block": REALISTIC_FULL_TEXT,
    }
    plan_block = "p001 keep：读者熟悉\np002 expand：补前置概念"
    user_a = _rewrite_user_contents(paragraphs, paragraphs[:3], 1, 3, plan_block=plan_block, **blocks)[1]
    user_b = _rewrite_user_contents(
        paragraphs, paragraphs[3:], 4, len(paragraphs), plan_block=plan_block, **blocks
    )[1]

    # 顺序：规则 < 全文 < 计划 < 本次段号（计划是共享前缀的一部分，不是变化块）
    assert user_a.count("第一遍的动作计划") == 1
    assert plan_block in user_a
    assert user_a.index("规则：") < user_a.index("原文（全文")
    assert user_a.index("原文（全文") < user_a.index("第一遍的动作计划") < user_a.index("本次给你第 1–3 段")

    first_diff = next(index for index, (left, right) in enumerate(zip(user_a, user_b)) if left != right)
    assert user_a[first_diff] == "1" and user_b[first_diff] == "4"
    assert user_a[first_diff - len("本次给你第 "):first_diff] == "本次给你第 "
    assert first_diff > user_a.index(plan_block), "计划块也该落在共享前缀里"


def test_empty_plan_block_adds_no_characters_and_keeps_the_prompt_verbatim() -> None:
    """``plan_block`` 为空（或缺省）时提示词**逐字不变**：``rewrite-v7`` 因此不用升版。

    照「空块零字符插入」的先例：空串 == 缺省；有块与无块的差异**只在那一格**。
    """
    base = _rewrite_user(full_text_block="[p001] 甲")
    assert _rewrite_user(full_text_block="[p001] 甲", plan_block="") == base, "空块与不传形参必须逐字相同"
    assert "第一遍的动作计划" not in base

    with_plan = _rewrite_user(full_text_block="[p001] 甲", plan_block="p001 keep")
    assert with_plan.count(PLAN_SECTION) == 1
    assert with_plan.replace(PLAN_SECTION, "") == base, "计划块不是「只在那一格插入」——别处也被改了"
    # 逐字稳定：同参数两次渲染完全相同，且不含时间戳 / 随机量
    assert _rewrite_user(full_text_block="[p001] 甲", plan_block="p001 keep") == with_plan
    assert "generated_at" not in with_plan

def test_plan_block_flattens_each_note_onto_one_line() -> None:
    """计划块的格式契约：**一行一段**「段 id 动作：依据」——note 里的换行不许把这个块劈开。

    否则第二遍的模型会把多行依据的后半截当成另一段的计划（复核第 8 条）。
    """
    entries = [
        PlanEntry("p001", "expand", "第一行\n第二行"),
        PlanEntry("p002", "keep", "  多个   空格\t制表  "),
        PlanEntry("p003", "compress", ""),
    ]
    block = _plan_block(entries)
    assert block == "p001 expand：第一行 第二行\np002 keep：多个 空格 制表\np003 compress"
    assert len(block.splitlines()) == 3, "一段一行，换行 / 连续空白都被压平"


def test_illegal_plan_action_falls_back_to_keep_and_says_so(
    fake_chat_client, dirs: SimpleNamespace
) -> None:
    """计划里的越界动作（``frobnicate``）按 keep 处理，并留下**可见降级**；条目一个不丢。"""
    write_fake_doc(dirs)
    doc = build_doc(FAKE_SOURCE)

    def plan(prompt: str) -> str:
        return json.dumps(
            {
                "plan": [
                    {
                        "paragraph": pid,
                        "action": "frobnicate" if pid == "p001" else "keep",
                        "note": f"{pid} 假依据",
                    }
                    for pid in plan_prompt_ids(prompt)
                ]
            },
            ensure_ascii=False,
        )

    client = fake_chat_client(handler=two_pass_handler(plan, fake_handler_for(doc)))
    outcome = run_rewrite(
        "fake-doc", dirs.data, dirs.profiles, "local-default", client, plan_first=True
    )

    meta = json.loads(outcome.decisions_path.read_text(encoding="utf-8"))["meta"]
    by_id = {item["paragraph"]: item for item in meta["plan"]}
    assert by_id["p001"]["action"] == "keep", "越界动作必须回落 keep"
    assert len(meta["plan"]) == len(doc["paragraphs"]), "降级只改动作，条目不许丢"
    assert any("p001" in note and "frobnicate" in note for note in meta["degraded"]), meta["degraded"]
    # 计划里记的是 keep ⇒ 与最终动作一致时不写 plan_action（越界取值不会漏进产物）
    changes = json.loads(outcome.changes_path.read_text(encoding="utf-8"))["changes"]
    assert "plan_action" not in changes[0]


def test_plan_missing_is_not_unplanned_and_keeps_the_exit_code(
    run_cli, rewrite_env: SimpleNamespace, fake_chat_client, sample_fake_handler
) -> None:
    """第一遍漏段只进 ``meta.plan_missing``：**不算 unplanned、不改退出码**（CLI 仍 0）。"""
    plan_prompts: list[str] = []

    def plan(prompt: str) -> str:
        plan_prompts.append(prompt)
        ids = plan_prompt_ids(prompt)
        assert ids, "第一遍的提示词里应该有全篇段 id"
        return plan_handler(missing=(ids[-1],))(prompt)

    client = fake_chat_client(handler=two_pass_handler(plan, sample_fake_handler))
    code, output = run_cli_with_fake(
        run_cli, rewrite_env, client, "rewrite", "sample", "--reader", "local-default", "--plan-first"
    )

    assert code == EXIT_OK, output
    assert len(plan_prompts) == 1
    meta = json.loads((rewrite_env.target / "decisions.json").read_text(encoding="utf-8"))["meta"]
    assert meta["plan_missing"], "漏掉的那一段要看得见"
    missing = meta["plan_missing"][0]
    assert missing not in meta["unplanned_paragraphs"], "plan_missing 不是 unplanned（否则会误报退出码 6）"
    assert meta["rewrite_incomplete"] is False
    assert meta["plan_first"] is True
    # 命令行：正常「改写完成」+ 一条点名漏段的告警，不是「改写不完整」
    assert "改写不完整" not in output
    assert "第一遍打标：" in output, output
    assert missing in output


def test_plan_first_rerun_with_only_reuses_the_stored_plan(
    run_cli, rewrite_env: SimpleNamespace, fake_chat_client, sample_fake_handler
) -> None:
    """``--plan-first --only`` 重跑**复用旧产物里的计划**：不新增第一遍调用，计划原样保留。"""
    plan_prompts: list[str] = []

    def plan(prompt: str) -> str:
        plan_prompts.append(prompt)
        return plan_handler()(prompt)

    client = fake_chat_client(handler=two_pass_handler(plan, sample_fake_handler))
    code, output = run_cli_with_fake(
        run_cli, rewrite_env, client, "rewrite", "sample", "--reader", "local-default", "--plan-first"
    )
    assert code == EXIT_OK, output
    assert len(plan_prompts) == 1
    # 命令行摘要的形状：「第一遍打标：N 段（展开 x / 压缩 y / 保持 z）；1 次请求」
    assert re.search(r"第一遍打标：\d+ 段（[^）]*）；1 次请求", output), output
    target = rewrite_env.target
    first_meta = json.loads((target / "decisions.json").read_text(encoding="utf-8"))["meta"]
    assert first_meta["plan"], "第一遍的计划应该落进产物"
    first_plan = first_meta["plan"]

    code, output = run_cli_with_fake(
        run_cli,
        rewrite_env,
        client,
        "rewrite",
        "sample",
        "--reader",
        "local-default",
        "--plan-first",
        "--only",
        "p002",
    )
    assert code == EXIT_OK, output
    assert len(plan_prompts) == 1, "局部重跑不许重新打标（复用旧产物里的计划）"
    meta = json.loads((target / "decisions.json").read_text(encoding="utf-8"))["meta"]
    assert meta["plan"] == first_plan, "复用之后计划必须原样保留"
    assert meta["plan_first"] is True
    assert any("复用既有产物里第一遍的计划" in note for note in meta["degraded"]), meta["degraded"]

def test_plan_first_rerun_without_the_flag_still_injects_the_stored_plan(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """不带 ``--plan-first`` 的局部重跑：**旧产物有计划就照旧注入**，绝不静默降级成单遍。

    现场（2026-10-19 复核第 1 条）：先 ``--plan-first`` 整篇跑，再不带该 flag 跑 ``--only``，
    目标段会被单遍改写（``changes`` 里连 ``plan_action`` 都不出现），而 ``meta.plan_first`` /
    ``meta.plan`` 原样保留 ⇒ 产物看起来「按计划跑且计划与执行一致」，降级里一个字都没有。
    口径是**一致性优先**：同一份产物始终按同一份计划执行。
    """
    doc = write_fake_doc(dirs)
    plan_prompts: list[str] = []

    def plan(prompt: str) -> str:
        plan_prompts.append(prompt)
        return plan_handler()(prompt)

    first = fake_chat_client(handler=two_pass_handler(plan, fake_handler_for(doc)))
    run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", first, plan_first=True)
    stored = _read_payload(dirs, "decisions.json")["meta"]["plan"]
    assert len(plan_prompts) == 1

    client = fake_chat_client(handler=fake_handler_for(doc))
    targets = resolve_rerun_targets("fake-doc", dirs.data, dirs.profiles, "local-default", only="p002")
    outcome = run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", client, targets=targets)

    # ① 一次打标请求都不发；目标段那一批照旧带着旧计划（p002 的计划行逐字在提示词里）
    assert len(plan_prompts) == 1, "不带 flag 的局部重跑也不许重新打标"
    assert len(client.calls) == 1
    prompt = client.calls[0][-1]["content"]
    assert "第一遍的动作计划" in prompt
    assert "p002 keep：p002 假计划依据" in prompt, prompt

    # ② 产物里那份计划没变，并且**说得清**这一轮复用了它（plan_reused = true、没有降级痕迹）
    meta = _read_payload(dirs, "decisions.json")["meta"]
    assert meta["plan"] == stored
    assert meta["plan_first"] is True and meta["plan_reused"] is True
    assert "plan_not_applied" not in meta
    assert outcome.plan_reused is True and outcome.plan_not_applied == []
    assert any("沿用既有产物里第一遍的计划" in note for note in meta["degraded"]), meta["degraded"]


def test_plan_not_applied_is_named_in_meta_and_on_the_command_line(
    run_cli, rewrite_env: SimpleNamespace, fake_chat_client, sample_fake_handler
) -> None:
    """旧计划里没有「这次重跑的那一段」时**不静默**：``meta.plan_not_applied`` + 命令行点名。

    同时钉住记账口径（复核第 4 条）：复用旧计划的重跑要写「0 次请求」，不许照抄旧产物那次。
    """
    plan_prompts: list[str] = []

    def plan(prompt: str) -> str:
        plan_prompts.append(prompt)
        return plan_handler(missing=("p002",))(prompt)

    client = fake_chat_client(handler=two_pass_handler(plan, sample_fake_handler))
    code, output = run_cli_with_fake(
        run_cli, rewrite_env, client, "rewrite", "sample", "--reader", "local-default", "--plan-first"
    )
    assert code == EXIT_OK, output
    assert len(plan_prompts) == 1
    first_meta = json.loads((rewrite_env.target / "decisions.json").read_text(encoding="utf-8"))["meta"]
    assert first_meta["plan_missing"] == ["p002"], "这一段本来就没有计划条目（考的是无法注入那一档）"

    code, output = run_cli_with_fake(
        run_cli,
        rewrite_env,
        client,
        "rewrite",
        "sample",
        "--reader",
        "local-default",
        "--plan-first",
        "--only",
        "p002",
    )
    assert code == EXIT_OK, output
    assert len(plan_prompts) == 1, "复用旧计划的重跑不许重新打标"
    # 记账按**实际请求数**打：0 次打标请求
    assert "复用旧计划，0 次请求" in output, output
    meta = json.loads((rewrite_env.target / "decisions.json").read_text(encoding="utf-8"))["meta"]
    assert meta["plan_not_applied"] == ["p002"], meta.get("plan_not_applied")
    assert meta["plan_reused"] is True
    assert any("没有可注入的计划" in note for note in meta["degraded"]), meta["degraded"]
    assert "没能注入既有计划" in output, output

    # 再跑一段，这次**不带** --plan-first：旧计划里有 p001 ⇒ 照旧注入，命令行也说得清；
    # 上一轮「p002 注入不了」的点名必须被清掉（不许留成假账）
    code, output = run_cli_with_fake(
        run_cli, rewrite_env, client, "rewrite", "sample", "--reader", "local-default", "--only", "p001"
    )
    assert code == EXIT_OK, output
    assert "沿用既有产物里第一遍的计划照旧注入第二遍" in output, output
    meta = json.loads((rewrite_env.target / "decisions.json").read_text(encoding="utf-8"))["meta"]
    assert meta["plan_reused"] is True
    assert "plan_not_applied" not in meta, "这一轮的目标段在旧计划里有条目 ⇒ 上一轮的点名要清掉"


def test_reuse_plan_refuses_when_the_reader_background_or_policy_differs(
    dirs: SimpleNamespace,
) -> None:
    """读者 / 背景指纹 / 事实锚口径任一对不上：**不复用**旧计划（复核第 2 条）。

    否则产物会写成「B 读者的 reader + A 读者的 plan」——错的东西看起来是对的，比多打一次标贵得多。
    """
    base = MergeBase(
        source_paragraphs=[{"id": "p001", "text": "甲"}],
        decisions_payload={
            "decisions": [],
            "meta": {
                "plan_first": True,
                "reader": "reader-a",
                "background_fingerprint": "aaaa1111",
                "fact_anchor_policy": "keep_all",
                "plan": [{"paragraph": "p001", "action": "expand", "note": "A 的依据"}],
                "plan_missing": [],
            },
        },
        changes_payload={"changes": []},
        blocks=["甲"],
        target=dirs.data / "fake-doc",
    )
    same = _reuse_plan(
        base, reader="reader-a", background_fingerprint="aaaa1111", fact_anchor_policy="keep_all"
    )
    assert same is not None and same.ok, "三项都对得上时才复用"
    assert [entry.paragraph for entry in same.entries] == ["p001"]

    for label, kwargs in (
        ("读者", {"reader": "reader-b"}),
        ("背景", {"background_fingerprint": "bbbb2222"}),
        ("事实锚", {"fact_anchor_policy": "allow_declared_drops"}),
    ):
        params = {"reader": "reader-a", "background_fingerprint": "aaaa1111", "fact_anchor_policy": "keep_all"}
        params.update(kwargs)
        reuse = _reuse_plan(base, **params)
        assert reuse is not None and not reuse.ok, label
        assert label in reuse.reason, reuse.reason


def test_switch_reader_rerun_retags_instead_of_reusing_the_old_plan(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """换读者后 ``--plan-first --only`` **重新全篇打标**，并把 ``plan_reused=False`` 写进产物。"""
    doc = write_fake_doc(dirs)
    plan_prompts: list[str] = []

    def plan(prompt: str) -> str:
        plan_prompts.append(prompt)
        return plan_handler()(prompt)

    first = fake_chat_client(handler=two_pass_handler(plan, fake_handler_for(doc)))
    run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", first, plan_first=True)
    assert len(plan_prompts) == 1

    # 两次都得用「按判别标记分流」的假客户端——否则打标请求也会被当成改写请求，计数失真
    client = fake_chat_client(handler=two_pass_handler(plan, fake_handler_for(doc)))
    targets = resolve_rerun_targets("fake-doc", dirs.data, dirs.profiles, "reader-b", only="p002")
    run_rewrite(
        "fake-doc",
        dirs.data,
        dirs.profiles,
        "reader-b",
        client,
        targets=targets,
        plan_first=True,
    )

    assert len(plan_prompts) == 2, "读者换了就不能沿用旧计划——必须重新全篇打标（多一次请求）"
    meta = _read_payload(dirs, "decisions.json")["meta"]
    assert meta["reader"] == "reader-b"
    assert meta["plan_reused"] is False
    assert any(
        "不复用" in note and "读者" in note for note in meta["degraded"]
    ), meta["degraded"]


def test_reuse_plan_degrades_out_of_range_actions_visibly(dirs: SimpleNamespace) -> None:
    """复用路径的越界动作同样**可见降级**（不许静默改成 keep；与 ``_parse_plan`` 同口径）。"""
    base = MergeBase(
        source_paragraphs=[{"id": "p001", "text": "甲"}],
        decisions_payload={
            "decisions": [],
            "meta": {
                "plan_first": True,
                "reader": "local-default",
                "background_fingerprint": "",
                "fact_anchor_policy": "keep_all",
                "plan": [{"paragraph": "p001", "action": "frobnicate", "note": "旧依据"}],
                "plan_missing": [],
            },
        },
        changes_payload={"changes": []},
        blocks=["甲"],
        target=dirs.data / "fake-doc",
    )
    reuse = _reuse_plan(
        base, reader="local-default", background_fingerprint="", fact_anchor_policy="keep_all"
    )
    assert reuse is not None and reuse.ok
    assert reuse.entries[0].action == "keep", "越界动作仍按 keep 处理"
    assert any("frobnicate" in note and "p001" in note for note in reuse.notes), reuse.notes


def test_default_run_adds_no_plan_or_digest_keys_and_no_extra_request(
    fake_chat_client, dirs: SimpleNamespace
) -> None:
    """默认路径（不带新开关）：**不新增请求**、产物不多出任何一个键（``plan_action`` 也不出现）。"""
    write_fake_doc(dirs)
    doc = build_doc(FAKE_SOURCE)
    client = fake_chat_client(handler=fake_handler_for(doc))
    outcome = run_rewrite("fake-doc", dirs.data, dirs.profiles, "local-default", client)

    new_keys = (
        "plan_first",
        "plan",
        "plan_missing",
        "plan_usage",
        "plan_request_count",
        "plan_attempts",
        "plan_retry_account",
        "plan_prompt_version",
        "conversation_digest",
    )
    decisions_payload = json.loads(outcome.decisions_path.read_text(encoding="utf-8"))
    changes_payload = json.loads(outcome.changes_path.read_text(encoding="utf-8"))
    for key in new_keys:
        assert key not in decisions_payload["meta"], key
        assert key not in changes_payload["meta"], key
    assert all("plan_action" not in item for item in changes_payload["changes"])
    assert all(not is_plan_prompt(call[-1]["content"]) for call in client.calls), "默认路径不许发第一遍的请求"
    assert outcome.plan_first is False and outcome.conversation_digest is False


# ------------------------------------- 逐段对话的上下文瘦身（--conversation-digest） --
def test_conversation_digest_keeps_the_shell_out_of_the_transcript(fake_chat_client) -> None:
    """开：上下文里只有一行摘要（没有 why / confidence 外壳）；关：与现状逐字一致。"""
    doc = build_doc(FAKE_SOURCE)
    calls: dict[str, list[list[dict[str, str]]]] = {"on": [], "off": []}

    def handler_factory(key: str):
        def handler(messages: list[dict[str, str]]) -> str:
            calls[key].append([dict(message) for message in messages])
            pid = prompt_ids(messages[-1]["content"])[0]
            action = "expand" if pid == "p002" else "keep"
            return json.dumps(
                {
                    "decisions": [
                        {
                            "paragraph": pid,
                            "action": action,
                            "why": {"profile": ["c01:unknown"], "note": "对话假依据"},
                            "confidence": 0.8,
                            "rewrite": "第二段补一句解释。" if action == "expand" else "",
                        }
                    ]
                },
                ensure_ascii=False,
            )

        return handler

    _d, _g, meta_on = planDecisions(
        doc,
        None,
        {"reader": "tester"},
        fake_chat_client(handler=handler_factory("on")),
        source_id="fake-doc",
        per_paragraph=True,
        conversation=True,
        concurrency=1,
        validation_retries=0,
        conversation_digest=True,
    )
    assert meta_on["conversation_digest"] is True
    # 第 2 轮：system + 初始 user + 上一轮 assistant（摘要）+ 本轮 user
    first_turn = calls["on"][1][2]
    assert first_turn["role"] == "assistant"
    assert first_turn["content"] == "p001 keep", "外壳（why / confidence）不许进上下文"
    assert "why" not in first_turn["content"] and "confidence" not in first_turn["content"]
    # 展开段带上改写后的正文（一行，仍是摘要形态）
    assert calls["on"][2][4]["content"] == "p002 expand：第二段补一句解释。"

    _d2, _g2, meta_off = planDecisions(
        doc,
        None,
        {"reader": "tester"},
        fake_chat_client(handler=handler_factory("off")),
        source_id="fake-doc",
        per_paragraph=True,
        conversation=True,
        concurrency=1,
        validation_retries=0,
    )
    assert "conversation_digest" not in meta_off, "关着时产物不许多个键"
    raw_turn = calls["off"][1][2]["content"]
    assert raw_turn.startswith("{") and '"why"' in raw_turn and '"confidence"' in raw_turn, (
        "关着时必须仍把上一轮的原始 JSON 塞回去（行为与现状逐字一致）"
    )


def test_cli_conversation_digest_requires_the_conversation_mode(run_cli, rewrite_env: SimpleNamespace) -> None:
    """``--conversation-digest`` 缺了 ``--conversation`` 是用法错误（不静默忽略这个开关）。"""
    code, output = run_cli(
        "--data-dir",
        str(rewrite_env.dirs.data),
        "--config",
        str(rewrite_env.config),
        "rewrite",
        "sample",
        "--conversation-digest",
    )
    assert code == EXIT_USAGE, output
    assert "--conversation-digest" in output and "--conversation" in output

def test_conversation_digest_is_rejected_before_any_request(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """``--conversation-digest`` 的组合校验必须**早于任何请求**（复核第 3 条）。

    现场：这条校验原先只在 ``planDecisions`` 里，``--plan-first --conversation-digest``（缺
    ``--conversation``）会先把**第一遍打标的钱花掉**，再以退出码 2 收场。库层要自己拦住。
    """
    doc = write_fake_doc(dirs)
    client = fake_chat_client(handler=fake_handler_for(doc))
    with pytest.raises(InputError) as excinfo:
        run_rewrite(
            "fake-doc",
            dirs.data,
            dirs.profiles,
            "local-default",
            client,
            per_paragraph=True,
            plan_first=True,
            conversation_digest=True,
        )
    assert "conversation_digest" in str(excinfo.value)
    assert client.calls == [], "校验不过时一个请求都不许发（第一遍打标也不行）"

    # 连 --per-paragraph 都没有的那一档同样要在发请求之前拦住
    client = fake_chat_client(handler=fake_handler_for(doc))
    with pytest.raises(InputError):
        run_rewrite(
            "fake-doc",
            dirs.data,
            dirs.profiles,
            "local-default",
            client,
            plan_first=True,
            conversation_digest=True,
        )
    assert client.calls == []


def test_plan_first_and_conversation_digest_can_be_combined(
    fake_chat_client, dirs: SimpleNamespace
) -> None:
    """两个开关互不冲突：``--plan-first`` + ``--conversation`` + ``--conversation-digest`` 一起跑。"""
    write_fake_doc(dirs)
    doc = build_doc(FAKE_SOURCE)

    def plan(prompt: str) -> str:
        return plan_handler()(prompt)

    client = fake_chat_client(handler=two_pass_handler(plan, fake_handler_for(doc)))
    outcome = run_rewrite(
        "fake-doc",
        dirs.data,
        dirs.profiles,
        "local-default",
        client,
        plan_first=True,
        per_paragraph=True,
        conversation=True,
        concurrency=1,
        conversation_digest=True,
        validation_retries=0,
    )

    meta = json.loads(outcome.decisions_path.read_text(encoding="utf-8"))["meta"]
    assert meta["planning_mode"] == "conversation"
    assert meta["plan_first"] is True and meta["conversation_digest"] is True
    assert len(meta["plan"]) == len(doc["paragraphs"])
    assert outcome.plan_first is True and outcome.conversation_digest is True
