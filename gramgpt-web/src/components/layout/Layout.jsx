import { NavLink, useNavigate } from 'react-router-dom'
import { useAuthStore } from '../../store/authStore'

const NAV = [
  { to: '/', emoji: '◈', label: 'Дашборд', color: '#7c4dff' },
  { to: '/accounts', emoji: '◉', label: 'Аккаунты', color: '#3d8bff' },
  { to: '/proxies', emoji: '◎', label: 'Прокси', color: '#00c2b2' },
  { to: '/inbox', emoji: '◆', label: 'Входящие', color: '#ff3d9a' },
  { to: '/commenting', emoji: '◇', label: 'Комментинг', color: '#ff6b35' },
  { to: '/warmup', emoji: '◐', label: 'Прогрев', color: '#3dd68c' },
  { to: '/reactions', emoji: '◑', label: 'Реакции', color: '#ff3d9a' },
  { to: '/leomatch', emoji: '❤', label: 'Leomatch', color: '#ff3d9a' },
  { to: '/parser', emoji: '◍', label: 'Парсер', color: '#3d8bff' },
  { to: '/tasks', emoji: '◌', label: 'Задачи', color: '#e3a13f' },
  { to: '/analytics', emoji: '◈', label: 'Аналитика', color: '#00c2b2' },
  { to: '/api-keys', emoji: '◇', label: 'API ключи', color: '#ff6b35' },
  { to: '/settings', emoji: '◎', label: 'Настройки', color: '#888' },
]

// ── Знак ufo.gram ──────────────────────────────────────────────────
// Тарелка с орбитальным кольцом. Кольцо медленно вращается, корпус чуть
// покачивается — движение фоновое, взгляд не тянет.
// Размеры те же, что у прежней буквы «G»: 32 пикселя со скруглением 8,
// чтобы вёрстка боковой панели не поехала.
function UfoMark({ size = 32 }) {
  return (
    <div
      style={{
        width: size, height: size, borderRadius: size / 4, flexShrink: 0,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        background: 'linear-gradient(135deg, #7c4dff 0%, #4de8ff 100%)',
      }}
      aria-hidden="true"
    >
      <svg viewBox="0 0 32 32" width={size * 0.72} height={size * 0.72}>
        {/* Кольцо орбиты. Вращение эллипса читается как облёт по кругу. */}
        <ellipse
          cx="16" cy="17" rx="13" ry="5"
          fill="none" stroke="#ffffff" strokeWidth="1.6" opacity="0.55"
          style={{
            transformOrigin: '16px 17px',
            animation: 'ufo-orbit 9s linear infinite',
          }}
        />
        {/* Корпус и купол */}
        <g style={{ animation: 'ufo-bob 3.4s ease-in-out infinite' }}>
          <ellipse cx="16" cy="16.5" rx="7.5" ry="2.8" fill="#fff" />
          <path d="M12 15.4a4 4 0 0 1 8 0z" fill="#fff" />
          <circle cx="16" cy="17.4" r="0.9" fill="#7c4dff" />
        </g>
      </svg>
      <style>{`
        @keyframes ufo-orbit { to { transform: rotate(360deg); } }
        @keyframes ufo-bob {
          0%, 100% { transform: translateY(0); }
          50%      { transform: translateY(-0.7px); }
        }
        @media (prefers-reduced-motion: reduce) {
          svg ellipse, svg g { animation: none !important; }
        }
      `}</style>
    </div>
  )
}


export default function Layout({ children }) {
  const { user, logout } = useAuthStore()
  const navigate = useNavigate()

  const handleLogout = async () => {
    await logout()
    navigate('/login')
  }

  return (
    <div style={{ display: 'flex', height: '100vh', overflow: 'hidden' }}>
      {/* SIDEBAR */}
      <aside style={{
        width: 232, flexShrink: 0,
        background: 'var(--bg-2)',
        borderRight: '1px solid var(--border)',
        display: 'flex', flexDirection: 'column',
      }}>
        {/* Logo */}
        <div style={{ padding: '22px 20px 18px', borderBottom: '1px solid var(--border)' }}>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4,
          }}>
            <UfoMark size={32} />
            <div>
              <div style={{
                fontFamily: 'var(--font-sans)', fontSize: 16, fontWeight: 700,
                color: 'var(--text)', letterSpacing: '-0.03em',
              }}>
                {/* Точка посередине — главный приём знака: она делит слово
                    и держит акцентный цвет луча. */}
                ufo<span style={{ color: '#4de8ff' }}>.</span>gram
              </div>
              <div style={{ fontSize: 10, color: 'var(--text-3)', letterSpacing: '0.06em' }}>
                v2.0
              </div>
            </div>
          </div>
        </div>

        {/* Nav */}
        <nav style={{ flex: 1, padding: '10px 10px', display: 'flex', flexDirection: 'column', gap: 2 }}>
          {NAV.map(({ to, emoji, label, color }) => (
            <NavLink key={to} to={to} end={to === '/'} style={({ isActive }) => ({
              display: 'flex', alignItems: 'center', gap: 10,
              padding: '10px 12px', borderRadius: 10,
              color: isActive ? '#fff' : 'var(--text-2)',
              background: isActive ? 'rgba(124,77,255,0.18)' : 'transparent',
              fontSize: 13, fontWeight: isActive ? 600 : 400,
              transition: 'all 0.15s', textDecoration: 'none',
              border: isActive ? '1px solid rgba(124,77,255,0.25)' : '1px solid transparent',
            })}
              onMouseEnter={e => {
                if (!e.currentTarget.style.background.includes('0.18')) {
                  e.currentTarget.style.background = 'rgba(255,255,255,0.04)'
                }
              }}
              onMouseLeave={e => {
                if (!e.currentTarget.style.background.includes('0.18')) {
                  e.currentTarget.style.background = 'transparent'
                }
              }}>
              <span style={{ fontSize: 16, lineHeight: 1, color }}>{emoji}</span>
              {label}
            </NavLink>
          ))}
        </nav>

        {/* User */}
        <div style={{ padding: '12px 10px 16px', borderTop: '1px solid var(--border)' }}>
          <div style={{
            padding: '12px 14px', background: 'rgba(124,77,255,0.08)',
            border: '1px solid rgba(124,77,255,0.15)', borderRadius: 12,
          }}>
            {/* Plan badge */}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
              <span style={{ fontSize: 11, color: 'var(--text-3)' }}>Тариф</span>
              <span style={{
                fontSize: 10, fontWeight: 700, padding: '2px 8px', borderRadius: 4,
                background: 'linear-gradient(135deg, #7c4dff, #3d8bff)',
                color: '#fff', fontFamily: 'var(--font-mono)', letterSpacing: '0.04em',
              }}>{(user?.plan || 'starter').toUpperCase()}</span>
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-2)', marginBottom: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {user?.email}
            </div>
            <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 10 }}>
              Аккаунтов: до {user?.account_limit}
            </div>
            <button onClick={handleLogout} style={{
              fontSize: 11, color: 'var(--red)', background: 'none', border: 'none',
              padding: 0, cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4,
              opacity: 0.7, transition: 'opacity 0.15s',
            }}
              onMouseEnter={e => e.currentTarget.style.opacity = 1}
              onMouseLeave={e => e.currentTarget.style.opacity = 0.7}>
              Выйти →
            </button>
          </div>
        </div>
      </aside>

      {/* MAIN */}
      <main style={{ flex: 1, overflow: 'auto', background: 'var(--bg)' }}>
        {children}
      </main>
    </div>
  )
}