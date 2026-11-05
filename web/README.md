# web/ — 前端两页（启动页 + 阅读器页，纯静态、零构建）

HTML + CSS + 原生 ES 模块，没有打包器、没有 npm 依赖、不联网取任何东西。
由服务端静态托管（W1 的 `uv run ar serve` 把 `/` 指向本目录），也可以直接起一个静态服务打开。

**2026-10-15 拆页**：`GET /` 是**启动页**（`web/index.html` + `js/launcher.js`：顶栏一颗
「测试和修改」按钮 + 主栏三块「背景测评 / 历史记录 / 输入文件」），`GET /reader` 是**阅读器页**
（`web/reader.html` + `js/main.js`，
就是拆页前那个 `index.html`，2026-10-15 只删掉了顶栏的状态入口与 `#status-host` 两块）。
带页面参数（`source` / `fixture` **这两个键存在**就走，空值也算存在；别的 query 一概不影响托管）
打开 `/` 时后端 **302** 到 `/reader?<原样 query>`（顺序与编码都不动），老链接 `/?source=<id>` 照旧能用；
`web/reader.html` 还不存在时 `/reader` 回**带原因的 404**，绝不回落到启动页。
两页共用 `css/reader.css` 的变量体系（主题选择器是 `body[data-bg|data-ink|data-size]`）与
「渲染前应用显示设置」那套口径：两页都把同一份存档打到 `<body>` 上（阅读器页由 `js/main.js` boot 里的
`bootstrapSettings(document.body)` 打；启动页由 `<head>` 那段脚本 + `<body>` 开头那句同一份结论打两次，
再由 `js/launcher.js` boot 里的 `bootstrapSettings(document.body)` 接管），
启动页另加载 `css/launcher.css`（阅读器页不加载它）。

## 数据来源（双模式，PLAN-skeleton.md §4）

| 模式 | 触发 | 请求 |
|---|---|---|
| 夹具模式 | URL 带 `?fixture=<source_id>` | `../samples/<source_id>/doc.json`、`explain.json`（同源静态文件，失败时回退 `/samples/...`）。**没有** `/api/compress` 与 `/api/events`：点「太啰嗦」会说明「夹具模式没有压缩后端」，行为事件只在本地攒着并留一句「夹具模式不记行为信号」 |
| 正式模式 | 不带 `fixture` | `GET /api/doc/<source_id>`、`POST /api/explain`、`GET /api/profile`（画像指纹 + 画像面板的当前值；夹具模式不请求它）、`POST /api/profile/rating` / `POST /api/profile/preference`（画像面板写回，2026-10-22）、`POST /api/compress`（读中点「太啰嗦」）、`POST /api/events`（行为事件，攒够 10 条或 15 秒一批） |

正式模式还需要材料 id：`?source=<source_id>`；没给就在页面上明说缺什么（不静默）。

例：`web/reader.html?fixture=capital-v1-ch01`（真服务上带 `?fixture=` 打开 `/` 会被 302 到
`/reader?fixture=capital-v1-ch01`）。

**启动页（2026-10-15）恒按正式模式取数**：历史记录的 `GET /api/sources`、输入文件的
`POST /api/ingest`、背景测评的 `GET /api/survey` 三件都是后端接口，**没有夹具版本**
（夹具那条路属于阅读器页的 `?fixture=`）。带 `?source=` / `?fixture=` 打开 `/` 反正会被 302 到
阅读器页，所以这一页根本不该出现那两个参数；真出现了也不改这里的取数口径（页面照常能用，
只是不认它们）。

## 文件

```
index.html        启动页（2026-10-15）：四块（[data-role="launcher-block"] + data-block=
                  status|survey|history|upload；status 那一块 2026-10-20 起在**顶栏里**，只留一颗
                  「测试和修改」按钮，宿主 #status-host 不在顶栏里——顶栏有 backdrop-filter，
                  会把 position: fixed 的浮层裁在顶栏那一行）+ 渲染前应用显示设置的同步脚本 + 就绪标志
                  globalThis.__arDebug（含 page 与 launcher.{sources,upload,survey}，在模块之前同步建好）；
                  2026-10-24 顶栏多一颗「通知」按钮（`data-role="notify-toggle"`，**不包进**
                  `data-role="launcher-block"`——启动页的块数被用例钉死在 4），面板宿主
                  `#notify-host` 与 `#status-host` 一样留在 body 层（顶栏有 backdrop-filter）
reader.html       阅读器页：拆页前那个 index.html，2026-10-15 只删了顶栏 `data-role="status-toggle"`
                  按钮块与 `#status-host` 宿主块，其余（主栏 / 抽屉 / 卡片层 / `#survey-host` / 调试面）都在
                  （`__arDebug` 里 `status*` 那一组声明还留着，只是阅读器页不挂 status.js、不会去取 /status.json）；
                  2026-10-20 顶栏品牌名从 `<div class="topbar__brand">` 改成 `a.topbar__brand[href="/"]`
                  （回启动页，见下面「两页之间的路」；启动页 `index.html` 那处仍是 `div`，指回自己没有意义）；
                  2026-10-24 顶栏多一颗「通知」按钮（`data-role="notify-toggle"`，在徽标与「画像」之间），
                  面板宿主 `#notify-host` 紧挨着 `.banners`（body 层，不在顶栏里）
css/reader.css    两页共用的全部样式（变量体系 + 三档背景 × 两档文字 × 四档字号的主题覆盖 + 背景问卷卡片
                  + 状态面板 + 段落标记区的「太啰嗦」+ 就地配图容器）
css/launcher.css  启动页版面（2026-10-15，**只服务 index.html**，阅读器页不加载它）：**颜色**全部走
                  reader.css 的变量（本文件不留一个硬编码颜色）；**字号是例外**，逐条写死的 rem
                  （没走 `--reading-size`，那是正文栏专用档）。只管主栏三块卡片、顶栏那颗「测试和修改」
                  入口（.lcard--bar：去掉卡片外观，只留按钮）与两个宿主不占位、不留框。
                  **主栏版面**（2026-10-20）：两列弹性网格 —— 历史记录 / 输入文件并排（输入文件在左）、
                  背景测评整行铺满且**未答时在第一行、有记录时在最后一行**（开关是 `.launcher` 上的
                  `data-survey`，由 js/launcher.js 按问卷状态打；视觉顺序靠 flex `order`，DOM 次序不变）
js/api.js         双模式取数 + 读者画像指纹（会话缓存键的第五段，见 loadProfileSignal / profileSignalOf）
                  + 读前背景问卷的四个调用（fetchSurvey / submitSurvey / requestSurveyFollowups / 夹具加载）
                  + 当场压缩 requestCompress（POST /api/compress）+ 行为事件上报 postEvents（POST /api/events）
                  + 启动页的三个（2026-10-15）：fetchSources（GET /api/sources）、postIngest（POST /api/ingest）、
                  fetchJsonExplained（非 2xx 时把后端 `error` 当消息、`hint` 放进 detail 备查）；
                  2026-10-15 起老 `fetchJson` 也统一成同一口径（非 2xx 带后端 `error`/`hint` 原文，
                  「合法 JSON `null`」不再被说成「不是合法 JSON（未知原因）」），`firstOk` 一个字没改）
js/schema.js      冻结 schema 的小工具：动作中文名、悬停文案、按空行切段、事实锚申报的读法、
                  依据画像兜底补齐的判据（`profileRefsFilled()` 只认 `changes[i].profile_refs_filled === true`）
js/md.js          极简 Markdown 渲染（标题 / 引用 / 列表 / 表格 / 粗体 / 行内代码）
js/view.js        造 DOM：段落块 + 标记 + 「太啰嗦」入口、动作标记的就地刷新（applyChangeToBlock）、
                  换正文（setBlockText）、抽屉原文、选片浮条、讲解卡片（正文滚动区 + 底部固定区）、
                  卡片里两块的**就地重画**（`renderCardDeclaration` 事实锚申报 /
                  `renderCardProfile` 依据画像 + 兜底补齐徽标，压缩后按本次结果重画）、
                  横幅（同文本去重 + `×` 手动关 + 到点自动收起——成功类 8 秒 / 降级与失败 20 秒
                  + 失效时间 `data-expires-at` + 收起进通知中心，见「降级纪律」与「通知中心」两段）
js/figures.js     **就地配图**（PLAN-actions-feedback.md §4.3）：段落内正文之后插 [data-role="figure"]、
                  SVG 走 DOMParser + 白名单、mermaid 懒加载 + render()、失败一律退回文字
js/events.js      **行为埋点**（§7.1/§7.2）：缓冲 + ≥10 条或 ≥15 s flush + visibilitychange/pagehide 强刷，
                  夹具模式不发请求（一次性可见降级）
js/selection.js   选区工具：规范化（与后端缓存键同口径）、选区归属（锚点所在段）、浮条定位
js/profile.js     读者画像面板（2026-10-22）：概念自评（知道 / 知道一点 / 不知道）+ 事实锚口径
                  （全保留 / 要结论）；只造界面，取数与写盘在 main.js，口径见下面「读者画像面板」段
js/settings.js    阅读显示设置：三档背景 × 两档文字 × 四档字号 + localStorage + 正文对比度实测告警
js/cache.js       会话内讲解缓存 + 在飞去重 + 请求序号（防重复付费与迟到响应覆盖）
js/survey.js      读前背景问卷：造表单 / 条件题联动 / 校验 / 提交与跳过 / 模型追问入口 / 版本提示；
                  2026-10-15 起多两个出口**给启动页用**——openSurveyManually（由按钮显式打开，
                  重填也走它；已答也照开，无条件）与 setSurveyLifecycleHooks（答完 / 关闭后刷新
                  启动页那张卡片；阅读器不设钩子，那边行为一个字没变）
js/status.js      「测试和修改」面板：读同源静态文件 /status.json（由 tools/gen_status.py 生成），
                  渲染①测试结论②本次修改；读不到 / 坏了就在面板里写「尚未生成」（不硬编码任何数字）。
                  2026-10-15 起入口与宿主都在**启动页**（mountStatus 原样复用，面板行为一个字没改）；
                  2026-10-20 入口从主栏那张卡搬进**顶栏**（按钮文字「测试和修改」），卡片里的说明文字
                  与备注并进了面板内部（lead），主栏不再有状态那一块
js/notify.js      通知中心（2026-10-24）：**纯内存** store（record / list / unread / markCollapsed /
                  subscribe）+ 顶栏入口与面板（mountNotify / isNotifyOpen）。横幅收起或失效时由
                  view.js 记一条（`{id, text, state, createdAt, expiresAt, collapsed, reason}`），
                  面板里可回看、点条目能把那条提示原样重弹一次；**不落 localStorage、不新增后端接口**
                  （刷新即清空是可接受的）。形态照 status.js：层 `pointer-events: none`、Esc 关闭、
                  关闭后整层移除、焦点回入口；面板 `z-index` 取 71 一档（问卷 70 之上、状态面板 72 之下）。
                  它**不 import view.js**（否则与「view.js 记条目」成环）：重弹由页面在
                  `mountNotify({replay})` 里把 `addBanner` 传进去
js/launcher.js    启动页装配（2026-10-15）：四块接线（mountStatus / 问卷卡 / 材料清单 / 上传摄取）
                  + 启动页自己的可见降级出口（横幅复用 view.js 的 addBanner：同文本去重 / 手动关 /
                  到点自动收起（8 秒 / 20 秒）/ 失效时间 / 收起进通知中心这几条显示层纪律只有那一份实现，
                  不另写第二份）；`ready` 在问卷卡与
                  材料清单两块取数都落定（含各自的降级）之后才置位（状态入口同步挂载不可能失败、
                  上传等读者交互；
                  两份里真有 Promise 被 reject 的意外也逐条走 degrade，不静默）
js/main.js        装配与交互：当前段、抽屉、卡片、选片浮条、显示设置、读完块、降级、画像指纹刷新、
                  读者画像面板（取数 / 写回 / 错误与降级，2026-10-22）、问卷挂接、配图挂接、
                  「太啰嗦」压链接线、行为埋点接线；
                  2026-10-15 起**不再挂状态入口**（删掉 mountStatus 挂载与 `dom.statusToggle` /
                  `dom.statusHost`，也不再 import `status.js`——`Esc` 里那句 `isStatusOpen()` 让路判定
                  随之删掉，那一页它恒为 false；阅读器页的 `Esc` 只让路给问卷）；
                  2026-10-24 起再把**通知中心**挂上（`mountNotify`）：`Esc` 与「点外部关闭」与画像 /
                  显示面板并列处理，`Esc` 那条放在问卷判定**之前**（一次 `Esc` 只关一层）
vendor/mermaid.min.js  **离线构建的 mermaid 12.0.0（5,575,485 B ≈ 5.3 MiB）**，从
                  `deepseek-chat/app/js/vendor/mermaid.min.js` 原样复制而来（`Copy-Item`，没有二次加工）。
                  **它不在首屏**：`reader.html` 里没有它的 `<script>`，由 js/figures.js 在
                  「真有 mermaid 图滚进视口」时注入一次（每页最多一次），详见下面的「就地配图」段
status.json       **机器生成的产物，不入库**（见 .gitignore）：`uv run python tools/gen_status.py` 写它，
                  页面只在读者点开「测试和修改」时读一次（`cache: "no-store"`，重新生成后点一下就是新数字）；
                  2026-10-20 起那个入口在启动页**顶栏**里（阅读器页没有入口，也不再请求它）
