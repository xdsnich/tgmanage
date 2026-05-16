"""
Миграция XXX: Добавить is_superuser в users
- Флаг для владельца / разработчика — снимает все лимиты
"""

# ВАЖНО: замени XXX на следующий номер у тебя в api/migrations/
# Если последняя миграция была 019_api_app_platform, то эта будет 020
MIGRATION_ID = "020"
DESCRIPTION = "Добавить is_superuser в users (снимает лимиты)"

UP_SQL = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_superuser BOOLEAN NOT NULL DEFAULT FALSE",
]

DOWN_SQL = [
    "ALTER TABLE users DROP COLUMN IF EXISTS is_superuser",
]
