"""
GramGPT — utils/user_lock.py
Per-user concurrency limiter (anti-noisy-neighbor).

Зачем: 1 юзер с 500 TG-аккаунтами не должен съесть всю мощность воркера.
Например при 40 worker threads на 1000 платформенных юзеров — нечестно если
один пользователь монополизирует все 40 слотов.

Это per-user counter в Redis, инкрементируется при старте сессии плана,
декрементируется при завершении. TTL спасает от утечек если воркер крашится.

Использование внутри plan_executor:
  from utils.user_lock import acquire_user_slot, release_user_slot
  if not acquire_user_slot(acc.user_id):
      return {"status": "user_at_limit"}
  try:
      # ...работа...
  finally:
      release_user_slot(acc.user_id)
"""

import os
import logging
from utils.redis_pool import get_redis as _get_redis

logger = logging.getLogger(__name__)

_PREFIX = "gramgpt:user_slots:"

# Дефолтный лимит одновременных TG-сессий на одного User.
# Подбирается под кол-во воркер-тредов:
#   40 thread воркер → 10 slots/user → как минимум 4 разных юзера могут идти в полную силу
#   100 thread → 20 slots/user
# Перебивается env-переменной MAX_SLOTS_PER_USER
DEFAULT_MAX_PER_USER = int(os.getenv("MAX_SLOTS_PER_USER", "10"))
SLOT_TTL_SECONDS = 1800  # 30 минут — защита от утечек если процесс крашится


def acquire_user_slot(user_id: int, max_concurrent: int = DEFAULT_MAX_PER_USER) -> bool:
    """Atomic acquire — True если слот взят, False если юзер на лимите.
    Использует INCR (атомарный) + откат при превышении.

    TTL ставится ТОЛЬКО при первом инкременте с 0→1 (или когда TTL == -1).
    Иначе каждый acquire сбрасывал бы TTL и «зависший» счётчик после краша
    процесса без release НИКОГДА не exipre'ился (инцидент 2026-07-13).
    """
    try:
        r = _get_redis()
        key = f"{_PREFIX}{user_id}"
        current = r.incr(key)
        # Ставим TTL ТОЛЬКО если его не было (только что создали key).
        # `EXPIRE ... NX` — фича Redis 7.0+. На старом сервере (здесь 3.0.504)
        # он отвечает ResponseError "wrong number of arguments for 'expire'".
        #
        # ── ФИКС 2026-08-22 ───────────────────────────────────────
        # Ловился только TypeError (случай старого redis-py), а прилетал
        # ResponseError от старого СЕРВЕРА. Исключение уходило во внешний
        # except → fail-open, то есть acquire_user_slot ВСЕГДА возвращал
        # True и лимит одновременных сессий не работал вообще. Плюс ключ
        # оставался без TTL (наблюдалось user_slots:2 с ttl=-1) — ради
        # этого в dispatch_plans пришлось держать отдельный санитайзер.
        try:
            r.expire(key, SLOT_TTL_SECONDS, nx=True)
        except Exception:
            # Старый сервер/клиент — ставим TTL только если он не установлен.
            try:
                if r.ttl(key) < 0:
                    r.expire(key, SLOT_TTL_SECONDS)
            except Exception:
                pass
        if current > max_concurrent:
            # Откатываем — другой воркер уже занял последний слот
            r.decr(key)
            logger.info(f"[user_slot] user {user_id}: лимит {max_concurrent} достигнут (запрошено {current})")
            return False
        return True
    except Exception as e:
        # Fail-open — если Redis недоступен, не блокируем работу
        logger.warning(f"[user_slot] acquire error (user {user_id}): {e}")
        return True


def release_user_slot(user_id: int):
    """Atomic release. Никогда не уходит ниже 0."""
    try:
        r = _get_redis()
        key = f"{_PREFIX}{user_id}"
        val = r.decr(key)
        if val is not None and val < 0:
            r.set(key, 0)
    except Exception as e:
        logger.warning(f"[user_slot] release error (user {user_id}): {e}")


def get_user_active(user_id: int) -> int:
    """Текущее количество активных слотов юзера."""
    try:
        r = _get_redis()
        return int(r.get(f"{_PREFIX}{user_id}") or 0)
    except Exception:
        return 0


def reset_user_slots(user_id: int):
    """Сбросить счётчик в 0 (на случай прод-аварии)."""
    try:
        r = _get_redis()
        r.delete(f"{_PREFIX}{user_id}")
    except Exception:
        pass
