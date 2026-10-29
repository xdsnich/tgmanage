# -*- coding: utf-8 -*-
"""Найти предел: сколько каналов влезает в одну ссылку на папку.

Пятьдесят проходят, девяносто четыре — нет, причём отказ приходит и на
свежем аккаунте, который ссылок ещё не делал. Значит упираемся не в
накопленные ссылки, а в размер пачки.

Идём сверху вниз с паузами: каждая попытка создать ссылку копит флуд, и
без пауз мы сломаем себе опыт, как уже было.

Папку и ссылку убираем после каждой попытки — иначе упрёмся в предел
папок (их у обычного аккаунта десять).

Запуск из папки api:
    python ..\tools\folder_limit.py --donor 159 --no-proxy
"""
import argparse
import asyncio
import os
import random
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

ПАУЗА = 45          # между попытками, чтобы не копить флуд
РАЗМЕРЫ = [60, 70, 80, 90, 100]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--donor", type=int, required=True)
    ap.add_argument("--no-proxy", action="store_true")
    a = ap.parse_args()

    sys.argv = [sys.argv[0]]        # чтобы чужой парсер не мешал
    from folder_test import клиент  # переиспользуем подключение
    from sqlalchemy import text as sa_text
    from utils.db_pool import async_session as Session
    from utils.account_lock import release_account_lock
    from telethon.tl.functions.messages import UpdateDialogFilterRequest
    from telethon.tl.functions.chatlists import (
        ExportChatlistInviteRequest, GetExportedInvitesRequest,
        DeleteExportedInviteRequest)
    from telethon.tl.types import (
        DialogFilter, InputChatlistDialogFilter, TextWithEntities)

    async with Session() as db:
        каналы = [x[0] for x in (await db.execute(sa_text("""
            SELECT channel_username FROM campaign_channel_assignments
            WHERE account_id = :d AND status = 'joined'
            ORDER BY random() LIMIT 100"""), {"d": a.donor})).all()]
    print("у донора каналов под рукой: %d" % len(каналы))

    упёрлись = [None]
    клиент_, err = await клиент(a.donor, без_прокси=a.no_proxy)
    if клиент_ is None:
        print("донор: %s" % err)
        return
    предел = None
    try:
        await клиент_.connect()
        # Разрешаем каналы один раз — дальше только режем список.
        peers = []
        приватных = [0]
        for ch in каналы:
            try:
                ent = await клиент_.get_entity(ch)
                # Приватный канал в общую папку не положить, и отказ
                # прилетит на всю пачку. Публичный узнаётся по адресу.
                if not getattr(ent, "username", None):
                    приватных[0] += 1
                    continue
                peers.append(await клиент_.get_input_entity(ent))
            except Exception:
                pass
            await asyncio.sleep(0.12)
        print("доступных: %d (приватных отсеяно: %d)"
              % (len(peers), приватных[0]))
        print()

        for размер in РАЗМЕРЫ:
            if размер > len(peers):
                continue
            # Случайная выборка, а не начало списка: иначе один и тот же
            # испорченный канал попадает в каждую попытку и выглядит
            # как предел по размеру.
            набор = random.sample(peers, размер)
            номер = random.randint(50, 90)
            try:
                await клиент_(UpdateDialogFilterRequest(
                    id=номер,
                    filter=DialogFilter(
                        id=номер,
                        title=TextWithEntities(text="lim%d" % номер, entities=[]),
                        pinned_peers=[], include_peers=набор, exclude_peers=[])))
            except Exception as e:
                print("  %3d — папку не создать: %s" % (размер, type(e).__name__))
                continue
            await asyncio.sleep(2)

            итог = None
            try:
                res = await клиент_(ExportChatlistInviteRequest(
                    chatlist=InputChatlistDialogFilter(filter_id=номер),
                    title="lim%d" % номер, peers=набор))
                итог = "ПРОШЛО  " + res.invite.url
                предел = размер
            except Exception as e:
                текст = str(e)
                if "INVITES_TOO_MUCH" in текст:
                    итог = "ПРЕДЕЛ ПО КОЛИЧЕСТВУ (INVITES_TOO_MUCH)"
                    упёрлись[0] = размер
                elif type(e).__name__ == "ChannelPrivateError":
                    итог = "испорченный канал в выборке (не про размер)"
                elif type(e).__name__ == "FloodWaitError":
                    итог = "флуд: " + текст[:50]
                    упёрлись[0] = -1
                else:
                    итог = "отказ " + type(e).__name__
            print("  %3d каналов — %s" % (размер, итог))

            # Убираем за собой: и ссылку, и папку.
            try:
                инв = await клиент_(GetExportedInvitesRequest(
                    chatlist=InputChatlistDialogFilter(filter_id=номер)))
                for i in (getattr(инв, "invites", []) or []):
                    await клиент_(DeleteExportedInviteRequest(
                        chatlist=InputChatlistDialogFilter(filter_id=номер),
                        slug=i.url.rstrip("/").split("/")[-1]))
                    await asyncio.sleep(0.5)
            except Exception:
                pass
            try:
                await клиент_(UpdateDialogFilterRequest(id=номер, filter=None))
            except Exception:
                pass

            if упёрлись[0]:
                break          # дальше пробовать нечего
            await asyncio.sleep(ПАУЗА)
    finally:
        try:
            await клиент_.disconnect()
        except Exception:
            pass
        release_account_lock(a.donor)

    print()
    if предел:
        print("НАИБОЛЬШИЙ ПРОШЕДШИЙ РАЗМЕР: %d каналов" % предел)
    if упёрлись[0] and упёрлись[0] > 0:
        print("ПРЕДЕЛ по количеству начинается с %d" % упёрлись[0])
    elif упёрлись[0] == -1:
        print("остановлено флудом — повторить позже")
    if not предел and not упёрлись[0]:
        print("ни один размер не прошёл — мешали испорченные каналы")


asyncio.run(main())
