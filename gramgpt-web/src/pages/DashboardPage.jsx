import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { accountsAPI, tasksAPI } from '../services/api'
import { Card, TrustBar, Button, Spinner, StatusBadge, StatCard } from '../components/ui'
import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer } from 'recharts'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
const PIE_COLORS = { active: '#3dd68c', spamblock: '#f85149', frozen: '#e3a13f', error: '#f85149', unknown: '#444' }

export default function DashboardPage() {
  const [stats, setStats] = useState(null)
  const [accounts, setAccounts] = useState([])
  const [loading, setLoading] = useState(true)
  const [checking, setChecking] = useState(false)
  const [taskId, setTaskId] = useState(null)
  const navigate = useNavigate()

  const load = async () => {
    try {
      const [s, a] = await Promise.all([accountsAPI.stats(), accountsAPI.list()])
      setStats(s.data); setAccounts(a.data)
    } catch { }
    setLoading(false)
  }

  useEffect(() => { load() }, [])
  useAutoRefresh(() => load(), 15000)
  useEffect(() => {
    if (!taskId) return
    const iv = setInterval(async () => {
      try {
        const { data } = await tasksAPI.getStatus(taskId)
        if (data.status === 'SUCCESS' || data.status === 'FAILURE') {
          setChecking(false); setTaskId(null); load(); clearInterval(iv)
        }
      } catch { clearInterval(iv); setChecking(false); setTaskId(null) }
    }, 1500)
    return () => clearInterval(iv)
  }, [taskId])

  const handleCheckAll = async () => {
    setChecking(true)
    try { const { data } = await tasksAPI.checkAccounts(false); setTaskId(data.task_id) }
    catch { setChecking(false) }
  }

  if (loading) return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
      <Spinner size={32} />
    </div>
  )

  const pieData = stats ? [
    { name: 'Активных', value: stats.active, key: 'active' },
    { name: 'Спамблок', value: stats.spamblock, key: 'spamblock' },
    { name: 'Заморожено', value: stats.frozen, key: 'frozen' },
    { name: 'Ошибки', value: stats.error, key: 'error' },
    { name: 'Неизвестно', value: stats.unknown, key: 'unknown' },
  ].filter(d => d.value > 0) : []

  const recent = accounts.slice(0, 6)

  return (
    <div style={{ padding: '28px 32px', maxWidth: 1120, animation: 'fadeUp 0.4s cubic-bezier(0.16,1,0.3,1)' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 32 }}>
        <div>
          <div style={{ fontSize: 11, color: 'var(--violet)', fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>
            ● LIVE DASHBOARD
          </div>
          <h1 style={{ fontSize: 28, fontWeight: 800, letterSpacing: '-0.04em', lineHeight: 1.1 }}>
            Health Monitor
          </h1>
          <p style={{ fontSize: 13, color: 'var(--text-3)', marginTop: 6 }}>
            Состояние пула Telegram-аккаунтов в реальном времени
          </p>
        </div>
        <Button variant="primary" onClick={handleCheckAll} loading={checking} disabled={checking || !stats?.total}>
          {checking ? 'Проверяю...' : '⚡ Проверить всё'}
        </Button>
      </div>

      {/* Stats grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 14, marginBottom: 24 }}>
        <StatCard label="Всего" value={stats?.total ?? 0} icon="◈" />
        <StatCard label="Активных" value={stats?.active ?? 0} color="var(--green)" icon="●" />
        <StatCard label="Спамблок" value={stats?.spamblock ?? 0} color="var(--red)" icon="◉" />
        <StatCard label="Заморожено" value={stats?.frozen ?? 0} color="var(--yellow)" icon="◎" />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 300px', gap: 16 }}>
        {/* Accounts list */}
        <Card style={{ padding: 0 }}>
          <div style={{
            padding: '16px 20px', borderBottom: '1px solid var(--border)',
            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          }}>
            <span style={{ fontWeight: 700, fontSize: 14, letterSpacing: '-0.02em' }}>Последние аккаунты</span>
            <button onClick={() => navigate('/accounts')} style={{
              fontSize: 12, color: 'var(--violet)', background: 'none', border: 'none',
              cursor: 'pointer', fontWeight: 600,
            }}>Все аккаунты →</button>
          </div>
          {recent.length === 0 ? (
            <div style={{ padding: '40px', textAlign: 'center', color: 'var(--text-3)', fontSize: 13 }}>
              Нет аккаунтов.{' '}
              <button onClick={() => navigate('/accounts')} style={{ color: 'var(--violet)', background: 'none', border: 'none', cursor: 'pointer', fontWeight: 600 }}>
                Добавить?
              </button>
            </div>
          ) : recent.map((acc, i) => (
            <div key={acc.id} onClick={() => navigate('/accounts')} style={{
              padding: '14px 20px', display: 'flex', alignItems: 'center', gap: 14,
              borderBottom: i < recent.length - 1 ? '1px solid var(--border)' : 'none',
              cursor: 'pointer', transition: 'background 0.12s',
            }}
              onMouseEnter={e => e.currentTarget.style.background = 'rgba(255,255,255,0.025)'}
              onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
              {/* Avatar */}
              <div style={{
                width: 38, height: 38, borderRadius: 10, flexShrink: 0,
                background: 'linear-gradient(135deg, rgba(124,77,255,0.3), rgba(61,139,255,0.2))',
                border: '1px solid rgba(124,77,255,0.2)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontSize: 15, fontWeight: 700, color: 'var(--violet)',
              }}>
                {acc.first_name?.[0]?.toUpperCase() || '?'}
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 2 }}>
                  {acc.first_name} {acc.last_name || ''}
                  {acc.username && <span style={{ color: 'var(--text-3)', marginLeft: 6, fontSize: 11, fontWeight: 400 }}>@{acc.username}</span>}
                </div>
                <div style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'var(--font-mono)' }}>{acc.phone}</div>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 6 }}>
                <StatusBadge status={acc.status} />
                <TrustBar score={acc.trust_score} />
              </div>
            </div>
          ))}
        </Card>

        {/* Right column */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {/* Pie chart */}
          {pieData.length > 0 && (
            <Card>
              <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 16, letterSpacing: '-0.02em' }}>Распределение</div>
              <ResponsiveContainer width="100%" height={160}>
                <PieChart>
                  <Pie data={pieData} cx="50%" cy="50%" innerRadius={45} outerRadius={70} paddingAngle={4} dataKey="value">
                    {pieData.map(d => <Cell key={d.key} fill={PIE_COLORS[d.key] || '#555'} />)}
                  </Pie>
                  <Tooltip contentStyle={{ background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 12 }} />
                </PieChart>
              </ResponsiveContainer>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 7, marginTop: 6 }}>
                {pieData.map(d => (
                  <div key={d.key} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
                    <div style={{ width: 8, height: 8, borderRadius: 2, background: PIE_COLORS[d.key], flexShrink: 0 }} />
                    <span style={{ color: 'var(--text-2)', flex: 1 }}>{d.name}</span>
                    <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--text)', fontWeight: 600 }}>{d.value}</span>
                  </div>
                ))}
              </div>
            </Card>
          )}

          {/* Trust summary */}
          {stats?.total > 0 && (
            <Card style={{ background: 'linear-gradient(145deg, #2d1b4e 0%, #1a1025 100%)', borderColor: 'rgba(124,77,255,0.2)' }}>
              <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 14, letterSpacing: '-0.02em' }}>Trust Score</div>
              <TrustBar score={stats.avg_trust || 0} />
              <div style={{ display: 'flex', gap: 16, marginTop: 14 }}>
                {[
                  { label: 'Макс', val: stats.max_trust || 0, color: 'var(--green)' },
                  { label: 'Мин', val: stats.min_trust || 0, color: 'var(--red)' },
                  { label: '2FA', val: stats.with_2fa || 0, color: 'var(--violet)' },
                ].map(({ label, val, color }) => (
                  <div key={label} style={{ flex: 1, textAlign: 'center' }}>
                    <div style={{ fontSize: 20, fontWeight: 800, color, letterSpacing: '-0.04em' }}>{val}</div>
                    <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 2, letterSpacing: '0.06em' }}>{label}</div>
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}
