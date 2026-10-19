# PLAN：动作补齐 + 行为回流 + 局部重跑（第四批）

> **状态：契约冻结稿**（2026-10-13 由协调者写；实施子代理以本文件为唯一契约来源，
> 与 `DESIGN.md` / `README.md` 冲突处以本文件为准，发现冲突写进结算回报，不要自行改契约）。
> 生效范围：`compress` 动作、`illustrate` 动作与图渲染、「太啰嗦」当场压缩、
> 行为信号回流（`events.jsonl` + 画像合并）、局部重跑（只重跑指定段 / 失败批次 / 受影响段）。
>
> **本批硬约束：只做代码级改动，全程零真实模型调用。**
> 不许运行 `uv run ar analyze` / `uv run ar rewrite` / `uv run ar serve` 去打真网关，
> 不许设 `AR_E2E_BILLABLE`，不许跑 `tests/web` 里那条 billable 用例。
> 测试一律假模型（注入固定响应 / `monkeypatch`）、夹具、路由级假后端。

---

## 0. 范围与非目标

**做**：

1. **压缩动作 `compress`**：`ar rewrite` 里真正实现（现在枚举位在、一律按 keep 处理并标 `deferred`）。
2. **配图动作 `illustrate`**：后端产出图定义并存进变更记录，前端就地渲染；生成/渲染失败退回文字。
3. **读中「太啰嗦」→ 当场压缩该段并刷新正文**（用户 2026-10-13 拍板，见 §1 第 12 条）。
4. **行为信号回流**：阅读行为落 `events.jsonl` → 画像合并规则 `applySignals` 演化
   `concepts[].state` / `confidence` / `evidence`；`overrides` 永不被覆盖。
5. **局部重跑**：`ar rewrite` 追加 `--only` / `--retry-failed` / `--from-signals`，
   只重跑目标段并与既有产物逐段合并；在线压缩复用同一套合并器。

**不做**（别顺手做）：严格档、按材料出题的短测验、网页/PDF/剪贴板入口、跨材料概念复用、
多人账号、讲解链路注入背景（`/api/explain` 仍只按概念状态讲）、自研图表渲染、改画像指纹口径。

---

## 1. 拍板表（决策点 → 结论）

| # | 决策点 | 结论 |
|---|---|---|
| 1 | 图的交付通道 | **`changes[i].figure` 内联**，不新建 `figures.json`（`/api/doc` 与夹具已带 `changes`；`ar check` 本来就读它） |
| 2 | 图在 `rewritten.md` 里的落法 | **图不进 md**：illustrate 段正文与原文**逐字节相同**；图由前端在该段容器内就地插入。保住「按空行切段 + 逐段对位」与 text_hash 校验 |
| 3 | 图内数字与事实锚 | 图**不参与**事实锚（SVG 坐标数字无法避免）。只对 `figure.source` 里多出的事实锚点记一条降级 |
| 4 | SVG 来源 | 模型产源码 + 后端白名单清洗（禁 `<script>` / `on*` 属性 / `foreignObject` / 外部 `href`）。不自研渲染（DESIGN §1 非目标） |
| 5 | `deferred` 机制 | **保留**：它表示「枚举内、本期未实现」。本批之后 compress / illustrate 不再进 deferred；DEFERRED_NOTE 与 CLI 文案一个字不改 |
| 6 | mermaid 交付 | 搬本机离线构建 `deepseek-chat/app/js/vendor/mermaid.min.js`（实测 5,575,485 B）到 `web/vendor/mermaid.min.js`；**懒加载**（首次真的要画图才注入 `<script>`），加载或渲染失败 → 退回文字 |
| 7 | 事件落盘位置 | `profiles/<reader>/events.jsonl`：一行一条 JSON、`ensure_ascii=False`、追加写、结尾换行 |
| 8 | 采集端点是否改画像 | **不改**。`POST /api/events` 只 append。任何一次 `profile.yaml` 写盘都会换指纹（`cache.py` 的 `(路径,字节数,mtime_ns)`）⇒ 讲解 LRU 全失效 + 前端会话键变化 ⇒ 同一 (段,kind) 重复计费而文字一模一样 |
| 9 | 画像合并入口 | `ar.profile.applySignals(profile, signals, *, now=None) -> ProfileUpdate`（纯函数，不碰磁盘）；CLI 显式动作 `ar feedback <id> --reader R [--apply]`，**默认 dry-run**，`--apply` 才写盘；写盘前若序列化文本与现有文件逐字相同则**不写** |
| 10 | 局部重跑 CLI 形态 | 在既有 `ar rewrite` 上**追加可选参数** `--only p012,p014` / `--retry-failed` / `--from-signals`（三者互斥，同给 → 退出码 2）。子命令名与既有参数语义不变 |
| 11 | 合并方式 | **逐段 patch-in-place**：未重跑段逐字节保留；`fidelity` **重置为未校验**；`usage` / `batch_usage` / `attempts` / `retry_account` 记**本次重跑**的账（口径不变），另追加 `meta.rerun` 与 `meta.rerun_history[]` |
| 12 | 「太啰嗦」点击 | **当场调模型压缩该段并刷新正文**（用户拍板）。事实锚不过 → **拒绝写入**、保留原段、可见降级 |
| 13 | 在线压缩是否落盘 | **落盘**：写回该段的 `decisions.json` / `changes.json` / `rewritten.md`，`fidelity` 重置并提示重跑 `ar check` |
| 14 | 在线压缩缓存 | 进进程内 LRU（键含画像指纹）；重复点同一段命中缓存不再调模型 |
| 15 | 短停留是否当负信号 | **不当**，只在 evidence 留痕。负信号只来自显式求助（没懂 / 太啰嗦 / 选片讲解）。理由：快速略过也可能是「已经懂了」 |
| 16 | 新模型工具 | 加第五个 `compress_paragraph`；`tests/test_tools.py` 的「恰好四个工具」断言同步改成「恰好五个」 |
| 17 | 新子命令 | 加 `ar feedback`（第 7 个子命令，纯增补）。原有五个子命令 + `ar tools` 的名称/参数/退出码语义一个不变 |

