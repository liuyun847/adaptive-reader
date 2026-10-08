# adaptive-reader · 走通骨架实施契约

> 上位设计：同目录 [DESIGN.md](DESIGN.md)（数据模型 §4、阅读器交互 §5、降级纪律 §6、测试 §7 均以它为准，本文件不重复）。
> 本文件由协调者维护，是**执行子代理的唯一契约**：接口、目录、命令、验收标准都以此处为准；与 DESIGN.md 冲突时以本文件为准（本文件更新更晚），并把冲突回报给协调者。
> 依据：用户 2026-10-05 对 DESIGN.md §10 六问的拍板 + 「先做更小的走通骨架」的选择。

## 1. 已拍板（2026-10-05）

| # | 事项 | 结论 |
|---|---|---|
| 1 | 「严格档」（逐句可回溯规格） | **一期不做，但接口预留**：重写引擎按 `mode` 留插口，默认 `free`（自由重述 + 事实锚）；不实现 `strict` |
| 2 | 图表方案 | 方向定为 **mermaid + SVG**（结构/流程类走 mermaid，需要精确排版或自定义图形时走 SVG），**本骨架不做配图**，二期落地 |
| 3 | 范围 | **先做走通骨架**（见 §2），链路验证过之后再补齐测验、四动作全量、行为回流、配图、网页/PDF 入口 |
| 4 | 模型接入 | OpenAI 兼容 HTTP + 项目内配置；**密钥不进任何被提交的文件** |
| 5 | 画像 | YAML，落 `profiles/<读者 id>/profile.yaml`（便于手改 `overrides`） |
| 6 | 压缩提示 | 只在读完时提示，读中不常驻 |
| 7 | 概念清单复用 | 一期按单篇独立，字段预留跨材料合并（`domain` / `concept_key`） |
| 8 | 项目名 | 沿用目录名 `adaptive-reader` |

## 2. 骨架范围

**做**（一条命令链能跑完，产物全部落盘）

1. 摄取：本地 Markdown/TXT → `source.md` + `source.meta.json`，段落锚点 `pNNN` + `text_hash` 稳定可复现。
2. 概念清单：调模型抽概念、前置关系、配图机会点 → `concepts.json`。
3. 最小画像：`profiles/<reader>/profile.yaml`，支持手写与「自评勾选」（不做自动出题）。
4. 逐段决策 + 改写：`decisions.json` → `rewritten.md` + `changes.json`；动作先只实现 **expand / keep**（compress / illustrate 留枚举位与占位分支，遇到即按 keep 处理并在 `changes.json` 标注 `"deferred": true`）。
5. 事实锚校验：数字 / 引号引文 / 书名号三类 + 段缺失检查，违规非零退出并点名段 id。
6. 阅读器：改写版主栏 + 原文右侧抽屉（默认折叠、跟随当前段）+ 段落动作标记 + 讲解卡片（按需生成）。
7. 本地服务：静态前端 + JSON API，能触发上面各步。

**不做**（明确留给下一批）

自动出题与三层测验 · 行为信号回流与局部重跑 · 配图与图表渲染 · 压缩动作 · 网页/PDF/剪贴板入口 · 跨材料概念复用 · 多人账号。

## 3. 技术栈与目录

