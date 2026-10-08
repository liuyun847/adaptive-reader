/**
 * 视图层：把数据造成 DOM。只负责「造」，不负责「什么时候造」——交互都在 main.js。
 *
 * 所有可断言的状态都落在 data-* 上（data-paragraph / data-action / data-current / data-state /
 * data-unrewritten），无头验证既断言文字也断言这些属性，避免只靠样式猜状态。
 */

import { renderInto } from './md.js';
import { changeSummary, DEFERRED_NOTE } from './schema.js';

export const EXPLAIN_KINDS = [
  { kind: 'explain', label: '没懂' },
  { kind: 'simpler', label: '再简单点' },
  { kind: 'detail', label: '再详细点' },
];

/** 主栏一个段落块：左侧动作标记 + 悬停说明，右侧正文。 */
export function buildParagraphBlock({ paragraphId, text, change, unrewritten = false, sourceText = null }) {
  const block = document.createElement('div');
  block.className = 'block';
  block.id = `blk-${paragraphId}`;
  block.dataset.paragraph = paragraphId;
  block.dataset.unrewritten = String(Boolean(unrewritten));

  // ---- 标记槽：动作 + 依据摘要
  const summary = changeSummary(change);
  const gutter = document.createElement('div');
  gutter.className = 'block__gutter';

  const marker = document.createElement('button');
  marker.type = 'button';
  marker.className = 'marker';
  marker.dataset.role = 'marker';
  marker.dataset.paragraph = paragraphId;
  marker.dataset.action = summary.action || 'none';
  marker.setAttribute('aria-label', `段落 ${paragraphId} 的改写动作：${summary.label}；点击查看讲解`);
  gutter.appendChild(marker);

  const tip = document.createElement('div');
  tip.className = 'tip';
  tip.dataset.role = 'tip';
  tip.dataset.paragraph = paragraphId;
  tip.setAttribute('role', 'tooltip');

  const tipAction = document.createElement('span');
  tipAction.className = 'tip__action';
  tipAction.dataset.role = 'tip-action';
  tipAction.textContent = summary.action ? `动作：${summary.label}` : summary.label;
  tip.appendChild(tipAction);

  const tipNote = document.createElement('span');
  tipNote.className = 'tip__note';
  tipNote.dataset.role = 'tip-note';
  tipNote.textContent = summary.note;
  tip.appendChild(tipNote);

  if (summary.profile.length) {
    const tipProfile = document.createElement('span');
    tipProfile.className = 'tip__profile';
    tipProfile.dataset.role = 'tip-profile';
    tipProfile.textContent = `依据画像：${summary.profile.join('、')}`;
    tip.appendChild(tipProfile);
  }

  if (summary.deferred) {
    const tipDeferred = document.createElement('span');
    tipDeferred.className = 'tip__deferred';
    tipDeferred.dataset.role = 'tip-deferred';
    tipDeferred.textContent = `（${DEFERRED_NOTE}）`;
    tip.appendChild(tipDeferred);
  }

  gutter.appendChild(tip);
  block.appendChild(gutter);

  // ---- 正文
  const body = document.createElement('div');
  body.className = 'block__text';
  body.dataset.role = 'block-text';

  if (unrewritten) {
    const tag = document.createElement('span');
    tag.className = 'unrewritten-tag';
    tag.dataset.role = 'unrewritten-tag';
    tag.textContent = '未改写（显示原文）';
    body.appendChild(tag);
    const p = document.createElement('p');
    p.textContent = sourceText == null ? text : sourceText;
    body.appendChild(p);
  } else {
    renderInto(body, text);
  }

  block.appendChild(body);
  return block;
}

/** 抽屉里的一条原文。 */
export function buildSourceParagraph(paragraph) {
  const el = document.createElement('div');
  el.className = 'src-para';
  el.dataset.role = 'src-para';
  el.dataset.paragraph = paragraph.id;
  el.dataset.current = 'false';

  const id = document.createElement('span');
  id.className = 'src-para__id';
  id.textContent = `原文 ${paragraph.id}`;
  el.appendChild(id);

  const text = document.createElement('span');
  text.className = 'src-para__text';
  text.dataset.role = 'src-para-text';
  text.textContent = paragraph.text;
  el.appendChild(text);

  return el;
}

/**
 * 讲解卡片。正文区（可滚）与状态区（底部固定，不随长答案滚出视口）分开，
 * 降级文案写在状态区（卡片位置上，不是静默空白）。
 */
