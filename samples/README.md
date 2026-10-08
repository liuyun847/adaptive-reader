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
    doc.json       = GET /api/doc/capital-v1-ch01 的响应体（{ meta, rewritten, changes, concepts }）
    explain.json   = POST /api/explain 的响应体（单段样例，见下）
  _build/
    make_fixture.py  夹具生成器（手写内容 + 锚点计算 + 长度复核报表）
  _verify/
    reader-fixture.png / reader-fixture-collapsed.png   阅读器无头验证截图（M4 验收用）
    crosscheck.txt                                     悬停文案 vs changes.json 的逐字核对记录
```

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
引证合并、末尾一张结论表；全文比底本短 **41.6%**（5,240 → 3,058 字符）。
段数与段序与底本一致（25 段），改写段 N 对应底本段 N。

### `changes.action` 的标注规则

长度比 = 改写段字符数 / 底本段字符数。比例带 `[0.85, 1.15]` → `keep`；`> 1.15` → `expand`；
`< 0.85` → `compress`。底本不足 60 字符的结构性短块（标题、分隔线、诗行引子）不受比例带约束，
按意图标注。`_build/make_fixture.py` 会打印逐段长度对照并在不符时标 `<<<`，当前不符条数 = 0。

分布：`keep` 6 / `expand` 4 / `compress` 15。

> **compress 与骨架的关系（要看的一条）**：`PLAN-skeleton.md` §2.4 规定骨架的 `rewrite` 只实现
> `expand` / `keep`，`compress` 留枚举位、遇到按 `keep` 处理并标 `"deferred": true`。
> 本夹具为了给出「自由重述」形态（短 41.6%），有 15 段是真正被压短的，因此按上面的长度规则
> 标成 `compress` 并带 `"deferred": true` —— 页面会把这种段显示为「压缩（骨架未实现，按保持处理）」。
> 换句话说：**这个夹具的 action 分布不是骨架 rewrite 会产出的分布**，它是有意覆盖 UI 三种显示分支的。

## explain.json

契约规定它是 `POST /api/explain` 的**响应体**（不是多段的字典），所以整个夹具只有一份讲解，
对应 `p022`（「越懒越有价值」那个误解）。响应体里多带了一个信息性字段 `"paragraph": "p022"`；
前端在夹具模式下若发现请求的段与它不一致，会在卡片里显示一行「夹具样例对应 p022」的说明，
不假装这就是那段自己的讲解。

## 用法

- 夹具模式（不需要后端）：打开 `web/index.html?fixture=capital-v1-ch01`，
  页面会读同源静态文件 `samples/capital-v1-ch01/doc.json` 与 `explain.json`。
- 重新生成夹具：`python samples/_build/make_fixture.py`（只读 `学习/` 下的底本，不改它）。
- 无头验证：`python -m pytest tests/web -q`（用本机 Edge，不下浏览器内核）。
