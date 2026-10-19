"""``why.profile`` 的语义、可见性与兜底（2026-10-14 复核修复，提示词 ``rewrite-v6``）。

现场（2026-10-13 真机产物 ``data/capital-v1-ch01``）：25 段的 ``why.profile`` 全是空数组，
其中 7 段的 ``why.note`` 自称「画像为空 / 未提供（概念）自评」，而 4 段在同一句里又引用了背景。
根因在 ``ar.rewrite._format_profile_block`` 的「空自评」分支——它把「画像为空」与
「请只依据概念难度提示判断，并在 why.profile 里写空数组 []」一起塞进了提示词：
模型照着写，于是既产出了空数组、又复读了「画像为空」。

本文件守五件事（对应用户拍板的两条修法）：

1. **措辞**：那段「空自评」文案如实说「读者没有提供任何概念自评」，一个字都不提「画像为空」
   （背景非空即算「有画像」，见契约 §6.4），也不再指示模型写空数组（那件事归规则 11）；
2. **保留**：模型给了引用就逐条保留（提示词规则 11 + ``ar.tools._DECISION`` 的字段语义同口径）；
3. **不静默**：模型漏键 / 给错形状 / 该给引用却给空数组 ⇒ 逐段可见降级 + 照读者自评兜底补齐；
4. **不误报**：读者本来就没有这些概念的自评时，空数组是合法结果（不补齐、不报降级）；
5. **结构化标记**：补齐了的段在 ``changes[i].profile_refs_filled`` 上留一个**布尔字段**
   （照 ``figure`` / ``deferred`` 的「为真才出现」先例）——页面那枚「兜底补齐」徽标只认它，
   **不许拿正则去猜 ``why.note`` 的文案**（两条写盘路径都要带上）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from ar.fidelity import DEFAULT_FACT_ANCHOR_POLICY
from ar.ingest import ingest
from ar.jsonio import read_json
from ar.profile import default_profile, save_profile
from ar.rewrite import (
    _format_profile_block,
    _self_rating_refs,
    _self_rating_states,
    resolve_rerun_targets,
    run_rewrite,
)
from ar.survey import has_background, has_concept_signals
from ar.tools import get as get_tool
from test_rewrite import fake_run, prompt_ids, write_fake_doc

READER = "local-default"
SOURCE_ID = "fake-doc"
#: 假概念清单里 c01 出现在这两段（``test_rewrite.write_fake_doc``）
C01_PARAGRAPHS = ("p002", "p005")

#: 「模型没给 why.profile 这个键」的哨兵（``None`` 也是一个合法取值：显式 null）
_MISSING = object()


def refs_handler(profile_value: Any = _MISSING, *, note: str = "假依据") -> Callable[[Any], str]:
    """按提示词里的段 id 造一批决策；``why`` 里放什么由 ``profile_value`` 决定。

    ``profile_value`` 是 ``_MISSING`` 时**整个 ``profile`` 键都不给**（模型漏键的现场），
    否则原样放进 ``why.profile``（空数组 / 字符串 / 带垃圾项的数组都能造出来）。
    """

    def handler(messages: list[dict[str, str]]) -> str:
        decisions = []
        for pid in prompt_ids(messages[-1]["content"]):
            why: dict[str, Any] = {"note": f"{pid} 的{note}"}
            if profile_value is not _MISSING:
                why["profile"] = profile_value
            decisions.append(
                {"paragraph": pid, "action": "keep", "why": why, "confidence": 0.8, "rewrite": ""}
            )
        return json.dumps({"decisions": decisions}, ensure_ascii=False)

    return handler


def write_self_rating(dirs: SimpleNamespace, states: dict[str, str], **extra: Any) -> None:
    """给读者写一份只带概念自评的画像（``states`` = ``{概念 id: 状态}``）。"""
    profile = default_profile(READER)
    profile["concepts"] = {
        cid: {"state": state, "confidence": 0.7} for cid, state in states.items()
    }
    for key, value in extra.items():
        profile[key] = value
    save_profile(dirs.profiles, READER, profile)


def changes_of(outcome: Any) -> dict[str, Any]:
    return read_json(outcome.changes_path)


def by_paragraph(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["paragraph"]: item for item in payload["changes"]}


#: 「模型没给 why.profile 引用」这一类**逐段**说明的判据（「不误报」的用例用它数有没有多报）。
#: 注意别拿「why.profile」四个字当判据：``meta.degraded`` 里有一条 run 级的背景模式说明也含它，
#: 而那条是**对**的（它说的是「why.profile 保留模型给的引用」）。
REF_COMPLAINT_MARKERS = (
    "模型没有给 why.profile 引用",
    "why.profile 缺失",
    "why.profile 不是数组",
    "why.profile 里有",
)


def ref_complaints(payload: dict[str, Any]) -> list[str]:
    return [
        note
        for note in payload["meta"]["degraded"]
        if any(marker in note for marker in REF_COMPLAINT_MARKERS)
    ]


# ------------------------------------------------- ① 措辞：不许再说「画像为空」 --
def test_empty_self_rating_wording_never_says_the_profile_is_empty(dirs: SimpleNamespace) -> None:
    """空自评分支只说「没有提供任何概念自评」，并区分「有背景 / 连背景都没有」两种情况。"""
    no_signal = _format_profile_block(READER, {"concepts": {}}, [])
    assert "没有提供任何概念自评" in no_signal
    assert "也没有读前背景" in no_signal
    assert "画像为空" not in no_signal
    # 不许在这里指示模型写空数组：那件事归提示词规则 11（单段压缩那份提示词没有这个字段）
    assert "空数组" not in no_signal

    background_only = default_profile(READER)
    background_only["background"] = {"version": 1, "education": "undergraduate"}
    assert has_background(background_only) is True, "现场没造对：这份画像应当算「有背景」"
    with_background = _format_profile_block(READER, background_only, [])
    assert "没有提供任何概念自评" in with_background
    assert "只有读前背景问卷" in with_background
    assert "画像为空" not in with_background
    assert "空数组" not in with_background
    # 两段文案各自逐字稳定（同一画像必得同一段文本 ⇒ 前缀缓存不受影响）
    assert _format_profile_block(READER, {"concepts": {}}, []) == no_signal
    assert _format_profile_block(READER, background_only, []) == with_background


def test_prompt_carries_the_precise_wording_and_the_profile_rule(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """整条提示词里：那句「画像为空」不再出现，规则 11 的语义在场。"""
    write_fake_doc(dirs)
    write_self_rating(dirs, {})  # 读者没有概念自评、也没有背景
    client = fake_chat_client(handler=refs_handler([]))
    fake_run(dirs, client)

    prompt = client.calls[0][-1]["content"]
    assert "没有提供任何概念自评" in prompt
    assert "画像为空" not in prompt
    assert "why.profile 写的是你引用到的读者自评" in prompt
    assert "逐条照抄" in prompt
    # 背景那一条口径没被带乱：背景依据仍然只写 why.note（规则 8）
    assert "背景不要写进 why.profile" in prompt


# --------------------------------------- ② 保留 + ③ 不静默 + ④ 不误报（真跑一遍） --
def test_model_given_refs_are_kept_verbatim(dirs: SimpleNamespace, fake_chat_client) -> None:
    """读者有自评、模型也给了引用 ⇒ 逐条保留（一个字不改写、不补齐、不报降级）。"""
    write_fake_doc(dirs)
    write_self_rating(dirs, {"c01": "unknown"})
    outcome = fake_run(dirs, fake_chat_client(handler=refs_handler(["c01:known"])))

    changes = changes_of(outcome)
    rows = by_paragraph(changes)
    # 模型写的是 c01:known（画像里其实是 unknown）：产物照模型给的记，骨架不替它改口径
    assert rows["p002"]["why"]["profile"] == ["c01:known"]
    assert rows["p005"]["why"]["profile"] == ["c01:known"]
    assert outcome.profile_refs_filled == []
    assert not any("兜底补齐" in note for note in changes["meta"]["degraded"])
    # 没补齐的段**不出现**这个结构化标记（键不出现 == 不是补齐的，照 figure 的先例）
    assert "profile_refs_filled" not in rows["p002"]
    assert "profile_refs_filled" not in rows["p005"]


def test_empty_refs_with_reader_self_rating_are_filled_and_visible(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """「该给引用却给空数组」⇒ 逐段可见降级 + 只照读者自评兜底补齐。"""
    write_fake_doc(dirs)
    write_self_rating(dirs, {"c01": "unknown"})
    outcome = fake_run(dirs, fake_chat_client(handler=refs_handler([])))

    changes = changes_of(outcome)
    rows = by_paragraph(changes)
    for pid in C01_PARAGRAPHS:
        assert rows[pid]["why"]["profile"] == ["c01:unknown"], pid
        assert "兜底补齐" in rows[pid]["why"]["note"], pid
        # 结构化标记（2026-10-14）：页面那枚徽标的唯一判据，**为真才出现**
        assert rows[pid]["profile_refs_filled"] is True, pid
    assert outcome.profile_refs_filled == list(C01_PARAGRAPHS)
    filled_notes = [note for note in changes["meta"]["degraded"] if "模型没有给 why.profile 引用" in note]
    assert len(filled_notes) == len(C01_PARAGRAPHS)
    assert all("已按读者画像兜底补齐" in note for note in filled_notes)
    # 不涉及 c01 的段落：空数组照旧、不补齐、也不被误报（也就没有那个标记）
    assert rows["p003"]["why"]["profile"] == []
    assert "profile_refs_filled" not in rows["p003"]
    assert "p003" not in outcome.profile_refs_filled
    assert not any(note.startswith("p003：") for note in changes["meta"]["degraded"])
    # 两份产物都带上那条降级（decisions.json 与 changes.json 的 meta 同源）
    assert read_json(outcome.decisions_path)["meta"]["degraded"] == changes["meta"]["degraded"]
    # 标记只落在 changes.json 的条目上：decisions.json 的记录形态冻结（照 figure / deferred 的先例）
    assert all(
        "profile_refs_filled" not in record for record in read_json(outcome.decisions_path)["decisions"]
    )


def test_rerun_path_also_writes_the_structural_flag(dirs: SimpleNamespace, fake_chat_client) -> None:
    """局部重跑那条写盘路径同样带这个标记；未重跑段沿用既有产物（那时没有它就还是没有）。

    两条路径都要写（与 ``changes.json`` 的 ``meta.paragraph_count`` 同一条纪律）：
    只补一整篇那条路径，会让「重跑一次就看不见标记」变成新的静默。
    """
    write_fake_doc(dirs)
    write_self_rating(dirs, {"c01": "unknown"})

    # 第一遍（整篇）：模型给了引用 ⇒ 谁都不是补齐的
    first = fake_run(dirs, fake_chat_client(handler=refs_handler(["c01:known"])))
    assert "profile_refs_filled" not in by_paragraph(changes_of(first))["p002"]

    # 第二遍（--only p002）：模型这次漏键 ⇒ 这一段补齐并带上标记；p005 沿用旧条目
    targets = resolve_rerun_targets(SOURCE_ID, dirs.data, dirs.profiles, READER, only="p002")
    outcome = run_rewrite(
        SOURCE_ID,
        dirs.data,
        dirs.profiles,
        READER,
        fake_chat_client(handler=refs_handler(_MISSING)),
        targets=targets,
    )

    rows = by_paragraph(changes_of(outcome))
    assert rows["p002"]["profile_refs_filled"] is True
    assert rows["p002"]["why"]["profile"] == ["c01:unknown"]
    assert "profile_refs_filled" not in rows["p005"], "未重跑段沿用旧条目，旧条目里没有这个键"
    assert outcome.profile_refs_filled == ["p002"]


@pytest.mark.parametrize(
    ("profile_value", "expected"),
    [
        (_MISSING, "why.profile 缺失（模型没有给这一段的画像引用）"),
        ("c01:unknown", "why.profile 不是数组"),
        ([{"source": "背景"}], "why.profile 里有 1 项不是字符串"),
        ({"c01": "unknown"}, "why.profile 不是数组"),
    ],
)
def test_broken_why_profile_shapes_are_visible_and_filled(
    dirs: SimpleNamespace, fake_chat_client, profile_value: Any, expected: str
) -> None:
    """模型漏键 / 给错形状：**不静默**（逐段一条可读说明）且照样兜底补齐。"""
    write_fake_doc(dirs)
    write_self_rating(dirs, {"c01": "unknown"})
    outcome = fake_run(dirs, fake_chat_client(handler=refs_handler(profile_value)))

    changes = changes_of(outcome)
    rows = by_paragraph(changes)
    complaints = ref_complaints(changes)
    assert any(expected in note and note.startswith("p002：") for note in complaints), complaints
    assert rows["p002"]["why"]["profile"] == ["c01:unknown"]
    assert "兜底补齐" in rows["p002"]["why"]["note"]


def test_integer_profile_entries_are_not_silently_coerced(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """模型给 ``[42]`` **不许**被 ``str()`` 静静转成 ``"42"`` 留下（2026-10-14 收紧）。

    此前那条分支是 ``isinstance(item, (str, int))`` 后再 ``str(item)``：产物里会多出一个
    从没在画像里出现过的引用 ``"42"``——既不报也不补，正是本项目禁的「静默」。
    收紧的依据（三条，见 `ar.rewrite._normalize_plan_items` 的注释）：

      ① 引用形态是 `概念 id:读者自评状态`（`c03:unknown`），概念 id 必须形如 `c01`
         （`ar.analyze.validate_inventory` 逐条校验）⇒ 整数在任何输入下都不是合法引用；
      ② `ar.tools._DECISION` 的出参 schema 本来就写着 `profile.items: {"type": "string"}`，
         实现比契约松就是「模型输出不经 schema 校验、实现挑什么就落什么」的口子；
      ③ 全仓没有一处测试或调用方依赖整数被接受。

    现在它与「数组里混进对象」走**同一档可见说明**，并照读者自评兜底补齐。
    """
    write_fake_doc(dirs)
    write_self_rating(dirs, {"c01": "unknown"})
    outcome = fake_run(dirs, fake_chat_client(handler=refs_handler([42])))

    changes = changes_of(outcome)
    rows = by_paragraph(changes)
    # ① 那个整数不许以任何形式留在产物里（此前会变成字符串 "42"）
    assert rows["p002"]["why"]["profile"] == ["c01:unknown"]
    assert "42" not in rows["p002"]["why"]["profile"]
    assert "42" not in rows["p002"]["why"]["note"], "兜底补齐的出处说明里也不许冒出这个整数"
    # ② 不静默：一条与其它「错形状」同档的可见说明（逐段点名）
    complaints = ref_complaints(changes)
    assert any(
        "why.profile 里有 1 项不是字符串" in note and note.startswith("p002：") for note in complaints
    ), complaints
    # ③ 照读者自己的自评兜底补齐，并留结构化标记（页面那枚徽标的判据）
    assert "兜底补齐" in rows["p002"]["why"]["note"]
    assert rows["p002"]["profile_refs_filled"] is True
    assert outcome.profile_refs_filled == list(C01_PARAGRAPHS)


def test_reader_without_self_rating_is_not_flagged(dirs: SimpleNamespace, fake_chat_client) -> None:
    """本段涉及的概念读者一个都没自评 ⇒ 空数组是合法结果，不误报、不补齐。"""
    write_fake_doc(dirs)
    # 读者有自评，但那份自评是另一个概念（c99 不在任何一段里）——即「本段无可引用」
    write_self_rating(dirs, {"c99": "known"})
    outcome = fake_run(dirs, fake_chat_client(handler=refs_handler([])))

    changes = changes_of(outcome)
    rows = by_paragraph(changes)
    assert all(row["why"]["profile"] == [] for row in rows.values())
    assert outcome.profile_refs_filled == []
    assert ref_complaints(changes) == []
    assert not any("兜底补齐" in row["why"]["note"] for row in rows.values())


def test_background_only_reader_with_empty_refs_is_not_flagged(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """真机那一档（只有背景、没有概念自评）：``profile_used`` 为真，但空数组**不报降级**。"""
    write_fake_doc(dirs)
    profile = default_profile(READER)
    profile["background"] = {"version": 1, "education": "undergraduate"}
    save_profile(dirs.profiles, READER, profile)
    assert has_concept_signals(profile) is False and has_background(profile) is True

    outcome = fake_run(dirs, fake_chat_client(handler=refs_handler([])))

    changes = changes_of(outcome)
    assert changes["meta"]["profile_used"] is True
    assert changes["meta"]["background_used"] is True
    assert all(row["why"]["profile"] == [] for row in by_paragraph(changes).values())
    assert outcome.profile_refs_filled == []
    assert ref_complaints(changes) == []


def test_empty_profile_mode_still_overwrites_refs_with_difficulty(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """完全空画像那条老路一个字没变：``why.profile`` 写「难度 N（无画像）」或空 + note 说明。"""
    write_fake_doc(dirs)
    outcome = fake_run(dirs, fake_chat_client(handler=refs_handler(["c01:unknown"])))

    changes = changes_of(outcome)
    rows = by_paragraph(changes)
    assert changes["meta"]["profile_used"] is False
    assert rows["p002"]["why"]["profile"] == ["难度 4（无画像）"]
    assert rows["p003"]["why"]["profile"] == [] and "无画像" in rows["p003"]["why"]["note"]
    assert outcome.profile_refs_filled == []


# ----------------------------------------------------------- 单元：兜底依据的来源 --
def test_self_rating_refs_use_overrides_first_and_only_reader_data() -> None:
    """兜底只能用读者自己写下的东西：``overrides``（手动结论）优先于 ``concepts``。"""
    profile = {
        "concepts": {"c01": {"state": "unknown"}, "c02": {"state": "partial"}},
        "overrides": [{"concept": "c01", "state": "known", "note": "别再解释了"}],
    }
    states = _self_rating_states(profile)
    assert states == {"c01": "known", "c02": "partial"}
    concepts = {"c01": ["p002"], "c02": ["p002", "p005"]}
    assert _self_rating_refs("p002", states, concepts) == ["c01:known", "c02:partial"]
    assert _self_rating_refs("p003", states, concepts) == []
    # 画像读坏了（形状不对）不抛错，也不编造：拿不到自评就当「没有可引用的」
    assert _self_rating_states({"concepts": "坏掉的", "overrides": [{"concept": "c01"}]}) == {}


def test_prepare_declarations_reports_a_non_object_shape(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """``fact_declarations`` 非对象（模型给了一个字符串）：按「没有申报」处理，但**可见**。"""
    write_fake_doc(dirs)
    write_self_rating(dirs, {"c01": "known"})

    def handler(messages: list[dict[str, str]]) -> str:
        decisions = [
            {
                "paragraph": pid,
                "action": "keep",
                "why": {"profile": ["c01:known"], "note": f"{pid} 的假依据"},
                "confidence": 0.8,
                "rewrite": "",
                "fact_declarations": "数字 1696",
            }
            for pid in prompt_ids(messages[-1]["content"])
        ]
        return json.dumps({"decisions": decisions}, ensure_ascii=False)

    outcome = fake_run(dirs, fake_chat_client(handler=handler))

    changes = changes_of(outcome)
    notes = [note for note in changes["meta"]["degraded"] if "fact_declarations 不是对象" in note]
    assert any(note.startswith("p002：") for note in notes), changes["meta"]["degraded"]
    # 安全方向不变：整条按「没有申报」处理 ⇒ 那一段不出现 fact_declarations 键
    assert "fact_declarations" not in by_paragraph(changes)["p002"]


# ------------------------------------------------- 工具定义层：字段语义（不是装饰） --
def test_decision_schema_documents_the_profile_rule(run_cli) -> None:
    """``_DECISION`` 里 ``why.profile`` 的字段语义写明「有自评时必须逐条保留模型给的引用」。"""
    spec = get_tool("rewrite_batch")
    why_schema = spec.output["properties"]["decisions"]["items"]["properties"]["why"]
    profile_schema = why_schema["properties"]["profile"]
    assert "逐条保留" in why_schema["description"]
    assert "逐条保留" in profile_schema["description"]
    assert "c03:unknown" in profile_schema["description"]
    assert "空数组" in profile_schema["description"]
    assert "背景" in why_schema["properties"]["note"]["description"]

    code, output = run_cli("tools", "--json")
    assert code == 0, output
    payload = json.loads(output)
    rewrite_batch = next(item for item in payload if item["name"] == "rewrite_batch")
    description = rewrite_batch["output"]["properties"]["decisions"]["items"]["properties"]["why"][
        "properties"
    ]["profile"]["description"]
    assert "逐条保留" in description


# --------------------------------------------------------- 命令行：这一层看得见 --
def test_cli_prints_the_why_profile_account(
    dirs: SimpleNamespace, sample_md, tmp_path, run_cli, monkeypatch, fake_chat_client
) -> None:
    """``ar rewrite`` 打 ``why.profile`` 的账：几段有引用 / 几段空 / 哪几段是兜底补齐的。"""
    from ar import cli

    ingest(sample_md, "sample", dirs.data)
    (dirs.data / "sample" / "concepts.json").write_text(
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
                "meta": {"source_id": "sample", "model": "fake-model", "prompt_version": "concepts-v2"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    write_self_rating(dirs, {"c01": "unknown"})
    config_path = tmp_path / "config.local.json"
    config_path.write_text(
        json.dumps({"model": "fake-config-model", "retry": {"maxRetries": 0}, "api_key": "fake-key"}),
        encoding="utf-8",
    )
    monkeypatch.delenv("AR_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_make_client", lambda config: fake_chat_client(handler=refs_handler([])))

    code, output = run_cli(
        "--data-dir",
        str(dirs.data),
        "--profiles-dir",
        str(dirs.profiles),
        "--config",
        str(config_path),
        "rewrite",
        "sample",
        "--reader",
        READER,
    )

    assert code == 0, output
    assert "why.profile：" in output
    # 三段的 c01 都该被兜底补齐（p002 / p003 / p005 都涉及 c01）
    assert "3 段有引用 / 3 段空" in output
    assert "已按读者自评兜底补齐" in output and "p002" in output
    # 命令行说的与产物里的逐段账一致（同一份数据，两处口径不许分叉）
    changes = read_json(dirs.data / "sample" / "changes.json")
    rows = by_paragraph(changes)
    assert rows["p002"]["why"]["profile"] == ["c01:unknown"]
    assert changes["meta"]["fact_anchor_policy"] == DEFAULT_FACT_ANCHOR_POLICY
