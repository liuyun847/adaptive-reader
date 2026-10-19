# adaptive-reader · 后端骨架

给一段材料，先摸清读者知道什么，再把材料改写成更贴合他的版本；读的时候原文收在右侧抽屉，
遇到不懂的点标记弹讲解卡片。设计见 [DESIGN.md](DESIGN.md)，实施契约见 [PLAN-skeleton.md](PLAN-skeleton.md)。

**2026-10-18 这一批**（`rewrite-v7` + 逐段循环 + 一次性基线）：

1. **提示词重排 + 全文进固定上下文**：材料行 / 概念清单 / 配图候选挪到规则之后，并新增
   `{full_text_block}`（同一材料的所有批 / 所有段共享）与 `{feedback_block}`（校验回灌，空串零插入）。
   前缀从 4433 字符 / 2297 token 涨到 **9889 字符 / 5364 token**，**同一轮的第 2–6 批开始命中缓存**
   （23,040 / 35,535，65%）——2026-10-08「同轮 6 批互不命中」的结论按 v7 作废。
2. **逐段循环 / 逐段对话**：`--per-paragraph` 一段一请求（`--concurrency 1` 串行、默认 4 并发）；
   加 `--conversation` 则改成**同一条对话**——原文与已改内容都留在 `messages` 里，每轮只推进一段。
   两种都是硬校验不过就回灌重试，次数用尽仍不过的段**不写模型稿**（保持原文 + 退出码 6）。
3. **一次性基线**：`ar baseline <id>`，单次调用吃全文、吐全篇，只写 `data/<id>/_eval/`，
   不碰三份主产物；用于和分批 / 逐段做成本与保真对照。
4. **五版对照**（分批 / 逐段串行 / 逐段并发 / 逐段对话 / 一次性）见 §2.1.2：
   分批仍是默认首选；逐段并发用约 4 倍 prompt 换回接近的墙钟；逐段对话更保守（0 展开 / 1 压缩），
   一次性基线最便宜但 25 段只改了 1 段。
5. **修掉一个段数 bug**：改写真身带空行会把 `rewritten.md` 切成多段（逐段模式实测踩到），
   现在并成同一段并记一条可见降级。

**当前状态（2026-10-14）：七个子命令可用（原五个 + 只读的 `ar tools` + `ar feedback`）；「摄取 → 概念 → 改写 →
校验 → 阅读」这条命令链已真机验收**（`ar tools` 只列举工具契约、`ar feedback` 只回流行为信号，
两者**都不参与**这条链，也没有真机验收记录；第四批的压缩 / 配图 / 行为回流
与 2026-10-14 的事实锚申报**只有假模型 + 夹具的逻辑覆盖**；
**局部重跑**另有一次**窄范围**真机记录——2026-10-14 用 `ar rewrite … --only p008` 跑了两档、
共 2 次调用 / 12694 token，验的是「局部重跑这条路本身」与 `rewrite-v6` 的 `why.profile` 措辞 / 保留，
**不是整篇、也不是多轮**，范围与限制见 §8 第 23 条，逐条见 §7、§8）。

最近一批（2026-10-15）把**入口与阅读页拆开**，并把「换材料」这件事从命令行搬到页面上：

1. **`GET /` 变成启动页**（`web/index.html`），四块：① **状态**——原阅读器顶栏那枚「状态」按钮与它的
   面板整体搬来（数据源照旧是同源静态 `web/status.json`）；② **做测试**——**读前背景问卷入口**
   （显示已答 / 已跳过 / 未答 + 作答时间，点按钮**就地打开**问卷，**绝不自动弹**）；
   ③ **历史记录**——材料清单（`data/` 下每份材料的段数、最后动过的时间、动作统计、事实锚校验状态，
   点一条进阅读器）；④ **输入文件**——浏览器选 `.md` / `.txt` 上传 → 后端摄取成新材料。
2. **阅读器页移到 `GET /reader`**（`web/reader.html`，由原 `index.html` 改名而来，只少了状态入口那两处）；
   老链接 `/?source=…`、`/?fixture=…` **302** 到 `/reader?<原样 query>`，一个都没失效。
3. **顺带去掉那条打扰**：答过 / 跳过过背景问卷时，**不再弹**「已答过，本次不再打扰…」的黄色横幅
   （原因只落 `__arDebug.surveyBootReason` 供无头验证断言，`?survey=1` 这条出路照旧）。
4. 为支撑启动页新增 **`GET /api/sources`**（材料清单，**只读、一个字节都不写盘**）与
   **`POST /api/ingest`**（上传文本 → 摄取，**只摄取**、不调模型）；请求 / 响应形状、入参边界与
   降级规则逐条见 §4。
5. 两条端点上线前做过一次**只读对抗复核**，随后按复核结论修掉 **5 条**「本该降级或 400 却 500」与
   「静默」的问题：产物时间戳越界、产物被别的进程独占读不出来、正文编不成 UTF-8、`data/` 下的悬空
   目录链接、`os.replace` 撞上被占用时的误导文案；顺带给 `GET /api/survey` **追加** `answered_at`
   （启动页要显示「已答于 …」）。**未修、留待裁决**的三处小口径与一处摄取并发边界见 §8 第 24 条。
6. 启动页本身也过了一次**只读对抗复核**，按结论修掉 4 个缺陷：**启动页不吃显示设置**（在阅读器里
   选了深色 / 大字号，回启动页仍是纸白，两页底色不一致）、**上传连点会发多次请求**并把「已经摄取成功」
   显示成 409 失败、**非 UTF-8 / 二进制文件被静默按乱码摄取**（现在四处可见：状态行 / 结果块 /
   横幅 / `degradations`）、`__arDebug` 的 `invalid` 档读不到原因且留着上一轮字段；另修若干口径
   （JSON `null` 的措辞、结构不符档缺 `url`、启动页那行说明文案实测对比度不足已调色、
   阅读器页那处已成死判定的 `isStatusOpen()` 已删、阅读器 doc 载入失败现在显示后端原文）。
   前端口径的逐条落点在 [web/README.md](web/README.md)。

用例账：后端 **688 passed / 0 skipped**、前端 **184 passed / 1 skipped**（口径见 §7）；
前端模块与页面的逐项说明见 [web/README.md](web/README.md)。

本轮（2026-10-14，`PLAN-actions-feedback.md` §14.1）把**事实锚从「逐段严格比对」松绑成
「可申报 + 可见」**：

1. 模型在 `changes[i].fact_declarations` 里**逐段申报**「本段没有原样保留哪几项事实锚」
   （数字 / 引号引文 / 书名号，可带一句 `note`）；
2. 放不放行由**读者画像里那条偏好** `pref_fact_anchor` 决定（`keep_all` 严 / `allow_declared_drops`
   松「要结论」），**不是全局开关、也不是一次性 CLI flag**；口径进提示词固定块（提示词升 **`rewrite-v5`**）；
   2026-10-14 收尾把**在线压缩**也接上同一条口径（提示词升 **`compress-v2`**）——
   同一段不会再出现「`ar check` 放行、点『太啰嗦』却被拒」；
3. **三处可见**：产物（`fact_declarations` + `fidelity.fact_anchor_policy` / `waived[]` /
   `over_declared[]`）、命令行（`ar rewrite` 打每段申报、`ar check` 打口径与逐条放行 + 逐条多申报）、
   页面（卡片「本段申报（事实锚）」+ 放行 / 未放行 / 多申报徽标 + 悬停那一行 + 压缩后的横幅）；
4. **没申报的缺失照旧失败（退出码 4）**，多出来的锚点与顺序调换**任何口径都不放行**——
   这条网只松「举例数据可以丢」，不松事实漂移。详见 §3「事实锚『可申报 + 可见』」。

同日（2026-10-14）的**复核修复**把 `why.profile` 这一层收口（证据与查证过程见 §8 第 19 条）：

1. **改掉会自相矛盾的提示词文案**：`ar.rewrite._format_profile_block` 的「空自评」分支不再写
   「画像为空」、也不再指示模型把 `why.profile` 写成空数组 `[]`（背景非空即算「有画像」，
   那句话只说明「读者没有提供概念自评」）；提示词升 **`rewrite-v6`**（规则 11 交代
   `why.profile` 该写什么），单段压缩共用同一个画像块、同升 **`compress-v3`**；
2. **强制保留模型给的引用**：`ar.tools` 的 `_DECISION` 字段语义写明「有自评时必须逐条保留」；
   产出链路对**漏键 / 给错形状 / 该给引用却给空数组**一律可见（`meta.degraded` + CLI 一行账）
   并**只照读者自己的自评**兜底补齐；读者本来没有自评的段**不误报、不补齐**；
3. **三处显示一致**：产物（`why.profile` + `meta.degraded` + 逐段的结构化标记
   `changes[i].profile_refs_filled`）、`ar serve` 页面（空数组显示「依据画像：无自评引用」；
   被兜底补齐的段另挂一枚「骨架兜底补齐（模型没给引用）」徽标 + 悬停一行出处说明）、
   CLI（`why.profile：N 段有引用 / M 段空`）都不许写成「画像为空」。

本轮（第四批，`PLAN-actions-feedback.md`）把四个动作补齐、把读中交互与行为回流接上：

1. **`compress` 动作真实现**（四动作 `expand` / `compress` / `keep` / `illustrate` 全部落地）：
   压缩走「必须更短 + 句末标点收尾」的反向判据，逐段保真跳过名单含 `compress`；提示词升 **`rewrite-v4`**
   （2026-10-14 起现行版本是 **`rewrite-v6`**：多了事实锚口径固定块与逐段申报规则，同日复核修复又加了
   `why.profile` 的语义规则与精确措辞，见本节开头）。
2. **`illustrate` 动作与图渲染**：产物 `changes[i].figure = {kind, source, caption}`（**只在成功时出现**），
   失败则 `action` 退回 `keep` + `figure_error` + 可见降级；**图不进 `rewritten.md`**（该段正文逐字节等于原文），
   由前端 `web/js/figures.js` 在段内插入；mermaid 走 `web/vendor/mermaid.min.js`（**懒加载**，每页最多注入一次），
   SVG 走后端白名单 + 前端 `DOMParser` 白名单；六种失败一律原位退回文字 + `degrade()`。
3. **「太啰嗦」当场压缩** `POST /api/compress` + 压缩缓存（`ar.cache` 的 `COMPRESS_KIND` / `compress_cache_key`，
   与讲解共用同一份 LRU）；成功响应 17 键含 `hint` / `artifacts_updated` / `fact_declarations` /
   `waived[]` / `over_declared[]` / `fact_anchor_policy`，降级 4 键
   `{ok, reason, text, degraded}`；写盘前做**单段事实锚比对**（与 `ar check` 同一套判据与口径，
   见本节开头第 2、3 条），不一致就不写盘。
4. **局部重跑** `ar rewrite --only / --retry-failed / --from-signals`（三者互斥）+ 逐段 patch 合并器
   `merge_artifacts`（`fidelity` 重置、`meta.rerun` / `rerun_history[]`、退出码 6 按**合并后**状态重算）
   与**行为回流**（新模块 `src/ar/events.py` + `POST /api/events` + `profile.applySignals` +
   `ar feedback` dry-run / `--apply`；事件落 `profiles/<reader>/events.jsonl`，**端点只 append、绝不写 `profile.yaml`**）。

**第四批那几批改动本身是零真实模型调用验收的**：全部新逻辑只用假模型（注入固定响应 / `monkeypatch`）、
夹具与路由级假后端验收——这与「逻辑已实现」是两回事，见 §8 第 3 条与 DESIGN §10.2。
（2026-10-14 另补了一次**窄范围**真机验证：`--only p008` 单段局部重跑 × 2 档，
覆盖 `rewrite-v6` 的 `why.profile` 措辞与保留；**没有**覆盖压缩 / 配图 / 行为回流，
也不是整篇重跑——见 §8 第 23 条。）

上一批（第二批）补了**跨材料的读前背景问卷 + 模型追问**：六道固定题（条件题联动、可整场跳过）→
可选的模型追问 → 渲染成**逐字稳定**的「读者素描」注入 `rewrite` 提示词的固定块（提示词升 `rewrite-v3`），
产物 `meta` 追加 `background_used` / `background_fingerprint`。
再上一批补的是「提示词前缀缓存」的模板重排 + 首发预热、缓存用量入产物与 `/api/explain` 响应。

第三批把原先散在四个业务模块里的模型调用收敛成**五个具名工具 + 一个统一调用层**
（新增 `src/ar/llm.py` 与 `src/ar/tools.py`）：重试照 DSH 内置重试的口径（默认 5 次重试、
错误码白名单、500→10000 ms 指数退避 ±10% 抖动），**失败不抛**，逐次尝试落进产物
`meta.attempts` / `meta.retry_account`；CLI 新增**只读**子命令 `ar tools`（第四批在此之上加了第五个工具
`compress_paragraph`）。**原有五个子命令（+ 只读的 `ar tools`）的名称、参数、退出码语义一个没变**，
产物既有字段一个没改（只追加字段）。见 §2、§3、§5.1。

2026-10-13 当晚的**对抗性复核修复**（第三批收尾，见 §3「模型调用的尝试记账」）：
① 失败文案统一成 `模型调用失败（已试 N 次，末次 <CODE>）：…`（`ar.llm.failure_message`），
重试花了几次在 `/api/explain` 降级文案、`analyze` / `rewrite` 报错里都看得见；
② `/api/explain` 的响应追加 `attempts`（成功与降级都给）；
③ CLI 那行账同时打**批数**与**真实请求数 / 重试次数**（`meta.usage.requests` 仍是批数，语义未改）；
④ 工具层入参校验失败改为返回 `INVALID_REQUEST` 失败信封（不再抛异常 → 不再把调用方的 bug 报成 400 / 500）。

同一晚还做了 `PLAN-llm-tools.md` §8.6 的**技术债清理**：`rewrite` 的批次执行删掉了「已有事件循环 →
退回全串行」那条分支，**所有批次（含第 1 批预热）都改到工作线程里执行**，async 调用方与 CLI 走同一条
路径（预热顺序、并发上限 4、切批阈值、产物结构全部不变；`planDecisions` 仍是同步函数）。后端用例随之
到 **394 passed**（第四批之后 514 passed，2026-10-13 复核修复又补了 8 条 ⇒ **522 passed**；同日收尾复核再补 1 条钉缓存路径的用例 ⇒ **523 passed**；2026-10-14 事实锚申报补 20 条 ⇒ **543 passed**，同日收尾（在线压缩接上同一条口径 + 多申报可见）再补 13 条 ⇒ **556 passed**，同日复核修复又补 17 条 ⇒ **573 passed**，同日页面标记（兜底补齐的可见面）再补 1 条 ⇒ **574 passed**，同日**收尾清理**（原子写的坏路径快速失败等 9 条）⇒ **583 passed**，同日复核**补漏**（摄取 / 画像 / 事件流三处写盘入口统一到同一条快速失败口径）再补 14 条 ⇒ 现行 **597 passed**，数字以 §7 为准）。残留（同步 API 的固有语义）：在事件循环线程里调用会阻塞该线程，将来要非阻塞需另加
async 版入口。

| 已实现 | 尚未实现 / 只有逻辑覆盖（无真机验收） |
|---|---|
| M1 摄取与稳定锚点（`ingest`） | 严格档（`rewrite --mode strict` → 明确报错 + 退出码 3，不静默按 free 跑） |
| M2 概念清单抽取与契约校验（`analyze`） | **按本篇材料出题的读前短测验**（`DESIGN.md` §3.1 的 `buildQuiz` / `quiz.json` 那一套） |
| **M3 逐段决策 + 改写（`rewrite`）**：四动作全部实现（`expand` / `compress` / `keep` / `illustrate`），提示词 `rewrite-v7`（全文进固定上下文 + 逐段循环 + 校验回灌） | **网页抓取** / PDF / 剪贴板入口（同一 `SourceDoc` 契约）——纯文本（`.md` / `.txt`）的上传入口已由启动页的「输入文件」补上，但它**只摄取**，不代办 `analyze` / `rewrite` / `check` |
| **M3 事实锚校验（`check` 的数字 / 引号引文 / 书名号《》+ 段缺失）**：2026-10-14 起**可申报 + 可见**（申报放行由读者画像偏好 `pref_fact_anchor` 决定；未申报的缺失照旧退出码 4） | 跨材料概念复用（一期按单篇独立） |
| **配图与图表渲染**（`illustrate` → `changes[].figure`；mermaid 懒加载 + SVG 白名单；失败退回文字） | 页面自评勾选入口（后端 `POST /api/profile/rating` 已可用，前端还没有对应 UI） |
| **局部重跑**（`ar rewrite --only / --retry-failed / --from-signals` + 逐段 patch 合并器） | **效果观测**：「背景 / 行为信号有没有让改写更贴读者」目前没有对照证据（本项目没有理解力测验兜底） |
| **行为回流**（`events.jsonl` → `POST /api/events`（只 append）→ `ar feedback` 折算 → `profile.applySignals`） | 讲解链路注入背景（`/api/explain` 仍只按概念状态讲） |
| **「太啰嗦」当场压缩**（`POST /api/compress` + 压缩缓存 + 单段事实锚比对（**同一档读者口径 + 申报通道**）+ 写回三份产物） | 多人账号 / 按请求传读者（读者 id 取自应用级配置） |
| 最小画像（`profiles/local-default/profile.yaml`，自评写回 + 手写 `overrides`；行为回流只累加计数、不动 `overrides`） | — |
| **读前背景问卷 + 读者素描**（六道固定题 + 条件题联动 + 整场跳过；可选模型追问；答案落 `background`，素描注入 `rewrite` 提示词，产物追记 `background_used` / `background_fingerprint`） | — |
| 模型客户端（OpenAI 兼容流式，密钥不落盘、报错脱敏）+ 前端 `web/`（零构建静态页：**启动页 `GET /`**（状态 / 背景问卷入口 / 材料清单历史 / 上传摄取四块）+ **阅读器页 `GET /reader`**，已实现，口径与用例见 [web/README.md](web/README.md)） | — |

> **第四批的「已实现」= 逻辑已实现 + 假模型 / 夹具覆盖**（后端与前端用例见 §7），
> **不等于真机验收**：压缩、配图、行为回流这三项**没有**跑过真网关（那几批的硬约束就是零真实模型调用）；
> **局部重跑**是唯一例外，但它那条真机记录**很窄**——2026-10-14 只跑了 1 段（`p008`）× 2 档
> （见 §8 第 23 条），既不是整篇也不是多轮。

> **「读前」这个词现在指两件事**：本层是**跨材料的读前背景问卷**（学历 / 高中六选三 / 大学学科门类 /
> 自报熟悉领域 / 常读材料 / 偏好讲法，**不读材料也能答、答一次长期复用**，产物是画像里的 `background`
> 与 `profiles/<reader>/survey.json`）；`DESIGN.md` §3.1/§5 里那个「读前短测验」是**按本篇材料的概念清单
> 出题**（自评 → 辨识 → 复述三层，产物 `quiz.json`），**仍未实现**。两者并列不冲突，见
> [PLAN-background-survey.md](PLAN-background-survey.md) §0。

真机验收（模型 `deepseek/deepseek-v4.1-flash`）：
`data/capital-v1-ch01/` 是**真实产物**（25 段 / 10 概念 / 展开 10 段 + 保持 15 段，34 个数字 + 11 条引文 +
13 个书名号全部与原文一致），`uv run ar check capital-v1-ch01` 退出码 0；
真服务页面（`?source=capital-v1-ch01` + 本机 Edge）与 `/api/explain` 三种 kind 均实测通过，
截图在 `data/capital-v1-ch01/_verify/`（页面联调是 2026-10-05 那次；产物是 2026-10-13 重跑的，
见 §2.1.1，段号与锚点没变，页面照旧可读）。

**读前背景问卷这一层已于 2026-10-13 真机验收通过**（`rewrite-v3` + 读者素描，指纹 `c9037bb4`，
token 账见 §2.1.1）：① `rewrite-v3` 带素描在真网关跑通、事实锚全过（退出码 0，数字 34 / 引文 11 /
书名号 13 全部一致）；② 「追问在真模型上的问法」此前已实测，本轮没有新证据
（唯一依据仍是 `profiles/local-default/survey.json` 里 2026-10-09 那次 5 问 / 1418 token /
`degraded: false` 的记录）；③ 「填背景后重跑的 token 账」照 §2.1.1 的两遍实测数字。
Python 用例仍全用假模型，前端无头用例仍走夹具 / 路由级假后端——逻辑覆盖与真机验收是两回事。

## 1. 环境与安装

系统 Python **3.12.7**（`%LOCALAPPDATA%\Programs\Python\Python312\python.exe`）+ uv 管项目；
`.python-version` 写死 `3.12.7`，让 uv 命中已装解释器、不触发 Python 下载。pip 镜像已是阿里云。

```powershell
cd adaptive-reader
uv sync          # 建 .venv 并安装依赖（starlette / uvicorn / httpx / httpx-sse / PyYAML）
uv run ar --help
```

开发依赖含 `playwright==1.39.0`（配已缓存内核与 `pytest-playwright`）。
**绝不运行 `playwright install`**：本机不下载浏览器内核，无头验证用本机已装的 Edge（`channel="msedge"`）。

## 2. 命令（契约 §4 冻结的五个 + 只读的 `tools` + `feedback` + 对照用的 `baseline`）