---

## 2. 导航：现状关键落点

> 行号是 2026-10-13 只读侦察时的快照，改动后会有偏移；**按符号名找，不要硬信行号**。

| 落点 | 现状 |
|---|---|
| `src/ar/rewrite.py:85-88` | `ACTIONS` / `IMPLEMENTED_ACTIONS` / `UNIMPLEMENTED_ACTIONS` 三个枚举 |
| `rewrite.py:199` `needs_rewrite` | 只认 `expand`（compress 加了不改这里就是静默空操作） |
| `rewrite.py:254-281` `_normalize_plan_items` | 只挑认识的键，**其余静默丢弃**（模型输出不经 JSON Schema） |
| `rewrite.py:316-320` | 「枚举内未实现 → 按 keep + 标 deferred」那一处 |
| `rewrite.py:337-346` | 改写文本取值与「展开却没变长」的截断判据（对 compress 是反的） |
| `rewrite.py:396` `_batch_ranges` / `:493` `_plan_one_batch` / `:629` `total` | 批次在全篇下标空间切分 ⇒ 局部重跑可「先切批再取交集」 |
| `rewrite.py:1126` | 逐段保真时**只跳过 expand** 的名单（漏了 compress 会误报 keep_mismatch） |
| `rewrite.py:1139-1149` | `fidelity` 在 rewrite 阶段写 `checked:false` |
| `rewrite.py:1151-1160` / `:1191-1248` | `action_counts` / `deferred_count` / `meta`（含 `implemented_actions`） |
| `rewrite.py:413` `_format_profile_block` | 提示词里画像的可见面只有 `cid=state`（改 evidence/confidence 不破坏前缀缓存） |
| `rewrite.py:439-462` | 概念清单进提示词的构造 —— **`illustration_candidates` 目前根本没进提示词** |
| `src/ar/prompts.py:58` | 提示词版本常量（`rewrite-v3` → 本批升 `rewrite-v4`） |
| `prompts.py:139-178` | `REWRITE_USER_TEMPLATE`：`:157` 规则 2 写死「compress / illustrate 本期不实现」 |
| `prompts.py:122` | `REWRITE_ACTIONS` |
| `src/ar/tools.py:142-158` | `_DECISION`（action 无 enum，要加可选 `figure`） |
| `tools.py:180-196` | `REWRITE_BATCH_PARAMETERS`（新入参必须**可选**，不进 required） |
| `tools.py:535-569` | `_run_rewrite_batch` |
| `src/ar/fidelity.py:69-72` | `facts_of`：图内联进 md 时会把 SVG 坐标数字当事实锚（所以图不进 md） |
| `fidelity.py:117-119` + `web/js/schema.js:50-55` | 按空行切块 + 逐段对位，段数必须等于原文 |
| `src/ar/analyze.py` | `illustration_candidates` 与 `validate_inventory`（`kind` 只四类） |
| `src/ar/profile.py:61` / `:70` / `:115` / `:126` / `:155` | `default_profile` / `_normalize` 顶层白名单 / `save_profile` 原子写 / `apply_rating` / `overrides` 只写不读 |
| `src/ar/cache.py:37` / `:51` | 画像指纹与讲解缓存键 |
| `src/ar/server.py:474-486` | 路由表（新端点必须排在 `/api/{rest:path}` 兜底**之前**） |
| `src/ar/survey.py:1050` `write_survey_record` | 事件记账要对齐的风格（读者级记录文件） |
| `web/js/main.js:267/292/435/458/513/685/846-868/318` | 天然埋点：读完 / 当前段 / 卡片开关 / 讲解决策 / 选片浮条 / 抽屉 |
| `web/js/view.js:11-15` `EXPLAIN_KINDS` | 「没懂 / 再简单点 / 再详细点」是唯一等价物；**全仓 `web/` 没有「太啰嗦」字样**（本批新增） |
| `web/index.html:139-197` `__arDebug` | 前端断言面；`survey.js:517-529` / `status.js:54-57` 的 `emit()` 是埋点写法范本 |
| `samples/_build/make_fixture.py:160-207,313-336` | 手写夹具（现有 **15 个 compress 段带 `deferred:true`**，实现后不成立，要重定） |

---

## 3. 压缩动作（离线，`ar rewrite` 内）

1. `COMPRESS` 从 `UNIMPLEMENTED_ACTIONS` 移进 `IMPLEMENTED_ACTIONS`（枚举整体仍是四动作）。
2. `needs_rewrite` 认 `expand` **与** `compress`。
3. 改写文本取值分支加 `compress`。
4. **截断判据拆成两条**：`expand` 求「变长 + 句末标点收尾」；`compress` 求「**变短** + 句末标点收尾」。
   现在那条「不比原文长」的文案对 compress 是反的。
5. **逐段保真跳过名单必须加 `compress`**（否则该段拿原文比改写稿、误报 keep_mismatch，
   连带 `paragraphs_match_source:false` 与 CLI 假警告）。这是本项最容易漏的一处。
6. 提示词：规则 2 改成四动作判据、删掉「compress / illustrate 本期不实现」；
   `compress` 的约束写清「只删冗余、不许丢事实锚（数字 / 引号引文 / 书名号《》）」。
7. CLI 的动作计数打印**动态化**（现在 `cli.py:187-195` 硬编码 expand/keep）。
8. 事实锚：压缩稿与原文的逐段比对走现成的 `check_fidelity`，**不放宽**。

---

## 4. 配图动作（`illustrate` + 图渲染）

### 4.1 产物字段（冻结）

`changes[i]`：

```json
{
  "paragraph": "p034",
  "action": "illustrate",
  "why": { "profile": [], "note": "流程类难点：三步递进，纯文字看不出先后" },
  "confidence": 0.8,
  "facts": { "numbers": [], "quotes": [] },
  "figure": { "kind": "mermaid", "source": "flowchart TD\n  A[...] --> B[...]", "caption": "三步递进" }
}
```

