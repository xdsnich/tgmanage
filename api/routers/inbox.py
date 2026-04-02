"""
GramGPT API — routers/inbox.py
Входящие сообщения и ИИ-диалоги.
Все подключения через make_telethon_client (с прокси).
"""

import sys
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount
from models.proxy import Proxy
from models.ai_dialog import AIDialog

router = APIRouter(prefix="/inbox", tags=["inbox"])


class SendMessageRequest(BaseModel):
    text: str

class AIConfigRequest(BaseModel):
    system_prompt: str = ""
    is_active: bool = False
    llm_provider: str = "claude"


async def _get_account(db, account_id, user_id):
    result = await db.execute(
        select(TelegramAccount).where(TelegramAccount.id == account_id, TelegramAccount.user_id == user_id)
    )
    acc = result.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    return acc


async def _get_client(acc, db):
    """Создаёт TelegramClient С ПРОКСИ (явный запрос, без lazy-load)"""
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


@router.get("/accounts/{account_id}/dialogs")
async def get_dialogs(account_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    acc = await _get_account(db, account_id, current_user.id)
    client = await _get_client(acc, db)

    dialogs_list = []
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return []

        dialogs = await client.get_dialogs(limit=50)
        from telethon.tl.types import User as TgUser

        for d in dialogs:
            if not isinstance(d.entity, TgUser) or d.entity.bot:
                continue

            ai_result = await db.execute(
                select(AIDialog).where(AIDialog.account_id == account_id, AIDialog.contact_id == d.entity.id)
            )
            ai_dialog = ai_result.scalar_one_or_none()

            name = f"{d.entity.first_name or ''} {d.entity.last_name or ''}".strip()
            if not name:
                name = d.entity.username or str(d.entity.id)

            dialogs_list.append({
                "id": d.entity.id, "contact_id": d.entity.id,
                "name": name, "contact_name": name,
                "username": d.entity.username or "",
                "last_message": (d.message.text or "")[:100] if d.message else "",
                "preview": (d.message.text or "")[:80] if d.message else "",
                "unread_count": d.unread_count,
                "time": d.message.date.isoformat() if d.message and d.message.date else None,
                "is_ai_active": ai_dialog.is_active if ai_dialog else False,
            })

        await client.disconnect()
    except HTTPException: raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")

    return dialogs_list


@router.get("/accounts/{account_id}/dialogs/{contact_id}/messages")
async def get_messages(account_id: int, contact_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    acc = await _get_account(db, account_id, current_user.id)
    client = await _get_client(acc, db)

    messages_list = []
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return []

        messages = await client.get_messages(contact_id, limit=50)
        for m in messages:
            if not m.text: continue
            messages_list.append({
                "id": m.id, "text": m.text, "message": m.text,
                "from": "me" if m.out else "them",
                "is_outgoing": m.out, "is_ai": False,
                "time": m.date.isoformat() if m.date else None,
            })
        messages_list.reverse()
        await client.disconnect()
    except Exception as e:
        try: await client.disconnect()
        except: pass
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")

    return messages_list


@router.post("/accounts/{account_id}/dialogs/{contact_id}/send")
async def send_message(account_id: int, contact_id: int, body: SendMessageRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    acc = await _get_account(db, account_id, current_user.id)
    client = await _get_client(acc, db)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(status_code=400, detail="Сессия не активна")

        await client.send_message(contact_id, body.text)
        await client.disconnect()
        return {"success": True, "message": "Сообщение отправлено"}

    except HTTPException: raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")


@router.get("/accounts/{account_id}/dialogs/{contact_id}/ai-config")
async def get_ai_config(account_id: int, contact_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_account(db, account_id, current_user.id)
    result = await db.execute(select(AIDialog).where(AIDialog.account_id == account_id, AIDialog.contact_id == contact_id))
    ai = result.scalar_one_or_none()
    if not ai:
        return {"system_prompt": "", "is_active": False, "llm_provider": "claude"}
    return {"system_prompt": ai.system_prompt, "is_active": ai.is_active, "llm_provider": getattr(ai, 'llm_provider', 'claude') or 'claude'}


@router.post("/accounts/{account_id}/dialogs/{contact_id}/ai-config")
async def set_ai_config(account_id: int, contact_id: int, body: AIConfigRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_account(db, account_id, current_user.id)
    result = await db.execute(select(AIDialog).where(AIDialog.account_id == account_id, AIDialog.contact_id == contact_id))
    ai = result.scalar_one_or_none()

    if ai:
        ai.system_prompt = body.system_prompt
        ai.is_active = body.is_active
        ai.llm_provider = body.llm_provider
        ai.updated_at = datetime.utcnow()
    else:
        ai = AIDialog(account_id=account_id, contact_id=contact_id, system_prompt=body.system_prompt, is_active=body.is_active, llm_provider=body.llm_provider)
        db.add(ai)

    await db.flush()
    return {"success": True, "system_prompt": ai.system_prompt, "is_active": ai.is_active, "llm_provider": ai.llm_provider}