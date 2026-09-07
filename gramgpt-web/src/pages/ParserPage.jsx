import { useEffect, useState, useRef } from 'react'
import { accountsAPI, parserAPI } from '../services/api'
import { Card, Button, Input, Modal, Badge, Spinner, Empty, StatCard } from '../components/ui'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
import useLocalStorageState from '../hooks/useLocalStorageState'
import ParserMetrics from './ParserMetrics'
import WebScraperPanel from './WebScraperPanel'

function CheckBanner({ icon, label, status, current, line, color, onStop, onDismiss }) {
  const running = status === 'running'
  const done = status === 'done'
  const bg = running ? 'rgba(255,255,255,0.04)' : done ? 'rgba(61,214,140,0.06)' : 'rgba(248,81,73,0.06)'
  const border = running ? 'rgba(255,255,255,0.12)' : done ? 'rgba(61,214,140,0.2)' : 'rgba(248,81,73,0.2)'
  const textColor = running ? color : done ? 'var(--green)' : 'var(--red)'
  return (
    <div style={{
      marginBottom: 10, padding: '12px 16px', borderRadius: 12,
      background: bg, border: `1px solid ${border}`,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: running ? 8 : 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 15 }}>{running ? icon : done ? '✅' : '❌'}</span>
          <span style={{ fontWeight: 700, fontSize: 13, color: textColor }}>
            {running ? `${label}...` : done ? `${label} — готово` : `${label} — ошибка`}
          </span>
          <span style={{ fontSize: 12, color: 'var(--text-3)' }}>{line}</span>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          {running && <button onClick={onStop} style={{ background: 'var(--red-dim)', color: 'var(--red)', border: '1px solid rgba(248,81,73,0.3)', padding: '4px 10px', borderRadius: 6, fontSize: 11, fontWeight: 600, cursor: 'pointer' }}>⏹ Стоп</button>}
          {!running && <button onClick={onDismiss} style={{ background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer', fontSize: 16 }}>✕</button>}
        </div>
      </div>
      {running && current && (
        <div style={{ fontSize: 11, color: 'var(--text-3)', paddingLeft: 25 }}>
          Сейчас: <span style={{ color }}>@{current}</span>
        </div>
      )}
    </div>
  )
}

// Сколько каналов берём с сервера за раз и сколько строк рисуем.
// В базе десятки тысяч каналов: и запрос целиком, и отрисовка всех
// разом вешали вкладку. Фильтры считаются по загруженному списку,
// а остальное догружается по кнопке.
const PAGE_SIZE = 300

export default function ParserPage() {
  const [channels, setChannels] = useState([])
  const [totalChannels, setTotalChannels] = useState(0)
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE)
  // Счётчики по всей базе, а не по загруженной странице
  const [dbStats, setDbStats] = useState(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [accounts, setAccounts] = useState([])
  const [loading, setLoading] = useState(true)
  const [searching, setSearching] = useState(false)
  const [searchModal, setSearchModal] = useState(false)
  const [importModal, setImportModal] = useState(false)
  const [toast, setToast] = useState(null)
  const [folders, setFolders] = useState([])
  const [filterFolder, setFilterFolder] = useState('all')
  const [onlyWithComments, setOnlyWithComments] = useState(false)
  const [onlyUnchecked, setOnlyUnchecked] = useState(false)
  // 'subs' — крупные сверху, 'new' — свежие находки паутинки сверху
  const [sortBy, setSortBy] = useState('subs')
  const [newFolderInput, setNewFolderInput] = useState(false)
  const [selectedChannels, setSelectedChannels] = useState([])
  const [folderModal, setFolderModal] = useState(false)
  const [folderName, setFolderName] = useState('')

  const [form, setForm] = useState({
    account_id: null,                       // legacy, single-mode
    account_ids: [],                        // multi-mode — новый флоу
    keywords: '', min_subscribers: 1000, max_subscribers: 500000,
    only_with_comments: true, active_hours: 48, source: 'telegram',
    min_concurrency: 2, max_concurrency: 7, // случайная параллельность
  })
  // Cooldowns на аккаунтах (после FloodWait). Показываем баджиком в списке.
  const [accCooldowns, setAccCooldowns] = useState({})     // {acc_id: {seconds_left}}
  // Multi-парсер state
  const [multiJobId, setMultiJobId] = useLocalStorageState('parser_multi_job', null)
  const [multiProgress, setMultiProgress] = useState(null)
  const multiPollRef = useRef(null)
  const [importText, setImportText] = useState('')

  // Keyword geo-expansion
  const [geoExpand, setGeoExpand]         = useState(false)
  const [selectedGeos, setSelectedGeos]   = useState(['ru', 'en'])
  const [availableGeos, setAvailableGeos] = useState([])
  const [geoPresets, setGeoPresets]       = useState({})
  const [geosLoaded, setGeosLoaded]       = useState(false)
  const [expandOpts, setExpandOpts]       = useState({
    include_translit: true,
    include_translations: true,
    include_geo_variants: false,
    include_prefixes_suffixes: false,
    include_topic_synonyms: false,
  })
  const [expandPreview, setExpandPreview] = useState(null)  // { total_keywords }
  const [expanding, setExpanding]         = useState(false)

  const loadGeos = async () => {
    if (geosLoaded) return
    try {
      const { data } = await parserAPI.keywordGeos()
      setAvailableGeos(data.geos || [])
      setGeoPresets(data.presets || {})
      setGeosLoaded(true)
    } catch { }
  }

  const previewExpand = async (seeds, geos, opts) => {
    if (!seeds.length || !geos.length) { setExpandPreview(null); return }
    try {
      const { data } = await parserAPI.keywordExpand({
        seeds, target_geos: geos, ...opts, max_per_seed: 60,
      })
      setExpandPreview({ total: data.total_keywords })
    } catch { setExpandPreview(null) }
  }

  // Page tabs
  // pageTab персистится в localStorage — при F5 остаёмся на той же под-вкладке.
  const [pageTab, setPageTab] = useLocalStorageState('parser_active_tab', 'channels')

  // Whitelist (pass-rate)
  const [whitelist, setWhitelist]         = useState([])
  const [wlLoading, setWlLoading]         = useState(false)
  const [wlSortBy, setWlSortBy]           = useState('pass_rate')
  const [wlMinRate, setWlMinRate]         = useState(0)

  // Search progress
  const [searchProgress, setSearchProgress]   = useState(null)
  const searchPollRef = useRef(null)
  const searchDismissRef = useRef(null)

  // Spider (similar crawler)
  const [spiderModal, setSpiderModal]         = useState(false)
  const [spiderSaving, setSpiderSaving]       = useState(false)
  const [spiderProgress, setSpiderProgress]   = useState(null)
  const [spiderForm, setSpiderForm]           = useState({
    account_id: null,               // legacy single
    account_ids: [],                // multi
    seeds: '', max_depth: 2, max_channels: 500,
    folder: '', pause_min: 8.0, pause_max: 15.0,
    stop_on_flood: true, flood_cooldown_sec: 300,
    min_concurrency: 2, max_concurrency: 7,
    only_with_comments: true,       // ← только каналы с комментами
  })
  const spiderPollRef = useRef(null)
  // Multi-паутинка
  const [spiderMultiJobId, setSpiderMultiJobId] = useLocalStorageState('spider_multi_job', null)
  const [spiderMultiProgress, setSpiderMultiProgress] = useState(null)
  const spiderMultiPollRef = useRef(null)

  // Check modal (verify_comments + alive_check)
  const [checkModal, setCheckModal]    = useState(false)
  const [checkMode, setCheckMode]      = useState('verify') // 'verify' | 'alive'
  const [checkSaving, setCheckSaving]  = useState(false)
  const [verifyForm, setVerifyForm]    = useState({
    account_id: null, folder: '', limit: 200, active_hours: 0,
    only_unverified: true, pause_min: 2.0, pause_max: 4.0,
    stop_on_flood: true, flood_cooldown_sec: 300,
  })
  const [aliveForm, setAliveForm]      = useState({
    account_id: null, folder: '', limit: 200, max_days_inactive: 30,
    pause_min: 0.3, pause_max: 0.8,
  })
  const [verifyProgress, setVerifyProgress] = useState(null)
  const [aliveProgress, setAliveProgress]   = useState(null)
  const verifyPollRef = useRef(null)
  const alivePollRef  = useRef(null)

  const showToast = (t, type = 'success') => { setToast({ text: t, type }); setTimeout(() => setToast(null), 3500) }

  // ── Загрузка страницами ───────────────────────────────────────
  // Сервер отдаёт по PAGE_SIZE каналов. Раньше тянули все 35 тысяч на
  // каждое открытие вкладки и на каждое автообновление раз в 15 секунд —
  // страница висла, а свежие находки паутинки появлялись только после
  // полной перезагрузки.
  // Фильтры считает база, а не браузер: иначе «только непроверенные» на
  // загруженной странице искали бы среди трёхсот каналов из тридцати пяти
  // тысяч и показывали пусто.
  const query = () => ({
    ...(filterFolder !== 'all' ? { folder: filterFolder } : {}),
    ...(onlyWithComments ? { only_with_comments: true } : {}),
    ...(onlyUnchecked ? { only_unchecked: true } : {}),
    ...(sortBy !== 'subs' ? { sort: sortBy } : {}),
  })

  const load = async (silent = false, reset = false) => {
    if (!silent) setLoading(true)
    // Автообновление не должно отматывать список назад: если человек уже
    // догрузил несколько страниц, обновляем ровно столько же строк.
    // При смене фильтра, наоборот, начинаем с одной страницы.
    const take = (silent && !reset)
      ? Math.min(Math.max(channels.length, PAGE_SIZE), 5000)
      : PAGE_SIZE
    try {
      const [c, a, f, st] = await Promise.all([
        parserAPI.list({ ...query(), limit: take, offset: 0 }),
        accountsAPI.list(),
        parserAPI.folders().catch(() => ({ data: [] })),
        parserAPI.stats(filterFolder !== 'all' ? { folder: filterFolder } : {})
          .catch(() => ({ data: null })),
      ])
      setChannels(c.data)
      if (st.data) setDbStats(st.data)
      setTotalChannels(Number(c.headers?.['x-total-count'] ?? c.data.length))
      setAccounts(a.data.filter(acc => acc.status === 'active'))
      setFolders(f.data || [])
    } catch { }
    if (!silent) setLoading(false)
  }

  // Догрузить следующую страницу, не трогая уже показанное
  const loadMore = async () => {
    if (loadingMore) return
    setLoadingMore(true)
    try {
      const c = await parserAPI.list({
        ...query(), limit: PAGE_SIZE, offset: channels.length })
      setChannels(prev => {
        const seen = new Set(prev.map(x => x.id))
        return [...prev, ...c.data.filter(x => !seen.has(x.id))]
      })
      setTotalChannels(Number(c.headers?.['x-total-count'] ?? 0))
    } catch { }
    setLoadingMore(false)
  }

  useEffect(() => { load() }, [])
  // Сменили фильтр — начинаем список заново с первой страницы.
  // На первом рендере не дёргаем: там уже отработал эффект выше.
  const firstRun = useRef(true)
  useEffect(() => {
    if (firstRun.current) { firstRun.current = false; return }
    setVisibleCount(PAGE_SIZE)
    load(true, true)
  }, [filterFolder, onlyWithComments, onlyUnchecked, sortBy])
  useAutoRefresh(() => load(true), 15000)

  // ── Search progress polling ─────────────────────────────────
  const loadSearchProgress = async () => {
    try {
      const { data } = await parserAPI.searchProgress()
      if (data.status === 'idle') {
        setSearchProgress(prev => {
          // Если было running → стало idle, значит задача завершена, ничего не делаем
          return prev?.status === 'running' ? prev : null
        })
        return 'idle'
      }
      setSearchProgress(data)
      return data.status
    } catch { return 'idle' }
  }

  useEffect(() => {
    loadSearchProgress()
  }, [])

  useEffect(() => {
    if (searchPollRef.current) clearInterval(searchPollRef.current)
    if (searchDismissRef.current) clearTimeout(searchDismissRef.current)

    if (searchProgress?.status === 'running') {
      searchPollRef.current = setInterval(async () => {
        const status = await loadSearchProgress()
        if (status !== 'running') {
          clearInterval(searchPollRef.current)
          load(true)
        }
      }, 2500)
    } else if (searchProgress?.status === 'done' || searchProgress?.status === 'error') {
      // Автоисчезновение через 5 секунд после завершения
      searchDismissRef.current = setTimeout(() => setSearchProgress(null), 5000)
    }

    return () => {
      if (searchPollRef.current) clearInterval(searchPollRef.current)
      if (searchDismissRef.current) clearTimeout(searchDismissRef.current)
    }
  }, [searchProgress?.status])

  const handleSearchStop = async () => {
    try {
      await parserAPI.searchStop()
      clearInterval(searchPollRef.current)
      setSearchProgress(prev => prev ? { ...prev, status: 'done' } : null)
      load(true)
    } catch { }
  }

  // ── MULTI-парсинг polling ───────────────────────────────────
  const loadMultiProgress = async (jobId) => {
    if (!jobId) return 'idle'
    try {
      const { data } = await parserAPI.searchMultiProgress(jobId)
      if (!data || data.status === 'idle') return 'idle'
      setMultiProgress(data)
      return data.status
    } catch { return 'idle' }
  }

  const loadCooldowns = async () => {
    try {
      const { data } = await parserAPI.searchMultiCooldowns()
      const map = {}
      for (const row of data || []) {
        map[row.account_id] = row
      }
      setAccCooldowns(map)
    } catch { }
  }

  // На mount:
  //   1) Читаем /multi/active — есть ли живые multi-задачи у юзера.
  //      Даже если localStorage пустой, если бэк говорит running — авто-
  //      подхватываем. Так задача переживает F5 в любом браузере.
  //   2) Если LS был, но на бэке нет свежих running задач — чистим LS.
  useEffect(() => {
    (async () => {
      let active = []
      try {
        const { data } = await parserAPI.searchMultiActive()
        active = data.jobs || []
      } catch {}
      const running = active.find(j => j.status === 'running')
      if (running) {
        setMultiJobId(running.job_id)
        loadMultiProgress(running.job_id)
      } else if (multiJobId) {
        // LS-джоб не running, но всё равно тянем чтобы показать final
        // state (done/error/cancelled) если он свежий
        const st = await loadMultiProgress(multiJobId)
        if (st === 'idle') setMultiJobId(null)
      }
      loadCooldowns()
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (multiPollRef.current) clearInterval(multiPollRef.current)
    if (multiProgress?.status === 'running' && multiJobId) {
      multiPollRef.current = setInterval(async () => {
        const st = await loadMultiProgress(multiJobId)
        if (['done', 'idle', 'error', 'cancelled'].includes(st)) {
          clearInterval(multiPollRef.current)
          loadCooldowns()  // после done — обновляем cooldowns
          load(true)       // подтянуть найденные каналы
        }
      }, 2500)
    }
    return () => { if (multiPollRef.current) clearInterval(multiPollRef.current) }
  }, [multiProgress?.status, multiJobId])

  const handleMultiStop = async () => {
    if (!multiJobId) return
    try {
      await parserAPI.searchMultiStop(multiJobId)
      setMultiProgress(p => p ? { ...p, status: 'cancelling' } : null)
    } catch { }
  }

  const handleMultiForceReset = async () => {
    if (!multiJobId) return
    if (!confirm(
      '🚨 Force-reset multi-парсера:\n\n' +
      '  1. Убить celery-таск через revoke(terminate=True) — SIGTERM в воркер\n' +
      '  2. Поставить cancel-флаг (второй пояс безопасности)\n' +
      '  3. Пометить снапшот как error → UI перестанет крутить\n\n' +
      'Продолжить?'
    )) return
    try {
      const { data } = await parserAPI.searchMultiReset(multiJobId)
      setMultiProgress(p => p ? {
        ...p, status: 'error', stale: true,
        error: `Force-reset юзером${data?.revoked ? ' (celery revoked)' : ''}`,
      } : null)
      showToast(`Force-reset ok${data?.revoked ? ' · celery убит' : ''}`, 'success')
      // Немного подождём и обновим cooldowns/каналы
      setTimeout(() => { loadCooldowns(); load(true) }, 1500)
    } catch (err) {
      showToast(err.response?.data?.detail || 'Force-reset не удался', 'error')
    }
  }

  const handleMultiClose = () => {
    setMultiJobId(null)
    setMultiProgress(null)
  }

  const handleClearCooldown = async (accId) => {
    try {
      await parserAPI.searchMultiCooldownClear(accId)
      await loadCooldowns()
    } catch { }
  }

  // ── Spider polling ──────────────────────────────────────────
  const loadSpiderProgress = async () => {
    try {
      const { data } = await parserAPI.similarProgress()
      setSpiderProgress(data.status === 'idle' ? null : data)
      return data.status
    } catch { return 'idle' }
  }

  useEffect(() => {
    loadSpiderProgress()
  }, [])

  useEffect(() => {
    if (spiderPollRef.current) clearInterval(spiderPollRef.current)
    if (spiderProgress?.status === 'running') {
      spiderPollRef.current = setInterval(async () => {
        const status = await loadSpiderProgress()
        if (status !== 'running') {
          clearInterval(spiderPollRef.current)
          load()
        }
      }, 3000)
    }
    return () => { if (spiderPollRef.current) clearInterval(spiderPollRef.current) }
  }, [spiderProgress?.status])

  const handleSpiderStart = async () => {
    const useMulti = (spiderForm.account_ids || []).length > 0
    if (!useMulti && !spiderForm.account_id) return
    if (!spiderForm.seeds.trim()) return
    setSpiderSaving(true)
    try {
      const seeds = spiderForm.seeds.split('\n').map(s => s.trim().replace(/^@/, '')).filter(Boolean)
      if (useMulti) {
        const payload = {
          account_ids: spiderForm.account_ids,
          seeds,
          max_depth: spiderForm.max_depth,
          max_channels: spiderForm.max_channels,
          folder: spiderForm.folder || '',
          pause_min: spiderForm.pause_min,
          pause_max: spiderForm.pause_max,
          min_concurrency: spiderForm.min_concurrency || 2,
          max_concurrency: spiderForm.max_concurrency || 7,
          only_with_comments: spiderForm.only_with_comments,
        }
        const { data } = await parserAPI.similarMultiStart(payload)
        setSpiderMultiJobId(data.job_id)
        setSpiderMultiProgress({ status: 'running', job_id: data.job_id, accounts: {} })
        setSpiderModal(false)
        showToast(`🕷 Multi-паутинка: ${data.accounts_scheduled} акк, ${seeds.length} seeds`)
      } else {
        await parserAPI.similarStart({ ...spiderForm, seeds })
        setSpiderModal(false)
        showToast(`Паутинка запущена: ${seeds.length} стартовых каналов`)
        await loadSpiderProgress()
      }
    } catch (err) {
      const status = err.response?.status
      const detail = err.response?.data?.detail || 'Ошибка'
      if (status === 409 && useMulti) {
        // Уже идёт — подпишемся на прогресс
        try {
          const { data } = await parserAPI.similarMultiActive()
          const running = (data.jobs || []).find(j => j.status === 'running')
          if (running) {
            setSpiderMultiJobId(running.job_id)
            loadSpiderMultiProgress(running.job_id)
          }
        } catch {}
      } else {
        showToast(detail, 'error')
      }
    }
    setSpiderSaving(false)
  }

  const handleSpiderStop = async () => {
    try {
      await parserAPI.similarStop()
      showToast('Паутинка остановлена')
      setTimeout(async () => { await loadSpiderProgress(); await load() }, 2000)
    } catch { }
  }

  // ── Multi-паутинка polling + persistence ──
  const loadSpiderMultiProgress = async (jobId) => {
    if (!jobId) return 'idle'
    try {
      const { data } = await parserAPI.similarMultiProgress(jobId)
      if (!data || data.status === 'idle') return 'idle'
      setSpiderMultiProgress(data)
      return data.status
    } catch { return 'idle' }
  }
  useEffect(() => {
    (async () => {
      let active = []
      try {
        const { data } = await parserAPI.similarMultiActive()
        active = data.jobs || []
      } catch {}
      const running = active.find(j => j.status === 'running')
      if (running) {
        setSpiderMultiJobId(running.job_id)
        loadSpiderMultiProgress(running.job_id)
      } else if (spiderMultiJobId) {
        const st = await loadSpiderMultiProgress(spiderMultiJobId)
        if (st === 'idle') setSpiderMultiJobId(null)
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => {
    if (spiderMultiPollRef.current) clearInterval(spiderMultiPollRef.current)
    if (spiderMultiProgress?.status === 'running' && spiderMultiJobId) {
      spiderMultiPollRef.current = setInterval(async () => {
        const st = await loadSpiderMultiProgress(spiderMultiJobId)
        if (['done', 'idle', 'error', 'cancelled'].includes(st)) {
          clearInterval(spiderMultiPollRef.current)
          load(true)
        }
      }, 2500)
    }
    return () => { if (spiderMultiPollRef.current) clearInterval(spiderMultiPollRef.current) }
  }, [spiderMultiProgress?.status, spiderMultiJobId])

  const handleSpiderMultiStop = async () => {
    if (!spiderMultiJobId) return
    try {
      await parserAPI.similarMultiStop(spiderMultiJobId)
      setSpiderMultiProgress(p => p ? { ...p, status: 'cancelling' } : null)
    } catch {}
  }
  const handleSpiderMultiForceReset = async () => {
    if (!spiderMultiJobId) return
    if (!confirm('🚨 Force-reset multi-паутинки?\n\nSIGTERM в celery-воркер + очистка Redis.')) return
    try {
      const { data } = await parserAPI.similarMultiReset(spiderMultiJobId)
      setSpiderMultiProgress(p => p ? { ...p, status: 'error', stale: true, error: `Force-reset${data?.revoked ? ' (revoked)' : ''}` } : null)
      showToast('Force-reset ok', 'success')
    } catch (err) {
      showToast(err.response?.data?.detail || 'Reset не удался', 'error')
    }
  }
  const handleSpiderMultiClose = () => {
    setSpiderMultiJobId(null)
    setSpiderMultiProgress(null)
  }

  // ── Verify / Alive progress polling ──────────────────────────
  const loadVerifyProgress = async () => {
    try {
      const { data } = await parserAPI.verifyProgress()
      setVerifyProgress(data.status === 'idle' ? null : data)
      return data.status
    } catch { return 'idle' }
  }
  const loadAliveProgress = async () => {
    try {
      const { data } = await parserAPI.aliveProgress()
      setAliveProgress(data.status === 'idle' ? null : data)
      return data.status
    } catch { return 'idle' }
  }

  useEffect(() => { loadVerifyProgress(); loadAliveProgress() }, [])

  useEffect(() => {
    if (verifyPollRef.current) clearInterval(verifyPollRef.current)
    if (verifyProgress?.status === 'running') {
      verifyPollRef.current = setInterval(async () => {
        const status = await loadVerifyProgress()
        if (status !== 'running') { clearInterval(verifyPollRef.current); load() }
      }, 3000)
    }
    return () => { if (verifyPollRef.current) clearInterval(verifyPollRef.current) }
  }, [verifyProgress?.status])

  useEffect(() => {
    if (alivePollRef.current) clearInterval(alivePollRef.current)
    if (aliveProgress?.status === 'running') {
      alivePollRef.current = setInterval(async () => {
        const status = await loadAliveProgress()
        if (status !== 'running') { clearInterval(alivePollRef.current); load() }
      }, 3000)
    }
    return () => { if (alivePollRef.current) clearInterval(alivePollRef.current) }
  }, [aliveProgress?.status])

  const handleVerifyStart = async () => {
    if (!verifyForm.account_id) { showToast('Выбери аккаунт', 'error'); return }
    setCheckSaving(true)
    try {
      await parserAPI.verifyStart(verifyForm)
      setCheckModal(false)
      showToast('Проверка комментариев запущена')
      await loadVerifyProgress()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setCheckSaving(false)
  }
  const handleVerifyStop = async () => {
    try { await parserAPI.verifyStop(); showToast('Останавливаю...'); setTimeout(loadVerifyProgress, 1500) } catch { }
  }
  const handleAliveStart = async () => {
    setCheckSaving(true)
    try {
      await parserAPI.aliveStart(aliveForm)
      setCheckModal(false)
      showToast('Проверка живости запущена (web-only, без API)')
      await loadAliveProgress()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
    setCheckSaving(false)
  }
  const handleAliveStop = async () => {
    try { await parserAPI.aliveStop(); showToast('Останавливаю...'); setTimeout(loadAliveProgress, 1500) } catch { }
  }

  const loadWhitelist = async (sortBy = wlSortBy, minRate = wlMinRate) => {
    setWlLoading(true)
    try {
      const { data } = await parserAPI.whitelist(minRate, sortBy)
      setWhitelist(data)
    } catch { }
    setWlLoading(false)
  }

  const handleWlDelete = async (id) => {
    try {
      await parserAPI.whitelistDelete(id)
      setWhitelist(prev => prev.filter(x => x.id !== id))
      showToast('Статистика сброшена')
    } catch { showToast('Ошибка', 'error') }
  }


  const filteredChannels = channels.filter(c => {
    if (filterFolder !== 'all' && (c.folder || '') !== filterFolder) return false
    // «С комментариями» означает «можно писать», а не «есть обсуждение».
    // Проверенный канал судим по вердикту, непроверенный — по старому флагу:
    // он там кандидат и помечен в списке отдельно.
    if (onlyWithComments) {
      if (c.cap_commentable === false) return false
      if (c.cap_commentable !== true && !c.has_comments) return false
    }
    if (onlyUnchecked && c.checked) return false
    return true
  })

  // При смене фильтра начинаем показ заново — иначе после сужения выборки
  // остался бы прокрученный хвост от прошлой.
  useEffect(() => { setVisibleCount(PAGE_SIZE) }, [filterFolder, onlyWithComments, onlyUnchecked])

  const handleSetFolder = async (folder) => {
    if (!selectedChannels.length) return
    try {
      await parserAPI.setFolder(selectedChannels, folder)
      showToast(`${selectedChannels.length} каналов → "${folder}"`)
      setSelectedChannels([]); setFolderModal(false); await load()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
  }

  const toggleSelect = (id) => {
    setSelectedChannels(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id])
  }

  const selectAll = () => {
    if (selectedChannels.length === filteredChannels.length) setSelectedChannels([])
    else setSelectedChannels(filteredChannels.map(c => c.id))
  }

  const handleSearch = async () => {
    // Multi-режим: если выбрано >0 акков в чекбоксах.
    const useMulti = (form.account_ids || []).length > 0
    if (!useMulti && !form.account_id) return
    if (!form.keywords.trim()) return
    setSearching(true)
    try {
      let searchForm = { ...form }
      if (geoExpand && selectedGeos.length > 0) {
        setExpanding(true)
        const seeds = form.keywords.split(',').map(s => s.trim()).filter(Boolean)
        const { data } = await parserAPI.keywordExpand({
          seeds, target_geos: selectedGeos, ...expandOpts, max_per_seed: 60,
        })
        const flat = [...new Set(
          Object.values(data.results).flatMap(r => r.flat)
        )]
        searchForm = { ...form, keywords: flat.join(', ') }
        setExpanding(false)
      }
      if (useMulti) {
        // Multi-режим — random 2..7 воркеров, FloodWait→cooldown≥1час
        const payload = {
          account_ids: form.account_ids,
          keywords: searchForm.keywords,
          min_subscribers: searchForm.min_subscribers,
          max_subscribers: searchForm.max_subscribers,
          only_with_comments: searchForm.only_with_comments,
          active_hours: searchForm.active_hours,
          min_concurrency: searchForm.min_concurrency || 2,
          max_concurrency: searchForm.max_concurrency || 7,
        }
        const { data } = await parserAPI.searchMulti(payload)
        setMultiJobId(data.job_id)
        setMultiProgress({ status: 'running', job_id: data.job_id, accounts: {} })
        setSearchModal(false)
        showToast(`🚀 Multi: ${data.accounts_scheduled} акк, ${data.keywords} ключей`)
      } else {
        await parserAPI.search(searchForm)
        setSearchModal(false)
        setTimeout(loadSearchProgress, 1000)
      }
    } catch (err) {
      setExpanding(false)
      showToast(err.response?.data?.detail || 'Ошибка поиска', 'error')
    }
    setSearching(false)
  }

  const handleExport = async () => {
    try {
      const { data } = await parserAPI.exportCSV()
      const url = window.URL.createObjectURL(new Blob([data]))
      const a = document.createElement('a'); a.href = url; a.download = 'channels.csv'; a.click()
      showToast('CSV скачан')
    } catch { showToast('Ошибка экспорта', 'error') }
  }

  // ── Универсальный экспорт с фильтрами ──
  // filters: {folder?, only_with_comments?, min_subscribers?, max_subscribers?, format?}
  // Опция silent — не тостить успех (для быстрых per-folder кнопок).
  const doExport = async (filters, silent = false) => {
    try {
      const params = {}
      if (filters.folder !== undefined) params.folder = filters.folder
      if (filters.only_with_comments) params.only_with_comments = true
      if (filters.min_subscribers) params.min_subscribers = filters.min_subscribers
      if (filters.max_subscribers) params.max_subscribers = filters.max_subscribers
      if (filters.min_pass_rate) params.min_pass_rate = filters.min_pass_rate
      if (filters.no_skips) params.no_skips = true
      if (filters.min_attempts) params.min_attempts = filters.min_attempts
      params.format = filters.format || 'csv'
      const resp = await parserAPI.exportChannels(params)
      const count = resp.headers['x-records-count'] || '?'
      // Достаём filename из Content-Disposition
      const cd = resp.headers['content-disposition'] || ''
      let filename = 'channels.csv'
      const m1 = cd.match(/filename\*=UTF-8''([^;]+)/i)
      const m2 = cd.match(/filename="?([^";]+)"?/i)
      if (m1) filename = decodeURIComponent(m1[1])
      else if (m2) filename = m2[1]
      const url = window.URL.createObjectURL(new Blob([resp.data]))
      const a = document.createElement('a'); a.href = url; a.download = filename; a.click()
      window.URL.revokeObjectURL(url)
      if (!silent) showToast(`💾 ${count} каналов → ${filename}`)
    } catch (err) {
      showToast(err.response?.data?.detail || 'Ошибка экспорта', 'error')
    }
  }

  // Модалка «Скачать список»
  const [exportModal, setExportModal] = useState(false)
  const [exportForm, setExportForm] = useState({
    source: 'parsed',             // parsed | tgstat_checked
    folder: 'all',                // 'all' | '' (без папки) | название (только для parsed)
    only_with_comments: false,
    min_subscribers: 0,
    min_pass_rate: 0,             // 0..100 % успешно прошедших комментов
    no_skips: false,              // отбросить каналы где skipped_count > 0
    min_attempts: 0,              // минимум N попыток чтобы статистика была значима
    format: 'csv',                // csv | txt | txt_links
  })
  const [exportCounts, setExportCounts] = useState(null)  // {parsed: {total, with_comments}, tgstat_checked: {...}}

  // Подгружаем счётчики при открытии модалки чтобы юзер сразу видел
  // сколько в каждом источнике (Parsed: 555 с комм / TGStat: 1277 с комм).
  useEffect(() => {
    if (!exportModal) return
    parserAPI.exportCounts()
      .then(({ data }) => setExportCounts(data))
      .catch(() => setExportCounts(null))
  }, [exportModal])

  const handleImport = async () => {
    const list = importText.split('\n').map(s => s.trim()).filter(Boolean)
    if (!list.length) return
    try {
      const { data } = await parserAPI.importList(list)
      showToast(`Добавлено ${data.added} каналов`)
      setImportModal(false); setImportText(''); await load()
    } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
  }

  const handleClear = async () => {
    if (!window.confirm('Удалить все каналы?')) return
    try { await parserAPI.clearAll(); showToast('Очищено'); await load() } catch { }
  }

  if (loading) return <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}><Spinner size={32} /></div>

  return (
    <div style={{ padding: '28px 32px', animation: 'fadeUp 0.4s cubic-bezier(0.16,1,0.3,1)' }}>
      {toast && <div style={{ position: 'fixed', top: 24, right: 24, zIndex: 999, padding: '12px 20px', borderRadius: 12, fontSize: 13, fontWeight: 600, background: toast.type === 'error' ? 'var(--red-dim)' : 'var(--green-dim)', color: toast.type === 'error' ? 'var(--red)' : 'var(--green)', boxShadow: '0 8px 30px rgba(0,0,0,0.5)', animation: 'fadeUp 0.3s ease' }}>{toast.text}</div>}

      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 28 }}>
        <div>
          <div style={{ fontSize: 11, color: 'var(--blue)', fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>🔍 ПАРСЕР</div>
          <h1 style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.04em' }}>Парсер каналов</h1>
          <p style={{ fontSize: 13, color: 'var(--text-3)', marginTop: 4 }}>Сбор базы каналов-доноров для комментинга</p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <Button variant="ghost" onClick={() => setImportModal(true)}>📥 Импорт</Button>
          <Button variant="ghost" onClick={() => setExportModal(true)} disabled={channels.length === 0}>📤 Скачать</Button>
          <Button variant="outline" onClick={() => { setSpiderModal(true) }}
            style={spiderProgress?.status === 'running' ? { borderColor: 'rgba(0,194,178,0.5)', color: 'var(--teal)' } : {}}>
            🕷 Паутинка
          </Button>
          <Button variant="outline" onClick={() => setCheckModal(true)}
            style={(verifyProgress?.status === 'running' || aliveProgress?.status === 'running')
              ? { borderColor: 'rgba(255,176,32,0.5)', color: 'var(--orange, #ffb020)' } : {}}>
            🛡 Проверка
          </Button>
          <Button variant="primary" onClick={() => { setSearchModal(true); loadGeos() }}>🔍 Поиск</Button>
        </div>
      </div>

      {/* Tab bar */}
      <div style={{ display: 'flex', gap: 4, marginBottom: 20, background: 'var(--bg-2)', padding: 4, borderRadius: 12, border: '1px solid var(--border)', width: 'fit-content' }}>
        {[
          { key: 'channels', label: '📋 Каналы' },
          { key: 'whitelist', label: '📊 Проходимость' },
          { key: 'metrics', label: '📈 Метрики' },
          { key: 'web', label: '🛰️ Web-парсер' },
        ].map(t => (
          <button key={t.key} onClick={() => {
            setPageTab(t.key)
            if (t.key === 'whitelist' && whitelist.length === 0) loadWhitelist()
          }} style={{
            padding: '7px 18px', borderRadius: 9, fontSize: 12, fontWeight: pageTab === t.key ? 700 : 500,
            border: 'none', cursor: 'pointer', transition: 'all 0.15s',
            background: pageTab === t.key ? 'var(--bg-card)' : 'transparent',
            color: pageTab === t.key ? 'var(--text)' : 'var(--text-3)',
            boxShadow: pageTab === t.key ? '0 1px 3px rgba(0,0,0,0.2)' : 'none',
          }}>{t.label}</button>
        ))}
      </div>

      {pageTab === 'channels' && channels.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12, marginBottom: 20 }}>
          <div onClick={() => setOnlyWithComments(false)} style={{
            cursor: 'pointer',
            outline: !onlyWithComments ? '2px solid rgba(124,77,255,0.5)' : 'none',
            outlineOffset: -2, borderRadius: 12,
          }}>
            <StatCard label={`Каналов в базе${!onlyWithComments ? ' • активный' : ''}`}
              value={(dbStats?.total ?? channels.length).toLocaleString('ru')} icon="📢" />
          </div>
          <div onClick={() => setOnlyWithComments(true)} style={{
            cursor: 'pointer',
            outline: onlyWithComments ? '2px solid var(--green)' : 'none',
            outlineOffset: -2, borderRadius: 12,
          }} title="Кликни — показать только каналы с открытыми комментариями">
            <StatCard
              label={`С комментариями${onlyWithComments ? ' • активный' : ''}`}
              value={(dbStats?.with_comments
                      ?? channels.filter(c => c.has_comments).length).toLocaleString('ru')}
              color="var(--green)" icon="💬"
            />
          </div>
          {/* Сколько каналов ещё не проверяли на комментарии (2026-08-24).
              Раньше этого не было видно вовсе, и непроверенные молча
              смешивались с проверенными-без-комментов. */}
          <div
            onClick={() => setOnlyUnchecked(v => !v)}
            style={{
              cursor: 'pointer',
              outline: onlyUnchecked ? '2px solid rgba(255,170,0,0.9)' : 'none',
              outlineOffset: -2, borderRadius: 12,
            }} title="Кликни — показать только каналы, которые ещё не проверяли">
            <StatCard
              label={`Не проверено${onlyUnchecked ? ' • активный' : ''}`}
              value={(dbStats?.unchecked
                      ?? channels.filter(c => !c.checked).length).toLocaleString('ru')}
              color="rgba(255,170,0,0.95)" icon="❓"
            />
          </div>
          <StatCard label="Ср. подписчиков"
            value={(dbStats?.avg_subscribers
                    ?? (channels.length ? Math.round(channels.reduce((s, c) => s + c.subscribers, 0) / channels.length) : 0)
                   ).toLocaleString('ru')}
            color="var(--violet)" icon="👥" />
        </div>
      )}

      {/* Search progress banner */}
      {searchProgress && (
        <div style={{
          marginBottom: 10, padding: '12px 16px', borderRadius: 12,
          background: searchProgress.status === 'running'
            ? 'rgba(61,139,255,0.06)'
            : searchProgress.status === 'done'
            ? 'rgba(61,214,140,0.06)'
            : 'rgba(248,81,73,0.06)',
          border: `1px solid ${searchProgress.status === 'running' ? 'rgba(61,139,255,0.2)' : searchProgress.status === 'done' ? 'rgba(61,214,140,0.2)' : 'rgba(248,81,73,0.2)'}`,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: searchProgress.status === 'running' ? 8 : 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ fontSize: 15 }}>
                {searchProgress.status === 'running' ? '🔍' : searchProgress.status === 'done' ? '✅' : '❌'}
              </span>
              <span style={{ fontWeight: 700, fontSize: 13, color: searchProgress.status === 'running' ? 'var(--blue)' : searchProgress.status === 'done' ? 'var(--green)' : 'var(--red)' }}>
                {searchProgress.status === 'running' ? 'Поиск каналов...'
                  : searchProgress.status === 'done' ? 'Поиск завершён'
                  : 'Ошибка поиска'}
              </span>
              <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
                Найдено: {searchProgress.found} &nbsp;·&nbsp; Сохранено: {searchProgress.saved}
                {searchProgress.status === 'running' && searchProgress.total_keywords > 0 && (
                  <> &nbsp;·&nbsp; Ключей: {searchProgress.total_keywords}</>
                )}
              </span>
            </div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              {searchProgress.status === 'running' && (
                <Button variant="danger" size="sm" onClick={handleSearchStop}>⏹ Стоп</Button>
              )}
              {searchProgress.status !== 'running' && (
                <button onClick={() => setSearchProgress(null)} style={{ background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer', fontSize: 16 }}>✕</button>
              )}
            </div>
          </div>

          {searchProgress.status === 'running' && (
            <>
              {searchProgress.current && (
                <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 6, paddingLeft: 25 }}>
                  Запрос: <span style={{ color: 'var(--blue)' }}>{searchProgress.current}</span>
                </div>
              )}
              {/* Прогресс-бар (анимированный пока running) */}
              <div style={{ height: 3, background: 'rgba(255,255,255,0.06)', borderRadius: 2, overflow: 'hidden' }}>
                <div style={{
                  height: '100%', borderRadius: 2,
                  background: 'linear-gradient(90deg, #3d8bff, #7c4dff)',
                  width: searchProgress.total_keywords > 0
                    ? `${Math.min(95, Math.round((searchProgress.found / Math.max(searchProgress.total_keywords * 10, 1)) * 100))}%`
                    : '100%',
                  animation: 'searchPulse 1.5s ease-in-out infinite',
                  transition: 'width 0.5s ease',
                }} />
              </div>
            </>
          )}

          {searchProgress.status === 'done' && (
            <div style={{ height: 3, background: 'rgba(255,255,255,0.06)', borderRadius: 2, overflow: 'hidden', marginTop: 8 }}>
              <div style={{ height: '100%', width: '100%', borderRadius: 2, background: 'var(--green)' }} />
            </div>
          )}
        </div>
      )}

      {/* Multi-search progress banner */}
      {multiProgress && multiProgress.status !== 'idle' && (
        <div style={{
          marginBottom: 10, padding: '12px 16px', borderRadius: 12,
          background: multiProgress.status === 'running'
            ? 'rgba(124,77,255,0.08)'
            : multiProgress.status === 'done'
            ? 'rgba(61,214,140,0.06)'
            : 'rgba(248,81,73,0.06)',
          border: `1px solid ${multiProgress.status === 'running' ? 'rgba(124,77,255,0.3)' : multiProgress.status === 'done' ? 'rgba(61,214,140,0.2)' : 'rgba(248,81,73,0.2)'}`,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
              <span style={{ fontSize: 15 }}>
                {multiProgress.status === 'running' ? '🚀'
                  : multiProgress.status === 'done' ? '✅'
                  : multiProgress.status === 'cancelled' ? '⏹'
                  : '❌'}
              </span>
              <span style={{ fontWeight: 700, fontSize: 13,
                color: multiProgress.status === 'running' ? 'var(--violet)'
                     : multiProgress.status === 'done' ? 'var(--green)'
                     : 'var(--red)' }}>
                Multi-парсинг
                {multiProgress.concurrency
                  ? ` · ${multiProgress.concurrency} воркер(ов) одновременно`
                  : ''}
              </span>
              <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
                Найдено: <b>{multiProgress.found_total || 0}</b>
                &nbsp;·&nbsp; В БД: <b>{multiProgress.saved_total || 0}</b>
                &nbsp;·&nbsp; Акков: {multiProgress.total_accounts || 0}
              </span>
            </div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              {multiProgress.status === 'running' && (
                <>
                  <Button variant="danger" size="sm" onClick={handleMultiStop}>⏹ Стоп</Button>
                  <Button variant="danger" size="sm" onClick={handleMultiForceReset}
                    style={{ background: '#7f1d1d', borderColor: '#dc2626' }}
                    title="Убить celery-таск через SIGTERM + очистить Redis. Если обычный Стоп не сработал.">
                    🚨 Force reset
                  </Button>
                </>
              )}
              {multiProgress.status !== 'running' && (
                <button onClick={handleMultiClose} style={{ background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer', fontSize: 16 }}>✕</button>
              )}
            </div>
          </div>

          {/* Error message для error/stale */}
          {(multiProgress.status === 'error' || multiProgress.stale) && (
            <div style={{
              marginBottom: 8, padding: '8px 10px', borderRadius: 6,
              background: 'rgba(248,81,73,0.10)', border: '1px solid rgba(248,81,73,0.35)',
              fontSize: 12, color: 'var(--red)',
            }}>
              {multiProgress.stale ? '⚠ Воркер умер — задача прервана. ' : ''}
              {multiProgress.error || 'Ошибка выполнения. Запусти заново.'}
            </div>
          )}

          {/* Per-account grid */}
          {multiProgress.accounts && Object.keys(multiProgress.accounts).length > 0 && (
            <div style={{
              display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))',
              gap: 6, marginTop: 6,
            }}>
              {Object.entries(multiProgress.accounts).map(([accId, acc]) => {
                const icon = {
                  queued: '⋯', running: '🔍', done: '✅',
                  flooded: '⏰', cancelled: '⏹',
                  not_authorized: '🚫', no_session: '🚫',
                  connect_fail: '❌',
                  busy_other: '🔒', yielded: '↪',
                }[acc.status] || '·'
                const color = {
                  running: 'rgba(124,77,255,0.20)',
                  done: 'rgba(61,214,140,0.15)',
                  flooded: 'rgba(255,152,0,0.18)',
                  cancelled: 'rgba(150,150,150,0.15)',
                  busy_other: 'rgba(100,116,139,0.20)',
                  yielded: 'rgba(100,116,139,0.20)',
                }[acc.status] || 'rgba(255,255,255,0.04)'
                return (
                  <div key={accId} style={{
                    padding: '6px 9px', borderRadius: 8,
                    background: color, fontSize: 11,
                    border: '1px solid rgba(255,255,255,0.05)',
                  }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span style={{ fontWeight: 600 }}>{icon} {acc.phone}</span>
                      <span style={{ color: 'var(--text-3)' }}>
                        {acc.saved || 0}/{acc.found || 0}
                      </span>
                    </div>
                    <div style={{ color: 'var(--text-3)', fontSize: 10, marginTop: 2 }}>
                      {acc.status === 'running' && acc.current_kw && (
                        <>«{acc.current_kw}» · kw {acc.keywords_done}/{acc.keywords_total}</>
                      )}
                      {acc.status === 'flooded' && (
                        <>⏰ Flood {acc.flood_wait}с → пауза ≥1ч</>
                      )}
                      {acc.status === 'queued' && (<>ждёт очереди · {acc.keywords_total} kw</>)}
                      {acc.status === 'done' && (<>готово · {acc.keywords_done} kw обработано</>)}
                      {acc.status === 'cancelled' && (<>остановлено</>)}
                      {acc.status === 'not_authorized' && (<>не авторизован</>)}
                      {acc.status === 'connect_fail' && (<>ошибка подключения</>)}
                      {acc.status === 'busy_other' && (<>🔒 занят warmup/comment/refix</>)}
                      {acc.status === 'yielded' && (<>↪ передан warmup/comment</>)}
                    </div>
                  </div>
                )
              })}
            </div>
          )}

          {/* Skipped list */}
          {Array.isArray(multiProgress.skipped) && multiProgress.skipped.length > 0 && (
            <div style={{ marginTop: 8, fontSize: 10, color: 'var(--text-3)' }}>
              Пропущено ({multiProgress.skipped.length}):{' '}
              {multiProgress.skipped.slice(0, 5).map(s => `${s.phone || s.acc_id} (${s.reason})`).join(', ')}
              {multiProgress.skipped.length > 5 ? '…' : ''}
            </div>
          )}
        </div>
      )}

      {/* Spider progress banner */}
      {spiderProgress && (
        <div style={{
          marginBottom: 16, padding: '12px 16px', borderRadius: 12,
          background: spiderProgress.status === 'running'
            ? 'rgba(0,194,178,0.06)'
            : spiderProgress.status === 'done'
            ? 'rgba(61,214,140,0.06)'
            : 'rgba(248,81,73,0.06)',
          border: `1px solid ${spiderProgress.status === 'running' ? 'rgba(0,194,178,0.2)' : spiderProgress.status === 'done' ? 'rgba(61,214,140,0.2)' : 'rgba(248,81,73,0.2)'}`,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: spiderProgress.status === 'running' ? 8 : 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ fontSize: 15 }}>{spiderProgress.status === 'running' ? '🕷' : spiderProgress.status === 'done' ? '✅' : '❌'}</span>
              <span style={{ fontWeight: 700, fontSize: 13, color: spiderProgress.status === 'running' ? '#00c2b2' : spiderProgress.status === 'done' ? 'var(--green)' : 'var(--red)' }}>
                {spiderProgress.status === 'running' ? 'Паутинка работает...'
                  : spiderProgress.status === 'done' ? 'Паутинка завершена'
                  : 'Ошибка паутинки'}
              </span>
              <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
                Найдено: {spiderProgress.found} &nbsp;·&nbsp; Сохранено: {spiderProgress.saved}
                {spiderProgress.status === 'running' && <> &nbsp;·&nbsp; В очереди: {spiderProgress.queue}</>}
              </span>
            </div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              {spiderProgress.status === 'running' && (
                <Button variant="danger" size="sm" onClick={handleSpiderStop}>⏹ Стоп</Button>
              )}
              {spiderProgress.status !== 'running' && (
                <button onClick={() => setSpiderProgress(null)} style={{ background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer', fontSize: 16 }}>✕</button>
              )}
            </div>
          </div>
          {spiderProgress.status === 'running' && spiderProgress.current && (
            <div style={{ fontSize: 11, color: 'var(--text-3)', paddingLeft: 25 }}>
              Сейчас: <span style={{ color: '#00c2b2' }}>@{spiderProgress.current}</span>
            </div>
          )}
        </div>
      )}

      {/* Multi-паутинка progress banner */}
      {spiderMultiProgress && spiderMultiProgress.status !== 'idle' && (
        <div style={{
          marginBottom: 16, padding: '12px 16px', borderRadius: 12,
          background: spiderMultiProgress.status === 'running'
            ? 'rgba(0,194,178,0.08)'
            : spiderMultiProgress.status === 'done'
            ? 'rgba(61,214,140,0.06)'
            : 'rgba(248,81,73,0.06)',
          border: `1px solid ${spiderMultiProgress.status === 'running' ? 'rgba(0,194,178,0.3)' : spiderMultiProgress.status === 'done' ? 'rgba(61,214,140,0.2)' : 'rgba(248,81,73,0.2)'}`,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8, gap: 8, flexWrap: 'wrap' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
              <span style={{ fontSize: 15 }}>
                {spiderMultiProgress.status === 'running' ? '🕷'
                  : spiderMultiProgress.status === 'done' ? '✅'
                  : spiderMultiProgress.status === 'cancelled' ? '⏹'
                  : '❌'}
              </span>
              <span style={{ fontWeight: 700, fontSize: 13,
                color: spiderMultiProgress.status === 'running' ? '#00c2b2'
                     : spiderMultiProgress.status === 'done' ? 'var(--green)'
                     : 'var(--red)' }}>
                Multi-паутинка
                {spiderMultiProgress.concurrency
                  ? ` · ${spiderMultiProgress.concurrency} воркер(ов) одновременно`
                  : ''}
              </span>
              <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
                Найдено: <b>{spiderMultiProgress.found_total || 0}</b>
                &nbsp;·&nbsp; В БД: <b>{spiderMultiProgress.saved_total || 0}</b>
                &nbsp;·&nbsp; Очередь: {spiderMultiProgress.queue_size || 0}
                &nbsp;·&nbsp; Акков: {spiderMultiProgress.total_accounts || 0}
              </span>
            </div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              {spiderMultiProgress.status === 'running' && (
                <>
                  <Button variant="danger" size="sm" onClick={handleSpiderMultiStop}>⏹ Стоп</Button>
                  <Button variant="danger" size="sm" onClick={handleSpiderMultiForceReset}
                    style={{ background: '#7f1d1d', borderColor: '#dc2626' }}
                    title="SIGTERM в celery + очистка Redis. Если Стоп не сработал.">
                    🚨 Force reset
                  </Button>
                </>
              )}
              {spiderMultiProgress.status !== 'running' && (
                <button onClick={handleSpiderMultiClose} style={{ background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer', fontSize: 16 }}>✕</button>
              )}
            </div>
          </div>

          {(spiderMultiProgress.status === 'error' || spiderMultiProgress.stale) && (
            <div style={{
              marginBottom: 8, padding: '8px 10px', borderRadius: 6,
              background: 'rgba(248,81,73,0.10)', border: '1px solid rgba(248,81,73,0.35)',
              fontSize: 12, color: 'var(--red)',
            }}>
              {spiderMultiProgress.stale ? '⚠ Воркер умер — задача прервана. ' : ''}
              {spiderMultiProgress.error || 'Ошибка выполнения. Запусти заново.'}
            </div>
          )}

          {/* Per-account grid */}
          {spiderMultiProgress.accounts && Object.keys(spiderMultiProgress.accounts).length > 0 && (
            <div style={{
              display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))',
              gap: 6, marginTop: 6,
            }}>
              {Object.entries(spiderMultiProgress.accounts).map(([accId, acc]) => {
                const icon = {
                  queued: '⋯', running: '🕷', done: '✅',
                  flooded: '⏰', cancelled: '⏹',
                  not_authorized: '🚫', no_session: '🚫',
                  connect_fail: '❌',
                  busy_other: '🔒', yielded: '↪',
                }[acc.status] || '·'
                const color = {
                  running: 'rgba(0,194,178,0.18)',
                  done: 'rgba(61,214,140,0.15)',
                  flooded: 'rgba(255,152,0,0.18)',
                  cancelled: 'rgba(150,150,150,0.15)',
                  busy_other: 'rgba(100,116,139,0.20)',
                  yielded: 'rgba(100,116,139,0.20)',
                }[acc.status] || 'rgba(255,255,255,0.04)'
                return (
                  <div key={accId} style={{
                    padding: '6px 9px', borderRadius: 8,
                    background: color, fontSize: 11,
                    border: '1px solid rgba(255,255,255,0.05)',
                  }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span style={{ fontWeight: 600 }}>{icon} {acc.phone}</span>
                      <span style={{ color: 'var(--text-3)' }}>
                        {acc.saved || 0}/{acc.found || 0}
                      </span>
                    </div>
                    <div style={{ color: 'var(--text-3)', fontSize: 10, marginTop: 2 }}>
                      {acc.status === 'running' && acc.current_seed && (
                        <>@{acc.current_seed} · d{acc.depth} · очередь {acc.queue_size}</>
                      )}
                      {acc.status === 'flooded' && (<>⏰ Flood {acc.flood_wait}с → пауза ≥1ч</>)}
                      {acc.status === 'queued' && (<>ждёт · {acc.seeds_total} seeds</>)}
                      {acc.status === 'done' && (<>готово · {acc.seeds_done} seeds</>)}
                      {acc.status === 'cancelled' && (<>остановлено</>)}
                      {acc.status === 'not_authorized' && (<>не авторизован</>)}
                      {acc.status === 'connect_fail' && (<>ошибка подключения</>)}
                      {acc.status === 'busy_other' && (<>🔒 занят warmup/comment/refix</>)}
                      {acc.status === 'yielded' && (<>↪ передан warmup/comment</>)}
                    </div>
                  </div>
                )
              })}
            </div>
          )}

          {Array.isArray(spiderMultiProgress.skipped) && spiderMultiProgress.skipped.length > 0 && (
            <div style={{ marginTop: 8, fontSize: 10, color: 'var(--text-3)' }}>
              Пропущено ({spiderMultiProgress.skipped.length}):{' '}
              {spiderMultiProgress.skipped.slice(0, 5).map(s => `${s.phone || s.acc_id} (${s.reason})`).join(', ')}
              {spiderMultiProgress.skipped.length > 5 ? '…' : ''}
            </div>
          )}
        </div>
      )}

      {/* Verify progress banner */}
      {verifyProgress && (
        <CheckBanner
          icon="💬" label="Проверка комментариев"
          status={verifyProgress.status} current={verifyProgress.current}
          line={`Проверено: ${verifyProgress.checked} · С комментами: ${verifyProgress.with_comments}${verifyProgress.status === 'running' ? ` · Осталось: ${verifyProgress.remaining}` : ''}`}
          color="var(--blue)"
          onStop={handleVerifyStop} onDismiss={() => setVerifyProgress(null)}
        />
      )}

      {/* Alive progress banner */}
      {aliveProgress && (
        <CheckBanner
          icon="🩺" label="Проверка живости"
          status={aliveProgress.status} current={aliveProgress.current}
          line={`Проверено: ${aliveProgress.checked} · Живых: ${aliveProgress.alive}${aliveProgress.status === 'running' ? ` · Осталось: ${aliveProgress.remaining}` : ''}`}
          color="#ffb020"
          onStop={handleAliveStop} onDismiss={() => setAliveProgress(null)}
        />
      )}

      {pageTab === 'channels' && (channels.length === 0 ? (
        <Empty icon="🔍" title="База каналов пуста" subtitle="Запустите поиск или импортируйте список" action={<Button variant="primary" onClick={() => setSearchModal(true)}>🔍 Поиск</Button>} />
      ) : (
        <>
          {/* Порядок списка. По умолчанию крупные сверху, но паутинка
              приносит каналы от 50 тысяч, а на первой странице по размеру
              стоят от 420 тысяч — свежих находок так не увидеть. */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 10 }}>
            <span style={{ fontSize: 10, color: 'var(--text-3)', fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase', minWidth: 60 }}>↕ Порядок</span>
            {[['subs', 'Крупные сверху'], ['new', 'Свежие находки']].map(([v, label]) => (
              <button key={v} onClick={() => setSortBy(v)} style={{
                padding: '5px 12px', borderRadius: 20, cursor: 'pointer', fontSize: 11, transition: 'all 0.15s',
                fontWeight: sortBy === v ? 600 : 400,
                border: `1px solid ${sortBy === v ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                background: sortBy === v ? 'rgba(124,77,255,0.15)' : 'transparent',
                color: sortBy === v ? 'var(--violet)' : 'var(--text-3)',
              }}>{label}</button>
            ))}
          </div>

          {/* Папки — чипы */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', marginBottom: 12 }}>
            <span style={{ fontSize: 10, color: 'var(--text-3)', fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase', minWidth: 60 }}>📁 Папки</span>
            <button onClick={() => setFilterFolder('all')} style={{
              padding: '5px 12px', borderRadius: 20, cursor: 'pointer', fontSize: 11, transition: 'all 0.15s',
              fontWeight: filterFolder === 'all' ? 600 : 400,
              border: `1px solid ${filterFolder === 'all' ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
              background: filterFolder === 'all' ? 'rgba(124,77,255,0.15)' : 'transparent',
              color: filterFolder === 'all' ? 'var(--violet)' : 'var(--text-3)',
            }}>Все ({(dbStats?.total ?? channels.length).toLocaleString('ru')})</button>
            <button onClick={() => setFilterFolder('')} style={{
              padding: '5px 12px', borderRadius: 20, cursor: 'pointer', fontSize: 11, transition: 'all 0.15s',
              fontWeight: filterFolder === '' ? 600 : 400,
              border: `1px solid ${filterFolder === '' ? 'rgba(255,180,0,0.4)' : 'var(--border)'}`,
              background: filterFolder === '' ? 'rgba(255,180,0,0.12)' : 'transparent',
              color: filterFolder === '' ? 'var(--yellow)' : 'var(--text-3)',
            }}>Без папки ({channels.filter(c => !c.folder).length})</button>
            {folders.map(f => (
              <div key={f.name} style={{
                display: 'inline-flex', alignItems: 'stretch',
                borderRadius: 20, overflow: 'hidden',
                border: `1px solid ${filterFolder === f.name ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
              }}>
                <button onClick={() => setFilterFolder(filterFolder === f.name ? 'all' : f.name)} style={{
                  padding: '5px 12px', cursor: 'pointer', fontSize: 11, transition: 'all 0.15s',
                  fontWeight: filterFolder === f.name ? 600 : 400,
                  border: 'none',
                  background: filterFolder === f.name ? 'rgba(124,77,255,0.15)' : 'transparent',
                  color: filterFolder === f.name ? 'var(--violet)' : 'var(--text-3)',
                }}>{f.name} ({f.count})</button>
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    setExportForm({
                      folder: f.name, only_with_comments: false, min_subscribers: 0, format: 'csv',
                    })
                    setExportModal(true)
                  }}
                  title={`Скачать «${f.name}» (${f.count} каналов${f.with_comments ? `, ${f.with_comments} с комментами` : ''})`}
                  style={{
                    padding: '5px 8px', cursor: 'pointer', fontSize: 11,
                    background: 'transparent', color: 'var(--text-3)',
                    border: 'none', borderLeft: '1px solid var(--border)',
                  }}
                >📥</button>
              </div>
            ))}
            {!newFolderInput ? (
              <button onClick={() => setNewFolderInput(true)} style={{
                padding: '5px 10px', borderRadius: 20, cursor: 'pointer', fontSize: 11,
                border: '1px dashed var(--border)', background: 'transparent', color: 'var(--text-3)',
              }}>+ Создать</button>
            ) : (
              <form onSubmit={e => { e.preventDefault(); const v = e.target.fname.value.trim(); if (v) setFolderName(v); setNewFolderInput(false); if (v) setFolderModal(true) }} style={{ display: 'flex', gap: 4 }}>
                <input name="fname" autoFocus placeholder="Имя папки..." style={{
                  padding: '4px 10px', borderRadius: 20, border: '1px solid rgba(124,77,255,0.4)', background: 'rgba(124,77,255,0.08)',
                  color: 'var(--text)', fontSize: 11, outline: 'none', width: 120,
                }} />
                <button type="submit" style={{ padding: '4px 8px', borderRadius: 20, border: '1px solid rgba(61,214,140,0.4)', background: 'rgba(61,214,140,0.1)', color: 'var(--green)', fontSize: 11, cursor: 'pointer' }}>✓</button>
                <button type="button" onClick={() => setNewFolderInput(false)} style={{ padding: '4px 8px', borderRadius: 20, border: '1px solid var(--border)', background: 'transparent', color: 'var(--text-3)', fontSize: 11, cursor: 'pointer' }}>✕</button>
              </form>
            )}
          </div>

          {/* Toolbar */}
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <button onClick={selectAll} style={{ padding: '5px 12px', borderRadius: 8, border: '1px solid var(--border)', background: 'transparent', color: 'var(--text-3)', fontSize: 11, cursor: 'pointer' }}>
                {selectedChannels.length === filteredChannels.length && filteredChannels.length > 0 ? '☑ Снять всё' : '☐ Выбрать всё'}
              </button>
              {selectedChannels.length > 0 && (
                <Button variant="outline" size="sm" onClick={() => { setFolderName(''); setFolderModal(true) }}>
                  📁 В папку ({selectedChannels.length})
                </Button>
              )}
            </div>
            <Button variant="ghost" size="sm" onClick={handleClear}>🗑 Очистить всё</Button>
          </div>

          {/* Channels list */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {filteredChannels.slice(0, visibleCount).map(c => (
              <Card key={c.id} style={{ padding: '10px 14px', borderLeft: selectedChannels.includes(c.id) ? '3px solid var(--violet)' : '3px solid transparent' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <input type="checkbox" checked={selectedChannels.includes(c.id)} onChange={() => toggleSelect(c.id)}
                    style={{ accentColor: 'var(--violet)', cursor: 'pointer' }} onClick={e => e.stopPropagation()} />
                  <div style={{ flex: 1 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 2 }}>
                      <span style={{ fontWeight: 700, fontSize: 13 }}>@{c.username}</span>
                      <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{c.title}</span>
                      {/* Статус проверки на комментарии (2026-08-24).
                          Раньше был только зелёный значок при has_comments, и
                          «не проверен» выглядел ровно как «проверен, комментов
                          нет». Теперь три разных состояния. */}
                      {!c.checked && (
                        <span title="Канал ещё не проверяли на комментарии"
                          style={{ fontSize: 10, padding: '1px 8px', borderRadius: 10,
                            background: 'rgba(255,170,0,0.12)', color: 'rgba(255,170,0,0.95)',
                            border: '1px solid rgba(255,170,0,0.3)', fontWeight: 600 }}>
                          ❓ не проверен
                        </span>
                      )}
                      {c.checked && c.cap_commentable === true && (
                        <Badge color="green" title={c.cap_join_to_send
                          ? 'Комментировать можно, но нужно вступить в обсуждение'
                          : 'Комментировать можно сразу'}>
                          💬{c.cap_join_to_send ? '↩' : ''}
                        </Badge>
                      )}
                      {c.checked && c.cap_commentable === false && (
                        <span title={`Комментировать нельзя: ${c.cap_reason}`}
                          style={{ fontSize: 10, padding: '1px 8px', borderRadius: 10,
                            background: 'rgba(255,80,80,0.10)', color: 'rgba(255,120,120,0.95)',
                            border: '1px solid rgba(255,80,80,0.25)' }}>
                          🚫 {{
                            no_discussion: 'нет обсуждений',
                            needs_approval: 'по заявке',
                            write_banned: 'писать нельзя',
                            username_not_found: 'канала нет',
                          }[c.cap_reason] || c.cap_reason}
                        </span>
                      )}
                      {/* Живучесть комментов (2026-08-27). Значок «можно
                          комментировать» отвечает лишь на вопрос, пустят ли
                          писать. Пустить могут, а через десять минут снять
                          модерацией — и снаружи это выглядело одинаково.
                          Показываем долю выживших там, где уже есть вердикт
                          хотя бы по одному комменту. */}
                      {c.survival_rate !== null && c.survival_rate !== undefined && (
                        <span title={`Написано ${c.comments_written}, живы ${c.comments_alive}, удалено ${c.comments_deleted}`}
                          style={{ fontSize: 10, padding: '1px 8px', borderRadius: 10, fontWeight: 600,
                            background: c.survival_rate >= 80 ? 'rgba(61,214,140,0.12)'
                                      : c.survival_rate >= 50 ? 'rgba(255,180,0,0.12)'
                                      : 'rgba(248,81,73,0.12)',
                            color: c.survival_rate >= 80 ? 'var(--green)'
                                 : c.survival_rate >= 50 ? 'var(--yellow)' : 'var(--red)',
                            border: `1px solid ${c.survival_rate >= 80 ? 'rgba(61,214,140,0.25)'
                                              : c.survival_rate >= 50 ? 'rgba(255,180,0,0.25)'
                                              : 'rgba(248,81,73,0.25)'}` }}>
                          🌱 {Math.round(c.survival_rate)}%
                          {c.comments_deleted > 0 ? ` (−${c.comments_deleted})` : ''}
                        </span>
                      )}
                      {c.folder && <span style={{ fontSize: 10, padding: '1px 8px', borderRadius: 10, background: 'rgba(124,77,255,0.1)', color: 'rgba(124,77,255,0.8)', border: '1px solid rgba(124,77,255,0.2)' }}>📁 {c.folder}</span>}
                    </div>
                    <div style={{ display: 'flex', gap: 14, fontSize: 10, color: 'var(--text-3)' }}>
                      <span>👥 {c.subscribers?.toLocaleString()}</span>
                      {c.last_post_date && <span>📅 {new Date(c.last_post_date).toLocaleDateString('ru')}</span>}
                      <span>🔍 {c.search_query}</span>
                      {c.comments_written > 0 && <span>✍ написано {c.comments_written}</span>}
                    </div>
                  </div>
                  <button onClick={() => parserAPI.delete(c.id).then(load)} style={{ background: 'none', border: 'none', color: 'var(--red)', cursor: 'pointer', fontSize: 14 }}>✕</button>
                </div>
              </Card>
            ))}

            {filteredChannels.length <= visibleCount && channels.length < totalChannels && (
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center',
                            gap: 12, padding: '14px 0' }}>
                <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
                  загружено {channels.length.toLocaleString('ru')} из {totalChannels.toLocaleString('ru')}
                </span>
                <Button size="sm" variant="ghost" disabled={loadingMore}
                  onClick={async () => { await loadMore(); setVisibleCount(v => v + PAGE_SIZE) }}>
                  {loadingMore ? 'Гружу…' : `Загрузить ещё ${PAGE_SIZE}`}
                </Button>
              </div>
            )}

            {filteredChannels.length > visibleCount && (
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center',
                            gap: 12, padding: '14px 0' }}>
                <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
                  показано {visibleCount} из {filteredChannels.length.toLocaleString('ru')}
                </span>
                <Button size="sm" variant="ghost"
                  onClick={() => setVisibleCount(v => v + PAGE_SIZE)}>
                  Показать ещё {PAGE_SIZE}
                </Button>
                <Button size="sm" variant="ghost"
                  onClick={() => setVisibleCount(filteredChannels.length)}>
                  Показать все
                </Button>
              </div>
            )}
          </div>
        </>
      ))}

      {/* Whitelist / Проходимость tab */}
      {pageTab === 'whitelist' && (
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
            <div style={{ flex: 1 }}>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Мин. проходимость (%)</label>
              <input type="range" min={0} max={100} step={5} value={wlMinRate * 100} onChange={e => {
                const v = parseInt(e.target.value) / 100
                setWlMinRate(v)
                loadWhitelist(wlSortBy, v)
              }} style={{ width: 180 }} />
              <span style={{ marginLeft: 8, fontSize: 12, color: 'var(--text-2)' }}>{Math.round(wlMinRate * 100)}%</span>
            </div>
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Сортировка</label>
              <select value={wlSortBy} onChange={e => { setWlSortBy(e.target.value); loadWhitelist(e.target.value, wlMinRate) }} style={{ padding: '8px 12px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 12, outline: 'none' }}>
                <option value="pass_rate">По проходимости</option>
                <option value="attempts">По попыткам</option>
                <option value="last_ban">По последнему бану</option>
              </select>
            </div>
            <div style={{ alignSelf: 'flex-end', display: 'flex', gap: 6 }}>
              <Button variant="ghost" size="sm" onClick={() => loadWhitelist()}>↺ Обновить</Button>
              <Button variant="danger" size="sm" onClick={async () => {
                if (!window.confirm(
                  '🗑 Очистить ВСЮ проходимость?\n\n' +
                  'Удалятся все ChannelBanStats — все текущие показатели pass_rate ' +
                  'обнулятся. Полезно если данные там нерелевантные ' +
                  '(старые прогоны без auto-verify).\n\n' +
                  'После очистки: следующий раз verify-механизм начнёт считать с нуля.'
                )) return
                try {
                  const { data } = await parserAPI.whitelistWipe()
                  showToast(`Очищено ${data.deleted} записей`)
                  loadWhitelist()
                } catch (err) { showToast(err.response?.data?.detail || 'Ошибка', 'error') }
              }}>🗑 Очистить всё</Button>
            </div>
          </div>

          {wlLoading ? (
            <div style={{ textAlign: 'center', padding: 40, color: 'var(--text-3)' }}>Загрузка...</div>
          ) : whitelist.length === 0 ? (
            <Empty icon="📊" title="Нет данных о проходимости" subtitle="Проходимость считается автоматически в процессе комментинга" />
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {whitelist.map(ch => {
                const pct = Math.round(ch.pass_rate ?? 0)
                const rateColor = pct >= 70 ? 'var(--green)' : pct >= 40 ? 'var(--yellow)' : 'var(--red)'
                return (
                  <Card key={ch.id} style={{ padding: '10px 14px' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                      <div style={{ flex: 1 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
                          <span style={{ fontWeight: 700, fontSize: 13 }}>@{ch.channel_username}</span>
                          <span style={{
                            fontSize: 12, fontWeight: 700, padding: '2px 10px', borderRadius: 20,
                            background: pct >= 70 ? 'rgba(61,214,140,0.12)' : pct >= 40 ? 'rgba(255,180,0,0.12)' : 'rgba(248,81,73,0.12)',
                            color: rateColor,
                            border: `1px solid ${pct >= 70 ? 'rgba(61,214,140,0.25)' : pct >= 40 ? 'rgba(255,180,0,0.25)' : 'rgba(248,81,73,0.25)'}`,
                          }}>{pct}%</span>
                        </div>
                        <div style={{ display: 'flex', gap: 16, fontSize: 11, color: 'var(--text-3)', flexWrap: 'wrap' }}>
                          <span>✅ Успешно: {ch.total_attempts - (ch.banned_count || 0) - (ch.skipped_count || 0)}</span>
                          <span>🚫 Баны: {ch.banned_count || 0}</span>
                          {(ch.skipped_count || 0) > 0 && (
                            <span title={ch.last_skip_reason || ''} style={{ color: 'var(--yellow)' }}>
                              ⏭ Скипы: {ch.skipped_count}
                              {ch.last_skip_reason ? ` (${ch.last_skip_reason})` : ''}
                            </span>
                          )}
                          <span>📊 Всего: {ch.total_attempts}</span>
                          {ch.last_ban_reason && <span>💬 {ch.last_ban_reason}</span>}
                        </div>
                      </div>
                      {/* Pass-rate bar */}
                      <div style={{ width: 80 }}>
                        <div style={{ height: 6, background: 'rgba(255,255,255,0.06)', borderRadius: 3, overflow: 'hidden' }}>
                          <div style={{ height: '100%', width: `${pct}%`, borderRadius: 3, background: rateColor, transition: 'width 0.4s ease' }} />
                        </div>
                      </div>
                      <button onClick={() => handleWlDelete(ch.id)} style={{ background: 'none', border: 'none', color: 'var(--red)', cursor: 'pointer', fontSize: 14, padding: 4 }}>✕</button>
                    </div>
                  </Card>
                )
              })}
            </div>
          )}
        </div>
      )}

      {/* Metrics tab */}
      {pageTab === 'metrics' && <ParserMetrics />}

      {/* Web-scraper tab (Camoufox + пул прокси) */}
      {pageTab === 'web' && <WebScraperPanel />}

      {/* Folder assignment modal */}
      <Modal open={folderModal} onClose={() => setFolderModal(false)} title="📁 Назначить папку" width={400}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ fontSize: 13, color: 'var(--text-2)' }}>
            Выбрано каналов: <strong>{selectedChannels.length}</strong>
          </div>
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, display: 'block', marginBottom: 6 }}>Папка</label>
            <input list="folder-list" value={folderName} onChange={e => setFolderName(e.target.value)}
              placeholder="Введи или выбери папку" autoFocus
              style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 13, outline: 'none' }} />
            <datalist id="folder-list">{folders.map(f => <option key={f.name} value={f.name} />)}</datalist>
          </div>
          {/* Quick buttons for existing folders */}
          {folders.length > 0 && (
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              {folders.map(f => (
                <button key={f.name} onClick={() => setFolderName(f.name)} style={{
                  padding: '5px 12px', borderRadius: 20, fontSize: 11, cursor: 'pointer',
                  border: `1px solid ${folderName === f.name ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                  background: folderName === f.name ? 'rgba(124,77,255,0.15)' : 'transparent',
                  color: folderName === f.name ? 'var(--violet)' : 'var(--text-3)',
                }}>{f.name}</button>
              ))}
            </div>
          )}
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" onClick={() => setFolderModal(false)}>Отмена</Button>
            <Button variant="primary" disabled={!folderName.trim()} onClick={() => handleSetFolder(folderName.trim())}>
              📁 Назначить
            </Button>
          </div>
        </div>
      </Modal>

      {/* Search Modal */}
      <Modal open={searchModal} onClose={() => setSearchModal(false)} title="Поиск каналов" width={560}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 6 }}>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
                Аккаунты · выбрано {form.account_ids.length}/{accounts.length}
              </label>
              <div style={{ display: 'flex', gap: 8, fontSize: 11 }}>
                <button type="button" onClick={() => {
                  const notCd = accounts.filter(a => !accCooldowns[a.id]).map(a => a.id)
                  setForm(f => ({ ...f, account_ids: notCd }))
                }} style={{ background: 'none', border: 'none', color: 'var(--violet)', cursor: 'pointer', textDecoration: 'underline' }}>все ✓</button>
                <button type="button" onClick={() => setForm(f => ({ ...f, account_ids: [] }))} style={{ background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer', textDecoration: 'underline' }}>сброс</button>
              </div>
            </div>
            <div style={{
              maxHeight: 200, overflowY: 'auto',
              padding: 6, borderRadius: 10, border: '1px solid var(--border)',
              background: 'var(--bg-3)',
              display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 4,
            }}>
              {accounts.length === 0 && (
                <div style={{ padding: 10, fontSize: 12, color: 'var(--text-3)', textAlign: 'center' }}>Нет активных аккаунтов</div>
              )}
              {accounts.map(a => {
                const on = form.account_ids.includes(a.id)
                const cd = accCooldowns[a.id]
                const cdMin = cd ? Math.ceil(cd.seconds_left / 60) : 0
                return (
                  <label key={a.id} style={{
                    display: 'flex', alignItems: 'center', gap: 6, padding: '5px 8px',
                    borderRadius: 6, cursor: cd ? 'default' : 'pointer',
                    background: on ? 'rgba(124,77,255,0.15)' : 'transparent',
                    border: `1px solid ${on ? 'rgba(124,77,255,0.35)' : 'transparent'}`,
                    fontSize: 12,
                    opacity: cd ? 0.55 : 1,
                  }}>
                    <input type="checkbox" checked={on} disabled={!!cd}
                      onChange={e => setForm(f => ({
                        ...f,
                        account_ids: e.target.checked
                          ? [...f.account_ids, a.id]
                          : f.account_ids.filter(x => x !== a.id),
                      }))}
                      style={{ accentColor: 'var(--violet)' }} />
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {a.first_name || a.phone}
                    </span>
                    {cd && (
                      <span title={`FloodWait cooldown — ещё ${cdMin} мин`}
                        style={{ fontSize: 10, color: 'var(--orange, #ff9800)', cursor: 'pointer' }}
                        onClick={(e) => { e.preventDefault(); handleClearCooldown(a.id) }}
                      >
                        ⏰ {cdMin}м
                      </span>
                    )}
                  </label>
                )
              })}
            </div>
            <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 4 }}>
              💡 Multi-режим: случайно 2–7 акков одновременно (по разным прокси).
              При FloodWait акк уходит на перекур ≥1 час без ретраев.
              Клик на «⏰ Nм» — сброс cooldown вручную.
            </div>
          </div>

          <Input label="Ключевые слова (через запятую)" value={form.keywords} onChange={e => setForm(f => ({ ...f, keywords: e.target.value }))} placeholder="криптовалюта, крипта, блокчейн" />

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <Input label="Мин. подписчиков" type="number" value={form.min_subscribers} onChange={e => setForm(f => ({ ...f, min_subscribers: parseInt(e.target.value) || 0 }))} />
            <Input label="Макс. подписчиков" type="number" value={form.max_subscribers} onChange={e => setForm(f => ({ ...f, max_subscribers: parseInt(e.target.value) || 0 }))} />
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, cursor: 'pointer' }}>
              <input type="checkbox" checked={form.only_with_comments} onChange={e => setForm(f => ({ ...f, only_with_comments: e.target.checked }))} />
              Только с комментариями
            </label>
            <Input label="Посты за последние (часов)" type="number" value={form.active_hours} onChange={e => setForm(f => ({ ...f, active_hours: parseInt(e.target.value) || 0 }))} />
          </div>

          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Где искать</label>
            <select value={form.source} onChange={e => setForm(f => ({ ...f, source: e.target.value }))} style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 14, outline: 'none' }}>
              <option value="telegram">📱 Telegram (через аккаунт)</option>
              <option value="tgstat">📊 TGStat API</option>
              <option value="both">🔍 Оба источника</option>
            </select>
          </div>



          <div style={{ padding: '10px 14px', background: 'var(--bg-3)', borderRadius: 10, fontSize: 11, color: 'var(--text-3)', lineHeight: 1.6 }}>
            💡 Поиск через Telegram API + TGStat (если задан TGSTAT_API_KEY в .env).
            Бесплатный ключ TGStat: <a href="https://api.tgstat.ru" target="_blank" style={{ color: 'var(--violet)' }}>api.tgstat.ru</a>
          </div>

          {/* ── Geo keyword expansion ── */}
          <div style={{ border: `1px solid ${geoExpand ? 'rgba(124,77,255,0.35)' : 'var(--border)'}`, borderRadius: 12, overflow: 'hidden', transition: 'border-color 0.2s' }}>
            <button onClick={() => setGeoExpand(v => !v)} style={{
              width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              padding: '10px 14px', background: geoExpand ? 'rgba(124,77,255,0.08)' : 'var(--bg-3)',
              border: 'none', cursor: 'pointer', transition: 'background 0.2s',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ fontSize: 14 }}>🌐</span>
                <span style={{ fontSize: 13, fontWeight: 700, color: geoExpand ? 'var(--violet)' : 'var(--text-2)' }}>Расширить по языкам</span>
                {geoExpand && expandPreview && (
                  <span style={{ fontSize: 11, padding: '2px 8px', borderRadius: 10, background: 'rgba(124,77,255,0.15)', color: 'var(--violet)', fontWeight: 600 }}>
                    ~{expandPreview.total} ключей
                  </span>
                )}
              </div>
              <span style={{ fontSize: 12, color: 'var(--text-3)' }}>{geoExpand ? '▲' : '▼'}</span>
            </button>

            {geoExpand && (
              <div style={{ padding: '12px 14px', borderTop: '1px solid var(--border)', display: 'flex', flexDirection: 'column', gap: 12 }}>

                {/* Пресеты */}
                <div>
                  <div style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: 6 }}>Пресеты</div>
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    {[
                      { key: 'cis',       label: '🤝 СНГ',       geos: ['ru', 'ua'] },
                      { key: 'ua_ru_en',  label: '🇺🇦🇷🇺🇺🇸 RU+UA+EN', geos: ['ua', 'ru', 'en'] },
                      { key: 'europe',    label: '🌍 Европа',    geos: ['en', 'de', 'fr', 'it', 'es', 'pl', 'nl', 'pt'] },
                      { key: 'latam',     label: '🌎 Латам',     geos: ['es', 'pt'] },
                      { key: 'asia',      label: '🌏 Азия',      geos: ['hi', 'id', 'vi', 'zh', 'ja', 'ko', 'th'] },
                      { key: 'mena',      label: '🌙 MENA',      geos: ['ar', 'fa', 'tr'] },
                      { key: 'global_en', label: '🌐 English',   geos: ['en'] },
                    ].map(p => {
                      const isActive = p.geos.every(g => selectedGeos.includes(g)) && p.geos.length === selectedGeos.length
                      return (
                        <button key={p.key} onClick={() => {
                          setSelectedGeos(p.geos)
                          setExpandPreview(null)
                          if (form.keywords.trim()) {
                            const seeds = form.keywords.split(',').map(s => s.trim()).filter(Boolean)
                            previewExpand(seeds, p.geos, expandOpts)
                          }
                        }} style={{
                          padding: '5px 12px', borderRadius: 20, fontSize: 11, cursor: 'pointer', transition: 'all 0.15s',
                          background: isActive ? 'rgba(124,77,255,0.18)' : 'var(--bg-card)',
                          border: `1px solid ${isActive ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                          color: isActive ? 'var(--violet)' : 'var(--text-2)', fontWeight: isActive ? 700 : 400,
                        }}>{p.label}</button>
                      )
                    })}
                  </div>
                </div>

                {/* Языки */}
                <div>
                  <div style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: 6 }}>
                    Языки <span style={{ fontWeight: 400, textTransform: 'none', letterSpacing: 0 }}>({selectedGeos.length} выбрано)</span>
                  </div>
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    {(availableGeos.length > 0 ? availableGeos : [
                      {code:'ru',name:'Русский'},{code:'en',name:'English'},{code:'ua',name:'Українська'},
                      {code:'de',name:'Deutsch'},{code:'fr',name:'Français'},{code:'es',name:'Español'},
                      {code:'pt',name:'Português'},{code:'it',name:'Italiano'},{code:'pl',name:'Polski'},
                      {code:'nl',name:'Nederlands'},{code:'tr',name:'Türkçe'},{code:'ar',name:'العربية'},
                      {code:'fa',name:'فارسی'},{code:'hi',name:'हिन्दी'},{code:'id',name:'Indonesia'},
                      {code:'zh',name:'中文'},{code:'ja',name:'日本語'},{code:'ko',name:'한국어'},
                    ]).map(g => {
                      const on = selectedGeos.includes(g.code)
                      return (
                        <button key={g.code} onClick={() => {
                          const next = on ? selectedGeos.filter(x => x !== g.code) : [...selectedGeos, g.code]
                          setSelectedGeos(next)
                          setExpandPreview(null)
                          if (form.keywords.trim() && next.length) {
                            const seeds = form.keywords.split(',').map(s => s.trim()).filter(Boolean)
                            previewExpand(seeds, next, expandOpts)
                          }
                        }} style={{
                          padding: '4px 10px', borderRadius: 16, fontSize: 11, cursor: 'pointer', transition: 'all 0.12s',
                          background: on ? 'rgba(61,139,255,0.15)' : 'transparent',
                          border: `1px solid ${on ? 'rgba(61,139,255,0.4)' : 'var(--border)'}`,
                          color: on ? 'var(--blue)' : 'var(--text-3)', fontWeight: on ? 700 : 400,
                        }}>{g.code.toUpperCase()} <span style={{ opacity: 0.6 }}>{g.name}</span></button>
                      )
                    })}
                  </div>
                </div>

                {/* Опции расширения */}
                <div>
                  <div style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: 6 }}>Что добавлять</div>
                  <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                    {[
                      { key: 'include_translations',     label: '🌐 Переводы' },
                      { key: 'include_translit',         label: '🔤 Транслит' },
                      { key: 'include_topic_synonyms',   label: '🧠 Синонимы' },
                      { key: 'include_geo_variants',     label: '📍 Гео-варианты' },
                      { key: 'include_prefixes_suffixes',label: '🔗 Суффиксы' },
                    ].map(opt => {
                      const on = expandOpts[opt.key]
                      return (
                        <label key={opt.key} style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 12, cursor: 'pointer', color: on ? 'var(--text)' : 'var(--text-3)' }}>
                          <input type="checkbox" checked={on} onChange={e => {
                            const next = { ...expandOpts, [opt.key]: e.target.checked }
                            setExpandOpts(next)
                            if (form.keywords.trim() && selectedGeos.length) {
                              const seeds = form.keywords.split(',').map(s => s.trim()).filter(Boolean)
                              previewExpand(seeds, selectedGeos, next)
                            }
                          }} style={{ accentColor: 'var(--violet)' }} />
                          {opt.label}
                        </label>
                      )
                    })}
                  </div>
                </div>

                {/* Превью */}
                <div style={{ fontSize: 11, color: 'var(--text-3)', padding: '6px 10px', background: 'rgba(124,77,255,0.06)', borderRadius: 8, borderLeft: '3px solid rgba(124,77,255,0.3)' }}>
                  {expandPreview
                    ? `✅ Будет сформировано ~${expandPreview.total} ключевых слов → передаётся в поиск`
                    : `💡 Ключевые слова будут переведены на выбранные языки и расширены вариантами написания перед поиском`
                  }
                </div>
              </div>
            )}
          </div>

          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" onClick={() => setSearchModal(false)}>Закрыть</Button>
            <Button variant="primary" loading={searching}
              disabled={(form.account_ids.length === 0) || !form.keywords.trim()}
              onClick={handleSearch}>
              {expanding
                ? '🌐 Расширяю...'
                : searching
                  ? 'Запускаю...'
                  : geoExpand && selectedGeos.length > 0
                    ? `🌐 Расширить и найти · ${form.account_ids.length} акк`
                    : `🔍 Multi-парсинг · ${form.account_ids.length} акк`}
            </Button>
          </div>
        </div>
      </Modal>

      {/* Spider Modal */}
      <Modal open={spiderModal} onClose={() => setSpiderModal(false)} title="🕷 Парсинг паутинкой" width={520}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>

          <div style={{ padding: '10px 14px', background: 'rgba(0,194,178,0.06)', border: '1px solid rgba(0,194,178,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.7 }}>
            🕸 Программа берёт стартовые каналы, находит <em>похожие</em> и идёт дальше на указанную глубину. Быстро собирает базу родственных каналов без ключевых слов.
          </div>

          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 6 }}>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
                Аккаунты · выбрано {spiderForm.account_ids.length}/{accounts.length}
              </label>
              <div style={{ display: 'flex', gap: 8, fontSize: 11 }}>
                <button type="button" onClick={() => {
                  const notCd = accounts.filter(a => !accCooldowns[a.id]).map(a => a.id)
                  setSpiderForm(f => ({ ...f, account_ids: notCd }))
                }} style={{ background: 'none', border: 'none', color: 'var(--teal, #00c2b2)', cursor: 'pointer', textDecoration: 'underline' }}>все ✓</button>
                <button type="button" onClick={() => setSpiderForm(f => ({ ...f, account_ids: [] }))} style={{ background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer', textDecoration: 'underline' }}>сброс</button>
              </div>
            </div>
            <div style={{
              maxHeight: 180, overflowY: 'auto',
              padding: 6, borderRadius: 10, border: '1px solid var(--border)',
              background: 'var(--bg-3)',
              display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 4,
            }}>
              {accounts.length === 0 && (
                <div style={{ padding: 10, fontSize: 12, color: 'var(--text-3)', textAlign: 'center' }}>Нет активных аккаунтов</div>
              )}
              {accounts.map(a => {
                const on = spiderForm.account_ids.includes(a.id)
                const cd = accCooldowns[a.id]
                const cdMin = cd ? Math.ceil(cd.seconds_left / 60) : 0
                return (
                  <label key={a.id} style={{
                    display: 'flex', alignItems: 'center', gap: 6, padding: '5px 8px',
                    borderRadius: 6, cursor: cd ? 'default' : 'pointer',
                    background: on ? 'rgba(0,194,178,0.15)' : 'transparent',
                    border: `1px solid ${on ? 'rgba(0,194,178,0.35)' : 'transparent'}`,
                    fontSize: 12, opacity: cd ? 0.55 : 1,
                  }}>
                    <input type="checkbox" checked={on} disabled={!!cd}
                      onChange={e => setSpiderForm(f => ({
                        ...f,
                        account_ids: e.target.checked
                          ? [...f.account_ids, a.id]
                          : f.account_ids.filter(x => x !== a.id),
                      }))}
                      style={{ accentColor: 'var(--teal, #00c2b2)' }} />
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {a.first_name || a.phone}
                    </span>
                    {cd && (
                      <span title={`Cooldown ещё ${cdMin} мин`}
                        style={{ fontSize: 10, color: 'var(--orange, #ff9800)', cursor: 'pointer' }}
                        onClick={(e) => { e.preventDefault(); handleClearCooldown(a.id) }}
                      >⏰ {cdMin}м</span>
                    )}
                  </label>
                )
              })}
            </div>
            <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 4 }}>
              💡 Multi-режим: случайно 2–7 акков одновременно (по разным прокси).
              Общий Redis SET дедупа — акки не парсят одинаковые каналы.
              FloodWait → cooldown ≥1ч без ретраев.
            </div>
          </div>

          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>
              Стартовые каналы (seeds)
            </label>
            <textarea
              value={spiderForm.seeds}
              onChange={e => setSpiderForm(f => ({ ...f, seeds: e.target.value }))}
              rows={5}
              placeholder={'@crypto_news\n@bitcoin_ru\nblockchain_daily\nhttps://t.me/trading_signals'}
              style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 13, fontFamily: 'var(--font-mono)', resize: 'vertical', outline: 'none', boxSizing: 'border-box' }}
            />
            <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4 }}>По одному каналу на строку, @ необязательно</div>
          </div>

          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 8 }}>Глубина обхода</label>
            <div style={{ display: 'flex', gap: 8 }}>
              {[
                { v: 1, label: '1 уровень',  hint: 'только похожие на seeds' },
                { v: 2, label: '2 уровня',   hint: 'и похожие на похожих' },
                { v: 3, label: '3 уровня',   hint: 'ещё глубже (медленно)' },
              ].map(({ v, label, hint }) => (
                <button
                  key={v}
                  onClick={() => setSpiderForm(f => ({ ...f, max_depth: v }))}
                  style={{
                    flex: 1, padding: '10px 8px', borderRadius: 10, cursor: 'pointer', textAlign: 'center', transition: 'all 0.15s',
                    background: spiderForm.max_depth === v ? 'rgba(0,194,178,0.15)' : 'var(--bg-3)',
                    border: `1px solid ${spiderForm.max_depth === v ? 'rgba(0,194,178,0.4)' : 'var(--border)'}`,
                    color: spiderForm.max_depth === v ? '#00c2b2' : 'var(--text-2)',
                  }}
                >
                  <div style={{ fontWeight: 700, fontSize: 18, marginBottom: 2 }}>{v}</div>
                  <div style={{ fontSize: 11, fontWeight: 600 }}>{label}</div>
                  <div style={{ fontSize: 10, color: spiderForm.max_depth === v ? 'rgba(0,194,178,0.7)' : 'var(--text-3)', marginTop: 2 }}>{hint}</div>
                </button>
              ))}
            </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <Input
              label="Макс. каналов"
              type="number"
              value={spiderForm.max_channels}
              onChange={e => setSpiderForm(f => ({ ...f, max_channels: parseInt(e.target.value) || 100 }))}
            />
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Сохранить в папку</label>
              <input
                list="spider-folder-list"
                value={spiderForm.folder}
                onChange={e => setSpiderForm(f => ({ ...f, folder: e.target.value }))}
                placeholder="Необязательно"
                style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 13, outline: 'none', boxSizing: 'border-box' }}
              />
              <datalist id="spider-folder-list">{folders.map(f => <option key={f.name} value={f.name} />)}</datalist>
            </div>
          </div>

          {/* Паузы и поведение при FloodWait */}
          <div style={{ padding: '12px 14px', background: 'var(--bg-3)', borderRadius: 10, border: '1px solid var(--border)', display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
              Anti-flood
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
              <Input label="Пауза мин. (сек)" type="number" step="0.5" value={spiderForm.pause_min}
                onChange={e => setSpiderForm(f => ({ ...f, pause_min: parseFloat(e.target.value) || 0 }))} />
              <Input label="Пауза макс. (сек)" type="number" step="0.5" value={spiderForm.pause_max}
                onChange={e => setSpiderForm(f => ({ ...f, pause_max: parseFloat(e.target.value) || 0 }))} />
            </div>
            <label style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', fontSize: 12, color: 'var(--text-2)' }}>
              <input type="checkbox" checked={spiderForm.only_with_comments}
                onChange={e => setSpiderForm(f => ({ ...f, only_with_comments: e.target.checked }))}
                style={{ width: 16, height: 16, accentColor: '#00c2b2' }} />
              <span><b>💬 Только каналы с комментариями</b> (Precise-check через GetFullChannel во время crawl'а — +1 запрос TG на кандидата, но сохраняем сразу правильный флаг)</span>
            </label>

            <label style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', fontSize: 12, color: 'var(--text-2)' }}>
              <input type="checkbox" checked={spiderForm.stop_on_flood}
                onChange={e => setSpiderForm(f => ({ ...f, stop_on_flood: e.target.checked }))}
                style={{ width: 16, height: 16, accentColor: 'var(--violet)' }} />
              <span><b>Стоп при FloodWait</b> (безопасно — даём аккаунту реально остыть)</span>
            </label>
            {!spiderForm.stop_on_flood && (
              <Input label="Cool-down после FloodWait (сек, сверх Telegram-wait)" type="number" value={spiderForm.flood_cooldown_sec}
                onChange={e => setSpiderForm(f => ({ ...f, flood_cooldown_sec: parseInt(e.target.value) || 0 }))} />
            )}
          </div>

          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 4 }}>
            <Button variant="ghost" onClick={() => setSpiderModal(false)}>Отмена</Button>
            <Button
              variant="primary"
              loading={spiderSaving}
              disabled={spiderForm.account_ids.length === 0 || !spiderForm.seeds.trim()}
              onClick={handleSpiderStart}
              style={{ background: '#00c2b2' }}
            >
              🕷 Multi-паутинка · {spiderForm.account_ids.length} акк
            </Button>
          </div>
        </div>
      </Modal>

      {/* Check modal (verify_comments + alive_check) */}
      <Modal open={checkModal} onClose={() => setCheckModal(false)} title="🛡 Проверка каналов" width={500}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ padding: '10px 12px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 8, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.5 }}>
            Проверки запускаются в фоне через выбранный аккаунт (с прокси). Каналы берутся из выбранной папки (или вся база).
          </div>

          {/* Mode tabs */}
          <div style={{ display: 'flex', gap: 4, padding: 4, background: 'var(--bg-2)', borderRadius: 10, border: '1px solid var(--border)' }}>
            {[
              { key: 'verify', label: '💬 Есть ли комменты' },
              { key: 'alive',  label: '🩺 Живой ли канал' },
            ].map(t => (
              <button key={t.key} type="button" onClick={() => setCheckMode(t.key)} style={{
                flex: 1, padding: '8px 12px', borderRadius: 7, fontSize: 12, fontWeight: checkMode === t.key ? 700 : 500,
                border: 'none', cursor: 'pointer', transition: 'all 0.15s',
                background: checkMode === t.key ? 'var(--bg-card)' : 'transparent',
                color: checkMode === t.key ? 'var(--text)' : 'var(--text-3)',
              }}>{t.label}</button>
            ))}
          </div>

          {checkMode === 'verify' && (
            <>
              <div>
                <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Аккаунт (с прокси)</label>
                <select value={verifyForm.account_id || ''} onChange={e => setVerifyForm(f => ({ ...f, account_id: parseInt(e.target.value) || null }))} style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 13, outline: 'none' }}>
                  <option value="">— Выбрать аккаунт —</option>
                  {accounts.map(a => (
                    <option key={a.id} value={a.id} disabled={!a.proxy_id}>
                      {a.first_name || a.phone} {a.proxy_id ? '' : ' (нет прокси)'}
                    </option>
                  ))}
                </select>
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                <div>
                  <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Папка</label>
                  <select value={verifyForm.folder} onChange={e => setVerifyForm(f => ({ ...f, folder: e.target.value }))} style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 13, outline: 'none' }}>
                    <option value="">Вся база</option>
                    {folders.map(f => <option key={f.name} value={f.name}>{f.name} ({f.count})</option>)}
                  </select>
                </div>
                <Input label="Лимит" type="number" value={verifyForm.limit}
                  onChange={e => setVerifyForm(f => ({ ...f, limit: parseInt(e.target.value) || 0 }))} />
              </div>

              <Input label="Активен за (часов, 0 — выключить веб-фильтр)" type="number" value={verifyForm.active_hours}
                onChange={e => setVerifyForm(f => ({ ...f, active_hours: parseInt(e.target.value) || 0 }))} />

              <label style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', padding: '8px 10px', background: 'var(--bg-3)', borderRadius: 8, border: '1px solid var(--border)', fontSize: 12 }}>
                <input type="checkbox" checked={verifyForm.only_unverified}
                  onChange={e => setVerifyForm(f => ({ ...f, only_unverified: e.target.checked }))}
                  style={{ width: 16, height: 16, accentColor: 'var(--violet)' }} />
                Только непроверенные (где has_comments = false)
              </label>

              {/* Паузы и Anti-flood */}
              <div style={{ padding: '12px 14px', background: 'var(--bg-3)', borderRadius: 10, border: '1px solid var(--border)', display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
                  Anti-flood
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                  <Input label="Пауза мин. (сек)" type="number" step="0.5" value={verifyForm.pause_min}
                    onChange={e => setVerifyForm(f => ({ ...f, pause_min: parseFloat(e.target.value) || 0 }))} />
                  <Input label="Пауза макс. (сек)" type="number" step="0.5" value={verifyForm.pause_max}
                    onChange={e => setVerifyForm(f => ({ ...f, pause_max: parseFloat(e.target.value) || 0 }))} />
                </div>
                <label style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', fontSize: 12, color: 'var(--text-2)' }}>
                  <input type="checkbox" checked={verifyForm.stop_on_flood}
                    onChange={e => setVerifyForm(f => ({ ...f, stop_on_flood: e.target.checked }))}
                    style={{ width: 16, height: 16, accentColor: 'var(--violet)' }} />
                  <span><b>Стоп при FloodWait</b> (безопасно — даём аккаунту реально остыть)</span>
                </label>
                {!verifyForm.stop_on_flood && (
                  <Input label="Cool-down после FloodWait (сек, сверх Telegram-wait)" type="number" value={verifyForm.flood_cooldown_sec}
                    onChange={e => setVerifyForm(f => ({ ...f, flood_cooldown_sec: parseInt(e.target.value) || 0 }))} />
                )}
              </div>

              <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 4 }}>
                <Button variant="ghost" onClick={() => setCheckModal(false)}>Отмена</Button>
                <Button variant="primary" loading={checkSaving} disabled={!verifyForm.account_id} onClick={handleVerifyStart}>
                  💬 Запустить проверку
                </Button>
              </div>
            </>
          )}

          {checkMode === 'alive' && (
            <>
              <div style={{ padding: '8px 12px', background: 'rgba(61,214,140,0.06)', border: '1px solid rgba(61,214,140,0.18)', borderRadius: 8, fontSize: 11, color: 'var(--text-2)', lineHeight: 1.5 }}>
                ✅ Web-only: парсит preview <code style={{ background: 'var(--bg-3)', padding: '1px 5px', borderRadius: 4 }}>t.me/s/{`{username}`}</code>, <b>не дёргает Telegram API</b> — никакого риска флуда/бана. Аккаунт не нужен.
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                <div>
                  <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Папка</label>
                  <select value={aliveForm.folder} onChange={e => setAliveForm(f => ({ ...f, folder: e.target.value }))} style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 13, outline: 'none' }}>
                    <option value="">Вся база</option>
                    {folders.map(f => <option key={f.name} value={f.name}>{f.name} ({f.count})</option>)}
                  </select>
                </div>
                <Input label="Лимит" type="number" value={aliveForm.limit}
                  onChange={e => setAliveForm(f => ({ ...f, limit: parseInt(e.target.value) || 0 }))} />
              </div>

              <Input label="«Живой» = пост за последние N дней" type="number" value={aliveForm.max_days_inactive}
                onChange={e => setAliveForm(f => ({ ...f, max_days_inactive: parseInt(e.target.value) || 1 }))} />
              <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: -8 }}>
                Каналы без поста дольше этого срока считаются «мёртвыми». В БД пишется <code>last_post_date</code> — потом можно отфильтровать список вручную.
              </div>

              {/* Паузы (только для web — FloodWait нерелевантен) */}
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                <Input label="Пауза мин. (сек)" type="number" step="0.1" value={aliveForm.pause_min}
                  onChange={e => setAliveForm(f => ({ ...f, pause_min: parseFloat(e.target.value) || 0 }))} />
                <Input label="Пауза макс. (сек)" type="number" step="0.1" value={aliveForm.pause_max}
                  onChange={e => setAliveForm(f => ({ ...f, pause_max: parseFloat(e.target.value) || 0 }))} />
              </div>

              <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 4 }}>
                <Button variant="ghost" onClick={() => setCheckModal(false)}>Отмена</Button>
                <Button variant="primary" loading={checkSaving} onClick={handleAliveStart}>
                  🩺 Запустить проверку
                </Button>
              </div>
            </>
          )}
        </div>
      </Modal>

      {/* Import Modal */}
      <Modal open={exportModal} onClose={() => setExportModal(false)} title="📤 Скачать каналы" width={520}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {/* Источник */}
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Источник</label>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
              {[
                {
                  key: 'parsed',
                  label: '📁 База (parsed)',
                  desc: exportCounts
                    ? `${exportCounts.parsed.with_comments} с комм / ${exportCounts.parsed.total}`
                    : 'ParsedChannel',
                },
                {
                  key: 'tgstat_checked',
                  label: '🌐 TGStat (checked)',
                  desc: exportCounts
                    ? `${exportCounts.tgstat_checked.with_comments} с комм / ${exportCounts.tgstat_checked.total}`
                    : 'TgstatCheckedChannel',
                },
              ].map(opt => {
                const on = exportForm.source === opt.key
                return (
                  <button key={opt.key} onClick={() => setExportForm(f => ({ ...f, source: opt.key }))} style={{
                    padding: '10px 12px', borderRadius: 10, cursor: 'pointer',
                    fontSize: 12, fontWeight: on ? 700 : 500,
                    background: on ? 'rgba(124,77,255,0.15)' : 'var(--bg-3)',
                    border: `1px solid ${on ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                    color: on ? 'var(--violet)' : 'var(--text-2)',
                    display: 'flex', flexDirection: 'column', gap: 3, textAlign: 'left',
                  }}>
                    <div>{opt.label}</div>
                    <div style={{ fontSize: 10, opacity: 0.75, fontWeight: 400 }}>{opt.desc}</div>
                  </button>
                )
              })}
            </div>
            <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 4 }}>
              💡 <b>База</b> — то что осело в ParsedChannel (уникальные найденные).
              &nbsp;<b>TGStat checked</b> — ВСЁ что web-скрейпер проверил на TGStat
              (включая каналы которые не попали в базу из-за дедупа).
            </div>
          </div>

          {/* Папка (только для parsed) */}
          {exportForm.source === 'parsed' && (
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Папка</label>
              <select value={exportForm.folder}
                onChange={e => setExportForm(f => ({ ...f, folder: e.target.value }))}
                style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 14, outline: 'none' }}>
                <option value="all">Все каналы ({channels.length})</option>
                <option value="">Без папки ({channels.filter(c => !c.folder).length})</option>
                {folders.map(f => (
                  <option key={f.name} value={f.name}>
                    {f.name} ({f.count}{f.with_comments ? ` · ${f.with_comments} с комм` : ''})
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* Формат */}
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Формат</label>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 6 }}>
              {[
                { key: 'csv', label: '📊 CSV', desc: 'Все колонки' },
                { key: 'txt', label: '📝 TXT @', desc: '@usernames' },
                { key: 'txt_links', label: '🔗 TXT ссылки', desc: 't.me/xxx' },
              ].map(opt => {
                const on = exportForm.format === opt.key
                return (
                  <button key={opt.key} onClick={() => setExportForm(f => ({ ...f, format: opt.key }))} style={{
                    padding: '10px 8px', borderRadius: 10, cursor: 'pointer',
                    fontSize: 12, fontWeight: on ? 700 : 500,
                    background: on ? 'rgba(124,77,255,0.15)' : 'var(--bg-3)',
                    border: `1px solid ${on ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                    color: on ? 'var(--violet)' : 'var(--text-2)',
                    display: 'flex', flexDirection: 'column', gap: 2,
                  }}>
                    <div>{opt.label}</div>
                    <div style={{ fontSize: 10, opacity: 0.7, fontWeight: 400 }}>{opt.desc}</div>
                  </button>
                )
              })}
            </div>
          </div>

          {/* Только с комментариями */}
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, cursor: 'pointer' }}>
            <input type="checkbox" checked={exportForm.only_with_comments}
              onChange={e => setExportForm(f => ({ ...f, only_with_comments: e.target.checked }))}
              style={{ accentColor: 'var(--violet)' }} />
            💬 Только с открытыми комментариями
          </label>

          {/* Мин. подписчиков */}
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Мин. подписчиков (0 = без фильтра)</label>
            <input type="number" value={exportForm.min_subscribers}
              onChange={e => setExportForm(f => ({ ...f, min_subscribers: parseInt(e.target.value) || 0 }))}
              style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 14, outline: 'none' }} />
          </div>

          {/* ── Фильтр по проходимости из ChannelBanStats ── */}
          <div style={{ padding: 12, background: 'rgba(0,194,178,0.06)', border: '1px solid rgba(0,194,178,0.15)', borderRadius: 10, display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div style={{ fontSize: 11, color: 'var(--green)', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
              📊 Фильтр по проходимости (кампании комментов)
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              <div>
                <label style={{ fontSize: 10, color: 'var(--text-3)', display: 'block', marginBottom: 4 }}>Мин. процент прохождения</label>
                <input type="number" min={0} max={100} value={exportForm.min_pass_rate}
                  onChange={e => setExportForm(f => ({ ...f, min_pass_rate: parseFloat(e.target.value) || 0 }))}
                  placeholder="0..100"
                  style={{ width: '100%', padding: '8px 10px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 13, outline: 'none' }} />
              </div>
              <div>
                <label style={{ fontSize: 10, color: 'var(--text-3)', display: 'block', marginBottom: 4 }}>Мин. попыток (0 = любые)</label>
                <input type="number" min={0} value={exportForm.min_attempts}
                  onChange={e => setExportForm(f => ({ ...f, min_attempts: parseInt(e.target.value) || 0 }))}
                  placeholder="напр. 3"
                  style={{ width: '100%', padding: '8px 10px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 13, outline: 'none' }} />
              </div>
            </div>

            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, cursor: 'pointer' }}>
              <input type="checkbox" checked={exportForm.no_skips}
                onChange={e => setExportForm(f => ({ ...f, no_skips: e.target.checked }))}
                style={{ accentColor: 'var(--green)' }} />
              ✅ Только каналы БЕЗ скипов (skipped_count=0)
            </label>

            <div style={{ fontSize: 10, color: 'var(--text-3)', lineHeight: 1.5 }}>
              💡 Ставь <b>100%</b> + <b>мин. попыток 3+</b> чтобы получить каналы где все 3+ комментов прошли без банов и скипов.
              Каналы без статистики (не участвовали в кампаниях) фильтруются <b>только</b> если задан min_attempts или min_pass_rate.
            </div>
          </div>

          <div style={{ padding: '8px 12px', background: 'rgba(139,92,246,0.06)', borderLeft: '3px solid rgba(139,92,246,0.3)', borderRadius: 6, fontSize: 11, color: 'var(--text-3)', lineHeight: 1.5 }}>
            💡 <b>CSV</b> — для Excel/базы. <b>TXT</b> — @username по одному на строку (готово к вставке в кампанию комментинга).
            <b> TXT ссылки</b> — t.me/xxx (для открытия в Telegram).
          </div>

          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" onClick={() => setExportModal(false)}>Отмена</Button>
            <Button variant="primary" onClick={async () => {
              const f = {
                source: exportForm.source,
                format: exportForm.format,
                only_with_comments: exportForm.only_with_comments,
                min_subscribers: exportForm.min_subscribers || undefined,
                min_pass_rate: exportForm.min_pass_rate || undefined,
                no_skips: exportForm.no_skips || undefined,
                min_attempts: exportForm.min_attempts || undefined,
              }
              // folder — только для source=parsed. Для tgstat_checked папок нет.
              if (exportForm.source === 'parsed' && exportForm.folder !== 'all') {
                f.folder = exportForm.folder
              }
              await doExport(f)
              setExportModal(false)
            }}>💾 Скачать</Button>
          </div>
        </div>
      </Modal>

      <Modal open={importModal} onClose={() => setImportModal(false)} title="Импорт каналов" width={480}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ padding: '10px 14px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>
            Введите юзернеймы или ссылки — по одному на строку
          </div>
          <textarea value={importText} onChange={e => setImportText(e.target.value)} rows={8} placeholder={"@crypto_news\n@blockchain_ru\nhttps://t.me/bitcoin"} style={{ width: '100%', padding: '12px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 13, fontFamily: 'var(--font-mono)', resize: 'vertical', outline: 'none' }} />
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" onClick={() => setImportModal(false)}>Отмена</Button>
            <Button variant="primary" onClick={handleImport}>Импортировать</Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}