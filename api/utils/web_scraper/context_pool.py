"""
context_pool.py — Пул долгоживущих изолированных browser contexts на одном Camoufox.

Зачем пул вместо "браузер на каждый запрос":
  Запуск Camoufox-инстанса = 3-5 секунд (init JS engine, генерация
  fingerprint, прогрев). На 1000 URL это +1 час чистого простоя.
  Решение: один Camoufox живёт всё время работы скрейпера. На каждый
  URL создаётся свежий BrowserContext (полная изоляция cookies/cache/
  localStorage/IndexedDB), который закрывается после.

  Один и тот же Camoufox-инстанс при этом подаёт одинаковый fingerprint —
  это нормально, потому что разные контексты внутри браузера выглядят
  для удалённого WAF как разные пользователи (новые cookies + новый
  proxy = новый identity).

Пауза 15-35 сек между ротациями на одном IP:
  Если на один и тот же IP сразу идёт второй контекст, WAF видит
  "два независимых пользователя с одного адреса" — повышает score
  подозрительности. Пауза эмулирует естественный интервал.
"""

import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager
from typing import Any, TYPE_CHECKING

logger = logging.getLogger(__name__)


class BrowserContextPool:
    """
    Управляет одним Camoufox-инстансом и выдаёт изолированные контексты.

    Не пытается "переиспользовать" контексты между URL — это снижает
    изоляцию и провоцирует обнаружение. Контекст = один запрос.
    Браузер же остаётся живым.
    """

    def __init__(
        self,
        node_rotation_min_sec: float = 15.0,
        node_rotation_max_sec: float = 35.0,
        camoufox_kwargs: dict | None = None,
        page_locale: str = "en-US",
    ):
        self.rotation_min = node_rotation_min_sec
        self.rotation_max = node_rotation_max_sec
        # Аргументы для AsyncCamoufox: humanize, geoip, locale и т.д.
        # См. https://github.com/daijro/camoufox#parameters
        self.camoufox_kwargs = camoufox_kwargs or {}
        self.page_locale = page_locale
        self._browser: Any = None
        self._camoufox_session: Any = None
        self._last_use_per_node: dict[str, float] = {}
        self._started = False

    async def start(self) -> None:
        """Запускает Camoufox. Идемпотентно — повторный вызов no-op."""
        if self._started:
            return
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError as e:
            raise RuntimeError(
                "camoufox не установлен. Установи: pip install -U camoufox[geoip] "
                "&& camoufox fetch"
            ) from e

        # AsyncCamoufox — context manager, у которого __aenter__ запускает
        # Firefox-патченную сборку. Сохраняем session, чтобы корректно
        # закрыть в stop().
        self._camoufox_session = AsyncCamoufox(**self.camoufox_kwargs)
        self._browser = await self._camoufox_session.__aenter__()
        self._started = True
        logger.info("[context_pool] Camoufox запущен")

    async def stop(self) -> None:
        if not self._started:
            return
        if self._camoufox_session is not None:
            try:
                await self._camoufox_session.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"[context_pool] Ошибка при остановке Camoufox: {e}")
        self._browser = None
        self._camoufox_session = None
        self._started = False
        logger.info("[context_pool] Camoufox остановлен")

    @asynccontextmanager
    async def context_for(self, node):
        """
        Возвращает свежий изолированный context через прокси `node.url`.

        Поведение:
          1. Если этот IP использовался недавно — досыпаем до 15-35 сек
          2. Создаём context с прокси
          3. yield его потребителю
          4. В finally: закрываем context, фиксируем время использования IP
        """
        if not self._started:
            raise RuntimeError("BrowserContextPool не запущен — вызови start()")

        # 1. Соблюдаем паузу между сессиями на одном узле
        last = self._last_use_per_node.get(node.url, 0.0)
        elapsed = time.time() - last
        required_pause = random.uniform(self.rotation_min, self.rotation_max)
        wait = required_pause - elapsed
        if wait > 0:
            logger.debug(
                f"[context_pool] node rotation pause: жду {wait:.1f}с "
                f"(прошло {elapsed:.1f}с с прошлой сессии)"
            )
            await asyncio.sleep(wait)

        # 2. Создаём изолированный контекст
        # proxy server передаётся в Playwright-формате. Для http/https/socks5
        # Playwright сам парсит формат scheme://user:pass@host:port.
        proxy_cfg = self._parse_proxy(node.url)
        ctx = await self._browser.new_context(
            proxy=proxy_cfg,
            locale=self.page_locale,
        )

        try:
            yield ctx
        finally:
            # 3. Чистим состояние и закрываем — context больше не используется
            try:
                await ctx.clear_cookies()
            except Exception:
                pass
            try:
                await ctx.close()
            except Exception as e:
                logger.debug(f"[context_pool] ctx.close() error: {e}")
            self._last_use_per_node[node.url] = time.time()

    @staticmethod
    def _parse_proxy(url: str) -> dict:
        """
        Преобразует scheme://user:pass@host:port в Playwright-формат:
            {"server": "scheme://host:port", "username": "...", "password": "..."}
        Playwright поддерживает http, https, socks5.
        """
        from urllib.parse import urlparse

        p = urlparse(url)
        scheme = (p.scheme or "http").lower()
        host = p.hostname or ""
        port = p.port
        if not host or not port:
            raise ValueError(f"Невалидный прокси URL: {url}")

        cfg = {"server": f"{scheme}://{host}:{port}"}
        if p.username:
            cfg["username"] = p.username
        if p.password:
            cfg["password"] = p.password
        return cfg
