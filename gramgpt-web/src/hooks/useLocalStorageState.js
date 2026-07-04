import { useState, useEffect } from 'react'

/**
 * useLocalStorageState — как useState, но значение живёт в localStorage.
 * При маунте читает сохранённое, при setValue пишет обратно.
 *
 * Использование:
 *   const [tab, setTab] = useLocalStorageState('parser_active_tab', 'channels')
 *
 * Ошибки чтения/записи проглатываются (Private Mode, quota, etc.) —
 * компонент останется рабочим и без persist.
 */
export default function useLocalStorageState(key, defaultValue) {
  const [value, setValue] = useState(() => {
    try {
      const raw = localStorage.getItem(key)
      if (raw === null) return defaultValue
      // Обратная совместимость: старые ключи писались как plain string,
      // не JSON. Пробуем JSON.parse, при неудаче возвращаем raw как есть.
      try { return JSON.parse(raw) } catch { return raw }
    } catch {
      return defaultValue
    }
  })

  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(value))
    } catch {}
  }, [key, value])

  return [value, setValue]
}
