# adaptive-reader · 后端骨架

给一段材料，先摸清读者知道什么，再把材料改写成更贴合他的版本；读的时候原文收在右侧抽屉，
遇到不懂的点标记弹讲解卡片。设计见 [DESIGN.md](DESIGN.md)，实施契约见 [PLAN-skeleton.md](PLAN-skeleton.md)。

**当前状态（W3，2026-10-05）：五个子命令全部可用，端到端一条命令链跑通并已真机验收**

| 已实现 | 尚未实现（接口留位） |
|---|---|
| M1 摄取与稳定锚点（`ingest`） | 严格档（`rewrite --mode strict` → 明确报错 + 退出码 3，不静默按 free 跑） |
| M2 概念清单抽取与契约校验（`analyze`） | 压缩 / 配图两个动作（枚举位已留，遇到按 `keep` 处理并标 `deferred`） |
| **M3 逐段决策 + 改写（`rewrite`）** | 读前测验、行为回流、局部重跑 |
| **M3 事实锚校验（`check` 的数字 / 引号引文 / 书名号《》+ 段缺失）** | 配图与图表渲染（二期走 mermaid + SVG）、网页 / PDF 入口 |
| 最小画像（`profiles/local-default/profile.yaml`，自评写回 + 手写 `overrides`） | 跨材料概念复用（一期按单篇独立） |
| 模型客户端（OpenAI 兼容流式，密钥不落盘、报错脱敏） | 前端 `web/` 由另一路（W2）负责，本文档不覆盖它的实现 |

真机验收（2026-10-05，模型 `deepseek/deepseek-v4.1-flash`）：
`data/capital-v1-ch01/` 是**真实产物**（25 段 / 12 概念 / 展开 8 段 + 保持 17 段，34 个数字 + 11 条引文 +
13 个书名号全部与原文一致），`uv run ar check capital-v1-ch01` 退出码 0；
真服务页面（`?source=capital-v1-ch01` + 本机 Edge）与 `/api/explain` 三种 kind 均实测通过，
截图在 `data/capital-v1-ch01/_verify/`。

## 1. 环境与安装

系统 Python **3.12.7**（`C:\Users\MLTZ\AppData\Local\Programs\Python\Python312\python.exe`）+ uv 管项目；
`.python-version` 写死 `3.12.7`，让 uv 命中已装解释器、不触发 Python 下载。pip 镜像已是阿里云。

```powershell
cd adaptive-reader
uv sync          # 建 .venv 并安装依赖（starlette / uvicorn / httpx / httpx-sse / PyYAML）
uv run ar --help
```

开发依赖含 `playwright==1.39.0`（配已缓存内核与 `pytest-playwright`）。
**绝不运行 `playwright install`**：本机不下载浏览器内核，无头验证用本机已装的 Edge（`channel="msedge"`）。

## 2. 命令（契约 §4 冻结）

```
uv run ar ingest  <文件路径> --id <source_id>     # 摄取 → source.md + source.meta.json
uv run ar analyze <source_id>                     # 调模型抽概念 → concepts.json
uv run ar analyze <source_id> --check-only        # 只校验已有 concepts.json，不调模型
uv run ar rewrite <source_id> --reader local-default   # 逐段决策 + 改写 → decisions.json + rewritten.md + changes.json
uv run ar check   <source_id>                     # 锚点校验 + 事实锚校验（数字 / 引号引文 / 书名号《》+ 段缺失）
uv run ar serve   --port 8765                     # 本地服务：/api/* + 托管 web/
```

`rewrite` 的选项：`--reader`（读者 id，默认 `local-default`）、`--mode free|strict`
（默认 `free`；`strict` 是**预留插口**，一期不实现，遇到即报错 + 退出码 3、不写任何产物）、
`--max-chars-per-batch`（合并请求的粒度，默认 1100 字符：25 段合并成 4–6 次请求，省 token）。

全局选项 `--data-dir` / `--profiles-dir` / `--config` 可写在子命令前或后，
默认分别是 `<项目根>/data`、`<项目根>/profiles`、`<项目根>/config.local.json`。

跑样例材料（《资本论》第一卷第一章第 1 节底本，5,290 字符 / 空行切分 25 块）：

