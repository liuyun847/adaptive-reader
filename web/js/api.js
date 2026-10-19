/**
 * 数据来源：正式模式走 /api/*，夹具模式读同源静态文件（PLAN-skeleton §4）。
 *
 *   正式：GET  /api/doc/<source_id>            POST /api/explain
 *          GET  /api/profile（只用来算会话缓存键里的画像指纹，见 loadProfileSignal）
 *          POST /api/compress（读中「太啰嗦」当场压缩该段，见 requestCompress）
 *          POST /api/events（行为事件批量上报，见 postEvents；只 append，绝不写画像）
 *          GET  /api/sources（启动页的材料清单，见 fetchSources；**只读**）
 *          POST /api/ingest（启动页的上传摄取，见 postIngest；只摄取，不调模型）
 *   夹具：GET  ../samples/<source_id>/doc.json GET  ../samples/<source_id>/explain.json
 *          （URL 带 ?fixture=<source_id> 时启用；compress / events 没有夹具后端，前端按模式分流）
 *
 * 失败一律抛 DataError（带 url / status / tried），由上层显示成可见的降级提示——不静默。
 */

export const FIXTURE_PARAM = 'fixture';
export const SOURCE_PARAM = 'source';
/** 显式打开背景问卷的开关（夹具模式看问卷界面用，见 js/survey.js）。 */
export const SURVEY_PARAM = 'survey';

export class DataError extends Error {
  constructor(message, detail = {}) {
    super(message);
    this.name = 'DataError';
    this.detail = detail;
    this.url = detail.url || null;
    this.status = typeof detail.status === 'number' ? detail.status : null;
  }
}

/** 从 URL 查询串读出本次页面的运行模式。 */
export function readContext(search) {
  const params = new URLSearchParams(search || '');
  const fixture = params.get(FIXTURE_PARAM);
  const source = params.get(SOURCE_PARAM);
  return {
    mode: fixture ? 'fixture' : 'api',
    sourceId: fixture || source || null,
    explicitSource: Boolean(source),
    // `?survey=1`：显式打开背景问卷（夹具模式默认不弹，见 js/survey.js 的 bootSurvey）
    surveyAsked: params.get(SURVEY_PARAM) === '1',
  };
}

async function fetchJson(url, options) {
  let res;
  try {
    res = await fetch(url, options);
  } catch (err) {
    throw new DataError(`网络请求失败（${err.message}）`, { url, kind: 'network' });
  }
  if (!res.ok) {
    // 后端（`ar.server`）的 400 / 404 / 409 / 500 **一律回 JSON + 可读 `error`**（契约）：
    // 有就把它（连同 `hint`）带进消息，别让读者只看到干巴巴的「HTTP 404」——启动页那边
    // （`fetchJsonExplained`）早就这么做了，2026-10-15 把这条口径统一过来。
    // 响应不是 JSON（网关 / 静态服务的纯文本错误页）时退回 `HTTP <status>`，一个字都不丢。
    let said = '';
    let hint = null;
    try {
      const body = await res.json();
      if (body && typeof body.error === 'string' && body.error.trim()) said = body.error.trim();
      if (body && typeof body.hint === 'string' && body.hint.trim()) hint = body.hint.trim();
    } catch (err) {
      said = '';
    }
    const status = `HTTP ${res.status}`;
    throw new DataError(
      said ? `${status}：${said}${hint ? ` —— ${hint}` : ''}` : status,
      { url, kind: 'http', status: res.status, hint },
    );
  }
  let payload = null;
  let parseError = null;
  try {
    payload = await res.json();
  } catch (err) {
    parseError = err;
  }
  if (payload === null) {
    // 合法 JSON 的 `null`（或空响应）不是「解析失败」：别把两种原因混成一句假话
    throw new DataError(
      parseError
        ? `响应不是合法 JSON（${parseError.message}）`
        : '响应是 JSON `null`：结构不认识（后端应当回一个 JSON 对象）',
      { url, kind: 'json', status: res.status },
    );
  }
  return payload;
}

