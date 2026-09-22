# -*- coding: utf-8 -*-
"""Возврат ограниченных аккаунтов в realtime, когда срок вышел.

Зачем. Ограничения Telegram за жалобы пользователей почти всегда
временные: бот называет точную дату снятия — чаще сутки, иногда неделю.
Аккаунт, снятый с кампании, сам обратно не вернётся, и каждый такой
простой стоит нам его суточной нормы. Раньше возврат делался руками и
поэтому не делался вовсе.

Как. Ограниченный аккаунт помечается ключом в Redis со сроком снятия.
Задача просыпается раз в двадцать минут, смотрит, у кого срок вышел, и
у таких СПРАШИВАЕТ @SpamBot. Только его ответ считается за факт: срок
могли продлить, а могли снять раньше. Чист — возвращаем в кампанию.

Срок «?» означает, что бот тогда не назвал дату. Такие тоже проверяются,
но не чаще раза в сутки, чтобы не дёргать бота впустую.
"""
import asyncio
import logging
import os
import random
from datetime import datetime, timedelta

from celery_app import celery_app

logger = logging.getLogger(__name__)

CAMP_ID = int(os.getenv("RT_CAMPAIGN_ID", "143"))
LIMIT_KEY = "gramgpt:rt:limited:{acc}"
# Сколько аккаунтов опрашиваем за проход. Каждый опрос — это подключение
# и занятый адрес; оптом они отняли бы адреса у самого комментинга.
PER_RUN = int(os.getenv("RT_RESTORE_PER_RUN", "4"))
# Аккаунты без названной даты проверяем не чаще этого.
BLIND_RETRY_H = int(os.getenv("RT_RESTORE_BLIND_RETRY_H", "24"))
# Сколько секунд на проход можно потратить в ожидании свободных адресов.
# Задачу Celery убивает на десятой минуте, поэтому берём семь с запасом.
БЮДЖЕТ_ОЖИДАНИЯ = int(os.getenv("RT_RESTORE_WAIT_BUDGET", "420"))

# Аккаунты, которых в кампании быть не должно ни при каких сроках.
НЕ_ВОЗВРАЩАТЬ = {
    int(x) for x in os.getenv("RT_NEVER_RESTORE", "48,95").split(",") if x.strip()
}


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _redis():
    from utils.redis_pool import get_redis
    return get_redis()


def mark_limited(acc_id: int, until: str = "?"):
    """Пометить аккаунт ограниченным. until — ISO-время либо '?'."""
    try:
        # Поля пишем по одному: сервер здесь старой версии и многопольный
        # HSET не принимает — молча падает на «wrong number of arguments».
        r = _redis()
        k = LIMIT_KEY.format(acc=acc_id)
        r.hset(k, "until", until)
        r.hset(k, "checked", datetime.utcnow().isoformat())
    except Exception:
        logger.debug("[rt_restore] не удалось пометить acc#%s", acc_id,
                     exc_info=True)


def _прочитать(acc_id):
    try:
        d = _redis().hgetall(LIMIT_KEY.format(acc=acc_id)) or {}
    except Exception:
        return None
    out = {}
    for k, v in d.items():
        k = k.decode() if isinstance(k, bytes) else k
        out[k] = v.decode() if isinstance(v, bytes) else v
    return out or None


async def снять_с_кампании(db, campaign_id, acc_id):
    """Убрать аккаунт из списка кампании и погасить его ожидающие задания.

    Вызывается исполнителем сразу, как Telegram сказал «забанен». Держать
    такого в списке нельзя: каждое его задание занимает адрес на две
    минуты и гарантированно падает.
    """
    import json
    from sqlalchemy import text as sa_text

    ids = (await db.execute(sa_text(
        "SELECT account_ids FROM campaigns WHERE id=:c"),
        {"c": campaign_id})).scalar() or []
    ids = [int(x) for x in ids]
    if acc_id not in ids:
        return False
    await db.execute(sa_text(
        "UPDATE campaigns SET account_ids=CAST(:i AS jsonb), updated_at=now()"
        " WHERE id=:c"),
        {"i": json.dumps([x for x in ids if x != acc_id]), "c": campaign_id})
    await db.execute(sa_text(
        "UPDATE comment_queue SET status='aborted', error='аккаунт ограничен'"
        " WHERE campaign_id=:c AND status='scheduled' AND account_id=:a"),
        {"c": campaign_id, "a": acc_id})
    logger.warning("[rt_restore] acc#%s снят с кампании %s до снятия ограничения",
                   acc_id, campaign_id)
    return True


