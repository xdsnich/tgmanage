"""
node_rotator.py — Менеджер пула из 34 статических IPv4-узлов с cooldown.

Зачем такой класс существует:
  WAF-системы (Cloudflare, Akamai Bot Manager, DataDome, PerimeterX) ведут
  trust score на конкретный IP. Если узел получает 403/429/timeout —
  ставим ему cooldown 15-20 минут, чтобы trust score успел восстановиться.
  Иначе повторные запросы с того же IP только ухудшают репутацию и могут
  отправить адрес в постоянный blacklist.

Стратегия выдачи:
  Round-robin среди доступных узлов (не "первый рабочий"). Это распределяет
  нагрузку равномерно — иначе один IP получает все запросы, пока остальные
  стоят без дела, и быстрее палится.

Concurrency model:
  - Per-node asyncio.Lock — один воркер одновременно на один узел
  - Глобальный lock — атомарная выборка кандидата для round-robin
  - Состояние in-memory: при перезапуске процесса cooldown сбрасывается
    (это допустимо — за время рестарта IP всё равно "отдохнул")
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)


@dataclass
class ProxyNode:
    """Один прокси-узел из пула. URL в формате scheme://user:pass@host:port."""

    url: str
    cooldown_until: float = 0.0       # unix ts: до этого момента не выдаём
    last_used_at: float = 0.0         # последний acquire
    last_rotation_at: float = 0.0     # последняя смена контекста на этом IP
    successes: int = 0
    failures: int = 0
    # Кол-во ПОДРЯД идущих неудач без единого успеха между ними. Используется
    # для эскалирующего cooldown: 1-й 403 = x1, 2-й = x3, 3-й = x8 + burn.
    # Обнуляется на любом success.
    consecutive_failures: int = 0
    # Полностью выключен на эту сессию (сожжён): 3+ подряд 403. Не выдаём
    # даже если cooldown_until истёк — фингерпринт уже палёный.
    burned: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def is_cooldown(self) -> bool:
        return time.time() < self.cooldown_until

    @property
    def cooldown_remaining(self) -> int:
        return max(0, int(self.cooldown_until - time.time()))

    @property
    def is_busy(self) -> bool:
        return self._lock.locked()

    @property
    def is_available(self) -> bool:
        """Узел готов принять запрос: не занят, не в cooldown, не сожжён."""
        return not self.burned and not self.is_cooldown and not self.is_busy


