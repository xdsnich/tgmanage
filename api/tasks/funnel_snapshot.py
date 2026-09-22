# -*- coding: utf-8 -*-
"""Снимок воронки в полночь — чтобы считать суточный приток.

Зачем. Список заявок читается постранично и на практике отдаётся не
целиком, поэтому пересчитать «сколько пришло за день» перебором нельзя.
Но у канала есть два готовых счётчика — подписчики и заявки в ожидании,
— и они отдаются одним запросом, без листания и без потолка.

Как считаем приток. Не по одним заявкам: принятая заявка уходит из
ожидающих и появляется в подписчиках, и если считать только ожидающих,
каждый приём выглядел бы как отток. Поэтому берём СУММУ двух счётчиков —
она растёт на каждого пришедшего независимо от того, приняли его или нет.

    приток за день = (подписчики + ждущие) сейчас
                   − (подписчики + ждущие) в полночь

Снимок кладётся в Redis под датой и живёт двое суток: этого хватает,
чтобы ответить и про сегодня, и про вчера.
"""
import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone

from celery_app import celery_app

logger = logging.getLogger(__name__)

ADMIN_ACC = int(os.getenv("FUNNEL_ADMIN_ACC", "48"))      # @jeka_opasniy
FUNNEL_ID = int(os.getenv("FUNNEL_CHANNEL_ID", "4437551288"))
KYIV = timezone(timedelta(hours=3))
SNAP_KEY = "gramgpt:funnel:snap:{day}"
SNAP_TTL = 60 * 60 * 48


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def прочитать_счётчики():
    """(подписчиков, ждущих) прямо из Telegram. None при неудаче."""
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as Session
    from utils.telegram import make_telethon_client
    from utils.ip_throttle import acquire_ip_lock
    from utils.account_lock import acquire_account_lock, release_account_lock
    from telethon.tl.functions.channels import GetFullChannelRequest

    async with Session() as db:
        acc = (await db.execute(select(TelegramAccount)
               .options(joinedload(TelegramAccount.api_app))
               .where(TelegramAccount.id == ADMIN_ACC))).scalar_one_or_none()
        proxy = (await db.execute(select(Proxy)
                 .where(Proxy.id == acc.proxy_id))).scalar_one_or_none() if acc else None
    if not acc or not proxy:
        return None, "нет прокси"

    # Админ занят выдачей ссылок и живёт на общем адресе, поэтому ждём
    # своего окна, а не отказываемся при первой занятости.
    for _ in range(20):
        if acquire_ip_lock(proxy, owner=ADMIN_ACC) and \
                acquire_account_lock(ADMIN_ACC, ttl=300):
            break
        await asyncio.sleep(20)
    else:
        return None, "окно админа так и не освободилось"

    client = make_telethon_client(acc, proxy)
    if client is None:
        release_account_lock(ADMIN_ACC)
        return None, "нет сессии"
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return None, "сессия мертва"
        dest = None
        async for d in client.iter_dialogs(limit=200):
            if getattr(d.entity, "id", None) == FUNNEL_ID:
                dest = d.entity
                break
        if dest is None:
            return None, "канал воронки не найден"
        full = (await client(GetFullChannelRequest(dest))).full_chat
        return (int(getattr(full, "participants_count", 0) or 0),
                int(getattr(full, "requests_pending", 0) or 0)), ""
    except Exception as e:
        return None, "сбой %s: %s" % (type(e).__name__, str(e)[:70])
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        release_account_lock(ADMIN_ACC)


def _redis():
    from utils.redis_pool import get_redis
    return get_redis()


def записать_снимок(день, подписчиков, ждущих):
    r = _redis()
    k = SNAP_KEY.format(day=день)
    r.hset(k, "participants", подписчиков)
    r.hset(k, "pending", ждущих)
    r.hset(k, "at", datetime.now(KYIV).isoformat())
    r.expire(k, SNAP_TTL)


def прочитать_снимок(день):
    try:
        d = _redis().hgetall(SNAP_KEY.format(day=день)) or {}
    except Exception:
        return None
    out = {}
    for k, v in d.items():
        k = k.decode() if isinstance(k, bytes) else k
        out[k] = v.decode() if isinstance(v, bytes) else v
    if "participants" not in out:
        return None
    return {"participants": int(out["participants"]),
            "pending": int(out["pending"]), "at": out.get("at", "")}


async def _снять(день=None):
    день = день or datetime.now(KYIV).date().isoformat()
    счёт, err = await прочитать_счётчики()
    if счёт is None:
        logger.warning("[funnel] снимок не снят: %s", err)
        return {"ok": False, "error": err}
    п, ж = счёт
    записать_снимок(день, п, ж)
    logger.warning("[funnel] снимок %s: подписчиков %d, ждут %d (сумма %d)",
                   день, п, ж, п + ж)
    return {"ok": True, "день": день, "подписчиков": п, "ждут": ж, "сумма": п + ж}


async def _приток():
    """Сколько пришло с полуночи. Опирается на снимок этого дня."""
    сегодня = datetime.now(KYIV).date().isoformat()
    база = прочитать_снимок(сегодня)
    счёт, err = await прочитать_счётчики()
    if счёт is None:
        return {"ok": False, "error": err}
    п, ж = счёт
    if not база:
        # Снимка нет — ставим текущий как точку отсчёта, приток посчитаем
        # со следующего запроса.
        записать_снимок(сегодня, п, ж)
        return {"ok": True, "подписчиков": п, "ждут": ж,
                "приток": None,
                "примечание": "снимка за сегодня не было, поставил точку отсчёта"}
    приток = (п + ж) - (база["participants"] + база["pending"])
    return {"ok": True, "подписчиков": п, "ждут": ж, "приток": приток,
            "отсчёт_от": база["at"],
            "было_в_полночь": база["participants"] + база["pending"]}


@celery_app.task(bind=True, name="tasks.funnel_snapshot.snapshot_funnel",
                 max_retries=0)
def snapshot_funnel(self, день=None):
    return run_async(_снять(день))


@celery_app.task(bind=True, name="tasks.funnel_snapshot.funnel_inflow",
                 max_retries=0)
def funnel_inflow(self):
    return run_async(_приток())
