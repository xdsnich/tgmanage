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
from fastapi import HTTPException, status

from models.account import TelegramAccount, AccountStatus
from models.user import User
from schemas.account import AccountUpdate

logger = logging.getLogger(__name__)


async def get_accounts(db: AsyncSession, user_id: int) -> list[TelegramAccount]:
    result = await db.execute(
        select(TelegramAccount)
        .where(TelegramAccount.user_id == user_id)
        .order_by(TelegramAccount.added_at.desc())
    )
    return result.scalars().all()


async def get_account(db: AsyncSession, account_id: int, user_id: int) -> TelegramAccount:
    result = await db.execute(
        select(TelegramAccount).where(
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
        select(TelegramAccount).where(
            TelegramAccount.phone == phone,
            TelegramAccount.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def get_stats(db: AsyncSession, user_id: int) -> dict:
    """Статистика по аккаунтам"""
    result = await db.execute(
        select(TelegramAccount).where(TelegramAccount.user_id == user_id)
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


async def check_limit(db: AsyncSession, user: User):
    """Проверяет не превышен ли лимит аккаунтов по тарифу"""
    result = await db.execute(
        select(func.count()).where(TelegramAccount.user_id == user.id)
    )
    count = result.scalar()
    if count >= user.account_limit:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Достигнут лимит аккаунтов для тарифа {user.plan} ({user.account_limit} акк.). Обновите тариф."
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
    """Удаляет аккаунт из БД + удаляет .session файл с диска"""
    # Удаляем .session файл если есть
    if account.session_file:
        session_path = Path(account.session_file)
        if session_path.exists():
            try:
                session_path.unlink()
                logger.info(f"Удалён session файл: {session_path}")
            except Exception as e:
                logger.warning(f"Не удалось удалить session файл {session_path}: {e}")

        # Также удаляем .session-journal если есть
        journal_path = Path(str(account.session_file) + "-journal")
        if journal_path.exists():
            try:
                journal_path.unlink()
            except:
                pass

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
        existing.updated_at = datetime.utcnow()
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
    )
    db.add(account)
    await db.flush()
    return account