"""
GramGPT API — tasks/warmup_v2.py
Прогрев аккаунтов v2 — имитация живого человека.

Принципы:
  - Активность 8:00–23:00, ночью спим
  - Градация: день 1 = 2-5 действий, день 7 = 15-25
  - Случайный порядок действий с весами
  - Паузы 30с–15мин между действиями
  - 15% шанс дня отдыха
  - Разное время старта для каждого аккаунта
"""

import asyncio
import random
import sys
import os
import logging
from datetime import datetime, timedelta

from celery_app import celery_app

logger = logging.getLogger(__name__)

API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Конфигурация ─────────────────────────────────────────────

ACTIONS = [
    {"name": "read_feed",      "weight": 30, "label": "📖 Чтение ленты"},
    {"name": "set_reaction",   "weight": 20, "label": "😍 Реакция"},
    {"name": "view_stories",   "weight": 15, "label": "👁 Просмотр Stories"},
    {"name": "view_profile",   "weight": 10, "label": "👤 Просмотр профиля"},
    {"name": "typing",         "weight": 10, "label": "⌨️ Печатает"},
    {"name": "search",         "weight": 5,  "label": "🔍 Поиск"},
    {"name": "join_channel",   "weight": 5,  "label": "📢 Вступление в канал"},
    {"name": "forward_saved",  "weight": 5,  "label": "💾 Пересылка в Saved"},
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

# Лимиты по дням (day → (min_actions, max_actions))
DAY_LIMITS = {
    1: (2, 5),
    2: (3, 7),
    3: (5, 10),
    4: (7, 14),
    5: (10, 18),
    6: (12, 22),
    7: (15, 25),
}

REST_CHANCE = 0.15  # 15% шанс дня отдыха
ACTIVE_HOURS = (8, 23)  # Активность с 8 до 23


# ── Выбор действия ───────────────────────────────────────────

def pick_action() -> dict:
    """Выбирает случайное действие с учётом весов."""
    total = sum(a["weight"] for a in ACTIONS)
    r = random.randint(1, total)
    cumulative = 0
    for action in ACTIONS:
        cumulative += action["weight"]
        if r <= cumulative:
            return action
    return ACTIONS[0]


def get_day_limit(day: int) -> tuple:
    """Лимит действий для конкретного дня."""
    if day in DAY_LIMITS:
        return DAY_LIMITS[day]
    return (15, 25)  # после 7го дня — стабильно


def random_delay() -> int:
    """Случайная задержка между действиями (секунды)."""
    r = random.random()
    if r < 0.3:
        return random.randint(30, 120)       # 30% — 30с–2мин
    elif r < 0.6:
        return random.randint(120, 300)      # 30% — 2–5мин
    elif r < 0.85:
        return random.randint(300, 600)      # 25% — 5–10мин
    else:
        return random.randint(600, 900)      # 15% — 10–15мин


# ── Действия ─────────────────────────────────────────────────

async def _do_read_feed(client, phone):
    """Читает последние сообщения в случайном диалоге."""
    dialogs = await client.get_dialogs(limit=15)
    if not dialogs:
        return "Нет диалогов", ""
    dialog = random.choice(dialogs)
    messages = await client.get_messages(dialog, limit=random.randint(3, 10))
    for msg in messages:
        await client.send_read_acknowledge(dialog, msg)
        await asyncio.sleep(random.uniform(0.5, 2))
    return f"Прочитал {len(messages)} сообщений", dialog.name or ""


async def _do_reaction(client, phone):
    """Ставит случайную реакцию на пост в случайном канале."""
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
        return f"Поставил {emoji} на пост", ch.name or ""
    except Exception as e:
        if "REACTION_INVALID" in str(e):
            return f"Реакции отключены", ch.name or ""
        raise


async def _do_view_stories(client, phone):
    """Просматривает Stories контактов."""
    try:
        from telethon.tl.functions.stories import GetAllStoriesRequest
        result = await client(GetAllStoriesRequest(next=False, hidden=False))
        count = len(result.peer_stories) if hasattr(result, 'peer_stories') else 0
        return f"Просмотрел Stories ({count} пиров)", ""
    except Exception:
        return "Stories недоступны", ""


async def _do_view_profile(client, phone):
    """Открывает случайный профиль."""
    from telethon.tl.functions.users import GetFullUserRequest
    dialogs = await client.get_dialogs(limit=20)
    users = [d for d in dialogs if d.is_user and not d.entity.bot]
    if not users:
        return "Нет контактов", ""

    user = random.choice(users)
    try:
        await client(GetFullUserRequest(user.entity))
        return f"Посмотрел профиль", user.name or ""
    except:
        return "Профиль недоступен", ""


async def _do_typing(client, phone):
    """Имитация набора текста в Saved Messages."""
    from telethon.tl.functions.messages import SetTypingRequest
    from telethon.tl.types import SendMessageTypingAction
    me = await client.get_me()
    await client(SetTypingRequest(
        peer=me,
        action=SendMessageTypingAction(),
    ))
    await asyncio.sleep(random.uniform(2, 6))
    return "Печатал в Saved Messages", ""


async def _do_search(client, phone):
    """Поиск в Telegram."""
    from telethon.tl.functions.contacts import SearchRequest
    word = random.choice(SEARCH_WORDS)
    try:
        await client(SearchRequest(q=word, limit=5))
        return f"Поискал: «{word}»", ""
    except:
        return f"Поиск: «{word}» — ошибка", ""


async def _do_join_channel(client, phone):
    """Вступает в случайный популярный канал."""
    from telethon.tl.functions.channels import JoinChannelRequest
    ch_name = random.choice(POPULAR_CHANNELS)
    try:
        entity = await client.get_entity(ch_name)
        await client(JoinChannelRequest(entity))
        return f"Вступил в @{ch_name}", ch_name
    except:
        return f"Не удалось вступить в @{ch_name}", ch_name


async def _do_forward_saved(client, phone):
    """Пересылает случайный пост в Saved Messages."""
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
    return "Переслал пост в Saved", ch.name or ""


ACTION_FNS = {
    "read_feed":     _do_read_feed,
    "set_reaction":  _do_reaction,
    "view_stories":  _do_view_stories,
    "view_profile":  _do_view_profile,
    "typing":        _do_typing,
    "search":        _do_search,
    "join_channel":  _do_join_channel,
    "forward_saved": _do_forward_saved,
}


# ── Обработка одного аккаунта ────────────────────────────────

async def _warmup_single(task_row, account, proxy, db):
    """Выполняет ОДНО действие прогрева для аккаунта."""
    from utils.telegram import make_telethon_client
    from models.warmup_log import WarmupLog

    phone = account.phone
    now = datetime.utcnow()
    hour = now.hour

    # Проверка: активные часы (8–23)
    if hour < ACTIVE_HOURS[0] or hour >= ACTIVE_HOURS[1]:
        return {"status": "sleeping", "phone": phone}

    # Проверка: день отдыха
    if task_row.is_resting:
        return {"status": "resting", "phone": phone}

    # Проверка: ещё не время (offset не прошёл)
    if task_row.next_action_at and now < task_row.next_action_at:
        return {"status": "waiting", "phone": phone}

    # Проверка: дневной лимит
    if task_row.today_actions >= task_row.today_limit:
        return {"status": "daily_limit", "phone": phone}

    # Выбираем действие
    action = pick_action()
    action_fn = ACTION_FNS.get(action["name"])
    if not action_fn:
        return {"status": "no_action", "phone": phone}

    client = make_telethon_client(account, proxy)
    if not client:
        log = WarmupLog(task_id=task_row.id, account_id=account.id, action="error",
                        detail="Нет session файла", success=False)
        db.add(log)
        return {"status": "no_session", "phone": phone}

    try:
        await client.connect()
        if not await client.is_user_authorized():
            log = WarmupLog(task_id=task_row.id, account_id=account.id, action="error",
                            detail="Не авторизован", success=False)
            db.add(log)
            return {"status": "not_authorized", "phone": phone}

        # Выполняем действие
        detail, channel = await action_fn(client, phone)

        # Определяем эмодзи для лога
        emoji = ""
        if action["name"] == "set_reaction" and "Поставил" in detail:
            emoji = detail.split("Поставил ")[1].split(" ")[0] if "Поставил " in detail else ""

        # Логируем
        log = WarmupLog(
            task_id=task_row.id, account_id=account.id,
            action=action["name"], detail=detail, emoji=emoji,
            channel=channel, success=True,
        )
        db.add(log)

        # Обновляем счётчики
        task_row.actions_done += 1
        task_row.today_actions += 1
        task_row.updated_at = now

        # Обновляем специфические счётчики
        if action["name"] == "read_feed":
            task_row.feeds_read += 1
        elif action["name"] == "set_reaction":
            task_row.reactions_set += 1
        elif action["name"] == "view_stories":
            task_row.stories_viewed += 1
        elif action["name"] == "join_channel":
            task_row.channels_joined += 1

        # Следующее действие через случайную паузу
        delay = random_delay()
        task_row.next_action_at = now + timedelta(seconds=delay)

        logger.info(f"[warmup][{phone}] {action['label']}: {detail} (след. через {delay}с)")

        return {"status": "ok", "phone": phone, "action": action["label"], "detail": detail, "next_delay": delay}

    except Exception as e:
        log = WarmupLog(
            task_id=task_row.id, account_id=account.id,
            action=action["name"], detail=str(e)[:200],
            success=False, error=str(e)[:200],
        )
        db.add(log)
        logger.error(f"[warmup][{phone}] Ошибка: {e}")
        task_row.next_action_at = now + timedelta(seconds=300)
        return {"status": "error", "phone": phone, "error": str(e)[:100]}

    finally:
        try:
            await client.disconnect()
        except:
            pass


# ── Обработка всех задач ─────────────────────────────────────

async def _process_all_warmups_v2():
    """Проверяет все активные прогревы и выполняет действия."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from config import DATABASE_URL
    from models.warmup import WarmupTask
    from models.account import TelegramAccount
    from models.proxy import Proxy

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
            processed = 0
            results = []

            for t in tasks:
                # ── Управление днями ─────────────────────
                # Новый день?
                if t.day_started_at:
                    hours_since = (now - t.day_started_at).total_seconds() / 3600
                    if hours_since >= 24:
                        t.day += 1
                        t.day_started_at = now
                        t.today_actions = 0
                        min_a, max_a = get_day_limit(t.day)
                        t.today_limit = random.randint(min_a, max_a)
                        t.is_resting = random.random() < REST_CHANCE

                        if t.is_resting:
                            logger.info(f"[warmup] Task #{t.id}: День {t.day} — ОТДЫХ 😴")
                        else:
                            logger.info(f"[warmup] Task #{t.id}: День {t.day} — лимит {t.today_limit} действий")

                        # Завершение после total_days
                        if t.day > (t.total_days or 7):
                            t.status = "finished"
                            t.finished_at = now
                            logger.info(f"[warmup] Task #{t.id}: Прогрев завершён ({t.total_days} дней)")
                            continue
                else:
                    # Первый запуск
                    t.day_started_at = now
                    t.day = 1
                    min_a, max_a = get_day_limit(1)
                    t.today_limit = random.randint(min_a, max_a)
                    t.is_resting = False

                # ── Загружаем аккаунт ────────────────────
                acc_r = await db.execute(
                    select(TelegramAccount)
                    .options(joinedload(TelegramAccount.api_app))
                    .where(TelegramAccount.id == t.account_id)
                )
                acc = acc_r.scalar_one_or_none()
                if not acc or acc.status not in ("active", "unknown"):
                    continue

                proxy = None
                if acc.proxy_id:
                    proxy_r = await db.execute(select(Proxy).where(Proxy.id == acc.proxy_id))
                    proxy = proxy_r.scalar_one_or_none()

                res = await _warmup_single(t, acc, proxy, db)
                results.append(res)
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
    """Запускается каждые 60 секунд Celery Beat."""
    self.update_state(state="PROGRESS", meta={"message": "Прогрев v2..."})
    return run_async(_process_all_warmups_v2())