```powershell
uv run ar ingest "..\学习\_第1章第1节三版对照\00-底本.md" --id capital-v1-ch01
uv run ar analyze capital-v1-ch01
uv run ar rewrite capital-v1-ch01 --reader local-default
uv run ar check   capital-v1-ch01
uv run ar serve   --port 8765      # 再打开 http://127.0.0.1:8765/?source=capital-v1-ch01
```

`capital-v1-ch01` 与前端夹具 `samples/capital-v1-ch01/doc.json` 用同一个 source_id，
两种模式（`?source=` / `?fixture=`）指向同一份材料、同一组段号。

## 2.1 端到端实跑记录（2026-10-05 真机）

五步都在 `adaptive-reader/` 下执行，注意 `AR_API_KEY` 只在运行时注入（见 §5）：

| 步 | 命令 | 关键输出 | 产物 |
|---|---|---|---|
| 1 | `uv run ar ingest "..\学习\_第1章第1节三版对照\00-底本.md" --id capital-v1-ch01` | `25 段（p001 … p025）`，`锚点稳定：与上一次摄取完全一致` | `data/capital-v1-ch01/source.md`、`source.meta.json` |
| 2 | `uv run ar analyze capital-v1-ch01` | `概念 12 条，配图候选 4 条` | `data/capital-v1-ch01/concepts.json` |
| 3 | `uv run ar rewrite capital-v1-ch01 --reader local-default` | `25 段（展开 8 / 保持 17）`；`请求 6 次；tokens：prompt 9885 / completion 25402 / 合计 35287`；`段序：与原文一致` | `data/capital-v1-ch01/decisions.json`、`rewritten.md`、`changes.json` |
| 4 | `uv run ar check capital-v1-ch01` | `逐段比对 25 段——数字 34 项 / 引号引文 11 项 / 书名号《》13 项，全部与原文一致；没有段缺失`，**退出码 0** | 结果写回 `changes.json` 的 `fidelity` |
| 5 | `uv run ar serve --port 8765` | 页面 `?source=capital-v1-ch01`：主栏 25 段真实改写稿、抽屉跟随当前段、无 JS 报错、无降级横幅 | 截图 `data/capital-v1-ch01/_verify/*.png` |

**token 结算**：`analyze` 1 次请求；`rewrite` 6 次请求（25 段合并、并发发 4–6 批）；
`/api/explain` 真机每次 1 次请求。合计真机调用 **10 次**（analyze 1 + rewrite 6 + explain 3）。

`_verify/serve.log` 是那一次真服务运行的 uvicorn 日志（HTTP 请求行，不含密钥与正文）。

## 3. 产物与数据模型

落盘位置（字段名与 [DESIGN.md](DESIGN.md) §4 一致，逐字不改）：

| 路径 | 内容 |
|---|---|
| `data/<id>/source.md` | 源文本**原样**（按字节复制，只读） |
| `data/<id>/source.meta.json` | `source_id` / `ingest{kind,path,hash,at}` / `paragraphs[{id,index,text,text_hash}]` |
| `data/<id>/concepts.json` | `concepts[{id,name,surface,prereq,difficulty_hint,paragraphs,domain,concept_key}]` + `illustration_candidates[{paragraph,kind,why}]` + `meta` |
| `data/<id>/concepts.raw.txt` | 只在抽取失败时出现：模型原始输出（排查用） |
| `data/<id>/decisions.json` | `decisions[{paragraph,action,why{profile,note},confidence}]`（**每段一条**）+ `meta{action_counts,deferred_count,profile_used,request_count,usage,degraded,…}` |
| `data/<id>/rewritten.md` | 改写稿（段序与源一致、段间空行分隔、段首不写 id）；未实现的动作按保持处理，即该段是原文 |
| `data/<id>/changes.json` | `changes[{paragraph,action,why,confidence,facts{numbers,quotes},deferred?}]` + `fidelity{checked,mismatches,counts,…}` + `meta{…,model_errors,unplanned_paragraphs,rewrite_incomplete}` |
| `data/<id>/_verify/` | 真服务联调的截图与 uvicorn 日志（人工验收材料，不是程序产物） |
| `profiles/<reader>/profile.yaml` | 读者画像（`concepts` / `preferences` / `overrides`） |

