# -*- coding: utf-8 -*-
"""Выдать семи аккаунтам персональные ссылки и прикрепить их каналы.

У этих аккаунтов канал уже есть — со своим адресом и названием в нужном
стиле, — но он ничего не даёт: в нём нет поста со ссылкой на воронку, и
сам канал не прикреплён к профилю. То есть человек, открывший профиль,
канала не увидит, а если и зайдёт — идти оттуда некуда.

Две фазы, чтобы каждый аккаунт подключался один раз:

  1. Админ (jeka_opasniy) одним заходом создаёт все ссылки партии.
     Обязательно С ЗАЯВКОЙ (request_needed): без неё в канал влетает
     кто угодно, наткнувшийся на ссылку, и считать становится нечего.
     Заголовок «acc#N адрес_канала» — по нему потом читается статистика
     в разделе пригласительных ссылок.

  2. Каждый аккаунт заходит один раз и делает всё сразу: публикует пост
     со своей ссылкой и прикрепляет канал к профилю. Аккаунты на разных
     адресах идут одновременно.
"""
import asyncio
import os
import sys
import random
import argparse
from collections import defaultdict
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

ADMIN = 48
FUNNEL_ID = 4437551288


async def _клиент(acc_id, ttl=600):
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as Session
    from utils.telegram import make_telethon_client
    from utils.ip_throttle import acquire_ip_lock
    from utils.account_lock import acquire_account_lock

    async with Session() as db:
        acc = (await db.execute(select(TelegramAccount)
               .options(joinedload(TelegramAccount.api_app))
               .where(TelegramAccount.id == acc_id))).scalar_one_or_none()
        proxy = (await db.execute(select(Proxy)
                 .where(Proxy.id == acc.proxy_id))).scalar_one_or_none() if acc else None
    if not acc or not proxy:
        return None, "нет прокси"
    if not acquire_ip_lock(proxy, owner=acc_id):
        return None, "адрес занят"
    if not acquire_account_lock(acc_id, ttl=ttl):
        return None, "аккаунт занят"
    c = make_telethon_client(acc, proxy)
    if c is None:
        return None, "нет сессии"
    return c, ""


async def создать_ссылки(пары):
    """пары: [(acc_id, адрес_канала)] → {acc_id: ссылка}."""
    from telethon.tl.functions.messages import ExportChatInviteRequest
    from utils.account_lock import release_account_lock

    for попытка in range(15):
        client, err = await _клиент(ADMIN)
        if client:
            break
        print("админ: %s — жду" % err, flush=True)
        await asyncio.sleep(45)
    else:
        return {}

    out = {}
    try:
        await client.connect()
        dest = None
        async for d in client.iter_dialogs(limit=200):
            if getattr(d.entity, "id", None) == FUNNEL_ID:
                dest = d.entity
                break
        if dest is None:
            print("канал воронки не найден")
            return out
        for aid, адрес in пары:
            try:
                res = await client(ExportChatInviteRequest(
                    peer=dest, title="acc#%d %s" % (aid, адрес),
                    expire_date=None, usage_limit=None, request_needed=True))
                out[aid] = res.link
                print("acc#%-4s ссылка %s" % (aid, res.link), flush=True)
            except Exception as e:
                print("acc#%-4s ссылка не вышла: %s" % (aid, type(e).__name__),
                      flush=True)
            await asyncio.sleep(random.uniform(1.5, 3))
        return out
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        release_account_lock(ADMIN)


async def поставить(acc_id, ссылка):
    """Один заход: пост со ссылкой + прикрепление канала."""
    from telethon.tl.functions.account import UpdatePersonalChannelRequest
    from utils.account_lock import release_account_lock

    client, err = await _клиент(acc_id)
    if client is None:
        return err
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return "сессия мертва"
        свои = []
        async for d in client.iter_dialogs(limit=200):
            e = d.entity
            if getattr(e, "broadcast", False) and getattr(e, "creator", False):
                свои.append(e)
        if not свои:
            return "нет канала"
        # Если каналов несколько — берём самый ранний, он настоящий.
        ch = min(свои, key=lambda e: e.date)

        # Пост: если он уже есть — правим текст, не создавая второй.
        старый = None
        async for m in client.iter_messages(ch, limit=10):
            if m.text and "t.me/" in m.text:
                старый = m
                break
        if старый is not None:
            await client.edit_message(ch, старый.id, ссылка, link_preview=False)
            что = "пост поправлен"
        else:
            await client.send_message(ch, ссылка, link_preview=False)
            что = "пост опубликован"
        await asyncio.sleep(random.uniform(1.5, 3))
        await client(UpdatePersonalChannelRequest(channel=ch))
        return "%s, канал прикреплён (t.me/%s)" % (
            что, getattr(ch, "username", "") or "?")
    except Exception as e:
        return "сбой %s" % type(e).__name__
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        release_account_lock(acc_id)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", required=True)
    a = ap.parse_args()

    from sqlalchemy import text as sa_text
    from utils.db_pool import async_session as Session

    ids = [int(x) for x in a.ids.split(",") if x.strip()]
    async with Session() as db:
        адрес = dict((r[0], "%s:%s" % (r[1], r[2])) for r in (
            await db.execute(sa_text("""
                SELECT a.id, COALESCE(p.host,'?'), COALESCE(p.port,0)
                FROM accounts a LEFT JOIN proxies p ON p.id = a.proxy_id
                WHERE a.id = ANY(:i)"""), {"i": ids})).all())

    ссылки = await создать_ссылки([(i, "acc%d" % i) for i in ids])
    print(flush=True)

    по_адресам = defaultdict(list)
    for i in ids:
        по_адресам[адрес.get(i, "?")].append(i)

    async def очередь(список):
        for i in список:
            if i not in ссылки:
                print("acc#%-4s пропуск — нет ссылки" % i, flush=True)
                continue
            for _ in range(8):
                r = await поставить(i, ссылки[i])
                if r not in ("адрес занят", "аккаунт занят"):
                    break
                await asyncio.sleep(random.uniform(35, 60))
            print("[%s] acc#%-4s %s"
                  % (datetime.now().strftime("%H:%M:%S"), i, r), flush=True)

    await asyncio.gather(*[очередь(v) for v in по_адресам.values()])


asyncio.run(main())
