"""`ar.jsonio` 的原子写（文本 / 字节）与**坏路径快速失败**（2026-10-14 真缺陷修复）。

现场（真机验证时暴露）：原子写用 `tempfile.mkstemp(dir=<产物目录>)` 建临时文件，而
`tempfile` 内部把 `PermissionError` 当成「候选名被占用」一起吞掉、接着重试 `TMP_MAX` 次，
最后抛一个语焉不详的 `FileExistsError`（`No usable temporary file name found`）。
**`TMP_MAX` 不是「一万次」**：2026-10-14 在本机实测（Windows / CPython 3.12.7）为
`2147483647`（21 亿级，近似无界），在一个真不可写的目录上实测 1 秒里已经试了 5000 多个候选名
仍在循环（见 `test_tmp_max_is_nearly_unbounded_on_this_platform_and_our_bound_is_32` 的说明）。
Windows 上 `os.access(dir, os.W_OK)` 又是假阳性（目录 ACL 与只读属性它看不准，实测对
`icacls /deny` 过的目录照样返回 `True`），所以坏目录要真写到那一步才暴露——表现是
「**不报错、单核 100%、十几分钟不结束**」：实测一次真机调用因此卡死 13 分钟、进程被杀，
响应与用量一起丢。

修法（两种失败必须分得开，见 `ar.jsonio._open_temp_in`）：

  - **目录不可写 / 路径不合法**（`PermissionError` 等 `OSError`）⇒ **立刻**抛 `WriteError`，
    消息里点名**哪个目录**、**原始 errno**；
  - **候选名已被占用**（`FileExistsError`）⇒ 照旧换名字重试（上限 `_TMP_NAME_ATTEMPTS = 32`），
    这条「同名文件已存在」的语义一个字没丢。

`write_text`（文本）与 `write_bytes`（字节，`ar.ingest` 的 `source.md` 用）共用
`ar.jsonio._atomic_write` 这一段内核，坏路径口径逐字相同——两条路都在这份用例里钉住。

**Windows 上没法可靠造出「真的写不进」的目录**（新建目录默认当前用户可写，改 ACL 要动
`icacls` 且受管理员权限影响；把目录设成只读对目录本身也拦不住在它里面建文件），
所以那两条用 **monkeypatch 精确模拟 `os.open` 的返回值**：只对目标目录里的候选路径抛
`PermissionError(EACCES)`，其余路径一律转交真的 `os.open`——模拟的是「系统在写的那一刻拒绝」，
与真机现场是同一个失败点（不是绕过被测代码）。另有一条**不打桩**的真实路径用例
（目标目录本身是个文件 ⇒ 系统直接给 `ENOTDIR` / `EEXIST` 一类错）。
"""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path

import pytest

from ar import jsonio
from ar.errors import EXIT_USAGE, WriteError


def temp_names(directory: Path) -> list[str]:
    """目录里的临时文件（正常路径跑完必须是空的——原子写不许留残骸）。"""
    return sorted(p.name for p in directory.iterdir() if p.name.startswith(".tmp-"))


# --------------------------------------------------------------- 正常路径 --
def test_write_text_is_atomic_and_leaves_no_temp_file(tmp_path: Path) -> None:
    """正常路径照旧：内容逐字落盘、同名旧文件被**替换**、不留临时文件。"""
    target = tmp_path / "changes.json"
    target.write_text("旧内容", encoding="utf-8")

    assert jsonio.write_text(target, "新内容") == target

    assert target.read_text(encoding="utf-8") == "新内容"
    assert temp_names(tmp_path) == [], "原子写的临时文件必须清干净"


def test_write_json_round_trips_and_uses_lf(tmp_path: Path) -> None:
    """`write_json` 的产物口径不变：UTF-8、`ensure_ascii=False`、缩进 2、**行尾是 LF**。"""
    target = tmp_path / "nested" / "changes.json"

    jsonio.write_json(target, {"名字": "值"})

    raw = target.read_bytes()
    assert b"\r\n" not in raw, "产物行尾必须统一成 LF（写盘用 newline=\"\\n\"）"
    assert raw.endswith(b"\n")
    assert jsonio.read_json(target) == {"名字": "值"}
    assert temp_names(target.parent) == []


# ------------------------------------------------ 正常路径：字节写 --
def test_write_bytes_is_atomic_and_keeps_the_bytes_exactly(tmp_path: Path) -> None:
    """`write_bytes`（`ar.ingest` 的 `source.md` 用它）：**逐字节**落盘、同名旧文件被替换、不留残骸。

    这条与文本路径的区别只有一个：不做任何编码 / 行尾转换——CRLF 与裸 `\\r` 必须原样保留
    （`source.md` 是「源文本原样」的契约，DESIGN §4.1）。
    """
    target = tmp_path / "source.md"
    target.write_bytes("旧的".encode("utf-8"))
    payload = "# 标题\r\n\r\n正文一。\r\n\n正文二。\n".encode("utf-8")

    assert jsonio.write_bytes(target, payload) == target

    assert target.read_bytes() == payload, "一个字节都不许变（含 CRLF / 裸 CR）"
    assert temp_names(tmp_path) == [], "原子写的临时文件必须清干净"
    assert not target.with_name(target.name + ".tmp").exists()


