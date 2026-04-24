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
from schemas.api_app import ApiAppCreate, ApiAppUpdate, ApiAppOut, KNOWN_PUBLIC_APIS, detect_platform_by_api_id
from services import api_apps as app_svc

router = APIRouter(prefix="/api-apps", tags=["api-apps"])


# ══════════════════════════════════════════════════════════════
# Фиксированные пути ВЫШЕ чем /{app_id}
# ══════════════════════════════════════════════════════════════


# ── KNOWN PUBLIC APIS ────────────────────────────────────────

@router.get("/known-public")
async def get_known_public_apis(
    current_user: User = Depends(get_current_user),
):
    """
    Справочник известных публичных api_id от официальных клиентов Telegram.
    Используется фронтом для кнопок быстрого добавления.
    """
    return [
        {
            "api_id": api_id,
            "api_hash": info["api_hash"],
            "title": info["title"],
            "platform": info["platform"],
            "description": info["description"],
        }
        for api_id, info in KNOWN_PUBLIC_APIS.items()
    ]


# ── DETECT PLATFORM ──────────────────────────────────────────

@router.get("/detect-platform/{api_id}")
async def detect_platform(
    api_id: int,
    current_user: User = Depends(get_current_user),
):
    """Автоопределение платформы по api_id."""
    platform = detect_platform_by_api_id(api_id)
    is_known = api_id in KNOWN_PUBLIC_APIS
    return {
        "api_id": api_id,
        "platform": platform,
        "is_known_public": is_known,
        "suggested_title": KNOWN_PUBLIC_APIS.get(api_id, {}).get("title", ""),
    }


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
            "platform": getattr(app, 'platform', 'android') or 'android',
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
    """Добавить новое API-приложение. Platform автоопределяется если не передан."""
    existing = await db.execute(
        select(ApiApp).where(
            ApiApp.user_id == current_user.id,
            ApiApp.api_id == data.api_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"API app с api_id={data.api_id} уже существует")

    # Платформа: если передан → берём; если нет → автоопределение по api_id
    platform = data.platform or detect_platform_by_api_id(data.api_id)

    # Title: если пустой и это известный публичный api_id → берём его название
    title = data.title.strip()
    if not title:
        if data.api_id in KNOWN_PUBLIC_APIS:
            title = KNOWN_PUBLIC_APIS[data.api_id]["title"]
        else:
            title = f"App #{data.api_id}"

    app = ApiApp(
        user_id=current_user.id,
        api_id=data.api_id,
        api_hash=data.api_hash.strip(),
        title=title,
        platform=platform,
        max_accounts=data.max_accounts,
        notes=data.notes,
    )
    db.add(app)
    await db.flush()

    return {
        "id": app.id,
        "api_id": app.api_id,
        "title": app.title,
        "platform": app.platform,
        "max_accounts": app.max_accounts,
        "message": f"API-приложение '{app.title}' ({app.platform}) добавлено.",
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
    return {
        "id": app.id,
        "api_id": app.api_id,
        "api_hash": app.api_hash,
        "title": app.title,
        "platform": getattr(app, 'platform', 'android') or 'android',
        "max_accounts": app.max_accounts,
        "is_active": app.is_active,
        "notes": app.notes,
        "accounts_count": app._accounts_count,
        "created_at": app.created_at,
        "updated_at": app.updated_at,
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
        app.title = data.title.strip()
    if data.platform is not None:
        app.platform = data.platform
    if data.max_accounts is not None:
        if data.max_accounts < 1 or data.max_accounts > 500:
            raise HTTPException(status_code=400, detail="max_accounts должно быть от 1 до 500")
        app.max_accounts = data.max_accounts
    if data.is_active is not None:
        app.is_active = data.is_active
    if data.notes is not None:
        app.notes = data.notes

    await db.flush()
    return {"message": "API-приложение обновлено", "id": app.id}


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

    # Проверяем что нет аккаунтов — иначе они останутся без API
    count_r = await db.execute(
        select(func.count(TelegramAccount.id)).where(TelegramAccount.api_app_id == app_id)
    )
    count = count_r.scalar() or 0
    if count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Нельзя удалить: {count} аккаунтов привязаны к этому API. Сначала удалите аккаунты.",
        )

    await db.delete(app)
    await db.flush()
    return {"message": "API-приложение удалено"}