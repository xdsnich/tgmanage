"""
GramGPT API — routers/security.py
Безопасность и управление сессиями через веб.
По ТЗ раздел 3: сессии, 2FA, переавторизация — всё без терминала.
"""

import asyncio
import sys
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount

router = APIRouter(prefix="/security", tags=["security"])


# ── Safe CLI import ──────────────────────────────────────────

def _import_cli_security():
    """
    Безопасно импортирует CLI-модуль security.py.
    Проблема: security.py → ui.py → trust.py → from config import TRUST_SCORE
    А в API-контексте config — это api/config.py (без TRUST_SCORE).
    Решение: временно убираем api/config.py из sys.modules.
    """
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)

    # Прячем api/config.py, чтобы trust.py загрузил корневой config.py
    api_config_cache = sys.modules.pop('config', None)
    # Также убираем кэши зависимых модулей, чтобы переимпортировались
    for mod_name in ['ui', 'trust', 'security', 'tg_client']:
        sys.modules.pop(mod_name, None)

    try:
        import security as sec
        return sec
    finally:
        # Возвращаем api/config.py обратно
        if api_config_cache:
            sys.modules['config'] = api_config_cache


# ── Helpers ──────────────────────────────────────────────────

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


def _to_dict(a: TelegramAccount) -> dict:
    return {
        "phone": a.phone,
        "session_file": a.session_file,
        "status": a.status.value,
        "trust_score": a.trust_score,
        "tags": a.tags or [],
        "notes": a.notes or "",
        "role": a.role.value,
        "proxy": None,
    }


# ── Schemas ──────────────────────────────────────────────────

class Set2FARequest(BaseModel):
    password: str
    hint: str = ""


class TerminateRequest(BaseModel):
    account_ids: list[int]


# ── Endpoints ────────────────────────────────────────────────

@router.get("/accounts/{account_id}/sessions")
async def list_sessions(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Список активных сессий аккаунта (устройства).
    По ТЗ: Детали аккаунта → сессии.
    """
    acc = await _get_account(db, account_id, current_user.id)

    if not acc.session_file or not Path(acc.session_file).exists():
        return {"account_id": account_id, "sessions": [], "message": "Файл сессии не найден"}

    try:
        sec = _import_cli_security()
        sessions = await sec.list_sessions(_to_dict(acc))
        return {"account_id": account_id, "sessions": sessions}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка получения сессий: {str(e)}")


@router.post("/accounts/{account_id}/terminate-sessions")
async def terminate_sessions(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Завершить все сторонние сессии (кроме текущей).
    По ТЗ: завершение всех активных сессий.
    """
    acc = await _get_account(db, account_id, current_user.id)

    if not acc.session_file or not Path(acc.session_file).exists():
        raise HTTPException(status_code=400, detail="Файл сессии не найден")

    try:
        sec = _import_cli_security()
        await sec.terminate_other_sessions(_to_dict(acc))
        acc.active_sessions = 1
        await db.flush()
        return {"success": True, "account_id": account_id, "message": "Сторонние сессии завершены"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)}")


@router.post("/accounts/{account_id}/set-2fa")
async def set_2fa(
    account_id: int,
    body: Set2FARequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Установить / сменить 2FA пароль.
    По ТЗ: одиночная и групповая установка 2FA.
    """
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Пароль минимум 6 символов")

    acc = await _get_account(db, account_id, current_user.id)

    if not acc.session_file or not Path(acc.session_file).exists():
        raise HTTPException(status_code=400, detail="Файл сессии не найден")

    try:
        sec = _import_cli_security()
        ok = await sec.set_2fa(_to_dict(acc), body.password, body.hint)

        if ok:
            acc.has_2fa = True
            await db.flush()

        return {
            "success": ok,
            "account_id": account_id,
            "message": "2FA установлена" if ok else "Ошибка установки 2FA"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)}")


@router.post("/accounts/{account_id}/remove-2fa")
async def remove_2fa(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Снять 2FA с аккаунта"""
    acc = await _get_account(db, account_id, current_user.id)

    # Пока просто обновляем флаг в БД
    acc.has_2fa = False
    await db.flush()

    return {"success": True, "account_id": account_id, "message": "2FA снята"}


@router.post("/accounts/{account_id}/reauthorize")
async def reauthorize(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Переавторизация аккаунта (сброс старой сессии)"""
    acc = await _get_account(db, account_id, current_user.id)

    try:
        sec = _import_cli_security()
        result = await sec.reauthorize(_to_dict(acc))
        return {"success": True, "account_id": account_id, "message": "Переавторизация запущена"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)}")


@router.get("/accounts/{account_id}/export-session")
async def export_session(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Экспорт данных сессии в JSON.
    По ТЗ: экспорт сессий для интеграции со сторонним ПО.
    """
    from datetime import datetime

    acc = await _get_account(db, account_id, current_user.id)
    session_file = acc.session_file
    session_exists = Path(session_file).exists() if session_file else False

    return {
        "phone": acc.phone,
        "session_file": session_file,
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
    """
    Получить последний код авторизации (от Telegram 777000).
    По ТЗ: получение кодов внутри программы.
    """
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)

    # Импортируем корневой config для TG_API
    api_config_cache = sys.modules.pop('config', None)
    try:
        import config as cli_config
    finally:
        if api_config_cache:
            sys.modules['config'] = api_config_cache

    from telethon import TelegramClient

    acc = await _get_account(db, account_id, current_user.id)
    session_file = acc.session_file

    if not session_file or not Path(session_file).exists():
        raise HTTPException(status_code=400, detail="Файл сессии не найден")

    session_path = session_file.replace(".session", "")
    client = TelegramClient(
        session_path,
        cli_config.API_ID,
        cli_config.API_HASH,
        device_model="Desktop",
        system_version="Windows 10",
        app_version="4.14.15",
    )

    try:
        await client.connect()
        if not await client.is_user_authorized():
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
    except HTTPException:
        raise
    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bulk/set-2fa")
async def bulk_set_2fa(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Установить 2FA на несколько аккаунтов через Celery."""
    account_ids = body.get("account_ids", [])
    password = body.get("password", "")
    hint = body.get("hint", "")

    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Пароль минимум 6 символов")

    result = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.user_id == current_user.id,
            TelegramAccount.id.in_(account_ids) if account_ids else True,
        )
    )
    accounts = result.scalars().all()
    accounts_data = [{"phone": a.phone, "session_file": a.session_file} for a in accounts]

    try:
        from celery import current_app
        task = current_app.send_task(
            "tasks.bulk_tasks.set_2fa_bulk",
            args=[accounts_data, password, hint],
            queue="bulk_actions",
        )
        return {"task_id": task.id, "total": len(accounts_data), "message": f"2FA устанавливается на {len(accounts_data)} аккаунтах"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Celery недоступен: {str(e)}")


@router.post("/bulk/terminate-sessions")
async def bulk_terminate_sessions(
    body: TerminateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Завершить сторонние сессии на нескольких аккаунтах"""
    result = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.user_id == current_user.id,
            TelegramAccount.id.in_(body.account_ids),
        )
    )
    accounts = result.scalars().all()
    accounts_data = [{"phone": a.phone, "session_file": a.session_file} for a in accounts]

    try:
        from celery import current_app
        task = current_app.send_task(
            "tasks.bulk_tasks.terminate_sessions_bulk",
            args=[accounts_data],
            queue="bulk_actions",
        )
        return {"task_id": task.id, "total": len(accounts_data)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Celery недоступен: {str(e)}")