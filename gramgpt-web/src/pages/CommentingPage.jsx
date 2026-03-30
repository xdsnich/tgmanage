import { useEffect, useState } from 'react'
import { commentingAPI, accountsAPI } from '../services/api'
import { Card, Button, Input, Modal, Badge, Spinner, Empty, StatCard } from '../components/ui'

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
  draft: 'default', active: 'green', paused: 'yellow', stopped: 'red', finished: 'blue',
}
const STATUS_LABELS = {
  draft: 'Черновик', active: 'Активна', paused: 'Пауза', stopped: 'Остановлена', finished: 'Завершена',
}

export default function CommentingPage() {
  const [campaigns, setCampaigns] = useState([])
  const [accounts, setAccounts] = useState([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(null)
  const [createModal, setCreateModal] = useState(false)
  const [detailModal, setDetailModal] = useState(false)
  const [channelModal, setChannelModal] = useState(false)
  const [saving, setSaving] = useState(false)
  const [toast, setToast] = useState(null)

  // Create form
  const [form, setForm] = useState({
    name: '', account_ids: [], trigger_mode: 'all', trigger_percent: 50,
    trigger_keywords: '', llm_provider: 'claude', tone: 'positive',
    custom_prompt: '', comment_length: 'medium',
    max_comments: 100, max_hours: 24,
    delay_join: 10, delay_comment: 250, delay_between: 60,
  })

  const [channelText, setChannelText] = useState('')

  const showToast = (text, type = 'success') => {
    setToast({ text, type }); setTimeout(() => setToast(null), 3500)
  }

  const load = async () => {
    setLoading(true)
    try {
      const [c, a] = await Promise.all([commentingAPI.list(), accountsAPI.list()])
      setCampaigns(c.data); setAccounts(a.data.filter(acc => acc.status === 'active'))
    } catch {}
    setLoading(false)
  }

  useEffect(() => { load() }, [])

  const handleCreate = async () => {
    setSaving(true)
    try {
      const payload = {
        ...form,
        trigger_keywords: form.trigger_keywords ? form.trigger_keywords.split(',').map(s => s.trim()).filter(Boolean) : [],
      }
      await commentingAPI.create(payload)
      setCreateModal(false); showToast('Кампания создана'); await load()
      setForm({ name: '', account_ids: [], trigger_mode: 'all', trigger_percent: 50, trigger_keywords: '', llm_provider: 'claude', tone: 'positive', custom_prompt: '', comment_length: 'medium', max_comments: 100, max_hours: 24, delay_join: 10, delay_comment: 250, delay_between: 60 })
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setSaving(false)
  }

  const handleAction = async (id, action) => {
    try {
      if (action === 'start') await commentingAPI.start(id)
      else if (action === 'pause') await commentingAPI.pause(id)
      else if (action === 'stop') await commentingAPI.stop(id)
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
      showToast(data.message); setChannelModal(false); setChannelText('')
      await load()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setSaving(false)
  }

  const handleRemoveChannel = async (campaignId, channelId) => {
    try { await commentingAPI.removeChannel(campaignId, channelId); showToast('Канал удалён'); await load() }
    catch {}
  }

  const openDetail = (c) => { setSelected(c); setDetailModal(true) }

  if (loading) return <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}><Spinner size={32} /></div>

  return (
    <div style={{ padding: '28px 32px', animation: 'fadeUp 0.4s cubic-bezier(0.16,1,0.3,1)' }}>
      {toast && (
        <div style={{ position: 'fixed', top: 24, right: 24, zIndex: 999, padding: '12px 20px', borderRadius: 12, fontSize: 13, fontWeight: 600, background: toast.type === 'error' ? 'var(--red-dim)' : 'var(--green-dim)', color: toast.type === 'error' ? 'var(--red)' : 'var(--green)', border: `1px solid ${toast.type === 'error' ? 'rgba(248,81,73,0.3)' : 'rgba(61,214,140,0.3)'}`, boxShadow: '0 8px 30px rgba(0,0,0,0.5)', animation: 'fadeUp 0.3s ease' }}>{toast.text}</div>
      )}

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 28 }}>
        <div>
          <div style={{ fontSize: 11, color: 'var(--pink)', fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>🧠 НЕЙРОКОММЕНТИНГ</div>
          <h1 style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.04em' }}>Кампании</h1>
          <p style={{ fontSize: 13, color: 'var(--text-3)', marginTop: 4 }}>{campaigns.length} кампаний</p>
        </div>
        <Button variant="primary" onClick={() => setCreateModal(true)}>+ Создать кампанию</Button>
      </div>

      {/* Stats */}
      {campaigns.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginBottom: 20 }}>
          <StatCard label="Всего кампаний" value={campaigns.length} icon="📋" />
          <StatCard label="Активных" value={campaigns.filter(c => c.status === 'active').length} color="var(--green)" icon="▶" />
          <StatCard label="Комментариев" value={campaigns.reduce((s, c) => s + (c.comments_sent || 0), 0)} color="var(--violet)" icon="💬" />
          <StatCard label="Каналов" value={campaigns.reduce((s, c) => s + (c.channels_count || 0), 0)} color="var(--blue)" icon="📢" />
        </div>
      )}

      {/* Campaign list */}
      {campaigns.length === 0 ? (
        <Empty icon="🧠" title="Нет кампаний" subtitle="Создайте первую кампанию нейрокомментинга" action={<Button variant="primary" onClick={() => setCreateModal(true)}>+ Создать</Button>} />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {campaigns.map(c => (
            <Card key={c.id} onClick={() => openDetail(c)} style={{ cursor: 'pointer' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <div style={{ flex: 1 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
                    <span style={{ fontWeight: 700, fontSize: 15, letterSpacing: '-0.02em' }}>{c.name}</span>
                    <Badge color={STATUS_COLORS[c.status]}>{STATUS_LABELS[c.status]}</Badge>
                    <Badge color="violet">{c.llm_provider === 'claude' ? 'Claude' : 'GPT-4o'}</Badge>
                    <Badge color="default">{TONES.find(t => t.value === c.tone)?.label || c.tone}</Badge>
                  </div>
                  <div style={{ display: 'flex', gap: 16, fontSize: 12, color: 'var(--text-3)' }}>
                    <span>💬 {c.comments_sent}/{c.max_comments}</span>
                    <span>📢 {c.channels_count} каналов</span>
                    <span>👤 {(c.account_ids || []).length} акк.</span>
                    <span>⏱ {c.trigger_mode === 'all' ? 'Каждый пост' : c.trigger_mode === 'random' ? `${c.trigger_percent}% постов` : 'По ключам'}</span>
                  </div>
                  {/* Progress bar */}
                  <div style={{ marginTop: 8, height: 3, background: 'rgba(255,255,255,0.06)', borderRadius: 2, overflow: 'hidden' }}>
                    <div style={{ width: `${Math.min(100, c.max_comments > 0 ? (c.comments_sent / c.max_comments) * 100 : 0)}%`, height: '100%', background: 'linear-gradient(90deg, #7c4dff, #3d8bff)', transition: 'width 0.6s' }} />
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 6, marginLeft: 16 }} onClick={e => e.stopPropagation()}>
                  {c.status === 'draft' || c.status === 'paused' || c.status === 'stopped' ? (
                    <Button variant="primary" size="sm" onClick={() => handleAction(c.id, 'start')}>▶ Старт</Button>
                  ) : null}
                  {c.status === 'active' ? (
                    <>
                      <Button variant="ghost" size="sm" onClick={() => handleAction(c.id, 'pause')}>⏸ Пауза</Button>
                      <Button variant="danger" size="sm" onClick={() => handleAction(c.id, 'stop')}>⏹ Стоп</Button>
                    </>
                  ) : null}
                  <Button variant="ghost" size="sm" onClick={() => handleAction(c.id, 'delete')}>✕</Button>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      {/* ── Create Campaign Modal ──────────────────────── */}
      <Modal open={createModal} onClose={() => setCreateModal(false)} title="Новая кампания" width={600}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14, maxHeight: '70vh', overflow: 'auto' }}>
          <Input label="Название" value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} placeholder="Крипто-комментинг" />

          {/* Accounts */}
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
            </div>
            {accounts.length === 0 && <div style={{ fontSize: 12, color: 'var(--text-3)' }}>Нет активных аккаунтов</div>}
          </div>

          {/* LLM Provider */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>LLM</label>
              <select value={form.llm_provider} onChange={e => setForm(f => ({ ...f, llm_provider: e.target.value }))} style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 14, outline: 'none' }}>
                <option value="claude">Claude (Anthropic)</option>
                <option value="openai">GPT-4o (OpenAI)</option>
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

          {/* Tone */}
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

          {/* Trigger */}
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

          {form.trigger_mode === 'random' && (
            <Input label="Процент постов (%)" type="number" value={form.trigger_percent} onChange={e => setForm(f => ({ ...f, trigger_percent: parseInt(e.target.value) || 0 }))} />
          )}
          {form.trigger_mode === 'keywords' && (
            <Input label="Ключевые слова (через запятую)" value={form.trigger_keywords} onChange={e => setForm(f => ({ ...f, trigger_keywords: e.target.value }))} placeholder="крипта, блокчейн, биткоин" />
          )}

          {/* Limits & Delays */}
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

      {/* ── Detail Modal ───────────────────────────────── */}
      <Modal open={detailModal} onClose={() => setDetailModal(false)} title={selected?.name || 'Кампания'} width={600}>
        {selected && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14, maxHeight: '70vh', overflow: 'auto' }}>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <Badge color={STATUS_COLORS[selected.status]}>{STATUS_LABELS[selected.status]}</Badge>
              <Badge color="violet">{selected.llm_provider === 'claude' ? 'Claude' : 'GPT-4o'}</Badge>
              <Badge color="default">{TONES.find(t => t.value === selected.tone)?.label}</Badge>
              <Badge color="blue">💬 {selected.comments_sent}/{selected.max_comments}</Badge>
            </div>

            {/* Progress */}
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, color: 'var(--text-3)', marginBottom: 4 }}>
                <span>Прогресс</span>
                <span>{Math.min(100, Math.round(selected.comments_sent / Math.max(selected.max_comments, 1) * 100))}%</span>
              </div>
              <div style={{ height: 6, background: 'rgba(255,255,255,0.06)', borderRadius: 3, overflow: 'hidden' }}>
                <div style={{ width: `${Math.min(100, selected.comments_sent / Math.max(selected.max_comments, 1) * 100)}%`, height: '100%', background: 'var(--grad-purple)', borderRadius: 3 }} />
              </div>
            </div>

            {/* Channels */}
            <div>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                <span style={{ fontWeight: 700, fontSize: 13 }}>Целевые каналы ({(selected.channels || []).length})</span>
                <Button variant="outline" size="sm" onClick={() => setChannelModal(true)}>+ Добавить</Button>
              </div>
              {(selected.channels || []).length === 0 ? (
                <div style={{ fontSize: 12, color: 'var(--text-3)', textAlign: 'center', padding: '16px 0' }}>
                  Нет каналов — добавьте для запуска
                </div>
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

            {/* Settings info */}
            <div style={{ padding: '12px 14px', background: 'var(--bg-3)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)' }}>
              <div>Триггер: {TRIGGERS.find(t => t.value === selected.trigger_mode)?.label}{selected.trigger_mode === 'random' ? ` (${selected.trigger_percent}%)` : ''}{selected.trigger_mode === 'keywords' ? `: ${(selected.trigger_keywords || []).join(', ')}` : ''}</div>
              <div style={{ marginTop: 4 }}>Тайминги: вход {selected.delay_join}с · коммент {selected.delay_comment}с · между {selected.delay_between}с</div>
              <div style={{ marginTop: 4 }}>Лимит: {selected.max_comments} комментов / {selected.max_hours}ч</div>
              {selected.custom_prompt && <div style={{ marginTop: 4, color: 'var(--violet)' }}>Промпт: {selected.custom_prompt.slice(0, 100)}...</div>}
            </div>
          </div>
        )}
      </Modal>

      {/* ── Add Channels Modal ─────────────────────────── */}
      <Modal open={channelModal} onClose={() => setChannelModal(false)} title="Добавить каналы" width={480}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ padding: '10px 14px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>
            Введите юзернеймы или ссылки — по одному на строку.<br />
            Форматы: <code style={{ color: 'var(--violet)' }}>@username</code>, <code style={{ color: 'var(--violet)' }}>https://t.me/username</code>
          </div>
          <textarea value={channelText} onChange={e => setChannelText(e.target.value)} rows={6} placeholder={"@crypto_news\n@blockchain_ru\nhttps://t.me/bitcoin_channel"} style={{ width: '100%', padding: '12px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 13, fontFamily: 'var(--font-mono)', resize: 'vertical', outline: 'none' }} />
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" onClick={() => setChannelModal(false)}>Отмена</Button>
            <Button variant="primary" loading={saving} onClick={handleAddChannels}>Добавить</Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
