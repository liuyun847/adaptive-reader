/**
 * 阅读器入口：读 URL → 取数据 → 渲染主栏/抽屉/卡片 → 挂交互。
 *
 * 界面纪律（DESIGN §5/§6）：
 *   - 主栏是唯一常驻内容；抽屉默认折叠；讲解只出卡片，不改动正文；
 *   - 任何降级（模型不可用、请求失败、段数对不上、没有概念清单）都必须在页面上留下可见文字，
 *     同时在 globalThis.__arDebug 里留下可断言的状态标志。无头验证只等 __arDebug.ready，不 sleep。
 *
 * 2026-10-08 这批改造（四件事，都在这一个文件里接线）：
 *   ① 讲解粒度从「整段」下探到「选中的文本片段」：选中文字 → 浮条 → 卡片标明片段与所属段；
 *   ② 阅读显示设置（背景 / 文字颜色 / 字号），渲染前应用、落 localStorage；
 *   ③ 读完提示从底部浮窗改成正文末尾的流内块；
 *   ④ 会话内讲解缓存 + 在飞去重 + 请求序号（堵住重复付费与迟到响应覆盖）。
 *
 * 同日复核后的三处补丁（也在这个文件里）：
 *   - 显示设置改完就量一次正文栏对比度，读不清时在设置面板里出可见告警（手动组合不拦，但要说）；
 *   - 会话缓存键补上画像指纹（每次讲解前刷一次 `GET /api/profile` 的摘要，画像一变键就变）；
 *   - 卡片状态区挪到底部固定区（view.js），长答案下也看得见。
 *
 * 2026-10-08 第二批（读前背景问卷，PLAN-background-survey.md §7 / §9）：
 *   正文渲染完成、`__arDebug.ready` 置位**之后**才问一句服务端画像要不要弹问卷——
 *   不挡阅读，正文渲染失败时压根不弹；「首次打开」的判定取 `GET /api/survey` 的
 *   `answers` / `skipped`（不读 localStorage，那个键是显示设置）。界面本身在 js/survey.js。
 *
 * 2026-10-09（横幅的显示层纪律）：横幅改由 `view.js` 的 `addBanner` 统一管——同文本只留一条、
 * 每条带 `×` 可手动关、到点自己收起（成功类 8 秒，降级 / 失败 20 秒；收起后条目进顶栏「通知」历史，
 * 未读计数 + 可回看 + 可重弹——见 `view.js` 的 `BANNER_TTL_MS` 与 `js/notify.js`）。
 * 这里只负责把 `kind` 映射成 `data-state`（`bannerState`）：横幅是显示层，
 * `__arDebug` 的标志与语义一个字没动。
 *
 * 2026-10-09 第二批（顶栏「状态」入口）：界面在 `js/status.js`（数据源＝同源静态文件 `/status.json`，
 * 由 `tools/gen_status.py` 生成）。2026-10-15 起这个入口**搬到了启动页**（`web/index.html` +
 * `web/js/launcher.js`）：阅读器页（`web/reader.html`）里既没有那个按钮也没有 `#status-host`，
 * 这一页也不再 import `status.js`——原先 `Esc` 里那句 `isStatusOpen()` 让路判定随之删掉
 * （恒为 false 的死判定），阅读器页的 `Esc` 现在只让路给问卷（`isSurveyOpen()`）。
 *
 * 2026-10-24（横幅的失效时间 + 顶栏通知中心）：横幅上的失效文字与倒计时在 `view.js`
 * （`BANNER_TTL_MS` / `armBannerClock`），收起后的历史在 `js/notify.js`（纯内存）。
 * 这里只做三件事：把入口与宿主交给 `mountNotify`、`Esc` 先关通知面板（在问卷之前判断，
 * 一次 `Esc` 只关一层）、「点外部」与画像 / 显示面板并列处理。`__arDebug.notifications`
 * （`{open, unread, items}`）由 `notify.js` 自己写，供无头验证断言。
 *
 * 2026-10-13（PLAN-actions-feedback.md 的 W 路，三件事都在这里接线）：
 *   ① **就地配图**：`changes[i].figure` / `figure_error` 交给 `js/figures.js` 在段落容器内画
 *      （mermaid 懒加载、SVG 走 DOMParser，都绕开 `md.js` 的转义；失败退回文字并降级可见）；
 *   ② **「太啰嗦」当场压缩**：段落标记区的那个标记 → `POST /api/compress` → 成功刷新该段正文与
 *      动作标记并提示「已压缩，请重跑 ar check」；降级（`ok:false`）正文一个字符都不动；
 *   ③ **行为埋点**：`js/events.js`（缓冲 + 阈值/定时 flush + `visibilitychange`/`pagehide` 强刷）。
 *      埋点只上报 `POST /api/events`，**不触发任何讲解 / 压缩请求**；夹具模式不发请求但留可见说明。
 */

import {
  DataError, fetchProfile, loadDoc, loadProfileSignal, postEvents, postPreference, postRating,
  profileSignalOf, readContext, requestCompress, requestExplain,
} from './api.js';
import { createExplainCache } from './cache.js';
import { armFigures, buildFigure } from './figures.js';
import {
  FLUSH_INTERVAL_MS, initEvents, emitEvent, eventsSnapshot, flushEvents, setReadComplete,
} from './events.js';
import {
  addBanner, applyChangeToBlock, buildCard, buildParagraphBlock, buildSelectBar,
  buildSourceParagraph, fillCard, layoutCard, layoutCardFixed, renderCardDeclaration, renderCardProfile,
  repositionCard, setBlockText, setCardAnswer, setCardMeta, setCardStatus, setExplainButtonsDisabled,
  setGutterAction, setSelectBarInfo,
} from './view.js';
import { asArray, actionLabel, splitRewritten } from './schema.js';
import {
  cacheKey, describeSelection, findSelectionScope, normalizeSelection, selectionRect, toolbarPosition,
} from './selection.js';
import {
  MIN_CONTRAST, bootstrapSettings, buildSettingsPanel, measureInkContrast, resetSettings,
  setSettingsPanelOpen, syncSettingsPanel, updateSettings,
} from './settings.js';
import {
  FACT_ANCHOR_OPTIONS, RATING_LABELS, buildProfilePanel, conceptStateOf, factAnchorOf,
  renderProfile, setProfilePanelOpen,
} from './profile.js';
import { bootSurvey, isSurveyOpen } from './survey.js';
import {
  closeNotifyPanel, isNotifyOpen, mountNotify, panelContains as notifyPanelContains,
} from './notify.js';

const D = globalThis.__arDebug;
D.jsLoaded = true;

const dom = {
  body: document.body,
  banners: document.querySelector('[data-role="banners"]'),
  article: document.querySelector('[data-role="rewritten"]'),
  sentinel: document.querySelector('[data-role="end-sentinel"]'),
  main: document.querySelector('[data-role="main"]'),
  drawer: document.querySelector('[data-role="drawer"]'),
  drawerToggle: document.querySelector('[data-role="drawer-toggle"]'),
  drawerList: document.querySelector('[data-role="drawer-list"]'),
  drawerCurrent: document.querySelector('[data-role="drawer-current"]'),
  drawerEmpty: document.querySelector('[data-role="drawer-empty"]'),
  cardLayer: document.querySelector('[data-role="card-layer"]'),
  compression: document.querySelector('[data-role="compression"]'),
  selectBar: document.querySelector('[data-role="select-bar"]'),
  settingsToggle: document.querySelector('[data-role="settings-toggle"]'),
  settingsPanel: document.querySelector('[data-role="settings-panel"]'),
  profileToggle: document.querySelector('[data-role="profile-toggle"]'),
  profilePanel: document.querySelector('[data-role="profile-panel"]'),
  // 通知中心（2026-10-24）：入口在顶栏，宿主在 body 层（顶栏有 backdrop-filter，浮层放进去会被裁）
  notifyToggle: document.querySelector('[data-role="notify-toggle"]'),
  notifyHost: document.querySelector('[data-role="notify-host"]'),
  // 状态面板的入口与宿主**不在这里**：2026-10-15 起它搬到了启动页（web/index.html +
  // web/js/launcher.js），阅读器页（web/reader.html）里既没有那个按钮也没有 #status-host。
  sourceBadge: document.querySelector('[data-role="source-id"]'),
  modeBadge: document.querySelector('[data-role="mode-badge"]'),
  progress: document.querySelector('[data-role="progress"]'),
};

const state = {
  ctx: null,
  paragraphs: [],
  blocks: new Map(),        // paragraphId -> 主栏元素
  sourceNodes: new Map(),   // paragraphId -> 抽屉元素
  changes: new Map(),
  concepts: new Map(),
  // 事实锚校验块（§14.1）：整份产物只此一份，`fidelity.waived` 按段查（放行标记要显示在卡片与悬停里）
  fidelity: null,
  card: null,
  current: null,
  readComplete: false,
  settings: null,           // { bg, ink, size, inkExplicit }
  settingsCtx: null,        // updateSettings/resetSettings 要的上下文（root + storage）
  settingsPanelOpen: false,
  inkContrast: null,        // 正文栏当前的实测对比度（量不出来为 null）
  profile: null,            // 最近一次读到的读者画像（GET/POST /api/profile* 的 profile 字段）
  profilePanelOpen: false,
  profileLoadSeq: 0,        // 读画像的请求序号：迟到的响应不许盖掉新的
  profileBusy: false,       // 正在写画像（自评 / 偏好一次只跑一个）
  profileSignal: '',        // 会话缓存键的画像指纹（夹具模式 / 拿不到时为空串）
  profileSignalNoted: false,
  selection: null,          // 最近一次选区的描述（浮条展示中的那一份）
  cache: createExplainCache(),
  inflight: new Map(),      // tokenKey -> seq（同一张卡片同时在飞的请求）
  seq: 0,
  pending: null,            // 当前卡片请求的 token，用来丢弃迟到的响应
  currentSince: null,       // 进入当前段的时刻（用来结算这一段的 dwell）
  compressPending: null,    // 正在压缩的段 id（一次只跑一个）
  readElapsedFrom: null,    // 第一次进入某一段的时刻（read_abandon 的 ms 用）
};

