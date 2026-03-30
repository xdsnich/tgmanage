import { useEffect, useState, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { accountsAPI, importAPI } from '../services/api'
import { Card, Button, Input, Modal, TrustBar, StatusBadge, Empty, Spinner, Badge } from '../components/ui'

const ROLES = ['default', 'продавец', 'прогреватель', 'читатель', 'консультант']
const STATUS_FILTERS = [
  { key: 'all', label: 'Все' },
  { key: 'active', label: '● Живые' },
  { key: 'spamblock', label: '● Спам' },
  { key: 'frozen', label: '● Заморожено' },
  { key: 'unknown', label: '● Неизвестно' },
]

export default function AccountsPage() {
  const [accounts, setAccounts] = useState([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [filterStatus, setFilterStatus] = useState('all')
  const [selected, setSelected] = useState(null)
  const [addModal, setAddModal] = useState(false)
  const [editModal, setEditModal] = useState(false)
  const [phone, setPhone] = useState('')
  const [editData, setEditData] = useState({})
  const [saving, setSaving] = useState(false)
  const navigate = useNavigate()

  // Import modals
  const [importModal, setImportModal] = useState(false)
  const [importType, setImportType] = useState(null) // 'session' | 'tdata' | 'session-batch'
  const [importFiles, setImportFiles] = useState([])
  const [importPhone, setImportPhone] = useState('')
  const [importing, setImporting] = useState(false)
  const [importResult, setImportResult] = useState(null)
  const fileInputRef = useRef(null)

  const load = async () => {
    setLoading(true)
    try { const { data } = await accountsAPI.list(); setAccounts(data) } catch { }
    setLoading(false)
  }

  useEffect(() => { load() }, [])

  const filtered = accounts.filter(a => {
    const q = search.toLowerCase()
    const matchSearch = !q || [a.phone, a.username, a.first_name, a.last_name, a.status].some(v => (v || '').toLowerCase().includes(q))
    const matchStatus = filterStatus === 'all' || a.status === filterStatus
    return matchSearch && matchStatus
  })

  const handleAdd = async (e) => {
    e.preventDefault(); setSaving(true)
    try {
      await accountsAPI.create(phone.startsWith('+') ? phone : '+' + phone)
      setAddModal(false); setPhone(''); await load()
    } catch (err) { alert(err.response?.data?.detail || 'Ошибка') }
    setSaving(false)
  }

  const handleEdit = async (e) => {
    e.preventDefault(); setSaving(true)
    try {
      await accountsAPI.update(selected.id, editData)
      setEditModal(false); await load()
    } catch (err) { alert(err.response?.data?.detail || 'Ошибка') }
    setSaving(false)
  }

  const handleDelete = async (id) => {
    if (!window.confirm('Удалить аккаунт?')) return
    try { await accountsAPI.delete(id); await load() } catch { }
  }

  const openEdit = (acc, e) => {
    e.stopPropagation()
    setSelected(acc)
    setEditData({ role: acc.role, notes: acc.notes || '', tags: acc.tags || [] })
    setEditModal(true)
  }

  return (
    <div style={{ padding: '28px 32px', animation: 'fadeUp 0.4s cubic-bezier(0.16,1,0.3,1)' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 28 }}>
        <div>
          <div style={{ fontSize: 11, color: 'var(--blue)', fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>◉ АККАУНТЫ</div>
          <h1 style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.04em' }}>Управление аккаунтами</h1>
          <p style={{ fontSize: 13, color: 'var(--text-3)', marginTop: 4 }}>{accounts.length} аккаунтов в базе</p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <Button variant="ghost" onClick={() => { setImportType(null); setImportResult(null); setImportFiles([]); setImportPhone(''); setImportModal(true) }}>📦 Импорт</Button>
          <Button variant="primary" onClick={() => setAddModal(true)}>+ Добавить</Button>
        </div>
      </div>

      {/* Filters */}
      <div style={{ display: 'flex', gap: 10, marginBottom: 20, flexWrap: 'wrap' }}>
        <div style={{ flex: 1, maxWidth: 300 }}>
          <Input placeholder="🔍  Поиск..." value={search} onChange={e => setSearch(e.target.value)} />
        </div>
        <div style={{ display: 'flex', gap: 6 }}>
          {STATUS_FILTERS.map(({ key, label }) => (
            <button key={key} onClick={() => setFilterStatus(key)} style={{
              padding: '9px 14px', borderRadius: 10, cursor: 'pointer',
              border: `1px solid ${filterStatus === key ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
              background: filterStatus === key ? 'rgba(124,77,255,0.15)' : 'transparent',
              color: filterStatus === key ? 'var(--violet)' : 'var(--text-2)',
              fontSize: 12, fontWeight: filterStatus === key ? 600 : 400,
              transition: 'all 0.15s',
            }}>{label}</button>
          ))}
        </div>
      </div>

      {/* Table */}
      {loading ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 64 }}><Spinner size={28} /></div>
      ) : filtered.length === 0 ? (
        <Empty icon="👤" title="Нет аккаунтов"
          subtitle={search || filterStatus !== 'all' ? 'Попробуй изменить фильтры' : 'Добавь первый аккаунт'}
          action={!search && filterStatus === 'all' && <Button variant="primary" onClick={() => setAddModal(true)}>+ Добавить</Button>} />
      ) : (
        <div style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', overflow: 'hidden' }}>
          {/* Header row */}
          <div style={{
            display: 'grid', gridTemplateColumns: '2fr 1.2fr 1fr 1.2fr 100px 100px',
            padding: '10px 20px', borderBottom: '1px solid var(--border)',
            fontSize: 10, color: 'var(--text-3)', letterSpacing: '0.1em', fontWeight: 700, textTransform: 'uppercase',
          }}>
            <span>Аккаунт</span><span>Телефон</span><span>Статус</span><span>Trust</span><span>Роль</span><span style={{ textAlign: 'right' }}>Действия</span>
          </div>

          {filtered.map((acc, i) => (
            <div key={acc.id} onClick={() => navigate(`/accounts/${acc.id}`)} style={{
              display: 'grid', gridTemplateColumns: '2fr 1.2fr 1fr 1.2fr 100px 100px',
              padding: '14px 20px', alignItems: 'center',
              borderBottom: i < filtered.length - 1 ? '1px solid var(--border)' : 'none',
              transition: 'background 0.1s', cursor: 'pointer',
            }}
              onMouseEnter={e => e.currentTarget.style.background = 'rgba(255,255,255,0.02)'}
              onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
              {/* Name + avatar */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <div style={{
                  width: 32, height: 32, borderRadius: 8, flexShrink: 0,
                  background: 'linear-gradient(135deg, rgba(124,77,255,0.25), rgba(61,139,255,0.15))',
                  border: '1px solid rgba(124,77,255,0.15)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: 12, fontWeight: 700, color: 'var(--violet)',
                }}>{acc.first_name?.[0]?.toUpperCase() || '?'}</div>
                <div>
                  <div style={{ fontWeight: 600, fontSize: 13 }}>
                    {acc.first_name || 'Без имени'} {acc.last_name || ''}
                  </div>
                  {acc.username && <div style={{ fontSize: 10, color: 'var(--text-3)' }}>@{acc.username}</div>}
                </div>
              </div>
              <div style={{ fontSize: 12, fontFamily: 'var(--font-mono)', color: 'var(--text-2)' }}>{acc.phone}</div>
              <StatusBadge status={acc.status} />
              <TrustBar score={acc.trust_score} />
              <div>
                {acc.role !== 'default' && <Badge color="violet">{acc.role}</Badge>}
              </div>
              <div style={{ display: 'flex', gap: 4, justifyContent: 'flex-end' }}>
                <button onClick={(e) => openEdit(acc, e)} title="Редактировать" style={{
                  width: 28, height: 28, borderRadius: 6, border: '1px solid var(--border)',
                  background: 'transparent', color: 'var(--text-3)', cursor: 'pointer',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12,
                  transition: 'all 0.15s',
                }}
                  onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--violet)'; e.currentTarget.style.color = 'var(--violet)' }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.color = 'var(--text-3)' }}>
                  ✎
                </button>
                <button onClick={(e) => { e.stopPropagation(); handleDelete(acc.id) }} title="Удалить" style={{
                  width: 28, height: 28, borderRadius: 6, border: '1px solid var(--border)',
                  background: 'transparent', color: 'var(--text-3)', cursor: 'pointer',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12,
                  transition: 'all 0.15s',
                }}
                  onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--red)'; e.currentTarget.style.color = 'var(--red)' }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.color = 'var(--text-3)' }}>
                  ✕
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Add modal */}
      <Modal open={addModal} onClose={() => setAddModal(false)} title="Добавить аккаунт" width={420}>
        <form onSubmit={handleAdd} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ padding: '10px 14px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>
            Введите номер телефона аккаунта. После добавления используйте авторизацию на странице деталей аккаунта.
          </div>
          <Input label="Номер телефона" value={phone} onChange={e => setPhone(e.target.value)} placeholder="+380..." required autoFocus />
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" type="button" onClick={() => setAddModal(false)}>Отмена</Button>
            <Button variant="primary" type="submit" loading={saving}>Добавить</Button>
          </div>
        </form>
      </Modal>

      {/* Edit modal */}
      <Modal open={editModal} onClose={() => setEditModal(false)} title={`Редактировать · ${selected?.phone || ''}`} width={440}>
        <form onSubmit={handleEdit} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Роль</label>
            <select value={editData.role || 'default'} onChange={e => setEditData(d => ({ ...d, role: e.target.value }))} style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)', color: 'var(--text)', fontSize: 14, outline: 'none' }}>
              {ROLES.map(r => <option key={r} value={r}>{r === 'default' ? 'Без роли' : r}</option>)}
            </select>
          </div>
          <Input label="Заметки" value={editData.notes || ''} onChange={e => setEditData(d => ({ ...d, notes: e.target.value }))} placeholder="Заметки об аккаунте" />
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" type="button" onClick={() => setEditModal(false)}>Отмена</Button>
            <Button variant="primary" type="submit" loading={saving}>Сохранить</Button>
          </div>
        </form>
      </Modal>

      {/* Import modal */}
      <Modal open={importModal} onClose={() => setImportModal(false)} title="Импорт аккаунтов" width={520}>
        {!importType ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div style={{ padding: '12px 14px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>
              Выберите способ импорта аккаунтов
            </div>
            {[
              { type: 'session', icon: '📄', title: '.session файл', desc: 'Один файл Telethon/Pyrogram сессии' },
              { type: 'session-batch', icon: '📁', title: 'Пакет .session', desc: 'Несколько .session файлов сразу' },
              { type: 'tdata', icon: '📦', title: 'TData архив (ZIP)', desc: 'Архив папки Telegram Desktop' },
              { type: 'json', icon: '📥', title: 'Из JSON (CLI)', desc: 'Импорт из data/accounts.json' },
            ].map(({ type, icon, title, desc }) => (
              <div key={type} onClick={() => {
                if (type === 'json') { accountsAPI.importJson().then(load); setImportModal(false); return }
                setImportType(type)
              }} style={{
                padding: '14px 16px', background: 'var(--bg-3)', border: '1px solid var(--border)',
                borderRadius: 12, cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 14,
                transition: 'all 0.15s',
              }}
                onMouseEnter={e => { e.currentTarget.style.borderColor = 'rgba(124,77,255,0.4)'; e.currentTarget.style.background = 'rgba(124,77,255,0.08)' }}
                onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.background = 'var(--bg-3)' }}>
                <span style={{ fontSize: 28 }}>{icon}</span>
                <div>
                  <div style={{ fontWeight: 600, fontSize: 14 }}>{title}</div>
                  <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 2 }}>{desc}</div>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <button onClick={() => { setImportType(null); setImportResult(null); setImportFiles([]) }} style={{
              background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer',
              fontSize: 12, padding: 0, textAlign: 'left',
            }}>← Назад к выбору</button>

            {importType === 'tdata' && (
              <div style={{ padding: '10px 14px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>
                Загрузите ZIP архив содержащий папку TData из Telegram Desktop.<br />
                Поддерживаются: opentele, telethon-tdata.
              </div>
            )}

            {/* File picker */}
            <div
              onClick={() => fileInputRef.current?.click()}
              style={{
                padding: '32px 20px', border: '2px dashed var(--border)', borderRadius: 14,
                textAlign: 'center', cursor: 'pointer', transition: 'border-color 0.2s',
              }}
              onMouseEnter={e => e.currentTarget.style.borderColor = 'var(--violet)'}
              onMouseLeave={e => e.currentTarget.style.borderColor = 'var(--border)'}
              onDragOver={e => { e.preventDefault(); e.currentTarget.style.borderColor = 'var(--violet)' }}
              onDragLeave={e => { e.currentTarget.style.borderColor = 'var(--border)' }}
              onDrop={e => {
                e.preventDefault()
                e.currentTarget.style.borderColor = 'var(--border)'
                const files = Array.from(e.dataTransfer.files)
                setImportFiles(files)
              }}
            >
              <div style={{ fontSize: 32, marginBottom: 8 }}>
                {importType === 'tdata' ? '📦' : '📄'}
              </div>
              <div style={{ fontSize: 13, fontWeight: 600 }}>
                {importFiles.length > 0
                  ? `Выбрано: ${importFiles.map(f => f.name).join(', ')}`
                  : 'Перетащите файл сюда или нажмите для выбора'
                }
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4 }}>
                {importType === 'tdata' ? 'ZIP архив с TData' : importType === 'session-batch' ? 'Несколько .session файлов' : '.session файл'}
              </div>
            </div>
            <input
              ref={fileInputRef}
              type="file"
              accept={importType === 'tdata' ? '.zip' : '.session'}
              multiple={importType === 'session-batch'}
              style={{ display: 'none' }}
              onChange={e => setImportFiles(Array.from(e.target.files))}
            />

            {(importType === 'session' || importType === 'tdata') && (
              <Input label="Номер телефона (необязательно)" value={importPhone} onChange={e => setImportPhone(e.target.value)} placeholder="+380..." />
            )}

            {importResult && (
              <div style={{
                padding: '10px 14px', borderRadius: 10, fontSize: 13,
                background: importResult.success ? 'var(--green-dim)' : 'var(--red-dim)',
                color: importResult.success ? 'var(--green)' : 'var(--red)',
                border: `1px solid ${importResult.success ? 'rgba(61,214,140,0.2)' : 'rgba(248,81,73,0.2)'}`,
              }}>{importResult.message}</div>
            )}

            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <Button variant="ghost" onClick={() => setImportModal(false)}>Отмена</Button>
              <Button variant="primary" loading={importing} disabled={importFiles.length === 0} onClick={async () => {
                setImporting(true); setImportResult(null)
                try {
                  let res
                  if (importType === 'session') {
                    res = await importAPI.uploadSession(importFiles[0], importPhone)
                  } else if (importType === 'session-batch') {
                    res = await importAPI.uploadSessionsBatch(importFiles)
                  } else if (importType === 'tdata') {
                    res = await importAPI.uploadTData(importFiles[0], importPhone)
                  }
                  setImportResult({ success: true, message: res.data.message || 'Импорт успешен!' })
                  await load()
                } catch (err) {
                  setImportResult({ success: false, message: err.response?.data?.detail || 'Ошибка импорта' })
                }
                setImporting(false)
              }}>
                {importing ? 'Импорт...' : 'Импортировать'}
              </Button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  )
}