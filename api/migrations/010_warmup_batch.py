"""Миграция 010: Warmup batch — группировка задач прогрева"""

MIGRATION_ID = "010"
DESCRIPTION = "WarmupTask: batch_id и batch_name для группировки задач прогрева"

UP_SQL = [
    "ALTER TABLE warmup_tasks ADD COLUMN IF NOT EXISTS batch_id VARCHAR(64) NULL",
    "ALTER TABLE warmup_tasks ADD COLUMN IF NOT EXISTS batch_name VARCHAR(128) NULL",
    "CREATE INDEX IF NOT EXISTS ix_warmup_tasks_batch_id ON warmup_tasks(batch_id) WHERE batch_id IS NOT NULL",
]

DOWN_SQL = [
    "DROP INDEX IF EXISTS ix_warmup_tasks_batch_id",
    "ALTER TABLE warmup_tasks DROP COLUMN IF EXISTS batch_name",
    "ALTER TABLE warmup_tasks DROP COLUMN IF EXISTS batch_id",
]