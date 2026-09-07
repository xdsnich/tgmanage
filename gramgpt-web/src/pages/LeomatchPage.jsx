import { useEffect, useState } from 'react'
import { Card, Button, Input, Badge, Spinner, Empty } from '../components/ui'
import { accountsAPI, leomatchAPI } from '../services/api'

const LANGS = [
  '🇺🇦 Українська', '🇷🇺 Русский', '🇬🇧 English', '🇰🇿 Қазақ',
  '🇮🇩 Indonesia', '🇲🇾 Malay', '🇪🇸 Español', '🇵🇱 Polski',
  '🇮🇹 Italiano', '🇵🇹 🇧🇷 Portuguesa', '🇺🇿 Oʻzbekcha',
]

export default function LeomatchPage() {
  const [tab, setTab] = useState('new')                 // 'new' | 'tasks'
  const [tasks, setTasks] = useState([])
  const [selected, setSelected] = useState(null)        // detail
  const [loadingList, setLoadingList] = useState(false)
  const [likesProgress, setLikesProgress] = useState(null)   // {status,sent,matches,fail,...}

  // ── Form (new batch) ─────────────────────────────────────
  const [accounts, setAccounts] = useState([])
  const [checkedAccIds, setCheckedAccIds] = useState({})
  const [label, setLabel] = useState('')
  const [language, setLanguage] = useState(LANGS[0])
  const [cities, setCities] = useState('Київ\nЛьвів\nОдеса\nДніпро')
  const [descriptions, setDescriptions] = useState('Привіт :)\nЛюблю каву й розмови\nШукаю нових знайомств')
  const [names, setNames] = useState('')
  const [ageMin, setAgeMin] = useState(22)
  const [ageMax, setAgeMax] = useState(32)
  const [seek, setSeek] = useState('male')
  const [genderMode, setGenderMode] = useState('auto')
  const [photosPerAcc, setPhotosPerAcc] = useState(1)
  const [uploadedPhotos, setUploadedPhotos] = useState([])   // [rel_paths]
  const [uploading, setUploading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [errorMsg, setErrorMsg] = useState('')

  useEffect(() => {
    loadAccounts()
    loadTasks()
  }, [])

  // Auto-refresh: раз в 3 сек обновляем список задач если хоть одна в running,
  // и обновляем открытую детальную задачу если она running.
  useEffect(() => {
    const hasRunning = tasks.some(t => t.status === 'running')
    const detailIsRunning = selected?.task?.status === 'running'
    if (!hasRunning && !detailIsRunning) return
    const id = setInterval(() => {
      loadTasks()
      if (selected?.task?.id) openTask(selected.task.id)
    }, 3000)
    return () => clearInterval(id)
  }, [tasks, selected])

  // Likes progress polling — 2 сек когда open task, останавливается когда статус not-running
  useEffect(() => {
    if (!selected?.task?.id) { setLikesProgress(null); return }
    let stopped = false
    const poll = async () => {
      if (stopped) return
      try {
        const { data } = await leomatchAPI.likesStatus(selected.task.id)
        setLikesProgress(data)
      } catch (e) { /* ignore */ }
    }
    poll()
    const id = setInterval(poll, 2000)
    return () => { stopped = true; clearInterval(id) }
  }, [selected?.task?.id])

  async function loadAccounts() {
    try {
      const { data } = await accountsAPI.list()
      // Только active акки (frozen нельзя)
      setAccounts((data || []).filter(a => a.status === 'active'))
    } catch (e) { console.error(e) }
  }
  async function loadTasks() {
    setLoadingList(true)
    try {
      const { data } = await leomatchAPI.listTasks()
      setTasks(data || [])
    } catch (e) { console.error(e) }
    setLoadingList(false)
  }

  const checkedIds = Object.keys(checkedAccIds).filter(k => checkedAccIds[k]).map(Number)

  const toggleAll = () => {
    if (checkedIds.length === accounts.length) setCheckedAccIds({})
    else {
      const m = {}
      for (const a of accounts) m[a.id] = true
      setCheckedAccIds(m)
    }
  }

  const onFilesSelected = async (e) => {
    const files = Array.from(e.target.files || [])
    if (!files.length) return
    setUploading(true)
    try {
      const { data } = await leomatchAPI.uploadPhotos(files)
      setUploadedPhotos(prev => [...prev, ...(data.saved || [])])
    } catch (err) {
      setErrorMsg('Upload failed: ' + (err?.response?.data?.detail || err.message))
    }
    setUploading(false)
    e.target.value = ''
  }

  const submit = async () => {
    setErrorMsg('')
    if (!checkedIds.length) return setErrorMsg('Выбери хотя бы 1 акк')
    if (!uploadedPhotos.length) return setErrorMsg('Загрузи хотя бы 1 фото')
    const cityArr = cities.split('\n').map(s => s.trim()).filter(Boolean)
    if (!cityArr.length) return setErrorMsg('Города пусты')
    const descArr = descriptions.split('\n').map(s => s.trim()).filter(Boolean)
    const nameArr = names.split('\n').map(s => s.trim()).filter(Boolean)

    setSubmitting(true)
    try {
      const { data } = await leomatchAPI.createTask({
        label,
        account_ids: checkedIds,
        cities: cityArr,
        descriptions: descArr,
        names: nameArr,
        photos: uploadedPhotos,
        language,
        age_min: Number(ageMin),
        age_max: Number(ageMax),
        seek,
        gender_mode: genderMode,
        photos_per_acc: Number(photosPerAcc),
      })
      // Сразу стартуем + переходим на вкладку с деталями
      await leomatchAPI.startTask(data.id)
      setTab('tasks')
      setUploadedPhotos([])
      setCheckedAccIds({})
      await loadTasks()
      openTask(data.id)   // сразу открыли детали чтобы видеть прогресс
    } catch (err) {
      setErrorMsg(err?.response?.data?.detail || err.message)
    }
    setSubmitting(false)
  }

  const openTask = async (id) => {
    try {
      const { data } = await leomatchAPI.getTask(id)
      setSelected(data)
    } catch (e) { console.error(e) }
  }

  const doDelete = async (id, isRunning) => {
    const msg = isRunning
      ? '⚠ Задача СЕЙЧАС РАБОТАЕТ. Force-delete (работающие акки могут прерваться посреди онбординга). Продолжить?'
      : 'Удалить задачу?'
    if (!confirm(msg)) return
    try {
      await leomatchAPI.deleteTask(id)
      if (selected?.task?.id === id) setSelected(null)
      loadTasks()
    } catch (e) { alert(e?.response?.data?.detail || e.message) }
  }

  return (
    <div style={{ padding: 20, maxWidth: 1400, margin: '0 auto' }}>
      <h1 style={{ fontSize: 24, marginBottom: 16 }}>❤ Leomatch — массовый онбординг</h1>
      <p style={{ color: 'var(--text-3)', fontSize: 12, marginBottom: 16 }}>
        Регистрирует твои аккаунты в <b>@leomatchbot</b> с рандомным профилем из пула
        (фото/города/описания). Проверено: acc#210 прошёл шаги 1-10 автоматически.
      </p>

      {/* Tabs */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 16, borderBottom: '1px solid var(--border)' }}>
        {[
          { key: 'new', label: '＋ Новая задача' },
          { key: 'tasks', label: `📋 Задачи (${tasks.length})` },
        ].map(t => (
          <div key={t.key} onClick={() => setTab(t.key)}
               style={{
                 padding: '8px 14px', cursor: 'pointer', fontSize: 13, fontWeight: 700,
                 borderBottom: tab === t.key ? '2px solid var(--accent)' : '2px solid transparent',
                 color: tab === t.key ? 'var(--accent)' : 'var(--text-2)',
               }}>{t.label}</div>
        ))}
      </div>

      {/* NEW TASK TAB */}
      {tab === 'new' && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <Card style={{ padding: 14 }}>
              <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-3)', textTransform: 'uppercase', marginBottom: 8 }}>
                Аккаунты для онбординга
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 8 }}>
                <span>Выбрано: <b>{checkedIds.length}</b> из {accounts.length} (только active)</span>
                <Button variant="outline" size="sm" onClick={toggleAll}>
                  {checkedIds.length === accounts.length ? 'Снять все' : 'Выбрать все'}
                </Button>
              </div>
              <div style={{ maxHeight: 300, overflowY: 'auto', border: '1px solid var(--border)', borderRadius: 6, padding: 6 }}>
                {accounts.map(a => (
                  <label key={a.id}
                         style={{ display: 'flex', alignItems: 'center', gap: 6, padding: 4, fontSize: 12, cursor: 'pointer' }}>
                    <input type="checkbox"
                           checked={!!checkedAccIds[a.id]}
                           onChange={e => setCheckedAccIds({ ...checkedAccIds, [a.id]: e.target.checked })} />
                    <span style={{ fontWeight: 700 }}>acc#{a.id}</span>
                    <span style={{ color: 'var(--text-3)' }}>{a.phone}</span>
                    {a.first_name && <span style={{ color: 'var(--text-2)' }}>· {a.first_name}</span>}
                  </label>
                ))}
              </div>
            </Card>

            <Card style={{ padding: 14 }}>
              <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-3)', textTransform: 'uppercase', marginBottom: 8 }}>
                Фото / Видео (пул)
              </div>
              <input type="file" multiple accept="image/*,video/mp4" onChange={onFilesSelected} disabled={uploading} />
              {uploading && <span style={{ marginLeft: 8, color: 'var(--text-3)' }}>Загружается…</span>}
              <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 6 }}>
                Загружено: <b>{uploadedPhotos.length}</b> файлов. На акк выдастся <b>{photosPerAcc}</b> случайных.
              </div>
              {uploadedPhotos.length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 8 }}>
                  {uploadedPhotos.map((p, i) => (
                    <Badge key={i} tone="info">{p.split('/').pop().slice(0, 12)}…</Badge>
                  ))}
                  <Button variant="outline" size="sm" onClick={() => setUploadedPhotos([])}>
                    очистить
                  </Button>
                </div>
              )}
            </Card>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <Card style={{ padding: 14 }}>
              <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-3)', textTransform: 'uppercase', marginBottom: 8 }}>
                Пулы (по 1 в строке — рандом на акк)
              </div>
              <label style={{ fontSize: 11, color: 'var(--text-3)' }}>Города</label>
              <textarea value={cities} onChange={e => setCities(e.target.value)} rows={4}
                        style={{ width: '100%', background: 'var(--bg-3)', border: '1px solid var(--border)', color: 'var(--text-1)', borderRadius: 6, padding: 6, fontSize: 12, marginBottom: 8 }} />
              <label style={{ fontSize: 11, color: 'var(--text-3)' }}>Описания «О себе»</label>
              <textarea value={descriptions} onChange={e => setDescriptions(e.target.value)} rows={4}
                        style={{ width: '100%', background: 'var(--bg-3)', border: '1px solid var(--border)', color: 'var(--text-1)', borderRadius: 6, padding: 6, fontSize: 12, marginBottom: 8 }} />
              <label style={{ fontSize: 11, color: 'var(--text-3)' }}>Имена (опционально — иначе first_name TG-акка)</label>
              <textarea value={names} onChange={e => setNames(e.target.value)} rows={3}
                        style={{ width: '100%', background: 'var(--bg-3)', border: '1px solid var(--border)', color: 'var(--text-1)', borderRadius: 6, padding: 6, fontSize: 12 }} />
            </Card>

            <Card style={{ padding: 14 }}>
              <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-3)', textTransform: 'uppercase', marginBottom: 8 }}>Параметры</div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, fontSize: 12 }}>
                <label>Метка<br /><Input value={label} onChange={e => setLabel(e.target.value)} placeholder="batch #1" /></label>
                <label>Язык бота<br />
                  <select value={language} onChange={e => setLanguage(e.target.value)}
                          style={{ width: '100%', padding: 6, background: 'var(--bg-3)', border: '1px solid var(--border)', color: 'var(--text-1)', borderRadius: 6, fontSize: 12 }}>
                    {LANGS.map(l => <option key={l} value={l}>{l}</option>)}
                  </select>
                </label>
                <label>Возраст мин<br /><Input type="number" value={ageMin} onChange={e => setAgeMin(e.target.value)} /></label>
                <label>Возраст макс<br /><Input type="number" value={ageMax} onChange={e => setAgeMax(e.target.value)} /></label>
                <label>Пол акка<br />
                  <select value={genderMode} onChange={e => setGenderMode(e.target.value)}
                          style={{ width: '100%', padding: 6, background: 'var(--bg-3)', border: '1px solid var(--border)', color: 'var(--text-1)', borderRadius: 6, fontSize: 12 }}>
                    <option value="auto">Авто (по имени)</option>
                    <option value="male">Всем мужской</option>
                    <option value="female">Всем женский</option>
                  </select>
                </label>
                <label>Кого искать<br />
                  <select value={seek} onChange={e => setSeek(e.target.value)}
                          style={{ width: '100%', padding: 6, background: 'var(--bg-3)', border: '1px solid var(--border)', color: 'var(--text-1)', borderRadius: 6, fontSize: 12 }}>
                    <option value="male">Парней</option>
                    <option value="female">Девушек</option>
                    <option value="any">Не важно</option>
                  </select>
                </label>
                <label>Фото на акк<br /><Input type="number" min={1} max={3} value={photosPerAcc} onChange={e => setPhotosPerAcc(e.target.value)} /></label>
              </div>
            </Card>

            {errorMsg && (
              <div style={{ padding: 10, background: 'rgba(255,60,60,0.15)', border: '1px solid var(--red)', borderRadius: 6, fontSize: 12, color: 'var(--red)' }}>
                ⚠ {errorMsg}
              </div>
            )}

            <Button onClick={submit} disabled={submitting} style={{ padding: '12px 16px', fontSize: 14, fontWeight: 700 }}>
              {submitting ? 'Создание…' : `▶ Создать и запустить (${checkedIds.length} акк)`}
            </Button>
          </div>
        </div>
      )}

      {/* TASKS LIST TAB */}
      {tab === 'tasks' && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.5fr', gap: 16 }}>
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
              <span style={{ fontWeight: 700 }}>Мои задачи</span>
              <Button variant="outline" size="sm" onClick={loadTasks}>↻ Обновить</Button>
            </div>
            {loadingList ? <Spinner /> : tasks.length === 0 ? <Empty label="Нет задач" /> : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {tasks.map(t => (
                  <Card key={t.id} onClick={() => openTask(t.id)}
                        style={{ padding: 10, cursor: 'pointer',
                                 border: selected?.task?.id === t.id ? '1px solid var(--accent)' : '1px solid var(--border)' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span style={{ fontWeight: 700, fontSize: 13 }}>#{t.id} {t.label}</span>
                      <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                        {t.status === 'running' && <Spinner size={12} />}
                        <Badge tone={t.status === 'done' ? 'success' : t.status === 'running' ? 'info' : t.status === 'error' ? 'danger' : 'default'}>
                          {t.status}
                        </Badge>
                      </div>
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4 }}>
                      ✅ {t.done_items} / ❌ {t.failed_items} / всего {t.total_items}
                      {t.status === 'running' && t.total_items > 0 && (
                        <span style={{ marginLeft: 6, color: 'var(--accent)' }}>
                          · {Math.round(100 * (t.done_items + t.failed_items) / t.total_items)}%
                        </span>
                      )}
                    </div>
                    <div style={{ fontSize: 10, color: 'var(--text-3)' }}>
                      {new Date(t.created_at).toLocaleString()}
                    </div>
                  </Card>
                ))}
              </div>
            )}
          </div>

          <div>
            {!selected ? <Empty label="Выбери задачу слева" /> : (
              <Card style={{ padding: 14 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                  <div>
                    <div style={{ fontWeight: 700, display: 'flex', alignItems: 'center', gap: 6 }}>
                      #{selected.task.id} {selected.task.label}
                      {selected.task.status === 'running' && <Spinner size={14} />}
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--text-3)', display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                      <span>Статус: <b style={{ color: selected.task.status === 'running' ? 'var(--accent)' : 'inherit' }}>{selected.task.status}</b></span>
                      {selected.counts && (
                        <>
                          <span>⏳ {selected.counts.pending} pending</span>
                          <span style={{ color: 'var(--accent)' }}>▶ {selected.counts.running} running</span>
                          <span style={{ color: 'var(--green)' }}>✅ {selected.counts.done}</span>
                          <span style={{ color: 'var(--red)' }}>❌ {selected.counts.error}</span>
                        </>
                      )}
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: 6 }}>
                    <Button variant="outline" size="sm" onClick={() => openTask(selected.task.id)}>↻ Обновить</Button>
                    <Button variant="outline" size="sm"
                            title="Отправить рандомное количество лайков (5-11) от каждого акка"
                            onClick={async () => {
                              const lmin = parseInt(prompt('Мин. лайков на акк:', '5') || '5')
                              const lmax = parseInt(prompt('Макс. лайков на акк:', '11') || '11')
                              const par  = parseInt(prompt('Параллельно акков (2-4):', '3') || '3')
                              if (!confirm(`Разослать ${lmin}-${lmax} лайков от ${selected.counts?.done || 0} акков, параллельно ${par}. Продолжить?`)) return
                              try {
                                await leomatchAPI.sendLikes(selected.task.id, { likes_min: lmin, likes_max: lmax, max_parallel: par })
                                alert('Лайки запущены в фоне. Смотри логи celery_worker.console.log и итог в task.error.')
                              } catch (e) { alert(e?.response?.data?.detail || e.message) }
                            }}>
                      ❤️ Send likes
                    </Button>
                    <Button variant="outline" size="sm"
                            title="Экстренная остановка запущенного batch лайков (независимо от того что показывает панель прогресса)"
                            style={{ borderColor: 'var(--red)', color: 'var(--red)' }}
                            onClick={async () => {
                              if (!confirm('⏹ Force stop лайков ЭТОГО batch?\n\nRedis-флаг будет установлен, целери прекратит запускать новых акков в течение 3-5 сек. Уже работающие акки прервутся между свайпами.')) return
                              try {
                                await leomatchAPI.likesStop(selected.task.id)
                                alert('✅ STOP сигнал отправлен.\n\nЖди 5-10 сек — панель прогресса покажет "stopping" → "stopped". Если не изменилось за минуту — целери завис (нужен рестарт стека).')
                              } catch (e) {
                                alert('❌ Ошибка Stop: ' + (e?.response?.data?.detail || e.message))
                              }
                            }}>
                      ⏹ STOP likes
                    </Button>
                    <Button variant="outline" size="sm"
                            title="Проверить disabled анкеты и заменить фото на новую вариацию"
                            onClick={async () => {
                              if (!confirm(`Пройти по done акков (${selected.counts?.done || 0}), найти disabled ('Твоя анкета отключена'), заменить фото. Consecutive: 2 параллельно + stagger.`)) return
                              try {
                                await leomatchAPI.replacePhotos(selected.task.id)
                                alert('Замена фото запущена в фоне. Смотри логи celery_worker.console.log.')
                              } catch (e) { alert(e?.response?.data?.detail || e.message) }
                            }}>
                      🔄 Заменить фото
                    </Button>
                    <Button variant="outline" size="sm"
                            title="Убрать ограничения приватности для всех done акков"
                            onClick={async () => {
                              if (!confirm(`Пройти по всем done акков (${selected.counts?.done || 0}) и убрать Privacy → Forwards? Займёт ~${Math.ceil((selected.counts?.done || 0) / 4 * 3)}с`)) return
                              try {
                                await leomatchAPI.fixPrivacy(selected.task.id)
                                alert('Fix privacy запущен в фоне, целери работает')
                              } catch (e) { alert(e?.response?.data?.detail || e.message) }
                            }}>
                      🔓 Fix privacy
                    </Button>
                    <Button variant="outline" size="sm"
                            onClick={() => doDelete(selected.task.id, selected.task.status === 'running')}>
                      🗑 {selected.task.status === 'running' ? 'Force delete' : 'Удалить'}
                    </Button>
                  </div>
                </div>
                <div style={{ fontSize: 11, color: 'var(--text-3)', marginBottom: 10 }}>
                  {selected.config.cities.length} городов · {selected.config.descriptions.length} описаний · {selected.config.photos_count} фото · seek={selected.config.seek} · age {selected.config.age_min}-{selected.config.age_max}
                </div>

                {/* Likes progress panel (Redis-backed, живёт даже после F5) */}
                {likesProgress && likesProgress.status && likesProgress.status !== 'idle' && (
                  <Card style={{ padding: 12, marginBottom: 10, border: `1px solid ${likesProgress.status === 'running' ? 'var(--accent)' : 'var(--border)'}` }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                      <span style={{ fontWeight: 700, display: 'flex', alignItems: 'center', gap: 6 }}>
                        ❤️ Лайки
                        <Badge tone={
                          likesProgress.status === 'running' ? 'info' :
                          likesProgress.status === 'done' ? 'success' :
                          likesProgress.status === 'stopping' ? 'warning' :
                          likesProgress.status === 'stopped' ? 'danger' : 'default'
                        }>{likesProgress.status}</Badge>
                        {likesProgress.status === 'running' && <Spinner size={12} />}
                      </span>
                      {(likesProgress.status === 'running' || likesProgress.status === 'stopping') && (
                        <Button variant="outline" size="sm"
                                onClick={async () => {
                                  if (!confirm('Force stop лайков? Уже работающие акки доделают свою партию.')) return
                                  try {
                                    await leomatchAPI.likesStop(selected.task.id)
                                  } catch (e) { alert(e?.response?.data?.detail || e.message) }
                                }}>
                          ⏹ Stop
                        </Button>
                      )}
                    </div>
                    <div style={{ display: 'flex', gap: 12, fontSize: 12, flexWrap: 'wrap', marginBottom: 8 }}>
                      <span>✅ <b>{likesProgress.accounts_done || 0}</b>/{likesProgress.accounts_total || 0} акков</span>
                      <span style={{ color: 'var(--accent)' }}>▶ {likesProgress.in_flight || 0} сейчас работают</span>
                      <span>⏳ {likesProgress.remaining || 0} в очереди</span>
                      <span style={{ color: 'var(--green)' }}>❤️ <b>{likesProgress.total_likes || 0}</b> лайков</span>
                      <span style={{ color: 'var(--text-3)' }}>👎 <b>{likesProgress.total_dislikes || 0}</b> дизлайков</span>
                      <span style={{ color: 'var(--pink, #ff3d9a)' }}>💘 <b>{likesProgress.total_matches || 0}</b> matches</span>
                      <span style={{ color: 'var(--red)' }}>❌ <b>{likesProgress.fail || 0}</b> fail</span>
                    </div>
                    {likesProgress.accounts_total > 0 && (
                      <div style={{ height: 6, background: 'var(--bg-3)', borderRadius: 3, overflow: 'hidden', marginBottom: 8 }}>
                        <div style={{
                          height: '100%',
                          width: `${Math.min(100, Math.round(100 * likesProgress.accounts_done / likesProgress.accounts_total))}%`,
                          background: 'var(--accent)', transition: 'width 0.5s',
                        }} />
                      </div>
                    )}
                    {likesProgress.last_events?.length > 0 && (
                      <details>
                        <summary style={{ cursor: 'pointer', fontSize: 11, color: 'var(--text-3)' }}>
                          Последние события ({likesProgress.last_events.length})
                        </summary>
                        <div style={{ fontFamily: 'monospace', fontSize: 10, marginTop: 4, color: 'var(--text-2)', maxHeight: 180, overflowY: 'auto' }}>
                          {likesProgress.last_events.slice().reverse().map((e, i) => (
                            <div key={i} style={{ color: e.includes('ERR') ? 'var(--red)' : undefined }}>{e}</div>
                          ))}
                        </div>
                      </details>
                    )}
                    {likesProgress.errs?.length > 0 && (
                      <details style={{ marginTop: 4 }}>
                        <summary style={{ cursor: 'pointer', fontSize: 11, color: 'var(--red)' }}>
                          Ошибки ({likesProgress.errs.length})
                        </summary>
                        <div style={{ fontFamily: 'monospace', fontSize: 10, marginTop: 4, color: 'var(--red)', maxHeight: 180, overflowY: 'auto' }}>
                          {likesProgress.errs.map((e, i) => <div key={i}>{e}</div>)}
                        </div>
                      </details>
                    )}
                  </Card>
                )}
                <div style={{ maxHeight: 500, overflowY: 'auto' }}>
                  {selected.items.map(it => (
                    <div key={it.id} style={{ padding: 8, borderTop: '1px solid var(--border)', fontSize: 12 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <span>
                          <b>acc#{it.account_id}</b> {it.phone && <span style={{ color: 'var(--text-3)' }}>{it.phone}</span>}
                          {it.first_name && <span style={{ color: 'var(--text-2)' }}> · {it.first_name}</span>}
                        </span>
                        <Badge tone={it.status === 'done' ? 'success' : it.status === 'running' ? 'info' : it.status === 'error' ? 'danger' : 'default'}>
                          {it.status}
                        </Badge>
                      </div>
                      <div style={{ fontSize: 10, color: 'var(--text-3)', marginTop: 2 }}>
                        Профиль: {it.profile?.own_gender}, {it.profile?.age} лет · {it.profile?.city} · «{(it.profile?.description || '').slice(0, 40)}»
                      </div>
                      {it.error && <div style={{ fontSize: 10, color: 'var(--red)' }}>❌ {it.error}</div>}
                      {it.steps_log?.length > 0 && (
                        <details style={{ marginTop: 4 }}>
                          <summary style={{ cursor: 'pointer', fontSize: 10, color: 'var(--text-3)' }}>
                            Шаги ({it.steps_log.length})
                          </summary>
                          <div style={{ fontFamily: 'monospace', fontSize: 10, marginTop: 4, color: 'var(--text-2)' }}>
                            {it.steps_log.map((s, i) => (
                              <div key={i}>#{s.step} {s.action} {s.note ? `⚠ ${s.note}` : ''}</div>
                            ))}
                          </div>
                        </details>
                      )}
                    </div>
                  ))}
                </div>
              </Card>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
