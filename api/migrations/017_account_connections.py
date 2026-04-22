"""Миграция 017: лог подключений аккаунтов (account_connections)"""

MIGRATION_ID = "017"
DESCRIPTION = "AccountConnection: история подключений для каждого аккаунта"

UP_SQL = [
    """
    CREATE TABLE IF NOT EXISTS account_connections (
        id             SERIAL PRIMARY KEY,
        account_id     INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        connected_at   TIMESTAMP DEFAULT NOW() NOT NULL,
        source         VARCHAR(32) DEFAULT 'unknown',
        proxy_id       INTEGER NULL REFERENCES proxies(id) ON DELETE SET NULL,
        success        BOOLEAN DEFAULT TRUE,
        error          VARCHAR(500) NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_account_connections_account_date ON account_connections(account_id, connected_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_account_connections_source ON account_connections(source)",
]

DOWN_SQL = [
    "DROP INDEX IF EXISTS ix_account_connections_source",
    "DROP INDEX IF EXISTS ix_account_connections_account_date",
    "DROP TABLE IF EXISTS account_connections",
]
