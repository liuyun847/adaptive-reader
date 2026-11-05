/**
 * 视图层：把数据造成 DOM。只负责「造」，不负责「什么时候造」——交互都在 main.js。
 *
 * 所有可断言的状态都落在 data-* 上（data-paragraph / data-action / data-current / data-state /
 * data-unrewritten / data-repeats / data-banner-id / data-expires-at），无头验证既断言文字也断言
 * 这些属性，避免只靠样式猜状态。
 */

import { renderInto } from './md.js';
import { absoluteLocalStamp, markCollapsed as markCollapsedNotify, record as recordNotify } from './notify.js';
import {
  changeSummary, DEFERRED_NOTE, FACT_ANCHOR_POLICY_LABELS, factDeclarations, NO_PROFILE_REFS_LABEL,
  PROFILE_REFS_FILLED_BADGE, PROFILE_REFS_FILLED_TIP,
} from './schema.js';

export const EXPLAIN_KINDS = [
  { kind: 'explain', label: '没懂' },
  { kind: 'simpler', label: '再简单点' },
  { kind: 'detail', label: '再详细点' },
];

/**
 * 「太啰嗦」标记的文案（DESIGN §5 的那一行：默认不显示，点一下触发**局部压缩**）。
 * 它跟动作标记在同一个「段落标记区」（`.block__gutter`）里，悬停或键盘聚焦该段时出现。
 */
export const TOO_VERBOSE_LABEL = '太啰嗦';

/** 主栏一个段落块：左侧动作标记 + 悬停说明（含「太啰嗦」入口），右侧正文。 */
export function buildParagraphBlock({
  paragraphId, text, change, unrewritten = false, sourceText = null, fidelity = null,
}) {
  const block = document.createElement('div');
  block.className = 'block';
  block.id = `blk-${paragraphId}`;
  block.dataset.paragraph = paragraphId;
  block.dataset.unrewritten = String(Boolean(unrewritten));

  // ---- 标记槽：动作标记 + 悬停说明 + 「太啰嗦」（都在 .block__gutter 里，悬停才现身）
  const gutter = document.createElement('div');
  gutter.className = 'block__gutter';

  const marker = document.createElement('button');
  marker.type = 'button';
  marker.className = 'marker';
  marker.dataset.role = 'marker';
  marker.dataset.paragraph = paragraphId;
  marker.dataset.action = 'none';
  gutter.appendChild(marker);

  const tip = document.createElement('div');
  tip.className = 'tip';
  tip.dataset.role = 'tip';
  tip.dataset.paragraph = paragraphId;
  tip.setAttribute('role', 'tooltip');
  gutter.appendChild(tip);

  // 「太啰嗦」：不在动作标记里（那个是「看为什么」），它是「当场把这段压短」的动作入口。
  // 放在同一个标记槽里，靠 CSS 由悬停 / 键盘聚焦驱动显隐（默认不显示，见 reader.css）。
  const verbose = document.createElement('button');
  verbose.type = 'button';
  verbose.className = 'gutter-action';
  verbose.dataset.role = 'too-verbose';
  verbose.dataset.paragraph = paragraphId;
  verbose.dataset.state = 'idle';
  verbose.textContent = TOO_VERBOSE_LABEL;
  verbose.setAttribute('aria-label', `段落 ${paragraphId} 写得太啰嗦：点一下当场压缩这一段，正文会刷新`);
  gutter.appendChild(verbose);

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
  applyChangeToBlock(block, change, { fidelity });
  return block;
}

/**
 * 把变更记录写进块上的标记槽（动作 + 依据摘要 + `deferred` 说明 + 事实锚申报）。
 *
 * 单独一个函数是为了**压缩之后能就地刷新**：`applyCompressed()` 改了这一段之后要重画
 * 动作标记与悬停文案（`web/js/main.js` 的「太啰嗦」那条链路会再调一次）。
 *
 * @returns {ReturnType<typeof changeSummary>} 这次算出来的摘要（调用方可直接用）
 */
