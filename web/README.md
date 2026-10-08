# web/ — 阅读器前端（纯静态、零构建）

HTML + CSS + 原生 ES 模块，没有打包器、没有 npm 依赖、不联网取任何东西。
由服务端静态托管（W1 的 `uv run ar serve` 把 `/` 指向本目录），也可以直接起一个静态服务打开。

## 数据来源（双模式，PLAN-skeleton.md §4）

| 模式 | 触发 | 请求 |
|---|---|---|
| 夹具模式 | URL 带 `?fixture=<source_id>` | `../samples/<source_id>/doc.json`、`explain.json`（同源静态文件，失败时回退 `/samples/...`） |
| 正式模式 | 不带 `fixture` | `GET /api/doc/<source_id>`、`POST /api/explain` |

正式模式还需要材料 id：`?source=<source_id>`；没给就在页面上明说缺什么（不静默）。

例：`web/index.html?fixture=capital-v1-ch01`。

## 文件

```
index.html        外壳 + 就绪标志 globalThis.__arDebug（在模块之前同步建好）
css/reader.css    全部样式（主栏 / 抽屉 / 标记 / 卡片 / 压缩提示）
js/api.js         双模式取数；失败一律抛 DataError（带 url / status / tried）
js/schema.js      冻结 schema 的小工具：动作中文名、悬停文案、按空行切段
js/md.js          极简 Markdown 渲染（标题 / 引用 / 列表 / 表格 / 粗体 / 行内代码）
js/view.js        造 DOM：段落块 + 标记、抽屉原文、讲解卡片、横幅
js/main.js        装配与交互：当前段、抽屉、卡片、压缩提示、降级
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
  **卡片不改动正文**（有测试守着）。
- **压缩提示**只在读到末尾（触底）时出现，读中不显示。

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
| 没指定材料 id | 红色横幅说明要用哪个参数 | `fatal` |

`__arDebug` 还有 `ready`（就绪标志，无头验证只等它）、`mode`、`sourceId`、`paragraphs`、
`rewrittenParagraphs`、`currentParagraph`、`drawerOpen`、`cardOpen`、`cards`、`lastCard`、
`cardCloses`、`compression`、`changes`、`concepts`、`errors`。

## 无头验证

```bash
python -m pytest tests/web -q          # 在本项目根目录跑
```

用本机 Edge（`channel="msedge"`，退路是已缓存内核 `chromium-1084`），**不下载任何浏览器内核**。
测试自己起一个静态服务把项目根目录当站点，所以整条验证不依赖后端。
