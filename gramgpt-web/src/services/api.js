import axios from 'axios'

// Базовый URL — через vite proxy идёт на localhost:8000
const api = axios.create({
  baseURL: '/api/v1',
  timeout: 15000,
})

// Автоматически добавляем JWT токен в каждый запрос
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// При 401 — очищаем токен и редиректим на логин
api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      localStorage.removeItem('access_token')
      localStorage.removeItem('refresh_token')
      window.location.href = '/login'
    }
    return Promise.reject(err)
  }
)

// ── AUTH ─────────────────────────────────────────────────────
export const authAPI = {
  login: (email, password) =>
    api.post('/auth/login', { email, password }),

  register: (email, password) =>
    api.post('/auth/register', { email, password }),

  me: () =>
    api.get('/auth/me'),

  logout: () =>
    api.post('/auth/logout'),

  changePassword: (old_password, new_password) =>
    api.post('/auth/change-password', { old_password, new_password }),
}

// ── ACCOUNTS ─────────────────────────────────────────────────
export const accountsAPI = {
  list: () =>
    api.get('/accounts/'),

  get: (id) =>
    api.get(`/accounts/${id}`),

  create: (phone) =>
    api.post('/accounts/', { phone }),

  update: (id, data) =>
    api.patch(`/accounts/${id}`, data),

  updateTelegramProfile: (id, data) =>
    api.post(`/accounts/${id}/update-telegram-profile`, data, { timeout: 30000 }),

  setAvatar: (id, file) => {
    const form = new FormData()
    form.append('file', file)
    return api.post(`/accounts/${id}/set-avatar`, form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 30000,
    })
  },

  pinChannel: (id, channelLink) =>
    api.post(`/accounts/${id}/pin-channel`, { channel_link: channelLink }, { timeout: 30000 }),

  downloadSession: (id) =>
    api.get(`/accounts/${id}/download-session`, { responseType: 'blob', timeout: 30000 }),

  exportTData: (id) =>
    api.get(`/accounts/${id}/export-tdata`, { responseType: 'blob', timeout: 60000 }),

  importTData: (file, proxyId = null) => {
    const form = new FormData()
    form.append('file', file)
    if (proxyId) form.append('proxy_id', proxyId)
    return api.post('/accounts/import-tdata', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 60000,
    })
  },

  detectTData: (file) => {
    const form = new FormData()
    form.append('file', file)
    return api.post('/accounts/detect-tdata', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 120000,
    })
  },

  importTDataBatch: (sessionId, accounts) =>
    api.post('/accounts/import-tdata-batch', { session_id: sessionId, accounts }, { timeout: 180000 }),

  delete: (id) =>
    api.delete(`/accounts/${id}`),

  stats: () =>
    api.get('/accounts/stats'),

  filters: () =>
    api.get('/accounts/filters'),

  importJson: () =>
    api.post('/accounts/import-json'),
}

// ── PROXIES ──────────────────────────────────────────────────
export const proxiesAPI = {
  list: () =>
    api.get('/proxies/'),

  create: (data) =>
    api.post('/proxies/', data),

  bulkCreate: (text) =>
    api.post('/proxies/bulk', { proxies_text: text }),

  delete: (id) =>
    api.delete(`/proxies/${id}`),

  update: (id, data) =>
    api.patch(`/proxies/${id}`, data),

  assign: (accountId, proxyId) =>
    api.post('/proxies/assign', { account_id: accountId, proxy_id: proxyId }),

  unassign: (accountId) =>
    api.post('/proxies/assign', { account_id: accountId, proxy_id: null }),

  autoAssign: () =>
    api.post('/proxies/auto-assign'),

  check: (id) =>
    api.post(`/proxies/${id}/check`),

  checkAll: () =>
    api.post('/proxies/check-all'),
}

// ── TASKS ────────────────────────────────────────────────────
export const tasksAPI = {
  checkAccounts: (check_spam = false) =>
    api.post('/tasks/check-accounts', { check_spam }),

  checkProxies: () =>
    api.post('/tasks/check-proxies'),

  getStatus: (taskId) =>
    api.get(`/tasks/${taskId}`),

  cancel: (taskId) =>
    api.delete(`/tasks/${taskId}`),
}

