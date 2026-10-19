/**
 * 启动页装配（2026-10-15）：`web/index.html` —— 「启动页 + 阅读器页」拆页之后新增的那一页。
 *
 * 四块（顺序就是页面上的顺序，见 `web/index.html` 的 `data-block`）：
 *   ① **状态**：搬来阅读器顶栏那个面板（`js/status.js` 的 `mountStatus`，数据源＝同源静态文件
 *      `/status.json`）；这里只做接线，面板自己的行为一个字没改。
 *   ② **做测试**：读前背景问卷。卡片先显示现状（`GET /api/survey`：已答 / 已跳过 / 未答 / 读不到），
 *      点「填写 / 重填问卷」才打开问卷（`js/survey.js` 的 `openSurveyManually`）——
 *      **绝不自动弹**（2026-10-15 读者刚要求删掉自动打扰的横幅，这里不再加一个自动弹窗）。
 *   ③ **历史记录**：`GET /api/sources` 的材料清单；点一条进 `/reader?source=<id>`。
 *   ④ **输入文件**：`.md` / `.txt` → `POST /api/ingest`（只摄取，不调模型）。
 *
 * 纪律（与阅读器同一条，见 `web/README.md` 的「降级纪律」）：
 *   - **任何失败都要看得见**：接口不可用 / 结构不符 / 上传失败 / 问卷读不到，一律在这两处留字——
 *     块内那条 `data-state` 状态行（带原因）**和** `__arDebug.degradations`（配套的全局横幅）；
 *     全文件没有一处 `.catch(() => {})` 式的静默。
 *   - **就绪只靠显式状态**：`globalThis.__arDebug.ready` 在四块都到了确定状态（含各自的降级）之后
 *     才置位；无头验证只等它，不 sleep（与 `js/main.js` 同一口径）。
 *   - **零构建**：不引外部 CDN、不加打包器、不用框架；横幅复用 `js/view.js` 的 `addBanner`
 *     （同文本去重 / 可手动关 / 成功类自动消失这四条显示层纪律只有那一份实现，不另写第二份）。
 */

import { DataError, SURVEY_URL, fetchSources, fetchSurvey, postIngest } from './api.js';
import { bootstrapSettings } from './settings.js';
import { mountStatus } from './status.js';
import { openSurveyManually, setSurveyLifecycleHooks } from './survey.js';
import { addBanner } from './view.js';

const D = globalThis.__arDebug;
D.jsLoaded = true;

/**
 * 启动页恒按**正式模式**取数：`/api/sources`、`/api/ingest`、`/api/survey` 三件都是后端接口，
 * 没有夹具版本（阅读器的 `?fixture=` 是另一条路，走静态样例文件）。
 * 带 `?source=` / `?fixture=` 打开 `/` 时后端会 **302 到 `/reader`**，所以这一页根本不该出现那两个参数；
 * 真出现了也不改变这里的取数口径——页面照样能用，只是不认那两个参数。
 */
const API_CTX = { mode: 'api', sourceId: null, explicitSource: false, surveyAsked: false };

/** 上传的白名单（与后端 `ar.server.UPLOAD_EXTENSIONS` 同一个口径，比对前先转小写）。 */
const UPLOAD_EXTENSIONS = ['.md', '.txt'];

/** `source_id` 的合法形状：与后端 `ar.paths.SOURCE_ID_RE` 同一条正则 + 另拒 `..`（后端也是这么判的）。 */
const SOURCE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

/** 动作键 → 中文名（清单里那行「展开 10 / 保持 15」）。没列到的键照原样打出来，不静默丢掉。 */
const ACTION_LABELS = {
  expand: '展开', keep: '保持', compress: '压缩', illustrate: '配图',
};

/** 动作在清单里的固定顺序（与契约 §3 的 `action_counts` 同序）。 */
const ACTION_ORDER = ['expand', 'keep', 'compress', 'illustrate'];

const dom = {
  banners: document.querySelector('[data-role="banners"]'),
  statusToggle: document.querySelector('[data-role="status-toggle"]'),
  statusHost: document.querySelector('[data-role="status-host"]'),
  surveyState: document.querySelector('[data-role="launcher-survey-state"]'),
  surveyWhen: document.querySelector('[data-role="launcher-survey-when"]'),
  surveyOpen: document.querySelector('[data-role="launcher-survey-open"]'),
  surveyDegraded: document.querySelector('[data-role="launcher-survey-degraded"]'),
  historyState: document.querySelector('[data-role="launcher-history-state"]'),
  historyList: document.querySelector('[data-role="launcher-history-list"]'),
  historyDegraded: document.querySelector('[data-role="launcher-history-degraded"]'),
  uploadFile: document.querySelector('[data-role="upload-file"]'),
  uploadSourceId: document.querySelector('[data-role="upload-source-id"]'),
  uploadOverwrite: document.querySelector('[data-role="upload-overwrite"]'),
  uploadSubmit: document.querySelector('[data-role="upload-submit"]'),
  uploadStatus: document.querySelector('[data-role="upload-status"]'),
  uploadResult: document.querySelector('[data-role="upload-result"]'),
};

