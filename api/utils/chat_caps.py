"""
utils/chat_caps.py — кэш свойств ЧАТА (не канала).

Зачем отдельно от channel_caps: у канала вопрос «есть ли привязанная группа
обсуждений», у чата — «пустят ли туда писать». Это разные проверки и разные
поля ответа, смешивать их в одной таблице значило бы городить NULL-ы.

Стоимость проверки та же, что у канала: два read-only запроса,
get_entity + GetFullChannelRequest. Ни вступления, ни отправки сообщения.

Что выясняем:
  is_megagroup   — супергруппа. В обычную группу (не мегагруппу) Telegram
                   не пускает по username, туда только по приглашению.
  needs_approval — вступление по заявке (join_request). Заявку никто не
                   одобрит, такой чат бесполезен.
  write_banned   — обычному участнику запрещено писать
                   (default_banned_rights.send_messages). Чат только для чтения.
  slowmode_sec   — задержка между сообщениями. При большом значении аккаунт
                   успеет одну реплику и будет ждать, разговора не выйдет.
  members        — участников. Совсем маленькие чаты бессмысленны: там
                   новое лицо слишком заметно.

Свойства чата от аккаунта не зависят, поэтому знание общее для всего парка
и выясняется один раз — как и с каналами.
"""

import os
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Свежесть вердикта: чат могли закрыть или открыть
CAP_TTL_DAYS = int(os.getenv("CHAT_CAP_TTL_DAYS", "14"))

# Слоумод, выше которого чат для нас бесполезен: аккаунт успеет одну реплику
# за сессию и уйдёт ждать. Разговора не получится.
MAX_SLOWMODE_SEC = int(os.getenv("CHAT_MAX_SLOWMODE_SEC", "120"))

# Минимум участников. В чате на два десятка человек новичок, сразу пишущий
# со ссылкой в профиле, слишком заметен.
MIN_MEMBERS = int(os.getenv("CHAT_MIN_MEMBERS", "300"))


def _clean(username: str) -> str:
    return (username or "").lstrip("@").strip()


def extract_caps_from_full(full, entity=None) -> dict:
    """Достаёт свойства чата из ответа GetFullChannelRequest."""
    out = {
        "chat_id": None,
        "is_megagroup": None,
        "needs_approval": None,
        "write_banned": None,
        "slowmode_sec": None,
        "members": None,
    }
    ent = entity
    if ent is None:
        # берём сам чат из full.chats — он там всегда первый
        chats = getattr(full, "chats", None) or []
        ent = chats[0] if chats else None
    if ent is not None:
        out["chat_id"] = getattr(ent, "id", None)
        out["is_megagroup"] = bool(getattr(ent, "megagroup", False))
        out["needs_approval"] = bool(getattr(ent, "join_request", False))
        dbr = getattr(ent, "default_banned_rights", None)
        out["write_banned"] = bool(getattr(dbr, "send_messages", False)) if dbr else False
    fc = getattr(full, "full_chat", None)
    if fc is not None:
        out["members"] = getattr(fc, "participants_count", None)
        out["slowmode_sec"] = getattr(fc, "slowmode_seconds", None) or 0
    return out


def verdict(caps: dict) -> tuple[bool, str]:
    """(writable, reason) — стоит ли вообще пытаться разговаривать."""
    if caps.get("is_megagroup") is False:
        return False, "not_megagroup"
    if caps.get("needs_approval"):
        return False, "needs_approval"
    if caps.get("write_banned"):
        return False, "write_banned"
    sm = caps.get("slowmode_sec") or 0
    if sm > MAX_SLOWMODE_SEC:
        return False, f"slowmode_{sm}"
    m = caps.get("members")
    if m is not None and m < MIN_MEMBERS:
        return False, "too_small"
    return True, "ok"


async def get_cap(db, username: str):
    """Строка chat_capabilities или None. Протухшие (>CAP_TTL_DAYS) — как None."""
    from sqlalchemy import text
    clean = _clean(username)
    if not clean:
        return None
    try:
        row = (await db.execute(text("""
            SELECT * FROM chat_capabilities WHERE chat_username = :u
        """), {"u": clean})).mappings().first()
        if row is None:
            return None
        ca = row.get("checked_at")
        if ca and (datetime.utcnow() - ca) > timedelta(days=CAP_TTL_DAYS):
            return None
        return row
    except Exception as e:
        logger.debug(f"[chat_caps] get fail @{clean}: {e}")
        return None


