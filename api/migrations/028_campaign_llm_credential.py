"""Миграция 028: Campaign.llm_credential_id — выбор конкретного LLM-ключа.

Раньше комментинг брал default-ключ выбранного провайдера. Теперь можно
указать конкретный ServiceCredential.id, если у пользователя несколько
ключей одного провайдера (например 2 Claude — основной и резерв).
"""

MIGRATION_ID = "028"
DESCRIPTION  = "Campaign.llm_credential_id — конкретный LLM-ключ для кампании"

UP_SQL = [
    """
    ALTER TABLE campaigns
        ADD COLUMN IF NOT EXISTS llm_credential_id INTEGER;
    """,
    "CREATE INDEX IF NOT EXISTS ix_campaigns_llm_credential ON campaigns(llm_credential_id);",
]

DOWN_SQL = [
    "DROP INDEX IF EXISTS ix_campaigns_llm_credential;",
    "ALTER TABLE campaigns DROP COLUMN IF EXISTS llm_credential_id;",
]
