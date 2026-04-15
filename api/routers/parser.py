"""
GramGPT API — routers/parser.py
Парсер целевых каналов. Все подключения через make_telethon_client (с прокси).
"""

import sys
import os
import csv
import io
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
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

router = APIRouter(prefix="/parser", tags=["parser"])


# ── Helper ───────────────────────────────────────────────────

async def _get_client(acc, db):
    """Создаёт TelegramClient С ПРОКСИ"""
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
    account_id: int
    keywords: str
    min_subscribers: int = 0
    max_subscribers: int = 1000000
    only_with_comments: bool = False
    active_hours: int = 0
    source: str = "telegram"  # "telegram" | "tgstat" | "both"

class ImportRequest(BaseModel):
    channels: list[str]


# ── Endpoints ────────────────────────────────────────────────

@router.get("/channels")
async def list_parsed_channels(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ParsedChannel).where(ParsedChannel.user_id == current_user.id).order_by(ParsedChannel.subscribers.desc())
    )
    channels = result.scalars().all()
    return [{
        "id": c.id, "username": c.username, "title": c.title,
        "subscribers": c.subscribers, "has_comments": c.has_comments,
        "last_post_date": c.last_post_date.isoformat() if c.last_post_date else None,
        "search_query": c.search_query, "added_at": c.added_at.isoformat(),
        "folder": c.folder or "",
    } for c in channels]


