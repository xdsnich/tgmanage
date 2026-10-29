"""
GramGPT — tasks/comment_executor.py
Обработчик очереди комментариев.

Каждые 60с проверяет comment_queue:
  - Берёт items где scheduled_at <= now и status='scheduled'
  - Выполняет "человекоподобное" комментирование:
    1. Pre-read (3-8 постов)
    2. Пауза 30-180с (чтение)
    3. 40% шанс реакции перед комментарием
    4. Typing 3-12с
    5. 10% шанс "передумал"
    6. Отправка комментария
    7. Post-read (1-3 поста)
  - Обновляет account_behavior cooldowns
  - Обработка ошибок (FLOOD_WAIT, PEER_FLOOD, frozen)
"""

import asyncio
import sys
import os
import re
import random
import logging
from datetime import datetime

from celery_app import celery_app
from tasks.realtime_comments import RT_IP_COOLDOWN_SEC

logger = logging.getLogger(__name__)
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try: return loop.run_until_complete(coro)
    finally: loop.close()


def _val(x):
    return x.value if hasattr(x, 'value') else x


async def _drop_channel(db, item, reason: str, только_если_никого=False,
                        в_реакции=False):
    """Убрать канал из realtime-кампании: писать туда мы не можем.

    Только для realtime. У плановой кампании канал закреплён за многими
    аккаунтами, и отказ одному ещё не значит отказ всем — там свой разбор.
    Здесь наоборот: пишет строго участник обсуждения, и если Telegram
    ответил «нет доступа» или «комментарии отключены», то ходить туда
    нечем и незачем. Наблюдатель опрашивает канал раз в минуту, а каждая
    неудачная попытка вдобавок занимает адрес — за час так набегает
    десяток отказов на ровном месте.

    Не удаляем, а гасим флаг: канал остаётся в кампании и виден, вердикт
    можно пересмотреть, а сеялка каналов не притащит его снова как новый.
    """
    from sqlalchemy import text as sa_text

    ch = (item.channel or "").lstrip("@")
    if not ch:
        return

    # «Нет доступа» — приговор паре аккаунт-канал, а не каналу. У 7%
    # каналов участников двое и больше, и отказ одному ещё не значит,
    # что писать туда некем: остальные заходят спокойно. Поэтому для
    # таких отказов сначала смотрим, остался ли хоть кто-то.
    if только_если_никого:
        остались = (await db.execute(sa_text("""
            SELECT count(*) FROM campaign_channel_assignments ca
            JOIN accounts a ON a.id = ca.account_id
            WHERE ca.status = 'joined' AND a.status = 'active'
              AND lower(ca.channel_username) = lower(:ch)"""),
            {"ch": ch})).scalar() or 0
        if остались:
            logger.info("[executor] @%s оставлен: участников ещё %d",
                        ch, остались)
            return

    # Бан в чате — это запрет ПИСАТЬ нашим аккаунтам, а не конец канала.
    # Чат живой, комментарии идут, и аккаунт без бана там по-прежнему
    # может ставить реакции — каждая приводит человека в профиль. Раньше
    # такие каналы гасились целиком: так пропало 556 штук. Теперь канал
    # остаётся в кампании в режиме «только реакции», наблюдатель перестаёт
    # звать туда писать, а обход ходит туда ставить реакции.
    if в_реакции:
        res = await db.execute(sa_text("""
            UPDATE target_channels t SET mode = 'react'
            FROM campaigns c
            WHERE c.id = t.campaign_id AND t.campaign_id = :camp
              AND c.campaign_type = 'realtime'
              AND lower(t.username) = lower(:ch)
              AND t.is_active AND t.mode = 'comment'"""),
            {"camp": item.campaign_id, "ch": ch})
        if res.rowcount:
            logger.warning("[executor] @%s → только реакции: %s", ch, reason)
        return

    res = await db.execute(sa_text("""
        UPDATE target_channels t SET is_active = false
        FROM campaigns c
        WHERE c.id = t.campaign_id AND t.campaign_id = :camp
          AND c.campaign_type = 'realtime'
          AND lower(t.username) = lower(:ch) AND t.is_active"""),
        {"camp": item.campaign_id, "ch": ch})
    if res.rowcount:
        logger.warning("[executor] @%s убран из кампании %s: %s",
                       ch, item.campaign_id, reason)