`concepts.json` / `decisions.json` / `changes.json` 的 `meta` 是回归基线（DESIGN §7）：
`model`、`prompt_version`（`concepts-v1` / `rewrite-v1` / `explain-v2`）、`generated_at`，
另有 `usage`（token）与 `degraded`（本次所有可见降级）。
**任何元数据里都不含密钥值。**

模块分工（`src/ar/`）：

| 模块 | 职责 |
|---|---|
| `ingest.py` | 摄取、**切分规则**、锚点哈希、重摄取锚点对照（M1） |
| `anchors.py` | `verify_source()`：元数据与源文重新对齐（`ar check` 的锚点部分） |
| `config.py` | 配置加载、密钥解析（`AR_API_KEY` > 文件）、`redact()` 脱敏 |
| `model.py` | OpenAI 兼容流式客户端（httpx + httpx-sse，参数降级 + 重试 + 脱敏） |
| `prompts.py` | 提示词与版本号（回归基线的一部分） |
| `analyze.py` | 概念清单抽取、整理（repairs）与契约校验（M2） |
| `rewrite.py` | 逐段决策（`planDecisions`）、改写（`rewrite`）、事实锚复核（`check_fidelity`）（M3） |
| `fidelity.py` | 事实锚抽取与逐段比对（数字 / 引号引文 / 书名号《》+ 段缺失）（M3） |
| `profile.py` | 画像 YAML 读写、自评写入（`overrides` 追记） |
| `explain.py` | `POST /api/explain` 的业务实现（整段 / 选中片段两种作用域，降级可见） |
| `cache.py` | 讲解的进程内 LRU 缓存（成功结果、容量 128、按画像指纹失效） |
| `server.py` | HTTP 层、静态托管（`web/` + `samples/`） |
| `cli.py` | 5 个子命令、退出码、打印（`ingest` 的切分与校验入口都在这条链上） |


### 段落切分与锚点（这套东西是全系统的骨架）

- 切分规则落在 **`src/ar/ingest.py` 的 `split_paragraphs()`**：段落 = 源文档里被**空行**分隔的块，
  从 1 起编号；Markdown 的标题行、引用行、分隔线**同样算段落**（契约按空行切分，不作特殊处理）。
  上文那份底本因此是 **25 段**（文档头 3 块 + 正文 22 块），段号与前端夹具
  `samples/capital-v1-ch01/doc.json` 的 `p001…p025` 逐一对齐——
  `tests/test_ingest.py::test_real_sample_anchors_match_frontend_fixture` 会守住这条一致性：
  同一条材料在夹具模式与 API 模式下必须是同一组锚点（DESIGN §4.5）。
- 锚点：`id = "p" + 三位序号`；`text_hash = sha256(规范化文本)[:8]`，
  规范化 = 去首尾空白 + 合并连续空白 + 全角空格（U+3000）转半角。
- **同一文件重复摄取必得同一组 id 与 text_hash**（`ingest.at` 除外）；材料变了会在重摄取时
  逐条点名「段落消失 / 新增 / text_hash 变化」，并提示历史画像需重新校准（DESIGN §6）。
- `src/ar/anchors.py::verify_source()` 是检查入口：把 `source.meta.json` 与 `source.md`
  重新对齐，任何不一致都点名段 id（`ar check` 跑的就是它）。

### 概念清单的契约校验（M2）

写盘前必过 `src/ar/analyze.py::validate_inventory()`：字段齐全、概念 id 唯一且形如 `c01`、
每条 `paragraphs` 的段 id 都存在于 `source.meta.json`、`prereq` 只指向本次输出里的概念、
`illustration_candidates[].kind` 只能是 **流程 / 结构 / 对比 / 数量** 四类。
不通过就**不写盘**并以退出码 4 结束。`ar analyze <id> --check-only` 可随时复验已有产物。

模型输出里指向不存在段 id 的引用会被**过滤并记进 `meta.repairs`**（同时打印到 stderr），
所以落盘文件永远满足契约，而「模型说了不存在的段」这件事仍然看得见。

### 逐段决策与改写（M3）

- `src/ar/rewrite.py::planDecisions()`：把 25 段按 `--max-chars-per-batch`（默认 1100 字符）
  合并成 4–6 批**并发**发请求（省 token），返回的 `decisions` 仍然**每段一条**。
