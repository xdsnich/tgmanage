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
 *   🇺🇸 US · 1.2.3.4:1080 (socks5) ✅
 * Если страны нет — флаг и код опускаются, остаётся как было.
 */
export function proxyLabel(p, { showValid = true } = {}) {
  if (!p) return ''
  const flag = countryFlag(p.country_code)
  const head = flag ? `${flag} ${p.country_code.toUpperCase()} · ` : ''
  const validMark = showValid
    ? (p.is_valid === true ? ' ✅' : p.is_valid === false ? ' ❌' : '')
    : ''
  return `${head}${p.host}:${p.port} (${p.protocol})${validMark}`
}