function degrade(kind, message) {
  D.degradations.push({ kind, message });
  return addBanner(dom.banners, message, bannerState(kind));
}

/* ------------------------------------------------------------ 行为埋点（③） */

/** 结算当前段的停留：把 `ms` 记成一条 `dwell`（切换段、读完、离场时各调用一次）。 */
function settleDwell() {
  const paragraph = state.current;
  if (!paragraph || !state.currentSince) return null;
  const ms = Math.max(0, Date.now() - state.currentSince);
  state.currentSince = Date.now();
  return emitEvent('dwell', { paragraph, ms });
}

/**
 * 强制 flush（`pagehide` / `visibilitychange`）之前先把现场结算掉：
 * 最后一段的停留时间不能因为「还没切段」就丢掉；真正离开页面（`pagehide`）且没读完时，
 * 记一条 `read_abandon`。切标签页（`visibilitychange`）**不算**放弃——读者会回来。
 *
 * 只在**离场**这两种时机结算：阈值 / 定时的 flush 只是把缓冲倒出去，
 * 不该凭空把一次停留切成几段（`dwell` 的口径是「换了段或离开了才结算」）。
 */
function settleBeforeFlush(reason) {
  if (reason !== 'pagehide' && reason !== 'visibilitychange') return;
  settleDwell();
  if (reason !== 'pagehide' || state.readComplete) return;
  const index = state.current ? D.paragraphs.indexOf(state.current) + 1 : 0;
  emitEvent('read_abandon', {
    paragraph: state.current || null,
    ms: state.readElapsedFrom ? Math.max(0, Date.now() - state.readElapsedFrom) : 0,
    read_paragraphs: Math.max(0, index),
  });
}

/**
 * 横幅的 `data-state`：只有「失败」染红、「做成了」染绿，其余（降级 / 说明）一律黄。
 *
 * 2026-10-09 修「横幅不消失、还会重复堆叠」时补的：这里原先把所有非 error 的 kind 都压成
 * `warn`，于是「背景已保存」（survey.js 用 `info` 发出来）在页面上与降级混在一起，
 * 成功类也就无从区分、更谈不上自动消失。**只做状态映射，不改 `__arDebug` 的任何语义。**
 */
function bannerState(kind) {
  if (kind === 'error' || kind === 'fatal') return 'error';
  if (kind === 'info' || kind === 'ok') return 'info';
  return 'warn';
}

function fatal(err) {
  const message = `材料载入失败：${err.message}。页面没拿到改写稿，无法阅读。`;
  D.fatal = message;
  addBanner(dom.banners, message, 'error');
  dom.modeBadge.dataset.mode = 'error';
  dom.modeBadge.textContent = '载入失败';
  finishReady();
}

function finishReady() {
  D.ready = true;
  dom.body.dataset.ready = 'true';
}

/* ------------------------------------------------------------ 渲染 */

function renderDoc(doc, docUrl) {
  const meta = doc.meta || {};
  const paragraphs = Array.isArray(meta.paragraphs)
    ? meta.paragraphs.filter((p) => p && typeof p.id === 'string')
    : [];
  const blocks = splitRewritten(doc.rewritten);
  const changesArr = asArray(doc.changes, 'changes');
  const conceptsArr = asArray(doc.concepts, 'concepts');

  state.paragraphs = paragraphs;
  D.paragraphs = paragraphs.map((p) => p.id);
  D.docUrl = docUrl;
  D.sourceId = meta.source_id || state.ctx.sourceId;
  // 事实锚校验块（§14.1）：`fidelity.waived` 是「哪几段申报后放行了」的唯一依据，
  // 卡片与悬停都要显示这条标记，所以整份留一份在 state 里
  state.fidelity = (doc.changes && doc.changes.fidelity) || null;
  D.fidelity = state.fidelity;

  dom.sourceBadge.textContent = `材料：${D.sourceId || '—'}`;

  changesArr.forEach((c) => {
    if (c && typeof c.paragraph === 'string') state.changes.set(c.paragraph, c);
  });
  conceptsArr.forEach((c) => {
    if (c && typeof c.id === 'string') state.concepts.set(c.id, c);
  });

  // ---- 主栏：改写稿段 N 对应原文段 N（段数/段序与源一致，PLAN-skeleton §5 M3）
  const rewrittenIds = [];
  const unrewrittenIds = [];
  paragraphs.forEach((paragraph, index) => {
    const text = blocks[index];
    const hasRewrite = typeof text === 'string' && text.length > 0;
    const change = state.changes.get(paragraph.id) || null;
    const node = buildParagraphBlock({
      paragraphId: paragraph.id,
      text: hasRewrite ? text : paragraph.text,
      change,
      unrewritten: !hasRewrite,
      sourceText: paragraph.text,
      fidelity: state.fidelity,
    });
    dom.article.appendChild(node);
    state.blocks.set(paragraph.id, node);
    (hasRewrite ? rewrittenIds : unrewrittenIds).push(paragraph.id);
    // ---- 配图：图容器在段落内、**正文之后**（§4.3）。图不进 rewritten.md，由前端就地插。
    //      `degrade` 是 main.js 的可见降级出口：画不出来就在原位置留字 + 横幅（不留空白、不留破图）。
    const figureNode = buildFigure(change, { degrade });
    if (figureNode) node.appendChild(figureNode);
  });
  // 图容器已经全部进 DOM 了，这时候才把 mermaid 图交给观察器（懒加载，见 figures.js 的 armFigures）
  armFigures();

  D.rewrittenParagraphs = rewrittenIds;
  D.unrewritten = unrewrittenIds;
  D.changes = Object.fromEntries(state.changes);
  D.concepts = Object.fromEntries(state.concepts);

  const docDegraded = Array.isArray(doc.degraded)
    ? doc.degraded.filter((m) => typeof m === 'string' && m.trim())
    : (doc.degraded ? ['后端没有给出具体原因'] : []);
  docDegraded.forEach((message) => degrade('doc-degraded', `后端降级：${message}`));

  const extraBlocks = blocks.length - paragraphs.length;
  if (extraBlocks > 0) {
    degrade('rewritten-extra',
      `改写稿有 ${blocks.length} 段，原文只有 ${paragraphs.length} 段：多出的 ${extraBlocks} 段无法对应到原文段落，未渲染。`);
  }
  if (unrewrittenIds.length) {
    degrade('rewritten-missing',
      `有 ${unrewrittenIds.length} 段没有拿到改写稿（${unrewrittenIds.join('、')}），已按纪律显示原文并标「未改写」。`);
  }
  if (!paragraphs.length) {
    degrade('no-paragraphs', '响应里没有 paragraphs，主栏没有可渲染的段落。');
  }
  if (!changesArr.length) {
    degrade('no-changes', '没有变更记录（changes.json 为空）：段落标记无法显示动作与依据。');
  }
  if (!conceptsArr.length) {
    degrade('no-concepts', '没有取得概念清单，已降级为「无画像」模式：讲解卡片不含概念与前置关系。');
  }

  renderDrawer(paragraphs);
  computeCompression(paragraphs);

  // 画像面板若在正文载入前就被打开，概念清单此刻才齐：就地重画一次（不重新请求画像）
  if (state.profilePanelOpen && dom.profilePanel && !dom.profilePanel.hidden) {
    paintProfile({ ...D.profilePanel });
  }
}

function renderDrawer(paragraphs) {
  dom.drawerList.textContent = '';
  state.sourceNodes.clear();
  if (!paragraphs.length) {
    dom.drawerEmpty.hidden = false;
    dom.drawerEmpty.textContent = '没有原文段落可显示。';
    degrade('drawer-empty', '抽屉里没有原文段落可显示。');
    return;
  }
  dom.drawerEmpty.hidden = true;
  paragraphs.forEach((paragraph) => {
    const node = buildSourceParagraph(paragraph);
    dom.drawerList.appendChild(node);
    state.sourceNodes.set(paragraph.id, node);
  });
}