- **只实现 `expand` / `keep`**：模型若给出枚举内的 `compress` / `illustrate`，一律按 `keep` 处理、
  原文照旧，并在 `changes.json` 该段标 `"deferred": true`、在 `meta.degraded` 里点名
  （页面会显示「本骨架未实现该动作，按保持处理」）。
  **枚举外**的动作（模型写了别的词、或干脆没给 `action`）同样按 `keep` 处理，但**不标 `deferred`**
  ——`deferred` 只表示「枚举内的未实现动作」，降级文案与产物里有没有这个键必须一致。
- **降级全部可见**：缺某段的决策 → 补一条 `keep` + `confidence: 0` + 降级说明；
  模型调用失败 → 整批按 `keep` 并把失败原因写进产物；「展开」却没变长或没以句末标点收尾
  （疑似截断）→ 记进 `meta.degraded` 并点名段 id。
- **有段落没被改写 ≠ 成功**（退出码 6）：批次调用失败或模型没给决策时，那些段落退回原文
  ——事实锚只能证明「没改坏」，证明不了「真的改写了」。这类情况 `rewrite` 打印
  `✗ 有段落因模型问题未被改写` 并列出**失败批次与受影响段 id**，以退出码 **6** 结束；
  产物里留机器可读证据：`meta.model_errors[{batch,paragraphs,error}]`、
  `meta.unplanned_paragraphs[]`、`meta.rewrite_incomplete: true`，`meta.degraded` 里也有一条汇总。
  承接这份产物的 `ar check` 以**同一个码 6** 结束（见下）。重跑 `ar rewrite` 即可补齐。
- **空画像时不许静默按另一种逻辑跑**：`decisions.json` / `changes.json` 的 `meta` 里写
  `profile_used: false` 与 `profile_note`，每段 `why.profile` 要么写「难度 N（无画像）」，
  要么在 `why.note` 末尾写「（无画像：本段没有可依据的读者信号）」——
  25 段里 14 段标了难度、11 段写了说明，页面的段落悬停提示与卡片都能看到。
- **`mode` 预留**：`free`（默认，自由重述 + 事实锚）已实现；`strict`（逐句可回溯规格）
  一期不做，`ar rewrite --mode strict` 直接报错 + 退出码 3，**不写任何产物、不发任何请求**。

### 事实锚校验（M3，自由重述唯一的自动保真底线）

`src/ar/fidelity.py` 抽三类锚点，逐段比对（多重集 + **出现顺序**都不许变）：

| 类别 | 抽取规则 | 例子 |
|---|---|---|
| 数字 | 正则 `\d+(?:,\d{3})*(?:[．.]\d+)?`（全角数字 / 句点 / 逗号先归一），与 `学习/_整理/spec_check.py` 同口径 | `1859`、`1696`、`1,859`、`3.14` |
| 引号引文 | 成对 `“…”` / `「…」`（单字引号不算，噪声太大），内容归一空白，长度 2–60 字 | `“庞大的商品堆积”` → `庞大的商品堆积` |
| 书名号 | `《…》`（`[注：…]` 里也认；包住整个书名的反引号先剥一层） | `《政治经济学批判》` → `政治经济学批判` |

比对口径与检查器失灵的防范（照 `学习/_整理/selfcheck.py` 的教训）：

- 先剥掉包住整个书名的反引号（`` `《新币轻铸论。答洛克先生关于提高货币价值的意见》` ``）。
- 比对是**数量 + 顺序**：少一个、多一个、顺序调换都算不一致——只查「有没有出现过」
  会漏掉「把 619811 改成 619812 又保留原数字」这类改动。
- **段缺失 / 多余段**单独检查：改写稿按空行切分后的块数必须与 `source.meta.json` 一致，
  对不上的段一律点名段 id。
- **0 比对项 = 检查器空跑 = 失败**：空文档（`paragraphs: []` + 空 `source.md` + 空
  `rewritten.md`）会让比对循环空转、什么问题都不报。所以「真正进入比对的段数」（`counts.compared`）
  为 0 时主动报 `检查器空跑：…0 比对项不算通过` 并以退出码 4 结束；`source.meta.json` 里 0 段
  同样在锚点校验阶段就报（口径与 `analyze` 把「空数组」当失败一致）。
