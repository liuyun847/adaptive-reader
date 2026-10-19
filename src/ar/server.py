"""HTTP 层：``/api/*`` JSON 接口 + 托管 ``web/`` 静态目录（契约 §4）。

接口逐字按下表实现（前端只依赖这些）：

===========================  ==================================================
``GET  /``                    托管 ``web/index.html``（启动页）；带 ``?source=`` / ``?fixture=``
                              任一存在时 **302** → ``/reader?<原样 query>``
``GET  /reader``              托管 ``web/reader.html``（阅读器页）。该文件还不存在时回
                              **带可读文案的 404**，绝不回落到启动页
``GET  /api/sources``         材料清单 ``{ sources[], degraded[] }``（**只读，一个字节都不写盘**：
                              扫 ``data/`` 下每个目录，缺哪份产物就哪一块 ``null`` + 一条可读降级，
                              坏 JSON / 读不出来（被独占、权限不足）/ 坏时间戳 /
                              悬空 junction 条目都只降级、不 500）
``POST /api/ingest``          ``{ filename, content, source_id?, overwrite? }``
                              → ``{ source_id, paragraphs, ingested_at, anchors_stable,
                              overwritten, degraded[] }``（**只摄取**：走 ``ar.ingest`` 的同一条路径
                              写 ``source.md`` + ``source.meta.json``；不调模型，也不跑
                              analyze / rewrite / check）
``GET  /api/doc/<source_id>`` ``{ meta, rewritten, changes, concepts, degraded }``
``GET  /api/profile``         ``{ profile, degraded }``（默认读者 local-default）
``POST /api/profile/rating``  ``{ concept_id, state, note? }`` → 更新画像并写回 YAML
``POST /api/profile/preference`` ``{ id, value, note? }`` → 写入一条偏好并写回 YAML
                              （§14.1：事实锚口径 ``pref_fact_anchor`` 的唯一创建路径）
``POST /api/explain``         ``{ source_id, paragraph, kind, selection?, selection_from? }``
                              → ``{ text, degraded, scope?, cached?, selection_truncated?, usage?, attempts[] }``
``POST /api/events``          ``{ events:[…] }``（也容忍单个事件对象）
                              → ``{ ok, written, duplicates, total, degraded[] }``
                              （**只 append 到 ``profiles/<reader>/events.jsonl``，绝不写 ``profile.yaml``**）
``POST /api/compress``        ``{ source_id, paragraph, reason? }``（读者点「太啰嗦」）
                              → 成功 ``{ ok:true, paragraph, action:"compress", text, scope, cached,
                              usage, attempts, model, prompt_version, degraded, artifacts_updated,
                              hint, fact_declarations, waived[], over_declared[], fact_anchor_policy }``
                              （``degraded`` 平时是 ``[]``，会非空只有三种：这一段没有事实锚、
                              申报读不通、「行为事件没记上」；都不影响压缩结果）；
                              降级一律 **200** + ``{ ok:false, reason, text, degraded[] }``
                              （``reason`` 枚举见 ``ar.rewrite.COMPRESS_REASONS``）
``GET  /api/survey``          ``{ version, answered_version, answered_at, questions[], answers, skipped, degraded[] }``
                              （``answered_at`` 是追加字段：画像 ``background.answered_at``，
                              缺就 ``null``，不拿 ``survey.json`` 的 ``updated_at`` 冒充）
``POST /api/survey``          ``{ answers?, skipped?, followups? }`` → ``{ ok, background, sketch, degraded[] }``
                              （``followups`` 并进 ``background.followups[]``；``skipped`` 只记标志、不清旧答案）
``POST /api/survey/followup`` ``{ followups? }`` → ``{ questions[{q,hint}], usage?, model?, prompt_version?, degraded }``
``GET  /api/health``          ``{ ok, model_configured, ... }``
===========================  ==================================================

约定：

- 每个响应都带 ``degraded``（或 ``degraded`` 字段）：缺失的产物、未配置的模型都会在这里说明，
  **不允许静默空白**。
- ``web/`` 目录可能还不存在（前端由另一路并行开发）：静态托管容忍目录缺失，
  只回一个写明原因的 404，不崩、不影响 ``/api/*``。
- 启动页与阅读器页是**两个不同的页面**（``GET /`` → ``web/index.html``、
  ``GET /reader`` → ``web/reader.html``）：``reader.html`` 还没就位时 ``/reader`` 回带原因的 404，
  **绝不回落到启动页** —— 回落只会让「阅读器还没做好」看起来像「页面一切正常」。
  带页面参数（``?source=`` / ``?fixture=`` 任一存在）的 ``/`` **302** 到 ``/reader``，
  query **原样保留**（顺序、编码都不动）：老链接 ``/?source=<id>`` 因此照旧能用。
- ``/api/sources`` 是**只读清单**：它只 ``stat`` 与读产物，一个字节都不写盘。
  每种缺失都走同一条口径——**缺哪份产物，响应里那一块就是 ``null``**（不是缺键、更不是编造的
  ``0`` / 空串）+ 顶层 ``degraded`` 一条可读说明；产物 JSON 坏了、文件在但读不出来
  （被别的进程独占 / 权限不足）也照此办（绝不 500）。「键不存在」与「值为 0」分得开：
  老产物缺 ``paragraph_count`` 时写 ``null``，``meta.degraded`` 条数同理。
  **坏时间戳只 null 那一条**：产物 mtime 超出 ``datetime.fromtimestamp`` 的可表示范围
  （Windows 上限约公元 3000 年）时 ``mtime`` 写 ``null`` + 一条点名材料 / 文件 / 原因的说明，
  一份坏时间戳不许把整份清单打成 500。``data/`` 下的条目：``.`` 开头的目录、没有
  ``source.meta.json`` 的目录、**悬空 junction / stat 失败的条目**各记一条说明，
  **普通散文件静默跳过**（材料是目录，这是既有口径）。
- ``POST /api/ingest`` **只摄取**：写 ``data/<source_id>/{source.md,source.meta.json}``，
  段锚点由 ``ar.ingest`` 生成（**不自己拼 ``source.meta.json``**，与 ``ar ingest`` 逐字一致），
  **不调模型、不跑 analyze / rewrite / check**。扩展名只收 ``.md`` / ``.txt``（大小写不敏感）、
  正文上限 :data:`MAX_UPLOAD_CHARS` 字符；目标材料已存在且没带 ``overwrite: true`` → **409**
  （说清「已存在」与「怎么覆盖」），入参不合法 → **400**（含**正文里有编不成 UTF-8 的字符**，
  例如 JSON 里的孤立代理项 ``"\ud800"``：那是客户端输入错，一个字节都不写），
  磁盘写不进去 → 500 兜底 + 可读消息。
  摄取模块要的是「一个磁盘上的源文件路径」，所以正文先写进 ``data/.upload-<随机>/`` 这个
  **暂存目录**（不落系统临时目录：本机 ``%TEMP%`` 不可写，见 :data:`UPLOAD_STAGING_PREFIX`），
  再调它，收尾**必删**（``finally``：异常路径也不留残骸；真删不掉则进响应的 ``degraded``，
  但**不改写**结果）。副作用照实记一笔：那时 ``source.meta.json`` 的 ``ingest.path``
  指的是这个已被删掉的暂存路径（摄取模块记的是传进去的真实路径）。
- ``/api/explain`` 在模型未配置或调用失败时返回 200 + ``degraded: true`` + 说明文本，
  让前端能直接把这段说明渲染进卡片。
- ``/api/explain`` 的追加字段分两类：**请求侧**的 ``scope``（``paragraph`` / ``selection``）、
  ``selection_truncated``（选中片段超 600 字符被截断）与 **``attempts``**（这次调用的逐次尝试账，
  结构 = 产物里的 ``meta.attempts``；没发请求时是空数组 ``[]``——未配置 / 缓存命中 / 材料或段落不存在）
  每次响应都给；**产出侧**的 ``cached``（进程内缓存命中，见 ``ar.cache``）与 ``usage``
  （本次模型调用的原始 token 账，含前缀缓存命中 ``prompt_tokens_details.cached_tokens``；
  没调模型 / 缓存命中时是 ``{}``）只在成功响应里出现。
  旧键（``text`` / ``degraded`` / ``model`` / ``prompt_version`` / 失败时的 ``reason``）逐字不变、
  枚举不变。``attempts`` 是讲解的**唯一**记账出口（讲解不落盘），所以成功与降级都给。
- 读前背景问卷的三条端点（``ar.survey``，PLAN-background-survey §8）：题型 / 选项 / 条件题规则
  一律由后端给（``questions[]`` 带 ``requires``），**前端不硬编码**；题目或选项一变就升
  ``version``，老答案照样可读。``POST /api/survey`` 校验失败回 400（``InputError``）并点名题号；
  ``POST /api/survey/followup`` 失败仍回 200 + ``degraded`` 说明（照 ``/api/explain`` 的惯例），
  **固定题答案不受影响**。旧键一个不改。
- ``POST /api/events``（PLAN-actions-feedback §7.1/§7.2）**只 append**：写
  ``profiles/<reader>/events.jsonl``，**绝不写 ``profile.yaml``** —— 写一次画像就换指纹
  （``cache.py`` 的 ``(路径, 字节数, mtime_ns)``）⇒ 讲解缓存全失效 + 前端会话键变化。
  ``reader`` 由服务端盖章（请求里给了也不认）；``source_id`` 非法 → 400；
  段不在 ``source.meta.json`` 里 → 丢弃 + ``degraded``（不是 400：前端手里可能拿着旧产物）；
  材料未摄取 / 写盘失败 → 200 + ``ok:false`` + ``written:0`` + ``degraded``（不许假装成功）。
  "修复" 由 ``ar feedback`` 显式动作做（默认 dry-run，``--apply`` 才写盘）。
- ``POST /api/compress``（PLAN-actions-feedback §5.2）：「太啰嗦」当场压缩该段并**写回三份产物**。
  读者 id 取应用级配置（本批不加按请求传读者）。**降级一律 200**（照 ``/api/explain`` 的惯例），
  ``reason`` 取 ``ar.rewrite.COMPRESS_REASONS`` 的枚举。
  **事实锚走与 ``ar check`` 同一套口径**（2026-10-14，§14.1）：``compress-v3`` 提示词让模型在
  ``fact_declarations`` 里逐条申报本次压缩放弃了哪几项锚点，写盘前用同一个
  ``compare_blocks_detailed`` 按**读者画像当前的口径** ``pref_fact_anchor`` 判 ——
  少了且已申报 ⇒ 松口径放行（申报落 ``changes[i].fact_declarations``、响应带
  ``fact_declarations`` / ``waived[]`` / ``fact_anchor_policy``，重跑 ``ar check`` 后同一批条目进
  ``fidelity.waived``）；**没申报的缺失、多出来的锚点、顺序调换、申报与实测对不上 ⇒ 一律拒写**
  （正文一个字节不动，降级文案点名对不上的锚点）。缓存命中时 ``cached:true`` / ``usage:{}`` /
  ``artifacts_updated:false``（这次没写盘）、不发请求，但申报与放行条目跟着缓存一起回（页面照样显示）。
  该次点击顺带写一条 ``kind="compress"``
  的行为事件 —— **事件写失败不影响压缩结果**（压缩照常写盘、``ok`` 照常 ``true``），
  但**不再静默**：成功响应的 ``degraded`` 会带一条可读说明，前端在成功路径上打 warn 横幅
  （2026-10-13 收口：行为事件是尽力而为的采集，不是「失败也不许说」）。
"""

