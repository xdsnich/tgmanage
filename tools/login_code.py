# -*- coding: utf-8 -*-
"""Достать код входа, который Telegram прислал в приложение.

Когда аккаунт входит с нового устройства, код приходит не по SMS, а
сообщением от служебного Telegram (777000) в уже работающую сессию. Этот
скрипт подключается нашей сессией и показывает последний такой код.

Чтение и только чтение: код не подтверждаем, ничего не меняем.

Использование:
    python login_code.py --acc 48            # показать что есть сейчас
    python login_code.py --acc 48 --wait 180 # подождать новый до 3 минут
"""
import asyncio
import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

СЛУЖБА = 777000
KYIV = timezone(timedelta(hours=3))
RE_CODE = re.compile(r"\b(\d{5,6})\b")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--acc", type=int, required=True)
    ap.add_argument("--wait", type=int, default=0,
                    help="сколько секунд ждать нового кода")
    a = ap.parse_args()

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
               .where(TelegramAccount.id == a.acc))).scalar_one_or_none()
        proxy = (await db.execute(select(Proxy)
                 .where(Proxy.id == acc.proxy_id))).scalar_one_or_none() if acc else None
    if not acc or not proxy:
        print("нет аккаунта или прокси")
        return

    for _ in range(30):
        if acquire_ip_lock(proxy, owner=a.acc) and \
                acquire_account_lock(a.acc, ttl=600):
            break
        print("адрес занят, жду…", flush=True)
        await asyncio.sleep(20)
    else:
        print("не дождался свободного адреса")
        return

    client = make_telethon_client(acc, proxy)
    if client is None:
        release_account_lock(a.acc)
        print("нет сессии")
        return

    try:
        await client.connect()
        if not await client.is_user_authorized():
            print("сессия мертва")
            return
        me = await client.get_me()
        print("подключён: %s @%s  (%s)"
              % (me.first_name, me.username, acc.phone), flush=True)
        print(flush=True)

        async def показать(предел=5):
            найдено = []
            async for m in client.iter_messages(СЛУЖБА, limit=предел):
                if not m.text:
                    continue
                код = RE_CODE.search(m.text.replace("\n", " "))
                if not код:
                    continue
                когда = m.date.astimezone(KYIV)
                возраст = (datetime.now(timezone.utc) - m.date).total_seconds()
                найдено.append((код.group(1), когда, возраст))
            return найдено

        было = await показать()
        if было:
            print("последние коды в приложении:")
            for код, когда, возраст in было:
                метка = "  ← свежий" if возраст < 300 else ""
                print("   %s   пришёл %s (%.0f мин назад)%s"
                      % (код, когда.strftime("%H:%M:%S"), возраст / 60, метка))
        else:
            print("кодов в переписке со служебным Telegram нет")

        if a.wait:
            print(flush=True)
            print("жду новый код до %d секунд — начинай вход…" % a.wait, flush=True)
            известные = {к for к, _, _ in было}
            до = datetime.now() + timedelta(seconds=a.wait)
            while datetime.now() < до:
                await asyncio.sleep(5)
                for код, когда, возраст in await показать(3):
                    if код not in известные and возраст < 300:
                        print(flush=True)
                        print("=" * 44)
                        print("   КОД ВХОДА:  %s" % код)
                        print("=" * 44)
                        return
            print("новый код так и не пришёл")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        release_account_lock(a.acc)


asyncio.run(main())
