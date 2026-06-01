"""
GramGPT API — routers/channels.py
Управление каналами. Все подключения через make_telethon_client (с прокси).
"""

import sys
import os
import asyncio
import logging
logger = logging.getLogger(__name__)
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
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
        select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(
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

class CreateFullChannelRequest(BaseModel):
    account_id: int
    title: str
    description: str = ""
    username: str = ""
    first_post: str = ""  # Опционально — первый пост
    pin_to_profile: bool = True  # Закрепить в профиле автоматически


@router.post("/create-full")
async def create_channel_full(
    account_id: int = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    username: str = Form(""),
    first_post: str = Form(""),
    pin_to_profile: bool = Form(True),
    reconnect_pause_sec: int = Form(2),
    post_photo: UploadFile = File(None),
    avatar: UploadFile = File(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Создание канала в два сетевых этапа на одном session-файле:
    1) Сетап канала: create + username + аватар + закрепить в профиле.
    2) client.disconnect() → sleep(reconnect_pause_sec=2) → client.connect()
       (та же сессия, без повторной авторизации — просто сброс сетевого
       коннекта, чтобы Telegram увидел пост как новую активность).
    3) Первый пост.
    Это даёт результат как «человек закрыл и снова открыл приложение перед
    тем как написать пост» и не триггерит флуд-фильтр на свежем канале.
    """
    import tempfile

    acc = await _get_account(db, account_id, current_user.id)
    client = await _get_client(acc, db)

    # Сохраняем файлы во временные пути
    post_photo_path = None
    avatar_path = None
    if post_photo and post_photo.filename:
        suffix = "." + (post_photo.filename.split(".")[-1] if "." in post_photo.filename else "jpg")
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await post_photo.read())
            post_photo_path = tmp.name

    if avatar and avatar.filename:
        suffix = "." + (avatar.filename.split(".")[-1] if "." in avatar.filename else "jpg")
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await avatar.read())
            avatar_path = tmp.name

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(status_code=400, detail="Сессия не активна")

        from telethon.tl.functions.channels import (
            CreateChannelRequest as TgCreateChannel,
            UpdateUsernameRequest,
            EditPhotoRequest,
        )
        from telethon.tl.types import InputChatUploadedPhoto
        from telethon import errors

        # 1. Создаём канал
        result = await client(TgCreateChannel(
            title=title,
            about=description,
            broadcast=True,
            megagroup=False,
        ))
        channel = result.chats[0]

        # 2. Username
        channel_link = f"https://t.me/c/{channel.id}"
        if username:
            try:
                clean_username = username.lstrip('@').strip()
                await client(UpdateUsernameRequest(channel=channel, username=clean_username))
                channel_link = f"https://t.me/{clean_username}"
                await asyncio.sleep(1)
            except errors.UsernameInvalidError:
                return {"success": False, "error": "Username недоступен или невалиден"}
            except errors.UsernameOccupiedError:
                return {"success": False, "error": "Username уже занят"}
            except Exception as e:
                logger.warning(f"Set username: {e}")

        # 3. Аватар канала
        avatar_ok = False
        if avatar_path:
            try:
                file_handle = await client.upload_file(avatar_path)
                await client(EditPhotoRequest(
                    channel=channel,
                    photo=InputChatUploadedPhoto(file=file_handle),
                ))
                avatar_ok = True
                await asyncio.sleep(1)
            except Exception as e:
                logger.warning(f"Avatar upload: {e}")

        # 4. Закрепляем канал в профиле (это часть «настройки канала», не контента)
        pin_ok = False
        if pin_to_profile:
            try:
                from telethon.tl.functions.messages import ToggleDialogPinRequest
                from telethon.tl.types import InputDialogPeer, InputPeerChannel
                peer = InputPeerChannel(channel_id=channel.id, access_hash=channel.access_hash)
                await client(ToggleDialogPinRequest(
                    peer=InputDialogPeer(peer=peer),
                    pinned=True,
                ))
                pin_ok = True
            except Exception as e:
                logger.warning(f"Pin channel: {e}")

        # === КАНАЛ СОЗДАН ===
        # Дальше — отдельный сетевой этап «первый пост»: тот же session-файл,
        # но disconnect → пауза → reconnect (имитация «закрыл и снова открыл
        # приложение») — иначе Telegram режет пост на свежем канале.
        first_post_ok = False
        has_post_text = bool(first_post.strip())
        has_post_photo = post_photo_path is not None

        if has_post_text or has_post_photo:
            pause = max(0, int(reconnect_pause_sec))
            logger.info(f"[create-full] channel {channel.id} setup done — disconnect, sleep {pause}s, reconnect, then first post")
            try:
                await client.disconnect()
            except Exception as e:
                logger.warning(f"disconnect before post: {e}")
            if pause:
                await asyncio.sleep(pause)
            try:
                await asyncio.wait_for(client.connect(), timeout=30)
            except Exception as e:
                logger.warning(f"reconnect before post: {e}")

            try:
                if has_post_photo:
                    await client.send_file(
                        entity=channel,
                        file=post_photo_path,
                        caption=first_post if has_post_text else "",
                    )
                else:
                    await client.send_message(entity=channel, message=first_post)
                first_post_ok = True
            except Exception as e:
                logger.warning(f"First post: {e}")

        channel_data = {
            "id": channel.id,
            "title": title,
            "username": username.lstrip('@') if username else "",
            "link": channel_link,
            "description": description,
        }

        # Сохраняем в БД
        channels = acc.channels or []
        channels.append(channel_data)
        acc.channels = channels
        await db.flush()

        await client.disconnect()

        return {
            "success": True,
            "channel": channel_data,
            "first_post_published": first_post_ok,
            "first_post_has_photo": has_post_photo,
            "pinned_to_profile": pin_ok,
            "avatar_set": avatar_ok,
        }

    except HTTPException:
        raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        logger.error(f"create_channel_full error: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")
    finally:
        # Удаляем временные файлы
        import os as _os
        for p in [post_photo_path, avatar_path]:
            if p:
                try: _os.unlink(p)
                except: pass
                
@router.post("/set-avatar")
async def set_channel_avatar(
    account_id: int,
    channel_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Установить аватар канала"""
    from fastapi import UploadFile, File
    import tempfile
    import aiofiles

    acc = await _get_account(db, account_id, current_user.id)
    client = await _get_client(acc, db)

    # Сохраняем во временный файл
    suffix = "." + (file.filename.split(".")[-1] if "." in file.filename else "jpg")
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = tmp.name
        content = await file.read()
        tmp.write(content)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(status_code=400, detail="Сессия не активна")

        from telethon.tl.functions.channels import EditPhotoRequest
        from telethon.tl.types import InputChatUploadedPhoto

        file_handle = await client.upload_file(tmp_path)
        channel = await client.get_entity(channel_id)
        await client(EditPhotoRequest(
            channel=channel,
            photo=InputChatUploadedPhoto(file=file_handle),
        ))

        await client.disconnect()
        os.unlink(tmp_path)
        return {"success": True}

    except Exception as e:
        try: await client.disconnect()
        except: pass
        try: os.unlink(tmp_path)
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


