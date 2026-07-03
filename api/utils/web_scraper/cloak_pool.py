"""
cloak_pool.py — Пул персистентных profiles CloakBrowser: один долгоживущий
браузерный контекст на прокси.

Отличие от BrowserContextPool (Camoufox):
  BrowserContextPool держит один браузер и на каждый URL создаёт новый
  чистый context — всегда нулевые cookies и storage. Каждый визит для WAF
  выглядит как визит нового первого-раз-пришёл юзера. Через 100 URL таких
  «нулевых» визитов TGStat поднимает trust score, начинает подсовывать
  Cloudflare challenge, а после нескольких fail — блок IP.

  CloakProfilePool держит по одному persistent-context на прокси.
  Профиль хранит fingerprint, cookies, localStorage, ServiceWorker
  и sessionStorage между запусками скрейпера. Второй визит с того же
  прокси приходит уже «залогиненным» (с cookies от прошлой сессии) —
  WAF видит вернувшегося юзера, а не первый визит. Cloudflare-челлендж
  такому обычно не показывается.

Профили на диске:
  profiles_dir/{sha1(proxy_url)[:12]}/  — по одному на каждый прокси
  На 50 прокси в стабильном режиме ~ 2-3 GB. idle_shutdown закрывает
  контексты которые не используются > N секунд (RAM экономия — сам
  профиль на диске остаётся).

Лицензия бинарника:
  CloakBrowser Binary License: internal use для собственного бизнеса
  бесплатно. Chromium 148+ требует Pro-ключ; по умолчанию пинуем
  v146 которая свободна навсегда. Если у юзера есть Pro-ключ — можно
  передать через cloak_kwargs["license_key"].
"""

import asyncio
import hashlib
import logging
import random
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _profile_hash(proxy_url: str) -> str:
    """Стабильный короткий id профиля по URL прокси (одинаковый прокси →
    одинаковый профиль → тот же fingerprint, cookies и т.д. при повторном
    запуске скрейпера)."""
    return hashlib.sha1(proxy_url.encode("utf-8")).hexdigest()[:12]


