"""Миграция 020: добавляет поле last_verification в parsed_channels"""

MIGRATION_ID = "020"
DESCRIPTION = "Parsed channels: поле last_verification для фильтрации частоты проверок"

UP_SQL = [
    """
    ALTER TABLE parsed_channels
    ADD COLUMN IF NOT EXISTS last_verification TIMESTAMP
    """,
    # Додаємо індекс, оскільки ми будемо часто робити запити з умовою WHERE last_verification < ...
    "CREATE INDEX IF NOT EXISTS ix_parsed_channels_last_verification ON parsed_channels(last_verification)"
]

DOWN_SQL = [
    "DROP INDEX IF EXISTS ix_parsed_channels_last_verification",
    "ALTER TABLE parsed_channels DROP COLUMN IF EXISTS last_verification",
]