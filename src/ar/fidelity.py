"""事实锚校验（M3，契约 §2.5 / DESIGN §2.4 与 §6）：数字 / 引号引文 / 书名号 + 段缺失。

**这是自由重述唯一的自动保真底线**：改写可以换叙述方式，但原文里的数字、引号引文、
书名号《》里的书名必须逐字一致。做法是三样事实各自抽成**多重集**，逐段比对：

- 数字：正则 ``\\d+(?:,\\d{3})*(?:[．.]\\d+)?``（与 ``学习/_整理/spec_check.py`` 同口径），
  先做全角数字与全角句点归一，再抽；
- 引号引文：成对的 ``“…”`` / ``「…」``（``„…“`` 也认），内容归一空白；单字引号
  （如「《物》」这种加引号的单词）不算引文，否则噪声太大；
- 书名号：``《…》``（嵌套的 ``[注：…]`` 里也认；《` `` 《…》 `` `》这种外层包着完整书名的
  Markdown 形式剥掉一层反引号再比）。

比对口径是**多重集**而不是「包含」：同一段里数字少一个、多一个、位置换了都算不一致——
只查「有没有出现过」会漏掉「把 619811 改成 619812 又保留原数字」这类改动
（``学习/_整理/selfcheck.py`` 的教训：检查器失灵时永远报 0 问题）。

段缺失检查：改写稿按空行切分后的块数必须与 ``source.meta.json`` 的段数一致，逐段对齐；
对不上的段一律点名段 id。违规由 ``ar check`` 收集后**非零退出**（退出码 4）。

**0 比对项 = 检查器空跑 = 失败**：空文档会让逐段比对空转、什么问题都不报（「最干净的报告」
恰恰是最没跑过的那种）。所以 ``compare_blocks`` 在真正进入比对的段数为 0 时主动产出一条
``kind="empty_check"`` 的违规；调用方（``rewrite.check_fidelity``）据此把 ``fidelity.checked``
保持为 ``false``。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = [
    "NUMBER_PATTERN",
    "QUOTE_PAIRS",
    "BOOK_PATTERN",
    "MIN_QUOTE_CHARS",
    "MAX_QUOTE_CHARS",
    "ParagraphFacts",
    "extract_numbers",
    "extract_quotes",
    "extract_books",
    "facts_of",
    "split_rewritten_blocks",
    "same_facts",
    "compare_blocks",
    "render_fact_report",
]

# 与 ``学习/_整理/spec_check.py`` 同口径（含全角句点小数与千分位）
NUMBER_PATTERN = r"\d+(?:,\d{3})*(?:[．.]\d+)?"
# 成对引号：中文双引号 / 日式直角引号 / 德式低引号（成对才认，落单的不算引文）
QUOTE_PAIRS = (("\u201c", "\u201d"), ("\u300c", "\u300d"), ("\u201e", "\u201c"))
BOOK_PATTERN = r"《([^《》]{1,40})》"

MIN_QUOTE_CHARS = 2
MAX_QUOTE_CHARS = 60

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_FULLWIDTH_PUNCT = str.maketrans("．，", ".,")  # 全角句点 / 逗号：数字里的千分位与小数点
_WHITESPACE_RE = re.compile(r"\s+")
_BLANK_LINE_RE = re.compile(r"\n[ \t\r]*\n")

def _normalize(match: str) -> str:
    """引文 / 书名的比较用规范化：全角数字转半角 + 合并空白 + 去首尾空白。"""
    return _WHITESPACE_RE.sub(" ", match.translate(_FULLWIDTH_DIGITS)).strip()


def extract_numbers(text: str) -> list[str]:
    """抽数字串（全角数字与全角句点 / 逗号先归一），保序、保留重复。"""
    normalized = (text or "").translate(_FULLWIDTH_DIGITS).translate(_FULLWIDTH_PUNCT)
    return re.findall(NUMBER_PATTERN, normalized)


def extract_quotes(text: str) -> list[str]:
    """抽成对引号里的引文（``“…”`` / ``「…」``），按出现位置排序、保留重复。"""
    text = text or ""
    hits: list[tuple[int, str]] = []
    for opening, closing in QUOTE_PAIRS:
        pattern = re.compile(re.escape(opening) + r"([^" + re.escape(opening + closing) + r"]*)" + re.escape(closing))
        for match in pattern.finditer(text):
            content = _normalize(match.group(1))
            if MIN_QUOTE_CHARS <= len(content) <= MAX_QUOTE_CHARS:
                hits.append((match.start(), content))
    hits.sort(key=lambda item: item[0])
    return [content for _position, content in hits]


def extract_books(text: str) -> list[str]:
    """抽书名号 ``《…》`` 里的书名（先剥掉包住整个书名的反引号包装）。"""
    cleaned = re.sub(r"`\s*(《[^《》]{1,40}》)\s*`", r"\1", text or "")
    return [_normalize(match) for match in re.findall(BOOK_PATTERN, cleaned)]


@dataclass
class ParagraphFacts:
    """一段文本里的三类事实锚（保序，保留重复）。"""

    numbers: list[str] = field(default_factory=list)
    quotes: list[str] = field(default_factory=list)
    books: list[str] = field(default_factory=list)

    def all(self) -> list[str]:
        """三类锚点拼成一个列表（保序：数字 → 引文 → 书名）。"""
        return [*self.numbers, *self.quotes, *self.books]


def facts_of(text: str) -> ParagraphFacts:
    """抽一段文本的三类事实锚。"""
    return ParagraphFacts(
        numbers=extract_numbers(text),
        quotes=extract_quotes(text),
        books=extract_books(text),
    )


def split_rewritten_blocks(markdown: str) -> list[str]:
    """按空行切改写稿（与摄取切段、前端 ``splitRewritten`` 同一规则）。"""
    return [block.strip() for block in _BLANK_LINE_RE.split(markdown or "") if block.strip()]


def _diff_multiset(expected: Iterable[str], actual: Iterable[str]) -> tuple[list[str], list[str]]:
    """多重集差：返回（原文有而改写稿没有的，改写稿多出来的），都去重后排序。"""
    expected_counter, actual_counter = Counter(expected), Counter(actual)
    missing = sorted((expected_counter - actual_counter).elements())
    extra = sorted((actual_counter - expected_counter).elements())
    return missing, extra


def same_facts(expected: list[str], actual: list[str], *, ordered: bool = True) -> bool:
    """两类事实是否一致。

    - ``ordered=True``（默认，按段比对用）：数量相同、且三类锚点**出现的先后顺序也不许变**。
      原因与 DESIGN §2.4 同源：错位到别的段落之外，事实锚是唯一能自动证明保真的东西；
      而「把 27 页挪到 1696 年前面」这种改动本身也可能改意思——宁可让人工复核。
    - ``ordered=False``（只比种类与个数，不看顺序）：留作宽松口径的落点，目前没有调用方。
    """
    if len(expected) != len(actual):
        return False
    if not ordered:
        return Counter(expected) == Counter(actual)
    seen: set[str] = set()
    expected_rank = [item for item in expected if not (item in seen or seen.add(item))]
    seen.clear()
    actual_rank = [item for item in actual if not (item in seen or seen.add(item))]
    return expected_rank == actual_rank


def _preview(items: list[str], limit: int = 3) -> str:
    shown = "、".join(f"「{item}」" for item in items[:limit])
    return shown + (f" 等 {len(items)} 项" if len(items) > limit else "")


@dataclass
class BlockMismatch:
    """一段的三类事实锚比对结果（``paragraph`` 为 ``None`` 表示整段缺失 / 多余）。"""

    paragraph: str | None
    message: str


def compare_blocks(
    source_paragraphs: list[dict[str, Any]],
    rewritten_blocks: list[str],
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    """逐段比对事实锚。

    返回 ``(problems, mismatches, counts)``：

    - ``problems``：给人看的问题清单（每条都点名段 id，可直接进 ``ValidationFailed``）；
    - ``mismatches``：机器读的结构化清单，落 ``changes.json`` 的 ``fidelity.mismatches``；
    - ``counts``：本次实际比了多少项（``compared`` = 真正进入比对的段数，``paragraphs`` = 原文段数；
      数字 / 引文 / 书名同理），写进 check 的输出，证明校验真跑了。

    **0 比对项 = 检查器空跑 = 失败**：一篇空文档（原文 0 段、改写稿 0 块）会让这个循环空转、
    什么问题都不报——看起来「全部一致」，其实什么都没查。所以 ``compared == 0`` 时这里
    主动产出一条 ``kind="empty_check"`` 的违规（调用方据此判失败、并把 ``checked`` 保持为 false）。
    """
    problems: list[str] = []
    mismatches: list[dict[str, Any]] = []
    counts = {"paragraphs": len(source_paragraphs), "compared": 0, "numbers": 0, "quotes": 0, "books": 0}

    if len(rewritten_blocks) != len(source_paragraphs):
        problems.append(
            f"改写稿有 {len(rewritten_blocks)} 段，原文有 {len(source_paragraphs)} 段：段数对不上，无法逐段比对"
        )
        mismatches.append(
            {
                "paragraph": None,
                "kind": "paragraph_missing",
                "source": [],
                "rewritten": [],
                "detail": f"原文 {len(source_paragraphs)} 段 / 改写稿 {len(rewritten_blocks)} 段",
            }
        )

    paired = list(zip(source_paragraphs, rewritten_blocks))
    counts["compared"] = len(paired)
    if not paired:
        # 0 比对项：检查器空跑（不能因为「没报错」就当通过）
        detail = f"原文 {len(source_paragraphs)} 段 / 改写稿 {len(rewritten_blocks)} 段，真正进入比对的 0 段"
        problems.append(f"检查器空跑：{detail}——0 比对项不算通过")
        mismatches.append(
            {
                "paragraph": None,
                "kind": "empty_check",
                "source": [],
                "rewritten": [],
                "detail": detail,
            }
        )
    if len(rewritten_blocks) < len(source_paragraphs):
        for paragraph in source_paragraphs[len(rewritten_blocks):]:
            pid = str(paragraph.get("id"))
            problems.append(f"{pid}：改写稿里没有对应的段落（改写稿只有 {len(rewritten_blocks)} 段）")
            mismatches.append(
                {"paragraph": pid, "kind": "paragraph_missing", "source": [], "rewritten": [], "detail": "整段缺失"}
            )
    elif len(rewritten_blocks) > len(source_paragraphs):
        for position in range(len(source_paragraphs), len(rewritten_blocks)):
            problems.append(f"改写稿第 {position + 1} 段没有对应的原文段落（多出来的段落）：{rewritten_blocks[position][:30]}…")
            mismatches.append(
                {
                    "paragraph": None,
                    "kind": "paragraph_extra",
                    "source": [],
                    "rewritten": [],
                    "detail": f"改写稿第 {position + 1} 段多余",
                }
            )

    for paragraph, block in paired:
        pid = str(paragraph.get("id"))
        expected = facts_of(str(paragraph.get("text") or ""))
        actual = facts_of(block)
        counts["numbers"] += len(expected.numbers)
        counts["quotes"] += len(expected.quotes)
        counts["books"] += len(expected.books)

        for field_name, label in (("numbers", "数字"), ("quotes", "引号引文"), ("books", "书名号书名")):
            want: list[str] = getattr(expected, field_name)
            got: list[str] = getattr(actual, field_name)
            if same_facts(want, got):
                continue
            missing, extra = _diff_multiset(want, got)
            detail_parts: list[str] = []
            if missing:
                detail_parts.append(f"改写稿里少了 {_preview(missing)}")
            if extra:
                detail_parts.append(f"改写稿里多出 {_preview(extra)}")
            if not detail_parts:
                detail_parts.append("数量相同但出现顺序变了")
            problems.append(f"{pid}：{label}与原文不一致（{'；'.join(detail_parts)}）")
            mismatches.append(
                {
                    "paragraph": pid,
                    "kind": field_name,
                    "source": want,
                    "rewritten": got,
                    "missing": missing,
                    "extra": extra,
                    "detail": f"{label}与原文不一致",
                }
            )
    return problems, mismatches, counts


def render_fact_report(counts: dict[str, int]) -> str:
    """一句话说明这次校验实际覆盖了多少锚点（省得「检查器没跑」看不出来）。"""
    compared = int(counts.get("compared", counts.get("paragraphs", 0)) or 0)
    if compared <= 0:
        return "检查器空跑：0 比对项（没有任何段落真正进入事实锚比对）"
    total = int(counts.get("paragraphs", compared) or 0)
    scope = f"逐段比对 {compared} 段" + (f"（原文共 {total} 段）" if total != compared else "")
    return (
        f"{scope}："
        f"数字 {counts.get('numbers', 0)} 项 / 引号引文 {counts.get('quotes', 0)} 项 / "
        f"书名号 {counts.get('books', 0)} 项"
    )
