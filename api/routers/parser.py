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
from sqlalchemy import select, delete, func
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
    precise_check: bool = True
    flood_threshold: int = 300


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
    from models.channel_ban_stats import ChannelBanStats

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

    # ── Загружаем pass_rate для всех каналов одним запросом ──
    usernames = [c.username for c in channels if c.username]
    stats_map = {}
    if usernames:
        stats_r = await db.execute(
            select(ChannelBanStats).where(
                ChannelBanStats.user_id == current_user.id,
                ChannelBanStats.channel_username.in_(usernames),
            )
        )
        for s in stats_r.scalars().all():
            pass_rate = 100.0 if s.total_attempts == 0 else round(
                (s.total_attempts - s.banned_count) / s.total_attempts * 100, 1
            )
            stats_map[s.channel_username] = {
                "pass_rate": pass_rate,
                "total_attempts": s.total_attempts,
                "banned_count": s.banned_count,
            }

    return [{
        "id": c.id,
        "username": c.username or "",
        "title": c.title or "",
        "language": getattr(c, 'language', '') or "",
        "subscribers": c.subscribers or 0,
        "has_comments": bool(c.has_comments),
        "last_post_date": c.last_post_date.isoformat() if c.last_post_date else None,
        "last_verification": getattr(c, 'last_verification').isoformat() if getattr(c, 'last_verification', None) else None,
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
        # Проходимость (из ChannelBanStats)
        "pass_rate": stats_map.get(c.username, {}).get("pass_rate"),  # None если нет статы
        "total_attempts": stats_map.get(c.username, {}).get("total_attempts", 0),
        "banned_count": stats_map.get(c.username, {}).get("banned_count", 0),
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
    # Тепер дістаємо не тільки папку, але й статус коментарів
    result = await db.execute(
        select(ParsedChannel.folder, ParsedChannel.has_comments).where(
            ParsedChannel.user_id == current_user.id,
            ParsedChannel.folder != "",
            ParsedChannel.folder.isnot(None)
        )
    )
    rows = result.all()
    folder_counts = {}
    
    for folder, has_comments in rows:
        if folder not in folder_counts:
            folder_counts[folder] = {"count": 0, "with_comments": 0}
        folder_counts[folder]["count"] += 1
        if has_comments:
            folder_counts[folder]["with_comments"] += 1

    return [
        {"name": k, "count": v["count"], "with_comments": v["with_comments"]} 
        for k, v in sorted(folder_counts.items())
    ]


@router.get("/folders/{folder_name}/channels")
async def get_folder_channels(
    folder_name: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from models.channel_ban_stats import ChannelBanStats

    result = await db.execute(
        select(ParsedChannel).where(
            ParsedChannel.user_id == current_user.id,
            ParsedChannel.folder == folder_name,
        )
    )
    channels = result.scalars().all()

    usernames = [c.username for c in channels if c.username]
    stats_map = {}
    if usernames:
        stats_r = await db.execute(
            select(ChannelBanStats).where(
                ChannelBanStats.user_id == current_user.id,
                ChannelBanStats.channel_username.in_(usernames),
            )
        )
        for s in stats_r.scalars().all():
            pass_rate = 100.0 if s.total_attempts == 0 else round(
                (s.total_attempts - s.banned_count) / s.total_attempts * 100, 1
            )
            stats_map[s.channel_username] = pass_rate

    return [{
        "id": c.id, "username": c.username, "title": c.title,
        "subscribers": c.subscribers, "has_comments": c.has_comments,
        "pass_rate": stats_map.get(c.username),
        "folder": c.folder,
    } for c in channels]

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
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    r.setex(f"parser:stop:{current_user.id}", 3600, "1")
    task_id = r.get(f"parser:task_id:{current_user.id}")
    if task_id:
        try:
            from celery_app import celery_app
            celery_app.control.revoke(task_id.decode(), terminate=True, signal="SIGTERM")
        except Exception:
            pass
        r.delete(f"parser:task_id:{current_user.id}")
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

# ══════════════════════════════════════════════════════════
# Similar channels crawler (обход графа похожих каналов)
# ══════════════════════════════════════════════════════════

class SimilarCrawlRequest(BaseModel):
    account_id: int
    seeds: list[str]                   # username каналов (без @)
    max_depth: int = 2                 # 1 = только 1 уровень, 2 = с похожих тоже идём дальше, 3 = ещё глубже
    max_channels: int = 1000
    folder: str = ""
    pause_min: float = 2.0
    pause_max: float = 5.0


@router.post("/similar/start")
async def start_similar_crawl(
    body: SimilarCrawlRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запускает обход графа похожих каналов в фоне."""
    if not body.seeds:
        raise HTTPException(status_code=400, detail="Укажи хотя бы один seed-канал")

    # Проверка аккаунта
    acc_r = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.id == body.account_id,
            TelegramAccount.user_id == current_user.id,
        )
    )
    acc = acc_r.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    if not acc.proxy_id:
        raise HTTPException(status_code=400, detail="У аккаунта нет прокси")

    from celery_app import celery_app
    task = celery_app.send_task(
        "tasks.parser_similar_tasks.run_similar_crawler",
        args=[current_user.id, body.account_id, body.model_dump()],
        queue="ai_dialogs",
    )

    return {
        "task_id": task.id,
        "status": "started",
        "message": f"Crawler запущен: {len(body.seeds)} seeds, глубина {body.max_depth}",
    }


@router.get("/similar/progress")
async def get_similar_progress(
    current_user: User = Depends(get_current_user),
):
    """Прогресс обхода похожих."""
    import redis as redis_lib
    import os
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    progress = r.get(f"parser:similar:progress:{current_user.id}")
    if not progress:
        return {"status": "idle"}

    try:
        parts = progress.decode().split("|")
        return {
            "status": parts[0],        # running | done | error
            "found": int(parts[1]),
            "saved": int(parts[2]),
            "queue": int(parts[3]),    # размер очереди
            "current": parts[4] if len(parts) > 4 else "",
        }
    except Exception:
        return {"status": "idle"}


@router.post("/similar/stop")
async def stop_similar_crawl(
    current_user: User = Depends(get_current_user),
):
    import redis as redis_lib
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    r.setex(f"parser:similar:stop:{current_user.id}", 3600, "1")
    task_id = r.get(f"parser:similar:task_id:{current_user.id}")
    if task_id:
        try:
            from celery_app import celery_app
            celery_app.control.revoke(task_id.decode(), terminate=True, signal="SIGTERM")
        except Exception:
            pass
        r.delete(f"parser:similar:task_id:{current_user.id}")
    return {"status": "stopping"}


# ══════════════════════════════════════════════════════════
# Similar channels crawler (обход графа похожих каналов)
# ══════════════════════════════════════════════════════════

class SimilarCrawlRequest(BaseModel):
    account_id: int
    seeds: list[str]                   # username каналов (без @)
    max_depth: int = 2                 # 1 = только 1 уровень, 2 = с похожих тоже идём дальше, 3 = ещё глубже
    max_channels: int = 1000
    folder: str = ""
    pause_min: float = 2.0
    pause_max: float = 5.0


@router.post("/similar/start")
async def start_similar_crawl(
    body: SimilarCrawlRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запускает обход графа похожих каналов в фоне."""
    if not body.seeds:
        raise HTTPException(status_code=400, detail="Укажи хотя бы один seed-канал")

    # Проверка аккаунта
    acc_r = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.id == body.account_id,
            TelegramAccount.user_id == current_user.id,
        )
    )
    acc = acc_r.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    if not acc.proxy_id:
        raise HTTPException(status_code=400, detail="У аккаунта нет прокси")

    from celery_app import celery_app
    task = celery_app.send_task(
        "tasks.parser_similar_tasks.run_similar_crawler",
        args=[current_user.id, body.account_id, body.model_dump()],
        queue="ai_dialogs",
    )

    return {
        "task_id": task.id,
        "status": "started",
        "message": f"Crawler запущен: {len(body.seeds)} seeds, глубина {body.max_depth}",
    }


@router.get("/similar/progress")
async def get_similar_progress(
    current_user: User = Depends(get_current_user),
):
    """Прогресс обхода похожих."""
    import redis as redis_lib
    import os
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    progress = r.get(f"parser:similar:progress:{current_user.id}")
    if not progress:
        return {"status": "idle"}

    try:
        parts = progress.decode().split("|")
        return {
            "status": parts[0],        # running | done | error
            "found": int(parts[1]),
            "saved": int(parts[2]),
            "queue": int(parts[3]),    # размер очереди
            "current": parts[4] if len(parts) > 4 else "",
        }
    except Exception:
        return {"status": "idle"}


# ══════════════════════════════════════════════════════════
# Comments Verifier — проверка has_comments пачками
# ══════════════════════════════════════════════════════════

class VerifyCommentsRequest(BaseModel):
    account_id: int
    folder: str = ""
    limit: int = 200
    pause_min: float = 2.0
    pause_max: float = 4.0
    only_unverified: bool = True
    active_hours: int = 0
    min_verify_interval_days: int = 0
    stop_on_flood: bool = True            # на первом FloodWait — стоп
    flood_cooldown_sec: int = 300         # если stop_on_flood=False: сколько ждать сверх FloodWait перед retry


@router.post("/verify/start")
async def start_verify_comments(
    body: VerifyCommentsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запускает проверку реальных has_comments через GetFullChannel."""
    acc_r = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.id == body.account_id,
            TelegramAccount.user_id == current_user.id,
        )
    )
    acc = acc_r.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    if not acc.proxy_id:
        raise HTTPException(status_code=400, detail="У аккаунта нет прокси")

    from celery_app import celery_app
    task = celery_app.send_task(
        "tasks.parser_similar_tasks.run_verify_comments",
        args=[current_user.id, body.account_id, body.model_dump()],
        queue="ai_dialogs",
    )

    return {
        "task_id": task.id,
        "status": "started",
        "message": f"Проверка запущена (лимит {body.limit})",
    }


@router.get("/verify/progress")
async def get_verify_progress(
    current_user: User = Depends(get_current_user),
):
    """Прогресс проверки комментов."""
    import redis as redis_lib
    import os
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    progress = r.get(f"parser:verify:progress:{current_user.id}")
    if not progress:
        return {"status": "idle"}

    try:
        parts = progress.decode().split("|")
        return {
            "status": parts[0],          # running | done | error
            "checked": int(parts[1]),
            "with_comments": int(parts[2]),
            "remaining": int(parts[3]),
            "current": parts[4] if len(parts) > 4 else "",
        }
    except Exception:
        return {"status": "idle"}


@router.post("/verify/stop")
async def stop_verify_comments(
    current_user: User = Depends(get_current_user),
):
    """Прерывает текущую проверку."""
    import redis as redis_lib
    import os
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    r.setex(f"parser:verify:stop:{current_user.id}", 300, "1")
    return {"status": "stopping"}


# ══════════════════════════════════════════════════════════
# Alive Check — был ли пост за последние N дней
# ══════════════════════════════════════════════════════════

class AliveCheckRequest(BaseModel):
    account_id: Optional[int] = None  # только для логов — таск работает через web (t.me/s/), не через API
    folder: str = ""
    limit: int = 200
    max_days_inactive: int = 30  # каналы без поста дольше этого — «мёртвые»
    pause_min: float = 0.3
    pause_max: float = 0.8


@router.post("/alive/start")
async def start_alive_check(
    body: AliveCheckRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Проверяет дату последнего поста для каналов в папке через web (t.me/s/{username}).
    НЕ дёргает Telegram API — никакого риска флуда/бана. account_id опционален и
    используется только для логирования.
    """
    from celery_app import celery_app
    task = celery_app.send_task(
        "tasks.parser_similar_tasks.run_alive_check",
        args=[current_user.id, body.account_id or 0, body.model_dump()],
        queue="ai_dialogs",
    )
    return {
        "task_id": task.id,
        "status": "started",
        "message": f"Проверка живости запущена (лимит {body.limit}, порог {body.max_days_inactive} дн.)",
    }


@router.get("/alive/progress")
async def get_alive_progress(
    current_user: User = Depends(get_current_user),
):
    """Прогресс проверки живости."""
    import redis as redis_lib
    import os
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    progress = r.get(f"parser:alive:progress:{current_user.id}")
    if not progress:
        return {"status": "idle"}
    try:
        parts = progress.decode().split("|")
        return {
            "status": parts[0],
            "checked": int(parts[1]),
            "alive": int(parts[2]),
            "remaining": int(parts[3]),
            "current": parts[4] if len(parts) > 4 else "",
        }
    except Exception:
        return {"status": "idle"}


@router.post("/alive/stop")
async def stop_alive_check(
    current_user: User = Depends(get_current_user),
):
    """Прерывает проверку живости."""
    import redis as redis_lib
    import os
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    r.setex(f"parser:alive:stop:{current_user.id}", 300, "1")
    return {"status": "stopping"}


# ══════════════════════════════════════════════════════════
# Keyword Expander — расширение ключевых слов
# ══════════════════════════════════════════════════════════

class ExpandKeywordsRequest(BaseModel):
    seeds: list[str]                              # исходные keywords
    target_geos: Optional[list[str]] = None       # ["en", "ru", "ua"...] или None = все
    include_translit: bool = True
    include_translations: bool = True
    include_geo_variants: bool = True
    include_prefixes_suffixes: bool = True
    include_topic_synonyms: bool = True
    max_per_seed: int = 100


@router.post("/keywords/expand")
async def expand_keywords_endpoint(
    body: ExpandKeywordsRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Расширяет seed-ключевики в десятки вариантов.
    Возвращает группированный результат: {seed: {category: [keywords...]}}
    """
    from utils.keyword_expander import expand_keywords

    if not body.seeds:
        raise HTTPException(status_code=400, detail="Укажите хотя бы один seed")

    seeds_clean = [s.strip() for s in body.seeds if s.strip()]
    if not seeds_clean:
        raise HTTPException(status_code=400, detail="Все seeds пустые")

    expanded = expand_keywords(
        seeds=seeds_clean,
        target_geos=body.target_geos,
        include_translit=body.include_translit,
        include_translations=body.include_translations,
        include_geo_variants=body.include_geo_variants,
        include_prefixes_suffixes=body.include_prefixes_suffixes,
        include_topic_synonyms=body.include_topic_synonyms,
        max_per_seed=body.max_per_seed,
    )

    # Преобразуем в JSON-friendly формат с группировкой по категориям
    result = {}
    total = 0
    for seed, keywords in expanded.items():
        by_category = {}
        for kw in keywords:
            by_category.setdefault(kw.category, []).append({
                "keyword": kw.keyword,
                "geo": kw.geo,
                "rating": kw.rating,
            })
        result[seed] = {
            "total": len(keywords),
            "categories": by_category,
            "flat": [kw.keyword for kw in keywords],  # плоский список для copy-paste
        }
        total += len(keywords)

    return {
        "seeds_count": len(seeds_clean),
        "total_keywords": total,
        "results": result,
    }


@router.get("/keywords/geos")
async def list_keyword_geos(
    current_user: User = Depends(get_current_user),
):
    """Возвращает доступные гео + готовые пресеты."""
    from utils.keyword_expander import list_available_geos, get_geo_presets
    return {
        "geos": list_available_geos(),
        "presets": get_geo_presets(),
    }

# ══════════════════════════════════════════════════════════
# Parser Metrics / Stats — дашборд парсера (v2, без ParsedChannel.source)
#
# Эти endpoints ЗАМЕНЯЮТ предыдущие /parser/stats/* в api/routers/parser.py
# Удали старые (если уже вставил) и вставь эти.
#
# Требует:
#   from datetime import datetime, timedelta
#   from sqlalchemy import func, case, cast, Date
#   from models.parser_event import ParserEvent
# ══════════════════════════════════════════════════════════


def _classify_source_sql():
    """SQL CASE-выражение: определяет source канала по search_query."""
    from sqlalchemy import case, func
    # similar:@xxx → similar
    # import → import
    # всё остальное → search (telegram)
    return case(
        (func.coalesce(ParsedChannel.search_query, '').like('similar:%'), 'similar'),
        (func.coalesce(ParsedChannel.search_query, '') == 'import', 'import'),
        else_='search',
    )


@router.get("/stats/overview")
async def get_parser_stats_overview(
    period: str = "all",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """KPI-метрики парсера: общее + сегодня."""
    from datetime import datetime, timedelta
    from sqlalchemy import func, case
    from models.parser_event import ParserEvent

    # ── общее: каналы в БД ──
    total_q = await db.execute(
        select(func.count(ParsedChannel.id)).where(ParsedChannel.user_id == current_user.id)
    )
    total_channels = total_q.scalar() or 0

    # ── по source (определяем через search_query) ──
    source_case = _classify_source_sql()
    by_src_q = await db.execute(
        select(
            source_case.label("source"),
            func.count(ParsedChannel.id).label("cnt"),
        ).where(ParsedChannel.user_id == current_user.id)
         .group_by(source_case)
    )
    by_source = {row[0]: row[1] for row in by_src_q.fetchall()}

    # ── сегодня: каналы добавленные сегодня ──
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_added_q = await db.execute(
        select(func.count(ParsedChannel.id)).where(
            ParsedChannel.user_id == current_user.id,
            ParsedChannel.added_at >= today_start,
        )
    )
    today_added = today_added_q.scalar() or 0

    # ── сегодня: FLOOD события ──
    flood_q = await db.execute(
        select(
            func.count(ParserEvent.id),
            func.coalesce(func.sum(ParserEvent.wait_seconds), 0),
        ).where(
            ParserEvent.user_id == current_user.id,
            ParserEvent.event_type == "flood_wait",
            ParserEvent.created_at >= today_start,
        )
    )
    row = flood_q.fetchone()
    today_flood_events = row[0] or 0
    today_flood_wait_sum = int(row[1] or 0)

    # ── Avg speed (каналов/мин) — из последних 10 session_done сегодня ──
    speed_q = await db.execute(
        select(ParserEvent.channels_saved, ParserEvent.duration_sec).where(
            ParserEvent.user_id == current_user.id,
            ParserEvent.event_type == "session_done",
            ParserEvent.created_at >= today_start,
            ParserEvent.duration_sec > 0,
            ParserEvent.channels_saved > 0,
        ).order_by(ParserEvent.created_at.desc()).limit(10)
    )
    rows = speed_q.fetchall()
    if rows:
        total_saved = sum(r[0] for r in rows)
        total_minutes = sum(r[1] for r in rows) / 60
        avg_speed = round(total_saved / total_minutes, 1) if total_minutes > 0 else 0
    else:
        avg_speed = 0

    # ── Всего FLOOD за всё время ──
    total_flood_q = await db.execute(
        select(func.count(ParserEvent.id)).where(
            ParserEvent.user_id == current_user.id,
            ParserEvent.event_type == "flood_wait",
        )
    )
    total_flood = total_flood_q.scalar() or 0

    return {
        "total_channels": total_channels,
        "today_added": today_added,
        "today_flood_events": today_flood_events,
        "today_flood_wait_seconds": today_flood_wait_sum,
        "total_flood_events": total_flood,
        "avg_speed_per_min": avg_speed,
        "by_source": by_source,
    }


@router.get("/stats/activity")
async def get_parser_activity(
    days: int = 7,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """График активности: сколько каналов добавлено в БД по дням."""
    from datetime import datetime, timedelta
    from sqlalchemy import func, cast, Date

    cutoff = datetime.utcnow() - timedelta(days=days)
    q = await db.execute(
        select(
            cast(ParsedChannel.added_at, Date).label("day"),
            func.count(ParsedChannel.id),
        ).where(
            ParsedChannel.user_id == current_user.id,
            ParsedChannel.added_at >= cutoff,
        ).group_by("day").order_by("day")
    )

    by_day = {row[0].isoformat(): row[1] for row in q.fetchall()}
    result = []
    today = datetime.utcnow().date()
    for i in range(days, -1, -1):
        d = today - timedelta(days=i)
        result.append({
            "date": d.isoformat(),
            "count": by_day.get(d.isoformat(), 0),
        })

    return {"days": result}


@router.get("/stats/flood-events")
async def get_flood_events(
    limit: int = 20,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Последние FLOOD_WAIT события."""
    from models.parser_event import ParserEvent

    q = await db.execute(
        select(ParserEvent).where(
            ParserEvent.user_id == current_user.id,
            ParserEvent.event_type == "flood_wait",
        ).order_by(ParserEvent.created_at.desc()).limit(limit)
    )
    events = q.scalars().all()

    return [
        {
            "id": e.id,
            "source": e.source,
            "wait_seconds": e.wait_seconds,
            "seed": e.seed,
            "details": e.details,
            "account_id": e.account_id,
            "created_at": e.created_at.isoformat() + "Z",
        }
        for e in events
    ]


@router.get("/stats/top-seeds")
async def get_top_seeds(
    limit: int = 10,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Топ seeds — какие search_query дали больше всего каналов."""
    from sqlalchemy import func

    q = await db.execute(
        select(
            ParsedChannel.search_query,
            func.count(ParsedChannel.id).label("cnt"),
        ).where(
            ParsedChannel.user_id == current_user.id,
            ParsedChannel.search_query != "",
        ).group_by(ParsedChannel.search_query)
         .order_by(func.count(ParsedChannel.id).desc())
         .limit(limit)
    )

    return [
        {"seed": row[0] or "(пусто)", "count": row[1]}
        for row in q.fetchall()
    ]


@router.get("/stats/by-account")
async def get_stats_by_account(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Сколько каналов нашёл каждый аккаунт (через parser_events.account_id)."""
    from sqlalchemy import func
    from models.parser_event import ParserEvent

    q = await db.execute(
        select(
            ParserEvent.account_id,
            func.coalesce(func.sum(ParserEvent.channels_saved), 0).label("saved"),
            func.coalesce(func.sum(ParserEvent.channels_found), 0).label("found"),
            func.count(ParserEvent.id).label("sessions"),
        ).where(
            ParserEvent.user_id == current_user.id,
            ParserEvent.event_type == "session_done",
            ParserEvent.account_id.isnot(None),
        ).group_by(ParserEvent.account_id)
         .order_by(func.sum(ParserEvent.channels_saved).desc())
    )

    rows = q.fetchall()
    if not rows:
        return []

    acc_ids = [r[0] for r in rows]
    acc_q = await db.execute(
        select(TelegramAccount).where(TelegramAccount.id.in_(acc_ids))
    )
    acc_map = {a.id: a for a in acc_q.scalars().all()}

    result = []
    for r in rows:
        acc = acc_map.get(r[0])
        result.append({
            "account_id": r[0],
            "name": (acc.first_name or acc.phone or f"#{r[0]}") if acc else f"#{r[0]}",
            "saved": int(r[1]),
            "found": int(r[2]),
            "sessions": int(r[3]),
        })
    return result


@router.get("/stats/sessions")
async def get_recent_sessions(
    limit: int = 20,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Последние завершённые сессии парсинга."""
    from models.parser_event import ParserEvent

    q = await db.execute(
        select(ParserEvent).where(
            ParserEvent.user_id == current_user.id,
            ParserEvent.event_type == "session_done",
        ).order_by(ParserEvent.created_at.desc()).limit(limit)
    )
    events = q.scalars().all()

    return [
        {
            "id": e.id,
            "source": e.source,
            "account_id": e.account_id,
            "found": e.channels_found,
            "saved": e.channels_saved,
            "duration_sec": e.duration_sec,
            "speed_per_min": round(e.channels_saved / (e.duration_sec / 60), 1) if e.duration_sec > 0 else 0,
            "details": e.details,
            "created_at": e.created_at.isoformat() + "Z",
        }
        for e in events
    ]

# ══════════════════════════════════════════════════════════
# Language Detector
# ══════════════════════════════════════════════════════════

class DetectLanguageRequest(BaseModel):
    folder: str = ""
    limit: int = 500
    auto_folder: bool = True
    only_unknown: bool = True

@router.post("/language/start")
async def start_detect_language(
    body: DetectLanguageRequest,
    current_user: User = Depends(get_current_user)
):
    from celery_app import celery_app
    task = celery_app.send_task(
        "tasks.parser_similar_tasks.run_detect_language",
        args=[current_user.id, body.model_dump()],
        queue="ai_dialogs",
    )
    return {"task_id": task.id, "status": "started"}

@router.get("/language/progress")
async def get_language_progress(current_user: User = Depends(get_current_user)):
    import redis as redis_lib
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    progress = r.get(f"parser:lang:progress:{current_user.id}")
    if not progress: return {"status": "idle"}
    
    try:
        parts = progress.decode().split("|")
        return {
            "status": parts[0],
            "checked": int(parts[1]),
            "detected": int(parts[2]),
            "remaining": int(parts[3]),
            "current": parts[4] if len(parts) > 4 else "",
        }
    except Exception:
        return {"status": "idle"}

@router.post("/language/stop")
async def stop_language(current_user: User = Depends(get_current_user)):
    import redis as redis_lib
    r = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    r.setex(f"parser:lang:stop:{current_user.id}", 300, "1")
    return {"status": "stopping"}


# ═══════════════════════════════════════════════════════════════════
# Web-парсер (Camoufox + пул из ~34 статических IPv4)
# ═══════════════════════════════════════════════════════════════════
# Эндпоинты для отказоустойчивого парсинга публичной статистики со SPA.
# Внутрянка: api/utils/web_scraper (worker pool + cooldown + UX-эмуляция),
# Celery-таск: tasks.web_scraper_tasks.run_web_scraper.
#
# Flow:
#   1. POST /parser/web/scrape/start  — фронт шлёт urls + proxies + options
#      → бэк генерит job_id, ставит таск в queue=parsers, возвращает job_id
#   2. GET /parser/web/scrape/progress?job_id=X — фронт поллит каждые 2 сек
#   3. POST /parser/web/scrape/stop?job_id=X — фронт ставит cancel-флаг
#   4. GET /parser/web/scrape/results?job_id=X — скачать JSONL с результатами

import json as _json
import uuid as _uuid
from pathlib import Path as _Path
from fastapi.responses import FileResponse as _FileResponse


def _ws_progress_key(user_id: int, job_id: str) -> str:
    return f"gramgpt:web_scraper:progress:{user_id}:{job_id}"


def _ws_cancel_key(user_id: int, job_id: str) -> str:
    return f"gramgpt:web_scraper:cancel:{user_id}:{job_id}"


def _ws_jsonl_path(user_id: int, job_id: str) -> _Path:
    api_dir = _Path(__file__).resolve().parent.parent
    return (api_dir / ".." / "data" / "web_scraper" / str(user_id) / f"{job_id}.jsonl").resolve()


class WebScrapeStartRequest(BaseModel):
    urls: list[str]
    proxies: list[str]
    # Все опции необязательны — есть безопасные дефолты в Celery-таске
    max_workers: Optional[int] = 3
    max_retries: Optional[int] = 3
    page_timeout_sec: Optional[float] = 60.0
    cooldown_min_sec: Optional[int] = 900
    cooldown_max_sec: Optional[int] = 1200
    node_rotation_min_sec: Optional[float] = 15.0
    node_rotation_max_sec: Optional[float] = 35.0
    page_locale: Optional[str] = "en-US"
    humanize: Optional[bool] = True
    headless: Optional[bool] = True


@router.post("/web/scrape/start")
async def web_scrape_start(
    body: WebScrapeStartRequest,
    current_user: User = Depends(get_current_user),
):
    """Запускает Celery-таск Camoufox-скрейпинга. Возвращает job_id."""
    urls = [u.strip() for u in body.urls if u and u.strip()]
    proxies = [p.strip() for p in body.proxies if p and p.strip()]

    if not urls:
        raise HTTPException(status_code=400, detail="urls пустой")
    if not proxies:
        raise HTTPException(status_code=400, detail="proxies пустой")
    if len(proxies) < 3:
        # Меньше 3 узлов = нет смысла в cooldown'е, сразу упрёмся
        raise HTTPException(
            status_code=400,
            detail=f"Минимум 3 прокси для скрейпинга (передано {len(proxies)})",
        )

    job_id = _uuid.uuid4().hex

    options = {
        "max_workers": max(1, min(int(body.max_workers or 3), 6)),
        "max_retries": max(1, min(int(body.max_retries or 3), 5)),
        "page_timeout_sec": float(body.page_timeout_sec or 60.0),
        "cooldown_min_sec": int(body.cooldown_min_sec or 900),
        "cooldown_max_sec": int(body.cooldown_max_sec or 1200),
        "node_rotation_min_sec": float(body.node_rotation_min_sec or 15.0),
        "node_rotation_max_sec": float(body.node_rotation_max_sec or 35.0),
        "page_locale": body.page_locale or "en-US",
        "camoufox": {
            "humanize": bool(body.humanize),
            "headless": bool(body.headless),
        },
    }

    from celery_app import celery_app
    task = celery_app.send_task(
        "tasks.web_scraper_tasks.run_web_scraper",
        args=[current_user.id, job_id, urls, proxies, options],
        queue="parsers",
    )

    # Сразу публикуем initial progress, чтобы фронт не получил пустоту на
    # первом поллинге, пока Celery поднимает Camoufox.
    import redis as _redis_lib
    r = _redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    initial = {
        "status": "queued",
        "job_id": job_id,
        "urls_total": len(urls),
        "proxies_total": len(proxies),
        "ts": int(__import__("time").time()),
    }
    r.setex(_ws_progress_key(current_user.id, job_id), 86400,
            _json.dumps(initial, ensure_ascii=False))

    return {
        "job_id": job_id,
        "task_id": task.id,
        "urls_total": len(urls),
        "proxies_total": len(proxies),
        "status": "queued",
    }


@router.get("/web/scrape/progress")
async def web_scrape_progress(
    job_id: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    """Текущий snapshot прогресса. Фронт поллит каждые 2 сек."""
    import redis as _redis_lib
    r = _redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    raw = r.get(_ws_progress_key(current_user.id, job_id))
    if not raw:
        return {"status": "unknown", "job_id": job_id}
    try:
        return _json.loads(raw)
    except Exception:
        return {"status": "unknown", "job_id": job_id, "raw": raw.decode("utf-8", errors="ignore")}


@router.post("/web/scrape/stop")
async def web_scrape_stop(
    job_id: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    """
    Выставляет cancel-флаг. WebScraper.cancel_check() читает его раз
    в ~0.5-2 сек, после чего воркеры завершают текущий URL и выходят.
    """
    import redis as _redis_lib
    r = _redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    r.setex(_ws_cancel_key(current_user.id, job_id), 3600, "1")
    return {"status": "cancelling", "job_id": job_id}


@router.get("/web/scrape/results")
async def web_scrape_results(
    job_id: str = Query(...),
    current_user: User = Depends(get_current_user),
):
    """Скачивает JSONL с результатами. Содержит даже частично записанные строки."""
    path = _ws_jsonl_path(current_user.id, job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Файл результатов не найден")
    return _FileResponse(
        str(path),
        media_type="application/x-ndjson",
        filename=f"web_scrape_{job_id}.jsonl",
    )


@router.get("/web/scrape/jobs")
async def web_scrape_list_jobs(current_user: User = Depends(get_current_user)):
    """
    Список последних job'ов юзера — по содержимому data/web_scraper/{user_id}/.
    Для UI "история запусков".
    """
    api_dir = _Path(__file__).resolve().parent.parent
    user_dir = (api_dir / ".." / "data" / "web_scraper" / str(current_user.id)).resolve()
    if not user_dir.exists():
        return {"jobs": []}
    jobs = []
    for f in sorted(user_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
        try:
            stat = f.stat()
            # Считаем строки = записи. Дёшево — файл максимум на 10к URL.
            with f.open("r", encoding="utf-8") as fh:
                count = sum(1 for _ in fh)
            jobs.append({
                "job_id": f.stem,
                "records": count,
                "size_bytes": stat.st_size,
                "modified_at": int(stat.st_mtime),
            })
        except Exception:
            continue
    return {"jobs": jobs}