def test_write_bytes_creates_parent_directories_like_write_text(tmp_path: Path) -> None:
    """父目录不存在时自己建（与 `write_text` 同一条内核）：`data/<id>/source.md` 首次摄取靠它。"""
    target = tmp_path / "data" / "sample" / "source.md"

    jsonio.write_bytes(target, b"abc")

    assert target.read_bytes() == b"abc"
    assert temp_names(target.parent) == []


def test_write_bytes_unwritable_directory_fails_fast_with_one_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """字节写的坏路径口径与文本写**逐字相同**：一次就失败 + 点名目录与 errno + 不留残骸。

    这是 2026-10-14 复核揪出的第二个漏网入口（`ar.ingest` 的 `source.md` 此前用裸
    `tempfile.mkstemp`，在真不可写目录上实测 1 秒里空转 5000 多次仍在循环）。
    """
    locked = tmp_path / "locked"
    locked.mkdir()
    real_open = os.open
    attempts: list[str] = []

    def fake_open(path, flags, mode=0o777):
        if Path(path).parent == locked:
            attempts.append(str(path))
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_open(path, flags, mode)

    monkeypatch.setattr(jsonio.os, "open", fake_open)

    with pytest.raises(WriteError) as excinfo:
        jsonio.write_bytes(locked / "source.md", "正文".encode("utf-8"))

    message = str(excinfo.value)
    assert str(locked) in message, f"错误里必须点名是哪个目录：{message}"
    assert f"errno {errno.EACCES}" in message, f"错误里必须带原始 errno：{message}"
    assert len(attempts) == 1, f"目录不可写必须一次就失败，实测试了 {len(attempts)} 次"
    assert not (locked / "source.md").exists()
    assert temp_names(locked) == [], "失败时不许留下候选临时文件"


def test_write_bytes_replace_failure_cleans_the_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """字节写改名那一步失败：清掉临时文件 + 可读错误（与文本写同一条内核，同样钉一遍）。"""

    def fake_replace(src, dst):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(jsonio.os, "replace", fake_replace)

    target = tmp_path / "source.md"
    with pytest.raises(WriteError) as excinfo:
        jsonio.write_bytes(target, "写不进去".encode("utf-8"))

    assert f"errno {errno.EACCES}" in str(excinfo.value)
    assert str(tmp_path) in str(excinfo.value)
    assert not target.exists(), "没写成就绝不许留下半个目标文件"
    assert temp_names(tmp_path) == []


# ------------------------------------------------ 坏路径：不可写 ⇒ 立刻失败 --
def test_unwritable_directory_fails_fast_with_a_readable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目录不可写 ⇒ **一次就失败**（不是照着 `TMP_MAX` 空转下去），错误里点名目录与 errno。"""
    locked = tmp_path / "locked"
    locked.mkdir()
    real_open = os.open
    attempts: list[str] = []

    def fake_open(path, flags, mode=0o777):
        # 只拦目标目录里的候选临时文件：其余路径（pytest 自己的东西）照旧走真实现
        if Path(path).parent == locked:
            attempts.append(str(path))
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_open(path, flags, mode)

    monkeypatch.setattr(jsonio.os, "open", fake_open)

    with pytest.raises(WriteError) as excinfo:
        jsonio.write_text(locked / "changes.json", "{}")

    message = str(excinfo.value)
    assert str(locked) in message, f"错误里必须点名是哪个目录：{message}"
    assert f"errno {errno.EACCES}" in message, f"错误里必须带原始 errno：{message}"
    assert "Permission denied" in message, f"系统给的话要原样带上：{message}"
    assert len(attempts) == 1, f"目录不可写必须一次就失败，实测试了 {len(attempts)} 次"
    assert not (locked / "changes.json").exists()


def test_unwritable_directory_is_not_mistaken_for_a_name_clash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两种失败分得开：不可写给 `WriteError`，**不是**「名字用完了」那句 `FileExistsError`。"""
    locked = tmp_path / "locked"
    locked.mkdir()
    real_open = os.open

    def fake_open(path, flags, mode=0o777):
        if Path(path).parent == locked:
            raise PermissionError(errno.EACCES, "拒绝访问")
        return real_open(path, flags, mode)

    monkeypatch.setattr(jsonio.os, "open", fake_open)

    with pytest.raises(WriteError) as excinfo:
        jsonio.write_text(locked / "changes.json", "{}")

    assert not isinstance(excinfo.value, FileExistsError)
    assert "No usable temporary file name found" not in str(excinfo.value)
    assert excinfo.value.exit_code == EXIT_USAGE, "CLI 上它归「用法 / 输入错误」那一档（退出码 2）"


