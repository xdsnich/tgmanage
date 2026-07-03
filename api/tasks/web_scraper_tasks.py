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


def _log_dir(user_id: int) -> Path:
    """Корень для per-job логов юзера. Отдельный файл на каждый job'ик —
    юзер может скачать его с UI и прислать разработчику без риска утечки
    других job'ов и без ковыряния в общем logs/celery.log."""
    p = Path(API_DIR) / ".." / "logs" / "web_scraper" / str(user_id)
    return p.resolve()


def _log_path(user_id: int, job_id: str) -> Path:
    return _log_dir(user_id) / f"{job_id}.log"


def _setup_job_logger(user_id: int, job_id: str) -> tuple[logging.Handler | None, Path]:
    """
    Attach отдельный FileHandler на релевантные логгеры на время job'а.
    Пишет DEBUG-уровень (максимум деталей) в logs/web_scraper/{user_id}/{job_id}.log —
    независимо от глобального LOG_LEVEL. Только web_scraper и cloak_pool
    события, ничего лишнего.

    Возвращает (handler, path). Handler можно передать в _teardown_job_logger
    в finally, чтобы он корректно detach'нулся и файл закрылся.
    """
    path = _log_path(user_id, job_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        h = logging.FileHandler(str(path), encoding="utf-8")
        h.setLevel(logging.DEBUG)
        h.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
            datefmt="%H:%M:%S",
        ))
        # Прикрепляем к логгерам которые пишут скрейпер-события.
        # Root не трогаем — иначе будут посторонние сообщения.
        for name in (
            "utils.web_scraper.scraper",
            "utils.web_scraper.cloak_pool",
            "utils.web_scraper.context_pool",
            "utils.web_scraper.node_rotator",
            "tasks.web_scraper_tasks",
        ):
            lg = logging.getLogger(name)
            # DEBUG-messages от этих модулей должны доходить до нашего handler'а
            if lg.level > logging.DEBUG or lg.level == logging.NOTSET:
                lg.setLevel(logging.DEBUG)
            lg.addHandler(h)
        return h, path
    except Exception as e:
        logger.warning(f"[web_scraper] не удалось создать per-job log: {e}")
        return None, path


def _teardown_job_logger(h: logging.Handler | None) -> None:
    if h is None:
        return
    for name in (
        "utils.web_scraper.scraper",
        "utils.web_scraper.cloak_pool",
        "utils.web_scraper.context_pool",
        "utils.web_scraper.node_rotator",
        "tasks.web_scraper_tasks",
    ):
        try:
            logging.getLogger(name).removeHandler(h)
        except Exception:
            pass
    try:
        h.flush()
        h.close()
    except Exception:
        pass


def _dump_env_diagnostics(
    job_id: str, user_id: int, engine: str,
    proxies_count: int, dropped_count: int, options: dict, urls_count: int,
) -> None:
    """Дампит окружение в per-job log при старте — самое важное для
    отладки: версии Python/cloakbrowser/camoufox, путь к бинарнику,
    выбранный движок, дефолты. Дальше по логу можно понять что сломалось."""
    lines = [
        "═" * 60,
        f"WEB SCRAPER JOB START  user={user_id}  job={job_id}",
        "═" * 60,
        f"engine        : {engine}",
        f"urls          : {urls_count}",
        f"proxies       : {proxies_count} (dropped {dropped_count})",
        f"python        : {sys.version.split()[0]}  {sys.platform}",
    ]
    try:
        import platform as _plat
        lines.append(f"os            : {_plat.platform()}")
    except Exception:
        pass
    def _pkg_version(name: str) -> str:
        # У camoufox __version__ — подмодуль, а не строка; у playwright
        # вообще нет .__version__. Единый способ — importlib.metadata.
        try:
            from importlib.metadata import version, PackageNotFoundError
            try:
                return version(name)
            except PackageNotFoundError:
                return "не установлен"
        except Exception as e:
            return f"?({type(e).__name__})"

    lines.append(f"cloakbrowser  : {_pkg_version('cloakbrowser')}")
    try:
        from cloakbrowser import CHROMIUM_VERSION as _cv, ensure_binary as _eb
        bin_path = _eb() if engine == "cloak" else None
        lines.append(f"chromium_ver  : {_cv}")
        if bin_path:
            sz = ""
            try:
                sz = f"  ({os.path.getsize(bin_path) / (1024*1024):.1f} MB)"
            except Exception:
                pass
            lines.append(f"chromium_bin  : {bin_path}{sz}")
    except ImportError:
        pass
    except Exception as e:
        lines.append(f"chromium_bin  : (ошибка получения: {type(e).__name__}: {e})")
    lines.append(f"camoufox      : {_pkg_version('camoufox')}")
    lines.append(f"playwright    : {_pkg_version('playwright')}")
    # Options — что реально запросили с фронта
    lines.append("options       :")
    for k in sorted(options.keys()):
        v = options[k]
        # Секьюр: если вдруг попадёт что-то с "password/token/key" — маскируем
        vs = str(v)
        if any(t in k.lower() for t in ("pass", "token", "secret", "key")) and len(vs) > 6:
            vs = vs[:3] + "***" + vs[-2:]
        lines.append(f"  {k} = {vs}")
    lines.append("═" * 60)
    for line in lines:
        logger.info(line)


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


