"""
GramGPT — tasks/web_scraper_tasks.py

Celery-таск-обёртка над WebScraper (api/utils/web_scraper).

Что делает:
  - Получает list URLs + list proxies + options из API-эндпоинта
  - Создаёт JSONL-файл для job'а в data/web_scraper/{user_id}/{job_id}.jsonl
  - Запускает WebScraper c progress_callback пишущим в Redis
  - cancel_check читает Redis-флаг — снаружи /stop эндпоинт его выставляет
  - При окончании пишет финальный snapshot прогресса со status=done/cancelled/error

Что НЕ делает:
  - Не парсит конкретный домен — extractor универсальный (title + meta + h1 +
    OG-теги + первые 5000 символов текста). Если нужно специфическое — это
    отдельная задача, расширим extractor по типу домена.
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from celery_app import celery_app

logger = logging.getLogger(__name__)
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# ── Redis keys ──────────────────────────────────────────────────────

def _progress_key(user_id: int, job_id: str) -> str:
    return f"gramgpt:web_scraper:progress:{user_id}:{job_id}"


def _cancel_key(user_id: int, job_id: str) -> str:
    return f"gramgpt:web_scraper:cancel:{user_id}:{job_id}"


def _job_dir(user_id: int) -> Path:
    """Корень для JSONL-результатов конкретного юзера."""
    p = Path(API_DIR) / ".." / "data" / "web_scraper" / str(user_id)
    return p.resolve()


def _jsonl_path(user_id: int, job_id: str) -> Path:
    return _job_dir(user_id) / f"{job_id}.jsonl"


# ── Helpers ─────────────────────────────────────────────────────────

def _run_async(coro):
    """sync→async bridge. Celery threads + asyncio = свой loop на task."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Universal page extractor ────────────────────────────────────────

async def _universal_extractor(page: Any) -> dict:
    """
    Универсальный extractor: title, meta description, OG-теги, H1, текст.

    Подходит для большинства публичных страниц (статистика каналов,
    карточки товаров, статьи). Для специфичных доменов — отдельный
    extractor, передавать через параметр extractor_name (todo).
    """
    # Все JS-evaluate'ы — в одном вызове, чтобы не дёргать pickup-стоимость
    # IPC до Camoufox несколько раз.
    data = await page.evaluate("""
        () => {
            const meta = (name, attr='name') =>
                document.querySelector(`meta[${attr}="${name}"]`)?.content || null;
            const h1List = Array.from(document.querySelectorAll('h1'))
                .map(h => h.innerText.trim())
                .filter(Boolean);
            // innerText даёт уже rendered текст без HTML-тегов
            const bodyText = (document.body?.innerText || '').slice(0, 5000);
            return {
                title: document.title || null,
                meta_description: meta('description'),
                meta_keywords: meta('keywords'),
                og_title: meta('og:title', 'property'),
                og_description: meta('og:description', 'property'),
                og_image: meta('og:image', 'property'),
                og_url: meta('og:url', 'property'),
                canonical: document.querySelector('link[rel="canonical"]')?.href || null,
                h1: h1List,
                lang: document.documentElement.lang || null,
                body_excerpt: bodyText,
                final_url: window.location.href,
            };
        }
    """)
    return data


# ── Main task ───────────────────────────────────────────────────────

@celery_app.task(bind=True, name="tasks.web_scraper_tasks.run_web_scraper")
def run_web_scraper(self, user_id: int, job_id: str, urls: list[str],
                    proxies: list[str], options: dict | None = None):
    """
    Запуск скрейпинга в Celery worker.

    Args:
        user_id: владелец job'а — для изоляции файлов и Redis keys
        job_id: UUID, генерится API эндпоинтом при /start
        urls: список URL для обработки
        proxies: список прокси (рекомендуется 34, можно меньше — будет warn)
        options: {
            "extractor": "universal" | "tgstat",   # выбор стратегии чтения
            "max_workers": 3,
            "max_retries": 3,
            "page_timeout_sec": 60,
            "cooldown_min_sec": 900,
            "cooldown_max_sec": 1200,
            "node_rotation_min_sec": 15,
            "node_rotation_max_sec": 35,
            "camoufox": {"humanize": true, "geoip": true, "locale": "en-US"}
        }
    """
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)
    options = options or {}
    logger.info(
        f"[web_scraper] start user={user_id} job={job_id} "
        f"urls={len(urls)} proxies={len(proxies)}"
    )
    return _run_async(_run(user_id, job_id, urls, proxies, options))


