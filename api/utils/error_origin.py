"""
utils/error_origin.py — откуда взялась ошибка и что она значит.

Расследование 2026-08-22 показало корневую проблему учёта: в
`warmup_logs.error` вперемешку лежали настоящие ответы Telegram
(`chat_write_forbidden`, `FLOOD_WAIT_60`) и наши собственные причины
пропуска (`llm_empty`, `no_posts`, `shadow_ban_no_visible_msg`).
Отличить их можно было только регэкспом по тексту, поэтому любой
счётчик «здоровья аккаунта» неминуемо наказывал бы аккаунт за то,
что у нас кончились ключи Groq или сломался наш собственный детектор.

Здесь два независимых измерения:

  origin   — КТО сказал: 'telegram' (исключение от Telethon) или
             'internal' (наше решение не отправлять).
  severity — ЧТО это значит, только для origin='telegram':
               'account' — Telegram предъявляет претензию АККАУНТУ,
                           единственное что влияет на его здоровье
               'channel' — проблема КАНАЛА, аккаунт не виноват
               'transient' — сеть/прокси/глюк, не виноват никто

Железное правило: в бюджет здоровья аккаунта попадает ТОЛЬКО
severity='account'. «LLM не ответила два раза» физически не может
отправить аккаунт отдыхать.
"""

# ── Наши внутренние коды пропуска ───────────────────────────
# Всё что система решила сама, не спрашивая Telegram.
INTERNAL_REASONS = {
    "llm_empty",
    # Детализированные причины отказа LLM (services/llm.py).
    # Все они — НАШИ проблемы: кончилась квота, снята модель, битый ключ,
    # сеть. Аккаунт к ним отношения не имеет и наказан быть не должен.
    "llm_rate_limit",
    "llm_model_404",
    "llm_bad_key",
    "llm_network",
    "llm_no_keys",
    "llm_truly_empty",
    "llm_local_limit",
    "llm_refusal",       # модель ответила отмазкой вместо коммента
    "post_no_text",      # у поста нет текста (фото/видео без подписи)
    "no_text_posts",     # во всём канале нет постов с текстом
    "disc_unresolved",   # не смогли резолвить discussion group
    "disc_join_refused", # прямой join в группу закрыт, комментить можно
    "acc_no_access",     # канал недоступен именно этому аккаунту
    "no_posts",
    "hard_guard_no_discussion",
    "shadow_ban_no_visible_msg",
    "not_subscribed",
    "acc_forbidden_cached",
    "invite_pending",
    "user_declined",
    "unknown",
    "cap_not_commentable",
    "cap_needs_approval",
    "cap_no_discussion",
    "cap_write_banned",
}

# ── Telegram: претензия к АККАУНТУ (единственное что влияет на здоровье) ──
_ACCOUNT_MARKERS = (
    "FLOOD_WAIT", "FLOODWAIT",
    # Текст самого Telethon: "A wait of 900 seconds is required".
    # Без этого маркера FloodWaitError с пустым error_code уезжал
    # в transient и отдых не ставился (поймано тестом 2026-08-22).
    "A WAIT OF",
    "PEER_FLOOD", "PEERFLOOD",
    "AUTH_KEY_UNREGISTERED",
    "USER_DEACTIVATED", "USERDEACTIVATED",
    "FROZEN_METHOD_INVALID", "NOT AVAILABLE FOR FROZEN ACCOUNTS",
    "SESSION_REVOKED", "SESSION_EXPIRED",
    "PHONE_NUMBER_BANNED",
    "USER_RESTRICTED",
)

# ── Telegram: проблема КАНАЛА (аккаунт ни при чём) ──────────
_CHANNEL_MARKERS = (
    "CHAT_WRITE_FORBIDDEN", "CHATWRITEFORBIDDEN",
    "CHAT_GUEST_SEND_FORBIDDEN", "CHATGUESTSEND",
    # "USER_BANNED" отдельно: наш короткий skip-код `user_banned` не
    # содержал полного "USER_BANNED_IN_CHANNEL" и уезжал в internal
    # (поймано тестом 2026-08-22). Бан в конкретном чате — свойство канала.
    "USER_BANNED", "USERBANNED",
    "USER_BANNED_IN_CHANNEL", "USERBANNEDINCHANNEL",
    "CHANNEL_PRIVATE", "CHANNELPRIVATE",
    "CHAT_RESTRICTED", "CHATRESTRICTED",
    "MSG_ID_INVALID", "MSGIDINVALID", "MESSAGE ID USED IN THE PEER",
    "ALLOW_PAYMENT_REQUIRED",
    "INVITE_REQUEST_SENT", "INVITEREQUESTSENT",
    "USERNAME_NOT_OCCUPIED", "USERNAMEINVALID", "USERNAME_INVALID",
    "CHAT_SEND_PLAIN_FORBIDDEN", "CHATSENDPLAIN",
    "PEER_ID_INVALID", "PEERIDINVALID",
    "REPLY_MESSAGE_ID_INVALID",
    "CHAT_ADMIN_REQUIRED",
    # Наши коды, но выведенные ПРЯМО из ответа Telegram (linked_chat_id == 0
    # и т.п.). Это настоящие свойства канала, их надо запоминать глобально.
    "NO_LINKED_CHAT", "NO_DISCUSSION_ACCESS",
)

