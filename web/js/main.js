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
 */

import {
  DataError, loadDoc, loadProfileSignal, readContext, requestExplain,
} from './api.js';
import { createExplainCache } from './cache.js';
import {
  addBanner, buildCard, buildParagraphBlock, buildSelectBar, buildSourceParagraph, fillCard,
  layoutCard, layoutCardFixed, repositionCard, setCardAnswer, setCardMeta, setCardStatus,
  setExplainButtonsDisabled, setSelectBarInfo,
} from './view.js';
import { asArray, splitRewritten } from './schema.js';
import {
  cacheKey, describeSelection, findSelectionScope, normalizeSelection, selectionRect, toolbarPosition,
} from './selection.js';
import {
  MIN_CONTRAST, bootstrapSettings, buildSettingsPanel, measureInkContrast, resetSettings,
  setSettingsPanelOpen, syncSettingsPanel, updateSettings,
} from './settings.js';

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
  card: null,
  current: null,
  readComplete: false,
  settings: null,           // { bg, ink, size, inkExplicit }
  settingsCtx: null,        // updateSettings/resetSettings 要的上下文（root + storage）
  settingsPanelOpen: false,
  inkContrast: null,        // 正文栏当前的实测对比度（量不出来为 null）
  profileSignal: '',        // 会话缓存键的画像指纹（夹具模式 / 拿不到时为空串）
  profileSignalNoted: false,
  selection: null,          // 最近一次选区的描述（浮条展示中的那一份）
  cache: createExplainCache(),
  inflight: new Map(),      // tokenKey -> seq（同一张卡片同时在飞的请求）
  seq: 0,
  pending: null,            // 当前卡片请求的 token，用来丢弃迟到的响应
};

function degrade(kind, message) {
  D.degradations.push({ kind, message });
  return addBanner(dom.banners, message, kind === 'fatal' ? 'error' : 'warn');
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
    const node = buildParagraphBlock({
      paragraphId: paragraph.id,
      text: hasRewrite ? text : paragraph.text,
      change: state.changes.get(paragraph.id) || null,
      unrewritten: !hasRewrite,
      sourceText: paragraph.text,
    });
    dom.article.appendChild(node);
    state.blocks.set(paragraph.id, node);
    (hasRewrite ? rewrittenIds : unrewrittenIds).push(paragraph.id);
  });

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
  state.current = paragraphId;
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
  if (open) scrollDrawerToCurrent();
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
  state.settingsPanelOpen = Boolean(open);
  D.settingsPanelOpen = state.settingsPanelOpen;
  setSettingsPanelOpen(dom.settingsPanel, dom.settingsToggle, state.settingsPanelOpen);
}

function toggleSettingsPanel() {
  setSettingsPanelOpen_(!state.settingsPanelOpen);
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
  });
  card.dataset.cardState = 'open';
  if (selection) layoutCardFixed(card);
  else layoutCard(card, anchor);
  D.cardOpen = true;
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
  // 段落标记：悬停显示动作与依据；点击弹卡片
  state.blocks.forEach((block, paragraphId) => {
    const gutter = block.querySelector('.block__gutter');
    const marker = block.querySelector('[data-role="marker"]');
    gutter.addEventListener('mouseenter', () => block.classList.add('is-hover'));
    gutter.addEventListener('mouseleave', () => block.classList.remove('is-hover'));
    marker.addEventListener('click', (event) => {
      event.stopPropagation();
      openCard(paragraphId, marker);
    });
  });

  // 点外部关闭卡片；Esc 也关（顺带收浮条与设置面板）
  document.addEventListener('click', (event) => {
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
    closeCard('escape');
    setSettingsPanelOpen_(false);
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

/* ------------------------------------------------------------ 启动 */

async function boot() {
  // 无头验证入口（同 __arDebug 的其它字段：可断言的显式状态，不是业务逻辑）
  D.debugSelect = debugSelect;

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

  if (state.ctx.mode === 'fixture') {
    addBanner(dom.banners,
      `夹具模式：数据来自静态文件 samples/${state.ctx.sourceId}/doc.json，不是后端 /api/*，也不是模型实时产出。`);
  }

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
}

boot();
