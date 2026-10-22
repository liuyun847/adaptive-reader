/**
 * 读前背景问卷（PLAN-background-survey.md §3 / §7 / §9 / §11）。
 *
 * 形态与纪律：
 *   - **新建组件**：项目里此前没有任何模态 / 遮罩 / `<dialog>` / `<form>` 先例，所以这里自己造一份。
 *     卡片视觉与交互参考设置面板（`settings.js` 的单选行与面板骨架）。
 *   - **不用 `<form>`**：任何默认提交都会刷新页面；按钮一律 `type="button"`，提交走 JS。
 *   - **不吞正文点击**：遮罩层与卡片的非交互部分都是 `pointer-events: none`，只有真正的控件收事件——
 *     答卷时正文照常可滚、可点、可选字；关闭后整层被移除，不留残影。
 *   - **滚轮归题区**（2026-10-09 补）：题区是卡片里唯一的滚动容器，但它必须是 `pointer-events: none`
 *     （否则卡片那块矩形会吃掉正文点击），于是命中测试会跳过它、滚轮落到背后正文上——
 *     所以在**文档捕获阶段**按坐标命中判定接管（`handleWheel`）：指针在卡片里就滚题区并
 *     `preventDefault()`（滚到边界也不放给页面），指针在层上别处一律不插手（正文照常滚）。
 *   - **键盘可达**：`Esc` 与「跳过」同效（**文档级**监听，见 `handleEscapeKey`：读者点过正文之后
 *     焦点在 `#rewritten` 上，只听卡片的 keydown 会漏）；打开时焦点进入卡片，关闭后焦点回到正文栏
 *     （`#rewritten` 带 `tabindex="-1"`，见 index.html）。
 *   - **模型追问**（契约 §4）：提交成功后同一张卡片进「完成 → 追问」两步（`state.phase` 三态），
 *     `POST /api/survey/followup` 只**生成**问题，读者的作答按 §8 归 `POST /api/survey` 的 `followups` 键。
 *   - **题目只有一个来源**：正式模式取 `GET /api/survey` 的 `questions[]`；夹具模式取
 *     `samples/<id>/survey-questions.json`（那份是从后端的 `QUESTIONS` 直接导出的同一份数据）。
 *     本文件里**没有**第二份手写题表——两份题表必然走偏（2026-10-08 就因为字段名走偏过一次）。
 *   - **版本口径**：「问卷已更新」判定为 `answered_version !== null && answered_version !== version`，
 *     两个值都取 `GET /api/survey` 顶层；**不从浏览器本地存储读版本号**。
 *   - **可见降级**：跳过 / 提交成功 / 夹具模式不保存 / 后端降级 / 版本不一致 / 校验失败 / 题目拉不到 /
 *     追问失败或降级，都在页面上留字，并在 `globalThis.__arDebug.survey*` 留可断言的标志。
 *   - **由按钮显式打开（2026-10-15）**：启动页（`web/index.html` + `web/js/launcher.js`）的「背景测评」
 *     卡片走 `openSurveyManually` 打开**同一张**问卷（题目、提交、跳过、追问全走既有路径），
 *     并用 `setSurveyLifecycleHooks` 在「答完 / 关闭」之后刷新卡片上的状态。
 *     阅读器那条路（`bootSurvey`）既不调这个入口也不设钩子 ⇒ 行为与改造前逐字一致。
 */

import {
  DataError, fetchSurvey, loadFixtureFollowups, loadFixtureQuestions, loadFixtureResponse,
  requestSurveyFollowups, submitSurvey,
} from './api.js';

/** 契约 §3 的问卷版本号；题目或选项一变就升版（后端 `ar.survey.SURVEY_VERSION` 是权威）。 */
export const SURVEY_VERSION = 1;

/** 打开开关（夹具模式要看问卷界面时用）：`?survey=1`。 */
export const SURVEY_PARAM = 'survey';

/**
 * 条件题规则（契约 §3）。
 * 用「声明式条件」而不是散在各处的 if：题目可能来自后端，条件全靠本地规则复算。
 */
export const WHEN = {
  ALWAYS: { kind: 'always' },
};

export function whenEquals(field, values) {
  return { kind: 'equals', field, values };
}

/** `!==`（含「还没答」）：Q3b 在 Q3a 未答时也该跟着 Q3a 出现（契约 §3 的出现条件只看学历）。 */
export function whenNotEquals(field, value) {
  return { kind: 'notEquals', field, value, includeEmpty: true };
}

/* --------------------------------------------------------------- 条件求值 */

function hasValue(value) {
  if (Array.isArray(value)) return value.length > 0;
  return value !== undefined && value !== null && String(value).trim() !== '';
}

/** 单一条件的求值；答不出（题没出现 / 没答）一律按「不满足」算。 */
export function evalWhen(cond, answers) {
  const rules = [];
  if (cond) rules.push(cond);
  if (!rules.length) return true;
  return rules.every((rule) => {
    if (!rule || rule.kind === 'always') return true;
    const value = answers ? answers[rule.field] : undefined;
    if (rule.kind === 'equals') return rule.values.includes(value);
    if (rule.kind === 'notEquals') {
      // includeEmpty：条件字段「还没答」时也算满足（Q3b 与 Q3a 同时出现，选了被排除的值才收起）
      if (rule.includeEmpty && !hasValue(value)) return true;
      if (!hasValue(value)) return false;
      const excluded = Array.isArray(rule.values) && rule.values.length ? rule.values : [rule.value];
      return !excluded.includes(value);
    }
    return true;
  });
}

/** 一题该不该出现：全部条件都要满足（Q2/Q3a/Q3b 的显隐就靠它）。 */
export function isVisible(question, answers) {
  const conds = [question.when, question.alsoWhen].filter(Boolean);
  return conds.every((cond) => evalWhen(cond, answers));
}

/* --------------------------------------------------------------- 造 DOM */

function rawAnswers(form) {
  const answers = {};
  if (!form) return answers;
  form.querySelectorAll('input[data-role="survey-single"]:checked').forEach((input) => {
    answers[input.dataset.field] = input.value;
  });
  form.querySelectorAll('input[data-role="survey-multi"]:checked').forEach((input) => {
    const field = input.dataset.field;
    if (!Array.isArray(answers[field])) answers[field] = [];
    answers[field].push(input.value);
  });
  form.querySelectorAll('[data-role="survey-text"], [data-role="survey-note"]').forEach((el) => {
    const field = el.dataset.field;
    const text = String(el.value == null ? '' : el.value).trim();
    if (text) answers[field] = text;
  });
  return answers;
}

function questionsOf(form) {
  if (!form || !form.__surveyQuestions) return [];
  return form.__surveyQuestions;
}

/**
 * 收集答案。
 *
 * **条件题没出现时字段不写进答案**（契约 §3）：不是写 null，而是整项不出现——
 * 否则「没问过」与「问了没答」在后端就分不开了。
 * 判「这一题出现没有」用的是表单里的**全部勾选值**（与界面显隐同一把尺子）：
 * 若改用「已收进答案的前缀」来判断，像 Q3b 这种依赖二级条件（Q3a ≠ 没读大学）的题
 * 会因为 Q3a 自己还没被收进来而永远判成不可见，答案里就悄悄少一项。
 */
export function collectAnswers(form) {
  const raw = rawAnswers(form);
  return pickVisibleFields(raw, questionsOf(form));
}

/** 按「题目出现没有」过滤一份取值：两次判断（界面显隐 / 收集答案）共用它，口径只有一个。 */
function pickVisibleFields(raw, questions) {
  if (!questions.length) return { ...raw };
  const answers = {};
  questions.forEach((question) => {
    if (!isVisible(question, raw)) return;
    const value = raw[question.field];
    if (value === undefined || value === null) return;
    if (Array.isArray(value) && !value.length) return;
    if (typeof value === 'string' && !value.trim()) return;
    answers[question.field] = value;
  });
  return answers;
}

/**
 * 一题该不该出现（界面显隐）。
 *
 * 判条件时用**表单里的全部勾选值**（`rawAnswers`），不用 `collectAnswers` 的结果：
 * 后者为了「没问过就别写进答案」已经把不可见题的取值滤掉了，条件字段自己未必活到那一步。
 * 两者的差别只影响「送什么」，不影响「看得见什么」——这正是 Q3b 这类二级条件需要的。
 */
export function visibleQuestions(form) {
  const raw = rawAnswers(form);
  return questionsOf(form).filter((q) => isVisible(q, raw));
}

/** 当前条件题规则算出来的可见题目快照（无头验证据此断言联动）。 */
function snapshotVisibility(form) {
  const vis = {};
  const raw = rawAnswers(form);
  questionsOf(form).forEach((q) => { vis[q.qid] = isVisible(q, raw); });
  return vis;
}

