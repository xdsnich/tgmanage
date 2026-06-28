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
    """sync→async bridge. Celery threads + asyncio = свой loop на task.

    ВАЖНО для Windows: используем ProactorEventLoop, а не SelectorEventLoop.
    Camoufox/Playwright запускает Firefox-binary через subprocess —
    SelectorEventLoop на Windows кидает NotImplementedError в
    _make_subprocess_transport. По умолчанию asyncio.new_event_loop() в
    треде Celery даёт SelectorEventLoop → весь скрейпер падает с
    "NotImplementedError" глубоко внутри Playwright transport.

    asyncio.run() работает потому что на Windows он сам ставит правильный
    policy, но мы здесь не можем его юзать — у нас один таск = один новый
    loop в треде, иначе run() конфликтует с другими тасками.
    """
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        except Exception:
            pass


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


def _filter_playwright_compatible(proxies: list[str]) -> tuple[list[str], list[str]]:
    """
    Возвращает (compatible, dropped) с описаниями.
    Playwright/Camoufox НЕ поддерживает socks5+auth: при попытке создать
    context с таким прокси падает с "Browser does not support socks5 proxy
    authentication". Чтобы не занимать слоты пула и не плодить cooldown'ы,
    отфильтровываем их заранее.

    Совместимые:
      - http(s)://[user:pass@]host:port — всегда OK
      - socks5://host:port (без auth) — OK
    Несовместимые:
      - socks5://user:pass@host:port — DROPPED
    """
    from urllib.parse import urlparse
    ok, dropped = [], []
    for u in proxies:
        u = (u or "").strip()
        if not u:
            continue
        try:
            p = urlparse(u)
            scheme = (p.scheme or "").lower()
            has_auth = bool(p.username)
            if scheme in ("socks5", "socks5h", "socks4") and has_auth:
                dropped.append(u)
                continue
            ok.append(u)
        except Exception:
            dropped.append(u)
    return ok, dropped