const state = {
  uploadPending: false,     // 一次只跑一个摄取请求（按钮在此期间禁用）
  surveyOpening: false,     // 问卷打开请求在飞（防连点开出两张表单）
};

/* ------------------------------------------------------------ 可见降级出口 */

/** `__arDebug` 的三个分组（与 index.html 里声明的那一份同名）：只合并补丁，不整体替换。 */
function setDebug(group, patch) {
  if (!D.launcher || !D.launcher[group]) return;
  Object.assign(D.launcher[group], patch);
}

/** 横幅的 `data-state`：只有「失败」染红、「做成了」染绿，其余（降级 / 说明）一律黄（同 main.js）。 */
function bannerState(kind) {
  if (kind === 'error' || kind === 'fatal') return 'error';
  if (kind === 'info' || kind === 'ok') return 'info';
  return 'warn';
}

/**
 * 记一条可见降级：`__arDebug.degradations` + 一条横幅。
 * 与阅读器的 `main.js` 同一套口径（两个出口都要有），**绝不静默**。
 */
function degrade(kind, message) {
  D.degradations.push({ kind, message });
  return addBanner(dom.banners, message, bannerState(kind));
}

/** 给问卷模块用的横幅出口（`survey.js` 提交成功 / 跳过等由它自己决定文案与颜色）。 */
function banner(text, kind) {
  return addBanner(dom.banners, text, bannerState(kind));
}

/** 把一串说明渲染成块内的可见列表（没有内容就整块 `hidden`，不留空壳）。 */
function renderNotes(container, messages) {
  if (!container) return;
  const rows = (Array.isArray(messages) ? messages : []).filter((m) => typeof m === 'string' && m.trim());
  container.textContent = '';
  rows.forEach((message) => {
    const item = document.createElement('li');
    item.className = 'lnotes__item';
    item.dataset.role = 'launcher-note';
    item.textContent = message;
    container.appendChild(item);
  });
  container.hidden = !rows.length;
}

/**
 * 把后端给的 ISO 时间戳（带时区偏移，如 `2026-10-13T12:27:42+08:00`）写成**本地可读格式**。
 *
 * 为什么按字符串拆而不是 `new Date()` + `toLocaleString()`：后端给的是**本地时区**的时间戳，
 * 再让浏览器按「本机时区」转一次会在跨时区访问时把同一个瞬间显示成另一个钟点，
 * 读者对着 `data/` 目录看文件时间就对不上了。所以原样保留年月日时分秒与偏移
 * （`2026-10-13 12:27:42 +08:00`），只把 `T` 换成空格；认不出来的形状**原样显示**（不猜、不丢）。
 */
export function formatLocalStamp(raw) {
  if (typeof raw !== 'string' || !raw.trim()) return '';
  const text = raw.trim();
  const matched = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$/.exec(text);
  if (!matched) return text;
  const [, year, month, day, hour, minute, second, offset] = matched;
  return `${year}-${month}-${day} ${hour}:${minute}:${second} ${offset === 'Z' ? '+00:00' : offset}`;
}

/** 材料在阅读器里的地址（后端 `GET /reader` 就是阅读器页，参数原样透传）。 */
export function readerHref(sourceId) {
  return `/reader?source=${encodeURIComponent(String(sourceId))}`;
}

/* ------------------------------------------------------------ ③ 历史记录 */

/**
 * 动作统计那行：`展开 10 / 保持 15`；读不出来就说读不出来（不写 0 冒充）。
 *
 * **只列非 0 的动作**：真实产物里 `action_counts` 四个键都在（`{expand:10, compress:0, keep:15,
 * illustrate:0}`），把「压缩 0 / 配图 0」也打出来只会把两个关键数字淹掉——0 就是「这一段动作没发生」，
 * 不写出来不丢信息。全是 0（空材料）时明写「各动作都是 0」。
 */
function actionsText(rewrite) {
  if (!rewrite || typeof rewrite !== 'object') return '动作统计未提供';
  const counts = rewrite.action_counts;
  if (!counts || typeof counts !== 'object' || Array.isArray(counts)) return '动作统计未提供';
  const parts = [];
  ACTION_ORDER.forEach((key) => {
    if (typeof counts[key] === 'number' && counts[key] > 0) {
      parts.push(`${ACTION_LABELS[key]} ${counts[key]}`);
    }
  });
  // 后端将来加了新动作：照原键名打出来，别让它悄悄消失（清单是给人看的）
  Object.keys(counts).forEach((key) => {
    if (ACTION_ORDER.includes(key) || typeof counts[key] !== 'number' || counts[key] <= 0) return;
    parts.push(`${key} ${counts[key]}`);
  });
  return parts.length ? parts.join(' / ') : '动作统计为空（各动作都是 0）';
}

