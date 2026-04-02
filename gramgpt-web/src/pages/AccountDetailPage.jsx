import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { accountsAPI, securityAPI, channelsAPI, actionsAPI, tgAuthAPI, proxiesAPI } from '../services/api'
import { Card, Button, Input, Modal, TrustBar, StatusBadge, Badge, Spinner, Empty } from '../components/ui'

const ROLES = ['default', 'продавец', 'прогреватель', 'читатель', 'консультант']

const TRUST_EVENTS = [
  { event: 'Системный мут получен', delta: -3 },
  { event: 'Чистый день без нарушений', delta: +1 },
  { event: 'Полное заполнение профиля', delta: +2 },
  { event: '2FA активирована', delta: +1 },
  { event: 'Спамблок обнаружен', delta: -5 },
  { event: 'Успешная переавторизация', delta: +1 },
]

export default function AccountDetailPage() {
  const { id } = useParams()
  const navigate = useNavigate()

  const [account, setAccount] = useState(null)
  const [loading, setLoading] = useState(true)
  const [sessions, setSessions] = useState([])
  const [sessionsLoading, setSessionsLoading] = useState(false)
  const [channels, setChannels] = useState([])
  const [channelsLoading, setChannelsLoading] = useState(false)

  // Modals
  const [editModal, setEditModal] = useState(false)
  const [twoFAModal, setTwoFAModal] = useState(false)
  const [authModal, setAuthModal] = useState(false)
  const [channelModal, setChannelModal] = useState(false)
  const [proxyModal, setProxyModal] = useState(false)
  const [exportModal, setExportModal] = useState(false)

  // Forms
  const [editData, setEditData] = useState({})
  const [proxies, setProxies] = useState([])
  const [selectedProxyId, setSelectedProxyId] = useState(null)
  const [twoFAPass, setTwoFAPass] = useState('')
  const [twoFAHint, setTwoFAHint] = useState('')
  const [authPhone, setAuthPhone] = useState('')
  const [authCode, setAuthCode] = useState('')
  const [authProxyId, setAuthProxyId] = useState(null)
  const [authStep, setAuthStep] = useState('idle') // idle | code_sent | needs_2fa
  const [authMsg, setAuthMsg] = useState('')
  const [channelTitle, setChannelTitle] = useState('')
  const [channelDesc, setChannelDesc] = useState('')
  const [channelUsername, setChannelUsername] = useState('')
  const [exportData, setExportData] = useState(null)

  const [saving, setSaving] = useState(false)
  const [actionLoading, setActionLoading] = useState(null)
  const [toast, setToast] = useState(null)

  const showToast = (text, type = 'success') => {
    setToast({ text, type })
    setTimeout(() => setToast(null), 3500)
  }

  const load = async () => {
    setLoading(true)
    try {
      const { data } = await accountsAPI.get(id)
      setAccount(data)
      setEditData({ first_name: data.first_name, last_name: data.last_name, bio: data.bio, role: data.role, notes: data.notes || '', tags: data.tags || [] })
      setAuthPhone(data.phone)
    } catch { navigate('/accounts') }
    setLoading(false)
  }

  const loadSessions = async () => {
    setSessionsLoading(true)
    try { const { data } = await securityAPI.listSessions(id); setSessions(data.sessions || []) }
    catch { setSessions([]) }
    setSessionsLoading(false)
  }

  const loadChannels = async () => {
    setChannelsLoading(true)
    try { const { data } = await channelsAPI.list(id); setChannels(data.channels || []) }
    catch { setChannels([]) }
    setChannelsLoading(false)
  }

  useEffect(() => { load() }, [id])

  // ── Handlers ──────────────────────────────────────────────

  const handleSaveProfile = async (e) => {
    e.preventDefault(); setSaving(true)
    try {
      await accountsAPI.update(id, editData)
      setEditModal(false); showToast('Профиль обновлён'); await load()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setSaving(false)
  }

  const handleSet2FA = async (e) => {
    e.preventDefault(); setSaving(true)
    try {
      await securityAPI.set2FA(id, twoFAPass, twoFAHint)
      setTwoFAModal(false); setTwoFAPass(''); setTwoFAHint('')
      showToast('2FA установлена'); await load()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setSaving(false)
  }

  const handleRemove2FA = async () => {
    if (!window.confirm('Снять двухфакторную аутентификацию?')) return
    setSaving(true)
    try {
      await securityAPI.remove2FA(id)
      showToast('2FA снята'); await load()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setSaving(false)
  }

  const handleTerminateSessions = async () => {
    if (!window.confirm('Завершить все сторонние сессии?')) return
    setSaving(true)
    try {
      await securityAPI.terminateSessions(id)
      showToast('Сессии завершены'); await loadSessions()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setSaving(false)
  }

  const handleExportSession = async () => {
    try {
      const { data } = await securityAPI.exportSession(id)
      setExportData(data); setExportModal(true)
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
  }

  const handleSendCode = async () => {
    setSaving(true); setAuthMsg('')
    try {
      // Загружаем прокси для выбора если ещё не загрузили
      if (proxies.length === 0) {
        try { const { data } = await proxiesAPI.list(); setProxies(data) } catch { }
      }
      const { data } = await tgAuthAPI.sendCode(authPhone, authProxyId || null)
      setAuthStep('code_sent')
      setAuthMsg(data.message)
    } catch (err) { setAuthMsg(err.response?.data?.detail || 'Ошибка') }
    setSaving(false)
  }

  const handleConfirmCode = async () => {
    setSaving(true); setAuthMsg('')
    try {
      await tgAuthAPI.confirm(authPhone, authCode)
      setAuthModal(false); setAuthStep('idle'); setAuthCode('')
      showToast('Авторизация успешна'); await load()
    } catch (err) {
      const detail = err.response?.data?.detail || ''
      if (detail.includes('2FA')) { setAuthStep('needs_2fa'); setAuthMsg('Требуется пароль 2FA') }
      else { setAuthMsg(detail || 'Ошибка подтверждения') }
    }
    setSaving(false)
  }

  const handleConfirm2FA = async () => {
    setSaving(true); setAuthMsg('')
    try {
      await tgAuthAPI.confirm2FA(authPhone, authCode)
      setAuthModal(false); setAuthStep('idle'); setAuthCode('')
      showToast('Авторизация успешна'); await load()
    } catch (err) { setAuthMsg(err.response?.data?.detail || 'Ошибка') }
    setSaving(false)
  }

  const handleCreateChannel = async (e) => {
    e.preventDefault(); setSaving(true)
    try {
      await channelsAPI.create(parseInt(id), channelTitle, channelDesc, channelUsername)
      setChannelModal(false); setChannelTitle(''); setChannelDesc(''); setChannelUsername('')
      showToast('Канал создан'); await loadChannels()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setSaving(false)
  }

  const quickAction = async (actionFn, label) => {
    setActionLoading(label)
    try {
      await actionFn([parseInt(id)])
      showToast(`${label} — выполнено`)
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setActionLoading(null)
  }

  // ── Render ────────────────────────────────────────────────

  if (loading) return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
      <Spinner size={32} />
    </div>
  )

  if (!account) return <Empty icon="❌" title="Аккаунт не найден" />

  const a = account
  const trustColor = a.trust_score >= 70 ? 'var(--green)' : a.trust_score >= 40 ? 'var(--yellow)' : 'var(--red)'
  const trustLabel = a.trust_score >= 80 ? 'Отлично' : a.trust_score >= 60 ? 'Хорошо' : a.trust_score >= 40 ? 'Средне' : 'Слабо'

  // Trust Score recommendations
  const recommendations = []
  if (!a.username) recommendations.push('Установить username')
  if (!a.bio) recommendations.push('Заполнить Bio')
  if (!a.has_photo) recommendations.push('Загрузить аватарку')
  if (!a.has_2fa) recommendations.push('Включить 2FA')
  if (!a.proxy_id) recommendations.push('Назначить прокси')

  return (
    <div style={{ padding: '28px 32px', animation: 'fadeUp 0.4s cubic-bezier(0.16,1,0.3,1)', maxWidth: 1100 }}>
      {/* Toast */}
      {toast && (
        <div style={{
          position: 'fixed', top: 24, right: 24, zIndex: 999,
          padding: '12px 20px', borderRadius: 12, fontSize: 13, fontWeight: 600,
          background: toast.type === 'error' ? 'var(--red-dim)' : 'var(--green-dim)',
          color: toast.type === 'error' ? 'var(--red)' : 'var(--green)',
          border: `1px solid ${toast.type === 'error' ? 'rgba(248,81,73,0.3)' : 'rgba(61,214,140,0.3)'}`,
          boxShadow: '0 8px 30px rgba(0,0,0,0.5)', animation: 'fadeUp 0.3s ease',
        }}>{toast.text}</div>
      )}

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 24 }}>
        <button onClick={() => navigate('/accounts')} style={{
          background: 'rgba(255,255,255,0.06)', border: '1px solid var(--border)',
          color: 'var(--text-2)', borderRadius: 8, padding: '6px 12px', cursor: 'pointer',
          fontSize: 12, fontWeight: 600, transition: 'all 0.15s',
        }}>← Назад</button>
        <div style={{ fontSize: 11, color: 'var(--text-3)' }}>Аккаунты / Детали</div>
      </div>

      {/* Profile header card */}
      <Card style={{ marginBottom: 16, padding: '24px 28px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 20 }}>
          {/* Avatar */}
          <div style={{
            width: 64, height: 64, borderRadius: 16, flexShrink: 0,
            background: 'linear-gradient(135deg, rgba(124,77,255,0.3), rgba(61,139,255,0.2))',
            border: '2px solid rgba(124,77,255,0.3)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: 24, fontWeight: 800, color: 'var(--violet)',
          }}>
            {a.first_name?.[0]?.toUpperCase() || '?'}
          </div>

          {/* Info */}
          <div style={{ flex: 1 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
              <span style={{ fontSize: 20, fontWeight: 800, letterSpacing: '-0.03em' }}>
                {a.first_name || ''} {a.last_name || ''}
              </span>
              <StatusBadge status={a.status} />
              {a.role !== 'default' && <Badge color="violet">{a.role}</Badge>}
            </div>
            <div style={{ display: 'flex', gap: 16, fontSize: 13, color: 'var(--text-2)' }}>
              <span style={{ fontFamily: 'var(--font-mono)' }}>{a.phone}</span>
              {a.username && <span>@{a.username}</span>}
              {a.bio && <span style={{ color: 'var(--text-3)' }}>«{a.bio}»</span>}
            </div>
            <div style={{ display: 'flex', gap: 10, marginTop: 8 }}>
              {(a.tags || []).map(t => <Badge key={t} color="blue">{t}</Badge>)}
            </div>
          </div>

          {/* Actions */}
          <div style={{ display: 'flex', gap: 8 }}>
            <Button variant="outline" size="sm" onClick={() => setEditModal(true)}>Редактировать</Button>
            <Button variant="ghost" size="sm" onClick={async () => { setAuthStep('idle'); setAuthMsg(''); try { const { data } = await proxiesAPI.list(); setProxies(data) } catch { }; setAuthModal(true) }}>Авторизовать</Button>
          </div>
        </div>
      </Card>

      {/* Two-column layout */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>

        {/* ── LEFT COLUMN ──────────────────────────────────── */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>

          {/* Trust Score */}
          <Card>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
              <span style={{ fontWeight: 700, fontSize: 14, letterSpacing: '-0.02em' }}>Trust Score</span>
              <span style={{ fontSize: 28, fontWeight: 800, color: trustColor, fontFamily: 'var(--font-mono)' }}>{a.trust_score}</span>
            </div>
            <TrustBar score={a.trust_score} />
            <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 10 }}>Оценка: {trustLabel}</div>

            {/* Recommendations */}
            {recommendations.length > 0 && (
              <div style={{ marginTop: 16, padding: '12px 14px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 10 }}>
                <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--violet)', marginBottom: 8, letterSpacing: '0.06em', textTransform: 'uppercase' }}>Рекомендации</div>
                {recommendations.map(r => (
                  <div key={r} style={{ fontSize: 12, color: 'var(--text-2)', padding: '3px 0' }}>→ {r}</div>
                ))}
              </div>
            )}

            {/* Trust events legend */}
            <div style={{ marginTop: 14, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
              <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-3)', marginBottom: 8, letterSpacing: '0.06em', textTransform: 'uppercase' }}>Как формируется</div>
              {TRUST_EVENTS.map(({ event, delta }) => (
                <div key={event} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: 'var(--text-3)', padding: '2px 0' }}>
                  <span>{event}</span>
                  <span style={{ fontFamily: 'var(--font-mono)', color: delta > 0 ? 'var(--green)' : 'var(--red)' }}>
                    {delta > 0 ? '+' : ''}{delta}
                  </span>
                </div>
              ))}
            </div>
          </Card>

          {/* Info card */}
          <Card>
            <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 14, letterSpacing: '-0.02em' }}>Информация</div>
            {[
              ['Telegram ID', a.tg_id || '—'],
              ['Фото', a.has_photo ? '✅ Есть' : '❌ Нет'],
              ['2FA', a.has_2fa ? '✅ Включена' : '❌ Отключена'],
              ['Активных сессий', a.active_sessions],
              ['Прокси', a.proxy_id ? `🟢 #${a.proxy_id}` : '❌ Не назначен'],
              ['Добавлен', a.added_at ? new Date(a.added_at).toLocaleDateString('ru') : '—'],
              ['Последняя проверка', a.last_checked ? new Date(a.last_checked).toLocaleString('ru') : '— не проверялся'],
            ].map(([label, val]) => (
              <div key={label} style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px solid var(--border)', fontSize: 13 }}>
                <span style={{ color: 'var(--text-3)' }}>{label}</span>
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }}>{val}</span>
              </div>
            ))}
            {a.notes && (
              <div style={{ marginTop: 12, padding: '10px 12px', background: 'var(--bg-3)', borderRadius: 8, fontSize: 12, color: 'var(--text-2)' }}>
                📝 {a.notes}
              </div>
            )}
            {a.error && (
              <div style={{ marginTop: 8, padding: '10px 12px', background: 'var(--red-dim)', border: '1px solid rgba(248,81,73,0.2)', borderRadius: 8, fontSize: 12, color: 'var(--red)' }}>
                ⚠ {a.error}
              </div>
            )}
          </Card>
        </div>

        {/* ── RIGHT COLUMN ─────────────────────────────────── */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>

          {/* Sessions */}
          <Card>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
              <span style={{ fontWeight: 700, fontSize: 14, letterSpacing: '-0.02em' }}>Активные сессии</span>
              <div style={{ display: 'flex', gap: 6 }}>
                <Button variant="ghost" size="sm" onClick={loadSessions} loading={sessionsLoading}>Загрузить</Button>
                <Button variant="danger" size="sm" onClick={handleTerminateSessions} loading={saving}>Завершить все</Button>
              </div>
            </div>
            {sessions.length === 0 ? (
              <div style={{ fontSize: 12, color: 'var(--text-3)', padding: '16px 0', textAlign: 'center' }}>
                Нажмите «Загрузить» для получения списка сессий
              </div>
            ) : sessions.map((s, i) => (
              <div key={i} style={{
                padding: '10px 12px', borderRadius: 8, background: 'var(--bg-3)',
                marginBottom: 6, fontSize: 12,
              }}>
                <div style={{ fontWeight: 600, color: 'var(--text)' }}>{s.device_model || s.app_name || 'Устройство'}</div>
                <div style={{ color: 'var(--text-3)', marginTop: 2 }}>
                  {s.platform || ''} · {s.ip || ''} · {s.country || ''}
                </div>
              </div>
            ))}
          </Card>

          {/* Security actions */}
          <Card>
            <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 14, letterSpacing: '-0.02em' }}>Безопасность</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {a.has_2fa ? (
                <Button variant="danger" size="sm" onClick={handleRemove2FA} loading={saving} style={{ width: '100%' }}>
                  🔓 Снять 2FA
                </Button>
              ) : (
                <Button variant="outline" size="sm" onClick={() => setTwoFAModal(true)} style={{ width: '100%' }}>
                  🔐 Установить 2FA
                </Button>
              )}
              <Button variant="ghost" size="sm" onClick={handleExportSession} style={{ width: '100%' }}>
                📦 Экспорт сессии (JSON)
              </Button>
            </div>
          </Card>

          {/* Channels */}
          <Card>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
              <span style={{ fontWeight: 700, fontSize: 14, letterSpacing: '-0.02em' }}>Каналы</span>
              <div style={{ display: 'flex', gap: 6 }}>
                <Button variant="ghost" size="sm" onClick={loadChannels} loading={channelsLoading}>Загрузить</Button>
                <Button variant="outline" size="sm" onClick={() => setChannelModal(true)}>+ Создать</Button>
              </div>
            </div>
            {channels.length === 0 ? (
              <div style={{ fontSize: 12, color: 'var(--text-3)', padding: '16px 0', textAlign: 'center' }}>
                Нажмите «Загрузить» для получения списка каналов
              </div>
            ) : channels.map((ch, i) => (
              <div key={i} style={{
                padding: '10px 12px', borderRadius: 8, background: 'var(--bg-3)',
                marginBottom: 6, display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              }}>
                <div>
                  <div style={{ fontWeight: 600, fontSize: 13, color: 'var(--text)' }}>{ch.title}</div>
                  <div style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'var(--font-mono)' }}>{ch.link || ch.username || ''}</div>
                </div>
                {ch.members !== undefined && (
                  <Badge color="blue">{ch.members} подп.</Badge>
                )}
              </div>
            ))}
          </Card>

          {/* Quick Actions */}
          <Card>
            <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 14, letterSpacing: '-0.02em' }}>Быстрые действия</div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              {[
                { label: 'Выход из чатов', fn: actionsAPI.leaveChats, icon: '💬' },
                { label: 'Отписка от каналов', fn: actionsAPI.leaveChannels, icon: '📢' },
                { label: 'Удалить переписки', fn: actionsAPI.deleteDialogs, icon: '🗑' },
                { label: 'Прочитать всё', fn: actionsAPI.readAll, icon: '✉️' },
                { label: 'Очистить кэш', fn: actionsAPI.clearCache, icon: '🧹' },
                { label: 'Открепить папки', fn: actionsAPI.unpinFolders, icon: '📁' },
              ].map(({ label, fn, icon }) => (
                <Button key={label} variant="ghost" size="sm"
                  loading={actionLoading === label}
                  onClick={() => quickAction(fn, label)}
                  style={{ justifyContent: 'flex-start', fontSize: 12 }}>
                  {icon} {label}
                </Button>
              ))}
              <Button variant="ghost" size="sm" onClick={async () => {
                try { const { data } = await proxiesAPI.list(); setProxies(data); setSelectedProxyId(a.proxy_id); setProxyModal(true) }
                catch { showToast('Ошибка загрузки прокси', 'error') }
              }} style={{ justifyContent: 'flex-start', fontSize: 12 }}>
                🔒 {a.proxy_id ? 'Сменить прокси' : 'Назначить прокси'}
              </Button>
            </div>
          </Card>
        </div>
      </div>

      {/* ── MODALS ───────────────────────────────────────────── */}

      {/* Edit profile */}
      <Modal open={editModal} onClose={() => setEditModal(false)} title="Редактировать профиль" width={480}>
        <form onSubmit={handleSaveProfile} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <Input label="Имя" value={editData.first_name || ''} onChange={e => setEditData(d => ({ ...d, first_name: e.target.value }))} />
            <Input label="Фамилия" value={editData.last_name || ''} onChange={e => setEditData(d => ({ ...d, last_name: e.target.value }))} />
          </div>
          <Input label="Bio" value={editData.bio || ''} onChange={e => setEditData(d => ({ ...d, bio: e.target.value }))} placeholder="Описание профиля" />
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Роль</label>
            <select value={editData.role || 'default'} onChange={e => setEditData(d => ({ ...d, role: e.target.value }))} style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)', color: 'var(--text)', fontSize: 14, outline: 'none' }}>
              {ROLES.map(r => <option key={r} value={r}>{r === 'default' ? 'Без роли' : r}</option>)}
            </select>
          </div>
          <Input label="Заметки" value={editData.notes || ''} onChange={e => setEditData(d => ({ ...d, notes: e.target.value }))} placeholder="Ваши заметки" />
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 4 }}>
            <Button variant="ghost" type="button" onClick={() => setEditModal(false)}>Отмена</Button>
            <Button variant="primary" type="submit" loading={saving}>Сохранить</Button>
          </div>
        </form>
      </Modal>

      {/* Set 2FA */}
      <Modal open={twoFAModal} onClose={() => setTwoFAModal(false)} title="Установить 2FA" width={420}>
        <form onSubmit={handleSet2FA} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ padding: '10px 14px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)' }}>
            Двухфакторная аутентификация защитит аккаунт от несанкционированного доступа.
          </div>
          <Input label="Пароль 2FA" type="password" value={twoFAPass} onChange={e => setTwoFAPass(e.target.value)} required placeholder="Придумайте пароль" />
          <Input label="Подсказка (опционально)" value={twoFAHint} onChange={e => setTwoFAHint(e.target.value)} placeholder="Чтобы не забыть" />
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" type="button" onClick={() => setTwoFAModal(false)}>Отмена</Button>
            <Button variant="primary" type="submit" loading={saving}>Установить</Button>
          </div>
        </form>
      </Modal>

      {/* TG Auth */}
      <Modal open={authModal} onClose={() => { setAuthModal(false); setAuthStep('idle'); setAuthMsg('') }} title="Авторизация Telegram" width={440}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ padding: '10px 14px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>
            Авторизация происходит прямо на платформе. Код придёт в Telegram или по SMS.
          </div>
          <Input label="Номер телефона" value={authPhone} onChange={e => setAuthPhone(e.target.value)} placeholder="+380..." disabled={authStep !== 'idle'} />

          {authStep === 'idle' && (
            <>
              <div>
                <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Прокси (опционально)</label>
                <select value={authProxyId || ''} onChange={e => setAuthProxyId(e.target.value ? parseInt(e.target.value) : null)} style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 13, outline: 'none' }}>
                  <option value="">Без прокси (прямое подключение)</option>
                  {proxies.filter(p => p.is_valid !== false).map(p => (
                    <option key={p.id} value={p.id}>{p.host}:{p.port} [{p.protocol}] {p.is_valid ? '✓' : '?'}</option>
                  ))}
                </select>
                <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 4 }}>Авторизация и все действия аккаунта будут через этот прокси</div>
              </div>
              <Button variant="primary" onClick={handleSendCode} loading={saving}>Запросить код</Button>
            </>
          )}

          {authStep === 'code_sent' && (
            <>
              <Input label="Код авторизации" value={authCode} onChange={e => setAuthCode(e.target.value)} placeholder="12345" autoFocus />
              <Button variant="primary" onClick={handleConfirmCode} loading={saving}>Подтвердить</Button>
            </>
          )}

          {authStep === 'needs_2fa' && (
            <>
              <Input label="Пароль 2FA" type="password" value={authCode} onChange={e => setAuthCode(e.target.value)} placeholder="Введите пароль 2FA" autoFocus />
              <Button variant="primary" onClick={handleConfirm2FA} loading={saving}>Подтвердить 2FA</Button>
            </>
          )}

          {authMsg && (
            <div style={{
              padding: '10px 14px', borderRadius: 10, fontSize: 13,
              background: authMsg.includes('Ошибка') || authMsg.includes('Неверн') ? 'var(--red-dim)' : 'var(--green-dim)',
              color: authMsg.includes('Ошибка') || authMsg.includes('Неверн') ? 'var(--red)' : 'var(--green)',
              border: `1px solid ${authMsg.includes('Ошибка') || authMsg.includes('Неверн') ? 'rgba(248,81,73,0.2)' : 'rgba(61,214,140,0.2)'}`,
            }}>{authMsg}</div>
          )}
        </div>
      </Modal>

      {/* Create channel */}
      <Modal open={channelModal} onClose={() => setChannelModal(false)} title="Создать канал" width={440}>
        <form onSubmit={handleCreateChannel} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <Input label="Название канала" value={channelTitle} onChange={e => setChannelTitle(e.target.value)} required placeholder="Мой канал" />
          <Input label="Username (опционально)" value={channelUsername} onChange={e => setChannelUsername(e.target.value)} placeholder="my_channel (без @)" />
          <Input label="Описание (опционально)" value={channelDesc} onChange={e => setChannelDesc(e.target.value)} placeholder="О чём канал" />
          <div style={{ fontSize: 11, color: 'var(--text-3)', padding: '0 2px' }}>
            Если указать username — канал станет публичным (t.me/username). Без username — приватный.
          </div>
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" type="button" onClick={() => setChannelModal(false)}>Отмена</Button>
            <Button variant="primary" type="submit" loading={saving}>Создать</Button>
          </div>
        </form>
      </Modal>

      {/* Proxy */}
      <Modal open={proxyModal} onClose={() => setProxyModal(false)} title="Назначить прокси" width={480}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ padding: '10px 14px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>
            Все подключения аккаунта будут через выбранный прокси.
            {a.proxy_id && <><br />Текущий прокси: <strong>#{a.proxy_id}</strong></>}
          </div>

          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Прокси</label>
            <select value={selectedProxyId || ''} onChange={e => setSelectedProxyId(e.target.value ? parseInt(e.target.value) : null)} style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 14, outline: 'none' }}>
              <option value="">❌ Без прокси (прямое подключение)</option>
              {proxies.map(p => (
                <option key={p.id} value={p.id}>
                  {p.host}:{p.port} ({p.protocol}) {p.is_valid === true ? '✅' : p.is_valid === false ? '❌' : '❓'}
                </option>
              ))}
            </select>
          </div>

          {proxies.length === 0 && (
            <div style={{ fontSize: 12, color: 'var(--text-3)', textAlign: 'center', padding: '8px 0' }}>
              Нет прокси. Добавьте на странице Прокси.
            </div>
          )}

          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" onClick={() => setProxyModal(false)}>Отмена</Button>
            <Button variant="primary" loading={saving} onClick={async () => {
              setSaving(true)
              try {
                await accountsAPI.update(parseInt(id), { proxy_id: selectedProxyId })
                setProxyModal(false)
                showToast(selectedProxyId ? 'Прокси назначен' : 'Прокси убран')
                await load()
              } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
              setSaving(false)
            }}>Сохранить</Button>
          </div>
        </div>
      </Modal>

      {/* Export session */}
      <Modal open={exportModal} onClose={() => setExportModal(false)} title="Экспорт сессии (JSON)" width={520}>
        <pre style={{
          padding: '14px 16px', background: 'var(--bg-3)', borderRadius: 10,
          fontSize: 11, color: 'var(--violet)', fontFamily: 'var(--font-mono)',
          overflowX: 'auto', maxHeight: 300, lineHeight: 1.6, whiteSpace: 'pre-wrap',
        }}>{JSON.stringify(exportData, null, 2)}</pre>
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 14 }}>
          <Button variant="ghost" onClick={() => setExportModal(false)}>Закрыть</Button>
          <Button variant="primary" onClick={() => {
            navigator.clipboard.writeText(JSON.stringify(exportData, null, 2))
            showToast('Скопировано в буфер')
          }}>Копировать</Button>
        </div>
      </Modal>
    </div>
  )
}