"""
tasks/quarantine_probe.py — проверка каналов силами карантинных аккаунтов.

Зачем именно они: аккаунт с ограничением Telegram не может писать в
супергруппы, но резолвит каналы и читает их свойства как обычный. Проверено
26.08 на acc#55 и acc#90 — подписка и чтение проходят, отказ приходит только
на отправке. Значит 62 простаивающих аккаунта могут закрывать проверку, не
тратя суточные подключения рабочего парка.

Что даёт один канал: два read-only запроса — get_entity и GetFullChannel —
возвращают И число участников, И наличие обсуждений. То есть размер канала
выясняется бесплатно, обращаться за ним к TGStat не нужно.

Предохранители, без которых это опасно:

  * ПОДРЯД-ОТКАЗЫ. Если аккаунт получает MAX_CONSECUTIVE_FAILS отказов
    подряд — он останавливается. Обычно это значит, что упёрлись в лимит
    или сессия развалилась, и продолжать нельзя.

  * ДОЛЯ ОТКАЗОВ. Отдельно считается доля неудач за весь прогон аккаунта.
    Пробитый порог тоже останавливает: подряд-отказов может не быть, а
    каждая вторая проверка падать.

  * FLOOD_WAIT не списывается на канал. В августе на этом накопилось 355
    ложных отметок «плохой канал» — виноват был не канал, а перебор
    запросов. Флуд поднимается к account_health и обрывает работу аккаунта.
"""

import asyncio
import os
import random
import sys
import logging
from datetime import datetime

from celery_app import celery_app

logger = logging.getLogger(__name__)
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Сколько каналов проверяет один аккаунт за прогон
PER_ACCOUNT = int(os.getenv("QPROBE_PER_ACCOUNT", "60"))

# ── Бюджет запросов в час ──────────────────────────────────────────
# Ограничение Telegram на resolve/getFullChannel — не «столько-то за подход»,
# а квота в окне времени. Пауза между волнами её не обнуляет, поэтому одного
# лимита на партию мало: аккаунт отработает 45 каналов, отдохнёт 25 минут и
# продолжит выбирать ту же квоту.
# Потолок взят из замера 27.08: в волне по 120 каналов шестнадцать аккаунтов
# из двадцати восьми оборвались на FLOOD_WAIT, в среднем на 57-м канале.
# Берём с запасом вдвое.
HOUR_BUDGET = int(os.getenv("QPROBE_HOUR_BUDGET", "30"))
_HOUR_KEY = "gramgpt:qprobe_hour:{acc}"


def _hour_spent(account_id: int) -> int:
    """Сколько каналов аккаунт уже проверил в текущем часовом окне."""
    try:
        from utils.redis_pool import get_redis
        v = get_redis().get(_HOUR_KEY.format(acc=account_id))
        return int(v) if v else 0
    except Exception:
        return 0


def _hour_spend(account_id: int, n: int = 1):
    """Отмечает израсходованные запросы. Окно фиксированное: заводится
    первым запросом и живёт час, отдельная чистка не нужна."""
    try:
        from utils.redis_pool import get_redis
        r = get_redis()
        k = _HOUR_KEY.format(acc=account_id)
        cur = r.incr(k, n)
        if int(cur) <= n:
            r.expire(k, 3600)
    except Exception:
        pass
# Паузы между каналами — темп живого пользователя, а не сканера
DELAY_MIN = float(os.getenv("QPROBE_DELAY_MIN", "18"))
DELAY_MAX = float(os.getenv("QPROBE_DELAY_MAX", "40"))
# Столько отказов ПОДРЯД — стоп
MAX_CONSECUTIVE_FAILS = int(os.getenv("QPROBE_MAX_CONSEC_FAILS", "5"))
# Такая доля отказов (после MIN_FOR_RATE проверок) — стоп
MAX_FAIL_RATE = float(os.getenv("QPROBE_MAX_FAIL_RATE", "0.5"))
MIN_FOR_RATE = int(os.getenv("QPROBE_MIN_FOR_RATE", "10"))
# За сколько минут до сессии кампании освобождать ей прокси. Должно быть
# не меньше длительности прогона: 150 каналов по 18-40с — это до 50 минут.
# Запас, который оставляем кампании перед её сессией.
QPROBE_GAP_MIN = int(os.getenv("QPROBE_GAP_MIN", "8"))
# Меньше этого числа каналов браться не стоит: подключение дороже работы.
QPROBE_MIN_BATCH = int(os.getenv("QPROBE_MIN_BATCH", "8"))


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()



