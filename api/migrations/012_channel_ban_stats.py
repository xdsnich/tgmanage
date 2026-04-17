"""Миграция 012: статистика банов по каналам"""

MIGRATION_ID = "012"
DESCRIPTION = "Channel ban stats: отслеживание проходимости каналов"

UP_SQL = [
    """
    CREATE TABLE IF NOT EXISTS channel_ban_stats (
        id                  SERIAL PRIMARY KEY,
        user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        channel_username    VARCHAR(128) NOT NULL,
        total_attempts      INTEGER DEFAULT 0,
        banned_count        INTEGER DEFAULT 0,
        last_ban_reason     TEXT,
        last_updated        TIMESTAMP DEFAULT NOW(),
        created_at          TIMESTAMP DEFAULT NOW(),
        UNIQUE(user_id, channel_username)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_channel_ban_stats_user ON channel_ban_stats(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_channel_ban_stats_username ON channel_ban_stats(channel_username)",
]

DOWN_SQL = ["DROP TABLE IF EXISTS channel_ban_stats"]