class NodeRotator:
    """
    Пул из N узлов с cooldown-механизмом и round-robin выдачей.

    Пример использования:
        node = await rotator.acquire()
        try:
            ... use node.url ...
            await rotator.mark_success(node)
        except WafError:
            await rotator.mark_failed(node, "WAF block")
    """

    def __init__(
        self,
        proxies: list[str],
        cooldown_min_sec: int = 900,   # 15 минут
        cooldown_max_sec: int = 1200,  # 20 минут
    ):
        if not proxies:
            raise ValueError("Список прокси не может быть пустым")
        # Перемешиваем прокси-лист на инициализации. Иначе на каждом
        # запуске скрейпера первый прокси всегда идёт первым — TGStat
        # видит одну и ту же последовательность IP и может пометить
        # это как ботовский паттерн.
        _proxies = [u.strip() for u in proxies if u and u.strip()]
        random.shuffle(_proxies)
        self.nodes: list[ProxyNode] = [ProxyNode(url=u) for u in _proxies]
        if not self.nodes:
            raise ValueError("После фильтрации пустых строк прокси не осталось")
        self.cooldown_min_sec = cooldown_min_sec
        self.cooldown_max_sec = cooldown_max_sec
        self._global_lock = asyncio.Lock()
        logger.info(
            f"[rotator] Инициализировано {len(self.nodes)} узлов, "
            f"cooldown={cooldown_min_sec}-{cooldown_max_sec}с"
        )

    async def acquire(self, wait_timeout: float = 180.0, cancel_check=None) -> ProxyNode:
        """
        Возвращает следующий доступный узел и захватывает его per-node lock.

        Если все в cooldown или заняты — ждёт. Если все заняты и истёк
        wait_timeout (default 3 мин) — кидает TimeoutError. Раньше было
        10 мин — слишком долго, юзер в UI не понимал почему висит.
        Теперь воркер быстрее уходит с ошибкой и таск finalize'ится.

        cancel_check: опциональный callable() -> bool. Если во время wait
        возвращает True — прерываем как TimeoutError('cancelled'). Без
        этого acquire мог висеть до wait_timeout игнорируя нажатие Стоп.

        ВАЖНО: вызывающий обязан вызвать mark_success/mark_failed,
        иначе узел останется залоченным.
        """
        deadline = time.time() + wait_timeout
        while True:
            if cancel_check is not None and cancel_check():
                raise asyncio.TimeoutError("cancelled during node wait")
            async with self._global_lock:
                available = [n for n in self.nodes if n.is_available]
                if available:
                    # Случайный выбор из доступных вместо round-robin.
                    # Раньше _rr_index давал фиксированный порядок → TGStat
                    # видел один и тот же паттерн IP-ротации. random.choice
                    # ломает предсказуемость.
                    node = random.choice(available)
                    # acquire под global_lock'ом безопасен — мы только что
                    # проверили что lock свободен и никто другой не успеет
                    # его захватить пока мы держим global_lock.
                    await node._lock.acquire()
                    node.last_used_at = time.time()
                    return node

            if time.time() > deadline:
                raise asyncio.TimeoutError(
                    f"Все {len(self.nodes)} узлов заняты или в cooldown в течение {wait_timeout}с"
                )
            # Освобождаем event loop и пробуем снова — узлы освободятся
            # либо когда воркеры закончат, либо когда истечёт cooldown
            await asyncio.sleep(0.5)

    def release(self, node: ProxyNode) -> None:
        """Освобождает per-node lock без изменения статистики."""
        try:
            node._lock.release()
        except RuntimeError:
            pass  # уже отпущен — это не ошибка в нашем контексте

    async def mark_failed(self, node: ProxyNode, reason: str = "") -> None:
        """
        Помечает узел как зафейленный: ставит cooldown и освобождает lock.
        Вызывать на 403/429/timeout/connection error.

        Эскалирующий cooldown:
          - 1-й фейл подряд → base cooldown
          - 2-й фейл подряд → ×3 (Cloudflare помнит IP несколько минут после
            challenge — 60-120с точно не хватит, нужно 5-15 мин)
          - 3-й фейл подряд → ×8 + burn узел (фингерпринт спалён на эту
            сессию, дальше пробовать = ловить перманентный ban)

        Плюс мультипликатор по типу проблемы:
          - "HTTP 403" / "HTTP 429" — WAF-challenge, ×2 (CF помнит долго)
          - "HTTP 5xx" — сервер, ×1
          - "timeout" / "connection" — сетевые, ×1
        """
        node.consecutive_failures += 1
        node.failures += 1
        streak = node.consecutive_failures

        # Escalation по количеству подряд
        if streak == 1:
            streak_mult = 1.0
        elif streak == 2:
            streak_mult = 3.0
        else:  # 3+
            streak_mult = 8.0

        # Escalation по типу
        reason_low = (reason or "").lower()
        is_waf = "403" in reason_low or "429" in reason_low or "cloudflare" in reason_low
        reason_mult = 2.0 if is_waf else 1.0

        base = random.randint(self.cooldown_min_sec, self.cooldown_max_sec)
        cd = int(base * streak_mult * reason_mult)
        # Верхний предел — час, ниже нет смысла держать в cooldown
        cd = min(cd, 3600)
        node.cooldown_until = time.time() + cd

        # На 3-м подряд WAF-фейле — burn: фингерпринт этого IP+профиля
        # уже засветился, дальнейшие визиты в эту сессию только ухудшат
        # trust score. Проще выключить узел до следующего job'а.
        if streak >= 3 and is_waf:
            node.burned = True
            logger.warning(
                f"[rotator] Узел {self._mask(node.url)} СОЖЖЁН на эту сессию "
                f"(3 подряд WAF-фейла). Выключен до следующего job."
            )
        else:
            logger.warning(
                f"[rotator] Узел {self._mask(node.url)} → cooldown {cd}с "
                f"(reason: {reason}, streak: {streak}, mult: {streak_mult}×{reason_mult})"
            )
        self.release(node)

    async def mark_success(self, node: ProxyNode) -> None:
        """Помечает узел как успешный и освобождает lock. Сбрасывает streak."""
        node.successes += 1
        node.consecutive_failures = 0
        self.release(node)

    def mark_rotation(self, node: ProxyNode) -> None:
        """Запоминает момент смены контекста на узле — для соблюдения паузы."""
        node.last_rotation_at = time.time()

    @staticmethod
    def _mask(url: str) -> str:
        """Маскирует креды для логов: http://user:pass@host → http://***@host."""
        try:
            p = urlparse(url)
            if p.username or p.password:
                hostport = p.hostname or ""
                if p.port:
                    hostport += f":{p.port}"
                return urlunparse(p._replace(netloc=f"***@{hostport}"))
        except Exception:
            pass
        return url

    def stats(self) -> dict:
        """Снимок состояния пула — для прогресс-UI и мониторинга."""
        return {
            "total": len(self.nodes),
            "available": sum(1 for n in self.nodes if n.is_available),
            "in_cooldown": sum(1 for n in self.nodes if n.is_cooldown and not n.burned),
            "busy": sum(1 for n in self.nodes if n.is_busy),
            "burned": sum(1 for n in self.nodes if n.burned),
            "total_successes": sum(n.successes for n in self.nodes),
            "total_failures": sum(n.failures for n in self.nodes),
            "nodes": [
                {
                    "url": self._mask(n.url),
                    "available": n.is_available,
                    "cooldown_remaining": n.cooldown_remaining,
                    "successes": n.successes,
                    "failures": n.failures,
                    "consecutive_failures": n.consecutive_failures,
                    "burned": n.burned,
                    "busy": n.is_busy,
                }
                for n in self.nodes
            ],
        }
