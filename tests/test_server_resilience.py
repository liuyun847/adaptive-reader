"""``GET /api/sources`` 与 ``POST /api/ingest`` 的**抗崩**用例（2026-10-15 只读对抗复核实测出的 5 条）。

全是「实测能复现的 500 / 静默」，每条都照**契约**写（不是照实现写）：

1. **坏时间戳**：产物 mtime 超出本地时间可表示范围（Windows 上 ``datetime.fromtimestamp`` 的上限
   约公元 3000 年）⇒ 这一条的 ``mtime`` 写 ``null`` + 一条点名「哪个材料 / 哪个文件 / 什么原因」的
   ``degraded``，其余材料照常列出来；**一个坏时间戳不许把整份清单打成 500**；
2. **产物读不出来**（被别的进程独占 / 权限不足）⇒ 同「坏 JSON」一档：那一块 ``null`` + 可读说明；
3. **``content`` 里有孤立代理项**（编不成 UTF-8）⇒ **400** + 可读文案（不是 500），且一个字节都不写；
4. **``data/`` 下的悬空 junction / 符号链接 / stat 失败的条目** ⇒ 一条可读说明（点名条目与原因）；
   普通散文件照旧**静默跳过**（既有口径，本文件不动它）；
5. **``os.replace`` 撞上「目标文件被别的进程占着」** ⇒ 有界短退避重试；等完还是失败则原样抛，
   文案要能区分「目标文件被占用」与「目录不可写」，并点名文件与重试次数。

前 4 条在 ``tmp_path`` 的隔离目录里跑；第 5 条是 ``ar.jsonio`` 的原子写（``/api/ingest`` 的落盘路径），
直接打函数。独占句柄与 junction 是 Windows 专有语义，那几条用例在非 Windows 上 skip。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from starlette.testclient import TestClient

from ar import jsonio
from ar.errors import WriteError
from ar.jsonio import write_text
from test_server_sources import (
    DECISIONS_META,
    FIDELITY_OK,
    make_client,
    make_material,
    staging_leftovers,
)

#: 本机（Windows）本地时间转换的上限约公元 3000 年：``1e11`` 秒 ≈ 公元 5138 年，
#: NTFS 存得下（``os.utime`` 成功），但 ``datetime.fromtimestamp`` 抛 ``OSError [Errno 22]``。
FAR_FUTURE = 1e11

WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt", reason="独占句柄 / junction 是 Windows 的文件共享语义"
)


# ------------------------------------------------------------------------ 工具 --
def sources_response(dirs: SimpleNamespace, *, client: TestClient | None = None) -> Any:
    """打 ``GET /api/sources`` 拿**原始响应**（不预设状态码，好让 500 自己现形）。"""
    if client is None:
        client = make_client(dirs, raise_server_exceptions=False)
    return client.get("/api/sources")


def payload_of(response: Any) -> dict[str, Any]:
    """200 + JSON 响应体；不是 200 就把状态码与正文一起抛出来（这就是「不许 500」的判据）。"""
    assert response.status_code == 200, f"清单端点不许 500：{response.status_code} {response.text[:400]}"
    assert response.headers["content-type"].startswith("application/json")
    return response.json()


def notes_of(payload: dict[str, Any]) -> str:
    return " / ".join(payload["degraded"])


def entry_of(payload: dict[str, Any], source_id: str) -> dict[str, Any]:
    rows = [row for row in payload["sources"] if row["source_id"] == source_id]
    assert rows, f"材料 {source_id} 没被列出来：{payload['sources']}"
    return rows[0]


def entry_notes(payload: dict[str, Any], source_id: str) -> str:
    """只属于这一条材料的说明（**别的材料的说明不算**：降级必须点名到具体条目）。"""
    return " / ".join(note for note in payload["degraded"] if note.startswith(f"{source_id}："))


def hold_open_exclusively(path: Path) -> Callable[[], None]:
    """把 ``path`` 以**独占**模式打开（Windows ``CreateFileW`` + ``dwShareMode=0``），返回关闭函数。

    Python 的 ``open()`` 在 Windows 上走 MSVCRT 的「共享读 / 共享写」语义，拿它挡不住别的读者；
    只有 ``dwShareMode=0`` 才等价于「另一个进程正独占这个文件」，读它就会 ``PermissionError``。
    """
    import ctypes

    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateFileW(
        str(path), GENERIC_READ, 0, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None
    )
    assert handle and handle != 2**64 - 1, f"独占打开失败（CreateFileW 返回 {handle}）：{path}"

    def close() -> None:
        kernel32.CloseHandle(ctypes.c_void_p(handle))

    return close


def make_junction(link: Path, target: Path) -> None:
    """``mklink /J``（junction 不需要管理员权限，符号链接才需要）。"""
    done = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    if done.returncode != 0 or not os.path.lexists(link):
        pytest.skip(f"这台机器建不出 junction（{done.returncode}）：{done.stdout}{done.stderr}")


def shadow_stat_on(monkeypatch: pytest.MonkeyPatch, name: str, error: OSError) -> None:
    """让**某个文件名**的 ``Path.stat`` 抛指定错误（真机上的「权限不足 / 枚举不到」那一档）。"""
    real_stat = Path.stat

    def fake_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self.name == name:
            raise error
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", fake_stat)


def sharing_conflict(target: Path) -> PermissionError:
    """``os.replace`` 撞上「目标文件被别的进程占着」时 Windows 给的那个错（``WinError 5``）。"""
    exc = PermissionError(13, "另一个进程正开着这个文件（模拟 WinError 5）", str(target))
    exc.winerror = 5
    return exc


# ------------------------------------ ① 坏时间戳：那一条 null + 可读说明，别整份 500 --
def test_sources_far_future_artifact_mtime_degrades_instead_of_500(
    dirs: SimpleNamespace, sample_md: Path
) -> None:
    """产物 mtime 超出可表示范围 ⇒ ``mtime: null`` + 点名材料与文件，**其余字段照常**（绝不 500）。"""
    target = make_material(dirs, sample_md, "alpha", changes=FIDELITY_OK, decisions=DECISIONS_META)
    os.utime(target / "changes.json", (FAR_FUTURE, FAR_FUTURE))

    payload = payload_of(sources_response(dirs))

    entry = entry_of(payload, "alpha")
    assert entry["mtime"] is None, "坏时间戳只该让这一条写 null，不许编造时间"
    assert entry["paragraphs"] == 6, "坏时间戳不许影响别的字段（段数来自 source.meta.json）"
    assert entry["rewrite"] is not None and entry["fidelity"] is not None, "别的块照常"
    note = entry_notes(payload, "alpha")
    assert "changes.json" in note, f"降级必须点名是哪个文件：{note}"
    assert "mtime" in note
    assert ("超出" in note or "OSError" in note or "Errno 22" in note), f"降级必须说清原因：{note}"


def test_sources_far_future_source_meta_mtime_degrades_instead_of_500(
    dirs: SimpleNamespace, sample_md: Path
) -> None:
    """三份产物一份都没有时退回 ``source.meta.json`` 的 mtime；它超范围也**只 null 那一条**。"""
    target = make_material(dirs, sample_md, "alpha", concepts=False, rewritten=False)
    os.utime(target / "source.meta.json", (FAR_FUTURE, FAR_FUTURE))

    payload = payload_of(sources_response(dirs))

    entry = entry_of(payload, "alpha")
    assert entry["mtime"] is None
    assert entry["paragraphs"] == 6
    note = entry_notes(payload, "alpha")
    assert "source.meta.json" in note and "mtime" in note, note


def test_sources_far_future_mtime_does_not_hide_other_materials(
    dirs: SimpleNamespace, sample_md: Path
) -> None:
    """一棵树里有一条坏时间戳，**别的材料照常列出来**（不是整份清单全灭）。"""
    good = make_material(dirs, sample_md, "alpha", decisions=DECISIONS_META, changes=FIDELITY_OK)
    bad = make_material(dirs, sample_md, "zeta", decisions=DECISIONS_META, changes=FIDELITY_OK)
    os.utime(good / "rewritten.md", (1_700_000_000.0, 1_700_000_000.0))
    os.utime(bad / "rewritten.md", (FAR_FUTURE, FAR_FUTURE))

    payload = payload_of(sources_response(dirs))

    assert [row["source_id"] for row in payload["sources"]] == ["alpha", "zeta"]
    assert entry_of(payload, "alpha")["mtime"] is not None, "好材料的时间照给"
    assert entry_of(payload, "zeta")["mtime"] is None
    stale = [note for note in payload["degraded"] if "超出" in note]
    assert len(stale) == 1 and stale[0].startswith("zeta："), f"坏时间戳的说明只该记在坏材料头上：{stale}"


# ------------------------------------------- ② 产物读不出来：null + 点名文件与原因 --
@WINDOWS_ONLY
def test_sources_meta_locked_by_another_process_degrades_instead_of_500(
    dirs: SimpleNamespace, sample_md: Path
) -> None:
    """``source.meta.json`` 被别的进程独占 ⇒ 200 + 段数 / 摄取时间 ``null`` + 点名文件与原因。

    真缺陷是裸 ``PermissionError`` 冒到兜底处理器（500）。契约是「那一块 ``null`` + 一条可读降级」。
    """
    target = make_material(dirs, sample_md, "alpha", at="2026-10-05T12:00:00+08:00")
    close = hold_open_exclusively(target / "source.meta.json")
    try:
        payload = payload_of(sources_response(dirs))
    finally:
        close()

    entry = entry_of(payload, "alpha")
    assert entry["paragraphs"] is None and entry["ingested_at"] is None
    note = entry_notes(payload, "alpha")
    assert "source.meta.json" in note, f"降级必须点名文件：{note}"
    assert ("PermissionError" in note or "拒绝访问" in note or "errno 13" in note), f"要说清原因：{note}"


@WINDOWS_ONLY
def test_sources_artifact_locked_by_another_process_degrades_that_block_only(
    dirs: SimpleNamespace, sample_md: Path
) -> None:
    """``concepts.json`` 被独占 ⇒ 只有 ``concepts`` 那一块 ``null`` + 说明，其余块照常。"""
    target = make_material(dirs, sample_md, "alpha", decisions={"generated_at": "2026-10-13T12:27:42+08:00"})
    close = hold_open_exclusively(target / "concepts.json")
    try:
        payload = payload_of(sources_response(dirs))
    finally:
        close()

    entry = entry_of(payload, "alpha")
    assert entry["concepts"] is None
    assert entry["rewrite"] is not None and entry["paragraphs"] == 6
    note = entry_notes(payload, "alpha")
    assert "concepts.json" in note and ("PermissionError" in note or "errno 13" in note), note


@WINDOWS_ONLY
def test_sources_fidelity_locked_does_not_turn_into_a_silent_pass(
    dirs: SimpleNamespace, sample_md: Path
) -> None:
    """``changes.json`` 被独占 ⇒ 保真状态按未知记（``fidelity: null``），**不许**当成「校验通过」。"""
    target = make_material(dirs, sample_md, "alpha", changes=FIDELITY_OK)
    close = hold_open_exclusively(target / "changes.json")
    try:
        payload = payload_of(sources_response(dirs))
    finally:
        close()

    entry = entry_of(payload, "alpha")
    assert entry["fidelity"] is None, "读不出来 ≠ 校验通过"
    assert "changes.json" in entry_notes(payload, "alpha")


# -------------------------------- ③ 正文含孤立代理项：400 + 可读文案，一个字节都不写 --
@pytest.mark.parametrize("filename", ["s.md", "档案.md"])
def test_ingest_rejects_lone_surrogate_content_as_400(dirs: SimpleNamespace, filename: str) -> None:
    """``content`` 里有编不成 UTF-8 的孤立代理项 ⇒ **400** + 可读文案（不是 500），且没写任何东西。

    ``档案.md`` 那一档顺带盖住「派生 id 时先算内容哈希」的那次编码（slug 为空才会走到）。
    """
    body = json.dumps({"filename": filename, "content": "\ud800"}, ensure_ascii=True)
    client = make_client(dirs, raise_server_exceptions=False)

    response = client.post(
        "/api/ingest", content=body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 400, f"输入错该是 400，不是 500：{response.status_code} {response.text[:300]}"
    assert response.headers["content-type"].startswith("application/json"), "不许回 HTML 错误页"
    error = response.json()["error"]
    assert "content" in error and "UTF-8" in error, f"要说清是哪个字段、哪种错：{error}"
    assert "U+D800" in error, f"要点名那个字符：{error}"
    assert staging_leftovers(dirs.data) == [], "失败路径不许留暂存目录"
    assert [path.name for path in dirs.data.iterdir()] == [], "一个字节都不许写（更别说建材料目录）"


def test_ingest_still_accepts_normal_content_after_the_surrogate_gate(
    dirs: SimpleNamespace,
) -> None:
    """同一条端点照旧收正常正文（别为了拦代理项把正常路径拦掉）。"""
    response = make_client(dirs).post(
        "/api/ingest", json={"filename": "a.md", "content": "第一段。\n\n第二段。", "source_id": "a"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["paragraphs"] == 2


# ------------------------------- ④ 悬空 junction / stat 失败的条目：点名说明，别静默 --
@WINDOWS_ONLY
def test_sources_dangling_junction_entry_is_reported_not_silently_skipped(
    dirs: SimpleNamespace,
) -> None:
    """``data/dangling`` 是指向已删除目标的 junction ⇒ 清单里给一条点名说明（不是一声不吭）。"""
    real = dirs.tmp / "gone-target"
    real.mkdir()
    (real / "keep.txt").write_text("x", encoding="utf-8")
    link = dirs.data / "dangling"
    make_junction(link, real)
    shutil.rmtree(real)  # 目标没了 ⇒ 悬空 junction
    assert not link.is_dir() and os.path.lexists(link), "现场没造出来：它必须是「列得出来但进不去」"

    payload = payload_of(sources_response(dirs))

    assert payload["sources"] == []
    note = notes_of(payload)
    assert "dangling" in note, f"降级必须点名这个条目：{note}"
    assert ("stat" in note or "找不到" in note or "FileNotFoundError" in note), f"要说清原因：{note}"


@WINDOWS_ONLY
def test_sources_dangling_rewritten_md_is_reported_not_silently_skipped(
    dirs: SimpleNamespace, sample_md: Path
) -> None:
    """``rewritten.md`` 是个悬空 junction（stat 不到）⇒ 说明里点名材料与文件，且 mtime 退回别的产物。

    ``rewritten.md`` 在响应里**没有对应的块**（不像 concepts / rewrite / fidelity 那样自带一条
    「还没有 …」），所以这一支必须自己说，否则 mtime 悄悄退到别的产物、``degraded`` 里一个字都没有。
    """
    target = make_material(dirs, sample_md, "alpha", rewritten=False)
    real = dirs.tmp / "gone-artifact"
    real.mkdir()
    make_junction(target / "rewritten.md", real)
    shutil.rmtree(real)
    assert not (target / "rewritten.md").exists()

    payload = payload_of(sources_response(dirs))

    entry = entry_of(payload, "alpha")
    assert entry["mtime"] is not None, "退回 source.meta.json 的 mtime（别的产物一份都没有）"
    note = entry_notes(payload, "alpha")
    assert "rewritten.md" in note, f"降级必须点名文件：{note}"


def test_sources_artifact_stat_failure_names_material_file_and_reason(
    dirs: SimpleNamespace, sample_md: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``rewritten.md`` 的 ``stat`` 抛权限错 ⇒ 说明里点名材料、文件与原因（真机上「枚举不到」那一档）。"""
    target = make_material(dirs, sample_md, "alpha", changes=FIDELITY_OK)
    os.utime(target / "changes.json", (1_700_000_600.0, 1_700_000_600.0))
    shadow_stat_on(monkeypatch, "rewritten.md", PermissionError(13, "Permission denied", str(target / "rewritten.md")))

    payload = payload_of(sources_response(dirs))

    entry = entry_of(payload, "alpha")
    assert entry["mtime"] is not None, "坏的那一份不计入，剩下那份照用"
    note = entry_notes(payload, "alpha")
    assert "rewritten.md" in note and ("PermissionError" in note or "errno 13" in note), note


