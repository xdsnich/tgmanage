"""
GramGPT API — services/api_apps.py
Логика управления API-приложениями и авто-распределения аккаунтов.

ВАЖНО: API-ключ назначается аккаунту ОДИН РАЗ при импорте/авторизации.
Сессия Telethon привязана к api_id+api_hash — если поменять,
Telegram убьёт сессию и аккаунт отлетит.
Поэтому перемещение между ключами ЗАПРЕЩЕНО для авторизованных аккаунтов.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional

from models.api_app import ApiApp
from models.account import TelegramAccount


async def get_all_apps(db: AsyncSession, user_id: int) -> list[ApiApp]:
    """Все API-приложения пользователя с подсчётом аккаунтов."""
    result = await db.execute(
        select(ApiApp)
        .where(ApiApp.user_id == user_id)
        .order_by(ApiApp.created_at)
    )
    apps = result.scalars().all()

    for app in apps:
        count_r = await db.execute(
            select(func.count(TelegramAccount.id))
            .where(TelegramAccount.api_app_id == app.id)
        )
        app._accounts_count = count_r.scalar() or 0

    return apps


async def get_app_by_id(db: AsyncSession, app_id: int, user_id: int) -> Optional[ApiApp]:
    """Одно приложение по ID."""
    result = await db.execute(
        select(ApiApp).where(ApiApp.id == app_id, ApiApp.user_id == user_id)
    )
    app = result.scalar_one_or_none()
    if app:
        count_r = await db.execute(
            select(func.count(TelegramAccount.id))
            .where(TelegramAccount.api_app_id == app.id)
        )
        app._accounts_count = count_r.scalar() or 0
    return app


async def pick_best_app(db: AsyncSession, user_id: int) -> Optional[ApiApp]:
    """
    Выбирает API-приложение с наименьшей загрузкой.
    Вызывается ТОЛЬКО при импорте НОВОГО аккаунта.
    Возвращает None → fallback на глобальный API_ID/API_HASH.
    """
    result = await db.execute(
        select(ApiApp).where(
            ApiApp.user_id == user_id,
            ApiApp.is_active == True,
        )
    )
    apps = result.scalars().all()

    if not apps:
        return None

    best_app = None
    best_ratio = 2.0

    for app in apps:
        count_r = await db.execute(
            select(func.count(TelegramAccount.id))
            .where(TelegramAccount.api_app_id == app.id)
        )
        count = count_r.scalar() or 0

        if count >= app.max_accounts:
            continue

        ratio = count / app.max_accounts
        if ratio < best_ratio:
            best_ratio = ratio
            best_app = app

    return best_app


async def get_api_credentials(db: AsyncSession, account: TelegramAccount) -> tuple[int, str]:
    """
    Возвращает (api_id, api_hash) для конкретного аккаунта.
    Приоритет: привязанный API-app → глобальный config.
    """
    if account.api_app_id:
        result = await db.execute(
            select(ApiApp).where(ApiApp.id == account.api_app_id)
        )
        app = result.scalar_one_or_none()
        if app and app.is_active:
            return app.api_id, app.api_hash

    # Fallback — глобальные ключи
    import importlib.util
    import os
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    config_path = os.path.join(root_dir, "config.py")
    spec = importlib.util.spec_from_file_location("cli_config", config_path)
    cli_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli_config)
    return cli_config.API_ID, cli_config.API_HASH


async def check_can_delete(db: AsyncSession, app_id: int, user_id: int) -> dict:
    """
    Проверяет можно ли удалить API-приложение.
    Если на нём есть аккаунты — НЕЛЬЗЯ (сессии сломаются).
    """
    count_r = await db.execute(
        select(func.count(TelegramAccount.id))
        .where(
            TelegramAccount.api_app_id == app_id,
            TelegramAccount.user_id == user_id,
        )
    )
    count = count_r.scalar() or 0

    return {
        "can_delete": count == 0,
        "accounts_count": count,
        "message": f"На этом ключе {count} аккаунтов. Удаление невозможно — сессии сломаются."
                   if count > 0 else "Можно удалить"
    }


async def get_stats(db: AsyncSession, user_id: int) -> dict:
    """Статистика по API-приложениям."""
    apps = await get_all_apps(db, user_id)

    total_capacity = sum(a.max_accounts for a in apps) if apps else 0
    total_used = 0

    apps_data = []
    for app in apps:
        count = app._accounts_count
        total_used += count
        apps_data.append({
            "id": app.id,
            "title": app.title or f"App #{app.api_id}",
            "api_id": app.api_id,
            "used": count,
            "max": app.max_accounts,
            "percent": round(count / app.max_accounts * 100, 1) if app.max_accounts else 0,
            "is_active": app.is_active,
        })

    # Аккаунты на глобальном ключе (без привязки)
    unassigned_r = await db.execute(
        select(func.count(TelegramAccount.id))
        .where(
            TelegramAccount.user_id == user_id,
            TelegramAccount.api_app_id == None,
        )
    )
    unassigned = unassigned_r.scalar() or 0

    return {
        "total_apps": len(apps),
        "total_capacity": total_capacity,
        "total_used": total_used,
        "on_global_key": unassigned,
        "apps": apps_data,
    }