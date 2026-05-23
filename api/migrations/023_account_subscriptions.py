"""Миграция 023: таблиця account_subscriptions"""

MIGRATION_ID = "023"
DESCRIPTION = "Додано глобальний реєстр підписок (account_subscriptions)"

UP_SQL = [
    """
    CREATE TABLE IF NOT EXISTS account_subscriptions (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        channel_username VARCHAR(255) NOT NULL,
        status VARCHAR(32) DEFAULT 'active',
        joined_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_account_subscriptions_account_id ON account_subscriptions(account_id);",
    "CREATE INDEX IF NOT EXISTS ix_account_subscriptions_channel ON account_subscriptions(channel_username);"
]

DOWN_SQL = [
    "DROP TABLE IF EXISTS account_subscriptions CASCADE;"
]