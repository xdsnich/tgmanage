"""
Одноразовая утилита: загрузить страницу tgstat и посмотреть в каком
именно HTML живут карточки канала, чтобы починить селекторы.
"""

import asyncio
import sys
from pathlib import Path

api_dir = Path(__file__).resolve().parent.parent.parent
if str(api_dir) not in sys.path:
    sys.path.insert(0, str(api_dir))


async def main():
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(humanize=True, headless=True) as browser:
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto("https://tgstat.com/ratings/channels",
                        wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        # Вытаскиваем одну карточку с её родителем
        sample = await page.evaluate("""
            () => {
                const a = document.querySelector('a[href*="/channel/@"]');
                if (!a) return {error: 'no anchor'};
                let card = a.closest('.card, .peer-item, li, .row, tr, div');
                let level = 0;
                while (card && card.outerHTML.length < 200 && level < 5) {
                    card = card.parentElement;
                    level++;
                }
                return {
                    href: a.getAttribute('href'),
                    cardHTML: card ? card.outerHTML.substring(0, 3000) : null,
                    cardText: card ? (card.innerText || '').substring(0, 500) : null,
                };
            }
        """)
        print("=== HREF ===")
        print(sample.get("href"))
        print("\n=== CARD TEXT (first 500 chars) ===")
        print(sample.get("cardText"))
        print("\n=== CARD HTML (first 3000 chars) ===")
        print(sample.get("cardHTML"))
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
