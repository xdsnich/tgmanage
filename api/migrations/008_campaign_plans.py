"""
Миграция 008: Campaign Plans — планы дня для каждого аккаунта
"""

MIGRATION_ID = "008"
DESCRIPTION = "Campaign Plans: рандомное расписание дня для каждого аккаунта кампании"

UP_SQL = [
    """
    CREATE TABLE IF NOT EXISTS campaign_plans (
        id              SERIAL PRIMARY KEY,
        campaign_id     INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
        account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        plan_date       DATE NOT NULL,
        day_number      INTEGER DEFAULT 1,
        plan            JSONB NOT NULL DEFAULT '{}',
        total_comments  INTEGER DEFAULT 0,
        executed_idx    INTEGER DEFAULT 0,
        status          VARCHAR(32) DEFAULT 'active',
        created_at      TIMESTAMP DEFAULT NOW(),
        
        UNIQUE(campaign_id, account_id, plan_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_campaign_plans_campaign ON campaign_plans(campaign_id)",
    "CREATE INDEX IF NOT EXISTS ix_campaign_plans_account ON campaign_plans(account_id)",
    "CREATE INDEX IF NOT EXISTS ix_campaign_plans_date ON campaign_plans(plan_date)",
    "CREATE INDEX IF NOT EXISTS ix_campaign_plans_status ON campaign_plans(status)",
]

DOWN_SQL = [
    "DROP TABLE IF EXISTS campaign_plans",
]
