"""
GramGPT API — routers/analytics.py
Эндпоинты аналитики и дашборда
По ТЗ раздел 4: Health Dashboard, Trust Score, фильтрация
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import joinedload
from datetime import datetime, timedelta
from typing import Optional

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount, AccountStatus, AccountRole

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/dashboard")
async def get_dashboard(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Главный дашборд — полная сводка по всем аккаунтам.
    По ТЗ: Health Dashboard со статусами, Trust Score, проверками.
    """
    result = await db.execute(
        select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(TelegramAccount.user_id == current_user.id)
    )
    accounts = result.scalars().all()

    if not accounts:
        return {
            "total": 0,
            "by_status": {},
            "trust": {"avg": 0, "max": 0, "min": 0, "buckets": {}},
            "checks": {"today": 0, "week": 0, "never": 0},
            "profile": {"with_username": 0, "with_bio": 0, "with_photo": 0, "with_proxy": 0, "with_2fa": 0},
            "plan": current_user.plan.value,
            "account_limit": current_user.account_limit,
        }

    total = len(accounts)
    now = datetime.utcnow()

    # Статусы
    by_status = {}
    for a in accounts:
        s = a.status.value
        by_status[s] = by_status.get(s, 0) + 1

    # Trust Score
    scores = [a.trust_score for a in accounts]
    trust_buckets = {
        "excellent": sum(1 for s in scores if s >= 80),
        "good":      sum(1 for s in scores if 60 <= s < 80),
        "medium":    sum(1 for s in scores if 40 <= s < 60),
        "weak":      sum(1 for s in scores if 20 <= s < 40),
        "critical":  sum(1 for s in scores if s < 20),
    }

    # Проверки
    checked_today = 0
    checked_week  = 0
    never_checked = 0
    for a in accounts:
        lc = a.last_checked
        if not lc:
            never_checked += 1
            continue
        diff = (now - lc).days
        if diff == 0:
            checked_today += 1
        if diff <= 7:
            checked_week += 1

    # Профили
    with_username = sum(1 for a in accounts if a.username)
    with_bio      = sum(1 for a in accounts if a.bio)
    with_photo    = sum(1 for a in accounts if a.has_photo)
    with_proxy    = sum(1 for a in accounts if a.proxy_id)
    with_2fa      = sum(1 for a in accounts if a.has_2fa)

    return {
        "total": total,
        "by_status": by_status,
        "trust": {
            "avg": sum(scores) // total if scores else 0,
            "max": max(scores) if scores else 0,
            "min": min(scores) if scores else 0,
            "buckets": trust_buckets,
        },
        "checks": {
            "today": checked_today,
            "week": checked_week,
            "never": never_checked,
        },
        "profile": {
            "with_username": with_username,
            "with_bio": with_bio,
            "with_photo": with_photo,
            "with_proxy": with_proxy,
            "with_2fa": with_2fa,
        },
        "plan": current_user.plan.value,
        "account_limit": current_user.account_limit,
        "used_slots": total,
    }


