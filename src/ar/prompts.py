"""提示词与版本号。

**回归基线**（DESIGN §7）：产物元数据里记录 ``model`` 与提示词版本，
同一输入两次生成的差异才是可度量的。改提示词时必须同时升版本号。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CONCEPTS_PROMPT_VERSION",
    "EXPLAIN_PROMPT_VERSION",
    "REWRITE_PROMPT_VERSION",
    "CONCEPTS_SYSTEM_PROMPT",
    "build_concepts_messages",
    "EXPLAIN_SYSTEM_PROMPT",
    "EXPLAIN_KINDS",
    "build_explain_messages",
    "REWRITE_SYSTEM_PROMPT",
    "REWRITE_ACTIONS",
    "build_rewrite_messages",
]

CONCEPTS_PROMPT_VERSION = "concepts-v1"
EXPLAIN_PROMPT_VERSION = "explain-v1"
REWRITE_PROMPT_VERSION = "rewrite-v1"

CONCEPTS_SYSTEM_PROMPT = (
    "你是中文材料的概念分析器。你读一份材料，列出读者理解它需要掌握的概念（尤其是容易卡住的地方）、"
    "概念之间的前置关系，以及值得配图的位置。只输出一个 JSON 对象：不要解释、不要 Markdown 代码围栏、"
    "不要在 JSON 前后加任何文字。"
)

CONCEPTS_USER_TEMPLATE = """材料（每段前面是段落 id，引用时必须原样使用这些 id）：

{paragraph_block}

请输出这样的 JSON：
{{"concepts":[{{"id":"c01","name":"概念名","surface":["原文里的原词"],"prereq":[],"difficulty_hint":3,"paragraphs":["p001"],"domain":"领域","concept_key":"english_key"}}],"illustration_candidates":[{{"paragraph":"p001","kind":"流程","why":"为什么图比文字省力"}}]}}

规则：
1. concepts 给 5–20 条，按在材料里出现的先后排序；id 必须是 c01、c02… 这种形式。
2. surface 是该概念在材料里出现过的原词，1–4 个，必须逐字来自材料。
3. prereq 是理解本概念前需要先掌握的概念，只能填本次输出里出现过的 concept id；没有就给 []。
4. difficulty_hint 是 1–5 的整数（对没有专业背景的成年读者而言的难度）。
5. paragraphs 只能从上面给出的段落 id 里选，至少一个。
6. domain 是领域短语（如「政治经济学」）；concept_key 是稳定的英文短标识（如 socially_necessary_labor_time），
   二期用于跨材料合并，同一概念在不同材料里必须一致。
7. illustration_candidates 只在「图比文字省力」时给：kind 只能是 流程 / 结构 / 对比 / 数量 之一，
   why 用一句话说明为什么图更好。宁可少给。
8. 拿不准的概念不要列：宁少勿滥。"""

EXPLAIN_SYSTEM_PROMPT = (
    "你是中文阅读助手。读者正在读一段材料，点开了这一段。请针对这一段给出他需要的那一种讲解。"
    "只讲这一段涉及的内容，不要复述整篇文章，不要输出 Markdown 标题，直接给正文（可含短列表）。"
)

EXPLAIN_KINDS: dict[str, str] = {
    "explain": "把这个段落讲清楚：先说它在说什么，再解释里面出现的术语与隐含前提。",
    "simpler": "用更简单的话重讲这一段：短句、少术语（术语必须出现时顺手解释）、保留原来的事实与数字。",
    "detail": "把这一段讲深一点：补充它省略的推理步骤、它的前提与它可能引出的疑问。",
}


def build_concepts_messages(meta: dict[str, Any]) -> list[dict[str, str]]:
    """把段落表拼成提示词（段落 id 在前，供模型引用）。"""
    lines = [f"[{p.get('id')}] {p.get('text', '')}" for p in (meta.get("paragraphs") or [])]
    return [
        {"role": "system", "content": CONCEPTS_SYSTEM_PROMPT},
        {"role": "user", "content": CONCEPTS_USER_TEMPLATE.format(paragraph_block="\n\n".join(lines))},
    ]


# --------------------------------------------------------------- 逐段决策与改写 --
# 骨架只实现 expand / keep（契约 §2.4）；compress / illustrate 留枚举位：
# 提示词里明确禁止输出这两个动作，模型若仍输出，``ar.rewrite`` 按 keep 处理并标 ``deferred``。
REWRITE_ACTIONS = ("expand", "keep", "compress", "illustrate")

REWRITE_SYSTEM_PROMPT = (
    "你是中文材料的分段改写引擎：按给定的读者画像与概念清单，对【原文段落】逐段决定动作并产出改写稿。"
    "你对原文的唯一义务是事实保真：原文里的数字、引号引文、书名号《》里的书名，必须逐字照抄，"
    "连注文（[注：…]、[数字]）里的数字与引文也不例外；只允许改叙述方式，不允许改动事实、不允许补充原文没有的事实。"
    "只输出一个 JSON 对象：不要解释、不要 Markdown 代码围栏、不要在 JSON 前后加任何文字。"
)

REWRITE_USER_TEMPLATE = """读者画像：
{profile_block}

