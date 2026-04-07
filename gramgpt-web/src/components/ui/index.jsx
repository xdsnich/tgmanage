// JetBrains-style UI components

export function Button({ children, variant = 'primary', size = 'md', loading, disabled, onClick, type = 'button', style }) {
  const base = {
    display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
    gap: 8, fontWeight: 600, border: 'none',
    cursor: disabled || loading ? 'not-allowed' : 'pointer',
    transition: 'all 0.2s cubic-bezier(0.16,1,0.3,1)',
    fontFamily: 'var(--font-sans)', letterSpacing: '-0.01em',
    opacity: disabled || loading ? 0.5 : 1, position: 'relative', overflow: 'hidden',
  }
  const sizes = {
    sm: { padding: '7px 14px', fontSize: 12, borderRadius: 8 },
    md: { padding: '10px 20px', fontSize: 14, borderRadius: 10 },
    lg: { padding: '14px 28px', fontSize: 15, borderRadius: 12 },
  }
  const variants = {
    primary: { background: 'linear-gradient(135deg, #7c4dff 0%, #3d8bff 100%)', color: '#fff', boxShadow: '0 4px 20px rgba(124,77,255,0.35)' },
    pink: { background: 'linear-gradient(135deg, #ff3d9a 0%, #7c4dff 100%)', color: '#fff', boxShadow: '0 4px 20px rgba(255,61,154,0.3)' },
    ghost: { background: 'transparent', color: 'var(--text-2)', border: '1px solid var(--border)' },
    danger: { background: 'var(--red-dim)', color: 'var(--red)', border: '1px solid rgba(248,81,73,0.25)' },
    outline: { background: 'transparent', color: 'var(--violet)', border: '1px solid rgba(124,77,255,0.4)' },
  }
  const handleMouseEnter = (e) => {
    if (disabled || loading) return
    if (variant === 'primary' || variant === 'pink') e.currentTarget.style.transform = 'translateY(-1px)'
    if (variant === 'ghost') e.currentTarget.style.borderColor = 'var(--border-2)'
    if (variant === 'outline') e.currentTarget.style.background = 'rgba(124,77,255,0.1)'
  }
  const handleMouseLeave = (e) => {
    e.currentTarget.style.transform = 'translateY(0)'
    if (variant === 'ghost') e.currentTarget.style.borderColor = 'var(--border)'
    if (variant === 'outline') e.currentTarget.style.background = 'transparent'
  }
  return (
    <button type={type} onClick={onClick} disabled={disabled || loading}
      onMouseEnter={handleMouseEnter} onMouseLeave={handleMouseLeave}
      style={{ ...base, ...sizes[size], ...variants[variant], ...style }}>
      {loading && <Spinner size={14} color="#fff" />}
      {children}
    </button>
  )
}

export function Badge({ children, color = 'default' }) {
  const colors = {
    default: { bg: 'rgba(255,255,255,0.06)', color: 'var(--text-2)', border: 'rgba(255,255,255,0.08)' },
    green: { bg: 'var(--green-dim)', color: 'var(--green)', border: 'rgba(61,214,140,0.25)' },
    red: { bg: 'var(--red-dim)', color: 'var(--red)', border: 'rgba(248,81,73,0.25)' },
    yellow: { bg: 'var(--yellow-dim)', color: 'var(--yellow)', border: 'rgba(227,161,63,0.25)' },
    violet: { bg: 'var(--violet-dim)', color: 'var(--violet)', border: 'rgba(124,77,255,0.25)' },
    blue: { bg: 'var(--blue-dim)', color: 'var(--blue)', border: 'rgba(61,139,255,0.25)' },
    pink: { bg: 'var(--pink-dim)', color: 'var(--pink)', border: 'rgba(255,61,154,0.25)' },
    teal: { bg: 'var(--teal-dim)', color: 'var(--teal)', border: 'rgba(0,194,178,0.25)' },
  }
  const c = colors[color] || colors.default
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center',
      padding: '3px 10px', borderRadius: 6, fontSize: 11, fontWeight: 600,
      letterSpacing: '0.02em', fontFamily: 'var(--font-mono)',
      background: c.bg, color: c.color, border: `1px solid ${c.border}`,
    }}>{children}</span>
  )
}

export function Card({ children, onClick, style, gradient }) {
  const handleEnter = (e) => {
    if (!onClick) return
    e.currentTarget.style.borderColor = 'rgba(124,77,255,0.35)'
    e.currentTarget.style.transform = 'translateY(-2px)'
    e.currentTarget.style.boxShadow = '0 8px 30px rgba(124,77,255,0.12)'
  }
  const handleLeave = (e) => {
    e.currentTarget.style.borderColor = 'var(--border)'
    e.currentTarget.style.transform = 'translateY(0)'
    e.currentTarget.style.boxShadow = 'none'
  }
  return (
    <div onClick={onClick} onMouseEnter={handleEnter} onMouseLeave={handleLeave} style={{
      background: gradient ? 'linear-gradient(145deg, #2d1b4e 0%, #1a1025 100%)' : 'var(--bg-2)',
      border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: 20,
      transition: 'border-color 0.2s, transform 0.2s, box-shadow 0.2s',
      cursor: onClick ? 'pointer' : 'default', ...style,
    }}>{children}</div>
  )
}

