"""
scraper.py — Главный orchestrator WebScraper.

Связывает воедино:
  asyncio.Queue → 3-4 worker pool → NodeRotator → BrowserContextPool →
  UX-эмуляция → user extractor → JSONLCheckpoint

Лимиты по умолчанию подобраны под пул 34 IPv4 и WAF-нагрузку:
  - max_workers=3 — даже при 34 узлах больше 3-4 параллельных запросов
    к одному target-домену = риск global rate limit на стороне target
  - 3 ретрая на URL — больше смысла нет, после 3 фейлов URL мёртв
  - page_timeout=60s — Camoufox с JS-челленджами загружается долго

Чёткое разделение ответственности:
  - WebScraper НЕ знает что именно вытаскивать со страницы. Это делает
    user-provided extractor: async def(page) -> dict
  - WebScraper отвечает за: очередь, ретраи, ротацию узлов, UX,
    persistence, прогресс и отмену.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional

from .node_rotator import NodeRotator, ProxyNode
from .context_pool import BrowserContextPool
from .cloak_pool import CloakProfilePool
from .checkpoint import JSONLCheckpoint
from .retry import backoff_delay
from .ux_emulator import simulate_pre_read_session, micro_pause

logger = logging.getLogger(__name__)


# Тип extractor: получает Playwright Page, возвращает dict с данными.
# Может бросать исключения — WebScraper их поймает и засчитает как fail.
ExtractorFn = Callable[[Any], Awaitable[dict]]

# Тип progress callback: получает snapshot статуса, ничего не возвращает.
ProgressFn = Callable[[dict], Awaitable[None]]

# Тип cancel check: синхронная функция, возвращает True если надо остановиться.
CancelFn = Callable[[], bool]


@dataclass
class ScrapeResult:
    """Результат обработки одного URL — внутренний формат, не для JSONL."""

    url: str
    success: bool
    data: Optional[dict] = None
    error: Optional[str] = None
    proxy_used: Optional[str] = None
    attempts: int = 1
    duration_sec: float = 0.0
    http_status: Optional[int] = None


@dataclass
class ScraperStats:
    """Счётчики для прогресс-репортинга."""

    total: int = 0          # всего URL в job (включая уже сделанные)
    skipped: int = 0        # уже было в checkpoint
    pending: int = 0        # к обработке
    done: int = 0           # завершено в этом запуске (ok + failed)
    ok: int = 0
    failed: int = 0
    started_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict:
        return {
            "total": self.total,
            "skipped": self.skipped,
            "pending": self.pending,
            "done": self.done,
            "ok": self.ok,
            "failed": self.failed,
            "elapsed_sec": round(time.time() - self.started_at, 1),
        }


class WebScraper:
    """
    Главный класс. Async context manager.

    Минимальный пример использования:

        async def my_extractor(page) -> dict:
            return {"title": await page.title()}

        scraper = WebScraper(
            proxies=[...34 urls...],
            checkpoint_path="data/results.jsonl",
            extractor=my_extractor,
            max_workers=3,
        )
        async with scraper:
            await scraper.run([url1, url2, ...])
    """

    def __init__(
        self,
        proxies: list[str],
        checkpoint_path: str | Path,
        extractor: ExtractorFn,
        max_workers: int = 3,
        max_retries: int = 3,
        page_timeout_sec: float = 60.0,
        cooldown_min_sec: int = 900,
        cooldown_max_sec: int = 1200,
        node_rotation_min_sec: float = 15.0,
        node_rotation_max_sec: float = 35.0,
        progress_callback: Optional[ProgressFn] = None,
        cancel_check: Optional[CancelFn] = None,
        camoufox_kwargs: Optional[dict] = None,
        page_locale: str = "en-US",
        # ── Выбор движка браузера ─────────────────────────────
        # camoufox — patched Firefox с fresh context на каждый URL (текущий дефолт).
        # cloak    — CloakBrowser с persistent-профилем на каждый прокси.
        #            Cookies и fingerprint сохраняются между запусками —
        #            WAF видит "вернувшегося юзера", не первый визит.
        engine: Literal["camoufox", "cloak"] = "camoufox",
        cloak_kwargs: Optional[dict] = None,
        profiles_dir: Optional[str | Path] = None,
        # ── Рандомизация "человеческого" поведения ────────────
        # Стартовый разброс воркеров — 2-3-4 воркер стартуют не сразу,
        # а с задержкой 0..N сек. Иначе WAF видит N синхронных запросов.
        worker_start_stagger_max_sec: float = 45.0,
        # После каждого URL воркер с вероятностью break_probability
        # уходит "на перерыв" на break_min..break_max сек. Так
        # эффективное число активных воркеров плавает во времени
        # (0-2-5-3-1-4-...), а не константа 3.
        break_probability: float = 0.20,
        break_min_sec: float = 60.0,
        break_max_sec: float = 300.0,
        # Дополнительный "хвост" на странице после чтения — рандом.
        # Реальный юзер полистал → задержался ещё немного → ушёл.
        post_read_tail_min_sec: float = 0.0,
        post_read_tail_max_sec: float = 15.0,
    ):
        if max_workers > 4:
            logger.warning(
                f"max_workers={max_workers} > 4 — повышенный риск WAF-блока. "
                f"Рекомендуется 3-4 для пула из ~34 узлов."
            )
        if not proxies:
            raise ValueError("proxies не может быть пустым списком")

        self.max_workers = max_workers
        self.max_retries = max_retries
        self.page_timeout_sec = page_timeout_sec
        self.extractor = extractor
        self.progress_callback = progress_callback
        self.cancel_check = cancel_check or (lambda: False)

        # Рандомизация "человеческого" поведения
        self.worker_start_stagger_max_sec = float(worker_start_stagger_max_sec)
        self.break_probability = max(0.0, min(1.0, float(break_probability)))
        self.break_min_sec = float(break_min_sec)
        self.break_max_sec = float(break_max_sec)
        self.post_read_tail_min_sec = float(post_read_tail_min_sec)
        self.post_read_tail_max_sec = float(post_read_tail_max_sec)

        self.rotator = NodeRotator(
            proxies=proxies,
            cooldown_min_sec=cooldown_min_sec,
            cooldown_max_sec=cooldown_max_sec,
        )
        self.engine = engine
        if engine == "cloak":
            # По умолчанию — data/scraper_profiles/ рядом с data/web_scraper/.
            # Один и тот же прокси во всех job'ах → один и тот же профиль:
            # cookies и fingerprint копятся между запусками скрейпера.
            if profiles_dir is None:
                profiles_dir = Path(__file__).resolve().parents[3] / "data" / "scraper_profiles"
            self.pool = CloakProfilePool(
                profiles_dir=profiles_dir,
                node_rotation_min_sec=node_rotation_min_sec,
                node_rotation_max_sec=node_rotation_max_sec,
                cloak_kwargs=cloak_kwargs or {},
                page_locale=page_locale,
            )
        else:
            self.pool = BrowserContextPool(
                node_rotation_min_sec=node_rotation_min_sec,
                node_rotation_max_sec=node_rotation_max_sec,
                camoufox_kwargs=camoufox_kwargs or {},
                page_locale=page_locale,
            )
        self.checkpoint = JSONLCheckpoint(checkpoint_path, key_field="url")
        self.stats = ScraperStats()
        # Текущие URL обрабатываемые воркерами — для UI "в работе сейчас"
        self._in_flight: dict[int, str] = {}
        self._in_flight_lock = asyncio.Lock()
        # Browser-crash recovery state
        self._browser_crash_count: int = 0
        self._browser_healthy: bool = True
        self._restart_lock = asyncio.Lock()
        # Circuit breaker: если N consecutive fails — форс-cancel
        # чтобы не пожечь весь пул прокси на бесполезных retry.
        self._consecutive_fails: int = 0
        self._max_consecutive_fails: int = 20
        self._force_stop: bool = False
        # Оборачиваем внешний cancel_check чтобы также реагировать на
        # circuit breaker
        _external_cancel = self.cancel_check
        self.cancel_check = lambda: self._force_stop or _external_cancel()

    # ── Context manager ──────────────────────────────────────────────

    async def __aenter__(self):
        await self.pool.start()
        return self

    async def __aexit__(self, *args):
        await self.pool.stop()

    async def _emergency_pool_stop(self) -> None:
        """
        Вызывается watchdog'ом при cancel. Закрывает pool чтобы все
        pending page.goto/new_context мгновенно упали, а не ждали
        60 сек до page_timeout. После этого _process_url увидит
        "browser closed" ошибку — но крашрекавери НЕ будет запускать
        рестарт, потому что self._cancel_pool_closed=True.
        """
        try:
            await self.pool.stop()
            logger.info("[scraper] emergency: pool closed, in-flight goto/new_context должны упасть")
        except Exception as e:
            logger.warning(f"[scraper] emergency pool stop error: {type(e).__name__}: {e}")

    # ── Public API ───────────────────────────────────────────────────

    async def run(self, urls: list[str]) -> None:
        """
        Запускает обработку списка URL. Блокирует пока не закончит или
        пока cancel_check() не вернёт True.
        """
        # Фильтруем уже обработанные через checkpoint
        pending = self.checkpoint.filter_pending(urls)
        self.stats.total = len(urls)
        self.stats.skipped = len(urls) - len(pending)
        self.stats.pending = len(pending)

        logger.info(
            f"[scraper] Запуск: всего {len(urls)} URL, "
            f"пропущено {self.stats.skipped} (уже в checkpoint), "
            f"к обработке {len(pending)}, воркеров {self.max_workers}"
        )

        if not pending:
            await self._report_progress(reason="nothing_to_do")
            return

        queue: asyncio.Queue[str] = asyncio.Queue()
        for u in pending:
            queue.put_nowait(u)

        workers = [
            asyncio.create_task(self._worker(i, queue), name=f"scraper-worker-{i}")
            for i in range(self.max_workers)
        ]

        # Watchdog: раз в 1 сек проверяет cancel_check. Если юзер нажал
        # "Стоп" — форс-стопим:
        #   1. отменяем task'и воркеров (asyncio.cancel)
        #   2. ЗАКРЫВАЕМ browser pool — все in-flight page.goto()
        #      мгновенно падают с "Connection closed while reading from
        #      the driver". Иначе Playwright ждёт page_timeout (60 сек)
        #      прежде чем реально освободиться на CancelledError, потому
        #      что transport читает из subprocess pipe в блокирующем
        #      режиме и CancelledError не пробивает этот read.
        # Помечаем что закрытие сделано ПО КАНСЕЛУ, а не крах — чтобы
        # _process_url не пытался восстановить браузер.
        self._cancel_pool_closed: bool = False
        async def _cancel_watchdog():
            while True:
                await asyncio.sleep(1.0)
                if self.cancel_check():
                    logger.info(
                        "[scraper] cancel_check=True — force cancel воркеров "
                        "+ закрываю browser pool чтобы разблокировать goto()"
                    )
                    for w in workers:
                        if not w.done():
                            w.cancel()
                    # Фоном закрываем pool, не блокируя watchdog
                    self._cancel_pool_closed = True
                    asyncio.create_task(
                        self._emergency_pool_stop(),
                        name="scraper-emergency-pool-stop",
                    )
                    return
                if all(w.done() for w in workers):
                    return
        watchdog = asyncio.create_task(_cancel_watchdog(), name="scraper-cancel-watchdog")

        try:
            await queue.join()
        except asyncio.CancelledError:
            logger.info("[scraper] run() cancelled")
            raise
        finally:
            # Сообщаем воркерам остановиться. queue.join() уже ждал
            # пока все task_done(), но воркеры остаются в while True,
            # поэтому отменяем их явно.
            for w in workers:
                w.cancel()
            watchdog.cancel()
            await asyncio.gather(*workers, watchdog, return_exceptions=True)
            await self._report_progress(reason="finished")

        logger.info(f"[scraper] Готово: {self.stats.snapshot()}")

    # ── Worker loop ──────────────────────────────────────────────────

    async def _worker(self, worker_id: int, queue: asyncio.Queue[str]) -> None:
        """
        Один воркер крутится в цикле, забирая URL из очереди.
        Завершается когда:
          - queue пустая и task_done() для всех вызван (cancel из run())
          - cancel_check() вернул True

        Рандомизация "человеческого" поведения:
          - Стартовый разброс: воркер #0 стартует сразу, #1,#2,... —
            с задержкой uniform(0, worker_start_stagger_max_sec).
            Иначе WAF видит N синхронных запросов.
          - Random breaks: после каждого URL с вероятностью
            break_probability уходим "на перерыв" 60-300 сек.
            → эффективное число активных воркеров плавает во времени
            (0-2-5-3-1-4-...), как будто живые люди заходят и уходят.
        """
        # 1. Стартовый разброс — только для воркеров с id > 0
        if worker_id > 0 and self.worker_start_stagger_max_sec > 0:
            delay = random.uniform(0, self.worker_start_stagger_max_sec)
            logger.info(
                f"[worker-{worker_id}] стартовая задержка {delay:.1f}с"
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if self.cancel_check():
                return

        logger.debug(f"[worker-{worker_id}] start")
        while True:
            if self.cancel_check():
                logger.info(f"[worker-{worker_id}] отмена")
                return
            try:
                url = await asyncio.wait_for(queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                # Очередь пустая — но run() может ещё положить (нет в нашем
                # случае, но архитектурно поддерживаем). Просто продолжаем.
                continue

            async with self._in_flight_lock:
                self._in_flight[worker_id] = url

            try:
                result = await self._process_url(worker_id, url)
                if result.success:
                    self.stats.ok += 1
                    self._consecutive_fails = 0
                else:
                    self.stats.failed += 1
                    self._consecutive_fails += 1
                    # Circuit breaker: 20 подряд failed → TGStat нас
                    # либо забанил, либо прокси-пул мёртв. Смысла жечь
                    # оставшиеся URL нет — форсим завершение.
                    if self._consecutive_fails >= self._max_consecutive_fails:
                        logger.error(
                            f"[worker-{worker_id}] 🛑 circuit breaker: "
                            f"{self._consecutive_fails} fails подряд — "
                            f"остальные URL пропускаем"
                        )
                        self._force_stop = True

                # Запись в JSONL — атомарно под локом checkpoint'а
                await self.checkpoint.save({
                    "url": url,
                    "success": result.success,
                    "data": result.data,
                    "error": result.error,
                    "proxy": result.proxy_used,
                    "attempts": result.attempts,
                    "http_status": result.http_status,
                    "duration_sec": round(result.duration_sec, 2),
                    "ts": int(time.time()),
                })
                self.stats.done += 1
                await self._report_progress(current=url)

            except asyncio.CancelledError:
                # При cancel — task_done() надо вызвать чтобы queue.join()
                # не висел, и выходим.
                queue.task_done()
                async with self._in_flight_lock:
                    self._in_flight.pop(worker_id, None)
                logger.info(f"[worker-{worker_id}] cancelled на {url}")
                raise
            except Exception as e:
                # Что-то совсем неожиданное — логируем и продолжаем,
                # не валим весь scraper.
                logger.exception(f"[worker-{worker_id}] catastrophic error on {url}: {e}")
                self.stats.failed += 1
                self.stats.done += 1
                try:
                    await self.checkpoint.save({
                        "url": url,
                        "success": False,
                        "error": f"catastrophic: {type(e).__name__}: {e}",
                        "ts": int(time.time()),
                    })
                except Exception:
                    pass
                await self._report_progress(current=url)
            finally:
                async with self._in_flight_lock:
                    self._in_flight.pop(worker_id, None)
                queue.task_done()

            # Рандомный перерыв — эмулирует "пользователь ушёл на кофе".
            # Каждый воркер сам решает — вероятность и длительность
            # независимы, поэтому эффективное число активных воркеров
            # плавает по времени (0-2-5-3-1-4-...) а не константа.
            if self.break_probability > 0 and random.random() < self.break_probability:
                dur = random.uniform(self.break_min_sec, self.break_max_sec)
                logger.info(
                    f"[worker-{worker_id}] 🛑 перерыв {dur:.0f}с "
                    f"(эмуляция человека)"
                )
                # Спим кусочками чтоб cancel_check срабатывал быстро
                slept = 0.0
                while slept < dur:
                    if self.cancel_check():
                        return
                    step = min(2.0, dur - slept)
                    try:
                        await asyncio.sleep(step)
                    except asyncio.CancelledError:
                        return
                    slept += step

    # ── Per-URL processing with retries ──────────────────────────────

    async def _process_url(self, worker_id: int, url: str) -> ScrapeResult:
        """
        Обрабатывает один URL с ретраями. Каждая попытка берёт свежий
        узел из rotator. 403/429 → cooldown узла без задержки retry,
        5xx/timeout → exponential backoff.
        """
        start = time.time()
        last_error: Optional[str] = None
        last_status: Optional[int] = None
        attempts_made = 0

        for attempt in range(1, self.max_retries + 1):
            attempts_made = attempt
            if self.cancel_check():
                last_error = "cancelled"
                break

            try:
                node = await self.rotator.acquire(
                    wait_timeout=600.0,
                    cancel_check=self.cancel_check,
                )
            except asyncio.TimeoutError as _e:
                if self.cancel_check():
                    last_error = "cancelled"
                else:
                    last_error = "no_available_node"
                    logger.error(
                        f"[worker-{worker_id}] {url}: все узлы заняты/в cooldown >10мин"
                    )
                break

            proxy_label = NodeRotator._mask(node.url)
            logger.info(
                f"[worker-{worker_id}] {url} attempt={attempt} via {proxy_label}"
            )

            try:
                async with self.pool.context_for(node) as ctx:
                    page = await ctx.new_page()
                    try:
                        # Микро-пауза перед goto — Camoufox иногда нужен
                        # момент после открытия page чтобы инициализировать
                        # JS-environment под fingerprint.
                        await micro_pause(0.5, 2.0)

                        response = await page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=int(self.page_timeout_sec * 1000),
                        )
                        last_status = response.status if response else 0

                        # 403/429 → узел "сгорел" на этот URL/домен
                        if last_status in (403, 429):
                            await self.rotator.mark_failed(
                                node, f"HTTP {last_status}"
                            )
                            last_error = f"HTTP {last_status}"
                            try:
                                await page.close()
                            except Exception:
                                pass
                            # Не делаем backoff — сразу retry с другим узлом
                            # (контекст уже закрыт через async with)
                            continue

                        # 5xx — может быть временным, делаем retry с backoff
                        if last_status >= 500:
                            await self.rotator.mark_success(node)
                            last_error = f"HTTP {last_status}"
                            try:
                                await page.close()
                            except Exception:
                                pass
                            if attempt < self.max_retries:
                                await backoff_delay(attempt)
                            continue

                        # UX-эмуляция перед чтением DOM
                        await simulate_pre_read_session(page)

                        # Пользовательский extractor — может бросить
                        # исключение если структура страницы изменилась
                        # или WAF подсунул challenge page.
                        data = await self.extractor(page)

                        # "Хвост" после чтения — реальный юзер полистал,
                        # задержался ещё немного, потом ушёл. Без tail'а
                        # мы отваливаемся ровно после скролла — заметный
                        # ботовский паттерн.
                        if self.post_read_tail_max_sec > 0:
                            tail = random.uniform(
                                self.post_read_tail_min_sec,
                                self.post_read_tail_max_sec,
                            )
                            if tail > 0:
                                try:
                                    await asyncio.sleep(tail)
                                except asyncio.CancelledError:
                                    raise

                        try:
                            await page.close()
                        except Exception:
                            pass
                        await self.rotator.mark_success(node)

                        return ScrapeResult(
                            url=url,
                            success=True,
                            data=data,
                            proxy_used=proxy_label,
                            attempts=attempt,
                            duration_sec=time.time() - start,
                            http_status=last_status,
                        )

                    except Exception:
                        # Закрываем page и пробрасываем — обработка снаружи
                        try:
                            await page.close()
                        except Exception:
                            pass
                        raise

            except asyncio.TimeoutError:
                await self.rotator.mark_failed(node, "timeout")
                last_error = "timeout"
                if attempt < self.max_retries:
                    await backoff_delay(attempt)
            except asyncio.CancelledError:
                self.rotator.release(node)
                raise
            except Exception as e:
                err_str = f"{type(e).__name__}: {str(e)[:120]}"
                last_error = err_str

                # Детект краха браузера. Playwright/Camoufox после
                # смерти Firefox бросает "Connection closed while
                # reading from the driver" на КАЖДЫЙ new_context/
                # new_page/goto. Без перезапуска все следующие URL
                # мгновенно фейлятся с этой ошибкой (0 сек duration).
                low = str(e).lower()
                is_browser_crash = (
                    "connection closed while reading from the driver" in low
                    or "target page, context or browser has been closed" in low
                    or "browser closed" in low
                )
                if is_browser_crash:
                    # Прокси не виноват — не отправляем его в cooldown
                    self.rotator.release(node)
                    # Если pool закрыт по нашей же отмене (watchdog вызвал
                    # emergency_pool_stop) — это НЕ крах, это cancel.
                    # НЕ пытаемся рестартить браузер (иначе восстановим
                    # то что сами специально закрыли), просто выходим.
                    if getattr(self, "_cancel_pool_closed", False) or self.cancel_check():
                        logger.info(
                            f"[worker] browser closed по отмене — выход без retry"
                        )
                        return ScrapeResult(
                            url=url, success=False,
                            error="cancelled",
                            attempts=attempt,
                            duration_sec=time.time() - start,
                        )
                    self._browser_crash_count += 1
                    self._browser_healthy = False
                    logger.error(
                        f"[worker] browser crash #{self._browser_crash_count}: {err_str}"
                    )
                    # Слишком много крашей подряд (>3) — что-то с
                    # ресурсами системы, force-stop чтоб не сжечь
                    # ещё больше прокси.
                    if self._browser_crash_count >= 3:
                        logger.error(
                            "[worker] 3+ browser crashes — force stop"
                        )
                        self._force_stop = True
                        return ScrapeResult(
                            url=url, success=False,
                            error="browser crashed too many times",
                            attempts=attempt,
                            duration_sec=time.time() - start,
                        )
                    # Атомарный перезапуск: только один воркер делает
                    # restart, остальные ждут пока он закончит.
                    async with self._restart_lock:
                        if not self._browser_healthy:
                            try:
                                await self.pool.restart()
                                self._browser_healthy = True
                            except Exception as re:
                                logger.error(
                                    f"[worker] restart failed: {re}"
                                )
                                self._force_stop = True
                                return ScrapeResult(
                                    url=url, success=False,
                                    error=f"restart failed: {re}",
                                    attempts=attempt,
                                    duration_sec=time.time() - start,
                                )
                    # Retry этот же URL с другим прокси
                    if attempt < self.max_retries:
                        await backoff_delay(attempt)
                    continue

                # Обычная сетевая ошибка — прокси в cooldown
                await self.rotator.mark_failed(node, err_str)
                if attempt < self.max_retries:
                    await backoff_delay(attempt)

        return ScrapeResult(
            url=url,
            success=False,
            error=last_error or "unknown",
            attempts=attempts_made,
            duration_sec=time.time() - start,
            http_status=last_status,
        )

    # ── Progress reporting ───────────────────────────────────────────

    async def _report_progress(self, current: str = "", reason: str = "") -> None:
        if not self.progress_callback:
            return
        async with self._in_flight_lock:
            in_flight = dict(self._in_flight)
        snapshot = {
            **self.stats.snapshot(),
            "current_url": current,
            "in_flight": in_flight,
            "rotator": self.rotator.stats(),
            "reason": reason,
        }
        try:
            await self.progress_callback(snapshot)
        except Exception as e:
            logger.debug(f"[scraper] progress_callback error: {e}")
