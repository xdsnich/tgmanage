import { useEffect, useState, useRef } from 'react'
import { warmupAPI, accountsAPI, commentingAPI, serviceCredentialsAPI } from '../services/api'
import { Card, Button, Input, Modal, Badge, Spinner, Empty } from '../components/ui'
import { useAutoRefresh } from '../hooks/useAutoRefresh'

const MODE_INFO = {
  careful: { label: '🐢 Осторожный', desc: 'Мало действий, большие паузы', color: '#3dd68c' },
  normal: { label: '👤 Нормальный', desc: 'Как обычный пользователь', color: '#3d8bff' },
  aggressive: { label: '⚡ Агрессивный', desc: 'Больше действий, быстрее', color: '#ff6b35' },
}

export default function WarmupPage() {
  const [tasks, setTasks] = useState([])
  const [accounts, setAccounts] = useState([])
  const [logs, setLogs] = useState([])
  const [loading, setLoading] = useState(true)
  const [createModal, setCreateModal] = useState(false)
  const [logsModal, setLogsModal] = useState(false)
  const [selectedTask, setSelectedTask] = useState(null)
  const [channelsModal, setChannelsModal] = useState(false)
  const [chText, setChText] = useState('')
  const [chSaving, setChSaving] = useState(false)
  const [chBatch, setChBatch] = useState(null)      // {batch_id, batch_name}
  const [chPool, setChPool] = useState(null)        // [{channel, tasks_count, subscribed_count}]
  const [chPoolLoading, setChPoolLoading] = useState(false)

  const openChannelsModal = async (batch) => {
    setChBatch(batch)
    setChText('')
    setChPool(null)
    setChannelsModal(true)
    setChPoolLoading(true)
    try {
      // Для одиночных задач (без реального batch_id) у нас на фронте
      // создаётся виртуальный id "single_<task_id>". В этом случае —
      // показываем target_channels из самой таски (батча на бэке нет).
      const isSingle = String(batch.batch_id || '').startsWith('single_')
      if (isSingle) {
        const t = (batch.tasks || [])[0]
        const targets = (t?.target_channels || [])  // если поле есть
        const subbed = t?.subscribed_channels || {}
        const subbedLow = new Set(Object.keys(subbed).map(s => s.toLowerCase()))
        const pool = targets.map(c => ({
          channel: c.replace(/^@/, ''),
          tasks_count: 1,
          subscribed_count: subbedLow.has(c.replace(/^@/, '').toLowerCase()) ? 1 : 0,
        }))
        setChPool(pool)
        setChText(pool.map(p => '@' + p.channel).join('\n'))
        return
      }
      const { data } = await warmupAPI.batchChannels(batch.batch_id)
      const pool = data.channels || []
      setChPool(pool)
      setChText(pool.map(p => '@' + p.channel).join('\n'))
    } catch (e) {
      setChPool([])
      const status = e.response?.status
      const detail = e.response?.data?.detail || e.message
      // 404 чаще всего значит что uvicorn не подхватил новый код
      const hint = status === 404
        ? '\n\nЭто эндпоинт добавили недавно. Если только обновил код — перезапусти прогу:\n  .\\stop_all.ps1\n  .\\start_all.ps1'
        : ''
      alert(`Не удалось загрузить список каналов: ${detail}${hint}`)
    } finally {
      setChPoolLoading(false)
    }
  }

  const handleChannelsSave = async () => {
    if (!chBatch) return
    const channels = chText
      .split(/[\n,]+/)
      .map(s => s.trim().replace(/^@/, ''))
      .filter(Boolean)
    // Замена всем списком (action=replace) — юзер видит и правит финальный
    // вариант. Пустой список = убрать всё, на это нужен confirm.
    if (channels.length === 0) {
      if (!window.confirm('Список пуст — это удалит ВСЕ целевые каналы из прогрева. Продолжить?')) return
    }
    setChSaving(true)
    try {
      const isSingle = String(chBatch.batch_id || '').startsWith('single_')
      if (isSingle) {
        const t = (chBatch.tasks || [])[0]
        if (!t) { alert('Не найдена задача прогрева'); return }
        const { data } = await warmupAPI.editChannels(t.id, 'replace', channels)
        setChannelsModal(false)
        setChText('')
        await load()
        alert(
          `✅ Каналы обновлены\n\n` +
          `Было: ${data.old_count} → стало: ${data.new_count}\n` +
          `Перегенерировано будущих дней: ${data.future_days_regenerated}` +
          (data.warning ? `\n\n⚠ ${data.warning}` : '')
        )
        return
      }
      const { data } = await warmupAPI.editBatchChannels(chBatch.batch_id, 'replace', channels)
      setChannelsModal(false)
      setChText('')
      await load()
      alert(
        `✅ Каналы обновлены\n\n` +
        `Пул каналов: ${data.old_pool_count} → ${data.new_pool_count}\n` +
        `Аккаунтов обновлено: ${data.tasks_updated}\n` +
        `Перегенерировано будущих дней: ${data.future_days_regenerated}`
      )
    } catch (e) {
      const status = e.response?.status
      const detail = e.response?.data?.detail || e.message
      const hint = status === 404
        ? '\n\nПерезапусти прогу: .\\stop_all.ps1 + .\\start_all.ps1'
        : ''
      alert(`Ошибка: ${detail}${hint}`)
    } finally {
      setChSaving(false)
    }
  }
  const [taskLogs, setTaskLogs] = useState([])
  const [logsLoading, setLogsLoading] = useState(false)
  const [saving, setSaving] = useState(false)

  const [form, setForm] = useState({ account_ids: [], total_days: 7, mode: 'normal', target_channels: '', daily_join_max: 3 })
  const [onlyAlive, setOnlyAlive] = useState(true)
  const logsRef = useRef(null)

  // План прогрева (мониторинг)
  const [planModal, setPlanModal] = useState(false)
  const [planData, setPlanData] = useState(null)
  const [planLoading, setPlanLoading] = useState(false)

  // Сворачивание батчей (компаний прогрева)
  const [expandedBatches, setExpandedBatches] = useState({})
  const toggleBatch = (bid) => setExpandedBatches(s => ({ ...s, [bid]: !s[bid] }))

  // Schedule-campaign modal
  const [scheduleModal, setScheduleModal] = useState(false)
  const [scheduleBatch, setScheduleBatch] = useState(null) // вся группа { batch_id, batch_name, tasks }
  const [scheduleLlmCreds, setScheduleLlmCreds] = useState([])
  const [scheduleSaving, setScheduleSaving] = useState(false)
  const [scheduleResult, setScheduleResult] = useState(null)
  const [scheduleForm, setScheduleForm] = useState({
    name: '',
    trigger_mode: 'all',
    trigger_percent: 50,
    trigger_keywords: '',
    llm_provider: 'claude',
    llm_credential_id: null,
    tone: 'positive',
    custom_prompt: '',
    comment_length: 'medium',
    max_comments: 100,
    max_hours: 24,
    delay_join: 10,
    delay_comment: 250,
    delay_between: 60,
  })

  const openScheduleModal = async (group) => {
    setScheduleBatch(group)
    setScheduleResult(null)
    setScheduleForm(f => ({ ...f, name: `Кампания: ${group.batch_name}` }))
    try {
      const { data } = await serviceCredentialsAPI.list()
      setScheduleLlmCreds((data || []).filter(k => k.is_active))
    } catch { setScheduleLlmCreds([]) }
    setScheduleModal(true)
  }

  const submitSchedule = async () => {
    if (!scheduleBatch) return
    if (!scheduleForm.name.trim()) { alert('Название кампании не может быть пустым'); return }
    setScheduleSaving(true)
    try {
      const payload = {
        batch_id: scheduleBatch.batch_id,
        ...scheduleForm,
        trigger_keywords: scheduleForm.trigger_keywords
          ? scheduleForm.trigger_keywords.split(',').map(s => s.trim()).filter(Boolean) : [],
      }
      const { data } = await commentingAPI.scheduleAfterWarmup(payload)
      setScheduleResult(data)
    } catch (err) {
      alert(err.response?.data?.detail || 'Ошибка планирования кампании')
    }
    setScheduleSaving(false)
  }

  // Группируем задачи по batch_id, сохраняя порядок (новые сверху)
  const batchGroups = (() => {
    const order = []
    const map = {}
    for (const t of tasks) {
      const bid = t.batch_id || `single_${t.id}`
      if (!map[bid]) {
        map[bid] = { batch_id: bid, batch_name: t.batch_name || 'Прогрев', tasks: [] }
        order.push(bid)
      }
      map[bid].tasks.push(t)
    }
    return order.map(bid => map[bid])
  })()

  // Массовые действия для батча
  const handleBatchAction = async (batchTasks, action) => {
    for (const t of batchTasks) {
      try {
        if (action === 'pause' && t.status === 'running') await warmupAPI.pause(t.id)
        else if (action === 'resume' && t.status === 'paused') await warmupAPI.start(t.id)
        else if (action === 'stop' && t.status === 'running') await warmupAPI.stop(t.id)
        else if (action === 'start' && t.status === 'idle') await warmupAPI.start(t.id)
      } catch { }
    }
    await load()
  }

  const handleDeleteBatch = async (group) => {
    if (!window.confirm(`Удалить весь прогрев "${group.batch_name}" (${group.tasks.length} акк.) со всеми логами?`)) return
    try {
      // batch_id вида single_N — это одиночная задача без батча, удаляем как задачу
      if (group.batch_id.startsWith('single_')) {
        for (const t of group.tasks) await warmupAPI.delete(t.id)
      } else {
        await warmupAPI.deleteBatch(group.batch_id)
      }
      await load()
    } catch (err) { alert(err.response?.data?.detail || 'Ошибка') }
  }

  const openPlan = async (task) => {
    setSelectedTask(task); setPlanModal(true); setPlanLoading(true); setPlanData(null)
    try { const { data } = await warmupAPI.plan(task.id); setPlanData(data) }
    catch { setPlanData(null) }
    setPlanLoading(false)
  }

  const load = async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      const [tasksRes, accsRes, logsRes] = await Promise.all([
        warmupAPI.list(),
        accountsAPI.list(),
        warmupAPI.liveLogs().catch(() => ({ data: [] })),
      ])
      setTasks(tasksRes.data)
      setAccounts(accsRes.data)
      setLogs(logsRes.data || [])
    } catch (err) { console.error(err) }
    if (!silent) setLoading(false)
  }

  useEffect(() => { load() }, [])
  useAutoRefresh(() => load(true), 15000)

  // Авто-обновление логов каждые 15с
  useEffect(() => {
    const iv = setInterval(async () => {
      try {
        const [tasksRes, logsRes] = await Promise.all([
          warmupAPI.list(),
          warmupAPI.liveLogs(),
        ])
        setTasks(tasksRes.data)
        setLogs(logsRes.data || [])
      } catch { }
    }, 35000)
    return () => clearInterval(iv)
  }, [])

  const handleCreate = async () => {
    if (!form.account_ids.length) { alert('Выбери аккаунты'); return }
    setSaving(true)
    try {
      // Парсим каналы: по строкам/запятым/пробелам, убираем @ и пустые
      const channels = (form.target_channels || '')
        .split(/[\s,\n]+/)
        .map(c => c.replace(/^@/, '').replace(/^https?:\/\/t\.me\//i, '').trim())
        .filter(Boolean)
      const dailyMax = Math.max(0, parseInt(form.daily_join_max) || 0)
      const { data } = await warmupAPI.create({
        account_ids: form.account_ids,
        total_days: parseInt(form.total_days) || 7,
        mode: form.mode,
        target_channels: channels,
        daily_join_min: 0,
        daily_join_max: dailyMax,
      })
      alert(data.message + (channels.length ? `\nDrip-подписка: ${channels.length} каналов, до ${dailyMax}/день` : ''))
      setCreateModal(false)
      setForm({ account_ids: [], total_days: 7, mode: 'normal', target_channels: '', daily_join_max: 3 })
      await load()
    } catch (err) { alert(err.response?.data?.detail || 'Ошибка') }
    setSaving(false)
  }

  const handleStart = async (taskId) => {
    try { await warmupAPI.start(taskId); await load() }
    catch (err) { alert(err.response?.data?.detail || 'Ошибка') }
  }

  const handleStartAll = async () => {
    try {
      const { data } = await warmupAPI.startAll()
      alert(data.message); await load()
    } catch (err) { alert(err.response?.data?.detail || 'Ошибка') }
  }

  const handleStop = async (taskId) => {
    try { await warmupAPI.stop(taskId); await load() }
    catch (err) { alert(err.response?.data?.detail || 'Ошибка') }
  }

  const handleDelete = async (taskId) => {
    if (!window.confirm('Удалить задачу и все логи?')) return
    try { await warmupAPI.delete(taskId); await load() }
    catch (err) { alert(err.response?.data?.detail || 'Ошибка') }
  }

  const openLogs = async (task) => {
    setSelectedTask(task); setLogsModal(true); setLogsLoading(true)
    try {
      const { data } = await warmupAPI.taskLogs(task.id)
      setTaskLogs(data)
    } catch { setTaskLogs([]) }
    setLogsLoading(false)
  }

  // Видимый список — фильтруется по «живые» если тогл включён.
  const visibleAccounts = onlyAlive
    ? accounts.filter(a => a.status === 'active')
    : accounts

  const toggleAccount = (id) => setForm(f => ({
    ...f, account_ids: f.account_ids.includes(id) ? f.account_ids.filter(x => x !== id) : [...f.account_ids, id]
  }))
  const selectAll = () => setForm(f => ({
    // «Выбрать все» работает с ВИДИМЫМИ. Уже выбранные но скрытые сохраняем.
    ...f, account_ids: f.account_ids.length >= visibleAccounts.length
      ? f.account_ids.filter(id => !visibleAccounts.some(a => a.id === id))
      : [...new Set([...f.account_ids, ...visibleAccounts.map(a => a.id)])]
  }))

  const statusBadge = (t) => {
    if (t.status === 'running' && t.is_resting) return { color: 'yellow', label: '😴 Отдыхает' }
    if (t.status === 'running') return { color: 'green', label: '▶ Работает' }
    if (t.status === 'finished') return { color: 'default', label: '✅ Завершён' }
    if (t.status === 'idle') return { color: 'blue', label: '⏸ Ожидает' }
    if (t.status === 'paused') return { color: 'yellow', label: '⏸ На паузе' }
    return { color: 'default', label: t.status }
  }

  const runningCount = tasks.filter(t => t.status === 'running').length
  const idleCount = tasks.filter(t => t.status === 'idle').length
  const handlePause = async (taskId) => {
    try { await warmupAPI.pause(taskId); await load() }
    catch (err) { alert(err.response?.data?.detail || 'Ошибка') }
  }

  // Карточка одного аккаунта в прогреве
  const renderTaskCard = (t) => {
    const sb = statusBadge(t)
    const pct = t.today_limit ? Math.round((t.today_actions / t.today_limit) * 100) : 0
    return (
      <div key={t.id} style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: '16px 20px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{ fontSize: 18 }}>{t.is_resting ? '😴' : '🔥'}</div>
            <div>
              <div style={{ fontWeight: 700, fontSize: 13 }}>{t.account_name || t.account_phone}</div>
              <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
                День {t.day}/{t.total_days} · {MODE_INFO[t.mode]?.label || t.mode}
                {t.start_offset_min > 0 && t.status === 'idle' && ` · Старт +${t.start_offset_min} мин`}
              </div>
            </div>
          </div>
          <Badge color={sb.color}>{sb.label}</Badge>
        </div>

        {t.status === 'running' && (
          <div style={{ marginBottom: 10 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: 'var(--text-3)', marginBottom: 4 }}>
              <span>Сегодня: {t.today_actions}/{t.today_limit}</span>
              <span>Всего: {t.actions_done}</span>
            </div>
            <div style={{ height: 4, borderRadius: 2, background: 'rgba(255,255,255,0.06)', overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${Math.min(pct, 100)}%`, background: '#3dd68c', borderRadius: 2, transition: 'width 0.5s' }} />
            </div>
          </div>
        )}
        {t.status === 'running' && t.next_action_at && (
          <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 8, padding: '6px 10px', background: 'rgba(255,255,255,0.03)', borderRadius: 6 }}>
            ⏰ Следующая сессия: {new Date(t.next_action_at).toLocaleTimeString('ru', { hour: '2-digit', minute: '2-digit' })}
            {' '}({Math.max(0, Math.round((new Date(t.next_action_at) - new Date()) / 60000))} мин)
          </div>
        )}
        <div style={{ display: 'flex', gap: 12, fontSize: 11, color: 'var(--text-3)', marginBottom: 10 }}>
          <span>📖 {t.feeds_read}</span>
          <span>😍 {t.reactions_set}</span>
          <span>👁 {t.stories_viewed}</span>
          <span>📢 {t.channels_joined}</span>
          <span>📋 {t.logs_count} логов</span>
        </div>

        {t.target_count > 0 && (
          <div style={{ marginBottom: 10, padding: '8px 12px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 8 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, marginBottom: 4 }}>
              <span style={{ color: 'var(--violet)', fontWeight: 600 }}>📢 Drip-подписка</span>
              <span style={{ color: 'var(--text-3)' }}>
                {t.subscribed_count}/{t.target_count} каналов
                {t.status === 'running' && ` · сегодня ${t.joined_today}/${t.daily_join_max}`}
              </span>
            </div>
            <div style={{ height: 4, borderRadius: 2, background: 'rgba(255,255,255,0.06)', overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${t.target_count ? Math.round((t.subscribed_count / t.target_count) * 100) : 0}%`, background: 'var(--violet)', borderRadius: 2, transition: 'width 0.5s' }} />
            </div>
          </div>
        )}

        <div style={{ display: 'flex', gap: 6 }}>
          {t.status === 'idle' && <Button variant="primary" size="sm" onClick={() => handleStart(t.id)}>▶ Старт</Button>}
          {t.status === 'running' && (
            <>
              <Button variant="ghost" size="sm" onClick={() => handlePause(t.id)}>⏸ Пауза</Button>
              <Button variant="danger" size="sm" onClick={() => handleStop(t.id)}>⏹ Завершить</Button>
            </>
          )}
          {t.status === 'paused' && <Button variant="primary" size="sm" onClick={() => handleStart(t.id)}>▶ Продолжить</Button>}
          <Button variant="ghost" size="sm" onClick={() => openPlan(t)}>📅 План</Button>
          <Button variant="ghost" size="sm" onClick={() => openLogs(t)}>📋 Логи</Button>
          {t.status !== 'running' && <Button variant="ghost" size="sm" onClick={() => handleDelete(t.id)}>✕</Button>}
        </div>
      </div>
    )
  }

  return (
    <div style={{ padding: '28px 32px', animation: 'fadeUp 0.4s cubic-bezier(0.16,1,0.3,1)' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 28 }}>
        <div>
          <div style={{ fontSize: 11, color: '#3dd68c', fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>🔥 ПРОГРЕВ v2</div>
          <h1 style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.04em' }}>Прогрев аккаунтов</h1>
          <p style={{ fontSize: 13, color: 'var(--text-3)', marginTop: 4 }}>Умная имитация живого пользователя с расписанием и логами</p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {idleCount > 0 && <Button variant="ghost" onClick={handleStartAll}>▶ Запустить все ({idleCount})</Button>}
          <Button variant="primary" onClick={() => setCreateModal(true)}>+ Новый прогрев</Button>
        </div>
      </div>

      {/* Stats */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginBottom: 24 }}>
        {[
          { label: 'Всего задач', value: tasks.length, color: '#7c4dff' },
          { label: 'Работает', value: runningCount, color: '#3dd68c' },
          { label: 'Ожидает', value: idleCount, color: '#3d8bff' },
          { label: 'Действий всего', value: tasks.reduce((s, t) => s + (t.actions_done || 0), 0), color: '#ff6b35' },
        ].map(({ label, value, color }) => (
          <div key={label} style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: '18px 20px' }}>
            <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 6 }}>{label}</div>
            <div style={{ fontSize: 28, fontWeight: 800, color }}>{value}</div>
          </div>
        ))}
      </div>

      {/* Two columns: tasks + live logs */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        {/* Tasks */}
        <div>
          <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 12 }}>Задачи прогрева</div>
          {loading ? <Spinner size={24} /> : tasks.length === 0 ? (
            <Empty icon="🔥" title="Нет задач" subtitle="Создай прогрев для аккаунтов"
              action={<Button variant="primary" onClick={() => setCreateModal(true)}>+ Создать</Button>} />
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {batchGroups.map(g => {
                const isOpen = expandedBatches[g.batch_id]
                const running = g.tasks.filter(t => t.status === 'running').length
                const done = g.tasks.filter(t => t.status === 'finished').length
                const subTotal = g.tasks.reduce((s, t) => s + (t.subscribed_count || 0), 0)
                const targetTotal = g.tasks.reduce((s, t) => s + (t.target_count || 0), 0)
                return (
                  <div key={g.batch_id} style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', overflow: 'hidden' }}>
                    {/* Заголовок батча (кликабельный) */}
                    <div onClick={() => toggleBatch(g.batch_id)} style={{
                      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                      padding: '14px 18px', cursor: 'pointer', userSelect: 'none',
                      background: isOpen ? 'rgba(124,77,255,0.06)' : 'transparent',
                      transition: 'background 0.15s',
                    }}
                      onMouseEnter={e => { if (!isOpen) e.currentTarget.style.background = 'rgba(255,255,255,0.02)' }}
                      onMouseLeave={e => { if (!isOpen) e.currentTarget.style.background = 'transparent' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                        <span style={{ fontSize: 13, color: 'var(--text-3)', transition: 'transform 0.2s', transform: isOpen ? 'rotate(90deg)' : 'none' }}>▶</span>
                        <div>
                          <div style={{ fontWeight: 700, fontSize: 14 }}>🔥 {g.batch_name}</div>
                          <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2 }}>
                            {g.tasks.length} акк. · ▶ {running} работает{done > 0 ? ` · ✅ ${done} завершено` : ''}
                            {targetTotal > 0 && ` · 📢 ${subTotal}/${targetTotal} подписок`}
                          </div>
                        </div>
                      </div>
                      <div style={{ display: 'flex', gap: 6, alignItems: 'center' }} onClick={e => e.stopPropagation()}>
                        {running > 0 && <Button variant="ghost" size="sm" onClick={() => handleBatchAction(g.tasks, 'pause')}>⏸ Пауза всех</Button>}
                        {g.tasks.some(t => t.status === 'paused') && <Button variant="ghost" size="sm" onClick={() => handleBatchAction(g.tasks, 'resume')}>▶ Продолжить всех</Button>}
                        {g.tasks.some(t => t.status === 'idle') && <Button variant="primary" size="sm" onClick={() => handleBatchAction(g.tasks, 'start')}>▶ Старт всех</Button>}
                        {running > 0 && <Button variant="danger" size="sm" onClick={() => { if (window.confirm(`Завершить весь прогрев "${g.batch_name}" (${running} акк.)?`)) handleBatchAction(g.tasks, 'stop') }}>⏹ Стоп всех</Button>}
                        {(running > 0 || g.tasks.some(t => t.status === 'paused')) && subTotal > 0 && (
                          <Button variant="outline" size="sm" onClick={() => openScheduleModal(g)} title="Запланировать кампанию комментинга на момент окончания прогрева">
                            📅 Запланировать кампанию
                          </Button>
                        )}
                        <Button variant="ghost" size="sm" onClick={() => openChannelsModal(g)} title="Изменить список каналов для всех аккаунтов этого прогрева">
                          📢 Каналы
                        </Button>
                        {running === 0 && <Button variant="ghost" size="sm" onClick={() => handleDeleteBatch(g)} title="Удалить весь прогрев">🗑 Удалить</Button>}
                      </div>
                    </div>

                    {/* Развёрнутые карточки аккаунтов */}
                    {isOpen && (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 10, padding: '0 14px 14px' }}>
                        {g.tasks.map(t => renderTaskCard(t))}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>

        {/* Live logs */}
        <div>
          <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 12 }}>📡 Лайв-логи</div>
          <div ref={logsRef} style={{
            background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius)',
            padding: '12px 16px', maxHeight: 600, overflowY: 'auto',
          }}>
            {logs.length === 0 ? (
              <div style={{ fontSize: 12, color: 'var(--text-3)', textAlign: 'center', padding: '40px 0' }}>
                Пока нет логов. Запусти прогрев чтобы увидеть активность.
              </div>
            ) : logs.map(l => (
              <div key={l.id} style={{
                padding: '8px 10px', borderBottom: '1px solid var(--border)',
                display: 'flex', gap: 10, alignItems: 'flex-start',
              }}>
                <div style={{ fontSize: 16, flexShrink: 0, marginTop: 2 }}>
                  {l.success ? (l.action === 'error' ? '❌' : '✅') : '❌'}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 2 }}>
                    <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)' }}>{l.account_name}</span>
                    <span style={{ fontSize: 11, color: 'var(--violet)' }}>{l.action_label}</span>
                    {l.emoji && <span style={{ fontSize: 14 }}>{l.emoji}</span>}
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
                    {l.detail}
                    {l.channel && <span style={{ color: 'var(--blue)' }}> · @{l.channel}</span>}
                  </div>
                  {l.error && <div style={{ fontSize: 11, color: 'var(--red)', marginTop: 2 }}>{l.error}</div>}
                </div>
                <div style={{ fontSize: 10, color: 'var(--text-3)', flexShrink: 0, fontFamily: 'var(--font-mono)' }}>
                  {new Date(l.created_at).toLocaleTimeString('ru', { hour: '2-digit', minute: '2-digit' })}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* ══ Create Modal ══ */}
      {createModal && (
        <Modal open={true} title="Новый прогрев" onClose={() => setCreateModal(false)} width={580}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16, maxHeight: '70vh', overflow: 'auto' }}>
            {/* Accounts */}
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
                  Аккаунты ({form.account_ids.length} / {visibleAccounts.length})
                </label>
                <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
                  <label style={{ fontSize: 11, color: 'var(--text-2)', display: 'flex', alignItems: 'center', gap: 5, cursor: 'pointer' }}>
                    <input type="checkbox" checked={onlyAlive} onChange={e => setOnlyAlive(e.target.checked)} />
                    Только живые
                  </label>
                  <button onClick={selectAll} style={{ fontSize: 11, color: 'var(--violet)', background: 'none', border: 'none', cursor: 'pointer' }}>
                    {form.account_ids.length >= visibleAccounts.length ? 'Снять видимые' : 'Выбрать видимые'}
                  </button>
                </div>
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, maxHeight: 120, overflowY: 'auto' }}>
                {visibleAccounts.map(a => (
                  <button key={a.id} onClick={() => toggleAccount(a.id)} style={{
                    padding: '6px 12px', borderRadius: 8, fontSize: 12, cursor: 'pointer',
                    background: form.account_ids.includes(a.id) ? 'rgba(124,77,255,0.2)' : 'var(--bg-3)',
                    border: `1px solid ${form.account_ids.includes(a.id) ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                    color: form.account_ids.includes(a.id) ? 'var(--violet)' : 'var(--text-2)', transition: 'all 0.15s',
                  }}>{a.first_name || a.phone}</button>
                ))}
                {visibleAccounts.length === 0 && (
                  <span style={{ fontSize: 11, color: 'var(--text-3)' }}>
                    {onlyAlive ? 'Живых аккаунтов нет. Сними галку «Только живые» чтобы увидеть остальных.' : 'Аккаунтов нет'}
                  </span>
                )}
              </div>
            </div>

            {/* Mode */}
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 8 }}>Режим</label>
              <div style={{ display: 'flex', gap: 8 }}>
                {Object.entries(MODE_INFO).map(([key, m]) => (
                  <button key={key} onClick={() => setForm(f => ({ ...f, mode: key }))} style={{
                    flex: 1, padding: '12px', borderRadius: 10, cursor: 'pointer', textAlign: 'left',
                    background: form.mode === key ? `${m.color}15` : 'var(--bg-3)',
                    border: `1px solid ${form.mode === key ? `${m.color}50` : 'var(--border)'}`,
                    color: form.mode === key ? m.color : 'var(--text-2)', transition: 'all 0.15s',
                  }}>
                    <div style={{ fontSize: 13, fontWeight: 600 }}>{m.label}</div>
                    <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 3 }}>{m.desc}</div>
                  </button>
                ))}
              </div>
            </div>

            {/* Days */}
            <Input label="Количество дней прогрева" type="number" value={form.total_days}
              onChange={e => setForm(f => ({ ...f, total_days: e.target.value }))} />

            {/* Drip-подписка на каналы */}
            <div style={{ padding: '14px 16px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.2)', borderRadius: 10 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--violet)', marginBottom: 4 }}>
                📢 Drip-подписка на каналы (опционально)
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 10, lineHeight: 1.6 }}>
                За время прогрева аккаунты <strong>рандомно подпишутся</strong> на эти каналы (0–{form.daily_join_max || 0} в день).
                Потом можно экспортировать их в кампанию комментинга — и аккаунты будут писать
                <strong> не сразу после подписки</strong>, а через дни. Это снижает баны.
              </div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>
                Каналы (по одному на строку или через запятую)
              </label>
              <textarea
                value={form.target_channels}
                onChange={e => setForm(f => ({ ...f, target_channels: e.target.value }))}
                rows={5}
                placeholder={"@durov\n@telegram\nDC_Draino\nhttps://t.me/example"}
                style={{
                  width: '100%', padding: '10px 12px', background: 'var(--bg-3)',
                  border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)',
                  fontSize: 13, outline: 'none', resize: 'vertical', fontFamily: 'var(--font-mono)',
                }}
              />
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 10 }}>
                <label style={{ fontSize: 12, color: 'var(--text-2)' }}>Макс. подписок в день:</label>
                <input
                  type="range" min="0" max="5" step="1"
                  value={form.daily_join_max}
                  onChange={e => setForm(f => ({ ...f, daily_join_max: e.target.value }))}
                  style={{ flex: 1, accentColor: 'var(--violet)' }}
                />
                <span style={{ fontSize: 14, fontWeight: 700, color: 'var(--violet)', minWidth: 20, textAlign: 'center' }}>
                  {form.daily_join_max}
                </span>
              </div>
              {(() => {
                const cnt = (form.target_channels || '').split(/[\s,\n]+/).map(c => c.trim()).filter(Boolean).length
                const nAcc = form.account_ids.length || 1
                const perAcc = Math.ceil(cnt / nAcc)
                return cnt > 0 ? (
                  <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 8, lineHeight: 1.6 }}>
                    <div>
                      <strong style={{ color: 'var(--violet)' }}>{cnt}</strong> каналов
                      {nAcc > 1 && <> ÷ <strong>{nAcc}</strong> акк = <strong style={{ color: 'var(--teal)' }}>~{perAcc}/акк</strong> (распределяются, не дублируются — анти-бан)</>}
                    </div>
                    {form.daily_join_max > 0
                      ? <div>Каждый аккаунт подпишется на свой набор за {form.total_days} дней (до {form.daily_join_max}/день).</div>
                      : <div style={{ color: 'var(--yellow)' }}>⚠ подписки выключены (макс/день = 0)</div>}
                  </div>
                ) : null
              })()}
            </div>

            {/* Info */}
            <div style={{ padding: '14px 16px', background: 'rgba(61,214,140,0.06)', border: '1px solid rgba(61,214,140,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.7 }}>
              <div style={{ fontWeight: 700, marginBottom: 6 }}>Как это работает:</div>
              • <strong>Запускается сразу</strong> после создания — кликать старт у каждого не надо<br />
              • Каждый аккаунт начинает в <strong>разное время</strong> (разброс до 3ч)<br />
              • День 1: 2–5 действий → День 7: 15–25 действий<br />
              • 5% шанс «дня отдыха» — аккаунт пропускает день<br />
              • Активность только 8:00–23:00<br />
              • Случайные паузы 30с–15мин между действиями
            </div>

            <div style={{ display: 'flex', gap: 10 }}>
              <Button variant="ghost" onClick={() => setCreateModal(false)} style={{ flex: 1 }}>Отмена</Button>
              <Button variant="primary" onClick={handleCreate} disabled={saving} style={{ flex: 1 }}>
                {saving ? 'Создаю...' : `Создать (${form.account_ids.length} акк.)`}
              </Button>
            </div>
          </div>
        </Modal>
      )}

      {/* ══ Plan/Activity Modal ══ */}
      {planModal && selectedTask && (
        <Modal open={true} title={`План прогрева: ${selectedTask.account_name || selectedTask.account_phone}`} onClose={() => setPlanModal(false)} width={640}>
          {planLoading ? <Spinner size={24} /> : !planData ? (
            <div style={{ textAlign: 'center', padding: 40, color: 'var(--text-3)', fontSize: 13 }}>
              Нет данных. Запусти прогрев.
            </div>
          ) : (
            <div style={{ maxHeight: 560, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 12 }}>
              {/* Заголовок-сводка */}
              <div style={{ padding: '12px 14px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>
                День <strong>{planData.current_day}/{planData.total_days}</strong> · статус {planData.status}
                {' '}· сегодня действий {planData.today_actions}/{planData.today_limit}
                {planData.next_action_at && (
                  <div style={{ marginTop: 4 }}>
                    ⏰ Следующая сессия: {new Date(planData.next_action_at).toLocaleString('ru', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}
                  </div>
                )}
                {planData.target_count > 0 && (
                  <div style={{ marginTop: 6 }}>
                    📢 Drip-каналы: <strong style={{ color: 'var(--violet)' }}>{planData.subscribed_count}/{planData.target_count}</strong> подписано
                    {' '}· до {planData.daily_join_max}/день
                  </div>
                )}
              </div>

              {/* Подписки drip — общий трекинг */}
              {planData.subscriptions && planData.subscriptions.length > 0 && (
                <div style={{ background: 'var(--bg-2)', border: '1px solid rgba(124,77,255,0.2)', borderRadius: 10, padding: '12px 14px' }}>
                  <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--violet)', marginBottom: 8 }}>
                    📢 Подписки ({planData.subscribed_count}/{planData.target_count})
                  </div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                    {planData.subscriptions.map((s, i) => (
                      <div key={i} title={s.subscribed_at ? new Date(s.subscribed_at + 'Z').toLocaleString('ru') : 'ещё не подписан'}
                        style={{
                          fontSize: 11, padding: '4px 10px', borderRadius: 6,
                          background: s.subscribed ? 'var(--green-dim)' : 'var(--bg-3)',
                          border: `1px solid ${s.subscribed ? 'rgba(61,214,140,0.3)' : 'var(--border)'}`,
                          color: s.subscribed ? 'var(--green)' : 'var(--text-3)',
                          fontFamily: 'var(--font-mono)',
                        }}>
                        {s.subscribed ? '✓' : '⏳'} @{s.channel}
                        {s.subscribed_at && (
                          <span style={{ opacity: 0.7, marginLeft: 4 }}>
                            {new Date(s.subscribed_at + 'Z').toLocaleDateString('ru', { day: '2-digit', month: '2-digit' })}
                          </span>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* План по дням: зелёное=сделано, фиолетовое=сейчас, серое=впереди */}
              {planData.days.length === 0 ? (
                <div style={{ textAlign: 'center', padding: 24, color: 'var(--text-3)', fontSize: 12 }}>
                  План ещё не сгенерирован.
                </div>
              ) : planData.days.map(day => (
                <div key={day.day_number} style={{
                  background: 'var(--bg-2)', border: `1px solid ${day.is_today ? 'rgba(61,214,140,0.4)' : 'var(--border)'}`,
                  borderRadius: 10, padding: '12px 14px',
                }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                    <div style={{ fontSize: 13, fontWeight: 700 }}>
                      День {day.day_number} <span style={{ fontWeight: 400, color: 'var(--text-3)', fontSize: 11 }}>· {day.plan_date}</span>
                      {day.is_today && <span style={{ marginLeft: 8, fontSize: 10, color: 'var(--green)', fontWeight: 600 }}>● СЕГОДНЯ</span>}
                      {day.is_past && <span style={{ marginLeft: 8, fontSize: 10, color: 'var(--text-3)' }}>✓ прошёл</span>}
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
                      {day.mood === 'rest' ? '😴 отдых' : `${day.mood} · ${day.total_sessions} сессий · ${day.executed_idx}/${day.total_sessions}`}
                    </div>
                  </div>
                  {day.sessions.length === 0 ? (
                    <div style={{ fontSize: 11, color: 'var(--text-3)' }}>День отдыха — без активности</div>
                  ) : (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                      {day.sessions.map(s => {
                        // Цвет: done=зелёный, is_next=фиолетовый(сейчас), future=серый
                        const bg = s.done ? 'rgba(61,214,140,0.12)' : s.is_next ? 'rgba(124,77,255,0.14)' : 'var(--bg-3)'
                        const bd = s.done ? 'rgba(61,214,140,0.3)' : s.is_next ? 'rgba(124,77,255,0.4)' : 'var(--border)'
                        return (
                          <div key={s.session} style={{
                            display: 'flex', alignItems: 'center', gap: 10, padding: '6px 10px',
                            background: bg, border: `1px solid ${bd}`, borderRadius: 6,
                          }}>
                            <span style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: 'var(--blue)', minWidth: 42 }}>{s.time}</span>
                            <span style={{ fontSize: 11, minWidth: 16 }}>
                              {s.done ? '✓' : s.is_next ? '▶' : '○'}
                            </span>
                            {s.skipped ? (
                              <span style={{ fontSize: 11, color: 'var(--text-3)' }}>⏭ пропуск: {s.skip_reason}</span>
                            ) : (
                              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', fontSize: 11, color: s.done ? 'var(--text-3)' : 'var(--text-2)' }}>
                                {s.actions_summary.map((a, i) => (
                                  <span key={i}>{a.label}{a.count > 1 ? ` ×${a.count}` : ''}</span>
                                ))}
                              </div>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </Modal>
      )}

      {/* ══ Logs Modal ══ */}
      {logsModal && selectedTask && (
        <Modal open={true} title={`Логи: ${selectedTask.account_name || selectedTask.account_phone}`} onClose={() => setLogsModal(false)} width={600}>
          {logsLoading ? <Spinner size={24} /> : (
            <div style={{ maxHeight: 500, overflowY: 'auto' }}>
              {taskLogs.length === 0 ? (
                <div style={{ textAlign: 'center', padding: 40, color: 'var(--text-3)', fontSize: 13 }}>Пока нет логов</div>
              ) : taskLogs.map(l => (
                <div key={l.id} style={{ padding: '10px 12px', borderBottom: '1px solid var(--border)', display: 'flex', gap: 10, alignItems: 'flex-start' }}>
                  <div style={{ fontSize: 14, flexShrink: 0 }}>{l.success ? '✅' : '❌'}</div>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 12, fontWeight: 600 }}>{l.action_label} {l.emoji}</div>
                    <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
                      {l.detail}
                      {l.channel && <span style={{ color: 'var(--blue)' }}> · @{l.channel}</span>}
                    </div>
                    {l.error && <div style={{ fontSize: 11, color: 'var(--red)' }}>{l.error}</div>}
                  </div>
                  <div style={{ fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--font-mono)', flexShrink: 0 }}>
                    {new Date(l.created_at).toLocaleString('ru', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Modal>
      )}

      {/* ══ Edit channels Modal (batch-level) ══ */}
      {channelsModal && chBatch && (() => {
        const inFormCount = chText.split(/[\n,]+/).map(s => s.trim()).filter(Boolean).length
        const wasCount = chPool?.length || 0
        return (
          <Modal open={true} title={`📢 Каналы прогрева · ${chBatch.batch_name}`}
                 onClose={() => setChannelsModal(false)} width={620}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12, maxHeight: '75vh', overflow: 'auto' }}>
              <div style={{ fontSize: 12, color: 'var(--text-3)' }}>
                Список целевых каналов для всех {chBatch.tasks?.length || 0} аккаунтов в этом прогреве.
                Редактируй прямо в форме — добавляй новые строки, удаляй ненужные, или сноси всё. После
                сохранения новый список заново распределится по аккаунтам.
              </div>

              {chPoolLoading ? (
                <div style={{ padding: 30, textAlign: 'center' }}><Spinner size={24} /></div>
              ) : (
                <>
                  <div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 6 }}>
                      <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
                        Каналы (по одному на строку или через запятую)
                      </label>
                      <span style={{ fontSize: 11, color: inFormCount === wasCount ? 'var(--text-3)' : 'var(--violet)' }}>
                        Было {wasCount} → станет <strong>{inFormCount}</strong>
                      </span>
                    </div>
                    <textarea
                      value={chText}
                      onChange={e => setChText(e.target.value)}
                      placeholder={'@channel1\nchannel2\n@channel3'}
                      style={{
                        width: '100%', minHeight: 280, padding: 12, borderRadius: 8,
                        background: 'var(--bg-3)', border: '1px solid var(--border)',
                        color: 'var(--text)', fontSize: 12, fontFamily: 'monospace', resize: 'vertical',
                        lineHeight: 1.5,
                      }}
                    />
                  </div>

                  {chPool && chPool.length > 0 && (
                    <details style={{ fontSize: 11, color: 'var(--text-3)' }}>
                      <summary style={{ cursor: 'pointer', userSelect: 'none' }}>
                        Статус подписок ({chPool.filter(p => p.subscribed_count >= p.tasks_count).length}/{chPool.length} полностью подписаны)
                      </summary>
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, maxHeight: 100, overflowY: 'auto', marginTop: 8, padding: 8, background: 'var(--bg-3)', borderRadius: 6 }}>
                        {chPool.map(p => {
                          const done = p.subscribed_count >= p.tasks_count
                          return (
                            <span key={p.channel}
                              title={`${p.subscribed_count} из ${p.tasks_count} акк. уже подписаны`}
                              style={{
                                padding: '2px 7px', borderRadius: 5, fontSize: 10,
                                background: done ? 'rgba(61,214,140,0.15)' : 'rgba(255,180,0,0.10)',
                                color: done ? 'var(--green)' : '#e8a400',
                                fontFamily: 'monospace',
                              }}>
                              {done ? '✓' : '○'} @{p.channel} {p.subscribed_count}/{p.tasks_count}
                            </span>
                          )
                        })}
                      </div>
                    </details>
                  )}

                  <div style={{ fontSize: 11, color: 'var(--text-3)', padding: '8px 10px', background: 'rgba(255,180,0,0.06)', border: '1px solid rgba(255,180,0,0.2)', borderRadius: 8 }}>
                    Сегодняшний день не трогается. Каналы на которые акки уже подписаны — повторно не подписываются.
                  </div>
                </>
              )}

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                <Button variant="ghost" onClick={() => setChannelsModal(false)} disabled={chSaving}>Отмена</Button>
                <Button variant="primary" onClick={handleChannelsSave} loading={chSaving} disabled={chPoolLoading}>Сохранить</Button>
              </div>
            </div>
          </Modal>
        )
      })()}

      {/* ══ Schedule campaign after warmup ══ */}
      {scheduleModal && scheduleBatch && (
        <Modal open={true} onClose={() => setScheduleModal(false)}
          title={`📅 Запланировать кампанию · ${scheduleBatch.batch_name}`} width={580}>
          {scheduleResult ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
              <div style={{ padding: '14px 16px', background: 'rgba(61,214,140,0.08)', border: '1px solid rgba(61,214,140,0.25)', borderRadius: 10, fontSize: 13, color: 'var(--text)' }}>
                ✅ <b>{scheduleResult.message}</b>
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.6 }}>
                Импортировано каналов: <b>{scheduleResult.channels_imported}</b><br />
                Аккаунтов: <b>{scheduleResult.accounts}</b><br />
                Старт автоматически когда:
                <ul style={{ margin: '6px 0 0 18px', padding: 0 }}>
                  <li>все прогревы этого batch'а закончатся, ИЛИ</li>
                  <li>наступит {scheduleResult.scheduled_start_at ? new Date(scheduleResult.scheduled_start_at).toLocaleString('ru') : '—'}</li>
                </ul>
              </div>
              <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                <Button variant="primary" onClick={() => setScheduleModal(false)}>OK</Button>
              </div>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
              <div style={{ padding: '10px 12px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 8, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>
                Каналы и аккаунты подтянутся из прогрева <b>«{scheduleBatch.batch_name}»</b> ({scheduleBatch.tasks.length} акк.) автоматически.
                Кампания стартует в момент завершения прогрева (или по fallback-времени).
              </div>

              <Input label="Название кампании" value={scheduleForm.name}
                onChange={e => setScheduleForm(f => ({ ...f, name: e.target.value }))} />

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                <div>
                  <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>LLM</label>
                  <select value={scheduleForm.llm_provider}
                    onChange={e => setScheduleForm(f => ({ ...f, llm_provider: e.target.value, llm_credential_id: null }))}
                    style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 14, outline: 'none' }}>
                    <option value="claude">Claude (Anthropic)</option>
                    <option value="openai">GPT-4o (OpenAI)</option>
                    <option value="gemini">Gemini (Google)</option>
                    <option value="groq">Llama 3.3 70B (Groq)</option>
                  </select>
                </div>
                <div>
                  <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Длина</label>
                  <select value={scheduleForm.comment_length}
                    onChange={e => setScheduleForm(f => ({ ...f, comment_length: e.target.value }))}
                    style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 14, outline: 'none' }}>
                    <option value="short">Короткий</option>
                    <option value="medium">Средний</option>
                    <option value="long">Развёрнутый</option>
                  </select>
                </div>
              </div>

              {(() => {
                const matching = scheduleLlmCreds.filter(k => k.provider === scheduleForm.llm_provider)
                if (matching.length === 0) {
                  return (
                    <div style={{ padding: '8px 12px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, fontSize: 11, color: 'var(--text-3)' }}>
                      Нет ключей для <b>{scheduleForm.llm_provider}</b> в БД — будет использоваться env.
                    </div>
                  )
                }
                return (
                  <div>
                    <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>API ключ</label>
                    <select value={scheduleForm.llm_credential_id || ''}
                      onChange={e => setScheduleForm(f => ({ ...f, llm_credential_id: e.target.value ? parseInt(e.target.value) : null }))}
                      style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 13, outline: 'none' }}>
                      <option value="">По умолчанию (default-ключ {scheduleForm.llm_provider})</option>
                      {matching.map(k => (
                        <option key={k.id} value={k.id}>
                          {k.is_default ? '⭐ ' : ''}{k.label || `#${k.id}`} · {k.api_key_masked}
                        </option>
                      ))}
                    </select>
                  </div>
                )
              })()}

              <div>
                <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Тон</label>
                <select value={scheduleForm.tone}
                  onChange={e => setScheduleForm(f => ({ ...f, tone: e.target.value }))}
                  style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 14, outline: 'none' }}>
                  <option value="positive">Позитивный</option>
                  <option value="neutral">Нейтральный</option>
                  <option value="critical">Критический</option>
                  <option value="curious">Любопытный</option>
                </select>
              </div>

              <div>
                <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Доп. промпт (опционально)</label>
                <textarea value={scheduleForm.custom_prompt} rows={2}
                  onChange={e => setScheduleForm(f => ({ ...f, custom_prompt: e.target.value }))}
                  style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 13, outline: 'none', resize: 'vertical', fontFamily: 'var(--font-sans)' }} />
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                <Input label="Макс. комментов" type="number" value={scheduleForm.max_comments}
                  onChange={e => setScheduleForm(f => ({ ...f, max_comments: parseInt(e.target.value) || 0 }))} />
                <Input label="Макс. часов работы" type="number" value={scheduleForm.max_hours}
                  onChange={e => setScheduleForm(f => ({ ...f, max_hours: parseInt(e.target.value) || 0 }))} />
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 }}>
                <Input label="Delay join (с)" type="number" value={scheduleForm.delay_join}
                  onChange={e => setScheduleForm(f => ({ ...f, delay_join: parseInt(e.target.value) || 0 }))} />
                <Input label="Delay comment (с)" type="number" value={scheduleForm.delay_comment}
                  onChange={e => setScheduleForm(f => ({ ...f, delay_comment: parseInt(e.target.value) || 0 }))} />
                <Input label="Between (с)" type="number" value={scheduleForm.delay_between}
                  onChange={e => setScheduleForm(f => ({ ...f, delay_between: parseInt(e.target.value) || 0 }))} />
              </div>

              <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                <Button variant="ghost" onClick={() => setScheduleModal(false)}>Отмена</Button>
                <Button variant="primary" loading={scheduleSaving} onClick={submitSchedule}>
                  📅 Запланировать
                </Button>
              </div>
            </div>
          )}
        </Modal>
      )}
    </div>
  )
}