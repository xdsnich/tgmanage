# -*- coding: utf-8 -*-
"""Читаемый экспорт аккаунтов/каналов из базы в один JSON.
Это удобство для быстрого обзора флота (и чтобы Клод в новом чате сразу
увидел список), не замена дампу: полная база — в gramgpt.dump.

Запуск:  python export_accounts.py <путь_к_output.json>
"""
import asyncio
import json
import sys

OUT = sys.argv[1] if len(sys.argv) > 1 else "accounts-export.json"
DSN = "postgresql://gramgpt:gramgpt@localhost:5432/gramgpt"
TABLES = ["accounts", "target_channels", "campaigns",
          "campaign_channel_assignments", "account_subscriptions"]


async def main():
    import asyncpg
    conn = await asyncpg.connect(DSN)
    data = {}
    for t in TABLES:
        try:
            rows = await conn.fetch("SELECT * FROM %s" % t)
            data[t] = [dict(r) for r in rows]
        except Exception as e:
            data[t] = {"__error__": str(e)[:200]}
    await conn.close()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    n = sum(len(v) for v in data.values() if isinstance(v, list))
    print(n)


if __name__ == "__main__":
    asyncio.run(main())
