"""
utils/ip_throttle.py — Per-IP cooldown lock (Redis-based).

Цель: ни один IP/прокси не может обслужить второй Telegram-connect
ближе чем через 10-15 минут после первого. Это убивает риск burst'а
типа того, что случился 2026-06-12 (166 акков на 33 IP за 2 секунды).

Почему per-host:port, а не per-proxy_id:
  В БД могут быть несколько Proxy-записей, ссылающихся на физически
  один и тот же IP (например, http и socks5 на одном хосте, или две
  копии записи у разных юзеров). Замок нужен на ФИЗИЧЕСКИЙ адрес,
  иначе на одном IP всё равно соберётся pile-up.

ENV:
  IP_COOLDOWN_SEC=900            (15 минут, дефолт)
  IP_COOLDOWN_JITTER_SEC=120     (random ±0..120с к TTL, чтобы окна
                                  не выравнивались по минутным тикам)
  IP_THROTTLE_DISABLED=1         (для emergency/dev — отключить совсем)
"""

import logging
import os
import random
from typing import Any, Optional

from utils.redis_pool import get_redis as _get_redis

logger = logging.getLogger(__name__)

IP_COOLDOWN_SEC = int(os.getenv("IP_COOLDOWN_SEC", "900"))           # 15 мин
IP_COOLDOWN_JITTER_SEC = int(os.getenv("IP_COOLDOWN_JITTER_SEC", "120"))
IP_THROTTLE_DISABLED = os.getenv("IP_THROTTLE_DISABLED", "0").lower() in ("1", "true", "yes")


def _proxy_key(host: str, port: Any) -> str:
    return f"gramgpt:ip_lock:{host}:{port}"


def _extract_host_port(proxy_obj: Any) -> Optional[tuple[str, int]]:
    """Достаёт host:port из Proxy-модели или dict'а. None если не получилось."""
    if not proxy_obj:
        return None
    if isinstance(proxy_obj, dict):
        host = proxy_obj.get("host")
        port = proxy_obj.get("port")
    else:
        host = getattr(proxy_obj, "host", None)
        port = getattr(proxy_obj, "port", None)
    if not host or not port:
        return None
    try:
        return str(host), int(port)
    except (ValueError, TypeError):
        return None


def acquire_ip_lock(proxy_obj: Any, ttl: Optional[int] = None) -> bool:
    """
    Захватывает per-IP cooldown lock.

    Returns:
        True  — IP свободен, можно подключаться
        False — IP в cooldown, нужно отложить запуск
    """
    if IP_THROTTLE_DISABLED:
        return True
    hp = _extract_host_port(proxy_obj)
    if not hp:
        return True  # без прокси — не блокируем
    host, port = hp

    actual_ttl = (ttl if ttl is not None else IP_COOLDOWN_SEC) + \
                 random.randint(0, IP_COOLDOWN_JITTER_SEC)

    try:
        r = _get_redis()
        acquired = r.set(_proxy_key(host, port), "1", nx=True, ex=actual_ttl)
        if acquired:
            logger.debug(f"[ip_throttle] ✅ {host}:{port} захвачен на {actual_ttl}с")
        else:
            logger.info(f"[ip_throttle] ⏳ {host}:{port} в cooldown")
        return bool(acquired)
    except Exception as e:
        logger.warning(f"[ip_throttle] redis error ({host}:{port}): {e}")
        return True  # fail-open — не блокируем работу при упавшем Redis


def get_ip_cooldown_remaining(proxy_obj: Any) -> int:
    """
    Возвращает сколько секунд осталось до освобождения IP.
    0 если свободен или Redis недоступен.
    """
    if IP_THROTTLE_DISABLED:
        return 0
    hp = _extract_host_port(proxy_obj)
    if not hp:
        return 0
    host, port = hp
    try:
        r = _get_redis()
        ttl = r.ttl(_proxy_key(host, port))
        if ttl is None or ttl < 0:
            return 0
        return int(ttl)
    except Exception:
        return 0


def is_ip_locked(proxy_obj: Any) -> bool:
    """Удобный хелпер для UI/мониторинга."""
    return get_ip_cooldown_remaining(proxy_obj) > 0
