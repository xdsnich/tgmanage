"""
Миграция 006: Commenting v2 — очередь комментариев и профили поведения
"""

MIGRATION_ID = "006"
DESCRIPTION = "Commenting v2: comment_queue, account_behavior, campaign_id в warmup_tasks"

UP_SQL = [
    # ── comment_queue ──────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS comment_queue (
        id            SERIAL PRIMARY KEY,
        campaign_id   INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
        account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        channel       VARCHAR(128) NOT NULL,
        post_id       INTEGER NOT NULL,
        post_text     TEXT DEFAULT '',
        personality   JSONB DEFAULT '{}',
        style         JSONB DEFAULT '{}',
        status        VARCHAR(32) DEFAULT 'scheduled',
        scheduled_at  TIMESTAMP NOT NULL,
        executed_at   TIMESTAMP,
        comment_text  TEXT,
        error         TEXT,
        created_at    TIMESTAMP DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_comment_queue_campaign_id ON comment_queue(campaign_id)",
    "CREATE INDEX IF NOT EXISTS ix_comment_queue_account_id ON comment_queue(account_id)",
    "CREATE INDEX IF NOT EXISTS ix_comment_queue_status ON comment_queue(status)",
    "CREATE INDEX IF NOT EXISTS ix_comment_queue_scheduled_at ON comment_queue(scheduled_at)",

    # ── account_behavior ───────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS account_behavior (
        id                       SERIAL PRIMARY KEY,
        account_id               INTEGER NOT NULL UNIQUE REFERENCES accounts(id) ON DELETE CASCADE,
        personality              VARCHAR(32) NOT NULL,
        timing_profile           VARCHAR(32) NOT NULL,
        style_profile            JSONB NOT NULL,
        comments_today           INTEGER DEFAULT 0,
        last_comment_at          TIMESTAMP,
        day_reset_at             TIMESTAMP,
        channels_commented_today JSONB DEFAULT '[]',
        created_at               TIMESTAMP DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_account_behavior_account_id ON account_behavior(account_id)",

    # ── warmup_tasks: добавить campaign_id ─────────────────────
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'warmup_tasks' AND column_name = 'campaign_id'
        ) THEN
            ALTER TABLE warmup_tasks ADD COLUMN campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL;
        END IF;
    END
    $$
    """,
]

DOWN_SQL = [
    "ALTER TABLE warmup_tasks DROP COLUMN IF EXISTS campaign_id",
    "DROP TABLE IF EXISTS account_behavior",
    "DROP TABLE IF EXISTS comment_queue",
]
