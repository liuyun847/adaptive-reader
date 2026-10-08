/**
 * 阅读显示设置：背景 3 档 × 文字 2 档 × 正文字号 4 档。
 *
 * 三条纪律：
 *   1. **渲染前应用**：`index.html` 里有一段同步脚本按同一套口径先给 `<body>` 打上属性，
 *      本模块的 `bootstrap()` 再接管。所以刷新时不会先闪一下纸白底。
 *   2. **驱动方式是 body 上的属性**（`data-bg` / `data-ink` / `data-size`），
 *      颜色全部由 reader.css 的变量承担——页面里不再散落硬编码颜色。
 *   3. **首访跟随系统深浅色**（`prefers-color-scheme`）；用户手动选过之后以手动为准
 *      （`localStorage["ar.settings.v1"]` 里存了 `bg` 就算手动选过）。
 *
 * 靠对比度校验定的取值（相对比度，WCAG 2.1）：`--muted` / `--panel-muted` 取 #5f6874，
 * 纸白 5.41:1 / 暖黄 4.82:1 / 白面板 5.65:1，都过 AA 4.5:1（旧值 #6b7480 在暖黄底只有 4.04:1）。
 *
 * 第四条纪律（2026-10-08 复核后补）：**手动组合出读不清的颜色要说出来**。
 * 背景与文字颜色是两个独立档位，理论上能组合出「深色背景 + 深墨文字」这种 1.2:1 的正文。
 * 这类组合不拦（用户有权这么选），但设置面板里会出现一条可见的对比度告警
 * （`contrastWarning()` 的文案 + `[data-role="settings-warning"]` 元素），
 * 实测值同时留在 `globalThis.__arDebug.inkContrast` 里，无头验证可断言。
 */

export const STORAGE_KEY = 'ar.settings.v1';

export const BG_VALUES = ['paper', 'warm', 'dark'];
export const INK_VALUES = ['ink', 'light'];
export const SIZE_VALUES = ['15', '16', '17', '19'];

export const BG_LABELS = { paper: '纸白', warm: '暖黄', dark: '深色' };
export const INK_LABELS = { ink: '深墨', light: '浅灰' };
export const SIZE_LABELS = { 15: '小 15px', 16: '中 16px', 17: '大 17px', 19: '特大 19px' };

/** 深色背景配浅色字——用户没手动指定文字颜色时跟着背景走。 */
export const DERIVED_INK = { paper: 'ink', warm: 'ink', dark: 'light' };

/** 正文类文字的对比度门槛（WCAG 2.1 AA，正文与小字同一档）。 */
export const MIN_CONTRAST = 4.5;

/* ------------------------------------------------- 对比度（可见告警用，纯函数） */

function channelToLinear(value) {
  const v = Math.min(255, Math.max(0, Number(value) || 0)) / 255;
  return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

/** 解析 getComputedStyle 给的颜色串（`rgb()` / `rgba()`）→ `{r,g,b,a}`；解析不了返回 null。 */
export function parseColor(color) {
  const text = String(color == null ? '' : color).trim();
  const match = text.match(/^rgba?\(([^)]+)\)$/i);
  if (!match) return null;
  const parts = match[1].split(/[\s,/]+/).filter(Boolean).map(Number);
  if (parts.length < 3 || parts.slice(0, 3).some((n) => !Number.isFinite(n))) return null;
  const alpha = parts.length > 3 && Number.isFinite(parts[3]) ? parts[3] : 1;
  return { r: parts[0], g: parts[1], b: parts[2], a: alpha };
}

/** 相对亮度（WCAG 2.1 定义）；颜色解析不了返回 null。 */
export function relativeLuminance(color) {
  const c = typeof color === 'string' ? parseColor(color) : color;
  if (!c) return null;
  return 0.2126 * channelToLinear(c.r) + 0.7152 * channelToLinear(c.g) + 0.0722 * channelToLinear(c.b);
}

