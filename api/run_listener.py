"""
GramGPT — run_listener.py
Persistent Telethon event listener для ЗАКРЫТЫХ каналов.
Клиент подключается ОДИН раз и пассивно слушает новые посты.
Telegram сам присылает уведомления — никакого polling.

Запуск: cd api && python run_listener.py

Для ПУБЛИЧНЫХ каналов используется веб-парсинг (run_periodic.py).
Этот скрипт ТОЛЬКО для закрытых каналов где веб-парсинг не работает.
"""

import asyncio
import sys
import os
import random
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s [LISTENER] %(message)s')
logger = logging.getLogger(__name__)

API_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.abspath(os.path.join(API_DIR, ".."))

if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(API_DIR, '.env'))


async def main():
    from sqlalchemy.orm import joinedload
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select
    from config import DATABASE_URL
    from models.campaign import Campaign, TargetChannel, CampaignStatus, CommentLog
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.telegram import make_telethon_client, _build_proxy, get_cli_config
    from services.channel_monitor import is_channel_public
    from telethon import events

    engine = create_async_engine(DATABASE_URL, pool_size=2, max_overflow=0)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    cli_config = get_cli_config()

    # ── Собираем данные: какие закрытые каналы слушать ────────
    logger.info("Загружаю активные кампании...")

    async with Session() as db:
        camp_r = await db.execute(
            select(Campaign).where(Campaign.status == CampaignStatus.active)
        )
        campaigns = camp_r.scalars().all()

        if not campaigns:
            logger.info("Нет активных кампаний. Жду...")
            await engine.dispose()
            return

        # Собираем все закрытые каналы + аккаунт + прокси
        private_channels = {}  # username → {campaign, channel, account, proxy}
        account_cache = {}     # id → (account, proxy)

        for c in campaigns:
            ch_r = await db.execute(
                select(TargetChannel).where(
                    TargetChannel.campaign_id == c.id,
                    TargetChannel.is_active == True,
                )
            )
            channels = ch_r.scalars().all()

            if not c.account_ids:
                continue

            # Кэшируем аккаунт
            acc_id = c.account_ids[0]  # Берём первый
            if acc_id not in account_cache:
                acc_r = await db.execute(select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(TelegramAccount.id == acc_id))
                account = acc_r.scalar_one_or_none()
                if account and account.session_file:
                    proxy = None
                    if account.proxy_id:
                        proxy_r = await db.execute(select(Proxy).where(Proxy.id == account.proxy_id))
                        proxy = proxy_r.scalar_one_or_none()
                    account_cache[acc_id] = (account, proxy)

            if acc_id not in account_cache:
                continue

            for ch in channels:
                if not ch.username:
                    continue

                # Проверяем — публичный или закрытый
                is_public = await is_channel_public(ch.username)

                if not is_public:
                    private_channels[ch.username] = {
                        "campaign": c,
                        "channel": ch,
                        "account_id": acc_id,
                    }
                    logger.info(f"  🔒 Закрытый: @{ch.username} (кампания: {c.name})")
                else:
                    logger.info(f"  🌐 Публичный: @{ch.username} → веб-парсинг")

    if not private_channels:
        logger.info("Нет закрытых каналов. Event listener не нужен.")
        await engine.dispose()
        return

    logger.info(f"Закрытых каналов: {len(private_channels)}")

    # ── Выбираем аккаунт для подключения ─────────────────────
    # Берём первый доступный аккаунт
    acc_id = list(private_channels.values())[0]["account_id"]
    account, proxy = account_cache[acc_id]

    proxy_info = f" через прокси {proxy.host}:{proxy.port}" if proxy else " напрямую"
    logger.info(f"Аккаунт: {account.phone}{proxy_info}")

    # ── Создаём persistent клиент ────────────────────────────
    client = make_telethon_client(account, proxy)
    if not client:
        logger.error("Не удалось создать клиент!")
        await engine.dispose()
        return

    await client.connect()
    if not await client.is_user_authorized():
        logger.error("Сессия не активна!")
        await client.disconnect()
        await engine.dispose()
        return

    logger.info("✅ Подключён к Telegram")

    # Резолвим каналы
    channel_entities = {}
    for username, info in private_channels.items():
        try:
            entity = await client.get_entity(username)
            channel_entities[entity.id] = {
                "username": username,
                "entity": entity,
                **info,
            }
            logger.info(f"  ✅ @{username} → id={entity.id}")
        except Exception as e:
            logger.error(f"  ❌ @{username}: {e}")

        await asyncio.sleep(1)

    if not channel_entities:
        logger.error("Не удалось подписаться ни на один канал!")
        await client.disconnect()
        await engine.dispose()
        return

    chat_ids = list(channel_entities.keys())
    logger.info(f"Слушаю {len(chat_ids)} каналов...")

    # ── Event handler ────────────────────────────────────────
    @client.on(events.NewMessage(chats=chat_ids))
    async def on_new_post(event):
        """Telegram САМ присылает новые посты — никакого polling!"""
        chat_id = event.chat_id
        info = channel_entities.get(chat_id)
        if not info:
            return

        username = info["username"]
        campaign = info["campaign"]
        channel = info["channel"]

        post_text = event.message.text or ""
        if not post_text or len(post_text) < 10:
            return

        msg_id = event.message.id
        logger.info(f"📨 Новый пост @{username} #{msg_id}: {post_text[:60]}...")

        # Обновляем last_post_id
        async with Session() as db2:
            ch_r = await db2.execute(
                select(TargetChannel).where(TargetChannel.id == channel.id)
            )
            ch = ch_r.scalar_one_or_none()
            if ch:
                ch.last_post_id = msg_id
                await db2.commit()

        # Триггер
        _val = lambda x: x.value if hasattr(x, 'value') else x
        mode = _val(campaign.trigger_mode)
        if mode == "random" and random.randint(1, 100) > campaign.trigger_percent:
            logger.info(f"  Пропуск (random {campaign.trigger_percent}%)")
            return
        if mode == "keywords":
            kws = campaign.trigger_keywords or []
            if not any(k.lower() in post_text.lower() for k in kws):
                logger.info(f"  Пропуск (keywords не совпали)")
                return

        # Проверяем есть ли комментарии у поста
        has_comments = event.message.replies and getattr(event.message.replies, 'comments', False)
        if not has_comments:
            logger.info(f"  Пропуск (комментарии отключены)")
            return

        # Задержка (имитация чтения)
        delay = min(campaign.delay_comment + random.randint(-20, 20), 60)
        if delay > 5:
            logger.info(f"  Задержка {delay}с...")
            await asyncio.sleep(delay)

        # LLM
        from tasks.commenting_tasks import call_llm, build_prompt
        prompt = build_prompt(_val(campaign.tone), campaign.comment_length, campaign.custom_prompt)
        provider = _val(campaign.llm_provider)
        comment = call_llm(provider, prompt, post_text)

        if not comment:
            logger.warning(f"  LLM пустой ответ")
            return

        logger.info(f"  LLM: {comment[:80]}...")

        # Отправляем коммент (тот же клиент — уже подключён!)
        try:
            await client.send_message(
                entity=info["entity"],
                message=comment,
                comment_to=msg_id,
            )
            logger.info(f"  ✅ Коммент отправлен в @{username} #{msg_id}")

            # Записываем в БД
            async with Session() as db3:
                camp_r = await db3.execute(select(Campaign).where(Campaign.id == campaign.id))
                camp = camp_r.scalar_one_or_none()
                if camp:
                    camp.comments_sent += 1

                ch_r = await db3.execute(select(TargetChannel).where(TargetChannel.id == channel.id))
                ch = ch_r.scalar_one_or_none()
                if ch:
                    ch.comments_sent += 1

                db3.add(CommentLog(
                    campaign_id=campaign.id, account_id=account.id,
                    account_phone=account.phone,
                    channel_username=username, channel_title=channel.title or "",
                    post_id=msg_id, post_text=post_text[:500],
                    comment_text=comment, llm_provider=provider,
                ))
                await db3.commit()

        except Exception as e:
            logger.error(f"  ❌ Ошибка отправки: {e}")

        # Пауза между комментариями
        between = max(campaign.delay_between + random.randint(-10, 10), 15)
        await asyncio.sleep(between)

    # ── Запуск (бесконечно слушаем) ──────────────────────────
    print()
    print("=" * 50)
    print("  🎧 Event Listener запущен")
    print(f"  Каналов: {len(chat_ids)}")
    print(f"  Аккаунт: {account.phone}")
    print(f"  Прокси: {proxy.host}:{proxy.port}" if proxy else "  Прокси: нет")
    print("  Ctrl+C для остановки")
    print("=" * 50)
    print()

    try:
        await client.run_until_disconnected()
    except KeyboardInterrupt:
        logger.info("👋 Listener остановлен")
    finally:
        await client.disconnect()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())