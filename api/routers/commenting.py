"""
GramGPT API — routers/commenting.py
Нейрокомментинг: CRUD кампаний, целевые каналы, старт/стоп.
По ТЗ раздел 3.6.
"""

from datetime import datetime
import random
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.campaign import (
    Campaign, TargetChannel, CampaignStatus,
    TriggerMode, LLMProvider, CommentTone,
)

router = APIRouter(prefix="/commenting", tags=["commenting"])


# ── Schemas ──────────────────────────────────────────────────

class CampaignCreate(BaseModel):
    name: str
    account_ids: list[int] = []
    trigger_mode: str = "all"
    trigger_percent: int = 50
    trigger_keywords: list[str] = []
    llm_provider: str = "claude"
    tone: str = "positive"
    custom_prompt: str = ""
    comment_length: str = "medium"
    max_comments: int = 100
    max_hours: int = 24
    delay_join: int = 10
    delay_comment: int = 250
    delay_between: int = 60


class CampaignUpdate(BaseModel):
    name: Optional[str] = None
    account_ids: Optional[list[int]] = None
    trigger_mode: Optional[str] = None
    trigger_percent: Optional[int] = None
    trigger_keywords: Optional[list[str]] = None
    llm_provider: Optional[str] = None
    tone: Optional[str] = None
    custom_prompt: Optional[str] = None
    comment_length: Optional[str] = None
    max_comments: Optional[int] = None
    max_hours: Optional[int] = None
    delay_join: Optional[int] = None
    delay_comment: Optional[int] = None
    delay_between: Optional[int] = None


class AddChannelsRequest(BaseModel):
    channels: list[str]   # Список @username или https://t.me/... ссылок


class ChannelOut(BaseModel):
    id: int
    username: str
    title: str
    link: str
    subscribers: int
    has_comments: bool
    last_post_id: int
    comments_sent: int
    is_active: bool

    model_config = {"from_attributes": True}


# ── Helpers ──────────────────────────────────────────────────

async def _get_campaign(db, campaign_id: int, user_id: int) -> Campaign:
    result = await db.execute(
        select(Campaign).where(Campaign.id == campaign_id, Campaign.user_id == user_id)
    )
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    return c


def _val(x):
    """Безопасно достаёт .value из enum или возвращает строку как есть"""
    return x.value if hasattr(x, 'value') else x


def _campaign_to_dict(c: Campaign, channels: list = None) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "status": _val(c.status),
        "account_ids": c.account_ids or [],
        "trigger_mode": _val(c.trigger_mode),
        "trigger_percent": c.trigger_percent,
        "trigger_keywords": c.trigger_keywords or [],
        "llm_provider": _val(c.llm_provider),
        "tone": _val(c.tone),
        "custom_prompt": c.custom_prompt,
        "comment_length": c.comment_length,
        "max_comments": c.max_comments,
        "max_hours": c.max_hours,
        "comments_sent": c.comments_sent,
        "delay_join": c.delay_join,
        "delay_comment": c.delay_comment,
        "delay_between": c.delay_between,
        "started_at": c.started_at.isoformat() if c.started_at else None,
        "finished_at": c.finished_at.isoformat() if c.finished_at else None,
        "created_at": c.created_at.isoformat(),
        "channels": [
            {
                "id": ch.id, "username": ch.username, "title": ch.title,
                "link": ch.link, "subscribers": ch.subscribers,
                "has_comments": ch.has_comments, "comments_sent": ch.comments_sent,
                "is_active": ch.is_active, "last_post_id": ch.last_post_id,
            }
            for ch in (channels or [])
        ],
        "channels_count": len(channels) if channels else 0,
    }


# ── CRUD Campaigns ───────────────────────────────────────────

