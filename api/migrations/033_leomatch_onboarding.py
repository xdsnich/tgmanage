"""Миграция 033: таблицы для массового онбординга в @leomatchbot."""

MIGRATION_ID = "033"
DESCRIPTION = "leomatch_onboarding_tasks + leomatch_onboarding_items"

UP_SQL = [
    """
    CREATE TABLE IF NOT EXISTS leomatch_onboarding_tasks (
        id            SERIAL PRIMARY KEY,
        user_id       INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        label         VARCHAR(128) DEFAULT '' NOT NULL,
        cities        JSONB DEFAULT '[]'::jsonb NOT NULL,
        descriptions  JSONB DEFAULT '[]'::jsonb NOT NULL,
        names         JSONB DEFAULT '[]'::jsonb NOT NULL,
        photos        JSONB DEFAULT '[]'::jsonb NOT NULL,
        language      VARCHAR(32) DEFAULT '🇺🇦 Українська' NOT NULL,
        age_min       INT DEFAULT 22 NOT NULL,
        age_max       INT DEFAULT 32 NOT NULL,
        seek          VARCHAR(16) DEFAULT 'male' NOT NULL,
        gender_mode   VARCHAR(16) DEFAULT 'auto' NOT NULL,
        photos_per_acc INT DEFAULT 1 NOT NULL,
        status        VARCHAR(32) DEFAULT 'pending' NOT NULL,
        total_items   INT DEFAULT 0 NOT NULL,
        done_items    INT DEFAULT 0 NOT NULL,
        failed_items  INT DEFAULT 0 NOT NULL,
        error         TEXT DEFAULT '' NOT NULL,
        created_at    TIMESTAMP DEFAULT NOW() NOT NULL,
        started_at    TIMESTAMP,
        finished_at   TIMESTAMP
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_leomatch_tasks_user ON leomatch_onboarding_tasks(user_id);",
    "CREATE INDEX IF NOT EXISTS ix_leomatch_tasks_created ON leomatch_onboarding_tasks(created_at DESC);",
    """
    CREATE TABLE IF NOT EXISTS leomatch_onboarding_items (
        id           SERIAL PRIMARY KEY,
        task_id      INT NOT NULL REFERENCES leomatch_onboarding_tasks(id) ON DELETE CASCADE,
        account_id   INT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        profile      JSONB DEFAULT '{}'::jsonb NOT NULL,
        status       VARCHAR(32) DEFAULT 'pending' NOT NULL,
        steps_log    JSONB DEFAULT '[]'::jsonb NOT NULL,
        error        TEXT DEFAULT '' NOT NULL,
        created_at   TIMESTAMP DEFAULT NOW() NOT NULL,
        started_at   TIMESTAMP,
        finished_at  TIMESTAMP
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_leomatch_items_task ON leomatch_onboarding_items(task_id);",
    "CREATE INDEX IF NOT EXISTS ix_leomatch_items_account ON leomatch_onboarding_items(account_id);",
    "CREATE INDEX IF NOT EXISTS ix_leomatch_items_status ON leomatch_onboarding_items(status);",
]

DOWN_SQL = [
    "DROP TABLE IF EXISTS leomatch_onboarding_items;",
    "DROP TABLE IF EXISTS leomatch_onboarding_tasks;",
]
