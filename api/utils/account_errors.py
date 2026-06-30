"""
utils/account_errors.py — Helper для записи ошибок аккаунтов в БД.

Принципы:
  - Никогда не валит вызывающий код, ловит свои exceptions внутри
  - Авто-классификация error_type по тексту/типу исключения
  - Принимает либо db-сессию (если есть), либо открывает свою через NullPool
    (для Celery-тасок, где web-pool недоступен)

Использование:
    from utils.account_errors import log_account_error_sync

    try:
        await client.connect()
    except Exception as e:
        await log_account_error_async(
            db=None,                       # своё session откроет
            account_id=acc.id,
            user_id=acc.user_id,
            source="warmup",
            exc=e,
            message=f"Сессия упала на день {plan.day_number}",
            proxy_id=acc.proxy_id,
            warmup_task_id=plan.warmup_task_id,
        )
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Классификатор ошибок ───────────────────────────────────────

def classify_error(exc: Optional[BaseException], message: str = "") -> str:
    """Возвращает короткую категорию для UI/фильтра.

    Категории:
      FLOOD_WAIT, PEER_FLOOD, AUTH_KEY_UNREGISTERED, FROZEN,
      ProxyConnectError, ProxyTimeout, ConnectionError, Timeout,
      Captcha, SessionRevoked, Unknown
    """
    blob = (message or "")
    if exc is not None:
        blob += " " + (type(exc).__name__ or "") + " " + str(exc)
    bl = blob.upper()

    if "FLOOD_WAIT" in bl or "FLOODWAIT" in bl:
        return "FLOOD_WAIT"
    if "PEER_FLOOD" in bl or "PEERFLOOD" in bl:
        return "PEER_FLOOD"
    if "AUTH_KEY_UNREGISTERED" in bl or "AUTHKEYUNREG" in bl:
        return "AUTH_KEY_UNREGISTERED"
    if "USERDEACTIVATED" in bl or "BAN" in bl or "FROZEN" in bl:
        return "FROZEN"
    if "PROXY" in bl and ("TIMEOUT" in bl or "TIMED OUT" in bl):
        return "ProxyTimeout"
    if "PROXY" in bl and ("CONNECT" in bl or "REFUSED" in bl):
        return "ProxyConnectError"
    if "TIMEOUT" in bl or "TIMED OUT" in bl:
        return "Timeout"
    if "CONNECTION" in bl and ("REFUSED" in bl or "RESET" in bl or "ABORTED" in bl):
        return "ConnectionError"
    if "CAPTCHA" in bl:
        return "Captcha"
    if "SESSION" in bl and ("REVOKED" in bl or "EXPIRED" in bl):
        return "SessionRevoked"
    return "Unknown"


# ── Файловый дублирующий лог (per account) ─────────────────────
# По юзер-запросу: "в отдельных лог файлах". Пишем в
# logs/accounts/{user_id}/{account_id}.log

def _per_account_log_path(user_id: int, account_id: int) -> Path:
    root = Path(__file__).resolve().parent.parent.parent / "logs" / "accounts" / str(user_id)
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{account_id}.log"


def _write_file_log(user_id: int, account_id: int, source: str,
                    error_type: str, message: str, context: Optional[str]):
    try:
        path = _per_account_log_path(user_id, account_id)
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{source}] [{error_type}] {message}"
        if context:
            line += "\n    " + context.replace("\n", "\n    ")
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        logger.debug(f"[account_errors] file log skip: {e}")


# ── Запись в БД ────────────────────────────────────────────────

async def log_account_error_async(
    *,
    account_id: int,
    user_id: int,
    source: str,
    error_type: Optional[str] = None,
    exc: Optional[BaseException] = None,
    message: str = "",
    context: Optional[str] = None,
    proxy_id: Optional[int] = None,
    campaign_id: Optional[int] = None,
    warmup_task_id: Optional[int] = None,
    db = None,  # AsyncSession если уже есть, иначе откроем свою
) -> None:
    """Пишет ошибку в account_error_logs + дублирует в файл.
    Никогда не валит вызывающий код.
    """
    try:
        if not error_type:
            error_type = classify_error(exc, message)
        if not message and exc is not None:
            message = f"{type(exc).__name__}: {exc}"[:500]

        # Файловый дубль (быстро, не зависит от DB)
        _write_file_log(user_id, account_id, source, error_type, message, context)

        # БД-запись
        from models.account_error import AccountErrorLog
        row = AccountErrorLog(
            account_id=account_id,
            user_id=user_id,
            source=source[:32],
            error_type=error_type[:64],
            message=(message or "")[:5000],
            context=(context[:10000] if context else None),
            proxy_id=proxy_id,
            campaign_id=campaign_id,
            warmup_task_id=warmup_task_id,
            created_at=datetime.utcnow(),
        )
        if db is not None:
            db.add(row)
            # commit оставляем вызывающему коду (он мог быть в транзакции)
            try:
                await db.flush()
            except Exception:
                pass
        else:
            # Своя сессия для Celery-таски — NullPool
            from utils.db_pool import async_session as TaskSession
            async with TaskSession() as own_db:
                own_db.add(row)
                await own_db.commit()
    except Exception as e:
        # Лог "молча падает" не должен ронять прогрев/комментинг
        logger.warning(f"[account_errors] log skipped: {e}")
