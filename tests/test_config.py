"""配置与密钥纪律（契约 §3：密钥只在运行时读，任何地方都不得出现值）。"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from ar.config import API_KEY_ENV, DEFAULT_CONFIG, Config, load_config, redact
from ar.errors import InputError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FAKE_KEY = "sk-" + "b" * 40


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_defaults_without_config_file(tmp_path: Path) -> None:
    config = load_config(tmp_path / "nope.json", env={})
    assert config.base_url == DEFAULT_CONFIG["base_url"]
    assert config.model == DEFAULT_CONFIG["model"]
    assert config.timeout_s == 90.0
    assert config.max_retries == 1
    assert config.max_tokens is None
    assert config.api_key == "" and config.api_key_origin is None
    assert config.model_configured is False


def test_config_file_overrides_and_unknown_keys(tmp_path: Path) -> None:
    path = _write(tmp_path, {"model": "other/model", "temperature": 0.2, "typo_key": 1})
    config = load_config(path, env={})
    assert config.model == "other/model"
    assert config.temperature == 0.2
    assert config.unknown_keys == ("typo_key",)
    assert config.source_path == str(path)


def test_env_key_wins_over_file(tmp_path: Path) -> None:
    path = _write(tmp_path, {"api_key": "file-key-value"})
    config = load_config(path, env={API_KEY_ENV: FAKE_KEY})
    assert config.api_key == FAKE_KEY
    assert config.api_key_origin == f"env:{API_KEY_ENV}"
    assert config.model_configured is True


def test_file_key_used_when_env_empty(tmp_path: Path) -> None:
    path = _write(tmp_path, {"api_key": FAKE_KEY})
    config = load_config(path, env={API_KEY_ENV: "   "})
    assert config.api_key == FAKE_KEY
    assert config.api_key_origin == f"file:{path}"


def test_blank_api_key_means_unconfigured(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, {"api_key": ""}), env={})
    assert config.api_key == ""
    assert config.model_configured is False


def test_key_never_appears_in_repr_public_dict_or_errors(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, {"api_key": FAKE_KEY}), env={})
    assert FAKE_KEY not in repr(config)
    assert FAKE_KEY not in json.dumps(config.public_dict(), ensure_ascii=False)
    assert config.public_dict()["api_key_present"] is True
    assert config.public_dict()["api_key_origin"].startswith("file:")


def test_redact_scrubs_secrets_and_token_shapes() -> None:
    text = f"authorization: Bearer {FAKE_KEY} | 另外 sk-{'c' * 20} | {'d' * 40}"
    cleaned = redact(text, secrets=[FAKE_KEY])
    assert FAKE_KEY not in cleaned
    assert "sk-" + "c" * 20 not in cleaned
    assert "d" * 40 not in cleaned
    assert "REDACTED" in cleaned


def test_bad_config_types_raise(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="timeout_s"):
        load_config(_write(tmp_path, {"timeout_s": "90"}), env={})
    with pytest.raises(InputError, match="max_retries"):
        load_config(_write(tmp_path, {"max_retries": -1}), env={})
    with pytest.raises(InputError, match="base_url"):
        load_config(_write(tmp_path, {"base_url": ""}), env={})
    with pytest.raises(InputError, match="api_key"):
        load_config(_write(tmp_path, {"api_key": 123}), env={})
    bad = tmp_path / "broken.json"
    bad.write_text("{ not json", encoding="utf-8")
    with pytest.raises(InputError, match="JSON 解析失败"):
        load_config(bad, env={})


def test_config_is_frozen_and_key_excluded_from_equality() -> None:
    config = Config()
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.model = "x"  # type: ignore[misc]
    assert Config(api_key="one") == Config(api_key="two")  # 密钥不参与比较


def test_project_example_config_contains_no_secret() -> None:
    """交付文件里绝不能出现密钥：模板的 api_key 必须是空串，且没有令牌形态的串。"""
    example = json.loads((PROJECT_ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert example["api_key"] == ""
    # 下划线开头的键是注释（与 retry 段同一约定）：模板允许写，读配置时忽略
    assert set(DEFAULT_CONFIG) | {"api_key"} >= {key for key in example if not key.startswith("_")}
    raw = (PROJECT_ROOT / "config.example.json").read_text(encoding="utf-8")
    assert "sk-" not in raw
    assert not re.search(r"[A-Za-z0-9_\-]{32,}", raw)