async def _do_smart_comment(client, account, channel_username, post_id, comment_text, personality):
    """
    Человекоподобное комментирование:
    pre-read → пауза → реакция(40%) → typing → abort(10%) → send → post-read
    Возвращает (status, detail, steps[]) — steps = лог каждого действия.
    """
    steps = []  # лог шагов: [{"action": "...", "detail": "...", "seconds": N}]
    entity = await client.get_entity(channel_username)

    # 1. Прочитать последние посты (листаем ленту)
    read_count = random.randint(
        personality.get("reads_before_comment_min", 3),
        personality.get("reads_before_comment_max", 8),
    )
    posts = await client.get_messages(entity, limit=read_count)
    for p in posts:
        await client.send_read_acknowledge(entity, p)
        await asyncio.sleep(random.uniform(1, 4))
    steps.append({"action": "pre_read", "detail": f"Прочитал {len(posts)} постов в @{channel_username}"})

    # 2. Задержка — "читаем" целевой пост (НЕ БОЛЕЕ 2-3 мин)
    read_time = random.randint(15, 180)
    await asyncio.sleep(read_time)
    steps.append({"action": "read_post", "detail": f"Читал пост #{post_id} ({read_time}с)"})

    # 3. Иногда ставим реакцию перед комментарием (40%)
    reaction_chance = random.uniform(0.05, 0.55)  # От 15% до 55% каждый раз разный
    if random.random() < reaction_chance:

        # Раньше здесь брался случайный эмодзи из пяти зашитых и слался
        # вслепую. В канале, где разрешены только 👍 и ❤️, такая реакция
        # отвергалась, и мы её просто теряли; там, где реакции выключены
        # совсем, каждая сессия дарила Telegram гарантированный отказ.
        # Живой человек видит доступные реакции и жмёт из них — берём ту
        # же логику, что давно работает на реакциях под комментариями.
        try:
            from telethon.tl.functions.messages import SendReactionRequest
            from telethon.tl.types import ReactionEmoji
            from telethon.errors import ReactionInvalidError

            разрешены = await _get_available_reactions(client, entity)
            if not разрешены:
                steps.append({"action": "reaction_skip",
                              "detail": "В канале реакции отключены"})
            else:
                # Несколько попыток разными эмодзи: канал мог отвергнуть
                # конкретный, но принять соседний.
                поставлен = None
                for emoji in random.sample(разрешены, min(3, len(разрешены))):
                    try:
                        await client(SendReactionRequest(
                            peer=entity, msg_id=post_id,
                            reaction=[ReactionEmoji(emoticon=emoji)]
                        ))
                        поставлен = emoji
                        break
                    except ReactionInvalidError:
                        continue
                if поставлен:
                    steps.append({"action": "reaction",
                                  "detail": f"Поставил {поставлен} на пост #{post_id}"})
                    await asyncio.sleep(random.randint(5, 30))
                else:
                    steps.append({"action": "reaction_skip",
                                  "detail": "Канал отверг все выбранные эмодзи"})
        except Exception as e:
            steps.append({"action": "reaction",
                          "detail": f"Реакция не вышла: {type(e).__name__}"})
    else:
        steps.append({"action": "reaction_skip", "detail": "Реакцию не ставил (рандом)"})

    # 4. Typing перед комментарием
    typing_done = False
    if personality.get("typing_before_comment", True):
        try:
            from telethon.tl.functions.messages import SetTypingRequest, GetDiscussionMessageRequest
            from telethon.tl.types import SendMessageTypingAction

            try:
                disc = await client(GetDiscussionMessageRequest(peer=entity, msg_id=post_id))
                if disc and disc.messages:
                    discussion_peer = disc.messages[0].peer_id
                    typing_duration = random.randint(2, 25)
                    await client(SetTypingRequest(peer=discussion_peer, action=SendMessageTypingAction()))
                    await asyncio.sleep(typing_duration)
                    steps.append({"action": "typing", "detail": f"Печатал {typing_duration}с в discussion"})
                    typing_done = True
            except Exception:
                typing_duration = random.randint(2, 25)
                await asyncio.sleep(typing_duration)
                steps.append({"action": "typing", "detail": f"Пауза {typing_duration}с (discussion недоступен)"})
                typing_done = True
        except Exception:
            wait = random.randint(3, 8)
            await asyncio.sleep(wait)
            steps.append({"action": "typing", "detail": f"Пауза {wait}с"})
            typing_done = True

    if not typing_done:
        steps.append({"action": "typing_skip", "detail": "Импульсивный — без typing"})

    # 5. 10% шанс "передумал"
    abort_chance = random.uniform(0.04, 0.19)  # 5-18%
    if random.random() < abort_chance:
        steps.append({"action": "abort", "detail": "Начал писать, передумал"})
        return "aborted", "Начал писать, передумал", steps

    # 6. Отправляем комментарий и сразу вытаскиваем adress-tuple для verify:
    # discussion_peer_id + discussion_msg_id. Telethon возвращает объект
    # Message откуда можно получить peer_id и id — оба они нам нужны для
    # последующего get_messages(peer, ids=[msg_id]) в verify-фазе.
    #
    # Auto-join: если TG говорит ChatGuestSendForbidden — гостям писать
    # нельзя, надо сначала вступить в linked discussion group. Пробуем
    # один раз JoinChannelRequest и повторяем send.
    from telethon.errors import ChatGuestSendForbiddenError
    from telethon.tl.functions.channels import (
        GetFullChannelRequest, JoinChannelRequest,
    )
    from telethon.errors import UserAlreadyParticipantError
    sent_msg = None
    for _try in range(2):
        try:
            sent_msg = await client.send_message(entity=entity, message=comment_text, comment_to=post_id)
            break
        except ChatGuestSendForbiddenError:
            if _try > 0:
                raise
            # Резолвим linked discussion и вступаем
            try:
                full = await client(GetFullChannelRequest(entity))
                linked_id = getattr(full.full_chat, "linked_chat_id", 0) or 0
                if not linked_id:
                    raise    # нечего join'ить
                disc = await client.get_entity(linked_id)
                try:
                    await client(JoinChannelRequest(disc))
                except UserAlreadyParticipantError:
                    pass
                steps.append({"action": "join_discussion", "detail": f"вступил в discussion @{channel_username}"})
                await asyncio.sleep(random.uniform(2, 5))
                # retry send через continue for-loop
            except Exception:
                raise    # оригинальный ChatGuestSendForbidden поднимаем наверх
    # Peer discussion-группы, куда попал коммент
    discussion_peer_id = None
    discussion_msg_id = None
    try:
        if sent_msg is not None:
            discussion_msg_id = int(getattr(sent_msg, "id", 0) or 0)
            # peer_id → PeerChannel/PeerChat/PeerUser
            peer = getattr(sent_msg, "peer_id", None)
            if peer is not None:
                # PeerChannel(channel_id=...) / PeerChat(chat_id=...) / PeerUser
                discussion_peer_id = (
                    getattr(peer, "channel_id", None)
                    or getattr(peer, "chat_id", None)
                    or getattr(peer, "user_id", None)
                )
                if discussion_peer_id is not None:
                    discussion_peer_id = int(discussion_peer_id)
    except Exception:
        pass
    steps.append({
        "action": "comment_sent",
        "detail": f"Отправил: {comment_text[:80]}",
        "discussion_peer_id": discussion_peer_id,
        "discussion_msg_id": discussion_msg_id,
    })

    # 6.5. Реакции на первые комменты под этим постом — одна-две.
    # Остальной объём набирается обходом соседних каналов ниже.
    try:
        rc_result = await _react_on_comments(client, entity, post_id)
        steps.append({"action": "react_comments", "detail": rc_result[1]})
    except Exception as e:
        steps.append({"action": "react_comments", "detail": f"skip ({type(e).__name__})"})

    # 6.7. Обход соседних каналов реакциями. Аккаунт уже подключён и
    # держит адрес десять минут — за это же подключение он отмечается
    # ещё в паре-тройке каналов, вместо того чтобы сваливать весь след
    # под один пост.
    try:
        tour = await _reaction_tour(client, account.id, channel_username)
        steps.append({"action": "reaction_tour", "detail": tour})
    except Exception as e:
        steps.append({"action": "reaction_tour", "detail": "skip (%s)" % type(e).__name__})

    # 7. После комментария — прочитать ещё 1-3 поста (не уходим сразу)
    await asyncio.sleep(random.randint(5, 20))
    more_posts = await client.get_messages(entity, limit=random.randint(1, 3))
    for p in more_posts:
        await client.send_read_acknowledge(entity, p)
        await asyncio.sleep(random.uniform(1, 3))
    steps.append({"action": "post_read", "detail": f"Дочитал {len(more_posts)} постов после коммента"})

    return "ok", f"Комментарий отправлен в @{channel_username}", steps


