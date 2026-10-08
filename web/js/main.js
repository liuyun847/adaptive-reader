/**
 * 阅读器入口：读 URL → 取数据 → 渲染主栏/抽屉/卡片 → 挂交互。
 *
 * 界面纪律（DESIGN §5/§6）：
 *   - 主栏是唯一常驻内容；抽屉默认折叠；讲解只出卡片，不改动正文；
 *   - 任何降级（模型不可用、请求失败、段数对不上、没有概念清单）都必须在页面上留下可见文字，
 *     同时在 globalThis.__arDebug 里留下可断言的状态标志。无头验证只等 __arDebug.ready，不 sleep。
 */

import {
  DataError, loadDoc, readContext, requestExplain,
} from './api.js';
import { asArray, splitRewritten } from './schema.js';
import {
  addBanner, buildCard, buildParagraphBlock, buildSourceParagraph, fillCard,
  layoutCard, setCardAnswer, setCardStatus, setExplainButtonsDisabled,
} from './view.js';

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
  if (open) scrollDrawerToCurrent();
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
        requestCardExplain(pid, btn.dataset.kind);
      });
    });
  }
  return state.card;
}

function openCard(paragraphId, anchor) {
  const card = ensureCard();
  fillCard(card, {
    paragraphId,
    change: state.changes.get(paragraphId) || null,
    conceptsById: D.concepts,
  });
  card.dataset.cardState = 'open';
  layoutCard(card, anchor);
  D.cardOpen = true;
  D.lastCard = {
    paragraph: paragraphId,
    action: (state.changes.get(paragraphId) || {}).action || null,
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
  D.cardCloses = (D.cardCloses || []).concat([{ reason, at: Date.now() }]);
  if (D.lastCard) D.lastCard.closed = true;
}

async function requestCardExplain(paragraphId, kind) {
  const card = state.card;
  if (!card) return;
  setExplainButtonsDisabled(card, true);
  setCardAnswer(card, '');
  setCardStatus(card, 'loading', `正在生成讲解（${kind}）…`);
  D.lastCard = { paragraph: paragraphId, kind, state: 'loading', text: '', at: Date.now() };

  try {
    const { payload, url, mode } = await requestExplain(state.ctx, { paragraph: paragraphId, kind });
    const text = typeof payload.text === 'string' ? payload.text.trim() : '';
    const fixtureMismatch = mode === 'fixture'
      && typeof payload.paragraph === 'string'
      && payload.paragraph !== paragraphId;
    const note = fixtureMismatch
      ? `（夹具模式：这份 explain.json 是 ${payload.paragraph} 的样例讲解，不是本段自己的）\n`
      : '';

    if (payload.degraded === true) {
      const copy = text || '模型不可用：本次讲解没有生成。该段原文可在右侧「原文」抽屉里对照。';
      setCardStatus(card, 'degraded', `${note}降级显示：${copy}`);
      D.lastCard = { paragraph: paragraphId, kind, state: 'degraded', text: copy, url, at: Date.now() };
      degrade('explain-degraded', `讲解降级（${paragraphId}）：${copy}`);
    } else if (!text) {
      const copy = '模型返回了空内容：本次讲解没有生成。该段原文可在右侧「原文」抽屉里对照。';
      setCardStatus(card, 'degraded', `${note}降级显示：${copy}`);
      D.lastCard = { paragraph: paragraphId, kind, state: 'degraded', text: copy, url, at: Date.now() };
      degrade('explain-empty', `讲解为空（${paragraphId}）：${copy}`);
    } else {
      setCardAnswer(card, text);
      setCardStatus(card, 'ok', `${note}讲解已生成（${kind}）。`);
      D.lastCard = { paragraph: paragraphId, kind, state: 'ok', text, url, at: Date.now() };
    }
  } catch (err) {
    const copy = `讲解生成失败：${err.message}。这一段仍可在右侧「原文」抽屉里对照原文。`;
    setCardStatus(card, 'error', copy);
    D.lastCard = {
      paragraph: paragraphId,
      kind,
      state: 'error',
      text: copy,
      url: err.url || null,
      errorKind: (err.detail && err.detail.kind) || null,
      at: Date.now(),
    };
    degrade('explain-error', `讲解请求失败（${paragraphId}）：${err.message}`);
  } finally {
    setExplainButtonsDisabled(card, false);
    D.cards.push({ ...D.lastCard });
  }
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

  // 点外部关闭卡片；Esc 也关
  document.addEventListener('click', (event) => {
    if (!D.cardOpen || !state.card) return;
    if (state.card.contains(event.target)) return;
    if (event.target.closest && event.target.closest('[data-role="marker"]')) return;
    closeCard('outside');
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeCard('escape');
  });

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
