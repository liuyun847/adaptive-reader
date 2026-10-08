/**
 * 数据来源：正式模式走 /api/*，夹具模式读同源静态文件（PLAN-skeleton §4）。
 *
 *   正式：GET  /api/doc/<source_id>            POST /api/explain
 *          GET  /api/profile（只用来算会话缓存键里的画像指纹，见 loadProfileSignal）
 *   夹具：GET  ../samples/<source_id>/doc.json GET  ../samples/<source_id>/explain.json
 *          （URL 带 ?fixture=<source_id> 时启用）
 *
 * 失败一律抛 DataError（带 url / status / tried），由上层显示成可见的降级提示——不静默。
 */

export const FIXTURE_PARAM = 'fixture';
export const SOURCE_PARAM = 'source';

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
    throw new DataError(`HTTP ${res.status}`, { url, kind: 'http', status: res.status });
  }
  try {
    return await res.json();
  } catch (err) {
    throw new DataError(`响应不是合法 JSON（${err.message}）`, { url, kind: 'json', status: res.status });
  }
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