/** 校验状态：已校验通过 / 有 N 处不一致 / 未校验（外加「读不出来」这一档，不冒充通过）。 */
function fidelityOf(entry) {
  const block = entry && entry.fidelity;
  if (!block || typeof block !== 'object') {
    return { text: '未校验（还没有 changes.json）', state: 'unknown' };
  }
  if (block.checked === true) {
    const count = typeof block.mismatch_count === 'number' ? block.mismatch_count : null;
    if (count === null) return { text: '已校验（不一致条数读不出来）', state: 'warn' };
    if (count === 0) return { text: '已校验通过', state: 'ok' };
    return { text: `有 ${count} 处不一致`, state: 'error' };
  }
  if (block.checked === false) return { text: '未校验（还没跑过 ar check）', state: 'unknown' };
  return { text: '未校验（校验状态读不出来）', state: 'warn' };
}

/** 缺哪份产物：三块各自独立判定，全缺就是「只摄取」那一档。 */
function missingText(entry) {
  const missing = [];
  if (!entry.concepts || typeof entry.concepts !== 'object') missing.push('概念清单未抽取');
  if (!entry.rewrite || typeof entry.rewrite !== 'object') missing.push('逐段改写还没跑');
  if (!entry.fidelity || typeof entry.fidelity !== 'object') missing.push('事实锚还没校验');
  if (!missing.length) return '三份产物齐（概念清单 / 逐段改写 / 事实锚校验）';
  if (missing.length === 3) return '只摄取：概念清单、逐段改写、事实锚校验都还没跑';
  return missing.join('、');
}

/** 一条材料（整块是链接：点哪都进阅读器，键盘也能 Tab 到、回车即进）。 */
function historyItem(entry) {
  const sourceId = String(entry.source_id);
  const paragraphs = typeof entry.paragraphs === 'number' ? `${entry.paragraphs} 段` : '段数未知';
  const mtime = formatLocalStamp(entry.mtime) || '时间未知';
  const fidelity = fidelityOf(entry);

  const item = document.createElement('li');
  item.className = 'hist__item';
  item.dataset.role = 'history-item';
  item.dataset.sourceId = sourceId;

  const link = document.createElement('a');
  link.className = 'hist__link';
  link.dataset.role = 'history-link';
  link.href = readerHref(sourceId);
  link.title = `打开 ${sourceId} 的阅读器页`;

  const head = document.createElement('div');
  head.className = 'hist__head';
  const id = document.createElement('span');
  id.className = 'hist__id';
  id.dataset.role = 'history-id';
  id.textContent = sourceId;
  head.appendChild(id);

  const badge = document.createElement('span');
  badge.className = 'hist__badge';
  badge.dataset.role = 'history-fidelity';
  badge.dataset.state = fidelity.state;
  badge.textContent = fidelity.text;
  head.appendChild(badge);
  link.appendChild(head);

  const meta = document.createElement('div');
  meta.className = 'hist__meta';
  [['history-paragraphs', paragraphs], ['history-mtime', mtime], ['history-actions', actionsText(entry.rewrite)]]
    .forEach(([role, text]) => {
      const span = document.createElement('span');
      span.className = 'hist__meta-item';
      span.dataset.role = role;
      span.textContent = text;
      meta.appendChild(span);
    });
  link.appendChild(meta);

  const note = document.createElement('div');
  note.className = 'hist__note';
  note.dataset.role = 'history-missing';
  note.textContent = missingText(entry);
  link.appendChild(note);

  item.appendChild(link);
  return item;
}

/** 清单里的说明（后端 `degraded[]`：缺哪份产物、哪条材料被跳过）——块内可见列表。 */
function historyNotes(payload, entries) {
  const notes = Array.isArray(payload && payload.degraded)
    ? payload.degraded.filter((m) => typeof m === 'string' && m.trim())
    : [];
  // 结构不符的条目不许被静默跳过：逐条点名
  (Array.isArray(entries) ? entries : []).forEach((entry, index) => {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)
      || typeof entry.source_id !== 'string' || !entry.source_id.trim()) {
      notes.push(`清单第 ${index + 1} 条没有可用的 source_id（结构不符），已跳过`);
    }
  });
  return notes;
}

function renderSources(payload, url) {
  const entries = Array.isArray(payload.sources) ? payload.sources : [];
  const rows = entries.filter((entry) => entry && typeof entry === 'object' && !Array.isArray(entry)
    && typeof entry.source_id === 'string' && entry.source_id.trim());

  dom.historyList.textContent = '';
  rows.forEach((entry) => dom.historyList.appendChild(historyItem(entry)));

  const notes = historyNotes(payload, entries);
  renderNotes(dom.historyDegraded, notes);
  const structural = notes.filter((m) => m.includes('结构不符'));

  if (!rows.length) {
    dom.historyState.dataset.state = 'empty';
    dom.historyState.textContent = 'data/ 下还没有材料，先在下面上传一个。';
    setDebug('sources', {
      state: 'empty', url, count: 0, error: null, degraded: notes,
    });
  } else {
    dom.historyState.dataset.state = 'ok';
    dom.historyState.textContent = `共 ${rows.length} 份材料（按最后动过的时间倒序，新的在前）：`;
    setDebug('sources', {
      state: 'ok', url, count: rows.length, error: null, degraded: notes,
    });
  }
  structural.forEach((message) => degrade('sources-shape', `材料清单：${message}`));
  if (!rows.length && payload.degraded && payload.degraded.length) {
    // 「空列表」也可能带着原因（data 目录不存在这类）：那是读者要看见的信息
    degrade('sources-degraded', `材料清单：${notes.join('；')}`);
  }
}

