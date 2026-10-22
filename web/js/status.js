/**
 * 启动页顶栏的「测试和修改」面板（2026-10-09 新增，2026-10-20 从主栏那张卡搬进顶栏）。
 *
 * 两块内容：① 测试结论（后端 / 前端两套的 passed / failed / skipped + 耗时）② 本次修改
 * （note 一句话 + 汇总 + 文件清单）。**数据源只有一个**：同源静态文件 `/status.json`
 * （由 `tools/gen_status.py` 生成，后端把它挂在 `/` 上，与 `/api/*` 同源）。
 * 本文件里**没有任何数字**——读不到 / 读坏了就走「尚未生成」的降级文案，
 * 不空白、不假装成功（`web/README.md` 的「降级纪律」）。
 *
 * 入口在顶栏（`data-role="status-toggle"`，按钮文字「测试和修改」）；原来主栏那张
 * ① 状态卡里的说明文字与「报告读不到时…」备注都已并入**面板内部**（见 `buildStatusCard`
 * 的 lead），主栏不再有那一块。
 *
 * 形态与纪律（与 `survey.js` 同一套视觉语言与指针模型）：
 *   - 层 `.status-layer` 与卡片 `.status` 都是 `pointer-events: none`，只有关闭按钮要回 auto
 *     ——面板打开时正文照常可滚、可点、可选字；
 *   - **滚轮归面板**：滚动区（`.status__body`）保持 `pointer-events: none`，命中测试会跳过它，
 *     所以滚轮在**文档捕获阶段**按**坐标命中判定**接管（`handleWheel`，与 `survey.js` 同一口径）：
 *     指针在卡片矩形里就 `preventDefault()` 并把 `deltaY` 给滚动区（滚到边界也不放给页面）；
 *     指针在层上别处一律不插手（正文照常滚）。滚动区另有 `overscroll-behavior: contain` 兜底。
 *   - **键盘**：`Esc` 关闭（文档级监听：焦点可能已经不在面板上）；关闭后整层从 DOM 移除、
 *     焦点回到入口（`focusBefore`）；入口是原生 `button`，`:focus-visible` 有可见轮廓。
 *   - 每次打开都重新取一次 `/status.json`（`cache: "no-store"`）：重新生成报告后点一下就看到新的数字。
 */

import { isSurveyOpen } from './survey.js';

/** 报告地址：同源静态文件（正式服务把 `web/` 挂在 `/` 上；夹具模式的静态服务同样按站点根取）。 */
export const STATUS_URL = '/status.json';

/** 报告没生成时给读者看的命令（与 `tools/gen_status.py` 的用法一致，不写第二份说明）。 */
export const STATUS_COMMAND = 'uv run python tools/gen_status.py';

/** git 状态码 → 读者看得懂的名字（`??` 是未跟踪＝新文件）。 */
const STATUS_LABELS = {
  '??': '新文件', M: '改动', A: '新增', D: '删除', R: '改名', C: '复制', T: '类型变化', U: '冲突',
};

const state = {
  host: null,             // #status-host
  layer: null,            // 遮罩层（pointer-events: none）
  card: null,
  toggle: null,           // 顶栏入口
  open: false,
  payload: null,          // 成功读到的报告（降级时为 null）
  reportState: 'closed',  // closed | loading | ok | error | degraded
  error: null,            // 降级原因（可读文案）
  degraded: [],           // 可见降级清单（`__arDebug.statusDegraded` 的来源）
  events: [],
  focusBefore: null,
  seq: 0,                 // 每次打开的请求序号：关掉再打开时丢弃迟到响应
};

function debugHandle() {
  return globalThis.__arDebug || null;
}

function emit(type, detail = {}) {
  state.events.push({ type, at: Date.now(), ...detail });
  const debug = debugHandle();
  if (debug) debug.statusEvents = state.events.map((e) => ({ ...e }));
}

/** 降级只落在面板里（读者点开就看到）+ `__arDebug.statusDegraded`；不另发横幅——面板本身就是那条可见出口。 */
function note(message) {
  state.degraded.push({ kind: 'status-report', message });
  const debug = debugHandle();
  if (debug) debug.statusDegraded = state.degraded.map((d) => ({ ...d }));
}

function setReportState(name) {
  state.reportState = name;
  if (state.card) state.card.dataset.state = name;
  const debug = debugHandle();
  if (debug) debug.statusState = name;
}

