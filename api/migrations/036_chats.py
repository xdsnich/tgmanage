"""Миграция 036: отдельные чаты как самостоятельная сущность.

До сих пор в `parsed_channels` лежали только каналы, и различить их было
не с чем. Для разговоров в чатах нужна другая сущность: у чата нет постов,
зато есть поток сообщений, и пригодность определяется иначе — не наличием
linked-группы, а тем, пускают ли туда писать.

Что добавляется:

  parsed_channels.kind — 'channel' | 'chat'. Дефолт 'channel', потому что
  всё, что там уже лежит (8885 записей на 26.08), — каналы.

  chat_capabilities — кэш свойств чата, по образцу channel_capabilities.
  Свойства чата от аккаунта не зависят, поэтому знание общее для всего парка
  и выясняется один раз:
    is_megagroup    — супергруппа (в обычные группы Telegram писать не даёт
                      без приглашения)
    needs_approval  — вступление по заявке (join_request)
    write_banned    — обычному участнику запрещено писать
                      (default_banned_rights.send_messages)
    slowmode_sec    — задержка между сообщениями; при большом значении чат
                      бесполезен, аккаунт успеет одно сообщение за сессию
    members         — сколько участников
  Вердикт `writable` считается один раз и дальше читается планировщиком.

  chat_messages — что мы написали и на что отвечали. Нужно для двух вещей:
  продолжения разговора в следующей сессии (кто ответил на нашу реплику) и
  проверки, не удалили ли написанное — та же схема, что с comment_logs.
"""

MIGRATION_ID = "036"
DESCRIPTION  = "чаты: parsed_channels.kind, chat_capabilities, chat_messages"

UP_SQL = [
    """
    ALTER TABLE parsed_channels
        ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT 'channel';
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_parsed_channels_kind
        ON parsed_channels (kind);
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_capabilities (
        id              SERIAL PRIMARY KEY,
        chat_username   VARCHAR(128) NOT NULL UNIQUE,
        chat_id         BIGINT,
        is_megagroup    BOOLEAN,
        needs_approval  BOOLEAN,
        write_banned    BOOLEAN,
        slowmode_sec    INTEGER,
        members         INTEGER,
        writable        BOOLEAN,
        reason          VARCHAR(64),
        checked_at      TIMESTAMP,
        checked_by_account INTEGER,
        probe_source    VARCHAR(32),
        fail_count      INTEGER DEFAULT 0,
        last_error      VARCHAR(200),
        created_at      TIMESTAMP DEFAULT NOW()
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_chat_caps_writable
        ON chat_capabilities (writable);
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id              SERIAL PRIMARY KEY,
        campaign_id     INTEGER,
        account_id      INTEGER NOT NULL,
        account_phone   VARCHAR(32),
        chat_username   VARCHAR(128) NOT NULL,
        chat_peer_id    BIGINT,
        -- на какое сообщение отвечали и что написали сами
        reply_to_msg_id BIGINT,
        reply_to_text   TEXT,
        reply_to_user   BIGINT,
        my_msg_id       BIGINT,
        my_text         TEXT,
        llm_provider    VARCHAR(32),
        -- продолжение разговора: ответил ли кто-то нам
        got_reply       BOOLEAN DEFAULT FALSE,
        got_reply_at    TIMESTAMP,
        followed_up     BOOLEAN DEFAULT FALSE,
        -- дожило ли написанное
        verified_at     TIMESTAMP,
        was_deleted     BOOLEAN DEFAULT FALSE,
        verify_status   VARCHAR(32),
        created_at      TIMESTAMP DEFAULT NOW()
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_chat_messages_acc
        ON chat_messages (account_id, created_at);
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_chat_messages_followup
        ON chat_messages (followed_up, got_reply);
    """,
]

DOWN_SQL = [
    "DROP TABLE IF EXISTS chat_messages;",
    "DROP TABLE IF EXISTS chat_capabilities;",
    "DROP INDEX IF EXISTS ix_parsed_channels_kind;",
    "ALTER TABLE parsed_channels DROP COLUMN IF EXISTS kind;",
]
