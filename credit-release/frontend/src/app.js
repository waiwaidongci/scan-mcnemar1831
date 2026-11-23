// 应用入口：哈希路由、导航与登录态渲染。

import { api } from './api.js';
import { PAGES, session, navigate } from './state.js';
import { el, toast } from './ui.js';
import { loginPage } from './pages/login.js';
import { overviewPage } from './pages/overview.js';
import { datasetsPage } from './pages/datasets.js';
import { thresholdPage } from './pages/threshold.js';
import { acceptancePage } from './pages/acceptance.js';
import { resultsPage } from './pages/results.js';
import { auditPage } from './pages/audit.js';
import { ROLE_LABEL } from './state.js';

const view = document.querySelector('#view');

function renderChrome() {
  const nav = document.querySelector('#nav');
  nav.replaceChildren(...PAGES.map((page) => el('a', {
    href: `#/${page.key}`,
    class: currentPage() === page.key ? 'active' : '',
  }, page.label)));

  const box = document.querySelector('#session-box');
  if (session.isAuthed) {
    box.replaceChildren(
      el('span', { class: `badge ${session.actor.role}` },
        `${session.actor.username} · ${ROLE_LABEL[session.actor.role] || session.actor.role}`),
      el('button', { class: 'small secondary', onclick: () => {
        session.signOut();
        navigate('overview');
      } }, '退出'));
  } else {
    box.replaceChildren(el('span', { class: 'badge' }, '未登录'));
  }
}

function currentPage() {
  const key = location.hash.slice(2).split('/')[0];
  return PAGES.some((p) => p.key === key) ? key : 'overview';
}

async function render() {
  renderChrome();
  if (!session.isAuthed) {
    view.replaceChildren(loginPage());
    return;
  }
  const page = currentPage();
  view.replaceChildren(el('p', { class: 'muted' }, el('span', { class: 'spinner' }), ' 加载中…'));
  try {
    let node;
    switch (page) {
      case 'datasets': node = await datasetsPage(); break;
      case 'threshold': node = await thresholdPage(); break;
      case 'acceptance': node = await acceptancePage(); break;
      case 'results': node = await resultsPage(); break;
      case 'audit': node = await auditPage(session.actor); break;
      default: node = await overviewPage();
    }
    view.replaceChildren(node);
  } catch (error) {
    if (error.status === 401) {
      session.signOut();
      view.replaceChildren(loginPage());
      toast('登录已过期，请重新登录', 'error');
    } else {
      view.replaceChildren(el('div', { class: 'panel' },
        el('h2', {}, '页面加载失败'), el('p', { class: 'tag bad' }, error.message)));
    }
  }
}

window.addEventListener('hashchange', render);

(async function boot() {
  await session.restore();
  if (!location.hash) location.hash = '#/overview';
  await render();
})();
