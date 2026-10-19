"""「启动页」后端：``GET /api/sources``（只读材料清单）+ ``POST /api/ingest``（上传文本并摄取）+ 三条页面路由。

契约（2026-10-15 冻结）见 ``src/ar/server.py`` 的模块文档。本文件按四段组织：

1. **材料清单**：空 ``data/`` / 只有 ``source.meta.json`` / 三份产物齐全且事实锚通过 /
   事实锚有 mismatches / 产物 JSON 损坏 —— 五种情形的响应形状与 ``degraded`` 文案；
   外加排序、老产物缺键写 ``null``、以及「一个字节都不写盘」；
2. **上传摄取**：扩展名白名单、空 / 超长正文、非法 ``source_id``、从文件名与纯中文名派生 id、
   已存在 409、``overwrite: true`` 覆盖、暂存目录必删（**落在 ``data/.upload-<随机>/``**，
   不依赖本机那个不可写的 ``%TEMP%``）、失败路径不给 HTML 错误页；
3. **摄取与 CLI 逐字一致**：段锚点必须与 ``ar.ingest.ingest`` 直接跑出来的一模一样
   （端点不另写一份切段逻辑）；
4. **页面路由**：``/``（无 query / 带 query 302）、``/reader``（有文件 / 没文件）；
5. **``GET /api/survey`` 的追加字段 ``answered_at``**（2026-10-15）：有作答时间就透传，没有 / 老画像
   缺键就 ``null``，**不拿 ``survey.json`` 的 ``updated_at`` 冒充**。

全部在 ``tmp_path`` 的隔离目录里跑，用 Starlette ``TestClient`` 直打应用，不起真实端口、
不碰项目真实 ``data/``。模型一律不参与：``POST /api/ingest`` 那条路的假客户端**一被调用就报错**，
用来钉住「这条端点只摄取、不调模型」。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from ar import server
from ar.config import Config
from ar.ingest import ingest
from ar.profile import default_profile, save_profile
from ar.server import MAX_UPLOAD_CHARS, UPLOAD_EXTENSIONS, build_app
from ar.survey import SURVEY_VERSION, survey_record_path

# 材料目录里三份「最后动过」判据用的产物（顺序与 server 里那一份一致）
MTIME_ARTIFACTS = ("rewritten.md", "changes.json", "decisions.json")
# 固定的时间戳，让「谁最新」一目了然（os.utime 用整秒，避开文件系统精度差）
T_OLD = 1_700_000_000.0
T_MID = T_OLD + 600.0
T_NEW = T_OLD + 1200.0

CONCEPTS = {
    "concepts": [
        {"id": "c01", "name": "商品", "surface": ["商品"], "prereq": [], "difficulty_hint": 2,
         "paragraphs": ["p001"], "domain": "政治经济学", "concept_key": "commodity"},
        {"id": "c02", "name": "价值", "surface": ["价值"], "prereq": ["c01"], "difficulty_hint": 3,
         "paragraphs": ["p002"], "domain": "政治经济学", "concept_key": "value"},
    ],
    "illustration_candidates": [],
    "meta": {"model": "fake-model", "prompt_version": "concepts-v2",
             "generated_at": "2026-10-08T14:21:05+08:00"},
}

DECISIONS_META = {
    "model": "fake-model",
    "prompt_version": "rewrite-v6",
    "generated_at": "2026-10-13T12:27:42+08:00",
    "paragraph_count": 6,
    "action_counts": {"expand": 2, "compress": 0, "keep": 4, "illustrate": 0},
    "degraded": ["背景模式（没有降级）：读者没有概念自评", "另一条可见降级"],
}

FIDELITY_OK = {
    "checked": True,
    "checked_at": "2026-10-14T11:38:34+08:00",
    "mismatches": [],
    "counts": {"paragraphs": 6, "compared": 6, "numbers": 3, "quotes": 1, "books": 1},
    "fact_anchor_policy": "allow_declared_drops",
    "waived": [],
}


# ------------------------------------------------------------------------ 工具 --
def make_client(
    dirs: SimpleNamespace,
    *,
    data_dir: Path | None = None,
    web_dir: Path | None = None,
    client_factory: Any = None,
    raise_server_exceptions: bool = True,
) -> TestClient:
    """打 ``build_app`` 的 TestClient；``data_dir`` / ``web_dir`` 可按需指到别处。"""
    config = Config(base_url="https://gateway.invalid/provider/v1", model="fake/model")
    app = build_app(
        data_dir=data_dir if data_dir is not None else dirs.data,
        profiles_dir=dirs.profiles,
        web_dir=web_dir if web_dir is not None else dirs.web,
        config=config,
        reader="local-default",
        client_factory=client_factory,
    )
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def exploding_client_factory() -> Any:
    """假客户端工厂：**被调用就报错** —— 用来证明这条端点一个模型请求都没发。"""

    raise AssertionError("POST /api/ingest 不该碰模型")


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def iso_of(path: Path) -> str:
    """文件 mtime → 本地时区 ISO 8601（秒精度），与响应里那个字段同一口径。"""
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().replace(microsecond=0).isoformat()


def snapshot(root: Path) -> list[tuple[str, int, int]]:
    """目录树快照（相对路径 / 字节数 / mtime_ns）：用来钉住只读端点没写盘。"""
    if not root.exists():
        return []
    rows: list[tuple[str, int, int]] = []
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        rows.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns))
    return rows


def make_material(
    dirs: SimpleNamespace,
    sample_md: Path,
    source_id: str,
    *,
    at: str | None = None,
    concepts: bool = True,
    decisions: Any = None,
    changes: Any = None,
    rewritten: bool = True,
) -> Path:
    """造一份材料：先真摄取（``source.md`` + ``source.meta.json``），再按开关补三份产物。

    ``decisions`` / ``changes`` 给 ``None`` 表示不写这份产物（考「缺产物」那一档），
    给字符串表示**原样写入**（考「JSON 损坏」那一档，写进去的东西不合法是故意的）。
    """
    target = doc_dir_of(dirs, source_id)
    ingest(sample_md, source_id, dirs.data, at=at)
    if concepts:
        write_json(target / "concepts.json", CONCEPTS)
    if rewritten:
        (target / "rewritten.md").write_text("改写稿正文\n", encoding="utf-8")
    if decisions is not None:
        if isinstance(decisions, str):
            (target / "decisions.json").write_text(decisions, encoding="utf-8")
        else:
            write_json(target / "decisions.json", {"decisions": [], "meta": decisions})
    if changes is not None:
        if isinstance(changes, str):
            (target / "changes.json").write_text(changes, encoding="utf-8")
        else:
            write_json(target / "changes.json", {"changes": [], "fidelity": changes, "meta": {}})
    return target


def doc_dir_of(dirs: SimpleNamespace, source_id: str) -> Path:
    return dirs.data / source_id


def sources_of(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/sources")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    return response.json()


# ------------------------------------------------------- ① 材料清单：空与缺产物 --
def test_sources_empty_data_dir_is_empty_and_writes_nothing(dirs: SimpleNamespace) -> None:
    """``data/`` 为空 ⇒ ``sources: []`` + **不记降级**（契约明说的那一档），且一个字节都没写。"""
    client = make_client(dirs)
    before = snapshot(dirs.tmp)

    payload = sources_of(client)

    assert payload == {"sources": [], "degraded": []}
    assert snapshot(dirs.tmp) == before, "只读端点不许动盘上任何东西"


def test_sources_missing_data_dir_says_so_instead_of_blank(dirs: SimpleNamespace) -> None:
    """目录**不存在**（而不是空）另说：留一句说明，免得 ``--data-dir`` 指错了地方还一片空白。"""
    missing = dirs.tmp / "no-such-data"
    payload = sources_of(make_client(dirs, data_dir=missing))

    assert payload["sources"] == []
    assert len(payload["degraded"]) == 1
    assert str(missing) in payload["degraded"][0]
    assert "还不存在" in payload["degraded"][0]
    assert not missing.exists(), "只读端点不许把 data/ 建出来"


def test_sources_only_meta_means_three_null_blocks_and_readable_notes(
    dirs: SimpleNamespace, sample_md: Path
) -> None:
    """只有 ``source.meta.json``：段数与摄取时间照给，三块产物全是 ``null`` + 每条一句说明。

    顺带钉住两条「跳过」规则：``.`` 开头的目录、没有 ``source.meta.json`` 的目录都跳过并各记一条；
    ``data/`` 下的散文件不是材料，**静默跳过**（它不在「跳过的目录」那条口径里）。
    """
    target = make_material(
        dirs, sample_md, "alpha", at="2026-10-05T12:00:00+08:00",
        concepts=False, rewritten=False,
    )
    (dirs.data / ".hidden").mkdir()
    (dirs.data / "no-meta").mkdir()
    (dirs.data / "README.txt").write_text("我不是材料\n", encoding="utf-8")

    payload = sources_of(make_client(dirs))

    assert [row["source_id"] for row in payload["sources"]] == ["alpha"]
    entry = payload["sources"][0]
    assert set(entry) == {"source_id", "paragraphs", "ingested_at", "mtime", "concepts", "rewrite", "fidelity"}
    assert entry["paragraphs"] == 6
    assert entry["ingested_at"] == "2026-10-05T12:00:00+08:00"
    # 三份产物一份都没有 ⇒ mtime 退回 source.meta.json
    assert entry["mtime"] == iso_of(target / "source.meta.json")
    assert entry["concepts"] is None and entry["rewrite"] is None and entry["fidelity"] is None

    notes = " / ".join(payload["degraded"])
    assert "alpha：还没有 concepts.json" in notes and "概念清单未抽取" in notes
    assert "alpha：还没有 decisions.json" in notes
    assert "alpha：还没有 changes.json" in notes
    assert ".hidden：以「.」开头的目录不是材料目录，已跳过" in notes
    assert "no-meta：目录里没有 source.meta.json" in notes
    assert "README.txt" not in notes, "散文件不是材料目录，静默跳过"


# ----------------------------------------------------- ① 材料清单：产物齐全 --
def test_sources_full_artifacts_with_passing_fidelity(dirs: SimpleNamespace, sample_md: Path) -> None:
    """三份产物齐全 + 事实锚通过：三块各自成形，``degraded`` 干净（``[]``）。"""
    target = make_material(
        dirs, sample_md, "alpha", at="2026-10-05T12:00:00+08:00",
        decisions=DECISIONS_META, changes=FIDELITY_OK,
    )
    payload = sources_of(make_client(dirs))

    assert payload["degraded"] == []
    entry = payload["sources"][0]
    assert entry["source_id"] == "alpha" and entry["paragraphs"] == 6
    assert entry["concepts"] == {
        "present": True,
        "count": 2,
        "generated_at": "2026-10-08T14:21:05+08:00",
    }
    assert entry["rewrite"] == {
        "present": True,
        "generated_at": "2026-10-13T12:27:42+08:00",
        "action_counts": {"expand": 2, "compress": 0, "keep": 4, "illustrate": 0},
        "degraded_count": 2,
        "paragraph_count": 6,
    }
    assert entry["fidelity"] == {
        "present": True,
        "checked": True,
        "ok": True,
        "mismatch_count": 0,
        "counts": {"paragraphs": 6, "compared": 6, "numbers": 3, "quotes": 1, "books": 1},
    }
    # 「最后动过」取三份产物里最新的一份
    os.utime(target / "decisions.json", (T_OLD, T_OLD))
    os.utime(target / "changes.json", (T_NEW, T_NEW))
    os.utime(target / "rewritten.md", (T_MID, T_MID))
    entry = sources_of(make_client(dirs))["sources"][0]
    assert entry["mtime"] == datetime.fromtimestamp(T_NEW).astimezone().replace(microsecond=0).isoformat()


def test_sources_fidelity_with_mismatches_is_not_ok(dirs: SimpleNamespace, sample_md: Path) -> None:
    """有 mismatches ⇒ ``ok: false`` + ``mismatch_count`` 是**条数**（不是布尔、不是编造的 0）。"""
    fidelity = dict(FIDELITY_OK)
    fidelity["mismatches"] = [
        {"paragraph": "p003", "kind": "numbers", "detail": "改写稿少了一个数字 42"},
        {"paragraph": "p005", "kind": "quotes", "detail": "引号引文不一致"},
    ]
    make_material(dirs, sample_md, "alpha", decisions=DECISIONS_META, changes=fidelity)

    entry = sources_of(make_client(dirs))["sources"][0]
    assert entry["fidelity"]["checked"] is True
    assert entry["fidelity"]["mismatch_count"] == 2
    assert entry["fidelity"]["ok"] is False
    # counts 原样透传
    assert entry["fidelity"]["counts"]["compared"] == 6


def test_sources_old_artifacts_missing_keys_are_null_not_zero(
    dirs: SimpleNamespace, sample_md: Path
) -> None:
    """老产物缺键 ⇒ ``null``（「键不存在」≠「值不对」）：``paragraph_count`` / ``degraded_count`` / ``counts``。

    盘上那份 ``changes.json`` 就还没有 ``paragraph_count``，这条用例钉住「不许补一个 0」。
    """
    old_decisions = {"generated_at": "2026-09-01T00:00:00+08:00"}  # 没有 paragraph_count / degraded / action_counts
    old_fidelity = {"mismatches": []}  # 没有 checked / counts（老产物）
    make_material(dirs, sample_md, "alpha", decisions=old_decisions, changes=old_fidelity)

    entry = sources_of(make_client(dirs))["sources"][0]
    assert entry["rewrite"]["paragraph_count"] is None
    assert entry["rewrite"]["degraded_count"] is None
    assert entry["rewrite"]["action_counts"] is None
    assert entry["rewrite"]["generated_at"] == "2026-09-01T00:00:00+08:00"
    assert entry["fidelity"]["checked"] is None
    assert entry["fidelity"]["counts"] is None
    assert entry["fidelity"]["mismatch_count"] == 0
    # 没校验过就不算通过（宁可说不通过，也不许把「没校验」包装成「通过」）
    assert entry["fidelity"]["ok"] is False


def test_sources_broken_artifacts_degrade_instead_of_500(dirs: SimpleNamespace, sample_md: Path) -> None:
    """三份产物都是坏 JSON ⇒ 200 + 三块 ``null`` + 三条可读说明（绝不 500）。"""
    target = make_material(dirs, sample_md, "alpha", concepts=False, decisions=None, changes=None)
    (target / "concepts.json").write_text("{ 这不是 JSON", encoding="utf-8")
    (target / "decisions.json").write_text("[1, 2, 3]", encoding="utf-8")  # 合法 JSON，但不是对象
    (target / "changes.json").write_text("", encoding="utf-8")

    payload = sources_of(make_client(dirs))

    entry = payload["sources"][0]
    assert entry["concepts"] is None and entry["rewrite"] is None and entry["fidelity"] is None
    assert entry["paragraphs"] == 6, "坏产物不该影响段数（它来自 source.meta.json）"
    notes = " / ".join(payload["degraded"])
    assert "alpha：concepts.json 读不出来" in notes and "概念清单按未抽取处理" in notes
    assert "alpha：decisions.json 读不出来" in notes
    assert "changes.json 读不出来" in notes
    assert "不是 JSON 对象，而是 list" in notes, "形状不对也要说清是哪种形状"


def test_sources_broken_source_meta_still_lists_the_material(dirs: SimpleNamespace) -> None:
    """``source.meta.json`` 本身坏了：材料**照旧列出来**，但段数 / 摄取时间写 ``null`` + 一句说明。"""
    target = dirs.data / "broken-meta"
    target.mkdir(parents=True)
    (target / "source.meta.json").write_text("{ 坏了", encoding="utf-8")

    payload = sources_of(make_client(dirs))

    entry = payload["sources"][0]
    assert entry["source_id"] == "broken-meta"
    assert entry["paragraphs"] is None and entry["ingested_at"] is None
    assert entry["mtime"] == iso_of(target / "source.meta.json")
    assert "broken-meta：source.meta.json 读不出来" in " / ".join(payload["degraded"])


def test_sources_sorted_by_mtime_desc_then_id_asc(dirs: SimpleNamespace, sample_md: Path) -> None:
    """排序：``mtime`` 倒序；同一时刻按 ``source_id`` 升序（两次调用结果一致）。"""
    for source_id in ("alpha", "beta", "gamma", "zeta"):
        make_material(dirs, sample_md, source_id, rewritten=True)
    os.utime(dirs.data / "gamma" / "rewritten.md", (T_NEW, T_NEW))
    os.utime(dirs.data / "beta" / "rewritten.md", (T_MID, T_MID))
    os.utime(dirs.data / "alpha" / "rewritten.md", (T_OLD, T_OLD))
    os.utime(dirs.data / "zeta" / "rewritten.md", (T_OLD, T_OLD))  # 与 alpha 同刻 ⇒ 按 id 升序

    client = make_client(dirs)
    first = [row["source_id"] for row in sources_of(client)["sources"]]
    second = [row["source_id"] for row in sources_of(client)["sources"]]

    assert first == ["gamma", "beta", "alpha", "zeta"]
    assert first == second, "同样的盘上状态必须给出同样的顺序"


def test_sources_same_displayed_second_falls_back_to_id_order(dirs: SimpleNamespace, sample_md: Path) -> None:
    """显示精度是秒：差 0.4 秒的两份材料在响应里是**同一时刻**，顺序按 ``source_id`` 升序。

    这是有意的口径——排序用的就是响应里暴露的那个字符串，
    于是「页面照 mtime 排」和「接口给的顺序」永远一致（不然看起来像接口排错了）。
    """
    for source_id in ("alpha", "zeta"):
        make_material(dirs, sample_md, source_id, rewritten=True)
    os.utime(dirs.data / "alpha" / "rewritten.md", (T_OLD, T_OLD))
    os.utime(dirs.data / "zeta" / "rewritten.md", (T_OLD + 0.4, T_OLD + 0.4))

    rows = sources_of(make_client(dirs))["sources"]

    assert [row["source_id"] for row in rows] == ["alpha", "zeta"]
    assert rows[0]["mtime"] == rows[1]["mtime"], "这两个时刻在秒精度下就是同一个"


def test_sources_read_only_snapshot_unchanged_with_full_artifacts(
    dirs: SimpleNamespace, sample_md: Path
) -> None:
    """只读纪律（最硬的一条）：产物齐全时连读带列，盘上一个字节、一个 mtime 都不许变。"""
    make_material(dirs, sample_md, "alpha", decisions=DECISIONS_META, changes=FIDELITY_OK)
    client = make_client(dirs)
    before = snapshot(dirs.tmp)

    sources_of(client)
    sources_of(client)

    assert snapshot(dirs.tmp) == before


# ------------------------------------------------------------ ② 上传摄取：入参 --
@pytest.mark.parametrize("filename", ["notes", "notes.pdf", "notes.md.zip", "notes.markdown", "档案.docx"])
def test_ingest_rejects_extensions_outside_the_whitelist(dirs: SimpleNamespace, filename: str) -> None:
    response = make_client(dirs).post(
        "/api/ingest", json={"filename": filename, "content": "第一段。\n\n第二段。"}
    )
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert "扩展名不在白名单" in error and filename in error
    assert " / ".join(UPLOAD_EXTENSIONS) in error, "文案里要点名收了哪几种"
    assert not (dirs.data / "notes").exists()


@pytest.mark.parametrize("filename", [None, 123, "", "   ", ["a.md"]])
def test_ingest_rejects_bad_filename_types(dirs: SimpleNamespace, filename: Any) -> None:
    body: dict[str, Any] = {"content": "第一段。\n\n第二段。"}
    if filename is not None:
        body["filename"] = filename
    response = make_client(dirs).post("/api/ingest", json=body)
    assert response.status_code == 400, response.text
    assert "filename 必须是非空字符串" in response.json()["error"]


@pytest.mark.parametrize("content", ["", 123, None, ["第一段"]])
def test_ingest_rejects_empty_or_non_string_content(dirs: SimpleNamespace, content: Any) -> None:
    response = make_client(dirs).post("/api/ingest", json={"filename": "a.md", "content": content})
    assert response.status_code == 400, response.text
    assert "content 必须是非空字符串" in response.json()["error"]


def test_ingest_rejects_whitespace_only_content_with_the_ingest_message(dirs: SimpleNamespace) -> None:
    """全是空白的正文交给**摄取模块**判（它是切段规则的唯一实现），文案由它给，照样 400。"""
    response = make_client(dirs).post("/api/ingest", json={"filename": "a.md", "content": "  \n\n\t\n "})
    assert response.status_code == 400, response.text
    assert "没有可摄取的正文段落" in response.json()["error"]
    assert not (dirs.data / "a").exists(), "判失败就不该建出材料目录"


def test_ingest_rejects_oversized_content_and_names_the_limit(dirs: SimpleNamespace) -> None:
    """超长正文**拒收**（不截断——截断会悄悄改掉材料内容），文案里必须写清上限。"""
    too_long = "字" * (MAX_UPLOAD_CHARS + 1)
    response = make_client(dirs).post("/api/ingest", json={"filename": "big.txt", "content": too_long})

    assert response.status_code == 400, response.status_code
    error = response.json()["error"]
    assert str(MAX_UPLOAD_CHARS) in error and str(MAX_UPLOAD_CHARS + 1) in error
    assert "上限" in error
    assert not (dirs.data / "big").exists()

    # 正好卡在上限的正文必须收下（边界不算超）
    at_limit = "字" * MAX_UPLOAD_CHARS
    ok = make_client(dirs).post("/api/ingest", json={"filename": "big.txt", "content": at_limit})
    assert ok.status_code == 200, ok.text
    assert ok.json()["paragraphs"] == 1


@pytest.mark.parametrize(
    "source_id",
    ["../evil", "a/b", "a\\b", "..", ".hidden", "-lead", "x" * 65, "有中文", "a b"],
)
def test_ingest_rejects_illegal_source_id(dirs: SimpleNamespace, source_id: str) -> None:
    response = make_client(dirs).post(
        "/api/ingest",
        json={"filename": "a.md", "content": "第一段。\n\n第二段。", "source_id": source_id},
    )
    assert response.status_code == 400, response.text
    assert "source_id 非法" in response.json()["error"]
    assert not any(dirs.data.iterdir()), "非法 id 一个目录都不许建"


def test_ingest_rejects_non_string_source_id(dirs: SimpleNamespace) -> None:
    response = make_client(dirs).post(
        "/api/ingest", json={"filename": "a.md", "content": "第一段。", "source_id": 7}
    )
    assert response.status_code == 400, response.text
    assert "source_id 必须是字符串" in response.json()["error"]


def test_ingest_rejects_non_object_body(dirs: SimpleNamespace) -> None:
    response = make_client(dirs).post("/api/ingest", json=[1, 2, 3])
    assert response.status_code == 400, response.text
    assert "请求体必须是 JSON 对象" in response.json()["error"]


# ------------------------------------------------------ ② 上传摄取：派生 id 与成功 --
def test_ingest_derives_id_from_filename_and_writes_content_verbatim(
    dirs: SimpleNamespace, sample_md: Path
) -> None:
    """扩展名大小写不敏感；省略 ``source_id`` 时从文件名主干 slug 化，正文**原样**落盘。"""
    content = sample_md.read_text(encoding="utf-8")
    payload = make_client(dirs).post(
        "/api/ingest", json={"filename": "My Report (final).MD", "content": content}
    ).json()

    assert payload["source_id"] == "my-report-final"
    assert payload["paragraphs"] == 6
    assert payload["overwritten"] is False
    assert payload["anchors_stable"] is True, "首次摄取没有可比的旧锚点，没有警告就是稳定"
    assert payload["degraded"] == []
    target = dirs.data / "my-report-final"
    # 「原文原样」是对**上传进来的那份文本**说的：写成字节，与 content 的 UTF-8 编码逐字节一致
    assert (target / "source.md").read_bytes() == content.encode("utf-8")
    meta = json.loads((target / "source.meta.json").read_text(encoding="utf-8"))
    assert payload["ingested_at"] == meta["ingest"]["at"]


def test_ingest_derives_content_hash_id_for_a_chinese_filename(dirs: SimpleNamespace) -> None:
    """纯中文文件名 slug 化为空 ⇒ ``upload-<内容 sha256 前 8 位>``（合法、且与内容绑定）。"""
    content = "第一段。\n\n第二段。"
    expected = "upload-" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]

    client = make_client(dirs)
    first = client.post("/api/ingest", json={"filename": "资本论选段.md", "content": content})
    assert first.status_code == 200, first.text
    assert first.json()["source_id"] == expected

    # 同一份内容再传一次：id 一样 ⇒ 落到「已存在」那条路上（不会每次上传都堆一个新目录）
    again = client.post("/api/ingest", json={"filename": "另一个中文名.md", "content": content})
    assert again.status_code == 409, again.text
    assert [p.name for p in dirs.data.iterdir()] == [expected]


def test_ingest_conflict_409_then_overwrite_true(dirs: SimpleNamespace) -> None:
    """已存在且不带 ``overwrite: true`` ⇒ 409 + 可读文案（说清「已存在」与「怎么覆盖」），且不写盘。

    覆盖成功后 ``overwritten: true``，并把摄取模块的锚点变化警告原样透传（``anchors_stable: false``）。
    """
    content_a = "第一段。\n\n第二段。\n\n第三段。"
    content_b = "改过的第一段，长度也不一样。\n\n第二段。"
    client = make_client(dirs)
    body = {"filename": "notes.md", "content": content_a, "source_id": "notes"}
    assert client.post("/api/ingest", json=body).json()["overwritten"] is False
    before = snapshot(dirs.data / "notes")

    conflict = client.post("/api/ingest", json={**body, "content": content_b})

    assert conflict.status_code == 409, conflict.text
    payload = conflict.json()
    assert "已经存在" in payload["error"] and "notes" in payload["error"]
    assert "overwrite: true" in payload["hint"]
    assert "source.md" in payload["error"], "要说清是哪个文件已存在"
    assert snapshot(dirs.data / "notes") == before, "409 这条路一个字节都不许写"

    # 非布尔的真值（字符串 "true"）不算 overwrite —— 否则「不覆盖」会被读成「覆盖」
    truthy = client.post("/api/ingest", json={**body, "content": content_b, "overwrite": "true"})
    assert truthy.status_code == 409, truthy.text

    overwritten = client.post("/api/ingest", json={**body, "content": content_b, "overwrite": True}).json()
    assert overwritten["overwritten"] is True
    assert overwritten["source_id"] == "notes"
    assert overwritten["paragraphs"] == 2
    assert overwritten["anchors_stable"] is False
    notes = " / ".join(overwritten["degraded"])
    assert "段落消失" in notes and "材料已变" in notes, "锚点变化的警告要原样透传"
    assert (dirs.data / "notes" / "source.md").read_text(encoding="utf-8") == content_b

    # 用同一份内容再覆盖：锚点逐字一致 ⇒ 稳定，且不再有警告
    same = client.post("/api/ingest", json={**body, "content": content_b, "overwrite": True}).json()
    assert same["anchors_stable"] is True and same["degraded"] == []


def test_ingest_only_ingests_never_calls_the_model_nor_downstream_steps(
    dirs: SimpleNamespace, sample_md: Path
) -> None:
    """**只摄取**：不调模型、不跑 analyze / rewrite / check —— 目录里只该多出两份摄取产物。"""
    client = make_client(dirs, client_factory=exploding_client_factory)
    payload = client.post(
        "/api/ingest",
        json={"filename": "sample.txt", "content": sample_md.read_text(encoding="utf-8"), "source_id": "sample"},
    ).json()

    assert payload["degraded"] == []
    names = sorted(path.name for path in (dirs.data / "sample").iterdir())
    assert names == ["source.md", "source.meta.json"]


def test_ingest_segments_match_the_cli_ingest_path_verbatim(
    dirs: SimpleNamespace, sample_md: Path, tmp_path: Path
) -> None:
    """段锚点必须与 ``ar.ingest.ingest``（CLI ``ar ingest`` 走的同一条路）逐字一致。

    端点的正文是从临时文件喂给摄取模块的，但**切段规则只能有一份实现**：
    同一份文本经端点与经模块直接摄取，``paragraphs`` 必须一模一样。
    """
    # 按字节读、按 UTF-8 解码：保住源文件的行尾（``source.md`` 是**按字节**写的，
    # 拿 ``read_text`` 读进来会被通用换行翻成 LF，那样比的是两份不同的文本）
    content = sample_md.read_bytes().decode("utf-8")
    payload = make_client(dirs).post(
        "/api/ingest", json={"filename": "via-api.md", "content": content, "source_id": "via-api"}
    ).json()
    direct = ingest(sample_md, "via-cli", tmp_path / "other-data")

    mine = json.loads((dirs.data / "via-api" / "source.meta.json").read_text(encoding="utf-8"))
    assert mine["paragraphs"] == direct.meta["paragraphs"]
    assert payload["paragraphs"] == len(direct.paragraphs) == 6
    assert (dirs.data / "via-api" / "source.md").read_bytes() == sample_md.read_bytes()


# ------------------------------------------------- ② 上传摄取：暂存目录必删 --
def _spy_on_staging(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """记录端点建出来的暂存目录（``data/.upload-<随机>/``）。"""
    created: list[Path] = []
    real = server._upload_staging_dir

    def spy(data_root: Path) -> Path:
        path = real(data_root)
        created.append(path)
        return path

    monkeypatch.setattr(server, "_upload_staging_dir", spy)
    return created


def staging_leftovers(root: Path) -> list[str]:
    """``data/`` 里还没删掉的暂存目录（正常路径上必须为空）。"""
    return sorted(path.name for path in root.iterdir() if path.name.startswith(".upload-"))


def test_ingest_removes_its_staging_dir_on_success(dirs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    created = _spy_on_staging(monkeypatch)
    response = make_client(dirs).post(
        "/api/ingest", json={"filename": "a.md", "content": "第一段。\n\n第二段。", "source_id": "a"}
    )

    assert response.status_code == 200 and response.json()["degraded"] == []
    assert created, "端点必须真的经暂存文件走摄取模块（这次没建过暂存目录）"
    assert all(not path.exists() for path in created), f"暂存目录没删掉：{created}"
    assert staging_leftovers(dirs.data) == []
    assert sorted(path.name for path in dirs.data.iterdir()) == ["a"], "data/ 里只该多出材料目录"


@pytest.mark.parametrize(
    "body",
    [
        {"filename": "a.md", "content": "  \n\n \t"},  # 摄取模块判「没有正文段落」
        {"filename": "a.pdf", "content": "第一段。"},  # 扩展名被白名单拦下（更早的一步）
        {"filename": "a.md", "content": "第一段。", "source_id": "../evil"},  # 非法 id
    ],
)
def test_ingest_removes_its_staging_dir_on_failure_paths(
    dirs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    """异常路径（400）也不许留残骸：``finally`` 必删，一处不落。"""
    created = _spy_on_staging(monkeypatch)
    response = make_client(dirs).post("/api/ingest", json=body)

    assert response.status_code == 400, response.text
    for path in created:
        assert not path.exists(), f"失败路径留下了暂存目录：{path}"
    assert staging_leftovers(dirs.data) == []


def test_ingest_write_failure_is_500_with_a_readable_error_and_no_leftovers(
    dirs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写盘失败 ⇒ 500 + 可读消息（点名目录 + errno），**且暂存目录照样删干净**。

    坏路径不打桩：``data/taken`` 是个同名文件 ⇒ 摄取模块 ``mkdir`` 拿不到目录（``EEXIST``）。
    ``WriteError`` 故意没有专用处理器（口径见 ``ar.errors``）：它走 500 兜底，不是 400。
    """
    created = _spy_on_staging(monkeypatch)
    (dirs.data / "taken").write_text("我不是目录", encoding="utf-8")

    response = make_client(dirs, raise_server_exceptions=False).post(
        "/api/ingest", json={"filename": "a.md", "content": "第一段。\n\n第二段。", "source_id": "taken"}
    )

    assert response.status_code == 500, response.text
    assert response.headers["content-type"].startswith("application/json"), "不许回 HTML 错误页"
    error = response.json()["error"]
    assert "写不进去" in error and "errno" in error, error
    assert created and all(not path.exists() for path in created)
    assert staging_leftovers(dirs.data) == []
    assert (dirs.data / "taken").is_file(), "坏路径不许被改写"


