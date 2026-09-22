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
  IP_COOLDOWN_SEC=600            (10 минут, дефолт — было 900/15мин,
                                  но при 5+ акках на IP это создавало
                                  циклы ip_cooldown и потерю сессий,
                                  инцидент 2026-07-14 → 126 smart_comment
                                  выкинуто из 285 запланированных за день)
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

IP_COOLDOWN_SEC = int(os.getenv("IP_COOLDOWN_SEC", "600"))           # 10 мин
IP_COOLDOWN_JITTER_SEC = int(os.getenv("IP_COOLDOWN_JITTER_SEC", "120"))
IP_THROTTLE_DISABLED = os.getenv("IP_THROTTLE_DISABLED", "0").lower() in ("1", "true", "yes")


def _proxy_key(host: str, port: Any) -> str:
    return f"gramgpt:ip_lock:{host}:{port}"


def _owner_key(host: str, port: int) -> str:
    """Чей адрес в текущем окне. Живёт полные IP_COOLDOWN_SEC и держит
    чужие аккаунты снаружи, даже когда сам замок уже снят."""
    return f"gramgpt:ip_owner:{host}:{port}"


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


def acquire_ip_lock(proxy_obj: Any, ttl: Optional[int] = None,
                    owner: Optional[int] = None) -> bool:
    """
    Захватывает per-IP cooldown lock.

    ttl — на сколько адрес считается занятым. Короткий ttl имеет смысл
    только вместе с owner (см. ниже); без owner он ослабил бы ровно ту
    защиту, ради которой замок и заводился.

    owner — id аккаунта, который подключается. Разделяет два очень разных
    риска, которые до этого лежали под одним замком:

      • разные аккаунты с одного адреса подряд — то, из-за чего замок и
        появился (2026-06-12: 166 акков на 33 IP за две секунды). Здесь
        по-прежнему держим полные IP_COOLDOWN_SEC;

      • тот же аккаунт переподключается с того же адреса — обычная жизнь
        телефона, и растягивать её на десять минут незачем.

    Поэтому владение адресом отмечается отдельным ключом на полные
    IP_COOLDOWN_SEC, а сам замок можно ставить коротким: пока адрес
    «принадлежит» аккаунту, он возвращается к нему быстро, а чужие
    подождут столько же, сколько ждали раньше.

    Ключ владения ставится только если его ещё нет и НЕ продлевается —
    иначе аккаунт, работающий часто, забрал бы адрес себе навсегда.

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

    # Джиттер соразмерен самому остыванию. Фиксированные ±120с поверх
    # десяти минут — это разброс в пятую часть, а поверх двух минут он
    # удвоил бы срок и съел весь смысл короткого окна. Верхний предел
    # прежний, так что для дефолтных десяти минут ничего не меняется.
    base = ttl if ttl is not None else IP_COOLDOWN_SEC
    jitter = min(IP_COOLDOWN_JITTER_SEC, max(5, base // 4))
    actual_ttl = base + random.randint(0, jitter)

    try:
        r = _get_redis()

        # Кому адрес принадлежит в текущем окне.
        prev = r.get(_owner_key(host, port))
        if prev is not None:
            prev = prev.decode() if isinstance(prev, bytes) else str(prev)
            if owner is None or str(owner) != prev:
                logger.info(
                    f"[ip_throttle] ⏳ {host}:{port} занят аккаунтом {prev}")
                return False

        acquired = r.set(_proxy_key(host, port), str(owner or "?"),
                         nx=True, ex=actual_ttl)
        if not acquired:
            logger.info(f"[ip_throttle] ⏳ {host}:{port} в cooldown")
            return False

        if owner is not None:
            r.set(_owner_key(host, port), str(owner),
                  nx=True, ex=IP_COOLDOWN_SEC)
        logger.debug(f"[ip_throttle] ✅ {host}:{port} захвачен на {actual_ttl}с")
        return True
    except Exception as e:
        logger.warning(f"[ip_throttle] redis error ({host}:{port}): {e}")
        return True  # fail-open — не блокируем работу при упавшем Redis


def is_ip_busy(host: str, port: Any = None, owner: Optional[int] = None) -> bool:
    """Занят ли адрес — БЕЗ захвата.

    Нужна там, где надо только выбрать подходящего кандидата и не отнимать
    адрес у того, кто его вот-вот займёт: отбор аккаунта под комментарий,
    диспетчер очереди. Захват делает уже сам исполнитель, когда доходит
    до подключения.

    Порт может быть неизвестен (в отборе аккаунтов у нас на руках только
    host) — тогда считаем адрес занятым, если занят любой его порт.
    """
    if IP_THROTTLE_DISABLED:
        return False
    if not host:
        return False
    try:
        r = _get_redis()
        if port is not None:
            if r.exists(_proxy_key(str(host), int(port))):
                return True
            keys = [_owner_key(str(host), int(port))]
        else:
            if r.keys(f"gramgpt:ip_lock:{host}:*"):
                return True
            keys = r.keys(f"gramgpt:ip_owner:{host}:*")

        # Замок снят, но адрес мог остаться закреплён за другим аккаунтом:
        # для него он свободен, для нас — нет. Спрашивающий без owner
        # узнаёт только про сам замок, как и раньше.
        if owner is None:
            return False
        for k in keys:
            v = r.get(k)
            if v is None:
                continue
            v = v.decode() if isinstance(v, bytes) else str(v)
            if v != str(owner):
                return True
        return False
    except Exception:
        return False   # redis упал — не мешаем работать


def refresh_ip_lock(proxy_obj: Any, ttl: Optional[int] = None) -> bool:
    """Продлить удержание IP.

    acquire_ip_lock ставит замок на IP_COOLDOWN_SEC (10 минут). Этого хватает
    обычной сессии, но не длинной работе: проверка каналов держит аккаунт
    25-40 минут, замок за это время истекает, и на тот же IP заходит кампания
    — получаются два одновременных подключения с одного адреса, ровно то,
    от чего механизм и защищает.
    Поэтому долгие задачи обязаны периодически продлевать замок.

    Здесь SET без NX: вызывающий уже владеет замком, и его задача — не
    захватить, а удержать.
    """
    hp = _extract_host_port(proxy_obj)
    if not hp or IP_THROTTLE_DISABLED:
        return True
    host, port = hp
    actual_ttl = (ttl if ttl is not None else IP_COOLDOWN_SEC) +                  random.randint(0, IP_COOLDOWN_JITTER_SEC)
    try:
        _get_redis().set(_proxy_key(host, port), "1", ex=actual_ttl)
        return True
    except Exception as e:
        logger.warning(f"[ip_throttle] refresh error ({host}:{port}): {e}")
        return False


def release_ip_lock(proxy_obj: Any) -> bool:
    """Освободить IP сразу, не дожидаясь истечения замка.

    Нужно длинным задачам: закончив работу, они должны вернуть адрес, иначе
    кампания зря прождёт остаток десятиминутного окна.
    """
    hp = _extract_host_port(proxy_obj)
    if not hp or IP_THROTTLE_DISABLED:
        return True
    host, port = hp
    try:
        _get_redis().delete(_proxy_key(host, port))
        logger.debug(f"[ip_throttle] 🔓 {host}:{port} освобождён")
        return True
    except Exception:
        return False


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
