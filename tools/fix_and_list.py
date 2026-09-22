# -*- coding: utf-8 -*-
"""Починить, доназначить юзернеймы и снять итоговую картину.

Три дела за один заход на аккаунт, чтобы не гонять его туда-сюда:

  1. У пятнадцати аккаунтов сегодня по ошибке создан второй канал. Их
     собственный канал существовал с июля — просто не был прикреплён к
     профилю, и проверка это не увидела. Прикрепляем СТАРЫЙ, новый
     оставляем висеть непривязанным: удаление необратимо, а пустой канал
     никому не мешает.

  2. Кому не хватает юзернейма — выдаём. Имя при этом не трогаем: у
     работающих аккаунтов оно и так в нужном стиле.

  3. Со всех читаем итог — имя, юзернейм, прикреплённый канал, — чтобы
     было что проверить глазами.

Идём параллельно по адресам: одно подключение на прокси, но все прокси
одновременно.
"""
import asyncio
import os
import sys
import random
import argparse
from collections import defaultdict
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

ЮЗЕРЫ = ["amelia_taylor", "ameli_taylor", "amelia_t", "ameli_t",
         "amelita_t", "amy_taylor", "amelinka_t", "ami_taylor"]


async def обработать(acc_id, чинить, давать_юзер):
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as Session
    from utils.telegram import make_telethon_client
    from utils.ip_throttle import acquire_ip_lock
    from utils.account_lock import acquire_account_lock, release_account_lock
    from telethon.tl.functions.users import GetFullUserRequest
    from telethon.tl.functions.account import (
        UpdatePersonalChannelRequest, UpdateUsernameRequest)
    from telethon import errors as terr

    итог = {"acc": acc_id, "итог": "", "имя": "", "юзер": "",
            "канал": "", "каналов": 0, "что_сделал": []}

    async with Session() as db:
        acc = (await db.execute(select(TelegramAccount)
               .options(joinedload(TelegramAccount.api_app))
               .where(TelegramAccount.id == acc_id))).scalar_one_or_none()
        proxy = (await db.execute(select(Proxy)
                 .where(Proxy.id == acc.proxy_id))).scalar_one_or_none() if acc else None
    if not acc or not proxy:
        итог["итог"] = "нет прокси"
        return итог
    if not acquire_ip_lock(proxy, owner=acc_id):
        итог["итог"] = "адрес занят"
        return итог
    if not acquire_account_lock(acc_id, ttl=600):
        итог["итог"] = "аккаунт занят"
        return итог

    client = make_telethon_client(acc, proxy)
    if client is None:
        release_account_lock(acc_id)
        итог["итог"] = "нет сессии"
        return итог
    try:
        await client.connect()
        if not await client.is_user_authorized():
            итог["итог"] = "сессия мертва"
            return итог

        me = await client.get_me()
        итог["имя"] = ("%s %s" % (me.first_name or "", me.last_name or "")).strip()
        итог["юзер"] = me.username or ""

        # Сверяем базу с тем, что реально в Telegram. Ник могли поставить
        # руками или прежним заходом, а таблица accounts об этом не знала
        # — и всё, что смотрит в базу, считало аккаунт безымянным.
        try:
            from sqlalchemy import text as _t
            async with Session() as _db:
                было = (await _db.execute(_t(
                    "SELECT username FROM accounts WHERE id=:i"),
                    {"i": acc_id})).scalar()
                if (было or "") != итог["юзер"]:
                    await _db.execute(_t(
                        "UPDATE accounts SET username=:u WHERE id=:i"),
                        {"u": итог["юзер"] or None, "i": acc_id})
                    await _db.commit()
                    итог["что_сделал"].append("сверил базу")
        except Exception:
            pass

        # Свои каналы: только те, что аккаунт создал сам.
        свои = []
        async for d in client.iter_dialogs(limit=200):
            e = d.entity
            if getattr(e, "broadcast", False) and getattr(e, "creator", False):
                свои.append(e)
        итог["каналов"] = len(свои)

        # Починка: прикрепляем самый ранний — он настоящий, с историей.
        if чинить and len(свои) > 1:
            старый = min(свои, key=lambda e: e.date)
            await client(UpdatePersonalChannelRequest(channel=старый))
            итог["что_сделал"].append("прикрепил старый")
            await asyncio.sleep(random.uniform(1.5, 3))

        # Юзернейм, если его нет.
        if давать_юзер and not итог["юзер"]:
            for база in random.sample(ЮЗЕРЫ, len(ЮЗЕРЫ)):
                готово = False
                for _ in range(3):
                    кандидат = база + str(random.randint(10, 99999))
                    try:
                        await client(UpdateUsernameRequest(username=кандидат))
                        итог["юзер"] = кандидат
                        итог["что_сделал"].append("выдал юзернейм")
                        # Записываем и в базу: раньше ник менялся только
                        # в Telegram, а таблица accounts оставалась со
                        # старым значением — и всё, что смотрит в базу,
                        # видело «ника нет».
                        try:
                            from sqlalchemy import text as _t
                            async with Session() as _db:
                                await _db.execute(_t(
                                    "UPDATE accounts SET username=:u WHERE id=:i"),
                                    {"u": кандидат, "i": acc_id})
                                await _db.commit()
                        except Exception:
                            pass
                        готово = True
                        break
                    except (terr.UsernameOccupiedError, terr.UsernameInvalidError):
                        await asyncio.sleep(random.uniform(1, 2))
                    except Exception as e:
                        итог["что_сделал"].append("юзер не вышел: %s" % type(e).__name__)
                        готово = True
                        break
                if готово:
                    break
            await asyncio.sleep(random.uniform(1.5, 3))

        # Что в итоге прикреплено к профилю.
        try:
            full = await client(GetFullUserRequest(me))
            pid = getattr(full.full_user, "personal_channel_id", None)
            if pid:
                for e in свои:
                    if e.id == pid:
                        итог["канал"] = "%s|%s" % (
                            getattr(e, "username", "") or "?", e.title)
                        break
                else:
                    итог["канал"] = "id:%s" % pid
        except Exception:
            pass

        итог["итог"] = "ок"
        return итог
    except Exception as e:
        итог["итог"] = "сбой %s" % type(e).__name__
        return итог
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        release_account_lock(acc_id)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", default="", help="кому прикрепить старый канал")
    ap.add_argument("--username", default="", help="кому выдать юзернейм")
    ap.add_argument("--read", default="", help="кого просто прочитать")
    a = ap.parse_args()

    from sqlalchemy import text as sa_text
    from utils.db_pool import async_session as Session

    разбор = lambda s: [int(x) for x in s.split(",") if x.strip()]
    чинить = set(разбор(a.fix))
    юзер = set(разбор(a.username))
    читать = set(разбор(a.read))
    все = sorted(чинить | юзер | читать)

    async with Session() as db:
        адрес = dict((r[0], "%s:%s" % (r[1], r[2])) for r in (
            await db.execute(sa_text("""
                SELECT a.id, COALESCE(p.host,'?'), COALESCE(p.port,0)
                FROM accounts a LEFT JOIN proxies p ON p.id = a.proxy_id
                WHERE a.id = ANY(:i)"""), {"i": все})).all())

    по_адресам = defaultdict(list)
    for i in все:
        по_адресам[адрес.get(i, "?")].append(i)

    print("аккаунтов %d, адресов %d — работают одновременно"
          % (len(все), len(по_адресам)), flush=True)
    результаты = []

    async def очередь(список):
        for i in список:
            for _ in range(6):
                r = await обработать(i, i in чинить, i in юзер)
                if r["итог"] not in ("адрес занят", "аккаунт занят"):
                    break
                await asyncio.sleep(random.uniform(30, 60))
            результаты.append(r)
            если = ", ".join(r["что_сделал"])
            print("[%s] acc#%-5s %-12s %s"
                  % (datetime.now().strftime("%H:%M:%S"), r["acc"], r["итог"],
                     если), flush=True)
            await asyncio.sleep(random.uniform(2, 6))

    await asyncio.gather(*[очередь(v) for v in по_адресам.values()])

    print()
    print("=" * 96)
    print("%-8s %-24s %-24s %s" % ("акк", "имя", "юзернейм", "прикреплённый канал"))
    print("=" * 96)
    for r in sorted(результаты, key=lambda x: x["acc"]):
        if r["итог"] != "ок":
            print("%-8s %s" % ("#%d" % r["acc"], r["итог"]))
            continue
        u = ("t.me/" + r["юзер"]) if r["юзер"] else "—"
        ch = r["канал"].split("|")
        канал = ("t.me/%s  %s" % (ch[0], ch[1])) if len(ch) == 2 else (r["канал"] or "НЕТ")
        пометка = "  (каналов %d)" % r["каналов"] if r["каналов"] > 1 else ""
        print("%-8s %-24s %-24s %s%s"
              % ("#%d" % r["acc"], r["имя"][:24], u[:24], канал[:44], пометка))


asyncio.run(main())
