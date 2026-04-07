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

logger = logging.getLogger(__name__)
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try: return loop.run_until_complete(coro)
    finally: loop.close()


def _val(x):
    return x.value if hasattr(x, 'value') else x


async def _do_smart_comment(client, account, channel_username, post_id, comment_text, personality):
    """
    Человекоподобное комментирование:
    pre-read → пауза → реакция(40%) → typing → abort(10%) → send → post-read
    """
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

    # 2. Задержка — "читаем" целевой пост (НЕ БОЛЕЕ 2-3 мин)
    read_time = random.randint(30, 120)
    await asyncio.sleep(read_time)

    # 3. Иногда ставим реакцию перед комментарием (40%)
    if random.random() < 0.4:
        try:
            from telethon.tl.functions.messages import SendReactionRequest
            from telethon.tl.types import ReactionEmoji
            emoji = random.choice(["👍", "🔥", "❤️", "🤔", "👏"])
            await client(SendReactionRequest(
                peer=entity, msg_id=post_id,
                reaction=[ReactionEmoji(emoticon=emoji)]
            ))
            await asyncio.sleep(random.randint(5, 30))
        except Exception:
            pass

    # 4. Typing перед комментарием
    if personality.get("typing_before_comment", True):
        try:
            from telethon.tl.functions.messages import SetTypingRequest, GetDiscussionMessageRequest
            from telethon.tl.types import SendMessageTypingAction

            # Пробуем получить discussion group для typing
            try:
                disc = await client(GetDiscussionMessageRequest(peer=entity, msg_id=post_id))
                if disc and disc.messages:
                    discussion_peer = disc.messages[0].peer_id
                    typing_duration = random.randint(3, 12)
                    await client(SetTypingRequest(peer=discussion_peer, action=SendMessageTypingAction()))
                    await asyncio.sleep(typing_duration)
            except Exception:
                # Typing в сам канал если discussion недоступен
                typing_duration = random.randint(3, 12)
                await asyncio.sleep(typing_duration)
        except Exception:
            await asyncio.sleep(random.randint(3, 8))

    # 5. 10% шанс "передумал"
    if random.random() < 0.10:
        return "aborted", "Начал писать, передумал"

    # 6. Отправляем комментарий
    await client.send_message(entity=entity, message=comment_text, comment_to=post_id)

    # 7. После комментария — прочитать ещё 1-3 поста (не уходим сразу)
    await asyncio.sleep(random.randint(5, 20))
    more_posts = await client.get_messages(entity, limit=random.randint(1, 3))
    for p in more_posts:
        await client.send_read_acknowledge(entity, p)
        await asyncio.sleep(random.uniform(1, 3))

    return "ok", f"Комментарий отправлен в @{channel_username}"


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

    # Загружаем кампанию
    camp_r = await db.execute(select(Campaign).where(Campaign.id == item.campaign_id))
    campaign = camp_r.scalar_one_or_none()
    if not campaign or _val(campaign.status) != "active":
        item.status = "failed"
        item.error = "Кампания не активна"
        item.executed_at = now
        return

    # Behavior
    behavior = await get_or_create_behavior(db, account.id, account.phone)

    # Генерируем комментарий через LLM
    from services.llm import build_comment_prompt
    style_profile = item.style or {}
    personality = item.personality or {}

    prompt = build_comment_prompt(item.post_text, style_profile, personality)
    comment_text = generate_comment(_val(campaign.llm_provider), prompt, item.post_text)

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
            item.status = "failed"
            item.error = "Не авторизован"
            item.executed_at = now
            return

        status, detail = await _do_smart_comment(
            client, account, item.channel, item.post_id, comment_text, personality
        )

        item.executed_at = datetime.utcnow()
        item.comment_text = comment_text

        if status == "aborted":
            item.status = "aborted"
            item.error = detail
            logger.info(f"[executor] 🚫 {account.phone} → @{item.channel}: передумал")
            return

        if status == "ok":
            item.status = "done"

            # Обновляем счётчики кампании
            campaign.comments_sent += 1

            # Обновляем behavior
            behavior.comments_today += 1
            behavior.last_comment_at = datetime.utcnow()
            channels = behavior.channels_commented_today or []
            channels.append(item.channel)
            behavior.channels_commented_today = channels

            # Лог
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
            ))

            logger.info(f"[executor] ✅ {account.phone} → @{item.channel} #{item.post_id}: {comment_text[:50]}...")

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
            logger.warning(f"[executor] PEER_FLOOD — {account.phone}")
            item.status = "failed"
            item.error = "PEER_FLOOD"

        elif "AUTH_KEY_UNREGISTERED" in err or "UserDeactivatedBan" in type(e).__name__:
            logger.warning(f"[executor] Account frozen: {account.phone}")
            account.status = "frozen"
            item.status = "failed"
            item.error = "Account frozen"

        elif "CHANNEL_PRIVATE" in err or "private" in err.lower():
            logger.warning(f"[executor] @{item.channel}: нет доступа")
            item.status = "failed"
            item.error = "Channel private"

        elif "GetDiscussionMessage" in err or "MESSAGE_ID_INVALID" in err:
            logger.warning(f"[executor] @{item.channel} #{item.post_id}: комменты отключены")
            item.status = "failed"
            item.error = "Comments disabled"

        else:
            logger.error(f"[executor] ❌ {account.phone} → @{item.channel}: {e}")
            item.status = "failed"
            item.error = str(e)[:500]

    finally:
        try: await client.disconnect()
        except: pass
        release_account_lock(item.account_id)


async def _process_comment_queue():
    """Обрабатывает очередь комментариев — запускается каждые 60с."""
    if API_DIR not in sys.path: sys.path.insert(0, API_DIR)

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select
    from config import DATABASE_URL
    from models.comment_queue import CommentQueue

    engine = create_async_engine(DATABASE_URL, pool_size=2, max_overflow=0)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

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
        finally:
            await engine.dispose()


@celery_app.task(bind=True, name="tasks.comment_executor.process_comment_queue")
def process_comment_queue(self):
    """Обработка очереди комментариев — каждые 60с."""
    self.update_state(state="PROGRESS", meta={"message": "Обработка очереди..."})
    return run_async(_process_comment_queue())