def test_sources_plain_files_in_data_are_still_skipped_silently(
    dirs: SimpleNamespace, sample_md: Path
) -> None:
    """既有口径一个字不改：``data/`` 下的**普通散文件**静默跳过，不为它加说明。"""
    make_material(dirs, sample_md, "alpha")
    (dirs.data / "README.txt").write_text("我不是材料\n", encoding="utf-8")

    payload = payload_of(sources_response(dirs))

    assert "README.txt" not in notes_of(payload)
    assert [row["source_id"] for row in payload["sources"]] == ["alpha"]


# ------------------------ ⑤ os.replace 撞上「目标被占着」：有界重试 + 能区分的文案 --
def test_atomic_write_retries_a_short_busy_window_with_bounded_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """前两次 ``os.replace`` 撞上占用 ⇒ 短退避重试，第三次成功；总等待 ≤ 200 ms。"""
    target = tmp_path / "out.json"
    target.write_text("old", encoding="utf-8")
    calls: list[str] = []
    real_replace = os.replace

    def flaky(src: Any, dst: Any) -> None:
        calls.append(str(dst))
        if len(calls) <= 2:
            raise sharing_conflict(Path(dst))
        real_replace(src, dst)

    sleeps: list[float] = []
    monkeypatch.setattr(jsonio.os, "replace", flaky)
    monkeypatch.setattr(jsonio.time, "sleep", sleeps.append)

    write_text(target, "new")

    assert len(calls) == 3, f"撞两次占用就该试第三次：{calls}"
    assert sleeps == [0.05, 0.10], f"退避要有界且写死：{sleeps}"
    assert sum(sleeps) <= 0.2, "总等待不许超过 200 ms"
    assert target.read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".tmp-*")), "成功后不留临时文件"


