"""
GramGPT API — routers/proxies.py
Управление прокси + назначение на аккаунты.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
from sqlalchemy.orm import joinedload
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
    added = 0
    errors = []
    for line in data.proxies_text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            errors.append(line)
            continue

        # Парсим форматы: host:port:login:pass | socks5://login:pass@host:port | host:port
        host, port, login, password, protocol = "", 0, "", "", "socks5"
        try:
            if "://" in line:
                protocol, rest = line.split("://", 1)
                if "@" in rest:
                    creds, addr = rest.rsplit("@", 1)
                    login, password = creds.split(":", 1)
                else:
                    addr = rest
                host, port = addr.rsplit(":", 1)
                port = int(port)
            else:
                parts = line.split(":")
                if len(parts) == 4:
                    host, port, login, password = parts[0], int(parts[1]), parts[2], parts[3]
                elif len(parts) == 2:
                    host, port = parts[0], int(parts[1])
                else:
                    errors.append(line)
                    continue
        except:
            errors.append(line)
            continue

        proxy = Proxy(
            user_id=current_user.id,
            host=host, port=port,
            login=login, password=password,
            protocol=protocol if protocol in ("socks5", "http") else "socks5",
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
        acc_result = await db.execute(
            select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(TelegramAccount.proxy_id == proxy_id)
        )
        for acc in acc_result.scalars().all():
            acc.proxy_id = None
        await db.delete(proxy)


class ProxyUpdate(BaseModel):
    host: Optional[str] = None
    port: Optional[int] = None
    login: Optional[str] = None
    password: Optional[str] = None
    protocol: Optional[str] = None


@router.patch("/{proxy_id}")
async def update_proxy(
    proxy_id: int,
    data: ProxyUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Редактировать прокси (хост, порт, логин, пароль, протокол)"""
    result = await db.execute(
        select(Proxy).where(Proxy.id == proxy_id, Proxy.user_id == current_user.id)
    )
    proxy = result.scalar_one_or_none()
    if not proxy:
        raise HTTPException(status_code=404, detail="Прокси не найден")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(proxy, key, value)

    # Сбрасываем статус проверки при изменении
    proxy.is_valid = None
    proxy.error = None

    await db.flush()
    return {
        "id": proxy.id, "host": proxy.host, "port": proxy.port,
        "login": proxy.login, "protocol": proxy.protocol.value if hasattr(proxy.protocol, 'value') else proxy.protocol,
        "is_valid": proxy.is_valid, "message": "Прокси обновлён",
    }


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
        select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(
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
        select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(
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


@router.post("/{proxy_id}/check")
async def check_proxy(
    proxy_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Проверяет доступность прокси + определяет страну по IP"""
    import asyncio
    import httpx

    result = await db.execute(
        select(Proxy).where(Proxy.id == proxy_id, Proxy.user_id == current_user.id)
    )
    proxy = result.scalar_one_or_none()
    if not proxy:
        raise HTTPException(status_code=404, detail="Прокси не найден")

    from datetime import datetime

    # Определяем страну по IP (бесплатный API)
    country = ""
    country_code = ""
    city = ""
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            geo = await http.get(f"http://ip-api.com/json/{proxy.host}?fields=status,country,countryCode,city")
            if geo.status_code == 200:
                data = geo.json()
                if data.get("status") == "success":
                    country = data.get("country", "")
                    country_code = data.get("countryCode", "")
                    city = data.get("city", "")
    except:
        pass

    try:
        conn = asyncio.open_connection(proxy.host, proxy.port)
        reader, writer = await asyncio.wait_for(conn, timeout=10)
        writer.close()
        await writer.wait_closed()

        proxy.is_valid = True
        proxy.error = None
        proxy.last_checked = datetime.utcnow()
        await db.flush()

        location = f"{city}, {country}" if city else country
        return {
            "success": True, "is_valid": True,
            "country": country, "country_code": country_code, "city": city,
            "message": f"✅ {proxy.host}:{proxy.port} — {location or 'OK'}",
        }

    except Exception as e:
        proxy.is_valid = False
        proxy.error = str(e)[:200]
        proxy.last_checked = datetime.utcnow()
        await db.flush()

        location = f"{city}, {country}" if city else country
        return {
            "success": True, "is_valid": False,
            "country": country, "country_code": country_code, "city": city,
            "message": f"❌ {proxy.host}:{proxy.port} — {location or ''} недоступен",
        }


@router.post("/check-all")
async def check_all_proxies(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Проверяет все прокси + определяет страну"""
    import asyncio
    import httpx

    result = await db.execute(
        select(Proxy).where(Proxy.user_id == current_user.id)
    )
    proxies = result.scalars().all()
    if not proxies:
        return {"total": 0, "valid": 0, "invalid": 0, "results": []}

    from datetime import datetime

    valid_count = 0
    results = []

    for proxy in proxies:
        # Гео
        country, city = "", ""
        try:
            async with httpx.AsyncClient(timeout=5) as http:
                geo = await http.get(f"http://ip-api.com/json/{proxy.host}?fields=status,country,countryCode,city")
                if geo.status_code == 200:
                    data = geo.json()
                    if data.get("status") == "success":
                        country = data.get("country", "")
                        city = data.get("city", "")
        except:
            pass

        # TCP check
        try:
            conn = asyncio.open_connection(proxy.host, proxy.port)
            reader, writer = await asyncio.wait_for(conn, timeout=8)
            writer.close()
            await writer.wait_closed()
            proxy.is_valid = True
            proxy.error = None
            valid_count += 1
        except Exception as e:
            proxy.is_valid = False
            proxy.error = str(e)[:200]
        proxy.last_checked = datetime.utcnow()

        location = f"{city}, {country}" if city else country
        results.append({
            "id": proxy.id, "host": proxy.host, "port": proxy.port,
            "is_valid": proxy.is_valid, "country": country, "city": city, "location": location,
        })

    await db.flush()
    return {"total": len(proxies), "valid": valid_count, "invalid": len(proxies) - valid_count, "results": results}