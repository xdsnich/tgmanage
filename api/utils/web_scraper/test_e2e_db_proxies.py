"""
test_e2e_db_proxies.py — реальный end-to-end тест:
  1. Берём прокси из БД (Proxy, валидные)
  2. Запускаем WebScraper на одной странице /ratings/chats
  3. Парсим, печатаем что вышло
  4. Печатаем какие были ошибки если что упало

Запуск (из api):
    python -m utils.web_scraper.test_e2e_db_proxies
"""

import asyncio
import sys
from pathlib import Path

api_dir = Path(__file__).resolve().parent.parent.parent
if str(api_dir) not in sys.path:
    sys.path.insert(0, str(api_dir))


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


async def progress_cb(snap):
    print(
        f"[progress] status={snap.get('status')} "
        f"done={snap.get('done')}/{snap.get('total')} "
        f"ok={snap.get('ok')} fail={snap.get('failed')} "
        f"current={snap.get('current_url','')[:80]}"
    )


async def main():
    print("[1/4] Читаю прокси из БД...")
    proxies = await get_proxies_from_db()
    print(f"      OK -- valid proxies in DB: {len(proxies)}")
    if len(proxies) < 3:
        print("      FATAL -- меньше 3 валидных прокси, тест невозможен")
        return

    print("[2/4] Готовлю WebScraper...")
    from utils.web_scraper import WebScraper, tgstat_channels_extractor

    # Берём 1 страницу для теста чтоб быстрее
    urls = ["https://tgstat.com/ratings/chats?country=RU"]

    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="ws_e2e_"))
    jsonl = tmp / "result.jsonl"
    print(f"      OK -- jsonl path: {jsonl}")

    scraper = WebScraper(
        proxies=proxies,
        checkpoint_path=jsonl,
        extractor=tgstat_channels_extractor,
        max_workers=2,
        max_retries=2,
        page_timeout_sec=45.0,
        cooldown_min_sec=180,  # для теста — 3 мин, чтоб не ждать 15
        cooldown_max_sec=240,
        node_rotation_min_sec=5.0,
        node_rotation_max_sec=10.0,
        progress_callback=progress_cb,
        camoufox_kwargs={"humanize": True, "headless": True},
    )

    print("[3/4] Запускаю scraper...")
    try:
        async with scraper:
            await scraper.run(urls)
    except Exception as e:
        print(f"      FATAL -- scraper crashed: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return

    print("[4/4] Читаю результат из JSONL...")
    import json
    if not jsonl.exists():
        print("      FAIL -- JSONL не создан")
        return

    lines = jsonl.read_text(encoding="utf-8").strip().split("\n")
    print(f"      JSONL strings: {len(lines)}")
    total_channels = 0
    sample_shown = False
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("success"):
            data = rec.get("data") or {}
            channels = data.get("channels") or []
            total_channels += len(channels)
            if channels and not sample_shown:
                sample_shown = True
                print(f"      SAMPLE (first 5 channels из первого среза):")
                for ch in channels[:5]:
                    print(f"        {ch.get('username'):<25} "
                          f"subs={ch.get('subscribers') or '?':<10} "
                          f"kind={ch.get('kind')} "
                          f"linked={ch.get('linked_channel_guess') or '—'} "
                          f"title={ch.get('title')!r}")
        else:
            print(f"      FAIL row: url={rec.get('url')} err={rec.get('error')}")

    if total_channels == 0:
        print("\n      ❌ FAIL -- ноль каналов. Проверь логи скрейпера выше.")
    else:
        print(f"\n      ✅ OK -- total channels: {total_channels}")


if __name__ == "__main__":
    asyncio.run(main())
