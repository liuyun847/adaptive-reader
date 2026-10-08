"""配置：``config.local.json``（默认路径）→ ``Config``。

配置项（契约 §3）：``base_url`` / ``model`` / ``temperature`` / ``timeout_s`` /
``max_retries``，另有可选 ``max_tokens``（默认 ``null`` = 请求体不带该字段）与 ``api_key``。

**密钥纪律**：`api_key` 只在运行时取值，优先级为 环境变量 ``AR_API_KEY`` > 配置文件 ``api_key``。
取值后只存在 ``Config`` 里：``repr`` 与 ``public_dict()`` 都不含它的值，
解密后的值也不会被写进任何产物、日志或报错（打印一律走 ``public_dict()``）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import InputError
from .jsonio import read_json_if_exists
from .paths import DEFAULT_CONFIG_PATH

__all__ = ["API_KEY_ENV", "DEFAULT_CONFIG", "Config", "load_config", "redact"]

API_KEY_ENV = "AR_API_KEY"

DEFAULT_CONFIG: dict[str, Any] = {
    "base_url": "https://api.commandcode.ai/provider/v1",
    "model": "deepseek/deepseek-v4.1-flash",
    "temperature": 0.6,
    "timeout_s": 90.0,
    "max_retries": 1,
    "max_tokens": None,
}

_KNOWN_KEYS = (*DEFAULT_CONFIG.keys(), "api_key")


@dataclass(frozen=True)
class Config:
    """运行期配置。``api_key`` 不参与 ``repr`` / ``==``，避免任何意外泄漏。"""

    base_url: str = DEFAULT_CONFIG["base_url"]
    model: str = DEFAULT_CONFIG["model"]
    temperature: float | None = DEFAULT_CONFIG["temperature"]
    timeout_s: float = DEFAULT_CONFIG["timeout_s"]
    max_retries: int = DEFAULT_CONFIG["max_retries"]
    max_tokens: int | None = DEFAULT_CONFIG["max_tokens"]
    api_key: str = field(default="", repr=False, compare=False)
    api_key_origin: str | None = field(default=None, repr=False, compare=False)
    source_path: str | None = None
    unknown_keys: tuple[str, ...] = ()

    @property
    def model_configured(self) -> bool:
        """模型可用 = 有密钥 且 base_url/model 都填了。"""
        return bool(self.api_key) and bool(self.base_url) and bool(self.model)

    def public_dict(self) -> dict[str, Any]:
        """可打印 / 可进 API 响应的视图：不含密钥值，只说明它从哪来。"""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "temperature": self.temperature,
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "max_tokens": self.max_tokens,
            "api_key_present": bool(self.api_key),
            "api_key_origin": self.api_key_origin,
            "source_path": self.source_path,
            "unknown_keys": list(self.unknown_keys),
        }


def _coerce_float(name: str, value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"配置项 {name} 必须是数字，实际是 {type(value).__name__}")
    return float(value)


def _coerce_int(name: str, value: Any, *, minimum: int = 0) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"配置项 {name} 必须是整数，实际是 {type(value).__name__}")
    if value < minimum:
        raise InputError(f"配置项 {name} 不能小于 {minimum}，实际是 {value}")
    return value


def load_config(path: Path | str | None = None, *, env: dict[str, str] | None = None) -> Config:
    """读配置并解析密钥。``path=None`` 用 ``config.local.json``（不存在则用默认值）。"""
    env = os.environ if env is None else env
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    raw = read_json_if_exists(config_path)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise InputError(f"配置文件根节点必须是 JSON 对象：{config_path}")

    unknown = tuple(sorted(k for k in raw if k not in _KNOWN_KEYS))
    merged = {**DEFAULT_CONFIG, **{k: v for k, v in raw.items() if k in _KNOWN_KEYS}}

    base_url = merged["base_url"]
    model = merged["model"]
    if not isinstance(base_url, str) or not base_url.strip():
        raise InputError("配置项 base_url 必须是非空字符串")
    if not isinstance(model, str) or not model.strip():
        raise InputError("配置项 model 必须是非空字符串")

    timeout_s = _coerce_float("timeout_s", merged["timeout_s"]) or 90.0
    if timeout_s <= 0:
        raise InputError("配置项 timeout_s 必须大于 0")
    max_retries = _coerce_int("max_retries", merged["max_retries"], minimum=0)
    max_tokens = _coerce_int("max_tokens", merged["max_tokens"], minimum=1)

    file_key = raw.get("api_key")
    if file_key is not None and not isinstance(file_key, str):
        raise InputError("配置项 api_key 必须是字符串（空串表示未配置）")

    env_key = (env.get(API_KEY_ENV) or "").strip()
    file_key_clean = (file_key or "").strip()
    if env_key:
        api_key, origin = env_key, f"env:{API_KEY_ENV}"
    elif file_key_clean:
        api_key, origin = file_key_clean, f"file:{config_path}"
    else:
        api_key, origin = "", None

    return Config(
        base_url=base_url.strip(),
        model=model.strip(),
        temperature=_coerce_float("temperature", merged["temperature"]),
        timeout_s=timeout_s,
        max_retries=0 if max_retries is None else max_retries,
        max_tokens=max_tokens,
        api_key=api_key,
        api_key_origin=origin,
        source_path=str(config_path) if config_path.is_file() else None,
        unknown_keys=unknown,
    )


# 常见密钥形态：Bearer 令牌、sk- 前缀、以及调用方显式传入的密钥值
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{4,}")
_SK_RE = re.compile(r"sk-[A-Za-z0-9._\-]{8,}")
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{32,}")


def redact(text: Any, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """脱敏：抹掉显式密钥、``Bearer xxx``、``sk-xxx`` 与任何超长令牌串。

    任何要打印、要落盘、要进 HTTP 响应的错误文本都必须先过这里。
    """
    out = "" if text is None else str(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, "***REDACTED***")
    out = _BEARER_RE.sub("Bearer ***REDACTED***", out)
    out = _SK_RE.sub("sk-***REDACTED***", out)
    out = _LONG_TOKEN_RE.sub("***REDACTED***", out)
    return out


def with_api_key(config: Config, api_key: str, origin: str) -> Config:
    """测试 / 内部注入用：返回替换了密钥的副本（不落盘）。"""
    return replace(config, api_key=api_key, api_key_origin=origin)
