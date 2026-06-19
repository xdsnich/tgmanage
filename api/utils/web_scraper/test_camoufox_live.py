"""
test_camoufox_live.py — live-проверка что Camoufox реально запускается
и что tgstat_extractor корректно вытаскивает карточки каналов.

Запуск (без прокси, через прямой интернет):
    cd api
    python -m utils.web_scraper.test_camoufox_live

Не использует наш NodeRotator/Pool — это просто
"подключиться к tgstat → проверить что extractor возвращает что-то".

Если все 4 шага OK — значит остальная инфра (worker pool, cooldown,
checkpoint) тоже будет работать, потому что они уже покрыты
test_smoke.py.
"""

import asyncio
import sys
from pathlib import Path

api_dir = Path(__file__).resolve().parent.parent.parent
if str(api_dir) not in sys.path:
    sys.path.insert(0, str(api_dir))


async def main():
    print("[1/4] Импортируем Camoufox...")
    from camoufox.async_api import AsyncCamoufox
    print("      OK")

    print("[2/4] Запускаем браузер (headless)...")
    async with AsyncCamoufox(humanize=True, headless=True) as browser:
        print("      OK")

        print("[3/4] Открываем рейтинг tgstat (без прокси, прямой инет)...")
        ctx = await browser.new_context()
        page = await ctx.new_page()
        url = "https://tgstat.com/ratings/channels"
        response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        status = response.status if response else 0
        title = await page.title()
        print(f"      OK -- HTTP {status}, title={title[:80]!r}")

        print("[4/4] Запускаем tgstat_channels_extractor...")
        from utils.web_scraper.tgstat_extractor import tgstat_channels_extractor
        data = await tgstat_channels_extractor(page)
        print(f"      OK -- channels_count={data.get('channels_count', 0)}")
        for ch in (data.get("channels") or [])[:5]:
            print(f"        - {ch.get('username')!s:<25} subs={ch.get('subscribers')!s:<10} "
                  f"comments={ch.get('has_comments')!s:<6} title={ch.get('title')!r}")
        if data.get("channels_count", 0) == 0:
            print("      WARN -- 0 каналов извлечено. extractor нужно подкрутить под текущую разметку.")

        await ctx.close()

    print("\nGotov.")


if __name__ == "__main__":
    asyncio.run(main())
