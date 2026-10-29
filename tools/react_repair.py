# -*- coding: utf-8 -*-
"""Разовая починка под режим «только реакции».

До 28.09 бан в чате гасил канал целиком, хотя реакции там проходили бы:
чат живой, и аккаунт без бана спокойно их ставит. Заодно в таблице
подписок копились ложные 'joined': вступить в КАНАЛ можно и с баном в
ЧАТЕ, и ночная подписка ставила 'joined' заново.

Три шага:
  1. Пары с баном в чате → 'failed'. Иначе наблюдатель зовёт такой
     аккаунт писать туда, где его забанили, а план папок считает его
     «чистым».
  2. Погашенные за бан каналы → снова активны, режим 'react'. Берём
     только те, где ПОСЛЕДНЯЯ попытка кончилась баном — значит канал
     погасили именно за него, а не за что-то другое.
  3. Активные comment-каналы, где после шага 1 писать некем → 'react'.
     Ровно так теперь поступает исполнитель на каждом новом бане.

По умолчанию только считает. Применить: --go. Всё изменённое пишется в
файл отката рядом со скриптом.

Запуск из папки api:
    python ..\\tools\\react_repair.py
    python ..\\tools\\react_repair.py --go
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

CAMPAIGN = 143

# Бан в чате — по истории пары: админ выгнал («Channel private») или
# запретили писать («can't write»), и после него ни одного удачного
# комментария.
БАН_ПАРЫ = """EXISTS (
    SELECT 1 FROM comment_queue q
    WHERE q.account_id = ca.account_id
      AND lower(q.channel) = lower(ca.channel_username)
      AND q.status = 'failed'
      AND (q.error ILIKE 'Channel private%' OR q.error ILIKE '%can''t write%')
      AND NOT EXISTS (
          SELECT 1 FROM comment_queue d
          WHERE d.account_id = q.account_id
            AND lower(d.channel) = lower(q.channel)
            AND d.status = 'done' AND d.executed_at > q.executed_at))"""


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true", help="применить")
    a = ap.parse_args()

    from sqlalchemy import text as t
    from utils.db_pool import async_session as S

    откат = {"when": datetime.now().isoformat(timespec="seconds")}
    async with S() as db:
        # ── 1. Ложные 'joined' ─────────────────────────────────────
        пары = (await db.execute(t("""
            SELECT ca.id, ca.account_id, ca.channel_username
            FROM campaign_channel_assignments ca
            WHERE ca.status = 'joined' AND """ + БАН_ПАРЫ))).all()
        print("1. пар 'joined' с баном в чате:           %d" % len(пары))
        откат["pairs_to_failed"] = [r[0] for r in пары]

        # ── 2. Погашенные за бан ──────────────────────────────────
        погашенные = [r[0] for r in (await db.execute(t("""
            SELECT tc.id FROM target_channels tc
            WHERE tc.campaign_id = :c AND NOT tc.is_active AND tc.mode = 'comment'
              AND (SELECT (q.status = 'failed'
                           AND (q.error ILIKE 'Channel private%'
                                OR q.error ILIKE '%can''t write%'))
                   FROM comment_queue q
                   WHERE q.campaign_id = :c AND lower(q.channel) = lower(tc.username)
                   ORDER BY q.id DESC LIMIT 1)
              AND NOT EXISTS (SELECT 1 FROM comment_queue d
                   WHERE d.campaign_id = :c AND lower(d.channel) = lower(tc.username)
                     AND (d.error ILIKE '%disabled%' OR d.error ILIKE '%PAYMENT%'))"""),
            {"c": CAMPAIGN})).all()]
        print("2. погашено за бан, вернуть в реакции:     %d" % len(погашенные))
        откат["channels_revived"] = погашенные

        if a.go:
            if откат["pairs_to_failed"]:
                await db.execute(t("""
                    UPDATE campaign_channel_assignments SET status = 'failed'
                    WHERE id = ANY(:ids)"""), {"ids": откат["pairs_to_failed"]})
            if погашенные:
                await db.execute(t("""
                    UPDATE target_channels SET is_active = true, mode = 'react'
                    WHERE id = ANY(:ids)"""), {"ids": погашенные})

        # ── 3. Писать некем → в реакции ───────────────────────────
        # Считаем после шага 1: ложные 'joined' уже не держат канал.
        исключить = откат["pairs_to_failed"] if not a.go else []
        некем = [r[0] for r in (await db.execute(t("""
            SELECT tc.id FROM target_channels tc
            WHERE tc.campaign_id = :c AND tc.is_active AND tc.mode = 'comment'
              AND EXISTS (SELECT 1 FROM comment_queue q
                   WHERE q.campaign_id = :c AND lower(q.channel) = lower(tc.username)
                     AND q.status = 'failed'
                     AND (q.error ILIKE 'Channel private%' OR q.error ILIKE '%can''t write%'))
              AND NOT EXISTS (SELECT 1 FROM campaign_channel_assignments ca
                   JOIN accounts ac ON ac.id = ca.account_id AND ac.status = 'active'
                   WHERE lower(ca.channel_username) = lower(tc.username)
                     AND ca.status = 'joined' AND NOT (ca.id = ANY(:ex)))"""),
            {"c": CAMPAIGN, "ex": исключить})).all()]
        print("3. comment-каналов, где писать стало некем: %d" % len(некем))
        откат["channels_to_react"] = некем

        if a.go:
            if некем:
                await db.execute(t("""
                    UPDATE target_channels SET mode = 'react'
                    WHERE id = ANY(:ids)"""), {"ids": некем})
            await db.commit()

        print()
        for r in (await db.execute(t("""
            SELECT mode, count(*) FILTER (WHERE is_active), count(*)
            FROM target_channels WHERE campaign_id = :c GROUP BY 1 ORDER BY 3 DESC"""),
                {"c": CAMPAIGN})).all():
            print("   %-9s активных %-6d всего %d" % tuple(r))

    if a.go:
        путь = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "react_repair_откат_%s.json"
                            % datetime.now().strftime("%Y%m%d_%H%M%S"))
        with open(путь, "w", encoding="utf-8") as f:
            json.dump(откат, f, ensure_ascii=False)
        print()
        print("ПРИМЕНЕНО. Файл отката: %s" % путь)
    else:
        print()
        print("сухой прогон — ничего не изменено; применить: --go")


asyncio.run(main())
