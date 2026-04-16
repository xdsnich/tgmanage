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
    "session_start": "▶ Начало сессии",
    "session_end":   "⏹ Конец сессии",
    "rest_day":      "😴 День отдыха",
    "finished":      "✅ Завершён",
    "error":         "❌ Ошибка",
    "send_saved":    "💬 Сообщение в Saved",
    "reply_dm":      "↩️ Ответ на ЛС",
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
        "next_action_at": (t.next_action_at.isoformat() + "Z") if getattr(t, 'next_action_at', None) else None,
        "start_offset_min": getattr(t, 'start_offset_min', 0) or 0,
        "mode_config": MODE_CONFIG.get(t.mode, MODE_CONFIG["normal"]),
        "logs_count": logs_count,
        "started_at": t.started_at.isoformat() if t.started_at else None,
        "finished_at": t.finished_at.isoformat() if getattr(t, 'finished_at', None) else None,
        "created_at": t.created_at.isoformat(),
        "batch_id": getattr(t, 'batch_id', None) or f"single_{t.id}",
        "batch_name": getattr(t, 'batch_name', None),
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
    # Генерируем batch_id для группировки
    import secrets
    batch_id = secrets.token_hex(8)
    batch_name = f"Прогрев {len(body.account_ids)} акк. — {datetime.utcnow().strftime('%d.%m %H:%M')}"

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
            batch_id=batch_id,
            batch_name=batch_name,
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
    day_mult = {"careful": 0.6, "normal": 1.0, "aggressive": 1.4}.get(t.mode, 1.0)
    t.today_limit = int(random.randint(25, 50) * 0.4 * day_mult)

    # Offset — первое действие не сразу
    offset = getattr(t, 'start_offset_min', 0) or 0
    t.next_action_at = now + timedelta(minutes=offset)

    await db.flush()

    # Генерируем план прогрева
    from tasks.plan_generator import generate_daily_plan
    from tasks.behavior_engine import assign_personality
    from models.campaign_plan import CampaignPlan
    from models.account import TelegramAccount
    from sqlalchemy import delete as sa_delete
    from datetime import date

    acc = (await db.execute(select(TelegramAccount).where(TelegramAccount.id == t.account_id))).scalar_one_or_none()
    await db.execute(sa_delete(CampaignPlan).where(CampaignPlan.warmup_task_id == t.id))

    total_days = getattr(t, 'total_days', 7) or 7
    personality = assign_personality(str(t.account_id))

    for day_num in range(1, total_days + 1):
        plan_date = date.today() + timedelta(days=day_num - 1)
        plan = generate_daily_plan(
            account_id=t.account_id,
            phone=acc.phone if acc else str(t.account_id),
            campaign_channels=[],
            campaign_id=0,
            day_number=day_num,
            comments_today=0,
            personality=personality,
        )
        db.add(CampaignPlan(
            campaign_id=None, warmup_task_id=t.id,
            account_id=t.account_id, plan_date=plan_date,
            day_number=day_num, plan=plan,
            total_comments=0, executed_idx=0, status="active",
        ))

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
    from tasks.plan_generator import generate_daily_plan
    from tasks.behavior_engine import assign_personality
    from models.campaign_plan import CampaignPlan
    from models.account import TelegramAccount
    from sqlalchemy import delete as sa_delete
    from datetime import date

    for t in tasks:
        acc = (await db.execute(select(TelegramAccount).where(TelegramAccount.id == t.account_id))).scalar_one_or_none()
        await db.execute(sa_delete(CampaignPlan).where(CampaignPlan.warmup_task_id == t.id))

        total_days = getattr(t, 'total_days', 7) or 7
        personality = assign_personality(str(t.account_id))

        for day_num in range(1, total_days + 1):
            plan_date = date.today() + timedelta(days=day_num - 1)
            plan = generate_daily_plan(
                account_id=t.account_id,
                phone=acc.phone if acc else str(t.account_id),
                campaign_channels=[],
                campaign_id=0,
                day_number=day_num,
                comments_today=0,
                personality=personality,
            )
            db.add(CampaignPlan(
                campaign_id=None, warmup_task_id=t.id,
                account_id=t.account_id, plan_date=plan_date,
                day_number=day_num, plan=plan,
                total_comments=0, executed_idx=0, status="active",
            ))

    await db.flush()
    return {"started": started, "message": f"Запущено {started} задач"}