def _filter_playwright_compatible(proxies: list[str], engine: str = "camoufox") -> tuple[list[str], list[str]]:
    """
    Возвращает (compatible, dropped).

    Camoufox (Firefox через Playwright new_context(proxy=...)) НЕ поддерживает
    socks5+auth: при попытке создать context падает с "Browser does not support
    socks5 proxy authentication". Отбрасываем такие заранее.

    CloakBrowser (Chromium через launch_persistent_context_async(proxy=...))
    внутри передаёт прокси через --proxy-server + Chrome обрабатывает креды
    сам, поэтому socks5+auth работает нативно. Ничего не фильтруем.

    Совместимые для camoufox:
      - http(s)://[user:pass@]host:port
      - socks5://host:port (без auth)
    """
    from urllib.parse import urlparse
    if engine == "cloak":
        return [u.strip() for u in proxies if u and u.strip()], []

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

    # ── Per-job log file: logs/web_scraper/{user_id}/{job_id}.log ─────
    # Всё что пишется скрейпером во время этого job'а копится в отдельный
    # файл. Юзер может скачать его с UI и прислать разработчику.
    _job_log_handler, _job_log_path = _setup_job_logger(user_id, job_id)
    _run_ok = False
    try:
        # ── Выбор движка: camoufox (default) / cloak (persistent profiles) ─
        engine = (options.get("engine") or "camoufox").lower()
        if engine not in ("camoufox", "cloak"):
            logger.warning(f"[web_scraper] неизвестный engine={engine}, fallback camoufox")
            engine = "camoufox"

        # ── Фильтр прокси (socks5+auth несовместим с Camoufox) ─────
        proxies, dropped_proxies = _filter_playwright_compatible(proxies, engine=engine)
        if dropped_proxies:
            logger.warning(
                f"[web_scraper] отброшено {len(dropped_proxies)} прокси "
                f"(socks5+auth не поддерживается Playwright/Camoufox)"
            )

        # ── Env diagnostics — ПЕРВОЕ что попадает в лог ────────────
        _dump_env_diagnostics(
            job_id=job_id, user_id=user_id, engine=engine,
            proxies_count=len(proxies), dropped_count=len(dropped_proxies),
            options=options, urls_count=len(urls),
        )
        _result = await __run_body(
            user_id, job_id, urls, proxies, options, engine,
            dropped_proxies, redis,
        )
        _run_ok = True
        return _result
    finally:
        logger.info(
            f"[web_scraper] job {job_id} finished (ok={_run_ok}), "
            f"log saved to {_job_log_path}"
        )
        _teardown_job_logger(_job_log_handler)


