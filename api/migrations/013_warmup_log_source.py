"""Миграция 013: разделение логов прогрева и комментинга"""

MIGRATION_ID = "013"
DESCRIPTION = "WarmupLog: добавить source и campaign_id для разделения"

UP_SQL = [
    "ALTER TABLE warmup_logs ADD COLUMN IF NOT EXISTS source VARCHAR(32) DEFAULT 'warmup'",
    "ALTER TABLE warmup_logs ADD COLUMN IF NOT EXISTS campaign_id INTEGER NULL",
    "CREATE INDEX IF NOT EXISTS ix_warmup_logs_source ON warmup_logs(source)",
    "CREATE INDEX IF NOT EXISTS ix_warmup_logs_campaign_id ON warmup_logs(campaign_id) WHERE campaign_id IS NOT NULL",
]

DOWN_SQL = [
    "DROP INDEX IF EXISTS ix_warmup_logs_campaign_id",
    "DROP INDEX IF EXISTS ix_warmup_logs_source",
    "ALTER TABLE warmup_logs DROP COLUMN IF EXISTS campaign_id",
    "ALTER TABLE warmup_logs DROP COLUMN IF EXISTS source",
]