# -*- coding: utf-8 -*-
"""Сколько заявок в воронку пришло сегодня — по данным самого Telegram.

Заходим админом @jeka_opasniy, находим среди его диалогов канал воронки и
читаем список заявок на вступление. У каждой заявки есть дата — по ней и
отбираем сегодняшние.

Только чтение: ничего не принимаем и не отклоняем.
"""
import asyncio
import os
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

ADMIN_ACC = 48                  # @jeka_opasniy
FUNNEL_ID = 4437551288          # «Amelie💓💔»
KYIV = timezone(timedelta(hours=3))
PAID = ("157.22.53.174", "138.249.151.12")


async def main():
    from sqlalchemy import text as sa_text
    from utils.db_pool import async_session as Session
    from utils.telegram import get_account_with_proxy, make_telethon_client
    from utils.ip_throttle import acquire_ip_lock, is_ip_busy
    from telethon.tl.functions.messages import GetChatInviteImportersRequest
    from telethon.tl.types import InputUserEmpty

    async with Session() as db:
        acc, proxy = await get_account_with_proxy(db, ADMIN_ACC)
    if not acc or not proxy:
        print("аккаунт или прокси недоступны")
        return
    if proxy.host not in PAID:
        print("прокси %s не оплачен — не подключаюсь" % proxy.host)
        return
    # Аккаунт делит и прокси, и файл сессии с проверкой каналов. Лезть
    # туда вторым подключением нельзя: и правило «один коннект на прокси»,
    # и SQLite сессии этого не переживут.
    #
    # Порядок важен. Сначала убеждаемся, что файл сессии свободен, и только
    # потом берём замок прокси. Наоборот нельзя: замок держится десять минут
    # и не снимается — заняв его впустую, мы на это время остановим саму
    # проверку каналов, ради которой аккаунт и занят.
    import sqlite3

    def session_free():
        """Свободен ли файл сессии. BEGIN IMMEDIATE — самая честная проверка:
        именно на ней спотыкается второй писатель."""
        try:
            con = sqlite3.connect(acc.session_file, timeout=0.5)
            con.execute("BEGIN IMMEDIATE")
            con.rollback()
            con.close()
            return True
        except sqlite3.OperationalError:
            return False

    client = None
    for attempt in range(1, 31):
        if not session_free():
            print("[%2d] сессия занята проверкой каналов, жду 30с" % attempt)
            await asyncio.sleep(30)
            continue
        if is_ip_busy(proxy.host, proxy.port):
            print("[%2d] прокси %s остывает, жду 30с" % (attempt, proxy.host))
            await asyncio.sleep(30)
            continue
        if not acquire_ip_lock(proxy, owner=acc.id):
            await asyncio.sleep(30)
            continue
        print("[%2d] окно свободно, подключаюсь" % attempt)
        client = make_telethon_client(acc, proxy)
        try:
            await client.connect()
            break
        except Exception as e:
            if "locked" not in str(e).lower():
                raise
            print("     не успел, сессию перехватили — жду")
            try:
                await client.disconnect()
            except Exception:
                pass
            client = None
            await asyncio.sleep(30)
    if client is None:
        print("не дождался свободного окна — попробуй позже")
        return

    try:
        if not await client.is_user_authorized():
            print("сессия мертва")
            return
        me = await client.get_me()
        print("подключён: %s @%s" % (me.first_name, me.username or "-"))

        # Канал ищем среди диалогов: так получаем готовый peer с access_hash.
        dest = None
        async for d in client.iter_dialogs(limit=200):
            if getattr(d.entity, "id", None) == FUNNEL_ID:
                dest = d.entity
                break
        if dest is None:
            print("канал воронки не найден в диалогах")
            return
        print("канал:    %s\n" % getattr(dest, "title", "?"))

        # Все ждущие решения заявки, постранично.
        pend, offset_date, offset_user = [], None, InputUserEmpty()
        while True:
            res = await client(GetChatInviteImportersRequest(
                peer=dest, requested=True, q="",
                offset_date=offset_date, offset_user=offset_user, limit=100))
            if not res.importers:
                break
            pend.extend(res.importers)
            if len(res.importers) < 100:
                break
            last = res.importers[-1]
            offset_date = last.date
            try:
                offset_user = await client.get_input_entity(last.user_id)
            except Exception:
                break
            await asyncio.sleep(1.5)

        now = datetime.now(KYIV)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)

        by_day, by_hour, links = Counter(), Counter(), Counter()
        today_n = 0
        for imp in pend:
            d = imp.date.astimezone(KYIV)
            by_day[d.date()] += 1
            if d >= today:
                today_n += 1
                by_hour[d.hour] += 1
                lk = getattr(imp, "via_link", None)
                links[getattr(lk, "link", None) or "без ссылки"] += 1

        print("=" * 50)
        print("  ЖДУТ РЕШЕНИЯ ВСЕГО:     %d" % len(pend))
        print("  ИЗ НИХ СЕГОДНЯ с 00:00: %d" % today_n)
        print("=" * 50)

        if by_hour:
            print("\n-- сегодня по часам --")
            for h in sorted(by_hour):
                print("  %02d:00  %s %d" % (h, "#" * by_hour[h], by_hour[h]))
        if links:
            print("\n-- по пригласительным ссылкам --")
            for u, n in links.most_common(15):
                print("  %-42s %d" % (u[:42], n))

        print("\n-- по дням (все ждущие) --")
        for d in sorted(by_day, reverse=True)[:8]:
            print("  %s  %-4d%s" % (d, by_day[d],
                                    "  <- сегодня" if d == now.date() else ""))
    except Exception as e:
        print("сбой %s: %s" % (type(e).__name__, str(e)[:200]))
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


asyncio.run(main())
