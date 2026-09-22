# -*- coding: utf-8 -*-
"""Посадить второго участника в каналы, где писать реально получается.

Зачем. У 80% каналов наш участник ровно один, а писать в один канал чаще
раза в полчаса нельзя. Значит при живом канале, который постит каждые
пять минут, мы забираем один пост из шести — остальные уходят мимо,
подменить некем. Второй участник снимает этот затык без всякого риска:
пока первый на перерыве, пишет второй.

Отбор каналов — по опыту, а не по размеру. Канал-миллионник, в котором мы
двадцать раз пробовали и ни разу не смогли, второго аккаунта не стоит:
там либо комментарии закрыты, либо нас не пускают. Берём только те, где
успехи есть, и сортируем по размеру.

Отбор аккаунтов — по загруженности: сажаем того, у кого каналов меньше
всех, чтобы не упереться в лимит подписок и не перегружать одного.
"""
import asyncio
import argparse
import os
import random
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

CAMP = 143
PER_TASK = 4            # подписок в одной задаче (влезает в 10 минут)
TASK_MIN = 8
WINDOW_MIN = 420        # на сколько минут вперёд размазываем
IP_GAP_MIN = 12         # минимум между заходами с одного адреса
ПОТОЛОК_КАНАЛОВ = 380   # выше этого аккаунт упрётся в лимит Telegram

НЕЛЬЗЯ = {48, 95}
ОГРАНИЧЕНЫ = {51, 71, 77, 102, 105, 112, 125, 141, 144, 168,
              177, 180, 183, 187, 191, 192, 193}
СЛОМАНЫ = {118, 119, 215, 216, 218}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=300, help="сколько каналов взять")
    ap.add_argument("--members", type=int, default=2, help="сколько участников нужно")
    ap.add_argument("--min-wins", type=int, default=1, help="минимум успешных комментов")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    from sqlalchemy import text as sa_text
    from utils.db_pool import async_session as Session

    плохие = list(НЕЛЬЗЯ | ОГРАНИЧЕНЫ | СЛОМАНЫ)

    async with Session() as db:
        # ── Каналы: успехи есть, сортируем по размеру ──────────────
        каналы = (await db.execute(sa_text("""
            SELECT q.channel, pc.subscribers, q.ok, q.n
            FROM (SELECT channel, count(*) n,
                         count(*) FILTER (WHERE status='done') ok
                  FROM comment_queue WHERE campaign_id = :c GROUP BY 1) q
            JOIN parsed_channels pc ON lower(pc.username) = lower(q.channel)
            WHERE q.ok >= :w
            ORDER BY pc.subscribers DESC
            LIMIT :lim"""),
            {"c": CAMP, "w": a.min_wins, "lim": a.top})).all()

        # ── Кто уже состоит ───────────────────────────────────────
        участники = defaultdict(set)
        for ch, acc in (await db.execute(sa_text("""
            SELECT lower(ca.channel_username), ca.account_id
            FROM campaign_channel_assignments ca
            JOIN accounts a ON a.id = ca.account_id AND a.status = 'active'
            WHERE ca.status = 'joined'"""))).all():
            участники[ch].add(acc)

        # ── Свободные аккаунты и их загрузка ──────────────────────
        грузы = dict((r[0], r[1]) for r in (await db.execute(sa_text("""
            SELECT a.id, count(ca.id)
            FROM accounts a
            JOIN proxies p ON p.id = a.proxy_id
            LEFT JOIN campaign_channel_assignments ca
                   ON ca.account_id = a.id AND ca.status = 'joined'
            WHERE a.status = 'active' AND p.is_valid
              AND NOT (a.id = ANY(:bad))
            GROUP BY a.id"""), {"bad": плохие})).all())
        адрес = dict((r[0], r[1]) for r in (await db.execute(sa_text("""
            SELECT a.id, COALESCE(p.host, '?') FROM accounts a
            LEFT JOIN proxies p ON p.id = a.proxy_id
            WHERE a.id = ANY(:i)"""), {"i": list(грузы)})).all())

    # ── Раздача: каждому каналу добираем до нужного числа ─────────
    назначено = defaultdict(list)         # acc -> [каналы]
    без_кандидата = 0
    добор = 0
    for ch, subs, ok, n in каналы:
        есть = участники[ch.lower()]
        не_хватает = a.members - len(есть)
        while не_хватает > 0:
            # Берём наименее загруженного, кто там ещё не состоит.
            кандидаты = [i for i in грузы
                         if i not in есть
                         and грузы[i] + len(назначено[i]) < ПОТОЛОК_КАНАЛОВ]
            if not кандидаты:
                без_кандидата += 1
                break
            кто = min(кандидаты, key=lambda i: грузы[i] + len(назначено[i]))
            назначено[кто].append(ch)
            есть.add(кто)
            добор += 1
            не_хватает -= 1

    # ── Задачи: режем на порции и разводим по времени ────────────
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
        шаг = max(IP_GAP_MIN, WINDOW_MIN // max(1, len(lst)))
        for i, (acc, _h, chs) in enumerate(lst):
            план.append((i * шаг + random.randint(0, шаг // 2), acc, host, chs))
    план.sort()

    print("каналов отобрано:      %d (успехов от %d, по убыванию размера)"
          % (len(каналы), a.min_wins))
    if каналы:
        print("  крупнейший           @%s  %d подп." % (каналы[0][0], каналы[0][1]))
        print("  наименьший в наборе  @%s  %d подп." % (каналы[-1][0], каналы[-1][1]))
    print("нужно участников:      %d на канал" % a.members)
    print("добираем подписок:     %d" % добор)
    print("аккаунтов задействовано: %d" % len(назначено))
    if без_кандидата:
        print("каналов без кандидата: %d (все свободные уже состоят там)" % без_кандидата)
    print("задач:                 %d, последняя через %d мин"
          % (len(план), план[-1][0] if план else 0))
    print()
    print("первые шесть запусков:")
    for d, acc, host, chs in план[:6]:
        print("   через %3d мин  acc#%-5s %-16s %s"
              % (d, acc, host[:16], ", ".join("@" + c for c in chs)[:48]))

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