- 运行时：**系统 Python 3.12.7**（`C:\Users\MLTZ\AppData\Local\Programs\Python\Python312\python.exe`）+ uv 管项目（`pyproject.toml`）；`.python-version` 写 `3.12.7`，让 uv 命中已装解释器、不触发 Python 下载。pip 镜像已是阿里云（`%APPDATA%\pip\pip.ini`），装包可达。
- 服务端依赖：`starlette` + `uvicorn`（路由 / 静态托管 / SSE）、`httpx` + `httpx-sse`（模型客户端）、`PyYAML`（画像）；开发依赖 `pytest`（＋`pytest-cov`）、`playwright`（＋`pytest-playwright`）。**不允许**引入其它运行期依赖。退路：若装包受阻，改纯标准库 `http.server` + `urllib` 并自写 SSE 解析（工作区先例 `clash-relay\relay.py`）。
- 前端零构建：`web/` 下纯 HTML/CSS/ES 模块，由服务端静态托管；**不引入打包器**（同工作区先例 `deepseek-chat\app\`：5,811 行原生 ES 模块 + 单文件 CSS，已交付真机）。
- 无头 E2E：用 `playwright` 启动**本机已装的 Edge**（`channel="msedge"`），**禁止触发浏览器内核下载**（本机网络受限）。playwright 固定 **`1.39.0`**：与已缓存内核 `chromium-1084`（119.0.6045.9）和本机 `pytest-playwright 0.7.2` 匹配；**绝不运行 `playwright install`**。备选：Node CDP，照 `deepseek-chat\tools\smoke.mjs` 的整套做法（就绪靠页面内显式标志、不靠 sleep；不用 `--dump-dom`/`--screenshot` 判状态；报错靠 `Log.entryAdded` 抓）。
- 图表（二期预留）：本机已有离线 mermaid 构建 —— `deepseek-chat\app\js\vendor\mermaid.min.js`（mermaid 12.0.0，普通 `<script>` 引入后全局 `mermaid`）。二期直接复制进 `web/vendor/` 即可，不引 CDN。校验靠浏览器内 `await mermaid.parse(code,{suppressErrors:true})`，并注意 `mermaid.render` 可能 resolve 出空串、同页并发渲染 id 必须唯一。
- 目录（对 DESIGN §3.3 的两处偏离，已在此冻结：① `server/` 改为 **src 布局 `src/ar/`** —— uv + hatchling + console script，照同工作区 `text2video` 的先例，CLI 因此是 `uv run ar …` 而不是 `python -m ar`；② 新增 `data/` 存放运行产物）：

```
adaptive-reader/
  DESIGN.md  PLAN-skeleton.md  README.md
  pyproject.toml  .python-version  config.example.json  config.local.json(不进版本库，运行时自填)
  src/ar/          Python 包：摄取 / 分析 / 决策 / 改写 / 事实锚 / HTTP 服务 + CLI 入口
  web/             阅读器前端（静态，零构建）
  profiles/local-default/profile.yaml
  samples/<source_id>/   夹具产物（前端可脱离后端独立跑）
  data/<source_id>/      运行产物落盘处
  tests/                 后端测试（tests/web/ 归前端）
```

- 配置：`config.json` 读 `{ base_url, model, temperature, timeout_s, max_retries }`，默认值取本机实测可用的组合（`https://api.commandcode.ai/provider/v1` + `deepseek/deepseek-v4.1-flash`，参数面照 `test_api\ttft-bench\bench.mjs`：`POST {base}/chat/completions`、`stream:true`、`stream_options.include_usage` 被网关拒时降级重试、超时 + 最多 1 次重试、错误信息脱敏）。密钥优先级：环境变量 `AR_API_KEY` > `config.local.json` 的 `api_key`；**只在运行时取值**（可从 `~\.dsh\.credentials.yaml` 的 `CMD_API_KEY` 注入环境变量，取值与使用过程不得打印、不得落盘）。**日志、报错、样例文件、产物元数据里都不得出现密钥值**。产物元数据须记录 `model` 与提示词版本（DESIGN §7 回归基线）。

## 4. 冻结接口

命令行（每步都能单独重跑，符合 DESIGN §3.1「任一模块都能单独重跑」）：

```
uv run ar ingest  <文件路径> --id <source_id>
uv run ar analyze <source_id>
uv run ar rewrite <source_id> --reader local-default   # 内部先 decisions.json 再 rewritten/changes
uv run ar check   <source_id>                          # 事实锚 + 锚点稳定性，违规非零退出
uv run ar serve   --port 8765
```

落盘产物（字段沿用 DESIGN §4，逐字不改名）：

| 文件 | 关键字段 |
|---|---|
| `data/<id>/source.md` | 源文本原样，只读 |
| `data/<id>/source.meta.json` | `source_id` / `ingest{kind,path,hash,at}` / `paragraphs[{id,index,text,text_hash}]` |
| `data/<id>/concepts.json` | `concepts[{id,name,surface[],prereq[],difficulty_hint,paragraphs[],domain,concept_key}]` / `illustration_candidates[{paragraph,kind,why}]`，`kind ∈ 流程/结构/对比/数量` |
| `data/<id>/decisions.json` | `[{paragraph, action, why{profile[],note}, confidence}]`，`action ∈ expand\|compress\|keep\|illustrate` |
| `data/<id>/rewritten.md` | 改写稿（段序与源一致，段落之间空行分隔，段首不写 id） |
| `data/<id>/changes.json` | `changes[]`（含 `paragraph`/`action`/`why`/`confidence`/`facts`/`deferred?`）+ `fidelity{checked,mismatches[]}` |
| `profiles/<reader>/profile.yaml` | DESIGN §4.3 原样（`concepts` / `preferences` / `overrides`） |

