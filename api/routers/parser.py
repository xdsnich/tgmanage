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
        "id": c.id,
        "username": c.username or "",
        "title": c.title or "",
        "subscribers": c.subscribers or 0,
        "has_comments": bool(c.has_comments),
        "last_post_date": c.last_post_date.isoformat() if c.last_post_date else None,
        "search_query": c.search_query or "",
        "added_at": c.added_at.isoformat() if c.added_at else None,
        "folder": getattr(c, 'folder', '') or "",
        "country": getattr(c, 'country', '') or "",
        "language": getattr(c, 'language', '') or "",
        "category": getattr(c, 'category', '') or "",
        "description": (getattr(c, 'description', '') or "")[:200],
        "avg_post_reach": getattr(c, 'avg_post_reach', 0) or 0,
        "err": getattr(c, 'err', 0) or 0,
        "source": getattr(c, 'source', 'telegram') or "telegram",
    } for c in channels]


@router.post("/search")
async def search_channels(
    body: SearchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запускает парсинг в фоне через Celery — возвращается сразу."""
    keywords = [k.strip() for k in body.keywords.split(",") if k.strip()]
    if not keywords:
        raise HTTPException(status_code=400, detail="Укажите хотя бы одно ключевое слово")

    # Проверяем аккаунт
    acc_r = await db.execute(
        select(TelegramAccount)
        .where(TelegramAccount.id == body.account_id, TelegramAccount.user_id == current_user.id)
    )
    acc = acc_r.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    if not acc.proxy_id:
        raise HTTPException(status_code=400, detail="У аккаунта нет прокси")

    # Отправляем в Celery
    from celery_app import celery_app
    task = celery_app.send_task(
        "tasks.parser_tasks.run_parser_search",
        args=[current_user.id, body.account_id, body.model_dump()],
        queue="ai_dialogs",  # или bulk_actions
    )

    return {
        "task_id": task.id,
        "status": "started",
        "message": "Парсинг запущен в фоне. Каналы появятся в списке по мере нахождения."
    }

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
@router.get("/search/progress")
async def get_search_progress(
    current_user: User = Depends(get_current_user),
):
    """Статус текущего парсинга"""
    import redis as redis_lib
    import os
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    progress = r.get(f"parser:progress:{current_user.id}")
    if not progress:
        return {"status": "idle"}

    try:
        parts = progress.decode().split("|")
        return {
            "status": parts[0],        # running | done | error
            "found": int(parts[1]),
            "saved": int(parts[2]),
            "total_keywords": int(parts[3]),
            "current": parts[4] if len(parts) > 4 else "",
        }
    except Exception:
        return {"status": "idle"}


@router.post("/search/stop")
async def stop_search(
    current_user: User = Depends(get_current_user),
):
    """Прерывает текущий парсинг"""
    import redis as redis_lib
    import os
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    r.setex(f"parser:stop:{current_user.id}", 300, "1")
    return {"status": "stop requested"}
@router.get("/whitelist")
async def get_whitelist(
    min_rate: float = 0,  # фильтр по минимальной проходимости
    sort_by: str = "pass_rate",  # pass_rate | attempts | recent
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Статистика проходимости каналов"""
    from models.channel_ban_stats import ChannelBanStats
    from sqlalchemy import func

    result = await db.execute(
        select(ChannelBanStats).where(ChannelBanStats.user_id == current_user.id)
    )
    stats = result.scalars().all()

    out = []
    for s in stats:
        pass_rate = 100.0 if s.total_attempts == 0 else round(
            (s.total_attempts - s.banned_count) / s.total_attempts * 100, 1
        )
        if pass_rate < min_rate:
            continue
        out.append({
            "id": s.id,
            "channel_username": s.channel_username,
            "total_attempts": s.total_attempts,
            "banned_count": s.banned_count,
            "pass_rate": pass_rate,
            "last_ban_reason": s.last_ban_reason,
            "last_updated": s.last_updated.isoformat() + "Z",
        })

    # Сортировка
    if sort_by == "attempts":
        out.sort(key=lambda x: x["total_attempts"], reverse=True)
    elif sort_by == "recent":
        out.sort(key=lambda x: x["last_updated"], reverse=True)
    else:
        out.sort(key=lambda x: x["pass_rate"], reverse=True)

    return out


@router.delete("/whitelist/{stat_id}", status_code=204)
async def delete_whitelist_entry(
    stat_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Удалить запись (сбросить статистику канала)"""
    from models.channel_ban_stats import ChannelBanStats
    result = await db.execute(
        select(ChannelBanStats).where(
            ChannelBanStats.id == stat_id,
            ChannelBanStats.user_id == current_user.id,
        )
    )
    s = result.scalar_one_or_none()
    if s:
        await db.delete(s)
        await db.flush()