/**
 * 取一次材料清单并渲染。
 *
 * 三条失败路径都走同一条可见出口（块内状态行 + 横幅 + `__arDebug`）：
 * 网络 / HTTP 失败、响应不是 JSON、**结构不符**（`sources` 不是数组）——
 * 页面不崩、不空白、也不静默跳过读不懂的条目。
 */
async function refreshSources() {
  dom.historyState.dataset.state = 'loading';
  dom.historyState.textContent = '正在读取材料清单（GET /api/sources）…';
  setDebug('sources', { state: 'loading', error: null });
  try {
    const { payload, url } = await fetchSources();
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      // `url` 一定要带上：`launcher.sources.url` 的口径是「**实际打到**的地址」，
      // 结构不符也是打到了这个地址才发现的（2026-10-15 复核：这两条原来留在 null 上）
      throw new DataError('材料清单的结构不认识：响应不是一个 JSON 对象。', { kind: 'shape', url });
    }
    if (!Array.isArray(payload.sources)) {
      throw new DataError(
        `材料清单的结构不认识：sources 不是数组（拿到的是 ${payload.sources === null ? 'null' : typeof payload.sources}）。`,
        { kind: 'shape', url },
      );
    }
    renderSources(payload, url);
  } catch (err) {
    const message = (err && err.message) || String(err);
    dom.historyList.textContent = '';
    renderNotes(dom.historyDegraded, []);
    dom.historyState.dataset.state = 'degraded';
    dom.historyState.textContent = `材料清单没能读到：${message}`;
    setDebug('sources', {
      state: 'degraded', error: message, count: null,
      url: (err && err.url) || null, degraded: [],
    });
    degrade('sources-load', `读不到材料清单（GET /api/sources 失败：${message}）：历史记录这一块空着，上传不受影响。`);
  }
}

/* ------------------------------------------------------------ ④ 输入文件 */

function extensionOf(name) {
  const text = String(name || '');
  const dot = text.lastIndexOf('.');
  return dot === -1 ? '' : text.slice(dot).toLowerCase();
}

/**
 * 提交前的**粗判**（真正的权威是后端：这里只是别把明显的错发出去）。
 *
 * 「粗判」到什么程度，如实说清（2026-10-15 复核后改的注释口径——原来写「判据与后端逐条对齐」，
 * 那是假话）：
 *   - **前端只做后端也做的两条**：扩展名白名单（`.md` / `.txt`，比对前转小写）、
 *     `source_id` 正则 + 拒 `..`（与 `ar.paths.SOURCE_ID_RE` 同一条），再加「得先选一个文件」；
 *   - **不做后端的正文字符上限**（`ar.server.MAX_UPLOAD_CHARS = 2_000_000` 字符）：
 *     字符数只有读完文件才知道，而 `File.size` 是**字节数**（UTF-8 下一个汉字 3 字节），
 *     拿字节数去比字符上限会把合法文件误挡（2.1 MB 的中文只有约 70 万字符）。
 *     超过上限的由后端 400 兜底，页面上照样看得见（`renderUploadFailure`）；
 *   - **扩展名判据也不完全等价**：文件名恰好叫 `.md` 时 `extensionOf()` 认它是 `.md`（前端放行），
 *     后端按「没有扩展名」回 400；这一档同样靠后端兜底。
 *   - **空内容的判定照后端**：`content === ''` 才算空；全是空白的文本后端收，前端不擅自加严。
 */
function uploadPrecheck() {
  const file = dom.uploadFile && dom.uploadFile.files && dom.uploadFile.files[0]
    ? dom.uploadFile.files[0]
    : null;
  if (!file) {
    return { error: '还没有选文件：先选一个 .md / .txt（UTF-8 文本）。' };
  }
  const suffix = extensionOf(file.name);
  if (!UPLOAD_EXTENSIONS.includes(suffix)) {
    return {
      error: `这个文件的扩展名不在白名单里（${file.name || '未命名'}）：只收 ${UPLOAD_EXTENSIONS.join(' / ')}，大小写不敏感。`,
    };
  }
  const raw = String((dom.uploadSourceId && dom.uploadSourceId.value) || '').trim();
  if (raw && (!SOURCE_ID_RE.test(raw) || raw.includes('..'))) {
    return {
      error: `source_id 不合法（${raw}）：只能用字母 / 数字开头、由字母数字与 . _ - 组成、长度 1–64，且不能含 ".."。`,
    };
  }
  return { file, sourceId: raw, overwrite: Boolean(dom.uploadOverwrite && dom.uploadOverwrite.checked) };
}

