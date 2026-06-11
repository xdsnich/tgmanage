import { useState, useEffect } from 'react'
import { proxiesAPI, apiAppsAPI } from '../services/api'
import { Button, Spinner, Badge } from '../components/ui'
import api from '../services/api'

const PLATFORM_ICONS = { android: '📱', ios: '🍎', desktop: '🖥', macos: '💻' }

/**
 * Импорт аккаунтов из Telegram Web localStorage.
 * Шаг 1: пользователь вставляет блоб из localStorage → парсим → показываем превью
 * Шаг 2: на каждый превью назначаем прокси и api_app, импортируем
 */
export default function WebSessionImport({ onSuccess, onClose }) {
  const [step, setStep] = useState('paste') // paste | preview | importing
  const [blob, setBlob] = useState('')
  const [parsing, setParsing] = useState(false)
  const [previews, setPreviews] = useState([])
  const [proxies, setProxies] = useState([])
  const [apiApps, setApiApps] = useState([])
  // Per-account assignment: { [label]: {proxyId, apiAppId, phone, status, message} }
  const [assignments, setAssignments] = useState({})
  const [bulkProxyId, setBulkProxyId] = useState(null)
  const [bulkApiAppId, setBulkApiAppId] = useState(null)
  const [importing, setImporting] = useState(false)

  useEffect(() => {
    Promise.all([
      proxiesAPI.list().catch(() => ({ data: [] })),
      apiAppsAPI.list().catch(() => ({ data: [] })),
    ]).then(([p, a]) => {
      setProxies(p.data || [])
      setApiApps((a.data || []).filter(x => x.is_active))
    })
  }, [])

  const handleParse = async () => {
    if (!blob.trim()) {
      alert('Вставь данные из localStorage')
      return
    }
    setParsing(true)
    try {
      const { data } = await api.post('/import/web-storage-parse', { storage_blob: blob })
      setPreviews(data.accounts || [])
      // Init assignments
      const init = {}
        ; (data.accounts || []).forEach(a => {
          init[a.label] = { proxyId: null, apiAppId: null, phone: '', status: 'pending', message: '' }
        })
      setAssignments(init)
      setStep('preview')
    } catch (err) {
      alert(err.response?.data?.detail || 'Ошибка парсинга')
    }
    setParsing(false)
  }

  const applyBulk = () => {
    if (!bulkProxyId && !bulkApiAppId) return
    setAssignments(prev => {
      const updated = { ...prev }
      Object.keys(updated).forEach(k => {
        if (bulkProxyId) updated[k] = { ...updated[k], proxyId: bulkProxyId }
        if (bulkApiAppId) updated[k] = { ...updated[k], apiAppId: bulkApiAppId }
      })
      return updated
    })
  }

  const updateAssignment = (label, patch) => {
    setAssignments(prev => ({ ...prev, [label]: { ...prev[label], ...patch } }))
  }

  const handleImportOne = async (preview) => {
    const a = assignments[preview.label]
    if (!a.proxyId) {
      updateAssignment(preview.label, { status: 'error', message: 'Выбери прокси' })
      return
    }
    updateAssignment(preview.label, { status: 'importing', message: 'Импорт...' })
    try {
      const { data } = await api.post('/import/web-session', {
        dc_id: preview.dc_id,
        auth_key: preview.auth_key,
        proxy_id: a.proxyId,
        api_app_id: a.apiAppId || null,
        phone: a.phone || null,
        user_id: preview.user_id || null,
      })
      updateAssignment(preview.label, {
        status: 'success',
        message: `${data.phone} ${data.first_name ? '· ' + data.first_name : ''}`,
      })
      if (onSuccess) onSuccess()
    } catch (err) {
      updateAssignment(preview.label, {
        status: 'error',
        message: err.response?.data?.detail || 'Ошибка',
      })
    }
  }

  const handleImportAll = async () => {
    setImporting(true)
    for (const p of previews) {
      const a = assignments[p.label]
      if (a.status === 'success') continue
      if (!a.proxyId) {
        updateAssignment(p.label, { status: 'error', message: 'Нет прокси' })
        continue
      }
      await handleImportOne(p)
    }
    setImporting(false)
  }

  // ═══════════════ PASTE STEP ═══════════════
  if (step === 'paste') {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div style={{
          padding: '12px 14px', background: 'rgba(61,214,140,0.08)',
          border: '1px solid rgba(61,214,140,0.25)', borderRadius: 10,
          fontSize: 12, color: 'var(--text-2)', lineHeight: 1.7,
        }}>
          <div style={{ fontWeight: 700, marginBottom: 6, fontSize: 13, color: 'var(--green)' }}>⚡ Самый надёжный способ (одной командой):</div>
          <div>1. Открой <code>web.telegram.org/k/</code> и авторизуйся в нужном аккаунте (можно несколько через меню профиля).</div>
          <div>2. Нажми <code>F12</code> → вкладка <strong>Console</strong>.</div>
          <div>3. Вставь команду и нажми Enter:</div>
          <pre style={{
            background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 6,
            padding: '8px 10px', fontSize: 11, color: 'var(--blue)', margin: '6px 0',
            fontFamily: 'var(--font-mono)', overflowX: 'auto',
          }}>{`copy(JSON.stringify(localStorage))`}</pre>
          <div>4. Готово — localStorage уже в буфере. Вставь ниже (Ctrl+V).</div>
          <div style={{ marginTop: 6, fontSize: 11, color: 'var(--text-3)' }}>
            Эта команда копирует ВСЁ одним JSON-объектом — без обрезаний больших base64-аватарок, без табов.
          </div>
        </div>

        <div style={{
          padding: '10px 14px', background: 'rgba(124,77,255,0.06)',
          border: '1px solid rgba(124,77,255,0.18)', borderRadius: 10,
          fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6,
        }}>
          <div style={{ fontWeight: 700, marginBottom: 4, fontSize: 12 }}>Альтернатива (если консоль недоступна):</div>
          <div>F12 → <strong>Application → Local Storage → https://web.telegram.org</strong> → выдели всё (Ctrl+A) → копируй → вставь сюда. Парсер понимает оба формата.</div>
        </div>

        <div>
          <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>
            Содержимое localStorage
          </label>
          <textarea
            value={blob}
            onChange={e => setBlob(e.target.value)}
            placeholder='account1{"dcId":4,"dc4_auth_key":"..."} account2{...} ...'
            style={{
              width: '100%', minHeight: 200, padding: '12px 14px',
              background: 'var(--bg-3)', border: '1px solid var(--border)',
              borderRadius: 10, color: 'var(--text)', fontSize: 11,
              fontFamily: 'var(--font-mono)', outline: 'none', resize: 'vertical',
            }}
          />
          <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 4 }}>
            Ничего не отправляется до твоего подтверждения. На следующем шаге увидишь превью аккаунтов.
          </div>
        </div>

        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <Button variant="ghost" onClick={onClose}>Отмена</Button>
          <Button variant="primary" loading={parsing} disabled={!blob.trim()} onClick={handleParse}>
            Распарсить
          </Button>
        </div>
      </div>
    )
  }

  // ═══════════════ PREVIEW & IMPORT STEP ═══════════════
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <button onClick={() => { setStep('paste'); setPreviews([]); setAssignments({}) }} style={{
        background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer',
        fontSize: 12, padding: 0, textAlign: 'left',
      }}>← Назад</button>

      <div style={{
        padding: '12px 14px', background: 'rgba(61,214,140,0.06)',
        border: '1px solid rgba(61,214,140,0.2)', borderRadius: 10,
        fontSize: 13, color: 'var(--green)',
      }}>
        ✅ Найдено аккаунтов: <strong>{previews.length}</strong>. Назначь прокси и api_id, потом импортируй.
      </div>

      {/* Bulk assignment */}
      <div style={{
        padding: '12px 14px', background: 'var(--bg-3)', borderRadius: 10,
        display: 'flex', flexDirection: 'column', gap: 8,
      }}>
        <div style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
          ⚡ Применить ко всем
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <select
            value={bulkProxyId || ''}
            onChange={e => setBulkProxyId(e.target.value ? parseInt(e.target.value) : null)}
            style={{ flex: 1, padding: '8px 10px', background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 12, outline: 'none' }}>
            <option value="">— прокси для всех —</option>
            {proxies.map(p => (
              <option key={p.id} value={p.id}>
                {p.host}:{p.port} ({p.protocol}) {p.is_valid === true ? '✅' : ''}
              </option>
            ))}
          </select>
          <select
            value={bulkApiAppId || ''}
            onChange={e => setBulkApiAppId(e.target.value ? parseInt(e.target.value) : null)}
            style={{ flex: 1, padding: '8px 10px', background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 12, outline: 'none' }}>
            <option value="">— api_id (по умолчанию Web K 2496) —</option>
            {apiApps.map(a => (
              <option key={a.id} value={a.id}>
                {PLATFORM_ICONS[a.platform] || '🔑'} {a.title} ({a.api_id})
              </option>
            ))}
          </select>
          <Button variant="outline" size="sm" onClick={applyBulk}>Применить</Button>
        </div>
        <div style={{ fontSize: 10, color: 'var(--text-3)' }}>
          💡 Рекомендация: <strong>тот же прокси, через который ты входил в Web</strong>, и api_id <strong>Telegram Web K (2496)</strong>.
        </div>
      </div>

      {/* List of accounts */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxHeight: 400, overflowY: 'auto' }}>
        {previews.map(p => {
          const a = assignments[p.label] || {}
          const statusColor = {
            pending: 'var(--text-3)',
            importing: 'var(--blue)',
            success: 'var(--green)',
            error: 'var(--red)',
          }[a.status || 'pending']
          const statusIcon = { pending: '⏳', importing: '⟳', success: '✅', error: '❌' }[a.status || 'pending']

          return (
            <div key={p.label} style={{
              padding: '12px 14px', background: 'var(--bg-3)',
              border: `1px solid ${a.status === 'success' ? 'rgba(61,214,140,0.3)' : a.status === 'error' ? 'rgba(248,81,73,0.3)' : 'var(--border)'}`,
              borderRadius: 10,
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
                <span style={{ fontSize: 18 }}>{statusIcon}</span>
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 600, fontSize: 13 }}>
                    {p.label} <Badge>DC {p.dc_id}</Badge>
                    {p.user_id && <span style={{ marginLeft: 8, color: 'var(--text-3)', fontSize: 11 }}>user_id: {p.user_id}</span>}
                  </div>
                  <div style={{ fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--font-mono)', marginTop: 2 }}>
                    fingerprint: {p.fingerprint || '—'} · auth_key: {p.auth_key.slice(0, 16)}...
                  </div>
                </div>
                {a.status === 'success' && (
                  <span style={{ fontSize: 12, color: statusColor }}>{a.message}</span>
                )}
              </div>

              {a.status !== 'success' && (
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr auto', gap: 6 }}>
                  <select
                    value={a.proxyId || ''}
                    onChange={e => updateAssignment(p.label, { proxyId: e.target.value ? parseInt(e.target.value) : null })}
                    style={{ padding: '7px 10px', background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 6, color: 'var(--text)', fontSize: 11, outline: 'none' }}>
                    <option value="">— прокси —</option>
                    {proxies.map(pr => (
                      <option key={pr.id} value={pr.id}>{pr.host}:{pr.port}</option>
                    ))}
                  </select>
                  <select
                    value={a.apiAppId || ''}
                    onChange={e => updateAssignment(p.label, { apiAppId: e.target.value ? parseInt(e.target.value) : null })}
                    style={{ padding: '7px 10px', background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 6, color: 'var(--text)', fontSize: 11, outline: 'none' }}>
                    <option value="">Web K (2496)</option>
                    {apiApps.map(ap => (
                      <option key={ap.id} value={ap.id}>{PLATFORM_ICONS[ap.platform]} {ap.title}</option>
                    ))}
                  </select>
                  <Button
                    variant="primary" size="sm"
                    disabled={!a.proxyId || a.status === 'importing'}
                    onClick={() => handleImportOne(p)}>
                    {a.status === 'importing' ? <Spinner size={12} /> : 'Импорт'}
                  </Button>
                </div>
              )}

              {a.status === 'error' && (
                <div style={{ marginTop: 6, fontSize: 11, color: 'var(--red)' }}>
                  ❌ {a.message}
                </div>
              )}
            </div>
          )
        })}
      </div>

      <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
        <Button variant="ghost" onClick={onClose}>Закрыть</Button>
        <Button variant="primary" loading={importing} onClick={handleImportAll}>
          Импортировать все
        </Button>
      </div>
    </div>
  )
}
