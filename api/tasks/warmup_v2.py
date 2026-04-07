"""
GramGPT API — tasks/warmup_v2.py
Прогрев аккаунтов v2 — модель сессий живого человека.

Вместо "одно действие каждые N минут" — сессии:
  - Утренняя проверка (8–10): 5-10 действий за 5 мин
  - Мини-проверки (10–18): 1-3 действия за 30с, каждые 2ч
  - Обеденный залип (12–14): 8-15 действий за 10 мин
  - Вечерний сёрфинг (19–23): 10-25 действий за 20 мин

Между сессиями — тишина (часы). Как реальный человек.
"""

import asyncio
import random
import re
import sys
import os
import logging
from datetime import datetime, timedelta
from collections import defaultdict

from celery_app import celery_app

logger = logging.getLogger(__name__)

API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════════════════

ACTIONS = [
    {"name": "read_feed",      "weight": 25, "label": "📖 Чтение ленты"},
    {"name": "set_reaction",   "weight": 18, "label": "😍 Реакция"},
    {"name": "view_stories",   "weight": 12, "label": "👁 Просмотр Stories"},
    {"name": "view_profile",   "weight": 8,  "label": "👤 Просмотр профиля"},
    {"name": "typing",         "weight": 5,  "label": "⌨️ Печатает"},
    {"name": "search",         "weight": 5,  "label": "🔍 Поиск"},
    {"name": "join_channel",   "weight": 4,  "label": "📢 Вступление в канал"},
    {"name": "forward_saved",  "weight": 5,  "label": "💾 Пересылка в Saved"},
    {"name": "send_saved",     "weight": 10, "label": "💬 Сообщение в Saved"},
    {"name": "reply_dm",       "weight": 8,  "label": "↩️ Ответ на ЛС"},
    {"name": "smart_comment",  "weight": 0,  "label": "💬 Комментарий (v2)"},  # weight=0: не выбирается рандомно, вставляется из очереди
]

SAVED_MESSAGES = [
    "ок", "👍", "✅", "нагадати", "перевірити", "зробити",
    "купити", "📌", "🔖", "!", "...", "++", "потім",
    "завтра", "важливо", "прочитати", "📎", "💡", "не забути",
    "ссылка", "todo", "check", "done", "⭐",
]

REPLY_MESSAGES = [
    "👍", "ок", "окк", "добре", "зрозумів", "дякую", "спс",
    "👌", "✅", "🤝", "ага", "да", "принял", "буду",
    "хорошо", "лан", "ладно", "+", ")", "🙏",
]

REACTION_EMOJIS = ["👍", "🔥", "❤️", "😁", "🎉", "🤩", "👏", "💯", "😍", "🤔"]

POPULAR_CHANNELS = [
    "telegram", "durov", "tginfo", "interface", "design_channel",
    "techcrunch", "bbcnews", "caborka", "exploitex", "habr_com",
]

SEARCH_WORDS = [
    "новости", "crypto", "погода", "рецепт", "фильм", "музыка",
    "спорт", "технологии", "бизнес", "путешествия", "кот", "мемы",
]

REST_CHANCE = 0.15  # 15% шанс дня отдыха

# Per-action-type daily limits
MAX_DAILY_PER_ACTION = {
    "join_channel": 3,
    "set_reaction": 30,
    "send_saved": 15,
    "reply_dm": 10,
}

# Track per-account per-action daily counts: {task_id: {action_name: count}}
_action_daily_counts: dict[int, dict[str, int]] = {}
_action_daily_date: dict[int, object] = {}


def _check_action_daily_limit(task_id: int, action_name: str) -> bool:
    """Returns True if action is under daily limit for this task."""
    from datetime import date
    today = date.today()
    if _action_daily_date.get(task_id) != today:
        _action_daily_counts[task_id] = defaultdict(int)
        _action_daily_date[task_id] = today

    limit = MAX_DAILY_PER_ACTION.get(action_name)
    if limit is None:
        return True
    return _action_daily_counts[task_id][action_name] < limit


def _increment_action_daily(task_id: int, action_name: str):
    from datetime import date
    today = date.today()
    if _action_daily_date.get(task_id) != today:
        _action_daily_counts[task_id] = defaultdict(int)
        _action_daily_date[task_id] = today
    _action_daily_counts[task_id][action_name] += 1