async def _free_window_min(db, proxy_id: int) -> float:
    """Сколько минут прокси свободен до ближайшей сессии кампании.

    Кампания работает по расписанию и ждать не может: отложенная сессия
    съезжает на 10-12 минут, а просроченная больше чем на 4 часа
    отбрасывается совсем. Проверка каналов — фоновая, её подвинуть ничего
    не стоит. Поэтому проверка не «захватывает и держит», а **встраивается
    в промежутки** между сессиями: сколько минут свободно, столько каналов
    и берёт.

    Возвращает минуты до ближайшей сессии; если сегодня сессий на этом
    прокси больше нет — время до конца суток.

    Времена сессий лежат внутри JSON плана, в местном поясе (UTC+3) — том
    же, в котором их читает диспетчер.
    """
    import json as _json
    from datetime import datetime as _dt
    from sqlalchemy import text as _text
    try:
        rows = (await db.execute(_text("""
            SELECT cp.plan, cp.executed_idx
            FROM campaign_plans cp
            JOIN accounts a ON a.id = cp.account_id
            JOIN campaigns c ON c.id = cp.campaign_id
            WHERE a.proxy_id = :pid
              AND cp.plan_date = CURRENT_DATE
              AND c.status = 'active'
        """), {"pid": proxy_id})).all()
    except Exception as e:
        # Не знаем расписание — значит не знаем, свободен ли адрес.
        # Возвращаем ноль: лучше пропустить прогон, чем занять прокси,
        # который через минуту понадобится кампании.
        logger.warning(f"[qprobe] расписание прокси #{proxy_id} не прочиталось: "
                       f"{type(e).__name__}: {e}")
        return 0.0

    now = _dt.now()
    cur = now.hour * 60 + now.minute
    best = None
    for plan_json, done_idx in rows:
        pl = _json.loads(plan_json) if isinstance(plan_json, str) else plan_json
        sessions = [x for x in (pl.get("sessions") or []) if not x.get("skipped")]
        for i, sess in enumerate(sessions):
            if i < (done_idx or 0):
                continue
            h, m = sess.get("connect_at_hour"), sess.get("connect_at_minute")
            if h is None:
                continue
            delta = (h * 60 + m) - cur
            if delta >= 0 and (best is None or delta < best):
                best = delta
    # Сессий больше нет — свободны до конца суток
    return float(best if best is not None else max(0, 24 * 60 - cur))


