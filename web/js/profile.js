/**
 * 读者画像面板（阅读器页顶栏的「画像」入口，2026-10-22）。
 *
 * 为什么要这个文件：自评（`POST /api/profile/rating`）与「事实锚口径」偏好
 * （`POST /api/profile/preference`）后端早就有，但此前只能命令行 / URL / 手改
 * `profiles/<reader>/profile.yaml`。这一层只负责**界面**：列表、当前值、按钮；
 * 取数与写盘全在 `js/main.js` 里接（它管 `__arDebug`、横幅与降级），本文件不碰网络。
 *
 * 界面纪律（与设置面板同一套）：
 *   - 面板挂顶栏，绝对定位；点外部 / Esc / × 关闭；
 *   - 概念只列**当前这份材料**的（concepts.json），不在这一页凭空造概念；
 *   - 夹具模式没有画像后端：控件一律禁用并在面板里写明原因（不静默、不假装保存）；
 *   - 写失败时页面上显示的还是上一次读到的那份画像，**不先把界面改成成功**。
 */

/** 自评的三档（与 `ar.profile.STATES` 同序同值）。 */
export const RATING_STATES = ['known', 'partial', 'unknown'];

/** 三档的中文说法（页面文案的唯一来源）。 */
export const RATING_LABELS = { known: '知道', partial: '知道一点', unknown: '不知道' };

/** 还没自评过的概念（不是后端枚举，只是页面的显示档）。 */
export const STATE_UNRATED = 'unrated';

/** 「事实锚口径」这条偏好的 id（与 `ar.profile.FACT_ANCHOR_PREFERENCE_ID` 一致）。 */
export const FACT_ANCHOR_PREFERENCE_ID = 'pref_fact_anchor';

/** 画像里没有这条偏好时的口径：严（与 `ar.fidelity.DEFAULT_FACT_ANCHOR_POLICY` 一致）。 */
export const FACT_ANCHOR_DEFAULT = 'keep_all';

/** 两档口径的页面文案；`hint` 说清「放行什么、不放行什么」。 */
export const FACT_ANCHOR_OPTIONS = [
  {
    value: 'keep_all',
    label: '全保留（严）',
    hint: '数字 / 引号引文 / 书名号一个都不许少；模型申报了也不放行。',
  },
  {
    value: 'allow_declared_drops',
    label: '要结论（松）',
    hint: '允许模型申报后丢掉纯举例的数据、把引文改成转述；没申报的缺失照旧失败。',
  },
];

/**
 * 从画像里读「事实锚口径」这条偏好。
 * 缺省 / 取值不认识都退到严口径——与后端 `resolve_fact_anchor_policy` 同向，
 * 但**页面上要把「写坏了」说出来**（`invalid`），不许悄悄显示成读者选过的严口径。
 */
export function factAnchorOf(profile) {
  const preferences = Array.isArray(profile && profile.preferences) ? profile.preferences : [];
  const entry = preferences.find(
    (item) => item && typeof item === 'object'
      && String(item.id || '').trim() === FACT_ANCHOR_PREFERENCE_ID,
  );
  const raw = entry && typeof entry.value === 'string' ? entry.value.trim() : '';
  const known = FACT_ANCHOR_OPTIONS.some((option) => option.value === raw);
  const statement = entry && typeof entry.statement === 'string' ? entry.statement.trim() : '';
  return {
    id: FACT_ANCHOR_PREFERENCE_ID,
    value: known ? raw : FACT_ANCHOR_DEFAULT,
    explicit: Boolean(entry) && known,
    invalid: Boolean(entry) && !known,
    raw: raw || null,
    statement,
  };
}

/** 画像里某个概念当前的自评档；没自评 / 值不认识 → `unrated`。 */
export function conceptStateOf(profile, conceptId) {
  const concepts = profile && typeof profile.concepts === 'object' ? profile.concepts : null;
  const entry = concepts && conceptId ? concepts[conceptId] : null;
  const value = entry && typeof entry === 'object' ? entry.state : null;
  return RATING_STATES.includes(value) ? value : STATE_UNRATED;
}

