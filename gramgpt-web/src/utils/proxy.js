/**
 * Утилиты отображения прокси в селектах/списках.
 */

/**
 * country_code ("US", "DE", "UA"...) → emoji-флаг.
 * Использует Unicode Regional Indicator Symbols — поддерживается всеми
 * современными браузерами, JSX и шрифтами по умолчанию.
 * Возвращает пустую строку если код пустой или не 2-буквенный.
 */
export function countryFlag(code) {
  if (!code || typeof code !== 'string') return ''
  const cc = code.trim().toUpperCase()
  if (cc.length !== 2 || !/^[A-Z]{2}$/.test(cc)) return ''
  const A = 0x1F1E6
  const a = 'A'.charCodeAt(0)
  return String.fromCodePoint(A + cc.charCodeAt(0) - a, A + cc.charCodeAt(1) - a)
}

/**
 * Подпись для прокси в селекторе:
 *   🇺🇸 US · 1.2.3.4:1080 (socks5) ✅ · 👤 5
 *   ── флаг + код страны (если есть)
 *   ── host:port (protocol)
 *   ── ✅ / ❌ если showValid (по умолчанию)
 *   ── 👤 N — сколько аккаунтов уже сидит на этом прокси
 *
 * Опции:
 *   showValid: показывать ли ✅/❌ значок (default true)
 *   showCount: показывать ли счётчик 👤 (default true; нужен accounts_count в объекте прокси)
 */
export function proxyLabel(p, { showValid = true, showCount = true } = {}) {
  if (!p) return ''
  const flag = countryFlag(p.country_code)
  const head = flag ? `${flag} ${p.country_code.toUpperCase()} · ` : ''
  const validMark = showValid
    ? (p.is_valid === true ? ' ✅' : p.is_valid === false ? ' ❌' : '')
    : ''
  // 👤 N — даже если N = 0, юзеру полезно видеть «никто ещё не использует»
  const countMark = showCount && typeof p.accounts_count === 'number'
    ? ` · 👤 ${p.accounts_count}`
    : ''
  return `${head}${p.host}:${p.port} (${p.protocol})${validMark}${countMark}`
}