# ═══════════════════════════════════════════════════════════
# Reactions on comments — weighted toward top comments
# ═══════════════════════════════════════════════════════════

_DEFAULT_REACTIONS = ["👍", "🔥", "❤️", "🤔", "👏", "😁", "🥰", "🤩", "😱", "🤯", "💯", "🙏"]


async def _get_available_reactions(client, entity) -> list:
    """Возвращает список emoji-строк доступных в этом канале.
    Читает full_chat.available_reactions:
      * ChatReactionsAll → все стандартные emoji-реакции (наш _DEFAULT_REACTIONS)
      * ChatReactionsSome(reactions=[...]) → конкретный список ReactionEmoji
      * ChatReactionsNone / чтение упало → пустой список (реакции отключены)
    """
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.types import (
            ChatReactionsAll, ChatReactionsSome, ChatReactionsNone,
            ReactionEmoji,
        )
        full = await client(GetFullChannelRequest(entity))
        r = getattr(full.full_chat, "available_reactions", None)
        if isinstance(r, ChatReactionsAll):
            return list(_DEFAULT_REACTIONS)
        if isinstance(r, ChatReactionsSome):
            out = []
            for x in (r.reactions or []):
                if isinstance(x, ReactionEmoji):
                    out.append(x.emoticon)
            return out or list(_DEFAULT_REACTIONS)
        if isinstance(r, ChatReactionsNone):
            return []
    except Exception:
        pass
    # Fallback: default set (для лички / не-канала / ошибок parse)
    return list(_DEFAULT_REACTIONS)


# Сколько соседних каналов обойти реакциями за одну сессию и сколько
# реакций оставить в каждом. Смысл в том, чтобы набрать объём вширь, а не
# вглубь: аккаунт уже подключён и держит адрес десять минут, а тратит его
# на один комментарий. За то же подключение он отмечается ещё в паре-тройке
# каналов — охват втрое шире, след втрое тоньше, и за реакции не банят
# (проверено дважды: два прогона по десять пар, ноль ограничений).
#
# Главная цель обхода — каналы «только реакции»: там нашим аккаунтам
# запретили писать, и без обхода они не дают ничего. Поэтому они идут
# первыми, а обычные каналы добирают остаток.
TOUR_CHANNELS_MIN = int(os.getenv("RT_TOUR_CHANNELS_MIN", "3"))
TOUR_CHANNELS = int(os.getenv("RT_TOUR_CHANNELS", "4"))
TOUR_PER_CHANNEL = int(os.getenv("RT_TOUR_PER_CHANNEL", "2"))
# Счётчик по режимам за сутки — чтобы видеть, сколько реакций пришлось
# именно на каналы, где писать нельзя.
TOUR_MODE_KEY = "gramgpt:rt:reacts_mode:{day}"
# Ошибки, после которых аккаунту в этом чате делать нечего: выгнали,
# забанили или закрыли. Пару снимаем, чтобы обход сюда не возвращался.
_ТУР_БАН = ("ChannelPrivateError", "UserBannedInChannelError",
            "ChatWriteForbiddenError", "ChatAdminRequiredError")
# Суточный потолок реакций на аккаунт. У Telegram он есть — мы поймали
# ReactionsTooMany на acc#81, — поэтому считаем и свой.
TOUR_DAY_LIMIT = int(os.getenv("RT_TOUR_DAY_LIMIT", "40"))
TOUR_KEY = "gramgpt:rt:reacts:{acc}:{day}"


def _reacts_left(acc_id: int) -> int:
    """Сколько реакций аккаунту ещё можно сегодня."""
    try:
        from datetime import date
        from utils.redis_pool import get_redis
        k = TOUR_KEY.format(acc=acc_id, day=date.today().isoformat())
        return max(0, TOUR_DAY_LIMIT - int(get_redis().get(k) or 0))
    except Exception:
        return TOUR_DAY_LIMIT


def _reacts_use(acc_id: int, n: int = 1, режим: str = None):
    try:
        from datetime import date
        from utils.redis_pool import get_redis
        r = get_redis()
        день = date.today().isoformat()
        k = TOUR_KEY.format(acc=acc_id, day=день)
        r.incrby(k, n)
        r.expire(k, 172800)
        if режим:
            km = TOUR_MODE_KEY.format(day=день)
            r.hincrby(km, режим, n)
            r.expire(km, 8 * 86400)
    except Exception:
        pass


def _emoji_order(msg, pool: list, n: int = 3) -> list:
    """В каком порядке пробовать эмодзи на сообщении.

    Сначала те, что на нём уже стоят: живой человек чаще жмёт готовую
    реакцию, чем выбирает новую. Заодно это обходит ReactionsTooMany —
    Telegram отвечает им, когда на сообщении уже максимум РАЗНЫХ реакций
    и мы пытаемся добавить ещё одну новую. Уже стоящую добавить можно.
    """
    есть = []
    try:
        for rc in (msg.reactions.results or []):
            e = getattr(rc.reaction, "emoticon", None)
            if e and e in pool and e not in есть:
                есть.append(e)
    except Exception:
        pass
    random.shuffle(есть)
    прочие = [e for e in pool if e not in есть]
    random.shuffle(прочие)
    return (есть + прочие)[:n]


async def _снять_пару(account_id: int, канал: str):
    """Аккаунту в этом чате делать нечего — больше сюда не водим."""
    try:
        from sqlalchemy import text as _t
        from utils.db_pool import async_session as _S
        async with _S() as db:
            await db.execute(_t("""
                UPDATE campaign_channel_assignments SET status = 'failed'
                WHERE account_id = :a AND lower(channel_username) = lower(:ch)
                  AND status = 'joined'"""), {"a": account_id, "ch": канал})
            await db.commit()
    except Exception:
        logger.debug("[tour] не удалось снять пару", exc_info=True)