def test_ingest_staging_failure_is_500_and_never_a_silent_success(dirs: SimpleNamespace) -> None:
    """暂存目录都建不出来（data/ 被同名文件占住）⇒ 500 + 可读消息，绝不假装摄取成功。"""
    blocker = dirs.tmp / "blocked-data"
    blocker.write_text("我不是目录", encoding="utf-8")

    response = make_client(dirs, data_dir=blocker, raise_server_exceptions=False).post(
        "/api/ingest", json={"filename": "a.md", "content": "第一段。"}
    )

    assert response.status_code == 500, response.text
    error = response.json()["error"]
    assert "没法暂存" in error and "errno" in error, error
    assert blocker.is_file(), "坏路径不许被改写"


# ------------------------------------------------------------------ ③ 页面路由 --
def test_root_serves_the_startup_page_without_query(dirs: SimpleNamespace) -> None:
    dirs.web.mkdir(parents=True)
    (dirs.web / "index.html").write_text("<!doctype html><p>startup-page</p>", encoding="utf-8")

    response = make_client(dirs).get("/")

    assert response.status_code == 200
    assert "startup-page" in response.text
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize(
    "query,location",
    [
        ("source=capital-v1-ch01", "/reader?source=capital-v1-ch01"),
        ("fixture=capital-v1-ch01", "/reader?fixture=capital-v1-ch01"),
        # query 原样保留：顺序、多个参数、空值都不动
        ("fixture=x&source=y&extra=1", "/reader?fixture=x&source=y&extra=1"),
        ("source=", "/reader?source="),
    ],
)
def test_root_with_page_params_redirects_to_reader_preserving_query(
    dirs: SimpleNamespace, query: str, location: str
) -> None:
    """带 ``source`` / ``fixture`` 的 ``/`` → **302** ``/reader``，query 原样带过去（老链接照旧能用）。"""
    dirs.web.mkdir(parents=True)
    (dirs.web / "index.html").write_text("<!doctype html><p>startup-page</p>", encoding="utf-8")

    response = make_client(dirs).get(f"/?{query}", follow_redirects=False)

    assert response.status_code == 302, response.text
    assert response.headers["location"] == location
    assert response.headers["location"].endswith(query), "query 逐字带过去，不重排、不重新编码"


