/**
 * AccountErrorsPanel — переиспользуемая панель ошибок аккаунтов.
 * Используется во вкладке Прогрева, Комментинга и на детали аккаунта.
 *
 * Props:
 *   accountId?      number — фильтр по конкретному акку (для AccountDetail)
 *   sourceFilter?   "warmup" | "commenting" | null — pre-set источник
 *   embedded?       bool — рендерить без обёрток Card (для модалок)
 */
import { useEffect, useState } from 'react'
import { accountsAPI } from '../services/api'

const ERROR_COLORS = {
  FLOOD_WAIT: '#eab308',
  PEER_FLOOD: '#f97316',
  AUTH_KEY_UNREGISTERED: '#ef4444',
  FROZEN: '#ef4444',
  ProxyTimeout: '#94a3b8',
  ProxyConnectError: '#94a3b8',
  Timeout: '#94a3b8',
  ConnectionError: '#94a3b8',
  Captcha: '#a855f7',
  SessionRevoked: '#dc2626',
  Unknown: '#64748b',
}
const ERROR_EMOJI = {
  FLOOD_WAIT: '⏳',
  PEER_FLOOD: '🚫',
  AUTH_KEY_UNREGISTERED: '🔑',
  FROZEN: '❄️',
  ProxyTimeout: '🌐',
  ProxyConnectError: '🌐',
  Timeout: '⏱',
  ConnectionError: '🔌',
  Captcha: '🤖',
  SessionRevoked: '⛔',
  Unknown: '❓',
}

