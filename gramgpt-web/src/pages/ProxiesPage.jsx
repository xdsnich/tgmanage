import { useEffect, useState } from 'react'
import { proxiesAPI } from '../services/api'
import { Button, Modal, Input, Empty, Spinner, Badge } from '../components/ui'
import { useAutoRefresh } from '../hooks/useAutoRefresh'

/**
 * Парсит одну строку прокси в {host, port, login, password, protocol}.
 * Поддерживает форматы:
 *   - host:port
 *   - host:port:user:pass
 *   - host:port:user:pass:protocol
 *   - protocol://user:pass@host:port
 *   - protocol://host:port
 * Возвращает null если строка не похожа на прокси (не вмешиваемся в форму).
 */
function parseProxyLine(raw) {
  const s = (raw || '').trim()
  if (!s) return null

  // URL-формат: socks5://user:pass@host:port  или  http://host:port
  const urlMatch = s.match(/^(socks5|socks4|http|https):\/\/(?:([^:@\s]+):([^@\s]+)@)?([^:\s]+):(\d+)\/?$/i)
  if (urlMatch) {
    const [, proto, login, password, host, port] = urlMatch
    return {
      host, port,
      login: login || '',
      password: password || '',
      protocol: proto.toLowerCase() === 'socks4' ? 'socks5' : proto.toLowerCase(),
    }
  }

  // Colon-формат: host:port[:user[:pass[:protocol]]]
  const parts = s.split(':').map(p => p.trim()).filter(Boolean)
  if (parts.length < 2) return null
  if (!/^\d+$/.test(parts[1])) return null   // вторая часть должна быть портом

  const [host, port, login = '', password = '', maybeProto = ''] = parts
  const proto = ['socks5', 'http', 'https', 'socks4'].includes(maybeProto.toLowerCase())
    ? (maybeProto.toLowerCase() === 'socks4' ? 'socks5' : maybeProto.toLowerCase())
    : null

  return {
    host, port,
    login, password,
    ...(proto ? { protocol: proto } : {}),
  }
}


function expiryInfo(expires_at) {
  if (!expires_at) return { label: '∞', color: 'var(--text-3)', warn: false, expired: false }
  const now = new Date()
  const end = new Date(expires_at)
  const diff = end - now
  if (diff <= 0) return { label: 'Истёк', color: 'var(--red)', warn: true, expired: true }
  const days = Math.ceil(diff / 86400000)
  if (days <= 3) return { label: `${days}д`, color: 'var(--yellow)', warn: true, expired: false }
  return { label: `${days}д`, color: 'var(--green)', warn: false, expired: false }
}