/** 段落计数口径：两侧都去掉空白，原文侧再去掉 Markdown 标记，比较「看得见的字符」。 */
function visibleCharCount(text, stripMarkdown) {
  let s = String(text || '');
  if (stripMarkdown) {
    s = s
      .replace(/^#{1,6}[ \t]*/gm, '')
      .replace(/^[ \t]*>[ \t]?/gm, '')
      .replace(/^[ \t]*(?:[-*+]|\d+\.)[ \t]+/gm, '')
      .replace(/[*`]/g, '')
      .replace(/-{3,}/g, '');
  }
  return s.replace(/\s+/g, '').length;
}

function computeCompression(paragraphs) {
  const srcText = paragraphs.map((p) => p.text || '').join('');
  // 只数正文区；「未改写」徽标不算进字符数（未改写的段按它显示的原文计）
  const rwText = Array.from(dom.article.querySelectorAll('[data-role="block-text"]'))
    .map((el) => {
      const tag = el.querySelector('[data-role="unrewritten-tag"]');
      return tag ? el.textContent.replace(tag.textContent, '') : el.textContent;
    })
    .join('');
  const srcChars = visibleCharCount(srcText, true);
  const rwChars = visibleCharCount(rwText, false);
  const delta = rwChars - srcChars;
  const pct = srcChars ? Math.round((Math.abs(delta) / srcChars) * 100) : 0;
  const direction = delta === 0 ? 'same' : (delta < 0 ? 'shorter' : 'longer');
  D.compression = {
    srcChars, rwChars, deltaChars: delta, pct, direction, shown: false,
    method: '去掉空白；原文侧另去掉 Markdown 标记',
  };
}

/**
 * 读完（滚到底）才显示正文末尾的压缩块。
 * 它是**流内块**（`[data-role="compression"]`，在 — 改写稿结束 — 之后），不是浮窗；
 * 语义与调试面沿用旧口径：初始 hidden，读完才显示，`data-state` + `__arDebug.compression.shown/text`。
 */
function markReadComplete() {
  if (state.readComplete) return;
  state.readComplete = true;
  const stats = D.compression
    || { srcChars: 0, rwChars: 0, pct: 0, direction: 'same' };
  renderReadNotice(stats);
  // 读完＝正信号（§7.3 的「读完且该概念无任何求助 +0.25」）：记一条行为事件；
  // 之后离场就不再记 read_abandon（`setReadComplete` 就是那条开关）。
  emitEvent('read_complete', {
    ms: state.readElapsedFrom ? Math.max(0, Date.now() - state.readElapsedFrom) : 0,
  });
  setReadComplete(true);
}

/**
 * 按当前统计写读完块。
 * 单独抽出来是因为**压缩之后正文变了**：统计要重算，已经显示出来的那句也要跟着刷新
 * （否则页面上的字数与正文对不上——那是静默的不一致）。
 */
function renderReadNotice(stats) {
  const notice = dom.compression;
  notice.textContent = '';
  const lead = document.createTextNode('读完：本次改写比原文');
  const strong = document.createElement('b');
  if (stats.direction === 'shorter') strong.textContent = `短 ${stats.pct}%`;
  else if (stats.direction === 'longer') strong.textContent = `长 ${stats.pct}%`;
  else strong.textContent = '长度相当';
  const tail = document.createTextNode(
    `（原文 ${stats.srcChars.toLocaleString('en-US')} 字 → 改写 ${stats.rwChars.toLocaleString('en-US')} 字）`,
  );
  notice.appendChild(lead);
  notice.appendChild(strong);
  notice.appendChild(tail);
  notice.hidden = false;
  notice.dataset.state = 'shown';
  D.compression = { ...stats, shown: true, text: notice.textContent };
}

/* ------------------------------------------------------------ 当前段 / 抽屉 */

function setCurrent(paragraphId) {
  if (!paragraphId || state.current === paragraphId) return;
  // 换段之前先把上一段的停留时间结算掉（`dwell` 的 ms 就是这么来的）
  settleDwell();
  state.current = paragraphId;
  state.currentSince = Date.now();
  if (!state.readElapsedFrom) state.readElapsedFrom = state.currentSince;
  D.currentParagraph = paragraphId;
  dom.body.dataset.currentParagraph = paragraphId;

  state.blocks.forEach((node, id) => {
    node.classList.toggle('is-current', id === paragraphId);
  });
  state.sourceNodes.forEach((node, id) => {
    node.dataset.current = String(id === paragraphId);
  });
  dom.drawerCurrent.textContent = `当前段 ${paragraphId}`;

  const index = D.paragraphs.indexOf(paragraphId);
  dom.progress.textContent = `第 ${index + 1} / ${D.paragraphs.length} 段 · ${paragraphId}`;
  if (D.drawerOpen) scrollDrawerToCurrent();
}

function scrollDrawerToCurrent() {
  const node = state.sourceNodes.get(state.current);
  if (!node) return;
  const list = dom.drawerList;
  list.scrollTop = Math.max(0, node.offsetTop - list.clientHeight / 2 + node.offsetHeight / 2);
}

function setDrawer(open) {
  dom.body.dataset.drawer = open ? 'open' : 'collapsed';
  dom.drawer.dataset.state = open ? 'open' : 'collapsed';
  dom.drawerToggle.setAttribute('aria-expanded', String(open));
  D.drawerOpen = open;
  hideSelectBar();
  if (open) {
    scrollDrawerToCurrent();
    // 打开原文抽屉＝读者要看底本对照，记一条行为事件（`drawer_open`，§7.1）
    emitEvent('drawer_open', { paragraph: state.current || null });
  }
}

/* --------------------------------------------------- 阅读显示设置（②） */

/**
 * 量一次正文栏的实测对比度，写进设置面板的告警与 `__arDebug`。
 * 背景 × 文字颜色是两个独立档位，能组合出「深色背景 + 深墨文字」这种读不清的正文——
 * 不拦用户，但必须在设置面板里说出来（`syncSettingsPanel` 负责显示）。
 */
function syncDisplaySettings() {
  const ratio = measureInkContrast();
  state.inkContrast = ratio;
  D.inkContrast = ratio;                                   // 数值，量不出来是 null
  D.inkContrastLow = typeof ratio === 'number' && ratio < MIN_CONTRAST;
  syncSettingsPanel(dom.settingsPanel, state.settings, ratio);
}

function mountSettings() {
  const panel = buildSettingsPanel();
  if (dom.settingsPanel) dom.settingsPanel.replaceWith(panel);
  dom.settingsPanel = panel;

  state.settingsCtx = { root: document.body, storage: globalThis.localStorage || null, settings: state.settings };

  dom.settingsToggle.addEventListener('click', (event) => {
    event.stopPropagation();
    toggleSettingsPanel();
  });
  panel.addEventListener('click', (event) => {
    event.stopPropagation();
    const role = event.target.dataset ? event.target.dataset.role : null;
    if (role === 'settings-close') { setSettingsPanelOpen_(false); return; }
    if (role === 'settings-reset') {
      state.settings = resetSettings(state.settingsCtx);
      state.settingsCtx.settings = state.settings;
      D.settings = { ...state.settings };
      syncDisplaySettings();
      return;
    }
    const input = event.target.closest ? event.target.closest('input[data-setting]') : null;
    if (!input) return;
    const patch = { [input.dataset.setting]: input.value };
    if (input.dataset.setting === 'ink') patch.inkExplicit = true;
    state.settings = updateSettings(state.settingsCtx, patch);
    state.settingsCtx.settings = state.settings;
    D.settings = { ...state.settings };
    syncDisplaySettings();
  });
  panel.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') setSettingsPanelOpen_(false);
  });
}

function setSettingsPanelOpen_(open) {
  if (open && state.profilePanelOpen) setProfilePanelOpen_(false);
  state.settingsPanelOpen = Boolean(open);
  D.settingsPanelOpen = state.settingsPanelOpen;
  setSettingsPanelOpen(dom.settingsPanel, dom.settingsToggle, state.settingsPanelOpen);
}

function toggleSettingsPanel() {
  setSettingsPanelOpen_(!state.settingsPanelOpen);
}

/* --------------------------------------------------- 读者画像面板（2026-10-22） */

/** 面板要列的概念：只取当前材料的概念清单（concepts.json）。 */
function profileConcepts() {
  const items = [];
  state.concepts.forEach((concept) => {
    if (!concept || typeof concept.id !== 'string' || !concept.id) return;
    const name = typeof concept.name === 'string' && concept.name.trim() ? concept.name.trim() : concept.id;
    items.push({ id: concept.id, name, state: conceptStateOf(state.profile, concept.id) });
  });
  return items;
}

/** 记一条画像面板事件（可断言的流水，不进行为事件流）。 */
function recordProfileEvent(kind, detail = null) {
  const prev = D.profilePanel || {};
  const events = Array.isArray(prev.events) ? prev.events.slice() : [];
  events.push({ kind, at: Date.now(), detail: detail || null });
  D.profilePanel = { ...prev, events };
}

/** 后端 `degraded` 的三种形状都认（数组 / 字符串 / 布尔），逐条变成可见说明。 */
function profileNotes(payload) {
  if (!payload) return [];
  const raw = payload.degraded;
  if (Array.isArray(raw)) return raw.filter((note) => typeof note === 'string' && note.trim());
  if (typeof raw === 'string' && raw.trim()) return [raw.trim()];
  if (raw === true) return ['后端没有给出具体原因（degraded: true）。'];
  return [];
}

/**
 * 把当前画像画进面板，并同步 `__arDebug.profilePanel`（唯一出口：别处不要直接改面板 DOM）。
 *
 * `locked` 的口径：加载中 / 读失败 / 写盘中 / 夹具模式 → 控件禁用；
 * **写失败（write-error）不禁用**——画像没改动，读者可以重试。
 */
function paintProfile(extra = {}) {
  const prev = D.profilePanel || {};
  const panelState = extra.state || prev.state || 'closed';
  const locked = extra.locked !== undefined
    ? extra.locked
    : ['fixture', 'loading', 'error', 'pending'].includes(panelState);
  const model = {
    open: state.profilePanelOpen,
    state: panelState,
    statusText: extra.statusText || '',
    mode: state.ctx && state.ctx.mode === 'fixture' ? 'fixture' : 'api',
    // 读失败且手上没有旧画像时**不列概念**：列出来会像「都没自评过」，而真相是没读到
    concepts: (panelState === 'error' && !state.profile) ? [] : profileConcepts(),
    preference: factAnchorOf(state.profile),
    degraded: Array.isArray(extra.degraded) ? extra.degraded : [],
    url: extra.url !== undefined ? extra.url : (prev.url || null),
    locked,
    events: prev.events || [],
    lastWrite: extra.lastWrite !== undefined ? extra.lastWrite : (prev.lastWrite || null),
    error: extra.error || null,
  };
  renderProfile(dom.profilePanel, model);
  D.profilePanel = model;
  return model;
}

/**
 * 一次画像读 / 写成功后统一收口：存画像、算指纹（会话讲解缓存的第五段）、重画面板。
 * 指纹直接由响应里的整份画像算，省一次 `GET /api/profile`。
 */
function applyProfile(payload, url, extra = {}) {
  state.profile = payload && payload.profile && typeof payload.profile === 'object' ? payload.profile : null;
  state.profileSignal = profileSignalOf(payload || {});
  D.profileSignal = state.profileSignal;
  const rated = profileConcepts().filter((concept) => concept.state !== 'unrated').length;
  return paintProfile({
    state: 'ready',
    statusText: `已同步画像（本材料已自评 ${rated} 条概念）`,
    url,
    degraded: profileNotes(payload),
    ...extra,
  });
}

async function loadProfile_() {
  if (!state.ctx) return;
  if (state.ctx.mode === 'fixture') {
    paintProfile({
      state: 'fixture',
      statusText: '夹具模式：没有画像后端（GET/POST /api/profile 只在正式模式可用），自评与偏好都不能保存。',
      degraded: [],
      url: null,
      locked: true,
    });
    return;
  }
  const seq = state.profileLoadSeq + 1;
  state.profileLoadSeq = seq;
  recordProfileEvent('load');
  paintProfile({ state: 'loading', statusText: '正在读读者画像…', degraded: [], locked: true });
  try {
    const { payload, url } = await fetchProfile(state.ctx);
    if (seq !== state.profileLoadSeq) return;      // 迟到响应：只丢掉，不盖新状态
    applyProfile(payload, url, {});
  } catch (err) {
    if (seq !== state.profileLoadSeq) return;
    paintProfile({
      state: 'error',
      statusText: `画像读不出来：${err.message}`,
      degraded: [],
      locked: true,
      error: err.message,
    });
    degrade('profile-load', `读不到读者画像（${err.message}）：这次不能改自评或偏好，阅读照常。`);
  }
}

function setProfilePanelOpen_(open) {
  const next = Boolean(open);
  if (!next && !state.profilePanelOpen) return;   // 已经关着：不要每次 Esc 都记一条 close
  if (next && state.settingsPanelOpen) setSettingsPanelOpen_(false);
  state.profilePanelOpen = next;
  D.profilePanel = { ...(D.profilePanel || {}), open: next };
  setProfilePanelOpen(dom.profilePanel, dom.profileToggle, next);
  recordProfileEvent(next ? 'open' : 'close');
  if (next) loadProfile_();
}

function toggleProfilePanel() {
  setProfilePanelOpen_(!state.profilePanelOpen);
}

/** 写一条自评：成功才改界面（响应里的 profile 是写回后的整份画像）。 */
async function submitRating(conceptId, ratingState) {
  if (!state.ctx || state.ctx.mode !== 'api' || state.profileBusy) return;
  const concept = state.concepts.get(conceptId);
  const name = concept && concept.name ? concept.name : conceptId;
  const label = RATING_LABELS[ratingState] || ratingState;
  // 同档重复点不写盘：自评端点不是幂等的（后端会再追一条 evidence / override），
  // 白写一次还会换画像指纹 ⇒ 讲解缓存全失效。
  if (conceptStateOf(state.profile, conceptId) === ratingState) {
    recordProfileEvent('rating-noop', { concept: conceptId, state: ratingState });
    paintProfile({ state: 'ready', statusText: `${name} 已经是「${label}」，没有重复写盘。`, locked: false });
    return;
  }
  state.profileBusy = true;
  paintProfile({ state: 'pending', statusText: `正在写回自评：${name} = ${label}…`, locked: true });
  try {
    const { payload, url } = await postRating(state.ctx, { conceptId, state: ratingState });
    state.profileBusy = false;
    recordProfileEvent('rating-ok', { concept: conceptId, state: ratingState });
    applyProfile(payload, url, {
      statusText: `已记下自评：${name} = ${label}`,
      lastWrite: { kind: 'rating', concept: conceptId, state: ratingState, at: Date.now(), ok: true },
    });
    addBanner(dom.banners,
      `已记下自评：${name} = ${label}（写回画像；下一次 ar rewrite 才会用上）。`, 'info');
  } catch (err) {
    state.profileBusy = false;
    recordProfileEvent('rating-error', { concept: conceptId, state: ratingState, message: err.message });
    paintProfile({
      state: 'write-error',
      statusText: `自评没写上：${err.message}`,
      locked: false,
      error: err.message,
    });
    degrade('profile-rating',
      `自评没写上（${name}）：${err.message}。画像没有改动，页面上显示的还是上一次读到的那份。`);
  }
}

/** 写「事实锚口径」偏好；同 id 同取值时后端幂等（不写盘、指纹不变）。 */
async function submitPreference(value) {
  if (!state.ctx || state.ctx.mode !== 'api' || state.profileBusy) return;
  const option = FACT_ANCHOR_OPTIONS.find((item) => item.value === value);
  if (!option) return;
  const preferenceId = factAnchorOf(state.profile).id;
  state.profileBusy = true;
  paintProfile({ state: 'pending', statusText: `正在写回事实锚口径：${option.label}…`, locked: true });
  try {
    const { payload, url } = await postPreference(state.ctx, { id: preferenceId, value });
    state.profileBusy = false;
    recordProfileEvent('preference-ok', { id: preferenceId, value });
    applyProfile(payload, url, {
      statusText: `事实锚口径已改为：${option.label}`,
      lastWrite: { kind: 'preference', id: preferenceId, value, at: Date.now(), ok: true },
    });
    addBanner(dom.banners,
      `事实锚口径已改为「${option.label}」：下一次 ar check / ar rewrite 起按它判（已有产物不变，要一致就重跑）。`,
      'info');
  } catch (err) {
    state.profileBusy = false;
    recordProfileEvent('preference-error', { id: preferenceId, value, message: err.message });
    paintProfile({
      state: 'write-error',
      statusText: `事实锚口径没写上：${err.message}`,
      locked: false,
      error: err.message,
    });
    degrade('profile-preference',
      `事实锚口径没写上（${option.label}）：${err.message}。画像没有改动，当前口径照旧。`);
  }
}

function mountProfile() {
  const panel = buildProfilePanel();
  if (dom.profilePanel) dom.profilePanel.replaceWith(panel);
  dom.profilePanel = panel;
  paintProfile({ state: 'closed', statusText: '', locked: true });

  dom.profileToggle.addEventListener('click', (event) => {
    event.stopPropagation();
    toggleProfilePanel();
  });
  panel.addEventListener('click', (event) => {
    event.stopPropagation();
    const role = event.target.dataset ? event.target.dataset.role : null;
    if (role === 'profile-close') { setProfilePanelOpen_(false); return; }
    if (role === 'profile-rate') {
      submitRating(event.target.dataset.concept, event.target.dataset.state);
    }
  });
  panel.addEventListener('change', (event) => {
    const input = event.target.closest ? event.target.closest('input[data-role="profile-preference"]') : null;
    if (!input) return;
    submitPreference(input.value);
  });
  panel.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') setProfilePanelOpen_(false);
  });
}

/* ------------------------------------------------------------ 讲解卡片 */

function ensureCard() {
  if (!state.card) {
    state.card = buildCard();
    dom.cardLayer.appendChild(state.card);
    state.card.querySelector('[data-role="card-close"]')
      .addEventListener('click', () => closeCard('close-button'));
    state.card.querySelectorAll('[data-role="explain"]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const pid = btn.dataset.paragraph || state.card.dataset.paragraph;
        requestCardExplain(pid, btn.dataset.kind, cardSelectionContext(state.card));
      });
    });
    // 答案把卡片撑高后要重新夹回视口（不然底部按钮会被顶到视口外）
    if (typeof ResizeObserver === 'function') {
      const observer = new ResizeObserver(() => {
        if (!state.card || !D.cardOpen) return;
        const anchor = state.card.dataset.cardScope === 'selection'
          ? null
          : dom.article.querySelector(`#blk-${state.card.dataset.paragraph} [data-role="marker"]`);
        repositionCard(state.card, anchor);
      });
      observer.observe(state.card);
      state.cardObserver = observer;
    }
  }
  return state.card;
}