# ── Сессии — расписание "живого" человека ────────────────────

# ── Базовые сессии — время будет сдвигаться рандомно ─────────

SESSIONS = [
    {
        "name": "morning",
        "label": "🌅 Утренняя проверка",
        "hour_start": 8, "hour_end": 10,
        "actions_min": 5, "actions_max": 10,
        "delay_min": 10, "delay_max": 30,
        "chance": 0.70,          # 30% шанс пропустить
        "shift_range": (-60, 90),  # сдвиг ±60-90 минут
    },
    {
        "name": "mini_1",
        "label": "📱 Мини-проверка",
        "hour_start": 10, "hour_end": 12,
        "actions_min": 1, "actions_max": 3,
        "delay_min": 5, "delay_max": 15,
        "chance": 0.50,
        "shift_range": (-30, 60),
    },
    {
        "name": "lunch",
        "label": "🍔 Обеденный залип",
        "hour_start": 12, "hour_end": 14,
        "actions_min": 8, "actions_max": 15,
        "delay_min": 15, "delay_max": 45,
        "chance": 0.65,
        "shift_range": (-45, 75),
    },
    {
        "name": "mini_2",
        "label": "📱 Мини-проверка",
        "hour_start": 14, "hour_end": 16,
        "actions_min": 1, "actions_max": 3,
        "delay_min": 5, "delay_max": 15,
        "chance": 0.40,
        "shift_range": (-30, 60),
    },
    {
        "name": "mini_3",
        "label": "📱 Мини-проверка",
        "hour_start": 16, "hour_end": 19,
        "actions_min": 2, "actions_max": 5,
        "delay_min": 10, "delay_max": 30,
        "chance": 0.50,
        "shift_range": (-45, 60),
    },
    {
        "name": "evening",
        "label": "🌙 Вечерний сёрфинг",
        "hour_start": 19, "hour_end": 24,
        "actions_min": 10, "actions_max": 25,
        "delay_min": 10, "delay_max": 60,
        "chance": 0.80,          # 20% шанс пропустить вечер
        "shift_range": (-60, 60),
    },
]

# Типы дней — не каждый день одинаковый
DAY_TYPES = [
    {"name": "normal",   "weight": 40, "session_mult": 1.0, "label": "Обычный день"},
    {"name": "active",   "weight": 20, "session_mult": 1.3, "label": "Активный день"},
    {"name": "lazy",     "weight": 20, "session_mult": 0.5, "label": "Ленивый день — только 1-2 захода"},
    {"name": "random",   "weight": 15, "session_mult": 0.8, "label": "Хаотичный день — рандомные часы"},
    {"name": "rest",     "weight": 5,  "session_mult": 0.0, "label": "День отдыха — не заходит"},
]

DAY_MULTIPLIER = {
    1: 0.4,
    2: 0.55,
    3: 0.7,
    4: 0.8,
    5: 0.9,
    6: 1.0,
    7: 1.0,
}

MODE_MULTIPLIER = {
    "careful": 0.6,
    "normal": 1.0,
    "aggressive": 1.4,
}


# ═══════════════════════════════════════════════════════════
# ВЫБОР ДЕЙСТВИЯ
# ═══════════════════════════════════════════════════════════

def pick_action() -> dict:
    total = sum(a["weight"] for a in ACTIONS)
    r = random.randint(1, total)
    cumulative = 0
    for action in ACTIONS:
        cumulative += action["weight"]
        if r <= cumulative:
            return action
    return ACTIONS[0]


def pick_day_type() -> dict:
    """Выбирает тип дня с учётом весов."""
    total = sum(d["weight"] for d in DAY_TYPES)
    r = random.randint(1, total)
    cumulative = 0
    for dt in DAY_TYPES:
        cumulative += dt["weight"]
        if r <= cumulative:
            return dt
    return DAY_TYPES[0]


def get_current_session(hour: int) -> dict | None:
    """Какая сессия сейчас — с учётом рандомного сдвига времени."""
    for s in SESSIONS:
        # Сдвигаем время сессии рандомно
        shift_min = random.randint(s["shift_range"][0], s["shift_range"][1])
        shifted_start = s["hour_start"] + shift_min / 60
        shifted_end = s["hour_end"] + shift_min / 60

        if shifted_start <= hour < shifted_end:
            return s
    return None


