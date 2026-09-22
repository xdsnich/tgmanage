# -*- coding: utf-8 -*-
"""Выгрузить сессию аккаунта строкой — для своего приложения.

Строка StringSession содержит ключ авторизации и номер дата-центра. Её
понимает Telethon и почти всё, что на нём построено.

Ключ авторизации привязан к тому api_id, с которым он был получен, поэтому
вместе со строкой печатаем и api_id с api_hash: с чужими значениями
Telegram может оборвать сессию.

Только чтение: сессию не трогаем и не пересоздаём.
"""
import asyncio
import argparse
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--acc", type=int, required=True)
    a = ap.parse_args()

    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as Session
    from utils.telegram import make_telethon_client
    from utils.ip_throttle import acquire_ip_lock
    from utils.account_lock import acquire_account_lock, release_account_lock
    from telethon.sessions import StringSession

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
                acquire_account_lock(a.acc, ttl=300):
            break
        print("адрес занят, жду…", flush=True)
        await asyncio.sleep(20)
    else:
        print("не дождался свободного адреса")
        return

    client = make_telethon_client(acc, proxy)
    if client is None:
        release_account_lock(a.acc)
        print("нет файла сессии")
        return

    try:
        await client.connect()
        if not await client.is_user_authorized():
            print("сессия мертва — выгружать нечего")
            return
        me = await client.get_me()
        строка = StringSession.save(client.session)

        app = acc.api_app
        print()
        print("=" * 72)
        print("  %s  @%s   %s" % (me.first_name or "", me.username or "—", acc.phone))
        print("=" * 72)
        print()
        print("API_ID    = %s" % getattr(app, "api_id", "?"))
        print("API_HASH  = %s" % getattr(app, "api_hash", "?"))
        print("DC        = %s" % client.session.dc_id)
        print()
        print("SESSION_STRING:")
        print(строка)
        print()
        print("ПРОКСИ (ставь тот же, иначе Telegram сочтёт это угоном):")
        print("  %s:%s" % (proxy.host, proxy.port))
        if proxy.login:
            print("  логин %s  пароль %s" % (proxy.login, proxy.password))
        print()
        print("УСТРОЙСТВО (чтобы не выглядеть новым клиентом):")
        for поле in ("device_model", "system_version", "app_version", "lang_code"):
            зн = getattr(client, "_init_request", None)
            print("  %-16s %s" % (поле, getattr(зн, поле, "—") if зн else "—"))
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        release_account_lock(a.acc)


asyncio.run(main())