/** 卡片当前的片段上下文（整段卡片返回 null）。 */
function cardSelectionContext(card) {
  const text = card.dataset.selection;
  if (!text) return null;
  const preview = card.querySelector('[data-role="card-selection-preview"]');
  return {
    text,
    from: card.dataset.selectionFrom === 'source' ? 'source' : 'rewritten',
    preview: preview ? preview.textContent : text,
    previewTruncated: Boolean(preview && preview.dataset.truncated === 'true'),
  };
}

/**
 * 打开卡片。`selection`（可选）来自选片浮条：
 * { text, preview, previewTruncated, paragraphs, chars, from }
 */
function openCard(paragraphId, anchor, selection = null) {
  const card = ensureCard();
  fillCard(card, {
    paragraphId,
    change: state.changes.get(paragraphId) || null,
    conceptsById: D.concepts,
    selection,
    // 事实锚申报的「已放行」标记要看 fidelity.waived（§14.1），所以整份校验块一起交进去
    fidelity: state.fidelity,
  });
  card.dataset.cardState = 'open';
  if (selection) layoutCardFixed(card);
  else layoutCard(card, anchor);
  D.cardOpen = true;
  // 卡片开关**不进行为流**（§7.1 的 kind 白名单里没有这一类，见 web/README 的「行为埋点」段）：
  // 它只落在这个可断言的调试面上，免得臆造一个服务端会丢掉的新 kind。
  D.cardOpens = (D.cardOpens || []).concat([{
    paragraph: paragraphId, scope: selection ? 'selection' : 'paragraph', at: Date.now(),
  }]);
  D.lastCard = {
    paragraph: paragraphId,
    action: (state.changes.get(paragraphId) || {}).action || null,
    scope: selection ? 'selection' : 'paragraph',
    selection: selection ? selection.text : null,
    state: 'idle',
    text: '',
    at: Date.now(),
  };
}

function closeCard(reason) {
  if (!state.card || !D.cardOpen) return;
  state.card.dataset.cardState = 'closed';
  state.card.hidden = true;
  D.cardOpen = false;
  state.pending = null;
  D.cardCloses = (D.cardCloses || []).concat([{ reason, at: Date.now() }]);
  if (D.lastCard) D.lastCard.closed = true;
}

/** 讲解缓存的可断言快照（无头验证与排查都用它）。 */
function cacheSnapshot() {
  return { size: state.cache.size(), inflight: state.cache.inflightSize(), keys: state.cache.keys() };
}

