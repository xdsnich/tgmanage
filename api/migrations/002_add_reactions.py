"""
Миграция 002: Реакции на посты
- Создать таблицу reaction_tasks
"""

MIGRATION_ID = "002"
DESCRIPTION = "Добавить таблицу reaction_tasks"

UP_SQL = [
    """
    CREATE TABLE IF NOT EXISTS reaction_tasks (
        id               SERIAL PRIMARY KEY,
        user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        channel_link     VARCHAR(256) NOT NULL,
        post_id          INTEGER,
        account_ids      JSONB DEFAULT '[]',
        reactions        JSONB DEFAULT '[]',
        mode             VARCHAR(32) DEFAULT 'random',
        count            INTEGER DEFAULT 0,
        delay_min        INTEGER DEFAULT 3,
        delay_max        INTEGER DEFAULT 15,
        status           VARCHAR(32) DEFAULT 'pending',
        reactions_sent   INTEGER DEFAULT 0,
        reactions_failed INTEGER DEFAULT 0,
        error            TEXT,
        results          JSONB DEFAULT '[]',
        started_at       TIMESTAMP,
        finished_at      TIMESTAMP,
        created_at       TIMESTAMP DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_reaction_tasks_user_id ON reaction_tasks(user_id)",
]

DOWN_SQL = [
    "DROP TABLE IF EXISTS reaction_tasks",
]
