"""提示词与版本号。

**回归基线**（DESIGN §7）：产物元数据里记录 ``model`` 与提示词版本，
同一输入两次生成的差异才是可度量的。改提示词时必须同时升版本号。

**前缀缓存纪律**（2026-10-08，``rewrite-v7`` 重排）：网关按「提示词从头开始逐字相同的部分」
算缓存命中，而同一轮里的多次请求（逐批改写 / 逐段改写 / 同一材料的重跑）只有**会变的那一小块**不同。
所以模板按同一条规矩排：**通用固定块**（system / 画像 / 读者背景 / 事实锚口径 / JSON 示例 / 规则）
排最前；**按材料变的块**（材料行 / 概念清单 / 配图候选 / 全文）排在规则之后；
**本次要处理的段**（段号 + 正文 + 校验反馈）永远压在 user 消息末尾。
- ``REWRITE_USER_TEMPLATE``（``rewrite-v7``，2026-10-18）：把概念清单 / 配图候选 / 材料行从固定块中间
  挪到规则之后，并新增 ``{full_text_block}``（全文进固定上下文，见 ``_full_text_section``）。
  同一读者换材料时，可共用的通用前缀从 513 字符涨到 3516 字符（1740 token；跨材料命中还要看网关门槛）；
  同一材料内，批间 / 段间共享前缀 = 通用块 + 材料块 + 全文（capital-v1-ch01 实测 9889 字符 / 5364 token），
  首个差异字符仍落在「本次给你第 N 段」的段号位。
  ``{feedback_block}`` 是逐段循环的校验回灌（空串 == 缺省 == 零字符插入），只影响尾巴。
  ``{plan_section}``（``plan-v1``，两遍式的第一遍计划）紧跟全文块之后、本次段号之前，
  同样是**空串零字符插入**（见 :func:`_plan_section`）——不带计划的调用方拿到的提示词逐字不变。
- ``PLAN_USER_TEMPLATE``（``plan-v2``，2026-10-31）：两遍式的**第一遍**——一次调用给全篇每段打动作标，
  **不写任何正文**。它由**同一段** ``REWRITE_USER_HEAD``（画像 / 背景 / 口径 / JSON 示例 / 规则 /
  材料行 / 概念 / 配图 / 全文）**加一条打标尾巴**拼成，system 也直接用改写那份：于是两遍之间共享
  9889 字符（5364 token）前缀，**第一遍建好的缓存能被第二遍第 1 批直接命中**（``plan-v1`` 时两遍的
  公共前缀是 0 字符）。打标指令与固定判别标记「【动作打标·第一遍】」全部**后置**在尾巴里
  ——第二遍那条尾巴是计划块 + 本次段，两份提示词的分岔点就在全文块之后。
- 演进：``v3`` 插 ``{background_block}``；``v4`` 加 ``{illustration_block}`` 与四动作规则；
  ``v5`` 加 ``{fact_anchor_block}`` 与规则 10；``v6`` 加规则 11（``why.profile`` 语义）；
  ``v7`` 重排 + 全文 + 校验反馈槽。每次改提示词都升版本号，**别拿旧版逐字对账**。
- ``CONCEPTS_USER_TEMPLATE``：跨材料共享前缀 152 → 878 字符；
- ``COMPRESS_USER_TEMPLATE``：固定块（画像 / **事实锚口径** / 概念清单 / 规则）在前、
  **要压缩的那段原文在末尾**；``compress-v2`` 起多一个 ``{fact_anchor_block}``
  （与 ``rewrite-v5`` 共用同一份口径文案，只是规则序号不同），依然逐字稳定；
- ``SURVEY_USER_TEMPLATE``：追问的固定块在前、读者的素描与作答在末尾。
模板里没有任何时间戳 / 随机量（``generated_at`` 只进产物，不进提示词）。
改模板时**不要**把会变的内容挪回前面，否则缓存命中率会掉回去。
"""

from __future__ import annotations

import json
from typing import Any

from .fidelity import DEFAULT_FACT_ANCHOR_POLICY
from .ingest import normalize_text

__all__ = [
    "CONCEPTS_PROMPT_VERSION",
    "EXPLAIN_PROMPT_VERSION",
    "REWRITE_PROMPT_VERSION",
    "PLAN_PROMPT_VERSION",
    "PLAN_USER_TAIL",
    "PLAN_USER_TEMPLATE",
    "build_plan_messages",
    "REWRITE_USER_HEAD",
    "REWRITE_USER_TAIL",
    "SURVEY_PROMPT_VERSION",
    "COMPRESS_PROMPT_VERSION",
    "ONESHOT_PROMPT_VERSION",
    "build_oneshot_messages",
    "CONCEPTS_SYSTEM_PROMPT",
    "build_concepts_messages",
    "EXPLAIN_SYSTEM_PROMPT",
    "EXPLAIN_KINDS",
    "SELECTION_MAX_CHARS",
    "SELECTION_SOURCES",
    "DEFAULT_SELECTION_FROM",
    "SELECTION_SOURCE_LABELS",
    "build_explain_messages",
    "REWRITE_SYSTEM_PROMPT",
    "REWRITE_ACTIONS",
    "FACT_ANCHOR_BLOCKS",
    "DEFAULT_FACT_ANCHOR_POLICY",
    "fact_anchor_block",
    "EMPTY_BACKGROUND_SKETCH",
    "EMPTY_CONCEPT_BLOCK",
    "EMPTY_PROFILE_BLOCK",
    "DEFAULT_COMPRESS_INSTRUCTION",
    "format_paragraph_block",
    "build_rewrite_messages",
    "COMPRESS_SYSTEM_PROMPT",
    "COMPRESS_USER_TEMPLATE",
    "build_compress_messages",
    "SURVEY_SYSTEM_PROMPT",
    "SURVEY_USER_TEMPLATE",
    "build_survey_messages",
]

