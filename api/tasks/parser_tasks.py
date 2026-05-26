"""
GramGPT — tasks/parser_tasks.py

Парсинг каналов по keywords через Telegram-поиск.

Улучшения:
 1. has_comments определяется через GetFullChannel.linked_chat_id (точно!)
    — раньше через replies.comments последнего поста, давало ложные срабатывания.
 2. Логирование событий в parser_events для метрик (FLOOD_WAIT, session_done).
 3. FloodWaitError обработка через telethon.errors (не через re-parse строки).
"""

import asyncio
import sys
import os
import time
import logging
import random
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


async def _interruptible_sleep(seconds: float, r, stop_key: str) -> bool:
    """Sleep in 0.5s chunks. Returns True if stop signal was received."""
    end = time.time() + seconds
    while time.time() < end:
        if r.get(stop_key):
            return True
        await asyncio.sleep(min(0.5, max(0.0, end - time.time())))
    return False


# ═══════════════════════════════════════════════════════════
# Event logging для метрик
# ═══════════════════════════════════════════════════════════

async def _log_event(user_id, event_type, **kwargs):
    """Пишет событие в parser_events (не валит таску если таблицы/модуля нет)."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)
    try:
        from utils.parser_events import log_event
        await log_event(user_id=user_id, event_type=event_type, **kwargs)
    except Exception as e:
        logger.warning(f"[parser_events] skip: {e}")


# ═══════════════════════════════════════════════════════════
# DB helpers
# ═══════════════════════════════════════════════════════════

async def _save_channel(user_id, channel_data):
    """Сохраняет один канал в свою отдельную сессию БД."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy import select
    from models.parsed_channel import ParsedChannel
    from utils.db_pool import async_session as Session

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
        if hasattr(ParsedChannel, 'source'):
            ch_fields["source"] = "telegram"

        db.add(ParsedChannel(**ch_fields))
        await db.commit()
        return True


# ═══════════════════════════════════════════════════════════
# Точная проверка has_comments через GetFullChannel
# ═══════════════════════════════════════════════════════════

async def _check_has_comments(client, chat) -> tuple[bool, int]:
    """
    Точная проверка: канал имеет linked discussion group?
    Возвращает (has_comments, flood_wait_seconds).
    """
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.errors import FloodWaitError, ChannelPrivateError

    try:
        full = await client(GetFullChannelRequest(channel=chat))
        linked_id = getattr(full.full_chat, 'linked_chat_id', 0) or 0
        return (linked_id > 0, 0)
    except FloodWaitError as e:
        return (False, e.seconds)
    except ChannelPrivateError:
        return (False, 0)
    except Exception as e:
        logger.warning(f"[parser] GetFullChannel @{getattr(chat, 'username', '?')}: {str(e)[:100]}")
        return (False, 0)


async def _get_last_post_date(client, chat):
    """Просто получает дату последнего поста (для active_hours фильтра)."""
    from telethon.errors import FloodWaitError
    try:
        msgs = await client.get_messages(chat, limit=1)
        if msgs:
            return msgs[0].date
    except FloodWaitError:
        pass
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════
# Основной parser
# ═══════════════════════════════════════════════════════════

