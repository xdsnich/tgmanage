"""
GramGPT — utils/redis_pool.py
Shared Redis connection pool.

Зачем: каждый вызов redis.from_url() открывает новый TCP-коннект (~50мс).
При 40 параллельных задачах × 5-10 redis-операций = 200-400 connect/disconnect/сек.
Один pool на процесс мультиплексирует все вызовы.

Использование:
  from utils.redis_pool import get_redis
  r = get_redis()
  r.set(...)
"""

import os
import logging
import redis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Один пул на процесс. max_connections — потолок одновременных коннектов.
# 50 = достаточно для 100 параллельных gevent-корутин.
_pool: redis.ConnectionPool | None = None


def get_redis() -> redis.Redis:
    """Возвращает Redis-клиент использующий общий пул."""
    global _pool
    if _pool is None:
        _pool = redis.ConnectionPool.from_url(
            REDIS_URL,
            max_connections=50,
            socket_timeout=5,
            socket_connect_timeout=5,
            health_check_interval=30,
        )
        logger.info(f"[redis_pool] Pool создан: {REDIS_URL} (max=50)")
    return redis.Redis(connection_pool=_pool)


def close_pool():
    """Закрыть пул (для тестов и graceful shutdown)."""
    global _pool
    if _pool is not None:
        try:
            _pool.disconnect()
        except Exception:
            pass
        _pool = None