- `figure.kind ∈ {mermaid, svg}`；`figure.source` 是 **mermaid 源码**（首词就是图类型关键字）或**自包含 SVG 源码**；
  `caption` 一句话图注（可缺，≤40 字）。
- **`figure` 只在成功时出现**（照 `deferred` 的「为真才出现」先例）；失败时该条追加
  `"figure_error": "一句话原因"`，**且 `action` 记为 `keep`**（正文照旧），降级进 `meta.degraded`。
- illustrate 段的 `rewritten.md` 正文 = 原文**逐字节**（图不进 md，见 §1 第 2 条）。
- `decisions.json` **不带** `figure`（与不带 `deferred` 同口径）。

### 4.2 后端校验与清洗（失败一律可见）

- `kind` 只收 `mermaid` / `svg`，别的值 → 按配图失败处理。
- mermaid：非空、首词是图类型关键字（`flowchart` / `graph` / `sequenceDiagram` / `stateDiagram-v2` /
  `classDiagram` / `erDiagram` / `gantt` / `pie` / `mindmap` / `timeline`）、不含 ``` 围栏、
  行数 ≤ 40、字符数 ≤ 2000。
- SVG：用标准库 `xml.etree.ElementTree` 解析。**白名单外一律拒绝，不做「清洗后接着用」**
  （勘误 2026-10-13：原文写的是「清洗后继续用」，与本文 §11 验收第 2 条冲突，**以本条为准**）：
  XML 解析失败 / 根节点不是 `svg` / 含 `<script>` / 含任何 `on*` 属性 / 含 `foreignObject` /
  含引用外部资源的 `href` 或 `xlink:href`（`#` 内部锚点保留）→ 一律按配图失败处理
  （`action` 退回 `keep` + `figure_error` 点名违规项 + 可见降级）。合规 SVG 原样返回。
- 图源码里出现原文没有的数字 / 引号引文 / 书名号 → **不阻断**，记一条降级（§1 第 3 条）。
- 配图候选：把 `concepts.json` 的 `illustration_candidates` **放进 `rewrite-v4` 提示词的固定块**
  （概念清单之后、材料正文之前；逐字稳定，不许有时间戳 / 随机数），让模型知道哪里值得配图。
  段级依据只写进 `why.note`。

### 4.3 前端渲染（`web/`）

- 前端在段落容器 `#blk-pNNN` 内、正文之后插入图容器 `[data-role="figure"]`。
  图块**绕开 `web/js/md.js`**（它的 `escapeHtml` 会把 SVG 转成源码）。
- mermaid **懒加载**：仅在页面真的要画图时注入 `<script src=".../vendor/mermaid.min.js">`（5.3 MiB，禁进首屏）。
  加载完调 `mermaid.render()` 得到 SVG 插入容器。
- **失败退回文字**：加载失败 / `render()` 抛错 / 后端已给 `figure_error` → 该位置显示一句可读说明
  （「图渲染失败，退回文字说明」+ `caption` 或 `why.note`），走 `main.js` 的 `degrade()` 让降级可见。
  **不留空白、不留破图**（DESIGN §6）。
- 图渲染**不产生任何模型请求**。

---

## 5. 「太啰嗦」当场压缩（在线，用户拍板）

### 5.1 模型工具（第五个）

`ar.tools` 新增具名工具 `compress_paragraph`：入参 `{paragraph, text, concepts?, profile_block?, instruction?}`，
出参 `{action:"compress", text, why_note?}`。提示词版本常量 `compress-v1`（新增）。
工具内部同样经 `src/ar/llm.py`（重试 / 记账 / 失败不抛），**不抛异常**、入参不合法返回 `INVALID_REQUEST` 信封。

> **订正（2026-10-14 收尾，§14.1）**：入参追加**可选** `fact_anchor_policy`（本读者的事实锚口径，
> 进提示词的「事实锚口径」固定块），出参追加**可选** `fact_declarations`
> （`{numbers?, quotes?, books?, note?}`，**原样带回**，归一与问题清单交给
> `ar.fidelity.parse_declarations`——与 `rewrite_batch` 把 decisions 原样带回同一条分工）。
> 提示词版本随之升 **`compress-v2`**（同一段口径文案 + 规则 6 + JSON 示例那一格）。

### 5.2 HTTP 端点

`POST /api/compress`

- 请求：`{ source_id, paragraph, reason? }`（`reason` 默认 `"too_verbose"`；读者 id 取应用级配置，本次不加按请求传读者）。
- 成功响应：`{ ok: true, paragraph, action: "compress", text, scope: "paragraph",
  cached: false, usage, attempts, model, prompt_version, artifacts_updated: true,
  hint: "已压缩，请重跑 ar check。", degraded: [...] }`。
  **勘误（2026-10-13 收口裁定）**：`degraded` **不必为空** —— 成功路径也要能看见降级：
  「写 compress 行为事件失败」（append 抛错或事件没通过 `validate_event`）时仍是 `ok:true`、
  产物照常写盘，但 `degraded` 里带一条可读说明；前端在成功路径上把非空 `degraded` 显示成 warn 横幅。
  **`hint` 这个键名是冻结的**（2026-10-13 裁决：前端路已按 `hint` 实现，缺省回退固定文案
  「已压缩，请重跑 ar check。」）——后端要给别的措辞就换值，不要换键名。
- 降级响应（**一律 200**，照 `/api/explain` 惯例）：`{ ok: false, reason, text, degraded: [...] }`，
  `reason ∈ model_not_configured / model_error / bad_reply / fact_anchor_mismatch / no_artifacts /
  paragraph_not_found / compress_not_shorter`。
- 事实锚：写盘前先对该段做单段事实锚比对（数字 / 引号引文 / 书名号，数量 + 顺序）；
  不一致 → `fact_anchor_mismatch`，**不写盘**、正文不变、降级文案点名对不上的锚点。
  **勘误（2026-10-14 收尾，§14.1）**：比对改用与 `ar check` **同一个**
  `ar.fidelity.compare_blocks_detailed`，口径取**同一处**（读者画像偏好 `pref_fact_anchor`）：
  少了且**已申报** ⇒ 松口径放行（申报落 `changes[i].fact_declarations`，放行条目进响应的 `waived`，
  重跑 `ar check` 后进 `fidelity.waived`）；**没申报的缺失 / 多出来的锚点 / 顺序调换 / 申报与实测对不上
  ⇒ 照旧拒绝写入**。于是同一段不再出现「`ar check` 放行、在线压缩被拒」。