function questionBlock(question) {
  const block = document.createElement('div');
  block.className = 'survey__q';
  block.dataset.role = 'survey-question';
  block.dataset.qid = question.qid;

  const label = document.createElement('div');
  label.className = 'survey__label';
  label.textContent = question.label;
  block.appendChild(label);

  if (question.hint) {
    const hint = document.createElement('div');
    hint.className = 'survey__hint';
    hint.dataset.role = 'survey-hint';
    hint.textContent = question.hint;
    block.appendChild(hint);
  }

  if (question.kind === 'single' || question.kind === 'multi') {
    const group = document.createElement('div');
    group.className = 'survey__options';
    group.setAttribute('role', question.kind === 'single' ? 'radiogroup' : 'group');
    group.setAttribute('aria-label', question.label);
    (question.options || []).forEach((option) => {
      const row = document.createElement('label');
      row.className = 'survey__option';
      row.dataset.role = 'survey-option';
      row.dataset.value = option.value;

      const input = document.createElement('input');
      input.type = question.kind === 'single' ? 'radio' : 'checkbox';
      input.name = `ar-survey-${question.qid}`;
      input.value = option.value;
      input.dataset.role = question.kind === 'single' ? 'survey-single' : 'survey-multi';
      input.dataset.field = question.field;
      input.dataset.qid = question.qid;
      input.dataset.value = option.value;

      const text = document.createElement('span');
      text.textContent = option.label || option.value;

      row.appendChild(input);
      row.appendChild(text);
      group.appendChild(row);
    });
    block.appendChild(group);
    return block;
  }

  const input = document.createElement(question.kind === 'textarea' ? 'textarea' : 'input');
  if (question.kind === 'textarea') input.rows = 3;
  else input.type = 'text';
  input.className = 'survey__text';
  input.dataset.role = question.kind === 'textarea' ? 'survey-note' : 'survey-text';
  input.dataset.field = question.field;
  input.dataset.qid = question.qid;
  if (question.placeholder) input.placeholder = question.placeholder;
  if (question.maxLength) input.maxLength = question.maxLength;
  input.setAttribute('aria-label', question.label);
  block.appendChild(input);
  return block;
}

/**
 * 造问卷卡片（不含遮罩层）。
 * @returns {HTMLDivElement} 卡片元素（带 `data-role="survey-card"`）
 */
export function buildSurveyForm(doc = document) {
  const card = doc.createElement('div');
  card.className = 'survey';
  card.dataset.role = 'survey-card';
  card.dataset.state = 'empty';
  card.setAttribute('role', 'dialog');
  card.setAttribute('aria-modal', 'false');
  card.setAttribute('aria-label', '读前背景问卷');

  const head = doc.createElement('div');
  head.className = 'survey__head';
  const title = doc.createElement('span');
  title.className = 'survey__title';
  title.dataset.role = 'survey-title';
  title.textContent = '读前背景问卷';
  head.appendChild(title);
  const close = doc.createElement('button');
  close.type = 'button';
  close.className = 'survey__close';
  close.dataset.role = 'survey-close';
  close.setAttribute('aria-label', '关闭背景问卷（与「跳过」同效）');
  close.textContent = '×';
  head.appendChild(close);
  card.appendChild(head);

  const lead = doc.createElement('div');
  lead.className = 'survey__lead';
  lead.dataset.role = 'survey-lead';
  lead.textContent = '这些背景只用来决定改写时哪段展开、哪段保持，跨材料长期复用；不想答可以整场跳过。';
  card.appendChild(lead);

  // 阶段说明：提交成功后在这里写「背景已保存…」，并放进「让模型追问几个细节（可选）」入口
  const phase = doc.createElement('div');
  phase.className = 'survey__phase';
  phase.dataset.role = 'survey-phase';
  phase.hidden = true;
  card.appendChild(phase);

  const body = doc.createElement('div');
  body.className = 'survey__body';
  body.dataset.role = 'survey-body';
  card.appendChild(body);

  const status = doc.createElement('div');
  status.className = 'survey__status';
  status.dataset.role = 'survey-status';
  status.dataset.state = 'empty';
  status.setAttribute('role', 'status');
  status.hidden = true;
  card.appendChild(status);

  const foot = doc.createElement('div');
  foot.className = 'survey__foot';
  foot.dataset.role = 'survey-foot';

  // 问题阶段的按钮（提交成功后由 renderDonePhase 换成「追问 / 完成」那一组）
  const skip = doc.createElement('button');
  skip.type = 'button';
  skip.className = 'survey__btn survey__btn--ghost';
  skip.dataset.role = 'survey-skip';
  skip.dataset.phase = 'questions';
  skip.textContent = '跳过';
  foot.appendChild(skip);
  const submit = doc.createElement('button');
  submit.type = 'button';
  submit.className = 'survey__btn survey__btn--primary';
  submit.dataset.role = 'survey-submit';
  submit.dataset.phase = 'questions';
  submit.textContent = '保存背景';
  foot.appendChild(submit);

  // 追问阶段的按钮
  const followupBtn = doc.createElement('button');
  followupBtn.type = 'button';
  followupBtn.className = 'survey__btn survey__btn--primary';
  followupBtn.dataset.role = 'survey-followup-start';
  followupBtn.dataset.phase = 'done';
  followupBtn.textContent = '让模型追问几个细节（可选）';
  followupBtn.hidden = true;
  foot.appendChild(followupBtn);

  const followupSave = doc.createElement('button');
  followupSave.type = 'button';
  followupSave.className = 'survey__btn survey__btn--primary';
  followupSave.dataset.role = 'survey-followup-save';
  followupSave.dataset.phase = 'followup';
  followupSave.textContent = '记下追问的答案';
  followupSave.hidden = true;
  foot.appendChild(followupSave);

  const finish = doc.createElement('button');
  finish.type = 'button';
  finish.className = 'survey__btn survey__btn--ghost';
  finish.dataset.role = 'survey-finish';
  finish.dataset.phase = 'done';
  finish.textContent = '完成';
  finish.hidden = true;
  foot.appendChild(finish);

  const followupSkip = doc.createElement('button');
  followupSkip.type = 'button';
  followupSkip.className = 'survey__btn survey__btn--ghost';
  followupSkip.dataset.role = 'survey-followup-skip';
  followupSkip.dataset.phase = 'followup';
  followupSkip.textContent = '不用了';
  followupSkip.hidden = true;
  foot.appendChild(followupSkip);

  const escNote = doc.createElement('span');
  escNote.className = 'survey__note';
  escNote.dataset.role = 'survey-foot-note';   // 注意别叫 survey-note：那是补充说明输入框的角色
  escNote.textContent = '按 Esc 与「跳过」同效。';
  foot.appendChild(escNote);
  card.appendChild(foot);

  return card;
}

/**
 * 造问卷所在的层（卡片 + 定位）。
 *
 * 层本身**不吃指针事件**（CSS 的 `.survey-layer { pointer-events: none }`）；
 * 卡片内部再分两级：控件收事件、非交互部分（标题 / 说明 / 状态区 / 题区）让路——
 * 于是问卷打开时正文照常可以滚动、点击、选中文字。这就是「不吞正文点击」的落点。
 * 题区让路之后它自己的滚轮由 `handleWheel` 按坐标接住（见那里的说明）。
 */
export function buildSurveyLayer(doc = document) {
  const layer = doc.createElement('div');
  layer.className = 'survey-layer';
  layer.dataset.role = 'survey-layer';
  layer.appendChild(buildSurveyForm(doc));
  return layer;
}

/* --------------------------------------------------------------- 状态同步 */

function setStatus(form, state_, text) {
  const status = form.querySelector('[data-role="survey-status"]');
  form.dataset.state = state_;
  if (!status) return;
  status.dataset.state = state_;
  status.textContent = text || '';
  status.hidden = !text;
}

/**
 * 阶段说明区（卡片顶部，题目前的那个块）：写当前这一步的结论，并把追问入口放进去。
 * 与「状态区」（`survey-status`，写校验/错误）分开：一个是阶段说明，一个是本次操作的结果。
 */
function setPhase(form, { title = '', note = '', actions = [], state: phaseState = '' } = {}) {
  const box = form.querySelector('[data-role="survey-phase"]');
  if (!box) return;
  box.textContent = '';
  box.hidden = !title && !note && !actions.length;
  box.dataset.state = phaseState;
  if (title) {
    const head = document.createElement('div');
    head.className = 'survey__phase-title';
    head.dataset.role = 'survey-phase-title';
    head.textContent = title;
    box.appendChild(head);
  }
  if (note) {
    const text = document.createElement('div');
    text.className = 'survey__phase-note';
    text.dataset.role = 'survey-phase-note';
    text.textContent = note;
    box.appendChild(text);
  }
  actions.forEach((action) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'survey__btn survey__btn--primary';
    btn.dataset.role = action.role;
    btn.dataset.phase = 'followup';
    btn.textContent = action.label;
    box.appendChild(btn);
  });
}

