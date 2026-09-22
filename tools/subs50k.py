# -*- coding: utf-8 -*-
"""Подписать рабочие аккаунты на каналы от 50к, где можно комментировать.

Что считаем рабочим аккаунтом. Активный, с живым прокси, и — по решению
17.09 — включая тех, у кого ограничение ВРЕМЕННОЕ: они вернутся в строй
сами, и к тому моменту пусть будут уже подписаны. Аккаунты с бессрочным
ограничением не берём: их Telegram отпустит неизвестно когда и отпустит
ли вообще.

Каналы берём по убыванию размера и только те, где у рабочих аккаунтов
ещё нет ни одного участника, — сперва закрыть пустые, а вторых участников
добирать отдельно.

Нагрузку размазываем на весь день: заходы с одного адреса разведены шире
остывания, чтобы подписки не отняли адреса у комментинга.
"""
import asyncio
import argparse
import os
import random
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

PER_TASK = 4            # подписок в одной задаче (влезает в 10 минут)
TASK_MIN = 8
IP_GAP_MIN = 14         # минимум между заходами с одного адреса
ПОТОЛОК_КАНАЛОВ = 380   # выше этого аккаунт упрётся в лимит Telegram
НЕЛЬЗЯ = {48, 95}
СЛОМАНЫ = {118, 119}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-account", type=int, default=12)
    ap.add_argument("--min-subs", type=int, default=50000)
    ap.add_argument("--window", type=int, default=600, help="на сколько минут размазать")
    # Подписки, поставленные в очередь, но ещё не выполненные, в базе никак
    # не отмечены — повторный запуск выбрал бы те же каналы и подписался бы
    # дважды. Пропускаем столько верхних, сколько уже разобрала прошлая
    # партия: порядок по размеру устойчив, значит и срез повторяем.
    ap.add_argument("--skip", type=int, default=0,
                    help="пропустить столько верхних каналов")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    from sqlalchemy import text as sa_text
    from utils.db_pool import async_session as Session
    from utils.redis_pool import get_redis

    # Бессрочно ограниченные — мимо; со сроком — берём.
    r = get_redis()
    бессроч = set()
    for k in r.scan_iter(match="gramgpt:rt:limited:*"):
        k = k.decode() if isinstance(k, bytes) else k
        u = r.hget(k, "until")
        u = (u.decode() if isinstance(u, bytes) else u) if u else "?"
        if u == "?":
            бессроч.add(int(k.rsplit(":", 1)[1]))

    вне = list(бессроч | НЕЛЬЗЯ | СЛОМАНЫ)

    async with Session() as db:
        аккаунты = [x[0] for x in (await db.execute(sa_text("""
            SELECT a.id FROM accounts a JOIN proxies p ON p.id = a.proxy_id
            WHERE a.status='active' AND p.is_valid AND NOT (a.id = ANY(:b))
            ORDER BY a.id"""), {"b": вне})).all()]

        # Каналы без единого нашего участника, по убыванию размера.
        каналы = [(x[0], x[1]) for x in (await db.execute(sa_text("""
            SELECT pc.username, pc.subscribers FROM parsed_channels pc
            WHERE pc.subscribers >= :m AND pc.has_comments
              AND NOT EXISTS (
                SELECT 1 FROM campaign_channel_assignments ca
                WHERE ca.status = 'joined' AND ca.account_id = ANY(:i)
                  AND lower(ca.channel_username) = lower(pc.username))
            ORDER BY pc.subscribers DESC
            LIMIT :lim OFFSET :off"""),
            {"m": a.min_subs, "i": аккаунты,
             "lim": len(аккаунты) * a.per_account, "off": a.skip})).all()]

        грузы = dict((x[0], x[1]) for x in (await db.execute(sa_text("""
            SELECT a.id, count(ca.id) FROM accounts a
            LEFT JOIN campaign_channel_assignments ca
                   ON ca.account_id = a.id AND ca.status = 'joined'
            WHERE a.id = ANY(:i) GROUP BY a.id"""), {"i": аккаунты})).all())
        адрес = dict((x[0], x[1]) for x in (await db.execute(sa_text("""
            SELECT a.id, COALESCE(p.host,'?') FROM accounts a
            LEFT JOIN proxies p ON p.id = a.proxy_id
            WHERE a.id = ANY(:i)"""), {"i": аккаунты})).all())

    # Раздача: канал уходит наименее загруженному аккаунту.
    назначено = defaultdict(list)
    for ch, subs in каналы:
        кандидаты = [i for i in аккаунты
                     if len(назначено[i]) < a.per_account
                     and грузы.get(i, 0) + len(назначено[i]) < ПОТОЛОК_КАНАЛОВ]
        if not кандидаты:
            break
        кто = min(кандидаты, key=lambda i: грузы.get(i, 0) + len(назначено[i]))
        назначено[кто].append(ch)

    задачи = []
    for acc, chs in назначено.items():
        random.shuffle(chs)
        for i in range(0, len(chs), PER_TASK):
            задачи.append((acc, адрес.get(acc, "?"), chs[i:i + PER_TASK]))

    по_адресам = defaultdict(list)
    for з in задачи:
        по_адресам[з[1]].append(з)
    план = []
    for host, lst in по_адресам.items():
        random.shuffle(lst)
        шаг = max(IP_GAP_MIN, a.window // max(1, len(lst)))
        for i, (acc, _h, chs) in enumerate(lst):
            план.append((i * шаг + random.randint(0, шаг // 2), acc, host, chs))
    план.sort()

    подписок = sum(len(x[3]) for x in план)
    print("аккаунтов:          %d (бессрочно ограниченные исключены: %d)"
          % (len(аккаунты), len(бессроч)))
    print("каналов отобрано:   %d (от %d подписчиков, без наших участников)"
          % (len(каналы), a.min_subs))
    if каналы:
        print("   крупнейший       @%s  %d" % (каналы[0][0], каналы[0][1]))
        print("   наименьший       @%s  %d" % (каналы[-1][0], каналы[-1][1]))
    print("подписок:           %d, задач %d" % (подписок, len(план)))
    print("последняя стартует через %d мин" % (план[-1][0] if план else 0))
    print()
    for d, acc, host, chs in план[:5]:
        print("   через %3d мин  acc#%-5s %-16s %s"
              % (d, acc, host[:16], ", ".join("@" + c for c in chs)[:46]))

    if not a.apply:
        print()
        print("показ без изменений. Запусти с --apply.")
        return

    from celery_app import celery_app
    for d, acc, host, chs in план:
        celery_app.send_task(
            "tasks.subscribe_tasks.run_subscribe",
            args=[{"account_ids": [acc], "channels": chs,
                   "total_minutes": TASK_MIN}],
            queue="subscribe", countdown=d * 60 + random.randint(0, 60))
    print()
    print("поставлено задач: %d" % len(план))


asyncio.run(main())