/**
 * 刷一次画像指纹（会话缓存键的第五段）。
 *
 * 为什么要有：后端缓存键含 `profile.yaml` 的 `(字节数, mtime_ns)`，画像一变旧条目就失效；
 * 前端会话缓存若不带等价信号，「会话内改了画像（自评写回 / 手改 overrides）」后仍会命中旧答案。
 * 取数只用现有能力（`GET /api/profile` 的内容摘要），不请新接口。
 *
 * 失败处理（不静默）：**保留上一次的信号**（清空会让键漂移、白白多花一次模型调用），
 * 并留一条降级横幅说明「本次会话的缓存不会随画像变化失效」。
 */
async function refreshProfileSignal() {
  if (!state.ctx || state.ctx.mode !== 'api') {
    D.profileSignal = state.profileSignal;
    return state.profileSignal;
  }
  try {
    state.profileSignal = await loadProfileSignal(state.ctx);
    D.profileSignal = state.profileSignal;
  } catch (err) {
    if (!state.profileSignalNoted) {
      state.profileSignalNoted = true;
      degrade('profile-signal',
        `拿不到读者画像指纹（/api/profile 失败：${err.message}）：`
        + '本次会话的讲解缓存不随画像变化失效（画像改过之后可能命中旧答案）。');
    }
    D.profileSignal = state.profileSignal;
  }
  return state.profileSignal;
}

/**
 * 现场生成讲解（慢入口）。
 *
 * 会话缓存 + 在飞去重 + 请求序号三件事都在这里：
 *   - 命中缓存：直接渲染，标「本次会话已生成」，不发请求；
 *   - 同一 key 已在飞：复用同一个 Promise（`D.explainDeduped` 计数，测试据此断言没重复发请求）；
 *   - 迟到的响应：token 与当前卡片不符就只写进缓存，不动卡片（`D.lastCard.stale = true`）。
 *
 * 缓存键在**取过画像指纹之后**才定：画像在会话内变了，键就变，旧答案不会被命中。
 */
async function requestCardExplain(paragraphId, kind, selection = null) {
  const card = state.card;
  if (!card) return;
  const selectionText = selection ? normalizeSelection(selection.text) : '';
  const selectionFrom = selection && selection.from === 'source' ? 'source' : 'rewritten';

  setExplainButtonsDisabled(card, true);
  setCardAnswer(card, '');
  setCardMeta(card, '');
  setCardStatus(card, 'loading', selectionText
    ? `正在按选中片段生成讲解（${kind}）…`
    : `正在生成讲解（${kind}）…`);

  const profileSignal = await refreshProfileSignal();
  const key = cacheKey(D.sourceId, paragraphId, kind, selectionText, profileSignal);

  const baseRecord = {
    paragraph: paragraphId,
    kind,
    scope: selectionText ? 'selection' : 'paragraph',
    selection: selectionText || null,
    selectionFrom: selectionText ? selectionFrom : null,
    state: 'loading',
    text: '',
    at: Date.now(),
  };
  D.lastCard = { ...baseRecord };
  state.seq += 1;
  const token = { key, seq: state.seq };
  state.pending = token;
  D.pendingExplain = { key, seq: token.seq };

  const outcome = await state.cache.request(
    { sourceId: D.sourceId, paragraph: paragraphId, kind, selection: selectionText },
    {
      cacheKey,
      run: () => requestExplain(state.ctx, {
        paragraph: paragraphId, kind, selection: selectionText, selectionFrom,
      }),
    },
  );
  D.explainCache = cacheSnapshot();

  const commit = (result, fromCache) => {
    const done = renderExplainResult(card, {
      paragraphId, kind, selectionText, selectionFrom, token, result, fromCache,
    });
    const summary = { ...baseRecord, state: done.state, text: done.text, url: done.url };
    if (!done.live) summary.stale = true;
    D.lastCard = summary;
    D.cards.push({ ...summary });
    const payload = (result && result.entry && result.entry.payload) || {};
    noteExplainEvent(paragraphId, kind, selectionText, selectionFrom, {
      cached: Boolean(fromCache || payload.cached === true),
      degraded: done.state !== 'ok',
    });
  };

  if (outcome.status === 'cached') {
    // 命中会话缓存：直接渲染，标「本次会话已生成」，不再发请求
    commit({ entry: outcome.entry, ok: true }, true);
  } else {
    if (outcome.status === 'inflight') D.explainDeduped = (D.explainDeduped || 0) + 1;
    const settled = await outcome.promise;
    if (settled.ok) {
      commit(settled, false);
    } else {
      const err = settled.error;
      const copy = `讲解生成失败：${err.message}。这一段仍可在右侧「原文」抽屉里对照原文。`;
      const summary = {
        ...baseRecord,
        state: 'error',
        text: copy,
        url: err.url || null,
        errorKind: (err.detail && err.detail.kind) || null,
      };
      if (isLive(token)) {
        setCardStatus(card, 'error', copy);
        D.lastCard = summary;
      } else {
        // 迟到且失败的请求：不覆盖当前卡片
        D.lastCard = { ...summary, stale: true };
      }
      D.cards.push({ ...D.lastCard });
      degrade('explain-error', `讲解请求失败（${paragraphId}）：${err.message}`);
      noteExplainEvent(paragraphId, kind, selectionText, selectionFrom, { cached: false, degraded: true });
    }
  }

  // 收尾再刷新一次缓存快照：这里才是「本次请求彻底结束」的时点
  D.explainCache = cacheSnapshot();
  setExplainButtonsDisabled(card, false);
}

/** token 还是当前卡片的那一个吗？不是就说明响应迟到了。 */
function isLive(token) {
  return Boolean(state.pending && state.pending.seq === token.seq && state.pending.key === token.key);
}

/**
 * 把一次请求结果写进卡片（只在 token 仍然有效时动 DOM）。
 * @returns {{state:string, text:string, url:string|null, live:boolean}}
 */
function renderExplainResult(card, { paragraphId, kind, selectionText, selectionFrom, token, result, fromCache }) {
  const entry = result.entry || {};
  const payload = entry.payload || {};
  const url = entry.url || null;
  const live = isLive(token);
  const text = typeof payload.text === 'string' ? payload.text.trim() : '';
  const notes = [];
  if (fromCache) notes.push('本次会话已生成（直接复用，没有再次请求模型）');
  if (payload.cached === true) notes.push('后端缓存命中');
  if (payload.selection_truncated === true) notes.push('片段超过 600 字，本次只按前 600 字讲解');
  const fixtureMismatch = entry.mode === 'fixture'
    && typeof payload.paragraph === 'string'
    && payload.paragraph !== paragraphId;
  if (fixtureMismatch) {
    notes.push(`夹具模式：这份 explain.json 是 ${payload.paragraph} 的样例讲解，不是本段自己的`);
  }
  if (selectionText && payload.scope !== 'selection') {
    notes.push(payload.scope === 'paragraph'
      ? '后端按整段讲的（scope: paragraph）：段号不存在之类的请求在片段校验之前就返回了'
      : '响应没有 scope 字段：无法确认后端是按片段讲的（旧响应或夹具）');
  }
  const scopeNote = selectionText
    ? `讲解按选中片段生成（第 ${paragraphId} 段，来自${selectionFrom === 'source' ? '原文' : '改写稿'}）`
    : `讲解已生成（${kind}）`;

  let nextState = 'ok';
  let copy = text;
  let statusText = '';
  if (payload.degraded === true) {
    copy = text || (selectionText
      ? '模型不可用：本次片段讲解没有生成。该段原文可在右侧「原文」抽屉里对照。'
      : '模型不可用：本次讲解没有生成。该段原文可在右侧「原文」抽屉里对照。');
    nextState = 'degraded';
    statusText = `降级显示：${copy}`;
  } else if (!text) {
    copy = selectionText
      ? '模型返回了空内容：本次片段讲解没有生成。该段原文可在右侧「原文」抽屉里对照。'
      : '模型返回了空内容：本次讲解没有生成。该段原文可在右侧「原文」抽屉里对照。';
    nextState = 'degraded';
    statusText = `降级显示：${copy}`;
  } else {
    statusText = notes.length ? `${scopeNote}。${notes.join('；')}` : `${scopeNote}。`;
  }

  if (live) {
    if (nextState === 'ok') setCardAnswer(card, text);
    setCardStatus(card, nextState, statusText);
    if (fromCache) setCardMeta(card, '本次会话已生成');
  }
  if (nextState === 'degraded') {
    degrade(`explain-${fromCache ? 'cached-degraded' : 'degraded'}`,
      `讲解降级（${paragraphId}${selectionText ? ' 片段' : ''}）：${copy}`);
  }
  return { state: nextState, text: copy, url, live };
}

/**
 * 讲解结果的行为事件（§7.1 的 `explain`）：只记「这次讲解是怎么来的」，**不触发任何请求**。
 *
 * `degraded` 的口径：只要这次不是「正常拿到了一段讲解」（后端降级 / 空回复 / 请求失败）就是 true
 * ——行为流要能分辨「读者求助且得到了答案」与「求助没得到答案」。
 */
function noteExplainEvent(paragraphId, kind, selectionText, selectionFrom, { cached, degraded: isDegraded }) {
  const payload = {
    paragraph: paragraphId,
    explain_kind: kind,
    scope: selectionText ? 'selection' : 'paragraph',
    selection_chars: selectionText ? selectionText.length : 0,
    cached: Boolean(cached),
    degraded: Boolean(isDegraded),
  };
  if (selectionText) payload.selection_from = selectionFrom;
  return emitEvent('explain', payload);
}

/* ------------------------------------- 读中「太啰嗦」当场压缩（②，§5 / §12 第 12 条） */

/** 请求里的 `reason`（契约 §5.2 的默认值）。 */
const COMPRESS_REASON = 'too_verbose';

