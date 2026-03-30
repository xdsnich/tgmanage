"""
GramGPT API — routers/proxies.py
Управление прокси + назначение на аккаунты.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional

from database import get_db
from schemas.proxy import ProxyCreate, ProxyOut, ProxyBulkCreate
from models.proxy import Proxy
from models.account import TelegramAccount
from routers.deps import get_current_user
from models.user import User

router = APIRouter(prefix="/proxies", tags=["proxies"])


# ── Schemas ──────────────────────────────────────────────────

class AssignProxyRequest(BaseModel):
    account_id: int
    proxy_id: Optional[int] = None  # None = снять прокси


# ── CRUD ─────────────────────────────────────────────────────

@router.get("/", response_model=list[ProxyOut])
async def list_proxies(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Proxy).where(Proxy.user_id == current_user.id)
    )
    return result.scalars().all()


@router.post("/", response_model=ProxyOut, status_code=201)
async def create_proxy(
    data: ProxyCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    proxy = Proxy(user_id=current_user.id, **data.model_dump())
    db.add(proxy)
    await db.flush()
    return proxy


@router.post("/bulk")
async def create_proxies_bulk(
    data: ProxyBulkCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
    from db import parse_proxy_line

    added = 0
    errors = []
    for line in data.proxies_text.strip().splitlines():
        parsed = parse_proxy_line(line)
        if not parsed:
            errors.append(line)
            continue
        proxy = Proxy(
            user_id=current_user.id,
            host=parsed["host"], port=parsed["port"],
            login=parsed.get("login", ""), password=parsed.get("password", ""),
            protocol=parsed.get("protocol", "socks5"),
        )
        db.add(proxy)
        added += 1

    await db.flush()
    return {"added": added, "errors": errors}


@router.delete("/{proxy_id}", status_code=204)
async def delete_proxy(
    proxy_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Proxy).where(Proxy.id == proxy_id, Proxy.user_id == current_user.id)
    )
    proxy = result.scalar_one_or_none()
    if proxy:
        # Снимаем прокси со всех аккаунтов
        acc_result = await db.execute(
            select(TelegramAccount).where(TelegramAccount.proxy_id == proxy_id)
        )
        for acc in acc_result.scalars().all():
            acc.proxy_id = None
        await db.delete(proxy)


# ── Назначение прокси на аккаунт ─────────────────────────────

@router.post("/assign")
async def assign_proxy(
    body: AssignProxyRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Назначить или снять прокси с аккаунта.
    proxy_id = null → снять прокси.
    """
    # Проверяем аккаунт
    acc_result = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.id == body.account_id,
            TelegramAccount.user_id == current_user.id,
        )
    )
    acc = acc_result.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")

    if body.proxy_id:
        # Проверяем прокси
        proxy_result = await db.execute(
            select(Proxy).where(Proxy.id == body.proxy_id, Proxy.user_id == current_user.id)
        )
        proxy = proxy_result.scalar_one_or_none()
        if not proxy:
            raise HTTPException(status_code=404, detail="Прокси не найден")

        acc.proxy_id = body.proxy_id
        await db.flush()
        return {
            "success": True,
            "account_id": acc.id,
            "proxy_id": proxy.id,
            "proxy": f"{proxy.host}:{proxy.port}",
            "message": f"Прокси {proxy.host}:{proxy.port} назначен на {acc.phone}",
        }
    else:
        acc.proxy_id = None
        await db.flush()
        return {
            "success": True,
            "account_id": acc.id,
            "proxy_id": None,
            "message": f"Прокси снят с {acc.phone}",
        }


@router.post("/auto-assign")
async def auto_assign_proxies(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Автоназначение: распределяет валидные прокси по аккаунтам без прокси."""
    # Аккаунты без прокси
    acc_result = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.user_id == current_user.id,
            TelegramAccount.proxy_id.is_(None),
        )
    )
    unassigned = acc_result.scalars().all()

    # Валидные прокси
    proxy_result = await db.execute(
        select(Proxy).where(Proxy.user_id == current_user.id, Proxy.is_valid == True)
    )
    valid_proxies = proxy_result.scalars().all()

    if not valid_proxies:
        raise HTTPException(status_code=400, detail="Нет валидных прокси. Сначала проверьте прокси.")
    if not unassigned:
        return {"assigned": 0, "message": "Все аккаунты уже имеют прокси"}

    assigned = 0
    for i, acc in enumerate(unassigned):
        proxy = valid_proxies[i % len(valid_proxies)]
        acc.proxy_id = proxy.id
        assigned += 1

    await db.flush()
    return {"assigned": assigned, "message": f"Назначено {assigned} прокси на аккаунты"}