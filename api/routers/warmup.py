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
from sqlalchemy import select, func, desc, delete as sa_delete

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
    # Drip-подписка на целевые каналы за время прогрева
    target_channels: list[str] = []      # 30+ каналов на 7 дней — рандомная подписка по дням
    daily_join_min: int = 0              # Минимум подписок в день
    daily_join_max: int = 3              # Максимум подписок в день
    auto_start: bool = True              # Запустить сразу после создания (не кликать старт у каждого)


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
        "started_at": (t.started_at.isoformat() + "Z") if t.started_at else None,
        "finished_at": (t.finished_at.isoformat() + "Z") if getattr(t, 'finished_at', None) else None,
        "created_at": t.created_at.isoformat() + "Z",
        "batch_id": getattr(t, 'batch_id', None) or f"single_{t.id}",
        "batch_name": getattr(t, 'batch_name', None),
        # Drip-подписка
        "target_count": len(getattr(t, 'target_channels', []) or []),
        "subscribed_count": len(getattr(t, 'subscribed_channels', {}) or {}),
        "daily_join_max": getattr(t, 'daily_join_max', 0) or 0,
        "joined_today": getattr(t, 'joined_today', 0) or 0,
    }


async def _activate_warmup_task(t: WarmupTask, db: AsyncSession) -> int:
    """Переводит задачу прогрева в running + генерирует CampaignPlan на все дни.

    Прогрев теперь исполняется ЕДИНЫМ движком plan_executor (как комментинг),
    поэтому план генерируется заранее и точно исполняется. drip-подписки
    на target_channels распределяются по дням как действия 'warmup_subscribe'
    (видны в плане, не сразу — по дням). Родной warmup_v2-движок отключён.
    """
    from tasks.plan_generator import generate_daily_plan
    from tasks.behavior_engine import assign_personality
    from models.campaign_plan import CampaignPlan
    from models.account import TelegramAccount
    from sqlalchemy import delete as sa_delete
    from datetime import date

    now = datetime.utcnow()
    t.status = "running"
    t.started_at = now
    t.day = 1
    t.day_started_at = now
    t.today_actions = 0
    t.joined_today = 0
    t.actions_done = 0
    t.feeds_read = 0
    t.stories_viewed = 0
    t.reactions_set = 0
    t.channels_joined = 0

    offset = getattr(t, 'start_offset_min', 0) or 0
    t.next_action_at = now + timedelta(minutes=offset)

    acc = (await db.execute(
        select(TelegramAccount).where(TelegramAccount.id == t.account_id)
    )).scalar_one_or_none()

    # Чистим старые планы этой задачи
    await db.execute(sa_delete(CampaignPlan).where(CampaignPlan.warmup_task_id == t.id))
    await db.flush()

    total_days = getattr(t, 'total_days', 7) or 7
    personality = assign_personality(str(t.account_id))

    # ── Распределяем target_channels по дням (drip) ──
    # Каждый канал → случайный день (с capacity daily_join_max). Подписки
    # начинаются НЕ с 1-го дня в основном — спред по прогреву.
    targets = [c.lstrip('@').strip() for c in (t.target_channels or []) if c.strip()]
    random.shuffle(targets)
    daily_max = max(0, t.daily_join_max or 0)
    day_subs = {d: [] for d in range(1, total_days + 1)}
    if daily_max > 0:
        for ch in targets:
            days_order = list(range(1, total_days + 1))
            random.shuffle(days_order)
            for d in days_order:
                if len(day_subs[d]) < daily_max:
                    day_subs[d].append(ch)
                    break

    today_limit_set = False
    for day_num in range(1, total_days + 1):
        plan_date = date.today() + timedelta(days=day_num - 1)
        plan = generate_daily_plan(
            account_id=t.account_id,
            phone=acc.phone if acc else str(t.account_id),
            campaign_channels=[], campaign_id=0,
            day_number=day_num, comments_today=0, personality=personality,
        )

        # Вставляем warmup_subscribe действия в сессии этого дня
        subs_today = day_subs.get(day_num, [])
        if subs_today:
            sessions = [s for s in plan.get("sessions", []) if not s.get("skipped") and s.get("actions")]
            for i, ch in enumerate(subs_today):
                if sessions:
                    sess = sessions[i % len(sessions)]
                    pos = random.randint(0, len(sess["actions"]))
                    sess["actions"].insert(pos, {
                        "type": "warmup_subscribe",
                        "channel": ch,
                        "pause_after": random.randint(30, 120),
                    })

        # today_limit для карточки = действия 1-го дня
        if not today_limit_set:
            t.today_limit = sum(len(s.get("actions", [])) for s in plan.get("sessions", [])) or 5
            today_limit_set = True

        db.add(CampaignPlan(
            campaign_id=None, warmup_task_id=t.id,
            account_id=t.account_id, plan_date=plan_date,
            day_number=day_num, plan=plan,
            total_comments=0, executed_idx=0, status="active",
        ))

    await db.flush()
    return offset