async def _reaction_tour(client, account_id: int, кроме: str) -> str:
    """Пройтись реакциями по соседним каналам, не выходя из сессии.

    Зачем. Аккаунт уже подключён и держит адрес десять минут, а тратит
    его на один комментарий. За то же подключение он заходит ещё в 3-4
    канала и ставит реакции под верхними комментариями — каждый автор
    получает уведомление, открывает профиль и видит канал воронки.

    Куда. В первую очередь в каналы «только реакции»: там нашим
    аккаунтам запретили писать, и кроме обхода трафика с них нет. Только
    туда, где аккаунт УЖЕ состоит — вступать на лету лишний след. И
    никогда туда, где ЭТОГО аккаунта забанили: бан на пару аккаунт-чат,
    и забаненный не поставит даже реакцию. Сосед без бана — поставит.

    Ошибки не страшны: канал мог закрыть чат. Идём дальше, а если
    аккаунта выгнали — снимаем пару, чтобы не возвращаться.
    """
    import random as _r
    from sqlalchemy import text as _t
    from telethon.tl.functions.messages import (
        SendReactionRequest, GetDiscussionMessageRequest)
    from telethon.tl.types import ReactionEmoji
    from telethon.errors import ReactionInvalidError
    from utils.db_pool import async_session as _S

    осталось = _reacts_left(account_id)
    if осталось <= 0:
        return "суточный потолок реакций выбран"

    нужно = _r.randint(TOUR_CHANNELS_MIN, max(TOUR_CHANNELS_MIN, TOUR_CHANNELS))

    # Бан — по истории этой пары: «Channel private» (админ выгнал из
    # чата) или «can't write» (запретили писать), и после него ни одного
    # удачного комментария. Статусу 'joined' одному не верим: ночная
    # подписка ставит его заново, потому что вступить в КАНАЛ можно и
    # с баном в ЧАТЕ — так набралась тысяча ложных «подписан».
    async with _S() as db:
        каналы = [(x[0], bool(x[1])) for x in (await db.execute(_t("""
            SELECT ca.channel_username, bool_or(tc.mode = 'react') AS react
            FROM campaign_channel_assignments ca
            JOIN target_channels tc
              ON lower(tc.username) = lower(ca.channel_username)
             AND tc.is_active AND tc.mode IN ('comment', 'react')
            WHERE ca.account_id = :a AND ca.status = 'joined'
              AND lower(ca.channel_username) <> lower(:ex)
              AND NOT EXISTS (
                  SELECT 1 FROM comment_queue q
                  WHERE q.account_id = ca.account_id
                    AND lower(q.channel) = lower(ca.channel_username)
                    AND q.status = 'failed'
                    AND (q.error ILIKE 'Channel private%%'
                         OR q.error ILIKE '%%can''t write%%')
                    AND NOT EXISTS (
                        SELECT 1 FROM comment_queue d
                        WHERE d.account_id = q.account_id
                          AND lower(d.channel) = lower(q.channel)
                          AND d.status = 'done'
                          AND d.executed_at > q.executed_at))
            GROUP BY ca.channel_username
            ORDER BY bool_or(tc.mode = 'react') DESC, random()
            LIMIT :lim"""),
            {"a": account_id, "ex": (кроме or "").lstrip("@"),
             "lim": нужно * 3})).all()]
    if not каналы:
        return "нет соседних каналов"

    посещено = поставлено = 0
    по_режиму = {"react": 0, "comment": 0}
    журнал = []
    for ch, react in каналы:
        if посещено >= нужно or осталось <= 0:
            break
        режим = "react" if react else "comment"
        try:
            ent = await client.get_entity(ch)
            # Свежий пост без комментариев — пустой заход. Берём самый
            # новый из трёх, под которым уже кто-то написал.
            посты = await client.get_messages(ent, limit=3)
            пост = next((p for p in посты
                         if getattr(getattr(p, "replies", None), "replies", 0)), None)
            if not пост:
                continue
            disc = await client(GetDiscussionMessageRequest(
                peer=ent, msg_id=пост.id))
            if not (disc and disc.messages):
                continue
            корень = disc.messages[0]
            чат = await client.get_entity(корень.peer_id)
            комменты = [c for c in await client.get_messages(
                чат, limit=8, reply_to=корень.id) if c.id != корень.id]
            if not комменты:
                continue
            разр = await _get_available_reactions(client, чат)
            if not разр:
                continue
            посещено += 1
            # Первые комментарии — самые верхние и самые кликаемые.
            комменты.reverse()
            сколько = min(TOUR_PER_CHANNEL, len(комменты), осталось)
            тут = 0
            выгнан = False
            for msg in комменты[:сколько]:
                for emoji in _emoji_order(msg, разр):
                    try:
                        await client(SendReactionRequest(
                            peer=чат, msg_id=msg.id,
                            reaction=[ReactionEmoji(emoticon=emoji)]))
                        тут += 1
                        осталось -= 1
                        _reacts_use(account_id, 1, режим)
                        break
                    except ReactionInvalidError:
                        continue
                    except Exception as e:
                        if type(e).__name__ in _ТУР_БАН:
                            выгнан = True
                        журнал.append("@%s:%s" % (ch, type(e).__name__))
                        break
                if выгнан:
                    break
                await asyncio.sleep(_r.uniform(2, 6))
            if выгнан:
                await _снять_пару(account_id, ch)
            поставлено += тут
            по_режиму[режим] += тут
            журнал.append("@%s[%s]%d" % (ch, режим[0], тут))
            await asyncio.sleep(_r.uniform(4, 12))
        except Exception as e:
            if type(e).__name__ in _ТУР_БАН:
                await _снять_пару(account_id, ch)
            журнал.append("@%s:%s" % (ch, type(e).__name__))
            continue

    return "обошёл %d каналов, поставил %d реакций (react %d, comment %d): %s" % (
        посещено, поставлено, по_режиму["react"], по_режиму["comment"],
        " ".join(журнал))


