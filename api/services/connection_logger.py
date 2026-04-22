"""
GramGPT API — services/connection_logger.py
Простой async helper для записи подключения в БД.
Используется во всех местах где делается client.connect() — plan_executor, warmup_v2, comment_executor, ai_tasks.
"""

import logging
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def log_connection(
    db: AsyncSession,
    account_id: int,
    source: str = "unknown",
    success: bool = True,
    proxy_id: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    """
    Записывает одно подключение в таблицу account_connections.
    Не крашит даже если модель не подгружена / БД недоступна.
    """
    try:
        from models.account_connection import AccountConnection
        db.add(AccountConnection(
            account_id=account_id,
            source=source,
            success=success,
            proxy_id=proxy_id,
            error=error[:500] if error else None,
        ))
        await db.flush()
    except Exception as e:
        logger.warning(f"[connection_logger] Не удалось записать подключение acc={account_id}: {e}")
        try:
            await db.rollback()
        except Exception:
            pass