# concepts-v2（2026-10-08）：规则尾移到「材料正文」之前——正文块挪到 user 末尾后，
# 跨材料逐字相同的前缀从 152 字符涨到 878 字符（提示词前缀缓存）。语义没变，但提示词变了，版本号跟着升。
CONCEPTS_PROMPT_VERSION = "concepts-v2"
# explain-v2（2026-10-08）：新增「选中片段」作用域（selection / selection_from），
# 讲解对象可能只是段里的一小段文字而不是整段——提示词语义变了，版本号必须跟着升。
EXPLAIN_PROMPT_VERSION = "explain-v2"
# rewrite-v4（2026-10-13）：规则 2 从「只能是 expand 或 keep」改成四个动作的判据，
# 删掉「compress / illustrate 本期不实现，不要输出」，补 compress 的约束、illustrate 的 figure 形状，
# 并在概念清单之后、材料正文之前插入**逐字稳定**的「配图候选」块（``{illustration_block}``）。
# 该块为空时（材料没有配图候选）**一个字符都不插**：空块 == 缺省，有块与无块的差异只在那一格
# （本批同时还改了规则 2/3/9 与 JSON 示例，所以**不是**「与 rewrite-v3 逐字相同」——
# 但首位差异字符仍落在段号位，批间共享前缀的性质不变，见 ``build_rewrite_messages``）。
# rewrite-v5（2026-10-14）：事实锚**可申报 + 可见**（PLAN-actions-feedback §14.1）——
# ① 新增规则 10：每段都要用 ``fact_declarations`` 逐条申报「本段没有原样保留哪几项事实锚」；
# ② 读者背景之后插入**逐字稳定**的「事实锚口径」块（``{fact_anchor_block}``，两档取值各一段固定文案，
#   见 ``FACT_ANCHOR_BLOCKS``）；③ JSON 示例里补上 ``fact_declarations`` 那一格。
# 于是提示词确实变了（版本号必须跟着升），但首位差异字符仍落在段号位。
# rewrite-v6（2026-10-14 复核修复）：``why.profile`` 的语义写进规则 11（**有自评就必须逐条保留
# 模型给的引用**，没有自评可引用时才写空数组 []），并且 ``ar.rewrite._format_profile_block`` 的
# 「空自评」分支不再说「画像为空 / 请只依据概念难度提示判断」——背景非空即算「有画像」
# （契约 §6.4），那句话只说明「读者没有提供概念自评」，与背景是两件事（旧措辞会把模型带进
# 「画像为空」而同一句又引用背景的自相矛盾，2026-10-13 真机产物里出现过 7 段）。
# rewrite-v7（2026-10-18）：重排 + 全文进固定上下文 + 校验反馈槽（逐段循环用）——
# ① 材料行 / 概念清单 / 配图候选从固定块中间挪到规则之后，通用前缀变长（同一读者换材料可共用）；
# ② 新增「原文（全文）」块（``{full_text_block}``），同一材料的所有批 / 所有段共享同一份全文；
# ③ 新增 ``{feedback_block}``（空串零字符插入，只在逐段循环校验失败重试时出现）；
# ④ 规则 1 / 2 / 5 的方位词改「下面」（材料块挪到规则之后），规则 11 去掉写在正文里的版本号。
# 提示词内容变了，版本号必须跟着升。
REWRITE_PROMPT_VERSION = "rewrite-v7"
# plan-v1（2026-10-19）：两遍式改写（``--plan-first``）的**第一遍**——一次调用给全篇每段打动作标，
# **不写任何正文**；第二遍仍走 ``rewrite-v7``，只是多一格 ``{plan_section}``（空串零字符插入）。
# 于是 **``rewrite-v7`` 不用升版**：不带计划的提示词逐字未变（有测试钉住），版本变化记在这一格上。
# plan-v2（2026-10-31）：**让两遍共享同一段前缀**（plan-v1 的公共前缀是 0 字符，打标那一遍拿不到缓存）——
# 第一遍改成「``REWRITE_USER_HEAD`` + 打标尾巴」、system 与改写那一遍逐字相同，两遍的差异推到全文块之后。
# 第二遍的提示词**一个字符都没动**（``rewrite-v7`` 因此仍不升版，有测试钉住）。
PLAN_PROMPT_VERSION = "plan-v2"
# survey-v1（2026-10-08）：读前背景问卷的「模型追问」提示词（3–5 个细节问题）。
SURVEY_PROMPT_VERSION = "survey-v1"
# compress-v1（2026-10-13）：单段压缩提示词（读中「太啰嗦」→ 当场压缩该段，见 PLAN §5）。
# 与另外四份一样：固定块在前、**要压缩的那段原文在末尾**（前缀缓存纪律）。
# compress-v2（2026-10-14）：在线压缩补上**事实锚申报通道**（PLAN-actions-feedback §14.1）——
# ① 读者画像之后插入**逐字稳定**的「事实锚口径」块（``{fact_anchor_block}``，``rule_no=6``，
#   与 ``rewrite-v5`` 共用同一份口径文案）；② 新增规则 6（逐条申报 ``fact_declarations``）；
# ③ JSON 示例里补上 ``fact_declarations`` 那一格。语义确实变了，版本号必须跟着升；
# 换的只是固定块与规则正文，**要压缩的那段原文仍在末尾**，前缀缓存的性质不变。
# compress-v3（2026-10-14 复核修复）：这份提示词和 ``rewrite`` **共用**
# ``ar.rewrite._format_profile_block``，那个函数的「空自评」分支措辞改了（不再说「画像为空」）
# ⇒ 提示词内容确实变了，按同一条纪律升版本号（不升就是「提示词变了、版本号还说没变」）。
COMPRESS_PROMPT_VERSION = "compress-v3"

CONCEPTS_SYSTEM_PROMPT = (
    "你是中文材料的概念分析器。你读一份材料，列出读者理解它需要掌握的概念（尤其是容易卡住的地方）、"
    "概念之间的前置关系，以及值得配图的位置。只输出一个 JSON 对象：不要解释、不要 Markdown 代码围栏、"
    "不要在 JSON 前后加任何文字。"
)

#: 固定块在前、**材料正文在最后**（前缀缓存：跨材料逐字相同的前缀 ~1000 字符）。
CONCEPTS_USER_TEMPLATE = """请输出这样的 JSON：
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
8. 拿不准的概念不要列：宁少勿滥。

材料（每段前面是段落 id，引用时必须原样使用这些 id）：

{paragraph_block}"""

EXPLAIN_SYSTEM_PROMPT = (
    "你是中文阅读助手。读者正在读一段材料，点开了这一段。请针对这一段给出他需要的那一种讲解。"
    "只讲这一段涉及的内容，不要复述整篇文章，不要输出 Markdown 标题，直接给正文（可含短列表）。"
    "如果读者另外给出了「选中的片段」，讲解的重点就是那段选中的文字：整段原文只作上下文，"
    "用来判断指代与前后文，不要把它整段复述或逐句讲解。"
)