- `ar check` 把本次**真的比了多少项**写回 `changes.json` 的 `fidelity.counts`
  （`compared` = 真正比过的段数，另记数字 / 引文 / 书名各多少项）并打印出来
  （防「检查器没跑也报通过」）；不过就**非零退出（4）并逐条点名段 id**。
- **`fidelity.checked` 的时机**：`rewrite` 阶段写 `false`（`counts` 留 `null`，
  `summary` 写「尚未校验」），只有 `ar check` **比对项 > 0 且没有任何不一致**时才写 `true` + `counts`
  ——只看 rewrite 产物的人不会误读成「已查过、干净」。校验失败时 `checked` 仍为 `false`，
  但 `mismatches` / `counts` 照样写回（「跑过了、结果是坏的」要留痕）。
- **产物缺失 / 不可读 / 为空**（`changes.json` 没了、不是 JSON 对象、`changes` 不是数组或为空）
  → 退出码 2 并点名缺什么（与「`rewritten.md` 缺失」同一语义）；没得比就不许报通过。
  变更记录**条数与材料段数对不上**（被截断 / 与当前材料不是同一次改写）也按这一类报——
  `ar rewrite` 保证「每段一条」，所以正常产物永远对得上。
- 负向对照（`tests/test_rewrite.py`、`tests/test_no_silent_degradation.py`）：在**临时副本**上
  改数字 / 改引文 / 改书名 / 删一段 / 加一段 / 删 `changes.json` / 清空文档，
  断言「注入确实生效」+ 检查器报错并点名段 id，还原后必须重新通过。


## 4. HTTP 接口

| 方法 与 路径 | 说明 |
|---|---|
| `GET /` | 托管 `web/index.html`；`web/` 不存在时回**写明原因**的 404，不影响 `/api/*` |
| `GET /api/doc/<source_id>` | `{ meta, rewritten, changes, concepts, degraded }`；产物缺失的字段是 `null`，原因写在 `degraded` 里 |
| `GET /api/profile` | `{ profile, degraded }`（默认读者 `local-default`） |
| `POST /api/profile/rating` | `{ concept_id, state, note? }`（`state ∈ known/partial/unknown`）→ 写回 `profile.yaml`，返回 `{ ok, profile }` |
| `POST /api/explain` | `{ source_id, paragraph, kind, selection?, selection_from? }`（`kind ∈ explain/simpler/detail`；`selection_from ∈ rewritten/source`，默认 `rewritten`）→ `{ text, degraded, scope, cached?, selection_truncated?, model?, prompt_version? }` |
| `GET /api/health` | `{ ok, model_configured, model, api_key_origin, web_static }` |

另外挂了一个**只读**的 `/samples/`（指向项目根的 `samples/`）：契约没要求，
但前端夹具模式（`?fixture=<id>` 读 `samples/<id>/doc.json`）因此也能走本服务。
`web/` 与 `samples/` 目录不存在时都只回写明原因的 404，不崩、不影响 `/api/*`。

约定：

- **降级必须可见**（DESIGN §6）：`/api/explain` 在模型未配置或调用失败时返回 200 +
  `degraded: true` + 一段说明文本（前端直接渲染进卡片），不返回空白、不假装成功。
- `degraded` 是数组（`/api/doc`、`/api/profile`）或布尔（`/api/explain`），是**追加**字段：
  §4 冻结的键一个不少，前端可以只读自己认识的部分。
- **选中片段讲解**（`/api/explain` 的追加能力，旧请求体不受影响）：请求体多两个**可选**字段
  `selection`（读者划中的那段文字）与 `selection_from`（`rewritten` 默认 / `source`，只用于在提示词里
  标注片段来自改写稿还是原文；给别的值回 400）。
  - `selection` 缺失或规范化后为空 → **行为与旧版逐字一致**（讲整段，`scope: "paragraph"`）；
  - 规范化口径与锚点一致（去首尾空白 + 合并连续空白 + 全角空格 U+3000 转半角），规范化后上限 **600 字符**：
    超出截断到 600 并在响应里追加 `selection_truncated: true`（**降级必须可见，不许静默截断**）；
  - `selection` 非空 → 讲解对象是**那段片段**，`paragraph` 继续提供上下文（邻段、画像），
    响应里 `scope` 记为 `"selection"`；`selection` **不引入任何新锚点或哈希**。