# ── Сеть / прокси / временные глюки — не виноват никто ──────
_TRANSIENT_MARKERS = (
    "TIMEOUT", "TIMED OUT",
    "CONNECTIONERROR", "CONNECTION RESET", "CONNECTION REFUSED",
    "CONNECTION TO TELEGRAM FAILED",
    "CANNOT SEND REQUESTS WHILE DISCONNECTED",
    "SERVERERROR", "RPC CALL FAIL", "INTERNAL SERVER ERROR",
    "TYPENOTFOUND", "COULD NOT FIND A MATCHING CONSTRUCTOR",
    "INVALIDBUFFER", "SECURITYERROR", "SECURITYCHECK",
    "PROXY",
)


def classify_origin(error_code: str = "", exc: BaseException | None = None) -> str:
    """'telegram' если ошибку сказал Telegram, 'internal' если решили мы.

    Определяющий признак — НАЛИЧИЕ ИСКЛЮЧЕНИЯ. Если код вызвал нас со
    своим строковым skip-кодом и без exc — это наше решение, чем бы
    строка ни выглядела.
    """
    code = (error_code or "").strip()
    if exc is not None:
        return "telegram"
    if not code:
        return "internal"
    if code in INTERNAL_REASONS:
        return "internal"
    # Наши составные коды: "channel_bad: ChannelPrivateError",
    # "auto_join_fail: InviteRequestSentError" — это ПЕРЕСКАЗ ответа
    # Telegram нашими словами, источник всё-таки Telegram.
    blob = code.upper()
    if any(m in blob for m in _ACCOUNT_MARKERS + _CHANNEL_MARKERS + _TRANSIENT_MARKERS):
        return "telegram"
    return "internal"


def classify_severity(error_code: str = "", exc: BaseException | None = None) -> str:
    """'account' | 'channel' | 'transient' | 'none'.

    'none' для origin='internal' — такие ошибки не значат ничего
    ни для аккаунта, ни для канала, только для нас самих.
    """
    if classify_origin(error_code, exc) != "telegram":
        return "none"

    blob = (error_code or "")
    if exc is not None:
        blob += " " + type(exc).__name__ + " " + str(exc)
    blob = blob.upper()

    # FloodWait надёжнее опознаётся по атрибуту .seconds, чем по тексту:
    # формулировка Telethon менялась между версиями.
    if exc is not None and hasattr(exc, "seconds"):
        try:
            if int(exc.seconds) > 0:
                return "account"
        except Exception:
            pass

    # Порядок важен: transient проверяем ПЕРЕД account, иначе
    # "Connection to Telegram failed" утащит аккаунт в наказание.
    if any(m in blob for m in _TRANSIENT_MARKERS):
        return "transient"
    if any(m in blob for m in _ACCOUNT_MARKERS):
        return "account"
    if any(m in blob for m in _CHANNEL_MARKERS):
        return "channel"
    return "transient"      # неизвестное от Telegram — не наказываем


def parse_flood_wait(error_code: str = "", exc: BaseException | None = None) -> int:
    """Секунды из FLOOD_WAIT, иначе 0."""
    import re
    if exc is not None and hasattr(exc, "seconds"):
        try:
            return int(exc.seconds)
        except Exception:
            pass
    blob = (error_code or "") + " " + (str(exc) if exc else "")
    m = re.search(r"FLOOD_?WAIT[_ ]?(\d+)", blob, re.IGNORECASE)
    if m:
        return int(m.group(1))
    if "FLOOD" in blob.upper():
        m2 = re.search(r"(\d+)", blob)
        if m2:
            return int(m2.group(1))
    return 0
