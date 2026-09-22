# -*- coding: utf-8 -*-
"""Сторож realtime-кампании: темп, ошибки, флуды, узкие места.

Одним заходом отвечает на четыре вопроса, которые нужны каждый раз:
сколько написали, что ломается, не поймал ли кто ограничение и во что
именно упирается движок прямо сейчас.

Времена везде считаются от naive-UTC: created_at в базе наивный, а now()
— с поясом, и сравнение их напрямую молча смещает всё на три часа.
"""
import asyncio
import os
import sys
from datetime import date, datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

CAMP = 143


async def main():
    from sqlalchemy import text as t
    from utils.db_pool import async_session as S
    from utils.redis_pool import get_redis

    r = get_redis()
    час = datetime.utcnow() - timedelta(hours=1)
    сегодня = date.today().isoformat()

    async with S() as db:
        ids = (await db.execute(t(
            "SELECT account_ids FROM campaigns WHERE id=:c"), {"c": CAMP})).scalar() or []

        print("=" * 62)
        print("  realtime — %s" % datetime.now().strftime("%d.%m %H:%M"))
        print("=" * 62)

        # ── Темп ────────────────────────────────────────────────
        итог = dict((x[0], x[1]) for x in (await db.execute(t("""
            SELECT status, count(*) FROM comment_queue
            WHERE campaign_id=:c AND created_at::date=CURRENT_DATE
            GROUP BY 1"""), {"c": CAMP})).all())
        всего = sum(итог.values())
        готово = итог.get("done", 0)
        print("СЕГОДНЯ  написано %d   в очереди %d   сбоев %d   брошено %d"
              % (готово, итог.get("scheduled", 0) + итог.get("executing", 0),
                 итог.get("failed", 0), итог.get("aborted", 0)))
        if всего:
            print("         доля успеха %.0f%%" % (100.0 * готово / всего))

        за_час = (await db.execute(t("""
            SELECT count(*) FROM comment_queue
            WHERE campaign_id=:c AND status='done' AND executed_at > :h"""),
            {"c": CAMP, "h": час})).scalar()
        print("ЗА ЧАС   %d комментов (%.1f в минуту)" % (за_час, за_час / 60.0))

        # ── Норма ───────────────────────────────────────────────
        норма = потрачено = выбрали = 0
        for a in ids:
            сырое = r.get("gramgpt:rt:since:%d" % a)
            нач = (date.fromisoformat(сырое.decode() if isinstance(сырое, bytes)
                                      else str(сырое)) if сырое else date.today())
            # Читаем ту же настройку, что и движок: цифра 12 была зашита
            # в скрипт, а в .env её подняли до 20, и сторож занижал норму.
            старт = int(os.getenv("RT_RAMP_START", "20"))
            шаг = int(os.getenv("RT_RAMP_STEP", "9"))
            потолок = int(os.getenv("RT_PER_ACCOUNT_DAY", "40"))
            n = max(1, min(потолок, старт + шаг * max(0, (date.today() - нач).days)))
            u = int(r.get("gramgpt:rt:quota:%d:%s" % (a, сегодня)) or 0)
            норма += n
            потрачено += u
            if u >= n:
                выбрали += 1
        print("НОРМА    %d из %d  (осталось %d);  выбрали полностью %d акк. из %d"
              % (потрачено, норма, норма - потрачено, выбрали, len(ids)))

        # ── Что ломается ────────────────────────────────────────
        сбои = (await db.execute(t("""
            SELECT left(COALESCE(error,'—'), 52) e, count(*) c
            FROM comment_queue
            WHERE campaign_id=:c AND status IN ('failed','aborted') AND created_at > :h
            GROUP BY 1 ORDER BY 2 DESC LIMIT 6"""), {"c": CAMP, "h": час})).all()
        if сбои:
            print()
            print("СБОИ ЗА ЧАС")
            for e, c in сбои:
                print("   %-54s %d" % (e, c))

        # ── Флуды и ограничения ─────────────────────────────────
        флуд = (await db.execute(t("""
            SELECT a.id, a.phone, l.error_type, l.created_at
            FROM account_error_logs l JOIN accounts a ON a.id=l.account_id
            WHERE l.created_at > :h
              AND (l.error_type ILIKE '%flood%' OR l.error_type ILIKE '%peer%'
                   OR l.message ILIKE '%flood%' OR l.message ILIKE '%spam%')
            ORDER BY l.created_at DESC LIMIT 12"""), {"h": час})).all()
        print()
        if флуд:
            print("⚠ ФЛУДЫ И ОГРАНИЧЕНИЯ ЗА ЧАС: %d" % len(флуд))
            for i, p, e, d in флуд:
                print("   acc#%-5s %-16s %-28s %s" % (i, p, str(e)[:28],
                                                      d.strftime("%H:%M")))
        else:
            print("ФЛУДОВ НЕТ")

        отдых = (await db.execute(t("""
            SELECT count(*) FROM accounts WHERE id = ANY(:i) AND status <> 'active'"""),
            {"i": ids})).scalar()
        if отдых:
            print("⚠ выбыло из строя аккаунтов: %d" % отдых)

        # ── Узкое место ─────────────────────────────────────────
        ждут = (await db.execute(t("""
            SELECT count(*) FROM comment_queue
            WHERE campaign_id=:c AND status='scheduled'"""), {"c": CAMP})).scalar()
        каналов = (await db.execute(t("""
            SELECT count(*) FROM target_channels WHERE campaign_id=:c AND is_active"""),
            {"c": CAMP})).scalar()
        print()
        print("СОСТОЯНИЕ  каналов в наблюдении %d,  заданий ждёт адреса %d"
              % (каналов, ждут))


asyncio.run(main())
