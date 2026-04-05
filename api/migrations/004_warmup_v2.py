"""
Миграция 004: Новая система прогрева
- Новые поля в warmup_tasks (day, schedule, daily stats)
- Таблица warmup_logs для детальных логов
"""

MIGRATION_ID = "004"
DESCRIPTION = "Новая система прогрева с логами и расписанием"

UP_SQL = [
    # ── Логи прогрева ────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS warmup_logs (
        id           SERIAL PRIMARY KEY,
        task_id      INTEGER NOT NULL REFERENCES warmup_tasks(id) ON DELETE CASCADE,
        account_id   INTEGER NOT NULL,
        action       VARCHAR(64) NOT NULL,
        detail       TEXT DEFAULT '',
        emoji        VARCHAR(16) DEFAULT '',
        channel      VARCHAR(128) DEFAULT '',
        success      BOOLEAN DEFAULT TRUE,
        error        TEXT,
        created_at   TIMESTAMP DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_warmup_logs_task_id ON warmup_logs(task_id)",
    "CREATE INDEX IF NOT EXISTS ix_warmup_logs_account_id ON warmup_logs(account_id)",
    "CREATE INDEX IF NOT EXISTS ix_warmup_logs_created_at ON warmup_logs(created_at)",

    # ── Новые поля в warmup_tasks ────────────────────────
    "ALTER TABLE warmup_tasks ADD COLUMN IF NOT EXISTS day INTEGER DEFAULT 1",
    "ALTER TABLE warmup_tasks ADD COLUMN IF NOT EXISTS day_started_at TIMESTAMP",
    "ALTER TABLE warmup_tasks ADD COLUMN IF NOT EXISTS today_actions INTEGER DEFAULT 0",
    "ALTER TABLE warmup_tasks ADD COLUMN IF NOT EXISTS today_limit INTEGER DEFAULT 5",
    "ALTER TABLE warmup_tasks ADD COLUMN IF NOT EXISTS is_resting BOOLEAN DEFAULT FALSE",
    "ALTER TABLE warmup_tasks ADD COLUMN IF NOT EXISTS next_action_at TIMESTAMP",
    "ALTER TABLE warmup_tasks ADD COLUMN IF NOT EXISTS start_offset_min INTEGER DEFAULT 0",
    "ALTER TABLE warmup_tasks ADD COLUMN IF NOT EXISTS total_days INTEGER DEFAULT 7",
]

DOWN_SQL = [
    "DROP TABLE IF EXISTS warmup_logs",
    "ALTER TABLE warmup_tasks DROP COLUMN IF EXISTS day",
    "ALTER TABLE warmup_tasks DROP COLUMN IF EXISTS day_started_at",
    "ALTER TABLE warmup_tasks DROP COLUMN IF EXISTS today_actions",
    "ALTER TABLE warmup_tasks DROP COLUMN IF EXISTS today_limit",
    "ALTER TABLE warmup_tasks DROP COLUMN IF EXISTS is_resting",
    "ALTER TABLE warmup_tasks DROP COLUMN IF EXISTS next_action_at",
    "ALTER TABLE warmup_tasks DROP COLUMN IF EXISTS start_offset_min",
    "ALTER TABLE warmup_tasks DROP COLUMN IF EXISTS total_days",
]