/** 压缩成功之后这一段的新依据文案（悬停与卡片都用它，说明「为什么正文和刚才不一样了」）。 */
const COMPRESS_NOTE = '读者点了「太啰嗦」，这一段已当场压缩：只删冗余，事实锚（数字 / 引文 / 书名号）'
  + '一个不少。产物已写回、fidelity 已重置，请重跑 `ar check`。';

/**
 * 点「太啰嗦」→ 当场压缩这一段（契约 §5.2）。
 *
 * - 成功：用返回的 `text` 刷新该段正文 + 动作标记 + 悬停依据，横幅写「已压缩，请重跑 ar check」；
 *   成功响应里 `degraded` 若非空**照样打一条 warn 横幅**（两种来源，都不影响 `ok` 与写盘：
 *   ① 这一段没有事实锚 ⇒ 本次压缩**未经事实锚校验**；② 那条行为事件没记上）——降级必须看得见
 *   （`degrade()` 的既有用法，正文与产物不受影响）；
 * - 降级（`ok:false`）与请求失败：**正文一个字符都不动**，只留可见横幅与 `__arDebug.compress`；
 * - 夹具模式：没有这个后端，说清楚而不是假装压了；
 * - 无论哪条路都写一条行为事件（`kind: compress`，带 `ok` / `reason`），而**埋点本身不发模型请求**。
 * - 一次只压一段（连点不会变成两次计费）；压成功那一段的标记转 `done` 且不可再点。
 */
async function requestParagraphCompress(paragraphId) {
  if (state.compressPending) return null;
  const block = state.blocks.get(paragraphId);
  if (!block) return null;
  // 已经压过的段不再压（标记是 disabled 的，但脚本仍可能触发 click）——同一段不重复付费
  const button = block.querySelector('[data-role="too-verbose"]');
  if (button && button.dataset.state === 'done') return null;

  const started = Date.now();
  D.compressEvents = (D.compressEvents || []).concat([{ type: 'compress-request', paragraph: paragraphId, at: started }]);
  const record = {
    paragraph: paragraphId, at: started, state: 'request', ok: null, reason: null, text: '', url: null,
  };
  D.compress = record;

  if (!state.ctx || state.ctx.mode !== 'api') {
    const copy = '这一段没能压缩：夹具模式没有压缩后端（POST /api/compress 只在正式模式可用），正文保持原样。';
    degrade('compress-fixture', copy);
    emitEvent('compress', { paragraph: paragraphId, ok: false, reason: 'fixture' });
    D.compress = { ...record, state: 'fixture', ok: false, reason: 'fixture' };
    D.compressEvents = D.compressEvents.concat([{ type: 'compress-done', paragraph: paragraphId, state: 'fixture' }]);
    return D.compress;
  }

  state.compressPending = paragraphId;
  setGutterAction(block, 'pending', '压缩中…');
  try {
    const { payload, url } = await requestCompress(state.ctx, { paragraph: paragraphId, reason: COMPRESS_REASON });
    const text = payload && typeof payload.text === 'string' ? payload.text.trim() : '';
    if (payload && payload.ok === true && text) {
      applyCompressedParagraph(block, paragraphId, text, payload);
      const hint = typeof payload.hint === 'string' && payload.hint.trim()
        ? payload.hint.trim()
        : '已压缩，请重跑 ar check。';
      addBanner(dom.banners, hint, 'info');
      // 成功路径也可能带降级（这一段没有事实锚 ⇒ 未经校验；或行为事件没记上）：都不影响压缩与正文，
      // 但必须可见 —— 走 degrade() 的 warn 横幅，不藏在调试面里。
      const notes = Array.isArray(payload.degraded) ? payload.degraded.filter(Boolean) : [];
      if (notes.length) {
        degrade('compress-degraded-notes', `这一段已压缩，但后端有降级说明（${paragraphId}）：${notes.join('；')}`);
      }
      emitEvent('compress', { paragraph: paragraphId, ok: true });
      setGutterAction(block, 'done', '已压缩');
      D.compress = {
        ...record, state: 'ok', ok: true, text, url, hint,
        cached: payload.cached === true,
        model: payload.model || null,
        artifactsUpdated: payload.artifacts_updated === true,
        degraded: notes,
        // 事实锚申报的可见面（§14.1）：这次压缩申报了什么、按哪一档口径放行了哪几项（没放行就是 []）
        factDeclarations: (payload.fact_declarations && typeof payload.fact_declarations === 'object')
          ? payload.fact_declarations : {},
        waived: Array.isArray(payload.waived) ? payload.waived : [],
        overDeclared: Array.isArray(payload.over_declared) ? payload.over_declared : [],
        factAnchorPolicy: typeof payload.fact_anchor_policy === 'string' ? payload.fact_anchor_policy : '',
      };
    } else {
      const reason = (payload && payload.reason) || 'unknown';
      const seen = Array.isArray(payload && payload.degraded) ? payload.degraded.filter(Boolean) : [];
      const copy = `这一段没能压缩（${reason}）：正文保持原样${seen.length ? `；后端说明：${seen.join('；')}` : ''}。`;
      degrade('compress-degraded', `压缩降级（${paragraphId}）：${copy}`);
      emitEvent('compress', { paragraph: paragraphId, ok: false, reason });
      setGutterAction(block, 'idle');
      D.compress = { ...record, state: 'degraded', ok: false, reason, url, text, degraded: seen };
    }
  } catch (err) {
    const message = (err && err.message) || String(err);
    degrade('compress-error', `压缩请求失败（${paragraphId}）：${message}。正文保持原样。`);
    emitEvent('compress', { paragraph: paragraphId, ok: false, reason: 'request_failed' });
    setGutterAction(block, 'idle');
    D.compress = { ...record, state: 'error', ok: false, reason: 'request_failed', message };
  } finally {
    state.compressPending = null;
    D.compressEvents = D.compressEvents.concat([{
      type: 'compress-done', paragraph: paragraphId, state: D.compress ? D.compress.state : null, at: Date.now(),
    }]);
  }
  return D.compress;
}

/**
 * 把压缩稿落进页面：换正文、换动作标记与依据、重算压缩统计、必要时刷新读完块。
 *
 * 只碰**页面状态**：产物落盘是后端的事（`artifacts_updated`），前端不写文件。
 * 图那一块特殊：压缩之后正文与原来的配图不再配套，就地把它摘掉并在 `__arDebug.figures` 里留痕。
 *
 * 事实锚申报（§14.1）：`compress-v2` 起在线压缩也有申报通道，所以这一段要按**本次压缩**的结果重画——
 * 后端响应带回 `fact_declarations`（这次申报了什么）/ `waived`（按读者口径放行了哪几项）/
 * `over_declared`（多申报了几项）/ `fact_anchor_policy`（哪一档口径），
 * 同一份数据也已经写进 `changes[i].fact_declarations`（重跑 `ar check` 后进 `fidelity.waived`）。
 * 页面上就地补一层 `fidelity` 覆盖（只影响**这一段**的显示，不改产物），
 * 免得到重跑 `ar check` 之前一直显示「未放行」——那与后端刚做的判定自相矛盾。
 *
 * 依据画像（2026-10-14）：后端压缩那一段走的是**全新**的变更记录（`why.profile` 清空、
 * `why.note` 换成压缩说明），所以产物里已经没有 `profile_refs_filled` 了。本函数于是把
 * 那个键一并摘掉（与 `deferred` / `figure` / `fact_declarations` 同一条口径），
 * 卡片开着时也把「依据画像」那一块就地重画——**不许留一枚产物里已经不在的徽标**。
 */
function applyCompressedParagraph(block, paragraphId, text, payload) {
  setBlockText(block, text);

  const previous = state.changes.get(paragraphId) || null;
  const previousWhy = (previous && previous.why) || {};
  const next = {
    ...(previous || {}),
    paragraph: paragraphId,
    action: 'compress',
    why: {
      profile: Array.isArray(previousWhy.profile) ? previousWhy.profile.slice() : [],
      note: COMPRESS_NOTE,
    },
    confidence: previous && typeof previous.confidence === 'number' ? previous.confidence : null,
    facts: (previous && previous.facts) || { numbers: [], quotes: [] },
  };
  // 这一段现在是「真压过了」：deferred（枚举内未实现）与配图标记都不再成立；
  // 申报换成**本次压缩**的那一份（后端在响应里带回，与写进产物的是同一份数据）：
  // 本次没有申报 ⇒ 整键不出现（照「为真才出现」的先例），旧的那份不能留（它说的是上一次的账）。
  delete next.deferred;
  delete next.figure;
  delete next.figure_error;
  delete next.fact_declarations;
  // 兜底补齐标记同理要摘掉（2026-10-14）：后端压缩那一段走的是**全新**的变更记录
  // （`why.profile` 清空、`why.note` 换成压缩说明），产物里已经**没有** `profile_refs_filled`
  // 这个键了——本地重画若沿用压前那一份，页面就会挂着「产物已无标记」的过期徽标。
  delete next.profile_refs_filled;
  const declared = payload && payload.fact_declarations;
  if (declared && typeof declared === 'object' && !Array.isArray(declared) && Object.keys(declared).length) {
    next.fact_declarations = declared;
  }
  applyCompressFidelity(paragraphId, payload);
  state.changes.set(paragraphId, next);
  D.changes = Object.fromEntries(state.changes);
  applyChangeToBlock(block, next, { fidelity: state.fidelity });

  const figureNode = block.querySelector('[data-role="figure"]');
  if (figureNode) {
    if (Array.isArray(D.figures)) {
      D.figures.forEach((item) => {
        if (item.paragraph === paragraphId) item.state = 'removed';
      });
    }
    figureNode.remove();
  }

  // 卡片正开着这一段：把「动作 + 依据」就地换成新的（答案与缓存不动，别把读者正在看的东西抹掉），
  // 申报那一块按本次压缩的结果重画（原来是整块摘掉——那时压缩路径还没有申报通道）
  if (D.cardOpen && state.card && state.card.dataset.paragraph === paragraphId) {
    const actionEl = state.card.querySelector('[data-role="card-action"]');
    if (actionEl) {
      actionEl.textContent = actionLabel('compress');
      actionEl.dataset.action = 'compress';
    }
    const whyEl = state.card.querySelector('[data-role="card-why"]');
    if (whyEl) whyEl.textContent = next.why.note;
    renderCardDeclaration(state.card, next, state.fidelity);
    // 「依据画像」那一块也要重画：这一段原来的 `profile_refs_filled` 标记已经作废
    // （产物里压缩写的是全新一条记录，没有这个键），不重画就会留着过期的「兜底补齐」徽标。
    renderCardProfile(state.card, next);
  }

  const noticeShown = Boolean(D.compression && D.compression.shown);
  computeCompression(state.paragraphs);
  if (noticeShown) renderReadNotice(D.compression);
}

