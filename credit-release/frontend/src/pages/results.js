// 验收结果：尝试列表、详情指标与同版 CSV 导出。

import { api, downloadFile } from '../api.js';
import { session } from '../state.js';
import { el, emptyRow, fmtTime, panel, scoreLabel, statusTag, toast } from '../ui.js';

async function fetchAttempts() {
  return api('/acceptance/attempts');
}

function passTag(passed) {
  return passed ? el('span', { class: 'tag ok' }, '通过') : el('span', { class: 'tag bad' }, '不通过');
}

export async function renderAttemptTable({ allowResume = false } = {}) {
  let attempts;
  try {
    attempts = await fetchAttempts();
  } catch (error) {
    return el('p', { class: 'tag bad' }, error.message);
  }

  const rows = attempts.map((item) => {
    const cells = [
      el('td', {}, `#${item.id}`),
      el('td', { class: 'mono' }, item.batch_id),
      el('td', {}, `v${item.threshold_version}`),
      el('td', {}, `第 ${item.attempt_no} 次${item.resume_count ? `（续跑 ${item.resume_count} 次）` : ''}`),
      el('td', {}, statusTag(item.status)),
      el('td', {}, item.result ? passTag(item.result.passed) : '—'),
      el('td', {}, item.publishable ? el('span', { class: 'tag ok' }, '可发布')
                                     : el('span', { class: 'tag neutral' }, '不可发布')),
      el('td', { class: 'nowrap' }, fmtTime(item.finished_at || item.created_at)),
    ];
    const actions = el('td', {}, el('div', { class: 'pill-row' },
      el('button', { class: 'small secondary', onclick: () => showDetail(item.id) }, '详情'),
      item.status === 'complete' && item.publishable
        ? el('button', { class: 'small', onclick: () => exportAttempt(item.id) }, '导出 CSV')
        : null,
      item.status === 'interrupted' && allowResume
        ? el('button', { class: 'small danger', onclick: () => resume(item.id) }, '续跑')
        : null));
    return el('tr', {}, ...cells, actions);
  });

  return el('div', { class: 'table-wrap' }, el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, '尝试'), el('th', {}, '验收批号'), el('th', {}, '阈值版本'),
      el('th', {}, '次数'), el('th', {}, '状态'), el('th', {}, '结论'),
      el('th', {}, '发布资格'), el('th', {}, '时间'), el('th', {}, '操作'))),
    el('tbody', {}, attempts.length ? rows : [emptyRow(9, '暂无验收尝试')])));
}

async function exportAttempt(attemptId) {
  try {
    await downloadFile(`/exports/${attemptId}/export`, `置信分放行验收-尝试${attemptId}.csv`);
    toast('导出已开始', 'success');
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function resume(attemptId) {
  try {
    const result = await api(`/acceptance/${attemptId}/resume`, { body: {} });
    toast(`已按原阈值与数据版本续跑为新尝试 #${result.attempt_id}`, 'success');
    setTimeout(() => location.reload(), 800);
  } catch (error) {
    toast(error.message, 'error');
  }
}

const detailModal = () => document.querySelector('#detail-modal')
  || (() => {
    const node = el('div', { id: 'detail-modal',
      style: 'position:fixed;inset:0;background:rgba(15,23,37,.45);z-index:40;display:grid;place-items:center;padding:24px' });
    node.addEventListener('click', (event) => { if (event.target === node) node.remove(); });
    document.body.append(node);
    return node;
  })();

async function showDetail(attemptId) {
  const modal = detailModal();
  modal.replaceChildren(el('div', { class: 'panel', style: 'width:min(960px,96vw);max-height:88vh;overflow:auto' },
    el('p', { class: 'muted' }, el('span', { class: 'spinner' }), ' 加载验收详情…')));
  try {
    const item = await api(`/acceptance/${attemptId}`);
    const result = item.result;
    const metrics = result ? [
      metric('B 合格放行总数', result.total_b_released_qualified, `A 为 ${result.total_a_released_qualified} 张`),
      metric('B 错放总数', result.total_b_wrong_releases, result.all_within_cap ? '各票种均未超上限' : '存在超限票种'),
      metric('B 较 A 多放行合格票', `+${result.delta_qualified}`,
             result.b_beats_a ? '严格多于 A' : '未严格多于 A'),
      metric('验收结论', result.passed ? '通过' : '不通过', result.reason),
    ] : [];
    modal.replaceChildren(el('div', { class: 'panel', style: 'width:min(960px,96vw);max-height:88vh;overflow:auto' },
      el('h2', {}, `验收尝试 #${item.id} · ${item.batch_id}`),
      el('p', { class: 'hint' },
        `封存阈值 v${item.threshold_version} · 数据指纹 ${item.dataset_checksum} · 状态 ${item.status}`),
      result ? el('div', { class: 'grid cols-4' }, ...metrics) : el('p', { class: 'muted' }, '尚无结果'),
      result ? perTypeTable(result) : null,
      result ? el('div', { class: 'pill-row', style: 'margin-top:14px' },
        item.publishable
          ? el('button', { onclick: () => exportAttempt(item.id) }, '导出同版 CSV')
          : el('span', { class: 'tag bad' }, '基于已作废阈值，旧尝试不得发布'),
        el('button', { class: 'secondary', onclick: () => modal.remove() }, '关闭'))
        : el('div', { class: 'pill-row' }, el('button', { class: 'secondary', onclick: () => modal.remove() }, '关闭'))));
  } catch (error) {
    toast(error.message, 'error');
    modal.remove();
  }
}

function metric(label, value, detail) {
  return el('div', { class: 'metric' },
    el('div', { class: 'label' }, label),
    el('div', { class: 'value' }, String(value)),
    el('div', { class: 'detail' }, detail));
}

function perTypeTable(result) {
  return el('div', { class: 'table-wrap', style: 'margin-top:14px' }, el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, '票种'), el('th', {}, '封存阈值'), el('th', {}, '错放上限'),
      el('th', {}, 'B 合格放行'), el('th', {}, 'B 错放'),
      el('th', {}, 'A 合格放行'), el('th', {}, 'A 错放'),
      el('th', {}, 'B-A 合格差'), el('th', {}, '错放超限'))),
    el('tbody', {}, ...result.per_type.map((row) => el('tr', {},
      el('td', {}, row.ticket_type),
      el('td', {}, scoreLabel(row.threshold)),
      el('td', {}, String(row.cap)),
      el('td', {}, String(row.b_released_qualified)),
      el('td', {}, String(row.b_wrong_releases)),
      el('td', {}, String(row.a_released_qualified)),
      el('td', {}, String(row.a_wrong_releases)),
      el('td', {}, (row.delta_qualified >= 0 ? '+' : '') + row.delta_qualified),
      el('td', {}, row.within_cap ? el('span', { class: 'tag ok' }, '否')
                                  : el('span', { class: 'tag bad' }, '是')))))));
}

export async function resultsPage() {
  const wrap = el('div', {},
    panel('验收结果与交付', '页面展示与 CSV 导出读取同一份封存结果，字段包含阈值、批次、合格放行数与错放数',
      await renderAttemptTable({ allowResume: session.canWrite })));
  return wrap;
}