- 落盘：成功时写回三段产物（§13 条），`fidelity` 重置为未校验，响应里带 `artifacts_updated: true`
  与一句「请重跑 `ar check`」的提示（前端展示）。
- 缓存：键 = `(source_id, paragraph, "compress", 画像指纹)`；命中时 `cached: true`、结构同成功响应、`usage: {}`、不发请求。
- 端点必须排在 `/api/{rest:path}` 兜底之前。
- 该次点击同时产生一条行为事件（§7），kind = `compress`，**事件写失败不影响压缩结果**。

> **订正（2026-10-14 收尾，§14.1）**：成功响应的键从 13 个增到 **17 个**，追加
> `fact_declarations`（本次申报，无申报时 `{}`）/ `waived[]`（本次按读者口径放行的条目）/
> `over_declared[]`（「锚点一个没少却申报放弃了某项」的条目——**不改放行结论**）/
> `fact_anchor_policy`（本次采用的口径）。四个键在缓存命中时跟着缓存一起回；
> 成功路径的 `degraded` 因而多两种可能：申报读不通、以及「放行 / 多申报」的一句可读说明。
> 前端的「本段申报（事实锚）」块由这几个键就地渲染，不必等重跑 `ar check`。

---

## 6. 局部重跑

### 6.1 参数（都追加在 `ar rewrite` 上，互斥）

| 参数 | 目标段来源 |
|---|---|
| `--only p012,p014` | 显式段 id 列表（校验：段存在、格式合法，否则退出码 2） |
| `--retry-failed` | 既有产物的 `decisions.json` 里 `meta.model_errors[].paragraphs` + `meta.unplanned_paragraphs` 的并集；为空则打印「没有需要重跑的段」+ 退出码 0、不写盘 |
| `--from-signals` | 行为回流产出的「受影响段」：`concepts.json` 的 `concepts[].paragraphs` 反查（见 §7.3） |

- 全缺省 = 今天的整篇行为，**逐字不变**。
- 三者同给或两两同给 → 退出码 2 + 明确文案。

### 6.2 执行与合并

- 批次仍按**全篇下标空间**切（`start`/`end`/`total` 语义不变），每批再与目标段取交集；
  空交集不调模型。
- 合并（`rewrite.py` 内收成一个函数，供在线压缩复用）：
  1. 前置校验：三份产物都在、`len(changes.changes) == len(source.meta.paragraphs)`；
     不合格 → 退出码 2 并提示「先整篇 `ar rewrite`」（不许半合并）；
  2. `rewritten.md` 按同一套切块口径逐段替换目标下标（段间空行 + 结尾换行口径不变）；
  3. `decisions.json` / `changes.json` 按 `paragraph` 逐条替换，段序不变；
  4. `fidelity` **重置为未校验**（绝不许把上次的 `checked:true` 带过来）；
  5. `action_counts` / `deferred_count` 按合并后全量重算；
  6. `usage` / `batch_usage` / `attempts` / `retry_account` = **本次重跑**的账；
  7. 追加 `meta.rerun = {at, reason, requested, paragraphs, batches, previous_generated_at}` 与
     `meta.rerun_history[]`（每次一条，含该次 `usage` / `retry_account`）；
  8. `model_errors` / `unplanned_paragraphs` / `rewrite_incomplete` **按合并后真实状态重算**：
     目标段的旧条目先清、按本次结果写回；非目标段的历史条目保留；与目标段部分重叠的历史条目
     拆成「仍失败的段」，空条目删除。退出码 6 随之只反映合并后的真实状态（补齐后应回到 0）。

---

## 7. 行为信号回流

### 7.1 事件（`events.jsonl`，一行一条）

```json
{"v":1,"id":"<客户端 uuid>","at":"2026-10-13T18:20:00+08:00","kind":"explain",
 "source_id":"capital-v1-ch01","reader":"local-default","paragraph":"p012",
 "explain_kind":"explain","scope":"paragraph","cached":false,"degraded":false}
```

- `kind` 白名单：`dwell`（`ms`）/ `read_complete` / `read_abandon` / `explain`
  （`explain_kind` / `scope` / `selection_chars` / `selection_from` / `cached` / `degraded`）/
  `drawer_open` / `selection`（`chars` / `spans` / `from`）/ `compress`（`ok` / `reason?`）/ `skip`（`reason`）。
  **白名单里没有「卡片开关」**（2026-10-13 裁决）：讲解卡片的开合只落前端的 `__arDebug`
  断言面，**不进事件流、也不成为画像信号**。要让开卡变成信号，得先扩这张白名单并让回流规则认它。
- `reader` 由服务端盖章（请求里给了也不认）；`id` 用于幂等去重；缺 `id` 时用
  `(kind, source_id, paragraph, at)` 复合键兜底。
- 校验：`source_id` 非法 → 400；`paragraph` 不在 `source.meta.json` 里 → **丢弃并记 degraded**（不是 400，
  前端可能拿着旧产物）；材料未摄取 → 200 + `ok:false` + degraded（照 `/api/explain` 惯例）。

### 7.2 端点 `POST /api/events`

- 请求 `{events:[…]}`（也容忍单个对象）；响应 `{ok, written, duplicates, total, degraded[]}`。
- **只 append，绝不写 `profile.yaml`**（§1 第 8 条）。
- 写盘失败 → `ok:false` + `written:0` + degraded 文案；**不许 200 假装成功**。
- 新模块 `src/ar/events.py`：`events_path` / `read_events` / `append_events` / `event_id` / `dedup`，
  **不进 `ar.tools` 注册表**（它不调模型）。
