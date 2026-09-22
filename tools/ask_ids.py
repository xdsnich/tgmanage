# -*- coding: utf-8 -*-
"""Спросить @SpamBot про конкретные аккаунты — по списку номеров.

Нужен, когда движок вернул «забанен в супергруппах»: сама по себе эта
ошибка не говорит, глобальное это ограничение или запрет в одном чате.
Бот отвечает прямо, и только его ответ считается за факт.

Аккаунты на разных адресах идут одновременно, на одном — по очереди.
"""
import asyncio
import argparse
import os
import random
import sys
from collections import defaultdict
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from spambot_check import ask


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", required=True)
    a = ap.parse_args()

    from sqlalchemy import text as sa_text
    from utils.db_pool import async_session as Session

    ids = [int(x) for x in a.ids.split(",") if x.strip()]
    async with Session() as db:
        адрес = dict((r[0], "%s:%s" % (r[1], r[2])) for r in (
            await db.execute(sa_text("""
                SELECT a.id, COALESCE(p.host, '?'), COALESCE(p.port, 0)
                FROM accounts a LEFT JOIN proxies p ON p.id = a.proxy_id
                WHERE a.id = ANY(:i)"""), {"i": ids})).all())

    по_адресам = defaultdict(list)
    for i in ids:
        по_адресам[адрес.get(i, "?")].append(i)
    print("аккаунтов %d на %d адресах — идут одновременно"
          % (len(ids), len(по_адресам)), flush=True)

    итоги = []

    async def очередь(список):
        for i in список:
            ответ = None
            # Адрес закрепляется за аккаунтом на десять минут, а на одном
            # адресе у нас живёт два-три аккаунта. Восьми попыток по
            # полминуты (это шесть минут) не хватало, и соседи по адресу
            # уходили непроверенными — в первом заходе так потерялись 33
            # аккаунта из 94. Ждём с запасом на всю очередь адреса.
            for _ in range(40):
                try:
                    ответ = await ask(i)
                except Exception as e:
                    ответ = "сбой %s" % type(e).__name__
                if ответ and "занят" not in str(ответ):
                    break
                await asyncio.sleep(random.uniform(30, 60))
            итоги.append((i, ответ))
            print("[%s] acc#%-5s %s" % (datetime.now().strftime("%H:%M:%S"), i,
                                        str(ответ)[:150]), flush=True)

    await asyncio.gather(*[очередь(v) for v in по_адресам.values()])

    print()
    print("=" * 78)
    for i, о in sorted(итоги):
        о = str(о or "")
        свободен = "good news" in о.lower() or "no limits" in о.lower()
        print("acc#%-5s %-9s %s" % (i, "СВОБОДЕН" if свободен else "ОГРАНИЧЕН",
                                    о[:110]))


asyncio.run(main())
