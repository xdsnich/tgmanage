"""GramGPT — tasks/parser_tasks.py — парсинг каналов в фоне"""

import asyncio
import sys
import os
import logging
import random
import re
from datetime import datetime, timedelta

from celery_app import celery_app

logger = logging.getLogger(__name__)
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _save_channel(user_id, channel_data):
    """Сохраняет один канал в свою отдельную сессию БД"""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select
    from config import DATABASE_URL
    from models.parsed_channel import ParsedChannel

    engine = create_async_engine(DATABASE_URL, pool_size=1, max_overflow=0)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with Session() as db:
            existing = await db.execute(
                select(ParsedChannel).where(
                    ParsedChannel.user_id == user_id,
                    ParsedChannel.username == channel_data["username"],
                )
            )
            if existing.scalar_one_or_none():
                return False

            post_date = None
            if channel_data.get("last_post_date"):
                try:
                    dt = datetime.fromisoformat(channel_data["last_post_date"].replace("Z", ""))
                    post_date = dt.replace(tzinfo=None)
                except Exception:
                    pass

            ch_fields = {
                "user_id": user_id,
                "channel_id": channel_data.get("channel_id", 0),
                "username": channel_data["username"],
                "title": channel_data["title"],
                "subscribers": channel_data["subscribers"],
                "has_comments": channel_data["has_comments"],
                "last_post_date": post_date,
                "search_query": channel_data["search_query"],
            }
            # Добавляем source только если поле есть в модели
            if hasattr(ParsedChannel, 'source'):
                ch_fields["source"] = "telegram"

            db.add(ParsedChannel(**ch_fields))
            await db.commit()
            return True
    finally:
        await engine.dispose()


