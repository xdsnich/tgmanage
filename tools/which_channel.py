# -*- coding: utf-8 -*-
"""Определить настоящий канал аккаунта по его персональной ссылке.

У части аккаунтов каналов несколько: старые остались от прежних заходов,
плюс я вчера по ошибке создал лишние. Понять со стороны, какой из них
рабочий, по названию или дате нельзя — они все выглядят одинаково.

Но у каждого рабочего аккаунта есть СВОЯ пригласительная ссылка в воронку.
Их создавал админ (jeka_opasniy) с заголовком вида «acc#58 amelie_link_access»,
и именно эта ссылка вшита в пост нужного канала. Значит правило простое:
канал, в чьём посте лежит персональная ссылка аккаунта, и есть настоящий.

Порядок:
  1. заходим админом и читаем все пригласительные ссылки канала воронки —
     из заголовка достаём номер аккаунта;
  2. по каждому аккаунту читаем посты во всех его каналах и ищем свою
     ссылку;
  3. нашли — прикрепляем этот канал к профилю.

Ничего не удаляем: лишние каналы просто остаются отцепленными.
"""
import asyncio
import os
import re
import sys
import random
import argparse
from collections import defaultdict
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

ADMIN = 48
FUNNEL_ID = 4437551288


async def ссылки_админа():
    """Все пригласительные ссылки воронки: acc_id -> набор ссылок."""
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as Session
    from utils.telegram import make_telethon_client
    from utils.ip_throttle import acquire_ip_lock
    from telethon.tl.functions.messages import GetExportedChatInvitesRequest
    from telethon.tl.types import InputUserSelf

    async with Session() as db:
        acc = (await db.execute(select(TelegramAccount)
               .options(joinedload(TelegramAccount.api_app))
               .where(TelegramAccount.id == ADMIN))).scalar_one_or_none()
        proxy = (await db.execute(select(Proxy)
                 .where(Proxy.id == acc.proxy_id))).scalar_one_or_none()

    acquire_ip_lock(proxy, owner=ADMIN)
    client = make_telethon_client(acc, proxy)
    карта = defaultdict(set)
    try:
        await client.connect()
        dest = None
        async for d in client.iter_dialogs(limit=200):
            if getattr(d.entity, "id", None) == FUNNEL_ID:
                dest = d.entity
                break
        if dest is None:
            print("канал воронки не найден у админа")
            return карта

        offset_link = None
        всего = 0
        while True:
            res = await client(GetExportedChatInvitesRequest(
                peer=dest, admin_id=InputUserSelf(), limit=100,
                offset_link=offset_link))
            if not res.invites:
                break
            for inv in res.invites:
                всего += 1
                title = getattr(inv, "title", "") or ""
                m = re.search(r"acc#?(\d+)", title)
                if m:
                    карта[int(m.group(1))].add(inv.link)
            if len(res.invites) < 100:
                break
            offset_link = res.invites[-1].link
            await asyncio.sleep(1.5)
        print("ссылок у админа: %d, с номером аккаунта в названии: %d"
              % (всего, sum(len(v) for v in карта.values())))
        return карта
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def хвост(link):
    """Опознавательная часть ссылки: t.me/+AbCdEf → AbCdEf."""
    return link.rstrip("/").split("/")[-1].lstrip("+")