export default function ProxiesPage() {
  const [proxies, setProxies] = useState([])
  const [loading, setLoading] = useState(true)
  const [addModal, setAddModal] = useState(false)
  const [bulkModal, setBulkModal] = useState(false)
  const [form, setForm] = useState({ host: '', port: '', login: '', password: '', protocol: 'socks5', duration_days: 0 })
  const [bulkText, setBulkText] = useState('')
  const [bulkDays, setBulkDays] = useState(0)
  const [saving, setSaving] = useState(false)
  const [checking, setChecking] = useState(false)
  const [checkingId, setCheckingId] = useState(null)
  const [toast, setToast] = useState(null)
  const [editModal, setEditModal] = useState(false)
  const [editProxy, setEditProxy] = useState(null)
  const [editForm, setEditForm] = useState({ host: '', port: '', login: '', password: '', protocol: 'socks5', duration_days: 0 })

  const showToast = (text, type = 'success') => { setToast({ text, type }); setTimeout(() => setToast(null), 3500) }

  const load = async (silent = false) => {
    if (!silent) setLoading(true)
    try { const { data } = await proxiesAPI.list(); setProxies(data) } catch { }
    if (!silent) setLoading(false)
  }
  useEffect(() => { load() }, [])
  useAutoRefresh(() => load(true), 15000)

  const handleAdd = async (e) => {
    e.preventDefault(); setSaving(true)
    try {
      await proxiesAPI.create({ ...form, port: parseInt(form.port), duration_days: form.duration_days })
      setAddModal(false)
      setForm({ host: '', port: '', login: '', password: '', protocol: 'socks5', duration_days: 0 })
      showToast('Прокси добавлен'); await load()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setSaving(false)
  }

  const handleBulk = async (e) => {
    e.preventDefault(); setSaving(true)
    try {
      const { data } = await proxiesAPI.bulkCreate(bulkText, bulkDays)
      showToast(`Добавлено: ${data.added}. Ошибок: ${data.errors?.length || 0}`)
      setBulkModal(false); setBulkText(''); setBulkDays(0); await load()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setSaving(false)
  }

  const handleDelete = async (id) => {
    if (!window.confirm('Удалить прокси?')) return
    try { await proxiesAPI.delete(id); showToast('Удалено'); await load() } catch { }
  }

  const handleCheckAll = async () => {
    setChecking(true)
    try {
      const { data } = await proxiesAPI.checkAll()
      showToast(`Проверено: ${data.total}. Валидных: ${data.valid}, нерабочих: ${data.invalid}`)
      await load()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка проверки', 'error') }
    setChecking(false)
  }

  const handleCheckOne = async (id) => {
    setCheckingId(id)
    try {
      const { data } = await proxiesAPI.check(id)
      showToast(data.message)
      await load()
    } catch { showToast('Ошибка', 'error') }
    setCheckingId(null)
  }

  const handleAutoAssign = async () => {
    try { const { data } = await proxiesAPI.autoAssign(); showToast(data.message) }
    catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
  }

  const handleEdit = (p) => {
    setEditProxy(p)
    setEditForm({ host: p.host, port: String(p.port), login: p.login || '', password: '', protocol: p.protocol, duration_days: 0 })
    setEditModal(true)
  }

  const handleSaveEdit = async (e) => {
    e.preventDefault(); setSaving(true)
    try {
      const payload = {
        host: editForm.host, port: parseInt(editForm.port),
        login: editForm.login, protocol: editForm.protocol,
        duration_days: editForm.duration_days,
        duration_hours: 0,
      }
      if (editForm.password) payload.password = editForm.password
      await proxiesAPI.update(editProxy.id, payload)
      setEditModal(false); showToast('Прокси обновлён'); await load()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setSaving(false)
  }

  const valid = proxies.filter(p => p.is_valid === true).length
  const invalid = proxies.filter(p => p.is_valid === false).length
  const unchecked = proxies.filter(p => p.is_valid === null).length
  const expired = proxies.filter(p => p.expires_at && new Date(p.expires_at) < new Date()).length

  const DAY_PRESETS = [
    { d: 0, label: '∞' },
    { d: 7, label: '7д' },
    { d: 14, label: '14д' },
    { d: 30, label: '30д' },
    { d: 60, label: '60д' },
    { d: 90, label: '90д' },
  ]

  return (
    <div style={{ padding: '28px 32px', animation: 'fadeUp 0.4s cubic-bezier(0.16,1,0.3,1)' }}>
      {toast && <div style={{ position: 'fixed', top: 24, right: 24, zIndex: 999, padding: '12px 20px', borderRadius: 12, fontSize: 13, fontWeight: 600, background: toast.type === 'error' ? 'var(--red-dim)' : 'var(--green-dim)', color: toast.type === 'error' ? 'var(--red)' : 'var(--green)', boxShadow: '0 8px 30px rgba(0,0,0,0.5)', animation: 'fadeUp 0.3s ease' }}>{toast.text}</div>}

      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 28 }}>
        <div>
          <div style={{ fontSize: 11, color: 'var(--teal)', fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>◎ ПРОКСИ</div>
          <h1 style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.04em' }}>Управление прокси</h1>
          <div style={{ display: 'flex', gap: 14, marginTop: 6, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 12, color: 'var(--green)' }}>✓ {valid} валидных</span>
            <span style={{ fontSize: 12, color: 'var(--red)' }}>✗ {invalid} нерабочих</span>
            <span style={{ fontSize: 12, color: 'var(--text-3)' }}>? {unchecked} не проверено</span>
            {expired > 0 && <span style={{ fontSize: 12, color: 'var(--yellow)' }}>⏰ {expired} истёкших</span>}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {proxies.length > 0 && (
            <>
              <Button variant="ghost" onClick={handleAutoAssign}>🔗 Авто-назначить</Button>
              <Button variant="ghost" onClick={handleCheckAll} loading={checking}>
                {checking ? '⏳ Проверяю...' : '🔍 Проверить все'}
              </Button>
            </>
          )}
          <Button variant="ghost" onClick={() => setBulkModal(true)}>📋 Загрузить список</Button>
          <Button variant="primary" onClick={() => setAddModal(true)}>+ Добавить</Button>
        </div>
      </div>

      {loading ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 64 }}><Spinner size={28} /></div>
      ) : proxies.length === 0 ? (
        <Empty icon="🔗" title="Нет прокси" subtitle="Добавь прокси для назначения на аккаунты"
          action={<Button variant="primary" onClick={() => setBulkModal(true)}>📋 Загрузить список</Button>} />
      ) : (
        <div style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', overflow: 'hidden' }}>
          <div style={{ display: 'grid', gridTemplateColumns: '2fr 80px 1fr 130px 70px 90px 90px 160px', padding: '10px 20px', borderBottom: '1px solid var(--border)', fontSize: 10, color: 'var(--text-3)', letterSpacing: '0.1em', fontWeight: 700, textTransform: 'uppercase' }}>
            <span>Адрес</span><span>Протокол</span><span>Логин</span><span>Страна</span><span title="Аккаунтов на этом прокси">Акк-в</span><span>Срок</span><span>Статус</span><span style={{ textAlign: 'right' }}>Действия</span>
          </div>
          {proxies.map((p, i) => {
            const loc = p.city && p.country ? `${p.city}, ${p.country}` : p.country || '—'
            const exp = expiryInfo(p.expires_at)
            return (
              <div key={p.id} style={{
                display: 'grid', gridTemplateColumns: '2fr 80px 1fr 130px 70px 90px 90px 160px',
                padding: '13px 20px', alignItems: 'center',
                borderBottom: i < proxies.length - 1 ? '1px solid var(--border)' : 'none',
                transition: 'background 0.1s',
                background: exp.expired ? 'rgba(248,81,73,0.03)' : 'transparent',
              }}
                onMouseEnter={e => e.currentTarget.style.background = exp.expired ? 'rgba(248,81,73,0.06)' : 'rgba(255,255,255,0.02)'}
                onMouseLeave={e => e.currentTarget.style.background = exp.expired ? 'rgba(248,81,73,0.03)' : 'transparent'}>

                <div style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }}>{p.host}:{p.port}</div>
                <Badge color={p.protocol === 'socks5' ? 'violet' : 'blue'}>{p.protocol.toUpperCase()}</Badge>
                <div style={{ fontSize: 12, color: 'var(--text-2)' }}>{p.login || '—'}</div>

                {/* Страна из БД */}
                <div style={{ fontSize: 12, color: 'var(--text-2)', display: 'flex', alignItems: 'center', gap: 4 }}>
                  {p.country_code && (
                    <span style={{ fontSize: 10, padding: '1px 5px', borderRadius: 4, background: 'rgba(255,255,255,0.06)', fontFamily: 'var(--font-mono)', color: 'var(--text-3)' }}>
                      {p.country_code}
                    </span>
                  )}
                  {loc !== '—' ? loc : <span style={{ color: 'var(--text-3)' }}>—</span>}
                </div>

                {/* Аккаунтов на прокси */}
                <div style={{ fontSize: 13, fontWeight: 700, color: (p.accounts_count || 0) > 0 ? 'var(--violet)' : 'var(--text-3)', textAlign: 'center' }}>
                  {p.accounts_count || 0}
                </div>

                {/* Срок действия */}
                <div style={{ fontSize: 12, fontWeight: 600, color: exp.color, display: 'flex', alignItems: 'center', gap: 4 }}>
                  {exp.expired && <span>⏰</span>}
                  {exp.warn && !exp.expired && <span>⚠️</span>}
                  {exp.label}
                  {p.expires_at && !exp.expired && (
                    <span style={{ fontSize: 10, color: 'var(--text-3)', fontWeight: 400 }}>
                      до {new Date(p.expires_at).toLocaleDateString('ru', { day: 'numeric', month: 'short' })}
                    </span>
                  )}
                </div>

                {/* Статус */}
                <div>
                  {p.is_valid === true && <Badge color="green">✓ OK</Badge>}
                  {p.is_valid === false && <Badge color="red">✗ Нет</Badge>}
                  {p.is_valid === null && <Badge color="default">?</Badge>}
                </div>

                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 6 }}>
                  <button onClick={() => handleEdit(p)} style={{ padding: '5px 10px', borderRadius: 7, border: '1px solid transparent', background: 'transparent', color: 'var(--text-3)', fontSize: 11, cursor: 'pointer', transition: 'all 0.15s' }}
                    onMouseEnter={e => { e.currentTarget.style.background = 'rgba(59,130,246,0.1)'; e.currentTarget.style.color = 'var(--blue)' }}
                    onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--text-3)' }}>✏️</button>
                  <button onClick={() => handleCheckOne(p.id)} disabled={checkingId === p.id} style={{ padding: '5px 10px', borderRadius: 7, border: '1px solid transparent', background: 'transparent', color: 'var(--text-3)', fontSize: 11, cursor: 'pointer', transition: 'all 0.15s' }}
                    onMouseEnter={e => { e.currentTarget.style.background = 'rgba(124,77,255,0.1)'; e.currentTarget.style.color = 'var(--violet)' }}
                    onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--text-3)' }}>
                    {checkingId === p.id ? '⏳' : '🔍'}
                  </button>
                  <button onClick={() => handleDelete(p.id)} style={{ padding: '5px 10px', borderRadius: 7, border: '1px solid transparent', background: 'transparent', color: 'var(--text-3)', fontSize: 11, cursor: 'pointer', transition: 'all 0.15s' }}
                    onMouseEnter={e => { e.currentTarget.style.background = 'var(--red-dim)'; e.currentTarget.style.color = 'var(--red)'; e.currentTarget.style.borderColor = 'rgba(248,81,73,0.3)' }}
                    onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--text-3)'; e.currentTarget.style.borderColor = 'transparent' }}>🗑</button>
                </div>
              </div>
            )
          })}
        </div>
      )}

      {/* ── Add Modal ── */}
      <Modal open={addModal} onClose={() => setAddModal(false)} title="Добавить прокси">
        <form onSubmit={handleAdd} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {/* ── Быстрая вставка одной строкой ── */}
          <div>
            <label style={{ fontSize: 11, color: 'var(--violet)', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>
              🔥 Быстрая вставка (одной строкой)
            </label>
            <input
              type="text"
              placeholder="130.49.48.99:62744:d9VMTTsk:DzQSjAhD  или  socks5://user:pass@1.2.3.4:1080"
              onChange={e => {
                const parsed = parseProxyLine(e.target.value)
                if (parsed) setForm(prev => ({ ...prev, ...parsed }))
              }}
              style={{
                width: '100%', padding: '10px 14px',
                background: 'rgba(124,77,255,0.08)',
                border: '1px solid rgba(124,77,255,0.35)',
                borderRadius: 'var(--radius-sm)', color: 'var(--text)',
                fontSize: 13, outline: 'none',
                fontFamily: 'var(--font-mono)',
                boxSizing: 'border-box',
              }}
            />
            <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4 }}>
              Поддерживается <code>host:port:user:pass</code>, <code>host:port:user:pass:protocol</code>, <code>host:port</code>, <code>socks5://user:pass@host:port</code>. Поля ниже заполнятся автоматически.
            </div>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: 'var(--text-3)', fontSize: 10, letterSpacing: '0.1em' }}>
            <div style={{ flex: 1, height: 1, background: 'var(--border)' }} />
            ИЛИ ЗАПОЛНИТЕ ВРУЧНУЮ
            <div style={{ flex: 1, height: 1, background: 'var(--border)' }} />
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 100px', gap: 10 }}>
            <Input label="Host" value={form.host} onChange={e => setForm(d => ({ ...d, host: e.target.value }))} placeholder="1.2.3.4" required />
            <Input label="Port" value={form.port} onChange={e => setForm(d => ({ ...d, port: e.target.value }))} placeholder="1080" type="number" required />
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <Input label="Логин" value={form.login} onChange={e => setForm(d => ({ ...d, login: e.target.value }))} placeholder="user" />
            <Input label="Пароль" value={form.password} onChange={e => setForm(d => ({ ...d, password: e.target.value }))} type="password" placeholder="pass" />
          </div>
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Протокол</label>
            <select value={form.protocol} onChange={e => setForm(d => ({ ...d, protocol: e.target.value }))} style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)', color: 'var(--text)', fontSize: 14, outline: 'none' }}>
              <option value="socks5">SOCKS5</option>
              <option value="http">HTTP</option>
            </select>
          </div>
          <DayPicker label="Срок действия" value={form.duration_days} onChange={d => setForm(f => ({ ...f, duration_days: d }))} presets={DAY_PRESETS} />
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 4 }}>
            <Button variant="ghost" type="button" onClick={() => setAddModal(false)}>Отмена</Button>
            <Button variant="primary" type="submit" loading={saving}>Добавить</Button>
          </div>
        </form>
      </Modal>

      {/* ── Bulk Modal ── */}
      <Modal open={bulkModal} onClose={() => setBulkModal(false)} title="Загрузить список прокси" width={520}>
        <form onSubmit={handleBulk} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ padding: '12px 14px', background: 'rgba(124,77,255,0.08)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.8 }}>
            Поддерживаемые форматы:<br />
            <code style={{ color: 'var(--violet)' }}>socks5://login:pass@host:port</code><br />
            <code style={{ color: 'var(--violet)' }}>host:port:login:pass</code><br />
            <code style={{ color: 'var(--violet)' }}>host:port</code>
          </div>
          <textarea value={bulkText} onChange={e => setBulkText(e.target.value)} placeholder={"1.2.3.4:1080:user:pass\n5.6.7.8:1080\n..."} rows={8} style={{ background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', padding: '12px 14px', fontSize: 13, fontFamily: 'var(--font-mono)', resize: 'vertical', outline: 'none' }} />
          <DayPicker label="Срок действия для всех" value={bulkDays} onChange={setBulkDays} presets={DAY_PRESETS} />
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" type="button" onClick={() => setBulkModal(false)}>Отмена</Button>
            <Button variant="primary" type="submit" loading={saving}>Загрузить</Button>
          </div>
        </form>
      </Modal>

      {/* ── Edit Modal ── */}
      <Modal open={editModal} onClose={() => setEditModal(false)} title="Редактировать прокси" width={480}>
        <form onSubmit={handleSaveEdit} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          
          {/* ── Быстрое изменение одной строкой ── */}
          <div>
            <label style={{ fontSize: 11, color: 'var(--violet)', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>
              ⚡ Быстрое изменение (одной строкой)
            </label>
            <input
              type="text"
              placeholder="136.234.222.154:64510:user:pass"
              onChange={e => {
                const parsed = parseProxyLine(e.target.value)
                // Обновляем editForm, если парсер успешно разобрал строку
                if (parsed) setEditForm(prev => ({ ...prev, ...parsed }))
              }}
              style={{
                width: '100%', padding: '10px 14px',
                background: 'rgba(124,77,255,0.08)',
                border: '1px solid rgba(124,77,255,0.35)',
                borderRadius: 'var(--radius-sm)', color: 'var(--text)',
                fontSize: 13, outline: 'none',
                fontFamily: 'var(--font-mono)',
                boxSizing: 'border-box',
              }}
            />
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: 'var(--text-3)', fontSize: 10, letterSpacing: '0.1em' }}>
            <div style={{ flex: 1, height: 1, background: 'var(--border)' }} />
            ИЛИ ИЗМЕНИТЕ ВРУЧНУЮ
            <div style={{ flex: 1, height: 1, background: 'var(--border)' }} />
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 100px', gap: 10 }}>
            <Input label="Host" value={editForm.host} onChange={e => setEditForm(d => ({ ...d, host: e.target.value }))} placeholder="1.2.3.4" required />
            <Input label="Port" value={editForm.port} onChange={e => setEditForm(d => ({ ...d, port: e.target.value }))} placeholder="1080" type="number" required />
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <Input label="Логин" value={editForm.login} onChange={e => setEditForm(d => ({ ...d, login: e.target.value }))} placeholder="user" />
            <Input label="Пароль (пусто = не менять)" value={editForm.password} onChange={e => setEditForm(d => ({ ...d, password: e.target.value }))} type="password" placeholder="оставь пустым" />
          </div>
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Протокол</label>
            <select value={editForm.protocol} onChange={e => setEditForm(d => ({ ...d, protocol: e.target.value }))} style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)', color: 'var(--text)', fontSize: 14, outline: 'none' }}>
              <option value="socks5">SOCKS5</option>
              <option value="http">HTTP</option>
            </select>
          </div>
          
          <DayPicker
            label={editProxy?.expires_at ? `Продлить срок (сейчас: до ${new Date(editProxy.expires_at).toLocaleDateString('ru')})` : 'Установить срок'}
            value={editForm.duration_days}
            onChange={d => setEditForm(f => ({ ...f, duration_days: d }))}
            presets={DAY_PRESETS}
          />
          
          {editForm.duration_days === 0 && editProxy?.expires_at && (
            <div style={{ fontSize: 11, color: 'var(--text-3)', padding: '6px 10px', background: 'rgba(255,180,0,0.06)', borderRadius: 8, borderLeft: '3px solid rgba(255,180,0,0.3)' }}>
              ⚠️ Если оставить ∞ — существующий срок ({new Date(editProxy.expires_at).toLocaleDateString('ru')}) сохранится
            </div>
          )}
          
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 4 }}>
            <Button variant="ghost" type="button" onClick={() => setEditModal(false)}>Отмена</Button>
            <Button variant="primary" type="submit" loading={saving}>Сохранить</Button>
          </div>
        </form>
      </Modal>
    </div>
  )
}