HTTP（前端只依赖这些）：

```
GET  /                      → web/index.html
GET  /api/doc/<source_id>   → { meta, rewritten, changes, concepts }
GET  /api/profile           → { profile }（默认读者 local-default）
POST /api/profile/rating    → { concept_id, state, note? } 更新画像（写回 YAML）
POST /api/explain           → { source_id, paragraph, kind: "explain"|"simpler"|"detail" } → { text, degraded? }
GET  /api/health            → { ok, model_configured }
```

前端**双模式**（写进契约，便于前后端并行开发）：

- 正式联调走上表 `/api/*`，页面参数**冻结为 `?source=<source_id>`**；`GET /api/doc/<id>` 的响应体允许带一个**可选**的 `degraded` 字符串数组（可见降级清单），前端逐条显示为横幅；
- **夹具模式**：URL 带 `?fixture=<source_id>` 时，页面改读同源静态文件 `samples/<source_id>/doc.json`（内容＝`GET /api/doc` 的响应体）与 `samples/<source_id>/explain.json`（＝`POST /api/explain` 的响应体）。用于脱离后端做无头验证，也是 W2 与 W1 并行开发的解耦点。

锚点规则（全系统骨架）：`id = "p" + 三位序号`（从 1 起，按源文档空行切分的段序）；`text_hash = sha256(规范化文本)[:8]`，规范化 = 去首尾空白 + 合并连续空白 + 全角空格转半角。**重新摄取同一文件必须得到同一组 id**（有测试）。

## 5. 里程碑与验收

| 里程碑 | 交付 | 验收（必须实际跑过并贴出证据） |
|---|---|---|
| M1 摄取与锚点 | `ingest` + 单测 | 对同一文件跑两次，`paragraphs[].id` 集合与 `text_hash` 完全一致（`at` 字段除外）；人为改一个字段重摄取后能被检出 |
| M2 概念清单 | `analyze` + 契约校验 | `concepts.json` 字段齐全；每个概念的 `paragraphs` 引用都存在于 `source.meta.json`；`kind` 只在四类内；**负向**：把某个 `paragraphs` 改成不存在的 id，校验必须报错（非零退出） |
| M3 决策与改写 | `rewrite` + `check` | `decisions.json` 每段都有 `why`；`rewritten.md` 段数/段序与源一致；**负向**：往改写稿副本改掉一个数字，`check` 必须非零退出并指出段 id（参照 `学习\_整理\selfcheck.py` 的「临时副本注入 + 注入生效断言」写法） |
| M4 阅读器 | `web/` + `serve` + 无头验证 | 页面加载样例后：主栏显示改写稿；抽屉默认折叠、展开后显示**当前段**原文；段落标记悬停显示动作与依据；点「没懂」当场出讲解卡片；模型不可用时出现**可见**降级文案（不是静默空白）。无头验证用 playwright 驱动本机 Edge（夹具模式即可，不依赖后端），断言页面内显式就绪标志而不是 sleep |
| 端到端 | 一条命令链 + README | 对 `学习\_第1章第1节三版对照\00-底本.md` 跑通 ingest → analyze → rewrite → check → serve；**并用真服务打开页面（`?source=<id>`，不是夹具模式）**确认前后端联调通过；README 里写清完整命令与产物路径 |

**全局纪律**：任何降级都必须可见（DESIGN §6）——模型超时/未配置时，未改写的段落显示原文并标「未改写」，不允许静默空白或假装成功。

**退出码（2026-10-05 对抗性复核后硬化，README §6 同步为准）**：`0` 成功 · `2` 用法/输入错误（含**校验所需产物缺失 / 不可读 / 为空**，与「`rewritten.md` 缺失」同一语义）· `3` 未实现（`rewrite --mode strict`）· `4` 校验失败（锚点、概念清单契约、事实锚，以及**检查器空跑：0 比对项**）· `5` 模型未配置或整篇调用失败 · `6` **有段落因模型问题未被改写**（`rewrite` 与 `check` 同码；产物可用但不完整，必须能重跑补齐）。