export function Input({ label, error, ...props }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {label && <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase' }}>{label}</label>}
      <input
        style={{
          background: 'var(--bg-3)', border: `1px solid ${error ? 'var(--red)' : 'var(--border)'}`,
          borderRadius: 'var(--radius-sm)', color: 'var(--text)',
          padding: '10px 14px', fontSize: 14, outline: 'none', width: '100%',
          transition: 'border-color 0.15s, box-shadow 0.15s',
        }}
        onFocus={e => { e.target.style.borderColor = 'var(--violet)'; e.target.style.boxShadow = '0 0 0 3px rgba(124,77,255,0.15)' }}
        onBlur={e => { e.target.style.borderColor = error ? 'var(--red)' : 'var(--border)'; e.target.style.boxShadow = 'none' }}
        {...props}
      />
      {error && <span style={{ fontSize: 12, color: 'var(--red)' }}>{error}</span>}
    </div>
  )
}

export function Spinner({ size = 20, color = 'var(--violet)' }) {
  return (
    <div style={{
      width: size, height: size, flexShrink: 0,
      border: `2px solid ${color}33`, borderTopColor: color,
      borderRadius: '50%', animation: 'spin 0.65s linear infinite',
    }} />
  )
}

export function TrustBar({ score }) {
  const bar = score >= 70
    ? 'linear-gradient(90deg,#3dd68c,#00c2b2)'
    : score >= 40
      ? 'linear-gradient(90deg,#e3a13f,#ff6b35)'
      : 'linear-gradient(90deg,#f85149,#ff3d9a)'
  const color = score >= 70 ? 'var(--green)' : score >= 40 ? 'var(--yellow)' : 'var(--red)'
  const label = score >= 80 ? 'Отлично' : score >= 60 ? 'Хорошо' : score >= 40 ? 'Средне' : 'Слабо'
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <div style={{ flex: 1, height: 3, background: 'rgba(255,255,255,0.06)', borderRadius: 2, overflow: 'hidden' }}>
        <div style={{ width: `${score}%`, height: '100%', background: bar, borderRadius: 2, transition: 'width 0.6s cubic-bezier(0.16,1,0.3,1)' }} />
      </div>
      <span style={{ fontSize: 11, color, fontFamily: 'var(--font-mono)', minWidth: 24, textAlign: 'right' }}>{score}</span>
      <span style={{ fontSize: 11, color: 'var(--text-3)', minWidth: 40 }}>{label}</span>
    </div>
  )
}

export function StatusBadge({ status }) {
  const map = {
    active: { label: '● Живой', color: 'green' },
    spamblock: { label: '● Спамблок', color: 'red' },
    frozen: { label: '● Заморожен', color: 'yellow' },
    quarantine: { label: '● Карантин', color: 'pink' },
    error: { label: '● Ошибка', color: 'red' },
    unknown: { label: '● Неизвестно', color: 'default' },
  }
  const info = map[status] || map.unknown
  return <Badge color={info.color}>{info.label}</Badge>
}

export function Empty({ icon, title, subtitle, action }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '72px 24px', gap: 12, textAlign: 'center' }}>
      {icon && <div style={{ fontSize: 44, marginBottom: 4 }}>{icon}</div>}
      <div style={{ fontSize: 17, fontWeight: 700, color: 'var(--text)', letterSpacing: '-0.02em' }}>{title}</div>
      {subtitle && <div style={{ fontSize: 13, color: 'var(--text-3)', maxWidth: 300, lineHeight: 1.6 }}>{subtitle}</div>}
      {action && <div style={{ marginTop: 12 }}>{action}</div>}
    </div>
  )
}

export function Modal({ open = true, onClose, title, children, width = 480 }) {
  if (!open) return null
  return (
    <div onClick={onClose} style={{
      position: 'fixed', inset: 0, zIndex: 200,
      background: 'rgba(0,0,0,0.8)', backdropFilter: 'blur(8px)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      padding: 24, animation: 'fadeIn 0.15s ease',
    }}>
      <div onClick={e => e.stopPropagation()} style={{
        background: 'var(--bg-2)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius)', width: '100%', maxWidth: width,
        animation: 'fadeUp 0.25s cubic-bezier(0.16,1,0.3,1)',
        boxShadow: '0 24px 80px rgba(0,0,0,0.7)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '18px 24px', borderBottom: '1px solid var(--border)' }}>
          <h3 style={{ fontSize: 15, fontWeight: 700, letterSpacing: '-0.02em' }}>{title}</h3>
          <button onClick={onClose} style={{
            background: 'rgba(255,255,255,0.06)', border: 'none', color: 'var(--text-3)',
            width: 28, height: 28, borderRadius: 6, cursor: 'pointer', fontSize: 14,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}>✕</button>
        </div>
        <div style={{ padding: 24 }}>{children}</div>
      </div>
    </div>
  )
}

export function StatCard({ label, value, color, icon }) {
  return (
    <div style={{
      background: 'var(--bg-2)', border: '1px solid var(--border)',
      borderRadius: 'var(--radius)', padding: '20px 22px',
      transition: 'transform 0.2s, box-shadow 0.2s, border-color 0.2s',
    }}
      onMouseEnter={e => { e.currentTarget.style.transform = 'translateY(-2px)'; e.currentTarget.style.borderColor = 'rgba(124,77,255,0.3)'; e.currentTarget.style.boxShadow = '0 8px 30px rgba(0,0,0,0.3)' }}
      onMouseLeave={e => { e.currentTarget.style.transform = 'translateY(0)'; e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.boxShadow = 'none' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
        <span style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase' }}>{label}</span>
        {icon && <span style={{ fontSize: 18 }}>{icon}</span>}
      </div>
      <div style={{ fontSize: 34, fontWeight: 800, letterSpacing: '-0.04em', color: color || 'var(--text)' }}>{value}</div>
    </div>
  )
}

export function Divider({ label }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 12, margin: '4px 0' }}>
      <div style={{ flex: 1, height: 1, background: 'var(--border)' }} />
      {label && <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{label}</span>}
      <div style={{ flex: 1, height: 1, background: 'var(--border)' }} />
    </div>
  )
}