/** 依次尝试候选地址，全部失败时把每个地址的失败原因一起抛出来。 */
async function firstOk(urls, options) {
  const tried = [];
  for (const url of urls) {
    try {
      return { data: await fetchJson(url, options), url };
    } catch (err) {
      tried.push({ url, message: err.message, status: err.status });
    }
  }
  throw new DataError(
    tried.map((t) => `${t.url} → ${t.message}`).join('；'),
    { tried, kind: 'all-failed', url: tried.length ? tried[0].url : null },
  );
}

/* ------------------------------------------------- 读者画像指纹（缓存键的第五段） */

export const PROFILE_URL = '/api/profile';

/** FNV-1a 32 位（纯函数、无依赖）：把画像内容压成 8 位十六进制，够当「变没变」的信号。 */
export function fnv1a(text) {
  let hash = 0x811c9dc5;
  const source = String(text == null ? '' : text);
  for (let i = 0; i < source.length; i += 1) {
    hash ^= source.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, '0');
}

/**
 * 画像指纹（前端等价信号）：后端缓存键里含 `profiles/<reader>/profile.yaml` 的
 * `(路径, 字节数, mtime_ns)`（`src/ar/cache.py`）。前端会话缓存的键不带同口径的信号时，
 * 「会话内画像变了（自评写回 / 手改 overrides）」仍会命中旧答案——所以这里补上。
 *
 * 现有能力里没有指纹接口（`GET /api/profile` 只回解析后的画像），因此用**画像内容的摘要**
 * 当信号：内容一变摘要就变。它比后端的 size+mtime 更严（内容没变、只是 mtime 变了不改键），
 * 方向是安全的：绝不会命中「画像已经变了」的旧答案。
 */
export function profileSignalOf(payload) {
  return fnv1a(JSON.stringify({
    profile: (payload && payload.profile) || null,
    degraded: (payload && payload.degraded) || [],
  }));
}

/**
 * 取一次画像信号。夹具模式没有后端也没有画像 → 返回 `''`（键里那一段留空，且不请求任何接口）。
 * 失败时抛 DataError，由调用方决定「保留上一次信号 + 留可见降级」。
 */
export async function loadProfileSignal(ctx) {
  if (!ctx || ctx.mode !== 'api') return '';
  const { data } = await firstOk([PROFILE_URL]);
  return profileSignalOf(data);
}

/**
 * 候选地址用相对路径打头（../samples/...）：既能在「项目根目录起静态服务」下命中，
 * 也能在服务端把 web/ 挂到 /web/ 之外的子路径时命中；再加一条根相对路径兜底。
 */
export function docUrls(ctx) {
  if (ctx.mode === 'fixture') {
    if (!ctx.sourceId) {
      throw new DataError('夹具模式必须给 ?fixture=<source_id>', { kind: 'param' });
    }
    return [
      `../samples/${ctx.sourceId}/doc.json`,
      `/samples/${ctx.sourceId}/doc.json`,
    ];
  }
  if (!ctx.sourceId) {
    throw new DataError(
      '没有指定材料：正式模式请用 ?source=<source_id>（夹具模式用 ?fixture=<source_id>）',
      { kind: 'param' },
    );
  }
  return [`/api/doc/${encodeURIComponent(ctx.sourceId)}`];
}

export function explainUrls(ctx) {
  if (ctx.mode === 'fixture') {
    return [
      `../samples/${ctx.sourceId}/explain.json`,
      `/samples/${ctx.sourceId}/explain.json`,
    ];
  }
  return ['/api/explain'];
}

/** 载入 doc（= GET /api/doc 的响应体）：{ meta, rewritten, changes, concepts }。 */
export async function loadDoc(ctx) {
  const { data, url } = await firstOk(docUrls(ctx));
  if (!data || typeof data !== 'object' || !data.meta) {
    throw new DataError('响应里没有 meta 字段，不是合法的 doc 响应体', { url, kind: 'shape' });
  }
  return { doc: data, url };
}

