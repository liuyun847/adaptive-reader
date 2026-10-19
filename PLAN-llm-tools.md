# PLAN · 模型调用工具化与重试改造

> 状态：**阶段 1 已实施并验收**（2026-10-13）；**阶段 2（四个调用点迁移）已完成并验收**（2026-10-13，
> 含 §8.4 实施记录、§8.5 复核修复与 §8.6 技术债清理；实测后端 **394 passed** / 前端 **98 passed + 1 skipped**
> ——**都是该批验收时（2026-10-13）的数字，属历史值**，当前计数见 [README.md](README.md) §7）。
> 唯一没做的是 §6.5 真实网关冒烟（用户 2026-10-13 明确豁免）。
> 决策已确认：① 重试默认值照 DSH 内置重试（5 次 / 500→10000 ms）；② 额外新增只读的 `ar tools` 子命令。
> 本文是实施规格书：接口签名与字段名按本文写死，实现方不得自行改名。
> **实施中的裁决与偏离见 §8**；正文与 §8 冲突处，一律以 §8 为准（例如 §3.1 规则 2 的重试判据已按 §8 D1 修正）。

## 0. 一句话

把现在散在四个业务模块里的模型调用，收敛成「**四个具名工具 + 一个统一调用层**」，重试策略照 DSH 内置重试实现，并把每次尝试记账落进产物。

## 1. 现状与要修的问题

四个调用点（全仓仅此四处会调模型）：

| 调用点 | 文件 | 粒度 |
|---|---|---|
| 概念清单抽取 | `src/ar/analyze.py` | 整篇 1 次请求 |
| 逐段决策 + 改写 | `src/ar/rewrite.py` | 按字符切批，每批 1 次 |
| 讲解卡片 | `src/ar/explain.py` | 1 次/点击 |
| 读前背景追问 | `src/ar/survey.py` | 1 次/点击 |

传输层 `src/ar/model.py`（httpx + httpx-sse 直连 OpenAI 兼容端点）现存六个问题：

1. 重试**不分错误码**：捕获 `(httpx.HTTPError, ModelCallFailed)` 一律重试，401/400 这类确定性错误也白花一次调用。
2. **无退避**：失败立即重发，没有 sleep。
3. **不解析上游 `Retry-After`**。
4. 每次尝试**新建 `httpx.AsyncClient`**，无连接复用。
5. **无错误码归一**：失败只有一段异常文本，调用方无从判断「该不该再试」。
6. **无逐次尝试记账**：产物里只有汇总的请求数与 token，看不到「第几次尝试失败、等了多久」。

## 2. 范本（已核实的本机事实，勿再重查）

策略形状与默认值 —— `~\.dsh\profiles\node_modules\@deepseek-ai\dsh-llm\lib\types\retry-policy.js`：

- `mode`: `normal`（有界）| `always`（无上限，本项目不实现，仅解析后拒绝或忽略）
- `maxRetries` 默认 **5**（即最多 6 次尝试）
- `retryableCodes` 默认 `[EMPTY_RESPONSE, RATE_LIMIT, SERVER, TIMEOUT, TRANSPORT]`
- `backoff`: `initialDelayMs` **500** / `maxDelayMs` **10000** / `jitterRatio` **0.1**

执行器 —— `~\.dsh\profiles\node_modules\@deepseek-ai\dsh-llm-retry\lib\index.js`：

- 退避 = 指数 × 对称抖动，再封顶：
  `exponent = min(retry - 1, 1024)`；`exponential = min(initial * 2 ** exponent, maxDelay)`；
  `jitter = 1 - ratio + 2 * ratio * random()`；`delay = min(exponential * jitter, maxDelay)`
- `Retry-After`（毫秒）在 `maxDelayMs` 界内 → **优先用它**，不用本地退避；**超界 → normal 模式直接放弃重试**。
- 失败是**返回值/终止对象**，不是抛出；失败的那次尝试不进入历史。
- 两条纪律：**先记账再等待**；**可重试用白名单、可回退用黑名单**（本项目只有一条路由，不涉及回退）。

工具形态参照 —— DSH 用 `defineTool({ name, description, parameters(JSON Schema), output, execute })` 注册，schema 可被外部列举。本项目只借形态，不引入任何 JS 依赖。

