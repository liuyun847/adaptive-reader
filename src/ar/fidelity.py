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

**申报与口径（2026-10-14，PLAN-actions-feedback §14.1）**：严格比对仍然不许悄悄改坏，但两类需求
是正当的——「举例数据只为给出定性结论」「引文是噪音、改成想表达的意思」。所以松绑成
**可申报 + 可见**：

- 模型在 ``changes[i].fact_declarations`` 里**逐条申报**本段没有原样保留的事实锚
  （``{"numbers": [...], "quotes": [...], "books": [...], "note": "..."}``，没有就整个键不出现）；
- **口径由读者画像里那条偏好决定**（``ar.profile`` 的 ``pref_fact_anchor``，取值见
  :data:`FACT_ANCHOR_POLICIES`）：``keep_all``（严，申报了也不放行）/
  ``allow_declared_drops``（松，**已申报**的缺失放行，未申报的照旧算违规）；
- **申报只覆盖「少了什么」**：多出来的事实锚（换成别的数字 / 编一条引文）与「数量相同但顺序变了」
  任何口径下都不放行——那才是真正的事实漂移；
- 放行的每一项都进 ``fidelity.waived``（机器可读）与 CLI 输出，绝不静默。
- **多申报也不静默**（2026-10-14 收尾，PLAN-actions-feedback §14.1）：段落锚点一个没少（原样照抄）、
  模型却申报「放弃了某项」时，放行结论与退出码**都不变**（本来就没有缺失可放行），但每一条都进
  ``fidelity.over_declared``（``{paragraph,kind,declared,detail}``）并由 CLI 逐条打印——
  「申报与实测对不上」是模型的账，读者要看得见。
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
    "DECLARATION_FIELD",
    "DECLARABLE_FIELDS",
    "FACT_ANCHOR_POLICIES",
    "DEFAULT_FACT_ANCHOR_POLICY",
    "MAX_DECLARATION_NOTE_CHARS",
    "ParagraphFacts",
    "FactComparison",
    "extract_numbers",
    "extract_quotes",
    "extract_books",
    "facts_of",
    "split_rewritten_blocks",
    "same_facts",
    "parse_declarations",
    "normalize_fact_anchor_policy",
    "compare_blocks",
    "compare_blocks_detailed",
    "render_fact_report",
]

# 与 ``学习/_整理/spec_check.py`` 同口径（含全角句点小数与千分位）
NUMBER_PATTERN = r"\d+(?:,\d{3})*(?:[．.]\d+)?"
# 成对引号：中文双引号 / 日式直角引号 / 德式低引号（成对才认，落单的不算引文）
QUOTE_PAIRS = (("\u201c", "\u201d"), ("\u300c", "\u300d"), ("\u201e", "\u201c"))
BOOK_PATTERN = r"《([^《》]{1,40})》"

MIN_QUOTE_CHARS = 2
MAX_QUOTE_CHARS = 60

# ------------------------------------------------ 事实锚申报与校验口径（§14.1） --
#: ``changes[i]`` 里那格申报的字段名（模型逐段申报「本段没有原样保留哪几项事实锚」）。
#: **没有申报时整个键不出现**（照 ``figure`` / ``deferred`` 的「为真才出现」先例）。
DECLARATION_FIELD = "fact_declarations"
#: 可申报的三类事实锚（与 ``ParagraphFacts`` 的字段名一致）
DECLARABLE_FIELDS = ("numbers", "quotes", "books")
#: 校验口径 = 读者画像里 ``pref_fact_anchor`` 那条偏好的取值（**唯一来源在这里**，
#: ``ar.profile`` / ``ar.prompts`` / ``ar.rewrite`` 都从这里取，别各自写一份字面量）：
#: ``keep_all`` 严（申报了也不放行）/ ``allow_declared_drops`` 松（已申报的缺失放行）
FACT_ANCHOR_POLICIES = ("keep_all", "allow_declared_drops")
DEFAULT_FACT_ANCHOR_POLICY = "keep_all"
#: 申报里 ``note`` 的长度上限（防产物被长文本撑爆；超出截断并说明，不静默）
MAX_DECLARATION_NOTE_CHARS = 200

_FIELD_LABELS = {"numbers": "数字", "quotes": "引号引文", "books": "书名号书名"}

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


# ------------------------------------------------------ 申报的解析与口径归一 --
def _normalize_declared(field_name: str, value: str) -> str:
    """申报项按**抽取口径**归一，否则「模型抄对了原文」也会因全角数字 / 空白差异对不上。"""
    if field_name == "numbers":
        return value.translate(_FULLWIDTH_DIGITS).translate(_FULLWIDTH_PUNCT).strip()
    return _normalize(value)


