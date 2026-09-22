# -*- coding: utf-8 -*-
"""Режим канала: комментируем или только реагируем.

Зачем колонка, а не список в памяти. Потеря этой пометки означает, что
аккаунт пойдёт писать туда, где его забанят, — а это шесть аккаунтов в
день по нашей статистике. Колонка переживает чистку памяти и перезапуски,
видна в интерфейсе, и отбор может фильтровать прямо запросом, не таща
полторы тысячи имён в память на каждом обходе.

Значения:
    comment — обычный канал, пишем комментарии (по умолчанию)
    react   — писать ЗАПРЕЩЕНО, ходим только ставить реакции
    dead    — не трогаем вовсе (комментарии закрыты или платные)
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
        await db.execute(t("""
            ALTER TABLE target_channels
            ADD COLUMN IF NOT EXISTS mode VARCHAR(16) NOT NULL DEFAULT 'comment'"""))
        await db.execute(t("""
            CREATE INDEX IF NOT EXISTS ix_target_channels_mode
            ON target_channels (campaign_id, mode, is_active)"""))
        await db.commit()
        print("колонка mode добавлена")

        # Каналы, где писать запретили, но комментарии не закрыты —
        # именно в них реакции и проходят. 500 крупнейших.
        res = await db.execute(t("""
            UPDATE target_channels tc
            SET mode = 'react', is_active = true
            WHERE tc.campaign_id = 143
              AND tc.username IN (
                SELECT q.channel
                FROM comment_queue q
                JOIN parsed_channels pc ON lower(pc.username) = lower(q.channel)
                WHERE q.campaign_id = 143
                  AND EXISTS (SELECT 1 FROM comment_queue f
                        WHERE f.campaign_id = 143 AND f.channel = q.channel
                          AND f.status = 'failed'
                          AND (f.error ILIKE '%can%t write%' OR f.error ILIKE '%banned%'
                               OR f.error ILIKE '%private%'))
                  AND NOT EXISTS (SELECT 1 FROM comment_queue d
                        WHERE d.campaign_id = 143 AND d.channel = q.channel
                          AND (d.error ILIKE '%disabled%' OR d.error ILIKE '%PAYMENT%'))
                GROUP BY q.channel
                ORDER BY max(pc.subscribers) DESC NULLS LAST
                LIMIT 500)
            RETURNING username"""))
        помечено = len(res.all())

        # Мёртвые: комментарии закрыты или платные. Туда не ходим вовсе.
        res2 = await db.execute(t("""
            UPDATE target_channels tc SET mode = 'dead'
            WHERE tc.campaign_id = 143 AND tc.mode = 'comment'
              AND EXISTS (SELECT 1 FROM comment_queue d
                    WHERE d.campaign_id = 143 AND lower(d.channel) = lower(tc.username)
                      AND (d.error ILIKE '%disabled%' OR d.error ILIKE '%PAYMENT%'))
            RETURNING username"""))
        мёртвых = len(res2.all())
        await db.commit()

        print("помечено «только реакции»: %d" % помечено)
        print("помечено «мёртвый»:        %d" % мёртвых)
        print()
        for r in (await db.execute(t("""
            SELECT mode, count(*) FILTER (WHERE is_active) активных, count(*) всего
            FROM target_channels WHERE campaign_id = 143
            GROUP BY 1 ORDER BY 3 DESC"""))).all():
            print("   %-10s активных %-6d всего %d" % (r[0], r[1], r[2]))


asyncio.run(main())