**范本自身的已知盲点（本项目要修掉）**：宿主在「流没有终止事件就被掐断」时抛的 `STREAM_CLOSED` 不在默认可重试集合里 ⇒ 不重试。本项目把「流意外提前结束」一律归到可重试的 `TRANSPORT`，不留这个盲点。

## 3. 目标设计

### 3.1 新模块 `src/ar/llm.py` —— 统一调用层

数据结构一律 `@dataclass(frozen=True)`，字段名按此写死：

```python
DEFAULT_RETRYABLE_CODES = ("EMPTY_RESPONSE", "RATE_LIMIT", "SERVER", "TIMEOUT", "TRANSPORT")
# 显式永不重试（即使 always 模式也不重试）
NEVER_RETRY_CODES = ("AUTH", "INVALID_REQUEST", "QUOTA", "NOT_CONFIGURED")

@dataclass(frozen=True)
class RetryPolicy:
    mode: str = "normal"                                   # normal | always
    max_retries: int = 5
    retryable_codes: tuple[str, ...] = DEFAULT_RETRYABLE_CODES
    initial_delay_ms: int = 500
    max_delay_ms: int = 10_000
    jitter_ratio: float = 0.1

@dataclass(frozen=True)
class LlmRequest:
    tool: str                          # 工具名，用于记账
    messages: tuple[dict[str, str], ...]
    temperature: float | None = None
    max_tokens: int | None = None
    prompt_version: str | None = None

@dataclass(frozen=True)
class LlmFailure:
    code: str                          # §3.2 的码之一
    message: str                       # 已过 redact()
    status: int | None = None
    retry_after_ms: int | None = None

@dataclass(frozen=True)
class LlmAttempt:
    try_no: int                        # 第几次尝试，1 起
    elapsed_ms: int
    code: str | None                   # 成功为 None
    delay_ms: int | None               # 这次失败后等待多久；不重试为 None
    retry_after_used: bool = False      # 这个 delay 是否来自上游 Retry-After

@dataclass(frozen=True)
class LlmResult:
    ok: bool
    text: str = ""
    usage: dict | None = None
    model: str | None = None
    failure: LlmFailure | None = None
    attempts: tuple[LlmAttempt, ...] = ()
    degraded_params: tuple[str, ...] = ()   # 例如 stream_options 被拒后关闭
```

对外函数（同步/异步成对，同步包装沿用现有 `asyncio.run` 风格）：

```python
def call(request: LlmRequest, *, client: ModelClient, policy: RetryPolicy) -> LlmResult
async def acall(request: LlmRequest, *, client: ModelClient, policy: RetryPolicy) -> LlmResult
def compute_retry_delay(policy: RetryPolicy, retry: int, random_value: float) -> int
def resolve_retry_policy(raw: dict | None) -> RetryPolicy      # 解析配置，非法值报错
```

行为规则：

1. **失败不抛**（`NOT_CONFIGURED` 除外，它在发请求前就返回一个 `ok=False` 的结果，也不重试）。
2. 重试判据：`ok == False` 且 `failure.code in policy.retryable_codes` 且 `code not in NEVER_RETRY_CODES` 且 重试次数未用尽。`always` 模式忽略白名单但仍然不重试 `NEVER_RETRY_CODES`。
3. `Retry-After`：`failure.retry_after_ms` 存在且 `<= max_delay_ms` → 用它（`retry_after_used=True`）；`> max_delay_ms` → `normal` 模式放弃重试。
4. 每次尝试都**从原始 `LlmRequest` 重建请求**，不带着上一次的残留。
5. **先记账再等待**：`LlmAttempt` 在 sleep 之前就生成。
6. 注入缝（便于测试钉死）：`random_value`（或 `rng` 可调用）、`sleep` 可调用、`clock` 可调用，默认走标准库；测试里传假实现。
7. 账目最终由调用方写进产物 `meta.attempts`（结构：`[{tool, try, elapsed_ms, code, delay_ms, retry_after_used}]`，整次调用一条汇总 `{tool, requests, retries, codes}`）。

### 3.2 错误码归一（实现在 `src/ar/model.py`）

