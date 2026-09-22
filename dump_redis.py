# -*- coding: utf-8 -*-
"""Выгрузить память Redis в файл — для резервной копии.

Что там лежит и почему это важно:

    gramgpt:rt:limited:*   реестр ограниченных аккаунтов со сроками.
                           Потеряв его, система снова отправит
                           забаненный аккаунт писать — и он получит
                           ограничение повторно, уже дольше.
    gramgpt:rt:quota:*     сколько кто сегодня написал.
    gramgpt:rt:since:*     с какого дня аккаунт в деле — от этого
                           считается разгон нормы.
    gramgpt:rt:reacts:*    счётчики реакций.
    gramgpt:funnel:snap:*  полуночные снимки воронки.

Замки адресов и пометки «недавно писал» не сохраняем сознательно: они
живут минутами и через полчаса всё равно протухнут.

Запуск:  python dump_redis.py <файл.json>
"""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Скрипт вызывают и из корня, и из api — кладём оба пути, чтобы импорт
# находился в любом случае. Иначе выгрузка молча не срабатывает, а
# заметить это в резервной копии можно слишком поздно.
import os as _os
_корень = _os.path.dirname(_os.path.abspath(__file__))
for _p in (_os.path.join(_корень, "api"), _корень, "."):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main():
    if len(sys.argv) < 2:
        print("укажи файл")
        return
    from utils.redis_pool import get_redis
    r = get_redis()

    # Берём только долгоживущее. Короткие замки восстанавливать вредно:
    # после переезда они заперли бы адреса на пустом месте.
    ШАБЛОНЫ = ("gramgpt:rt:limited:*", "gramgpt:rt:quota:*",
               "gramgpt:rt:since:*", "gramgpt:rt:reacts:*",
               "gramgpt:funnel:snap:*", "gramgpt:rt:sent:*")

    out = {}
    for шаблон in ШАБЛОНЫ:
        for k in r.scan_iter(match=шаблон, count=500):
            k = k.decode() if isinstance(k, bytes) else k
            try:
                тип = r.type(k)
                тип = тип.decode() if isinstance(тип, bytes) else тип
                ttl = r.ttl(k)
                if тип == "hash":
                    d = {}
                    for a, b in r.hgetall(k).items():
                        a = a.decode() if isinstance(a, bytes) else a
                        b = b.decode() if isinstance(b, bytes) else b
                        d[a] = b
                    out[k] = {"type": "hash", "value": d, "ttl": ttl}
                elif тип == "string":
                    v = r.get(k)
                    out[k] = {"type": "string",
                              "value": v.decode() if isinstance(v, bytes) else v,
                              "ttl": ttl}
            except Exception:
                continue

    with open(sys.argv[1], "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(len(out))


main()