EXPLAIN_KINDS: dict[str, str] = {
    "explain": "把这个段落讲清楚：先说它在说什么，再解释里面出现的术语与隐含前提。",
    "simpler": "用更简单的话重讲这一段：短句、少术语（术语必须出现时顺手解释）、保留原来的事实与数字。",
    "detail": "把这一段讲深一点：补充它省略的推理步骤、它的前提与它可能引出的疑问。",
}

# ---------------------------------------------------------------- 选中片段 --
# 读者在主栏改写稿或抽屉原文里划一段文字 → 只讲这段文字，段落仍然提供上下文。
# 上限 600 字符：超出就截断并**如实上报** ``selection_truncated``（降级必须可见，不许静默截断）。
SELECTION_MAX_CHARS = 600
SELECTION_SOURCES = ("rewritten", "source")
DEFAULT_SELECTION_FROM = "rewritten"
SELECTION_SOURCE_LABELS: dict[str, str] = {"rewritten": "改写稿", "source": "原文"}


def build_concepts_messages(meta: dict[str, Any]) -> list[dict[str, str]]:
    """把段落表拼成提示词（**正文块在最后**，段 id 供模型引用；规则与 JSON 示例在前做缓存前缀）。"""
    lines = [f"[{p.get('id')}] {p.get('text', '')}" for p in (meta.get("paragraphs") or [])]
    return [
        {"role": "system", "content": CONCEPTS_SYSTEM_PROMPT},
        {"role": "user", "content": CONCEPTS_USER_TEMPLATE.format(paragraph_block="\n\n".join(lines))},
    ]


# --------------------------------------------------------------- 逐段决策与改写 --
# 契约 §2.4 的四个动作；**四个都已实现**（2026-10-13 起：compress 真压缩、illustrate 出图定义）。
# 枚举**外**的动作（含模型没给 action）由 ``ar.rewrite`` 按 keep 处理，**不标 ``deferred``**
# ——``deferred`` 只表示「枚举内、本期未实现的动作」，现在没有这样的动作。
REWRITE_ACTIONS = ("expand", "keep", "compress", "illustrate")

#: 素描为空时的固定文案（与 ``ar.survey.EMPTY_BACKGROUND`` 逐字一致，有测试对账）。
#: **不带「读者背景：」前缀**：模板里那一行已经写了，素描再带一次会拼出两行同样的字。
#: 提示词模板必须逐字稳定，所以缺省值写死在这里，而不是让调用方自己拼。
EMPTY_BACKGROUND_SKETCH = "未提供"

# ------------------------------------------------- 事实锚口径（rewrite-v5 / compress-v2，§14.1） --
#: 读者背景之后的一格「事实锚口径」：**两档取值各一段固定文案**（逐字稳定 ⇒ 前缀缓存不受影响）。
#: 取值与 ``ar.fidelity.FACT_ANCHOR_POLICIES`` 一一对应（唯一来源在那边，这里只写提示词措辞）；
#: 口径进提示词，模型才知道这一轮能不能丢举例数据——但**不论哪一档都要逐条申报**。
#:
#: ⚠ 两段文案里的 ``{rule}`` 是**申报规则的序号占位符**：同一份口径文案被两份提示词共用
#: （``rewrite-v5`` 的规则 10 与 ``compress-v2`` 的规则 6），序号不能写死。
#: 直接取这里的两段字面量会带着 ``{rule}``，请用 :func:`fact_anchor_block` 取值。
FACT_ANCHOR_BLOCKS = {
    "keep_all": (
        "本读者的口径是 keep_all（要素材）：数字 / 引号引文 / 书名号《》一个都不许少，"
        "不要为了好读丢掉任何一项；确实必须放弃的，照规则 {rule} 逐条申报（本档下申报不会让校验放行，"
        "它只是让读者看见你动了什么）。"
    ),
    "allow_declared_drops": (
        "本读者的口径是 allow_declared_drops（要结论）：纯举例性的数据可以删掉、引文可以改成转述，"
        "**但照规则 {rule} 逐条申报放弃的每一项**；没申报的缺失一律算违规。"
        "不许新增原文没有的事实锚（换成别的数字 / 编一条引文都不行）。"
    ),
}


def fact_anchor_block(policy: str, *, rule_no: int = 10) -> str:
    """事实锚口径 → 提示词里 ``{fact_anchor_block}`` 那一格的内容（空串按严口径写）。

    **逐字稳定**：同一档取值（+ 同一 ``rule_no``）永远得到同一段文本（同一读者重跑必得逐字相同的
    提示词 ⇒ 前缀缓存有效）。不认识的取值一律按 :data:`DEFAULT_FACT_ANCHOR_POLICY` 办
    （与 ``ar.fidelity`` 同一口径）。

    ``rule_no`` 是这段文案里「逐条申报」指向的那条规则的序号：``rewrite`` 用默认的 10、
    单段压缩（``compress-v2``）传 6——默认值保证 ``rewrite-v5`` 的提示词**逐字未变**。
    """
    template = FACT_ANCHOR_BLOCKS.get(
        str(policy or "").strip(), FACT_ANCHOR_BLOCKS[DEFAULT_FACT_ANCHOR_POLICY]
    )
    return template.format(rule=rule_no)
REWRITE_SYSTEM_PROMPT = (
    "你是中文材料的分段改写引擎：按给定的读者画像与概念清单，对【原文段落】逐段决定动作并产出改写稿。"
    "你对原文的唯一义务是事实保真：原文里的数字、引号引文、书名号《》里的书名，必须逐字照抄，"
    "连注文（[注：…]、[数字]）里的数字与引文也不例外；只允许改叙述方式，不允许改动事实、不允许补充原文没有的事实。"
    "只输出一个 JSON 对象：不要解释、不要 Markdown 代码围栏、不要在 JSON 前后加任何文字。"
)

