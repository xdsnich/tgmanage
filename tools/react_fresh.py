# -*- coding: utf-8 -*-
"""Пройдёт ли реакция в чате СВЕЖИМ аккаунтом, который там не комментировал?

Что уже знаем. Там, где аккаунту запретили писать, реакция на пост
проходит, а реакция на комментарий — нет: чат отвечает «забанен» или
вовсе недоступен. Но это проверялось ТЕМ ЖЕ аккаунтом, которого забанили.

Вопрос, который это оставляет открытым: бан висит на паре «аккаунт-чат»
или чат закрыт для всех? Если на паре — значит чужой аккаунт, который там
никогда не писал, сможет и вступить, и отреагировать. Тогда мёртвые
каналы возвращаются в работу почти в полном объёме, просто другими
аккаунтами.

Что делает скрипт. Для каждого канала берёт аккаунт, которого там
никогда не было, вступает в канал, находит привязанный чат, вступает и
в него — и пробует поставить реакцию: сперва на пост, потом на чужой
комментарий.

Вступления настоящие, поэтому аккаунты берём рабочие и по одному на
адрес, чтобы не толкаться с комментингом.
"""
import asyncio
import argparse
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


async def опыт(acc_id, channel):
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as Session
    from utils.telegram import make_telethon_client
    from utils.ip_throttle import acquire_ip_lock
    from utils.account_lock import acquire_account_lock, release_account_lock

    и = {"акк": acc_id, "канал": channel, "вступил": None,
         "пост": None, "чат_вступил": None, "чат_реакция": None, "прим": ""}

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
    if not acquire_account_lock(acc_id, ttl=420):
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
        from telethon.tl.functions.channels import JoinChannelRequest
        from telethon.tl.functions.messages import (
            SendReactionRequest, GetDiscussionMessageRequest)
        from telethon.tl.types import ReactionEmoji
        from telethon.errors import ReactionInvalidError, UserAlreadyParticipantError

        try:
            entity = await client.get_entity(channel)
        except Exception as e:
            и["прим"] = "канал не открылся: " + type(e).__name__
            return и

        # ── Вступаем в канал ────────────────────────────────────
        try:
            await client(JoinChannelRequest(entity))
            и["вступил"] = "да"
        except UserAlreadyParticipantError:
            и["вступил"] = "уже был"
        except Exception as e:
            и["вступил"] = "нет: " + type(e).__name__
        await asyncio.sleep(random.uniform(3, 8))

        msgs = await client.get_messages(entity, limit=3)
        if not msgs:
            и["прим"] = "нет постов"
            return и
        цель = msgs[0]

        # ── Реакция на пост ─────────────────────────────────────
        разр = await _get_available_reactions(client, entity)
        if not разр:
            и["пост"] = "реакции отключены"
        else:
            for e in random.sample(разр, min(3, len(разр))):
                try:
                    await client(SendReactionRequest(
                        peer=entity, msg_id=цель.id,
                        reaction=[ReactionEmoji(emoticon=e)]))
                    и["пост"] = "ПОСТАВЛЕНА " + e
                    break
                except ReactionInvalidError:
                    continue
                except Exception as ex:
                    и["пост"] = "отказ " + type(ex).__name__
                    break
            if not и["пост"]:
                и["пост"] = "все эмодзи отвергнуты"

        await asyncio.sleep(random.uniform(3, 7))

        # ── Чат: вступаем и пробуем реакцию на чужой коммент ────
        try:
            disc = await client(GetDiscussionMessageRequest(
                peer=entity, msg_id=цель.id))
            if not (disc and disc.messages):
                и["чат_реакция"] = "у поста нет обсуждения"
                return и
            корень = disc.messages[0]
            чат = await client.get_entity(корень.peer_id)
        except Exception as e:
            и["чат_реакция"] = "чат не открылся: " + type(e).__name__
            return и

        try:
            await client(JoinChannelRequest(чат))
            и["чат_вступил"] = "да"
        except UserAlreadyParticipantError:
            и["чат_вступил"] = "уже был"
        except Exception as e:
            и["чат_вступил"] = "нет: " + type(e).__name__
        await asyncio.sleep(random.uniform(3, 8))

        try:
            комменты = await client.get_messages(чат, limit=6, reply_to=корень.id)
            комменты = [c for c in комменты if c.id != корень.id]
        except Exception as e:
            и["чат_реакция"] = "комменты не прочитались: " + type(e).__name__
            return и
        if not комменты:
            и["чат_реакция"] = "комментариев нет"
            return и

        разр2 = await _get_available_reactions(client, чат)
        if not разр2:
            и["чат_реакция"] = "в чате реакции отключены"
            return и
        for e in random.sample(разр2, min(3, len(разр2))):
            try:
                await client(SendReactionRequest(
                    peer=чат, msg_id=комменты[0].id,
                    reaction=[ReactionEmoji(emoticon=e)]))
                и["чат_реакция"] = "ПОСТАВЛЕНА " + e
                return и
            except ReactionInvalidError:
                continue
            except Exception as ex:
                и["чат_реакция"] = "отказ " + type(ex).__name__
                return и
        и["чат_реакция"] = "все эмодзи отвергнуты"
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10)
    a = ap.parse_args()

    from sqlalchemy import text as sa_text
    from utils.db_pool import async_session as Session
    from utils.redis_pool import get_redis

    r = get_redis()
    ограничены = set()
    for k in r.scan_iter(match="gramgpt:rt:limited:*"):
        k = k.decode() if isinstance(k, bytes) else k
        ограничены.add(int(k.rsplit(":", 1)[1]))

    async with Session() as db:
        # Каналы, где писать запретили. Берём покрупнее — если выйдет,
        # с них и отдача больше.
        каналы = [x[0] for x in (await db.execute(sa_text("""
            SELECT DISTINCT q.channel
            FROM comment_queue q
            JOIN parsed_channels pc ON lower(pc.username)=lower(q.channel)
            WHERE q.campaign_id=143 AND q.status='failed'
              AND (q.error ILIKE '%can%t write%' OR q.error ILIKE '%banned%'
                   OR q.error ILIKE '%private%')
              AND pc.subscribers >= 100000
            ORDER BY q.channel LIMIT 200"""))).all()]
        random.shuffle(каналы)

        свободные = [x[0] for x in (await db.execute(sa_text("""
            SELECT a.id FROM accounts a JOIN proxies p ON p.id=a.proxy_id
            WHERE a.status='active' AND p.is_valid
              AND NOT (a.id = ANY(:огр)) AND a.id NOT IN (48,95,118,119)
            ORDER BY a.id"""), {"огр": list(ограничены)})).all()]
        адрес = dict((x[0], x[1]) for x in (await db.execute(sa_text("""
            SELECT a.id, COALESCE(p.host,'?') FROM accounts a
            LEFT JOIN proxies p ON p.id=a.proxy_id WHERE a.id = ANY(:i)"""),
            {"i": свободные})).all())

        # Для каждого канала — аккаунт, которого там НИКОГДА не было.
        пары = []
        занятые_адреса = set()
        for ch in каналы:
            если_был = set(x[0] for x in (await db.execute(sa_text("""
                SELECT DISTINCT account_id FROM campaign_channel_assignments
                WHERE lower(channel_username)=lower(:ch)
                UNION SELECT DISTINCT account_id FROM comment_queue
                WHERE lower(channel)=lower(:ch)"""), {"ch": ch})).all())
            for acc in свободные:
                h = адрес.get(acc, "?")
                if acc in если_был or h in занятые_адреса:
                    continue
                пары.append((acc, ch))
                занятые_адреса.add(h)
                break
            if len(пары) >= a.count:
                break

    print("проверок: %d (аккаунт, который в канале НИКОГДА не был)" % len(пары))
    for acc, ch in пары:
        print("   acc#%-5s → @%s" % (acc, ch))
    print(flush=True)

    итоги = await asyncio.gather(*[опыт(acc, ch) for acc, ch in пары])

    print()
    print("=" * 78)
    print("%-7s %-22s %-11s %-16s %s" % ("акк", "канал", "в канал", "реакция на пост", "реакция в чате"))
    print("=" * 78)
    for и in итоги:
        print("%-7s @%-21s %-11s %-16s %s"
              % ("#%s" % и["акк"], и["канал"][:21], (и["вступил"] or "—")[:11],
                 (и["пост"] or и["прим"] or "—")[:16],
                 (и["чат_реакция"] or "—")[:30]))
    print()
    поста = sum(1 for и in итоги if (и.get("пост") or "").startswith("ПОСТАВЛЕНА"))
    чата = sum(1 for и in итоги if (и.get("чат_реакция") or "").startswith("ПОСТАВЛЕНА"))
    print("ИТОГ: на пост прошло %d из %d, в чате прошло %d из %d"
          % (поста, len(итоги), чата, len(итоги)))
    json.dump(итоги, io.open(os.path.join(ПАПКА, "react_fresh_result.json"),
                             "w", encoding="utf-8"), ensure_ascii=False, indent=1)


asyncio.run(main())