def test_root_ignores_other_query_params(dirs: SimpleNamespace) -> None:
    """别的 query（既没有 source 也没有 fixture）不影响托管：照旧是启动页。"""
    dirs.web.mkdir(parents=True)
    (dirs.web / "index.html").write_text("<!doctype html><p>startup-page</p>", encoding="utf-8")

    response = make_client(dirs).get("/?debug=1")

    assert response.status_code == 200
    assert "startup-page" in response.text


def test_reader_serves_reader_html_when_present(dirs: SimpleNamespace) -> None:
    dirs.web.mkdir(parents=True)
    (dirs.web / "index.html").write_text("<!doctype html><p>startup-page</p>", encoding="utf-8")
    (dirs.web / "reader.html").write_text("<!doctype html><p>reader-page</p>", encoding="utf-8")

    response = make_client(dirs).get("/reader")

    assert response.status_code == 200
    assert "reader-page" in response.text
    assert response.headers["content-type"].startswith("text/html")


def test_reader_404_is_readable_and_never_falls_back_to_the_startup_page(dirs: SimpleNamespace) -> None:
    """``reader.html`` 还不存在（前端正在改名）⇒ 404 + 可读文案；**绝不回落到启动页、绝不 500**。"""
    dirs.web.mkdir(parents=True)
    (dirs.web / "index.html").write_text("<!doctype html><p>startup-page</p>", encoding="utf-8")

    response = make_client(dirs).get("/reader")

    assert response.status_code == 404, response.text
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert "reader.html 不存在" in payload["error"]
    assert "启动页" in payload["hint"]
    assert str(dirs.web) in payload["web_dir"]
    assert "startup-page" not in response.text, "不许回落到启动页"
    assert "文件或路由不存在" not in response.text, "这是本路由的 404，不是静态托管的兜底 404"


