/**
 * 冻结 schema 的读写约定（PLAN-skeleton.md §4）。
 *
 * 这里的函数是纯函数，页面与测试都靠它们把 changes.json / concepts.json 翻译成人话，
 * 免得「动作中文名」这类映射在几处各写一遍。
 */

/** action 枚举 → 中文名。枚举值只有这四个（PLAN-skeleton §4）。 */
export const ACTION_LABEL = {
  expand: '展开',
  compress: '压缩',
  keep: '保持',
  illustrate: '配图',
};

/** 骨架里 compress / illustrate 只留枚举位，遇到按 keep 处理（PLAN-skeleton §2.4）。 */
export const DEFERRED_NOTE = '本骨架未实现该动作，按「保持」处理';

/**
 * `why.profile` 为空时页面显示的固定文案（悬停与卡片两处共用同一句）。
 *
 * **「没有自评引用」不是「画像为空」**（DESIGN §4.4 / README §3 的纪律）：读者没有提供概念自评，
 * 但他可能填了读前背景问卷——背景非空即算「有画像」，而背景依据按契约只写在 `why.note` 里，
 * 所以空数组是**合法结果**（后端在「读者有自评、模型却没给引用」时会记一条可见降级并兜底补齐）。
 * 这里因此只如实说「没有自评引用」，一个字都不许写成「画像为空」。
 */
export const NO_PROFILE_REFS_LABEL = '依据画像：无自评引用';

/**
 * 事实锚申报（`changes[i].fact_declarations`，PLAN-actions-feedback §14.1）：
 * 三类锚点的字段名 → 中文名，与后端 `ar.fidelity` 的字段名逐字对齐。
 */
export const DECLARATION_LABELS = { numbers: '数字', quotes: '引号引文', books: '书名号' };

/**
 * 「这一段 `why.profile` 是**骨架兜底补齐**的」两处文案（2026-10-14 复核修复的可见面）。
 *
 * 现场：模型漏键 / 给错形状 / 该给引用却给空数组时，后端照**读者自己的自评**补齐 `why.profile`，
 * 并在 `meta.degraded` 与本段的 `why.note` 里留痕。此前这条信息只在产物、命令行与悬停的
 * `why.note` 长句里——卡片上看不出「这条依据是骨架补的、不是模型给的」。
 *
 * 判据是产物里的**结构化字段** `changes[i].profile_refs_filled`（见 `profileRefsFilled()`），
 * **不是** `why.note` 的文案（那是人读的散文，改一个标点就会让正则失配）。
 */
export const PROFILE_REFS_FILLED_BADGE = '骨架兜底补齐（模型没给引用）';
export const PROFILE_REFS_FILLED_TIP =
  '依据画像由骨架兜底补齐：模型没有给 why.profile 引用，这里照读者自评逐条列出';

/**
 * 这一段 `why.profile` 是不是骨架兜底补齐的（`changes[i].profile_refs_filled`）。
 *
 * 口径：**只认严格 `true`**。键不出现（老产物、或本来就不是补齐的）与形状不对（字符串 / 数字）
 * 一律当「不是」——结构化字段读不出来时不许猜，也不许拿 `why.note` 的文案兜。
 */
export function profileRefsFilled(change) {
  return Boolean(change) && change.profile_refs_filled === true;
}

/** 口径取值 → 中文说明（后端 `ar.fidelity.FACT_ANCHOR_POLICIES` 的两档）。 */
export const FACT_ANCHOR_POLICY_LABELS = {
  keep_all: '全保留（要素材）',
  allow_declared_drops: '允许申报后丢弃（要结论）',
};

