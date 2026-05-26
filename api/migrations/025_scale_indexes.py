"""Миграция 025: композитные индексы на горячих таблицах для scale-up.

Зачем: при 500-1000 юзерах и 30-40 одновременных тасках диспетчеры
сканируют campaign_plans/campaigns каждые 60 сек, smart_comment guard
проверяет campaign_channel_assignments на КАЖДОМ комменте.
Без композитных индексов это секвенциальные сканы — деградация O(N).

С индексами — O(log N), даже при миллионе строк отклик миллисекунды.

ВАЖНО: CREATE INDEX IF NOT EXISTS — миграция идемпотентна, можно
запускать повторно. CONCURRENTLY не используем т.к. migrate.py
оборачивает в транзакцию (CONCURRENTLY несовместим с транзакцией).
"""

MIGRATION_ID = "025"
DESCRIPTION  = "Композитные индексы для scale (campaign_plans, assignments, logs, etc.)"

UP_SQL = [
    # ═══════════════════════════════════════════════════════════
    # 1. campaign_plans — диспетчер сканирует каждые 60с
    # ═══════════════════════════════════════════════════════════
    # Главный фильтр: WHERE plan_date = today AND status = 'active'
    "CREATE INDEX IF NOT EXISTS ix_cp_plandate_status ON campaign_plans(plan_date, status);",

    # Warmup-вариант диспетчера: + warmup_task_id != NULL
    "CREATE INDEX IF NOT EXISTS ix_cp_warmup_plandate_status ON campaign_plans(warmup_task_id, plan_date, status);",

    # Autoclose-чек кампании: WHERE campaign_id = X AND status = 'active'
    "CREATE INDEX IF NOT EXISTS ix_cp_campaign_status ON campaign_plans(campaign_id, status);",

    # ═══════════════════════════════════════════════════════════
    # 2. campaigns — autoclose проверяет каждые 60с
    # ═══════════════════════════════════════════════════════════
    # Фильтр: WHERE status = 'active'
    "CREATE INDEX IF NOT EXISTS ix_campaigns_status ON campaigns(status);",

    # ═══════════════════════════════════════════════════════════
    # 3. campaign_channel_assignments — smart_comment guard
    #    (фильтр НА КАЖДЫЙ комментарий, самый горячий путь)
    # ═══════════════════════════════════════════════════════════
    # Композитный для exact-match guard:
    # WHERE campaign_id=? AND account_id=? AND channel_username=? AND status='joined'
    "CREATE INDEX IF NOT EXISTS ix_cca_camp_acc_ch_status ON campaign_channel_assignments(campaign_id, account_id, channel_username, status);",

    # Для проверки "есть ли вообще assignments в этой кампании":
    "CREATE INDEX IF NOT EXISTS ix_cca_camp_channel ON campaign_channel_assignments(campaign_id, channel_username);",

    # ═══════════════════════════════════════════════════════════
    # 4. comment_logs — UI activity feed кампании
    # ═══════════════════════════════════════════════════════════
    # Запрос: WHERE campaign_id = X ORDER BY created_at DESC LIMIT N
    "CREATE INDEX IF NOT EXISTS ix_comment_logs_camp_created ON comment_logs(campaign_id, created_at DESC);",

    # Поиск по аккаунту (статистика по комментам)
    "CREATE INDEX IF NOT EXISTS ix_comment_logs_acc_created ON comment_logs(account_id, created_at DESC);",

    # ═══════════════════════════════════════════════════════════
    # 5. warmup_logs — UI activity для warmup И commenting
    # ═══════════════════════════════════════════════════════════
    # Warmup task feed:
    "CREATE INDEX IF NOT EXISTS ix_warmup_logs_task_created ON warmup_logs(task_id, created_at DESC);",

    # Commenting campaign feed (source='commenting' + campaign_id):
    "CREATE INDEX IF NOT EXISTS ix_warmup_logs_camp_created ON warmup_logs(campaign_id, created_at DESC) WHERE campaign_id IS NOT NULL;",

    # Account detail page feed:
    "CREATE INDEX IF NOT EXISTS ix_warmup_logs_acc_created ON warmup_logs(account_id, created_at DESC);",

    # ═══════════════════════════════════════════════════════════
    # 6. channel_ban_stats — whitelist UI
    # ═══════════════════════════════════════════════════════════
    # Запрос: WHERE user_id = X ORDER BY (total_attempts - banned_count) DESC
    # (pass_rate — property в Python, на стороне БД сортировка по total_attempts/banned_count)
    "CREATE INDEX IF NOT EXISTS ix_cbs_user_attempts ON channel_ban_stats(user_id, total_attempts);",

    # ═══════════════════════════════════════════════════════════
    # 7. parsed_channels — UI парсера + verify_comments task
    # ═══════════════════════════════════════════════════════════
    # Фильтр по has_comments (whitelist):
    "CREATE INDEX IF NOT EXISTS ix_parsed_user_hascomments ON parsed_channels(user_id, has_comments);",

    # Фильтр по language (language detector):
    "CREATE INDEX IF NOT EXISTS ix_parsed_user_language ON parsed_channels(user_id, language);",

    # Фильтр по folder (UI группировка):
    "CREATE INDEX IF NOT EXISTS ix_parsed_user_folder ON parsed_channels(user_id, folder);",

    # Verify-таск ищет каналы с last_verification старее N дней:
    "CREATE INDEX IF NOT EXISTS ix_parsed_user_lastverif ON parsed_channels(user_id, last_verification);",

    # ═══════════════════════════════════════════════════════════
    # 8. accounts — фильтры в UI
    # ═══════════════════════════════════════════════════════════
    # Список аккаунтов юзера с фильтром по status:
    "CREATE INDEX IF NOT EXISTS ix_accounts_user_status ON accounts(user_id, status);",
]

DOWN_SQL = [
    "DROP INDEX IF EXISTS ix_cp_plandate_status;",
    "DROP INDEX IF EXISTS ix_cp_warmup_plandate_status;",
    "DROP INDEX IF EXISTS ix_cp_campaign_status;",
    "DROP INDEX IF EXISTS ix_campaigns_status;",
    "DROP INDEX IF EXISTS ix_cca_camp_acc_ch_status;",
    "DROP INDEX IF EXISTS ix_cca_camp_channel;",
    "DROP INDEX IF EXISTS ix_comment_logs_camp_created;",
    "DROP INDEX IF EXISTS ix_comment_logs_acc_created;",
    "DROP INDEX IF EXISTS ix_warmup_logs_task_created;",
    "DROP INDEX IF EXISTS ix_warmup_logs_camp_created;",
    "DROP INDEX IF EXISTS ix_warmup_logs_acc_created;",
    "DROP INDEX IF EXISTS ix_cbs_user_attempts;",
    "DROP INDEX IF EXISTS ix_parsed_user_hascomments;",
    "DROP INDEX IF EXISTS ix_parsed_user_language;",
    "DROP INDEX IF EXISTS ix_parsed_user_folder;",
    "DROP INDEX IF EXISTS ix_parsed_user_lastverif;",
    "DROP INDEX IF EXISTS ix_accounts_user_status;",
]
