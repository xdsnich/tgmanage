"""
GramGPT API — services/accounts.py
Бизнес-логика: аккаунты Telegram
"""

from typing import Optional
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from fastapi import HTTPException, status

from models.account import TelegramAccount, AccountStatus
from models.user import User
from schemas.account import AccountUpdate
# trust_score хранится в БД и обновляется через CLI


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

    # Проверяем нет ли уже такого аккаунта
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
        acc = existing
    else:
        await check_limit(db, user)
        acc = TelegramAccount(user_id=user.id, phone=phone)
        db.add(acc)

    # Маппинг полей
    field_map = {
        "tg_id":          "id",
        "first_name":     "first_name",
        "last_name":      "last_name",
        "username":       "username",
        "bio":            "bio",
        "has_photo":      "has_photo",
        "has_2fa":        "has_2fa",
        "active_sessions":"active_sessions",
        "session_file":   "session_file",
        "status":         "status",
        "trust_score":    "trust_score",
        "role":           "role",
        "tags":           "tags",
        "notes":          "notes",
        "channels":       "channels",
        "error":          "error",
    }

    for db_field, dict_key in field_map.items():
        val = account_dict.get(dict_key)
        if val is not None:
            setattr(acc, db_field, val)

    # Даты
    for date_field in ["added_at", "last_checked"]:
        val = account_dict.get(date_field)
        if val:
            try:
                setattr(acc, date_field, datetime.fromisoformat(val))
            except Exception:
                pass

    await db.flush()
    return acc


async def get_stats(db: AsyncSession, user_id: int) -> dict:
    """Возвращает статистику по аккаунтам для дашборда"""
    accounts = await get_accounts(db, user_id)

    if not accounts:
        return {"total": 0}

    total = len(accounts)
    by_status = {}
    scores = []

    for a in accounts:
        s = a.status.value
        by_status[s] = by_status.get(s, 0) + 1
        scores.append(a.trust_score)

    return {
        "total":       total,
        "active":      by_status.get("active", 0),
        "spamblock":   by_status.get("spamblock", 0),
        "frozen":      by_status.get("frozen", 0),
        "quarantine":  by_status.get("quarantine", 0),
        "error":       by_status.get("error", 0),
        "unknown":     by_status.get("unknown", 0),
        "avg_trust":   sum(scores) // total if scores else 0,
        "max_trust":   max(scores) if scores else 0,
        "min_trust":   min(scores) if scores else 0,
        "with_proxy":  sum(1 for a in accounts if a.proxy_id),
        "with_2fa":    sum(1 for a in accounts if a.has_2fa),
    }