class EditChannelInfoRequest(BaseModel):
    account_id: int
    channel_id: int
    title: Optional[str] = None
    description: Optional[str] = None


@router.post("/edit-info")
async def edit_channel_info(
    body: EditChannelInfoRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Изменить название и/или описание канала."""
    acc = await _get_account(db, body.account_id, current_user.id)
    client = await _get_client(acc, db)

    title = (body.title or "").strip()
    description = body.description if body.description is not None else None
    if not title and description is None:
        raise HTTPException(status_code=400, detail="Нечего изменять")

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(status_code=400, detail="Сессия не активна")

        from telethon.tl.functions.channels import EditTitleRequest
        from telethon.tl.functions.messages import EditChatAboutRequest

        entity = await client.get_entity(body.channel_id)

        title_ok = False
        if title and title != getattr(entity, "title", ""):
            await client(EditTitleRequest(channel=entity, title=title))
            title_ok = True

        about_ok = False
        if description is not None:
            await client(EditChatAboutRequest(peer=entity, about=description))
            about_ok = True

        # Обновим кэшированную запись в acc.channels
        channels = acc.channels or []
        for c in channels:
            if c.get("id") == body.channel_id:
                if title_ok: c["title"] = title
                if about_ok: c["description"] = description
                break
        acc.channels = channels
        await db.flush()

        await client.disconnect()
        return {"success": True, "title_updated": title_ok, "description_updated": about_ok}

    except HTTPException: raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        logger.error(f"edit-info error: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")


@router.post("/post")
async def post_to_channel(
    account_id: int = Form(...),
    channel_id: int = Form(...),
    text: str = Form(""),
    photo: UploadFile = File(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Опубликовать пост (текст + опционально фото) в существующий канал аккаунта."""
    import tempfile

    if not text.strip() and not (photo and photo.filename):
        raise HTTPException(status_code=400, detail="Пустой пост (нет ни текста, ни фото)")

    acc = await _get_account(db, account_id, current_user.id)
    client = await _get_client(acc, db)

    photo_path = None
    if photo and photo.filename:
        suffix = "." + (photo.filename.split(".")[-1] if "." in photo.filename else "jpg")
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await photo.read())
            photo_path = tmp.name

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(status_code=400, detail="Сессия не активна")

        entity = await client.get_entity(channel_id)

        if photo_path:
            msg = await client.send_file(entity=entity, file=photo_path, caption=text or "")
        else:
            msg = await client.send_message(entity=entity, message=text)

        await client.disconnect()
        return {"success": True, "message_id": getattr(msg, "id", None)}

    except HTTPException: raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        logger.error(f"post-to-channel error: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")
    finally:
        if photo_path:
            try: os.unlink(photo_path)
            except: pass


@router.post("/batch-create")
async def batch_create_channels(
    body: BatchCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Создать каналы для нескольких аккаунтов через Celery"""
    result = await db.execute(
        select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(
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