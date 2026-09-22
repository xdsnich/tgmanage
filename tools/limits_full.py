# -*- coding: utf-8 -*-
"""Полный ответ @SpamBot по каждому аккаунту — целиком, без сокращений.

Прежний разбор вытаскивал из ответа только строчку «limited until …» и,
если её не было, писал «срок не назван». Но бот в таком случае пишет не
пустоту, а другой текст — и в нём-то и объяснение: бывает «ограничение
снимут вручную после проверки», бывает «пиши в поддержку», бывает совсем
иное. Здесь печатаем ответ целиком.

Заодно спрашиваем у бота продолжение: на сообщение с ограничением он
обычно даёт кнопку обжалования, и её наличие тоже говорит о характере
ограничения.
"""
import asyncio
import argparse
import os
import random
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

KYIV = timezone(timedelta(hours=3))
RE_UNTIL = re.compile(
    r"limited until ([0-9]{1,2} \w{3} [0-9]{4}, [0-9]{1,2}:[0-9]{2})", re.I)


async def спросить(acc_id):
    """Полный текст ответа бота плюс подписи кнопок, если они есть."""
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
        return {"acc": acc_id, "итог": "нет прокси"}
    if not acquire_ip_lock(proxy, owner=acc_id):
        return {"acc": acc_id, "итог": "адрес занят"}
    if not acquire_account_lock(acc_id, ttl=240):
        return {"acc": acc_id, "итог": "аккаунт занят"}

    client = make_telethon_client(acc, proxy)
    if client is None:
        release_account_lock(acc_id)
        return {"acc": acc_id, "итог": "нет сессии"}
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return {"acc": acc_id, "итог": "сессия мертва"}
        async with client.conversation("@SpamBot", timeout=30) as conv:
            await conv.send_message("/start")
            await asyncio.sleep(random.uniform(1.5, 3))
            ответ = await conv.get_response()
        текст = ответ.text or ""
        кнопки = []
        try:
            for ряд in (ответ.buttons or []):
                for b in ряд:
                    кнопки.append(b.text)
        except Exception:
            pass
        низ = текст.lower()
        чист = ("good news" in низ or "no limits" in низ or "free as a bird" in низ)
        m = RE_UNTIL.search(текст)
        return {"acc": acc_id, "итог": "ЧИСТ" if чист else "ОГРАНИЧЕН",
                "срок": m.group(1) if m else None,
                "кнопки": кнопки, "текст": текст, "телефон": acc.phone}
    except Exception as e:
        return {"acc": acc_id, "итог": "сбой %s" % type(e).__name__}
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
        адрес = dict((r[0], r[1]) for r in (await db.execute(sa_text("""
            SELECT a.id, COALESCE(p.host, '?') FROM accounts a
            LEFT JOIN proxies p ON p.id = a.proxy_id WHERE a.id = ANY(:i)"""),
            {"i": ids})).all())

    по_адресам = defaultdict(list)
    for i in ids:
        по_адресам[адрес.get(i, "?")].append(i)
    print("аккаунтов %d на %d адресах" % (len(ids), len(по_адресам)), flush=True)

    итоги = []

    async def очередь(список):
        for i in список:
            r = None
            for _ in range(40):
                r = await спросить(i)
                if "занят" not in str(r.get("итог", "")):
                    break
                await asyncio.sleep(random.uniform(25, 50))
            итоги.append(r)
            print("  acc#%-5s %s" % (i, r.get("итог")), flush=True)

    await asyncio.gather(*[очередь(v) for v in по_адресам.values()])

    print()
    for r in sorted(итоги, key=lambda x: x["acc"]):
        print("=" * 76)
        print("acc#%-5s %-16s %s%s"
              % (r["acc"], r.get("телефон", ""), r.get("итог"),
                 ("   до " + r["срок"]) if r.get("срок") else ""))
        т = (r.get("текст") or "").strip()
        if т:
            print()
            for стр in т.split("\n"):
                print("   " + стр)
        if r.get("кнопки"):
            print()
            print("   кнопки: %s" % " | ".join(r["кнопки"]))
        print()


asyncio.run(main())
