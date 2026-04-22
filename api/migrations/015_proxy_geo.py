"""Миграция 015: Geo поля для прокси (country, country_code, city)"""

MIGRATION_ID = "015"
DESCRIPTION = "Proxy: добавить country, country_code, city для отображения гео"

UP_SQL = [
    "ALTER TABLE proxies ADD COLUMN IF NOT EXISTS country VARCHAR(64) DEFAULT ''",
    "ALTER TABLE proxies ADD COLUMN IF NOT EXISTS country_code VARCHAR(8) DEFAULT ''",
    "ALTER TABLE proxies ADD COLUMN IF NOT EXISTS city VARCHAR(64) DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS ix_proxies_country ON proxies(country)",
]

DOWN_SQL = [
    "DROP INDEX IF EXISTS ix_proxies_country",
    "ALTER TABLE proxies DROP COLUMN IF EXISTS country",
    "ALTER TABLE proxies DROP COLUMN IF EXISTS country_code",
    "ALTER TABLE proxies DROP COLUMN IF EXISTS city",
]