/**
 * 请求一段讲解（= POST /api/explain 的响应体）。
 *
 * 冻结契约：
 *   请求 `{ source_id, paragraph, kind, selection?, selection_from? }`
 *     —— 有 `selection` 时后端以**片段**为讲解对象，`paragraph` 仍提供上下文；
 *        `selection_from ∈ rewritten|source`（可选，默认 rewritten）。
 *   响应 `{ text, degraded?, model?, prompt_version?, reason? }`，
 *     片段请求可能追加 `scope: "selection"|"paragraph"`、`cached: true`、`selection_truncated: true`。
 *   追加字段一律**可选**：夹具响应与旧响应缺这些键时，前端必须照常跑。
 *
 * @param {{paragraph:string, kind:'explain'|'simpler'|'detail',
 *          selection?:string, selectionFrom?:'rewritten'|'source'}} req
 */
export async function requestExplain(ctx, req) {
  const selection = typeof req.selection === 'string' ? req.selection : '';
  const body = {
    source_id: ctx.sourceId,
    paragraph: req.paragraph,
    kind: req.kind,
  };
  if (selection) {
    body.selection = selection;
    body.selection_from = req.selectionFrom === 'source' ? 'source' : 'rewritten';
  }
  const options = ctx.mode === 'fixture'
    ? undefined
    : {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    };
  const { data, url } = await firstOk(explainUrls(ctx), options);
  return { payload: data || {}, url, mode: ctx.mode };
}

/* ------------------------------------------------- 读前背景问卷（PLAN-background-survey §8） */

/**
 * 契约（追加端点，不改旧键）：
 *
 *   GET  /api/survey          → { version, answered_version, questions[], answers, skipped, degraded[] }
 *                               `answers` 是当前画像里的背景（扁平字段名，无则 null）；题目由后端给。
 *   POST /api/survey          ← { answers?, skipped?, followups? }
 *                             → { ok, background, sketch, degraded[] }；校验失败 400（InputError 的既有转换）
 *   POST /api/survey/followup ← { } 或 { followups: [{q, a}] }
 *                             → { questions[{q,hint}], text, degraded, prompt_version, reason?, model?, usage? }
 *
 * 夹具模式没有后端：这些函数一律抛 DataError，由调用方（`js/survey.js` 的 bootSurvey）在请求之前
 * 就按 `ctx.mode === 'fixture'` 分流——夹具模式读 `samples/<id>/` 下的静态文件，不发任何请求。
 */
export const SURVEY_URL = '/api/survey';

/** 取问卷（题目 + 当前画像里的背景）。正式模式专用；夹具模式别调用。 */
export async function fetchSurvey(ctx) {
  if (ctx && ctx.mode === 'fixture') {
    throw new DataError('夹具模式没有问卷后端（GET /api/survey 只在正式模式可用）', {
      url: SURVEY_URL, kind: 'fixture', status: null,
    });
  }
  const { data, url } = await firstOk([SURVEY_URL]);
  return { payload: data || {}, url, mode: 'api' };
}

/**
 * 写画像：提交固定题作答（`{ answers }`）、整场跳过（`{ skipped: true }`）、
 * 或保存读者对模型追问的作答（`{ followups: [{q, a}] }`，`at` 由后端补）。
 *
 * 三类请求体都是契约 §8 的 `POST /api/survey` 形状，可单用也可合用：
 *   - `{ answers }`：八个作答槽位，**不带 `version`**（多一个键就 400）；
 *   - `{ skipped: true }`：只记跳过标志，已有背景与追问**不会被清掉**；
 *   - `{ followups: [...] }`：把追问的问答并进 `background.followups[]`（只给这一个键也合法）。
 * 失败一律抛 DataError（带 status），由调用方显示成可见降级并把答案留在表单里。
 */