export function applyChangeToBlock(block, change, { fidelity = null } = {}) {
  const paragraphId = block.dataset.paragraph;
  const summary = changeSummary(change);
  const declaration = factDeclarations(change, fidelity);

  const marker = block.querySelector('[data-role="marker"]');
  if (marker) {
    marker.dataset.action = summary.action || 'none';
    marker.dataset.declared = String(declaration.total > 0);
    // 兜底补齐（2026-10-14）：可断言的结构化标记（页面徽标与悬停行都只认它，不猜 why.note）
    marker.dataset.profileFilled = String(summary.profileFilled);
    marker.setAttribute('aria-label', `段落 ${paragraphId} 的改写动作：${summary.label}；点击查看讲解`);
  }

  const tip = block.querySelector('[data-role="tip"]');
  if (tip) {
    tip.textContent = '';

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
      tipProfile.dataset.empty = 'false';
      tipProfile.textContent = `依据画像：${summary.profile.join('、')}`;
      tip.appendChild(tipProfile);
    } else {
      // 空数组也要说出来（2026-10-14）：什么都不显示会被读成「没有画像」——那是另一回事
      // （读者可能填了背景问卷，背景依据按契约只写在 why.note 里）。文案只说「无自评引用」。
      const tipProfile = document.createElement('span');
      tipProfile.className = 'tip__profile';
      tipProfile.dataset.role = 'tip-profile';
      tipProfile.dataset.empty = 'true';
      tipProfile.textContent = NO_PROFILE_REFS_LABEL;
      tip.appendChild(tipProfile);
    }

    if (summary.profileFilled) {
      // 兜底补齐的出处（2026-10-14）：这一段的 why.profile 不是模型给的，是骨架照读者自己的自评
      // 补的。判据是产物里的 **结构化字段** `changes[i].profile_refs_filled`，不是 why.note 的文案。
      tip.querySelector('[data-role="tip-profile"]').dataset.filled = 'true';
      const tipFilled = document.createElement('span');
      tipFilled.className = 'tip__filled';
      tipFilled.dataset.role = 'tip-profile-filled';
      tipFilled.textContent = PROFILE_REFS_FILLED_TIP;
      tip.appendChild(tipFilled);
    }

    if (declaration.total > 0) {
      // 事实锚申报（§14.1）：悬停就能看见「这一段放弃了哪几项锚点」，不必点开卡片。
      // 三种结论都要说得出：已放行 / 未放行 / **多申报**（锚点一个没少却申报放弃了它）
      const tipDeclared = document.createElement('span');
      tipDeclared.className = 'tip__declared';
      tipDeclared.dataset.role = 'tip-declared';
      tipDeclared.dataset.waived = String(declaration.waived);
      tipDeclared.dataset.overDeclared = String(declaration.overDeclared);
      const verdict = declaration.waived
        ? '（已按读者口径放行）'
        : (declaration.overDeclared ? '（多申报：本段一个都没少）' : '（未放行：口径不允许）');
      tipDeclared.textContent =
        `事实锚申报：${declaration.items.map((item) => `${item.label}「${item.text}」`).join('、')}${verdict}`;
      tip.appendChild(tipDeclared);
    }

    if (summary.deferred) {
      const tipDeferred = document.createElement('span');
      tipDeferred.className = 'tip__deferred';
      tipDeferred.dataset.role = 'tip-deferred';
      tipDeferred.textContent = `（${DEFERRED_NOTE}）`;
      tip.appendChild(tipDeferred);
    }
  }
  return summary;
}

/**
 * 换掉一个段落的正文（「太啰嗦」压缩成功之后刷新用）。
 * 走 `md.js` 的同一套渲染（压缩后的正文还是 Markdown），并摘掉可能有的「未改写」徽标。
 */
export function setBlockText(block, text) {
  const body = block.querySelector('[data-role="block-text"]');
  if (!body) return null;
  body.textContent = '';
  renderInto(body, text);
  const tag = body.querySelector('[data-role="unrewritten-tag"]');
  if (tag) tag.remove();
  block.dataset.unrewritten = 'false';
  return body;
}

