# -*- coding: utf-8 -*-
"""Снять застрявшие замки realtime после перезагрузки/краха.

После жёсткого ребута процессы умирают, не освободив замки в Redis:
  gramgpt:ip_owner:*    (600с, nx, никогда не продлевается) — держит прокси
  gramgpt:account_lock:* — держит аккаунт
  gramgpt:ip_lock:*      — короткий, но на всякий случай
Пока их не снять, диспетчер realtime думает, что адреса/аккаунты заняты,
и ничего не пишет, пока TTL сам не истечёт. На старте процессов НИКТО этих
замков не держит, поэтому их безопасно удалить разом.

Запуск из папки api:  python ..\\tools\\clear_locks.py
"""
import os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.abspath("."))

PATTERNS = [
    "gramgpt:ip_owner:*",
    "gramgpt:account_lock:*",
    "gramgpt:ip_lock:*",
]


def main():
    from utils.redis_pool import get_redis
    r = get_redis()
    total = 0
    for pat in PATTERNS:
        keys = list(r.scan_iter(match=pat))
        if keys:
            r.delete(*keys)
        total += len(keys)
        # ASCII-вывод: лог автозапуска читает PowerShell 5.1, кириллица в
        # stdout там превращается в мусор.
        print("cleared %-26s %d" % (pat, len(keys)))
    print("TOTAL locks cleared: %d" % total)


if __name__ == "__main__":
    main()
