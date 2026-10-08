# web/ — 阅读器前端（纯静态、零构建）

HTML + CSS + 原生 ES 模块，没有打包器、没有 npm 依赖、不联网取任何东西。
由服务端静态托管（W1 的 `uv run ar serve` 把 `/` 指向本目录），也可以直接起一个静态服务打开。

## 数据来源（双模式，PLAN-skeleton.md §4）

| 模式 | 触发 | 请求 |
|---|---|---|
| 夹具模式 | URL 带 `?fixture=<source_id>` | `../samples/<source_id>/doc.json`、`explain.json`（同源静态文件，失败时回退 `/samples/...`） |
| 正式模式 | 不带 `fixture` | `GET /api/doc/<source_id>`、`POST /api/explain`、`GET /api/profile`（只取画像指纹当会话缓存键；夹具模式不请求它） |

正式模式还需要材料 id：`?source=<source_id>`；没给就在页面上明说缺什么（不静默）。

例：`web/index.html?fixture=capital-v1-ch01`。

## 文件

```
index.html        外壳 + 渲染前应用显示设置的同步脚本 + 就绪标志 globalThis.__arDebug（在模块之前同步建好）
css/reader.css    全部样式（变量体系 + 三档背景 × 两档文字 × 四档字号的主题覆盖）
js/api.js         双模式取数 + 读者画像指纹（会话缓存键的第五段，见 loadProfileSignal / profileSignalOf）
js/schema.js      冻结 schema 的小工具：动作中文名、悬停文案、按空行切段
js/md.js          极简 Markdown 渲染（标题 / 引用 / 列表 / 表格 / 粗体 / 行内代码）
js/view.js        造 DOM：段落块 + 标记、抽屉原文、选片浮条、讲解卡片（正文滚动区 + 底部固定区）、横幅
js/selection.js   选区工具：规范化（与后端缓存键同口径）、选区归属（锚点所在段）、浮条定位
js/settings.js    阅读显示设置：三档背景 × 两档文字 × 四档字号 + localStorage + 正文对比度实测告警
js/cache.js       会话内讲解缓存 + 在飞去重 + 请求序号（防重复付费与迟到响应覆盖）
js/main.js        装配与交互：当前段、抽屉、卡片、选片浮条、显示设置、读完块、降级、画像指纹刷新
```

## 交互（DESIGN §5 骨架档）

- **主栏**是唯一常驻内容。改写稿按空行切段，第 N 段对应原文第 N 段（契约要求段数/段序一致），
  每段带 `data-paragraph="pNNN"` 作联动锚点。
- **原文抽屉**固定在右侧，默认只露 46px 的窄边；点窄边滑出，再点收起。展开后**跟随当前段**：
  当前段 = 最后一个「顶端越过视口 35% 阅读线」的段，抽屉里对应原文高亮并滚到中间。
- **段落动作标记**在每段左侧，悬停（或键盘聚焦）显示「动作 + 依据摘要」，全部取自 `changes.json` 的
  `action` / `why.note` / `why.profile`；`deferred: true` 的段额外标「本骨架未实现该动作」。
- **讲解卡片**：点标记弹出，内容是 `changes.json` + `concepts.json` 里已有的东西（快入口）；
  卡片里的「没懂 / 再简单点 / 再详细点」走现场生成（慢入口）。点卡片外部或按 Esc 关闭。
  **卡片不改动正文**（有测试守着）。布局三段：头部固定 → 中间内容滚动区 → **底部固定区**
  （会话缓存 / 截断轻标记 + 状态行 + 动作按钮；长答案下状态行也一直看得见，不随内容区滚走）。