from __future__ import annotations

import hashlib
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .cache import ExplainCache
from .config import Config
from .errors import InputError, WriteError
from .events import append_events, dedup, read_events, validate_event
from .explain import aexplain
from .ingest import ingest
from .jsonio import now_iso, read_json_if_exists, read_text_if_exists
from .model import ModelClient
from .paths import (
    DEFAULT_DATA_DIR,
    DEFAULT_PROFILES_DIR,
    DEFAULT_READER,
    DEFAULT_SAMPLES_DIR,
    DEFAULT_WEB_DIR,
    check_source_id,
    doc_dir,
)
from .profile import apply_rating, load_profile, save_profile, set_preference
from .prompts import DEFAULT_SELECTION_FROM, SELECTION_SOURCES
from .rewrite import (
    COMPRESS_HINT,
    COMPRESS_SCOPE,
    DEFAULT_COMPRESS_REASON,
    acompress_paragraph,
)
from .survey import (
    QUESTIONS,
    SURVEY_VERSION,
    answered_version,
    answers_of,
    append_followups,
    asurvey_followups,
    background_of,
    finalize_background,
    has_fixed_answers,
    has_followups,
    merge_followups_into,
    normalize_background,
    render_sketch,
    save_background,
    validate_answers,
    write_survey_record,
)

__all__ = ["build_app", "MAX_UPLOAD_CHARS", "UPLOAD_EXTENSIONS"]

#: ``POST /api/ingest`` 收的扩展名白名单（比对前先转小写，所以大小写不敏感）
UPLOAD_EXTENSIONS = (".md", ".txt")

#: ``POST /api/ingest`` 的正文上限：**字符数**（不是字节数）。超出**拒收**，不静默截断——
#: 截断会悄悄改掉材料内容，而摄取是「原文原样落盘」的入口（DESIGN §4.1）。
MAX_UPLOAD_CHARS = 2_000_000

#: source_id slug 化：不是 ``[a-z0-9]`` 的连续片段压成一个 ``-``（见 :func:`_upload_source_id`）
_SLUG_RE = re.compile(r"[^a-z0-9]+")

#: 暂存文件名清洗：只留这些字符，其余换 ``_``（Windows 上 ``:`` ``*`` ``?`` 这类字符
#: 会让写盘直接抛 ``OSError``，而它来自**上传的文件名**，不该把一次摄取变成 500）
_UPLOAD_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


#: 上传正文的暂存目录名前缀：``data/.upload-<随机>/<清洗过的原文件名>``。
#:
#: **为什么不用 ``tempfile``**：本机 ``%TEMP%`` 不可写（pyproject.toml 里那条 pytest basetemp
#: 注释记的就是这件事——pytest 的 basetemp 因此落到项目根）。那种环境下 ``tempfile.gettempdir()``
#: 会一路退回**当前工作目录**，于是 ``ar serve`` 从哪个目录起，暂存文件就落在哪儿；
#: 从不可写的目录起则每次上传都失败。放进 ``data/`` 里，与摄取真正要写的是同一棵树：
#: **能摄取就一定能暂存**，失败模式少一个，也不会往用户的随便哪个目录里丢东西。
UPLOAD_STAGING_PREFIX = ".upload-"


#: 「最后动过」的判据：材料目录里这三份产物，取**存在者中最新**的一份（三份都没有才退回
#: ``source.meta.json`` 的 mtime）。顺序与响应里的内容块无关，只是固定下来保证结果稳定。
_MTIME_ARTIFACTS = ("rewritten.md", "changes.json", "decisions.json")

#: 上表里**自带响应块**的两份产物：它们缺失时 ``/api/sources`` 会给出「还没有 …」的说明，
#: 所以 mtime 那一步不必重复第二句。``rewritten.md`` 不在其中——它在响应里没有对应块，
#: 缺失或 stat 不到必须由 mtime 那一步自己说（否则就是静默）。
_ARTIFACTS_WITH_OWN_BLOCK = ("changes.json", "decisions.json")


def _os_error_text(exc: OSError) -> str:
    """``OSError`` → 一句人读的原因（点名异常类型 / errno / 原始消息，不吞不猜）。

    ``/api/sources`` 上每一种「盘上这东西读不到」的降级都用它拼原因：少了这一段，
    降级就只剩一句「读不出来」，排查时不知道是被占用、权限不足还是路径坏了。
    """
    errno_part = f"errno {exc.errno}：" if exc.errno is not None else ""
    return f"{type(exc).__name__}: {errno_part}{exc.strerror or exc}"


def _iso_from_stamp(stamp: float) -> tuple[str | None, str | None]:
    """时间戳 → ``(本地时区 ISO 8601 秒精度, 转不出来时的原因)``。

    ``datetime.fromtimestamp`` 不是「拿到数字就一定能转」：Windows 上本地时间转换的上限约
    公元 3000 年，再往后抛 ``OSError [Errno 22]``（``OverflowError`` / ``ValueError`` 同理）。
    这里**只回原因、不抛**——一个坏时间戳只该让那一条的 ``mtime`` 变 ``null`` + 一句说明，
    **绝不许把整份清单打成 500**（2026-10-15 复核实测：一个 `os.utime` 到公元 5000 年的
    产物文件就能让 ``GET /api/sources`` 整份 500，且文案里连材料 id 都没有）。
    """
    try:
        return datetime.fromtimestamp(stamp).astimezone().replace(microsecond=0).isoformat(), None
    except (OSError, ValueError, OverflowError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _iso_mtime(path: Path) -> tuple[str | None, str | None]:
    """文件的最后修改时间 → ``(本地时区 ISO 8601 秒精度, 读不出来时的原因)``。

    两处都可能失败，**都不抛**（调用方把原因变成一条点名材料的可读降级）：``stat`` 失败
    （文件不在 / 权限不足）与时间戳超出可表示范围（见 :func:`_iso_from_stamp`）。
    秒精度是与产物里的 ``generated_at`` 对齐的——同一批产物混用两种精度只会让人误判先后。
    """
    try:
        stamp = path.stat().st_mtime
    except OSError as exc:
        return None, _os_error_text(exc)
    return _iso_from_stamp(stamp)


def _str_or_none(value: Any) -> str | None:
    """只接受非空字符串，其余（缺键 / ``null`` / 数字）一律回 ``None``。

    ``/api/sources`` 的每个时间戳都过这一道：时间戳的位置上写 ``0`` 或空串都是编造。
    """
    return value if isinstance(value, str) and value else None


def _load_object_reporting(
    path: Path,
    degraded: list[str],
    *,
    missing: str,
    broken: str,
) -> dict[str, Any] | None:
    """读一份产物 JSON 对象；**缺失 / 解析失败 / 形状不是对象 / 读不出来**都只记一条可读说明并回 ``None``。

    这是 ``/api/sources`` 「绝不 500」的落点：清单是一次「盘上现在有什么」的快照，
    一份坏产物只该让它对应的那一块变 ``null``（+ 一句人读的话），其余材料照常列出来。

    ``OSError`` 单独接一档（2026-10-15 复核补漏）：文件**在**、但读不出来（被别的进程独占、
    权限不足）在真机上是一条裸 ``PermissionError`` 冒到兜底 → 500。它和坏 JSON 同一档处理：
    那一块 ``null`` + 点名文件与原因。**不改 ``ar.jsonio.read_text`` 的异常口径**——
    那个函数服务全项目（CLI 等），把 ``OSError`` 收进去会牵动别的入口，不在本次范围。
    """
    if not path.is_file():
        degraded.append(missing)
        return None
    try:
        payload = read_json_if_exists(path)  # 文件在，走的必然是解析分支
    except InputError as exc:
        degraded.append(f"{broken}（{exc}）")
        return None
    except OSError as exc:
        degraded.append(f"{broken}（读的时候失败：{_os_error_text(exc)}）")
        return None
    if not isinstance(payload, dict):
        degraded.append(f"{broken}（顶层不是 JSON 对象，而是 {type(payload).__name__}）")
        return None
    return payload


def _reject_unencodable_content(content: str) -> None:
    """正文按 UTF-8 编码不了 ⇒ 400（``InputError``）+ 可读文案，点名字段与那个字符。

    摄取是「原文原样落盘」的入口：正文最终一定要变成 UTF-8 字节（暂存文件与派生 id 时的
    内容哈希都要 ``encode("utf-8")``）。JSON 里写得出**孤立代理项**（``"\\ud800"``）而
    ``json.loads`` 照收，于是那一步抛 ``UnicodeEncodeError`` —— 那是客户端给的正文有问题，
    该在入参校验这里变成 400，而不是在写盘时炸成 500（2026-10-15 复核实测修）。

    文案只写**码位**（``U+D800``），不把那半个字符原样拼进去：它自己也编不成 UTF-8，
    拼进错误消息会让响应体再炸一次。
    """
    try:
        content.encode("utf-8")
    except UnicodeEncodeError as exc:
        bad = content[exc.start] if 0 <= exc.start < len(content) else ""
        where = f"第 {exc.start + 1} 个字符" + (f"（U+{ord(bad):04X}）" if bad else "")
        raise InputError(
            f"content 里有没法按 UTF-8 编码的字符：{where}编不成 UTF-8 文本"
            "（孤立代理项，通常来自上游的错误编码）；本次没有写任何东西，请修正正文后重传"
        ) from exc


def _upload_source_id(filename: str, content: str) -> str:
    """从文件名主干派生 ``source_id``（接口契约冻结）：小写 → 非 ``[a-z0-9]`` 压成 ``-`` → 去首尾 ``-``。

    slug 为空（纯中文名、纯符号名）时用 ``upload-<content 的 sha256 前 8 位>``：这样
    ① 结果一定合法（能过 :func:`ar.paths.check_source_id`）；② 与**内容**绑定——
    同一份文本重传必得同一个 id，于是能被 409 那条「已存在」拦住，不会每次上传都堆一个新目录。
    """
    stem = Path(filename).stem.lower()
    slug = _SLUG_RE.sub("-", stem).strip("-")[:64].strip("-")
    if slug:
        return slug
    return "upload-" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]


