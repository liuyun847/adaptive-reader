"""锚点校验（M1 的检查入口）：``source.meta.json`` 是否与 ``source.md`` 仍然对齐。

用途有两个：

1. ``ar check`` 的锚点稳定性部分：材料被改动、或被手工编辑过元数据时**报错并点名段 id**，
   避免"看起来正常、实际锚点已经对不上"的静默失灵（这正是 ``学习/_整理`` 栽过两次的事故类型）。
2. 重摄取前后对照：见 ``ingest.anchor_diff_warnings``。

问题一律以「段 id + 具体差异」的形式返回，交给 CLI 打印并以退出码 4 结束。

**空文档不算通过**：``source.meta.json`` 里 0 段（0 个可校验的锚点）= 检查器空跑，
同样返回问题（口径与 ``analyze.validate_inventory`` 把空数组当失败一致）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .ingest import paragraph_id, split_paragraphs, text_hash, normalize_text
from .jsonio import read_json_if_exists
from .paths import doc_dir

__all__ = ["verify_source"]


def verify_source(data_dir: Path | str, source_id: str) -> list[str]:
    """校验一份摄取产物；返回问题清单（空列表 = 通过）。"""
    target = doc_dir(data_dir, source_id)
    meta_path = target / "source.meta.json"
    source_path = target / "source.md"

    if not meta_path.is_file():
        return [f"{meta_path} 不存在：请先运行 `ar ingest <文件> --id {source_id}`"]
    if not source_path.is_file():
        return [f"{source_path} 不存在：摄取产物不完整，请重新摄取"]

    meta = read_json_if_exists(meta_path) or {}
    raw = source_path.read_bytes()
    recorded_paras = list(meta.get("paragraphs") or [])
    problems: list[str] = []

    if not recorded_paras:
        # 空文档 / 段落表为空：一个锚点都没比，等于检查器空跑——0 比对项不算通过
        # （与 ``analyze.validate_inventory`` 把「空数组」当失败同一口径，DESIGN §6）。
        problems.append(
            "source.meta.json 里 0 段（source.md 里也没有可切分的块）：没有任何锚点可比对，"
            "检查器空跑——空文档不算通过"
        )

    recorded_hash = (meta.get("ingest") or {}).get("hash")
    actual_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
    if recorded_hash != actual_hash:
        problems.append(
            f"source.md 与 meta 记录的来源哈希不一致（meta={recorded_hash} 实际={actual_hash}）："
            "源文本被改动过，全部锚点不可信"
        )

    if meta.get("source_id") != source_id:
        problems.append(f"meta.source_id = {meta.get('source_id')!r}，与目录名 {source_id!r} 不一致")

    try:
        recomputed = split_paragraphs(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        return problems + [f"source.md 不是合法 UTF-8 文本：{exc.reason}"]

    if len(recomputed) != len(recorded_paras):
        problems.append(
            f"段落数不一致：meta 记 {len(recorded_paras)} 段，按切分规则重算 {len(recomputed)} 段"
        )

    for i, block in enumerate(recomputed, start=1):
        expected_id = paragraph_id(i)
        if i > len(recorded_paras):
            problems.append(f"{expected_id}: meta 里缺这一段（重算文本：{block[:20]}…）")
            continue
        rec = recorded_paras[i - 1]
        pid = rec.get("id") or expected_id
        if rec.get("id") != expected_id:
            problems.append(f"{pid}: 段号与顺序不一致（第 {i} 段应为 {expected_id}）")
        if rec.get("index") != i:
            problems.append(f"{pid}: index={rec.get('index')!r}，应为 {i}")
        actual_th = text_hash(block)
        if rec.get("text_hash") != actual_th:
            problems.append(
                f"{pid}: text_hash 不一致（meta={rec.get('text_hash')!r}，按 source.md 重算={actual_th}）："
                "该段文本或元数据已被改动"
            )
        if normalize_text(str(rec.get("text") or "")) != normalize_text(block):
            problems.append(f"{pid}: text 字段与 source.md 该段不一致")

    for rec in recorded_paras[len(recomputed) :]:
        problems.append(f"{rec.get('id')!r}: meta 里有这一段，但按切分规则已不存在")

    return problems