#: 通用固定块在前、**按材料变的块与本次段落在后**（``rewrite-v7``，前缀缓存）：
#: 材料行 / 概念清单 / 配图候选 / 全文统一放在规则之后；本次段号、正文与校验反馈仍是最后一块。
#: 同一材料内批间 / 段间只差最后一块；``{feedback_block}`` 空串时零字符插入。
#: **拆分**（``plan-v2``）：``REWRITE_USER_HEAD`` 是两遍式与改写共用的那一段（到全文块结束），
#: ``REWRITE_USER_TAIL`` 是改写那一遍的尾巴（计划块 + 本次段）。两段相拼与拆分前**逐字相同**，
#: 所以 ``rewrite-v7`` 一个字都没变（有测试钉住 sha256）——拆开只为让第一遍能复用同一个头做缓存。
REWRITE_USER_HEAD = """读者画像：
{profile_block}

读者背景：
{background_block}

事实锚口径：
{fact_anchor_block}

请输出这样的 JSON：
{{"decisions":[{{"paragraph":"p001","action":"keep","why":{{"profile":[],"note":"一句话说明为什么这样处理"}},"confidence":0.9,"rewrite":""}},{{"paragraph":"p002","action":"expand","why":{{"profile":["c07:unknown"],"note":"必须先讲清前置的 c06，否则读者不知道价值从哪来"}},"confidence":0.8,"rewrite":"改写后的完整段落"}},{{"paragraph":"p006","action":"compress","why":{{"profile":[],"note":"同一个意思说了三遍，只留一遍"}},"confidence":0.7,"rewrite":"压缩后的完整段落","fact_declarations":{{"numbers":["1696"],"quotes":[],"books":[],"note":"删掉了重复出现的年份 1696"}}}},{{"paragraph":"p034","action":"illustrate","why":{{"profile":[],"note":"流程类难点：三步递进，纯文字看不出先后"}},"confidence":0.8,"figure":{{"kind":"mermaid","source":"flowchart TD\\n  A[商品] --> B[货币]","caption":"三步递进"}}}}]}}

规则：
1. 下面给出的每一段都必须出现在 decisions 里，且段落顺序与原文一致；不许新增段落、不许合并段落、不许遗漏段落。
2. action 只能是 expand / compress / keep / illustrate 四个动作之一：
   - expand：画像里有该段涉及的概念属于 unknown / partial，或前置概念未掌握时，把这层意思补出来（可以加一两句讲解）；
   - compress：这一段没有新信息、只是把同一件事翻来覆去说时，删掉冗余与重复，把它说得更短
     （**事实锚一个都不许丢**：数字 / 引号引文 / 书名号《》照旧要全在）；
   - keep：其余情况一律保住原文（**宁少勿多**：拿不准就 keep）；
   - illustrate：只有「图比文字省力」（流程 / 结构 / 对比 / 数量）时才用，并在 figure 里给出图定义；
     下面「配图候选」里点到的段优先考虑，没点到但确实图更省力的也可以。
3. 只有 action=expand 或 action=compress 时才填 rewrite，且必须是该段改写后的完整正文：
   - expand：**先原样抄下整段原文，再补你要补的话**，不许改写、缩写、概括或漏掉原文的任何一句；
   - compress：只删冗余，**必须比原文短**，同样不许漏掉原文任何一句里的事实；
   两种情况都不许只回一部分（输出被截断就是失败），最后一段话要以句号 / 问号 / 感叹号收尾。
4. **事实保真（红线）**：原文里出现的数字（含年份、页码、分数、比例）、引号引文、书名号《》里的书名，
   必须逐字照抄、一个字符都不许改，**而且在同一段里只能各出现原文那么多次**
   （不许重复引用同一句引文、不许把原文没加引号的词加上引号）。
   注文 [注：…] 整块照抄，[26]、[27] 这类注释编号也照抄。
   keep 的段落请**原样**输出，不要顺手改标点、加空格或整理格式。
5. 读者已知（known）的概念不要再解释；依据必须来自上面给的读者画像与读者背景、以及下面给的概念清单，不要凭空推断读者水平。
6. confidence 是你对「这条决策合适」的把握（0–1 的小数）。
7. 不要写「改写说明」「导读」之类的额外段落；只输出 JSON。
8. **读者背景是展开 / 保持的依据之一**：与读者的学历、大学专业、自报熟悉领域、常读材料相符的段落倾向 keep
   （他已经熟的少展开、少解释），他**生疏**的领域要展开；读者写了「遇到看不懂时希望怎么讲」时，
   展开的话要照那个偏好写（例如「举个例子」「补前置概念」）。
   用了读者背景就把依据写进该段的 why.note（例如「读者是经济学门类本科，这段的专业术语可少解释」）；
   背景不要写进 why.profile，也不要在正文里提读者本人（不要写「作为经济学专业的你」这类话）。
9. illustrate 的 figure：kind 只能是 mermaid 或 svg；
   - mermaid 的 source 直接以图类型关键字开头（flowchart / graph / sequenceDiagram / stateDiagram-v2 /
     classDiagram / erDiagram / gantt / pie / mindmap / timeline），不要写 Markdown 代码围栏，
     行数 ≤ 40、字符数 ≤ 2000；
   - svg 的 source 必须是自包含的 SVG 源码：不许 <script>、不许 on* 事件属性、不许 foreignObject、
     不许指向外部地址的 href（只允许 #内部锚点）；
   - figure.caption 是一句话图注（可缺，≤40 字）；
   - illustrate 段**不要填 rewrite**：正文保持原文，图由阅读器就地渲染。
10. **事实锚申报（fact_declarations，逐段都要想一遍）**：每一段都要问自己
   「这一段里，原文的事实锚我有没有没原样保留的」。有就在该段的 fact_declarations 里**逐条申报**：
   {{"numbers":["原样写下的数字"],"quotes":["原样写下的引文"],"books":["原样写下的书名"],
   "note":"一句话说明为什么放弃它们"}}；没有就整个键都不写（**不要**给空数组）。
   申报里的每一项必须**逐字来自原文的那一项**（照抄数字本身、引号里的内容、书名号里的书名，
   不要写「页码」「某条引文」这类描述）。规则：申报只是「说清你动了什么」，
   没有申报的缺失一律按违规处理；多给原文没有的数字 / 引文 / 书名（改写成别的事实）任何情况下都不允许。
11. **why.profile 写的是你引用到的读者自评**：上面「读者画像」一节写出了自评时，
   把你依据的那几条**逐条照抄**进 why.profile（形如 c07:unknown——概念 id + 那一节给的状态，
   取值逐字一致，有多条就都写上）；读者没有任何概念自评可引用时写空数组 []，
   **不要凭空编一条自评**。读者背景同样是依据，但它的说明写在 why.note 里（见规则 8）。

材料：《{source_id}》，共 {total} 段。

概念清单：
{concept_block}{illustration_block}{full_text_section}"""

#: 改写那一遍的尾巴：计划块（``{plan_section}``，空串零字符插入）+ 本次段号 + 本批正文 + 校验反馈。
#: **两遍式的第一遍不从这一段开始**——它的尾巴是 :data:`PLAN_USER_TAIL`（同一段头的另一种续写）。
REWRITE_USER_TAIL = """{plan_section}

本次给你第 {start}–{end} 段（下面再给一遍，便于你逐段处理）：

{paragraph_block}{feedback_block}"""