async def _react_on_comments(client, channel_entity, post_id: int) -> tuple[str, str]:
    """Ставит одну-две реакции на верхние комменты под постом.

    Объём набирается не здесь, а обходом соседних каналов: две реакции
    под одним постом — это человек, двенадцать — это программа.

    Возвращает (status, detail).
    """
    from telethon.tl.functions.messages import (
        SendReactionRequest, GetDiscussionMessageRequest)
    from telethon.tl.types import ReactionEmoji
    from telethon.errors import ReactionInvalidError

    # 1. Корень обсуждения. Раньше брался linked_chat_id, а комментарии
    # запрашивались по id поста в КАНАЛЕ — в группе у него свой номер, и
    # Telegram отвечал MsgIdInvalid. Комментарий при этом уходил как ни в
    # чём не бывало, поэтому поломку не было видно: 6065 удачных сессий
    # прошли без единой реакции. GetDiscussionMessage отдаёт и группу, и
    # настоящий id корня разом — так делает обход соседних каналов ниже.
    try:
        disc = await client(GetDiscussionMessageRequest(
            peer=channel_entity, msg_id=post_id))
        if not (disc and disc.messages):
            return "skip", "нет discussion-группы"
        корень = disc.messages[0]
        discussion = await client.get_entity(корень.peer_id)
    except Exception as e:
        return "skip", f"нет discussion ({type(e).__name__})"

    # 2. Комменты приходят новые → старые, разворачиваем: верхние первыми.
    try:
        comments = [c for c in await client.get_messages(
            discussion, limit=40, reply_to=корень.id) if c.id != корень.id]
    except Exception as e:
        return "skip", f"комменты не читаются ({type(e).__name__})"
    if not comments:
        return "skip", "0 комментов"
    comments.reverse()

    # 3. Разрешённые emoji спрашиваем у ГРУППЫ, а не у канала: реакции
    # летят в группу, и набор у неё свой. Спрашивали у канала — отсюда
    # пятая часть отказов на ровном месте.
    reactions_pool = await _get_available_reactions(client, discussion)
    if not reactions_pool:
        return "skip", "реакции отключены в чате"

    # 4. Одна-две, и обе из верхних шести — по ним и кликают живые люди.
    target_count = min(random.randint(1, 2), len(comments))
    верх = comments[:6]
    выбор = random.sample(верх, min(target_count, len(верх)))

    ok_count = 0
    отказы = []
    for msg in выбор:
        # Несколько эмодзи подряд: чат мог отвергнуть конкретный, но
        # принять соседний. Уже стоящие на сообщении — первыми.
        for emoji in _emoji_order(msg, reactions_pool):
            try:
                await client(SendReactionRequest(
                    peer=discussion, msg_id=msg.id,
                    reaction=[ReactionEmoji(emoticon=emoji)],
                ))
                ok_count += 1
                break
            except ReactionInvalidError:
                continue
            except Exception as e:
                отказы.append(type(e).__name__)
                break
        else:
            отказы.append("ReactionInvalid")
        await asyncio.sleep(random.uniform(1, 4))

    return "ok", (
        "Реакции на комменты: %d ok, %d fail, target=%d, пул=%d emoji%s"
        % (ok_count, len(отказы), target_count, len(reactions_pool),
           (" [" + ",".join(sorted(set(отказы))) + "]") if отказы else "")
    )