```

## 交互（DESIGN §5 骨架档）

下面这些默认说的是**阅读器页**（`web/reader.html`）；标了「启动页」的那两条（顶栏的「测试和修改」入口、
背景测评的问卷卡）在启动页上，四块的逐条说明见本节末尾的「启动页四块」，两页的其余交互互不影响。
两页共有的是顶栏那颗「通知」按钮与通知面板（2026-10-24，见「降级纪律」末尾的「通知中心」段）——
横幅收起 / 失效的条目在两边都能回看与重弹，历史各自只存内存。

- **主栏**是唯一常驻内容。改写稿按空行切段，第 N 段对应原文第 N 段（契约要求段数/段序一致），
  每段带 `data-paragraph="pNNN"` 作联动锚点。
- **原文抽屉**固定在右侧，默认只露 46px 的窄边；点窄边滑出，再点收起。展开后**跟随当前段**：
  当前段 = 最后一个「顶端越过视口 35% 阅读线」的段，抽屉里对应原文高亮并滚到中间。
- **段落动作标记**在每段左侧，悬停（或键盘聚焦）显示「动作 + 依据摘要」，全部取自 `changes.json` 的
  `action` / `why.note` / `why.profile`；`deferred: true` 的段额外标「本骨架未实现该动作」
  （2026-10-13 起 compress / illustrate 都实现了，夹具里已没有这种段；这一行显示仍在，
  由 `test_reader_ui.py::test_deferred_note_still_shows_for_a_future_unimplemented_action` 注入守着）。
  `why.profile` **为空时显示「依据画像：无自评引用」**（`data-empty="true"`），不再什么都不显示——
  空数组不等于「画像为空」（读者可能填了背景问卷，背景依据只写在 `why.note` 里），
  沉默会被读成「没有画像」（2026-10-14 复核修复，见 `schema.js` 的 `NO_PROFILE_REFS_LABEL`）。
  产物里带 `changes[i].profile_refs_filled` 的段（**这一段 `why.profile` 是骨架照读者自评兜底补齐的**）
  再挂一枚徽标「骨架兜底补齐（模型没给引用）」，悬停多一行出处说明，见下面「依据画像兜底补齐」段。
  有**事实锚申报**（`fact_declarations`）的段再加一行「事实锚申报：数字「1」（已按读者口径放行）」，
  并在标记上留 `data-declared="true|false"`（悬停就能发现「这一段丢了锚点」，见下面「事实锚申报」段）。
  多申报（锚点一个没少却申报放弃了某项）那一行写成「（多申报：本段一个都没少）」。点「太啰嗦」
  压过这一段之后，这一行按**本次压缩**的申报就地重画（`compress-v2` 起在线压缩也有申报通道）。
- **「太啰嗦」标记（2026-10-13 新增，DESIGN §5 那一行）**：同一条标记槽（`.block__gutter`）里、
  动作标记下面那个小按钮，**默认不显示**（`opacity: 0` + `visibility: hidden`），
  悬停这一段或键盘聚焦到这一段的标记上才现身。点它 → `POST /api/compress` → 当场把这一段压短：
  成功就用返回的 `text` **刷新该段正文**（走同一套 `md.js` 渲染）、动作标记改「压缩」、
  悬停依据换成本次压缩的说明，横幅写「已压缩，请重跑 `ar check`」；**降级 / 失败时正文一个字符都不动**，
  只留可见横幅（详见「降级纪律」与「接口契约」）。压过的段按钮转「已压缩」且不再可点（同一段不重复付费）。
  压过之后产物里那一段是**全新**的记录：旧申报块与「兜底补齐」徽标都随之作废、就地摘掉（见下面两段）。
- **配图就地渲染（2026-10-13 新增）**：`changes[i].figure` 的段，图容器 `[data-role="figure"]`
  插在**该段容器内、正文之后**；`figure_error` 的段在原位置留一句可读说明。见下面「就地配图」段。
- **行为埋点（2026-10-13 新增）**：停留 / 读完 / 讲解 / 选片 / 抽屉 / 压缩这些交互落 `events.jsonl`
  （`POST /api/events`），见下面「行为埋点」段。
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
  存档 `localStorage["ar.settings.v1"] = {bg, ink, size, inkExplicit}`；两页共用同一份配色与字号变量，
  从阅读器跳到启动页、或反过来，**不换底色**。这条口径 2026-10-15 复核时实测**不成立**，当天修掉：
  主题覆盖的选择器是 `body[data-bg|data-ink|data-size]`（`css/reader.css` 末尾三段），
  所以设置必须打到 `<body>` 上，两页各一个落点、同一套算法（localStorage → 归一化 → 打属性）：
  - 阅读器页：`reader.html` 的 `<head>` 同步脚本先打 `<html>`，`js/main.js` boot 第一步的
    `bootstrapSettings(document.body)` 接管（首访跟随 `prefers-color-scheme` 并落盘）；
  - 启动页（**本次修**）：`index.html` 的 `<head>` 同步脚本算出结论后留在
    `globalThis.__arApplyDisplaySettings` 上，`<body>` **最开头**那句再把它打到 body 上
    （在内容解析与首帧之前，所以「渲染前不闪白底」仍然成立），`js/launcher.js` boot 第一步同样调
    `bootstrapSettings(document.body)` 接管并补 `<html>` 那一半。
    修前只打 `<html>`（而选择器不认 `<html>`），启动页因此恒为标记里写死的纸白 16：同一份存档实测
    启动页 `rgb(251, 250, 247)` / 阅读器页 `rgb(27, 31, 35)`；空存档 + 系统深色首访同样错。
  **两条颜色轴（2026-10-08 复核修）**：正文栏跟 `data-ink`；「浮在内容之上的面」（顶栏 / 徽标 /
  抽屉 / 卡片 / 浮条 / 设置面板 / 段落标记）跟 `data-bg`——`--panel` / `--panel-ink` / `--panel-line`
  等成对定义，**深色档的面板也是深的、文字是浅的**，不再出现「白底浅字 / 深底深字」。
  背景 × 文字 6 种组合里，正文类文字（卡片正文与理由、chips、按钮、抽屉原文与标题、设置面板）
  实测 ≥ 4.5:1；手动组合出读不清的正文（纸白 / 暖黄 + 浅灰、深色 + 深墨）时，设置面板里显示
  一条可见告警（实测对比度 + 出路），并在 `__arDebug.inkContrast` / `inkContrastLow` 留可断言的值。
  **启动页那句导语（`.launcher__lead`）是例外里的一档**（2026-10-15 复核修）：它取的是跟背景成对的
  `--panel-muted`，六种组合**实测**（Edge + `getComputedStyle`，`tests/web/test_launcher.py` 的
  `test_launcher_lead_text_is_readable_in_all_six_combinations` 守着）= 纸白 5.41:1 / 暖黄 4.82:1 /
  深色 8.64:1（同一档背景配上两档文字颜色结果一样，因为这条颜色只跟 `data-bg` 走），
  **不借**跟 `data-ink` 走的 `--muted`——启动页没有设置面板可以放告警，
  「纸白背景 + 浅灰文字」这种手动组合下 `--muted` 只剩 1.84:1（暖黄 + 浅灰 1.64:1、深色 + 深墨 2.94:1）。
- **读者画像面板（2026-10-22，阅读器页顶栏「画像」）**：把此前只能命令行 / URL / 手改
  `profiles/<reader>/profile.yaml` 的两件事搬到页面上——**概念自评**（`POST /api/profile/rating`）
  与**事实锚口径**（`POST /api/profile/preference`）。入口是 `[data-role="profile-toggle"]`，
  宿主 `[data-role="profile-panel"]`；面板结构在 `js/profile.js`，取数 / 写盘 / 横幅 / 降级在 `js/main.js`。
  - **只在点开时读** `GET /api/profile`（不挡正文）；概念列表**只列当前材料** `concepts.json` 里的概念，
    每行 `[data-role="profile-concept"][data-concept][data-state]`（`data-state` ∈ `known` / `partial` /
    `unknown` / 页面自己的 `unrated`），旁边一行「知道 / 知道一点 / 不知道 / 未自评」，
    三个按钮 `[data-role="profile-rate"][data-concept][data-state]`，当前档带 `aria-pressed="true"`。
  - **事实锚口径**是两条单选 `[data-role="profile-preference"][value]`（`keep_all` / `allow_declared_drops`），
    当前值照画像里的 `pref_fact_anchor` 选中；没有这条偏好时选中严口径并注明「按默认 `keep_all` 处理」，
    取值写坏了（拼错 / 没有 `value`）**明说后端按严口径处理**（`invalid`，不静默显示成读者选过）。
    只暴露这一条偏好：后端 `PREFERENCE_VALUES` 目前也只接受它。
  - **写回**：点自评发 `{concept_id, state}`，改口径发 `{id: "pref_fact_anchor", value}`（都不带 `note`）。
    成功时用响应里的**整份画像**就地重画（不额外再读一次），横幅写「已记下自评：商品 = 知道…」/
    「事实锚口径已改为…」；**同一档重复点不发请求**（自评端点不幂等，白写会再追一条 evidence
    并换画像指纹）；画像指纹**当场重算**（`profileSignalOf`），会话讲解缓存随之换键
    ——后端 LRU 的键里也有画像指纹，下一次讲解自然不再命中旧答案。自评写的是手动结论（`overrides`），
    行为信号回流不会覆盖它；口径写回后**下一次 `ar check` / `ar rewrite`** 才按新值判（已有产物不变）。
  - **失败不装成功**：读失败 → 状态行 `data-state="error"` + 横幅，且**手上没有旧画像时不列概念**
    （列出来会像「都没自评过」；空态文案写明「画像没读到」），控件禁用；
    写失败 → `data-state="write-error"` + 横幅，**该行仍是写之前那一档**、`lastWrite` 不出现，
    控件**保持可用**（画像没变，可以重试）。
  - **夹具模式**（`?fixture=`）**一个 `/api/*` 都不请求**：状态行 `data-state="fixture"` 写明
    「没有画像后端，自评与偏好都不能保存」，自评按钮与口径单选一律 `disabled`（概念照常列出——
    那是夹具 doc 里的概念清单，不是画像）。
  - 与设置面板**互斥**（开一个就关另一个）、点外部 / `Esc` / `×` 关闭。
    可断言状态全在 `__arDebug.profilePanel`：`open` / `state` / `mode` / `url` / `concepts` /
    `preference` / `degraded` / `events` / `lastWrite` / `error` / `locked`。
    **不记行为事件**（`§7.1` 的白名单里没有「自评」这一类；这一批不扩事件种类）。
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
- **读前背景问卷（2026-10-08 新增，PLAN-background-survey.md §7/§9）**：正文渲染完成、`__arDebug.ready`
  置位**之后**才问一句服务端画像要不要弹问卷——不挡阅读，正文渲染失败时压根不弹。
  **「首次打开」只认服务端画像**（`GET /api/survey` 的 `answers` / `skipped`）：答过或跳过过就不再弹，
  而且这一档**页面上一个字都不加**——「已答过 / 已跳过，本次不再打扰…」那两条说明横幅（2026-10-15 之前
  由 `main.js` 的 `bootSurvey_()` 挂 warn 横幅）**整条去掉**，原因只落在 `__arDebug.surveyBootReason`
  （`already-answered` / `skipped-before`）上供无头验证断言；想重填走 `?survey=1`。
  **不复用 `localStorage["ar.settings.v1"]`**（那是显示设置的键，与画像无关）。
  - **由按钮显式打开（2026-10-15 拆页后新增的这一档，启动页的背景测评卡）**：启动页那张卡片先显示
    现状（`GET /api/survey`：未答 / 已答 / 已跳过 / 读不到，状态行 `[data-role="launcher-survey-state"]`
    的 `data-state ∈ unanswered|answered|skipped|loading|degraded`，初始 `idle`），点「填写问卷 / 重填问卷」
    （`[data-role="launcher-survey-open"]`）才调 `survey.js` 的 `openSurveyManually()` 打开**同一张**问卷
    （同一套题、同一个写入端点、同样的提交 / 跳过 / 追问路径）——**已答也照开**（重填本来就是它的用途），
    与阅读器 `bootSurvey` 的自动判定**互不参与**。启动页**绝不自动弹**：
    `__arDebug.surveyBootReason = "launcher-no-auto-popup"`（`surveyOpen` 恒为 `false`，
    页面上不会出现 `[data-role="survey-card"]`），2026-10-15 那位读者的意见是「别再自动打扰」，
    所以这里不另加一个自动弹窗。答完（`submitted` 钩子）与关闭（`closed` 钩子）之后卡片**立刻**
    刷新成「已答 / 已跳过」（`setSurveyLifecycleHooks`，钩子抛错只记 `__arDebug.errors`，不回流到
    问卷自己的收尾）——不靠轮询、不靠 sleep；**阅读器从不设这两个钩子**，所以那条路行为一字未变。
    打开期间按钮保持禁用（一次只开一张表单），关闭后放开；后端这次没给题目时**不打开空壳**
    （`lastOpen.reason = "no-questions"` + 一条可见降级 `survey-questions-empty`）。
    `?survey=1`（正式模式先看先填）与夹具模式（`?fixture=` 加 `?survey=1` / `?survey=fixture`）
    两条老路**行为未变**，仍只在阅读器页上生效。
  - 形态：居中卡片 + 半透明层（项目里此前没有模态先例，这是新建组件）。
    **层与卡片都不吃指针事件**，只有控件（`label` / `input` / `textarea` / `button`）收事件——
    问卷打开时正文照常可滚、可点、可选字；关闭后整层被移除，不留残影。
  - **滚轮归属（2026-10-09 补，用户实测「背景问卷的滚动会穿透」的修法）**：题区（`.survey__body`）
    是卡片里唯一的滚动容器，但它也必须是 `pointer-events: none`（给它 `auto`，卡片那块矩形就变成
    点击死区，正文在卡片范围内的点击会被吃掉）——而命中测试会**跳过** `pointer-events: none` 的元素，
    滚轮于是落到背后正文上。所以滚轮在 `document` 的**捕获阶段**按**坐标命中判定**接管
    （`survey.js` 的 `handleWheel`，非 passive）：指针在卡片矩形里就`preventDefault()` 并滚题区
    （滚到顶/底也不放给页面），指针在卡片之外（遮罩层上）一律不插手、正文照常滚。
    题区另有 `overscroll-behavior: contain` 作为滚动链的第二道闸。
  - **作答框（`textarea`）的边界语义（2026-10-09 复核补）**：固定题 Q3b / Q4b 与追问阶段的作答框
    都是 `rows = 3` 的 `<textarea>`，它自己就是滚动区，而 `preventDefault()` 取消的是**整条链的
    默认滚动动作**（会连它自己的滚动一起取消，实测 `scrollTop` 恒 0）。所以按「它在这个方向上
    还能不能滚」分流：**还能滚 ⇒ 不拦**，原生滚动归它自己（`window` 不会因此被带动，它的滚动
    祖先是题区）；**滚不动了**（已在顶 / 已在底 / 本来就没溢出）⇒ `preventDefault()` + `deltaY`
    给题区——「卡片范围内绝不带动正文」这条不变量在任何一档都成立。原生链已实测：textarea 到底
    后再滚，滚的是题区（`body.scrollTop` 247 → 639），`window.scrollY` 恒 0。
  - 不用 `<form>`（默认提交会刷新页面）；`Esc` 与「跳过」同效——
    **`Esc` 监听挂在 `document` 上、走的就是「跳过」那条路径**（`survey.js` 的 `handleEscapeKey` →
    `requestSkip('escape')`）：读者点过正文之后焦点会落到 `#rewritten`，只听卡片上的 `keydown` 会漏掉
    （2026-10-09 复核抓到的缺口，已有用例守着）。打开时焦点进卡片，关闭后焦点回正文栏
    （`#rewritten` 带 `tabindex="-1"`）。第一版不做焦点陷阱（契约不要求）。
  - 题目**只有一个来源**，前端不硬编码、也不留第二份手写题表：正式模式取 `GET /api/survey` 的
    `questions[]`，夹具模式取 `samples/<id>/survey-questions.json`（由后端 `ar.survey.QUESTIONS` 导出）。
    条件题规则优先读后端的 `requires`（权威，`not_in` 可能排除多个值），`when` / `alsoWhen` 别名只当兜底。
  - 提交：`POST /api/survey` 只发 `{answers}`（八个字段槽位，**不带 `version`** —— 后端只认槽位字段，
    多给一个键就是 400）；整场跳过发 `{skipped: true}`。提交后横幅写「背景已保存」+
    「改写产物要重跑 `ar rewrite` 才会用上背景」（前端不触发重跑）。跳过横幅写「未提供背景，按通用读者处理」。
  - **模型追问（2026-10-08 补，契约 §4）**：提交成功后**同一张卡片**换到「背景已保存 + 让模型追问
    几个细节（可选）」这一步（不是常驻按钮；夹具模式没有追问后端，这一步直接收尾）。
    点它 → `POST /api/survey/followup`（空请求体）→ 把 3–5 个问题连同后端给的 `hint` 渲染成输入框
    → 作答后「记下追问的答案」再 `POST /api/survey`（**写入画像**归这个端点）带上
    `{followups: [{q, a}]}`；`/api/survey/followup` 只负责生成问题。
    **追问答案不进八个作答槽位**（进了就 400），`at` 由后端盖章。
    降级（模型未配置 / 调用失败 / 空回复 / HTTP 失败 / 存答案失败）一律在状态区与横幅留字：
    固定题的背景不受影响，卡片不关、字不丢，可以重试或「不用了 / 完成」离开。
  - 版本：「问卷已更新」判定 = `answered_version !== null && answered_version !== version`，
    两个值都取 `GET /api/survey` 顶层，**不从浏览器本地存储读版本号**。
  - 夹具模式：默认不弹；`?survey=1` 可看可填，提交后写明「夹具模式不保存」；
    `?survey=fixture` 连响应形状一起夹具化（读 `samples/<id>/survey.json`），覆盖回填与版本提示两条分支。
- **启动页顶栏的「测试和修改」入口（2026-10-09 新增，2026-10-15 从阅读器顶栏搬到启动页，
  2026-10-20 从主栏那张卡搬进顶栏）**：顶栏里那颗「测试和修改」按钮
  （`data-role="status-toggle"`，宿主 `#status-host`，与阅读器原来的那两颗逐字同源），
  点开显示两块：**① 测试结论**（后端 `uv run pytest tests -q` 与前端 `python -m pytest tests/web -q`
  两套的 passed / failed / skipped + 各自耗时 + pytest 的原始摘要行 + 报告生成时间）、
  **② 本次修改**（`--note` 给的一句话 + 汇总「N 个文件 / +X −Y」+ 文件清单：git 状态码、名字、路径、
  增删行数；未跟踪文件标「新文件」并按实际行数计入）。
  - **面板内部自带说明**（2026-10-20）：原来主栏那张卡里的说明文字与「报告读不到时…」那句备注
    都并进了面板的 lead，主栏不再有那一块（顶栏里只留这一颗按钮）。
  - **数据源只有一个**：同源静态文件 `GET /status.json`（正式服务把 `web/` 挂在 `/` 上，与 `/api/*` 同源，
    启动页就是 `/`）。**前端一个数字都不硬编码**——报告里没有的就不显示，读不到就写「尚未生成」。
  - 每次打开都重新取一次（`cache: "no-store"`）：重新生成报告后点一下按钮就是新数字，不用刷新页面。
  - `Esc` 关闭（文档级监听：焦点可能已经不在面板上）、`×` 关闭；关闭后**整层从 DOM 移除**、
    焦点回入口（没记到就回正文栏 `#rewritten`——启动页里没有那个 id，这一档就原地不动）。
    打开时焦点进面板（卡片 `tabindex="-1"`），
    入口是原生 `button`、`:focus-visible` 有 2px 轮廓（与其它控件同一口径）。
  - **指针事件沿用问卷那套三段式**（层 `none` → 卡片 `none` → 控件 `auto`）——面板打开时正文照常可滚可点；
    于是滚动区 `.status__body` 也拿不到滚轮，归 `js/status.js` 的 `handleWheel` 按坐标命中判定接管
    （口径与 `survey.js` 完全一致：指针在卡片矩形里就 `preventDefault()` + 把 `deltaY` 给滚动区，
    滚到边界也不放给页面；指针在层上别处一律不插手），`overscroll-behavior: contain` 作第二道闸。
  - 面板与问卷**各自**在 `document` 上挂 `Esc` 监听、各自只看自己的 `state.open`。于是**两层同时开着时，
    一个 `Esc` 会把两层一起关掉**——这是拆页前就有的**已知小瑕疵**（不是本次改动引入的）；要修得给这两层
    做一个共享的「谁在最上层」判定，本次不动。2026-10-15 拆页后这两层的宿主都在启动页上：
    **阅读器页里既没有那颗按钮也没有 `#status-host`**，`status.js` 也不再被 `main.js` import
    （原先 `Esc` 里那句 `isStatusOpen()` 让路判定随之删掉——那一页它恒为 false，是死判定；
    `test_status_panel.py::test_reader_page_has_no_status_entry` 守着拆页纪律）。

### 启动页四块（`web/index.html` + `js/launcher.js`，2026-10-15；2026-10-20 状态那块搬进顶栏）

- **测试和修改（在顶栏里）**：见上面那条（面板在这里，数据源仍是 `/status.json`；主栏里没有这一块）。
- **通知（在顶栏里，2026-10-24）**：顶栏那颗「通知」按钮 + 面板（`js/notify.js`，见上面「通知中心」段）。
  它**不占块**：按钮不包进任何 `data-role="launcher-block"`，启动页的块数仍是 4。
- **背景测评**：见上面「读前背景问卷」里的「由按钮显式打开」那一条。
- **历史记录**：`GET /api/sources` 渲染材料清单，**顺序照后端给的**（mtime 倒序、同一时刻按
  `source_id` 升序）——前端**不重排**（重排会让「两次调用结果稳定」这条契约在页面上失真）。
  每行是**整块可点的 `<a href="/reader?source=<id>">`**（点哪都进阅读器，键盘也能 Tab 到、回车即进），
  内含 `[data-role="history-id" | history-paragraphs | history-mtime | history-actions |
  history-fidelity | history-missing]`：材料 id、段数、最后动过的时间（本地可读格式，见上面契约）、
  动作统计（只列非 0）、校验状态四档、缺哪份产物（三块都缺写「只摄取…」）。
  空列表写「data/ 下还没有材料，先在下面上传一个。」；404 / 非 JSON / 结构不符都写进块内状态行
  （坏条目**逐条点名**，不静默跳过）；后端 `degraded[]` 渲染成块内说明列表（`[data-role="launcher-note"]`）。
- **输入文件**：`input[type=file][accept=".md,.txt"]` + 可选 `source_id` + 「覆盖已存在的同名材料」
  勾选 + 「摄取」按钮 → `POST /api/ingest`。选完（或改完 `source_id`）按钮才可用，粗判不合格时
  状态行写原因（见「降级纪律」末尾那条）；**上传中按钮禁用**（一次只跑一个摄取请求）。
  2026-10-15 复核修的两条：
  - **连点只发一个请求**：过了粗判就**同步**置位 `state.uploadPending` + 禁用按钮，**然后**才
    `await file.text()`（`finally` 里复位）。修前占位在读文件之后，同一轮事件循环里连点 3 次会各读一份、
    各发一个请求（实测 3 个 `POST /api/ingest`）：第一次 200 已写盘，第 2/3 次撞后端 409，
    页面最后红字写「摄取失败（HTTP 409）…已经存在」——**页面在说假话**；
  - **非 UTF-8 也看得见**：`file.text()` 按 UTF-8 解码，坏字节只会变成 `U+FFFD`、**永不 reject**，
    所以读完正文要嗅一下（`encodingNoteOf()`：出现 `U+FFFD` 即算，另加「有 NUL 字节＝像是二进制」）：
    命中就**照常摄取**（后端是权威，前端不拦），但在四处留字——输入文件块状态行、结果块里一条
    `[data-role="upload-encoding-note"]`、warn 横幅、`__arDebug.degradations`（`kind: upload-encoding`），
    文案是「这份文件不是 UTF-8 文本（…）：已按替换字符摄取，正文可能乱码。」，
    `launcher.upload.encoding = "utf-8-lossy"` + `.encodingNote` 备查。
    修前一份 GBK 文件会显示成「已摄取：xxx（2 段，新建）」、一个提示都没有，而盘上 `source.md` 全是替换字符。
  成功在结果块里显示 source_id / 段数 / 是否覆盖 / 摄取时间 + 一颗「去阅读」（`/reader?source=<id>`），
  并**立刻重取历史记录**（新材料的段数与时间由后端算，前端不自己拼一条）；失败显示后端 `error`
  （409 另显示 `hint`），结果块整块收起来。
- **两页之间的路**：启动页 → 阅读器**只这一条**——历史记录与输入文件的链接都指向 `/reader?source=<id>`
  （`js/launcher.js` 的 `readerHref` 统一拼），启动页自己不渲染任何阅读界面；反向那条 2026-10-20 才补上：
  阅读器页**顶栏品牌名**是 `<a class="topbar__brand" href="/">`（悬停变 `--accent`、键盘可 Tab 到，
  样式见 `css/reader.css`；在那之前阅读器页一个出口都没有）。
- **已知小瑕疵（拆页前就有，本次未修）**：状态面板与背景问卷**各自**挂 `document` 级 `Esc` 监听、
  各自只看自己的 `state.open`，所以两层同时开着时**一个 `Esc` 会把两层一起关掉**。2026-10-15 拆页后
  这两层的宿主都在**启动页**上（阅读器页只挂问卷，`main.js` 里那句 `isStatusOpen()` 让路判定已随之删掉
  ——那一页没有状态面板，它恒为 false）；要修得给这两层做一个共享的「谁在最上层」判定。

### 就地配图（`js/figures.js`，PLAN-actions-feedback §4.3）

图来自 `changes[i].figure = {kind, source, caption?}`（**内联在变更记录里**，没有单独的 `figures.json`）。

- **图不进 `rewritten.md`**（契约 §1 第 2 条）：`illustrate` 段的正文与底本**逐字节相同**，
  图由前端在该段容器 `#blk-pNNN` 内、正文之后就地插入。保住「按空行切段 + 逐段对位」与 `text_hash` 校验。
- **绕开 `js/md.js`**：`md.js` 的 `escapeHtml()` 会把 `<svg …>` 转成源码文本，所以图块自己造 DOM——
  SVG 走 `DOMParser` 解析 + 白名单清洗后 `importNode`（**真节点**）；mermaid 走 `mermaid.render()`
  返回的标记串。正文照旧走 `md.js`，两条路互不干预。
- **mermaid 懒加载**（`vendor/mermaid.min.js`，5.3 MiB）：
  - `reader.html` 里**没有**它的 `<script>`（不进首屏、不参与首屏加载）；
  - 整篇渲染完之后由 `figures.js` 的 `armFigures()` 把 mermaid 图交给 `IntersectionObserver`，
    **图真的滚进视口**（`rootMargin: 0`）才注入 `<script>` 并调 `mermaid.render()`；
  - **每页最多注入一次**（两个 mermaid 图也只下一个脚本）；加载失败也**不重试下一次**——
    同一个失败结论复用给后面的图，避免反复去下 5.3 MiB；
  - 初始化口径：`securityLevel: 'strict'`（图里的 HTML 一律转义）、`suppressErrorRendering: true`
    （出错时不要往页面里画那张「Syntax error in text」的红图——那正是「破图」）、
    深色档（`body[data-bg="dark"]`）用 `theme: 'dark'`；缺 `structuredClone` / `Object.hasOwn` 的老内核
    **先探测再放弃**（不下这 5.3 MiB，直接退回文字）。
- **失败退回文字**（DESIGN §6）：加载失败 / `render()` 抛错 / 渲染返回空串 / 后端已给 `figure_error` /
  SVG 畸形 / 带 `<script>` 的 SVG / 不认识的 `kind`，一律在**原位置**写
  「图渲染失败，退回文字说明：<原因>」（有 `caption` 时图注照常显示，没有就把 `why.note` 一并写出来），
  并把降级交给 `main.js` 的 `degrade()`（横幅 + `__arDebug.degradations`）。**不留空白、不留破图**。
- **图渲染不产生任何模型请求**。
- 可断言：容器的 `data-state`（`pending` / `loading` / `ready` / `fallback`）、`data-kind`、
  `__arDebug.figures[]`（`{id, paragraph, kind, caption, state, reason, where, sourceChars}`）、
  `__arDebug.figureScript`（`{src, injected, state, error}`，`injected` 就是注入次数，正常是 0 或 1）。
- 「太啰嗦」压短某一段之后，那一段原来的图**不再配套**：就地摘掉，`__arDebug.figures` 里对应记录转 `removed`。

### 行为埋点（`js/events.js`，PLAN-actions-feedback §7.1 / §7.2）

只做三件事：**攒、按条件送、送不出去就说出来**。

- **攒**：`emitEvent(kind, payload)` 入缓冲，`kind` 只认契约 §7.1 的白名单
  （`dwell` / `read_complete` / `read_abandon` / `explain` / `drawer_open` / `selection` / `compress` / `skip`）；
  白名单外的 kind 记进 `__arDebug.events.rejected`，**不发**（服务端也会丢，不如在前端留痕）。
  事件体：`{v:1, id, at, kind, source_id, paragraph?, …}`，`at` 是带时区偏移的本地 ISO 时间，
  `id` 是客户端 uuid（服务端拿它幂等去重），`reader` 由服务端盖章、前端不传。
- **送**：`POST /api/events`，体是 `{events: [...]}`。触发条件：**≥ 10 条**、
  **距首条 ≥ 15 秒**、`visibilitychange`（转隐藏）、`pagehide`（这两次带 `keepalive`，
  页面正在卸载也送得出去）。同一时刻只有一个在飞请求。
- **埋点位置**（都在 `js/main.js`）：当前段切换（结算上一段的 `dwell`）、读完（`read_complete`）、
  讲解结果（`explain`，含 `cached` / `degraded`）、选片浮条出现（`selection`）、
  抽屉打开（`drawer_open`）、「太啰嗦」点击结果（`compress`，含 `ok` / `reason`）、
  `pagehide` 时没读完补一条 `read_abandon`。
- **`dwell` 的结算时机**：换段、`pagehide` / `visibilitychange`（离场）。阈值 / 定时 flush
  只是把缓冲倒出去，不会把一次停留切成几段。
- **`visibilitychange` 不算放弃**（读者会回来），只有 `pagehide` 且没读完才记 `read_abandon`。
- **卡片开关不进行为流**：§7.1 的白名单里没有这一类，前端**不臆造新 kind**；
  打开 / 关闭只落在 `__arDebug.cardOpens` / `cardCloses` 上。将来若要把「打开卡片」也当信号，
  得先往契约的白名单里加一个 kind、并让服务端认它。
- **埋点自己不触发任何讲解 / 压缩请求**：这条路只会用到 `api.js` 的 `postEvents`。
- **夹具模式不发请求**（`?fixture=` 没有这个后端）：事件照样进缓冲与 `__arDebug.events`，
  flush 时丢弃并在页面上留**一次性**降级「夹具模式不记行为信号」。
- 失败可见：请求失败 / 后端回 `ok:false` / 后端带 `degraded` 都留可见横幅（`kind: events` / `events-fixture`），
  **不许静默丢弃**；丢掉的条数记在 `__arDebug.events.dropped` / `.failed`。
- 可断言：`__arDebug.events`（`{buffered, threshold: 10, intervalMs: 15000, sent, attempted,
  dropped, failed, inflight, kinds, rejected, flushes[{reason,status,count,at}], records[], fixtureNoted,
  readComplete}`）、`__arDebug.debugEvent(kind, payload)` / `__arDebug.debugFlush(reason)`
  （无头验证入口，与 `debugSelect` 同一性质）。

### 事实锚申报（`js/schema.js` 的 `factDeclarations`，PLAN-actions-feedback §14.1）

后端松绑了事实锚（「可申报 + 可见」），前端负责**把申报摆到读者眼前**——两处、同一份数据：

- **卡片**（`fillCard`，整段模式）：`changes[i].fact_declarations` 非空时加一段
  「本段申报（事实锚）」，每条一个 chip（`[data-role="card-declaration-item"]`、
  `data-field ∈ numbers|quotes|books`，文本形如 `数字「1」`）；后面跟一枚徽标——
  - `[data-role="card-declaration-waived"]`「已按读者口径放行（要结论）」
    ⇐ 该段出现在 `changes.fidelity.waived` 里（**同一份申报，口径不同结论不同**）；
  - `[data-role="card-declaration-over"]`「本段一个锚点都没少：模型多申报了（不影响校验结论）」
    ⇐ 该段出现在 `changes.fidelity.over_declared` 里（**多申报**：锚点一个没少却申报放弃了某项。
    它既不是「已放行」（没有缺失可放行），也不是「未放行」（那句会让人以为真丢了锚点），
    所以单独一枚徽标——2026-10-14 收尾补的可见面）；
  - `[data-role="card-declaration-blocked"]`「未放行：当前口径不允许丢掉事实锚」
    ⇐ 有申报但 `waived` / `over_declared` 里都没有它（严口径 `keep_all`，或申报与实测对不上）。
  有 `note` 时再补一行「模型说明：…」（`[data-role="card-declaration-note"]`）。
- **悬停说明**（`applyChangeToBlock`）：该段动作标记的悬停里加一行
  `[data-role="tip-declared"]`「事实锚申报：数字「1」（已按读者口径放行）」，
  标记上留 `data-declared="true|false"`，那一行还有 `data-waived` / `data-over-declared`
  两个可断言属性——**不点开卡片也能发现这一段丢了锚点（或申报得离谱）**。
- **在线压缩之后就地重画**（`renderCardDeclaration`，2026-10-14 收尾）：这一段被「太啰嗦」压过之后，
  后端响应带回 `fact_declarations` / `waived` / `over_declared` / `fact_anchor_policy`，
  前端据此把卡片的申报块与悬停那一行**就地重画**，并给这一段落补一层 `fidelity` 覆盖
  （`applyCompressFidelity`：先摘掉这一段在旧 `waived` / `over_declared` 里的行，再并进本次的条目）。
  理由：产物里的 `fidelity` 每次合并后都被**重置**为未校验（`waived: []`，既有契约），
  只看产物会把刚放行的显示成「未放行」。**本次没申报就把旧的整块摘掉**（那是上一版的账）；
  重跑 `ar check` 后刷新页面，产物里的 `fidelity` 才是权威。

三条口径：

1. **键不出现 == 没有申报**：`fact_declarations` 只在模型真的放弃了某项锚点时才有（照 `figure` 的
   「为真才出现」先例），所以没申报的段这一块整段不出现，**不写「本段没有申报」这种空话**；
2. **片段模式不显示**：申报与事实锚一样是**整段口径**（`isSelection` 时两块一起隐藏，
   对片段会误导）；
3. **读不出来也要说**：结构不对（不是对象 / 只有 `note`）时渲染一枚
   `[data-role="card-declaration-broken"]`「申报读不出来（结构不对）」，**不静默当成没有申报**。

页面只**显示**申报，不判断也不改写作答：放行与否由 `ar check` 按读者画像那条偏好
（`pref_fact_anchor`）判，结论落在 `changes.fidelity` 里；前端也不提供改这项偏好的 UI
（写入走 `POST /api/profile/preference` 或手改 `profile.yaml`，见根 README §4）。
唯一的例外是**在线压缩那一次**：后端在响应里把本次的申报与放行结论一起带回来，页面就地显示
（上面第三条），不必等重跑 `ar check`。
可断言：`__arDebug.fidelity`（整份 `changes.fidelity`，含 `fact_anchor_policy` / `waived` /
`over_declared`）与 `__arDebug.compress`（本次压缩的 `factDeclarations` / `waived` /
`overDeclared` / `factAnchorPolicy`）。

### 依据画像兜底补齐（`js/schema.js` 的 `profileRefsFilled`，2026-10-14）

后端在「读者有概念自评、模型却没给 `why.profile` 引用」（漏键 / 给错形状 / 该给引用却给空数组）时，
照**读者自己的自评**兜底补齐那一段的 `why.profile`，逐段记 `meta.degraded`，并在本段产物上留一个
**结构化标记** `changes[i].profile_refs_filled: true`（**为真才出现**，照 `figure` / `deferred` 的先例；
**不进** `decisions.json`）。前端只负责把这个出处摆到读者眼前：

- **判据只有一个**：`change.profile_refs_filled === true`（`schema.js` 的 `profileRefsFilled()`，
  只认严格 `true`）。**不许拿正则去猜 `why.note` 的文案**——那是人读的散文（后端在末尾追加
  「（依据画像由骨架兜底补齐：…）」），改一个标点就失配，而且「文案像」不等于「真的是补齐的」。
- **卡片**（`fillCard`）：`[data-role="card-profile-filled"]` 徽标「骨架兜底补齐（模型没给引用）」，
  挂在「依据画像」那一行的引用 chips 后面（`.chip--filled`，提示色，与「已按读者口径放行」同一套 token）。
  引用本身照旧逐条渲染，徽标只说明**这些引用是谁给的**。
- **悬停**（`applyChangeToBlock`）：`[data-role="tip-profile-filled"]`
  「依据画像由骨架兜底补齐：模型没有给 why.profile 引用，这里照读者自评逐条列出」；
  引用那一行 `[data-role="tip-profile"]` 上另有 `data-filled="true"`，段落标记上留
  `data-profile-filled="true|false"`——**不点开卡片也能发现这一段的依据是骨架补的**。
- **不误报**：没有这个键的段（老产物 / 本来就不是补齐的 / 值不是 `true`）一处都不显示。
- **在线压缩之后这枚徽标要摘掉**（2026-10-14 修）：压缩把这一段在产物里换成**全新**的变更记录
  （`why.profile` 清空、`why.note` 换成压缩说明），原来那份 `profile_refs_filled` 已经不在产物里
  ——本地重画若沿用压前那一份，就会出现「产物已无标记、页面还挂着徽标」这种自相矛盾的显示。
  所以 `main.js` 的 `applyCompressedParagraph` 与 `deferred` / `figure` / `fact_declarations`
  同一条口径**把那个键一并删掉**；卡片正开着这一段时另调 `view.js` 的 `renderCardProfile`
  把「依据画像」那一块**原地重画**（位置不变，不许追加到卡片末尾）。
  口径边界：**引用本身照旧沿用压前那一份**（正文/依据的显示口径没变，产物里那一段的
  `why.profile` 要重跑 `ar rewrite` 才是权威），被摘掉的只有「这些引用是骨架补的」这枚徽标
  ——它说的是这一段产物的标记，而那个标记已经没了。

### 状态报告怎么生成（`web/status.json`）

```powershell
uv run python tools/gen_status.py                     # 两套测试都跑（前端那套要本机 Edge，约 65 秒）
uv run python tools/gen_status.py --skip-web          # 只跑后端那套
uv run python tools/gen_status.py --note "一句话说明"  # 可选：给这次改动写一句（显示在「本次修改」第一行）
```

工作目录＝项目根。脚本做三件事，全部如实照取、**不做判断也不清理**：

| 块 | 来源 |
|---|---|
| `tests.backend` | `uv run pytest tests -q`，取**最后一行摘要**拆出 passed / failed / skipped 与套件自报耗时（脚本自己跑在 uv 的 `.venv` 里时用同一解释器直调 `-m pytest tests -q`，见下面的「两处本机适配」） |
| `tests.web` | `python -m pytest tests/web -q`（**系统 Python，不是 uv 的 .venv**；`uv run` 会把 `.venv\Scripts` 插到 PATH 最前面，脚本逐条扫 PATH 跳过 .venv，必要时用 `AR_STATUS_PYTHON` 指定。**不跑 `playwright install`**） |
| `changes` | `git status --porcelain -uall`（状态 + 路径；`-uall` 把未跟踪目录展开成文件）+ `git diff --numstat`（逐文件增删行数，与 `git diff --stat` 同源同数）+ `git log -1 --oneline` 当锚点；未跟踪文件按实际行数计入 `insertions` 并标 `untracked` |

**两处本机适配（不是怪癖，是受限环境实测撞出来的；原因也写在 `tools/gen_status.py` 的注释里）**：

1. **子进程的临时目录指到项目内的 `.status-tmp/`**（跑完即删，已进 `.gitignore`）：
   本机 DSH 沙箱下 `uv run` 派生的子孙进程写不了系统临时目录（实测 `PermissionError(13)`），
   而 playwright 启动浏览器要在临时目录里 `mkdtemp('playwright-artifacts-')`，那一步 EPERM 会让
   **整个前端套件变成 88 errors**（不是用例坏了，是浏览器起不来）。指到项目内就照常跑。
2. **后端那套不在 uv 里再套一层 uv**：嵌套 `uv run` 会在 uv 缓存目录上撞权限
   （`Failed to initialize cache at …\uv\cache` + `os error 5 拒绝访问`），
   那一刻摘到的「最后一行摘要」是 uv 的报错、不是 pytest 的结论——第一次真跑就是这么写出
   `0 passed` 的假数字的。脚本用 `.venv` 的同一个解释器直调 `-m pytest tests -q`（同一套用例、
   同一个环境），产物里 `command` 照旧写 `uv run pytest tests -q`，实际命令记在 `via` 字段备查。
   脚本**不在** `.venv` 里时（比如用系统 Python 直接跑）退回 `uv run pytest tests -q`。

产物口径：UTF-8 无 BOM、LF、缩进 2；`generated_at` 是本地时区的 ISO 时间。
它**机器生成、不入库**（`.gitignore` 里有一条 `web/status.json`），
所以「谁都能重跑一遍拿到同样的口径」。退出码 0 ＝ 报告写成功——**测试红不红由产物里的数字表达**
（页面把 `failed > 0` / `ok: false` 的那一套染成醒目色），写不出去才非 0。

字段形状（页面只读这几项；多出来的键它不认识也不会坏）：

```jsonc
// 下面是 2026-10-09 10:31 那次真实生成的摘录（数字随当次跑动，示例只为看形状）
{"generated_at": "2026-10-09T10:31:23+08:00",
 "tests": {"backend": {"command": "uv run pytest tests -q", "ran": true, "ok": true, "exit_code": 0,
                       "passed": 248, "failed": 0, "skipped": 0, "errors": 0, "seconds": 3.2,
                       "summary": "248 passed, 1 warning in 3.21s",
                       "via": "…\\.venv\\Scripts\\python.exe -m pytest tests -q"},
           "web":     {"command": "python -m pytest tests/web -q", "ran": true, "ok": true, "exit_code": 0,
                       "passed": 98, "failed": 0, "skipped": 1, "errors": 0, "seconds": 59.2,
                       "summary": "98 passed, 1 skipped in 59.17s",
                       "interpreter": "…\\Python312\\python.exe"}},
 "changes": {"head": "94e9917 perf: prompt 前缀重排 + 首批预热 + 缓存命中可视",
             "note": "一句话说明（--note 给的，可缺）",
             "files": [{"status": "M", "path": "web/js/view.js", "insertions": 107, "deletions": 3},
                       {"status": "??", "path": "tools/gen_status.py", "insertions": 371, "deletions": 0,
                        "untracked": true}],
             "totals": {"files": 33, "insertions": 10910, "deletions": 85}}}
```

`--skip-web` 时 `tests.web` 写成 `{"command": …, "ran": false, "reason": "--skip-web"}`——
页面显示「本次没跑」，**不拿假数字冒充**（那会连「这套没跑」都看不出来）。

## 降级纪律（DESIGN §6）

任何降级都在页面上留字，同时在 `globalThis.__arDebug` 留可断言的标志。
横幅到点会自己收起，**但收起的条目进顶栏「通知」历史**（未读计数 + 可回看 + 点条目原样重弹）——
所以「到点收起」不等于「把降级藏起来」：留字这件事由「横幅 + 通知中心」两处共同兜住，
这也是下面「横幅纪律」给降级 / 失败也配了失效时长的依据。

| 情况 | 页面 | `__arDebug` |
|---|---|---|
| doc 载不进来 | 红色横幅「材料载入失败：…」，**带上后端 `error`（与 `hint`）原文**（`HTTP 404：找不到材料 x… —— 先运行 …`；响应不是 JSON 时退回 `HTTP <status>`） | `fatal` |
| 有段没有改写稿 | 该段显示原文 + 「未改写」徽标 + 横幅 | `unrewritten[]`、`degradations[]` |
| 没有变更记录 | 横幅提示标记无法显示动作与依据 | `degradations[]` |
| 没有概念清单 | 横幅提示降级为「无画像」模式 | `degradations[]` |
| 讲解请求失败 | 卡片状态区红字「讲解生成失败：HTTP 500…」（不许空白） | `lastCard.state = "error"` |
| 响应 `degraded: true` / 空文本 | 卡片状态区黄字「降级显示：…」 | `lastCard.state = "degraded"` |
| 片段请求的响应没有 `scope` 字段 | 卡片状态区注明「无法确认后端是按片段讲的」（旧响应 / 夹具） | 状态行文字 |
| 片段请求但响应是 `scope: "paragraph"` | 状态行写明「后端按整段讲的（段号不存在这类请求在片段校验前就返回了）」 | 状态行文字 |
| 拿不到读者画像指纹（`GET /api/profile` 失败） | 横幅说明「本次会话的讲解缓存不随画像变化失效」，并**保留上一次的指纹**（不让键漂移） | `degradations[]`（`kind: profile-signal`） |
| 没指定材料 id | 红色横幅说明要用哪个参数 | `fatal` |
| 拿不到问卷题目（`GET /api/survey` 失败 / 夹具题目缺失） | 横幅说明「本次不弹问卷，阅读照常、背景按未提供处理」 | `surveyBootReason = "load-failed"`、`degradations[]`（`kind: survey-load`） |
| 后端回了空 `questions[]` | 横幅写明「本次用的是夹具题目，内容可能与后端版本不一致」 | `surveyQuestionSource = "api-fallback-fixture"`、`degradations[]`（`kind: survey-questions-fallback`） |
| 问卷版本不一致（`answered_version !== version`） | 横幅「问卷已更新：你上次作答时是 v1，当前是 v2…」 | `degradations[]`（`kind: survey-version`） |
| 提交失败（400 / 网络） | 状态区红字「背景没能保存：…」，**表单留在原地、答案不丢** | `surveyEvents[]`（`submit-error`）、`degradations[]`（`kind: survey-error`） |
| 跳过失败（没落盘） | 状态区红字「跳过没有保存成功：…可以直接再点一次重试；也可以关掉问卷直接阅读——服务端没有记下这次跳过，所以下次打开材料时它还会再问你一次」，**表单不关** | `surveyEvents[]`（`skip-error`）、`degradations[]`（`kind: survey-error`） |
| 追问降级（模型未配置 / 调用失败 / 空回复） | 状态区黄字「这次没能生成追问：…」，横幅同名；固定题背景不受影响，可「完成」离开 | `surveyEvents[]`（`followup-done`，`degraded: true`）、`surveyFollowup`、`degradations[]`（`kind: survey-followup`） |
| 追问请求 HTTP 失败 | 状态区红字「追问请求失败：…」，卡片不关、可重试 | `surveyEvents[]`（`followup-error`） |
| 追问答案保存失败 | 状态区红字「追问的答案没能保存：…」，**卡片不关、输入框里的字还在** | `surveyEvents[]`（`followups-save-error`） |
| 夹具模式 · 追问这一步 | 夹具模式提交后直接收尾（没有追问后端），横幅仍写「夹具模式不保存」 | `surveyEvents[]`（`followups-unavailable`） |
| 跳过（整场） | 横幅「问卷已跳过并记下（skipped=true）。未提供背景，按通用读者处理…」 | `surveyEvents[]`（`skip-done`）、`surveyOpen = false` |
| 提交时前端校验不过（六选三不是 3 项） | 卡片内状态区黄字「还不能保存：…」，不发请求 | `surveyEvents[]`（`invalid`） |
| 夹具模式提交 | 横幅「夹具模式不保存背景：这份表单只是用来看问卷界面（答案不落盘）」 | `surveyEvents[]`（`submit-done`，`kind: "fixture"`） |
| 状态报告取不到（`/status.json` 404 / 网络失败） | 面板里黄框「状态报告尚未生成」+ 生成命令 +「这次没读到它：HTTP 404 …」，**不留空白、不假装成功** | `statusState = "degraded"`、`statusPayload = null`、`statusError`、`statusDegraded[]`、`statusEvents[]`（`degrade`） |
| 状态报告是坏 JSON / 少 `tests`、`changes` 两块 | 同上（原因写成「报告内容不认识」/解析错误的原文） | 同上 |
| 状态报告里某套没跑（`--skip-web` / 找不到系统 Python） | 那一行写「本次没跑：--skip-web」，**不拿假数字冒充** | 结论行 `data-state="skip"`；跑没跑看 `statusPayload.tests.<套>.ran` |
| 状态报告里某套 `failed > 0` / `ok: false` | 那套的结论行用醒目色（`--err-*`：红边 + 红底 + 红字），与「全绿」一眼分得开 | 结论行 `data-state="error"`；卡片 `data-state="error"` |
| 状态报告里没有 `note` | 「本次修改」不渲染那一行（不是写空行） | — |
| **配图渲染失败**（mermaid 加载失败 / `render()` 抛错 / 空结果 / SVG 畸形或含脚本 / 后端给了 `figure_error`） | 图容器原位一句「图渲染失败，退回文字说明：…」（+ 图注或 `why.note`），整块转警示色；横幅写「配图渲染失败（pNNN）：…」 | `figures[].state = "fallback"`、`figures[].reason`、`degradations[]`（`kind: figure`） |
| **「太啰嗦」压缩降级**（`ok:false`：事实锚对不上 / 模型没配上 / 无产物…） | 横幅「压缩降级（pNNN）：这一段没能压缩（reason）…」，**正文一个字符都不动**，标记可重试 | `compress.state = "degraded"`、`compress.ok = false`、`compress.reason`、`degradations[]`（`kind: compress-degraded`） |
| **「太啰嗦」成功但响应 `degraded` 非空**（三种成因：那次点击的行为事件没记上 / 申报读不通 / 申报后放行或多申报的说明） | 成功横幅「已压缩，请重跑 `ar check`」照旧，**另外**加一条 warn 横幅把后端那句说明原样带出来；**正文照常刷新**（降级不拦住压缩结果） | `compress.state = "ok"`、`compress.ok = true`、`compress.degraded[]`、`degradations[]`（`kind: compress-degraded-notes`） |
| **「太啰嗦」请求失败**（HTTP / 网络） | 同行文案写「压缩请求失败（pNNN）：HTTP 500…」，正文不动，可重试 | `compress.state = "error"`、`compress.reason = "request_failed"`、`degradations[]`（`kind: compress-error`） |
| **夹具模式点「太啰嗦」** | 横幅「夹具模式没有压缩后端…」，正文不动 | `compress.state = "fixture"`、`degradations[]`（`kind: compress-fixture`） |
| **事实锚申报读不出来**（`fact_declarations` 不是对象 / 数组里混进非字符串 / 只有 `note`） | 卡片里那一条写成「申报读不出来（结构不对）」，**不假装没有申报**（后端也把同一件事记进 `meta.degraded`；整条不是对象那一档由工具层给出说明，2026-10-14 复核修复） | `card-declaration-broken` chip |
| **`why.profile` 为空**（这一段没有自评引用） | 悬停那一行与卡片的「依据画像」段都写「依据画像：无自评引用」，**不再什么都不显示**——空数组不等于「画像为空」（读者可能填了背景问卷，背景依据只写在 `why.note` 里），沉默会被读成「没有画像」 | `tip-profile[data-empty="true"]`、`card-profile-empty` |
| **依据画像兜底补齐**（这一段 `why.profile` 是骨架照读者自评补的，产物里有 `changes[i].profile_refs_filled`） | 卡片「依据画像」那一行加一枚徽标「骨架兜底补齐（模型没给引用）」，悬停多一行出处说明；引用 chips 照旧逐条渲染（徽标说的是出处，不是引用本身）。**在线压缩后徽标就地摘掉**（产物里那一段换成全新记录、已经没有这个键），引用照旧 | `card-profile-filled` chip、`tip-profile-filled`、`tip-profile[data-filled="true"]`、`marker[data-profile-filled="true"]` |
| **多申报**（锚点一个没少却申报放弃了某项；`fidelity.over_declared` 里有这一段） | 卡片改挂「本段一个锚点都没少：模型多申报了（不影响校验结论）」，悬停那一行写「（多申报：本段一个都没少）」——**不是「未放行」**（没有缺失可放行，退出码照旧 0） | `card-declaration-over` chip、`tip-declared[data-over-declared="true"]` |
| **行为事件送不出去 / 后端没写成功** | 横幅「行为信号没能送出去 / 没能记下（N 条）…」，**阅读照常**、缓冲继续攒 | `events.failed`、`events.flushes[].status = "error" \| "failed"`、`degradations[]`（`kind: events`） |
| **夹具模式的行为事件**（`?fixture=` 没有 `/api/events`） | **一次性**横幅「夹具模式不记行为信号：…」（再来几批也只留这一条），事件照样进 `__arDebug` | `events.fixtureNoted = true`、`events.dropped`、`degradations[]`（`kind: events-fixture`） |
| **启动页 · 材料清单读不到**（`GET /api/sources` 网络 / HTTP / 非 JSON 失败） | 历史记录块内状态行「材料清单没能读到：…」（`data-state="degraded"`）+ 横幅「读不到材料清单…历史记录这一块空着，上传不受影响」；响应是**合法 JSON `null`** 时按实说「响应是 JSON \`null\`：结构不认识」（2026-10-15 修：原来把这句写成「响应不是合法 JSON（未知原因）」，是假话） | `launcher.sources.state = "degraded"`、`.error`、`.url`、`degradations[]`（`kind: sources-load`） |
| **启动页 · 材料清单结构不符**（响应不是 JSON 对象 / `sources` 不是数组 / 某条没有可用的 `source_id`） | 历史记录块内状态行写明结构不认识，坏条目**逐条点名**「清单第 N 条没有可用的 source_id（结构不符），已跳过」+ 横幅 | `launcher.sources.state = "degraded"`、`.error`、**`.url`（＝实际打到的 `/api/sources`，2026-10-15 修：原来这两条留在 `null`）**、`.degraded[]`、`degradations[]`（`kind: sources-shape`） |
| **启动页 · 清单是空的但后端带说明**（如 `data/` 目录不存在） | 历史记录块内状态行「data/ 下还没有材料，先在下面上传一个。」+ 块内说明列表 + 横幅「材料清单：…」 | `launcher.sources.state = "empty"`、`.count = 0`、`.degraded[]`、`degradations[]`（`kind: sources-degraded`） |
| **启动页 · 上传读不出文件内容**（`file.text()` 抛错） | 输入文件块内状态行红字「读不出这个文件的内容（…）：换一份 UTF-8 文本再试。」+ 横幅 | `launcher.upload.state = "error"`、`.error`、`degradations[]`（`kind: upload-read`） |
| **启动页 · 摄取**的是**非 UTF-8 文本**（正文里有 `U+FFFD` 替换字符，或有 NUL 字节像是二进制） | 照常摄取（后端是权威，前端不拦），但四处留字：输入文件块状态行追加「这份文件不是 UTF-8 文本（…）：已按替换字符摄取，正文可能乱码。」、结果块里一条 `[data-role="upload-encoding-note"]`、warn 横幅、`degradations[]` | `launcher.upload.state = "ok"`（摄取确实成功了）、`.encoding = "utf-8-lossy"`、`.encodingNote`、`degradations[]`（`kind: upload-encoding`） |
| **启动页 · 摄取失败**（400 / 409 / 500 或网络失败） | 输入文件块内状态行「摄取失败（HTTP 409）：后端 `error` 原文 —— 后端 `hint`」，结果块整块收起来 + 横幅 | `launcher.upload.state = "error"`、`.error`（后端 `error`）、`.hint`（409 的出路说明，没有就是 `null`）、`.httpStatus`、`degradations[]`（`kind: upload-error`） |
| **启动页 · 问卷状态读不到**（`GET /api/survey` 失败 / 结构不认识） | 背景测评块内状态行「问卷状态读不到：…」（`data-state="degraded"`），**按钮照旧可点**（读者能再试）+ 横幅 | `launcher.survey.state = "degraded"`、`.error`、`degradations[]`（`kind: survey-status`） |
| **启动页 · 点「填写问卷」但后端没给题目**（`questions[]` 为空） | 横幅「问卷打不开（页面上不会出现一张空表单）…可以稍后再点一次，或直接在阅读器里用 `?survey=1` 打开」，**不开空壳** | `launcher.survey.lastOpen = {opened:false, reason:"no-questions"}`、`degradations[]`（`kind: survey-questions-empty`） |
| **启动页 · 某块连初始化都炸了**（取数 Promise 被 reject 这类意外） | 横幅「启动页第 N 块没能初始化（…）：那一块会显示成失败状态」 | 那一组停在失败态、`degradations[]`（`kind: launcher-boot`） |

启动页那几条的落点与阅读器同一套：`launcher.js` 的 `degrade(kind, message)` 一边 push 进
`__arDebug.degradations`、一边按 `bannerState(kind)` 挂横幅（口径与 `main.js` 逐字一致）。
**前端粗判不算降级**：没选文件 / 扩展名不在白名单（只收 `.md` / `.txt`，大小写不敏感）/
`source_id` 形状不合法（`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` 且不含 `..`）/ 文件是空的
只在输入文件块内写状态行（`data-state="invalid"` / `"ready"`），**不发请求也不打横幅**——权威判定在后端。
这一档 2026-10-15 复核修了两处：①`invalid` 时 `launcher.upload.error` 要写**原因**（原来恒为 `null`），
并且 `sourceId / paragraphs / overwritten / httpStatus / url / hint / encoding / encodingNote`
一律清成 `null`（原来上一轮的 `httpStatus: 200`、`paragraphs: 3` 会留在调试面上——调试面在说假话）；
②注释里的「判据与后端逐条对齐」是假话，已改成如实描述：前端**只做后端也做的那两条**
（扩展名白名单、`source_id` 形状），**不判**后端的正文字符上限（`ar.server.MAX_UPLOAD_CHARS = 2_000_000`
字符；字符数只有读完文件才知道，而 `File.size` 是字节数，拿它去比字符上限会误挡合法的中文长文），
扩展名判据也不完全等价（文件名恰好叫 `.md` 时前端放行、后端按「没有扩展名」回 400）——
这两种都由后端 400 兜底，页面上照样看得见。

### 横幅的显示层纪律（2026-10-09）

上表里的「横幅」是同一条显示通道：容器 `[data-role="banners"]`，渲染在 `js/view.js` 的 `addBanner`，
`data-state` 的映射在 `js/main.js` 的 `bannerState`（`error`/`fatal` → 红，`info`/`ok` → 绿，
其余降级与说明 → 黄）。缺陷现场是「提交背景问卷后横幅不消失、同一句话还叠三条」，
所以这一层补了四条规矩（2026-10-24 又补两条：失效时间、收起进通知中心）——
**只动显示层**，上表的 `__arDebug` 标志与语义一个字没改：

| 规矩 | 判据 | 实现 |
|---|---|---|
| 同文本只留一条 | 容器里已有同文本横幅时不再新增节点，只把 `data-repeats` 加一，并在横幅里写「（同一提示已出现 N 次）」；关掉之后再出现算新的一条 | `addBanner` 按 `data-text` 比对 |
| 每条能手动关 | 每条带 `×`（`[data-role="banner-close"]`）：原生 `button`，鼠标点与键盘 `Enter`/`Space` 等价，`:focus-visible` 有 2px 轮廓；关掉一条不动别的横幅，`__arDebug` 一个字节都不变 | `addBanner` 的关闭按钮 + `removeBanner(el, 'close')` |
| 成功类 8 秒收起 | `data-state="info"`（**做成了**，如「背景已保存」）在 `BANNER_TTL_MS.info = 8000`（8 秒）后自己走 | `armBannerClock` 的 `setTimeout` |
| 降级 / 失败 20 秒收起（2026-10-24 改） | `data-state="warn"` / `"error"`（模型未配置 / 保存失败 / 提交失败 / 段数对不上…）在 `BANNER_TTL_MS.warn = 20000`（20 秒）后自己走，**收起的那一刻条目进顶栏「通知」历史**（未读 +1、可回看、点条目原样重弹）。**依据**：早先它们取 `0`/永不消失，是因为「自己消失等于把降级藏起来」；现在收起的条目有地方可寻（通知中心兜住可见性），那个前提不成立了。20 秒比成功类长：降级文案通常要写完原因和下一步怎么办 | 同一套 `armBannerClock`（`ttlOf` 读 `BANNER_TTL_MS`） |
| 失效时间可见（2026-10-24） | 每条横幅带 `span.banner__expiry`（`[data-role="banner-expiry"]`，**不可聚焦**：横幅里不许因此多一个 Tab 停点，插在 text 之后、`×` 之前）：**三档都写「还剩 N 秒」**并由 JS 每秒刷新；`data-expires-at` 是失效时刻（epoch ms），悬停的 `title` 是绝对本地时刻（带时区偏移，横幅本体上也有同一句）。`0` = 永不 仍是合法取值（`paintExpiry` 会写「不会自动收起」），只是当前四档都不取它。**双通道**是为断言：秒数会抖动，用例按 `data-expires-at` 判 | `view.js` 的 `armBannerClock` / `paintExpiry` |
| 收起进通知中心（2026-10-24） | 到点消失或被 `×` 关掉时，条目「收起」进顶栏「通知」面板的历史（`reason` 分 `timeout` / `close`）：可回看，点条目能把那条提示原样重弹一次；**纯内存**，刷新即清空，不落 localStorage、不新增后端接口 | `removeBanner` → `js/notify.js` 的 `markCollapsed`；面板见下面「通知中心」段 |

两个时长的理由：成功提示没有待办，中文一眼扫完一行约 2–3 秒，8 秒给了「正在看别处」的读者
两倍余量；短于 5 秒来不及看清，长于 10 秒就变成新的视觉噪音。降级 / 失败给 20 秒——它们的话要说完
「出了什么事 + 下一步怎么办」，读得慢；而且「消失」不再是「没了」（条目在通知中心里），所以可以比
成功类跑得久一点，却不必无限期占着屏幕。改时长只改 `web/js/view.js` 的 `BANNER_TTL_MS`
（`BANNER_AUTO_DISMISS_MS` 是 `BANNER_TTL_MS.info` 的别名，老名字保留；无头用例
`tests/web/test_banner_lifecycle.py` 的 `AUTO_DISMISS_MS` / `DEGRADE_TTL_MS` 要跟着改）。

**不许用 CSS 动画 / 过渡来「拖时间」**（2026-10-24 明确）：到点必须由 JS 的 `setTimeout` 真的把
节点从 DOM 摘掉，`tests/web/test_banner_lifecycle.py` 按 `[data-role="banner"]` 的计数断言到点归零
（成功类 8 秒、降级 / 失败 20 秒各一条用例）。

#### 通知中心（`js/notify.js`，2026-10-24）

横幅会自动消失、也能被读者关掉，关掉之后那句话就彻底没了——所以补一个**纯内存**的通知中心：

- **入口**：两个页面的**顶栏**各一颗「通知」按钮（`[data-role="notify-toggle"]`，外观复用
  `.settings-toggle`）。它**不叫** `status-toggle`（`test_status_panel.py` 断言阅读器页那个 role
  的计数为 0），启动页那颗也**不包进** `data-role="launcher-block"`（`test_launcher.py` 把启动页的
  块数钉死在 4）；
- **未读徽标**：`span.notify__count[data-role="notify-count"]`，未读数 = **已收起且还没在面板里看过**
  的条数（还挂在页面上的横幅不算未读——它就在读者眼前）。`data-unread="0"` 时由 CSS
  （`.notify__count[data-unread="0"]{display:none}`）隐藏，JS 只负责写数；打开面板即清零；
- **面板**：层 `[data-role="notify-layer"]` + 卡片 `[data-role="notify-panel"]`（`role="dialog"`、
  `aria-label`、`tabIndex=-1`），内容 = 头（通知 / `×`）+ 列表 `[data-role="notify-list"]` +
  空态 `[data-role="notify-empty"]`（「还没有通知。收起的提示会留在这里。」）+ 脚
  `[data-role="notify-foot"]`（「已收起 N 条」，一条都没有时整行不占位）；
- **宿主在 body 层**：`#notify-host`（`[data-role="notify-host"]`）紧挨着 `.banners`，**不放顶栏里**
  ——顶栏有 `backdrop-filter` 与 `z-index: 40` 的层叠上下文，`position: fixed` 的后代会被裁在顶栏
  那一行，还会被抽屉（45）与卡片层（60）压住。面板 `z-index` 取 **71** 一档（问卷 70 之上、
  状态面板 72 之下）；
- **指针事件**：层 `pointer-events: none`（正文照常可点可滚），只有卡片自己收事件；点卡片之外由
  页面关掉它（阅读器页在 `main.js` 的 document click 里，启动页在 `launcher.js` 的 `wireNotify`，
  两处都用 `panelContains()` 判内外）；
- **键盘 / 无障碍**：入口带 `aria-expanded` / `aria-controls`；`Esc` 关闭（问卷开着时让路给问卷，
  一次 `Esc` 只关一层）；关闭后焦点回入口；历史条目本身是原生 `button`（Tab + Enter 即重弹）；
- **重弹**：点条目 → 页面用 `addBanner(容器, item.text, item.state, { record: false })` 把那条原样弹
  回来（`record: false` 是为了不在历史里翻倍），然后面板自己让开（读者要看的是刚弹回来的横幅）；
- **纯内存**：不落 localStorage、不新增后端接口，刷新即清空（这是**预期**，见页面上那句空态文案）。
  `__arDebug.notifications` = `{open, unread, items[]}`，每项
  `{id, text, state, createdAt, expiresAt, collapsed, collapsedAt, reason, seen}`，无头验证只断言它。
- **重弹那条横幅不记账**：`record: false` 的横幅**不进 store**，所以它被 `×` 关掉时
  `removeBanner` 会拿一个未知 id 去 `markCollapsed`——那条路径是安全的 no-op（返回 `null`，
  不抛错、不在历史里凭空多一条、不动未读），`test_notify_center.py` 有一条专门断言它；
- **窄屏（≤430px，2026-10-24 修回归）**：新按钮刚加上时 390px 下顶栏内容比可用宽度多 34px，
  「Aa 显示」被挤进右侧抽屉窄边；同一宽度下徽标还会折成两三行（行高是写死的 48px）。
  修法**只在 CSS**（`css/reader.css` 的 `@media (max-width: 430px)`）：品牌名与徽标允许省略号
  （先截材料 id——读者刚点进来，最没信息量），「第 N / M 段」段号做 **sr-only 视觉隐藏**
  （`position: absolute` + `clip-path`，文字仍在无障碍树里，读屏照样读得到进度；**不**改成
  `aria-label`，免得盖掉随时在变的动态文字），三颗按钮照旧 `flex: 0 0 auto` 不参与压缩。
  ≤880px 那颗「通知」按钮只留图标（与「画像 / 显示」同一档）。实测 1400 / 1024 / 800 / 430 / 390 / 360
  六个宽度：`.topbar__side` `scrollWidth <= clientWidth`（无横向溢出）、顶栏仍 48px、
  三颗按钮都点得到（复跑脚本 `data/_verify/notify/narrow.py`，用例
  `test_notify_center.py::test_topbar_fits_in_one_line_at_narrow_widths` 守着 430 / 390 / 360）。

`__arDebug` 还有 `ready`（就绪标志，无头验证只等它）、`mode`、`sourceId`、`paragraphs`、
`rewrittenParagraphs`、`currentParagraph`、`drawerOpen`、`cardOpen`、`cards`、`lastCard`、
`cardCloses`、`compression`、`changes`、`concepts`、`fidelity`（2026-10-14 新增：`changes.fidelity`
  原样，含 `fact_anchor_policy` / `waived`）、`errors`，以及 2026-10-08 新增的
`toolbarOpen`、`selection`、`settings`、`settingsPanelOpen`、`explainCache`、`explainDeduped`、
`pendingExplain`、`inkContrast`、`inkContrastLow`、`profileSignal`、
`debugSelect({paragraph, from, start, length})`（无头验证造选区用）。

2026-10-13 新增的第三组（配图 / 压缩 / 行为埋点）：

- `figures[]`：本次渲染的图，每项 `{id, paragraph, kind, caption, state, reason, where, sourceChars}`；
  `state ∈ pending | loading | ready | fallback | removed`；
- `figureScript`：`{src, injected, state, error}`——`injected` 是**真实注入 `<script>` 的次数**
  （懒加载「只注入一次」看它，正常是 0 或 1），`state ∈ idle | loading | ready | failed`；
- `compress`：最近一次「太啰嗦」的结果 `{paragraph, state, ok, reason, text, url, hint, cached, model,
  artifactsUpdated, degraded?, message?}`，`state ∈ request | ok | degraded | error | fixture`；
  **成功路径也可能带 `degraded`**（那条行为事件没记上时后端会回一句可读说明），
  那时 `state` 仍是 `ok`、正文照常刷新，只是多一条 warn 横幅（见「降级纪律」表）；
- `compressEvents[]`：`compress-request` / `compress-done` 流水；
- `cardOpens[]`：卡片打开流水（**不进行为流**，见上）；
- `events`：行为埋点快照（字段见上面「行为埋点」段）；
- `eventsFlushIntervalMs`：15000（与 `events.intervalMs` 同值，方便断言）；
- `debugEvent(kind, payload)` / `debugFlush(reason)`：无头验证入口（与 `debugSelect` 同一性质，
  不是业务逻辑——页面自己的埋点走的是同一个 `emitEvent` / `flushEvents`）。

问卷那一组（同样是 2026-10-08 新增，供无头用例等待与断言，不靠 sleep）：
`survey`（本次打开的元信息：`openReason` / `version` / `questions[]` / `visible`）、
`surveyOpen`、`surveyState`（`closed|open|submitting`）、`surveyPhase`
（`questions` → `done` → `followup`，同一次问卷流程里的三个阶段）、`surveyVisible`（每题显隐快照）、
`surveySeen[]`、`surveyAnswers`、`surveyAnswerCount`、`surveyDegraded[]`、
`surveyEvents[]`（`open` / `change` / `invalid` / `submit-request` / `submit-done` /
`followups-offered` / `followups-unavailable` / `followup-request` / `followup-done` /
`followup-error` / `followups-save-request` / `followups-done` / `followups-save-error` /
`followups-skipped` / `skip-request` / `skip-done` / `skip-error` / `close`）、
`surveyBootReason`（`fixture-mode` / `fixture-explicit` / `api-first-visit` / `api-explicit` /
`already-answered` / `skipped-before` / `load-failed`；后两个**只在这里**，页面上不弹横幅）、
`surveyQuestionSource`
（`api` / `fixture` / `fixture-response` / `api-fallback-fixture`）、
`surveyQuestionsRaw[]`（题目的 `{qid, field}` 清单，和提交体键集对照用）、
`surveyFollowup`（追问响应体）、`surveyFollowupQuestions[]`（这次追问的问题）、
`surveyUrl`、`surveyFixtureResponse`。

状态面板那一组（2026-10-09 新增，供无头用例断言；2026-10-15 起写它的 `js/status.js` 只在启动页挂载）：
`statusOpen`、`statusState`（`closed` → `loading` → `ok` / `error` / `degraded`）、
`statusUrl`（`/status.json` 实际打到的地址）、`statusPayload`（读到的报告；降级时为 `null`）、
`statusError`（报告用不了的原因）、`statusDegraded[]`、`statusEvents[]`（`open` / `render` / `degrade` / `close`）。

启动页那一组（2026-10-15 新增，**只存在于启动页**；阅读器页 `reader.html` 里那份 `__arDebug`
声明没有 `page` 与 `launcher` 这两个键）：

- `page`：`"launcher"`（拆页的页面标识，真服务上打开 `/` 的断言就看它）；
- `launcher.sources`（历史记录）：`{state, url, count, error, degraded[]}`，
  `state ∈ idle | loading | ok | empty | degraded`；`count` 是**真正渲染出来的行数**
  （空列表是 `0`，读不到是 `null`——不拿 0 冒充「读到空清单」）；
- `launcher.upload`（输入文件）：`{state, sourceId, paragraphs, overwritten, error, hint, httpStatus, url,
  encoding, encodingNote}`，`state ∈ idle | ready | invalid | uploading | ok | error`；`error` / `hint`
  是后端那两句话的原文（成功路径上都被清成 `null`）；**`invalid` 档也带 `error`**（＝状态行那句原因，
  2026-10-15 复核修：原来恒为 `null`），且每次进新状态都把上一轮的结果字段
  （`sourceId / paragraphs / overwritten / httpStatus / url / encoding / encodingNote`）清成 `null`；
  `encoding ∈ "utf-8" | "utf-8-lossy" | null`——`lossy` 表示正文里嗅到了 `U+FFFD` 或 NUL 字节
  （非 UTF-8：照常摄取，但 `encodingNote` 在四个出口都可见）；
- `launcher.survey`（背景测评）：`{state, answeredAt, answered, skipped, error, url, version,
  answeredVersion, degraded[], openReason, lastOpen}`，
  `state ∈ idle | loading | answered | skipped | unanswered | degraded`；
  `openReason ∈ launcher | no-questions | load-failed`（这次是怎么打开的），
  `lastOpen = {opened, reason, at}`（最近一次点「填写问卷」的结果）；`degraded[]` 是卡片里那份
  可见说明清单（后端的 `degraded[]` + 「问卷已更新」那一句）；
- `surveyBootReason = "launcher-no-auto-popup"`：启动页**不自动弹**问卷的那一档原因。

三组都是**只合并补丁、不整体替换**（`launcher.js` 的 `setDebug`），所以断言只看自己要的那几个键；
`ready` 在②③两块取数都落定（含各自的降级）之后才置位——无头验证只等它，不 sleep。

## 接口契约（读前背景问卷，PLAN-background-survey §8）

```jsonc
// GET /api/survey → 题目由后端给，前端不硬编码
{"version": 1, "answered_version": null,     // answered_version：读者上次作答时的版本（没答过是 null）
 "questions": [ /* qid/field/label/kind/options/when/alsoWhen/exactly/exclusive/maxLength/hint/placeholder */ ],
 "answers": {"education": "undergraduate", "discipline": "economics", "reading_materials": ["textbook"]},
 "skipped": false, "degraded": []}

