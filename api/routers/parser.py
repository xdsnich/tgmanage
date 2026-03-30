"""
GramGPT API — routers/parser.py
Парсер целевых каналов: поиск по ключам, фильтрация, экспорт/импорт.
По ТЗ раздел 3.5.
"""

import sys
import os
import csv
import io
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount
from models.parsed_channel import ParsedChannel

router = APIRouter(prefix="/parser", tags=["parser"])


# ── Safe CLI import ──────────────────────────────────────────

def _get_cli_config():
    import importlib.util
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    config_path = os.path.join(root_dir, "config.py")
    spec = importlib.util.spec_from_file_location("cli_config", config_path)
    cli_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli_config)
    return cli_config


# ── Schemas ──────────────────────────────────────────────────

class SearchRequest(BaseModel):
    account_id: int
    keywords: str                  # "криптовалюта, крипта, блокчейн"
    min_subscribers: int = 0
    max_subscribers: int = 1000000
    only_with_comments: bool = False
    active_hours: int = 0          # 0 = без фильтра, 48 = посты за последние 48ч


class ImportRequest(BaseModel):
    channels: list[str]            # ["@channel1", "@channel2"]


# ── Endpoints ────────────────────────────────────────────────

@router.get("/channels")
async def list_parsed_channels(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список спарсенных каналов"""
    result = await db.execute(
        select(ParsedChannel).where(ParsedChannel.user_id == current_user.id).order_by(ParsedChannel.subscribers.desc())
    )
    channels = result.scalars().all()
    return [{
        "id": c.id,
        "username": c.username,
        "title": c.title,
        "subscribers": c.subscribers,
        "has_comments": c.has_comments,
        "last_post_date": c.last_post_date.isoformat() if c.last_post_date else None,
        "search_query": c.search_query,
        "added_at": c.added_at.isoformat(),
    } for c in channels]


@router.post("/search")
async def search_channels(
    body: SearchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Поиск каналов по ключевым словам через Telegram.
    Использует Telethon для поиска публичных каналов.
    """
    # Проверяем аккаунт
    acc_r = await db.execute(
        select(TelegramAccount).where(TelegramAccount.id == body.account_id, TelegramAccount.user_id == current_user.id)
    )
    acc = acc_r.scalar_one_or_none()
    if not acc or not acc.session_file:
        raise HTTPException(status_code=404, detail="Аккаунт не найден или без сессии")

    cli_config = _get_cli_config()

    from telethon import TelegramClient
    from telethon.tl.functions.contacts import SearchRequest as TgSearchRequest
    from telethon.tl.functions.messages import SearchGlobalRequest
    from telethon.tl.types import Channel, InputMessagesFilterEmpty, InputPeerEmpty
    import asyncio

    session_path = acc.session_file.replace(".session", "")
    client = TelegramClient(
        session_path, cli_config.API_ID, cli_config.API_HASH,
        device_model="Desktop", system_version="Windows 10", app_version="4.14.15",
    )

    found = []
    seen_usernames = set()
    keywords = [k.strip() for k in body.keywords.split(",") if k.strip()]

    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise HTTPException(status_code=400, detail="Сессия не активна")

        for kw in keywords:
            # ── Метод 1: contacts.Search (по названию канала) ────
            try:
                result = await client(TgSearchRequest(q=kw, limit=20))
                for chat in result.chats:
                    if not isinstance(chat, Channel) or not chat.broadcast:
                        continue
                    if not chat.username or chat.username in seen_usernames:
                        continue
                    seen_usernames.add(chat.username)

                    subs = getattr(chat, 'participants_count', 0) or 0
                    if subs < body.min_subscribers or subs > body.max_subscribers:
                        continue

                    has_comments = False
                    last_post = None
                    try:
                        msgs = await client.get_messages(chat, limit=1)
                        if msgs:
                            last_post = msgs[0].date
                            if msgs[0].replies and getattr(msgs[0].replies, 'comments', False):
                                has_comments = True
                    except:
                        pass

                    if body.only_with_comments and not has_comments:
                        continue
                    if body.active_hours > 0 and last_post:
                        cutoff = datetime.utcnow() - timedelta(hours=body.active_hours)
                        if last_post.replace(tzinfo=None) < cutoff:
                            continue

                    found.append({
                        "channel_id": chat.id, "username": chat.username, "title": chat.title,
                        "subscribers": subs, "has_comments": has_comments,
                        "last_post_date": last_post.isoformat() if last_post else None, "search_query": kw,
                    })
            except:
                pass

            await asyncio.sleep(1)

            # ── Метод 2: messages.SearchGlobal (по тексту постов) ──
            try:
                global_result = await client(SearchGlobalRequest(
                    q=kw, filter=InputMessagesFilterEmpty(),
                    min_date=datetime(2020, 1, 1), max_date=datetime.utcnow(),
                    offset_rate=0, offset_peer=InputPeerEmpty(),
                    offset_id=0, limit=50,
                ))

                for chat in global_result.chats:
                    if not isinstance(chat, Channel) or not chat.broadcast:
                        continue
                    if not chat.username or chat.username in seen_usernames:
                        continue
                    seen_usernames.add(chat.username)

                    subs = getattr(chat, 'participants_count', 0) or 0
                    if subs < body.min_subscribers or subs > body.max_subscribers:
                        continue

                    has_comments = False
                    last_post = None
                    try:
                        msgs = await client.get_messages(chat, limit=1)
                        if msgs:
                            last_post = msgs[0].date
                            if msgs[0].replies and getattr(msgs[0].replies, 'comments', False):
                                has_comments = True
                    except:
                        pass

                    if body.only_with_comments and not has_comments:
                        continue
                    if body.active_hours > 0 and last_post:
                        cutoff = datetime.utcnow() - timedelta(hours=body.active_hours)
                        if last_post.replace(tzinfo=None) < cutoff:
                            continue

                    found.append({
                        "channel_id": chat.id, "username": chat.username, "title": chat.title,
                        "subscribers": subs, "has_comments": has_comments,
                        "last_post_date": last_post.isoformat() if last_post else None, "search_query": kw,
                    })
            except:
                pass

            await asyncio.sleep(2)  # Пауза между ключевыми словами

        await client.disconnect()

    except HTTPException:
        raise
    except Exception as e:
        try:
            await client.disconnect()
        except:
            pass
        raise HTTPException(status_code=500, detail=f"Ошибка поиска: {str(e)}")

    # ── Метод 3: TGStat API (если есть ключ) ────────────────
    tgstat_token = os.getenv("TGSTAT_API_KEY", "")
    if tgstat_token:
        import httpx
        for kw in keywords:
            try:
                async with httpx.AsyncClient(timeout=15) as http:
                    resp = await http.get(
                        "https://api.tgstat.ru/channels/search",
                        params={
                            "token": tgstat_token,
                            "q": kw,
                            "limit": 50,
                            "country": "",
                        },
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        for item in data.get("response", {}).get("items", []):
                            username = (item.get("username") or "").replace("@", "")
                            if not username or username in seen_usernames:
                                continue
                            seen_usernames.add(username)

                            subs = item.get("participants_count", 0) or 0
                            if subs < body.min_subscribers or subs > body.max_subscribers:
                                continue

                            found.append({
                                "channel_id": item.get("id", 0),
                                "username": username,
                                "title": item.get("title", username),
                                "subscribers": subs,
                                "has_comments": True,  # TGStat не даёт эту инфу, ставим True
                                "last_post_date": None,
                                "search_query": kw,
                            })
            except:
                pass
            await asyncio.sleep(1)

    # Сохраняем в БД
    saved = 0
    for ch in found:
        existing = await db.execute(
            select(ParsedChannel).where(ParsedChannel.user_id == current_user.id, ParsedChannel.username == ch["username"])
        )
        if existing.scalar_one_or_none():
            continue

        # Убираем timezone из даты (PostgreSQL TIMESTAMP WITHOUT TIME ZONE)
        post_date = None
        if ch["last_post_date"]:
            dt = datetime.fromisoformat(ch["last_post_date"])
            post_date = dt.replace(tzinfo=None)

        db.add(ParsedChannel(
            user_id=current_user.id,
            channel_id=ch["channel_id"],
            username=ch["username"],
            title=ch["title"],
            subscribers=ch["subscribers"],
            has_comments=ch["has_comments"],
            last_post_date=post_date,
            search_query=ch["search_query"],
        ))
        saved += 1

    await db.flush()

    return {
        "found": len(found),
        "saved": saved,
        "channels": found,
    }


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
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Удалить все спарсенные каналы"""
    await db.execute(delete(ParsedChannel).where(ParsedChannel.user_id == current_user.id))
    await db.flush()


@router.get("/export")
async def export_channels_csv(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Экспорт каналов в CSV"""
    result = await db.execute(
        select(ParsedChannel).where(ParsedChannel.user_id == current_user.id).order_by(ParsedChannel.subscribers.desc())
    )
    channels = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["username", "title", "subscribers", "has_comments", "last_post", "query"])
    for c in channels:
        writer.writerow([
            f"@{c.username}", c.title, c.subscribers, c.has_comments,
            c.last_post_date.isoformat() if c.last_post_date else "",
            c.search_query,
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=channels.csv"},
    )


@router.post("/import")
async def import_channels(
    body: ImportRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Импорт списка каналов (юзернеймы/ссылки)"""
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

        db.add(ParsedChannel(
            user_id=current_user.id,
            username=username,
            title=username,
            search_query="import",
        ))
        added += 1

    await db.flush()
    return {"added": added}