async def _execute_queue_item(item, db):
    """Выполняет один элемент очереди."""
    if API_DIR not in sys.path: sys.path.insert(0, API_DIR)

    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from models.campaign import Campaign, CommentLog, CampaignStatus
    from models.comment_queue import CommentQueue
    from utils.telegram import make_telethon_client
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.ip_throttle import acquire_ip_lock
    from services.llm import generate_comment
    from tasks.behavior_engine import get_or_create_behavior

    now = datetime.utcnow()

    # Acquire Redis lock — skip if another session is using this account
    if not acquire_account_lock(item.account_id, ttl=300):
        logger.info(f"[executor] Аккаунт {item.account_id} занят (lock) — пропуск")
        item.status = "scheduled"  # Return to queue for retry
        return

    # Помечаем как executing
    item.status = "executing"
    await db.flush()

    # Загружаем аккаунт
    acc_r = await db.execute(
        select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
        .where(TelegramAccount.id == item.account_id)
    )
    account = acc_r.scalar_one_or_none()
    if not account or not account.session_file:
        item.status = "failed"
        item.error = "Аккаунт не найден или нет session"
        item.executed_at = now
        return

    if _val(account.status) not in ("active", "unknown"):
        item.status = "failed"
        item.error = f"Аккаунт в статусе {_val(account.status)}"
        item.executed_at = now
        return

    # Прокси
    proxy = None
    if account.proxy_id:
        proxy_r = await db.execute(select(Proxy).where(Proxy.id == account.proxy_id))
        proxy = proxy_r.scalar_one_or_none()

    if not proxy:
        item.status = "failed"
        item.error = "Нет прокси"
        item.executed_at = now
        logger.warning(f"[executor] Аккаунт {account.phone} без прокси — пропуск")
        return

    # Кампанию загружаем ДО замка адреса: от её типа зависит, насколько
    # долго адрес держать.
    camp_r = await db.execute(select(Campaign).where(Campaign.id == item.campaign_id))
    campaign = camp_r.scalar_one_or_none()
    _is_rt = bool(campaign) and _val(campaign.campaign_type) == "realtime"

    # ── Замок адреса ────────────────────────────────────────────────
    # Замка аккаунта мало: на одном прокси сидят четыре-пять аккаунтов, и
    # без этого сюда мог влезть второй, пока первый работает, — а плановый
    # движок и проверка каналов давно берут этот замок. Диспетчер лишь
    # СМОТРИТ на занятость, захватывает адрес тот, кто реально
    # подключается, то есть мы.
    #
    # Realtime держит адрес коротко. Смысл движка в том, чтобы успеть под
    # свежий пост, а десятиминутное остывание давало адресу занятость 83%
    # и превращало очередь в склад протухающих заданий. Ослабления защиты
    # тут нет: полные десять минут по-прежнему отделяют РАЗНЫЕ аккаунты на
    # одном адресе — именно это и было причиной инцидента 2026-06-12.
    # Быстро возвращается только тот же самый аккаунт, за которым адрес
    # уже закреплён (см. owner в utils/ip_throttle).
    _ttl = RT_IP_COOLDOWN_SEC if _is_rt else None
    if not acquire_ip_lock(proxy, ttl=_ttl, owner=account.id):
        logger.info(f"[executor] адрес {proxy.host} занят — задание вернётся в очередь")
        item.status = "scheduled"
        release_account_lock(item.account_id)
        return

    if not campaign or _val(campaign.status) != "active":
        item.status = "failed"
        item.error = "Кампания не активна"
        item.executed_at = now
        return

    # Behavior
    behavior = await get_or_create_behavior(db, account.id, account.phone)

    # Генерируем комментарий через LLM
    # Генерируем комментарий через LLM
    from services.llm import build_comment_prompt
    from services.service_credentials import get_api_key, get_api_keys_pool
    style_profile = item.style or {}
    personality = item.personality or {}

    prompt = build_comment_prompt(item.post_text, style_profile, personality)

    provider = _val(campaign.llm_provider)
    api_key = await get_api_key(
        db, campaign.user_id, provider,
        credential_id=getattr(campaign, "llm_credential_id", None),
    )
    # Весь пул ключей, а не один. У Groq потолок 8000 токенов в минуту НА
    # КЛЮЧ, а один наш запрос весит около тысячи — то есть с одного ключа
    # выходит восемь комментариев в минуту, дальше 429. Ключей у юзера
    # четыре, и плановый движок давно берёт их все (api_keys=), а сюда
    # передавался ровно один: замер 09.09 — у ключа #3 израсходовано, у
    # остальных трёх по 999 запросов из 1000, то есть они простаивали.
    api_keys = await get_api_keys_pool(db, campaign.user_id, provider)
    comment_text = generate_comment(provider, prompt, item.post_text,
                                    api_key=api_key, api_keys=api_keys)

    if not comment_text:
        item.status = "failed"
        item.error = "LLM не сгенерировал комментарий"
        item.executed_at = now
        return

    # Подключаемся и выполняем smart comment
    client = make_telethon_client(account, proxy)
    if not client:
        item.status = "failed"
        item.error = "Не удалось создать клиент"
        item.executed_at = now
        return

    try:
        await client.connect()
        if not await client.is_user_authorized():
            from services.connection_logger import log_connection
            from utils.connection_limiter import increment_connection
            increment_connection(account.id)
            await log_connection(db, account.id, source='commenting', proxy_id=proxy.id if proxy else None)
            item.status = "failed"
            item.error = "Не авторизован"
            item.executed_at = now
            return

        # ── Предохранитель по режиму канала ─────────────────────────
        # Читаем режим у самого края, перед самой отправкой. Отбор такие
        # каналы уже не отдаёт, но задание могло лечь в очередь раньше
        # смены режима — а один комментарий туда, где нас забанили,
        # стоит аккаунта.
        try:
            from sqlalchemy import text as _sa_text
            _режим = (await db.execute(_sa_text("""
                SELECT mode FROM target_channels
                WHERE campaign_id = :c AND lower(username) = lower(:ch)
                LIMIT 1"""),
                {"c": item.campaign_id,
                 "ch": (item.channel or "").lstrip("@")})).scalar()
        except Exception:
            _режим = None
        if _режим in ("react", "dead"):
            logger.warning("[executor] @%s режим «%s» — писать нельзя, отменяю",
                           item.channel, _режим)
            item.status = "aborted"
            item.error = "канал только для реакций"
            try:
                from tasks.realtime_comments import _quota_give_back
                _quota_give_back(item.account_id, item.channel)
            except Exception:
                pass
            await db.commit()
            return {"status": "skipped", "reason": "режим %s" % _режим}

        status, detail, steps = await _do_smart_comment(
            client, account, item.channel, item.post_id, comment_text, personality
        )

        item.executed_at = datetime.utcnow()
        item.comment_text = comment_text

        # Сохраняем шаги в personality поле (для логов)
        import json
        item.personality = {
            **(item.personality or {}),
            "_steps": steps,
        }

        if status == "aborted":
            item.status = "aborted"
            item.error = detail
            logger.info(f"[executor] {account.phone} -> @{item.channel}: передумал")
            return

        if status == "ok":
            item.status = "done"

            # АТОМАРНЫЙ инкремент через SQL (см. plan_executor 2026-07-14 fix):
            # обычный += даёт race при параллельных сессиях, теряет инкременты.
            from sqlalchemy import update as _sa_update
            from models.campaign import Campaign as _Camp
            await db.execute(
                _sa_update(_Camp)
                .where(_Camp.id == campaign.id)
                .values(comments_sent=_Camp.comments_sent + 1)
            )

            # Обновляем behavior
            behavior.comments_today += 1
            behavior.last_comment_at = datetime.utcnow()
            channels = behavior.channels_commented_today or []
            channels.append(item.channel)
            behavior.channels_commented_today = channels

            # Лог — сохраняем discussion peer/msg id для последующего verify.
            # Достаём из шага 'comment_sent' в steps (см. _do_smart_comment).
            disc_peer = disc_msg = None
            for _st in steps:
                if _st.get("action") == "comment_sent":
                    disc_peer = _st.get("discussion_peer_id")
                    disc_msg  = _st.get("discussion_msg_id")
                    break
            db.add(CommentLog(
                campaign_id=campaign.id,
                account_id=account.id,
                account_phone=account.phone,
                channel_username=item.channel,
                channel_title="",
                post_id=item.post_id,
                post_text=item.post_text[:500],
                comment_text=comment_text,
                llm_provider=_val(campaign.llm_provider),
                discussion_peer_id=disc_peer,
                discussion_msg_id=disc_msg,
            ))

            logger.info(f"[executor] {account.phone} -> @{item.channel} #{item.post_id}: {comment_text[:50]}...")

    except Exception as e:
        err = str(e)
        item.executed_at = datetime.utcnow()

        if "FLOOD_WAIT" in err:
            wait = int(re.search(r"(\d+)", err).group(1)) if re.search(r"(\d+)", err) else 60
            logger.warning(f"[executor] FLOOD_WAIT_{wait} — {account.phone}")
            await asyncio.sleep(wait + random.randint(5, 15))
            item.status = "failed"
            item.error = f"FLOOD_WAIT_{wait}"

        elif "PEER_FLOOD" in err:
            # PEER_FLOOD = Telegram уже включил жёсткий anti-spam на аккаунт.
            # Если продолжать слать — гарантированный 24h+ ban. Замораживаем,
            # пусть человек разберётся и разбанит при необходимости.
            logger.error(f"[executor] PEER_FLOOD — {account.phone} → freezing account")
            account.status = "frozen"
            item.status = "failed"
            item.error = "PEER_FLOOD (account auto-frozen)"

        elif "AUTH_KEY_UNREGISTERED" in err or "UserDeactivatedBan" in type(e).__name__:
            logger.warning(f"[executor] Account frozen: {account.phone}")
            account.status = "frozen"
            item.status = "failed"
            item.error = "Account frozen"

        elif "banned from sending messages" in err.lower():
            # Глобальное ограничение: писать аккаунт не может нигде.
            # Снимаем его с realtime и ставим отметку со сроком, который
            # назвал Telegram — по ней задача rt_restore вернёт его сама.
            logger.warning("[executor] acc#%s ограничен на отправку",
                           item.account_id)
            item.status = "failed"
            item.error = "You're banned from sending messages in supergroups/channels"
            try:
                from tasks.rt_restore import mark_limited, снять_с_кампании
                mark_limited(item.account_id, "?")
                await снять_с_кампании(db, item.campaign_id, item.account_id)
            except Exception:
                logger.debug("[executor] не удалось снять ограниченного",
                             exc_info=True)

        elif "CHANNEL_PRIVATE" in err or "private" in err.lower():
            logger.warning(f"[executor] @{item.channel}: нет доступа")
            item.status = "failed"
            item.error = "Channel private"
            # Отметку о членстве снимаем. Realtime-отбор пускает писать
            # только участника обсуждения и верит двум источникам: флагу
            # в Redis и записи 'joined' в assignments. Раз Telegram
            # отвечает «нет доступа» — участия давно нет, а отметка
            # осталась. Пока она висит, тот же аккаунт будет выбираться
            # на тот же канал снова: 22 отправки из 60 ушли именно так,
            # и каждая заняла адрес на десять минут впустую.
            try:
                _ch = (item.channel or "").lstrip("@")
                from utils.redis_pool import get_redis
                get_redis().delete("gramgpt:disc_joined:%d:%s"
                                   % (item.account_id, _ch))
                from sqlalchemy import text as _sa_text
                await db.execute(_sa_text("""
                    UPDATE campaign_channel_assignments SET status = 'failed'
                    WHERE account_id = :a AND channel_username = :ch
                      AND status = 'joined'"""),
                    {"a": item.account_id, "ch": _ch})
                await _drop_channel(db, item, "нет доступа",
                                    только_если_никого=True, в_реакции=True)
            except Exception:
                logger.debug("[executor] не удалось снять отметку о членстве",
                             exc_info=True)

        elif ("can't write in this chat" in err.lower()
              or "ChatWriteForbidden" in type(e).__name__):
            # «Нельзя писать в этот чат» — приговор паре аккаунт-канал:
            # либо аккаунта там забанили, либо чат ему закрыт. Раньше эта
            # ошибка никак не помечалась, и движок выбирал ту же пару
            # снова и снова: за сутки acc#166 упал так 36 раз в ОДИН
            # канал, acc#202 — 47 раз, всего 258 попыток вхолостую, и
            # каждая занимала адрес на две минуты.
            #
            # Снимаем отметку о членстве у ЭТОГО аккаунта, а канал гасим
            # только если писать в него стало реально некем: у части
            # каналов участников двое и больше.
            logger.warning("[executor] acc#%s не может писать в @%s",
                           item.account_id, item.channel)
            item.status = "failed"
            item.error = "You can't write in this chat"
            try:
                _ch = (item.channel or "").lstrip("@")
                from utils.redis_pool import get_redis
                get_redis().delete("gramgpt:disc_joined:%d:%s"
                                   % (item.account_id, _ch))
                from sqlalchemy import text as _sa_text
                await db.execute(_sa_text("""
                    UPDATE campaign_channel_assignments SET status = 'failed'
                    WHERE account_id = :a AND channel_username = :ch
                      AND status = 'joined'"""),
                    {"a": item.account_id, "ch": _ch})
                await _drop_channel(db, item, "писать запрещено",
                                    только_если_никого=True, в_реакции=True)
            except Exception:
                logger.debug("[executor] не удалось снять пару", exc_info=True)

        elif "ALLOW_PAYMENT_REQUIRED" in err:
            # Канал берёт плату за комментарий: писать может только тот,
            # у кого оплачена подписка. Для нас он бесполезен так же, как
            # закрытый, поэтому гасим сразу — иначе аккаунт будет ходить
            # туда снова и снова, занимая адрес на десять минут за раз.
            logger.warning(f"[executor] @{item.channel}: комментарии платные")
            item.status = "failed"
            item.error = "Комментарии платные"
            await _drop_channel(db, item, "комментарии платные")

        elif "GetDiscussionMessage" in err or "MESSAGE_ID_INVALID" in err:
            logger.warning(f"[executor] @{item.channel} #{item.post_id}: комменты отключены")
            item.status = "failed"
            item.error = "Comments disabled"
            await _drop_channel(db, item, "комментарии отключены")

        else:
            logger.error(f"[executor] ❌ {account.phone} → @{item.channel}: {e}")
            item.status = "failed"
            item.error = str(e)[:500]

    finally:
        try: await client.disconnect()
        except: pass
        release_account_lock(item.account_id)
        # ── Норму возвращаем, если коммента не вышло ─────────────────
        # В realtime норма списывается заранее, при постановке в очередь,
        # чтобы аккаунт не набрал заданий сверх суточного потолка. Но
        # если задание провалилось или аккаунт «передумал», коммента не
        # было — и держать за него единицу нормы нельзя.
        try:
            if item.status in ("failed", "aborted"):
                from tasks.realtime_comments import _quota_give_back
                _quota_give_back(item.account_id, item.channel)
        except Exception:
            pass
        # Замок адреса НЕ отпускаем намеренно. Это не мьютекс на время
        # сессии, а остывание: ключ живёт 10 минут после захвата, чтобы
        # подключения с одного IP не шли впритык друг за другом. Плановый
        # движок ведёт себя так же — берёт и не отпускает.
        # Досрочное освобождение сводило бы остывание на нет.