export function buildCard() {
  const card = document.createElement('div');
  card.className = 'card';
  card.dataset.role = 'card';
  card.dataset.cardState = 'closed';
  card.setAttribute('role', 'dialog');
  card.setAttribute('aria-label', '段落讲解卡片');
  card.hidden = true;

  const head = document.createElement('div');
  head.className = 'card__head';

  const pid = document.createElement('span');
  pid.className = 'card__id';
  pid.dataset.role = 'card-paragraph';
  head.appendChild(pid);

  const action = document.createElement('span');
  action.className = 'card__action';
  action.dataset.role = 'card-action';
  head.appendChild(action);

  const scope = document.createElement('span');
  scope.className = 'card__scope';
  scope.dataset.role = 'card-scope';
  scope.textContent = '选中片段';
  scope.hidden = true;
  head.appendChild(scope);

  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'card__close';
  close.dataset.role = 'card-close';
  close.setAttribute('aria-label', '关闭讲解卡片');
  close.textContent = '×';
  head.appendChild(close);

  card.appendChild(head);

  // 正文内容滚动区（头部与底部固定区不参与滚动，长答案也点得到按钮）
  const content = document.createElement('div');
  content.className = 'card__content';
  content.dataset.role = 'card-content';
  card.appendChild(content);

  // 底部固定区（2026-10-08 复核修）：meta + 状态行 + 动作按钮都在这里，
  // 它们在 .card__foot 里、**不随内容滚动**——长答案下「后端缓存命中 / 片段被截断」也一眼可见。
  const foot = document.createElement('div');
  foot.className = 'card__foot';
  foot.dataset.role = 'card-foot';
  card.appendChild(foot);

  // 片段模式：选出的是哪一段、跨了几段都在这里说清楚（整段模式整行隐藏）
  const pick = document.createElement('div');
  pick.className = 'card__pick';
  pick.dataset.role = 'card-pick';
  pick.hidden = true;
  const pickMeta = document.createElement('div');
  pickMeta.className = 'card__pick-meta';
  pickMeta.dataset.role = 'card-pick-meta';
  pick.appendChild(pickMeta);
  const pickPreview = document.createElement('div');
  pickPreview.className = 'card__pick-preview';
  pickPreview.dataset.role = 'card-selection-preview';
  pick.appendChild(pickPreview);
  content.appendChild(pick);

  const body = document.createElement('div');
  body.className = 'card__body';
  body.dataset.role = 'card-body';
  content.appendChild(body);

  const answer = document.createElement('div');
  answer.className = 'card__answer';
  answer.dataset.role = 'card-answer';
  content.appendChild(answer);

  // 轻标记行 + 状态行：都在底部固定区（不随内容滚动），见上
  const meta = document.createElement('div');
  meta.className = 'card__meta';
  meta.dataset.role = 'card-meta';
  meta.hidden = true;
  foot.appendChild(meta);

  const status = document.createElement('div');
  status.className = 'card__status';
  status.dataset.role = 'card-status';
  status.dataset.state = 'empty';
  status.setAttribute('aria-live', 'polite');
  foot.appendChild(status);

  const actions = document.createElement('div');
  actions.className = 'card__actions';
  EXPLAIN_KINDS.forEach((spec) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.dataset.role = 'explain';
    btn.dataset.kind = spec.kind;
    btn.dataset.paragraph = '';
    btn.textContent = spec.label;
    actions.appendChild(btn);
  });
  foot.appendChild(actions);

  return card;
}

function section(label, valueNode) {
  const wrap = document.createElement('div');
  wrap.className = 'card__section';
  const labelEl = document.createElement('span');
  labelEl.className = 'card__label';
  labelEl.textContent = label;
  wrap.appendChild(labelEl);
  wrap.appendChild(valueNode);
  return wrap;
}

/**
 * 用 changes / concepts 填卡片的预生成部分（点标记时的「快」入口）。
 *
 * `selection`（可选）：{ text, preview, paragraphs, chars, from } —— 从选区来的讲解。
 * 片段模式下卡片头标明「选中片段 + 段号 + 片段预览」，并**隐藏事实锚 chips**
 * （事实锚是整段口径，对片段会误导）；概念 chips 照常显示。
 */