// ── ANALYTICS ────────────────────────────────────────────────
export const analyticsAPI = {
  dashboard: () =>
    api.get('/analytics/dashboard'),

  search: (q) =>
    api.get('/analytics/search', { params: { q } }),
}

// ── SECURITY (сессии, 2FA) ──────────────────────────────────
export const securityAPI = {
  listSessions: (accountId) =>
    api.get(`/security/accounts/${accountId}/sessions`),

  terminateSessions: (accountId) =>
    api.post(`/security/accounts/${accountId}/terminate-sessions`),

  set2FA: (accountId, password, hint = '') =>
    api.post(`/security/accounts/${accountId}/set-2fa`, { password, hint }),

  remove2FA: (accountId) =>
    api.post(`/security/accounts/${accountId}/remove-2fa`),

  reauthorize: (accountId) =>
    api.post(`/security/accounts/${accountId}/reauthorize`),

  exportSession: (accountId) =>
    api.get(`/security/accounts/${accountId}/export-session`),
}

// ── CHANNELS ─────────────────────────────────────────────────
export const channelsAPI = {
  list: (accountId) =>
    api.get(`/channels/accounts/${accountId}`),

  create: (accountId, title, description = '', username = '') =>
    api.post('/channels/create', { account_id: accountId, title, description, username }),

  pin: (accountId, channelLink) =>
    api.post('/channels/pin', { account_id: accountId, channel_link: channelLink }),
}

// ── ACTIONS (быстрые действия) ──────────────────────────────
export const actionsAPI = {
  leaveChats: (accountIds) =>
    api.post('/actions/leave-chats', { account_ids: accountIds }),

  leaveChannels: (accountIds) =>
    api.post('/actions/leave-channels', { account_ids: accountIds }),

  deleteDialogs: (accountIds) =>
    api.post('/actions/delete-dialogs', { account_ids: accountIds }),

  readAll: (accountIds) =>
    api.post('/actions/read-all', { account_ids: accountIds }),

  clearCache: (accountIds) =>
    api.post('/actions/clear-cache', { account_ids: accountIds }),

  unpinFolders: (accountIds) =>
    api.post('/actions/unpin-folders', { account_ids: accountIds }),
}

// ── TG AUTH (веб-авторизация Telegram) ───────────────────────
export const tgAuthAPI = {
  sendCode: (phone, proxyId = null) =>
    api.post('/tg-auth/send-code', { phone, proxy_id: proxyId }),

  confirm: (phone, code) =>
    api.post('/tg-auth/confirm', { phone, code }),

  confirm2FA: (phone, password) =>
    api.post('/tg-auth/confirm-2fa', { phone, password }),
}

// ── IMPORT (TData + Session файлы) ───────────────────────────
export const importAPI = {
  // Загрузка одного .session файла
  uploadSession: (file, phone = '') => {
    const form = new FormData()
    form.append('file', file)
    if (phone) form.append('phone', phone)
    return api.post('/import/session', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 60000,
    })
  },

  // Пакетная загрузка .session файлов
  uploadSessionsBatch: (files) => {
    const form = new FormData()
    files.forEach(f => form.append('files', f))
    return api.post('/import/sessions-batch', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 120000,
    })
  },

  // Загрузка TData архива (ZIP)
  uploadTData: (file, proxyId = null) => {
    const form = new FormData()
    form.append('file', file)
    if (proxyId) form.append('proxy_id', proxyId)
    return api.post('/accounts/import-tdata', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 120000,
    })
  },
}

// ── PARSER ───────────────────────────────────────────────────
export const parserAPI = {
  list: () => api.get('/parser/channels'),
  search: (data) => api.post('/parser/search', data, { timeout: 120000 }),
  delete: (id) => api.delete(`/parser/channels/${id}`),
  clearAll: () => api.delete('/parser/channels'),
  exportCSV: () => api.get('/parser/export', { responseType: 'blob' }),
  importList: (channels) => api.post('/parser/import', { channels }),
  folders: () => api.get('/parser/folders'),
  folderChannels: (name) => api.get(`/parser/folders/${encodeURIComponent(name)}/channels`),
  setFolder: (channelIds, folder) => api.post('/parser/set-folder', { channel_ids: channelIds, folder }),
  updateChannelFolder: (id, folder) => api.patch(`/parser/channels/${id}/folder`, { folder }),
}

