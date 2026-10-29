# -*- coding: utf-8 -*-
"""Сколько каналов от 10к принёс каталог TGStat — по базе, не по логам.

Счётчики в логах у каждого хоста свои и обнуляются при перезапуске, а
база — одна: здесь каждый канал записан ровно один раз.

Запуск из папки api:
    python ..\\tools\\tgstat_catalog_count.py
"""
import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))


async def main():
    from sqlalchemy import text as t
    from utils.db_pool import async_session as S
    async with S() as db:
        rows = (await db.execute(t("""
            SELECT split_part(search_query, ':', 1) AS хост, count(*),
                   count(*) FILTER (WHERE subscribers >= 100000),
                   count(*) FILTER (WHERE last_post_date > now() - interval '7 days')
            FROM parsed_channels
            WHERE search_query ~ '^([a-z]+\\.)?tgstat\\.(ru|com):'
            GROUP BY 1 ORDER BY 2 DESC"""))).all()
        всего = sum(r[1] for r in rows)
        print("НОВЫЕ КАНАЛЫ ОТ 10к ИЗ КАТАЛОГА TGSTAT: %d" % всего)
        print()
        print("   %-16s %7s %9s %14s" % ("хост", "новых", "от 100к", "живые (7 дн)"))
        for r in rows:
            print("   %-16s %7d %9d %14d" % tuple(r))
        дублей = (await db.execute(t("""
            SELECT count(*) FROM (
              SELECT lower(username) FROM parsed_channels
              WHERE search_query ~ '^([a-z]+\\.)?tgstat\\.(ru|com):'
              GROUP BY 1 HAVING count(*) > 1) x"""))).scalar()
        print()
        print("   повторов среди них: %d" % дублей)


asyncio.run(main())