class CloakProfilePool:
    """
    Пул persistent-контекстов CloakBrowser: по одному профилю на прокси.

    Поведение относительно интерфейса BrowserContextPool:
      - start()  — no-op в плане запуска браузера (запускаются лениво по
                   первому обращению к прокси). Только скачивает бинарник
                   если его ещё нет и стартует idle-janitor.
      - stop()   — закрывает ВСЕ живые контексты. Диск с профилями сохраняется.
      - restart()— крах Chromium → закрываем всё, следующий context_for
                   создаст с нуля.
      - context_for(node) — возвращает persistent context для прокси node.url.
                   Первый вызов на прокси → launch. Последующие → тот же ctx.

    Concurrency:
      Один воркер на один прокси одновременно (гарантирует NodeRotator per-node
      lock). Поэтому внутри одного persistent-контекста никогда не бывает двух
      параллельных страниц — безопасно.
    """

    def __init__(
        self,
        profiles_dir: str | Path,
        node_rotation_min_sec: float = 15.0,
        node_rotation_max_sec: float = 35.0,
        cloak_kwargs: dict | None = None,
        page_locale: str = "en-US",
        idle_shutdown_sec: float = 300.0,
        browser_version: str | None = None,
    ):
        self.profiles_dir = Path(profiles_dir)
        self.rotation_min = float(node_rotation_min_sec)
        self.rotation_max = float(node_rotation_max_sec)
        # kwargs которые уйдут в launch_persistent_context_async: humanize,
        # geoip, headless, license_key и т.п.
        self.cloak_kwargs = dict(cloak_kwargs or {})
        self.page_locale = page_locale
        self.idle_shutdown_sec = float(idle_shutdown_sec)
        # Дефолт cloakbrowser сейчас — 146.0.7680.177.5, свободная ветка
        # (Pro-ключ не нужен). Оставляем None → библиотека сама подставит.
        # Если юзер явно передал версию через cloak_kwargs["browser_version"]
        # или через параметр — уважим. ВАЖНО: нужен полный пин вида
        # "146.0.7680.177.5", а не "146" — иначе ValueError.
        self.browser_version = (
            self.cloak_kwargs.pop("browser_version", None) or browser_version
        )

        # proxy_url -> {"ctx": BrowserContext, "last_use": ts, "profile_dir": Path}
        self._contexts: dict[str, dict] = {}
        # На случай одновременного первого обращения двух воркеров к одному
        # прокси (теоретически невозможно из-за NodeRotator, но
        # архитектурно защищаемся). Один делает launch, второй ждёт.
        self._launch_locks: dict[str, asyncio.Lock] = {}
        self._last_use_per_node: dict[str, float] = {}

        self._started = False
        self._closing = False
        self._idle_task: asyncio.Task | None = None

    # ── Lifecycle ────────────────────────────────────────────

    async def start(self) -> None:
        """Готовит пул. Идемпотентно. Реальный browser не стартует до
        первого context_for() — старт по прокси, не глобальный."""
        if self._started:
            return
        try:
            from cloakbrowser import ensure_binary  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "cloakbrowser не установлен. Установи: pip install -U cloakbrowser"
            ) from e

        # Скачиваем бинарник Chromium заранее — на 200MB первый URL иначе
        # будет висеть под 30 сек. ensure_binary синхронный, поэтому
        # уводим в executor чтоб не блокировать event loop.
        try:
            from cloakbrowser import ensure_binary as _ensure
            # Передаём browser_version только если явно задан (иначе пусть
            # библиотека возьмёт свой дефолт — сейчас 146.0.7680.177.5,
            # свободная ветка).
            kwargs = {}
            if self.browser_version:
                kwargs["browser_version"] = self.browser_version
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: _ensure(**kwargs)
            )
        except Exception as e:
            # Не фатал — при launch попытается снова
            logger.warning(f"[cloak_pool] ensure_binary warning: {e}")

        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self._started = True
        self._closing = False
        self._idle_task = asyncio.create_task(
            self._idle_janitor(), name="cloak-idle-janitor"
        )
        # Считаем сколько было запусков всего — для отладки крашей
        self._launch_count_total = getattr(self, "_launch_count_total", 0)
        self._restart_count_total = getattr(self, "_restart_count_total", 0)
        # На случай если папка с профилями осталась с прошлых прогонов —
        # покажем сколько «унаследовали».
        existing = 0
        try:
            existing = sum(1 for _ in self.profiles_dir.iterdir() if _.is_dir())
        except Exception:
            pass
        logger.info(
            f"[cloak_pool] Готов. profiles_dir={self.profiles_dir} "
            f"(на диске уже {existing} профилей от прошлых прогонов) "
            f"browser_version={self.browser_version or 'default'} "
            f"idle_shutdown={self.idle_shutdown_sec}с "
            f"cloak_kwargs={dict((k, v) for k, v in self.cloak_kwargs.items() if k not in ('license_key',))}"
        )

    async def stop(self) -> None:
        """Закрывает все живые persistent-контексты. Профили на диске
        остаются — при следующем start() их подхватят с уже накопленными
        cookies и fingerprint."""
        self._closing = True
        if self._idle_task is not None:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except Exception:
                pass
            self._idle_task = None

        closed_ok = 0
        closed_fail = 0
        for proxy_url, entry in list(self._contexts.items()):
            ctx = entry.get("ctx")
            if ctx is None:
                continue
            try:
                await ctx.close()
                closed_ok += 1
            except Exception as e:
                closed_fail += 1
                # Часто это "Target page, context or browser has been closed" —
                # ожидаемо когда emergency_pool_stop уже закрыл, или когда
                # Chromium сам крахнул. Не тревожим warning'ом.
                logger.debug(
                    f"[cloak_pool] ctx.close error for {_profile_hash(proxy_url)}: {e}"
                )
        self._contexts.clear()
        self._launch_locks.clear()
        self._started = False
        self._closing = False
        logger.info(
            f"[cloak_pool] Закрыто {closed_ok} профилей (ошибок при закрытии: {closed_fail})"
        )

    async def restart(self) -> None:
        """Полный restart после краха Chromium. Диск с профилями сохраняется,
        следующее context_for() поднимет свежий контекст под тем же
        профилем — cookies и fingerprint останутся."""
        self._restart_count_total = getattr(self, "_restart_count_total", 0) + 1
        n_before = len(self._contexts)
        t0 = time.time()
        logger.warning(
            f"[cloak_pool] 🔄 Restart #{self._restart_count_total}: было "
            f"{n_before} живых профилей"
        )
        await self.stop()
        # Небольшая пауза чтобы OS корректно закрыла процессы
        await asyncio.sleep(2.0)
        await self.start()
        logger.info(
            f"[cloak_pool] ✅ Restart готов за {time.time()-t0:.1f}с "
            f"(профили на диске сохранились, cookies и fingerprint переживут)"
        )

    # ── Main API ─────────────────────────────────────────────

    @asynccontextmanager
    async def context_for(self, node):
        """
        Возвращает persistent BrowserContext для прокси node.url.
        Не закрывает контекст после yield — он живёт до stop/restart/idle.

        Соблюдает node rotation pause 15-35 сек: если тот же прокси
        использовался только что, ждём — эмулирует нормальный интервал
        между запросами реального юзера.

        Потребитель делает `page = await ctx.new_page()` и потом
        `await page.close()` — контекст между вызовами держит cookies.
        """
        if not self._started:
            raise RuntimeError("CloakProfilePool не запущен — вызови start()")

        proxy_url = node.url

        # Пауза между сессиями на одном IP
        last = self._last_use_per_node.get(proxy_url, 0.0)
        elapsed = time.time() - last
        required_pause = random.uniform(self.rotation_min, self.rotation_max)
        wait = required_pause - elapsed
        if wait > 0:
            logger.debug(
                f"[cloak_pool] rotation pause: {wait:.1f}с "
                f"(прошло {elapsed:.1f}с с прошлой сессии на этом прокси)"
            )
            await asyncio.sleep(wait)

        ctx = await self._get_or_launch(proxy_url)

        # Помечаем контекст как "в использовании" + освежаем last_use.
        # Без этого idle_janitor мог закрыть context прямо во время
        # обработки медленного URL (last_use оставалась старой пока
        # воркер сидел в page.goto/evaluate), из-за чего worker получал
        # "Target page, context or browser has been closed" mid-eval.
        entry = self._contexts.get(proxy_url)
        if entry is not None:
            entry["in_use"] = entry.get("in_use", 0) + 1
            entry["last_use"] = time.time()

        try:
            yield ctx
        finally:
            # НЕ закрываем ctx — в этом весь смысл пула. Только фиксируем
            # время последнего использования + освобождаем счётчик in_use.
            now = time.time()
            self._last_use_per_node[proxy_url] = now
            entry = self._contexts.get(proxy_url)
            if entry is not None:
                entry["last_use"] = now
                entry["in_use"] = max(0, entry.get("in_use", 1) - 1)

    # ── Internals ────────────────────────────────────────────

    async def _get_or_launch(self, proxy_url: str) -> Any:
        entry = self._contexts.get(proxy_url)
        if entry is not None and entry.get("ctx") is not None:
            return entry["ctx"]

        # Первый доступ к этому прокси — launch под lock, чтобы два воркера
        # не создали два конкурирующих контекста.
        launch_lock = self._launch_locks.setdefault(proxy_url, asyncio.Lock())
        async with launch_lock:
            entry = self._contexts.get(proxy_url)
            if entry is not None and entry.get("ctx") is not None:
                return entry["ctx"]

            from cloakbrowser import launch_persistent_context_async

            profile_dir = self.profiles_dir / _profile_hash(proxy_url)
            profile_dir.mkdir(parents=True, exist_ok=True)

            kw = dict(self.cloak_kwargs)
            kw.setdefault("headless", True)
            kw.setdefault("geoip", True)      # timezone/locale автоматом из IP прокси
            kw.setdefault("humanize", True)   # мышка/клавиатура/скролл под человека
            # Передаём именованные — они не должны быть перекрыты cloak_kwargs
            kw["proxy"] = proxy_url
            kw["locale"] = self.page_locale
            kw["user_data_dir"] = str(profile_dir)
            if self.browser_version:
                kw["browser_version"] = self.browser_version

            first_visit = not any(profile_dir.iterdir()) if profile_dir.exists() else True
            logger.info(
                f"[cloak_pool] launch profile {_profile_hash(proxy_url)} "
                f"(headless={kw.get('headless')}, geoip={kw.get('geoip')}, "
                f"humanize={kw.get('humanize')}, v={self.browser_version or 'default'}, "
                f"first_visit={first_visit})"
            )
            t0 = time.time()
            try:
                ctx = await launch_persistent_context_async(**kw)
            except Exception as e:
                logger.error(
                    f"[cloak_pool] ❌ launch FAILED for {_profile_hash(proxy_url)} "
                    f"после {time.time()-t0:.1f}с: {type(e).__name__}: {str(e)[:250]}"
                )
                raise
            self._launch_count_total = getattr(self, "_launch_count_total", 0) + 1
            logger.info(
                f"[cloak_pool] ✓ launched profile {_profile_hash(proxy_url)} "
                f"за {time.time()-t0:.1f}с "
                f"(всего запусков в этой сессии: {self._launch_count_total})"
            )
            self._contexts[proxy_url] = {
                "ctx": ctx,
                "last_use": time.time(),
                "profile_dir": profile_dir,
            }
            return ctx

    async def _close_one(self, proxy_url: str) -> None:
        entry = self._contexts.pop(proxy_url, None)
        if entry is None:
            return
        ctx = entry.get("ctx")
        if ctx is None:
            return
        try:
            await ctx.close()
            logger.info(
                f"[cloak_pool] закрыт профиль {_profile_hash(proxy_url)}"
            )
        except Exception as e:
            logger.debug(
                f"[cloak_pool] close_one error for {_profile_hash(proxy_url)}: {e}"
            )

    async def _idle_janitor(self) -> None:
        """
        Раз в 60 сек закрывает контексты без активности > idle_shutdown_sec.
        Профиль на диске остаётся, RAM освобождается. При следующем
        обращении к прокси — перезапуск с сохранёнными cookies.
        """
        try:
            while True:
                await asyncio.sleep(60.0)
                if self._closing:
                    return
                cutoff = time.time() - self.idle_shutdown_sec
                # Гоняем только те что действительно idle И никто их не
                # держит прямо сейчас. Иначе гонка: воркер сидит в
                # медленном page.goto/evaluate, ctx закрывается на нём.
                stale = [
                    url
                    for url, e in self._contexts.items()
                    if e.get("last_use", 0) < cutoff and e.get("in_use", 0) == 0
                ]
                if stale:
                    logger.info(
                        f"[cloak_pool] idle janitor: закрываю {len(stale)} "
                        f"профилей (не использовались >{int(self.idle_shutdown_sec)}с). "
                        f"Активных останется: {len(self._contexts) - len(stale)}"
                    )
                    for url in stale:
                        await self._close_one(url)
        except asyncio.CancelledError:
            return

    def stats(self) -> dict:
        """Снимок состояния пула — для UI/логов."""
        now = time.time()
        return {
            "profiles_total": len(self._contexts),
            "profiles_dir": str(self.profiles_dir),
            "browser_version": self.browser_version or "default",
            "profiles": [
                {
                    "proxy_hash": _profile_hash(url),
                    "idle_sec": int(now - e.get("last_use", now)),
                    "profile_dir": str(e.get("profile_dir", "")),
                }
                for url, e in self._contexts.items()
            ],
        }
