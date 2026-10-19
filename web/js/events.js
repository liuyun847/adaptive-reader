/**
 * 行为埋点（PLAN-actions-feedback.md §7.1 / §7.2）。
 *
 * 前端这一侧只做三件事：**攒、按条件送、送不出去就说出来**。
 *
 *   - 攒：`emitEvent(kind, payload)` 把一条事件压进缓冲，同时留一份可断言的快照
 *     （`globalThis.__arDebug.events`）——无头验证断言缓冲与发送口径时不 sleep。
 *   - 送：**≥ 10 条**或**距首条 ≥ 15 秒**就 flush（`POST /api/events`，一次一批
 *     `{events: [...]}`）；另外在 `visibilitychange`（转隐藏）与 `pagehide` 强刷一次
 *     ——这两次用 `keepalive`，页面正在卸载也送得出去。
 *   - 说出来：请求失败 / 后端回 `ok:false` / 后端带 `degraded` / 夹具模式（没有这个后端）
 *     一律走 `main.js` 的 `degrade()` 留可见文字，**不许静默丢弃**。
 *
 * 两条纪律：
 *
 * 1. **埋点自己不许触发任何讲解 / 压缩请求**：本模块只调 `POST /api/events`，
 *    拿到的 `api` 是 `api.js` 的 `postEvents`（不是 explain / compress）。
 * 2. **夹具模式不发请求**（`?fixture=` 没有后端）：事件照样进缓冲与 `__arDebug`，
 *    flush 时丢弃并给**一次性**可见降级「夹具模式不记行为信号」。
 *
 * `kind` 只认契约 §7.1 的白名单（`EVENT_KINDS`）；别的 kind 记进 `rejected` 不发出——
 * 服务端会按白名单丢弃，与其发出去被丢掉，不如在前端就留下可断言的痕迹。
 */

import { postEvents } from './api.js';

export const EVENTS_URL = '/api/events';

/** 契约 §7.1 的 kind 白名单（顺序与那一节一致）。 */
export const EVENT_KINDS = [
  'dwell', 'read_complete', 'read_abandon', 'explain', 'drawer_open', 'selection', 'compress', 'skip',
];

/** flush 阈值：条数。 */
export const FLUSH_THRESHOLD = 10;
/** flush 阈值：距**首条**入缓冲的毫秒数。 */
export const FLUSH_INTERVAL_MS = 15000;

/** 夹具模式的那一次可见降级文案（契约要求的原话在里面）。 */
export const FIXTURE_NOTE = '夹具模式不记行为信号：这次会话的停留 / 读完 / 讲解 / 压缩都不会写进'
  + '任何文件（夹具模式读的是静态样例，没有 /api/events 后端）。阅读照常。';

/** `__arDebug.events.records` 最多留多少条（可断言用，不是业务数据）。 */
const RECORD_CAP = 300;

const state = {
  ctx: null,
  api: null,
  mode: null,
  sourceId: null,
  buffer: [],            // 待发送
  records: [],           // 本次会话所有事件（含已发送），供 __arDebug 断言
  kinds: {},             // kind -> 条数
  rejected: [],          // 白名单外的 kind（前端就拦下，不发给服务端）
  flushes: [],           // 每次 flush 的记录：{reason, count, status, at, response?}
  sent: 0,               // 服务端确认写入的条数（written 之和）
  attempted: 0,          // 送出去的条数（含失败）
  dropped: 0,            // 夹具模式丢弃的条数
  failed: 0,             // 请求失败 / 后端没写成功而丢掉的条数
  inflight: 0,
  timer: null,
  seq: 0,
  degradeFn: null,
  bannerFn: null,
  fixtureNoted: false,
  preFlush: null,        // main.js 挂进来的「强制 flush 之前先结算一下现场」（见 initEvents）
  readComplete: false,
  lastParagraph: null,
  listenersMounted: false,
};

function debugHandle() {
  return globalThis.__arDebug || null;
}

