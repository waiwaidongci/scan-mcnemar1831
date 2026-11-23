// 极简 DOM 构造与通用小组件。

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function statusTag(status) {
  const map = {
    complete: ['ok', '已完成'], queued: ['neutral', '排队中'], running: ['info', '运行中'],
    failed: ['bad', '失败'], interrupted: ['warn', '已中断'],
    active: ['ok', '生效中'], superseded: ['neutral', '已作废'],
    tuning: ['info', '调参批'], acceptance: ['warn', '验收批'],
  };
  const [kind, label] = map[status] || ['neutral', status];
  return el('span', { class: `tag ${kind}` }, label);
}

export function toast(message, kind = '') {
  const box = document.querySelector('#toast');
  box.textContent = message;
  box.className = `toast ${kind}`;
  box.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { box.hidden = true; }, 3600);
}

export function fmtTime(text) {
  return text ? text.replace('T', ' ').replace('+00:00', ' UTC') : '—';
}

export function scoreLabel(value) {
  return value === null || value === undefined ? '不放行（最严）' : `≥ ${Number(value).toLocaleString()}`;
}

export function panel(title, hint, ...children) {
  return el('section', { class: 'panel' },
    el('h2', {}, title),
    hint ? el('p', { class: 'hint' }, hint) : null,
    ...children);
}

export function emptyRow(cols, text) {
  return el('tr', {}, el('td', { class: 'center muted', colspan: cols }, text));
}
