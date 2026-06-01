"""Миграция 029: Campaign.scheduled_start_at — отложенный авто-старт кампании.

Позволяет создать кампанию со status='scheduled' и опционально датой
scheduled_start_at. dispatch_plans автоматически переведёт её в active,
когда:
  - все WarmupTask привязанного warmup_batch_id окажутся в 'finished', ИЛИ
  - scheduled_start_at <= now (fallback)
— что наступит раньше.

Новый статус 'scheduled' хранится как обычная строка в campaigns.status
(enum хранится как VARCHAR), миграция SQL для него не нужна.
"""

MIGRATION_ID = "029"
DESCRIPTION  = "Campaign.scheduled_start_at — отложенный авто-старт кампании"

UP_SQL = [
    """
    ALTER TABLE campaigns
        ADD COLUMN IF NOT EXISTS scheduled_start_at TIMESTAMP;
    """,
    "CREATE INDEX IF NOT EXISTS ix_campaigns_scheduled_start ON campaigns(scheduled_start_at);",
]

DOWN_SQL = [
    "DROP INDEX IF EXISTS ix_campaigns_scheduled_start;",
    "ALTER TABLE campaigns DROP COLUMN IF EXISTS scheduled_start_at;",
]
