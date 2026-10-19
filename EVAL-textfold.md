# textfold 对照评估（TODO 4.③）

对一个把论文折成五档详略的开源项目做对照，回答一个问题：**它有哪些做法值得搬进 adaptive-reader 的
「段锚点 + 逐段改写」链路，哪些搬不过来。** 结论在 §5 的判定表。

- 评估对象：[chenxiachan/textfold](https://github.com/chenxiachan/textfold)，快照 `2cd77e0`（0.10.7，
  2026-10-15 最后一次推送；2026-10-18 查到 349 ★ / 29 fork）。Apache-2.0，Python 3.9+，依赖一长串
  （bs4 / lxml / trafilatura / mammoth / pypdfium2…），但**折叠链路本身**不依赖这些：切分、提示词、契约、
  落盘四块是纯 Python。
- 取法：`git clone --depth 1 https://gh-proxy.com/https://github.com/chenxiachan/textfold`
  （本机 hosts 把 github 域名指到 127.0.0.1，直连被拒）。克隆落在临时目录，看完即删，没进工作区。
- 范围：折叠链路（切分 → 生成 → 契约 → 落盘 → 取用）与它给 agent 的接口。PDF 引擎、Zotero、
  桌面打包、翻译不在内。
- 结论：**值得借两件事——句级证据锚（§5 A）与正文前的目录（§5 D）**。五级梯子搬不过来
  （§5 F）；无模型兜底与「写时就折叠」记着，眼下没有落地条件（§5 B / C）。

## 1. 它做了什么

一句话：**把同一份文本预先折成五档详略，每档标明「是谁的话」，每一行短文字都能按句索回原文。**

| 档 | 每段变成 | 谁写的 |
|---|---|---|
| 1 Full | 原段落 | 作者 |
| 2 Brief | 段内 1–3 句话，一字不改 | 作者 |
| 3 Key | 一句要点 + 一句理由 | 模型（依据 Brief 那几句） |
| 4 Takeaway | 一句（≤14 词） | 模型 |
| 5 Topic | 一个名词短语（≤3 词，优先取 Takeaway 里的原词） | 模型 |

配套的契约写在 [docs/format.md](https://github.com/chenxiachan/textfold/blob/2cd77e0/docs/format.md)：
**两类文字不混**——`full` / `brief` 是作者原话，`key` / `takeaway` / `topic` 是模型写的，
后者每行都带 `evidence`（它依据的原句下标）。

它自己说有三个用途：人读（章节地图 → 逐级缩放）、折 agent 的回答（Claude Code / Pi 插件，不用第二个模型）、
给脚本按层取（`textfold get --level key --section 3`）。

## 2. 链路拆解

### 2.1 单位：section → unit → sentence

- **section** = 标题 + 其下的段落 / 列表项 / 题注，是**一次模型调用的批次**（`ladder.build`，
  [ladder.py:251](https://github.com/chenxiachan/textfold/blob/2cd77e0/textfold/ladder.py#L251)）。
- **unit** = 段落、列表项、图/表题注（`docs/format.md` 的「A unit」）。和我们的粒度一样。
- **sentence**：解析时就把 unit 切成句（`tokens.split_sentences`，
  [tokens.py:48](https://github.com/chenxiachan/textfold/blob/2cd77e0/textfold/tokens.py#L48)），
  句子有下标，`evidence` 就是这些下标。**这是我们没有的一层。**
- **token**：词、标点、公式/引文/交叉引用原子；只为排版与动画服务（`m` 索引让词在位移动画里“滑”而不是“换”）。

### 2.2 生成：一个 section 一次调用

提示词给模型四样东西：标题、摘要（作为全局上下文）、大纲（▶ 标出当前节）、该节每个 unit 的**编号句**。
输出的 JSON 每 unit 一条：`role`（8 类：context / gap / claim / method / setup / result / analysis /
limitation）、`takeaway`、`topic`、`reason`、`evidence`。

- `evidence` 规则（[ladder.py:107](https://github.com/chenxiachan/textfold/blob/2cd77e0/textfold/ladder.py#L107)）：
  1–2 句（unit 有 7 句以上才允许 3 句），升序，“能支撑要点的原句”。
- 词数预算是**软**的：Takeaway ≤14 词、理由 ≤22 词、Topic ≤3 词；超了只记一条 warning，不拒绝。
- 4 并发；**一个 section 失败只毁它自己**，退到“本地梯子”，其余照常，失败与超长都打日志。
- 缓存键 = `sha256(system, prompt, schema, model[, effort])`（[llm.py:64](https://github.com/chenxiachan/textfold/blob/2cd77e0/textfold/llm.py#L64)）；
  `force` 跳过缓存 = 重新采样。

### 2.3 契约靠「构造关系」，不靠事后比对

- `brief` **就是** `evidence` 那几句（[fold.py:39](https://github.com/chenxiachan/textfold/blob/2cd77e0/textfold/fold.py#L39)），
  所以它逐字，不需要校验。
- `takeaway` 是 `key` 的前 `tn` 个 token、`topic` 用 `locate()` 在 `takeaway` 里找逐字串
  （[ladder.py:222](https://github.com/chenxiachan/textfold/blob/2cd77e0/textfold/ladder.py#L222)）；
  找不到但短语不超过 5 词就保留模型自己的写法。**契约有回退，不是硬失败。**
- 全篇**没有保真检查器**。Brief 忠实是因为它抄的原句；Key / Takeaway 是自由生成，数字或因果写错了，
  机器不会发现。它靠“证据句让读者能自己翻回去”来兜，不靠比对。

### 2.4 两条不用模型的路

- **本地梯子**（[ladder.py:212](https://github.com/chenxiachan/textfold/blob/2cd77e0/textfold/ladder.py#L212)）：
  模型没回 → Brief 用第一句、Takeaway 用前 14 个词、Topic 用前 3 个词。整篇模型不可用时，文档仍可读、
  可缩放，只是短档全是作者原话。
- **插件折叠**（[hooks/fold.ts:154](https://github.com/chenxiachan/textfold/blob/2cd77e0/plugins/textfold/hooks/fold.ts#L154)）：
  纯截断，零模型调用。代价是**写的时候就得按可折的格式写**——每条回答先给答案、每段首句自带要点；
  不足 80 词、或折完省不到 25% 的回答原样显示。

### 2.5 落盘与取用

- `papers/<id>/layers/en.json` + `tr-<lang>.json`，翻译层记着它依据的英文版号，英文重新生成后标过期
  （[store.py:4](https://github.com/chenxiachan/textfold/blob/2cd77e0/textfold/store.py#L4)）。
  阅读器**由层组装，零模型调用**——同一篇每次打开都一样。
- `textfold get <文档>` 先给地图（每节主题），`--level key --section 3` 只给这一节的要点，
  `--json` 给整份格式（[fold.py:93](https://github.com/chenxiachan/textfold/blob/2cd77e0/textfold/fold.py#L93)）。
  这是给 agent 的**渐进披露**：先花小钱看地图，再决定把上下文花在哪一节。
- `provenance` 里标 `ai_generated` / `model` / `levels_made`，按 EU AI Act 50(2) 的口径做机器可读标记。

## 3. 与 adaptive-reader 逐项对照

| 维度 | textfold | adaptive-reader 现状 |
|---|---|---|
| 工作单位 | 段落 / 列表项 / 题注；**段内有句下标** | 只有段落（`ingest.py:72` 按空行切，标题行也是段） |
| 章节结构 | section = 标题 + units，有层级，可出地图 | 无（标题只是普通段落） |
| 模型调用 | 一 section 一次，返回该节全部 unit 的五档 | 按字符预算分批（默认 1100 字符/批，`rewrite.py:1092`），首批串行预热、其余 4 并发 |
| 输出形态 | 每段五档**单调**详略 | 每段一个动作 + 一版改写（expand / compress / keep / illustrate） |
| 适配对象 | 没有读者模型，同一篇对所有人一样 | 读者画像 + 背景问卷 + 行为回流，同一材料对不同读者不同 |
| 保真手段 | 结构约束（Brief 是原句、Topic 取自 Takeaway）+ 证据句；**无检查器** | 事后比对数字 / 引文 / 书名，数量与顺序都查（`fidelity.py:339`）+ 申报放行 |
| 失败降级 | 该 section 用本地梯子（首句），全篇仍可读 | 该批退回 keep + `meta.model_errors` + 退出码 6；`--retry-failed` 可补 |
| 取用接口 | CLI 按层按节给 Markdown / JSON | HTTP 整篇 / 单段 / 讲解；`ar tools` 是**内部**模型工具层，不是对外读取接口 |
| 角色与关系 | 8 类 role + 4 类论证链接（supports / builds_on / qualifies / explains） | 无 |
| 溯源 | 读者可见的 `ai_generated` / `model` | `meta.model` / `prompt_version`（给运维，不给读者） |

## 4. 哪些它没有、别指望它替你解决

- **事实锚**。它没有任何自动比对；Key / Takeaway 里的数字、引文、书名错了没人拦。我们那套
  「数量 + 顺序」的比对和申报放行口径，它整块没有。反过来，它的 Brief 逐字是因为抄原句——
  我们 `keep` / `illustrate` 也是逐字节原文，这一点两边等价。
- **读者模型**。它同一篇对所有人一样。
- **失败可见与可恢复**。退出码 6、`meta.model_errors`、`unplanned_paragraphs`、`--retry-failed`、
  `rerun_history` 这一套它没有；它失败只打一行日志、退本地梯子。
- **分批缓存命中可观测**。`batch_usage[]` 能看出“预热到底有没有生效”，它只记命中与否与花费，没有 token 账。
- **提示词版本化**。它把 prompt 直接拼在代码里，没有 `rewrite-v6` 这种版本号，改一个字缓存键就变，
  但人看不出“这版产物是哪版提示词写的”。

## 5. 可借鉴项

| 项 | 判定 | 一句话理由 |
|---|---|---|
| **A 句级证据锚** | **建议做** | 唯一直接补上“改写依据只能指到段、指不到句”的结构缺口 |
| B 失败时的原句摘兜底 | 待定 | 和事实锚的严口径冲突，先要拍“骨架申报算不算申报” |
| C 段首句即要点（写时约定） | 待定 | 便宜，但只有真机实测才能判断会不会把改写写模板化 |
| D 章节地图 | **采纳（待实现）** | 形态定为正文前插入目录；条目来源待拍板（TODO 7） |
| E 对外按层取用 | 不做（无消费者） | `ar tools` 是内部工具层，外面没有读这份产物的程序 |
| F 五级梯子 / 词位滑动 / 多源解析 | 不借 | 与“按画像逐段改写”不是同一个问题 |

### A. 句级证据锚（建议做）

**借什么。** 段内先切句、句带下标；模型在 `changes[i]` 里报「这一段依据原文第几句」。

**为什么值。** 现在 `why` 是散文，事实锚只能报「这一段少了 1696」，原文抽屉只能整段高亮。
有了句下标：

- 段落标记悬停可以写「依据第 2 句：『……』」，而不是一段概括；
- 抽屉只高亮那一句，读者不用在一整段里找；
- `ar check` 不通过时能定位到句，而不是“这一段有问题”；
- 讲解卡可以按句引用，模型不容易顺着整段编。

**它证明不了什么（必须写进字段文档）。** 句下标只证明模型**声称**用了哪句，不证明改写忠于那句；
忠于与否仍由事实锚判、由人判。**别把它当成第二张保真网**，否则下一个人会把“有证据句”读成“已核验”。

**规模。** 中。全是追加字段，`text_hash` 与段锚点口径一字不改。

### B. 失败时的原句摘兜底（待定）

textfold 在模型整篇不可用时，用“首句”给出短档；我们的失败降级是 `keep`（原文）——对
「太啰嗦」的读者来说，这等于什么都没做。

卡点在口径，不在实现：事实锚的严口径是“少一个锚点、没申报就失败”。骨架自己摘句**必然**丢锚点，
那么谁有权申报？给骨架申报权 = 改契约（现在申报是模型的声明）；只对
`allow_declared_drops` 的读者生效 = 严口径读者拿不到这个兜底。

更值得考虑的形状是把它做成**枚举内的动作**（`excerpt`：模型只挑句下标，一个字都不生成）：
忠实由构造保证（改写稿是原文的子序列），贵在要新增第五个动作、一套判据和页面徽标。
判定：先不做，等 A 落地、真的有人抱怨「压缩失败等于没压」再谈。

### C. 段首句即要点（待定）

它的插件能零成本折叠，靠的就是写的时候“先给答案、每段首句自带要点”。对我们只有扫读价值，
且 expand 段需要铺垫，不能强制。代价是一条提示词规则 + `rewrite-v7`；风险是改写变模板化。
当前没有真机实测的批次，没法 A/B。挂起，等下次有实测机会一起验。

### D. 章节地图 → 正文前的目录（采纳，待实现）

2026-10-18 改判：要做。形态定为**阅读器正文之前插入一个目录**（章节地图就是那张地图）。

渲染与联动不难：条目点一下滚到某段，与现有 `data-paragraph="pNNN"` 是同一套。
难的是**目录从哪来**——样例材料只有一个 `#` 标题段（`p001`），正文 22 段没有小标题，
按 Markdown 标题做目录只有一条，等于没有。

| 来源 | 做法 | 代价 |
|---|---|---|
| 源文本标题 | 抽 `#`–`######` 行与短标题段 | 对这份材料几乎空目录 |
| 模型分段 | 一次调用把全篇切成 3–8 节，每节一句小标题 | 新产物 + 提示词版本 + 契约 + 测试 |
| 已有概念清单 | 目录列 `concepts[].name`，点条目跳到首段 | 零新调用，但它是概念表，不是章节 |

不管选哪条，“节”都要有稳定锚点（首段 id）：重摄取后按段 id 重新对齐，对不上的条目
**可见点名**，照 `anchor_diff_warnings` 的口径；目录为空或只有一节时不显示，也要能说清为什么。

判定：**采纳**，条目来源待拍板（`TODO.md` 第 7 条）。倾向**模型分段**：只有它对无标题材料也给得出
真目录，代价是比另两条多一次调用。

### E. 对外按层取用（不做）

`textfold get --level` 服务的是外部 agent。我们的 `ar tools` 是内部模型工具层（把“拼 messages →
调模型 → 解析”收成一步），没有外部消费者。要做也得先有那个消费者，否则就是给无人使用的接口写契约。

### F. 不借的三样

- **五级梯子是单调压缩，我们的 expand 是反方向。** 难段要变长，硬套会让“层级”在难段上倒退，
  读者看到的是“越读越短”，与项目目标相反。
- **词位滑动**（LCS + `m` 索引，[tokens.py:90](https://github.com/chenxiachan/textfold/blob/2cd77e0/textfold/tokens.py#L90)）
  是渲染层的事，收益全在观感；我们没有这套动画设施。
- **多源解析 / 翻译**是另一个问题域（它约一半的代码在解析 PDF、Word、JATS、网页）。

## 6. 若做 A：改动清单

| 文件 | 改什么 | 纪律 |
|---|---|---|
| `src/ar/ingest.py` | 新增 `split_sentences()`；每段追加 `sentences: [{start, end}]`，偏移基于 `paragraphs[].text` **原样**，不复制文本 | `text_hash` 与段 id 口径不动；纯函数先写测试 |
| `src/ar/anchors.py` | `verify_source` 追加“句子拼回来等于该段”的校验 | 新字段也要能被检出漂移，否则多一个静默面 |
| `src/ar/prompts.py` | `rewrite-v7`：JSON 每段加 `evidence: [int]`（升序、1–2 个、必须是源句下标） | 升版本号；固定块尽量不动，保住前缀缓存 |
| `src/ar/rewrite.py` | 越界 / 非升序 / 该给不给 → `meta.degraded` 点名段，**不阻断写盘**；写 `changes[i].evidence`，**不进** `decisions.json` | 照 `figure` / `fact_declarations` 的“为真才出现”先例；只对 `TEXT_ACTIONS`（expand / compress）写，keep / illustrate 不写 |
| `web/js/schema.js`、`web/js/main.js` | 标记悬停多一行「依据第 N 句」；抽屉高亮该句 | 判据只认结构化字段，不拿正则猜 `why.note` |
| `tests/` | 切句单测（CRLF、全角空格、`1.2`、`et al.`、`“…”`、`。` 后无空格）；越界下标的降级；夹具 schema | 负向对照：注入越界下标必须可见 |
| `README.md` §3、`web/README.md`、`DESIGN.md` §4 | 新字段与 UI 落点 | 变更后同步描述性文档 |

## 7. 复核方式

```bash
git clone --depth 1 https://gh-proxy.com/https://github.com/chenxiachan/textfold <临时目录>/textfold
```

| 看什么 | 位置 |
|---|---|
| 五档与“谁的话” | `textfold/fold.py:13-14` |
| Brief = 证据句 | `textfold/fold.py:39` |
| 证据规则与风格约束 | `textfold/ladder.py:107`、`:111` |
| 本地梯子 | `textfold/ladder.py:212` |
| Topic 逐字回退 | `textfold/ladder.py:222` |
| 词位对齐（动画用） | `textfold/tokens.py:90` |
| 缓存键 | `textfold/llm.py:64` |
| 分层落盘 | `textfold/store.py:4-5` |
| 按层取用 | `textfold/fold.py:93` |
| 格式契约 | `docs/format.md` |
| 零模型插件 | `plugins/textfold/hooks/fold.ts:71`、`:154` |

## 8. 本次没做

- 没装、没跑它的生成：要连模型，而评估的是机制，跑一遍只多一份样例。
- 没改 adaptive-reader 的任何代码、产物与夹具。
- D 只记了形态与三条备选来源，目录本身没实现。
- 临时克隆看完即删；本篇之外没有留下文件。