// ── COMMENTING (нейрокомментинг) ─────────────────────────────
export const commentingAPI = {
  list: () =>
    api.get('/commenting/campaigns'),

  get: (id) =>
    api.get(`/commenting/campaigns/${id}`),

  create: (data) =>
    api.post('/commenting/campaigns', data),

  update: (id, data) =>
    api.patch(`/commenting/campaigns/${id}`, data),

  delete: (id) =>
    api.delete(`/commenting/campaigns/${id}`),

  start: (id) =>
    api.post(`/commenting/campaigns/${id}/start`),

  pause: (id) =>
    api.post(`/commenting/campaigns/${id}/pause`),

  stop: (id) =>
    api.post(`/commenting/campaigns/${id}/stop`),

  addChannels: (id, channels) =>
    api.post(`/commenting/campaigns/${id}/channels`, { channels }),

  addChannelsFromFolder: async (campaignId, folderName) => {
    const { data } = await api.get(`/parser/folders/${encodeURIComponent(folderName)}/channels`)
    const usernames = data.map(ch => `@${ch.username}`).filter(Boolean)
    if (!usernames.length) return { data: { added: 0 } }
    return api.post(`/commenting/campaigns/${campaignId}/channels`, { channels: usernames })
  },

  removeChannel: (campaignId, channelId) =>
    api.delete(`/commenting/campaigns/${campaignId}/channels/${channelId}`),

  stats: (id) =>
    api.get(`/commenting/campaigns/${id}/stats`),

  logs: (campaignId = null, limit = 50) =>
    api.get('/commenting/logs', { params: { campaign_id: campaignId, limit } }),
}

// ── API APPS (мульти-API ключи) ──────────────────────────────
export const apiAppsAPI = {
  list: () => api.get('/api-apps'),
  get: (id) => api.get(`/api-apps/${id}`),
  create: (data) => api.post('/api-apps', data),
  update: (id, data) => api.patch(`/api-apps/${id}`, data),
  delete: (id) => api.delete(`/api-apps/${id}`),
  stats: () => api.get('/api-apps/stats/overview'),
}

// ── REACTIONS (реакции на посты) ──────────────────────────────
export const reactionsAPI = {
  list: () => api.get('/reactions/tasks'),
  create: (data) => api.post('/reactions/tasks', data),
  run: (id) => api.post(`/reactions/tasks/${id}/run`),
  delete: (id) => api.delete(`/reactions/tasks/${id}`),
  emojis: () => api.get('/reactions/emojis'),
  quick: (data) => api.post('/reactions/quick', data),
}
export const warmupAPI = {
  list: () => api.get('/warmup/tasks'),
  create: (data) => api.post('/warmup/tasks', data),
  start: (id) => api.post(`/warmup/tasks/${id}/start`),
  startAll: () => api.post('/warmup/tasks/start-all'),
  stop: (id) => api.post(`/warmup/tasks/${id}/stop`),
  delete: (id) => api.delete(`/warmup/tasks/${id}`),
  taskLogs: (id, limit = 50) => api.get(`/warmup/tasks/${id}/logs`, { params: { limit } }),
  liveLogs: (limit = 30) => api.get('/warmup/logs/live', { params: { limit } }),
  modes: () => api.get('/warmup/modes'),
  pause: (id) => api.post(`/warmup/tasks/${id}/pause`),
}

export const subscribeAPI = {
  list: () => api.get('/subscribe/tasks'),
  create: (data) => api.post('/subscribe/tasks', data),
  run: (id) => api.post(`/subscribe/tasks/${id}/run`),
  delete: (id) => api.delete(`/subscribe/tasks/${id}`),
}
export default api