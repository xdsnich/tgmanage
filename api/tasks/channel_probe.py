"""
GramGPT — tasks/channel_probe.py
Активное заполнение channel_capabilities.

Зачем: чтобы аккаунты не долбились в каналы, где заведомо нельзя
комментировать (нет обсуждений / вступление по заявке / писать запрещено).
Пассивно кэш наполняется сам по ходу работы, но каналы, куда ещё никто не
заходил, надо проверить заранее — иначе первый отказ всё равно получит
живой аккаунт.

Стоимость (замер 2026-08-22, acc#208, 30 незнакомых каналов подряд):
  * 2 read-only запроса на канал: get_entity + GetFullChannelRequest
  * НИ ОДНОГО FLOOD_WAIT
  * ~0.2с чистого времени на канал
  * ни вступлений, ни отправки сообщений

Нагрузка размазывается: PROBE_PER_ACCOUNT каналов за один запуск на аккаунт.
При 4823 каналах и 141 аккаунте это ~34 канала на аккаунт — около минуты.
Аккаунт берётся только если он не отдыхает и не выбрал суточный лимит
подключений.
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

# Сколько каналов проверяет ОДИН аккаунт за запуск.
PROBE_PER_ACCOUNT = int(os.getenv("PROBE_PER_ACCOUNT", "40"))
# Пауза между проверками — чтобы не выглядело как скан.
PROBE_DELAY_MIN = float(os.getenv("PROBE_DELAY_MIN", "1.2"))
PROBE_DELAY_MAX = float(os.getenv("PROBE_DELAY_MAX", "3.5"))


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _probe_with_account(account_id: int, limit: int = None) -> dict:
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy import select, text
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as Session
    from utils.telegram import make_telethon_client
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.account_health import is_resting, record_error
    from utils.connection_limiter import try_acquire_connection, release_connection
    from utils.ip_throttle import acquire_ip_lock
    from utils.channel_caps import probe_channel

    limit = limit or PROBE_PER_ACCOUNT
    checked = 0
    bad = 0
    unknown = 0

    resting, why = is_resting(account_id)
    if resting:
        return {"status": "resting", "reason": why}

    async with Session() as db:
        acc = (await db.execute(
            select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
            .where(TelegramAccount.id == account_id)
        )).scalar_one_or_none()
        if not acc or acc.status not in ("active", "unknown"):
            return {"status": "inactive"}

        proxy = None
        if acc.proxy_id:
            proxy = (await db.execute(select(Proxy).where(Proxy.id == acc.proxy_id))).scalar_one_or_none()
        if not proxy:
            return {"status": "no_proxy"}
        if not acquire_ip_lock(proxy):
            return {"status": "ip_cooldown"}
        if not try_acquire_connection(account_id):
            return {"status": "daily_limit"}
        if not acquire_account_lock(account_id, ttl=900):
            release_connection(account_id)
            return {"status": "locked"}

        client = make_telethon_client(acc, proxy)
        if not client:
            release_connection(account_id)
            release_account_lock(account_id)
            return {"status": "no_client"}

        try:
            await client.connect()
            if not await client.is_user_authorized():
                return {"status": "not_authorized"}

            # Каналы без вердикта: активные цели, которых ещё нет в кэше
            rows = (await db.execute(text("""
                SELECT DISTINCT tc.username
                FROM target_channels tc
                LEFT JOIN channel_capabilities cc ON cc.channel_username = tc.username
                WHERE tc.is_active = true
                  AND tc.username IS NOT NULL AND tc.username <> ''
                  AND cc.id IS NULL
                LIMIT :lim
            """), {"lim": limit})).all()
            targets = [r[0] for r in rows]
            if not targets:
                return {"status": "nothing_to_probe"}

            logger.info(f"[probe] acc#{account_id}: проверяю {len(targets)} каналов")

            for uname in targets:
                try:
                    res = await probe_channel(client, db, uname, account_id=account_id)
                    # commentable может быть None = «не выяснили» (сеть, сервер TG).
                    # Это НЕ то же самое, что «непригоден»: такой канал просто
                    # остаётся в очереди и будет перепроверен позже.
                    verdict_ = res.get("commentable")
                    if verdict_ is None:
                        unknown += 1
                    else:
                        checked += 1
                        if verdict_ is False:
                            bad += 1
                except Exception as e:
                    # FLOOD_WAIT здесь = претензия к аккаунту, останавливаемся
                    h = record_error(account_id, "", e)
                    if h.get("severity") == "account":
                        logger.warning(f"[probe] acc#{account_id}: {type(e).__name__} — стоп")
                        break
                await db.commit()
                await asyncio.sleep(random.uniform(PROBE_DELAY_MIN, PROBE_DELAY_MAX))

            await db.commit()
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            release_account_lock(account_id)

    return {"status": "ok", "checked": checked, "not_commentable": bad,
            "unknown": unknown}


async def _dispatch_probe():
    """Раскидывает проверку по свободным аккаунтам — по одному таску на аккаунт."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)
    from sqlalchemy import text
    from utils.db_pool import async_session as Session
    from utils.account_health import is_resting

    async with Session() as db:
        pending = (await db.execute(text("""
            SELECT COUNT(*) FROM target_channels tc
            LEFT JOIN channel_capabilities cc ON cc.channel_username = tc.username
            WHERE tc.is_active = true AND tc.username IS NOT NULL AND cc.id IS NULL
        """))).scalar() or 0
        if not pending:
            return {"dispatched": 0, "pending": 0}

        accs = (await db.execute(text("""
            SELECT a.id FROM accounts a
            WHERE a.status = 'active' AND a.proxy_id IS NOT NULL
        """))).all()

    free = [a[0] for a in accs if not is_resting(a[0])[0]]
    if not free:
        return {"dispatched": 0, "pending": pending, "reason": "нет свободных аккаунтов"}

    # Сколько аккаунтов реально нужно, чтобы закрыть очередь
    need = min(len(free), max(1, (pending + PROBE_PER_ACCOUNT - 1) // PROBE_PER_ACCOUNT))
    random.shuffle(free)
    chosen = free[:need]

    # Размазываем старты — тот же anti-burst принцип, что в dispatch_plans
    window = max(60, len(chosen) * 45)
    for acc_id in chosen:
        celery_app.send_task(
            "tasks.channel_probe.probe_with_account",
            args=[acc_id],
            queue="parsers",
            countdown=random.uniform(0, window),
        )
    logger.info(
        f"[probe] очередь {pending} каналов → {len(chosen)} аккаунтов "
        f"в окне {window}с"
    )
    return {"dispatched": len(chosen), "pending": pending}


@celery_app.task(bind=True, name="tasks.channel_probe.probe_with_account")
def probe_with_account(self, account_id: int, limit: int = None):
    """Проверка пачки каналов одним аккаунтом (2 read-only запроса на канал)."""
    return run_async(_probe_with_account(account_id, limit))


@celery_app.task(bind=True, name="tasks.channel_probe.dispatch_probe")
def dispatch_probe(self):
    """Диспетчер: раскидывает непроверенные каналы по свободным аккаунтам."""
    return run_async(_dispatch_probe())
