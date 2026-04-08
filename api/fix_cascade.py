import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select
from config import DATABASE_URL

async def fix():
    engine = create_async_engine(DATABASE_URL)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession)
    async with Session() as db:
        from models.account import TelegramAccount
        r = await db.execute(select(TelegramAccount))
        for acc in r.scalars().all():
            if acc.device_fingerprint and "|ru" in acc.device_fingerprint:
                old = acc.device_fingerprint
                # Убираем |ru или |en из конца, оставляем device|system|app_version
                parts = acc.device_fingerprint.split("|")
                if len(parts) == 4:
                    acc.device_fingerprint = f"{parts[0]}|{parts[1]}|{parts[2]}"
                print(f"  {acc.phone}: {old} -> {acc.device_fingerprint}")
            elif acc.device_fingerprint:
                print(f"  {acc.phone}: {acc.device_fingerprint} (ok)")
        await db.commit()
    await engine.dispose()
    print("Done!")

asyncio.run(fix())