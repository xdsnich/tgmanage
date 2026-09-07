import { useEffect, useState, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { accountsAPI, importAPI, proxiesAPI, channelsAPI, diagnosticsAPI, apiAppsAPI, accountMediaAPI, tasksAPI } from '../services/api'
import WebSessionImport from './WebSessionImport'
import { proxyLabel, countryFlag } from '../utils/proxy'
import { Card, Button, Input, Modal, TrustBar, StatusBadge, Empty, Spinner, Badge } from '../components/ui'

const ROLES = ['default', 'продавец', 'прогреватель', 'читатель', 'консультант']
const STATUS_FILTERS = [
  { key: 'all', label: 'Все' },
  { key: 'active', label: '● Живые' },
  { key: 'spamblock', label: '● Спам' },
  { key: 'frozen', label: '● Заморожено' },
  { key: 'unknown', label: '● Неизвестно' },
]

export default function AccountsPage() {
  const [accounts, setAccounts] = useState([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')

  // ── Multi-select (bulk operations) ────────────────────────
  // Персистим в localStorage — если юзер случайно кликнул на строку
  // (а не по чекбоксу) и провалился в детали акка, при возврате
  // назад выбор восстановится, не придётся выбирать заново.
  const SELECTED_LS = 'accounts_selected_ids'
  const [selectedIds, setSelectedIdsRaw] = useState(() => {
    try {
      const raw = localStorage.getItem(SELECTED_LS)
      if (!raw) return new Set()
      const arr = JSON.parse(raw)
      if (!Array.isArray(arr)) return new Set()
      return new Set(arr.filter(x => typeof x === 'number'))
    } catch { return new Set() }
  })
  const setSelectedIds = (updater) => {
    setSelectedIdsRaw(prev => {
      const next = typeof updater === 'function' ? updater(prev) : updater
      try {
        if (next && next.size > 0) {
          localStorage.setItem(SELECTED_LS, JSON.stringify([...next]))
        } else {
          localStorage.removeItem(SELECTED_LS)
        }
      } catch {}
      return next
    })
  }
  const [bulkMediaModal, setBulkMediaModal] = useState(false)
  const [bulkMediaFiles, setBulkMediaFiles] = useState([])
  const [bulkUploading, setBulkUploading] = useState(false)
  const [bulkDist, setBulkDist] = useState('random')   // 'random' | 'all'
  const [bulkRandomMin, setBulkRandomMin] = useState(1)
  const [bulkRandomMax, setBulkRandomMax] = useState(3)
  const bulkFileInputRef = useRef(null)

  const toggleSelect = (id) => setSelectedIds(s => {
    const n = new Set(s)
    if (n.has(id)) n.delete(id); else n.add(id)
    return n
  })
  const selectAll = (ids) => setSelectedIds(new Set(ids))
  const clearSelection = () => setSelectedIds(new Set())

  const openBulkMedia = () => {
    setBulkMediaFiles([])
    setBulkMediaModal(true)
  }

  const handleBulkUpload = async () => {
    if (!bulkMediaFiles.length) { alert('Выбери хотя бы одно фото'); return }
    if (!selectedIds.size) { alert('Не выбрано ни одного аккаунта'); return }
    setBulkUploading(true)
    try {
      const fd = new FormData()
      bulkMediaFiles.forEach(f => fd.append('files', f))
      fd.append('distribution', bulkDist)
      fd.append('random_min', String(bulkRandomMin))
      fd.append('random_max', String(bulkRandomMax))
      const { data } = await accountMediaAPI.bulkUpload([...selectedIds], fd)
      alert(data.message)
      setBulkMediaModal(false)
      setBulkMediaFiles([])
    } catch (err) {
      alert(err.response?.data?.detail || 'Ошибка загрузки')
    }
    setBulkUploading(false)
  }

  const handleBulkClearMedia = async () => {
    if (!selectedIds.size) return
    if (!window.confirm(`Удалить ВСЕ фото у ${selectedIds.size} выбранных аккаунтов?`)) return
    try {
      const { data } = await accountMediaAPI.bulkClear([...selectedIds])
      alert(`Удалено ${data.total_removed} файлов из ${data.accounts_count} папок`)
    } catch (err) {
      alert(err.response?.data?.detail || 'Ошибка')
    }
  }

  const [bulkExporting, setBulkExporting] = useState(false)
  const [checkProgress, setCheckProgress] = useState(null) // {current, total, taskId}
  const [checking, setChecking] = useState(false)
  const [checkResults, setCheckResults] = useState(null)   // {summary, results[]}

  // ── Bulk «Подготовка» (аватарки + имена + канал, разнесённо во времени) ──
  const [prepModal, setPrepModal] = useState(false)
  const [prepChangeAvatars, setPrepChangeAvatars] = useState(false)
  const [prepAvatarFiles, setPrepAvatarFiles] = useState([])
  const [prepDragOver, setPrepDragOver] = useState(false)
  const [prepDeletePrevProfileAvatar, setPrepDeletePrevProfileAvatar] = useState(false)
  const [prepChangeNames, setPrepChangeNames] = useState(false)
  const [prepNamesText, setPrepNamesText] = useState('')
  // Каналы
  const [prepCreateChannels, setPrepCreateChannels] = useState(false)
  const [prepChTitles, setPrepChTitles] = useState('')
  const [prepChDescriptions, setPrepChDescriptions] = useState('')
  const [prepChFirstPost, setPrepChFirstPost] = useState('')
  const [prepChPin, setPrepChPin] = useState(true)
  // Публичный канал (с @username) + пул кандидатов
  const [prepChPublic, setPrepChPublic] = useState(false)
  const [prepChUsernames, setPrepChUsernames] = useState('')
  const [prepChAvatarFiles, setPrepChAvatarFiles] = useState([])
  const [prepChDragOver, setPrepChDragOver] = useState(false)
  const [prepDeletePrevChannelAvatar, setPrepDeletePrevChannelAvatar] = useState(false)
  const [prepChPostPhotoFiles, setPrepChPostPhotoFiles] = useState([])
  const [prepChPostDragOver, setPrepChPostDragOver] = useState(false)
  // Ручное распределение постов по акк-id (не рандомно).
  // Формат: { [acc_id]: { text: "...", photoFile: File | null } }
  const [prepChCustomPostsEnabled, setPrepChCustomPostsEnabled] = useState(false)
  const [prepChCustomPosts, setPrepChCustomPosts] = useState({})
  const prepChFileInputRef = useRef(null)
  const prepChPostFileInputRef = useRef(null)
  // Общее
  const [prepMinDelay, setPrepMinDelay] = useState(60)
  const [prepMaxDelay, setPrepMaxDelay] = useState(180)
  const [prepMode, setPrepMode] = useState('fast')  // 'fast' | 'plan'
  const [prepDurationHours, setPrepDurationHours] = useState(0.5)
  const [prepSessionsPerAcc, setPrepSessionsPerAcc] = useState(3)
  const [prepRecommend, setPrepRecommend] = useState(null)
  const [prepStarting, setPrepStarting] = useState(false)
  // Персистентность настроек модалки — чтобы при случайной ошибке
  // или перезагрузке страницы не терять форму. Файлы (File) не
  // сериализуются, поэтому не персистятся. Остальное — да.
  const PREP_FORM_LS = 'prep_form_state_v1'
  const prepFormLoadedRef = useRef(false)
  useEffect(() => {
    try {
      const raw = localStorage.getItem(PREP_FORM_LS)
      if (raw) {
        const s = JSON.parse(raw)
        if (typeof s.prepChangeAvatars === 'boolean') setPrepChangeAvatars(s.prepChangeAvatars)
        if (typeof s.prepDeletePrevProfileAvatar === 'boolean') setPrepDeletePrevProfileAvatar(s.prepDeletePrevProfileAvatar)
        if (typeof s.prepChangeNames === 'boolean') setPrepChangeNames(s.prepChangeNames)
        if (typeof s.prepNamesText === 'string') setPrepNamesText(s.prepNamesText)
        if (typeof s.prepCreateChannels === 'boolean') setPrepCreateChannels(s.prepCreateChannels)
        if (typeof s.prepChTitles === 'string') setPrepChTitles(s.prepChTitles)
        if (typeof s.prepChDescriptions === 'string') setPrepChDescriptions(s.prepChDescriptions)
        if (typeof s.prepChFirstPost === 'string') setPrepChFirstPost(s.prepChFirstPost)
        if (typeof s.prepChPin === 'boolean') setPrepChPin(s.prepChPin)
        if (typeof s.prepChPublic === 'boolean') setPrepChPublic(s.prepChPublic)
        if (typeof s.prepChUsernames === 'string') setPrepChUsernames(s.prepChUsernames)
        if (typeof s.prepDeletePrevChannelAvatar === 'boolean') setPrepDeletePrevChannelAvatar(s.prepDeletePrevChannelAvatar)
        if (typeof s.prepChCustomPostsEnabled === 'boolean') setPrepChCustomPostsEnabled(s.prepChCustomPostsEnabled)
        // Текст постов сохраняем, файлы нет
        if (s.prepChCustomPostsText && typeof s.prepChCustomPostsText === 'object') {
          const restored = {}
          for (const [k, v] of Object.entries(s.prepChCustomPostsText)) {
            if (v && typeof v.text === 'string') restored[k] = { text: v.text, photoFile: null }
          }
          if (Object.keys(restored).length) setPrepChCustomPosts(restored)
        }
        if (typeof s.prepMinDelay === 'number') setPrepMinDelay(s.prepMinDelay)
        if (typeof s.prepMaxDelay === 'number') setPrepMaxDelay(s.prepMaxDelay)
        if (typeof s.prepMode === 'string') setPrepMode(s.prepMode)
        if (typeof s.prepDurationHours === 'number') setPrepDurationHours(s.prepDurationHours)
        if (typeof s.prepSessionsPerAcc === 'number') setPrepSessionsPerAcc(s.prepSessionsPerAcc)
      }
    } catch {}
    prepFormLoadedRef.current = true
  }, [])
  // Autosave при любом изменении. Debounce небольшой чтобы не спамить LS
  // на каждый symbolgin набора текста.
  useEffect(() => {
    if (!prepFormLoadedRef.current) return
    const t = setTimeout(() => {
      try {
        // Извлекаем только текст из custom posts (File объекты не сериализуемы)
        const customPostsText = {}
        for (const [k, v] of Object.entries(prepChCustomPosts || {})) {
          if (v?.text) customPostsText[k] = { text: v.text }
        }
        const s = {
          prepChangeAvatars, prepDeletePrevProfileAvatar,
          prepChangeNames, prepNamesText,
          prepCreateChannels, prepChTitles, prepChDescriptions, prepChFirstPost,
          prepChPin, prepChPublic, prepChUsernames,
          prepDeletePrevChannelAvatar,
          prepChCustomPostsEnabled,
          prepChCustomPostsText: customPostsText,
          prepMinDelay, prepMaxDelay,
          prepMode, prepDurationHours, prepSessionsPerAcc,
        }
        localStorage.setItem(PREP_FORM_LS, JSON.stringify(s))
      } catch {}
    }, 300)
    return () => clearTimeout(t)
  }, [
    prepChangeAvatars, prepDeletePrevProfileAvatar,
    prepChangeNames, prepNamesText,
    prepCreateChannels, prepChTitles, prepChDescriptions, prepChFirstPost,
    prepChPin, prepChPublic, prepChUsernames,
    prepDeletePrevChannelAvatar,
    prepChCustomPostsEnabled, prepChCustomPosts,
    prepMinDelay, prepMaxDelay,
    prepMode, prepDurationHours, prepSessionsPerAcc,
  ])
  // Кнопка «Очистить форму» — чтобы юзер мог обнулить если хочет свежий запуск.
  const clearPrepForm = () => {
    setPrepChangeAvatars(false); setPrepAvatarFiles([]); setPrepDeletePrevProfileAvatar(false)
    setPrepChangeNames(false); setPrepNamesText('')
    setPrepCreateChannels(false); setPrepChTitles(''); setPrepChDescriptions(''); setPrepChFirstPost('')
    setPrepChPin(true); setPrepChPublic(false); setPrepChUsernames('')
    setPrepChAvatarFiles([]); setPrepDeletePrevChannelAvatar(false)
    setPrepChPostPhotoFiles([])
    setPrepChCustomPostsEnabled(false); setPrepChCustomPosts({})
    setPrepMinDelay(60); setPrepMaxDelay(180)
    setPrepMode('fast'); setPrepDurationHours(0.5); setPrepSessionsPerAcc(3)
    try { localStorage.removeItem(PREP_FORM_LS) } catch {}
  }
  // prepJobId и prepMode2 персистятся в localStorage — чтобы:
  //   1) Если юзер закроет модалку (или обновит страницу) — poll на прогресс
  //      продолжится и статус будет виден в floating badge
  //   2) На новом заходе можно повторно открыть модалку и увидеть текущее
  //      состояние job'а без запуска нового
  const PREP_LS_JOB = 'prep_active_job'
  const PREP_LS_MODE = 'prep_active_mode'
  const [prepJobId, setPrepJobIdRaw] = useState(() => {
    try { return localStorage.getItem(PREP_LS_JOB) || null } catch { return null }
  })
  const setPrepJobId = (v) => {
    setPrepJobIdRaw(v)
    try {
      if (v) localStorage.setItem(PREP_LS_JOB, v)
      else localStorage.removeItem(PREP_LS_JOB)
    } catch {}
  }
  const [prepProgress, setPrepProgress] = useState(null)
  const [prepMode2, setPrepMode2Raw] = useState(() => {
    try { return localStorage.getItem(PREP_LS_MODE) || 'fast' } catch { return 'fast' }
  })
  const setPrepMode2 = (v) => {
    setPrepMode2Raw(v)
    try {
      if (v) localStorage.setItem(PREP_LS_MODE, v)
      else localStorage.removeItem(PREP_LS_MODE)
    } catch {}
  }
  const prepFileInputRef = useRef(null)
  const prepPollRef = useRef(null)

  // Добавляем файлы к уже выбранным, с дедупом по name+size (чтобы юзер
  // мог таскать несколько партий и не переживать за дубли).
  const addPrepFiles = (incoming) => {
    if (!incoming || incoming.length === 0) return
    // Только картинки — на dnd могут прилететь любые типы
    const images = Array.from(incoming).filter(f =>
      f.type && f.type.startsWith('image/')
    )
    if (images.length === 0) {
      alert('Можно только изображения (image/*)')
      return
    }
    setPrepAvatarFiles(prev => {
      const seen = new Set(prev.map(f => `${f.name}::${f.size}`))
      const merged = [...prev]
      let dupes = 0
      for (const f of images) {
        const key = `${f.name}::${f.size}`
        if (seen.has(key)) { dupes++; continue }
        seen.add(key)
        merged.push(f)
      }
      if (dupes > 0) {
        console.log(`[prep] пропущено ${dupes} дубликатов файлов`)
      }
      return merged
    })
  }

  const removePrepFile = (idx) => {
    setPrepAvatarFiles(prev => prev.filter((_, i) => i !== idx))
  }

  const openPrepModal = () => {
    // НЕ обнуляем форму — она восстанавливается из localStorage через
    // useEffect. Чистим только эфемерные вещи: файлы (File объекты
    // не сериализуемы) и текущий job.
    setPrepAvatarFiles([])
    setPrepChAvatarFiles([])
    setPrepChPostPhotoFiles([])
    // Из custom-posts подчищаем photoFile — тексты остаются
    setPrepChCustomPosts(prev => {
      const next = {}
      for (const [k, v] of Object.entries(prev || {})) {
        if (v?.text) next[k] = { text: v.text, photoFile: null }
      }
      return next
    })
    setPrepRecommend(null)
    setPrepJobId(null)
    setPrepProgress(null)
    setPrepModal(true)
  }

  // Вычисляем число action'ов на акк — для рекомендации duration
  // Валидные username'ы для публичного канала: [A-Za-z][A-Za-z0-9_]{4,31}
  const prepChValidUsernames = (() => {
    if (!prepChPublic) return []
    const re = /^[A-Za-z][A-Za-z0-9_]{4,31}$/
    const list = []
    for (const raw of prepChUsernames.split('\n')) {
      const u = raw.trim().replace(/^@/, '')
      if (u && re.test(u) && !list.includes(u)) list.push(u)
    }
    return list
  })()

  const prepActionsPerAcc = (() => {
    let n = 0
    if (prepChangeNames) n += 1
    if (prepChangeAvatars) n += 1
    if (prepCreateChannels) {
      n += 1  // create_channel
      if (prepChAvatarFiles.length > 0) n += 1  // channel avatar
      if (prepChPin) n += 1  // pin
      if (prepChFirstPost.trim() || prepChPostPhotoFiles.length > 0) n += 1  // first post (текст ИЛИ фото)
    }
    return Math.max(1, n)
  })()

  // Автоматически подтягиваем рекомендацию когда меняются параметры
  const [prepDurationUserEdited, setPrepDurationUserEdited] = useState(false)
  useEffect(() => {
    if (!prepModal || prepMode !== 'plan' || !selectedIds.size) {
      setPrepRecommend(null); return
    }
    let cancelled = false
    accountsAPI.bulkPreparePlanRecommend(selectedIds.size, prepActionsPerAcc)
      .then(({ data }) => {
        if (cancelled) return
        setPrepRecommend(data)
        // Автоподстановка рекомендации если юзер сам не менял значение —
        // тогда открытие модалки сразу показывает разумное время под задачу,
        // а не hardcoded 2ч или 30 мин.
        if (!prepDurationUserEdited && data.recommended_hours) {
          setPrepDurationHours(data.recommended_hours)
        }
      })
      .catch(() => { if (!cancelled) setPrepRecommend(null) })
    return () => { cancelled = true }
  }, [prepModal, prepMode, selectedIds.size, prepActionsPerAcc, prepDurationUserEdited])

  // Аналог addPrepFiles но для channel-аватарок
  const addPrepChFiles = (incoming) => {
    if (!incoming || incoming.length === 0) return
    const images = Array.from(incoming).filter(f => f.type && f.type.startsWith('image/'))
    if (images.length === 0) { alert('Только изображения'); return }
    setPrepChAvatarFiles(prev => {
      const seen = new Set(prev.map(f => `${f.name}::${f.size}`))
      const merged = [...prev]
      for (const f of images) {
        const key = `${f.name}::${f.size}`
        if (seen.has(key)) continue
        seen.add(key)
        merged.push(f)
      }
      return merged
    })
  }
  const removePrepChFile = (idx) => {
    setPrepChAvatarFiles(prev => prev.filter((_, i) => i !== idx))
  }

  // Фото/видео первого поста. Принимаем image/* И video/*.
  // Telethon send_file автоматически определит тип по расширению:
  //   .mov/.mp4/.webm/.mkv/.avi → как видео (streamable если возможно)
  //   .jpg/.png/.webp/.gif      → как фото
  // Некоторые ОС/браузеры не выставляют MIME для .mov (пустой f.type),
  // так что дополнительно смотрим на расширение.
  const VIDEO_EXTS = ['.mov', '.mp4', '.webm', '.mkv', '.avi', '.m4v']
  const addPrepChPostFiles = (incoming) => {
    if (!incoming || incoming.length === 0) return
    const accepted = Array.from(incoming).filter(f => {
      const type = f.type || ''
      if (type.startsWith('image/') || type.startsWith('video/')) return true
      const nameLower = (f.name || '').toLowerCase()
      return VIDEO_EXTS.some(ext => nameLower.endsWith(ext))
    })
    if (accepted.length === 0) {
      alert('Только изображения или видео (mp4, mov, webm, mkv, avi, m4v)')
      return
    }
    setPrepChPostPhotoFiles(prev => {
      const seen = new Set(prev.map(f => `${f.name}::${f.size}`))
      const merged = [...prev]
      for (const f of accepted) {
        const key = `${f.name}::${f.size}`
        if (seen.has(key)) continue
        seen.add(key)
        merged.push(f)
      }
      return merged
    })
  }
  const removePrepChPostFile = (idx) => {
    setPrepChPostPhotoFiles(prev => prev.filter((_, i) => i !== idx))
  }

  const startPrepPolling = (jobId, mode) => {
    if (prepPollRef.current) clearInterval(prepPollRef.current)
    const useMode = mode || 'fast'
    const fn = useMode === 'plan'
      ? accountsAPI.bulkPreparePlanProgress
      : accountsAPI.bulkPrepareProgress
    const poll = async () => {
      try {
        const { data } = await fn(jobId)
        setPrepProgress({ ...data, _mode: useMode })
        if (data.status === 'done' || data.status === 'error') {
          // Останавливаем polling — но НЕ чистим prepJobId.
          // Раньше здесь стоял setPrepJobId(null) — из-за чего клик
          // по done-job'у в списке моментально сбрасывался обратно
          // в список: polling получал 'done' → clearJobId → перерендер.
          // Job'ы теперь остаются видимыми в timeline пока юзер сам
          // не закроет модалку или не нажмёт «⟳ Новый job».
          clearInterval(prepPollRef.current)
          prepPollRef.current = null
        }
      } catch (e) { console.error('prep poll', e) }
    }
    poll()
    // Plan-mode медленный (сессии по расписанию) — но нужно быстро
    // отслеживать переход generating → scheduled → running
    prepPollRef.current = setInterval(poll, useMode === 'plan' ? 3000 : 2500)
  }

  const [prepCancelling, setPrepCancelling] = useState(false)
  const [prepResuming, setPrepResuming] = useState(false)
  const [prepRefixingVideo, setPrepRefixingVideo] = useState(false)
  const [prepRefixingAva, setPrepRefixingAva] = useState(false)
  const [prepRefixingAttach, setPrepRefixingAttach] = useState(false)
  const prepRefixFileInputRef = useRef(null)
  const prepRefixAvaInputRef = useRef(null)
  // Прогресс двух refix-задач (avatar / video) для текущего prepJobId
  const [prepRefixProgress, setPrepRefixProgress] = useState({})  // { avatar: {...}, video: {...} }
  const prepRefixPollRefs = useRef({})   // { avatar: intervalId, video: intervalId }

  const startPrepRefixPolling = (jobId, kind) => {
    if (prepRefixPollRefs.current[kind]) clearInterval(prepRefixPollRefs.current[kind])
    const poll = async () => {
      try {
        const { data } = await accountsAPI.bulkPreparePlanRefixProgress(jobId, kind)
        setPrepRefixProgress(prev => ({ ...prev, [kind]: data }))
        if (['done', 'idle', 'error', 'cancelled'].includes(data.status)) {
          clearInterval(prepRefixPollRefs.current[kind])
          prepRefixPollRefs.current[kind] = null
        }
      } catch (e) { /* silent */ }
    }
    poll()
    prepRefixPollRefs.current[kind] = setInterval(poll, 3000)
  }
  // При смене prepJobId — переставляем polling, чистим прошлое состояние
  useEffect(() => {
    // stop old
    for (const k of ['avatar', 'video', 'attach']) {
      if (prepRefixPollRefs.current[k]) {
        clearInterval(prepRefixPollRefs.current[k])
        prepRefixPollRefs.current[k] = null
      }
    }
    setPrepRefixProgress({})
    if (prepJobId) {
      startPrepRefixPolling(prepJobId, 'avatar')
      startPrepRefixPolling(prepJobId, 'video')
      startPrepRefixPolling(prepJobId, 'attach')
    }
    return () => {
      for (const k of ['avatar', 'video', 'attach']) {
        if (prepRefixPollRefs.current[k]) clearInterval(prepRefixPollRefs.current[k])
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prepJobId])
  const handlePrepRefixAva = async () => {
    if (!prepJobId) return
    prepRefixAvaInputRef.current?.click()
  }
  const handlePrepRefixAvaFile = async (file, deletePrev) => {
    if (!file || !prepJobId) return
    setPrepRefixingAva(true)
    try {
      const fd = new FormData()
      fd.append('job_id', prepJobId)
      fd.append('photo_file', file)
      fd.append('delay_min_sec', '30')
      fd.append('delay_max_sec', '90')
      fd.append('delete_previous', deletePrev ? 'true' : 'false')
      const { data } = await accountsAPI.bulkPreparePlanRefixChannelAvatar(fd)
      // Не alert'им — прогресс покажется в inline banner ниже
      startPrepRefixPolling(prepJobId, 'avatar')
      setPrepRefixProgress(prev => ({ ...prev, avatar: {
        status: 'running', total: 0, done: 0, ok: 0, failed: 0, kind: 'avatar',
      } }))
    } catch (err) {
      const status = err.response?.status
      const detail = err.response?.data?.detail || 'Не удалось запустить refix'
      if (status === 409) {
        startPrepRefixPolling(prepJobId, 'avatar')
      } else {
        alert(detail)
      }
    } finally {
      setPrepRefixingAva(false)
    }
  }
  const handlePrepRefixVideo = async () => {
    if (!prepJobId) return
    prepRefixFileInputRef.current?.click()
  }
  const handlePrepRefixVideoFile = async (file, caption) => {
    if (!file || !prepJobId) return
    setPrepRefixingVideo(true)
    try {
      const fd = new FormData()
      fd.append('job_id', prepJobId)
      fd.append('caption', caption || '')
      fd.append('video_file', file)
      fd.append('delay_min_sec', '30')
      fd.append('delay_max_sec', '90')
      const { data } = await accountsAPI.bulkPreparePlanRefixFirstPost(fd)
      startPrepRefixPolling(prepJobId, 'video')
      setPrepRefixProgress(prev => ({ ...prev, video: {
        status: 'running', total: 0, done: 0, ok: 0, failed: 0, kind: 'video',
      } }))
    } catch (err) {
      const status = err.response?.status
      const detail = err.response?.data?.detail || 'Не удалось запустить refix'
      if (status === 409) {
        startPrepRefixPolling(prepJobId, 'video')
      } else {
        alert(detail)
      }
    } finally {
      setPrepRefixingVideo(false)
    }
  }
  const handlePrepRefixAttach = async () => {
    if (!prepJobId) return
    if (!confirm(
      '🔗 Прикрепить созданный канал к профилю юзера (personal_channel) — ' +
      'это тот значок канала в профиле, не в закреплённых чатах.\n\n' +
      'Пройдёт по всем ~N акаунтам этого job\'а. У кого канал уже прикреплён — ' +
      'пропустит. Продолжить?'
    )) return
    setPrepRefixingAttach(true)
    try {
      await accountsAPI.bulkPreparePlanRefixAttachChannel(prepJobId)
      startPrepRefixPolling(prepJobId, 'attach')
      setPrepRefixProgress(prev => ({ ...prev, attach: {
        status: 'running', total: 0, done: 0, ok: 0, failed: 0, kind: 'attach',
      } }))
    } catch (err) {
      const status = err.response?.status
      const detail = err.response?.data?.detail || 'Не удалось запустить прикрепление'
      // 409 = уже идёт. Не показываем как ошибку — сразу подписываемся
      // на живой прогресс, юзер увидит текущий баннер.
      if (status === 409) {
        startPrepRefixPolling(prepJobId, 'attach')
      } else {
        alert(detail)
      }
    } finally {
      setPrepRefixingAttach(false)
    }
  }
  const handlePrepPlanResume = async () => {
    if (!prepJobId) return
    const hours = parseFloat(prompt(
      '🔄 Дожать skipped/failed сессии из этого job\'а.\n\n' +
      'Соберёт все действия которые не выполнились (skipped/failed/cancelled)\n' +
      'и запланирует их в новом job\'е с другими рандомными слотами.\n\n' +
      'За сколько часов дожать?', '1'
    ))
    if (!hours || hours < 0.1) return
    setPrepResuming(true)
    try {
      const { data } = await accountsAPI.bulkPreparePlanResume(prepJobId, hours, 2)
      alert(`Resume запущен · new_job=${data.new_job_id.slice(0, 8)}...`)
      // Переключаемся на новый job чтобы видеть его прогресс
      setPrepJobId(data.new_job_id)
      setPrepProgress({
        status: 'queued', total: 0, done: 0, ok: 0, failed: 0,
        _mode: 'plan', phase: 'generating',
      })
      startPrepPolling(data.new_job_id, 'plan')
    } catch (err) {
      alert(err.response?.data?.detail || 'Не удалось дожать')
    } finally {
      setPrepResuming(false)
    }
  }

  const handlePrepPlanCancel = async () => {
    if (!prepJobId) return
    if (!window.confirm(
      '🛑 ФОРСИРОВАННО остановить план?\n\n' +
      '• Все запланированные сессии моментально удаляются из очереди Celery (revoke)\n' +
      '• Все pending/scheduled/waiting сессии помечаются cancelled\n' +
      '• Уже работающие сессии получают SIGTERM — Telethon connection\n' +
      '  оборвётся (Telegram обычно ok с этим)\n\n' +
      'Продолжить?'
    )) return
    setPrepCancelling(true)
    try {
      const { data } = await accountsAPI.bulkPreparePlanCancel(prepJobId)
      alert(data.message || 'Остановлено')
      // Сразу перечитываем прогресс, чтобы UI обновился
      try {
        const { data: prog } = await accountsAPI.bulkPreparePlanProgress(prepJobId)
        setPrepProgress({ ...prog, _mode: 'plan' })
      } catch {}
    } catch (err) {
      alert(err.response?.data?.detail || 'Не удалось отменить')
    } finally {
      setPrepCancelling(false)
    }
  }

  const handlePrepStart = async () => {
    if (!selectedIds.size) { alert('Не выбран ни один аккаунт'); return }
    if (!prepChangeAvatars && !prepChangeNames && !prepCreateChannels) {
      alert('Выбери хотя бы одно действие: аватарка, имя или канал'); return
    }
    if (prepChangeAvatars && !prepAvatarFiles.length) {
      alert('Ты включил смену аватарок, но не загрузил файлы'); return
    }
    const namesLines = prepNamesText.split(/\n/).map(s => s.trim()).filter(Boolean)
    if (prepChangeNames && namesLines.length === 0) {
      alert('Ты включил смену имён, но список пуст'); return
    }
    const chTitlesLines = prepChTitles.split(/\n/).map(s => s.trim()).filter(Boolean)
    if (prepCreateChannels && chTitlesLines.length === 0) {
      alert('Ты включил создание каналов, но список названий пуст'); return
    }
    if (prepCreateChannels && prepChPublic && prepChValidUsernames.length === 0) {
      alert('Ты включил публичный канал, но валидных username\'ов нет (5-32 символа, начинается с буквы, латиница/цифры/подчёркивание)')
      return
    }
    if (prepMinDelay > prepMaxDelay) {
      alert('min задержка не может быть больше max'); return
    }
    setPrepStarting(true)
    try {
      const fd = new FormData()
      fd.append('account_ids', JSON.stringify([...selectedIds]))
      fd.append('change_avatars', prepChangeAvatars ? 'true' : 'false')
      fd.append('delete_previous_profile_avatar',
        (prepChangeAvatars && prepDeletePrevProfileAvatar) ? 'true' : 'false')
      fd.append('change_names', prepChangeNames ? 'true' : 'false')
      fd.append('create_channels', prepCreateChannels ? 'true' : 'false')
      fd.append('delete_previous_channel_avatar',
        (prepCreateChannels && prepDeletePrevChannelAvatar) ? 'true' : 'false')
      fd.append('names_text', prepChangeNames ? prepNamesText : '')
      fd.append('channel_titles_text', prepCreateChannels ? prepChTitles : '')
      fd.append('channel_descriptions_text', prepCreateChannels ? prepChDescriptions : '')
      // Legacy — если новое поле пусто, старый single-line description больше не используется
      fd.append('channel_description', '')
      fd.append('channel_first_post', prepCreateChannels ? prepChFirstPost : '')
      fd.append('channel_pin', prepCreateChannels && prepChPin ? 'true' : 'false')
      fd.append('channel_public', prepCreateChannels && prepChPublic ? 'true' : 'false')
      fd.append('channel_usernames_text', prepCreateChannels && prepChPublic ? prepChUsernames : '')
      fd.append('min_delay_sec', String(prepMinDelay))
      fd.append('max_delay_sec', String(prepMaxDelay))
      fd.append('plan_mode', prepMode === 'plan' ? 'true' : 'false')
      fd.append('target_duration_hours', String(prepDurationHours))
      fd.append('sessions_per_acc', String(prepSessionsPerAcc))
      if (prepChangeAvatars) {
        prepAvatarFiles.forEach(f => fd.append('files', f))
      }
      if (prepCreateChannels) {
        prepChAvatarFiles.forEach(f => fd.append('channel_avatar_files', f))
        prepChPostPhotoFiles.forEach(f => fd.append('channel_post_files', f))
        // Ручное распределение: сериализуем map { acc_id → { text, photo_filename } }
        // + приклеиваем кастомные файлы в тот же channel_post_files pool.
        // Backend матчит по photo_filename → путь, дедуп по имени.
        if (prepChCustomPostsEnabled) {
          const arr = []
          const seenFileNames = new Set(prepChPostPhotoFiles.map(f => f.name))
          for (const accId of selectedIds) {
            const entry = prepChCustomPosts[accId]
            if (!entry) continue
            const text = (entry.text || '').trim()
            const photoFile = entry.photoFile || null
            if (!text && !photoFile) continue
            let photoFilename = null
            if (photoFile) {
              photoFilename = photoFile.name
              // Не задваиваем — если файл с таким именем уже в pool'е, не добавляем
              if (!seenFileNames.has(photoFilename)) {
                fd.append('channel_post_files', photoFile)
                seenFileNames.add(photoFilename)
              }
            }
            arr.push({
              account_id: accId,
              text,
              photo_filename: photoFilename,
            })
          }
          fd.append('channel_posts_json', JSON.stringify(arr))
        }
      }
      const { data } = await accountsAPI.bulkPrepare(fd)
      setPrepJobId(data.job_id)
      setPrepMode2(data.mode || prepMode)
      setPrepProgress({
        status: 'queued', total: data.total, done: 0, ok: 0, failed: 0,
        est_total_sec: data.est_total_sec, _mode: data.mode || prepMode,
      })
      startPrepPolling(data.job_id, data.mode || prepMode)
    } catch (err) {
      alert(err.response?.data?.detail || 'Ошибка запуска')
    } finally {
      setPrepStarting(false)
    }
  }

  // Запуск проверки выбранных аккаунтов: жмёт /tasks/check-accounts с
  // account_ids, ловит task_id, поллит /tasks/{id} раз в 2 сек до конца,
  // потом refresh списка чтобы статусы (active/frozen/spamblock) обновились.
  const handleCheckSelected = async () => {
    if (!selectedIds.size) return
    if (!window.confirm(`Проверить статус ${selectedIds.size} аккаунтов?\n\nЭто подключится к каждому через прокси и обновит статус (active/frozen/spamblock).`)) return
    setChecking(true)
    setCheckProgress({ current: 0, total: selectedIds.size, taskId: null })
    try {
      const { data } = await tasksAPI.checkAccounts(false, [...selectedIds])
      const taskId = data.task_id
      setCheckProgress(p => ({ ...p, taskId, total: data.total || p.total }))

      // Поллинг прогресса.
      // Бэк отдаёт: { status: PENDING|PROGRESS|SUCCESS|FAILURE,
      //               progress: {current, total, message}, result: {...} }
      // (а не st.state/st.info/st.meta — это была опечатка)
      const selSize = selectedIds.size
      const poll = setInterval(async () => {
        try {
          const { data: st } = await tasksAPI.getStatus(taskId)
          const prog = st.progress || {}
          const current = prog.current ?? 0
          const total = prog.total || selSize
          setCheckProgress({ current, total, taskId, message: prog.message })
          if (st.status === 'SUCCESS' || st.status === 'FAILURE') {
            clearInterval(poll)
            setChecking(false)
            const r = st.result || {}
            const okStr = st.status === 'SUCCESS'
              ? `✅ Проверено ${r.total || total}: ${r.active || 0} active · ${r.spam || 0} spam · ${r.frozen || 0} frozen · ${r.errors || 0} err`
              : `❌ Ошибка: ${st.error || 'не удалось'}`
            setCheckProgress({
              current: r.total || total, total: r.total || total,
              taskId, status: st.status === 'SUCCESS' ? 'done' : 'error',
              message: okStr,
            })
            // Открываем модал с детальными результатами по каждому акку
            if (st.status === 'SUCCESS' && Array.isArray(r.results)) {
              setCheckResults({
                total: r.total, active: r.active, spam: r.spam,
                frozen: r.frozen, errors: r.errors,
                results: r.results,
              })
            }
            await load()
            setTimeout(() => setCheckProgress(null), 5000)
          }
        } catch (e) {
          clearInterval(poll)
          setChecking(false)
          setCheckProgress(null)
        }
      }, 2000)
    } catch (e) {
      setChecking(false)
      setCheckProgress(null)
      alert(e.response?.data?.detail || 'Ошибка запуска проверки')
    }
  }
  const [exportProgress, setExportProgress] = useState(null) // {current, total, ok, failed, current_phone, status}
  const [exportJobId, setExportJobId] = useState(null)
  const exportPollRef = useRef(null)

  const stopExportPoll = () => {
    if (exportPollRef.current) {
      clearInterval(exportPollRef.current)
      exportPollRef.current = null
    }
  }

  const handleBulkExportTData = async () => {
    if (!selectedIds.size) return
    if (selectedIds.size > 500) {
      alert(`Слишком много за раз: ${selectedIds.size}. Лимит 500 — разбей на пачки.`)
      return
    }
    const estMin = Math.max(1, Math.round(selectedIds.size * 10 / 60))
    if (!window.confirm(
      `Экспортировать TData для ${selectedIds.size} аккаунтов в один ZIP?\n\n` +
      `Каждый экспорт коннектится к Telegram через прокси аккаунта — обязательно ` +
      `чтобы у всех выбранных был прокси (иначе вернётся 400). Между акками ` +
      `5-10 сек пауза — anti-flood защита.\n\n` +
      `Это займёт примерно ${estMin} мин. Можно отменить — экспортированные акки уйдут в архив частично.`
    )) return

    // Генерируем job_id для прогресса/отмены
    const jobId = (crypto.randomUUID && crypto.randomUUID()) ||
      `job_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`
    setExportJobId(jobId)
    setExportProgress({ status: 'running', current: 0, total: selectedIds.size, ok: 0, failed: 0, current_phone: '', message: 'Запуск...' })
    setBulkExporting(true)

    // Поллинг прогресса
    exportPollRef.current = setInterval(async () => {
      try {
        const { data } = await accountsAPI.bulkExportTDataProgress(jobId)
        if (data && data.status && data.status !== 'idle') {
          setExportProgress(data)
        }
      } catch { /* ignore */ }
    }, 2000)

    try {
      const { data } = await accountsAPI.bulkExportTData([...selectedIds], jobId)
      // Скачиваем blob как файл
      const url = window.URL.createObjectURL(new Blob([data], { type: 'application/zip' }))
      const a = document.createElement('a')
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T-]/g, '').slice(2)
      a.href = url
      a.download = `tdata_bulk_${selectedIds.size}acc_${stamp}.zip`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      window.URL.revokeObjectURL(url)
    } catch (err) {
      let msg = 'Ошибка экспорта TData'
      try {
        if (err.response?.data instanceof Blob) {
          const text = await err.response.data.text()
          try { msg = JSON.parse(text).detail || text } catch { msg = text }
        } else if (err.response?.data?.detail) {
          msg = err.response.data.detail
        }
      } catch { /* ignore */ }
      // Если 499 — это отмена пользователем, не ошибка
      if (err.response?.status !== 499) {
        alert(msg)
      }
    } finally {
      stopExportPoll()
      setBulkExporting(false)
      setExportJobId(null)
      // Прогресс прячем через 4 сек чтобы юзер увидел финальный статус
      setTimeout(() => setExportProgress(null), 4000)
    }
  }

  const handleCancelExport = async () => {
    if (!exportJobId) return
    if (!window.confirm('Отменить экспорт? Уже экспортированные акк. попадут в архив, остальные — нет.')) return
    try {
      await accountsAPI.bulkExportTDataCancel(exportJobId)
    } catch { /* ignore */ }
    setExportProgress(p => p ? { ...p, message: 'Останавливаю...', status: 'cancelling' } : p)
  }

  const [filterStatus, setFilterStatus] = useState('all')
  const [filterGeo, setFilterGeo] = useState('all')
  const [filterCategory, setFilterCategory] = useState('all')
  const [geoList, setGeoList] = useState([])
  const [categoryList, setCategoryList] = useState([])
  const [newGeoInput, setNewGeoInput] = useState(false)
  const [newCatInput, setNewCatInput] = useState(false)
  const [selected, setSelected] = useState(null)
  const [addModal, setAddModal] = useState(false)
  const [editModal, setEditModal] = useState(false)
  const [phone, setPhone] = useState('')
  const [addApiAppId, setAddApiAppId] = useState('')
  const [addProxyId, setAddProxyId] = useState('')
  const [apiApps, setApiApps] = useState([])
  const [addProxies, setAddProxies] = useState([])
  const [editData, setEditData] = useState({})
  const [saving, setSaving] = useState(false)
  const navigate = useNavigate()

  // Import modals
  const [importModal, setImportModal] = useState(false)
  const [importType, setImportType] = useState(null) // 'session' | 'tdata' | 'session-batch'
  const [importFiles, setImportFiles] = useState([])
  const [importPhone, setImportPhone] = useState('')
  const [importing, setImporting] = useState(false)
  const [importResult, setImportResult] = useState(null)
  const [importProxyId, setImportProxyId] = useState(null)
  const [importProxies, setImportProxies] = useState([])
  const [tdataDetected, setTdataDetected] = useState([])  // [{index, phone, name, proxy_string}]
  const [tdataSessionId, setTdataSessionId] = useState(null)
  const [tdataStep, setTdataStep] = useState('upload')  // upload | assign | importing
  const fileInputRef = useRef(null)
  const avatarInputRef = useRef(null)
  const postPhotoInputRef = useRef(null)

  // Channel creation
  const [channelModal, setChannelModal] = useState(false)
  const [channelAccount, setChannelAccount] = useState(null)
  const [channelForm, setChannelForm] = useState({ title: '', username: '', description: '', first_post: '', pin_to_profile: true })
  const [channelAvatar, setChannelAvatar] = useState(null)
  const [channelPostPhoto, setChannelPostPhoto] = useState(null)
  const [channelAvatarPreview, setChannelAvatarPreview] = useState(null)
  const [channelPostPhotoPreview, setChannelPostPhotoPreview] = useState(null)
  const [creating, setCreating] = useState(false)
  const [creatingStage, setCreatingStage] = useState(null) // 'setup' | 'reconnect' | 'post'
  const [channelResult, setChannelResult] = useState(null)

  // Diagnostics: test join
  const [diagModal, setDiagModal] = useState(false)
  const [diagAccountId, setDiagAccountId] = useState(null)
  const [diagChannel, setDiagChannel] = useState('')
  const [diagLeaveAfter, setDiagLeaveAfter] = useState(false)
  const [diagRunning, setDiagRunning] = useState(false)
  const [diagResult, setDiagResult] = useState(null)

  const load = async () => {
    setLoading(true)
    try {
      const [accRes, filRes] = await Promise.all([accountsAPI.list(), accountsAPI.filters().catch(() => ({ data: { geos: [], categories: [] } }))])
      setAccounts(accRes.data)
      setGeoList(filRes.data.geos || [])
      setCategoryList(filRes.data.categories || [])
      // Чистим selectedIds от акков которых больше нет в БД (удалены /
      // не принадлежат юзеру). Иначе если из localStorage подтянутся
      // мёртвые id, они будут учитываться в bulk-действиях.
      const validIds = new Set((accRes.data || []).map(a => a.id))
      setSelectedIds(prev => {
        let dirty = false
        const next = new Set()
        for (const id of prev) {
          if (validIds.has(id)) next.add(id)
          else dirty = true
        }
        return dirty ? next : prev
      })
    } catch { }
    setLoading(false)
  }

  useEffect(() => { load() }, [])

  // Стоп-интервал поллинга прогресса экспорта при размонтировании
  useEffect(() => {
    return () => stopExportPoll()
  }, [])

  // ── Список всех активных prep-plan job'ов юзера (Redis TTL=7д) ──
  // Читаем на mount, показываем в UI в виде списка. Если в localStorage
  // ничего нет, но на бэке живой running job — авто-подхватываем.
  const [prepPlanList, setPrepPlanList] = useState([])
  const [prepPlanListLoading, setPrepPlanListLoading] = useState(false)
  const loadPrepPlanList = async () => {
    setPrepPlanListLoading(true)
    try {
      const { data } = await accountsAPI.bulkPreparePlanList()
      const jobs = data.jobs || []
      setPrepPlanList(jobs)
      // Автоподхват живой задачи:
      //   - если LS пуст → берём первую running/scheduled
      //   - если LS указывает на задачу которой уже нет (или phase=done/
      //     cancelled/error) → переключаем на живой running (если есть)
      // Это чинит кейс когда старый prep_active_job в LS блокировал
      // отображение badge для новой активной кампании.
      const ACTIVE_PHASES = ['running', 'scheduled', 'generating', 'cancelling']
      const active = jobs.find(j => ACTIVE_PHASES.includes(j.phase))
      const lsJob = jobs.find(j => j.job_id === prepJobId)
      const lsJobDead = prepJobId && (!lsJob || !ACTIVE_PHASES.includes(lsJob.phase))
      if ((!prepJobId || lsJobDead) && active) {
        console.log('[prep] auto-switch to live job', active.job_id, 'phase=', active.phase)
        setPrepJobId(active.job_id)
        setPrepMode2('plan')
        startPrepPolling(active.job_id, 'plan')
      }
    } catch (e) {
      console.error('prep list', e)
    } finally {
      setPrepPlanListLoading(false)
    }
  }
  useEffect(() => {
    loadPrepPlanList()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── Rehydrate prep job из localStorage ──
  // Если юзер закрыл модалку или обновил страницу пока идёт подготовка,
  // возобновляем poll'инг чтобы floating badge показал статус.
  useEffect(() => {
    if (!prepJobId) return
    const fn = prepMode2 === 'plan'
      ? accountsAPI.bulkPreparePlanProgress
      : accountsAPI.bulkPrepareProgress
    let cancelled = false
    fn(prepJobId).then(({ data }) => {
      if (cancelled) return
      if (!data || data.status === 'idle') {
        // job не найден в Redis — старый прогон, чистим localStorage
        setPrepJobId(null)
        return
      }
      setPrepProgress({ ...data, _mode: prepMode2 })
      // Если ещё не готов — возобновляем polling
      if (data.status !== 'done' && data.status !== 'error') {
        startPrepPolling(prepJobId, prepMode2)
      }
    }).catch(() => {
      if (!cancelled) setPrepJobId(null)
    })
    return () => {
      cancelled = true
      if (prepPollRef.current) clearInterval(prepPollRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Превью загруженных фото в модалке создания канала
  useEffect(() => {
    if (!channelAvatar) { setChannelAvatarPreview(null); return }
    const url = URL.createObjectURL(channelAvatar)
    setChannelAvatarPreview(url)
    return () => URL.revokeObjectURL(url)
  }, [channelAvatar])
  useEffect(() => {
    if (!channelPostPhoto) { setChannelPostPhotoPreview(null); return }
    const url = URL.createObjectURL(channelPostPhoto)
    setChannelPostPhotoPreview(url)
    return () => URL.revokeObjectURL(url)
  }, [channelPostPhoto])

  const filtered = accounts.filter(a => {
    const q = search.toLowerCase()
    const matchSearch = !q || [a.phone, a.username, a.first_name, a.last_name, a.status, a.geo, a.category].some(v => (v || '').toLowerCase().includes(q))
    const matchStatus = filterStatus === 'all' || a.status === filterStatus
    const matchGeo = filterGeo === 'all' || (a.geo || '') === filterGeo
    const matchCategory = filterCategory === 'all' || (a.category || '') === filterCategory
    return matchSearch && matchStatus && matchGeo && matchCategory
  })

  const openAddModal = async () => {
    setPhone(''); setAddApiAppId(''); setAddProxyId(''); setAddModal(true)
    try { const { data } = await apiAppsAPI.list(); setApiApps(data || []) }
    catch { setApiApps([]) }
    try { const { data } = await proxiesAPI.list(); setAddProxies(data || []) }
    catch { setAddProxies([]) }
  }

  const handleAdd = async (e) => {
    e.preventDefault(); setSaving(true)
    try {
      await accountsAPI.create(
        phone.startsWith('+') ? phone : '+' + phone,
        addApiAppId ? parseInt(addApiAppId) : null,
        addProxyId ? parseInt(addProxyId) : null,
      )
      setAddModal(false); setPhone(''); setAddApiAppId(''); setAddProxyId(''); await load()
    } catch (err) { alert(err.response?.data?.detail || 'Ошибка') }
    setSaving(false)
  }

  const handleEdit = async (e) => {
    e.preventDefault(); setSaving(true)
    try {
      await accountsAPI.update(selected.id, editData)
      setEditModal(false); await load()
    } catch (err) { alert(err.response?.data?.detail || 'Ошибка') }
    setSaving(false)
  }

  const handleDelete = async (id) => {
    if (!window.confirm('Удалить аккаунт?')) return
    try { await accountsAPI.delete(id); await load() } catch { }
  }

  const openEdit = (acc, e) => {
    e.stopPropagation()
    setSelected(acc)
    setEditData({ role: acc.role, notes: acc.notes || '', tags: acc.tags || [], geo: acc.geo || '', category: acc.category || '' })
    setEditModal(true)
  }

  const openChannelModal = (acc, e) => {
    e.stopPropagation()
    setChannelAccount(acc)
    setChannelForm({ title: '', username: '', description: '', first_post: '', pin_to_profile: true })
    setChannelAvatar(null)
    setChannelPostPhoto(null)
    setChannelResult(null)
    setChannelModal(true)
  }

  const handleCreateChannel = async (e) => {
    e.preventDefault()
    setCreating(true)
    setCreatingStage('setup')
    const hasFirstPost = !!(channelForm.first_post.trim() || channelPostPhoto)
    // Этапы по времени совпадают с серверным flow (см. POST /channels/create-full)
    const stageTimers = []
    if (hasFirstPost) {
      stageTimers.push(setTimeout(() => setCreatingStage('reconnect'), 6000))
      stageTimers.push(setTimeout(() => setCreatingStage('post'), 9000))
    }
    try {
      const fd = new FormData()
      fd.append('account_id', channelAccount.id)
      fd.append('title', channelForm.title)
      fd.append('description', channelForm.description)
      fd.append('username', channelForm.username.replace(/^@/, ''))
      fd.append('first_post', channelForm.first_post)
      fd.append('pin_to_profile', channelForm.pin_to_profile)
      if (channelAvatar) fd.append('avatar', channelAvatar)
      if (channelPostPhoto) fd.append('post_photo', channelPostPhoto)
      const { data } = await channelsAPI.createFull(fd)
      setChannelResult({ success: data.success, channel: data.channel, pinned: data.pinned_to_profile, hasPost: data.first_post_published, avatarSet: data.avatar_set })
      await load()
    } catch (err) {
      setChannelResult({ success: false, error: err.response?.data?.detail || 'Ошибка создания канала' })
    }
    stageTimers.forEach(clearTimeout)
    setCreatingStage(null)
    setCreating(false)
  }

  return (
    <div style={{ padding: '28px 32px', animation: 'fadeUp 0.4s cubic-bezier(0.16,1,0.3,1)' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 28 }}>
        <div>
          <div style={{ fontSize: 11, color: 'var(--blue)', fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>◉ АККАУНТЫ</div>
          <h1 style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.04em' }}>Управление аккаунтами</h1>
          <p style={{ fontSize: 13, color: 'var(--text-3)', marginTop: 4 }}>{accounts.length} аккаунтов в базе</p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <Button variant="ghost" onClick={() => {
            setDiagAccountId(accounts[0]?.id || null); setDiagChannel(''); setDiagResult(null); setDiagLeaveAfter(false); setDiagModal(true)
          }}>🔍 Тест подписки</Button>
          <Button variant="ghost" onClick={() => { setImportType(null); setImportResult(null); setImportFiles([]); setImportPhone(''); setImportModal(true) }}>📦 Импорт</Button>
          <Button variant="primary" onClick={openAddModal}>+ Добавить</Button>
        </div>
      </div>

      {/* Filters */}
      <div style={{ display: 'flex', gap: 10, marginBottom: 20, flexWrap: 'wrap' }}>
        <div style={{ flex: 1, maxWidth: 300 }}>
          <Input placeholder="🔍  Поиск..." value={search} onChange={e => setSearch(e.target.value)} />
        </div>
        <div style={{ display: 'flex', gap: 6 }}>
          {STATUS_FILTERS.map(({ key, label }) => {
            const count = key === 'all'
              ? accounts.length
              : accounts.filter(a => a.status === key).length
            return (
              <button key={key} onClick={() => setFilterStatus(key)} style={{
                padding: '9px 14px', borderRadius: 10, cursor: 'pointer',
                border: `1px solid ${filterStatus === key ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                background: filterStatus === key ? 'rgba(124,77,255,0.15)' : 'transparent',
                color: filterStatus === key ? 'var(--violet)' : 'var(--text-2)',
                fontSize: 12, fontWeight: filterStatus === key ? 600 : 400,
                transition: 'all 0.15s',
              }}>{label} <span style={{ opacity: 0.6, marginLeft: 4 }}>{count}</span></button>
            )
          })}
        </div>
      </div>

      {/* Фильтры: Гео + Тематика */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 14 }}>
        {/* Гео */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 10, color: 'var(--text-3)', fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase', minWidth: 70 }}>🌍 Гео</span>
          <button onClick={() => setFilterGeo('all')} style={{
            padding: '5px 12px', borderRadius: 20, cursor: 'pointer', fontSize: 11, fontWeight: filterGeo === 'all' ? 600 : 400, transition: 'all 0.15s',
            border: `1px solid ${filterGeo === 'all' ? 'rgba(59,130,246,0.4)' : 'var(--border)'}`,
            background: filterGeo === 'all' ? 'rgba(59,130,246,0.15)' : 'transparent',
            color: filterGeo === 'all' ? 'var(--blue)' : 'var(--text-3)',
          }}>Все</button>
          {geoList.map(g => (
            <button key={g} onClick={() => setFilterGeo(filterGeo === g ? 'all' : g)} style={{
              padding: '5px 12px', borderRadius: 20, cursor: 'pointer', fontSize: 11, fontWeight: filterGeo === g ? 600 : 400, transition: 'all 0.15s',
              border: `1px solid ${filterGeo === g ? 'rgba(59,130,246,0.4)' : 'var(--border)'}`,
              background: filterGeo === g ? 'rgba(59,130,246,0.15)' : 'transparent',
              color: filterGeo === g ? 'var(--blue)' : 'var(--text-3)',
            }}>{g}</button>
          ))}
          {!newGeoInput ? (
            <button onClick={() => setNewGeoInput(true)} style={{
              padding: '5px 10px', borderRadius: 20, cursor: 'pointer', fontSize: 11,
              border: '1px dashed var(--border)', background: 'transparent', color: 'var(--text-3)',
            }}>+ Добавить</button>
          ) : (
            <form onSubmit={e => { e.preventDefault(); const v = e.target.geo.value.trim(); if (v && !geoList.includes(v)) { setGeoList([...geoList, v]); } setNewGeoInput(false) }} style={{ display: 'flex', gap: 4 }}>
              <input name="geo" autoFocus placeholder="UA, US, DE..." style={{
                padding: '4px 10px', borderRadius: 20, border: '1px solid rgba(59,130,246,0.4)', background: 'rgba(59,130,246,0.08)',
                color: 'var(--text)', fontSize: 11, outline: 'none', width: 80,
              }} />
              <button type="submit" style={{ padding: '4px 8px', borderRadius: 20, border: '1px solid rgba(61,214,140,0.4)', background: 'rgba(61,214,140,0.1)', color: 'var(--green)', fontSize: 11, cursor: 'pointer' }}>✓</button>
              <button type="button" onClick={() => setNewGeoInput(false)} style={{ padding: '4px 8px', borderRadius: 20, border: '1px solid var(--border)', background: 'transparent', color: 'var(--text-3)', fontSize: 11, cursor: 'pointer' }}>✕</button>
            </form>
          )}
        </div>

        {/* Тематика */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 10, color: 'var(--text-3)', fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase', minWidth: 70 }}>📁 Тема</span>
          <button onClick={() => setFilterCategory('all')} style={{
            padding: '5px 12px', borderRadius: 20, cursor: 'pointer', fontSize: 11, fontWeight: filterCategory === 'all' ? 600 : 400, transition: 'all 0.15s',
            border: `1px solid ${filterCategory === 'all' ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
            background: filterCategory === 'all' ? 'rgba(124,77,255,0.15)' : 'transparent',
            color: filterCategory === 'all' ? 'var(--violet)' : 'var(--text-3)',
          }}>Все</button>
          {categoryList.map(c => (
            <button key={c} onClick={() => setFilterCategory(filterCategory === c ? 'all' : c)} style={{
              padding: '5px 12px', borderRadius: 20, cursor: 'pointer', fontSize: 11, fontWeight: filterCategory === c ? 600 : 400, transition: 'all 0.15s',
              border: `1px solid ${filterCategory === c ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
              background: filterCategory === c ? 'rgba(124,77,255,0.15)' : 'transparent',
              color: filterCategory === c ? 'var(--violet)' : 'var(--text-3)',
            }}>{c}</button>
          ))}
          {!newCatInput ? (
            <button onClick={() => setNewCatInput(true)} style={{
              padding: '5px 10px', borderRadius: 20, cursor: 'pointer', fontSize: 11,
              border: '1px dashed var(--border)', background: 'transparent', color: 'var(--text-3)',
            }}>+ Добавить</button>
          ) : (
            <form onSubmit={e => { e.preventDefault(); const v = e.target.cat.value.trim(); if (v && !categoryList.includes(v)) { setCategoryList([...categoryList, v]); } setNewCatInput(false) }} style={{ display: 'flex', gap: 4 }}>
              <input name="cat" autoFocus placeholder="Крипто, Новини..." style={{
                padding: '4px 10px', borderRadius: 20, border: '1px solid rgba(124,77,255,0.4)', background: 'rgba(124,77,255,0.08)',
                color: 'var(--text)', fontSize: 11, outline: 'none', width: 120,
              }} />
              <button type="submit" style={{ padding: '4px 8px', borderRadius: 20, border: '1px solid rgba(61,214,140,0.4)', background: 'rgba(61,214,140,0.1)', color: 'var(--green)', fontSize: 11, cursor: 'pointer' }}>✓</button>
              <button type="button" onClick={() => setNewCatInput(false)} style={{ padding: '4px 8px', borderRadius: 20, border: '1px solid var(--border)', background: 'transparent', color: 'var(--text-3)', fontSize: 11, cursor: 'pointer' }}>✕</button>
            </form>
          )}
        </div>
      </div>

      {/* Table */}
      {loading ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 64 }}><Spinner size={28} /></div>
      ) : filtered.length === 0 ? (
        <Empty icon="👤" title="Нет аккаунтов"
          subtitle={search || filterStatus !== 'all' ? 'Попробуй изменить фильтры' : 'Добавь первый аккаунт'}
          action={!search && filterStatus === 'all' && <Button variant="primary" onClick={openAddModal}>+ Добавить</Button>} />
      ) : (
        <div style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', overflow: 'hidden' }}>
          {/* Header row */}
          <div style={{
            display: 'grid', gridTemplateColumns: '36px 2fr 1.2fr 0.8fr 0.8fr 1fr 80px 130px',
            padding: '10px 20px', borderBottom: '1px solid var(--border)',
            fontSize: 10, color: 'var(--text-3)', letterSpacing: '0.1em', fontWeight: 700, textTransform: 'uppercase',
            alignItems: 'center',
          }}>
            <input type="checkbox"
              onClick={e => e.stopPropagation()}
              checked={filtered.length > 0 && filtered.every(a => selectedIds.has(a.id))}
              onChange={e => e.target.checked ? selectAll(filtered.map(a => a.id)) : clearSelection()}
              style={{ width: 16, height: 16, accentColor: 'var(--violet)', cursor: 'pointer' }}
              title="Выбрать все на странице" />
            <span>Аккаунт</span><span>Телефон</span><span>Гео</span><span>Тема</span><span>Статус</span><span>Trust</span><span style={{ textAlign: 'right' }}>Действия</span>
          </div>

          {filtered.map((acc, i) => (
            <div key={acc.id} onClick={() => navigate(`/accounts/${acc.id}`)} style={{
              display: 'grid', gridTemplateColumns: '36px 2fr 1.2fr 0.8fr 0.8fr 1fr 80px 130px',
              padding: '14px 20px', alignItems: 'center',
              borderBottom: i < filtered.length - 1 ? '1px solid var(--border)' : 'none',
              transition: 'background 0.1s', cursor: 'pointer',
              // Активная кампания → лёгкая зелёная подложка + узкая полоса слева
              background: selectedIds.has(acc.id)
                ? 'rgba(124,77,255,0.06)'
                : acc.active_campaign ? 'rgba(61,214,140,0.05)' : 'transparent',
              boxShadow: acc.active_campaign && !selectedIds.has(acc.id)
                ? 'inset 3px 0 0 0 var(--green)' : 'none',
            }}
              onMouseEnter={e => {
                if (!selectedIds.has(acc.id)) {
                  e.currentTarget.style.background = acc.active_campaign
                    ? 'rgba(61,214,140,0.10)' : 'rgba(255,255,255,0.02)'
                }
              }}
              onMouseLeave={e => {
                e.currentTarget.style.background = selectedIds.has(acc.id)
                  ? 'rgba(124,77,255,0.06)'
                  : acc.active_campaign ? 'rgba(61,214,140,0.05)' : 'transparent'
              }}>
              {/* Checkbox — расширенная кликабельная область на всю ячейку
                  чтобы промах по 16×16 чекбоксу не открывал профиль.
                  Ячейка колонки = 36px ширины × вся высота строки. */}
              <div
                onClick={e => {
                  e.stopPropagation()
                  toggleSelect(acc.id)
                }}
                style={{
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  alignSelf: 'stretch',      // растягиваем на всю высоту строки
                  cursor: 'pointer',
                  minHeight: 44,             // гарантия достаточной вертикальной зоны попадания
                }}>
                <input type="checkbox"
                  checked={selectedIds.has(acc.id)}
                  onChange={() => {}}        // управляется через onClick обёртки
                  readOnly
                  style={{
                    width: 16, height: 16, accentColor: 'var(--violet)',
                    pointerEvents: 'none',    // клики летят на обёртку
                  }} />
              </div>
              {/* Name + avatar */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <div style={{
                  width: 32, height: 32, borderRadius: 8, flexShrink: 0,
                  background: 'linear-gradient(135deg, rgba(124,77,255,0.25), rgba(61,139,255,0.15))',
                  border: '1px solid rgba(124,77,255,0.15)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: 12, fontWeight: 700, color: 'var(--violet)',
                }}>{acc.first_name?.[0]?.toUpperCase() || '?'}</div>
                <div>
                  <div style={{ fontWeight: 600, fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}>
                    {acc.first_name || 'Без имени'} {acc.last_name || ''}
                    {acc.active_campaign && (
                      <span
                        title={`Активная кампания: ${acc.active_campaign.name}`}
                        onClick={e => { e.stopPropagation(); navigate('/commenting') }}
                        style={{
                          fontSize: 10, padding: '2px 7px', borderRadius: 8,
                          background: 'rgba(61,214,140,0.15)', color: 'var(--green)',
                          border: '1px solid rgba(61,214,140,0.3)', cursor: 'pointer',
                          maxWidth: 140, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                        }}>
                        {acc.active_campaign.name}
                      </span>
                    )}
                  </div>
                  {acc.username && <div style={{ fontSize: 10, color: 'var(--text-3)' }}>@{acc.username}</div>}
                </div>
              </div>
              <div>
                <div style={{ fontSize: 12, fontFamily: 'var(--font-mono)', color: 'var(--text-2)' }}>{acc.phone}</div>
                {/* Прокси под номером: флаг + страна/город или серый — */}
                <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 2, display: 'flex', alignItems: 'center', gap: 4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {!acc.proxy_id ? (
                    <span style={{ color: 'var(--red)', opacity: 0.7 }}>⛔ без прокси</span>
                  ) : acc.proxy_country_code || acc.proxy_country ? (
                    <>
                      <span>{countryFlag(acc.proxy_country_code) || '🌐'}</span>
                      <span>
                        {[acc.proxy_country, acc.proxy_city].filter(Boolean).join(', ') || acc.proxy_host || '?'}
                      </span>
                    </>
                  ) : (
                    <span title="Прокси без гео-данных — нажми «Проверить» на странице Прокси">
                      🌐 {acc.proxy_host || `#${acc.proxy_id}`}
                    </span>
                  )}
                </div>
              </div>
              <div>{acc.geo ? <span style={{ fontSize: 11, padding: '2px 8px', borderRadius: 10, background: 'rgba(59,130,246,0.1)', color: 'rgba(59,130,246,0.8)', border: '1px solid rgba(59,130,246,0.2)' }}>{acc.geo}</span> : <span style={{ fontSize: 10, color: 'var(--text-3)' }}>—</span>}</div>
              <div>{acc.category ? <span style={{ fontSize: 11, padding: '2px 8px', borderRadius: 10, background: 'rgba(124,77,255,0.1)', color: 'rgba(124,77,255,0.8)', border: '1px solid rgba(124,77,255,0.2)' }}>{acc.category}</span> : <span style={{ fontSize: 10, color: 'var(--text-3)' }}>—</span>}</div>
              <StatusBadge status={acc.status} />
              <TrustBar score={acc.trust_score} />
              <div style={{ display: 'flex', gap: 4, justifyContent: 'flex-end' }}>
                <button onClick={(e) => openChannelModal(acc, e)} title="Создать канал" style={{
                  width: 28, height: 28, borderRadius: 6, border: '1px solid var(--border)',
                  background: 'transparent', color: 'var(--text-3)', cursor: 'pointer',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 13,
                  transition: 'all 0.15s',
                }}
                  onMouseEnter={e => { e.currentTarget.style.borderColor = 'rgba(0,194,178,0.5)'; e.currentTarget.style.color = 'var(--teal)' }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.color = 'var(--text-3)' }}>
                  📺
                </button>
                <button onClick={(e) => openEdit(acc, e)} title="Редактировать" style={{
                  width: 28, height: 28, borderRadius: 6, border: '1px solid var(--border)',
                  background: 'transparent', color: 'var(--text-3)', cursor: 'pointer',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12,
                  transition: 'all 0.15s',
                }}
                  onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--violet)'; e.currentTarget.style.color = 'var(--violet)' }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.color = 'var(--text-3)' }}>
                  ✎
                </button>
                <button onClick={(e) => { e.stopPropagation(); handleDelete(acc.id) }} title="Удалить" style={{
                  width: 28, height: 28, borderRadius: 6, border: '1px solid var(--border)',
                  background: 'transparent', color: 'var(--text-3)', cursor: 'pointer',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12,
                  transition: 'all 0.15s',
                }}
                  onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--red)'; e.currentTarget.style.color = 'var(--red)' }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.color = 'var(--text-3)' }}>
                  ✕
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Add modal */}
      <Modal open={addModal} onClose={() => setAddModal(false)} title="Добавить аккаунт" width={420}>
        <form onSubmit={handleAdd} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ padding: '10px 14px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>
            Введите номер телефона аккаунта. После добавления используйте авторизацию на странице деталей аккаунта.
          </div>
          <Input label="Номер телефона" value={phone} onChange={e => setPhone(e.target.value)} placeholder="+380..." required autoFocus />

          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>
              API ID (приложение)
            </label>
            <select value={addApiAppId} onChange={e => setAddApiAppId(e.target.value)}
              style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)', color: 'var(--text)', fontSize: 14, outline: 'none' }}>
              <option value="">Публичный (Android по умолчанию)</option>
              {apiApps.map(a => (
                <option key={a.id} value={a.id}>
                  {a.title || `API ${a.api_id}`} · id={a.api_id} · {a.platform || 'android'}
                  {a.is_active === false ? ' (выкл)' : ''}
                </option>
              ))}
            </select>
            <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4 }}>
              Аккаунт будет авторизован через выбранный API ID. Управление ключами — на странице «API ключи».
            </div>
          </div>

          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>
              Прокси
            </label>
            <select value={addProxyId} onChange={e => setAddProxyId(e.target.value)}
              style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)', color: 'var(--text)', fontSize: 14, outline: 'none' }}>
              <option value="">Без прокси (назначу позже)</option>
              {addProxies.filter(p => p.is_valid !== false).map(p => (
                <option key={p.id} value={p.id}>{proxyLabel(p)}</option>
              ))}
            </select>
            <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4 }}>
              Прокси привяжется к аккаунту сразу и будет предвыбран при авторизации.
            </div>
          </div>

          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" type="button" onClick={() => setAddModal(false)}>Отмена</Button>
            <Button variant="primary" type="submit" loading={saving}>Добавить</Button>
          </div>
        </form>
      </Modal>

      {/* Edit modal */}
      <Modal open={editModal} onClose={() => setEditModal(false)} title={`Редактировать · ${selected?.phone || ''}`} width={440}>
        <form onSubmit={handleEdit} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Роль</label>
            <select value={editData.role || 'default'} onChange={e => setEditData(d => ({ ...d, role: e.target.value }))} style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)', color: 'var(--text)', fontSize: 14, outline: 'none' }}>
              {ROLES.map(r => <option key={r} value={r}>{r === 'default' ? 'Без роли' : r}</option>)}
            </select>
          </div>
          <Input label="Заметки" value={editData.notes || ''} onChange={e => setEditData(d => ({ ...d, notes: e.target.value }))} placeholder="Заметки об аккаунте" />

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>🌍 Гео</label>
              <input list="geo-list" value={editData.geo || ''} onChange={e => setEditData(d => ({ ...d, geo: e.target.value }))}
                placeholder="Введи или выбери" style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 13, outline: 'none' }} />
              <datalist id="geo-list">{geoList.map(g => <option key={g} value={g} />)}</datalist>
            </div>
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>📁 Тематика</label>
              <input list="cat-list" value={editData.category || ''} onChange={e => setEditData(d => ({ ...d, category: e.target.value }))}
                placeholder="Введи или выбери" style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 13, outline: 'none' }} />
              <datalist id="cat-list">{categoryList.map(c => <option key={c} value={c} />)}</datalist>
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" type="button" onClick={() => setEditModal(false)}>Отмена</Button>
            <Button variant="primary" type="submit" loading={saving}>Сохранить</Button>
          </div>
        </form>
      </Modal>

      {/* Diagnostics: test join modal */}
      <Modal open={diagModal} onClose={() => setDiagModal(false)} title="🔍 Тест подписки на канал" width={620}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ padding: '10px 14px', background: 'rgba(61,139,255,0.06)', border: '1px solid rgba(61,139,255,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>
            Симулирует тот же процесс что делает <b>plan_executor</b> при подписке на канал в кампании.
            Покажет каждый шаг и точную ошибку Telegram — резолв, pre-check, JoinRequest, верификация.
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Аккаунт</label>
              <select value={diagAccountId || ''} onChange={e => setDiagAccountId(e.target.value ? parseInt(e.target.value) : null)}
                style={{ width: '100%', padding: '10px 12px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 13, outline: 'none' }}>
                <option value="">— Выбери аккаунт —</option>
                {accounts.map(a => (
                  <option key={a.id} value={a.id}>
                    {a.first_name || a.phone} {a.username ? `(@${a.username})` : ''} · {a.status}
                  </option>
                ))}
              </select>
            </div>
            <Input label="Канал (@username)" value={diagChannel}
              onChange={e => setDiagChannel(e.target.value)}
              placeholder="@DC_Draino или DC_Draino" />
          </div>

          <label style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', fontSize: 13, color: 'var(--text-2)' }}>
            <input type="checkbox" checked={diagLeaveAfter} onChange={e => setDiagLeaveAfter(e.target.checked)}
              style={{ width: 16, height: 16, accentColor: 'var(--violet)', cursor: 'pointer' }} />
            <span>Выйти из канала после теста (только если только что вступили)</span>
          </label>

          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" onClick={() => setDiagModal(false)}>Закрыть</Button>
            <Button variant="primary" loading={diagRunning}
              disabled={!diagAccountId || !diagChannel.trim()}
              onClick={async () => {
                setDiagRunning(true); setDiagResult(null)
                try {
                  const { data } = await diagnosticsAPI.testJoin(diagAccountId, diagChannel.trim(), diagLeaveAfter)
                  setDiagResult(data)
                } catch (err) {
                  setDiagResult({
                    success: false,
                    error: err.response?.data?.detail || err.message || 'Ошибка',
                    error_type: 'NetworkError',
                    steps: [],
                  })
                }
                setDiagRunning(false)
              }}>
              ▶️ Запустить тест
            </Button>
          </div>

          {diagResult && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 4 }}>
              {/* Summary */}
              <div style={{
                padding: '14px 16px', borderRadius: 10,
                background: diagResult.success ? 'var(--green-dim)' : 'var(--red-dim)',
                border: `1px solid ${diagResult.success ? 'rgba(61,214,140,0.25)' : 'rgba(248,81,73,0.25)'}`,
              }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <div style={{ fontSize: 15, fontWeight: 700, color: diagResult.success ? 'var(--green)' : 'var(--red)' }}>
                    {diagResult.success
                      ? (diagResult.already_in ? '✓ Уже подписан' : '✓ Подписка работает')
                      : '✕ Подписка не прошла'}
                  </div>
                  {diagResult.elapsed_seconds != null && (
                    <div style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'var(--font-mono)' }}>{diagResult.elapsed_seconds}с</div>
                  )}
                </div>
                {diagResult.error && (
                  <div style={{ marginTop: 6, fontSize: 12, color: 'var(--text-2)' }}>
                    <b>{diagResult.error_type || 'Error'}:</b> {diagResult.error}
                  </div>
                )}
              </div>

              {/* Steps */}
              {diagResult.steps && diagResult.steps.length > 0 && (
                <div style={{ background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, overflow: 'hidden' }}>
                  {diagResult.steps.map((s, i) => (
                    <div key={i} style={{
                      display: 'flex', gap: 12, padding: '12px 14px',
                      borderBottom: i < diagResult.steps.length - 1 ? '1px solid var(--border)' : 'none',
                      alignItems: 'flex-start',
                    }}>
                      <div style={{
                        width: 24, height: 24, borderRadius: 12, flexShrink: 0,
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                        background: s.ok ? 'var(--green-dim)' : 'var(--red-dim)',
                        color: s.ok ? 'var(--green)' : 'var(--red)',
                        fontSize: 12, fontWeight: 700,
                      }}>{s.ok ? '✓' : '✕'}</div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)' }}>{s.label}</span>
                          <span style={{ fontSize: 10, color: 'var(--text-3)', fontFamily: 'var(--font-mono)' }}>{s.step}</span>
                        </div>
                        {s.detail && <div style={{ fontSize: 12, color: 'var(--text-2)', marginTop: 3, lineHeight: 1.5 }}>{s.detail}</div>}
                        {s.error && (
                          <div style={{
                            marginTop: 6, padding: '6px 10px', borderRadius: 6,
                            background: 'rgba(248,81,73,0.08)', border: '1px solid rgba(248,81,73,0.2)',
                            fontSize: 11, fontFamily: 'var(--font-mono)', color: 'var(--red)',
                            wordBreak: 'break-word',
                          }}>
                            {s.error_type && <b>{s.error_type}: </b>}{s.error}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </Modal>

      {/* Channel creation modal */}
      <Modal open={channelModal} onClose={() => { setChannelModal(false); setChannelResult(null) }}
        title={`Создать канал · ${channelAccount?.first_name || channelAccount?.phone || ''}`} width={540}>
        {channelResult ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16, alignItems: 'center', padding: '24px 0', textAlign: 'center' }}>
            {channelResult.success ? (
              <>
                <div style={{ fontSize: 48 }}>📺</div>
                <div style={{ fontSize: 17, fontWeight: 700, color: 'var(--green)' }}>Канал создан!</div>
                {channelResult.channel && (
                  <div style={{ padding: '12px 16px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, width: '100%', textAlign: 'left' }}>
                    <div style={{ fontWeight: 600, fontSize: 14 }}>{channelResult.channel.title}</div>
                    {channelResult.channel.link && <div style={{ fontSize: 12, color: 'var(--teal)', marginTop: 4 }}>{channelResult.channel.link}</div>}
                  </div>
                )}
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, justifyContent: 'center' }}>
                  {channelResult.avatarSet && <span style={{ fontSize: 11, padding: '3px 10px', borderRadius: 6, background: 'var(--green-dim)', color: 'var(--green)', border: '1px solid rgba(61,214,140,0.25)' }}>✓ Аватар загружен</span>}
                  {channelResult.hasPost && <span style={{ fontSize: 11, padding: '3px 10px', borderRadius: 6, background: 'var(--blue-dim)', color: 'var(--blue)', border: '1px solid rgba(61,139,255,0.25)' }}>✓ Первый пост опубликован</span>}
                  {channelResult.pinned && <span style={{ fontSize: 11, padding: '3px 10px', borderRadius: 6, background: 'var(--violet-dim)', color: 'var(--violet)', border: '1px solid rgba(124,77,255,0.25)' }}>✓ Закреплён в профиле</span>}
                </div>
                <Button variant="primary" onClick={() => { setChannelModal(false); setChannelResult(null) }}>Готово</Button>
              </>
            ) : (
              <>
                <div style={{ fontSize: 48 }}>❌</div>
                <div style={{ fontSize: 14, color: 'var(--red)' }}>{channelResult.error}</div>
                <Button variant="ghost" onClick={() => setChannelResult(null)}>← Попробовать снова</Button>
              </>
            )}
          </div>
        ) : (
          <form onSubmit={handleCreateChannel} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <Input label="Название канала *" value={channelForm.title} required
              onChange={e => setChannelForm(f => ({ ...f, title: e.target.value }))}
              placeholder="Мой крутой канал" autoFocus />

            <Input label="@username (необязательно)" value={channelForm.username}
              onChange={e => setChannelForm(f => ({ ...f, username: e.target.value }))}
              placeholder="@mychannel" />

            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Описание</label>
              <textarea value={channelForm.description} rows={2}
                onChange={e => setChannelForm(f => ({ ...f, description: e.target.value }))}
                placeholder="Краткое описание канала"
                style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 14, outline: 'none', resize: 'vertical', fontFamily: 'var(--font-sans)' }} />
            </div>

            <div>
              <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Первый пост</label>
              <textarea value={channelForm.first_post} rows={3}
                onChange={e => setChannelForm(f => ({ ...f, first_post: e.target.value }))}
                placeholder="Текст первого сообщения (необязательно)"
                style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 14, outline: 'none', resize: 'vertical', fontFamily: 'var(--font-sans)' }} />
              <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4, lineHeight: 1.5 }}>
                Пост публикуется отдельным этапом: канал создаётся → сессия переподключается → пост уходит. Так Telegram не режет первое сообщение на свежем канале.
              </div>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
              {/* Avatar upload */}
              <div>
                <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Аватар канала</label>
                <div onClick={() => avatarInputRef.current?.click()} style={{
                  position: 'relative', height: 80, border: '2px dashed var(--border)', borderRadius: 10,
                  display: 'flex', flexDirection: channelAvatarPreview ? 'row' : 'column',
                  alignItems: 'center', justifyContent: 'center',
                  cursor: 'pointer', transition: 'border-color 0.15s', gap: 8, padding: channelAvatarPreview ? '6px 10px' : 0,
                  overflow: 'hidden',
                }}
                  onMouseEnter={e => e.currentTarget.style.borderColor = 'var(--violet)'}
                  onMouseLeave={e => e.currentTarget.style.borderColor = 'var(--border)'}>
                  {channelAvatar ? (
                    <>
                      {channelAvatarPreview && (
                        <img src={channelAvatarPreview} alt="avatar preview"
                          style={{ width: 56, height: 56, borderRadius: '50%', objectFit: 'cover', flexShrink: 0, border: '1px solid var(--border)' }} />
                      )}
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0, flex: 1 }}>
                        <span style={{ fontSize: 11, color: 'var(--green)', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          ✓ {channelAvatar.name}
                        </span>
                        <span style={{ fontSize: 10, color: 'var(--text-3)' }}>нажми чтобы сменить</span>
                      </div>
                      <button type="button" onClick={(ev) => { ev.stopPropagation(); setChannelAvatar(null) }}
                        title="Удалить" style={{
                          position: 'absolute', top: 4, right: 4, width: 18, height: 18, borderRadius: '50%',
                          background: 'var(--bg-2)', border: '1px solid var(--border)', color: 'var(--text-3)',
                          fontSize: 11, cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 0,
                        }}>×</button>
                    </>
                  ) : (
                    <>
                      <span style={{ fontSize: 22 }}>🖼</span>
                      <span style={{ fontSize: 11, color: 'var(--text-3)' }}>выбрать фото</span>
                    </>
                  )}
                </div>
                <input ref={avatarInputRef} type="file" accept="image/*" style={{ display: 'none' }}
                  onChange={e => setChannelAvatar(e.target.files[0] || null)} />
              </div>

              {/* Post photo upload */}
              <div>
                <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Фото к посту</label>
                <div onClick={() => postPhotoInputRef.current?.click()} style={{
                  position: 'relative', height: 80, border: '2px dashed var(--border)', borderRadius: 10,
                  display: 'flex', flexDirection: channelPostPhotoPreview ? 'row' : 'column',
                  alignItems: 'center', justifyContent: 'center',
                  cursor: 'pointer', transition: 'border-color 0.15s', gap: 8, padding: channelPostPhotoPreview ? '6px 10px' : 0,
                  overflow: 'hidden',
                }}
                  onMouseEnter={e => e.currentTarget.style.borderColor = 'var(--violet)'}
                  onMouseLeave={e => e.currentTarget.style.borderColor = 'var(--border)'}>
                  {channelPostPhoto ? (
                    <>
                      {channelPostPhotoPreview && (
                        <img src={channelPostPhotoPreview} alt="post photo preview"
                          style={{ width: 56, height: 56, borderRadius: 8, objectFit: 'cover', flexShrink: 0, border: '1px solid var(--border)' }} />
                      )}
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0, flex: 1 }}>
                        <span style={{ fontSize: 11, color: 'var(--green)', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          ✓ {channelPostPhoto.name}
                        </span>
                        <span style={{ fontSize: 10, color: 'var(--text-3)' }}>нажми чтобы сменить</span>
                      </div>
                      <button type="button" onClick={(ev) => { ev.stopPropagation(); setChannelPostPhoto(null) }}
                        title="Удалить" style={{
                          position: 'absolute', top: 4, right: 4, width: 18, height: 18, borderRadius: '50%',
                          background: 'var(--bg-2)', border: '1px solid var(--border)', color: 'var(--text-3)',
                          fontSize: 11, cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 0,
                        }}>×</button>
                    </>
                  ) : (
                    <>
                      <span style={{ fontSize: 22 }}>📸</span>
                      <span style={{ fontSize: 11, color: 'var(--text-3)' }}>фото к посту</span>
                    </>
                  )}
                </div>
                <input ref={postPhotoInputRef} type="file" accept="image/*" style={{ display: 'none' }}
                  onChange={e => setChannelPostPhoto(e.target.files[0] || null)} />
              </div>
            </div>

            {/* Pin toggle */}
            <label style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', padding: '10px 12px', background: 'var(--bg-3)', borderRadius: 8, border: '1px solid var(--border)' }}>
              <input type="checkbox" checked={channelForm.pin_to_profile}
                onChange={e => setChannelForm(f => ({ ...f, pin_to_profile: e.target.checked }))}
                style={{ width: 16, height: 16, accentColor: 'var(--violet)', cursor: 'pointer' }} />
              <div>
                <div style={{ fontSize: 13, fontWeight: 600 }}>Закрепить канал в профиле</div>
                <div style={{ fontSize: 11, color: 'var(--text-3)' }}>Канал появится как личный канал аккаунта</div>
              </div>
            </label>

            {creating && (
              <div style={{ padding: '10px 12px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
                {[
                  { key: 'setup', label: 'Создаю канал, ставлю аватар и закрепляю в профиле' },
                  { key: 'reconnect', label: 'Переподключаю сессию (2 сек пауза)' },
                  { key: 'post', label: 'Публикую первый пост' },
                ].map((s, i, arr) => {
                  const stages = arr.map(x => x.key)
                  const curIdx = stages.indexOf(creatingStage)
                  const myIdx = stages.indexOf(s.key)
                  const done = myIdx < curIdx
                  const active = myIdx === curIdx
                  return (
                    <div key={s.key} style={{
                      display: 'flex', alignItems: 'center', gap: 8, fontSize: 12,
                      color: done ? 'var(--green)' : active ? 'var(--violet)' : 'var(--text-3)'
                    }}>
                      <span style={{ width: 14 }}>{done ? '✓' : active ? '◐' : '○'}</span>
                      <span style={{ fontWeight: active ? 600 : 400 }}>{s.label}</span>
                    </div>
                  )
                })}
              </div>
            )}

            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <Button variant="ghost" type="button" onClick={() => setChannelModal(false)}>Отмена</Button>
              <Button variant="primary" type="submit" loading={creating}>📺 Создать канал</Button>
            </div>
          </form>
        )}
      </Modal>

      {/* Import modal */}
      <Modal open={importModal} onClose={() => setImportModal(false)} title="Импорт аккаунтов" width={importType === 'web' ? 780 : 520}>
        {!importType ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div style={{ padding: '12px 14px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>
              Выберите способ импорта аккаунтов
            </div>
            {[
              { type: 'session', icon: '📄', title: '.session файл', desc: 'Один файл Telethon/Pyrogram сессии' },
              { type: 'session-batch', icon: '📁', title: 'Пакет .session', desc: 'Несколько .session файлов сразу' },
              { type: 'tdata', icon: '📦', title: 'TData архив (ZIP)', desc: 'Архив папки Telegram Desktop' },
              { type: 'web', icon: '🌐', title: 'Telegram Web (localStorage)', desc: 'Скопированная строка из DevTools (web.telegram.org/k)' },
              { type: 'json', icon: '📥', title: 'Из JSON (CLI)', desc: 'Импорт из data/accounts.json' },
            ].map(({ type, icon, title, desc }) => (
              <div key={type} onClick={() => {
                if (type === 'json') { accountsAPI.importJson().then(load); setImportModal(false); return }
                setImportType(type)
              }} style={{
                padding: '14px 16px', background: 'var(--bg-3)', border: '1px solid var(--border)',
                borderRadius: 12, cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 14,
                transition: 'all 0.15s',
              }}
                onMouseEnter={e => { e.currentTarget.style.borderColor = 'rgba(124,77,255,0.4)'; e.currentTarget.style.background = 'rgba(124,77,255,0.08)' }}
                onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.background = 'var(--bg-3)' }}>
                <span style={{ fontSize: 28 }}>{icon}</span>
                <div>
                  <div style={{ fontWeight: 600, fontSize: 14 }}>{title}</div>
                  <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 2 }}>{desc}</div>
                </div>
              </div>
            ))}
          </div>
        ) : importType === 'web' ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <button onClick={() => setImportType(null)} style={{
              background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer',
              fontSize: 12, padding: 0, textAlign: 'left',
            }}>← Назад к выбору</button>

            <div style={{ padding: '12px 14px', background: 'rgba(124,77,255,0.08)', border: '1px solid rgba(124,77,255,0.2)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.7 }}>
              <div style={{ fontWeight: 700, marginBottom: 6, fontSize: 13 }}>📍 Где найти строку в DevTools</div>
              <ol style={{ margin: '0 0 0 18px', padding: 0 }}>
                <li>Открой <code style={{ background: 'var(--bg-3)', padding: '1px 5px', borderRadius: 4 }}>web.telegram.org/k</code> и залогинься в Telegram</li>
                <li>Нажми <b>F12</b> (или Ctrl+Shift+I) — откроется DevTools</li>
                <li>Вкладка <b>Application</b> (Chrome/Edge) или <b>Storage</b> (Firefox)</li>
                <li>Слева: <b>Local Storage</b> → <code style={{ background: 'var(--bg-3)', padding: '1px 5px', borderRadius: 4 }}>https://web.telegram.org</code></li>
                <li>Найди ключи вида <code style={{ background: 'var(--bg-3)', padding: '1px 5px', borderRadius: 4 }}>account1</code>, <code style={{ background: 'var(--bg-3)', padding: '1px 5px', borderRadius: 4 }}>account2</code>... и значение из колонки <b>Value</b></li>
                <li>Скопируй <b>весь блок</b> либо несколько <code>accountN</code> вместе (JSON) и вставь ниже</li>
              </ol>
              <div style={{ marginTop: 8, fontSize: 11, color: 'var(--text-3)' }}>
                Можно вставить либо одну запись (<code>{`{"dcId":2,"dc2_auth_key":"..."}`}</code>), либо весь localStorage целиком —
                парсер найдёт все аккаунты автоматически.
              </div>
            </div>

            <WebSessionImport
              onSuccess={async () => { await load(); setImportModal(false); setImportType(null) }}
              onClose={() => { setImportType(null) }}
            />
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <button onClick={() => { setImportType(null); setImportResult(null); setImportFiles([]) }} style={{
              background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer',
              fontSize: 12, padding: 0, textAlign: 'left',
            }}>← Назад к выбору</button>

            {importType === 'tdata' && (
              <div style={{ padding: '10px 14px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 10, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.7 }}>
                Загрузите ZIP архив содержащий папку TData из Telegram Desktop.<br />
                Поддерживаются: opentele, telethon-tdata.
              </div>
            )}

            {/* File picker */}
            <div
              onClick={() => fileInputRef.current?.click()}
              style={{
                padding: '32px 20px', border: '2px dashed var(--border)', borderRadius: 14,
                textAlign: 'center', cursor: 'pointer', transition: 'border-color 0.2s',
              }}
              onMouseEnter={e => e.currentTarget.style.borderColor = 'var(--violet)'}
              onMouseLeave={e => e.currentTarget.style.borderColor = 'var(--border)'}
              onDragOver={e => { e.preventDefault(); e.currentTarget.style.borderColor = 'var(--violet)' }}
              onDragLeave={e => { e.currentTarget.style.borderColor = 'var(--border)' }}
              onDrop={e => {
                e.preventDefault()
                e.currentTarget.style.borderColor = 'var(--border)'
                const files = Array.from(e.dataTransfer.files)
                setImportFiles(files)
              }}
            >
              <div style={{ fontSize: 32, marginBottom: 8 }}>
                {importType === 'tdata' ? '📦' : '📄'}
              </div>
              <div style={{ fontSize: 13, fontWeight: 600 }}>
                {importFiles.length > 0
                  ? `Выбрано: ${importFiles.map(f => f.name).join(', ')}`
                  : 'Перетащите файл сюда или нажмите для выбора'
                }
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4 }}>
                {importType === 'tdata' ? 'ZIP архив с TData' : importType === 'session-batch' ? 'Несколько .session файлов' : '.session файл'}
              </div>
            </div>
            <input
              ref={fileInputRef}
              type="file"
              accept={importType === 'tdata' ? '.zip' : '.session'}
              multiple={importType === 'session-batch'}
              style={{ display: 'none' }}
              onChange={e => setImportFiles(Array.from(e.target.files))}
            />

            {importType === 'session' && (
              <Input label="Номер телефона (необязательно)" value={importPhone} onChange={e => setImportPhone(e.target.value)} placeholder="+380..." />
            )}

            {importType === 'session' && (
              <div>
                <label style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase', display: 'block', marginBottom: 6 }}>Прокси (рекомендуется)</label>
                <select value={importProxyId || ''} onChange={e => setImportProxyId(e.target.value ? parseInt(e.target.value) : null)}
                  onFocus={async () => { if (!importProxies.length) { try { const { data } = await proxiesAPI.list(); setImportProxies(data) } catch { } } }}
                  style={{ width: '100%', padding: '10px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text)', fontSize: 14, outline: 'none' }}>
                  <option value="">Без прокси</option>
                  {importProxies.map(p => <option key={p.id} value={p.id}>{proxyLabel(p, { showValid: false })}</option>)}
                </select>
              </div>
            )}

            {importResult && (
              <div style={{
                padding: '10px 14px', borderRadius: 10, fontSize: 13,
                background: importResult.success ? 'var(--green-dim)' : 'var(--red-dim)',
                color: importResult.success ? 'var(--green)' : 'var(--red)',
                border: `1px solid ${importResult.success ? 'rgba(61,214,140,0.2)' : 'rgba(248,81,73,0.2)'}`,
              }}>{importResult.message}</div>
            )}

            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <Button variant="ghost" onClick={() => setImportModal(false)}>Отмена</Button>
              <Button variant="primary" loading={importing} disabled={importFiles.length === 0} onClick={async () => {
                setImporting(true); setImportResult(null)
                try {
                  let res
                  if (importType === 'session') {
                    res = await importAPI.uploadSession(importFiles[0], importPhone)
                  } else if (importType === 'session-batch') {
                    res = await importAPI.uploadSessionsBatch(importFiles)
                  } else if (importType === 'tdata') {
                    // Шаг 1: Детектим аккаунты
                    const { data } = await accountsAPI.detectTData(importFiles[0])
                    setTdataSessionId(data.session_id)
                    setTdataDetected(data.accounts.map(a => ({ ...a, proxy_string: a.proxy_string || '', selected: true })))
                    setTdataStep('assign')
                    setImporting(false)
                    return // Не закрываем модал — показываем таблицу
                  }
                  setImportResult({ success: true, message: res.data.message || 'Импорт успешен!' })
                  await load()
                } catch (err) {
                  setImportResult({ success: false, message: err.response?.data?.detail || 'Ошибка импорта' })
                }
                setImporting(false)
              }}>
                {importing ? 'Импорт...' : 'Импортировать'}
              </Button>
            </div>
          </div>
        )}

        {/* TData Batch — таблица аккаунтов с прокси */}
        {tdataStep === 'assign' && tdataDetected.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <button onClick={() => { setTdataStep('upload'); setTdataDetected([]); setImportType(null) }} style={{
              background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer', fontSize: 12, padding: 0, textAlign: 'left',
            }}>← Назад</button>

            <div style={{ padding: '10px 14px', background: 'rgba(61,214,140,0.08)', border: '1px solid rgba(61,214,140,0.2)', borderRadius: 10, fontSize: 13, color: 'var(--green)' }}>
              Найдено {tdataDetected.length} аккаунтов. Отметьте нужные, назначьте прокси и нажмите "Импортировать".
            </div>

            {/* Чекбокс "Выбрать все" */}
            <label style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', padding: '4px 8px' }}>
              <input type="checkbox"
                checked={tdataDetected.every(a => a.selected)}
                onChange={e => {
                  const checked = e.target.checked
                  setTdataDetected(prev => prev.map(a => ({ ...a, selected: checked })))
                }}
                style={{ width: 16, height: 16, accentColor: 'var(--violet)' }}
              />
              <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-2)' }}>
                Выбрать все ({tdataDetected.filter(a => a.selected).length} из {tdataDetected.length})
              </span>
            </label>

            {/* Список аккаунтов с прокруткой */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxHeight: '45vh', overflowY: 'auto', paddingRight: 6 }}>
              {tdataDetected.map((acc, i) => (
                <div key={i} style={{
                  padding: '12px 14px', background: 'var(--bg-3)', border: '1px solid var(--border)',
                  borderRadius: 10, display: 'flex', alignItems: 'center', gap: 12,
                  opacity: acc.selected ? 1 : 0.5, transition: 'opacity 0.2s'
                }}>
                  <input type="checkbox"
                    checked={acc.selected}
                    onChange={e => setTdataDetected(prev => prev.map((a, j) => j === i ? { ...a, selected: e.target.checked } : a))}
                    style={{ width: 18, height: 18, accentColor: 'var(--violet)', cursor: 'pointer' }}
                  />
                  <div style={{ flex: '0 0 30px', fontSize: 18, textAlign: 'center' }}>👤</div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 600, fontSize: 13 }}>{acc.name || 'Без имени'}</div>
                    <div style={{ fontSize: 11, color: 'var(--text-3)' }}>{acc.phone || 'Номер не определён'} {acc.username ? `@${acc.username}` : ''}</div>
                  </div>
                  <div style={{ flex: '0 0 280px' }}>
                    <input
                      disabled={!acc.selected}
                      value={acc.proxy_string}
                      onChange={e => setTdataDetected(prev => prev.map((a, j) => j === i ? { ...a, proxy_string: e.target.value } : a))}
                      placeholder="ip:port:login:password"
                      style={{ width: '100%', padding: '8px 10px', background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 12, outline: 'none', fontFamily: 'monospace' }}
                    />
                  </div>
                </div>
              ))}
            </div>

            {/* Быстрое назначение одного прокси ВЫБРАННЫМ */}
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 6 }}>
              <input id="bulk-proxy" placeholder="Один прокси для выбранных (ip:port:log:pass)" style={{ flex: 1, padding: '8px 12px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 12, outline: 'none', fontFamily: 'monospace' }} />
              <Button variant="outline" size="sm" onClick={() => {
                const v = document.getElementById('bulk-proxy')?.value || ''
                if (v) setTdataDetected(prev => prev.map(a => a.selected ? { ...a, proxy_string: v } : a))
              }}>Применить к выбранным</Button>
            </div>

            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <select id="bulk-proxy-select"
                onFocus={async () => { if (!importProxies.length) { try { const { data } = await proxiesAPI.list(); setImportProxies(data) } catch { } } }}
                style={{ flex: 1, padding: '8px 12px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 8, color: 'var(--text)', fontSize: 12, outline: 'none' }}>
                <option value="">Выбрать из существующих прокси</option>
                {importProxies.map(p => <option key={p.id} value={`${p.host}:${p.port}:${p.login || ''}:${p.password || ''}`}>{proxyLabel(p, { showValid: false })}</option>)}
              </select>
              <Button variant="outline" size="sm" onClick={() => {
                const v = document.getElementById('bulk-proxy-select')?.value || ''
                if (v) setTdataDetected(prev => prev.map(a => a.selected ? { ...a, proxy_string: v } : a))
              }}>Применить</Button>
            </div>

            {importResult && (
              <div style={{
                padding: '10px 14px', borderRadius: 10, fontSize: 13,
                background: importResult.success ? 'var(--green-dim)' : 'var(--red-dim)',
                color: importResult.success ? 'var(--green)' : 'var(--red)',
                lineHeight: 1.6,
              }}>
                <div>{importResult.message}</div>
                {importResult.failed && importResult.failed.length > 0 && (
                  <div style={{ marginTop: 8, paddingTop: 8, borderTop: '1px solid rgba(255,255,255,0.08)' }}>
                    {importResult.failed.map((f, i) => (
                      <div key={i} style={{ fontSize: 12, marginTop: i ? 6 : 0 }}>
                        <b>акк #{f.index}:</b> {f.error}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 10 }}>
              <Button variant="ghost" onClick={() => { setImportModal(false); setTdataStep('upload'); setTdataDetected([]); setImportResult(null) }}>Закрыть</Button>
              <Button variant="primary" loading={importing}
                disabled={tdataDetected.filter(a => a.selected).length === 0}
                onClick={async () => {
                  const selectedAccounts = tdataDetected.filter(a => a.selected)
                  if (selectedAccounts.length === 0) return

                  setImporting(true); setImportResult(null)
                  try {
                    const accountsPayload = selectedAccounts.map(a => ({ index: a.index, proxy_string: a.proxy_string }))
                    const { data } = await accountsAPI.importTDataBatch(tdataSessionId, accountsPayload)

                    const failed = (data.results || []).filter(r => !r.success)
                    if (data.success > 0) {
                      setImportResult({
                        success: true,
                        message: `Импортировано ${data.success}/${selectedAccounts.length} аккаунтов`
                          + (failed.length ? `. С ошибками: ${failed.length}.` : ''),
                        failed,
                      })
                      await load()
                      if (failed.length === 0) {
                        setTimeout(() => {
                          setImportModal(false); setTdataStep('upload')
                          setTdataDetected([]); setImportResult(null)
                        }, 1500)
                      }
                    } else {
                      setImportResult({
                        success: false,
                        message: `Не импортирован ни один аккаунт (${selectedAccounts.length}). Причины:`,
                        failed,
                      })
                    }
                  } catch (err) {
                    setImportResult({ success: false, message: err.response?.data?.detail || 'Ошибка импорта', failed: [] })
                  }
                  setImporting(false)
                }}>
                📦 Импортировать {tdataDetected.filter(a => a.selected).length} акк.
              </Button>
            </div>
          </div>
        )}
      </Modal>

      {/* ══ Bulk export progress banner ══ */}
      {exportProgress && (() => {
        const pct = exportProgress.total > 0
          ? Math.min(100, Math.round((exportProgress.current / exportProgress.total) * 100))
          : 0
        const isDone = exportProgress.status === 'done'
        const isCancelled = exportProgress.status === 'cancelled' || exportProgress.status === 'cancelling'
        const isError = exportProgress.status === 'error'
        const colorTop = isDone ? 'var(--green)' : isError ? 'var(--red)' : isCancelled ? 'var(--yellow)' : 'var(--violet)'
        return (
          <div style={{
            position: 'fixed', bottom: 90, left: '50%', transform: 'translateX(-50%)',
            zIndex: 101, width: 'min(520px, 92vw)',
            background: 'var(--bg-2)', border: `1px solid ${colorTop}55`,
            borderRadius: 14, padding: '14px 18px',
            boxShadow: `0 10px 40px rgba(0,0,0,0.5), 0 0 24px ${colorTop}33`,
            display: 'flex', flexDirection: 'column', gap: 10,
          }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--text)', display: 'flex', alignItems: 'center', gap: 8 }}>
                {isDone ? '✅' : isError ? '❌' : isCancelled ? '⏹' : '📦'}
                <span>{isDone ? 'Экспорт TData завершён' : isError ? 'Ошибка экспорта' : isCancelled ? 'Экспорт отменён' : 'Экспорт TData'}</span>
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-3)', fontFamily: 'var(--font-mono)' }}>
                {exportProgress.current}/{exportProgress.total} · {pct}%
              </div>
            </div>

            {/* Progress bar */}
            <div style={{ height: 6, background: 'rgba(255,255,255,0.06)', borderRadius: 3, overflow: 'hidden' }}>
              <div style={{
                height: '100%', width: `${pct}%`,
                background: `linear-gradient(90deg, ${colorTop}, ${colorTop}99)`,
                transition: 'width 0.4s ease',
              }} />
            </div>

            {/* Stats row */}
            <div style={{ display: 'flex', gap: 14, fontSize: 11, color: 'var(--text-2)', flexWrap: 'wrap' }}>
              <span style={{ color: 'var(--green)' }}>✅ {exportProgress.ok || 0} OK</span>
              {exportProgress.failed > 0 && <span style={{ color: 'var(--red)' }}>❌ {exportProgress.failed} fail</span>}
              {exportProgress.current_phone && exportProgress.status === 'running' && (
                <span style={{ color: 'var(--text-3)', fontFamily: 'var(--font-mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
                  📞 {exportProgress.current_phone}
                </span>
              )}
            </div>

            {exportProgress.message && (
              <div style={{ fontSize: 11, color: 'var(--text-3)' }}>{exportProgress.message}</div>
            )}

            {bulkExporting && exportJobId && (
              <Button variant="danger" size="sm" onClick={handleCancelExport}
                disabled={exportProgress.status === 'cancelling'}>
                {exportProgress.status === 'cancelling' ? '⏳ Останавливаю...' : '⏹ Отменить экспорт'}
              </Button>
            )}
          </div>
        )
      })()}

      {/* ══ Floating action bar (multi-select) ══ */}
      {selectedIds.size > 0 && (
        <div style={{
          position: 'fixed', bottom: 24, left: '50%', transform: 'translateX(-50%)',
          zIndex: 100,
          background: 'var(--bg-2)', border: '1px solid rgba(124,77,255,0.4)',
          borderRadius: 14, padding: '12px 18px',
          boxShadow: '0 10px 40px rgba(0,0,0,0.5), 0 0 20px rgba(124,77,255,0.15)',
          display: 'flex', alignItems: 'center', gap: 14,
        }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)' }}>
            Выбрано: <span style={{ color: 'var(--violet)' }}>{selectedIds.size}</span>
          </div>
          <div style={{ width: 1, height: 22, background: 'var(--border)' }} />
          <Button variant="primary" size="sm" onClick={openBulkMedia}>
            📸 Загрузить фото
          </Button>
          <Button variant="ghost" size="sm" onClick={handleBulkClearMedia} title="Удалить все фото у выбранных">
            🗑 Очистить фото
          </Button>
          <Button variant="primary" size="sm" onClick={openPrepModal}
            title="Смена аватарки + имени/фамилии, разнесённо во времени (без флудвейтов)">
            ⚙ Подготовка
          </Button>
          <Button variant="outline" size="sm" loading={bulkExporting}
            onClick={handleBulkExportTData}
            title="Экспортировать TData всех выбранных аккаунтов в один ZIP-архив">
            📦 Экспорт TData
          </Button>
          <Button variant="outline" size="sm" loading={checking}
            onClick={handleCheckSelected}
            title="Подключиться к каждому через прокси и обновить статус (active/frozen/spamblock)">
            ✅ Проверить
          </Button>
          <Button variant="ghost" size="sm" onClick={clearSelection}>Снять выбор</Button>
        </div>
      )}

      {/* ══ Check-accounts: модал с детальными результатами ══ */}
      <Modal
        open={!!checkResults}
        onClose={() => setCheckResults(null)}
        title="🔍 Результаты проверки аккаунтов"
        width={680}
      >
        {checkResults && (() => {
          const STATUS_META = {
            active:    { emoji: '✅', label: 'Живой',     color: 'var(--green)' },
            spamblock: { emoji: '⚠️', label: 'Спамблок',  color: '#f59e0b' },
            frozen:    { emoji: '❌', label: 'Заморожен', color: 'var(--red)' },
            error:     { emoji: '💥', label: 'Ошибка',    color: 'var(--red)' },
            unknown:   { emoji: '❓', label: 'Неизвестно', color: 'var(--text-3)' },
          }
          const rows = [...checkResults.results].sort((a, b) => {
            // Сначала проблемные (frozen/error/spam), потом active
            const order = { frozen: 0, error: 1, spamblock: 2, unknown: 3, active: 4 }
            return (order[a.status] ?? 5) - (order[b.status] ?? 5)
          })
          return (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {/* Сводка */}
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8 }}>
                <div style={{ padding: 10, borderRadius: 8, background: 'rgba(34,197,94,0.12)', textAlign: 'center' }}>
                  <div style={{ fontSize: 11, color: 'var(--text-3)' }}>✅ Живых</div>
                  <div style={{ fontSize: 20, fontWeight: 700, color: 'var(--green)' }}>{checkResults.active || 0}</div>
                </div>
                <div style={{ padding: 10, borderRadius: 8, background: 'rgba(245,158,11,0.12)', textAlign: 'center' }}>
                  <div style={{ fontSize: 11, color: 'var(--text-3)' }}>⚠️ Спам</div>
                  <div style={{ fontSize: 20, fontWeight: 700, color: '#f59e0b' }}>{checkResults.spam || 0}</div>
                </div>
                <div style={{ padding: 10, borderRadius: 8, background: 'rgba(239,68,68,0.12)', textAlign: 'center' }}>
                  <div style={{ fontSize: 11, color: 'var(--text-3)' }}>❌ Заморожен</div>
                  <div style={{ fontSize: 20, fontWeight: 700, color: 'var(--red)' }}>{checkResults.frozen || 0}</div>
                </div>
                <div style={{ padding: 10, borderRadius: 8, background: 'rgba(168,85,247,0.12)', textAlign: 'center' }}>
                  <div style={{ fontSize: 11, color: 'var(--text-3)' }}>💥 Ошибок</div>
                  <div style={{ fontSize: 20, fontWeight: 700, color: 'var(--violet)' }}>{checkResults.errors || 0}</div>
                </div>
              </div>

              {/* Таблица по каждому акку */}
              <div style={{ maxHeight: 380, overflowY: 'auto', border: '1px solid var(--border)', borderRadius: 10 }}>
                {rows.map((r, i) => {
                  const meta = STATUS_META[r.status] || STATUS_META.unknown
                  return (
                    <div key={i} style={{
                      display: 'grid',
                      gridTemplateColumns: '24px 1fr 120px 1fr',
                      gap: 10, padding: '10px 14px', alignItems: 'center',
                      borderBottom: i < rows.length - 1 ? '1px solid var(--border)' : 'none',
                      background: r.status === 'frozen' || r.status === 'error' ? 'rgba(239,68,68,0.04)' : 'transparent',
                    }}>
                      <span style={{ fontSize: 16 }}>{meta.emoji}</span>
                      <span style={{ fontSize: 13, fontWeight: 600 }}>{r.phone}</span>
                      <span style={{ fontSize: 12, fontWeight: 600, color: meta.color }}>{meta.label}</span>
                      <span style={{ fontSize: 11, color: 'var(--text-3)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {r.error || (r.trust_score != null ? `trust: ${r.trust_score}` : '')}
                      </span>
                    </div>
                  )
                })}
              </div>

              <div style={{ fontSize: 11, color: 'var(--text-3)', textAlign: 'center' }}>
                Подробнее в logs/celery_worker.console.log
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                <Button variant="ghost" onClick={() => setCheckResults(null)}>Закрыть</Button>
              </div>
            </div>
          )
        })()}
      </Modal>

      {/* ══ Check-accounts progress banner ══ */}
      {checkProgress && (() => {
        const pct = Math.min(100, Math.round((checkProgress.current / Math.max(1, checkProgress.total)) * 100))
        const isDone = checkProgress.status === 'done'
        const isError = checkProgress.status === 'error'
        const color = isDone ? 'var(--green)' : isError ? 'var(--red)' : 'var(--violet)'
        return (
          <div style={{
            position: 'fixed', bottom: 90, left: '50%', transform: 'translateX(-50%)',
            zIndex: 99, background: 'var(--bg-2)',
            border: `2px solid ${color}`, borderRadius: 14, padding: '14px 22px',
            minWidth: 360, maxWidth: 640,
            boxShadow: '0 10px 40px rgba(0,0,0,0.5)',
          }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
              <div style={{ fontWeight: 700, color, fontSize: 13 }}>
                {isDone ? '✅' : isError ? '❌' : '🔍'} Проверка аккаунтов
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-2)' }}>
                {checkProgress.current} / {checkProgress.total} · {pct}%
              </div>
            </div>
            <div style={{ height: 6, borderRadius: 3, background: 'var(--bg-3)', overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${pct}%`, background: color, transition: 'width 0.4s ease' }} />
            </div>
            {checkProgress.message && (
              <div style={{ marginTop: 6, fontSize: 11, color: 'var(--text-3)' }}>
                {checkProgress.message}
              </div>
            )}
          </div>
        )
      })()}

      {/* ══ Bulk media upload modal ══ */}
      <Modal open={bulkMediaModal} onClose={() => setBulkMediaModal(false)}
        title={`📸 Фото для ${selectedIds.size} аккаунтов`} width={500}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ padding: '10px 12px', background: 'rgba(124,77,255,0.06)', border: '1px solid rgba(124,77,255,0.15)', borderRadius: 8, fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>
            Файлы складываются в локальные папки аккаунтов — <b>Telegram не дёргается</b>.
            Сами аватарки/сториз ставятся прогревом из этой папки (там уже есть IP-throttle и stagger).
          </div>

          <div onClick={() => bulkFileInputRef.current?.click()}
            style={{
              minHeight: 100, border: '2px dashed var(--border)', borderRadius: 10,
              display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
              cursor: 'pointer', padding: 14, gap: 8, transition: 'border-color 0.15s',
            }}
            onMouseEnter={e => e.currentTarget.style.borderColor = 'var(--violet)'}
            onMouseLeave={e => e.currentTarget.style.borderColor = 'var(--border)'}>
            {bulkMediaFiles.length === 0 ? (
              <>
                <span style={{ fontSize: 24 }}>📸</span>
                <span style={{ fontSize: 12, color: 'var(--text-3)' }}>Выбрать фото (можно несколько)</span>
              </>
            ) : (
              <>
                <span style={{ fontSize: 12, color: 'var(--green)', fontWeight: 600 }}>
                  ✓ Выбрано {bulkMediaFiles.length} файл(ов)
                </span>
                <span style={{ fontSize: 11, color: 'var(--text-3)' }}>нажми чтобы выбрать другие</span>
              </>
            )}
          </div>
          <input ref={bulkFileInputRef} type="file" accept="image/*" multiple
            style={{ display: 'none' }}
            onChange={e => setBulkMediaFiles(Array.from(e.target.files || []))} />

          {bulkMediaFiles.length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, maxHeight: 200, overflowY: 'auto', padding: '6px 0' }}>
              {bulkMediaFiles.map((f, i) => (
                <div key={i} style={{
                  fontSize: 11, padding: '4px 10px', background: 'var(--bg-3)',
                  borderRadius: 6, border: '1px solid var(--border)', color: 'var(--text-2)',
                  display: 'flex', alignItems: 'center', gap: 6,
                }}>
                  <span style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{f.name}</span>
                  <button onClick={() => setBulkMediaFiles(prev => prev.filter((_, j) => j !== i))}
                    style={{ background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer', fontSize: 14, padding: 0 }}>×</button>
                </div>
              ))}
            </div>
          )}

          {/* Режим распределения */}
          {bulkMediaFiles.length > 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <div style={{ fontSize: 11, color: 'var(--text-3)', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
                Как распределить
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                {[
                  { key: 'random', label: '🎲 Случайно', desc: 'каждому акк — свой рандомный набор (рекомендуется)' },
                  { key: 'all',    label: '📋 Всем одинаково', desc: 'каждый акк получит все фото' },
                ].map(o => {
                  const on = bulkDist === o.key
                  return (
                    <button key={o.key} onClick={() => setBulkDist(o.key)} style={{
                      flex: 1, padding: '8px 10px', borderRadius: 8, cursor: 'pointer', textAlign: 'left',
                      background: on ? 'rgba(124,77,255,0.15)' : 'var(--bg-3)',
                      border: `1px solid ${on ? 'rgba(124,77,255,0.4)' : 'var(--border)'}`,
                      color: on ? 'var(--text)' : 'var(--text-2)',
                    }}>
                      <div style={{ fontSize: 12, fontWeight: 600 }}>{o.label}</div>
                      <div style={{ fontSize: 10, opacity: 0.75, marginTop: 2 }}>{o.desc}</div>
                    </button>
                  )
                })}
              </div>

              {bulkDist === 'random' && (
                <div style={{ display: 'flex', gap: 10, alignItems: 'flex-end' }}>
                  <div style={{ flex: 1 }}>
                    <label style={{ fontSize: 11, color: 'var(--text-3)', display: 'block', marginBottom: 4 }}>Мин. фото/акк</label>
                    <input type="number" value={bulkRandomMin}
                      min={1} max={bulkMediaFiles.length}
                      onChange={e => setBulkRandomMin(Math.max(1, Math.min(parseInt(e.target.value || '1'), bulkMediaFiles.length)))}
                      style={{ width: '100%', padding: '6px 10px', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg-3)', color: 'var(--text)', fontSize: 12 }} />
                  </div>
                  <div style={{ flex: 1 }}>
                    <label style={{ fontSize: 11, color: 'var(--text-3)', display: 'block', marginBottom: 4 }}>Макс. фото/акк</label>
                    <input type="number" value={bulkRandomMax}
                      min={bulkRandomMin} max={bulkMediaFiles.length}
                      onChange={e => setBulkRandomMax(Math.max(bulkRandomMin, Math.min(parseInt(e.target.value || '1'), bulkMediaFiles.length)))}
                      style={{ width: '100%', padding: '6px 10px', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg-3)', color: 'var(--text)', fontSize: 12 }} />
                  </div>
                </div>
              )}

              <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
                {bulkDist === 'all'
                  ? `Каждый акк получит ${bulkMediaFiles.length} фото. Итого ${bulkMediaFiles.length * selectedIds.size} файлов.`
                  : `Каждый акк получит случайно ${bulkRandomMin}-${bulkRandomMax} фото из ${bulkMediaFiles.length}. Ожидаемо ~${Math.round((bulkRandomMin + bulkRandomMax) / 2 * selectedIds.size)} файлов.`}
              </div>
            </div>
          )}

          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <Button variant="ghost" onClick={() => setBulkMediaModal(false)}>Отмена</Button>
            <Button variant="primary" loading={bulkUploading}
              disabled={bulkMediaFiles.length === 0 || selectedIds.size === 0}
              onClick={handleBulkUpload}>
              📤 Распределить
            </Button>
          </div>
        </div>
      </Modal>

      {/* ══ Floating badge для активного prep job'а ══ */}
      {prepJobId && prepProgress && prepProgress.status !== 'done' && prepProgress.status !== 'error' && !prepModal && (
        <div style={{
          position: 'fixed', top: 20, right: 20, zIndex: 90,
          background: 'var(--bg-2)', border: '1px solid rgba(139,92,246,0.4)',
          borderRadius: 10, padding: '10px 14px',
          boxShadow: '0 8px 30px rgba(0,0,0,0.4)',
          fontSize: 12, color: 'var(--text)',
          minWidth: 240, maxWidth: 340,
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10, marginBottom: 6 }}>
            <span style={{ fontWeight: 700 }}>
              {prepProgress._mode === 'plan' ? '🗓 План' : '⚙ Подготовка'} идёт
            </span>
            <button onClick={() => setPrepModal(true)}
              style={{
                background: 'rgba(139,92,246,0.15)', border: '1px solid var(--violet)',
                color: 'var(--text)', padding: '3px 10px', borderRadius: 4,
                cursor: 'pointer', fontSize: 11,
              }}>Открыть</button>
          </div>
          {prepProgress._mode === 'plan' ? (
            <>
              <div style={{ fontSize: 11, color: 'var(--text-2)' }}>
                {prepProgress.phase === 'generating' && '🧠 Генерация…'}
                {prepProgress.phase === 'scheduled' && '🗓 Запланировано, жду'}
                {prepProgress.phase === 'running' && '🔄 Сессии идут'}
              </div>
              {prepProgress.totals && (
                <div style={{ marginTop: 4, fontSize: 11 }}>
                  <span style={{ color: 'var(--green)' }}>{prepProgress.totals.sessions_done}</span>
                  {' / '}{prepProgress.totals.sessions_total}
                  {' сессий '}
                  <span style={{ color: 'var(--text-3)' }}>({prepProgress.totals.accounts_total} акк)</span>
                </div>
              )}
              <div style={{ height: 4, background: 'var(--bg-3)', borderRadius: 2, overflow: 'hidden', marginTop: 6 }}>
                <div style={{
                  height: '100%',
                  width: `${prepProgress.totals ? Math.min(100, ((prepProgress.totals.sessions_done || 0) / Math.max(1, prepProgress.totals.sessions_total || 1)) * 100) : 0}%`,
                  background: 'var(--violet)', transition: 'width 0.5s',
                }} />
              </div>
            </>
          ) : (
            <>
              <div style={{ fontSize: 11, color: 'var(--text-2)' }}>
                {prepProgress.done || 0} / {prepProgress.total || 0} акк
                {prepProgress.sleeping_sec > 0 && ` · 💤 ${prepProgress.sleeping_sec}с`}
              </div>
              <div style={{ height: 4, background: 'var(--bg-3)', borderRadius: 2, overflow: 'hidden', marginTop: 6 }}>
                <div style={{
                  height: '100%',
                  width: `${Math.min(100, ((prepProgress.done || 0) / Math.max(1, prepProgress.total || 1)) * 100)}%`,
                  background: 'var(--violet)', transition: 'width 0.5s',
                }} />
              </div>
            </>
          )}
        </div>
      )}

      {/* ══ Prepare (аватарки + имена, разнесённо) ══ */}
      <Modal open={prepModal} onClose={() => setPrepModal(false)}
        title={`⚙ Подготовка · ${selectedIds.size} аккаунт(ов)`} width={560}>
        {!prepJobId ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            {/* Список активных plan-job'ов из Redis */}
            {prepPlanList.length > 0 && (
              <div style={{
                padding: 10, borderRadius: 8, background: 'var(--bg-2)',
                border: '1px solid var(--border)',
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                  <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--text)' }}>
                    🗓 Существующие план-job'ы ({prepPlanList.length})
                  </div>
                  <button onClick={loadPrepPlanList}
                    disabled={prepPlanListLoading}
                    style={{ background: 'none', border: 'none', color: 'var(--text-3)', cursor: 'pointer', fontSize: 11 }}>
                    {prepPlanListLoading ? '…' : '↻'}
                  </button>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 200, overflowY: 'auto' }}>
                  {prepPlanList.map(j => {
                    const when = j.start_ts ? new Date(j.start_ts * 1000).toLocaleString('ru-RU', {
                      day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit',
                    }) : '?'
                    const undone = (j.sessions_skipped || 0) + (j.sessions_failed || 0)
                    const phaseIcon = j.phase === 'done' ? '✅' :
                                      j.phase === 'running' ? '🔄' :
                                      j.phase === 'scheduled' ? '🗓' :
                                      j.phase === 'generating' ? '🧠' :
                                      j.phase === 'cancelling' ? '🛑' :
                                      j.phase === 'cancelled' ? '⛔' : '❓'
                    return (
                      <div key={j.job_id} onClick={() => {
                        setPrepJobId(j.job_id)
                        setPrepMode2('plan')
                        startPrepPolling(j.job_id, 'plan')
                      }} style={{
                        padding: '6px 8px', borderRadius: 5, cursor: 'pointer',
                        background: 'var(--bg-3)', border: '1px solid transparent',
                        display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8,
                        fontSize: 11,
                      }}
                        onMouseEnter={e => e.currentTarget.style.borderColor = 'var(--violet)'}
                        onMouseLeave={e => e.currentTarget.style.borderColor = 'transparent'}>
                        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                          <span>{phaseIcon}</span>
                          <code style={{ fontSize: 10, color: 'var(--text-3)' }}>{j.job_id.slice(0, 8)}</code>
                          <span style={{ color: 'var(--text-2)' }}>{j.total_accounts} акк</span>
                          {j.resumed_from && (
                            <span style={{ fontSize: 9, color: 'var(--text-3)' }}>
                              ↩ {j.resumed_from.slice(0, 6)}
                            </span>
                          )}
                        </div>
                        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                          <span style={{ color: 'var(--green)' }}>{j.sessions_done}</span>
                          <span style={{ color: 'var(--text-3)' }}>/</span>
                          <span style={{ color: 'var(--text-3)' }}>{j.sessions_total}</span>
                          {undone > 0 && (
                            <span style={{ color: 'var(--red)' }}>({undone} недоделано)</span>
                          )}
                          <span style={{ color: 'var(--text-3)', marginLeft: 4 }}>{when}</span>
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}
            <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.5 }}>
              Разнесёт операции во времени с рандомными задержками (обычно 60-180 сек между акками).
              Один прокси = одно подключение за раз. Порядок акков перемешан, ресурсы (файлы + имена)
              распределяются случайно.
            </div>

            {/* Чек-бокс 1: аватарки */}
            <div style={{
              padding: 12, borderRadius: 8,
              background: prepChangeAvatars ? 'rgba(139,92,246,0.08)' : 'var(--bg-2)',
              border: `1px solid ${prepChangeAvatars ? 'var(--violet)' : 'var(--border)'}`,
            }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: 13, fontWeight: 600 }}>
                <input type="checkbox" checked={prepChangeAvatars}
                  onChange={e => setPrepChangeAvatars(e.target.checked)} />
                📸 Поменять аватарки
              </label>
              {prepChangeAvatars && (
                <div style={{ marginTop: 10 }}>
                  <div
                    onClick={() => prepFileInputRef.current?.click()}
                    onDragOver={e => { e.preventDefault(); e.stopPropagation(); setPrepDragOver(true) }}
                    onDragEnter={e => { e.preventDefault(); e.stopPropagation(); setPrepDragOver(true) }}
                    onDragLeave={e => { e.preventDefault(); e.stopPropagation(); setPrepDragOver(false) }}
                    onDrop={e => {
                      e.preventDefault(); e.stopPropagation(); setPrepDragOver(false)
                      addPrepFiles(e.dataTransfer.files)
                    }}
                    style={{
                      padding: 18, textAlign: 'center', cursor: 'pointer',
                      border: `2px dashed ${prepDragOver ? 'var(--violet)' : 'var(--border)'}`,
                      borderRadius: 8,
                      background: prepDragOver ? 'rgba(139,92,246,0.10)' : 'var(--bg-3)',
                      fontSize: 12, color: 'var(--text-2)',
                      transition: 'all 0.15s ease',
                    }}>
                    {prepAvatarFiles.length === 0 ? (
                      <>
                        <div style={{ fontSize: 24, marginBottom: 6 }}>
                          {prepDragOver ? '📥' : '🖼️'}
                        </div>
                        <div style={{ fontWeight: 600, color: 'var(--text)', marginBottom: 3 }}>
                          {prepDragOver ? 'Отпусти чтобы добавить' : 'Перетащи фото сюда'}
                        </div>
                        <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
                          или кликни чтобы выбрать · любое количество (1 или несколько, из папки не нужно)
                        </div>
                      </>
                    ) : (
                      <>
                        <div style={{ fontWeight: 600, color: 'var(--text)' }}>
                          ✓ {prepAvatarFiles.length} файл(ов) выбрано
                        </div>
                        <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 3 }}>
                          {prepDragOver
                            ? 'Отпусти чтобы дописать ещё'
                            : 'Кликни или перетащи ещё чтобы дополнить · распределятся рандомно'}
                        </div>
                      </>
                    )}
                  </div>
                  <input ref={prepFileInputRef} type="file" accept="image/*" multiple
                    style={{ display: 'none' }}
                    onChange={e => {
                      addPrepFiles(e.target.files)
                      // Сбрасываем value чтобы повторный выбор того же файла триггерил change
                      e.target.value = ''
                    }} />
                  {prepAvatarFiles.length > 0 && (
                    <div style={{
                      marginTop: 8, maxHeight: 140, overflowY: 'auto',
                      background: 'var(--bg-3)', borderRadius: 6, padding: 4,
                    }}>
                      {prepAvatarFiles.map((f, i) => (
                        <div key={`${f.name}-${f.size}-${i}`} style={{
                          display: 'flex', alignItems: 'center', gap: 8,
                          padding: '4px 8px', fontSize: 11,
                        }}>
                          <span style={{ color: 'var(--text-3)' }}>🖼</span>
                          <span style={{ color: 'var(--text-2)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {f.name}
                          </span>
                          <span style={{ color: 'var(--text-3)', fontSize: 10 }}>
                            {(f.size / 1024).toFixed(0)} KB
                          </span>
                          <button onClick={(ev) => { ev.stopPropagation(); removePrepFile(i) }}
                            style={{
                              background: 'transparent', border: 'none', cursor: 'pointer',
                              color: 'var(--text-3)', fontSize: 14, padding: '0 4px',
                            }}
                            title="Убрать">×</button>
                        </div>
                      ))}
                    </div>
                  )}
                  {prepAvatarFiles.length > 0 && (
                    <div style={{ marginTop: 6, display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
                      <button onClick={() => setPrepAvatarFiles([])}
                        style={{
                          background: 'none', border: 'none', color: 'var(--text-3)',
                          cursor: 'pointer', fontSize: 11,
                        }}>
                        ✕ Очистить всё
                      </button>
                      {prepAvatarFiles.length < selectedIds.size && (
                        <span style={{ fontSize: 11, color: 'var(--text-3)' }}>
                          💡 Файлов ({prepAvatarFiles.length}) меньше чем акков ({selectedIds.size}) —
                          некоторые файлы повторятся.
                        </span>
                      )}
                    </div>
                  )}
                  {/* Удалить предыдущие фото профиля перед загрузкой новой */}
                  <label style={{
                    marginTop: 10, padding: '8px 10px', borderRadius: 6,
                    background: prepDeletePrevProfileAvatar ? 'rgba(239,68,68,0.08)' : 'var(--bg-3)',
                    border: `1px solid ${prepDeletePrevProfileAvatar ? 'rgba(239,68,68,0.35)' : 'var(--border)'}`,
                    display: 'flex', alignItems: 'flex-start', gap: 8, cursor: 'pointer',
                  }}>
                    <input type="checkbox"
                      checked={prepDeletePrevProfileAvatar}
                      onChange={e => setPrepDeletePrevProfileAvatar(e.target.checked)}
                      style={{ marginTop: 2 }} />
                    <div>
                      <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)' }}>
                        🗑 Удалить старые фото профиля
                      </div>
                      <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 2, lineHeight: 1.4 }}>
                        Перед uploadом новой аватарки удалит все существующие фото профиля.
                        Оба действия — в одной сессии, пауза между ними 2-5 сек.
                      </div>
                    </div>
                  </label>
                </div>
              )}
            </div>

            {/* Чек-бокс 2: имена */}
            <div style={{
              padding: 12, borderRadius: 8,
              background: prepChangeNames ? 'rgba(139,92,246,0.08)' : 'var(--bg-2)',
              border: `1px solid ${prepChangeNames ? 'var(--violet)' : 'var(--border)'}`,
            }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: 13, fontWeight: 600 }}>
                <input type="checkbox" checked={prepChangeNames}
                  onChange={e => setPrepChangeNames(e.target.checked)} />
                📝 Поменять имя и фамилию
              </label>
              {prepChangeNames && (
                <div style={{ marginTop: 10 }}>
                  <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 6 }}>
                    По одному имени в строке. Формат: <code>Имя Фамилия</code> (фамилия опциональна).
                    Каждому акку случайное из списка.
                  </div>
                  <textarea value={prepNamesText}
                    onChange={e => setPrepNamesText(e.target.value)}
                    placeholder={`Иван Петров\nАнна Соколова\nМария\nДмитрий Кузнецов`}
                    style={{
                      width: '100%', minHeight: 120, padding: 10,
                      background: 'var(--bg-3)', border: '1px solid var(--border)',
                      borderRadius: 6, color: 'var(--text)', fontSize: 12,
                      fontFamily: 'monospace', resize: 'vertical',
                    }} />
                  {prepNamesText.trim() && (
                    <div style={{ marginTop: 4, fontSize: 11, color: 'var(--text-3)' }}>
                      Строк: {prepNamesText.split('\n').filter(s => s.trim()).length}
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* Чек-бокс 3: создать канал */}
            <div style={{
              padding: 12, borderRadius: 8,
              background: prepCreateChannels ? 'rgba(139,92,246,0.08)' : 'var(--bg-2)',
              border: `1px solid ${prepCreateChannels ? 'var(--violet)' : 'var(--border)'}`,
            }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: 13, fontWeight: 600 }}>
                <input type="checkbox" checked={prepCreateChannels}
                  onChange={e => setPrepCreateChannels(e.target.checked)} />
                📢 Создать канал у каждого акка
              </label>
              {prepCreateChannels && (
                <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 10 }}>
                  <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
                    Создание идёт поэтапно: <b>создать → username → аватар → закрепить → disconnect → sleep 2с → reconnect → первый пост</b>.
                    Каждому акку случайное название и (если есть) случайный аватар канала.
                  </div>

                  {/* Названия канала */}
                  <div>
                    <div style={{ fontSize: 11, color: 'var(--text-2)', marginBottom: 4, fontWeight: 600 }}>
                      Названия каналов (одно на строку) *
                    </div>
                    <textarea value={prepChTitles}
                      onChange={e => setPrepChTitles(e.target.value)}
                      placeholder={`Крипто новости\nБизнес и финансы\nЛайфхаки дня\nНовости IT`}
                      style={{
                        width: '100%', minHeight: 80, padding: 8,
                        background: 'var(--bg-3)', border: '1px solid var(--border)',
                        borderRadius: 6, color: 'var(--text)', fontSize: 12,
                        fontFamily: 'monospace', resize: 'vertical',
                      }} />
                    {prepChTitles.trim() && (
                      <div style={{ marginTop: 2, fontSize: 10, color: 'var(--text-3)' }}>
                        Строк: {prepChTitles.split('\n').filter(s => s.trim()).length}
                      </div>
                    )}
                  </div>

                  {/* Описания — многострочные, рандомизируются */}
                  <div>
                    <div style={{ fontSize: 11, color: 'var(--text-2)', marginBottom: 4, fontWeight: 600 }}>
                      Описания канала (одно на строку — каждому акку случайное; опционально)
                    </div>
                    <textarea value={prepChDescriptions}
                      onChange={e => setPrepChDescriptions(e.target.value)}
                      placeholder={`Канал автора о жизни и работе\nПутевые заметки и мысли\nЛайфстайл + полезное\nПерсональный дневник`}
                      style={{
                        width: '100%', minHeight: 70, padding: 8,
                        background: 'var(--bg-3)', border: '1px solid var(--border)',
                        borderRadius: 6, color: 'var(--text)', fontSize: 12,
                        fontFamily: 'monospace', resize: 'vertical',
                      }} />
                    {prepChDescriptions.trim() && (
                      <div style={{ marginTop: 2, fontSize: 10, color: 'var(--text-3)' }}>
                        Вариантов: {prepChDescriptions.split('\n').filter(s => s.trim()).length}
                      </div>
                    )}
                  </div>

                  {/* Первый пост — текст */}
                  <div>
                    <div style={{ fontSize: 11, color: 'var(--text-2)', marginBottom: 4, fontWeight: 600 }}>
                      Первый пост — текст (общий, опционально — публикуется после reconnect)
                    </div>
                    <textarea value={prepChFirstPost}
                      onChange={e => setPrepChFirstPost(e.target.value)}
                      maxLength={4096}
                      placeholder="Например: Всем привет 👋 Это мой канал"
                      style={{
                        width: '100%', minHeight: 50, padding: 8,
                        background: 'var(--bg-3)', border: '1px solid var(--border)',
                        borderRadius: 6, color: 'var(--text)', fontSize: 12, resize: 'vertical',
                      }} />
                  </div>

                  {/* Первый пост — фото или видео (drag&drop) */}
                  <div>
                    <div style={{ fontSize: 11, color: 'var(--text-2)', marginBottom: 4, fontWeight: 600 }}>
                      Первый пост — фото/видео (опционально; распределяется рандомно; текст выше идёт как подпись).
                      <span style={{ fontWeight: 400, color: 'var(--text-3)' }}>
                        {' '}Поддержка: jpg/png/webp/gif · mp4/mov/webm/mkv/avi/m4v.
                      </span>
                    </div>
                    <div
                      onClick={() => prepChPostFileInputRef.current?.click()}
                      onDragOver={e => { e.preventDefault(); e.stopPropagation(); setPrepChPostDragOver(true) }}
                      onDragEnter={e => { e.preventDefault(); e.stopPropagation(); setPrepChPostDragOver(true) }}
                      onDragLeave={e => { e.preventDefault(); e.stopPropagation(); setPrepChPostDragOver(false) }}
                      onDrop={e => {
                        e.preventDefault(); e.stopPropagation(); setPrepChPostDragOver(false)
                        addPrepChPostFiles(e.dataTransfer.files)
                      }}
                      style={{
                        padding: 12, textAlign: 'center', cursor: 'pointer',
                        border: `2px dashed ${prepChPostDragOver ? 'var(--violet)' : 'var(--border)'}`,
                        borderRadius: 6,
                        background: prepChPostDragOver ? 'rgba(139,92,246,0.10)' : 'var(--bg-3)',
                        fontSize: 11, color: 'var(--text-2)',
                        transition: 'all 0.15s ease',
                      }}>
                      {prepChPostPhotoFiles.length === 0
                        ? (prepChPostDragOver ? '📥 Отпусти чтобы добавить' : '🖼️/🎬 Перетащи или кликни · фото или видео · распределится рандомно')
                        : `✓ ${prepChPostPhotoFiles.length} файл(ов) — кликни/перетащи чтобы дополнить`}
                    </div>
                    {/* accept:
                        - image/*, video/*  — стандартные MIME
                        - .mov               — Safari/старые macOS иногда не выставляют video/quicktime,
                                               нужно явное расширение чтобы диалог показал файл
                        - .mkv/.avi/.m4v     — аналогично, часто без MIME */}
                    <input ref={prepChPostFileInputRef} type="file"
                      accept="image/*,video/*,.mov,.mkv,.avi,.m4v"
                      multiple
                      style={{ display: 'none' }}
                      onChange={e => { addPrepChPostFiles(e.target.files); e.target.value = '' }} />
                    {prepChPostPhotoFiles.length > 0 && (
                      <div style={{
                        marginTop: 6, maxHeight: 100, overflowY: 'auto',
                        background: 'var(--bg-3)', borderRadius: 4, padding: 4,
                      }}>
                        {prepChPostPhotoFiles.map((f, i) => {
                          const nameLower = (f.name || '').toLowerCase()
                          const isVideo = (f.type || '').startsWith('video/') ||
                            VIDEO_EXTS.some(ext => nameLower.endsWith(ext))
                          const sizeMb = f.size / (1024 * 1024)
                          return (
                            <div key={`${f.name}-${f.size}-${i}`} style={{
                              display: 'flex', alignItems: 'center', gap: 6,
                              padding: '3px 6px', fontSize: 10,
                            }}>
                              <span style={{ color: 'var(--text-3)' }}>{isVideo ? '🎬' : '🖼'}</span>
                              <span style={{ color: 'var(--text-2)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                {f.name}
                              </span>
                              <span style={{ color: 'var(--text-3)', fontSize: 9 }}>
                                {sizeMb >= 1 ? `${sizeMb.toFixed(1)} MB` : `${(f.size / 1024).toFixed(0)} KB`}
                              </span>
                              <button onClick={(ev) => { ev.stopPropagation(); removePrepChPostFile(i) }}
                                style={{ background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--text-3)', fontSize: 12 }}>×</button>
                            </div>
                          )
                        })}
                      </div>
                    )}
                  </div>

                  {/* ── Ручное распределение постов по акк-id ── */}
                  <div style={{
                    padding: 10, borderRadius: 8,
                    background: prepChCustomPostsEnabled ? 'rgba(139,92,246,0.06)' : 'var(--bg-3)',
                    border: `1px solid ${prepChCustomPostsEnabled ? 'var(--violet)' : 'var(--border)'}`,
                  }}>
                    <label style={{ display: 'flex', alignItems: 'flex-start', gap: 8, cursor: 'pointer' }}>
                      <input type="checkbox"
                        checked={prepChCustomPostsEnabled}
                        onChange={e => setPrepChCustomPostsEnabled(e.target.checked)}
                        style={{ marginTop: 2 }} />
                      <div>
                        <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)' }}>
                          🎯 Разные посты для каждого акка (ручное распределение)
                        </div>
                        <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 3, lineHeight: 1.4 }}>
                          Ты сам ставишь: для акка #1 — свой текст и фото/видео,
                          для акка #2 — другое. Общий пост выше и рандомизация игнорятся
                          для акков у которых задано ручное значение.
                          Акки без ручного поста возьмут общий (если задан).
                        </div>
                      </div>
                    </label>
                    {prepChCustomPostsEnabled && (
                      <div style={{
                        marginTop: 10, maxHeight: 340, overflowY: 'auto',
                        background: 'var(--bg-2)', borderRadius: 6, padding: 6,
                        display: 'flex', flexDirection: 'column', gap: 6,
                      }}>
                        {selectedIds.size === 0 ? (
                          <div style={{ fontSize: 11, color: 'var(--text-3)', padding: 8, textAlign: 'center' }}>
                            Ни один акк не выбран — сначала отметь чекбоксами в списке акков
                          </div>
                        ) : (
                          [...selectedIds].map(accId => {
                            const acc = accounts.find(a => a.id === accId)
                            const label = acc ? (acc.first_name ? `${acc.first_name} · ${acc.phone}` : acc.phone) : `#${accId}`
                            const entry = prepChCustomPosts[accId] || { text: '', photoFile: null }
                            return (
                              <div key={accId} style={{
                                padding: 8, borderRadius: 5, background: 'var(--bg-3)',
                                border: '1px solid var(--border)',
                              }}>
                                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                                  <code style={{ fontSize: 10, color: 'var(--text-2)' }}>{label}</code>
                                  {(entry.text || entry.photoFile) && (
                                    <button onClick={() => setPrepChCustomPosts(prev => {
                                      const next = { ...prev }; delete next[accId]; return next
                                    })} style={{
                                      background: 'transparent', border: 'none', cursor: 'pointer',
                                      color: 'var(--text-3)', fontSize: 10,
                                    }}>✕ очистить</button>
                                  )}
                                </div>
                                <textarea value={entry.text}
                                  onChange={e => setPrepChCustomPosts(prev => ({
                                    ...prev, [accId]: { ...(prev[accId] || {}), text: e.target.value }
                                  }))}
                                  placeholder="Текст поста для этого акка (опционально)"
                                  maxLength={4096}
                                  style={{
                                    width: '100%', minHeight: 40, padding: 6,
                                    background: 'var(--bg)', border: '1px solid var(--border)',
                                    borderRadius: 4, color: 'var(--text)', fontSize: 11, resize: 'vertical',
                                  }} />
                                <div style={{ marginTop: 4, display: 'flex', alignItems: 'center', gap: 6 }}>
                                  <label style={{
                                    padding: '3px 8px', borderRadius: 4, cursor: 'pointer',
                                    background: 'var(--bg)', border: '1px solid var(--border)',
                                    fontSize: 10, color: 'var(--text-2)',
                                  }}>
                                    📎 {entry.photoFile ? 'сменить' : 'фото/видео'}
                                    <input type="file"
                                      accept="image/*,video/*,.mov,.mkv,.avi,.m4v"
                                      style={{ display: 'none' }}
                                      onChange={e => {
                                        const f = e.target.files?.[0]
                                        if (!f) return
                                        setPrepChCustomPosts(prev => ({
                                          ...prev, [accId]: { ...(prev[accId] || {}), photoFile: f }
                                        }))
                                        e.target.value = ''
                                      }} />
                                  </label>
                                  {entry.photoFile && (() => {
                                    const nameLower = (entry.photoFile.name || '').toLowerCase()
                                    const isVideo = (entry.photoFile.type || '').startsWith('video/') ||
                                      VIDEO_EXTS.some(ext => nameLower.endsWith(ext))
                                    const mb = entry.photoFile.size / (1024 * 1024)
                                    return (
                                      <>
                                        <span style={{ fontSize: 10, color: 'var(--text-2)', flex: 1,
                                          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                          {isVideo ? '🎬' : '🖼'} {entry.photoFile.name}
                                        </span>
                                        <span style={{ fontSize: 9, color: 'var(--text-3)' }}>
                                          {mb >= 1 ? `${mb.toFixed(1)} MB` : `${(entry.photoFile.size / 1024).toFixed(0)} KB`}
                                        </span>
                                        <button onClick={() => setPrepChCustomPosts(prev => ({
                                          ...prev, [accId]: { ...(prev[accId] || {}), photoFile: null }
                                        }))} style={{
                                          background: 'transparent', border: 'none', cursor: 'pointer',
                                          color: 'var(--text-3)', fontSize: 12,
                                        }}>×</button>
                                      </>
                                    )
                                  })()}
                                </div>
                              </div>
                            )
                          })
                        )}
                      </div>
                    )}
                  </div>

                  {/* Аватарки канала (drag-and-drop) */}
                  <div>
                    <div style={{ fontSize: 11, color: 'var(--text-2)', marginBottom: 4, fontWeight: 600 }}>
                      Аватарки канала (опционально — если нужно поставить фото на канал)
                    </div>
                    <div
                      onClick={() => prepChFileInputRef.current?.click()}
                      onDragOver={e => { e.preventDefault(); e.stopPropagation(); setPrepChDragOver(true) }}
                      onDragEnter={e => { e.preventDefault(); e.stopPropagation(); setPrepChDragOver(true) }}
                      onDragLeave={e => { e.preventDefault(); e.stopPropagation(); setPrepChDragOver(false) }}
                      onDrop={e => {
                        e.preventDefault(); e.stopPropagation(); setPrepChDragOver(false)
                        addPrepChFiles(e.dataTransfer.files)
                      }}
                      style={{
                        padding: 12, textAlign: 'center', cursor: 'pointer',
                        border: `2px dashed ${prepChDragOver ? 'var(--violet)' : 'var(--border)'}`,
                        borderRadius: 6,
                        background: prepChDragOver ? 'rgba(139,92,246,0.10)' : 'var(--bg-3)',
                        fontSize: 11, color: 'var(--text-2)',
                        transition: 'all 0.15s ease',
                      }}>
                      {prepChAvatarFiles.length === 0
                        ? (prepChDragOver ? '📥 Отпусти чтобы добавить' : '🖼️ Перетащи или кликни · распределятся рандомно')
                        : `✓ ${prepChAvatarFiles.length} файл(ов) — кликни/перетащи чтобы дополнить`}
                    </div>
                    <input ref={prepChFileInputRef} type="file" accept="image/*" multiple
                      style={{ display: 'none' }}
                      onChange={e => { addPrepChFiles(e.target.files); e.target.value = '' }} />
                    {prepChAvatarFiles.length > 0 && (
                      <div style={{
                        marginTop: 6, maxHeight: 100, overflowY: 'auto',
                        background: 'var(--bg-3)', borderRadius: 4, padding: 4,
                      }}>
                        {prepChAvatarFiles.map((f, i) => (
                          <div key={`${f.name}-${f.size}-${i}`} style={{
                            display: 'flex', alignItems: 'center', gap: 6,
                            padding: '3px 6px', fontSize: 10,
                          }}>
                            <span style={{ color: 'var(--text-3)' }}>🖼</span>
                            <span style={{ color: 'var(--text-2)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {f.name}
                            </span>
                            <button onClick={(ev) => { ev.stopPropagation(); removePrepChFile(i) }}
                              style={{ background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--text-3)', fontSize: 12 }}>×</button>
                          </div>
                        ))}
                      </div>
                    )}
                    {/* Удалить старую аватарку канала перед uploadом новой.
                        В fast-mode бесполезно (канал только создан), но
                        оставлено для plan-mode когда set_channel_avatar
                        может выполняться после существующего create_channel. */}
                    <label style={{
                      marginTop: 8, padding: '6px 10px', borderRadius: 6,
                      background: prepDeletePrevChannelAvatar ? 'rgba(239,68,68,0.08)' : 'var(--bg-3)',
                      border: `1px solid ${prepDeletePrevChannelAvatar ? 'rgba(239,68,68,0.35)' : 'var(--border)'}`,
                      display: 'flex', alignItems: 'flex-start', gap: 6, cursor: 'pointer',
                    }}>
                      <input type="checkbox"
                        checked={prepDeletePrevChannelAvatar}
                        onChange={e => setPrepDeletePrevChannelAvatar(e.target.checked)}
                        style={{ marginTop: 2 }} />
                      <div>
                        <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text)' }}>
                          🗑 Удалить старую аватарку канала перед uploadом
                        </div>
                        <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 2, lineHeight: 1.4 }}>
                          В одной сессии: сначала снимет фото → пауза 2-5с → поставит новую. Актуально в plan-mode.
                        </div>
                      </div>
                    </label>
                  </div>

                  {/* ── Публичный канал ── */}
                  <div style={{
                    padding: 10, borderRadius: 8,
                    background: prepChPublic ? 'rgba(139,92,246,0.06)' : 'var(--bg-3)',
                    border: `1px solid ${prepChPublic ? 'var(--violet)' : 'var(--border)'}`,
                  }}>
                    <label style={{ display: 'flex', alignItems: 'flex-start', gap: 8, cursor: 'pointer' }}>
                      <input type="checkbox"
                        checked={prepChPublic}
                        onChange={e => setPrepChPublic(e.target.checked)}
                        style={{ marginTop: 2 }} />
                      <div>
                        <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)' }}>
                          🌍 Публичный канал (с @username)
                        </div>
                        <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 3, lineHeight: 1.4 }}>
                          После создания канала попробует поставить @username из пула ниже.
                          Если имя занято → добавит числовой суффикс (например
                          <code style={{ background: 'var(--bg)', padding: '0 3px', borderRadius: 2 }}>mychannel</code>
                          →
                          <code style={{ background: 'var(--bg)', padding: '0 3px', borderRadius: 2 }}>mychannel4287</code>).
                          Без чекбокса — приватный канал с t.me/+invite ссылкой.
                        </div>
                      </div>
                    </label>
                    {prepChPublic && (
                      <div style={{ marginTop: 10 }}>
                        <div style={{ fontSize: 11, color: 'var(--text-2)', marginBottom: 4, fontWeight: 600 }}>
                          Пул username'ов (по одному на строку)
                        </div>
                        <textarea value={prepChUsernames}
                          onChange={e => setPrepChUsernames(e.target.value)}
                          placeholder={`mychannel\nnewsdaily\nlifeblog\ncryptocorner`}
                          style={{
                            width: '100%', minHeight: 70, padding: 8,
                            background: 'var(--bg)', border: '1px solid var(--border)',
                            borderRadius: 6, color: 'var(--text)', fontSize: 12,
                            fontFamily: 'monospace', resize: 'vertical',
                          }} />
                        <div style={{ marginTop: 4, fontSize: 10, color: 'var(--text-3)', lineHeight: 1.4 }}>
                          Правила Telegram: 5-32 символа, начинается с буквы,
                          латиница/цифры/подчёркивание.
                          Валидных: <b style={{ color: prepChValidUsernames.length > 0 ? 'var(--green)' : 'var(--red)' }}>
                            {prepChValidUsernames.length}
                          </b>
                          {' '}из {prepChUsernames.split('\n').filter(s => s.trim()).length} введённых.
                          Каждому акку — случайный из пула, при коллизии — суффикс.
                        </div>
                      </div>
                    )}
                  </div>

                  {/* Pin */}
                  <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: 'var(--text-2)', cursor: 'pointer' }}>
                    <input type="checkbox" checked={prepChPin}
                      onChange={e => setPrepChPin(e.target.checked)} />
                    📌 Закрепить канал в профиле (рекомендуется — выглядит как «личный канал автора»)
                  </label>
                </div>
              )}
            </div>

            {/* Режим: fast / plan */}
            <div style={{ padding: 12, borderRadius: 8, background: 'var(--bg-2)', border: '1px solid var(--border)' }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)', marginBottom: 8 }}>
                🎛 Режим выполнения
              </div>
              <div style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
                <button type="button"
                  onClick={() => setPrepMode('fast')}
                  style={{
                    flex: 1, padding: '10px 12px', borderRadius: 6, cursor: 'pointer',
                    background: prepMode === 'fast' ? 'rgba(139,92,246,0.15)' : 'var(--bg-3)',
                    border: `1px solid ${prepMode === 'fast' ? 'var(--violet)' : 'var(--border)'}`,
                    color: 'var(--text)', fontSize: 12, textAlign: 'left',
                  }}>
                  <div style={{ fontWeight: 700 }}>⚡ Быстро</div>
                  <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 3 }}>
                    По одному коннекту на акк. Все действия за раз. Пауза между акками 60-180 сек.
                  </div>
                </button>
                <button type="button"
                  onClick={() => setPrepMode('plan')}
                  style={{
                    flex: 1, padding: '10px 12px', borderRadius: 6, cursor: 'pointer',
                    background: prepMode === 'plan' ? 'rgba(139,92,246,0.15)' : 'var(--bg-3)',
                    border: `1px solid ${prepMode === 'plan' ? 'var(--violet)' : 'var(--border)'}`,
                    color: 'var(--text)', fontSize: 12, textAlign: 'left',
                  }}>
                  <div style={{ fontWeight: 700 }}>🗓 План (расписание) ⭐</div>
                  <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 3 }}>
                    N сессий на акк, действия разбиты во времени. Разные акки идут параллельно
                    на разных прокси. Выглядит как «человек заходит в TG в разное время».
                  </div>
                </button>
              </div>

              {prepMode === 'fast' && (
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontSize: 12, color: 'var(--text-2)' }}>Пауза между акками</span>
                  <input type="number" value={prepMinDelay} min={10} max={3600}
                    onChange={e => setPrepMinDelay(Math.max(10, Math.min(3600, parseInt(e.target.value || '60'))))}
                    style={{ width: 70, padding: '5px 8px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 6, color: 'var(--text)', fontSize: 12 }} />
                  <span style={{ fontSize: 12, color: 'var(--text-3)' }}>—</span>
                  <input type="number" value={prepMaxDelay} min={prepMinDelay} max={3600}
                    onChange={e => setPrepMaxDelay(Math.max(prepMinDelay, Math.min(3600, parseInt(e.target.value || '180'))))}
                    style={{ width: 70, padding: '5px 8px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 6, color: 'var(--text)', fontSize: 12 }} />
                  <span style={{ fontSize: 12, color: 'var(--text-2)' }}>сек</span>
                  <div style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-3)' }}>
                    ≈ {(() => {
                      const avg = (prepMinDelay + prepMaxDelay) / 2
                      const totalSec = selectedIds.size * avg
                      if (totalSec < 60) return `${Math.round(totalSec)} сек`
                      if (totalSec < 3600) return `${Math.round(totalSec / 60)} мин`
                      const h = Math.floor(totalSec / 3600), m = Math.round((totalSec % 3600) / 60)
                      return `${h}ч ${m}м`
                    })()}
                  </div>
                </div>
              )}

              {prepMode === 'plan' && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontSize: 12, color: 'var(--text-2)', minWidth: 130 }}>
                      Уложиться за (часов):
                    </span>
                    <input type="number" step="0.1" min={0.1} max={24}
                      value={prepDurationHours}
                      onChange={e => {
                        setPrepDurationUserEdited(true)
                        setPrepDurationHours(Math.max(0.1, Math.min(24, parseFloat(e.target.value || '0.5'))))
                      }}
                      style={{ width: 80, padding: '5px 8px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 6, color: 'var(--text)', fontSize: 12 }} />
                    {prepRecommend && (
                      <div style={{ marginLeft: 'auto', fontSize: 11 }}>
                        <span style={{ color: 'var(--text-3)' }}>рекомендация: </span>
                        <button type="button" onClick={() => {
                          setPrepDurationUserEdited(false)
                          setPrepDurationHours(prepRecommend.recommended_hours)
                        }}
                          style={{ background: 'none', border: 'none', color: 'var(--violet)', fontWeight: 600, cursor: 'pointer', fontSize: 11 }}>
                          {prepRecommend.recommended_hours}ч
                        </button>
                        <span style={{ color: 'var(--text-3)' }}>
                          {' '}(мин {prepRecommend.min_hours}ч)
                        </span>
                      </div>
                    )}
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontSize: 12, color: 'var(--text-2)', minWidth: 130 }}>
                      Сессий на акк:
                    </span>
                    <input type="number" min={1} max={6}
                      value={prepSessionsPerAcc}
                      onChange={e => setPrepSessionsPerAcc(Math.max(1, Math.min(6, parseInt(e.target.value || '3'))))}
                      style={{ width: 80, padding: '5px 8px', background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 6, color: 'var(--text)', fontSize: 12 }} />
                    <span style={{ fontSize: 11, color: 'var(--text-3)', marginLeft: 8 }}>
                      действий распределится по сессиям в разном порядке для каждого акка
                    </span>
                  </div>
                  <div style={{
                    padding: '6px 10px', borderRadius: 6, fontSize: 11,
                    background: 'rgba(61,214,140,0.06)', border: '1px dashed rgba(61,214,140,0.30)',
                    color: 'var(--text-2)',
                  }}>
                    🕐 <b>{selectedIds.size}</b> акк(ов) × <b>{prepSessionsPerAcc}</b> сессий за <b>{prepDurationHours}ч</b> ≈
                    {' '}{Math.round((selectedIds.size * prepSessionsPerAcc * 60) / (prepDurationHours * 60))} акк·сессий/час.
                    Разные прокси идут параллельно, один прокси — по очереди.
                  </div>
                </div>
              )}
            </div>

            <div style={{ display: 'flex', gap: 8, justifyContent: 'space-between', alignItems: 'center' }}>
              <button type="button"
                onClick={() => {
                  if (window.confirm('Очистить всю форму подготовки? Тексты, чекбоксы, имена, username\'ы — всё обнулится (файлы уже эфемерны).'))
                    clearPrepForm()
                }}
                style={{
                  background: 'transparent', border: 'none', cursor: 'pointer',
                  color: 'var(--text-3)', fontSize: 11, textDecoration: 'underline',
                }}
                title="Форма автосохраняется в localStorage. Эта кнопка обнуляет всё.">
                🗑 Очистить форму
              </button>
              <div style={{ display: 'flex', gap: 8 }}>
                <Button variant="ghost" onClick={() => setPrepModal(false)}>Отмена</Button>
                <Button variant="primary" loading={prepStarting} onClick={handlePrepStart}>
                  🚀 Запустить {prepMode === 'plan' ? 'план' : 'подготовку'}
                </Button>
              </div>
            </div>
          </div>
        ) : (
          /* Прогресс */
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {prepProgress && prepProgress._mode !== 'plan' && (
              /* ── FAST MODE PROGRESS ─────────────────────────── */
              <>
                <div style={{ fontSize: 13, color: 'var(--text)' }}>
                  Статус: <b>{prepProgress.status === 'done' ? '✅ Готово' :
                            prepProgress.status === 'queued' ? '⏳ В очереди…' :
                            prepProgress.status === 'error' ? '❌ Ошибка' : '🔄 Работаем…'}</b>
                </div>
                <div style={{ background: 'var(--bg-2)', borderRadius: 8, padding: '10px 14px' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 6 }}>
                    <span style={{ fontSize: 12, color: 'var(--text-2)' }}>
                      Обработано: <b>{prepProgress.done || 0}</b> / {prepProgress.total || 0}
                    </span>
                    <span style={{ fontSize: 11, color: 'var(--text-3)' }}>
                      <span style={{ color: 'var(--green)' }}>{prepProgress.ok || 0} ok</span>
                      {prepProgress.failed > 0 && (
                        <span style={{ color: 'var(--red)', marginLeft: 8 }}>{prepProgress.failed} fail</span>
                      )}
                    </span>
                  </div>
                  <div style={{ height: 8, background: 'var(--bg-3)', borderRadius: 4, overflow: 'hidden' }}>
                    <div style={{
                      height: '100%',
                      width: `${Math.min(100, ((prepProgress.done || 0) / Math.max(1, prepProgress.total || 1)) * 100)}%`,
                      background: 'var(--violet)', transition: 'width 0.5s',
                    }} />
                  </div>
                  {prepProgress.current_phone && prepProgress.status !== 'done' && (
                    <div style={{ marginTop: 8, fontSize: 11, color: 'var(--text-3)' }}>
                      Сейчас: <code style={{ background: 'var(--bg-3)', padding: '1px 5px', borderRadius: 3 }}>{prepProgress.current_phone}</code>
                      {prepProgress.sleeping_sec > 0 && (
                        <span style={{ marginLeft: 8 }}>💤 пауза {prepProgress.sleeping_sec}с</span>
                      )}
                    </div>
                  )}
                </div>
                {prepProgress.results && prepProgress.results.length > 0 && (
                  <div style={{
                    maxHeight: 200, overflowY: 'auto',
                    background: 'var(--bg-3)', borderRadius: 6, padding: 8,
                    fontFamily: 'monospace', fontSize: 11,
                  }}>
                    {prepProgress.results.slice().reverse().map((r, i) => (
                      <div key={i} style={{
                        display: 'flex', gap: 8, padding: '3px 6px',
                        color: r.success ? 'var(--text-2)' : 'var(--red)',
                      }}>
                        <span style={{ color: r.success ? 'var(--green)' : 'var(--red)' }}>
                          {r.success ? '✓' : '✗'}
                        </span>
                        <span>{r.phone}</span>
                        <span style={{ marginLeft: 'auto', opacity: 0.7 }}>
                          {r.success ? (r.message || 'ok') : (r.error || 'err')}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}

            {prepProgress && prepProgress._mode === 'plan' && (
              /* ── PLAN MODE PROGRESS ─────────────────────────── */
              <>
                <div style={{ fontSize: 13, color: 'var(--text)' }}>
                  {(() => {
                    const phase = prepProgress.phase || (
                      prepProgress.status === 'done' ? 'done' :
                      prepProgress.status === 'idle' ? 'generating' :
                      'running'
                    )
                    // Ближайшая pending-сессия для отображения "первая в HH:MM"
                    let nextTs = null
                    if (prepProgress.accounts) {
                      for (const a of prepProgress.accounts) {
                        for (const s of (a.sessions || [])) {
                          if (['pending', 'scheduled'].includes(s.status)) {
                            if (nextTs === null || s.connect_ts < nextTs) nextTs = s.connect_ts
                          }
                        }
                      }
                    }
                    const nextTime = nextTs ? new Date(nextTs * 1000).toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit'}) : null
                    const now = Math.floor(Date.now() / 1000)
                    const nextIn = nextTs ? Math.max(0, nextTs - now) : null
                    const nextInStr = nextIn == null ? null :
                      nextIn < 60 ? `${nextIn}с` :
                      nextIn < 3600 ? `${Math.round(nextIn / 60)}м` :
                      `${Math.floor(nextIn/3600)}ч ${Math.round((nextIn%3600)/60)}м`
                    return (
                      <>
                        Статус: <b>
                          {phase === 'generating' ? '🧠 Генерирую расписание сессий…' :
                           phase === 'scheduled' ? `🗓 Запланировано${nextTime ? `, первая сессия в ${nextTime} (через ${nextInStr})` : ''}` :
                           phase === 'running' ? '🔄 План идёт' :
                           phase === 'cancelling' ? '🛑 Останавливаю…' :
                           phase === 'cancelled' ? '⛔ Остановлено' :
                           phase === 'done' ? '✅ Все сессии выполнены' :
                           '🔄 План идёт…'}
                        </b>
                        {phase === 'generating' && (
                          <div style={{ marginTop: 4, fontSize: 11, color: 'var(--text-3)' }}>
                            Обычно занимает 1-3 секунды. Если висит &gt; 30 сек — Celery worker может быть не запущен.
                          </div>
                        )}
                        {phase === 'cancelling' && (
                          <div style={{ marginTop: 4, fontSize: 11, color: 'var(--text-3)' }}>
                            Revoke отправлен в Celery — очередь чистится. Через 1-2 сек всё будет cancelled.
                          </div>
                        )}
                        {['running', 'scheduled', 'cancelling'].includes(phase) && (
                          <div style={{ marginTop: 8 }}>
                            <Button variant="ghost" size="sm"
                              loading={prepCancelling}
                              disabled={phase === 'cancelling'}
                              onClick={handlePrepPlanCancel}
                              style={{ color: 'var(--red)' }}>
                              🛑 Остановить план
                            </Button>
                          </div>
                        )}
                        {/* Дожать: показываем на done/cancelled если есть skipped/failed */}
                        {['done', 'cancelled'].includes(phase) && prepProgress.totals && (
                          (prepProgress.totals.sessions_failed || 0) +
                          (prepProgress.totals.sessions_skipped || 0) > 0
                        ) && (
                          <div style={{ marginTop: 8 }}>
                            <Button variant="primary" size="sm"
                              loading={prepResuming}
                              onClick={handlePrepPlanResume}>
                              🔄 Дожать skipped/failed ({(prepProgress.totals.sessions_skipped || 0) + (prepProgress.totals.sessions_failed || 0)})
                            </Button>
                          </div>
                        )}
                        {/* Refix первого поста: показываем на done/running если в job'е был create_channels */}
                        {prepProgress.meta?.config?.create_channels && (
                          <div style={{ marginTop: 8, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                            <Button variant="secondary" size="sm"
                              loading={prepRefixingVideo}
                              onClick={handlePrepRefixVideo}
                              title="Заменить первый пост во всех каналах этого job'а на новое видео/фото. Полезно если .mov отправился как документ.">
                              🎬 Перезалить первый пост
                            </Button>
                            <Button variant="secondary" size="sm"
                              loading={prepRefixingAva}
                              onClick={handlePrepRefixAva}
                              title="Поставить/сменить аватарку у всех каналов этого job'а. Полезно если забыл включить чекбокс при запуске плана.">
                              🖼 Аватарка канала
                            </Button>
                            <Button variant="secondary" size="sm"
                              loading={prepRefixingAttach}
                              onClick={handlePrepRefixAttach}
                              title="Прикрепить созданный канал к профилю юзера (personal_channel — значок канала в профиле, не в закреплённых чатах). Файл не нужен.">
                              🔗 Прикрепить канал в профиль
                            </Button>
                            <input ref={prepRefixFileInputRef} type="file"
                              accept="image/*,video/*,.mov,.mkv,.avi,.m4v"
                              style={{ display: 'none' }}
                              onChange={e => {
                                const f = e.target.files?.[0]
                                if (!f) return
                                const cap = window.prompt(
                                  '🎬 Refix первого поста\n\n' +
                                  `Файл: ${f.name} (${(f.size/(1024*1024)).toFixed(1)} MB)\n\n` +
                                  'Каналы обхода: все где успел создаться канал в этом job\'е.\n' +
                                  'Между акками — пауза 30-90 сек + IP throttle.\n\n' +
                                  'Подпись под медиа (опционально):', ''
                                )
                                if (cap === null) { e.target.value = ''; return }
                                handlePrepRefixVideoFile(f, cap)
                                e.target.value = ''
                              }} />
                            <input ref={prepRefixAvaInputRef} type="file"
                              accept="image/*"
                              style={{ display: 'none' }}
                              onChange={e => {
                                const f = e.target.files?.[0]
                                if (!f) return
                                const delPrev = window.confirm(
                                  '🖼 Refix аватарки канала\n\n' +
                                  `Файл: ${f.name} (${(f.size/1024).toFixed(0)} KB)\n\n` +
                                  'Ставится в ВСЕ каналы акков этого job\'а.\n' +
                                  'Между акками — пауза 30-90 сек + IP throttle.\n\n' +
                                  'Удалить старую аватарку сначала? (если каналы уже что-то имели)\n' +
                                  '  OK = удалить старую → пауза 2-5 сек → поставить новую\n' +
                                  '  Cancel = просто поставить новую поверх'
                                )
                                handlePrepRefixAvaFile(f, delPrev)
                                e.target.value = ''
                              }} />
                          </div>
                        )}
                        {/* ── Прогресс refix'ов (avatar + video) ── */}
                        {['avatar', 'video', 'attach'].map(kind => {
                          const rp = prepRefixProgress[kind]
                          if (!rp || rp.status === 'idle') return null
                          const label =
                            kind === 'avatar' ? '🖼 Аватарки каналов'
                            : kind === 'video' ? '🎬 Первый пост'
                            : '🔗 Прикрепление канала в профиль'
                          const done = rp.done || 0
                          const total = rp.total || 0
                          const ok = rp.ok || 0
                          const fail = rp.failed || 0
                          const pct = total > 0 ? Math.round((done / total) * 100) : 0
                          const isDone = rp.status === 'done'
                          const isError = rp.status === 'error'
                          const isCancelled = rp.status === 'cancelled'
                          const isStale = !!rp.stale
                          const bgColor = isDone ? 'rgba(61,214,140,0.08)'
                                        : isError ? 'rgba(248,81,73,0.10)'
                                        : isCancelled ? 'rgba(150,150,150,0.10)'
                                        : 'rgba(139,92,246,0.08)'
                          const borderColor = isDone ? 'rgba(61,214,140,0.35)'
                                            : isError ? 'rgba(248,81,73,0.45)'
                                            : isCancelled ? 'rgba(150,150,150,0.35)'
                                            : 'rgba(139,92,246,0.35)'
                          const titleIcon = isDone ? '✅'
                                          : isError ? '❌'
                                          : isCancelled ? '⏹'
                                          : '🔄'
                          return (
                            <div key={kind} style={{
                              marginTop: 8, padding: '8px 12px', borderRadius: 6,
                              background: bgColor,
                              border: `1px solid ${borderColor}`,
                              fontSize: 11,
                            }}>
                              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                                <b style={{ color: 'var(--text)' }}>
                                  {titleIcon} {label} · {done}/{total} акк
                                </b>
                                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                                  <span style={{ color: 'var(--text-3)' }}>
                                    <span style={{ color: 'var(--green)' }}>{ok} ok</span>
                                    {fail > 0 && <span style={{ color: 'var(--red)', marginLeft: 8 }}>{fail} fail</span>}
                                  </span>
                                  {(isError || isDone || isCancelled) && (
                                    <button
                                      onClick={() => setPrepRefixProgress(prev => {
                                        const next = { ...prev }; delete next[kind]; return next
                                      })}
                                      title="Закрыть баннер"
                                      style={{
                                        background: 'none', border: 'none',
                                        color: 'var(--text-3)', cursor: 'pointer',
                                        fontSize: 14, padding: '0 4px', lineHeight: 1,
                                      }}
                                    >✕</button>
                                  )}
                                </div>
                              </div>
                              {isError && (
                                <div style={{ color: 'var(--red)', marginTop: 4, marginBottom: 4 }}>
                                  {isStale ? '⚠ Воркер умер, задача прервана. ' : ''}
                                  {rp.error || 'Ошибка выполнения. Запусти заново.'}
                                </div>
                              )}
                              <div style={{ height: 4, background: 'var(--bg-3)', borderRadius: 2, overflow: 'hidden' }}>
                                <div style={{
                                  height: '100%', width: `${pct}%`,
                                  background: isDone ? 'var(--green)' : 'var(--violet)',
                                  transition: 'width 0.5s',
                                }} />
                              </div>
                              {!isDone && rp.current_phone && (
                                <div style={{ marginTop: 4, color: 'var(--text-3)' }}>
                                  Сейчас: <code style={{ background: 'var(--bg-3)', padding: '1px 4px', borderRadius: 3 }}>{rp.current_phone}</code>
                                  {rp.sleeping_sec > 0 && <span style={{ marginLeft: 6 }}>💤 пауза {rp.sleeping_sec}с</span>}
                                </div>
                              )}
                              {/* Список последних результатов + retry статус */}
                              {rp.results && rp.results.length > 0 && (
                                <div style={{
                                  marginTop: 6, maxHeight: 150, overflowY: 'auto',
                                  background: 'var(--bg-3)', borderRadius: 4, padding: 4,
                                  fontSize: 10, fontFamily: 'monospace',
                                }}>
                                  {rp.results.slice().reverse().map((r, i) => {
                                    const reason = r.reason || ''
                                    const isRetrying = reason.includes('(retry ') || reason.includes('жду и повторю')
                                    const isFlood = reason.toLowerCase().includes('floodwait') ||
                                                    reason.toLowerCase().includes('wait of')
                                    let icon = r.ok ? '✓' : '✗'
                                    let color = r.ok ? 'var(--green)' : 'var(--red)'
                                    if (isRetrying) { icon = '🔁'; color = '#f97316' }
                                    else if (isFlood && !r.ok) { icon = '⏰'; color = '#eab308' }
                                    return (
                                      <div key={i} style={{
                                        display: 'flex', gap: 6, padding: '2px 4px',
                                        color: r.ok ? 'var(--text-2)' : (isRetrying ? '#f97316' : 'var(--red)'),
                                      }}>
                                        <span style={{ color }}>{icon}</span>
                                        <span>{r.phone || `acc:${r.acc_id}`}</span>
                                        {r.attempts > 1 && (
                                          <span style={{ color: 'var(--text-3)', marginLeft: 2 }}>×{r.attempts}</span>
                                        )}
                                        {reason && (
                                          <span style={{ marginLeft: 'auto', opacity: 0.7 }}>
                                            {reason.slice(0, 60)}
                                          </span>
                                        )}
                                      </div>
                                    )
                                  })}
                                </div>
                              )}
                            </div>
                          )
                        })}
                      </>
                    )
                  })()}
                </div>
                {prepProgress.totals && (
                  <div style={{ background: 'var(--bg-2)', borderRadius: 8, padding: '10px 14px' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6, fontSize: 12, color: 'var(--text-2)' }}>
                      <span>Сессий: <b>{prepProgress.totals.sessions_done}</b> / {prepProgress.totals.sessions_total}</span>
                      <span style={{ display: 'flex', gap: 8 }}>
                        <span style={{ color: 'var(--green)' }}>{prepProgress.totals.sessions_done} ok</span>
                        {prepProgress.totals.sessions_failed > 0 && (
                          <span style={{ color: 'var(--red)' }}>{prepProgress.totals.sessions_failed} fail</span>
                        )}
                        {(prepProgress.totals.sessions_skipped || 0) > 0 && (
                          <span style={{ color: '#94a3b8' }}>{prepProgress.totals.sessions_skipped} skip</span>
                        )}
                        <span style={{ color: 'var(--text-3)' }}>{prepProgress.totals.sessions_pending} ждут</span>
                      </span>
                    </div>
                    <div style={{ height: 8, background: 'var(--bg-3)', borderRadius: 4, overflow: 'hidden' }}>
                      <div style={{
                        height: '100%',
                        width: `${Math.min(100, ((prepProgress.totals.sessions_done || 0) / Math.max(1, prepProgress.totals.sessions_total || 1)) * 100)}%`,
                        background: 'var(--violet)', transition: 'width 0.5s',
                      }} />
                    </div>
                    {prepProgress.meta?.target_duration_sec && (
                      <div style={{ marginTop: 6, fontSize: 11, color: 'var(--text-3)' }}>
                        🕐 Целевое время: {Math.round(prepProgress.meta.target_duration_sec / 3600 * 10) / 10}ч ·
                        {' '}{prepProgress.totals.accounts_total} акк ·
                        {' '}начато {new Date(prepProgress.meta.start_ts * 1000).toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit'})}
                      </div>
                    )}
                  </div>
                )}
                {prepProgress.accounts && prepProgress.accounts.length > 0 && (
                  <div style={{
                    maxHeight: 320, overflowY: 'auto',
                    background: 'var(--bg-3)', borderRadius: 6, padding: 4,
                    fontSize: 11,
                  }}>
                    {prepProgress.accounts.map(a => (
                      <div key={a.account_id} style={{
                        padding: '6px 8px', borderBottom: '1px solid var(--bg-2)',
                      }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                          <code style={{ background: 'var(--bg-2)', padding: '1px 5px', borderRadius: 3, fontSize: 11 }}>
                            {a.phone}
                          </code>
                          <span style={{ fontSize: 10, color: 'var(--text-3)' }}>
                            <span style={{ color: 'var(--green)' }}>{a.sessions_done}</span>
                            {' / '}
                            {a.sessions_total} сессий
                            {a.sessions_failed > 0 && (
                              <span style={{ color: 'var(--red)', marginLeft: 4 }}>({a.sessions_failed} fail)</span>
                            )}
                          </span>
                        </div>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                          {a.sessions.map((s, i) => {
                            const nowMs = Date.now()
                            const isPast = s.connect_ts * 1000 < nowMs
                            const lateSec = isPast ? Math.floor((nowMs - s.connect_ts * 1000) / 1000) : 0
                            const time = new Date(s.connect_ts * 1000).toLocaleTimeString('ru-RU', {hour:'2-digit', minute:'2-digit'})
                            // Стили по статусу.
                            //   failed_retry — сессия падала, Celery retry
                            //     запланировал повтор. Оранжевый.
                            //   late (pending + past connect_ts) — жёлтый
                            //     (не запустилась вовремя, ждёт своей очереди
                            //      или worker занят).
                            // Терминальные статусы (без retry):
                            //   done            — успех
                            //   failed          — упало во время выполнения
                            //   failed_permanent — legacy (со старых job'ов)
                            //   skipped         — прокси/акк был занят, пропущено (без retry!)
                            //   not_authorized  — сессия акка мертва
                            //   cancelled       — юзер нажал стоп
                            const isFailed = s.status === 'failed' || s.status === 'failed_permanent'
                            const isSkipped = s.status === 'skipped'
                            const isNotAuth = s.status === 'not_authorized'
                            const isCancelled = s.status === 'cancelled'
                            const bg = s.status === 'done' ? 'rgba(61,214,140,0.15)'
                                     : isFailed ? 'rgba(239,68,68,0.15)'
                                     : s.status === 'running' ? 'rgba(139,92,246,0.15)'
                                     : isCancelled ? 'rgba(148,148,148,0.15)'
                                     : isSkipped ? 'rgba(148,163,184,0.15)'
                                     : isNotAuth ? 'rgba(239,68,68,0.10)'
                                     : isPast ? 'rgba(234,179,8,0.15)' : 'var(--bg-2)'
                            const color = s.status === 'done' ? 'var(--green)'
                                       : isFailed ? 'var(--red)'
                                       : s.status === 'running' ? 'var(--violet)'
                                       : isCancelled ? 'var(--text-3)'
                                       : isSkipped ? '#94a3b8'
                                       : isNotAuth ? 'var(--red)'
                                       : isPast ? '#eab308' : 'var(--text-3)'
                            const icon = s.status === 'done' ? '✓'
                                       : isFailed ? '✗'
                                       : s.status === 'running' ? '⏳'
                                       : isCancelled ? '⛔'
                                       : isSkipped ? '⤵'
                                       : isNotAuth ? '🔒'
                                       : isPast ? '⏱' : '○'
                            // Строим tooltip: время, actions, статус, причина
                            const tipLines = []
                            tipLines.push(`Время: ${new Date(s.connect_ts * 1000).toLocaleString('ru-RU')}`)
                            tipLines.push(`Действия: ${(s.actions || []).join(', ')}`)
                            if (s.status === 'done') {
                              tipLines.push('✓ Готово')
                              if (s.error) tipLines.push(`⚠ ${s.error}`)  // частичный успех
                            } else if (isFailed) {
                              tipLines.push(`✗ Провалено: ${s.error || 'unknown'}`)
                            } else if (s.status === 'running') {
                              tipLines.push('⏳ Идёт')
                            } else if (isCancelled) {
                              tipLines.push('⛔ Отменено')
                              if (s.error) tipLines.push(s.error)
                            } else if (isSkipped) {
                              tipLines.push('⤵ Пропущено (без retry)')
                              if (s.error) tipLines.push(s.error)
                            } else if (isNotAuth) {
                              tipLines.push('🔒 Сессия акка мертва')
                              if (s.error) tipLines.push(s.error)
                            } else if (isPast) {
                              tipLines.push(`⏱ Опоздание ${lateSec < 60 ? lateSec + 'с' : Math.round(lateSec/60) + 'мин'}`)
                              tipLines.push('Worker ещё не подхватил задачу (обычно означает что celery воркер перегружен)')
                            } else {
                              tipLines.push('○ Ждёт своего слота')
                            }
                            return (
                              <span key={i} title={tipLines.join('\n')}
                                style={{
                                  padding: '2px 6px', borderRadius: 3, background: bg, color,
                                  fontSize: 10, whiteSpace: 'nowrap', cursor: 'help',
                                }}>
                                {icon} {time} ({s.actions_count})
                              </span>
                            )
                          })}
                        </div>
                        {/* Проблемные сессии — отдельная строка снизу с последней ошибкой */}
                        {(() => {
                          const problems = a.sessions.filter(s =>
                            s.error || s.status === 'failed_retry' || s.status === 'failed_permanent' ||
                            (s.status !== 'done' && s.status !== 'running' && s.connect_ts * 1000 < Date.now())
                          )
                          if (problems.length === 0) return null
                          const lastProblem = problems[problems.length - 1]
                          const nowMs = Date.now()
                          let msg = ''
                          if (lastProblem.status === 'cancelled') {
                            msg = `⛔ Отменено — ${lastProblem.error || 'пользователем'}`
                          } else if (lastProblem.status === 'skipped') {
                            msg = `⤵ Пропущено — ${lastProblem.error || 'прокси/акк был занят'}`
                          } else if (lastProblem.status === 'not_authorized') {
                            msg = `🔒 ${lastProblem.error || 'Сессия акка мертва — переимпортируй'}`
                          } else if (lastProblem.status === 'failed' || lastProblem.status === 'failed_permanent') {
                            msg = `✗ Провалено — ${lastProblem.error || 'ошибка при выполнении'}`
                          } else if (lastProblem.error) {
                            msg = `⚠ ${lastProblem.error}`
                          } else if (lastProblem.connect_ts * 1000 < nowMs) {
                            const lateSec = Math.floor((nowMs - lastProblem.connect_ts * 1000) / 1000)
                            const lateStr = lateSec < 60 ? `${lateSec}с` : `${Math.round(lateSec/60)}мин`
                            msg = `⏱ Опоздание ${lateStr} — обычно из-за занятого прокси (акк ждёт освобождения IP-lock)`
                          }
                          if (!msg) return null
                          const isRetry = lastProblem.status === 'failed_retry'
                          const isFail = lastProblem.status === 'failed_permanent'
                          const bg = isFail ? 'rgba(239,68,68,0.08)'
                                   : isRetry ? 'rgba(249,115,22,0.08)'
                                   : 'rgba(234,179,8,0.06)'
                          const bd = isFail ? 'rgba(239,68,68,0.30)'
                                   : isRetry ? 'rgba(249,115,22,0.30)'
                                   : 'rgba(234,179,8,0.30)'
                          return (
                            <div style={{
                              marginTop: 4, padding: '3px 8px', fontSize: 10,
                              background: bg, border: `1px solid ${bd}`,
                              borderRadius: 3, color: 'var(--text-2)',
                            }}>
                              {msg}
                            </div>
                          )
                        })()}
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}
            <div style={{ display: 'flex', gap: 8, justifyContent: 'space-between' }}>
              <Button variant="ghost" size="sm"
                onClick={() => {
                  if (prepPollRef.current) clearInterval(prepPollRef.current)
                  setPrepJobId(null)
                  setPrepProgress(null)
                  // Модалка не закрывается — снова config-view
                }}
                title="Забыть этот job и открыть форму для нового">
                ⟳ Новый job
              </Button>
              <Button variant="ghost" onClick={() => {
                // НЕ снимаем polling — job продолжается в фоне,
                // будет виден через floating badge
                setPrepModal(false)
              }}>
                {prepProgress?.status === 'done' ? 'Закрыть' : 'Закрыть (job продолжится)'}
              </Button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  )
}