async def record_caps(db, username: str, caps: dict, account_id: int = None,
                      source: str = "probe", error: str = None):
    """UPSERT свойств чата. Никогда не роняет вызывающий код."""
    from sqlalchemy import text
    clean = _clean(username)
    if not clean:
        return None
    ok, why = verdict(caps)
    try:
        await db.execute(text("""
            INSERT INTO chat_capabilities
                (chat_username, chat_id, is_megagroup, needs_approval, write_banned,
                 slowmode_sec, members, writable, reason, checked_at,
                 checked_by_account, probe_source, fail_count, last_error)
            VALUES (:u, :cid, :mg, :na, :wb, :sm, :mem, :w, :r, (NOW() AT TIME ZONE 'UTC'), :acc, :src,
                    CASE WHEN CAST(:err AS VARCHAR) IS NULL THEN 0 ELSE 1 END, CAST(:err AS VARCHAR))
            ON CONFLICT (chat_username) DO UPDATE SET
                chat_id        = COALESCE(EXCLUDED.chat_id, chat_capabilities.chat_id),
                is_megagroup   = COALESCE(EXCLUDED.is_megagroup, chat_capabilities.is_megagroup),
                needs_approval = COALESCE(EXCLUDED.needs_approval, chat_capabilities.needs_approval),
                write_banned   = COALESCE(EXCLUDED.write_banned, chat_capabilities.write_banned),
                slowmode_sec   = COALESCE(EXCLUDED.slowmode_sec, chat_capabilities.slowmode_sec),
                members        = COALESCE(EXCLUDED.members, chat_capabilities.members),
                writable       = EXCLUDED.writable,
                reason         = EXCLUDED.reason,
                checked_at     = (NOW() AT TIME ZONE 'UTC'),
                checked_by_account = EXCLUDED.checked_by_account,
                probe_source   = EXCLUDED.probe_source,
                fail_count     = chat_capabilities.fail_count
                                 + CASE WHEN CAST(:err AS VARCHAR) IS NULL THEN 0 ELSE 1 END,
                last_error     = COALESCE(EXCLUDED.last_error, chat_capabilities.last_error)
        """), {
            "u": clean, "cid": caps.get("chat_id"),
            "mg": caps.get("is_megagroup"), "na": caps.get("needs_approval"),
            "wb": caps.get("write_banned"), "sm": caps.get("slowmode_sec"),
            "mem": caps.get("members"), "w": ok, "r": why,
            "acc": account_id, "src": source,
            "err": (str(error)[:200] if error else None),
        })
        await db.flush()
        return {"writable": ok, "reason": why}
    except Exception as e:
        logger.debug(f"[chat_caps] record fail @{clean}: {e}")
        return None


async def probe_chat(client, db, username: str, account_id: int = None) -> dict:
    """Два read-only запроса: узнать свойства чата и записать их.

    FloodWaitError НЕ ловим — это претензия Telegram к аккаунту, её должен
    увидеть вызывающий. Списывать флуд на «плохой чат» нельзя: ровно на этом
    в августе накопилось 355 ложных отметок по каналам.
    """
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.errors import FloodWaitError

    clean = _clean(username)
    try:
        ent = await client.get_entity(clean)
        full = await client(GetFullChannelRequest(ent))
    except FloodWaitError:
        raise
    except Exception as e:
        # Чат недоступен — записываем как непригодный с явной причиной
        await record_caps(db, clean, {"is_megagroup": None}, account_id=account_id,
                          source="probe", error=f"{type(e).__name__}")
        return {"writable": False, "reason": f"probe_{type(e).__name__}"}

    caps = extract_caps_from_full(full, ent)
    res = await record_caps(db, clean, caps, account_id=account_id, source="probe")
    return res or {"writable": None, "reason": "record_failed"}
