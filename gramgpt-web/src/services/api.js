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

// ── 401 handling: refresh-then-retry, logout только если refresh не отдал новый токен
//
// Раньше на ЛЮБОЙ 401 (даже если access-токен просто истёк, а refresh жив)
// мы стирали оба токена и редиректили на /login — отсюда и было ощущение
// «постоянно выкидывает». Теперь: пробуем /auth/refresh; если он вернул
// новую пару — повторяем оригинальный запрос; если refresh сам отдал ошибку
// — только тогда чистим хранилище и редиректим.

let refreshPromise = null  // singleton чтобы 10 одновременных 401 не порождали 10 refresh-запросов

function clearAuthAndRedirect() {
  localStorage.removeItem('access_token')
  localStorage.removeItem('refresh_token')
  // Не редиректим если мы уже на /login или /register (иначе цикл и стёртая форма)
  const p = window.location.pathname
  if (p !== '/login' && p !== '/register') {
    window.location.href = '/login'
  }
}

api.interceptors.response.use(
  (res) => res,
  async (err) => {
    const status = err.response?.status
    const original = err.config
    const url = original?.url || ''

    // Не лезем в refresh-логику для самих auth-эндпоинтов (login/refresh/logout)
    // — иначе при неверном пароле или невалидном refresh мы бесконечно зациклимся.
    const isAuthEndpoint = url.startsWith('/auth/')
    if (status !== 401 || isAuthEndpoint || original?._retried) {
      return Promise.reject(err)
    }

    const refresh = localStorage.getItem('refresh_token')
    if (!refresh) {
      clearAuthAndRedirect()
      return Promise.reject(err)
    }

    // Один общий refresh на все одновременные 401-ошибки
    if (!refreshPromise) {
      refreshPromise = axios
        .post('/api/v1/auth/refresh', { refresh_token: refresh })
        .then((r) => {
          localStorage.setItem('access_token', r.data.access_token)
          localStorage.setItem('refresh_token', r.data.refresh_token)
          return r.data.access_token
        })
        .catch((e) => {
          // Refresh-токен невалиден/истёк — реально logout
          clearAuthAndRedirect()
          throw e
        })
        .finally(() => { refreshPromise = null })
    }

    try {
      const newAccess = await refreshPromise
      original._retried = true
      original.headers = original.headers || {}
      original.headers.Authorization = `Bearer ${newAccess}`
      return api(original)
    } catch {
      return Promise.reject(err)
    }
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

  refresh: (refreshToken) =>
    api.post('/auth/refresh', { refresh_token: refreshToken }),

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

  create: (phone, apiAppId = null, proxyId = null) =>
    api.post('/accounts/', { phone, api_app_id: apiAppId, proxy_id: proxyId }),

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

  // Bulk: один ZIP для N аккаунтов сразу. На каждый акк ~10 сек
  // (~3 сек ToTDesktop + 5-10 сек anti-flood пауза). 500 акков ≈ 85 мин.
  // Timeout 2 часа с запасом.
  bulkExportTData: (accountIds) =>
    api.post('/accounts/bulk/export-tdata', { account_ids: accountIds }, {
      responseType: 'blob', timeout: 7200000,
    }),

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

  bulkCreate: (text, days = 0) =>
    api.post('/proxies/bulk', { proxies_text: text, duration_days: days }),

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

  terminateSession: (accountId, hash) =>
    api.post(`/security/accounts/${accountId}/terminate-session/${hash}`),

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

  createFull: (formData) =>
    api.post('/channels/create-full', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 120000,
    }),

  pin: (accountId, channelLink) =>
    api.post('/channels/pin', { account_id: accountId, channel_link: channelLink }),

  editInfo: (accountId, channelId, { title = null, description = null } = {}) =>
    api.post('/channels/edit-info', {
      account_id: accountId, channel_id: channelId, title, description,
    }),

  postToChannel: (formData) =>
    api.post('/channels/post', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 60000,
    }),
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
  sendCode: (phone, proxyId = null, apiAppId = null) =>
    api.post('/tg-auth/send-code', { phone, proxy_id: proxyId, api_app_id: apiAppId }),

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
  searchProgress: () => api.get('/parser/search/progress'),
  searchStop: () => api.post('/parser/search/stop'),
  delete: (id) => api.delete(`/parser/channels/${id}`),
  clearAll: () => api.delete('/parser/channels'),
  exportCSV: () => api.get('/parser/export', { responseType: 'blob' }),
  importList: (channels) => api.post('/parser/import', { channels }),
  folders: () => api.get('/parser/folders'),
  folderChannels: (name) => api.get(`/parser/folders/${encodeURIComponent(name)}/channels`),
  setFolder: (channelIds, folder) => api.post('/parser/set-folder', { channel_ids: channelIds, folder }),
  updateChannelFolder: (id, folder) => api.patch(`/parser/channels/${id}/folder`, { folder }),
  similarStart: (data) => api.post('/parser/similar/start', data, { timeout: 30000 }),
  similarProgress: () => api.get('/parser/similar/progress'),
  similarStop: () => api.post('/parser/similar/stop'),
  whitelist: (minRate = 0, sortBy = 'pass_rate') =>
    api.get('/parser/whitelist', { params: { min_rate: minRate, sort_by: sortBy } }),
  whitelistDelete: (id) => api.delete(`/parser/whitelist/${id}`),
  statsOverview: () => api.get('/parser/stats/overview'),
  keywordGeos: () => api.get('/parser/keywords/geos'),
  keywordExpand: (data) => api.post('/parser/keywords/expand', data),
  statsActivity: (days = 7) => api.get('/parser/stats/activity', { params: { days } }),
  statsFloodEvents: (limit = 20) => api.get('/parser/stats/flood-events', { params: { limit } }),
  statsTopSeeds: (limit = 10) => api.get('/parser/stats/top-seeds', { params: { limit } }),
  statsByAccount: () => api.get('/parser/stats/by-account'),
  statsSessions: (limit = 15) => api.get('/parser/stats/sessions', { params: { limit } }),

  // Verify: has_comments проверка
  verifyStart: (data) => api.post('/parser/verify/start', data),
  verifyProgress: () => api.get('/parser/verify/progress'),
  verifyStop: () => api.post('/parser/verify/stop'),

  // Alive: проверка живости (последний пост за N дней)
  aliveStart: (data) => api.post('/parser/alive/start', data),
  aliveProgress: () => api.get('/parser/alive/progress'),
  aliveStop: () => api.post('/parser/alive/stop'),
}

