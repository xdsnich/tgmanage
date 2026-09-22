"""
GramGPT — tasks/bulk_channel_check.py
Массовая проверка каналов на пригодность к комментированию.

Схема работы (по требованию 2026-08-24):
  * внутри ОДНОГО прокси аккаунты идут ПОСЛЕДОВАТЕЛЬНО, с паузой между ними —
    два аккаунта с одного IP никогда не подключаются одновременно;
  * РАЗНЫЕ прокси работают ПАРАЛЛЕЛЬНО — по одному воркеру на IP;
  * внутри аккаунта пауза между каналами, и жёсткий суточный потолок резолвов.

Почему потолок нужен. Каждая проверка начинается с резолва юзернейма
(get_entity), а у ResolveUsername свой лимит. Замер 23.08: acc#210 шёл со
скоростью 49 проверок в час и словил FLOOD_WAIT на 5 часов, а аккаунты,
работавшие по 8-33 в час, не пострадали — при том же суточном объёме.
Значит ограничивать надо ТЕМП, а не только количество.

Экономия резолвов: для каналов, уже знакомых сессии аккаунта,
get_input_entity берёт peer из локального кэша и в сеть не ходит вовсе.
Пробуем сначала его, и только при промахе тратим резолв.

ENV:
  BULKCHK_PER_ACCOUNT_DAY   сколько каналов максимум на аккаунт в сутки (120)
  BULKCHK_DELAY_MIN/MAX     пауза между каналами внутри аккаунта, сек (120/240)
  BULKCHK_ACCOUNT_GAP       пауза между аккаунтами одного прокси, сек (300)
  BULKCHK_MAX_PROXIES       сколько прокси обрабатывать параллельно (0 = все)
  BULKCHK_MIN_SUBS          не проверять каналы мельче (0 = проверять все)
  BULKCHK_STATUSES          статусы аккаунтов-проверяющих (quarantine)
"""

import asyncio
import os
import random
import sys
import logging
from datetime import date, datetime

from celery_app import celery_app

logger = logging.getLogger(__name__)
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

PER_ACCOUNT_DAY = int(os.getenv("BULKCHK_PER_ACCOUNT_DAY", "120"))
# Пауза между каналами. При 25-45 секундах выходило около сотни
# проверок в час — втрое выше измеренного безопасного коридора
# (8-33 в час выживали, 49 в час дали FLOOD_WAIT). Ставим 120-240
# секунд: это 15-30 в час, внутри коридора с запасом.
DELAY_MIN = float(os.getenv("BULKCHK_DELAY_MIN", "120"))
DELAY_MAX = float(os.getenv("BULKCHK_DELAY_MAX", "240"))
ACCOUNT_GAP = float(os.getenv("BULKCHK_ACCOUNT_GAP", "300"))
MAX_PROXIES = int(os.getenv("BULKCHK_MAX_PROXIES", "0"))
# Ниже этого порога каналы не проверяем вовсе: проверка стоит
# резолва, а он у аккаунта дефицитный ресурс.
MIN_SUBS = int(os.getenv("BULKCHK_MIN_SUBS", "0"))
# Чьими аккаунтами проверяем. Список нужен в двух местах —
# при отборе и при открытии аккаунта, — поэтому он общий.
STATUSES = [x.strip() for x in
            os.getenv("BULKCHK_STATUSES", "quarantine").split(",")
            if x.strip()]

PROGRESS_KEY = "gramgpt:bulkchk:progress"
STOP_KEY = "gramgpt:bulkchk:stop"
QUOTA_KEY = "gramgpt:bulkchk:quota:{acc}:{day}"


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _quota_left(acc_id: int) -> int:
    """Сколько проверок аккаунту ещё можно сделать сегодня."""
    try:
        from utils.redis_pool import get_redis
        r = get_redis()
        used = int(r.get(QUOTA_KEY.format(acc=acc_id, day=date.today().isoformat())) or 0)
        return max(0, PER_ACCOUNT_DAY - used)
    except Exception:
        return PER_ACCOUNT_DAY


def _quota_use(acc_id: int, n: int = 1):
    try:
        from utils.redis_pool import get_redis
        r = get_redis()
        k = QUOTA_KEY.format(acc=acc_id, day=date.today().isoformat())
        r.incrby(k, n)
        r.expire(k, 172800)
    except Exception:
        pass


