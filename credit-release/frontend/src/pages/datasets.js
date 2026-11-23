// 数据批次：导入调参批/验收批，展示批号票号互斥与封存前限制。

import { api, downloadFile } from '../api.js';
import { session } from '../state.js';
import { el, emptyRow, fmtTime, panel, statusTag, toast } from '../ui.js';

const HEADER = '批号,票号,票种,B 置信分,A 是否放行,人工真值是否合格';

const SAMPLE = [
  HEADER,
  'TUNE-DEMO-1,TUNE-DEMO-1-增-001,增值税发票,96.2,是,是',
  'TUNE-DEMO-1,TUNE-DEMO-1-增-002,增值税发票,88.0,否,是',
  'TUNE-DEMO-1,TUNE-DEMO-1-增-003,增值税发票,71.5,否,否',
  'TUNE-DEMO-1,TUNE-DEMO-1-零-001,零售小票,94.0,是,是',
  'TUNE-DEMO-1,TUNE-DEMO-1-零-002,零售小票,62.0,否,否',
].join('\n');

export async function datasetsPage() {
  const wrap = el('div', {});
  const roleSelect = el('select', {},
    el('option', { value: 'tuning' }, '调参批（用于拟合/封存阈值）'),
    el('option', { value: 'acceptance' }, '验收批（封存后才可导入）'));
  const nameInput = el('input', { placeholder: '例如：11 月调参批' });
  const fileInput = el('input', { type: 'file', accept: '.csv,text/csv' });
  const textArea = el('textarea', { placeholder: '或直接粘贴 UTF-8 CSV 文本……' });
  const preview = el('div', {});

  fileInput.addEventListener('change', async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    textArea.value = await file.text();
    if (!nameInput.value) nameInput.value = file.name.replace(/\.csv$/i, '');
  });

  async function loadList() {
    const rows = await api('/datasets');
    preview.replaceChildren(buildTable(rows));
  }

  function buildTable(rows) {
    return el('div', { class: 'table-wrap' }, el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, 'ID'), el('th', {}, '角色'), el('th', {}, '批号'), el('th', {}, '名称'),
        el('th', {}, '行数'), el('th', {}, '数据指纹 SHA-256'), el('th', {}, '揭晓'),
        el('th', {}, '导入人'), el('th', {}, '导入时间'))),
      el('tbody', {},
        rows.length ? rows.map((row) => el('tr', {},
          el('td', {}, String(row.id)),
          el('td', {}, statusTag(row.role)),
          el('td', { class: 'mono' }, row.batch_id),
          el('td', {}, row.name),
          el('td', {}, String(row.row_count)),
          el('td', { class: 'mono' }, row.checksum.slice(0, 16) + '…'),
          el('td', {}, row.revealed ? statusTag('interrupted') : el('span', { class: 'tag neutral' }, '未揭晓')),
          el('td', {}, row.author),
          el('td', { class: 'nowrap' }, fmtTime(row.created_at))))
          : [emptyRow(9, '暂无数据批次')])));
  }

  const uploadBtn = el('button', {
    onclick: async () => {
      const csvText = textArea.value.trim();
      if (!csvText) return toast('请选择 CSV 文件或粘贴文本', 'error');
      uploadBtn.disabled = true;
      try {
        const result = await api('/datasets', {
          body: {
            role: roleSelect.value,
            name: nameInput.value.trim() || '未命名批次',
            filename: fileInput.files?.[0]?.name || 'paste.csv',
            csv_text: csvText,
          },
        });
        toast(`导入成功：批号 ${result.batch_id}，${result.row_count} 行`, 'success');
        textArea.value = '';
        fileInput.value = '';
        await loadList();
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        uploadBtn.disabled = false;
      }
    },
  }, '导入批次');

  const sampleBtn = el('button', { class: 'secondary', onclick: () => {
    textArea.value = SAMPLE;
    nameInput.value = '样例调参批（请改批号后使用）';
  } }, '填入样例文本');

  const templateBtn = el('button', { class: 'secondary', onclick: async () => {
    try {
      await downloadFile('/template', '置信分放行导入模板.csv');
    } catch (error) {
      toast(error.message, 'error');
    }
  } }, '下载 CSV 模板');

  wrap.append(
    panel('导入数据批次', '字段：批号、票号、票种（增值税发票/零售小票）、B 置信分（0–100）、A 是否放行、人工真值是否合格',
      el('div', { class: 'grid cols-2' },
        el('div', {},
          el('label', { class: 'field' }, el('span', {}, '批次角色'), roleSelect),
          el('label', { class: 'field' }, el('span', {}, '批次名称'), nameInput),
          el('label', { class: 'field' }, el('span', {}, 'CSV 文件'), fileInput)),
        el('div', {},
          el('label', { class: 'field' }, el('span', {}, 'CSV 文本（UTF-8）'), textArea))),
      el('div', { class: 'pill-row' },
        session.canWrite ? uploadBtn : null,
        sampleBtn, templateBtn),
      el('p', { class: 'hint', style: 'margin-top:12px' },
        '互斥校验：调参批与验收批不得共用批号或票号；首版阈值封存前，验收批导入会被拒绝。')),
    panel('已导入批次', '同一文件只允许一个批号，且必须覆盖全部票种', preview));

  try {
    await loadList();
  } catch (error) {
    toast(error.message, 'error');
  }
  return wrap;
}
