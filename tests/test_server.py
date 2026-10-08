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
    return TestClient(app)


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


def test_root_ignores_source_query_param(dirs: SimpleNamespace) -> None:
    """页面参数冻结为 ``?source=<source_id>``：查询串不能影响 `/` 的页面托管。"""
    (dirs.web).mkdir(parents=True, exist_ok=True)
    (dirs.web / "index.html").write_text("<!doctype html><p>page</p>", encoding="utf-8")
    response = make_client(dirs).get("/?source=capital-v1-ch01")
    assert response.status_code == 200
    assert "page" in response.text


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
