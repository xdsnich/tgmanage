import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text
from config import DATABASE_URL

async def fix():
    engine = create_async_engine(DATABASE_URL)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession)
    async with Session() as db:
        r = await db.execute(text("UPDATE warmup_tasks SET status = 'running' WHERE status = 'active'"))
        await db.commit()
        print(f"Updated {r.rowcount} tasks")
    await engine.dispose()

asyncio.run(fix())