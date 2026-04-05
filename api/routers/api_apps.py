"""
GramGPT API — routers/api_apps.py
Управление API-приложениями Telegram (мульти-API система).

ВАЖНО: Нельзя перемещать аккаунты между API-ключами.
Сессия привязана к api_id+api_hash навсегда.
Распределение происходит ТОЛЬКО при импорте нового аккаунта.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.api_app import ApiApp
from models.account import TelegramAccount
from schemas.api_app import ApiAppCreate, ApiAppUpdate, ApiAppOut
from services import api_apps as app_svc

router = APIRouter(prefix="/api-apps", tags=["api-apps"])


# ══════════════════════════════════════════════════════════════
# Фиксированные пути ВЫШЕ чем /{app_id}
# ══════════════════════════════════════════════════════════════


# ── STATS ────────────────────────────────────────────────────

@router.get("/stats/overview")
async def api_apps_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Общая статистика по загрузке API-приложений."""
    return await app_svc.get_stats(db, current_user.id)


# ── LIST ─────────────────────────────────────────────────────

@router.get("")
async def list_api_apps(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Все API-приложения пользователя с подсчётом аккаунтов."""
    apps = await app_svc.get_all_apps(db, current_user.id)
    result = []
    for app in apps:
        result.append({
            "id": app.id,
            "api_id": app.api_id,
            "api_hash": app.api_hash,
            "title": app.title,
            "max_accounts": app.max_accounts,
            "is_active": app.is_active,
            "notes": app.notes,
            "accounts_count": app._accounts_count,
            "created_at": app.created_at,
            "updated_at": app.updated_at,
        })
    return result


# ── CREATE ───────────────────────────────────────────────────

@router.post("")
async def create_api_app(
    data: ApiAppCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Добавить новое API-приложение (api_id + api_hash с my.telegram.org)."""
    existing = await db.execute(
        select(ApiApp).where(
            ApiApp.user_id == current_user.id,
            ApiApp.api_id == data.api_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"API app с api_id={data.api_id} уже существует")

    app = ApiApp(
        user_id=current_user.id,
        api_id=data.api_id,
        api_hash=data.api_hash.strip(),
        title=data.title.strip() or f"App #{data.api_id}",
        max_accounts=data.max_accounts,
        notes=data.notes,
    )
    db.add(app)
    await db.flush()

    return {
        "id": app.id,
        "api_id": app.api_id,
        "title": app.title,
        "max_accounts": app.max_accounts,
        "message": "API-приложение добавлено. Новые аккаунты будут автоматически назначаться на этот ключ.",
    }


# ── GET ONE ──────────────────────────────────────────────────

@router.get("/{app_id}")
async def get_api_app(
    app_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    app = await app_svc.get_app_by_id(db, app_id, current_user.id)
    if not app:
        raise HTTPException(status_code=404, detail="API-приложение не найдено")

    acc_r = await db.execute(
        select(TelegramAccount)
        .where(TelegramAccount.api_app_id == app.id)
        .order_by(TelegramAccount.added_at.desc())
    )
    accounts = acc_r.scalars().all()

    return {
        "id": app.id,
        "api_id": app.api_id,
        "api_hash": app.api_hash,
        "title": app.title,
        "max_accounts": app.max_accounts,
        "is_active": app.is_active,
        "notes": app.notes,
        "accounts_count": app._accounts_count,
        "created_at": app.created_at,
        "updated_at": app.updated_at,
        "accounts": [
            {"id": a.id, "phone": a.phone, "username": a.username,
             "status": a.status.value, "first_name": a.first_name}
            for a in accounts
        ],
    }


# ── UPDATE ───────────────────────────────────────────────────

@router.patch("/{app_id}")
async def update_api_app(
    app_id: int,
    data: ApiAppUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    app = await app_svc.get_app_by_id(db, app_id, current_user.id)
    if not app:
        raise HTTPException(status_code=404, detail="API-приложение не найдено")

    if data.title is not None:
        app.title = data.title
    if data.max_accounts is not None:
        app.max_accounts = data.max_accounts
    if data.is_active is not None:
        app.is_active = data.is_active
    if data.notes is not None:
        app.notes = data.notes

    await db.flush()
    return {"id": app.id, "message": "Обновлено"}


# ── DELETE ───────────────────────────────────────────────────

@router.delete("/{app_id}")
async def delete_api_app(
    app_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    app = await app_svc.get_app_by_id(db, app_id, current_user.id)
    if not app:
        raise HTTPException(status_code=404, detail="API-приложение не найдено")

    # Проверяем — если есть аккаунты, удалять НЕЛЬЗЯ
    check = await app_svc.check_can_delete(db, app.id, current_user.id)
    if not check["can_delete"]:
        raise HTTPException(
            status_code=400,
            detail=check["message"]
        )

    await db.delete(app)
    await db.flush()

    return {"message": "API-приложение удалено"}