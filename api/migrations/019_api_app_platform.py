"""Миграция 019: добавляет поле platform в api_apps"""

MIGRATION_ID = "019"
DESCRIPTION = "API apps: поле platform (android/ios/desktop/macos) для правильных device fingerprints"

UP_SQL = [
    """
    ALTER TABLE api_apps
    ADD COLUMN IF NOT EXISTS platform VARCHAR(16) DEFAULT 'android'
    """,
    # Автоопределение платформы для уже существующих api_apps по известным публичным api_id
    "UPDATE api_apps SET platform = 'android'  WHERE api_id IN (6, 21724, 4)",
    "UPDATE api_apps SET platform = 'ios'      WHERE api_id IN (8)",
    "UPDATE api_apps SET platform = 'desktop'  WHERE api_id IN (2040, 17349)",
    "UPDATE api_apps SET platform = 'macos'    WHERE api_id IN (2834)",
    # Для всех остальных — android по умолчанию (самое безопасное)
    "UPDATE api_apps SET platform = 'android' WHERE platform IS NULL OR platform = ''",
    "CREATE INDEX IF NOT EXISTS ix_api_apps_platform ON api_apps(platform)",
]

DOWN_SQL = [
    "DROP INDEX IF EXISTS ix_api_apps_platform",
    "ALTER TABLE api_apps DROP COLUMN IF EXISTS platform",
]