- **选片讲解浮条（2026-10-08）**：在主栏改写稿或右侧原文抽屉里**选中文字**，选区旁浮出小工具条
  （段号 + 没懂 / 再简单点 / 再详细点）。点任一按钮走同一张卡片，卡片头部标明这是**选中片段**、
  所属段号、片段预览（60 字截断）。片段模式下**隐藏事实锚 chips**（那是整段口径），保留概念 chips。
  选区**归属看锚点（起点）落在哪一段**（跨段时也只按那一段讲，并写明「选区跨 N 段，仅按 pXXX 讲解」）；
  空 / 纯空白 / 1 个字符不出浮条；滚动、点空白、Esc 都收起。
  抽屉里的选区请求标 `selection_from: "source"`，主栏标 `"rewritten"`。
- **阅读显示设置（2026-10-08）**：topbar 右侧「Aa」开面板。
  背景 3 档 `paper #fbfaf7` / `warm #f6ecd9` / `dark #1b1f23`；文字 2 档 `ink #24292f` / `light #e8eaed`；
  正文字号 4 档 `15 / 16 / 17 / 19`px（**只影响正文栏**，抽屉与卡片保持界面字号）。
  实现方式是 `<body>` 上的 `data-bg` / `data-ink` / `data-size` 驱动 CSS 变量；颜色全部收进变量，
  页面里不留硬编码颜色。首访跟随 `prefers-color-scheme`；用户手动选过之后以手动为准。
  存档 `localStorage["ar.settings.v1"] = {bg, ink, size, inkExplicit}`，
  由 `index.html` 里的同步脚本在 **body 渲染前**打上属性（不闪白底），`js/settings.js` 再接管。
  **两条颜色轴（2026-10-08 复核修）**：正文栏跟 `data-ink`；「浮在内容之上的面」（顶栏 / 徽标 /
  抽屉 / 卡片 / 浮条 / 设置面板 / 段落标记）跟 `data-bg`——`--panel` / `--panel-ink` / `--panel-line`
  等成对定义，**深色档的面板也是深的、文字是浅的**，不再出现「白底浅字 / 深底深字」。
  背景 × 文字 6 种组合里，正文类文字（卡片正文与理由、chips、按钮、抽屉原文与标题、设置面板）
  实测 ≥ 4.5:1；手动组合出读不清的正文（纸白 / 暖黄 + 浅灰、深色 + 深墨）时，设置面板里显示
  一条可见告警（实测对比度 + 出路），并在 `__arDebug.inkContrast` / `inkContrastLow` 留可断言的值。
- **读完提示（2026-10-08）**：从底部浮窗改成正文末尾「— 改写稿结束 —」**之后**的流内块
  （分隔线 + 弱化文字），读完（IntersectionObserver 盯 `end-sentinel`）才显示。
  语义与调试面沿用旧口径：初始 `hidden`、`data-state`、`__arDebug.compression.shown/text` 一个不少。
- **会话内讲解缓存（2026-10-08）**：key = `sourceId|paragraph|kind|规范化后的 selection|画像指纹`
  （规范化 ＝ 去首尾空白 + 合并连续空白 + 全角空格 U+3000 转半角，与后端缓存键同口径；
  画像指纹 ＝ `GET /api/profile` 响应内容的 FNV-1a 摘要，**每次讲解前刷新一次**——后端缓存键里也有
  画像信号，少了它「会话内改画像后仍命中旧答案」。夹具模式没有后端，第五段为空串）。
  命中就直接渲染并标「本次会话已生成」；同一 key 在飞时复用同一个 Promise（在飞去重）；
  每次请求带序号，响应回来时卡片已经切到别的 key 就只写进缓存、不覆盖当前卡片。
  缓存只在内存里（刷新即失效），不落盘——避免把模型输出写进 localStorage。

## 降级纪律（DESIGN §6）

任何降级都在页面上留字，同时在 `globalThis.__arDebug` 留可断言的标志：

