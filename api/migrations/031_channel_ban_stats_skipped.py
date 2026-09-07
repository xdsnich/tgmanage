"""Миграция 031: ChannelBanStats.skipped_count + last_skip_reason.

skipped_count — комменты которые НЕ дошли до отправки: канал без
discussion, ChatWriteForbidden, MsgIdInvalid и т.д. Не ошибка, но
и не успех. Растёт → системная проблема с каналом.
"""

MIGRATION_ID = "031"
DESCRIPTION  = "ChannelBanStats.skipped_count + last_skip_reason"

UP_SQL = [
    """
    ALTER TABLE channel_ban_stats
        ADD COLUMN IF NOT EXISTS skipped_count    INTEGER DEFAULT 0,
        ADD COLUMN IF NOT EXISTS last_skip_reason TEXT;
    """,
]

DOWN_SQL = [
    "ALTER TABLE channel_ban_stats DROP COLUMN IF EXISTS skipped_count;",
    "ALTER TABLE channel_ban_stats DROP COLUMN IF EXISTS last_skip_reason;",
]
