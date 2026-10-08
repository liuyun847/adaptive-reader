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

export function actionLabel(action) {
  return ACTION_LABEL[action] || `未知动作（${action}）`;
}

/**
 * 段落动作标记的悬停文案：动作 + 依据摘要，数据全部来自 changes.json。
 * @returns {{action:string, label:string, note:string, profile:string[], deferred:boolean, confidence:number|null}}
 */
export function changeSummary(change) {
  if (!change) {
    return {
      action: null,
      label: '无变更记录',
      note: 'changes.json 里没有这一段的记录，无法说明它被做了什么。',
      profile: [],
      deferred: false,
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
