# -*- coding: utf-8 -*-
"""100 аккаунтов на 34 прокси, не больше трёх на адрес.

Кто остаётся на своём адресе: самый ценный — рабочий без ограничения с
наибольшим числом комментариев. Смена адреса для Telegram — сигнал сама
по себе, поэтому переезжают наименее ценные: карантин без комментариев.

Аккаунты вне сотни от этих прокси отвязываем (proxy_id = NULL), иначе на
адресе снова окажется по 4-6 аккаунтов. Защищённых (Жека, Мария) не
трогаем вовсе — ни переездом, ни отвязкой.

По умолчанию — сухой прогон. Применить: --go. Прежняя привязка пишется в
файл отката рядом со скриптом.

Запуск из папки api:
    python ..\\tools\\proxy_rebalance.py
    python ..\\tools\\proxy_rebalance.py --go
"""
import argparse, asyncio, json, os, sys
from collections import defaultdict
from datetime import datetime
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.abspath("."))

НЕ_ТРОГАТЬ = {48, 95, 217}
НА_ПРОКСИ = 3
АККАУНТОВ = 100


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true")
    a = ap.parse_args()

    from sqlalchemy import text as t
    from utils.db_pool import async_session as S
    from utils.redis_pool import get_redis

    r = get_redis()
    ограничен = set()
    for k in r.scan_iter(match="gramgpt:rt:limited:*"):
        k = k.decode() if isinstance(k, bytes) else k
        ограничен.add(int(k.rsplit(":", 1)[1]))

    async with S() as db:
        живые = {row[0]: row[1] for row in (await db.execute(t("""
            SELECT id, host FROM proxies WHERE is_valid AND expires_at > now()"""))).all()}
        accs = (await db.execute(t("""
            SELECT a.id, a.status::text, a.proxy_id,
                   (SELECT count(*) FROM comment_queue q WHERE q.account_id = a.id AND q.status = 'done')
            FROM accounts a"""))).all()

    def ценность(x):
        aid, st, pid, комм = x
        return (0 if st == "active" and aid not in ограничен else 1 if st == "active" else 2, -комм, aid)

    кандидаты = sorted([x for x in accs if x[0] not in НЕ_ТРОГАТЬ and x[1] in ("active", "quarantine")],
                       key=ценность)
    сотня = кандидаты[:АККАУНТОВ]
    в_сотне = {x[0] for x in сотня}

    на = defaultdict(list)
    лишние = []
    for x in сотня:
        (на[x[2]].append(x) if x[2] in живые else лишние.append(x))
    for pid in list(на):
        на[pid].sort(key=ценность)
        while len(на[pid]) > НА_ПРОКСИ:
            лишние.append(на[pid].pop())
    защищённые_на = defaultdict(int)
    for x in accs:
        if x[0] in НЕ_ТРОГАТЬ and x[2] in живые:
            защищённые_на[x[2]] += 1

    переезды = []
    for x in sorted(лишние, key=ценность):
        места = [pid for pid in живые if len(на[pid]) + защищённые_на[pid] < НА_ПРОКСИ]
        if not места:
            места = [pid for pid in живые if len(на[pid]) < НА_ПРОКСИ]
        цель = min(места, key=lambda pid: len(на[pid]) + защищённые_на[pid]) if места else None
        if цель:
            на[цель].append(x)
        переезды.append((x, цель))

    отвязать = [x for x in accs if x[0] not in в_сотне and x[0] not in НЕ_ТРОГАТЬ and x[2] in живые]

    print("сотня: рабочих без огр. %d, рабочих с огр. %d, карантин %d"
          % (sum(1 for x in сотня if x[1] == "active" and x[0] not in ограничен),
             sum(1 for x in сотня if x[1] == "active" and x[0] in ограничен),
             sum(1 for x in сотня if x[1] == "quarantine")))
    print("живых прокси: %d, мест: %d" % (len(живые), len(живые) * НА_ПРОКСИ))
    print("переездов: %d (не хватило места: %d)" % (len(переезды), sum(1 for _, c in переезды if not c)))
    print("отвязать от прокси (вне сотни): %d" % len(отвязать))
    for pid, n in защищённые_на.items():
        if n:
            print("  на proxy#%d (%s) сидит защищённый аккаунт — его не трогаю" % (pid, живые[pid]))
    print()
    for x, c in переезды:
        print("  переезд acc#%-4s %-10s комм %-4s proxy#%s → proxy#%s %s"
              % (x[0], x[1], x[3], x[2], c, живые.get(c, "")))
    print()
    print("РАСКЛАДКА:")
    for pid in sorted(живые, key=lambda p: живые[p]):
        print("  proxy#%-3s %-16s %s%s" % (pid, живые[pid], ", ".join("#%d" % x[0] for x in на[pid]),
                                          "  + защищённый" if защищённые_на[pid] else ""))

    if not a.go:
        print("\nсухой прогон — ничего не изменено; применить: --go")
        return

    откат = {"when": datetime.now().isoformat(timespec="seconds"),
             "proxy_before": {x[0]: x[2] for x in accs if x[0] in {y[0] for y, _ in переезды}
                              | {y[0] for y in отвязать}}}
    async with S() as db:
        for x, c in переезды:
            if c:
                await db.execute(t("UPDATE accounts SET proxy_id = :p WHERE id = :a"), {"p": c, "a": x[0]})
        if отвязать:
            await db.execute(t("UPDATE accounts SET proxy_id = NULL WHERE id = ANY(:ids)"),
                             {"ids": [x[0] for x in отвязать]})
        await db.commit()
    путь = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "proxy_rebalance_откат_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    json.dump(откат, open(путь, "w", encoding="utf-8"), ensure_ascii=False)
    print("\nПРИМЕНЕНО. Откат: %s" % путь)


asyncio.run(main())
