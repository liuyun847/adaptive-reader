/**
 * 就地配图渲染（PLAN-actions-feedback.md §4.1–§4.3）。
 *
 * 图从哪来：`changes[i].figure = { kind: "mermaid"|"svg", source, caption? }`（内联在变更记录里，
 * 没有单独的 figures.json）。图**不进 `rewritten.md`**（illustrate 段正文与原文逐字节相同），
 * 由本模块在该段容器 `#blk-pNNN` 内、正文之后插入 `[data-role="figure"]` 容器。
 *
 * 三条硬约束：
 *
 * 1. **绕开 `md.js`**：`md.js` 的 `escapeHtml()` 会把 `<svg …>` 转成源码文本，所以图块自己造 DOM
 *    （SVG 走 `DOMParser` 解析 + 白名单清洗后 `importNode`，mermaid 走 `mermaid.render()` 返回的
 *    标记串）。正文照旧走 `md.js`，两条路互不干预。
 * 2. **mermaid 懒加载**：`vendor/mermaid.min.js` 有 5.3 MiB，**绝不能进首屏**——
 *    页面里没有它的 `<script>` 标签，只有当某个 mermaid 图**滚到视口附近**时才注入一次
 *    （注入上限＝每页 1 次，见 `loadMermaid` 的注释），加载完调 `mermaid.render()`。
 * 3. **失败退回文字**（DESIGN §6）：加载失败 / `render()` 抛错 / 后端已给 `figure_error` /
 *    源码不合白名单，一律在**原位置**写一句可读说明（「图渲染失败，退回文字说明：…」+ 图注或
 *    `why.note`），并把降级交给 `main.js` 的 `degrade()` 走横幅与 `__arDebug.degradations`。
 *    **不留空白、不留破图**。
 *
 * 图渲染**不产生任何模型请求**；可断言的状态落在容器的 `data-state`（pending/loading/ready/
 * fallback）与 `globalThis.__arDebug.figures` / `__arDebug.figureScript` 上。
 */

/** 懒加载的脚本地址（相对**文档**解析：页面在 `web/index.html`，所以落到 `web/vendor/`）。 */
export const MERMAID_SRC = 'vendor/mermaid.min.js';

export const SVG_NS = 'http://www.w3.org/2000/svg';
export const FIGURE_KINDS = ['mermaid', 'svg'];

/**
 * 与后端同口径的前端白名单（`PLAN-actions-feedback §4.2` 那四条）：
 * 禁 `<script>` / 一切 `on*` 属性 / `foreignObject` / 指向外部资源的 `href`。
 * 后端已经清洗过一遍，这里再做一遍是「不信远端数据」的第二道闸——
 * 命中就直接退回文字，不冒险插进页面。
 *
 * `on*` 那一条前面要求一个分隔符（空白 / 引号 / 括号 / `<`）：不然 `monkey=1` 这种标签里的
 * 「on…=」会被误判成事件属性，把一张好图退回文字。
 */
