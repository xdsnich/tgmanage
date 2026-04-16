"""
Миграция: добавить поддержку warmup планов в campaign_plans.

Запуск:
  cd api
  python migrations/009_warmup_plans.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from config import DATABASE_URL


async def migrate():
    engine = create_async_engine(DATABASE_URL)

    async with engine.begin() as conn:
        print("1. Делаем campaign_id nullable...")
        await conn.execute(text(
            "ALTER TABLE campaign_plans ALTER COLUMN campaign_id DROP NOT NULL"
        ))
        print("   OK")

        print("2. Добавляем колонку warmup_task_id...")
        await conn.execute(text(
            "ALTER TABLE campaign_plans ADD COLUMN IF NOT EXISTS warmup_task_id INTEGER NULL"
        ))
        print("   OK")

        print("3. Удаляем старый unique constraint...")
        await conn.execute(text(
            "ALTER TABLE campaign_plans DROP CONSTRAINT IF EXISTS campaign_plans_campaign_id_account_id_plan_date_key"
        ))
        print("   OK")

        print("4. Добавляем новый unique constraint...")
        # Сначала удалим если уже есть с таким именем
        await conn.execute(text(
            "ALTER TABLE campaign_plans DROP CONSTRAINT IF EXISTS campaign_plans_unique"
        ))
        await conn.execute(text(
            "ALTER TABLE campaign_plans ADD CONSTRAINT campaign_plans_unique "
            "UNIQUE (campaign_id, account_id, plan_date, warmup_task_id)"
        ))
        print("   OK")

        print("5. Индекс на warmup_task_id для быстрого поиска...")
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_campaign_plans_warmup_task "
            "ON campaign_plans (warmup_task_id) WHERE warmup_task_id IS NOT NULL"
        ))
        print("   OK")

    await engine.dispose()
    print("\nМиграция завершена!")


if __name__ == "__main__":
    asyncio.run(migrate())