| 触发 | 码 | 可重试 |
|---|---|---|
| HTTP 401 / 403 | `AUTH` | 否 |
| HTTP 400 / 413 / 422 | `INVALID_REQUEST` | 否 |
| HTTP 429 | `RATE_LIMIT` | 是 |
| HTTP 5xx | `SERVER` | 是 |
| 读超时 / 连接超时（`httpx.TimeoutException`） | `TIMEOUT` | 是 |
| 连接失败、协议错误、**流提前结束（无 `[DONE]` 且无 finish）** | `TRANSPORT` | 是 |
| 流内 `error` 字段 | 有 status 按上表，否则 `SERVER` | 视码 |
| 收完但正文为空 | `EMPTY_RESPONSE` | 是 |
| 配额耗尽类文案（网关明确报额度用尽） | `QUOTA` | 否 |
| 未配置密钥 | `NOT_CONFIGURED` | 否 |
| 其它 | `UNKNOWN` | 否 |

`UNKNOWN` 不重试是**刻意**的：范本给的经验是「拿不到码宁可不动」（`adapter-failure.js` 把不可识别的错误归 `UNKNOWN` 且它不在白名单里）。

`src/ar/model.py` 保留职责：拼 URL、发请求、解析 SSE、拼增量、`stream_options` 被拒自动降级（保留现状）、`redact()`。改动点：把重试循环**移出** `model.py` 交给 `llm.py`；`_request_once` 返回 `LlmResult` 而非抛异常；新增错误码归一。

### 3.3 新模块 `src/ar/tools.py` —— 工具注册表与四个工具

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict          # 入参 JSON Schema
    output: dict              # 出参 JSON Schema
    prompt_version: str
    execute: Callable[[dict], dict]     # 同步；内部走 llm.call
```

注册表 API：`register(spec)` / `get(name)` / `all_tools()` / `schemas()`（返回可 JSON 序列化的 `[{name, description, parameters, output, prompt_version}]`）。

四个工具（名称写死）：

| name | description（英文一句，供 schema 展示） | 入参 | 出参 |
|---|---|---|---|
| `analyze_concepts` | Extract the concept inventory of a whole document. | `{paragraphs:[{id,text}], max_concepts?:int}` | `{concepts:[{id,name,surface,prereq[],difficulty_hint,paragraphs[],domain,concept_key}], illustration_candidates:[{paragraph,kind,why}]}` |
| `rewrite_batch` | Decide and rewrite a batch of paragraphs for one reader. | `{paragraphs:[{id,text}], start:int, end:int, profile_block:str, concept_block:str, background_sketch:str}` | `{decisions:[{paragraph,action,why:{profile[],note},confidence,rewrite}]}` |
| `explain_paragraph` | Explain one paragraph or a selected excerpt. | `{paragraph:str, kind:str, text:str, selection?:str, selection_from:str, prev?:str, next?:str, profile_hint:str}` | `{text:str}` |
| `survey_followups` | Generate follow-up questions from a reader's background. | `{sketch:str, answers:object}` | `{questions:[{q:str,hint:str}]}` |

约束：

- 提示词**逐字不变**：分别复用 `prompts.py` 里 `concepts-v2` / `rewrite-v3` / `explain-v2` / `survey-v1` 的 system 与 user 模板，只把「拼 messages」这一步收进工具内部。
- 工具内部完成「模型输出 → 结构」的解析（复用现有 `extract_json_object` / `_parse_batch` / `_normalize_plan_items` 等），但**校验与落盘仍留在原模块**（`validate_inventory` 等不搬）。
- `execute` 返回 `{"ok":..., "data":..., "failure":..., "attempts":..., "usage":...}` 形状（工具层统一信封），业务模块据此走原有的降级/退出码路径。

### 3.4 四个调用点迁移

| 文件 | 改成 | 保持不变 |
|---|---|---|
| `analyze.py` | 经 `tools.get("analyze_concepts")` | 解析失败落 `concepts.raw.txt` + 退出码 5；`validate_inventory` 不过不写盘 + 退出码 4 |
| `rewrite.py` | 每批经 `rewrite_batch` | 批失败记 `meta.model_errors` / `unplanned_paragraphs` + 退出码 6；第 1 批串行预热、其余并发上限 4 不变 |
| `explain.py` | 经 `explain_paragraph` | 降级响应键与语义不变；缓存键不变 |
| `survey.py` | 经 `survey_followups` | 失败仍 200 + `degraded`；记账仍落 `survey.json` |

四处**不再直接引用 `ModelClient`**，只引用工具层与 `llm.py`。

### 3.5 配置扩展

`config.local.json` 新增**可选** `retry` 段，形状照 DSH：

```json
"retry": { "mode": "normal", "maxRetries": 5,
           "retryableCodes": ["EMPTY_RESPONSE","RATE_LIMIT","SERVER","TIMEOUT","TRANSPORT"],
           "backoff": { "initialDelayMs": 500, "maxDelayMs": 10000, "jitterRatio": 0.1 } }
