"""
profile_seeder.py — «выращивание» органичного профиля прокси.

Идея:
    WAF-системы (Cloudflare, Akamai, DataDome) считают trust score
    не только по IP, но и по фингерпринту БРАУЗЕРА. Свежий CloakBrowser
    профиль с cookie только от TGStat → «этот юзер существует только
    для парсинга TGStat» → повышенный score → 403.

    Профиль с cookie от 5-10 популярных сайтов (Wikipedia, Google,
    YouTube, VK, ...) выглядит как «обычный человек, который иногда
    заходит на TGStat» → низкий score → пропуск без challenge.

Стратегия:
    Постепенное seeding — 1-2 сайта за сессию, а не 10 сразу.
    Bulk-визит 10 сайтов за 60 сек = ТОЖЕ ботовский паттерн (человек
    так не делает). Профиль подрастает во времени.

    Затухающая нужность: чем больше seed-сессий сделано, тем реже
    делаем новые. После 5 сессий — стоп (профиль достаточно оброс).

Состояние хранится в `{profile_dir}/.seed_meta.json`:
    {
      "seeds_done": 3,           # сколько seed-сессий прошло
      "last_seed_at": 1704412800,# unix ts последней seed-сессии
      "sites_visited": [...],    # какие сайты уже посещали
      "created_at": ...          # когда впервые сюда пришли
    }
"""

from __future__ import annotations
import json
import logging
import random
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# Пул «органичных» сайтов для seeding.
# Критерии выбора:
#   - Популярные (реальные юзеры их точно посещают в диапазоне ±дни)
#   - Не имеют собственной агрессивной bot-детекции на публичных страницах
#   - Ставят разнообразные first-party cookie
#   - Смыслово сочетаются с TGStat (Telegram-related audience):
#     русскоговорящие, tech-savvy, читатели новостей
SEED_SITES: list[dict] = [
    # ── Общие «нейтральные» ────────────────────────────────────
    {"url": "https://en.wikipedia.org/wiki/Special:Random",
     "min_dwell": 8.0, "max_dwell": 20.0, "tag": "wiki"},
    {"url": "https://www.google.com/search?q=telegram+news",
     "min_dwell": 5.0, "max_dwell": 15.0, "tag": "google"},
    {"url": "https://www.youtube.com/",
     "min_dwell": 6.0, "max_dwell": 18.0, "tag": "youtube"},
    {"url": "https://github.com/trending",
     "min_dwell": 5.0, "max_dwell": 15.0, "tag": "github"},
    {"url": "https://stackoverflow.com/questions",
     "min_dwell": 5.0, "max_dwell": 12.0, "tag": "stackoverflow"},
    # ── Ру-аудитория (TGStat в основном ру) ────────────────────
    {"url": "https://vk.com/",
     "min_dwell": 4.0, "max_dwell": 12.0, "tag": "vk"},
    {"url": "https://yandex.ru/",
     "min_dwell": 3.0, "max_dwell": 10.0, "tag": "yandex"},
    {"url": "https://habr.com/ru/",
     "min_dwell": 5.0, "max_dwell": 15.0, "tag": "habr"},
    # ── Смежное (Telegram-tangent) ─────────────────────────────
    {"url": "https://telegram.org/",
     "min_dwell": 4.0, "max_dwell": 12.0, "tag": "telegram_org"},
    {"url": "https://web.telegram.org/",
     "min_dwell": 3.0, "max_dwell": 8.0, "tag": "web_telegram"},
]

# После скольких seed-сессий профиль считается «достаточно оброс»
MAX_SEEDS_PER_PROFILE = 5

# Минимум секунд между seed-сессиями (не пылим за 1 день, распределяем)
MIN_INTERVAL_BETWEEN_SEEDS_SEC = 6 * 3600  # 6 часов

# Сколько сайтов за одну seed-сессию (не все — распределяем во времени)
SITES_PER_SESSION_MIN = 1
SITES_PER_SESSION_MAX = 3


def _meta_path(profile_dir: str | Path) -> Path:
    return Path(profile_dir) / ".seed_meta.json"


def _load_meta(profile_dir: str | Path) -> dict:
    p = _meta_path(profile_dir)
    if not p.exists():
        return {
            "seeds_done": 0,
            "last_seed_at": 0,
            "sites_visited": [],
            "created_at": int(time.time()),
        }
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        # Битый JSON — начнём с нуля, не рушим прогон
        return {"seeds_done": 0, "last_seed_at": 0, "sites_visited": []}


