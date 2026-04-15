import { useEffect, useRef } from 'react'

export function useAutoRefresh(fetchFn, intervalMs = 15000, enabled = true) {
    const ref = useRef(fetchFn)
    ref.current = fetchFn

    useEffect(() => {
        if (!enabled) return
        const iv = setInterval(() => {
            try { ref.current() } catch { }
        }, intervalMs)
        return () => clearInterval(iv)
    }, [intervalMs, enabled])
}