function DayPicker({ label, value, onChange, presets }) {
  return (
    <div>
      <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>{label}</label>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
        {presets.map(p => (
          <button key={p.d} type="button" onClick={() => onChange(p.d)} style={{
            padding: '6px 14px', borderRadius: 8, fontSize: 12, fontWeight: 600, cursor: 'pointer', transition: 'all 0.15s',
            background: value === p.d ? 'rgba(0,194,178,0.15)' : 'var(--bg-3)',
            border: `1px solid ${value === p.d ? 'rgba(0,194,178,0.4)' : 'var(--border)'}`,
            color: value === p.d ? '#00c2b2' : 'var(--text-2)',
          }}>{p.label}</button>
        ))}
        {/* Поле для ввода своего значения */}
        <input
          type="number"
          placeholder="Свой срок..."
          value={value === 0 ? '' : value}
          onChange={(e) => {
            const val = parseInt(e.target.value, 10);
            onChange(isNaN(val) || val < 0 ? 0 : val);
          }}
          style={{
            width: '110px', padding: '6px 10px', background: 'var(--bg-3)',
            border: '1px solid var(--border)', borderRadius: 8,
            color: 'var(--text)', fontSize: 12, outline: 'none'
          }}
        />
      </div>
      {value > 0 && (
        <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 6 }}>
          Прокси будет действителен до {new Date(Date.now() + value * 86400000).toLocaleDateString('ru', { day: 'numeric', month: 'long', year: 'numeric' })}
        </div>
      )}
    </div>
  )
}