概念清单：
{concept_block}

材料：《{source_id}》，共 {total} 段，本次给你第 {start}–{end} 段。

原文段落（每段以 [段 id] 开头，引用时必须原样使用这些 id）：
{paragraph_block}

请输出这样的 JSON：
{{"decisions":[{{"paragraph":"p001","action":"keep","why":{{"profile":[],"note":"一句话说明为什么这样处理"}},"confidence":0.9,"rewrite":""}},{{"paragraph":"p002","action":"expand","why":{{"profile":["c07:unknown"],"note":"必须先讲清前置的 c06，否则读者不知道价值从哪来"}},"confidence":0.8,"rewrite":"改写后的完整段落"}}]}}

规则：
1. 上面给的每一段都必须出现在 decisions 里，且段落顺序与原文一致；不许新增段落、不许合并段落、不许遗漏段落。
2. action 只能是 expand 或 keep：
   - expand：画像里有该段涉及的概念属于 unknown / partial，或前置概念未掌握时，把这层意思补出来（可以加一两句讲解）；
   - keep：其余情况一律保住原文（**宁少勿多**：拿不准就 keep）。compress / illustrate 本期不实现，不要输出。
3. 只有 action=expand 时才填 rewrite，且必须是该段改写后的完整正文：**先原样抄下整段原文，再补你要补的话**，
   不许改写、缩写、概括或漏掉原文的任何一句（输出被截断就是失败），最后一段话要以句号 / 问号 / 感叹号收尾。
4. **事实保真（红线）**：原文里出现的数字（含年份、页码、分数、比例）、引号引文、书名号《》里的书名，
   必须逐字照抄、一个字符都不许改，**而且在同一段里只能各出现原文那么多次**
   （不许重复引用同一句引文、不许把原文没加引号的词加上引号）。
   注文 [注：…] 整块照抄，[26]、[27] 这类注释编号也照抄。
   keep 的段落请**原样**输出，不要顺手改标点、加空格或整理格式。
5. 读者已知（known）的概念不要再解释；依据必须来自上面给的概念清单与读者画像，不要凭空推断读者水平。
6. confidence 是你对「这条决策合适」的把握（0–1 的小数）。
7. 不要写「改写说明」「导读」之类的额外段落；只输出 JSON。"""


def _format_paragraph_block(paragraphs: list[dict[str, Any]]) -> str:
    """段落拼块：``[p012] 正文``，段间空行（与摄取切段规则同形）。"""
    return "\n\n".join(f"[{p.get('id')}] {p.get('text', '')}" for p in paragraphs)


def build_rewrite_messages(
    source_id: str,
    paragraphs: list[dict[str, Any]],
    *,
    profile_block: str,
    concept_block: str,
    start: int,
    end: int,
    total: int,
) -> list[dict[str, str]]:
    """一段提示词吃一批段落（省 token：25 段合并进 4 次请求，见 W3 轮次说明）。

    ``start`` / ``end`` 是这批段落在全篇里的序号（1 起，含两端），只用于让模型知道自己看的是哪一段。
    """
    return [
        {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": REWRITE_USER_TEMPLATE.format(
                profile_block=profile_block,
                concept_block=concept_block,
                source_id=source_id,
                total=total,
                start=start,
                end=end,
                paragraph_block=_format_paragraph_block(paragraphs),
            ),
        },
    ]


def build_explain_messages(
    *,
    kind: str,
    paragraph_id: str,
    paragraph_text: str,
    neighbors: list[tuple[str, str]] | None = None,
    profile_hint: str | None = None,
) -> list[dict[str, str]]:
    """讲解卡片的提示词：本段原文 + 少量上下文 + 读者画像提示。"""
    parts = [f"段落 id：{paragraph_id}", "", "这一段原文：", paragraph_text]
    if neighbors:
        parts += ["", "前后段（仅供定位，不要讲解它们）："]
        parts += [f"[{nid}] {text[:200]}" for nid, text in neighbors]
    parts += ["", "讲解方式：" + EXPLAIN_KINDS.get(kind, EXPLAIN_KINDS["explain"])]
    if profile_hint:
        parts += ["", "读者画像提示（可信度不高，只作参考）：", profile_hint]
    return [
        {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]
