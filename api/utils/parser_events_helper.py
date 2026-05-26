"""
GramGPT API — utils/parser_events.py
Хелпер для логирования событий парсера в БД (неблокирующий).

Использование из Celery-таска:
    await log_event(user_id, event_type="flood_wait", source="similar",
                    wait_seconds=33, seed="@crypto")
"""

import os
import sys
import logging
from typing import Optional

logger = logging.getLogger(__name__)
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


async def log_event(
    user_id: int,
    event_type: str,
    source: Optional[str] = None,
    account_id: Optional[int] = None,
    wait_seconds: int = 0,
    channels_found: int = 0,
    channels_saved: int = 0,
    duration_sec: int = 0,
    seed: Optional[str] = None,
    details: Optional[str] = None,
):
    """Записывает событие парсера в БД. При ошибке — просто логирует, не падает."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    try:
        from models.parser_event import ParserEvent
        from utils.db_pool import async_session as Session

        async with Session() as db:
            event = ParserEvent(
                user_id=user_id,
                account_id=account_id,
                event_type=event_type,
                source=source,
                wait_seconds=wait_seconds,
                channels_found=channels_found,
                channels_saved=channels_saved,
                duration_sec=duration_sec,
                seed=seed[:256] if seed else None,
                details=details[:1000] if details else None,
            )
            db.add(event)
            await db.commit()
    except Exception as e:
        logger.warning(f"[parser_events] failed to log: {e}")