两条纪律（都是 DESIGN §6 的落地）：

1. **任何降级都必须反映在退出码或可见产物上**——"跑了但没做"不许报成功。事实锚只能证明"没改坏"，证明不了"真的改写了"，所以"有段落没被改写"必须由退出码 6 与 `model_errors` 暴露；其余不改变退出码的降级（空画像、缺概念清单、未知动作）必须写进 `degraded[]` 或产物字段。
2. **比对项为 0 ＝ 检查器空跑 ＝ 失败**（空文档不算通过）。`fidelity.checked=true` 只在 `check` 真正比对（`compared > 0` 且无不一致）后才成立；`rewrite` 阶段落 `checked:false`。

## 6. 分工与写范围

| 路 | 范围 | 写目录 |
|---|---|---|
| W1 | 项目骨架 + M1 + M2 + CLI + 模型客户端 + **HTTP 层（`/api/*` + 托管 `web/`）** + 后端 `README.md` | `pyproject.toml`、`.python-version`、`config.example.json`、`src/ar/`、`tests/`（除 `tests/web/`）、`README.md` |
| W2 | M4 前端 + `samples/` 夹具 + 独立无头验证（吃 §4 冻结 schema 与夹具模式，不依赖 W1 进度） | `web/`、`samples/`、`tests/web/` |
| W3 | M3（决策 + 改写 + 事实锚）+ 端到端串联 + DESIGN.md 状态与 §10 决策更新（等 W1 结算后再派） | `src/ar/`（决策/改写/事实锚模块）、`data/`、`DESIGN.md` |

## 7. 样例材料与坑（侦察结论）

- 主样例：`学习\_第1章第1节三版对照\00-底本.md`（5,290 字符 / 按空行切分 **25 段**，其中 3 段是文件头块（标题、引用块、`---`），`p004` 起是正文 22 段；含 1 段 429 字爬虫伪重复，即 `p022`＝`p023`）。同目录 `C-现代重述.md` 是「自由重述」的形态参照（比底本短 42%，带导读与结论表），`B-可读改写.md` 是严格档形态参照。
- **前端夹具 `samples/capital-v1-ch01/` 的 action 分布不是骨架 rewrite 会产出的分布**：它有意覆盖 UI 三个显示分支（含 15 段 `compress` + `deferred:true`，页面显示「骨架未实现该动作，按保持处理」）。夹具是手造样例（`samples/README.md` 已声明），真实产物由端到端那一步产出。
- 规模压力样例（骨架不用）：`学习\原文\01-资本论第一卷_第1章_商品.md`（37,184 字符 / 199 段）——留给骨架跑通之后。
- 可搬的做法：`学习\_整理\selfcheck.py`（负向对照 harness：临时副本注入 + 「注入未生效」断言 + 只看退出码）、`spec_check.py` 的数字多重集校验（正则 `\d+(?:,\d{3})*(?:[．.]\d+)?`、先剥文件头与整理者注、两侧基线必须是**直接输入**）、`terms.py` 的 `count_safe()` 跨词误匹配排除（术语统计必踩：如「劳动日」被算成「劳动」）。
- 不可搬：`coverage.py` 的路径契约与 `清理底稿`/`原文` 两级基线（那是它自己那条变换链的规矩），本项目的基线是 **摄取输入 vs 改写稿**，对齐骨架是段 id 而不是字符 offset。
- `学习\_整理\coverage.py` 只把「未找到」计入退出码，不校验「存疑新句」——本项目若复用该思路，自己补这条断言。

## 8. 风险与降级

| 风险 | 处理 |
|---|---|
| 模型超时/未配置 | 未改写段显示原文并标「未改写」；`/api/health` 明确 `model_configured`；不静默 |
| 概念清单抽取失败 | 退化为「无画像」模式并显式提示（DESIGN §6） |
| 中文难度量尺不成熟 | 只当辅助信号，不做验收标准 |
| 「读着舒服」≠「读懂了」 | 骨架阶段不做理解力测验，但 README 必须写明这一条限制，别让人误以为跑通即可用 |