@router.get("/search")
async def search_accounts(
    q: str = Query(..., min_length=1),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Поиск аккаунтов по номеру, username, имени, статусу, тегу.
    По ТЗ: поиск по номеру телефона, username, гео, статусу ограничений.
    """
    result = await db.execute(
        select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(TelegramAccount.user_id == current_user.id)
    )
    accounts = result.scalars().all()

    q_lower = q.lower().strip()
    matched = []
    for a in accounts:
        tags = a.tags or []
        if (
            q_lower in (a.phone or "").lower() or
            q_lower in (a.username or "").lower() or
            q_lower in (a.first_name or "").lower() or
            q_lower in (a.last_name or "").lower() or
            q_lower in (a.status.value or "").lower() or
            q_lower in (a.role.value or "").lower() or
            q_lower in (a.notes or "").lower() or
            any(q_lower in tag.lower() for tag in tags)
        ):
            matched.append(a)

    return {
        "query": q,
        "count": len(matched),
        "results": [_account_to_dict(a) for a in matched],
    }


@router.get("/filter")
async def filter_accounts(
    status: Optional[str] = None,
    role: Optional[str] = None,
    min_trust: Optional[int] = None,
    max_trust: Optional[int] = None,
    has_proxy: Optional[bool] = None,
    has_username: Optional[bool] = None,
    has_photo: Optional[bool] = None,
    has_2fa: Optional[bool] = None,
    tag: Optional[str] = None,
    sort_by: str = "trust",       # trust | added | checked | phone
    sort_desc: bool = True,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Фильтрация и сортировка аккаунтов.
    По ТЗ: фильтрация по тегам, роли, Trust Score, дате добавления.
    """
    result = await db.execute(
        select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(TelegramAccount.user_id == current_user.id)
    )
    accounts = list(result.scalars().all())

    # Фильтры
    if status:
        accounts = [a for a in accounts if a.status.value == status]
    if role:
        accounts = [a for a in accounts if a.role.value == role]
    if min_trust is not None:
        accounts = [a for a in accounts if a.trust_score >= min_trust]
    if max_trust is not None:
        accounts = [a for a in accounts if a.trust_score <= max_trust]
    if has_proxy is not None:
        accounts = [a for a in accounts if bool(a.proxy_id) == has_proxy]
    if has_username is not None:
        accounts = [a for a in accounts if bool(a.username) == has_username]
    if has_photo is not None:
        accounts = [a for a in accounts if a.has_photo == has_photo]
    if has_2fa is not None:
        accounts = [a for a in accounts if a.has_2fa == has_2fa]
    if tag:
        accounts = [a for a in accounts if tag in (a.tags or [])]

    # Сортировка
    key_map = {
        "trust":   lambda a: a.trust_score,
        "added":   lambda a: a.added_at or datetime.min,
        "checked": lambda a: a.last_checked or datetime.min,
        "phone":   lambda a: a.phone or "",
        "status":  lambda a: a.status.value,
    }
    key_fn = key_map.get(sort_by, key_map["trust"])
    accounts.sort(key=key_fn, reverse=sort_desc)

    return {
        "count": len(accounts),
        "filters": {
            "status": status, "role": role,
            "min_trust": min_trust, "max_trust": max_trust,
            "has_proxy": has_proxy, "has_username": has_username,
            "tag": tag,
        },
        "results": [_account_to_dict(a) for a in accounts],
    }


@router.get("/account/{account_id}/detail")
async def account_detail(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Детальная информация по аккаунту с рекомендациями Trust Score.
    По ТЗ: детальный просмотр + подсказки по Trust Score.
    """
    result = await db.execute(
        select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(
            TelegramAccount.id == account_id,
            TelegramAccount.user_id == current_user.id,
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Аккаунт не найден")

    # Рекомендации
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
    import trust as trust_module

    acc_dict = _account_to_dict(account)
    # trust_module.get_recommendations принимает dict
    tips = trust_module.get_recommendations({
        "username": account.username,
        "bio": account.bio,
        "has_photo": account.has_photo,
        "status": account.status.value,
    })

    if account.active_sessions and account.active_sessions > 5:
        tips.append(f"⚠️ {account.active_sessions} активных сессий — завершите лишние")
    if not account.proxy_id:
        tips.append("🔒 Назначьте прокси для безопасности")
    if not account.last_checked:
        tips.append("🔍 Аккаунт ни разу не проверялся")

    # Возраст в базе
    days_in_db = (datetime.utcnow() - account.added_at).days if account.added_at else 0

    return {
        **acc_dict,
        "days_in_db": days_in_db,
        "trust_grade": trust_module.get_grade(account.trust_score),
        "recommendations": tips,
        "channels_count": len(account.channels or []),
    }


# ── Helpers ──────────────────────────────────────────────────

def _account_to_dict(a: TelegramAccount) -> dict:
    return {
        "id": a.id,
        "phone": a.phone,
        "tg_id": a.tg_id,
        "first_name": a.first_name,
        "last_name": a.last_name,
        "username": a.username,
        "bio": a.bio,
        "has_photo": a.has_photo,
        "has_2fa": a.has_2fa,
        "active_sessions": a.active_sessions,
        "status": a.status.value,
        "trust_score": a.trust_score,
        "role": a.role.value,
        "tags": a.tags or [],
        "notes": a.notes,
        "channels": a.channels or [],
        "proxy_id": a.proxy_id,
        "added_at": a.added_at.isoformat() if a.added_at else None,
        "last_checked": a.last_checked.isoformat() if a.last_checked else None,
        "error": a.error,
    }
