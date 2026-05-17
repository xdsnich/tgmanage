"""Миграция 022: добавляет поле language в parsed_channels"""

MIGRATION_ID = "022"
DESCRIPTION = "Parsed channels: поле language"

UP_SQL = [
    "ALTER TABLE parsed_channels ADD COLUMN IF NOT EXISTS language VARCHAR(10)"
]

DOWN_SQL = [
    "ALTER TABLE parsed_channels DROP COLUMN IF EXISTS language"
]