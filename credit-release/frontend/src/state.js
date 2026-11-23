// 会话状态与导航常量。

import { api, clearToken, getToken, setToken } from './api.js';

export const PAGES = [
  { key: 'overview', label: '验收总览' },
  { key: 'datasets', label: '数据批次' },
  { key: 'threshold', label: '阈值封存' },
  { key: 'acceptance', label: '运行验收' },
  { key: 'results', label: '验收结果' },
  { key: 'audit', label: '审计与交付' },
];

export const ROLE_LABEL = { admin: '复核主管', analyst: '分析员', viewer: '只读查看' };

let actor = null;

export const session = {
  get actor() { return actor; },
  get isAuthed() { return actor !== null; },
  get canWrite() { return actor?.role === 'admin' || actor?.role === 'analyst'; },
  get canSeal() { return actor?.role === 'admin'; },
  async restore() {
    if (!getToken()) return;
    try {
      actor = await api('/auth/me');
    } catch (error) {
      this.signOut();
    }
  },
  async signIn(username, password) {
    const result = await api('/auth/login', { body: { username, password } });
    setToken(result.token);
    actor = { username: result.username, role: result.role };
  },
  signOut() {
    actor = null;
    clearToken();
  },
};

export function navigate(page) {
  location.hash = `#/${page}`;
}
