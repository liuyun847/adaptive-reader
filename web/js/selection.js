/**
 * 选片讲解的选区工具（纯函数 + 一点 DOM 度量）。
 *
 * 为什么单独一个模块：主栏正文由 md.js 渲染，选区很容易跨越 `<p>` / `<strong>` / `<code>`
 * 这些行内标签而拆成多个 Range 片段；规范化口径又必须与后端缓存键一致
 * （去首尾空白 + 合并连续空白 + 全角空格 U+3000 转半角）。这些都放在这里，
 * 页面与测试都用同一份实现，免得「前端算一个键、后端算另一个键」。
 *
 * 区位约定：段落块（`#rewritten .block`）与抽屉原文（`.src-para`）都能选；
 * 选区归属看**锚点（起点）落在哪一段**（跨段时也用它，见 `describeSelection`），
 * 另记下选区碰到过几段（卡片会写明只按那一段讲）。
 */

/** 段落容器的选择器：主栏改写稿块 + 抽屉原文块。 */
export const PARAGRAPH_SELECTOR = '#rewritten .block[data-paragraph], .src-para[data-paragraph]';

/** 选区两侧的容器 → 讲解时用的来源标记（与 POST /api/explain 的 selection_from 对齐）。 */
export const SELECTION_SOURCES = {
  rewritten: { selector: '[data-role="rewritten"]', label: '改写稿' },
  source: { selector: '[data-role="drawer-list"]', label: '原文' },
};

/**
 * 契约 §4 的规范化：去首尾空白 + 合并连续空白 + 全角空格（U+3000）转半角。
 * 讲解缓存的键与后端同口径，靠这一条对齐。
 */
