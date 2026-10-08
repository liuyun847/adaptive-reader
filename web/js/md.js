/**
 * 极简 Markdown 渲染（零依赖、零构建）。
 *
 * 只处理改写稿里实际会出现的语法，不做通用 Markdown 解析：
 *   块级：标题（#~######）、分隔线（---）、引用（>）、无序/有序列表、表格、普通段落
 *   行内：**粗体**、`代码`
 *
 * 安全：先做 HTML 转义，再套行内标签，因此改写稿里的尖括号不会变成标签。
 */

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;' };

export function escapeHtml(text) {
  return String(text).replace(/[&<>]/g, (c) => ESCAPES[c]);
}

/** 行内格式：先转义，再套 <strong> / <code>。 */
export function inlineHtml(text) {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

const HEADING_RE = /^(#{1,6})\s+(.*)$/;
const RULE_RE = /^\s*-{3,}\s*$/;
const LIST_RE = /^\s*(?:[-*+]|\d+\.)\s+/;
const TABLE_SEP_RE = /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/;

/**
 * 把一个空行切分块拆成语义片段。
 * @returns {Array<{type:'heading'|'rule'|'quote'|'text'|'list'|'table', ...}>}
 */
export function parseBlock(block) {
  const lines = block.split('\n');

  if (lines.length === 1 && RULE_RE.test(lines[0])) return [{ type: 'rule' }];

  const heading = lines[0].match(HEADING_RE);
  if (heading && lines.length === 1) {
    return [{ type: 'heading', level: Math.min(heading[1].length + 1, 6), text: heading[2] }];
  }

  if (lines.every((l) => /^\s*>/.test(l))) {
    return [{ type: 'quote', text: lines.map((l) => l.replace(/^\s*>\s?/, '')).join(' ') }];
  }

  const segments = [];
  let buffer = [];
  const flushText = () => {
    if (buffer.length) {
      segments.push({ type: 'text', text: buffer.join(' ') });
      buffer = [];
    }
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (line.includes('|') && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1])) {
      flushText();
      const rows = [line, lines[i + 1]];
      let j = i + 2;
      while (j < lines.length && lines[j].includes('|')) rows.push(lines[j++]);
      i = j - 1;
      segments.push({ type: 'table', rows });
      continue;
    }
    if (LIST_RE.test(line)) {
      flushText();
      const items = [line];
      while (i + 1 < lines.length && LIST_RE.test(lines[i + 1])) items.push(lines[++i]);
      segments.push({
        type: 'list',
        ordered: /^\s*\d+\./.test(items[0]),
        items: items.map((it) => it.replace(LIST_RE, '')),
      });
      continue;
    }
    buffer.push(line);
  }
  flushText();
  return segments;
}

function splitTableRow(row) {
  return row.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((c) => c.trim());
}

/** 片段 → DOM。 */
export function segmentToElement(segment, doc = document) {
  switch (segment.type) {
    case 'rule': {
      const el = doc.createElement('hr');
      el.className = 'rule';
      return el;
    }
    case 'heading': {
      const el = doc.createElement(`h${segment.level}`);
      el.innerHTML = inlineHtml(segment.text);
      return el;
    }
    case 'quote': {
      const el = doc.createElement('blockquote');
      el.innerHTML = inlineHtml(segment.text);
      return el;
    }
    case 'list': {
      const el = doc.createElement(segment.ordered ? 'ol' : 'ul');
      segment.items.forEach((item) => {
        const li = doc.createElement('li');
        li.innerHTML = inlineHtml(item);
        el.appendChild(li);
      });
      return el;
    }
    case 'table': {
      const table = doc.createElement('table');
      const thead = doc.createElement('thead');
      const tbody = doc.createElement('tbody');
      const rows = segment.rows;
      const headRow = doc.createElement('tr');
      splitTableRow(rows[0]).forEach((cell) => {
        const th = doc.createElement('th');
        th.innerHTML = inlineHtml(cell);
        headRow.appendChild(th);
      });
      thead.appendChild(headRow);
      rows.slice(2).forEach((row) => {
        const tr = doc.createElement('tr');
        splitTableRow(row).forEach((cell) => {
          const td = doc.createElement('td');
          td.innerHTML = inlineHtml(cell);
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      table.appendChild(thead);
      table.appendChild(tbody);
      return table;
    }
    default: {
      const el = doc.createElement('p');
      el.innerHTML = inlineHtml(segment.text);
      return el;
    }
  }
}

/** 把一块 Markdown 渲染进容器。 */
export function renderInto(container, block, doc = document) {
  parseBlock(block).forEach((segment) => container.appendChild(segmentToElement(segment, doc)));
}