async def _run(user_id: int, job_id: str, urls: list[str],
               proxies: list[str], options: dict) -> dict:
    from utils.redis_pool import get_redis
    from utils.web_scraper import WebScraper, tgstat_channels_extractor

    redis = get_redis()

    # Выбор extractor'а по options.extractor
    extractor_name = (options.get("extractor") or "universal").lower()
    if extractor_name == "tgstat":
        page_extractor = tgstat_channels_extractor
        logger.info("[web_scraper] extractor=tgstat (карточки каналов)")
    else:
        page_extractor = _universal_extractor
        logger.info("[web_scraper] extractor=universal (title/meta/h1/body)")
    prog_k = _progress_key(user_id, job_id)
    cancel_k = _cancel_key(user_id, job_id)
    jsonl = _jsonl_path(user_id, job_id)
    jsonl.parent.mkdir(parents=True, exist_ok=True)

    # ── Progress callback: пишем snapshot в Redis ─────────────────────
    def _publish_progress(status: str, extra: dict | None = None):
        payload = {
            "status": status,
            "job_id": job_id,
            "urls_total": len(urls),
            "ts": int(time.time()),
        }
        if extra:
            payload.update(extra)
        try:
            redis.setex(prog_k, 86400, json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"[web_scraper] redis setex failed: {e}")

    async def progress_cb(snapshot: dict) -> None:
        _publish_progress("running", snapshot)

    # ── Cancel check: читаем флаг из Redis (синхронно) ────────────────
    def cancel_check() -> bool:
        try:
            return bool(redis.get(cancel_k))
        except Exception:
            return False

    # Очищаем cancel-флаг на старте (вдруг повисел с прошлого job'а)
    try:
        redis.delete(cancel_k)
    except Exception:
        pass

    _publish_progress("starting", {"phase": "launching_camoufox"})

    # ── Параметры WebScraper из options ──────────────────────────────
    camoufox_kwargs = options.get("camoufox") or {}
    # Безопасные дефолты для headless + стабильный fingerprint
    camoufox_kwargs.setdefault("humanize", True)
    camoufox_kwargs.setdefault("headless", True)

    try:
        scraper = WebScraper(
            proxies=proxies,
            checkpoint_path=jsonl,
            extractor=page_extractor,
            max_workers=int(options.get("max_workers", 3)),
            max_retries=int(options.get("max_retries", 3)),
            page_timeout_sec=float(options.get("page_timeout_sec", 60.0)),
            cooldown_min_sec=int(options.get("cooldown_min_sec", 900)),
            cooldown_max_sec=int(options.get("cooldown_max_sec", 1200)),
            node_rotation_min_sec=float(options.get("node_rotation_min_sec", 15.0)),
            node_rotation_max_sec=float(options.get("node_rotation_max_sec", 35.0)),
            progress_callback=progress_cb,
            cancel_check=cancel_check,
            camoufox_kwargs=camoufox_kwargs,
            page_locale=options.get("page_locale", "en-US"),
        )
    except Exception as e:
        logger.exception("[web_scraper] init failed")
        _publish_progress("error", {"error": f"init: {type(e).__name__}: {e}"})
        return {"ok": False, "error": str(e)}

    try:
        async with scraper:
            await scraper.run(urls)
    except asyncio.CancelledError:
        _publish_progress("cancelled", {
            **scraper.stats.snapshot(),
            "rotator": scraper.rotator.stats(),
        })
        return {"ok": False, "cancelled": True}
    except Exception as e:
        logger.exception("[web_scraper] run failed")
        _publish_progress("error", {
            "error": f"{type(e).__name__}: {e}",
            **scraper.stats.snapshot(),
        })
        return {"ok": False, "error": str(e)}

    # Финальный snapshot — done или cancelled, в зависимости от флага
    final_status = "cancelled" if cancel_check() else "done"
    _publish_progress(final_status, {
        **scraper.stats.snapshot(),
        "rotator": scraper.rotator.stats(),
        "jsonl_path": str(jsonl),
    })

    return {
        "ok": True,
        "status": final_status,
        "stats": scraper.stats.snapshot(),
        "jsonl_path": str(jsonl),
    }