```
uv run ar ingest  <文件路径> --id <source_id>     # 摄取 → source.md + source.meta.json
uv run ar analyze <source_id>                     # 调模型抽概念 → concepts.json
uv run ar analyze <source_id> --check-only        # 只校验已有 concepts.json，不调模型
uv run ar rewrite <source_id> --reader local-default   # 逐段决策 + 改写 → decisions.json + rewritten.md + changes.json
uv run ar rewrite <source_id> --only p012,p014    # 局部重跑：只重跑这些段，与既有产物逐段合并
uv run ar rewrite <source_id> --retry-failed      # 局部重跑：只重跑上次没被改写的段（meta.model_errors ∪ unplanned_paragraphs）
uv run ar rewrite <source_id> --from-signals      # 局部重跑：只重跑行为信号算出的受影响段（读 events.jsonl，不写画像）
uv run ar rewrite <source_id> --per-paragraph --concurrency 1   # 逐段循环（串行）：一段一请求 + 校验回灌重试
uv run ar rewrite <source_id> --per-paragraph --concurrency 4   # 逐段循环（并发上限 4）
uv run ar rewrite <source_id> --per-paragraph --conversation     # 逐段对话：原文与已改内容留在同一上下文（强制串行）
uv run ar baseline <source_id> --reader local-default           # 一次性整篇改写基线（只写 data/<id>/_eval/）
uv run ar check   <source_id>                     # 锚点校验 + 事实锚校验（数字 / 引号引文 / 书名号《》+ 段缺失）
uv run ar check   <source_id> --reader local-default   # 口径按这个读者的画像偏好 pref_fact_anchor 判（默认读者）
uv run ar serve   --port 8765                     # 本地服务：/api/* + 托管 web/
uv run ar tools                                   # 列出模型工具契约（只读：名称 / 描述 / 入参出参 schema）
uv run ar tools --json                            # 同上，机器可读 JSON 数组（可被 json.loads 解析）
uv run ar feedback <source_id> --reader local-default          # 行为信号回流：打印将发生的画像变更（dry-run）
uv run ar feedback <source_id> --reader local-default --apply  # 真的写 profile.yaml（内容逐字相同则不写盘）
```

**七个子命令**（合计）：`ingest` / `analyze` / `rewrite` / `check` / `serve` 这五个是契约 §4
冻结面，**名称、参数、退出码语义一个没变**；`tools`（2026-10-13）与 `feedback`（第四批）都是纯增补项。
`rewrite` 第四批**追加**三个**互斥**的可选参数（同给或两两同给 → 退出码 2 + 明确文案；全缺省时行为与整篇改写
**逐字不变**）：

| 参数 | 目标段来源 |
|---|---|
| `--only p012,p014` | 显式段 id 列表（逗号分隔；段 id 格式非法或不在材料里 → 退出码 2） |
| `--retry-failed` | 既有 `decisions.json` 的 `meta.model_errors[].paragraphs` ∪ `meta.unplanned_paragraphs` |
| `--from-signals` | 行为回流产出的「受影响段」：读 `events.jsonl` → `applySignals` → 按 `concepts[].paragraphs` 反查（**不写 `profile.yaml`**，要固化画像得显式跑 `ar feedback --apply`） |

**目标段集为空时**（例如 `--retry-failed` 但上次没有失败段）只打印「没有需要重跑的段」，
**不写盘、不调模型**，以退出码 0 结束（见 §6）。

`check` 在 2026-10-14 **追加**一个**可选**参数 `--reader`（默认 `local-default`）：事实锚口径按
这个读者的画像偏好 `pref_fact_anchor` 判（严 / 松两档见 §3 的「事实锚『可申报 + 可见』」）。
**CLI 恒按这个读者判，不会回退到产物 `meta.reader`**——那条回退只对直接调
`check_fidelity(reader=None)` 的调用方生效；产物 `meta.fact_anchor_policy` 与当前口径不一致时
另给一条可见说明（口径是「现在的标准」，不是「当时的标准」）。
`ar rewrite` 打口径与每段申报、`ar check` 打口径与逐条放行，两处都在 §3 那一节里列了。

`ar tools` 是 2026-10-13 新增的**只读**子命令：不读配置、不调模型、不发请求，
只列举**五个**工具（`analyze_concepts` / `rewrite_batch` / `explain_paragraph` / `survey_followups` /
`compress_paragraph`）的名称、描述、入参出参与提示词版本；`--json` 输出同一份契约的 JSON 数组
（照 `tools.schemas()`）。`compress_paragraph` 是第四批新增的第五个工具（提示词版本
`compress-v3`：2026-10-14 收尾从 `compress-v1` 升到 `compress-v2`，多了事实锚申报通道；
同日复核修复升到 `compress-v3`——它与 `rewrite` 共用同一个「读者画像」块，块措辞一改内容就变了），
`POST /api/compress` 走的就是它。

`ar feedback`（第四批新增，第 7 个子命令）：读 `profiles/<reader>/events.jsonl` → 折算信号 →
`ar.profile.applySignals`（纯函数，不碰磁盘）→ 打印 `changed` / `skipped_overrides` /
`evidence_added` / `affected_paragraphs` / `degraded`。**默认 dry-run**，`--apply` 才写 `profile.yaml`，
且写盘前若序列化文本与现有文件逐字相同就**不写**（一次无谓写盘会换画像指纹 ⇒ 讲解缓存全失效）。
退出码：0 成功（含「无变更」）/ 2 用法或输入错误（材料未摄取、`concepts.json` 缺失这类产物缺失按 2）。
`overrides` 里的概念**整条跳过、一个字符都不改**。

**`--apply` 是幂等的**（2026-10-13 复核修复 F2）：只折算 `profiles/<reader>/feedback.json` 里
还没记过的事件（按事件 id 判），成功后把本次**看过**的该材料事件 id 写回那份文件
（保留最近 500 条）+ `last_at` 水位线 ⇒ 同一份事件重复跑 `--apply` 不再叠加 `score` / `evidence`
（没有它：连跑三次会让 `score` −1.0 → −2.0 → −3.0，把 `state` 推过阈值）。
dry-run 一个字节都不写；水位线文件缺失 / 损坏按「全部事件都未处理」处理并给一条可见降级（不静默）。

**读前背景问卷没有 CLI 子命令**（`cli.py` 的原有五个子命令是冻结面，`tools` / `feedback` 都是增补项、
不干这件事）：填问卷走页面
（`GET/POST /api/survey`，见 §4），或直接手改 `profiles/<reader>/profile.yaml` 的 `background` 块
（形态见 §3）。问卷本身不调模型；「让模型追问」才是模型调用，它只在
`POST /api/survey/followup` 上发生。

**状态报告也没有 CLI 子命令**（同上：原有五个子命令是冻结面，`tools` 只是只读列举契约、`feedback` 只回流行为信号），它是前端的一个辅助脚本：

```powershell
uv run python tools/gen_status.py --note "一句话说明"   # 跑两套测试 + 取未提交改动 → 写 web/status.json
uv run python tools/gen_status.py --skip-web           # 跳过前端那套（要本机 Edge、约 65 秒）
```

启动页（`GET /`，即 `web/index.html`）①状态块里的「状态」按钮读的就是这份 `web/status.json`
（两块：① 测试结论 ② 本次修改）；阅读器页顶栏**没有**这个入口（2026-10-15 起搬到启动页）。
产物**机器生成、不入库**（`.gitignore`），页面读不到时显示「尚未生成」+ 上面的命令；
口径与字段形状见 [web/README.md](web/README.md) 的「状态报告怎么生成」。

脚本对子进程做了两处**本机适配**（不是怪癖，是本机受限环境实测撞出来的，注释里写了原因）：
子进程的临时目录指到项目内的 `.status-tmp/`（跑完即删，已进 `.gitignore`）——
受限环境下 `uv run` 派生的进程写不了系统 TEMP，playwright 的 `mkdtemp` 会 EPERM、前端套件整个报错；
后端那套用 `.venv` 解释器直调 `-m pytest tests -q` 而不在 uv 里再套一层 uv——
嵌套 `uv run` 会撞 uv 缓存目录权限，摘到的「摘要行」会变成 uv 的报错而不是测试结论。

`rewrite` 的选项：`--reader`（读者 id，默认 `local-default`）、`--mode free|strict`
（默认 `free`；`strict` 是**预留插口**，一期不实现，遇到即报错 + 退出码 3、不写任何产物）、
`--max-chars-per-batch`（合并请求的粒度，默认 1100 字符：25 段合并成 4–6 次请求，省 token），
外加第四批的三个**互斥**重跑参数 `--only` / `--retry-failed` / `--from-signals`（见上表）。
`--per-paragraph` 切到**逐段循环**（一段一请求；`--concurrency` 默认 4、`1` = 串行；
`--validation-retries` 默认 2：校验不过就把差异回灌到 user 末尾重试，次数用尽仍不过则该段
不写模型稿、按保持原文处理并进 `unplanned`）。这些参数只在 `--per-paragraph` 下生效。
`--conversation`（必须与 `--per-paragraph` 同用、强制串行）把「独立请求」换成**一条累积的对话**：
第一轮仍是 v7 的全文 / 规则，之后每轮把上一轮的 assistant 决策与当前段指令追加进 `messages`，
校验反馈也追加成新的 user 消息——模型能看到前面几段已经怎么改。
`--help` 里的文案与本节一致（`ar feedback --help` 同样写明默认 dry-run、`--apply` 才写盘、
内容逐字相同则不写）。

`check` 的选项（2026-10-14 追加）：`--reader`（读者 id，默认 `local-default`）——
事实锚口径按**这个读者当前**的画像偏好 `pref_fact_anchor` 判（严 / 松两档见 §3）；
不给画像目录的老调用方用产物 `meta.fact_anchor_policy`，产物没记就按严口径（与改动前逐字一致）。
不传也能跑：缺省读者就是 `local-default`，与 `rewrite` 的缺省一致。

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

## 2.1 端到端实跑记录（2026-10-05 首跑 / 2026-10-08 重跑）

五步都在 `adaptive-reader/` 下执行，注意 `AR_API_KEY` 只在运行时注入（见 §5）：

| 步 | 命令 | 关键输出 | 产物 |
|---|---|---|---|
| 1 | `uv run ar ingest "..\学习\_第1章第1节三版对照\00-底本.md" --id capital-v1-ch01` | `25 段（p001 … p025）`，`锚点稳定：与上一次摄取完全一致` | `data/capital-v1-ch01/source.md`、`source.meta.json` |
| 2 | `uv run ar analyze capital-v1-ch01` | `概念 12 条，配图候选 4 条`（**2026-10-05 首跑原样**；重跑后是 10 条 / 4 条，见 §2.1.1） | `data/capital-v1-ch01/concepts.json` |
| 3 | `uv run ar rewrite capital-v1-ch01 --reader local-default` | `25 段（展开 8 / 保持 17）`（**2026-10-05 首跑原样**；盘上现在这份 `rewrite-v3` 产物是展开 10 / 保持 15，见 §2.1.1）；`请求 6 次；tokens：prompt 9885 / completion 25402 / 合计 35287`；`段序：与原文一致` | `data/capital-v1-ch01/decisions.json`、`rewritten.md`、`changes.json` |
| 4 | `uv run ar check capital-v1-ch01` | `逐段比对 25 段——数字 34 项 / 引号引文 11 项 / 书名号《》13 项，全部与原文一致；没有段缺失`，**退出码 0** | 结果写回 `changes.json` 的 `fidelity` |
| 5 | `uv run ar serve --port 8765` | 页面 `?source=capital-v1-ch01`：主栏 25 段真实改写稿、抽屉跟随当前段、无 JS 报错、无降级横幅 | 截图 `data/capital-v1-ch01/_verify/*.png` |

**token 结算（首跑）**：`analyze` 1 次请求；`rewrite` 6 次请求（25 段合并、并发发 4–6 批）；
`/api/explain` 真机每次 1 次请求。合计真机调用 **10 次**（analyze 1 + rewrite 6 + explain 3）。

> ⚠ 表里第 3 行那句 `请求 6 次；tokens：…` 是**当时的原样输出**（那时还没有重试，批数就是请求数）。
> 2026-10-13 起同一行改成 `请求 N 次（M 批；其中重试 K 次）；tokens：…`：`N` 是**真实 HTTP 次数**
> （含每次重试），`M` 是**批数**（= `meta.usage.requests`，语义未变）。两个数最多差 6 倍，
> 只报批数会把真实请求数严重低估。见 §3 的「模型调用的尝试记账」。

`_verify/serve.log` 是那一次真服务运行的 uvicorn 日志（HTTP 请求行，不含密钥与正文）。

### 2.1.1 2026-10-08 重跑：提示词重排 + 首发预热 + 缓存用量入产物

改了三处（`rewrite-v2` / `concepts-v2`，见 §3 与 §8 第 14 条）：把**会变的内容**（本批段号 + 正文）
挪到 user 消息末尾（批间共享前缀 972 → **2007 字符**，同一份材料同一份概念清单下实测；概念清单换一版
绝对值会跟着变——重跑后的 10 概念清单下是 874 → 1909）、`rewrite` **先串行跑第 1 批预热再并发其余批次**、
产物里记 `meta.usage.cached_tokens` 与逐批 `meta.batch_usage[]`。授权重跑，实测：

| 轮次（`ar rewrite capital-v1-ch01 --reader local-default`） | prompt | completion | total | **cached_tokens** | 墙钟 |
|---|---|---|---|---|---|
| 备份产物（2026-10-05，`rewrite-v1`；字段当时被汇总逻辑丢掉） | 9885 | 25402 | 35287 | 未记录 | 未记录 |
| 重跑 A（`rewrite-v2`，**冷**：这些提示词从没发过） | 9489 | 40563 | 50052 | **0** | 151.6 s |
| 重跑 C（`rewrite-v2`，**热**：与重跑 A 逐字相同） | 9489 | 19133 | 28622 | **9088（96%）** | 63.2 s |

- 重跑 C 的 `data/capital-v1-ch01/decisions.json` / `changes.json` 里直接看得到
  `meta.usage.cached_tokens: 9088`，逐批明细 `meta.batch_usage[]` 里 6 批各命中 1408–1664；
  `uv run ar check capital-v1-ch01` **退出码 0**（25 段，数字 34 / 引文 11 / 书名号 13 全部一致）。
- **「重排 + 预热」本身在冷跑上没有省到钱**：网关只在「逐字相同的前缀 ≥ 约 2048 token（或差异落在
  最后一个块内）」时才算命中，而这批提示词的固定前缀约 1040 token（1909 字符），批间差异约 500 token
  ⇒ 6 批全部未命中。省下来的是**重跑**：同一提示词再发一次，prompt 侧命中 96%（≈9000 token），
  墙钟 151.6 → 63.2 s。门槛的实测依据见 §8 第 14 条。
- `analyze`（`concepts-v2`，材料正文挪到末尾）：`prompt 3510 / completion 3975 / 合计 7485`，
  `cached 0`，28.2 s，概念 10 条 / 配图候选 4 条。
- 中间还有一次重跑 B（同 A，热）：cached 9088、completion 22892、87 s；但它有 2 段把 `[p024] ` 这类
  **段 id 标记写进了正文**，被事实锚判为「多出数字 024/025」⇒ `ar check` 退出码 4。
  **没有放宽校验**：丢掉这次产物、重跑 C 得到干净输出（那一轮结束时产物目录放的是重跑 C；
  2026-10-13 重跑后盘上放的是 `rewrite-v3` 的产物，见下）。

**2026-10-13：读前背景问卷（`rewrite-v3` + 读者素描）真机验收。** 画像里那份背景是 2026-10-09 填的
（本科 / 高中物化地 / 大学艺术学门类·产品设计 / 自报熟悉 编程与计算机·自然科学·哲学与思想 /
常读 通俗读物与新闻评论 / 偏好补前置概念），渲染出的**素描 96 字符**，未触发 §8 第 15 条的
400 字符上限丢弃。同一份提示词整篇跑了两遍：

| 轮次（`ar rewrite capital-v1-ch01 --reader local-default`） | prompt | completion | total | **cached_tokens** | 墙钟 |
|---|---|---|---|---|---|
| 第 1 遍（2026-10-13T12:24:08，`rewrite-v3`；**冷**：这版提示词从没发过）——坏产物，已丢弃 | 10761 | 33140 | 43901 | **0** | 101.83 s |
| 第 2 遍（2026-10-13T12:27:42，`rewrite-v3`；**热**：与第 1 遍逐字相同）——**盘上现在这份** | 10761 | 27034 | 37795 | **10368（96%）** | 80.84 s |

- **第 1 遍坏在哪**：记账字段全对（`prompt_version: rewrite-v3`、`background_used: true`、
  `background_fingerprint: c9037bb4`、`profile_used: true`、`profile_note` 属「仅背景，无概念自评」档、
  `action_counts {expand: 9, keep: 16}`），但最后一批（第 24–25 段）模型把 `[p024] ` 段号前缀写进正文
  并**重复了一段** ⇒ `rewritten.md` **27 段** ≠ 原文 25 段、p024/p025 数字丢失、`ar check` **退出码 4**、
  `fidelity.checked: false`。按上面重跑 B 的先例，**丢掉坏产物、整篇重跑，没有放宽校验**。
- **为什么整篇重跑而不是局部重跑**：那次（2026-10-13 上午）局部重跑**尚未实现**，
  `ar rewrite` 只有整篇这一个入口（**第四批已补上** `--only` / `--retry-failed` / `--from-signals`，
  见 §2 / §3；但那一批新能力只有假模型覆盖）。代价是第 1 遍那份 completion（33140）白花。
- **第 2 遍验收全绿**：`uv run ar check capital-v1-ch01` **退出码 0**；`fidelity.checked: true`、
  `mismatches: []`、`counts {paragraphs: 25, compared: 25, numbers: 34, quotes: 11, books: 13}`；
  `rewritten.md` 按空行切分 **25 段**、全文无 `[p0NN]` 前缀；`action_counts {expand: 10, keep: 15}`；
  `degraded` 只有「背景模式（没有降级）」1 条、`model_errors: []`、`rewrite_incomplete: false`、
  `deferred_count: 0`；6 批逐批命中 1664 / 1792 / 1792 / 1664 / 1664 / 1792（求和 = 10368）。
- **这次重跑命中前缀缓存 96%**（10368 / prompt 10761），与 §8 第 14 条「同一提示词再发一次、
  prompt 侧命中 96%」的既有结论一致——素描在固定块里、两遍提示词逐字相同，加进背景没破坏这条规律。
- 素描注入之后的**模型遵守度**问题（`why.note` 自称「画像为空」+ 25 段 `why.profile` 空数组）
  单列在 §8 第 19 条：**2026-10-14 已定位并修**（根因是提示词里那段「空自评」文案，提示词升
  `rewrite-v6` / `compress-v3`），它不是记账 bug，也不影响上面这套 token 账与事实锚结论。


### 2.1.2 五版对照：分批 / 逐段串行 / 逐段并发 / 逐段对话 / 一次性基线（2026-10-18，`rewrite-v7`）

同一份材料（`capital-v1-ch01`）、同一读者（`local-default`）、同一模型（`deepseek/deepseek-v4.1-flash`），
五版各跑一次；产物写 `data/_eval/<版>/capital-v1-ch01/`，**不覆盖**主产物。**不是严格 A/B**：
后跑的版本会吃到前面版本建好的前缀缓存（逐段两版又是热跑），墙钟只作量级参考；每次只跑一遍，
temperature 0.6 的随机性没有做重复实验摊平。

| 版本 | 调用 | 墙钟 | prompt | completion | 前缀缓存 | 文本改动段 | 动作 | `ar check` |
|---|---|---|---|---|---|---|---|---|
| 分批（默认，6 批） | 6 | 71.7 s | 35,535 | 26,779 | 23,040（65%） | 5 段 | expand 4 / compress 1 / keep 16 / illustrate 4 | 通过，1 条多申报 |
| 逐段串行（`--concurrency 1`） | 25 | 320.7 s | 138,342 | 40,581 | 136,832（99%） | 5 段 | 同上 | 通过 |
| 逐段并发（`--concurrency 4`） | 26（25 + 1 次校验重试） | 93.1 s | 143,935 | 39,071 | 142,208（99%） | 5 段 | 同上 | 通过 |
| 逐段对话（`--conversation`） | 25 | 179.6 s | 214,033 | 10,633 | 205,312（96%） | 1 段 | expand 0 / compress 1 / keep 20 / illustrate 4 | 通过 |
| 一次性基线（`ar baseline`） | 1 | 62.2 s | 3,968 | 10,927 | 0（首次调用） | 1 段 | 无动作（整篇一次出） | 事实锚全过，几乎没改 |

读法：

- **分批仍是默认首选**：墙钟与 token 都最低；事实锚全过。逐段串行的 completion 是它的 1.5 倍、
  prompt 是近 4 倍——25 次调用每次都要重新推理一遍，缓存只能省 prompt 的钱，省不了 completion。
- **逐段并发把墙钟压回同量级**（93 s vs 72 s），代价是 prompt 14.4 万（99% 命中，按缓存价计）与一次
  校验重试；适合「要逐段校验、又等不起串行」的场合。
- **逐段串行的价值在失败粒度**：每段独立校验，一次失败只废一段；这次实测里并发版 p021 多出一句引文，
  被校验拦下并回灌重试后通过（26 次调用 = 25 段 + 1 次重试），正文没有把多出的引文写进产物。
- **三版的决策并不相同**：动作计数一样（4 / 1 / 16 / 4），但展开的段不同（分批 p008、p012；串行 p020、p021；
  并发 p008、p020），25 段里 18–19 段的三版正文逐字相同——**逐段并没有明显改变质量，只是换了花的钱**。
