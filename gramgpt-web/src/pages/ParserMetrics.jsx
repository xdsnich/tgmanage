import { useEffect, useState } from 'react'
import { parserAPI } from '../services/api'
import { Button, Spinner, Empty, Badge } from '../components/ui'

/**
 * Компонент таба "📊 Метрики" парсера.
 * Показывает KPI, график активности, FLOOD события, топ seeds, по аккаунтам.
 */
export default function ParserMetrics() {
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)

  const [overview, setOverview] = useState(null)
  const [activity, setActivity] = useState([])
  const [floodEvents, setFloodEvents] = useState([])
  const [topSeeds, setTopSeeds] = useState([])
  const [byAccount, setByAccount] = useState([])
  const [sessions, setSessions] = useState([])

  const load = async () => {
    setRefreshing(true)
    try {
      const [ov, act, flood, seeds, accs, sess] = await Promise.all([
        parserAPI.statsOverview().catch(() => ({ data: null })),
        parserAPI.statsActivity(7).catch(() => ({ data: { days: [] } })),
        parserAPI.statsFloodEvents(20).catch(() => ({ data: [] })),
        parserAPI.statsTopSeeds(10).catch(() => ({ data: [] })),
        parserAPI.statsByAccount().catch(() => ({ data: [] })),
        parserAPI.statsSessions(15).catch(() => ({ data: [] })),
      ])
      setOverview(ov.data)
      setActivity(act.data?.days || [])
      setFloodEvents(flood.data || [])
      setTopSeeds(seeds.data || [])
      setByAccount(accs.data || [])
      setSessions(sess.data || [])
    } catch (err) { console.error('metrics load:', err) }
    setLoading(false)
    setRefreshing(false)
  }

  useEffect(() => { load() }, [])

  // Авто-обновление каждые 30 сек
  useEffect(() => {
    const iv = setInterval(() => load(), 30000)
    return () => clearInterval(iv)
  }, [])

  if (loading) return <div style={{ display: 'flex', justifyContent: 'center', padding: 60 }}><Spinner size={32} /></div>

  const maxActivity = Math.max(1, ...activity.map(d => d.count))

  const sourceColors = {
    similar: '#7c4dff',
    telegram: '#3d8bff',
    search: '#3d8bff',
    import: '#00c2b2',
    unknown: '#888',
  }

  const sourceLabels = {
    similar: '🕸 Crawler',
    telegram: '🔍 Поиск',
    search: '🔍 Поиск',
    import: '📥 Импорт',
    unknown: '❓ Неизвестно',
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>

      {/* Refresh button */}
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <Button variant="ghost" size="sm" onClick={load} disabled={refreshing}>
          {refreshing ? <Spinner size={12} /> : '🔄'} Обновить
        </Button>
      </div>

      {/* ══ KPI карточки ══ */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 12 }}>
        <KpiCard
          icon="🔍"
          label="Всего каналов"
          value={overview?.total_channels ?? 0}
          sub={`+${overview?.today_added ?? 0} сегодня`}
          subColor="var(--green)"
          accent="linear-gradient(135deg, rgba(61,139,255,0.15), rgba(124,77,255,0.05))"
          border="rgba(61,139,255,0.3)"
        />
        <KpiCard
          icon="⏳"
          label="Добавлено сегодня"
          value={overview?.today_added ?? 0}
          sub="за сутки"
          accent="linear-gradient(135deg, rgba(61,214,140,0.15), rgba(0,194,178,0.05))"
          border="rgba(61,214,140,0.3)"
        />
        <KpiCard
          icon="⚠"
          label="FLOOD сегодня"
          value={overview?.today_flood_events ?? 0}
          sub={overview?.today_flood_wait_seconds ? `∑ ${overview.today_flood_wait_seconds}с ожидания` : 'нет блокировок'}
          subColor={overview?.today_flood_events > 0 ? 'var(--yellow)' : 'var(--text-3)'}
          accent="linear-gradient(135deg, rgba(255,193,7,0.15), rgba(255,61,154,0.05))"
          border="rgba(255,193,7,0.3)"
        />
        <KpiCard
          icon="⚡"
          label="Скорость"
          value={overview?.avg_speed_per_min ?? 0}
          sub="каналов / мин"
          accent="linear-gradient(135deg, rgba(0,194,178,0.15), rgba(61,139,255,0.05))"
          border="rgba(0,194,178,0.3)"
        />
      </div>

      {/* ══ График активности + источники ══ */}
      <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 12 }}>
        {/* График активности */}
        <div style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 12, padding: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
            📈 Активность за 7 дней
          </div>
          {activity.length === 0 ? (
            <div style={{ fontSize: 12, color: 'var(--text-3)', padding: 20, textAlign: 'center' }}>Нет данных</div>
          ) : (
            <div style={{ display: 'flex', alignItems: 'flex-end', gap: 8, height: 140 }}>
              {activity.map((d, i) => {
                const h = maxActivity > 0 ? (d.count / maxActivity) * 100 : 0
                const date = new Date(d.date)
                const dayName = date.toLocaleDateString('ru', { weekday: 'short' })
                const dayNum = date.getDate()
                return (
                  <div key={i} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4 }}>
                    <div style={{ fontSize: 10, color: d.count > 0 ? 'var(--violet)' : 'var(--text-3)', fontWeight: 700 }}>
                      {d.count || ''}
                    </div>
                    <div style={{
                      width: '100%',
                      height: `${Math.max(h, d.count > 0 ? 4 : 0)}%`,
                      background: d.count > 0 ? 'linear-gradient(180deg, #00c2b2 0%, #7c4dff 100%)' : 'var(--bg-3)',
                      borderRadius: '6px 6px 0 0',
                      transition: 'height 0.5s',
                      minHeight: d.count > 0 ? 4 : 2,
                    }} />
                    <div style={{ fontSize: 10, color: 'var(--text-3)', textAlign: 'center' }}>
                      <div>{dayName}</div>
                      <div style={{ opacity: 0.6 }}>{dayNum}</div>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>

        {/* По источникам */}
        <div style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 12, padding: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 12 }}>📊 Источники</div>
          {overview && overview.by_source && Object.keys(overview.by_source).length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {Object.entries(overview.by_source).map(([src, count]) => {
                const pct = overview.total_channels > 0 ? Math.round(count / overview.total_channels * 100) : 0
                return (
                  <div key={src}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, marginBottom: 4 }}>
                      <span>{sourceLabels[src] || src}</span>
                      <span style={{ color: 'var(--text-3)', fontWeight: 700 }}>{count} · {pct}%</span>
                    </div>
                    <div style={{ height: 6, background: 'rgba(255,255,255,0.06)', borderRadius: 3, overflow: 'hidden' }}>
                      <div style={{
                        width: `${pct}%`, height: '100%',
                        background: sourceColors[src] || '#888',
                        transition: 'width 0.5s',
                      }} />
                    </div>
                  </div>
                )
              })}
              <div style={{ marginTop: 4, padding: 8, background: 'var(--bg-3)', borderRadius: 6, fontSize: 10, color: 'var(--text-3)', textAlign: 'center' }}>
                Всего: <strong style={{ color: 'var(--text)' }}>{overview.total_channels}</strong> каналов
              </div>
            </div>
          ) : (
            <div style={{ fontSize: 12, color: 'var(--text-3)', padding: 10 }}>Нет данных</div>
          )}
        </div>
      </div>

      {/* ══ Top seeds + by account ══ */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        {/* Топ seeds */}
        <div style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 12, padding: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 12 }}>🏆 Топ источников (seeds / keywords)</div>
          {topSeeds.length === 0 ? (
            <div style={{ fontSize: 12, color: 'var(--text-3)' }}>Нет данных</div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 300, overflow: 'auto' }}>
              {topSeeds.map((s, i) => {
                const isSimilar = s.seed.startsWith('similar:')
                const clean = isSimilar ? s.seed.replace('similar:', '') : s.seed
                return (
                  <div key={i} style={{
                    display: 'flex', alignItems: 'center', gap: 8,
                    padding: '8px 10px', background: 'var(--bg-3)', borderRadius: 8,
                  }}>
                    <div style={{
                      width: 24, height: 24, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center',
                      background: i === 0 ? 'rgba(255,193,7,0.2)' : i < 3 ? 'rgba(124,77,255,0.15)' : 'rgba(255,255,255,0.05)',
                      color: i === 0 ? 'var(--yellow)' : i < 3 ? 'var(--violet)' : 'var(--text-3)',
                      fontSize: 11, fontWeight: 800,
                    }}>{i + 1}</div>
                    <span style={{ flex: 1, fontSize: 12, fontFamily: 'var(--font-mono)', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {clean}
                    </span>
                    {isSimilar && <Badge color="violet">🕸</Badge>}
                    <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--violet)' }}>{s.count}</span>
                  </div>
                )
              })}
            </div>
          )}
        </div>

        {/* По аккаунтам */}
        <div style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 12, padding: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 12 }}>👤 По аккаунтам</div>
          {byAccount.length === 0 ? (
            <div style={{ fontSize: 12, color: 'var(--text-3)' }}>Нет данных — запусти crawler/verify</div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 300, overflow: 'auto' }}>
              {byAccount.map((a, i) => (
                <div key={a.account_id} style={{
                  display: 'flex', alignItems: 'center', gap: 8,
                  padding: '8px 10px', background: 'var(--bg-3)', borderRadius: 8,
                }}>
                  <div style={{
                    width: 24, height: 24, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center',
                    background: i === 0 ? 'rgba(61,214,140,0.2)' : 'rgba(255,255,255,0.05)',
                    color: i === 0 ? 'var(--green)' : 'var(--text-3)',
                    fontSize: 11, fontWeight: 800,
                  }}>{i + 1}</div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 12, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{a.name}</div>
                    <div style={{ fontSize: 10, color: 'var(--text-3)' }}>{a.sessions} сессий</div>
                  </div>
                  <div style={{ textAlign: 'right' }}>
                    <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--green)' }}>{a.saved}</div>
                    <div style={{ fontSize: 9, color: 'var(--text-3)' }}>найдено {a.found}</div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ══ FLOOD_WAIT события ══ */}
      <div style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 12, padding: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <div style={{ fontSize: 13, fontWeight: 700 }}>
            ⚠ FLOOD_WAIT события <span style={{ color: 'var(--text-3)', fontWeight: 500 }}>(всего: {overview?.total_flood_events ?? 0})</span>
          </div>
          <span style={{ fontSize: 11, color: 'var(--text-3)' }}>Последние {floodEvents.length}</span>
        </div>
        {floodEvents.length === 0 ? (
          <div style={{ fontSize: 12, color: 'var(--green)', padding: 20, textAlign: 'center', background: 'rgba(61,214,140,0.06)', borderRadius: 8 }}>
            ✅ FLOOD не было — паузы настроены оптимально
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 280, overflow: 'auto' }}>
            {floodEvents.map(e => {
              const d = new Date(e.created_at)
              const timeStr = d.toLocaleTimeString('ru', { hour: '2-digit', minute: '2-digit' })
              const dateStr = d.toLocaleDateString('ru', { day: '2-digit', month: '2-digit' })
              const waitColor = e.wait_seconds > 60 ? 'var(--red)' : e.wait_seconds > 20 ? 'var(--yellow)' : 'var(--text-2)'
              return (
                <div key={e.id} style={{
                  display: 'grid', gridTemplateColumns: '70px 90px 60px 1fr', gap: 10,
                  padding: '6px 10px', background: 'var(--bg-3)', borderRadius: 6,
                  fontSize: 11, alignItems: 'center',
                }}>
                  <span style={{ color: 'var(--text-3)', fontFamily: 'var(--font-mono)' }}>{dateStr} {timeStr}</span>
                  <Badge color={e.source === 'similar' ? 'violet' : 'green'}>{e.source || '—'}</Badge>
                  <span style={{ fontWeight: 700, color: waitColor }}>{e.wait_seconds}с</span>
                  <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-2)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {e.seed ? `@${e.seed}` : ''} {e.details ? <span style={{ opacity: 0.6 }}>({e.details})</span> : ''}
                  </span>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* ══ История сессий ══ */}
      <div style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 12, padding: 16 }}>
        <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 12 }}>🗂 История сессий парсинга</div>
        {sessions.length === 0 ? (
          <div style={{ fontSize: 12, color: 'var(--text-3)', padding: 20, textAlign: 'center' }}>
            Сессий пока нет. Запусти crawler или verify.
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 400, overflow: 'auto' }}>
            {sessions.map(s => {
              const d = new Date(s.created_at)
              const timeStr = d.toLocaleString('ru', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
              const durationStr = s.duration_sec < 60
                ? `${s.duration_sec}с`
                : `${Math.floor(s.duration_sec / 60)}м ${s.duration_sec % 60}с`
              return (
                <div key={s.id} style={{
                  display: 'grid', gridTemplateColumns: '100px 90px 80px 80px 80px 1fr', gap: 10,
                  padding: '8px 10px', background: 'var(--bg-3)', borderRadius: 6,
                  fontSize: 11, alignItems: 'center',
                }}>
                  <span style={{ color: 'var(--text-3)', fontFamily: 'var(--font-mono)' }}>{timeStr}</span>
                  <Badge color={s.source === 'similar' ? 'violet' : s.source === 'verify' ? 'green' : 'blue'}>
                    {s.source}
                  </Badge>
                  <span style={{ color: 'var(--text-3)' }}>{durationStr}</span>
                  <span style={{ fontWeight: 700, color: 'var(--green)' }}>+{s.saved} сохр</span>
                  <span style={{ color: 'var(--violet)' }}>⚡ {s.speed_per_min}/мин</span>
                  <span style={{ color: 'var(--text-3)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 10 }}>
                    {s.details || ''}
                  </span>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

// ─── Вспомогательный компонент ───
function KpiCard({ icon, label, value, sub, subColor = 'var(--text-3)', accent, border }) {
  return (
    <div style={{
      padding: '14px 16px', borderRadius: 12,
      background: accent || 'var(--bg-2)',
      border: `1px solid ${border || 'var(--border)'}`,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <span style={{ fontSize: 16 }}>{icon}</span>
        <span style={{ fontSize: 10, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase' }}>{label}</span>
      </div>
      <div style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.03em', lineHeight: 1 }}>
        {typeof value === 'number' ? value.toLocaleString() : value}
      </div>
      {sub && <div style={{ fontSize: 10, color: subColor, marginTop: 4 }}>{sub}</div>}
    </div>
  )
}
