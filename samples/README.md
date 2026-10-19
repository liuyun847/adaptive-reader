# samples/ — 前端夹具（**手造样例，不是模型产出**）

这些文件是 W2（阅读器前端）用来脱离后端做无头验证的夹具，也是 W1/W3 对照 `PLAN-skeleton.md` §4
冻结 schema 的参考样例。

> ⚠️ **它们是手写的，不是任何模型跑出来的结果。** 改写稿、`changes`、`concepts` 全部由
> `_build/make_fixture.py` 里的手写常量装配而成，不得当成「骨架 rewrite 的真实输出」引用，
> 也不得据此判断改写质量。

## 目录

```
samples/
  capital-v1-ch01/
    doc.json               = GET /api/doc/capital-v1-ch01 的响应体（{ meta, rewritten, changes, concepts }；
                             含两个 illustrate 的 figure 样本、一个 figure_error 失败样本、
                             一个事实锚申报样本（p004）与一个「依据画像兜底补齐」样本（p005））
    explain.json           = POST /api/explain 的响应体（单段样例，见下）
    survey-questions.json  = GET /api/survey 的 questions[] 部分（读前背景问卷的题目，见下）
    survey.json            = GET /api/survey 的完整响应体（version / answered_version / answers，见下）
    survey-followup.json   = POST /api/survey/followup 的响应体（模型追问，**手写**样例，见下）
  _build/
    make_fixture.py  夹具生成器（手写内容 + 锚点计算 + 长度复核报表）
  _verify/
    reader-fixture.png / reader-fixture-collapsed.png   阅读器无头验证截图（M4 验收用，**2026-10-05 那一次**）
    crosscheck.txt                                     悬停文案 vs changes.json 的逐字核对记录（同一次）
```

> `_verify/` 是**历史核对存档**（2026-10-05 跑的那一次）。2026-10-13 这批把 `p013` / `p016` / `p024`
> 的形态改了（配图、配图失败、去掉 `deferred`），所以那两张截图与那份核对记录**不再代表当前夹具**：
> 它们保留下来只作「当时确实逐字核对过」的证据，要复核当前形态请重跑
> `python -m pytest tests/web -q`（`test_fixture_schema.py` / `test_reader_ui.py` / `test_figures.py`）。

## 材料来源与段数

- 底本：`学习/_第1章第1节三版对照/00-底本.md`（5,290 字符，CRLF 下 5,341 字节）。
- 切段：按**空行**切分 → **25 段**（`p001`–`p025`），完全照 `PLAN-skeleton.md` §4
  「按源文档空行切分的段序」执行，`text` 为切分后原样文本。
- 与 `PLAN-skeleton.md` §7 侦察结论「22 段」的差异：那 22 段是**剥掉文件头**（标题行、`> 底本/说明`
  引用块、`---` 三条）之后的正文段数；本夹具按契约字面规则保留文件头，因此是 25 段。
  两者只差这 3 条头部块，`p004` 起完全对应。
- `p022` 与 `p023` 在底本里逐字重复（各 429 字，侦察结论里点名的「爬虫伪重复」段）。

## 锚点

| 规则 | 实现 |
|---|---|
| `id` | `"p" + 三位序号`，从 1 起，按空行切分段序 |
| `text_hash` | `sha256(规范化文本)[:8]` |
| 规范化 | 去首尾空白 + 合并连续空白 + 全角空格（U+3000）转半角 |

`tests/web/test_fixture_schema.py` 会从 `meta.paragraphs[].text` 重算 `text_hash` 并断言一致，
所以夹具的锚点自洽性是被测试守住的，不靠人工。

## 改写稿形态

照 `学习/_第1章第1节三版对照/C-现代重述.md` 的自由重述形态写：带导读、体例说明从简、
引证合并、末尾一张结论表；全文比底本短 **33.5%**（5,240 → 3,482 字符）。
段数与段序与底本一致（25 段），改写段 N 对应底本段 N。

### `changes.action` 的标注规则

长度比 = 改写段字符数 / 底本段字符数。比例带 `[0.85, 1.15]` → `keep`；`> 1.15` → `expand`；
`< 0.85` → `compress`。底本不足 60 字符的结构性短块（标题、分隔线、诗行引子）不受比例带约束，
按意图标注。`_build/make_fixture.py` 会打印逐段长度对照并在不符时标 `<<<`，当前不符条数 = 0。

分布：`keep` 7 / `expand` 4 / `compress` 12 / `illustrate` 2。

**配图段不受比例带约束**（2026-10-13 起）：图不进 `rewritten.md`，`illustrate` 段的正文与底本
**逐字节相同**（`p013` / `p024`），所以它们的长度比恒为 1.00。装配脚本用 `SAME_AS_SOURCE` 占位符
从底本填进去，**不手抄**——抄一遍就可能抄错，而契约要求逐字节相等。

### 配图与配图失败（2026-10-13 新增，PLAN-actions-feedback §4.1）

