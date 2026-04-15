"""
Лимит подключений к Telegram на аккаунт в день.
Не больше 6 подключений — норма для реального пользователя.
"""

import os
import logging
from datetime import date

logger = logging.getLogger(__name__)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
MAX_DAILY_CONNECTIONS = 6


def _get_redis():
    import redis
    return redis.from_url(REDIS_URL)


def check_connection_limit(account_id: int) -> bool:
    """True = можно подключаться. False = лимит исчерпан."""
    try:
        r = _get_redis()
        key = f"gramgpt:connects:{account_id}:{date.today().isoformat()}"
        count = int(r.get(key) or 0)
        if count >= MAX_DAILY_CONNECTIONS:
            logger.info(f"[limit] Аккаунт {account_id}: {count}/{MAX_DAILY_CONNECTIONS} подключений — лимит")
            return False
        return True
    except Exception:
        return True  # Fail-open


def increment_connection(account_id: int):
    """Увеличить счётчик подключений на 1."""
    try:
        r = _get_redis()
        key = f"gramgpt:connects:{account_id}:{date.today().isoformat()}"
        r.incr(key)
        r.expire(key, 86400)  # Автоудаление через 24ч
    except Exception:
        pass


def get_connection_count(account_id: int) -> int:
    """Текущее количество подключений сегодня."""
    try:
        r = _get_redis()
        key = f"gramgpt:connects:{account_id}:{date.today().isoformat()}"
        return int(r.get(key) or 0)
    except Exception:
        return 0