async def _спросить_бота(acc_id, дедлайн=None):
    """Вернуть (чист, текст). Адрес и аккаунт берём и отпускаем честно.

    Про терпение. Ограниченный аккаунт снят с кампании, но его адрес
    продолжают занимать соседи, и закрепляется он за каждым на десять
    минут. Прежняя версия просила адрес ровно один раз и уходила ни с
    чем — при работающем комментинге это значило «никогда»: за полдня
    задача не смогла проверить ни одного созревшего. Поэтому ждём
    короткими шагами, ловя момент, когда чужое закрепление истечёт.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as Session
    from utils.telegram import make_telethon_client
    from utils.ip_throttle import acquire_ip_lock
    from utils.account_lock import acquire_account_lock, release_account_lock

    async with Session() as db:
        acc = (await db.execute(select(TelegramAccount)
               .options(joinedload(TelegramAccount.api_app))
               .where(TelegramAccount.id == acc_id))).scalar_one_or_none()
        proxy = (await db.execute(select(Proxy)
                 .where(Proxy.id == acc.proxy_id))).scalar_one_or_none() if acc else None
    if not acc or not proxy:
        return None, "нет прокси"
    while True:
        if acquire_ip_lock(proxy, owner=acc_id):
            break
        if дедлайн is None or datetime.utcnow() >= дедлайн:
            return None, "адрес занят"
        await asyncio.sleep(random.uniform(8, 15))
    if not acquire_account_lock(acc_id, ttl=180):
        return None, "аккаунт занят"

    client = make_telethon_client(acc, proxy)
    if client is None:
        release_account_lock(acc_id)
        return None, "нет сессии"
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return None, "сессия мертва"
        async with client.conversation("@SpamBot", timeout=25) as conv:
            await conv.send_message("/start")
            await asyncio.sleep(random.uniform(1.5, 3))
            текст = (await conv.get_response()).text or ""
        низ = текст.lower()
        чист = ("good news" in низ or "no limits" in низ
                or "free as a bird" in низ)
        return чист, текст
    except Exception as e:
        return None, "сбой %s" % type(e).__name__
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        release_account_lock(acc_id)


def _срок_из_текста(текст: str):
    """Выцепить дату снятия: '...limited until 19 Sep 2026, 11:05 UTC.'"""
    import re
    m = re.search(r"limited until ([0-9]{1,2} \w{3} [0-9]{4}, [0-9:]{4,5})", текст)
    if not m:
        return "?"
    try:
        return datetime.strptime(m.group(1), "%d %b %Y, %H:%M").isoformat()
    except Exception:
        return "?"


async def _once():
    import json
    from sqlalchemy import text as sa_text
    from utils.db_pool import async_session as Session

    r = _redis()
    сейчас = datetime.utcnow()
    try:
        ключи = [k.decode() if isinstance(k, bytes) else k
                 for k in r.scan_iter(match="gramgpt:rt:limited:*", count=500)]
    except Exception:
        return {"error": "redis"}

    созрели = []
    for k in ключи:
        try:
            acc_id = int(k.rsplit(":", 1)[1])
        except Exception:
            continue
        if acc_id in НЕ_ВОЗВРАЩАТЬ:
            continue
        d = _прочитать(acc_id) or {}
        until = d.get("until") or "?"
        if until != "?":
            try:
                if datetime.fromisoformat(until) > сейчас:
                    continue            # срок ещё идёт, не тревожим
            except Exception:
                pass
        else:
            # Даты нет — проверяем вслепую, но редко.
            try:
                был = datetime.fromisoformat(d.get("checked") or "")
                if сейчас - был < timedelta(hours=BLIND_RETRY_H):
                    continue
            except Exception:
                pass
        созрели.append(acc_id)

    stats = {"помечено": len(ключи), "созрело": len(созрели),
             "спрошено": 0, "возвращено": 0, "ещё_ограничены": 0}
    if not созрели:
        return stats

    random.shuffle(созрели)
    вернуть = []
    # Жёсткий предел задачи — десять минут. Оставляем себе семь на всё
    # ожидание адресов, иначе Celery оборвёт нас на полпути.
    дедлайн = datetime.utcnow() + timedelta(seconds=БЮДЖЕТ_ОЖИДАНИЯ)
    for acc_id in созрели[:PER_RUN]:
        if datetime.utcnow() >= дедлайн:
            logger.info("[rt_restore] бюджет ожидания вышел, остальные — в следующий раз")
            break
        чист, текст = await _спросить_бота(acc_id, дедлайн)
        if чист is None:
            logger.info("[rt_restore] acc#%s — %s, отложил", acc_id, текст)
            continue
        stats["спрошено"] += 1
        if чист:
            вернуть.append(acc_id)
        else:
            срок = _срок_из_текста(текст)
            mark_limited(acc_id, срок)
            stats["ещё_ограничены"] += 1
            logger.info("[rt_restore] acc#%s всё ещё ограничен до %s",
                        acc_id, срок)
        await asyncio.sleep(random.uniform(2, 5))

    if вернуть:
        async with Session() as db:
            ids = (await db.execute(sa_text(
                "SELECT account_ids FROM campaigns WHERE id=:c"),
                {"c": CAMP_ID})).scalar() or []
            стало = sorted(set(int(x) for x in ids) | set(вернуть))
            await db.execute(sa_text(
                "UPDATE campaigns SET account_ids=CAST(:i AS jsonb),"
                " updated_at=now() WHERE id=:c"),
                {"i": json.dumps(стало), "c": CAMP_ID})
            await db.commit()
        for acc_id in вернуть:
            try:
                r.delete(LIMIT_KEY.format(acc=acc_id))
            except Exception:
                pass
        stats["возвращено"] = len(вернуть)
        logger.warning("[rt_restore] ✅ вернул в кампанию: %s",
                       ", ".join("acc#%d" % i for i in вернуть))

    return stats


@celery_app.task(bind=True, name="tasks.rt_restore.restore_limited",
                 max_retries=0)
def restore_limited(self):
    st = run_async(_once())
    logger.info("[rt_restore] %s", st)
    return st