async def _probe_with_account(account_id: int, limit: int = None,
                              shard: int = 0, shards: int = 1,
                              folder: str = None) -> dict:
    """folder — брать только каналы из этой папки. Нужно, когда интересует
    конкретная тема и ждать общей очереди из десятков тысяч не хочется."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy import select, text
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as Session
    from utils.telegram import make_telethon_client
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.account_health import is_resting, record_error, flood_remaining
    from utils.ip_throttle import acquire_ip_lock, refresh_ip_lock, release_ip_lock
    from utils.channel_caps import extract_caps_from_full, record_caps
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.errors import FloodWaitError

    limit = limit or PER_ACCOUNT
    checked = bad = fails = 0
    consec = 0
    stop_reason = None

    # ── Отдых отдыху рознь ────────────────────────────────────────
    # Ограничение от Telegram («аккаунт ограничен») запрещает ПИСАТЬ, но
    # читать и резолвить каналы не мешает — проверено 26.08 на acc#55 и
    # acc#90. Для проверки свойств канала запись не нужна, поэтому такой
    # отдых работе не препятствует, в этом и смысл использовать простаивающие
    # аккаунты.
    # А вот FLOOD_WAIT и PEER_FLOOD означают «не трогай Telegram вообще» —
    # там любой запрос углубляет наказание, и работать нельзя.
    # Флуд проверяем ОТДЕЛЬНОЙ отметкой, а не по тексту причины отдыха.
    # У карантинного аккаунта уже висит многосуточный отдых «@SpamBot:
    # аккаунт ограничен», и set_rest его не укорачивает — поэтому свежий
    # FLOOD_WAIT в причине не появляется. 27.08 на этом 16 аккаунтов из 28
    # продолжили работать под флудом и получили паузы до 5,7 часов.
    _fl = flood_remaining(account_id)
    if _fl > 0:
        return {"status": "flooded", "wait_sec": _fl}

    resting, why = is_resting(account_id)
    if resting:
        _w = (why or "").upper()
        if ("FLOOD" in _w) or ("PEER" in _w):
            return {"status": "resting", "reason": why}
        logger.info(f"[qprobe] acc#{account_id}: отдых «{why[:40]}» "
                    f"чтению не мешает — работаем")

    async with Session() as db:
        acc = (await db.execute(
            select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
            .where(TelegramAccount.id == account_id)
        )).scalar_one_or_none()
        if not acc:
            return {"status": "no_account"}
        proxy = (await db.execute(
            select(Proxy).where(Proxy.id == acc.proxy_id))).scalar_one_or_none()
        if not proxy:
            return {"status": "no_proxy"}
        # ── Уступаем прокси кампании ──────────────────────────────
        # Проверка держит адрес 25-40 минут. Если кампании он понадобится
        # раньше, её сессия будет отложена, а то и потеряна — она идёт по
        # расписанию. Фоновую проверку сдвинуть дешевле: этот аккаунт
        # просто пропускаем, каналы никуда не денутся.
        window = await _free_window_min(db, acc.proxy_id)
        # Оставляем кампании запас: заканчиваем за QPROBE_GAP_MIN до её сессии,
        # чтобы освободить адрес заранее, а не впритык.
        usable = window - QPROBE_GAP_MIN
        avg_delay = (DELAY_MIN + DELAY_MAX) / 2 / 60.0      # минут на канал
        fits = int(usable / avg_delay) if usable > 0 else 0
        if fits < QPROBE_MIN_BATCH:
            logger.info(f"[qprobe] acc#{account_id}: окно на {proxy.host} "
                        f"{window:.0f} мин — мало, пропускаю")
            return {"status": "window_too_small", "window_min": round(window)}
        spent = _hour_spent(account_id)
        budget = HOUR_BUDGET - spent
        if budget < QPROBE_MIN_BATCH:
            # Замки ещё не взяты — освобождать нечего. Раньше здесь стояли
            # release_*, и они снимали чужой замок на этом же адресе.
            logger.info(f"[qprobe] acc#{account_id}: за час уже {spent} проверок "
                        f"из {HOUR_BUDGET} — пропускаю, чтобы не словить флуд")
            return {"status": "hour_budget", "spent": spent}

        limit = min(limit, fits, budget)
        logger.info(f"[qprobe] acc#{account_id}: окно {window:.0f} мин на "
                    f"{proxy.host}, бюджет часа {budget} → берём {limit} каналов")

        # owner=account_id — представляемся замку. Без имени он пишет
        # адрес на «никого» и держит его десять минут, выталкивая
        # настоящего владельца: ровно это ломало подписки и проверки
        # через @SpamBot, пока не добавили owner.
        if not acquire_ip_lock(proxy, owner=account_id):
            return {"status": "ip_cooldown"}
        if not acquire_account_lock(account_id, ttl=3600):
            return {"status": "locked"}

        # Берём непроверенные, начиная с самых крупных: у них выше
        # и охват, и шанс, что обсуждения открыты.
        #
        # ВАЖНО — срез свой у каждого аккаунта. Без этого все параллельные
        # исполнители получают ОДИН И ТОТ ЖЕ список (запрос-то одинаковый),
        # проверяют одни каналы по нескольку раз и дерутся за уникальный
        # ключ в channel_capabilities. Раскладываем по остатку от деления
        # хэша имени: аккаунт берёт только «свою» полосу, пересечений нет
        # и никакой координации между воркерами не требуется.
        # ── Сначала перепроверка подозрительных ────────────────────
        # Канал, отдавший аккаунту ChannelPrivateError, остаётся помечен
        # пригодным: mark_bad хоронит такие только после подтверждения от
        # двух РАЗНЫХ аккаунтов, иначе личный бан одного закрыл бы канал
        # для всех. Само правило верное, но ждать второго совпадения можно
        # неделями, а канал каждый день сжигает слот заново. Замер 27.08:
        # 38 каналов дали эту ошибку, помечен не был НИ ОДИН.
        # Поэтому подтверждение добываем сами: сюда приходит другой аккаунт
        # и проверяет заново. Не сможет и он — record_caps запишет отказ.
        recheck = [r[0] for r in (await db.execute(text("""
            SELECT cc.channel_username
            FROM channel_capabilities cc
            WHERE cc.commentable = true
              AND COALESCE(cc.fail_count, 0) >= 1
              AND cc.last_error IS NOT NULL
              AND (cc.checked_by_account IS NULL OR cc.checked_by_account <> :acc)
              AND (:shards <= 1 OR mod(abs(hashtext(cc.channel_username)), :shards) = :shard)
            ORDER BY cc.fail_count DESC, cc.checked_at ASC
            LIMIT :lim
        """), {"lim": max(1, limit // 3), "acc": account_id,
               "shards": shards, "shard": shard})).all()]
        if recheck:
            logger.info(f"[qprobe] acc#{account_id}: {len(recheck)} каналов на перепроверку")

        rows = (await db.execute(text("""
            SELECT pc.username
            FROM parsed_channels pc
            LEFT JOIN channel_capabilities cc ON cc.channel_username = pc.username
            WHERE pc.username IS NOT NULL AND pc.username <> ''
              AND pc.kind = 'channel'
              AND cc.id IS NULL
              -- Карты сайта TGStat исключены. Два независимых замера 26.08
              -- (40 и 42 случайных канала): МЁРТВЫХ 48-50%, пригодных 10%,
              -- медианный размер 688-858 подписчиков, каналов от 100k — ноль.
              -- Для сравнения, записи из рейтингов: мёртвых 0.1%,
              -- пригодных 39%. Проверять их — четыре дня работы парка ради
              -- ~1900 каналов по несколько сотен подписчиков.
              AND COALESCE(pc.search_query, '') <> 'tgstat_sitemap'
              AND (CAST(:folder AS VARCHAR) IS NULL OR pc.folder = CAST(:folder AS VARCHAR))
              AND (:shards <= 1 OR mod(abs(hashtext(pc.username)), :shards) = :shard)
            ORDER BY pc.subscribers DESC NULLS LAST
            LIMIT :lim
        """), {"lim": limit, "shards": shards, "shard": shard,
               "folder": folder})).all()
        targets = recheck + [r[0] for r in rows if r[0] not in set(recheck)]
        targets = targets[:limit]
        if not targets:
            release_account_lock(account_id)
            return {"status": "nothing_to_probe"}

        client = make_telethon_client(acc, proxy)
        if not client:
            release_account_lock(account_id)
            return {"status": "no_client"}

        try:
            await client.connect()
            if not await client.is_user_authorized():
                return {"status": "not_authorized"}
            logger.info(f"[qprobe] acc#{account_id}: {len(targets)} каналов")

            for uname in targets:
                try:
                    ent = await client.get_entity(uname)
                    full = await client(GetFullChannelRequest(ent))
                    caps = extract_caps_from_full(full, None)
                    res = await record_caps(db, uname, caps, account_id=account_id,
                                            source="qprobe")
                    # размер канала приходит тем же ответом — записываем
                    members = getattr(full.full_chat, "participants_count", None)
                    if members:
                        # Пишем БЕЗУСЛОВНО. Раньше стояло «только если
                        # больше» — и завышенное число уже не исправлялось
                        # никогда: у @Walking_Dead_TV с разбора карточки
                        # осталось 1,1 млн вместо настоящих 18 тысяч.
                        # Значение приходит прямо из Telegram, это истина,
                        # а не кандидат на повышение.
                        await db.execute(text("""
                            UPDATE parsed_channels SET subscribers = :m
                            WHERE username = :u
                        """), {"m": int(members), "u": uname})
                    await db.commit()
                    # Держим старый флаг has_comments в согласии с вердиктом.
                    # Вкладка «с комментариями» фильтрует именно по нему, и без
                    # этой строки свежепроверенные пригодные каналы туда не
                    # попадали: вердикт лежал в кэше свойств, а флаг оставался
                    # false с момента загрузки. Замер 26.08: так «потерялось»
                    # 1480 пригодных каналов.
                    if res and res.get("commentable") is not None:
                        await db.execute(text(
                            "UPDATE parsed_channels SET has_comments = :hc WHERE username = :u AND has_comments IS DISTINCT FROM :hc"
                        ), {"hc": bool(res["commentable"]), "u": uname})
                    checked += 1
                    consec = 0
                    _hour_spend(account_id)
                    if res and res.get("commentable") is False:
                        bad += 1
                except FloodWaitError as fw:
                    # Претензия к аккаунту, не к каналу.
                    # Не пережидаем: FLOOD_WAIT здесь приходит на часы
                    # (27.08 — до 5,7 ч), спать столько внутри воркера
                    # нельзя. Останавливаем аккаунт, каналы никуда не денутся.
                    record_error(account_id, f"FloodWait {fw.seconds}", fw)
                    stop_reason = f"flood_{fw.seconds}"
                    logger.warning(f"[qprobe] acc#{account_id}: FLOOD_WAIT {fw.seconds}с — стоп")
                    # ── Пишем флуд в warmup_logs ──────────────────────
                    # Иначе его не видно нигде, кроме строчки в логе воркера:
                    # проба в журнал не писала вовсе, и отчёт «флудов за
                    # сутки» по warmup_logs честно показывал ноль, хотя
                    # 26.08 флудили семь аккаунтов, а 27.08 — шестнадцать.
                    try:
                        from models.warmup_log import WarmupLog as _WL
                        db.add(_WL(
                            task_id=None, account_id=account_id, action="error",
                            detail=f"FLOOD_WAIT {fw.seconds}с на проверке каналов",
                            channel=uname or "", success=False,
                            error=f"flood_wait_{fw.seconds}"[:64],
                            error_origin="telegram", source="probe",
                        ))
                        await db.commit()
                    except Exception:
                        await db.rollback()
                    break
                except Exception as e:
                    fails += 1
                    consec += 1
                    _hour_spend(account_id)   # отказ тоже стоил запроса
                    logger.warning(f"[qprobe] acc#{account_id} @{uname}: "
                                   f"{type(e).__name__}: {str(e)[:140]}")
                    # Ошибку доступа записываем как есть. Раньше сюда шло
                    # has_discussion=False, и канал, который просто закрылся,
                    # оседал в базе с причиной «нет обсуждений».
                    _en = type(e).__name__
                    _hard = _en in ("ChannelPrivateError", "UsernameNotOccupiedError",
                                    "UsernameInvalidError", "ChannelInvalidError",
                                    "InviteHashExpiredError")
                    await record_caps(db, uname, {"has_discussion": False},
                                      account_id=account_id, source="qprobe",
                                      error=_en,
                                      reason_override=(_en if _hard else None))
                    await db.commit()
                    if consec >= MAX_CONSECUTIVE_FAILS:
                        stop_reason = f"{consec} отказов подряд"
                        logger.warning(f"[qprobe] acc#{account_id}: {stop_reason} — стоп")
                        break
                    done = checked + fails
                    if done >= MIN_FOR_RATE and fails / done > MAX_FAIL_RATE:
                        stop_reason = f"доля отказов {fails}/{done}"
                        logger.warning(f"[qprobe] acc#{account_id}: {stop_reason} — стоп")
                        break
                # ── Удерживаем IP за собой ────────────────────────────
                # Замок ставится на 10 минут, а прогон идёт 25-40. Без
                # продления он истечёт посреди работы, и кампания подключится
                # на тот же адрес параллельно — два одновременных соединения
                # с одного IP, ровно то, от чего защищаемся. 26.08 замерено:
                # 54 карантинных аккаунта из 63 сидят на прокси кампании,
                # так что пересечение почти неизбежно.
                if (checked + fails) % 10 == 0:
                    refresh_ip_lock(proxy)
                await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            release_account_lock(account_id)
            # Возвращаем адрес сразу: иначе кампания зря прождёт остаток
            # десятиминутного окна.
            release_ip_lock(proxy)

    return {"status": "ok", "checked": checked, "not_commentable": bad,
            "fails": fails, "stopped": stop_reason}


@celery_app.task(bind=True, name="tasks.quarantine_probe.probe_with_account",
                 time_limit=3 * 3600, soft_time_limit=int(2.9 * 3600))
def probe_with_account(self, account_id: int, limit: int = None,
                       shard: int = 0, shards: int = 1):
    """Проверка пачки каналов одним карантинным аккаунтом."""
    return run_async(_probe_with_account(account_id, limit, shard, shards))