async def _run(user_id: int, job_id: str, urls: list[str],
               proxies: list[str], options: dict) -> dict:
    from utils.redis_pool import get_redis
    from utils.web_scraper import WebScraper, tgstat_channels_extractor
    from utils.web_scraper.tgstat_channel_detail import tgstat_channel_detail_extractor

    redis = get_redis()

    # ── Фильтр прокси (socks5+auth не поддерживается Playwright) ──
    proxies, dropped_proxies = _filter_playwright_compatible(proxies)
    if dropped_proxies:
        logger.warning(
            f"[web_scraper] отброшено {len(dropped_proxies)} прокси "
            f"(socks5+auth не поддерживается Playwright)"
        )

    # Выбор extractor'а по options.extractor
    extractor_name = (options.get("extractor") or "universal").lower()
    if extractor_name == "tgstat":
        page_extractor = tgstat_channels_extractor
        logger.info("[web_scraper] extractor=tgstat (рейтинг → карточки)")
    elif extractor_name == "tgstat_channel_detail":
        page_extractor = tgstat_channel_detail_extractor
        logger.info("[web_scraper] extractor=tgstat_channel_detail (linked chat)")
    else:
        page_extractor = _universal_extractor
        logger.info("[web_scraper] extractor=universal (title/meta/h1/body)")

    # Будут ли использоваться 2 stage'a (для tgstat — да по умолчанию)
    pipeline = (options.get("pipeline") or "").lower()
    do_stage2_channel_details = (extractor_name == "tgstat" and pipeline != "stage1_only")

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

    # ═══════════════════════════════════════════════════════════════
    # STAGE 2: для каждого канала с rating-страниц открываем
    # /channel/@xxx и проверяем наличие linked discussion chat.
    # Только каналы с linked_chat сохраняем в БД.
    # ═══════════════════════════════════════════════════════════════
    stage2_summary = {"channels_total": 0, "with_comments": 0, "without_comments": 0, "saved": 0}
    if do_stage2_channel_details and not cancel_check():
        _publish_progress("running", {
            **scraper.stats.snapshot(),
            "phase": "stage2_channel_details",
        })

        # Собираем уникальные @username каналов из Stage 1 JSONL
        candidate_usernames = []
        seen = set()
        try:
            with jsonl.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if not rec.get("success"):
                        continue
                    data = rec.get("data") or {}
                    for ch in (data.get("channels") or []):
                        u = (ch.get("username") or "").lstrip("@")
                        if not u or ch.get("kind") != "channel":
                            continue
                        low = u.lower()
                        if low in seen:
                            continue
                        seen.add(low)
                        candidate_usernames.append(u)
        except Exception as e:
            logger.warning(f"[web_scraper] не смог прочитать Stage 1 JSONL: {e}")

        logger.info(f"[web_scraper] Stage 2: {len(candidate_usernames)} каналов на проверку")

        # Лимит из options (для теста / экономии прокси). 0 = без лимита.
        stage2_limit = int(options.get("stage2_limit", 0) or 0)
        if stage2_limit > 0 and len(candidate_usernames) > stage2_limit:
            candidate_usernames = candidate_usernames[:stage2_limit]
            logger.info(f"[web_scraper] Stage 2: ограничено до {stage2_limit}")

        detail_urls = [f"https://tgstat.com/channel/@{u}" for u in candidate_usernames]
        jsonl_stage2 = jsonl.parent / f"{job_id}.stage2.jsonl"

        try:
            scraper2 = WebScraper(
                proxies=proxies,
                checkpoint_path=jsonl_stage2,
                extractor=tgstat_channel_detail_extractor,
                max_workers=int(options.get("max_workers", 3)),
                max_retries=2,
                page_timeout_sec=float(options.get("page_timeout_sec", 45.0)),
                # Stage 2 — короче cooldown, потому что каждый прокси
                # уже использовался на Stage 1, нужен меньший лаг между
                # запросами а не "trust score recovery"
                cooldown_min_sec=int(options.get("stage2_cooldown_min_sec", 120)),
                cooldown_max_sec=int(options.get("stage2_cooldown_max_sec", 180)),
                node_rotation_min_sec=5.0,
                node_rotation_max_sec=15.0,
                progress_callback=progress_cb,
                cancel_check=cancel_check,
                camoufox_kwargs=camoufox_kwargs,
                page_locale=options.get("page_locale", "en-US"),
            )
            async with scraper2:
                await scraper2.run(detail_urls)
        except Exception as e:
            logger.exception("[web_scraper] Stage 2 failed")
            _publish_progress("error", {"error": f"stage2: {type(e).__name__}: {e}"})
            # Stage 1 уже сделал работу — не валим всё, идём дальше
            # к сохранению того что есть.

        # Парсим Stage 2 JSONL — собираем каналы с linked chat
        saved_channels = []
        try:
            with jsonl_stage2.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if not rec.get("success"):
                        continue
                    data = rec.get("data") or {}
                    stage2_summary["channels_total"] += 1
                    if data.get("has_open_comments"):
                        stage2_summary["with_comments"] += 1
                        saved_channels.append({
                            "username": (data.get("channel_username") or "").lstrip("@"),
                            "title": data.get("title") or "",
                            "subscribers": int(data.get("subscribers") or 0),
                            "linked_chat": (data.get("linked_chat_username") or "").lstrip("@"),
                            "category": data.get("category"),
                            "verified": bool(data.get("verified")),
                        })
                    else:
                        stage2_summary["without_comments"] += 1
        except Exception as e:
            logger.warning(f"[web_scraper] Stage 2 JSONL read error: {e}")

        # ── Сохранение в БД ───────────────────────────────────
        # UPSERT по (user_id, username): если канал уже был — обновим
        # subscribers/title, has_comments=True. Если не было — создадим.
        #
        # ВАЖНО: используем utils.db_pool.async_session (NullPool), а
        # НЕ database.AsyncSessionLocal (pool_size=10). Иначе долгая
        # DB-операция в Celery-таске занимает соединения из общего пула
        # uvicorn'а → все API возвращают 500. Bug fix.
        if saved_channels:
            try:
                from sqlalchemy import select
                from utils.db_pool import async_session as TaskSession
                from models.parsed_channel import ParsedChannel
                from datetime import datetime as _dt

                async with TaskSession() as db:
                    for ch in saved_channels:
                        u = ch["username"]
                        if not u:
                            continue
                        existing = (await db.execute(
                            select(ParsedChannel).where(
                                ParsedChannel.user_id == user_id,
                                ParsedChannel.username == u,
                            )
                        )).scalar_one_or_none()
                        if existing:
                            existing.title = ch["title"] or existing.title
                            existing.subscribers = ch["subscribers"] or existing.subscribers
                            existing.has_comments = True
                            existing.last_verification = _dt.utcnow()
                            existing.search_query = f"tgstat_web:{job_id}"
                        else:
                            db.add(ParsedChannel(
                                user_id=user_id,
                                username=u,
                                title=ch["title"],
                                subscribers=ch["subscribers"],
                                has_comments=True,
                                language=None,
                                search_query=f"tgstat_web:{job_id}",
                                folder="",
                                last_verification=_dt.utcnow(),
                                added_at=_dt.utcnow(),
                            ))
                        stage2_summary["saved"] += 1
                    await db.commit()
            except Exception as e:
                logger.exception("[web_scraper] DB save failed")
                stage2_summary["db_error"] = str(e)[:200]

        logger.info(f"[web_scraper] Stage 2 done: {stage2_summary}")

    # Финальный snapshot — done или cancelled, в зависимости от флага
    final_status = "cancelled" if cancel_check() else "done"
    _publish_progress(final_status, {
        **scraper.stats.snapshot(),
        "rotator": scraper.rotator.stats(),
        "jsonl_path": str(jsonl),
        "stage2": stage2_summary,
        "dropped_proxies": len(dropped_proxies),
    })

    return {
        "ok": True,
        "status": final_status,
        "stats": scraper.stats.snapshot(),
        "stage2": stage2_summary,
        "jsonl_path": str(jsonl),
    }