- **逐段对话反而更保守，不是更一致**：同一份 `messages` 里累积了前 24 轮的 `keep` 决策后，模型明显被锚定——
  第一次跑（未加反锚定提示）是 0 展开 / 0 压缩 / 21 保持 / 4 配图；加上「每段独立判断、不要跟着保持」后
  也只到 0 展开 / 1 压缩。completion 只有 1.1 万（独立串行 4.1 万），但**几乎没改写**；prompt 21.4 万（96% 命中）。
  结论：**上下文累积会改变决策分布，不只是换成本**；要「逐段 + 高质量」目前仍用独立请求，
  对话模式适合「跨段一致性优先、且能接受模型惜字」的场合。
- **一次性基线很便宜但几乎没改**：25 段里只改了 1 段（其余逐字照抄），completion 1.1 万（其中 7,740 是
  reasoning）；它把「不需要改就照抄」执行得最保守，作为「便宜但没做多少事」的下限对照。

## 3. 产物与数据模型

落盘位置（字段名与 [DESIGN.md](DESIGN.md) §4 一致，逐字不改）：

| 路径 | 内容 |
|---|---|
| `data/<id>/source.md` | 源文本**原样**（按字节复制，只读） |
| `data/<id>/source.meta.json` | `source_id` / `ingest{kind,path,hash,at}` / `paragraphs[{id,index,text,text_hash}]` |
| `data/<id>/concepts.json` | `concepts[{id,name,surface,prereq,difficulty_hint,paragraphs,domain,concept_key}]` + `illustration_candidates[{paragraph,kind,why}]` + `meta`（2026-10-13 起另含 `attempts` / `retry_account`，见下） |
| `data/<id>/concepts.raw.txt` | 只在抽取失败时出现：模型原始输出（排查用） |
| `data/<id>/decisions.json` | `decisions[{paragraph,action,why{profile,note},confidence}]`（**每段一条**，**不带** `figure` / `deferred`）+ `meta{action_counts,deferred_count,profile_used,profile_note,background_used,background_fingerprint,request_count,usage,batch_usage,degraded,attempts,retry_account,rerun?,rerun_history?,…}`（`batch_usage[]` 每条另含 `attempts`；`rerun` / `rerun_history` 见下） |
| `data/<id>/rewritten.md` | 改写稿（段序与源一致、段间空行分隔、段首不写 id）；`illustrate` 段的正文与原文**逐字节相同**（**图不进 md**），枚举内未实现的动作按保持处理 |
| `data/<id>/changes.json` | `changes[{paragraph,action,why,confidence,facts{numbers,quotes},fact_declarations?,profile_refs_filled?,figure?,figure_error?,deferred?}]` + `fidelity{checked,mismatches,counts,fact_anchor_policy,waived,over_declared,…}` + `meta{paragraph_count,action_counts,deferred_count,…,fact_anchor_policy,fact_anchor_policy_source,model_errors,unplanned_paragraphs,rewrite_incomplete,attempts,retry_account,rerun?,rerun_history?}`（`figure` **只在配图成功时出现**，失败时该条 `action` 退回 `keep` + `figure_error`；`fact_declarations` **只在模型真的申报了放弃某项事实锚时出现**——整篇改写与**在线压缩**两条路径都会写它；`profile_refs_filled`（布尔 `true`）**只在骨架照读者自评兜底补齐了这一段 `why.profile` 时出现**，是页面那枚徽标的唯一判据，同样不进 `decisions.json`；`waived` / `over_declared` **只由 `ar check` 填**，改写与压缩阶段一律留空数组；`paragraph_count` 与 `decisions.json` 的 `meta` 同口径，2026-10-13 复核修复 F5 补上） |
| `data/<id>/_verify/` | 真服务联调的截图与 uvicorn 日志（人工验收材料，不是程序产物） |
| `data/<id>/_eval/` | **一次性基线**（`ar baseline`）的证据：`oneshot.json` + `oneshot.md`（读不通时另有 `oneshot.raw.txt`）。**不碰**三份主产物；`data/` 已进 `.gitignore`，留作人工对照 |
| `profiles/<reader>/profile.yaml` | 读者画像（顶层键 `reader` / `background` / `concepts` / `preferences` / `overrides`；行为演化的计数器放在 `concepts[cid]` **内层**，不动顶层白名单；`preferences[]` 的条目里**可以带取值** `value`——目前只有事实锚口径 `pref_fact_anchor` 用它，`value ∈ keep_all / allow_declared_drops`，写入走 `set_preference` / `POST /api/profile/preference`） |
| `profiles/<reader>/events.jsonl` | **行为事件流**（第四批新增）：一行一条 JSON、`ensure_ascii=False`、追加写、结尾换行；由 `POST /api/events` 与在线压缩各写各的，**只 append、绝不写 `profile.yaml`**（写一次画像就换指纹 ⇒ 讲解缓存全失效）。`ar feedback` 读它折算画像变更 |
| `profiles/<reader>/feedback.json` | **回流水位线**（2026-10-13 复核修复 F2，读者级记录文件，照 `survey.json` 的先例，**不进版本库**）：`{reader, updated_at, last_at, processed[]}` —— `processed` 是 `ar feedback --apply` 已经折算过的**事件 id**（保留最近 500 条），`last_at` 是水位线留痕。它**不写 `profile.yaml` 的顶层新键**、**不参与画像指纹**（指纹只看 `profile.yaml`），判「已处理」只看 id 集合、不按时间跳过（多材料交错 + 客户端时钟偏移下按时间跳过会丢事件） |
| `profiles/<reader>/survey.json` | 读前背景问卷的记录文件（**不进版本库**）：问卷版本、每题**原始**作答（截断前的自由文本也在）、落画像后的 `background`、追问的原始问答、追问调用的元数据（`model` / `prompt_version` / `usage` / `followup_history[]` / 追问那条 `call` 事件的 `attempts`） |

**提示词版本**：`concepts-v2` / **`rewrite-v7`** / `explain-v2` / `survey-v1`，加在线压缩的
**`compress-v3`**、一次性基线的 **`oneshot-v1`**（`src/ar/prompts.py` 里的六个常量）。
最近的变化：
- `rewrite-v6 → rewrite-v7`（2026-10-18）：材料行 / 概念清单 / 配图候选从固定块中间挪到**规则之后**，
  并在同一条「按材料变」的段里新增**全文**（`{full_text_block}`）与**校验反馈槽**（`{feedback_block}`，
  空串零字符插入）；规则 1 / 2 / 5 的方位词改「下面」，规则 11 去掉写在正文里的版本号。
  同一读者换材料时可共用的通用前缀从 513 字符涨到 3516 字符；同一材料内批间 / 段间前缀
  含全文（capital-v1-ch01 实测 9889 字符 / 5364 token）。旧语义变化（都是 2026-10-14）：

- `rewrite-v4 → rewrite-v5`：读者背景之后多了一段**逐字稳定**的「事实锚口径」固定块
  （`{fact_anchor_block}`，两档取值各一段固定文案）并新增规则 10——每段都要在
  `fact_declarations` 里逐条申报「本段没有原样保留哪几项事实锚」；
- `rewrite-v5 → rewrite-v6`（同日复核修复）：新增规则 11——`why.profile` 里**有自评就必须逐条保留
  模型给的引用**（形如 `c03:unknown`），没有自评可引用时才写空数组 `[]`；同时
  `_format_profile_block` 的「空自评」分支措辞改精确（**不再说「画像为空」**，见 §8 第 19 条）；
- `compress-v1 → compress-v2`（同日收尾）是**在线压缩补上申报通道**：同一段
  `{fact_anchor_block}` 口径固定块（**共用同一份文案**，只是规则序号换成 6——两处都在
  `FACT_ANCHOR_BLOCKS` 里，`{rule}` 是序号占位符）+ 一条申报规则 + JSON 示例里那一格；
  `compress-v2 → compress-v3`（同日复核修复）是**那份提示词与 `rewrite` 共用同一个「读者画像」块**，
  块的措辞一改它的内容也就变了，按同一条纪律升版本号（不升就是「提示词变了、版本号还说没变」）；
- 「要压的那段原文在末尾」这条前缀缓存纪律不变。
- `rewrite-v3 → rewrite-v4` 的变化是规则 2 从「只实现展开 / 保持，compress / illustrate 本期不实现」
  改成四动作判据，并在固定块里加一段**逐字稳定**的 `illustration_block`（`concepts.json` 的
  `illustration_candidates`，概念清单之后、材料正文之前；有块与无块的差异只在那一格）；
  `explain-v2` / `survey-v1` / `concepts-v2` 不动。
- `compress_paragraph` 工具走 `compress-v3`，它的提示词与 `rewrite-v7` 是两份东西
  （**共用「事实锚口径」那一格的口径文案**，其余规则各写各的；两条路径的**校验口径**因此一致，
  见下一节）。
- （沿革：`rewrite-v3` 相对 `rewrite-v2` 的唯一语义变化是固定块里多了一行「读者背景：{读者素描}」。）

`concepts.json` / `decisions.json` / `changes.json` 的 `meta` 是回归基线（DESIGN §7）：
`model`、`prompt_version`、`generated_at`，另有 `usage`（token）与 `degraded`（本次所有可见降级）。
**任何元数据里都不含密钥值。**

`rewrite` 的 `meta.usage` 里，`requests` / `prompt_tokens` / `completion_tokens` / `total_tokens`
四个字段口径不变，**追加** `cached_tokens`：模型侧上报的**前缀缓存命中** prompt token 合计，
取 `prompt_tokens_details.cached_tokens` 或扁平字段 `prompt_cache_hit_tokens`（取到哪个算哪个，
见 `src/ar/model.py::cached_tokens_of`）；网关一个都没上报时写 `null`——**`null` 与 `0` 分得开**
（`null` = 没上报，不许编造成 0）。另有 `meta.batch_usage[]`（每批一条：`batch` / `paragraph_count` /
`prompt_tokens` / `completion_tokens` / `total_tokens` / `cached_tokens?` / `failed?`），
用来判断「预热到底有没有生效」：第 1 批通常是未命中，后面几批才有命中。

`rewrite` 的 `meta` 里另有两个由读前背景问卷决定的**追加**字段（旧键一个不改）：

| 字段 | 含义 |
|---|---|
| `background_used` | `true` / `false`：这次改写是否拿到了非空素描。判据是「固定题答过**或**有非空追问摘要」（`ar.survey.has_background`），与 `profile_used` 一起决定每段 `why.profile` 留不留「（无画像）」标记 |
| `background_fingerprint` | 素描文本的 8 位 sha256 哈希（`ar.survey.sketch_fingerprint`）。**背景一变它就变**，是「这次用的是哪版背景」唯一的对照依据——旧产物与新产物的改写决策不可直接比（`PLAN-background-survey.md` §14）。没有背景时素描是固定文案 `未提供`，哈希就是这段文案的哈希 |

**⚠ 盘上的样例产物比文档旧（「键不存在」≠「值不对」）**：`data/capital-v1-ch01/` 现在躺着的那三份产物
是 **2026-10-13 12:27** 那次 `ar rewrite` 留下的（`concepts.json` 更早，2026-10-08），早于本批复核修复 F5
给 `changes.json` 的 `meta` 追加 `paragraph_count`，更早于 `attempts` / `retry_account` 那次改造。所以：
`changes.json` 的 `meta` 里**还没有** `paragraph_count`（`decisions.json` / `concepts.json` 本来就有，都是 25），
三份的 `meta` 里**一个都没有** `attempts` / `retry_account`——代码在整篇与局部重跑两条路径上都写
（见下面 F5 那一行与「模型调用的尝试记账」小节），只是盘上这份产物**没重跑**。要让它出现得重跑 `ar rewrite`
（打真网关），本批硬约束禁止真实模型调用，**因此没有重生成**。另：`data/` 被 `.gitignore` 忽略、它是**运行产物**
不是夹具；前端夹具只有 `samples/capital-v1-ch01/doc.json`（＝ `GET /api/doc` 的响应体，与 `/api/doc` 同形，
键集由 `tests/web/test_fixture_schema.py` 守门）。

### 第四批的四组追加字段（全是追加，旧字段一个没改）

**① 配图的 `figure` / `figure_error`**（`PLAN-actions-feedback.md` §4.1）：

| 字段 | 位置 | 形态与口径 |
|---|---|---|
| `figure` | `changes[i]` | `{kind, source, caption?}`，`kind ∈ mermaid / svg`；**只在配图成功时出现**（照 `deferred` 的「为真才出现」先例）。mermaid 的 `source` 是**源码**（首词就是图类型关键字，行数 ≤ 40、字符数 ≤ 2000）；`svg` 是自包含 SVG 源码 |
| `figure_error` | `changes[i]` | 一句话失败原因；出现时该条 `action` **退回 `keep`**（正文照旧），降级同时进 `meta.degraded` |
| — | `decisions.json` | **不带** `figure`（与**不带** `deferred` 同口径）：决策记录只留动作与依据 |
| — | `rewritten.md` | illustrate 段的正文与原文**逐字节相同**、**图不进 md**：保住「按空行切段 + 逐段对位」与 `text_hash` 校验，图由前端在该段容器内就地插入；`fidelity.facts_of` 因此不会把 SVG 坐标数字当成事实锚 |

**② 局部重跑的 `meta.rerun` / `meta.rerun_history[]`**（§6.2；`decisions.json` 与 `changes.json` 的 `meta` 都有）：

| 字段 | 含义 |
|---|---|
| `meta.rerun` | 本次重跑 `{at, reason, requested, paragraphs, batches, previous_generated_at}`：`reason ∈ only / retry-failed / from-signals`，`requested` 原样记 `--only` 的段列表或标志位 |
| `meta.rerun_history[]` | 每次重跑一条（**追加、不覆盖**），含该次的 `usage` / `retry_account`——本次全篇的 `usage` / `batch_usage` / `attempts` / `retry_account` 记的都是**本次重跑**的账 |
| `fidelity` | 局部重跑后**重置为未校验**（`checked: false`、`counts: null`）：绝不许把上次的 `checked: true` 带过来，要复核就重跑 `ar check` |
| 退出码 6 | 按**合并后**的真实状态重算：目标段的旧失败条目先清、按本次结果写回；非目标段的历史条目保留；补齐后回到 0 |

**③ 行为事件流 `profiles/<reader>/events.jsonl`**（§7.1）：一行一条，`kind` 白名单
`dwell` / `read_complete` / `read_abandon` / `explain` / `drawer_open` / `selection` / `compress` / `skip`
（**没有「卡片开关」**：讲解卡片的开合只落前端 `__arDebug`，不进事件流、也不成为画像信号）。
`reader` 由服务端盖章；`id` 用于幂等去重（缺 `id` 时用 `(kind, source_id, paragraph, at)` 复合键兜底）。
`ar feedback` 读它 → 折算信号 → `applySignals` 演化 `concepts[].state` / `confidence` / `evidence`
（阈值 `|score| ≥ 1.2` 且同向证据 ≥ 2 条才改 `state`，一次只升降一档；未达阈值只累加，不动 `state` / `confidence`；
`overrides` 保护、短停留不当负信号）。**计数器放在 `concepts[cid]` 内层**，顶层白名单不动。

**④ 在线压缩的缓存与响应**（§5.2）：缓存键 = `(source_id, paragraph, "compress", "", 画像指纹)`，
与讲解共用同一份 LRU（`ar.cache` 的 `COMPRESS_KIND` / `compress_cache_key`）。

### 2026-10-13 只读复核后的三处修复（都是追加 / 更正好口径，旧字段一个没改）

| # | 位置 | 改法 |
|---|---|---|
| F5 | `changes.json` 的 `meta` | **追加** `paragraph_count`（= 全篇段数），与 `decisions.json` 的 `meta` 同口径；整篇 `ar rewrite` 与局部重跑（`--only` / `--retry-failed` / `--from-signals`）两条路径都写 |
| F2 | `profiles/<reader>/feedback.json`（新） | `ar feedback --apply` 的**幂等水位线**：只折算没记过的事件（按 id、不按时间），成功后写回已处理 id（最近 500 条）+ `last_at`；dry-run 不写；文件缺失 / 损坏按「全部未处理」办并给可见降级（见 §2 / §3 的表） |
| F1 | `POST /api/compress` 的成功响应 `degraded` | 这一段三类事实锚合计为 0 时**不拒绝写入**（没有事实可毁的压缩无害），但成功响应的 `degraded` 里加一条可读说明：**本次压缩未经事实锚校验**（0 比对项 = 检查器空跑）；缓存命中时这条说明跟着一起回（见 §4） |

### 读前背景问卷的 `background` 块（画像的第二个写入来源）

`profiles/<reader>/profile.yaml` 的顶层键 `background`（`ar.survey.normalize_background` 定键顺序）：

```yaml
reader: local-default
background:
  version: 1                     # 问卷版本；题目或选项一变就升版
  answered_at: "2026-10-08T16:40:00+08:00"
  skipped: false                 # 整场跳过时 true，其余字段可缺
  education: undergraduate                         # Q1 受教育程度
  high_school_electives: [physics, chemistry, biology]   # Q2 六选三（正好 3 项）
  university:                    # Q3a + Q3b **嵌套**（不是扁平的 discipline / major）
    discipline: economics
    major: "国际经济与贸易"
  strengths: [economics_finance, programming_computer]   # Q4 自报熟悉领域
  strengths_note: "读过《资本论》第一卷前两章"            # Q4b 补充说明
  reading_materials: [textbook, papers]                 # Q5 常读材料类型
  preference: prerequisites                             # Q6 遇到看不懂时希望怎么讲
  followups:                     # 模型追问的问答（可缺）
    - { q: "……", a: "……", at: "2026-10-08T16:45:00+08:00" }
concepts: {}
preferences: []
overrides: []
```

- **两种形态只差 `university` 这一层**：请求体与 `GET /api/survey` 的 `answers` 里是**扁平**的
  `discipline` / `major`（题号槽位视角），落画像后收进 `university`；读的时候两种都认，
  **写盘一律收进 `university`**（`ar.survey.field_value()` 是唯一的取值口径）。
- **条件不满足的字段不写进画像**（不是写 null）：Q1 选「初中及以下」时 Q2 / Q3a / Q3b 都不出现，
  这样「没问过」与「问了没答」不会混在一起。
- 枚举值一律是**英文稳定串**（`undergraduate` / `economics` / `preferences`…），中文只在展示层出现；
  唯一来源是 `src/ar/survey.py` 的题目定义（`GET /api/survey` 原样返回，前端不自建第二份题表）。
- 老画像没有 `background` 键 → 视为「未提供」，不报错、不写盘。
- `profiles/<reader>/survey.json` 记**原始**作答与追问调用的账，但**不参与缓存指纹**：
  两处缓存失效信号都只看 `profile.yaml`（后端 `src/ar/cache.py` 的 `(路径, 字节数, mtime_ns)`；
  前端 `web/js/api.js` 的响应摘要）。所以答案**必须**同时写进 `profile.yaml`。

### 模型调用的尝试记账：`meta.attempts` / `meta.retry_account`（2026-10-13 新增）

逐次尝试的账从本轮起落进产物。**全是追加字段，旧字段名与语义一个没改**（PLAN-llm-tools §8.3）。

| 字段 | 形态 | 含义 |
|---|---|---|
| `meta.attempts` | `[{tool,try,elapsed_ms,code,delay_ms,retry_after_used}, …]` | 逐次：哪个工具、第几次尝试（1 起）、这次耗时、失败的归一错误码（成功为 `null`）、失败后等了多久（不重试为 `null`）、这个等待是否来自上游 `Retry-After` |
| `meta.retry_account` | `{tool,requests,retries,codes}` | 汇总：请求数（= 尝试条数，**这是真实 HTTP 次数**）、重试数、失败过的错误码（去重保序） |

落盘位置：

- `concepts.json` / `decisions.json` / `changes.json` 的 `meta`：追加 `attempts` + `retry_account`；
- `decisions.json` 的 `meta.batch_usage[]` **每条**追加该批的 `attempts`（看得出是哪一批在重试）；
- `profiles/<reader>/survey.json` 的追问 `call` 事件追加 `attempts`（那一层没有模型调用，不加 `retry_account`）；
- `POST /api/explain` 的响应追加 `attempts`（2026-10-13 复核修复）：**成功与降级都给**，
  结构同 `meta.attempts`；没发请求时是空数组 `[]`（未配置 / 缓存命中 / 材料或段落不存在）。
  讲解不落盘，这是它的重试账唯一的出口；
- `rewrite` 的 `meta.retry_account` 是**多批汇总**：`requests` = 全部批次的总尝试数，每批首发不计重试；
  单批时与 `llm.retry_account` 逐字一致。

**两个「请求数」不是一回事**（`ar rewrite` 的产物里两个都有，容易读错）：

| 字段 | 语义 | 举个数 |
|---|---|---|
| `meta.usage.requests` | **批数**（既有字段语义，未改）：一批段落合并成一次请求的「批」有几批 | 3 |
| `meta.retry_account.requests` | **真实 HTTP 次数**：含每次重试，最多是批数的 6 倍（默认重试 5 次） | 6 |

所以 CLI 那行账**两个都打**：`请求 6 次（3 批；其中重试 3 次）；tokens：…`
（2026-10-13 复核修复：迁移前只打批数，把「真实请求数」严重低估了）。

失败文案也只有一处定义：`ar.llm.failure_message(result)` 产出
`模型调用失败（已试 N 次，末次 <CODE>）：<脱敏消息>`（`N` = 真实请求数、`<CODE>` = 末次失败的错误码，
照旧过 `redact()`）。四个调用点在失败分支都用它——`/api/explain` 的降级 `text`、
`ar analyze` 的报错、`rewrite` 的 `meta.model_errors[].error`、问卷追问的降级说明，
重试花掉的钱在用户看得见的地方留痕。

记账纪律是**先记账再等待**：`attempts` 里那条记录在 sleep 之前就生成，所以进程即使在等待中被掐断，
产物里也留得住「第几次失败、等了多久」。