def _upload_staging_name(filename: str) -> str:
    """暂存文件用**清洗过**的文件名：只留字母 / 数字 / 点 / 下划线 / 连字符。

    它同时决定 ``source.meta.json`` 里 ``ingest.path`` 显示成什么（摄取模块记的是真实传进去的路径），
    所以保留原文件名的主体、只把危险字符换掉，比一个纯随机名更容易排查。
    """
    stem = _UPLOAD_NAME_RE.sub("_", Path(filename).stem).strip("._")[:40] or "upload"
    return f"{stem}{Path(filename).suffix.lower()}"


def _upload_staging_dir(data_root: Path) -> Path:
    """在 ``data/`` 下建一个**本次上传专用**的暂存目录（``data/.upload-<随机>/``）并返回它。

    名字以 ``.`` 开头 ⇒ ``GET /api/sources`` 会照既有规则把它当「不是材料目录」跳过
    （正常路径上它在请求结束前就被删了）；万一被打断留下一个，也会在清单的 ``degraded`` 里
    **看得见**，不会像系统临时目录那样悄悄堆在别处。

    建不出来属于**服务端**环境问题（不是客户端输入错）⇒ 抛 ``WriteError``（500 + 可读消息，
    口径见 ``ar.errors``），并点名目录与 errno。
    """
    path = Path(data_root) / f"{UPLOAD_STAGING_PREFIX}{secrets.token_hex(6)}"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WriteError(
            f"上传的文本没法暂存：建不出暂存目录 {path}"
            f"（errno {exc.errno}：{exc.strerror or exc}）"
        ) from exc
    return path


def _cleanup_upload_dir(directory: Path, degraded: list[str]) -> None:
    """删掉上传用的暂存目录（**异常路径也必跑**，见 ``post_ingest`` 的 ``finally``）。

    删不掉就把话说出来（进成功响应的 ``degraded``）：暂存文件悄悄留在盘上也算一种降级，
    但不影响本次摄取结果，所以**只记一条说明、不抛异常**——抛了会把一次成功的摄取变成 500。
    """
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        degraded.append(
            f"上传用的暂存目录没能删掉（不影响本次摄取结果，可手工删除）：{directory}（{exc}）"
        )


