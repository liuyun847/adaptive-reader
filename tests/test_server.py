"""HTTP 层：/api/* 契约 + 静态托管（含 web/ 目录不存在时不崩）。

用 Starlette 的 TestClient 直接打应用，不起真实端口；所有模型调用都注入假客户端。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from ar.analyze import validate_inventory
from ar.config import Config
from ar.ingest import ingest
from ar.model import ModelCallFailed
from ar.server import build_app

GOOD_CONCEPTS = {
    "concepts": [
        {
            "id": "c01",
            "name": "商品",
            "surface": ["商品"],
            "prereq": [],
            "difficulty_hint": 2,
            "paragraphs": ["p001"],
            "domain": "政治经济学",
            "concept_key": "commodity",
        }
    ],
    "illustration_candidates": [{"paragraph": "p001", "kind": "流程", "why": "递进"}],
    "meta": {"model": "fake-model", "prompt_version": "concepts-v1"},
}


def make_client(
    dirs: SimpleNamespace,
    *,
    api_key: str = "",
    api_key_origin: str | None = None,
    client_factory=None,
    raise_server_exceptions: bool = True,
) -> TestClient:
    config = Config(
        base_url="https://gateway.invalid/provider/v1",
        model="fake/model",
        api_key=api_key,
        api_key_origin=api_key_origin,
    )
    app = build_app(
        data_dir=dirs.data,
        profiles_dir=dirs.profiles,
        web_dir=dirs.web,
        config=config,
        reader="local-default",
        client_factory=client_factory,
    )
    # 写盘失败（WriteError）落在 Starlette 兜底处理器上：它会先把 500 响应发出去、再把异常
    # 重新抛出，所以要看**响应本身**必须关掉 TestClient 的 raise_server_exceptions。
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


# ------------------------------------------------------------------- health --
def test_health_reports_model_state_without_leaking_key(dirs: SimpleNamespace) -> None:
    payload = make_client(dirs).get("/api/health").json()
    assert payload["ok"] is True
    assert payload["model_configured"] is False
    assert payload["api_key_origin"] is None
    assert payload["web_static"] is False

    secret = "sk-" + "9" * 40
    configured = make_client(dirs, api_key=secret, api_key_origin="env:AR_API_KEY")
    body = configured.get("/api/health").json()
    assert body["model_configured"] is True
    assert body["api_key_origin"] == "env:AR_API_KEY"
    assert secret not in json.dumps(body)


# ---------------------------------------------------------------------- doc --
def test_doc_endpoint_returns_meta_and_visible_degradation(dirs: SimpleNamespace, sample_md: Path) -> None:
    ingest(sample_md, "sample", dirs.data)
    response = make_client(dirs).get("/api/doc/sample")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"meta", "rewritten", "changes", "concepts", "degraded"}
    assert payload["meta"]["source_id"] == "sample"
    assert len(payload["meta"]["paragraphs"]) == 6
    assert payload["rewritten"] is None and payload["changes"] is None and payload["concepts"] is None
    joined = " / ".join(payload["degraded"])
    assert "rewritten.md" in joined and "concepts.json" in joined and "无画像" in joined


def test_doc_endpoint_serves_written_artifacts(dirs: SimpleNamespace, sample_md: Path) -> None:
    ingest(sample_md, "sample", dirs.data)
    target = dirs.data / "sample"
    (target / "rewritten.md").write_text("改写稿正文\n", encoding="utf-8")
    (target / "changes.json").write_text(
        json.dumps(
            {
                "changes": [{"paragraph": "p001", "action": "keep", "why": {"profile": [], "note": "无依据"}}],
                "fidelity": {"checked": False, "mismatches": []},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (target / "concepts.json").write_text(json.dumps(GOOD_CONCEPTS, ensure_ascii=False), encoding="utf-8")

    payload = make_client(dirs).get("/api/doc/sample").json()
    assert payload["rewritten"] == "改写稿正文\n"
    assert payload["changes"]["changes"][0]["paragraph"] == "p001"
    assert payload["concepts"]["concepts"][0]["id"] == "c01"
    assert payload["degraded"] == []


def test_doc_endpoint_404_and_illegal_id(dirs: SimpleNamespace) -> None:
    client = make_client(dirs)
    missing = client.get("/api/doc/nosuchdoc")
    assert missing.status_code == 404
    assert "找不到材料" in missing.json()["error"]
    assert missing.json()["degraded"]

    illegal = client.get("/api/doc/bad..id")
    assert illegal.status_code == 400
    assert "source_id 非法" in illegal.json()["error"]


# ------------------------------------------------------------------ profile --
def test_profile_get_returns_skeleton_with_degraded_note(dirs: SimpleNamespace) -> None:
    payload = make_client(dirs).get("/api/profile").json()
    assert payload["profile"]["reader"] == "local-default"
    assert payload["degraded"] and "不存在" in payload["degraded"][0]


def test_profile_rating_writes_yaml_and_reloads(dirs: SimpleNamespace, sample_md: Path) -> None:
    client = make_client(dirs)
    response = client.post("/api/profile/rating", json={"concept_id": "c01", "state": "known", "note": "这段我懂"})
    assert response.status_code == 200, response.text
    assert response.json()["profile"]["concepts"]["c01"]["state"] == "known"

    path = dirs.profiles / "local-default" / "profile.yaml"
    assert path.is_file()
    assert "这段我懂" in path.read_text(encoding="utf-8")

    reloaded = client.get("/api/profile").json()
    assert reloaded["profile"]["concepts"]["c01"]["state"] == "known"
    assert reloaded["profile"]["overrides"][0]["concept"] == "c01"


@pytest.mark.parametrize(
    "body",
    [
        {"concept_id": "c01", "state": "大概知道"},
        {"concept_id": "", "state": "known"},
        {"state": "known"},
        {"concept_id": "c01", "state": 3},
        {"concept_id": "c01", "state": "known", "note": 5},
    ],
)
def test_profile_rating_rejects_bad_bodies(dirs: SimpleNamespace, body: dict) -> None:
    response = make_client(dirs).post("/api/profile/rating", json=body)
    assert response.status_code == 400, response.text
    assert response.json()["error"]


def test_profile_rating_rejects_broken_json(dirs: SimpleNamespace) -> None:
    response = make_client(dirs).post(
        "/api/profile/rating", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert "JSON" in response.json()["error"]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/profile/rating", {"concept_id": "c01", "state": "known"}),
        ("/api/profile/preference", {"id": "pref_fact_anchor", "value": "keep_all"}),
    ],
)
def test_profile_writes_on_an_unwritable_path_are_500_with_a_readable_error(
    dirs: SimpleNamespace, path: str, body: dict
) -> None:
    """画像写不进去 ⇒ 500 但**消息可读**（点名目录 + 原始 errno），不是一句裸 `PermissionError`。

    2026-10-14 复核口径：写盘失败（`WriteError`）**故意**不注册专用处理器——坏的是「服务端跑在
    哪个目录上」，不是客户端给了坏输入，所以既不能报 400，也不能只说一句「服务内部错误」。
    `POST /api/profile/rating` / `/api/profile/preference` 都走 `ar.profile.save_profile` 这条写盘路径
    （`profile.yaml`），此前它抛的是裸 `PermissionError`。

    **不打桩**的真实坏路径：`profiles/<reader>` 是个同名文件 ⇒ `mkdir` 拿不到目录（`EEXIST`）。
    """
    blocker = dirs.profiles / "local-default"
    blocker.write_text("我不是目录", encoding="utf-8")

    response = make_client(dirs, raise_server_exceptions=False).post(path, json=body)

    assert response.status_code == 500, response.text
    error = response.json()["error"]
    assert "写不进去" in error and str(blocker) in error, error
    assert "errno" in error, f"错误里必须带原始 errno：{error}"
    assert "PermissionError" not in error, "不许再是一句裸的 PermissionError"
    assert blocker.is_file(), "坏路径不许被改写"


# ------------------------------------------------------------------ explain --
def test_explain_degrades_visibly_when_model_not_configured(dirs: SimpleNamespace, sample_md: Path) -> None:
    ingest(sample_md, "sample", dirs.data)
    response = make_client(dirs).post(
        "/api/explain", json={"source_id": "sample", "paragraph": "p001", "kind": "explain"}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["degraded"] is True
    assert payload["reason"] == "model_not_configured"
    assert payload["text"].strip(), "降级也必须给出可见的说明文本，不能空白"
    assert "AR_API_KEY" in payload["text"]


def test_explain_uses_model_when_configured(dirs: SimpleNamespace, sample_md: Path, fake_client) -> None:
    ingest(sample_md, "sample", dirs.data)
    fake = fake_client(text="这一段说的是商品的二重性。")
    client = make_client(dirs, api_key="sk-" + "1" * 40, client_factory=lambda: fake)
    # p004 = 正文第一块（p001..p003 是标题 / 出处引用 / 分隔线）
    response = client.post("/api/explain", json={"source_id": "sample", "paragraph": "p004", "kind": "simpler"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["degraded"] is False
    assert payload["text"] == "这一段说的是商品的二重性。"
    # explain-v2（2026-10-08）：新增「选中片段」作用域，提示词语义变了 → 版本号跟着升（不是放宽断言）
    assert payload["model"] == "fake-model" and payload["prompt_version"] == "explain-v2"
    assert payload["scope"] == "paragraph"  # 不带 selection：作用域仍是整段
    assert "cached" not in payload  # 首次调用不是缓存命中
    # 提示词里必须带上那一段的原文，并写明要求的讲解方式
    sent = fake.calls[0][1]["content"]
    assert "第一段" in sent and "更简单的话" in sent


def test_explain_model_failure_is_degraded_not_500(dirs: SimpleNamespace, sample_md: Path, fake_client) -> None:
    ingest(sample_md, "sample", dirs.data)
    failing = fake_client(error=ModelCallFailed("模型调用失败（已试 2 次）：HTTP 503"))
    client = make_client(dirs, api_key="sk-" + "2" * 40, client_factory=lambda: failing)
    response = client.post("/api/explain", json={"source_id": "sample", "paragraph": "p002"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["degraded"] is True and payload["reason"] == "model_error"
    assert "503" in payload["text"]


def test_explain_unknown_paragraph_and_missing_source_are_degraded(dirs: SimpleNamespace, sample_md: Path) -> None:
    client = make_client(dirs)
    no_source = client.post("/api/explain", json={"source_id": "nosuchdoc", "paragraph": "p001"}).json()
    assert no_source["degraded"] is True and no_source["reason"] == "source_not_found"

    ingest(sample_md, "sample", dirs.data)
    bad_paragraph = client.post("/api/explain", json={"source_id": "sample", "paragraph": "p404"}).json()
    assert bad_paragraph["degraded"] is True and bad_paragraph["reason"] == "paragraph_not_found"
    assert "p404" in bad_paragraph["text"]


def test_explain_never_turns_a_tool_validation_failure_into_400_or_500(
    dirs: SimpleNamespace, sample_md: Path, fake_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工具层入参校验失败（调用方的 bug）也只回 200 + 可见降级。

    迁移后 ``tools`` 的入参校验**不再抛 ``InputError``**（改为 ``INVALID_REQUEST`` 失败信封）：
    抛异常会被本层当成 400（把内部错误伪装成客户端输入错误），其它异常则是 500。
    四个调用点构造的入参本来就合规，这里把不合规那一刻强行造出来（换掉 ``tools.run_tool``）。
    """
    from ar import tools

    ingest(sample_md, "sample", dirs.data)
    fake = fake_client(text="不该被用到。")
    real_run_tool = tools.run_tool

    def broken_run_tool(name: str, params: dict, **kwargs):
        return real_run_tool(name, {"nope": 1}, **kwargs)  # 入参不合契约

    monkeypatch.setattr("ar.tools.run_tool", broken_run_tool)
    client = make_client(dirs, api_key="sk-" + "9" * 40, client_factory=lambda: fake)
    response = client.post("/api/explain", json={"source_id": "sample", "paragraph": "p002"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["degraded"] is True and payload["reason"] == "model_error"
    assert "INVALID_REQUEST" in payload["text"] and "已试 0 次" in payload["text"]
    assert payload["attempts"] == [] and fake.calls == [], "入参不合法时一个请求都不发"


@pytest.mark.parametrize(
    "body", [{"paragraph": "p001"}, {"source_id": "sample"}, {"source_id": 1, "paragraph": "p001"}]
)
def test_explain_rejects_bad_bodies(dirs: SimpleNamespace, body: dict) -> None:
    response = make_client(dirs).post("/api/explain", json=body)
    assert response.status_code == 400
    assert response.json()["error"]


# ------------------------------------------------------------------- 静态托管 --
def test_root_without_web_dir_returns_explained_404_and_api_still_works(dirs: SimpleNamespace) -> None:
    client = make_client(dirs)
    assert not dirs.web.exists()
    response = client.get("/")
    assert response.status_code == 404
    assert "web/index.html 不存在" in response.json()["error"]
    assert client.get("/api/health").status_code == 200


def test_root_serves_index_and_relative_assets(dirs: SimpleNamespace) -> None:
    (dirs.web / "js").mkdir(parents=True)
    (dirs.web / "index.html").write_text("<!doctype html><title>reader</title>", encoding="utf-8")
    (dirs.web / "js" / "app.js").write_text("export const ready = true;\n", encoding="utf-8")

    client = make_client(dirs)
    index = client.get("/")
    assert index.status_code == 200
    assert "reader" in index.text
    assert client.get("/api/health").json()["web_static"] is True
    asset = client.get("/js/app.js")
    assert asset.status_code == 200
    assert "export const ready" in asset.text


def test_unknown_route_returns_json_404(dirs: SimpleNamespace) -> None:
    response = make_client(dirs).get("/api/nope")
    assert response.status_code == 404
    assert response.json()["status"] == 404


def test_samples_mount_serves_fixture_and_tolerates_missing(dirs: SimpleNamespace) -> None:
    """夹具模式（?fixture=<id> → samples/<id>/doc.json）走本服务也能用。"""
    config = Config(base_url="https://gateway.invalid/provider/v1", model="fake/model")
    samples = dirs.tmp / "samples"
    app = build_app(
        data_dir=dirs.data,
        profiles_dir=dirs.profiles,
        web_dir=dirs.web,
        samples_dir=samples,
        config=config,
    )
    client = TestClient(app)
    # 目录还不存在：可见 404，不是 500
    assert client.get("/samples/x/doc.json").status_code == 404

    (samples / "fixture-doc").mkdir(parents=True)
    (samples / "fixture-doc" / "doc.json").write_text('{"meta": {"source_id": "fixture-doc"}}', encoding="utf-8")
    served = client.get("/samples/fixture-doc/doc.json")
    assert served.status_code == 200
    assert served.json()["meta"]["source_id"] == "fixture-doc"


def test_project_web_index_is_served_when_present(dirs: SimpleNamespace) -> None:
    """真实项目里 web/ 已经落地时，build_app 的默认目录应当能直接托管它。"""
    project_root = Path(__file__).resolve().parents[1]
    if not (project_root / "web" / "index.html").is_file():
        pytest.skip("web/index.html 还不存在（前端由另一路提供）")
    from ar.server import build_app as real_build

    app = real_build(data_dir=dirs.data, profiles_dir=dirs.profiles)
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "html" in response.headers["content-type"]


def test_root_redirects_page_params_and_ignores_other_query(dirs: SimpleNamespace) -> None:
    """页面参数冻结为 ``?source=<source_id>``：带它的 ``/`` **302** 到 ``/reader``，query 原样带走。

    2026-10-15 起 ``/`` 是**启动页**、``/reader`` 才是阅读器页（`PLAN` 的启动页功能）：
    老链接 ``/?source=<id>`` 必须继续能用，所以这里给 302 而不是 404；跳过去之后
    ``reader.html`` 还没就位时是**带原因的 404**（绝不悄悄回落到启动页）。
    不带页面参数的 ``/`` 照旧托管 ``index.html``（见下面的 ``/?debug=1``）。
    新端点的完整覆盖在 ``tests/test_server_sources.py``。
    """
    (dirs.web).mkdir(parents=True, exist_ok=True)
    (dirs.web / "index.html").write_text("<!doctype html><p>startup-page</p>", encoding="utf-8")
    client = make_client(dirs)

    response = client.get("/?source=capital-v1-ch01", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/reader?source=capital-v1-ch01"

    landed = client.get("/?source=capital-v1-ch01")  # 跟随跳转后的落点
    assert landed.status_code == 404, "reader.html 还不存在：不许回落到启动页、更不许 500"
    assert "reader.html" in landed.text
    assert "startup-page" not in landed.text

    unaffected = client.get("/?debug=1")
    assert unaffected.status_code == 200
    assert "startup-page" in unaffected.text


def test_api_doc_shape_matches_frontend_fixture(dirs: SimpleNamespace, real_sample: Path) -> None:
    """`/api/doc` 的形状必须与前端夹具同形（只多一个追加字段 `degraded`）。

    前端夹具（另一路的 samples/<id>/doc.json）已被 schema 测试锁住；这里拿同一份材料
    走真实摄取 + 真实接口，逐段比对，并让夹具的 concepts 过一遍我的契约校验。
    """
    fixture_path = Path(__file__).resolve().parents[1] / "samples" / "capital-v1-ch01" / "doc.json"
    ingest(real_sample, "capital-v1-ch01", dirs.data)
    client = make_client(dirs)

    payload = client.get("/api/doc/capital-v1-ch01").json()
    assert set(payload) == {"meta", "rewritten", "changes", "concepts", "degraded"}
    assert set(payload["meta"]) == {"source_id", "ingest", "paragraphs"}
    assert payload["rewritten"] is None  # M3 未实现：不给假内容，但 degraded 里说明原因
    assert any("rewritten.md" in note for note in payload["degraded"])

    if not fixture_path.is_file():
        pytest.skip(f"前端夹具不存在：{fixture_path}")
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert set(fixture) | {"degraded"} == set(payload)
    mine = [(p["id"], p["index"], p["text"], p["text_hash"]) for p in payload["meta"]["paragraphs"]]
    theirs = [(p["id"], p["index"], p["text"], p["text_hash"]) for p in fixture["meta"]["paragraphs"]]
    assert mine == theirs, "真实摄取的段落表必须与前端夹具逐段一致（含 p022 hash=4b2b7c3c）"
    assert payload["meta"]["source_id"] == fixture["meta"]["source_id"] == "capital-v1-ch01"

    # 两边独立实现要互认：夹具的 concepts 必须能过我的严格契约校验
    assert validate_inventory(fixture["concepts"], payload["meta"]) == []

    # 把夹具的三份产物放进 data/<id>/，接口应当原样透传、degraded 清空
    target = dirs.data / "capital-v1-ch01"
    (target / "concepts.json").write_text(json.dumps(fixture["concepts"], ensure_ascii=False), encoding="utf-8")
    (target / "rewritten.md").write_text(fixture["rewritten"], encoding="utf-8")
    (target / "changes.json").write_text(json.dumps(fixture["changes"], ensure_ascii=False), encoding="utf-8")
    full = client.get("/api/doc/capital-v1-ch01").json()
    assert full["degraded"] == []
    assert full["rewritten"] == fixture["rewritten"]
    assert full["changes"] == fixture["changes"]
    assert full["concepts"] == fixture["concepts"]


# -------------------------------------------------------------------- events --
AT = "2026-10-13T18:20:00+08:00"


def _events_file(dirs: SimpleNamespace) -> Path:
    return dirs.profiles / "local-default" / "events.jsonl"


def test_events_appends_jsonl_and_stamps_reader_server_side(dirs: SimpleNamespace, sample_md: Path) -> None:
    ingest(sample_md, "sample", dirs.data)
    response = make_client(dirs).post(
        "/api/events",
        json={
            "events": [
                {
                    "id": "e1",
                    "at": AT,
                    "kind": "explain",
                    "source_id": "sample",
                    "paragraph": "p004",
                    "reader": "someone-else",  # 服务端盖章，这个值不认
                    "scope": "paragraph",
                    "explain_kind": "explain",
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"ok", "written", "duplicates", "total", "degraded"}
    assert payload == {"ok": True, "written": 1, "duplicates": 0, "total": 1, "degraded": []}

    text = _events_file(dirs).read_text(encoding="utf-8")
    assert text.endswith("\n") and text.count("\n") == 1, "一行一条 JSON、结尾换行"
    event = json.loads(text)
    assert event["reader"] == "local-default" and "someone-else" not in text
    assert event["v"] == 1 and event["id"] == "e1" and event["paragraph"] == "p004"
    assert event["kind"] == "explain" and event["at"] == AT
    assert not (dirs.profiles / "local-default" / "profile.yaml").exists()


def test_events_accepts_single_object_and_dedups_by_id_and_composite_key(
    dirs: SimpleNamespace, sample_md: Path
) -> None:
    ingest(sample_md, "sample", dirs.data)
    client = make_client(dirs)

    single = client.post("/api/events", json={"id": "e9", "kind": "read_complete", "source_id": "sample", "at": AT})
    assert single.status_code == 200, single.text
    assert single.json()["written"] == 1 and single.json()["total"] == 1

    again = client.post("/api/events", json={"id": "e9", "kind": "read_complete", "source_id": "sample", "at": AT})
    assert again.json()["written"] == 0 and again.json()["duplicates"] == 1 and again.json()["ok"] is True
    assert any("重复" in note for note in again.json()["degraded"])

    # 缺 id → 复合键兜底去重
    dwell = {"kind": "dwell", "source_id": "sample", "paragraph": "p004", "ms": 900, "at": AT}
    assert client.post("/api/events", json=dwell).json()["written"] == 1
    repeat = client.post("/api/events", json=dwell).json()
    assert repeat["written"] == 0 and repeat["duplicates"] == 1
    assert len(_events_file(dirs).read_text(encoding="utf-8").strip().splitlines()) == 2


def test_events_drops_unknown_paragraph_and_unknown_kind_without_writing(
    dirs: SimpleNamespace, sample_md: Path
) -> None:
    ingest(sample_md, "sample", dirs.data)
    response = make_client(dirs).post(
        "/api/events",
        json={
            "events": [
                {"id": "ok1", "kind": "explain", "source_id": "sample", "paragraph": "p004", "at": AT},
                {"id": "ok1", "kind": "explain", "source_id": "sample", "paragraph": "p004", "at": AT},  # 批内重复
                {"id": "bad", "kind": "explain", "source_id": "sample", "paragraph": "p404", "at": AT},
                {"kind": "没这个", "source_id": "sample", "at": AT},
            ]
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True and payload["written"] == 1
    assert payload["duplicates"] == 1 and payload["total"] == 4
    joined = " / ".join(payload["degraded"])
    assert "p404" in joined and "没这个" in joined and "重复" in joined

    lines = _events_file(dirs).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["id"] == "ok1", "被丢弃的事件绝不许写进文件"


@pytest.mark.parametrize("source_id", [None, "", "   ", 7, "bad..id", "有空格 的"])
def test_events_rejects_illegal_source_id_with_400(dirs: SimpleNamespace, source_id: object) -> None:
    response = make_client(dirs).post(
        "/api/events", json={"events": [{"kind": "read_complete", "source_id": source_id}]}
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]
    assert not _events_file(dirs).exists()


def test_events_uningested_material_is_ok_false_and_writes_nothing(dirs: SimpleNamespace) -> None:
    response = make_client(dirs).post(
        "/api/events",
        json={"events": [{"id": "x", "kind": "explain", "source_id": "nosuchdoc", "paragraph": "p001", "at": AT}]},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is False and payload["written"] == 0 and payload["total"] == 1
    assert any("尚未摄取" in note for note in payload["degraded"])
    assert not _events_file(dirs).exists()


def test_events_partial_batch_with_missing_material_reports_ok_false(dirs: SimpleNamespace, sample_md: Path) -> None:
    ingest(sample_md, "sample", dirs.data)
    payload = (
        make_client(dirs)
        .post(
            "/api/events",
            json={
                "events": [
                    {"id": "a", "kind": "read_complete", "source_id": "nosuchdoc", "at": AT},
                    {"id": "b", "kind": "read_complete", "source_id": "sample", "at": AT},
                ]
            },
        )
        .json()
    )
    assert payload["ok"] is False  # 有材料没摄取：整体不许报成功
    assert payload["written"] == 1 and payload["total"] == 2
    assert any("尚未摄取" in note for note in payload["degraded"])


def test_events_write_failure_is_visible_and_never_faked(dirs: SimpleNamespace, sample_md: Path) -> None:
    """事件写失败：契约不变（200 + `ok:false` + `written:0` + `degraded`），但消息必须可读。

    `ar.events.append_events` 现在把 `OSError` 翻成 `WriteError`（点名目录 + 原始 errno），
    所以 `ar.server` 的 except 里同时列了 `WriteError` 与 `OSError`——**事件是尽力而为的采集**，
    不许因为换成可读错误就退化成 500（DESIGN §6）。
    """
    ingest(sample_md, "sample", dirs.data)
    target = _events_file(dirs)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()  # 把 events.jsonl 变成目录：追加必然失败

    response = make_client(dirs).post(
        "/api/events", json={"events": [{"id": "x", "kind": "read_complete", "source_id": "sample", "at": AT}]}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is False and payload["written"] == 0 and payload["total"] == 1
    note = next(n for n in payload["degraded"] if "写盘失败" in n)
    assert "WriteError" in note, f"坏目录给的是可读错误（不是裸 PermissionError）：{note}"
    # 文案点名的是**目录**（写不进去的是这个目录）+ 原始 errno，与 ar.jsonio 那条口径逐字一致
    assert str(target.parent) in note and "errno" in note, f"要点名是哪个目录、原始 errno 是多少：{note}"
    assert target.is_dir()


def test_events_never_touches_profile_yaml_bytes_or_mtime(dirs: SimpleNamespace, sample_md: Path) -> None:
    client = make_client(dirs)
    assert client.post("/api/profile/rating", json={"concept_id": "c01", "state": "known"}).status_code == 200
    path = dirs.profiles / "local-default" / "profile.yaml"
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    ingest(sample_md, "sample", dirs.data)
    response = client.post(
        "/api/events",
        json={
            "events": [
                {"id": "e1", "kind": "explain", "source_id": "sample", "paragraph": "p004", "at": AT},
                {"id": "e2", "kind": "read_complete", "source_id": "sample", "at": AT},
            ]
        },
    )
    assert response.json()["written"] == 2

    # 写一次画像就换指纹（路径, 字节数, mtime_ns）⇒ 讲解缓存全失效 + 前端会话键变化
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime
    assert _events_file(dirs).is_file()


def test_events_empty_and_malformed_bodies(dirs: SimpleNamespace) -> None:
    client = make_client(dirs)
    empty = client.post("/api/events", json={}).json()
    assert empty == {
        "ok": True,
        "written": 0,
        "duplicates": 0,
        "total": 0,
        "degraded": ["请求里没有事件（events 缺失或为空）：什么都没写"],
    }
    blank_list = client.post("/api/events", json={"events": []}).json()
    assert blank_list["written"] == 0 and blank_list["total"] == 0

    assert client.post("/api/events", json={"events": "不是数组"}).status_code == 400
    assert client.post("/api/events", json=[1, 2]).status_code == 400
    broken = client.post("/api/events", content=b"{not json", headers={"content-type": "application/json"})
    assert broken.status_code == 400 and "JSON" in broken.json()["error"]
    assert not _events_file(dirs).exists()


def test_events_route_sits_before_the_api_fallback(dirs: SimpleNamespace) -> None:
    response = make_client(dirs).get("/api/events")
    assert response.status_code == 404
    assert "接口不存在" in response.json()["error"], "新端点必须排在 /api/{rest:path} 兜底之前"
