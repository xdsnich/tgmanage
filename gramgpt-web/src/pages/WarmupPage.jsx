import { useEffect, useState } from 'react'
import { accountsAPI } from '../services/api'
import { Card, Button, Modal, Badge, Spinner, Empty, StatCard } from '../components/ui'
import api from '../services/api'

const warmupAPI = {
  list: () => api.get('/warmup/tasks'),
  create: (data) => api.post('/warmup/tasks', data),
  start: (id) => api.post(`/warmup/tasks/${id}/start`),
  stop: (id) => api.post(`/warmup/tasks/${id}/stop`),
  delete: (id) => api.delete(`/warmup/tasks/${id}`),
}

const MODES = [
  { value: 'careful', label: '🐢 Осторожный', desc: '5 действий/час, большие паузы', color: 'green' },
  { value: 'normal', label: '🚶 Обычный', desc: '15 действий/час, средние паузы', color: 'blue' },
  { value: 'aggressive', label: '🏃 Агрессивный', desc: '30 действий/час, минимальные паузы', color: 'red' },
]

const STATUS_MAP = { idle: 'Ожидает', running: 'Работает', paused: 'Пауза', finished: 'Завершён' }
const STATUS_COLORS = { idle: 'default', running: 'green', paused: 'yellow', finished: 'blue' }