def _stopped() -> bool:
    try:
        from utils.redis_pool import get_redis
        return bool(get_redis().get(STOP_KEY))
    except Exception:
        return False


async def _publish(state: dict):
    try:
        import json
        from utils.redis_pool import get_redis
        get_redis().setex(PROGRESS_KEY, 3600, json.dumps(state, ensure_ascii=False))
    except Exception:
        pass


async def _take_channels(db, limit: int) -> list[str]:
    """Забирает пачку непроверенных каналов.

    Порядок приоритета (по запросу 2026-08-24):
      1. has_comments=true — каналы, которые в базе ЧИСЛЯТСЯ как «с
         комментариями». Именно их надо перепроверить в первую очередь:
         флаг говорит лишь о наличии обсуждений и врёт почти в четверти
         случаев. Замер по 1388 уже проверенным из этой группы:
             1064 ok, 175 needs_approval, 93 no_discussion,
               29 write_banned, 27 username_not_found
         То есть 23% помечены «с комментариями», а писать туда нельзя.
      2. Внутри группы — крупные каналы вперёд.
    """
    from sqlalchemy import text
    rows = (await db.execute(text("""
        SELECT pc.username FROM parsed_channels pc
        LEFT JOIN channel_capabilities cc ON cc.channel_username = pc.username
        WHERE cc.id IS NULL AND pc.username IS NOT NULL AND pc.username <> ''
          AND COALESCE(pc.subscribers, 0) >= :minsubs
        -- Сначала крупные. Раньше первым шёл has_comments, и наверх
        -- всплывала мелочь из sitemap (21 тысяча, медиана 688 подписчиков)
        -- просто потому, что у неё стоял флаг обсуждения. Проверка — самый
        -- дорогой ресурс, тратить её надо на то, что пойдёт в работу.
        ORDER BY pc.subscribers DESC NULLS LAST, pc.has_comments DESC
        LIMIT :lim
    """), {"lim": limit, "minsubs": MIN_SUBS})).all()
    return [r[0] for r in rows]


async def _check_one(client, db, uname: str, acc_id: int) -> str:
    """Одна проверка. Возвращает reason либо 'flood'/'skip'.

    Сначала пробуем взять peer из локального кэша сессии (get_input_entity) —
    для знакомых каналов это НЕ тратит резолв. Только при промахе идём в сеть.
    """
    from telethon.errors import FloodWaitError
    from telethon.tl.functions.channels import GetFullChannelRequest
    from utils.channel_caps import extract_caps_from_full, record_caps, verdict, mark_bad

    peer = None
    used_resolve = False
    try:
        peer = await client.get_input_entity(uname)      # кэш сессии, без сети
    except Exception:
        peer = None

    try:
        if peer is None:
            peer = await client.get_entity(uname)         # резолв, тратит квоту
            used_resolve = True
        full = await client(GetFullChannelRequest(channel=peer))
    except FloodWaitError:
        raise
    except Exception as e:
        et = type(e).__name__
        if et in ("UsernameNotOccupiedError", "UsernameInvalidError", "ValueError"):
            await mark_bad(db, uname, reason="username_not_found",
                           account_id=acc_id, error=str(e))
            return "username_not_found"
        if et == "ChannelPrivateError":
            await mark_bad(db, uname, reason="channelprivate",
                           account_id=acc_id, error=str(e))
            return "channelprivate"
        logger.debug(f"[bulkchk] @{uname}: {et} — канал НЕ помечаем")
        return "skip"

    caps = extract_caps_from_full(full, peer)
    await record_caps(db, uname, caps, account_id=acc_id, source="bulk_check")
    ok, why = verdict(caps)
    # ── Считаем КАЖДУЮ проверку, а не только сетевой резолв ──────────
    # Раньше квота росла лишь когда peer не нашёлся в кэше. В ночь на
    # 08.09 все 5768 проверок прошли из кэша — счётчик не сдвинулся ни
    # разу, аккаунты пробили свои 120 без остановки и сделали по 185
    # штук со скоростью 90 в час. Итог: 17 аккаунтов словили FLOOD_WAIT
    # по 22 часа.
    # Кэш экономит резолв, но GetFullChannelRequest всё равно уходит в
    # сеть и имеет свой лимит — вот его и не учитывали.
    _quota_use(acc_id, 1)
    return why


