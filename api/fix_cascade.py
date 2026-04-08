import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select
from config import DATABASE_URL
from utils.telegram import _get_device_fingerprint

async def fix():
    engine = create_async_engine(DATABASE_URL)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession)
    async with Session() as db:
        from models.account import TelegramAccount
        r = await db.execute(select(TelegramAccount))
        for acc in r.scalars().all():
            fp = _get_device_fingerprint(acc.phone)
            new_val = f"{fp['device']}|{fp['system']}|{fp['app_version']}"
            old_val = acc.device_fingerprint
            acc.device_fingerprint = new_val
            print(f"  {acc.phone}: {old_val} -> {new_val}")
        await db.commit()
    await engine.dispose()
    print("Done!")

asyncio.run(fix())