export default function WarmupPage() {
  const [tasks, setTasks] = useState([])
  const [accounts, setAccounts] = useState([])
  const [loading, setLoading] = useState(true)
  const [createModal, setCreateModal] = useState(false)
  const [saving, setSaving] = useState(false)
  const [toast, setToast] = useState(null)

  const [form, setForm] = useState({
    account_id: null, mode: 'normal',
    read_feed: true, view_stories: true, set_reactions: true, join_channels: false,
  })

  const showToast = (t, type = 'success') => { setToast({ text: t, type }); setTimeout(() => setToast(null), 3500) }

  const load = async () => {
    setLoading(true)
    try {
      const [t, a] = await Promise.all([warmupAPI.list(), accountsAPI.list()])
      setTasks(t.data); setAccounts(a.data.filter(acc => acc.status === 'active'))
    } catch {}
    setLoading(false)
  }

  useEffect(() => { load() }, [])

  const handleCreate = async () => {
    if (!form.account_id) return
    setSaving(true)
    try { await warmupAPI.create(form); setCreateModal(false); showToast('Прогрев создан'); await load() }
    catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setSaving(false)
  }

  const handleAction = async (id, action) => {
    try {
      if (action === 'start') await warmupAPI.start(id)
      else if (action === 'stop') await warmupAPI.stop(id)
      else if (action === 'delete') { if (!window.confirm('Удалить?')) return; await warmupAPI.delete(id) }
      showToast(action === 'delete' ? 'Удалено' : `Прогрев: ${action}`)
      await load()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
  }

  if (loading) return <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}><Spinner size={32} /></div>

  return (
    <div style={{ padding: '28px 32px', animation: 'fadeUp 0.4s cubic-bezier(0.16,1,0.3,1)' }}>
      {toast && <div style={{ position: 'fixed', top: 24, right: 24, zIndex: 999, padding: '12px 20px', borderRadius: 12, fontSize: 13, fontWeight: 600, background: toast.type === 'error' ? 'var(--red-dim)' : 'var(--green-dim)', color: toast.type === 'error' ? 'var(--red)' : 'var(--green)', boxShadow: '0 8px 30px rgba(0,0,0,0.5)', animation: 'fadeUp 0.3s ease' }}>{toast.text}</div>}

      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 28 }}>
        <div>
          <div style={{ fontSize: 11, color: 'var(--green)', fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>🔥 ПРОГРЕВ</div>
          <h1 style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.04em' }}>Warm-up аккаунтов</h1>
          <p style={{ fontSize: 13, color: 'var(--text-3)', marginTop: 4 }}>Имитация действий живого человека для повышения траста</p>
        </div>
        <Button variant="primary" onClick={() => setCreateModal(true)}>+ Создать прогрев</Button>
      </div>

      {tasks.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginBottom: 20 }}>
          <StatCard label="Всего задач" value={tasks.length} icon="📋" />
          <StatCard label="Активных" value={tasks.filter(t => t.status === 'running').length} color="var(--green)" icon="▶" />
          <StatCard label="Действий" value={tasks.reduce((s, t) => s + (t.actions_done || 0), 0)} color="var(--violet)" icon="⚡" />
          <StatCard label="Реакций" value={tasks.reduce((s, t) => s + (t.reactions_set || 0), 0)} color="var(--pink)" icon="👍" />
        </div>
      )}

      {tasks.length === 0 ? (
        <Empty icon="🔥" title="Нет задач прогрева" subtitle="Создайте прогрев для аккаунта" action={<Button variant="primary" onClick={() => setCreateModal(true)}>+ Создать</Button>} />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {tasks.map(t => (
            <Card key={t.id}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <div style={{ flex: 1 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
                    <span style={{ fontWeight: 700, fontSize: 15 }}>{t.account_name || t.account_phone}</span>
                    <Badge color={STATUS_COLORS[t.status]}>{STATUS_MAP[t.status]}</Badge>
                    <Badge color={MODES.find(m => m.value === t.mode)?.color || 'default'}>{MODES.find(m => m.value === t.mode)?.label}</Badge>
                  </div>
                  <div style={{ display: 'flex', gap: 16, fontSize: 12, color: 'var(--text-3)' }}>
                    <span>⚡ {t.actions_done} действий</span>
                    <span>📖 {t.feeds_read} лент</span>
                    <span>👁 {t.stories_viewed} сторис</span>
                    <span>👍 {t.reactions_set} реакций</span>
                    <span>📢 {t.channels_joined} каналов</span>
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 6 }}>
                  {(t.status === 'idle' || t.status === 'finished') && <Button variant="primary" size="sm" onClick={() => handleAction(t.id, 'start')}>▶ Старт</Button>}
                  {t.status === 'running' && <Button variant="danger" size="sm" onClick={() => handleAction(t.id, 'stop')}>⏹ Стоп</Button>}
                  <Button variant="ghost" size="sm" onClick={() => handleAction(t.id, 'delete')}>✕</Button>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      <Modal open={createModal} onClose={() => setCreateModal(false)} title="Новый прогрев" width={500}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Аккаунт</label>
            <select value={form.account_id || ''} onChange={e => setForm(f => ({ ...f, account_id: parseInt(e.target.value) }))} style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 14, outline: 'none' }}>
              <option value="">Выберите аккаунт</option>
              {accounts.map(a => <option key={a.id} value={a.id}>{a.first_name || a.phone} ({a.phone})</option>)}
            </select>
          </div>

          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Режим</label>
            <div style={{ display: 'flex', gap: 8 }}>
              {MODES.map(m => (
                <button key={m.value} onClick={() => setForm(f => ({ ...f, mode: m.value }))} style={{
                  flex: 1, padding: '10px', borderRadius: 10, fontSize: 12, cursor: 'pointer', textAlign: 'center',
                  background: form.mode === m.value ? 'rgba(124,77,255,0.2)' : 'var(--bg-3)',
                  border: `1px solid ${form.mode === m.value ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                  color: form.mode === m.value ? 'var(--violet)' : 'var(--text-2)',
                }}>
                  <div style={{ fontWeight: 600 }}>{m.label}</div>
                  <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 2 }}>{m.desc}</div>
                </button>
              ))}
            </div>
          </div>

          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Действия</label>
            {[
              { key: 'read_feed', label: '📖 Чтение ленты каналов' },
              { key: 'view_stories', label: '👁 Просмотр Stories' },
              { key: 'set_reactions', label: '👍 Реакции на посты' },
              { key: 'join_channels', label: '📢 Вступление в каналы' },
            ].map(({ key, label }) => (
              <label key={key} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 0', cursor: 'pointer' }}>
                <input type="checkbox" checked={form[key]} onChange={e => setForm(f => ({ ...f, [key]: e.target.checked }))} style={{ width: 16, height: 16 }} />
                <span style={{ fontSize: 13 }}>{label}</span>
              </label>
            ))}
          </div>

          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" onClick={() => setCreateModal(false)}>Отмена</Button>
            <Button variant="primary" loading={saving} disabled={!form.account_id} onClick={handleCreate}>Создать</Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