async def _worker_for_proxy(proxy_host: str, accounts: list, queue: asyncio.Queue,
                            state: dict, lock: asyncio.Lock):
    """Один воркер = один физический IP. Аккаунты внутри идут по очереди."""
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as Session
    from utils.telegram import make_telethon_client
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.ip_throttle import acquire_ip_lock
    from utils.account_health import is_resting, record_error
    from telethon.errors import FloodWaitError

    for idx, acc_id in enumerate(accounts):
        if _stopped() or queue.empty():
            return
        left = _quota_left(acc_id)
        if left <= 0:
            logger.info(f"[bulkchk] acc#{acc_id}: суточная квота выбрана")
            continue
        resting, why = is_resting(acc_id)
        if resting:
            logger.info(f"[bulkchk] acc#{acc_id}: отдыхает ({why})")
            continue
        if not acquire_account_lock(acc_id, ttl=3600):
            continue


        async with Session() as db:
            acc = (await db.execute(
                select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
                .where(TelegramAccount.id == acc_id)
            )).scalar_one_or_none()
            # Тот же список статусов, что и при отборе выше. Раньше здесь
            # жёстко стояло ("active", "unknown"): после перевода проверки
            # на карантинные аккаунты отбор давал их, а этот фильтр тут же
            # отбрасывал — задача завершалась с нулём проверок.
            if not acc or str(acc.status.value if hasattr(acc.status, "value")
                              else acc.status) not in STATUSES:
                release_account_lock(acc_id); continue
            proxy = (await db.execute(
                select(Proxy).where(Proxy.id == acc.proxy_id))).scalar_one_or_none()
            # ── Замок адреса ─────────────────────────────────────────
            # Проверка идёт карантинными аккаунтами, но прокси у них общие
            # с рабочими. Без этого замка проверка подключалась на адресе,
            # где в тот же момент шла сессия кампании, — а плановый движок
            # и realtime-очередь замок берут давно.
            # Не отпускаем: ключ живёт своим TTL как остывание адреса.
            if proxy and not acquire_ip_lock(proxy):
                logger.info(f"[bulkchk] адрес {proxy.host} занят — acc#{acc_id} пропуск")
                release_account_lock(acc_id)
                continue
            client = make_telethon_client(acc, proxy)
            if not client:
                release_account_lock(acc_id); continue

            done_here = 0
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    logger.warning(f"[bulkchk] acc#{acc_id}: не авторизован")
                    continue
                logger.info(f"[bulkchk] {proxy_host} acc#{acc_id}: старт, квота {left}")

                while done_here < left and not _stopped():
                    try:
                        uname = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    try:
                        why = await _check_one(client, db, uname, acc_id)
                        await db.commit()
                        async with lock:
                            state["checked"] += 1
                            state["by_reason"][why] = state["by_reason"].get(why, 0) + 1
                    except FloodWaitError as fw:
                        record_error(acc_id, "", fw)
                        logger.warning(f"[bulkchk] acc#{acc_id}: FLOOD_WAIT {fw.seconds}с — "
                                       f"аккаунт выходит, канал вернём в очередь")
                        await queue.put(uname)
                        async with lock:
                            state["floods"] += 1
                        break
                    except Exception as e:
                        logger.debug(f"[bulkchk] @{uname}: {type(e).__name__}")
                        async with lock:
                            state["errors"] += 1
                    done_here += 1
                    async with lock:
                        state["remaining"] = queue.qsize()
                    await _publish(state)
                    await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
            finally:
                try: await client.disconnect()
                except Exception: pass
                release_account_lock(acc_id)
                logger.info(f"[bulkchk] {proxy_host} acc#{acc_id}: сделано {done_here}")

        # пауза перед следующим аккаунтом ЭТОГО ЖЕ IP
        if idx < len(accounts) - 1 and not queue.empty() and not _stopped():
            await asyncio.sleep(ACCOUNT_GAP + random.uniform(0, 60))


