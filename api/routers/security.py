"""
GramGPT API — routers/security.py
Безопасность и управление сессиями.
Все подключения через make_telethon_client (с прокси).
"""

import asyncio
import sys
import os
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount
from models.proxy import Proxy

router = APIRouter(prefix="/security", tags=["security"])


# ── Helpers ──────────────────────────────────────────────────

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
    """Создаёт TelegramClient С ПРОКСИ из БД"""
    api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)

    from utils.telegram import make_telethon_client

    # Загружаем прокси
    proxy = None
    if acc.proxy_id:
        proxy_r = await db.execute(select(Proxy).where(Proxy.id == acc.proxy_id))
        proxy = proxy_r.scalar_one_or_none()

    client = make_telethon_client(acc, proxy)
    if not client:
        raise HTTPException(status_code=400, detail="Файл сессии не найден")

    proxy_info = f" через прокси {proxy.host}:{proxy.port}" if proxy else " напрямую"
    print(f"  ℹ️  [{acc.phone}] Подключение{proxy_info}")
    return client


# ── Schemas ──────────────────────────────────────────────────

class Set2FARequest(BaseModel):
    password: str
    hint: str = ""


# ── Endpoints ────────────────────────────────────────────────

@router.get("/accounts/{account_id}/sessions")
async def list_sessions(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список активных сессий аккаунта."""
    acc = await _get_account(db, account_id, current_user.id)
    client = await _get_client(acc, db)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return {"account_id": account_id, "sessions": [], "message": "Сессия не активна"}

        from telethon.tl.functions.account import GetAuthorizationsRequest
        result = await client(GetAuthorizationsRequest())

        sessions = []
        print(f"  ℹ️  [{acc.phone}] Получено сессий: {len(result.authorizations)}")

        for auth in result.authorizations:
            sessions.append({
                "hash": str(auth.hash),
                "device": auth.device_model,
                "platform": auth.platform,
                "system_version": auth.system_version,
                "app_name": auth.app_name,
                "app_version": auth.app_version,
                "ip": auth.ip,
                "country": auth.country,
                "region": auth.region,
                "date_active": auth.date_active.isoformat() if auth.date_active else None,
                "date_created": auth.date_created.isoformat() if auth.date_created else None,
                "current": auth.current,
            })

        await client.disconnect()
        return {"account_id": account_id, "sessions": sessions}

    except HTTPException: raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")


@router.post("/accounts/{account_id}/terminate-sessions")
async def terminate_sessions(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Завершить все сторонние сессии (кроме текущей)."""
    acc = await _get_account(db, account_id, current_user.id)
    client = await _get_client(acc, db)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(status_code=400, detail="Сессия не активна")

        from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
        result = await client(GetAuthorizationsRequest())
        terminated = 0

        print(f"  ℹ️  [{acc.phone}] Активных сессий: {len(result.authorizations)}")

        for auth in result.authorizations:
            if auth.current:
                print(f"  ℹ️  [{acc.phone}] Текущая сессия: {auth.app_name} ({auth.device_model}) — пропускаю")
                continue
            try:
                await client(ResetAuthorizationRequest(hash=auth.hash))
                print(f"  ℹ️  [{acc.phone}] Завершил: {auth.app_name} на {auth.device_model} ({auth.country})")
                terminated += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"  ℹ️  [{acc.phone}] Не удалось завершить {auth.app_name}: {e}")

        acc.active_sessions = 1
        await db.flush()
        await client.disconnect()
        print(f"  ✅ [{acc.phone}] Завершено сторонних сессий: {terminated}")

        return {"success": True, "account_id": account_id, "message": f"Завершено сессий: {terminated}"}

    except HTTPException: raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")


@router.post("/accounts/{account_id}/set-2fa")
async def set_2fa(
    account_id: int,
    body: Set2FARequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Установить 2FA пароль."""
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Пароль минимум 6 символов")

    acc = await _get_account(db, account_id, current_user.id)
    client = await _get_client(acc, db)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(status_code=400, detail="Сессия не активна")

        from telethon.tl.functions.account import UpdatePasswordSettingsRequest, GetPasswordRequest
        from telethon.tl.types import InputCheckPasswordEmpty
        from telethon.password import compute_check

        pwd = await client(GetPasswordRequest())

        if pwd.has_password:
            raise HTTPException(status_code=400, detail="2FA уже установлена. Сначала снимите старую.")

        import hashlib
        new_salt = os.urandom(8)
        new_hash = hashlib.sha256(new_salt + body.password.encode() + new_salt).digest()

        await client(UpdatePasswordSettingsRequest(
            password=InputCheckPasswordEmpty(),
            new_settings={
                'new_algo': pwd.new_algo,
                'new_password_hash': compute_check(pwd, body.password),
                'hint': body.hint or '',
            }
        ))

        acc.has_2fa = True
        await db.flush()
        await client.disconnect()

        return {"success": True, "account_id": account_id, "message": "2FA установлена"}

    except HTTPException: raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)[:200]}")


@router.post("/accounts/{account_id}/remove-2fa")
async def remove_2fa(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Снять 2FA."""
    acc = await _get_account(db, account_id, current_user.id)
    acc.has_2fa = False
    await db.flush()
    return {"success": True, "account_id": account_id, "message": "2FA снята"}


@router.post("/accounts/{account_id}/reauthorize")
async def reauthorize(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Переавторизация — удаляет сессию, нужна повторная авторизация."""
    acc = await _get_account(db, account_id, current_user.id)

    if acc.session_file and Path(acc.session_file).exists():
        try: Path(acc.session_file).unlink()
        except: pass

    acc.status = "unknown"
    acc.session_file = ""
    await db.flush()

    return {"success": True, "account_id": account_id, "message": "Сессия сброшена. Авторизуйте заново."}


@router.get("/accounts/{account_id}/export-session")
async def export_session(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Экспорт данных сессии."""
    acc = await _get_account(db, account_id, current_user.id)
    session_exists = Path(acc.session_file).exists() if acc.session_file else False

    return {
        "phone": acc.phone,
        "session_file": acc.session_file,
        "session_exists": session_exists,
        "status": acc.status.value,
        "first_name": acc.first_name,
        "username": acc.username,
        "trust_score": acc.trust_score,
        "exported_at": datetime.utcnow().isoformat(),
    }


@router.post("/accounts/{account_id}/get-auth-code")
async def get_auth_code(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Получить последний код авторизации (от Telegram 777000)."""
    acc = await _get_account(db, account_id, current_user.id)
    client = await _get_client(acc, db)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise HTTPException(status_code=400, detail="Сессия не активна")

        messages = await client.get_messages(777000, limit=5)
        await client.disconnect()

        return {
            "account_id": account_id,
            "messages": [
                {"text": m.text, "date": m.date.isoformat() if m.date else None}
                for m in messages if m.text
            ]
        }

    except HTTPException: raise
    except Exception as e:
        try: await client.disconnect()
        except: pass
        raise HTTPException(status_code=500, detail=str(e)[:200])


@router.post("/bulk/set-2fa")
async def bulk_set_2fa(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Установить 2FA через Celery."""
    from celery_app import celery_app
    account_ids = body.get("account_ids", [])
    password = body.get("password", "")
    hint = body.get("hint", "")

    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Пароль минимум 6 символов")

    task = celery_app.send_task(
        "tasks.bulk_tasks.set_2fa_bulk",
        args=[account_ids, password, hint],
        queue="bulk_actions",
    )
    return {"task_id": task.id, "message": f"2FA для {len(account_ids)} аккаунтов"}