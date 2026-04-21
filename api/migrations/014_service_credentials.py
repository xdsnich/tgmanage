"""Миграция 014: Service credentials для LLM и сторонних сервисов"""

MIGRATION_ID = "014"
DESCRIPTION = "ServiceCredential: ключи для Claude/OpenAI/Gemini/Groq/TGStat"

UP_SQL = [
    """
    CREATE TABLE IF NOT EXISTS service_credentials (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        provider    VARCHAR(32) NOT NULL,
        api_key     TEXT NOT NULL,
        label       VARCHAR(128) DEFAULT '',
        is_active   BOOLEAN DEFAULT TRUE,
        is_default  BOOLEAN DEFAULT FALSE,
        notes       TEXT DEFAULT '',
        created_at  TIMESTAMP DEFAULT NOW(),
        updated_at  TIMESTAMP DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_service_credentials_user ON service_credentials(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_service_credentials_provider ON service_credentials(provider)",
]

DOWN_SQL = [
    "DROP INDEX IF EXISTS ix_service_credentials_provider",
    "DROP INDEX IF EXISTS ix_service_credentials_user",
    "DROP TABLE IF EXISTS service_credentials",
]
