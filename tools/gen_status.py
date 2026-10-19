#!/usr/bin/env python
"""生成 `web/status.json`：阅读页顶栏「状态」面板的数据源（测试结论 + 本次修改）。

用法（工作目录＝项目根）：

    uv run python tools/gen_status.py                 # 两套都跑（前端那套要本机 Edge，约 65 秒）
    uv run python tools/gen_status.py --skip-web      # 只跑后端那套
    uv run python tools/gen_status.py --note "一句话"  # 可选：给这次改动写一句说明

口径（数字只由本脚本写，前端一个数字都不硬编码）：

- **后端**：`uv run pytest tests -q` 的等价执行——脚本自己就跑在 uv 的 `.venv` 里，
  所以直接调当前解释器（`sys.executable -m pytest tests -q`）跑同一套用例，
  **不在 uv 里再套一层 uv**（实测：嵌套 `uv run` 会在 uv 缓存目录上撞 `os error 5 拒绝访问`，
  那次摘到的「摘要行」是 uv 的报错而不是 pytest 的结论）。脚本不在 `.venv` 里时才退回 `uv run pytest`；
- **前端**：`python -m pytest tests/web -q`——**系统 Python，不是 uv 的 .venv**。
  ⚠ `uv run` 会把 `.venv\\Scripts` 放到 PATH 最前面，`shutil.which("python")` 因此会指回 .venv，
  所以 `find_system_python()` 逐条扫 PATH 并跳过 .venv（可用 `AR_STATUS_PYTHON` 显式指定）。
  那套用例要本机 Edge：夹具自己找内核，**本脚本绝不触发任何浏览器内核下载**（不跑 playwright install）；
- **子进程的临时目录**指到项目内的 `.status-tmp/`（跑完删掉）：浏览器夹具的临时产物不往系统临时目录里塞，
  也能在「系统临时目录不可写」的受限环境里照常跑（实测：那种环境下 playwright 的 `mkdtemp` 会 EPERM，
  整个前端套件变成 88 errors）；
- 测试结论取 pytest 的**最后一行摘要**（passed / failed / skipped + 套件自己报的耗时）；
- 改动清单：`git status --porcelain -uall`（状态 + 路径；`-uall` 把未跟踪目录展开成文件，
  这样才能逐个给未跟踪文件数行数）+ `git diff --numstat`（未提交改动的逐文件增删行数，
  与 `git diff --stat` 同源同数、只是机器可读；未跟踪文件按实际行数计入 insertions 并标 `untracked`）
  + `git log -1 --oneline` 当锚点。

产物 `web/status.json`：UTF-8 无 BOM、LF、缩进 2，**机器生成、不入库**（见 `.gitignore`）；
页面读不到 / 读坏了会显示「尚未生成」的降级文案，不会假装成功。

退出码：0 ＝ 报告写成功（测试红不红由产物里的数字表达，页面按数字染色）；写不出去才非 0。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "status.json"
TEMP_DIR = ROOT / ".status-tmp"                       # 子进程的临时目录（跑完删掉，见文件头）

BACKEND_DISPLAY = "uv run pytest tests -q"
WEB_DISPLAY = "python -m pytest tests/web -q"
TIMEOUT_SECONDS = 1800

# pytest 摘要行：`248 passed, 1 warning in 2.46s` / `84 passed, 1 skipped in 65.03s` / `no tests ran in 0.01s`
SUMMARY_RE = re.compile(r"in ([\d.]+)s\s*$")
SUMMARY_HINT_RE = re.compile(r"\b(passed|failed|error|errors|no tests ran)\b")


def _force_utf8_streams() -> None:
    """让 stdout / stderr 走 UTF-8（口径同 `src/ar/cli.py` 的 `_force_utf8_streams`）。

    Windows 控制台默认用本地代码页（GBK）：这一行要打印的「−」（U+2212，与页面汇总里同一个字符）
    在 GBK 里不存在，直接 `print` 会抛 `UnicodeEncodeError`——报告其实已经写好了，却因为打印而崩。
    `reconfigure` 只在流支持时生效，失败就退回 `errors="replace"`，不影响主流程。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):  # pragma: no cover - 交互式重定向的极端情况
            continue


