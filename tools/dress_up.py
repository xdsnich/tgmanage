# -*- coding: utf-8 -*-
"""Оформить аккаунт так же, как оформлены рабочие.

Двадцать аккаунтов в кампании стоят пустыми: без аватара, без имени по
образцу и без своего канала. Человек, пришедший по комментарию, попадает
в голый профиль и дальше не идёт — подписка, потраченная на такой аккаунт,
пропадает зря.

Собираем ровно то, что есть у действующих (сверено по выгрузке профилей):
  • аватар;
  • имя из того же набора, что у рабочих: «Amelia Taylor ❤️», «Ameli 💋»
    и так далее — единый узнаваемый образ на всех аккаунтах;
  • свой канал с названием в стиле Amelia и адресом amelie_link_*;
  • канал прикреплён к профилю, чтобы висел прямо под именем.

Всё делается за ОДНО подключение на аккаунт, с паузами между шагами.
Перед первым постом клиент отключается и подключается заново: на свежем
канале первый пост иначе режется флуд-фильтром — так же поступает штатный
_create_channel_step_by_step в bulk_tasks.

Ссылку ставим общую. Персональные раздаёт потом funnel_links.py — там своя
трёхфазная процедура, чтобы админ создавал их одним заходом.
"""
import asyncio
import os
import sys
import random
import argparse
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

ФОТО = r"C:\Users\ufo001\Downloads\photos_Poly_ava"
РЕВИЗИЯ = r"C:\Users\ufo001\AppData\Local\Temp\spambot_audit.json"
ССЫЛКА = "https://t.me/+svfGMyv7w5hiM2Ey"
# Описание профиля и текст поста НЕ сочиняем. Комментарии уходят в каналы
# на разных языках, и любая фраза на одном из них сузила бы охват: русский
# текст в профиле у человека, пишущего под персидским постом, выглядит
# чужеродно. Оставляем только ссылку — она понятна везде.
ОПИСАНИЕ = ""
ПОСТ = ССЫЛКА

# Имена профилей — ровно тот набор, что уже используется на рабочих
# аккаунтах. Первое слово идёт в имя, остальное в фамилию, поэтому в
# Telegram строка отображается точно так же, как записана здесь.
ИМЕНА_СТРОКИ = [
    "Amelia Taylor ❤️", "Ameli Taylor 💋",
    "Amelita Taylor ✨", "Amy Taylor 💖",
    "Amelinka Taylor 💕", "Ami Taylor 💘",
    "Amelias Taylor 🎀", "Amelka Taylor 🍭",
]
ИМЕНА = [tuple(x.split(" ", 1)) if " " in x else (x, "")
         for x in ИМЕНА_СТРОКИ]
НАЗВАНИЯ = [
    "Amelie 🦋", "Ameli 💓", "Amelka 🍭", "Amyy ⚡", "Amel 🌊", "Ameliya 🌙",
    "Amelita ☘️", "Ami 💘", "Amelinka 💕", "Amela 🌈", "Amiee 🖤", "Am 🌸",
    "Ameliaa 💓", "Amell ☀️", "Ameliah 💗", "Amy 💖", "Amiii 🌹", "Amelitt 🤍",
    "Ameliana 🥂", "Ameliya 💎",
]
АДРЕСА = ["amelie_link", "amelie_linker", "amelie_lnk", "amelie_lnkr"]
# Юзернеймы самих профилей. Нужны, чтобы аккаунт можно было открыть по
# прямой ссылке t.me/… и проверить глазами — без него профиль доступен
# только из комментария или через прикреплённый канал.
ЮЗЕРЫ = ["amelia_taylor", "ameli_taylor", "amelia_t", "ameli_t",
         "amelita_t", "amy_taylor", "amelinka_t", "ami_taylor"]


