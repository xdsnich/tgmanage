import { useEffect, useState } from 'react'
import { apiAppsAPI } from '../services/api'
import { Card, Button, Input, Modal, Badge, Spinner, Empty, StatusBadge } from '../components/ui'

export default function ApiKeysPage() {
  const [apps, setApps] = useState([])
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(true)
  const [addModal, setAddModal] = useState(false)
  const [editModal, setEditModal] = useState(false)
  const [detailModal, setDetailModal] = useState(false)
  const [selected, setSelected] = useState(null)
  const [detailApp, setDetailApp] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [saving, setSaving] = useState(false)

  const [form, setForm] = useState({ api_id: '', api_hash: '', title: '', max_accounts: 100, notes: '' })

  const load = async () => {
    setLoading(true)
    try {
      const [appsRes, statsRes] = await Promise.all([
        apiAppsAPI.list(),
        apiAppsAPI.stats(),
      ])
      setApps(appsRes.data)
      setStats(statsRes.data)
    } catch (err) {
      console.error(err)
    }
    setLoading(false)
  }

  useEffect(() => { load() }, [])

  const handleAdd = async () => {
    if (!form.api_id || !form.api_hash) {
      alert('Заполни API ID и API Hash')
      return
    }
    setSaving(true)
    try {
      await apiAppsAPI.create({
        api_id: parseInt(form.api_id),
        api_hash: form.api_hash.trim(),
        title: form.title.trim(),
        max_accounts: parseInt(form.max_accounts) || 100,
        notes: form.notes,
      })
      setAddModal(false)
      setForm({ api_id: '', api_hash: '', title: '', max_accounts: 100, notes: '' })
      await load()
    } catch (err) {
      alert(err.response?.data?.detail || 'Ошибка добавления')
    }
    setSaving(false)
  }

  const handleToggle = async (app, e) => {
    e.stopPropagation()
    try {
      await apiAppsAPI.update(app.id, { is_active: !app.is_active })
      await load()
    } catch (err) {
      alert(err.response?.data?.detail || 'Ошибка')
    }
  }

  const handleDelete = async (app, e) => {
    e.stopPropagation()
    if (app.accounts_count > 0) {
      alert(`Нельзя удалить — на этом ключе ${app.accounts_count} аккаунтов. Сессии привязаны к этому api_id.`)
      return
    }
    if (!window.confirm(`Удалить "${app.title}"?`)) return
    try {
      await apiAppsAPI.delete(app.id)
      await load()
    } catch (err) {
      alert(err.response?.data?.detail || 'Ошибка')
    }
  }

  const handleOpenEdit = (app, e) => {
    e.stopPropagation()
    setSelected({ ...app })
    setEditModal(true)
  }

  const handleSaveEdit = async () => {
    if (!selected) return
    setSaving(true)
    try {
      await apiAppsAPI.update(selected.id, {
        title: selected.title,
        max_accounts: parseInt(selected.max_accounts),
        notes: selected.notes,
      })
      setEditModal(false)
      await load()
    } catch (err) {
      alert(err.response?.data?.detail || 'Ошибка')
    }
    setSaving(false)
  }

  const handleOpenDetail = async (app) => {
    setDetailLoading(true)
    setDetailModal(true)
    try {
      const { data } = await apiAppsAPI.get(app.id)
      setDetailApp(data)
    } catch (err) {
      alert(err.response?.data?.detail || 'Ошибка загрузки')
      setDetailModal(false)
    }
    setDetailLoading(false)
  }

  const usedPercent = (used, max) => max ? Math.round(used / max * 100) : 0

  return (
    <div style={{ padding: '28px 32px', animation: 'fadeUp 0.4s cubic-bezier(0.16,1,0.3,1)' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 28 }}>
        <div>
          <div style={{ fontSize: 11, color: '#ff6b35', fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>🔑 API КЛЮЧИ</div>
          <h1 style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.04em' }}>Мульти-API управление</h1>
          <p style={{ fontSize: 13, color: 'var(--text-3)', marginTop: 4 }}>
            Распределяй аккаунты по разным API-приложениям для защиты от бана
          </p>
        </div>
        <Button variant="primary" onClick={() => setAddModal(true)}>+ Добавить API</Button>
      </div>

      {/* Stats cards */}
      {stats && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginBottom: 24 }}>
          {[
            { label: 'API-приложений', value: stats.total_apps, color: '#7c4dff' },
            { label: 'Ёмкость', value: stats.total_capacity, color: '#3d8bff' },
            { label: 'На API ключах', value: stats.total_used, color: '#3dd68c' },
            { label: 'На глобальном ключе', value: stats.on_global_key, color: stats.on_global_key > 0 ? '#e3a13f' : '#888' },
          ].map(({ label, value, color }) => (
            <div key={label} style={{
              background: 'var(--bg-2)', border: '1px solid var(--border)',
              borderRadius: 'var(--radius)', padding: '18px 20px',
            }}>
              <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 6, letterSpacing: '0.04em' }}>{label}</div>
              <div style={{ fontSize: 28, fontWeight: 800, color, letterSpacing: '-0.03em' }}>{value}</div>
            </div>
          ))}
        </div>
      )}

      {/* Apps list */}
      {loading ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 64 }}><Spinner size={28} /></div>
      ) : apps.length === 0 ? (
        <Empty
          icon="🔑"
          title="Нет API-приложений"
          subtitle="Добавь API ключи с my.telegram.org для безопасного масштабирования"
          action={<Button variant="primary" onClick={() => setAddModal(true)}>+ Добавить первый API</Button>}
        />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {apps.map(app => {
            const pct = usedPercent(app.accounts_count, app.max_accounts)
            const barColor = pct >= 90 ? '#f85149' : pct >= 70 ? '#e3a13f' : '#3dd68c'

            return (
              <div key={app.id} onClick={() => handleOpenDetail(app)} style={{
                background: 'var(--bg-2)', border: '1px solid var(--border)',
                borderRadius: 'var(--radius)', padding: '20px 24px',
                opacity: app.is_active ? 1 : 0.5,
                transition: 'all 0.2s',
                cursor: 'pointer',
              }}
                onMouseEnter={e => { e.currentTarget.style.borderColor = 'rgba(124,77,255,0.35)'; e.currentTarget.style.transform = 'translateY(-1px)' }}
                onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.transform = 'translateY(0)' }}
              >
                {/* Top row */}
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                    <div style={{
                      width: 36, height: 36, borderRadius: 10, flexShrink: 0,
                      background: app.is_active
                        ? 'linear-gradient(135deg, rgba(124,77,255,0.25), rgba(61,139,255,0.15))'
                        : 'rgba(255,255,255,0.05)',
                      border: `1px solid ${app.is_active ? 'rgba(124,77,255,0.2)' : 'var(--border)'}`,
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      fontSize: 16,
                    }}>🔑</div>
                    <div>
                      <div style={{ fontWeight: 700, fontSize: 14, letterSpacing: '-0.02em' }}>
                        {app.title || `App #${app.api_id}`}
                      </div>
                      <div style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'var(--font-mono)', marginTop: 2 }}>
                        api_id: {app.api_id} · hash: {app.api_hash.slice(0, 8)}...
                      </div>
                    </div>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <Badge color={app.is_active ? 'green' : 'default'}>
                      {app.is_active ? 'Активно' : 'Выключено'}
                    </Badge>
                    <button onClick={(e) => handleToggle(app, e)} title={app.is_active ? 'Выключить' : 'Включить'} style={{
                      background: 'none', border: '1px solid var(--border)', borderRadius: 8,
                      padding: '6px 10px', cursor: 'pointer', fontSize: 12,
                      color: 'var(--text-2)', transition: 'all 0.15s',
                    }}
                      onMouseEnter={e => e.currentTarget.style.borderColor = 'rgba(124,77,255,0.4)'}
                      onMouseLeave={e => e.currentTarget.style.borderColor = 'var(--border)'}
                    >{app.is_active ? '⏸' : '▶'}</button>
                    <button onClick={(e) => handleOpenEdit(app, e)} style={{
                      background: 'none', border: '1px solid var(--border)', borderRadius: 8,
                      padding: '6px 10px', cursor: 'pointer', fontSize: 12,
                      color: 'var(--text-2)', transition: 'all 0.15s',
                    }}
                      onMouseEnter={e => e.currentTarget.style.borderColor = 'rgba(124,77,255,0.4)'}
                      onMouseLeave={e => e.currentTarget.style.borderColor = 'var(--border)'}
                    >✏️</button>
                    <button onClick={(e) => handleDelete(app, e)} title={app.accounts_count > 0 ? 'Нельзя удалить — есть привязанные аккаунты' : 'Удалить'} style={{
                      background: 'none', border: '1px solid var(--border)', borderRadius: 8,
                      padding: '6px 10px', cursor: app.accounts_count > 0 ? 'not-allowed' : 'pointer', fontSize: 12,
                      color: app.accounts_count > 0 ? 'var(--text-3)' : 'var(--red)',
                      opacity: app.accounts_count > 0 ? 0.4 : 1,
                      transition: 'all 0.15s',
                    }}
                      onMouseEnter={e => { if (app.accounts_count === 0) e.currentTarget.style.borderColor = 'rgba(248,81,73,0.4)' }}
                      onMouseLeave={e => e.currentTarget.style.borderColor = 'var(--border)'}
                    >🗑</button>
                  </div>
                </div>

                {/* Progress bar */}
                <div style={{ marginBottom: 8 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: 'var(--text-3)', marginBottom: 6 }}>
                    <span>Аккаунтов: {app.accounts_count} / {app.max_accounts}</span>
                    <span style={{ color: barColor, fontWeight: 600 }}>{pct}%</span>
                  </div>
                  <div style={{
                    height: 6, borderRadius: 3,
                    background: 'rgba(255,255,255,0.06)',
                    overflow: 'hidden',
                  }}>
                    <div style={{
                      height: '100%', borderRadius: 3,
                      width: `${Math.min(pct, 100)}%`,
                      background: barColor,
                      transition: 'width 0.6s cubic-bezier(0.16,1,0.3,1)',
                    }} />
                  </div>
                </div>

                {/* Click hint */}
                <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4 }}>
                  Нажми чтобы посмотреть аккаунты →
                </div>

                {app.notes && (
                  <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 6, fontStyle: 'italic' }}>
                    {app.notes}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {/* Info block */}
      <div style={{
        marginTop: 24, padding: '18px 22px',
        background: 'rgba(124,77,255,0.06)',
        border: '1px solid rgba(124,77,255,0.15)',
        borderRadius: 'var(--radius)',
        fontSize: 12, color: 'var(--text-2)', lineHeight: 1.7,
      }}>
        <div style={{ fontWeight: 700, marginBottom: 8, fontSize: 13 }}>💡 Как работает мульти-API</div>
        <div>
          1. Зайди на <span style={{ color: 'var(--blue)', fontFamily: 'var(--font-mono)' }}>my.telegram.org</span> → API development tools → создай несколько приложений<br />
          2. Добавь каждый api_id + api_hash сюда<br />
          3. При импорте <strong>новых</strong> аккаунтов система автоматически выберет ключ с наименьшей загрузкой<br />
          4. Ключ привязывается к аккаунту <strong>навсегда</strong> — перемещение сломает сессию<br />
          5. Рекомендация: 50–100 аккаунтов на одно API-приложение
        </div>
      </div>

      {stats?.on_global_key > 0 && (
        <div style={{
          marginTop: 12, padding: '14px 22px',
          background: 'rgba(227,161,63,0.06)',
          border: '1px solid rgba(227,161,63,0.15)',
          borderRadius: 'var(--radius)',
          fontSize: 12, color: 'var(--yellow)', lineHeight: 1.7,
        }}>
          ⚠️ <strong>{stats.on_global_key} аккаунтов</strong> на глобальном ключе из .env.
          Они были импортированы до добавления API-приложений. Новые аккаунты будут автоматически распределяться по добавленным ключам.
        </div>
      )}

      {/* ══ Detail Modal — список аккаунтов на ключе ══ */}
      {detailModal && (
        <Modal open={true} title={detailApp ? `${detailApp.title} — аккаунты` : 'Загрузка...'} onClose={() => { setDetailModal(false); setDetailApp(null) }} width={600}>
          {detailLoading ? (
            <div style={{ display: 'flex', justifyContent: 'center', padding: 40 }}><Spinner size={24} /></div>
          ) : detailApp ? (
            <div>
              {/* Key info */}
              <div style={{
                display: 'flex', gap: 16, marginBottom: 20, padding: '12px 16px',
                background: 'rgba(255,255,255,0.03)', borderRadius: 10,
                fontSize: 12, color: 'var(--text-3)', fontFamily: 'var(--font-mono)',
              }}>
                <span>api_id: {detailApp.api_id}</span>
                <span>hash: {detailApp.api_hash.slice(0, 16)}...</span>
                <span>лимит: {detailApp.accounts_count}/{detailApp.max_accounts}</span>
              </div>

              {/* Accounts list */}
              {detailApp.accounts.length === 0 ? (
                <div style={{ textAlign: 'center', padding: '32px 0', color: 'var(--text-3)', fontSize: 13 }}>
                  Пока нет аккаунтов на этом ключе.<br />
                  <span style={{ fontSize: 12 }}>Импортируй новые аккаунты — они автоматически попадут сюда.</span>
                </div>
              ) : (
                <div style={{ maxHeight: 400, overflowY: 'auto' }}>
                  {/* Table header */}
                  <div style={{
                    display: 'grid', gridTemplateColumns: '1.5fr 1fr 0.8fr',
                    padding: '8px 12px', fontSize: 10, color: 'var(--text-3)',
                    letterSpacing: '0.1em', fontWeight: 700, textTransform: 'uppercase',
                    borderBottom: '1px solid var(--border)',
                  }}>
                    <span>Аккаунт</span>
                    <span>Телефон</span>
                    <span>Статус</span>
                  </div>

                  {detailApp.accounts.map((acc, i) => (
                    <div key={acc.id} style={{
                      display: 'grid', gridTemplateColumns: '1.5fr 1fr 0.8fr',
                      padding: '12px 12px', alignItems: 'center',
                      borderBottom: i < detailApp.accounts.length - 1 ? '1px solid var(--border)' : 'none',
                      transition: 'background 0.1s',
                    }}
                      onMouseEnter={e => e.currentTarget.style.background = 'rgba(255,255,255,0.02)'}
                      onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
                    >
                      {/* Name */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                        <div style={{
                          width: 28, height: 28, borderRadius: 7, flexShrink: 0,
                          background: 'linear-gradient(135deg, rgba(124,77,255,0.25), rgba(61,139,255,0.15))',
                          border: '1px solid rgba(124,77,255,0.15)',
                          display: 'flex', alignItems: 'center', justifyContent: 'center',
                          fontSize: 11, fontWeight: 700, color: 'var(--violet)',
                        }}>{acc.first_name?.[0]?.toUpperCase() || '?'}</div>
                        <div>
                          <div style={{ fontSize: 13, fontWeight: 600 }}>
                            {acc.first_name || 'Без имени'}
                          </div>
                          {acc.username && (
                            <div style={{ fontSize: 11, color: 'var(--text-3)' }}>@{acc.username}</div>
                          )}
                        </div>
                      </div>

                      {/* Phone */}
                      <div style={{ fontSize: 12, color: 'var(--text-2)', fontFamily: 'var(--font-mono)' }}>
                        {acc.phone}
                      </div>

                      {/* Status */}
                      <StatusBadge status={acc.status} />
                    </div>
                  ))}
                </div>
              )}

              {/* Summary */}
              <div style={{
                marginTop: 16, padding: '10px 14px',
                background: 'rgba(124,77,255,0.06)',
                borderRadius: 8, fontSize: 12, color: 'var(--text-3)',
                display: 'flex', justifyContent: 'space-between',
              }}>
                <span>Всего: {detailApp.accounts_count} аккаунтов</span>
                <span>Свободно: {detailApp.max_accounts - detailApp.accounts_count} слотов</span>
              </div>
            </div>
          ) : null}
        </Modal>
      )}

      {/* ══ Add Modal ══ */}
      {addModal && (
        <Modal open={true} title="Добавить API-приложение" onClose={() => setAddModal(false)}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <Input
              label="API ID" type="number" autoFocus
              placeholder="12345678"
              value={form.api_id}
              onChange={e => setForm(f => ({ ...f, api_id: e.target.value }))}
            />
            <Input
              label="API Hash"
              placeholder="abcdef1234567890..."
              value={form.api_hash}
              onChange={e => setForm(f => ({ ...f, api_hash: e.target.value }))}
            />
            <Input
              label="Название (опционально)"
              placeholder="Мой App #1"
              value={form.title}
              onChange={e => setForm(f => ({ ...f, title: e.target.value }))}
            />
            <Input
              label="Макс. аккаунтов" type="number"
              placeholder="100"
              value={form.max_accounts}
              onChange={e => setForm(f => ({ ...f, max_accounts: e.target.value }))}
            />
            <div style={{ display: 'flex', gap: 10, marginTop: 8 }}>
              <Button variant="ghost" onClick={() => setAddModal(false)} style={{ flex: 1 }}>Отмена</Button>
              <Button variant="primary" onClick={handleAdd} disabled={saving} style={{ flex: 1 }}>
                {saving ? 'Сохраняю...' : 'Добавить'}
              </Button>
            </div>
          </div>
        </Modal>
      )}

      {/* ══ Edit Modal ══ */}
      {editModal && selected && (
        <Modal open={true} title={`Редактировать: ${selected.title}`} onClose={() => setEditModal(false)}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div style={{ fontSize: 12, color: 'var(--text-3)', fontFamily: 'var(--font-mono)', padding: '10px 14px', background: 'rgba(255,255,255,0.03)', borderRadius: 8 }}>
              api_id: {selected.api_id} · hash: {selected.api_hash.slice(0, 16)}...
            </div>
            <Input
              label="Название" autoFocus
              value={selected.title}
              onChange={e => setSelected(s => ({ ...s, title: e.target.value }))}
            />
            <Input
              label="Макс. аккаунтов" type="number"
              value={selected.max_accounts}
              onChange={e => setSelected(s => ({ ...s, max_accounts: e.target.value }))}
            />
            <Input
              label="Заметки"
              value={selected.notes || ''}
              onChange={e => setSelected(s => ({ ...s, notes: e.target.value }))}
            />
            <div style={{ display: 'flex', gap: 10, marginTop: 8 }}>
              <Button variant="ghost" onClick={() => setEditModal(false)} style={{ flex: 1 }}>Отмена</Button>
              <Button variant="primary" onClick={handleSaveEdit} disabled={saving} style={{ flex: 1 }}>
                {saving ? 'Сохраняю...' : 'Сохранить'}
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}