def _distribute_warmup_channels(channels: list, account_ids: list, overlap: float = 0.25) -> dict:
    """Распределяет каналы между аккаунтами с небольшим пересечением.

    Зачем: Telegram связывает аккаунты по ОДИНАКОВЫМ подпискам. Если все
    аккаунты подписаны на одни и те же каналы — это палит ферму. Поэтому
    каждый канал идёт ОДНОМУ основному аккаунту (round-robin для баланса),
    и ~overlap доля каналов дублируется ещё на 1 случайный аккаунт (чтобы
    каналы были покрыты комментами от 1-2 аккаунтов, но наборы оставались
    разными).

    Пример: 24 канала, 3 аккаунта, overlap=0.25 → ~8 основных на аккаунт
    + ~2 пересекающихся = ~10/акк, но наборы у всех разные.

    Возвращает {account_id: [channels]}.
    """
    result = {a: [] for a in account_ids}
    if not account_ids or not channels:
        return result
    n = len(account_ids)
    shuffled = list(channels)
    random.shuffle(shuffled)
    for i, ch in enumerate(shuffled):
        primary = account_ids[i % n]
        result[primary].append(ch)
        # Небольшое пересечение: иногда канал получает и 2-й аккаунт
        if n > 1 and random.random() < overlap:
            others = [a for a in account_ids if a != primary]
            second = random.choice(others)
            if ch not in result[second]:
                result[second].append(ch)
    return result


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
    """Создать прогрев для нескольких аккаунтов сразу.

    Каналы РАСПРЕДЕЛЯЮТСЯ между аккаунтами (с небольшим пересечением),
    а НЕ дублируются всем — чтобы Telegram не связал аккаунты по одинаковым
    подпискам. Каждый аккаунт получает свой набор target_channels.
    """
    if not body.account_ids:
        raise HTTPException(status_code=400, detail="Выбери хотя бы один аккаунт")

    import secrets
    batch_id = secrets.token_hex(8)
    batch_name = f"Прогрев {len(body.account_ids)} акк. — {datetime.utcnow().strftime('%d.%m %H:%M')}"

    skipped = []

    # ── Шаг 1: валидируем аккаунты (нужно знать valid set до распределения) ──
    valid = []  # [(acc_id, acc)]
    for acc_id in body.account_ids:
        acc = (await db.execute(
            select(TelegramAccount).where(
                TelegramAccount.id == acc_id,
                TelegramAccount.user_id == current_user.id,
            )
        )).scalar_one_or_none()
        if not acc:
            skipped.append({"id": acc_id, "reason": "Не найден"})
            continue
        existing = (await db.execute(
            select(WarmupTask).where(
                WarmupTask.account_id == acc_id,
                WarmupTask.status.in_(["idle", "running"]),
            )
        )).scalar_one_or_none()
        if existing:
            skipped.append({"id": acc_id, "reason": "Уже есть активный прогрев"})
            continue
        valid.append((acc_id, acc))

    if not valid:
        return {"created": 0, "skipped": len(skipped), "tasks": [],
                "skipped_details": skipped, "message": "Нет аккаунтов для создания"}

    # ── Шаг 2: чистим каналы + распределяем по валидным аккаунтам ──
    clean_targets = []
    seen = set()
    for ch in (body.target_channels or []):
        c = (ch or "").lstrip('@').strip()
        if c and c.lower() not in seen:
            clean_targets.append(c)
            seen.add(c.lower())

    valid_ids = [a for a, _ in valid]
    distribution = _distribute_warmup_channels(clean_targets, valid_ids, overlap=0.25)

    # ── Шаг 3: создаём задачи, каждой — свой набор каналов ──
    created = []
    for i, (acc_id, acc) in enumerate(valid):
        offset = min(random.randint(0, 90) * i, 180)  # разброс старта до 3ч
        my_channels = distribution.get(acc_id, [])

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
            target_channels=my_channels,                 # ← СВОЙ набор, не общий
            daily_join_min=max(0, body.daily_join_min),
            daily_join_max=max(body.daily_join_min, body.daily_join_max),
        )
        db.add(t)
        await db.flush()

        if body.auto_start:
            await _activate_warmup_task(t, db)

        created.append({
            "id": t.id,
            "account_id": acc_id,
            "account_name": acc.first_name or acc.phone,
            "start_offset_min": offset,
            "channels_assigned": len(my_channels),
        })

    started_note = " и запущено" if body.auto_start else ""
    total_unique = len(clean_targets)
    return {
        "created": len(created),
        "skipped": len(skipped),
        "tasks": created,
        "skipped_details": skipped,
        "auto_started": body.auto_start,
        "channels_total": total_unique,
        "message": (
            f"Создано{started_note} {len(created)} задач. "
            + (f"{total_unique} каналов распределены по {len(created)} аккаунтам "
               f"(~{total_unique // max(1, len(created))}/акк с пересечением). " if total_unique else "")
            + (f"Пропущено: {len(skipped)}." if skipped else "")
        ),
    }