export default function AccountErrorsPanel({ accountId, sourceFilter, embedded }) {
  const [errors, setErrors] = useState([])
  const [loading, setLoading] = useState(true)
  const [hours, setHours] = useState(24)
  const [source, setSource] = useState(sourceFilter || '')
  const [errorType, setErrorType] = useState('')
  const [summary, setSummary] = useState(null)

  const load = async () => {
    setLoading(true)
    try {
      const params = { hours, limit: 200 }
      if (accountId) params.account_id = accountId
      if (source) params.source = source
      if (errorType) params.error_type = errorType
      const [errRes, sumRes] = await Promise.all([
        accountsAPI.errors(params),
        accountId ? Promise.resolve({ data: null }) : accountsAPI.errorsSummary(hours),
      ])
      setErrors(errRes.data || [])
      setSummary(sumRes.data)
    } catch (e) {
      console.error('account errors load', e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() /* eslint-disable-line */ }, [hours, source, errorType, accountId])

  const wrap = (children) => embedded ? <div>{children}</div> : (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>{children}</div>
  )

  return wrap(
    <>
      {/* Top filters */}
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', gap: 4 }}>
          {[1, 6, 24, 168, 720].map(h => (
            <button key={h} onClick={() => setHours(h)} style={{
              padding: '5px 10px', borderRadius: 6, fontSize: 11,
              background: hours === h ? 'rgba(124,77,255,0.15)' : 'var(--bg-3)',
              border: `1px solid ${hours === h ? 'var(--violet)' : 'var(--border)'}`,
              color: hours === h ? 'var(--violet)' : 'var(--text-2)', cursor: 'pointer',
            }}>{h < 24 ? `${h}ч` : h === 24 ? '24ч' : h === 168 ? '7 дн' : '30 дн'}</button>
          ))}
        </div>
        <select value={source} onChange={e => setSource(e.target.value)} style={{
          padding: '5px 8px', borderRadius: 6, fontSize: 11,
          background: 'var(--bg-3)', border: '1px solid var(--border)', color: 'var(--text-2)',
        }}>
          <option value="">Все источники</option>
          <option value="warmup">прогрев</option>
          <option value="commenting">комментинг</option>
          <option value="parser">парсер</option>
          <option value="bulk">bulk</option>
          <option value="tg_auth">авторизация</option>
        </select>
        {!accountId && summary && summary.by_type?.length > 0 && (
          <select value={errorType} onChange={e => setErrorType(e.target.value)} style={{
            padding: '5px 8px', borderRadius: 6, fontSize: 11,
            background: 'var(--bg-3)', border: '1px solid var(--border)', color: 'var(--text-2)',
          }}>
            <option value="">Все типы</option>
            {summary.by_type.map(t => (
              <option key={t.error_type} value={t.error_type}>
                {ERROR_EMOJI[t.error_type] || '❓'} {t.error_type} ({t.count})
              </option>
            ))}
          </select>
        )}
        <button onClick={load} style={{
          marginLeft: 'auto', padding: '5px 10px', borderRadius: 6, fontSize: 11,
          background: 'none', border: '1px solid var(--border)', color: 'var(--text-3)', cursor: 'pointer',
        }}>↻ Обновить</button>
      </div>

      {/* Summary tiles */}
      {!accountId && summary && summary.total > 0 && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {summary.by_type.slice(0, 8).map(t => (
            <button key={t.error_type}
              onClick={() => setErrorType(errorType === t.error_type ? '' : t.error_type)}
              style={{
                padding: '6px 10px', borderRadius: 8, fontSize: 11, cursor: 'pointer',
                background: errorType === t.error_type ? `${ERROR_COLORS[t.error_type]}33` : 'var(--bg-3)',
                border: `1px solid ${errorType === t.error_type ? ERROR_COLORS[t.error_type] : 'var(--border)'}`,
                color: ERROR_COLORS[t.error_type] || 'var(--text-2)',
              }}>
              {ERROR_EMOJI[t.error_type] || '❓'} {t.error_type}: <b>{t.count}</b>
            </button>
          ))}
        </div>
      )}

      {/* List */}
      {loading ? (
        <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-3)', fontSize: 12 }}>Загрузка…</div>
      ) : errors.length === 0 ? (
        <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-3)', fontSize: 12 }}>
          ✨ Нет ошибок за выбранный период
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 540, overflowY: 'auto' }}>
          {errors.map(e => (
            <div key={e.id} style={{
              padding: '10px 12px', borderRadius: 8, background: 'var(--bg-3)',
              border: `1px solid ${(ERROR_COLORS[e.error_type] || 'var(--border)')}55`,
              display: 'grid', gridTemplateColumns: '32px 1fr auto', gap: 10, alignItems: 'flex-start',
            }}>
              <div style={{ fontSize: 18 }}>{ERROR_EMOJI[e.error_type] || '❓'}</div>
              <div style={{ minWidth: 0 }}>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 2, flexWrap: 'wrap' }}>
                  <span style={{
                    fontSize: 11, padding: '1px 6px', borderRadius: 4, fontWeight: 600,
                    color: ERROR_COLORS[e.error_type] || 'var(--text-2)',
                    background: `${ERROR_COLORS[e.error_type] || '#666'}22`,
                  }}>{e.error_type}</span>
                  <span style={{ fontSize: 10, color: 'var(--text-3)' }}>{e.source}</span>
                  {!accountId && (
                    <span style={{ fontSize: 10, color: 'var(--text-3)' }}>акк #{e.account_id}</span>
                  )}
                </div>
                <div style={{ fontSize: 12, color: 'var(--text-2)', wordBreak: 'break-word' }}>
                  {e.message || '—'}
                </div>
              </div>
              <div style={{ fontSize: 10, color: 'var(--text-3)', whiteSpace: 'nowrap' }}>
                {e.created_at ? new Date(e.created_at).toLocaleString('ru-RU') : ''}
              </div>
            </div>
          ))}
        </div>
      )}

      <div style={{ fontSize: 10, color: 'var(--text-3)', textAlign: 'center' }}>
        Полные логи также в файлах: logs/accounts/&lt;user_id&gt;/&lt;account_id&gt;.log
      </div>
    </>
  )
}
