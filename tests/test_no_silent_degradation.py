# -*- coding: utf-8 -*-
"""静默降级 / 检查器空跑的回归用例（P1–P5，2026-10-05 对抗性复核实测的五处缺陷）。

每个用例都对着一条「看起来正常、其实没做」的事故类型：

- **P1**：``changes.json`` 缺失 / 为空时 ``ar check`` 静默通过（退出码 0）；
- **P2**：模型整批失败时整条链退出码 0/0（「假装成功」）；
- **P3**：``paragraphs: []`` + 空文档 → 检查器空跑（0 比对项）却报「通过」；
- **P4**：``fidelity.checked: true`` 在比对之前就写进 rewrite 产物；
- **P5**：降级文案宣称「并标 deferred」，而产物里 deferred 为 false。

范式照 ``学习/_整理/selfcheck.py``：**只在临时副本上注入缺陷**（交付产物一个字节不动）、
断言「注入确实生效」、只看退出码。P1/P2/P3 都是负向对照（注入后必须非零退出）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ar.errors import EXIT_MODEL_PARTIAL, EXIT_OK, EXIT_USAGE, EXIT_VALIDATION
from ar.fidelity import compare_blocks
from ar.rewrite import CheckOutcome, check_fidelity, fidelity_problems, run_rewrite
from test_rewrite import (  # noqa: I001 - 同一 tests 目录里的共享素材与夹具
    FAKE_NUMBERS,
    batch_text,
    prompt_ids,
    _copy_doc_dir,
    fake_handler_for,
    fake_run,
    rewrite_env,
    run_cli_with_fake,
    sample_fake_handler,
    write_fake_doc,
)

#: 未知动作（模型没给 action）与枚举内未实现动作混在一起的假模型输出
UNKNOWN_ACTION = "frobnicate"


# ------------------------------------------------------------------- 工具 --
def write_empty_doc(dirs: SimpleNamespace, source_id: str = "empty-doc") -> Path:
    """造一份「空文档」现场：空 source.md + 空 rewritten.md + ``paragraphs: []``。

    ``ingest.hash`` 按空字节算，锚点校验才不会被「来源哈希不一致」提前拦下——
    这个用例要考的正是「一个锚点都没比也算通过」。
    """
    target = dirs.data / source_id
    target.mkdir(parents=True, exist_ok=True)
    payload = b""
    (target / "source.md").write_bytes(payload)
    (target / "source.meta.json").write_text(
        json.dumps(
            {
                "source_id": source_id,
                "ingest": {
                    "kind": "local_file",
                    "path": "(回归用例的空文档)",
                    "hash": "sha256:" + hashlib.sha256(payload).hexdigest(),
                    "at": "2026-10-05T00:00:00+08:00",
                },
                "paragraphs": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (target / "rewritten.md").write_text("", encoding="utf-8")
    (target / "changes.json").write_text(
        json.dumps({"changes": [], "fidelity": {"checked": False, "mismatches": []}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return target


def read_fidelity(target: Path) -> dict:
    return json.loads((target / "changes.json").read_text(encoding="utf-8"))["fidelity"]


# --------------------------------------------------------------------- P1 --
def test_p1_missing_changes_json_must_not_pass(run_cli, dirs: SimpleNamespace, fake_chat_client) -> None:
    """负向对照：删掉副本里的 ``changes.json`` → ``check`` 不许报通过，且要点名缺了什么。"""
    doc = write_fake_doc(dirs)
    fake_run(dirs, fake_chat_client(handler=fake_handler_for(doc)))
    copy = _copy_doc_dir(dirs.data, dirs.tmp / "p1-missing")
    changes = copy / "fake-doc" / "changes.json"

    changes.unlink()
    assert not changes.exists(), "注入未生效（changes.json 还在）"

    code, output = run_cli("--data-dir", str(copy), "check", "fake-doc")
    assert code != EXIT_OK, f"changes.json 没了却报通过：\n{output}"
    assert code == EXIT_USAGE, output  # 与「rewritten.md 缺失」同码
    assert "changes.json" in output, output
    assert "不静默通过" in output, output

    # 换成「存在但为空」的变更记录 → 仍然不许通过（「没得对」不等于「没问题」）
    changes.write_text(json.dumps({"changes": []}, ensure_ascii=False), encoding="utf-8")
    code, output = run_cli("--data-dir", str(copy), "check", "fake-doc")
    assert code != EXIT_OK, output
    assert "空数组" in output, output


@pytest.mark.parametrize("payload", ["[]", '{"changes": []}', '{"changes": {}}'])
def test_p1_empty_or_wrong_shape_changes_json_must_not_pass(
    run_cli, dirs: SimpleNamespace, fake_chat_client, payload: str
) -> None:
    """负向对照：``changes.json`` 是空 / 形态不对（没有逐段变更账）→ 非零退出。"""
    doc = write_fake_doc(dirs)
    fake_run(dirs, fake_chat_client(handler=fake_handler_for(doc)))
    copy = _copy_doc_dir(dirs.data, dirs.tmp / "p1-shape")
    changes = copy / "fake-doc" / "changes.json"

    original = changes.read_text(encoding="utf-8")
    changes.write_text(payload, encoding="utf-8")
    assert changes.read_text(encoding="utf-8") != original, "注入未生效"

    code, output = run_cli("--data-dir", str(copy), "check", "fake-doc")
    assert code == EXIT_USAGE, output
    assert "changes.json" in output, output

    # 还原原始记录 → 必须重新通过（否则说明这个用例根本没测到自己想测的东西）
    changes.write_text(original, encoding="utf-8")
    code, output = run_cli("--data-dir", str(copy), "check", "fake-doc")
    assert code == EXIT_OK, output


def test_p1_truncated_changes_record_must_not_pass(run_cli, dirs: SimpleNamespace, fake_chat_client) -> None:
    """负向对照：``changes`` 被截断（条数与段数对不上）→ 同样不许报通过。

    ``run_rewrite`` 保证「每段一条」（缺决策的段也会补一条），所以条数对不上只可能是
    产物被截断或与材料版本不匹配，不是正常的产物形态。
    """
    doc = write_fake_doc(dirs)
    fake_run(dirs, fake_chat_client(handler=fake_handler_for(doc)))
    copy = _copy_doc_dir(dirs.data, dirs.tmp / "p1-truncated")
    changes = copy / "fake-doc" / "changes.json"
    payload = json.loads(changes.read_text(encoding="utf-8"))
    assert len(payload["changes"]) == len(doc["paragraphs"]) == 7, "正常产物应当每段一条"

    payload["changes"] = payload["changes"][:3]
    changes.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert len(json.loads(changes.read_text(encoding="utf-8"))["changes"]) == 3, "注入未生效"

    code, output = run_cli("--data-dir", str(copy), "check", "fake-doc")
    assert code == EXIT_USAGE, output
    assert "3 条" in output and "7 段" in output, output


def test_p1_changes_json_missing_keeps_fidelity_mismatches_reported_too(
    run_cli, dirs: SimpleNamespace, fake_chat_client
) -> None:
    """产物缺失不该盖掉事实锚违规：先报「缺产物」还是先报「改坏了」都要看得见。

    这里固定的是**优先级**：产物缺失（2）先出，事实锚违规（4）在同一份副本里也真实存在
    （由 ``check_fidelity`` 的 outcome 同时给出，见下一个断言）。
    """
    doc = write_fake_doc(dirs)
    fake_run(dirs, fake_chat_client(handler=fake_handler_for(doc)))
    copy = _copy_doc_dir(dirs.data, dirs.tmp / "p1-both")
    target = copy / "fake-doc"
    (target / "changes.json").unlink()
    rewritten = target / "rewritten.md"
    original = rewritten.read_text(encoding="utf-8")
    mutated = original.replace(FAKE_NUMBERS[0] + " 年", "1858 年")
    assert mutated != original, "注入未生效（没找到要改的数字）"
    rewritten.write_text(mutated, encoding="utf-8")

    outcome = check_fidelity("fake-doc", copy)
    assert outcome.problems, "产物缺失没有被 CheckOutcome 收集（P1 的死变量又回来了）"
    assert outcome.mismatches, "事实锚违规没有被收集"

    code, output = run_cli("--data-dir", str(copy), "check", "fake-doc")
    assert code == EXIT_USAGE, output
    assert "changes.json" in output


def test_fidelity_problems_can_render_persisted_mismatches() -> None:
    """``fidelity_problems`` 也能只凭产物里的 ``mismatches`` 渲染（手工构造 outcome 的路径）。

    正常路径用 ``check_fidelity`` 直接给的 ``fact_problems``；这条守住「从持久化的
    ``changes.json`` 也能还原人读文案」的能力，避免那半边代码变成没人走的死分支。
    """
    outcome = CheckOutcome(
        source_id="doc",
        counts={"paragraphs": 2, "compared": 1, "numbers": 1, "quotes": 0, "books": 0},
        mismatches=[
            {
                "paragraph": "p001",
                "kind": "numbers",
                "source": ["1859"],
                "rewritten": ["1858"],
                "missing": ["1859"],
                "extra": ["1858"],
                "detail": "数字与原文不一致",
            }
        ],
    )
    assert fidelity_problems(outcome) == ["p001：数字与原文不一致（改写稿里少了 「1859」；改写稿里多出 「1858」）"]


# --------------------------------------------------------------------- P2 --
def test_p2_whole_batch_failure_exits_nonzero_with_evidence(
    run_cli, rewrite_env: SimpleNamespace, fake_chat_client
) -> None:
    """负向对照：模型整批必抛 → ``rewrite`` 退出码 6、产物留证、stdout 点名受影响段。"""

    def handler(messages):  # noqa: ANN001
        raise RuntimeError("网关不可用（回归用例假模型）")

    client = fake_chat_client(handler=handler)
    code, output = run_cli_with_fake(run_cli, rewrite_env, client, "rewrite", "sample")

    assert code == EXIT_MODEL_PARTIAL, output
    assert "改写不完整" in output, output
    assert "未被改写" in output, output
    assert "p001" in output and "p006" in output, output  # 点名受影响段
    assert "重跑" in output, output

    payload = json.loads((rewrite_env.target / "changes.json").read_text(encoding="utf-8"))
    errors = payload["meta"]["model_errors"]
    assert errors, "产物里没有 model_errors：失败批次没有留下可读证据"
    assert errors[0]["batch"] and errors[0]["paragraphs"] and errors[0]["error"], errors
    # 批次错误文案统一走 ar.llm.failure_message：带尝试次数与末次错误码（策略是「不重试」→ 已试 1 次）
    assert "已试 1 次，末次 UNKNOWN" in errors[0]["error"], errors[0]["error"]
    assert set(errors[0]["paragraphs"]) == {"p001", "p002", "p003", "p004", "p005", "p006"}
    # 失败归失败，变更账仍然是「每段一条」（check 的完整性校验以此为据）
    assert len(payload["changes"]) == 6
    assert payload["meta"]["rewrite_incomplete"] is True
    assert payload["meta"]["unplanned_paragraphs"], "模型没给决策的段没有被记下来"
    assert payload["fidelity"]["checked"] is False
    assert any("改写不完整" in item for item in payload["meta"]["degraded"])

    # 承接这份产物的 `check`：事实锚全对也必须以同一个码报「不完整」
    code, output = run_cli_with_fake(run_cli, rewrite_env, client, "check", "sample")
    assert code == EXIT_MODEL_PARTIAL, output
    assert "改写不完整" in output and "p001" in output, output


def test_p2_partial_batch_failure_also_exits_nonzero(dirs: SimpleNamespace, fake_chat_client) -> None:
    """只有一批坏掉时也是「有段落没被改写」：不许因为别批成功就算成功。"""
    doc = write_fake_doc(dirs)
    ok_handler = fake_handler_for(doc)

    def handler(messages):  # noqa: ANN001
        prompt = messages[-1]["content"]
        # 认准「这一段在本批里」：`[p001] ` 只会出现在段落块里（提示词的 JSON 例子里是 "paragraph":"p001"）
        if "[p001] " in batch_text(prompt):
            raise RuntimeError("这一批的网关抖了（回归用例假模型）")
        return ok_handler(messages)

    client = fake_chat_client(handler=handler)
    # 每 40 字符一批，让「只有一部分批次失败」真的可构造（默认 1100 字符会把 7 段并成一批）
    outcome = run_rewrite(
        "fake-doc", dirs.data, dirs.profiles, "local-default", client, max_chars_per_batch=40
    )
    assert outcome.model_errors, "批次失败没有被记进 model_errors"
    assert len(outcome.model_errors) < outcome.batch_count, "这个用例需要「部分批次失败」的现场"
    assert outcome.incomplete is True
    assert "p001" in outcome.unplanned_paragraphs
    # 没失败的那几批照常改写：产物可用（不是全废），但退出码必须报 6
    assert any(decision.action == "expand" for decision in outcome.decisions)
    fidelity = read_fidelity(dirs.data / "fake-doc")
    assert fidelity["checked"] is False


def test_p2_missing_decisions_without_batch_failure_also_exit_nonzero(
    dirs: SimpleNamespace, fake_chat_client
) -> None:
    """模型没报错、但少给了决策 → 那些段同样「一个字没改」，也要非零退出。"""
    write_fake_doc(dirs)

    def handler(messages):  # noqa: ANN001
        ids = prompt_ids(messages[-1]["content"])
        return json.dumps(
            {
                "decisions": [
                    {
                        "paragraph": ids[0],
                        "action": "keep",
                        "why": {"profile": [], "note": "只回第一段"},
                        "confidence": 0.9,
                    }
                ]
            },
            ensure_ascii=False,
        )

    client = fake_chat_client(handler=handler)
    outcome = fake_run(dirs, client)
    assert outcome.model_errors == [], "这一路不该有批次失败"
    assert outcome.unplanned_paragraphs, "模型没给决策的段没有被记下来"
    assert outcome.incomplete is True
    assert any("改写不完整" in item for item in outcome.degraded)
    fidelity = read_fidelity(dirs.data / "fake-doc")
    assert fidelity["checked"] is False


def test_p2_check_reflects_injected_model_errors_in_a_copy(
    run_cli, dirs: SimpleNamespace, fake_chat_client
) -> None:
    """负向对照（副本注入）：事实锚全对，但产物里记着「有批次失败」→ ``check`` 必须非零退出。

    这条固定的是 `check` 侧的口径：`check` 不许只看事实锚就报「通过」——它得读
    ``changes.json`` 的 ``meta.model_errors``，把「有段落没被改写」这件事一起报出来。
    """
    doc = write_fake_doc(dirs)
    fake_run(dirs, fake_chat_client(handler=fake_handler_for(doc)))
    copy = _copy_doc_dir(dirs.data, dirs.tmp / "p2-injected")
    changes = copy / "fake-doc" / "changes.json"

    # 基线：这份副本本来是干净的（事实锚全对、没有 model_errors）
    code, output = run_cli("--data-dir", str(copy), "check", "fake-doc")
    assert code == EXIT_OK, output

    payload = json.loads(changes.read_text(encoding="utf-8"))
    assert payload["meta"].get("model_errors") in (None, []), "基线产物不该有 model_errors"
    payload["meta"]["model_errors"] = [
        {"batch": "第 1–3 段", "paragraphs": ["p001", "p002", "p003"], "error": "模型调用失败（注入）"}
    ]
    payload["meta"]["unplanned_paragraphs"] = ["p001", "p002", "p003"]
    changes.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert json.loads(changes.read_text(encoding="utf-8"))["meta"]["model_errors"], "注入未生效"

    code, output = run_cli("--data-dir", str(copy), "check", "fake-doc")
    assert code == EXIT_MODEL_PARTIAL, output
    assert "改写不完整" in output and "p001" in output, output
    assert "事实锚校验通过" not in output, "事实锚全对也不能打印「通过」"


# --------------------------------------------------------------------- P3 --
def test_p3_empty_document_is_checker_idle_failure(run_cli, dirs: SimpleNamespace) -> None:
    """负向对照：空文档（0 比对项）→ ``check`` 非零退出，措辞必须点出「检查器空跑」。"""
    write_empty_doc(dirs)
    code, output = run_cli("--data-dir", str(dirs.data), "check", "empty-doc")
    assert code != EXIT_OK, f"空文档被当成「校验通过」：\n{output}"
    assert code == EXIT_VALIDATION, output
    assert "检查器空跑" in output, output
    assert "0 段" in output or "0 比对项" in output, output


def test_p3_compare_blocks_zero_items_is_a_problem() -> None:
    """纯函数面：0 个段落进入比对时必须产出 ``empty_check`` 违规、``compared`` 归零。"""
    problems, mismatches, counts = compare_blocks([], [])
    assert counts["compared"] == 0
    assert any("检查器空跑" in problem for problem in problems), problems
    assert any(item["kind"] == "empty_check" for item in mismatches), mismatches

    # 原文有段、改写稿空：段缺失照样报（且同样一个都没真正比对）
    doc_problems, doc_mismatches, doc_counts = compare_blocks(
        [{"id": "p001", "text": "第一段。"}], []
    )
    assert doc_counts["compared"] == 0 and doc_counts["paragraphs"] == 1
    assert any(item["kind"] == "empty_check" for item in doc_mismatches)
    assert any(item["kind"] == "paragraph_missing" for item in doc_mismatches)
    assert any("p001" in problem for problem in doc_problems)


def test_p3_check_fidelity_never_writes_checked_true_for_zero_items(dirs: SimpleNamespace) -> None:
    """``checked: true`` 只在比对项 > 0 时成立（0 比对项不许写成「已查过、干净」）。"""
    target = write_empty_doc(dirs)
    outcome = check_fidelity("empty-doc", dirs.data)

    assert outcome.checked is False
    assert any("检查器空跑" in problem for problem in fidelity_problems(outcome))
    fidelity = read_fidelity(target)
    assert fidelity["checked"] is False, "0 比对项却写成了 checked: true"
    assert fidelity["counts"]["compared"] == 0
    assert fidelity["mismatches"], "检查器空跑的结论必须写进产物（留痕）"


# --------------------------------------------------------------------- P4 --
def test_p4_checked_flips_only_after_check(
    run_cli, rewrite_env: SimpleNamespace, fake_chat_client, sample_fake_handler
) -> None:
    """``rewrite`` 落 ``checked: false``；``check`` 通过后才写 ``true`` + ``counts``。"""
    client = fake_chat_client(handler=sample_fake_handler)
    code, output = run_cli_with_fake(run_cli, rewrite_env, client, "rewrite", "sample")
    assert code == EXIT_OK, output

    fidelity = read_fidelity(rewrite_env.target)
    assert fidelity["checked"] is False, "rewrite 阶段就写了 checked: true（误导性产物）"
    assert fidelity["counts"] is None, "rewrite 阶段不该有 counts（那时候还没比过）"
    assert "ar check" in fidelity["summary"]

    code, output = run_cli_with_fake(run_cli, rewrite_env, client, "check", "sample")
    assert code == EXIT_OK, output
    fidelity = read_fidelity(rewrite_env.target)
    assert fidelity["checked"] is True
    assert fidelity["counts"]["compared"] == 6
    assert fidelity["mismatches"] == []


# --------------------------------------------------------------------- P5 --
def test_p5_unknown_action_wording_matches_artifact(dirs: SimpleNamespace, fake_chat_client) -> None:
    """未知动作（含模型没给 action）按保持处理、**不标 deferred**，文案必须与产物一致。

    P5 的原始样本（拿 ``compress`` 当「枚举内未实现」的动作）在 2026-10-13 之后不成立了
    ——四个动作都已实现、``deferred`` 不再有对象。现在守的是同一件事的另一半：
    **枚举外的动作文案说「未标 deferred」，产物里就必须真的没有 deferred 键**；
    而 ``deferred`` 机制本身仍在（``ar.rewrite.UNIMPLEMENTED_ACTIONS`` 空元组 + 那段文案都保留），
    这里用一条直调 ``_finalize_items`` 的记账断言把它钉住。
    """

    def handler(messages):  # noqa: ANN001
        ids = prompt_ids(messages[-1]["content"])
        decisions = []
        for pid in ids:
            if pid == "p001":
                action = ""  # 模型没给 action
            elif pid == "p002":
                action = UNKNOWN_ACTION  # 枚举外的动作
            else:
                action = "keep"
            decisions.append(
                {
                    "paragraph": pid,
                    "action": action,
                    "why": {"profile": [], "note": f"{pid} 的假依据"},
                    "confidence": 0.6,
                    "rewrite": "",
                }
            )
        return json.dumps({"decisions": decisions}, ensure_ascii=False)

    doc = write_fake_doc(dirs)
    outcome = fake_run(dirs, fake_chat_client(handler=handler))
    assert len(outcome.decisions) == len(doc["paragraphs"])

    by_id = {change["paragraph"]: change for change in outcome.changes}
    # 产物侧：未知动作 → keep 且**没有** deferred 键
    assert by_id["p001"]["action"] == "keep" and "deferred" not in by_id["p001"]
    assert by_id["p002"]["action"] == "keep" and "deferred" not in by_id["p002"]

    # 文案侧：声称「未标 deferred」的条目，产物里就必须真的没有 deferred 键（不许对不上）
    for pid in ("p001", "p002"):
        lines = [line for line in outcome.degraded if line.startswith(f"{pid}：")]
        assert lines, f"{pid} 的降级没有被记下来（静默降级）"
        for line in lines:
            assert "未标 deferred" in line, f"文案与产物不一致（产物没有 deferred 键）：{line}"
    assert not [line for line in outcome.degraded if "并标 deferred" in line], "四个动作都实现了，不该再有 deferred 文案"
    # 未知动作的说明写在 why.note 里，页面上也看得见
    assert "不在四类之内" in by_id["p001"]["why"]["note"]
    assert "deferred" in by_id["p001"]["why"]["note"]


def test_p5_deferred_mechanism_still_writes_true_only_for_unimplemented_actions() -> None:
    """``deferred`` 机制保留（文案与字段都不删）：只有**枚举内未实现**的动作才标它。

    当前 ``UNIMPLEMENTED_ACTIONS`` 是空元组（四个动作都实现了），所以产物里不会再出现
    ``deferred: true``；这条用直调 ``_finalize_items`` 把「机制还在、只是没有对象」钉住——
    将来真有枚举内未实现的动作时，它必须重新回到「按 keep + 标 deferred + 文案一致」。
    """
    from ar.rewrite import UNIMPLEMENTED_ACTIONS, _finalize_items

    assert UNIMPLEMENTED_ACTIONS == ()
    degraded: list[str] = []
    paragraph = {"id": "p001", "text": "第一段原文。"}
    item = _finalize_items(
        paragraph,
        {"paragraph": "p001", "action": "keep", "why": {"profile": [], "note": "假依据"}, "confidence": 0.5, "rewrite": ""},
        degraded=degraded,
    )
    assert item.decision.deferred is False and degraded == []