/** 相对比度（1–21）；任一颜色解析不了返回 null。 */
export function contrastRatio(colorA, colorB) {
  const a = relativeLuminance(colorA);
  const b = relativeLuminance(colorB);
  if (a == null || b == null) return null;
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

/**
 * 量一次「正文栏这组颜色」的实际对比度：探针元素继承 `<body>` 的 `color`（= `--ink`），
 * 背景取根节点的 `background-color`（= `--bg`）。取的是 **getComputedStyle 算好的颜色**，
 * 所以 CSS 里改了取值这里不会漂。拿不到就返回 null（不抛）。
 */
export function measureInkContrast(doc = typeof document === 'undefined' ? null : document, root = null) {
  if (!doc || typeof doc.createElement !== 'function') return null;
  const target = root || doc.body || doc.documentElement;
  if (!target || typeof globalThis.getComputedStyle !== 'function') return null;
  const probe = doc.createElement('span');
  probe.setAttribute('aria-hidden', 'true');
  probe.style.cssText = 'position:absolute;left:-9999px;top:0;width:0;height:0;visibility:hidden;';
  probe.textContent = '×';
  target.appendChild(probe);
  try {
    const ink = globalThis.getComputedStyle(probe).color;
    const bg = globalThis.getComputedStyle(target).backgroundColor;
    const parsed = parseColor(bg);
    if (!parsed || parsed.a === 0) return null;   // 底色透明（还没轮到 body）：量不出东西
    return contrastRatio(ink, bg);
  } catch (err) {
    return null;
  } finally {
    probe.remove();
  }
}

/** 对比度不足时的可见提示文案；够（或量不出来）就返回空串。 */
export function contrastWarning(ratio) {
  if (typeof ratio !== 'number' || !Number.isFinite(ratio) || ratio >= MIN_CONTRAST) return '';
  return `正文栏这组颜色读不清：文字与背景的对比度只有 ${ratio.toFixed(2)}:1`
    + `（AA 要求 ≥ ${MIN_CONTRAST}:1）。点「恢复默认」，或把「文字颜色」改回跟随背景的那一档。`;
}

export const DEFAULT_SETTINGS = {
  bg: 'paper',
  ink: 'ink',
  size: '16',
  inkExplicit: false,
};

function pick(value, allowed, fallback) {
  return allowed.includes(value) ? value : fallback;
}

/** 把任意来源（localStorage / 系统偏好 / 测试）的取值规整成一份可用的设置。 */
export function normalizeSettings(raw) {
  const src = raw && typeof raw === 'object' ? raw : {};
  const inkExplicit = src.inkExplicit === true && INK_VALUES.includes(src.ink);
  const bg = pick(src.bg, BG_VALUES, DEFAULT_SETTINGS.bg);
  const ink = inkExplicit ? src.ink : DERIVED_INK[bg];
  return { bg, ink, size: pick(String(src.size), SIZE_VALUES, DEFAULT_SETTINGS.size), inkExplicit };
}

/** 读 localStorage；读不到 / 值不认识 / 存储被禁用都按默认走（不抛）。 */
export function readStored(storage) {
  let store = storage;
  if (store === undefined) {
    try {
      store = globalThis.localStorage || null;
    } catch (err) {
      store = null;
    }
  }
  if (!store || typeof store.getItem !== 'function') return null;
  try {
    const raw = store.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    // 存过 bg 才算「用户手动选过」；没手动选过的不当作手动（首访仍跟随系统）
    if (!BG_VALUES.includes(parsed.bg)) return null;
    return normalizeSettings(parsed);
  } catch (err) {
    return null;
  }
}

export function saveSettings(settings, storage) {
  let store = storage;
  if (store === undefined) {
    try {
      store = globalThis.localStorage || null;
    } catch (err) {
      store = null;
    }
  }
  if (!store || typeof store.setItem !== 'function') return false;
  try {
    store.setItem(STORAGE_KEY, JSON.stringify(normalizeSettings(settings)));
    return true;
  } catch (err) {
    return false;
  }
}

/** 系统深浅色：只有 dark 算深色，其余（含 matchMedia 不存在）按浅色。 */
export function systemSettings(matchMedia) {
  const mm = matchMedia
    || (typeof globalThis.matchMedia === 'function' ? globalThis.matchMedia.bind(globalThis) : null);
  let dark = false;
  try {
    dark = Boolean(mm && mm('(prefers-color-scheme: dark)').matches);
  } catch (err) {
    dark = false;
  }
  return { bg: dark ? 'dark' : 'paper', ink: dark ? 'light' : 'ink', size: DEFAULT_SETTINGS.size, inkExplicit: false };
}

/** 首访（或存档无效）时用系统深浅色兜底，并把结论落盘，之后就以手动为准。 */
export function initialSettings(storage, matchMedia) {
  const stored = readStored(storage);
  if (stored) return stored;
  const sys = systemSettings(matchMedia);
  saveSettings(sys, storage);
  return sys;
}

/**
 * 把设置打到根节点上（默认 `<body>`）。幂等，返回生效的那一份。
 * 脚本在 `<body>` 之前跑时 `document.body` 还不存在，`documentElement` 是兜底。
 */
export function applySettings(settings, root, doc = typeof document === 'undefined' ? null : document) {
  const target = root || (doc && (doc.body || doc.documentElement));
  const effective = normalizeSettings(settings);
  if (target && target.dataset) {
    target.dataset.bg = effective.bg;
    target.dataset.ink = effective.ink;
    target.dataset.size = effective.size;
    target.dataset.inkExplicit = String(effective.inkExplicit);
  }
  return effective;
}

/**
 * 页面启动时接管：读存档（没有就跟随系统深浅色并落盘）→ 打到 `<body>` 上 → 返回生效值。
 * `index.html` 里那段同步脚本已经在渲染前做过同一件事，这里做的是「补漏 + 把结论交给调用方」。
 */
export function bootstrapSettings(root, storage, matchMedia) {
  const effective = initialSettings(storage, matchMedia);
  return applySettings(effective, root);
}

/** 点一下设置项：写内存 + 存 localStorage + 立刻生效。 */
export function updateSettings(context, patch, storage) {
  const next = normalizeSettings({ ...context.settings, ...patch });
  context.settings = next;
  saveSettings(next, storage === undefined ? context.storage : storage);
  return applySettings(next, context.root);
}

/** 恢复默认：清掉存档、回到「跟随系统深浅色」的初始状态。 */
export function resetSettings(context, storage) {
  const store = storage === undefined ? context.storage : storage;
  const sys = systemSettings();
  context.settings = sys;
  try {
    if (store && typeof store.removeItem === 'function') store.removeItem(STORAGE_KEY);
  } catch (err) {
    /* 存储被禁用：内存里已经是默认值，照常返回 */
  }
  saveSettings(sys, store);
  return applySettings(sys, context.root);
}

const SETTINGS_PRESETS = [
  { key: 'bg', label: '背景', role: 'settings-bg', values: BG_VALUES, labels: BG_LABELS },
  { key: 'ink', label: '文字颜色', role: 'settings-ink', values: INK_VALUES, labels: INK_LABELS },
  { key: 'size', label: '正文字号', role: 'settings-size', values: SIZE_VALUES, labels: SIZE_LABELS },
];

function presetRow(preset, current) {
  const row = document.createElement('div');
  row.className = 'settings__row';

  const label = document.createElement('span');
  label.className = 'settings__label';
  label.textContent = preset.label;
  row.appendChild(label);

  const group = document.createElement('div');
  group.className = 'settings__group';
  group.setAttribute('role', 'radiogroup');
  group.setAttribute('aria-label', preset.label);

  preset.values.forEach((value) => {
    const option = document.createElement('label');
    option.className = 'settings__option';
    const input = document.createElement('input');
    input.type = 'radio';
    input.name = `ar-settings-${preset.key}`;
    input.value = value;
    input.dataset.setting = preset.key;
    input.dataset.role = preset.role;
    input.checked = String(current[preset.key]) === String(value);
    const text = document.createElement('span');
    text.textContent = preset.labels[value] || value;
    option.appendChild(input);
    option.appendChild(text);
    group.appendChild(option);
  });

  row.appendChild(group);
  return row;
}

/**
 * 造设置面板（topbar 的「Aa」按钮点开）。
 * 面板本身只负责显示与收集点击，改设置走 `onPick`。
 */
export function buildSettingsPanel(doc = document) {
  const panel = doc.createElement('div');
  panel.className = 'settings';
  panel.dataset.role = 'settings-panel';
  panel.dataset.state = 'closed';
  panel.hidden = true;
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', '阅读显示设置');

  const head = doc.createElement('div');
  head.className = 'settings__head';
  const title = doc.createElement('span');
  title.className = 'settings__title';
  title.textContent = '阅读显示';
  head.appendChild(title);
  const close = doc.createElement('button');
  close.type = 'button';
  close.className = 'settings__close';
  close.dataset.role = 'settings-close';
  close.setAttribute('aria-label', '关闭显示设置');
  close.textContent = '×';
  head.appendChild(close);
  panel.appendChild(head);

  SETTINGS_PRESETS.forEach((preset) => panel.appendChild(presetRow(preset, DEFAULT_SETTINGS)));

  // 「正文栏这组颜色读不清」的可见提示（默认空着；由 syncSettingsPanel 按实测对比度填）
  const warn = doc.createElement('div');
  warn.className = 'settings__warn';
  warn.dataset.role = 'settings-warning';
  warn.hidden = true;
  panel.appendChild(warn);

  const foot = doc.createElement('div');
  foot.className = 'settings__foot';
  const reset = doc.createElement('button');
  reset.type = 'button';
  reset.className = 'settings__reset';
  reset.dataset.role = 'settings-reset';
  reset.textContent = '恢复默认';
  foot.appendChild(reset);
  const note = doc.createElement('span');
  note.className = 'settings__note';
  note.dataset.role = 'settings-note';
  note.textContent = '只改正文栏的字号；抽屉与卡片保持界面字号。';
  foot.appendChild(note);
  panel.appendChild(foot);

  return panel;
}

/**
 * 把面板里的选中项对齐到当前设置。
 * `contrast`（可选）：正文栏当前的实测对比度（`measureInkContrast()` 的返回值）——
 * 低于 AA 门槛时把告警文字显示出来（手动组合读不清必须说出来，不静默）。
 */
export function syncSettingsPanel(panel, settings, contrast = null) {
  if (!panel) return;
  const current = normalizeSettings(settings);
  panel.querySelectorAll('input[data-setting]').forEach((input) => {
    input.checked = String(current[input.dataset.setting]) === String(input.value);
  });
  const note = panel.querySelector('[data-role="settings-note"]');
  if (note) {
    note.textContent = current.inkExplicit
      ? '文字颜色：你手动指定过，不会跟着背景自动变。恢复默认后重新跟随背景。'
      : '只改正文栏的字号；抽屉与卡片保持界面字号。文字颜色默认跟着背景走。';
  }
  const warn = panel.querySelector('[data-role="settings-warning"]');
  if (warn) {
    const text = contrastWarning(contrast);
    warn.textContent = text;
    warn.hidden = !text;
  }
}

export function setSettingsPanelOpen(panel, button, open) {
  if (!panel) return;
  panel.hidden = !open;
  panel.dataset.state = open ? 'open' : 'closed';
  if (button) button.setAttribute('aria-expanded', String(Boolean(open)));
}

export { SETTINGS_PRESETS };