async def _run_parser(user_id: int, account_id: int, params: dict):
    """Запускает парсинг в фоне"""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from config import DATABASE_URL
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.telegram import make_telethon_client

    engine = create_async_engine(DATABASE_URL, pool_size=1, max_overflow=0)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    # Загружаем аккаунт
    async with Session() as db:
        acc_r = await db.execute(
            select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
            .where(TelegramAccount.id == account_id, TelegramAccount.user_id == user_id)
        )
        acc = acc_r.scalar_one_or_none()
        if not acc:
            return {"error": "Аккаунт не найден"}

        proxy = None
        if acc.proxy_id:
            proxy_r = await db.execute(select(Proxy).where(Proxy.id == acc.proxy_id))
            proxy = proxy_r.scalar_one_or_none()

        client = make_telethon_client(acc, proxy)
        if not client:
            return {"error": "Не удалось создать клиент"}

    await engine.dispose()

    import redis as redis_lib
    from config import DATABASE_URL
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r = redis_lib.from_url(redis_url)
    progress_key = f"parser:progress:{user_id}"
    stop_key = f"parser:stop:{user_id}"

    # Сбрасываем стоп-флаг
    r.delete(stop_key)

    keywords = [k.strip() for k in params["keywords"].split(",") if k.strip()]
    found = []
    saved_count = 0
    seen_usernames = set()

    # Начальный прогресс
    r.setex(progress_key, 3600, f"running|0|0|{len(keywords)}|старт")

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return {"error": "Аккаунт не авторизован"}

        from telethon.tl.functions.contacts import SearchRequest as TgSearchRequest

        for kw_idx, kw in enumerate(keywords, 1):
            # Проверка стоп-флага
            if r.get(stop_key):
                logger.info(f"🔍 [parser_task] Получен сигнал остановки")
                break

            if len(found) >= params["max_channels"]:
                break

            # Обновляем прогресс: status|found|saved|total_keywords|current_keyword
            r.setex(progress_key, 3600, f"running|{len(found)}|{saved_count}|{len(keywords)}|{kw}")
            logger.info(f"🔍 [parser_task] Поиск {kw_idx}/{len(keywords)}: '{kw}'")
            try:
                res = await client(TgSearchRequest(q=kw, limit=params["limit_per_keyword"]))

                for chat in res.chats:
                    if len(found) >= params["max_channels"]:
                        break
                    if not hasattr(chat, 'username') or not chat.username:
                        continue
                    if chat.username in seen_usernames:
                        continue

                    if params.get("name_endings"):
                        endings = [e.strip().lower() for e in params["name_endings"].split(",") if e.strip()]
                        if not any(chat.username.lower().endswith(e) for e in endings):
                            continue
                    if params.get("name_contains"):
                        parts = [p.strip().lower() for p in params["name_contains"].split(",") if p.strip()]
                        if not any(p in chat.username.lower() for p in parts):
                            continue

                    subs = getattr(chat, 'participants_count', 0) or 0
                    if subs == 0:
                        continue
                    if subs < params["min_subscribers"] or subs > params["max_subscribers"]:
                        continue

                    has_comments = False
                    last_post = None
                    try:
                        msgs = await client.get_messages(chat, limit=1)
                        if msgs:
                            last_post = msgs[0].date
                            if msgs[0].replies and getattr(msgs[0].replies, 'comments', False):
                                has_comments = True
                    except Exception:
                        pass

                    if params.get("only_with_comments") and not has_comments:
                        continue
                    if params.get("active_hours", 0) > 0 and last_post:
                        cutoff = datetime.utcnow() - timedelta(hours=params["active_hours"])
                        if last_post.replace(tzinfo=None) < cutoff:
                            continue

                    seen_usernames.add(chat.username)
                    ch_data = {
                        "channel_id": chat.id,
                        "username": chat.username,
                        "title": chat.title,
                        "subscribers": subs,
                        "has_comments": has_comments,
                        "last_post_date": last_post.isoformat() if last_post else None,
                        "search_query": kw,
                    }
                    found.append(ch_data)

                    # СОХРАНЯЕМ в отдельной сессии БД (не блокирует Telethon)
                    try:
                        saved = await _save_channel(user_id, ch_data)
                        if saved:
                            saved_count += 1
                            logger.info(f"  + @{chat.username} ({subs}) [сохранён]")
                        else:
                            logger.info(f"  + @{chat.username} (уже в БД)")
                        # Обновляем прогресс после каждого канала
                        r.setex(progress_key, 3600, f"running|{len(found)}|{saved_count}|{len(keywords)}|{kw}")
                    except Exception as e:
                        logger.warning(f"Save @{chat.username}: {e}")

                    await asyncio.sleep(random.uniform(
                        params.get("pause_between_channels_min", 0.8),
                        params.get("pause_between_channels_max", 1.5)
                    ))
            except Exception as e:
                err = str(e)
                if "FLOOD_WAIT" in err:
                    wait = int(re.search(r"(\d+)", err).group(1)) if re.search(r"(\d+)", err) else 60
                    logger.warning(f"🔍 FLOOD_WAIT_{wait} — прерываю поиск")
                    break
                logger.warning(f"🔍 Ошибка '{kw}': {e}")

            await asyncio.sleep(random.uniform(
                params.get("pause_between_keywords_min", 3),
                params.get("pause_between_keywords_max", 6)
            ))

        await client.disconnect()
        logger.info(f"🔍 [parser_task] Готово: найдено {len(found)}, сохранено {saved_count}")
        r.setex(progress_key, 300, f"done|{len(found)}|{saved_count}|{len(keywords)}|готово")
    except Exception as e:
        try: await client.disconnect()
        except: pass
        logger.error(f"🔍 [parser_task] Ошибка: {e}")
        return {"error": str(e)[:200]}

    return {"found": len(found), "saved": saved_count}


@celery_app.task(
    bind=True,
    name="tasks.parser_tasks.run_parser_search",
    acks_late=False,              # ACK сразу при старте, не при завершении
    reject_on_worker_lost=False,  # не возвращать в очередь при падении воркера
)
def run_parser_search(self, user_id: int, account_id: int, params: dict):
    """Фоновый парсинг каналов."""
    return run_async(_run_parser(user_id, account_id, params))