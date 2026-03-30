"""
GramGPT API — routers/warmup.py
Прогрев аккаунтов: имитация действий живого человека.
По ТЗ раздел 3.4.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.warmup import WarmupTask
from models.account import TelegramAccount

router = APIRouter(prefix="/warmup", tags=["warmup"])


# ── Schemas ──────────────────────────────────────────────────

class WarmupCreate(BaseModel):
    account_id: int
    mode: str = "normal"           # careful | normal | aggressive
    read_feed: bool = True
    view_stories: bool = True
    set_reactions: bool = True
    join_channels: bool = False


class WarmupUpdate(BaseModel):
    mode: Optional[str] = None
    read_feed: Optional[bool] = None
    view_stories: Optional[bool] = None
    set_reactions: Optional[bool] = None
    join_channels: Optional[bool] = None


# ── Helpers ──────────────────────────────────────────────────

MODE_LIMITS = {
    "careful":    {"actions_per_hour": 5,  "delay_min": 30, "delay_max": 120},
    "normal":     {"actions_per_hour": 15, "delay_min": 10, "delay_max": 60},
    "aggressive": {"actions_per_hour": 30, "delay_min": 5,  "delay_max": 30},
}

def _task_to_dict(t: WarmupTask) -> dict:
    return {
        "id": t.id,
        "account_id": t.account_id,
        "mode": t.mode,
        "status": t.status,
        "read_feed": t.read_feed,
        "view_stories": t.view_stories,
        "set_reactions": t.set_reactions,
        "join_channels": t.join_channels,
        "actions_done": t.actions_done,
        "feeds_read": t.feeds_read,
        "stories_viewed": t.stories_viewed,
        "reactions_set": t.reactions_set,
        "channels_joined": t.channels_joined,
        "mode_limits": MODE_LIMITS.get(t.mode, MODE_LIMITS["normal"]),
        "started_at": t.started_at.isoformat() if t.started_at else None,
        "created_at": t.created_at.isoformat(),
    }


# ── Endpoints ────────────────────────────────────────────────

@router.get("/tasks")
async def list_warmup_tasks(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WarmupTask).where(WarmupTask.user_id == current_user.id).order_by(WarmupTask.created_at.desc())
    )
    tasks = result.scalars().all()

    # Подтягиваем имена аккаунтов
    out = []
    for t in tasks:
        d = _task_to_dict(t)
        acc_r = await db.execute(select(TelegramAccount).where(TelegramAccount.id == t.account_id))
        acc = acc_r.scalar_one_or_none()
        d["account_phone"] = acc.phone if acc else "?"
        d["account_name"] = acc.first_name if acc else "?"
        out.append(d)
    return out


@router.post("/tasks")
async def create_warmup_task(
    body: WarmupCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Проверяем аккаунт
    acc_r = await db.execute(
        select(TelegramAccount).where(TelegramAccount.id == body.account_id, TelegramAccount.user_id == current_user.id)
    )
    acc = acc_r.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")

    # Проверяем нет ли уже задачи для этого аккаунта
    existing = await db.execute(
        select(WarmupTask).where(WarmupTask.account_id == body.account_id, WarmupTask.status.in_(["idle", "running"]))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Прогрев уже создан для этого аккаунта")

    t = WarmupTask(
        user_id=current_user.id,
        account_id=body.account_id,
        mode=body.mode,
        read_feed=body.read_feed,
        view_stories=body.view_stories,
        set_reactions=body.set_reactions,
        join_channels=body.join_channels,
    )
    db.add(t)
    await db.flush()
    return _task_to_dict(t)


@router.post("/tasks/{task_id}/start")
async def start_warmup(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WarmupTask).where(WarmupTask.id == task_id, WarmupTask.user_id == current_user.id)
    )
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    t.status = "running"
    t.started_at = datetime.utcnow()
    await db.flush()
    return {"success": True, "status": "running"}


@router.post("/tasks/{task_id}/stop")
async def stop_warmup(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WarmupTask).where(WarmupTask.id == task_id, WarmupTask.user_id == current_user.id)
    )
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    t.status = "finished"
    t.finished_at = datetime.utcnow()
    await db.flush()
    return {"success": True, "status": "finished"}


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_warmup(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WarmupTask).where(WarmupTask.id == task_id, WarmupTask.user_id == current_user.id)
    )
    t = result.scalar_one_or_none()
    if t:
        await db.delete(t)
        await db.flush()