def child_env() -> dict[str, str]:
    """子进程环境：把临时目录指到项目内的 `.status-tmp/`（跑完由 `cleanup_temp_dir()` 删掉）。

    **为什么要动 TEMP（不是怪癖，是本机实测的硬约束）**：本机 DSH 沙箱下，由 `uv run` 派生的
    子孙进程写不了系统临时目录——实测 `pathlib.Path(os.environ["TEMP"], "x.txt").write_text("ok")`
    直接 `PermissionError(13, 'Permission denied')`（同一个进程里 uv 初始化缓存也报
    `os error 5 拒绝访问`）。而 playwright 启动浏览器时要在临时目录里 `mkdtemp('playwright-artifacts-')`，
    那一步 EPERM 会让**整个前端套件的浏览器夹具全灭**（实测过一次：`10 passed, 88 errors`）。
    把子进程的 TEMP 指到项目内（可写）之后两套都照常跑通。
    `TMP` / `TMPDIR` 一并设上：Playwright / Node 在 Windows 上看 `TEMP`（`TMP` 兜底），
    Python 的 `tempfile` 认这几个名字。`.status-tmp/` 已进 `.gitignore`，且跑完即删（见 `main` 的 finally）。
    """
    TEMP_DIR.mkdir(exist_ok=True)
    return dict(os.environ, TEMP=str(TEMP_DIR), TMP=str(TEMP_DIR), TMPDIR=str(TEMP_DIR))


def cleanup_temp_dir() -> None:
    """幂等删除临时目录（可能已被删过 / 文件还被占着：尽力而为，不让它影响主流程）。"""
    shutil.rmtree(TEMP_DIR, ignore_errors=True)


def backend_command() -> tuple[list[str], str]:
    """后端套件的实际命令：脚本跑在 .venv 里就直调当前解释器，否则退回 `uv run pytest`。

    **为什么不直接嵌套 `uv run pytest`**：本机 DSH 沙箱下，`uv run` 派生出来的子进程再调一次
    `uv run` 时，uv 初始化自己的缓存目录会撞权限——实测
    `Failed to initialize cache at C:\\Users\\…\\uv\\cache` +
    `failed to open file …\\sdists-v9\\.git: 拒绝访问。(os error 5)`。
    那次「最后一行摘要」摘到的是 uv 的报错而不是 pytest 的结论，于是写出了一份 `0 passed` 的假数字
    （页面会把它当真的显示，比什么都没有更糟）。
    脚本自己就跑在 uv 的 `.venv` 里（`uv run python tools/gen_status.py`），用同一个解释器直调
    `-m pytest tests -q` 跑的是**同一套用例、同一个环境**；产物的 `command` 照旧写
    `uv run pytest tests -q`（人看的口径不变），实际命令写进 `via` 字段备查。

    返回 (实际命令, 说明)；说明会写进产物的 `via`，不写就是 `uv run pytest tests -q` 本身。
    """
    venv = (ROOT / ".venv").resolve()
    try:
        exe = Path(sys.executable).resolve()
    except OSError:  # pragma: no cover - 解释器路径异常
        exe = None
    if exe is not None and (exe == venv or venv in exe.parents):
        return [sys.executable, "-m", "pytest", "tests", "-q"], f"{sys.executable} -m pytest tests -q"
    return ["uv", "run", "pytest", "tests", "-q"], ""


def _count(text: str, word: str) -> int:
    match = re.search(rf"(\d+) {word}\b", text)
    return int(match.group(1)) if match else 0


def last_summary(output: str) -> str:
    """pytest 的**最后一行摘要**（带 passed/failed 且带 `in Ns` 的那一行）。"""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if SUMMARY_HINT_RE.search(line) and SUMMARY_RE.search(line):
            return line
    for line in reversed(lines):
        if SUMMARY_HINT_RE.search(line):
            return line
    return lines[-1] if lines else ""


