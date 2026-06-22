/**
 * WebScraperPanel — UI для Camoufox-скрейпинга.
 *
 * Два режима:
 *   1. tgstat (по умолчанию): выбираешь страны/категории + проксей,
 *      бэк сам строит URL'ы и парсит карточки каналов с детектом
 *      has_comments. Поверх — агрегация и таблица "вот что нашли".
 *   2. universal: ручной список URL + универсальный extractor
 *      (title/meta/h1/body[:5000]).
 */
import { useEffect, useRef, useState } from 'react'
import { parserAPI } from '../services/api'
import { Button, Card } from '../components/ui'

export default function WebScraperPanel() {
  // ── Общие настройки ──────────────────────────────────────────────
  const [mode, setMode] = useState('tgstat')         // 'tgstat' | 'universal'
  const [proxiesText, setProxiesText] = useState('')
  const [showAdvanced, setShowAdvanced] = useState(false)

  // Параметры скрейпера
  const [maxWorkers, setMaxWorkers] = useState(3)
  const [maxRetries, setMaxRetries] = useState(3)
  const [pageTimeout, setPageTimeout] = useState(60)
  const [cooldownMin, setCooldownMin] = useState(900)
  const [cooldownMax, setCooldownMax] = useState(1200)
  const [rotationMin, setRotationMin] = useState(15)
  const [rotationMax, setRotationMax] = useState(35)
  const [humanize, setHumanize] = useState(true)
  const [headless, setHeadless] = useState(true)

  // TGStat режим
  const [tgstatOptions, setTgstatOptions] = useState({ languages: [], categories: [] })
  const [selectedLangs, setSelectedLangs] = useState(['ru', 'uk', 'en'])
  const [selectedCats, setSelectedCats] = useState([])
  const [onlyWithComments, setOnlyWithComments] = useState(true)
  const [pagesPerGeo, setPagesPerGeo] = useState(1)
  const [includeGlobal, setIncludeGlobal] = useState(false)
  const [aggregated, setAggregated] = useState(null)
  const [aggregateMinSubs, setAggregateMinSubs] = useState(1000)
  const [aggregating, setAggregating] = useState(false)

  // Universal режим
  const [urlsText, setUrlsText] = useState('')

  // Job state
  const [jobId, setJobId] = useState(null)
  const [progress, setProgress] = useState(null)
  const [starting, setStarting] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [toast, setToast] = useState(null)
  const [jobs, setJobs] = useState([])
  const pollRef = useRef(null)

  // ── helpers ─────────────────────────────────────────────────────

  const parseLines = (text) => text.split(/[\n,]+/).map(s => s.trim()).filter(Boolean)
  const proxies = parseLines(proxiesText)
  const urls = parseLines(urlsText)

  const showToast = (msg, kind = 'ok') => {
    setToast({ msg, kind })
    setTimeout(() => setToast(null), 4000)
  }

  const stopPolling = () => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
  }

  const startPolling = (id) => {
    stopPolling()
    pollRef.current = setInterval(async () => {
      try {
        const { data } = await parserAPI.webScrapeProgress(id)
        setProgress(data)
        if (['done', 'cancelled', 'error', 'unknown'].includes(data.status)) {
          stopPolling()
          if (data.status === 'done') {
            loadJobs()
            // авто-агрегация для TGStat-jobs
            if (mode === 'tgstat') loadAggregate(id)
          }
        }
      } catch (e) { console.error('progress poll', e) }
    }, 2000)
  }

  const loadJobs = async () => {
    try {
      const { data } = await parserAPI.webScrapeListJobs()
      setJobs(data.jobs || [])
    } catch (e) { console.error(e) }
  }

  const loadTgstatOptions = async () => {
    try {
      const { data } = await parserAPI.webScrapeTgstatOptions()
      setTgstatOptions(data)
    } catch (e) { console.error(e) }
  }

  const loadAggregate = async (id, minSubs = aggregateMinSubs) => {
    setAggregating(true)
    try {
      const { data } = await parserAPI.webScrapeTgstatAggregate(id, onlyWithComments, minSubs)
      setAggregated(data)
    } catch (e) {
      console.error(e)
      showToast('Агрегация не удалась', 'err')
    } finally {
      setAggregating(false)
    }
  }

  useEffect(() => {
    loadJobs()
    loadTgstatOptions()
    return () => stopPolling()
  }, [])

  // Сколько URL будет сгенерировано в режиме TGStat — для preview
  const tgstatUrlsCount = (() => {
    const langs = selectedLangs.length || (includeGlobal ? 1 : 0)
    const cats = selectedCats.length || 1
    return langs * cats * Math.max(1, pagesPerGeo)
  })()

  // ── handlers ────────────────────────────────────────────────────

  const handleStart = async () => {
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
    setAggregated(null)
    try {
      let resp
      if (mode === 'tgstat') {
        if (selectedLangs.length === 0 && selectedCats.length === 0 && !includeGlobal) {
          showToast('Выбери хотя бы одну страну, категорию или вкл. глобальный', 'err')
          setStarting(false)
          return
        }
        resp = await parserAPI.webScrapeTgstatStart({
          proxies,
          languages: selectedLangs,
          categories: selectedCats,
          only_with_comments: onlyWithComments,
          pages_per_geo: pagesPerGeo,
          include_global: includeGlobal,
          max_workers: maxWorkers,
          max_retries: maxRetries,
          page_timeout_sec: pageTimeout,
          cooldown_min_sec: cooldownMin,
          cooldown_max_sec: cooldownMax,
          node_rotation_min_sec: rotationMin,
          node_rotation_max_sec: rotationMax,
          humanize, headless,
        })
      } else {
        if (urls.length === 0) {
          showToast('Добавь хотя бы один URL', 'err')
          setStarting(false)
          return
        }
        resp = await parserAPI.webScrapeStart({
          urls, proxies,
          max_workers: maxWorkers,
          max_retries: maxRetries,
          page_timeout_sec: pageTimeout,
          cooldown_min_sec: cooldownMin,
          cooldown_max_sec: cooldownMax,
          node_rotation_min_sec: rotationMin,
          node_rotation_max_sec: rotationMax,
          humanize, headless,
        })
      }
      const { data } = resp
      setJobId(data.job_id)
      setProgress({ status: 'queued', urls_total: data.urls_total, preview_urls: data.preview_urls })
      startPolling(data.job_id)
      showToast(`Job ${data.job_id.slice(0, 8)} запущен — ${data.urls_total} URL`, 'ok')
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
      showToast('Сигнал отмены отправлен…', 'ok')
    } catch (e) { showToast('Не удалось отменить', 'err') }
    finally { setStopping(false) }
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
    } catch (e) { showToast('Не удалось скачать JSONL', 'err') }
  }

  const handleDownloadCSV = () => {
    if (!aggregated?.channels?.length) return
    const headers = ['username', 'title', 'subscribers', 'has_comments', 'category', 'verified', 'language']
    const rows = aggregated.channels.map(c =>
      headers.map(h => {
        const v = c[h]
        if (v == null) return ''
        const s = String(v).replace(/"/g, '""')
        return /[",\n]/.test(s) ? `"${s}"` : s
      }).join(',')
    )
    const csv = [headers.join(','), ...rows].join('\n')
    const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `tgstat_channels_${jobId?.slice(0, 8) || 'export'}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  const toggle = (arr, setter) => (code) => {
    setter(arr.includes(code) ? arr.filter(x => x !== code) : [...arr, code])
  }

  // ── derived progress ───────────────────────────────────────────

  const total = progress?.urls_total || progress?.total || 0
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
    queued: 'var(--text-3)', starting: 'var(--violet)', running: 'var(--violet)',
    done: 'var(--green)', cancelled: '#eab308', error: 'var(--red)',
  }[status] || 'var(--text-3)'
  const statusEmoji = {
    queued: '⏳', starting: '🚀', running: '🛰️',
    done: '✅', cancelled: '⏹', error: '❌',
  }[status] || '·'

  // ── render ──────────────────────────────────────────────────────

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {toast && (
        <div style={{
          position: 'fixed', top: 80, right: 20, padding: '10px 18px',
          borderRadius: 10, background: toast.kind === 'err' ? 'var(--red)' : 'var(--green)',
          color: '#fff', fontWeight: 600, fontSize: 13, zIndex: 1000,
          boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
        }}>{toast.msg}</div>
      )}

      {/* Header info */}
      <Card style={{ padding: 14, background: 'var(--bg-2)' }}>
        <div style={{ fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>
          <strong style={{ color: 'var(--text)' }}>🛰️ Camoufox web-парсер.</strong>{' '}
          Пул из 34 статических IPv4 + cooldown 15-20 мин на 403/429 +
          ротация контекстов 15-35 сек на IP + эмуляция UX. JSONL с resume.
        </div>
      </Card>

      {/* Mode switcher */}
      <Card style={{ padding: 12 }}>
        <div style={{ display: 'flex', gap: 6, padding: 4, background: 'var(--bg-2)', borderRadius: 10 }}>
          {[
            { key: 'tgstat', label: '🌐 TGStat (рейтинг по гео)' },
            { key: 'universal', label: '📄 Универсальный (любые URL)' },
          ].map(m => (
            <button
              key={m.key}
              onClick={() => setMode(m.key)}
              disabled={isRunning}
              style={{
                flex: 1, padding: '9px 14px', borderRadius: 7, border: 'none',
                cursor: isRunning ? 'not-allowed' : 'pointer',
                background: mode === m.key ? 'var(--bg-card)' : 'transparent',
                color: mode === m.key ? 'var(--text)' : 'var(--text-3)',
                fontSize: 12, fontWeight: mode === m.key ? 700 : 500,
                boxShadow: mode === m.key ? '0 1px 3px rgba(0,0,0,0.2)' : 'none',
              }}
            >{m.label}</button>
          ))}
        </div>
      </Card>

      {/* TGStat mode controls */}
      {mode === 'tgstat' && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
          <Card style={{ padding: 14 }}>
            <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--text-2)', marginBottom: 8 }}>
              🌍 Страны / языки ({selectedLangs.length})
              <button
                onClick={() => setSelectedLangs([])}
                style={{ float: 'right', background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer', fontSize: 11 }}
              >Очистить</button>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 4, maxHeight: 260, overflowY: 'auto' }}>
              {tgstatOptions.languages.map(l => {
                const on = selectedLangs.includes(l.code)
                return (
                  <button
                    key={l.code}
                    onClick={() => toggle(selectedLangs, setSelectedLangs)(l.code)}
                    disabled={isRunning}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 6,
                      padding: '6px 9px', borderRadius: 6,
                      border: `1px solid ${on ? 'var(--violet)' : 'var(--border)'}`,
                      background: on ? 'rgba(139, 92, 246, 0.12)' : 'var(--bg)',
                      color: on ? 'var(--text)' : 'var(--text-2)',
                      cursor: isRunning ? 'not-allowed' : 'pointer',
                      fontSize: 11, textAlign: 'left',
                    }}
                  >
                    <span>{l.flag}</span>
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {l.label}
                    </span>
                    <code style={{ fontSize: 10, opacity: 0.6 }}>{l.code}</code>
                  </button>
                )
              })}
            </div>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 8, fontSize: 11, color: 'var(--text-2)' }}>
              <input type="checkbox" checked={includeGlobal} disabled={isRunning}
                onChange={e => setIncludeGlobal(e.target.checked)} />
              + Глобальный рейтинг (без языка)
            </label>
          </Card>

          <Card style={{ padding: 14 }}>
            <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--text-2)', marginBottom: 8 }}>
              📂 Категории ({selectedCats.length || 'все'})
              <button
                onClick={() => setSelectedCats([])}
                style={{ float: 'right', background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer', fontSize: 11 }}
              >Очистить</button>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 4, maxHeight: 260, overflowY: 'auto' }}>
              {tgstatOptions.categories.map(c => {
                const on = selectedCats.includes(c.code)
                return (
                  <button
                    key={c.code}
                    onClick={() => toggle(selectedCats, setSelectedCats)(c.code)}
                    disabled={isRunning}
                    style={{
                      padding: '6px 9px', borderRadius: 6,
                      border: `1px solid ${on ? 'var(--violet)' : 'var(--border)'}`,
                      background: on ? 'rgba(139, 92, 246, 0.12)' : 'var(--bg)',
                      color: on ? 'var(--text)' : 'var(--text-2)',
                      cursor: isRunning ? 'not-allowed' : 'pointer',
                      fontSize: 11, textAlign: 'left',
                    }}
                  >{c.label}</button>
                )
              })}
            </div>
            <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 6 }}>
              Пусто = все категории на странице рейтинга
            </div>
          </Card>
        </div>
      )}

      {/* TGStat filters */}
      {mode === 'tgstat' && (
        <Card style={{ padding: 14 }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12 }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: 'var(--text-2)' }}>
              <input type="checkbox" checked={onlyWithComments} disabled={isRunning}
                onChange={e => setOnlyWithComments(e.target.checked)} />
              💬 Только с комментариями (sort=discussions)
            </label>
            <NumField label="Страниц на срез" value={pagesPerGeo} onChange={setPagesPerGeo} min={1} max={10} disabled={isRunning} />
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', fontSize: 12, color: 'var(--text-3)' }}>
              <span style={{ marginRight: 6 }}>Будет URL'ов:</span>
              <strong style={{ color: 'var(--text)', fontSize: 14 }}>≈ {tgstatUrlsCount}</strong>
            </div>
          </div>
        </Card>
      )}

      {/* Universal: URL textarea */}
      {mode === 'universal' && (
        <Card style={{ padding: 14 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-3)', marginBottom: 8 }}>
            URL для парсинга ({urls.length})
          </div>
          <textarea
            value={urlsText}
            onChange={e => setUrlsText(e.target.value)}
            disabled={isRunning}
            placeholder={'https://example.com/page1\nhttps://example.com/page2\n…'}
            style={{
              width: '100%', minHeight: 180, padding: 10, borderRadius: 8,
              border: '1px solid var(--border)', background: 'var(--bg)',
              color: 'var(--text)', fontFamily: 'monospace', fontSize: 12,
              resize: 'vertical',
            }}
          />
        </Card>
      )}

      {/* Proxies */}
      <Card style={{ padding: 14 }}>
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
            width: '100%', minHeight: 140, padding: 10, borderRadius: 8,
            border: '1px solid var(--border)', background: 'var(--bg)',
            color: 'var(--text)', fontFamily: 'monospace', fontSize: 12,
            resize: 'vertical',
          }}
        />
        <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 6 }}>
          Поддержка: http / https / socks5 · одна строка = один прокси
        </div>
      </Card>

      {/* Advanced */}
      <Card style={{ padding: 14 }}>
        <button
          onClick={() => setShowAdvanced(v => !v)}
          style={{ background: 'none', border: 'none', color: 'var(--text-2)', fontSize: 13, fontWeight: 600, cursor: 'pointer', padding: 0 }}
        >
          {showAdvanced ? '▼' : '▶'} Расширенные настройки
        </button>
        {showAdvanced && (
          <div style={{ marginTop: 12, display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 10 }}>
            <NumField label="Воркеров (3-4)" value={maxWorkers} onChange={setMaxWorkers} min={1} max={6} disabled={isRunning} />
            <NumField label="Ретраев" value={maxRetries} onChange={setMaxRetries} min={1} max={5} disabled={isRunning} />
            <NumField label="Timeout (с)" value={pageTimeout} onChange={setPageTimeout} min={10} max={300} disabled={isRunning} />
            <NumField label="Cooldown min (с)" value={cooldownMin} onChange={setCooldownMin} min={60} max={3600} disabled={isRunning} />
            <NumField label="Cooldown max (с)" value={cooldownMax} onChange={setCooldownMax} min={60} max={7200} disabled={isRunning} />
            <NumField label="Ротация min (с)" value={rotationMin} onChange={setRotationMin} min={1} max={120} disabled={isRunning} />
            <NumField label="Ротация max (с)" value={rotationMax} onChange={setRotationMax} min={1} max={180} disabled={isRunning} />
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600 }}>Camoufox</label>
              <label style={{ fontSize: 12, color: 'var(--text-2)', display: 'flex', gap: 6, alignItems: 'center' }}>
                <input type="checkbox" checked={humanize} disabled={isRunning} onChange={e => setHumanize(e.target.checked)} /> humanize
              </label>
              <label style={{ fontSize: 12, color: 'var(--text-2)', display: 'flex', gap: 6, alignItems: 'center' }}>
                <input type="checkbox" checked={headless} disabled={isRunning} onChange={e => setHeadless(e.target.checked)} /> headless
              </label>
            </div>
          </div>
        )}
      </Card>

      {/* Action row */}
      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        {!isRunning ? (
          <Button variant="primary" onClick={handleStart} disabled={starting || proxies.length < 3}>
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

      {/* Progress panel */}
      {progress && (
        <Card style={{ padding: 16, border: `2px solid ${statusColor}` }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
            <div style={{ fontSize: 15, fontWeight: 700, color: statusColor }}>
              {statusEmoji} {labelForStatus(status)}
            </div>
            <div style={{ fontSize: 13, color: 'var(--text-2)' }}>
              {done + skipped} / {total} · {pct}%
            </div>
          </div>

          <div style={{ height: 8, borderRadius: 4, background: 'var(--bg-2)', overflow: 'hidden', marginBottom: 10 }}>
            <div style={{ height: '100%', width: `${pct}%`, background: statusColor, transition: 'width 0.4s ease' }} />
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 8, marginBottom: 10 }}>
            <SmallStat label="✅ OK" value={ok} color="var(--green)" />
            <SmallStat label="❌ Fail" value={failed} color="var(--red)" />
            <SmallStat label="⏭ Skip" value={skipped} color="var(--text-3)" />
            <SmallStat label="🌐 Узлов" value={`${rotator.available || 0}/${rotator.total || 0}`} />
            <SmallStat label="❄ Cooldown" value={rotator.in_cooldown || 0} color="#eab308" />
          </div>

          {progress.current_url && (
            <div style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              📞 {progress.current_url}
            </div>
          )}

          {progress.preview_urls && progress.preview_urls.length > 0 && status === 'queued' && (
            <div style={{ marginTop: 8, fontSize: 11, color: 'var(--text-3)' }}>
              Превью URL'ов: {progress.preview_urls.slice(0, 3).map(u => u.split('//')[1]).join(', ')}
              {progress.preview_urls.length > 3 && ' …'}
            </div>
          )}

          {progress.error && (
            <div style={{ marginTop: 10, padding: 10, borderRadius: 6, background: 'rgba(239, 68, 68, 0.1)', color: 'var(--red)', fontSize: 12, fontFamily: 'monospace' }}>
              {progress.error}
            </div>
          )}

          {isTerminal && jobId && (
            <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
              <Button variant="primary" onClick={() => handleDownload(jobId)}>
                📥 Скачать JSONL
              </Button>
              {mode === 'tgstat' && (
                <Button variant="secondary" onClick={() => loadAggregate(jobId)} disabled={aggregating}>
                  {aggregating ? '⏳ Считаю…' : '🔍 Показать каналы'}
                </Button>
              )}
            </div>
          )}
        </Card>
      )}

      {/* Aggregated TGStat results */}
      {aggregated && (
        <Card style={{ padding: 14 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
            <div style={{ fontSize: 14, fontWeight: 700, color: 'var(--text)' }}>
              📊 Найдено каналов: {aggregated.channels_count}
              <span style={{ fontSize: 11, fontWeight: 400, color: 'var(--text-3)', marginLeft: 10 }}>
                из {aggregated.rows_read} срезов
              </span>
            </div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <label style={{ fontSize: 11, color: 'var(--text-3)' }}>min подписчиков:</label>
              <input
                type="number" value={aggregateMinSubs}
                onChange={e => setAggregateMinSubs(Number(e.target.value))}
                style={{
                  padding: '4px 8px', borderRadius: 5, border: '1px solid var(--border)',
                  background: 'var(--bg)', color: 'var(--text)', fontSize: 12, width: 100,
                }}
              />
              <button onClick={() => loadAggregate(jobId, aggregateMinSubs)}
                style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', color: 'var(--text-2)', padding: '4px 10px', borderRadius: 5, cursor: 'pointer', fontSize: 11 }}
              >Пересчитать</button>
              <Button variant="secondary" onClick={handleDownloadCSV}>📤 CSV</Button>
            </div>
          </div>

          {aggregated.channels.length === 0 ? (
            <div style={{ fontSize: 12, color: 'var(--text-3)', padding: 20, textAlign: 'center' }}>
              Каналы под фильтр не нашлись. Попробуй снять «только с комментами» или снизить min подписчиков.
            </div>
          ) : (
            <div style={{ maxHeight: 480, overflowY: 'auto', border: '1px solid var(--border)', borderRadius: 6 }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                <thead style={{ position: 'sticky', top: 0, background: 'var(--bg-2)', zIndex: 1 }}>
                  <tr>
                    <Th>#</Th><Th>Username</Th><Th>Название</Th>
                    <Th>Подписчики</Th><Th>Комменты</Th><Th>Категория</Th><Th>Lang</Th>
                  </tr>
                </thead>
                <tbody>
                  {aggregated.channels.slice(0, 500).map((c, i) => (
                    <tr key={c.username} style={{ borderTop: '1px solid var(--border)' }}>
                      <Td muted>{i + 1}</Td>
                      <Td>
                        <a href={`https://t.me/${c.username.replace('@','')}`} target="_blank" rel="noreferrer"
                          style={{ color: 'var(--violet)', textDecoration: 'none' }}>
                          {c.username}
                        </a>
                        {c.verified && <span title="verified" style={{ marginLeft: 4 }}>✓</span>}
                      </Td>
                      <Td>{c.title || '—'}</Td>
                      <Td>{(c.subscribers || 0).toLocaleString('ru-RU')}</Td>
                      <Td>{c.has_comments ? '💬' : '—'}</Td>
                      <Td muted>{c.category || '—'}</Td>
                      <Td muted>{c.language || '—'}</Td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {aggregated.channels.length > 500 && (
                <div style={{ padding: 10, fontSize: 11, color: 'var(--text-3)', textAlign: 'center' }}>
                  Показаны первые 500 из {aggregated.channels.length} — полный список в CSV
                </div>
              )}
            </div>
          )}
        </Card>
      )}

      {/* Job history */}
      <Card style={{ padding: 14 }}>
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
                <div style={{ display: 'flex', gap: 6 }}>
                  <button onClick={() => loadAggregate(j.job_id)}
                    style={{ background: 'var(--bg)', color: 'var(--text-2)', border: '1px solid var(--border)', padding: '4px 8px', borderRadius: 4, fontSize: 11, cursor: 'pointer' }}
                  >🔍</button>
                  <button onClick={() => handleDownload(j.job_id)}
                    style={{ background: 'var(--violet)', color: '#fff', border: 'none', padding: '4px 10px', borderRadius: 4, fontSize: 11, cursor: 'pointer' }}
                  >📥</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}

function NumField({ label, value, onChange, min, max, disabled }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600 }}>{label}</label>
      <input
        type="number" value={value} min={min} max={max} disabled={disabled}
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
    <div style={{ padding: 8, borderRadius: 6, background: 'var(--bg-2)', display: 'flex', flexDirection: 'column', gap: 2 }}>
      <div style={{ fontSize: 10, color: 'var(--text-3)' }}>{label}</div>
      <div style={{ fontSize: 16, fontWeight: 700, color: color || 'var(--text)' }}>{value}</div>
    </div>
  )
}

function Th({ children }) {
  return <th style={{ padding: '8px 10px', textAlign: 'left', fontWeight: 600, fontSize: 11, color: 'var(--text-3)' }}>{children}</th>
}

function Td({ children, muted }) {
  return <td style={{ padding: '6px 10px', color: muted ? 'var(--text-3)' : 'var(--text)' }}>{children}</td>
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
