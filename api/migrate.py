"""
GramGPT — migrate.py
Запуск миграций БД.

Использование:
  cd api
  python migrate.py              # применить все новые миграции
  python migrate.py --status     # показать статус
  python migrate.py --down 001   # откатить миграцию 001
"""

import asyncio
import sys
import os
import importlib
import glob
from datetime import datetime

# Добавляем api/ в path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


async def get_engine():
    from sqlalchemy.ext.asyncio import create_async_engine
    from config import DATABASE_URL
    return create_async_engine(DATABASE_URL, pool_size=2, max_overflow=0)


async def ensure_migrations_table(engine):
    """Создаёт таблицу для отслеживания миграций (если нет)."""
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS _migrations (
                id          SERIAL PRIMARY KEY,
                migration   VARCHAR(128) NOT NULL UNIQUE,
                applied_at  TIMESTAMP DEFAULT NOW()
            )
        """))


async def get_applied(engine) -> set:
    """Какие миграции уже применены."""
    from sqlalchemy import text
    async with engine.begin() as conn:
        result = await conn.execute(text("SELECT migration FROM _migrations ORDER BY id"))
        return {row[0] for row in result.fetchall()}


async def discover_migrations() -> list:
    """Находит все файлы миграций в папке migrations/."""
    migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")
    files = sorted(glob.glob(os.path.join(migrations_dir, "[0-9]*.py")))

    result = []
    for f in files:
        name = os.path.basename(f).replace(".py", "")
        spec = importlib.util.spec_from_file_location(f"migrations.{name}", f)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        result.append({
            "name": name,
            "id": getattr(mod, "MIGRATION_ID", name[:3]),
            "description": getattr(mod, "DESCRIPTION", ""),
            "up_sql": getattr(mod, "UP_SQL", []),
            "down_sql": getattr(mod, "DOWN_SQL", []),
        })

    return result


async def run_up(engine, migration: dict):
    """Применить миграцию."""
    from sqlalchemy import text
    name = migration["name"]
    print(f"  ⏳ Применяю: {name} — {migration['description']}")

    async with engine.begin() as conn:
        for i, sql in enumerate(migration["up_sql"], 1):
            sql = sql.strip()
            if not sql:
                continue
            try:
                await conn.execute(text(sql))
                print(f"     ✓ SQL #{i} OK")
            except Exception as e:
                print(f"     ✗ SQL #{i} ОШИБКА: {e}")
                raise

        # Записываем что миграция применена
        await conn.execute(
            text("INSERT INTO _migrations (migration) VALUES (:name)"),
            {"name": name}
        )

    print(f"  ✅ {name} — применена")


async def run_down(engine, migration: dict):
    """Откатить миграцию."""
    from sqlalchemy import text
    name = migration["name"]
    print(f"  ⏳ Откат: {name}")

    async with engine.begin() as conn:
        for i, sql in enumerate(migration["down_sql"], 1):
            sql = sql.strip()
            if not sql:
                continue
            try:
                await conn.execute(text(sql))
                print(f"     ✓ Rollback SQL #{i} OK")
            except Exception as e:
                print(f"     ✗ Rollback SQL #{i} ОШИБКА: {e}")
                raise

        await conn.execute(
            text("DELETE FROM _migrations WHERE migration = :name"),
            {"name": name}
        )

    print(f"  ✅ {name} — откачена")


async def cmd_migrate():
    """Применить все непримёненные миграции."""
    engine = await get_engine()
    await ensure_migrations_table(engine)

    applied = await get_applied(engine)
    migrations = await discover_migrations()

    pending = [m for m in migrations if m["name"] not in applied]

    if not pending:
        print("✅ Все миграции уже применены. БД актуальна.")
        await engine.dispose()
        return

    print(f"📦 Найдено {len(pending)} новых миграций:\n")

    for m in pending:
        await run_up(engine, m)

    print(f"\n✅ Готово! Применено: {len(pending)} миграций.")
    await engine.dispose()


async def cmd_status():
    """Показать статус миграций."""
    engine = await get_engine()
    await ensure_migrations_table(engine)

    applied = await get_applied(engine)
    migrations = await discover_migrations()

    print("📋 Статус миграций:\n")
    for m in migrations:
        status = "✅ применена" if m["name"] in applied else "⬚  ожидает"
        print(f"  {status}  {m['name']} — {m['description']}")

    if not migrations:
        print("  (нет файлов миграций в api/migrations/)")

    await engine.dispose()


async def cmd_down(migration_id: str):
    """Откатить конкретную миграцию."""
    engine = await get_engine()
    await ensure_migrations_table(engine)

    applied = await get_applied(engine)
    migrations = await discover_migrations()

    target = None
    for m in migrations:
        if m["id"] == migration_id or m["name"].startswith(migration_id):
            target = m
            break

    if not target:
        print(f"❌ Миграция '{migration_id}' не найдена")
        await engine.dispose()
        return

    if target["name"] not in applied:
        print(f"⚠️  Миграция '{target['name']}' ещё не применена")
        await engine.dispose()
        return

    await run_down(engine, target)
    await engine.dispose()


def main():
    args = sys.argv[1:]

    if "--status" in args:
        asyncio.run(cmd_status())
    elif "--down" in args:
        idx = args.index("--down")
        if idx + 1 < len(args):
            asyncio.run(cmd_down(args[idx + 1]))
        else:
            print("Укажи ID миграции: python migrate.py --down 001")
    else:
        asyncio.run(cmd_migrate())


if __name__ == "__main__":
    main()