@WINDOWS_ONLY
def test_atomic_write_on_a_busy_target_says_busy_not_unwritable_directory(tmp_path: Path) -> None:
    """真机现场：目标文件被别的进程独占 ⇒ 抛 ``WriteError``，文案说的是**文件被占用**（不是目录不可写）。"""
    target = tmp_path / "source.meta.json"
    target.write_text('{"ok": 1}', encoding="utf-8")
    close = hold_open_exclusively(target)
    try:
        with pytest.raises(WriteError) as caught:
            write_text(target, '{"ok": 2}')
    finally:
        close()

    message = str(caught.value)
    assert str(target) in message, f"要点名文件：{message}"
    assert "占用" in message, f"要说清最像是被别的进程占着：{message}"
    assert "已重试" in message, f"重试过就要说：{message}"
    assert "目录" not in message.split("占用")[0], f"别把占用说成目录的问题：{message}"
    assert target.read_text(encoding="utf-8") == '{"ok": 1}', "失败时原文件一个字节都不许变"
    assert not list(tmp_path.glob(".tmp-*")), "失败后临时文件必须清掉"


def test_atomic_write_does_not_retry_other_replace_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非「共享冲突 / 拒绝访问」的 ``os.replace`` 失败**一次都不重试**，文案照旧（POSIX 口径不变）。"""
    sleeps: list[float] = []

    def broken(src: Any, dst: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(jsonio.os, "replace", broken)
    monkeypatch.setattr(jsonio.time, "sleep", sleeps.append)

    with pytest.raises(WriteError) as caught:
        write_text(tmp_path / "out.txt", "x")

    assert sleeps == [], "别的错误重试多少次都一样，别空转"
    message = str(caught.value)
    assert "写不进去" in message and "errno 28" in message, message
    assert not list(tmp_path.glob(".tmp-*")), "失败后临时文件必须清掉"
