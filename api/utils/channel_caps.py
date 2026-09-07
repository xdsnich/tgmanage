"""
utils/channel_caps.py — работа с глобальным кэшем свойств канала.

Два способа наполнения, оба дешёвые:

  1. PASSIVE (даром). Во время обычной сессии аккаунт и так зовёт
     GetFullChannelRequest перед комментом. Просто записываем что узнали —
     ноль дополнительных запросов.

  2. ACTIVE (2 запроса на канал). Для каналов, куда ещё никто не заходил.
     get_entity + GetFullChannelRequest, без вступления и без отправки.
     Замер 2026-08-22: 30 каналов, 0 FLOOD_WAIT, 0.2с на канал.
     4823 канала / 141 аккаунт = 34 канала на аккаунт ≈ минута работы.

Вердикт `commentable` считается один раз и дальше просто читается планировщиком,
чтобы мёртвые каналы вообще не попадали в планы.
"""

import os
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Свежесть вердикта: канал мог открыть/закрыть обсуждения
CAP_TTL_DAYS = 14


def _clean(username: str) -> str:
    return (username or "").lstrip("@").strip()


def entity_username(chat) -> str:
    """Юзернейм канала с учётом мульти-юзернеймов.

    ── 2026-08-22 ───────────────────────────────────────────────
    У части каналов поле `username` пустое, а имя лежит в списке
    `usernames` (фича Telegram с несколькими адресами). Раньше такие
    каналы уходили в лог как «@: отсеян» и НЕ попадали в кэш свойств —
    то есть проверка для них пропадала впустую и повторялась снова.
    """
    if chat is None:
        return ""
    u = getattr(chat, "username", None)
    if u:
        return _clean(u)
    for item in (getattr(chat, "usernames", None) or []):
        cand = getattr(item, "username", None)
        if cand:
            return _clean(cand)
    return ""


def extract_caps_from_full(full, entity=None) -> dict:
    """Достаёт свойства из ответа GetFullChannelRequest.

    ВАЖНО: linked-чат приходит прямо в `full.chats`, вместе с флагами
    join_request и default_banned_rights — вступать НЕ нужно.
    """
    out = {
        "has_discussion": False,
        "discussion_id": None,
        "needs_approval": None,
        "write_banned": None,
        "join_to_send": None,
    }
    try:
        linked = getattr(full.full_chat, "linked_chat_id", 0) or 0
    except Exception:
        linked = 0
    if not linked:
        return out

    out["has_discussion"] = True
    out["discussion_id"] = int(linked)

    disc_obj = None
    for cobj in (getattr(full, "chats", None) or []):
        if getattr(cobj, "id", None) == linked:
            disc_obj = cobj
            break
    if disc_obj is None:
        return out

    # Вступление только по заявке
    out["needs_approval"] = bool(getattr(disc_obj, "join_request", False))
    # Обычному участнику запрещено писать
    dbr = getattr(disc_obj, "default_banned_rights", None)
    out["write_banned"] = bool(getattr(dbr, "send_messages", False)) if dbr else False
    # Надо ли вступить в обсуждение ПЕРЕД первым комментом.
    # НЕ делает канал непригодным — исполнитель вступает сам, — но зная
    # это заранее, вступление можно сделать спокойным отдельным действием,
    # а не ловить ChatGuestSendForbidden на отправке.
    out["join_to_send"] = bool(getattr(disc_obj, "join_to_send", False))
    return out


def verdict(caps: dict) -> tuple[bool, str]:
    """(commentable, reason) — стоит ли вообще пытаться."""
    if not caps.get("has_discussion"):
        return False, "no_discussion"
    if caps.get("needs_approval"):
        return False, "needs_approval"
    if caps.get("write_banned"):
        return False, "write_banned"
    return True, "ok"


