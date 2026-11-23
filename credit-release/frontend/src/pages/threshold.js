// 阈值封存：选择调参批与各票种错放上限，预览候选阈值后由复核主管封存。

import { api } from '../api.js';
import { session } from '../state.js';
import { el, emptyRow, fmtTime, panel, scoreLabel, statusTag, toast } from '../ui.js';

function capInputs(types, initial = 2) {
  const inputs = {};
  const wrap = el('div', { class: 'grid cols-2' });
  for (const ticketType of types) {
    const input = el('input', { type: 'number', min: '0', step: '1', value: String(initial) });
    inputs[ticketType] = input;
    wrap.append(el('label', { class: 'field' },
      el('span', {}, `${ticketType} · 错放上限（张）`), input));
  }
  return { wrap, inputs };
}

function candidateTable(entries) {
  return el('div', { class: 'table-wrap' }, el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, '候选阈值'), el('th', {}, '放行总数'),
      el('th', {}, '合格放行数'), el('th', {}, '错放数'),
      el('th', {}, '是否满足上限'), el('th', {}, '入选'))),
    el('tbody', {}, ...entries.map((candidate) => el('tr', {},
      el('td', {}, candidate.threshold_label),
      el('td', {}, String(candidate.released)),
      el('td', {}, String(candidate.released_qualified)),
      el('td', {}, String(candidate.wrong_releases)),
      el('td', {}, candidate.feasible ? el('span', { class: 'tag ok' }, '满足')
                                      : el('span', { class: 'tag bad' }, '超限')),
      el('td', {}, candidate.selected ? el('span', { class: 'tag info' }, '✓ 选中') : '—'))))));
}

export async function thresholdPage() {
  const wrap = el('div', {});
  const datasets = await api('/datasets');
  const tuning = datasets.filter((d) => d.role === 'tuning');
  const spec = await api('/spec');
  const types = spec.ticket_types;

  const select = el('select', {},
    tuning.length ? tuning.map((d) => el('option', { value: String(d.id) },
      `#${d.id} ${d.batch_id}（${d.row_count} 行）`))
      : [el('option', { value: '' }, '请先在数据批次页导入调参批')]);
  const { wrap: capWrap, inputs } = capInputs(types);
  const previewBox = el('div', {});
  const historyBox = el('div', {});

  async function runPreview() {
    const datasetId = Number(select.value);
    if (!datasetId) return;
    const caps = Object.fromEntries(types.map((t) => [t, Number(inputs[t].value)]));
    previewBox.replaceChildren(el('p', { class: 'muted' }, el('span', { class: 'spinner' }), ' 计算候选阈值…'));
    try {
      const data = await api('/thresholds/preview', { body: { dataset_id: datasetId, caps } });
      previewBox.replaceChildren(
        ...types.map((ticketType) => panel(`${ticketType} · 候选阈值权衡`,
          '规则：错放不超上限的阈值中，合格放行数最大者入选；放行数相同取更严阈值',
          candidateTable(data.preview[ticketType]),
          el('p', { class: 'hint' },
            `拟封存：${data.chosen[ticketType].threshold === null
              ? '不放行（该票种无可行阈值）'
              : `阈值 ${scoreLabel(data.chosen[ticketType].threshold)}`}，`
            + `调参合格放行 ${data.chosen[ticketType].tuning_released_qualified} 张，`
            + `错放 ${data.chosen[ticketType].tuning_wrong_releases} 张。`))));
    } catch (error) {
      previewBox.replaceChildren();
      toast(error.message, 'error');
    }
  }

  const previewBtn = el('button', { class: 'secondary', onclick: runPreview }, '预览阈值选择');
  const sealBtn = el('button', {
    onclick: async () => {
      const datasetId = Number(select.value);
      if (!datasetId) return toast('请选择调参批', 'error');
      const caps = Object.fromEntries(types.map((t) => [t, Number(inputs[t].value)]));
      if (!confirm('封存后该版本不可修改；再次封存会作废旧版本并使旧验收尝试不可发布。确认封存？')) return;
      try {
        const result = await api('/thresholds/seal', { body: { dataset_id: datasetId, caps } });
        toast(`已封存 v${result.version} 阈值`, 'success');
        await loadHistory();
      } catch (error) {
        toast(error.message, 'error');
      }
    },
  }, '封存当前阈值（仅复核主管）');
  if (!session.canSeal) {
    sealBtn.disabled = true;
    sealBtn.title = '仅复核主管可封存';
  }

  async function loadHistory() {
    const sets = await api('/thresholds');
    historyBox.replaceChildren(el('div', { class: 'table-wrap' }, el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, '版本'), el('th', {}, '状态'), el('th', {}, '调参批'),
        el('th', {}, '票种阈值'), el('th', {}, '错放上限'), el('th', {}, '封存人'), el('th', {}, '时间'))),
      el('tbody', {}, sets.length ? sets.map((item) => el('tr', {},
        el('td', {}, `v${item.version}`),
        el('td', {}, statusTag(item.status)),
        el('td', { class: 'mono' }, `#${item.dataset_id}`),
        el('td', {}, Object.values(item.sealed)
          .map((s) => `${s.ticket_type}: ${scoreLabel(s.threshold)}`).join('；')),
        el('td', {}, Object.entries(item.caps).map(([t, c]) => `${t} ${c}`).join('；')),
        el('td', {}, item.author),
        el('td', { class: 'nowrap' }, fmtTime(item.created_at))))
        : [emptyRow(7, '尚无封存版本')]))));
  }

  wrap.append(
    panel('选择调参批与错放上限', '阈值只允许在调参批上拟合；封存前验收批无法导入',
      el('label', { class: 'field' }, el('span', {}, '调参批'), select),
      capWrap,
      el('div', { class: 'pill-row' }, previewBtn, session.canWrite ? sealBtn : null)),
    previewBox,
    panel('封存版本历史', '旧版本标记为已作废，基于旧版本完成的尝试不得发布', historyBox));

  if (tuning.length) await runPreview();
  await loadHistory();
  return wrap;
}
