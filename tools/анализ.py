# -*- coding: utf-8 -*-
"""Полный разбор: комменты, ошибки, аккаунты, каналы, воронка."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))
KYIV = timezone(timedelta(hours=3))


async def main():
    from sqlalchemy import text as t
    from utils.db_pool import async_session as S
    from utils.redis_pool import get_redis

    async with S() as db:
        print("=" * 66)
        print("  КОМЕНТЫ ПО ДНЯМ")
        print("=" * 66)
        for r in (await db.execute(t("""
            SELECT created_at::date д,
                   count(*) FILTER (WHERE status='done') ok,
                   count(*) FILTER (WHERE status='failed') сбой,
                   count(*) FILTER (WHERE status='aborted') брош,
                   count(*) n
            FROM comment_queue WHERE campaign_id=143
              AND created_at > CURRENT_DATE - 7
            GROUP BY 1 ORDER BY 1"""))).all():
            доля = 100.0 * r[1] / max(1, r[4])
            print("  %s  написано %-5d  сбоев %-5dброшено %-5d из %-5d  (%.0f%%)"
                  % (r[0], r[1], r[2], r[3], r[4], доля))

        print()
        print("  ОШИБКИ ЗА ТРОЕ СУТОК")
        for r in (await db.execute(t("""
            SELECT COALESCE(left(error,46),'—') e, count(*) c
            FROM comment_queue WHERE campaign_id=143
              AND created_at > CURRENT_DATE - 3
              AND status IN ('failed','aborted')
            GROUP BY 1 ORDER BY 2 DESC LIMIT 8"""))).all():
            print("    %-48s %d" % (r[0], r[1]))

        print()
        print("  АККАУНТЫ")
        for r in (await db.execute(t("""
            SELECT status, count(*) FROM accounts GROUP BY 1 ORDER BY 2 DESC"""))).all():
            print("    %-12s %d" % (r[0], r[1]))
        ids = [int(x) for x in ((await db.execute(t(
            "SELECT account_ids FROM campaigns WHERE id=143"))).scalar() or [])]
        print("    в кампании   %d" % len(ids))

        print()
        print("  УЛЕТЕЛО ПО ДНЯМ")
        for r in (await db.execute(t("""
            SELECT created_at::date д, count(DISTINCT account_id) n
            FROM comment_queue WHERE campaign_id=143
              AND error LIKE '%banned from sending%'
              AND created_at > CURRENT_DATE - 7
            GROUP BY 1 ORDER BY 1"""))).all():
            print("    %s  %d" % (r[0], r[1]))

        print()
        print("  КАНАЛЫ")
        for r in (await db.execute(t("""
            SELECT mode, count(*) FILTER (WHERE is_active) акт, count(*) всего
            FROM target_channels WHERE campaign_id=143
            GROUP BY 1 ORDER BY 3 DESC"""))).all():
            print("    %-9s активных %-6d всего %d" % (r[0], r[1], r[2]))
        пар = (await db.execute(t("""SELECT count(*) FROM campaign_channel_assignments ca
            JOIN target_channels tc ON lower(tc.username)=lower(ca.channel_username)
              AND tc.campaign_id=143 AND tc.is_active AND tc.mode='comment'
            WHERE ca.status='joined' AND ca.account_id = ANY(:i)"""), {"i": ids})).scalar()
        print("    рабочих пар для комментинга: %d" % пар)
        бд = (await db.execute(t("SELECT count(*) FROM parsed_channels"))).scalar()
        print("    каналов в базе: %d" % бд)

    r2 = get_redis()
    сейчас = datetime.now(timezone.utc)
    огр = ист = 0
    for k in r2.scan_iter(match="gramgpt:rt:limited:*"):
        k = k.decode() if isinstance(k, bytes) else k
        огр += 1
        u = r2.hget(k, "until")
        u = (u.decode() if isinstance(u, bytes) else u) if u else "?"
        if u != "?":
            try:
                if datetime.fromisoformat(u).replace(tzinfo=timezone.utc) <= сейчас:
                    ист += 1
            except Exception:
                pass
    print()
    print("  ОГРАНИЧЕНИЯ: в реестре %d, из них срок истёк %d" % (огр, ист))


asyncio.run(main())
