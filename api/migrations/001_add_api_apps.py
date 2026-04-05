"""
Миграция 001: Мульти-API система
- Создать таблицу api_apps
- Добавить колонку api_app_id в accounts
"""

MIGRATION_ID = "001"
DESCRIPTION = "Добавить таблицу api_apps + колонку api_app_id в accounts"

UP_SQL = [
    # ── Таблица api_apps ─────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS api_apps (
        id            SERIAL PRIMARY KEY,
        user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        api_id        INTEGER NOT NULL,
        api_hash      VARCHAR(64) NOT NULL,
        title         VARCHAR(128) DEFAULT '',
        max_accounts  INTEGER DEFAULT 100,
        is_active     BOOLEAN DEFAULT TRUE,
        notes         TEXT DEFAULT '',
        created_at    TIMESTAMP DEFAULT NOW(),
        updated_at    TIMESTAMP DEFAULT NOW()
    )
    """,

    "CREATE INDEX IF NOT EXISTS ix_api_apps_user_id ON api_apps(user_id)",

    # ── Новая колонка в accounts ─────────────────────────────
    """
    ALTER TABLE accounts
        ADD COLUMN IF NOT EXISTS api_app_id INTEGER
        REFERENCES api_apps(id) ON DELETE SET NULL
    """,

    "CREATE INDEX IF NOT EXISTS ix_accounts_api_app_id ON accounts(api_app_id)",
]

DOWN_SQL = [
    "ALTER TABLE accounts DROP COLUMN IF EXISTS api_app_id",
    "DROP INDEX IF EXISTS ix_api_apps_user_id",
    "DROP TABLE IF EXISTS api_apps",
]