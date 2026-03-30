import { useEffect, useState } from 'react'
import { analyticsAPI } from '../services/api'
import { Card, StatCard, Badge, Spinner, TrustBar, Button, Empty } from '../components/ui'
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell,
  PieChart, Pie, RadialBarChart, RadialBar, Legend,
} from 'recharts'

const STATUS_COLORS = {
  active: '#3dd68c', spamblock: '#f85149', frozen: '#e3a13f',
  quarantine: '#ff3d9a', error: '#f85149', unknown: '#555',
}
const STATUS_LABELS = {
  active: 'Активные', spamblock: 'Спамблок', frozen: 'Заморожено',
  quarantine: 'Карантин', error: 'Ошибка', unknown: 'Неизвестно',
}
const BUCKET_COLORS = {
  excellent: '#3dd68c', good: '#00c2b2', medium: '#e3a13f', weak: '#ff6b35', critical: '#f85149',
}
const BUCKET_LABELS = {
  excellent: 'Отличный (80-100)', good: 'Хороший (60-79)', medium: 'Средний (40-59)',
  weak: 'Слабый (20-39)', critical: 'Критический (0-19)',
}

const CustomTooltip = ({ active, payload }) => {
  if (!active || !payload?.length) return null
  const d = payload[0]
  return (
    <div style={{
      padding: '8px 14px', background: 'var(--bg-2)', border: '1px solid var(--border)',
      borderRadius: 10, fontSize: 12, boxShadow: '0 8px 24px rgba(0,0,0,0.5)',
    }}>
      <div style={{ fontWeight: 600 }}>{d.name || d.payload?.name}</div>
      <div style={{ color: d.color || 'var(--violet)', fontFamily: 'var(--font-mono)', marginTop: 2 }}>{d.value}</div>
    </div>
  )
}

