# -*- coding: utf-8 -*-
"""Собрать новую realtime-кампанию: каналы + режимы + назначение аккаунтам.

Только база, живых подключений нет. Подписка — отдельным шагом.

Набор каналов (самые крупные первыми, свежепроверенные уже внутри):
  comment — куда МОЖНО писать        (cc.commentable)
  react   — писать нельзя, реакции да (write_banned / needs_approval)

Раскладка: 1 аккаунт на канал. Каждому аккаунту COMMENT_PER комментинга и
REACT_PER реакций. Каналы режутся по аккаунтам без пересечений.

По умолчанию сухой прогон. Применить: --go. Откат: --rollback <id>.

Запуск из папки api:
    python ..\\tools\\build_campaign.py
    python ..\\tools\\build_campaign.py --go
"""
import argparse, asyncio, os, sys
from datetime import datetime
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.abspath("."))

НЕ_ТРОГАТЬ = [48, 95, 217]
COMMENT_PER = int(os.getenv("CAMP_COMMENT_PER", "150"))
REACT_PER = int(os.getenv("CAMP_REACT_PER", "50"))
ИМЯ = os.getenv("CAMP_NAME", "Realtime октябрь — крупные + реакции")
REACT_WHERE = "NOT cc.commentable AND cc.reason IN ('write_banned','needs_approval')"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true")
    ap.add_argument("--rollback", type=int)
    a = ap.parse_args()

    from sqlalchemy import text as t
    from utils.db_pool import async_session as S

    if a.rollback:
        async with S() as db:
            await db.execute(t("DELETE FROM campaign_channel_assignments WHERE campaign_id=:c"), {"c": a.rollback})
            await db.execute(t("DELETE FROM target_channels WHERE campaign_id=:c"), {"c": a.rollback})
            await db.execute(t("DELETE FROM campaigns WHERE id=:c"), {"c": a.rollback})
            await db.commit()
        print("кампания %d и её каналы/назначения удалены" % a.rollback)
        return

    async with S() as db:
        q = lambda s, **p: db.execute(t(s), p)
        явные = os.getenv("CAMP_ACCOUNT_IDS", "").strip()
        if явные:
            # Явный список рабочих аккаунтов (после аудита @SpamBot).
            акки = [int(x) for x in явные.split(",") if x.strip()]
        else:
            акки = [r[0] for r in (await q("""
                SELECT a.id FROM accounts a
                JOIN proxies p ON p.id=a.proxy_id AND p.is_valid AND (p.expires_at IS NULL OR p.expires_at>now())
                WHERE a.status='active' AND NOT (a.id=ANY(:ex))
                ORDER BY a.id""", ex=НЕ_ТРОГАТЬ)).all()]
        n_acc = len(акки)
        нужно_comment = n_acc * COMMENT_PER
        нужно_react = n_acc * REACT_PER

        async def topn(where, n):
            return [(r[0], r[1], r[2]) for r in (await q("""
                SELECT lower(pc.username), max(pc.subscribers), max(pc.title)
                FROM parsed_channels pc
                JOIN channel_capabilities cc ON lower(cc.channel_username)=lower(pc.username)
                WHERE pc.user_id=2 AND pc.subscribers>=10000 AND %s
                GROUP BY lower(pc.username)
                ORDER BY max(pc.subscribers) DESC LIMIT :n""" % where, n=n)).all()]

        comment = await topn("cc.commentable", нужно_comment)
        react = await topn(REACT_WHERE, нужно_react)

        print("аккаунтов: %d" % n_acc)
        print("комментинг: нужно %d, нашлось %d (до %s подписчиков)"
              % (нужно_comment, len(comment), comment[-1][1] if comment else 0))
        print("реакции:    нужно %d, нашлось %d (до %s подписчиков)"
              % (нужно_react, len(react), react[-1][1] if react else 0))
        print("аудитория comment: %d, react: %d"
              % (sum(x[1] for x in comment), sum(x[1] for x in react)))

        if not a.go:
            print("\nсухой прогон — применить: --go")
            return

        # ── Кампания ──
        cid = (await q("""
            INSERT INTO campaigns (user_id, name, status, account_ids, trigger_mode,
                                   trigger_percent, trigger_keywords, llm_provider, tone,
                                   custom_prompt, comment_length, max_comments, max_hours,
                                   comments_sent, delay_join, delay_comment, delay_between,
                                   campaign_type, created_at, updated_at)
            VALUES (2, :n, 'draft', CAST(:ids AS json), 'all',
                    100, CAST('[]' AS json), 'claude', 'positive',
                    '', 'medium', 2000000, 24,
                    0, 10, 250, 60,
                    'realtime', now(), now())
            RETURNING id""", n=ИМЯ, ids=__import__("json").dumps(акки))).scalar()
        print("\nсоздана кампания #%d (draft)" % cid)

        # ── Каналы ── режим через колонку mode (raw)
        async def вставить(список, mode):
            for u, subs, title in список:
                await q("""INSERT INTO target_channels (campaign_id, username, title, link, subscribers,
                               has_comments, last_post_id, comments_sent, is_active, mode, added_at)
                           VALUES (:c, :u, :t, :lnk, :subs, :hc, 0, 0, true, :m, now())""",
                        c=cid, u=u, t=(title or "")[:255], lnk="https://t.me/" + u,
                        subs=int(subs or 0), hc=(mode == "comment"), m=mode)
        await вставить(comment, "comment")
        await вставить(react, "react")
        await db.commit()
        print("каналов записано: %d comment + %d react" % (len(comment), len(react)))

        # ── Назначение: 1 аккаунт на канал, по кускам ──
        def раздать(список, per):
            наз = []
            for i, acc in enumerate(акки):
                наз += [(acc, u) for (u, _, _) in список[i * per:(i + 1) * per]]
            return наз
        назначения = раздать(comment, COMMENT_PER) + раздать(react, REACT_PER)
        # пачками
        for i in range(0, len(назначения), 1000):
            vals = назначения[i:i + 1000]
            await db.execute(t("""
                INSERT INTO campaign_channel_assignments
                    (campaign_id, account_id, channel_username, status, assigned_at)
                SELECT :c, x.a, x.u, 'pending', now()
                FROM unnest(CAST(:accs AS int[]), CAST(:chs AS text[])) AS x(a, u)"""),
                {"c": cid, "accs": [a for a, _ in vals], "chs": [u for _, u in vals]})
        await db.commit()
        print("назначений создано: %d (status=pending)" % len(назначения))
        print("\nГОТОВО. Кампания #%d в статусе draft, подписок ещё нет." % cid)
        print("откат: python ..\\tools\\build_campaign.py --rollback %d" % cid)


asyncio.run(main())
