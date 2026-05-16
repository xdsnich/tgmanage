"""
Утилита: сделать юзера суперюзером.
Использование:
  cd api
  python make_superuser.py твой@email.com
"""
import sys
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select, update

from config import DATABASE_URL
from models.user import User


async def make_superuser(email: str):
    engine = create_async_engine(DATABASE_URL)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        # Найти юзера
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        if not user:
            print(f"❌ Юзер с email='{email}' не найден")
            await engine.dispose()
            return

        if user.is_superuser:
            print(f"✓ {email} (id={user.id}) уже суперюзер")
            await engine.dispose()
            return

        user.is_superuser = True
        await db.commit()
        print(f"✅ {email} (id={user.id}) теперь суперюзер — лимиты сняты")

    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python make_superuser.py твой@email.com")
        sys.exit(1)

    email = sys.argv[1].strip().lower()
    asyncio.run(make_superuser(email))