const FORBIDDEN = /<\s*script|<\s*foreignObject|[\s"'(<]on[a-z]+\s*=|javascript:/i;

/** 本次页面里最多注入一次脚本：装的是同一个脚本，注入两次没有意义。 */
let mermaidLoader = null;

let uidSeq = 0;
let recordSeq = 0;

/** 待渲染的 mermaid 图：容器元素 → 任务（`WeakMap` 免得图从 DOM 移除后还留着引用）。 */
const pendingMermaid = new WeakMap();

/**
 * 还没挂上观察器的容器（`buildFigure()` 造容器时**它还没进 DOM**，那时候量不出位置）。
 *
 * 这条是实测撞出来的：在 `renderDoc()` 的循环里就地 `observe()`，容器当时还没被 append，
 * 观察器的首次回调会把它算成「已进入视口」——于是页面一打开 5.3 MiB 的绘图库就下了，
 * 懒加载形同虚设。所以由调用方在**整篇渲染完之后**调一次 `armFigures()` 再观察。
 */
const pendingContainers = [];

/** 共用一个观察器：图滚到视口附近才真去画（`rootMargin` 让它提前一点开始）。 */
let observer = null;

function debugHandle() {
  return globalThis.__arDebug || null;
}

/**
 * 图表库需要的能力探测。
 *
 * mermaid 12 面向现代浏览器构建，用到 `structuredClone`（Chrome 98+）与 `Object.hasOwn`（Chrome 93+）。
 * 老内核上它的产物连语法都解析不过（SyntaxError）——白下 5.3 MiB 再报错没有意义，
 * 所以先探测、不支持就直接退回文字（与 deepseek-chat 的 `hydrate.js` 同一套判据）。
 */
export function figureSupportOk() {
  return typeof globalThis.structuredClone === 'function'
    && typeof Object.hasOwn === 'function';
}

/** 脚本注入的可断言快照（无头验证断言「只注入一次」看它，不改页面行为）。 */
function publishLoaderState(entry) {
  const debug = debugHandle();
  if (!debug) return;
  debug.figureScript = {
    src: MERMAID_SRC,
    injected: entry ? entry.injected : 0,
    state: entry ? entry.state : 'idle',
    error: entry ? entry.error : null,
  };
}

/**
 * 懒加载并初始化 mermaid（多次调用共享同一个 Promise，**脚本最多注入一次**）。
 *
 * 失败时**不重置** Promise：同一页里再遇到第二个 mermaid 图，直接复用同一个失败结论退回文字，
 * 而不是反复去下载 5 MiB（「只注入一次」这条口径按最坏情况也成立）。
 */
export function loadMermaid() {
  if (mermaidLoader) return mermaidLoader.promise;
  const entry = { injected: 0, state: 'idle', error: null, promise: null };
  mermaidLoader = entry;
  publishLoaderState(entry);

  entry.promise = new Promise((resolve, reject) => {
    if (globalThis.mermaid) {
      resolve(globalThis.mermaid);
      return;
    }
    if (!figureSupportOk()) {
      reject(new Error('当前浏览器缺少图表库需要的 API（structuredClone / Object.hasOwn），无法渲染 mermaid 图'));
      return;
    }
    entry.state = 'loading';
    const script = document.createElement('script');
    script.src = MERMAID_SRC;
    script.async = true;
    script.dataset.role = 'mermaid-loader';
    script.onload = () => {
      if (globalThis.mermaid) resolve(globalThis.mermaid);
      else reject(new Error(`图表库加载完成但没有暴露 mermaid（${MERMAID_SRC}）`));
    };
    script.onerror = () => reject(new Error(`图表库加载失败：${MERMAID_SRC} 取不到`));
    entry.injected += 1;
    document.head.appendChild(script);
  }).then((mermaid) => {
    mermaid.initialize({
      startOnLoad: false,
      // strict：标签里的 HTML 一律转义，杜绝在图里塞脚本
      securityLevel: 'strict',
      // 出错时**不要**往页面里画那张「Syntax error in text」的红图（那正是「破图」）
      suppressErrorRendering: true,
      theme: (document.body && document.body.dataset && document.body.dataset.bg) === 'dark'
        ? 'dark' : 'default',
      fontFamily: (typeof getComputedStyle === 'function' && document.body)
        ? getComputedStyle(document.body).fontFamily : undefined,
      flowchart: { htmlLabels: false },
      sequence: { htmlLabels: false },
      gantt: { htmlLabels: false },
      maxTextSize: 50000,
    });
    entry.state = 'ready';
    publishLoaderState(entry);
    return mermaid;
  }).catch((err) => {
    entry.state = 'failed';
    entry.error = String((err && err.message) || err);
    publishLoaderState(entry);
    throw err;
  });
  publishLoaderState(entry);
  return entry.promise;
}

/* --------------------------------------------------------------- SVG 源码 */

/** 与后端 `§4.2` 同口径的清洗：删 script / on* 属性 / foreignObject / 外部 href（`#` 内部锚点保留）。 */
export function cleanSvg(root) {
  root.querySelectorAll('script, foreignObject').forEach((node) => {
    if (node.parentNode) node.parentNode.removeChild(node);
  });
  const walk = [root, ...Array.from(root.querySelectorAll('*'))];
  walk.forEach((node) => {
    if (!node.attributes) return;
    Array.from(node.attributes).forEach((attr) => {
      const name = attr.name.toLowerCase();
      const value = String(attr.value || '').trim();
      if (name.startsWith('on')) {
        node.removeAttribute(attr.name);
        return;
      }
      if ((name === 'href' || name === 'xlink:href') && value && !value.startsWith('#')) {
        node.removeAttribute(attr.name);
      }
    });
  });
  return root;
}

/**
 * SVG 源码 → 可插入页面的真实节点。任何一步不合法都抛（由调用处退回文字）。
 *
 * 用 `image/svg+xml` 解析（顺带拿到 XML 合法性）；少了 `xmlns` 的源码在这个模式下会落到
 * 无命名空间，插进 HTML 里就是「看不见的破图」——所以补上命名空间再解析一次。
 */
export function svgElementOf(source, doc = document) {
  const text = String(source == null ? '' : source).trim();
  if (!text) throw new Error('图源码是空的');
  if (FORBIDDEN.test(text)) throw new Error('图源码里有不允许的内容（脚本 / 事件属性 / 外部引用）');

  const parsed = new DOMParser().parseFromString(text, 'image/svg+xml');
  if (parsed.querySelector('parsererror')) throw new Error('SVG 源码不是合法 XML');
  let root = parsed.documentElement;
  if (!root || root.localName !== 'svg') throw new Error('SVG 源码的根元素不是 <svg>');
  if (root.namespaceURI !== SVG_NS) {
    root.setAttribute('xmlns', SVG_NS);
    const again = new DOMParser().parseFromString(
      new XMLSerializer().serializeToString(root), 'image/svg+xml',
    );
    if (again.querySelector('parsererror')) throw new Error('SVG 源码不是合法 XML');
    root = again.documentElement;
    if (!root || root.localName !== 'svg' || root.namespaceURI !== SVG_NS) {
      throw new Error('SVG 源码缺少 xmlns，插进页面也画不出来');
    }
  }
  cleanSvg(root);
  root.removeAttribute('onload');
  return doc.importNode(root, true);
}

/* ------------------------------------------------------------ mermaid 源码 */

/** mermaid 源码的基本形状检查（与后端 §4.2 同口径的前端版；后端已校验，这里是第二道闸）。 */
export function assertMermaidSource(source) {
  const text = String(source == null ? '' : source);
  if (!text.trim()) throw new Error('图源码是空的');
  if (text.includes('```')) throw new Error('图源码里带了 ``` 围栏');
  if (FORBIDDEN.test(text)) throw new Error('图源码里有不允许的内容（脚本 / 事件属性 / 外部引用）');
  return text;
}

/* ------------------------------------------------------------- 图容器 DOM */

function setState(container, state) {
  container.dataset.state = state;
}

/** 图的可断言记录按 **figures.js 自己发的 id** 找（段号 + kind 在同段多图时不唯一）。 */
function recordOf(container) {
  const debug = debugHandle();
  if (!debug || !Array.isArray(debug.figures)) return null;
  const id = container.dataset.figureId;
  return debug.figures.find((item) => item.id === id) || null;
}

function markRecord(container, patch) {
  const record = recordOf(container);
  if (record) Object.assign(record, patch);
}

function hold(container) {
  return container.querySelector('[data-role="figure-hold"]');
}

/**
 * 造图容器并挂上渲染。容器在段落块内、正文之后（调用方负责 append）。
 *
 * @param {{paragraph:string, figure?:{kind:string,source:string,caption?:string},
 *          figure_error?:string, why?:{note?:string}, action?:string}} change 变更记录那一条
 * @param {{degrade?:(kind:string,message:string)=>void}} hooks `main.js` 的可见降级出口
 * @returns {HTMLElement|null} 图容器（没有图也没有 figure_error 时返回 null）
 */
export function buildFigure(change, { degrade } = {}) {
  if (!change || (!change.figure && !change.figure_error)) return null;
  const paragraphId = String(change.paragraph || '');
  const figure = change.figure || null;
  const kind = figure ? String(figure.kind || '') : null;
  const caption = figure && figure.caption ? String(figure.caption) : '';
  const whyNote = (change.why && change.why.note) ? String(change.why.note) : '';

  recordSeq += 1;
  const figureId = `fig${recordSeq}`;
  const container = document.createElement('div');
  container.className = 'figure';
  container.dataset.role = 'figure';
  container.dataset.figureId = figureId;
  container.dataset.paragraph = paragraphId;
  container.dataset.kind = kind || 'none';
  setState(container, 'pending');

  const canvas = document.createElement('div');
  canvas.className = 'figure__hold';
  canvas.dataset.role = 'figure-hold';
  container.appendChild(canvas);

  if (caption) {
    const figcaption = document.createElement('div');
    figcaption.className = 'figure__caption';
    figcaption.dataset.role = 'figure-caption';
    figcaption.textContent = caption;
    container.appendChild(figcaption);
  }

  const debug = debugHandle();
  const record = {
    id: figureId,
    paragraph: paragraphId, kind, caption, state: 'pending', reason: null,
    sourceChars: figure && typeof figure.source === 'string' ? figure.source.length : 0,
  };
  if (debug && Array.isArray(debug.figures)) debug.figures.push(record);

  const task = { figureId, paragraphId, figure, caption, whyNote, degrade, container };
  // 后端已经判定这张图没画成（`figure_error`）：正文照旧、动作记 keep，位置上一句话说明。
  if (change.figure_error) {
    fallback(container, task, new Error(String(change.figure_error)), 'figure_error');
    return container;
  }
  if (FIGURE_KINDS.indexOf(kind) === -1) {
    fallback(container, task, new Error(`不认识的图类型（${kind || '缺 kind'}）`), 'bad-kind');
    return container;
  }

  if (kind === 'svg') {
    try {
      canvas.appendChild(svgElementOf(figure.source));
      setState(container, 'ready');
      markRecord(container, { state: 'ready' });
    } catch (err) {
      fallback(container, task, err, 'svg');
    }
    return container;
  }

  // mermaid：先只放一块占位文字，等 `armFigures()`（整篇渲染完）之后**真滚进视口**再画
  canvas.textContent = '这里是配图位：滚到它时再加载绘图库…';
  pendingMermaid.set(container, task);
  pendingContainers.push(container);
  return container;
}

/**
 * 整篇渲染完之后调一次：把还等着画的 mermaid 图交给观察器。
 *
 * 为什么不在 `buildFigure()` 里就地观察：那时容器还没进 DOM（`renderDoc` 的循环是
 * 「造块 → append」），观察器的首次回调会把没有布局盒的它算成已进入视口 ⇒ 首屏就下载 5.3 MiB。
 *
 * @returns {number} 这次挂上观察器的图数量（0 表示这一页没有 mermaid 图）
 */
export function armFigures() {
  if (!pendingContainers.length) return 0;
  const list = pendingContainers.splice(0, pendingContainers.length);
  if (typeof IntersectionObserver !== 'function') {
    // 没有观察器就直接画（老内核）：宁可慢一点，也不能留一块永远不画的白块
    list.forEach((container) => startMermaid(container));
    return list.length;
  }
  list.forEach((container) => {
    if (pendingMermaid.has(container)) figureObserver().observe(container);
  });
  return list.length;
}

/**
 * 共用观察器：图**真的进视口**才开始渲染（只画一次，画完就取消观察）。
 *
 * `rootMargin` 取 0（不提前预取）：5.3 MiB 的绘图库只在读者真的滚到图那里时才下载，
 * 页面打开时不碰它——夹具里 `p013` 的图就在首屏下方 100px 处，留任何正余量都会在加载时命中它，
 * 「不进首屏」这条口径也就没法用测试钉住。
 */
function figureObserver() {
  if (!observer) {
    observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        observer.unobserve(entry.target);
        startMermaid(entry.target);
      });
    }, { rootMargin: '0px', threshold: 0 });
  }
  return observer;
}

