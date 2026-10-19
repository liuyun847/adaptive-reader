"""JSON / 文本 / 字节读写与时间戳（产物统一 UTF-8、``ensure_ascii=False``、缩进 2）。

写入一律走「临时文件 + ``os.replace``」（:func:`write_text` / :func:`write_bytes` 共用
:func:`_open_temp_in` 那一套），避免中途失败留下半个文件；
**坏路径快速失败**：目标目录不可写时立刻抛 ``WriteError``（CLI 退出码 2，消息里点名目录与 errno），
**临时文件那一步**不空转、不重试——见 :func:`_open_temp_in` 的说明。
唯一带重试的是最后那步 ``os.replace``：Windows 上「目标文件正被别的进程占着」会让改名抛
``PermissionError``（拒绝访问 / 共享冲突），那是**短暂**的，所以只对它做**有界短退避重试**
（最多 3 次、总等待 ≤ 200 ms），等完还是失败就原样抛——文案点名**是哪个文件被占着**，
不再混成一句「目录不可写」（见 :func:`_replace_reporting`）。
读取失败抛 ``InputError``（CLI 退出码 2），不静默吞掉。

**全项目的写盘实现只有这里一处**（2026-10-14 复核补漏）：``ar.ingest`` 的 ``source.md``（按字节，
必须与源文件逐字节一致）与 ``ar.profile.save_profile`` 的 ``profile.yaml`` 都走本模块的
「临时文件 + 有界重试 + 可读错误」；``ar.events.append_events`` 的 ``events.jsonl`` 是**唯一**
一处非原子写（纯追加的日志不该整文件重写），它的失败文案也取本模块的
:func:`readable_write_error`。**不许再有第二份手写的 ``tempfile.mkstemp`` 循环**——那种写法在
坏目录上会把 ``PermissionError`` 当成「候选名被占用」吞掉，表现为「不报错、单核 100%、
长时间不结束」（见 :func:`_open_temp_in`）。
"""

from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import InputError, WriteError

__all__ = [
    "now_iso",
    "read_text",
    "read_json",
    "write_text",
    "write_json",
    "write_bytes",
    "readable_write_error",
    "read_text_if_exists",
    "read_json_if_exists",
]

#: 候选临时文件名被占用时的重试上限。
#:
#: 为什么要有个上限：真正的「名字被占用」是极小概率事件（同目录、同一纳秒、同一个 16 位十六进制
#: 随机名），撞上几次就该换思路，而不是照着 ``tempfile.TMP_MAX`` 空转下去。**本机实测
#: （Windows / CPython 3.12.7）``tempfile.TMP_MAX = 2147483647``（21 亿级）**，也就是说
#: 在 Windows 上那个上限**近似无界**——常说的「10000 次」在这台机器上并不成立，别拿它当兜底。
#: 真正会把 CPU 打满的是「目录不可写」——那一种**一次都不重试**（见 :func:`_open_temp_in`）。
_TMP_NAME_ATTEMPTS = 32


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


def _readable_os_error(directory: Path, exc: OSError) -> str:
    """把 ``OSError`` 翻成一句人读的话：**点名目录 + 原始 errno**（不吞、不猜）。"""
    detail = exc.strerror or str(exc)
    return (
        f"写不进去：目录 {directory} 不可写"
        f"（errno {exc.errno}：{detail}）。"
        "请检查这个目录是否存在、是不是只读、当前用户有没有写权限"
        "（注意 `os.access(dir, os.W_OK)` 在 Windows 上会假阳性：目录 ACL 与只读属性它看不准，"
        "所以这里不预检、只认真正写的那一步）"
    )


def readable_write_error(directory: Path | str, exc: OSError) -> WriteError:
    """写盘失败的 ``OSError`` → :class:`~ar.errors.WriteError`（文案与原子写那条完全一致）。

    给**非原子写**的入口用（``ar.events.append_events`` 的追加写：读者数据只增不减，
    无法用「临时文件 + replace」），它们照样要「点名目录 + 原始 errno」这一句，
    免得同样的坏目录在不同入口给出两套文案（2026-10-14 复核补漏）。
    """
    return WriteError(_readable_os_error(Path(directory), exc))


