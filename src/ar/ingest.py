"""摄取 Ingest（M1）：本地 Markdown/TXT → ``source.md`` + ``source.meta.json``。

产物（DESIGN §4.1，字段逐字不改名）：

- ``source.md``：源文本**原样**（按字节复制，只读，不改一个字符）；
- ``source.meta.json``：``source_id`` / ``ingest{kind,path,hash,at}`` /
  ``paragraphs[{id,index,text,text_hash}]``。

**段落切分规则就落在这个模块**（``split_paragraphs()``，契约 §4）：

- 段落 = 源文档里被**空行**分隔的块，从 1 起编号（``id = "p" + 三位序号``）；
- Markdown 的标题行、引用行、分隔线**同样算段落**（它们是源文档的一部分，契约按空行切分、
  不作特殊处理）—— 所以 ``学习/_第1章第1节三版对照/00-底本.md`` 是 **25 段**
  （文档头 3 块 + 正文 22 块），与前端夹具 ``samples/*/doc.json`` 的段号逐一对齐；
- 段文本只去首尾空白、段内换行统一成 ``\\n``（CRLF 兼容），不改字。

锚点规则：``text_hash = sha256(规范化文本)[:8]``，
规范化 = 去首尾空白 + 合并连续空白 + 全角空格（U+3000）转半角。**同一文件重复摄取必得同一组 id 与 text_hash**。
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import InputError
from .jsonio import now_iso, read_json_if_exists, write_json
from .paths import doc_dir

__all__ = [
    "PARAGRAPH_ID_WIDTH",
    "IngestResult",
    "normalize_text",
    "paragraph_id",
    "split_paragraphs",
    "text_hash",
    "ingest",
    "anchor_diff_warnings",
]

PARAGRAPH_ID_WIDTH = 3

# 空行（允许行内只有空白）；\\r 用于兼容 CRLF
_BLANK_LINE_RE = re.compile(r"\n[ \t\r]*\n")
_FULLWIDTH_SPACE = "\u3000"
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """锚点用的规范化：全角空格转半角 + 连续空白合并 + 去首尾空白。"""
    return _WHITESPACE_RE.sub(" ", text.replace(_FULLWIDTH_SPACE, " ")).strip()


def text_hash(text: str) -> str:
    """``sha256(规范化文本)[:8]``（小写十六进制，8 位）。"""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()[:8]


def paragraph_id(index: int) -> str:
    """段号 → 锚点 id（从 1 起，三位补零）。"""
    if index < 1:
        raise ValueError(f"段号从 1 起，收到 {index}")
    return f"p{index:0{PARAGRAPH_ID_WIDTH}d}"


def split_paragraphs(raw: str) -> list[str]:
    """切段：按空行分块（规则见模块文档，与前端夹具的段号约定一致）。"""
    blocks = [b.strip() for b in _BLANK_LINE_RE.split(raw)]
    paragraphs: list[str] = []
    for block in blocks:
        if not block:
            continue
        # 段内换行统一成 \n（CRLF → LF），段文本本身不改字
        paragraphs.append(block.replace("\r\n", "\n").replace("\r", "\n"))
    return paragraphs


def _write_bytes(path: Path, payload: bytes) -> Path:
    """按字节原子写入（保证 ``source.md`` 与源文件逐字节一致）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path


@dataclass
class IngestResult:
    """摄取结果：元数据 + 与上一次摄取的锚点对照提示。"""

    source_id: str
    doc_dir: Path
    meta: dict[str, Any]
    previous_present: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def paragraphs(self) -> list[dict[str, Any]]:
        return list(self.meta.get("paragraphs") or [])


def anchor_diff_warnings(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[str]:
    """重摄取后对照旧锚点：id 增删 / text_hash 变化都点名（DESIGN §6 需重新校准）。"""
    if not previous:
        return []
    prev_paras = {p.get("id"): p for p in (previous.get("paragraphs") or [])}
    cur_paras = {p.get("id"): p for p in (current.get("paragraphs") or [])}
    warnings: list[str] = []

    removed = sorted(i for i in prev_paras if i not in cur_paras)
    added = sorted(i for i in cur_paras if i not in prev_paras)
    changed = sorted(
        i
        for i in cur_paras
        if i in prev_paras and prev_paras[i].get("text_hash") != cur_paras[i].get("text_hash")
    )
    if removed:
        warnings.append(f"段落消失：{', '.join(removed)}")
    if added:
        warnings.append(f"新增段落：{', '.join(added)}")
    if changed:
        warnings.append(f"段落文本变化（text_hash 不同）：{', '.join(changed)}")
    if warnings:
        warnings.append(
            "材料已变：历史画像与行为事件按段 id 对齐，请重新校准（不要沿用旧结论）"
        )
    return warnings


def ingest(
    path: Path | str,
    source_id: str,
    data_dir: Path | str,
    *,
    at: str | None = None,
) -> IngestResult:
    """摄取一个本地 UTF-8 文本文件，写 ``data/<source_id>/{source.md,source.meta.json}``。"""
    src = Path(path)
    if not src.is_file():
        raise InputError(f"输入文件不存在：{src}")
    raw_bytes = src.read_bytes()
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputError(f"只支持 UTF-8 文本（本入口不做编码猜测）：{src}（{exc.reason}）") from exc

    paragraphs = split_paragraphs(text)
    if not paragraphs:
        raise InputError(f"没有可摄取的正文段落（整篇都是标题/引用/空行？）：{src}")

    target = doc_dir(data_dir, source_id)
    previous = read_json_if_exists(target / "source.meta.json")

    meta: dict[str, Any] = {
        "source_id": source_id,
        "ingest": {
            "kind": "local_file",
            "path": str(src.resolve()),
            "hash": "sha256:" + hashlib.sha256(raw_bytes).hexdigest(),
            "at": at or now_iso(),
        },
        "paragraphs": [
            {
                "id": paragraph_id(i),
                "index": i,
                "text": block,
                "text_hash": text_hash(block),
            }
            for i, block in enumerate(paragraphs, start=1)
        ],
    }

    _write_bytes(target / "source.md", raw_bytes)
    write_json(target / "source.meta.json", meta)

    return IngestResult(
        source_id=source_id,
        doc_dir=target,
        meta=meta,
        previous_present=previous is not None,
        warnings=anchor_diff_warnings(previous, meta),
    )