def find_system_python() -> tuple[str | None, str]:
    """找系统 Python（**不是** uv 的 .venv）：PATH 里逐条找，跳过 .venv 与 Windows 商店别名。

    `uv run` 会把 `.venv\\Scripts` 插到 PATH 最前面，`shutil.which("python")` 因此不可信。
    """
    venv = (ROOT / ".venv").resolve()
    for entry in (os.environ.get("PATH") or "").split(os.pathsep):
        if not entry.strip():
            continue
        for name in ("python.exe", "python"):
            candidate = Path(entry) / name
            if not candidate.is_file():
                continue
            try:
                resolved = candidate.resolve()
            except OSError:  # 断链 / 权限：换下一个
                continue
            if resolved == venv or venv in resolved.parents:
                continue                                  # uv 的 .venv：前端套件不用它
            if "windowsapps" in str(resolved).lower():
                continue                                  # 商店占位符（点了只会弹安装提示）
            return str(resolved), ""
    return None, "PATH 里找不到系统 Python（uv 的 .venv 不算）；可以用 AR_STATUS_PYTHON 显式指定"


def run_suite(command: list[str], display: str, **extra) -> dict:
    """跑一套测试并把「最后一行摘要」拆成 passed / failed / skipped / 耗时。"""
    suite: dict = {"command": display, **extra}
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command, cwd=str(ROOT), capture_output=True, text=True, env=child_env(),
            encoding="utf-8", errors="replace", timeout=TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        suite.update({"ran": False, "ok": False, "reason": f"命令没找到：{exc}"})
        return suite
    except subprocess.TimeoutExpired:
        suite.update({"ran": False, "ok": False, "reason": f"超时（>{TIMEOUT_SECONDS} 秒）"})
        return suite
    elapsed = time.monotonic() - started

    output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    summary = last_summary(output)
    seconds_match = SUMMARY_RE.search(summary)
    suite.update({
        "ran": True,
        "exit_code": proc.returncode,
        "passed": _count(summary, "passed"),
        "failed": _count(summary, "failed"),
        "skipped": _count(summary, "skipped"),
        "errors": _count(summary, "error") + _count(summary, "errors"),
        "seconds": round(float(seconds_match.group(1)) if seconds_match else elapsed, 1),
        "summary": summary or "（没拿到 pytest 的摘要行）",
    })
    suite["ok"] = proc.returncode == 0 and not suite["failed"] and not suite["errors"]
    return suite


def git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{proc.stderr.strip() or proc.returncode}")
    return proc.stdout


def status_entries() -> list[tuple[str, str]]:
    """`git status --porcelain -uall` → [(状态码, 路径)]；改名只留新路径。"""
    entries: list[tuple[str, str]] = []
    for line in git("status", "--porcelain", "-uall").splitlines():
        if not line.strip():
            continue
        code = line[:2].strip() or "?"
        path = line[3:].strip().strip('"')
        if " -> " in path:                                # 改名：`R  old -> new`
            path = path.split(" -> ", 1)[1].strip().strip('"')
        entries.append((code, path))
    return entries


def diff_numstat() -> dict[str, tuple[int, int]]:
    """`git diff --numstat` → {路径: (增, 删)}（二进制文件是 `-`，按 0 算）。"""
    stats: dict[str, tuple[int, int]] = {}
    for line in git("diff", "--numstat").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0], parts[1], parts[2].strip().strip('"')
        stats[path] = (int(added) if added.isdigit() else 0, int(deleted) if deleted.isdigit() else 0)
    return stats


