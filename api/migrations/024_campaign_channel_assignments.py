"""Миграция 024: матрица распределения каналов по аккаунтам кампании"""

MIGRATION_ID = "024"
DESCRIPTION  = "Таблица campaign_channel_assignments — органическая воронка подписок"

UP_SQL = [
    """
    CREATE TABLE IF NOT EXISTS campaign_channel_assignments (
        id               SERIAL PRIMARY KEY,
        campaign_id      INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
        account_id       INTEGER NOT NULL REFERENCES accounts(id)  ON DELETE CASCADE,
        channel_username VARCHAR(255) NOT NULL,
        status           VARCHAR(32)  NOT NULL DEFAULT 'pending',
        planned_join_day INTEGER,
        assigned_at      TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        joined_at        TIMESTAMP WITHOUT TIME ZONE
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_cca_campaign  ON campaign_channel_assignments(campaign_id);",
    "CREATE INDEX IF NOT EXISTS ix_cca_account   ON campaign_channel_assignments(account_id);",
    "CREATE INDEX IF NOT EXISTS ix_cca_status    ON campaign_channel_assignments(status);",
]

DOWN_SQL = [
    "DROP TABLE IF EXISTS campaign_channel_assignments CASCADE;",
]