def parse_declarations(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """把模型给的申报归一化成 ``{"numbers": [...], "quotes": [...], "books": [...], "note": "..."}``。

    返回 ``(申报, 问题清单)``：结构不对 / 非字符串条目 / 不认识的键**不静默丢**，
    一律写进问题清单（调用方记进 ``meta.degraded``）。
    归一化口径与 :func:`extract_numbers` / :func:`extract_quotes` / :func:`extract_books` 一致；
    保序、**保留重复**（多重集语义：申报两次就是两次）。``raw`` 为空 / 空字典 → ``({}, [])``。
    """
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, [f"{DECLARATION_FIELD} 不是对象（实际 {type(raw).__name__}）：按「没有申报」处理"]
    declared: dict[str, Any] = {}
    notes: list[str] = []
    for field_name in DECLARABLE_FIELDS:
        value = raw.get(field_name)
        if value is None:
            continue
        if isinstance(value, str):  # 模型偶尔只给一个字符串：收下（一项申报），不当成结构错误
            value = [value]
        if not isinstance(value, list):
            notes.append(
                f"{DECLARATION_FIELD}.{field_name} 不是数组（实际 {type(value).__name__}）：已忽略"
            )
            continue
        items: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                notes.append(f"{DECLARATION_FIELD}.{field_name} 里有非字符串 / 空白项：已忽略（申报不放行）")
                continue
            items.append(_normalize_declared(field_name, item))
        if items:
            declared[field_name] = items
    for key in raw:
        if key not in DECLARABLE_FIELDS and key != "note":
            notes.append(
                f"{DECLARATION_FIELD} 里有不认识的键 {key!r}：已忽略"
                f"（只认 {' / '.join(DECLARABLE_FIELDS)} 与 note）"
            )
    note = raw.get("note")
    if isinstance(note, str) and note.strip():
        text = note.strip()
        if len(text) > MAX_DECLARATION_NOTE_CHARS:
            notes.append(
                f"{DECLARATION_FIELD}.note 超过 {MAX_DECLARATION_NOTE_CHARS} 字（{len(text)} 字）：已截断"
            )
            text = text[:MAX_DECLARATION_NOTE_CHARS]
        declared["note"] = text
    return declared, notes


def normalize_fact_anchor_policy(value: Any) -> str:
    """口径取值归一：不认识 / 缺省一律按严口径 :data:`DEFAULT_FACT_ANCHOR_POLICY` 办。

    「不认识就退到最严的那一档」是刻意的：口径读坏了只会更严格，不会悄悄放宽事实锚。
    """
    text = str(value or "").strip()
    return text if text in FACT_ANCHOR_POLICIES else DEFAULT_FACT_ANCHOR_POLICY


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


@dataclass
class FactComparison:
    """一次逐段事实锚比对的完整结果（:func:`compare_blocks_detailed` 的返回）。

    - ``problems`` / ``mismatches`` / ``counts``：与 :func:`compare_blocks` 的三个返回值同义；
    - ``waived``：**已申报并被口径放行**的缺失（``{paragraph,kind,source,rewritten,missing,declared,unused,detail}``），
      落 ``changes.json`` 的 ``fidelity.waived``，也由 CLI 逐条打印——放行这件事必须看得见；
    - ``over_declared``：**申报与实测对不上**的条目（``{paragraph,kind,declared,detail}``）——
      这一段根本没丢这类锚点，模型却申报放弃了它。**不影响放行结论与退出码**（没有缺失可放行），
      但落 ``changes.json`` 的 ``fidelity.over_declared`` 并由 CLI 逐条打印，绝不静默；
    - ``declaration_notes``：申报本身读不通的地方（结构不对 / 值不认识），调用方记进 ``meta.degraded``；
    - ``policy``：本次实际采用的口径（:data:`FACT_ANCHOR_POLICIES` 之一，不认识的值退到严口径）。
    """

    problems: list[str] = field(default_factory=list)
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, Any] = field(default_factory=dict)
    waived: list[dict[str, Any]] = field(default_factory=list)
    over_declared: list[dict[str, Any]] = field(default_factory=list)
    declaration_notes: list[str] = field(default_factory=list)
    policy: str = DEFAULT_FACT_ANCHOR_POLICY


def _coverage(missing: list[str], declared: list[str]) -> tuple[list[str], list[str]]:
    """申报是否覆盖了全部缺失项（多重集包含）。

    返回 ``(没被申报覆盖的缺失项, 申报了但实测并不缺的项)``：前者非空就是**未申报的缺失**
    （照旧算违规），后者只作可见说明（申报与实测对不上，别让它悄悄过去）。
    """
    pool = Counter(declared)
    uncovered: list[str] = []
    for item in missing:
        if pool.get(item, 0) > 0:
            pool[item] -= 1
        else:
            uncovered.append(item)
    unused = [item for item, count in pool.items() for _ in range(max(0, count))]
    return uncovered, unused