/**
 * 「太啰嗦」标记的状态：`idle`（可点）/ `pending`（正在压）/ `done`（这一段已压缩）/ `error`（可重试）。
 * 文案默认回 `TOO_VERBOSE_LABEL`；`done` 之后写「已压缩」，避免同一段重复付费。
 */
export function setGutterAction(block, state, label = null) {
  const btn = block.querySelector('[data-role="too-verbose"]');
  if (!btn) return null;
  btn.dataset.state = state;
  btn.textContent = label || TOO_VERBOSE_LABEL;
  btn.disabled = state === 'pending' || state === 'done';
  return btn;
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
export function fillCard(card, { paragraphId, change, conceptsById, selection = null, fidelity = null }) {
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

  // 「依据画像」那一块交给 buildCardProfileSection（同一段代码要给压缩后的就地刷新复用，
  // 见 renderCardProfile）：这里 body 刚清空，所以插进去就是它该在的位置。
  body.appendChild(buildCardProfileSection(change));

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

  // 事实锚申报（§14.1）：这一段**没有原样保留**哪几项事实锚，以及按读者口径放行了没有。
  // 不申报时这一块整段不出现（键不出现 == 没有申报）；结构读不通就明说「申报读不出来」，不静默。
  // 片段模式下同样不显示：申报与事实锚一样是**整段口径**（对片段会误导）。
  renderCardDeclaration(card, change, fidelity);

  card.querySelectorAll('[data-role="explain"]').forEach((btn) => {
    btn.dataset.paragraph = paragraphId;
  });

  setCardStatus(card, 'empty', '');
  card.querySelector('[data-role="card-answer"]').textContent = '';
}

/**
 * 造卡片里「依据画像」那一节（引用 chips + 空的说明 + 兜底补齐徽标）。
 *
 * 判据全部来自产物：`why.profile` 为空时写 `NO_PROFILE_REFS_LABEL`（不显示会被读成「画像为空」），
 * 兜底补齐那枚徽标只认结构化字段 `changes[i].profile_refs_filled`（`changeSummary` 里的
 * `profileFilled`），**不许拿正则去猜 `why.note` 的文案**。
 */
function buildCardProfileSection(change) {
  const summary = changeSummary(change);
  if (summary.profile.length || summary.profileFilled) {
    const chips = document.createElement('div');
    chips.className = 'card__chips';
    summary.profile.forEach((item) => {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.dataset.role = 'card-profile-chip';
      chip.textContent = item;
      chips.appendChild(chip);
    });
    if (summary.profileFilled) {
      // 可见标记（2026-10-14）：这些引用**不是模型给的**，是骨架照读者自己的自评补的——
      // 读者据此知道「这条依据的出处要核对」，判据是 changes[i].profile_refs_filled。
      const badge = document.createElement('span');
      badge.className = 'chip chip--filled';
      badge.dataset.role = 'card-profile-filled';
      badge.textContent = PROFILE_REFS_FILLED_BADGE;
      chips.appendChild(badge);
    }
    return markProfileSection(section('依据画像', chips));
  }
  // 空数组也要显式说明（与悬停那一行同一句文案）：不显示会被读成「画像为空」，
  // 而「读者没提供概念自评」与「没有画像」是两件事（背景依据只写在 why.note 里）。
  const none = document.createElement('div');
  none.className = 'card__profile-empty';
  none.dataset.role = 'card-profile-empty';
  none.textContent = NO_PROFILE_REFS_LABEL;
  return markProfileSection(section('依据画像', none));
}

/** 给「依据画像」那一节盖个可定位的戳，供 `renderCardProfile` 就地换掉它。 */
function markProfileSection(wrap) {
  wrap.dataset.role = 'card-profile-section';
  return wrap;
}

/**
 * 渲染（或就地重画）卡片里「依据画像」那一块（2026-10-14）。
 *
 * 单独一个函数是为了**压缩之后能就地刷新**：在线压缩会把这一段在产物里的 `why.profile` 清空
 * （`why.note` 换成压缩说明），本段原来那份 `profile_refs_filled` 标记随之在本段产物上消失——
 * 页面若沿用压前那一份，就会出现「产物里已经没有标记、卡片上还挂着『骨架兜底补齐』徽标」。
 * 与 `renderCardDeclaration` 同一条纪律：**只认产物字段**，不猜 `why.note` 的文案。
 *
 * 位置纪律：有过旧的就**原地换掉**（不是追加到卡片末尾），免得压缩后卡片的段落顺序变样。
 *
 * @returns {HTMLElement|null} 新插入的那一节（拿不到 `card-body` 时是 null）
 */
export function renderCardProfile(card, change) {
  const body = card.querySelector('[data-role="card-body"]');
  if (!body) return null;
  const fresh = buildCardProfileSection(change);
  const stale = body.querySelector('[data-role="card-profile-section"]');
  if (stale) {
    stale.replaceWith(fresh);
  } else {
    body.appendChild(fresh);
  }
  return fresh;
}

/**
 * 渲染（或摘掉）卡片里「本段申报（事实锚）」那一块（§14.1）。
 *
 * 单独一个函数是为了**压缩之后能就地刷新**：在线压缩（`POST /api/compress` → `compress-v2`）
 * 现在也可能带回一份申报与放行结论，`web/js/main.js` 那条链路压完要重画这一块
 * （此前是「压缩后一律摘掉」，因为那时压缩路径没有申报通道）。
 *
 * 三条口径：
 * 1. **键不出现 == 没有申报**：整块不出现，不写「本段没有申报」这种空话；
 * 2. **片段模式不显示**：申报是整段口径（`card.dataset.scope === 'selection'` 时直接摘掉）；
 * 3. **结构读不通不静默**：`fact_declarations` 不是对象 / 里面没有可读条目时写明「申报读不出来」。
 *
 * @returns {HTMLElement|null} 新插入的那一节（没插任何东西时是 null）
 */
export function renderCardDeclaration(card, change, fidelity = null) {
  const body = card.querySelector('[data-role="card-body"]');
  if (!body) return null;
  // 先摘掉旧的那一节（含它里面的「模型说明」行）：压缩后重画时不能留一份过期申报
  body.querySelectorAll('[data-role="card-declaration"]').forEach((row) => {
    const wrapper = row.closest('.card__section') || row;
    wrapper.remove();
  });
  if (card.dataset.scope === 'selection') return null;

  const declaration = factDeclarations(change, fidelity);
  if (!(declaration.total > 0 || declaration.invalid)) return null;

  const chips = document.createElement('div');
  chips.className = 'card__chips';
  chips.dataset.role = 'card-declaration';
  if (declaration.items.length) {
    declaration.items.forEach((item) => {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.dataset.role = 'card-declaration-item';
      chip.dataset.field = item.field;
      chip.textContent = `${item.label}「${item.text}」`;
      chips.appendChild(chip);
    });
  } else {
    const broken = document.createElement('span');
    broken.className = 'chip';
    broken.dataset.role = 'card-declaration-broken';
    broken.textContent = '申报读不出来（结构不对）';
    chips.appendChild(broken);
  }
  if (declaration.waived) {
    const badge = document.createElement('span');
    badge.className = 'chip chip--waived';
    badge.dataset.role = 'card-declaration-waived';
    const label = FACT_ANCHOR_POLICY_LABELS[declaration.policy] || declaration.policy;
    badge.textContent = `已按读者口径放行${label ? `（${label}）` : ''}`;
    chips.appendChild(badge);
  } else if (declaration.overDeclared) {
    // 多申报（2026-10-14 收尾）：锚点一个没少却申报「放弃了某项」——**不是违规**（没有缺失可放行），
    // 但也不能说成「未放行：口径不允许」（那句会让人以为真丢了锚点）。用提示色单独说清。
    const badge = document.createElement('span');
    badge.className = 'chip chip--waived';
    badge.dataset.role = 'card-declaration-over';
    badge.textContent = '本段一个锚点都没少：模型多申报了（不影响校验结论）';
    chips.appendChild(badge);
  } else if (declaration.items.length) {
    const badge = document.createElement('span');
    badge.className = 'chip chip--blocked';
    badge.dataset.role = 'card-declaration-blocked';
    badge.textContent = '未放行：当前口径不允许丢掉事实锚';
    chips.appendChild(badge);
  }
  const wrap = section('本段申报（事实锚）', chips);
  if (declaration.note) {
    const note = document.createElement('div');
    note.className = 'card__label';
    note.dataset.role = 'card-declaration-note';
    note.textContent = `模型说明：${declaration.note}`;
    wrap.appendChild(note);
  }
  body.appendChild(wrap);
  return wrap;
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

/* --------------------------------------------------- 横幅（页头降级提示） */

/**
 * 每档横幅的驻留时长（毫秒）：成功类 **8 秒**、降级 / 失败类 **20 秒**。
 * `0` 仍是「永不自动收起」的语义（`armBannerClock` / `paintExpiry` 都留着这条分支），
 * 但当前四档**都不取 0**。
 *
 * 8 秒（`info` / `ok`，即「做成了」如「背景已保存」）：它没有待办；中文一眼扫完一行约 2–3 秒，
 * 8 秒给了「正在看别处」的读者三倍余量，而短于 5 秒来不及看清、长于 10 秒就变成新的视觉噪音。
 *
 * 20 秒（`warn` / `error`，降级与失败）：**它们同样会自己走，这不再是「把降级藏起来」**——
 * 收起的那一刻，条目会被记成一条**未读通知**（顶栏通知中心：徽标计数 + 可回看 + 点条目原样重弹，
 * 见 `js/notify.js`），可见性由通知中心兜住；占屏于是可以由时间兜底。取 20 秒而不是 8 秒，
 * 是因为降级文案通常更长（要写完原因和下一步怎么办）、需要的阅读时间更多，而「消失」不再等于「没了」。
 * 早先它们必须是 0 的理由（`web/README.md` 的「降级纪律（DESIGN §6）」：自己消失等于没报）
 * 只在「收起的条目无处可寻」时才成立——现在有历史了，那个前提不在了。
 *
 * 改时长只改这一处：`web/README.md` 的「横幅纪律」与
 * `tests/web/test_banner_lifecycle.py` 的 `AUTO_DISMISS_MS` / `DEGRADE_TTL_MS` 都按它对齐。
 */
export const BANNER_TTL_MS = { info: 8000, ok: 8000, warn: 20000, error: 20000 };

/** 成功类（`info`）的自动消失时长。老名字保留：README 与无头用例引用的是这一个。 */
export const BANNER_AUTO_DISMISS_MS = BANNER_TTL_MS.info;

/** 某一档的驻留时长（毫秒）；`0`（或没登记的档）= 永不自动收起。 */
function ttlOf(stateName) {
  const ttl = BANNER_TTL_MS[stateName];
  return Number.isFinite(ttl) && ttl > 0 ? ttl : 0;
}

/** 每条横幅的时钟：自动消失定时器 + 倒计时刷新器（关闭 / 移除时一起清掉，别让它们抓着已经不在页面上的元素）。 */
const bannerClocks = new WeakMap();

/** 横幅序号：`data-banner-id` 与通知中心里那条记录共用一个 id（收起时按它找回条目）。 */
let bannerSeq = 0;

/** 容器里同文本的那一条；只在**还在页面上**的横幅里找——关掉之后再出现算新的一条。 */
function findBannerByText(container, message) {
  const list = container.querySelectorAll('[data-role="banner"]');
  for (const el of list) {
    if ((el.dataset.text || '') === message) return el;
  }
  return null;
}

/** 清掉一条横幅的时钟（定时器 + 倒计时刷新器）。 */
function clearBannerClock(el) {
  const clock = bannerClocks.get(el);
  if (!clock) return;
  if (clock.timeout) clearTimeout(clock.timeout);
  if (clock.ticker) clearInterval(clock.ticker);
  bannerClocks.delete(el);
}

/** 剩余秒数（向上取整：剩 0.2 秒也写「还剩 1 秒」，不会先写 0 再消失）。 */
function remainingSeconds(expiresAt, now = Date.now()) {
  return Math.max(0, Math.ceil((expiresAt - now) / 1000));
}

/**
 * 刷新横幅上的「失效时间」那一段（2026-10-24）。
 *
 * 双通道：给人看的文字（`还剩 N 秒`）+ `data-expires-at`（epoch ms），悬停的 `title` 是绝对本地
 * 时刻（带时区偏移）。**不用 CSS 动画 / 过渡来「拖」时间**：到点由 `setTimeout` 真的把节点从 DOM
 * 摘掉（`tests/web/test_banner_lifecycle.py` 按 DOM 计数断言）。
 *
 * 三档（成功类 8 秒 / 降级与失败 20 秒）都走「还剩 N 秒」这一条；`expiresAt === 0` 的
 * 「不会自动收起」分支留着给「这一档没有失效时长」的情形（现在没有哪一档这样配）。
 */
function paintExpiry(el, expiresAt) {
  const span = el.querySelector('[data-role="banner-expiry"]');
  if (!span) return;
  if (!expiresAt) {
    span.textContent = '不会自动收起';
    span.title = '这条提示不会自动收起：看完点 × 关掉';
    return;
  }
  span.textContent = `还剩 ${remainingSeconds(expiresAt)} 秒`;
  span.title = `到 ${absoluteLocalStamp(expiresAt)} 自动收起`;
}

/**
 * 给一条横幅排上「失效时间 + 自动消失」：`data-expires-at`、倒计时刷新、到点移除，全在这里。
 *
 * 同一文本重复触发时也从**最后一次**出现重新计时（`armBannerClock` 会先清掉上一轮的时钟）。
 * 返回这一轮的失效时刻（epoch ms；0 = 永不），通知中心那条记录用它。
 *
 * 三档都排时钟：成功类 8 秒、降级与失败 20 秒（`BANNER_TTL_MS`）。到点移除时 reason 是 `timeout`，
 * `removeBanner` 会把这条「收起」进通知中心——所以降级 / 失败自己走之后，条目仍在历史里、未读 +1。
 */
function armBannerClock(el, stateName) {
  clearBannerClock(el);
  const ttl = ttlOf(stateName);
  const expiresAt = ttl > 0 ? Date.now() + ttl : 0;
  el.dataset.expiresAt = String(expiresAt);
  el.title = expiresAt
    ? `到 ${absoluteLocalStamp(expiresAt)} 自动收起（这条提示会自己走，也可以点 × 现在关；收起后留在顶栏「通知」里）`
    : '不会自动收起：这一档没有设失效时长';
  paintExpiry(el, expiresAt);
  if (!expiresAt) return expiresAt;
  bannerClocks.set(el, {
    timeout: setTimeout(() => removeBanner(el, 'timeout'), ttl),
    ticker: setInterval(() => paintExpiry(el, expiresAt), 1000),
  });
  return expiresAt;
}

/**
 * 移掉一条横幅：清时钟 + 摘 DOM + 把它「收起」进通知中心（`×` 与到点消失走同一条路，
 * 只是 reason 不同：`close` / `timeout`）。通知历史只存内存——刷新页面即清空。
 */
function removeBanner(el, reason = 'close') {
  clearBannerClock(el);
  if (el.parentNode) el.parentNode.removeChild(el);
  markCollapsedNotify(el.dataset.bannerId || '', reason);
}

/**
 * 页头/横幅：任何降级都要在这里留下可见文字。
 *
 * 四条显示层纪律（2026-10-09 修「横幅不消失、还会重复堆叠」；2026-10-24 补失效时间与通知中心）：
 *   - **同文本只留一条**：同内容再来时只把 `data-repeats` 加一、可读地写「已出现 N 次」，
 *     不再叠一条（重复触发照样各记一条 `__arDebug.degradations`——去重属显示层，不是记录层）；
 *   - **每条都能手动关**：`×` 按钮，鼠标与键盘等价（原生 button：Enter/Space 即点），
 *     关掉一条不动别的；
 *   - **到点自己收起**：`info` / `ok` 8 秒、`warn` / `error` 20 秒（见 `BANNER_TTL_MS`）；
 *     降级与失败**同样会走**——收起时条目进通知中心（未读计数 + 可回看 + 可重弹），
 *     所以「自己消失」不再等于「把降级藏起来」；
 *   - **失效时间可见 + 收起进通知中心**：每条横幅带 `span.banner__expiry`（`还剩 N 秒`，每秒刷新）、
 *     `data-expires-at`（epoch ms）与悬停的绝对时刻 `title`；消失或被关掉时由 `js/notify.js`
 *     记成一条「已收起」的历史（纯内存，可回看、可重弹）。
 *
 * 本函数只碰 DOM 与通知 store：不读不写 `__arDebug`，也不改变调用方的任何语义。
 *
 * `options.record === false`：这次**不记**通知（通知中心点条目重弹时用——历史里已经有这条，
 * 重弹再记一条只会让同一条提示在历史里翻倍）。
 */
export function addBanner(container, text, state = 'warn', options = {}) {
  if (!container) return null;
  const message = String(text == null ? '' : text);
  const keepHistory = options.record !== false;

  const existing = findBannerByText(container, message);
  if (existing) {
    const repeats = Number(existing.dataset.repeats || '1') + 1;
    existing.dataset.repeats = String(repeats);
    const counter = existing.querySelector('[data-role="banner-repeat"]');
    if (counter) {
      counter.textContent = `（同一提示已出现 ${repeats} 次）`;
      counter.hidden = false;
    }
    // 从**最后一次**出现重新计时（失效时间与倒计时一起跟着走）；
    // 没登记时长的档 ttlOf 返回 0，armBannerClock 会把它画成「不会自动收起」，不必另开分支
    armBannerClock(existing, existing.dataset.state);
    return existing;
  }

  bannerSeq += 1;
  const bannerId = `b${bannerSeq}`;

  const el = document.createElement('div');
  el.className = 'banner';
  el.dataset.role = 'banner';
  el.dataset.state = state;
  el.dataset.text = message;   // 去重比的是这一条（textContent 里还有 `×` 与重复计数）
  el.dataset.repeats = '1';
  el.dataset.bannerId = bannerId;

  const body = document.createElement('span');
  body.className = 'banner__text';
  body.textContent = message;
  el.appendChild(body);

  // 失效时间：只在文字之后、关闭按钮之前插一段**不可聚焦**的 span
  // （横幅里不许因此多出 Tab 停点，见 tests/web/test_banner_lifecycle.py 的键盘用例）
  const expiry = document.createElement('span');
  expiry.className = 'banner__expiry';
  expiry.dataset.role = 'banner-expiry';
  el.appendChild(expiry);

  const counter = document.createElement('span');
  counter.className = 'banner__repeat';
  counter.dataset.role = 'banner-repeat';
  counter.hidden = true;
  el.appendChild(counter);

  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'banner__close';
  close.dataset.role = 'banner-close';
  close.setAttribute('aria-label', '关闭这条提示');
  close.textContent = '×';
  close.addEventListener('click', (event) => {
    event.stopPropagation();
    removeBanner(el, 'close');
  });
  el.appendChild(close);

  container.appendChild(el);
  const expiresAt = armBannerClock(el, state);
  if (keepHistory) {
    recordNotify({ id: bannerId, text: message, bannerState: state, expiresAt });
  }
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