async def _process_comment_queue():
    logger.warning("[DEPRECATED] _process_comment_queue — используйте dispatch_comments")
    return {"processed": 0, "deprecated": True}
    """Обрабатывает очередь комментариев — запускается каждые 60с."""
    if API_DIR not in sys.path: sys.path.insert(0, API_DIR)

    from sqlalchemy import select
    from models.comment_queue import CommentQueue
    from utils.db_pool import async_session as Session

    async with Session() as db:
        try:
            now = datetime.utcnow()

            # Берём задачи готовые к выполнению
            result = await db.execute(
                select(CommentQueue).where(
                    CommentQueue.status == "scheduled",
                    CommentQueue.scheduled_at <= now,
                ).order_by(CommentQueue.scheduled_at.asc()).limit(3)  # Max 3 за раз
            )
            items = result.scalars().all()

            if not items:
                return {"processed": 0}

            logger.info(f"[executor] Найдено {len(items)} комментариев в очереди")

            processed = 0
            for item in items:
                try:
                    await _execute_queue_item(item, db)
                    await db.commit()
                    processed += 1
                except Exception as e:
                    logger.error(f"[executor] Ошибка обработки #{item.id}: {e}")
                    await db.rollback()
                    item.status = "failed"
                    item.error = str(e)[:500]
                    item.executed_at = datetime.utcnow()
                    await db.commit()

                # Пауза между комментариями
                if processed < len(items):
                    await asyncio.sleep(random.randint(10, 30))

            return {"processed": processed}

        except Exception as e:
            logger.error(f"[executor] Ошибка: {e}")
            await db.rollback()
            return {"error": str(e)}


