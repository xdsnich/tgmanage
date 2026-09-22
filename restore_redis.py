# -*- coding: utf-8 -*-
"""Вернуть память Redis из резервной копии.

Восстанавливает то, что система помнит о себе: кто ограничен и до какого
числа, сколько кому осталось нормы на сегодня, с какого дня аккаунт в
деле (от этого считается разгон), счётчики реакций, снимки воронки.

Особая осторожность со сроками жизни. Ключ «норма на сегодня» живёт двое
суток, и если восстановить его через неделю, он оживёт с чужой датой в
имени — вреда не будет, но и толку тоже. А вот реестр ограничений вернуть
важно: без него забаненный аккаунт снова пойдёт писать и получит
ограничение повторно, уже на больший срок.

Запуск:  python restore_redis.py <файл.json>
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_корень = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_корень, "api"), _корень, "."):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main():
    if len(sys.argv) < 2:
        print("укажи файл с выгрузкой")
        return
    from utils.redis_pool import get_redis
    r = get_redis()

    with open(sys.argv[1], encoding="utf-8") as f:
        данные = json.load(f)

    вернули = пропустили = 0
    for ключ, з in данные.items():
        try:
            ttl = з.get("ttl")
            if з["type"] == "hash":
                for поле, знач in (з["value"] or {}).items():
                    r.hset(ключ, поле, знач)
            elif з["type"] == "string":
                r.set(ключ, з["value"])
            else:
                пропустили += 1
                continue
            # Срок жизни возвращаем, только если он был осмысленный.
            # -1 означает «вечно», -2 — «ключа уже не было».
            if isinstance(ttl, int) and ttl > 0:
                r.expire(ключ, ttl)
            вернули += 1
        except Exception:
            пропустили += 1

    print("возвращено ключей: %d" % вернули)
    if пропустили:
        print("пропущено: %d" % пропустили)

    огр = len([k for k in данные if ":limited:" in k])
    if огр:
        print()
        print("в том числе реестр ограниченных аккаунтов: %d" % огр)
        print("это главное — без него забаненный аккаунт снова пойдёт писать")


main()
