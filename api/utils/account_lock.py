"""
GramGPT API — utils/account_lock.py
Redis-based lock to prevent parallel connections for the same account.
Uses SETNX for atomic acquire.
"""

import os
import logging

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
_LOCK_PREFIX = "gramgpt:account_lock:"


def _get_redis():
    import redis
    return redis.from_url(REDIS_URL)


def acquire_account_lock(account_id: int, ttl: int = 300) -> bool:
    """
    Try to acquire an exclusive lock for account_id.
    Returns True if lock acquired, False if already held.
    TTL prevents deadlocks if holder crashes.
    """
    try:
        r = _get_redis()
        key = f"{_LOCK_PREFIX}{account_id}"
        acquired = r.set(key, "1", nx=True, ex=ttl)
        if acquired:
            logger.debug(f"Lock acquired: account {account_id}")
        else:
            logger.info(f"Lock busy: account {account_id}")
        return bool(acquired)
    except Exception as e:
        logger.warning(f"Redis lock error (account {account_id}): {e}")
        return True  # Fail-open: allow if Redis is down


def release_account_lock(account_id: int):
    """Release the lock for account_id."""
    try:
        r = _get_redis()
        key = f"{_LOCK_PREFIX}{account_id}"
        r.delete(key)
        logger.debug(f"Lock released: account {account_id}")
    except Exception as e:
        logger.warning(f"Redis unlock error (account {account_id}): {e}")