export function fillCard(card, { paragraphId, change, conceptsById, selection = null }) {
  const summary = changeSummary(change);
  card.dataset.paragraph = paragraphId;
  card.querySelector('[data-role="card-paragraph"]').textContent = paragraphId;

  const scopeEl = card.querySelector('[data-role="card-scope"]');
  const pick = card.querySelector('[data-role="card-pick"]');
  const pickMeta = card.querySelector('[data-role="card-pick-meta"]');
  const pickPreview = card.querySelector('[data-role="card-selection-preview"]');
  const isSelection = Boolean(selection && selection.text);
  card.dataset.scope = isSelection ? 'selection' : 'paragraph';
  scopeEl.hidden = !isSelection;
  pick.hidden = !isSelection;
  if (isSelection) {
    card.dataset.selection = selection.text;
    card.dataset.selectionFrom = selection.from || 'rewritten';
    card.dataset.selectionSpan = String(selection.paragraphs || 1);
    pickMeta.textContent = selection.paragraphs > 1
      ? `选中片段 · 第 ${paragraphId} 段（选区跨 ${selection.paragraphs} 段，仅按 ${paragraphId} 讲解）· 共 ${selection.chars} 字`
      : `选中片段 · 第 ${paragraphId} 段 · 共 ${selection.chars} 字`;
    pickPreview.textContent = selection.preview || selection.text;
    pickPreview.dataset.truncated = String(Boolean(selection.previewTruncated));
  } else {
    delete card.dataset.selection;
    delete card.dataset.selectionFrom;
    delete card.dataset.selectionSpan;
    pickMeta.textContent = '';
    pickPreview.textContent = '';
  }

  const actionEl = card.querySelector('[data-role="card-action"]');
  actionEl.textContent = summary.label;
  actionEl.dataset.action = summary.action || 'none';

  const body = card.querySelector('[data-role="card-body"]');
  body.textContent = '';

  const why = document.createElement('div');
  why.className = 'card__why';
  why.dataset.role = 'card-why';
  why.textContent = summary.note;
  body.appendChild(why);

  if (summary.profile.length) {
    const chips = document.createElement('div');
    chips.className = 'card__chips';
    summary.profile.forEach((item) => {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.dataset.role = 'card-profile-chip';
      chip.textContent = item;
      chips.appendChild(chip);
    });
    body.appendChild(section('依据画像', chips));
  }

  if (summary.confidence != null) {
    const conf = document.createElement('span');
    conf.className = 'chip';
    conf.dataset.role = 'card-confidence';
    conf.textContent = `置信度 ${summary.confidence}`;
    body.appendChild(conf);
  }

  if (summary.deferred) {
    const d = document.createElement('div');
    d.className = 'card__label';
    d.dataset.role = 'card-deferred';
    d.textContent = `⚠ ${DEFERRED_NOTE}`;
    body.appendChild(d);
  }

  // 这一段牵涉的概念（来自 concepts.json 的反向索引），含前置概念的名字
  const conceptIds = Object.values(conceptsById)
    .filter((c) => Array.isArray(c.paragraphs) && c.paragraphs.includes(paragraphId))
    .map((c) => c.id);
  if (conceptIds.length) {
    const chips = document.createElement('div');
    chips.className = 'card__chips';
    conceptIds.forEach((cid) => {
      const concept = conceptsById[cid];
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.dataset.role = 'card-concept';
      const prereqNames = (concept.prereq || [])
        .map((pid) => (conceptsById[pid] ? conceptsById[pid].name : pid))
        .join('、');
      chip.textContent = prereqNames ? `${concept.name}（前置：${prereqNames}）` : concept.name;
      chips.appendChild(chip);
    });
    body.appendChild(section('这一段牵涉的概念', chips));
  }

  const facts = (change && change.facts) || {};
  const anchors = [...(facts.numbers || []), ...(facts.quotes || [])];
  // 事实锚是整段口径（这一段里必须原样出现的数字与引文）——对片段会误导，片段模式下不显示
  if (anchors.length && !isSelection) {
    const chips = document.createElement('div');
    chips.className = 'card__chips';
    chips.dataset.role = 'card-facts';
    anchors.forEach((item) => {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.dataset.role = 'card-fact';
      chip.textContent = item;
      chips.appendChild(chip);
    });
    body.appendChild(section('事实锚（须与原文一致）', chips));
  }

  card.querySelectorAll('[data-role="explain"]').forEach((btn) => {
    btn.dataset.paragraph = paragraphId;
  });

  setCardStatus(card, 'empty', '');
  card.querySelector('[data-role="card-answer"]').textContent = '';
}

/** 卡片状态区。state ∈ empty|loading|ok|degraded|error，非 empty 时必有可见文字。 */
export function setCardStatus(card, state, text) {
  const status = card.querySelector('[data-role="card-status"]');
  status.dataset.state = state;
  status.textContent = text || '';
  return status;
}

export function setCardAnswer(card, text) {
  const answer = card.querySelector('[data-role="card-answer"]');
  answer.textContent = text || '';
  return answer;
}

