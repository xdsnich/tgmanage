# -*- coding: utf-8 -*-
"""Короткая сводка: что кампания сделала за последние дни."""
import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))


async def main():
    from sqlalchemy import text as t
    from utils.db_pool import async_session as S
    async with S() as db:
        print("КОМЕНТЫ ПО ДНЯМ")
        for r in (await db.execute(t("""
            SELECT created_at::date д,
                   count(*) FILTER (WHERE status='done') ok, count(*) n
            FROM comment_queue WHERE campaign_id=143
              AND created_at > CURRENT_DATE - 4
            GROUP BY 1 ORDER BY 1"""))).all():
            print("   %s  написано %-5d из %-5d попыток" % (r[0], r[1], r[2]))

        print()
        c = (await db.execute(t("SELECT status, comments_sent FROM campaigns WHERE id=143"))).first()
        ids = [int(x) for x in ((await db.execute(t(
            "SELECT account_ids FROM campaigns WHERE id=143"))).scalar() or [])]
        print("кампания: %s, отправлено всего %s, аккаунтов %d" % (c[0], c[1], len(ids)))

        print()
        print("КАНАЛЫ ПО РЕЖИМАМ")
        for r in (await db.execute(t("""
            SELECT mode, count(*) FILTER (WHERE is_active) акт, count(*) всего
            FROM target_channels WHERE campaign_id=143 GROUP BY 1 ORDER BY 3 DESC"""))).all():
            print("   %-9s активных %-6d всего %d" % (r[0], r[1], r[2]))

        print()
        пар = (await db.execute(t("""SELECT count(*) FROM campaign_channel_assignments ca
            JOIN target_channels tc ON lower(tc.username)=lower(ca.channel_username)
              AND tc.campaign_id=143 AND tc.is_active AND tc.mode='comment'
            WHERE ca.status='joined' AND ca.account_id = ANY(:i)"""), {"i": ids})).scalar()
        print("рабочих пар для комментинга: %d" % пар)


asyncio.run(main())
