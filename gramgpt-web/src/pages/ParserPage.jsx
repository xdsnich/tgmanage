import { useEffect, useState } from 'react'
import { accountsAPI, parserAPI } from '../services/api'
import { Card, Button, Input, Modal, Badge, Spinner, Empty, StatCard } from '../components/ui'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
export default function ParserPage() {
  const [channels, setChannels] = useState([])
  const [accounts, setAccounts] = useState([])
  const [loading, setLoading] = useState(true)
  const [searching, setSearching] = useState(false)
  const [searchModal, setSearchModal] = useState(false)
  const [importModal, setImportModal] = useState(false)
  const [toast, setToast] = useState(null)
  const [folders, setFolders] = useState([])
  const [filterFolder, setFilterFolder] = useState('all')
  const [newFolderInput, setNewFolderInput] = useState(false)
  const [selectedChannels, setSelectedChannels] = useState([])
  const [folderModal, setFolderModal] = useState(false)
  const [folderName, setFolderName] = useState('')

  const [form, setForm] = useState({
    account_id: null, keywords: '', min_subscribers: 1000, max_subscribers: 500000,
    only_with_comments: true, active_hours: 48, source: 'telegram',
  })
  const [importText, setImportText] = useState('')
  const [searchResult, setSearchResult] = useState(null)

  const showToast = (t, type = 'success') => { setToast({ text: t, type }); setTimeout(() => setToast(null), 3500) }

  const load = async () => {
    setLoading(true)
    try {
      const [c, a, f] = await Promise.all([
        parserAPI.list(), accountsAPI.list(),
        parserAPI.folders().catch(() => ({ data: [] })),
      ])
      setChannels(c.data); setAccounts(a.data.filter(acc => acc.status === 'active'))
      setFolders(f.data || [])
    } catch { }
    setLoading(false)
  }

  useEffect(() => { load() }, [])
  useAutoRefresh(() => load(), 15000)

  const filteredChannels = channels.filter(c =>
    filterFolder === 'all' || (c.folder || '') === filterFolder
  )

  const handleSetFolder = async (folder) => {
    if (!selectedChannels.length) return
    try {
      await parserAPI.setFolder(selectedChannels, folder)
      showToast(`${selectedChannels.length} каналов → "${folder}"`)
      setSelectedChannels([]); setFolderModal(false); await load()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
  }

  const toggleSelect = (id) => {
    setSelectedChannels(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id])
  }

  const selectAll = () => {
    if (selectedChannels.length === filteredChannels.length) setSelectedChannels([])
    else setSelectedChannels(filteredChannels.map(c => c.id))
  }

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
          {/* Папки — чипы */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', marginBottom: 12 }}>
            <span style={{ fontSize: 10, color: 'var(--text-3)', fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase', minWidth: 60 }}>📁 Папки</span>
            <button onClick={() => setFilterFolder('all')} style={{
              padding: '5px 12px', borderRadius: 20, cursor: 'pointer', fontSize: 11, transition: 'all 0.15s',
              fontWeight: filterFolder === 'all' ? 600 : 400,
              border: `1px solid ${filterFolder === 'all' ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
              background: filterFolder === 'all' ? 'rgba(124,77,255,0.15)' : 'transparent',
              color: filterFolder === 'all' ? 'var(--violet)' : 'var(--text-3)',
            }}>Все ({channels.length})</button>
            <button onClick={() => setFilterFolder('')} style={{
              padding: '5px 12px', borderRadius: 20, cursor: 'pointer', fontSize: 11, transition: 'all 0.15s',
              fontWeight: filterFolder === '' ? 600 : 400,
              border: `1px solid ${filterFolder === '' ? 'rgba(255,180,0,0.4)' : 'var(--border)'}`,
              background: filterFolder === '' ? 'rgba(255,180,0,0.12)' : 'transparent',
              color: filterFolder === '' ? 'var(--yellow)' : 'var(--text-3)',
            }}>Без папки ({channels.filter(c => !c.folder).length})</button>
            {folders.map(f => (
              <button key={f.name} onClick={() => setFilterFolder(filterFolder === f.name ? 'all' : f.name)} style={{
                padding: '5px 12px', borderRadius: 20, cursor: 'pointer', fontSize: 11, transition: 'all 0.15s',
                fontWeight: filterFolder === f.name ? 600 : 400,
                border: `1px solid ${filterFolder === f.name ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                background: filterFolder === f.name ? 'rgba(124,77,255,0.15)' : 'transparent',
                color: filterFolder === f.name ? 'var(--violet)' : 'var(--text-3)',
              }}>{f.name} ({f.count})</button>
            ))}
            {!newFolderInput ? (
              <button onClick={() => setNewFolderInput(true)} style={{
                padding: '5px 10px', borderRadius: 20, cursor: 'pointer', fontSize: 11,
                border: '1px dashed var(--border)', background: 'transparent', color: 'var(--text-3)',
              }}>+ Создать</button>
            ) : (
              <form onSubmit={e => { e.preventDefault(); const v = e.target.fname.value.trim(); if (v) setFolderName(v); setNewFolderInput(false); if (v) setFolderModal(true) }} style={{ display: 'flex', gap: 4 }}>
                <input name="fname" autoFocus placeholder="Имя папки..." style={{
                  padding: '4px 10px', borderRadius: 20, border: '1px solid rgba(124,77,255,0.4)', background: 'rgba(124,77,255,0.08)',
                  color: 'var(--text)', fontSize: 11, outline: 'none', width: 120,
                }} />
                <button type="submit" style={{ padding: '4px 8px', borderRadius: 20, border: '1px solid rgba(61,214,140,0.4)', background: 'rgba(61,214,140,0.1)', color: 'var(--green)', fontSize: 11, cursor: 'pointer' }}>✓</button>
                <button type="button" onClick={() => setNewFolderInput(false)} style={{ padding: '4px 8px', borderRadius: 20, border: '1px solid var(--border)', background: 'transparent', color: 'var(--text-3)', fontSize: 11, cursor: 'pointer' }}>✕</button>
              </form>
            )}
          </div>

          {/* Toolbar */}
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <button onClick={selectAll} style={{ padding: '5px 12px', borderRadius: 8, border: '1px solid var(--border)', background: 'transparent', color: 'var(--text-3)', fontSize: 11, cursor: 'pointer' }}>
                {selectedChannels.length === filteredChannels.length && filteredChannels.length > 0 ? '☑ Снять всё' : '☐ Выбрать всё'}
              </button>
              {selectedChannels.length > 0 && (
                <Button variant="outline" size="sm" onClick={() => { setFolderName(''); setFolderModal(true) }}>
                  📁 В папку ({selectedChannels.length})
                </Button>
              )}
            </div>
            <Button variant="ghost" size="sm" onClick={handleClear}>🗑 Очистить всё</Button>
          </div>

          {/* Channels list */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {filteredChannels.map(c => (
              <Card key={c.id} style={{ padding: '10px 14px', borderLeft: selectedChannels.includes(c.id) ? '3px solid var(--violet)' : '3px solid transparent' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <input type="checkbox" checked={selectedChannels.includes(c.id)} onChange={() => toggleSelect(c.id)}
                    style={{ accentColor: 'var(--violet)', cursor: 'pointer' }} onClick={e => e.stopPropagation()} />
                  <div style={{ flex: 1 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 2 }}>
                      <span style={{ fontWeight: 700, fontSize: 13 }}>@{c.username}</span>
                      <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{c.title}</span>
                      {c.has_comments && <Badge color="green">💬</Badge>}
                      {c.folder && <span style={{ fontSize: 10, padding: '1px 8px', borderRadius: 10, background: 'rgba(124,77,255,0.1)', color: 'rgba(124,77,255,0.8)', border: '1px solid rgba(124,77,255,0.2)' }}>📁 {c.folder}</span>}
                    </div>
                    <div style={{ display: 'flex', gap: 14, fontSize: 10, color: 'var(--text-3)' }}>
                      <span>👥 {c.subscribers?.toLocaleString()}</span>
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

      {/* Folder assignment modal */}
      <Modal open={folderModal} onClose={() => setFolderModal(false)} title="📁 Назначить папку" width={400}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ fontSize: 13, color: 'var(--text-2)' }}>
            Выбрано каналов: <strong>{selectedChannels.length}</strong>
          </div>
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, display: 'block', marginBottom: 6 }}>Папка</label>
            <input list="folder-list" value={folderName} onChange={e => setFolderName(e.target.value)}
              placeholder="Введи или выбери папку" autoFocus
              style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 13, outline: 'none' }} />
            <datalist id="folder-list">{folders.map(f => <option key={f.name} value={f.name} />)}</datalist>
          </div>
          {/* Quick buttons for existing folders */}
          {folders.length > 0 && (
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              {folders.map(f => (
                <button key={f.name} onClick={() => setFolderName(f.name)} style={{
                  padding: '5px 12px', borderRadius: 20, fontSize: 11, cursor: 'pointer',
                  border: `1px solid ${folderName === f.name ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                  background: folderName === f.name ? 'rgba(124,77,255,0.15)' : 'transparent',
                  color: folderName === f.name ? 'var(--violet)' : 'var(--text-3)',
                }}>{f.name}</button>
              ))}
            </div>
          )}
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" onClick={() => setFolderModal(false)}>Отмена</Button>
            <Button variant="primary" disabled={!folderName.trim()} onClick={() => handleSetFolder(folderName.trim())}>
              📁 Назначить
            </Button>
          </div>
        </div>
      </Modal>

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

          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Где искать</label>
            <select value={form.source} onChange={e => setForm(f => ({ ...f, source: e.target.value }))} style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 14, outline: 'none' }}>
              <option value="telegram">📱 Telegram (через аккаунт)</option>
              <option value="tgstat">📊 TGStat API</option>
              <option value="both">🔍 Оба источника</option>
            </select>
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