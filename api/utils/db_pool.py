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


def _прогреть_движок():
    """Сделать самое первое подключение заранее, в одиночку.

    Первое обращение к движку защищено внутренним замком asyncio: он
    рождается в том цикле событий, который подключился первым. А каждая
    задача Celery крутится в СВОЁМ цикле (-P threads, свой loop на задачу),
    и когда две задачи стартуют в одну миллисекунду, вторая пытается
    дождаться замка из чужого цикла и падает с «Lock is bound to a
    different event loop». При каждом перезапуске так гибли по две первые
    задачи — обход каналов и диспетчер.

    Прогрев снимает гонку: к моменту, когда пойдут задачи, первое
    подключение давно сделано и замок больше не берётся.

    Если нас импортировали изнутри уже работающего цикла (FastAPI), греть
    нельзя и не нужно — там всё равно один цикл на процесс.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
        return
    except RuntimeError:
        pass

    async def _ping():
        from sqlalchemy import text as _t
        async with _engine.connect() as c:
            await c.execute(_t("SELECT 1"))

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_ping())
        logger.warning("[db_pool] Движок прогрет — первое подключение сделано")
    except Exception as e:
        # Не беда: без прогрева вернётся прежнее поведение, не более того.
        logger.warning("[db_pool] прогрев не удался: %s", e)
    finally:
        # dispose здесь НЕЛЬЗЯ: он пересоздаёт пул, и прогрев пропал бы зря.
        loop.close()


_прогреть_движок()


def get_engine():
    """Возвращает shared engine (если кому-то нужен для низкоуровневых операций)."""
    return _engine


async def dispose_pool():
    """Закрыть пул. Использовать ТОЛЬКО при остановке процесса."""
    await _engine.dispose()