async def _run_parser(user_id: int, account_id: int, params: dict):
    """Запускает парсинг в фоне."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.telegram import make_telethon_client
    from utils.db_pool import async_session as Session

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

    import redis as redis_lib
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r = redis_lib.from_url(redis_url)
    progress_key = f"parser:progress:{user_id}"
    stop_key = f"parser:stop:{user_id}"
    r.delete(stop_key)
    # Store progress key so the Celery task can be revoked if needed
    # (task_id is stored by the outer Celery wrapper)

    keywords = [k.strip() for k in params["keywords"].split(",") if k.strip()]
    found = []
    saved_count = 0
    seen_usernames = set()
    flood_events = 0
    flood_total_wait = 0

    # Flood threshold — если больше, прерываем
    flood_threshold = int(params.get("flood_threshold", 300))
    # Нужна ли точная проверка (по умолчанию ДА — через GetFullChannel)
    precise_check = bool(params.get("precise_check", True))

    start_time = time.time()

    # Start event
    await _log_event(
        user_id, "session_start", source="search", account_id=account_id,
        details=f"keywords={len(keywords)} precise={precise_check} only_comments={params.get('only_with_comments')}",
    )

    r.setex(progress_key, 3600, f"running|0|0|{len(keywords)}|старт")

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return {"error": "Аккаунт не авторизован"}

        try:
            from utils.connection_limiter import increment_connection
            increment_connection(acc.id)
        except Exception:
            pass

        from telethon.tl.functions.contacts import SearchRequest as TgSearchRequest
        from telethon.errors import FloodWaitError

        for kw_idx, kw in enumerate(keywords, 1):
            if r.get(stop_key):
                logger.info(f"🔍 [parser] Stop signal")
                break
            if len(found) >= params["max_channels"]:
                break

            r.setex(progress_key, 3600, f"running|{len(found)}|{saved_count}|{len(keywords)}|{kw}")
            logger.info(f"🔍 [parser] Поиск {kw_idx}/{len(keywords)}: '{kw}'")

            try:
                res = await client(TgSearchRequest(q=kw, limit=params["limit_per_keyword"]))

                for chat in res.chats:
                    if r.get(stop_key):
                        break
                    if len(found) >= params["max_channels"]:
                        break
                    if not hasattr(chat, 'username') or not chat.username:
                        continue
                    if chat.username in seen_usernames:
                        continue

                    # Фильтры по username
                    if params.get("name_endings"):
                        endings = [e.strip().lower() for e in params["name_endings"].split(",") if e.strip()]
                        if not any(chat.username.lower().endswith(e) for e in endings):
                            continue
                    if params.get("name_contains"):
                        parts = [p.strip().lower() for p in params["name_contains"].split(",") if p.strip()]
                        if not any(p in chat.username.lower() for p in parts):
                            continue

                    # Фильтр по подписчикам
                    subs = getattr(chat, 'participants_count', 0) or 0
                    if subs == 0:
                        continue
                    if subs < params["min_subscribers"] or subs > params["max_subscribers"]:
                        continue

                    # ── ТОЧНАЯ ПРОВЕРКА has_comments ──
                    has_comments = False
                    last_post = None

                    if precise_check:
                        # GetFullChannel — единственный надёжный способ
                        has_comments, fw = await _check_has_comments(client, chat)
                        if fw > 0:
                            flood_events += 1
                            await _log_event(user_id, "flood_wait", source="search",
                                             account_id=account_id, wait_seconds=fw,
                                             seed=chat.username, details="GetFullChannel")
                            if fw > flood_threshold:
                                logger.warning(f"🔍 [parser] FLOOD_WAIT {fw}s — прерываю")
                                r.setex(progress_key, 300, f"error|{len(found)}|{saved_count}|{len(keywords)}|FLOOD {fw}s")
                                raise FloodWaitError(request=None, seconds=fw)
                            logger.info(f"🔍 [parser] FLOOD_WAIT {fw}s — жду")
                            flood_total_wait += fw
                            stopped = await _interruptible_sleep(fw + 2, r, stop_key)
                            if stopped:
                                logger.info("🔍 [parser] Stop during flood wait")
                                raise StopIteration
                            # Повторяем проверку
                            has_comments, fw2 = await _check_has_comments(client, chat)

                        # Получаем дату поста (только если нужна для active_hours фильтра)
                        if params.get("active_hours", 0) > 0:
                            last_post = await _get_last_post_date(client, chat)
                    else:
                        # Быстрая проверка (как было) — по replies.comments
                        try:
                            msgs = await client.get_messages(chat, limit=1)
                            if msgs:
                                last_post = msgs[0].date
                                if msgs[0].replies and getattr(msgs[0].replies, 'comments', False):
                                    has_comments = True
                        except FloodWaitError as e:
                            flood_events += 1
                            flood_total_wait += e.seconds
                            await asyncio.sleep(e.seconds + 1)
                        except Exception:
                            pass

                    # Фильтры по результату
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

                    try:
                        saved = await _save_channel(user_id, ch_data)
                        if saved:
                            saved_count += 1
                            logger.info(f"  + @{chat.username} ({subs}) comments={has_comments} [сохранён]")
                        else:
                            logger.info(f"  + @{chat.username} (уже в БД)")

                        r.setex(progress_key, 3600, f"running|{len(found)}|{saved_count}|{len(keywords)}|{kw}")
                    except Exception as e:
                        logger.warning(f"Save @{chat.username}: {e}")

                    # Пауза между каналами
                    stopped = await _interruptible_sleep(
                        random.uniform(
                            params.get("pause_between_channels_min", 0.8),
                            params.get("pause_between_channels_max", 1.5)
                        ), r, stop_key
                    )
                    if stopped:
                        break

            except StopIteration:
                break
            except FloodWaitError as e:
                flood_events += 1
                flood_total_wait += e.seconds
                await _log_event(user_id, "flood_wait", source="search",
                                 account_id=account_id, wait_seconds=e.seconds,
                                 seed=kw, details="TgSearchRequest")
                if e.seconds > flood_threshold:
                    logger.warning(f"🔍 FLOOD_WAIT_{e.seconds} — прерываю")
                    break
                logger.warning(f"🔍 FLOOD_WAIT {e.seconds}s — жду")
                stopped = await _interruptible_sleep(e.seconds + 2, r, stop_key)
                if stopped:
                    break
            except Exception as e:
                logger.warning(f"🔍 Ошибка '{kw}': {e}")

            # Пауза между keywords
            stopped = await _interruptible_sleep(
                random.uniform(
                    params.get("pause_between_keywords_min", 3),
                    params.get("pause_between_keywords_max", 6)
                ), r, stop_key
            )
            if stopped:
                break

        await client.disconnect()

        duration = int(time.time() - start_time)
        logger.info(f"🔍 [parser] Готово: найдено {len(found)}, сохранено {saved_count}, duration={duration}s, floods={flood_events}")
        r.setex(progress_key, 300, f"done|{len(found)}|{saved_count}|{len(keywords)}|готово")

        # Session done event
        await _log_event(
            user_id, "session_done", source="search", account_id=account_id,
            channels_found=len(found), channels_saved=saved_count,
            duration_sec=duration,
            details=f"keywords={len(keywords)} floods={flood_events} flood_wait={flood_total_wait}s precise={precise_check}",
        )

    except Exception as e:
        try: await client.disconnect()
        except: pass
        logger.error(f"🔍 [parser] Ошибка: {e}")
        await _log_event(user_id, "error", source="search", account_id=account_id,
                         details=str(e)[:500])
        return {"error": str(e)[:200]}

    return {"found": len(found), "saved": saved_count, "flood_wait": flood_total_wait}


@celery_app.task(
    bind=True,
    name="tasks.parser_tasks.run_parser_search",
    acks_late=False,
    reject_on_worker_lost=False,
)
def run_parser_search(self, user_id: int, account_id: int, params: dict):
    """Фоновый парсинг каналов."""
    import redis as redis_lib
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    r.setex(f"parser:task_id:{user_id}", 7200, self.request.id)
    return run_async(_run_parser(user_id, account_id, params))