/** 本地时区偏移的 ISO 时间（契约示例是 `2026-10-13T18:20:00+08:00` 这种带偏移的形态）。 */
export function isoWithOffset(date = new Date()) {
  const pad = (value, width = 2) => String(Math.abs(value)).padStart(width, '0');
  const offsetMinutes = -date.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? '+' : '-';
  const body = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
    + `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  const offset = `${sign}${pad(Math.floor(Math.abs(offsetMinutes) / 60))}:${pad(Math.abs(offsetMinutes) % 60)}`;
  return `${body}${offset}`;
}

/** 客户端 uuid（`id` 供服务端幂等去重）；没有 `crypto.randomUUID` 时退到时间戳 + 序号 + 随机数。 */
export function eventId() {
  try {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
      return globalThis.crypto.randomUUID();
    }
  } catch (err) { /* 退到下面那条 */ }
  state.seq += 1;
  return `ar-${Date.now().toString(36)}-${state.seq}-${Math.random().toString(36).slice(2, 10)}`;
}

/** 可断言快照（`__arDebug.events`）；只读的一份拷贝，不是业务数据。 */
export function eventsSnapshot() {
  return {
    mode: state.mode,
    sourceId: state.sourceId,
    url: EVENTS_URL,
    buffered: state.buffer.length,
    threshold: FLUSH_THRESHOLD,
    intervalMs: FLUSH_INTERVAL_MS,
    total: state.records.length,
    sent: state.sent,
    attempted: state.attempted,
    dropped: state.dropped,
    failed: state.failed,
    inflight: state.inflight,
    kinds: { ...state.kinds },
    rejected: state.rejected.map((r) => ({ ...r })),
    flushes: state.flushes.map((f) => ({ ...f })),
    records: state.records.map((r) => ({ ...r })),
    fixtureNoted: state.fixtureNoted,
    readComplete: state.readComplete,
  };
}

function publish() {
  const debug = debugHandle();
  if (debug) debug.events = eventsSnapshot();
}

function degrade(kind, message) {
  if (typeof state.degradeFn === 'function') state.degradeFn(kind, message);
  else if (typeof state.bannerFn === 'function') state.bannerFn(message, 'warn');
}

function clearTimer() {
  if (state.timer) {
    globalThis.clearTimeout(state.timer);
    state.timer = null;
  }
}

/** 距首条入缓冲 15 秒到点就 flush（`setTimeout` 只在有缓冲时排一次）。 */
function schedule() {
  if (state.timer || !state.buffer.length) return;
  state.timer = globalThis.setTimeout(() => {
    state.timer = null;
    flushEvents('interval');
  }, FLUSH_INTERVAL_MS);
}

function recordFlush(reason, count, status, extra = {}) {
  state.flushes.push({ reason, count, status, at: Date.now(), ...extra });
  publish();
}

/**
 * 入一条事件。`kind` 必须在白名单里；`payload` 里的键原样并进事件体
 * （`paragraph` / `ms` / `explain_kind` / `scope` / `cached` / `degraded` / `ok` / `reason` …）。
 *
 * @returns {object|null} 入缓冲的那条事件（白名单外返回 null，并记进 `rejected`）
 */
export function emitEvent(kind, payload = {}) {
  if (EVENT_KINDS.indexOf(kind) === -1) {
    state.rejected.push({ kind: String(kind), at: Date.now() });
    publish();
    return null;
  }
  const record = { v: 1, id: eventId(), at: isoWithOffset(), kind };
  if (state.sourceId) record.source_id = state.sourceId;
  Object.keys(payload || {}).forEach((key) => {
    const value = payload[key];
    if (value !== undefined && value !== null) record[key] = value;
  });

  state.buffer.push(record);
  state.records.push({ ...record });
  if (state.records.length > RECORD_CAP) state.records.splice(0, state.records.length - RECORD_CAP);
  state.kinds[kind] = (state.kinds[kind] || 0) + 1;
  if (typeof record.paragraph === 'string') state.lastParagraph = record.paragraph;
  publish();

  if (state.buffer.length >= FLUSH_THRESHOLD) flushEvents('threshold');
  else schedule();
  return record;
}

/**
 * 把缓冲送出去。同一时刻只允许一个在飞请求（`inflight` 就是这把锁）：
 * `preFlush` 里若又入了一条事件而触发阈值 flush，那次会直接让路，不会并发发两批。
 *
 * @param {string} reason `threshold` / `interval` / `visibilitychange` / `pagehide` / `manual`
 */
export async function flushEvents(reason = 'manual') {
  clearTimer();
  if (state.inflight) return { ok: false, written: 0, reason: 'inflight' };
  state.inflight += 1;
  publish();
  try {
    // 强制 flush 之前先让调用方结算现场（读完最后一段的 dwell / 记 read_abandon）
    if (typeof state.preFlush === 'function') {
      try {
        state.preFlush(reason);
      } catch (err) {
        // 结算失败不该挡住上报
        state.rejected.push({ kind: 'preflush', at: Date.now(), message: String((err && err.message) || err) });
      }
    }
    const batch = state.buffer.splice(0, state.buffer.length);
    if (!batch.length) {
      recordFlush(reason, 0, 'empty');
      return { ok: true, written: 0, reason: 'empty' };
    }
    state.attempted += batch.length;

    if (state.mode !== 'api' || typeof state.api !== 'function') {
      state.dropped += batch.length;
      if (!state.fixtureNoted) {
        state.fixtureNoted = true;
        degrade('events-fixture', FIXTURE_NOTE);
      }
      recordFlush(reason, batch.length, 'fixture');
      return { ok: false, written: 0, reason: 'fixture', dropped: batch.length };
    }

    try {
      const { payload, url } = await state.api(state.ctx, batch);
      const response = payload || {};
      const written = Number(response.written || 0);
      const degradedList = Array.isArray(response.degraded) ? response.degraded.filter(Boolean) : [];
      if (response.ok !== true) {
        state.failed += batch.length;
        const why = degradedList.join('；') || '后端没有写成功';
        degrade('events', `行为信号没能记下（${batch.length} 条，${url}）：${why}。阅读不受影响。`);
        recordFlush(reason, batch.length, 'failed', { response });
        return { ok: false, written, reason: 'failed' };
      }
      state.sent += written;
      if (degradedList.length) {
        degrade('events', `行为信号有降级（${batch.length} 条）：${degradedList.join('；')}。`);
      }
      recordFlush(reason, batch.length, 'sent', { url, response });
      return {
        ok: true, written, duplicates: Number(response.duplicates || 0), total: Number(response.total || 0),
      };
    } catch (err) {
      state.failed += batch.length;
      degrade('events',
        `行为信号没能送出去（${batch.length} 条）：${(err && err.message) || err}。阅读不受影响。`);
      recordFlush(reason, batch.length, 'error', { message: String((err && err.message) || err) });
      return { ok: false, written: 0, reason: 'error' };
    }
  } finally {
    state.inflight -= 1;
    publish();
  }
}

/** 读完（滚到底）之后就不再记 `read_abandon`。 */
export function setReadComplete(value) {
  state.readComplete = Boolean(value);
  publish();
}

/**
 * 挂上事件模块。
 *
 * @param {{ctx:object, api:Function, sourceId?:string, banner?:Function,
 *          degrade?:Function, preFlush?:Function}} opts
 *   - `api`：`api.js` 的 `postEvents`（**不是** explain / compress）；
 *   - `preFlush(reason)`：强制 flush 之前结算现场（`main.js` 用来记最后一段的 dwell 与 read_abandon）；
 *   - `degrade(kind, message)` / `banner(text, kind)`：可见降级出口（沿用 `main.js` 那两个）。
 */
export function initEvents({ ctx, api, sourceId = null, banner = null, degrade: degradeFn = null, preFlush = null } = {}) {
  state.ctx = ctx || null;
  state.api = api || null;
  state.mode = ctx ? ctx.mode : null;
  state.sourceId = sourceId || (ctx ? ctx.sourceId : null) || null;
  state.bannerFn = banner;
  state.degradeFn = degradeFn;
  state.preFlush = preFlush;
  publish();

  if (!state.listenersMounted) {
    state.listenersMounted = true;
    // 转隐藏 / 卸载前强刷一次（这两次最要紧，别让用户读完的东西停在内存里）
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') flushEvents('visibilitychange');
    });
    globalThis.addEventListener('pagehide', () => {
      flushEvents('pagehide');
    });
  }
  return state;
}