@celery_app.task(bind=True, name="tasks.comment_executor.process_comment_queue")
def process_comment_queue(self):
    """Обработка очереди комментариев — каждые 60с."""
    self.update_state(state="PROGRESS", meta={"message": "Обработка очереди..."})
    return run_async(_process_comment_queue())

# ═══════════════════════════════════════════════════════════
# ПАРАЛЛЕЛЬНЫЙ РЕЖИМ
# Диспетчер (<1с) → отдельная задача на каждый комментарий
# ═══════════════════════════════════════════════════════════

async def _dispatch_comments():
    """Находит готовые комментарии → отправляет по одной задаче."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy import select
    from models.comment_queue import CommentQueue
    from utils.db_pool import async_session as Session

    dispatched = 0

    try:
        async with Session() as db:
            now = datetime.utcnow()
            result = await db.execute(
                select(CommentQueue).where(
                    CommentQueue.status == "scheduled",
                    CommentQueue.scheduled_at <= now,
                ).order_by(CommentQueue.scheduled_at.asc()).limit(50)
            )
            items = result.scalars().all()

            # STAGGER — копия логики из plan_executor.dispatch_plans.
            # Без неё 50 готовых комментариев улетели бы в очередь за
            # один тик и создали burst (как dispatch_plans до фикса
            # 2026-06-12). Размазываем countdown по окну.
            import random as _rand
            _rand.shuffle(items)
            stagger_min  = int(os.getenv("DISPATCH_STAGGER_MIN_SEC", "30"))
            stagger_max  = int(os.getenv("DISPATCH_STAGGER_MAX_SEC", "3600"))
            per_plan_sec = float(os.getenv("DISPATCH_STAGGER_PER_PLAN_SEC", "30"))
            window_sec = max(stagger_min, min(stagger_max, int(len(items) * per_plan_sec)))

            for item in items:
                countdown = _rand.uniform(0, window_sec) if items else 0
                celery_app.send_task(
                    "tasks.comment_executor.execute_single_comment",
                    args=[item.id],
                    queue="ai_dialogs",
                    countdown=countdown,
                )
                dispatched += 1

            if dispatched:
                logger.info(
                    f"[dispatch] Comments: отправлено {dispatched} в окне "
                    f"{window_sec}с (anti-burst stagger)"
                )
    except Exception as e:
        logger.error(f"[dispatch] Comments ошибка: {e}")

    return {"dispatched": dispatched}


async def _execute_single_comment(queue_id: int):
    """Выполняет ОДИН комментарий — отдельная Celery задача."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy import select
    from models.comment_queue import CommentQueue
    from utils.db_pool import async_session as Session

    async with Session() as db:
        try:
            item = (await db.execute(
                select(CommentQueue).where(CommentQueue.id == queue_id)
            )).scalar_one_or_none()

            if not item:
                return {"status": "skip", "reason": "not found"}
            if item.status not in ("scheduled", "dispatched"):
                return {"status": "skip", "reason": f"status={item.status}"}

            await _execute_queue_item(item, db)
            await db.commit()

            return {"status": item.status, "channel": item.channel}

        except Exception as e:
            logger.error(f"[single_comment] #{queue_id}: {e}")
            try:
                await db.rollback()
            except:
                pass
            return {"error": str(e)}


@celery_app.task(bind=True, name="tasks.comment_executor.dispatch_comments")
def dispatch_comments(self):
    """Диспетчер комментариев (<1с)."""
    return run_async(_dispatch_comments())


# Жёсткий потолок на одну сессию комментинга. Celery с потоками не умеет
# убивать зависшие задания (task_time_limit не работает на -P threads), и
# одно повисшее на прокси подключение держит поток навсегда. 14.10 так
# забились все 40 потоков — воркер «жив», но 5,5 ч ничего не обрабатывал.
# wait_for отменяет корутину по дедлайну, finally внутри отпускает замки,
# цикл run_async закрывается — поток освобождается.
EXEC_HARD_TIMEOUT = int(os.getenv("EXEC_HARD_TIMEOUT_SEC", "480"))


async def _execute_single_guarded(queue_id: int):
    try:
        return await asyncio.wait_for(_execute_single_comment(queue_id), EXEC_HARD_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error("[single_comment] #%s завис дольше %dс — снято по таймауту",
                     queue_id, EXEC_HARD_TIMEOUT)
        try:
            from sqlalchemy import text as _t
            from utils.db_pool import async_session as _S
            async with _S() as db:
                await db.execute(_t("""UPDATE comment_queue SET status='failed',
                    error='сессия зависла, снято по таймауту', executed_at=now()
                    WHERE id=:i AND status IN ('scheduled','executing','dispatched')"""), {"i": queue_id})
                await db.commit()
        except Exception:
            pass
        return {"status": "timeout"}


@celery_app.task(bind=True, name="tasks.comment_executor.execute_single_comment")
def execute_single_comment(self, queue_id: int):
    """Один комментарий — параллельно с другими."""
    return run_async(_execute_single_guarded(queue_id))