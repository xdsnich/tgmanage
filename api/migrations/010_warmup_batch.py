"""Добавить batch_id для группировки задач прогрева"""
import asyncio
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from config import DATABASE_URL

async def migrate():
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE warmup_tasks ADD COLUMN IF NOT EXISTS batch_id VARCHAR(32) NULL"))
        await conn.execute(text("ALTER TABLE warmup_tasks ADD COLUMN IF NOT EXISTS batch_name VARCHAR(128) NULL"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_warmup_batch ON warmup_tasks (batch_id)"))
        print("Done!")
    await engine.dispose()

asyncio.run(migrate())