async def разобрать(acc_id, мои_ссылки, прикреплять):
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as Session
    from utils.telegram import make_telethon_client
    from utils.ip_throttle import acquire_ip_lock
    from utils.account_lock import acquire_account_lock, release_account_lock
    from telethon.tl.functions.account import UpdatePersonalChannelRequest
    from telethon.tl.functions.users import GetFullUserRequest

    из = {"acc": acc_id, "итог": "", "каналы": [], "нашёл": None, "было": None}

    async with Session() as db:
        acc = (await db.execute(select(TelegramAccount)
               .options(joinedload(TelegramAccount.api_app))
               .where(TelegramAccount.id == acc_id))).scalar_one_or_none()
        proxy = (await db.execute(select(Proxy)
                 .where(Proxy.id == acc.proxy_id))).scalar_one_or_none() if acc else None
    if not acc or not proxy:
        из["итог"] = "нет прокси"
        return из
    if not acquire_ip_lock(proxy, owner=acc_id):
        из["итог"] = "адрес занят"
        return из
    if not acquire_account_lock(acc_id, ttl=600):
        из["итог"] = "аккаунт занят"
        return из

    client = make_telethon_client(acc, proxy)
    if client is None:
        release_account_lock(acc_id)
        из["итог"] = "нет сессии"
        return из
    try:
        await client.connect()
        if not await client.is_user_authorized():
            из["итог"] = "сессия мертва"
            return из
        me = await client.get_me()
        try:
            full = await client(GetFullUserRequest(me))
            из["было"] = getattr(full.full_user, "personal_channel_id", None)
        except Exception:
            pass

        свои = []
        async for d in client.iter_dialogs(limit=200):
            e = d.entity
            if getattr(e, "broadcast", False) and getattr(e, "creator", False):
                свои.append(e)

        хвосты = {хвост(l) for l in мои_ссылки}
        нашёл = None
        for e in свои:
            текст = ""
            async for msg in client.iter_messages(e, limit=20):
                текст += (msg.text or "") + "\n"
            есть = any(h and h in текст for h in хвосты)
            из["каналы"].append({
                "username": getattr(e, "username", "") or "",
                "title": e.title, "создан": e.date.strftime("%m-%d"),
                "постов": текст.count("\n"), "своя_ссылка": есть,
                "прикреплён": e.id == из["было"]})
            if есть and нашёл is None:
                нашёл = e
        из["нашёл"] = (getattr(нашёл, "username", "") or str(нашёл.id)) if нашёл else None

        if прикреплять and нашёл is not None and нашёл.id != из["было"]:
            await client(UpdatePersonalChannelRequest(channel=нашёл))
            из["итог"] = "перецепил"
        else:
            из["итог"] = "ок"
        return из
    except Exception as e:
        из["итог"] = "сбой %s" % type(e).__name__
        return из
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        release_account_lock(acc_id)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="перецепить профиль на найденный канал")
    a = ap.parse_args()

    from sqlalchemy import text as sa_text
    from utils.db_pool import async_session as Session

    ids = [int(x) for x in a.ids.split(",") if x.strip()]
    карта = await ссылки_админа()
    нет_ссылки = [i for i in ids if not карта.get(i)]
    if нет_ссылки:
        print("персональной ссылки нет у: %s"
              % ", ".join("#%d" % i for i in нет_ссылки))
    print()

    async with Session() as db:
        адрес = dict((r[0], "%s:%s" % (r[1], r[2])) for r in (
            await db.execute(sa_text("""
                SELECT a.id, COALESCE(p.host,'?'), COALESCE(p.port,0)
                FROM accounts a LEFT JOIN proxies p ON p.id = a.proxy_id
                WHERE a.id = ANY(:i)"""), {"i": ids})).all())
    по_адресам = defaultdict(list)
    for i in ids:
        по_адресам[адрес.get(i, "?")].append(i)

    итоги = []

    async def очередь(список):
        for i in список:
            for _ in range(20):
                r = await разобрать(i, карта.get(i, set()), a.apply)
                if r["итог"] not in ("адрес занят", "аккаунт занят"):
                    break
                await asyncio.sleep(random.uniform(30, 60))
            итоги.append(r)
            print("[%s] acc#%-5s %-10s каналов %d, своя ссылка в: %s"
                  % (datetime.now().strftime("%H:%M:%S"), r["acc"], r["итог"],
                     len(r["каналы"]), r["нашёл"] or "НИ В ОДНОМ"), flush=True)

    await asyncio.gather(*[очередь(v) for v in по_адресам.values()])

    print()
    print("=" * 92)
    for r in sorted(итоги, key=lambda x: x["acc"]):
        print("acc#%-5s %s" % (r["acc"], r["итог"]))
        for c in r["каналы"]:
            метки = []
            if c["своя_ссылка"]:
                метки.append("СВОЯ ССЫЛКА")
            if c["прикреплён"]:
                метки.append("был прикреплён")
            print("    t.me/%-26s %-18s создан %s  %s"
                  % (c["username"] or "—", c["title"][:18], c["создан"],
                     ", ".join(метки)))


asyncio.run(main())
