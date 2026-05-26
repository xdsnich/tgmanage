"""
GramGPT — utils/db_pool.py
Shared async SQLAlchemy engine + session factory.

Зачем: до этого каждый Celery-таск создавал свой engine (~50мс на старт +
открытие 1-2 TCP-коннектов к PG). При 40 параллельных тасках = 40 engine'ов
= 80+ TCP-коннектов одновременно.

Один engine на процесс с pool_size=20 + max_overflow=50 → до 70 параллельных
сессий, реальных TCP-коннектов ~25 (мультиплексируются).

Использование внутри Celery-тасков:
  from utils.db_pool import async_session as Session

  async with Session() as db:
      # ... работа ...

  # Engine.dispose() НЕ вызывать — он живёт всё время воркера.
"""

import os
import logging
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from config import DATABASE_URL

logger = logging.getLogger(__name__)

# ── Параметры пула ──────────────────────────────────────
# pool_size:     постоянные соединения (всегда живые)
# max_overflow:  дополнительные соединения которые открываются по запросу
# pool_pre_ping: чекать TCP перед использованием (защита от мёртвых)
# pool_recycle:  переоткрывать соединение раз в 30 мин (защита от idle timeout)
# pool_timeout:  макс ожидание свободного соединения, потом ошибка
DB_POOL_SIZE     = int(os.getenv("DB_POOL_SIZE", "20"))
DB_MAX_OVERFLOW  = int(os.getenv("DB_MAX_OVERFLOW", "50"))
DB_POOL_TIMEOUT  = int(os.getenv("DB_POOL_TIMEOUT", "30"))
DB_POOL_RECYCLE  = int(os.getenv("DB_POOL_RECYCLE", "1800"))


_engine = create_async_engine(
    DATABASE_URL,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=DB_POOL_TIMEOUT,
    pool_recycle=DB_POOL_RECYCLE,
    pool_pre_ping=True,
)

async_session = async_sessionmaker(
    bind=_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

logger.info(
    f"[db_pool] Engine создан: pool_size={DB_POOL_SIZE}, "
    f"max_overflow={DB_MAX_OVERFLOW} (макс {DB_POOL_SIZE + DB_MAX_OVERFLOW} сессий)"
)


def get_engine():
    """Возвращает shared engine (если кому-то нужен для низкоуровневых операций)."""
    return _engine


async def dispose_pool():
    """Закрыть пул. Использовать ТОЛЬКО при остановке процесса."""
    await _engine.dispose()
