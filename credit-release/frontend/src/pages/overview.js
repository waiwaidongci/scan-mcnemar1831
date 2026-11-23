// 验收总览：规则口径、封存与验收最新状态。

import { api } from '../api.js';
import { el, fmtTime, panel, scoreLabel, statusTag, toast } from '../ui.js';

function metric(label, value, detail) {
  return el('div', { class: 'metric' },
    el('div', { class: 'label' }, label),
    el('div', { class: 'value' }, String(value)),
    detail ? el('div', { class: 'detail' }, detail) : null);
}

export async function overviewPage() {
  const wrap = el('div', {});
  let spec, datasets, sets, attempts;
  try {
    [spec, datasets, sets, attempts] = await Promise.all([
      api('/spec'), api('/datasets'), api('/thresholds'),
      api('/acceptance/batches').catch(() => []),
    ]);
  } catch (error) {
    toast(error.message, 'error');
    return wrap;
  }

  const active = sets.find((item) => item.status === 'active');
  const latest = attempts?.length ? attempts.slice().sort((a, b) => b.id - a.id)[0] : null;
  const tuningCount = datasets.filter((d) => d.role === 'tuning').length;
  const acctCount = datasets.filter((d) => d.role === 'acceptance').length;

  wrap.append(
    panel('业务口径与当班流程', '财务拟以候选引擎 B 的置信分放行票据，人工真值用于离线调参与验收',
      el('ol', { class: 'rule-list' }, ...spec.rule_summary.map((rule) => el('li', {}, rule)))),

    el('div', { class: 'grid cols-4' },
      metric('调参批数量', tuningCount, '仅调参批可拟合阈值'),
      metric('验收批数量', acctCount, '封存后才能导入'),
      metric('封存阈值版本', active ? `v${active.version}` : '未封存',
             active ? `封存于 ${fmtTime(active.created_at)}` : '先在调参批封存'),
      metric('已揭晓验收批', attempts?.length ?? 0, '同批只认一套阈值')),

    panel('当前生效封存阈值', active
      ? `基于调参批 #${active.dataset_id}，错放上限由复核主管封存`
      : '尚未封存任何阈值，验收批导入入口保持关闭',
      active ? el('div', { class: 'table-wrap' }, (() => {
        const table = el('table', {},
          el('thead', {}, el('tr', {},
            el('th', {}, '票种'), el('th', {}, '封存阈值'), el('th', {}, '错放上限'),
            el('th', {}, '调参合格放行'), el('th', {}, '调参错放'), el('th', {}, '状态'))),
          el('tbody', {}, ...Object.values(active.sealed).map((row) => el('tr', {},
            el('td', {}, row.ticket_type),
            el('td', {}, scoreLabel(row.threshold)),
            el('td', {}, String(row.cap)),
            el('td', {}, String(row.tuning_released_qualified)),
            el('td', {}, String(row.tuning_wrong_releases)),
            el('td', {}, row.blocked ? statusTag('interrupted') : statusTag('active'))))));
        return table;
      })()) : el('p', { class: 'muted' }, '请前往「阈值封存」页面完成首版封存。')),

    panel('最新验收动作', '完成结果与可发布状态在「验收结果」查看',
      latest ? el('dl', { class: 'kv' },
        el('dt', {}, '验收批号'), el('dd', {}, latest.batch_id),
        el('dt', {}, '阈值版本'), el('dd', {}, `v${latest.threshold_version}（${latest.threshold_status}）`),
        el('dt', {}, '绑定时间'), el('dd', {}, fmtTime(latest.bound_at)),
        el('dt', {}, '绑定人'), el('dd', {}, latest.bound_by))
        : el('p', { class: 'muted' }, '暂无已揭晓验收批。')),
  );
  return wrap;
}