async def get_cap(db, username: str):
    """Возвращает ChannelCapability или None. Устаревшие (>CAP_TTL_DAYS) — как None."""
    from sqlalchemy import select
    from models.channel_capability import ChannelCapability
    clean = _clean(username)
    if not clean:
        return None
    try:
        row = (await db.execute(
            select(ChannelCapability).where(ChannelCapability.channel_username == clean)
        )).scalar_one_or_none()
        if row is None:
            return None
        if row.checked_at and (datetime.utcnow() - row.checked_at) > timedelta(days=CAP_TTL_DAYS):
            return None      # протухло, перепроверить
        return row
    except Exception as e:
        logger.debug(f"[caps] get fail @{clean}: {e}")
        return None


async def record_caps(db, username: str, caps: dict, account_id: int = None,
                      source: str = "observed", error: str = None,
                      reason_override: str = None):
    """UPSERT свойств канала. Никогда не роняет вызывающий код.

    Раньше здесь был SELECT, затем создание строки при её отсутствии. Между
    этими шагами вклинивался другой исполнитель, и вставка падала на
    уникальном ключе — а вместе с ней разваливалась вся транзакция сессии
    (PendingRollbackError), и аккаунт терял остаток прогона.
    Инцидент 26.08: четыре карантинных аккаунта проверяли один и тот же
    список каналов. Порядок раздачи исправлен отдельно, но и сама запись
    теперь атомарна — одним оператором, без окна для гонки.
    """
    # Время пишем явным UTC. Колонка timestamp-without-tz, а часовой пояс
    # базы — Europe/Kiev, поэтому голый NOW() кладёт туда местное время,
    # тогда как ORM-пути в проекте пишут datetime.utcnow(). Столбец
    # оказывался смесью двух шкал с разбегом в три часа, и любая выборка
    # «что проверено за последние N часов» врала.
    from sqlalchemy import text
    clean = _clean(username)
    if not clean:
        return None
    ok, why = verdict({
        "has_discussion": caps.get("has_discussion"),
        "needs_approval": caps.get("needs_approval"),
        "write_banned": caps.get("write_banned"),
    })
    # Когда канал вообще не открылся, has_discussion=False и вердикт
    # получался «нет обсуждений» — это враньё: обсуждения там, возможно,
    # есть, просто канал стал приватным или исчез. Причину подменяем на
    # настоящую, иначе потом не понять, что за каналы отсеялись.
    if reason_override:
        ok, why = False, reason_override[:64]
    try:
        await db.execute(text("""
            INSERT INTO channel_capabilities
                (channel_username, has_discussion, discussion_id, needs_approval,
                 write_banned, join_to_send, commentable, reason, checked_at,
                 checked_by_account, probe_source, fail_count, last_error, created_at)
            VALUES (:u, :hd, :did, :na, :wb, :jts, :ok, :why, (NOW() AT TIME ZONE 'UTC'), :acc, :src,
                    CASE WHEN CAST(:err AS VARCHAR) IS NULL THEN 0 ELSE 1 END, CAST(:err AS VARCHAR), (NOW() AT TIME ZONE 'UTC'))
            ON CONFLICT (channel_username) DO UPDATE SET
                has_discussion = COALESCE(EXCLUDED.has_discussion, channel_capabilities.has_discussion),
                discussion_id  = COALESCE(EXCLUDED.discussion_id,  channel_capabilities.discussion_id),
                needs_approval = COALESCE(EXCLUDED.needs_approval, channel_capabilities.needs_approval),
                write_banned   = COALESCE(EXCLUDED.write_banned,   channel_capabilities.write_banned),
                join_to_send   = COALESCE(EXCLUDED.join_to_send,   channel_capabilities.join_to_send),
                commentable    = EXCLUDED.commentable,
                reason         = EXCLUDED.reason,
                checked_at     = (NOW() AT TIME ZONE 'UTC'),
                checked_by_account = EXCLUDED.checked_by_account,
                probe_source   = EXCLUDED.probe_source,
                fail_count     = channel_capabilities.fail_count
                                 + CASE WHEN CAST(:err AS VARCHAR) IS NULL THEN 0 ELSE 1 END,
                last_error     = COALESCE(EXCLUDED.last_error, channel_capabilities.last_error)
        """), {
            "u": clean,
            "hd": caps.get("has_discussion"), "did": caps.get("discussion_id"),
            "na": caps.get("needs_approval"), "wb": caps.get("write_banned"),
            "jts": caps.get("join_to_send"), "ok": ok, "why": why,
            "acc": account_id, "src": source,
            "err": (str(error)[:200] if error else None),
        })
        await db.flush()
        return {"commentable": ok, "reason": why}
    except Exception as e:
        logger.debug(f"[caps] record fail @{clean}: {e}")
        return None