def count_lines(path: Path) -> tuple[int, bool]:
    """未跟踪文件的行数：返回 (行数, 是不是二进制)。最后一行没换行也算一行。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return 0, False
    if b"\0" in raw[:8192]:
        return 0, True
    text = raw.decode("utf-8", errors="replace")
    if not text:
        return 0, False
    return text.count("\n") + (0 if text.endswith("\n") else 1), False


def collect_changes(note: str) -> dict:
    """未提交的改动清单：状态 + 路径 + 增删行数（未跟踪文件单独标出来并数行数）。"""
    try:
        head = git("log", "-1", "--oneline").strip()
        stats = diff_numstat()
        files: list[dict] = []
        for code, path in status_entries():
            if code == "??":
                lines, binary = count_lines(ROOT / path)
                row = {"status": code, "path": path, "insertions": lines, "deletions": 0,
                       "untracked": True}
                if binary:
                    row["binary"] = True
            else:
                added, deleted = stats.get(path, (0, 0))
                row = {"status": code, "path": path, "insertions": added, "deletions": deleted}
            files.append(row)
    except RuntimeError as exc:                            # 不是 git 仓库 / git 不可用：可见降级
        return {"head": "", "error": str(exc), "files": [],
                "totals": {"files": 0, "insertions": 0, "deletions": 0}}

    changes = {
        "head": head,
        "files": files,
        "totals": {
            "files": len(files),
            "insertions": sum(row["insertions"] for row in files),
            "deletions": sum(row["deletions"] for row in files),
        },
    }
    if note:
        changes["note"] = note
    return changes


def build_report(*, skip_web: bool, note: str) -> dict:
    backend_cmd, backend_via = backend_command()
    backend_extra = {"via": backend_via} if backend_via else {}
    tests: dict = {"backend": run_suite(backend_cmd, BACKEND_DISPLAY, **backend_extra)}
    if skip_web:
        tests["web"] = {"command": WEB_DISPLAY, "ran": False, "reason": "--skip-web"}
    else:
        python_exe, why = find_system_python()
        if python_exe is None:
            tests["web"] = {"command": WEB_DISPLAY, "ran": False, "reason": why}
        else:
            tests["web"] = run_suite(
                [python_exe, "-m", "pytest", "tests/web", "-q"], WEB_DISPLAY, interpreter=python_exe,
            )
    cleanup_temp_dir()                                    # 先删临时目录，再照取改动清单（别把它的产物算进去）
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tests": tests,
        "changes": collect_changes(note),
    }


def write_report(report: dict) -> None:
    """UTF-8 无 BOM、LF、缩进 2（页面与人工看都稳定）。"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    OUT.write_text(text, encoding="utf-8", newline="\n")


def describe(suite: dict) -> str:
    if not suite.get("ran"):
        return f"本次没跑（{suite.get('reason') or '原因未知'}）"
    verdict = "全绿" if suite.get("ok") else "有失败"
    return (f"{suite['passed']} passed / {suite['failed']} failed / {suite['skipped']} skipped"
            f"（{suite['seconds']}s，{verdict}）")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_streams()
    parser = argparse.ArgumentParser(
        description="生成 web/status.json（阅读页顶栏「状态」面板的数据源）",
    )
    parser.add_argument("--skip-web", action="store_true",
                        help="不跑前端套件（tests/web 要本机 Edge，约 65 秒）")
    parser.add_argument("--note", default="", help="写进产物的一句话说明（可选）")
    args = parser.parse_args(argv)

    # 跑两套 + 写产物整段套 try/finally：不管中间出什么岔子，临时目录都不留在工作区里
    try:
        report = build_report(skip_web=args.skip_web, note=args.note.strip())
        try:
            write_report(report)
        except OSError as exc:
            print(f"✗ 写不出 {OUT}：{exc}", file=sys.stderr)
            return 1
    finally:
        cleanup_temp_dir()

    changes = report["changes"]
    if changes.get("error"):
        print(f"  ⚠ 改动清单拿不到：{changes['error']}")
    print(f"  后端 {BACKEND_DISPLAY} → {describe(report['tests']['backend'])}")
    print(f"  前端 {WEB_DISPLAY} → {describe(report['tests']['web'])}")
    print("  改动 {} 个文件 / +{} −{}（head: {}）".format(
        changes["totals"]["files"], changes["totals"]["insertions"], changes["totals"]["deletions"],
        changes.get("head") or "—",
    ))
    print(f"  写出 {OUT.relative_to(ROOT)}（{report['generated_at']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
