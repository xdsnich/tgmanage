"""
GramGPT API — routers/proxies.py
Управление прокси + назначение на аккаунты.

При создании прокси автоматически:
1. Определяется гео по IP (ip-api.com)
2. Проверяется TCP-доступность
3. Если указан срок — сохраняется expires_at (абсолютный timestamp)
"""

import asyncio
import httpx
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

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
    proxy_id: Optional[int] = None


# ── Helpers ──────────────────────────────────────────────────

async def _detect_geo(host: str) -> dict:
    """Определяет страну/город по IP."""
    out = {"country": "", "country_code": "", "city": ""}
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            resp = await http.get(
                f"http://ip-api.com/json/{host}?fields=status,country,countryCode,city"
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success":
                    out["country"] = data.get("country", "")
                    out["country_code"] = data.get("countryCode", "")
                    out["city"] = data.get("city", "")
    except Exception:
        pass
    return out


async def _tcp_check(host: str, port: int, timeout: float = 8) -> tuple[bool, str]:
    try:
        conn = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(conn, timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True, ""
    except Exception as e:
        return False, str(e)[:200]


async def _check_and_update(proxy: Proxy) -> None:
    """Проверка + определение гео в одном месте."""
    geo = await _detect_geo(proxy.host)
    if geo["country"]:
        proxy.country = geo["country"]
    if geo["country_code"]:
        proxy.country_code = geo["country_code"]
    if geo["city"]:
        proxy.city = geo["city"]

    ok, err = await _tcp_check(proxy.host, proxy.port)
    proxy.is_valid = ok
    proxy.error = None if ok else err
    proxy.last_checked = datetime.utcnow()


def _compute_expires_at(days: int, hours: int) -> Optional[datetime]:
    """Возвращает абсолютный timestamp через X дней Y часов. None если 0+0."""
    total_hours = (days or 0) * 24 + (hours or 0)
    if total_hours <= 0:
        return None
    return datetime.utcnow() + timedelta(hours=total_hours)


# ── CRUD ─────────────────────────────────────────────────────

@router.get("/", response_model=list[ProxyOut])
async def list_proxies(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    from sqlalchemy import func
    from models.account import TelegramAccount

    proxies = (await db.execute(
        select(Proxy).where(Proxy.user_id == current_user.id)
    )).scalars().all()

    # Один SQL для всех счётчиков, чтобы не плодить N+1 запросов на 100+ прокси
    counts_rows = (await db.execute(
        select(TelegramAccount.proxy_id, func.count(TelegramAccount.id))
        .where(
            TelegramAccount.user_id == current_user.id,
            TelegramAccount.proxy_id.in_([p.id for p in proxies]) if proxies else False,
        )
        .group_by(TelegramAccount.proxy_id)
    )).all() if proxies else []
    counts = {row[0]: row[1] for row in counts_rows}

    out = []
    for p in proxies:
        po = ProxyOut.model_validate(p)
        po.accounts_count = counts.get(p.id, 0)
        out.append(po)
    return out


@router.post("/", response_model=ProxyOut, status_code=201)
async def create_proxy(
    data: ProxyCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Создаёт прокси + сразу проверяет + устанавливает срок действия."""
    expires_at = _compute_expires_at(data.duration_days, data.duration_hours)

    proxy = Proxy(
        user_id=current_user.id,
        host=data.host,
        port=data.port,
        login=data.login,
        password=data.password,
        protocol=data.protocol,
        country=data.country,
        city=data.city,
        expires_at=expires_at,
    )
    db.add(proxy)
    await db.flush()

    await _check_and_update(proxy)
    await db.flush()
    return proxy


@router.post("/bulk")
async def create_proxies_bulk(
    data: ProxyBulkCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Массовое добавление + авто-проверка + общий срок действия."""
    added_proxies = []
    errors = []
    expires_at = _compute_expires_at(data.duration_days, data.duration_hours)

    for line in data.proxies_text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

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
        except Exception:
            errors.append(line)
            continue

        proxy = Proxy(
            user_id=current_user.id,
            host=host, port=port,
            login=login, password=password,
            protocol=protocol if protocol in ("socks5", "http") else "socks5",
            expires_at=expires_at,
        )
        db.add(proxy)
        added_proxies.append(proxy)

    await db.flush()

    # Параллельная проверка
    semaphore = asyncio.Semaphore(10)
    async def check_one(proxy: Proxy):
        async with semaphore:
            await _check_and_update(proxy)

    if added_proxies:
        await asyncio.gather(*[check_one(p) for p in added_proxies])
        await db.flush()

    valid_count = sum(1 for p in added_proxies if p.is_valid)
    return {
        "added": len(added_proxies),
        "valid": valid_count,
        "invalid": len(added_proxies) - valid_count,
        "errors": errors,
    }


@router.patch("/{proxy_id}", response_model=ProxyOut)
async def update_proxy(
    proxy_id: int,
    data: ProxyCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Proxy).where(Proxy.id == proxy_id, Proxy.user_id == current_user.id)
    )
    proxy = result.scalar_one_or_none()
    if not proxy:
        raise HTTPException(status_code=404, detail="Прокси не найден")

    host_changed = (proxy.host != data.host)

    proxy.host = data.host
    proxy.port = data.port
    proxy.login = data.login
    if data.password:
        proxy.password = data.password
    proxy.protocol = data.protocol
    if data.country:
        proxy.country = data.country
    if data.city:
        proxy.city = data.city

    # Срок действия: если задан duration > 0 — обновляем expires_at (продлеваем)
    new_expires = _compute_expires_at(data.duration_days, data.duration_hours)
    if new_expires is not None:
        proxy.expires_at = new_expires
    # Если duration = 0 — НЕ трогаем (сохраняем существующий срок)

    await db.flush()

    if host_changed:
        await _check_and_update(proxy)
        await db.flush()
    return proxy


@router.post("/{proxy_id}/clear-expiration")
async def clear_expiration(
    proxy_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Убрать срок действия (сделать бессрочным)."""
    result = await db.execute(
        select(Proxy).where(Proxy.id == proxy_id, Proxy.user_id == current_user.id)
    )
    proxy = result.scalar_one_or_none()
    if not proxy:
        raise HTTPException(status_code=404, detail="Прокси не найден")
    proxy.expires_at = None
    await db.flush()
    return {"success": True, "message": "Срок действия убран"}


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
            select(TelegramAccount).where(TelegramAccount.proxy_id == proxy.id)
        )
        for acc in acc_result.scalars().all():
            acc.proxy_id = None
        await db.delete(proxy)
        await db.flush()


# ── ASSIGN / AUTO-ASSIGN ─────────────────────────────────────

@router.post("/assign")
async def assign_proxy(
    data: AssignProxyRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    acc_r = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.id == data.account_id,
            TelegramAccount.user_id == current_user.id,
        )
    )
    account = acc_r.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")

    if data.proxy_id is not None:
        prx_r = await db.execute(
            select(Proxy).where(Proxy.id == data.proxy_id, Proxy.user_id == current_user.id)
        )
        if not prx_r.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Прокси не найден")

    account.proxy_id = data.proxy_id
    await db.flush()
    return {"success": True, "proxy_id": data.proxy_id}


@router.post("/auto-assign")
async def auto_assign(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Берём только валидные И не истёкшие прокси
    now = datetime.utcnow()
    proxies_r = await db.execute(
        select(Proxy).where(
            Proxy.user_id == current_user.id,
            Proxy.is_valid == True,
        )
    )
    all_valid = proxies_r.scalars().all()
    valid_proxies = [p for p in all_valid if p.expires_at is None or p.expires_at > now]

    accs_r = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.user_id == current_user.id,
            TelegramAccount.proxy_id == None,
        )
    )
    unassigned = accs_r.scalars().all()

    if not valid_proxies:
        raise HTTPException(status_code=400, detail="Нет валидных неистёкших прокси.")
    if not unassigned:
        return {"assigned": 0, "message": "Все аккаунты уже имеют прокси"}

    assigned = 0
    for i, acc in enumerate(unassigned):
        proxy = valid_proxies[i % len(valid_proxies)]
        acc.proxy_id = proxy.id
        assigned += 1

    await db.flush()
    return {"assigned": assigned, "message": f"Назначено {assigned} прокси на аккаунты"}


# ── CHECK ────────────────────────────────────────────────────

@router.post("/{proxy_id}/check")
async def check_proxy(
    proxy_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Proxy).where(Proxy.id == proxy_id, Proxy.user_id == current_user.id)
    )
    proxy = result.scalar_one_or_none()
    if not proxy:
        raise HTTPException(status_code=404, detail="Прокси не найден")

    await _check_and_update(proxy)
    await db.flush()

    location = f"{proxy.city}, {proxy.country}" if proxy.city else proxy.country
    return {
        "success": True,
        "is_valid": proxy.is_valid,
        "country": proxy.country,
        "country_code": proxy.country_code,
        "city": proxy.city,
        "location": location,
        "message": f"{'✅' if proxy.is_valid else '❌'} {proxy.host}:{proxy.port} — {location or ('OK' if proxy.is_valid else 'недоступен')}",
    }


@router.post("/check-all")
async def check_all_proxies(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Proxy).where(Proxy.user_id == current_user.id)
    )
    proxies = result.scalars().all()
    if not proxies:
        return {"total": 0, "valid": 0, "invalid": 0, "results": []}

    semaphore = asyncio.Semaphore(10)
    async def check_one(proxy: Proxy):
        async with semaphore:
            await _check_and_update(proxy)

    await asyncio.gather(*[check_one(p) for p in proxies])
    await db.flush()

    valid_count = sum(1 for p in proxies if p.is_valid)
    results = [{
        "id": p.id, "host": p.host, "port": p.port,
        "is_valid": p.is_valid,
        "country": p.country, "city": p.city,
        "location": f"{p.city}, {p.country}" if p.city else p.country,
    } for p in proxies]

    return {
        "total": len(proxies),
        "valid": valid_count,
        "invalid": len(proxies) - valid_count,
        "results": results,
    }