# Отказы, которые МОГУТ быть персональными: аккаунт замьючен/ограничен
# именно в этом чате, а для остальных канал нормальный.
# Аудит 2026-08-22: из 7 каналов с историей chat_write_forbidden ЧЕТЫРЕ
# по факту оказались полностью пригодны (has_discussion, без заявки, без
# запрета). Пометить канал плохим по одному такому отказу — значит
# закрыть нормальный канал для всех 141 аккаунта.
_PER_ACCOUNT_AMBIGUOUS = (
    "chat_write_forbidden", "chatwriteforbidden",
    "user_banned", "userbanned", "userbannedinchannel",
    "channelprivate", "channelprivateerror",
)
# Сколько РАЗНЫХ аккаунтов должны наткнуться, чтобы признать проблему общей
CONFIRM_ACCOUNTS = 2


def _is_ambiguous(reason: str) -> bool:
    r = (reason or "").lower()
    return any(m in r for m in _PER_ACCOUNT_AMBIGUOUS)


def _confirm_count(username: str, reason: str, account_id) -> int:
    """Копит аккаунты, наткнувшиеся на неоднозначный отказ. Возвращает их число."""
    if account_id is None:
        return CONFIRM_ACCOUNTS      # нет id — считаем подтверждённым
    try:
        from utils.redis_pool import get_redis
        r = get_redis()
        k = f"gramgpt:cap_confirm:{_clean(username)}:{(reason or '')[:24]}"
        r.sadd(k, int(account_id))
        r.expire(k, 14 * 86400)
        return int(r.scard(k))
    except Exception:
        return CONFIRM_ACCOUNTS


async def mark_bad(db, username: str, reason: str, account_id: int = None,
                   error: str = None):
    """Пометить канал непригодным по факту наблюдения (не по проверке).

    Однозначные свойства канала (нет linked-чата) помечаются сразу.
    Неоднозначные (chat_write_forbidden, user_banned, ChannelPrivate) —
    только после того как на них наткнулись CONFIRM_ACCOUNTS разных
    аккаунтов: поодиночке это чаще персональное ограничение, а не
    свойство канала.

    Запись атомарна по той же причине, что и в record_caps: здесь был
    SELECT, затем db.add() при отсутствии строки, и между ними успевал
    вклиниться другой исполнитель. Вставка падала на уникальном ключе,
    а с ней разваливалась вся транзакция сессии — 26.08 так оборвался
    прогон проверки каналов на @taghirrotbee.
    """
    from sqlalchemy import text
    clean = _clean(username)
    if not clean:
        return
    try:
        if _is_ambiguous(reason):
            n = _confirm_count(clean, reason, account_id)
            if n < CONFIRM_ACCOUNTS:
                # Пока только один аккаунт — копим счётчик, канал НЕ хороним.
                await db.execute(text("""
                    INSERT INTO channel_capabilities
                        (channel_username, fail_count, last_error, created_at)
                    VALUES (:u, 1, CAST(:err AS VARCHAR), (NOW() AT TIME ZONE 'UTC'))
                    ON CONFLICT (channel_username) DO UPDATE SET
                        fail_count = channel_capabilities.fail_count + 1,
                        last_error = COALESCE(EXCLUDED.last_error, channel_capabilities.last_error)
                """), {"u": clean, "err": (str(error)[:200] if error else reason[:200])})
                await db.flush()
                logger.info(
                    f"[caps] @{clean}: {reason} от 1 аккаунта — возможно личное "
                    f"ограничение, канал пока НЕ помечаем (нужно {CONFIRM_ACCOUNTS})"
                )
                return
            logger.info(f"[caps] @{clean}: {reason} подтверждён {n} аккаунтами → помечаем")

        await db.execute(text("""
            INSERT INTO channel_capabilities
                (channel_username, commentable, reason, checked_at,
                 checked_by_account, probe_source, fail_count, last_error, created_at)
            VALUES (:u, false, :r, (NOW() AT TIME ZONE 'UTC'), :acc, 'observed', 1,
                    CAST(:err AS VARCHAR), (NOW() AT TIME ZONE 'UTC'))
            ON CONFLICT (channel_username) DO UPDATE SET
                commentable        = false,
                reason             = EXCLUDED.reason,
                checked_at         = (NOW() AT TIME ZONE 'UTC'),
                checked_by_account = EXCLUDED.checked_by_account,
                probe_source       = 'observed',
                fail_count         = channel_capabilities.fail_count + 1,
                last_error         = COALESCE(EXCLUDED.last_error, channel_capabilities.last_error)
        """), {
            "u": clean, "r": reason[:64], "acc": account_id,
            "err": (str(error)[:200] if error else None),
        })
        await db.flush()
        logger.info(f"[caps] @{clean} → не комментируем ({reason})")
    except Exception as e:
        logger.debug(f"[caps] mark_bad fail @{clean}: {e}")