function el(doc, tag, className, text) {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** 造画像面板的空壳（结构固定，内容由 `renderProfile` 填）。 */
export function buildProfilePanel(doc = document) {
  const panel = el(doc, 'div', 'profile');
  panel.id = 'profile-panel';           // 顶栏按钮 aria-controls 指向它（replaceWith 后仍保留）
  panel.dataset.role = 'profile-panel';
  panel.dataset.state = 'closed';
  panel.hidden = true;
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', '读者画像');

  const head = el(doc, 'div', 'profile__head');
  head.appendChild(el(doc, 'span', 'profile__title', '读者画像'));
  const close = el(doc, 'button', 'profile__close', '×');
  close.type = 'button';
  close.dataset.role = 'profile-close';
  close.setAttribute('aria-label', '关闭读者画像');
  head.appendChild(close);
  panel.appendChild(head);

  const status = el(doc, 'div', 'profile__status', '');
  status.dataset.role = 'profile-status';
  status.dataset.state = 'closed';
  status.setAttribute('aria-live', 'polite');
  panel.appendChild(status);

  const degraded = el(doc, 'div', 'profile__degraded');
  degraded.dataset.role = 'profile-degraded';
  degraded.hidden = true;
  panel.appendChild(degraded);

  const ratingSection = el(doc, 'section', 'profile__section');
  ratingSection.dataset.role = 'profile-rating-section';
  const ratingLabel = el(doc, 'div', 'profile__label', '概念自评');
  ratingLabel.appendChild(el(doc, 'span', 'profile__note', '（写回画像；下一次 ar rewrite 才会用上）'));
  ratingSection.appendChild(ratingLabel);
  const concepts = el(doc, 'div', 'profile__concepts');
  concepts.dataset.role = 'profile-concepts';
  ratingSection.appendChild(concepts);
  const empty = el(doc, 'div', 'profile__empty', '');
  empty.dataset.role = 'profile-empty';
  empty.hidden = true;
  ratingSection.appendChild(empty);
  panel.appendChild(ratingSection);

  const prefSection = el(doc, 'section', 'profile__section');
  prefSection.dataset.role = 'profile-preference-section';
  const prefLabel = el(doc, 'div', 'profile__label', '事实锚口径');
  prefLabel.appendChild(el(doc, 'span', 'profile__note', '（数字 / 引文 / 书名号允不允许少）'));
  prefSection.appendChild(prefLabel);
  const options = el(doc, 'div', 'profile__options');
  options.dataset.role = 'profile-preferences';
  options.setAttribute('role', 'radiogroup');
  options.setAttribute('aria-label', '事实锚口径');
  prefSection.appendChild(options);
  const prefNote = el(doc, 'div', 'profile__note');
  prefNote.dataset.role = 'profile-preference-note';
  prefSection.appendChild(prefNote);
  panel.appendChild(prefSection);

  const hint = el(doc, 'div', 'profile__hint');
  hint.dataset.role = 'profile-hint';
  hint.textContent = '这里只列当前材料的概念。自评会写成手动结论（overrides），行为信号不会覆盖它；'
    + '事实锚口径按读者画像保存，下一次 ar check / ar rewrite 起按它判。';
  panel.appendChild(hint);

  return panel;
}

/** 开 / 关面板（`aria-expanded` 与 `hidden` 一起改）。 */
export function setProfilePanelOpen(panel, button, open) {
  if (!panel) return;
  panel.hidden = !open;
  panel.dataset.state = open ? 'open' : 'closed';
  if (button) button.setAttribute('aria-expanded', String(Boolean(open)));
}

function syncDisabled(panel, locked) {
  panel.dataset.locked = String(Boolean(locked));
  panel.querySelectorAll('button[data-role="profile-rate"], input[data-role="profile-preference"]')
    .forEach((node) => { node.disabled = Boolean(locked); });
}

/**
 * 把一份「视图模型」画进面板（**唯一**的内容渲染函数）。
 *
 * @param {HTMLElement} panel  `buildProfilePanel` 造出来的那个节点
 * @param {{state:string, statusText:string, mode:'api'|'fixture', locked:boolean,
 *          concepts:Array<{id:string,name:string,state:string}>,
 *          preference?:object, degraded?:string[]}} model
 */
export function renderProfile(panel, model) {
  if (!panel) return;
  const doc = panel.ownerDocument || document;
  const view = model || {};
  const state = view.state || 'closed';
  panel.dataset.state = state;
  panel.dataset.mode = view.mode === 'fixture' ? 'fixture' : 'api';

  const status = panel.querySelector('[data-role="profile-status"]');
  if (status) {
    status.dataset.state = state;
    status.textContent = view.statusText || '';
  }

  const degraded = panel.querySelector('[data-role="profile-degraded"]');
  if (degraded) {
    degraded.textContent = '';
    const notes = Array.isArray(view.degraded)
      ? view.degraded.filter((note) => typeof note === 'string' && note.trim())
      : [];
    notes.forEach((note) => degraded.appendChild(el(doc, 'div', 'profile__degraded-line', note)));
    degraded.hidden = notes.length === 0;
  }

  const conceptsBox = panel.querySelector('[data-role="profile-concepts"]');
  const empty = panel.querySelector('[data-role="profile-empty"]');
  if (conceptsBox) {
    conceptsBox.textContent = '';
    const concepts = Array.isArray(view.concepts) ? view.concepts : [];
    concepts.forEach((concept) => {
      const row = el(doc, 'div', 'profile__concept');
      row.dataset.role = 'profile-concept';
      row.dataset.concept = concept.id;
      row.dataset.state = RATING_STATES.includes(concept.state) ? concept.state : STATE_UNRATED;

      const name = el(doc, 'span', 'profile__concept-name', concept.name || concept.id);
      name.title = concept.id;
      row.appendChild(name);

      const shown = el(doc, 'span', 'profile__concept-state');
      shown.dataset.role = 'profile-concept-state';
      shown.textContent = RATING_LABELS[concept.state] || '未自评';
      row.appendChild(shown);

      const group = el(doc, 'div', 'profile__rating');
      group.setAttribute('role', 'group');
      group.setAttribute('aria-label', `${concept.name || concept.id} 的自评`);
      RATING_STATES.forEach((value) => {
        const button = el(doc, 'button', 'profile__rate', RATING_LABELS[value]);
        button.type = 'button';
        button.dataset.role = 'profile-rate';
        button.dataset.concept = concept.id;
        button.dataset.state = value;
        button.setAttribute('aria-pressed', String(concept.state === value));
        button.title = `自评：${RATING_LABELS[value]}`;
        group.appendChild(button);
      });
      row.appendChild(group);
      conceptsBox.appendChild(row);
    });
    if (empty) {
      const noConcepts = concepts.length === 0;
      const loading = view.state === 'loading';
      empty.hidden = !noConcepts || loading;
      if (noConcepts && !loading) {
        if (view.state === 'error') {
          empty.textContent = '画像没读到：概念自评暂时不可用（原因见上方状态行）。';
        } else if (view.mode === 'fixture') {
          empty.textContent = '这份材料没有概念清单（夹具样例里也没有），没有可自评的概念。';
        } else {
          empty.textContent = '没有取得概念清单（concepts.json 缺失或为空），没有可自评的概念。';
        }
      } else {
        empty.textContent = '';
      }
    }
  }

  const prefBox = panel.querySelector('[data-role="profile-preferences"]');
  const prefNote = panel.querySelector('[data-role="profile-preference-note"]');
  const preference = view.preference || factAnchorOf(null);
  if (prefBox) {
    prefBox.textContent = '';
    FACT_ANCHOR_OPTIONS.forEach((option) => {
      const label = el(doc, 'label', 'profile__option');
      const input = doc.createElement('input');
      input.type = 'radio';
      input.name = 'ar-profile-fact-anchor';
      input.value = option.value;
      input.dataset.role = 'profile-preference';
      input.checked = preference.value === option.value;
      label.appendChild(input);
      const text = el(doc, 'span', 'profile__option-label', option.label);
      text.title = option.hint;
      label.appendChild(text);
      prefBox.appendChild(label);
    });
  }
  if (prefNote) {
    const current = FACT_ANCHOR_OPTIONS.find((option) => option.value === preference.value);
    if (preference.invalid) {
      prefNote.textContent = `画像里的取值 ${preference.raw || '（空）'} 不认识：后端按严口径处理。`
        + `重新选一档会写回一个合法取值。`;
    } else if (!preference.explicit) {
      prefNote.textContent = `画像里还没有这条偏好：当前按严口径（${FACT_ANCHOR_DEFAULT}）处理。`;
    } else {
      prefNote.textContent = preference.statement
        ? `当前：${current.label} —— ${preference.statement}`
        : `当前：${current.label} —— ${current.hint}`;
    }
  }

  syncDisabled(panel, view.locked === true);
}
