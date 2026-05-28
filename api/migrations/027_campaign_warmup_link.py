"""Миграция 027: связь кампании комментинга с batch'ем прогрева.

Когда кампания создаётся из прогрева — она знает batch_id прогрева.
При старте кампании каналы на которые аккаунты уже подписались за прогрев
становятся commentable с 1-го дня (status='joined'), а не требуют
повторной подписки. Это и есть "не пишем в первый день после подписки".
"""

MIGRATION_ID = "027"
DESCRIPTION  = "Campaign.warmup_batch_id — связь с прогревом для pre-joined каналов"

UP_SQL = [
    """
    ALTER TABLE campaigns
        ADD COLUMN IF NOT EXISTS warmup_batch_id VARCHAR(64);
    """,
    "CREATE INDEX IF NOT EXISTS ix_campaigns_warmup_batch ON campaigns(warmup_batch_id);",
]

DOWN_SQL = [
    "DROP INDEX IF EXISTS ix_campaigns_warmup_batch;",
    "ALTER TABLE campaigns DROP COLUMN IF EXISTS warmup_batch_id;",
]