export async function submitSurvey(ctx, body) {
  if (ctx && ctx.mode === 'fixture') {
    throw new DataError('夹具模式不保存背景（POST /api/survey 只在正式模式可用）', {
      url: SURVEY_URL, kind: 'fixture', status: null,
    });
  }
  const payload = body && typeof body === 'object' ? body : {};
  const { data, url } = await firstOk([SURVEY_URL], {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return { payload: data || {}, url, mode: 'api' };
}

/**
 * 夹具模式的题目来源：同源静态文件 `samples/<id>/survey-questions.json`。
 *
 * 这份文件是**从后端的 `ar.survey.QUESTIONS` 直接导出**的同一份数据（不是手抄的第二份题表），
 * 所以夹具模式渲染出来的题目与正式模式一致；`tests/web/test_survey_ui.py` 会拿后端的题目定义
 * 比对它与提交体的字段名，两边走偏就会红。
 *
 * 候选地址与 doc.json 同规矩：先相对路径（`web/` 之外挂子路径时也命中），再根相对路径兜底。
 */
export function fixtureSurveyUrls(ctx) {
  const sourceId = (ctx && ctx.sourceId) || '';
  return [
    `../samples/${sourceId}/survey-questions.json`,
    `/samples/${sourceId}/survey-questions.json`,
  ];
}

/** 载入夹具题目；失败抛 DataError（由调用方转成可见降级）。 */
export async function loadFixtureQuestions(ctx) {
  const { data, url } = await firstOk(fixtureSurveyUrls(ctx));
  const questions = Array.isArray(data && data.questions) ? data.questions : [];
  if (!questions.length) {
    throw new DataError('夹具题目文件里没有 questions[]', { url, kind: 'shape' });
  }
  return { questions, version: data.version, url, mode: 'fixture' };
}

/**
 * 夹具模式的**响应形状**样例：`samples/<id>/survey.json` = `GET /api/survey` 的响应体
 * （`version` / `answered_version` / `questions` / `answers` / `skipped` / `degraded`）。
 *
 * 只有 `?survey=fixture` 才会读它（页面默认不看）：用来在无后端时也覆盖
 * 「问卷已更新」提示与「回填已存答案」两条分支。
 */
export function fixtureSurveyResponseUrls(ctx) {
  const sourceId = (ctx && ctx.sourceId) || '';
  return [
    `../samples/${sourceId}/survey.json`,
    `/samples/${sourceId}/survey.json`,
  ];
}

/** 载入夹具响应体（形状校验与 fetchSurvey 同口径）。 */
export async function loadFixtureResponse(ctx) {
  const { data, url } = await firstOk(fixtureSurveyResponseUrls(ctx));
  if (!data || typeof data !== 'object') {
    throw new DataError('夹具响应不是合法 JSON 对象', { url, kind: 'shape' });
  }
  return { payload: data, url, mode: 'fixture' };
}

/* ------------------------------------- 当场压缩 + 行为事件（PLAN-actions-feedback §5.2 / §7.2） */

export const COMPRESS_URL = '/api/compress';
export const EVENTS_URL = '/api/events';

/**
 * 读中「太啰嗦」→ 当场压缩该段（契约 §5.2）。
 *
 * 请求 `{ source_id, paragraph, reason? }`（`reason` 默认 `too_verbose`；读者 id 由服务端定）；
 * 响应形状**一律 200**（照 `/api/explain` 的惯例）：
 *   成功 `{ ok: true, paragraph, action: "compress", text, scope: "paragraph", cached, usage,
 *            attempts, model, prompt_version, degraded, artifacts_updated: true, hint? }`
 *        —— `degraded` 平时是 `[]`；非空只说明「行为事件没记上」（压缩结果与产物不受影响），
 *        调用处照样要打 warn 横幅（成功路径也要看得见降级）；
 *   降级 `{ ok: false, reason, text, degraded: [...] }`，
 *   `reason ∈ model_not_configured / model_error / bad_reply / fact_anchor_mismatch / no_artifacts /
 *             paragraph_not_found / compress_not_shorter`。
 *
 * 夹具模式没有这个后端（`?fixture=` 只读静态样例）：调用处按 `ctx.mode` 先分流，
 * 这里也留一道兜底，把「夹具模式点了这个按钮」变成可见降级而不是一个 404。
 */
export async function requestCompress(ctx, { paragraph, reason = 'too_verbose' } = {}) {
  if (!ctx || ctx.mode !== 'api') {
    throw new DataError('夹具模式没有压缩后端（POST /api/compress 只在正式模式可用）', {
      url: COMPRESS_URL, kind: 'fixture', status: null,
    });
  }
  const { data, url } = await firstOk([COMPRESS_URL], {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ source_id: ctx.sourceId, paragraph, reason }),
  });
  return { payload: data || {}, url, mode: 'api' };
}

