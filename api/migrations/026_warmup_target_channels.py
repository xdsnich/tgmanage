"""Миграция 026: warmup с целевыми каналами (drip-подписка).

Идея: warmup-прогрев получает список 30+ каналов и в течение 7 дней
рандомно подписывается на них (0-3 в день на аккаунт). В конце прогрева
каналы переносятся в кампанию комментинга со статусом 'joined' — и аккаунт
не пишет в канал в первый день после подписки, а через несколько дней.

Новые поля warmup_tasks:
  - target_channels: list — кандидаты для подписки за время прогрева
  - subscribed_channels: dict — {channel_username: ISO timestamp}
  - daily_join_min, daily_join_max — диапазон подписок в день
  - joined_today — счётчик подписок за сегодня (сбрасывается с днём)
"""

MIGRATION_ID = "026"
DESCRIPTION  = "Warmup: drip-подписка на целевые каналы"

UP_SQL = [
    """
    ALTER TABLE warmup_tasks
        ADD COLUMN IF NOT EXISTS target_channels    JSONB DEFAULT '[]'::jsonb NOT NULL,
        ADD COLUMN IF NOT EXISTS subscribed_channels JSONB DEFAULT '{}'::jsonb NOT NULL,
        ADD COLUMN IF NOT EXISTS daily_join_min     INTEGER DEFAULT 0  NOT NULL,
        ADD COLUMN IF NOT EXISTS daily_join_max     INTEGER DEFAULT 3  NOT NULL,
        ADD COLUMN IF NOT EXISTS joined_today       INTEGER DEFAULT 0  NOT NULL;
    """,
]

DOWN_SQL = [
    """
    ALTER TABLE warmup_tasks
        DROP COLUMN IF EXISTS target_channels,
        DROP COLUMN IF EXISTS subscribed_channels,
        DROP COLUMN IF EXISTS daily_join_min,
        DROP COLUMN IF EXISTS daily_join_max,
        DROP COLUMN IF EXISTS joined_today;
    """,
]
