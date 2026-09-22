# -*- coding: utf-8 -*-
"""Не забанили ли нас ЗА САМИ РЕАКЦИИ?

Час назад свежие аккаунты поставили реакции в каналах, где старым писать
запрещено. Вопрос, от которого зависит всё дальнейшее: держится ли это,
или одна реакция уже стоила аккаунту доступа.

Если держится — подписывать полторы тысячи каналов под реакции имеет
смысл. Если аккаунт после первой же реакции получает запрет — вся затея
бессмысленна, и лучше узнать это на десяти парах, чем на тысяче.

Берём ТЕ ЖЕ пары аккаунт-канал и повторяем ровно то же действие. Никаких
вступлений — только реакция: аккаунты уже внутри.
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


async def повторить(acc_id, channel):
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as Session
    from utils.telegram import make_telethon_client
    from utils.ip_throttle import acquire_ip_lock
    from utils.account_lock import acquire_account_lock, release_account_lock

    и = {"акк": acc_id, "канал": channel, "пост": None,
         "чат": None, "прим": ""}

    async with Session() as db:
        acc = (await db.execute(select(TelegramAccount)
               .options(joinedload(TelegramAccount.api_app))
               .where(TelegramAccount.id == acc_id))).scalar_one_or_none()
        proxy = (await db.execute(select(Proxy)
                 .where(Proxy.id == acc.proxy_id))).scalar_one_or_none() if acc else None
    if not acc or not proxy:
        и["прим"] = "нет прокси"
        return и
    if not acquire_ip_lock(proxy, owner=acc_id):
        и["прим"] = "адрес занят"
        return и
    if not acquire_account_lock(acc_id, ttl=300):
        и["прим"] = "аккаунт занят"
        return и

    client = make_telethon_client(acc, proxy)
    if client is None:
        release_account_lock(acc_id)
        и["прим"] = "нет сессии"
        return и
    try:
        await client.connect()
        if not await client.is_user_authorized():
            и["прим"] = "сессия мертва"
            return и

        from tasks.comment_executor import _get_available_reactions
        from telethon.tl.functions.messages import (
            SendReactionRequest, GetDiscussionMessageRequest)
        from telethon.tl.types import ReactionEmoji
        from telethon.errors import ReactionInvalidError

        try:
            entity = await client.get_entity(channel)
        except Exception as e:
            и["прим"] = "канал не открылся: " + type(e).__name__
            return и

        msgs = await client.get_messages(entity, limit=4)
        if not msgs:
            и["прим"] = "нет постов"
            return и
        # Берём ДРУГОЙ пост, а не тот же: реакция на уже отмеченный
        # просто снялась бы, и мы не узнали бы, проходит ли новая.
        цель = msgs[1] if len(msgs) > 1 else msgs[0]

        разр = await _get_available_reactions(client, entity)
        if not разр:
            и["пост"] = "реакции отключены"
        else:
            for e in random.sample(разр, min(3, len(разр))):
                try:
                    await client(SendReactionRequest(
                        peer=entity, msg_id=цель.id,
                        reaction=[ReactionEmoji(emoticon=e)]))
                    и["пост"] = "ПРОШЛА " + e
                    break
                except ReactionInvalidError:
                    continue
                except Exception as ex:
                    и["пост"] = "ОТКАЗ " + type(ex).__name__
                    break
            if not и["пост"]:
                и["пост"] = "все эмодзи отвергнуты"

        await asyncio.sleep(random.uniform(2, 5))

        try:
            disc = await client(GetDiscussionMessageRequest(
                peer=entity, msg_id=цель.id))
            if not (disc and disc.messages):
                и["чат"] = "нет обсуждения"
                return и
            корень = disc.messages[0]
            чат = await client.get_entity(корень.peer_id)
            комменты = [c for c in await client.get_messages(
                чат, limit=6, reply_to=корень.id) if c.id != корень.id]
        except Exception as e:
            и["чат"] = "чат: " + type(e).__name__
            return и
        if not комменты:
            и["чат"] = "комментариев нет"
            return и

        разр2 = await _get_available_reactions(client, чат)
        if not разр2:
            и["чат"] = "в чате реакции отключены"
            return и
        for e in random.sample(разр2, min(3, len(разр2))):
            try:
                await client(SendReactionRequest(
                    peer=чат, msg_id=комменты[0].id,
                    reaction=[ReactionEmoji(emoticon=e)]))
                и["чат"] = "ПРОШЛА " + e
                return и
            except ReactionInvalidError:
                continue
            except Exception as ex:
                и["чат"] = "ОТКАЗ " + type(ex).__name__
                return и
        и["чат"] = "все эмодзи отвергнуты"
        return и
    except Exception as e:
        и["прим"] = "сбой " + type(e).__name__
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
    print("повторяем %d пар на %d адресах" % (len(пары), len(по_адресам)), flush=True)

    итоги = []

    async def очередь(список):
        for p in список:
            r = None
            for _ in range(25):
                r = await повторить(p["акк"], p["канал"])
                if "занят" not in (r.get("прим") or ""):
                    break
                await asyncio.sleep(random.uniform(25, 45))
            r["было_чат"] = p.get("было_чат")
            итоги.append(r)
            print("   acc#%-5s @%-22s пост: %-18s чат: %s"
                  % (r["акк"], r["канал"][:22],
                     (r.get("пост") or r.get("прим") or "")[:18],
                     r.get("чат") or "—"), flush=True)

    await asyncio.gather(*[очередь(v) for v in по_адресам.values()])

    print()
    print("=" * 72)
    прошло_пост = sum(1 for x in итоги if (x.get("пост") or "").startswith("ПРОШЛА"))
    прошло_чат = sum(1 for x in итоги if (x.get("чат") or "").startswith("ПРОШЛА"))
    бан = sum(1 for x in итоги
              if "Banned" in (x.get("чат") or "") or "Banned" in (x.get("пост") or ""))
    print("  повторно прошло на пост:  %d из %d" % (прошло_пост, len(итоги)))
    print("  повторно прошло в чате:   %d из %d" % (прошло_чат, len(итоги)))
    print("  ПОЛУЧИЛИ БАН за реакции:  %d" % бан)
    print()
    print("  вывод: %s" % ("реакции банов не приносят — можно разворачивать"
                           if бан == 0 else "за реакции банят, затея не годится"))
    json.dump(итоги, io.open(os.path.join(ПАПКА, "retry_result.json"),
                             "w", encoding="utf-8"), ensure_ascii=False, indent=1)


asyncio.run(main())