- 前端新建 `web/js/events.js`（照 `survey.js` 的 state + `emit` 模式）：缓冲 + 阈值/定时 flush
  （≥10 条或 ≥15 s，另在 `visibilitychange` / `pagehide` 强刷）；夹具模式不发请求，
  一次性可见降级「夹具模式不记行为信号」。

### 7.3 合并规则 `applySignals`

- 纯函数（不碰磁盘），签名 `applySignals(profile, signals, *, now=None) -> ProfileUpdate`；
  `signals` 每条形如 `{concept, paragraph, kind, weight, at, detail}`。
- 计数器放 **`concepts[cid]` 内层**（如 `signals.{score,negative,positive,last_at,history[]}`）——
  **不动顶层白名单**（动顶层就要同步 `default_profile` / `_normalize` / `__all__` / 模块文档 /
  `tests/test_profile.py` 的键集断言）。
- 权重草案：`explain`(没懂) −1.0；`compress`(太啰嗦) −0.75；选片求助 −0.5；读完且该概念无任何求助 +0.25。
- 衰减：合并时先把既有 `score` 乘 `0.5 ** (Δdays / 14)`（14 天半衰期）。
- 阈值：`|score| ≥ 1.2` **且同向证据 ≥ 2 条**（来自 ≥2 个不同段或不同日期）才允许改 `state`；
  一次只升降一档（`known ↔ partial ↔ unknown`）。
- `confidence`：目标值 known 0.8 / partial 0.55 / unknown 0.25；接受改动时朝目标移动
  `clamp(0.15 * |score|, 0.05, 0.2)`，总域 clamp 到 `[0.05, 0.95]`。
- **未达阈值只累加 `score` / `evidence`，完全不动 `state` 与 `confidence`**
  （这样改写提示词可见面逐字不变，前缀缓存不受影响）。
- `overrides` 保护：合并前收集 `protected = {o["concept"] for o in overrides if o.get("state")}`，
  命中者整条跳过，返回 `skipped_overrides[{concept, override_state}]` 走可见清单；
  **绝不修改或删除任何既有 overrides 条目**（要有专门断言）。
- 短停留**不作为负信号**（§1 第 15 条），只记 evidence。
- 偏好类：只对 `pref_paragraph_len` 追加 evidence（太啰嗦 / 长段反复回看），不自动生成新偏好。
- `affected_paragraphs(update, inventory)`（放 `profile.py`）：把 `changed[].concept` 经
  `concepts.json` 的 `concepts[].paragraphs` 反查成段 id 集合，供 `--from-signals` 用。

### 7.4 CLI `ar feedback`

```
uv run ar feedback <source_id> --reader local-reader          # dry-run：打印将发生的变更
uv run ar feedback <source_id> --reader local-reader --apply  # 写盘（内容没变则不写）
```

- 读 `events.jsonl` → 折算 signals → `applySignals` → 打印 `changed` / `skipped_overrides` /
  `evidence_added` / `degraded`；`--apply` 才写 `profile.yaml`。
- 退出码：0 成功（含「无变更」）；2 用法 / 输入错误；产物缺失按 2。

---

## 8. 提示词与工具面（冻结）

- `prompts.py`：`REWRITE_PROMPT_VERSION` → **`rewrite-v4`**；新增 `COMPRESS_PROMPT_VERSION = "compress-v1"`。
  `explain-v2` / `survey-v1` / `concepts-v2` **不动**。
- `build_rewrite_messages(..., illustration_block: str = "")`：新形参与模板位。
  **勘误（2026-10-13，E1 落地后回填）**：原文写的「空块时输出与 `rewrite-v3` 逐字相同」按字面做不到
  ——同一批还改了规则 2/3/9 与 JSON 示例。可操作口径（E1 已实现并钉住）：空块 == 缺省 == 零字符插入，
  有块与无块的差异**只在那一格**（断言方式：把那一格从「有块」版本里删掉 == 无块版本），
  且「首位差异字符仍落在段号位」这条前缀缓存性质照旧成立。
- `REWRITE_BATCH_PARAMETERS`：`illustration_block` 为**可选**入参，不进 `required`；
  `tools.py:_run_rewrite_batch` 与 `rewrite.py` 的批次调用各补一处透传。
- `_DECISION` 加可选 `figure`（对象：`kind` enum + `source` + `caption`）。
  **模型输出不经 JSON Schema 校验**，所以 `_normalize_plan_items` 的 dict 字面量必须同步加 `figure`，
  否则静默丢弃。
- `ar tools` 的 JSON 输出随之从四个工具变五个（含 `compress_paragraph`），契约测试同步。

---

## 9. 全程纪律

1. **零真实模型调用**（本文件开头那条），任何测试都不得打真网关。
2. **降级必须可见**：不许静默截断、静默丢弃、静默失败；新失败路径一律进 `degraded` 并给可读文案。
3. **不许放宽既有校验**：事实锚、段数对位、`fidelity.checked` 的写入时机、退出码 6 的口径一个不动。
4. **旧字段不删不改**：本批所有新字段都是追加；`usage.requests` 仍是「批数」，
   `retry_account.requests` 仍是「真实 HTTP 次数」。
5. **产物不带密钥**；所有对外文本过 `redact()`。
6. **临时文件纪律**：临时目录指到项目内并跑完即删，别在项目根留 `tmp*`
   （前端套件要 Edge，`uv run pytest tests -q` 不收集 `tests/web`）。
7. **不提交 git**：本批改动留在工作区，由用户自己看 diff 后决定。
8. **改了行为就同步文档**（注释 / 模块文档 / `__all__` / README / DESIGN 的相关段落）。

---

## 10. 分路与文件所有权

