/**
 * 通知中心（顶栏「通知」按钮 + 面板，2026-10-24）。
 *
 * 为什么要这个文件：横幅（`js/view.js` 的 `addBanner`）会自动消失、也能被读者自己关掉，
 * 关掉之后那句话就**彻底没了**——读者想再看一眼刚才那条降级写的是什么，没有任何出路。
 * 这里补一个**纯内存**的通知中心：横幅消失 / 被关掉时把条目「收起」进历史，顶栏那颗按钮
 * 带未读数，点开可回看，点条目能把那条提示在页面上**原样重弹一次**。
 *
 * 纪律（与 `js/status.js` / `js/profile.js` 同一套）：
 *   - **只存内存**：不落 localStorage、不新增后端接口。页面刷新后清空是可接受的
 *     （历史是「刚才那几条」，不是需要跨会话留档的数据）；
 *   - **层在 body 层**：宿主 `[data-role="notify-host"]` 紧挨着 `.banners`，**不在顶栏里**——
 *     顶栏有 `backdrop-filter` 与 `z-index: 40` 的层叠上下文，`position: fixed` 的后代会被
 *     裁在顶栏那一行里，还会被抽屉（45）与卡片层（60）压住。面板的 `z-index` 取 71 一档
 *     （问卷层 70 之上、状态面板 72 之下，见 `css/reader.css`）；
 *   - **指针事件**：层 `pointer-events: none`（正文照常可点可滚），只有卡片自己收事件；
 *     点卡片之外的地方由页面关掉它（阅读器页在 `js/main.js` 的 document click 里，
 *     启动页在 `js/launcher.js` 的 `wireNotify`，两处都调 `panelContains()` 判内外）；
 *   - **键盘**：`Esc` 关闭（页面级监听，问卷开着时让路给问卷，一次 `Esc` 只关一层），
 *     关闭后焦点回到入口；入口是原生 `button`，`:focus-visible` 有可见轮廓；
 *   - **不反向依赖**：本文件不 import `view.js`（否则与「`view.js` 记条目」形成循环依赖）。
 *     点条目要重弹横幅时，由页面在 `mountNotify({ replay })` 里把 `addBanner` 传进来。
 *
 * 时间：横幅的失效时刻用「倒计时文字 + `data-expires-at`（epoch ms）+ `title` 绝对本地时刻」
 * 三通道（倒计时与 `title` 在 `view.js` 里刷新，这里只提供绝对时刻的格式化与历史条目的
 * 「何时收起的」）。格式化照 `js/events.js` 的 `isoWithOffset` 的本地带偏移算法。
 */

/** 内存里最多留多少条（够回看，不无限涨）。 */
const MAX_ITEMS = 60;

const state = {
  items: [],        // 新的在前：{id, text, state, createdAt, expiresAt, collapsed, collapsedAt, reason, seen}
  seq: 0,           // 没有 id 时的兜底序号
  host: null,       // [data-role="notify-host"]
  toggle: null,     // 顶栏那颗「通知」按钮
  layer: null,      // 遮罩层（pointer-events: none）
  panel: null,      // 面板卡片
  open: false,
  focusBefore: null,
  replay: null,     // (item) => void：点历史条目时由页面把横幅原样重弹一次
  listeners: new Set(),
};

function debugHandle() {
  return globalThis.__arDebug || null;
}

/** 一条通知的可断言快照（`__arDebug.notifications.items` 的元素）。 */
function snapshotItem(item) {
  return {
    id: item.id,
    text: item.text,
    state: item.state,
    createdAt: item.createdAt,
    expiresAt: item.expiresAt,
    collapsed: item.collapsed,
    collapsedAt: item.collapsedAt,
    reason: item.reason,
    seen: item.seen,
  };
}

/**
 * 本地时区、带偏移的绝对时刻（`2026-10-24 15:04:32 +08:00`）。
 *
 * 算法照 `js/events.js` 的 `isoWithOffset`（同一份偏移计算），只把 `T` 换成空格——
 * 横幅悬停的 `title` 与历史条目里的时刻都走它，两处显示同一个瞬间必须同一口径。
 */
