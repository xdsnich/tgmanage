# -*- coding: utf-8 -*-
"""Проходят ли реакции НА КОММЕНТАРИИ — единственное, что даёт трафик.

Реакция на пост нас не интересует: она уже проверена, проходит всегда, но
висит в списке реагирующих под постом канала, куда мало кто заглядывает.
Трафик даёт реакция В ЧАТЕ: там люди сидят и переписываются, там видно,
кто на что отреагировал, и оттуда кликают в профиль.

Почему прошлый прогон дал ложные отказы. Он брал заранее выбранный пост
и, если у того не оказывалось обсуждения, записывал MsgIdInvalidError —
как будто чат отказал. На самом деле у поста просто не было комментариев.

Здесь перебираем несколько последних постов, пока не найдётся такой, у
которого обсуждение есть и в нём кто-то писал. И только тогда пробуем
реакцию. Отказ теперь означает именно отказ, а не неудачный выбор поста.
"""
import asyncio
import io
import json
import os
import random
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

ПАПКА = (r"C:\Users\ufo001\AppData\Local\Temp\claude"
         r"\c--Users-ufo001-source-repos-tg-manager1"
         r"\defc6c28-f948-4b88-ba84-af9b175b96fd\scratchpad")
ПОСТОВ_ПЕРЕБРАТЬ = 8


async def проверить(acc_id, channel):
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as Session
    from utils.telegram import make_telethon_client
    from utils.ip_throttle import acquire_ip_lock
    from utils.account_lock import acquire_account_lock, release_account_lock

    и = {"акк": acc_id, "канал": channel, "постов_перебрал": 0,
         "нашёл_обсуждение": False, "комментов": 0, "итог": None, "прим": ""}

    async with Session() as db:
        acc = (await db.execute(select(TelegramAccount)
               .options(joinedload(TelegramAccount.api_app))
               .where(TelegramAccount.id == acc_id))).scalar_one_or_none()
        proxy = (await db.execute(select(Proxy)
                 .where(Proxy.id == acc.proxy_id))).scalar_one_or_none() if acc else None
    if not acc or not proxy:
        и["итог"] = "нет прокси"
        return и
    if not acquire_ip_lock(proxy, owner=acc_id):
        и["итог"] = "адрес занят"
        return и
    if not acquire_account_lock(acc_id, ttl=420):
        и["итог"] = "аккаунт занят"
        return и

    client = make_telethon_client(acc, proxy)
    if client is None:
        release_account_lock(acc_id)
        и["итог"] = "нет сессии"
        return и
    try:
        await client.connect()
        if not await client.is_user_authorized():
            и["итог"] = "сессия мертва"
            return и

        from tasks.comment_executor import _get_available_reactions
        from telethon.tl.functions.messages import (
            SendReactionRequest, GetDiscussionMessageRequest)
        from telethon.tl.types import ReactionEmoji
        from telethon.errors import ReactionInvalidError

        try:
            entity = await client.get_entity(channel)
        except Exception as e:
            и["итог"] = "канал не открылся: " + type(e).__name__
            return и

        посты = await client.get_messages(entity, limit=ПОСТОВ_ПЕРЕБРАТЬ)
        if not посты:
            и["итог"] = "нет постов"
            return и

        # Ищем пост, у которого обсуждение есть И в нём кто-то писал.
        чат = комменты = None
        for пост in посты:
            и["постов_перебрал"] += 1
            try:
                disc = await client(GetDiscussionMessageRequest(
                    peer=entity, msg_id=пост.id))
            except Exception:
                continue
            if not (disc and disc.messages):
                continue
            корень = disc.messages[0]
            try:
                ч = await client.get_entity(корень.peer_id)
                к = [c for c in await client.get_messages(
                    ч, limit=8, reply_to=корень.id) if c.id != корень.id]
            except Exception as e:
                и["итог"] = "чат недоступен: " + type(e).__name__
                return и
            if к:
                чат, комменты = ч, к
                и["нашёл_обсуждение"] = True
                и["комментов"] = len(к)
                break
            await asyncio.sleep(random.uniform(0.6, 1.4))

        if not комменты:
            и["итог"] = "ни у одного поста нет комментариев"
            return и

        разр = await _get_available_reactions(client, чат)
        if not разр:
            и["итог"] = "в чате реакции отключены"
            return и

        # Реагируем на СЛУЧАЙНЫЙ комментарий, а не на первый: при повторных
        # прогонах реакция на тот же снялась бы, и мы приняли бы это за отказ.
        цель = random.choice(комменты)
        for e in random.sample(разр, min(4, len(разр))):
            try:
                await client(SendReactionRequest(
                    peer=чат, msg_id=цель.id,
                    reaction=[ReactionEmoji(emoticon=e)]))
                и["итог"] = "ПРОШЛА " + e
                return и
            except ReactionInvalidError:
                continue
            except Exception as ex:
                и["итог"] = "ОТКАЗ " + type(ex).__name__
                return и
        и["итог"] = "все эмодзи отвергнуты"
        return и
    except Exception as e:
        и["итог"] = "сбой " + type(e).__name__
        return и
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        release_account_lock(acc_id)


async def main():
    пары = json.load(io.open(os.path.join(ПАПКА, "retry_pairs.json"),
                             encoding="utf-8"))
    from sqlalchemy import text as sa_text
    from utils.db_pool import async_session as Session
    async with Session() as db:
        адрес = dict((r[0], r[1]) for r in (await db.execute(sa_text("""
            SELECT a.id, COALESCE(p.host,'?') FROM accounts a
            LEFT JOIN proxies p ON p.id=a.proxy_id WHERE a.id = ANY(:i)"""),
            {"i": [p["акк"] for p in пары]})).all())

    по_адресам = defaultdict(list)
    for p in пары:
        по_адресам[адрес.get(p["акк"], "?")].append(p)
    print("проверяем РЕАКЦИИ В ЧАТЕ: %d пар на %d адресах"
          % (len(пары), len(по_адресам)), flush=True)
    print(flush=True)

    итоги = []

    async def очередь(список):
        for p in список:
            r = None
            for _ in range(25):
                r = await проверить(p["акк"], p["канал"])
                if "занят" not in (r.get("итог") or ""):
                    break
                await asyncio.sleep(random.uniform(25, 45))
            итоги.append(r)
            print("   acc#%-5s @%-22s постов %d, комментов %-3d → %s"
                  % (r["акк"], r["канал"][:22], r["постов_перебрал"],
                     r["комментов"], r["итог"]), flush=True)

    await asyncio.gather(*[очередь(v) for v in по_адресам.values()])

    прошло = sum(1 for x in итоги if (x.get("итог") or "").startswith("ПРОШЛА"))
    бан = sum(1 for x in итоги if "Banned" in (x.get("итог") or ""))
    закрыт = sum(1 for x in итоги if "WriteForbidden" in (x.get("итог") or ""))
    нет_обс = sum(1 for x in итоги if "комментариев" in (x.get("итог") or "")
                  or "недоступен" in (x.get("итог") or ""))
    print()
    print("=" * 64)
    print("  РЕАКЦИЯ В ЧАТЕ ПРОШЛА:      %d из %d" % (прошло, len(итоги)))
    print("  бан аккаунта:                %d" % бан)
    print("  чат закрыт на запись всем:   %d" % закрыт)
    print("  обсуждения нет / недоступно: %d" % нет_обс)
    json.dump(итоги, io.open(os.path.join(ПАПКА, "chat_react_result.json"),
                             "w", encoding="utf-8"), ensure_ascii=False, indent=1)


asyncio.run(main())
