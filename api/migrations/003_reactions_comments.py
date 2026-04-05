"""
Миграция 003: Реакции на комментарии
- Добавить поля target и comments_limit в reaction_tasks
"""

MIGRATION_ID = "003"
DESCRIPTION = "Добавить поля target и comments_limit в reaction_tasks"

UP_SQL = [
    "ALTER TABLE reaction_tasks ADD COLUMN IF NOT EXISTS target VARCHAR(32) DEFAULT 'post'",
    "ALTER TABLE reaction_tasks ADD COLUMN IF NOT EXISTS comments_limit INTEGER DEFAULT 5",
]

DOWN_SQL = [
    "ALTER TABLE reaction_tasks DROP COLUMN IF EXISTS target",
    "ALTER TABLE reaction_tasks DROP COLUMN IF EXISTS comments_limit",
]
