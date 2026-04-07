import { useEffect, useState, useRef } from 'react'
import { warmupAPI, accountsAPI } from '../services/api'
import { Card, Button, Input, Modal, Badge, Spinner, Empty } from '../components/ui'

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
  const [taskLogs, setTaskLogs] = useState([])
  const [logsLoading, setLogsLoading] = useState(false)
  const [saving, setSaving] = useState(false)

  const [form, setForm] = useState({ account_ids: [], total_days: 7, mode: 'normal' })
  const logsRef = useRef(null)

  const load = async () => {
    setLoading(true)
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
    setLoading(false)
  }

  useEffect(() => { load() }, [])

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
      const { data } = await warmupAPI.create({
        account_ids: form.account_ids,
        total_days: parseInt(form.total_days) || 7,
        mode: form.mode,
      })
      alert(data.message)
      setCreateModal(false)
      setForm({ account_ids: [], total_days: 7, mode: 'normal' })
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

  const toggleAccount = (id) => setForm(f => ({
    ...f, account_ids: f.account_ids.includes(id) ? f.account_ids.filter(x => x !== id) : [...f.account_ids, id]
  }))
  const selectAll = () => setForm(f => ({
    ...f, account_ids: f.account_ids.length === accounts.length ? [] : accounts.map(a => a.id)
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
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {tasks.map(t => {
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

                    {/* Progress today */}
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
                    {/* Next action time */}
                    {t.status === 'running' && t.next_action_at && (
                      <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 8, padding: '6px 10px', background: 'rgba(255,255,255,0.03)', borderRadius: 6 }}>
                        ⏰ Следующая сессия: {new Date(t.next_action_at).toLocaleTimeString('ru', { hour: '2-digit', minute: '2-digit' })}
                        {' '}({Math.max(0, Math.round((new Date(t.next_action_at) - new Date()) / 60000))} мин)
                      </div>
                    )}
                    {/* Stats mini */}
                    <div style={{ display: 'flex', gap: 12, fontSize: 11, color: 'var(--text-3)', marginBottom: 10 }}>
                      <span>📖 {t.feeds_read}</span>
                      <span>😍 {t.reactions_set}</span>
                      <span>👁 {t.stories_viewed}</span>
                      <span>📢 {t.channels_joined}</span>
                      <span>📋 {t.logs_count} логов</span>
                    </div>

                    {/* Actions */}
                    <div style={{ display: 'flex', gap: 6 }}>
                      {t.status === 'idle' && <Button variant="primary" size="sm" onClick={() => handleStart(t.id)}>▶ Старт</Button>}
                      {t.status === 'running' && (
                        <>
                          <Button variant="ghost" size="sm" onClick={() => handlePause(t.id)}>⏸ Пауза</Button>
                          <Button variant="danger" size="sm" onClick={() => handleStop(t.id)}>⏹ Завершить</Button>
                        </>
                      )}
                      {t.status === 'paused' && <Button variant="primary" size="sm" onClick={() => handleStart(t.id)}>▶ Продолжить</Button>}
                      <Button variant="ghost" size="sm" onClick={() => openLogs(t)}>📋 Логи</Button>
                      {t.status !== 'running' && <Button variant="ghost" size="sm" onClick={() => handleDelete(t.id)}>✕</Button>}
                    </div>
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
                  Аккаунты ({form.account_ids.length})
                </label>
                <button onClick={selectAll} style={{ fontSize: 11, color: 'var(--violet)', background: 'none', border: 'none', cursor: 'pointer' }}>
                  {form.account_ids.length === accounts.length ? 'Снять все' : 'Выбрать все'}
                </button>
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, maxHeight: 120, overflowY: 'auto' }}>
                {accounts.map(a => (
                  <button key={a.id} onClick={() => toggleAccount(a.id)} style={{
                    padding: '6px 12px', borderRadius: 8, fontSize: 12, cursor: 'pointer',
                    background: form.account_ids.includes(a.id) ? 'rgba(124,77,255,0.2)' : 'var(--bg-3)',
                    border: `1px solid ${form.account_ids.includes(a.id) ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                    color: form.account_ids.includes(a.id) ? 'var(--violet)' : 'var(--text-2)', transition: 'all 0.15s',
                  }}>{a.first_name || a.phone}</button>
                ))}
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

            {/* Info */}
            <div style={{ padding: '14px 16px', background: 'rgba(61,214,140,0.06)', border: '1px solid rgba(61,214,140,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.7 }}>
              <div style={{ fontWeight: 700, marginBottom: 6 }}>Как это работает:</div>
              • Каждый аккаунт начинает в <strong>разное время</strong> (разброс до 3ч)<br />
              • День 1: 2–5 действий → День 7: 15–25 действий<br />
              • 15% шанс «дня отдыха» — аккаунт пропускает день<br />
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
    </div>
  )
}