async def оформить(acc_id, фото_профиль, фото_канал, имя, фамилия, название,
                   только_имя=False, ставить_юзер=False,
                   без_канала=False):
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as Session
    from utils.telegram import make_telethon_client
    from utils.ip_throttle import acquire_ip_lock
    from utils.account_lock import acquire_account_lock, release_account_lock
    from telethon.tl.functions.account import (
        UpdateProfileRequest, UpdatePersonalChannelRequest)
    from telethon.tl.functions.photos import UploadProfilePhotoRequest
    from telethon.tl.functions.channels import (
        CreateChannelRequest, EditPhotoRequest, UpdateUsernameRequest)
    from telethon.tl.types import InputChatUploadedPhoto
    from telethon import errors as terr

    async with Session() as db:
        acc = (await db.execute(select(TelegramAccount)
               .options(joinedload(TelegramAccount.api_app))
               .where(TelegramAccount.id == acc_id))).scalar_one_or_none()
        proxy = (await db.execute(select(Proxy)
                 .where(Proxy.id == acc.proxy_id))).scalar_one_or_none() if acc else None
    if not acc or not proxy:
        return {"acc": acc_id, "итог": "нет аккаунта или прокси"}
    if not acquire_ip_lock(proxy, owner=acc_id):
        return {"acc": acc_id, "итог": "адрес занят"}
    if not acquire_account_lock(acc_id, ttl=900):
        return {"acc": acc_id, "итог": "аккаунт занят"}

    сделано = []
    client = make_telethon_client(acc, proxy)
    if client is None:
        release_account_lock(acc_id)
        return {"acc": acc_id, "итог": "клиент не создан"}
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return {"acc": acc_id, "итог": "сессия мертва"}

        # 1. Имя.
        # about передаём пустым намеренно: если в профиле уже было
        # описание, оно сотрётся, а не останется от прежнего владельца.
        await client(UpdateProfileRequest(
            first_name=имя, last_name=фамилия, about=ОПИСАНИЕ))
        сделано.append("имя")
        await asyncio.sleep(random.uniform(2, 5))

        # 1б. Юзернейм профиля.
        if ставить_юзер:
            from telethon.tl.functions.account import (
                UpdateUsernameRequest as UpdUser)
            for база in random.sample(ЮЗЕРЫ, len(ЮЗЕРЫ)):
                готово_ = False
                for _ in range(3):
                    кандидат = база + str(random.randint(10, 99999))
                    try:
                        await client(UpdUser(username=кандидат))
                        сделано.append("@" + кандидат)
                        готово_ = True
                        break
                    except (terr.UsernameOccupiedError,
                            terr.UsernameInvalidError):
                        await asyncio.sleep(random.uniform(1, 2))
                    except Exception as e:
                        сделано.append("юзер не вышел: %s" % type(e).__name__)
                        готово_ = True
                        break
                if готово_:
                    break
            await asyncio.sleep(random.uniform(2, 4))

        if без_канала:
            f2 = await client.upload_file(фото_профиль)
            await client(UploadProfilePhotoRequest(file=f2))
            сделано.append("аватар")
            return {"acc": acc_id, "итог": "готово", "шаги": сделано}

        # Аккаунту, у которого канал уже есть, больше ничего не нужно:
        # второй канал ему не завести, а менять аватар работающему профилю
        # незачем — люди его уже видели.
        if только_имя:
            return {"acc": acc_id, "итог": "готово", "шаги": сделано}

        # 2. Аватар.
        f = await client.upload_file(фото_профиль)
        await client(UploadProfilePhotoRequest(file=f))
        сделано.append("аватар")
        await asyncio.sleep(random.uniform(2, 5))

        # 3. Канал.
        resp = await client(CreateChannelRequest(
            title=название, about="", broadcast=True, megagroup=False))
        ch = resp.chats[0]
        сделано.append("канал")
        await asyncio.sleep(random.uniform(2, 4))

        # 4. Адрес канала. Занятые перебираем с числовым хвостом.
        адрес = None
        for база in random.sample(АДРЕСА, len(АДРЕСА)):
            for _ in range(4):
                кандидат = база + str(random.randint(100, 99999))
                try:
                    await client(UpdateUsernameRequest(channel=ch, username=кандидат))
                    адрес = кандидат
                    break
                except (terr.UsernameOccupiedError, terr.UsernameInvalidError):
                    await asyncio.sleep(random.uniform(1, 2))
            if адрес:
                break
        if адрес:
            сделано.append("@" + адрес)
        await asyncio.sleep(random.uniform(2, 4))

        # 5. Аватар канала — другой снимок, не тот же, что в профиле.
        try:
            cf = await client.upload_file(фото_канал)
            await client(EditPhotoRequest(
                channel=ch, photo=InputChatUploadedPhoto(file=cf)))
            сделано.append("фото канала")
        except Exception:
            pass
        await asyncio.sleep(random.uniform(2, 4))

        # 6. Прикрепляем канал к профилю.
        try:
            await client(UpdatePersonalChannelRequest(channel=ch))
            сделано.append("прикреплён")
        except Exception as e:
            сделано.append("не прикрепился: %s" % type(e).__name__)

        # 7. Первый пост — только после переподключения.
        await client.disconnect()
        await asyncio.sleep(random.uniform(3, 6))
        await client.connect()
        try:
            await client.send_message(ch, ПОСТ, link_preview=False)
            сделано.append("пост")
        except Exception as e:
            сделано.append("пост не ушёл: %s" % type(e).__name__)

        return {"acc": acc_id, "итог": "готово", "адрес": адрес, "шаги": сделано}
    except Exception as e:
        return {"acc": acc_id,
                "итог": "сбой %s: %s" % (type(e).__name__, str(e)[:90]),
                "шаги": сделано}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        release_account_lock(acc_id)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", required=True)
    ap.add_argument("--gap-min", type=int, default=4)
    ap.add_argument("--gap-max", type=int, default=11)
    ap.add_argument("--with-username", action="store_true",
                    help="заодно выдать профилю юзернейм")
    ap.add_argument("--no-channel", action="store_true",
                    help="имя, ник и аватар — канал не создавать")
    ap.add_argument("--rename-only", action="store_true",
                    help="только имя, канал и аватар не трогать")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    ids = [int(x) for x in a.ids.split(",") if x.strip()]

    # Ограниченных не трогаем. Оформлять аккаунт, которому Telegram
    # запретил работать, бессмысленно: заходы всё равно уйдут в отказ, а
    # лишнее подключение к ограниченному профилю — последнее, что ему
    # сейчас нужно. Вердикт берём из последней ревизии спам-бота.
    try:
        import json as _js
        вердикт = {int(x["acc"]): x.get("verdict")
                   for x in _js.load(open(РЕВИЗИЯ, encoding="utf-8"))}
        плохие = [i for i in ids if вердикт.get(i) == "ОГРАНИЧЕН"]
        if плохие:
            print("пропускаю ограниченных (%d): %s"
                  % (len(плохие), ", ".join("#%d" % i for i in плохие)))
            ids = [i for i in ids if i not in плохие]
    except Exception:
        print("ревизия спам-бота недоступна — фильтр не применён")
    файлы = [os.path.join(ФОТО, f) for f in sorted(os.listdir(ФОТО))
             if f.lower().endswith((".jpg", ".jpeg", ".png"))]
    if not файлы:
        print("в папке нет фотографий")
        return

    random.shuffle(файлы)
    имена = ИМЕНА[:]
    названия = НАЗВАНИЯ[:]
    random.shuffle(имена)
    random.shuffle(названия)

    задания = []
    for i, aid in enumerate(ids):
        задания.append((aid,
                        файлы[i % len(файлы)],
                        файлы[(i + len(файлы) // 2) % len(файлы)],
                        имена[i % len(имена)][0], имена[i % len(имена)][1],
                        названия[i % len(названия)]))

    print("аккаунтов: %d, фотографий в папке: %d" % (len(ids), len(файлы)))
    print()
    for aid, pa, ca, fn, ln, t in задания[:6]:
        print("   acc#%-5s %-18s канал %-14s ава %s"
              % (aid, ("%s %s" % (fn, ln)).strip(), t, os.path.basename(pa)[:26]))
    if len(задания) > 6:
        print("   … и ещё %d" % (len(задания) - 6))

    if not a.apply:
        print()
        print("показ без изменений. Запусти с --apply.")
        return

    print()
    # Идём ПАРАЛЛЕЛЬНО по адресам. Ограничение — одно подключение на
    # адрес, а не одно на всю программу: аккаунты на разных прокси друг
    # другу не мешают вовсе. Поэтому на каждый адрес заводим свою
    # очередь, внутри неё строго по одному, а сами очереди работают
    # одновременно. При тридцати с лишним адресах это разница между
    # четырьмя часами и двадцатью минутами.
    from sqlalchemy import text as sa_text
    from utils.db_pool import async_session as Session2
    async with Session2() as db:
        адрес_акка = dict((r[0], "%s:%s" % (r[1], r[2])) for r in (
            await db.execute(sa_text("""
                SELECT a.id, COALESCE(p.host,'?'), COALESCE(p.port,0)
                FROM accounts a LEFT JOIN proxies p ON p.id = a.proxy_id
                WHERE a.id = ANY(:i)"""), {"i": ids})).all())

    по_адресам = {}
    for з in задания:
        по_адресам.setdefault(адрес_акка.get(з[0], "?"), []).append(з)

    итоги = {"готово": 0, "сбой": 0}

    async def очередь_адреса(адрес, список):
        for aid, pa, ca, fn, ln, t in список:
            # Занятый адрес — не отказ, а «зайди позже»: его делят с
            # комментингом и подписками. Ждём и пробуем снова.
            for попытка in range(25):
                r = await оформить(aid, pa, ca, fn, ln, t,
                                   только_имя=a.rename_only,
                                   ставить_юзер=a.with_username,
                           без_канала=a.no_channel)
                if r["итог"] not in ("адрес занят", "аккаунт занят"):
                    break
                await asyncio.sleep(random.uniform(45, 90))
            print("[%s] %-22s acc#%-5s %-10s %s"
                  % (datetime.now().strftime("%H:%M:%S"), адрес[:22], r["acc"],
                     r["итог"], ", ".join(r.get("шаги", []))[:60]), flush=True)
            итоги["готово" if r["итог"] == "готово" else "сбой"] += 1
            await asyncio.sleep(random.uniform(a.gap_min * 60, a.gap_max * 60))

    print("адресов задействовано: %d, работают одновременно" % len(по_адресам))
    print()
    await asyncio.gather(*[очередь_адреса(k, v) for k, v in по_адресам.items()])
    print()
    print("итог: готово %d, не вышло %d" % (итоги["готово"], итоги["сбой"]))


asyncio.run(main())