async def check_and_record(client, chat, username: str = None,
                           account_id: int = None, source: str = "parser") -> tuple:
    """Для ПАРСЕРОВ: один GetFullChannelRequest → и has_comments, и свойства.

    Парсеры и так звали GetFullChannel ради linked_chat_id. Из того же
    ответа бесплатно достаются join_request и default_banned_rights, так
    что каналы «только по заявке» и «писать нельзя» отсеиваются сразу на
    парсинге, а не когда в них по очереди упрётся каждый из 141 аккаунта.

    Возвращает (commentable, flood_wait_seconds, caps):
      commentable — есть обсуждения И не по заявке И писать разрешено
      flood_wait  — секунды из FloodWaitError, иначе 0
      caps        — словарь свойств

    Свою сессию БД открывает сам: парсеры пишут каналы каждый в своей.
    Никогда не роняет вызывающий код.
    """
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.errors import FloodWaitError, ChannelPrivateError

    # ── Под какими именами записывать (2026-08-22) ───────────────
    # Записываем под ЗАПРОШЕННЫМ именем и, если оно отличается, под
    # каноническим. Причины (обе встречены на живых данных):
    #   * в базе может лежать устаревший алиас: запрашивали @adsninjas,
    #     а канонический юзернейм канала — @adsninja;
    #   * у части каналов в объекте нет ни username, ни usernames —
    #     каноническое имя пустое, и запись потерялась бы вовсе.
    # Ключ поиска для остальной системы — именно ЗАПРОШЕННОЕ имя
    # (так канал записан в target_channels и в планах), поэтому оно
    # первично. Без этого канал переспрашивался бы бесконечно.
    requested = _clean(username or "")
    canonical = entity_username(chat)
    names = [n for n in (requested, canonical) if n]
    if not names:
        names = []
    seen_names = []
    for n in names:
        if n.lower() not in [s.lower() for s in seen_names]:
            seen_names.append(n)
    uname = seen_names[0] if seen_names else ""

    try:
        full = await client(GetFullChannelRequest(channel=chat))
    except FloodWaitError as e:
        return (False, int(getattr(e, "seconds", 0) or 0), {})
    except ChannelPrivateError:
        for n in seen_names:
            await _record_standalone(n, {"has_discussion": False}, account_id,
                                     source, error="ChannelPrivateError")
        return (False, 0, {})
    except Exception as e:
        logger.debug(f"[caps] GetFullChannel @{uname}: {str(e)[:120]}")
        return (False, 0, {})

    caps = extract_caps_from_full(full, chat)
    ok, why = verdict(caps)
    for n in seen_names:
        await _record_standalone(n, caps, account_id, source)
    return (ok, 0, {**caps, "commentable": ok, "reason": why})