# ═══════════════════════════════════════════════════════════
# Drip-подписки: видеть прогресс + экспортировать каналы в кампанию
# ═══════════════════════════════════════════════════════════

@router.get("/tasks/{task_id}/subscribed-channels")
async def get_task_subscribed_channels(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает каналы которыми аккаунт обзавёлся за прогрев.
    Используется чтобы потом импортнуть их в кампанию комментинга со статусом 'joined'."""
    t = (await db.execute(
        select(WarmupTask).where(WarmupTask.id == task_id, WarmupTask.user_id == current_user.id)
    )).scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    return {
        "task_id":           t.id,
        "account_id":        t.account_id,
        "target_channels":   t.target_channels or [],
        "subscribed":        t.subscribed_channels or {},
        "subscribed_count":  len(t.subscribed_channels or {}),
        "remaining_target":  len([c for c in (t.target_channels or [])
                                   if c.lstrip('@').strip() not in (t.subscribed_channels or {})]),
        "joined_today":      t.joined_today or 0,
        "daily_join_max":    t.daily_join_max or 0,
        "day":               t.day,
        "total_days":        t.total_days,
        "status":            t.status,
    }


@router.get("/batches/{batch_id}/subscribed-channels")
async def get_batch_subscribed_channels(
    batch_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает агрегат по batch'у (группа прогрева): какой аккаунт на какие каналы подписан.
    Это форма для импорта в кампанию: {account_id: [ch1, ch2, ...]}."""
    tasks = (await db.execute(
        select(WarmupTask).where(
            WarmupTask.batch_id == batch_id,
            WarmupTask.user_id == current_user.id,
        )
    )).scalars().all()

    if not tasks:
        raise HTTPException(status_code=404, detail="Batch не найден")

    mapping = {}
    all_subscribed = set()
    for t in tasks:
        chs = list((t.subscribed_channels or {}).keys())
        mapping[t.account_id] = chs
        all_subscribed.update(chs)

    return {
        "batch_id":            batch_id,
        "batch_name":          tasks[0].batch_name,
        "total_tasks":         len(tasks),
        "accounts":            mapping,                          # {acc_id: [channels]}
        "unique_channels":     sorted(all_subscribed),           # все уникальные каналы
        "unique_count":        len(all_subscribed),
        "ready_for_campaign":  all(t.status in ("finished", "running") for t in tasks),
    }


@router.get("/tasks/{task_id}/plan")
async def get_warmup_plan(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """План прогрева по дням: что запланировано, что выполнено, что предстоит.

    Прогрев исполняется тем же plan_executor что и комментинг, по этому самому
    CampaignPlan. Поэтому план ТОЧНО совпадает с выполнением:
      - session.done   = сессия уже выполнена (executed_idx)
      - session.is_next = следующая к выполнению
      - иначе          = ещё впереди
    """
    from models.campaign_plan import CampaignPlan
    from datetime import date

    t = (await db.execute(
        select(WarmupTask).where(WarmupTask.id == task_id, WarmupTask.user_id == current_user.id)
    )).scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    plans = (await db.execute(
        select(CampaignPlan)
        .where(CampaignPlan.warmup_task_id == task_id)
        .order_by(CampaignPlan.day_number.asc())
    )).scalars().all()

    ACTION_RU = {
        "read_feed": "📖 Чтение", "set_reaction": "😍 Реакция", "view_stories": "👁 Stories",
        "view_profile": "👤 Профиль", "search": "🔍 Поиск", "send_saved": "💬 Saved",
        "forward_saved": "💾 Пересылка", "reply_dm": "↩️ ЛС", "typing": "⌨️ Печатает",
        "join_channel": "🎭 Decoy", "warmup_subscribe": "📢 Подписка", "idle": "⏸ Пауза",
    }

    today = date.today()
    subscribed = t.subscribed_channels or {}
    targets = t.target_channels or []

    days = []
    for p in plans:
        plan_data = p.plan or {}
        sess_list = plan_data.get("sessions", [])
        is_today = (p.plan_date == today)
        is_past = (p.plan_date < today)
        sessions_out = []
        for si, sess in enumerate(sess_list):
            acts = sess.get("actions", [])
            counts = {}
            for a in acts:
                tp = a.get("type", "?")
                counts[tp] = counts.get(tp, 0) + 1
            # done/next/future
            done = (si < p.executed_idx) or is_past
            is_next = (not is_past) and is_today and (si == p.executed_idx)
            sessions_out.append({
                "session": si + 1,
                "time": f"{sess.get('connect_at_hour', 0):02d}:{sess.get('connect_at_minute', 0):02d}",
                "skipped": sess.get("skipped", False),
                "skip_reason": sess.get("skip_reason"),
                "done": done,
                "is_next": is_next,
                "action_count": len(acts),
                "actions_summary": [{"label": ACTION_RU.get(k, k), "count": v} for k, v in counts.items()],
            })
        days.append({
            "day_number": p.day_number,
            "plan_date": p.plan_date.isoformat() if p.plan_date else None,
            "mood": plan_data.get("mood", "?"),
            "executed_idx": p.executed_idx,
            "total_sessions": len(sess_list),
            "status": p.status,
            "is_today": is_today,
            "is_past": is_past,
            "sessions": sessions_out,
        })

    # ── Подписки: target-каналы со статусом (подписан/ожидает) ──
    subscriptions = []
    for ch in targets:
        c = ch.lstrip('@').strip()
        ts = subscribed.get(c)
        subscriptions.append({"channel": c, "subscribed": ts is not None, "subscribed_at": ts})
    for ch, ts in subscribed.items():
        if ch not in [x.lstrip('@').strip() for x in targets]:
            subscriptions.append({"channel": ch, "subscribed": True, "subscribed_at": ts})

    return {
        "task_id": task_id,
        "account_id": t.account_id,
        "status": t.status,
        "current_day": t.day,
        "total_days": t.total_days,
        "today_actions": t.today_actions or 0,
        "today_limit": t.today_limit or 0,
        "next_action_at": (t.next_action_at.isoformat() + "Z") if t.next_action_at else None,
        "target_channels": targets,
        "subscriptions": subscriptions,
        "subscribed_count": len(subscribed),
        "target_count": len(targets),
        "daily_join_max": t.daily_join_max or 0,
        "days": days,
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

    offset = await _activate_warmup_task(t, db)
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
        now = datetime.utcnow()
        t.status = "running"
        t.started_at = now
        t.day = 1
        t.day_started_at = now
        t.today_actions = 0
        # Сброс общих счётчиков чтобы не мешались со старыми
        t.actions_done = 0
        t.feeds_read = 0
        t.stories_viewed = 0
        t.reactions_set = 0
        t.channels_joined = 0

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
    from sqlalchemy import delete as sa_delete
    from models.campaign_plan import CampaignPlan

    result = await db.execute(
        select(WarmupTask).where(WarmupTask.id == task_id, WarmupTask.user_id == current_user.id)
    )
    t = result.scalar_one_or_none()
    if t:
        # 1. Удаляем связанные планы
        await db.execute(sa_delete(CampaignPlan).where(CampaignPlan.warmup_task_id == t.id))

        # 2. Удаляем логи прогрева
        await db.execute(sa_delete(WarmupLog).where(WarmupLog.task_id == t.id))

        # 3. Удаляем саму задачу
        await db.delete(t)
        await db.flush()


@router.delete("/batches/{batch_id}", status_code=200)
async def delete_warmup_batch(
    batch_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Удаляет ВЕСЬ батч прогрева (все аккаунты) + их планы и логи."""
    from sqlalchemy import delete as sa_delete
    from models.campaign_plan import CampaignPlan

    tasks = (await db.execute(
        select(WarmupTask).where(
            WarmupTask.batch_id == batch_id,
            WarmupTask.user_id == current_user.id,
        )
    )).scalars().all()

    if not tasks:
        raise HTTPException(status_code=404, detail="Batch не найден")

    task_ids = [t.id for t in tasks]
    # 1. Планы
    await db.execute(sa_delete(CampaignPlan).where(CampaignPlan.warmup_task_id.in_(task_ids)))
    # 2. Логи
    await db.execute(sa_delete(WarmupLog).where(WarmupLog.task_id.in_(task_ids)))
    # 3. Сами задачи
    await db.execute(sa_delete(WarmupTask).where(WarmupTask.id.in_(task_ids)))
    await db.flush()

    return {"deleted": len(task_ids), "message": f"Удалён прогрев ({len(task_ids)} аккаунтов)"}


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
        .where(
            WarmupLog.task_id == task_id,
            (WarmupLog.source == 'warmup') | (WarmupLog.source == None),
        )
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
        "created_at": l.created_at.isoformat() + "Z" if l.created_at else None,
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
        .where(
            WarmupLog.task_id.in_(task_ids),
            (WarmupLog.source == 'warmup') | (WarmupLog.source == None),
        )
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
        "created_at": l.created_at.isoformat() + "Z" if l.created_at else None,
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

    # Автогенерация планов если задача running, но планов нет
    if not plans and wt.status == "running":
        from tasks.plan_generator import generate_daily_plan
        from tasks.behavior_engine import assign_personality
        from datetime import date, timedelta

        acc = (await db.execute(
            select(TelegramAccount).where(TelegramAccount.id == wt.account_id)
        )).scalar_one_or_none()

        total_days = getattr(wt, 'total_days', 7) or 7
        personality = assign_personality(str(wt.account_id))

        for day_num in range(1, total_days + 1):
            plan_date = date.today() + timedelta(days=day_num - 1)
            plan_data = generate_daily_plan(
                account_id=wt.account_id,
                phone=acc.phone if acc else str(wt.account_id),
                campaign_channels=[],
                campaign_id=0,
                day_number=day_num,
                comments_today=0,
                personality=personality,
            )
            db.add(CampaignPlan(
                campaign_id=None, warmup_task_id=wt.id,
                account_id=wt.account_id, plan_date=plan_date,
                day_number=day_num, plan=plan_data,
                total_comments=0, executed_idx=0, status="active",
            ))
        await db.flush()
        await db.commit()

        # Перезагружаем
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
    # Только логи warmup для этого аккаунта (исключаем commenting)
    plan_logs = (await db.execute(
        select(WarmupLog).where(
            WarmupLog.account_id == wt.account_id,
            WarmupLog.task_id == None,
            WarmupLog.created_at >= started_at,
            # Только warmup-логи (NULL = старые записи до миграции, считаем warmup)
            (WarmupLog.source == 'warmup') | (WarmupLog.source == None),
        ).order_by(WarmupLog.created_at.desc()).limit(limit)
    )).scalars().all()

    # Старые логи от warmup_v2 (по task_id) — тоже с фильтром
    old_logs = (await db.execute(
        select(WarmupLog).where(
            WarmupLog.task_id == task_id,
            (WarmupLog.source == 'warmup') | (WarmupLog.source == None),
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
        "created_at": l.created_at.isoformat() + "Z" if l.created_at else None,
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