/**
 * 非 UTF-8 的嗅探（纯函数）：`file.text()` 按 UTF-8 解码，坏字节只会变成 U+FFFD、**永不 reject**，
 * 所以「不是 UTF-8」这件事只能从解出来的正文里认。
 *
 * 判据两条，命中一条就算（都取「出现即算」这一档，不做比例阈值——一份好文件里混进一个坏字节
 * 同样会显示成乱码，读者有权知道）：
 *   - 正文里有替换字符 `U+FFFD`（坏字节被解码器替换掉的痕迹）；
 *   - 正文里有 NUL 字节（`\u0000`）：文本文件不会有它，基本就是二进制。
 *
 * 返回 `{encoding, note}`：`encoding ∈ 'utf-8' | 'utf-8-lossy'`，`note` 为 null 表示没问题。
 * 命中时**不拦请求**（后端才是权威），只把这件事写进状态行 / 结果块 / 横幅 / `degradations`。
 */
export function encodingNoteOf(content) {
  const text = String(content == null ? '' : content);
  const replaced = (text.match(/\uFFFD/g) || []).length;
  const hasNul = text.includes('\u0000');
  if (!replaced && !hasNul) return { encoding: 'utf-8', note: null };
  const why = hasNul
    ? '正文里有 NUL 字节，像是二进制文件'
    : `有 ${replaced} 处替换字符（U+FFFD）`;
  return {
    encoding: 'utf-8-lossy',
    note: `这份文件不是 UTF-8 文本（${why}）：已按替换字符摄取，正文可能乱码。`,
  };
}

/** 上一轮摄取留下的结果字段：新一次提交（含各种 invalid 档）开始时**一律清成 null**。 */
const UPLOAD_OUTCOME_FIELDS = [
  'sourceId', 'paragraphs', 'overwritten', 'error', 'hint', 'httpStatus', 'url',
  'encoding', 'encodingNote',
];

function clearedUploadOutcome() {
  const patch = {};
  UPLOAD_OUTCOME_FIELDS.forEach((field) => { patch[field] = null; });
  return patch;
}

/**
 * 状态行的唯一出口（`data-state` 是可断言的显式状态）。
 *
 * **进任何一档都先把上一轮的结果字段清干净**（`patch` 里的键除外）：状态行写着「invalid」时
 * `launcher.upload` 里就不该还留着上一轮的 `httpStatus: 200` / `paragraphs: 3`——
 * 那是调试面在说假话（2026-10-15 复核实测：空文件那一档 `error` 还是 null、旧值全留着）。
 * invalid 档的原因由调用方放进 `patch.error`，所以「状态行有字、调试面 error 有原因」两处一致。
 */
function setUploadStatus(stateName, text, patch) {
  dom.uploadStatus.dataset.state = stateName;
  dom.uploadStatus.textContent = text;
  setDebug('upload', { ...clearedUploadOutcome(), state: stateName, ...(patch || {}) });
}

/** 选完文件 / 改完 source_id 之后：按钮可用性与状态行跟着走（未选文件时按钮就是禁用）。 */
function syncUploadControls() {
  if (state.uploadPending) return;
  const check = uploadPrecheck();
  dom.uploadSubmit.disabled = Boolean(check.error);
  if (check.error) setUploadStatus('invalid', check.error, { error: check.error });
  else setUploadStatus('ready', `已选：${check.file.name}（${check.file.size} 字节），可以摄取。`);
}