async def _record_standalone(username: str, caps: dict, account_id: int = None,
                             source: str = "parser", error: str = None):
    """record_caps со своей сессией БД (для парсеров)."""
    if not username:
        return
    try:
        from utils.db_pool import async_session as Session
        async with Session() as db:
            await record_caps(db, username, caps, account_id=account_id,
                              source=source, error=error)
            await db.commit()
    except Exception as e:
        logger.debug(f"[caps] standalone write @{username}: {e}")


async def probe_channel(client, db, username: str, account_id: int = None) -> dict:
    """ACTIVE-проверка: 2 read-only запроса, без вступления и отправки.

    Возвращает caps + 'commentable'/'reason'.

    ── ФИКС 2026-08-24 ──────────────────────────────────────────
    Раньше тут стоял голый `except Exception`, и ЛЮБАЯ ошибка писалась
    в кэш как свойство канала. В том числе FloodWaitError — а он про
    АККАУНТ (упёрлись в лимит запросов), а не про канал. Итог: 355
    нормальных каналов получили пометку `probe_FloodWaitError` и стали
    считаться непригодными, хотя мы про них попросту ничего не узнали.
    Та же болезнь, что была с shadow_ban и chat_write_forbidden —
    чужую проблему записали каналу.

    Теперь три разных исхода:
      * FloodWait / сеть / таймаут → НЕ пишем ничего, пробрасываем наверх,
        чтобы вызывающий остановил аккаунт;
      * юзернейм не существует → это правда свойство канала, помечаем;
      * приватный/забанен → неоднозначно, идёт через mark_bad с порогом
        подтверждения от двух аккаунтов.
    """
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.errors import (
        FloodWaitError, UsernameNotOccupiedError, UsernameInvalidError,
        ChannelPrivateError, ServerError,
    )
    clean = _clean(username)
    try:
        entity = await client.get_entity(clean)
        full = await client(GetFullChannelRequest(entity))
        caps = extract_caps_from_full(full, entity)
        await record_caps(db, clean, caps, account_id=account_id, source="telegram")
        ok, why = verdict(caps)
        return {**caps, "commentable": ok, "reason": why}

    except FloodWaitError:
        # Про канал не узнали НИЧЕГО. Пробрасываем — пусть аккаунт остановится.
        raise

    except (UsernameNotOccupiedError, UsernameInvalidError, ValueError) as e:
        # Юзернейма не существует — это действительно свойство канала.
        await mark_bad(db, clean, reason="username_not_found",
                       account_id=account_id, error=str(e))
        return {"commentable": False, "reason": "username_not_found"}

    except ChannelPrivateError as e:
        # Неоднозначно: приватный для всех или конкретный акк кикнут.
        # mark_bad сам потребует подтверждения от CONFIRM_ACCOUNTS аккаунтов.
        await mark_bad(db, clean, reason="channelprivate",
                       account_id=account_id, error=str(e))
        return {"commentable": False, "reason": "channelprivate"}

    except (ServerError, TimeoutError, ConnectionError, OSError) as e:
        # Наша сторона / сервер TG. Канал ни при чём — молчим.
        logger.debug(f"[caps] probe @{clean} transient: {type(e).__name__}")
        return {"commentable": None, "reason": "transient", "error": str(e)[:200]}

    except Exception as e:
        # Неизвестное. Раньше это молча хоронило канал — больше нет.
        logger.warning(f"[caps] probe @{clean} НЕИЗВЕСТНАЯ ошибка "
                       f"{type(e).__name__}: {str(e)[:120]} — канал НЕ помечаем")
        return {"commentable": None, "reason": f"unknown_{type(e).__name__}",
                "error": str(e)[:200]}


# ─────────────────────────────────────────────────────────────
# Резолв linked discussion group
# ─────────────────────────────────────────────────────────────
# Инцидент 2026-08-24 (кампания #139): 19 из 34 ошибок первой ночи были
# «The channel specified is private and you lack permission to access it»
# на действии join_discussion_of. Выглядело как претензия Telegram к каналу,
# а на деле это НАШ неверный запрос: код звал get_entity(linked_chat_id) с
# голым числовым id. Такой id резолвится только если access_hash группы уже
# лежит в кэше сессии — а для группы, в которой аккаунт ещё НЕ состоит, его
# там нет по определению. Telegram на это честно отвечает ChannelPrivateError.
#
# Правильный источник access_hash — сам ответ GetFullChannelRequest: linked-чат
# приходит в full.chats целиком, вместе с хэшем. Ни одного лишнего запроса:
# GetFullChannel всё равно вызывается, чтобы узнать linked_chat_id.

