"""
GramGPT API — routers/proxies.py
Эндпоинты: управление прокси
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from schemas.proxy import ProxyCreate, ProxyOut, ProxyBulkCreate
from models.proxy import Proxy
from routers.deps import get_current_user
from models.user import User

router = APIRouter(prefix="/proxies", tags=["proxies"])


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
    """Загрузка пула прокси из текста (host:port:login:pass)"""
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
            host=parsed["host"],
            port=parsed["port"],
            login=parsed.get("login", ""),
            password=parsed.get("password", ""),
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
        await db.delete(proxy)