/** 按阶段切换底部那一组按钮（问题阶段 / 提交完成后 / 追问阶段）。 */
function setButtonPhase(form, phase) {
  form.querySelectorAll('[data-role="survey-foot"] [data-phase]').forEach((btn) => {
    btn.hidden = btn.dataset.phase !== phase;
  });
  const note = form.querySelector('[data-role="survey-foot-note"]');
  if (note) {
    note.textContent = phase === 'questions'
      ? '按 Esc 与「跳过」同效。'
      : '追问只问这一次；不答也行，固定题的背景已经保存好了。';
  }
}

function syncBody(form, debug) {
  const seen = [];
  const raw = rawAnswers(form);
  Array.from(form.querySelectorAll('[data-role="survey-question"]')).forEach((block) => {
    const question = questionsOf(form).find((q) => q.qid === block.dataset.qid);
    if (!question) return;
    const visible = isVisible(question, raw);
    block.hidden = !visible;
    block.dataset.visible = String(visible);
    seen.push(block.dataset.qid);
  });
  const answers = collectAnswers(form);
  state.visible = snapshotVisibility(form);
  if (debug) {
    debug.surveyVisible = { ...state.visible };
    debug.surveySeen = seen.slice();
    debug.surveyAnswers = answers;
    debug.surveyAnswerCount = Object.keys(answers).length;
  }
  return seen;
}

/**
 * 校验：条件题先按规则复算一遍（只校验**当前可见**的题），
 * 不通过就把错误写在问卷里（不静默、不发请求）。后端还会独立复算同一规则。
 * @returns {string[]} 错误清单（空数组＝通过）
 */
export function validateAnswers(form) {
  const answers = collectAnswers(form);
  const errors = [];
  visibleQuestions(form).forEach((question) => {
    const value = answers[question.field];
    if (question.exactly && Array.isArray(value)
      && !value.some((v) => (question.exclusive || []).includes(v))
      && value.length !== question.exactly) {
      errors.push(`「${question.label}」要正好选 ${question.exactly} 项（现在选了 ${value.length} 项），或选「没上过高中 / 记不清」。`);
    }
    if (question.atLeast && Array.isArray(value) && value.length < question.atLeast) {
      errors.push(`「${question.label}」请至少选 1 项，或选「都没有」。`);
    }
  });
  return errors;
}

/* --------------------------------------------------------------- 模块状态 */

const state = {
  host: null,              // #survey-host（index.html 里的宿主）
  layer: null,             // 遮罩层（pointer-events: none，不吞正文点击）
  form: null,
  ctx: null,
  open: false,
  openReason: null,
  mode: 'api',
  phase: 'questions',       // questions | done | followup（同一次问卷流程里的三个阶段）
  version: null,            // 本次打开用的问卷版本（题目来源的 version）
  questionSource: null,     // api | fixture | fixture-response | api-fallback-fixture
  meta: null,
  answers: {},
  visible: {},              // 题目显隐快照（`__arDebug.surveyVisible` 的来源）
  degraded: [],
  events: [],
  bannerFn: null,           // main.js 挂进来的「可见横幅」出口
  degradeFn: null,          // main.js 的 degrade()（顺带记 __arDebug.degradations）
  focusBefore: null,
  popupSuppressed: false,   // 关闭（跳过 / 已保存 / 显式关掉）后本次页面不再自动弹
  // 生命周期钩子（2026-10-15 起给启动页用：答完 / 关闭之后刷新卡片上的问卷状态）：
  // **阅读器不设这两个钩子**，所以那边的行为一个字都没变（见 setSurveyLifecycleHooks）。
  hooks: { closed: null, submitted: null },
};

function debugHandle() {
  return globalThis.__arDebug || null;
}

/** 开关状态同步（`__arDebug` 是唯一权威：main.js 的 Esc 让路判定也读它）。 */
function setOpenFlag(open) {
  state.open = Boolean(open);
  const debug = debugHandle();
  if (debug) {
    debug.surveyOpen = state.open;
    debug.surveyState = state.open ? 'open' : 'closed';
  }
  if (typeof document !== 'undefined' && document.body && document.body.dataset) {
    document.body.dataset.survey = state.open ? 'open' : 'closed';
  }
}

/** 问卷此刻是否展开（main.js 的 Esc 让路判定用它，不靠轮询）。 */
export function isSurveyOpen() {
  const debug = debugHandle();
  if (debug && typeof debug.surveyOpen === 'boolean') return debug.surveyOpen;
  return state.open;
}

function emit(type, detail = {}) {
  const debug = debugHandle();
  if (!debug) return detail;
  state.events.push({ type, at: Date.now(), ...detail });
  debug.surveyEvents = state.events.map((e) => ({ ...e }));
  debug.surveyOpen = state.open;
  debug.surveyState = state.open ? (type === 'submit-request' ? 'submitting' : 'open') : 'closed';
  if (Object.prototype.hasOwnProperty.call(detail, 'answers')) {
    const answers = detail.answers || {};
    debug.surveyAnswers = answers;
    debug.surveyAnswerCount = Object.keys(answers).length;
  }
  return detail;
}

function banner(text, kind = 'warn') {
  if (typeof state.bannerFn === 'function') state.bannerFn(text, kind);
}

function note(message, kind = 'survey') {
  state.degraded.push({ kind, message });
  const debug = debugHandle();
  if (debug) debug.surveyDegraded = state.degraded.map((d) => ({ ...d }));
  if (typeof state.degradeFn === 'function') state.degradeFn(kind, message);
  else banner(message, 'warn');
}

/** `note()` 的别名：调用处写 `degrade(...)` 更贴 main.js 的既有命名。 */
const degrade = note;

/**
 * 生命周期钩子（2026-10-15 新增，**只给启动页用**）。
 *
 * 启动页的「背景测评」卡片要先显示现状（已答 / 已跳过 / 未答），而读者点开问卷答完或关掉之后，
 * 那张卡片必须**立刻**刷新——不许靠轮询、也不许靠 sleep（无头验证只等显式状态）。
 * 所以问卷自己在这里留两个显式出口：
 *   - `closed(reason)`：问卷整层被移除之后调用（跳过 / 完成 / Esc / × 都算）；
 *   - `submitted({answers})`：**正式模式提交成功**之后调用（这时卡片还在「已完成 + 要不要追问」那一步，
 *     读者可能过一会儿才关掉它；状态卡不该等到那时候才变成「已答」）。
 *
 * 阅读器（`main.js` 的 `bootSurvey`）从没调过这个函数 ⇒ 两个钩子都是 `null` ⇒ 那条路行为逐字不变。
 * 钩子里抛错**不许**影响问卷自己的收尾（关闭流程已经走完了），只把错误记进 `__arDebug.errors`。
 */
export function setSurveyLifecycleHooks({ closed = null, submitted = null } = {}) {
  state.hooks = {
    closed: typeof closed === 'function' ? closed : null,
    submitted: typeof submitted === 'function' ? submitted : null,
  };
}

/** 触发一个生命周期钩子；钩子自己的异常不回流（记进调试面，见上）。 */
function notifyHook(name, payload) {
  const fn = state.hooks ? state.hooks[name] : null;
  if (typeof fn !== 'function') return;
  try {
    fn(payload);
  } catch (err) {
    const debug = debugHandle();
    if (debug && Array.isArray(debug.errors)) {
      debug.errors.push({ kind: `survey-hook-${name}`, message: String((err && err.message) || err) });
    }
  }
}

/* --------------------------------------------------------------- 打开 / 关闭 */

function clearLayer() {
  if (state.layer && state.layer.parentNode) state.layer.parentNode.removeChild(state.layer);
  state.layer = null;
  state.form = null;
}

/**
 * 关掉问卷并清干净现场：焦点回到正文栏（打开前记着的那一个元素还在就用它）。
 * 一律不写 `hidden=false` 的残留层——「关闭后不留残影」靠这里保证。
 */
export function closeSurvey(reason = 'unknown') {
  if (!state.open) return;
  const answers = state.form ? collectAnswers(state.form) : state.answers;
  setOpenFlag(false);
  state.popupSuppressed = true;
  emit('close', { reason, answers });
  clearLayer();
  // 层已经收干净了才通知外面（启动页据此刷新「背景测评」卡片的状态；阅读器没设钩子）
  notifyHook('closed', { reason, answers });

  const before = state.focusBefore;
  state.focusBefore = null;
  if (before && typeof before.focus === 'function' && before.isConnected) {
    try { before.focus(); return; } catch (err) { /* 落回正文栏 */ }
  }
  const article = document.getElementById('rewritten');
  if (article && typeof article.focus === 'function') {
    try { article.focus({ preventScroll: true }); } catch (err) { /* 焦点回不去也不报错 */ }
  }
}

