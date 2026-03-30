import { useEffect, useState } from 'react'
import { accountsAPI } from '../services/api'
import { Card, Button, Input, Modal, Badge, Spinner, Empty, StatCard } from '../components/ui'
import api from '../services/api'

const parserAPI = {
  list: () => api.get('/parser/channels'),
  search: (data) => api.post('/parser/search', data),
  delete: (id) => api.delete(`/parser/channels/${id}`),
  clearAll: () => api.delete('/parser/channels'),
  exportCSV: () => api.get('/parser/export', { responseType: 'blob' }),
  importList: (channels) => api.post('/parser/import', { channels }),
}

export default function ParserPage() {
  const [channels, setChannels] = useState([])
  const [accounts, setAccounts] = useState([])
  const [loading, setLoading] = useState(true)
  const [searching, setSearching] = useState(false)
  const [searchModal, setSearchModal] = useState(false)
  const [importModal, setImportModal] = useState(false)
  const [toast, setToast] = useState(null)

  const [form, setForm] = useState({
    account_id: null, keywords: '', min_subscribers: 1000, max_subscribers: 500000,
    only_with_comments: true, active_hours: 48,
  })
  const [importText, setImportText] = useState('')
  const [searchResult, setSearchResult] = useState(null)

  const showToast = (t, type = 'success') => { setToast({ text: t, type }); setTimeout(() => setToast(null), 3500) }

  const load = async () => {
    setLoading(true)
    try {
      const [c, a] = await Promise.all([parserAPI.list(), accountsAPI.list()])
      setChannels(c.data); setAccounts(a.data.filter(acc => acc.status === 'active'))
    } catch { }
    setLoading(false)
  }

  useEffect(() => { load() }, [])

  const handleSearch = async () => {
    if (!form.account_id || !form.keywords.trim()) return
    setSearching(true); setSearchResult(null)
    try {
      const { data } = await parserAPI.search(form)
      setSearchResult(data)
      showToast(`Найдено ${data.found} каналов, сохранено ${data.saved}`)
      await load()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка поиска', 'error') }
    setSearching(false)
  }

  const handleExport = async () => {
    try {
      const { data } = await parserAPI.exportCSV()
      const url = window.URL.createObjectURL(new Blob([data]))
      const a = document.createElement('a'); a.href = url; a.download = 'channels.csv'; a.click()
      showToast('CSV скачан')
    } catch { showToast('Ошибка экспорта', 'error') }
  }

  const handleImport = async () => {
    const list = importText.split('\n').map(s => s.trim()).filter(Boolean)
    if (!list.length) return
    try {
      const { data } = await parserAPI.importList(list)
      showToast(`Добавлено ${data.added} каналов`)
      setImportModal(false); setImportText(''); await load()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
  }

  const handleClear = async () => {
    if (!window.confirm('Удалить все каналы?')) return
    try { await parserAPI.clearAll(); showToast('Очищено'); await load() } catch { }
  }

  if (loading) return <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}><Spinner size={32} /></div>

  return (
    <div style={{ padding: '28px 32px', animation: 'fadeUp 0.4s cubic-bezier(0.16,1,0.3,1)' }}>
      {toast && <div style={{ position: 'fixed', top: 24, right: 24, zIndex: 999, padding: '12px 20px', borderRadius: 12, fontSize: 13, fontWeight: 600, background: toast.type === 'error' ? 'var(--red-dim)' : 'var(--green-dim)', color: toast.type === 'error' ? 'var(--red)' : 'var(--green)', boxShadow: '0 8px 30px rgba(0,0,0,0.5)', animation: 'fadeUp 0.3s ease' }}>{toast.text}</div>}

      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 28 }}>
        <div>
          <div style={{ fontSize: 11, color: 'var(--blue)', fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>🔍 ПАРСЕР</div>
          <h1 style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.04em' }}>Парсер каналов</h1>
          <p style={{ fontSize: 13, color: 'var(--text-3)', marginTop: 4 }}>Сбор базы каналов-доноров для комментинга</p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <Button variant="ghost" onClick={() => setImportModal(true)}>📥 Импорт</Button>
          <Button variant="ghost" onClick={handleExport} disabled={channels.length === 0}>📤 CSV</Button>
          <Button variant="primary" onClick={() => { setSearchResult(null); setSearchModal(true) }}>🔍 Поиск</Button>
        </div>
      </div>

      {channels.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12, marginBottom: 20 }}>
          <StatCard label="Каналов в базе" value={channels.length} icon="📢" />
          <StatCard label="С комментариями" value={channels.filter(c => c.has_comments).length} color="var(--green)" icon="💬" />
          <StatCard label="Ср. подписчиков" value={channels.length > 0 ? Math.round(channels.reduce((s, c) => s + c.subscribers, 0) / channels.length).toLocaleString() : 0} color="var(--violet)" icon="👥" />
        </div>
      )}

      {channels.length === 0 ? (
        <Empty icon="🔍" title="База каналов пуста" subtitle="Запустите поиск или импортируйте список" action={<Button variant="primary" onClick={() => setSearchModal(true)}>🔍 Поиск</Button>} />
      ) : (
        <>
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
            <Button variant="ghost" size="sm" onClick={handleClear}>🗑 Очистить всё</Button>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {channels.map(c => (
              <Card key={c.id} style={{ padding: '12px 16px' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <div style={{ flex: 1 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                      <span style={{ fontWeight: 700, fontSize: 14 }}>@{c.username}</span>
                      <span style={{ fontSize: 12, color: 'var(--text-3)' }}>{c.title}</span>
                      {c.has_comments && <Badge color="green">💬</Badge>}
                    </div>
                    <div style={{ display: 'flex', gap: 14, fontSize: 11, color: 'var(--text-3)' }}>
                      <span>👥 {c.subscribers.toLocaleString()}</span>
                      {c.last_post_date && <span>📅 {new Date(c.last_post_date).toLocaleDateString('ru')}</span>}
                      <span>🔍 {c.search_query}</span>
                    </div>
                  </div>
                  <button onClick={() => parserAPI.delete(c.id).then(load)} style={{ background: 'none', border: 'none', color: 'var(--red)', cursor: 'pointer', fontSize: 14 }}>✕</button>
                </div>
              </Card>
            ))}
          </div>
        </>
      )}

      {/* Search Modal */}
      <Modal open={searchModal} onClose={() => setSearchModal(false)} title="Поиск каналов" width={520}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Аккаунт для поиска</label>
            <select value={form.account_id || ''} onChange={e => setForm(f => ({ ...f, account_id: parseInt(e.target.value) }))} style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 14, outline: 'none' }}>
              <option value="">Выберите</option>
              {accounts.map(a => <option key={a.id} value={a.id}>{a.first_name || a.phone}</option>)}
            </select>
          </div>

          <Input label="Ключевые слова (через запятую)" value={form.keywords} onChange={e => setForm(f => ({ ...f, keywords: e.target.value }))} placeholder="криптовалюта, крипта, блокчейн" />

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <Input label="Мин. подписчиков" type="number" value={form.min_subscribers} onChange={e => setForm(f => ({ ...f, min_subscribers: parseInt(e.target.value) || 0 }))} />
            <Input label="Макс. подписчиков" type="number" value={form.max_subscribers} onChange={e => setForm(f => ({ ...f, max_subscribers: parseInt(e.target.value) || 0 }))} />
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, cursor: 'pointer' }}>
              <input type="checkbox" checked={form.only_with_comments} onChange={e => setForm(f => ({ ...f, only_with_comments: e.target.checked }))} />
              Только с комментариями
            </label>
            <Input label="Посты за последние (часов)" type="number" value={form.active_hours} onChange={e => setForm(f => ({ ...f, active_hours: parseInt(e.target.value) || 0 }))} />
          </div>

          {searchResult && (
            <div style={{ padding: '10px 14px', borderRadius: 10, fontSize: 13, background: 'var(--green-dim)', color: 'var(--green)', border: '1px solid rgba(61,214,140,0.2)' }}>
              Найдено: {searchResult.found} каналов, сохранено: {searchResult.saved}
            </div>
          )}

          <div style={{ padding: '10px 14px', background: 'var(--bg-3)', borderRadius: 10, fontSize: 11, color: 'var(--text-3)', lineHeight: 1.6 }}>
            💡 Поиск через Telegram API + TGStat (если задан TGSTAT_API_KEY в .env).
            Бесплатный ключ TGStat: <a href="https://api.tgstat.ru" target="_blank" style={{ color: 'var(--violet)' }}>api.tgstat.ru</a>
          </div>

          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" onClick={() => setSearchModal(false)}>Закрыть</Button>
            <Button variant="primary" loading={searching} disabled={!form.account_id || !form.keywords.trim()} onClick={handleSearch}>
              {searching ? 'Ищу...' : '🔍 Найти'}
            </Button>
          </div>
        </div>
      </Modal>

      {/* Import Modal */}
      <Modal open={importModal} onClose={() => setImportModal(false)} title="Импорт каналов" width={480}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ padding: '10px 14px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>
            Введите юзернеймы или ссылки — по одному на строку
          </div>
          <textarea value={importText} onChange={e => setImportText(e.target.value)} rows={8} placeholder={"@crypto_news\n@blockchain_ru\nhttps://t.me/bitcoin"} style={{ width: '100%', padding: '12px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 13, fontFamily: 'var(--font-mono)', resize: 'vertical', outline: 'none' }} />
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" onClick={() => setImportModal(false)}>Отмена</Button>
            <Button variant="primary" onClick={handleImport}>Импортировать</Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}