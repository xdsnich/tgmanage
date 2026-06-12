"""
retry.py — Exponential backoff с full jitter.

Когда retry применим:
  - TimeoutError (часто временное)
  - 5xx ответ (сервер моргнул)
  - ConnectionError, чтение байт оборвалось

Когда retry НЕ применим (обрабатывается выше):
  - 403/429 → узел в cooldown, retry с другим узлом
  - 4xx (кроме 429) → ошибка запроса, retry не поможет

Стратегия full jitter (AWS Architecture Blog):
  delay = uniform(0, base * factor^(attempt-1))
  В отличие от "fixed jitter" (delay ± noise), full jitter лучше
  размазывает повторные попытки во времени и снижает thundering herd
  когда много воркеров одновременно ретрают.

Дефолтные значения для 3 попыток:
  attempt 1: 0..5s
  attempt 2: 0..12.5s
  attempt 3: 0..31s
"""

import asyncio
import logging
import random

logger = logging.getLogger(__name__)


async def backoff_delay(
    attempt: int,
    base: float = 5.0,
    factor: float = 2.5,
    max_delay: float = 120.0,
    full_jitter: bool = True,
) -> float:
    """
    Спит exp-backoff паузу перед попыткой #attempt (1-based: 1, 2, 3...).

    Args:
        attempt: номер попытки (1 = первая повторная)
        base: базовая задержка
        factor: множитель экспоненты
        max_delay: верхний потолок паузы
        full_jitter: если True — uniform(0, exp); если False — exp ± 20%

    Returns:
        фактическую длительность сна (для логов)
    """
    raw = min(base * (factor ** (attempt - 1)), max_delay)

    if full_jitter:
        delay = random.uniform(0, raw)
    else:
        # "equal jitter": половина детерминированная, половина случайная
        delay = raw / 2 + random.uniform(0, raw / 2)

    logger.debug(f"[retry] attempt={attempt}, sleep={delay:.2f}s (raw={raw:.2f})")
    await asyncio.sleep(delay)
    return delay
