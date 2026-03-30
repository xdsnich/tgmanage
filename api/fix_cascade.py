"""
Одноразовый скрипт: пересоздаёт FK constraints с ON DELETE CASCADE.
Запускать из папки api/:
  python fix_cascade.py
"""

import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from config import DATABASE_URL


async def fix():
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        # ai_dialogs: пересоздаём FK
        try:
            await conn.execute(text("""
                ALTER TABLE ai_dialogs
                DROP CONSTRAINT IF EXISTS ai_dialogs_account_id_fkey
            """))
            await conn.execute(text("""
                ALTER TABLE ai_dialogs
                ADD CONSTRAINT ai_dialogs_account_id_fkey
                FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
            """))
            print("✅ ai_dialogs FK fixed")
        except Exception as e:
            print(f"❌ ai_dialogs: {e}")

        # actions_log: пересоздаём FK
        try:
            await conn.execute(text("""
                ALTER TABLE actions_log
                DROP CONSTRAINT IF EXISTS actions_log_account_id_fkey
            """))
            await conn.execute(text("""
                ALTER TABLE actions_log
                ADD CONSTRAINT actions_log_account_id_fkey
                FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
            """))
            print("✅ actions_log FK fixed")
        except Exception as e:
            print(f"❌ actions_log: {e}")

    await engine.dispose()
    print("Done!")


asyncio.run(fix())