- `scope` / `cached` / `selection_truncated` 是**追加**字段：`scope`（`paragraph` / `selection`）与
  `selection_truncated` 描述**这次请求要什么**，每次响应都给（前端靠 `scope` 确认「确实按片段讲的」）；
  `cached`（缓存命中）与 `model` / `prompt_version` 描述**这次产出了什么**，只在成功响应
  （`degraded: false`）里出现。降级响应因此是 `{ text, degraded, reason, scope }`——旧键一个不少、
  `reason` 枚举不变。
- **`scope` 的口径例外**：`p404 + selection` 这类「段号不存在」的请求在**段号校验**阶段就走掉了
  （`src/ar/explain.py` 里 `find_paragraph` 早于 `prepare_selection`），所以响应里的 `scope`
  是默认的 `"paragraph"`、也不会出现 `selection_truncated`——**不是请求里带 `selection` 就一定回
  `"selection"`**。前端遇到这种响应会在状态行写明「后端按整段讲的（scope: paragraph）」。
- **讲解缓存**（进程内，`src/ar/cache.py`）：容量 128 的 LRU，键 =
  `(source_id, paragraph, kind, 规范化并截断后的 selection, 画像指纹)`，其中画像指纹 =
  `profiles/<reader>/profile.yaml` 的 `(路径, 字节数, mtime_ns)`。命中时响应追加 `cached: true`
  且**不再调模型**；**只缓存成功结果**（失败与降级不进缓存）。不落盘、不加 HTTP 缓存头、服务重启即空；
  画像一变（自评写回 / 手改 `overrides`）指纹就变，旧条目自然失效。`selection_from` 不进键（键按契约冻结），
  所以同一段文字按「改写稿 / 原文」各请求一次时，第二次直接命中。
  **缓存只在画像变化时失效，没有「重新生成」入口**：同一段（或同一片段 + 同一 kind）在一个进程里
  拿到的永远是同一份文字，**刷新页面也一样**（缓存不在浏览器里）；要换一份，只能改画像（换指纹）
  或重启服务。前端另有一层**会话内**缓存（`web/js/cache.js`，刷新即空、不落盘），它的键也带画像指纹
  （每次请求前读一次 `GET /api/profile` 的内容摘要）。
- `/api/profile/rating` 同时写两处：`concepts[<cid>].state`（当前状态）与
  `overrides[]`（手动结论，按 DESIGN 优先级最高、行为信号不得覆盖）。
  自评给的把握度是 `confidence = 0.7`：自评只反映「以为知道」，所以不给满分。
- 静态前端与 `/api/*` 同源；前端夹具模式（`?fixture=<source_id>` 读 `samples/<id>/doc.json`）
  不需要后端，见 PLAN §4。

## 5. 配置与密钥纪律

`config.example.json` 是模板；`config.local.json`（**不进版本库**）覆盖默认值：

```json
{ "base_url": "https://api.commandcode.ai/provider/v1",
  "model": "deepseek/deepseek-v4.1-flash",
  "temperature": 0.2, "timeout_s": 90, "max_retries": 1, "max_tokens": null, "api_key": "" }
```

- 默认端点与模型取本机实测可用的组合；参数面照 `test_api/ttft-bench/bench.mjs`：
  `POST {base_url}/chat/completions`、`stream: true`、`stream_options.include_usage`
  （**被网关拒绝时自动关掉该参数重试，且不占用重试额度**）、单次超时 + 最多 `max_retries` 次重试。
- 本项目自用的 `config.local.json` 目前只覆盖一项：`temperature: 0.2`
  （改写要的是照抄事实，温度低更稳；`config.example.json` 里的默认是 0.6）。
- 密钥优先级：环境变量 `AR_API_KEY` > 配置文件 `api_key`；**只在运行时读取**。
  任何文件、日志、报错、样例、产物元数据里都不会出现密钥值——所有对外文本都过
  `src/ar/config.py::redact()`（抹掉密钥值、`Bearer xxx`、`sk-xxx` 与超长令牌串）。
- `api_key` 为 `""`（或未设 `AR_API_KEY`）= 模型未配置：`/api/health` 的 `model_configured` 为 false，
  `ar analyze` 以退出码 5 结束，`/api/explain` 返回可见降级文案。