@router.post("/search")
async def search_channels(
    body: SearchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Поиск каналов через Telegram С ПРОКСИ."""
    acc_r = await db.execute(
        select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(TelegramAccount.id == body.account_id, TelegramAccount.user_id == current_user.id)
    )
    acc = acc_r.scalar_one_or_none()
    if not acc or not acc.session_file:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")

    found = []
    seen_usernames = set()
    keywords = [k.strip() for k in body.keywords.split(",") if k.strip()]
    print(f"🔍 Парсер: keywords={keywords}, source={body.source}, account={acc.phone}")

    # ── Telegram поиск ───────────────────────────────────────
    if body.source in ("telegram", "both"):
        client = await _get_client(acc, db)

        from telethon.tl.functions.contacts import SearchRequest as TgSearchRequest
        from telethon.tl.functions.messages import SearchGlobalRequest
        from telethon.tl.types import Channel, InputMessagesFilterEmpty, InputPeerEmpty
        import asyncio

        try:
            await client.connect()
            print(f"🔍 Telegram: подключён")
            if not await client.is_user_authorized():
                await client.disconnect()
                raise HTTPException(status_code=400, detail="Сессия не активна")

            for kw in keywords:
                print(f"🔍 Telegram: ищу '{kw}'...")

                # Метод 1: contacts.Search
                try:
                    result = await client(TgSearchRequest(q=kw, limit=20))
                    print(f"🔍 contacts.Search: найдено {len(result.chats)} чатов")
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
                        except Exception as e:
                            print(f"🔍 Ошибка get_messages @{chat.username}: {e}")

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
                        print(f"🔍 + @{chat.username} ({subs} подписчиков)")
                except Exception as e:
                    print(f"🔍 contacts.Search ошибка: {e}")

                await asyncio.sleep(1)

                # Метод 2: messages.SearchGlobal
                try:
                    global_result = await client(SearchGlobalRequest(
                        q=kw, filter=InputMessagesFilterEmpty(),
                        min_date=datetime(2020, 1, 1), max_date=datetime.utcnow(),
                        offset_rate=0, offset_peer=InputPeerEmpty(),
                        offset_id=0, limit=50,
                    ))
                    print(f"🔍 SearchGlobal: найдено {len(global_result.chats)} чатов")
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
                        except Exception as e:
                            print(f"🔍 Ошибка get_messages @{chat.username}: {e}")

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
                        print(f"🔍 + @{chat.username} ({subs} подписчиков)")
                except Exception as e:
                    print(f"🔍 SearchGlobal ошибка: {e}")

                await asyncio.sleep(2)

            await client.disconnect()
            print(f"🔍 Telegram: отключён, найдено {len(found)} каналов")

        except HTTPException: raise
        except Exception as e:
            try: await client.disconnect()
            except: pass
            print(f"🔍 Telegram ОШИБКА: {e}")
            raise HTTPException(status_code=500, detail=f"Ошибка Telegram: {str(e)[:200]}")

    # ── TGStat API ───────────────────────────────────────────
    if body.source in ("tgstat", "both"):
        tgstat_token = os.getenv("TGSTAT_API_KEY", "")
        if tgstat_token:
            import httpx
            print(f"🔍 TGStat: начинаю поиск...")
            for kw in keywords:
                try:
                    async with httpx.AsyncClient(timeout=15) as http:
                        resp = await http.get("https://api.tgstat.ru/channels/search",
                                              params={"token": tgstat_token, "q": kw, "limit": 50})
                        print(f"🔍 TGStat '{kw}': status={resp.status_code}")
                        if resp.status_code == 200:
                            items = resp.json().get("response", {}).get("items", [])
                            print(f"🔍 TGStat '{kw}': найдено {len(items)} каналов")
                            for item in items:
                                username = (item.get("username") or "").replace("@", "")
                                if not username or username in seen_usernames:
                                    continue
                                seen_usernames.add(username)
                                subs = item.get("participants_count", 0) or 0
                                if subs < body.min_subscribers or subs > body.max_subscribers:
                                    continue
                                found.append({
                                    "channel_id": item.get("id", 0), "username": username,
                                    "title": item.get("title", username), "subscribers": subs,
                                    "has_comments": True, "last_post_date": None, "search_query": kw,
                                })
                        else:
                            print(f"🔍 TGStat ошибка: {resp.text[:200]}")
                except Exception as e:
                    print(f"🔍 TGStat ошибка: {e}")
                await asyncio.sleep(1)
        else:
            print("🔍 TGStat: TGSTAT_API_KEY не задан в .env")

    # Сохраняем в БД
    saved = 0
    for ch in found:
        existing = await db.execute(
            select(ParsedChannel).where(ParsedChannel.user_id == current_user.id, ParsedChannel.username == ch["username"])
        )
        if existing.scalar_one_or_none():
            continue

        post_date = None
        if ch["last_post_date"]:
            dt = datetime.fromisoformat(ch["last_post_date"])
            post_date = dt.replace(tzinfo=None)

        db.add(ParsedChannel(
            user_id=current_user.id, channel_id=ch["channel_id"],
            username=ch["username"], title=ch["title"],
            subscribers=ch["subscribers"], has_comments=ch["has_comments"],
            last_post_date=post_date, search_query=ch["search_query"],
        ))
        saved += 1

    await db.flush()
    print(f"🔍 ИТОГО: найдено {len(found)}, сохранено {saved}")
    return {"found": len(found), "saved": saved, "channels": found}


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
    await db.execute(delete(ParsedChannel).where(ParsedChannel.user_id == current_user.id))
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
    writer.writerow(["username", "title", "subscribers", "has_comments", "last_post", "query"])
    for c in channels:
        writer.writerow([f"@{c.username}", c.title, c.subscribers, c.has_comments,
                         c.last_post_date.isoformat() if c.last_post_date else "", c.search_query])

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
    """Уникальные папки + количество каналов в каждой"""
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
    """Каналы в конкретной папке"""
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
    """Назначить папку нескольким каналам"""
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
    """Изменить папку одного канала"""
    result = await db.execute(
        select(ParsedChannel).where(ParsedChannel.id == channel_id, ParsedChannel.user_id == current_user.id)
    )
    ch = result.scalar_one_or_none()
    if not ch:
        raise HTTPException(status_code=404, detail="Канал не найден")
    ch.folder = body.get("folder", "")
    await db.flush()
    return {"id": ch.id, "folder": ch.folder}