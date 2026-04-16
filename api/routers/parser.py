"""
GramGPT API — routers/parser.py (v3)
Парсер каналов через Telegram-поиск (user accounts).
TGStat оставлен как опция, но по умолчанию используется Telegram.
"""

import sys
import os
import csv
import io
import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.orm import joinedload

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount
from models.proxy import Proxy
from models.parsed_channel import ParsedChannel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/parser", tags=["parser"])


# ── Helper ───────────────────────────────────────────────────

async def _get_client(acc, db):
    api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    from utils.telegram import make_telethon_client

    proxy = None
    if acc.proxy_id:
        proxy_r = await db.execute(select(Proxy).where(Proxy.id == acc.proxy_id))
        proxy = proxy_r.scalar_one_or_none()

    client = make_telethon_client(acc, proxy)
    if not client:
        raise HTTPException(status_code=400, detail="Файл сессии не найден")
    return client


# ── Schemas ──────────────────────────────────────────────────

class SearchRequest(BaseModel):
    account_id: int  # Обязательно для Telegram поиска

    # Ключевые слова (через запятую)
    keywords: str
    
    # Фильтры подписчиков
    min_subscribers: int = 0
    max_subscribers: int = 10000000

    # Только с открытыми комментариями
    only_with_comments: bool = True
    active_hours: int = 0  # Посты за последние N часов

    # Фильтры username
    name_endings: Optional[str] = None  # "_news,_info,_ua"
    name_contains: Optional[str] = None  # "crypto,trade"

    # Лимиты
    limit_per_keyword: int = 50
    max_channels: int = 500

    # Кастомные паузы (секунды)
    pause_between_keywords_min: float = 3.0
    pause_between_keywords_max: float = 6.0
    pause_between_channels_min: float = 0.8
    pause_between_channels_max: float = 1.5


class ImportRequest(BaseModel):
    channels: list[str]


# ── Endpoints ────────────────────────────────────────────────

