"""
GramGPT — tasks/parser_similar_tasks.py

Два таска:
 1. run_similar_crawler — BFS обход похожих каналов
 2. run_verify_comments — пачковая проверка has_comments

Плюс: логирование событий в БД для метрик (FLOOD_WAIT, скорость, seeds).
"""

import asyncio
import sys
import os
import time
import logging
import random
from collections import deque
from datetime import datetime

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


# ═══════════════════════════════════════════════════════════
# Event logging (метрики парсера)
# ═══════════════════════════════════════════════════════════

async def _log_event(user_id, event_type, **kwargs):
    """Хелпер для записи события парсера в БД."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)
    try:
        from utils.parser_events_helper import log_event
        await log_event(user_id=user_id, event_type=event_type, **kwargs)
    except Exception as e:
        logger.warning(f"[parser_events] skip: {e}")


# ═══════════════════════════════════════════════════════════
# DB helpers
# ═══════════════════════════════════════════════════════════

async def _save_channel(user_id: int, channel_data: dict) -> bool:
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

            ch = ParsedChannel(
                user_id=user_id,
                channel_id=channel_data.get("channel_id", 0),
                username=channel_data["username"],
                title=channel_data["title"],
                subscribers=channel_data["subscribers"],
                has_comments=channel_data.get("has_comments", False),
                search_query=channel_data.get("search_query", "similar"),
                folder=channel_data.get("folder", ""),
            )
            for attr in ("country", "language", "category", "description", "source"):
                if hasattr(ch, attr) and channel_data.get(attr) is not None:
                    setattr(ch, attr, channel_data[attr])

            db.add(ch)
            await db.commit()
            return True
    finally:
        await engine.dispose()


async def _get_client_for(account_id: int):
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

    async with Session() as db:
        acc_r = await db.execute(
            select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(
                TelegramAccount.id == account_id
            )
        )
        account = acc_r.scalar_one_or_none()
        if not account:
            await engine.dispose()
            return None, None

        proxy = None
        if account.proxy_id:
            pr = await db.execute(select(Proxy).where(Proxy.id == account.proxy_id))
            proxy = pr.scalar_one_or_none()

    client = make_telethon_client(account, proxy)
    await engine.dispose()
    return client, account


# ═══════════════════════════════════════════════════════════
# SIMILAR CRAWLER
# ═══════════════════════════════════════════════════════════

async def _get_recommendations(client, channel_entity):
    from telethon.tl.functions.channels import GetChannelRecommendationsRequest
    from telethon.errors import FloodWaitError
    try:
        result = await client(GetChannelRecommendationsRequest(channel=channel_entity))
        return getattr(result, 'chats', [])
    except FloodWaitError as e:
        return ('flood', e.seconds)
    except Exception as e:
        logger.warning(f"[similar] recommendations error: {str(e)[:150]}")
        return []


async def _get_entity_safe(client, username, max_flood_wait=60):
    from telethon.errors import FloodWaitError, UsernameNotOccupiedError, ChannelPrivateError
    try:
        return await client.get_entity(username)
    except FloodWaitError as e:
        if e.seconds > max_flood_wait:
            return ('flood', e.seconds)
        await asyncio.sleep(e.seconds + 1)
        try:
            return await client.get_entity(username)
        except Exception:
            return None
    except (UsernameNotOccupiedError, ChannelPrivateError):
        return None
    except Exception as e:
        logger.warning(f"[similar] get_entity(@{username}): {str(e)[:100]}")
        return None


async def _run_similar_crawler(user_id: int, account_id: int, params: dict):
    import redis

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r = redis.from_url(redis_url)
    progress_key = f"parser:similar:progress:{user_id}"
    stop_key = f"parser:similar:stop:{user_id}"
    r.delete(stop_key)

    seeds = [s.strip().replace("@", "") for s in params.get("seeds", []) if s.strip()]
    if not seeds:
        return {"error": "Нет seed-каналов"}

    max_depth = int(params.get("max_depth", 2))
    max_channels = int(params.get("max_channels", 1000))
    folder = params.get("folder", "")
    pause_min = float(params.get("pause_min", 8.0))
    pause_max = float(params.get("pause_max", 15.0))
    flood_threshold = int(params.get("flood_threshold", 120))

    # Start event
    start_time = time.time()
    await _log_event(
        user_id, "session_start", source="similar", account_id=account_id,
        details=f"seeds={len(seeds)} depth={max_depth} max={max_channels}",
    )

    # Считаем сколько каналов сохранено ПО КАЖДОМУ seed'у
    seed_stats = {s: 0 for s in seeds}   # sourced_from_seed -> saved count
    # карта "текущий обход был начат из какого seed'а"
    origin_map = {s: s for s in seeds}

    queue = deque([(s, 0) for s in seeds])
    seen = set(seeds)
    saved_count = 0
    found_count = 0
    flood_total_wait = 0
    flood_events = 0

    client, account = await _get_client_for(account_id)
    if not client:
        return {"error": "Аккаунт не найден / нет сессии"}

    r.setex(progress_key, 3600, f"running|0|0|{len(seeds)}|старт")

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return {"error": "Аккаунт не авторизован"}

        try:
            from utils.connection_limiter import increment_connection
            increment_connection(account.id)
        except Exception:
            pass

        while queue:
            if r.get(stop_key):
                logger.info("[similar] Stop signal received")
                break
            if saved_count >= max_channels:
                break

            username, depth = queue.popleft()
            if depth >= max_depth:
                continue

            root_seed = origin_map.get(username, username)

            r.setex(progress_key, 3600, f"running|{found_count}|{saved_count}|{len(queue)}|{username}")

            entity = await _get_entity_safe(client, username, max_flood_wait=flood_threshold)
            if entity is None:
                continue
            if isinstance(entity, tuple) and entity[0] == 'flood':
                wait = entity[1]
                flood_events += 1
                await _log_event(user_id, "flood_wait", source="similar",
                                 account_id=account_id, wait_seconds=wait, seed=username,
                                 details="get_entity")
                if wait > flood_threshold:
                    r.setex(progress_key, 300, f"error|{found_count}|{saved_count}|{len(queue)}|FLOOD_WAIT {wait}s")
                    break
                flood_total_wait += wait
                await asyncio.sleep(wait + 2)
                queue.appendleft((username, depth))
                continue

            await asyncio.sleep(random.uniform(pause_min, pause_max))

            result = await _get_recommendations(client, entity)
            if isinstance(result, tuple) and result[0] == 'flood':
                wait = result[1]
                flood_events += 1
                await _log_event(user_id, "flood_wait", source="similar",
                                 account_id=account_id, wait_seconds=wait, seed=username,
                                 details="recommendations")
                if wait > flood_threshold:
                    r.setex(progress_key, 300, f"error|{found_count}|{saved_count}|{len(queue)}|FLOOD_WAIT {wait}s")
                    break
                flood_total_wait += wait
                await asyncio.sleep(wait + 2)
                queue.appendleft((username, depth))
                continue

            similar_chats = result
            logger.info(f"[similar] @{username} (d={depth}): найдено {len(similar_chats)} похожих")

            for chat in similar_chats:
                if not hasattr(chat, 'username') or not chat.username:
                    continue
                if chat.username in seen:
                    continue
                seen.add(chat.username)
                found_count += 1
                origin_map[chat.username] = root_seed

                try:
                    subs = getattr(chat, 'participants_count', 0) or 0
                    title = getattr(chat, 'title', chat.username) or chat.username

                    channel_data = {
                        "channel_id": chat.id,
                        "username": chat.username,
                        "title": title,
                        "subscribers": subs,
                        "has_comments": False,
                        "search_query": f"similar:@{root_seed}",
                        "folder": folder,
                        "source": "similar",
                    }

                    is_new = await _save_channel(user_id, channel_data)
                    if is_new:
                        saved_count += 1
                        seed_stats[root_seed] = seed_stats.get(root_seed, 0) + 1
                        logger.info(f"  + @{chat.username} ({subs}) [d={depth + 1}]")

                    if depth + 1 < max_depth:
                        queue.append((chat.username, depth + 1))

                except Exception as e:
                    logger.warning(f"[similar] save @{chat.username}: {str(e)[:100]}")

                r.setex(progress_key, 3600, f"running|{found_count}|{saved_count}|{len(queue)}|{username}")

        await client.disconnect()

        duration = int(time.time() - start_time)
        msg = f"done|{found_count}|{saved_count}|0|готово"
        if flood_total_wait > 0:
            msg = f"done|{found_count}|{saved_count}|0|готово (FLOOD {flood_total_wait}s)"
        r.setex(progress_key, 300, msg)

        # Session done event
        details_str = f"floods={flood_events} flood_wait={flood_total_wait}s seeds={','.join(f'{s}:{n}' for s, n in seed_stats.items() if n > 0)}"
        await _log_event(
            user_id, "session_done", source="similar", account_id=account_id,
            channels_found=found_count, channels_saved=saved_count,
            duration_sec=duration, details=details_str,
        )

        logger.info(f"[similar] Готово: найдено {found_count}, сохранено {saved_count}, длительность {duration}s")

    except Exception as e:
        try: await client.disconnect()
        except: pass
        logger.error(f"[similar] Критическая ошибка: {e}")
        r.setex(progress_key, 300, f"error|{found_count}|{saved_count}|0|{str(e)[:100]}")
        await _log_event(user_id, "error", source="similar", account_id=account_id,
                         details=str(e)[:500])
        return {"error": str(e)[:200]}

    return {"found": found_count, "saved": saved_count, "flood_wait": flood_total_wait}


# ═══════════════════════════════════════════════════════════
# COMMENTS VERIFIER
# ═══════════════════════════════════════════════════════════

async def _check_activity_via_web(username: str, active_hours: int) -> tuple[bool, str]:
    """Перевіряє дату останнього поста через t.me/s/ без навантаження на акаунт."""
    if active_hours <= 0:
        return True, "активність не важлива"
        
    url = f"https://t.me/s/{username}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 404:
                    return False, "web: канал не знайдено"
                if resp.status != 200:
                    return True, "web: помилка доступу (перевіримо через API)"
                html = await resp.text()

        # Шукаємо тег <time datetime="2026-05-14T12:34:56+00:00"
        times = re.findall(r'<time[^>]+datetime="([^"]+)"', html)
        if not times:
            return False, "web: немає постів або приватний"

        last_time_str = times[-1]
        last_date = datetime.fromisoformat(last_time_str.replace('Z', '+00:00'))
        
        delta_hours = (datetime.now(timezone.utc) - last_date).total_seconds() / 3600.0
        if delta_hours > active_hours:
            return False, f"web: мертвий ({int(delta_hours)}ч тому)"

        return True, f"web: живий ({int(delta_hours)}ч тому)"
    except Exception as e:
        logger.warning(f"[web_check] @{username} error: {e}")
        return True, "web: помилка парсингу"

async def _check_channel_has_comments(client, username) -> tuple[bool, int]:
    from telethon.errors import FloodWaitError, UsernameNotOccupiedError, ChannelPrivateError
    try:
        entity = await client.get_entity(username)
        msgs = await client.get_messages(entity, limit=1)
        if msgs and msgs[0].replies and getattr(msgs[0].replies, 'comments', False):
            return (True, 0)
        return (False, 0)
    except FloodWaitError as e:
        return (False, e.seconds)
    except (UsernameNotOccupiedError, ChannelPrivateError):
        return (False, 0)
    except Exception as e:
        logger.warning(f"[verify] @{username}: {str(e)[:100]}")
        return (False, 0)

import aiohttp
import re
from datetime import datetime, timezone

async def _check_channel_activity(username: str, active_hours: int) -> tuple[bool, str]:
    """Проверка активности через веб-версию t.me/s/ (без нагрузки на аккаунт)."""
    if active_hours <= 0:
        return True, "активность не важна"
        
    url = f"https://t.me/s/{username}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 404:
                    return False, "web: канал не найден"
                if resp.status != 200:
                    return True, "web: ошибка доступа"
                html = await resp.text()

        times = re.findall(r'<time[^>]+datetime="([^"]+)"', html)
        if not times:
            return False, "web: нет постов или приватный"

        last_time_str = times[-1]
        last_date = datetime.fromisoformat(last_time_str.replace('Z', '+00:00'))
        
        delta_hours = (datetime.now(timezone.utc) - last_date).total_seconds() / 3600.0
        if delta_hours > active_hours:
            return False, f"web: мертвый ({int(delta_hours)}ч назад)"

        return True, f"web: живой ({int(delta_hours)}ч назад)"
    except Exception as e:
        logger.warning(f"[web_check] @{username} error: {e}")
        return True, "web: ошибка парсинга"

async def _update_channel_has_comments(channel_id: int, has_comments: bool, subscribers: int = None):
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
            r = await db.execute(select(ParsedChannel).where(ParsedChannel.id == channel_id))
            ch = r.scalar_one_or_none()
            if ch:
                ch.has_comments = has_comments
                ch.last_verification = datetime.utcnow() # <--- ДОБАВЛЕНО ТОЛЬКО ЭТО
                if subscribers is not None and subscribers > 0:
                    ch.subscribers = subscribers
                await db.commit()
    finally:
        await engine.dispose()


async def _run_verify_comments(user_id: int, account_id: int, params: dict):
    import redis

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r = redis.from_url(redis_url)
    progress_key = f"parser:verify:progress:{user_id}"
    stop_key = f"parser:verify:stop:{user_id}"
    r.delete(stop_key)

    folder = params.get("folder", "")
    limit = int(params.get("limit", 200))
    pause_min = float(params.get("pause_min", 2.0))
    pause_max = float(params.get("pause_max", 4.0))
    only_unverified = bool(params.get("only_unverified", True))
    
    # НОВОЕ ПОЛЕ: извлекаем часы активности (по умолчанию 0 = отключено)
    active_hours = int(params.get("active_hours", 0))

    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select
    from config import DATABASE_URL
    from models.parsed_channel import ParsedChannel

    engine = create_async_engine(DATABASE_URL, pool_size=1, max_overflow=0)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    from datetime import datetime, timedelta

    min_days = int(params.get("min_verify_interval_days", 0))
    async with Session() as db:
        q = select(ParsedChannel).where(ParsedChannel.user_id == user_id)
        if folder:
            q = q.where(ParsedChannel.folder == folder)
        if only_unverified:
            q = q.where(ParsedChannel.has_comments == False)

        if min_days > 0:
            threshold = datetime.utcnow() - timedelta(days=min_days)
            # Выбираем каналы, которые никогда не проверялись ИЛИ проверялись давно
            q = q.where(
                (ParsedChannel.last_verification == None) | 
                (ParsedChannel.last_verification < threshold)
            )
        q = q.limit(limit)
        result = await db.execute(q)
        channels = result.scalars().all()
        channels_data = [(c.id, c.username) for c in channels if c.username]

    await engine.dispose()

    total = len(channels_data)
    if total == 0:
        r.setex(progress_key, 300, f"done|0|0|0|нет каналов для проверки")
        return {"checked": 0, "with_comments": 0}

    # Start event
    start_time = time.time()
    await _log_event(user_id, "session_start", source="verify", account_id=account_id,
                     details=f"folder={folder or 'ALL'} limit={limit} unverified_only={only_unverified} active_hours={active_hours}")

    logger.info(f"[verify] Старт проверки {total} каналов в папке '{folder or 'ALL'}'")
    r.setex(progress_key, 3600, f"running|0|0|{total}|старт")

    client, account = await _get_client_for(account_id)
    if not client:
        return {"error": "Аккаунт не найден"}

    checked = 0
    with_comments = 0
    flood_total_wait = 0
    flood_events = 0

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return {"error": "Не авторизован"}

        try:
            from utils.connection_limiter import increment_connection
            increment_connection(account.id)
        except Exception:
            pass

        # НОВЫЙ ЦИКЛ ОБХОДА
        for ch_id, username in channels_data:
            if r.get(stop_key):
                logger.info("[verify] Stop signal")
                break
            logger.info(f"👉 Починаю перевірку каналу: @{username}")
            reason = ""
            has_comments = False
            needs_api_check = True

            # 1. ПРОВЕРКА АКТИВНОСТИ ЧЕРЕЗ ВЕБ (без API телеграма)
            if active_hours > 0:
                is_active, web_reason = await _check_channel_activity(username, active_hours)
                reason = web_reason
                if not is_active:
                    needs_api_check = False # Канал мертвый, Telethon не дергаем

            # 2. ЛЕГКАЯ ПЕРЕВЕРКА КОММЕНТОВ (через оригинальное название функции)
            if needs_api_check:
                has_comments, flood_wait = await _check_channel_has_comments(client, username)

                if flood_wait > 0:
                    flood_events += 1
                    await _log_event(user_id, "flood_wait", source="verify",
                                     account_id=account_id, wait_seconds=flood_wait, seed=username)
                    if flood_wait > 300:
                        r.setex(progress_key, 300, f"error|{checked}|{with_comments}|{total - checked}|FLOOD {flood_wait}s")
                        has_critical_error = True
                        break 
                    logger.info(f"💬 Коментарі @{username}: has_comments={has_comments}, flood_wait={flood_wait}")
                    flood_total_wait += flood_wait
                    await asyncio.sleep(flood_wait + 2)
                    has_comments, flood_wait2 = await _check_channel_has_comments(client, username)

                if has_comments:
                    reason += " | есть комменты"
                else:
                    reason += " | без комментов"

            # 3. СОХРАНЕНИЕ
            await _update_channel_has_comments(ch_id, has_comments)
            checked += 1
            if has_comments:
                with_comments += 1
                logger.info(f"  ✅ @{username} → {reason}")
            else:
                logger.info(f"  ❌ @{username} → {reason}")

            r.setex(progress_key, 3600,
                    f"running|{checked}|{with_comments}|{total - checked}|{username}")

            await asyncio.sleep(random.uniform(pause_min, pause_max))

        await client.disconnect()

        duration = int(time.time() - start_time)
        msg = f"done|{checked}|{with_comments}|0|готово ({with_comments} из {checked} с комментами)"
        if flood_total_wait > 0:
            msg += f" (FLOOD {flood_total_wait}s)"
        r.setex(progress_key, 300, msg)

        await _log_event(
            user_id, "session_done", source="verify", account_id=account_id,
            channels_found=checked, channels_saved=with_comments,
            duration_sec=duration,
            details=f"floods={flood_events} flood_wait={flood_total_wait}s folder={folder or 'ALL'}",
        )

        logger.info(f"[verify] Готово: {checked} проверено, {with_comments} с комментами, {duration}s")

    except Exception as e:
        try: await client.disconnect()
        except: pass
        logger.error(f"[verify] Ошибка: {e}")
        r.setex(progress_key, 300, f"error|{checked}|{with_comments}|0|{str(e)[:100]}")
        await _log_event(user_id, "error", source="verify", account_id=account_id,
                         details=str(e)[:500])
        return {"error": str(e)[:200]}

    return {"checked": checked, "with_comments": with_comments, "flood_wait": flood_total_wait}


# ═══════════════════════════════════════════════════════════
# Celery tasks
# ═══════════════════════════════════════════════════════════

@celery_app.task(
    bind=True,
    name="tasks.parser_similar_tasks.run_similar_crawler",
    acks_late=False,
    reject_on_worker_lost=False,
)
def run_similar_crawler(self, user_id: int, account_id: int, params: dict):
    return run_async(_run_similar_crawler(user_id, account_id, params))


@celery_app.task(
    bind=True,
    name="tasks.parser_similar_tasks.run_verify_comments",
    acks_late=False,
    reject_on_worker_lost=False,
)
def run_verify_comments(self, user_id: int, account_id: int, params: dict):
    return run_async(_run_verify_comments(user_id, account_id, params))

import aiohttp
from bs4 import BeautifulSoup
from langdetect import detect, LangDetectException
import asyncio

async def _detect_channel_language(username: str) -> tuple[str, str]:
    """Асинхронний парсинг веб-сторінки каналу для визначення мови."""
    url = f"https://t.me/s/{username}"
    try:
        async with aiohttp.ClientSession() as session:
            # timeout 10 сек щоб не висіти на мертвих каналах
            async with session.get(url, timeout=10) as resp:
                if resp.status == 404: return None, "не знайдено"
                if resp.status != 200: return None, f"помилка {resp.status}"
                html = await resp.text()

        soup = BeautifulSoup(html, 'html.parser')
        # Беремо всі тексти повідомлень
        messages = soup.find_all('div', class_='tgme_widget_message_text')
        if not messages:
            return None, "немає тексту"

        # Об'єднуємо останні 15 повідомлень
        combined_text = " ".join([m.get_text(separator=' ') for m in messages[-15:]])
        
        if len(combined_text.strip()) < 30:
            return None, "замало тексту"

        # Визначаємо мову
        lang = detect(combined_text)
        return lang, "ок"
    except LangDetectException:
        return None, "не визначено"
    except Exception as e:
        return None, f"помилка: {str(e)[:20]}"


@celery_app.task(name="tasks.parser_similar_tasks.run_detect_language")
def run_detect_language(user_id: int, params: dict):
    """Синхронна обгортка для запуску."""
    asyncio.run(_run_detect_language(user_id, params))


async def _run_detect_language(user_id: int, params: dict):
    import redis
    import time
    
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r = redis.from_url(redis_url)
    progress_key = f"parser:lang:progress:{user_id}"
    stop_key = f"parser:lang:stop:{user_id}"
    r.delete(stop_key)

    folder = params.get("folder", "")
    limit = int(params.get("limit", 500))
    auto_folder = bool(params.get("auto_folder", True))
    only_unknown = bool(params.get("only_unknown", True))

    if API_DIR not in sys.path: sys.path.insert(0, API_DIR)
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select
    from config import DATABASE_URL
    from models.parsed_channel import ParsedChannel

    engine = create_async_engine(DATABASE_URL, pool_size=5, max_overflow=10)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        q = select(ParsedChannel).where(ParsedChannel.user_id == user_id)
        if folder:
            q = q.where(ParsedChannel.folder == folder)
        if only_unknown:
            q = q.where((ParsedChannel.language == None) | (ParsedChannel.language == ""))
        q = q.limit(limit)
        result = await db.execute(q)
        channels = result.scalars().all()
        channels_data = [(c.id, c.username) for c in channels if c.username]

    total = len(channels_data)
    if total == 0:
        r.setex(progress_key, 300, "done|0|0|0|немає каналів")
        return

    r.setex(progress_key, 3600, f"running|0|0|{total}|старт")
    logger.info(f"[lang] Старт перевірки {total} каналів")

    checked = 0
    detected = 0
    start_time = time.time()
    
    # Семафор: скільки каналів обробляти одночасно. 5 - безпечно і дуже швидко.
    semaphore = asyncio.Semaphore(5)

    async def process_channel(ch_id, username):
        async with semaphore:
            lang, reason = await _detect_channel_language(username)
            return ch_id, username, lang, reason

    # Створюємо пул тасок
    tasks = [process_channel(ch_id, username) for ch_id, username in channels_data]
    
    # Виконуємо їх конкурентно і оновлюємо прогрес як тільки якась таска завершиться
    for future in asyncio.as_completed(tasks):
        if r.get(stop_key):
            break
            
        ch_id, username, lang, reason = await future
        checked += 1
        
        if lang:
            detected += 1
            async with Session() as db:
                res = await db.execute(select(ParsedChannel).where(ParsedChannel.id == ch_id))
                ch = res.scalar_one_or_none()
                if ch:
                    ch.language = lang
                    if auto_folder:
                        ch.folder = lang  # АВТОМАТИЧНИЙ РОЗПОДІЛ ПО ПАПКАХ
                    await db.commit()
            logger.info(f" 🌍 @{username} → {lang}")
        
        r.setex(progress_key, 3600, f"running|{checked}|{detected}|{total - checked}|{username}")

    await engine.dispose()
    duration = int(time.time() - start_time)
    r.setex(progress_key, 300, f"done|{checked}|{detected}|0|готово за {duration}с")
    logger.info(f"[lang] Готово: {checked} перевірено, {detected} визначено")