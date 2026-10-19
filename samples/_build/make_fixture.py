"""生成前端夹具 `samples/<source_id>/doc.json` 与 `explain.json`（手造样例，非模型产出）。

用途：W2（阅读器前端）脱离后端做无头验证；也是 W1/W3 对照冻结 schema 的参考样例。

锚点规则（照 PLAN-skeleton.md §4，逐字实现）：
  id        = "p" + 三位序号（从 1 起，按源文档空行切分的段序）
  text_hash = sha256(规范化文本)[:8]
  规范化    = 去首尾空白 + 合并连续空白 + 全角空格(U+3000)转半角

改写稿是本文件里手写的 25 段（与底本的 25 个空行切分段一一对应），
`changes` 里的 action / why / confidence / facts 也全部手写，**不是 rewrite 的真实输出**。
脚本只做三件事：切段打锚点、把手写内容装配成 doc.json、把长度比对照表打到 stdout 供人工复核。

2026-10-13 重定（PLAN-actions-feedback.md §4 / §12 的 W 路，compress / illustrate 已实现）：

  - **去掉全部 `deferred`**：它表示「枚举内、本期未实现」；compress 与 illustrate 本批都实现了，
    再挂着就是假的。`deferred` 机制本身仍在 schema 里（留给将来的枚举成员），只是本夹具不再有。
  - **配图段**：`p013`（mermaid 流程图）与 `p024`（SVG 柱状图）是 **illustrate 段**。
    图**不进 `rewritten.md`**（§1 第 2 条）：这两段的正文与底本**逐字节相同**，
    图由前端在该段容器内就地插入。所以脚本里这两段用 `SAME_AS_SOURCE` 占位（装配时填原文，
    免得手抄一遍抄错）。
  - **配图失败样本**：`p016` 是「想配图但模型给的图过不了校验」的那一段——`action` 记 `keep`、
    带 `figure_error`、正文照旧（`§4.1` 的失败口径），前端据此走「退回文字说明」那条路。

2026-10-14 补两处（夹具必须跟着产物字段一起长，见 `samples/README.md`）：

  - **事实锚申报样本**（`§14.1`）：`p004` 带 `fact_declarations`，`fidelity` 里配 `fact_anchor_policy` /
    `waived`——脚本会先算一遍「申报的项在底本里有、在改写稿里真的没有」再落盘；
  - **兜底补齐样本**：`p005` 带 `profile_refs_filled: true`（`why.note` 末尾追加那一句出处）。
    此前这个字段只在 `doc.json` 里手改过、**生成器不产出**——一重生成夹具样本就消失，
    `tests/web/test_fixture_schema.py` 那条断言当场变红。现在由 `PROFILE_REFS_FILLED` 装配。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SAMPLES = HERE.parent
PROJECT = SAMPLES.parent

SOURCE_ID = "capital-v1-ch01"
SOURCE_FILE = PROJECT.parent / "学习" / "_第1章第1节三版对照" / "00-底本.md"
SOURCE_REL = "学习/_第1章第1节三版对照/00-底本.md"
INGEST_AT = "2026-10-05T09:00:00+08:00"


def normalize(text: str) -> str:
    """契约 §4 的规范化：去首尾空白 + 合并连续空白 + 全角空格转半角。"""
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:8]


def split_paragraphs(raw: str) -> list[str]:
    """按空行切段；丢弃纯空白块。返回的每段保留内部换行、去掉首尾空白。"""
    blocks = re.split(r"\n[ \t]*\n", raw.replace("\r\n", "\n").replace("\r", "\n"))
    return [b.strip() for b in blocks if b.strip()]


# ---------------------------------------------------------------- 手写改写稿
# 25 段，与底本空行切分的 25 段一一对应（段序一致）。形态参照 C-现代重述.md：
# 自由重述、带导读与结论表、不逐句对应原文；引证体例从简。
# `SAME_AS_SOURCE`：这一段与底本**逐字节相同**（illustrate 段的正文要求，见文件头）。
SAME_AS_SOURCE = None

REWRITTEN: list = [
    # p001 标题
    "# 第一篇 商品和货币 / 第一章 商品 / 1．商品的两个因素：使用价值与价值",
    # p002 体例说明（替换底本的「底本/说明」引首）
    "> 本稿为**自由重述**版，不逐句对应原文，引证体例从简，术语取当代通行说法。"
    "**不能当作《资本论》原文引用。**",
    # p003 分隔线
    "---",
    # p004 小标题
    "## 这一节要解决什么问题",
    # p005 开篇
    "论证从最日常的事实开始：在资本主义生产方式占统治地位的社会里，财富表现为「庞大的商品堆积」，"
    "单个商品是这种财富的元素形式。要理解资本，就得先拆开商品。这一节拆的是商品身上的两个因素"
    "——**使用价值**与**价值**，并回答：价值由什么决定。",
    # p006 商品与需要
    "商品首先是一个外界的物，靠自己的属性满足人的某种需要。需要的性质（由胃产生还是由幻想产生）无关紧要，"
    "满足的方式（直接作生活资料，还是作生产资料间接满足）也无关紧要。",
    # p007 质与量两个角度
    "每一种有用物（如铁、纸）都可以从质和量两个角度考察：它是许多属性的总和，因而可以在不同方面有用。"
    "发现物的多种使用方式，是历史的事情；为有用物的量找到社会尺度，同样是历史的事情"
    "——尺度不同，部分缘于物的性质，部分缘于约定俗成。",
    # p008 使用价值
    "物的有用性使物成为**使用价值**。但有用性不是悬空的：它决定于商品体的属性，离开商品体就不存在。"
    "所以商品体本身就是使用价值（财物）——铁、小麦、金刚石。使用价值同取得它耗费多少劳动无关，"
    "它总是以一定的量为前提（几打表、几码布、几吨铁），并且只在使用或消费中得到实现。"
    "不论财富的社会形式如何，使用价值总是财富的物质内容；而在我们要考察的社会形式中，"
    "它同时又是**交换价值的物质承担者**。",
    # p009 交换价值的两难
    "**交换价值**首先表现为一种使用价值同另一种使用价值相交换的量的比例，这个比例随时间和地点不断改变。"
    "于是出现一个悖论式的外观：交换价值似乎是偶然的、纯粹相对的东西；而「商品固有的、内在的交换价值」"
    "这种说法本身，就像「圆形的方」一样自相矛盾。巴尔本断言，任何东西都不可能有内在的交换价值。",
    # p010 诗句引子
    "巴特勒的顺口溜说得更直白——",  # 底本此处被空行截断，补成完整引导语

    # p011 诗句 + 转折
    "「物的价值正好和它会换来的东西相等。」现在我们进一步考察这个问题。",
    # p012 一夸特小麦的多重交换价值
    "既然一夸特小麦可以按各种极不相同的比例同 x 量鞋油、y 量绸缎、z 量金交换，小麦就有许多种交换价值。"
    "既然这些都是它的交换价值，它们必定能互相代替、同样大。由此得出两点：第一，同一种商品的各种有效交换价值"
    "表示一个等同的东西；第二，交换价值只能是某种与它相区别的内容的表现方式，即「表现形式」。",
    # p013 等式与第三种东西（illustrate：正文＝原文，图就地插）
    SAME_AS_SOURCE,
    # p014 几何学类比
    "用一个简单的几何学例子就能说明这一点：为了确定和比较各种直线形的面积，把它们分成三角形，"
    "再用三角形化成与外形完全不同的表现——底乘高的一半。各种商品的交换价值同样要化成一种共同东西，"
    "各自代表这种东西的多量或少量。这一步是后面「抽掉使用价值」的铺垫："
    "几何学里能化成面积的东西，在商品里对应的是劳动。",
    # p015 共同东西不是天然属性
    "这种共同东西不可能是商品的几何的、物理的、化学的或其他天然属性——因为商品交换关系的特点"
    "恰恰在于抽去使用价值：只要比例适当，一种使用价值就和任何另一种使用价值完全相等。",
    # p016 巴尔本 + 质的差别/量的差别（illustrate 失败样本：正文＝原文，只有 figure_error）
    SAME_AS_SOURCE,
    # p017 只剩劳动产品
    "那么把商品体的使用价值撇开，商品体就只剩下一个属性：它是劳动产品。",
    # p018 抽掉具体劳动
    "可是这一步还要走远一点：把劳动产品的使用价值抽去，也就等于把那些使它成为使用价值的物质组成部分"
    "和形式抽去。它们不再是桌子、房屋、纱，一切可以感觉到的属性都消失了；它们也不再是木匠劳动、"
    "瓦匠劳动、纺纱劳动。随着有用性质的消失，体现在产品中的各种劳动的有用性质也消失了，"
    "各种具体形式消失了，全都化为相同的人类劳动，即**抽象人类劳动**。",
    # p019 剩下来的东西 = 价值
    "现在考察劳动产品剩下来的东西：只是同一的「幽灵般的对象性」，只是无差别的人类劳动的单纯凝结，"
    "即不管以哪种形式进行的人类劳动力耗费的单纯凝结。这些物表示的是，在它们的生产上耗费了人类劳动力、"
    "积累了人类劳动。作为这个共有的社会实体的结晶，它们就是**价值**——商品价值。",
    # p020 交换关系中表现出来的共同东西就是价值
    "可见，在商品的交换关系本身中，交换价值表现为同使用价值完全无关的东西；把使用价值真正抽去，"
    "就得到刚才已经规定的价值。所以，在交换关系或交换价值中表现出来的共同东西，也就是商品的价值。"
    "这里先不管价值的形式，只考察价值本身。",
    # p021 价值量的计量
    "使用价值或财物之所以有价值，只是因为抽象人类劳动体现或物化在里面。那么它的价值量怎么计量？"
    "用它所包含的「形成价值的实体」即劳动的量来计量；劳动的量用劳动的持续时间来计量，"
    "而劳动时间又以小时、日等为单位。注意：这里问的是**量**——价值量用什么计量、尺度是什么；"
    "至于价值的实体是什么，前面已经回答过了，别把这两件事混在一起。",
    # p022 「越懒越有价值」的误解
    "由「价值取决于所耗费的劳动量」很容易推出一个误解：越懒、越不熟练的人花的时间越多，"
    "他的商品岂不越有价值？马克思的回答是：形成价值实体的劳动是相同的人类劳动，是同一的人类劳动力的耗费，"
    "每个单个劳动力只要具有社会平均劳动力的性质，在生产上就只使用**社会必要劳动时间**"
    "——在现有的社会正常的生产条件下，在社会平均的劳动熟练程度和劳动强度下，"
    "制造某种使用价值所需要的劳动时间。",
    # p023 底本伪重复段（原文与上一段逐字重复）
    "（底本此处有一整段与上一段逐字重复的录入冗余，约 429 字，此处不再重复。）",
    # p024 价值量与劳动生产力成反比（illustrate：正文＝原文，图就地插）
    SAME_AS_SOURCE,
    # p025 三条例外 + 结论表
    "三条例外：① 一个物可以是使用价值而不是价值——凡不是由于劳动而对人有用的情况都是，例如空气、处女地、"
    "天然草地、野生林。② 一个物可以有用、又是人类劳动产品，却不是商品——用自己的产品满足自己需要的人，"
    "生产的只是使用价值。③ 要成为商品，产品不仅要是有用的，还要是**为别人**的、社会的使用价值，"
    "必须通过交换转到把它当作使用价值使用的人手里。反过来的情形不存在："
    "**没有一个物可以是价值而不是使用物品**——物没有用，其中包含的劳动也就没有用，不能算作劳动，"
    "因此不形成价值。\n"
    "| 概念 | 从哪来 | 决定因素 | 计量 |\n"
    "| --- | --- | --- | --- |\n"
    "| 使用价值 | 物的有用性 | 商品体的属性 | 不可比（只有质的差别） |\n"
    "| 交换价值 | 价值的表现形式 | —— | 交换比例（随时空变动） |\n"
    "| 价值 | 抽象人类劳动的凝结 | 抽象人类劳动 | 劳动量 → 劳动时间 |\n"
    "| 价值量 | —— | 社会必要劳动时间 | 小时、日 |",
]

# ------------------------------------------------- 手写变更记录（index 与上表对齐）
# action ∈ expand|compress|keep|illustrate（四个都实现了，2026-10-13 起不再有 `deferred`）。
# 配图段（p013 / p024）与配图失败段（p016）的图字段由 FIGURES / FIGURE_ERRORS 在 build() 里挂上。
CHANGES: list[dict] = [
    dict(action="keep", confidence=0.96, profile=[], note="标题层级段，只把连接词改为顿号，不增删信息"),
    dict(action="compress", confidence=0.93, profile=[],
         note="体例说明：底本三行引首压成一行，只留「自由重述、不逐句对应」这条要紧提示"),
    dict(action="keep", confidence=0.99, profile=[], note="分隔线"),
    dict(action="keep", confidence=0.95, profile=[], note="小标题：底本的编号标题已在 p001 出现，此处改为导读标题"),
    dict(action="expand", confidence=0.88, profile=["c01:unknown"],
         note="开篇直接进入论证，先补一句论证起点与本节目标，否则读者不知道「拆商品」是要回答什么"),
    dict(action="compress", confidence=0.74, profile=["c01:known"],
         note="「需要由胃还是由幻想产生」的两层区分属常识，压掉重复铺陈"),
    dict(action="compress", confidence=0.71, profile=["c02:partial"],
         note="两处长注释合并为一句；「为有用物的量找到社会尺度」的举例删去"),
    dict(action="compress", confidence=0.69, profile=["c02:partial", "c03:unknown"],
         note="洛克与巴尔本的长注压缩为一句，保留「使用价值不是悬空的」「交换价值的物质承担者」两个要点",),
    dict(action="compress", confidence=0.72, profile=["c03:unknown"],
         note="列特隆注与「形容语的矛盾」的译者注合并，只留自相矛盾这个直觉"),
    dict(action="expand", confidence=0.90, profile=[],
         note="底本把诗句分行断开，此处补成一句完整引导语，避免读者以为正文被截断"),
    dict(action="keep", confidence=0.91, profile=[], note="巴特勒诗句原样保留；补一句转折承接「进一步考察」"),
    dict(action="compress", confidence=0.75, profile=["c03:partial"],
         note="鞋油/绸缎/金的三个符号保留，对象征推理的复述压紧"),
    dict(action="illustrate", confidence=0.73, profile=["c03:partial", "c04:unknown"],
         note="等式链是三步递进（一夸特小麦 → 三种商品 → 第三种东西），纯文字看不出先后："
              "配一张流程图，正文照旧不动"),
    dict(action="expand", confidence=0.84, profile=["c04:unknown"],
         note="几何类比是理解「化为共同东西」的关键，补一句它与后文「抽掉使用价值」的对应关系"),
    dict(action="compress", confidence=0.70, profile=["c04:unknown"],
         note="老巴尔本引文已在 p016 出现，此处只留结论句"),
    dict(action="keep", confidence=0.76, profile=["c04:partial"], figure_error=True,
         note="「质的差别 / 量的差别」适合并列对照，本想配一张对比图；"
              "模型给的图源码过不了校验，按配图失败处理，正文保持原文"),
    dict(action="keep", confidence=0.92, profile=[], note="短承接段，原样保留"),
    dict(action="compress", confidence=0.78, profile=["c05:unknown"],
         note="「抽掉使用价值」的连锁推理压紧，术语「抽象人类劳动」保留并加粗"),
    dict(action="compress", confidence=0.86, profile=["c04:unknown", "c05:unknown"],
         note="价值的定义段：去掉重复的「即不管以哪种形式……单纯凝结」，保留「幽灵般的对象性」与「社会实体的结晶」"),
    dict(action="compress", confidence=0.77, profile=["c04:partial"],
         note="「先不管形式、只考察价值」的方向说明压紧"),
    dict(action="expand", confidence=0.80, profile=["c06:unknown"],
         note="读者容易把「价值量」与「价值实体」混在一起，补一句提醒区分质与量"),
    dict(action="compress", confidence=0.79, profile=["c07:unknown"],
         note="「越懒越有价值」的误解保留（它是本段的论证枢纽），社会必要劳动时间的定义原样保留，"
              "革除重复的铺陈"),
    dict(action="compress", confidence=0.82, profile=["c08:unknown"],
         note="底本此处与上一段逐字重复（429 字伪重复），按压缩处理并说明"),
    dict(action="illustrate", confidence=0.68, profile=["c08:unknown"],
         note="同一劳动量在丰收年 / 歉收年表现为 8 比 4 蒲式耳：一张柱状图比文字省力，正文照旧不动"),
    dict(action="keep", confidence=0.75, profile=["c09:unknown"],
         note="三条例外压紧为一段；末尾按 C 版形态补了一张结论表，两相抵消，长度与原文基本持平"),
]

# 手写事实锚：从对应底本段里抽出的数字/引文（骨架的 facts 字段，供保真核对）
FACTS: list[dict] = [
    {}, {}, {}, {},
    dict(quotes=["庞大的商品堆积"]),
    {}, {}, {}, {},
    {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {},
    dict(quotes=["社会必要劳动时间"]),
    {},
    dict(numbers=["8", "4", "1823"]),
    dict(quotes=["没有一个物可以是价值而不是使用物品"]),
]

# 段落 → 概念引用（手写），与 concepts.json 的 paragraphs 字段互为反向索引
CONCEPTS: list[dict] = [
    dict(id="c01", name="商品", surface=["商品", "商品堆积"], prereq=[], difficulty_hint=1,
         paragraphs=["p005", "p006", "p012", "p019", "p025"], domain="政治经济学", concept_key="commodity"),
    dict(id="c02", name="使用价值", surface=["使用价值", "财物"], prereq=["c01"], difficulty_hint=2,
         paragraphs=["p007", "p008", "p016", "p017", "p025"], domain="政治经济学", concept_key="use-value"),
    dict(id="c03", name="交换价值", surface=["交换价值"], prereq=["c01"], difficulty_hint=3,
         paragraphs=["p008", "p009", "p012", "p013", "p014", "p020"], domain="政治经济学",
         concept_key="exchange-value"),
    dict(id="c04", name="价值", surface=["价值", "商品价值"], prereq=["c02", "c03", "c05"], difficulty_hint=4,
         paragraphs=["p013", "p014", "p015", "p019", "p020", "p021"], domain="政治经济学", concept_key="value"),
    dict(id="c05", name="抽象人类劳动", surface=["抽象人类劳动", "人类劳动"], prereq=["c02"], difficulty_hint=4,
         paragraphs=["p018", "p019"], domain="政治经济学", concept_key="abstract-labour"),
    dict(id="c06", name="价值量", surface=["价值量", "价值的大小"], prereq=["c04"], difficulty_hint=4,
         paragraphs=["p021", "p022", "p024"], domain="政治经济学", concept_key="magnitude-of-value"),
    dict(id="c07", name="社会必要劳动时间", surface=["社会必要劳动时间", "平均必要劳动时间"],
         prereq=["c06"], difficulty_hint=5, paragraphs=["p022", "p024"], domain="政治经济学",
         concept_key="socially-necessary-labour-time"),
    dict(id="c08", name="劳动生产力", surface=["劳动生产力", "生产力"], prereq=["c06"], difficulty_hint=4,
         paragraphs=["p024"], domain="政治经济学", concept_key="productivity-of-labour"),
    dict(id="c09", name="商品的条件（边界情形）", surface=["社会的使用价值", "为别人的使用价值"],
         prereq=["c01", "c02"], difficulty_hint=3, paragraphs=["p025"], domain="政治经济学",
         concept_key="conditions-of-commodity"),
]

ILLUSTRATION_CANDIDATES: list[dict] = [
    dict(paragraph="p013", kind="流程", why="小麦 → 鞋油/绸缎/金 的等式链是三步递进，纯文字看不出先后"),
    dict(paragraph="p016", kind="对比", why="使用价值有质的差别、交换价值只有量的差别，两组并列对照更省力"),
    dict(paragraph="p024", kind="数量", why="同一劳动量在丰年/歉年、富矿/贫矿的产出对比，表格比文字省力"),
    dict(paragraph="p025", kind="对比", why="使用价值/价值、商品/非商品的四组边界情形适合并列对照"),
]

# ------------------------------------------------------- 手写图定义（PLAN-actions-feedback §4.1）
# 形状与后端产出的 `changes[i].figure` 一致：{kind, source, caption?}；kind ∈ {mermaid, svg}。
#
# 两条口径（§4.1）：
#   - **图不进 rewritten.md**：配图段的正文与底本逐字节相同，图由前端在该段容器内就地插入；
#   - **图只在成功时出现**：失败的那一段（见 FIGURE_ERRORS）没有 `figure`，只有 `figure_error`，
#     且 `action` 记 `keep`（正文照旧）。
#
# mermaid 源码首词必须是图类型关键字（这里 `flowchart`）、不含 ``` 围栏、行数 ≤ 40、字符数 ≤ 2000；
# SVG 必须能被 XML 解析、自包含（不许 `<script>` / `on*` / `foreignObject` / 外部 href）。
# 这些不是「随便写写」——`tests/web/test_fixture_schema.py` 会照 §4.2 的校验口径逐条守住，
# 而前端的 `js/figures.js` 也会再校验一遍（白名单双保险）。
FIGURES: dict[str, dict] = {
    "p013": {
        "kind": "mermaid",
        "caption": "小麦的多种交换价值 → 同一个共同东西",
        "source": (
            "flowchart TD\n"
            '  A["一夸特小麦"] --> B["x 量鞋油"]\n'
            "  A --> C[\"y 量绸缎\"]\n"
            '  A --> D["z 量金"]\n'
            '  B --> E["这些交换价值互相代替、同样大"]\n'
            "  C --> E\n"
            "  D --> E\n"
            '  E --> F["等同的共同东西（第三种东西）"]'
        ),
    },
    "p024": {
        "kind": "svg",
        "caption": "同一劳动量：丰收年 8 蒲式耳 / 歉收年 4 蒲式耳",
        # 自包含 + 只用 currentColor（跟着主题走，不写死颜色）：整份 SVG 就是一根基线 + 两根柱子
        "source": (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 150" width="320" height="150"'
            ' role="img" aria-label="同一劳动量在丰收年与歉收年的产出对比">'
            '<g fill="none" stroke="currentColor" stroke-width="1.5">'
            '<line x1="40" y1="120" x2="300" y2="120"/>'
            '<rect x="60" y="40" width="52" height="80"/>'
            '<rect x="170" y="80" width="52" height="40"/>'
            "</g>"
            '<g fill="currentColor" font-size="12" text-anchor="middle">'
            '<text x="86" y="32">8 蒲式耳</text>'
            '<text x="196" y="72">4 蒲式耳</text>'
            '<text x="86" y="138">丰收年</text>'
            '<text x="196" y="138">歉收年</text>'
            '<text x="160" y="16">同一劳动量</text>'
            "</g>"
            "</svg>"
        ),
    },
}

# 配图失败的样本（§4.1）：一句话原因进 `figure_error`，`action` 记 keep、正文照旧。
FIGURE_ERRORS: dict[str, str] = {
    "p016": "mermaid 源码首词不是图类型关键字（模型返回的是说明文字，不是图），按配图失败处理",
}

# ------------------------------------------- 事实锚申报样本（§14.1，2026-10-14）
# 「事实锚可申报 + 可见」的两条可见面都在夹具里留样本：
#   - `changes[i].fact_declarations`：模型逐条申报本段**没有原样保留**哪几项事实锚；
#   - `changes.fidelity` 的 `fact_anchor_policy` / `waived`：`ar check` 按读者口径把**已申报**的缺失放行。
# 这里声明的是 p004：底本那一行的编号「1．」在本稿里改成导读标题后不再出现（编号已在 p001 出现），
# 所以申报放弃数字 1。**这条申报是算出来的、不是手抄的**：脚本会在下面断言
# 「申报的每一项在底本里真的有、在手写改写稿里真的没有」——申报与实测不符就不许落盘。
# 口径取 `allow_declared_drops`（要结论）：这个样本正是「申报后放行」那一档；
# `keep_all`（严口径）下同一份申报会被判违规（`ar check` 退出码 4），后端用例守着那条。
FACT_ANCHOR_POLICY = "allow_declared_drops"
DECLARATIONS: dict[str, dict] = {
    "p004": {
        "numbers": ["1"],
        "note": "底本的编号「1．」在本稿里换成导读标题（编号已在 p001 出现），本段不再重复这个数字",
    },
}

# ------------------------------------- 兜底补齐样本（2026-10-14 复核修复的可见面）
# 后端在「这一段涉及的概念**读者有自评**、模型却没给 `why.profile` 引用（漏键 / 给错形状 /
# 该给引用却给空数组）」时，照**读者自己的自评**兜底补齐那一段的 `why.profile`，并在
# `why.note` 末尾追加一句出处，同时在本段产物上留一个**结构化标记**
# `changes[i].profile_refs_filled: true`（**为真才出现**，照 `figure` / `deferred` 的先例；
# **不进** `decisions.json`）——页面那枚「骨架兜底补齐（模型没给引用）」徽标只认这个字段。
#
# 下面那句出处**逐字照抄** `ar.rewrite.run_rewrite` 里追加的那一句：抄错一个标点，
# 夹具就不再是「与后端真实形态一致」的样本（`tests/web/test_fixture_schema.py` 守的是形状，
# 文案由这条常量与后端同源——本文件是**手造样例**，做不到自动对账，所以只留一处常量）。
PROFILE_REFS_FILLED_NOTE = "（依据画像由骨架兜底补齐：模型没有给 why.profile 引用，这里照读者自评逐条列出）"
#: 哪几段是「兜底补齐」样本。**只能是一段**：`test_fixture_schema.py` 明确断言只有一条，
#: 前端那枚徽标的渲染分支留一个样本就够（多几条只会让「哪条是样本」变模糊）。
PROFILE_REFS_FILLED = ("p005",)

#: 事实锚的抽取口径（与 `ar.fidelity` 同一条正则；这里只用来核对上面的申报，别拿它做别的）
NUMBER_PATTERN = re.compile(r"\d+(?:,\d+)*(?:\.\d+)?")

EXPLAIN: dict = {
    "text": (
        "「越懒越有价值」这个推论，错在把两个人的劳动直接当成同一种东西来比。\n\n"
        "打个比方：同样做一张桌子，新手花 10 小时，老手花 2 小时。按「花了多少时间」算，"
        "新手的桌子该贵 5 倍——可市场上没人愿意出这个价。因为决定价值的不是「某个人实际花了多久」，"
        "而是「在现有社会条件下、按平均的熟练程度和强度，做出来需要多久」。这个平均时长就是"
        "社会必要劳动时间。新手多花的那 8 小时不算数。\n\n"
        "所以结论正好相反：懒和笨不会让商品更值钱，只会让这个人在同样的时间里创造出更少的价值。"
    ),
    "degraded": False,
    # 下面两个键（paragraph / kind）是**夹具专用**的信息性字段：后端 /api/explain 不返回它们，
    # 前端据此在夹具模式下提示「这份样例对应哪一段、哪种 kind」。响应里其它键严格照后端形状：
    # scope 每次响应都给；cached / selection_truncated 只在为真时出现（这份样例两者都不为真）；
    # selection_from 是**请求**字段，不属于响应（2026-10-08 复核修）。
    "paragraph": "p022",
    "kind": "simpler",
    "scope": "selection",
}


def build() -> dict:
    raw = SOURCE_FILE.read_text(encoding="utf-8")
    paragraphs = split_paragraphs(raw)
    assert len(paragraphs) == 25, f"底本空行切分应为 25 段，实际 {len(paragraphs)}"
    assert len(REWRITTEN) == len(paragraphs), "手写改写稿段数必须与底本段数一致"
    assert len(CHANGES) == len(paragraphs)
    assert len(FACTS) == len(paragraphs)

    # `SAME_AS_SOURCE` 占位：装配时填**原文**，保证「配图段正文与底本逐字节相同」这条口径
    # 是算出来的、不是手抄的（抄一遍就可能抄错，而 §4.1 要求逐字节相等）。
    rewritten = []
    for i, text in enumerate(REWRITTEN, start=1):
        pid = f"p{i:03d}"
        if text is SAME_AS_SOURCE:
            assert pid in FIGURES or pid in FIGURE_ERRORS, f"{pid} 用了 SAME_AS_SOURCE 却没有图定义"
            rewritten.append(paragraphs[i - 1])
        else:
            rewritten.append(text)

    meta_paragraphs = []
    for i, text in enumerate(paragraphs, start=1):
        meta_paragraphs.append({
            "id": f"p{i:03d}",
            "index": i,
            "text": text,
            "text_hash": text_hash(text),
        })

    # 长度对照（人工复核用）。标注规则：长度比 = 改写字符数 / 底本字符数；
    # 比例带 [0.85, 1.15] → keep，>1.15 → expand，<0.85 → compress；
    # 底本不足 60 字符的结构性短块（标题/分隔线/诗行引子）不受比例带约束，按意图标注。
    # 配图段（illustrate，含配图失败退回 keep 的那一段）不受比例带约束：图不进 md，正文＝原文。
    report = []
    mismatches = 0
    for i, (src, rw) in enumerate(zip(paragraphs, rewritten), start=1):
        ratio = len(rw) / len(src) if src else 0.0
        action = CHANGES[i - 1]["action"]
        pid = f"p{i:03d}"
        if action == "illustrate" or pid in FIGURE_ERRORS:
            expect = None
            assert rw == src, f"{pid} 是配图段，正文必须与底本逐字节相同"
        elif len(src) >= 60:
            expect = "expand" if ratio > 1.15 else ("compress" if ratio < 0.85 else "keep")
        else:
            expect = action
        flag = "" if expect in (None, action) else f"  <<< 与比例带不符（应为 {expect}）"
        if flag:
            mismatches += 1
        report.append((pid, len(src), len(rw), ratio, action, flag))

    changes = []
    for i, spec in enumerate(CHANGES, start=1):
        pid = f"p{i:03d}"
        entry = {
            "paragraph": pid,
            "action": spec["action"],
            "why": {"profile": spec["profile"], "note": spec["note"]},
            "confidence": spec["confidence"],
            "facts": {
                "numbers": FACTS[i - 1].get("numbers", []),
                "quotes": FACTS[i - 1].get("quotes", []),
            },
        }
        # 图：只在成功时出现（照 `deferred` 的「为真才出现」先例）；失败时只给 `figure_error`
        if pid in FIGURES:
            assert spec["action"] == "illustrate", f"{pid} 有 figure，action 必须是 illustrate"
            entry["figure"] = dict(FIGURES[pid])
        if pid in FIGURE_ERRORS:
            assert spec["action"] == "keep", f"{pid} 是配图失败样本，action 必须是 keep（正文照旧）"
            assert pid not in FIGURES, f"{pid} 不能既成功又失败"
            entry["figure_error"] = FIGURE_ERRORS[pid]
        # `deferred`（枚举内、本期未实现）本批之后不再有：compress / illustrate 都实现了
        assert "deferred" not in entry, f"{pid} 还挂着 deferred"
        if pid in DECLARATIONS:
            entry["fact_declarations"] = dict(DECLARATIONS[pid])
        if pid in PROFILE_REFS_FILLED:
            # 兜底补齐样本（2026-10-14）：标记「为真才出现」+ `why.note` 追加那一句出处。
            # **补齐出来的引用不可能是空数组**——后端只在「读者对这一段涉及的概念有自评」时才补齐
            # （没有可引用的自评时压根不走那条分支），所以这里先断言夹具自己没写错。
            assert entry["why"]["profile"], f"{pid} 标了兜底补齐，却没有自评引用（后端不会这样产出）"
            assert PROFILE_REFS_FILLED_NOTE not in entry["why"]["note"], f"{pid} 的 note 里已经有那句出处"
            entry["why"]["note"] = entry["why"]["note"] + PROFILE_REFS_FILLED_NOTE
            entry["profile_refs_filled"] = True
        changes.append(entry)

    # 申报样本要**对得上实测**：申报的每一项都得「底本有、改写稿没有」，否则这份夹具在骗自己
    for pid, declaration in DECLARATIONS.items():
        index = int(pid[1:]) - 1
        source_numbers = NUMBER_PATTERN.findall(paragraphs[index])
        rewritten_numbers = NUMBER_PATTERN.findall(rewritten[index])
        for number in declaration.get("numbers", []):
            assert number in source_numbers, f"{pid} 申报的数字 {number!r} 在底本里没有"
            assert number not in rewritten_numbers, f"{pid} 申报放弃了 {number!r}，但改写稿里还有它"

    fidelity = {"checked": True, "mismatches": [], "fact_anchor_policy": FACT_ANCHOR_POLICY,
                "waived": [
                    {
                        "paragraph": pid,
                        "kind": "numbers",
                        "missing": list(declaration.get("numbers") or []),
                        "detail": f"数字：申报 {len(declaration.get('numbers') or [])} 项，"
                                  f"按 {FACT_ANCHOR_POLICY} 放行",
                    }
                    for pid, declaration in DECLARATIONS.items()
                    if declaration.get("numbers")
                ]}

    doc = {
        "meta": {
            "source_id": SOURCE_ID,
            "ingest": {
                "kind": "local_file",
                "path": SOURCE_REL,
                "hash": "sha256:" + hashlib.sha256(SOURCE_FILE.read_bytes()).hexdigest(),
                "at": INGEST_AT,
            },
            "paragraphs": meta_paragraphs,
        },
        "rewritten": "\n\n".join(rewritten),
        "changes": {
            "changes": changes,
            # 形状样本（不是真的跑过校验）：口径 + 放行条目都留了样本，见上面 DECLARATIONS 的说明
            "fidelity": fidelity,
        },
        "concepts": {
            "concepts": CONCEPTS,
            "illustration_candidates": ILLUSTRATION_CANDIDATES,
        },
    }

    out_dir = SAMPLES / SOURCE_ID
    out_dir.mkdir(parents=True, exist_ok=True)
    # ``newline=""`` 是必须的：``Path.write_text`` 默认做通用换行转换，
    # Windows 下会把 JSON 里的 ``\n`` 写成 CRLF ⇒ 夹具在工作区变成纯 CRLF，
    # 与仓库里存的 LF 版本只差行尾（``git status`` 显示 M、``git diff`` 却是空的假改动）。
    # 夹具是**跨平台的字面量**，行尾必须与仓库里那份逐字节一致。
    (out_dir / "doc.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="")
    (out_dir / "explain.json").write_text(
        json.dumps(EXPLAIN, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="")

    src_total = sum(len(p) for p in paragraphs)
    rw_total = sum(len(p) for p in rewritten)
    counts: dict[str, int] = {}
    for spec in CHANGES:
        counts[spec["action"]] = counts.get(spec["action"], 0) + 1

    print(f"source_id={SOURCE_ID}")
    print(f"paragraphs={len(paragraphs)} 手写改写段={len(rewritten)}")
    print(f"底本总字符={src_total} 改写总字符={rw_total} "
          f"差={src_total - rw_total} ({(1 - rw_total / src_total) * 100:.1f}% 短)")
    print(f"action 分布={counts}")
    print(f"图={ {pid: spec['kind'] for pid, spec in FIGURES.items()} } "
          f"配图失败={sorted(FIGURE_ERRORS)}")
    print(f"事实锚申报样本={sorted(DECLARATIONS)} 口径={FACT_ANCHOR_POLICY} "
          f"放行条目={[row['paragraph'] for row in fidelity['waived']]}")
    print(f"兜底补齐样本={sorted(PROFILE_REFS_FILLED)}（同段带 profile_refs_filled 标记）")
    print(f"doc.json={out_dir / 'doc.json'}")
    print(f"比例带不符条数={mismatches}")
    print("--- 逐段长度对照 (id, src, rw, rw/src, action) ---")
    for row in report:
        print(f"{row[0]}  src={row[1]:4d}  rw={row[2]:4d}  ratio={row[3]:.2f}  {row[4]}{row[5]}")
    return doc


if __name__ == "__main__":
    build()