| 段 | 形态 | 说明 |
|---|---|---|
| `p013` | `action: illustrate` + `figure: {kind: "mermaid", source: "flowchart TD …", caption}` | 等式链的三步递进；`source` 首词是图类型关键字、无 ``` 围栏、≤40 行 |
| `p024` | `action: illustrate` + `figure: {kind: "svg", source: "<svg …>", caption}` | 丰收年 / 歉收年的 8 比 4；SVG 自包含、只用 `currentColor`（跟着主题走） |
| `p016` | `action: keep` + `figure_error: "mermaid 源码首词不是图类型关键字…"` | **配图失败样本**：正文照旧、没有 `figure`，前端据此走「退回文字说明」那条路 |

- 图**内联在 `changes[i].figure`** 里，没有单独的 `figures.json`；`figure` 只在成功时出现。
- 两段的 `figure.source` 都必须过后端 §4.2 的白名单口径，`tests/web/test_fixture_schema.py`
  逐条守着（mermaid 首词/行数/字符数、SVG 的 XML 合法性与「无 script / on\* / foreignObject / 外部 href」）。
- `concepts.illustration_candidates` 里给这三段都留了候选条目（说明当初为什么想配图）。

> **`deferred` 与本夹具的关系（2026-10-13 之后）**：`PLAN-skeleton.md` §2.4 的 `deferred`
> 表示「枚举内、本期未实现」。这两批把 `compress` 与 `illustrate` 都实现了，所以夹具里
> **一个 `deferred` 都没有**（`tests/web/test_fixture_schema.py` 断言 `"deferred" not in change`）。
> 机制本身仍留在 schema 里给将来的枚举成员，页面也仍在渲染「本骨架未实现该动作」那一行——
> 那条显示由 `test_reader_ui.py::test_deferred_note_still_shows_for_a_future_unimplemented_action`
> 注入一份变更记录守着（夹具不该为了测它而伪造现实里不会出现的字段）。

### 事实锚申报样本（2026-10-14 新增，PLAN-actions-feedback §14.1）

「事实锚可申报 + 可见」的两条可见面在夹具里各留一个样本：

| 位置 | 内容 |
|---|---|
| `changes[p004].fact_declarations` | `{numbers: ["1"], note: "底本的编号「1．」在本稿里换成导读标题（编号已在 p001 出现），本段不再重复这个数字"}` |
| `changes.fidelity.fact_anchor_policy` | `allow_declared_drops`（要结论：**已申报**的缺失放行） |
| `changes.fidelity.waived[]` | 一条 `{paragraph: "p004", kind: "numbers", missing: ["1"], detail: "数字：申报 1 项，按 allow_declared_drops 放行"}` |
| `changes.fidelity.over_declared[]` | 空数组（夹具里这份申报与实测对得上；**多申报**那一条由前端用例临时改一份副本注入，不伪造进夹具） |

- 选 `p004` 的原因：它在底本里只有一个数字（行首编号「1．」），改写稿里**确实没有**这个数字、
  也没有多出任何事实锚——「申报与实测对得上」这一点由脚本在生成时**算一遍再断言**
  （`make_fixture.py` 的申报样本一段），不靠手抄。
- `fidelity` 那一块仍是**形状样本**（`checked: true` 是手写声明，不是真跑过校验的产物）：
  本夹具是手造样例，只保证「形状与后端一致 + 申报自洽」。
- 页面据此渲染两处：卡片里的「本段申报（事实锚）」+「已按读者口径放行」徽标，以及
  悬停该段动作标记时的申报那一行；口径若是 `keep_all`，徽标改成「未放行」，多申报改成
  「本段一个锚点都没少：模型多申报了」。
  守卫用例：`tests/web/test_fact_declarations_ui.py`。

## 依据画像兜底补齐样本（2026-10-14 新增）

后端在「这一段涉及的概念**读者有自评**、模型却没给 `why.profile` 引用（漏键 / 给错形状 /
该给引用却给空数组）」时，照**读者自己的自评**兜底补齐 `why.profile`，并在本段产物上留一个
**结构化标记** `changes[i].profile_refs_filled: true`（**为真才出现**，照 `figure` / `deferred`
的先例；**不进** `decisions.json`）——页面那枚「骨架兜底补齐（模型没给引用）」徽标只认它。

| 位置 | 内容 |
|---|---|
| `changes[p005].profile_refs_filled` | `true`（**只有这一段**：守卫用例断言夹具里恰好一条样本） |
| `changes[p005].why.profile` | `["c01:unknown"]`（补齐出来的引用**不可能是空数组**——没有可引用的自评时后端压根不走那条分支） |
| `changes[p005].why.note` | 手写依据 + 那一句出处（逐字同 `ar.rewrite` 追加的那句） |

- **由生成器装配**（`make_fixture.py` 的 `PROFILE_REFS_FILLED` / `PROFILE_REFS_FILLED_NOTE`）：
  此前这个字段只在 `doc.json` 里手改过、生成器**不产出**——照本文「重新生成夹具」那条命令
  跑一遍，样本就会消失，`tests/web/test_fixture_schema.py::test_profile_refs_filled_sample_is_well_formed`
  当场变红。现在夹具是生成器的产物，手改与生成不会再分叉。
- 前端据此渲染：卡片「依据画像」那一行的徽标 + 悬停多一行出处说明（守卫用例
  `tests/web/test_profile_display.py`，含「`why.note` 里一个字不含『兜底补齐』也得挂出来」的负向对照）；
  **在线压缩之后这枚徽标就地摘掉**（产物那一段换成全新记录，已经没有这个键），见 `web/README.md`。

## explain.json

契约规定它是 `POST /api/explain` 的**响应体**（不是多段的字典），所以整个夹具只有一份讲解，
对应 `p022`（「越懒越有价值」那个误解）。响应体里多带了两个**夹具专用**的信息性字段
`"paragraph": "p022"` 与 `"kind": "simpler"`；前端在夹具模式下若发现请求的段与它不一致，
会在卡片里显示一行「夹具样例对应 p022」的说明，不假装这就是那段自己的讲解。
**后端不返回这两个键**——真实响应里只有 `text` / `degraded` / `scope`（+ 成功时的
`model` / `prompt_version`、降级时的 `reason`，以及**为真才出现**的 `cached` /
`selection_truncated`）。`selection_from` 是**请求**字段，不属于响应，这里不再出现
（2026-10-08 复核修：之前误把请求字段写进了响应夹具，还显式写了后端从不返回的
`cached: false` / `selection_truncated: false`）。`tests/web/test_fixture_schema.py` 会守住这两条。

## survey-questions.json

读前背景问卷（`PLAN-background-survey.md`）的题目夹具：读取方式与 `doc.json` 同一套
（前端在 `?fixture=<id>&survey=1` 时读 `samples/<id>/survey-questions.json`），
**不是手写的**——它由后端的题目定义直接导出，所以夹具模式渲染出来的题与正式模式一致：

```
uv run python -c "import json,sys; sys.path.insert(0,'src'); from ar.survey import QUESTIONS, SURVEY_VERSION; print(json.dumps({'version': SURVEY_VERSION, 'questions': QUESTIONS}, ensure_ascii=False, indent=2))" > samples/capital-v1-ch01/survey-questions.json
```

题目或选项一变（后端升 `SURVEY_VERSION`）就按上面这条重导一次；`tests/web/test_survey_ui.py`
里的 `test_fixture_questions_file_matches_backend_definition` 会把这份文件与后端 `QUESTIONS`
逐项比对，两边一漂移就红。

## survey.json

`GET /api/survey` 的**完整响应体**夹具（`version` / `answered_version` / `questions` / `answers` /
`skipped` / `degraded`）。只有 `?fixture=<id>&survey=fixture` 才会读它，用来在无后端时覆盖
「回填已存答案」与「问卷已更新」两条分支（示例里 `answered_version` 比 `version` 小 1）。
题目部分是后端导出的同一份；那份演示作答是手选的一组合法取值，用后端的函数拼：

```
uv run python -c "import json,sys; sys.path.insert(0,'src'); from ar.survey import QUESTIONS, SURVEY_VERSION, finalize_background, normalize_background, answers_of; A={'education':'undergraduate','high_school_electives':['physics','chemistry','biology'],'discipline':'economics','major':'国际经济与贸易','strengths':['economics_finance','programming_computer'],'strengths_note':'读过《资本论》第一卷前两章','reading_materials':['textbook','papers'],'preference':'example'}; B=normalize_background(finalize_background(dict(A))); print(json.dumps({'version':SURVEY_VERSION,'answered_version':SURVEY_VERSION-1,'questions':QUESTIONS,'answers':answers_of(B) or None,'skipped':False,'degraded':[]}, ensure_ascii=False, indent=2))" > samples/capital-v1-ch01/survey.json
```

## survey-followup.json

`POST /api/survey/followup` 的**响应体**夹具（`questions[{q,hint}]` / `degraded` / `prompt_version`）。
与 `doc.json`、`explain.json` 一样是**手写的**（本机没有可复现的模型输出可导，不能假装它是
模型产出）：题目文本取自后端测试 `tests/test_survey.py` 的 `FOLLOWUP_JSON` 样例。
`text` / `usage` 这类「这次模型产出了什么」的可选键**故意不写**——手写夹具不该伪造 token 账。
夹具模式点「让模型追问几个细节（可选）」时读它；`tests/web/test_survey_ui.py` 会用它驱动
追问输入区的渲染，并核对每题都带 hint。

## 用法

- 夹具模式（不需要后端）：打开 `web/index.html?fixture=capital-v1-ch01`，
  页面会读同源静态文件 `samples/capital-v1-ch01/doc.json` 与 `explain.json`。
- 重新生成夹具：`python samples/_build/make_fixture.py`（只读 `学习/` 下的底本，不改它）。
- 无头验证：`python -m pytest tests/web -q`（用本机 Edge，不下浏览器内核）。