def test_parent_path_that_is_a_file_fails_fast(tmp_path: Path) -> None:
    """**不打桩**的真实坏路径：目标目录本身是个文件 ⇒ 立刻给可读错误（不空转）。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("我不是目录", encoding="utf-8")

    with pytest.raises(WriteError) as excinfo:
        jsonio.write_text(blocker / "changes.json", "{}")

    message = str(excinfo.value)
    assert str(blocker) in message and "errno" in message, message


def test_replace_failure_cleans_the_temp_file_and_names_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写入 / 改名那一步才撞权限（临时文件建出来了）也要：清理残骸 + 可读错误。"""

    def fake_replace(src, dst):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(jsonio.os, "replace", fake_replace)

    target = tmp_path / "changes.json"
    with pytest.raises(WriteError) as excinfo:
        jsonio.write_text(target, "写不进去的内容")

    assert f"errno {errno.EACCES}" in str(excinfo.value)
    assert str(tmp_path) in str(excinfo.value)
    assert not target.exists(), "没写成就绝不许留下半个目标文件"
    assert temp_names(tmp_path) == [], "临时文件要清掉"


# ------------------------------------- 原有的「同名文件已存在」重试语义保留 --
def test_taken_temp_names_are_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """候选名被占用 ⇒ 换一个名字重试（这才叫「同名已存在」），最终照常写成功。"""
    real_open = os.open
    calls: list[str] = []

    def fake_open(path, flags, mode=0o777):
        calls.append(Path(path).name)
        if len(calls) <= 2:
            raise FileExistsError(errno.EEXIST, "File exists")
        return real_open(path, flags, mode)

    monkeypatch.setattr(jsonio.os, "open", fake_open)
    target = tmp_path / "changes.json"

    assert jsonio.write_text(target, "内容") == target

    assert len(calls) == 3, f"撞两次就该第三次成功，实际试了 {len(calls)} 次"
    assert target.read_text(encoding="utf-8") == "内容"
    assert temp_names(tmp_path) == []


def test_exhausted_name_attempts_report_a_readable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每个候选名都被占用 ⇒ 按**有界**次数放弃（不是照着 `TMP_MAX` 空转），错误可读。"""
    calls: list[str] = []

    def fake_open(path, flags, mode=0o777):
        calls.append(str(path))
        raise FileExistsError(errno.EEXIST, "File exists")

    monkeypatch.setattr(jsonio.os, "open", fake_open)

    with pytest.raises(WriteError) as excinfo:
        jsonio.write_text(tmp_path / "changes.json", "{}")

    assert len(calls) == jsonio._TMP_NAME_ATTEMPTS, f"重试上限写死在这个常量上，实际 {len(calls)} 次"
    assert jsonio._TMP_NAME_ATTEMPTS < tempfile.TMP_MAX, "上限必须远小于 tempfile.TMP_MAX，否则又回到空转"
    assert str(tmp_path) in str(excinfo.value)


def test_tmp_max_is_nearly_unbounded_on_this_platform_and_our_bound_is_32(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """数字口径订正（2026-10-14 复核）：`tempfile.TMP_MAX` **不是**常说的「一万次」。

    本机实测（Windows / CPython 3.12.7）`tempfile.TMP_MAX = 2147483647`（21 亿级、**近似无界**），
    也就是说旧写法在坏目录上根本没有一个「一万次就放弃」的兜底：实测在真不可写目录上 1 秒里
    已经试了 5000 多个候选名、仍在循环。现实现的上限是写死的 `_TMP_NAME_ATTEMPTS = 32`，
    它是**本项目自己的**有界值，与 `TMP_MAX` 无关。

    这条用例把两件事一起钉住：① 上限常量就是 32（改它要连带改文档里的六处说明）；
    ② 它远小于 `TMP_MAX`——`TMP_MAX` 具体多大随平台变（POSIX 上常是 10000），
    所以只断言量级关系，不把 `2147483647` 这个 Windows 实测值写死成跨平台断言。
    """
    assert jsonio._TMP_NAME_ATTEMPTS == 32
    assert tempfile.TMP_MAX >= 10000, "TMP_MAX 至少是「一万」量级：别把它当成小数字"
    assert jsonio._TMP_NAME_ATTEMPTS < tempfile.TMP_MAX

    # 顺带钉住「正常路径只建一次临时文件」：坏路径上的次数才需要按上限谈
    calls: list[int] = []
    real_open = os.open

    def counting_open(path, flags, mode=0o777):
        calls.append(1)
        return real_open(path, flags, mode)

    monkeypatch.setattr(jsonio.os, "open", counting_open)
    jsonio.write_text(tmp_path / "changes.json", "内容")
    assert len(calls) == 1
