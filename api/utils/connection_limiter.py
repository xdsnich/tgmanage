"""
Лимит подключений к Telegram на аккаунт в день.
Не больше 6 подключений — норма для реального пользователя.
"""

import os
import logging
from datetime import date
from utils.redis_pool import get_redis as _get_redis

logger = logging.getLogger(__name__)
# Сколько подключений к одному TG-аккаунту допустимо в сутки.
# 6 — нижняя граница для прогрева 4-6 сессий + комментинг.
# Поднимать только если знаешь что делаешь — Telegram связывает аккаунты
# по «device fingerprint + IP + connect frequency».
MAX_DAILY_CONNECTIONS = int(os.getenv("MAX_DAILY_CONNECTIONS", "6"))


def _key(account_id: int) -> str:
    return f"gramgpt:connects:{account_id}:{date.today().isoformat()}"


def try_acquire_connection(account_id: int) -> bool:
    """АТОМАРНО занимает слот подключения. True = можно, False = лимит.

    ── ФИКС 2026-08-22 ──────────────────────────────────────────
    Раньше проверка и инкремент были разнесены: check_connection_limit()
    делал GET в начале сессии, а increment_connection() — INCR только
    после успешного connect(), между ними шёл захват локов и IP-throttle.
    Диспетчер пускает сессии параллельно, поэтому несколько задач
    одновременно читали «5 < 6, можно» и все инкрементили.
    Замер по account_connections: 2026-07-24 восемнадцать аккаунтов
    превысили лимит 6 (максимум 8), 2026-07-20 — пятеро.

    Теперь INCR идёт ПЕРВЫМ и служит одновременно проверкой: если после
    инкремента значение больше лимита — откатываем и отказываем.
    Тот же приём, что в utils/user_lock.acquire_user_slot.
    """
    try:
        r = _get_redis()
        key = _key(account_id)
        count = int(r.incr(key))
        if count == 1:
            r.expire(key, 86400)
        elif r.ttl(key) < 0:
            # ключ без TTL (наследие старого кода) — не оставляем навечно
            r.expire(key, 86400)
        if count > MAX_DAILY_CONNECTIONS:
            r.decr(key)
            logger.info(
                f"[limit] Аккаунт {account_id}: {count - 1}/{MAX_DAILY_CONNECTIONS} подключений — лимит"
            )
            return False
        return True
    except Exception:
        return True  # Fail-open — Redis лёг, не блокируем работу


def release_connection(account_id: int):
    """Вернуть слот, если подключиться так и не удалось (прокси/лок/cooldown).

    Без этого отложенная из-за IP-cooldown сессия сжигала бы слот впустую.
    """
    try:
        r = _get_redis()
        key = _key(account_id)
        if int(r.get(key) or 0) > 0:
            r.decr(key)
    except Exception:
        pass


def check_connection_limit(account_id: int) -> bool:
    """DEPRECATED — не атомарна, оставлена для старых вызовов (warmup_v2).

    Только читает счётчик. Новый код должен звать try_acquire_connection().
    """
    try:
        r = _get_redis()
        count = int(r.get(_key(account_id)) or 0)
        if count >= MAX_DAILY_CONNECTIONS:
            logger.info(f"[limit] Аккаунт {account_id}: {count}/{MAX_DAILY_CONNECTIONS} подключений — лимит")
            return False
        return True
    except Exception:
        return True  # Fail-open


def increment_connection(account_id: int):
    """Увеличить счётчик подключений на 1.

    Для вызывающих, которые НЕ используют try_acquire_connection
    (warmup_v2, парсеры, ai_tasks) — там слот не резервируется заранее.
    В plan_executor больше не вызывается: слот уже занят в acquire.
    """
    try:
        r = _get_redis()
        key = _key(account_id)
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