def calc_session_actions(session: dict, day: int, mode: str) -> int:
    """Сколько действий для этой сессии с учётом дня и режима."""
    base = random.randint(session["actions_min"], session["actions_max"])
    day_mult = DAY_MULTIPLIER.get(day, 1.0)
    mode_mult = MODE_MULTIPLIER.get(mode, 1.0)
    result = int(base * day_mult * mode_mult)
    return max(result, 1)


# ═══════════════════════════════════════════════════════════
# ДЕЙСТВИЯ (Telegram)
# ═══════════════════════════════════════════════════════════

async def _do_read_feed(client, phone):
    dialogs = await client.get_dialogs(limit=15)
    if not dialogs:
        return "Нет диалогов", ""
    dialog = random.choice(dialogs)
    messages = await client.get_messages(dialog, limit=random.randint(3, 10))
    for msg in messages:
        await client.send_read_acknowledge(dialog, msg)
        await asyncio.sleep(random.uniform(0.5, 2))
    return f"Прочитал {len(messages)} сообщений в «{dialog.name or '?'}»", dialog.name or ""


async def _do_reaction(client, phone):
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import ReactionEmoji

    dialogs = await client.get_dialogs(limit=20)
    channels = [d for d in dialogs if d.is_channel and not d.is_group]
    if not channels:
        return "Нет каналов для реакции", ""

    ch = random.choice(channels)
    messages = await client.get_messages(ch, limit=5)
    if not messages:
        return "Нет постов", ch.name or ""

    msg = random.choice(messages)
    emoji = random.choice(REACTION_EMOJIS)

    try:
        await client(SendReactionRequest(
            peer=ch.entity,
            msg_id=msg.id,
            reaction=[ReactionEmoji(emoticon=emoji)],
        ))
        return f"Поставил {emoji} в «{ch.name or '?'}»", ch.name or ""
    except Exception as e:
        if "REACTION_INVALID" in str(e):
            return "Реакции отключены", ch.name or ""
        raise


async def _do_view_stories(client, phone):
    try:
        from telethon.tl.functions.stories import GetAllStoriesRequest
        result = await client(GetAllStoriesRequest(next=False, hidden=False))
        count = len(result.peer_stories) if hasattr(result, 'peer_stories') else 0
        return f"Просмотрел Stories ({count} пиров)", ""
    except Exception:
        return "Stories недоступны", ""


async def _do_view_profile(client, phone):
    from telethon.tl.functions.users import GetFullUserRequest
    dialogs = await client.get_dialogs(limit=20)
    users = [d for d in dialogs if d.is_user and not d.entity.bot]
    if not users:
        return "Нет контактов", ""
    user = random.choice(users)
    try:
        await client(GetFullUserRequest(user.entity))
        return f"Посмотрел профиль «{user.name or '?'}»", user.name or ""
    except:
        return "Профиль недоступен", ""


async def _do_typing(client, phone):
    from telethon.tl.functions.messages import SetTypingRequest
    from telethon.tl.types import SendMessageTypingAction
    me = await client.get_me()
    await client(SetTypingRequest(peer=me, action=SendMessageTypingAction()))
    await asyncio.sleep(random.uniform(2, 6))
    return "Печатал в Saved Messages", ""


async def _do_search(client, phone):
    from telethon.tl.functions.contacts import SearchRequest
    word = random.choice(SEARCH_WORDS)
    try:
        await client(SearchRequest(q=word, limit=5))
        return f"Поискал «{word}»", ""
    except:
        return f"Поиск «{word}» — ошибка", ""


async def _do_join_channel(client, phone):
    from telethon.tl.functions.channels import JoinChannelRequest
    ch_name = random.choice(POPULAR_CHANNELS)
    try:
        entity = await client.get_entity(ch_name)
        await client(JoinChannelRequest(entity))
        return f"Вступил в @{ch_name}", ch_name
    except:
        return f"Не удалось вступить в @{ch_name}", ch_name


async def _do_forward_saved(client, phone):
    dialogs = await client.get_dialogs(limit=15)
    channels = [d for d in dialogs if d.is_channel]
    if not channels:
        return "Нет каналов", ""
    ch = random.choice(channels)
    messages = await client.get_messages(ch, limit=5)
    if not messages:
        return "Нет постов", ch.name or ""
    msg = random.choice(messages)
    me = await client.get_me()
    await client.forward_messages(me, msg)
    return f"Переслал пост из «{ch.name or '?'}» в Saved", ch.name or ""

