"""
GramGPT — utils/db_pool.py
Shared async SQLAlchemy engine + session factory.

Зачем: до этого каждый Celery-таск создавал свой engine (~50мс на старт).
Теперь — один engine на процесс. Каждая task-сессия открывает свежее соединение.

ВАЖНО: используется NullPool (не пулинг соединений!) — причина:
  С Celery -P threads каждый таск работает в своём треде со своим asyncio loop.
  Если пулить соединения, они привязаны к loop'у того треда где открылись,
  и asyncpg падает при попытке использовать соединение из другого loop'а:
  "got Future <Future pending> attached to a different loop".

  NullPool открывает свежее соединение для каждой сессии и закрывает после.
  Overhead ~5-10мс на локальный PostgreSQL — копейки на фоне Telegram I/O.

Использование:
  from utils.db_pool import async_session as Session

  async with Session() as db:
      # ... работа ...
"""

import logging
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool

from config import DATABASE_URL

logger = logging.getLogger(__name__)


_engine = create_async_engine(
    DATABASE_URL,
    poolclass=NullPool,  # ← каждый таск открывает свежее соединение, безопасно для threads+asyncio
    pool_pre_ping=True,
)

async_session = async_sessionmaker(
    bind=_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

logger.info("[db_pool] Engine создан (NullPool — fresh connection per session)")


def get_engine():
    """Возвращает shared engine (если кому-то нужен для низкоуровневых операций)."""
    return _engine


async def dispose_pool():
    """Закрыть пул. Использовать ТОЛЬКО при остановке процесса."""
    await _engine.dispose()
