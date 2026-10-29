# -*- coding: utf-8 -*-
"""Реакции риалтайма по дням: сколько, где и что мешало.

Журнал реакций — шаги сессии в comment_queue.personality->'_steps':
    reaction        — на сам пост, перед комментарием (камуфляж, трафика нет)
    react_comments  — на комменты под своим постом (1-2 штуки)
    reaction_tour   — обход 3-4 соседних каналов, react-каналы первыми
Главная строка — «обход в react»: реакции в каналах, где нашим
аккаунтам запретили писать. Без обхода эти каналы не дают ничего.

Запуск из папки api:
    python ..\\tools\\реакции.py            # последние 7 дней
    python ..\\tools\\реакции.py 14
"""
import asyncio
import os
import re
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

ДНЕЙ = int(sys.argv[1]) if len(sys.argv) > 1 else 7

# Новый формат обхода — с разбивкой по режимам; старый — без неё.
ТУР_НОВ = re.compile(r"обошёл (\d+) каналов, поставил (\d+) реакций "
                     r"\(react (\d+), comment (\d+)\)")
ТУР_СТАР = re.compile(r"обошёл (\d+) каналов, поставил (\d+)")
КОММ = re.compile(r"(\d+) ok, (\d+) fail")
ОШИБКА = re.compile(r"@[\w\d_]+:(\w+)")


async def main():
    from sqlalchemy import text as t
    from utils.db_pool import async_session as S
    from utils.redis_pool import get_redis

    async with S() as db:
        строки = (await db.execute(t("""
            SELECT q.created_at::date, st->>'action', st->>'detail'
            FROM comment_queue q,
                 LATERAL jsonb_array_elements((q.personality::jsonb)->'_steps') st
            WHERE q.created_at > CURRENT_DATE - INTERVAL '%d days'
              AND (q.personality::jsonb) ? '_steps'
              AND st->>'action' IN ('reaction', 'react_comments', 'reaction_tour')
            """ % ДНЕЙ))).all()
        режимы = dict((await db.execute(t("""
            SELECT mode, count(*) FILTER (WHERE is_active)
            FROM target_channels WHERE campaign_id = 143 GROUP BY 1"""))).all())

    дни = defaultdict(Counter)
    мешало = Counter()
    for d, act, det in строки:
        b, det = дни[d], det or ""
        if act == "reaction":
            b["пост"] += det.startswith("Поставил")
        elif act == "react_comments":
            m = КОММ.search(det)
            if m:
                b["комм"] += int(m.group(1))
                b["комм_отказ"] += int(m.group(2))
                for тип in re.findall(r"\[(.+?)\]", det):
                    for x in тип.split(","):
                        мешало["под своим постом: " + x] += 1
            else:
                мешало["под своим постом: " + det[:45]] += 1
        elif act == "reaction_tour":
            b["сессий"] += 1
            m = ТУР_НОВ.search(det)
            if m:
                b["тур_react"] += int(m.group(3))
                b["тур_comment"] += int(m.group(4))
                b["каналов"] += int(m.group(1))
                for x in ОШИБКА.findall(det):
                    мешало["обход: " + x] += 1
            elif ТУР_СТАР.search(det):
                m = ТУР_СТАР.search(det)
                b["тур_старый"] += int(m.group(2))
                b["каналов"] += int(m.group(1))
            else:
                мешало["обход: " + det[:45]] += 1

    print("=" * 78)
    print("  РЕАКЦИИ ЗА %d ДНЕЙ" % ДНЕЙ)
    print("=" * 78)
    print("  %-11s %6s %9s %11s %11s %8s %7s" % (
        "дата", "пост", "комменты", "обход react", "обход comm", "всего", "сессий"))
    итог = Counter()
    for d in sorted(дни):
        b = дни[d]
        # До 28.09 обход не различал режимы — пишем его в общую колонку.
        тр = b["тур_react"]
        тк = b["тур_comment"] + b["тур_старый"]
        всего = b["пост"] + b["комм"] + тр + тк
        итог.update(b)
        итог["всего"] += всего
        print("  %-11s %6d %9d %11s %11d %8d %7d" % (
            d, b["пост"], b["комм"], тр if not b["тур_старый"] else "—",
            тк, всего, b["сессий"]))
    print("-" * 78)
    print("  всего %d: в react-каналах %d, под своим постом %d, на пост %d" % (
        итог["всего"], итог["тур_react"], итог["комм"], итог["пост"]))
    if итог["комм"] + итог["комм_отказ"]:
        print("  отказов под своим постом: %d из %d попыток" % (
            итог["комм_отказ"], итог["комм"] + итог["комм_отказ"]))

    # Живой счётчик обхода по режимам — пишется в момент реакции.
    try:
        from datetime import date, timedelta
        r = get_redis()
        print()
        print("  СЧЁТЧИК ОБХОДА (Redis):")
        for i in range(min(ДНЕЙ, 8)):
            день = (date.today() - timedelta(days=i)).isoformat()
            h = r.hgetall("gramgpt:rt:reacts_mode:%s" % день)
            if h:
                h = {(k.decode() if isinstance(k, bytes) else k): int(v) for k, v in h.items()}
                print("     %s  react %-5d comment %d" % (
                    день, h.get("react", 0), h.get("comment", 0)))
    except Exception:
        pass

    if мешало:
        print()
        print("  ЧТО МЕШАЛО")
        for k, v in мешало.most_common(14):
            print("     %-60s %d" % (k, v))

    print()
    print("  каналов: comment %s, react %s (активных)" % (
        режимы.get("comment", 0), режимы.get("react", 0)))


asyncio.run(main())
