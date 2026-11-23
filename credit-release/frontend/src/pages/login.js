// 登录页。

import { api } from '../api.js';
import { session, navigate } from '../state.js';
import { el, panel, toast } from '../ui.js';

export function loginPage() {
  const username = el('input', { value: 'analyst', autocomplete: 'username' });
  const password = el('input', { type: 'password', value: 'analyst-demo-2611',
                                 autocomplete: 'current-password' });
  const submit = el('button', {
    onclick: async () => {
      submit.disabled = true;
      try {
        await session.signIn(username.value.trim(), password.value);
        toast('登录成功', 'success');
        navigate('overview');
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        submit.disabled = false;
      }
    },
  }, '登录');

  return el('div', { class: 'login-wrap' },
    panel('登录验收台', '演示账号：admin/admin-demo-2611、analyst/analyst-demo-2611、viewer/viewer-demo-2611',
      el('label', { class: 'field' }, el('span', {}, '账号'), username),
      el('label', { class: 'field' }, el('span', {}, '口令'), password),
      el('div', { style: 'margin-top:6px' }, submit),
      el('p', { class: 'hint', style: 'margin-top:14px' },
        '权限：复核主管可封存阈值与查审计；分析员可导入数据、发起验收；只读账号可查看与下载可发布报告。')),
  );
}