/**
 * 行为事件批量上报（契约 §7.2）：请求 `{ events: [...] }`（也容忍单个对象）。
 * 响应 `{ ok, written, duplicates, total, degraded[] }`；**只 append，绝不写画像**。
 *
 * `keepalive: true` 是为了 `pagehide` / `visibilitychange` 那两次强制 flush：
 * 页面正在卸载时普通 fetch 会被浏览器掐掉，而这两次 flush 恰恰最要紧。
 * 夹具模式不请求它（`ctx.mode === 'fixture'` 时抛 DataError，由 js/events.js 转成可见降级）。
 */
export async function postEvents(ctx, events) {
  const list = Array.isArray(events) ? events : [];
  if (!ctx || ctx.mode !== 'api') {
    throw new DataError('夹具模式不记行为信号（POST /api/events 只在正式模式可用）', {
      url: EVENTS_URL, kind: 'fixture', status: null,
    });
  }
  const { data, url } = await firstOk([EVENTS_URL], {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ events: list }),
    keepalive: true,
  });
  return { payload: data || {}, url, mode: 'api' };
}

/* -------------------------------------- 模型追问（契约 §4；两个端点各司其职） */

export const SURVEY_FOLLOWUP_URL = '/api/survey/followup';

/**
 * 让模型按**已存**的固定题作答追问 3–5 个细节（不读材料正文）。**这个端点只负责生成问题**。
 *
 * - 请求体**空**（`{}`）：输入是画像里已存的作答与素描，前端不必再传一遍。
 * - 失败 / 未配置 / 超时都是 **200 + `degraded`**（照 `/api/explain` 的惯例），固定题答案不受影响。
 *   `degraded` 在响应里可能是**布尔**也可能是**字符串数组**，调用方两种都认。
 * - 响应：`{ questions: [{q, hint}], text, degraded, prompt_version, reason?, model?, usage? }`
 *   （`model` / `usage` 只在成功时出现）。
 *
 * ⚠ **保存读者的追问答案不走这里**：那是「写入画像」，按契约 §8 归 `POST /api/survey`
 * （请求体 `{followups: [{q, a}]}`，走 `submitSurvey`）。一个端点只干一件事，将来改一处不会漏另一处。
 * 后端在 `/api/survey/followup` 上留了兼容分支（也能并 followups），前端不再用它保存答案。
 */