/**
 * 把一次在线压缩的申报结果并进页面上的 `fidelity`（§14.1，**只影响显示**）。
 *
 * 产物的 `fidelity` 在每次合并之后都被**重置为未校验**（`waived: []`，这是既有契约：
 * 校验结论不能跨重跑复用），所以刚压完这段时间里页面若只看产物，会把「已按口径放行」显示成
 * 「未放行」。这里按本次响应的 `waived` / `over_declared` / `fact_anchor_policy` 给这一段落补一层
 * 覆盖：先把这一段在旧列表里的行摘掉（重压过一次，旧账作废），再并进本次的条目。
 * 重跑 `ar check` 后产物里的 `fidelity` 才是权威（那时刷新页面即可）。
 */
function applyCompressFidelity(paragraphId, payload) {
  const rowsOf = (value) => (Array.isArray(value) ? value.filter((row) => row && typeof row === 'object') : []);
  const waived = rowsOf(payload && payload.waived);
  const overDeclared = rowsOf(payload && payload.over_declared);
  const policy = payload && typeof payload.fact_anchor_policy === 'string' ? payload.fact_anchor_policy : '';
  const current = (state.fidelity && typeof state.fidelity === 'object') ? state.fidelity : null;
  if (!current && !waived.length && !overDeclared.length && !policy) return;
  const base = current || {};
  const keepOthers = (key) => rowsOf(base[key]).filter((row) => row.paragraph !== paragraphId);
  state.fidelity = {
    ...base,
    fact_anchor_policy: policy || base.fact_anchor_policy || '',
    waived: [...keepOthers('waived'), ...waived],
    over_declared: [...keepOthers('over_declared'), ...overDeclared],
  };
  D.fidelity = state.fidelity;
}

/* --------------------------------------------------- 选片讲解浮条（①） */

function currentBrowserSelection() {
  try {
    return window.getSelection ? window.getSelection() : null;
  } catch (err) {
    return null;
  }
}

function hideSelectBar() {
  if (!dom.selectBar) return;
  dom.selectBar.hidden = true;
  dom.selectBar.dataset.state = 'hidden';
  D.toolbarOpen = false;
  state.selection = null;
  D.selection = null;
}

function showSelectBar(desc) {
  const bar = dom.selectBar;
  if (!bar) return;
  window.clearTimeout(state.barTimer);
  setSelectBarInfo(bar, desc);
  bar.hidden = false;
  bar.dataset.state = 'shown';
  bar.dataset.paragraph = desc.paragraphId;
  bar.dataset.source = desc.from;
  bar.dataset.selection = desc.normalized;

  const bounds = selectionRect(currentBrowserSelection());
  if (bounds) {
    const pos = toolbarPosition(bounds, { width: bar.offsetWidth, height: bar.offsetHeight }, {
      width: window.innerWidth,
      height: window.innerHeight,
    });
    bar.style.left = `${pos.left}px`;
    bar.style.top = `${pos.top}px`;
    const arrow = bar.querySelector('[data-role="select-bar-arrow"]');
    if (arrow) arrow.hidden = pos.arrow === null;
    if (arrow && pos.arrow !== null) arrow.style.left = `${pos.arrow}px`;
  }
  D.toolbarOpen = true;
  const snapshot = {
    paragraph: desc.paragraphId,
    from: desc.from,
    text: desc.normalized,
    chars: desc.chars,
    spans: desc.spans,
    preview: desc.preview,
    previewTruncated: desc.previewTruncated,
  };
  // 页面内部状态与调试面同源：点浮条按钮时读的就是这一份
  state.selection = snapshot;
  D.selection = { ...snapshot };
  // 选了一段文字＝「选片求助」的入口（§7.1 的 `selection`：chars / spans / from）
  emitEvent('selection', {
    paragraph: desc.paragraphId,
    chars: desc.chars,
    spans: desc.spans,
    from: desc.from,
  });
}

/**
 * 选完就更新浮条：选区不可用（空 / 纯空白 / 1 个字符）或不在可讲区里 → 收起。
 * 用 setTimeout(0) 而不是动画帧：某些无头环境里动画帧不一定按预期推。
 */
function refreshSelectionBar() {
  window.clearTimeout(state.barTimer);
  state.barTimer = window.setTimeout(() => {
    state.barTimer = null;
    // 这一次是「按浮条按钮」触发的 mouseup：按钮那边已经接手，别再弹回来
    if (state.suppressBarRefresh) {
      state.suppressBarRefresh = false;
      return;
    }
    if (state.settingsPanelOpen) { hideSelectBar(); return; }
    const selection = currentBrowserSelection();
    const desc = describeSelection(selection);
    if (!desc) { hideSelectBar(); return; }
    showSelectBar(desc);
  }, 0);
}

/**
 * 调试/自动化入口：像鼠标那样选中指定段落里的一段文字，然后弹出浮条。
 * 与 `__arDebug` 的其它字段同一性质（可断言的显式状态），不是业务逻辑；
 * 无头验证用它复现「跨段选区」，免得拖拽的像素位置受字体渲染影响。
 *
 * 注意：主栏块与抽屉原文**共用段号**，所以 `from` 必须显式给（默认 rewritten）。
 */
function debugSelect({ paragraph, from = 'rewritten', start = 0, length = 8 }) {
  window.setTimeout(() => {
    const node = from === 'source'
      ? document.querySelector(`.src-para[data-paragraph="${paragraph}"]`)
      : document.querySelector(`#rewritten .block[data-paragraph="${paragraph}"]`);
    const scope = node ? findSelectionScope(node) : null;
    if (!node || !scope) return;
    // 选哪一段文字：主栏取 block-text，抽屉取 src-para-text
    const host = scope.from === 'source'
      ? node.querySelector('[data-role="src-para-text"]') || node
      : node.querySelector('[data-role="block-text"]') || node;
    const walker = document.createTreeWalker(host, NodeFilter.SHOW_TEXT);
    const parts = [];
    let textNode = walker.nextNode();
    while (textNode) {
      parts.push(textNode);
      textNode = walker.nextNode();
    }
    const full = parts.map((n) => n.nodeValue).join('');
    const begin = Math.max(0, Math.min(start, Math.max(0, full.length - 1)));
    const end = Math.max(begin + 1, Math.min(begin + length, full.length));
    const locate = (offset) => {
      let seen = 0;
      for (let i = 0; i < parts.length; i += 1) {
        const len = parts[i].nodeValue.length;
        if (offset <= seen + len) return { node: parts[i], offset: offset - seen };
        seen += len;
      }
      const last = parts[parts.length - 1];
      return { node: last, offset: last.nodeValue.length };
    };
    const a = locate(begin);
    const b = locate(end);
    const range = document.createRange();
    range.setStart(a.node, a.offset);
    range.setEnd(b.node, b.offset);
    const selection = currentBrowserSelection();
    if (!selection) return;
    selection.removeAllRanges();
    selection.addRange(range);
    refreshSelectionBar();
  }, 0);
}