def _save_meta(profile_dir: str | Path, meta: dict) -> None:
    p = _meta_path(profile_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug(f"[seeder] не смог сохранить {p}: {e}")


def should_seed(profile_dir: str | Path) -> bool:
    """True если этот профиль пора «оброс», иначе False.

    Условия seed:
      1. Ещё не сделали MAX_SEEDS_PER_PROFILE сессий И
      2. С последней seed-сессии прошло минимум MIN_INTERVAL_BETWEEN_SEEDS_SEC
    """
    meta = _load_meta(profile_dir)
    if meta.get("seeds_done", 0) >= MAX_SEEDS_PER_PROFILE:
        return False
    since_last = time.time() - meta.get("last_seed_at", 0)
    if since_last < MIN_INTERVAL_BETWEEN_SEEDS_SEC:
        return False
    return True


def _pick_sites(meta: dict, n: int) -> list[dict]:
    """Выбираем сайты для этой сессии.

    Приоритет: те что ещё НЕ посещали. Если все посетили — рандом из пула.
    """
    visited_tags = set(meta.get("sites_visited", []))
    unvisited = [s for s in SEED_SITES if s["tag"] not in visited_tags]
    pool = unvisited if unvisited else SEED_SITES
    n = max(1, min(n, len(pool)))
    return random.sample(pool, n)


async def seed_profile(
    context,
    profile_dir: str | Path,
    per_page_timeout_sec: float = 30.0,
) -> dict:
    """Запускает одну seed-сессию: посещает 1-3 сайта, ставит cookie.

    Args:
        context: активный CloakBrowser BrowserContext для этого прокси.
        profile_dir: путь к профилю на диске (для .seed_meta.json).
        per_page_timeout_sec: таймаут на страницу.

    Returns:
        dict с summary (seeds_done_after, sites_visited_this_run, errors).

    Failures НЕ пробрасываются — если сайт лежит или прокси не пускает,
    seed молча пропускается, чтобы не сорвать основную работу скрейпера.
    """
    meta = _load_meta(profile_dir)
    n_sites = random.randint(SITES_PER_SESSION_MIN, SITES_PER_SESSION_MAX)
    sites = _pick_sites(meta, n_sites)

    logger.info(
        f"[seeder] Profile {Path(profile_dir).name}: seed-сессия #{meta['seeds_done'] + 1}, "
        f"визитим {len(sites)} сайт(а): {[s['tag'] for s in sites]}"
    )

    import asyncio  # локальный импорт чтобы модуль был sync-friendly
    ok_tags: list[str] = []
    errors: list[str] = []

    for site in sites:
        page = None
        try:
            page = await context.new_page()
            await page.goto(
                site["url"],
                wait_until="domcontentloaded",
                timeout=int(per_page_timeout_sec * 1000),
            )
            # Небольшой скролл — эмулируем чтение
            try:
                await page.mouse.wheel(0, random.randint(300, 800))
            except Exception:
                pass
            dwell = random.uniform(site["min_dwell"], site["max_dwell"])
            await asyncio.sleep(dwell)
            ok_tags.append(site["tag"])
            logger.debug(
                f"[seeder] ✓ {site['tag']}: {site['url']} ({dwell:.1f}с)"
            )
        except Exception as e:
            errors.append(f"{site['tag']}: {type(e).__name__}")
            logger.debug(f"[seeder] ✗ {site['tag']} {type(e).__name__}: {e}")
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass

    # Апдейт мета
    meta["seeds_done"] = meta.get("seeds_done", 0) + 1
    meta["last_seed_at"] = int(time.time())
    visited = set(meta.get("sites_visited", []))
    visited.update(ok_tags)
    meta["sites_visited"] = sorted(visited)
    _save_meta(profile_dir, meta)

    logger.info(
        f"[seeder] Profile {Path(profile_dir).name}: seed done. "
        f"seeds_done={meta['seeds_done']}/{MAX_SEEDS_PER_PROFILE}, "
        f"total_domains={len(meta['sites_visited'])}, "
        f"ok={len(ok_tags)}, err={len(errors)}"
    )
    return {
        "seeds_done_after": meta["seeds_done"],
        "ok_tags": ok_tags,
        "errors": errors,
        "sites_visited_total": len(meta["sites_visited"]),
    }


def summarize_pool_seeding(profiles_root: str | Path) -> dict:
    """Утилита для UI/логов: сколько профилей уже оброс, сколько ещё нет."""
    root = Path(profiles_root)
    if not root.exists():
        return {"total": 0, "fully_seeded": 0, "partial": 0, "fresh": 0}
    total = fully = partial = fresh = 0
    for prof in root.iterdir():
        if not prof.is_dir():
            continue
        total += 1
        meta = _load_meta(prof)
        n = meta.get("seeds_done", 0)
        if n == 0:
            fresh += 1
        elif n >= MAX_SEEDS_PER_PROFILE:
            fully += 1
        else:
            partial += 1
    return {
        "total": total, "fully_seeded": fully,
        "partial": partial, "fresh": fresh,
    }