工具层的入参校验（`ar.tools`）失败也**不抛异常**：返回同一形状的失败信封、错误码 `INVALID_REQUEST`、
`degraded` 点名是哪个参数不合法，且一个请求都不发。抛 `InputError` 会被 HTTP 层报成 400
（把「调用方的 bug」伪装成客户端输入错误）、其它异常报成 500——四个调用点构造的入参本来就合规，
正常路径不受影响。

模块分工（`src/ar/`）：

| 模块 | 职责 |
|---|---|
| `ingest.py` | 摄取、**切分规则**、锚点哈希、重摄取锚点对照（M1） |
| `anchors.py` | `verify_source()`：元数据与源文重新对齐（`ar check` 的锚点部分） |
| `config.py` | 配置加载、密钥解析（`AR_API_KEY` > 文件）、`retry` 段解析（含废弃的顶层 `max_retries` 映射）、`redact()` 脱敏 |
| `model.py` | OpenAI 兼容流式客户端：**只发一次请求并把它看懂**——拼 URL 与请求体、发一次、解析 SSE、`stream_options` 被拒自动降级、`redact()` 脱敏、**错误码归一**；`arequest_once` 是「只发一次、返回 `LlmResult`、不抛」的入口，**自己不再重试**（重试交给 `llm.py`）；`chat` / `achat` 的外观保留（内部经 `llm.acall`，失败抛 `ModelCallFailed`） |
| `llm.py` | 统一调用层（2026-10-13 新增）：重试策略（错误码白名单 + 指数退避 + 上游 `Retry-After`）、逐次尝试记账、**失败不抛**；对外 `call`（同步）/ `acall`（异步） |
| `tools.py` | 工具注册表与**五个**具名工具（2026-10-13 新增，第四批加第五个）：`analyze_concepts` / `rewrite_batch` / `explain_paragraph` / `survey_followups` / `compress_paragraph`；每个工具把「拼 messages → 经 `llm.py` 调模型 → 解析成结构」收成一步，入参 / 出参是 JSON Schema，`ar tools` 直接列举 |
| `prompts.py` | 提示词与版本号（回归基线的一部分） |
| `analyze.py` | 概念清单抽取、整理（repairs）与契约校验（M2） |
| `rewrite.py` | 逐段决策（`planDecisions`）、改写（`rewrite`）、事实锚复核（`check_fidelity`）（M3） |
| `fidelity.py` | 事实锚抽取与逐段比对（数字 / 引号引文 / 书名号《》+ 段缺失）（M3） |
| `profile.py` | 画像 YAML 读写、自评写入（`overrides` 追记） |
| `survey.py` | 读前背景问卷：题目定义与校验、条件题规则、读者素描渲染（逐字稳定、≤400 字符）、模型追问、`survey.json` 读写（PLAN-background-survey 的落点） |
| `explain.py` | `POST /api/explain` 的业务实现（整段 / 选中片段两种作用域，降级可见） |
| `cache.py` | **讲解与在线压缩共用**的进程内 LRU（只缓存成功结果、容量 128、按画像指纹失效；`kind` 一格把两类结果分开） |
| `events.py` | **行为事件流**（第四批新增）：`events_path` / `read_events` / `append_events` / `event_id` / `dedup` / `validate_event` / `signals_from_events`；**不调模型、不进 `ar.tools` 注册表** |
| `server.py` | HTTP 层、静态托管（`web/` + `samples/`） |
| `cli.py` | 7 个子命令（原五个 + 只读的 `tools` + `feedback`）、退出码、打印（`ingest` 的切分与校验入口都在这条链上；局部重跑的目标段解析 `resolve_rerun_targets` 在 `rewrite.py`） |


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

- `src/ar/rewrite.py::planDecisions()`：默认把 25 段按 `--max-chars-per-batch`（默认 1100 字符）
  合并成 4–6 批，**先串行跑第 1 批（预热提示词前缀）、再并发跑其余批次（上限 4）**，返回的
  `decisions` 仍然**每段一条**。第 1 批失败按既有降级路径处理（该批退回原文并记
  `meta.model_errors`），不影响其余批次；逐批 token 与缓存命中记在 `meta.batch_usage[]`。
- `--per-paragraph`（`rewrite-v7`）：每段单独一次请求，提示词与分批模式相同（全文在固定上下文里），
  先做硬校验（决策存在 / 动作合法 / 压缩更短、展开更长 / 句末标点 / 事实锚按读者口径比对），
  不过就带具体差异重试；`meta.planning_mode` / `concurrency` / `validation_retries` 记录调度口径，
  `meta.batch_usage[]` 每段一条。次数用尽仍不过的段不写模型稿（保持原文 + `unplanned`，退出码 6）。
- **四个动作全部实现**（第四批起）：`expand`（展开）/ `compress`（压缩）/ `keep`（保持）/
  `illustrate`（配图）。**截断判据是两条反向的**：`expand` 求「比原文**长** + 句末标点收尾」，
  `compress` 求「比原文**短** + 句末标点收尾」；逐段保真的跳过名单 = `TEXT_ACTIONS`
  （`expand` / `compress`——这两类的正文本来就会变，事实锚交给 `ar check` 逐段比）；
  `illustrate` 段正文逐字节等于原文，因此天然过这一关。
  `deferred` 机制**保留**：它表示「枚举内、本期未实现」。本批之后四个动作都实现了，
  所以正常情况下不会再出现 `deferred`；将来真加了枚举内未实现的动作，那条文案一个字不变
  （前端与用例用注入守着这一行）。
  **枚举外**的动作（模型写了别的词、或干脆没给 `action`）按 `keep` 处理，但**不标 `deferred`**
  ——`deferred` 只表示「枚举内的未实现动作」，降级文案与产物里有没有这个键必须一致。
