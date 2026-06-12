/**
 * WebScraperPanel — UI для запуска Camoufox-скрейпинга через пул прокси.
 *
 * Вкладка в ParserPage. Принимает URLs + прокси (по строке), запускает
 * Celery-таск, поллит прогресс каждые 2 сек, даёт отменить, и в конце
 * предлагает скачать JSONL с результатами.
 */
import { useEffect, useRef, useState } from 'react'
import { parserAPI } from '../services/api'
import { Button, Card } from '../components/ui'

export default function WebScraperPanel() {
  const [urlsText, setUrlsText] = useState('')
  const [proxiesText, setProxiesText] = useState('')
  const [maxWorkers, setMaxWorkers] = useState(3)
  const [maxRetries, setMaxRetries] = useState(3)
  const [pageTimeout, setPageTimeout] = useState(60)
  const [cooldownMin, setCooldownMin] = useState(900)
  const [cooldownMax, setCooldownMax] = useState(1200)
  const [rotationMin, setRotationMin] = useState(15)
  const [rotationMax, setRotationMax] = useState(35)
  const [humanize, setHumanize] = useState(true)
  const [headless, setHeadless] = useState(true)
  const [showAdvanced, setShowAdvanced] = useState(false)

  const [jobId, setJobId] = useState(null)
  const [progress, setProgress] = useState(null)
  const [starting, setStarting] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [toast, setToast] = useState(null)
  const [jobs, setJobs] = useState([])
  const pollRef = useRef(null)

  // ── helpers ─────────────────────────────────────────────────────

  const parseLines = (text) => text.split(/[\n,]+/).map(s => s.trim()).filter(Boolean)
  const urls = parseLines(urlsText)
  const proxies = parseLines(proxiesText)

  const showToast = (msg, kind = 'ok') => {
    setToast({ msg, kind })
    setTimeout(() => setToast(null), 4000)
  }

  const stopPolling = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }

  const startPolling = (id) => {
    stopPolling()
    pollRef.current = setInterval(async () => {
      try {
        const { data } = await parserAPI.webScrapeProgress(id)
        setProgress(data)
        if (['done', 'cancelled', 'error', 'unknown'].includes(data.status)) {
          stopPolling()
          if (data.status === 'done') loadJobs()
        }
      } catch (e) {
        console.error('progress poll', e)
      }
    }, 2000)
  }

  const loadJobs = async () => {
    try {
      const { data } = await parserAPI.webScrapeListJobs()
      setJobs(data.jobs || [])
    } catch (e) { console.error(e) }
  }

  useEffect(() => {
    loadJobs()
    return () => stopPolling()
  }, [])

  // ── handlers ────────────────────────────────────────────────────

  const handleStart = async () => {
    if (urls.length === 0) {
      showToast('Добавь хотя бы один URL', 'err')
      return
    }
    if (proxies.length < 3) {
      showToast(`Минимум 3 прокси (введено ${proxies.length})`, 'err')
      return
    }
    if (proxies.length < 10) {
      const ok = confirm(
        `Введено только ${proxies.length} прокси. Архитектура рассчитана на 34 узла.\n\n` +
        `С меньшим пулом cooldown будет давить — узлы быстро уйдут в простой.\n\n` +
        `Продолжить?`
      )
      if (!ok) return
    }

    setStarting(true)
    try {
      const { data } = await parserAPI.webScrapeStart({
        urls,
        proxies,
        max_workers: maxWorkers,
        max_retries: maxRetries,
        page_timeout_sec: pageTimeout,
        cooldown_min_sec: cooldownMin,
        cooldown_max_sec: cooldownMax,
        node_rotation_min_sec: rotationMin,
        node_rotation_max_sec: rotationMax,
        humanize,
        headless,
      })
      setJobId(data.job_id)
      setProgress({ status: 'queued', urls_total: data.urls_total })
      startPolling(data.job_id)
      showToast(`Job ${data.job_id.slice(0, 8)} запущен`, 'ok')
    } catch (e) {
      showToast(e.response?.data?.detail || 'Ошибка запуска', 'err')
    } finally {
      setStarting(false)
    }
  }

  const handleStop = async () => {
    if (!jobId) return
    if (!confirm('Остановить скрейпинг? Уже сделанные результаты сохранятся.')) return
    setStopping(true)
    try {
      await parserAPI.webScrapeStop(jobId)
      showToast('Сигнал отмены отправлен, ждём завершения текущих воркеров…', 'ok')
    } catch (e) {
      showToast('Не удалось отменить', 'err')
    } finally {
      setStopping(false)
    }
  }

  const handleDownload = async (id) => {
    try {
      const { data } = await parserAPI.webScrapeResults(id)
      const blob = new Blob([data], { type: 'application/x-ndjson' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `web_scrape_${id.slice(0, 8)}.jsonl`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      showToast('Не удалось скачать JSONL', 'err')
    }
  }

  // ── derived progress numbers ───────────────────────────────────

  const total = progress?.urls_total || progress?.total || urls.length || 0
  const done = progress?.done || 0
  const ok = progress?.ok || 0
  const failed = progress?.failed || 0
  const skipped = progress?.skipped || 0
  const pct = total > 0 ? Math.min(100, Math.round((done + skipped) / total * 100)) : 0

  const rotator = progress?.rotator || {}
  const status = progress?.status || 'idle'
  const isRunning = ['queued', 'starting', 'running'].includes(status)
  const isTerminal = ['done', 'cancelled', 'error'].includes(status)

  const statusColor = {
    queued: 'var(--text-3)',
    starting: 'var(--violet)',
    running: 'var(--violet)',
    done: 'var(--green)',
    cancelled: '#eab308',
    error: 'var(--red)',
  }[status] || 'var(--text-3)'

  const statusEmoji = {
    queued: '⏳', starting: '🚀', running: '🛰️',
    done: '✅', cancelled: '⏹', error: '❌',
  }[status] || '·'

  // ── render ──────────────────────────────────────────────────────

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      {toast && (
        <div style={{
          position: 'fixed', top: 80, right: 20, padding: '10px 18px',
          borderRadius: 10, background: toast.kind === 'err' ? 'var(--red)' : 'var(--green)',
          color: '#fff', fontWeight: 600, fontSize: 13, zIndex: 1000,
          boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
        }}>{toast.msg}</div>
      )}

      {/* Header info */}
      <Card style={{ padding: 16, background: 'var(--bg-2)' }}>
        <div style={{ fontSize: 13, color: 'var(--text-2)', lineHeight: 1.6 }}>
          <strong style={{ color: 'var(--text)' }}>🛰️ Camoufox web-парсер</strong> — отказоустойчивый
          E2E-скрейпинг публичной статистики через пул из 34 статических IPv4.
          Микро-батчинг (3-4 воркера), cooldown узлов на 15-20 мин при 403/429,
          ротация контекстов 15-35 сек на один IP, эмуляция UX (мышь, скроллинг,
          паузы 10-30 сек). Результаты в JSONL с resume без дублирования.
        </div>
      </Card>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        {/* URLs */}
        <Card style={{ padding: 16 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-3)', marginBottom: 8 }}>
            URL для парсинга ({urls.length})
          </div>
          <textarea
            value={urlsText}
            onChange={e => setUrlsText(e.target.value)}
            disabled={isRunning}
            placeholder={'https://example.com/page1\nhttps://example.com/page2\n…'}
            style={{
              width: '100%', minHeight: 200, padding: 10, borderRadius: 8,
              border: '1px solid var(--border)', background: 'var(--bg)',
              color: 'var(--text)', fontFamily: 'monospace', fontSize: 12,
              resize: 'vertical',
            }}
          />
        </Card>

        {/* Proxies */}
        <Card style={{ padding: 16 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-3)', marginBottom: 8, display: 'flex', justifyContent: 'space-between' }}>
            <span>Прокси-узлы ({proxies.length})</span>
            <span style={{ color: proxies.length >= 34 ? 'var(--green)' : proxies.length >= 10 ? '#eab308' : 'var(--red)' }}>
              {proxies.length >= 34 ? '✓ оптимально' : proxies.length >= 10 ? '⚠ мало для cooldown' : '⚠ риск блока'}
            </span>
          </div>
          <textarea
            value={proxiesText}
            onChange={e => setProxiesText(e.target.value)}
            disabled={isRunning}
            placeholder={'http://user:pass@1.2.3.4:8080\nsocks5://user:pass@1.2.3.5:1080\n…'}
            style={{
              width: '100%', minHeight: 200, padding: 10, borderRadius: 8,
              border: '1px solid var(--border)', background: 'var(--bg)',
              color: 'var(--text)', fontFamily: 'monospace', fontSize: 12,
              resize: 'vertical',
            }}
          />
          <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 6 }}>
            Поддержка: http / https / socks5 · одна строка = один прокси
          </div>
        </Card>
      </div>

      {/* Advanced options */}
      <Card style={{ padding: 16 }}>
        <button
          onClick={() => setShowAdvanced(v => !v)}
          style={{
            background: 'none', border: 'none', color: 'var(--text-2)',
            fontSize: 13, fontWeight: 600, cursor: 'pointer', padding: 0,
          }}
        >
          {showAdvanced ? '▼' : '▶'} Расширенные настройки
        </button>
        {showAdvanced && (
          <div style={{ marginTop: 14, display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12 }}>
            <NumField label="Воркеров (3-4)" value={maxWorkers} onChange={setMaxWorkers} min={1} max={6} />
            <NumField label="Ретраев на URL" value={maxRetries} onChange={setMaxRetries} min={1} max={5} />
            <NumField label="Timeout страницы (сек)" value={pageTimeout} onChange={setPageTimeout} min={10} max={300} />
            <NumField label="Cooldown min (сек)" value={cooldownMin} onChange={setCooldownMin} min={60} max={3600} />
            <NumField label="Cooldown max (сек)" value={cooldownMax} onChange={setCooldownMax} min={60} max={7200} />
            <NumField label="Пауза ротации min (сек)" value={rotationMin} onChange={setRotationMin} min={1} max={120} />
            <NumField label="Пауза ротации max (сек)" value={rotationMax} onChange={setRotationMax} min={1} max={180} />
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600 }}>Camoufox</label>
              <label style={{ fontSize: 12, color: 'var(--text-2)', display: 'flex', gap: 6, alignItems: 'center' }}>
                <input type="checkbox" checked={humanize} onChange={e => setHumanize(e.target.checked)} /> humanize
              </label>
              <label style={{ fontSize: 12, color: 'var(--text-2)', display: 'flex', gap: 6, alignItems: 'center' }}>
                <input type="checkbox" checked={headless} onChange={e => setHeadless(e.target.checked)} /> headless
              </label>
            </div>
          </div>
        )}
      </Card>

      {/* Action row */}
      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        {!isRunning ? (
          <Button variant="primary" onClick={handleStart} disabled={starting || urls.length === 0 || proxies.length < 3}>
            {starting ? '⏳ Запуск...' : '🚀 Запустить скрейпинг'}
          </Button>
        ) : (
          <Button variant="danger" onClick={handleStop} disabled={stopping}>
            {stopping ? '⏳ Останавливаю...' : '⏹ Остановить'}
          </Button>
        )}
        {jobId && (
          <div style={{ fontSize: 12, color: 'var(--text-3)', fontFamily: 'monospace' }}>
            job: {jobId.slice(0, 8)}…
          </div>
        )}
      </div>

      {/* Live progress panel */}
      {progress && (
        <Card style={{ padding: 18, border: `2px solid ${statusColor}` }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
            <div style={{ fontSize: 15, fontWeight: 700, color: statusColor }}>
              {statusEmoji} {labelForStatus(status)}
            </div>
            <div style={{ fontSize: 13, color: 'var(--text-2)' }}>
              {done + skipped} / {total} · {pct}%
            </div>
          </div>

          {/* Progress bar */}
          <div style={{ height: 8, borderRadius: 4, background: 'var(--bg-2)', overflow: 'hidden', marginBottom: 12 }}>
            <div style={{
              height: '100%', width: `${pct}%`, background: statusColor,
              transition: 'width 0.4s ease',
            }} />
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 8, marginBottom: 12 }}>
            <SmallStat label="✅ OK" value={ok} color="var(--green)" />
            <SmallStat label="❌ Fail" value={failed} color="var(--red)" />
            <SmallStat label="⏭ Skip" value={skipped} color="var(--text-3)" />
            <SmallStat label="🌐 Узлов" value={`${rotator.available || 0}/${rotator.total || 0}`} color="var(--text-2)" />
            <SmallStat label="❄ В cooldown" value={rotator.in_cooldown || 0} color="#eab308" />
          </div>

          {progress.current_url && (
            <div style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              📞 {progress.current_url}
            </div>
          )}

          {progress.error && (
            <div style={{ marginTop: 10, padding: 10, borderRadius: 6, background: 'rgba(239, 68, 68, 0.1)', color: 'var(--red)', fontSize: 12, fontFamily: 'monospace' }}>
              {progress.error}
            </div>
          )}

          {isTerminal && jobId && (
            <div style={{ marginTop: 12 }}>
              <Button variant="primary" onClick={() => handleDownload(jobId)}>
                📥 Скачать JSONL ({progress.ok || 0} записей)
              </Button>
            </div>
          )}
        </Card>
      )}

      {/* Job history */}
      <Card style={{ padding: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-2)' }}>История запусков</div>
          <button onClick={loadJobs} style={{ background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer', fontSize: 12 }}>↻ Обновить</button>
        </div>
        {jobs.length === 0 ? (
          <div style={{ fontSize: 12, color: 'var(--text-3)' }}>Пока нет завершённых job'ов</div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {jobs.map(j => (
              <div key={j.job_id} style={{
                display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                padding: '8px 12px', borderRadius: 6, background: 'var(--bg-2)',
              }}>
                <div style={{ fontFamily: 'monospace', fontSize: 12, color: 'var(--text-2)' }}>
                  {j.job_id.slice(0, 12)}…
                </div>
                <div style={{ fontSize: 12, color: 'var(--text-3)' }}>
                  {j.records} записей · {(j.size_bytes / 1024).toFixed(1)} KB
                </div>
                <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
                  {new Date(j.modified_at * 1000).toLocaleString('ru-RU')}
                </div>
                <button
                  onClick={() => handleDownload(j.job_id)}
                  style={{
                    background: 'var(--violet)', color: '#fff', border: 'none',
                    padding: '4px 10px', borderRadius: 4, fontSize: 11, cursor: 'pointer',
                  }}
                >📥</button>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}

function NumField({ label, value, onChange, min, max }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600 }}>{label}</label>
      <input
        type="number"
        value={value}
        min={min}
        max={max}
        onChange={e => onChange(Number(e.target.value))}
        style={{
          padding: '6px 8px', borderRadius: 6, border: '1px solid var(--border)',
          background: 'var(--bg)', color: 'var(--text)', fontSize: 12, width: '100%',
        }}
      />
    </div>
  )
}

function SmallStat({ label, value, color }) {
  return (
    <div style={{
      padding: 8, borderRadius: 6, background: 'var(--bg-2)',
      display: 'flex', flexDirection: 'column', gap: 2,
    }}>
      <div style={{ fontSize: 10, color: 'var(--text-3)' }}>{label}</div>
      <div style={{ fontSize: 16, fontWeight: 700, color: color || 'var(--text)' }}>{value}</div>
    </div>
  )
}

function labelForStatus(s) {
  return {
    queued: 'В очереди',
    starting: 'Запуск Camoufox…',
    running: 'Работает',
    done: 'Готово',
    cancelled: 'Отменено',
    error: 'Ошибка',
    unknown: 'Статус неизвестен',
    idle: 'Ожидание',
  }[s] || s
}