/**
 * 由后端的 `requires`（条件题的**权威**形态）推出一条前端条件规则。
 *
 * 为什么要容错：后端同时给了 `when` / `alsoWhen` 别名，其中 `alsoWhen` 取 `not_in` 的**第一个值**，
 * 而权威的 `requires` 可能排除多个值（Q3b 的 `not_in` 是 `["none", "unsure"]`）。
 * 只看别名会让 Q3b 在选「不确定」时仍然出现，读者填的专业随后被后端按「条件不成立」丢掉——
 * 所以这里优先用 `requires` 复算，别名只当它在时的兜底。
 */
export function ruleFromRequires(requirements, index = 0) {
  const list = Array.isArray(requirements) ? requirements : [];
  const requirement = list[index];
  if (!requirement) return null;
  const field = String(requirement.field
    || ANSWER_SLOT_FIELDS[String(requirement.question || '')]
    || '');
  if (!field) return null;
  if (Array.isArray(requirement.in)) {
    return { kind: 'equals', field, values: requirement.in.map(String) };
  }
  const excluded = (Array.isArray(requirement.not_in) ? requirement.not_in : [])
    .map(String).filter((v) => v);
  if (!excluded.length) return null;
  // 前端语义：条件字段「还没答」时也算满足（Q3b 与 Q3a 同时出现，选了被排除的值才收起）
  return {
    kind: 'notEquals',
    field,
    value: excluded[0],
    values: excluded,
    includeEmpty: true,
  };
}

/** 题号 → 答案字段（与后端 `ANSWER_SLOTS` 同表；只在题目没给 `field` 时兜底）。 */
export const ANSWER_SLOT_FIELDS = {
  Q1: 'education',
  Q2: 'high_school_electives',
  Q3a: 'discipline',
  Q3b: 'major',
  Q4: 'strengths',
  Q4b: 'strengths_note',
  Q5: 'reading_materials',
  Q6: 'preference',
};

/**
 * 规整后端 / 夹具题目：只做形状归一（缺省值、类型转换）+ 条件规则兜底。
 *
 * 条件是后端给的：优先 `requires`（权威）复算，其次 `when` / `alsoWhen` 别名，
 * 都没有就「总是出现」。前端不维护自己的一张条件表。
 * 纯函数、无 DOM 依赖——测试直接拿它对着后端自己的 `QUESTIONS` 比对。
 */
export function normalizeQuestions(provided) {
  return (Array.isArray(provided) ? provided : []).map((raw) => {
    const qid = String(raw.qid || raw.id || '').trim();
    const kind = ['single', 'multi', 'text', 'textarea'].includes(raw.kind) ? raw.kind : 'single';
    const derivedWhen = ruleFromRequires(raw.requires, 0);
    const derivedAlso = ruleFromRequires(raw.requires, 1);
    return {
      qid,
      field: String(raw.field || raw.answer_slot || ANSWER_SLOT_FIELDS[qid] || qid),
      label: String(raw.label || raw.title || qid),
      kind,
      hint: raw.hint ? String(raw.hint) : '',
      placeholder: raw.placeholder ? String(raw.placeholder) : '',
      maxLength: Number.isFinite(Number(raw.maxLength)) ? Number(raw.maxLength) : 0,
      exactly: Number.isFinite(Number(raw.exactly)) ? Number(raw.exactly) : 0,
      atLeast: Number.isFinite(Number(raw.atLeast)) ? Number(raw.atLeast) : 0,
      exclusive: Array.isArray(raw.exclusive) ? raw.exclusive.map(String) : [],
      when: derivedWhen || raw.when || WHEN.ALWAYS,
      alsoWhen: derivedAlso || raw.alsoWhen || null,
      options: (Array.isArray(raw.options) ? raw.options : []).map((option) => ({
        value: String(option.value),
        label: String(option.label || option.value),
      })),
    };
  }).filter((q) => q.qid && (q.kind === 'text' || q.kind === 'textarea' || q.options.length));
}

function renderQuestions(form, provided, fixture) {
  const body = form.querySelector('[data-role="survey-body"]');
  body.textContent = '';
  const normalized = normalizeQuestions(provided);

  form.__surveyQuestions = normalized;
  form.dataset.fixture = String(Boolean(fixture));
  normalized.forEach((q) => body.appendChild(questionBlock(q)));

  // 回填已存答案（改过问卷就只回填对得上的取值，剩下的留空）
  const answers = state.answers && typeof state.answers === 'object' ? state.answers : {};
  normalized.forEach((question) => {
    const value = answers[question.field];
    if (value === undefined || value === null) return;
    if (question.kind === 'single') {
      const input = body.querySelector(`input[data-role="survey-single"][data-field="${question.field}"][value="${value}"]`);
      if (input) input.checked = true;
    } else if (question.kind === 'multi') {
      (Array.isArray(value) ? value : [value]).forEach((v) => {
        const input = body.querySelector(`input[data-role="survey-multi"][data-field="${question.field}"][value="${v}"]`);
        if (input) input.checked = true;
      });
    } else {
      const el = body.querySelector(`[data-role="survey-text"][data-field="${question.field}"], [data-role="survey-note"][data-field="${question.field}"]`);
      if (el) el.value = String(value);
    }
  });
}

function mountForm(questions, { fixture, version }) {
  const host = state.host || document.getElementById('survey-host') || document.body;
  state.host = host;
  clearLayer();

  const layer = buildSurveyLayer();
  const form = layer.querySelector('[data-role="survey-card"]');
  host.appendChild(layer);
  state.layer = layer;
  state.form = form;
  state.version = version === undefined ? SURVEY_VERSION : version;

  renderQuestions(form, questions, fixture);
  form.addEventListener('click', onClick);
  form.addEventListener('change', onFormChange);
  // Esc 不在卡片上单独监听：统一走文档级的 handleEscapeKey()，否则焦点被正文拿走时按 Esc 没人接
  // （`#rewritten` 是 tabindex="-1" 可聚焦的，点正文就会把焦点带走——那是设计上允许的）

  const title = form.querySelector('[data-role="survey-title"]');
  if (title) {
    title.textContent = `读前背景问卷（v${state.version}）`;
  }
  // 回填完之后再算一次联动：回填过答案时，依赖它们的条件题要立刻跟着出现
  syncBody(form, debugHandle());

  const first = form.querySelector('input[data-role="survey-single"], input[data-role="survey-multi"]');
  if (first && typeof first.focus === 'function') first.focus();
  return form;
}

/**
 * 打开问卷。题目必须由调用方给（正式模式来自后端 `GET /api/survey`，夹具模式来自
 * `samples/<id>/survey-questions.json`）——本模块没有兜底题表，给空就只渲染一个空壳。
 *
 * @param {{reason?:string, fixture?:boolean, questions?:Array, version?:number, answers?:object}} opts
 */
export function openSurvey(opts = {}) {
  const { reason = 'auto', fixture = false, questions = [], version = SURVEY_VERSION } = opts;
  const debug = debugHandle();
  state.openReason = reason;
  state.mode = fixture ? 'fixture' : 'api';
  if (opts.answers && typeof opts.answers === 'object') state.answers = opts.answers;
  state.focusBefore = document.activeElement && document.activeElement !== document.body
    ? document.activeElement
    : null;

  setOpenFlag(true);
  emit('open', { reason, mode: state.mode });
  state.phase = 'questions';
  mountForm(Array.isArray(questions) ? questions : [], { fixture, version });
  setButtonPhase(state.form, 'questions');
  const debugPhase = debugHandle();
  if (debugPhase) debugPhase.surveyPhase = state.phase;
  state.meta = {
    version: state.version,
    openReason: reason,
    fixture,
    questions: (state.form.__surveyQuestions || []).map((q) => q.qid),
    answers: state.answers || {},
  };
  if (debug) {
    debug.survey = { ...state.meta, visible: { ...state.visible } };
  }
  return state.form;
}

/* --------------------------------------------------------------- 事件 */

/** 题区的滚动容器（卡片里唯一的一个）。 */
function surveyBody(form) {
  return form ? form.querySelector('[data-role="survey-body"]') : null;
}

