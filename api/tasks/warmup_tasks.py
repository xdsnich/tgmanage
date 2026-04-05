"""
GramGPT API — tasks/warmup_tasks.py
Прогрев аккаунтов: имитация действий живого человека.
Очередь: ai_dialogs
"""

import asyncio
import sys
import os
import random
import logging
import importlib.util
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
from celery_app import celery_app

logger = logging.getLogger(__name__)

API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))

MODE_LIMITS = {
    "careful":    {"actions_per_hour": 5,  "delay_min": 30, "delay_max": 120},
    "normal":     {"actions_per_hour": 15, "delay_min": 10, "delay_max": 60},
    "aggressive": {"actions_per_hour": 30, "delay_min": 5,  "delay_max": 30},
}

def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _get_cli_config():
    config_path = os.path.join(ROOT_DIR, "config.py")
    spec = importlib.util.spec_from_file_location("cli_config", config_path)
    cli_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli_config)
    return cli_config


REACTIONS = ["👍", "❤️", "🔥", "👏", "😂", "😮", "😢", "🎉", "🤔", "👎"]


async def _warmup_account(task_row, account_row):
    """Выполняет одно действие прогрева для аккаунта"""
async def _warmup_account(task_row, account_row, proxy_row=None):
    """Выполняет одно действие прогрева для аккаунта"""
    from telethon.tl.types import Channel

    phone = account_row.phone
    session_file = account_row.session_file

    if not session_file or not os.path.exists(session_file):
        return

    limits = MODE_LIMITS.get(task_row.mode, MODE_LIMITS["normal"])

    sys.path.insert(0, API_DIR)
    from utils.telegram import make_telethon_client
    client = make_telethon_client(account_row, proxy_row)
    if not client:
        return

    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.warning(f"[warmup][{phone}] Сессия не активна")
            return

        actions_done = 0

        # 1. Чтение ленты (скроллинг диалогов)
        if task_row.read_feed:
            try:
                dialogs = await client.get_dialogs(limit=10)
                for d in dialogs[:random.randint(3, 7)]:
                    try:
                        await client.get_messages(d, limit=random.randint(1, 5))
                        await asyncio.sleep(random.uniform(1, 3))
                    except:
                        pass
                task_row.feeds_read += 1
                actions_done += 1
                logger.info(f"[warmup][{phone}] Прочитал ленту ({len(dialogs)} диалогов)")
            except Exception as e:
                logger.error(f"[warmup][{phone}] Ошибка чтения ленты: {e}")

        await asyncio.sleep(random.uniform(2, 5))

        # 2. Просмотр Stories
        if task_row.view_stories:
            try:
                from telethon.tl.functions.stories import GetAllReadPeerStoriesRequest
                await client(GetAllReadPeerStoriesRequest())
                task_row.stories_viewed += 1
                actions_done += 1
                logger.info(f"[warmup][{phone}] Просмотрел Stories")
            except Exception as e:
                logger.info(f"[warmup][{phone}] Stories: {e}")

        await asyncio.sleep(random.uniform(2, 5))

        # 3. Реакции на посты
        if task_row.set_reactions:
            try:
                dialogs = await client.get_dialogs(limit=20)
                channels = [d for d in dialogs if isinstance(d.entity, Channel) and d.entity.broadcast]

                if channels:
                    ch = random.choice(channels[:5])
                    msgs = await client.get_messages(ch, limit=5)
                    for msg in msgs[:random.randint(1, 3)]:
                        if msg.text and len(msg.text) > 5:
                            try:
                                from telethon.tl.functions.messages import SendReactionRequest
                                from telethon.tl.types import ReactionEmoji
                                reaction = random.choice(REACTIONS)
                                await client(SendReactionRequest(
                                    peer=ch.entity,
                                    msg_id=msg.id,
                                    reaction=[ReactionEmoji(emoticon=reaction)],
                                ))
                                task_row.reactions_set += 1
                                actions_done += 1
                                logger.info(f"[warmup][{phone}] Поставил {reaction} в {ch.title}")
                                await asyncio.sleep(random.uniform(2, 5))
                            except:
                                pass
            except Exception as e:
                logger.info(f"[warmup][{phone}] Реакции: {e}")

        await asyncio.sleep(random.uniform(2, 5))

        # 4. Вступление в каналы
        if task_row.join_channels:
            try:
                from telethon.tl.functions.channels import JoinChannelRequest
                popular = ["telegram", "durov", "tginfo"]
                ch_username = random.choice(popular)
                try:
                    entity = await client.get_entity(ch_username)
                    await client(JoinChannelRequest(entity))
                    task_row.channels_joined += 1
                    actions_done += 1
                    logger.info(f"[warmup][{phone}] Вступил в @{ch_username}")
                except:
                    pass
            except Exception as e:
                logger.info(f"[warmup][{phone}] Вступление: {e}")

        task_row.actions_done += actions_done
        task_row.updated_at = datetime.utcnow()
        logger.info(f"[warmup][{phone}] Цикл завершён: {actions_done} действий (всего: {task_row.actions_done})")

    except Exception as e:
        logger.error(f"[warmup][{phone}] Ошибка: {e}")
    finally:
        try:
            await client.disconnect()
        except:
            pass


async def _process_all_warmups():
    """Находит все активные задачи прогрева и выполняет"""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select
    from config import DATABASE_URL
    from models.warmup import WarmupTask
    from models.account import TelegramAccount

    engine = create_async_engine(DATABASE_URL, pool_size=2, max_overflow=0)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        from sqlalchemy.orm import joinedload

        try:
            result = await db.execute(
                select(WarmupTask).where(WarmupTask.status == "running")
            )
            tasks = result.scalars().all()

            if not tasks:
                return {"processed": 0}

            logger.info(f"Активных прогревов: {len(tasks)}")

            processed = 0
            for t in tasks:
                acc_r = await db.execute(
                    select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(TelegramAccount.id == t.account_id)
                )
                acc = acc_r.scalar_one_or_none()
                if not acc or acc.status != "active":
                    continue

                # Загружаем прокси
                from models.proxy import Proxy
                proxy = None
                if hasattr(acc, 'proxy_id') and acc.proxy_id:
                    proxy_r = await db.execute(select(Proxy).where(Proxy.id == acc.proxy_id))
                    proxy = proxy_r.scalar_one_or_none()

                await _warmup_account(t, acc, proxy)
                processed += 1

            await db.commit()
            return {"processed": processed, "total": len(tasks)}

        except Exception as e:
            logger.error(f"Ошибка прогрева: {e}")
            await db.rollback()
            return {"error": str(e)}
        finally:
            await engine.dispose()


@celery_app.task(bind=True, name="tasks.warmup_tasks.process_warmups")
def process_warmups(self):
    """Запускается периодически — выполняет прогрев активных аккаунтов"""
    return run_async(_process_all_warmups())