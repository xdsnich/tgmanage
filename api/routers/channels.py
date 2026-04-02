"""
GramGPT API — routers/channels.py
Управление каналами. Все подключения через make_telethon_client (с прокси).
"""

import sys
import os
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount
from models.proxy import Proxy

router = APIRouter(prefix="/channels", tags=["channels"])


# ── Schemas ──────────────────────────────────────────────────

class CreateChannelRequest(BaseModel):
    account_id: int
    title: str
    description: str = ""
    username: str = ""

class BatchCreateRequest(BaseModel):
    account_ids: list[int]
    title_template: str
    description: str = ""
    delay: float = 4.0

class PinChannelRequest(BaseModel):
    account_id: int
    channel_link: str


# ── Helper ───────────────────────────────────────────────────

async def _get_account(db, account_id, user_id) -> TelegramAccount:
    result = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.id == account_id,
            TelegramAccount.user_id == user_id,
        )
    )
    acc = result.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    return acc


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


# ── Endpoints ────────────────────────────────────────────────

@router.get("/accounts/{account_id}")
async def get_my_channels(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список каналов которыми владеет аккаунт"""
    acc = await _get_account(db, account_id, current_user.id)
    client = await _get_client(acc, db)

    channels = []
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return {"account_id": account_id, "channels": []}

        from telethon.tl.types import Channel
        dialogs = await client.get_dialogs()

        for dialog in dialogs:
            entity = dialog.entity
            if isinstance(entity, Channel) and entity.broadcast and entity.creator:
                link = f"https://t.me/{entity.username}" if entity.username else f"id{entity.id}"
                channels.append({
                    "id": entity.id,
                    "title": entity.title,
                    "username": entity.username or "",
                    "link": link,
                    "members": getattr(entity, "participants_count", 0),
                })

        if channels:
            acc.channels = channels
            await db.flush()

        await client.disconnect()
        return {"account_id": account_id, "channels": channels}

    except Exception as e:
        try: await client.disconnect()
        except: pass
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")


@router.post("/create")
async def create_channel(
    body: CreateChannelRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Создать канал от имени аккаунта"""
    acc = await _get_account(db, body.account_id, current_user.id)
    client = await _get_client(acc, db)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(status_code=400, detail="Сессия не активна")

        from telethon.tl.functions.channels import CreateChannelRequest as TgCreateChannel, UpdateUsernameRequest
        from telethon import errors

        result = await client(TgCreateChannel(
            title=body.title, about=body.description,
            broadcast=True, megagroup=False,
        ))

        channel = result.chats[0]
        channel_link = f"https://t.me/{channel.username}" if channel.username else f"id{channel.id}"

        if body.username:
            try:
                await client(UpdateUsernameRequest(channel=channel, username=body.username))
                channel_link = f"https://t.me/{body.username}"
            except errors.UsernameInvalidError:
                pass

        channel_data = {
            "id": channel.id, "title": body.title,
            "username": body.username, "link": channel_link,
            "description": body.description,
        }

        channels = acc.channels or []
        channels.append(channel_data)
        acc.channels = channels
        await db.flush()
        await client.disconnect()

        return {"success": True, "channel": channel_data}

    except HTTPException: raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")


@router.post("/pin")
async def pin_channel(
    body: PinChannelRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Закрепить канал в профиле"""
    acc = await _get_account(db, body.account_id, current_user.id)
    client = await _get_client(acc, db)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(status_code=400, detail="Сессия не активна")

        link = body.channel_link
        if link.startswith("@"): link = f"https://t.me/{link[1:]}"
        elif not link.startswith("http"): link = f"https://t.me/{link}"

        entity = await client.get_entity(link)

        from telethon.tl.functions.account import UpdatePersonalChannelRequest
        await client(UpdatePersonalChannelRequest(channel=entity))

        channels = acc.channels or []
        existing = next((c for c in channels if c.get("link") == link), None)
        if not existing:
            channels.append({"id": entity.id, "title": getattr(entity, 'title', ''), "link": link})
            acc.channels = channels
            await db.flush()

        await client.disconnect()
        return {"success": True, "message": f"Канал {link} закреплён"}

    except HTTPException: raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")


@router.post("/batch-create")
async def batch_create_channels(
    body: BatchCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Создать каналы для нескольких аккаунтов через Celery"""
    result = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.user_id == current_user.id,
            TelegramAccount.id.in_(body.account_ids),
        )
    )
    accounts = result.scalars().all()

    from celery import current_app
    accounts_data = [{"phone": a.phone, "session_file": a.session_file, "channels": a.channels or [], "first_name": a.first_name or ""} for a in accounts]

    task = current_app.send_task(
        "tasks.bulk_tasks.create_channels_bulk",
        args=[accounts_data, body.title_template, body.description, body.delay],
        queue="bulk_actions",
    )
    return {"task_id": task.id, "total": len(accounts_data)}