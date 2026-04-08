"""
Миграция 007: Device fingerprint в аккаунтах
- Хранить устройство в БД чтобы никогда не менялось
"""

MIGRATION_ID = "007"
DESCRIPTION = "Добавить device_fingerprint в accounts"

UP_SQL = [
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS device_fingerprint VARCHAR(64)",
]

DOWN_SQL = [
    "ALTER TABLE accounts DROP COLUMN IF EXISTS device_fingerprint",
]