export default function AnalyticsPage() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)

  const load = async () => {
    setLoading(true)
    try { const { data: d } = await analyticsAPI.dashboard(); setData(d) }
    catch {}
    setLoading(false)
  }

  useEffect(() => { load() }, [])

  if (loading) return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
      <Spinner size={32} />
    </div>
  )

  if (!data || data.total === 0) return (
    <div style={{ padding: '28px 32px' }}>
      <div style={{ marginBottom: 28 }}>
        <div style={{ fontSize: 11, color: 'var(--teal)', fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>◈ АНАЛИТИКА</div>
        <h1 style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.04em' }}>Аналитика</h1>
      </div>
      <Empty icon="📊" title="Нет данных для аналитики" subtitle="Добавьте аккаунты и проведите проверку" />
    </div>
  )

  const { total, by_status, trust, checks, profile, plan, account_limit, used_slots } = data

  // Prepare chart data
  const statusPieData = Object.entries(by_status).map(([key, val]) => ({
    name: STATUS_LABELS[key] || key, value: val, color: STATUS_COLORS[key] || '#555',
  })).filter(d => d.value > 0)

  const trustBucketData = Object.entries(trust.buckets).map(([key, val]) => ({
    name: BUCKET_LABELS[key] || key, value: val, color: BUCKET_COLORS[key] || '#555', key,
  })).filter(d => d.value > 0)

  const profileData = [
    { name: 'Username', value: profile.with_username, total, fill: '#7c4dff' },
    { name: 'Bio', value: profile.with_bio, total, fill: '#3d8bff' },
    { name: 'Фото', value: profile.with_photo, total, fill: '#ff3d9a' },
    { name: 'Прокси', value: profile.with_proxy, total, fill: '#00c2b2' },
    { name: '2FA', value: profile.with_2fa, total, fill: '#e3a13f' },
  ]

  const usagePct = account_limit > 0 ? Math.round((used_slots / account_limit) * 100) : 0

  return (
    <div style={{ padding: '28px 32px', animation: 'fadeUp 0.4s cubic-bezier(0.16,1,0.3,1)' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 28 }}>
        <div>
          <div style={{ fontSize: 11, color: 'var(--teal)', fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>◈ АНАЛИТИКА</div>
          <h1 style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.04em' }}>Health Dashboard</h1>
          <p style={{ fontSize: 13, color: 'var(--text-3)', marginTop: 4 }}>Полная аналитика по {total} аккаунтам</p>
        </div>
        <Button variant="ghost" onClick={load}>↻ Обновить</Button>
      </div>

      {/* Stats row */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 12, marginBottom: 20 }}>
        <StatCard label="Всего" value={total} icon="👤" />
        <StatCard label="Активных" value={by_status.active || 0} color="var(--green)" icon="✅" />
        <StatCard label="Спамблок" value={by_status.spamblock || 0} color="var(--red)" icon="🚫" />
        <StatCard label="Trust (сред.)" value={trust.avg} color="var(--violet)" icon="⚡" />
        <StatCard label="Лимит" value={`${used_slots}/${account_limit}`} color="var(--blue)" icon="📦" />
      </div>

      {/* Charts row */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 16, marginBottom: 16 }}>

        {/* Status distribution pie */}
        <Card>
          <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 16, letterSpacing: '-0.02em' }}>Распределение статусов</div>
          <ResponsiveContainer width="100%" height={200}>
            <PieChart>
              <Pie data={statusPieData} cx="50%" cy="50%" innerRadius={45} outerRadius={75} paddingAngle={3} dataKey="value">
                {statusPieData.map((d, i) => <Cell key={i} fill={d.color} stroke="transparent" />)}
              </Pie>
              <Tooltip content={<CustomTooltip />} />
            </PieChart>
          </ResponsiveContainer>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 8 }}>
            {statusPieData.map(d => (
              <div key={d.name} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11, color: 'var(--text-3)' }}>
                <div style={{ width: 8, height: 8, borderRadius: 2, background: d.color }} />
                {d.name}: {d.value}
              </div>
            ))}
          </div>
        </Card>

        {/* Trust Score distribution bar */}
        <Card>
          <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 16, letterSpacing: '-0.02em' }}>Trust Score</div>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={trustBucketData} margin={{ top: 0, right: 0, bottom: 0, left: -20 }}>
              <XAxis dataKey="name" tick={false} axisLine={false} />
              <YAxis tick={{ fill: 'rgba(255,255,255,0.3)', fontSize: 11 }} axisLine={false} tickLine={false} />
              <Tooltip content={<CustomTooltip />} />
              <Bar dataKey="value" radius={[6, 6, 0, 0]}>
                {trustBucketData.map((d, i) => <Cell key={i} fill={d.color} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 8 }}>
            {trustBucketData.map(d => (
              <div key={d.key} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11, color: 'var(--text-3)' }}>
                <div style={{ width: 8, height: 8, borderRadius: 2, background: d.color }} />
                {d.name}: {d.value}
              </div>
            ))}
          </div>
          <div style={{ marginTop: 10, display: 'flex', gap: 16, fontSize: 12 }}>
            <span style={{ color: 'var(--text-3)' }}>Мин: <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--red)' }}>{trust.min}</span></span>
            <span style={{ color: 'var(--text-3)' }}>Сред: <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--violet)' }}>{trust.avg}</span></span>
            <span style={{ color: 'var(--text-3)' }}>Макс: <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--green)' }}>{trust.max}</span></span>
          </div>
        </Card>

        {/* Checks summary */}
        <Card>
          <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 16, letterSpacing: '-0.02em' }}>Проверки</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            {[
              { label: 'Проверено сегодня', value: checks.today, color: 'var(--green)' },
              { label: 'За неделю', value: checks.week, color: 'var(--blue)' },
              { label: 'Никогда не проверялось', value: checks.never, color: 'var(--red)' },
            ].map(({ label, value, color }) => (
              <div key={label}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                  <span style={{ fontSize: 12, color: 'var(--text-3)' }}>{label}</span>
                  <span style={{ fontSize: 14, fontWeight: 700, color, fontFamily: 'var(--font-mono)' }}>{value}</span>
                </div>
                <div style={{ height: 4, background: 'rgba(255,255,255,0.06)', borderRadius: 2, overflow: 'hidden' }}>
                  <div style={{
                    width: total > 0 ? `${Math.round((value / total) * 100)}%` : '0%',
                    height: '100%', background: color, borderRadius: 2,
                    transition: 'width 0.6s ease',
                  }} />
                </div>
              </div>
            ))}
          </div>

          {/* Plan usage */}
          <div style={{ marginTop: 20, paddingTop: 14, borderTop: '1px solid var(--border)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
              <span style={{ fontSize: 12, color: 'var(--text-3)' }}>Использование тарифа</span>
              <Badge color={usagePct > 90 ? 'red' : usagePct > 70 ? 'yellow' : 'green'}>
                {plan.toUpperCase()} — {usagePct}%
              </Badge>
            </div>
            <div style={{ height: 6, background: 'rgba(255,255,255,0.06)', borderRadius: 3, overflow: 'hidden' }}>
              <div style={{
                width: `${Math.min(usagePct, 100)}%`,
                height: '100%',
                background: usagePct > 90 ? 'var(--red)' : usagePct > 70 ? 'var(--yellow)' : 'var(--grad-purple)',
                borderRadius: 3, transition: 'width 0.6s ease',
              }} />
            </div>
            <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4 }}>
              {used_slots} из {account_limit} аккаунтов
            </div>
          </div>
        </Card>
      </div>

      {/* Profile completeness */}
      <Card style={{ marginBottom: 16 }}>
        <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 18, letterSpacing: '-0.02em' }}>Заполненность профилей</div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 16 }}>
          {profileData.map(({ name, value, total: t, fill }) => {
            const pct = t > 0 ? Math.round((value / t) * 100) : 0
            return (
              <div key={name} style={{ textAlign: 'center' }}>
                {/* Circular progress */}
                <div style={{ position: 'relative', width: 80, height: 80, margin: '0 auto 10px' }}>
                  <svg viewBox="0 0 36 36" style={{ width: 80, height: 80, transform: 'rotate(-90deg)' }}>
                    <circle cx="18" cy="18" r="15.5" fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth="3" />
                    <circle cx="18" cy="18" r="15.5" fill="none" stroke={fill} strokeWidth="3"
                      strokeDasharray={`${pct * 0.975} 100`}
                      strokeLinecap="round" style={{ transition: 'stroke-dasharray 0.8s ease' }} />
                  </svg>
                  <div style={{
                    position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
                    fontSize: 14, fontWeight: 700, fontFamily: 'var(--font-mono)', color: fill,
                  }}>{pct}%</div>
                </div>
                <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 2 }}>{name}</div>
                <div style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'var(--font-mono)' }}>{value}/{t}</div>
              </div>
            )
          })}
        </div>
      </Card>

      {/* Recommendations */}
      <Card>
        <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 14, letterSpacing: '-0.02em' }}>Рекомендации по улучшению</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {checks.never > 0 && (
            <RecommendationRow
              icon="🔍" color="var(--red)"
              text={`${checks.never} аккаунтов никогда не проверялись — запустите проверку статуса`}
            />
          )}
          {profile.with_2fa < total && (
            <RecommendationRow
              icon="🔐" color="var(--yellow)"
              text={`${total - profile.with_2fa} аккаунтов без 2FA — установите двухфакторную аутентификацию`}
            />
          )}
          {profile.with_proxy < total && (
            <RecommendationRow
              icon="🌐" color="var(--blue)"
              text={`${total - profile.with_proxy} аккаунтов без прокси — назначьте прокси для безопасности`}
            />
          )}
          {profile.with_photo < total && (
            <RecommendationRow
              icon="📸" color="var(--pink)"
              text={`${total - profile.with_photo} аккаунтов без фото — загрузите аватарки для повышения Trust Score`}
            />
          )}
          {profile.with_bio < total && (
            <RecommendationRow
              icon="✏️" color="var(--teal)"
              text={`${total - profile.with_bio} аккаунтов без Bio — заполните описание профиля`}
            />
          )}
          {(by_status.spamblock || 0) > 0 && (
            <RecommendationRow
              icon="🚫" color="var(--red)"
              text={`${by_status.spamblock} аккаунтов в спамблоке — переведите в карантин и дождитесь снятия`}
            />
          )}
          {trust.avg < 50 && (
            <RecommendationRow
              icon="⚡" color="var(--orange)"
              text="Средний Trust Score ниже 50 — заполните профили и избегайте массовых действий"
            />
          )}
          {checks.never === 0 && profile.with_2fa === total && profile.with_proxy === total && trust.avg >= 70 && (
            <div style={{ padding: '14px 16px', background: 'var(--green-dim)', border: '1px solid rgba(61,214,140,0.2)', borderRadius: 10, fontSize: 13, color: 'var(--green)', fontWeight: 600 }}>
              ✅ Всё отлично! Все аккаунты проверены, защищены и настроены.
            </div>
          )}
        </div>
      </Card>
    </div>
  )
}

function RecommendationRow({ icon, color, text }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 12, padding: '10px 14px',
      background: 'var(--bg-3)', borderRadius: 10, borderLeft: `3px solid ${color}`,
    }}>
      <span style={{ fontSize: 18, flexShrink: 0 }}>{icon}</span>
      <span style={{ fontSize: 12, color: 'var(--text-2)', lineHeight: 1.5 }}>{text}</span>
    </div>
  )
}