// POST /api/survey ← 写画像：固定题作答（只发八个槽位，**不带 version**）/ 整场跳过 / 追问作答
{"answers": {"education": "...", "high_school_electives": [...], "discipline": "...", "major": "...",
             "strengths": [...], "strengths_note": "...", "reading_materials": [...], "preference": "..."}}
{"skipped": true}
{"followups": [{"q": "…", "a": "…"}]}   // 追问答案的**保存**归这里（`at` 由后端补）
// → {"ok": true, "background": {...}, "sketch": "读者背景：…", "degraded": []}

// POST /api/survey/followup ← **只生成**追问问题（空请求体；输入是画像里已存的作答）
{}                                   // → {questions: [{q, hint}], text, degraded, prompt_version, model?, usage?}
```

- 字段名以 `questions[].field` 为准（`reading_materials` / `preference`，**不是** `materials` / `explanation`）；
  `university` 只是画像里的收纳层，作答与接口一律**扁平**（`discipline` / `major`）。
- 条件题没出现时该字段**不出现**（不写 null）；提交体里多一个键后端就回 400（`validate_answers` 只认那八个槽位）。
- 追问答案**只能**放在 `followups` 里（`[{q, a}]`）；塞进八个槽位就是 400。
- **两个端点各司其职**：`/api/survey/followup` 生成问题，`/api/survey` 写画像
  （固定题作答 / 整场跳过 / 追问作答）。后端在 `/api/survey/followup` 上留了「也能并 followups」的兼容分支，
  前端不再用它保存答案。
- 版本号不进浏览器本地存储；`localStorage` 里只有显示设置那一个键 `ar.settings.v1`。
- 三个端点在前端都有入口（`GET`/`POST /api/survey` + `POST /api/survey/followup`），见上面的交互段。

## 接口契约（POST /api/compress 与 POST /api/events，PLAN-actions-feedback §5.2 / §7.2）

```jsonc
// POST /api/compress ← 读中「太啰嗦」：当场压缩这一段（读者 id 由服务端定，前端不传）
{"source_id": "capital-v1-ch01", "paragraph": "p021", "reason": "too_verbose"}
// → 成功（200）：`degraded` 平时是 `[]`
{"ok": true, "paragraph": "p021", "action": "compress", "text": "压缩后的正文…", "scope": "paragraph",
 "cached": false, "usage": {...}, "attempts": [...], "model": "…", "prompt_version": "compress-v3",
 "degraded": [], "artifacts_updated": true, "hint": "已压缩，请重跑 ar check。",
 "fact_declarations": {}, "waived": [], "over_declared": [], "fact_anchor_policy": "keep_all"}