/** 上传成功：写状态行 + 结果块（source_id / 段数 / 是否覆盖 + 「去阅读」）。 */
function renderUploadResult(payload, url, encoding) {
  const sourceId = typeof payload.source_id === 'string' ? payload.source_id : '';
  const paragraphs = typeof payload.paragraphs === 'number' ? payload.paragraphs : null;
  const overwritten = payload.overwritten === true;
  const notes = Array.isArray(payload.degraded)
    ? payload.degraded.filter((m) => typeof m === 'string' && m.trim())
    : [];
  const anchors = payload.anchors_stable;
  const paragraphText = paragraphs === null ? '段数未知' : `${paragraphs} 段`;
  const encodingKind = encoding && encoding.encoding ? encoding.encoding : 'utf-8';
  const encodingNote = encoding && encoding.note ? encoding.note : null;

  setUploadStatus('ok', `已摄取：${sourceId || '（后端没回 source_id）'}（${paragraphText}，`
    + `${overwritten ? '覆盖了原有材料' : '新建'}）。`
    + (anchors === false ? '注意：段锚点与上一次摄取不一致（覆盖后既有产物可能对不上，需要重跑 analyze / rewrite / check）。' : '')
    + (notes.length ? `后端说明：${notes.join('；')}` : '')
    + (encodingNote ? ` ${encodingNote}` : ''));

  dom.uploadResult.textContent = '';
  dom.uploadResult.hidden = false;
  const rows = [
    ['source_id', sourceId || '（后端没给）'],
    ['段数', paragraphText],
    ['是否覆盖', overwritten ? '覆盖了原有材料' : '新建（此前没有同名材料）'],
    ['摄取时间', formatLocalStamp(payload.ingested_at) || '（后端没给）'],
  ];
  rows.forEach(([label, value]) => {
    const row = document.createElement('div');
    row.className = 'lresult__row';
    row.dataset.role = 'upload-result-row';
    const name = document.createElement('span');
    name.className = 'lresult__label';
    name.textContent = label;
    const text = document.createElement('span');
    text.className = 'lresult__value';
    text.dataset.role = `upload-result-${label}`;
    text.textContent = value;
    row.appendChild(name);
    row.appendChild(text);
    dom.uploadResult.appendChild(row);
  });
  if (sourceId) {
    const go = document.createElement('a');
    go.className = 'lcard__btn';
    go.dataset.role = 'upload-reader-link';
    go.href = readerHref(sourceId);
    go.textContent = '去阅读';
    dom.uploadResult.appendChild(go);
  }
  if (notes.length) {
    const note = document.createElement('div');
    note.className = 'lresult__note';
    note.dataset.role = 'upload-result-note';
    note.textContent = `后端说明：${notes.join('；')}`;
    dom.uploadResult.appendChild(note);
  }
  if (encodingNote) {
    // 「这份文件不是 UTF-8」这一条是**读者必须看见**的说明（摄取本身成功了，所以不是失败态）
    const note = document.createElement('div');
    note.className = 'lresult__note';
    note.dataset.role = 'upload-encoding-note';
    note.textContent = encodingNote;
    dom.uploadResult.appendChild(note);
  }
  setDebug('upload', {
    state: 'ok', sourceId: sourceId || null, paragraphs, overwritten,
    error: null, hint: null, httpStatus: 200, url: url || null,
    encoding: encodingKind, encodingNote,
  });
}

/** 上传失败：把后端那句话（`error`）与出路（`hint`）原样显示出来，另打一条可见横幅。 */
function renderUploadFailure(err) {
  const httpStatus = typeof (err && err.status) === 'number' ? err.status : null;
  const message = (err && err.message) || String(err);
  const hint = err && err.detail && typeof err.detail.hint === 'string' ? err.detail.hint : null;
  const label = httpStatus ? `HTTP ${httpStatus}` : '请求失败';
  dom.uploadResult.textContent = '';
  dom.uploadResult.hidden = true;
  setUploadStatus('error', `摄取失败（${label}）：${message}${hint ? ` —— ${hint}` : ''}`);
  degrade('upload-error', `上传没能摄取（${label}）：${message}`);
  setDebug('upload', {
    state: 'error', sourceId: null, paragraphs: null, overwritten: null,
    error: message, hint, httpStatus, url: (err && err.url) || null,
  });
}

/**
 * 提交一次摄取：读文件（`file.text()`，UTF-8）→ `POST /api/ingest`。
 *
 * 四条纪律：
 *   - **先占位再读文件**：`file.text()` 也是异步的，占位放在它后面的话，同一轮事件循环里的连点会
 *     各读一份、各发一个请求（2026-10-15 复核实测：连点 3 次发 3 个 POST，第 2/3 个撞后端 409，
 *     页面最后红字写「摄取失败（HTTP 409）…已经存在」——而第一次其实已经写盘成功了）。
 *     所以过了粗判就**同步**置位 `state.uploadPending` + 禁用按钮，`finally` 里复位。
 *   - **在飞时再点一个请求都不发**：靠 `uploadPending` 这道同步闸（不是发出去再靠后端 409 兜）。
 *   - **非 UTF-8 也要说出来**（`encodingNoteOf`）：后端是权威、照常摄取，但状态行 / 结果块 / 横幅 /
 *     `degradations` 四处都要留字，不让「坏字节被替换成 U+FFFD」悄悄过去。
 *   - 成功后**刷新历史列表**（新材料的段数与时间由后端算，前端不自己拼一条）；
 *     失败一律可见（见 `renderUploadFailure`）。
 */
