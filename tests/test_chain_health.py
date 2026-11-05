"""多源回退链的 HTTP 面：``/api/health`` 在链模式下多给一跳一行，单源模式形状逐字不变。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from starlette.testclient import TestClient

from ar.chain import Route
from ar.config import Config
from ar.server import build_app


def dirs(tmp_path: Path) -> SimpleNamespace:
    data = tmp_path / "data"
    profiles = tmp_path / "profiles"
    web = tmp_path / "web"
    for path in (data, profiles, web):
        path.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(data=data, profiles=profiles, web=web)


def client_for(scope: SimpleNamespace, config: Config) -> TestClient:
    app = build_app(
        data_dir=scope.data,
        profiles_dir=scope.profiles,
        web_dir=scope.web,
        samples_dir=None,
        config=config,
    )
    return TestClient(app)


def test_health_single_source_shape_is_unchanged(tmp_path: Path) -> None:
    scope = dirs(tmp_path)
    config = Config(base_url="https://gateway.invalid/provider/v1", model="fake/model")
    payload = client_for(scope, config).get("/api/health").json()
    assert payload["model"] == "fake/model"
    assert "routes" not in payload


def test_health_chain_mode_lists_every_hop_without_secrets(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("CMD_API_KEY", "cmd-" + "a" * 20)
    monkeypatch.setenv("OPENCODE_API_KEY", "oc-" + "b" * 20)
    scope = dirs(tmp_path)
    routes = (
        Route("commandcode", "deepseek/deepseek-v4.1-flash", "https://a.invalid/v1", "CMD_API_KEY"),
        Route("opencode-go", "deepseek-v4.1-flash", "https://b.invalid/v1", "OPENCODE_API_KEY"),
    )
    config = Config(base_url="", model="", routes=routes)
    payload = client_for(scope, config).get("/api/health").json()

    assert payload["model_configured"] is True
    assert payload["model"].startswith("commandcode/deepseek/deepseek-v4.1-flash")
    assert [item["provider"] for item in payload["routes"]] == ["commandcode", "opencode-go"]
    assert payload["routes"][0]["api_key_origin"] == "env:CMD_API_KEY"
    assert payload["routes"][0]["cooldown_ms"] == 0 and payload["routes"][0]["cooldown_reason"] is None
    body = client_for(scope, config).get("/api/health").text
    assert "cmd-" + "a" * 20 not in body and "oc-" + "b" * 20 not in body
