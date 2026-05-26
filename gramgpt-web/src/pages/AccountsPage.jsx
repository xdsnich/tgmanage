import { useEffect, useState, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { accountsAPI, importAPI, proxiesAPI, channelsAPI, diagnosticsAPI } from '../services/api'
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
  const [filterGeo, setFilterGeo] = useState('all')
  const [filterCategory, setFilterCategory] = useState('all')
  const [geoList, setGeoList] = useState([])
  const [categoryList, setCategoryList] = useState([])
  const [newGeoInput, setNewGeoInput] = useState(false)
  const [newCatInput, setNewCatInput] = useState(false)
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
  const [importProxyId, setImportProxyId] = useState(null)
  const [importProxies, setImportProxies] = useState([])
  const [tdataDetected, setTdataDetected] = useState([])  // [{index, phone, name, proxy_string}]
  const [tdataSessionId, setTdataSessionId] = useState(null)
  const [tdataStep, setTdataStep] = useState('upload')  // upload | assign | importing
  const fileInputRef = useRef(null)
  const avatarInputRef = useRef(null)
  const postPhotoInputRef = useRef(null)

  // Channel creation
  const [channelModal, setChannelModal] = useState(false)
  const [channelAccount, setChannelAccount] = useState(null)
  const [channelForm, setChannelForm] = useState({ title: '', username: '', description: '', first_post: '', pin_to_profile: true })
  const [channelAvatar, setChannelAvatar] = useState(null)
  const [channelPostPhoto, setChannelPostPhoto] = useState(null)
  const [creating, setCreating] = useState(false)
  const [channelResult, setChannelResult] = useState(null)

  // Diagnostics: test join
  const [diagModal, setDiagModal] = useState(false)
  const [diagAccountId, setDiagAccountId] = useState(null)
  const [diagChannel, setDiagChannel] = useState('')
  const [diagLeaveAfter, setDiagLeaveAfter] = useState(false)
  const [diagRunning, setDiagRunning] = useState(false)
  const [diagResult, setDiagResult] = useState(null)

  const load = async () => {
    setLoading(true)
    try {
      const [accRes, filRes] = await Promise.all([accountsAPI.list(), accountsAPI.filters().catch(() => ({ data: { geos: [], categories: [] } }))])
      setAccounts(accRes.data)
      setGeoList(filRes.data.geos || [])
      setCategoryList(filRes.data.categories || [])
    } catch { }
    setLoading(false)
  }

  useEffect(() => { load() }, [])

  const filtered = accounts.filter(a => {
    const q = search.toLowerCase()
    const matchSearch = !q || [a.phone, a.username, a.first_name, a.last_name, a.status, a.geo, a.category].some(v => (v || '').toLowerCase().includes(q))
    const matchStatus = filterStatus === 'all' || a.status === filterStatus
    const matchGeo = filterGeo === 'all' || (a.geo || '') === filterGeo
    const matchCategory = filterCategory === 'all' || (a.category || '') === filterCategory
    return matchSearch && matchStatus && matchGeo && matchCategory
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
    setEditData({ role: acc.role, notes: acc.notes || '', tags: acc.tags || [], geo: acc.geo || '', category: acc.category || '' })
    setEditModal(true)
  }

  const openChannelModal = (acc, e) => {
    e.stopPropagation()
    setChannelAccount(acc)
    setChannelForm({ title: '', username: '', description: '', first_post: '', pin_to_profile: true })
    setChannelAvatar(null)
    setChannelPostPhoto(null)
    setChannelResult(null)
    setChannelModal(true)
  }

  const handleCreateChannel = async (e) => {
    e.preventDefault()
    setCreating(true)
    try {
      const fd = new FormData()
      fd.append('account_id', channelAccount.id)
      fd.append('title', channelForm.title)
      fd.append('description', channelForm.description)
      fd.append('username', channelForm.username.replace(/^@/, ''))
      fd.append('first_post', channelForm.first_post)
      fd.append('pin_to_profile', channelForm.pin_to_profile)
      if (channelAvatar) fd.append('avatar', channelAvatar)
      if (channelPostPhoto) fd.append('post_photo', channelPostPhoto)
      const { data } = await channelsAPI.createFull(fd)
      setChannelResult({ success: data.success, channel: data.channel, pinned: data.pinned_to_profile, hasPost: data.first_post_published, avatarSet: data.avatar_set })
      await load()
    } catch (err) {
      setChannelResult({ success: false, error: err.response?.data?.detail || 'Ошибка создания канала' })
    }
    setCreating(false)
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
          <Button variant="ghost" onClick={() => {
            setDiagAccountId(accounts[0]?.id || null); setDiagChannel(''); setDiagResult(null); setDiagLeaveAfter(false); setDiagModal(true)
          }}>🔍 Тест подписки</Button>
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

      {/* Фильтры: Гео + Тематика */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 14 }}>
        {/* Гео */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 10, color: 'var(--text-3)', fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase', minWidth: 70 }}>🌍 Гео</span>
          <button onClick={() => setFilterGeo('all')} style={{
            padding: '5px 12px', borderRadius: 20, cursor: 'pointer', fontSize: 11, fontWeight: filterGeo === 'all' ? 600 : 400, transition: 'all 0.15s',
            border: `1px solid ${filterGeo === 'all' ? 'rgba(59,130,246,0.4)' : 'var(--border)'}`,
            background: filterGeo === 'all' ? 'rgba(59,130,246,0.15)' : 'transparent',
            color: filterGeo === 'all' ? 'var(--blue)' : 'var(--text-3)',
          }}>Все</button>
          {geoList.map(g => (
            <button key={g} onClick={() => setFilterGeo(filterGeo === g ? 'all' : g)} style={{
              padding: '5px 12px', borderRadius: 20, cursor: 'pointer', fontSize: 11, fontWeight: filterGeo === g ? 600 : 400, transition: 'all 0.15s',
              border: `1px solid ${filterGeo === g ? 'rgba(59,130,246,0.4)' : 'var(--border)'}`,
              background: filterGeo === g ? 'rgba(59,130,246,0.15)' : 'transparent',
              color: filterGeo === g ? 'var(--blue)' : 'var(--text-3)',
            }}>{g}</button>
          ))}
          {!newGeoInput ? (
            <button onClick={() => setNewGeoInput(true)} style={{
              padding: '5px 10px', borderRadius: 20, cursor: 'pointer', fontSize: 11,
              border: '1px dashed var(--border)', background: 'transparent', color: 'var(--text-3)',
            }}>+ Добавить</button>
          ) : (
            <form onSubmit={e => { e.preventDefault(); const v = e.target.geo.value.trim(); if (v && !geoList.includes(v)) { setGeoList([...geoList, v]); } setNewGeoInput(false) }} style={{ display: 'flex', gap: 4 }}>
              <input name="geo" autoFocus placeholder="UA, US, DE..." style={{
                padding: '4px 10px', borderRadius: 20, border: '1px solid rgba(59,130,246,0.4)', background: 'rgba(59,130,246,0.08)',
                color: 'var(--text)', fontSize: 11, outline: 'none', width: 80,
              }} />
              <button type="submit" style={{ padding: '4px 8px', borderRadius: 20, border: '1px solid rgba(61,214,140,0.4)', background: 'rgba(61,214,140,0.1)', color: 'var(--green)', fontSize: 11, cursor: 'pointer' }}>✓</button>
              <button type="button" onClick={() => setNewGeoInput(false)} style={{ padding: '4px 8px', borderRadius: 20, border: '1px solid var(--border)', background: 'transparent', color: 'var(--text-3)', fontSize: 11, cursor: 'pointer' }}>✕</button>
            </form>
          )}
        </div>

        {/* Тематика */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 10, color: 'var(--text-3)', fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase', minWidth: 70 }}>📁 Тема</span>
          <button onClick={() => setFilterCategory('all')} style={{
            padding: '5px 12px', borderRadius: 20, cursor: 'pointer', fontSize: 11, fontWeight: filterCategory === 'all' ? 600 : 400, transition: 'all 0.15s',
            border: `1px solid ${filterCategory === 'all' ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
            background: filterCategory === 'all' ? 'rgba(124,77,255,0.15)' : 'transparent',
            color: filterCategory === 'all' ? 'var(--violet)' : 'var(--text-3)',
          }}>Все</button>
          {categoryList.map(c => (
            <button key={c} onClick={() => setFilterCategory(filterCategory === c ? 'all' : c)} style={{
              padding: '5px 12px', borderRadius: 20, cursor: 'pointer', fontSize: 11, fontWeight: filterCategory === c ? 600 : 400, transition: 'all 0.15s',
              border: `1px solid ${filterCategory === c ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
              background: filterCategory === c ? 'rgba(124,77,255,0.15)' : 'transparent',
              color: filterCategory === c ? 'var(--violet)' : 'var(--text-3)',
            }}>{c}</button>
          ))}
          {!newCatInput ? (
            <button onClick={() => setNewCatInput(true)} style={{
              padding: '5px 10px', borderRadius: 20, cursor: 'pointer', fontSize: 11,
              border: '1px dashed var(--border)', background: 'transparent', color: 'var(--text-3)',
            }}>+ Добавить</button>
          ) : (
            <form onSubmit={e => { e.preventDefault(); const v = e.target.cat.value.trim(); if (v && !categoryList.includes(v)) { setCategoryList([...categoryList, v]); } setNewCatInput(false) }} style={{ display: 'flex', gap: 4 }}>
              <input name="cat" autoFocus placeholder="Крипто, Новини..." style={{
                padding: '4px 10px', borderRadius: 20, border: '1px solid rgba(124,77,255,0.4)', background: 'rgba(124,77,255,0.08)',
                color: 'var(--text)', fontSize: 11, outline: 'none', width: 120,
              }} />
              <button type="submit" style={{ padding: '4px 8px', borderRadius: 20, border: '1px solid rgba(61,214,140,0.4)', background: 'rgba(61,214,140,0.1)', color: 'var(--green)', fontSize: 11, cursor: 'pointer' }}>✓</button>
              <button type="button" onClick={() => setNewCatInput(false)} style={{ padding: '4px 8px', borderRadius: 20, border: '1px solid var(--border)', background: 'transparent', color: 'var(--text-3)', fontSize: 11, cursor: 'pointer' }}>✕</button>
            </form>
          )}
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
            display: 'grid', gridTemplateColumns: '2fr 1.2fr 0.8fr 0.8fr 1fr 80px 130px',
            padding: '10px 20px', borderBottom: '1px solid var(--border)',
            fontSize: 10, color: 'var(--text-3)', letterSpacing: '0.1em', fontWeight: 700, textTransform: 'uppercase',
          }}>
            <span>Аккаунт</span><span>Телефон</span><span>Гео</span><span>Тема</span><span>Статус</span><span>Trust</span><span style={{ textAlign: 'right' }}>Действия</span>
          </div>

          {filtered.map((acc, i) => (
            <div key={acc.id} onClick={() => navigate(`/accounts/${acc.id}`)} style={{
              display: 'grid', gridTemplateColumns: '2fr 1.2fr 0.8fr 0.8fr 1fr 80px 130px',
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
              <div>{acc.geo ? <span style={{ fontSize: 11, padding: '2px 8px', borderRadius: 10, background: 'rgba(59,130,246,0.1)', color: 'rgba(59,130,246,0.8)', border: '1px solid rgba(59,130,246,0.2)' }}>{acc.geo}</span> : <span style={{ fontSize: 10, color: 'var(--text-3)' }}>—</span>}</div>
              <div>{acc.category ? <span style={{ fontSize: 11, padding: '2px 8px', borderRadius: 10, background: 'rgba(124,77,255,0.1)', color: 'rgba(124,77,255,0.8)', border: '1px solid rgba(124,77,255,0.2)' }}>{acc.category}</span> : <span style={{ fontSize: 10, color: 'var(--text-3)' }}>—</span>}</div>
              <StatusBadge status={acc.status} />
              <TrustBar score={acc.trust_score} />
              <div style={{ display: 'flex', gap: 4, justifyContent: 'flex-end' }}>
                <button onClick={(e) => openChannelModal(acc, e)} title="Создать канал" style={{
                  width: 28, height: 28, borderRadius: 6, border: '1px solid var(--border)',
                  background: 'transparent', color: 'var(--text-3)', cursor: 'pointer',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 13,
                  transition: 'all 0.15s',
                }}
                  onMouseEnter={e => { e.currentTarget.style.borderColor = 'rgba(0,194,178,0.5)'; e.currentTarget.style.color = 'var(--teal)' }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.color = 'var(--text-3)' }}>
                  📺
                </button>
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

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>🌍 Гео</label>
              <input list="geo-list" value={editData.geo || ''} onChange={e => setEditData(d => ({ ...d, geo: e.target.value }))}
                placeholder="Введи или выбери" style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 13, outline: 'none' }} />
              <datalist id="geo-list">{geoList.map(g => <option key={g} value={g} />)}</datalist>
            </div>
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>📁 Тематика</label>
              <input list="cat-list" value={editData.category || ''} onChange={e => setEditData(d => ({ ...d, category: e.target.value }))}
                placeholder="Введи или выбери" style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 13, outline: 'none' }} />
              <datalist id="cat-list">{categoryList.map(c => <option key={c} value={c} />)}</datalist>
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" type="button" onClick={() => setEditModal(false)}>Отмена</Button>
            <Button variant="primary" type="submit" loading={saving}>Сохранить</Button>
          </div>
        </form>
      </Modal>

      {/* Diagnostics: test join modal */}
      <Modal open={diagModal} onClose={() => setDiagModal(false)} title="🔍 Тест подписки на канал" width={620}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ padding: '10px 14px', background: 'rgba(61,139,255,0.06)', border: '1px solid rgba(61,139,255,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>
            Симулирует тот же процесс что делает <b>plan_executor</b> при подписке на канал в кампании.
            Покажет каждый шаг и точную ошибку Telegram — резолв, pre-check, JoinRequest, верификация.
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Аккаунт</label>
              <select value={diagAccountId || ''} onChange={e => setDiagAccountId(e.target.value ? parseInt(e.target.value) : null)}
                style={{ width: '100%', padding: '10px 12px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 13, outline: 'none' }}>
                <option value="">— Выбери аккаунт —</option>
                {accounts.map(a => (
                  <option key={a.id} value={a.id}>
                    {a.first_name || a.phone} {a.username ? `(@${a.username})` : ''} · {a.status}
                  </option>
                ))}
              </select>
            </div>
            <Input label="Канал (@username)" value={diagChannel}
              onChange={e => setDiagChannel(e.target.value)}
              placeholder="@DC_Draino или DC_Draino" />
          </div>

          <label style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', fontSize: 13, color: 'var(--text-2)' }}>
            <input type="checkbox" checked={diagLeaveAfter} onChange={e => setDiagLeaveAfter(e.target.checked)}
              style={{ width: 16, height: 16, accentColor: 'var(--violet)', cursor: 'pointer' }} />
            <span>Выйти из канала после теста (только если только что вступили)</span>
          </label>

          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" onClick={() => setDiagModal(false)}>Закрыть</Button>
            <Button variant="primary" loading={diagRunning}
              disabled={!diagAccountId || !diagChannel.trim()}
              onClick={async () => {
                setDiagRunning(true); setDiagResult(null)
                try {
                  const { data } = await diagnosticsAPI.testJoin(diagAccountId, diagChannel.trim(), diagLeaveAfter)
                  setDiagResult(data)
                } catch (err) {
                  setDiagResult({
                    success: false,
                    error: err.response?.data?.detail || err.message || 'Ошибка',
                    error_type: 'NetworkError',
                    steps: [],
                  })
                }
                setDiagRunning(false)
              }}>
              ▶️ Запустить тест
            </Button>
          </div>

          {diagResult && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 4 }}>
              {/* Summary */}
              <div style={{
                padding: '14px 16px', borderRadius: 10,
                background: diagResult.success ? 'var(--green-dim)' : 'var(--red-dim)',
                border: `1px solid ${diagResult.success ? 'rgba(61,214,140,0.25)' : 'rgba(248,81,73,0.25)'}`,
              }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <div style={{ fontSize: 15, fontWeight: 700, color: diagResult.success ? 'var(--green)' : 'var(--red)' }}>
                    {diagResult.success
                      ? (diagResult.already_in ? '✓ Уже подписан' : '✓ Подписка работает')
                      : '✕ Подписка не прошла'}
                  </div>
                  {diagResult.elapsed_seconds != null && (
                    <div style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'var(--font-mono)' }}>{diagResult.elapsed_seconds}с</div>
                  )}
                </div>
                {diagResult.error && (
                  <div style={{ marginTop: 6, fontSize: 12, color: 'var(--text-2)' }}>
                    <b>{diagResult.error_type || 'Error'}:</b> {diagResult.error}
                  </div>
                )}
              </div>

              {/* Steps */}
              {diagResult.steps && diagResult.steps.length > 0 && (
                <div style={{ background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, overflow: 'hidden' }}>
                  {diagResult.steps.map((s, i) => (
                    <div key={i} style={{
                      display: 'flex', gap: 12, padding: '12px 14px',
                      borderBottom: i < diagResult.steps.length - 1 ? '1px solid var(--border)' : 'none',
                      alignItems: 'flex-start',
                    }}>
                      <div style={{
                        width: 24, height: 24, borderRadius: 12, flexShrink: 0,
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                        background: s.ok ? 'var(--green-dim)' : 'var(--red-dim)',
                        color: s.ok ? 'var(--green)' : 'var(--red)',
                        fontSize: 12, fontWeight: 700,
                      }}>{s.ok ? '✓' : '✕'}</div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)' }}>{s.label}</span>
                          <span style={{ fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--font-mono)' }}>{s.step}</span>
                        </div>
                        {s.detail && <div style={{ fontSize: 12, color: 'var(--text-2)', marginTop: 3, lineHeight: 1.5 }}>{s.detail}</div>}
                        {s.error && (
                          <div style={{
                            marginTop: 6, padding: '6px 10px', borderRadius: 6,
                            background: 'rgba(248,81,73,0.08)', border: '1px solid rgba(248,81,73,0.2)',
                            fontSize: 11, fontFamily: 'var(--font-mono)', color: 'var(--red)',
                            wordBreak: 'break-word',
                          }}>
                            {s.error_type && <b>{s.error_type}: </b>}{s.error}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </Modal>

      {/* Channel creation modal */}
      <Modal open={channelModal} onClose={() => { setChannelModal(false); setChannelResult(null) }}
        title={`Создать канал · ${channelAccount?.first_name || channelAccount?.phone || ''}`} width={540}>
        {channelResult ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16, alignItems: 'center', padding: '24px 0', textAlign: 'center' }}>
            {channelResult.success ? (
              <>
                <div style={{ fontSize: 48 }}>📺</div>
                <div style={{ fontSize: 17, fontWeight: 700, color: 'var(--green)' }}>Канал создан!</div>
                {channelResult.channel && (
                  <div style={{ padding: '12px 16px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, width: '100%', textAlign: 'left' }}>
                    <div style={{ fontWeight: 600, fontSize: 14 }}>{channelResult.channel.title}</div>
                    {channelResult.channel.link && <div style={{ fontSize: 12, color: 'var(--teal)', marginTop: 4 }}>{channelResult.channel.link}</div>}
                  </div>
                )}
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, justifyContent: 'center' }}>
                  {channelResult.avatarSet && <span style={{ fontSize: 11, padding: '3px 10px', borderRadius: 6, background: 'var(--green-dim)', color: 'var(--green)', border: '1px solid rgba(61,214,140,0.25)' }}>✓ Аватар загружен</span>}
                  {channelResult.hasPost && <span style={{ fontSize: 11, padding: '3px 10px', borderRadius: 6, background: 'var(--blue-dim)', color: 'var(--blue)', border: '1px solid rgba(61,139,255,0.25)' }}>✓ Первый пост опубликован</span>}
                  {channelResult.pinned && <span style={{ fontSize: 11, padding: '3px 10px', borderRadius: 6, background: 'var(--violet-dim)', color: 'var(--violet)', border: '1px solid rgba(124,77,255,0.25)' }}>✓ Закреплён в профиле</span>}
                </div>
                <Button variant="primary" onClick={() => { setChannelModal(false); setChannelResult(null) }}>Готово</Button>
              </>
            ) : (
              <>
                <div style={{ fontSize: 48 }}>❌</div>
                <div style={{ fontSize: 14, color: 'var(--red)' }}>{channelResult.error}</div>
                <Button variant="ghost" onClick={() => setChannelResult(null)}>← Попробовать снова</Button>
              </>
            )}
          </div>
        ) : (
          <form onSubmit={handleCreateChannel} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <Input label="Название канала *" value={channelForm.title} required
              onChange={e => setChannelForm(f => ({ ...f, title: e.target.value }))}
              placeholder="Мой крутой канал" autoFocus />

            <Input label="@username (необязательно)" value={channelForm.username}
              onChange={e => setChannelForm(f => ({ ...f, username: e.target.value }))}
              placeholder="@mychannel" />

            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Описание</label>
              <textarea value={channelForm.description} rows={2}
                onChange={e => setChannelForm(f => ({ ...f, description: e.target.value }))}
                placeholder="Краткое описание канала"
                style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 14, outline: 'none', resize: 'vertical', fontFamily: 'var(--font-sans)' }} />
            </div>

            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Первый пост</label>
              <textarea value={channelForm.first_post} rows={3}
                onChange={e => setChannelForm(f => ({ ...f, first_post: e.target.value }))}
                placeholder="Текст первого сообщения (необязательно)"
                style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 14, outline: 'none', resize: 'vertical', fontFamily: 'var(--font-sans)' }} />
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
              {/* Avatar upload */}
              <div>
                <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Аватар канала</label>
                <div onClick={() => avatarInputRef.current?.click()} style={{
                  height: 80, border: '2px dashed var(--border)', borderRadius: 10,
                  display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
                  cursor: 'pointer', transition: 'border-color 0.15s', gap: 4,
                }}
                  onMouseEnter={e => e.currentTarget.style.borderColor = 'var(--violet)'}
                  onMouseLeave={e => e.currentTarget.style.borderColor = 'var(--border)'}>
                  {channelAvatar ? (
                    <>
                      <span style={{ fontSize: 11, color: 'var(--green)', fontWeight: 600 }}>✓ {channelAvatar.name.length > 16 ? channelAvatar.name.slice(0, 14) + '…' : channelAvatar.name}</span>
                      <span style={{ fontSize: 10, color: 'var(--text-3)' }}>нажми чтобы сменить</span>
                    </>
                  ) : (
                    <>
                      <span style={{ fontSize: 22 }}>🖼</span>
                      <span style={{ fontSize: 11, color: 'var(--text-3)' }}>выбрать фото</span>
                    </>
                  )}
                </div>
                <input ref={avatarInputRef} type="file" accept="image/*" style={{ display: 'none' }}
                  onChange={e => setChannelAvatar(e.target.files[0] || null)} />
              </div>

              {/* Post photo upload */}
              <div>
                <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Фото к посту</label>
                <div onClick={() => postPhotoInputRef.current?.click()} style={{
                  height: 80, border: '2px dashed var(--border)', borderRadius: 10,
                  display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
                  cursor: 'pointer', transition: 'border-color 0.15s', gap: 4,
                }}
                  onMouseEnter={e => e.currentTarget.style.borderColor = 'var(--violet)'}
                  onMouseLeave={e => e.currentTarget.style.borderColor = 'var(--border)'}>
                  {channelPostPhoto ? (
                    <>
                      <span style={{ fontSize: 11, color: 'var(--green)', fontWeight: 600 }}>✓ {channelPostPhoto.name.length > 16 ? channelPostPhoto.name.slice(0, 14) + '…' : channelPostPhoto.name}</span>
                      <span style={{ fontSize: 10, color: 'var(--text-3)' }}>нажми чтобы сменить</span>
                    </>
                  ) : (
                    <>
                      <span style={{ fontSize: 22 }}>📸</span>
                      <span style={{ fontSize: 11, color: 'var(--text-3)' }}>фото к посту</span>
                    </>
                  )}
                </div>
                <input ref={postPhotoInputRef} type="file" accept="image/*" style={{ display: 'none' }}
                  onChange={e => setChannelPostPhoto(e.target.files[0] || null)} />
              </div>
            </div>

            {/* Pin toggle */}
            <label style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', padding: '10px 12px', background: 'var(--bg-3)', borderRadius: 8, border: '1px solid var(--border)' }}>
              <input type="checkbox" checked={channelForm.pin_to_profile}
                onChange={e => setChannelForm(f => ({ ...f, pin_to_profile: e.target.checked }))}
                style={{ width: 16, height: 16, accentColor: 'var(--violet)', cursor: 'pointer' }} />
              <div>
                <div style={{ fontSize: 13, fontWeight: 600 }}>Закрепить канал в профиле</div>
                <div style={{ fontSize: 11, color: 'var(--text-3)' }}>Канал появится как личный канал аккаунта</div>
              </div>
            </label>

            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <Button variant="ghost" type="button" onClick={() => setChannelModal(false)}>Отмена</Button>
              <Button variant="primary" type="submit" loading={creating}>📺 Создать канал</Button>
            </div>
          </form>
        )}
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

            {importType === 'session' && (
              <Input label="Номер телефона (необязательно)" value={importPhone} onChange={e => setImportPhone(e.target.value)} placeholder="+380..." />
            )}

            {importType === 'session' && (
              <div>
                <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Прокси (рекомендуется)</label>
                <select value={importProxyId || ''} onChange={e => setImportProxyId(e.target.value ? parseInt(e.target.value) : null)}
                  onFocus={async () => { if (!importProxies.length) { try { const { data } = await proxiesAPI.list(); setImportProxies(data) } catch { } } }}
                  style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 14, outline: 'none' }}>
                  <option value="">Без прокси</option>
                  {importProxies.map(p => <option key={p.id} value={p.id}>{p.host}:{p.port} ({p.protocol})</option>)}
                </select>
              </div>
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
                    // Шаг 1: Детектим аккаунты
                    const { data } = await accountsAPI.detectTData(importFiles[0])
                    setTdataSessionId(data.session_id)
                    setTdataDetected(data.accounts.map(a => ({ ...a, proxy_string: '' })))
                    setTdataStep('assign')
                    setImporting(false)
                    return // Не закрываем модал — показываем таблицу
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

        {/* TData Batch — таблица аккаунтов с прокси */}
        {tdataStep === 'assign' && tdataDetected.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <button onClick={() => { setTdataStep('upload'); setTdataDetected([]); setImportType(null) }} style={{
              background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer', fontSize: 12, padding: 0, textAlign: 'left',
            }}>← Назад</button>

            <div style={{ padding: '10px 14px', background: 'rgba(61,214,140,0.08)', border: '1px solid rgba(61,214,140,0.2)', borderRadius: 10, fontSize: 13, color: 'var(--green)' }}>
              Найдено {tdataDetected.length} аккаунтов. Назначьте прокси и нажмите "Импортировать".
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {tdataDetected.map((acc, i) => (
                <div key={i} style={{ padding: '12px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, display: 'flex', alignItems: 'center', gap: 12 }}>
                  <div style={{ flex: '0 0 30px', fontSize: 18, textAlign: 'center' }}>👤</div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 600, fontSize: 13 }}>{acc.name || 'Без имени'}</div>
                    <div style={{ fontSize: 11, color: 'var(--text-3)' }}>{acc.phone || 'Номер не определён'} {acc.username ? `@${acc.username}` : ''}</div>
                  </div>
                  <div style={{ flex: '0 0 280px' }}>
                    <input
                      value={acc.proxy_string}
                      onChange={e => setTdataDetected(prev => prev.map((a, j) => j === i ? { ...a, proxy_string: e.target.value } : a))}
                      placeholder="ip:port:login:password"
                      style={{ width: '100%', padding: '8px 10px', background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 12, outline: 'none', fontFamily: 'monospace' }}
                    />
                  </div>
                </div>
              ))}
            </div>

            {/* Быстрое назначение одного прокси всем */}
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <input id="bulk-proxy" placeholder="Один прокси для всех (ip:port:login:password)" style={{ flex: 1, padding: '8px 12px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 12, outline: 'none', fontFamily: 'monospace' }} />
              <Button variant="outline" size="sm" onClick={() => {
                const v = document.getElementById('bulk-proxy')?.value || ''
                if (v) setTdataDetected(prev => prev.map(a => ({ ...a, proxy_string: v })))
              }}>Применить ко всем</Button>
            </div>

            {/* Или выбрать из существующих */}
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <select id="bulk-proxy-select"
                onFocus={async () => { if (!importProxies.length) { try { const { data } = await proxiesAPI.list(); setImportProxies(data) } catch { } } }}
                style={{ flex: 1, padding: '8px 12px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 12, outline: 'none' }}>
                <option value="">Выбрать из существующих прокси</option>
                {importProxies.map(p => <option key={p.id} value={`${p.host}:${p.port}:${p.login || ''}:${p.password || ''}`}>{p.host}:{p.port} ({p.protocol})</option>)}
              </select>
              <Button variant="outline" size="sm" onClick={() => {
                const v = document.getElementById('bulk-proxy-select')?.value || ''
                if (v) setTdataDetected(prev => prev.map(a => ({ ...a, proxy_string: v })))
              }}>Применить</Button>
            </div>

            {importResult && (
              <div style={{
                padding: '10px 14px', borderRadius: 10, fontSize: 13,
                background: importResult.success ? 'var(--green-dim)' : 'var(--red-dim)',
                color: importResult.success ? 'var(--green)' : 'var(--red)',
              }}>{importResult.message}</div>
            )}

            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <Button variant="ghost" onClick={() => { setImportModal(false); setTdataStep('upload'); setTdataDetected([]) }}>Отмена</Button>
              <Button variant="primary" loading={importing} onClick={async () => {
                setImporting(true); setImportResult(null)
                try {
                  const accounts = tdataDetected.map(a => ({ index: a.index, proxy_string: a.proxy_string }))
                  const { data } = await accountsAPI.importTDataBatch(tdataSessionId, accounts)
                  setImportResult({ success: true, message: `Импортировано ${data.success}/${data.total} аккаунтов` })
                  await load()
                  // Закрываем через 1.5с
                  setTimeout(() => { setImportModal(false); setTdataStep('upload'); setTdataDetected([]); setImportResult(null) }, 1500)
                } catch (err) {
                  setImportResult({ success: false, message: err.response?.data?.detail || 'Ошибка импорта' })
                }
                setImporting(false)
              }}>
                📦 Импортировать {tdataDetected.length} аккаунтов
              </Button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  )
}