"""M1：摄取与锚点稳定性 + 锚点校验的负向对照。

负向对照照 ``学习/_整理/selfcheck.py`` 的做法：**只在临时副本上注入缺陷**、
断言「注入确实生效」（字符串真的替换了）、然后**只看退出码**（并在有问题时打印输出）。
交付产物本身在任何用例里都不会被改动。
"""

from __future__ import annotations

import errno
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from ar.errors import EXIT_MODEL, EXIT_OK, EXIT_VALIDATION, EXIT_USAGE, WriteError
from ar.ingest import (
    anchor_diff_warnings,
    ingest,
    normalize_text,
    paragraph_id,
    split_paragraphs,
    text_hash,
)


# ------------------------------------------------------------------ 切分规则 --
def test_split_paragraphs_keeps_front_matter_blocks_as_paragraphs(sample_md: Path) -> None:
    """契约 §4：段落 = 空行分隔的块；Markdown 标题 / 引用 / 分隔线同样是段落。

    这条必须与前端夹具（``samples/*/doc.json``，段号 p001 = 标题）逐一对齐：
    锚点是全系统骨架，不能在切分这里做特殊处理。
    """
    paragraphs = split_paragraphs(sample_md.read_text(encoding="utf-8"))
    assert len(paragraphs) == 6, paragraphs
    assert paragraphs[0].startswith("# 样例标题")
    assert paragraphs[1].startswith("> 出处说明")
    assert paragraphs[2] == "---"
    assert paragraphs[3].startswith("第一段")
    assert paragraphs[5] == "第三段。"


def test_split_paragraphs_handles_crlf_and_multiple_blank_lines() -> None:
    raw = "# 标题\r\n\r\n正文一。\r\n\r\n\r\n正文二。\r\n"
    assert split_paragraphs(raw) == ["# 标题", "正文一。", "正文二。"]


def test_normalize_text_collapses_whitespace_and_fullwidth_space() -> None:
    assert normalize_text("  甲\u3000\u3000乙 \n 丙  ") == "甲 乙 丙"


def test_text_hash_and_paragraph_id_shape() -> None:
    digest = text_hash("甲乙")
    assert len(digest) == 8 and all(c in "0123456789abcdef" for c in digest)
    # 规范化后相同 → 哈希相同；不同 → 不同
    assert text_hash("甲 乙") == text_hash("甲\u3000乙")
    assert text_hash("甲") != text_hash("乙")
    assert paragraph_id(1) == "p001" and paragraph_id(22) == "p022"
    with pytest.raises(ValueError):
        paragraph_id(0)


# -------------------------------------------------------------------- 摄取 --
def test_ingest_writes_verbatim_source_and_meta(sample_md: Path, dirs: SimpleNamespace) -> None:
    result = ingest(sample_md, "sample", dirs.data)
    target = dirs.data / "sample"

    # source.md 与源文件逐字节一致（原样、只读）
    assert (target / "source.md").read_bytes() == sample_md.read_bytes()

    meta = json.loads((target / "source.meta.json").read_text(encoding="utf-8"))
    assert meta["source_id"] == "sample"
    assert meta["ingest"]["kind"] == "local_file"
    assert meta["ingest"]["path"] == str(sample_md.resolve())
    assert meta["ingest"]["hash"].startswith("sha256:")
    assert len(meta["paragraphs"]) == 6
    assert [p["id"] for p in meta["paragraphs"]] == [f"p{i:03d}" for i in range(1, 7)]
    assert [p["index"] for p in meta["paragraphs"]] == [1, 2, 3, 4, 5, 6]
    for entry in meta["paragraphs"]:
        assert set(entry) == {"id", "index", "text", "text_hash"}  # DESIGN §4.1 的字段，逐字不改名
        assert entry["text_hash"] == text_hash(entry["text"])
    assert result.warnings == []


