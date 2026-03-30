"""
Конвертирует колонку llm_provider из PostgreSQL enum в VARCHAR.
Это раз и навсегда убирает проблему с добавлением новых провайдеров.

cd api && python fix_enum_to_varchar.py
"""
import asyncio
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://gramgpt:gramgpt@localhost:5432/gramgpt")
# Извлекаем чистый URL для asyncpg (без +asyncpg)
PG_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


async def fix():
    conn = await asyncpg.connect(PG_URL)
    try:
        # 1. campaigns.llm_provider: enum → varchar
        try:
            await conn.execute("ALTER TABLE campaigns ALTER COLUMN llm_provider TYPE VARCHAR(32) USING llm_provider::text")
            print("✅ campaigns.llm_provider → VARCHAR(32)")
        except Exception as e:
            if "already" in str(e).lower() or "type" in str(e).lower():
                print(f"⚠ campaigns.llm_provider: {e}")
            else:
                print(f"❌ campaigns.llm_provider: {e}")

        # 2. campaigns.tone: enum → varchar
        try:
            await conn.execute("ALTER TABLE campaigns ALTER COLUMN tone TYPE VARCHAR(32) USING tone::text")
            print("✅ campaigns.tone → VARCHAR(32)")
        except Exception as e:
            print(f"⚠ campaigns.tone: {e}")

        # 3. campaigns.trigger_mode: enum → varchar
        try:
            await conn.execute("ALTER TABLE campaigns ALTER COLUMN trigger_mode TYPE VARCHAR(32) USING trigger_mode::text")
            print("✅ campaigns.trigger_mode → VARCHAR(32)")
        except Exception as e:
            print(f"⚠ campaigns.trigger_mode: {e}")

        # 4. campaigns.status: enum → varchar
        try:
            await conn.execute("ALTER TABLE campaigns ALTER COLUMN status TYPE VARCHAR(32) USING status::text")
            print("✅ campaigns.status → VARCHAR(32)")
        except Exception as e:
            print(f"⚠ campaigns.status: {e}")

        # 5. Удаляем старые enum типы
        for enum_name in ['llmprovider', 'commenttone', 'triggermode', 'campaignstatus']:
            try:
                await conn.execute(f"DROP TYPE IF EXISTS {enum_name}")
                print(f"✅ DROP TYPE {enum_name}")
            except Exception as e:
                print(f"⚠ DROP {enum_name}: {e}")

        print("\nГотово! Теперь llm_provider принимает любые значения: claude, openai, gemini, и т.д.")

    finally:
        await conn.close()


asyncio.run(fix())