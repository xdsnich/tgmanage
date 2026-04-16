"""
GramGPT — routers/health.py
Мониторинг состояния системы.
"""

from fastapi import APIRouter
from datetime import datetime
import redis as redis_lib
import os

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def health_check():
    """Общий статус системы — без авторизации."""
    status = {"api": True, "redis": False, "celery_workers": 0, "workers": [], "timestamp": datetime.utcnow().isoformat()}

    # Redis
    try:
        r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        r.ping()
        status["redis"] = True
    except:
        pass

    # Celery workers
    # Celery workers
    try:
        from celery_app import celery_app
        inspect = celery_app.control.inspect(timeout=0.8)
        stats = inspect.stats() or {}
        active = inspect.active() or {}

        for worker_name, worker_stats in stats.items():
            status["workers"].append({
                "name": worker_name,
                "active_tasks": len(active.get(worker_name, [])),
                "pid": worker_stats.get("pid"),
                "uptime": worker_stats.get("clock", "?"),
            })

        status["celery_workers"] = len(stats)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Celery health check failed: {e}")

    # DB
    try:
        from sqlalchemy.ext.asyncio import create_async_engine
        from config import DATABASE_URL
        engine = create_async_engine(DATABASE_URL, pool_size=1)
        async with engine.connect() as conn:
            from sqlalchemy import text
            await conn.execute(text("SELECT 1"))
        status["database"] = True
        await engine.dispose()
    except:
        status["database"] = False

    status["healthy"] = status["api"] and status["redis"] and status["celery_workers"] > 0

    return status