export function normalizeSelection(text) {
  return String(text == null ? '' : text)
    .replace(/\u3000/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/** 选区太短就不出浮条：空 / 纯空白 / 只有 1 个字符都不算「一段选片」。 */
export const MIN_SELECTION_CHARS = 2;

export function isUsableSelection(rawText) {
  return normalizeSelection(rawText).length >= MIN_SELECTION_CHARS;
}

/** 浮条/卡片头部的片段预览：按字符截断，不切断代理对（emoji）。 */
export function previewText(normalized, limit = 60) {
  const chars = Array.from(String(normalized == null ? '' : normalized));
  if (chars.length <= limit) return { text: chars.join(''), truncated: false };
  return { text: `${chars.slice(0, limit).join('')}…`, truncated: true };
}

/** 讲解缓存的键：sourceId|paragraph|kind|规范化后的 selection|画像指纹。
 *  画像指纹（第五段，可空：夹具模式 / 拿不到画像时为空串）由 api.js 的
 *  `profileSignalOf()` 算——会话内画像一变，键就变，旧答案不会被命中。 */
export function cacheKey(sourceId, paragraph, kind, selection, profileSignal = '') {
  return [
    sourceId || '', paragraph || '', kind || '', normalizeSelection(selection), profileSignal || '',
  ].join('|');
}

/** 从节点往上找最近的段落容器（主栏块或抽屉原文），返回归属与来源；不在可讲区里返回 null。 */
export function findSelectionScope(node) {
  let el = node && node.nodeType === 3 ? node.parentElement : node;
  while (el && el.nodeType === 1) {
    if (el.dataset && typeof el.dataset.paragraph === 'string' && el.dataset.paragraph) {
      if (el.dataset.role === 'src-para') {
        return { from: 'source', container: el, paragraphId: el.dataset.paragraph };
      }
      const block = el.closest ? el.closest('#rewritten .block[data-paragraph]') : null;
      if (block) {
        return { from: 'rewritten', container: block, paragraphId: block.dataset.paragraph };
      }
    }
    el = el.parentElement;
  }
  return null;
}

/**
 * 选区归属：按**锚点（起点）所在的那一段**挑段（跨段时亦然，见下面的归属注释）。
 *
 * @param {{toString:() => string, anchorNode?: Node}} selection 浏览器 Selection（测试里可以是同形对象）
 * @param {{root?: Document|Element, rects?: Array}} [options] `root` 便于测试注入；`rects` 覆盖几何度量
 * @returns {{from:string, paragraphId:string, rawText:string, normalized:string,
 *            chars:number, spans:number, preview:string, previewTruncated:boolean}|null}
 */
export function describeSelection(selection, options = {}) {
  if (!selection || typeof selection.toString !== 'function') return null;
  const root = options.root || (typeof document === 'undefined' ? null : document);
  if (!root) return null;

  const rawText = selection.toString();
  const normalized = normalizeSelection(rawText);
  if (!isUsableSelection(rawText)) return null;

  const scope = findSelectionScope(selection.anchorNode || null);
  if (!scope) return null;

  const bounds = SELECTION_SOURCES[scope.from];
  const host = root.querySelector ? root.querySelector(bounds.selector) : null;

  // 选区真正碰到过哪些段（跨段时用来算「跨了几段」）
  const touched = new Set();
  if (host && typeof selection.containsNode === 'function') {
    host.querySelectorAll('[data-paragraph]').forEach((node) => {
      try {
        if (selection.containsNode(node, true)) touched.add(node.dataset.paragraph);
      } catch (err) {
        /* 注释节点之类会抛，跳过即可——不影响归属 */
      }
    });
  }
  if (!touched.size) touched.add(scope.paragraphId);

  // 归属：只认锚点所在那一段（选区跨段时，只要选中文字里没有别的段的内容，
  // 选到相邻段空白也不该把卡片算到隔壁去）。
  const preview = previewText(normalized);
  return {
    from: scope.from,
    paragraphId: scope.paragraphId,
    rawText,
    normalized,
    chars: normalized.length,
    spans: touched.size,
    preview: preview.text,
    previewTruncated: preview.truncated,
  };
}

/** 选区的包围盒：固定定位的浮条要贴着它放。 */
export function selectionRect(selection) {
  if (!selection || typeof selection.getRangeAt !== 'function' || !selection.rangeCount) return null;
  let range;
  try {
    range = selection.getRangeAt(0);
  } catch (err) {
    return null;
  }
  const raw = Array.from(range.getClientRects());
  const list = raw.filter((r) => r && (r.width > 0 || r.height > 0));
  const usable = list.length ? list : [range.getBoundingClientRect()].filter((r) => r && (r.width > 0 || r.height > 0));
  if (!usable.length) return null;

  const first = usable[0];
  const last = usable[usable.length - 1];
  let top = Infinity;
  let bottom = -Infinity;
  usable.forEach((r) => {
    top = Math.min(top, r.top);
    bottom = Math.max(bottom, r.bottom);
  });
  return {
    top,
    bottom,
    left: first.left,
    right: last.right,
    centerX: (first.left + last.right) / 2,
  };
}

/**
 * 浮条的位置（视口坐标，配 position: fixed）。
 * 默认贴在选区下沿居中；下面放不下就翻到选区上沿；左右夹在视口内。
 * @returns {{left:number, top:number, arrow:number|null}}
 */
export function toolbarPosition(bounds, bar, view) {
  const margin = 8;
  const gap = 6;
  const width = bar.width || 0;
  const height = bar.height || 0;

  let left = Math.round(bounds.centerX - width / 2);
  left = Math.max(margin, Math.min(left, Math.max(margin, view.width - width - margin)));

  let top = Math.round(bounds.bottom + gap);
  if (top + height > view.height - margin) top = Math.round(bounds.top - gap - height);
  if (top < margin) top = margin;

  // 箭头指向选区；选不出合理位置就不画（别指着别处）
  let arrow = Math.round(bounds.centerX - left - 5);
  if (arrow < 8 || arrow > width - 18) arrow = null;
  return { left, top, arrow };
}
