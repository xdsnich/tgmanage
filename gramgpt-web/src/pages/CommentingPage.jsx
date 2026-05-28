import { useEffect, useState } from 'react'
import { commentingAPI, accountsAPI, parserAPI, subscribeAPI, warmupAPI } from '../services/api'
import { Card, Button, Input, Modal, Badge, Spinner, Empty, StatCard } from '../components/ui'
import { useAutoRefresh } from '../hooks/useAutoRefresh'

const TONES = [
  { value: 'positive',   label: 'Позитивный',         icon: '😊' },
  { value: 'negative',   label: 'Критичный',           icon: '🤨' },
  { value: 'question',   label: 'Вопрос автору',       icon: '❓' },
  { value: 'analytical', label: 'Аналитический',       icon: '🧐' },
  { value: 'short',      label: 'Краткий (2-3 слова)', icon: '⚡' },
  { value: 'custom',     label: 'Кастомный промпт',    icon: '✍️' },
]

const TRIGGERS = [
  { value: 'all',      label: 'Каждый пост'        },
  { value: 'random',   label: 'Случайный %'        },
  { value: 'keywords', label: 'По ключевым словам' },
]

const STATUS_COLORS = {
  draft: 'default', active: 'green', paused: 'yellow', stopped: 'red', finished: 'blue',
}
const STATUS_LABELS = {
  draft: 'Черновик', active: 'Активна', paused: 'Пауза', stopped: 'Остановлена', finished: 'Завершена',
}

// ── Plan helpers ──────────────────────────────────────────────
const ACTION_ICONS = {
  join_target_channel: '📢', join_channel: '📡',
  smart_comment: '💬', read_feed: '📖', set_reaction: '😍',
  view_stories: '👁', view_profile: '👤', search: '🔍',
  send_saved: '💾', forward_saved: '📤', reply_dm: '↩️',
  typing: '⌨️', idle: '⏸',
}

const actionLabel = (a) => {
  switch (a.type) {
    case 'join_target_channel': return `Вступает в @${a.channel}`
    case 'join_channel':        return `Вступает в @${a.channel || '...'} (фон)`
    case 'smart_comment':       return `Комментирует @${a.channel}`
    case 'read_feed':           return `Читает ${a.channel ? `@${a.channel}` : 'ленту'} (${a.count || 5} постов)`
    case 'set_reaction':        return `Реакция ${a.emoji || '👍'}${a.channel ? ` в @${a.channel}` : ''}`
    case 'view_stories':        return `Stories (${a.count || 3})`
    case 'view_profile':        return 'Просматривает профиль'
    case 'search':              return 'Поиск в Telegram'
    case 'send_saved':          return `Saved: "${a.text || '...'}"`
    case 'forward_saved':       return 'Форвард в Saved'
    case 'reply_dm':            return 'Читает ЛС'
    case 'typing':              return `Typing ${a.duration || 5}с`
    case 'idle':                return `Пауза ${a.duration || 60}с`
    default:                    return a.type
  }
}

