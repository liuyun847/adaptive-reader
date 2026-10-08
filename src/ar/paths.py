"""路径约定：产物落 ``data/<source_id>/``，画像落 ``profiles/<reader>/profile.yaml``。

默认目录都相对**项目根**解析（而不是当前工作目录），这样在任何地方执行
``uv run ar …`` 都会写进同一处；测试与临时运行用 ``--data-dir`` / ``--profiles-dir`` 覆盖。
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "PROJECT_ROOT",
    "DEFAULT_DATA_DIR",
    "DEFAULT_PROFILES_DIR",
    "DEFAULT_WEB_DIR",
    "DEFAULT_SAMPLES_DIR",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_READER",
    "SOURCE_ID_RE",
    "check_source_id",
    "check_reader_id",
    "doc_dir",
    "profile_path",
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_PROFILES_DIR = PROJECT_ROOT / "profiles"
DEFAULT_WEB_DIR = PROJECT_ROOT / "web"
DEFAULT_SAMPLES_DIR = PROJECT_ROOT / "samples"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.local.json"
DEFAULT_READER = "local-default"

# 只允许这些字符，避免 ``..`` 之类的路径穿越（HTTP 层与 CLI 共用同一个校验）
SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def check_source_id(source_id: str) -> str:
    """校验 source_id 并原样返回；非法则抛 ``InputError``。"""
    from .errors import InputError

    if not source_id or not SOURCE_ID_RE.match(source_id) or ".." in source_id:
        raise InputError(
            f"source_id 非法：{source_id!r}（只允许字母、数字、下划线、点、连字符，1–64 位，且不含 '..'）"
        )
    return source_id


def check_reader_id(reader: str) -> str:
    """读者 id 用同一套字符规则（它会变成目录名）。"""
    from .errors import InputError

    if not reader or not SOURCE_ID_RE.match(reader) or ".." in reader:
        raise InputError(f"reader id 非法：{reader!r}")
    return reader


def doc_dir(data_dir: Path | str, source_id: str) -> Path:
    """``data/<source_id>/``。"""
    return Path(data_dir) / check_source_id(source_id)


def profile_path(profiles_dir: Path | str, reader: str) -> Path:
    """``profiles/<reader>/profile.yaml``。"""
    return Path(profiles_dir) / check_reader_id(reader) / "profile.yaml"