export function setExplainButtonsDisabled(card, disabled) {
  card.querySelectorAll('[data-role="explain"]').forEach((btn) => {
    btn.disabled = Boolean(disabled);
  });
}

/**
 * 把卡片放到锚点旁边并夹在视口内（整段模式：贴着段落标记）。
 * 先显示再量高度，避免量到 0。
 *
 * 用「下边/左边」定位而不是 top：讲解答案会把卡片撑高，从下往上长就不会被顶出视口。
 */
export function layoutCard(card, anchor) {
  card.hidden = false;
  const rect = anchor.getBoundingClientRect();
  const width = card.offsetWidth;
  const height = card.offsetHeight;
  const margin = 10;
  let left = rect.right + margin;
  if (left + width > window.innerWidth - margin) {
    left = Math.max(margin, rect.left - width - margin);
  }
  let bottom = window.innerHeight - rect.top + 6;
  const maxBottom = window.innerHeight - height - margin;
  if (bottom > maxBottom) bottom = Math.max(margin, maxBottom);
  card.style.left = `${Math.round(left)}px`;
  card.style.top = 'auto';
  card.style.bottom = `${Math.round(Math.max(margin, bottom))}px`;
}

/**
 * 片段模式的卡片位置：固定在视口右下角。
 * 选片浮条就在选区旁，贴着锚点放容易与浮条/抽屉叠在一起 —— 固定角落最稳，也不会挡住选区。
 * 同样用 bottom 定位：卡片最高 70vh，答案再长也不会把按钮顶出视口。
 */
export function layoutCardFixed(card) {
  card.hidden = false;
  const width = card.offsetWidth;
  const margin = 14;
  const rail = 46;
  const minLeft = margin + rail;
  let left = window.innerWidth - width - margin;
  if (left < minLeft) left = minLeft;
  card.style.left = `${Math.round(left)}px`;
  card.style.top = 'auto';
  card.style.bottom = `${margin}px`;
}

/** 卡片高度随内容变化后再夹一次（配合 ResizeObserver；整段模式用锚点重算）。 */
export function repositionCard(card, anchor) {
  if (!card || card.hidden) return;
  if (anchor) layoutCard(card, anchor);
  else layoutCardFixed(card);
}

/** 页头/横幅：任何降级都要在这里留下可见文字。 */
export function addBanner(container, text, state = 'warn') {
  const el = document.createElement('div');
  el.className = 'banner';
  el.dataset.role = 'banner';
  el.dataset.state = state;
  el.textContent = text;
  container.appendChild(el);
  return el;
}

/* --------------------------------------------------- 选片讲解浮条 */

/**
 * 选中文字后浮在选区旁的小工具条：三个讲解按钮 + 段号（跨段时说明只按哪一段讲）。
 * 只造 DOM，不管什么时候出现——接线在 main.js。
 */
export function buildSelectBar() {
  const bar = document.createElement('div');
  bar.className = 'select-bar';
  bar.dataset.role = 'select-bar';
  bar.dataset.state = 'hidden';
  bar.setAttribute('role', 'toolbar');
  bar.setAttribute('aria-label', '选中片段的讲解动作');

  const arrow = document.createElement('span');
  arrow.className = 'select-bar__arrow';
  arrow.dataset.role = 'select-bar-arrow';
  arrow.setAttribute('aria-hidden', 'true');
  bar.appendChild(arrow);

  const meta = document.createElement('span');
  meta.className = 'select-bar__meta';
  meta.dataset.role = 'select-bar-meta';
  bar.appendChild(meta);

  const actions = document.createElement('span');
  actions.className = 'select-bar__actions';
  EXPLAIN_KINDS.forEach((spec) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'select-bar__btn';
    btn.dataset.role = 'select-explain';
    btn.dataset.kind = spec.kind;
    btn.textContent = spec.label;
    actions.appendChild(btn);
  });
  bar.appendChild(actions);

  return bar;
}

/** 浮条里的段号/跨段说明。 */
export function setSelectBarInfo(bar, { paragraphId, paragraphs }) {
  const meta = bar.querySelector('[data-role="select-bar-meta"]');
  if (!meta) return;
  meta.dataset.paragraph = paragraphId;
  meta.textContent = paragraphs > 1
    ? `${paragraphId}（跨 ${paragraphs} 段，按 ${paragraphId} 讲）`
    : paragraphId;
}

/** 卡片状态区上方的缓存/追加字段标记（「本次会话已生成」这类轻标记）。 */
export function setCardMeta(card, text) {
  const el = card.querySelector('[data-role="card-meta"]');
  if (!el) return null;
  el.textContent = text || '';
  el.hidden = !text;
  return el;
}