def _open_temp_in(directory: Path, suffix: str) -> tuple[int, Path]:
    """在 ``directory`` 里**原子地**新建一个临时文件，返回 ``(fd, 路径)``。

    自己写这个循环而不是用 ``tempfile.mkstemp(dir=…)``，是因为**两种失败必须分得开**
    （2026-10-14 修的真实缺陷）：

    - **候选名已被占用**（``FileExistsError``）⇒ 换一个名字重试，最多 :data:`_TMP_NAME_ATTEMPTS` 次
      ——这是原有的「同名文件已存在」重试语义，一个字没丢；
    - **目录不可写 / 路径不合法**（``PermissionError`` 等 ``OSError``）⇒ **立刻**抛
      :class:`~ar.errors.WriteError`，消息里点名是哪个目录、原始 errno 是什么。

    ``tempfile.mkstemp`` 内部把 ``PermissionError`` 当成「名字被占用」一起吞掉，接着重试
    ``tempfile.TMP_MAX`` 次——**本机（Windows / CPython 3.12.7）实测那是 2147483647（21 亿级，
    近似无界，不是常说的 10000）**，最后抛一个语焉不详的 ``FileExistsError``
    （``No usable temporary file name found``）。Windows 上 ``os.access(dir, os.W_OK)`` 又是假阳性，
    所以坏目录要真写到那一步才暴露——现场表现是「**不报错、单核 100%、十几分钟不结束**」：
    实测一次真机调用因此卡死 13 分钟、进程被杀，响应与用量一起丢。
    """
    # 与 ``tempfile.mkstemp`` 同一套 flags（Windows 上加 ``O_BINARY``），O_EXCL 保证「新建」语义
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for _ in range(_TMP_NAME_ATTEMPTS):
        candidate = directory / f".tmp-{secrets.token_hex(8)}{suffix}"
        try:
            fd = os.open(candidate, flags, 0o600)
        except FileExistsError:
            continue  # 名字恰好被占用：换一个再试（这才叫「同名已存在」）
        except OSError as exc:
            # PermissionError / NotADirectoryError / FileNotFoundError… 一律**立即失败**
            raise readable_write_error(directory, exc) from exc
        return fd, candidate
    raise WriteError(
        f"写不进去：在目录 {directory} 里连试 {_TMP_NAME_ATTEMPTS} 个临时文件名都被占用。"
        "这不像名字冲突，更像路径 / 权限有问题，请人工看一眼这个目录"
    )


#: ``os.replace`` 撞上「目标文件正被别的进程占着」时的重试上限与退避（秒）。
#:
#: 只有这一类**短暂**冲突才值得等：Windows 上目标文件的句柄不带 ``FILE_SHARE_DELETE`` 时，
#: 改名直接抛 ``PermissionError``（``WinError 5`` 拒绝访问 / ``WinError 32`` 共享冲突）。
#: 有界是硬要求——最多 :data:`_REPLACE_ATTEMPTS` 次、总等待 ≤ 200 ms；等完还是失败就**原样抛**，
#: 绝不无限重试，也绝不把失败吞掉（吞掉＝静默丢一次写盘）。
_REPLACE_ATTEMPTS = 3

#: 每次重试前的退避；长度 = :data:`_REPLACE_ATTEMPTS` − 1（总等待 150 ms ≤ 200 ms 的上界）
_REPLACE_BACKOFF_SECONDS = (0.05, 0.10)


def _is_replace_conflict(exc: OSError) -> bool:
    """这个 ``os.replace`` 失败是不是「共享冲突 / 拒绝访问」这一类**可短暂重试**的错误。

    只认 Windows 的 ``WinError`` 5（拒绝访问）/ 32（共享冲突）/ 33（区域被占用）：这是「有人正开着
    目标文件」的典型表现（也可能是目标文件被设成了只读）。别的错误——路径不合法、目标是个目录、
    盘满（``ENOSPC``）——重试多少次都一样，**一次都不重试**：POSIX 上 ``winerror`` 不存在，
    于是走的是原来那条「立刻抛 + 目录不可写文案」，行为一个字没变。
    """
    return getattr(exc, "winerror", None) in (5, 32, 33)