/**
 * 滚轮归属（2026-10-09 用户实测缺陷「背景问卷的滚动会穿透」的修法）。
 *
 * 现场：指针停在题区上滚动，题区自己不滚，背后正文跟着跑；题区滚到边界后正文接着跑。
 * 根因：题区是卡片里唯一的滚动容器，却必须留在 `pointer-events: none` 那一组里
 * （给它 auto，卡片这块矩形就变成点击死区，会吃掉「卡片范围内的正文点击」——
 * 两条既有用例会同时失败，见 reader.css 里问卷那一段注释）。而命中测试会**跳过**
 * `pointer-events: none` 的元素：指针落在题区的空白、间隙上时没有任何元素可命中，
 * 滚轮直接落到背后正文上，题区永远收不到（实测：指针在题区正中，
 * `window.scrollY` 0 → 300，题区 `scrollTop` 一直 0）。
 *
 * 修法：滚轮在**文档的捕获阶段**收（卡片自己也是 `pointer-events: none`，挂在卡片上收不到），
 * 再按**坐标命中判定**决定归谁——把浏览器因为 `pointer-events: none` 跳过的那次命中测试，
 * 用同一套判据在 JS 里补回来：
 *   - 指针在**卡片**矩形里 ⇒ 这一下滚轮归问卷：`preventDefault()` 拦下默认动作，
 *     滚动量给题区（滚到边界也不放给页面，否则正文会接着跑）；
 *   - 指针在卡片之外（遮罩层上）⇒ 一律不插手，正文照常滚（既有契约，不许破）。
 *
 * **`textarea` 是唯一的例外（2026-10-09 复核补）**：固定题的 Q3b / Q4b 与追问阶段的作答框都是
 * `<textarea>`，它自己就是滚动区（`pointer-events: auto`，`rows = 3`，答案长一点就溢出）。
 * 而 `preventDefault()` 取消的是**整条事件链的默认滚动动作**——先 `preventDefault()` 再让路
 * 会把 textarea 自己的滚动一起取消（实测：文本滚不动，`scrollTop` 恒 0，只能拖滚动条或用键盘），
 * 所以这里按「它在这一方向上还能不能滚」分流：
 *   - 还能滚 ⇒ **不拦**，原生滚动留给 textarea 自己（`window` 也不会因此被带动：
 *     它的滚动祖先是题区，题区 `overscroll-behavior: contain`）；
 *   - 滚不动了（已在顶 / 已在底 / 本来就没溢出）⇒ `preventDefault()` + 把 `deltaY` 加到题区，
 *     保住「卡片范围内绝不带动正文」这条不变量。
 *   这条原生链是**实测**过的（把本函数整体停用后量）：textarea 在 969/969 到底、再滚一次，
 *   滚的是**题区**（`body.scrollTop` 247 → 639）、`window.scrollY` 恒 0。
 *   - 只用 `deltaY`（问卷题区是纵排的；`deltaX` 走事件自带的横滚，不接管）。
 *   导出只为让无头用例能直接喂一个滚轮事件验判定（与 `renderFollowupPhase` 同一口径）。
 */
export function handleWheel(event) {
  if (!state.open) return;                       // 文档级监听：问卷没开就什么都不做
  const form = state.form;
  const body = surveyBody(form);
  if (!body) return;
  if (!insideRect(event, form.getBoundingClientRect())) return;   // 层上别处：正文照常滚
  const inner = innerScroller(event.target);
  if (inner && canScroll(inner, event.deltaY)) return;   // 子滚动区还能滚：别拦它的原生滚动
  event.preventDefault();
  body.scrollTop += event.deltaY;
}

/** 指针是不是落在这块矩形里（事件坐标与 `getBoundingClientRect()` 同为视口坐标）。 */
function insideRect(event, rect) {
  return event.clientX >= rect.left && event.clientX <= rect.right
    && event.clientY >= rect.top && event.clientY <= rect.bottom;
}

/** 指针下面那个**自带滚动**的子滚动区（问卷里只有 `<textarea>` 这一种）。 */
function innerScroller(target) {
  return target && target.closest ? target.closest('textarea') : null;
}

/**
 * 它在这个滚轮方向上还能不能滚（`canScrollUp` / `canScrollDown` 同一把尺子：
 * 滚不动就是「已在顶 / 已在底 / 本来没溢出」）。
 *
 * `scrollTop` 在缩放/小数行高下可能是小数，所以留半像素余量，别把「差 0.5px 到底」
 * 判成还能滚（那会拦下一次本该由题区接手的滚动）。
 */
function canScroll(el, deltaY) {
  const max = el.scrollHeight - el.clientHeight;
  if (max <= 0) return false;                    // 本来就没溢出
  if (deltaY < 0) return el.scrollTop > 0.5;
  if (deltaY > 0) return max - el.scrollTop > 0.5;
  return false;
}

function onFormChange(event) {
  const input = event.target;
  if (!input || !input.dataset) return;
  const group = input.closest ? input.closest('[data-role="survey-question"]') : null;
  if (group) group.dataset.answered = 'true';
  // 互斥项（「没上过高中 / 记不清」「都没有」「很少读长文」）一勾上，同组多选全部让位
  const form = state.form;
  if (input.dataset.role === 'survey-multi' && form) {
    const question = questionsOf(form).find((q) => q.qid === input.dataset.qid);
    const exclusive = (question && question.exclusive) || [];
    if (input.checked && exclusive.includes(input.value)) {
      form.querySelectorAll(`input[data-role="survey-multi"][data-qid="${input.dataset.qid}"]`).forEach((other) => {
        if (other !== input) other.checked = false;
      });
    } else if (input.checked) {
      form.querySelectorAll(`input[data-role="survey-multi"][data-qid="${input.dataset.qid}"]`).forEach((other) => {
        if (exclusive.includes(other.value)) other.checked = false;
      });
    }
  }
  const visible = syncBody(form, debugHandle());
  emit('change', { qid: input.dataset.qid, value: input.value, visible });
  const debug = debugHandle();
  if (debug && debug.survey) {
    debug.survey.visible = { ...state.visible };
    debug.survey.answers = collectAnswers(form);
  }
}

/**
 * 提交：先本地校验（条件题规则复算），再 POST /api/survey。
 * 夹具模式没有后端，只显示「夹具模式不保存」并把答案留在 `__arDebug.surveyAnswers`。
 *
 * ⚠ 提交体**不带 `version`**：后端的 `validate_answers` 只认八个字段槽位
 * （`ANSWER_SLOTS.values()`），多给一个键就是 `InputError` → HTTP 400（已实测）。
 * 问卷版本由后端自己按它的 `SURVEY_VERSION` 落进 `background.version`。
 */
function requestSubmit() {
  const form = state.form;
  if (!form || !state.open) return;
  const answers = collectAnswers(form);
  state.answers = answers;

  const errors = validateAnswers(form);
  if (errors.length) {
    setStatus(form, 'invalid', `还不能保存：${errors.join(' ')}`);
    emit('invalid', { answers, errors });
    return;
  }
  setStatus(form, 'submitting', '正在保存背景…');
  emit('submit-request', { answers });

  if (state.mode === 'fixture') {
    finishSubmit('fixture', {
      answers,
      message: '夹具模式不保存：这套界面读的是静态样例，答案不会写进任何画像文件。',
      bannerText: '夹具模式不保存背景：这份表单只是用来看问卷界面（答案不落盘）。',
    });
    return;
  }

  submitSurvey(state.ctx, { answers })
    .then(({ payload, url }) => {
      const degraded = Array.isArray(payload.degraded) ? payload.degraded.filter(Boolean) : [];
      const saved = { ...answers, ...(payload.background && typeof payload.background === 'object' ? payload.background : {}) };
      if (degraded.length) {
        note(`背景已提交，但后端有降级说明：${degraded.join('；')}`, 'survey-degraded');
      }
      finishSubmit('saved', {
        answers: saved,
        response: payload,
        message: '背景已保存（后端已写进读者画像）。',
        bannerText: '背景已保存。改写产物要重跑 ar rewrite 才会用上背景（前端不会自动触发重跑）。',
        url,
      });
    })
    .catch((err) => {
      // 提交失败：表单留在原地，答案不丢（读者可以重试或跳过）
      const copy = `背景没能保存：${err && err.message ? err.message : err}。答案还在表单里，可以重试或整场跳过。`;
      setStatus(form, 'error', copy);
      note(copy, 'survey-error');
      emit('submit-error', { answers, message: copy });
    });
}

function requestSkip(reason = 'button') {
  const form = state.form;
  const answers = form ? collectAnswers(form) : {};
  if (form && state.mode !== 'fixture' && Object.keys(answers).length) {
    // 已经答过一部分又问「跳过」：先说清楚会丢什么
    const confirmNote = '跳过会按「未提供背景」处理：已经选过的内容不会保存。再点一次「跳过」即确认。';
    if (form.dataset.skipArmed !== 'true') {
      form.dataset.skipArmed = 'true';
      setStatus(form, 'confirm', confirmNote);
      return;
    }
  }
  if (state.mode === 'fixture') {
    emit('skip-request', { reason, answers });
    emit('skip-done', { reason, answers: {}, fixture: true });
    banner('夹具模式不保存：已按「跳过」处理这份表单，不会写进任何画像文件。', 'warn');
    closeSurvey(reason);
    return;
  }
  emit('skip-request', { reason, answers });
  submitSurvey(state.ctx, { skipped: true })
    .then(({ payload }) => {
      const degraded = Array.isArray(payload.degraded) ? payload.degraded.filter(Boolean) : [];
      if (degraded.length) note(`跳过已提交，但后端有降级说明：${degraded.join('；')}`, 'survey-degraded');
      emit('skip-done', { reason, answers: {}, response: payload });
      banner('问卷已跳过并记下（skipped=true）。未提供背景，按通用读者处理：改写不会按你的背景调整，以后想补随时可以从材料页重填。', 'warn');
      closeSurvey(reason);
    })
    .catch((err) => {
      // 没落盘就不许关掉表单（2026-10-08 上级裁决）：留下可重试的入口，并说清「下次还会再问」
      const copy = `跳过没有保存成功：${err && err.message ? err.message : err}。`
        + '可以直接再点一次「跳过」重试；也可以关掉问卷直接阅读——'
        + '服务端没有记下这次跳过，所以下次打开材料时它还会再问你一次。';
      if (form) setStatus(form, 'error', copy);
      note(copy, 'survey-error');
      emit('skip-error', { reason, message: copy });
    });
}