/**
 * 读出「这一段申报了什么」。
 *
 * 契约（§14.1）：模型真的放弃了某项事实锚时才给 `fact_declarations`（**没有申报时整个键不出现**），
 * 形状 `{numbers:[...], quotes:[...], books:[...], note?}`。这里把它翻成可直接渲染的条目列表；
 * 结构不对（不是对象 / 数组里混进非字符串）一律**不静默**：`invalid: true` 让调用方说出来。
 *
 * `waived` / `overDeclared` 都从 `fidelity` 里读（`ar check` 的产物）：
 * - `waived`：这一段在 `fidelity.waived[]` 里 ⇒ **已按读者口径放行**；
 * - `overDeclared`：这一段在 `fidelity.over_declared[]` 里 ⇒ 锚点一个没少却申报放弃了某项
 *   （**多申报**，不影响放行结论，但要看得见——2026-10-14 收尾补的可见面）。
 *
 * @returns {{items:{field:string,label:string,text:string}[], note:string,
 *            total:number, waived:boolean, overDeclared:boolean, policy:string}}
 */
export function factDeclarations(change, fidelity = null) {
  const raw = change && change.fact_declarations;
  const empty = {
    items: [], note: '', total: 0, waived: false, overDeclared: false, policy: '', invalid: false,
  };
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return { ...empty, invalid: Boolean(raw) };
  }
  const items = [];
  Object.keys(DECLARATION_LABELS).forEach((field) => {
    const value = raw[field];
    if (value == null) return;
    const list = Array.isArray(value) ? value : [value];
    list.forEach((text) => {
      if (typeof text === 'string' && text.trim()) {
        items.push({ field, label: DECLARATION_LABELS[field], text: text.trim() });
      }
    });
  });
  const note = typeof raw.note === 'string' ? raw.note.trim() : '';
  const paragraph = change && change.paragraph;
  const rowsOf = (key) => ((fidelity && Array.isArray(fidelity[key])) ? fidelity[key] : []);
  const waived = rowsOf('waived').some((row) => row && row.paragraph === paragraph);
  const overDeclared = rowsOf('over_declared').some((row) => row && row.paragraph === paragraph);
  if (!items.length && !note) {
    return {
      ...empty,
      invalid: true,
      waived,
      overDeclared,
      policy: (fidelity && fidelity.fact_anchor_policy) || '',
    };
  }
  return {
    items,
    note,
    total: items.length,
    waived,
    overDeclared,
    policy: (fidelity && fidelity.fact_anchor_policy) || '',
    invalid: false,
  };
}

export function actionLabel(action) {
  return ACTION_LABEL[action] || `未知动作（${action}）`;
}

/**
 * 段落动作标记的悬停文案：动作 + 依据摘要，数据全部来自 changes.json。
 * @returns {{action:string, label:string, note:string, profile:string[], deferred:boolean,
 *            profileFilled:boolean, confidence:number|null}}
 */
export function changeSummary(change) {
  if (!change) {
    return {
      action: null,
      label: '无变更记录',
      note: 'changes.json 里没有这一段的记录，无法说明它被做了什么。',
      profile: [],
      deferred: false,
      profileFilled: false,
      confidence: null,
    };
  }
  const why = change.why || {};
  return {
    action: change.action,
    label: actionLabel(change.action),
    note: why.note || '（这一段没有记 why.note）',
    profile: Array.isArray(why.profile) ? why.profile.slice() : [],
    deferred: Boolean(change.deferred),
    // 兜底补齐（2026-10-14）：判据是结构化字段，不是 why.note 的文案
    profileFilled: profileRefsFilled(change),
    confidence: typeof change.confidence === 'number' ? change.confidence : null,
  };
}

/** 把 rewritten.md 按空行切成段落块（与后端切段规则一致）。 */
export function splitRewritten(markdown) {
  return String(markdown == null ? '' : markdown)
    .split(/\n[ \t]*\n/)
    .map((block) => block.trim())
    .filter((block) => block.length > 0);
}

/** changes / concepts 的响应体可能被包一层，也可能直接是数组，这里统一取出。 */
export function asArray(value, key) {
  if (Array.isArray(value)) return value;
  if (value && Array.isArray(value[key])) return value[key];
  return [];
}