# Сколько последних постов перебрать в поисках живого треда обсуждения.
DISC_SCAN_POSTS = int(os.getenv("DISC_SCAN_POSTS", "15"))


async def resolve_discussion_entity(client, channel, link_id: int = None,
                                    for_join: bool = True):
    """Вернуть entity linked discussion group канала (или None).

    Args:
        client:   Telethon-клиент
        channel:  канал — username или entity
        link_id:  известный linked_chat_id (из Redis-кэша). Экономит один
                  GetFullChannelRequest.
        for_join: True — объект нужен, чтобы ВСТУПИТЬ; False — только
                  прочитать свойства.

    Почему два пути. Замер 2026-08-25, acc#109 @TonNewbie:
    объект из `full.chats` приходит с корректным access_hash, и
    get_input_entity отдаёт ровно тот же хэш — но JoinChannelRequest с ним
    падает ChannelPrivateError. Тот же join с объектом из ответа
    GetDiscussionMessageRequest проходит без вопросов (проверено на
    @moscowtop и @hijeygallery, включая канал, где аккаунт не состоит).
    То есть Telegram пускает в linked-группу только по хэшу, полученному
    в контексте обсуждения. Для чтения свойств хватает full.chats —
    там же лежат join_request и default_banned_rights.

    НЕ ловит FloodWaitError — флуд это претензия к аккаунту, её должен
    увидеть вызывающий, а не списать на «приватный канал».
    """
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.tl.functions.messages import GetDiscussionMessageRequest
    from telethon.errors import FloodWaitError

    full = None
    lid = int(link_id) if link_id else 0
    if not lid:
        full = await client(GetFullChannelRequest(channel))
        lid = getattr(full.full_chat, "linked_chat_id", 0) or 0
    if not lid:
        return None

    # ── Путь для вступления: хэш из контекста обсуждения ──
    # Берём несколько последних постов: у свежего поста тред может быть
    # ещё не создан, и GetDiscussionMessage отвечает MsgIdInvalidError.
    if for_join:
        try:
            posts = await client.get_messages(channel, limit=DISC_SCAN_POSTS)
        except FloodWaitError:
            raise
        except Exception:
            posts = []
        # Сперва пробуем посты, у которых тред УЖЕ существует: у свежего
        # поста его может не быть, и GetDiscussionMessage отвечает
        # MsgIdInvalidError. Замер 25.08: при скане пяти последних постов
        # 23 канала из кампании не резолвились, хотя обсуждение у всех есть
        # и в кэше свойств они помечены пригодными.
        posts = sorted(posts, key=lambda m: 0 if getattr(m, "replies", None) else 1)
        for m in posts:
            try:
                dm = await client(GetDiscussionMessageRequest(peer=channel, msg_id=m.id))
            except FloodWaitError:
                raise
            except Exception:
                continue
            for ch in (dm.chats or []):
                if getattr(ch, "id", None) == lid:
                    return ch
        # Контекст обсуждения не дался — для вступления запасной путь
        # бесполезен: объект из full.chats гарантированно даст
        # ChannelPrivateError на JoinChannelRequest (замер на @TonNewbie,
        # где GetDiscussionMessage отвечает MsgIdInvalid на всех свежих
        # постах). Возвращаем None, чтобы вызывающий записал внутреннюю
        # причину, а не выдумывал претензию Telegram к каналу.
        return None

    # ── Для чтения свойств: из full.chats ──
    if full is None:
        try:
            full = await client(GetFullChannelRequest(channel))
        except FloodWaitError:
            raise
        except Exception:
            return None
    for ch in (full.chats or []):
        if getattr(ch, "id", None) == lid:
            return ch
    return None