// ── COMMENTING (нейрокомментинг) ─────────────────────────────
export const commentingAPI = {
  list: () =>
    api.get('/commenting/campaigns'),

  scheduleAfterWarmup: (data) =>
    api.post('/commenting/campaigns/schedule-after-warmup', data),

  cancelSchedule: (id) =>
    api.post(`/commenting/campaigns/${id}/cancel-schedule`),

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

  importFromWarmup: (campaignId, batchId) =>
    api.post(`/commenting/campaigns/${campaignId}/import-from-warmup`, { batch_id: batchId }),

  removeChannel: (campaignId, channelId) =>
    api.delete(`/commenting/campaigns/${campaignId}/channels/${channelId}`),

  stats: (id) =>
    api.get(`/commenting/campaigns/${id}/stats`),

  logs: (campaignId = null, limit = 50) =>
    api.get('/commenting/logs', { params: { campaign_id: campaignId, limit } }),

  activity: (id, limit = 50) =>
    api.get(`/commenting/campaigns/${id}/activity`, { params: { limit } }),

  plans: (id, day = null) =>
    api.get(`/commenting/campaigns/${id}/plans`, { params: day ? { day } : {} }),
}

// ── API APPS (Telegram api_id/api_hash) ──────────────────────
export const apiAppsAPI = {
  list: () => api.get('/api-apps'),
  get: (id) => api.get(`/api-apps/${id}`),
  create: (data) => api.post('/api-apps', data),
  update: (id, data) => api.patch(`/api-apps/${id}`, data),
  delete: (id) => api.delete(`/api-apps/${id}`),
  stats: () => api.get('/api-apps/stats/overview'),
}

