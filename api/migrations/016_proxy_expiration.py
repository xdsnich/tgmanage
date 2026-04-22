"""Миграция 016: Proxy expiration (expires_at)"""

MIGRATION_ID = "016"
DESCRIPTION = "Proxy: срок действия прокси (expires_at)"

UP_SQL = [
    "ALTER TABLE proxies ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP NULL",
    "CREATE INDEX IF NOT EXISTS ix_proxies_expires_at ON proxies(expires_at)",
]

DOWN_SQL = [
    "DROP INDEX IF EXISTS ix_proxies_expires_at",
    "ALTER TABLE proxies DROP COLUMN IF EXISTS expires_at",
]