| 路 | 内容 | 独占写范围 |
|---|---|---|
| **E1** | 压缩动作 + 配图动作（后端引擎）+ `rewrite-v4` 提示词 + 工具面（含新增 `compress_paragraph` 工具与 `illustration_block` 入参） | `src/ar/rewrite.py`、`src/ar/prompts.py`、`src/ar/tools.py`、`src/ar/analyze.py`、`src/ar/cli.py`（仅动作计数打印）、`tests/test_rewrite.py`、`tests/test_tools.py`、`tests/test_analyze.py`、`tests/test_no_silent_degradation.py`、`tests/test_survey.py`（仅那句提示词断言） |
| **W** | 前端：图渲染（含 mermaid 懒加载与失败退回）、「太啰嗦」按钮与压缩后刷新正文、行为埋点与 flush、夹具重定 | `web/**`（含新 `web/vendor/mermaid.min.js`、`web/js/figures.js`、`web/js/events.js`）、`samples/**`、`tests/web/**`、`web/README.md` |
| **S** | 行为回流：`events.py`（新）、`profile.applySignals` / `affected_paragraphs`、`POST /api/events`、`ar feedback` 的库侧 | `src/ar/events.py`（新）、`src/ar/profile.py`、`src/ar/server.py`（仅 `/api/events` 路由与处理）、`tests/test_events.py`（新）、`tests/test_profile.py`、`tests/test_server.py`（仅 events 相关）、`PLAN-actions-feedback.md` 的补充只写在回报里 |
| **E2**（后置，等 E1+S 结算） | 局部重跑（`--only` / `--retry-failed` / `--from-signals` + 合并器）+ `POST /api/compress` + 缓存 + `ar feedback` CLI 入口 | `src/ar/rewrite.py`、`src/ar/cli.py`、`src/ar/server.py`、`src/ar/cache.py`、`tests/test_rewrite.py`、`tests/test_cli.py`、`tests/test_server.py`、`tests/test_explain_scope.py`、`tests/test_compress_api.py`（新） |

**并发纪律**：E1 / W / S 三路并行，各自只碰自己那列文件；跨路要用到别人文件里的东西时，
**按本文件的契约名调用**，不要去改别人的文件；确实需要改，写进结算回报让协调者排下一轮。

---

## 11. 验收标准

1. **零真实模型调用**：结算里要贴出你跑的命令；`uv run pytest tests -q` 全绿
   （后端，假模型；新增用例只增不减）；前端 `python -m pytest tests/web -q` 全绿
   （需要本机 Edge；服务没起时那两条真服务用例是 skip，不是失败）。
2. **负向对照**（照 `学习/_整理/selfcheck.py` 的做法：临时副本注入缺陷 + 断言注入生效 + 只看结论）：
   - 压缩稿把某个数字改坏 → 事实锚必须报错并点名段 id；
   - illustrate 段给一个畸形 mermaid / 带 `<script>` 的 SVG → 必须退回 keep + `figure_error` + 可见降级；
   - `events.jsonl` 里塞一条 `paragraph` 不存在的段 → 丢弃 + degraded，**不许写进文件**；
   - `overrides` 里的概念收到反向行为信号 → state 一个字符都不许变；
   - 局部重跑后 `fidelity.checked` 必须是 `false`（不许带上次的 `true`）。
3. **口径不变**：全缺省的 `ar rewrite` 行为、五个既有子命令与 `ar tools` 的参数/退出码语义不变；
   `ar rewrite --mode strict` 仍退出码 3。
4. **文档同步**：`README.md`（§2 命令、§3 产物、§4 端点、§6 退出码、§7 测试、§8 已知限制里
   与本批相关的条目）、`DESIGN.md`（§10.2 能力表、§10.4 落点表）、`web/README.md`。
   报告里逐条列出「改了哪一节、改成什么」。

---

## 12. 受影响既有用例（实施时必须同步，别让它们红着交）

E1：`test_rewrite.py` 的 `test_plan_decisions_covers_every_paragraph` /
`test_rewrite_keeps_paragraph_order_and_marks_deferred` / `test_rewrite_translates_implemented_actions_only` /
`test_rewrite_template_puts_every_varying_part_at_the_end…`（那句「compress / illustrate 本期不实现」）/
`test_run_rewrite_writes_three_artifacts`（硬编码 `rewrite-v3`）/
`test_cli_rewrite_writes_artifacts_and_check_passes`（`implemented_actions`）与 `FAKE_ACTIONS` / 假处理器；
`test_no_silent_degradation.py::test_p5_unknown_action_wording_matches_artifact`（拿 compress 当
「枚举内未实现」样本，**样本要换**）；`test_survey.py` 里断言那句提示词的用例；`test_tools.py` 的
工具数 / schema / 提示词逐字复用断言。

W：`tests/web/test_fixture_schema.py`（`deferred` 与 compress 绑定、动作与长度比一致性——
夹具现有 15 个 compress 段要重定）、`tests/web/test_reader_ui.py`
（`test_marker_tooltip_shows_action_and_why` 断言 p024 显示「未实现/保持」）、
`tests/web/test_banner_lifecycle.py`（`__arDebug` 字段白名单）、`samples/_build/make_fixture.py`。

S：`tests/test_profile.py`（键集断言——按 §7.3 把计数器放 concepts 内层就不受影响；`apply_rating`
的 confidence 下限 0.7 与行为演化的优先级要补新用例钉住）、`tests/test_server.py`
（`/api/doc` 键集断言不要动；未知路由 404 用例守新端点顺序）。

E2：`tests/test_cli.py`（无新参数时行为与退出码逐字不变、`--help` 列表；**另有 E1 交下来的 3 条红**：
`TOOL_NAMES` 缺 `compress_paragraph`、工具计数 `4 → 5`、提示词版本 `rewrite-v3 → rewrite-v4` 并补 `compress-v1`）、
`tests/test_rewrite.py`（`meta` 新增字段后不破坏既有逐键断言）、
`tests/test_no_silent_degradation.py` 的 P2 三条（退出码 6 按合并后状态重算，另加「重跑补齐后回 0」）、
`tests/test_explain_scope.py`（不改指纹口径；缓存键与失效行为不变）。

---

## 13. 实施记录（2026-10-13 收口）

