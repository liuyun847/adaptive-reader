/**
 * 数据来源：正式模式走 /api/*，夹具模式读同源静态文件（PLAN-skeleton §4）。
 *
 *   正式：GET  /api/doc/<source_id>            POST /api/explain
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
 * 请求一段讲解（= POST /api/explain 的响应体）：{ text, degraded? }。
 * @param {{paragraph:string, kind:'explain'|'simpler'|'detail'}} req
 */
export async function requestExplain(ctx, req) {
  const options = ctx.mode === 'fixture'
    ? undefined
    : {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        source_id: ctx.sourceId,
        paragraph: req.paragraph,
        kind: req.kind,
      }),
    };
  const { data, url } = await firstOk(explainUrls(ctx), options);
  return { payload: data || {}, url, mode: ctx.mode };
}