export function absoluteLocalStamp(ms = Date.now()) {
  const value = Number(ms);
  const date = new Date(Number.isFinite(value) ? value : Date.now());
  const pad = (part, width = 2) => String(Math.abs(part)).padStart(width, '0');
  const offsetMinutes = -date.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? '+' : '-';
  const stamp = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  const offset = `${sign}${pad(Math.floor(Math.abs(offsetMinutes) / 60))}:${pad(Math.abs(offsetMinutes) % 60)}`;
  return `${stamp} ${offset}`;
}

/** 历史条目里那一小行时刻只留时分秒（日期在「刚刚」的语境里没有信息量）。 */
function clockOf(ms) {
  const date = new Date(Number(ms) || Date.now());
  const pad = (part) => String(part).padStart(2, '0');
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** 当前全部条目的快照（`__arDebug.notifications` 就是它）。 */
export function snapshot() {
  return { open: state.open, unread: unread(), items: state.items.map(snapshotItem) };
}

/**
 * 把状态推给两处订阅者：`__arDebug.notifications`（无头验证等它）与已挂的界面监听。
 * 订阅者自己的异常不许把 store 带崩——历史是只读服务，谁用坏了是它自己的事。
 */
function publishStore() {
  const payload = snapshot();
  const debug = debugHandle();
  if (debug) debug.notifications = payload;
  state.listeners.forEach((fn) => {
    try { fn(payload); } catch (err) { /* 订阅者自己的错 */ }
  });
}

/* --------------------------------------------------------------- 纯内存 store */

/**
 * 记一条通知（横幅造出来时调一次）。返回条目本体（调用方只读）。
 * `id` 由 `view.js` 给（与横幅的 `data-banner-id` 同一个），这样横幅消失时能按 id 收起它。
 */
export function record({ id = '', text = '', bannerState = 'warn', expiresAt = 0, createdAt = Date.now() } = {}) {
  state.seq += 1;
  const item = {
    id: id || `n${state.seq}`,
    text: String(text == null ? '' : text),
    state: bannerState,
    createdAt,
    expiresAt: Number(expiresAt) || 0,
    collapsed: false,
    collapsedAt: null,
    reason: null,
    seen: false,
  };
  state.items.unshift(item);
  if (state.items.length > MAX_ITEMS) state.items = state.items.slice(0, MAX_ITEMS);
  publishStore();
  return item;
}

/** 历史快照（**拷贝**：调用方改不动 store）。 */
export function list() {
  return state.items.map(snapshotItem);
}

/**
 * 未读数 = **已经收起**（横幅不在页面上了）且还没在面板里看过的条数。
 * 还挂在页面上的横幅不算未读：它就在读者眼前，再点一个红点提醒反而多余。
 */
export function unread() {
  return state.items.filter((item) => item.collapsed && !item.seen).length;
}

/**
 * 把一条通知标成「已收起」（横幅从 DOM 摘掉时调）。
 * `reason`：`timeout`（到点自己走）或 `close`（读者点 ×）——历史条目靠它说清怎么没的。
 */
export function markCollapsed(id, reason = 'close', at = Date.now()) {
  const item = id ? state.items.find((entry) => entry.id === id) : null;
  if (!item || item.collapsed) return null;
  item.collapsed = true;
  item.collapsedAt = at;
  item.reason = reason;
  if (state.open) item.seen = true;       // 面板正开着：这条就在读者眼前，不算未读
  publishStore();
  return item;
}

/** 订阅 store 变化（返回退订函数）。界面（徽标 / 列表）就挂在这上面。 */
export function subscribe(fn) {
  if (typeof fn !== 'function') return () => {};
  state.listeners.add(fn);
  return () => state.listeners.delete(fn);
}

/* --------------------------------------------------------------- 造 DOM */

function el(doc, tag, className, role, text) {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (role) node.dataset.role = role;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

/** 历史条目要的那行小字：「自动收起 · 15:04:32」/「手动关闭 · 15:04:32」。 */
function metaText(item) {
  const how = item.reason === 'timeout' ? '自动收起' : '手动关闭';
  const when = item.collapsedAt ? clockOf(item.collapsedAt) : '';
  return when ? `${how} · ${when}` : how;
}

function itemRow(doc, item) {
  const row = doc.createElement('button');
  row.type = 'button';
  row.className = 'notify__item';
  row.dataset.role = 'notify-item';
  row.dataset.id = item.id;
  row.dataset.state = item.state;
  row.dataset.collapsedBy = item.reason || '';
  row.title = '点一下：在页面上把这条提示原样重弹一次';
  row.appendChild(el(doc, 'span', 'notify__item-text', 'notify-item-text', item.text));
  row.appendChild(el(doc, 'span', 'notify__item-meta', 'notify-item-meta', metaText(item)));
  return row;
}

/** 面板空壳（结构固定，内容由 `renderPanel` 填）。导出只为让无头用例能直接验结构。 */
export function buildNotifyPanel(doc = document) {
  const panel = doc.createElement('div');
  panel.className = 'notify';
  panel.id = 'notify-panel';
  panel.dataset.role = 'notify-panel';
  panel.tabIndex = -1;                       // 打开时焦点落这里（不进入 Tab 序）
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'false');
  panel.setAttribute('aria-label', '通知：收起或失效的提示');

  const head = el(doc, 'div', 'notify__head');
  head.appendChild(el(doc, 'span', 'notify__title', 'notify-title', '通知'));
  const close = el(doc, 'button', 'notify__close', 'notify-close', '×');
  close.type = 'button';
  close.setAttribute('aria-label', '关闭通知面板');
  head.appendChild(close);
  panel.appendChild(head);

  // 列表用 div 而不是 ul：条目本身是原生 button（可点、可 Tab），ul 的直接子元素不该是按钮
  panel.appendChild(el(doc, 'div', 'notify__list', 'notify-list'));
  panel.appendChild(el(doc, 'div', 'notify__empty', 'notify-empty',
    '还没有通知。收起的提示会留在这里。'));
  panel.appendChild(el(doc, 'div', 'notify__foot', 'notify-foot', ''));
  return panel;
}

export function buildNotifyLayer(doc = document) {
  const layer = doc.createElement('div');
  layer.className = 'notify-layer';
  layer.dataset.role = 'notify-layer';
  layer.appendChild(buildNotifyPanel(doc));
  return layer;
}

/** 只把**已收起**的条目画进面板（还挂在页面上的横幅不算历史）。 */
function historyItems() {
  return state.items.filter((item) => item.collapsed);
}

function renderPanel() {
  const panel = state.panel;
  if (!panel) return;
  const listNode = panel.querySelector('[data-role="notify-list"]');
  const empty = panel.querySelector('[data-role="notify-empty"]');
  const foot = panel.querySelector('[data-role="notify-foot"]');
  const items = historyItems();
  if (listNode) {
    listNode.textContent = '';
    items.forEach((item) => listNode.appendChild(itemRow(panel.ownerDocument || document, item)));
  }
  if (empty) empty.hidden = items.length > 0;
  if (foot) foot.textContent = items.length ? `已收起 ${items.length} 条` : '';
}

/* --------------------------------------------------------------- 开关 */

/** 面板此刻是否展开（页面级的 Esc / 点外部判定读它，与 `isSurveyOpen` 同一口径）。 */
export function isNotifyOpen() {
  const debug = debugHandle();
  if (debug && debug.notifications && typeof debug.notifications.open === 'boolean') {
    return debug.notifications.open;
  }
  return state.open;
}

/** 事件目标是面板自己吗（页面判「点外部」用；面板没开时恒为 false）。 */
export function panelContains(node) {
  return Boolean(state.panel && node && state.panel.contains(node));
}

export function openNotifyPanel() {
  if (state.open) return state.panel;
  state.focusBefore = document.activeElement && document.activeElement !== document.body
    ? document.activeElement
    : null;
  state.open = true;
  state.items.forEach((item) => { item.seen = true; });   // 打开即读：未读清零

  const host = state.host || document.getElementById('notify-host') || document.body;
  state.host = host;
  const layer = buildNotifyLayer(document);
  state.layer = layer;
  state.panel = layer.querySelector('[data-role="notify-panel"]');
  host.appendChild(layer);
  renderPanel();
  state.panel.focus({ preventScroll: true });
  if (state.toggle) state.toggle.setAttribute('aria-expanded', 'true');
  if (document.body && document.body.dataset) document.body.dataset.notify = 'open';
  publishStore();
  return state.panel;
}

/**
 * 关掉面板并清干净现场：整层从 DOM 移除（不留残影），焦点回到入口。
 *
 * `reason` 只给调用方表达语义（`escape` / `outside` / `toggle` / `close-button` / `replay`），
 * 面板自己不靠它分支——五种出口做的事完全一样。`replay` 也一样：重弹之后把面板让开，
 * 读者才看得见刚弹回来的那条横幅。
 */
export function closeNotifyPanel(reason = 'unknown') {
  if (!state.open) {
    // 没开也算「已关闭」：重复调用不许有副作用（页面级监听可能被调两次）
    const debug = debugHandle();
    if (debug && !debug.notifications) debug.notifications = snapshot();
    return;
  }
  state.open = false;
  if (state.layer && state.layer.parentNode) state.layer.parentNode.removeChild(state.layer);
  state.layer = null;
  state.panel = null;
  if (state.toggle) state.toggle.setAttribute('aria-expanded', 'false');
  if (document.body && document.body.dataset) document.body.dataset.notify = 'closed';
  publishStore();
  const target = state.toggle && state.toggle.isConnected
    ? state.toggle
    : (state.focusBefore && state.focusBefore.isConnected ? state.focusBefore : null);
  state.focusBefore = null;
  if (target && typeof target.focus === 'function') {
    try { target.focus({ preventScroll: true }); } catch (err) { /* 焦点回不去也不报错 */ }
  }
  return;
}

export function toggleNotifyPanel() {
  if (state.open) closeNotifyPanel('toggle');
  else openNotifyPanel();
}

/** 点历史条目：把这条提示交给页面原样重弹一次（`replay` 由 `mountNotify` 注入）。 */
function replayItem(id) {
  const item = id ? state.items.find((entry) => entry.id === id) : null;
  if (!item) return null;
  if (state.replay) state.replay(snapshotItem(item));
  closeNotifyPanel('replay');
  return item;
}

/* --------------------------------------------------------------- 徽标 */

function syncBadge(count) {
  const toggle = state.toggle;
  if (!toggle) return;
  const badge = toggle.querySelector('[data-role="notify-count"]');
  const text = count > 99 ? '99+' : String(count);
  if (badge) {
    badge.textContent = text;
    badge.dataset.unread = String(count);          // CSS：0 时整颗隐藏
  }
  toggle.dataset.unread = String(count);           // 按钮上也留一份，便于断言
  toggle.title = count
    ? `通知（${count} 条未读；收起的提示会留在这里）`
    : '通知（收起的提示会留在这里）';
}

/* --------------------------------------------------------------- 启动 */

/**
 * 把通知中心接到顶栏入口上。
 *
 * `replay` 是页面注入的「重弹一次」：阅读器/启动页都传
 * `(item) => addBanner(容器, item.text, item.state, { record: false })`（`view.js`；`record: false`
 * 是为了不在历史里翻倍）。**本文件不 import view.js**，否则与「`view.js` 记条目」形成循环依赖。
 */
export function mountNotify({ host, toggle, replay } = {}) {
  state.host = host || document.getElementById('notify-host') || document.body;
  state.toggle = toggle || document.querySelector('[data-role="notify-toggle"]');
  state.replay = typeof replay === 'function' ? replay : null;

  if (state.toggle) {
    state.toggle.addEventListener('click', (event) => {
      event.stopPropagation();       // 别让页面的「点外部关闭」把刚打开的面板又关掉
      toggleNotifyPanel();
    });
  }
  if (state.host) {
    state.host.addEventListener('click', (event) => {
      event.stopPropagation();       // 面板自己的点击不归页面的「点外部」管
      const role = event.target && event.target.dataset ? event.target.dataset.role : null;
      if (role === 'notify-close') {
        closeNotifyPanel('close-button');
        return;
      }
      const row = event.target.closest ? event.target.closest('[data-role="notify-item"]') : null;
      if (row) replayItem(row.dataset.id || '');
    });
  }
  subscribe((payload) => { syncBadge(payload.unread); });
  syncBadge(unread());
  if (state.toggle) state.toggle.setAttribute('aria-expanded', state.open ? 'true' : 'false');
  if (document.body && document.body.dataset) document.body.dataset.notify = 'closed';
  publishStore();                    // 把 {open, unread, items} 摆到 __arDebug 上（无头验证等它）
  return { open: openNotifyPanel, close: closeNotifyPanel, toggle: toggleNotifyPanel, isOpen: isNotifyOpen };
}