#: 两段相拼 == 拆分前的模板（逐字相同：``rewrite-v7`` 的提示词一个字符都没变，有测试钉住）。
REWRITE_USER_TEMPLATE = REWRITE_USER_HEAD + REWRITE_USER_TAIL


def format_paragraph_block(paragraphs: list[dict[str, Any]]) -> str:
    """段落拼块：``[p012] 正文``，段间空行（与摄取切段规则同形）。"""
    return "\n\n".join(f"[{p.get('id')}] {p.get('text', '')}" for p in paragraphs)


def _full_text_section(full_text_block: str) -> str:
    """全文块 → 模板里 ``{full_text_section}`` 那一格的内容（``rewrite-v7``）。

    **空串（缺省）时零字符插入**：老调用方（不带全文）的提示词不会多出一个空标题；
    非空时返回「一个空行 + 一行标题 + 全文」，位置在概念清单之后、本次段号之前。
    标题里写明「每段以 [段 id] 开头」，与 ``format_paragraph_block`` 的拼法对应。
    """
    text = (full_text_block or "").strip("\n")
    if not text.strip():
        return ""
    return "\n\n原文（全文，每段以 [段 id] 开头，引用时必须原样使用这些 id）：\n" + text


def _feedback_section(feedback: str) -> str:
    """逐段循环的校验回灌（``rewrite-v7``）：空串零字符插入，非空时另起一段。

    它排在 user 消息的**最末尾**（本批正文之后），所以只会动尾巴，不破坏前缀缓存；
    模型是无状态的，回灌文案必须自己说清「上一次哪里没通过」，不能只写「再试一次」。
    """
    text = (feedback or "").strip()
    if not text:
        return ""
    return "\n\n" + text


def _plan_section(plan_block: str) -> str:
    """第一遍的计划块 → 模板里 ``{plan_section}`` 那一格的内容（``plan-v2``，两遍式）。

    **空串（缺省）时零字符插入**：不带计划的调用方（``--plan-first`` 未开）拿到的提示词
    **逐字不变**（``rewrite-v7`` 因此不用升版，有测试钉住）。非空时返回「一个空行 + 一行标题 + 计划」，
    位置紧跟全文块、在本次段号之前——同一材料的所有批 / 所有段共享这一块，首个差异字符仍落在段号位。
    计划块本身由 ``ar.rewrite._plan_block`` 生成（逐字稳定：同一份计划永远得到同一段文本）。
    """
    text = (plan_block or "").strip("\n")
    if not text.strip():
        return ""
    return (
        "\n\n第一遍的动作计划（每行「段 id 动作：一句话依据」；照这份计划执行——"
        "与计划不一致时，就在该段的 why.note 里写明为什么改主意）：\n" + text
    )


def _illustration_section(illustration_block: str) -> str:
    """配图候选块 → 模板里 ``{illustration_block}`` 那一格的内容。

    **空块返回空串（零字符插入）**：那一格紧跟在 ``{concept_block}`` 之后，空串接上去不会多出任何
    字符（多一个换行就不等价了），所以「空块 == 缺省」，有块与无块的差异**只在那一格**
    （别拿 ``rewrite-v3`` 的提示词逐字对账：本批同时还改了规则 2/3/9 与 JSON 示例）。
    非空时返回「一个空行 + 一行标题 + 候选清单」，位置就在概念清单之后、材料正文之前。
    """
    text = (illustration_block or "").strip()
    if not text:
        return ""
    return "\n\n配图候选（analyze 标出的「图比文字省力」的位置，逐条是「段 id 类型：为什么」）：\n" + text


def build_rewrite_messages(
    source_id: str,
    paragraphs: list[dict[str, Any]],
    *,
    profile_block: str,
    concept_block: str,
    start: int,
    end: int,
    total: int,
    background_block: str | None = None,
    illustration_block: str = "",
    fact_anchor_policy: str = DEFAULT_FACT_ANCHOR_POLICY,
    full_text_block: str = "",
    feedback: str = "",
    plan_block: str = "",
) -> list[dict[str, str]]:
    """一段提示词吃一批段落（默认 25 段切成 6 批；逐段循环时每段一批）。

    ``start`` / ``end`` 是这批段落在全篇里的序号（1 起，含两端），只用于让模型知道自己看的是哪一段。
    ``background_block`` 是读前背景问卷渲染出的读者素描（``ar.survey.render_sketch``）：
    它是**逐字稳定**的固定块，缺省（老调用方）时写「未提供」文案，保证模板逐字稳定。
    ``illustration_block`` 是配图候选素描（``ar.analyze.format_illustration_block``）：
    同为逐字稳定的固定块，**空串（缺省）时零字符插入**（空块 == 缺省 == 有块与无块的差异只在那一格）。
    ``fact_anchor_policy`` 是**本读者的事实锚口径**（``ar.fidelity.FACT_ANCHOR_POLICIES`` 之一，
    ``rewrite-v5`` 起进提示词的「事实锚口径」固定块）：同一读者重跑必得逐字相同的提示词；
    取值不认识 / 缺省时按严口径 :data:`DEFAULT_FACT_ANCHOR_POLICY` 写（与 ``ar.fidelity`` 同一口径）。
    ``full_text_block``（``rewrite-v7``）是**同一份材料的全文**（``format_paragraph_block`` 的产物）：
    它排在「按材料变」的那一段里，同一材料的所有批 / 所有段共享；缺省（老调用方）时零字符插入。
    ``feedback``（``rewrite-v7``）是逐段循环的校验回灌：**只追加在最后**，空串零字符插入，
    因此不会破坏前缀缓存。
    ``plan_block``（``plan-v2``，两遍式的第一遍计划，``ar.rewrite._plan_block`` 的产物）:
    它排在全文块之后、本次段号之前（同一材料的所有批共享），**空串（缺省）时零字符插入**——
    于是 ``--plan-first`` 未开时的提示词与加这一格之前**逐字相同**（``rewrite-v7`` 不用升版）。
    **本次段号与本批正文排在 user 消息末尾**；通用块 / 材料块 / 全文都在它们之前，
    改布局前先读模块文档的「前缀缓存纪律」。
    """
    return [
        {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": REWRITE_USER_TEMPLATE.format(
                profile_block=profile_block,
                background_block=background_block or EMPTY_BACKGROUND_SKETCH,
                fact_anchor_block=fact_anchor_block(fact_anchor_policy),
                concept_block=concept_block,
                illustration_block=_illustration_section(illustration_block),
                full_text_section=_full_text_section(full_text_block),
                plan_section=_plan_section(plan_block),
                source_id=source_id,
                total=total,
                start=start,
                end=end,
                paragraph_block=format_paragraph_block(paragraphs),
                feedback_block=_feedback_section(feedback),
            ),
        },
    ]