function setOpen(open) {
  state.open = Boolean(open);
  const debug = debugHandle();
  if (debug) debug.statusOpen = state.open;
  if (typeof document !== 'undefined' && document.body && document.body.dataset) {
    document.body.dataset.status = state.open ? 'open' : 'closed';
  }
  if (state.toggle) state.toggle.setAttribute('aria-expanded', state.open ? 'true' : 'false');
}

/** 面板此刻是否展开（main.js 的 Esc 让路判定读它，与 `isSurveyOpen` 同一口径）。 */
export function isStatusOpen() {
  const debug = debugHandle();
  if (debug && typeof debug.statusOpen === 'boolean') return debug.statusOpen;
  return state.open;
}

/* --------------------------------------------------------------- 造 DOM */

function el(doc, tag, className, role, text) {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (role) node.dataset.role = role;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

/**
 * 造面板卡片（不含遮罩层）。导出只为让无头用例能直接拿它验渲染，不必先开网。
 * @returns {HTMLDivElement} 带 `data-role="status-card"`
 */
export function buildStatusCard(doc = document) {
  const card = doc.createElement('div');
  card.className = 'status';
  card.dataset.role = 'status-card';
  card.dataset.state = 'loading';
  card.tabIndex = -1;                                  // 打开时焦点落这里（不进入 Tab 序）
  card.setAttribute('role', 'dialog');
  card.setAttribute('aria-modal', 'false');
  card.setAttribute('aria-label', '测试和修改：测试结论与本次修改');

  const head = el(doc, 'div', 'status__head');
  head.appendChild(el(doc, 'span', 'status__title', 'status-title', '测试和修改'));
  head.appendChild(el(doc, 'span', 'status__stamp', 'status-generated-at', ''));
  const close = el(doc, 'button', 'status__close', 'status-close', '×');
  close.type = 'button';
  close.setAttribute('aria-label', '关闭「测试和修改」面板');
  head.appendChild(close);
  card.appendChild(head);

  // 这段并的是原来主栏那张「① 状态」卡里的说明文字与备注（2026-10-20 随卡片一起搬进面板）
  card.appendChild(el(doc, 'div', 'status__lead', 'status-lead',
    '两套测试的结论（后端 / 前端）与本次未提交的改动，由 tools/gen_status.py 生成，'
    + '页面只负责显示，一个数字都不硬编码。报告读不到时这里会写「尚未生成」+ 生成命令'
    + '（不空白、不假装成功）。'));

  card.appendChild(el(doc, 'div', 'status__body', 'status-body'));
  const foot = el(doc, 'div', 'status__foot');
  foot.appendChild(el(doc, 'span', 'status__foot-note', 'status-foot-note', '按 Esc 关闭。'));
  card.appendChild(foot);
  return card;
}

export function buildStatusLayer(doc = document) {
  const layer = doc.createElement('div');
  layer.className = 'status-layer';
  layer.dataset.role = 'status-layer';
  layer.appendChild(buildStatusCard(doc));
  return layer;
}

/* --------------------------------------------------------------- 渲染 */

function bodyOf(card) {
  return card ? card.querySelector('[data-role="status-body"]') : null;
}

function secondsText(seconds) {
  const value = Number(seconds);
  return Number.isFinite(value) ? `${value.toFixed(1)} 秒` : '耗时未知';
}

function suiteRow(doc, name, label, suite) {
  const row = el(doc, 'div', 'status__suite', 'status-suite');
  row.dataset.suite = name;

  const head = el(doc, 'div', 'status__suite-head');
  head.appendChild(el(doc, 'span', 'status__suite-name', 'status-suite-name', `${label} · ${suite.command}`));
  row.appendChild(head);

  if (suite.ran === false) {
    // 这次没跑（--skip-web / 找不到解释器）：写清楚，不许拿假数字冒充
    row.dataset.state = 'skip';
    row.appendChild(el(doc, 'div', 'status__suite-note', 'status-suite-note',
      `本次没跑：${suite.reason || '原因未知'}`));
    return row;
  }

  const failed = Number(suite.failed) || 0;
  const errors = Number(suite.errors) || 0;
  const ok = suite.ok !== false && failed === 0 && errors === 0;
  row.dataset.state = ok ? 'ok' : 'error';

  const counts = el(doc, 'div', 'status__suite-counts', 'status-suite-counts',
    `通过 ${Number(suite.passed) || 0} / 失败 ${failed} / 跳过 ${Number(suite.skipped) || 0}`
    + (errors ? ` / 错误 ${errors}` : ''));
  row.appendChild(counts);
  row.appendChild(el(doc, 'div', 'status__suite-seconds', 'status-suite-seconds',
    secondsText(suite.seconds)));
  if (suite.summary) {
    row.appendChild(el(doc, 'div', 'status__suite-summary', 'status-suite-summary', suite.summary));
  }
  return row;
}

function testsSection(doc, tests) {
  const section = el(doc, 'section', 'status__section', 'status-tests');
  section.appendChild(el(doc, 'h3', 'status__heading', 'status-tests-heading', '① 测试结论'));
  const known = [['backend', '后端'], ['web', '前端']];
  known.forEach(([name, label]) => {
    const suite = tests ? tests[name] : null;
    if (!suite || typeof suite !== 'object') {
      const row = el(doc, 'div', 'status__suite', 'status-suite', '报告里没有这一套的结论。');
      row.dataset.suite = name;
      row.dataset.state = 'skip';
      section.appendChild(row);
      return;
    }
    section.appendChild(suiteRow(doc, name, label, suite));
  });
  return section;
}

/** 一个文件条目：`[状态码] [名字] 路径 +增 −删`（顺序也是 inner_text 的断言口径）。 */
function fileRow(doc, file) {
  const row = el(doc, 'li', 'status__file', 'status-file');
  row.dataset.status = String(file.status || '?');
  if (file.untracked) row.dataset.untracked = 'true';
  row.appendChild(el(doc, 'code', 'status__file-code', 'status-file-code', String(file.status || '?')));
  row.appendChild(el(doc, 'span', 'status__file-label', 'status-file-label', statusLabel(file.status)));
  row.appendChild(el(doc, 'span', 'status__file-path', 'status-file-path', String(file.path || '')));
  row.appendChild(el(doc, 'span', 'status__file-lines', 'status-file-lines',
    `+${Number(file.insertions) || 0} −${Number(file.deletions) || 0}`));
  return row;
}

function statusLabel(code) {
  const raw = String(code || '');
  if (STATUS_LABELS[raw]) return STATUS_LABELS[raw];
  const parts = raw.split('').map((ch) => STATUS_LABELS[ch]).filter(Boolean);
  return parts.length ? [...new Set(parts)].join('+') : `git 状态 ${raw}`;
}

function changesSection(doc, changes) {
  const section = el(doc, 'section', 'status__section', 'status-changes');
  section.appendChild(el(doc, 'h3', 'status__heading', 'status-changes-heading', '② 本次修改'));

  if (changes.error) {
    section.appendChild(el(doc, 'div', 'status__warn', 'status-changes-error',
      `改动清单拿不到：${changes.error}`));
  }
  if (changes.note) {
    section.appendChild(el(doc, 'div', 'status__note', 'status-note', String(changes.note)));
  }
  if (changes.head) {
    const line = el(doc, 'div', 'status__head-line', 'status-head');
    line.appendChild(el(doc, 'span', 'status__head-label', null, '起点提交：'));
    line.appendChild(el(doc, 'code', 'status__head-code', null, String(changes.head)));
    section.appendChild(line);
  }

  const totals = changes.totals || {};
  section.appendChild(el(doc, 'div', 'status__totals', 'status-totals',
    `${Number(totals.files) || 0} 个文件 / +${Number(totals.insertions) || 0} `
    + `−${Number(totals.deletions) || 0}`));

  const files = Array.isArray(changes.files) ? changes.files : [];
  if (!files.length) {
    section.appendChild(el(doc, 'div', 'status__empty', 'status-files-empty',
      '没有未提交的改动（工作区与这份提交一致）。'));
    return section;
  }
  const list = el(doc, 'ul', 'status__files', 'status-files');
  files.forEach((file) => list.appendChild(fileRow(doc, file)));
  section.appendChild(list);
  return section;
}

/** 报告读不到 / 坏掉 / 字段不认识时的可见降级文案（给读者看的，不出现测试结论）。 */
function missingBlock(doc, reason) {
  const box = el(doc, 'div', 'status__missing', 'status-missing');
  box.appendChild(el(doc, 'div', 'status__missing-title', 'status-missing-title',
    '状态报告尚未生成'));
  box.appendChild(el(doc, 'div', 'status__missing-body', 'status-missing-body',
    `在项目根跑 ${STATUS_COMMAND}，它会把两套测试的结论与当前未提交的改动写进 web/status.json，`
    + '刷新本页即可看到。'));
  if (reason) {
    box.appendChild(el(doc, 'div', 'status__missing-reason', 'status-missing-reason',
      `这次没读到它：${reason}`));
  }
  return box;
}

function renderMissing(reason) {
  state.payload = null;
  const debug = debugHandle();
  if (debug) debug.statusPayload = null;
  state.error = reason;
  if (debug) debug.statusError = reason;
  note(reason);
  const body = bodyOf(state.card);
  if (!body) return;
  body.textContent = '';
  body.appendChild(missingBlock(document, reason));
  setReportState('degraded');
  emit('degrade', { reason });
}

/** 把报告渲染进面板（`state.card` 已挂好）。 */
export function renderReport(payload) {
  const card = state.card;
  const body = bodyOf(card);
  if (!body) return;
  state.payload = payload;
  state.error = null;
  const debug = debugHandle();
  if (debug) {
    debug.statusPayload = payload;
    debug.statusError = null;
  }
  body.textContent = '';
  const stamp = card.querySelector('[data-role="status-generated-at"]');
  if (stamp) stamp.textContent = payload.generated_at ? `生成于 ${payload.generated_at}` : '';
  body.appendChild(testsSection(document, payload.tests));
  body.appendChild(changesSection(document, payload.changes || {}));
  const failed = ['backend', 'web'].some((name) => {
    const suite = payload.tests ? payload.tests[name] : null;
    return Boolean(suite && typeof suite === 'object'
      && (Number(suite.failed) || Number(suite.errors) || suite.ok === false));
  });
  setReportState(failed ? 'error' : 'ok');
  emit('render', { state: state.reportState });
}

/* --------------------------------------------------------------- 打开 / 关闭 */

function clearLayer() {
  if (state.layer && state.layer.parentNode) state.layer.parentNode.removeChild(state.layer);
  state.layer = null;
  state.card = null;
}

function reasonOf(err, response) {
  if (response && !response.ok) return `HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ''}`;
  return String((err && err.message) || err || '未知原因');
}

function usable(payload) {
  return Boolean(payload && typeof payload === 'object'
    && payload.tests && typeof payload.tests === 'object'
    && payload.changes && typeof payload.changes === 'object');
}

/**
 * 打开面板：每次重新取一次 `/status.json`（`no-store`），取不到 / 坏掉就在面板里写「尚未生成」。
 * 取数期间面板已可读（loading 占位），失败不抛给调用方（错误已在面板里可见）。
 */
export async function openStatusPanel() {
  if (state.open) return state.card;
  const debug = debugHandle();
  state.focusBefore = document.activeElement && document.activeElement !== document.body
    ? document.activeElement
    : null;
  setOpen(true);
  emit('open', {});
  state.seq += 1;
  const token = state.seq;

  const host = state.host || document.getElementById('status-host') || document.body;
  state.host = host;
  clearLayer();
  const layer = buildStatusLayer();
  state.layer = layer;
  state.card = layer.querySelector('[data-role="status-card"]');
  host.appendChild(layer);
  setReportState('loading');
  const body = bodyOf(state.card);
  body.appendChild(el(document, 'div', 'status__loading', 'status-loading',
    '正在读取状态报告 web/status.json…'));
  state.card.focus({ preventScroll: true });

  let url = STATUS_URL;
  if (typeof location !== 'undefined' && location.href) {
    try { url = new URL(STATUS_URL, location.href).href; } catch (err) { url = STATUS_URL; }
  }
  if (debug) debug.statusUrl = url;

  try {
    const response = await fetch(STATUS_URL, { cache: 'no-store' });
    if (!response.ok) throw new Error(reasonOf(null, response));
    const payload = await response.json();
    if (token !== state.seq || !state.open) return;          // 关掉又打开：丢弃迟到响应
    if (!usable(payload)) {
      renderMissing('报告内容不认识（少了 tests / changes 这两块）。');
      return;
    }
    renderReport(payload);
    return state.card;
  } catch (err) {
    if (token !== state.seq || !state.open) return;
    renderMissing(reasonOf(err, null));
    return state.card;
  }
}

/**
 * 关掉面板并清干净现场：整层从 DOM 移除（不留残影），焦点回到入口（记着的那一个还在就用它，
 * 否则回正文栏——`#rewritten` 带 `tabindex="-1"`）。
 */
export function closeStatusPanel(reason = 'unknown') {
  if (!state.open) return;
  setOpen(false);
  emit('close', { reason, reportState: state.reportState });
  clearLayer();
  state.error = null;

  const before = state.focusBefore;
  state.focusBefore = null;
  if (before && before.isConnected && typeof before.focus === 'function') {
    try { before.focus(); return; } catch (err) { /* 落回正文栏 */ }
  }
  const article = document.getElementById('rewritten');
  if (article && typeof article.focus === 'function') {
    try { article.focus({ preventScroll: true }); } catch (err) { /* 焦点回不去也不报错 */ }
  }
}

export function toggleStatusPanel() {
  if (state.open) closeStatusPanel('toggle');
  else openStatusPanel();
}

/* --------------------------------------------------------------- 滚轮与 Esc */

/** 指针是不是落在这块矩形里（事件坐标与 `getBoundingClientRect()` 同为视口坐标）。 */
function insideRect(event, rect) {
  return event.clientX >= rect.left && event.clientX <= rect.right
    && event.clientY >= rect.top && event.clientY <= rect.bottom;
}

/**
 * 滚轮归属（与 `survey.js` 的 `handleWheel` 同一口径）。
 *
 * 滚动区必须留在 `pointer-events: none` 那一组里（面板不许吞正文点击，见文件头），
 * 而命中测试会**跳过** `pointer-events: none` 的元素——滚轮于是落到背后正文上。
 * 所以在**文档捕获阶段**收滚轮，再按**坐标命中判定**把那次被跳过命中测试补回来：
 * 指针在**卡片**矩形里 ⇒ `preventDefault()` + 把 `deltaY` 给滚动区（滚到边界也不放给页面）；
 * 指针在卡片之外（遮罩层上）⇒ 一律不插手，正文照常滚。
 * 导出只为让无头用例能直接喂一个滚轮事件验判定。
 */
export function handleWheel(event) {
  if (!state.open) return;
  const card = state.card;
  const body = bodyOf(card);
  if (!body || !card) return;
  if (!insideRect(event, card.getBoundingClientRect())) return;
  event.preventDefault();
  body.scrollTop += event.deltaY;
}

/**
 * `Esc` 的入口（文档级：焦点可能已经不在面板上，只听卡片会漏）。
 * 问卷也开着时让路给问卷（与 `main.js` 的既有权重一致：一次 `Esc` 只关一层）。
 */
function handleEscapeKey(event) {
  if (event.key !== 'Escape' || !state.open) return;
  if (isSurveyOpen()) return;
  event.preventDefault();
  closeStatusPanel('escape');
}

// 模块只求值一次，这两条注册天然幂等（不随面板开关反复挂/卸）。
if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
  document.addEventListener('keydown', handleEscapeKey);
  // `passive: false` 是硬要求，否则 `preventDefault()` 拦不住浏览器对正文的滚动。
  document.addEventListener('wheel', handleWheel, { capture: true, passive: false });
}

/* --------------------------------------------------------------- 启动 */

/** 把面板接到顶栏入口上（点击开关；`×` 在卡片事件里处理）。 */
export function mountStatus({ host, toggle } = {}) {
  state.host = host || document.getElementById('status-host') || document.body;
  state.toggle = toggle || document.querySelector('[data-role="status-toggle"]');
  if (state.toggle) {
    state.toggle.addEventListener('click', (event) => {
      event.stopPropagation();
      toggleStatusPanel();
    });
  }
  if (typeof document !== 'undefined' && document.body && document.body.dataset) {
    document.body.dataset.status = 'closed';
  }
  const debug = debugHandle();
  if (debug) {
    debug.statusOpen = false;
    debug.statusState = 'closed';
  }
  // `×` 的点击：卡片自己也收（stopPropagation 免得点关闭顺带被 main.js 当成「点外部」）
  if (state.host) {
    state.host.addEventListener('click', (event) => {
      const role = event.target && event.target.dataset ? event.target.dataset.role : null;
      if (role === 'status-close') closeStatusPanel('close-button');
      event.stopPropagation();
    });
  }
  return { open: openStatusPanel, close: closeStatusPanel, isOpen: isStatusOpen };
}