async function runUpload() {
  if (state.uploadPending) return null;
  const check = uploadPrecheck();
  if (check.error) {
    setUploadStatus('invalid', check.error, { error: check.error });
    return null;
  }

  state.uploadPending = true;
  dom.uploadSubmit.disabled = true;
  try {
    let content = '';
    try {
      content = await check.file.text();
    } catch (err) {
      const message = (err && err.message) || String(err);
      setUploadStatus('error', `读不出这个文件的内容（${message}）：换一份 UTF-8 文本再试。`,
        { error: message });
      degrade('upload-read', `读不出上传文件的内容（${check.file.name}）：${message}`);
      return null;
    }
    if (content === '') {
      const message = `文件是空的（0 个字符）：${check.file.name} 里没有可摄取的内容。`;
      setUploadStatus('invalid', message, { error: message });
      return null;
    }

    const encoding = encodingNoteOf(content);
    setUploadStatus('uploading', `正在摄取 ${check.file.name}（${content.length} 个字符）…`);
    const body = { filename: check.file.name, content, overwrite: check.overwrite };
    if (check.sourceId) body.source_id = check.sourceId;   // 空串不传：后端按「从 filename 派生」办
    const { payload, url } = await postIngest(body);
    renderUploadResult(payload || {}, url, encoding);
    if (encoding.note) {
      degrade('upload-encoding',
        `上传的 ${check.file.name} 不是 UTF-8 文本：${encoding.note}（已照常摄取，后端是权威。）`);
    }
    await refreshSources();      // 新材料要立刻出现在③里（历史列表自己的失败也可见）
  } catch (err) {
    renderUploadFailure(err);
  } finally {
    state.uploadPending = false;
    dom.uploadSubmit.disabled = false;
  }
  return D.launcher.upload;
}

/* ------------------------------------------------------------ ② 做测试（问卷） */

/** 后端若给了作答时间就显示它（`answered_at` 在顶层，或藏在 `answers` 里，两种都认）。 */
function answeredAtOf(payload) {
  if (!payload || typeof payload !== 'object') return '';
  const direct = formatLocalStamp(payload.answered_at);
  if (direct) return direct;
  const nested = payload.answers && typeof payload.answers === 'object' ? payload.answers.answered_at : null;
  return formatLocalStamp(nested);
}

/** 版本提示（与 `js/survey.js` 的 `versionNote` 同一判据）：回答版本与当前版本不一致时说出来。 */
function versionNoteOf(payload) {
  const answered = payload ? payload.answered_version : null;
  if (answered === null || answered === undefined || answered === '') return '';
  const current = Number(payload && payload.version);
  const previous = Number(answered);
  if (!Number.isFinite(current) || !Number.isFinite(previous) || previous === current) return '';
  return `问卷已更新：你上次作答时是 v${previous}，当前是 v${current}（可以重填一份覆盖它）。`;
}

function renderSurveyStatus(payload, url) {
  const answers = payload && typeof payload.answers === 'object' && payload.answers && !Array.isArray(payload.answers)
    ? payload.answers
    : null;
  const answered = Boolean(answers && Object.keys(answers).length);
  const skipped = Boolean(payload && payload.skipped);
  const at = answeredAtOf(payload);
  const notes = Array.isArray(payload && payload.degraded)
    ? payload.degraded.filter((m) => typeof m === 'string' && m.trim())
    : [];
  const versionNote = versionNoteOf(payload);
  if (versionNote) notes.push(versionNote);

  let stateName = 'unanswered';
  let text = '还没答过这六道题（改写按通用读者处理）。';
  if (answered) {
    stateName = 'answered';
    text = at
      ? `已答（${at}）。`
      : '已答（后端没给作答时间：GET /api/survey 只回 answers / skipped / answered_version）。';
  } else if (skipped) {
    stateName = 'skipped';
    text = '已跳过（按通用读者处理：改写不会按你的背景调整）。';
  }

  dom.surveyState.dataset.state = stateName;
  dom.surveyState.textContent = text;
  dom.surveyWhen.textContent = '';
  dom.surveyOpen.disabled = false;
  dom.surveyOpen.textContent = answered || skipped ? '重填问卷' : '填写问卷';
  renderNotes(dom.surveyDegraded, notes);
  setDebug('survey', {
    state: stateName, answered, skipped, answeredAt: at || null, error: null,
    url: url || null, version: payload ? payload.version : null,
    answeredVersion: payload ? payload.answered_version : null,
    degraded: notes,
  });
}

/**
 * 取一次问卷现状（`GET /api/survey`）。
 *
 * 读不到时**卡片上写原因**（`data-state="degraded"` + `__arDebug.launcher.survey.error`），
 * 「填写问卷」按钮照旧可点——读者想再试一次不该被拦住。
 * 后端的 `degraded[]`（如画像文件读不出来）与版本提示渲染成卡片里的可见列表：
 * 它们是说明不是失败，所以**不另打横幅**（横幅留给真正的失败，免得变成噪音）。
 */
async function refreshSurveyStatus() {
  dom.surveyState.dataset.state = 'loading';
  dom.surveyState.textContent = '正在读取问卷状态…';
  setDebug('survey', { state: 'loading', error: null });
  try {
    const { payload, url } = await fetchSurvey(API_CTX);
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      // 同 sources：结构不符也是打到了这个地址才发现的，`url` 一起带上
      throw new DataError('问卷状态的结构不认识：响应不是一个 JSON 对象。', { kind: 'shape', url });
    }
    renderSurveyStatus(payload, url);
  } catch (err) {
    const message = (err && err.message) || String(err);
    dom.surveyState.dataset.state = 'degraded';
    dom.surveyState.textContent = `问卷状态读不到：${message}`;
    dom.surveyWhen.textContent = '';
    dom.surveyOpen.disabled = false;
    renderNotes(dom.surveyDegraded, []);
    setDebug('survey', {
      state: 'degraded', answered: false, skipped: false, answeredAt: null,
      error: message, url: (err && err.url) || null, degraded: [],
    });
    degrade('survey-status', `读不到问卷状态（GET ${SURVEY_URL} 失败：${message}）：卡片上写了原因，`
      + '点「填写问卷」仍可以再试一次。');
  }
}