class _TolerantStaticFiles(StaticFiles):
    """静态托管：目录不存在时回带原因的 404，而不是抛 ``RuntimeError``。

    Starlette 的 ``StaticFiles`` 即便用 ``check_dir=False`` 建，也会在**每个请求**上跑
    ``check_config()``，目录一旦不存在就抛 ``RuntimeError``（500 并打断整个应用）。
    前端 ``web/`` 由另一路并行开发、随时可能出现，所以这里在进入父类逻辑前先检查一次：
    目录不存在就回可见的 404；目录后来出现了，无需重启也能照常托管。
    """

    def __init__(self, *, web_dir: Path) -> None:
        self._web_dir = web_dir
        super().__init__(directory=str(web_dir), html=True, check_dir=False)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if not self._web_dir.is_dir():
            response = _json(
                {
                    "error": "前端静态目录不存在，静态文件暂时无法托管",
                    "status": 404,
                    "web_dir": str(self._web_dir),
                    "hint": "前端（web/）由另一路（W2）提供；/api/* 不受影响",
                },
                status_code=404,
            )
            await response(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


def _json(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code)


def _record_compress_event(
    profiles_root: Path,
    *,
    reader: str,
    source_id: str,
    paragraph: str,
    outcome: Any,
    reason: str,
) -> str | None:
    """把一次「太啰嗦」点击记成一条 ``kind="compress"`` 的行为事件（§7.1 / §5.2）。

    形状：``{v, id, at, kind:"compress", source_id, reader, paragraph, ok, reason?, scope, cached,
    requested_reason}`` —— ``ok:false`` 时带 ``reason``（与 ``ar.events`` 的折算口径对得上：
    折算成 ``-0.75`` 的信号，压缩没成功也照样说明「读者觉得这段啰嗦」）。

    返回值就是这条路径的可见性（2026-10-13 收口裁定）：**写成功给 ``None``**；
    写失败（异常 / 事件没通过校验）给一句可读说明，由 ``post_compress`` 放进**成功响应**的
    ``degraded`` 里。行为事件是**尽力而为**的采集，所以这把失败**绝不改写压缩结果**
    （压缩照常写盘、``ok`` 照常 ``true``），但也不许静默吞掉 —— 前端在成功路径上照样打一条 warn 横幅。
    """
    try:
        event, dropped = validate_event(
            {
                "kind": "compress",
                "source_id": source_id,
                "paragraph": paragraph,
                "ok": bool(outcome.ok),
                "reason": outcome.reason or "",
                "scope": COMPRESS_SCOPE,
                "cached": bool(outcome.cached),
                "requested_reason": reason,
            },
            reader=reader,
        )
    except Exception as exc:  # noqa: BLE001 - 记账失败绝不改写压缩结果，但要说出来
        return f"这段已压缩，但这次点击没能记成行为事件（不影响压缩结果与产物）：{type(exc).__name__}: {exc}"
    if event is None:
        return (
            "这段已压缩，但这次点击没能记成行为事件（不影响压缩结果与产物）："
            f"事件没通过校验（{dropped or '原因未知'}）"
        )
    if not outcome.reason:
        event.pop("reason", None)  # 成功事件不带 reason（§7.1：``reason?`` 只在降级时有意义）
    try:
        append_events(profiles_root, reader, [event])
    except Exception as exc:  # noqa: BLE001 - 同上
        return (
            "这段已压缩，但这次点击没能写进行为事件流 events.jsonl（不影响压缩结果与产物）："
            f"{type(exc).__name__}: {exc}"
        )
    return None


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001 - 客户端给了坏 JSON，回 400 而不是 500
        raise InputError(f"请求体不是合法 JSON：{exc}") from exc
    if not isinstance(body, dict):
        raise InputError("请求体必须是 JSON 对象")
    return body


def build_app(
    *,
    data_dir: Path | str = DEFAULT_DATA_DIR,
    profiles_dir: Path | str = DEFAULT_PROFILES_DIR,
    web_dir: Path | str = DEFAULT_WEB_DIR,
    samples_dir: Path | str | None = DEFAULT_SAMPLES_DIR,
    config: Config | None = None,
    reader: str = DEFAULT_READER,
    client_factory: Callable[[], Any] | None = None,
    explain_cache: ExplainCache | None = None,
) -> Starlette:
    """组装应用。测试可注入 ``client_factory``（返回假客户端：要提供统一调用层认的三个成员
    ``arequest_once`` / ``retry_policy`` / ``configured``，见 ``ar.tools._ResultKeeper`` 的转发说明）。

    ``explain_cache`` 默认**每个应用一个**（容量 128 的进程内 LRU）：一次进程内所有请求共用，
    进程重启即空；测试可以注入自己的实例来观察/清空缓存。
    """
    config = config or Config()
    data_root = Path(data_dir)
    profiles_root = Path(profiles_dir)
    web_root = Path(web_dir)
    samples_root = Path(samples_dir) if samples_dir is not None else None
    cache = explain_cache if explain_cache is not None else ExplainCache()

    def make_client() -> Any:
        if client_factory is not None:
            return client_factory()
        return ModelClient(config)

    # ------------------------------------------------------------------ 静态 --
    async def index(request: Request) -> Response:
        """启动页 ``web/index.html``；带页面参数时 302 到阅读器页（老链接 ``/?source=<id>`` 照旧能用）。

        判据是 ``source`` / ``fixture`` **这两个键存在**（空值也算存在）——前端的两套模式
        （``?source=`` 真产物 / ``?fixture=`` 夹具）都在这两个键上，别的 query 一概不影响托管。
        跳转时 query **原样拼**（``request.url.query``，不重排、不重新编码）。
        """
        if "source" in request.query_params or "fixture" in request.query_params:
            return RedirectResponse(f"/reader?{request.url.query}", status_code=302)
        index_path = web_root / "index.html"
        if index_path.is_file():
            return FileResponse(index_path, media_type="text/html; charset=utf-8")
        return _json(
            {
                "error": "web/index.html 不存在，前端静态文件还没生成",
                "web_dir": str(web_root),
                "hint": "前端由另一路（W2）提供；后端接口不受影响，可直接访问 /api/health",
            },
            status_code=404,
        )

    async def reader_page(request: Request) -> Response:
        """阅读器页 ``web/reader.html``。

        该文件由前端从原来的 ``index.html`` 改名而来，**今天可能还不存在**：那时回
        **带可读文案的 404**（与 ``index`` 同一条口径），不回落到启动页——
        「阅读器还没就位」和「页面正常」是两回事，回落只会把前者藏起来。
        """
        reader_path = web_root / "reader.html"
        if reader_path.is_file():
            return FileResponse(reader_path, media_type="text/html; charset=utf-8")
        return _json(
            {
                "error": "web/reader.html 不存在，阅读器页还没生成",
                "web_dir": str(web_root),
                "hint": (
                    "阅读器页由前端（W2）从 index.html 改名而来；"
                    "在它落地之前这里只回 404，不会回落到启动页"
                ),
            },
            status_code=404,
        )

    # -------------------------------------------------------------------- API --
    async def health(request: Request) -> Response:
        return _json(
            {
                "ok": True,
                "model_configured": bool(config.model_configured),
                "model": config.model,
                "api_key_origin": config.api_key_origin,
                "web_static": (web_root / "index.html").is_file(),
            }
        )

    async def get_sources(request: Request) -> Response:
        """``GET /api/sources``：材料清单（**只读**，一个字节都不写盘）。

        形状见模块文档；几条口径值得写在这儿：

        - **缺产物 = 整块 ``null`` + 一条 degraded**（不是缺键，也不是编造的 ``0`` / ``[]``）：
          ``concepts`` / ``rewrite`` / ``fidelity`` 三块各自独立判定，互不牵连；
          坏 JSON / 形状不对 / **文件在但读不出来（被别的进程独占、权限不足）**同一档处理；
        - ``mtime`` 取该材料目录下 ``rewritten.md`` / ``changes.json`` / ``decisions.json``
          里**存在者中最新**的那个（＝「最后动过」），三份都没有才退回 ``source.meta.json`` 的
          mtime；时间一律本地时区 ISO 8601，读不到写 ``null``。
          **坏时间戳只 null 这一条**：超出 ``datetime.fromtimestamp`` 可表示范围（Windows 上限约
          公元 3000 年）时写 ``null`` + 一条点名「哪个材料 / 哪个文件 / 什么原因」的说明，
          **绝不让一份坏时间戳把整份清单打成 500**；
        - ``rewritten.md`` 在响应里**没有对应的块**，所以它缺失 / stat 不到时不靠别人代说：
          mtime 那一步自己留一条说明（否则「mtime 悄悄退到别的产物」就是静默）；
        - ``data/`` 下的条目：``.`` 开头的目录、没有 ``source.meta.json`` 的目录各记一条说明；
          **悬空 junction / 符号链接 / stat 失败的条目**也记一条（点名条目与原因）；
          **普通散文件静默跳过**（既有口径，材料是目录）；
        - 排序 **mtime 倒序**（新的在前），同一时刻按 ``source_id`` 升序 —— 两次调用结果稳定。
          排的就是响应里暴露的那个秒精度字符串（不是裸 ``st_mtime``）：差不到一秒的两份材料
          在页面看来是同一时刻，那它们的先后就该由 ``source_id`` 决定，否则「按接口的 mtime 排」
          会和「接口给的顺序」对不上，看起来像排错了。
        """
        degraded: list[str] = []
        entries: list[dict[str, Any]] = []

        if not data_root.is_dir():
            # 「整个 data/ 是空的」这一档不记降级（契约明说了）；**目录不存在**另说：
            # 那更像是 --data-dir 指错了地方，留一句说明总比一片空白强（不许静默）。
            degraded.append(f"data 目录还不存在：{data_root}（还没有摄取过任何材料）")
        else:
            try:
                children = sorted(data_root.iterdir(), key=lambda item: item.name)
            except OSError as exc:
                # 连目录都列不出来（权限等）：说明白，别假装「没有材料」
                children = []
                degraded.append(f"data 目录列不出来：{data_root}（{type(exc).__name__}: {exc}）")

            for child in children:
                try:
                    child.stat()  # 只为回答「这个条目到底存不存在」：真正的判定在下面两步
                except OSError as exc:
                    # **悬空 junction / 符号链接（目标已删）**与「stat 失败」都落在这里：条目在 data/
                    # 里列得出来，却既不是文件也不是能进的目录。一声不吭地跳过等于告诉用户
                    # 「data/ 里没这东西」——而它明明看得见（2026-10-15 复核实测）。
                    degraded.append(
                        f"{child.name}：这个条目 stat 不到（悬空 junction / 符号链接，或权限问题），"
                        f"已跳过（{_os_error_text(exc)}）"
                    )
                    continue
                if not child.is_dir():
                    continue  # data/ 下的散文件不是材料（材料是目录）：既有口径，静默跳过
                if child.name.startswith("."):
                    degraded.append(f"{child.name}：以「.」开头的目录不是材料目录，已跳过")
                    continue
                meta_path = child / "source.meta.json"
                if not meta_path.is_file():
                    degraded.append(
                        f"{child.name}：目录里没有 source.meta.json，尚未摄取（或不是材料目录），已跳过"
                    )
                    continue

                source_id = child.name
                meta = _load_object_reporting(
                    meta_path,
                    degraded,
                    missing=f"{source_id}：没有 source.meta.json，尚未摄取（已跳过）",
                    broken=f"{source_id}：source.meta.json 读不出来，段数与摄取时间按未知处理",
                )
                rows = meta.get("paragraphs") if meta is not None else None
                paragraphs = len(rows) if isinstance(rows, list) else None
                if meta is not None and paragraphs is None:
                    degraded.append(
                        f"{source_id}：source.meta.json 里没有段落表（paragraphs 缺失或不是数组），段数写 null"
                    )
                ingest_meta = meta.get("ingest") if meta is not None else None
                ingested_at = _str_or_none(ingest_meta.get("at") if isinstance(ingest_meta, dict) else None)
                if meta is not None and ingested_at is None:
                    degraded.append(
                        f"{source_id}：source.meta.json 里没有 ingest.at，摄取时间写 null"
                    )

                # 「最后动过」：三份产物里存在者中最新的一份；三份都没有才退回 source.meta.json
                stamps: list[tuple[float, str]] = []
                for name in _MTIME_ARTIFACTS:
                    try:
                        stamps.append(((child / name).stat().st_mtime, name))
                    except OSError as exc:
                        # 「stat 不到」不再当成「没有这份产物」一声不吭：changes / decisions 各自那块
                        # 会给「还没有 …」的说明，**rewritten.md 在响应里没有对应的块**，
                        # 于是它的缺失只会在 mtime 上悄悄表现成「退到别的产物」。这一支必须自己说
                        # （点名材料 + 文件 + 原因），因为「降级不许静默」。
                        if name in _ARTIFACTS_WITH_OWN_BLOCK and isinstance(exc, FileNotFoundError):
                            continue  # 真的没有这份产物：那一块自己会给说明
                        reason = (
                            "还没有这份产物"
                            if isinstance(exc, FileNotFoundError)
                            else _os_error_text(exc)
                        )
                        degraded.append(
                            f"{source_id}：{name} 没有 mtime 可读（{reason}），本次不计入 mtime"
                        )
                if stamps:
                    newest = max(stamp for stamp, _name in stamps)
                    mtime, why = _iso_from_stamp(newest)
                    if mtime is None:
                        names = " / ".join(name for stamp, name in stamps if stamp == newest)
                        degraded.append(
                            f"{source_id}：{names} 的 mtime 超出可表示范围，这一条的 mtime 写 null"
                            f"（{why}）"
                        )
                else:
                    mtime, why = _iso_mtime(meta_path)
                    if mtime is None:
                        degraded.append(
                            f"{source_id}：source.meta.json 的 mtime 读不出来，这一条的 mtime 写 null"
                            f"（{why}）"
                        )

                concepts = _load_object_reporting(
                    child / "concepts.json",
                    degraded,
                    missing=f"{source_id}：还没有 concepts.json，概念清单未抽取",
                    broken=f"{source_id}：concepts.json 读不出来，概念清单按未抽取处理",
                )
                concepts_block: dict[str, Any] | None = None
                if concepts is not None:
                    concept_rows = concepts.get("concepts")
                    concepts_meta = concepts.get("meta")
                    concepts_block = {
                        "present": True,
                        "count": len(concept_rows) if isinstance(concept_rows, list) else None,
                        "generated_at": _str_or_none(
                            concepts_meta.get("generated_at") if isinstance(concepts_meta, dict) else None
                        ),
                    }

                decisions = _load_object_reporting(
                    child / "decisions.json",
                    degraded,
                    missing=f"{source_id}：还没有 decisions.json，逐段改写还没跑过",
                    broken=f"{source_id}：decisions.json 读不出来，改写状态按未知处理",
                )
                rewrite_block: dict[str, Any] | None = None
                if decisions is not None:
                    raw_meta = decisions.get("meta")
                    if not isinstance(raw_meta, dict):
                        degraded.append(
                            f"{source_id}：decisions.json 里没有 meta 块，改写计数与时间写 null"
                        )
                        raw_meta = {}
                    action_counts = raw_meta.get("action_counts")
                    degraded_rows = raw_meta.get("degraded")
                    paragraph_count = raw_meta.get("paragraph_count")
                    rewrite_block = {
                        "present": True,
                        "generated_at": _str_or_none(raw_meta.get("generated_at")),
                        "action_counts": action_counts if isinstance(action_counts, dict) else None,
                        # ``meta.degraded`` 是**条数**：键不存在写 null（老产物），存在但不是数组也写 null
                        "degraded_count": len(degraded_rows) if isinstance(degraded_rows, list) else None,
                        # 老产物缺 paragraph_count ⇒ null（「键不存在」≠「值为 0」）
                        "paragraph_count": paragraph_count
                        if isinstance(paragraph_count, int) and not isinstance(paragraph_count, bool)
                        else None,
                    }

                changes = _load_object_reporting(
                    child / "changes.json",
                    degraded,
                    missing=f"{source_id}：还没有 changes.json，事实锚校验没跑过",
                    broken=f"{source_id}：changes.json 读不出来，保真状态按未知处理",
                )
                fidelity_block: dict[str, Any] | None = None
                if changes is not None:
                    fidelity = changes.get("fidelity")
                    if not isinstance(fidelity, dict):
                        degraded.append(
                            f"{source_id}：changes.json 里没有 fidelity 块，保真状态按未校验记"
                        )
                        fidelity = {}
                    mismatch_rows = fidelity.get("mismatches")
                    checked = fidelity.get("checked")
                    checked = checked if isinstance(checked, bool) else None
                    counts = fidelity.get("counts")
                    mismatch_count = len(mismatch_rows) if isinstance(mismatch_rows, list) else None
                    fidelity_block = {
                        "present": True,
                        "checked": checked,
                        # ok = 「校验过」且「一处不一致都没有」；没校验过就不是通过（宁可说不通过，
                        # 也不许把「没校验」包装成「通过」）
                        "ok": bool(checked is True and mismatch_count == 0),
                        "mismatch_count": mismatch_count,
                        "counts": counts if isinstance(counts, dict) else None,
                    }

                entries.append(
                    {
                        "source_id": source_id,
                        "paragraphs": paragraphs,
                        "ingested_at": ingested_at,
                        "mtime": mtime,
                        "concepts": concepts_block,
                        "rewrite": rewrite_block,
                        "fidelity": fidelity_block,
                    }
                )

        # 两趟稳定排序：先 id 升序，再按 mtime 倒序 —— 于是「mtime 相同的按 source_id 升序」
        # 自动成立（第二趟是稳定排序，同刻条目的相对次序不会被它打乱）。
        # mtime 为 null（连 source.meta.json 都 stat 不到）的条目排在最后：它不是「最新」。
        entries.sort(key=lambda item: item["source_id"])
        entries.sort(key=lambda item: item["mtime"] or "", reverse=True)
        return _json({"sources": entries, "degraded": degraded})

    async def post_ingest(request: Request) -> Response:
        """``POST /api/ingest``：上传的文本 → ``data/<source_id>/``（**只摄取**）。

        请求 ``{filename, content, source_id?, overwrite?}``；成功 200
        ``{source_id, paragraphs, ingested_at, anchors_stable, overwritten, degraded[]}``。

        几处**非显然的决定**：

        - 段锚点一律由 ``ar.ingest.ingest`` 生成——它要一个「磁盘上的源文件路径」，
          所以正文先原样写进 **``data/.upload-<随机>/`` 暂存目录**再交给它，收尾 **``finally`` 必删**
          （异常路径也不例外；暂存不落系统临时目录，理由见 :data:`UPLOAD_STAGING_PREFIX`）；
          **不自己拼 ``source.meta.json``**：那样迟早和 ``ar ingest`` 的切段规则走偏。
        - ``anchors_stable`` 透传摄取模块的锚点对照判据（``IngestResult.warnings`` 为空＝没发现
          段落增删 / ``text_hash`` 变化，也就是 CLI 打的那行「锚点稳定」）。首次摄取**没有可比的旧锚点**，
          这时判据同样是「没有警告」⇒ ``true``（是不是覆盖来的由 ``overwritten`` 说明）。
        - ``overwrite`` **只有 JSON 布尔 ``true`` 才算**：目标目录里已有 ``source.md`` 而没带它 → 409。
          别用「真值」判断（``"false"`` 字符串是真值，那会把「不覆盖」读成「覆盖」）。
        - 覆盖只换 ``source.md`` / ``source.meta.json``：既有 concepts / decisions / changes 是
          按**旧锚点**算的，覆盖后它们可能对不上，需要重跑 analyze / rewrite / check（409 的文案里说了）。
        """
        body = await _json_body(request)
        filename = body.get("filename")
        content = body.get("content")
        raw_source_id = body.get("source_id")
        overwrite = body.get("overwrite") is True

        if not isinstance(filename, str) or not filename.strip():
            raise InputError("filename 必须是非空字符串（上传文件的原始文件名，用来派生 source_id）")
        # 前后空白不是文件名的一部分（它由上传控件给，不值得为这点差异回一句「扩展名不在白名单」）
        filename = filename.strip()
        suffix = Path(filename).suffix.lower()
        if suffix not in UPLOAD_EXTENSIONS:
            raise InputError(
                f"filename 的扩展名不在白名单里：{filename!r}"
                f"（只收 {' / '.join(UPLOAD_EXTENSIONS)}，大小写不敏感）"
            )
        if not isinstance(content, str) or content == "":
            raise InputError("content 必须是非空字符串（上传的文本按 UTF-8 文本处理）")
        if len(content) > MAX_UPLOAD_CHARS:
            raise InputError(
                f"content 太长：{len(content)} 个字符，上限 {MAX_UPLOAD_CHARS} 个字符"
                "（超出即拒收，不截断——截断会悄悄改掉材料内容）"
            )
        # 正文最终要按 UTF-8 落盘（``content.encode("utf-8")``：暂存文件、以及派生 id 时的内容哈希）。
        # 编不成的那一类（孤立的代理项，例如 JSON 里写 "\ud800"）是**客户端输入错**，
        # 所以在这里就地判成 400 —— 否则它会以 UnicodeEncodeError 的形式在写盘那一步炸成 500
        # （2026-10-15 复核实测）。放在写暂存目录之前，一个字节都还没落盘。
        _reject_unencodable_content(content)

        if raw_source_id is None or (isinstance(raw_source_id, str) and not raw_source_id.strip()):
            # 省略 / 空串都按「从 filename 派生」办（与 /api/explain 的 selection_from 同一口径）
            source_id = _upload_source_id(filename, content)
        elif not isinstance(raw_source_id, str):
            raise InputError("source_id 必须是字符串（可省略，省略时从 filename 派生）")
        else:
            # 复用全项目唯一那份 source_id 校验（正则 + 拒 ``..``），与 CLI / doc_dir 同一条口径
            source_id = check_source_id(raw_source_id.strip())

        target = doc_dir(data_root, source_id)
        already = (target / "source.md").is_file()
        if already and not overwrite:
            return _json(
                {
                    "error": f"材料 {source_id} 已经存在，本次没有写任何东西：{target / 'source.md'} 已存在",
                    "status": 409,
                    "source_id": source_id,
                    "hint": (
                        "要覆盖就带 overwrite: true（JSON 布尔 true）。覆盖只重写 source.md 与 "
                        "source.meta.json：既有 concepts / decisions / changes 是按旧锚点算的，"
                        "覆盖后可能需要重跑 analyze / rewrite / check"
                    ),
                },
                status_code=409,
            )

        degraded: list[str] = []
        # 暂存放 ``data/.upload-<随机>/``（理由见 UPLOAD_STAGING_PREFIX）：这是**摄取模块之外**
        # 唯一会被写下的东西，收尾必删；这一行在 409 判断之后，所以冲突那条路一个字节都不写。
        staging_dir = _upload_staging_dir(data_root)
        try:
            staging_path = staging_dir / _upload_staging_name(filename)
            try:
                staging_path.write_bytes(content.encode("utf-8"))
            except OSError as exc:
                # 暂存写不进去属于**服务端**环境问题（不是客户端输入错）：
                # 抛 WriteError 走 500 + 可读消息，而不是伪装成 400（口径见 ar.errors）
                raise WriteError(
                    f"上传的文本没能写到暂存文件：{staging_path}（{type(exc).__name__}: {exc}）"
                ) from exc
            result = ingest(staging_path, source_id, data_root)
        finally:
            _cleanup_upload_dir(staging_dir, degraded)

        ingest_meta = result.meta.get("ingest")
        return _json(
            {
                "source_id": source_id,
                "paragraphs": len(result.paragraphs),
                # 摄取模块保证写这个键（防御性读取：真缺了也写 null，不编造时间）
                "ingested_at": _str_or_none(
                    ingest_meta.get("at") if isinstance(ingest_meta, dict) else None
                ),
                "anchors_stable": not result.warnings,
                "overwritten": bool(already),
                # 摄取模块的可见降级（例：材料变了要重新校准画像）原样透传；后面那条只可能是
                # 「暂存目录没删掉」——它不影响结果，但也不许静默
                "degraded": [*result.warnings, *degraded],
            }
        )

    async def get_doc(request: Request) -> Response:
        source_id = request.path_params["source_id"]
        target = doc_dir(data_root, source_id)
        meta = read_json_if_exists(target / "source.meta.json")
        degraded: list[str] = []
        if meta is None:
            return _json(
                {
                    "error": f"找不到材料 {source_id}：{target / 'source.meta.json'} 不存在",
                    "hint": f"先运行 `uv run ar ingest <文件> --id {source_id}`",
                    "degraded": [f"材料 {source_id} 尚未摄取"],
                },
                status_code=404,
            )

        rewritten = read_text_if_exists(target / "rewritten.md")
        if rewritten is None:
            degraded.append("rewritten.md 不存在：改写（M3）尚未实现，主栏应显示原文并标「未改写」")
        changes = read_json_if_exists(target / "changes.json")
        if changes is None:
            degraded.append("changes.json 不存在：没有逐段变更记录，段落标记只能显示「未改写」")
        concepts = read_json_if_exists(target / "concepts.json")
        if concepts is None:
            degraded.append("concepts.json 不存在：概念清单未生成或抽取失败，当前为「无画像」模式")

        return _json(
            {
                "meta": meta,
                "rewritten": rewritten,
                "changes": changes,
                "concepts": concepts,
                "degraded": degraded,
            }
        )

    async def get_profile(request: Request) -> Response:
        profile, notes = load_profile(profiles_root, reader)
        return _json({"profile": profile, "degraded": notes})

    async def post_rating(request: Request) -> Response:
        body = await _json_body(request)
        concept_id = body.get("concept_id")
        state = body.get("state")
        note = body.get("note")
        if not isinstance(concept_id, str) or not concept_id.strip():
            raise InputError("concept_id 必须是非空字符串")
        if not isinstance(state, str):
            raise InputError("state 必须是字符串（known / partial / unknown）")
        if note is not None and not isinstance(note, str):
            raise InputError("note 必须是字符串")
        profile, _notes = load_profile(profiles_root, reader)
        updated = apply_rating(profile, concept_id.strip(), state, note)
        save_profile(profiles_root, reader, updated)
        return _json({"ok": True, "profile": updated, "degraded": []})

    async def post_preference(request: Request) -> Response:
        """写入一条**偏好**（§14.1）：``{id, value, note?}`` → 画像写回，返回 ``{ok, profile, degraded}``。

        这是偏好（尤其「事实锚口径」``pref_fact_anchor``）的**唯一创建路径**：行为信号回流
        （``applySignals``）只给已有偏好追加 evidence、绝不自动新建偏好，两条路分工明确。
        可选项由 ``ar.profile.PREFERENCE_VALUES`` 定：``id`` 不可写 / ``value`` 不在可选值里
        → 400（``InputError``）并点名合法取值；同 id 同取值**幂等**（不追加证据、不写盘）。
        """
        body = await _json_body(request)
        preference_id = body.get("id")
        value = body.get("value")
        note = body.get("note")
        if not isinstance(preference_id, str) or not preference_id.strip():
            raise InputError("id 必须是非空字符串（可写的偏好见 ar.profile.PREFERENCE_VALUES）")
        if not isinstance(value, str) or not value.strip():
            raise InputError("value 必须是非空字符串")
        if note is not None and not isinstance(note, str):
            raise InputError("note 必须是字符串")
        profile, notes = load_profile(profiles_root, reader)
        updated = set_preference(profile, preference_id.strip(), value.strip(), note)
        save_profile(profiles_root, reader, updated)
        return _json({"ok": True, "profile": updated, "degraded": list(notes)})

    async def post_explain(request: Request) -> Response:
        body = await _json_body(request)
        source_id = body.get("source_id")
        paragraph = body.get("paragraph")
        kind = body.get("kind") or "explain"
        selection = body.get("selection")
        selection_from = body.get("selection_from")
        if not isinstance(source_id, str) or not source_id.strip():
            raise InputError("source_id 必须是非空字符串")
        if not isinstance(paragraph, str) or not paragraph.strip():
            raise InputError("paragraph 必须是非空字符串（段 id，如 p012）")
        if not isinstance(kind, str):
            raise InputError("kind 必须是字符串（explain / simpler / detail）")
        if selection is not None and not isinstance(selection, str):
            raise InputError("selection 必须是字符串（页面上选中的那段文字，可省略）")
        if selection_from is None or (isinstance(selection_from, str) and not selection_from.strip()):
            selection_from = DEFAULT_SELECTION_FROM  # 可选字段：缺省 / 空串都按默认值处理
        elif not isinstance(selection_from, str) or selection_from.strip() not in SELECTION_SOURCES:
            raise InputError(
                f"selection_from 只能是 {' / '.join(SELECTION_SOURCES)}（默认 {DEFAULT_SELECTION_FROM}）"
            )

        outcome = await aexplain(
            source_id.strip(),
            paragraph.strip(),
            kind.strip(),
            selection=selection,
            selection_from=selection_from.strip(),
            data_dir=data_root,
            profiles_dir=profiles_root,
            reader=reader,
            client=make_client(),
            cache=cache,
        )
        payload: dict[str, Any] = {"text": outcome.text, "degraded": outcome.degraded}
        if outcome.reason:
            payload["reason"] = outcome.reason
        # scope 与 selection_truncated 描述的是**这次请求要什么**（整段还是片段、片段有没有被截断），
        # 与模型是否可用无关，所以每次响应都给（前端拿 scope 确认「确实按片段讲的」）。
        payload["scope"] = outcome.scope
        if outcome.selection_truncated:
            payload["selection_truncated"] = True
        # attempts（追加字段）：这次调用的逐次尝试账，**成功与降级都给**——讲解不落盘，
        # 这是它的重试账唯一能被看见的地方；没发请求时是空数组（未配置 / 缓存命中 / 段落不存在）。
        # 结构 = 产物里的 meta.attempts（{tool, try, elapsed_ms, code, delay_ms, retry_after_used}）。
        payload["attempts"] = [dict(item) for item in outcome.attempts]
        if not outcome.degraded:
            # 下面几个描述的是**这次产出了什么**：失败/降级时一律不出现
            payload["model"] = outcome.model
            payload["prompt_version"] = outcome.prompt_version
            # 原始 usage（含 prompt_tokens_details.cached_tokens 这类前缀缓存字段）；
            # 缓存命中（cached: true）或网关没回 usage 时是空对象 {}
            payload["usage"] = outcome.usage
            if outcome.cached:
                payload["cached"] = True
        return _json(payload)

    async def post_events(request: Request) -> Response:
        """``POST /api/events``：行为事件**只追加**到 ``profiles/<reader>/events.jsonl``。

        请求 ``{events:[…]}``（也容忍单个事件对象）；响应 ``{ok, written, duplicates, total, degraded[]}``。
        处置口径（PLAN-actions-feedback §7.1 的校验三条 + §7.2）：

        - ``reader`` **由服务端盖章**（请求里给了也不认）；``id`` 缺失用复合键兜底去重；
        - ``source_id`` 非法 → 400（``InputError``，整批不写）；
        - ``paragraph`` 不在 ``source.meta.json`` 里 → **丢弃 + degraded**（不是 400）；
        - 材料未摄取 / 写盘失败 → 200 + ``ok:false`` + ``written:0`` + ``degraded``；
        - **绝不写 ``profile.yaml``**：写一次画像就换指纹，讲解缓存与前端会话键全失效。
        """
        body = await _json_body(request)
        raw = body.get("events")
        if raw is None and body.get("kind") is not None:
            raw = [body]  # 容忍单个事件对象
        if raw is None:
            return _json(
                {
                    "ok": True,
                    "written": 0,
                    "duplicates": 0,
                    "total": 0,
                    "degraded": ["请求里没有事件（events 缺失或为空）：什么都没写"],
                }
            )
        if not isinstance(raw, list):
            raise InputError("events 必须是数组（也可直接给单个事件对象）")

        degraded: list[str] = []
        fatal = False
        stamped: list[dict[str, Any]] = []
        meta_cache: dict[str, Any] = {}

        def meta_of(source_id: str) -> Any:
            """``source.meta.json`` 按材料缓存一次（同一批里同一材料只读一次盘）。"""
            if source_id not in meta_cache:
                meta_cache[source_id] = read_json_if_exists(doc_dir(data_root, source_id) / "source.meta.json")
            return meta_cache[source_id]

        for position, item in enumerate(raw, start=1):
            event, reason = validate_event(item, reader=reader)
            if event is None:
                degraded.append(f"第 {position} 条事件被丢弃：{reason}")
                continue
            meta = meta_of(event["source_id"])
            if meta is None:
                fatal = True
                degraded.append(
                    f"材料 {event['source_id']} 尚未摄取：第 {position} 条事件没写盘"
                    f"（先跑 `uv run ar ingest <文件> --id {event['source_id']}`）"
                )
                continue
            paragraph = event.get("paragraph")
            if paragraph is None:
                stamped.append(event)  # 材料级事件（read_complete / read_abandon …）没有段
                continue
            if not isinstance(paragraph, str) or not paragraph.strip():
                degraded.append(f"第 {position} 条事件的 paragraph 不是非空字符串：已丢弃")
                continue
            paragraph = paragraph.strip()
            known = {row.get("id") for row in (meta.get("paragraphs") or []) if isinstance(row, dict)}
            if paragraph not in known:
                degraded.append(
                    f"第 {position} 条事件的段 {paragraph} 不在 {event['source_id']} 的 source.meta.json 里："
                    "已丢弃（前端可能拿着旧产物）"
                )
                continue
            event["paragraph"] = paragraph
            stamped.append(event)

        existing, read_notes = read_events(profiles_root, reader)
        degraded.extend(read_notes)
        fresh, duplicates = dedup(stamped, existing)
        if duplicates:
            degraded.append(f"{len(duplicates)} 条事件与 events.jsonl 里已有的重复（按 id 去重，先到先留）：已跳过")
        written = 0
        if fresh:
            try:
                written = append_events(profiles_root, reader, fresh)
            except (WriteError, OSError, TypeError, ValueError) as exc:
                # ``WriteError`` 是 ``append_events`` 对坏目录给的可读错误（点名目录 + errno，
                # 见 ar.jsonio.readable_write_error）：**契约不变**——事件是尽力而为的采集，
                # 照样回 200 + ok:false + written:0 + degraded，不退化成 500（DESIGN §6）。
                fatal = True
                written = 0
                degraded.append(f"events.jsonl 写盘失败（written=0，不假装成功）：{type(exc).__name__}: {exc}")
        return _json(
            {
                "ok": not fatal,
                "written": written,
                "duplicates": len(duplicates),
                "total": len(raw),
                "degraded": degraded,
            }
        )

    async def post_compress(request: Request) -> Response:
        """``POST /api/compress``：读者点「太啰嗦」→ 当场压缩该段并写回三份产物（§5.2）。

        请求 ``{source_id, paragraph, reason?}``（``reason`` 缺省 ``too_verbose``）。
        - 成功：``{ok:true, paragraph, action:"compress", text, scope, cached, usage, attempts,
          model, prompt_version, degraded, artifacts_updated, hint, fact_declarations, waived,
          over_declared, fact_anchor_policy}``（**17 键**，缺一不可：``over_declared`` 是「多申报」
          那一条，2026-10-14 收尾加的，别只数到 16 个就以为齐了；``hint`` 由前端直接展示；
          ``degraded`` 平时 ``[]``，非空只有三种可能：
          ① 这一段三类事实锚合计为 0，本次压缩**未经事实锚校验**（0 比对项 = 检查器空跑，见
          ``ar.rewrite.COMPRESS_NO_ANCHOR_NOTE``）；② 申报读不通（**整条不是对象**、结构不对 /
          不认识的键）；③ 那条行为事件没记上——三种都不影响 ``ok`` 与产物写盘）；
          ``fact_declarations``（本次申报，无申报时是 ``{}``）/ ``waived``（本次按读者口径放行的条目，
          结构同 ``fidelity.waived`` 的一行）/ ``over_declared``（本次「多申报」的条目：锚点一个没少
          却申报放弃了它，**不影响放行结论**）/ ``fact_anchor_policy``（本次采用的口径）是 §14.1 的
          可见面：页面据此渲染「本段申报（事实锚）」与放行徽标，重跑 ``ar check`` 后同一批条目进
          ``changes.json`` 的 ``fidelity.waived`` / ``fidelity.over_declared``；
        - **降级一律 200**：``{ok:false, reason, text, degraded[]}``，``reason`` 取
          ``ar.rewrite.COMPRESS_REASONS``（材料未摄取 / 段不存在也在这一路，不是 400）；
        - 参数本身不合法（缺 source_id / paragraph、类型不对）才 400；
        - 该次点击顺带写一条 ``kind="compress"`` 的行为事件，**写失败不影响压缩结果**，
          但会在成功响应的 ``degraded`` 里留一条说明（前端成功路径照样可见）。
        """
        body = await _json_body(request)
        source_id = body.get("source_id")
        paragraph = body.get("paragraph")
        reason = body.get("reason")
        if not isinstance(source_id, str) or not source_id.strip():
            raise InputError("source_id 必须是非空字符串")
        if not isinstance(paragraph, str) or not paragraph.strip():
            raise InputError("paragraph 必须是非空字符串（段 id，如 p012）")
        if reason is None or (isinstance(reason, str) and not reason.strip()):
            reason = DEFAULT_COMPRESS_REASON  # 可选字段：缺省 / 空串都按「太啰嗦」处理
        elif not isinstance(reason, str):
            raise InputError("reason 必须是字符串（可省略，默认 too_verbose）")
        else:
            reason = reason.strip()

        source_id = source_id.strip()
        paragraph = paragraph.strip()
        outcome = await acompress_paragraph(
            source_id,
            paragraph,
            data_dir=data_root,
            profiles_dir=profiles_root,
            reader=reader,
            client=make_client(),
            cache=cache,
            reason=reason,
        )
        # 记账（§7.1 的 kind="compress"）：写失败不改压缩结果，但**必须可见** ——
        # 成功响应的 degraded 里带一条可读说明（前端在成功路径上照样打 warn 横幅）
        event_note = _record_compress_event(
            profiles_root,
            reader=reader,
            source_id=source_id,
            paragraph=paragraph,
            outcome=outcome,
            reason=reason,
        )
        if not outcome.ok:
            # 降级一律 200：前端按 reason 分流，正文一个字符都不动
            return _json(
                {
                    "ok": False,
                    "reason": outcome.reason,
                    "text": outcome.text,
                    "degraded": list(outcome.degraded),
                }
            )
        return _json(
            {
                "ok": True,
                "paragraph": outcome.paragraph or paragraph,
                "action": "compress",
                "text": outcome.text,
                "scope": COMPRESS_SCOPE,
                "cached": bool(outcome.cached),
                "usage": dict(outcome.usage),
                "attempts": [dict(item) for item in outcome.attempts],
                "model": outcome.model,
                "prompt_version": outcome.prompt_version,
                # 成功路径上的可见说明：① 这一段没有事实锚（校验空跑，见 rewrite.COMPRESS_NO_ANCHOR_NOTE）；
                # ② 申报读不通（结构不对 / 不认识的键）；③ 那条行为事件没记上（尽力而为的采集）。
                # 三者都**不影响** ok 与写盘，前端在成功路径上把非空 degraded 打成 warn 横幅
                "degraded": [*outcome.degraded, *([event_note] if event_note else [])],
                "artifacts_updated": bool(outcome.artifacts_updated),
                "hint": outcome.hint or COMPRESS_HINT,
                # 事实锚申报的可见面（§14.1）：本次申报了什么、按哪一档口径放行了哪几项、
                # 有没有「多申报」（锚点一个没少却申报放弃了它，不影响放行结论）。
                # 同一份申报已写进 changes[i].fact_declarations（重跑 `ar check` 后进 fidelity.waived）
                "fact_declarations": dict(outcome.fact_declarations),
                "waived": [dict(item) for item in outcome.waived],
                "over_declared": [dict(item) for item in outcome.over_declared],
                "fact_anchor_policy": outcome.policy,
            }
        )

    async def get_survey(request: Request) -> Response:
        """``GET /api/survey``：题目由后端给（前端不硬编码），外加当前作答与可见降级。

        ``answered_at`` 是**追加字段**（2026-10-15，§4 追加字段纪律：旧键一个不改）：取值是画像
        ``background.answered_at`` —— ``ar.survey.finalize_background`` 落盘时写下的**作答时刻**。
        没有背景块 / 老画像缺这个键 → ``null``（**不编造**）。**不拿 ``survey.json`` 的
        ``updated_at`` 冒充**：那个键是「这份 record 最后一次被写」的时间（只补追随问也会变），
        与「读者什么时候答的固定题」不是一回事。
        """
        profile, notes = load_profile(profiles_root, reader)
        background = background_of(profile)
        stored = answered_version(profile)
        degraded: list[str] = list(notes)
        if stored is not None and stored != SURVEY_VERSION:
            degraded.append(
                f"画像里的背景问卷版本是 {stored}，当前问卷版本是 {SURVEY_VERSION}："
                "问卷已更新，可重新填（旧答案照旧可读，不会被丢掉）"
            )
        return _json(
            {
                "version": SURVEY_VERSION,
                # 读者上次答题时的问卷版本：没答过 / 画像为空 / 整场跳过 → null
                # （前端拿它做「问卷已更新，可重新填」的提示，见契约 §11）
                "answered_version": stored,
                # 上次作答的时间（追加字段）：只认真实落盘的作答时刻，缺就 null
                "answered_at": _str_or_none(background.get("answered_at")),
                "questions": QUESTIONS,
                "answers": answers_of(background) or None,
                "skipped": bool(background.get("skipped")),
                "degraded": degraded,
            }
        )

    async def post_survey(request: Request) -> Response:
        """``POST /api/survey``：提交固定题作答（或整场跳过，可附带追问的问答）。

        请求体是契约 §8 的 ``{ answers?, skipped?, followups? }``：
        ``followups``（读者对模型追问的作答）并进 ``background.followups[]``，**不丢**；
        只给 ``followups`` 不给 ``answers`` 也合法。
        校验失败一律 ``InputError`` → 400（由既有转换处理），并点名是哪一题。
        """
        body = await _json_body(request)
        raw_followups = body.get("followups")
        if raw_followups is not None and not isinstance(raw_followups, list):
            raise InputError("followups 必须是数组（每项 {q, a?, at?}）")
        answers = body.get("answers")
        if answers is None:
            # 也接受把作答直接铺在顶层（``skipped`` / ``followups`` 不是作答字段，要排除掉）
            answers = {key: value for key, value in body.items() if key not in ("skipped", "followups")}
        if not isinstance(answers, dict):
            raise InputError("answers 必须是 JSON 对象（题号 → 答案）")
        skipped = bool(body.get("skipped"))
        profile, notes = load_profile(profiles_root, reader)
        stored_version = answered_version(profile)
        existing = background_of(profile)

        # 写 record 时用：``None`` = 本次不动该项（见 ``write_survey_record``）
        record_answers: dict[str, Any] | None = None
        record_skipped: bool | None = None
        truncated: dict[str, bool] | None = None

        if skipped:
            if has_fixed_answers(profile) or has_followups(profile):
                # 跳过只记标志：**已有作答与追问一个不动**（静默删读者数据是最危险的一类行为）
                background = dict(existing)
                background["skipped"] = True
                degraded: list[str] = [
                    "已整场跳过背景问卷：只写 skipped=true；**已有背景保持不变**（不会清空已填的答案）"
                ]
            else:
                background = finalize_background({}, skipped=True)
                degraded = ["已整场跳过背景问卷：写 skipped=true，素描取「未提供」分支，按通用读者处理"]
            record_skipped = True
        else:
            picked, truncated, degraded = validate_answers(answers)
            if not picked and raw_followups is None:
                raise InputError(
                    "提交里没有任何有效作答（answers 为空，且 skipped 不是 true）："
                    "要么至少答一题，要么显式 skipped=true（不会清空已有背景）"
                )
            if picked:
                # 落盘前归一（字段顺序固定、去掉空值），保证同一次提交写出同一份 YAML
                background = normalize_background(finalize_background(picked))
                if existing.get("followups") and raw_followups is None:
                    # 重新提交固定题不该抹掉已答的追问（同一条纪律：不静默删读者数据）
                    background = merge_followups_into(background, existing["followups"])
                record_answers = dict(answers)
            else:
                # 只带了 followups：在已有背景上补追问（没有背景就新建一个）
                background = normalize_background(existing or finalize_background({}))
                truncated = None
            if raw_followups is not None:
                background = merge_followups_into(background, raw_followups)
            record_skipped = False
        degraded = list(degraded) + [note for note in notes if "不存在" not in note]
        if stored_version is not None and stored_version != SURVEY_VERSION:
            degraded.append(f"画像里的背景问卷版本是 {stored_version}，本次按当前版本 {SURVEY_VERSION} 重写")

        saved, save_notes = save_background(profiles_root, reader, background)
        degraded.extend(save_notes)
        write_survey_record(
            profiles_root,
            reader,
            answers=record_answers,
            background=background,
            truncated=truncated,
            skipped=record_skipped,
        )
        sketch = render_sketch(background)
        payload: dict[str, Any] = {
            "ok": True,
            "background": background_of(saved),
            "sketch": sketch,
            "degraded": degraded,
        }
        if truncated:
            # 降级必须可见：截断了哪一项要说出来（survey.json 里也留着原值）
            payload["truncated"] = dict(truncated)
        return _json(payload)

    async def post_survey_followup(request: Request) -> Response:
        """``POST /api/survey/followup``：按已存作答让模型追问 3–5 个细节。

        请求体可空（``{}``）；给了 ``followups`` 时先并进画像（``background.followups[]``）。
        失败 / 未配置 / 超时都返回 200 + ``degraded`` 说明，**固定题答案照常保存、不受影响**。
        """
        body: dict[str, Any] = {}
        if await request.body():
            body = await _json_body(request)
        profile, _notes = load_profile(profiles_root, reader)
        background = background_of(profile)
        if not background:
            raise InputError(
                f"profiles/{reader}/profile.yaml 里没有背景（background）："
                "请先提交固定问卷（POST /api/survey），再让模型追问"
            )
        merged = body.get("followups")
        if merged is not None:
            saved, _save_notes = append_followups(profiles_root, reader, merged)
            background = background_of(saved)
            # 记录文件跟着更新（原始作答与截断标记不动，只刷新背景），**不参与缓存指纹**（只看 profile.yaml）
            write_survey_record(
                profiles_root,
                reader,
                background=background,
                followup_meta={"event": "answers", "at": now_iso(), "count": len(background.get("followups") or [])},
            )
        outcome = await asurvey_followups(background, client=make_client(), reader=reader)
        # 这次追问调用的 token 账**同时**落在响应体与 survey.json 里（契约 §4）
        call_meta: dict[str, Any] = {
            "event": "call",
            "at": now_iso(),
            "degraded": outcome.degraded,
            "model": outcome.model,
            "prompt_version": outcome.prompt_version,
            "usage": dict(outcome.usage),
            "question_count": len(outcome.questions),
            # 逐次尝试账（追加字段，PLAN-llm-tools §3.4）：只进 survey.json，不进响应体
            "attempts": [dict(item) for item in outcome.attempts],
        }
        if outcome.reason:
            call_meta["reason"] = outcome.reason
        write_survey_record(profiles_root, reader, background=background, followup_meta=call_meta)
        payload = {
            "questions": outcome.questions,
            "text": outcome.text,
            "degraded": outcome.degraded,
            "prompt_version": outcome.prompt_version,
        }
        if outcome.reason:
            payload["reason"] = outcome.reason
        if not outcome.degraded:
            # 「这次产出了什么」：降级时一律不出现（与 /api/explain 同口径）
            payload["model"] = outcome.model
            payload["usage"] = outcome.usage
        return _json(payload)

    async def api_not_found(request: Request) -> Response:
        """未知的 /api/* 路径：回 JSON 404，不要落到静态托管的兜底里。"""
        return _json({"error": f"接口不存在：{request.url.path}", "status": 404}, status_code=404)

    routes = [
        # 启动页（GET /）：带 ?source= / ?fixture= 时 302 到阅读器页；不带 query 才托管 index.html
        Route("/", index, methods=["GET"]),
        # 阅读器页（GET /reader → web/reader.html）：必须排在静态 Mount("/") 之前，
        # 否则静态托管会先拿到 "/reader"（它只认文件与目录，不认 "reader.html"，会回 404 HTML）
        Route("/reader", reader_page, methods=["GET"]),
        Route("/api/health", health, methods=["GET"]),
        # 材料清单（只读）与上传摄取（只摄取）：都排在兜底路由 /api/{rest:path} 之前
        Route("/api/sources", get_sources, methods=["GET"]),
        Route("/api/ingest", post_ingest, methods=["POST"]),
        Route("/api/profile", get_profile, methods=["GET"]),
        Route("/api/profile/rating", post_rating, methods=["POST"]),
        # 偏好写入（§14.1：事实锚口径 pref_fact_anchor 的唯一创建路径），同样在兜底路由之前
        Route("/api/profile/preference", post_preference, methods=["POST"]),
        Route("/api/explain", post_explain, methods=["POST"]),
        # 行为事件采集（PLAN-actions-feedback §7.2）：只 append，绝不写 profile.yaml；
        # 必须排在兜底路由 /api/{rest:path} 之前
        Route("/api/events", post_events, methods=["POST"]),
        # 「太啰嗦」当场压缩（PLAN-actions-feedback §5.2）：写回三份产物；
        # 同样必须排在兜底路由 /api/{rest:path} 之前
        Route("/api/compress", post_compress, methods=["POST"]),
        # 读前背景问卷（PLAN-background-survey §8）：三条都排在兜底路由 /api/{rest:path} 之前
        Route("/api/survey", get_survey, methods=["GET"]),
        Route("/api/survey", post_survey, methods=["POST"]),
        Route("/api/survey/followup", post_survey_followup, methods=["POST"]),
        Route("/api/doc/{source_id}", get_doc, methods=["GET"]),
        Route("/api/{rest:path}", api_not_found),  # 必须在静态托管之前
    ]
    if samples_root is not None:
        # 夹具模式（`?fixture=<id>` 读 samples/<id>/doc.json）也能走本服务：契约没要求这条，
        # 是给前端的便利——只读，且只暴露 samples/ 一个目录
        routes.append(Mount("/samples", app=_TolerantStaticFiles(web_dir=samples_root), name="samples"))
    # 静态前端（目录可能还不存在：见 _TolerantStaticFiles）；挂在最后，兜住其余路径
    routes.append(Mount("/", app=_TolerantStaticFiles(web_dir=web_root), name="web"))

    async def on_input_error(request: Request, exc: Exception) -> Response:
        return _json({"error": str(exc)}, status_code=400)

    async def on_http_exception(request: Request, exc: Exception) -> Response:
        status = getattr(exc, "status_code", 500)
        detail = "文件或路由不存在" if status == 404 else str(getattr(exc, "detail", exc))
        return _json(
            {"error": detail, "status": status, "path": request.url.path},
            status_code=status,
        )

    async def on_unhandled(request: Request, exc: Exception) -> Response:  # pragma: no cover - 兜底
        return _json({"error": f"服务内部错误：{type(exc).__name__}: {exc}"}, status_code=500)

    return Starlette(
        routes=routes,
        exception_handlers={
            InputError: on_input_error,
            HTTPException: on_http_exception,
            Exception: on_unhandled,
        },
    )
