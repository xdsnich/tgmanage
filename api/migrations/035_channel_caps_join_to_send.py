"""Миграция 035: channel_capabilities.join_to_send.

Флаг discussion-группы `join_to_send`: надо ли вступить в неё ПЕРЕД первым
комментарием. Приходит в том же ответе GetFullChannelRequest, что и всё
остальное — дополнительных запросов ноль.

Канал с join_to_send=true остаётся ПРИГОДНЫМ: исполнитель умеет вступать
автоматически (обработчик ChatGuestSendForbidden в plan_executor). Но знать
об этом заранее полезно — можно вступить спокойно, отдельным действием
сессии, а не ловить отказ на отправке и не тратить попытку.

Замер 2026-08-22 на 10 пригодных каналах: 9 разрешают писать сразу,
1 требует вступления (10%).
"""

MIGRATION_ID = "035"
DESCRIPTION  = "channel_capabilities.join_to_send"

UP_SQL = [
    """
    ALTER TABLE channel_capabilities
        ADD COLUMN IF NOT EXISTS join_to_send BOOLEAN;
    """,
]

DOWN_SQL = [
    "ALTER TABLE channel_capabilities DROP COLUMN IF EXISTS join_to_send;",
]