async def __run_body(
    user_id: int, job_id: str, urls: list[str],
    proxies: list[str], options: dict, engine: str,
    dropped_proxies: list, redis,
) -> dict:
    """Тело _run без wrap'а с логгером — вынесено чтобы не увеличивать
    отступ на весь файл. Логика идентична предыдущей версии, просто
    engine и dropped_proxies уже вычислены снаружи."""
    from utils.web_scraper import WebScraper, tgstat_channels_extractor
    from utils.web_scraper.tgstat_channel_detail import tgstat_channel_detail_extractor

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

    _publish_progress("starting", {"phase": f"launching_{engine}"})

    # ── Параметры WebScraper из options ──────────────────────────────
    camoufox_kwargs = options.get("camoufox") or {}
    # Безопасные дефолты для headless + стабильный fingerprint
    camoufox_kwargs.setdefault("humanize", True)
    camoufox_kwargs.setdefault("headless", True)

    # Параметры CloakBrowser (при engine=cloak). geoip и humanize
    # включены в самом пуле по дефолту — можно переопределить.
    cloak_kwargs = options.get("cloak") or {}
    cloak_kwargs.setdefault("headless", True)

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
            engine=engine,
            cloak_kwargs=cloak_kwargs,
            # Рандомизация "человеческого" плана
            worker_start_stagger_max_sec=float(options.get("worker_start_stagger_max_sec", 45.0)),
            break_probability=float(options.get("break_probability", 0.20)),
            break_min_sec=float(options.get("break_min_sec", 60.0)),
            break_max_sec=float(options.get("break_max_sec", 300.0)),
            post_read_tail_min_sec=float(options.get("post_read_tail_min_sec", 0.0)),
            post_read_tail_max_sec=float(options.get("post_read_tail_max_sec", 15.0)),
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
    stage2_summary = {
        "channels_total": 0, "with_comments": 0, "without_comments": 0,
        "saved": 0, "saved_new": 0, "saved_updated": 0,
    }
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

        logger.info(f"[web_scraper] Stage 2: {len(candidate_usernames)} кандидатов из Stage 1")

        # ── Фильтр «уже проверенные» ─────────────────────────
        # Юзер: спарсили 300 → потом захотел ещё 400. Без этого
        # фильтра Stage 2 повторно проверит те же топовые каналы
        # (они всегда в топе рейтинга). Пропускаем всё что уже
        # проверялось за последние skip_recently_checked_days.
        skip_days = int(options.get("skip_recently_checked_days", 30) or 0)
        already_checked_low: set[str] = set()
        if skip_days > 0 and candidate_usernames:
            try:
                from sqlalchemy import select
                from datetime import datetime as _dt, timedelta as _td
                from utils.db_pool import async_session as TaskSession
                from models.tgstat_checked_channel import TgstatCheckedChannel

                since = _dt.utcnow() - _td(days=skip_days)
                async with TaskSession() as own_db:
                    rows = (await own_db.execute(
                        select(TgstatCheckedChannel.username).where(
                            TgstatCheckedChannel.user_id == user_id,
                            TgstatCheckedChannel.checked_at >= since,
                        )
                    )).all()
                    already_checked_low = {r[0].lower() for r in rows}
            except Exception as e:
                logger.warning(f"[web_scraper] skip-recently-checked filter failed: {e}")

        if already_checked_low:
            before = len(candidate_usernames)
            candidate_usernames = [
                u for u in candidate_usernames if u.lower() not in already_checked_low
            ]
            skipped = before - len(candidate_usernames)
            logger.info(
                f"[web_scraper] Stage 2: пропущено {skipped} уже проверенных "
                f"за последние {skip_days} дней, осталось {len(candidate_usernames)}"
            )

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
                # На детальных страницах TGStat нет смысла делать 3+ retry —
                # если канал реально за CF challenge и таймаутит, 2 retry
                # уже доказали что страница не отдаст контент быстро.
                max_retries=int(options.get("stage2_max_retries", 1)),
                # Поднимаем page_timeout — крупные каналы за Cloudflare
                # отдают 60-90 сек.
                page_timeout_sec=float(options.get("stage2_page_timeout_sec", 60.0)),
                cooldown_min_sec=int(options.get("stage2_cooldown_min_sec", 60)),
                cooldown_max_sec=int(options.get("stage2_cooldown_max_sec", 120)),
                node_rotation_min_sec=5.0,
                node_rotation_max_sec=15.0,
                progress_callback=progress_cb,
                cancel_check=cancel_check,
                camoufox_kwargs=camoufox_kwargs,
                page_locale=options.get("page_locale", "en-US"),
                engine=engine,
                cloak_kwargs=cloak_kwargs,
                # На Stage 2 ещё сильнее рандомизируем — детальных
                # страниц много, если хотим "живого юзера" — надо
                # чтоб не строчил одинаковыми задержками.
                worker_start_stagger_max_sec=float(options.get("stage2_worker_start_stagger_max_sec", 30.0)),
                break_probability=float(options.get("stage2_break_probability", 0.25)),
                break_min_sec=float(options.get("stage2_break_min_sec", 45.0)),
                break_max_sec=float(options.get("stage2_break_max_sec", 240.0)),
                post_read_tail_min_sec=float(options.get("stage2_post_read_tail_min_sec", 2.0)),
                post_read_tail_max_sec=float(options.get("stage2_post_read_tail_max_sec", 20.0)),
            )
            async with scraper2:
                await scraper2.run(detail_urls)
        except Exception as e:
            logger.exception("[web_scraper] Stage 2 failed")
            _publish_progress("error", {"error": f"stage2: {type(e).__name__}: {e}"})
            # Stage 1 уже сделал работу — не валим всё, идём дальше
            # к сохранению того что есть.

        # Парсим Stage 2 JSONL — собираем каналы с linked chat
        # Также собираем ВСЕ проверенные (с комментами и без) для
        # записи в TgstatCheckedChannel — чтобы следующий запуск не
        # проверял их повторно.
        saved_channels = []
        checked_records = []
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

                    u = (data.get("channel_username") or "").lstrip("@")
                    if not u:
                        continue
                    has_c = bool(data.get("has_open_comments"))
                    checked_records.append({
                        "username": u,
                        "has_comments": has_c,
                        "linked_chat": (data.get("linked_chat_username") or "").lstrip("@") or None,
                        "subscribers": int(data.get("subscribers") or 0),
                        "title": (data.get("title") or "")[:250] or None,
                    })
                    if has_c:
                        stage2_summary["with_comments"] += 1
                        saved_channels.append({
                            "username": u,
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

        # ── Запись в TgstatCheckedChannel — ВСЕ проверенные ─
        if checked_records:
            try:
                from sqlalchemy import select
                from datetime import datetime as _dt2
                from utils.db_pool import async_session as TaskSession
                from models.tgstat_checked_channel import TgstatCheckedChannel

                async with TaskSession() as own_db:
                    for c in checked_records:
                        existing = (await own_db.execute(
                            select(TgstatCheckedChannel).where(
                                TgstatCheckedChannel.user_id == user_id,
                                TgstatCheckedChannel.username == c["username"],
                            )
                        )).scalar_one_or_none()
                        if existing:
                            existing.has_comments = c["has_comments"]
                            existing.linked_chat = c["linked_chat"] or existing.linked_chat
                            existing.subscribers = c["subscribers"] or existing.subscribers
                            existing.title = c["title"] or existing.title
                            existing.last_job_id = job_id
                            existing.checked_at = _dt2.utcnow()
                        else:
                            own_db.add(TgstatCheckedChannel(
                                user_id=user_id,
                                username=c["username"],
                                has_comments=c["has_comments"],
                                linked_chat=c["linked_chat"],
                                subscribers=c["subscribers"],
                                title=c["title"],
                                last_job_id=job_id,
                                checked_at=_dt2.utcnow(),
                            ))
                    await own_db.commit()
                stage2_summary["checked_total_saved"] = len(checked_records)
            except Exception as e:
                logger.warning(f"[web_scraper] TgstatCheckedChannel save failed: {e}")

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
                            stage2_summary["saved_updated"] = stage2_summary.get("saved_updated", 0) + 1
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
                            stage2_summary["saved_new"] = stage2_summary.get("saved_new", 0) + 1
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