// → 成功且**申报后放行**（松口径）：申报与放行条目一起回，页面当场显示「本段申报」+ 放行徽标
{"ok": true, …, "degraded": ["本段申报放弃了 1 项事实锚，已按读者口径 allow_declared_drops 放行：…"],
 "hint": "已压缩，请重跑 ar check 复核（fidelity 已重置为未校验）；本段申报放弃了 1 项事实锚（已按读者口径 allow_declared_drops 放行）",
 "fact_declarations": {"numbers": ["1696"], "note": "举例数据只为给出定性结论"},
 "waived": [{"paragraph": "p021", "kind": "numbers", "missing": ["1696"], …}], "over_declared": [],
 "fact_anchor_policy": "allow_declared_drops"}
// → 成功但**行为事件没记上**（只有这一种成因）：照常写盘、`ok` 照常 true，只是多一条说明
{"ok": true, …, "artifacts_updated": true,
 "degraded": ["这段已压缩，但这次点击没能写进行为事件流 events.jsonl（不影响压缩结果与产物）：…"]}
// → 降级（**也是一律 200**，照 /api/explain 的惯例）：正文不变
{"ok": false, "reason": "fact_anchor_mismatch", "text": "…", "degraded": ["事实锚对不上：数字 8"]}
```

- `reason` 默认 `"too_verbose"`；降级的 `reason ∈ model_not_configured / model_error / bad_reply /
  fact_anchor_mismatch / no_artifacts / paragraph_not_found / compress_not_shorter`。
- **成功的判据是 `ok === true` 且 `text` 非空**；其余一律按降级处理（正文一个字符都不动）。
- **成功响应追加四个申报键**（`PLAN-actions-feedback` §14.1，2026-10-14 收尾）：
  `fact_declarations`（本次申报，无申报时 `{}`）/ `waived[]`（本次按读者口径放行的条目，
  结构同 `fidelity.waived` 的一行）/ `over_declared[]`（「锚点一个没少却申报放弃了某项」的条目，
  **不影响放行结论**）/ `fact_anchor_policy`（本次采用的口径）。前端据此**就地重画**这一段的
  「本段申报（事实锚）」与放行徽标（`renderCardDeclaration` + `applyCompressFidelity`）；
  缓存命中时这四个键跟着缓存一起回。
- **`degraded` 与 `ok` 是两条轴**：`ok:true` 时它承载三种说明——① 行为事件没记上；
  ② 申报读不通（结构不对 / 不认识的键）；③ 「申报后放行 / 多申报」的一句可读说明。
  压缩结果与产物**不受影响**，前端照常刷新正文、照常显示成功横幅，**额外**打一条 warn 横幅
  （`kind: compress-degraded-notes`，走 `degrade()`）。别把非空 `degraded` 当成压缩失败。
- `hint` 是后端给的那句「请重跑 `ar check`」提示，前端**原样展示**；后端没给时用固定文案
  「已压缩，请重跑 ar check。」。有申报放行时 `hint` 里会多一句「本段申报放弃了 N 项事实锚
  （已按读者口径 X 放行）」。`artifacts_updated` 只作可断言标志，前端不据此改行为。
- 夹具模式没有这个端点：点「太啰嗦」只在页面上说明「夹具模式没有压缩后端」，正文不变。

```jsonc
// POST /api/events ← 行为事件批量上报（也容忍单个对象）
{"events": [{"v": 1, "id": "客户端 uuid", "at": "2026-10-13T18:20:00+08:00", "kind": "dwell",
             "source_id": "capital-v1-ch01", "paragraph": "p012", "ms": 4200}]}
