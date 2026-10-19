"""事实锚「可申报 + 可见」（PLAN-actions-feedback §14.1）的后端用例。

三条硬底线（依次钉住）：

1. 缺失**且没申报** → ``ar check`` 退出码 4（这条网不许因为松绑而静默失效）；
2. 缺失**但已申报** + 读者口径 ``allow_declared_drops`` → 通过（退出码 0），
   申报出现在**产物**（``changes[i].fact_declarations`` / ``fidelity.waived``）与 **CLI 输出**里；
3. **同一份申报**在 ``keep_all``（严口径）下仍然退出码 4——偏好取值决定松 / 严，
   口径来自**读者画像里那条偏好**（不是全局开关、也不是一次性 CLI flag）。

另钉住四条边界（松绑只松「少了什么」，不松事实漂移）：多出来的锚点、顺序调换、
申报与实测对不上、申报读不通——一律不放行且可见。
偏好侧钉住：读写 / 创建路径（``set_preference`` + ``POST /api/profile/preference``）、
取值读坏退到严口径、以及「行为信号绝不自动新建偏好」这条老约束仍然成立。

全部假模型 / 假产物：**零真实模型调用**。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from ar.config import Config
from ar.errors import EXIT_OK, EXIT_VALIDATION, InputError
from ar.fidelity import (
    FACT_ANCHOR_POLICIES,
    compare_blocks_detailed,
    parse_declarations,
)
from ar.ingest import paragraph_id, text_hash
from ar.profile import (
    FACT_ANCHOR_PREFERENCE_ID,
    PREFERENCE_VALUES,
    applySignals,
    default_profile,
    fact_anchor_policy,
    load_profile,
    resolve_fact_anchor_policy,
    save_profile,
    set_preference,
)
from ar.prompts import FACT_ANCHOR_BLOCKS, build_rewrite_messages, fact_anchor_block
from ar.rewrite import check_fidelity, run_rewrite
from ar.server import build_app
from conftest import FakeChatClient

SOURCE_ID = "decl-doc"
NUMBERS = ("1859", "1696")
QUOTE = "庞大的商品堆积"
BOOK = "政治经济学批判"

#: p001 带三类事实锚（数字两个 / 引文一条 / 书名一个），p002 干净
SOURCE_BLOCKS = [
    f"马克思在 {NUMBERS[0]} 年写的《{BOOK}》里，开头就是“{QUOTE}”；另一处是 {NUMBERS[1]} 年。",
    "这一段没有任何需要保真的东西。",
]


def _declaration(**kwargs) -> dict:
    return {key: value for key, value in kwargs.items() if value is not None}


def write_material(
    dirs: SimpleNamespace,
    *,
    rewritten: list[str] | None = None,
    declarations: dict[str, dict] | None = None,
    meta_policy: str | None = None,
    reader: str = "local-default",
) -> Path:
    """写一份**锚点自洽**的假产物（``source.md`` / ``source.meta.json`` / ``rewritten.md`` / ``changes.json``）。

    ``rewritten`` 缺省 = 原样照抄（事实锚全对）；``declarations`` 是 ``{段 id: 申报}``；
    ``meta_policy`` 写进 ``changes.json`` 的 ``meta.fact_anchor_policy``（产物侧记的那一档）。
    """
    import hashlib

    markdown = "\n\n".join(SOURCE_BLOCKS) + "\n"
    target = dirs.data / SOURCE_ID
    target.mkdir(parents=True, exist_ok=True)
    (target / "source.md").write_bytes(markdown.encode("utf-8"))
    meta = {
        "source_id": SOURCE_ID,
        "ingest": {
            "kind": "local_file",
            "path": "(测试假材料)",
            "hash": "sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            "at": "2026-10-14T00:00:00+08:00",
        },
        "paragraphs": [
            {"id": paragraph_id(index), "index": index, "text": text, "text_hash": text_hash(text)}
            for index, text in enumerate(SOURCE_BLOCKS, start=1)
        ],
    }
    (target / "source.meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    blocks = rewritten if rewritten is not None else list(SOURCE_BLOCKS)
    (target / "rewritten.md").write_text("\n\n".join(blocks) + "\n", encoding="utf-8")

    changes = []
    for index in range(len(SOURCE_BLOCKS)):
        pid = paragraph_id(index + 1)
        entry = {
            "paragraph": pid,
            "action": "keep",
            "why": {"profile": [], "note": "假依据"},
            "confidence": 0.5,
            "facts": {"numbers": [], "quotes": []},
        }
        if declarations and pid in declarations:
            entry["fact_declarations"] = dict(declarations[pid])
        changes.append(entry)
    meta_block = {
        "source_id": SOURCE_ID,
        "reader": reader,
        "model": "fake-model",
        "paragraph_count": len(SOURCE_BLOCKS),
    }
    if meta_policy:
        meta_block["fact_anchor_policy"] = meta_policy
    (target / "changes.json").write_text(
        json.dumps(
            {"changes": changes, "fidelity": {"checked": False, "mismatches": []}, "meta": meta_block},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return target


def write_profile(dirs: SimpleNamespace, policy: str | None, reader: str = "local-default") -> dict:
    """写一份画像（可选带上事实锚口径偏好）；返回落盘后的画像。"""
    profile = default_profile(reader)
    if policy is not None:
        profile = set_preference(profile, FACT_ANCHOR_PREFERENCE_ID, policy)
    save_profile(dirs.profiles, reader, profile)
    return profile


def run_check(run_cli, dirs: SimpleNamespace, *extra: str) -> tuple[int, str]:
    return run_cli(
        "check",
        SOURCE_ID,
        "--data-dir",
        str(dirs.data),
        "--profiles-dir",
        str(dirs.profiles),
        *extra,
    )


def fidelity_of(dirs: SimpleNamespace) -> dict:
    payload = json.loads((dirs.data / SOURCE_ID / "changes.json").read_text(encoding="utf-8"))
    return payload["fidelity"]


# ------------------------------------------- ① 缺失且未申报 → 退出码 4（底线不变） --
def test_missing_and_undeclared_still_fails_with_exit_code_4(run_cli, dirs: SimpleNamespace) -> None:
    """一条锚点都没申报就丢掉 → 照旧失败，并点名「没申报」。"""
    write_material(dirs, rewritten=["这一年有这句话。", SOURCE_BLOCKS[1]])
    write_profile(dirs, None)

    code, output = run_check(run_cli, dirs)

    assert code == EXIT_VALIDATION, output
    assert "事实锚校验失败" in output
    assert "p001" in output
    assert "没有申报" in output
    # 严口径下没有放行条目，checked 保持 false（「跑过、结果是坏的」留痕）
    fidelity = fidelity_of(dirs)
    assert fidelity["checked"] is False
    assert fidelity["waived"] == []
    assert fidelity["fact_anchor_policy"] == "keep_all"


def test_declared_drop_passes_under_the_loose_policy_and_is_visible(
    run_cli, dirs: SimpleNamespace
) -> None:
    """② 缺失但**逐条申报** + 读者口径松 → 通过；申报在产物与命令行两处都看得见。"""
    declaration = _declaration(
        numbers=list(NUMBERS), quotes=[QUOTE], books=[BOOK], note="要结论：举例数据与引文一并舍弃"
    )
    write_material(
        dirs,
        rewritten=["这一年有这句话。", SOURCE_BLOCKS[1]],
        declarations={"p001": declaration},
        meta_policy="allow_declared_drops",
    )
    write_profile(dirs, "allow_declared_drops")

    code, output = run_check(run_cli, dirs)

    assert code == EXIT_OK, output
    assert "allow_declared_drops" in output
    assert "已申报并放行" in output
    assert "申报放弃" in output and NUMBERS[0] in output and QUOTE in output
    # 放行的条目按**类别**记（数字 / 引号引文 / 书名各一条），条目里写明申报了几项
    assert "其中 3 项已申报放行" in output
    assert "数字：申报 2 项" in output

    payload = json.loads((dirs.data / SOURCE_ID / "changes.json").read_text(encoding="utf-8"))
    # 申报原样留在产物里（谁都能复核「模型说了它要丢什么」）
    assert payload["changes"][0]["fact_declarations"] == declaration
    fidelity = payload["fidelity"]
    assert fidelity["checked"] is True
    assert fidelity["mismatches"] == []
    assert fidelity["fact_anchor_policy"] == "allow_declared_drops"
    waived = {row["paragraph"]: row for row in fidelity["waived"]}
    assert set(waived) == {"p001"}
    assert waived["p001"]["missing"] and waived["p001"]["declared"]
    assert "放行" in waived["p001"]["detail"]


def test_same_declaration_still_fails_under_keep_all(run_cli, dirs: SimpleNamespace) -> None:
    """③ 同一份申报在严口径下不放行：口径由画像里那条偏好决定，不是「申报了就过」。"""
    declaration = _declaration(numbers=list(NUMBERS), quotes=[QUOTE], books=[BOOK])
    write_material(
        dirs,
        rewritten=["这一年有这句话。", SOURCE_BLOCKS[1]],
        declarations={"p001": declaration},
        meta_policy="keep_all",
    )
    write_profile(dirs, "keep_all")

    code, output = run_check(run_cli, dirs)

    assert code == EXIT_VALIDATION, output
    assert "keep_all" in output
    assert "申报不放行" in output
    assert "pref_fact_anchor" in output, "报错里要说清怎么才能放行（改画像里那条偏好）"
    fidelity = fidelity_of(dirs)
    assert fidelity["waived"] == [] and fidelity["checked"] is False


# --------------------- ⑤ 乱申报（锚点一个没少却申报放弃了某项）→ 可见，但不改结论 --
def test_over_declaration_is_reported_without_changing_the_verdict(
    run_cli, dirs: SimpleNamespace
) -> None:
    """**2026-10-14 收尾**：原样照抄（三类锚点一个没少）却申报「放弃了某项」时，退出码仍是 0、
    放行结论不变（本来就没有缺失可放行），但 CLI 与 ``fidelity.over_declared`` 都要留字。

    此前 ``same_facts(...) → continue`` 在校验申报之前短路，这种「乱申报」一个字都不提。
    """
    declaration = _declaration(numbers=[NUMBERS[0]], note="我其实没动这个数字")
    write_material(dirs, declarations={"p001": declaration})  # 缺省 rewritten = 原样照抄
    write_profile(dirs, "allow_declared_drops")

    code, output = run_check(run_cli, dirs)

    assert code == EXIT_OK, output
    assert "事实锚校验通过" in output
    # CLI：多申报逐条打印（数字那一类）
    assert "事实锚多申报" in output
    assert NUMBERS[0] in output and "多申报" in output
    # 产物：fidelity.over_declared 留痕，放行列表照旧是空的（没有缺失可放行）
    fidelity = fidelity_of(dirs)
    assert fidelity["checked"] is True and fidelity["mismatches"] == []
    assert fidelity["waived"] == []
    rows = fidelity["over_declared"]
    assert [row["kind"] for row in rows] == ["numbers"]
    assert rows[0]["paragraph"] == "p001" and rows[0]["declared"] == [NUMBERS[0]]
    assert "多申报" in rows[0]["detail"]


def test_over_declaration_is_not_reported_when_it_matches_the_drop(
    run_cli, dirs: SimpleNamespace
) -> None:
    """对照组：真的丢了并如实申报 → 只进 ``waived``，``over_declared`` 一个条目都不许有。"""
    declaration = _declaration(numbers=list(NUMBERS), quotes=[QUOTE], books=[BOOK])
    write_material(
        dirs,
        rewritten=["这一年有这句话。", SOURCE_BLOCKS[1]],
        declarations={"p001": declaration},
        meta_policy="allow_declared_drops",
    )
    write_profile(dirs, "allow_declared_drops")

    code, output = run_check(run_cli, dirs)

    assert code == EXIT_OK, output
    assert "事实锚多申报" not in output
    fidelity = fidelity_of(dirs)
    assert fidelity["waived"] and fidelity["over_declared"] == []


def test_waived_row_reports_the_declared_items_that_were_not_actually_dropped(
    run_cli, dirs: SimpleNamespace
) -> None:
    """申报里混着「本来就没丢」的锚点：放行照旧，但那几项单独记一条 ``over_declared``。"""
    declaration = _declaration(
        numbers=list(NUMBERS), quotes=[QUOTE], books=[BOOK], note="数字、引文、书名一并舍弃"
    )
    write_material(
        dirs,
        # 实际只丢了 1859 与那条引文（1696 与书名照抄），申报里却把四项都报了
        rewritten=[f"马克思在《{BOOK}》里写过这一句，另一处是 {NUMBERS[1]} 年。", SOURCE_BLOCKS[1]],
        declarations={"p001": declaration},
        meta_policy="allow_declared_drops",
    )
    write_profile(dirs, "allow_declared_drops")

    code, output = run_check(run_cli, dirs)

    assert code == EXIT_OK, output
    fidelity = fidelity_of(dirs)
    assert [row["kind"] for row in fidelity["waived"]] == ["numbers", "quotes"]
    assert "p001" in output and "已申报并放行" in output
    over = {row["kind"]: row for row in fidelity["over_declared"]}
    # ① 放行那一行里混着「没丢」的 1696；② 书名那一类根本没少却申报放弃了它
    assert set(over) == {"numbers", "books"}, fidelity["over_declared"]
    assert over["numbers"]["declared"] == [NUMBERS[1]] and "并没有丢" in over["numbers"]["detail"]
    assert over["books"]["declared"] == [BOOK] and "一个都没少" in over["books"]["detail"]


# ------------------------------------- 松绑只松「少了什么」：四条边界都不放行 --
def test_extra_anchor_is_never_excused(run_cli, dirs: SimpleNamespace) -> None:
    """「把 1859 换成 1848」是事实漂移：申报了也不放行（多出来的锚点不在申报范围内）。"""
    write_material(
        dirs,
        rewritten=[f"马克思在 1848 年写的《{BOOK}》里，开头就是“{QUOTE}”。", SOURCE_BLOCKS[1]],
        declarations={"p001": _declaration(numbers=list(NUMBERS), quotes=[QUOTE], books=[BOOK])},
        meta_policy="allow_declared_drops",
    )
    write_profile(dirs, "allow_declared_drops")

    code, output = run_check(run_cli, dirs)

    assert code == EXIT_VALIDATION, output
    assert "多出来的事实锚不在申报范围内" in output
    assert fidelity_of(dirs)["waived"] == []


def test_order_change_is_never_excused(run_cli, dirs: SimpleNamespace) -> None:
    """数量相同但顺序变了：申报只覆盖「少了什么」，调序不放行。"""
    write_material(
        dirs,
        rewritten=[
            f"另一处是 {NUMBERS[1]} 年；马克思在 {NUMBERS[0]} 年写的《{BOOK}》里，开头就是“{QUOTE}”。",
            SOURCE_BLOCKS[1],
        ],
        declarations={"p001": _declaration(numbers=list(NUMBERS), quotes=[QUOTE], books=[BOOK])},
        meta_policy="allow_declared_drops",
    )
    write_profile(dirs, "allow_declared_drops")

    code, output = run_check(run_cli, dirs)

    assert code == EXIT_VALIDATION, output
    assert "顺序调换不在申报范围内" in output


def test_declaration_that_does_not_match_the_actual_gap_is_not_excused(
    run_cli, dirs: SimpleNamespace
) -> None:
    """申报的是别的东西（对不上实测缺失）→ 未申报的那部分照旧报错。"""
    write_material(
        dirs,
        rewritten=[f"马克思写的《{BOOK}》里，开头就是“{QUOTE}”。", SOURCE_BLOCKS[1]],
        declarations={"p001": _declaration(numbers=["404"])},
        meta_policy="allow_declared_drops",
    )
    write_profile(dirs, "allow_declared_drops")

    code, output = run_check(run_cli, dirs)

    assert code == EXIT_VALIDATION, output
    assert "对不上" in output and "未申报的缺失不放行" in output


def test_unreadable_declaration_is_visible_and_does_not_excuse_anything(
    run_cli, dirs: SimpleNamespace
) -> None:
    """申报结构读不通（不是对象 / 混进非字符串 / 不认识的键）→ 一条都不静默，且不放行。"""
    write_material(
        dirs,
        rewritten=["这一年有这句话。", SOURCE_BLOCKS[1]],
        declarations={"p001": {"numbers": ["1859", 42], "页码": ["27"]}},
        meta_policy="allow_declared_drops",
    )
    write_profile(dirs, "allow_declared_drops")

    code, output = run_check(run_cli, dirs)

    assert code == EXIT_VALIDATION, output
    assert "非字符串" in output and "不认识的键" in output
    assert fidelity_of(dirs)["waived"] == []


# ------------------------------------------------- 口径解析与「产物的口径 vs 现在的口径」 --
def test_check_uses_the_policy_recorded_in_the_artifact_without_a_profile_dir(dirs: SimpleNamespace) -> None:
    """没给画像目录（老调用方）= 用产物自己记的口径；产物没记 → 严口径（行为与改动前一致）。"""
    declaration = _declaration(numbers=list(NUMBERS), quotes=[QUOTE], books=[BOOK])
    write_material(
        dirs,
        rewritten=["这一年有这句话。", SOURCE_BLOCKS[1]],
        declarations={"p001": declaration},
        meta_policy="allow_declared_drops",
    )
    outcome = check_fidelity(SOURCE_ID, dirs.data)
    assert outcome.policy == "allow_declared_drops"
    assert outcome.mismatches == [] and len(outcome.waived) == 3

    write_material(
        dirs,
        rewritten=["这一年有这句话。", SOURCE_BLOCKS[1]],
        declarations={"p001": declaration},
    )
    outcome = check_fidelity(SOURCE_ID, dirs.data)
    assert outcome.policy == "keep_all"
    assert outcome.mismatches, "没记口径时按严口径办：这条缺失必须报出来"


def test_live_profile_wins_and_the_mismatch_is_visible(run_cli, dirs: SimpleNamespace) -> None:
    """产物记的是严口径、读者现在改成松口径：按**现在的**口径校验，并把这件事说出来。"""
    write_material(
        dirs,
        rewritten=["这一年有这句话。", SOURCE_BLOCKS[1]],
        declarations={"p001": _declaration(numbers=list(NUMBERS), quotes=[QUOTE], books=[BOOK])},
        meta_policy="keep_all",
    )
    write_profile(dirs, "allow_declared_drops")

    code, output = run_check(run_cli, dirs)

    assert code == EXIT_OK, output
    assert "产物是按 keep_all 改写的" in output and "allow_declared_drops" in output
    assert "重跑 `ar rewrite`" in output


def test_broken_preference_value_falls_back_to_strict_and_says_so(run_cli, dirs: SimpleNamespace) -> None:
    """取值不认识 / 没有 value → 退到严口径（只会更严，不会悄悄放宽），并给一条可见说明。"""
    assert fact_anchor_policy({}) == "keep_all"
    broken = {"reader": "r", "preferences": [{"id": FACT_ANCHOR_PREFERENCE_ID, "value": "loose"}]}
    resolved = resolve_fact_anchor_policy(broken)
    assert resolved.policy == "keep_all"
    assert resolved.warning and "不认识" in resolved.warning
    missing_value = {"reader": "r", "preferences": [{"id": FACT_ANCHOR_PREFERENCE_ID}]}
    assert resolve_fact_anchor_policy(missing_value).warning and "没有 value" in (
        resolve_fact_anchor_policy(missing_value).warning or ""
    )

    # 画像里写了个不认识的值：拿着它跑 check 也不放行（同一份申报仍然退出码 4）
    write_material(
        dirs,
        rewritten=["这一年有这句话。", SOURCE_BLOCKS[1]],
        declarations={"p001": _declaration(numbers=list(NUMBERS), quotes=[QUOTE], books=[BOOK])},
    )
    save_profile(dirs.profiles, "local-default", broken)
    code, output = run_check(run_cli, dirs)
    assert code == EXIT_VALIDATION, output
    assert "不认识" in output


def test_check_reader_flag_selects_another_profile(run_cli, dirs: SimpleNamespace) -> None:
    """``ar check --reader``：口径看的是**这个读者**的画像。"""
    write_material(
        dirs,
        rewritten=["这一年有这句话。", SOURCE_BLOCKS[1]],
        declarations={"p001": _declaration(numbers=list(NUMBERS), quotes=[QUOTE], books=[BOOK])},
        meta_policy="allow_declared_drops",
    )
    write_profile(dirs, "keep_all", reader="local-default")
    write_profile(dirs, "allow_declared_drops", reader="other-reader")

    strict_code, _ = run_check(run_cli, dirs)
    loose_code, loose_output = run_check(run_cli, dirs, "--reader", "other-reader")

    assert strict_code == EXIT_VALIDATION
    assert loose_code == EXIT_OK, loose_output
    assert "profile:other-reader" in loose_output


# ------------------------------------------------------- 偏好：读写与创建路径 --
def test_set_preference_creates_then_updates_and_is_idempotent() -> None:
    original = default_profile("local-default")
    created = set_preference(original, FACT_ANCHOR_PREFERENCE_ID, "allow_declared_drops")

    assert original["preferences"] == [], "原画像一个字节都不许动（纯函数）"
    entry = created["preferences"][0]
    assert entry["id"] == FACT_ANCHOR_PREFERENCE_ID
    assert entry["value"] == "allow_declared_drops"
    assert entry["statement"] and entry["confidence"] == 0.7
    assert entry["evidence"][0]["source"] == "manual"
    assert fact_anchor_policy(created) == "allow_declared_drops"

    # 同 id 同取值：幂等（不追加证据、不换时间戳）——调用方据此少写一次盘
    again = set_preference(created, FACT_ANCHOR_PREFERENCE_ID, "allow_declared_drops")
    assert again == created

    # 换取值：改 value + 换说法 + 追记一条证据（旧证据留着）
    flipped = set_preference(created, FACT_ANCHOR_PREFERENCE_ID, "keep_all", note="改回全保留")
    assert flipped["preferences"][0]["value"] == "keep_all"
    assert "全保留" in flipped["preferences"][0]["statement"]
    assert len(flipped["preferences"][0]["evidence"]) == 2
    assert fact_anchor_policy(flipped) == "keep_all"


def test_set_preference_rejects_unknown_ids_and_values(tmp_path: Path) -> None:
    profile = default_profile("local-default")
    with pytest.raises(InputError):
        set_preference(profile, "pref_paragraph_len", "short")
    with pytest.raises(InputError) as excinfo:
        set_preference(profile, FACT_ANCHOR_PREFERENCE_ID, "loose")
    assert "allow_declared_drops" in str(excinfo.value)
    assert set(PREFERENCE_VALUES) == {FACT_ANCHOR_PREFERENCE_ID}


def test_apply_signals_still_never_creates_a_preference() -> None:
    """两条路分工：行为信号只给**已有**偏好追加 evidence，绝不自动新建（老约束不变）。"""
    signal = {
        "preference": FACT_ANCHOR_PREFERENCE_ID,
        "paragraph": "p001",
        "kind": "compress",
        "weight": -0.75,
        "at": "2026-10-14T00:00:00+08:00",
        "detail": "（测试）读者标了太啰嗦",
    }
    missing = applySignals(default_profile("local-default"), [signal])
    assert missing.profile["preferences"] == []
    assert any("绝不自动新建偏好" in note for note in missing.degraded)

    with_entry = set_preference(
        default_profile("local-default"), FACT_ANCHOR_PREFERENCE_ID, "allow_declared_drops"
    )
    appended = applySignals(with_entry, [signal])
    assert appended.profile["preferences"][0]["value"] == "allow_declared_drops"
    assert len(appended.profile["preferences"][0]["evidence"]) == 2
    assert appended.evidence_added == 1


def test_preference_endpoint_writes_the_profile_and_rejects_bad_input(dirs: SimpleNamespace) -> None:
    """``POST /api/profile/preference`` 是偏好的**唯一写入入口**（网页侧只读不写也不影响它）。"""
    write_profile(dirs, None)  # 先有一份画像：否则响应里的 degraded 会带「画像不存在」那条说明
    config = Config(base_url="https://gateway.invalid/provider/v1", model="fake/model", api_key="")
    client = TestClient(
        build_app(
            data_dir=dirs.data,
            profiles_dir=dirs.profiles,
            web_dir=dirs.web,
            config=config,
            reader="local-default",
        )
    )

    ok = client.post(
        "/api/profile/preference",
        json={"id": FACT_ANCHOR_PREFERENCE_ID, "value": "allow_declared_drops", "note": "要结论"},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["ok"] is True and body["degraded"] == []
    profile, notes = load_profile(dirs.profiles, "local-default")
    assert notes == []
    assert fact_anchor_policy(profile) == "allow_declared_drops"
    # 读侧同源：GET /api/profile 直接返回这份画像
    assert client.get("/api/profile").json()["profile"]["preferences"][0]["value"] == "allow_declared_drops"

    for payload in (
        {"id": FACT_ANCHOR_PREFERENCE_ID, "value": "loose"},
        {"id": "pref_paragraph_len", "value": "short"},
        {"id": "", "value": "keep_all"},
        {"id": FACT_ANCHOR_PREFERENCE_ID, "value": ""},
    ):
        bad = client.post("/api/profile/preference", json=payload)
        assert bad.status_code == 400, payload
        assert "error" in bad.json()


# ------------------------------------------------- 提示词：两档口径各一段固定文案 --
def _prompt(policy: str) -> str:
    messages = build_rewrite_messages(
        "decl-doc",
        [{"id": "p001", "text": SOURCE_BLOCKS[0]}],
        profile_block="（无画像）",
        concept_block="（无概念清单）",
        start=1,
        end=1,
        total=1,
        fact_anchor_policy=policy,
    )
    return messages[-1]["content"]


def test_prompt_carries_one_stable_block_per_policy() -> None:
    strict = _prompt("keep_all")
    loose = _prompt("allow_declared_drops")
    strict_block = fact_anchor_block("keep_all")
    loose_block = fact_anchor_block("allow_declared_drops")

    assert strict_block in strict
    assert loose_block in loose
    assert loose_block not in strict
    # 逐字稳定：同一档取值重跑必得同一段提示词（前缀缓存与回归基线都靠它）
    assert _prompt("allow_declared_drops") == loose
    assert _prompt("keep_all") == strict
    # 取值不认识 / 缺省 → 按严口径写（与 ar.fidelity 同一默认值）
    assert _prompt("") == strict and _prompt("whatever") == strict
    # 两档的差异**只**在那一格：把口径块挖掉，其余部分逐字相同
    assert strict.replace(strict_block, "X") == loose.replace(loose_block, "X")
    # 会变的内容（本批段号与正文）仍在末尾：首个差异字符落在段号位这条性质不变
    assert strict.rstrip().endswith(f"[p001] {SOURCE_BLOCKS[0]}")
    assert set(FACT_ANCHOR_POLICIES) == set(FACT_ANCHOR_BLOCKS)


def test_fact_anchor_block_is_a_rule_number_template() -> None:
    """口径文案是**共用模板**：``{rule}`` 换成申报规则的序号，两份提示词各用各的编号。

    ``rewrite`` 用默认的 10（``rewrite-v5`` 的提示词因此逐字未变）；单段压缩（``compress-v2``）
    传 6，指向它自己那条申报规则。除了那个数字，两档文案一个字都不许变。
    """
    assert "{rule}" in FACT_ANCHOR_BLOCKS["keep_all"] and "{rule}" in FACT_ANCHOR_BLOCKS["allow_declared_drops"]
    assert "规则 10" in fact_anchor_block("keep_all")
    assert "规则 6" in fact_anchor_block("allow_declared_drops", rule_no=6)
    for policy in FACT_ANCHOR_POLICIES:
        assert fact_anchor_block(policy, rule_no=6) == fact_anchor_block(policy).replace("规则 10", "规则 6")


# --------------------------------------------- 端到端：模型申报 → 产物 → 命令行 --
def test_rewrite_records_the_model_declaration_and_the_reader_policy(dirs: SimpleNamespace) -> None:
    """一次真改写（假模型）：申报落 ``changes.json``、口径进提示词与两份产物的 meta。"""
    write_material(dirs, rewritten=list(SOURCE_BLOCKS))
    write_profile(dirs, "allow_declared_drops")
    declaration = _declaration(numbers=[NUMBERS[0]], note="示例数据舍弃")
    shortened = f"马克思在《{BOOK}》里写过“{QUOTE}”；另一处是 {NUMBERS[1]} 年。"

    def handler(messages):  # noqa: ANN001 - 假模型：按提示词里的段 id 造决策
        ids = [pid for pid in ("p001", "p002") if f"[{pid}] " in messages[-1]["content"]]
        decisions = []
        for pid in ids:
            entry = {
                "paragraph": pid,
                "action": "keep",
                "why": {"profile": [], "note": f"{pid} 的假依据"},
                "confidence": 0.6,
                "rewrite": "",
            }
            if pid == "p001":
                entry.update({"action": "compress", "rewrite": shortened, "fact_declarations": dict(declaration)})
            decisions.append(entry)
        return json.dumps({"decisions": decisions}, ensure_ascii=False)

    client = FakeChatClient(handler=handler)
    outcome = run_rewrite(SOURCE_ID, dirs.data, dirs.profiles, "local-default", client)

    assert outcome.fact_anchor_policy == "allow_declared_drops"
    assert "pref_fact_anchor" in outcome.fact_anchor_policy_source
    # 提示词里带的是**松口径**那一档（同一读者重跑必得同一段文案）
    assert all(fact_anchor_block("allow_declared_drops") in call[-1]["content"] for call in client.calls)

    changes_payload = json.loads((dirs.data / SOURCE_ID / "changes.json").read_text(encoding="utf-8"))
    decisions_payload = json.loads((dirs.data / SOURCE_ID / "decisions.json").read_text(encoding="utf-8"))
    assert changes_payload["changes"][0]["fact_declarations"] == declaration
    # 与 figure / deferred 同口径：决策记录只留动作与依据，申报只进 changes.json
    assert "fact_declarations" not in decisions_payload["decisions"][0]
    for payload in (changes_payload, decisions_payload):
        assert payload["meta"]["fact_anchor_policy"] == "allow_declared_drops"
    assert changes_payload["fidelity"]["fact_anchor_policy"] == "allow_declared_drops"
    assert changes_payload["fidelity"]["waived"] == [], "改写阶段不许预写放行条目"


def test_rewrite_cli_prints_the_policy_and_the_declarations(run_cli, dirs: SimpleNamespace, monkeypatch) -> None:
    """``ar rewrite`` 的命令行上看得见口径与每段申报了什么。"""
    write_material(dirs, rewritten=list(SOURCE_BLOCKS))
    write_profile(dirs, "allow_declared_drops")
    config_path = dirs.tmp / "config.json"
    config_path.write_text(json.dumps({"model": "fake", "api_key": "fake-key", "retry": {"maxRetries": 0}}), encoding="utf-8")
    declaration = _declaration(quotes=[QUOTE], note="改成转述")

    def handler(messages):  # noqa: ANN001
        ids = [pid for pid in ("p001", "p002") if f"[{pid}] " in messages[-1]["content"]]
        return json.dumps(
            {
                "decisions": [
                    {
                        "paragraph": pid,
                        "action": "compress" if pid == "p001" else "keep",
                        "why": {"profile": [], "note": "假依据"},
                        "confidence": 0.6,
                        "rewrite": f"马克思写过“{QUOTE}”，另一处是 {NUMBERS[0]} 与 {NUMBERS[1]} 年。"
                        if pid == "p001"
                        else "",
                        **({"fact_declarations": dict(declaration)} if pid == "p001" else {}),
                    }
                    for pid in ids
                ]
            },
            ensure_ascii=False,
        )

    from ar import cli

    monkeypatch.setattr(cli, "_make_client", lambda config: FakeChatClient(handler=handler))
    monkeypatch.delenv("AR_API_KEY", raising=False)
    code, output = run_cli(
        "rewrite",
        SOURCE_ID,
        "--reader",
        "local-default",
        "--data-dir",
        str(dirs.data),
        "--profiles-dir",
        str(dirs.profiles),
        "--config",
        str(config_path),
    )

    assert code == EXIT_OK, output
    assert "事实锚口径：allow_declared_drops" in output
    assert "事实锚申报：1 段" in output
    assert "p001" in output and "引号引文 1 项" in output and "改成转述" in output


# ------------------------------------------------------------- 单元：比对内核 --
def test_compare_blocks_detailed_keeps_the_strict_behaviour_by_default() -> None:
    """默认（不传口径 / 不传申报）= 改动前的严格比对，逐字一致。"""
    source = [{"id": "p001", "text": SOURCE_BLOCKS[0]}]
    rewritten = ["这一年有这句话。"]
    outcome = compare_blocks_detailed(source, rewritten)
    assert outcome.policy == "keep_all" and outcome.waived == []
    assert len(outcome.mismatches) == 3
    assert all("没申报" in problem for problem in outcome.problems)


def test_parse_declarations_normalizes_like_the_extractors() -> None:
    declared, notes = parse_declarations(
        {"numbers": ["１８５９"], "quotes": ["  庞大的商品堆积\n"], "books": ["政治经济学批判"], "note": " x "}
    )
    assert declared["numbers"] == ["1859"], "全角数字要按抽取口径归一，否则「抄对了」也对不上"
    assert declared["quotes"] == [QUOTE] and declared["books"] == [BOOK] and declared["note"] == "x"
    assert notes == []

    empty, notes = parse_declarations(None)
    assert empty == {} and notes == []
    broken, notes = parse_declarations(["不是对象"])
    assert broken == {} and notes and "不是对象" in notes[0]