/** mermaid 渲染：加载库 → `render()` → 把返回的 SVG 标记插进容器（绕开 md.js 的转义）。 */
async function startMermaid(container) {
  const task = pendingMermaid.get(container);
  if (!task) return;
  pendingMermaid.delete(container);
  const canvas = hold(container);
  setState(container, 'loading');
  markRecord(container, { state: 'loading' });
  if (canvas) canvas.textContent = '图表渲染中…';

  const id = `arfig${(uidSeq += 1)}`;
  try {
    const source = assertMermaidSource(task.figure && task.figure.source);
    const mermaid = await loadMermaid();
    const { svg } = await mermaid.render(id, source);
    // mermaid 偶尔「渲染成功」却返回空串：不显式判空就是静默空白
    if (!svg || !svg.trim()) throw new Error('图表库返回了空结果');
    if (canvas) canvas.innerHTML = svg;
    setState(container, 'ready');
    markRecord(container, { state: 'ready' });
  } catch (err) {
    fallback(container, task, err, 'mermaid');
  } finally {
    cleanupMermaidTemp(id);
  }
}

/** 收掉 mermaid 留在 body 上的临时节点（只认它自己的 `d<id>`，别碰我们自己插进容器的那个 svg）。 */
function cleanupMermaidTemp(id) {
  ['d' + id, 'i' + id].forEach((tempId) => {
    const node = document.getElementById(tempId);
    if (node && node.parentNode) node.parentNode.removeChild(node);
  });
}

/**
 * 退回文字说明（§4.3）：原位置一句话，读得懂、不空白、不破图；同时把降级交给 `degrade()`。
 * `where` 只进 `__arDebug.figures[].reason` 便于排查，不进页面上那句人话。
 */
function fallback(container, task, err, where) {
  const reason = String((err && err.message) || err || '未知原因');
  const canvas = hold(container);
  if (canvas) {
    canvas.textContent = '';
    const line = document.createElement('p');
    line.className = 'figure__fallback';
    line.dataset.role = 'figure-note';
    line.textContent = task.caption
      ? `图渲染失败，退回文字说明：${reason}`
      : `图渲染失败，退回文字说明：${reason}${task.whyNote ? `（原本要画的是：${task.whyNote}）` : ''}`;
    canvas.appendChild(line);
  }
  setState(container, 'fallback');
  markRecord(container, { state: 'fallback', reason, where });
  if (typeof task.degrade === 'function') {
    task.degrade('figure', `配图渲染失败（${task.paragraphId}）：${reason}。这一段只显示文字，没有图。`);
  }
}