// → {"ok": true, "written": 1, "duplicates": 0, "total": 1, "degraded": []}
```

- `reader` **由服务端盖章**（请求里给了也不认）；`id` 用于幂等去重。
- **只 append，绝不写 `profile.yaml`**（那会换画像指纹 ⇒ 讲解缓存全失效）。
- 写盘失败 → `ok:false` + `written:0` + `degraded` 文案（前端据此留可见横幅）；**不许 200 假装成功**。

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

## 接口契约（启动页的 GET /api/sources 与 POST /api/ingest，2026-10-15）

```jsonc
// GET /api/sources → 材料清单（**只读，一个字节都不写盘**）。排序由后端定好，前端不再重排
{"sources": [{"source_id": "capital-v1-ch01", "paragraphs": 25,
              "ingested_at": "2026-10-05T20:11:03+08:00",
              "mtime": "2026-10-13T12:27:42+08:00",     // 「最后动过」＝产物里存在者中最新的那个
              "concepts": {"present": true, "count": 10, "generated_at": "…"},   // 缺产物 → 整块 null
              "rewrite":  {"present": true, "generated_at": "…",
                           "action_counts": {"expand": 10, "compress": 0, "keep": 15, "illustrate": 0},
                           "degraded_count": 1, "paragraph_count": 25},
              "fidelity": {"present": true, "checked": true, "ok": true, "mismatch_count": 0,
                           "counts": {"paragraphs": 25, "compared": 25, "numbers": 34, "quotes": 11, "books": 13}}}],
 "degraded": []}