export async function requestSurveyFollowups(ctx, body) {
  if (ctx && ctx.mode === 'fixture') {
    throw new DataError('夹具模式没有追问后端（POST /api/survey/followup 只在正式模式可用）', {
      url: SURVEY_FOLLOWUP_URL, kind: 'fixture', status: null,
    });
  }
  const payload = body && typeof body === 'object' ? body : {};
  const { data, url } = await firstOk([SURVEY_FOLLOWUP_URL], {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return { payload: data || {}, url, mode: 'api' };
}

/**
 * 夹具模式的追问响应：`samples/<id>/survey-followup.json`。
 *
 * ⚠ 这一份是**手写的**（照后端 `SurveyOutcome` 的响应形状，本机没有可复现的模型输出可导），
 * 与 `samples/README.md` 开头那条「手造样例」纪律一致；前端只读
 * `questions` / `degraded` / `text` / `reason` 这几个键。
 */
export function fixtureFollowupUrls(ctx) {
  const sourceId = (ctx && ctx.sourceId) || '';
  return [
    `../samples/${sourceId}/survey-followup.json`,
    `/samples/${sourceId}/survey-followup.json`,
  ];
}

export async function loadFixtureFollowups(ctx) {
  const { data, url } = await firstOk(fixtureFollowupUrls(ctx));
  if (!data || typeof data !== 'object') {
    throw new DataError('夹具追问响应不是合法 JSON 对象', { url, kind: 'shape' });
  }
  return { payload: data, url, mode: 'fixture' };
}

/* ------------------------------------- 启动页：材料清单 + 上传摄取（2026-10-15，契约 §4） */

export const SOURCES_URL = '/api/sources';
export const INGEST_URL = '/api/ingest';

/**
 * 读一个 JSON 响应，**并把后端那句话带进错误里**。
 *
 * 为什么不复用上面的 `fetchJson`：它遇到非 2xx 只抛 `HTTP 400` 这种干巴巴的状态码，
 * 而后端 `/api/ingest` 的 400 / 409 / 500 **一律回 JSON + 可读 `error`**（契约明写），
 * 启动页要把那句话原样显示给读者（「材料 X 已经存在，本次没有写任何东西：…」这类）。
 * 所以这里把响应体读出来，非 2xx 时优先用 `error` 当消息、`hint` 放进 `detail` 备查。
 *
 * 老的 `fetchJson` / `firstOk` **一个字没改**：讲解 / 压缩 / 事件那几条路照旧走它们。
 */
async function fetchJsonExplained(url, options) {
  let res;
  try {
    res = await fetch(url, options);
  } catch (err) {
    throw new DataError(`网络请求失败（${err.message}）`, { url, kind: 'network' });
  }
  let payload = null;
  let parseError = null;
  try {
    payload = await res.json();
  } catch (err) {
    parseError = err;
  }
  if (!res.ok) {
    const said = payload && typeof payload.error === 'string' && payload.error.trim()
      ? payload.error.trim()
      : '';
    const status = `HTTP ${res.status}${res.statusText ? ` ${res.statusText}` : ''}`;
    throw new DataError(said || status, {
      url,
      kind: 'http',
      status: res.status,
      // 后端给的出路说明（409 的「要覆盖就带 overwrite: true…」就在这儿）——界面照样要显示它
      hint: payload && typeof payload.hint === 'string' && payload.hint.trim() ? payload.hint.trim() : null,
      body: payload,
    });
  }
  if (payload === null) {
    // 合法 JSON 的 `null`（或空响应）不是「解析失败」：两种原因分开说，别混成一句假话
    throw new DataError(
      parseError
        ? `响应不是合法 JSON（${parseError.message}）`
        : '响应是 JSON `null`：结构不认识（后端应当回一个 JSON 对象）',
      { url, kind: 'json', status: res.status },
    );
  }
  return { payload, url };
}

/**
 * `GET /api/sources`：材料清单（**只读**，一个字节都不写盘）。
 * 形状 `{sources: [...], degraded: [...]}`；排序已由后端定好（mtime 倒序 + source_id 升序），
 * 前端**不再重排**——重排会让「两次调用结果稳定」这条契约在页面上失真。
 * 缺产物时 `concepts` / `rewrite` / `fidelity` 三块各自是 `null`（不是缺键）。
 */
export async function fetchSources() {
  return fetchJsonExplained(SOURCES_URL);
}

/**
 * `POST /api/ingest`：上传的 UTF-8 文本 → 只摄取（不调模型、不跑 analyze / rewrite / check）。
 * 请求 `{filename, content, source_id?, overwrite?}`；成功 200
 * `{source_id, paragraphs, ingested_at, anchors_stable, overwritten, degraded[]}`。
 * 失败 400 / 409 / 500 一律 JSON + 可读 `error`（由 `fetchJsonExplained` 带进 DataError）。
 */
export async function postIngest(body) {
  return fetchJsonExplained(INGEST_URL, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}