@router.get("/campaigns")
async def list_campaigns(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список всех кампаний пользователя"""
    result = await db.execute(
        select(Campaign).where(Campaign.user_id == current_user.id).order_by(Campaign.created_at.desc())
    )
    campaigns = result.scalars().all()

    out = []
    for c in campaigns:
        ch_result = await db.execute(
            select(TargetChannel).where(TargetChannel.campaign_id == c.id)
        )
        channels = ch_result.scalars().all()
        out.append(_campaign_to_dict(c, channels))

    return out


@router.get("/campaigns/{campaign_id}")
async def get_campaign(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Детали кампании"""
    c = await _get_campaign(db, campaign_id, current_user.id)
    ch_result = await db.execute(
        select(TargetChannel).where(TargetChannel.campaign_id == c.id)
    )
    channels = ch_result.scalars().all()
    return _campaign_to_dict(c, channels)


@router.post("/campaigns")
async def create_campaign(
    body: CampaignCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Создать новую кампанию"""
    c = Campaign(
        user_id=current_user.id,
        name=body.name,
        account_ids=body.account_ids,
        trigger_mode=body.trigger_mode,
        trigger_percent=body.trigger_percent,
        trigger_keywords=body.trigger_keywords,
        llm_provider=body.llm_provider,
        tone=body.tone,
        custom_prompt=body.custom_prompt,
        comment_length=body.comment_length,
        max_comments=body.max_comments,
        max_hours=body.max_hours,
        delay_join=body.delay_join,
        delay_comment=body.delay_comment,
        delay_between=body.delay_between,
    )
    db.add(c)
    await db.flush()
    return _campaign_to_dict(c, [])


@router.patch("/campaigns/{campaign_id}")
async def update_campaign(
    campaign_id: int,
    body: CampaignUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Обновить настройки кампании"""
    c = await _get_campaign(db, campaign_id, current_user.id)

    update_data = body.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(c, key, value)
    c.updated_at = datetime.utcnow()
    await db.flush()

    ch_result = await db.execute(select(TargetChannel).where(TargetChannel.campaign_id == c.id))
    return _campaign_to_dict(c, ch_result.scalars().all())


@router.delete("/campaigns/{campaign_id}", status_code=204)
async def delete_campaign(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Удалить кампанию"""
    c = await _get_campaign(db, campaign_id, current_user.id)
    await db.delete(c)
    await db.flush()


# ── Start / Stop / Pause ─────────────────────────────────────

@router.post("/campaigns/{campaign_id}/start")
async def start_campaign(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запустить кампанию + автоматически включить прогрев для аккаунтов"""
    c = await _get_campaign(db, campaign_id, current_user.id)

    # Проверяем есть ли каналы
    ch_result = await db.execute(
        select(TargetChannel).where(TargetChannel.campaign_id == c.id, TargetChannel.is_active == True)
    )
    channels = ch_result.scalars().all()
    if not channels:
        raise HTTPException(status_code=400, detail="Добавьте целевые каналы перед запуском")

    if not c.account_ids:
        raise HTTPException(status_code=400, detail="Выберите аккаунты для комментинга")

    # ── Автоматический прогрев ───────────────────────────
    from models.warmup import WarmupTask

    warmup_created = 0
    warmup_existing = 0

    for acc_id in c.account_ids:
        # Проверяем есть ли уже активный прогрев
        existing = await db.execute(
            select(WarmupTask).where(
                WarmupTask.account_id == acc_id,
                WarmupTask.status.in_(["active", "paused"]),
            )
        )
        if existing.scalar_one_or_none():
            warmup_existing += 1
            continue

        # Создаём прогрев
        import random
        warmup = WarmupTask(
            user_id=current_user.id,
            account_id=acc_id,
            mode="normal",
            status="running",
            total_days=30,  # Прогрев на весь период кампании
            day=1,
            today_actions=0,
            today_limit=random.randint(15, 25),
            start_offset_min=random.randint(0, 90),
            campaign_id=c.id,
            day_started_at=datetime.utcnow(),
            next_action_at=datetime.utcnow(),
        )
        db.add(warmup)
        warmup_created += 1

    c.status = CampaignStatus.active
    c.started_at = datetime.utcnow()
    c.finished_at = None
    await db.flush()

    msg = "Кампания запущена"
    if warmup_created > 0:
        msg += f", прогрев включён для {warmup_created} аккаунтов"
    if warmup_existing > 0:
        msg += f" ({warmup_existing} уже прогреваются)"

    return {"success": True, "status": "active", "message": msg}

@router.post("/campaigns/{campaign_id}/pause")
async def pause_campaign(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Поставить на паузу"""
    c = await _get_campaign(db, campaign_id, current_user.id)
    c.status = CampaignStatus.paused
    await db.flush()
    return {"success": True, "status": "paused"}


@router.post("/campaigns/{campaign_id}/stop")
async def stop_campaign(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Остановить кампанию"""
    c = await _get_campaign(db, campaign_id, current_user.id)
    c.status = CampaignStatus.stopped
    c.finished_at = datetime.utcnow()
    await db.flush()
    return {"success": True, "status": "stopped"}


# ── Target Channels ──────────────────────────────────────────

@router.post("/campaigns/{campaign_id}/channels")
async def add_channels(
    campaign_id: int,
    body: AddChannelsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Добавить целевые каналы в кампанию"""
    c = await _get_campaign(db, campaign_id, current_user.id)

    added = 0
    for raw in body.channels:
        link = raw.strip()
        if not link:
            continue

        # Нормализуем
        username = ""
        if link.startswith("@"):
            username = link[1:]
            link = f"https://t.me/{username}"
        elif "t.me/" in link:
            username = link.split("t.me/")[-1].split("/")[0].replace("@", "")
        else:
            username = link.replace("@", "")
            link = f"https://t.me/{username}"

        # Проверяем дубликат
        existing = await db.execute(
            select(TargetChannel).where(
                TargetChannel.campaign_id == c.id,
                TargetChannel.username == username,
            )
        )
        if existing.scalar_one_or_none():
            continue

        ch = TargetChannel(
            campaign_id=c.id,
            username=username,
            link=link,
            title=username,
        )
        db.add(ch)
        added += 1

    await db.flush()
    return {"added": added, "message": f"Добавлено {added} каналов"}


@router.delete("/campaigns/{campaign_id}/channels/{channel_id}", status_code=204)
async def remove_channel(
    campaign_id: int,
    channel_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Удалить канал из кампании"""
    await _get_campaign(db, campaign_id, current_user.id)
    result = await db.execute(
        select(TargetChannel).where(TargetChannel.id == channel_id, TargetChannel.campaign_id == campaign_id)
    )
    ch = result.scalar_one_or_none()
    if ch:
        await db.delete(ch)
        await db.flush()


@router.get("/campaigns/{campaign_id}/stats")
async def campaign_stats(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Статистика кампании"""
    c = await _get_campaign(db, campaign_id, current_user.id)
    ch_result = await db.execute(
        select(TargetChannel).where(TargetChannel.campaign_id == c.id)
    )
    channels = ch_result.scalars().all()

    runtime_hours = 0
    if c.started_at:
        end = c.finished_at or datetime.utcnow()
        runtime_hours = round((end - c.started_at).total_seconds() / 3600, 1)

    return {
        "campaign_id": c.id,
        "status": c.status.value,
        "comments_sent": c.comments_sent,
        "max_comments": c.max_comments,
        "progress_pct": min(100, round(c.comments_sent / max(c.max_comments, 1) * 100)),
        "runtime_hours": runtime_hours,
        "max_hours": c.max_hours,
        "channels_total": len(channels),
        "channels_active": sum(1 for ch in channels if ch.is_active),
        "per_channel": [
            {"username": ch.username, "comments": ch.comments_sent}
            for ch in channels
        ],
    }


# ── Лог комментариев ────────────────────────────────────────

@router.get("/logs")
async def get_comment_logs(
    campaign_id: int = None,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """История всех комментариев. Можно фильтровать по campaign_id."""
    from models.campaign import CommentLog

    query = select(CommentLog).join(Campaign).where(Campaign.user_id == current_user.id)
    if campaign_id:
        query = query.where(CommentLog.campaign_id == campaign_id)
    query = query.order_by(CommentLog.created_at.desc()).limit(limit)

    result = await db.execute(query)
    logs = result.scalars().all()

    return [{
        "id": l.id,
        "campaign_id": l.campaign_id,
        "account_phone": l.account_phone,
        "channel_username": l.channel_username,
        "channel_title": l.channel_title,
        "post_id": l.post_id,
        "post_text": l.post_text[:200],
        "comment_text": l.comment_text,
        "llm_provider": l.llm_provider,
        "created_at": l.created_at.isoformat(),
    } for l in logs]


@router.get("/campaigns/{campaign_id}/activity")
@router.get("/campaigns/{campaign_id}/activity")
async def get_campaign_activity(
    campaign_id: int,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Вся активность кампании: прогрев + комментарии.
    Показывает что делали аккаунты: чтение, реакции, typing, комменты.
    """
    c = await _get_campaign(db, campaign_id, current_user.id)

    from models.comment_queue import CommentQueue
    from models.warmup_log import WarmupLog
    from models.warmup import WarmupTask
    from models.account import TelegramAccount

    # ── 1. Комментарии из очереди ────────────────────────
    cq_result = await db.execute(
        select(CommentQueue)
        .where(CommentQueue.campaign_id == c.id)
        .order_by(CommentQueue.created_at.desc())
        .limit(limit)
    )
    comments = cq_result.scalars().all()

    # ── 2. Логи прогрева привязанные к кампании ──────────
    warmup_result = await db.execute(
        select(WarmupLog)
        .join(WarmupTask, WarmupLog.task_id == WarmupTask.id)
        .where(WarmupTask.campaign_id == c.id)
        .order_by(WarmupLog.created_at.desc())
        .limit(limit)
    )
    warmup_logs = warmup_result.scalars().all()

    # ── 3. Подгружаем телефоны аккаунтов ─────────────────
    all_acc_ids = list(set(
        [q.account_id for q in comments] +
        [w.account_id for w in warmup_logs]
    ))
    acc_map = {}
    if all_acc_ids:
        accs_r = await db.execute(
            select(TelegramAccount).where(TelegramAccount.id.in_(all_acc_ids))
        )
        for a in accs_r.scalars().all():
            acc_map[a.id] = {"phone": a.phone, "name": a.first_name or a.phone}

    # ── 4. Собираем единый список ────────────────────────
    out = []

    # Комментарии
    for q in comments:
        acc_info = acc_map.get(q.account_id, {"phone": "?", "name": "?"})
        steps = []
        personality_name = ""
        style_name = ""
        if q.personality:
            steps = q.personality.get("_steps", [])
            personality_name = q.personality.get("name", "")
        if q.style:
            style_name = q.style.get("name", "")

        out.append({
            "id": f"c_{q.id}",
            "type": "comment",
            "account_id": q.account_id,
            "account_phone": acc_info["phone"],
            "account_name": acc_info["name"],
            "channel": q.channel,
            "post_id": q.post_id,
            "post_text": (q.post_text or "")[:200],
            "status": q.status,
            "comment_text": q.comment_text,
            "error": q.error,
            "personality": personality_name,
            "style": style_name,
            "steps": steps,
            "scheduled_at": q.scheduled_at.isoformat() + "Z" if q.scheduled_at else None,
            "executed_at": q.executed_at.isoformat() + "Z" if q.executed_at else None,
            "created_at": q.created_at.isoformat() + "Z",
            "sort_time": (q.executed_at or q.created_at).isoformat(),
        })

    # Warmup логи
    for w in warmup_logs:
        acc_info = acc_map.get(w.account_id, {"phone": "?", "name": "?"})

        # Иконка по типу действия
        action_icons = {
            "read_feed": "📖", "set_reaction": "😍", "view_stories": "👁",
            "view_profile": "👤", "typing": "⌨️", "search": "🔍",
            "join_channel": "📢", "forward_saved": "💾", "send_saved": "💬",
            "reply_dm": "↩️", "session_start": "▶", "session_end": "⏹",
            "smart_comment": "💬", "new_day": "🌅", "rest_day": "😴",
            "error": "❌",
        }
        icon = action_icons.get(w.action, "•")

        out.append({
            "id": f"w_{w.id}",
            "type": "warmup",
            "account_id": w.account_id,
            "account_phone": acc_info["phone"],
            "account_name": acc_info["name"],
            "channel": w.channel or "",
            "action": w.action,
            "action_icon": icon,
            "detail": w.detail or "",
            "success": w.success,
            "error": w.error,
            "created_at": w.created_at.isoformat() + "Z" if w.created_at else None,
            "sort_time": w.created_at.isoformat() if w.created_at else "",
        })

    # Сортируем по времени (новые сверху)
    out.sort(key=lambda x: x.get("sort_time", ""), reverse=True)

    return out[:limit]


# ── Comment Queue (v2) ─────────────────────────────────────

@router.get("/queue")
async def get_comment_queue(
    status: str = None,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Очередь комментариев. Фильтр по status: scheduled/executing/done/failed/aborted."""
    from models.comment_queue import CommentQueue

    query = (
        select(CommentQueue)
        .join(Campaign, CommentQueue.campaign_id == Campaign.id)
        .where(Campaign.user_id == current_user.id)
    )
    if status:
        query = query.where(CommentQueue.status == status)
    query = query.order_by(CommentQueue.scheduled_at.desc()).limit(limit)

    result = await db.execute(query)
    items = result.scalars().all()

    return [{
        "id": q.id,
        "campaign_id": q.campaign_id,
        "account_id": q.account_id,
        "channel": q.channel,
        "post_id": q.post_id,
        "post_text": (q.post_text or "")[:200],
        "personality": q.personality,
        "style": q.style,
        "status": q.status,
        "scheduled_at": q.scheduled_at.isoformat() if q.scheduled_at else None,
        "executed_at": q.executed_at.isoformat() if q.executed_at else None,
        "comment_text": q.comment_text,
        "error": q.error,
        "created_at": q.created_at.isoformat() if q.created_at else None,
    } for q in items]


@router.get("/queue/stats")
async def get_queue_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Статистика очереди комментариев."""
    from sqlalchemy import func
    from models.comment_queue import CommentQueue

    base = (
        select(CommentQueue.status, func.count(CommentQueue.id).label("cnt"))
        .join(Campaign, CommentQueue.campaign_id == Campaign.id)
        .where(Campaign.user_id == current_user.id)
        .group_by(CommentQueue.status)
    )
    result = await db.execute(base)
    rows = result.all()

    stats = {row.status: row.cnt for row in rows}
    return {
        "total": sum(stats.values()),
        "scheduled": stats.get("scheduled", 0),
        "executing": stats.get("executing", 0),
        "done": stats.get("done", 0),
        "failed": stats.get("failed", 0),
        "aborted": stats.get("aborted", 0),
    }


# ── Account Behavior (v2) ──────────────────────────────────

@router.get("/behavior")
async def get_account_behaviors(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Профили поведения аккаунтов."""
    from models.account_behavior import AccountBehavior
    from models.account import TelegramAccount

    result = await db.execute(
        select(AccountBehavior)
        .join(TelegramAccount, AccountBehavior.account_id == TelegramAccount.id)
        .where(TelegramAccount.user_id == current_user.id)
    )
    behaviors = result.scalars().all()

    # Подгружаем phone для отображения
    out = []
    for b in behaviors:
        acc_r = await db.execute(select(TelegramAccount).where(TelegramAccount.id == b.account_id))
        acc = acc_r.scalar_one_or_none()
        out.append({
            "id": b.id,
            "account_id": b.account_id,
            "phone": acc.phone if acc else "",
            "personality": b.personality,
            "timing_profile": b.timing_profile,
            "style_profile": b.style_profile,
            "comments_today": b.comments_today,
            "last_comment_at": b.last_comment_at.isoformat() if b.last_comment_at else None,
            "channels_commented_today": b.channels_commented_today or [],
            "created_at": b.created_at.isoformat() if b.created_at else None,
        })

    return out