@router.get("/channels")
async def list_parsed_channels(
    folder: Optional[str] = None,
    min_subscribers: Optional[int] = None,
    only_with_comments: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(ParsedChannel).where(ParsedChannel.user_id == current_user.id)
    if folder is not None:
        q = q.where(ParsedChannel.folder == folder)
    if min_subscribers is not None:
        q = q.where(ParsedChannel.subscribers >= min_subscribers)
    if only_with_comments:
        q = q.where(ParsedChannel.has_comments == True)
    q = q.order_by(ParsedChannel.subscribers.desc())

    result = await db.execute(q)
    channels = result.scalars().all()
    return [{
        "id": c.id, "username": c.username, "title": c.title,
        "subscribers": c.subscribers, "has_comments": c.has_comments,
        "last_post_date": c.last_post_date.isoformat() if c.last_post_date else None,
        "search_query": c.search_query, "added_at": c.added_at.isoformat(),
        "folder": c.folder or "",
        "country": c.country or "",
        "language": c.language or "",
        "category": c.category or "",
        "description": (c.description or "")[:200] if c.description else "",
        "avg_post_reach": c.avg_post_reach or 0,
        "err": c.err or 0,
        "source": c.source or "telegram",
    } for c in channels]


@router.post("/search")
async def search_channels(
    body: SearchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Поиск каналов через Telegram API (user account)."""
    keywords = [k.strip() for k in body.keywords.split(",") if k.strip()]
    if not keywords:
        raise HTTPException(status_code=400, detail="Укажите хотя бы одно ключевое слово")

    # Загружаем аккаунт
    acc_r = await db.execute(
        select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
        .where(TelegramAccount.id == body.account_id, TelegramAccount.user_id == current_user.id)
    )
    acc = acc_r.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    if not acc.proxy_id:
        raise HTTPException(status_code=400, detail="У аккаунта нет прокси")

    client = await _get_client(acc, db)
    found = []
    seen_usernames = set()

    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise HTTPException(status_code=400, detail="Аккаунт не авторизован")

        from telethon.tl.functions.contacts import SearchRequest as TgSearchRequest

        for kw in keywords:
            if len(found) >= body.max_channels:
                break

            logger.info(f"🔍 Telegram поиск: '{kw}'")
            try:
                res = await client(TgSearchRequest(q=kw, limit=body.limit_per_keyword))
                channels_found_kw = 0

                for chat in res.chats:
                    if len(found) >= body.max_channels:
                        break
                    if not hasattr(chat, 'username') or not chat.username:
                        continue
                    if chat.username in seen_usernames:
                        continue

                    # Фильтры username (дёшево — до запроса на канал)
                    if body.name_endings:
                        endings = [e.strip().lower() for e in body.name_endings.split(",") if e.strip()]
                        if not any(chat.username.lower().endswith(e) for e in endings):
                            continue
                    if body.name_contains:
                        parts = [p.strip().lower() for p in body.name_contains.split(",") if p.strip()]
                        if not any(p in chat.username.lower() for p in parts):
                            continue

                    # Подписчики — берём participants_count если есть
                    subs = getattr(chat, 'participants_count', 0) or 0

                    # Если нет в SearchRequest — нужно через get_full
                    # Если нет количества подписчиков — пропускаем (мелкий/приватный)
                    if subs == 0:
                        continue

                    if subs < body.min_subscribers or subs > body.max_subscribers:
                        continue

                    # Проверка комментариев + активности
                    has_comments = False
                    last_post = None
                    try:
                        msgs = await client.get_messages(chat, limit=1)
                        if msgs:
                            last_post = msgs[0].date
                            if msgs[0].replies and getattr(msgs[0].replies, 'comments', False):
                                has_comments = True
                    except Exception as e:
                        logger.debug(f"get_messages @{chat.username}: {e}")

                    if body.only_with_comments and not has_comments:
                        continue
                    if body.active_hours > 0 and last_post:
                        cutoff = datetime.utcnow() - timedelta(hours=body.active_hours)
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
                        "source": "telegram",
                    }
                    found.append(ch_data)
                    channels_found_kw += 1

                    # ═══ STREAMING: сохраняем в БД СРАЗУ ═══
                    try:
                        existing = await db.execute(
                            select(ParsedChannel).where(
                                ParsedChannel.user_id == current_user.id,
                                ParsedChannel.username == chat.username,
                            )
                        )
                        if not existing.scalar_one_or_none():
                            db.add(ParsedChannel(
                                user_id=current_user.id,
                                channel_id=chat.id,
                                username=chat.username,
                                title=chat.title,
                                subscribers=subs,
                                has_comments=has_comments,
                                last_post_date=last_post.replace(tzinfo=None) if last_post else None,
                                search_query=kw,
                                source="telegram",
                            ))
                            await db.commit()
                            logger.info(f"  + @{chat.username} ({subs} подписчиков) [сохранён]")
                        else:
                            logger.info(f"  + @{chat.username} (уже в БД)")
                    except Exception as e:
                        logger.warning(f"Save @{chat.username}: {e}")
                        try: await db.rollback()
                        except: pass

                    # Rate limit между проверками каналов (кастомный)
                    await asyncio.sleep(random.uniform(body.pause_between_channels_min, body.pause_between_channels_max))

                logger.info(f"  '{kw}': добавлено {channels_found_kw} каналов")
            except Exception as e:
                err = str(e)
                if "FLOOD_WAIT" in err:
                    import re
                    wait = int(re.search(r"(\d+)", err).group(1)) if re.search(r"(\d+)", err) else 60
                    logger.warning(f"🔍 FLOOD_WAIT_{wait} на '{kw}' — прерываю поиск")
                    break
                logger.warning(f"🔍 Ошибка поиска '{kw}': {e}")

            # Пауза между ключевыми словами (кастомная)
            await asyncio.sleep(random.uniform(body.pause_between_keywords_min, body.pause_between_keywords_max))

        await client.disconnect()
        logger.info(f"🔍 Telegram: найдено {len(found)} каналов")

    except HTTPException:
        try: await client.disconnect()
        except: pass
        raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        logger.error(f"🔍 Ошибка: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")

    # ═══ Сохраняем в БД ═══
# Каналы уже сохранены в процессе поиска (streaming)
    saved = len(found)

    return {"found": len(found), "saved": saved, "channels": found[:100]}

# ── Удаление, экспорт, импорт ───────────────────────────────

@router.delete("/channels/{channel_id}", status_code=204)
async def delete_parsed_channel(
    channel_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ParsedChannel).where(ParsedChannel.id == channel_id, ParsedChannel.user_id == current_user.id)
    )
    ch = result.scalar_one_or_none()
    if ch:
        await db.delete(ch)
        await db.flush()


@router.delete("/channels", status_code=204)
async def clear_all_parsed(
    folder: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = delete(ParsedChannel).where(ParsedChannel.user_id == current_user.id)
    if folder is not None:
        q = q.where(ParsedChannel.folder == folder)
    await db.execute(q)
    await db.flush()


@router.get("/export")
async def export_channels_csv(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ParsedChannel).where(ParsedChannel.user_id == current_user.id).order_by(ParsedChannel.subscribers.desc())
    )
    channels = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["username", "title", "subscribers", "has_comments", "folder", "last_post", "query"])
    for c in channels:
        writer.writerow([f"@{c.username}", c.title, c.subscribers, c.has_comments,
                         c.folder or "", c.last_post_date.isoformat() if c.last_post_date else "",
                         c.search_query])

    output.seek(0)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=channels.csv"})


@router.post("/import")
async def import_channels(
    body: ImportRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    added = 0
    for raw in body.channels:
        username = raw.strip().replace("@", "").replace("https://t.me/", "")
        if not username:
            continue
        existing = await db.execute(
            select(ParsedChannel).where(ParsedChannel.user_id == current_user.id, ParsedChannel.username == username)
        )
        if existing.scalar_one_or_none():
            continue
        db.add(ParsedChannel(user_id=current_user.id, username=username, title=username, search_query="import"))
        added += 1

    await db.flush()
    return {"added": added}


# ── Папки каналов ────────────────────────────────────────────

@router.get("/folders")
async def list_folders(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ParsedChannel.folder).where(
            ParsedChannel.user_id == current_user.id,
            ParsedChannel.folder != "",
        )
    )
    folders_raw = [r[0] for r in result.all()]
    folder_counts = {}
    for f in folders_raw:
        folder_counts[f] = folder_counts.get(f, 0) + 1
    return [{"name": k, "count": v} for k, v in sorted(folder_counts.items())]


@router.get("/folders/{folder_name}/channels")
async def get_folder_channels(
    folder_name: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ParsedChannel).where(
            ParsedChannel.user_id == current_user.id,
            ParsedChannel.folder == folder_name,
        )
    )
    channels = result.scalars().all()
    return [{"id": c.id, "username": c.username, "title": c.title,
             "subscribers": c.subscribers, "has_comments": c.has_comments} for c in channels]


class SetFolderRequest(BaseModel):
    channel_ids: list[int]
    folder: str


@router.post("/set-folder")
async def set_folder(
    body: SetFolderRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ParsedChannel).where(
            ParsedChannel.user_id == current_user.id,
            ParsedChannel.id.in_(body.channel_ids),
        )
    )
    channels = result.scalars().all()
    for ch in channels:
        ch.folder = body.folder
    await db.flush()
    return {"updated": len(channels), "folder": body.folder}


@router.patch("/channels/{channel_id}/folder")
async def update_channel_folder(
    channel_id: int,
    body: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ParsedChannel).where(ParsedChannel.id == channel_id, ParsedChannel.user_id == current_user.id)
    )
    ch = result.scalar_one_or_none()
    if not ch:
        raise HTTPException(status_code=404, detail="Канал не найден")
    ch.folder = body.get("folder", "")
    await db.flush()
    return {"id": ch.id, "folder": ch.folder}