## 6. 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 用法 / 输入错误（文件不存在、source_id 非法、JSON 坏了；`check` 找不到改写稿、**缺 `changes.json`、产物不是对象或 `changes` 为空数组**也是这一类） |
| 3 | 功能尚未实现（目前只剩 `rewrite --mode strict`：严格档接口预留） |
| 4 | 校验失败（锚点、概念清单契约、**事实锚**；含**检查器空跑**：0 比对项 / 空文档；逐条列出问题并点名段 id / 概念 id） |
| 5 | 模型未配置或调用失败（整篇都没跑起来） |
| 6 | **模型失败导致有段落未被改写**：批次调用失败或模型没给决策，那些段退回原文。`rewrite` 与 `check` 同码——`check` 的事实锚全对也照样报 6 |

码 6 的单一口径：**「有段落没被改写」以退出码 6 暴露**，`rewrite` 与 `check` 一致。
`rewrite` 打印 `✗ 有段落因模型问题未被改写` + 失败批次 + 受影响段 id；
`check` 打印同样的明细（先做完事实锚比对再报 6）。重跑 `ar rewrite` 补齐后两处都回到 0。
`degraded` 里的其它降级（空画像、缺概念清单、疑似截断、未知动作）**不改变退出码**——
它们不影响「这一段到底改没改」。

## 7. 测试

```powershell
uv run pytest tests -q            # 后端用例（167 passed）：默认不收集 tests/web（前端用例单独跑）
python -m pytest tests/web -q     # 前端无头用例（45 passed + 1 skipped，W2 提供，需要本机 Edge）
```

`tests/web` 里有两条**真服务联调**用例打 `http://127.0.0.1:8765`（跑之前先 `uv run ar serve --port 8765`；
**服务没起就 skip，不是失败**）：

- `test_real_service_page_and_explain_payload_without_calling_the_model`：**默认就跑**。它用
  `page.route` 把 `POST /api/explain` 拦下来核对请求体（`source_id` / `paragraph` / `kind` /
  `selection` / `selection_from`），一个模型调用都不发；
- `test_real_service_explain_end_to_end_is_billable`：**默认 skip**，要显式开开关才跑：

  ```powershell
  $env:AR_E2E_BILLABLE = "1"; python -m pytest tests/web -q -k billable
  ```

  代价是**一次真实的 `/api/explain` 模型调用**（历史记录里 12–46 秒、单次计费；后端进程内缓存
  命中时不再调模型，卡片状态行会标「后端缓存命中」，那一次不计费）。开关之外的任何运行都不会
  产生模型费用。

后端用例全部使用**假模型**（注入固定响应或 `monkeypatch`），默认不发真实网络请求；
负向对照照 `学习/_整理/selfcheck.py` 的做法：只在临时副本上注入缺陷 + 断言「注入确实生效」
+ 只看退出码。事实锚的负向对照在 `tests/test_rewrite.py`，覆盖数字 / 引文 / 书名 / 段缺失 / 多余段。

`tests/test_explain_scope.py` 守**选中片段作用域与讲解缓存**（2026-10-08）：
`selection` 缺失时与旧版逐字一致、非空时片段确实进了发往模型的 messages、规范化口径与锚点一致、
超 600 字符截断并上报 `selection_truncated`、缓存命中不再调模型、画像文件一变即失效、
失败与降级不进缓存、LRU 容量/淘汰/线程安全、三条既有降级路径的响应键集不变。

`tests/test_no_silent_degradation.py` 专门守**静默降级与检查器空跑**（2026-10-05 对抗性复核
实测出的五处缺陷，每处至少一条负向对照）：删 / 清空 `changes.json` 后 `check` 必须非零退出、
模型整批失败时整条链必须退出码 6 且产物留 `model_errors`、0 比对项必须报「检查器空跑」、
`rewrite` 不许写 `fidelity.checked: true`、未知动作的文案必须与产物一致（不宣称标了 `deferred`）。

`tests/web` 用**系统 Python** 跑（不是 uv 的 `.venv`）：`tests/web/conftest.py` 的浏览器夹具
只找本机 Edge 与已缓存内核，**任何情况下都不触发内核下载**。