function finishSubmit(kind, { answers, message, bannerText, response, url }) {
  emit('submit-done', { answers, kind, response: response || null, url: url || null, message });
  // 提交成功 / 夹具不保存都走同一个可见出口：横幅 + `__arDebug.surveyEvents`
  banner(bannerText, kind === 'fixture' ? 'warn' : 'info');
  if (kind === 'fixture') {
    // 夹具模式没有追问后端：收尾时把「不保存」写清楚（这一步不做成常驻按钮）
    emit('followups-unavailable', { reason: 'fixture' });
    closeSurvey('submitted-fixture');
    return;
  }
  // 正式模式：同一张卡片换成「已完成 + 让模型追问几个细节（可选）」——契约 §4 的「问一次」
  renderDonePhase(state.form, { message, bannerText });
  emit('followups-offered', { url: url || null });
  // 背景已经写进画像了：立刻通知外面刷新状态（启动页的卡片这时就该是「已答」，
  // 不必等读者再点「完成」把卡片关掉）。
  notifyHook('submitted', { answers });
}

/* ------------------------------------------------------- 模型追问（契约 §4） */

/** 把追问作答收成 `{q, a}`（空答的**不提交**：不写空条目）。 */
function collectFollowupAnswers(form) {
  if (!form) return [];
  return Array.from(form.querySelectorAll('[data-role="survey-followup-item"]'))
    .map((row) => {
      const input = row.querySelector('[data-role="survey-followup-answer"]');
      const answer = String((input && input.value) || '').trim();
      if (!answer) return null;
      return { q: row.dataset.q || '', a: answer };
    })
    .filter(Boolean);
}

/** 提交成功后：同一张卡片换成「已完成 + 让模型追问」这一步（不是常驻按钮）。 */
function renderDonePhase(form, { message, bannerText }) {
  const phase = form.querySelector('[data-role="survey-phase"]');
  if (!phase) return;
  phase.textContent = '';
  phase.hidden = false;
  phase.dataset.state = 'done';
  const head = document.createElement('div');
  head.className = 'survey__phase-title';
  head.dataset.role = 'survey-phase-title';
  head.textContent = '背景已保存';
  const note = document.createElement('div');
  note.className = 'survey__phase-note';
  note.dataset.role = 'survey-phase-note';
  note.textContent = `${message}${bannerText}。`;
  phase.appendChild(head);
  phase.appendChild(note);

  const body = form.querySelector('[data-role="survey-body"]');
  if (body) body.textContent = '';
  setStatus(form, 'ok', '');
  setButtonPhase(form, 'done');
  state.phase = 'done';
  const debug = debugHandle();
  if (debug) debug.surveyPhase = state.phase;
}

/**
 * 模型追问这一段的输入区（每题：问题 + 后端给的 hint + 一个输入框）。
 * 导出是为了让无头用例能拿夹具样例**直接**驱动这一段（夹具模式没有追问后端，走不到这里）。
 */
export function renderFollowupPhase(form, questions) {
  const phase = form.querySelector('[data-role="survey-phase"]');
  const body = form.querySelector('[data-role="survey-body"]');
  if (!phase || !body) return;
  phase.textContent = '';
  phase.hidden = false;
  phase.dataset.state = 'followup';
  const head = document.createElement('div');
  head.className = 'survey__phase-title';
  head.dataset.role = 'survey-phase-title';
  head.textContent = '模型追问（可选）';
  const note = document.createElement('div');
  note.className = 'survey__phase-note';
  note.dataset.role = 'survey-phase-note';
  note.textContent = '这些是模型按你刚才的作答问的；逐题作答或留空都行，只有填过的会被记下。';
  phase.appendChild(head);
  phase.appendChild(note);

  body.textContent = '';
  questions.forEach((item, index) => {
    const row = document.createElement('div');
    row.className = 'survey__followup';
    row.dataset.role = 'survey-followup-item';
    row.dataset.q = item.q;
    row.dataset.index = String(index);

    const label = document.createElement('div');
    label.className = 'survey__label';
    label.dataset.role = 'survey-followup-q';
    label.textContent = `${index + 1}. ${item.q}`;
    row.appendChild(label);

    if (item.hint) {
      const hint = document.createElement('div');
      hint.className = 'survey__hint';
      hint.dataset.role = 'survey-followup-hint';
      hint.textContent = `提示：${item.hint}`;
      row.appendChild(hint);
    }

    const input = document.createElement('textarea');
    input.rows = 2;
    input.className = 'survey__text';
    input.dataset.role = 'survey-followup-answer';
    input.setAttribute('aria-label', `追问 ${index + 1}：${item.q}`);
    row.appendChild(input);
    body.appendChild(row);
  });

  setStatus(form, 'ok', '');
  setButtonPhase(form, 'followup');
  state.phase = 'followup';
  const debug = debugHandle();
  if (debug) {
    debug.surveyPhase = state.phase;
    debug.surveyFollowupQuestions = questions.map((q) => q.q);
  }
}

function followupQuestions(payload) {
  const list = Array.isArray(payload && payload.questions) ? payload.questions : [];
  return list
    .map((item) => ({
      q: String((item && item.q) || '').trim(),
      hint: String((item && item.hint) || '').trim(),
    }))
    .filter((item) => item.q);
}

/** 降级 / 失败时的可见文案（`degraded` 可能是布尔，也可能是字符串数组）。 */
function followupDegradeText(payload) {
  const raw = payload ? payload.degraded : null;
  if (Array.isArray(raw)) return raw.filter(Boolean).join('；');
  if (typeof raw === 'string') return raw;
  if (raw === true) return String((payload && payload.text) || '').trim() || '模型这次没能给出追问。';
  return '';
}

/**
 * 点「让模型追问几个细节（可选）」：调 `POST /api/survey/followup`（空请求体）。
 *
 * 失败 / 未配置 / 超时都是 200 + `degraded`：**固定题答案不受影响**，这里只把说明写出来，
 * 读者可以「完成」离开（这一步整场可跳过）。
 */
function requestFollowups() {
  const form = state.form;
  if (!form || !state.open) return;
  setStatus(form, 'loading', '正在让模型读你的作答并追问…');
  emit('followup-request', { backgroundAnswers: state.answers || {} });

  const done = (payload, url, source) => {
    const questions = followupQuestions(payload);
    const degradeText = followupDegradeText(payload);
    state.followupPayload = payload;
    const debug = debugHandle();
    if (debug) debug.surveyFollowup = payload || null;
    if (questions.length) {
      renderFollowupPhase(form, questions);
      setStatus(form, 'ok', `模型追问了 ${questions.length} 个细节（可选作答）。`);
      emit('followup-done', {
        questions: questions.map((q) => q.q),
        degraded: false,
        source,
        url: url || null,
        promptVersion: payload.prompt_version || null,
      });
      if (source === 'fixture') {
        note('夹具模式不保存：这些追问来自静态样例 samples/<id>/survey-followup.json，'
          + '答案不会写进任何画像文件。', 'survey-fixture');
      }
      return;
    }
    // 没有题目：把原因说清楚（模型未配置 / 调用失败 / 空回复都走这里）
    const copy = `这次没能生成追问：${degradeText || '模型没有返回可用的问题'}`;
    setStatus(form, 'degraded', copy);
    degrade(copy, 'survey-followup');
    emit('followup-done', {
      questions: [], degraded: true, source, url: url || null,
      reason: (payload && payload.reason) || null,
    });
  };

  if (state.mode === 'fixture') {
    loadFixtureFollowups(state.ctx)
      .then(({ payload, url }) => done(payload, url, 'fixture'))
      .catch((err) => {
        const copy = `没能读到夹具追问（${err && err.message ? err.message : err}）：`
          + '夹具模式下这一步读 samples/<id>/survey-followup.json；固定题的背景不受影响，可以「完成」离开。';
        setStatus(form, 'degraded', copy);
        degrade(copy, 'survey-followup');
        emit('followup-error', { message: copy });
      });
    return;
  }

  requestSurveyFollowups(state.ctx, {})
    .then(({ payload, url }) => done(payload, url, 'api'))
    .catch((err) => {
      const copy = `追问请求失败：${err && err.message ? err.message : err}。`
        + '固定题的背景已经保存好了，不受影响；可以「完成」离开，或再点一次重试。';
      setStatus(form, 'error', copy);
      degrade(copy, 'survey-followup');
      emit('followup-error', { message: copy });
    });
}

