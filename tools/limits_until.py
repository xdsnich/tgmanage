# -*- coding: utf-8 -*-
"""До какого числа ограничен каждый аккаунт — по словам самого @SpamBot.

Бот в ответе называет точную дату снятия («limited until 19 Sep 2026,
11:05 UTC»), и иногда не называет вовсе — тогда срок открытый. Здесь мы
эту дату вытаскиваем, переводим в киевское время и заодно кладём в тот
же реестр, по которому задача rt_restore возвращает аккаунты в работу.

Ждём свой адрес долго: на одном адресе живут два-три аккаунта, а адрес
закрепляется за каждым на десять минут.
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from spambot_check import ask

KYIV = timezone(timedelta(hours=3))
RE_UNTIL = re.compile(
    r"limited until ([0-9]{1,2} \w{3} [0-9]{4}, [0-9]{1,2}:[0-9]{2})", re.I)


def срок(текст):
    """(datetime в UTC, как было в тексте) либо (None, '')."""
    m = RE_UNTIL.search(текст or "")
    if not m:
        return None, ""
    try:
        d = datetime.strptime(m.group(1), "%d %b %Y, %H:%M")
        return d.replace(tzinfo=timezone.utc), m.group(1)
    except Exception:
        return None, m.group(1)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", required=True)
    a = ap.parse_args()

    from sqlalchemy import text as sa_text
    from utils.db_pool import async_session as Session
    from tasks.rt_restore import mark_limited

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
    print("аккаунтов %d на %d адресах" % (len(ids), len(по_адресам)), flush=True)

    итоги = []

    async def очередь(список):
        for i in список:
            r = None
            for _ in range(40):
                try:
                    r = await ask(i)
                except Exception as e:
                    r = {"verdict": "сбой %s" % type(e).__name__}
                if r and "занят" not in str(r.get("verdict", "")):
                    break
                await asyncio.sleep(random.uniform(30, 60))
            текст = (r or {}).get("текст") or ""
            когда, сырое = срок(текст)
            итоги.append((i, (r or {}).get("verdict"), когда, сырое, текст))
            if когда is not None:
                mark_limited(i, когда.replace(tzinfo=None).isoformat())
            print("  acc#%-5s %s" % (i, сырое or (r or {}).get("verdict")),
                  flush=True)

    await asyncio.gather(*[очередь(v) for v in по_адресам.values()])

    сейчас = datetime.now(timezone.utc)
    print()
    print("=" * 68)
    print("%-8s %-22s %s" % ("акк", "ограничен до (Киев)", "осталось"))
    print("=" * 68)
    for i, verdict, когда, сырое, текст in sorted(итоги,
                                                  key=lambda x: (x[2] is None, x[2] or сейчас)):
        if verdict == "ЧИСТ":
            print("%-8s %-22s %s" % ("#%d" % i, "— свободен —", "можно возвращать"))
            continue
        if когда is None:
            хвост = "СРОК НЕ НАЗВАН" if verdict == "ОГРАНИЧЕН" else str(verdict)
            print("%-8s %-22s %s" % ("#%d" % i, хвост, ""))
            continue
        ост = когда - сейчас
        часов = ост.total_seconds() / 3600
        если = ("истёк" if часов <= 0 else
                "%.0f ч" % часов if часов < 48 else "%.1f сут" % (часов / 24))
        print("%-8s %-22s %s" % ("#%d" % i,
                                 когда.astimezone(KYIV).strftime("%d.%m %H:%M"), если))


asyncio.run(main())
