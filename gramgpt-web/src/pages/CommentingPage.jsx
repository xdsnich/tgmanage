import { useEffect, useState, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { commentingAPI, accountsAPI, parserAPI, subscribeAPI, warmupAPI, serviceCredentialsAPI } from '../services/api'
import { Card, Button, Input, Modal, Badge, Spinner, Empty, StatCard } from '../components/ui'
import AccountErrorsPanel from '../components/AccountErrorsPanel'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
import useLocalStorageState from '../hooks/useLocalStorageState'

const TONES = [
  { value: 'positive', label: 'Позитивный', icon: '😊' },
  { value: 'negative', label: 'Критичный', icon: '🤨' },
  { value: 'question', label: 'Вопрос автору', icon: '❓' },
  { value: 'analytical', label: 'Аналитический', icon: '🧐' },
  { value: 'short', label: 'Краткий (2-3 слова)', icon: '⚡' },
  { value: 'custom', label: 'Кастомный промпт', icon: '✍️' },
]

const TRIGGERS = [
  { value: 'all', label: 'Каждый пост' },
  { value: 'random', label: 'Случайный %' },
  { value: 'keywords', label: 'По ключевым словам' },
]

const STATUS_COLORS = {
  draft: 'default', scheduled: 'violet', active: 'green', paused: 'yellow', stopped: 'red', finished: 'blue',
}
const STATUS_LABELS = {
  draft: 'Черновик', scheduled: '📅 Запланирована', active: 'Активна', paused: 'Пауза', stopped: 'Остановлена', finished: 'Завершена',
}

// ── Plan helpers ──────────────────────────────────────────────
const ACTION_ICONS = {
  join_target_channel: '📢', join_channel: '📡',
  join_discussion_of: '💭',
  smart_comment: '💬', read_feed: '📖', set_reaction: '😍',
  react_to_comment: '👍',
  verify_my_comments: '✅',
  view_stories: '👁', view_profile: '👤', search: '🔍',
  send_saved: '💾', forward_saved: '📤', reply_dm: '↩️',
  typing: '⌨️', idle: '⏸', post_story: '📸',
}

const actionLabel = (a) => {
  switch (a.type) {
    case 'join_target_channel': return `Вступает в @${a.channel}`
    case 'join_channel': return `Вступает в @${a.channel || '...'} (фон)`
    case 'join_discussion_of': return `Вступает в discussion @${a.channel}`
    case 'smart_comment': return `Комментирует @${a.channel}`
    case 'read_feed': return `Читает ${a.channel ? `@${a.channel}` : 'ленту'} (${a.count || 5} постов)`
    case 'set_reaction': return `Реакция ${a.emoji || '👍'}${a.channel ? ` в @${a.channel}` : ''}`
    case 'react_to_comment': return `Реакция на коммент${a.channel ? ` в @${a.channel}` : ''} (×${a.count || 1})`
    case 'verify_my_comments': return `Проверяет свои комменты (window=${a.window_hours || 24}ч)`
    case 'view_stories': return `Stories (${a.count || 3})`
    case 'view_profile': return 'Просматривает профиль'
    case 'search': return 'Поиск в Telegram'
    case 'send_saved': return `Saved: "${a.text || '...'}"`
    case 'forward_saved': return 'Форвард в Saved'
    case 'reply_dm': return 'Читает ЛС'
    case 'typing': return `Typing ${a.duration || 5}с`
    case 'idle': return `Пауза ${a.duration || 60}с`
    case 'post_story': return 'Постит Story'
    default: return a.type
  }
}

const actionColor = (a) => {
  if (a.type === 'join_target_channel') return 'var(--blue)'
  if (a.type === 'join_discussion_of') return 'var(--violet)'
  if (a.type === 'smart_comment') return 'var(--green)'
  if (a.type === 'verify_my_comments') return 'var(--text-3)'
  if (a.type === 'set_reaction' || a.type === 'react_to_comment') return '#ff9f43'
  if (a.type === 'idle') return 'var(--text-3)'
  return 'var(--text-2)'
}

const fmtTime = (h, m) => `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`

const fmtDate = (dateStr) => {
  const d = new Date(dateStr)
  return d.toLocaleDateString('ru', { day: 'numeric', month: 'long' })
}

const PERSONALITY_LABELS = {
  lurker: 'Наблюдатель', reactor: 'Реактор',
  active_reader: 'Активный', commenter: 'Комментатор', night_owl: 'Сова',
}
const MOOD_LABELS = {
  lazy: 'Ленивый', tired: 'Уставший', normal: 'Обычный',
  active: 'Активный', hyper: 'Гиперактивный', focused: 'Сфокусированный',
}

export default function CommentingPage() {
  const navigate = useNavigate()
  const [campaigns, setCampaigns] = useState([])
  const [accounts, setAccounts] = useState([])
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useLocalStorageState('commenting_active_tab', 'campaigns')
  const [logs, setLogs] = useState([])
  const [logsLoading, setLogsLoading] = useState(false)
  const [selected, setSelected] = useState(null)
  const [createModal, setCreateModal] = useState(false)
  const [detailModal, setDetailModal] = useState(false)
  const [channelsLoading, setChannelsLoading] = useState(false)
  const [loadedDays, setLoadedDays] = useState(new Set())
  const [channelsShown, setChannelsShown] = useState(100)
  const [channelModal, setChannelModal] = useState(false)
  const [subscribeModal, setSubscribeModal] = useState(false)
  const [editModal, setEditModal] = useState(false)
  const [errorsModal, setErrorsModal] = useState(false)
  const [editForm, setEditForm] = useState({ id: null, name: '', account_ids: [] })
  const [editOnlyAlive, setEditOnlyAlive] = useState(true)
  const [editSaving, setEditSaving] = useState(false)
  const [saving, setSaving] = useState(false)
  const [toast, setToast] = useState(null)

  const openEdit = (c) => {
    setEditForm({
      id: c.id,
      name: c.name || '',
      account_ids: Array.isArray(c.account_ids) ? [...c.account_ids] : [],
      max_hours: c.max_hours ?? 24,
      max_comments: c.max_comments ?? 100,
      delay_join: c.delay_join ?? 10,
      delay_comment: c.delay_comment ?? 250,
      delay_between: c.delay_between ?? 60,
    })
    setEditModal(true)
  }

  const handleEditSave = async () => {
    if (!editForm.id) return
    if (!editForm.name.trim()) { alert('Имя не может быть пустым'); return }
    setEditSaving(true)
    try {
      // Смотрим что изменилось из «влияющих на план» полей
      const orig = campaigns.find(x => x.id === editForm.id)
      const newMaxCom = Number(editForm.max_comments) || 100
      const newMaxHrs = Number(editForm.max_hours) || 24
      const planChanged = orig && (orig.max_comments !== newMaxCom || orig.max_hours !== newMaxHrs
                                   || JSON.stringify((orig.account_ids || []).slice().sort())
                                      !== JSON.stringify((editForm.account_ids || []).slice().sort()))
      const wasActive = orig?.status === 'active'

      await commentingAPI.update(editForm.id, {
        name: editForm.name.trim(),
        account_ids: editForm.account_ids,
        max_hours: newMaxHrs,
        max_comments: newMaxCom,
        delay_join: Number(editForm.delay_join) || 10,
        delay_comment: Number(editForm.delay_comment) || 250,
        delay_between: Number(editForm.delay_between) || 60,
      })

      // Если кампания уже запущена И поменяли max_comments / max_hours / акки —
      // предлагаем пересобрать план чтобы новое кол-во распределилось.
      if (planChanged && wasActive) {
        const ok = confirm(
          `Изменены настройки, влияющие на план (max_comments/max_hours/акки).\n\n` +
          `Пересобрать план кампании? Существующий прогресс сохранится, ` +
          `но кол-во комментов на день будет пересчитано под новое значение.`
        )
        if (ok) {
          try {
            await commentingAPI.rebuildPlan(editForm.id)
            showToast('План пересобран под новые значения ✅')
          } catch (e) {
            alert('Rebuild ошибка: ' + (e.response?.data?.detail || e.message))
          }
        }
      }

      setEditModal(false)
      showToast('Кампания обновлена')
      await load()
    } catch (e) {
      alert('Ошибка: ' + (e.response?.data?.detail || e.message))
    } finally {
      setEditSaving(false)
    }
  }

  // Subscribe
  const [subscribeTasks, setSubscribeTasks] = useState([])
  const [subTime, setSubTime] = useState(240)
  const [subRunning, setSubRunning] = useState(false)

  // Create form
  const [form, setForm] = useState({
    name: '', account_ids: [], trigger_mode: 'all', trigger_percent: 50,
    trigger_keywords: '', llm_provider: 'claude', llm_credential_id: null,
    tone: 'positive',
    custom_prompt: '', comment_length: 'medium',
    max_comments: 100, max_hours: 24,
    delay_join: 10, delay_comment: 250, delay_between: 60,
    campaign_type: 'regular',      // regular | check_only
    notification_chat: '',
  })
  const [onlyAlive, setOnlyAlive] = useState(true)

  // LLM keys, доступные пользователю (для селектора в форме кампании)
  const [llmCreds, setLlmCreds] = useState([])

  const [channelText, setChannelText] = useState('')
  const [channelFolders, setChannelFolders] = useState([])
  const [warmupBatches, setWarmupBatches] = useState([])

  // Activity log
  const [activity, setActivity] = useState([])
  const [activityLoading, setActivityLoading] = useState(false)
  const [detailTab, setDetailTab] = useState('info')
  const [diagnostics, setDiagnostics] = useState(null)
  const [diagLoading, setDiagLoading] = useState(false)

  const loadDiagnostics = async (campaignId) => {
    if (!campaignId) return
    setDiagLoading(true)
    try {
      const { data } = await commentingAPI.diagnostics(campaignId)
      setDiagnostics(data)
    } catch { setDiagnostics(null) }
    setDiagLoading(false)
  }

  // Backfill discussion joins убран — теперь через органичный sprinkle
  // в plan_generator (см. join_discussion_of action).
  const [backfillProgress, setBackfillProgress] = useState(null)
  const backfillPollRef = useRef(null)
  const loadBackfillProgress = async (campaignId) => {
    if (!campaignId) return
    try {
      const { data } = await commentingAPI.backfillDiscussionsProgress(campaignId)
      setBackfillProgress(data.status === 'idle' ? null : data)
      return data.status
    } catch { return 'idle' }
  }
  useEffect(() => {
    if (backfillPollRef.current) clearInterval(backfillPollRef.current)
    if (backfillProgress?.status === 'running' && selected?.id) {
      backfillPollRef.current = setInterval(async () => {
        const st = await loadBackfillProgress(selected.id)
        if (st === 'done' || st === 'idle') {
          clearInterval(backfillPollRef.current)
          loadDiagnostics(selected.id)
        }
      }, 3000)
    }
    return () => { if (backfillPollRef.current) clearInterval(backfillPollRef.current) }
  }, [backfillProgress?.status, selected?.id])
  const handleBackfillDiscussions = async () => {
    if (!selected?.id) return
    if (!window.confirm(
      '💬 Догнать discussion-подписки?\n\n' +
      'Пройдёт по всем уже joined каналам этой кампании и вступит в их ' +
      'linked discussion group где акк ещё не участник. Использует Redis-кеш ' +
      'linked_chat_id — один и тот же канал резолвится ОДИН раз для всех акков.\n\n' +
      'После этого smart_comment на legacy-joined каналы не будет ловить ' +
      '"You join the discussion group before commenting".\n\n' +
      'Продолжить?'
    )) return
    try {
      await commentingAPI.backfillDiscussions(selected.id)
      setBackfillProgress({ status: 'running', campaign_id: selected.id })
      showToast('💬 Backfill запущен — прогресс на баннере')
    } catch (err) {
      showToast(err.response?.data?.detail || 'Не удалось запустить backfill', 'error')
    }
  }

  // Plans
  const [plans, setPlans] = useState([])
  const [plansLoading, setPlansLoading] = useState(false)
  const [openDays, setOpenDays] = useState(new Set())    // Set<dayNumber>
  const [openSessions, setOpenSessions] = useState(new Set())   // Set<"planId-sessIdx">

  const showToast = (text, type = 'success') => {
    setToast({ text, type }); setTimeout(() => setToast(null), 3500)
  }

  const load = async () => {
    setLoading(true)
    try {
      const [c, a, st, llm] = await Promise.all([
        commentingAPI.list(),
        accountsAPI.list(),
        subscribeAPI.list().catch(() => ({ data: [] })),
        serviceCredentialsAPI.list().catch(() => ({ data: [] })),
      ])
      setCampaigns(c.data)
      setAccounts(a.data.filter(acc => acc.status === 'active'))
      setSubscribeTasks(st.data || [])
      setLlmCreds((llm.data || []).filter(k => k.is_active))
    } catch { }
    setLoading(false)
  }

  useEffect(() => { load() }, [])
  useAutoRefresh(async () => {
    const [c, st] = await Promise.all([
      commentingAPI.list(),
      subscribeAPI.list().catch(() => ({ data: [] })),
    ])
    setCampaigns(c.data)
    setSubscribeTasks(st.data || [])
    // ── TIHIY merge selected: НЕ заменяем объект целиком (иначе Modal
    // пересобирается и scroll сбрасывается), а мержим только изменяемые
    // числа/статус.
    if (selected && detailModal) {
      const upd = c.data.find(x => x.id === selected.id)
      if (upd) {
        setSelected(prev => {
          if (!prev) return upd
          // Merge только "живых" полей — остальное оставляем как есть
          const merged = {
            ...prev,
            status: upd.status,
            comments_sent: upd.comments_sent,
            comments_verified: upd.comments_verified,
            channels_count: upd.channels_count,
            accounts_count: upd.accounts_count,
            started_at: upd.started_at,
            finished_at: upd.finished_at,
            max_comments: upd.max_comments,
            max_hours: upd.max_hours,
          }
          // Если ничего не изменилось — вернём prev (React не будет ре-рендерить)
          const changed = Object.keys(merged).some(k => merged[k] !== prev[k])
          return changed ? merged : prev
        })
      }
    }
  }, 30000)   // было 15с — увеличил до 30с

  // ── Auto-refresh для activity/plans/diagnostics — ВЫКЛЮЧЕНЫ ──
  // Раньше эти вкладки авто-обновлялись каждые 10-30с, но это перезаписывало
  // arrays state → React ре-рендерил список → scroll сбрасывался на верх.
  // Теперь только ручная кнопка "🔄 Обновить". Полное «замораживание» UI
  // пока пользователь смотрит.

  useEffect(() => {
    const hasRunning = subscribeTasks.some(st => st.status === 'running')
    if (!hasRunning) return
    const iv = setInterval(async () => {
      try { const { data } = await subscribeAPI.list(); setSubscribeTasks(data || []) } catch { }
    }, 10000)
    return () => clearInterval(iv)
  }, [subscribeTasks])

  const loadLogs = async () => {
    setLogsLoading(true)
    try { const { data } = await commentingAPI.logs(null, 100); setLogs(data) } catch { }
    setLogsLoading(false)
  }

  const loadActivity = async (campaignId, silent = false) => {
    if (!silent) setActivityLoading(true)
    try { const { data } = await commentingAPI.activity(campaignId, 50); setActivity(data) }
    catch { if (!silent) setActivity([]) }
    if (!silent) setActivityLoading(false)
  }

  // Сперва тянем только счётчики по дням — расписание сессий это почти
  // весь объём ответа (8 МБ на кампанию в 22 дня), а видно всегда лишь
  // один раскрытый день. Его расписание догружаем в openDay.
  const loadPlans = async (campaignId) => {
    if (!campaignId) return
    setPlansLoading(true)
    try {
      const { data } = await commentingAPI.plans(campaignId, null, false)
      setPlans(data)
      setLoadedDays(new Set())
      // Автоматически открываем первый день
      if (data.length > 0) {
        const days = [...new Set(data.map(p => p.day_number))]
        setOpenDays(new Set([days[0]]))
        loadDay(campaignId, days[0])
      }
    } catch { setPlans([]) }
    setPlansLoading(false)
  }

  // Расписание одного дня: подмешиваем сессии в уже загруженные планы.
  const loadDay = async (campaignId, day) => {
    if (!campaignId || loadedDays.has(day)) return
    setLoadedDays(prev => new Set(prev).add(day))
    try {
      const { data } = await commentingAPI.plans(campaignId, day)
      const byId = new Map(data.map(p => [p.id, p]))
      setPlans(prev => prev.map(p => (
        byId.has(p.id) ? { ...p, sessions: byId.get(p.id).sessions || [] } : p
      )))
    } catch {
      // не вышло — дадим попробовать снова при следующем раскрытии
      setLoadedDays(prev => { const n = new Set(prev); n.delete(day); return n })
    }
  }

  const toggleDay = (day) => setOpenDays(prev => {
    const s = new Set(prev)
    if (s.has(day)) s.delete(day)
    else { s.add(day); if (selected) loadDay(selected.id, day) }
    return s
  })
  const toggleSession = (key) => setOpenSessions(prev => { const s = new Set(prev); s.has(key) ? s.delete(key) : s.add(key); return s })

  const handleCreate = async () => {
    setSaving(true)
    try {
      const payload = {
        ...form,
        trigger_keywords: form.trigger_keywords
          ? form.trigger_keywords.split(',').map(s => s.trim()).filter(Boolean) : [],
      }
      await commentingAPI.create(payload)
      setCreateModal(false); showToast('Кампания создана'); await load()
      setForm({
        name: '', account_ids: [], trigger_mode: 'all', trigger_percent: 50,
        trigger_keywords: '', llm_provider: 'claude', llm_credential_id: null,
        tone: 'positive', custom_prompt: '',
        comment_length: 'medium', max_comments: 100, max_hours: 24,
        delay_join: 10, delay_comment: 250, delay_between: 60,
        campaign_type: 'regular', notification_chat: ''
      })
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setSaving(false)
  }

  const handleAction = async (id, action) => {
    try {
      if (action === 'start') await commentingAPI.start(id)
      else if (action === 'pause') await commentingAPI.pause(id)
      else if (action === 'stop') await commentingAPI.stop(id)
      else if (action === 'restart') {
        if (!window.confirm(
          '🔄 Перезапустить кампанию?\n\n' +
          '• Обнулятся счётчики (comments_sent = 0, finished_at = null)\n' +
          '• Удалятся старые CampaignPlan\n' +
          '• Каналам сбросится last_post_id и comments_sent\n' +
          '• Сгенерируются новые планы под текущие max_hours / max_comments\n\n' +
          'Продолжить?'
        )) return
        const { data } = await commentingAPI.restart(id)
        showToast(data.message || 'Перезапущено')
      }
      else if (action === 'cancel-schedule') {
        if (!window.confirm('Отменить планирование? Кампания вернётся в "Черновик".')) return
        await commentingAPI.cancelSchedule(id)
      }
      else if (action === 'rebuild-plan') {
        if (!window.confirm(
          '🔄 Пересобрать план?\n\n' +
          '• Удалятся незавершённые CampaignPlan (не выполненные сессии)\n' +
          '• Удалятся неотправленные items из очереди (scheduled)\n' +
          '• НЕ ТРОГАЕТСЯ: счётчик comments_sent, история CommentLog, joined каналы\n' +
          '• Новые планы создадутся с ТЕКУЩИМИ правилами:\n' +
          '  - распределение 0-1/2-3/4-6/7\n' +
          '  - night boost 30%\n' +
          '  - реакции 2-12 на комменты\n' +
          '  - per-channel лимит 2\n\n' +
          'Продолжить?'
        )) return
        const { data } = await commentingAPI.rebuildPlan(id)
        showToast(data.message || 'План пересобран')
      }
      else if (action === 'delete') {
        if (!window.confirm('Удалить кампанию?')) return
        await commentingAPI.delete(id)
      }
      if (action !== 'restart' && action !== 'rebuild-plan') {
        showToast(action === 'delete' ? 'Удалена' : `Кампания: ${action}`)
      }
      await load()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
  }

  const handleAddChannels = async () => {
    if (!channelText.trim() || !selected) return
    setSaving(true)
    try {
      const channels = channelText.split('\n').map(s => s.trim()).filter(Boolean)
      const { data } = await commentingAPI.addChannels(selected.id, channels)
      setChannelText('')
      setChannelModal(false)
      const { data: updated } = await commentingAPI.get(selected.id)
      setSelected({ ...updated })
      await load()
      showToast(data.message)
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setSaving(false)
  }

  const handleRemoveChannel = async (campaignId, channelId) => {
    try {
      await commentingAPI.removeChannel(campaignId, channelId)
      showToast('Канал удалён')
      await load()
      const { data: updated } = await commentingAPI.get(campaignId)
      setSelected(updated)
    } catch { }
  }

  // Перечень кампаний идёт без каналов, а подписке нужны их имена —
  // догружаем перед открытием окна.
  const openSubscribe = async (campaign) => {
    setSelected(campaign); setSubTime(240); setSubscribeModal(true)
    if (!campaign.channels || campaign.channels.length === 0) {
      try {
        const { data } = await commentingAPI.get(campaign.id)
        setSelected(prev => (prev && prev.id === campaign.id
          ? { ...prev, channels: data.channels || [] } : prev))
      } catch { }
    }
  }

  const handleSubscribe = async () => {
    if (!selected) return
    setSubRunning(true)
    try {
      const channels = (selected.channels || []).map(ch => '@' + ch.username)
      // Каналы догружаются отдельно; если нажали раньше, чем они приехали,
      // лучше сказать об этом, чем молча запустить подписку в пустоту.
      if (channels.length === 0) {
        showToast('Каналы ещё грузятся — секунду', 'error')
        setSubRunning(false)
        return
      }
      const { data: task } = await subscribeAPI.create({
        account_ids: selected.account_ids || [], channels, total_minutes: subTime,
      })
      await subscribeAPI.run(task.id)
      showToast(`Подписка запущена: ${channels.length} каналов за ~${subTime} мин`)
      setSubscribeModal(false)
      await load()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setSubRunning(false)
  }

  const getSubscribeForCampaign = (campaign) => {
    // В перечне у кампании только счётчик каналов; сверять по именам можем
    // лишь там, где список уже догружен (карточка кампании).
    if (!campaign.channels?.length) return null
    const names = campaign.channels.map(ch => ch.username)
    return subscribeTasks.find(st => st.status === 'running' && st.channels?.some(ch => names.includes(ch)))
      || subscribeTasks.find(st => st.status === 'done' && st.channels?.some(ch => names.includes(ch)))
      || null
  }

  // Перечень кампаний приходит без списков каналов — у активной их тысячи,
  // и перечень весил мегабайты. Каналы нужны только здесь, в карточке,
  // поэтому догружаем их при открытии.
  const openDetail = async (c) => {
    setSelected(c)
    setDetailTab('info')
    setActivity([])
    setPlans([])
    setOpenDays(new Set())
    setOpenSessions(new Set())
    setChannelsShown(100)
    setDetailModal(true)
    if (!c.channels || c.channels.length === 0) {
      setChannelsLoading(true)
      try {
        const { data } = await commentingAPI.get(c.id)
        setSelected(prev => (prev && prev.id === c.id
          ? { ...prev, channels: data.channels || [] } : prev))
      } catch { }
      setChannelsLoading(false)
    }
  }

  // ── Render helpers ──────────────────────────────────────────

  // Группировка планов по дням
  const plansByDay = plans.reduce((acc, p) => {
    if (!acc[p.day_number]) acc[p.day_number] = { date: p.plan_date, plans: [] }
    acc[p.day_number].plans.push(p)
    return acc
  }, {})

  if (loading) return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
      <Spinner size={32} />
    </div>
  )

  return (
    <div style={{ padding: '28px 32px', animation: 'fadeUp 0.4s cubic-bezier(0.16,1,0.3,1)' }}>
      {toast && (
        <div style={{
          position: 'fixed', top: 24, right: 24, zIndex: 999,
          padding: '12px 20px', borderRadius: 12, fontSize: 13, fontWeight: 600,
          background: toast.type === 'error' ? 'var(--red-dim)' : 'var(--green-dim)',
          color: toast.type === 'error' ? 'var(--red)' : 'var(--green)',
          border: `1px solid ${toast.type === 'error' ? 'rgba(248,81,73,0.3)' : 'rgba(61,214,140,0.3)'}`,
          boxShadow: '0 8px 30px rgba(0,0,0,0.5)', animation: 'fadeUp 0.3s ease',
        }}>{toast.text}</div>
      )}

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 28 }}>
        <div>
          <div style={{ fontSize: 11, color: 'var(--pink)', fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>◇ НЕЙРОКОММЕНТИНГ</div>
          <h1 style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.04em' }}>Кампании</h1>
          <p style={{ fontSize: 13, color: 'var(--text-3)', marginTop: 4 }}>{campaigns.length} кампаний</p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <Button variant="ghost" onClick={() => setErrorsModal(true)}>🐞 Ошибки аккаунтов</Button>
          <Button variant="primary" onClick={() => setCreateModal(true)}>+ Создать кампанию</Button>
        </div>
      </div>

      {/* Main Tabs */}
      <div style={{ display: 'flex', gap: 4, marginBottom: 20, background: 'var(--bg-2)', padding: 4, borderRadius: 12, border: '1px solid var(--border)', width: 'fit-content' }}>
        {[
          { key: 'campaigns', label: '📋 Кампании' },
          { key: 'logs', label: '📝 История комментов' },
        ].map(t => (
          <button key={t.key} onClick={() => { if (t.key === 'logs' && logs.length === 0) loadLogs(); setTab(t.key) }} style={{
            padding: '8px 18px', borderRadius: 10, border: 'none', cursor: 'pointer', fontSize: 13, fontWeight: 600,
            background: tab === t.key ? 'rgba(124,77,255,0.15)' : 'transparent',
            color: tab === t.key ? 'var(--violet)' : 'var(--text-3)', transition: 'all 0.15s',
          }}>{t.label}</button>
        ))}
      </div>

      {tab === 'logs' ? (
        <div>
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
            <Button variant="ghost" size="sm" onClick={loadLogs} loading={logsLoading}>🔄 Обновить</Button>
          </div>
          {logsLoading
            ? <div style={{ display: 'flex', justifyContent: 'center', padding: 40 }}><Spinner size={24} /></div>
            : logs.length === 0
              ? <Empty icon="📝" title="Нет комментариев" subtitle="Запустите кампанию и дождитесь первого комментария" />
              : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {logs.map(l => (
                    <Card key={l.id} style={{ padding: '14px 18px' }}>
                      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14 }}>
                        <div style={{ fontSize: 24 }}>💬</div>
                        <div style={{ flex: 1 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                            <span style={{ fontWeight: 700, fontSize: 13 }}>@{l.channel_username}</span>
                            <Badge color="violet">{l.llm_provider}</Badge>
                            <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{l.account_phone}</span>
                            <span style={{ fontSize: 11, color: 'var(--text-3)', marginLeft: 'auto' }}>{new Date(l.created_at).toLocaleString('ru')}</span>
                          </div>
                          <div style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 6, padding: '6px 10px', background: 'var(--bg-3)', borderRadius: 8, borderLeft: '3px solid var(--border)' }}>
                            📄 {l.post_text}
                          </div>
                          <div style={{ fontSize: 13, color: 'var(--green)', padding: '6px 10px', background: 'rgba(61,214,140,0.06)', borderRadius: 8, borderLeft: '3px solid var(--green)' }}>
                            💬 {l.comment_text}
                          </div>
                        </div>
                      </div>
                    </Card>
                  ))}
                </div>
              )
          }
        </div>
      ) : (
        <>
          {campaigns.length > 0 && (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginBottom: 20 }}>
              <StatCard label="Всего кампаний" value={campaigns.length} icon="📋" />
              <StatCard label="Активных" value={campaigns.filter(c => c.status === 'active').length} color="var(--green)" icon="▶" />
              <StatCard label="Комментариев" value={campaigns.reduce((s, c) => s + (c.comments_sent || 0), 0)} color="var(--violet)" icon="💬" />
              <StatCard label="Каналов" value={campaigns.reduce((s, c) => s + (c.channels_count || 0), 0)} color="var(--blue)" icon="📢" />
            </div>
          )}

          {campaigns.length === 0 ? (
            <Empty icon="◇" title="Нет кампаний" subtitle="Создайте первую кампанию нейрокомментинга"
              action={<Button variant="primary" onClick={() => setCreateModal(true)}>+ Создать</Button>} />
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {campaigns.map(c => {
                const sub = getSubscribeForCampaign(c)
                return (
                  <Card key={c.id} onClick={() => openDetail(c)} style={{ cursor: 'pointer' }}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                      <div style={{ flex: 1 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
                          <span style={{ fontWeight: 700, fontSize: 15, letterSpacing: '-0.02em' }}>{c.name}</span>
                          <Badge color={STATUS_COLORS[c.status]}>{STATUS_LABELS[c.status]}</Badge>
                          <Badge color="violet">{{ claude: 'Claude', openai: 'GPT-4o', gemini: 'Gemini', groq: 'Groq' }[c.llm_provider] || c.llm_provider}</Badge>
                          <Badge color="default">{TONES.find(t => t.value === c.tone)?.label || c.tone}</Badge>
                        </div>
                        <div style={{ display: 'flex', gap: 16, fontSize: 12, color: 'var(--text-3)', flexWrap: 'wrap' }}>
                          <span>💬 {c.comments_sent}/{c.max_comments}</span>
                          <span>📢 {c.channels_count} каналов</span>
                          <span>👤 {(c.account_ids || []).length} акк.</span>
                          <span>⏱ {c.trigger_mode === 'all' ? 'Каждый пост' : c.trigger_mode === 'random' ? `${c.trigger_percent}% постов` : 'По ключам'}</span>
                          {c.started_at && c.max_hours > 0 && (() => {
                            const endsAt = new Date(new Date(c.started_at).getTime() + c.max_hours * 3600000)
                            const diff = endsAt - new Date()
                            if (diff <= 0) return <span style={{ color: 'var(--green)' }}>✅ завершилась</span>
                            const h = Math.floor(diff / 3600000)
                            const m = Math.floor((diff % 3600000) / 60000)
                            return <span style={{ color: 'var(--blue)' }}>⏱ ещё {h > 0 ? `${h}ч ` : ''}{m}м</span>
                          })()}
                        </div>

                        {c.status === 'scheduled' && (
                          <div style={{
                            marginTop: 8, padding: '8px 12px', borderRadius: 8, fontSize: 12, display: 'inline-block',
                            background: 'rgba(124,77,255,0.08)',
                            color: 'var(--violet)',
                            border: '1px solid rgba(124,77,255,0.25)',
                            lineHeight: 1.5,
                          }}>
                            📅 <b>Стартует автоматически</b> — когда все прогревы batch'а закончатся
                            {c.scheduled_start_at && (
                              <>, либо не позднее <b>{new Date(c.scheduled_start_at).toLocaleString('ru')}</b></>
                            )}
                            . Кнопка <b>🚀 Запустить сейчас</b> справа — если не хочешь ждать.
                          </div>
                        )}

                        {sub && (
                          <div style={{
                            marginTop: 8, padding: '5px 10px', borderRadius: 6, fontSize: 11, display: 'inline-block',
                            background: sub.status === 'running' ? 'rgba(124,77,255,0.08)' : 'rgba(61,214,140,0.08)',
                            color: sub.status === 'running' ? 'var(--violet)' : 'var(--green)',
                            border: `1px solid ${sub.status === 'running' ? 'rgba(124,77,255,0.2)' : 'rgba(61,214,140,0.2)'}`,
                          }}>
                            {sub.status === 'running' ? (
                              <>◎ Подписка {sub.progress}% — ✅ {sub.subscribed} / ❌ {sub.failed}
                                {sub.started_at && sub.progress > 0 && (() => {
                                  const elapsed = (Date.now() - new Date(sub.started_at).getTime()) / 60000
                                  const total = elapsed / (sub.progress / 100)
                                  const remaining = Math.max(0, Math.round(total - elapsed))
                                  return remaining > 60
                                    ? ` · ~${Math.round(remaining / 60)}ч ${remaining % 60}м`
                                    : ` · ~${remaining} мин`
                                })()}
                              </>
                            ) : (
                              <>✅ Подписка: {sub.subscribed} подписано, {sub.skipped} уже были</>
                            )}
                          </div>
                        )}

                        <div style={{ marginTop: 8, height: 3, background: 'rgba(255,255,255,0.06)', borderRadius: 2, overflow: 'hidden' }}>
                          <div style={{ width: `${Math.min(100, c.max_comments > 0 ? (c.comments_sent / c.max_comments) * 100 : 0)}%`, height: '100%', background: 'linear-gradient(90deg, #7c4dff, #3d8bff)', transition: 'width 0.6s' }} />
                        </div>
                      </div>

                      <div style={{ display: 'flex', gap: 6, marginLeft: 16, alignItems: 'center' }} onClick={e => e.stopPropagation()}>
                        {c.channels_count > 0 && c.status !== 'active' && (!sub || sub.status === 'done') && (
                          <Button variant="outline" size="sm" onClick={() => openSubscribe(c)}>◎ Подписать</Button>
                        )}
                        {sub?.status === 'running' && (
                          <span style={{ fontSize: 11, color: 'var(--violet)', fontWeight: 600 }}>⏳ {sub.progress}%</span>
                        )}
                        {(c.status === 'draft' || c.status === 'paused') && (
                          <Button variant="primary" size="sm" onClick={() => handleAction(c.id, 'start')}>▶ Старт</Button>
                        )}
                        {/* Для stopped/finished — «Перезапустить»: обнуляет счётчики
                            и пересоздаёт планы с ТЕКУЩИМИ max_hours (юзер мог их обновить). */}
                        {(c.status === 'stopped' || c.status === 'finished') && (
                          <Button variant="primary" size="sm"
                            onClick={() => handleAction(c.id, 'restart')}
                            title="Перезапустить с обнулением счётчиков и новыми планами">
                            🔄 Перезапустить
                          </Button>
                        )}
                        {c.status === 'scheduled' && (
                          <>
                            <Button variant="primary" size="sm" title="Запустить сейчас, не дожидаясь окончания прогрева"
                              onClick={() => handleAction(c.id, 'start')}>
                              🚀 Запустить сейчас
                            </Button>
                            <Button variant="ghost" size="sm" title="Отменить планирование — кампания вернётся в «Черновик»"
                              onClick={() => handleAction(c.id, 'cancel-schedule')}>
                              ❌ Отменить
                            </Button>
                          </>
                        )}
                        {c.status === 'active' && (
                          <>
                            <Button variant="ghost" size="sm"
                              onClick={() => handleAction(c.id, 'rebuild-plan')}
                              title="Пересобрать план под текущие правила комментинга без сброса счётчиков">
                              🔄 Пересобрать план
                            </Button>
                            <Button variant="ghost" size="sm" onClick={() => handleAction(c.id, 'pause')}>⏸ Пауза</Button>
                            <Button variant="danger" size="sm" onClick={() => handleAction(c.id, 'stop')}>⏹ Стоп</Button>
                          </>
                        )}
                        <Button variant="ghost" size="sm" onClick={() => openEdit(c)} title="Переименовать / сменить аккаунты">✎</Button>
                        <Button variant="ghost" size="sm" onClick={() => handleAction(c.id, 'delete')}>✕</Button>
                      </div>
                    </div>
                  </Card>
                )
              })}
            </div>
          )}
        </>
      )}

      {/* ── Create Campaign Modal ──────────────────────── */}
      <Modal open={createModal} onClose={() => setCreateModal(false)} title="Новая кампания" width={600}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14, maxHeight: '70vh', overflow: 'auto' }}>
          <Input label="Название" value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} placeholder="Крипто-комментинг" />

          {/* ── Тип кампании ── */}
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>
              Тип кампании
            </label>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
              {[
                {
                  key: 'regular',
                  label: '💬 Обычная (комментинг)',
                  desc: 'Много комментов на канал. Verify авто в каждой 2-й сессии.',
                },
                {
                  key: 'check_only',
                  label: '🔍 Проверка каналов',
                  desc: '1 коммент/канал → screening. Без time-limit. Verify каждую сессию.',
                },
              ].map(opt => {
                const on = form.campaign_type === opt.key
                return (
                  <button key={opt.key}
                    onClick={() => setForm(f => ({ ...f, campaign_type: opt.key }))}
                    style={{
                      padding: '10px 12px', borderRadius: 10, cursor: 'pointer',
                      fontSize: 12, fontWeight: on ? 700 : 500,
                      background: on ? 'rgba(124,77,255,0.15)' : 'var(--bg-3)',
                      border: `1px solid ${on ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                      color: on ? 'var(--violet)' : 'var(--text-2)',
                      display: 'flex', flexDirection: 'column', gap: 3, textAlign: 'left',
                    }}>
                    <div>{opt.label}</div>
                    <div style={{ fontSize: 10, opacity: 0.75, fontWeight: 400, lineHeight: 1.4 }}>{opt.desc}</div>
                  </button>
                )
              })}
            </div>
          </div>

          <Input
            label="Уведомления при удалении коммента (@username или chat_id) — опционально"
            value={form.notification_chat || ''}
            onChange={e => setForm(f => ({ ...f, notification_chat: e.target.value }))}
            placeholder="@my_notifications"
          />

          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
                Аккаунты ({form.account_ids.length} / {(onlyAlive ? accounts.filter(a => a.status === 'active') : accounts).length})
              </label>
              <label style={{ fontSize: 11, color: 'var(--text-2)', display: 'flex', alignItems: 'center', gap: 5, cursor: 'pointer' }}>
                <input type="checkbox" checked={onlyAlive} onChange={e => setOnlyAlive(e.target.checked)} />
                Только живые
              </label>
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {(onlyAlive ? accounts.filter(a => a.status === 'active') : accounts).map(a => (
                <button key={a.id} onClick={() => setForm(f => ({ ...f, account_ids: f.account_ids.includes(a.id) ? f.account_ids.filter(x => x !== a.id) : [...f.account_ids, a.id] }))} style={{
                  padding: '6px 12px', borderRadius: 8, fontSize: 12, cursor: 'pointer', transition: 'all 0.15s',
                  background: form.account_ids.includes(a.id) ? 'rgba(124,77,255,0.2)' : 'var(--bg-3)',
                  border: `1px solid ${form.account_ids.includes(a.id) ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                  color: form.account_ids.includes(a.id) ? 'var(--violet)' : 'var(--text-2)',
                }}>{a.first_name || a.phone}</button>
              ))}
              {(onlyAlive ? accounts.filter(a => a.status === 'active') : accounts).length === 0 && (
                <div style={{ fontSize: 12, color: 'var(--text-3)' }}>
                  {onlyAlive ? 'Живых аккаунтов нет. Сними галку «Только живые» чтобы увидеть остальных.' : 'Нет аккаунтов'}
                </div>
              )}
            </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>LLM</label>
              <select value={form.llm_provider} onChange={e => setForm(f => ({ ...f, llm_provider: e.target.value, llm_credential_id: null }))} style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 14, outline: 'none' }}>
                <option value="claude">Claude (Anthropic)</option>
                <option value="openai">GPT-4o (OpenAI)</option>
                <option value="gemini">Gemini (Google)</option>
                <option value="groq">Llama 3.3 70B (Groq)</option>
              </select>
            </div>
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Длина комментария</label>
              <select value={form.comment_length} onChange={e => setForm(f => ({ ...f, comment_length: e.target.value }))} style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 14, outline: 'none' }}>
                <option value="short">Короткий (2-3 слова)</option>
                <option value="medium">Средний (1-3 предложения)</option>
                <option value="long">Развёрнутый (2-4 предложения)</option>
              </select>
            </div>
          </div>

          {/* Селектор конкретного LLM-ключа для текущего провайдера */}
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>
              API ключ
            </label>
            {(() => {
              const matching = llmCreds.filter(k => k.provider === form.llm_provider)
              if (matching.length === 0) {
                return (
                  <div style={{ padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, fontSize: 12, color: 'var(--text-3)' }}>
                    Нет добавленных ключей для <b>{form.llm_provider}</b>. Будет использоваться env (если есть). Добавь ключи в «🔑 API ключи» → «🤖 LLM сервисы».
                  </div>
                )
              }
              return (
                <>
                  <select value={form.llm_credential_id || ''}
                    onChange={e => setForm(f => ({ ...f, llm_credential_id: e.target.value ? parseInt(e.target.value) : null }))}
                    style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 14, outline: 'none' }}>
                    <option value="">По умолчанию (default-ключ {form.llm_provider})</option>
                    {matching.map(k => (
                      <option key={k.id} value={k.id}>
                        {k.is_default ? '⭐ ' : ''}{k.label || `#${k.id}`} · {k.api_key_masked}
                      </option>
                    ))}
                  </select>
                  <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4 }}>
                    Если «по умолчанию» — берётся ключ помеченный ⭐ default. Можно явно выбрать любой активный.
                  </div>
                </>
              )
            })()}
          </div>

          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Тональность</label>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 6 }}>
              {TONES.map(t => (
                <button key={t.value} onClick={() => setForm(f => ({ ...f, tone: t.value }))} style={{
                  padding: '8px 10px', borderRadius: 8, fontSize: 12, cursor: 'pointer', textAlign: 'left',
                  background: form.tone === t.value ? 'rgba(124,77,255,0.2)' : 'var(--bg-3)',
                  border: `1px solid ${form.tone === t.value ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                  color: form.tone === t.value ? 'var(--violet)' : 'var(--text-2)',
                }}>{t.icon} {t.label}</button>
              ))}
            </div>
          </div>

          {form.tone === 'custom' && (
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Кастомный промпт</label>
              <textarea value={form.custom_prompt} onChange={e => setForm(f => ({ ...f, custom_prompt: e.target.value }))} rows={4} placeholder="Ты — эксперт по крипте. Пиши комментарии..." style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 13, fontFamily: 'var(--font-mono)', resize: 'vertical', outline: 'none' }} />
            </div>
          )}

          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Триггер</label>
            <div style={{ display: 'flex', gap: 6 }}>
              {TRIGGERS.map(t => (
                <button key={t.value} onClick={() => setForm(f => ({ ...f, trigger_mode: t.value }))} style={{
                  padding: '8px 14px', borderRadius: 8, fontSize: 12, cursor: 'pointer',
                  background: form.trigger_mode === t.value ? 'rgba(124,77,255,0.2)' : 'var(--bg-3)',
                  border: `1px solid ${form.trigger_mode === t.value ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                  color: form.trigger_mode === t.value ? 'var(--violet)' : 'var(--text-2)',
                }}>{t.label}</button>
              ))}
            </div>
          </div>

          {form.trigger_mode === 'random' && <Input label="Процент постов (%)" type="number" value={form.trigger_percent} onChange={e => setForm(f => ({ ...f, trigger_percent: parseInt(e.target.value) || 0 }))} />}
          {form.trigger_mode === 'keywords' && <Input label="Ключевые слова (через запятую)" value={form.trigger_keywords} onChange={e => setForm(f => ({ ...f, trigger_keywords: e.target.value }))} placeholder="крипта, блокчейн, биткоин" />}

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 }}>
            <Input label="Макс. комментов" type="number" value={form.max_comments} onChange={e => setForm(f => ({ ...f, max_comments: parseInt(e.target.value) || 0 }))} />
            <Input label="Макс. часов" type="number" value={form.max_hours} onChange={e => setForm(f => ({ ...f, max_hours: parseInt(e.target.value) || 0 }))} />
            <Input label="Задержка коммент. (сек)" type="number" value={form.delay_comment} onChange={e => setForm(f => ({ ...f, delay_comment: parseInt(e.target.value) || 0 }))} />
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <Input label="Задержка входа (сек)" type="number" value={form.delay_join} onChange={e => setForm(f => ({ ...f, delay_join: parseInt(e.target.value) || 0 }))} />
            <Input label="Между комментами (сек)" type="number" value={form.delay_between} onChange={e => setForm(f => ({ ...f, delay_between: parseInt(e.target.value) || 0 }))} />
          </div>

          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 4 }}>
            <Button variant="ghost" onClick={() => setCreateModal(false)}>Отмена</Button>
            <Button variant="primary" loading={saving} disabled={!form.name} onClick={handleCreate}>Создать</Button>
          </div>
        </div>
      </Modal>

      {/* ── Edit Campaign Modal (название + аккаунты) ─── */}
      <Modal open={editModal} onClose={() => setEditModal(false)} title="✎ Редактировать кампанию" width={580}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14, maxHeight: '70vh', overflow: 'auto' }}>
          <Input label="Название" value={editForm.name}
            onChange={e => setEditForm(f => ({ ...f, name: e.target.value }))}
            placeholder="Крипто-комментинг" />

          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
                Аккаунты ({editForm.account_ids.length} / {(editOnlyAlive ? accounts.filter(a => a.status === 'active') : accounts).length})
              </label>
              <label style={{ fontSize: 11, color: 'var(--text-2)', display: 'flex', alignItems: 'center', gap: 5, cursor: 'pointer' }}>
                <input type="checkbox" checked={editOnlyAlive} onChange={e => setEditOnlyAlive(e.target.checked)} />
                Только живые
              </label>
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, maxHeight: 200, overflowY: 'auto' }}>
              {(editOnlyAlive ? accounts.filter(a => a.status === 'active') : accounts).map(a => {
                const on = editForm.account_ids.includes(a.id)
                return (
                  <button key={a.id}
                    onClick={() => setEditForm(f => ({
                      ...f,
                      account_ids: on
                        ? f.account_ids.filter(x => x !== a.id)
                        : [...f.account_ids, a.id]
                    }))}
                    style={{
                      padding: '6px 12px', borderRadius: 8, fontSize: 12, cursor: 'pointer', transition: 'all 0.15s',
                      background: on ? 'rgba(124,77,255,0.2)' : 'var(--bg-3)',
                      border: `1px solid ${on ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                      color: on ? 'var(--violet)' : 'var(--text-2)',
                    }}>{a.first_name || a.phone}</button>
                )
              })}
              {(editOnlyAlive ? accounts.filter(a => a.status === 'active') : accounts).length === 0 && (
                <div style={{ fontSize: 12, color: 'var(--text-3)' }}>Нет аккаунтов</div>
              )}
            </div>
          </div>

          {/* Лимиты + тайминги — редактируются, применяются при следующем /start или /restart */}
          <div style={{ padding: 12, borderRadius: 8, background: 'var(--bg-3)', border: '1px solid var(--border)' }}>
            <div style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: 8 }}>
              Лимиты и тайминги
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
              <Input label="Макс. часов" type="number"
                value={editForm.max_hours}
                onChange={e => setEditForm(f => ({ ...f, max_hours: parseInt(e.target.value) || 0 }))} />
              <Input label="Макс. комментов" type="number"
                value={editForm.max_comments}
                onChange={e => setEditForm(f => ({ ...f, max_comments: parseInt(e.target.value) || 0 }))} />
              <Input label="Задержка перед подпиской (сек)" type="number"
                value={editForm.delay_join}
                onChange={e => setEditForm(f => ({ ...f, delay_join: parseInt(e.target.value) || 0 }))} />
              <Input label="Задержка перед комментом (сек)" type="number"
                value={editForm.delay_comment}
                onChange={e => setEditForm(f => ({ ...f, delay_comment: parseInt(e.target.value) || 0 }))} />
              <Input label="Между комментами (сек)" type="number"
                value={editForm.delay_between}
                onChange={e => setEditForm(f => ({ ...f, delay_between: parseInt(e.target.value) || 0 }))} />
            </div>
            <div style={{ marginTop: 8, fontSize: 11, color: 'var(--text-3)', lineHeight: 1.4 }}>
              💡 Значения применятся при следующем <strong>▶ Старт</strong> или <strong>🔄 Перезапустить</strong>.
              Уже сгенерированные планы (если кампания active) продолжат работать со старыми параметрами.
            </div>
          </div>

          <div style={{ padding: 10, borderRadius: 8, background: 'rgba(255,180,0,0.08)', border: '1px solid rgba(255,180,0,0.2)', fontSize: 11, color: 'var(--text-2)' }}>
            Каналы кампании редактируй прямо в её карточке: кнопки <strong>📺 Каналы</strong> / <strong>+ Добавить</strong>.
          </div>

          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <Button variant="ghost" onClick={() => setEditModal(false)} disabled={editSaving}>Отмена</Button>
            <Button variant="primary" onClick={handleEditSave} loading={editSaving}>Сохранить</Button>
          </div>
        </div>
      </Modal>

      {/* ── Detail Modal ───────────────────────────────── */}
      <Modal open={detailModal} onClose={() => setDetailModal(false)} title={selected?.name || 'Кампания'} width={740}>
        {selected && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14, maxHeight: '78vh', overflow: 'auto' }}>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <Badge color={STATUS_COLORS[selected.status]}>{STATUS_LABELS[selected.status]}</Badge>
              <Badge color="violet">{{ claude: 'Claude', openai: 'GPT-4o', gemini: 'Gemini', groq: 'Groq' }[selected.llm_provider] || selected.llm_provider}</Badge>
              <Badge color="default">{TONES.find(t => t.value === selected.tone)?.label}</Badge>
              <Badge color="blue">💬 {selected.comments_sent}/{selected.max_comments}</Badge>
            </div>

            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, color: 'var(--text-3)', marginBottom: 4 }}>
                <span>Прогресс</span>
                <span>{Math.min(100, Math.round(selected.comments_sent / Math.max(selected.max_comments, 1) * 100))}%</span>
              </div>
              <div style={{ height: 6, background: 'rgba(255,255,255,0.06)', borderRadius: 3, overflow: 'hidden' }}>
                <div style={{ width: `${Math.min(100, selected.comments_sent / Math.max(selected.max_comments, 1) * 100)}%`, height: '100%', background: 'linear-gradient(90deg, #7c4dff, #3d8bff)', borderRadius: 3 }} />
              </div>
            </div>

            {/* Detail Tabs */}
            <div style={{ display: 'flex', gap: 4, background: 'var(--bg-2)', padding: 4, borderRadius: 10, border: '1px solid var(--border)' }}>
              {[
                { key: 'info', label: '📋 Инфо' },
                { key: 'plans', label: '📅 Планы' },
                { key: 'activity', label: '🔍 Активность' },
                { key: 'diagnostics', label: '🩺 Диагностика' },
              ].map(t => (
                <button key={t.key} onClick={() => {
                  setDetailTab(t.key)
                  if (t.key === 'activity' && activity.length === 0) loadActivity(selected.id)
                  if (t.key === 'plans' && plans.length === 0) loadPlans(selected.id)
                  if (t.key === 'diagnostics') loadDiagnostics(selected.id)
                }} style={{
                  flex: 1, padding: '7px 14px', borderRadius: 8, border: 'none', cursor: 'pointer', fontSize: 12, fontWeight: 600,
                  background: detailTab === t.key ? 'rgba(124,77,255,0.15)' : 'transparent',
                  color: detailTab === t.key ? 'var(--violet)' : 'var(--text-3)', transition: 'all 0.15s',
                }}>{t.label}</button>
              ))}
            </div>

            {/* ── Activity Tab ── */}
            {detailTab === 'activity' && (
              <div>
                <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 10 }}>
                  <Button variant="ghost" size="sm" onClick={() => loadActivity(selected.id)} loading={activityLoading}>🔄 Обновить</Button>
                </div>
                {activityLoading
                  ? <div style={{ display: 'flex', justifyContent: 'center', padding: 30 }}><Spinner size={20} /></div>
                  : activity.length === 0
                    ? <Empty icon="🔍" title="Нет активности" subtitle="Запустите кампанию — здесь появится каждое действие аккаунтов" />
                    : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                        {activity.map(a => (
                          <div key={a.id} style={{ padding: '10px 14px', background: 'var(--bg-3)', borderRadius: 10, border: '1px solid var(--border)' }}>
                            {a.type === 'warmup' ? (
                              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                                <span style={{ fontSize: 16 }}>{a.action_icon}</span>
                                <span style={{ fontWeight: 600, fontSize: 12, color: 'var(--text-2)' }}>{a.account_phone}</span>
                                <span style={{ fontSize: 12, color: a.success === false ? 'var(--red)' : 'var(--text-3)' }}>{a.detail}</span>
                                {a.channel && <span style={{ fontSize: 11, color: 'var(--violet)' }}>@{a.channel}</span>}
                                <span style={{ fontSize: 10, color: 'var(--text-3)', marginLeft: 'auto' }}>{a.created_at ? new Date(a.created_at).toLocaleTimeString('ru') : ''}</span>
                              </div>
                            ) : (
                              <>
                                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                                  <span style={{ fontSize: 18 }}>{a.status === 'done' ? '✅' : a.status === 'aborted' ? '🚫' : a.status === 'failed' ? '❌' : a.status === 'scheduled' ? '⏳' : '⚙️'}</span>
                                  <span style={{ fontWeight: 700, fontSize: 13 }}>{a.account_phone}</span>
                                  <span style={{ fontSize: 12, color: 'var(--text-3)' }}>→ @{a.channel} #{a.post_id}</span>
                                  <Badge color={a.status === 'done' ? 'green' : a.status === 'aborted' ? 'yellow' : a.status === 'failed' ? 'red' : 'default'}>{a.status}</Badge>
                                  {a.personality && <Badge color="violet">{a.personality}</Badge>}
                                </div>
                                {a.comment_text && (
                                  <div style={{ fontSize: 13, color: 'var(--green)', padding: '6px 10px', background: 'rgba(61,214,140,0.06)', borderRadius: 8, borderLeft: '3px solid var(--green)' }}>
                                    💬 {a.comment_text}
                                  </div>
                                )}
                                {a.error && (
                                  <div style={{ fontSize: 12, color: 'var(--red)', padding: '6px 10px', background: 'rgba(248,81,73,0.06)', borderRadius: 8, borderLeft: '3px solid var(--red)', marginTop: 4 }}>
                                    ❌ {a.error}
                                  </div>
                                )}
                                <div style={{ display: 'flex', gap: 12, fontSize: 10, color: 'var(--text-3)', marginTop: 6 }}>
                                  {a.executed_at && <span>⚡ {new Date(a.executed_at).toLocaleString('ru')}</span>}
                                </div>
                              </>
                            )}
                          </div>
                        ))}
                      </div>
                    )
                }
              </div>
            )}

            {/* ── Plans Tab ── */}
            {detailTab === 'plans' && (
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                  <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
                    {Object.keys(plansByDay).length} дней · {plans.length} планов
                  </span>
                  <Button variant="ghost" size="sm" onClick={() => loadPlans(selected.id)} loading={plansLoading}>🔄 Обновить</Button>
                </div>

                {plansLoading
                  ? <div style={{ display: 'flex', justifyContent: 'center', padding: 40 }}><Spinner size={24} /></div>
                  : Object.keys(plansByDay).length === 0
                    ? <Empty icon="📅" title="Нет планов" subtitle="Запустите кампанию — планы появятся здесь" />
                    : Object.entries(plansByDay)
                      .sort(([a], [b]) => Number(a) - Number(b))
                      .map(([dayNum, { date, plans: dayPlans }]) => {
                        const dayN = Number(dayNum)
                        const isOpen = openDays.has(dayN)

                        const totalSessions = dayPlans.reduce((s, p) => s + p.total_sessions, 0)
                        const totalComments = dayPlans.reduce((s, p) => s + p.total_comments, 0)
                        const writtenComments = dayPlans.reduce((s, p) => s + (p.smart_written || 0), 0)
                        const failedComments = dayPlans.reduce((s, p) => s + (p.smart_failed || 0), 0)
                        // Реакции на чужие комменты — отдельный источник охвата:
                        // автор коммента получает уведомление и может зайти в профиль.
                        const totalReactions = dayPlans.reduce((s, p) => s + (p.total_reactions || 0), 0)
                        const doneReactions = dayPlans.reduce((s, p) => s + (p.reactions_done || 0), 0)
                        const donePlans = dayPlans.filter(p => p.status === 'done').length

                        return (
                          <div key={dayNum} style={{ marginBottom: 10 }}>
                            {/* ── День header ── */}
                            <button
                              onClick={() => toggleDay(dayN)}
                              style={{
                                width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                                padding: '10px 14px', borderRadius: isOpen ? '10px 10px 0 0' : 10,
                                background: 'var(--bg-2)', border: '1px solid var(--border)',
                                cursor: 'pointer', transition: 'all 0.15s',
                              }}
                            >
                              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                                <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--text)' }}>
                                  {isOpen ? '▼' : '▶'} День {dayN}
                                </span>
                                <span style={{ fontSize: 12, color: 'var(--text-3)' }}>{fmtDate(date)}</span>
                              </div>
                              <div style={{ display: 'flex', gap: 10, alignItems: 'center', fontSize: 11, color: 'var(--text-3)' }}>
                                <span>👤 {dayPlans.length} акк.</span>
                                <span>🕐 {totalSessions} сессий</span>
                                {totalComments > 0 && (
                                  <span>
                                    💬 <span style={{ color: 'var(--green)', fontWeight: 700 }}>{writtenComments}</span>
                                    <span style={{ color: 'var(--text-3)' }}>+</span>
                                    <span style={{ color: 'var(--text-3)', fontWeight: 700 }}>{failedComments}</span>
                                    <span style={{ color: 'var(--text-3)' }}>/{totalComments}</span>
                                  </span>
                                )}
                                {totalReactions > 0 && (
                                  <span title="реакции на чужие комменты: поставлено / запланировано">
                                    😍 <span style={{ color: 'var(--green)', fontWeight: 700 }}>{doneReactions}</span>
                                    <span style={{ color: 'var(--text-3)' }}>/{totalReactions}</span>
                                  </span>
                                )}
                                {donePlans > 0 && <Badge color="green">{donePlans}/{dayPlans.length} ✓</Badge>}
                              </div>
                            </button>

                            {/* ── Аккаунты дня ── */}
                            {isOpen && (
                              <div style={{
                                border: '1px solid var(--border)', borderTop: 'none',
                                borderRadius: '0 0 10px 10px', overflow: 'hidden',
                              }}>
                                {dayPlans.map((plan, pi) => {
                                  const sessions = plan.sessions || []
                                  const doneCount = plan.executed_idx || 0
                                  const isDone = plan.status === 'done'

                                  return (
                                    <div key={plan.id} style={{
                                      background: pi % 2 === 0 ? 'var(--bg-3)' : 'var(--bg-2)',
                                      borderTop: pi > 0 ? '1px solid var(--border)' : 'none',
                                      padding: '10px 14px',
                                    }}>
                                      {/* Аккаунт header */}
                                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                                        <span style={{ fontWeight: 700, fontSize: 13, color: 'var(--text)' }}>
                                          {plan.account_phone}
                                        </span>
                                        {plan.account_name && plan.account_name !== plan.account_phone && (
                                          <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{plan.account_name}</span>
                                        )}
                                        <Badge color="violet">{PERSONALITY_LABELS[plan.personality] || plan.personality}</Badge>
                                        <Badge color="default">{MOOD_LABELS[plan.mood] || plan.mood}</Badge>
                                        {plan.total_comments > 0 && <Badge color="green">💬 {plan.total_comments}</Badge>}
                                        {plan.total_reactions > 0 && (
                                          <Badge color="violet" title="реакции на комменты: поставлено / запланировано">
                                            😍 {plan.reactions_done || 0}/{plan.total_reactions}
                                          </Badge>
                                        )}
                                        <div style={{ marginLeft: 'auto', fontSize: 11, color: isDone ? 'var(--green)' : 'var(--text-3)' }}>
                                          {isDone ? '✅ Выполнено' : `${doneCount}/${sessions.length} сессий`}
                                        </div>
                                      </div>

                                      {/* Сессии */}
                                      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                                        {sessions.map((sess, si) => {
                                          const sessKey = `${plan.id}-${si}`
                                          const isExpanded = openSessions.has(sessKey)
                                          const sessStatus = sess.skipped
                                            ? 'skip'
                                            : si < doneCount ? 'done'
                                              : si === doneCount && !isDone ? 'current'
                                                : 'pending'

                                          const bgColors = {
                                            done: 'rgba(61,214,140,0.06)',
                                            current: 'rgba(124,77,255,0.08)',
                                            skip: 'rgba(255,255,255,0.02)',
                                            pending: 'transparent',
                                          }
                                          const borderColors = {
                                            done: 'rgba(61,214,140,0.2)',
                                            current: 'rgba(124,77,255,0.3)',
                                            skip: 'var(--border)',
                                            pending: 'var(--border)',
                                          }
                                          const statusIcon = {
                                            done: '✅', current: '⏳', skip: '💤', pending: '○',
                                          }[sessStatus]

                                          const actions = (sess.actions || []).filter(a => a.type !== 'idle')

                                          // Мини-превью иконок действий
                                          const previewIcons = [...new Set(
                                            actions.slice(0, 5).map(a => ACTION_ICONS[a.type] || '•')
                                          )].join(' ')

                                          return (
                                            <div key={si} style={{
                                              background: bgColors[sessStatus],
                                              border: `1px solid ${borderColors[sessStatus]}`,
                                              borderRadius: 8,
                                              overflow: 'hidden',
                                            }}>
                                              {/* Сессия row */}
                                              <button
                                                onClick={() => !sess.skipped && toggleSession(sessKey)}
                                                style={{
                                                  width: '100%', display: 'flex', alignItems: 'center', gap: 8,
                                                  padding: '7px 10px', background: 'none', border: 'none',
                                                  cursor: sess.skipped ? 'default' : 'pointer', textAlign: 'left',
                                                }}
                                              >
                                                <span style={{ fontSize: 13, width: 18, textAlign: 'center', color: sessStatus === 'done' ? 'var(--green)' : sessStatus === 'current' ? 'var(--violet)' : 'var(--text-3)' }}>{statusIcon}</span>
                                                <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-2)', minWidth: 28 }}>#{si + 1}</span>
                                                <span style={{ fontSize: 12, color: 'var(--text-3)', minWidth: 36 }}>{fmtTime(sess.connect_at_hour, sess.connect_at_minute)}</span>

                                                {sess.skipped ? (
                                                  <span style={{ fontSize: 11, color: 'var(--text-3)', fontStyle: 'italic' }}>пропущено: {sess.skip_reason}</span>
                                                ) : (
                                                  <>
                                                    <span style={{ fontSize: 11, color: 'var(--text-3)', flex: 1 }}>
                                                      {previewIcons} &nbsp;
                                                      <span style={{ color: 'var(--text-3)' }}>{actions.length} действий</span>
                                                    </span>
                                                    {actions.some(a => a.type === 'join_target_channel') && (
                                                      <span style={{ fontSize: 10, color: 'var(--blue)', fontWeight: 600 }}>📢 вступление</span>
                                                    )}
                                                    {actions.some(a => a.type === 'smart_comment') && (
                                                      <span style={{ fontSize: 10, color: 'var(--green)', fontWeight: 600 }}>💬 коммент</span>
                                                    )}
                                                    <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{isExpanded ? '▲' : '▼'}</span>
                                                  </>
                                                )}
                                              </button>

                                              {/* Развернутые действия */}
                                              {isExpanded && !sess.skipped && (
                                                <div style={{ borderTop: '1px solid var(--border)', padding: '6px 10px 8px 10px' }}>
                                                  {(sess.actions || []).map((a, ai) => (
                                                    <div key={ai} style={{
                                                      display: 'flex', alignItems: 'center', gap: 8,
                                                      padding: '3px 0',
                                                      borderBottom: ai < sess.actions.length - 1 ? '1px solid rgba(255,255,255,0.04)' : 'none',
                                                    }}>
                                                      <span style={{ fontSize: 13, width: 20, textAlign: 'center', flexShrink: 0 }}>
                                                        {ACTION_ICONS[a.type] || '•'}
                                                      </span>
                                                      <span style={{ fontSize: 12, color: actionColor(a) }}>
                                                        {actionLabel(a)}
                                                      </span>
                                                      {a.pause_after && (
                                                        <span style={{ fontSize: 10, color: 'var(--text-3)', marginLeft: 'auto', flexShrink: 0 }}>
                                                          +{a.pause_after}с
                                                        </span>
                                                      )}
                                                    </div>
                                                  ))}
                                                </div>
                                              )}
                                            </div>
                                          )
                                        })}
                                      </div>
                                    </div>
                                  )
                                })}
                              </div>
                            )}
                          </div>
                        )
                      })
                }
              </div>
            )}

            {/* ── Diagnostics Tab ── */}
            {detailTab === 'diagnostics' && (
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12, flexWrap: 'wrap', gap: 8 }}>
                  <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
                    Реальное состояние кампании: планы, ошибки, зависшие сессии
                  </span>
                  <div style={{ display: 'flex', gap: 6 }}>
                    <Button variant="ghost" size="sm" onClick={() => loadDiagnostics(selected.id)} loading={diagLoading}>🔄 Обновить</Button>
                  </div>
                </div>

                {/* Backfill progress banner */}
                {backfillProgress && (
                  <Card style={{
                    padding: 12, marginBottom: 12,
                    borderColor: backfillProgress.status === 'running' ? 'var(--violet)' : 'var(--green)',
                  }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                      <div>
                        <div style={{ fontSize: 12, fontWeight: 700 }}>
                          {backfillProgress.status === 'running' ? '💬 Backfill в процессе' : '✅ Backfill завершён'}
                        </div>
                        <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2 }}>
                          {backfillProgress.done || 0} / {backfillProgress.total || '?'} обработано ·{' '}
                          ✅ подписано: {backfillProgress.joined || 0} ·{' '}
                          ⏭ уже был: {backfillProgress.already || 0} ·{' '}
                          🚫 без discussion: {backfillProgress.no_disc || 0} ·{' '}
                          ❌ ошибок: {backfillProgress.failed || 0}
                        </div>
                      </div>
                      {backfillProgress.status !== 'running' && (
                        <button onClick={() => setBackfillProgress(null)} style={{ background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer', fontSize: 16 }}>✕</button>
                      )}
                    </div>
                  </Card>
                )}
                {!diagnostics ? (
                  diagLoading
                    ? <div style={{ textAlign: 'center', padding: 40 }}><Spinner size={24} /></div>
                    : <Empty icon="🩺" title="Нет данных" subtitle="Нажми Обновить для загрузки" />
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                    {/* Summary */}
                    <Card style={{ padding: 14 }}>
                      <div style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: 8 }}>Общая сводка</div>
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 10 }}>
                        <div>
                          <div style={{ fontSize: 24, fontWeight: 700, color: 'var(--green)' }}>{diagnostics.summary.sent_comments}</div>
                          <div style={{ fontSize: 11, color: 'var(--text-3)' }}>✅ Отправлено</div>
                        </div>
                        <div>
                          <div style={{ fontSize: 24, fontWeight: 700, color: 'var(--text-3)' }}>{diagnostics.summary.skipped_comments || 0}</div>
                          <div style={{ fontSize: 11, color: 'var(--text-3)' }}>⏭ Скипы</div>
                        </div>
                        <div>
                          <div style={{ fontSize: 24, fontWeight: 700, color: 'var(--text-2)' }}>{diagnostics.summary.planned_comments}</div>
                          <div style={{ fontSize: 11, color: 'var(--text-3)' }}>Всего в плане</div>
                        </div>
                        <div>
                          <div style={{ fontSize: 24, fontWeight: 700, color: diagnostics.summary.percent_done >= 50 ? 'var(--green)' : diagnostics.summary.percent_done >= 20 ? 'var(--yellow)' : 'var(--red)' }}>
                            {diagnostics.summary.percent_done}%
                          </div>
                          <div style={{ fontSize: 11, color: 'var(--text-3)' }}>Отправлено %</div>
                        </div>
                        <div>
                          <div style={{ fontSize: 24, fontWeight: 700, color: (diagnostics.summary.percent_processed || 0) >= 50 ? 'var(--green)' : (diagnostics.summary.percent_processed || 0) >= 20 ? 'var(--yellow)' : 'var(--red)' }}>
                            {diagnostics.summary.percent_processed || 0}%
                          </div>
                          <div style={{ fontSize: 11, color: 'var(--text-3)' }}>Обработано %</div>
                        </div>
                      </div>
                    </Card>

                    {/* Assignment status */}
                    <Card style={{ padding: 14 }}>
                      <div style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: 8 }}>
                        Подписки на каналы (акк × канал)
                      </div>
                      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                        {Object.entries(diagnostics.assignments || {}).map(([k, v]) => {
                          const color = k === 'joined' ? 'var(--green)' : k === 'failed' ? 'var(--red)' : 'var(--yellow)'
                          const label = { joined: '✅ подписаны', pending: '⏳ ждут подписки', failed: '❌ провал подписки' }[k] || k
                          return (
                            <div key={k} style={{ padding: '8px 14px', borderRadius: 10, background: 'var(--bg-3)', border: `1px solid ${color}` }}>
                              <div style={{ fontSize: 20, fontWeight: 700, color }}>{v}</div>
                              <div style={{ fontSize: 10, color: 'var(--text-3)' }}>{label}</div>
                            </div>
                          )
                        })}
                      </div>
                    </Card>

                    {/* Plans by day */}
                    <Card style={{ padding: 14 }}>
                      <div style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: 8 }}>Планы по дням</div>
                      <table style={{ width: '100%', fontSize: 12 }}>
                        <thead>
                          <tr style={{ color: 'var(--text-3)', textAlign: 'left' }}>
                            <th style={{ padding: '4px 8px' }}>День</th>
                            <th style={{ padding: '4px 8px' }}>Статус</th>
                            <th style={{ padding: '4px 8px' }}>Планов</th>
                            <th style={{ padding: '4px 8px' }}>Выполнено действий</th>
                            <th style={{ padding: '4px 8px' }}>Запланировано комментов</th>
                          </tr>
                        </thead>
                        <tbody>
                          {(diagnostics.plans_by_day || []).map((p, i) => {
                            const color = p.status === 'done' ? 'var(--green)' : p.status === 'active' ? 'var(--blue)' : 'var(--red)'
                            return (
                              <tr key={i} style={{ borderTop: '1px solid var(--border)' }}>
                                <td style={{ padding: '6px 8px' }}>{p.day}</td>
                                <td style={{ padding: '6px 8px', color }}>{p.status}</td>
                                <td style={{ padding: '6px 8px' }}>{p.count}</td>
                                <td style={{ padding: '6px 8px' }}>{p.exec_actions_sum}</td>
                                <td style={{ padding: '6px 8px' }}>{p.planned_comments}</td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                    </Card>

                    {/* Errors */}
                    {(diagnostics.errors || []).length > 0 && (
                      <Card style={{ padding: 14, borderColor: 'var(--red)' }}>
                        <div style={{ fontSize: 11, color: 'var(--red)', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: 8 }}>
                          ❌ Ошибки (последние 48ч, топ 30)
                        </div>
                        <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 8 }}>
                          Кликни на acc#XX — открыть детали акка и проверить сессию.
                        </div>
                        <div style={{ maxHeight: 400, overflowY: 'auto' }}>
                          {diagnostics.errors.map((e, i) => (
                            <div key={i} style={{ padding: '6px 0', borderTop: '1px solid var(--border)', fontSize: 11 }}>
                              <span style={{ color: 'var(--red)', fontWeight: 700 }}>×{e.count}</span>
                              <span style={{ marginLeft: 8, color: 'var(--text-2)' }}>{e.action}</span>
                              <div style={{ color: 'var(--text-3)', marginTop: 2 }}>{e.detail}</div>
                              {(e.accounts || []).length > 0 && (
                                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 4 }}>
                                  {e.accounts.slice(0, 20).map(a => {
                                    // Backward-compat: старый формат — просто id; новый — {id, phone}
                                    const aid = typeof a === 'object' ? a.id : a
                                    const phone = typeof a === 'object' ? a.phone : ''
                                    return (
                                      <span
                                        key={aid}
                                        onClick={() => navigate(`/accounts/${aid}`)}
                                        title={`Открыть детали акка #${aid}${phone ? ` (${phone})` : ''}`}
                                        style={{
                                          display: 'inline-flex',
                                          alignItems: 'center',
                                          gap: 4,
                                          padding: '1px 5px',
                                          fontSize: 10,
                                          background: 'var(--bg-3)',
                                          border: '1px solid var(--border)',
                                          borderRadius: 3,
                                          cursor: 'pointer',
                                          transition: 'background 0.15s',
                                        }}
                                        onMouseEnter={(ev) => ev.currentTarget.style.background = 'var(--bg-4)'}
                                        onMouseLeave={(ev) => ev.currentTarget.style.background = 'var(--bg-3)'}
                                      >
                                        <span style={{ fontWeight: 700 }}>acc#{aid}</span>
                                        {phone && <span style={{ color: 'var(--text-3)' }}>{phone}</span>}
                                      </span>
                                    )
                                  })}
                                  {e.accounts.length > 20 && (
                                    <span style={{ fontSize: 10, color: 'var(--text-3)', padding: '1px 5px' }}>
                                      +{e.accounts.length - 20}
                                    </span>
                                  )}
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      </Card>
                    )}

                    {/* Actions breakdown */}
                    <Card style={{ padding: 14 }}>
                      <div style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: 8 }}>Все действия (48ч)</div>
                      <div style={{ maxHeight: 250, overflowY: 'auto' }}>
                        {(diagnostics.actions_breakdown || []).map((a, i) => (
                          <div key={i} style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', fontSize: 12, borderTop: i > 0 ? '1px solid var(--border)' : 'none' }}>
                            <span>{a.success ? '✅' : '❌'} {a.action}</span>
                            <span style={{ color: 'var(--text-3)' }}>{a.count}</span>
                          </div>
                        ))}
                      </div>
                    </Card>

                    {/* Stuck plans */}
                    {(diagnostics.stuck_plans || []).length > 0 && (
                      <Card style={{ padding: 14, borderColor: 'var(--yellow)' }}>
                        <div style={{ fontSize: 11, color: 'var(--yellow)', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: 8 }}>
                          ⚠️ Зависшие планы ({diagnostics.stuck_plans.length}) — статус active, но день уже прошёл
                        </div>
                        <div style={{ maxHeight: 300, overflowY: 'auto', fontSize: 11 }}>
                          {diagnostics.stuck_plans.map(p => (
                            <div key={p.plan_id} style={{ padding: '4px 0', borderTop: '1px solid var(--border)' }}>
                              plan#{p.plan_id} · acc={p.account_id} · day{p.day_number} · {p.plan_date} · executed {p.executed_idx}/{p.total_sessions}
                            </div>
                          ))}
                        </div>
                      </Card>
                    )}

                    {/* Zero productive accounts */}
                    {(diagnostics.zero_productive_accounts || []).length > 0 && (
                      <Card style={{ padding: 14 }}>
                        <div style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: 8 }}>
                          Акки без активности ({diagnostics.zero_productive_accounts.length})
                        </div>
                        <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 8 }}>
                          Кликни на акк — открыть детали и проверить сессию.
                        </div>
                        {(() => {
                          // Группируем по причине
                          const byReason = {}
                          for (const a of diagnostics.zero_productive_accounts) {
                            const r = a.reason || 'причина не определена'
                            if (!byReason[r]) byReason[r] = []
                            byReason[r].push(a)
                          }
                          const groups = Object.entries(byReason).sort((a, b) => b[1].length - a[1].length)
                          return (
                            <div style={{ maxHeight: 320, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 10 }}>
                              {groups.map(([reason, accs]) => {
                                const isBanned = /banned|dead|spam_blocked|invalid/.test(reason)
                                const isProxy = /прокси/.test(reason)
                                const isNoPlan = /план/i.test(reason) || /сессия/i.test(reason)
                                const color = isBanned ? 'var(--red)' : isProxy ? '#ff9f43' : isNoPlan ? 'var(--yellow)' : 'var(--text-2)'
                                return (
                                  <div key={reason}>
                                    <div style={{ fontSize: 11, fontWeight: 700, color, marginBottom: 4 }}>
                                      {reason} <span style={{ color: 'var(--text-3)', fontWeight: 400 }}>× {accs.length}</span>
                                    </div>
                                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                                      {accs.map(a => (
                                        <span
                                          key={a.account_id}
                                          onClick={() => navigate(`/accounts/${a.account_id}`)}
                                          title={a.last_error || a.reason || ''}
                                          style={{
                                            display: 'inline-block',
                                            padding: '2px 6px',
                                            fontSize: 11,
                                            background: 'var(--bg-3)',
                                            border: `1px solid ${color}`,
                                            borderRadius: 4,
                                            cursor: 'pointer',
                                            transition: 'background 0.15s',
                                          }}
                                          onMouseEnter={(e) => e.currentTarget.style.background = 'var(--bg-4)'}
                                          onMouseLeave={(e) => e.currentTarget.style.background = 'var(--bg-3)'}
                                        >
                                          acc#{a.account_id}
                                          {a.phone && <span style={{ color: 'var(--text-3)', marginLeft: 4 }}>{a.phone}</span>}
                                        </span>
                                      ))}
                                    </div>
                                  </div>
                                )
                              })}
                            </div>
                          )
                        })()}
                      </Card>
                    )}

                    {/* Проблемные каналы — кликабельный список */}
                    {(diagnostics.failed_channels || []).length > 0 && (
                      <Card style={{ padding: 14 }}>
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                          <div style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
                            🚨 Проблемные каналы ({diagnostics.failed_channels.length})
                          </div>
                          <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
                            клик → открыть в Telegram
                          </div>
                        </div>
                        <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 10 }}>
                          Каналы, куда пытались писать комменты, но упало. Отсортированы по кол-ву фейлов.
                        </div>
                        <div style={{ maxHeight: 480, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 6 }}>
                          {diagnostics.failed_channels.map((ch, i) => {
                            const pr = ch.pass_rate
                            const passColor = pr === 0 ? 'var(--red)' : pr < 30 ? '#ff9f43' : pr < 70 ? 'var(--yellow)' : 'var(--green)'
                            const errList = (ch.top_errors || []).map(e => `${e.error} (×${e.count})`).join('  •  ')
                            return (
                              <div
                                key={ch.channel + i}
                                style={{
                                  display: 'flex',
                                  flexDirection: 'column',
                                  gap: 6,
                                  padding: '10px 12px',
                                  background: 'var(--bg-3)',
                                  border: '1px solid var(--border)',
                                  borderRadius: 6,
                                  fontSize: 12,
                                }}
                              >
                                <div style={{ display: 'grid', gridTemplateColumns: '1fr auto auto auto', gap: 8, alignItems: 'center' }}>
                                  <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
                                    <a
                                      href={`https://t.me/${ch.channel}`}
                                      target="_blank"
                                      rel="noopener noreferrer"
                                      style={{ fontWeight: 700, color: 'var(--accent)', textDecoration: 'none', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                                      title={`Открыть @${ch.channel} в Telegram`}
                                    >
                                      @{ch.channel} ↗
                                    </a>
                                    <div style={{ fontSize: 10, color: 'var(--text-3)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={errList}>
                                      {errList || ch.last_error || '—'}
                                    </div>
                                  </div>
                                  <div style={{ fontSize: 11, color: 'var(--text-3)', textAlign: 'right', minWidth: 90 }}>
                                    <div>✅ {ch.ok} / ❌ {ch.failed}</div>
                                    <div style={{ fontSize: 10 }}>{ch.accounts_count} акк.</div>
                                  </div>
                                  <div style={{ fontSize: 13, fontWeight: 700, color: passColor, minWidth: 48, textAlign: 'right' }}>
                                    {pr}%
                                  </div>
                                  <a
                                    href={`https://t.me/${ch.channel}`}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    style={{
                                      padding: '4px 10px',
                                      background: 'var(--accent)',
                                      color: '#000',
                                      borderRadius: 4,
                                      fontSize: 11,
                                      fontWeight: 700,
                                      textDecoration: 'none',
                                    }}
                                  >
                                    Тест
                                  </a>
                                </div>
                                {(ch.accounts_sample || []).length > 0 && (
                                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                                    {ch.accounts_sample.map(a => {
                                      const aid = typeof a === 'object' ? a.id : a
                                      const phone = typeof a === 'object' ? a.phone : ''
                                      return (
                                        <span
                                          key={aid}
                                          onClick={(ev) => { ev.stopPropagation(); navigate(`/accounts/${aid}`) }}
                                          title={`Открыть акк #${aid}${phone ? ` (${phone})` : ''}`}
                                          style={{
                                            display: 'inline-flex',
                                            alignItems: 'center',
                                            gap: 4,
                                            padding: '1px 6px',
                                            fontSize: 10,
                                            background: 'var(--bg-4)',
                                            border: '1px solid var(--border)',
                                            borderRadius: 3,
                                            cursor: 'pointer',
                                            transition: 'background 0.15s',
                                          }}
                                          onMouseEnter={(ev) => ev.currentTarget.style.background = 'var(--bg-3)'}
                                          onMouseLeave={(ev) => ev.currentTarget.style.background = 'var(--bg-4)'}
                                        >
                                          <span style={{ fontWeight: 700 }}>acc#{aid}</span>
                                          {phone && <span style={{ color: 'var(--text-3)' }}>{phone}</span>}
                                        </span>
                                      )
                                    })}
                                    {ch.accounts_count > (ch.accounts_sample || []).length && (
                                      <span style={{ fontSize: 10, color: 'var(--text-3)', padding: '1px 6px' }}>
                                        +{ch.accounts_count - ch.accounts_sample.length}
                                      </span>
                                    )}
                                  </div>
                                )}
                              </div>
                            )
                          })}
                        </div>
                      </Card>
                    )}
                  </div>
                )}
              </div>
            )}

            {/* ── Info Tab ── */}
            {detailTab === 'info' && (
              <>
                {((selected.channels || []).length || selected.channels_count || 0) > 0 && selected.status !== 'active' && (
                  <div style={{ padding: '14px 16px', borderRadius: 10, background: 'rgba(0,194,178,0.06)', border: '1px solid rgba(0,194,178,0.15)' }}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
                      <span style={{ fontWeight: 700, fontSize: 13 }}>◎ Предподписка</span>
                      <Button variant="outline" size="sm" onClick={() => openSubscribe(selected)}>Подписать аккаунты</Button>
                    </div>
                    <div style={{ fontSize: 12, color: 'var(--text-3)' }}>
                      Подпиши {(selected.account_ids || []).length} аккаунтов на {((selected.channels || []).length || selected.channels_count || 0).toLocaleString('ru')} каналов перед запуском
                    </div>
                    {(() => {
                      const sub = getSubscribeForCampaign(selected)
                      if (!sub) return null
                      return (
                        <div style={{ marginTop: 8 }}>
                          {sub.status === 'running' && (
                            <>
                              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: 'var(--text-3)', marginBottom: 4 }}>
                                <span>✅ {sub.subscribed} / ❌ {sub.failed} / ✓ {sub.skipped}</span>
                                <span>{sub.progress}%</span>
                              </div>
                              <div style={{ height: 4, borderRadius: 2, background: 'rgba(255,255,255,0.06)', overflow: 'hidden' }}>
                                <div style={{ height: '100%', width: `${sub.progress}%`, background: '#7c4dff', borderRadius: 2, transition: 'width 0.5s' }} />
                              </div>
                            </>
                          )}
                          {sub.status === 'done' && (
                            <div style={{ fontSize: 11, color: 'var(--green)' }}>
                              ✅ Завершена: {sub.subscribed} подписано, {sub.skipped} уже были, {sub.failed} ошибок
                            </div>
                          )}
                        </div>
                      )
                    })()}
                  </div>
                )}

                {/* Channels */}
                <div>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                    <span style={{ fontWeight: 700, fontSize: 13 }}>Целевые каналы ({((selected.channels || []).length || selected.channels_count || 0).toLocaleString('ru')})</span>
                    <Button variant="outline" size="sm" onClick={async () => {
                      setChannelModal(true)
                      try { const { data } = await parserAPI.folders(); setChannelFolders(data) } catch { }
                      // Грузим батчи прогрева с подписками (для импорта pre-joined каналов)
                      try {
                        const { data: tasks } = await warmupAPI.list()
                        const byBatch = {}
                        for (const t of tasks) {
                          if (!t.batch_id) continue   // показываем ВСЕ батчи прогрева (даже с 0 подписок)
                          if (!byBatch[t.batch_id]) byBatch[t.batch_id] = { batch_id: t.batch_id, batch_name: t.batch_name || t.batch_id, accounts: 0, subscribed: 0, target: 0, statuses: [] }
                          byBatch[t.batch_id].accounts += 1
                          byBatch[t.batch_id].subscribed += (t.subscribed_count || 0)
                          byBatch[t.batch_id].target += (t.target_count || 0)
                          byBatch[t.batch_id].statuses.push(t.status)
                        }
                        setWarmupBatches(Object.values(byBatch))
                      } catch { setWarmupBatches([]) }
                    }}>+ Добавить</Button>
                  </div>
                  {channelsLoading && (selected.channels || []).length === 0 ? (
                    <div style={{ fontSize: 12, color: 'var(--text-3)', textAlign: 'center', padding: '16px 0' }}>Гружу каналы…</div>
                  ) : (selected.channels || []).length === 0 ? (
                    <div style={{ fontSize: 12, color: 'var(--text-3)', textAlign: 'center', padding: '16px 0' }}>Нет каналов — добавьте для запуска</div>
                  ) : (
                    <>
                      {/* Рисуем окном: у активной кампании каналов больше пяти
                          тысяч, и весь список разом браузер собирал заметно
                          дольше, чем открывалась сама карточка. */}
                      {(selected.channels || []).slice(0, channelsShown).map(ch => (
                        <div key={ch.id} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '8px 12px', background: 'var(--bg-3)', borderRadius: 8, marginBottom: 4 }}>
                          <div>
                            <span style={{ fontWeight: 600, fontSize: 13 }}>@{ch.username}</span>
                            <span style={{ fontSize: 11, color: 'var(--text-3)', marginLeft: 8 }}>💬 {ch.comments_sent}</span>
                          </div>
                          <button onClick={() => handleRemoveChannel(selected.id, ch.id)} style={{ background: 'none', border: 'none', color: 'var(--red)', cursor: 'pointer', fontSize: 14 }}>✕</button>
                        </div>
                      ))}
                      {(selected.channels || []).length > channelsShown && (
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 10, padding: '10px 0' }}>
                          <span style={{ fontSize: 11, color: 'var(--text-3)' }}>
                            показано {channelsShown} из {(selected.channels || []).length.toLocaleString('ru')}
                          </span>
                          <Button variant="ghost" size="sm"
                            onClick={() => setChannelsShown(n => n + 100)}>Ещё 100</Button>
                        </div>
                      )}
                    </>
                  )}
                </div>

                <div style={{ padding: '12px 14px', background: 'var(--bg-3)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)' }}>
                  <div>Триггер: {TRIGGERS.find(t => t.value === selected.trigger_mode)?.label}{selected.trigger_mode === 'random' ? ` (${selected.trigger_percent}%)` : ''}{selected.trigger_mode === 'keywords' ? `: ${(selected.trigger_keywords || []).join(', ')}` : ''}</div>
                  <div style={{ marginTop: 4 }}>Тайминги: вход {selected.delay_join}с · коммент {selected.delay_comment}с · между {selected.delay_between}с</div>
                  <div style={{ marginTop: 4 }}>Лимит: {selected.max_comments} комментов / {selected.max_hours}ч</div>
                  {selected.started_at && selected.max_hours > 0 && (() => {
                    const endsAt = new Date(new Date(selected.started_at).getTime() + selected.max_hours * 3600000)
                    const now = new Date()
                    const diff = endsAt - now
                    const isOver = diff <= 0
                    const hLeft = Math.floor(Math.abs(diff) / 3600000)
                    const mLeft = Math.floor((Math.abs(diff) % 3600000) / 60000)
                    return (
                      <div style={{ marginTop: 6, padding: '6px 10px', borderRadius: 8, background: isOver ? 'rgba(61,214,140,0.08)' : 'rgba(61,139,255,0.08)', border: `1px solid ${isOver ? 'rgba(61,214,140,0.2)' : 'rgba(61,139,255,0.2)'}`, display: 'flex', alignItems: 'center', gap: 8 }}>
                        <span style={{ fontSize: 13 }}>{isOver ? '✅' : '⏱'}</span>
                        <span style={{ color: isOver ? 'var(--green)' : 'var(--blue)', fontWeight: 600 }}>
                          {isOver ? 'Завершилась' : `Заканчивается`}
                        </span>
                        <span style={{ color: 'var(--text-3)' }}>
                          {endsAt.toLocaleString('ru', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}
                        </span>
                        {!isOver && (
                          <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--blue)', fontWeight: 600 }}>
                            через {hLeft > 0 ? `${hLeft}ч ` : ''}{mLeft}м
                          </span>
                        )}
                      </div>
                    )
                  })()}
                  {selected.custom_prompt && <div style={{ marginTop: 4, color: 'var(--violet)' }}>Промпт: {selected.custom_prompt.slice(0, 100)}...</div>}
                </div>
              </>
            )}
          </div>
        )}
      </Modal>

      {/* ── Subscribe Modal ────────────────────────────── */}
      {subscribeModal && selected && (
        <Modal open={true} title={`◎ Подписать: ${selected.name}`} onClose={() => setSubscribeModal(false)} width={480}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <div style={{ padding: '12px 14px', background: 'rgba(0,194,178,0.06)', border: '1px solid rgba(0,194,178,0.15)', borderRadius: 10, fontSize: 13, color: 'var(--text-2)', lineHeight: 1.7 }}>
              <strong>{(selected.account_ids || []).length}</strong> аккаунтов × <strong>{((selected.channels || []).length || selected.channels_count || 0).toLocaleString('ru')}</strong> каналов
              = <strong>{((selected.account_ids || []).length * ((selected.channels || []).length || selected.channels_count || 0)).toLocaleString('ru')}</strong> подписок
            </div>
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 8 }}>За сколько времени</label>
              <div style={{ display: 'flex', gap: 6 }}>
                {[{ min: 30, label: '30м' }, { min: 60, label: '1ч' }, { min: 120, label: '2ч' }, { min: 240, label: '4ч' }, { min: 480, label: '8ч' }].map(t => (
                  <button key={t.min} onClick={() => setSubTime(t.min)} style={{
                    flex: 1, padding: '10px', borderRadius: 8, cursor: 'pointer', textAlign: 'center',
                    background: subTime === t.min ? 'rgba(0,194,178,0.12)' : 'var(--bg-3)',
                    border: `1px solid ${subTime === t.min ? 'rgba(0,194,178,0.35)' : 'var(--border)'}`,
                    color: subTime === t.min ? '#00c2b2' : 'var(--text-2)',
                    fontSize: 13, fontWeight: 600, transition: 'all 0.15s',
                  }}>{t.label}</button>
                ))}
              </div>
            </div>
            <Input label="Или введи вручную (минуты)" type="number" value={subTime} onChange={e => setSubTime(parseInt(e.target.value) || 0)} />
            <div style={{ display: 'flex', gap: 10 }}>
              <Button variant="ghost" onClick={() => setSubscribeModal(false)} style={{ flex: 1 }}>Отмена</Button>
              <Button variant="primary" onClick={handleSubscribe} disabled={subRunning} style={{ flex: 1 }}>
                {subRunning ? '⏳ Запуск...' : `◎ Подписать за ~${subTime >= 60 ? Math.round(subTime / 60) + 'ч' : subTime + 'м'}`}
              </Button>
            </div>
          </div>
        </Modal>
      )}

      {/* ── Add Channels Modal ─────────────────────────── */}
      <Modal open={channelModal} onClose={() => setChannelModal(false)} title="Добавить каналы" width={520}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {/* Импорт из прогрева — pre-joined каналы (anti-ban). Показываем ВСЕГДА. */}
          <div style={{ padding: '14px', background: 'rgba(0,194,178,0.06)', border: '1px solid rgba(0,194,178,0.2)', borderRadius: 10 }}>
            <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--teal)', marginBottom: 4 }}>🔥 Импорт из прогрева (рекомендуется)</div>
            <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 10, lineHeight: 1.6 }}>
              Аккаунты уже подписаны на эти каналы за прогрев. При старте они будут commentable
              <strong> с 1-го дня</strong> — без вступления и не сразу после подписки. Меньше банов.
            </div>
            {warmupBatches.length === 0 ? (
              <div style={{ fontSize: 11, color: 'var(--text-3)', fontStyle: 'italic' }}>
                Нет прогревов. Создай прогрев с каналами на вкладке «Прогрев» — после подписок они появятся тут.
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {warmupBatches.map(b => {
                  const allDone = b.statuses.every(s => s === 'finished')
                  const noSubs = b.subscribed === 0
                  return (
                    <Button key={b.batch_id} variant="outline" size="sm" loading={saving}
                      disabled={noSubs}
                      onClick={async () => {
                        if (noSubs) return
                        setSaving(true)
                        try {
                          const { data } = await commentingAPI.importFromWarmup(selected.id, b.batch_id)
                          showToast(data.message || `Импортировано ${data.imported_channels} каналов`)
                          setChannelModal(false); await load()
                        } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
                        setSaving(false)
                      }}>
                      {noSubs ? '⏳' : allDone ? '✅' : '▶'} {b.batch_name} · {b.subscribed}/{b.target} подписок · {b.accounts} акк.
                      {noSubs ? ' (ещё не подписались)' : ''}
                    </Button>
                  )
                })}
              </div>
            )}
          </div>
          <div style={{ padding: '14px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 10 }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-2)', marginBottom: 8 }}>📁 Добавить из папки</div>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              {channelFolders.length === 0 ? (
                <span style={{ fontSize: 11, color: 'var(--text-3)' }}>Нет папок. Создайте в Парсере.</span>
              ) : channelFolders.map(f => (
                <Button key={f.name} variant="outline" size="sm" loading={saving} onClick={async () => {
                  setSaving(true)
                  try {
                    const { data } = await commentingAPI.addChannelsFromFolder(selected.id, f.name)
                    showToast(`Добавлено ${data.added} каналов из "${f.name}"`)
                    setChannelModal(false); await load()
                  } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
                  setSaving(false)
                }}>{f.name} ({f.count})</Button>
              ))}
            </div>
          </div>
          <div style={{ textAlign: 'center', fontSize: 11, color: 'var(--text-3)' }}>— или вручную —</div>
          <textarea value={channelText} onChange={e => setChannelText(e.target.value)} rows={5}
            placeholder={"@crypto_news\n@blockchain_ru\nhttps://t.me/bitcoin_channel"}
            style={{ width: '100%', padding: '12px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 13, fontFamily: 'var(--font-mono)', resize: 'vertical', outline: 'none' }} />
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" onClick={() => setChannelModal(false)}>Отмена</Button>
            <Button variant="primary" loading={saving} onClick={handleAddChannels}>Добавить</Button>
          </div>
        </div>
      </Modal>

      {/* ══ Errors Modal ══ */}
      <Modal open={errorsModal} onClose={() => setErrorsModal(false)}
        title="🐞 Ошибки аккаунтов — комментинг" width={920}>
        <AccountErrorsPanel sourceFilter="commenting" />
      </Modal>
    </div>
  )
}
