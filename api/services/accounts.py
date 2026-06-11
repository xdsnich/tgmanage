"""
GramGPT API — services/accounts.py
Бизнес-логика: аккаунты Telegram
"""

import os
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import joinedload
from fastapi import HTTPException, status

from models.account import TelegramAccount, AccountStatus
from models.user import User
from schemas.account import AccountUpdate

logger = logging.getLogger(__name__)


async def get_accounts(db: AsyncSession, user_id: int) -> list[TelegramAccount]:
    result = await db.execute(
        select(TelegramAccount)
        .options(joinedload(TelegramAccount.api_app), joinedload(TelegramAccount.proxy))
        .where(TelegramAccount.user_id == user_id)
        .order_by(TelegramAccount.added_at.desc())
    )
    accounts = result.scalars().all()
    # Прокидываем гео прокси в самые «плоские» поля, чтобы AccountOut
    # подобрал их без property-плясок (Pydantic from_attributes так умеет)
    for a in accounts:
        p = a.proxy
        a.proxy_country      = p.country if p else None
        a.proxy_country_code = p.country_code if p else None
        a.proxy_city         = p.city if p else None
        a.proxy_host         = f"{p.host}:{p.port}" if p else None
    return accounts


async def get_account(db: AsyncSession, account_id: int, user_id: int) -> TelegramAccount:
    result = await db.execute(
        select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(
            TelegramAccount.id == account_id,
            TelegramAccount.user_id == user_id
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    return account


async def get_account_by_phone(db: AsyncSession, phone: str, user_id: int) -> Optional[TelegramAccount]:
    result = await db.execute(
        select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(
            TelegramAccount.phone == phone,
            TelegramAccount.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def get_stats(db: AsyncSession, user_id: int) -> dict:
    """Статистика по аккаунтам"""
    result = await db.execute(
        select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(TelegramAccount.user_id == user_id)
    )
    accounts = result.scalars().all()

    total = len(accounts)
    active = sum(1 for a in accounts if a.status == AccountStatus.active)
    spamblock = sum(1 for a in accounts if a.status == AccountStatus.spamblock)
    avg_trust = round(sum(a.trust_score for a in accounts) / total) if total > 0 else 0

    return {
        "total": total,
        "active": active,
        "spamblock": spamblock,
        "frozen": sum(1 for a in accounts if a.status == AccountStatus.frozen),
        "error": sum(1 for a in accounts if a.status == AccountStatus.error),
        "avg_trust": avg_trust,
        "with_2fa": sum(1 for a in accounts if a.has_2fa),
        "with_photo": sum(1 for a in accounts if a.has_photo),
        "with_proxy": sum(1 for a in accounts if a.proxy_id),
    }


async def check_limit(db: AsyncSession, user: User) -> None:
    """Проверяет что юзер не превысил лимит аккаунтов по тарифу."""
    
    # ✅ НОВОЕ: суперюзер без лимитов
    if getattr(user, 'is_superuser', False):
        return
    
    # Существующая логика лимитов по тарифам
    plan_limits = {
        PlanEnum.starter: 10,
        PlanEnum.pro: 50,
        PlanEnum.enterprise: 500,
    }
    
    max_accounts = plan_limits.get(user.plan, 10)
    
    result = await db.execute(
        select(func.count(TelegramAccount.id)).where(TelegramAccount.user_id == user.id)
    )
    current_count = result.scalar() or 0
    
    if current_count >= max_accounts:
        raise HTTPException(
            status_code=403,
            detail=f"Достигнут лимит аккаунтов ({current_count}/{max_accounts}) для тарифа {user.plan.value}. "
                   f"Обнови тариф для увеличения лимита."
        )

async def create_account(db: AsyncSession, user: User, phone: str) -> TelegramAccount:
    await check_limit(db, user)

    existing = await get_account_by_phone(db, phone, user.id)
    if existing:
        raise HTTPException(status_code=400, detail=f"Аккаунт {phone} уже добавлен")

    account = TelegramAccount(
        user_id=user.id,
        phone=phone,
    )
    db.add(account)
    await db.flush()
    return account


async def update_account(db: AsyncSession, account: TelegramAccount,
                         data: AccountUpdate) -> TelegramAccount:
    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(account, key, value)
    account.updated_at = datetime.utcnow()
    await db.flush()
    return account


async def delete_account(db: AsyncSession, account: TelegramAccount):
    """
    Удаляет аккаунт из БД + удаляет .session файл с диска.
    Если session_file в БД пустой — всё равно пытается найти файл
    по номеру телефона (sessions/<phone>.session).
    """
    files_to_delete = []
 
    # 1) Файл из БД (основной путь)
    if account.session_file:
        files_to_delete.append(Path(account.session_file))
        files_to_delete.append(Path(str(account.session_file) + "-journal"))
 
    # 2) Файл по номеру телефона (fallback — если в БД пусто или путь невалидный)
    if account.phone:
        import os as _os
        sessions_dir = Path(_os.path.abspath(_os.path.join(
            _os.path.dirname(__file__), "..", "..", "sessions"
        )))
        phone_clean = account.phone.replace("+", "")
        fallback_session = sessions_dir / f"{phone_clean}.session"
        fallback_journal = sessions_dir / f"{phone_clean}.session-journal"
 
        # Добавляем если ещё не в списке
        if fallback_session not in files_to_delete:
            files_to_delete.append(fallback_session)
        if fallback_journal not in files_to_delete:
            files_to_delete.append(fallback_journal)
 
    # Удаляем все найденные файлы
    for path in files_to_delete:
        if path.exists():
            try:
                path.unlink()
                logger.info(f"Удалён файл: {path}")
            except Exception as e:
                logger.warning(f"Не удалось удалить {path}: {e}")
 
    # Удаляем запись из БД
    await db.delete(account)
    await db.flush()


async def sync_from_dict(db: AsyncSession, user: User, account_dict: dict) -> TelegramAccount:
    """
    Синхронизирует данные из CLI-формата (dict) в БД.
    Используется при импорте из JSON файлов.
    """
    phone = account_dict.get("phone", "")
    existing = await get_account_by_phone(db, phone, user.id)

    if existing:
        existing.tg_id = account_dict.get("id") or existing.tg_id
        existing.first_name = account_dict.get("first_name", existing.first_name)
        existing.last_name = account_dict.get("last_name", existing.last_name)
        existing.username = account_dict.get("username", existing.username)
        existing.bio = account_dict.get("bio", existing.bio)
        existing.has_photo = account_dict.get("has_photo", existing.has_photo)
        existing.has_2fa = account_dict.get("has_2fa", existing.has_2fa)
        existing.active_sessions = account_dict.get("active_sessions", existing.active_sessions)
        existing.session_file = account_dict.get("session_file", existing.session_file)
        existing.status = account_dict.get("status", existing.status.value)
        existing.trust_score = account_dict.get("trust_score", existing.trust_score)
        existing.device_fingerprint = account_dict.get("device_fingerprint") or existing.device_fingerprint
        existing.updated_at = datetime.utcnow()

        # Авто-распределение — если ещё не привязан к API-приложению
        if not existing.api_app_id:
            from services.api_apps import pick_best_app
            best_app = await pick_best_app(db, user.id)
            if best_app:
                existing.api_app_id = best_app.id

        await db.flush()
        return existing

    account = TelegramAccount(
        user_id=user.id,
        phone=phone,
        tg_id=account_dict.get("id"),
        first_name=account_dict.get("first_name", ""),
        last_name=account_dict.get("last_name", ""),
        username=account_dict.get("username", ""),
        bio=account_dict.get("bio", ""),
        has_photo=account_dict.get("has_photo", False),
        has_2fa=account_dict.get("has_2fa", False),
        active_sessions=account_dict.get("active_sessions", 0),
        session_file=account_dict.get("session_file", ""),
        status=account_dict.get("status", "unknown"),
        trust_score=account_dict.get("trust_score", 0),
        device_fingerprint=account_dict.get("device_fingerprint"),
    )
    db.add(account)
    await db.flush()

    # Авто-распределение по API-приложению
    from services.api_apps import pick_best_app
    best_app = await pick_best_app(db, user.id)
    if best_app:
        account.api_app_id = best_app.id
        await db.flush()

    return account