# ------------------------------------------------- 两遍式改写的第一遍（plan-v2） --
# 「先打标、再改写」：第一遍**只要动作**（一段一条，不写正文），第二遍把这计划放进固定上下文
# 照它改写——不写正文时没有「别把原文改坏」的压力，动作决定因此更敢下手（``--plan-first``）。
#
# ``plan-v2``（2026-10-31）：``plan-v1`` 的两遍之间**一个字符的公共前缀都没有**（system 不同、user 首字符
# 就分岔），打标那一遍的 prompt token 拿不到第二遍的缓存折扣。现在改成「**同一个头 + 各自的尾巴**」：
#   第一遍 = ``REWRITE_USER_HEAD`` + ``PLAN_USER_TAIL``（打标指令 / 示例 / 5 条规则，全部后置）
#   第二遍 = ``REWRITE_USER_HEAD`` + ``REWRITE_USER_TAIL``（计划块 + 本次段）
# system 两遍都用 :data:`REWRITE_SYSTEM_PROMPT`。于是两遍共享 9889 字符（5364 token）前缀，
# **第一遍建好的缓存能被第二遍第 1 批直接命中**（本机网关门槛约 2048 token，见 README §8 第 14 条）。
# 代价：第一遍要多背 rewrite 的 JSON 示例与 11 条规则——那正是它要共用的那段前缀，仍只全价买一次。
#
# ⚠ 头里那 11 条规则与 JSON 示例是**改写**那一遍的口径（要求正文、申报、why.profile）。打标尾巴必须
# 明确声明「规则 3／10／11 这一轮不适用、输出形状以本节为准」，否则模型会照着示例吐 decisions。

#: 打标那一遍的尾巴（``plan-v2``）：**固定判别标记** + 打标指令 + plan 的 JSON 示例 + 5 条规则。
#: 全部**后置**在共享头之后——「会变的排最后」是前缀缓存的前提，两遍的差异就在全文块之后这一截。
PLAN_USER_TAIL = """

【动作打标·第一遍】这一遍只判断动作，**一个字正文都不要写**：上面那份「请输出这样的 JSON」与
规则 3／10／11（正文、事实锚申报、why.profile）**这一轮都不适用**；本轮的输出形状以本节为准。

请输出这样的 JSON：
{{"plan":[{{"paragraph":"p001","action":"keep","note":"这一段读者没有生词"}},{{"paragraph":"p002","action":"expand","note":"读者没有 c03 的自评，前置概念要补"}}]}}

规则：
1. plan 必须**逐段一条**，段 id 与顺序和原文完全一致；不许新增段、合并段、拆分段、遗漏段。
2. action 只能取 expand / compress / keep / illustrate 四个值之一，判据与改写时相同：
   - expand：读者对这一段涉及的概念生疏（unknown / partial）或前置概念未掌握，要补一两句讲解；
   - compress：这一段没有新信息、只是把同一件事翻来覆去说时，删掉冗余、说得更短；
   - keep：其余情况一律保住原文（**拿不准就 keep**）；
   - illustrate：只有「图比文字省力」（流程 / 结构 / 对比 / 数量）时才用。
3. **这一遍不写正文**：不要给 rewrite、不要给 figure、不要复述或照抄原文的长句——段落原文只用来判断。
4. note 是一句话依据（说清是读者画像、读者背景还是概念清单里的哪一点），不许留空。
5. 只输出这个 JSON 对象，不要写「说明」「依据汇总」之类的额外内容。"""

#: 第一遍的 user 消息 = **改写那份的公共头** + 打标尾巴（``plan-v2``）。
#: ⚠ 「【动作打标·第一遍】」是**固定判别标记**：假客户端 / 用例靠它把两种提示词分流
#: （它在尾巴里，不在开头——开头必须与第二遍逐字相同）。
PLAN_USER_TEMPLATE = REWRITE_USER_HEAD + PLAN_USER_TAIL


