// 审计与交付：仅复核主管可看全量审计；任何人可查看尝试并下载可发布报告。

import { api, downloadFile } from '../api.js';
import { el, emptyRow, fmtTime, panel, toast } from '../ui.js';

const EVENT_LABEL = {
  'auth.login': '登录',
  'dataset.imported': '数据批次导入',
  'threshold.sealed': '阈值封存',
  'acceptance.bound': '验收批揭晓绑定',
  'acceptance.queued': '验收尝试排队',
  'acceptance.started': '验收开始',
  'acceptance.resumed': '中断后续跑',
  'acceptance.completed': '验收完成',
  'acceptance.interrupted': '尝试中断',
  'acceptance.failed': '验收失败',
  'acceptance.unpublishable': '旧尝试取消发布资格',
};

export async function auditPage(actor) {
  const wrap = el('div', {});
  if (actor.role !== 'admin') {
    wrap.append(panel('审计与交付', '全量审计仅复核主管可见；你可以在「验收结果」页查看与下载可发布报告。',
      el('p', { class: 'tag warn' }, '当前角色无权查看审计事件')));
    return wrap;
  }

  let events = [];
  try {
    events = await api('/audit?limit=200');
  } catch (error) {
    toast(error.message, 'error');
  }

  wrap.append(
    panel('审计事件流', '封存、揭晓、续跑、取消发布资格等关键动作均留痕，不可修改',
      el('div', { class: 'table-wrap' }, el('table', {},
        el('thead', {}, el('tr', {},
          el('th', {}, '时间'), el('th', {}, '操作人'), el('th', {}, '事件'),
          el('th', {}, '对象'), el('th', {}, '详情'))),
        el('tbody', {}, events.length ? events.map((event) => el('tr', {},
          el('td', { class: 'nowrap' }, fmtTime(event.created_at)),
          el('td', {}, event.actor),
          el('td', {}, el('span', { class: 'tag info' }, EVENT_LABEL[event.event] || event.event)),
          el('td', {}, event.target || '—'),
          el('td', { class: 'mono' }, event.info ? JSON.stringify(event.info) : '—')))
          : [emptyRow(5, '暂无审计事件')])))),
    panel('交付口径说明', '页面与导出同版列出阈值、批次、合格放行数和错放数',
      el('ul', { class: 'rule-list' },
        el('li', {}, 'CSV 导出仅允许 publishable=1 的已完成尝试，基于已作废封存阈值的旧尝试返回 409。'),
        el('li', {}, '导出内容直接读取验收完成时封存的 result_json 与数据指纹，不重新计算。'),
        el('li', {}, '中断尝试本身不可导出；续跑产生的新尝试沿用原阈值和数据版本，完成后按当时发布资格决定。'),
        el('li', {}, '调参更正后，已揭晓验收批拒绝再次发起；需导入未揭晓的新验收批绑定新版本。'))));
  return wrap;
}