async def _do_send_saved(client, phone):
    """Пишет случайное сообщение в Saved Messages."""
    me = await client.get_me()
    msg = random.choice(SAVED_MESSAGES)
    await client.send_message(me, msg)
    return f"Написал «{msg}» в Saved", ""


async def _do_reply_dm(client, phone):
    """Отвечает на последнее непрочитанное ЛС."""
    dialogs = await client.get_dialogs(limit=20)
    # Ищем непрочитанные ЛС (не боты, не каналы)
    unread = [d for d in dialogs if d.is_user and not d.entity.bot and d.unread_count > 0]

    if not unread:
        # Нет непрочитанных — просто читаем случайный диалог
        users = [d for d in dialogs if d.is_user and not d.entity.bot]
        if not users:
            return "Нет личных чатов", ""
        d = random.choice(users)
        msgs = await client.get_messages(d, limit=3)
        for m in msgs:
            await client.send_read_acknowledge(d, m)
        return f"Прочитал ЛС от «{d.name or '?'}»", d.name or ""

    # Есть непрочитанное — отвечаем
    d = random.choice(unread)
    reply = random.choice(REPLY_MESSAGES)

    # Иногда (30%) отвечаем стикером вместо текста
    if random.random() < 0.3:
        try:
            # Получаем стикерпак
            from telethon.tl.functions.messages import GetAllStickersRequest
            stickers = await client(GetAllStickersRequest(0))
            if stickers.sets:
                from telethon.tl.functions.messages import GetStickerSetRequest
                from telethon.tl.types import InputStickerSetID
                pack = random.choice(stickers.sets[:5])
                sticker_set = await client(GetStickerSetRequest(
                    stickerset=InputStickerSetID(id=pack.id, access_hash=pack.access_hash),
                    hash=0
                ))
                if sticker_set.documents:
                    sticker = random.choice(sticker_set.documents[:10])
                    await client.send_file(d.entity, sticker)
                    await client.send_read_acknowledge(d, await client.get_messages(d, limit=1))
                    return f"Отправил стикер в ЛС «{d.name or '?'}»", d.name or ""
        except:
            pass  # Если стикеры не получилось — отвечаем текстом

    await client.send_message(d.entity, reply)
    await client.send_read_acknowledge(d, await client.get_messages(d, limit=1))
    return f"Ответил «{reply}» в ЛС «{d.name or '?'}»", d.name or ""

async def _do_smart_comment_warmup(client, phone, queue_item=None):
    """
    Выполняет комментарий из очереди как часть warmup сессии.
    Если queue_item=None — просто пропускает.
    """
    if not queue_item:
        return "Нет задач в очереди", ""

    from tasks.comment_executor import _do_smart_comment
    from services.llm import generate_comment, build_comment_prompt

    personality = queue_item.personality or {}
    style = queue_item.style or {}
    prompt = build_comment_prompt(queue_item.post_text, style, personality)

    # Генерируем комментарий
    provider = "claude"  # default, кампания подставит свой
    try:
        from sqlalchemy import select
        # provider берём из personality если есть
        comment_text = generate_comment(provider, prompt, queue_item.post_text)
        if not comment_text:
            queue_item.status = "failed"
            queue_item.error = "LLM не сгенерировал"
            return "LLM ошибка", ""
    except Exception as e:
        queue_item.status = "failed"
        queue_item.error = str(e)[:200]
        return f"LLM ошибка: {e}", ""

    status, detail = await _do_smart_comment(
        client, None, queue_item.channel, queue_item.post_id, comment_text, personality
    )

    queue_item.comment_text = comment_text
    queue_item.executed_at = datetime.utcnow()

    if status == "aborted":
        queue_item.status = "aborted"
        queue_item.error = detail
        return f"Комментарий отменён (передумал)", queue_item.channel
    elif status == "ok":
        queue_item.status = "done"
        return f"Комментарий: {comment_text[:50]}...", queue_item.channel
    else:
        queue_item.status = "failed"
        queue_item.error = detail
        return f"Ошибка: {detail}", queue_item.channel


