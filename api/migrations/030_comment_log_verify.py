"""Миграция 030: CommentLog + verify-поля и Campaign.campaign_type.

Что добавляется:

comment_logs:
  - discussion_peer_id  BIGINT   — id linked discussion group / чата куда
                                    попал коммент (для последующего
                                    get_messages без повторного GetFullChannel)
  - discussion_msg_id   BIGINT   — id самого сообщения в этой discussion.
                                    Может отличаться от post_id (post_id
                                    это id поста в основном канале).
  - verified_at         TIMESTAMP NULL — когда проверили
  - was_deleted         BOOLEAN  NULL — True/False/NULL (не проверяли)
  - verify_status       VARCHAR(32) DEFAULT ''  — 'ok' | 'deleted' |
                                    'flood' | 'not_found' | 'unknown'

campaigns:
  - campaign_type       VARCHAR(32) DEFAULT 'regular'
      * 'regular'      — обычная кампания комментинга (с автоматической
                         верификацией через plan_executor)
      * 'check_only'   — Screening-кампания: 1 коммент/канал, без time-limit
                         на max_hours, проверка удаления в следующих сессиях.
  - notification_chat   VARCHAR(128) — куда слать уведомления «твой коммент
                         удалили». Опционально. Формат: @channel или числовой
                         chat_id.

Индексы:
  - ix_comment_logs_verify (account_id, verified_at) — быстрый выбор
      непроверенных за окном.
"""

MIGRATION_ID = "030"
DESCRIPTION  = "CommentLog verify fields + Campaign.campaign_type/notification_chat"

UP_SQL = [
    # ── comment_logs ──
    """
    ALTER TABLE comment_logs
        ADD COLUMN IF NOT EXISTS discussion_peer_id BIGINT,
        ADD COLUMN IF NOT EXISTS discussion_msg_id  BIGINT,
        ADD COLUMN IF NOT EXISTS verified_at        TIMESTAMP,
        ADD COLUMN IF NOT EXISTS was_deleted        BOOLEAN,
        ADD COLUMN IF NOT EXISTS verify_status      VARCHAR(32) DEFAULT '';
    """,
    "CREATE INDEX IF NOT EXISTS ix_comment_logs_verify ON comment_logs(account_id, verified_at);",
    # ── campaigns ──
    """
    ALTER TABLE campaigns
        ADD COLUMN IF NOT EXISTS campaign_type      VARCHAR(32) DEFAULT 'regular',
        ADD COLUMN IF NOT EXISTS notification_chat  VARCHAR(128);
    """,
    "CREATE INDEX IF NOT EXISTS ix_campaigns_type ON campaigns(campaign_type);",
]

DOWN_SQL = [
    "DROP INDEX IF EXISTS ix_campaigns_type;",
    "ALTER TABLE campaigns DROP COLUMN IF EXISTS notification_chat;",
    "ALTER TABLE campaigns DROP COLUMN IF EXISTS campaign_type;",
    "DROP INDEX IF EXISTS ix_comment_logs_verify;",
    """
    ALTER TABLE comment_logs
        DROP COLUMN IF EXISTS verify_status,
        DROP COLUMN IF EXISTS was_deleted,
        DROP COLUMN IF EXISTS verified_at,
        DROP COLUMN IF EXISTS discussion_msg_id,
        DROP COLUMN IF EXISTS discussion_peer_id;
    """,
]