| 情况 | 页面 | `__arDebug` |
|---|---|---|
| doc 载不进来 | 红色横幅「材料载入失败：…」 | `fatal` |
| 有段没有改写稿 | 该段显示原文 + 「未改写」徽标 + 横幅 | `unrewritten[]`、`degradations[]` |
| 没有变更记录 | 横幅提示标记无法显示动作与依据 | `degradations[]` |
| 没有概念清单 | 横幅提示降级为「无画像」模式 | `degradations[]` |
| 讲解请求失败 | 卡片状态区红字「讲解生成失败：HTTP 500…」（不许空白） | `lastCard.state = "error"` |
| 响应 `degraded: true` / 空文本 | 卡片状态区黄字「降级显示：…」 | `lastCard.state = "degraded"` |
| 片段请求的响应没有 `scope` 字段 | 卡片状态区注明「无法确认后端是按片段讲的」（旧响应 / 夹具） | 状态行文字 |
| 片段请求但响应是 `scope: "paragraph"` | 状态行写明「后端按整段讲的（段号不存在这类请求在片段校验前就返回了）」 | 状态行文字 |
| 拿不到读者画像指纹（`GET /api/profile` 失败） | 横幅说明「本次会话的讲解缓存不随画像变化失效」，并**保留上一次的指纹**（不让键漂移） | `degradations[]`（`kind: profile-signal`） |
| 没指定材料 id | 红色横幅说明要用哪个参数 | `fatal` |

`__arDebug` 还有 `ready`（就绪标志，无头验证只等它）、`mode`、`sourceId`、`paragraphs`、
`rewrittenParagraphs`、`currentParagraph`、`drawerOpen`、`cardOpen`、`cards`、`lastCard`、
`cardCloses`、`compression`、`changes`、`concepts`、`errors`，以及 2026-10-08 新增的
`toolbarOpen`、`selection`、`settings`、`settingsPanelOpen`、`explainCache`、`explainDeduped`、
`pendingExplain`、`inkContrast`、`inkContrastLow`、`profileSignal`、
`debugSelect({paragraph, from, start, length})`（无头验证造选区用）。

## 接口契约（POST /api/explain 的 2026-10-08 追加字段）

请求体旧字段语义不变，追加两个可选字段：

```json
{"source_id": "...", "paragraph": "p005", "kind": "explain|simpler|detail",
 "selection": "选中的文本片段", "selection_from": "rewritten|source"}
```

- 有 `selection` 时后端以**片段**为讲解对象，`paragraph` 仍提供上下文；
- 成功响应可能追加 `scope: "selection"|"paragraph"`、`cached: true`、`selection_truncated: true`；
  **追加字段「为真才出现」**：`cached` / `selection_truncated` 只有为 `true` 时才有键
  （后端从不返回 `false` 版本），`scope` 每次响应都给；
- `selection_from` 是**请求**字段，**响应里没有它**（夹具 `samples/*/explain.json` 同样遵守，
  见 `tests/web/test_fixture_schema.py`）；
- 段号不存在（如 `p404` + `selection`）时后端在**段号校验**阶段就返回降级，
  `prepare_selection` 没跑，所以 `scope` 是默认的 `"paragraph"`——前端据此在状态行写明「后端按整段讲的」；
- 旧键（`text` / `degraded` / `model` / `prompt_version` / 失败的 `reason`）一个不动；
- **前端容忍这些追加字段缺失**（夹具模式与旧响应都能跑），缺 `scope` 时在状态行写明。

## 无头验证

```bash
python -m pytest tests/web -q          # 在本项目根目录跑（45 passed + 1 skipped）
```

用本机 Edge（`channel="msedge"`，退路是已缓存内核 `chromium-1084`），**不下载任何浏览器内核**。
测试自己起一个静态服务把项目根目录当站点，所以**绝大多数用例不依赖后端**；
`test_reader_features.py` 末尾两条打真服务（`http://127.0.0.1:8765`，跑之前先起 `ar serve`，
**服务没起就 skip**）：

- `test_real_service_page_and_explain_payload_without_calling_the_model`：默认就跑，
  用 `page.route` 拦下 `POST /api/explain` 只核对请求体，**不发模型调用**；
- `test_real_service_explain_end_to_end_is_billable`：默认 skip，要显式开开关
  （`$env:AR_E2E_BILLABLE = "1"`）才跑——**一次真实计费调用**（后端缓存命中时不计费）。