## 8. 已知限制（别把「跑通」当「可用」）

1. **不做理解力测验**：调研实测「主观觉得好读」与「真的读懂」相关系数不到 0.1，
   本骨架没有任何测量读者是否真的读懂的手段（读者侧测验属后续里程碑）。
2. **只有一个能用的档位**：`free`（自由重述 + 事实锚）。严格档（逐句可回溯）接口留了位但没实现，
   跑 `--mode strict` 会明确报错——**别把它当成「跑过就是严格档」**。
3. **压缩 / 配图没实现**：枚举位在，遇到按 `keep` 处理并标 `deferred`；
   枚举外的动作按 `keep` 处理但**不标** `deferred`；
   真机跑 `capital-v1-ch01` 时模型没给出这两个动作，所以产物里 `deferred_count = 0`。
4. **事实锚只覆盖数字 / 引号引文 / 书名号《》三类**，且是**按段、按数量与顺序**的严格比对：
   人名、术语、转述无法自动守住；把事实挪到别的段落、在同一段里重复引用同一句引文、
   顺序调换，都会报成违规——**会有假阳性**，报出来时必须人工看一眼原文，不要一律当成模型改坏。
   真机第一轮就踩到过一条：模型在展开段里把「形成价值的实体」这同一个引文引了两次，
   `check` 退出码 4 点名 `p021`；收紧提示词（同一段里同一引文只能出现原文那么多次）后通过。
5. **中文难度量尺不成熟**：`difficulty_hint` 只是模型给的辅助信号，不作验收标准。
6. **空画像时依据很弱**：`profiles/local-default/profile.yaml` 现在是空的，
   决策只能依据概念清单的难度提示（产物里已显式标注）。要让改写「因人而异」，
   得先写 `concepts` 自评或用页面的自评勾选（`POST /api/profile/rating`）攒出画像。
7. **单读者**：画像格式按「任何读者都能用」设计，但当前只服务 `local-default` 一个人。
8. **token 成本随材料线性增长**：25 段约 4.5 万 token（analyze + rewrite）；
   读中按需展开（讲解卡片）是另一笔按次付费的账，目前每次 12–46 秒。
9. `data/` 与 `profiles/` 的**运行产物不进版本库**；若将来把本项目纳入 git，请自行
   `.gitignore` 掉 `data/`、`config.local.json` 与 `profiles/` 的实际取值。
10. **退出码 6 只说明「有段落没被改写」，不含「改写质量」**：模型整批失败 → 整篇保持原文
    （退出码 6）；模型正常但全篇都判 `keep` 也是**合法结果**（退出码 0，真机 25 段里就有 17 段
    是模型主动保持）。二者靠 `meta.model_errors` / `meta.unplanned_paragraphs` 区分，
    不靠「展开段数」——**0 个展开段不等于失败，有 model_errors 才是**。
11. **部分失败需要人工重跑**：退出码 6 的那次产物已经落盘、能读（失败批次退回原文），
    但没有「只重跑失败批次」的增量入口，只能整篇 `ar rewrite` 重跑（局部重跑属后续里程碑）。
12. **讲解缓存只在进程内**：容量 128，**服务重启即空**，按**画像指纹**（`profile.yaml` 的路径 + 字节数 +
    mtime_ns）失效；多进程 / 多实例部署时各持一份（本骨架就是单进程 uvicorn）。它算的是「别为同一件事
    重复付费」的账，不是持久化结果——**别把缓存当产物**：`data/` 里没有它，重启后第一次讲解照旧重新调模型。
    它**没有「重新生成」入口**：同一段重复点、刷新页面都拿到同一份文字（要换只能改画像或重启，见 §4）。
13. **显示设置允许手动组合出读不清的正文**（背景 3 档 × 文字 2 档，6 种组合里有 3 种是低对比：
    纸白 / 暖黄 + 浅灰、深色 + 深墨）。这类组合不拦（用户有权这么选），但设置面板里会显示实测对比度
    告警（阈值 AA 4.5:1）；「浮在内容之上的面」（顶栏 / 徽标 / 抽屉 / 卡片 / 浮条 / 设置面板）按**背景档**
    配对底色与文字，与文字颜色档无关，所以面板里的正文类文字在任何组合下都 ≥ 4.5:1。