/** 点「填写 / 重填问卷」：显式打开同一张问卷（**绝不自动弹**，见文件头）。 */
async function openSurveyFromCard() {
  if (state.surveyOpening) return null;
  state.surveyOpening = true;
  dom.surveyOpen.disabled = true;
  try {
    const result = await openSurveyManually({ ctx: API_CTX, banner, degrade });
    setDebug('survey', {
      openReason: result.reason,
      lastOpen: { opened: Boolean(result.opened), reason: result.reason, at: Date.now() },
    });
    if (result.opened) {
      // 问卷开着的时候按钮保持禁用（关掉时由 closed 钩子放开）：一次只开一张表单
      dom.surveyOpen.disabled = true;
    }
    return result;
  } finally {
    state.surveyOpening = false;
    if (!D.surveyOpen) dom.surveyOpen.disabled = false;
  }
}

/* ------------------------------------------------------------ 接线与启动 */

function wireStatus() {
  // ① 状态：面板自己那一套（打开 / 关闭 / Esc / 滚轮归属 / 降级文案）全在 js/status.js 里，
  //    这里只把入口与宿主接上——与阅读器改造前的那一次接线逐字同源。
  mountStatus({ host: dom.statusHost, toggle: dom.statusToggle });
}

function wireSurvey() {
  dom.surveyOpen.addEventListener('click', () => { openSurveyFromCard(); });
  // 答完（提交成功）/ 关掉问卷之后都要刷新卡片状态：这是「答完/关闭后刷新成已答」的落点，
  // 由 survey.js 显式回调，不靠轮询、不靠 sleep。
  setSurveyLifecycleHooks({
    submitted: () => { refreshSurveyStatus(); },
    closed: () => {
      dom.surveyOpen.disabled = false;
      refreshSurveyStatus();
    },
  });
  // 启动页**不自动弹**问卷：这一档原因也留在可断言的调试面上（阅读器那边是自动判定的那条路）
  D.surveyBootReason = 'launcher-no-auto-popup';
}

function wireUpload() {
  dom.uploadFile.addEventListener('change', syncUploadControls);
  dom.uploadSourceId.addEventListener('input', syncUploadControls);
  dom.uploadSubmit.addEventListener('click', () => { runUpload(); });
}

/**
 * 启动：先挂状态块与两块交互（同步、不可能失败），再并发取两份数据，
 * 两份都到确定状态之后才置 `ready`（无头验证只等它）。
 *
 * **第一件事是吃显示设置**（`bootstrapSettings(document.body)`，与阅读器页 boot 里同一句）：
 * 两页共用 `css/reader.css` 的主题，而那段主题覆盖的选择器是 `body[data-bg|data-ink|data-size]`。
 * 启动页原先只由 `<head>` 那段脚本给 `<html>` 打属性（选择器不认 `<html>`），于是它永远停在标记里
 * 写死的纸白 16，同一份存档下两页两套底色（2026-10-15 复核实测：启动页 `rgb(251, 250, 247)` /
 * 阅读器页 `rgb(27, 31, 35)`）。现在 `<head>` 那段算完结论后由 `<body>` 开头那句打到 body 上
 * （渲染前不闪白底那条仍然成立），这里再调一次 `bootstrapSettings` 接管并**补漏**。
 *
 * 两份取数各自的失败**已经在块内可见**（并各自打了横幅），所以这里不需要再兜一层文案；
 * 但真出现「连它自己都炸了」的意外（Promise 被 reject）时也不许静默：逐条走 `degrade()`。
 */
async function boot() {
  const settings = bootstrapSettings(document.body);
  if (!document.documentElement.dataset.bg) {
    // `<head>` 那段同步脚本没跑成（被 CSP 之类挡掉）：至少别让 body 与 html 不一致
    document.documentElement.dataset.bg = settings.bg;
    document.documentElement.dataset.ink = settings.ink;
    document.documentElement.dataset.size = settings.size;
  }

  wireStatus();
  wireSurvey();
  wireUpload();

  const settled = await Promise.allSettled([refreshSources(), refreshSurveyStatus()]);
  settled.forEach((item, index) => {
    if (item.status !== 'rejected') return;
    const reason = item.reason && item.reason.message ? item.reason.message : String(item.reason);
    degrade('launcher-boot', `启动页第 ${index + 1} 块没能初始化（${reason}）：那一块会显示成失败状态。`);
  });

  D.ready = true;
  document.body.dataset.ready = 'true';
}

boot();