const actionColor = (a) => {
  if (a.type === 'join_target_channel') return 'var(--blue)'
  if (a.type === 'smart_comment')       return 'var(--green)'
  if (a.type === 'set_reaction')        return '#ff9f43'
  if (a.type === 'idle')               return 'var(--text-3)'
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
  const [campaigns, setCampaigns]           = useState([])
  const [accounts, setAccounts]             = useState([])
  const [loading, setLoading]               = useState(true)
  const [tab, setTab]                       = useState('campaigns')
  const [logs, setLogs]                     = useState([])
  const [logsLoading, setLogsLoading]       = useState(false)
  const [selected, setSelected]             = useState(null)
  const [createModal, setCreateModal]       = useState(false)
  const [detailModal, setDetailModal]       = useState(false)
  const [channelModal, setChannelModal]     = useState(false)
  const [subscribeModal, setSubscribeModal] = useState(false)
  const [saving, setSaving]                 = useState(false)
  const [toast, setToast]                   = useState(null)

  // Subscribe
  const [subscribeTasks, setSubscribeTasks] = useState([])
  const [subTime, setSubTime]               = useState(240)
  const [subRunning, setSubRunning]         = useState(false)

  // Create form
  const [form, setForm] = useState({
    name: '', account_ids: [], trigger_mode: 'all', trigger_percent: 50,
    trigger_keywords: '', llm_provider: 'claude', tone: 'positive',
    custom_prompt: '', comment_length: 'medium',
    max_comments: 100, max_hours: 24,
    delay_join: 10, delay_comment: 250, delay_between: 60,
  })

  const [channelText, setChannelText]     = useState('')
  const [channelFolders, setChannelFolders] = useState([])
  const [warmupBatches, setWarmupBatches] = useState([])

  // Activity log
  const [activity, setActivity]           = useState([])
  const [activityLoading, setActivityLoading] = useState(false)
  const [detailTab, setDetailTab]         = useState('info')

  // Plans
  const [plans, setPlans]                   = useState([])
  const [plansLoading, setPlansLoading]     = useState(false)
  const [openDays, setOpenDays]             = useState(new Set())    // Set<dayNumber>
  const [openSessions, setOpenSessions]     = useState(new Set())   // Set<"planId-sessIdx">

  const showToast = (text, type = 'success') => {
    setToast({ text, type }); setTimeout(() => setToast(null), 3500)
  }

  const load = async () => {
    setLoading(true)
    try {
      const [c, a, st] = await Promise.all([
        commentingAPI.list(),
        accountsAPI.list(),
        subscribeAPI.list().catch(() => ({ data: [] })),
      ])
      setCampaigns(c.data)
      setAccounts(a.data.filter(acc => acc.status === 'active'))
      setSubscribeTasks(st.data || [])
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
    if (selected && detailModal) {
      const updated = c.data.find(x => x.id === selected.id)
      if (updated) setSelected(updated)
    }
  }, 15000)

  useAutoRefresh(
    () => loadActivity(selected?.id, true),
    10000,
    detailModal && detailTab === 'activity' && !!selected
  )
  useAutoRefresh(
    () => loadPlans(selected?.id),
    30000,
    detailModal && detailTab === 'plans' && !!selected
  )

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

  const loadPlans = async (campaignId) => {
    if (!campaignId) return
    setPlansLoading(true)
    try {
      const { data } = await commentingAPI.plans(campaignId)
      setPlans(data)
      // Автоматически открываем первый день
      if (data.length > 0) {
        const days = [...new Set(data.map(p => p.day_number))]
        setOpenDays(new Set([days[0]]))
      }
    } catch { setPlans([]) }
    setPlansLoading(false)
  }

  const toggleDay     = (day) => setOpenDays(prev => { const s = new Set(prev); s.has(day) ? s.delete(day) : s.add(day); return s })
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
      setForm({ name: '', account_ids: [], trigger_mode: 'all', trigger_percent: 50,
        trigger_keywords: '', llm_provider: 'claude', tone: 'positive', custom_prompt: '',
        comment_length: 'medium', max_comments: 100, max_hours: 24,
        delay_join: 10, delay_comment: 250, delay_between: 60 })
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setSaving(false)
  }

  const handleAction = async (id, action) => {
    try {
      if (action === 'start')       await commentingAPI.start(id)
      else if (action === 'pause')  await commentingAPI.pause(id)
      else if (action === 'stop')   await commentingAPI.stop(id)
      else if (action === 'delete') {
        if (!window.confirm('Удалить кампанию?')) return
        await commentingAPI.delete(id)
      }
      showToast(action === 'delete' ? 'Удалена' : `Кампания: ${action}`)
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

  const openSubscribe = (campaign) => { setSelected(campaign); setSubTime(240); setSubscribeModal(true) }

  const handleSubscribe = async () => {
    if (!selected) return
    setSubRunning(true)
    try {
      const channels = (selected.channels || []).map(ch => '@' + ch.username)
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
    if (!campaign.channels?.length) return null
    const names = campaign.channels.map(ch => ch.username)
    return subscribeTasks.find(st => st.status === 'running' && st.channels?.some(ch => names.includes(ch)))
        || subscribeTasks.find(st => st.status === 'done'    && st.channels?.some(ch => names.includes(ch)))
        || null
  }

  const openDetail = (c) => {
    setSelected(c)
    setDetailTab('info')
    setActivity([])
    setPlans([])
    setOpenDays(new Set())
    setOpenSessions(new Set())
    setDetailModal(true)
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
        <Button variant="primary" onClick={() => setCreateModal(true)}>+ Создать кампанию</Button>
      </div>

      {/* Main Tabs */}
      <div style={{ display: 'flex', gap: 4, marginBottom: 20, background: 'var(--bg-2)', padding: 4, borderRadius: 12, border: '1px solid var(--border)', width: 'fit-content' }}>
        {[
          { key: 'campaigns', label: '📋 Кампании' },
          { key: 'logs',      label: '📝 История комментов' },
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
              <StatCard label="Всего кампаний"  value={campaigns.length} icon="📋" />
              <StatCard label="Активных"         value={campaigns.filter(c => c.status === 'active').length} color="var(--green)" icon="▶" />
              <StatCard label="Комментариев"     value={campaigns.reduce((s, c) => s + (c.comments_sent || 0), 0)} color="var(--violet)" icon="💬" />
              <StatCard label="Каналов"          value={campaigns.reduce((s, c) => s + (c.channels_count || 0), 0)} color="var(--blue)" icon="📢" />
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
                        {(c.status === 'draft' || c.status === 'paused' || c.status === 'stopped') && (
                          <Button variant="primary" size="sm" onClick={() => handleAction(c.id, 'start')}>▶ Старт</Button>
                        )}
                        {c.status === 'active' && (
                          <>
                            <Button variant="ghost" size="sm" onClick={() => handleAction(c.id, 'pause')}>⏸ Пауза</Button>
                            <Button variant="danger" size="sm" onClick={() => handleAction(c.id, 'stop')}>⏹ Стоп</Button>
                          </>
                        )}
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

          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Аккаунты</label>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {accounts.map(a => (
                <button key={a.id} onClick={() => setForm(f => ({ ...f, account_ids: f.account_ids.includes(a.id) ? f.account_ids.filter(x => x !== a.id) : [...f.account_ids, a.id] }))} style={{
                  padding: '6px 12px', borderRadius: 8, fontSize: 12, cursor: 'pointer', transition: 'all 0.15s',
                  background: form.account_ids.includes(a.id) ? 'rgba(124,77,255,0.2)' : 'var(--bg-3)',
                  border: `1px solid ${form.account_ids.includes(a.id) ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                  color: form.account_ids.includes(a.id) ? 'var(--violet)' : 'var(--text-2)',
                }}>{a.first_name || a.phone}</button>
              ))}
              {accounts.length === 0 && <div style={{ fontSize: 12, color: 'var(--text-3)' }}>Нет активных аккаунтов</div>}
            </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>LLM</label>
              <select value={form.llm_provider} onChange={e => setForm(f => ({ ...f, llm_provider: e.target.value }))} style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 14, outline: 'none' }}>
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

          {form.trigger_mode === 'random'   && <Input label="Процент постов (%)"                type="number" value={form.trigger_percent}  onChange={e => setForm(f => ({ ...f, trigger_percent:  parseInt(e.target.value) || 0 }))} />}
          {form.trigger_mode === 'keywords' && <Input label="Ключевые слова (через запятую)" value={form.trigger_keywords} onChange={e => setForm(f => ({ ...f, trigger_keywords: e.target.value }))} placeholder="крипта, блокчейн, биткоин" />}

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 }}>
            <Input label="Макс. комментов"       type="number" value={form.max_comments}  onChange={e => setForm(f => ({ ...f, max_comments:   parseInt(e.target.value) || 0 }))} />
            <Input label="Макс. часов"            type="number" value={form.max_hours}     onChange={e => setForm(f => ({ ...f, max_hours:       parseInt(e.target.value) || 0 }))} />
            <Input label="Задержка коммент. (сек)" type="number" value={form.delay_comment} onChange={e => setForm(f => ({ ...f, delay_comment:  parseInt(e.target.value) || 0 }))} />
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <Input label="Задержка входа (сек)"   type="number" value={form.delay_join}    onChange={e => setForm(f => ({ ...f, delay_join:     parseInt(e.target.value) || 0 }))} />
            <Input label="Между комментами (сек)" type="number" value={form.delay_between} onChange={e => setForm(f => ({ ...f, delay_between:  parseInt(e.target.value) || 0 }))} />
          </div>

          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 4 }}>
            <Button variant="ghost" onClick={() => setCreateModal(false)}>Отмена</Button>
            <Button variant="primary" loading={saving} disabled={!form.name} onClick={handleCreate}>Создать</Button>
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
                { key: 'info',     label: '📋 Инфо'      },
                { key: 'plans',    label: '📅 Планы'     },
                { key: 'activity', label: '🔍 Активность' },
              ].map(t => (
                <button key={t.key} onClick={() => {
                  setDetailTab(t.key)
                  if (t.key === 'activity' && activity.length === 0) loadActivity(selected.id)
                  if (t.key === 'plans'    && plans.length === 0)    loadPlans(selected.id)
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
                          const donePlans     = dayPlans.filter(p => p.status === 'done').length

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
                                  {totalComments > 0 && <span style={{ color: 'var(--green)' }}>💬 {totalComments}</span>}
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
                                    const sessions  = plan.sessions || []
                                    const doneCount = plan.executed_idx || 0
                                    const isDone    = plan.status === 'done'

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
                                          <div style={{ marginLeft: 'auto', fontSize: 11, color: isDone ? 'var(--green)' : 'var(--text-3)' }}>
                                            {isDone ? '✅ Выполнено' : `${doneCount}/${sessions.length} сессий`}
                                          </div>
                                        </div>

                                        {/* Сессии */}
                                        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                                          {sessions.map((sess, si) => {
                                            const sessKey    = `${plan.id}-${si}`
                                            const isExpanded = openSessions.has(sessKey)
                                            const sessStatus = sess.skipped
                                              ? 'skip'
                                              : si < doneCount ? 'done'
                                              : si === doneCount && !isDone ? 'current'
                                              : 'pending'

                                            const bgColors = {
                                              done:    'rgba(61,214,140,0.06)',
                                              current: 'rgba(124,77,255,0.08)',
                                              skip:    'rgba(255,255,255,0.02)',
                                              pending: 'transparent',
                                            }
                                            const borderColors = {
                                              done:    'rgba(61,214,140,0.2)',
                                              current: 'rgba(124,77,255,0.3)',
                                              skip:    'var(--border)',
                                              pending: 'var(--border)',
                                            }
                                            const statusIcon = {
                                              done:    '✅', current: '⏳', skip: '💤', pending: '○',
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

            {/* ── Info Tab ── */}
            {detailTab === 'info' && (
              <>
                {(selected.channels || []).length > 0 && selected.status !== 'active' && (
                  <div style={{ padding: '14px 16px', borderRadius: 10, background: 'rgba(0,194,178,0.06)', border: '1px solid rgba(0,194,178,0.15)' }}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
                      <span style={{ fontWeight: 700, fontSize: 13 }}>◎ Предподписка</span>
                      <Button variant="outline" size="sm" onClick={() => openSubscribe(selected)}>Подписать аккаунты</Button>
                    </div>
                    <div style={{ fontSize: 12, color: 'var(--text-3)' }}>
                      Подпиши {(selected.account_ids || []).length} аккаунтов на {(selected.channels || []).length} каналов перед запуском
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
                    <span style={{ fontWeight: 700, fontSize: 13 }}>Целевые каналы ({(selected.channels || []).length})</span>
                    <Button variant="outline" size="sm" onClick={async () => {
                      setChannelModal(true)
                      try { const { data } = await parserAPI.folders(); setChannelFolders(data) } catch { }
                      // Грузим батчи прогрева с подписками (для импорта pre-joined каналов)
                      try {
                        const { data: tasks } = await warmupAPI.list()
                        const byBatch = {}
                        for (const t of tasks) {
                          if (!t.batch_id || (t.subscribed_count || 0) === 0) continue
                          if (!byBatch[t.batch_id]) byBatch[t.batch_id] = { batch_id: t.batch_id, batch_name: t.batch_name || t.batch_id, accounts: 0, subscribed: 0, statuses: [] }
                          byBatch[t.batch_id].accounts += 1
                          byBatch[t.batch_id].subscribed += (t.subscribed_count || 0)
                          byBatch[t.batch_id].statuses.push(t.status)
                        }
                        setWarmupBatches(Object.values(byBatch))
                      } catch { setWarmupBatches([]) }
                    }}>+ Добавить</Button>
                  </div>
                  {(selected.channels || []).length === 0 ? (
                    <div style={{ fontSize: 12, color: 'var(--text-3)', textAlign: 'center', padding: '16px 0' }}>Нет каналов — добавьте для запуска</div>
                  ) : (selected.channels || []).map(ch => (
                    <div key={ch.id} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '8px 12px', background: 'var(--bg-3)', borderRadius: 8, marginBottom: 4 }}>
                      <div>
                        <span style={{ fontWeight: 600, fontSize: 13 }}>@{ch.username}</span>
                        <span style={{ fontSize: 11, color: 'var(--text-3)', marginLeft: 8 }}>💬 {ch.comments_sent}</span>
                      </div>
                      <button onClick={() => handleRemoveChannel(selected.id, ch.id)} style={{ background: 'none', border: 'none', color: 'var(--red)', cursor: 'pointer', fontSize: 14 }}>✕</button>
                    </div>
                  ))}
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
              <strong>{(selected.account_ids || []).length}</strong> аккаунтов × <strong>{(selected.channels || []).length}</strong> каналов
              = <strong>{(selected.account_ids || []).length * (selected.channels || []).length}</strong> подписок
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
          {/* Импорт из прогрева — pre-joined каналы (anti-ban) */}
          {warmupBatches.length > 0 && (
            <div style={{ padding: '14px', background: 'rgba(0,194,178,0.06)', border: '1px solid rgba(0,194,178,0.2)', borderRadius: 10 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--teal)', marginBottom: 4 }}>🔥 Импорт из прогрева (рекомендуется)</div>
              <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 10, lineHeight: 1.6 }}>
                Аккаунты уже подписаны на эти каналы за прогрев. При старте они будут commentable
                <strong> с 1-го дня</strong> — без вступления и не сразу после подписки. Меньше банов.
              </div>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                {warmupBatches.map(b => {
                  const allDone = b.statuses.every(s => s === 'finished')
                  return (
                    <Button key={b.batch_id} variant="outline" size="sm" loading={saving} onClick={async () => {
                      setSaving(true)
                      try {
                        const { data } = await commentingAPI.importFromWarmup(selected.id, b.batch_id)
                        showToast(data.message || `Импортировано ${data.imported_channels} каналов`)
                        setChannelModal(false); await load()
                      } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
                      setSaving(false)
                    }}>
                      {allDone ? '✅' : '▶'} {b.batch_name} · {b.subscribed} подписок / {b.accounts} акк.
                    </Button>
                  )
                })}
              </div>
            </div>
          )}
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
    </div>
  )
}