- **配图失败一律可见**：`kind` 不是 `mermaid` / `svg`、mermaid 畸形（首词不是图类型关键字 /
  含 ``` 围栏 / 超行数或字符数上限）、SVG 白名单不过（解析失败 / 根节点不是 `svg` / 含 `<script>` /
  任何 `on*` 属性 / `foreignObject` / 引用外部资源的 `href`）→ 该段 `action` 退回 `keep` +
  `figure_error` 点名违规项 + `meta.degraded`；图源码里出现原文没有的数字 / 引文 / 书名号**不阻断**，
  只记一条降级（图内数字不参与事实锚，§1 第 3 条）。
- **降级全部可见**：缺某段的决策 → 补一条 `keep` + `confidence: 0` + 降级说明；
  模型调用失败 → 整批按 `keep` 并把失败原因写进产物；「展开」却没变长、「压缩」却没变短，
  或没以句末标点收尾（疑似截断）→ 记进 `meta.degraded` 并点名段 id。
- **有段落没被改写 ≠ 成功**（退出码 6）：批次调用失败或模型没给决策时，那些段落退回原文
  ——事实锚只能证明「没改坏」，证明不了「真的改写了」。这类情况 `rewrite` 打印
  `✗ 有段落因模型问题未被改写` 并列出**失败批次与受影响段 id**，以退出码 **6** 结束；
  产物里留机器可读证据：`meta.model_errors[{batch,paragraphs,error}]`、
  `meta.unplanned_paragraphs[]`、`meta.rewrite_incomplete: true`，`meta.degraded` 里也有一条汇总。
  承接这份产物的 `ar check` 以**同一个码 6** 结束（见下）。
  **第四批起不必整篇重跑**：`ar rewrite <id> --retry-failed` 只重跑这些段并与既有产物合并，
  补齐后退出码回到 0（见 §3 的 `meta.rerun`）。
- **「有画像」的口径**：`concepts` / `overrides` 有内容，**或**画像里有非空 `background`
  （固定题答过，或有非空追问摘要）——`decisions.json` / `changes.json` 的 `meta` 里写
  `profile_used` 与 `profile_note`，`profile_note` 分**三种**情况写明依据：
  「只有概念自评」（含手动推翻条数）/「仅背景，无概念自评」/「仅追问摘要（固定问卷也跳过了）」；
  两者都有时在概念那句后面追加「；另有读前背景问卷的读者素描（已注入提示词的「读者背景」块）」。
- **完全空画像时不许静默按另一种逻辑跑**：`profile_used: false` + `meta.degraded` 里一条
  「无画像模式」，每段 `why.profile` 要么写「难度 N（无画像）」，
  要么在 `why.note` 末尾写「（无画像：本段没有可依据的读者信号）」——25 段里 14 段标了难度、
  11 段写了说明，页面的段落悬停提示与卡片都能看到。
  **只有背景**时反过来：`profile_used: true`、`meta.degraded` 里一条「背景模式（没有降级）」，
  `why.profile` 保留模型给的引用（不写「（无画像）」标记），背景依据只写在 `why.note` 里
  （`PLAN-background-survey.md` §2 拍板 3 / §6.4）。
- **`why.profile` 的三条纪律**（2026-10-14 复核修复，`rewrite-v6`；元素形态 `c03:unknown`）：
  ① 读者有自评时**逐条保留模型给的引用**（`ar.tools` 的 `_DECISION` 字段语义与提示词规则 11 同口径，
  产物里原样落盘）；② 模型**漏键 / 给错形状（不是数组 / 有非字符串项）/ 该给引用却给空数组**时
  **绝不静默**：逐段记 `meta.degraded` + 照读者自己的自评**兜底补齐** `why.profile`
  （`why.note` 末尾注明「依据画像由骨架兜底补齐」的出处），并在该段 `changes[i]` 上留一个
  **结构化标记** `profile_refs_filled: true`（**为真才出现**，照 `figure` / `deferred` 的先例；
  整篇改写与局部重跑两条写盘路径都写，**不进** `decisions.json`），CLI 另打一行
  `why.profile：N 段有引用 / M 段空`（含哪几段是补齐的）；③ 读者本来就没有这些概念的自评时
  **不误报、不补齐**——空数组是契约允许的结果（「没有概念自评」不是「画像为空」）。
  页面在空数组时显示「依据画像：无自评引用」（悬停与卡片两处同一句），补齐的段另挂一枚
  「骨架兜底补齐（模型没给引用）」徽标（悬停多一行出处说明）——**判据只认那个结构化字段，
  不许拿正则去猜 `why.note` 的文案**（文案是人读的散文，改一个标点就会失配）。
- **`mode` 预留**：`free`（默认，自由重述 + 事实锚）已实现；`strict`（逐句可回溯规格）
  一期不做，`ar rewrite --mode strict` 直接报错 + 退出码 3，**不写任何产物、不发任何请求**。

### 一次性基线（`ar baseline`，只对照、不进主路径）

- 一次调用：同一份读者画像 / 背景 / 事实锚口径 / 概念清单 / 全文进提示词，模型一次吐整篇
  `{"paragraphs":[{paragraph,text}]}`（`oneshot-v1`）。没有逐段决策、没有 `fact_declarations`
  申报通道、没有校验重试——所以它**更严**（少了锚点没法申报放行），也更便宜（1 次请求）。
- 只写 `data/<id>/_eval/`：`oneshot.json`（usage / 耗时 / 事实锚 counts / mismatches / problems /
  缺段 / finish_reason）+ `oneshot.md`（按段对齐的整篇；缺段用原文补位）；输出读不通时另留
  `oneshot.raw.txt`。**不碰** `decisions.json` / `rewritten.md` / `changes.json`。
- 用途：给「分批 / 逐段」两条主路做成本与保真对照（2026-10-18 的实测对比见 §8 第 14 条）。

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

### 事实锚「可申报 + 可见」（2026-10-14，PLAN-actions-feedback §14.1）

严格比对带来的两类正当需求（「举例数据只为给出定性结论」「引文是噪音、改成想表达的意思」）
从这一版起可以**申报后放行**，但这条网只松「少了什么」，不松事实漂移：

| 环节 | 落点 | 语义 |
|---|---|---|
| 申报 | `changes[i].fact_declarations = {numbers?, quotes?, books?, note?}` | 模型**逐段申报**「本段没有原样保留哪几项事实锚」，每一项逐字来自原文；**只在真的申报了东西时出现**（照 `figure` 的「为真才出现」先例），**不进** `decisions.json` |
| 口径 | 读者画像 `preferences[]` 里 id 为 `pref_fact_anchor` 的那条，取值 `value ∈ keep_all / allow_declared_drops` | `keep_all`（严，默认）= 申报了也不放行；`allow_declared_drops`（松）= **已申报**的缺失放行。**是读者画像里的一条偏好**，不是全局开关、也不是一次性 CLI flag |
| 判据 | `ar.fidelity.compare_blocks_detailed(declarations=…, policy=…)` | 只有「缺失被申报覆盖」这一种情况放行；**多出来的锚点**（换成别的数字 / 编一条引文）、**顺序调换**、**申报与实测对不上**、**申报读不通**——任何口径下都算违规（退出码 4） |
| 产物 | `meta.fact_anchor_policy` / `meta.fact_anchor_policy_source`（两份产物）+ `fidelity.fact_anchor_policy` / `fidelity.waived[]` / `fidelity.over_declared[]` | 口径记「这次凭什么标准」；放行条目逐条留证（段 id / 类别 / 缺了什么 / 申报了什么 / 说明）；**多申报**（锚点一个没少却申报放弃了某项）单列 `over_declared[]`——**它不改退出码与放行结论**（没有缺失可放行），但也不许静默 |
| 命令行 | `ar check` 打 `事实锚口径：…（来源）`、逐条打「已申报并放行」，多申报逐条打 `⚠ 事实锚多申报：…`；`ar rewrite` 打口径与每段申报了什么 | 放行、申报与多申报都在人看得见的地方，不许只在产物里 |
| 页面 | 卡片「本段申报（事实锚）」+ 放行 / 未放行 / **多申报**三种徽标；悬停该段标记也有一行 | 见 [web/README.md](web/README.md) 的「事实锚申报」 |

口径取值、偏好 id 与默认值都只有一处定义（`src/ar/fidelity.py::FACT_ANCHOR_POLICIES` /
`ar.profile.FACT_ANCHOR_PREFERENCE_ID`）；**取值读坏一律退到严口径**并给一条可见降级
（`resolve_fact_anchor_policy` 的 `warning`），绝不静默放宽。

`ar check` 的 `--reader`（追加的可选参数，默认 `local-default`）：口径按**这个读者当前**的画像判；
没给画像目录的老调用方（或单测直调 `check_fidelity`）用产物自己记的口径，产物没记就按严口径
——**与改动前的行为逐字一致**。产物记的口径与读者现在的不一致时，命令行给一条可见说明
（「产物是按 X 改写的，本次按 Y 校验；要一致就重跑 `ar rewrite`」）。

**两条路径同一套口径**（2026-10-14 收尾）：整篇 / 局部重跑改写（`ar rewrite`）与**在线压缩**
（`POST /api/compress`）都走 `ar.fidelity.compare_blocks_detailed`，都按读者画像里那条偏好判。
在线压缩的申报通道由 **`compress-v2`** 提示词提供（模型在 `fact_declarations` 里逐条申报本次压缩
放弃了哪几项），于是**同一段不会再出现「`ar check` 放行、在线压缩被拒」**；底线一个字没松：
**没申报的缺失、多出来的锚点、顺序调换、申报与实测对不上，两条路径都拒写**。

**申报不改变动作与正文**：它只是「说清我动了什么」，模型不能靠申报把 `expand` 变成「可以随便删」。

偏好怎么写入：`ar.profile.set_preference`（纯函数）与 `POST /api/profile/preference`（HTTP），
这是**新偏好的唯一创建路径**；行为信号回流那一侧仍然**只给已有偏好追加 evidence、绝不自动新建偏好**
（`applySignals` 的原话与降级文案一个字没改）。


## 4. HTTP 接口

| 方法 与 路径 | 说明 |
|---|---|
| `GET /` | 托管**启动页** `web/index.html`（状态 / 背景问卷入口 / 材料清单 / 上传摄取四块）；带 `source` 或 `fixture` 任一 query（空值也算）→ **302** 到 `/reader?<原样 query>`（老链接 `/?source=…` 因此不失效，query 不重排、不重编码）；`web/` 不存在时回**写明原因**的 404，不影响 `/api/*` |
| `GET /reader` | 托管**阅读器页** `web/reader.html`（直接访问 `/reader.html` 也由静态托管兜住）；文件不存在 → **带可读原因的 JSON 404**，绝不回落到启动页、也不走静态兜底那句「文件或路由不存在」 |
| `GET /api/doc/<source_id>` | `{ meta, rewritten, changes, concepts, degraded }`；产物缺失的字段是 `null`，原因写在 `degraded` 里（**只摄取、没改写**的材料照此回 `rewritten: null` + 说明，页面显示原文并标「未改写」） |
| `GET /api/sources` | 材料清单（**只读，一个字节都不写盘**）：`{ sources[], degraded[] }`，每条 `{ source_id, paragraphs, ingested_at, mtime, concepts, rewrite, fidelity }`。缺产物 / 坏 JSON / 形状不对 ⇒ **那一块 `null`** + 顶层 `degraded` 一条可读说明（**绝不 500**）；`concepts = { present, count, generated_at }`、`rewrite = { present, generated_at, action_counts, degraded_count, paragraph_count }`、`fidelity = { present, checked, ok, mismatch_count, counts }`（`ok` = `checked` 为真且 `mismatch_count` 为 0 —— 老产物没有 `checked` 时是 `false`，「没校验过」不算通过）；`mtime` 取三份产物存在者中最新，都没有才退回 `source.meta.json`；排序按**响应里暴露的那个秒精度 `mtime`** 倒序、同刻按 `source_id` 升序（保证「按接口 mtime 排」与「接口给的顺序」永远一致）；空 `data/` → 空数组 + `degraded: []`，`data/` 目录**不存在**才记一条说明；`.upload-*` 残留目录会被点名（不静默） |
| `POST /api/ingest` | `{ filename, content, source_id?, overwrite? }` → 200 `{ source_id, paragraphs, ingested_at, anchors_stable, overwritten, degraded[] }`。**只摄取**：不调模型、不跑 `analyze` / `rewrite` / `check`；段锚点由 `ar.ingest` 的**同一条路径**生成（不另写切段逻辑，与 CLI 逐字一致）。入参口径：扩展名白名单 `.md` / `.txt`（大小写不敏感）、`content` 上限 **2 000 000 字符**（超出拒收，不静默截断）、`source_id` 走 `ar.paths.check_source_id` 同一份校验（没给就从文件名 slug 化，纯中文名退化成 `upload-<内容 sha256 前 8 位>`）；重名且没带 `overwrite: true`（**只认 JSON 布尔**，`"true"` 字符串不算）→ **409 且不写盘**；覆盖后锚点变了 → `anchors_stable: false` + 摄取模块那条「材料已变…请重新校准」原样进 `degraded`。暂存写在 `data/.upload-<随机>/<清洗过的原文件名>`（**不是系统临时目录** —— 本机 `%TEMP%` 不可写，放进 `data/` 才有「能摄取就一定能暂存」的保证），成功 / 400 / 409 / 500 四条路都用 `finally` 必删 |
| `GET /api/profile` | `{ profile, degraded }`（默认读者 `local-default`） |
| `POST /api/profile/rating` | `{ concept_id, state, note? }`（`state ∈ known/partial/unknown`）→ 写回 `profile.yaml`，返回 `{ ok, profile }` |
| `POST /api/profile/preference` | `{ id, value, note? }` → 写入一条偏好并写回 `profile.yaml`，返回 `{ ok, profile, degraded[] }`；**可写的偏好只有 `pref_fact_anchor`**（取值 `keep_all` / `allow_declared_drops`），`id` 不可写或取值非法 → 400；同 id 同取值幂等（不追加证据、不写盘） |
| `POST /api/explain` | `{ source_id, paragraph, kind, selection?, selection_from? }`（`kind ∈ explain/simpler/detail`；`selection_from ∈ rewritten/source`，默认 `rewritten`）→ `{ text, degraded, scope, cached?, selection_truncated?, usage?, attempts[], model?, prompt_version? }`（`attempts` 是逐次尝试账，成功与降级都给，没发请求时是 `[]`） |
| `POST /api/events` | `{ events:[…] }`（也容忍单个事件对象）→ `{ ok, written, duplicates, total, degraded[] }`。**只 append** 到 `profiles/<reader>/events.jsonl`，**绝不写 `profile.yaml`**；`reader` 由服务端盖章；段不在 `source.meta.json` 里 → **丢弃 + `degraded`**（不是 400：前端手里可能拿着旧产物）；写盘失败 → `ok:false` + `written:0` + `degraded` 文案，**不许 200 假装成功** |
| `POST /api/compress` | `{ source_id, paragraph, reason? }`（`reason` 默认 `too_verbose`，读者 id 取应用级配置）→ 成功 `{ ok:true, paragraph, action:"compress", text, scope:"paragraph", cached, usage, attempts, model, prompt_version, degraded, artifacts_updated, hint, fact_declarations, waived[], over_declared[], fact_anchor_policy }`（17 键；`degraded` 平时 `[]`，非空只有「本段无事实锚 ⇒ 校验空跑」「申报读不通」「行为事件没记上」三种，都不影响 `ok` 与写盘）；**降级一律 200** + `{ ok:false, reason, text, degraded[] }`（4 键），`reason ∈ model_not_configured / model_error / bad_reply / fact_anchor_mismatch / no_artifacts / paragraph_not_found / compress_not_shorter`。**写盘前做单段事实锚比对**（与 `ar check` 同一套判据、同一档读者口径）：少了且**已申报** ⇒ 松口径放行（申报落 `changes[i].fact_declarations`，放行条目进 `waived`）；**没申报的缺失 / 多出来的锚点 / 顺序调换 / 申报与实测对不上 ⇒ 不写盘、正文一个字节不动、降级文案点名对不上的锚点**；缓存命中 → `cached:true` / `usage:{}` / `artifacts_updated:false`、不发请求，申报与放行条目跟着缓存一起回 |
| `GET /api/survey` | `{ version, answered_version, questions[], answers, answered_at, skipped, degraded[] }`；题目由后端给（前端不硬编码），`answers` 是当前 `background`（没有则 `null`）；`answered_at` 是**追加**字段（2026-10-15）：取画像 `background.answered_at`（问卷答案落画像那一刻的时间），没有就写 `null` —— **不许拿 `survey.json` 的 `updated_at` 冒充**（它每次记录都会被重写，包括「只补追随问」那条路），`background.answered_at` 才有「落定后不动」的纪律 |
| `POST /api/survey` | `{ answers?, skipped?, followups? }` → `{ ok, background, sketch, degraded[] }`（截断时另加 `truncated{}`）；校验失败 400 并点名哪一题 |
| `POST /api/survey/followup` | 空请求体 → `{ questions[{q,hint}], text, degraded, prompt_version, reason?, model?, usage? }`；**只生成**追问问题，不写画像 |
| `GET /api/health` | `{ ok, model_configured, model, api_key_origin, web_static }` |

另外挂了一个**只读**的 `/samples/`（指向项目根的 `samples/`）：契约没要求，
但前端夹具模式（`?fixture=<id>` 读 `samples/<id>/doc.json`）因此也能走本服务。
`web/` 与 `samples/` 目录不存在时都只回写明原因的 404，不崩、不影响 `/api/*`。

约定：

- **降级必须可见**（DESIGN §6）：`/api/explain` 在模型未配置或调用失败时返回 200 +
  `degraded: true` + 一段说明文本（前端直接渲染进卡片），不返回空白、不假装成功。
- **`POST /api/ingest` 的已知瑕疵（上传语义的历史路径）**：`source.meta.json` 的 `ingest.path` 记的是
  那次摄取真正读到的文件——上传走的是 `data/.upload-<随机>/<原名>` 这个**随即被删掉的暂存路径**，
  所以上传来的材料在元数据里会指向一个不存在的路径（`ingest.kind` 同理仍是磁盘文件那档）。
  这是「摄取模块只认磁盘文件路径」的现状，不是写错：要让它干净得给 `ar.ingest` 加一个文本入口。
- **`/api/sources` 与上传摄取的降级口径（2026-10-15 复核修复）**：
  ① 产物 `mtime` 读不出来或**超出本地时间可表示范围**（Windows 上限约公元 3000 年）⇒ 该条
  `mtime: null` + 点名材料与文件的 `degraded`，**一个坏时间戳不许把整份清单打成 500**；
  ② 产物 JSON **坏 / 被别的进程独占（`PermissionError`）/ 权限不足算同一档** ⇒ 那一块 `null` +
  点名文件与原因，仍绝不 500（`ar.jsonio.read_text` 的异常口径一个字没动，是清单这一侧兜住的）；
  ③ `POST /api/ingest` 的 `content` 里有**编不成 UTF-8 的字符**（孤立代理项）⇒ **400** + 可读文案，
  一个字节都不写；
  ④ `data/` 下的**悬空 junction / 符号链接 / `stat` 失败**的条目各记一条点名说明（普通散文件照旧静默跳过，
  那条既有口径没改）；
  ⑤ `jsonio` 的原子替换撞上「**目标文件被别的进程占用 / 只读**」（Windows `WinError 5/32/33`）⇒
  **有界短退避重试**（≤3 次、总等待 ≤200 ms），等完仍失败就原样抛；失败文案点名文件与重试次数，
  并与「目录不可写」分开；**非这一类错误一次都不重试**（POSIX 行为未变）。
- `degraded` 是数组（`/api/doc`、`/api/profile`、`/api/sources`、`/api/ingest`）或布尔（`/api/explain`），是**追加**字段：
  §4 冻结的键一个不少，前端可以只读自己认识的部分。
- **`POST /api/events` 的降级口径**（`PLAN-actions-feedback.md` §7.1/§7.2）：三级处置分得开——
  `source_id` 非法 → **400**；`paragraph` 不在 `source.meta.json` 里 → **丢弃并记 `degraded`**
  （不是 400）；材料未摄取 / 写盘失败 → **200 + `ok:false` + `written:0` + `degraded`**（不假装成功）。
  事件流是**尽力而为的采集**：它坏了不影响阅读，也不需要重试队列。
- **`POST /api/compress` 的降级口径**（§5.2）：**降级一律 200**（照 `/api/explain` 的惯例），
  只有参数本身不合法（缺 `source_id` / `paragraph`、类型不对）才是 400；`hint` 这个键名是**冻结的**
  （前端按 `hint` 实现，缺省回退固定文案「已压缩，请重跑 `ar check`。」）。
  **成功路径上的 `degraded` 平时是 `[]`**，会非空只有三种情况，且**都不影响 `ok: true` 与产物写盘**：
  ① 该段三类事实锚（数字 / 引号引文 / 书名号）**合计为 0** ⇒ 这次压缩**未经事实锚校验**
  （0 比对项 = 检查器空跑；没有事实可毁的段落压缩本来无害，所以**不拒绝写入**，但必须说出来，
  缓存命中时这条说明跟着一起回）；② 申报本身读不通——**整条不是对象**（如模型给了一个字符串）、
  结构不对 / 值不是字符串 / 不认识的键；③ 那条行为事件没记上（采集是尽力而为）。
  三种都在 `degraded` 里留一条可读说明（「整条不是对象」那一档由工具层
  `ar.tools._run_compress_paragraph` 给出，2026-10-14 复核修复：此前它在工具层被静默丢掉，
  业务层只看到「没申报」），前端**在成功路径上也打一条 warn 横幅**（不静默——见 §8 第 21 条）；
  **安全方向不变**：整条读不通 = 没有申报 ⇒ 该拒写的仍然拒写。
  **申报后放行 / 多申报也走 `degraded` + 三个追加键**（§14.1，2026-10-14 收尾）：成功响应追加
  `fact_declarations`（本次申报，无申报时是 `{}`）、`waived[]`（本次按读者口径放行的条目，
  结构同 `fidelity.waived` 的一行）、`over_declared[]`（「锚点一个没少却申报放弃了某项」的条目），
  以及 `fact_anchor_policy`（本次采用的口径）——页面据此当场渲染「本段申报（事实锚）」与徽标，
  不必等重跑 `ar check`（缓存命中时这几个键跟着缓存一起回）。
  **事实锚过不了就一律拒写**（`ok:false` + `fact_anchor_mismatch`）：没申报的缺失、多出来的锚点、
  顺序调换、申报与实测对不上，**任何口径都不放行**。
- **事件流与回流水位线是两个文件**：`POST /api/events` 只 append `events.jsonl`（绝不写 `profile.yaml`）；
  `ar feedback --apply` 才写 `profile.yaml`，并把「已折算过的事件 id」写进 `profiles/<reader>/feedback.json`
  （保最近 500 条）+ `last_at` 水位线 ⇒ 同一份事件重复 `--apply` 不会重复折算（§2 / §3）。
- **选中片段讲解**（`/api/explain` 的追加能力，旧请求体不受影响）：请求体多两个**可选**字段
  `selection`（读者划中的那段文字）与 `selection_from`（`rewritten` 默认 / `source`，只用于在提示词里
  标注片段来自改写稿还是原文；给别的值回 400）。
  - `selection` 缺失或规范化后为空 → **行为与旧版逐字一致**（讲整段，`scope: "paragraph"`）；
  - 规范化口径与锚点一致（去首尾空白 + 合并连续空白 + 全角空格 U+3000 转半角），规范化后上限 **600 字符**：
    超出截断到 600 并在响应里追加 `selection_truncated: true`（**降级必须可见，不许静默截断**）；
  - `selection` 非空 → 讲解对象是**那段片段**，`paragraph` 继续提供上下文（邻段、画像），
    响应里 `scope` 记为 `"selection"`；`selection` **不引入任何新锚点或哈希**。
- `scope` / `cached` / `selection_truncated` / `usage` 是**追加**字段：`scope`（`paragraph` / `selection`）与
  `selection_truncated` 描述**这次请求要什么**，每次响应都给（前端靠 `scope` 确认「确实按片段讲的」）；
  `cached`（缓存命中）、`usage`（这次模型调用的**原始 token 账**，含前缀缓存命中
  `prompt_tokens_details.cached_tokens`；缓存命中或网关没回 usage 时是 `{}`）与 `model` /
  `prompt_version` 描述**这次产出了什么**，只在成功响应（`degraded: false`）里出现。
  降级响应因此是 `{ text, degraded, reason, scope }`——旧键一个不少、`reason` 枚举不变。
  `usage` 里不含密钥、不含正文（网关原样回什么就透传什么，不做改名或裁剪；网络错误日志照旧过 `redact()`）。
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
- **读前背景问卷的三个端点**（`PLAN-background-survey.md` §8 的契约，旧键一个不改）：
  - **提交体不带 `version`**：后端按只读白名单校验作答槽位（八个字段：`education` /
    `high_school_electives` / `discipline` / `major` / `strengths` / `strengths_note` /
    `reading_materials` / `preference`），**多给一个键就是 400**。问卷版本由服务端记进
    `background.version`。提交体两种写法后端都收（前端统一用 `{answers: {...}}`，
    也兼容把作答字段直接铺在顶层），`skipped` / `followups` 不是作答字段，会被排除。
  - **`answered_version` 在顶层**（`GET /api/survey` 给两个值）：`version` 是**当前**问卷版本，
    `answered_version` 是**读者上次作答时的版本**（没答过 / 画像为空 / 整场跳过时为 `null`）。
    页面的「问卷已更新」提示比较这两个值，**不把版本存进浏览器本地**。
  - **追问答案走 `POST /api/survey`**（请求体 `{followups: [{q, a}]}`，`at` 由后端盖章）——
    那属于「写入画像」；`POST /api/survey/followup` **只负责生成问题**（空请求体，输入是画像里
    已存的作答）。追问答案**不许塞进那八个作答槽位**（塞了就是 400）。后端在
    `/api/survey/followup` 上留了「也能并 followups」的兼容分支，前端不用它保存答案。
  - **追问失败仍回 200 + `degraded: true`**（照 `/api/explain` 的惯例；`reason ∈
    model_not_configured / model_error / bad_reply / empty_reply`），**固定题答案不受影响**；
    模型未配置时页面不给追问按钮的入口。
  - `questions[]` 除契约键外还给前端渲染用的**等价别名**（`qid` / `field` / `label` / `when` /
    `alsoWhen` / `exactly` / `exclusive` / `maxLength` / `placeholder` / `hint`）；别名与权威的
    `requires` **同源派生**，服务端自己的判定也求同一套规则（`ar.survey.condition_rules`
    是唯一来源，`not_in` 的 `values` **带全**被排除的值——只报第一个值会让「Q3a 选不确定」时
    Q3b 仍然显示，读者填的专业随后被丢弃）。
  - **空提交不许抹掉已有背景**：`answers` 为空且 `skipped` 不是 true → 400；重新提交固定题
    不会清掉已答的追问；已答过之后再整场跳过只记 `skipped: true`，**已有作答保持不变**。
  - 三个端点排在兜底路由 `/api/{rest:path}` **之前**；读者 id 取自应用级配置，本次不加按请求传读者的入口。
- **偏好写入端点**（`POST /api/profile/preference`，2026-10-14，§14.1）：体是 `{id, value, note?}`，
  成功 `{ ok: true, profile, degraded[] }`（`degraded` 里是读画像时的说明，例如画像文件还不存在）。
  **可写的偏好只有 `pref_fact_anchor`**（`keep_all` / `allow_declared_drops`）——表在
  `ar.profile.PREFERENCE_VALUES` 里；`id` 不在表里或 `value` 不在可选值里 → **400** 并点名合法取值。
  同 id 同取值幂等（不追加证据、不写盘：一次无谓写盘会换画像指纹 ⇒ 讲解缓存全失效）。
  它是偏好的**唯一创建路径**（`ar.profile.set_preference` 是同一个纯函数）；
  行为信号回流（`applySignals`）**仍然只给已有偏好追加 evidence、绝不自动新建偏好**。
  这条端点也排在兜底路由之前；读取侧不加新端点（`GET /api/profile` 直接返回整份画像）。

## 5. 配置与密钥纪律

`config.example.json` 是模板；`config.local.json`（**不进版本库**）覆盖默认值：

```json
{ "base_url": "https://api.commandcode.ai/provider/v1",
  "model": "deepseek/deepseek-v4.1-flash",
  "temperature": 0.2, "timeout_s": 90, "max_tokens": null, "api_key": "",
  "retry": { "mode": "normal", "maxRetries": 5,
             "retryableCodes": ["EMPTY_RESPONSE", "RATE_LIMIT", "SERVER", "TIMEOUT", "TRANSPORT"],
             "backoff": { "initialDelayMs": 500, "maxDelayMs": 10000, "jitterRatio": 0.1 } } }
```

- 默认端点与模型取本机实测可用的组合；参数面照 `test_api/ttft-bench/bench.mjs`：
  `POST {base_url}/chat/completions`、`stream: true`、`stream_options.include_usage`
  （**被网关拒绝时自动关掉该参数重发一次，且不占用重试额度**）、单次超时 + 按 `retry` 段重试（见 §5.1）。
- `retry` 段是 2026-10-13 新增的**可选**段（形状照 DSH 内置重试）：不写整段 = 用 §5.1 那份默认值。
  非法值（负数、`initialDelayMs > maxDelayMs`、`jitterRatio` 越界、未知错误码）**报错退出（码 2）**，
  不静默回落；段内未知键走 `unknown_keys` 告警。
- 顶层 `max_retries` **已废弃**：只在配置文件里**显式写过**时才映射成 `retry.maxRetries` 并给一条废弃告警；
  两处都写时以 `retry` 段为准。所以直接构造 `Config(max_retries=0)` **不等于**「不重试」，
  要显式写 `retry = {"maxRetries": 0}`。
  `config.example.json` 里删掉了这个顶层键，`retry` 段的默认口径用下划线开头的键当注释写在里面
  （读配置时忽略、不进未知字段告警）。
- 本项目自用的 `config.local.json` 目前覆盖四项：`base_url`（`https://api.commandcode.ai/provider/v1`）、
  `model`（`deepseek/deepseek-v4.1-flash`）、`temperature: 0.2`
  （改写要的是照抄事实，温度低更稳；`config.example.json` 里的默认是 0.6）、`api_key`
  （本机密钥，取自 DSH 凭据库 `~\.dsh\.credentials.yaml` 的 `refs.CMD_API_KEY`，
  与 auto 链当前生效首跳 commandcode 同源；**密钥值不写进任何文档、日志与产物**）。
- **`ar serve` 只在进程启动时读一次配置**（已实测）：`cli.py` 的启动路径只调一次
  `load_config(args.config)`，拿到的 `Config` 交给 `create_app()` 后就是冻结值，`server.py` 不重读文件。
  所以改完 `config.local.json`（例如补上 `api_key`）**必须重启服务才生效**——
  否则 `/api/health` 仍报 `model_configured: false`，页面上的「让模型追问」也照旧走
  「模型未配置」的降级分支（2026-10-09 就是这样踩到的）。
- 密钥优先级：环境变量 `AR_API_KEY` > 配置文件 `api_key`；**只在运行时读取**。
  任何文件、日志、报错、样例、产物元数据里都不会出现密钥值——所有对外文本都过
  `src/ar/config.py::redact()`（抹掉密钥值、`Bearer xxx`、`sk-xxx` 与超长令牌串）。
- `api_key` 为 `""`（或未设 `AR_API_KEY`）= 模型未配置：`/api/health` 的 `model_configured` 为 false，
  `ar analyze` 以退出码 5 结束，`/api/explain` 返回可见降级文案。

## 5.1 重试与错误码（2026-10-13 新增）

**结论先说**：模型调用统一经 `src/ar/llm.py`；默认最多重试 **5** 次（即最多 **6** 次尝试），
**只有白名单里的错误码才重试**，退避是指数 + 抖动，**失败不抛**（结果一律是返回值）。

默认策略（照 DSH 内置重试 `@deepseek-ai/dsh-llm-retry` 的口径实现）：

| 项 | 默认值 |
|---|---|
| 最大重试次数 | `5`（即最多 6 次尝试；`retry.maxRetries: 0` = 只试一次） |
| 可重试错误码白名单 | `EMPTY_RESPONSE` / `RATE_LIMIT` / `SERVER` / `TIMEOUT` / `TRANSPORT` |
| 退避 | 指数 × 对称抖动：500 → 1000 → 2000 → 4000 → 8000 → 10000 ms（封顶 10000），每次 ±10% 抖动 |
| 上游 `Retry-After` | 在 `maxDelayMs`（默认 10000 ms）界内**优先采用它**（记 `retry_after_used: true`）；**超界直接放弃重试**，不拿本地退避凑 |

错误码归一（实现在 `src/ar/model.py`，`llm.py` 只按码判该不该重试）：

| 触发 | 码 | 默认重试？ |
|---|---|---|
| HTTP 401 / 403 | `AUTH` | 否 |
| HTTP 400 / 413 / 422 | `INVALID_REQUEST` | 否 |
| HTTP 429 | `RATE_LIMIT` | **是** |
| HTTP 5xx | `SERVER` | **是** |
| 读超时 / 连接超时 | `TIMEOUT` | **是** |
| 连接失败、协议错误、**流提前结束**（既没有 `[DONE]` 也没有 `finish_reason`） | `TRANSPORT` | **是** |
| 流内 `error` 字段 | 有 status 按上表，否则 `SERVER` | 视码 |
| 收完但正文为空 | `EMPTY_RESPONSE` | **是** |
| 网关明确报额度用尽 | `QUOTA` | 否 |
| 未配置密钥 | `NOT_CONFIGURED` | 否（发请求前就返回 `ok=false`） |
| 其它 / 归不到码 | `UNKNOWN` | 否 |

- **默认只有白名单里的那五个码会重试**；`AUTH` / `INVALID_REQUEST` / `QUOTA` / `UNKNOWN` 一次就停
  （确定性错误白花一次调用没有意义），`NOT_CONFIGURED` 连请求都不发。
- `UNKNOWN` 不重试是**刻意**的：拿不到错误码宁可不动。**例外只有一个**：把某个码**显式写进**
  `retryableCodes` 就按写的重试（例如写 `["AUTH"]` 时 401 会重试、429 反而不会——有测试与实测钉住）。
- **流提前结束现在判失败并重试**（`TRANSPORT`），已收到的半截正文一律丢弃。这正是修掉范本自身的一个盲点：
  宿主在「流没有终止事件就被掐断」时抛的 `STREAM_CLOSED` 不在默认可重试集合里（于是不重试）；
  本项目把它归到可重试的 `TRANSPORT`。有 `finish_reason` 但没有 `[DONE]` 仍算成功。
- `retry.mode = "always"`（无上限重试）**不被接受**：写了直接报错、退出码 2，不静默按 `normal` 跑。
- `/api/explain` 的响应追加 `attempts`（2026-10-13 复核修复）：成功与降级都给，结构同产物里的
  `meta.attempts`，没发请求时是 `[]`——讲解不落盘，这是它的重试账唯一的出口；
  面向用户的失败文案（降级 `text`）里也写着「已试 N 次，末次 `<CODE>`」。
  详见 §3 的「模型调用的尝试记账」。

## 6. 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 用法 / 输入错误（文件不存在、source_id 非法、JSON 坏了；`check` 找不到改写稿、**缺 `changes.json`、产物不是对象或 `changes` 为空数组**也是这一类；**输出目录不可写**（`WriteError`，2026-10-14）同属这一档——**三份产物的写盘入口都是这一条口径**：`ingest` 的 `source.md` / `analyze`·`rewrite` 的 JSON 与 `rewritten.md`（`ar.jsonio`）、`profile.yaml`（`ar.profile.save_profile`）、以及 `ar feedback --apply` 的画像写回；`events.jsonl` 那条是 HTTP 侧的「尽力而为」采集，写失败不改退出码，见下） |
| 3 | 功能尚未实现（目前只剩 `rewrite --mode strict`：严格档接口预留） |
| 4 | 校验失败（锚点、概念清单契约、**事实锚**；含**检查器空跑**：0 比对项 / 空文档；逐条列出问题并点名段 id / 概念 id） |
| 5 | 模型未配置或调用失败（整篇都没跑起来） |
| 6 | **模型失败导致有段落未被改写**：批次调用失败或模型没给决策，那些段退回原文。`rewrite` 与 `check` 同码——`check` 的事实锚全对也照样报 6 |

码 6 的单一口径：**「有段落没被改写」以退出码 6 暴露**，`rewrite` 与 `check` 一致。
`rewrite` 打印 `✗ 有段落因模型问题未被改写` + 失败批次 + 受影响段 id；
`check` 打印同样的明细（先做完事实锚比对再报 6）。重跑 `ar rewrite` 补齐后两处都回到 0
（第四批起可以只补失败的那些段：`ar rewrite <id> --retry-failed`，退出码 6 **按合并后的真实状态重算**）。
`degraded` 里的其它降级（空画像、缺概念清单、疑似截断、未知动作、配图失败、压缩降级）**不改变退出码**——
它们不影响「这一段到底改没改」。

**事实锚申报放行不改变退出码**（2026-10-14，§14.1）：已申报且按读者口径放行的缺失算**通过**（退出码 0），
放行条目逐条写进 `fidelity.waived` 并打印；**没申报的缺失仍然是 4**，多出来的锚点与顺序调换
任何口径下也都是 4。也就是说口径只决定「已申报的那部分算不算过」，不改变码的语义。
**多申报（锚点一个没少却申报放弃了某项）同样不改退出码**：命令行逐条打 `⚠ 事实锚多申报：…`、
产物留 `fidelity.over_declared[]`、页面挂「多申报」徽标，但通过就是通过（没有缺失可放行）。

**第四批确认语义未变**，另补两条口径：

- **局部重跑的段集为空 → 退出码 0 且不写盘**：`--only` 解析出的段集为空不会发生（段不存在就报 2），
  但 `--retry-failed` / `--from-signals` 完全可能空——那时只打印「没有需要重跑的段」，
  **不写盘、不调模型**，以 0 结束（不是 2 也不是 6）。
- **局部重跑的三参数互斥**：`--only` / `--retry-failed` / `--from-signals` 两两或全给 → 退出码 2 +
  点名收到了哪几个；`--only` 里的段 id 格式非法或不在材料里 → 也是 2（**早于**模型门禁判定，
  所以「模型没配 + 目标段为空」报的是 0 而不是 5）。
- `ar feedback`：0 成功（含「无变更」）/ 2 用法或输入错误（材料未摄取、`concepts.json` 缺失）。

## 7. 测试

```powershell
uv run pytest tests -q            # 后端用例（688 passed / 0 skipped）：默认不收集 tests/web（前端用例单独跑）
python -m pytest tests/web -q     # 前端无头用例（184 passed + 1 skipped，需要本机 Edge）——**必须用系统 Python**
```

> **跑前端那套之前先把 `TEMP` 指到可写目录**（本机受限环境的实测结论，不是可选项）：
> ```powershell
> New-Item -ItemType Directory -Force -Path .status-tmp | Out-Null
> $env:TEMP = (Resolve-Path .status-tmp).Path; $env:TMP = $env:TEMP
> python -m pytest tests/web -q
> ```
> 在**受限环境**（DSH 沙箱）下不这么做的话，playwright 启动浏览器要在系统 `%TEMP%` 里
> `mkdtemp('playwright-artifacts-')`，那一步 `EPERM` 会让**整个前端套件变成一片 errors**
> ——**那是跑法错误，不是缺陷**（浏览器起不来 ⇒ 每个要用浏览器的用例各报一条 error，
> 条数随用例规模涨：文档里早前记的是 88 条，2026-10-14 这个规模下现场报的是 133 条）。
> 本次收尾跑的时候本会话的文件策略是 `danger-full-access`（系统 `%TEMP%` 可写），
> **重定向与不重定向两种跑法都是 `146 passed + 3 skipped`**——所以这条提醒是
> 「换个受限环境就会踩」，不是「现在一定失败」。
> `tools/gen_status.py` 对子进程做的就是这件事，见 [web/README.md](web/README.md) 的「两处本机适配」。
> `.status-tmp/` 已进 `.gitignore`，跑完可以删。

> **计数口径（2026-10-15「启动页 + 拆页」这批）**：后端 **673 passed / 0 skipped**
> = 上一版账面的 **597** + **58**（`tests/test_server_sources.py`：两条新端点，当天上午）
> + **18**（`tests/test_server_resilience.py` 16 条 + `tests/test_server_sources.py` 里 `answered_at` 2 条，
> 当天下午的复核修复）。中间那阵子出现过「654 passed + 1 skipped」，那 **1 skip** 是
> `web/index.html` 被改名成 `reader.html` 期间、一条「启动页存在才跑」的既有用例自动 skip 的，
> 新启动页写回去后它自己恢复（**不是**新加的 skip）。
> 前端 **184 passed / 1 skipped**（185 collected）= 上一版账面的 **146 + 3 skipped（149 collected）**
> 加了 **36** 条：`tests/web/test_launcher.py` **22 条**（启动页）+ 状态面板里那条
> 「阅读器页没有状态入口」**1 条** + 启动页只读对抗复核后的修复 **13 条**
> （`tests/web/test_launcher.py` 22→34、`tests/web/test_reader_ui.py` +1）；
> 唯一 skip 是**计费**的真服务用例 `test_real_service_explain_end_to_end_is_billable`（要 `AR_E2E_BILLABLE=1`）。
> 服务没起时前端套件会多 skip 两条真服务联调用例（**估 169 + 3，这批没实测**）。

> **2026-10-18（`rewrite-v7` + 逐段循环 + 一次性基线 + 逐段对话）再补 15 条 ⇒ 后端 688 passed**：
> `tests/test_baseline.py` 5 条（成功 / 事实锚不符 / 缺段补位 / 输出读不通 / 调用失败留证）、
> 逐段循环与 v7 布局 7 条（全文位置、反馈槽、串行顺序、并发重叠、校验回灌、重试用尽不写稿、
> 段内空行并段）、工具层全文 / 反馈透传 1 条、逐段对话 2 条（上下文累积 / 重试保留错误答案与反馈）。
> 另外这批把 `tests/test_survey.py` 里那条**精确键集**断言改成了「必需键全在 + 新键 `answered_at` 存在」
> —— §4 明确允许**追加**顶层键，精确键集会在每次追加时变红（改的是同一个函数，条数不变）。

> 计数口径（2026-10-14 **复核补漏**这一批）：后端 **597** = 上一版 **583** + 本次新增 **14 条用例**。
> 这 14 条来自 **13 个新写的用例函数**：其中 `tests/test_server.py` 那条按两个端点参数化 ⇒ 占 2 条，
> 其余 12 个函数各占 1 条；另有**一条既有用例**只补了断言、**不增条数**（见下面逐文件那段末句）。
> **同一类缺陷的其它写盘入口**（`ingest` 的 `source.md`、`profile.save_profile` 的 `profile.yaml`、
> `events.append_events` 的 `events.jsonl`）统一到「坏路径立即失败 + 点名目录与 errno」这条口径上，
> 顺带订正「`tempfile.TMP_MAX` = 10000」这个**错**的数字口径（本机实测 **2147483647**，近似无界）；
> 相关注释与文档已一并同步订正（涉及的位置不再逐个点数——加一处不该让这段文字立刻失真）。
> 逐文件（下面每个数都是 **pytest 收集到的用例数**）：
> `tests/test_jsonio.py` **5 条**（字节写 `write_bytes` 的逐字节保真 / 原子替换 / 不留残骸、
> 坏目录一次就失败、改名失败清残骸、`TMP_MAX` 与重试上限的数字口径）；
> `tests/test_ingest.py` **3 条**（`source.md` 走共享写盘路径且不留临时文件、坏目录**一次就失败**
> 且两份产物都不落、CLI 坏目录打可读错误并以 **2** 结束——不是卡住、不是裸 traceback）；
> `tests/test_profile.py` **2 条**（`save_profile` 坏目录 `WriteError`：点名目录 + errno、一次就失败、
> 旧的固定临时名 `profile.yaml.tmp` 一个字都不再出现；改名失败清残骸）；
> `tests/test_events.py` **2 条**（`append_events` 坏路径给可读 `WriteError`——不打桩，reader 目录
> 是个同名文件；追加写**只开一次文件**、没有重试循环）；
> `tests/test_server.py` **2 条**（**1 个新用例函数**按两个端点参数化：`POST /api/profile/rating` 与
> `/api/profile/preference` 在坏画像路径上是 **500 + 可读消息**，不是一句裸 `PermissionError`）。
> 另外，**既有的**「事件写失败」那条用例（`test_events_write_failure_is_visible_and_never_faked`）
> 补了「消息点名目录 + errno」的断言，钉住「换成可读错误但契约不变」——**条数不变**，
> 所以这 5 个文件是 **13 个新函数 / 14 条用例**（5+3+2+2+1 个函数、5+3+2+2+2 条用例）。
> 前端 **146 passed + 3 skipped 不变**（本批不动前端）。
>
> 计数口径（2026-10-14 收尾，上一版）：后端 **583** = 上一版 574 + 本次新增 9 条
> （`tests/test_jsonio.py` **新文件 8 条**：坏路径**快速失败**且错误里点名目录与 errno
> （monkeypatch 精确模拟 `os.open` 抛 `PermissionError(EACCES)`，见该文件头「Windows 上怎么造现场」）、
> 「候选名被占用」的重试语义**保留**、重试上限有界、不打桩的真实坏路径（目标目录是个文件）、
> 改名那一步失败要清掉临时文件、正常路径仍原子写且不留残渣；
> `tests/test_why_profile_refs.py` **1 条**：`why.profile` 里的整数**不许被 `str()` 静默转成字符串**，
> 按「非字符串项」给可见说明并照读者自评兜底补齐）；
> 前端 **146 + 3 skipped** = 上一版 144 + 3 skipped + 本次新增 2 条
> （`tests/web/test_compress_action.py`：压缩成功后产物里那份 `profile_refs_filled` 标记就地作废
> ——悬停那一行、`data-profile-filled`、卡片徽标三处一起摘掉，而引用照旧；
> 卡片**正开着**这一段时走「原地替换」分支，位置不变）。
> 上一版 574 = 573 + 2026-10-14 页面标记新增 1 条
> （`tests/test_why_profile_refs.py::test_rerun_path_also_writes_the_structural_flag`：
> 局部重跑那条写盘路径同样写 `changes[i].profile_refs_filled`，未重跑段沿用旧条目；
> 同一文件里另有三条既有用例各补了断言：补齐的段 `is True`、未补齐的段**键不出现**、
> `decisions.json` 的记录形态里不许有这个键）；
> 上一版 573 = 556 + 2026-10-14 复核修复新增 17 条
> （`tests/test_why_profile_refs.py` 15 条：措辞不再说「画像为空」×2、模型给的引用逐条保留、
> 该给引用却给空数组⇒兜底补齐且可见、漏键 / 三种错形状各自可见、本段无可引用与「只有背景」两档**不误报**、
> 空画像模式老路不变、兜底依据只取读者自评（`overrides` 优先）、申报非对象可见、`_DECISION` 字段语义、
> CLI 那一行账；`tests/test_compress_api.py` 2 条：`fact_declarations` 非对象时成功路径可见、
> 松口径下照旧拒写）；
> 前端 **144 + 3 skipped** = 上一版 141 + 3 skipped + 本次新增 3 条
> （`tests/web/test_profile_display.py`：兜底补齐的段卡片挂可见徽标 + 悬停那一行出处说明
> （负向对照：现场那段 `why.note` 里一个字都不含「兜底补齐」——页面若改用正则猜文案就会红）、
> 没有这个结构化字段的段**不挂**标记；`tests/web/test_fixture_schema.py`：夹具里的那一份
> `profile_refs_filled` 样本形状守约（为真才出现、值必须是 `true`、那一段必须有引用））。
> 3 条 skipped **全部**是真服务联调用例（本机 `ar serve --port 8765` 没起就 skip，不是失败）；
> 服务起着时其中的非计费用例会跑起来（2026-10-13 文档里那份「132 passed + 1 skipped」就是那次的口径）。

**前端那套必须用系统 Python 跑**（`python -m pytest tests/web -q`，不是 `uv run pytest`）：
`tests/web/conftest.py` 的浏览器夹具只找本机 Edge 与已缓存内核，且**任何情况下都不触发内核下载**；
用 uv 的 `.venv` 跑时子进程写不了系统 `%TEMP%`，playwright 的 `mkdtemp` 会 `EPERM`——
**那是跑法错误，不是缺陷**（`tools/gen_status.py` 对这两套各自用了正确的解释器）。
后端那套里 `tests/web` 不被收集（`tests/conftest.py` 的 `collect_ignore`），两套互不影响。
1 skipped 是那条**默认跳过**的真服务计费用例（见下）。

上面这两个数字不用手抄：`uv run python tools/gen_status.py` 会真跑这两套、把结果写进
`web/status.json`，**启动页**①状态块里的「状态」按钮读的就是它（口径见 [web/README.md](web/README.md)）。

**静态检查（ruff）的现状——如实记账**（2026-10-14 重核）：`uv run ruff check src tests`
现存 **7 条**（与 2026-10-13 那次**同一批**，见下表），**2026-10-14 的事实锚申报这批新增 1 条、
已清掉**：

| 文件:行:码 | 判据 | 处置 |
|---|---|---|
| `tests/test_fact_anchor_declarations.py:29` F401（`EXIT_USAGE` 未使用） | 该文件是本批**新建**的（不在 HEAD 里） | 从导入清单里删掉（不许 `# noqa` 掩盖） |

下表是 2026-10-13 那次逐条用 `git diff HEAD -- <文件>` 核过的结果：7 条**全在 HEAD 就有的行**上
（上下文行 / 未改动行）⇒ 都是**改动前就有的历史项**，一个字没动：

| 文件:行:码 | 判据 | 处置 |
|---|---|---|
| `src/ar/analyze.py:27:8` F401（`asyncio` 未使用） | 该行与 HEAD 逐字相同（本批在这一带只**新增**了 `from . import tools` 等行） | 历史项，不动 |
| `src/ar/analyze.py:36:21` F401（`.ingest.paragraph_id` 未使用） | 同上：行内容与 HEAD 一致（未被本批的新增行覆盖） | 历史项，不动 |
| `tests/test_no_silent_degradation.py:34:5` F401（`rewrite_env`） | 本批在这个文件里新增的只有 4 处（`+227` 起 2 行、`+399` 起 8 行、`+444`、`+448` 起 20 行），34 不在其中 | 历史项，不动 |
| `tests/test_no_silent_degradation.py:36:5` F401（`sample_fake_handler`） | 同上 | 历史项，不动 |
| `tests/test_no_silent_degradation.py:207:14` F811（形参重名） | 该行（函数签名里的 `rewrite_env`）与 HEAD 逐字相同 | 历史项，不动 |
| `tests/test_no_silent_degradation.py:377:14` F811 | 同上（本批改动只把这一带的上下文行整体下移了 2 行） | 历史项，不动 |
| `tests/test_no_silent_degradation.py:377:62` F811 | 同上 | 历史项，不动 |

**本批（本工作树相对 HEAD 的改动）确实引入过 3 条**——收口记录里「10 条全是改动前的历史项、零新增」
的说法**是错的**；这 3 条已按「清掉、不许 `# noqa` 掩盖」处理，现在 ruff 里一条都不剩：

| 文件:行:码 | 判据 | 处置 |
|---|---|---|
| `tests/web/test_behavior_events.py:22:8` F401（`import pytest`） | 该文件在 HEAD 里**不存在**（本批新建） | 删掉这行导入 |
| `src/ar/survey.py:262:53` E741（歧义变量名 `l`） | 该文件不在 HEAD（同一天未提交的新文件） | 改名成 `label`（`value` / `label` 成对） |
| `tests/test_survey.py:51:5` F401（`has_followups`） | 同上：文件不在 HEAD | 从导入清单里删掉 |

后两条出在同一天**另一路**（问卷）未提交的新文件上、不是第四批自己写的文件，但按上面那把尺子
（相对 HEAD 是否新增行）同样算「本批引入」，所以一并清掉——**口径统一，不挑对自己有利的那把尺子**。
（`PLAN-actions-feedback.md` §13 里那句「零新增」由协调者订正。）

**后端用例不得依赖开发机真实的 `config.local.json`**（2026-10-09 补的纪律）：
`tests/conftest.py` 的 `run_cli` 是**进程内**直调 `ar.cli.main()`，不传 `--config` 时
`load_config()` 读的就是 `paths.DEFAULT_CONFIG_PATH`（开发机那份）。踩坑经过：本机
`config.local.json` 补上 `api_key` 之后，三条「断言模型未配置」的用例当场从绿转红——
`test_serve_handler_wires_app_and_uvicorn`（启动横幅要写「未配置」）、
`test_cli_check_and_rewrite_report_their_exit_codes` 与 `test_rewrite_requires_model_config`
（都要退出码 5）；实际跑出来是「越过模型检查、继续走到缺产物」，退出码变成 2。
规矩：**CLI 相关用例必须显式传 `--config`**——要「没配模型」就用 conftest 的
`nokey_config`（空 `api_key` 的临时配置，顺手把环境变量 `AR_API_KEY` 摘掉，它的优先级高于配置文件）；
要「已配模型」照 `tests/test_rewrite.py` 的 `rewrite_env`（写假 key，客户端换成假客户端）。
`check` / `ingest` / `--mode strict` 与 `analyze --check-only` 在 `load_config()` 之前就分流，不受这条约束。
自证方式：把 `config.local.json` 改名藏起来再跑一遍 `uv run pytest tests -q`，必须同样全绿。

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
失败与降级不进缓存、LRU 容量/淘汰/线程安全、三条既有降级路径的响应键集不变；
成功响应追加的 `usage`（原样透传 `prompt_tokens_details.cached_tokens`）与缓存命中时的空 `usage`。

`tests/test_rewrite.py` / `tests/test_analyze.py` / `tests/test_model.py` 守**前缀缓存那一组改动**
（2026-10-08）：rewrite 模板里「段号 + 本批正文在 user 末尾」（按字符位置 + 批间共享前缀长度断言）、
concepts 模板里「材料正文在末尾」、**第 1 批串行跑完才发其余批次**（假模型记录调用顺序与并发峰值）
+ 预热批失败不拖累其余批次、`usage.cached_tokens` 的嵌套 / 扁平字段累加与逐批明细、
`cached_tokens_of()` 的字段解析（含 `null` 与 `0` 分得开）。

`tests/test_no_silent_degradation.py` 专门守**静默降级与检查器空跑**（2026-10-05 对抗性复核
实测出的五处缺陷，每处至少一条负向对照）：删 / 清空 `changes.json` 后 `check` 必须非零退出、
模型整批失败时整条链必须退出码 6 且产物留 `model_errors`、0 比对项必须报「检查器空跑」、
`rewrite` 不许写 `fidelity.checked: true`、未知动作的文案必须与产物一致（不宣称标了 `deferred`）。

`tests/test_survey.py` 守**读前背景问卷**（2026-10-08 第二批，`PLAN-background-survey.md` §12 的
后端第 1–8 条）：题目定义自洽与枚举闭合、别名与权威 `requires` 在**每个取值组合**下等价
（含 Q3a 选「不确定」），素描**逐字稳定**（同画像必得同文本）、两种缺省措辞分得开、
≤400 字符的丢弃顺序与「整项 / 整条丢、绝不截半句」、核心项不参与丢弃；
画像读写往返与**键集**（顶层键集同步到 `{reader, background, concepts, preferences, overrides}`、
`university` 是嵌套的且键顺序照契约）、老画像（无 `background`）读得出来且不产生副作用；
校验的四类负向（枚举外 / 六选三不是 3 项 / 互斥值同选 / 未知字段）与超长截断标记；
追问端点：成功记账、模型失败 / 未配置 / 回复不合格式一律 200 + `degraded` 且**固定题答案不丢**；
**提示词布局**（素描在固定块、批间首个差异字符仍在段号位、追问模板把作答放末尾）；
`profile_used` / `background_used` 的各种组合与 `why.profile` 不被误覆盖；
`answered_version` 的三种状态、跳过不清空已有作答、版本不一致可见、三条端点不泄漏密钥。

`tests/test_profile.py:107` 的键集断言已同步到新键集（`background` 是第五个顶层键——
漏进 `_normalize` 白名单的键会被**静默丢弃**，那正是本轮最容易踩的坑）。

`tests/test_llm.py` / `tests/test_tools.py` 守**模型调用工具化 + 重试改造**（2026-10-13，新增两个文件，后端用例从 248 条到 382 条；同日复核修复与 §8.6 技术债清理之后 394 条，**第四批之后 514 条、其复核修复后 522 条、收尾复核补 1 条后 523 条**——这几个数都是**当时**的口径，此后每一批的增量见本节开头的计数口径，2026-10-14 两批之后现行 **574 条**，见 `PLAN-llm-tools.md` §8.6）：

- `tests/test_llm.py`：退避序列（假时钟 + 假随机 0.5 → 500 / 1000 / 2000 / 4000 / 8000 / 10000，封顶不再增长）、
  白名单（`AUTH` / `INVALID_REQUEST` / `QUOTA` / `UNKNOWN` 各只尝试 1 次）、上游 `Retry-After` 界内优先与超界放弃、
  **每次尝试从原始请求重建**（重试后原始 messages 逐字未变）、记账条数 = `1 + 实际重试次数` 且 `delay_ms` 与退避序列一致、
  **流提前结束 → `TRANSPORT` 且可重试**（修掉范本盲点的对照用例）、`maxRetries: 0` 只尝试 1 次、
  `retry` 段解析的合法 / 非法 / 缺省三态、
  **`failure_message` 的文案口径**（`已试 N 次、末次 <CODE>`；未配置时写 `已试 0 次`；网关把密钥回显在错误正文里也不漏）。
- `tests/test_tools.py`：注册表恰好**五个**工具（第四批加了 `compress_paragraph`）、名称唯一、
  `schemas()` 可 JSON 序列化且字段齐全；
  每个工具的入参缺字段 / 有多余字段 / 类型不对**都返回 `INVALID_REQUEST` 失败信封**（不抛异常、不发请求、
  `degraded` 点名哪个参数），假模型返回下出参结构符合声明的 schema；模型输出不成结构 → 统一信封的失败分支。
- `tests/test_explain_scope.py`：`/api/explain` 的成功与降级响应都带 `attempts`（旧键一个不动），
  首发失败 + 重试成功时两条账都在、成功响应也看得见重试。
- `tests/test_rewrite.py`：`_usage_summary` 同时打批数与真实请求数（含重试），CLI 端到端一次
  「首发 503 + 重试成功」打 `请求 2 次（1 批；其中重试 1 次）` 而产物里 `usage.requests` 仍是 `1`。

`tests/web/test_survey_ui.py` 守**问卷界面**（同一批）：夹具题目文件与后端定义一致、条件题别名
覆盖后端排除的每个值、提交体键集与后端 `questions[].field` 对得上、表单渲染与条件题联动
（选「初中及以下」时大学题消失）、六选三不是 3 项时前端先拦下不发请求、提交 / 跳过的可见说明、
`?survey=1` 与 `?survey=fixture` 两条夹具分支、`Esc` 与「跳过」同效、焦点回正文、
首次打开弹问卷并写回背景、已答 / 已跳过时不再弹（**且页面上不留任何说明横幅**，原因只写
`__arDebug.surveyBootReason`）、版本更新提示、
追问全链路（入口 → 3–5 个问题 → 作答 → 保存）与四种降级（未配置 / HTTP 失败 / 保存失败 / 夹具模式）、
问卷打开时不吃滚动与点击、关层不留残影、键盘可达。

`tests/web` 用**系统 Python** 跑（不是 uv 的 `.venv`）：`tests/web/conftest.py` 的浏览器夹具
只找本机 Edge 与已缓存内核，**任何情况下都不触发内核下载**。

**第四批（压缩 / 配图 / 局部重跑 / 行为回流）的用例**——全部零真实模型调用：

- `tests/test_rewrite.py`：四动作的决策与判据（`compress` 求「更短 + 句末标点」、`expand` 求「更长 + 句末标点」）、
  `illustrate` 的 `figure` / `figure_error` 与「图不进 `rewritten.md`」的逐字节断言、
  mermaid 形状与 SVG 白名单的每条违规、`merge_artifacts` 的逐段 patch（未重跑段逐字节保留、
  `fidelity` 必重置为 `checked: false`、`meta.rerun` / `rerun_history[]`、退出码 6 按合并后状态重算）；
- `tests/test_compress_api.py`：`POST /api/compress` 的成功 17 键 / 降级 4 键、缓存命中不再调模型
  （**含零事实锚那一段**：命中缓存的响应仍带回「校验空跑」说明、`artifacts_updated:false`、产物一个字节不动；
  用例走两个 app 只共用一个缓存实例，另直接查缓存条目，把「说明来自缓存本身」钉住）、
  单段事实锚不一致时**不写盘且正文不变**、七种降级 `reason` 走 200、顺带写的那条行为事件；
  **申报通道**（`compress-v2`，§14.1）另有一组：缺失 + 已申报 + 松口径 ⇒ 放行且三处可见（产物 / 响应 /
  重跑 `ar check`）、缺失 + 未申报 ⇒ 照旧拒写、已申报 + 严口径 ⇒ 不放行、申报与实测对不上 ⇒ 拒写、
  多出来的锚点 ⇒ 拒写、多申报 ⇒ 放行但可见、提示词带本读者那一档口径块且原文仍在末尾；
- `tests/test_events.py`：`events.jsonl` 的追加写与去重、`kind` 白名单、`reader` 服务端盖章、
  段不存在的丢弃 + degraded、信号折算与权重；
- `tests/test_profile.py`：`applySignals` 的阈值 / 衰减 / 一次只升降一档 / 未达阈值不动 `state` 与 `confidence`、
  `overrides` **一个字符都不改**（专条断言）、`affected_paragraphs` 的反查；
- `tests/test_cli.py`：三个重跑参数的解析与互斥、目标段为空 → 0 且不写盘、`ar feedback` 的 dry-run / `--apply` /
  「内容逐字相同则不写盘」；
- `tests/web/test_figures.py` / `test_compress_action.py` / `test_behavior_events.py`：就地配图与六种失败退回文字、
  mermaid **只注入一次**、点「太啰嗦」刷新正文（含**成功但 `degraded` 非空 ⇒ warn 横幅**、
  **压缩后的申报与「已放行」徽标就地可见**、本次没申报就把旧申报摘掉、
  **产物里那份 `profile_refs_filled` 标记随之作废**（悬停行 / 标记属性 / 卡片徽标三处一起摘掉，
  引用照旧；卡片正开着时走「原地替换」分支、位置不变））、
  行为埋点的缓冲 / flush / 夹具模式一次性降级。

**事实锚「可申报 + 可见」的用例（2026-10-14，§14.1）**——同样零真实模型调用：

- `tests/test_fact_anchor_declarations.py`（20 条）：**退出码 4 的底线**（缺失且没申报照旧失败）、
  松口径下已申报的缺失放行且**产物与命令行两处都看得见**、同一份申报在 `keep_all` 下仍然失败（松 / 严对照）、
  四条不放行的边界（多出来的锚点 / 顺序调换 / 申报与实测对不上 / 申报读不通）、
  「没给画像目录就用产物记的口径、没记就按严口径」（与改动前逐字一致）、
  口径不一致时的可见说明、偏好读写与创建路径（`set_preference` 幂等 / 非法值 400）、
  `POST /api/profile/preference`、`applySignals` 仍然不自动新建偏好、
  提示词两档口径各一段**逐字稳定**的固定块且差异只在那一格、端到端「模型申报 → `changes.json` → CLI 打印」；
- `tests/web/test_fact_declarations_ui.py`（5 条）：卡片里的「本段申报（事实锚）」+ 放行徽标、
  干净段落不显示、悬停那一行（`tip-declared`）、严口径下改挂「未放行」徽标、
  片段模式与事实锚一起隐藏；
- `tests/web/test_fixture_schema.py`：夹具里那条申报样本与实测**自洽**（申报的项在底本里有、
  在改写稿里真的没有），放行条目必须来自一条申报。

**`why.profile` 措辞与兜底的用例（2026-10-14 复核修复，`rewrite-v6`）**——同样零真实模型调用：

- `tests/test_why_profile_refs.py`（17 条）：空自评文案**不再出现「画像为空」**、
  「有背景 / 连背景都没有」两种措辞各自逐字稳定、整条提示词里那句旧措辞也没了且规则 11 在场；
  模型给的引用**逐条保留**（连 `c01:known` 与画像不符也照记）；**该给引用却给空数组**⇒ 逐段降级 +
  兜底补齐 + 两份产物同源 + 该段带 `profile_refs_filled`；**漏键 / 字符串 / 带垃圾项的数组 / 字典**
  四种错形状各自可见；**数组里混进整数**（`[42]`）**不许被 `str()` 静默转成 `"42"` 留下**
  （2026-10-14 收紧：只接受字符串，与其它错形状同一档可见说明——依据见
  `ar.rewrite._normalize_plan_items` 的注释：引用形态是 `c03:unknown`、概念 id 必须形如 `c01`，
  `ar.tools._DECISION` 的 schema 本来就声明 `items: {"type": "string"}`）；
  **局部重跑（`--only`）那条写盘路径同样带这个标记**（未重跑段沿用旧条目）；
  「本段涉及的概念读者都没自评」「只有背景、没有概念自评」两档**不误报**（也就没有这个标记）；
  空画像模式老路一字未变；兜底只取读者自评（`overrides` 优先、读坏了不抛错也不编造）；
  `fact_declarations` 非对象时也可见；`ar.tools._DECISION` 的字段语义（含 `ar tools --json` 输出）；
  CLI 打 `why.profile：N 段有引用 / M 段空` 且与产物逐段账一致；
- `tests/web/test_profile_display.py`（4 条）：空 `why.profile` 时悬停与卡片都显示
  「依据画像：无自评引用」（且页面不出现「画像为空」）、非空时 chips 照旧逐条渲染、
  **兜底补齐的段**卡片挂「骨架兜底补齐（模型没给引用）」徽标 + 悬停那一行出处说明
  （负向对照：现场那段 `why.note` 里一个字都不含「兜底补齐」——页面若改用正则猜文案就会红）、
  没有这个字段的段**一处都不显示**（不误报）；
- `tests/web/test_fixture_schema.py` 也加了一条守夹具里那份 `profile_refs_filled` 样本的形状
  （为真才出现、值必须是 `true`、那一段必须有引用；**样本由 `samples/_build/make_fixture.py`
  装配**，见 `samples/README.md`——此前它只在 `doc.json` 里手改过，重生成夹具就会把这个样本弄丢）。

**产物写盘的用例（2026-10-14 真缺陷修复 + 同日复核补漏）**——针对一次真机验证暴露的真缺陷。
原子写的临时文件建在目标目录内，而 `tempfile.mkstemp` 把 `PermissionError` 当成「候选名被占用」
一起吞掉、按 `tempfile.TMP_MAX` 空转下去 ⇒ 坏路径上「不报错、单核 100%、十几分钟不结束」：

- **`TMP_MAX` 不是「一万次」**（2026-10-14 复核实测订正）：本机（Windows / CPython 3.12.7）
  `tempfile.TMP_MAX = 2147483647`（21 亿级）⇒ 在 Windows 上**近似无界**，旧写法根本没有
  「一万次就放弃」的兜底。实测在一个真不可写的目录（`icacls /deny <user>:(W)`）上，
  1 秒里已经试了 **5000 多个**候选名仍在循环。本实现改用自己写的循环，上限是写死的
  `_TMP_NAME_ATTEMPTS = 32`（与 `TMP_MAX` 无关；`tests/test_jsonio.py` 有一条用例把这个常量
  与量级关系一起钉住）；
- **坏路径快速失败**：目录不可写 ⇒ `WriteError`（**不是** `FileExistsError`「名字用完了」那句），
  消息里点名**哪个目录** + **原始 errno** + 系统给的话，且**一次就失败**（用 monkeypatch 精确
  模拟 `os.open` 抛 `PermissionError(EACCES)` 并数尝试次数）；
- **原有的「同名文件已存在」重试语义保留**：`FileExistsError` 换名字再试、最终照常写成功；
  每个候选名都被占用时按**有界**次数（`_TMP_NAME_ATTEMPTS = 32`）放弃并给可读错误；
- **不打桩的真实坏路径**：目标目录本身是个文件 ⇒ 立刻可读报错；
- **改名那一步失败**（临时文件建出来了、`os.replace` 被拒）：清掉临时文件 + 可读错误 + 不留半个目标文件；
- **正常路径仍原子写**：同名旧文件被替换、内容逐字落盘、行尾统一 LF、**不留临时文件残骸**；
- **两条写盘路一个口径**（复核补漏）：`write_text`（文本 / JSON）与 `write_bytes`（字节，
  `ingest` 的 `source.md`）共用 `ar.jsonio._atomic_write` 这段内核——上面的坏路径断言在两条路上
  各钉一遍。

**同一类缺陷的其它入口（2026-10-14 复核补漏，全部逐条登记）**——`ar/` 里所有会写文件的路径：

| 入口 | 写什么 | 走哪条实现 | 目标目录不可写时 |
|---|---|---|---|
| `ar.jsonio.write_text` / `write_json` | `ingest` 的 `source.meta.json`（段落表 + 锚点）、`concepts.json`（**仅模型答了却不成结构时**还有一个 `concepts.raw.txt`，`ar/analyze.py` 约 :490-493）、`changes.json` / `decisions.json` / `rewritten.md`、`feedback.json`（`ar feedback --apply` 的回流水位线）、`survey.json` | 本模块原子写（临时文件 + `os.replace`） | **立刻 `WriteError`**（点名目录 + errno，CLI 2 / HTTP 500），一次就失败 |
| `ar.jsonio.write_bytes` | `ingest` 的 `source.md`（与源文件逐字节一致） | 同上（同一段内核） | 同上（**本批补漏**：此前是裸 `tempfile.mkstemp`，实测 1 秒空转 5000+ 次） |
| `ar.profile.save_profile` | `profiles/<reader>/profile.yaml`（自评 / 偏好 / 问卷 / `ar feedback --apply` 都经它） | `jsonio.write_text` | 同上（**本批补漏**：此前是固定临时名 `profile.yaml.tmp` + 裸 `write_text`，抛裸 `PermissionError` ⇒ CLI 退出码 1 + traceback、HTTP 只有「服务内部错误」） |
| `ar.events.append_events` | `profiles/<reader>/events.jsonl`（**唯一一处非原子写**：纯追加的日志不该整文件重写） | `open(path, "a")`，无重试 | **立刻 `WriteError`**（点名目录 + errno）；HTTP 侧照旧 200 + `ok:false` + `written:0` + `degraded`（事件是尽力而为的采集，契约不变） |
| `ar.survey` / `ar.cli` | 问卷记录、回流水位线、画像 | 全部转交上面几条 | 随上面几条，无自己的写盘代码 |
| `tools/gen_status.py` | `web/status.json`（开发脚本，不进 CLI 契约；该文件已进 `.gitignore`） | 裸 `Path.write_text` | 裸 `OSError` + 非零退出（**开发脚本，故意不包**：它自己就把子进程 `TEMP` 指到项目内并在 `finally` 里清掉，失败要显式可见） |
| `samples/_build/make_fixture.py` | 夹具 `samples/<id>/doc.json` / `explain.json`（开发脚本，要重生成才跑） | 裸 `Path.write_text` | 同上（开发脚本；这几份产物本身**入库**，坏目录会让脚本立刻失败，不会静默产出半份夹具） |

（`tests/` 下的 `write_text` / `write_bytes` 是**用例自己的夹具准备**，一律写在 `tmp_path` 里，
不是产品写盘路径，不在这张表里。）

结论：**没有第三个「会空转」的入口**——会按 `TMP_MAX` 空转的只有 `tempfile.mkstemp`
（`ingest` / `profile` 两条已改掉），其余入口要么走同一段原子写内核，要么是一次性的 `open`。

其中「目录不可写」与「候选名被占用」两条用的是 monkeypatch 精确模拟而不是真造一个不可写目录：
Windows 上新建目录默认当前用户可写（`os.access(dir, os.W_OK)` 对 `icacls /deny` 过的目录照样
返回 `True`，实测确认过这个假阳性），改 ACL 要动 `icacls` 且受管理员权限影响，把目录设成只读
也拦不住在它里面建文件 ⇒ **造不出稳定可复现的现场**；打桩打在「系统在写的那一刻拒绝」这个
真实失败点上（只拦目标目录里的候选路径，其余路径转交真的 `os.open`），不是绕过被测代码。
**本次复核另外在真 `icacls /deny` 目录上实测过一遍**（产物目录先建好、再对目录本身拒绝写）：
两条入口都是 `WriteError` + **尝试次数 = 1**，对照的旧写法 1 秒 5000+ 次仍在循环
（现场做法与删法见本文件的 `icacls <dir> /deny "<user>:(W)"` → 用完先 `/remove:d` 再删目录）。

**临时目录纪律（跑测试或联调脚本时注意）**：临时目录会落到项目根，跑完请自行清理。本机 `%TEMP%`
对 `uv` 起的子进程不可写，所以用 `tempfile` 的测试 / 联调脚本会把临时目录建在**项目根**
（名字形如 `tmp5y9tmr58`，里面是 `profiles/local-default/` 的 `profile.yaml` 与 `survey.json` 副本）。
`.gitignore` **没有**覆盖它们，`git status` 里会看见一串 `tmp*`。2026-10-08 那次背景问卷任务里
一次留下过 4 个（**已清理**）——要么显式指定临时目录、要么跑完自己删掉（`tools/gen_status.py`
就是把子进程的 `TEMP` / `TMP` 指到项目内 `.status-tmp/` 并在 `finally` 里删掉），**别把它们留在项目根**。

## 8. 已知限制（别把「跑通」当「可用」）

1. **不做理解力测验**：调研实测「主观觉得好读」与「真的读懂」相关系数不到 0.1，
   本骨架没有任何测量读者是否真的读懂的手段（读者侧测验属后续里程碑）。
   **读前背景问卷不是理解力测验**：它问的是读者的既有背景（学历 / 专业 / 自报熟悉领域 / 常读材料），
   答的是「他可能熟悉什么」，不是「他读懂了多少」；「按材料概念出辨识题 / 复述题」那三层仍未实现。
2. **只有一个能用的档位**：`free`（自由重述 + 事实锚）。严格档（逐句可回溯）接口留了位但没实现，
   跑 `--mode strict` 会明确报错——**别把它当成「跑过就是严格档」**。
3. **压缩 / 配图 / 行为回流 / 局部重跑：逻辑已实现，但只有假模型 + 夹具覆盖，真机未跑**
   （第四批，2026-10-13）：四个动作（`expand` / `compress` / `keep` / `illustrate`）都落地了，
   `deferred` 机制保留但正常情况下不再出现；`ar rewrite --only / --retry-failed / --from-signals`
   与 `POST /api/compress` / `POST /api/events` / `ar feedback` 都有用例。
   **证据边界**：这些用例全部注入固定响应（假模型）、夹具或路由级假后端，
   **一次真网关调用都没有**——「模型在真机上会不会按四动作判据产出合规的压缩稿 / 图源码」**没有实测记录**。
   （2026-10-14 补过一次**窄范围**真机验证，覆盖的是**局部重跑这条路本身**与
   `rewrite-v6` 的 `why.profile` 措辞 / 保留，**没覆盖压缩稿、图源码与行为回流**，
   范围与限制见第 23 条。）
   真 mermaid 库的渲染**有一条用例覆盖**（`tests/web/test_figures.py` 里那条**不 stub** 的用例：
   真库由本机静态服务提供，滚动到夹具 p013 那条 flowchart 后断言渲染出的 `svg` 的 `class` 含
   `flowchart`——假库做不出这个 class），但**只覆盖夹具里那一条简单 flowchart**，
   `sequenceDiagram` / `mindmap` 等语法与长图性能**仍未验**；其余前端用例用的是几十字节的假库替身。
   别把「用例全绿」读成「真机可用」。
4. **事实锚只覆盖数字 / 引号引文 / 书名号《》三类**，且是**按段、按数量与顺序**的严格比对：
   人名、术语、转述无法自动守住；把事实挪到别的段落、在同一段里重复引用同一句引文、
   顺序调换，都会报成违规——**会有假阳性**，报出来时必须人工看一眼原文，不要一律当成模型改坏。
   真机第一轮就踩到过一条：模型在展开段里把「形成价值的实体」这同一个引文引了两次，
   `check` 退出码 4 点名 `p021`；收紧提示词（同一段里同一引文只能出现原文那么多次）后通过。
   **图不参与事实锚**（SVG 坐标里的数字无法避免）：图源码里多出原文没有的数字 / 引文 / 书名号
   **不阻断**，只记一条降级——所以「事实锚全过」不覆盖图的内容对不对。
   **2026-10-14 起这条比对可以「申报后放行」**（§14.1，见 §3 的那一节）：模型在
   `changes[i].fact_declarations` 里逐条申报它放弃了哪几项锚点，读者画像里那条偏好
   `pref_fact_anchor`（`allow_declared_drops`）说了算放不放行；**没申报的缺失照旧失败**，
   多出来的锚点、顺序调换、申报与实测对不上**任何口径下都不放行**。
   仍属边界：申报是**模型自述**，它说了什么就记什么，网只管「没申报就报错」——
   「模型有没有滥用申报」目前没有额外判据（人工复核申报条目即可：产物里逐条列着）。
5. **中文难度量尺不成熟**：`difficulty_hint` 只是模型给的辅助信号，不作验收标准。
6. **空画像时依据很弱**：盘上那份 `profiles/local-default/profile.yaml` 的 `concepts` 自评仍为空，
   `background` 已于 2026-10-09 填上（`answered_at: 2026-10-09T09:00:13+08:00`，所以 2026-10-13 真机
   那次产物是 `profile_used: true`、「仅背景，无概念自评」档），不再是「无画像」；**连 `background`
   也为空**时才走「无画像」降级路径，那时决策只能依据概念清单的难度提示（产物里已显式标注）。
   要让改写「因人而异」，有两条路：
   填一次读前背景问卷（页面首次打开材料时弹，落 `background`），或用页面的自评勾选
   （`POST /api/profile/rating`）写 `concepts` 自评。**注意口径**：背景非空即算「有画像」
   （`profile_used: true`），但那条路径下每段依据写在 `why.note` 里、`why.profile` 保留模型引用——
   「有没有画像」与「依据强不强」是两件事。
7. **单读者**：画像格式按「任何读者都能用」设计，但当前只服务 `local-default` 一个人。
8. **token 成本随材料线性增长**：25 段约 3.6 万 token（analyze 7,485 + 改写 28,622，2026-10-08 热缓存那轮）；
   冷跑（提示词第一次发）同规模约 5.8 万 token；读中按需展开（讲解卡片）是另一笔按次付费的账，
   目前每次 12–46 秒。
9. `data/` 与 `profiles/` 的**运行产物不进版本库**；若将来把本项目纳入 git，请自行
   `.gitignore` 掉 `data/`、`config.local.json` 与 `profiles/` 的实际取值。
10. **退出码 6 只说明「有段落没被改写」，不含「改写质量」**：模型整批失败 → 整篇保持原文
    （退出码 6）；模型正常但全篇都判 `keep` 也是**合法结果**（退出码 0，真机 25 段里就有 17 段
    是模型主动保持）。二者靠 `meta.model_errors` / `meta.unplanned_paragraphs` 区分，
    不靠「展开段数」——**0 个展开段不等于失败，有 model_errors 才是**。
11. **部分失败可以只补失败段（第四批起），但仍要人工触发**：退出码 6 的那次产物已经落盘、能读
    （失败批次退回原文）。`ar rewrite <id> --retry-failed` 会只重跑 `meta.model_errors[].paragraphs`
    ∪ `meta.unplanned_paragraphs` 并逐段合并，退出码 6 按**合并后**的真实状态重算；
    但这个入口**不会被自动触发**——`ar check` 报了 6 之后要不要补、补几次，仍然由人决定。
    合并是**逐段 patch**：未重跑段逐字节保留，`fidelity` 一律重置为未校验（要复核得重跑 `ar check`）。
12. **讲解缓存只在进程内**：容量 128，**服务重启即空**，按**画像指纹**（`profile.yaml` 的路径 + 字节数 +
    mtime_ns）失效；多进程 / 多实例部署时各持一份（本骨架就是单进程 uvicorn）。它算的是「别为同一件事
    重复付费」的账，不是持久化结果——**别把缓存当产物**：`data/` 里没有它，重启后第一次讲解照旧重新调模型。
    它**没有「重新生成」入口**：同一段重复点、刷新页面都拿到同一份文字（要换只能改画像或重启，见 §4）。
13. **显示设置允许手动组合出读不清的正文**（背景 3 档 × 文字 2 档，6 种组合里有 3 种是低对比：
    纸白 / 暖黄 + 浅灰、深色 + 深墨）。这类组合不拦（用户有权这么选），但设置面板里会显示实测对比度
    告警（阈值 AA 4.5:1）；「浮在内容之上的面」（顶栏 / 徽标 / 抽屉 / 卡片 / 浮条 / 设置面板）按**背景档**
    配对底色与文字，与文字颜色档无关，所以面板里的正文类文字在任何组合下都 ≥ 4.5:1。
14. **前缀缓存（prompt caching）要看网关的门槛，不是「排好序就一定命中」**（2026-10-08 实测本机网关）：
    提示词里会变的内容排在末尾（`ar.prompts` 的「前缀缓存纪律」）只是**必要条件**——网关按「从头逐字相同的
    token 前缀」给折扣，本机网关的实测口径是**共享前缀 ≥ 约 2048 token 才算命中**（命中量向下取整到 64 的
    整数倍）；共享前缀更短时，只有「差异落在最后一个块内」（尾巴 ≤ 约 64 token）才算命中。所以：
    - `rewrite` 的固定前缀约 1040 token（1909 字符）、批间差异约 500 token ⇒ **同一轮里 6 批互不命中**
      （重跑 A 实测 `cached_tokens: 0`），预热串行化本身也救不了（这是网关门槛，不是调用顺序问题）；
    - **重跑**（同一材料、同一提示词逐字相同）命中 96%（重跑 C 实测 `cached_tokens: 9088` / prompt 9489），
      墙钟 151.6 → 63.2 s；
    - 想让**同一轮内**也命中，得把固定前缀做到 ≥ 约 2048 token（≈ 3750 字符）——2026-10-08 时没做；
    - **2026-10-18 `rewrite-v7` 复测：同轮已经命中**。全文进固定上下文后，批间共享前缀 = 9889 字符 /
      5364 token：分批模式第 1 批冷启 0、第 2–6 批 4096–5376（合计 23040 / prompt 35535，65%）；
      逐段 25 次时命中 97–99%。**旧结论「同一轮 6 批互不命中」按 v7 之后作废**（五版对照见 §2.1.2）。
    实测口径（2026-10-08，同一网关 `deepseek/deepseek-v4.1-flash`，用 `max_tokens=1` 的探针请求量的）：
    共享 2100 token + 1400 token 尾 → 命中 2048；共享 1800 + 1400 尾 → 0；共享 500 + 20 尾 → 命中 512；
    共享 500 + 200 尾 → 0。命中量、逐批命中量都记在产物里（`meta.usage.cached_tokens` /
    `meta.batch_usage[]`），`/api/explain` 的成功响应里也有 `usage` 字段——**别只看网关账单猜**。
15. **读者素描 ≤ 400 字符，超长时整项 / 整条丢弃**（不截半句）：「读者背景」那一行装不下时，
    按固定顺序丢整项（先 Q5 常读材料 → Q6 偏好讲法 → Q2 六选三 → 最后 Q4b 补充说明），
    尾巴写「…（已省略 N 项）」；追问摘要另按整条从后往前丢，写「…（追问摘要省略 N 条）」。
    **核心项不参与丢弃**（学历 / 学科门类含专业 / 门类缺失时单独成项的专业 / 自报熟悉领域），
    连核心项都装不下时**直接报错**、不静默截断。丢弃集合永远是候选顺序的前缀。
    也就是说：读者填得越多，**丢掉的可能越多**——素描是给模型看的一句话速写，不是答案的完整副本
    （完整答案在 `profile.yaml` 与 `survey.json` 里）。
16. **模型追问不读材料正文**（用户 2026-10-08 拍板）：追问的输入只有读者的固定题作答与素描草稿，
    所以问出来的问题**不可能**贴着本篇材料的具体概念问（「你说熟悉经济金融——读过《资本论》原文吗？」
    这种可以，「第一章里那个 X 你懂吗」这种不会出现）。要按材料追问得等「按材料出题」那一层。
17. **讲解链路（`/api/explain`）不注入背景**：读者填了背景，讲解卡片仍只按概念状态讲
    （`profile_hint` 只取 `concepts` 的状态）。本轮明确没做，后续可扩。
18. **「键不存在」不等于「值为 false」**（读前背景问卷那一层的真机验收记录见 §2.1.1）：
    2026-10-08 及更早的产物（`meta.prompt_version: rewrite-v2`）里根本没有 `background_used` /
    `background_fingerprint` 这两个键，别把缺键读成「这次没用背景」；旧产物的指纹保留、
    **不得覆盖成新值**（那是唯一能判断「这次用的是哪版背景」的依据）。
19. **模型自称「画像为空」+ `why.profile` 全空**（2026-10-13 实测，**2026-10-14 已定位并修**）：
    这份产物 `why.note` 有 **7 段**（p008 / p009 / p010 / p011 / p022 / p024 / p025）写「画像为空 /
    未提供自评」，其中 **4 段在同一句里又引用了背景**（p024 同时写「读者画像为空」与
    「读者是艺术学门类本科」）；`why.profile` **25 段全是空数组**。但背景**确实**被用上了：
    p018 / p022 / p024 / p025 逐字引用了专业、学科门类、常读材料与「补前置概念」偏好；
    `meta` 的记账字段本身是对的（`background_used: true` / `background_fingerprint: c9037bb4`）。
    **根因（查证结论）**：那句话**不是模型自己发明的、也不是代码丢的**——
    空数组是**模型照着提示词给的**。`ar.rewrite._format_profile_block` 的「空自评」分支当时写的是
    「读者 local-default 没有提供任何概念自评（**画像为空**）：请只依据下面的概念难度提示判断，
    并**在 why.profile 里写空数组 []**」：同一个读者那次确实没有概念自评（只有背景），
    于是「画像为空」被模型复读进 `why.note`，「写空数组 []」被照办成 25 段空数组。
    证据：① 盘上 `decisions.json` / `changes.json` 的 25 段 `why.profile` 都是 `[]`
    （`Decision.to_record()` 与 `rewrite()` 都只做 `list(why["profile"])` 透传，非空就会被留下）；
    ② 同一份产物 `meta.profile_note` 是「仅背景，无概念自评」，正是走那个分支的判据；
    ③ 用当时的画像复算那段文本，逐字得到上面那句（含「画像为空」与「写空数组」）。
    **修法（2026-10-14，两条一起做）**：① 那段措辞改成如实说「读者**没有提供任何概念自评**」
    （不再出现「画像为空」，也不再指示模型写空数组），提示词升 **`rewrite-v6`**
    （单段压缩共用同一个画像块，同升 `compress-v3`）；② `why.profile` 的语义写进提示词规则 11
    与 `ar.tools._DECISION`（**有自评时必须逐条保留模型给的引用**），产出链路对
    **漏键 / 给错形状 / 该给引用却给空数组**一律可见（`meta.degraded` + CLI 一行账）并只照读者自评兜底补齐；
    ③ 补齐过的段在 `changes[i].profile_refs_filled` 上留一个结构化标记（为真才出现），
    `ar serve` 的卡片据此挂「骨架兜底补齐（模型没给引用）」徽标（此前这条信息只在产物、命令行与悬停的
    `why.note` 长句里，卡片上看不出来）。
    **盘上那份整篇产物仍是老措辞**：**2026-10-14 已用单段局部重跑在真网关上验过这一层**
    （`ar rewrite … --only p008`，两档、合计 2 次调用 / 12694 token ⇒ 新措辞与新引用都照做了，
    见第 23 条）；但**整篇产物没有被重跑**（用户已决定暂不重跑），
    它的 `prompt_version` 仍停在 `rewrite-v3`、`why.note` 仍是老那句。

20. **压缩缓存键只含画像指纹，不含产物状态**：在线压缩的键 =
    `(source_id, paragraph, "compress", "", 画像指纹)`，指纹只看 `profiles/<reader>/profile.yaml` 的
    `(路径, 字节数, mtime_ns)`。所以**整篇 `ar rewrite` 重跑过之后**（正文换了），进程内那条旧压缩条目
    仍然可能被命中：响应里 `cached: true`、`artifacts_updated: false`（这次没写盘、也不声称产物与缓存一致），
    但前端拿到的 `text` 可能与盘上的产物**不同步**。要复核就跑 `ar check`；要强制重新压缩，
    改一次画像（换指纹）或重启服务。这条是**已知取舍**：把产物状态也放进键会让「同一段重复点」
    在每次写盘后都重新计费。
21. **行为事件是尽力而为的采集**：事件落盘失败（`events.jsonl` 写不下去）、事件送不出去
    （`POST /api/events` 失败）、段号不在 `source.meta.json` 里被丢弃——这三种都不影响阅读与压缩结果，
    压缩那次点击**照常写盘、`ok` 照常 `true`**，只在响应的 `degraded` 里留一条可读说明，
    前端在**成功路径**上照样打一条 warn 横幅（采集不假装成功，但也不阻断主流程）。
    没有重试队列、没有本地补送：丢掉的事件就是丢了，`ar feedback` 只能用已落盘的那些折算画像。
    另有一处**幂等窗口**的边界（2026-10-13 复核修复 F2）：`--apply` 的去重靠
    `profiles/<reader>/feedback.json` 里**最近 500 条**事件 id，而 `events.jsonl` 只增不减——
    超过 500 条后最早的 id 会被挤出窗口，那几条事件若还在文件里就会被**再折算一遍**
    （挤出时命令行给一条可见降级说明，不静默）。要彻底避免就把 `ar.events.FEEDBACK_RECORD_KEEP` 调大。
22. **事实锚口径的三条边界**（2026-10-14，§14.1）：

    - **口径看的是「现在的标准」，不是「当时的标准」**：产物 `meta.fact_anchor_policy` 记着改写时
      生效的那一档，但 `ar check` 判的永远是**读者画像现在**那一档（`fidelity.fact_anchor_policy`
      因此会被这一次比对覆盖）。两者不一致时命令行给一条可见说明，**不会静默换口径**；
      要两边一致就重跑 `ar rewrite`（口径进了提示词的固定块，换口径等于换提示词）。
    - **两条路径同一套口径**（2026-10-14 收尾）：`POST /api/compress` 与 `ar check` 走同一个
      `ar.fidelity.compare_blocks_detailed`，口径也取同一处（读者画像 `pref_fact_anchor`）。
      `compress-v2` 提示词给了在线压缩申报通道，所以**同一段不会出现「`ar check` 放行、点『太啰嗦』
      却被拒绝」**；底线一个字没松：没申报的缺失 / 多出来的锚点 / 顺序调换 / 申报与实测对不上，
      两条路径都拒写（拒绝只是不写盘，正文一个字节不动，不会悄悄丢锚点）。
    - **多申报照旧放行，但要说出来**：段落锚点一个没少（原样照抄）却申报「放弃了某项」时，
      放行结论与退出码都不变（本来就没有缺失可放行），但命令行逐条打 `⚠ 事实锚多申报：…`、
      产物留 `fidelity.over_declared[]`、页面挂「本段一个锚点都没少：模型多申报了」徽标。此前
      `same_facts(...) → continue` 在校验申报之前短路，这种乱申报一个字都不提（2026-10-14 收尾修）。
    - **偏好没有页面 UI**：写入只有 `POST /api/profile/preference` 与手改 `profile.yaml` 两条路
      （`set_preference` 是同一个纯函数）；页面只**显示**口径与放行结论，不提供改它的入口。
      另：取值写坏（拼错 / 没有 `value`）**一律退到严口径**并给可见降级，永远不会悄悄放宽。

23. **2026-10-14 的真机验证：范围很窄，别读过头**（真网关，模型 `deepseek/deepseek-v4.1-flash`）。
    这一批（`rewrite-v6` / `compress-v3` 的 `why.profile` 语义与措辞）在此前**只有假模型覆盖**，
    这次补了一次真机记录，用的是**单段局部重跑**（第四批的 `--only`）：

    ```
    uv run ar rewrite <source_id> --reader <读者> --only p008
    ```

    两档、共 **2 次调用 / 合计 12694 token**：

    | 档 | 读者画像 | 观测结果 |
    |---|---|---|
    | 现网读者 `local-default` | `concepts: {}`（无概念自评）、只有 `background`（问卷填过） | `why.note` 里**不再出现「画像为空」**，改为如实引用背景；`why.profile` 仍是 `[]`——**这一档空数组是合法值**（「没有概念自评」≠「画像为空」），**没有触发**代码兜底补齐 |
    | 临时造的两条概念自评的读者 | 有概念自评 | `why.profile` = `["c02:unknown","c03:partial"]`，与提示词「读者画像」块**逐字、逐序一致**；模型遵守了新增的提示词规则 11（有自评时逐条保留引用）；**也没有触发**代码兜底补齐 |

    **必须如实限定范围**（别把这条读成「整条链真机验收过了」）：

    - 只验了 **1 段（`p008`）× 2 档**，**不是整篇、也不是多轮**；没验压缩稿、图源码、
      行为回流、事实锚申报在真机上的表现（那些仍然是零真机记录，见第 3 条）；
    - 验的是「模型在真机上肯不肯照新措辞与新规则 11 来写 `why.profile`」这一件事，
      **不是**改写质量或阅读效果（本项目没有理解力测验兜底，见第 1 条）；
    - **整篇产物没有被重跑**：盘上 `data/capital-v1-ch01/` 那三份仍是 2026-10-13 12:27 的
      `rewrite-v3` 产物（`prompt_version: rewrite-v3`、`p008` 的 `why.note` 仍是老那句），
      这次两档验证的产物也没有留在 `data/` 里。用户已决定**暂不重跑整篇**；
    - 这次跑动**只覆盖 `--only` 那条入口**；`--retry-failed` / `--from-signals` 仍然只有假模型覆盖。

24. **上传摄取与「边读边改」的并发边界**（2026-10-15，启动页那批的只读对抗复核实测）：
    - `POST /api/ingest` 带 `overwrite: true` 时，若**目标材料的既有 `source.meta.json` 正被别的进程
      独占**，会回 500：根因在**摄取模块**读旧锚点那一步（`ar.ingest`，命令行 `ar ingest` 走同一条路），
      本批没动它；HTTP 侧把「文件在但读不出来」在**清单端点**上修成了降级（见 §4 的 `/api/sources`），
      摄取这条读路径**留待裁决**。
    - **`source.md` 与 `source.meta.json` 不是一个原子对**（摄取顺序写、无回滚）：Windows 上
      `os.replace` 撞上「目标文件被别的进程占着」会失败——本批给 `jsonio` 加了**有界短退避重试**
      （≤3 次 / 总等待 ≤200 ms）并让失败文案点名「被占用」而不是「目录不可写」，但**重试等完仍失败**时
      可能留下「新 `source.md` + 旧 `source.meta.json`」这种**持久不一致**（要另一个进程一直占着句柄
      才撞得上；撞上后重跑一次摄取即可恢复，按段号对齐的产物会互相打架）。POSIX 上不存在这个问题。
    - 三处**已测出、未修**的小口径（都不是新端点的功能问题，属「要不要现在收拾」的取舍）：
      ① `/api/{rest:path}` 兜底路由只认 GET —— `POST /api/nope`（路径不存在）回 405、`GET /api/ingest`
      （路径存在、方法不对）回 404，且 405 不带 `Allow`；
      ② `source_id` 没有规范化 —— `"a."` / `"a"` / `"A"` 会落到同一个目录，`source.meta.json` 里记的 id
      可能与目录名不一致；
      ③ Windows 设备名（`nul` / `con` …）当 `source_id` 会 500 + 误导文案。
    - 同一次复核**实测确认没问题**的（别重复挖）：清单端点**只读**（整棵 `data/` 树的
      (路径, 字节数, mtime_ns) 快照前后一致，含回 500 那次）；路径注入（`..\..\evil.md`、绝对路径、
      UNC）只落成 `data\evil` 这类、`data/` 之外零新增；重名 409 **不写盘**；暂存目录只删自己那份；
      同一个新 id 并发进来是 200 + 409、不互踩。

（临时目录纪律见 §7 末段——它原本是本章第 20 条，属开发提示、不属于「已知限制」，已移出。）

## 状态快照（2026-10-12 自工作区 AGENTS.md 迁入）

工作区 `AGENTS.md` 的目录表里本目录那行原先自带一段状态细节，2026-10-12 索引行瘦身时**逐字迁入**如下
（索引行现在只留「目录 + 一句话作用 + 详见 README」；与本文既有章节重复的部分按原样保留，便于对照。
唯一一次例外：末句「真机验收」里那条「填背景 → `ar rewrite`」的状态标记按 2026-10-13 实测改成 ✅，
见 §2.1.1）：

> 阅读文本自适应重写工具(2026-10-08):段锚点 → 概念清单 → 逐段改写(展开/保持) → 事实锚校验 → 阅读器;`uv run ar ingest/analyze/rewrite/check/serve`(`ar serve --port 8765`,`web/` 零构建静态页)。新增选中片段讲解(`/api/explain`)、显示设置(3 背景×2 文字×4 字号)、读完提示=正文末尾流内块、两级讲解缓存(LRU 128,重启即空)、提示词重排+预热(重跑命中 96%)、顶栏「状态」入口(面板两块=测试结论:两套用例数字+耗时+生成时间;本次修改:git 未提交改动清单;数据源=同源静态文件 `web/status.json`,由 `tools/gen_status.py` 真跑两套测试生成,文件缺失时面板写明生成命令)、横幅显示纪律(同文本只留一条+重复计数、每条 `×` 可关且键盘等价、成功类 8 秒自动消失、降级类不自动消失)。**读前背景问卷已实现**:跨材料六道固定题(条件题联动/可整场跳过)+可选模型追问,逐字稳定(≤400 字符)的「读者素描」注入 `rewrite-v3` 提示词固定块,答案落画像 `background`(顶层键,`university` 嵌套),记录 `profiles/<reader>/survey.json`(不进版本库),三端点 `GET/POST /api/survey`+`POST /api/survey/followup`,产物 `meta` 追记 `background_used`/`background_fingerprint`。⚠ 这与设计文档里「按材料概念出题的读前短测验」是**两件事**,后者仍未实现。未实现:严格档、压缩、配图、按材料出题、行为回流、网页/PDF、跨材料复用、局部重跑。⚠ 事实锚按「段+数量+顺序」比对,挪段/重复引/调序即违规。⚠ 问卷卡片与状态面板的滚轮归各自模块按坐标接管(修掉了「滚动穿透到背后正文」)。⚠ `ar serve` 只在进程启动时读一次配置,改完必须重启才生效;`config.local.json` 已接上模型(四键),密钥来源=本机 DSH `llm-auto` 当前生效首跳 commandcode 的 `~\.dsh\.credentials.yaml`→`refs.CMD_API_KEY`(只写来源不写值)。真机验收:✅ 模型追问端点(5 问/11.3 秒/1418 token,非降级);✅ 2026-10-13 完成「填背景 → `ar rewrite` → 核对产物 `background_used`/`background_fingerprint` 与每段依据」(约 3–6 万 token)。测试 后端 248/前端 98 passed+1 skipped(8765 起着;没起 97+2,两条真服务用例 skip)。

对应关系（逐项核对表见 `dsh\dsh-token-savers-0.2.0\slim-a\slim-a.md`）：

- 命令与端点、`rewrite-v3` / 读前背景问卷 / `background_used` / `background_fingerprint` / `profiles/<reader>/survey.json`：
  本文 §2、§3、§4 已有对应章节；
- 测试数字（后端 **523 passed**（快照正文按逐字迁入保留，里面的 248 是 2026-10-12 当时的数字，
  382 / 394 / 514 / 522 是 2026-10-13 的数字，以本文 §7 为准）/
  前端 132 passed + 1 skipped，服务没起时多一条 skip）：本文 §7；
- 「未实现」清单：快照里那八项中，**压缩 / 配图 / 行为回流 / 局部重跑已于第四批实现**
  （§8 第 3 条：逻辑已实现；**真机验收只有局部重跑有一份窄范围记录**——1 段 × 2 档，
  见 §8 第 23 条，其余三项仍为零真机记录），**严格档、按材料出题、网页 / PDF、跨材料复用**仍未实现
  （§8 第 2 条与已实现/未实现对照表）；
- 真机验收「✅ 模型追问端点（5 问 / 11.3 秒 / 1418 token）」「✅ 填背景 → `ar rewrite` →
  核对产物 `background_used` / `background_fingerprint`（2026-10-13 完成，见 §2.1.1）」
  与两级讲解缓存 `LRU 128`、问卷卡片/状态面板滚轮接管（修掉「滚动穿透」）：**本次仅存于本快照**（原文逐字）。
- 快照里没有、2026-10-14 新增的一层：**事实锚「可申报 + 可见」**
  （`fact_declarations` + 读者画像偏好 `pref_fact_anchor` + `fidelity.waived` / `fidelity.over_declared`
  + 端点 `POST /api/profile/preference` + `ar check --reader`，提示词 `rewrite-v4 → rewrite-v5`；
  同日收尾把**在线压缩**也接上同一条口径，提示词 `compress-v1 → compress-v2`）：
  见本文 §3 的「事实锚『可申报 + 可见』」、§8 第 4 条与第 22 条、`DESIGN.md` §10.1c、
  `PLAN-actions-feedback.md` §14 第 1 条的状态块。

## 许可

MIT，见 [LICENSE](LICENSE)。
