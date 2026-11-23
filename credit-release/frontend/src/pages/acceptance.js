// 运行验收：揭晓验收批并绑定当前封存阈值；并发同批只认一套阈值；中断可续跑。

import { api } from '../api.js';
import { session } from '../state.js';
import { el, emptyRow, fmtTime, panel, statusTag, toast } from '../ui.js';
import { renderAttemptTable } from './results.js';

export async function acceptancePage() {
  const wrap = el('div', {});
  const [datasets, sets, batches] = await Promise.all([
    api('/datasets'), api('/thresholds'), api('/acceptance/batches')]);
  const active = sets.find((s) => s.status === 'active');
  const pending = datasets.filter((d) => d.role === 'acceptance' && !d.revealed);
  const boundIds = new Set(batches.map((b) => b.dataset_id));

  const select = el('select', {},
    pending.length
      ? pending.map((d) => el('option', { value: String(d.id) },
          `#${d.id} ${d.batch_id}（${d.row_count} 行，未揭晓）`))
      : [el('option', { value: '' }, '暂无未揭晓的验收批')]);

  const runBtn = el('button', {
    onclick: async () => {
      const datasetId = Number(select.value);
      if (!datasetId) return toast('请选择未揭晓的验收批', 'error');
      runBtn.disabled = true;
      try {
        const result = await api('/acceptance/run', { body: { dataset_id: datasetId } });
        toast(`验收尝试 #${result.attempt_id} 已排队，绑定当前封存阈值`, 'success');
        setTimeout(() => location.reload(), 700);
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        runBtn.disabled = false;
      }
    },
  }, '揭晓并发起验收');
  if (!session.canWrite) runBtn.disabled = true;

  const boundTable = el('div', { class: 'table-wrap' }, el('table', {},
    el('thead', {}, el('tr', {},
      el('th', {}, '验收批号'), el('th', {}, '行数'), el('th', {}, '绑定阈值版本'),
      el('th', {}, '阈值状态'), el('th', {}, '数据指纹'), el('th', {}, '揭晓时间'), el('th', {}, '揭晓人'))),
    el('tbody', {}, batches.length ? batches.map((b) => el('tr', {},
      el('td', { class: 'mono' }, b.batch_id),
      el('td', {}, String(b.row_count)),
      el('td', {}, `v${b.threshold_version}`),
      el('td', {}, statusTag(b.threshold_status)),
      el('td', { class: 'mono' }, b.checksum.slice(0, 16) + '…'),
      el('td', { class: 'nowrap' }, fmtTime(b.bound_at)),
      el('td', {}, b.bound_by))) : [emptyRow(7, '尚无已揭晓验收批')])));

  const attemptsBox = el('div', {});

  async function loadAttempts() {
    // 复用结果页的表格，并挂上续跑按钮
    const table = await renderAttemptTable({ allowResume: session.canWrite });
    attemptsBox.replaceChildren(table);
  }

  wrap.append(
    panel('发起验收', active
      ? `当前生效封存阈值为 v${active.version}；验收批揭晓时即与该版本绑定，之后不可换阈值复验`
      : '尚无生效封存阈值，请先在调参批封存',
      active ? el('label', { class: 'field' }, el('span', {}, '选择未揭晓验收批'), select)
             : el('p', { class: 'tag warn' }, '封存阈值后才能验收'),
      active && session.canWrite ? runBtn : null,
      el('p', { class: 'hint' },
        '已揭晓批次若在调参更正后再次发起，将被拒绝；须换一个尚未揭晓的新验收批。')),
    panel('已揭晓验收批与阈值绑定', '绑定关系不可变，确保并发验收同批只认一套封存阈值', boundTable),
    panel('验收尝试（含中断续跑）', '中断后续跑沿用原阈值和数据版本；被取代阈值的旧尝试标记为不可发布',
          attemptsBox));

  await loadAttempts();
  // 轻量轮询，便于观察运行态
  const timer = setInterval(() => {
    if (!document.body.contains(wrap)) return clearInterval(timer);
    loadAttempts().catch(() => clearInterval(timer));
  }, 2500);
  return wrap;
}