四条都落地了，**全部走假模型 / 夹具 / 路由级假后端，一次真实模型调用都没发**：
`uv run pytest tests -q` = **523 passed / 0 failed**（复核 + 收尾后的最终数字；修复前 514）；`python -m pytest tests/web -q`（**系统 Python**，
用 `.venv` 会因 `%TEMP%` 不可写而 `EPERM mkdtemp`，那是跑法错误不是缺陷）= **132 passed + 1 skipped**；
`uv run ruff check src tests` = **7 条，逐条核过都是改动前就有的历史项**
（这里原先记的「10 条全是历史项、零新增」**是错的**：本批一度引入 3 条 —— 新建的
`tests/web/test_behavior_events.py` 一个 `F401`，以及同日问卷那一路两个未提交文件里的 `F401` / `E741`；
收尾时按「相对 HEAD 的那一行是不是新增行」同一把尺子查明并**已清零**）；
`tools/gen_status.py` 已重生成 `web/status.json`（后端 523 / 前端 132+1）。

分四路并行做的（每路只碰自己那列文件）：引擎路（`rewrite.py` / `prompts.py` / `tools.py` / `analyze.py`
+ 五提示词与第五个工具 `compress_paragraph`）、前端路（`web/**` + `samples/**` + `tests/web/**`，
含 5,575,485 B 的离线 mermaid 与懒加载）、回流路（新 `events.py` + `profile.applySignals` +
`POST /api/events`）、增量路（局部重跑三参数 + 合并器 + `POST /api/compress` + `ar feedback`）。

**本文件的三条勘误**（都已在正文里就地改正）：① §8「空 `illustration_block` 时与 v3 逐字相同」按字面做不到；
② §4.2 的 SVG「清洗后接着用」与验收第 2 条冲突，改为白名单外一律拒绝；
③ §5.2 的成功响应 `degraded` 不必为空（事件写失败要在成功路径可见）。
**两条裁决**：`/api/compress` 成功响应的提示文案键名冻结为 `hint`；行为事件白名单**不含**卡片开关。

**本批没做到的**：`rewrite-v4` / `compress-v1` **没打真网关**（提示词是否真好用、真模型肯不肯按
`figure` 形状输出、压缩质量如何，都还没有真机证据）；离线 mermaid 的渲染**有一条真库用例**
（`tests/web/test_figures.py` 里不 stub 的那条：真库由本机静态服务提供，断言渲染出的 `svg` 的 `class`
含 `flowchart`，假库做不出这个 class），但只覆盖夹具里那条简单 flowchart，
`sequenceDiagram` / `mindmap` 等语法与长图性能未验；压缩缓存键不含产物状态（整篇重跑后旧条目仍可能命中，
见 README §8 新增条）。

### 13.1 复核与修复（2026-10-13 晚）

另派一路**只读**对抗性复核（不许改文件、不许提交、零模型调用）。结论：**无阻断问题**，
两套测试数字与本次记录一致，六类负向对照（改坏数字 / 畸形图 / 幽灵段事件 / overrides 反向信号 /
重跑后 `fidelity` 必须重置 / 退出码 6 补齐回 0）全部亲手注入并复现，另跑 91 条自写断言全绿；
Playwright 探针验到**首屏 16 个请求里 `mermaid.min.js` 0 个**（真懒加载），
`page.close` / `context.close` / 导航离开三条路径各收到一批 `/api/events`。

它揪出的 6 项**已全部修掉**（后端随之 514 → 522 条）：

| # | 问题 | 修法 |
|---|---|---|
| F1 | `/api/compress` 在「本段零事实锚」时校验**空跑且不留痕**，任何更短文本都过关 | 不拒绝（没有事实可毁），但成功响应 `degraded` 里加一条「本段没有可比对的事实锚，本次压缩未经事实锚校验」；有锚点段照旧真校验、不一致仍拒写 |
| F2 | `ar feedback --apply` **不幂等**：重复执行重复折算同一批事件，会把 state 推过阈值 | 新增读者级 `profiles/<reader>/feedback.json`（最近 500 条已处理事件 id + `last_at` 水位线）；只折算未处理事件；缺失/损坏按「全部未处理」并给可见降级 |
| F3 | README 低报自己的证据：称真 mermaid 库渲染「没有实测记录」 | 改为「有一条不 stub 的用例覆盖夹具那条 flowchart（断言 svg class 含 `flowchart`，假库做不出）」 |
| F4 | `prompts.py` / `tools.py` 注释仍写「空块与 v3 逐字相同」 | 按 §8 勘误改成「空块 == 缺省 == 零字符插入」 |
| F5 | `changes.json` 的 `meta` 没有 `paragraph_count`（`decisions.json` 有） | 整篇与合并两条路径都追加该字段 |
| F6 | CLI `ar rewrite` 的画像行前缀被顺手删掉（未授权） | 逐字还原 `画像：` 前缀 |

**复核者没能验的三处**（与上一条「本批没做到的」合并看）：真网关上的提示词质量、其他 mermaid 语法与长图性能、
浏览器被强杀时事件是否丢。另有一条**既有 flaky**：前端「滚动 + Esc 收起浮条」那条用例在整套负载下偶发超时
（单跑通过，与本批改动无关，下一轮再看要不要加等待条件）。

### 13.2 收尾（2026-10-13 晚）

- 只读验证者逐项确认 F1–F6 成立（自写 80 条断言、含两条负向注入），并发现本批**确实**引入过
  3 条 ruff 错误（被「历史项」这个宽口径掩盖）——已清零，见上文订正后的那条记账。
- 盘上 `data/capital-v1-ch01/` 的三份产物早于本批改动（`changes.json` 的 meta 还没有 `paragraph_count`，
  三份都没有更早那次改造的 `attempts` / `retry_account`）。README §3 已如实注明：**键不存在 ≠ 值不对**，
  要让它出现只能重跑 `ar rewrite`，而本批硬约束禁止真实模型调用，**没有重生成**。
- `/api/compress` 的**缓存命中路径**补了一条钉住用例（命中时仍带回「本段没有可比对的事实锚」那句说明），
  并做了两次负向注入（不存 `degraded` / 命中时丢掉 `degraded`）确认这条用例真的会红。