def _declaration_suffix(
    *,
    policy: str,
    field_name: str,
    declared_items: list[str],
    missing: list[str],
    extra: list[str],
    uncovered: list[str],
) -> str:
    """违规文案里那段「申报了没有 / 为什么没放行」的说明（不申报时也写明「没申报」）。"""
    if declared_items and policy != "allow_declared_drops":
        return (
            f"；本段已申报放弃 {_preview(declared_items)}，但当前口径是 keep_all（要素材 / 全保留），"
            "申报不放行（要放行得改读者画像里的 pref_fact_anchor 偏好）"
        )
    if extra:
        return "；多出来的事实锚不在申报范围内（申报只覆盖「少了什么」，换成别的数字 / 编一条引文一律不放行）"
    if not missing:
        return "；数量相同但顺序变了：顺序调换不在申报范围内，不放行"
    if declared_items:
        return (
            f"；本段申报的是 {_preview(declared_items)}，与实测缺失（{_preview(uncovered)}）对不上，"
            "未申报的缺失不放行"
        )
    return "；本段没有申报这项缺失（没申报就按现在的规则报错）"


def compare_blocks_detailed(
    source_paragraphs: list[dict[str, Any]],
    rewritten_blocks: list[str],
    *,
    declarations: dict[str, Any] | None = None,
    policy: str = DEFAULT_FACT_ANCHOR_POLICY,
) -> FactComparison:
    """逐段比对事实锚，并**按口径处理已申报的缺失**（§14.1）。

    ``declarations`` 是 ``{段 id: 该段的申报原文}``（``changes[i].fact_declarations``，可以为缺）；
    ``policy`` 是本次校验采用的口径（不是 :data:`FACT_ANCHOR_POLICIES` 里的值时退到严口径）。

    判定逐条（**只放宽「少了什么」，其余一律不放行**）：

    1. 段数 / 段序对不上、段缺失 / 多余段：照旧报错（与口径无关）；
    2. 三类锚点各自比对：**多出来的**（换成别的数字、编一条引文）→ 报错，永远不放行；
    3. 数量相同但顺序变了 → 报错，永远不放行；
    4. 只有「少了」时：``policy == allow_declared_drops`` 且**申报覆盖了全部缺失项** → 放行，
       记一条 ``waived``；其余情况（含严口径下一律）报错，并在文案里说清「申报了但没放行 / 没申报」；
    5. **申报与实测对不上**（这一类锚点一个没少、却申报放弃了它；或放行时申报里混着几项本来就没丢的）
       → 记一条 ``over_declared``（**只作可见说明**：没有缺失可放行，所以放行结论与退出码都不变）。
    """
    effective = normalize_fact_anchor_policy(policy)
    problems: list[str] = []
    mismatches: list[dict[str, Any]] = []
    waived: list[dict[str, Any]] = []
    over_declared: list[dict[str, Any]] = []
    declaration_notes: list[str] = []
    counts: dict[str, Any] = {
        "paragraphs": len(source_paragraphs),
        "compared": 0,
        "numbers": 0,
        "quotes": 0,
        "books": 0,
    }

    declared_by_paragraph: dict[str, dict[str, Any]] = {}
    for pid, raw in (declarations or {}).items():
        parsed, notes = parse_declarations(raw)
        for note in notes:
            declaration_notes.append(f"{pid}：{note}")
        if parsed:
            declared_by_paragraph[str(pid)] = parsed

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
                "declared": [],
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
                "declared": [],
                "detail": detail,
            }
        )
    if len(rewritten_blocks) < len(source_paragraphs):
        for paragraph in source_paragraphs[len(rewritten_blocks):]:
            pid = str(paragraph.get("id"))
            problems.append(f"{pid}：改写稿里没有对应的段落（改写稿只有 {len(rewritten_blocks)} 段）")
            mismatches.append(
                {
                    "paragraph": pid,
                    "kind": "paragraph_missing",
                    "source": [],
                    "rewritten": [],
                    "declared": [],
                    "detail": "整段缺失",
                }
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
                    "declared": [],
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
        declared = declared_by_paragraph.get(pid) or {}
        declared_note = str(declared.get("note") or "")

        for field_name, label in (("numbers", "数字"), ("quotes", "引号引文"), ("books", "书名号书名")):
            want: list[str] = getattr(expected, field_name)
            got: list[str] = getattr(actual, field_name)
            declared_items = [item for item in (declared.get(field_name) or []) if isinstance(item, str)]
            if same_facts(want, got):
                # 这一类锚点一个没少（原样照抄）却申报「放弃了某项」：**放行结论与退出码都不变**
                # （本来就没有缺失可放行），但「申报与实测对不上」必须看得见，绝不静默
                # （2026-10-14 收尾：此前 same_facts 一命中就 continue，把申报整个跳过了）。
                if declared_items:
                    over_declared.append(
                        {
                            "paragraph": pid,
                            "kind": field_name,
                            "declared": declared_items,
                            "detail": (
                                f"{label}：本段一个都没少（与原文逐项一致），却申报放弃了 "
                                f"{_preview(declared_items)}——多申报，不影响放行结论"
                            ),
                        }
                    )
                continue
            missing, extra = _diff_multiset(want, got)
            uncovered, unused = _coverage(missing, declared_items)
            if effective == "allow_declared_drops" and missing and not extra and not uncovered:
                # 已申报的缺失：按读者口径放行（**只有这一类**放行），逐条留证
                detail = f"{label}：申报 {len(missing)} 项，按 {effective} 放行"
                if declared_note:
                    detail += f"（模型说明：{declared_note}）"
                waived.append(
                    {
                        "paragraph": pid,
                        "kind": field_name,
                        "source": want,
                        "rewritten": got,
                        "missing": missing,
                        "declared": declared_items,
                        "unused": unused,
                        "detail": detail,
                    }
                )
                if unused:
                    # 放行那一行里混着「本来就没丢」的申报项：放行照旧，但要把这笔账单独说出来
                    over_declared.append(
                        {
                            "paragraph": pid,
                            "kind": field_name,
                            "declared": unused,
                            "detail": (
                                f"{label}：申报里的 {_preview(unused)} 本段并没有丢"
                                "——多申报，不影响本次放行"
                            ),
                        }
                    )
                continue
            detail_parts: list[str] = []
            if missing:
                detail_parts.append(f"改写稿里少了 {_preview(missing)}")
            if extra:
                detail_parts.append(f"改写稿里多出 {_preview(extra)}")
            if not detail_parts:
                detail_parts.append("数量相同但出现顺序变了")
            suffix = _declaration_suffix(
                policy=effective,
                field_name=field_name,
                declared_items=declared_items,
                missing=missing,
                extra=extra,
                uncovered=uncovered,
            )
            problems.append(f"{pid}：{label}与原文不一致（{'；'.join(detail_parts)}{suffix}）")
            mismatches.append(
                {
                    "paragraph": pid,
                    "kind": field_name,
                    "source": want,
                    "rewritten": got,
                    "missing": missing,
                    "extra": extra,
                    "declared": declared_items,
                    "detail": f"{label}与原文不一致",
                }
            )
    return FactComparison(
        problems=problems,
        mismatches=mismatches,
        counts=counts,
        waived=waived,
        over_declared=over_declared,
        declaration_notes=declaration_notes,
        policy=effective,
    )


def compare_blocks(
    source_paragraphs: list[dict[str, Any]],
    rewritten_blocks: list[str],
    *,
    declarations: dict[str, Any] | None = None,
    policy: str = DEFAULT_FACT_ANCHOR_POLICY,
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    """逐段比对事实锚（**兼容包装**：老调用方照样拿三个返回值）。

    返回 ``(problems, mismatches, counts)``：

    - ``problems``：给人看的问题清单（每条都点名段 id，可直接进 ``ValidationFailed``）；
    - ``mismatches``：机器读的结构化清单，落 ``changes.json`` 的 ``fidelity.mismatches``；
    - ``counts``：本次实际比了多少项（``compared`` = 真正进入比对的段数，``paragraphs`` = 原文段数；
      数字 / 引文 / 书名同理），写进 check 的输出，证明校验真跑了。

    **申报与口径**（``declarations`` / ``policy``，§14.1）走 :func:`compare_blocks_detailed`：
    这个包装把它的 ``problems`` / ``mismatches`` / ``counts`` 原样交出去，
    ``waived``（已申报放行的条目）请直接用详细版取——``ar check`` 就是那么调的。

    **0 比对项 = 检查器空跑 = 失败**：一篇空文档（原文 0 段、改写稿 0 块）会让这个循环空转、
    什么问题都不报——看起来「全部一致」，其实什么都没查。所以 ``compared == 0`` 时这里
    主动产出一条 ``kind="empty_check"`` 的违规（调用方据此判失败、并把 ``checked`` 保持为 false）。
    """
    outcome = compare_blocks_detailed(
        source_paragraphs, rewritten_blocks, declarations=declarations, policy=policy
    )
    return outcome.problems, outcome.mismatches, outcome.counts


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