async def _run_bulk_check(limit: int = None):
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)
    from sqlalchemy import text
    from utils.db_pool import async_session as Session
    from utils.redis_pool import get_redis

    try:
        get_redis().delete(STOP_KEY)
    except Exception:
        pass

    async with Session() as db:
        # аккаунты, сгруппированные по ФИЗИЧЕСКОМУ адресу прокси
        # ── Чьими аккаунтами проверяем ───────────────────────────────
        # По умолчанию карантинными, а не рабочими. Причин две. Рабочие
        # заняты кампанией и делят с карантинными те же адреса — проверка
        # отнимала бы у них прокси и слоты. А карантинным ограничение
        # Telegram мешает только ПИСАТЬ: читать свойства канала они могут,
        # и всё это время простаивают.
        # BULKCHK_STATUSES позволяет вернуть рабочих, когда кампания стоит.
        rows = (await db.execute(text("""
            SELECT p.host, a.id
            FROM accounts a JOIN proxies p ON p.id = a.proxy_id
            WHERE a.status::text = ANY(:st) AND p.is_valid
            ORDER BY p.host, a.id
        """), {"st": STATUSES})).all()
        by_proxy: dict[str, list[int]] = {}
        for host, acc_id in rows:
            by_proxy.setdefault(host, []).append(acc_id)

        capacity = sum(min(len(v) * PER_ACCOUNT_DAY, 10**6) for v in by_proxy.values())
        take = limit or capacity
        channels = await _take_channels(db, take)

    if not channels:
        return {"status": "nothing_to_check"}

    hosts = list(by_proxy.keys())
    if MAX_PROXIES:
        random.shuffle(hosts)
        hosts = hosts[:MAX_PROXIES]

    queue: asyncio.Queue = asyncio.Queue()
    for ch in channels:
        queue.put_nowait(ch)

    state = {
        "status": "running",
        "started_at": datetime.utcnow().isoformat(),
        "total": len(channels), "checked": 0, "remaining": len(channels),
        "floods": 0, "errors": 0, "by_reason": {},
        "proxies": len(hosts),
        "accounts": sum(len(by_proxy[h]) for h in hosts),
    }
    await _publish(state)
    logger.info(
        f"[bulkchk] старт: {len(channels)} каналов, {len(hosts)} прокси параллельно, "
        f"{state['accounts']} аккаунтов, потолок {PER_ACCOUNT_DAY}/аккаунт/сутки"
    )

    lock = asyncio.Lock()
    # По одному воркеру на КАЖДЫЙ физический IP — они идут параллельно,
    # а внутри воркера аккаунты строго по очереди.
    await asyncio.gather(*[
        _worker_for_proxy(h, by_proxy[h], queue, state, lock) for h in hosts
    ], return_exceptions=True)

    state["status"] = "done"
    state["remaining"] = queue.qsize()
    state["finished_at"] = datetime.utcnow().isoformat()
    await _publish(state)
    logger.info(f"[bulkchk] финиш: проверено {state['checked']}, "
                f"осталось {state['remaining']}, floods {state['floods']}")
    return state


@celery_app.task(
    bind=True,
    name="tasks.bulk_channel_check.run_bulk_check",
    # Глобальный task_time_limit=600 убил бы этот таск через 10 минут,
    # а полный проход по очереди идёт часами. Ставим свой потолок: 12 часов
    # жёстко, 11.5 мягко. Остановить раньше — stop_bulk_check (мягкий стоп
    # через Redis-флаг, воркеры доводят текущий канал и выходят).
    time_limit=12 * 3600,
    soft_time_limit=int(11.5 * 3600),
)
def run_bulk_check(self, limit: int = None):
    """Массовая проверка каналов: параллельно по прокси, последовательно внутри IP."""
    return run_async(_run_bulk_check(limit))


@celery_app.task(bind=True, name="tasks.bulk_channel_check.stop_bulk_check")
def stop_bulk_check(self):
    """Мягкая остановка: воркеры завершатся после текущего канала."""
    try:
        from utils.redis_pool import get_redis
        get_redis().setex(STOP_KEY, 3600, "1")
        return {"stopped": True}
    except Exception as e:
        return {"error": str(e)}