- **本文件（含 §13.1 的表格与上文两处订正）是这批唯一的收口记录**；README / DESIGN / `web/README.md`
  另有面向使用者的对应章节。

---

## 14. 挂起的后续调整（用户 2026-10-13 读过收口说明后提出）

1. **事实锚口径松绑**：现在三类锚点按「数量 + 顺序」逐段严格比对，删一个数字就退出码 4；
   但「举例数据只为给出定性结论」「引文是噪音、改成想表达的意思」这两类需求是正当的。
   方向定为**可申报 + 可见**：模型逐条申报「本段删了哪几个数字 / 把哪条引文改成了转述」，
   产物 / 页面 / 命令行都显示；**没申报就删，仍按现在的规则报错**（这条网是「自由重述」模式下
   唯一能自动抓事实漂移的东西，不许静默失效）。落地形态倾向做成**读者画像里的一条偏好**
   （要结论 → 允许丢举例数据；要素材 → 全留），而不是全局开关——画像一变只重跑受影响的段，
   这条链路本批刚打通。

   > **状态：已实现（2026-10-14）。** 落点是本节写的那三条，实现形态如下（与上面的方向一致，
   > 没有改动任何既有契约字段）：
   >
   > | 环节 | 实现 |
   > |---|---|
   > | 申报 | `changes[i].fact_declarations = {numbers, quotes, books, note?}`（**只在真的申报了东西时出现**；不进 `decisions.json`，与 `figure` 同口径） |
   > | 口径 | 读者画像里那条偏好 `pref_fact_anchor`，取值 `keep_all`（严，申报了也不放行）/ `allow_declared_drops`（松，**已申报**的缺失放行）；唯一来源 `src/ar/fidelity.py::FACT_ANCHOR_POLICIES` |
   > | 校验 | `ar.fidelity.compare_blocks_detailed(declarations=…, policy=…)`：**只放宽「少了什么」**；多出来的锚点、顺序调换、申报与实测对不上，任何口径都不放行（那才是真的要抓的事实漂移） |
   > | 三处可见 | 产物：`changes[i].fact_declarations` + `fidelity.fact_anchor_policy` + `fidelity.waived[]` / `fidelity.over_declared[]`（+ 两份 meta 的 `fact_anchor_policy` / `_source`）；命令行：`ar check` 打口径、逐条放行与逐条多申报，`ar rewrite` 打口径与每段申报；页面：卡片的「本段申报（事实锚）」+ 放行 / 未放行 / **多申报**徽标 + 悬停那一行（在线压缩之后同屏可见，见下面那两行的订正） |
   > | 偏好写入 | **唯一创建路径** `ar.profile.set_preference` + `POST /api/profile/preference`（行为信号那一侧仍然绝不自动新建偏好）；`ar check` 追加可选参数 `--reader`（**缺省 `local-default`**，与 `rewrite` 的缺省一致；CLI 恒按它判，不回退产物 `meta.reader` —— 那条回退只对直调 `check_fidelity(reader=None)` 的调用方生效） |
   > | 两条路径同一套口径（2026-10-14 收尾订正） | **在线压缩也接上申报通道**：提示词升 `compress-v2`（同一段口径文案 + 规则 6 + JSON 示例那一格），工具入参加 `fact_anchor_policy`、出参加 `fact_declarations`，写盘前改用**同一个** `compare_blocks_detailed` 并按**同一处**口径解析（`resolve_fact_anchor_policy`）。成功后：申报落 `changes[i].fact_declarations`，成功响应 13 → **17 键**（追加 `fact_declarations` / `waived[]` / `over_declared[]` / `fact_anchor_policy`，缓存命中时跟着回），页面当场渲染「本段申报」与徽标。**底线一个字没松**：没申报的缺失 / 多出来的锚点 / 顺序调换 / 申报与实测对不上，两条路径都拒写 |
   > | 多申报可见（2026-10-14 收尾） | **锚点一个没少却申报放弃了某项**（`same_facts(...) → continue` 曾把这种乱申报整个跳过）：结论与退出码都不变，但逐条进 `fidelity.over_declared[]`、CLI 打 `⚠ 事实锚多申报：…`、页面挂「本段一个锚点都没少：模型多申报了」徽标；放行那一行里混着「本来就没丢」的申报项也单列一条 |
   > | 测试 | `tests/test_fact_anchor_declarations.py`（24 条：退出码 4 的底线、松口径放行与三处可见、严 / 松对照、四条不放行的边界、偏好读写与端点、提示词固定块与 `{rule}` 模板、多申报可见 ×2、端到端申报落盘）+ `tests/test_compress_api.py`（+7 条在线压缩的申报通道：放行 / 未申报拒写 / 严口径不放行 / 申报对不上 / 多出锚点 / 多申报可见 / 提示词带口径块）+ `tests/web/test_fact_declarations_ui.py`（6 条前端可见面）+ 既有 543 条与前端 136 条全绿 ⇒ **收尾后后端 556 条 / 前端 139 + 3 skipped** |
   >
   > 仍未做：页面上直接改这条偏好的 UI（写入要靠端点或手改 `profile.yaml`）、
   > 「申报有没有被模型滥用」（现有网只管「没申报就报错」，收尾补的 `over_declared` 只抓
   > 「申报了却没丢」这一种反向离谱）、以及 `rewrite-v5` / `compress-v2` 的**真机验证**
   > （零真实模型调用这条硬约束下没法跑：真模型肯不肯逐条申报，没有实测记录）。
2. **行为回流规则要调整**（方向未定）：§7.3 那套阈值 / 衰减 / 权重 / 「一次只升降一档」
   全是实现时自定的数值，没有实测校准。现行值：权重 `explain −1.0` / 选片求助 `−0.5` /
   `compress −0.75` / `读完且无求助 +0.25`；阈值 `|score| ≥ 1.2` 且同向证据 ≥2 条；14 天半衰期；
   未达阈值只累加 `score` / `evidence`、`state` 与 `confidence` 一个字不动；`overrides` 永不覆盖。

两条都记在工作区根 `TODO.md` 第 5 条（与原有的「模型措辞」问题合并为同一条）。
