"""JSON / 文本读写与时间戳（产物统一 UTF-8、``ensure_ascii=False``、缩进 2）。

写入一律走「临时文件 + ``os.replace``」，避免中途失败留下半个文件；
读取失败抛 ``InputError``（CLI 退出码 2），不静默吞掉。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import InputError

__all__ = ["now_iso", "read_text", "read_json", "write_text", "write_json", "read_text_if_exists", "read_json_if_exists"]


def now_iso() -> str:
    """本地时区 ISO8601（秒精度），用于 ``at`` / ``generated_at``。"""
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def read_text(path: Path | str, *, encoding: str = "utf-8") -> str:
    p = Path(path)
    try:
        return p.read_text(encoding=encoding)
    except FileNotFoundError as exc:
        raise InputError(f"文件不存在：{p}") from exc
    except UnicodeDecodeError as exc:
        raise InputError(f"文件不是合法 {encoding} 文本：{p}（{exc}）") from exc


def read_text_if_exists(path: Path | str, *, encoding: str = "utf-8") -> str | None:
    p = Path(path)
    return p.read_text(encoding=encoding) if p.is_file() else None


def read_json(path: Path | str) -> Any:
    p = Path(path)
    raw = read_text(p)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError(f"JSON 解析失败：{p}（第 {exc.lineno} 行：{exc.msg}）") from exc


def read_json_if_exists(path: Path | str) -> Any | None:
    p = Path(path)
    return read_json(p) if p.is_file() else None


def write_text(path: Path | str, text: str, *, encoding: str = "utf-8") -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp-", suffix=p.suffix)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
        os.replace(tmp_name, p)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return p


def write_json(path: Path | str, payload: Any) -> Path:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    return write_text(path, text)
