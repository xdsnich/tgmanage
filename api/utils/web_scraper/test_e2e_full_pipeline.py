"""
Полный e2e: Stage1 (rating) → Stage2 (detail) → JSON output.
Использует прокси из БД. Лимит Stage2 = 5 каналов чтобы тест шёл быстро.
"""

import asyncio
import sys
import functools
from pathlib import Path

api_dir = Path(__file__).resolve().parent.parent.parent
if str(api_dir) not in sys.path:
    sys.path.insert(0, str(api_dir))

# Принудительно unbuffered print, иначе на Windows ничего не видно
# пока процесс не завершится.
print = functools.partial(print, flush=True)


async def get_proxies_from_db() -> list[str]:
    from sqlalchemy import select
    from database import AsyncSessionLocal
    from models.proxy import Proxy

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Proxy).where(Proxy.is_valid == True)
        )).scalars().all()
        out = []
        for p in rows:
            scheme = p.protocol.value if hasattr(p.protocol, "value") else str(p.protocol)
            if p.login and p.password:
                out.append(f"{scheme}://{p.login}:{p.password}@{p.host}:{p.port}")
            else:
                out.append(f"{scheme}://{p.host}:{p.port}")
        return out


async def main():
    # Подменяем _publish_progress на console-print, _run() сам сделает всё
    import tasks.web_scraper_tasks as t

    print("[1] Готовлю прокси из БД...")
    proxies = await get_proxies_from_db()
    print(f"    valid: {len(proxies)}")

    # Stage 1 URL: 1 страница рейтинга RU
    urls = ["https://tgstat.com/ratings/channels?country=RU"]

    options = {
        "extractor": "tgstat",
        "max_workers": 1,                   # последовательно — меньше нагрузка
        "max_retries": 3,
        "page_timeout_sec": 90.0,           # детальные страницы TGStat медленные
        "cooldown_min_sec": 180,
        "cooldown_max_sec": 240,
        "node_rotation_min_sec": 5.0,
        "node_rotation_max_sec": 10.0,
        "stage2_limit": 3,                  # ещё меньше для скорости
        "stage2_cooldown_min_sec": 60,
        "stage2_cooldown_max_sec": 120,
        "camoufox": {"humanize": True, "headless": True},
    }

    print("[2] Запускаю _run() напрямую (минуя Celery)...")
    import uuid
    job_id = uuid.uuid4().hex[:12]
    # Используем фейкового user_id = 1 (для теста БД-сохранения)
    user_id = 1

    result = await t._run(user_id, job_id, urls, proxies, options)

    print("\n=== RESULT ===")
    import json
    print(json.dumps(result, indent=2, ensure_ascii=False))

    # Проверим что в БД появились каналы
    if result.get("ok"):
        print("\n[3] Проверяю БД ParsedChannel для user_id=1 ...")
        from sqlalchemy import select, func
        from database import AsyncSessionLocal
        from models.parsed_channel import ParsedChannel

        async with AsyncSessionLocal() as db:
            recent = (await db.execute(
                select(ParsedChannel).where(
                    ParsedChannel.user_id == user_id,
                    ParsedChannel.search_query.like(f"tgstat_web:{job_id}%"),
                )
            )).scalars().all()
            print(f"    в БД появилось {len(recent)} каналов:")
            for r in recent:
                print(f"      @{r.username}  subs={r.subscribers}  has_comments={r.has_comments}  title={r.title!r}")


if __name__ == "__main__":
    asyncio.run(main())