// POST /api/ingest ← 输入文件（只摄取，不调模型、不跑 analyze / rewrite / check）
{"filename": "notes.md", "content": "正文…", "source_id": "draft-notes", "overwrite": false}
// → 200 {"source_id": "draft-notes", "paragraphs": 4, "ingested_at": "2026-10-15T21:40:00+08:00",
//        "anchors_stable": true, "overwritten": false, "degraded": []}
```

- **缺产物 = 那一块 `null` + 顶层 `degraded` 一条可读说明**（不是缺键、也不是编造的 `0` / `[]`）：
  历史记录块据此逐项点名「概念清单未抽取 / 逐段改写还没跑 / 事实锚还没校验」，三块都缺时写「只摄取…」。
- `mtime` 与 `ingested_at` 都是**本地时区**的 ISO 8601；前端**原样保留钟点与偏移**
  （`2026-10-13 12:27:42 +08:00`），**不**用 `new Date()` 按浏览器时区再转一次——
  那样跨时区访问会把同一个瞬间显示成另一个钟点，和 `data/` 目录里的文件时间对不上。
- 历史记录块的动作统计**只列非 0 的动作**（`展开 10 / 保持 15`）：真实产物四个键都在，
  把「压缩 0 / 配图 0」也打出来只会淹掉关键数字；全是 0 时明写「动作统计为空（各动作都是 0）」。
  后端将来加了新动作，照原键名打出来（不静默丢）。
- 校验状态四档：`checked === true` 且 `mismatch_count === 0` → 「已校验通过」（`ok`）；
  `> 0` → 「有 N 处不一致」（`error`）；`checked === false` → 「未校验（还没跑过 ar check）」；
  读不出来（没有 `fidelity` 块 / 字段形状不对 / 没有 `mismatch_count`）→ 「读不出来」那一档，
  **不冒充通过也不冒充未校验**（可断言的 `[data-role="history-fidelity"][data-state]`）。
- **`POST /api/ingest` 的失败一律回 JSON + 可读 `error`**：400（扩展名不在白名单 / `source_id`
  不合法 / 正文超 `MAX_UPLOAD_CHARS = 2_000_000` 字符 / 编不成 UTF-8）、**409**（同名材料已存在且
  没带 `overwrite: true`，另给 `hint` 说明怎么覆盖）、500（磁盘写不进去）。
  前端把 `error` 当消息、`hint` 放进 `DataError.detail`，两条都显示出来
  （`fetchJsonExplained`，见 `js/api.js`）。成功后前端**不自己拼一条清单**，而是重新 `GET /api/sources`。
- 成功响应里 `anchors_stable === false`（这次摄取与上一次的段锚点对不上）时，输入文件块的成功文案里
  **另加一句**「注意：段锚点与上一次摄取不一致（覆盖后既有产物可能对不上，需要重跑
  analyze / rewrite / check）」——降级说明与 `error` / `hint` 一样都要看得见。
- 两个端点都**没有夹具版本**（启动页恒正式模式），也没有模型调用。

## 无头验证

```bash
python -m pytest tests/web -q          # 在本项目根目录跑（210 passed + 3 skipped, 0 failed；213 collected）
```

上面那行是 **2026-10-24 通知中心当天**的实测口径（系统 Python；`ar serve --port 8765` **没在跑**、
`web/status.json` 已生成）：**210 passed + 3 skipped（213 collected，159 秒）**。三个 skip = 两条真服务
用例（服务没起，口径见文件末尾）+ 默认不跑的真实计费用例
`test_real_service_explain_end_to_end_is_billable`。相对 2026-10-22 的 196 collected，本批多 17 条：
`test_notify_center.py` **12 条**（9 条面板契约 + 窄屏 3 条参数化）+ `test_banner_lifecycle.py` 的
失效时间与收起 **5 条**（含 warn 的 20 秒那条线、error 同一条线各一条；195 + 17 = 212 =
210 passed + 那 2 条这次被 skip 的真服务用例）。

更早那行是 **2026-10-22 画像面板当天**的实测口径（系统 Python；`ar serve --port 8765` 在跑、
`web/status.json` 已生成）：**195 passed + 1 skipped（196 collected）**，唯一那个 skip 是默认不跑的
真实计费用例 `test_real_service_explain_end_to_end_is_billable`。
= 工作区现状的 **189 passed + 1 skipped**（2026-10-22 实测；旧账面 184 之后已有的未提交启动页改动
另多 5 条，不是这一批加的）+ 本批 `test_profile_ui.py` **6 条**。
更早的账：2026-10-15 复核修复当天实测 184 passed + 1 skipped（185 collected），
相对拆页当天的 171 passed + 1 skipped（172 collected）**多 13 条**，就是那次复核修复的用例：
`test_launcher.py` 12 条（B1 显示设置 4：存档吃到 body / 系统深色首访 / 两页底色一致 /
六种组合下导语读得清；B2 连点只发一个请求 1；B3 非 UTF-8 与二进制各 1；B4 invalid 三档 3；
C2 结构不符带真实 url 1；C1 JSON `null` 如实说 1）+ `test_reader_ui.py` 1 条
（C7：doc 载入失败显示后端 `error` / `hint` 原文）。
更早的账只当历史：**拆页前实测 146 passed + 3 skipped（149 collected）**，171 那一版的口径是
149 + 拆页新增的 23（启动页 22 条 + `test_status_panel.py` 的
`test_reader_page_has_no_status_entry`）；再早的 `144 passed + 3 skipped` 记于 2026-10-13 前后，
不是任何一个现行口径。
**服务没起时**另有两条真服务用例会 skip（`test_reader_features.py` 的
`test_real_service_page_and_explain_payload_without_calling_the_model` 与
`test_status_panel.py` 的真服务孪生用例）。这一档 **2026-10-24 实测过**：就是上面那行
**210 passed + 3 skipped（213 collected）**（服务没起、产物已生成；当年按 196 条口径推算的
「约 193 passed + 3 skipped」只是推算，现在以实测为准）。

**必须用系统 Python 跑**（`python -m pytest tests/web -q`，不是 `uv run pytest`）：
用 uv 的 `.venv` 时子进程写不了系统 `%TEMP%`，playwright 的 `mkdtemp` 会 `EPERM` ——
**那是跑法错误，不是缺陷**。

另外，`web/status.json` 还没生成时
`test_status_panel.py::test_launcher_renders_the_generated_artifact`（拆页前叫
`test_fixture_mode_renders_the_generated_artifact`）与它的真服务孪生用例
`test_real_service_status_panel_matches_the_artifact_without_a_model_call` 都会 skip
（多 skip、不是失败；后者还要求 `ar serve --port 8765` 在跑）。

用本机 Edge（`channel="msedge"`，退路是已缓存内核 `chromium-1084`），**不下载任何浏览器内核**。
测试自己起一个静态服务把项目根目录当站点，所以**绝大多数用例不依赖后端**；
问卷的正式模式用例用 `page.route` 在浏览器侧挂一层假后端（`/api/doc`、`/api/profile`、
`GET/POST /api/survey`），夹具用例走静态服务；
状态面板同理——用例用 `page.route` 把 `/status.json` 喂成各种形状（正常 / 404 / 坏 JSON / 没跑 /
失败），**不依赖磁盘上那份产物**；2026-10-15 拆页后这套用例（`test_status_panel.py`）一律打开**启动页**
（面板的入口在那里），并多一条 `test_reader_page_has_no_status_entry` 守拆页纪律；
2026-10-13 新增的三套（`test_figures.py` / `test_compress_action.py` / `test_behavior_events.py`）
共用一份更小范围的假后端 `tests/web/ar_backend.py`（`/api/doc`、`/api/profile`、
`/api/profile/rating`、`/api/profile/preference`、`/api/survey`、`/api/explain`、`/api/compress`、
`/api/events`，全是固定 JSON，**零真实模型调用**；写画像的两个端点会把请求体记进
`rating_calls` / `preference_calls` 并就地改内存里的画像，模拟后端「响应里回整份写回后的画像」；
它的 `open_fixture` / `open_api` 拆页后指向 `web/reader.html`——阅读器页）；
2026-10-15 新增的 `test_launcher.py`（34 条用例：四块都在 + 入口归位 + 清单逐字段 / 后端说明 /
空列表 / 404 / 结构不符（含「带真实 url」）+ 上传成功 / 400 / 409 / 上传中按钮禁用 / 连点只发一个请求 /
非 UTF-8 与二进制可见说明 / invalid 三档带原因且清旧值 + 问卷卡三态 / 版本提示与后端说明 /
读不到 / 点按钮打开同一张表单并在答完与关闭后刷新 / 无题目不空壳 + 显示设置 4 条：存档吃到 body /
系统深色首访 / 两页底色一致 / 六种组合下导语读得清）**只看启动页**，
假后端是本文件自己的 `LauncherApi`（`page.route` 拦 `/api/sources`、`/api/survey`、`/api/ingest`，
零真实模型调用；助手 `dbg` / `banner_texts` / `has_banner` 仍从 `ar_backend.py` 复用）；
mermaid 那几条要么放真库（5.3 MiB 从本机静态服务取，验证真渲染），要么用几十字节的假库
（验证「只注入一次」「render() 抛错 → 退回文字」）。
2026-10-22 新增的 `test_profile_ui.py`（6 条）用同一份 `ar_backend.py` 的假后端：
入口与当前状态、自评写回（请求体逐字段 + 面板 / 指纹 / 横幅）、口径写回、
夹具模式不请求 `/api/*` 且控件禁用、读失败、写失败不装成功。
2026-10-24 新增的 `test_notify_center.py`（12 条 = 9 条面板契约 + 窄屏 3 条参数化）只看**两页的顶栏与通知面板**（复用
`test_survey_ui.py` 的夹具页与 `test_status_panel.py` 的启动页开启 / 假后端，零真实请求）：
入口在两页顶栏且 role 不撞 `status-toggle` / 不包进 launcher-block、点开 `aria-expanded` 跟随、
横幅收起后未读上涨与打开即清零、`Esc` 与 `×` 与「点外部」三个出口、关闭后整层不留残影且焦点回入口、
历史条目点一下就原样重弹且不在历史里翻倍（连「重弹那条被关掉」也要安全：`record: false` 的横幅
没进 store，`markCollapsed` 拿到未知 id 必须是 no-op）、空历史有话说、刷新后清空（纯内存）、启动页同一套；
另 3 条参数化守**窄屏顶栏**（430 / 390 / 360：`.topbar__side` 无横向溢出、顶栏仍 48px、
三颗按钮都点得到、段号仍在无障碍树里）。
同一批 `test_banner_lifecycle.py` 另加 **5 条**：`span.banner__expiry` 的倒计时文字、
`data-expires-at ≈ now + 8s`（±2s）、倒计时每秒真的在减、warn 的 `data-expires-at ≈ now + 20s`
（±2s）与倒计时文字（原来那条 `== 0` /「不会自动收起」的断言随行为改变换成了这一条）、
warn 到 20 秒自动收起且条目进历史（`reason: timeout`、未读 +1）、error 同一条 20 秒时间线
（用「材料载入失败」造现场，到点归零 + 进历史）、以及自动消失后 `__arDebug.notifications.items`
仍含该条（`reason: timeout`）。
`test_reader_features.py` 末尾两条打真服务（`http://127.0.0.1:8765`，跑之前先起 `ar serve`，
**服务没起就 skip**）：

- `test_real_service_page_and_explain_payload_without_calling_the_model`：默认就跑，
  用 `page.route` 拦下 `POST /api/explain` 只核对请求体，**不发模型调用**；
- `test_real_service_explain_end_to_end_is_billable`：默认 skip，要显式开开关
  （`$env:AR_E2E_BILLABLE = "1"`）才跑——**一次真实计费调用**（后端缓存命中时不计费）。

状态面板还有一条真服务用例（同一条口径：服务没起或产物没生成就 skip，不是失败）：

- `test_status_panel.py::test_real_service_status_panel_matches_the_artifact_without_a_model_call`：
  真服务 + 磁盘上的 `web/status.json`，逐项核对页面数字（含 pytest 摘要行）——2026-10-15 起它开的
  是 `http://127.0.0.1:8765/`（启动页；`/?source=…` 那条链接现在会 302 到阅读器页，那边没有面板），
  并用**请求清单**证明这条用例一个模型调用都没发、也没有任何 POST。