/* ------------------------------------------------------------ 交互接线 */
function wireInteractions() {
  // 段落标记：悬停显示动作与依据；点击弹卡片；同一条标记槽里还有「太啰嗦」（当场压缩这一段）
  state.blocks.forEach((block, paragraphId) => {
    const gutter = block.querySelector('.block__gutter');
    const marker = block.querySelector('[data-role="marker"]');
    gutter.addEventListener('mouseenter', () => block.classList.add('is-hover'));
    gutter.addEventListener('mouseleave', () => block.classList.remove('is-hover'));
    marker.addEventListener('click', (event) => {
      event.stopPropagation();
      openCard(paragraphId, marker);
    });
    const verbose = block.querySelector('[data-role="too-verbose"]');
    if (verbose) {
      verbose.addEventListener('click', (event) => {
        event.stopPropagation();
        requestParagraphCompress(paragraphId);
      });
    }
  });

  // 点外部关闭卡片；Esc 也关（顺带收浮条与设置面板）
  document.addEventListener('click', (event) => {
    // 通知面板（2026-10-24）：与画像 / 显示面板并列的「点外部关闭」。
    // 面板自己的点击已被 js/notify.js 的宿主监听 stopPropagation 拦下，这里只会收到真正的外面。
    if (isNotifyOpen()
      && !notifyPanelContains(event.target)
      && !(dom.notifyToggle && dom.notifyToggle.contains(event.target))) {
      closeNotifyPanel('outside');
    }
    if (state.profilePanelOpen
      && !(dom.profilePanel && dom.profilePanel.contains(event.target))
      && !(dom.profileToggle && dom.profileToggle.contains(event.target))) {
      setProfilePanelOpen_(false);
    }
    if (state.settingsPanelOpen
      && !(dom.settingsPanel && dom.settingsPanel.contains(event.target))
      && !(dom.settingsToggle && dom.settingsToggle.contains(event.target))) {
      setSettingsPanelOpen_(false);
    }
    if (!D.cardOpen || !state.card) return;
    if (state.card.contains(event.target)) return;
    if (event.target.closest && event.target.closest('[data-role="marker"]')) return;
    closeCard('outside');
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    // 通知面板（2026-10-24）：它是最轻的一层，Esc 先关它，一次 Esc 只关一层。
    // 放在问卷之前判断——问卷开着时也必须能用 Esc 收起通知面板。
    if (isNotifyOpen()) {
      closeNotifyPanel('escape');
      return;
    }
    // 问卷优先：Esc 与问卷的「跳过」同效，由 js/survey.js 自己处理（那里还会问一次确认）。
    // 这里直接让路，免得同一个 Esc 顺带把讲解卡片也关掉。
    // （状态面板那条让路判定 2026-10-15 删掉了：面板搬到了启动页，阅读器页没有 `#status-host`，
    //   `status.js` 也压根不在这页挂上，`isStatusOpen()` 在这里恒为 false —— 死判定。）
    if (isSurveyOpen()) return;
    closeCard('escape');
    setSettingsPanelOpen_(false);
    setProfilePanelOpen_(false);
    hideSelectBar();
  });

  // ---- ① 选片讲解：选中文字 → 浮条 → 三个按钮走同一套讲解链路
  dom.selectBar.addEventListener('click', (event) => event.stopPropagation());
  // 按在浮条上的那一下不许清掉正文选区（否则点到按钮前选区就没了），
  // 同时把当时那一份选区快照留在手边——mousedown 早于 click，交接就靠它。
  dom.selectBar.addEventListener('mousedown', (event) => {
    event.preventDefault();
    event.stopPropagation();
    state.pickedSelection = state.selection;
    // 这一次 mouseup 由浮条引起：别让 refreshSelectionBar 再把浮条弹回来
    state.suppressBarRefresh = true;
    window.clearTimeout(state.barTimer);
    state.barTimer = null;
  });
  dom.selectBar.querySelectorAll('[data-role="select-explain"]').forEach((btn) => {
    btn.addEventListener('click', (event) => {
      event.stopPropagation();
      const desc = state.pickedSelection || state.selection;
      state.pickedSelection = null;
      if (!desc) return;
      openCard(desc.paragraph, dom.selectBar, {
        text: desc.text,
        preview: desc.preview,
        previewTruncated: desc.previewTruncated,
        paragraphs: desc.spans,
        chars: desc.chars,
        from: desc.from,
      });
      hideSelectBar();
      requestCardExplain(desc.paragraph, btn.dataset.kind, {
        text: desc.text,
        from: desc.from,
        preview: desc.preview,
        previewTruncated: desc.previewTruncated,
      });
    });
  });

  document.addEventListener('mouseup', refreshSelectionBar);
  document.addEventListener('keyup', (event) => {
    if (event.key === 'Shift' || event.key.startsWith('Arrow') || event.key === 'a') {
      refreshSelectionBar();
    }
  });
  document.addEventListener('mousedown', (event) => {
    if (dom.selectBar.contains(event.target)) return;
    hideSelectBar();
  });
  window.addEventListener('scroll', () => hideSelectBar(), { passive: true });
  dom.drawerList.addEventListener('scroll', () => hideSelectBar(), { passive: true });

  dom.drawerToggle.addEventListener('click', () => setDrawer(!D.drawerOpen));

  // 当前段 = 最后一个「顶端越过阅读线」的段。阅读线取视口高度的 35%，
  // 这样任意滚动位置都有确定结果（比「谁在一条窄带里」稳，短段也不会被跳过）。
  let ticking = false;
  const onScroll = () => {
    if (ticking) return;
    ticking = true;
    window.requestAnimationFrame(() => {
      ticking = false;
      updateCurrentFromScroll();
    });
  };
  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', onScroll);

  // 读完（触到底）才提示压缩了多少；读中不常驻
  const endObserver = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) markReadComplete();
  }, { threshold: 0 });
  endObserver.observe(dom.sentinel);
}

/** 按阅读线算当前段（只在滚动/尺寸变化时跑，不做轮询）。 */
export function readingLineParagraph(page_paragraphs, nodes, lineRatio = 0.35) {
  const line = window.innerHeight * lineRatio;
  let best = null;
  page_paragraphs.forEach((p) => {
    const node = nodes.get(p.id);
    if (!node) return;
    if (node.getBoundingClientRect().top <= line) best = p.id;
  });
  if (!best && page_paragraphs.length) best = page_paragraphs[0].id;
  return best;
}

function updateCurrentFromScroll() {
  setCurrent(readingLineParagraph(state.paragraphs, state.blocks));
}

/* ------------------------------------------------------------ 读前背景问卷 */

/**
 * 问一句服务端画像要不要弹问卷（PLAN-background-survey §7/§9）。
 *
 * 四条纪律：
 *   - **放在正文之后**：`finishReady()` 已置位、正文已可读；这里失败也不影响阅读；
 *   - **判定只看服务端画像**：`answers` 非空或 `skipped` 为真就不弹（不看 localStorage）；
 *   - **不打扰就是页面上一个字都不多**：`already-answered` / `skipped-before` 这一档
 *     **不弹横幅**（2026-10-15 起那条「已答过 / 已跳过」的说明横幅整条去掉），
 *     原因只落在 `__arDebug.surveyBootReason` 上供无头验证断言；`?survey=1` 这条出路
 *     写在 `web/README.md` 的交互段里，不靠横幅提醒读者；
 *   - **不 await**：问卷取数是异步的，`ready` 不等它（等它就等于让阅读等问卷）。
 *     无头验证等的是 `__arDebug.surveyOpen` / `surveyEvents` 这些问卷自己的显式状态。
 */
function bootSurvey_() {
  bootSurvey({
    ctx: state.ctx,
    banner: (text, kind) => addBanner(dom.banners, text, bannerState(kind)),
    degrade,
  }).then((result) => {
    // 只记原因（无头验证断言它），不弹横幅：已有背景 / 此前跳过时问卷不弹
    D.surveyBootReason = result && result.reason ? result.reason : null;
  }).catch((err) => {
    // bootSurvey 内部已把失败转成可见降级；这里只兜底「连它自己都炸了」的情况
    D.errors.push({ kind: 'survey-boot', message: String((err && err.message) || err) });
    degrade('survey-boot', `背景问卷没能启动（${err && err.message ? err.message : err}）：本次按「未提供背景」处理，阅读照常。`);
  });
}

/* ------------------------------------------------------------ 启动 */

async function boot() {
  // 无头验证入口（同 __arDebug 的其它字段：可断言的显式状态，不是业务逻辑）
  D.debugSelect = debugSelect;
  D.debugEvent = (kind, payload) => emitEvent(kind, payload || {});
  D.debugFlush = (reason) => flushEvents(reason || 'manual');

  // ① 选片讲解浮条：先造出来（无头验证要能查到 [data-role="select-bar"]），选区变化时才显示
  const bar = buildSelectBar();
  if (dom.selectBar) dom.selectBar.replaceWith(bar);
  dom.selectBar = bar;
  hideSelectBar();

  // ② 显示设置先落地：首访跟随系统深浅色，用户手动选过就以手动为准（存档在 ar.settings.v1）
  state.settings = bootstrapSettings(document.body);
  D.settings = { ...state.settings };
  mountSettings();
  syncDisplaySettings();       // 量一次对比度：手动组合读不清时设置面板里会出现告警
  D.profileSignal = state.profileSignal;

  state.ctx = readContext(window.location.search);
  D.mode = state.ctx.mode;
  D.sourceId = state.ctx.sourceId;
  dom.body.dataset.mode = state.ctx.mode;
  dom.modeBadge.dataset.mode = state.ctx.mode;
  dom.modeBadge.textContent = state.ctx.mode === 'fixture' ? '夹具模式' : '正式模式 /api';
  dom.sourceBadge.textContent = `材料：${state.ctx.sourceId || '（未指定）'}`;

  // 画像面板（2026-10-22）：入口常驻顶栏，**只在读者点开时**才去读 /api/profile（不挡阅读）
  mountProfile();

  // 通知中心（2026-10-24）：顶栏那颗「通知」按钮 + 横幅收起 / 失效后的历史（纯内存）。
  // 重弹走 addBanner 的 `record: false`：历史里已经有这条，重弹再记一条会让同一条在历史里翻倍。
  mountNotify({
    host: dom.notifyHost,
    toggle: dom.notifyToggle,
    replay: (item) => addBanner(dom.banners, item.text, item.state, { record: false }),
  });

  if (state.ctx.mode === 'fixture') {
    addBanner(dom.banners,
      `夹具模式：数据来自静态文件 samples/${state.ctx.sourceId}/doc.json，不是后端 /api/*，也不是模型实时产出。`);
  }

  // ④ 行为埋点（PLAN-actions-feedback §7.1/§7.2）：缓冲 + 阈值/定时 flush + 离场强刷。
  //    放在取正文之前挂：正文渲染本身也产生信号（dwell / 读完 / 讲解）。
  //    `api` 只给 `postEvents`——埋点这条路**碰不到**讲解 / 压缩（§9 的纪律）。
  initEvents({
    ctx: state.ctx,
    api: postEvents,
    sourceId: state.ctx.sourceId,
    banner: (text, kind) => addBanner(dom.banners, text, bannerState(kind)),
    degrade,
    preFlush: settleBeforeFlush,
  });
  D.eventsFlushIntervalMs = FLUSH_INTERVAL_MS;

  try {
    const { doc, url } = await loadDoc(state.ctx);
    renderDoc(doc, url);
    wireInteractions();
    updateCurrentFromScroll();
  } catch (err) {
    fatal(err instanceof DataError ? err : new DataError(String(err && err.message || err)));
    return;
  }
  finishReady();

  // ③ 正文就绪之后才问「要不要弹背景问卷」：不挡阅读；正文渲染失败时压根走不到这里（PLAN §7）
  bootSurvey_();
}

boot();
