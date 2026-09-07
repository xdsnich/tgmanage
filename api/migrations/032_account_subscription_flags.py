"""Миграция 032: расширить AccountSubscription для anti-ban / переиспользования.

Добавляем колонки:
  - channel_id           BIGINT NULL — Telegram peer ID (опционально)
  - is_channel_joined    BOOLEAN DEFAULT false
  - is_discussion_joined BOOLEAN DEFAULT false
  - discovered_via       VARCHAR(32) DEFAULT ''
  - last_verified_at     TIMESTAMP DEFAULT now()

Использование: при join_target/join_channel/join_discussion_of/react/comment
плюс через GetParticipantRequest check. При старте кампании — не ставим
join_target_channel action если акк уже подписан.
"""

MIGRATION_ID = "032"
DESCRIPTION  = "AccountSubscription: channel_id + is_channel_joined + is_discussion_joined + discovered_via"

UP_SQL = [
    """
    ALTER TABLE account_subscriptions
        ADD COLUMN IF NOT EXISTS channel_id           BIGINT,
        ADD COLUMN IF NOT EXISTS is_channel_joined    BOOLEAN DEFAULT false NOT NULL,
        ADD COLUMN IF NOT EXISTS is_discussion_joined BOOLEAN DEFAULT false NOT NULL,
        ADD COLUMN IF NOT EXISTS discovered_via       VARCHAR(32) DEFAULT '' NOT NULL,
        ADD COLUMN IF NOT EXISTS last_verified_at     TIMESTAMP DEFAULT NOW() NOT NULL;
    """,
    # UNIQUE (account_id, channel_username) — если старая миграция не сделала
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'account_subscriptions_account_id_channel_username_key'
        ) THEN
            ALTER TABLE account_subscriptions
                ADD CONSTRAINT account_subscriptions_account_id_channel_username_key
                UNIQUE (account_id, channel_username);
        END IF;
    END $$;
    """,
]

DOWN_SQL = [
    "ALTER TABLE account_subscriptions DROP COLUMN IF EXISTS channel_id;",
    "ALTER TABLE account_subscriptions DROP COLUMN IF EXISTS is_channel_joined;",
    "ALTER TABLE account_subscriptions DROP COLUMN IF EXISTS is_discussion_joined;",
    "ALTER TABLE account_subscriptions DROP COLUMN IF EXISTS discovered_via;",
    "ALTER TABLE account_subscriptions DROP COLUMN IF EXISTS last_verified_at;",
]