ACTION_FNS = {
    "read_feed":     _do_read_feed,
    "set_reaction":  _do_reaction,
    "view_stories":  _do_view_stories,
    "view_profile":  _do_view_profile,
    "typing":        _do_typing,
    "search":        _do_search,
    "join_channel":  _do_join_channel,
    "forward_saved": _do_forward_saved,
    "send_saved":    _do_send_saved,
    "reply_dm":      _do_reply_dm,
    "smart_comment": _do_smart_comment_warmup,
}


# ═══════════════════════════════════════════════════════════
# ВЫПОЛНЕНИЕ СЕССИИ
# ═══════════════════════════════════════════════════════════

async def _run_session(task_row, account, proxy, session_cfg, db):
    """
    Выполняет целую сессию — несколько действий подряд с короткими паузами.
    Имитирует: человек достал телефон, полистал 5 минут, убрал.
    """
    from utils.telegram import make_telethon_client
    from models.warmup_log import WarmupLog

    phone = account.phone
    day = getattr(task_row, 'day', 1) or 1
    mode = task_row.mode or "normal"

    # Сколько действий в этой сессии
    num_actions = calc_session_actions(session_cfg, day, mode)

    logger.info(f"[warmup][{phone}] ═══ Начало сессии «{session_cfg['label']}» — {num_actions} действий (день {day}, {mode})")

    # Логируем старт сессии
    log = WarmupLog(
        task_id=task_row.id, account_id=account.id,
        action="session_start",
        detail=f"Сессия «{session_cfg['label']}»: {num_actions} действий (день {day})",
        success=True,
    )
    db.add(log)

    client = make_telethon_client(account, proxy)
    if not client:
        log = WarmupLog(task_id=task_row.id, account_id=account.id,
                        action="error", detail="Нет session файла", success=False)
        db.add(log)
        return 0

    done = 0

    try:
        await client.connect()
        if not await client.is_user_authorized():
            log = WarmupLog(task_id=task_row.id, account_id=account.id,
                            action="error", detail="Не авторизован", success=False)
            db.add(log)
            return 0

        # ── Проверяем есть ли pending комментарии для этого аккаунта ──
        pending_comment = None
        comment_inserted = False
        try:
            from sqlalchemy import select
            from models.comment_queue import CommentQueue
            cq_result = await db.execute(
                select(CommentQueue).where(
                    CommentQueue.account_id == account.id,
                    CommentQueue.status == "scheduled",
                    CommentQueue.scheduled_at <= datetime.utcnow(),
                ).order_by(CommentQueue.scheduled_at.asc()).limit(1)
            )
            pending_comment = cq_result.scalar_one_or_none()
            if pending_comment:
                pending_comment.status = "executing"
                await db.flush()
                logger.info(f"[warmup][{phone}] Найден pending комментарий #{pending_comment.id} → @{pending_comment.channel}")
        except Exception as e:
            logger.warning(f"[warmup][{phone}] Ошибка проверки очереди: {e}")

        # Если есть pending — вставим smart_comment примерно в середину сессии
        comment_at_action = random.randint(max(1, num_actions // 3), max(2, num_actions * 2 // 3)) if pending_comment else -1

        for i in range(num_actions):
            # Проверяем дневной лимит
            if task_row.today_actions >= task_row.today_limit:
                logger.info(f"[warmup][{phone}] Дневной лимит достигнут ({task_row.today_limit})")
                break

            # Вставляем smart_comment в нужный момент
            if pending_comment and not comment_inserted and i == comment_at_action:
                action = {"name": "smart_comment", "weight": 0, "label": "💬 Комментарий (v2)"}
                action_fn = ACTION_FNS.get("smart_comment")
                comment_inserted = True
                try:
                    detail, channel = await action_fn(client, phone, queue_item=pending_comment)
                    log = WarmupLog(
                        task_id=task_row.id, account_id=account.id,
                        action="smart_comment", detail=detail,
                        channel=channel, success=(pending_comment.status == "done"),
                        created_at=datetime.utcnow(),
                    )
                    db.add(log)
                    await db.flush()
                    await db.commit()

                    if pending_comment.status == "done":
                        # Обновляем behavior
                        try:
                            from tasks.behavior_engine import get_or_create_behavior
                            behavior = await get_or_create_behavior(db, account.id, phone)
                            behavior.comments_today += 1
                            behavior.last_comment_at = datetime.utcnow()
                            channels_today = behavior.channels_commented_today or []
                            channels_today.append(pending_comment.channel)
                            behavior.channels_commented_today = channels_today

                            # Обновляем campaign comments_sent
                            from models.campaign import Campaign, CommentLog
                            camp_r = await db.execute(select(Campaign).where(Campaign.id == pending_comment.campaign_id))
                            camp = camp_r.scalar_one_or_none()
                            if camp:
                                camp.comments_sent += 1
                                db.add(CommentLog(
                                    campaign_id=camp.id, account_id=account.id,
                                    account_phone=phone, channel_username=pending_comment.channel,
                                    channel_title="", post_id=pending_comment.post_id,
                                    post_text=(pending_comment.post_text or "")[:500],
                                    comment_text=pending_comment.comment_text or "",
                                    llm_provider=_val(camp.llm_provider) if camp else "",
                                ))
                            await db.flush()
                            await db.commit()
                        except Exception as e:
                            logger.warning(f"[warmup][{phone}] Ошибка обновления behavior: {e}")

                    task_row.actions_done += 1
                    task_row.today_actions += 1
                    done += 1
                    logger.info(f"[warmup][{phone}]   [{done}/{num_actions}] {action['label']}: {detail}")
                except Exception as e:
                    logger.warning(f"[warmup][{phone}] Smart comment ошибка: {e}")
                    pending_comment.status = "failed"
                    pending_comment.error = str(e)[:200]
                    await db.flush()
                    await db.commit()

                if i < num_actions - 1:
                    delay = random.randint(session_cfg["delay_min"], session_cfg["delay_max"])
                    await asyncio.sleep(delay)
                continue

            action = pick_action()
            action_fn = ACTION_FNS.get(action["name"])
            if not action_fn:
                continue

            # Check per-action-type daily limit
            if not _check_action_daily_limit(task_row.id, action["name"]):
                logger.info(f"[warmup][{phone}]   {action['label']}: дневной лимит типа достигнут, пропуск")
                continue

            try:
                detail, channel = await action_fn(client, phone)

                emoji = ""
                if action["name"] == "set_reaction" and "Поставил" in detail:
                    parts = detail.split("Поставил ")
                    if len(parts) > 1:
                        emoji = parts[1].split(" ")[0]

                log = WarmupLog(
                    task_id=task_row.id, account_id=account.id,
                    action=action["name"], detail=detail, emoji=emoji,
                    channel=channel, success=True,
                    created_at=datetime.utcnow(),
                )
                db.add(log)
                await db.flush()
                await db.commit()

                # Обновляем счётчики
                task_row.actions_done += 1
                task_row.today_actions += 1
                _increment_action_daily(task_row.id, action["name"])
                if action["name"] == "read_feed": task_row.feeds_read += 1
                elif action["name"] == "set_reaction": task_row.reactions_set += 1
                elif action["name"] == "view_stories": task_row.stories_viewed += 1
                elif action["name"] == "join_channel": task_row.channels_joined += 1

                done += 1
                logger.info(f"[warmup][{phone}]   [{done}/{num_actions}] {action['label']}: {detail}")

            except Exception as e:
                err = str(e)
                if "FLOOD_WAIT" in err:
                    wait = int(re.search(r"(\d+)", err).group(1)) if re.search(r"(\d+)", err) else 60
                    logger.warning(f"[warmup][{phone}] FLOOD_WAIT_{wait} — sleeping and ending session early")
                    await asyncio.sleep(wait + random.randint(5, 15))
                    break
                elif "AUTH_KEY_UNREGISTERED" in err or "UserDeactivatedBan" in type(e).__name__:
                    logger.warning(f"[warmup][{phone}] Account frozen: {err[:80]}")
                    account.status = "frozen"
                    break
                elif "PEER_FLOOD" in err:
                    logger.warning(f"[warmup][{phone}] PEER_FLOOD — pausing for 24h")
                    now = datetime.utcnow()
                    task_row.next_action_at = now + timedelta(hours=24)
                    break

                log = WarmupLog(
                    task_id=task_row.id, account_id=account.id,
                    action=action["name"], detail=str(e)[:200],
                    success=False, error=str(e)[:200],
                    created_at=datetime.utcnow(),
                )
                db.add(log)
                await db.flush()
                await db.commit()
                logger.warning(f"[warmup][{phone}]   [{done}/{num_actions}] {action['label']}: ОШИБКА {str(e)[:80]}")

            # Пауза между действиями внутри сессии (10-60с)
            if i < num_actions - 1:
                delay = random.randint(session_cfg["delay_min"], session_cfg["delay_max"])
                logger.info(f"[warmup][{phone}]   ⏳ Пауза {delay}с...")
                await asyncio.sleep(delay)

    except Exception as e:
        log = WarmupLog(
            task_id=task_row.id, account_id=account.id,
            action="error", detail=f"Ошибка сессии: {str(e)[:200]}",
            success=False, error=str(e)[:200],
        )
        db.add(log)
        logger.error(f"[warmup][{phone}] Ошибка сессии: {e}")

    finally:
        try:
            await client.disconnect()
        except:
            pass

    # Логируем конец сессии
    log = WarmupLog(
        task_id=task_row.id, account_id=account.id,
        action="session_end",
        detail=f"Сессия завершена: {done}/{num_actions} действий",
        success=True,
    )
    db.add(log)

    task_row.updated_at = datetime.utcnow()

    logger.info(f"[warmup][{phone}] ═══ Сессия «{session_cfg['label']}» завершена: {done}/{num_actions}")

    return done


# ═══════════════════════════════════════════════════════════
# ГЛАВНЫЙ ОБРАБОТЧИК
# ═══════════════════════════════════════════════════════════

async def _process_all_warmups_v2():
    """
    Вызывается каждые 60 секунд.
    Для каждого аккаунта проверяет:
      - Сейчас время сессии?
      - Сессия уже была сегодня?
      - Если да — запускает сессию (несколько действий подряд)
      - Если нет — пропускает (waiting)
    """
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from config import DATABASE_URL
    from models.warmup import WarmupTask
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from models.warmup_log import WarmupLog

    engine = create_async_engine(DATABASE_URL, pool_size=2, max_overflow=0)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        try:
            result = await db.execute(
                select(WarmupTask).where(WarmupTask.status == "running")
            )
            tasks = result.scalars().all()

            if not tasks:
                await engine.dispose()
                return {"processed": 0}

            now = datetime.utcnow()
            hour = (now.hour + 3) % 24  
            processed = 0
            results = []

            for t in tasks:
                phone_tag = f"task#{t.id}"

                # ── Управление днями ─────────────────────
                if t.day_started_at:
                    hours_since = (now - t.day_started_at).total_seconds() / 3600
                    if hours_since >= 24:
                        t.day = (t.day or 1) + 1
                        t.day_started_at = now
                        t.today_actions = 0

                        # Выбираем ТИП дня
                        day_type = pick_day_type()

                        if day_type["name"] == "rest":
                            t.is_resting = True
                            t.today_limit = 0
                            logger.info(f"[warmup][{phone_tag}] День {t.day} — 😴 ПОЛНЫЙ ОТДЫХ")
                            log = WarmupLog(task_id=t.id, account_id=t.account_id,
                                            action="rest_day", detail=f"День {t.day} — {day_type['label']}", success=True)
                            db.add(log)
                        else:
                            t.is_resting = False
                            day_mult = DAY_MULTIPLIER.get(t.day, 1.0)
                            mode_mult = MODE_MULTIPLIER.get(t.mode, 1.0)
                            base_limit = random.randint(25, 50)
                            t.today_limit = int(base_limit * day_mult * mode_mult * day_type["session_mult"])
                            t.today_limit = max(t.today_limit, 3)  # минимум 3 действия

                            logger.info(f"[warmup][{phone_tag}] День {t.day} — {day_type['label']} — лимит {t.today_limit}")
                            log = WarmupLog(task_id=t.id, account_id=t.account_id,
                                            action="new_day",
                                            detail=f"День {t.day}: {day_type['label']}, лимит {t.today_limit}",
                                            success=True)
                            db.add(log)

                        # Завершение
                        total_days = getattr(t, 'total_days', 7) or 7
                        if t.day > total_days:
                            t.status = "finished"
                            t.finished_at = now
                            log = WarmupLog(task_id=t.id, account_id=t.account_id,
                                            action="finished", detail=f"Прогрев завершён ({total_days} дней)", success=True)
                            db.add(log)
                            logger.info(f"[warmup][{phone_tag}] ✅ Прогрев завершён")
                            continue
                else:
                    t.day_started_at = now
                    t.day = 1
                    day_type = pick_day_type()
                    # Первый день не может быть отдыхом
                    while day_type["name"] == "rest":
                        day_type = pick_day_type()
                    day_mult = DAY_MULTIPLIER.get(1, 0.4)
                    mode_mult = MODE_MULTIPLIER.get(t.mode, 1.0)
                    t.today_limit = int(random.randint(25, 50) * day_mult * mode_mult * day_type["session_mult"])
                    t.today_limit = max(t.today_limit, 3)
                    t.today_actions = 0
                    t.is_resting = False
                    log = WarmupLog(task_id=t.id, account_id=t.account_id,
                                    action="new_day",
                                    detail=f"День 1: {day_type['label']}, лимит {t.today_limit}",
                                    success=True)
                    db.add(log)

                # День отдыха — пропускаем
                if t.is_resting:
                    results.append({"status": "resting", "task_id": t.id})
                    continue

                # Ночь — спим
                if hour < 8 or hour >= 24:
                    results.append({"status": "sleeping", "task_id": t.id})
                    continue

                # Дневной лимит достигнут
                if t.today_actions >= (t.today_limit or 999):
                    results.append({"status": "daily_limit", "task_id": t.id})
                    continue

                # ── Ещё не время? (offset при старте) ────
                if t.next_action_at and now < t.next_action_at:
                    results.append({"status": "waiting", "task_id": t.id})
                    continue

                # ── Определяем текущую сессию ────────────
                session_cfg = get_current_session(hour)
                if not session_cfg:
                    results.append({"status": "no_session", "task_id": t.id})
                    continue

                # Шанс что сессия вообще будет
                if random.random() > session_cfg["chance"]:
                    # Пропускаем сессию — следующая проверка через 30-60 мин
                    t.next_action_at = now + timedelta(minutes=random.randint(30, 60))
                    results.append({"status": "skipped_session", "task_id": t.id})
                    logger.info(f"[warmup][{phone_tag}] Сессия «{session_cfg['label']}» пропущена (рандом)")
                    continue

                # Проверяем не было ли уже этой сессии сегодня
                existing_log = await db.execute(
                    select(WarmupLog).where(
                        WarmupLog.task_id == t.id,
                        WarmupLog.action == "session_start",
                        WarmupLog.detail.contains(session_cfg["label"]),
                        WarmupLog.created_at >= t.day_started_at,
                    ).limit(1)
                )
                if existing_log.scalar_one_or_none():
                    # Эта сессия уже была сегодня — ждём следующую
                    t.next_action_at = now + timedelta(minutes=random.randint(20, 40))
                    results.append({"status": "session_done_today", "task_id": t.id})
                    continue

                # ── Загружаем аккаунт ────────────────────
                acc_r = await db.execute(
                    select(TelegramAccount)
                    .options(joinedload(TelegramAccount.api_app))
                    .where(TelegramAccount.id == t.account_id)
                )
                acc = acc_r.scalar_one_or_none()
                if not acc or acc.status not in ("active", "unknown"):
                    results.append({"status": "inactive", "task_id": t.id})
                    continue

                proxy = None
                if acc.proxy_id:
                    proxy_r = await db.execute(select(Proxy).where(Proxy.id == acc.proxy_id))
                    proxy = proxy_r.scalar_one_or_none()

                # ── ЗАПУСКАЕМ СЕССИЮ ─────────────────────
                done = await _run_session(t, acc, proxy, session_cfg, db)

                # После сессии — тишина до следующей (1-3 часа)
                silence = random.randint(60, 180)
                t.next_action_at = now + timedelta(minutes=silence)

                results.append({
                    "status": "session_done",
                    "task_id": t.id,
                    "session": session_cfg["label"],
                    "actions": done,
                    "next_in_min": silence,
                })
                processed += 1

            await db.commit()
            await engine.dispose()
            return {"processed": processed, "results": results}

        except Exception as e:
            logger.error(f"Ошибка прогрева v2: {e}")
            await db.rollback()
            await engine.dispose()
            return {"error": str(e)}


@celery_app.task(bind=True, name="tasks.warmup_v2.process_warmups_v2")
def process_warmups_v2(self):
    """Вызывается каждые 60 секунд из run_periodic.py."""
    self.update_state(state="PROGRESS", meta={"message": "Прогрев v2..."})
    return run_async(_process_all_warmups_v2())