/**
 * 追问阶段收尾：有作答就先 `POST /api/survey` 带上 `{followups: [{q, a}]}` 落盘。
 *
 * 端点归属（2026-10-08 上级裁决）：`POST /api/survey/followup` 只**生成**追问问题；
 * 保存读者的追问答案属于「写入画像」，归 `POST /api/survey`（契约 §8 的 `followups` 键）。
 *
 * ⚠ 追问答案**不进八个作答槽位**（进了就是 400），只走 `followups` 这一个键、也不带 `version`；
 * `at` 由后端补，前端不造时间戳。只给 `followups` 不给 `answers` 是合法的，后端在已有背景上补，
 * 不会覆盖已经保存好的固定题作答。
 * 保存失败时**不关卡片**（读者刚敲的字不该被吞掉），写完错误让人重试或直接离开。
 */
function finishFollowups() {
  const form = state.form;
  if (!form || !state.open) return;
  const answers = collectFollowupAnswers(form);
  const close = (bannerText, eventType, extra = {}) => {
    emit(eventType, { answers, response: null, ...extra });
    banner(bannerText, 'warn');
    closeSurvey('followups-done');
  };

  if (state.mode === 'fixture') {
    close('夹具模式不保存：追问的答案没有写进任何画像文件（固定题的背景也不落盘）。',
      'followups-done', { saved: false, fixture: true });
    return;
  }
  if (!answers.length) {
    close('追问没有作答：已收尾，没有写盘（固定题的背景已保存）。', 'followups-done', { saved: false });
    return;
  }

  setStatus(form, 'loading', '正在保存追问的答案…');
  emit('followups-save-request', { answers });
  submitSurvey(state.ctx, { followups: answers })
    .then(({ payload }) => {
      const degradeText = followupDegradeText(payload);
      if (degradeText) {
        degrade(`追问的答案已提交，但后端有降级说明：${degradeText}`, 'survey-followup');
      }
      close(`追问的 ${answers.length} 条作答已记进画像（改写下次重跑就会带上）。`,
        'followups-done', { saved: true, response: payload });
    })
    .catch((err) => {
      const copy = `追问的答案没能保存：${err && err.message ? err.message : err}。`
        + '答案还在卡片里，可以重试「记下追问的答案」，或点「不用了」直接离开（不影响固定题的背景）。';
      setStatus(form, 'error', copy);
      degrade(copy, 'survey-followup');
      emit('followups-save-error', { answers, message: copy });
    });
}

function onClick(event) {
  const role = event.target && event.target.dataset ? event.target.dataset.role : null;
  if (role === 'survey-close') { event.stopPropagation(); requestSkip('close'); return; }
  if (role === 'survey-skip') { event.stopPropagation(); requestSkip('skip-button'); return; }
  if (role === 'survey-submit') { event.stopPropagation(); requestSubmit(); return; }
  if (role === 'survey-followup-start') { event.stopPropagation(); requestFollowups(); return; }
  if (role === 'survey-followup-save') { event.stopPropagation(); finishFollowups(); return; }
  if (role === 'survey-followup-skip') {
    event.stopPropagation();
    emit('followups-skipped', { reason: 'button' });
    closeSurvey('followups-skipped');
    return;
  }
  if (role === 'survey-finish') {
    event.stopPropagation();
    emit('followups-skipped', { reason: 'finish' });
    closeSurvey('followups-finished');
    return;
  }
  // 卡片内部的点击不许冒泡出去（层本身 pointer-events: none，正文照常收得到点击）
  event.stopPropagation();
}

/**
 * `Esc` 的**唯一**入口（文档级）。契约 §9 要求「`Esc` 与『跳过』同效」，而
 * **读者点一下正文就能把焦点带走**（`#rewritten` 是 `tabindex="-1"` 的可聚焦元素，那是设计上允许的），
 * 此时按键事件根本不经过问卷卡片——所以这个监听必须挂在 `document` 上，不能挂在卡片上。
 * （2026-10-08 复核抓到的真实缺口：卡片级 keydown + main.js 在 `isSurveyOpen()` 时让路 ⇒
 * 焦点在正文时按 `Esc` 完全没反应。）
 *
 * **复用同一条路径**，不另写一份：问题阶段直接调「跳过」的 `requestSkip('escape')`；
 * 提交完成 / 追问阶段与那一步的主按钮同效（收尾离开）。
 * 问卷没开时这里什么都不做，把 `Esc` 让给 main.js（它要关讲解卡片 / 设置面板）。
 */
function handleEscapeKey(event) {
  if (event.key !== 'Escape' || !state.open) return;
  event.preventDefault();
  if (state.phase === 'questions') {
    requestSkip('escape');
    return;
  }
  emit('followups-skipped', { reason: 'escape', phase: state.phase });
  closeSurvey(`followups-${state.phase}-escape`);
}

// 模块只求值一次，所以这两条注册天然幂等（不随问卷开关反复挂/卸）
if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
  document.addEventListener('keydown', handleEscapeKey);
  // 滚轮必须走**捕获**且在文档上：卡片自己是 `pointer-events: none`，
  // 挂在卡片上永远收不到滚轮（见 handleWheel）；`passive: false` 是硬要求，
  // 否则 `preventDefault()` 拦不住浏览器对正文的滚动。
  document.addEventListener('wheel', handleWheel, { capture: true, passive: false });
}

/* --------------------------------------------------------------- 启动 */

/** 从 URL 读出问卷相关的两个开关：
 *   - `?survey=1`：显式打开问卷（夹具模式默认不弹，正式模式也有用）；
 *   - `?survey=fixture`：夹具模式连**响应形状**一起夹具化，读
 *     `samples/<id>/survey.json`（版本提示与答案回填都按它走），见 api.js 的 loadFixtureResponse。 */
export function readSurveyContext(search) {
  const params = new URLSearchParams(search || '');
  const survey = params.get(SURVEY_PARAM);
  return {
    asked: survey === '1' || survey === 'fixture',
    askedFixtureResponse: survey === 'fixture',
    fixture: Boolean(params.get('fixture')),
    mode: params.get('fixture') ? 'fixture' : 'api',
  };
}

/**
 * 版本口径（2026-10-08 后端裁定）：「问卷已更新」的判定是
 * `answered_version !== null && answered_version !== version`。
 * 两个值都取 `GET /api/survey` 的**顶层**（`version` / `answered_version`），
 * **不从浏览器本地存储读任何版本号**——localStorage 里只有显示设置那一个键。
 */
function versionNote(payload) {
  const rawAnswered = payload ? payload.answered_version : null;
  if (rawAnswered === null || rawAnswered === undefined || rawAnswered === '') return '';
  const current = Number(payload && payload.version);
  const answered = Number(rawAnswered);
  if (!Number.isFinite(current) || !Number.isFinite(answered) || answered === current) return '';
  return `问卷已更新：你上次作答时是 v${answered}，当前是 v${current}。`
    + '可以重新填一份覆盖它；不填也不会丢旧答案，只是改写仍按旧版本作答理解你。';
}

/** 记一次「题目从哪来」（`__arDebug`：无头验证与排查都要看得见来源）。 */
function setQuestionSource(source, questions, version) {
  const debug = debugHandle();
  if (debug) {
    debug.surveyQuestionSource = source;   // api | fixture | api-fallback-fixture
    debug.surveyQuestionsRaw = questions.map((q) => ({
      qid: String(q.qid || q.id || ''),
      field: String(q.field || q.answer_slot || ''),
    }));
  }
  state.questionSource = source;
}

/** 版本不一致时的可见提示（挂横幅 + 记 `__arDebug.degradations`）。 */
function noteVersion(payload) {
  const text = versionNote(payload);
  if (text) note(text, 'survey-version');
}

