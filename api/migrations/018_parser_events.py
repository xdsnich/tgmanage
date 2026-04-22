"""Миграция 018: события парсера (FLOOD_WAIT, скорость, метрики)"""

MIGRATION_ID = "018"
DESCRIPTION = "Parser events: логирование FLOOD_WAIT и метрик парсера"

UP_SQL = [
    """
    CREATE TABLE IF NOT EXISTS parser_events (
        id              SERIAL PRIMARY KEY,
        user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        account_id      INTEGER,
        event_type      VARCHAR(32) NOT NULL,   -- flood_wait | session_start | session_done | error
        source          VARCHAR(32),            -- similar | search | verify | import
        wait_seconds    INTEGER DEFAULT 0,
        channels_found  INTEGER DEFAULT 0,
        channels_saved  INTEGER DEFAULT 0,
        duration_sec    INTEGER DEFAULT 0,
        seed            VARCHAR(256),           -- для crawler: seed-канал
        details         TEXT,
        created_at      TIMESTAMP DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_parser_events_user_date ON parser_events(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_parser_events_type ON parser_events(event_type)",
    "CREATE INDEX IF NOT EXISTS ix_parser_events_source ON parser_events(source)",
]

DOWN_SQL = ["DROP TABLE IF EXISTS parser_events"]