def build_plan_messages(
    source_id: str,
    paragraphs: list[dict[str, Any]],
    *,
    profile_block: str,
    concept_block: str,
    background_block: str | None = None,
    illustration_block: str = "",
    total: int | None = None,
    fact_anchor_policy: str = DEFAULT_FACT_ANCHOR_POLICY,
    full_text_block: str | None = None,
) -> list[dict[str, str]]:
    """两遍式的第一遍（``plan-v2``）：一次调用给全篇每段打动作标，**不要正文**。

    提示词 = 改写那份 user 消息的**公共头**（``REWRITE_USER_HEAD``：画像 / 背景 / 事实锚口径 /
    JSON 示例 / 规则 / 材料行 / 概念清单 / 配图候选 / 全文）+ ``PLAN_USER_TAIL``（打标指令与规则）；
    system 也用 :data:`REWRITE_SYSTEM_PROMPT`。**两遍共享前缀是这套排版的全部意义**：
    第一遍建立的缓存能被第二遍第 1 批命中（``plan-v1`` 时公共前缀 0 字符，打标的钱全价）。
    与 :func:`build_rewrite_messages` 的差别只有一处：第二遍在头之后接的是计划块 + 本次段。

    ``full_text_block`` 为 ``None``（老调用方）时按 ``paragraphs`` 自己拼（与改写那边给的逐字相同）；
    显式给空串表示「这一份就是没有全文」（那一格零字符插入），与改写那一遍同一口径。
    其余入参与改写一致：``background_block`` 缺省写「未提供」、``illustration_block`` 空块零字符插入、
    ``fact_anchor_policy`` 不认识就按严口径写 —— 同一读者、同一份材料的两次打标必得**逐字相同**的提示词。
    """
    count = len(paragraphs) if total is None else int(total)
    text = format_paragraph_block(paragraphs) if full_text_block is None else full_text_block
    head = REWRITE_USER_HEAD.format(
        profile_block=profile_block,
        background_block=background_block or EMPTY_BACKGROUND_SKETCH,
        fact_anchor_block=fact_anchor_block(fact_anchor_policy),
        concept_block=concept_block,
        illustration_block=_illustration_section(illustration_block),
        full_text_section=_full_text_section(text),
        source_id=source_id,
        total=count,
    )
    return [
        {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
        {"role": "user", "content": head + PLAN_USER_TAIL},
    ]


# ------------------------------------------------- 一次性整篇改写（对比基线，oneshot-v1） --
#: 基线不是主路径：**一次调用**吃全文、吐全文（``{"paragraphs":[{paragraph,text}]}``），
#: 没有逐段决策 / 申报 / 校验重试。它只用来和「分批 / 逐段」两条主路做成本与保真对照。
#: 提示词纪律与 rewrite 相同：通用块在前、材料块与全文居中、**原文全文是最后一块**。
ONESHOT_PROMPT_VERSION = "oneshot-v1"

ONESHOT_SYSTEM_PROMPT = (
    "你是中文材料的整篇改写引擎：按给定的读者画像与概念清单，把整篇材料改写成更贴合这位读者的版本。"
    "你对原文的唯一义务是事实保真：原文里的数字、引号引文、书名号《》里的书名必须逐字照抄，"
    "连注文（[注：…]、[数字]）里的数字与引文也不例外；只允许改叙述方式，不允许改动事实、"
    "不允许补充原文没有的事实。只输出一个 JSON 对象：不要解释、不要 Markdown 代码围栏、"
    "不要在 JSON 前后加任何文字。"
)

ONESHOT_USER_TEMPLATE = """读者画像：
{profile_block}

读者背景：
{background_block}

事实锚口径：
{fact_anchor_block}

请输出这样的 JSON：
{{"paragraphs":[{{"paragraph":"p001","text":"这一段的正文（改写过，或与原文逐字相同）"}}]}}

规则：
1. 原文共 {total} 段：输出必须**逐段一条**、段落 id 与顺序和原文完全一致；不许合并、拆分、遗漏段落。
2. 不需要改的段把原文**逐字照抄**，不要顺手改标点 / 加空格 / 整理格式。
3. **事实保真（红线）**：数字（含年份、页码、分数、比例）、引号引文、书名号《》里的书名必须逐字照抄，
   同一段里只能各出现原文那么多次；不许换成别的数字、不许编一条引文、不许补原文没有的事实。
4. 只输出 JSON，不要写「改写说明」「导读」之类的额外段落。

材料：《{source_id}》，共 {total} 段。

概念清单：
{concept_block}

原文（全文，每段以 [段 id] 开头，引用时必须原样使用这些 id）：
{full_text_block}"""


def build_oneshot_messages(
    source_id: str,
    paragraphs: list[dict[str, Any]],
    *,
    profile_block: str,
    concept_block: str,
    background_block: str | None = None,
    fact_anchor_policy: str = DEFAULT_FACT_ANCHOR_POLICY,
    total: int | None = None,
) -> list[dict[str, str]]:
    """一次性整篇改写的提示词（``oneshot-v1``）：全文进 user 末尾，一次调用出全篇。

    ``total`` 缺省取段落数；``background_block`` / ``fact_anchor_policy`` 与 rewrite 同一口径
    （同一读者、同一事实锚政策），**便于与主路径公平对照**。这条提示词没有逐段申报通道，
    所以事实锚校验按读者的口径判、但没有 ``fact_declarations`` 可放行——基线本来就该更严。
    """
    count = len(paragraphs) if total is None else int(total)
    return [
        {"role": "system", "content": ONESHOT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": ONESHOT_USER_TEMPLATE.format(
                profile_block=profile_block,
                background_block=background_block or EMPTY_BACKGROUND_SKETCH,
                fact_anchor_block=fact_anchor_block(fact_anchor_policy),
                concept_block=concept_block,
                source_id=source_id,
                total=count,
                full_text_block=format_paragraph_block(paragraphs),
            ),
        },
    ]


# ---------------------------------------------------------------- 单段压缩（读中） --
# 读者点「太啰嗦」→ 当场把这一段压短（PLAN-actions-feedback §5.1）。提示词版本 ``compress-v3``。
# 与另外四份模板同一条前缀缓存纪律：固定块（画像 / **事实锚口径** / 概念清单 / 规则 / JSON 形状）在前，
# **要压缩的那段原文在末尾**（逐字稳定 ⇒ 同一段重复点也能命中缓存前缀）。
EMPTY_PROFILE_BLOCK = "（没有读者画像：只按下面的规则压缩，不要凭空推断读者水平）"
EMPTY_CONCEPT_BLOCK = "（没有概念清单：不要依据概念状态做取舍，只做删冗余）"
#: ``instruction`` 缺省时的固定文案（逐字稳定，不写时间戳 / 随机量）
DEFAULT_COMPRESS_INSTRUCTION = "这一段太啰嗦：只删冗余，不许丢事实。"

COMPRESS_SYSTEM_PROMPT = (
    "你是中文材料的压缩引擎：读者觉得某一段太啰嗦，你要在**不丢事实**的前提下把它写短。"
    "你对原文的唯一义务是事实保真：原文里的数字、引号引文、书名号《》里的书名必须逐字照抄，"
    "连注文（[注：…]、[数字]）里的数字与引文也不例外；只允许删冗余，不允许补充原文没有的事实。"
    "只输出一个 JSON 对象：不要解释、不要 Markdown 代码围栏、不要在 JSON 前后加任何文字。"
)

COMPRESS_USER_TEMPLATE = """读者画像：
{profile_block}

事实锚口径：
{fact_anchor_block}

概念清单：
{concept_block}

压缩要求：{instruction}

请输出这样的 JSON：
{{"action":"compress","text":"压缩后的完整段落","why_note":"一句话说明删掉了什么"}}

规则：
1. 只删：重复的表述、可省的修饰与举例、冗长的连接语；同一个意思说了几遍就只留一遍。
2. **事实保真（红线）**：原文里的数字（含年份、页码、分数、比例）、引号引文、书名号《》里的书名，
   必须逐字照抄、一个字符都不许改，**而且在压缩稿里只能各出现原文那么多次**。
   注文 [注：…] 整块照抄，[26]、[27] 这类注释编号也照抄。
3. 压缩稿必须比原文**短**，并且以句末标点（。！？）收尾；不许只回原文的一部分（输出被截断就是失败）。
4. 不许补原文没有的内容，不许写「改写说明」「导读」，不要输出 Markdown 标题。
5. why_note 用一句话说明删掉了什么（可以缺）。
6. **事实锚申报（fact_declarations，压缩前先想一遍）**：问自己「这一段里，原文的事实锚我有没有
   没原样保留的」。有就在 fact_declarations 里**逐条申报**：
   {{"numbers":["原样写下的数字"],"quotes":["原样写下的引文"],"books":["原样写下的书名"],
   "note":"一句话说明为什么放弃它们"}}；**没有就整个键都不写**（不要给空数组、不要为凑格式写一个空申报）。
   申报里的每一项必须**逐字来自原文的那一项**（照抄数字本身、引号里的内容、书名号里的书名，
   不要写「页码」「某条引文」这类描述）。规则：申报只是「说清你动了什么」，
   没有申报的缺失一律按违规处理（本次压缩会被拒绝写入）；多给原文没有的数字 / 引文 / 书名
   （改写成别的事实）任何情况下都不允许。

原文段落（[段 id] 开头，引用时必须原样使用这个 id）：

[{paragraph}] {text}"""


