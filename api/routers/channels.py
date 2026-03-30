"""
GramGPT API — routers/channels.py
Управление каналами через веб.
По ТЗ раздел 2.3: создание, закрепление каналов.
"""

import sys
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.account import TelegramAccount

router = APIRouter(prefix="/channels", tags=["channels"])


# ── Safe CLI import ──────────────────────────────────────────

def _import_channel_manager():
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)

    api_config_cache = sys.modules.pop('config', None)
    for mod_name in ['ui', 'trust', 'channel_manager', 'tg_client']:
        sys.modules.pop(mod_name, None)

    try:
        import channel_manager as ch
        return ch
    finally:
        if api_config_cache:
            sys.modules['config'] = api_config_cache


# ── Schemas ──────────────────────────────────────────────────

class CreateChannelRequest(BaseModel):
    account_id: int
    title: str
    description: str = ""
    username: str = ""


class BatchCreateRequest(BaseModel):
    account_ids: list[int]
    title_template: str
    description: str = ""
    delay: float = 4.0


class PinChannelRequest(BaseModel):
    account_id: int
    channel_link: str


class PinExistingRequest(BaseModel):
    account_ids: list[int]
    channel_link: str


# ── Helper ───────────────────────────────────────────────────

async def _get_account(db, account_id, user_id) -> TelegramAccount:
    result = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.id == account_id,
            TelegramAccount.user_id == user_id,
        )
    )
    acc = result.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    return acc


def _to_dict(a: TelegramAccount) -> dict:
    return {
        "phone": a.phone,
        "session_file": a.session_file,
        "channels": a.channels or [],
        "first_name": a.first_name or "",
    }


# ── Endpoints ────────────────────────────────────────────────

@router.get("/accounts/{account_id}")
async def get_my_channels(
    account_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список каналов которыми владеет аккаунт"""
    acc = await _get_account(db, account_id, current_user.id)

    try:
        ch = _import_channel_manager()
        channels = await ch.get_my_channels(_to_dict(acc))

        if channels:
            acc.channels = channels
            await db.flush()

        return {"account_id": account_id, "channels": channels}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)}")


@router.post("/create")
async def create_channel(
    body: CreateChannelRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Создать новый канал от имени аккаунта"""
    acc = await _get_account(db, body.account_id, current_user.id)

    try:
        ch = _import_channel_manager()
        channel = await ch.create_channel(_to_dict(acc), body.title, body.description, body.username)

        if not channel:
            raise HTTPException(status_code=500, detail="Не удалось создать канал")

        channels = acc.channels or []
        channels.append(channel)
        acc.channels = channels
        await db.flush()

        return {"success": True, "channel": channel}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)}")


@router.post("/batch-create")
async def batch_create_channels(
    body: BatchCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Создать каналы для нескольких аккаунтов через Celery"""
    result = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.user_id == current_user.id,
            TelegramAccount.id.in_(body.account_ids),
        )
    )
    accounts = result.scalars().all()
    accounts_data = [_to_dict(a) for a in accounts]

    try:
        from celery import current_app
        task = current_app.send_task(
            "tasks.bulk_tasks.create_channels_bulk",
            args=[accounts_data, body.title_template, body.description, body.delay],
            queue="bulk_actions",
        )
        return {"task_id": task.id, "total": len(accounts_data), "message": f"Создание каналов для {len(accounts_data)} аккаунтов запущено"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Celery недоступен: {str(e)}")


@router.post("/pin")
async def pin_channel(
    body: PinChannelRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Закрепить канал в профиле аккаунта"""
    acc = await _get_account(db, body.account_id, current_user.id)

    try:
        ch = _import_channel_manager()
        ok = await ch.pin_channel_to_profile(_to_dict(acc), body.channel_link)
        return {"success": ok, "account_id": body.account_id, "channel_link": body.channel_link}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)}")


@router.post("/pin-existing")
async def pin_existing_channel(
    body: PinExistingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Закрепить существующий канал на несколько аккаунтов"""
    result = await db.execute(
        select(TelegramAccount).where(
            TelegramAccount.user_id == current_user.id,
            TelegramAccount.id.in_(body.account_ids),
        )
    )
    accounts = result.scalars().all()

    try:
        ch = _import_channel_manager()
        results = []
        for acc in accounts:
            ok = await ch.pin_existing_channel(_to_dict(acc), body.channel_link)
            results.append({"phone": acc.phone, "success": ok})

        return {"channel_link": body.channel_link, "results": results, "success_count": sum(1 for r in results if r["success"])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)}")