def test_ingest_source_md_goes_through_the_shared_atomic_path(
    sample_md: Path, dirs: SimpleNamespace
) -> None:
    """`source.md` 走 `ar.jsonio.write_bytes`（全项目唯一一份写盘实现）：逐字节 + 不留临时文件。

    2026-10-14 复核补漏前，这里是本模块自己的 `tempfile.mkstemp` 循环（`_write_bytes`），
    与 `jsonio` 那套「有界重试 + 坏路径快速失败」是两份逻辑——本用例钉住「只剩一份」：
    临时文件命名 / 清理口径与 `ar.jsonio` 完全一致（`.tmp-*` 前缀），跑完目录里一个不剩。
    """
    target = dirs.data / "sample"
    ingest(sample_md, "sample", dirs.data)

    assert (target / "source.md").read_bytes() == sample_md.read_bytes()
    assert (target / "source.meta.json").is_file()
    leftovers = [p.name for p in target.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == [], f"原子写不许留下候选临时文件：{leftovers}"


def test_ingest_unwritable_dir_fails_fast_with_one_attempt_and_writes_no_meta(
    sample_md: Path, dirs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**坏目录快速失败**（2026-10-14 复核揪出的第二个漏网入口）：一次就失败、两份产物都不落。

    打桩打在「系统在写的那一刻拒绝」这个真实失败点上（只拦目标目录里的候选临时文件，
    其余路径转交真的 `os.open`），并**数尝试次数**——旧写法在真不可写目录上实测 1 秒里
    试了 5000 多个候选名仍在循环（`tempfile.TMP_MAX = 2147483647`，近似无界）。
    """
    target = dirs.data / "sample"
    target.mkdir(parents=True)  # 先备好产物目录（模拟「目录在、但不可写」）
    real_open = os.open
    attempts: list[str] = []

    def fake_open(path, flags, mode=0o777):
        if Path(path).parent == target:
            attempts.append(str(path))
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_open(path, flags, mode)

    monkeypatch.setattr("ar.jsonio.os.open", fake_open)

    with pytest.raises(WriteError) as excinfo:
        ingest(sample_md, "sample", dirs.data)

    message = str(excinfo.value)
    assert str(target) in message, f"错误里必须点名是哪个目录：{message}"
    assert f"errno {errno.EACCES}" in message, f"错误里必须带原始 errno：{message}"
    assert len(attempts) == 1, f"目录不可写必须一次就失败，实测试了 {len(attempts)} 次"
    assert excinfo.value.exit_code == EXIT_USAGE, "CLI 上归「用法 / 输入错误」那一档（退出码 2）"
    assert not (target / "source.md").exists()
    assert not (target / "source.meta.json").exists(), "第一份就写不进去：不许继续写第二份"
    assert [p.name for p in target.iterdir() if p.name.startswith(".tmp-")] == []


def test_ingest_cli_reports_the_unwritable_dir_and_exits_2(
    sample_md: Path, dirs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, run_cli
) -> None:
    """CLI 口径：坏目录上 `ar ingest` 打一句可读错误并以**退出码 2** 结束（不是卡住、不是 1）。"""
    target = dirs.data / "sample"
    target.mkdir(parents=True)
    real_open = os.open

    def fake_open(path, flags, mode=0o777):
        if Path(path).parent == target:
            raise PermissionError(errno.EACCES, "拒绝访问")
        return real_open(path, flags, mode)

    monkeypatch.setattr("ar.jsonio.os.open", fake_open)

    code, output = run_cli(
        "ingest", str(sample_md), "--id", "sample", "--data-dir", str(dirs.data)
    )

    assert code == EXIT_USAGE, output
    assert "不可写" in output and "errno 13" in output, output
    assert "Traceback" not in output, "坏路径必须是可读错误，不是裸异常回溯"


def test_ingest_is_stable_across_two_runs(sample_md: Path, dirs: SimpleNamespace) -> None:
    """验收 1：同一文件跑两次，除 ``ingest.at`` 外完全一致。"""
    first = ingest(sample_md, "sample", dirs.data / "run1")
    second = ingest(sample_md, "sample", dirs.data / "run2")

    def strip_at(meta: dict) -> dict:
        meta = json.loads(json.dumps(meta))
        meta["ingest"].pop("at")
        return meta

    assert strip_at(first.meta) == strip_at(second.meta)
    assert [p["id"] for p in first.meta["paragraphs"]] == [p["id"] for p in second.meta["paragraphs"]]
    assert [p["text_hash"] for p in first.meta["paragraphs"]] == [
        p["text_hash"] for p in second.meta["paragraphs"]
    ]


def test_reingest_same_file_reports_stable_anchors(sample_md: Path, dirs: SimpleNamespace) -> None:
    ingest(sample_md, "sample", dirs.data)
    again = ingest(sample_md, "sample", dirs.data)
    assert again.previous_present is True
    assert again.warnings == []


def test_reingest_changed_file_warns_with_ids(tmp_path: Path, dirs: SimpleNamespace) -> None:
    path = tmp_path / "doc.md"
    path.write_text("第一段。\n\n第二段。\n", encoding="utf-8")
    ingest(path, "doc", dirs.data)
    path.write_text("第一段改过。\n\n第二段。\n\n第三段新增。\n", encoding="utf-8")
    result = ingest(path, "doc", dirs.data)
    joined = " / ".join(result.warnings)
    assert "p001" in joined and "text_hash 不同" in joined
    assert "p003" in joined and "新增段落" in joined


def test_anchor_diff_warnings_empty_without_previous() -> None:
    assert anchor_diff_warnings(None, {"paragraphs": [{"id": "p001", "text_hash": "x"}]}) == []


def test_ingest_rejects_missing_file_and_empty_document(tmp_path: Path, dirs: SimpleNamespace) -> None:
    from ar.errors import InputError

    with pytest.raises(InputError):
        ingest(tmp_path / "nope.md", "nope", dirs.data)

    empty = tmp_path / "empty.md"
    empty.write_text("\n\n   \n\n", encoding="utf-8")
    with pytest.raises(InputError, match="没有可摄取"):
        ingest(empty, "empty", dirs.data)


def test_ingest_keeps_document_head_blocks(tmp_path: Path, dirs: SimpleNamespace) -> None:
    """只有标题/引用/分隔线的文件也有 3 段（切分不丢块，段号才能与前端夹具一致）。"""
    doc = tmp_path / "head.md"
    doc.write_text("# 只有标题\n\n> 只有引用\n\n---\n", encoding="utf-8")
    result = ingest(doc, "head", dirs.data)
    assert [p["text"] for p in result.paragraphs] == ["# 只有标题", "> 只有引用", "---"]


def test_ingest_rejects_non_utf8(tmp_path: Path, dirs: SimpleNamespace) -> None:
    from ar.errors import InputError

    bad = tmp_path / "gbk.md"
    bad.write_bytes("中文".encode("gbk"))
    with pytest.raises(InputError, match="UTF-8"):
        ingest(bad, "bad", dirs.data)


# ------------------------------------------------------------- 真实样例材料 --
def test_real_sample_counts(real_sample: Path, dirs: SimpleNamespace) -> None:
    """契约 §7 的材料：5,341 字符；按空行切分 25 块（文档头 3 + 正文 22）。"""
    result = ingest(real_sample, "capital-v1-ch01", dirs.data)
    paragraphs = result.paragraphs
    assert len(paragraphs) == 25
    # 字符数按 UTF-8 解码后的原样文本算（含 \r\n；read_text 会把 CRLF 归一成 LF，故用 bytes）
    assert len(real_sample.read_bytes().decode("utf-8")) == 5341
    assert paragraphs[0]["id"] == "p001" and paragraphs[-1]["id"] == "p025"
    assert paragraphs[0]["text"].startswith("# 第一篇 商品和货币")  # p001 = 标题
    assert paragraphs[2]["text"] == "---"
    assert paragraphs[3]["text"].startswith("1．商品的两个因素")  # 正文第一块在 p004
    duplicates = [p for p in paragraphs if len(p["text"]) == 429]
    assert len(duplicates) == 2  # 爬虫伪重复：两段内容相同
    assert duplicates[0]["text"] == duplicates[1]["text"]
    assert duplicates[0]["text_hash"] == duplicates[1]["text_hash"]


def test_real_sample_ingest_twice_identical(real_sample: Path, dirs: SimpleNamespace) -> None:
    """验收 2：同一文件跑两次，段落 id 与 text_hash 完全一致。"""
    first = ingest(real_sample, "capital-v1-ch01", dirs.data)
    second = ingest(real_sample, "capital-v1-ch01", dirs.data)
    assert [p["id"] for p in first.paragraphs] == [p["id"] for p in second.paragraphs]
    assert [p["text_hash"] for p in first.paragraphs] == [p["text_hash"] for p in second.paragraphs]
    assert second.warnings == []
    assert first.meta["ingest"]["hash"] == second.meta["ingest"]["hash"]


def test_real_sample_anchors_match_frontend_fixture(real_sample: Path, dirs: SimpleNamespace) -> None:
    """与前端夹具（另一路 W2 的 samples/<id>/doc.json）段号/哈希逐一对齐。

    夹具不存在时跳过；一旦两边切分规则分叉，这条会红——锚点是全系统骨架，不能分叉。
    """
    fixture = Path(__file__).resolve().parents[1] / "samples" / "capital-v1-ch01" / "doc.json"
    if not fixture.is_file():
        pytest.skip(f"前端夹具不存在：{fixture}")
    fixture_meta = json.loads(fixture.read_text(encoding="utf-8"))["meta"]
    result = ingest(real_sample, "capital-v1-ch01", dirs.data)
    mine = [(p["id"], p["text_hash"], p["text"]) for p in result.paragraphs]
    theirs = [(p["id"], p["text_hash"], p["text"]) for p in fixture_meta["paragraphs"]]
    assert mine == theirs


# ------------------------------------------------------------- 负向对照（核心） --
def _copy_doc(source: Path, target: Path) -> Path:
    """把一份摄取产物复制到临时目录（绝不动交付产物本身）。"""
    shutil.copytree(source, target)
    return target


def _run_check(run_cli, doc_dir: Path, source_id: str | None = None) -> tuple[int, str]:
    """对某个产物目录跑 ``ar check``（data-dir 取它的上一级，source_id 取目录名）。"""
    return run_cli("--data-dir", str(doc_dir.parent), "check", source_id or doc_dir.name)


def test_negative_tampered_text_hash_is_caught(run_cli, sample_md: Path, dirs: SimpleNamespace) -> None:
    """验收 3：改掉某段 text_hash → 校验入口必须报错并点名段 id。"""
    ingest(sample_md, "sample", dirs.data)
    copy = _copy_doc(dirs.data / "sample", dirs.tmp / "tampered")
    meta_path = copy / "source.meta.json"

    original = meta_path.read_text(encoding="utf-8")
    victim_index = 4  # 第 5 块（p005："第二段。"）
    victim = json.loads(original)["paragraphs"][victim_index]["text_hash"]
    mutated = original.replace(f'"text_hash": "{victim}"', '"text_hash": "deadbeef"', 1)
    assert victim and mutated != original, "注入未生效（未找到可改之处）"
    meta_path.write_text(mutated, encoding="utf-8")
    # 断言注入确实落到 p005 上（而不只是字符串变了）
    assert json.loads(meta_path.read_text(encoding="utf-8"))["paragraphs"][victim_index]["text_hash"] == "deadbeef"

    code, output = _run_check(run_cli, copy)
    assert code == EXIT_VALIDATION, output
    assert "p005" in output, output
    assert "text_hash 不一致" in output, output


def test_negative_modified_source_is_caught(run_cli, sample_md: Path, dirs: SimpleNamespace) -> None:
    """把 source.md 改一个字 → 来源哈希与段文本都要报出来。"""
    ingest(sample_md, "sample", dirs.data)
    copy = _copy_doc(dirs.data / "sample", dirs.tmp / "changed")
    source_path = copy / "source.md"
    original = source_path.read_text(encoding="utf-8")
    mutated = original.replace("第二段。", "第二段改。")
    assert mutated != original, "注入未生效"
    source_path.write_text(mutated, encoding="utf-8")

    code, output = _run_check(run_cli, copy)
    assert code == EXIT_VALIDATION, output
    assert "p005" in output, output
    assert "来源哈希不一致" in output or "text_hash 不一致" in output, output


def test_negative_missing_meta_is_usage_error(run_cli, dirs: SimpleNamespace) -> None:
    code, output = run_cli("--data-dir", str(dirs.data), "check", "nosuchdoc")
    assert code == EXIT_VALIDATION, output
    assert "source.meta.json" in output


def test_positive_check_passes_anchors_then_asks_for_rewrite(run_cli, sample_md: Path, dirs: SimpleNamespace) -> None:
    """锚点没问题、但还没改写稿时：check 以退出码 2 结束并说清要先跑 rewrite（不假装成功）。"""
    ingest(sample_md, "sample", dirs.data)
    code, output = run_cli("--data-dir", str(dirs.data), "check", "sample")
    assert code == EXIT_USAGE, output
    assert "锚点校验通过" in output
    assert "rewritten.md" in output and "ar rewrite" in output


def test_rewrite_requires_model_config(run_cli, dirs: SimpleNamespace, nokey_config: Path) -> None:
    """没有模型就不能改写：退出码 5 + 可见说明，且一个产物都不写。

    `--config` 显式给一份空配置：这条断言要的是「模型未配置」，不能靠开发机的
    `config.local.json` 恰好没有钥匙（见 conftest 的 `nokey_config`）。
    """
    code, output = run_cli("--data-dir", str(dirs.data), "--config", str(nokey_config),
                           "rewrite", "sample", "--reader", "local-default")
    assert code == EXIT_MODEL, output
    assert "模型未配置" in output
    assert not (dirs.data / "sample" / "rewritten.md").exists()
    assert not (dirs.data / "sample" / "decisions.json").exists()
    assert not (dirs.data / "sample" / "changes.json").exists()


def test_cli_usage_errors(run_cli, sample_md: Path, dirs: SimpleNamespace) -> None:
    code, output = run_cli("--data-dir", str(dirs.data), "ingest", str(dirs.tmp / "nope.md"), "--id", "x")
    assert code == EXIT_USAGE, output

    code, output = run_cli("--data-dir", str(dirs.data), "ingest", str(sample_md), "--id", "../evil")
    assert code == EXIT_USAGE, output
    assert "source_id 非法" in output


def test_cli_ingest_roundtrip_via_argv(run_cli, sample_md: Path, dirs: SimpleNamespace) -> None:
    """全局选项写在子命令**后面**也要生效（argparse parents 的坑）。"""
    code, output = run_cli("ingest", str(sample_md), "--id", "sample", "--data-dir", str(dirs.data))
    assert code == EXIT_OK, output
    assert (dirs.data / "sample" / "source.meta.json").is_file()
    assert "6 段" in output
