"""
utils/chat_talk.py — выбор реплики и сборка контекста для разговора в чате.

Отделено от plan_executor намеренно: здесь чистые функции без сети и без БД,
их можно прогонять на сохранённых сообщениях и проверять пороги, не трогая
живые аккаунты.

Модель целиком описана в спецификации; здесь реализованы этапы 6-8:
на что отвечать, что показать модели и что не выпускать наружу.
"""

import os
import re
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ── Пороги отбора кандидата ──────────────────────────────────
# Короче этого отвечать не на что: «ага», «+», «спасибо».
MIN_TEXT_LEN = int(os.getenv("CHAT_MIN_TEXT_LEN", "15"))
# Свежее этого — мгновенный ответ, выглядит как дежурство у экрана.
MIN_AGE_SEC = int(os.getenv("CHAT_MIN_AGE_SEC", "120"))
# Старше этого — разговор уже ушёл дальше.
MAX_AGE_SEC = int(os.getenv("CHAT_MAX_AGE_SEC", str(6 * 3600)))
# Сколько предыдущих сообщений показать модели как нить разговора.
CONTEXT_DEPTH = int(os.getenv("CHAT_CONTEXT_DEPTH", "6"))
# Потолок длины нашего ответа. Простыня в чате видна сразу.
MAX_REPLY_LEN = int(os.getenv("CHAT_MAX_REPLY_LEN", "220"))

_URL_RE = re.compile(r"(https?://|t\.me/|@[A-Za-z]\w{3,})")


def _text_of(m) -> str:
    return (getattr(m, "message", None) or "").strip()


def _is_from_bot(m) -> bool:
    s = getattr(m, "sender", None)
    if s is not None and getattr(s, "bot", False):
        return True
    # сообщение от имени канала, а не человека
    if getattr(m, "post", False):
        return True
    fr = getattr(m, "from_id", None)
    return fr is not None and type(fr).__name__ == "PeerChannel"


def pick_reply_target(messages, my_user_id=None, already_replied=None, now=None):
    """Выбрать сообщение, на которое отвечаем.

    Args:
        messages: список сообщений, новые первыми (как отдаёт get_messages)
        my_user_id: наш id — свои сообщения не трогаем
        already_replied: set id, на которые мы уже отвечали
        now: для тестов

    Returns:
        (message, reason). message=None если подходящего нет,
        тогда reason объясняет почему.
    """
    now = now or datetime.utcnow()
    already_replied = already_replied or set()
    rejected = {}

    def rej(key):
        rejected[key] = rejected.get(key, 0) + 1

    cands = []
    for m in messages:
        txt = _text_of(m)
        if not txt:
            rej("без текста"); continue
        if len(txt) < MIN_TEXT_LEN:
            rej("слишком коротко"); continue
        if getattr(m, "action", None) is not None:
            rej("служебное"); continue
        if _is_from_bot(m):
            rej("от бота или канала"); continue
        sid = getattr(getattr(m, "from_id", None), "user_id", None)
        if my_user_id and sid == my_user_id:
            rej("наше"); continue
        if m.id in already_replied:
            rej("уже отвечали"); continue
        if getattr(m, "fwd_from", None) is not None:
            rej("переслано"); continue
        if _URL_RE.search(txt):
            rej("со ссылкой"); continue
        d = getattr(m, "date", None)
        if d is not None:
            age = (now - d.replace(tzinfo=None)).total_seconds()
            if age < MIN_AGE_SEC:
                rej("слишком свежее"); continue
            if age > MAX_AGE_SEC:
                rej("слишком старое"); continue
        cands.append(m)

    if not cands:
        detail = ", ".join(f"{k}: {v}" for k, v in sorted(rejected.items(), key=lambda x: -x[1]))
        return None, (detail or "нет сообщений")

    # Приоритет: вопрос, затем без ответов, затем свежее.
    def score(m):
        t = _text_of(m)
        s = 0
        if t.rstrip().endswith("?"):
            s += 100
        reps = getattr(m, "replies", None)
        if reps is None or not getattr(reps, "replies", 0):
            s += 40
        d = getattr(m, "date", None)
        if d is not None:
            age_h = (now - d.replace(tzinfo=None)).total_seconds() / 3600
            s += max(0, 20 - age_h * 3)
        return s

    best = max(cands, key=score)
    why = []
    if _text_of(best).rstrip().endswith("?"):
        why.append("вопрос")
    reps = getattr(best, "replies", None)
    if reps is None or not getattr(reps, "replies", 0):
        why.append("без ответов")
    return best, ("выбрано: " + ", ".join(why) if why else "выбрано")


def build_dialog_context(messages, target, depth=None):
    """Нить разговора вокруг выбранного сообщения.

    Возвращает список (имя, текст) в хронологическом порядке — то, что
    увидит модель. Без этого ответ попадает в слова, но мимо темы.
    """
    depth = depth or CONTEXT_DEPTH
    ordered = sorted(messages, key=lambda m: m.id)
    idx = next((i for i, m in enumerate(ordered) if m.id == target.id), None)
    if idx is None:
        return []
    start = max(0, idx - depth)
    out = []
    for m in ordered[start:idx + 1]:
        t = _text_of(m)
        if not t:
            continue
        s = getattr(m, "sender", None)
        name = (getattr(s, "first_name", None) or getattr(s, "username", None) or "участник")
        out.append((name, t))
    return out


def build_chat_prompt(context, style=None, personality=None) -> str:
    """Системный промпт для ответа в чате."""
    style_name = (style or {}).get("name", "thinker")
    tone = {
        "short_responder": "Отвечай очень коротко, одной фразой.",
        "thinker": "Отвечай по существу, спокойно, 1-2 предложения.",
        "questioner": "Ответь коротко и, если уместно, задай встречный вопрос.",
    }.get(style_name, "Отвечай коротко и по существу.")

    lines = "\n".join(f"{n}: {t}" for n, t in context)
    return (
        "Ты обычный участник группового чата в Telegram. Ниже кусок переписки, "
        "последнее сообщение — то, на которое надо ответить.\n\n"
        f"{lines}\n\n"
        f"{tone} Пиши на том же языке, что и собеседник. "
        "Без приветствий, без представлений, без ссылок и без рекламы. "
        "Не повторяй чужие слова, отвечай именно на последнее сообщение. "
        "Только текст ответа, ничего больше."
    )


def check_reply(text: str) -> tuple[bool, str]:
    """Последний барьер перед отправкой. (ok, reason)"""
    t = (text or "").strip()
    if not t:
        return False, "chat_empty_reply"
    if len(t) > MAX_REPLY_LEN:
        return False, "chat_reply_too_long"
    if _URL_RE.search(t):
        # Ссылку несёт профиль, а не реплика. Реклама незнакомым в чате —
        # ровно то, за что Telegram выдаёт ограничение аккаунту.
        return False, "chat_reply_has_link"
    return True, "ok"
