"""Миграция 034: разделение источника ошибок + глобальный кэш свойств канала.

Зачем (расследование 2026-08-22):

1. warmup_logs.error_origin
   В колонке `error` были свалены в кучу настоящие ответы Telegram
   (chat_write_forbidden, FLOOD_WAIT_60) и наши внутренние причины
   пропуска (llm_empty, no_posts, shadow_ban_no_visible_msg).
   Отличить их можно было только регэкспом по тексту, поэтому любой
   счётчик «здоровья аккаунта» неизбежно начинал наказывать аккаунт за
   то, что у нас кончились ключи Groq. Теперь источник пишется явно:
     'telegram' — исключение прилетело от Telethon/Telegram
     'internal' — наше собственное решение не отправлять
   В бюджет здоровья аккаунта попадает ТОЛЬКО 'telegram'.

2. channel_capabilities
   Знание о канале хранилось per-account (Redis gramgpt:ch_forbidden:{acc}:{ch}),
   поэтому каждый из 141 аккаунта заново упирался в ту же стену и заново
   получал отказ от Telegram. Отсюда 156 ключей ch_forbidden и 255
   disc_invite_pending. Теперь свойства канала общие: проверил один
   аккаунт — знают все, канал сразу исключается из планов.

   Все поля заполняются ДВУМЯ read-only запросами (get_entity +
   GetFullChannelRequest), без вступления в группу и без отправки
   сообщения. Замер 2026-08-22: 30 каналов, 0 FLOOD_WAIT, 0.2с на канал.
"""

MIGRATION_ID = "034"
DESCRIPTION  = "warmup_logs.error_origin + channel_capabilities"

UP_SQL = [
    """
    ALTER TABLE warmup_logs
        ADD COLUMN IF NOT EXISTS error_origin VARCHAR(16);
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_warmup_logs_error_origin
        ON warmup_logs (account_id, error_origin, created_at);
    """,
    """
    CREATE TABLE IF NOT EXISTS channel_capabilities (
        id                  SERIAL PRIMARY KEY,
        channel_username    VARCHAR(128) NOT NULL UNIQUE,
        -- есть ли linked discussion group
        has_discussion      BOOLEAN,
        discussion_id       BIGINT,
        -- вступление только по заявке (флаг join_request)
        needs_approval      BOOLEAN,
        -- обычному участнику запрещено писать (default_banned_rights.send_messages)
        write_banned        BOOLEAN,
        -- итоговый вердикт: можно ли вообще пытаться комментировать
        commentable         BOOLEAN,
        reason              VARCHAR(64),
        -- телеметрия проверки
        checked_at          TIMESTAMP,
        checked_by_account  INTEGER,
        probe_source        VARCHAR(16),      -- 'telegram' | 'scraper' | 'observed'
        fail_count          INTEGER DEFAULT 0,
        last_error          VARCHAR(200),
        created_at          TIMESTAMP DEFAULT NOW()
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_channel_caps_commentable
        ON channel_capabilities (commentable, checked_at);
    """,
]

DOWN_SQL = [
    "DROP INDEX IF EXISTS ix_warmup_logs_error_origin;",
    "ALTER TABLE warmup_logs DROP COLUMN IF EXISTS error_origin;",
    "DROP INDEX IF EXISTS ix_channel_caps_commentable;",
    "DROP TABLE IF EXISTS channel_capabilities;",
]