def test_new_api_routes_sit_before_the_api_fallback(dirs: SimpleNamespace) -> None:
    """``/api/sources`` / ``/api/ingest`` 必须排在 ``/api/{rest:path}`` 兜底之前。

    判据：真端点会给 200 / 400；被兜底吃掉的话 POST 会得到那句「接口不存在」的 404。
    （顺带记一笔：``GET /api/ingest`` 会落到兜底那个 JSON 404 —— Starlette 的路由器只在
    「路径匹配 + 方法不匹配」时记一次部分匹配，随后那个**全匹配**的兜底路由会赢，
    所以这里不是 405。这是既有框架行为，不是新端点的问题。）
    """
    client = make_client(dirs)

    assert client.get("/api/sources").status_code == 200, "被兜底路由吃掉的话这里会是 404"
    assert client.post("/api/ingest", json={}).status_code == 400, "同上：兜底会回 404 + 「接口不存在」"
    assert client.get("/api/ingest").status_code == 404
    assert "接口不存在" in client.get("/api/ingest").json()["error"]
    assert client.get("/api/nope").status_code == 404, "兜底路由本身还在"


# ------------------------------------------ ④ 背景问卷：GET /api/survey 的 answered_at --
def test_get_survey_passes_through_the_answered_at_recorded_in_the_profile(
    dirs: SimpleNamespace,
) -> None:
    """有作答时间 ⇒ 原样透传（追加字段，取值就是画像 ``background.answered_at``）；旧键一个不改。"""
    profile = default_profile("local-default")
    profile["background"] = {"version": SURVEY_VERSION, "answered_at": "2026-10-09T09:00:13+08:00"}
    save_profile(dirs.profiles, "local-default", profile)

    payload = make_client(dirs).get("/api/survey").json()

    assert payload["answered_at"] == "2026-10-09T09:00:13+08:00"
    assert set(payload) == {
        "version", "answered_version", "answered_at", "questions", "answers", "skipped", "degraded",
    }, "只在响应里追加 answered_at，原有键一个不多一个不少"


def test_get_survey_answered_at_is_null_when_unknown_and_never_faked(
    dirs: SimpleNamespace,
) -> None:
    """没有作答时间 ⇒ ``null``（不编造「现在」）；盘上有 ``survey.json`` 的 ``updated_at`` 也不许冒充。"""
    assert make_client(dirs).get("/api/survey").json()["answered_at"] is None, "空画像 ⇒ null"

    # 老画像：有背景块，但没写 answered_at；而同目录的 survey.json 里有一个 updated_at
    profile = default_profile("local-default")
    profile["background"] = {"version": SURVEY_VERSION, "education": "undergraduate"}
    save_profile(dirs.profiles, "local-default", profile)
    write_json(
        survey_record_path(dirs.profiles, "local-default"),
        {"version": SURVEY_VERSION, "updated_at": "2026-10-14T08:00:00+08:00", "answers": {}},
    )

    payload = make_client(dirs).get("/api/survey").json()

    assert payload["answered_at"] is None, "画像里没有作答时间就写 null，不许拿 updated_at 顶上"
    assert "2026-10-14T08:00:00+08:00" not in json.dumps(payload, ensure_ascii=False)