/**
 * 启动：决定要不要自动弹问卷。
 *
 * 「首次打开」的判定**只看服务端画像**（`GET /api/survey` 的 `answers` / `skipped`）：
 *   - 已有答案 → 不弹（`reason: already-answered`）；
 *   - 已跳过 → 不弹（`reason: skipped-before`）；
 *   - 夹具模式 → 默认不弹（没后端可比）；给了 `?survey=1` 才打开。
 * 绝不复用 `localStorage["ar.settings.v1"]`（那是阅读显示设置的键，与画像无关）。
 *
 * 题目来源只有一个：正式模式是 `GET /api/survey` 的 `questions[]`，
 * 夹具模式是 `samples/<id>/survey-questions.json`（后端 `QUESTIONS` 导出的同一份数据）。
 * 后端回了空题目时退到那份夹具文件，并在页面上写明「这次用的是夹具题目」——不静默。
 *
 * 本函数**不阻塞正文**：调用方在正文渲染完成、`__arDebug.ready` 置位之后调用，
 * 打开失败（如拿不到题目）只留可见横幅，不影响阅读。
 *
 * @param {{ctx:object, banner?:Function, degrade?:Function}} opts
 *        `banner(text, kind)` 挂横幅；`degrade(kind, message)` 记 `__arDebug.degradations`。
 * @returns {Promise<{opened:boolean, reason:string, actions?:object}>}
 */
export async function bootSurvey({ ctx, banner: bannerFn, degrade: degradeFn } = {}) {
  state.ctx = ctx || null;
  state.bannerFn = typeof bannerFn === 'function' ? bannerFn : null;
  state.degradeFn = typeof degradeFn === 'function' ? degradeFn : null;
  const url = readSurveyContext(globalThis.location ? globalThis.location.search : '');
  const fixture = Boolean(ctx && ctx.mode === 'fixture');
  state.mode = fixture ? 'fixture' : 'api';

  const openWith = (sourceQuestions, source, version, answers, reason) => {
    setQuestionSource(source, sourceQuestions, version);
    openSurvey({ reason, fixture, questions: sourceQuestions, version, answers });
    return { opened: true, reason };
  };

  if (fixture) {
    if (!url.asked) return { opened: false, reason: 'fixture-mode' };
    try {
      const { questions, version, url: asked } = await loadFixtureQuestions(ctx);
      // 只有 `?survey=fixture` 才读「响应形状」样例（读不到就是没有：不打任何请求）
      const response = url.askedFixtureResponse ? await loadFixtureResponse(ctx) : null;
      const payload = response ? (response.payload || {}) : null;
      const debug = debugHandle();
      if (debug) {
        debug.surveyUrl = (response && response.url) || asked || null;
        debug.surveyFixtureResponse = payload;
      }
      const answers = payload && typeof payload.answers === 'object' ? payload.answers : null;
      if (payload) noteVersion(payload);
      const chosenVersion = payload && Number.isFinite(Number(payload.version))
        ? Number(payload.version)
        : version;
      return openWith(questions, payload ? 'fixture-response' : 'fixture',
        chosenVersion, answers, 'fixture-explicit');
    } catch (err) {
      note(`拿不到夹具问卷题目（${err && err.message ? err.message : err}）：`
        + '夹具模式下问卷界面需要 samples/<id>/survey-questions.json（由后端的题目定义导出）；'
        + '本次不弹问卷，正文照常可读。', 'survey-load');
      return { opened: false, reason: 'load-failed' };
    }
  }

  try {
    const { payload, url: asked } = await fetchSurvey(ctx);
    const raw = payload && typeof payload.answers === 'object' && payload.answers ? payload.answers : null;
    // `answers: {}` 与 `answers: null` 一样算「没背景」：非空才算答过（契约 §11 的两条缺省措辞）
    const answers = raw && Object.keys(raw).length ? raw : null;
    const skipped = Boolean(payload && payload.skipped);
    const hasBackground = Boolean(answers || skipped);
    state.answers = answers || {};
    const version = Number.isFinite(Number(payload && payload.version))
      ? Number(payload.version)
      : SURVEY_VERSION;
    const provided = Array.isArray(payload && payload.questions) ? payload.questions : [];
    const debug = debugHandle();
    if (debug) debug.surveyUrl = asked || null;

    noteVersion(payload);

    if (!url.asked && hasBackground) {
      setQuestionSource('api', provided, version);
      return {
        opened: false,
        reason: answers ? 'already-answered' : 'skipped-before',
        actions: { answers, skipped },
      };
    }
    if (provided.length) {
      return openWith(provided, 'api', version, answers,
        url.asked ? 'api-explicit' : 'api-first-visit');
    }
    // 后端回了空题目：退到夹具题目（可见降级），仍让读者能填
    note('后端这次没给题目（questions[] 为空），已改用夹具题目 samples/<id>/survey-questions.json 渲染：'
      + '题目内容与后端版本可能不一致，答案照常按字段名提交。', 'survey-questions-fallback');
    const fallback = await loadFixtureQuestions(ctx);
    return openWith(fallback.questions, 'api-fallback-fixture',
      Number.isFinite(Number(fallback.version)) ? fallback.version : version, answers,
      url.asked ? 'api-explicit' : 'api-first-visit');
  } catch (err) {
    const message = `拿不到背景问卷题目（GET /api/survey 失败：${err && err.message ? err.message : err}）：`
      + '本次不弹问卷，阅读照常；背景相关的能力这次按「未提供」处理。';
    note(message, err instanceof DataError ? 'survey-load' : 'survey-load');
    return { opened: false, reason: 'load-failed' };
  }
}

/**
 * **由按钮显式打开**问卷（2026-10-15 新增，启动页「背景测评」卡片的那颗按钮）。
 *
 * 与 `bootSurvey` 的关系（有意只做最小改造，两边共用同一套题目 / 表单 / 提交路径）：
 *   - `bootSurvey` 仍然是**阅读器**那条路：它自己判定要不要弹（已答 / 已跳过 / 夹具模式都不弹），
 *     本函数**不参与**那条判定，也没有改动它的任何分支；
 *   - 本函数是「读者自己按了按钮」这一档：**无条件打开**（已经答过也照开——按钮写的就是
 *     「填写问卷 / 重填问卷」，重填本来就是它的用途），打开原因记作 `reason`（默认 `launcher`）；
 *   - 题目仍然只有 `GET /api/survey` 这一个来源，本函数**不读夹具题目**（启动页是正式模式页面，
 *     没有 `?fixture=` 上下文）；后端这次没给题目时**不打开空壳**，只留可见降级。
 *   - 提交 / 跳过 / 追问全部走既有路径（`requestSubmit` / `requestSkip` / `requestFollowups`），
 *     所以启动页打开的问卷与 `?survey=1` 打开的是**同一张**：同一套题、同一个写入端点。
 *
 * @param {{ctx:object, banner?:Function, degrade?:Function, reason?:string}} opts
 *        `banner(text, kind)` 挂横幅；`degrade(kind, message)` 记 `__arDebug.degradations`。
 * @returns {Promise<{opened:boolean, reason:string, questions?:number}>}
 */
export async function openSurveyManually({ ctx, banner: bannerFn, degrade: degradeFn, reason = 'launcher' } = {}) {
  state.ctx = ctx || null;
  state.bannerFn = typeof bannerFn === 'function' ? bannerFn : null;
  state.degradeFn = typeof degradeFn === 'function' ? degradeFn : null;
  const fixture = Boolean(ctx && ctx.mode === 'fixture');
  state.mode = fixture ? 'fixture' : 'api';
  if (fixture) {
    // 启动页不该出现这一档（它恒按正式模式取数）；真出现了也要说清楚，不许静默不响应
    note('启动页只支持正式模式（GET /api/survey）：这次没能打开问卷，阅读器里的 ?fixture= 那条路不受影响。',
      'survey-load');
    return { opened: false, reason: 'fixture-mode' };
  }

  try {
    const { payload, url: asked } = await fetchSurvey(ctx);
    const debug = debugHandle();
    if (debug) debug.surveyUrl = asked || null;
    const raw = payload && typeof payload.answers === 'object' && payload.answers ? payload.answers : null;
    // `answers: {}` 与 `answers: null` 一样算「没背景」（与 bootSurvey 同一口径）
    const answers = raw && Object.keys(raw).length ? raw : null;
    const version = Number.isFinite(Number(payload && payload.version))
      ? Number(payload.version)
      : SURVEY_VERSION;
    const provided = Array.isArray(payload && payload.questions) ? payload.questions : [];
    state.answers = answers || {};
    noteVersion(payload);
    setQuestionSource('api', provided, version);
    if (!provided.length) {
      note('后端这次没给问卷题目（questions[] 为空）：问卷打不开（页面上不会出现一张空表单），'
        + '可以稍后再点一次「填写问卷」，或直接在阅读器里用 ?survey=1 打开。', 'survey-questions-empty');
      return { opened: false, reason: 'no-questions' };
    }
    openSurvey({ reason, fixture: false, questions: provided, version, answers });
    return { opened: true, reason, questions: provided.length };
  } catch (err) {
    const message = `拿不到问卷题目（GET /api/survey 失败：${err && err.message ? err.message : err}）：`
      + '这次没能打开问卷；阅读功能不受影响。';
    note(message, 'survey-load');
    return { opened: false, reason: 'load-failed' };
  }
}
