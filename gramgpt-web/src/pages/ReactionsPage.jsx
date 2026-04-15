import { useEffect, useState } from 'react'
import { reactionsAPI, accountsAPI } from '../services/api'
import { Card, Button, Input, Modal, Badge, Spinner, Empty, StatusBadge } from '../components/ui'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
const POPULAR_EMOJIS = ["👍", "🔥", "❤️", "🎉", "🤩", "👏", "😁", "💯", "🏆", "❤️‍🔥", "🤣", "😍", "🙏", "🕊", "😎"]

const TARGET_OPTIONS = [
  { key: 'post', label: '📝 Пост', desc: 'Реакция на сам пост' },
  { key: 'comments', label: '💬 Комментарии', desc: 'Реакции на комменты под постом' },
  { key: 'both', label: '📝+💬 Пост + Комменты', desc: 'Реакции и на пост, и на комменты' },
]

export default function ReactionsPage() {
  const [tasks, setTasks] = useState([])
  const [accounts, setAccounts] = useState([])
  const [loading, setLoading] = useState(true)
  const [createModal, setCreateModal] = useState(false)
  const [resultModal, setResultModal] = useState(false)
  const [selectedTask, setSelectedTask] = useState(null)
  const [saving, setSaving] = useState(false)
  const [running, setRunning] = useState({})

  const [form, setForm] = useState({
    channel_link: '', post_id: '', account_ids: [], reactions: ['👍', '🔥'],
    mode: 'random', target: 'post', comments_limit: 5, count: 0, delay_min: 3, delay_max: 15,
  })

  const load = async () => {
    setLoading(true)
    try {
      const [tasksRes, accsRes] = await Promise.all([reactionsAPI.list(), accountsAPI.list()])
      setTasks(tasksRes.data)
      setAccounts(accsRes.data.filter(a => a.status === 'active'))
    } catch (err) { console.error(err) }
    setLoading(false)
  }

  useEffect(() => { load() }, [])
  useAutoRefresh(() => load(), 15000)

  const handleCreate = async () => {
    if (!form.channel_link || !form.account_ids.length || !form.reactions.length) {
      alert('Заполни канал, аккаунты и реакции'); return
    }
    setSaving(true)
    try {
      await reactionsAPI.create({
        channel_link: form.channel_link.trim(),
        post_id: form.post_id ? parseInt(form.post_id) : null,
        account_ids: form.account_ids,
        reactions: form.reactions,
        mode: form.mode,
        target: form.target,
        comments_limit: parseInt(form.comments_limit) || 5,
        count: parseInt(form.count) || 0,
        delay_min: parseInt(form.delay_min) || 3,
        delay_max: parseInt(form.delay_max) || 15,
      })
      setCreateModal(false)
      setForm({ channel_link: '', post_id: '', account_ids: [], reactions: ['👍', '🔥'], mode: 'random', target: 'post', comments_limit: 5, count: 0, delay_min: 3, delay_max: 15 })
      await load()
    } catch (err) { alert(err.response?.data?.detail || 'Ошибка') }
    setSaving(false)
  }

  const handleRun = async (taskId) => {
    setRunning(r => ({ ...r, [taskId]: true }))
    try {
      const { data } = await reactionsAPI.run(taskId)
      setSelectedTask(data); setResultModal(true); await load()
    } catch (err) { alert(err.response?.data?.detail || 'Ошибка запуска') }
    setRunning(r => ({ ...r, [taskId]: false }))
  }

  const handleDelete = async (taskId) => {
    if (!window.confirm('Удалить задачу?')) return
    try { await reactionsAPI.delete(taskId); await load() } catch { }
  }

  const toggleAccount = (id) => setForm(f => ({ ...f, account_ids: f.account_ids.includes(id) ? f.account_ids.filter(x => x !== id) : [...f.account_ids, id] }))
  const selectAllAccounts = () => setForm(f => ({ ...f, account_ids: f.account_ids.length === accounts.length ? [] : accounts.map(a => a.id) }))
  const toggleEmoji = (emoji) => setForm(f => ({ ...f, reactions: f.reactions.includes(emoji) ? f.reactions.filter(e => e !== emoji) : [...f.reactions, emoji] }))

  const statusColor = (s) => ({ done: 'green', running: 'violet', error: 'red', pending: 'default' }[s] || 'default')
  const statusLabel = (s) => ({ done: 'Готово', running: 'Запущено', error: 'Ошибка', pending: 'Ожидает' }[s] || s)
  const targetLabel = (t) => ({ post: '📝 Пост', comments: '💬 Комменты', both: '📝+💬 Оба' }[t] || t)

  return (
    <div style={{ padding: '28px 32px', animation: 'fadeUp 0.4s cubic-bezier(0.16,1,0.3,1)' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 28 }}>
        <div>
          <div style={{ fontSize: 11, color: '#ff3d9a', fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>😍 РЕАКЦИИ</div>
          <h1 style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.04em' }}>Реакции на посты и комменты</h1>
          <p style={{ fontSize: 13, color: 'var(--text-3)', marginTop: 4 }}>Ставь реакции на посты и комментарии с нескольких аккаунтов</p>
        </div>
        <Button variant="primary" onClick={() => setCreateModal(true)}>+ Новая задача</Button>
      </div>

      {/* Tasks list */}
      {loading ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 64 }}><Spinner size={28} /></div>
      ) : tasks.length === 0 ? (
        <Empty icon="😍" title="Нет задач на реакции" subtitle="Создай задачу чтобы начать"
          action={<Button variant="primary" onClick={() => setCreateModal(true)}>+ Создать</Button>} />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {tasks.map(t => (
            <div key={t.id} style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: '18px 22px' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <div style={{ fontSize: 22 }}>{t.reactions?.[0] || '👍'}</div>
                  <div>
                    <div style={{ fontWeight: 700, fontSize: 14 }}>{t.channel_link}</div>
                    <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2 }}>
                      Пост: {t.post_id || 'последний'} · {targetLabel(t.target)} · Аккаунтов: {t.account_ids?.length || 0} · Режим: {t.mode}
                      {t.target !== 'post' && ` · Комментов: ${t.comments_limit}`}
                    </div>
                  </div>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <Badge color={statusColor(t.status)}>{statusLabel(t.status)}</Badge>
                  {t.status === 'done' && <span style={{ fontSize: 11, color: 'var(--text-3)' }}>✅ {t.reactions_sent} / ❌ {t.reactions_failed}</span>}
                </div>
              </div>
              <div style={{ display: 'flex', gap: 4, marginBottom: 12, flexWrap: 'wrap' }}>
                {(t.reactions || []).map((e, i) => (
                  <span key={i} style={{ padding: '4px 8px', background: 'rgba(255,255,255,0.04)', borderRadius: 6, fontSize: 16 }}>{e}</span>
                ))}
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                {['pending', 'done', 'error'].includes(t.status) && (
                  <Button variant="primary" size="sm" onClick={() => handleRun(t.id)} disabled={running[t.id]}>
                    {running[t.id] ? '⏳ Запуск...' : '▶ Запустить'}
                  </Button>
                )}
                {t.status === 'done' && t.results?.length > 0 && (
                  <Button variant="ghost" size="sm" onClick={() => { setSelectedTask(t); setResultModal(true) }}>📊 Результаты</Button>
                )}
                <Button variant="ghost" size="sm" onClick={() => handleDelete(t.id)}>✕</Button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* ══ Create Modal ══ */}
      {createModal && (
        <Modal open={true} title="Новая задача на реакции" onClose={() => setCreateModal(false)} width={620}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16, maxHeight: '70vh', overflow: 'auto' }}>
            <Input label="Канал" autoFocus placeholder="@channel или https://t.me/channel" value={form.channel_link}
              onChange={e => setForm(f => ({ ...f, channel_link: e.target.value }))} />

            <Input label="ID поста (пусто = последний)" placeholder="12345" type="number" value={form.post_id}
              onChange={e => setForm(f => ({ ...f, post_id: e.target.value }))} />

            {/* Target */}
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 8 }}>Куда ставить реакции</label>
              <div style={{ display: 'flex', gap: 8 }}>
                {TARGET_OPTIONS.map(t => (
                  <button key={t.key} onClick={() => setForm(f => ({ ...f, target: t.key }))} style={{
                    flex: 1, padding: '10px 12px', borderRadius: 10, cursor: 'pointer', textAlign: 'left',
                    background: form.target === t.key ? 'rgba(255,61,154,0.12)' : 'var(--bg-3)',
                    border: `1px solid ${form.target === t.key ? 'rgba(255,61,154,0.35)' : 'var(--border)'}`,
                    color: form.target === t.key ? 'var(--pink)' : 'var(--text-2)', transition: 'all 0.15s',
                  }}>
                    <div style={{ fontSize: 13, fontWeight: 600 }}>{t.label}</div>
                    <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 3 }}>{t.desc}</div>
                  </button>
                ))}
              </div>
            </div>

            {/* Comments limit */}
            {form.target !== 'post' && (
              <Input label="Сколько комментариев реактить" type="number" value={form.comments_limit}
                onChange={e => setForm(f => ({ ...f, comments_limit: e.target.value }))} />
            )}

            {/* Accounts */}
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
                  Аккаунты ({form.account_ids.length}/{accounts.length})
                </label>
                <button onClick={selectAllAccounts} style={{ fontSize: 11, color: 'var(--violet)', background: 'none', border: 'none', cursor: 'pointer' }}>
                  {form.account_ids.length === accounts.length ? 'Снять все' : 'Выбрать все'}
                </button>
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, maxHeight: 100, overflowY: 'auto' }}>
                {accounts.map(a => (
                  <button key={a.id} onClick={() => toggleAccount(a.id)} style={{
                    padding: '6px 12px', borderRadius: 8, fontSize: 12, cursor: 'pointer', transition: 'all 0.15s',
                    background: form.account_ids.includes(a.id) ? 'rgba(124,77,255,0.2)' : 'var(--bg-3)',
                    border: `1px solid ${form.account_ids.includes(a.id) ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                    color: form.account_ids.includes(a.id) ? 'var(--violet)' : 'var(--text-2)',
                  }}>{a.first_name || a.phone}</button>
                ))}
                {accounts.length === 0 && <div style={{ fontSize: 12, color: 'var(--text-3)', padding: 8 }}>Нет активных аккаунтов</div>}
              </div>
            </div>

            {/* Emojis */}
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 8 }}>
                Реакции ({form.reactions.length} выбрано)
              </label>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {POPULAR_EMOJIS.map(emoji => (
                  <button key={emoji} onClick={() => toggleEmoji(emoji)} style={{
                    padding: '6px 10px', borderRadius: 8, fontSize: 18, cursor: 'pointer', transition: 'all 0.15s',
                    background: form.reactions.includes(emoji) ? 'rgba(255,61,154,0.15)' : 'var(--bg-3)',
                    border: `1px solid ${form.reactions.includes(emoji) ? 'rgba(255,61,154,0.35)' : 'var(--border)'}`,
                    transform: form.reactions.includes(emoji) ? 'scale(1.15)' : 'scale(1)',
                  }}>{emoji}</button>
                ))}
              </div>
            </div>

            {/* Mode */}
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 8 }}>Режим</label>
              <div style={{ display: 'flex', gap: 8 }}>
                {[
                  { key: 'random', label: '🎲 Случайная', desc: 'случайная из списка' },
                  { key: 'sequential', label: '📋 По очереди', desc: '1й акк — 1я реакция...' },
                  { key: 'all', label: '💥 Все сразу', desc: 'все реакции от каждого' },
                ].map(m => (
                  <button key={m.key} onClick={() => setForm(f => ({ ...f, mode: m.key }))} style={{
                    flex: 1, padding: '10px 12px', borderRadius: 10, cursor: 'pointer', textAlign: 'left',
                    background: form.mode === m.key ? 'rgba(124,77,255,0.12)' : 'var(--bg-3)',
                    border: `1px solid ${form.mode === m.key ? 'rgba(124,77,255,0.35)' : 'var(--border)'}`,
                    color: form.mode === m.key ? 'var(--violet)' : 'var(--text-2)', transition: 'all 0.15s',
                  }}>
                    <div style={{ fontSize: 13, fontWeight: 600 }}>{m.label}</div>
                    <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 3 }}>{m.desc}</div>
                  </button>
                ))}
              </div>
            </div>

            {/* Delays */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 }}>
              <Input label="Кол-во (0=все)" type="number" value={form.count} onChange={e => setForm(f => ({ ...f, count: e.target.value }))} />
              <Input label="Мин. задержка (сек)" type="number" value={form.delay_min} onChange={e => setForm(f => ({ ...f, delay_min: e.target.value }))} />
              <Input label="Макс. задержка (сек)" type="number" value={form.delay_max} onChange={e => setForm(f => ({ ...f, delay_max: e.target.value }))} />
            </div>

            <div style={{ display: 'flex', gap: 10, marginTop: 8 }}>
              <Button variant="ghost" onClick={() => setCreateModal(false)} style={{ flex: 1 }}>Отмена</Button>
              <Button variant="primary" onClick={handleCreate} disabled={saving} style={{ flex: 1 }}>
                {saving ? 'Создаю...' : 'Создать задачу'}
              </Button>
            </div>
          </div>
        </Modal>
      )}

      {/* ══ Results Modal ══ */}
      {resultModal && selectedTask && (
        <Modal open={true} title={`Результаты: ${selectedTask.channel_link}`} onClose={() => setResultModal(false)} width={620}>
          <div>
            <div style={{ display: 'flex', gap: 16, marginBottom: 20 }}>
              <div style={{ flex: 1, padding: '14px 16px', background: 'rgba(61,214,140,0.06)', borderRadius: 10, textAlign: 'center' }}>
                <div style={{ fontSize: 24, fontWeight: 800, color: 'var(--green)' }}>{selectedTask.reactions_sent}</div>
                <div style={{ fontSize: 11, color: 'var(--text-3)' }}>Отправлено</div>
              </div>
              <div style={{ flex: 1, padding: '14px 16px', background: 'rgba(248,81,73,0.06)', borderRadius: 10, textAlign: 'center' }}>
                <div style={{ fontSize: 24, fontWeight: 800, color: 'var(--red)' }}>{selectedTask.reactions_failed}</div>
                <div style={{ fontSize: 11, color: 'var(--text-3)' }}>Ошибок</div>
              </div>
            </div>
            <div style={{ maxHeight: 350, overflowY: 'auto' }}>
              {(selectedTask.results || []).map((r, i) => (
                <div key={i} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 12px', borderBottom: '1px solid var(--border)' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                    <span style={{ fontSize: 18 }}>{r.emoji}</span>
                    <div>
                      <div style={{ fontSize: 13, fontWeight: 600 }}>{r.phone}</div>
                      <div style={{ fontSize: 11, color: 'var(--text-3)' }}>{r.target}</div>
                      {r.error && <div style={{ fontSize: 11, color: 'var(--red)', marginTop: 2 }}>{r.error}</div>}
                    </div>
                  </div>
                  <Badge color={r.ok ? 'green' : 'red'}>{r.ok ? '✓' : '✗'}</Badge>
                </div>
              ))}
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}