// ── ACCOUNT MEDIA (фото для сториз) ──────────────────────────
export const accountMediaAPI = {
  list: (accountId) =>
    api.get(`/accounts/${accountId}/media`),

  upload: (accountId, formData) =>
    api.post(`/accounts/${accountId}/media/upload`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 60000,
    }),

  // Возвращает Blob — компонент сам делает URL.createObjectURL для <img src>
  // (нужно потому что эндпоинт защищён JWT, который браузер не шлёт через <img>)
  fetchFile: (accountId, filename) =>
    api.get(`/accounts/${accountId}/media/file/${encodeURIComponent(filename)}`, {
      responseType: 'blob',
    }),

  remove: (accountId, filename) =>
    api.delete(`/accounts/${accountId}/media/file/${encodeURIComponent(filename)}`),

  clear: (accountId) =>
    api.delete(`/accounts/${accountId}/media`),

  // Ручная публикация сториз — для теста Premium + API
  postStoryNow: (accountId, filename = '') =>
    api.post(`/accounts/${accountId}/media/post-story-now`,
      null, { params: filename ? { filename } : {} }),

  // ── Bulk: на несколько аккаунтов сразу ──
  bulkUpload: (accountIds, formData) => {
    // accountIds придёт как поле "account_ids" формы — CSV строкой
    formData.append('account_ids', accountIds.join(','))
    return api.post('/accounts/bulk/media/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 120000,
    })
  },
  bulkClear: (accountIds) =>
    api.post('/accounts/bulk/media/clear', { account_ids: accountIds }),
  bulkCounts: (accountIds) =>
    api.post('/accounts/bulk/media/list', { account_ids: accountIds }),
}

// ── SERVICE CREDENTIALS (Claude / OpenAI / Gemini / Groq / TGStat) ──
export const serviceCredentialsAPI = {
  list:      () => api.get('/service-credentials'),
  providers: () => api.get('/service-credentials/providers'),
  stats:     () => api.get('/service-credentials/stats'),
  create:    (data) => api.post('/service-credentials', data),
  update:    (id, data) => api.patch(`/service-credentials/${id}`, data),
  delete:    (id) => api.delete(`/service-credentials/${id}`),
  test:      (id) => api.post(`/service-credentials/${id}/test`),
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
  deleteBatch: (batchId) => api.delete(`/warmup/batches/${batchId}`),
  taskLogs: (id, limit = 50) => api.get(`/warmup/tasks/${id}/logs`, { params: { limit } }),
  liveLogs: (limit = 30) => api.get('/warmup/logs/live', { params: { limit } }),
  modes: () => api.get('/warmup/modes'),
  pause: (id) => api.post(`/warmup/tasks/${id}/pause`),
  subscribedChannels: (id) => api.get(`/warmup/tasks/${id}/subscribed-channels`),
  batchSubscribedChannels: (batchId) => api.get(`/warmup/batches/${batchId}/subscribed-channels`),
  plan: (id) => api.get(`/warmup/tasks/${id}/plan`),
}

export const subscribeAPI = {
  list: () => api.get('/subscribe/tasks'),
  create: (data) => api.post('/subscribe/tasks', data),
  run: (id) => api.post(`/subscribe/tasks/${id}/run`),
  delete: (id) => api.delete(`/subscribe/tasks/${id}`),
}
// ── DIAGNOSTICS (тест подписки, проверка аккаунта) ───────────
export const diagnosticsAPI = {
  testJoin: (accountId, channelUsername, leaveAfter = false) =>
    api.post('/diagnostics/test-join', {
      account_id: accountId,
      channel_username: channelUsername,
      leave_after: leaveAfter,
    }, { timeout: 60000 }),

  accountChannels: (accountId) =>
    api.get(`/diagnostics/account-channels/${accountId}`, { timeout: 60000 }),
}

export default api