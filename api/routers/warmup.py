"""
GramGPT API — routers/warmup.py (v2)
Прогрев аккаунтов с логами, расписанием и умным распределением.
"""

import random
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.warmup import WarmupTask
from models.warmup_log import WarmupLog
from models.account import TelegramAccount

router = APIRouter(prefix="/warmup", tags=["warmup"])


# ── Schemas ──────────────────────────────────────────────────

class WarmupCreate(BaseModel):
    account_ids: list[int]               # Несколько аккаунтов сразу
    total_days: int = 7                  # Сколько дней прогревать
    mode: str = "normal"                 # careful | normal | aggressive


class WarmupUpdate(BaseModel):
    mode: Optional[str] = None
    total_days: Optional[int] = None


# ── Конфиг режимов ───────────────────────────────────────────

MODE_CONFIG = {
    "careful":    {"day_mult": 0.5, "label": "🐢 Осторожный", "desc": "Мало действий, большие паузы"},
    "normal":     {"day_mult": 1.0, "label": "👤 Нормальный", "desc": "Как обычный пользователь"},
    "aggressive": {"day_mult": 1.5, "label": "⚡ Агрессивный", "desc": "Больше действий, быстрее прогрев"},
}

ACTION_LABELS = {
    "read_feed":     "📖 Чтение ленты",
    "set_reaction":  "😍 Реакция",
    "view_stories":  "👁 Stories",
    "view_profile":  "👤 Профиль",
    "typing":        "⌨️ Печатает",
    "search":        "🔍 Поиск",
    "join_channel":  "📢 Вступление",
    "forward_saved": "💾 Пересылка",
    "error":         "❌ Ошибка",
}


# ── Helpers ──────────────────────────────────────────────────