```

- 缺省 = §2 默认值；`config.example.json` 里补上这段并写清默认口径。
- 非法值（负数、`initialDelayMs > maxDelayMs`、`jitterRatio` 越界）→ 报错退出（退出码 2），不静默回落；**未知键**走现有 `unknown_keys` 告警机制。
- 旧的 `max_retries` 顶层键保留读入并映射为 `retry.maxRetries`（兼容），但同时给出 deprecation 提示。

### 3.6 CLI

新增第 6 个子命令（**只读**）：

```
uv run ar tools            # 人类可读：四个工具的名称 / 描述 / 入参出参 schema
uv run ar tools --json     # 机器可读 JSON 数组
```

其余五个子命令的名称、参数、退出码**不变**。

## 4. 保持不变（硬约束，越界即返工）

1. 五个既有子命令的名称、参数、退出码语义（0/2/3/4/5/6）。
2. 产物文件名与既有字段名；新增只能**追加**（`meta.attempts` / `meta.retry_account` / 逐批 `attempts`）。
3. HTTP 端点与响应键；`degraded` 布尔/数组语义不变。
4. 四个提示词版本号与正文**逐字不变**。
5. 密钥纪律：只运行时读取、报错过 `redact()`、产物与日志不含密钥值。
6. `stream: true` + `stream_options.include_usage`，以及「被拒后自动关掉该参数重试且不占重试额度」逻辑保留。
7. `rewrite` 的「第 1 批串行预热 + 其余并发上限 4」保留。
8. 讲解的进程内 LRU 缓存行为与键不变。

## 5. 测试清单

新增 `tests/test_llm.py`：

- 退避序列（假时钟 + 假随机 0.5）：500 / 1000 / 2000 / 4000 / 8000 / 10000，封顶不再增长。
- 白名单：`AUTH` / `INVALID_REQUEST` / `QUOTA` / `UNKNOWN` 各只尝试 1 次。
- `Retry-After` 界内优先（`retry_after_used=True`）、超界放弃重试。
- 每次尝试重建请求：原始 messages 对象在重试后逐字未变。
- 记账：尝试条数 = `1 + 实际重试次数`，`delay_ms` 与退避序列一致。
- 流提前结束 → `TRANSPORT` 且可重试（修掉范本盲点的对照用例）。
- `max_retries=0` → 只尝试 1 次。
- 配置解析：合法值/非法值/缺省/defaults 映射。

新增 `tests/test_tools.py`：

- 注册表：恰好四个工具、名称唯一、`schemas()` 可 JSON 序列化且字段齐全。
- 每个工具：入参缺字段报错；假模型返回下出参结构符合 schema（键集与类型）。
- 工具内部把模型输出解析成结构，异常输出 → 统一信封的失败分支。

改造既有用例：`tests/test_model.py`（新返回结构）、`tests/test_rewrite.py` / `tests/test_analyze.py`（断言 `attempts` 落盘）、`tests/test_cli.py`（`ar tools` 与 `ar tools --json`）。

## 6. 验收标准

1. `uv run pytest tests -q` 全绿。
2. `uv run ar tools` 与 `uv run ar tools --json` 输出四个工具的完整契约。
3. 假模型下端到端：`analyze` → `rewrite` → `check` 三步链跑通，产物里能看到 `meta.attempts`。
4. 反向验证：把 `retry.maxRetries` 设为 0，断言失败请求只发 1 次；把 `retryableCodes` 设为 `["AUTH"]`，断言 401 会重试而 429 不会（证明白名单真的生效）。
5. 真实网关冒烟（**需用户单独授权计费**）：`ar analyze` + `ar rewrite` 各一次，token 账与尝试账都落盘。

## 7. 不做的事

- 不引入任何 JS 依赖、不调用 DSH 的代码，只借设计与默认值。
- 不实现跨 provider 路由切换（本项目只有一条路由）；`RetryPolicy.mode` 的 `always` **直接报错拒绝**（见 §8 D7）。

## 8. 实施记录

### 8.1 规格书自身的矛盾与裁决（D1）

§3.1 规则 2 的「`code not in NEVER_RETRY_CODES`」与 §6 验收 4 的「把 `retryableCodes` 设为 `["AUTH"]` 时 401 应当重试」**不可能同时成立**。

**裁决：`normal` 模式下白名单是唯一权威。** 显式写进 `retryableCodes` 的码就重试（包括把 `AUTH` 显式写进去的场合）；`NEVER_RETRY_CODES`（`AUTH` / `INVALID_REQUEST` / `QUOTA` / `NOT_CONFIGURED`）降级为 `always` 模式的兜底。默认白名单本来就不含这四个码，所以**默认策略下「鉴权 / 参数 / 配额失败一次就停」的效果不变**（有测试与实测钉住）。

### 8.2 实现中自行决定并已接受的事项

| 编号 | 决定 | 理由 |
|---|---|---|
| D2 | `LlmResult` 追加 `finish_reason` 字段 | `ModelReply.finish_reason` 是既有公开字段，旧 `achat` 的外观要保持 |
| D3 | 工具信封是 7 键（在 §3.3 的 5 键外追加 `raw_text`、`degraded`） | analyze 要落 `concepts.raw.txt`；rewrite 要记 `meta.degraded` |
| D4 | `ToolSpec.execute` 额外接受关键字注入 `client` / `policy` | 避免全局可变状态，也不会在 rewrite 的并发批次间串味；不给 `client` 时返回 `NOT_CONFIGURED` 信封、不发请求 |
| D5 | `rewrite_batch` 追加**可选** `source_id` / `total`；`explain_paragraph` 的 `prev` / `next` 按相邻段正文处理 | 模板里有「材料：《{source_id}》，共 {total} 段」，缺了提示词就不等价 |
| D6 | `retry` 段加进 `DEFAULT_CONFIG`；示例文件里用**下划线开头的键当注释**（读入忽略、不进 unknown 告警）；示例里删掉废弃的顶层 `max_retries` | 既有 `test_config` 有「示例键集不得超过默认键集」的断言，示例不得新增顶层键 |
| D7 | `retry.mode = "always"` 直接报错（退出码 2），不静默忽略 | 不静默改变语义，也避免无上限重试 |
| D8 | `retryableCodes` 里出现未知码直接报错；**空数组合法**（表示显式「永不重试」） | 写错一个码会让重试静默失效 |
| D9 | 工具层按 `EXPLAIN_KINDS` 强校验 `kind` | 迁移时必须保证既有 HTTP 行为逐字不变：非法 `kind` 原本是 400，**不得变成 500** |
| D10 | 流没有 `[DONE]` 且没有 `finish_reason` → 判 `TRANSPORT` 失败并重试，已收到的半截正文丢弃 | 这正是 §2 要求修掉的范本盲点；有 `finish_reason` 无 `[DONE]` 仍算成功 |
| D11 | 顶层 `max_retries` 只在配置文件里**显式写过**时才映射进 `retry`；直接构造 `Config(max_retries=0)` 不再等于「不重试」 | 策略只看 `retry` 段；已写进字段注释与测试 |
| D12 | `stream_options` 被拒后的降级重发**不单独记一条 attempt** | 沿用「不占重试额度」的旧语义，只写进 `degraded_params` / 信封 `degraded` |

### 8.3 阶段 2（四个调用点迁移）细则

1. **尝试账落盘位置**：`concepts.json` 的 `meta` 追加 `attempts` 与 `retry_account`；`decisions.json` 与 `changes.json` 的 `meta` 追加同样两个字段，且既有 `meta.batch_usage[]` 的**每条**追加 `attempts`；`profiles/<reader>/survey.json` 的追问记账追加 `attempts`；`/api/explain` **不新增响应字段**（`usage` 已透传，讲解不落盘）。新增一律追加，旧字段名与语义不动。
2. **同步/异步**：工具 `execute` 是同步的（内部走 `llm.call`）。在已有事件循环的路径（`/api/explain`、`/api/survey/followup`）**必须** `await asyncio.to_thread(spec.execute, params, client=..., policy=...)`，直接调用会让 `asyncio.run` 在运行中的循环里抛错。CLI 同步路径直接调用。
3. `raw_text` 只允许写进 `concepts.raw.txt` 这类排查文件，不得进任何 `meta`。
4. 迁移后四个模块不再直接引用 `ModelClient`；退出码、HTTP 响应键、降级语义、`rewrite` 的「第 1 批串行预热 + 其余并发 4」、讲解缓存行为全部不变。
5. 顺手清理测试夹具里已废弃的顶层 `max_retries` 写法，消除废弃告警。

### 8.4 阶段 2 实施记录（四个调用点迁移）—— 已完成并验收

验收：`uv run python -m pytest tests -q` → **382 passed, 1 warning**（唯一告警是既有的 Starlette 弃用提示）。

实现结果：

- 四个模块不再直接引用 `ModelClient`（`grep` 0 命中）；`analyze` / `rewrite` 走同步路径，`explain` / `survey` 在 async 路由里用 `await asyncio.to_thread(tools.run_tool, ...)`。`server.py` 只多了一行（survey 的 `call` 事件加 `attempts`）。
- 尝试账落盘：`concepts.json` / `decisions.json` / `changes.json` 的 `meta` 追加 `attempts` 与 `retry_account`；`decisions.json` 的 `meta.batch_usage[]` **每条**追加该批 `attempts`；`survey.json` 的 `call` 事件追加 `attempts`（不加 `retry_account`，因为那一层没有模型调用）。
- `tools.py` 追加（既有签名与信封键集未动）：`ToolRun`、`_ResultKeeper`、`run_tool()`；`explain_paragraph` 追加**可选**入参 `prev_id` / `next_id`——不加的话提示词里邻段行会从 `[p003] 正文` 退化成 `[prev] 正文`（提示词漂移）。
- `rewrite` 的 `meta.retry_account` 是**多批汇总**（`requests` = 总尝试数；每批首发不计重试）。单批时与 `llm.retry_account` 逐字一致。

对 §8.2 的更正与补充：

| 项 | 结论 |
|---|---|
| **D9 更正** | 非法 `kind` 的**既有行为不是 400，而是软回落成 `explain` 讲法并返回 200**（只有非字符串 `kind` 才 400）。按「行为零变化」保留软回落，并新增用例同时钉住 200 与 400、绝不 500。将来若要改成 400，属行为变更，需单独裁决。 |
| D13 | 0 段材料的 `analyze` 改为直接按校验失败报（退出码 4、**不发请求**）；迁移前会先发一次注定无用的请求再以 4 收尾。退出码与产物不变。 |
| D14 | 删除两处死代码：`rewrite._extract_json`、`survey.survey_outcome_from`（`__all__` 换成 `outcome_from_tool_run`），全仓无引用。 |
| D15 | `rewrite` 的 `meta.degraded` 会多出参数降级说明（如「`stream_options.include_usage` 被网关拒绝」）——只增可见信息，不影响退出码与既有字段。 |

遗留技术债（记录在案，本次未处理）：

1. ~~`rewrite.planDecisions` 里「已有事件循环 → 退回全串行」的老分支~~ —— **已于 2026-10-13 解决（见 §8.6）**。原判据是 `asyncio.get_running_loop()`：命中时走全串行路径，而那条路径仍在**调用线程**里同步调 `asyncio.run`，所以在事件循环里调用会整批降级（`RuntimeError: asyncio.run() cannot be called from a running event loop`）。
2. 空白邻段行会被工具层跳过（`ingest` 保证段非空，实际材料不可达）。
3. `ruff check` 有 16 条既有告警（HEAD 就存在，非本次引入），未顺手修改以免越界。

### 8.5 复核与修复记录（2026-10-13）

独立只读对抗性复核的结论：**无阻断、无严重**；退出码、产物字段（只追加）、HTTP 响应键、提示词逐字不变**全部守住**；没有发现新的「模型失败却看不出」的路径；6 批并发下账目零串味；前端零改动。它抓到 2 条「一般」级可见性缺陷 + 1 条潜伏契约不一致，已全部修复：

| 修复项 | 处理 |
|---|---|
| 失败文案丢失尝试次数 | 新增 `llm.failure_message(result)` 作为**唯一**文案来源（形如「模型调用失败（已试 N 次，末次 `<CODE>`）：<脱敏消息>」），`analyze` / `rewrite` 的批次错误 / `explain` / `survey` 四处失败分支统一改用 |
| 讲解的重试账无痕 | `/api/explain` 的成功与降级响应**追加** `attempts`（六个键与产物 `meta.attempts` 一致）。**本条覆盖 §8.3 第 1 条「`/api/explain` 不新增响应字段」的说法**——当时只考虑到「讲解不落盘」，漏了「至少要让重试账可见」 |
| CLI 打印的请求数其实是批数 | `_usage_summary` 同时打印真实请求数与重试次数，形如「请求 2 次（1 批；其中重试 1 次）」；`usage.requests` **仍是批数**（既有语义不动）；CLI 另一处「N 次请求合并了 M 段」改为「N 批合并了 M 段」 |
| 工具层入参校验抛异常 | 改为返回 `INVALID_REQUEST` 失败信封（`degraded` 逐条点名参数、`attempts=[]`、不发任何请求），不再抛 `InputError`；消除「调用方的 bug 被报成 HTTP 400」的潜伏路径 |
| `server.py` 过时 docstring | 假客户端的要求改成 `arequest_once` / `retry_policy` / `configured` |

验收：`uv run python -m pytest tests -q` → **全绿**（修复前 382 条，本次新增 11 条用例、只增不减；那次跑动的绝对条数已被 §8.6 的新用例覆盖——**当时（2026-10-13）现行 394 passed，这是历史值**，此后每一批的增量与**当前计数一律以 [README.md](README.md) §7 为准**）。文案、响应字段、CLI 输出三处都有用例钉住，其中含一条「网关把密钥回显在错误正文里也不漏」的脱敏用例。

顺带确认（**非本次引入**，记录备查）：`analyze` 遇到「合法 JSON 但没有 `concepts` 数组」时抛 `InputError` ⇒ 退出码 2（把模型问题报成输入问题），与改造前逐字一致。

仍遗留：`ar check` 的失败明细会随新文案变长（属预期可见）。~~`rewrite.planDecisions` 的「已有事件循环 → 全串行」分支~~ 已于 §8.6 解决。

**未在真实网关上验证的一项**（用户 2026-10-13 明确选择不跑冒烟）：真实 SSE 流与 token 上报在新调用层里的表现。其余结论均有假模型或代码级证据。

### 8.6 技术债清理：rewrite 的批次执行（2026-10-13）—— 已完成并验收

`rewrite.planDecisions` 的「已有事件循环 → 退回全串行」分支已按方案 A 清除：**所有批次（含第 1 批预热）都改到工作线程里执行**——工作线程内没有运行中的事件循环，`asyncio.run` 正常工作，于是 async 调用方与 CLI 走同一条路径。

- **删掉的判据**：`try: asyncio.get_running_loop()` 命中时走 `_plan_batches_serial(...)`；该函数与模块级 `import asyncio` 一并作为死代码删除。
- **保留的语义**：第 1 批**先跑完**才发其余批次（前缀缓存预热）、其余批次并发上限 4、切批阈值、批次顺序、产物结构（`batch_usage` 逐批明细、`meta.attempts`、多批汇总 `retry_account`）全部不变。`planDecisions` 仍是同步函数（签名未动）。
- **新增用例**：在事件循环内调用 `planDecisions` ⇒ 正常完成、无 RuntimeError 降级、`unplanned_paragraphs` 为空、账目正确；并额外断言「批次都在调用线程之外执行」与「不出现『协程未 await』警告」（防止降级路径变形时漏检）。预热顺序与并发峰值仍由既有用例守住。

验收：`uv run python -m pytest tests -q` → **394 passed, 1 warning**（唯一告警是既有的 Starlette 弃用提示）；
前端 `uv run pytest tests/web -q` → **98 passed, 1 skipped**（口径见 README §7）。
⚠ 这两个数是**当时（2026-10-13）的历史值**：此后每一批都在往里加用例，**现行数字与增量清单一律以
[README.md](README.md) §7 为准**（本文件开头的「394 passed / 98 passed + 1 skipped」同样只是那一轮的口径）。

前后对照（同一脚本 `asyncio.run(_scenario())`，7 批）：修复前 7 批全降级、`model_errors` 7 条 `RuntimeError: asyncio.run() cannot be called from a running event loop`、7 段进 `unplanned`、`retry_account.requests = 0`；修复后 `model_errors = 0`、`unplanned` 空、`retry_account.requests = 7`。另做了反事实对照（把预热批打回调用线程）⇒ 新用例断言立即失败，证明断言非恒真。

残留（同步 API 的固有语义，非回归）：在事件循环线程里调用 `planDecisions` 会阻塞该线程直到全部批次跑完；将来若要非阻塞，需另加 async 版入口。
- 不改提示词内容，不改前端。
- 不做局部重跑（仍是整篇重跑），不扩 HTTP 接口面（除 `ar tools` 这个 CLI 子命令）。
