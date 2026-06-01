"""
Лимит публикаций сториз на аккаунт в день.

Telegram считает что нормальный пользователь не постит больше 1-2 сториз
в сутки. Постить 3+ как бот — флаг. Поэтому жёсткий лимит 1/день.

Использование: и в warmup, и в plan_executor вызываем перед публикацией:
    if not can_post_story_today(account_id):
        # skip — уже постили сегодня
    else:
        # ... публикация ...
        mark_story_posted_today(account_id)
"""

import os
import logging
from datetime import date
from utils.redis_pool import get_redis as _get_redis

logger = logging.getLogger(__name__)

# Дневной лимит (можно переопределить через env при желании, но >1 не рекомендую)
MAX_STORIES_PER_DAY = int(os.getenv("MAX_STORIES_PER_DAY", "1"))


def _key(account_id: int) -> str:
    return f"gramgpt:story_posted:{account_id}:{date.today().isoformat()}"


def can_post_story_today(account_id: int) -> bool:
    """True = можно публиковать. False = уже опубликовали сегодня."""
    try:
        r = _get_redis()
        count = int(r.get(_key(account_id)) or 0)
        return count < MAX_STORIES_PER_DAY
    except Exception:
        return True  # Fail-open: если Redis недоступен, не блокируем


def mark_story_posted_today(account_id: int) -> int:
    """Инкрементирует счётчик публикаций сегодня. Возвращает новое значение."""
    try:
        r = _get_redis()
        k = _key(account_id)
        n = r.incr(k)
        r.expire(k, 86400)  # авто-удаление через 24ч
        return int(n)
    except Exception:
        return 1


def get_story_count_today(account_id: int) -> int:
    """Сколько сториз опубликовано аккаунтом сегодня."""
    try:
        r = _get_redis()
        return int(r.get(_key(account_id)) or 0)
    except Exception:
        return 0