def _task_to_dict(t: WarmupTask, logs_count: int = 0) -> dict:
    return {
        "id": t.id,
        "account_id": t.account_id,
        "mode": t.mode,
        "status": t.status,
        "day": getattr(t, 'day', 1) or 1,
        "total_days": getattr(t, 'total_days', 7) or 7,
        "today_actions": getattr(t, 'today_actions', 0) or 0,
        "today_limit": getattr(t, 'today_limit', 5) or 5,
        "is_resting": getattr(t, 'is_resting', False) or False,
        "actions_done": t.actions_done,
        "feeds_read": t.feeds_read,
        "stories_viewed": t.stories_viewed,
        "reactions_set": t.reactions_set,
        "channels_joined": t.channels_joined,
        "next_action_at": t.next_action_at.isoformat() if getattr(t, 'next_action_at', None) else None,
        "start_offset_min": getattr(t, 'start_offset_min', 0) or 0,
        "mode_config": MODE_CONFIG.get(t.mode, MODE_CONFIG["normal"]),
        "logs_count": logs_count,
        "started_at": t.started_at.isoformat() if t.started_at else None,
        "finished_at": t.finished_at.isoformat() if getattr(t, 'finished_at', None) else None,
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

    out = []
    for t in tasks:
        # Считаем логи
        logs_r = await db.execute(
            select(func.count(WarmupLog.id)).where(WarmupLog.task_id == t.id)
        )
        logs_count = logs_r.scalar() or 0

        d = _task_to_dict(t, logs_count)

        # Подтягиваем имя аккаунта
        acc_r = await db.execute(select(TelegramAccount).where(TelegramAccount.id == t.account_id))
        acc = acc_r.scalar_one_or_none()
        d["account_phone"] = acc.phone if acc else "?"
        d["account_name"] = acc.first_name if acc else "?"
        out.append(d)

    return out


@router.post("/tasks")
async def create_warmup_tasks(
    body: WarmupCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Создать прогрев для нескольких аккаунтов сразу."""
    if not body.account_ids:
        raise HTTPException(status_code=400, detail="Выбери хотя бы один аккаунт")

    created = []
    skipped = []

    for i, acc_id in enumerate(body.account_ids):
        # Проверяем аккаунт
        acc_r = await db.execute(
            select(TelegramAccount).where(
                TelegramAccount.id == acc_id,
                TelegramAccount.user_id == current_user.id,
            )
        )
        acc = acc_r.scalar_one_or_none()
        if not acc:
            skipped.append({"id": acc_id, "reason": "Не найден"})
            continue

        # Проверяем нет ли уже активного
        existing = await db.execute(
            select(WarmupTask).where(
                WarmupTask.account_id == acc_id,
                WarmupTask.status.in_(["idle", "running"]),
            )
        )
        if existing.scalar_one_or_none():
            skipped.append({"id": acc_id, "reason": "Уже есть активный прогрев"})
            continue

        # Случайный offset старта (0–90 минут)
        offset = random.randint(0, 90) * i  # Каждый следующий — ещё позже
        offset = min(offset, 180)  # Максимум 3 часа

        t = WarmupTask(
            user_id=current_user.id,
            account_id=acc_id,
            mode=body.mode,
            total_days=body.total_days,
            start_offset_min=offset,
            read_feed=True,
            view_stories=True,
            set_reactions=True,
            join_channels=True,
        )
        db.add(t)
        await db.flush()
        created.append({
            "id": t.id,
            "account_id": acc_id,
            "account_name": acc.first_name or acc.phone,
            "start_offset_min": offset,
        })

    return {
        "created": len(created),
        "skipped": len(skipped),
        "tasks": created,
        "skipped_details": skipped,
        "message": f"Создано {len(created)} задач. Пропущено: {len(skipped)}",
    }


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

    now = datetime.utcnow()
    t.status = "running"
    t.started_at = now
    t.day = 1
    t.day_started_at = now
    t.today_actions = 0

    # Случайный лимит для первого дня
    from tasks.warmup_v2 import get_day_limit
    min_a, max_a = get_day_limit(1)
    mult = MODE_CONFIG.get(t.mode, {}).get("day_mult", 1.0)
    t.today_limit = int(random.randint(min_a, max_a) * mult)

    # Offset — первое действие не сразу
    offset = getattr(t, 'start_offset_min', 0) or 0
    t.next_action_at = now + timedelta(minutes=offset)

    await db.flush()
    return {"success": True, "status": "running", "start_offset_min": offset,
            "message": f"Прогрев запущен. Первое действие через {offset} мин."}


@router.post("/tasks/start-all")
async def start_all_warmups(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запустить все idle задачи."""
    result = await db.execute(
        select(WarmupTask).where(
            WarmupTask.user_id == current_user.id,
            WarmupTask.status == "idle",
        )
    )
    tasks = result.scalars().all()
    if not tasks:
        return {"started": 0, "message": "Нет задач для запуска"}

    from tasks.warmup_v2 import get_day_limit
    now = datetime.utcnow()
    started = 0

    for t in tasks:
        t.status = "running"
        t.started_at = now
        t.day = 1
        t.day_started_at = now
        t.today_actions = 0

        min_a, max_a = get_day_limit(1)
        mult = MODE_CONFIG.get(t.mode, {}).get("day_mult", 1.0)
        t.today_limit = int(random.randint(min_a, max_a) * mult)

        offset = getattr(t, 'start_offset_min', 0) or 0
        t.next_action_at = now + timedelta(minutes=offset)
        started += 1

    await db.flush()
    return {"started": started, "message": f"Запущено {started} задач"}


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
        # Удаляем логи
        await db.execute(
            select(WarmupLog).where(WarmupLog.task_id == t.id)
        )
        await db.delete(t)
        await db.flush()


# ── ЛОГИ ─────────────────────────────────────────────────────

@router.get("/tasks/{task_id}/logs")
async def get_warmup_logs(
    task_id: int,
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Логи конкретной задачи прогрева."""
    # Проверяем что задача принадлежит пользователю
    t_r = await db.execute(
        select(WarmupTask).where(WarmupTask.id == task_id, WarmupTask.user_id == current_user.id)
    )
    if not t_r.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Задача не найдена")

    result = await db.execute(
        select(WarmupLog)
        .where(WarmupLog.task_id == task_id)
        .order_by(desc(WarmupLog.created_at))
        .limit(limit)
    )
    logs = result.scalars().all()

    return [{
        "id": l.id,
        "action": l.action,
        "action_label": ACTION_LABELS.get(l.action, l.action),
        "detail": l.detail,
        "emoji": l.emoji,
        "channel": l.channel,
        "success": l.success,
        "error": l.error,
        "created_at": l.created_at.isoformat(),
    } for l in logs]


@router.get("/logs/live")
async def get_live_logs(
    limit: int = Query(default=30, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Последние логи по ВСЕМ задачам пользователя — лайв-вид."""
    # Находим task_ids пользователя
    tasks_r = await db.execute(
        select(WarmupTask.id).where(WarmupTask.user_id == current_user.id)
    )
    task_ids = [r[0] for r in tasks_r.all()]

    if not task_ids:
        return []

    result = await db.execute(
        select(WarmupLog)
        .where(WarmupLog.task_id.in_(task_ids))
        .order_by(desc(WarmupLog.created_at))
        .limit(limit)
    )
    logs = result.scalars().all()

    # Подгружаем имена аккаунтов
    acc_ids = list(set(l.account_id for l in logs))
    acc_map = {}
    if acc_ids:
        accs_r = await db.execute(
            select(TelegramAccount).where(TelegramAccount.id.in_(acc_ids))
        )
        for a in accs_r.scalars().all():
            acc_map[a.id] = {"phone": a.phone, "name": a.first_name or a.phone}

    return [{
        "id": l.id,
        "account_id": l.account_id,
        "account_name": acc_map.get(l.account_id, {}).get("name", "?"),
        "account_phone": acc_map.get(l.account_id, {}).get("phone", "?"),
        "action": l.action,
        "action_label": ACTION_LABELS.get(l.action, l.action),
        "detail": l.detail,
        "emoji": l.emoji,
        "channel": l.channel,
        "success": l.success,
        "error": l.error,
        "created_at": l.created_at.isoformat(),
    } for l in logs]


@router.get("/modes")
async def list_modes():
    """Доступные режимы прогрева."""
    return MODE_CONFIG