def build_compress_messages(
    *,
    paragraph: str,
    text: str,
    concepts: str = "",
    profile_block: str = "",
    instruction: str = "",
    fact_anchor_policy: str = DEFAULT_FACT_ANCHOR_POLICY,
) -> list[dict[str, str]]:
    """单段压缩提示词（``compress-v3``）：固定块在前、**该段原文在末尾**。

    ``concepts`` 是概念清单素描（``ar.rewrite._format_concept_block`` 的产物，与 ``rewrite_batch``
    的 ``concept_block`` 同一形态）；空串时写「没有概念清单」的固定文案。
    ``instruction`` 是读者的补充要求（缺省是「太啰嗦」那条固定文案）。
    ``fact_anchor_policy`` 是**本读者的事实锚口径**（``ar.fidelity.FACT_ANCHOR_POLICIES`` 之一，
    ``compress-v2`` 起进提示词的「事实锚口径」固定块，与 ``rewrite-v5`` 共用同一段文案）：
    同一读者重复点必得逐字相同的提示词（前缀缓存）；取值不认识 / 缺省时按严口径
    :data:`DEFAULT_FACT_ANCHOR_POLICY` 写（与 ``ar.fidelity`` 同一口径）。
    """
    return [
        {"role": "system", "content": COMPRESS_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": COMPRESS_USER_TEMPLATE.format(
                profile_block=(profile_block or "").strip() or EMPTY_PROFILE_BLOCK,
                fact_anchor_block=fact_anchor_block(fact_anchor_policy, rule_no=6),
                concept_block=(concepts or "").strip() or EMPTY_CONCEPT_BLOCK,
                instruction=(instruction or "").strip() or DEFAULT_COMPRESS_INSTRUCTION,
                paragraph=paragraph,
                text=text,
            ),
        },
    ]


# ------------------------------------------------------ 读前背景问卷的模型追问 --
# 输入只有**固定题的作答 + 素描草稿**（契约 §4：追问看作答，不读材料正文），
# 输出 3–5 个细节问题。固定块在前、作答在末尾（与另外两份模板同一条前缀缓存纪律）。
SURVEY_SYSTEM_PROMPT = (
    "你是中文阅读材料的助手。读者刚刚填完一份读前背景问卷，你要针对他的自述追问几个细节，"
    "帮后续的改写判断他真正熟悉什么、哪里需要多解释。只追问背景问卷里已经提到的内容，"
    "不要问与背景无关的问题，不要评价读者的水平，不要输出任何解释。"
    "只输出一个 JSON 对象：不要解释、不要 Markdown 代码围栏、不要在 JSON 前后加任何文字。"
)

SURVEY_USER_TEMPLATE = """请输出这样的 JSON：
{{"questions":[{{"q":"你说熟悉经济与金融——读过《资本论》原文吗？","hint":"读过哪几章 / 只读过二手介绍 / 没读过，都行"}}]}}

规则：
1. 给 3–5 个问题，按「最值得先问」的顺序排。
2. 每个问题都要**接着读者的自述**问（可以点名他写过的学历、专业、熟悉领域、常读材料、补充说明），
   不要问问卷里已经答过的事实，也不要问与背景无关的问题。
3. q 是一个完整的中文问句，一句话；hint 是一句话提示读者可以怎么答（给出几个可选的答法）。
4. 只输出 questions 一个字段，不要输出答案、不要输出建议。

读者背景（问卷作答的素描；读者没提供的一项就别问）：
{sketch_block}

读者的固定题作答（原始枚举值，仅供参考，不要在问题里直接念这些英文串）：
{answer_block}"""


def build_survey_messages(
    *,
    sketch: str,
    answers: dict[str, Any],
    reader: str = "",
) -> list[dict[str, str]]:
    """追问提示词：固定块（规则 + JSON 形状）在前，读者的作答与素描在末尾。

    ``sketch`` 由 ``ar.survey.render_sketch`` 渲染（逐字稳定）；``answers`` 是**结构化作答**，
    用 ``json.dumps(..., ensure_ascii=False)`` 原样给出，模型据此点名读者写过的东西。
    """
    answer_block = json.dumps(answers, ensure_ascii=False, sort_keys=False)
    return [
        {"role": "system", "content": SURVEY_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": SURVEY_USER_TEMPLATE.format(
                sketch_block=sketch or EMPTY_BACKGROUND_SKETCH,
                answer_block=answer_block,
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
    selection: str | None = None,
    selection_from: str = DEFAULT_SELECTION_FROM,
) -> list[dict[str, str]]:
    """讲解卡片的提示词：本段原文 + 少量上下文 + 读者画像提示。

    ``selection`` 非空时换成「选片」版式：**讲解重点 = 选中的片段**，整段原文降级为上下文
    （邻段、画像、kind 指令照旧都在，段落 id 仍然用于定位）。
    传进来的 ``selection`` 由调用方（``ar.explain.prepare_selection``）保证已规范化并截断到
    ``SELECTION_MAX_CHARS``；这里再按锚点口径（``ingest.normalize_text``，幂等）规范化一次只是兜底。
    """
    parts = [f"段落 id：{paragraph_id}"]
    focus = normalize_text(selection or "")
    if focus:
        label = SELECTION_SOURCE_LABELS.get(selection_from, SELECTION_SOURCE_LABELS[DEFAULT_SELECTION_FROM])
        parts += [
            "",
            f"读者选中的片段（来自{label}；**讲解重点就是这段文字**，其余内容只作上下文）：",
            focus,
            "",
            "该片段所在的整段原文（只作上下文：用它判断指代与前后文，不要整段复述）：",
            paragraph_text,
        ]
    else:
        parts += ["", "这一段原文：", paragraph_text]
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