@router.post("/tasks/{task_id}/pause")
async def pause_warmup(
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

    t.status = "paused"
    await db.flush()
    return {"success": True, "status": "paused"}


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

@router.get("/tasks/{task_id}/plans")
async def get_warmup_plans(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from models.campaign_plan import CampaignPlan
    from models.account import TelegramAccount

    # Проверяем владельца
    wt = (await db.execute(
        select(WarmupTask).where(WarmupTask.id == task_id, WarmupTask.user_id == current_user.id)
    )).scalar_one_or_none()
    if not wt:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    result = await db.execute(
        select(CampaignPlan).where(CampaignPlan.warmup_task_id == task_id)
        .order_by(CampaignPlan.plan_date)
    )
    plans = result.scalars().all()

    out = []
    for p in plans:
        acc = (await db.execute(
            select(TelegramAccount).where(TelegramAccount.id == p.account_id)
        )).scalar_one_or_none()
        sessions = p.plan.get("sessions", [])
        out.append({
            "id": p.id,
            "account_phone": acc.phone if acc else "?",
            "plan_date": p.plan_date.isoformat(),
            "day_number": p.day_number,
            "personality": p.plan.get("personality", "?"),
            "mood": p.plan.get("mood", "?"),
            "total_sessions": len(sessions),
            "total_comments": p.total_comments,
            "executed_idx": p.executed_idx,
            "status": p.status,
            "sessions": sessions,
        })
    return out


@router.get("/tasks/{task_id}/activity")
async def get_warmup_activity(
    task_id: int,
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from models.warmup_log import WarmupLog
    from models.account import TelegramAccount

    wt = (await db.execute(
        select(WarmupTask).where(WarmupTask.id == task_id, WarmupTask.user_id == current_user.id)
    )).scalar_one_or_none()
    if not wt:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    # Логи от plan_executor (task_id=NULL) для этого аккаунта
    # Дата создания задачи — берём логи только после её запуска
    started_at = wt.started_at or wt.created_at or datetime.utcnow()

    # Логи от plan_executor (task_id=NULL) для этого аккаунта, СОЗДАННЫЕ ПОСЛЕ старта задачи
    plan_logs = (await db.execute(
        select(WarmupLog).where(
            WarmupLog.account_id == wt.account_id,
            WarmupLog.task_id == None,
            WarmupLog.created_at >= started_at,
        ).order_by(WarmupLog.created_at.desc()).limit(limit)
    )).scalars().all()

    # Старые логи от warmup_v2 (по task_id)
    old_logs = (await db.execute(
        select(WarmupLog).where(
            WarmupLog.task_id == task_id
        ).order_by(WarmupLog.created_at.desc()).limit(limit)
    )).scalars().all()

    all_logs = list(plan_logs) + list(old_logs)
    all_logs.sort(key=lambda x: x.created_at or datetime.utcnow(), reverse=True)

    acc = (await db.execute(
        select(TelegramAccount).where(TelegramAccount.id == wt.account_id)
    )).scalar_one_or_none()
    phone = acc.phone if acc else "?"

    ACTION_ICONS = {
        "session_start": "▶", "session_end": "⏹",
        "read_feed": "📖", "view_stories": "👁",
        "set_reaction": "😍", "view_profile": "👤",
        "search": "🔍", "send_saved": "💬",
        "forward_saved": "💾", "reply_dm": "↩️",
        "smart_comment": "💬", "join_channel": "📢",
        "typing": "⌨️", "error": "❌",
    }

    return [{
        "id": l.id,
        "type": "warmup",
        "account_phone": phone,
        "action": l.action,
        "action_icon": ACTION_ICONS.get(l.action, "•"),
        "detail": l.detail,
        "channel": l.channel or "",
        "emoji": l.emoji or "",
        "success": l.success,
        "error": l.error,
        "created_at": l.created_at.isoformat() if l.created_at else None,
    } for l in all_logs[:limit]]
@router.post("/batch/{batch_id}/start")
async def start_batch(
    batch_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WarmupTask).where(
            WarmupTask.batch_id == batch_id,
            WarmupTask.user_id == current_user.id,
            WarmupTask.status.in_(["idle", "paused"]),
        )
    )
    tasks = result.scalars().all()
    now = datetime.utcnow()
    for t in tasks:
        t.status = "running"
        if not t.started_at:
            t.started_at = now
        t.next_action_at = now + timedelta(minutes=getattr(t, 'start_offset_min', 0) or 0)
    await db.flush()
    return {"started": len(tasks)}


@router.post("/batch/{batch_id}/stop")
async def stop_batch(
    batch_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WarmupTask).where(
            WarmupTask.batch_id == batch_id,
            WarmupTask.user_id == current_user.id,
            WarmupTask.status == "running",
        )
    )
    tasks = result.scalars().all()
    for t in tasks:
        t.status = "finished"
        t.finished_at = datetime.utcnow()
    await db.flush()
    return {"stopped": len(tasks)}


@router.delete("/batch/{batch_id}", status_code=204)
async def delete_batch(
    batch_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WarmupTask).where(
            WarmupTask.batch_id == batch_id,
            WarmupTask.user_id == current_user.id,
        )
    )
    tasks = result.scalars().all()
    for t in tasks:
        await db.delete(t)
    await db.flush()