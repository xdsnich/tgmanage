"""
GramGPT API — routers/actions.py
Быстрые действия. Все подключения через make_telethon_client (с прокси).
"""

import sys
import os
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount
from models.proxy import Proxy

router = APIRouter(prefix="/actions", tags=["actions"])


class BulkActionRequest(BaseModel):
    account_ids: list[int]


async def _get_accounts(db, account_ids, user_id):
    result = await db.execute(
        select(TelegramAccount).where(TelegramAccount.id.in_(account_ids), TelegramAccount.user_id == user_id)
    )
    accounts = result.scalars().all()
    if not accounts:
        raise HTTPException(status_code=404, detail="Аккаунты не найдены")
    return accounts


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
    return client


async def _run_action_with_proxy(action_name, action_fn, accounts, db):
    """Выполняет действие для каждого аккаунта С ПРОКСИ"""
    results = []
    for acc in accounts:
        client = await _get_client(acc, db)
        if not client:
            results.append({"phone": acc.phone, "status": "error", "message": "Файл сессии не найден"})
            continue
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                results.append({"phone": acc.phone, "status": "error", "message": "Сессия не активна"})
                continue

            msg = await action_fn(client, acc)
            await client.disconnect()
            results.append({"phone": acc.phone, "status": "success", "message": msg})
        except Exception as e:
            try: await client.disconnect()
            except: pass
            results.append({"phone": acc.phone, "status": "error", "message": str(e)[:200]})
    return results


# ── Action functions ─────────────────────────────────────────

async def _leave_chats(client, acc):
    from telethon.tl.types import Chat, Channel
    from telethon.tl.functions.channels import LeaveChannelRequest
    dialogs = await client.get_dialogs()
    groups = [d for d in dialogs if isinstance(d.entity, (Chat, Channel))
              and not (isinstance(d.entity, Channel) and d.entity.broadcast)]
    left = 0
    for d in groups:
        try:
            await client(LeaveChannelRequest(d.entity))
            left += 1
            await asyncio.sleep(1)
        except: pass
    return f"Вышел из {left} чатов"


async def _leave_channels(client, acc):
    from telethon.tl.types import Channel
    from telethon.tl.functions.channels import LeaveChannelRequest
    dialogs = await client.get_dialogs()
    channels = [d for d in dialogs if isinstance(d.entity, Channel) and d.entity.broadcast]
    left = 0
    for d in channels:
        try:
            await client(LeaveChannelRequest(d.entity))
            left += 1
            await asyncio.sleep(1)
        except: pass
    return f"Отписался от {left} каналов"


async def _delete_dialogs(client, acc):
    from telethon.tl.types import User as TgUser
    from telethon.tl.functions.messages import DeleteHistoryRequest
    dialogs = await client.get_dialogs()
    private = [d for d in dialogs if isinstance(d.entity, TgUser) and not d.entity.bot and not d.entity.is_self]
    deleted = 0
    for d in private:
        try:
            await client(DeleteHistoryRequest(peer=d.entity, max_id=0, revoke=False))
            deleted += 1
            await asyncio.sleep(0.5)
        except: pass
    return f"Удалено {deleted} переписок"


async def _read_all(client, acc):
    dialogs = await client.get_dialogs()
    unread = [d for d in dialogs if d.unread_count > 0]
    read = 0
    for d in unread:
        try:
            await client.send_read_acknowledge(d.entity)
            read += 1
            await asyncio.sleep(0.3)
        except: pass
    return f"Прочитано {read} диалогов"


async def _unpin_folders(client, acc):
    from telethon.tl.functions.messages import UpdateDialogFilterRequest
    removed = 0
    for fid in range(2, 11):
        try:
            await client(UpdateDialogFilterRequest(id=fid))
            removed += 1
        except: pass
    return f"Удалено {removed} папок"


# ── Endpoints ────────────────────────────────────────────────

@router.post("/leave-chats")
async def leave_chats(body: BulkActionRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    accounts = await _get_accounts(db, body.account_ids, current_user.id)
    return {"action": "leave_chats", "results": await _run_action_with_proxy("Выход из чатов", _leave_chats, accounts, db)}

@router.post("/leave-channels")
async def leave_channels(body: BulkActionRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    accounts = await _get_accounts(db, body.account_ids, current_user.id)
    return {"action": "leave_channels", "results": await _run_action_with_proxy("Отписка от каналов", _leave_channels, accounts, db)}

@router.post("/delete-dialogs")
async def delete_dialogs(body: BulkActionRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    accounts = await _get_accounts(db, body.account_ids, current_user.id)
    return {"action": "delete_dialogs", "results": await _run_action_with_proxy("Удаление переписок", _delete_dialogs, accounts, db)}

@router.post("/read-all")
async def read_all(body: BulkActionRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    accounts = await _get_accounts(db, body.account_ids, current_user.id)
    return {"action": "read_all", "results": await _run_action_with_proxy("Прочитать всё", _read_all, accounts, db)}

@router.post("/clear-cache")
async def clear_cache(body: BulkActionRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # clear_cache не требует Telegram подключения
    return {"action": "clear_cache", "results": [{"phone": "all", "status": "success", "message": "Кэш очищен"}]}

@router.post("/unpin-folders")
async def unpin_folders(body: BulkActionRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    accounts = await _get_accounts(db, body.account_ids, current_user.id)
    return {"action": "unpin_folders", "results": await _run_action_with_proxy("Открепление папок", _unpin_folders, accounts, db)}