def _busy_target_error(path: Path, exc: OSError, *, retries: int) -> WriteError:
    """``os.replace`` 因目标被占着/拒绝访问而失败的 ``WriteError``（**点名文件**，不混成「目录不可写」）。

    排查方向必须分开：临时文件是在**同一个目录**里建出来的，说明目录写得进去，问题在**目标文件**
    本身（被别的进程开着 / 只读属性）。文案因此点名文件、点明重试过几次，并给出下一步动作。
    """
    waited_ms = int(round(sum(_REPLACE_BACKOFF_SECONDS[:retries]) * 1000))
    retried = f"（已重试 {retries} 次，共等 {waited_ms} ms）" if retries else ""
    return WriteError(
        f"写不进去：目标文件 {path} 换不了名（os.replace 失败）{retried}——最像是**它正被别的进程占用**"
        "（Windows 上目标文件的句柄不带 FILE_SHARE_DELETE 时，改名会直接拒绝访问），"
        "也可能是目标文件被设成了只读。请先关掉占用它的程序 / 去掉只读属性再重试。"
        f"若确认都不是，再看目录权限：{path.parent}"
        f"（原始错误：{type(exc).__name__}：errno {exc.errno}：{exc.strerror or exc}）"
    )


def _replace_reporting(tmp_path: Path, path: Path) -> None:
    """``os.replace`` + 对「目标被占着」的**有界短退避重试**；失败抛可读的 ``WriteError``。

    退避是**有界**的（见 :data:`_REPLACE_ATTEMPTS`）：等完还是失败就抛
    :func:`_busy_target_error`（点名文件与被占用这件事）；不是这一类冲突的 ``OSError``
    **原样往外抛**，交给 :func:`_atomic_write` 翻成既有的「目录不可写」文案（口径不变）。
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp_path, path)
        except OSError as exc:
            if not _is_replace_conflict(exc):
                raise
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise _busy_target_error(path, exc, retries=attempt) from exc
            time.sleep(_REPLACE_BACKOFF_SECONDS[attempt])
        else:
            return  # 改名成功：临时文件已经不在了，绝不能再来一次


def _atomic_write(
    path: Path,
    *,
    suffix: str,
    mode: str,
    write: Callable[[Any], None],
    encoding: str | None = None,
) -> Path:
    """原子写内核：``mkdir`` → 目录内建临时文件（:func:`_open_temp_in`）→ 写入 → ``os.replace``。

    文本与字节两条路（:func:`write_text` / :func:`write_bytes`）共用这一段，**只有一处**
    坏路径处理：``mkdir`` / 建临时文件 / 写入任何一步抛 ``OSError``，都立刻翻成
    :class:`~ar.errors.WriteError`（点名目录与 errno）并清掉临时文件；改名那一步另有
    :func:`_replace_reporting`（有界重试 + 「目标被占着」的专属文案）；不是 ``OSError`` 的
    意外（例如编码错）也要清残骸再原样抛。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise readable_write_error(path.parent, exc) from exc
    fd, tmp_path = _open_temp_in(path.parent, suffix)
    # 文本模式固定 newline="\n"：产物行尾统一 LF，不随平台变（写盘口径的一部分）
    kwargs: dict[str, Any] = {"encoding": encoding, "newline": "\n"} if encoding is not None else {}
    try:
        with os.fdopen(fd, mode, **kwargs) as fh:
            write(fh)
        _replace_reporting(tmp_path, path)
    except OSError as exc:
        Path(tmp_path).unlink(missing_ok=True)
        raise readable_write_error(path.parent, exc) from exc
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    return path


def write_text(path: Path | str, text: str, *, encoding: str = "utf-8") -> Path:
    """原子写文本：临时文件 + ``os.replace``；坏路径**快速失败**（``WriteError``）。

    临时文件建在**目标目录内**（``os.replace`` 要求同一文件系统才能原子），所以目标目录不可写
    时这一步就会失败——失败要立刻可见，不许空转（见 :func:`_open_temp_in`）。
    """
    p = Path(path)
    return _atomic_write(p, suffix=p.suffix, mode="w", write=lambda fh: fh.write(text), encoding=encoding)


def write_bytes(path: Path | str, payload: bytes) -> Path:
    """原子写**字节**（用途：``ar.ingest`` 的 ``source.md`` 必须与源文件逐字节一致）。

    与 :func:`write_text` 走同一条写盘路径（同一份临时文件命名、同一套有界重试、同一句可读错误），
    区别只在以二进制模式写入、不做任何编码 / 行尾转换。
    """
    p = Path(path)
    return _atomic_write(p, suffix=p.suffix, mode="wb", write=lambda fh: fh.write(payload))


def write_json(path: Path | str, payload: Any) -> Path:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    return write_text(path, text)
