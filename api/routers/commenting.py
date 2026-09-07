"""
GramGPT API — routers/commenting.py
Нейрокомментинг: CRUD кампаний, целевые каналы, старт/стоп.
По ТЗ раздел 3.6.
"""

from datetime import datetime, timedelta
import os
import random
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from routers.deps import get_current_user
from models.user import User
from models.campaign import (
    Campaign, TargetChannel, CampaignStatus,
    TriggerMode, LLMProvider, CommentTone,
)

router = APIRouter(prefix="/commenting", tags=["commenting"])

# Доля пар (аккаунт, канал), уже покрытых существующими подписками, начиная
# с которой аккаунты считаются прогретыми и комменты раскладываются по дням
# РОВНО, а не прогревной волной. См. _do_start_campaign.
WARM_REUSE_PCT = float(os.getenv("CAMPAIGN_WARM_REUSE_PCT", "50"))

# Порог отсева каналов, где удаляют написанное (см. _do_start_campaign).
# Нужно НЕ МЕНЬШЕ стольких удалений и такой доли от всех попыток.
BANSTAT_MIN_DELETES = int(os.getenv("BANSTAT_MIN_DELETES", "2"))
BANSTAT_MIN_RATIO = float(os.getenv("BANSTAT_MIN_RATIO", "0.5"))

# Сколько подписок должно уже быть у типичного аккаунта, чтобы считать парк
# прогретым независимо от доли переиспользования в текущей раскладке.
WARM_MIN_SUBS = int(os.getenv("CAMPAIGN_WARM_MIN_SUBS", "15"))


# ── Channel distribution ──────────────────────────────────────

def _distribute_channels(channels: list, account_ids: list,
                         existing_subs: dict = None) -> dict:
    """
    Round-robin с учётом существующих подписок.

    Приоритет: канал X отдаётся акку который УЖЕ на него подписан
    (из existing_subs). Так экономим join_target_channel действия.

    Если несколько акков подписаны на X — отдаём тому у которого меньше
    каналов сейчас (балансировка). Каналы без подписчиков — round-robin.

    30 каналов / 5 аккаунтов → 6 уникальных каналов на аккаунт.

    existing_subs: {acc_id: set(channel_username_lower)} из AccountSubscription.
    """
    if not channels or not account_ids:
        return {acc_id: [] for acc_id in account_ids}
    existing_subs = existing_subs or {}
    assignments: dict = {acc_id: [] for acc_id in account_ids}

    target_per_acc = (len(channels) + len(account_ids) - 1) // len(account_ids)

    unassigned = []
    for ch in channels:
        ch_l = ch.lower()
        candidates = [
            aid for aid in account_ids
            if ch_l in existing_subs.get(aid, set())
            and len(assignments[aid]) < target_per_acc
        ]
        if candidates:
            candidates.sort(key=lambda a: len(assignments[a]))
            assignments[candidates[0]].append(ch)
        else:
            unassigned.append(ch)

    random.shuffle(unassigned)
    for ch in unassigned:
        acc = min(account_ids, key=lambda a: len(assignments[a]))
        assignments[acc].append(ch)

    return assignments




# ── Schemas ──────────────────────────────────────────────────

class CampaignCreate(BaseModel):
    name: str
    account_ids: list[int] = []
    trigger_mode: str = "all"
    trigger_percent: int = 50
    trigger_keywords: list[str] = []
    llm_provider: str = "claude"
    llm_credential_id: Optional[int] = None     # конкретный ключ; None = default провайдера
    tone: str = "positive"
    custom_prompt: str = ""
    comment_length: str = "medium"
    max_comments: int = 100
    max_hours: int = 24
    delay_join: int = 10
    delay_comment: int = 250
    delay_between: int = 60
    # ── Тип и уведомления (миграция 030) ─────────────────────
    # 'regular'    — обычный комментинг (verify авто в каждую 2-ю сессию)
    # 'check_only' — screening: 1 коммент/канал, verify в каждой сессии
    campaign_type: str = "regular"
    notification_chat: Optional[str] = None


class CampaignUpdate(BaseModel):
    name: Optional[str] = None
    account_ids: Optional[list[int]] = None
    trigger_mode: Optional[str] = None
    trigger_percent: Optional[int] = None
    trigger_keywords: Optional[list[str]] = None
    llm_provider: Optional[str] = None
    llm_credential_id: Optional[int] = None
    tone: Optional[str] = None
    custom_prompt: Optional[str] = None
    comment_length: Optional[str] = None
    max_comments: Optional[int] = None
    max_hours: Optional[int] = None
    delay_join: Optional[int] = None
    delay_comment: Optional[int] = None
    delay_between: Optional[int] = None
    campaign_type: Optional[str] = None
    notification_chat: Optional[str] = None


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


def _campaign_to_dict(c: Campaign, channels: list = None,
                      channels_count: int = None) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "status": _val(c.status),
        "account_ids": c.account_ids or [],
        "trigger_mode": _val(c.trigger_mode),
        "trigger_percent": c.trigger_percent,
        "trigger_keywords": c.trigger_keywords or [],
        "llm_provider": _val(c.llm_provider),
        "llm_credential_id": c.llm_credential_id,
        "warmup_batch_id": c.warmup_batch_id,
        "scheduled_start_at": (c.scheduled_start_at.isoformat() + "Z") if c.scheduled_start_at else None,
        "tone": _val(c.tone),
        "custom_prompt": c.custom_prompt,
        "comment_length": c.comment_length,
        "max_comments": c.max_comments,
        "max_hours": c.max_hours,
        "comments_sent": c.comments_sent,
        "delay_join": c.delay_join,
        "delay_comment": c.delay_comment,
        "delay_between": c.delay_between,
        "campaign_type": getattr(c, "campaign_type", "regular") or "regular",
        "notification_chat": getattr(c, "notification_chat", None),
        "started_at": (c.started_at.isoformat() + "Z") if c.started_at else None,
        "finished_at": (c.finished_at.isoformat() + "Z") if c.finished_at else None,
        "created_at": c.created_at.isoformat() + "Z",
        "channels": [
            {
                "id": ch.id, "username": ch.username, "title": ch.title,
                "link": ch.link, "subscribers": ch.subscribers,
                "has_comments": ch.has_comments, "comments_sent": ch.comments_sent,
                "is_active": ch.is_active, "last_post_id": ch.last_post_id,
            }
            for ch in (channels or [])
        ],
        # Счётчик приходит отдельно, когда список каналов не грузили:
        # в перечне кампаний он нужен, а сами каналы — нет.
        "channels_count": (channels_count if channels_count is not None
                           else (len(channels) if channels else 0)),
    }


# ── CRUD Campaigns ───────────────────────────────────────────

@router.get("/campaigns")
async def list_campaigns(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список всех кампаний пользователя — без списков каналов.

    Раньше на каждую кампанию догружались ВСЕ её целевые каналы и
    уезжали в ответ. У активной кампании их тысячи, и перечень весил
    2.7 МБ, который браузер разбирал заново каждые тридцать секунд —
    из-за этого вкладка и открывалась долго. В самом перечне каналы не
    показываются, нужен только их счёт; полный список отдаёт
    GET /campaigns/{id}, когда открывают карточку кампании.
    """
    result = await db.execute(
        select(Campaign).where(Campaign.user_id == current_user.id).order_by(Campaign.created_at.desc())
    )
    campaigns = result.scalars().all()

    # Один групповой счёт вместо запроса на каждую кампанию.
    counts = {}
    if campaigns:
        cnt_r = await db.execute(
            select(TargetChannel.campaign_id, func.count())
            .where(TargetChannel.campaign_id.in_([c.id for c in campaigns]))
            .group_by(TargetChannel.campaign_id)
        )
        counts = {cid: n for cid, n in cnt_r.all()}

    return [_campaign_to_dict(c, None, counts.get(c.id, 0)) for c in campaigns]


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
    # check_only: max_hours/max_comments ЗАДАЮТСЯ ПРИ СТАРТЕ кампании
    # (в _do_start_campaign) — на основе фактического кол-ва каналов и
    # аккаунтов. При создании ещё каналов нет, ставим 0-заглушки.
    _ct = (body.campaign_type or "regular").lower()
    if _ct not in ("regular", "check_only"):
        _ct = "regular"
    _max_hours = body.max_hours if _ct != "check_only" else 24
    _max_comments = body.max_comments if _ct != "check_only" else 1
    c = Campaign(
        user_id=current_user.id,
        name=body.name,
        account_ids=body.account_ids,
        trigger_mode=body.trigger_mode,
        trigger_percent=body.trigger_percent,
        trigger_keywords=body.trigger_keywords,
        llm_provider=body.llm_provider,
        llm_credential_id=body.llm_credential_id,
        tone=body.tone,
        custom_prompt=body.custom_prompt,
        comment_length=body.comment_length,
        max_comments=_max_comments,
        max_hours=_max_hours,
        delay_join=body.delay_join,
        delay_comment=body.delay_comment,
        delay_between=body.delay_between,
        campaign_type=_ct,
        notification_chat=body.notification_chat,
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

async def _do_start_campaign(db: AsyncSession, c: Campaign) -> dict:
    """
    Общая логика запуска кампании: генерация channel_assignments + campaign_plans.
    Вызывается из /start endpoint И из dispatch_plans auto-start блока.
    Кидает HTTPException — вызывающий должен сам решать что делать (endpoint
    отдаст 400, dispatch — пропустит и оставит scheduled).
    """
    ch_result = await db.execute(
        select(TargetChannel).where(TargetChannel.campaign_id == c.id, TargetChannel.is_active == True)
    )
    channels = ch_result.scalars().all()
    if not channels:
        raise HTTPException(status_code=400, detail="Добавьте целевые каналы перед запуском")
    if not c.account_ids:
        raise HTTPException(status_code=400, detail="Выберите аккаунты для комментинга")

    channel_usernames = [ch.username for ch in channels if ch.username]

    # ── Отсев непригодных каналов ДО генерации планов (2026-08-22) ──
    # Кэш свойств проверялся только в момент исполнения: канал успевал
    # занять слот комментария в плане, аккаунт до него доходил и лишь там
    # пропускал — слот сгорал впустую. Проверяем заранее: запрос в Telegram
    # тут не идёт, это чтение таблицы channel_capabilities.
    try:
        from models.channel_capability import ChannelCapability
        _bad_rows = (await db.execute(
            select(ChannelCapability.channel_username, ChannelCapability.reason)
            .where(
                ChannelCapability.channel_username.in_(channel_usernames),
                ChannelCapability.commentable == False,     # noqa: E712
            )
        )).all()
        _bad = {r[0]: r[1] for r in _bad_rows}
        if _bad:
            _before = len(channel_usernames)
            channel_usernames = [u for u in channel_usernames if u not in _bad]
            from collections import Counter as _C
            _by = _C(_bad.values())
            print(f"[start_campaign] отсеяно непригодных каналов: {_before - len(channel_usernames)} "
                  f"из {_before} ({dict(_by)})")
        if not channel_usernames:
            raise HTTPException(
                status_code=400,
                detail="Все выбранные каналы непригодны для комментирования "
                       "(нет обсуждений / вступление по заявке / писать запрещено)",
            )
    except HTTPException:
        raise
    except Exception as _ce:
        print(f"[start_campaign] фильтр по channel_capabilities пропущен: {_ce}")

    # ── Отсев каналов, где комменты систематически удаляют ───────────────
    # channel_capabilities отвечает на вопрос «пустят ли писать», а этот
    # фильтр — на вопрос «останется ли написанное». Разные вещи: канал
    # может принимать коммент и через минуту его сносить, и до сих пор
    # такие каналы получали слоты снова и снова.
    # Статистику копит verify_my_comments в ChannelBanStats. Порог
    # намеренно строгий: одно удаление бывает случайным (модератор чистил
    # ветку), поэтому нужно не меньше двух и не меньше половины попыток.
    try:
        from models.channel_ban_stats import ChannelBanStats as _CBS_f
        _rows_f = (await db.execute(
            select(_CBS_f.channel_username, _CBS_f.banned_count, _CBS_f.total_attempts)
            .where(
                _CBS_f.user_id == c.user_id,
                _CBS_f.channel_username.in_(channel_usernames),
                _CBS_f.banned_count >= BANSTAT_MIN_DELETES,
            )
        )).all()
        _bad_f = {
            r[0]: (r[1], r[2]) for r in _rows_f
            if r[2] and (r[1] / r[2]) >= BANSTAT_MIN_RATIO
        }
        if _bad_f:
            _before_f = len(channel_usernames)
            channel_usernames = [u for u in channel_usernames if u not in _bad_f]
            print(f"[start_campaign][camp {c.id}] отсеяно по удалению комментов: "
                  f"{_before_f - len(channel_usernames)} из {_before_f}")
            for _u, (_b, _t) in sorted(_bad_f.items(), key=lambda x: -x[1][0])[:8]:
                print(f"    @{_u}: удалено {_b} из {_t}")
        if not channel_usernames:
            raise HTTPException(
                status_code=400,
                detail="Все каналы отсеяны: в них систематически удаляют комментарии",
            )
    except HTTPException:
        raise
    except Exception as _be:
        print(f"[start_campaign] фильтр по удалениям пропущен: {_be}")

    # ── Генерация планов ─────────────────────────────────
    from models.campaign_plan import CampaignPlan
    from models.account import TelegramAccount
    from tasks.plan_generator import (
        distribute_comments_by_days,
        distribute_day_comments,
        generate_daily_plan,
    )
    from tasks.behavior_engine import assign_personality, assign_timing_profile, assign_style_profile

    import math
    # ── check_only: пересчёт max_comments/max_hours ПО ФАКТУ ──
    # На create ставили 1/24, поскольку каналов ещё не было. Сейчас
    # знаем сколько каналов и аккаунтов — считаем реалистично.
    _is_check_only = getattr(c, "campaign_type", "regular") == "check_only"
    if _is_check_only:
        n_channels = len(channel_usernames)
        n_accs = max(1, len(c.account_ids or []))
        # Средний per-acc-daily-target ≈ 4-5. Берём 5.
        _avg_daily = 5
        # Нужно X дней чтобы распределить каналы: ceil(N / (accs * avg))
        _needed_days = max(1, math.ceil(n_channels / (n_accs * _avg_daily)))
        # Cap = 14 дней (screening не должен растягиваться).
        # +1 день НЕ добавляем — вместо этого добавляем одну tail-verify
        # СЕССИЮ через ~40 мин после последней рабочей сессии последнего дня
        # (см. flag add_tail_verify_session в plan_generator).
        total_days = min(14, _needed_days)
        c.max_comments = n_channels     # ровно 1 коммент на канал
        # check_only: НЕ ставим max_hours — не должно быть time limit
        # (user настоял 2026-07-14, screening без таймера)
        c.max_hours = 0
        print(f"[start_campaign][check_only] channels={n_channels}, accs={n_accs} → "
              f"total_days={total_days} (+ 1 tail-verify session on last day), "
              f"max_comments={c.max_comments}, БЕЗ time-limit")
    else:
        if c.max_hours and c.max_hours < 24:
            total_days = 1
        elif c.max_hours:
            total_days = max(1, math.ceil(c.max_hours / 24))
        else:
            total_days = 7
        # НЕ добавляем +1 день, добавим одну tail-сессию через ~40 мин.
        print(f"[start_campaign] max_hours={c.max_hours}, total_days={total_days} "
              f"(+ 1 tail-verify session on last day)")

    # Загружаем аккаунты и их personality
    accounts_data = []
    _acc_proxy_host = {}      # account_id → host прокси (для группировки по IP)
    for acc_id in c.account_ids:
        acc_r = await db.execute(
            select(TelegramAccount).where(TelegramAccount.id == acc_id)
        )
        acc = acc_r.scalar_one_or_none()
        if acc and acc.phone:
            p = assign_personality(acc.phone)
            accounts_data.append((acc.id, acc.phone, p))
            if acc.proxy_id:
                from models.proxy import Proxy as _Px
                _px = (await db.execute(select(_Px).where(_Px.id == acc.proxy_id))).scalar_one_or_none()
                if _px:
                    _acc_proxy_host[acc.id] = _px.host

    if not accounts_data:
        raise HTTPException(status_code=400, detail="Нет активных аккаунтов")

    # ── Если кампания из прогрева — грузим что аккаунты уже подписали ────
    from datetime import datetime as _dt
    prejoined_map = {}     # {account_id: [channels]} — подписки из прогрева
    if c.warmup_batch_id:
        from models.warmup import WarmupTask
        wts = (await db.execute(
            select(WarmupTask).where(WarmupTask.batch_id == c.warmup_batch_id)
        )).scalars().all()
        for wt in wts:
            subs = list((wt.subscribed_channels or {}).keys())
            if subs:
                prejoined_map[wt.account_id] = subs
        print(f"[start_campaign] Из прогрева {c.warmup_batch_id}: {len(prejoined_map)} аккаунтов с подписками")

    # ── Распределяем каналы между аккаунтами ─────────────────────────────
    account_id_list = [a[0] for a in accounts_data]
    from_warmup = bool(c.warmup_batch_id and prejoined_map)
    if from_warmup:
        # Каждый аккаунт комментит в СВОИ подписанные за прогрев каналы.
        # Не используем случайное распределение — берём warmup-mapping.
        channel_dist = {a: prejoined_map.get(a, []) for a in account_id_list}
    else:
        # ── Subscription-aware distribution: приоритетно даём канал акку
        # который уже на него подписан (из AccountSubscription) — экономим
        # join_target_channel действия.
        _global_subs_map = {}
        try:
            from utils.subscriptions import get_account_subscribed_map
            _global_subs_map = await get_account_subscribed_map(db, account_id_list, ready_only=True)
            _global_subs_lower = {aid: {c.lower() for c in chans} for aid, chans in _global_subs_map.items()}
        except Exception:
            _global_subs_lower = {}
        channel_dist = _distribute_channels(channel_usernames, account_id_list, existing_subs=_global_subs_lower)

    # ── Сохраняем матрицу в БД (удаляем старую) ──────────────────────────
    from models.campaign_channel_assignment import CampaignChannelAssignment
    from sqlalchemy import delete as sa_delete

    await db.execute(
        sa_delete(CampaignChannelAssignment).where(CampaignChannelAssignment.campaign_id == c.id)
    )
    await db.flush()

    import math
    # ── Глобальный реестр подписок (уже читали для _distribute_channels
    # если from_warmup=False; для warmup читаем сейчас) ──
    if from_warmup:
        _global_subs_map = {}
        try:
            from utils.subscriptions import get_account_subscribed_map
            _global_subs_map = await get_account_subscribed_map(
                db, list(channel_dist.keys()), ready_only=True
            )
        except Exception:
            pass

    _prejoined_from_global = 0
    _total_pending = 0
    _would_be_pending_without_reuse = 0
    _per_acc_stats = []   # [(acc_id, n_assigned, n_pre_joined, n_pending)]
    for acc_id, assigned_chs in channel_dist.items():
        _global_subs = {c.lower() for c in _global_subs_map.get(acc_id, set())}
        if from_warmup:
            for ch in assigned_chs:
                db.add(CampaignChannelAssignment(
                    campaign_id=c.id,
                    account_id=acc_id,
                    channel_username=ch,
                    status="joined",
                    planned_join_day=0,
                    joined_at=_dt.utcnow(),
                ))
        else:
            pending_chs = [ch for ch in assigned_chs if ch.lower() not in _global_subs]
            already_joined_chs = [ch for ch in assigned_chs if ch.lower() in _global_subs]
            _would_be_pending_without_reuse += len(assigned_chs)
            _total_pending += len(pending_chs)
            _prejoined_from_global += len(already_joined_chs)
            _per_acc_stats.append((acc_id, len(assigned_chs), len(already_joined_chs), len(pending_chs)))
            for ch in already_joined_chs:
                db.add(CampaignChannelAssignment(
                    campaign_id=c.id,
                    account_id=acc_id,
                    channel_username=ch,
                    status="joined",
                    planned_join_day=0,
                    joined_at=_dt.utcnow(),
                ))
            jpd = min(2, max(1, math.ceil(len(pending_chs) / max(1, total_days)))) if pending_chs else 1
            for i, ch in enumerate(pending_chs):
                planned_day = (i // jpd) + 1
                db.add(CampaignChannelAssignment(
                    campaign_id=c.id,
                    account_id=acc_id,
                    channel_username=ch,
                    status="pending",
                    planned_join_day=planned_day,
                ))
    await db.flush()
    if not from_warmup:
        _saved_pct = 100 * _prejoined_from_global / max(1, _would_be_pending_without_reuse)
        print(f"\n[start_campaign][camp {c.id}] ═══ AccountSubscription-reuse ═══")
        print(f"  Всего пар (acc,channel): {_would_be_pending_without_reuse}")
        print(f"  Уже подписаны глобально: {_prejoined_from_global} ({_saved_pct:.1f}%)")
        print(f"  Осталось pending для join_target: {_total_pending}")
        if _per_acc_stats:
            _sorted = sorted(_per_acc_stats, key=lambda x: -x[2])
            print(f"  TOP-10 акков с максимальной экономией:")
            for aid, n_asg, n_pre, n_pend in _sorted[:10]:
                _pct = 100 * n_pre / max(1, n_asg)
                print(f"    acc#{aid}: {n_asg} каналов → {n_pre} уже joined ({_pct:.0f}%), {n_pend} pending")
        print()

    # ── Распределяем комменты по дням ────────────────────────────────────
    # check_only: равномерно (акки уже прогреты, screening-цель).
    # regular:    прогревная волна (первые дни мало, потом рост).
    #
    # 2026-08-24: волна включалась для ЛЮБОЙ regular-кампании, в том числе
    # на аккаунтах, которые уже подписаны на свои каналы. Для них она не
    # «прогрев», а простой: замер кампании #139 (148 акков, 3562 канала,
    # 74.6% пар уже подписаны) — день 1 дал 2 коммента, день 2 дал 25 при
    # среднем 419/день. Волна имеет смысл там, где аккаунту надо сперва
    # обжиться в канале; если он уже подписан, ждать нечего.
    # Считаем аккаунты прогретыми по доле переиспользованных подписок.
    _reuse_pct = (
        100.0 * _prejoined_from_global / _would_be_pending_without_reuse
        if _would_be_pending_without_reuse else 0.0
    )
    # Доля переиспользования — плохой единственный признак: она падает не
    # только когда аккаунты новые, но и когда им просто дали больше каналов.
    # Замер 26.08: после чистки состава те же 1711 каналов разделились на 89
    # аккаунтов вместо 148 (19 каналов на каждого вместо 11), доля упала с
    # 60.7% до 47.8% — и генератор счёл ветеранов новичками, включив
    # прогревную волну с пустыми первыми днями.
    # Второй признак надёжнее: сколько у аккаунта УЖЕ есть подписок вообще.
    # Аккаунт с четырьмя десятками каналов — не новичок, сколько бы новых
    # ему сейчас ни выдали.
    try:
        _sub_counts = sorted(len(v) for v in (_global_subs_map or {}).values())
        _median_subs = _sub_counts[len(_sub_counts) // 2] if _sub_counts else 0
    except Exception:
        _median_subs = 0
    _warm_accounts = (
        bool(from_warmup)
        or _reuse_pct >= WARM_REUSE_PCT
        or _median_subs >= WARM_MIN_SUBS
    )
    print(f"[start_campaign][camp {c.id}] медиана существующих подписок: {_median_subs}")
    _even = _is_check_only or _warm_accounts
    print(f"[start_campaign][camp {c.id}] переиспользовано подписок {_reuse_pct:.1f}% "
          f"→ аккаунты {'ПРОГРЕТЫ' if _warm_accounts else 'холодные'}, "
          f"распределение {'ровное' if _even else 'волна'}")

    # ── Сколько комментов раскладывать в день ────────────────────────────
    # Раньше сюда шёл c.max_comments, и бюджет работал сразу в двух ролях:
    # потолком кампании И входом для дневной квоты. Из-за этого возникала
    # спираль: выставишь бюджет по факту прошлой сборки — следующая упрётся
    # в него раньше, чем в механику сессий, и выдаст меньше. Замер 25-26.08:
    # 3357 → 2005 → 1728 при неизменном парке аккаунтов.
    #
    # Теперь дневная квота считается от ЁМКОСТИ: сколько комментов физически
    # готовы принять аккаунты за сутки (число каналов у каждого × допустимые
    # повторы ÷ дни). max_comments остаётся тем, чем должен быть — верхней
    # границей, и на квоту больше не влияет, пока её не ограничивает.
    _mult_cap = 1 if _is_check_only else 3
    _cap_day = sum(
        max(1, math.ceil(len(channel_dist.get(_a, [])) * _mult_cap / max(1, total_days)))
        for _a in [x[0] for x in accounts_data]
    )
    _alloc_total = _cap_day * total_days
    if c.max_comments and c.max_comments < _alloc_total:
        _alloc_total = c.max_comments
        print(f"[start_campaign][camp {c.id}] бюджет {c.max_comments} ниже ёмкости "
              f"{_cap_day * total_days} — раскладываем по бюджету")
    print(f"[start_campaign][camp {c.id}] ёмкость парка {_cap_day}/день "
          f"({len(accounts_data)} акк), раскладываем {_alloc_total} на {total_days} дн")
    daily_comments = distribute_comments_by_days(
        _alloc_total, total_days, even=_even,
    )
    # distribute_comments_by_days может схлопнуть "хвост" (маленький последний
    # день) в предпоследний → total_days уменьшается автоматом.
    if len(daily_comments) < total_days:
        total_days = len(daily_comments)

    # ── Удаляем старые планы этой кампании ───────────────────────────────
    from models.campaign_plan import CampaignPlan
    await db.execute(sa_delete(CampaignPlan).where(CampaignPlan.campaign_id == c.id))
    await db.flush()

    plans_created = 0
    total_planned_comments = 0
    # Кросс-day tracker для check_only: не даём повторно комментить один
    # и тот же канал в разных днях одной кампании.
    _commented_by_acc: dict[int, set[str]] = {}

    # ── Кто сейчас отдыхает по претензии Telegram ────────────────────────
    # Генерация планов про отдых не знала: аккаунту с FLOOD_WAIT на 20 часов
    # всё равно выдавались сессии и комменты на сегодня. Навредить это не
    # могло — execute_plan_session проверяет is_resting ДО захвата слота и
    # до подключения, так что в Telegram ничего не уходит, — но слоты
    # сгорали впустую: квота дня уходила аккаунту, который её не отработает.
    # Считаем момент окончания отдыха один раз и ниже пропускаем аккаунт в
    # тех днях, которые отдых накрывает целиком.
    _rest_until: dict = {}
    try:
        from utils.account_health import rest_remaining as _rest_left
        # ВАЖНО: ниже в дневном цикле есть `from datetime import date, timedelta`,
        # из-за чего `timedelta` для всей функции — локальное имя и здесь ещё
        # не связано. Берём под своим алиасом.
        from datetime import timedelta as _td
        # Местное время, а НЕ utcnow: дни планирования нарезаются по
        # date.today(), а часы сессий лежат на местной оси (UTC+3). Со
        # сравнением в UTC отдых до 01:16 по местному выглядел как «истёк
        # 26-го в 22:16» и аккаунт ошибочно получал сессии на 26-е.
        _now_dt = datetime.now()
        for _a_id, _, _ in accounts_data:
            _left = _rest_left(_a_id) or 0
            if _left > 0:
                _rest_until[_a_id] = _now_dt + _td(seconds=_left)
        if _rest_until:
            print(f"[start_campaign][camp {c.id}] отдыхают {len(_rest_until)} аккаунтов: "
                  + ", ".join(f"acc#{a}→{d.strftime('%d.%m %H:%M')}"
                              for a, d in sorted(_rest_until.items())[:10]))
    except Exception as _re:
        print(f"[start_campaign] проверка отдыха пропущена: {_re}")

    for day_num in range(1, total_days + 1):
        from datetime import date, timedelta
        plan_date = date.today() + timedelta(days=day_num - 1)

        # Аккаунт недоступен в этот день, если отдых заканчивается ПОЗЖЕ
        # самого дня — либо истекает уже вечером, когда на осмысленную
        # сессию времени не остаётся. Если отдых кончается днём, аккаунт
        # оставляем: он отработает поздние сессии, а ранние executor
        # пропустит сам по is_resting.
        _off_today = {
            a for a, until in _rest_until.items()
            if until.date() > plan_date
            or (until.date() == plan_date and until.hour >= 20)
        }
        _day_accounts = [(a_id, ph, p) for a_id, ph, p in accounts_data if a_id not in _off_today]
        if _off_today:
            print(f"[start_campaign][camp {c.id}] день {day_num}: "
                  f"пропускаю {len(_off_today)} отдыхающих аккаунтов")
        if not _day_accounts:
            print(f"[start_campaign][camp {c.id}] день {day_num}: все аккаунты отдыхают — день пуст")
            continue

        accs_with_p = [(a_id, p) for a_id, _, p in _day_accounts]
        # check_only: пропорционально по каналам акка (иначе personality
        # limits режут до 1-2 на акк/день вместо ~5). Инцидент 2026-07-18.
        # Для regular умножаем target ×3 чтобы разрешить до 3 комментов
        # в один канал (multiply уже подписанных).
        _mult = 1 if _is_check_only else 3
        _target_by_acc = {
            a_id: max(1, math.ceil(len(channel_dist.get(a_id, [])) * _mult / max(1, total_days)))
            for a_id, _, _ in _day_accounts
        }
        day_distribution = distribute_day_comments(
            daily_comments[day_num - 1], accs_with_p, target_by_acc=_target_by_acc,
        )

        # Флаг для plan_generator: добавить одну tail-verify сессию
        # ~40 мин ПОСЛЕ последней рабочей сессии. Ставим ТОЛЬКО на
        # последний день кампании — только там мы гарантируем добор
        # комментов последней рабочей сессии.
        _is_last_day = (day_num == total_days)

        # ── РАСПИСАНИЕ ПО IP на этот день (2026-08-22) ────────────────
        # Раньше каждый аккаунт рандомил себе часы сам, ничего не зная о
        # соседях по прокси. Замер показал: 37.5% пар подключений на одном
        # IP укладывались в 15 минут, и разгребал это ip_throttle —
        # откладыванием, с потерей сессий (14 июля выброшено 126
        # smart_comment из 285).
        #
        # Теперь сессии ВСЕХ аккаунтов одного IP раскладываются на общей
        # оси суток с гарантированным зазором. Времена остаются
        # случайными — они разные каждый день, просто не пересекаются.
        from utils.ip_scheduler import allocate_ip_day, MIN_GAP_MIN as _IP_GAP
        _ip_groups: dict = {}
        for _a_id, _phone, _pers in _day_accounts:
            _n = random.randint(
                _pers.get("session_count_min", 2),
                _pers.get("session_count_max", 5),
            )
            if day_num <= 1 and not _warm_accounts:
                # «Первый день мягче» — щадящий режим для ХОЛОДНЫХ аккаунтов,
                # которые только знакомятся с каналами. Прогретым он режет
                # и без того короткий остаток первых суток: замер кампании
                # #139 — 144 сессии на 148 аккаунтов, то есть почти всем по
                # одной, а первая сессия дня комментов не пишет по правилу
                # `sess_i >= 1`. В итоге день 1 давал 3 коммента.
                _n = max(1, _n - random.randint(1, 2))
            if _is_last_day:
                # +1 слот под tail-verify сессию: генератор возьмёт последний
                # выданный как verify-only. Придумать её время сам он не может —
                # оно бы не учитывало соседей по IP.
                _n += 1
            _host = _acc_proxy_host.get(_a_id) or f"__noproxy_{_a_id}"
            _ip_groups.setdefault(_host, []).append(
                {"id": _a_id, "phone": _phone, "sessions": _n}
            )
        # В первый день планируем только оставшуюся часть суток: сессии в
        # прошлом dispatch всё равно выбросит как просроченные.
        _not_before = None
        _day1_cap = None
        if day_num == 1:
            _now_local = datetime.utcnow() + timedelta(hours=3)     # UTC+3
            _not_before = min(1380, _now_local.hour * 60 + _now_local.minute + 15)
            # Сколько сессий вообще влезает в остаток суток на один IP.
            # Без этого ограничения аккаунты запрашивали больше слотов, чем
            # есть места; планировщик отдавал пустой список, и аккаунт
            # проваливался в локальную генерацию с несогласованными временами.
            _day1_cap = max(0, (1440 - _not_before) // _IP_GAP)

        _times_by_acc: dict = {}
        for _host, _accs in _ip_groups.items():
            # Первый день: режем запрос под реальную вместимость остатка
            # суток, поровну между аккаунтами этого IP.
            if _day1_cap is not None and _accs:
                _per = max(0, _day1_cap // len(_accs))
                for _a in _accs:
                    _a["sessions"] = min(_a["sessions"], _per)
                _accs = [_a for _a in _accs if _a["sessions"] > 0]
                if not _accs:
                    continue
            try:
                _times_by_acc.update(allocate_ip_day(
                    _accs, min_gap_min=_IP_GAP, not_before_min=_not_before,
                ))
            except Exception as _se:
                print(f"[start_campaign] ip_scheduler @{_host} упал: {_se} — "
                      f"аккаунты этого IP получат локальные времена")

        # Аккаунтам, которым места в остатке суток не досталось, отдаём
        # ПУСТОЙ список, а не None: пустой означает «сегодня не работаем»,
        # None включил бы локальную генерацию несогласованных времён.
        if day_num == 1:
            for _a_id, _, _ in _day_accounts:
                _times_by_acc.setdefault(_a_id, [])

        for acc_id, phone, personality in _day_accounts:
            comments_for_acc = day_distribution.get(acc_id, 0)
            timing = assign_timing_profile(phone)
            style = assign_style_profile(phone)

            # ── Вычисляем план подписок/комментов для этого дня ──────────
            assigned = channel_dist.get(acc_id, [])
            # AccountSubscription-reuse: часть каналов уже подписаны глобально
            # → НЕ ставим их в channels_to_join, сразу в commentable.
            _acc_global_subs = _global_subs_map.get(acc_id, set()) if not from_warmup else set()
            _acc_global_subs_l = {c.lower() for c in _acc_global_subs}
            _pre_joined_chs = [ch for ch in assigned if ch.lower() in _acc_global_subs_l]
            _pending_chs = [ch for ch in assigned if ch.lower() not in _acc_global_subs_l]
            # Размножаем уже-подписанные каналы (random 1-3 коммента в каждый)
            # чтобы больше комментов писалось без новых подписок.
            # pending НЕ трогаем — там будут join → 1 коммент (безопаснее для anti-ban).
            _pre_joined_multiplied = []
            for ch in _pre_joined_chs:
                n = random.randint(1, 3)
                _pre_joined_multiplied.extend([ch] * n)
            random.shuffle(_pre_joined_multiplied)
            _pre_joined_chs = _pre_joined_multiplied
            _is_check_only_here = getattr(c, "campaign_type", "regular") == "check_only"
            if from_warmup:
                channels_to_join_today   = []
                commentable_before_today = assigned
            elif _is_check_only_here:
                # check_only: НЕ ограничиваем 2 joins/day. Спреадим ПОДПИСКИ
                # (только pending!) за total_days-1 рабочих дней.
                # Уже подписанные глобально → сразу в commentable.
                _real_days = max(1, total_days)
                jpd = max(1, math.ceil(len(_pending_chs) / _real_days)) if _pending_chs else 1
                join_start = min(len(_pending_chs), (day_num - 1) * jpd)
                join_end   = min(len(_pending_chs),  day_num      * jpd)
                channels_to_join_today   = _pending_chs[join_start:join_end]
                # Commentable = уже joined глобально + pending которые запланированы
                # на ПРЕДЫДУЩИЕ дни этого запуска
                commentable_before_today = _pre_joined_chs + _pending_chs[:join_start]
            else:
                jpd = min(2, max(1, math.ceil(len(_pending_chs) / max(1, total_days)))) if _pending_chs else 1
                join_start = min(len(_pending_chs), (day_num - 1) * jpd)
                join_end   = min(len(_pending_chs),  day_num      * jpd)
                channels_to_join_today   = _pending_chs[join_start:join_end]
                commentable_before_today = _pre_joined_chs + _pending_chs[:join_start]

            # check_only: verify в КАЖДОЙ сессии (screening main-priority).
            _pers_for_plan = personality
            if _is_check_only_here:
                _pers_for_plan = {**personality, "_force_verify_every_session": True}
            plan = generate_daily_plan(
                account_id=acc_id,
                phone=phone,
                campaign_channels=channel_usernames,
                campaign_id=c.id,
                day_number=day_num,
                comments_today=comments_for_acc,
                personality=_pers_for_plan,
                timing=timing,
                style=style,
                channels_to_join=channels_to_join_today,
                commentable_from_prev_days=commentable_before_today,
                commented_from_prev_days=list(_commented_by_acc.get(acc_id, set())) if _is_check_only_here else None,
                add_tail_verify_session=_is_last_day,
                # regular: до 3 комментов в один канал за день (для уже подписанных)
                # check_only: 1 коммент/канал/кампанию (screening main-priority)
                max_comments_per_channel_per_day=1 if _is_check_only_here else 3,
                # Прогретым аккаунтам разрешаем комментировать уже в первой
                # сессии дня: они давно подписаны, «осматриваться» им незачем,
                # а новых подключений это не добавляет.
                first_session_comments=_warm_accounts and not _is_check_only_here,
                # Времена, согласованные с соседями по IP. Если планировщик
                # по какой-то причине не дал их — генератор возьмёт свои.
                session_times=_times_by_acc.get(acc_id),
            )
            # Обновляем кросс-day tracker (только для check_only — иначе
            # regular получит 1 коммент/канал/кампанию что не то что мы хотим)
            if _is_check_only_here:
                _s = _commented_by_acc.setdefault(acc_id, set())
                for _sess in plan.get("sessions", []):
                    for _act in _sess.get("actions", []):
                        if _act.get("type") == "smart_comment" and _act.get("channel"):
                            _s.add(_act["channel"])

            # Используем РЕАЛЬНОЕ количество smart_comment actions из плана,
            # а не запрошенное. Генератор может поместить МЕНЬШЕ комментов
            # если у акка не хватает уникальных commentable каналов (dedupe).
            _real_comments = plan.get("total_comments", comments_for_acc)
            db.add(CampaignPlan(
                campaign_id=c.id,
                account_id=acc_id,
                plan_date=plan_date,
                day_number=day_num,
                plan=plan,
                total_comments=_real_comments,
            ))
            plans_created += 1
            total_planned_comments += _real_comments

    c.status = CampaignStatus.active
    c.started_at = datetime.utcnow()
    c.finished_at = None
    await db.flush()

    return {
        "success": True,
        "status": "active",
        "message": f"Кампания запущена: {total_days} дней, {plans_created} планов, {total_planned_comments} комментов распределено",
    }


@router.post("/campaigns/{campaign_id}/start")
async def start_campaign(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Запустить кампанию + сгенерировать планы для всех аккаунтов"""
    c = await _get_campaign(db, campaign_id, current_user.id)
    result = await _do_start_campaign(db, c)
    await db.commit()
    return result


@router.post("/campaigns/{campaign_id}/rebuild-plan")
async def rebuild_campaign_plan(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Пересобрать план кампании БЕЗ сброса счётчиков и истории.

    Что делает:
      1. Удаляет незавершённые CampaignPlan (status='active')
      2. Удаляет неотправленные CommentQueue (status='scheduled')
      3. НЕ трогает: comments_sent, CommentLog (история), уже joined каналы,
         executing/sent items из очереди.
      4. Генерирует новые планы с ТЕКУЩИМИ правилами
         (динамический daily_target 0-1/2-3/4-6/7, night_boost 30%,
         реакции на комменты, cooldown 60-180 мин, per-channel=2).

    Полезно после обновления правил комментинга — старые планы уже созданы
    с прежними параметрами, но новые правила executor уже применяет.
    Эта кнопка синхронизирует UI-план с реальным поведением.
    """
    from datetime import date, timedelta
    from models.campaign_plan import CampaignPlan
    from models.comment_queue import CommentQueue
    from models.campaign_channel_assignment import CampaignChannelAssignment
    from models.account import TelegramAccount
    from sqlalchemy import delete as sa_delete
    from tasks.plan_generator import (
        distribute_comments_by_days,
        distribute_day_comments,
        generate_daily_plan,
    )
    from tasks.behavior_engine import (
        assign_personality, assign_timing_profile, assign_style_profile,
    )
    import math

    c = await _get_campaign(db, campaign_id, current_user.id)
    if c.status not in (
        CampaignStatus.active, CampaignStatus.paused, CampaignStatus.scheduled,
    ):
        raise HTTPException(
            status_code=400,
            detail=(f"Кампания в статусе {c.status.value if hasattr(c.status,'value') else c.status} — "
                    "пересборка допустима только для active/paused/scheduled"),
        )

    # 1. Удаляем ВСЕ планы (и active, и done) — иначе будут дубликаты
    # per (acc, date) когда новые планы день 1 попадут на today.
    # История в CommentLog и статистика comments_sent остаётся нетронутой.
    del_p = await db.execute(sa_delete(CampaignPlan).where(
        CampaignPlan.campaign_id == c.id,
    ))
    n_plans_deleted = del_p.rowcount or 0

    del_q = await db.execute(sa_delete(CommentQueue).where(
        CommentQueue.campaign_id == c.id,
        CommentQueue.status == "scheduled",
    ))
    n_queue_deleted = del_q.rowcount or 0

    await db.flush()

    # 2. Собираем данные для генерации (аккаунты, каналы, distribution)
    ch_result = await db.execute(
        select(TargetChannel).where(TargetChannel.campaign_id == c.id, TargetChannel.is_active == True)
    )
    channels = ch_result.scalars().all()
    channel_usernames = [ch.username for ch in channels if ch.username]
    if not channel_usernames or not c.account_ids:
        raise HTTPException(status_code=400, detail="Нет каналов или акков в кампании")

    # ── Отсев тех, кому сейчас работать нельзя ───────────────────────────
    # Инцидент 26.08: кнопка «перестроить» в интерфейсе — отдельный путь
    # генерации, и фильтров _do_start_campaign у него нет. Из-за этого 59
    # аккаунтов, отправленных отдыхать после подтверждённого ограничения
    # Telegram, снова получили планы и снова начали жечь слоты на
    # user_banned. Проверяем и статус, и отдых.
    from utils.account_health import is_resting as _is_rest_rb
    accounts_data = []
    _skipped_rb = []
    for acc_id in c.account_ids:
        acc_r = await db.execute(
            select(TelegramAccount).where(TelegramAccount.id == acc_id)
        )
        acc = acc_r.scalar_one_or_none()
        if not acc or not acc.phone:
            continue
        if _val(acc.status) not in ("active", "unknown"):
            _skipped_rb.append((acc.id, _val(acc.status)))
            continue
        try:
            _r_rb, _why_rb = _is_rest_rb(acc.id)
        except Exception:
            _r_rb, _why_rb = False, ""
        if _r_rb:
            _skipped_rb.append((acc.id, f"отдых: {_why_rb[:32]}"))
            continue
        accounts_data.append((acc.id, acc.phone, assign_personality(acc.phone)))
    if _skipped_rb:
        print(f"[rebuild][camp {c.id}] пропущено {len(_skipped_rb)} аккаунтов: "
              + ", ".join(f"acc#{a} ({w})" for a, w in _skipped_rb[:10]))
    if not accounts_data:
        raise HTTPException(status_code=400, detail="Нет активных аккаунтов")

    # 3. Читаем существующие channel assignments (какие уже joined)
    #    — НЕ удаляем и НЕ пересоздаём (акки уже подписались, повторно
    #    вступать бесполезно и вредно).
    ca_result = await db.execute(
        select(CampaignChannelAssignment).where(
            CampaignChannelAssignment.campaign_id == c.id
        )
    )
    channel_dist: dict[int, list[str]] = {}
    joined_by_acc: dict[int, set[str]] = {}
    for ca in ca_result.scalars().all():
        channel_dist.setdefault(ca.account_id, []).append(ca.channel_username)
        if ca.status == "joined":
            joined_by_acc.setdefault(ca.account_id, set()).add(ca.channel_username)

    # ── Дополняем из ГЛОБАЛЬНОГО реестра подписок AccountSubscription ──
    # Если акк уже подписан на канал через другую кампанию/warmup/decoy —
    # НЕ будем повторно join'ить в этой кампании (экономим API + меньше палева).
    # Юзер настоял 2026-07-16: is_channel_joined OR is_discussion_joined = "готов".
    try:
        from utils.subscriptions import get_account_subscribed_map
        _acc_ids = [a[0] for a in accounts_data]
        _global_subs = await get_account_subscribed_map(db, _acc_ids, ready_only=True)
        # Апгрейдим: если из reestra + канал есть в assignment этой кампании,
        # он теперь считается joined.
        _upgraded = 0
        for a_id, chans in _global_subs.items():
            local_channels = set(channel_dist.get(a_id, []))
            for ch in chans:
                # Нормализация username в reestre — lowercase без @.
                # В campaign channels могут быть с capitalization → normalize
                _ch_l = ch.lower()
                for local_ch in list(local_channels):
                    if local_ch.lower() == _ch_l:
                        _prev = joined_by_acc.setdefault(a_id, set())
                        if local_ch not in _prev:
                            _prev.add(local_ch)
                            _upgraded += 1
        if _upgraded:
            print(f"[rebuild][camp {c.id}] AccountSubscription upgrade: +{_upgraded} joined через глобальный реестр")
    except Exception as _e_sub:
        print(f"[rebuild][camp {c.id}] AccountSubscription check err: {_e_sub}")

    # 4. REBUILD LOGIC (сохраняем assignments, доделываем недостающее):
    #    - assignments НЕ трогаем (channel_dist уже загружено выше)
    #    - каждый акк держит свои те же ~13 каналов
    #    - pending → в этом rebuild планируем join_target_channel
    #    - joined → уже commentable с дня 1 (можно комментить сразу)
    #    - комменты = len(assigned) — по одному на каждый канал акка
    #    - progress счётчик comments_sent НЕ сбрасывается
    #
    # Считаем pending per-acc.
    max_pending = 0
    total_pending = 0
    for acc_id in [a[0] for a in accounts_data]:
        assigned = channel_dist.get(acc_id, [])
        joined = joined_by_acc.get(acc_id, set())
        pending_count = len([ch for ch in assigned if ch not in joined])
        total_pending += pending_count
        if pending_count > max_pending:
            max_pending = pending_count

    _rb_check_only = getattr(c, "campaign_type", "regular") == "check_only"
    if _rb_check_only:
        # ═══ КАЛЬКУЛЯТОР total_days (гибкий, 0-100% pre-joined) ═══
        # MAX из трёх ограничений:
        #   1) Максимум комментов/день   → ceil(total / MAX_PER_DAY)
        #   2) Максимум подписок/день    → ceil(max_pending / 3.5)  (3-4 join'а/акк/сутки)
        #   3) Максимум per-acc/день     → ceil(total / (n_accs * MAX_PER_ACC_PER_DAY))
        # + 1 дополнительный день на добор (страховка если акки с малым числом сессий).
        # Лимиты повышены x2.5 (2026-07-20) — реальные акки могут больше
        # 2026-07-23 — +ночные сессии, per_acc 10→11 умеренно
        _MAX_PER_DAY = 1200
        _MAX_PER_ACC_PER_DAY = 11
        _n_accs = max(1, len(accounts_data))
        total_assigned = sum(len(channel_dist.get(a[0], [])) for a in accounts_data)

        _by_max_per_day = max(1, math.ceil(total_assigned / _MAX_PER_DAY))
        _by_pending = max(1, math.ceil(max_pending / 3.5))
        _by_per_acc = max(1, math.ceil(total_assigned / (_n_accs * _MAX_PER_ACC_PER_DAY)))
        _min_days = max(_by_max_per_day, _by_pending, _by_per_acc)
        # Safety-день: +1 если много pending подписок (нужно время), +0 если
        # почти всё уже joined (иначе кампания растягивается зря).
        _safety = 1 if max_pending > 5 else 0
        total_days = min(14, _min_days + _safety)
        print(f"[rebuild][camp {c.id}] total_days={total_days} "
              f"(by_max_per_day={_by_max_per_day}, by_pending={_by_pending}, by_per_acc={_by_per_acc}, "
              f"+1 safety day, total_assigned={total_assigned}, max_pending={max_pending}, n_accs={_n_accs})")
        c.max_comments = total_assigned
        c.max_hours = 0
    else:
        if c.max_hours and c.max_hours < 24:
            total_days = 1
        elif c.max_hours:
            total_days = max(1, math.ceil(c.max_hours / 24))
        else:
            total_days = 7

    # ── Iterative: пробуем total_days, если coverage < 95% → +1 день ──
    # Стресс-тест 1000 конфигов (scripts/stress_test_plan_generation.py):
    #   +0: 643 конфигов ok сразу
    #   +1: 231 конфигов
    #   +2: 123 конфигов
    #   +3: 3 конфигов
    # Так гарантируем что план вмещает 95%+ запланированных комментов
    # для любой конфигурации (0-100% pre-joined, 5-500 акков, 3-30 каналов/акк).

    # Кросс-day tracker для check_only (сохраняется между попытками)
    # Инициализируем из CommentLog (реально отправленные) + WarmupLog
    # smart_comment fail (skip'нутые: hard_guard, chat_write_forbidden,
    # msg_id_invalid, shadow_ban) — иначе rebuild будет снова планировать
    # эти "мёртвые" каналы и они снова упадут в skip.
    # Инцидент 2026-07-20: acc#138 за день имел 3 fail записи @mag_vodogray
    # т.к. rebuild трижды пересобирал план и трижды его туда ставил.
    _commented_by_acc_initial: dict[int, set[str]] = {}
    if _rb_check_only:
        from models.campaign import CommentLog
        from models.warmup_log import WarmupLog
        # 1. Успешные комменты (CommentLog)
        clog_r = await db.execute(
            select(CommentLog.account_id, CommentLog.channel_username).where(
                CommentLog.campaign_id == c.id,
            )
        )
        for a_id, ch_u in clog_r.all():
            if ch_u:
                _commented_by_acc_initial.setdefault(a_id, set()).add(ch_u)
        # 2. Skip'нутые попытки (WarmupLog smart_comment fail)
        wl_r = await db.execute(
            select(WarmupLog.account_id, WarmupLog.channel).where(
                WarmupLog.campaign_id == c.id,
                WarmupLog.action == "smart_comment",
                WarmupLog.channel != "",
            )
        )
        for a_id, ch_u in wl_r.all():
            if ch_u:
                _commented_by_acc_initial.setdefault(a_id, set()).add(ch_u)

    plans_created = 0
    total_planned = 0
    generated_plans = []  # (day_num, acc_id, plan, real_comments) до сохранения
    _max_attempts = 4
    for _attempt in range(_max_attempts):
        # Сбрасываем состояние итерации
        generated_plans = []
        plans_created = 0
        total_planned = 0
        _commented_by_acc = {k: set(v) for k, v in _commented_by_acc_initial.items()}

        daily_comments = distribute_comments_by_days(
            c.max_comments, total_days, even=_rb_check_only,
        )
        # distribute_comments_by_days может схлопнуть "хвост" (маленький последний
        # день) в предпоследний → total_days уменьшается автоматом.
        if len(daily_comments) < total_days:
            total_days = len(daily_comments)

        for day_num in range(1, total_days + 1):
            plan_date = date.today() + timedelta(days=day_num - 1)
            accs_with_p = [(a_id, p) for a_id, _, p in accounts_data]
            # target_by_acc — обходим лимит 6/акк/день и рандом comment_chance.
            # Множим на 3 (max_comments_per_channel_per_day=3) чтобы дать акку
            # возможность написать до 3 комментов в один канал.
            _mult = 1 if _rb_check_only else 3
            _target_by_acc = {
                a_id: max(1, math.ceil(len(channel_dist.get(a_id, [])) * _mult / max(1, total_days)))
                for a_id, _, _ in accounts_data
            }
            day_distribution = distribute_day_comments(
                daily_comments[day_num - 1], accs_with_p, target_by_acc=_target_by_acc,
            )
            _is_last_day = (day_num == total_days)

            for acc_id, phone, personality in accounts_data:
                assigned = channel_dist.get(acc_id, [])
                already_joined = joined_by_acc.get(acc_id, set())
                pending_for_acc = [ch for ch in assigned if ch not in already_joined]
                # Размножаем уже-подписанные каналы (random 1-3 коммента) чтобы
                # больше комментов без новых подписок (anti-ban safety).
                already_joined_multi = []
                for _ch in already_joined:
                    already_joined_multi.extend([_ch] * random.randint(1, 3))
                random.shuffle(already_joined_multi)
                already_joined = already_joined_multi
                _fair_target = None
                if _rb_check_only:
                    _fair_target = max(1, math.ceil(len(assigned) / max(1, total_days)))
                    if day_num == total_days:
                        _already_planned = (total_days - 1) * _fair_target
                        _fair_target = max(_fair_target, len(assigned) - _already_planned)

                _jpd = max(1, math.ceil(len(pending_for_acc) / max(1, total_days)))
                _js = (day_num - 1) * _jpd
                _je = day_num * _jpd
                channels_to_join_today = pending_for_acc[_js:_je]
                # already_joined уже list (после multiply выше)
                commentable_before_today = list(already_joined) + pending_for_acc[:_js]

                comments_for_acc = day_distribution.get(acc_id, 0)
                if _rb_check_only:
                    comments_for_acc = _fair_target

                _pers = personality
                if _rb_check_only:
                    _pers = {**personality, "_force_verify_every_session": True}
                plan = generate_daily_plan(
                    account_id=acc_id, phone=phone,
                    campaign_channels=channel_usernames, campaign_id=c.id,
                    day_number=day_num, comments_today=comments_for_acc,
                    personality=_pers,
                    timing=assign_timing_profile(phone),
                    style=assign_style_profile(phone),
                    channels_to_join=channels_to_join_today,
                    commentable_from_prev_days=commentable_before_today,
                    commented_from_prev_days=list(_commented_by_acc.get(acc_id, set())) if _rb_check_only else None,
                    add_tail_verify_session=_is_last_day,
                    max_comments_per_channel_per_day=1 if _rb_check_only else 3,
                )
                if _rb_check_only:
                    _s = _commented_by_acc.setdefault(acc_id, set())
                    for _sess in plan.get("sessions", []):
                        for _act in _sess.get("actions", []):
                            if _act.get("type") == "smart_comment" and _act.get("channel"):
                                _s.add(_act["channel"])
                _real_comments = plan.get("total_comments", comments_for_acc)
                generated_plans.append((day_num, acc_id, plan, _real_comments, plan_date))
                total_planned += _real_comments

        # ── Coverage check (только check_only, для regular однократно) ──
        if not _rb_check_only:
            break
        total_target = sum(len(channel_dist.get(a[0], [])) for a in accounts_data)
        # Уже отправленные CommentLog не считаются в target — это накопленное,
        # не пересобирается. Осталось = target - already_sent.
        _already_sent = sum(len(v) for v in _commented_by_acc_initial.values())
        _remaining_target = max(1, total_target - _already_sent)
        _coverage = 100 * total_planned / _remaining_target
        print(f"[rebuild][camp {c.id}] attempt={_attempt+1} total_days={total_days} → coverage={_coverage:.1f}% "
              f"({total_planned}/{_remaining_target})")
        if _coverage >= 95.0 or total_days >= 14:
            break
        total_days += 1

    # Сохраняем финальный результат в БД
    plans_created = len(generated_plans)
    for day_num, acc_id, plan, real_c, plan_date in generated_plans:
        db.add(CampaignPlan(
            campaign_id=c.id, account_id=acc_id,
            plan_date=plan_date, day_number=day_num,
            plan=plan, total_comments=real_c,
        ))
    await db.flush()
    await db.commit()

    return {
        "ok": True,
        "message": (
            f"План пересобран: {n_plans_deleted} старых planов + {n_queue_deleted} pending "
            f"items удалено, {plans_created} новых planов создано ({total_planned} комментов запланировано)"
        ),
        "deleted_plans": n_plans_deleted,
        "deleted_queue": n_queue_deleted,
        "created_plans": plans_created,
        "total_planned_comments": total_planned,
    }


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


@router.post("/campaigns/{campaign_id}/restart")
async def restart_campaign(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Перезапустить завершённую/остановленную кампанию.

    Что делает:
      1. Проверяет что кампания в статусе finished/stopped/paused/draft
         (active нельзя перезапускать — надо сначала остановить)
      2. Обнуляет счётчики: comments_sent, finished_at
      3. Удаляет старые CampaignPlan (dispatch их бы попытался
         продолжить, а нам нужен новый прогон с текущими max_hours)
      4. Сбрасывает last_post_id у target_channels и comments_sent
         (по опции keep_channel_progress=false — параметр в UI)
      5. Вызывает _do_start_campaign — новые планы с текущими max_hours

    Полезно когда:
      - Кампания finished и хочешь ещё раз пройтись по каналам
      - После правок max_hours / max_comments / delay_* нужно
        перегенерировать планы
    """
    c = await _get_campaign(db, campaign_id, current_user.id)
    if c.status == CampaignStatus.active:
        raise HTTPException(
            status_code=400,
            detail="Кампания уже active — нельзя перезапускать без остановки",
        )

    # 1. Обнуляем счётчики
    c.comments_sent = 0
    c.finished_at = None
    c.started_at = None
    c.status = CampaignStatus.draft  # Временно, _do_start вернёт active

    # 2. Удаляем старые планы (dispatch'ер их бы подхватил)
    from models.campaign_plan import CampaignPlan
    from sqlalchemy import delete as sa_delete
    plans_del = await db.execute(
        sa_delete(CampaignPlan).where(CampaignPlan.campaign_id == c.id)
    )
    deleted_plans = plans_del.rowcount

    # 3. Обнуляем счётчики каналов (сколько уже комментов туда написали)
    #    и сбрасываем last_post_id чтобы кампания начала с недавнего поста
    ch_result = await db.execute(
        select(TargetChannel).where(TargetChannel.campaign_id == c.id)
    )
    channels = ch_result.scalars().all()
    for ch in channels:
        ch.comments_sent = 0
        ch.last_post_id = 0

    await db.flush()

    # 4. Стартуем как обычно — новые планы будут учитывать текущие max_hours
    result = await _do_start_campaign(db, c)
    await db.commit()
    result["restarted"] = True
    result["deleted_old_plans"] = deleted_plans
    result["channels_reset"] = len(channels)
    return result


@router.post("/campaigns/{campaign_id}/cancel-schedule")
async def cancel_schedule(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Отменить запланированный авто-старт. Кампания возвращается в 'draft',
    scheduled_start_at очищается, warmup_batch_id сохраняется (можно
    запустить вручную и каналы из прогрева всё равно будут pre-joined).
    """
    c = await _get_campaign(db, campaign_id, current_user.id)
    if c.status != CampaignStatus.scheduled:
        raise HTTPException(status_code=400, detail=f"Кампания не в статусе scheduled (status={c.status})")
    c.status = CampaignStatus.draft
    c.scheduled_start_at = None
    await db.flush()
    return {"success": True, "status": "draft", "message": "Планирование отменено. Можно запустить вручную."}


# ── Target Channels ──────────────────────────────────────────

@router.post("/campaigns/{campaign_id}/channels")
async def add_channels(
    campaign_id: int,
    body: AddChannelsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Добавить целевые каналы в кампанию.
    ФИЛЬТР: каналы у которых в ParsedChannel has_comments=False пропускаются
    (нет смысла подписываться и слать запросы если нельзя комментить)."""
    from models.parsed_channel import ParsedChannel
    c = await _get_campaign(db, campaign_id, current_user.id)

    # Нормализуем все входящие usernames заранее
    _raw_map = {}    # {username: (link, raw)}
    for raw in body.channels:
        link = raw.strip()
        if not link:
            continue
        if link.startswith("@"):
            username = link[1:]
            link = f"https://t.me/{username}"
        elif "t.me/" in link:
            username = link.split("t.me/")[-1].split("/")[0].replace("@", "")
        else:
            username = link.replace("@", "")
            link = f"https://t.me/{username}"
        if username:
            _raw_map[username] = link

    # Одним запросом читаем ParsedChannel.has_comments для всех
    _no_comm = set()
    if _raw_map:
        pc_r = await db.execute(select(ParsedChannel.username).where(
            ParsedChannel.username.in_(list(_raw_map.keys())),
            ParsedChannel.has_comments == False,
        ))
        _no_comm = {u for u, in pc_r.all()}

    added = 0
    skipped_no_comm = 0
    for username, link in _raw_map.items():
        # Фильтр: канал без комментов
        if username in _no_comm:
            skipped_no_comm += 1
            continue

        # Проверяем дубликат
        existing = await db.execute(
            select(TargetChannel).where(
                TargetChannel.campaign_id == c.id,
                TargetChannel.username == username,
            )
        )
        if existing.scalar_one_or_none():
            continue

        db.add(TargetChannel(
            campaign_id=c.id,
            username=username,
            link=link,
            title=username,
        ))
        added += 1

    await db.flush()
    return {
        "added": added,
        "skipped_no_comments": skipped_no_comm,
        "message": f"Добавлено {added} каналов. Пропущено {skipped_no_comm} без комментов.",
    }


class ImportWarmupRequest(BaseModel):
    batch_id: str


@router.post("/campaigns/{campaign_id}/import-from-warmup")
async def import_from_warmup(
    campaign_id: int,
    body: ImportWarmupRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Импортирует каналы из batch'а прогрева в кампанию.

    Что делает:
      1. Берёт из прогрева {account_id: [channels]} — на что аккаунты подписались
      2. Добавляет все уникальные каналы как TargetChannel кампании
      3. Добавляет эти аккаунты в кампанию (account_ids)
      4. Связывает кампанию с batch'ем (warmup_batch_id)

    При старте кампании эти каналы будут commentable с 1-го дня
    (status='joined'), потому что аккаунты уже подписаны за прогрев.
    Пишем НЕ сразу после подписки, а после прогрева → меньше банов.
    """
    from models.warmup import WarmupTask

    c = await _get_campaign(db, campaign_id, current_user.id)

    # ── Грузим warmup-задачи batch'а ──
    tasks = (await db.execute(
        select(WarmupTask).where(
            WarmupTask.batch_id == body.batch_id,
            WarmupTask.user_id == current_user.id,
        )
    )).scalars().all()

    if not tasks:
        raise HTTPException(status_code=404, detail="Batch прогрева не найден")

    # Собираем mapping account → подписанные каналы
    account_channels = {}     # {account_id: [channels]}
    all_channels = set()
    for t in tasks:
        subs = list((t.subscribed_channels or {}).keys())
        if subs:
            account_channels[t.account_id] = subs
            all_channels.update(subs)

    if not all_channels:
        raise HTTPException(
            status_code=400,
            detail="В прогреве ещё нет подписок. Дождись пока аккаунты подпишутся на каналы.",
        )

    # ── 1. Добавляем каналы как TargetChannel (без дублей + фильтр has_comments) ──
    from models.parsed_channel import ParsedChannel
    existing_usernames = set()
    ex_r = await db.execute(
        select(TargetChannel.username).where(TargetChannel.campaign_id == c.id)
    )
    existing_usernames = {row[0] for row in ex_r.fetchall()}

    # Фильтр: убираем каналы без комментов
    no_comm_r = await db.execute(select(ParsedChannel.username).where(
        ParsedChannel.username.in_(list(all_channels)),
        ParsedChannel.has_comments == False,
    ))
    _no_comm_import = {u for u, in no_comm_r.all()}

    added = 0
    skipped_no_comm = 0
    for username in sorted(all_channels):
        if username in existing_usernames:
            continue
        if username in _no_comm_import:
            skipped_no_comm += 1
            continue
        db.add(TargetChannel(
            campaign_id=c.id,
            username=username,
            link=f"https://t.me/{username}",
            title=username,
        ))
        added += 1
    if skipped_no_comm:
        print(f"[import_warmup] Пропущено {skipped_no_comm} каналов без комментов")

    # ── 2. Добавляем аккаунты прогрева в кампанию ──
    current_accs = set(c.account_ids or [])
    warmup_accs = set(account_channels.keys())
    c.account_ids = sorted(current_accs | warmup_accs)

    # ── 3. Связываем с batch'ем ──
    c.warmup_batch_id = body.batch_id

    await db.flush()
    await db.commit()

    return {
        "imported_channels": added,
        "total_unique_channels": len(all_channels),
        "warmup_accounts": len(warmup_accs),
        "campaign_accounts": len(c.account_ids),
        "message": (
            f"Импортировано {added} новых каналов из прогрева. "
            f"Аккаунтов: {len(warmup_accs)}. "
            f"При старте они будут commentable с 1-го дня (уже подписаны)."
        ),
    }


# ── SCHEDULE: создать кампанию и запланировать на момент окончания прогрева ──

class ScheduleAfterWarmupRequest(BaseModel):
    batch_id: str
    # все поля кампании (как в CampaignCreate, но без account_ids — они придут из batch)
    name: str
    trigger_mode: str = "all"
    trigger_percent: int = 50
    trigger_keywords: list[str] = []
    llm_provider: str = "claude"
    llm_credential_id: Optional[int] = None
    tone: str = "positive"
    custom_prompt: str = ""
    comment_length: str = "medium"
    max_comments: int = 100
    max_hours: int = 24
    delay_join: int = 10
    delay_comment: int = 250
    delay_between: int = 60


@router.post("/campaigns/schedule-after-warmup")
async def schedule_after_warmup(
    body: ScheduleAfterWarmupRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Создаёт кампанию со status='scheduled' и привязывает её к batch'у прогрева.

    Что делает за один запрос:
      1. Создаёт Campaign со всеми настройками + warmup_batch_id=<batch>.
      2. Подтягивает каналы из прогрева (как import_from_warmup).
      3. Подтягивает аккаунты прогрева в account_ids.
      4. Считает ожидаемое scheduled_start_at = max(WarmupTask.started_at + total_days дней)
         по всем задачам batch'а — это fallback, если прогрев не дойдёт до finished.
      5. Ставит status='scheduled'.

    Дальше dispatch_plans автоматически переведёт кампанию в active когда
    либо все WarmupTask батча в статусе 'finished', либо scheduled_start_at прошло.
    """
    from models.warmup import WarmupTask
    from datetime import timedelta

    tasks = (await db.execute(
        select(WarmupTask).where(
            WarmupTask.batch_id == body.batch_id,
            WarmupTask.user_id == current_user.id,
        )
    )).scalars().all()

    if not tasks:
        raise HTTPException(status_code=404, detail="Batch прогрева не найден")

    # Собираем mapping account → подписанные каналы
    account_channels = {}
    all_channels = set()
    for t in tasks:
        subs = list((t.subscribed_channels or {}).keys())
        if subs:
            account_channels[t.account_id] = subs
            all_channels.update(subs)

    if not account_channels:
        raise HTTPException(
            status_code=400,
            detail="В прогреве ещё нет подписок — нечего планировать. Дождись пока прогрев успеет подписать хоть один аккаунт.",
        )

    # Ожидаемое окончание прогрева (fallback на случай если задачи не дойдут до finished)
    expected_finish = None
    for t in tasks:
        if t.started_at:
            finish_est = t.started_at + timedelta(days=t.total_days or 7)
            if expected_finish is None or finish_est > expected_finish:
                expected_finish = finish_est

    # Создаём кампанию
    c = Campaign(
        user_id=current_user.id,
        name=body.name,
        account_ids=sorted(account_channels.keys()),
        trigger_mode=body.trigger_mode,
        trigger_percent=body.trigger_percent,
        trigger_keywords=body.trigger_keywords,
        llm_provider=body.llm_provider,
        llm_credential_id=body.llm_credential_id,
        tone=body.tone,
        custom_prompt=body.custom_prompt,
        comment_length=body.comment_length,
        max_comments=body.max_comments,
        max_hours=body.max_hours,
        delay_join=body.delay_join,
        delay_comment=body.delay_comment,
        delay_between=body.delay_between,
        warmup_batch_id=body.batch_id,
        scheduled_start_at=expected_finish,
        status=CampaignStatus.scheduled,
    )
    db.add(c)
    await db.flush()

    # Каналы (без дублей)
    for username in sorted(all_channels):
        db.add(TargetChannel(
            campaign_id=c.id,
            username=username,
            link=f"https://t.me/{username}",
            title=username,
        ))

    await db.commit()

    return {
        "campaign_id": c.id,
        "status": "scheduled",
        "scheduled_start_at": expected_finish.isoformat() + "Z" if expected_finish else None,
        "channels_imported": len(all_channels),
        "accounts": len(account_channels),
        "warmup_batch_id": body.batch_id,
        "message": (
            f"Кампания «{body.name}» запланирована. "
            f"Стартует автоматически по окончании прогрева "
            f"(ожидаемо: {expected_finish.strftime('%d.%m.%Y %H:%M') if expected_finish else 'когда все задачи завершатся'} UTC)."
        ),
    }


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


@router.get("/campaigns/{campaign_id}/diagnostics")
async def campaign_diagnostics(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Полный диагностический дамп по кампании — что реально работает,
    что застряло, какие ошибки. Юзер должен уметь понять где и почему
    он потерял 50-90% запланированных действий.

    Возвращает:
      * summary — % выполнения, comments_sent, planned
      * plan_status — сколько done / active / failed по дням
      * assignment_status — pending / joined / failed
      * errors — топ ошибок из warmup_logs за 48ч
      * stuck_plans — планы status=active где день уже прошёл
      * zero_productive_accs — акки которые вообще ничего не сделали
    """
    from datetime import datetime, timedelta
    from sqlalchemy import func, and_
    from models.campaign_plan import CampaignPlan
    from models.campaign_channel_assignment import CampaignChannelAssignment
    from models.warmup_log import WarmupLog

    c = await _get_campaign(db, campaign_id, current_user.id)

    # 1. Summary
    total_planned = int(c.max_comments or 0)
    sent = int(c.comments_sent or 0)
    # ── Skip'нутые: unique (acc, channel) из WarmupLog smart_comment fail ──
    # Считаем только один skip per (acc, channel) — множественные попытки
    # (dispatch retry, ip_cooldown) не должны раздувать цифру.
    from sqlalchemy import distinct
    _skip_q = await db.execute(
        select(func.count(distinct(func.concat(WarmupLog.account_id, ':', WarmupLog.channel)))).where(
            WarmupLog.campaign_id == c.id,
            WarmupLog.action == "smart_comment",
            WarmupLog.success == False,
            WarmupLog.channel != "",
        )
    )
    skipped = int(_skip_q.scalar() or 0)
    processed = sent + skipped   # обработано = написано + скипнуто
    percent = round(100 * sent / max(1, total_planned), 2) if total_planned else 0
    percent_processed = round(100 * processed / max(1, total_planned), 2) if total_planned else 0

    # 2. Plans by day+status
    plans_q = await db.execute(
        select(
            CampaignPlan.day_number, CampaignPlan.status,
            func.count().label("cnt"),
            func.sum(CampaignPlan.executed_idx).label("sum_exec"),
            func.sum(CampaignPlan.total_comments).label("sum_comments"),
        ).where(CampaignPlan.campaign_id == c.id)
         .group_by(CampaignPlan.day_number, CampaignPlan.status)
         .order_by(CampaignPlan.day_number, CampaignPlan.status)
    )
    plans_by_day = [
        {"day": r[0], "status": r[1], "count": r[2],
         "exec_actions_sum": int(r[3] or 0),
         "planned_comments": int(r[4] or 0)}
        for r in plans_q.all()
    ]

    # 3. Assignment status
    asg_q = await db.execute(
        select(CampaignChannelAssignment.status, func.count())
        .where(CampaignChannelAssignment.campaign_id == c.id)
        .group_by(CampaignChannelAssignment.status)
    )
    assignments = {r[0]: r[1] for r in asg_q.all()}

    # 4. Top errors (last 48h) + список акков для каждой ошибки
    cutoff = datetime.utcnow() - timedelta(hours=48)
    errs_q = await db.execute(
        select(WarmupLog.action, WarmupLog.detail, WarmupLog.account_id)
        .where(
            WarmupLog.campaign_id == c.id,
            WarmupLog.success == False,
            WarmupLog.created_at > cutoff,
        )
        .order_by(WarmupLog.created_at.desc())
        .limit(3000)
    )
    # Группируем в Python: (action, detail_prefix) → {count, accounts}
    _err_groups: dict[tuple[str, str], dict] = {}
    for action, detail, acc_id in errs_q.all():
        _det_key = (detail or "")[:200]
        _key = (action or "", _det_key)
        g = _err_groups.setdefault(_key, {"count": 0, "accounts": []})
        g["count"] += 1
        if acc_id and acc_id not in g["accounts"] and len(g["accounts"]) < 30:
            g["accounts"].append(acc_id)
    errors = sorted(
        [
            {"action": k[0], "detail": k[1], "count": v["count"], "accounts": v["accounts"]}
            for k, v in _err_groups.items()
        ],
        key=lambda x: -x["count"],
    )[:30]

    # 5. Success actions breakdown
    ok_q = await db.execute(
        select(WarmupLog.action, WarmupLog.success, func.count())
        .where(
            WarmupLog.campaign_id == c.id,
            WarmupLog.created_at > cutoff,
        )
        .group_by(WarmupLog.action, WarmupLog.success)
        .order_by(func.count().desc())
        .limit(30)
    )
    actions_breakdown = [
        {"action": r[0], "success": bool(r[1]), "count": r[2]}
        for r in ok_q.all()
    ]

    # 6. Stuck plans (status=active, date < today)
    today = datetime.utcnow().date()
    stuck_q = await db.execute(
        select(
            CampaignPlan.id, CampaignPlan.account_id, CampaignPlan.day_number,
            CampaignPlan.plan_date, CampaignPlan.executed_idx,
            CampaignPlan.plan, CampaignPlan.status,
        ).where(
            CampaignPlan.campaign_id == c.id,
            CampaignPlan.status == "active",
            CampaignPlan.plan_date < today,
        )
        .order_by(CampaignPlan.plan_date, CampaignPlan.id)
        .limit(50)
    )
    stuck_plans = []
    for r in stuck_q.all():
        n_sess = 0
        try:
            n_sess = len((r[5] or {}).get("sessions", []))
        except Exception:
            pass
        stuck_plans.append({
            "plan_id": r[0], "account_id": r[1], "day_number": r[2],
            "plan_date": r[3].isoformat() if r[3] else None,
            "executed_idx": r[4], "total_sessions": n_sess,
            "status": r[6],
        })

    # 7. Zero-productive accounts + причина бездействия
    zero_q = await db.execute(
        select(CampaignPlan.account_id, func.sum(CampaignPlan.executed_idx))
        .where(CampaignPlan.campaign_id == c.id)
        .group_by(CampaignPlan.account_id)
        .having(func.sum(CampaignPlan.executed_idx) == 0)
        .limit(200)
    )
    zero_ids = [r[0] for r in zero_q.all()]
    zero_accs = []
    if zero_ids:
        from models.account import TelegramAccount
        from models.proxy import Proxy
        # Тянем акки + прокси одним запросом
        accs_r = await db.execute(
            select(TelegramAccount).where(TelegramAccount.id.in_(zero_ids))
        )
        acc_map = {a.id: a for a in accs_r.scalars().all()}
        # Прокси всех сразу
        _proxy_ids = [a.proxy_id for a in acc_map.values() if a.proxy_id]
        proxy_map = {}
        if _proxy_ids:
            pr = await db.execute(select(Proxy).where(Proxy.id.in_(_proxy_ids)))
            proxy_map = {p.id: p for p in pr.scalars().all()}
        # Последняя ошибка каждого акка из WarmupLog (кампания)
        last_err_map: dict[int, tuple[str, datetime]] = {}
        err_q = await db.execute(
            select(WarmupLog.account_id, WarmupLog.action, WarmupLog.detail, WarmupLog.created_at)
            .where(
                WarmupLog.campaign_id == c.id,
                WarmupLog.account_id.in_(zero_ids),
                WarmupLog.success == False,
                WarmupLog.created_at > cutoff,
            )
            .order_by(WarmupLog.created_at.desc())
            .limit(500)
        )
        for a_id, action, detail, ts in err_q.all():
            if a_id not in last_err_map:
                last_err_map[a_id] = (f"{action}: {(detail or '')[:120]}", ts)
        # Планы на сегодня
        _today = datetime.utcnow().date()
        plans_today_q = await db.execute(
            select(CampaignPlan.account_id, func.count(CampaignPlan.id))
            .where(
                CampaignPlan.campaign_id == c.id,
                CampaignPlan.account_id.in_(zero_ids),
                CampaignPlan.plan_date == _today,
            )
            .group_by(CampaignPlan.account_id)
        )
        plans_today_map = {a_id: cnt for a_id, cnt in plans_today_q.all()}

        for a_id in zero_ids:
            a = acc_map.get(a_id)
            if not a:
                zero_accs.append({"account_id": a_id, "reason": "акк удалён из БД"})
                continue
            _status = getattr(a, "status", None)
            _status_val = _status.value if hasattr(_status, "value") else str(_status or "")
            _phone = a.phone or ""
            _proxy = proxy_map.get(a.proxy_id) if a.proxy_id else None
            _proxy_str = f"{_proxy.host}:{_proxy.port}" if _proxy else None
            _proxy_ok = bool(_proxy and getattr(_proxy, "is_active", True))
            _last_err_text, _last_err_at = last_err_map.get(a_id, (None, None))
            _plans_today = plans_today_map.get(a_id, 0)

            # Определяем причину по приоритету
            if _status_val in ("banned", "spam_blocked", "dead", "invalid"):
                reason = f"акк {_status_val}"
            elif _status_val not in ("active", "unknown"):
                reason = f"статус: {_status_val}"
            elif a.error:
                reason = f"ошибка акка: {(a.error or '')[:140]}"
            elif _proxy and not _proxy_ok:
                reason = f"прокси неактивна ({_proxy_str})"
            elif not _proxy:
                reason = "нет прокси"
            elif _last_err_text:
                reason = _last_err_text
            elif _plans_today == 0:
                reason = "планов на сегодня нет"
            else:
                reason = "сессия ещё не наступила / скипнута dispatcher'ом"

            zero_accs.append({
                "account_id": a_id,
                "phone": _phone,
                "status": _status_val,
                "proxy": _proxy_str,
                "proxy_active": _proxy_ok,
                "plans_today": _plans_today,
                "last_error": _last_err_text,
                "last_error_at": _last_err_at.isoformat() + "Z" if _last_err_at else None,
                "reason": reason,
            })

    # 8. Top productive accounts
    top_q = await db.execute(
        select(
            CampaignPlan.account_id,
            func.sum(CampaignPlan.executed_idx).label("exec_sum"),
            func.count(CampaignPlan.id).label("plan_cnt"),
        )
        .where(CampaignPlan.campaign_id == c.id)
        .group_by(CampaignPlan.account_id)
        .order_by(func.sum(CampaignPlan.executed_idx).desc())
        .limit(10)
    )
    top_accs = [
        {"account_id": r[0], "exec_actions": int(r[1] or 0), "plan_count": r[2]}
        for r in top_q.all()
    ]

    # 9. Failed channels — каналы куда пытались комментить, но упало.
    # Группируем по каналу: сколько попыток / успехов / фейлов / категории ошибок.
    from collections import defaultdict as _dd
    fch_q = await db.execute(
        select(
            WarmupLog.channel, WarmupLog.success, WarmupLog.error, WarmupLog.detail,
            WarmupLog.account_id, WarmupLog.created_at,
        ).where(
            WarmupLog.campaign_id == c.id,
            WarmupLog.action == "smart_comment",
            WarmupLog.channel != "",
        ).order_by(WarmupLog.created_at.desc())
    )
    _ch_data: dict[str, dict] = {}
    for ch, ok, err, det, acc_id, ts in fch_q.all():
        if not ch:
            continue
        d = _ch_data.setdefault(ch, {
            "channel": ch, "attempts": 0, "ok": 0, "failed": 0,
            "errors": _dd(int), "accounts": set(), "last_error": None,
            "last_error_at": None,
        })
        d["attempts"] += 1
        d["accounts"].add(acc_id)
        if ok:
            d["ok"] += 1
        else:
            d["failed"] += 1
            _err_short = (err or det or "unknown")[:80]
            d["errors"][_err_short] += 1
            if d["last_error"] is None:
                d["last_error"] = _err_short
                d["last_error_at"] = ts.isoformat() + "Z" if ts else None
    # Только каналы где были fails (для аналитики)
    failed_channels = []
    for d in _ch_data.values():
        if d["failed"] == 0:
            continue
        failed_channels.append({
            "channel": d["channel"],
            "attempts": d["attempts"],
            "ok": d["ok"],
            "failed": d["failed"],
            "pass_rate": round(100 * d["ok"] / max(1, d["attempts"]), 1),
            "accounts_count": len(d["accounts"]),
            "accounts_sample": sorted(d["accounts"])[:10],
            "top_errors": [{"error": e, "count": n} for e, n in
                           sorted(d["errors"].items(), key=lambda x: -x[1])[:3]],
            "last_error": d["last_error"],
            "last_error_at": d["last_error_at"],
        })
    # Сортируем по кол-ву fails (самые проблемные первые)
    failed_channels.sort(key=lambda x: -x["failed"])
    failed_channels = failed_channels[:200]   # top 200

    # Одним запросом — phone для всех acc_id из errors и failed_channels
    _all_acc_ids: set[int] = set()
    for e in errors:
        _all_acc_ids.update(e.get("accounts", []))
    for ch in failed_channels:
        _all_acc_ids.update(ch.get("accounts_sample", []))
    _phone_map: dict[int, str] = {}
    if _all_acc_ids:
        from models.account import TelegramAccount as _AccPh
        _ph_q = await db.execute(select(_AccPh.id, _AccPh.phone).where(_AccPh.id.in_(_all_acc_ids)))
        _phone_map = {rid: (rph or "") for rid, rph in _ph_q.all()}
    # Обогащаем: accounts / accounts_sample → [{id, phone}]
    for e in errors:
        e["accounts"] = [{"id": aid, "phone": _phone_map.get(aid, "")} for aid in e.get("accounts", [])]
    for ch in failed_channels:
        ch["accounts_sample"] = [{"id": aid, "phone": _phone_map.get(aid, "")}
                                 for aid in ch.get("accounts_sample", [])]

    return {
        "campaign_id": c.id,
        "name": c.name,
        "status": _val(c.status),
        "campaign_type": getattr(c, "campaign_type", "regular"),
        "summary": {
            "planned_comments": total_planned,
            "sent_comments": sent,
            "skipped_comments": skipped,
            "processed_comments": processed,
            "percent_done": percent,
            "percent_processed": percent_processed,
        },
        "plans_by_day": plans_by_day,
        "assignments": assignments,
        "errors": errors,
        "actions_breakdown": actions_breakdown,
        "stuck_plans": stuck_plans,
        "zero_productive_accounts": zero_accs,
        "top_productive_accounts": top_accs,
        "failed_channels": failed_channels,
    }


@router.post("/campaigns/{campaign_id}/discover-subscriptions")
async def discover_subscriptions_endpoint(
    campaign_id: int,
    only_campaign_channels: bool = True,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Сканирует существующие подписки всех акков кампании и записывает в
    AccountSubscription. Для acc'ов кампании: iter_dialogs → найденные каналы +
    их discussion group (через linked_chat_id).

    only_campaign_channels=True (default): пишем ТОЛЬКО те подписки, которые
    пересекаются с TargetChannel этой кампании. False: пишем всё (может быть
    ~1000 подписок на акк, тяжело для БД).

    После завершения при следующем rebuild кампании acc'и уже подписанные на
    канал (через discovery) НЕ будут получать join_target_channel action.
    """
    c = await _get_campaign(db, campaign_id, current_user.id)
    from celery_app import celery_app as _celery
    task = _celery.send_task(
        "tasks.subscription_discovery.discover_campaign",
        args=[c.id, bool(only_campaign_channels)],
        queue="bulk_actions",
    )
    return {
        "ok": True,
        "message": f"Discovery запущен для кампании #{c.id} — {len(c.account_ids or [])} акков",
        "task_id": task.id,
        "n_accs": len(c.account_ids or []),
    }


@router.post("/campaigns/{campaign_id}/backfill-discussions")
async def backfill_discussion_joins_endpoint(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Догнать discussion-подписки для всех joined каналов этой кампании.

    Юзкейс: часть акков подписалась на канал ДО того как мы добавили
    pre-emptive-join в discussion group. Каждый первый smart_comment на
    такой канал даёт ChatGuestSendForbidden, потом auto-join fallback +
    retry. Это тратит N лишних API-запросов. Backfill проходит один раз
    по всем joined и вступает в discussion где надо.

    Использует Redis-кеш linked_chat_id (24ч TTL) — если один и тот же
    канал у нескольких акков, GetFullChannelRequest вызывается только
    ОДИН раз.
    """
    c = await _get_campaign(db, campaign_id, current_user.id)
    from celery_app import celery_app
    task = celery_app.send_task(
        "tasks.backfill_discussions.backfill_discussion_joins",
        args=[c.id],
        queue="bulk_actions",
    )
    return {
        "ok": True,
        "task_id": task.id,
        "campaign_id": c.id,
        "message": "Backfill запущен — прогресс в /backfill-discussions/progress",
    }


@router.get("/campaigns/{campaign_id}/backfill-discussions/progress")
async def backfill_discussion_joins_progress(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Snapshot прогресса backfill'а. UI поллит каждые 2-3 сек."""
    await _get_campaign(db, campaign_id, current_user.id)   # ownership check
    import redis as _rl, json as _json, os as _os
    r = _rl.from_url(_os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    raw = r.get(f"backfill:discussions:{campaign_id}:progress")
    if not raw:
        return {"status": "idle"}
    try:
        return _json.loads(raw)
    except Exception:
        return {"status": "idle"}


@router.get("/campaigns/{campaign_id}/plans")
async def get_campaign_plans(
    campaign_id: int,
    day: int = None,
    with_sessions: bool = True,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Планы кампании — расписание каждого аккаунта.

    with_sessions=false отдаёт всё то же самое, но без поминутного
    расписания сессий. Оно занимает почти весь объём ответа: у кампании
    на 22 дня получалось восемь мегабайт, из которых расписание — семь
    с половиной. В интерфейсе дни свёрнуты и раскрыт всегда один, так
    что остальное грузилось впустую. Заголовку дня хватает счётчиков,
    а расписание берётся отдельным запросом с day=N при раскрытии.
    """
    c = await _get_campaign(db, campaign_id, current_user.id)

    from models.campaign_plan import CampaignPlan
    from models.account import TelegramAccount

    query = select(CampaignPlan).where(CampaignPlan.campaign_id == c.id)
    if day:
        query = query.where(CampaignPlan.day_number == day)
    # Иначе — все дни кампании

    result = await db.execute(query.order_by(CampaignPlan.plan_date, CampaignPlan.account_id))
    plans = result.scalars().all()

    acc_ids = list(set(p.account_id for p in plans))
    acc_map = {}
    if acc_ids:
        accs = await db.execute(select(TelegramAccount).where(TelegramAccount.id.in_(acc_ids)))
        for a in accs.scalars().all():
            acc_map[a.id] = {"phone": a.phone, "name": a.first_name or a.phone}

    # ── Aggregate: сколько smart_comment реально прошло / скипнуто по (day, acc) ──
    # Ключ: (plan_date_iso, account_id) → count
    from models.campaign import CommentLog
    from models.warmup_log import WarmupLog
    from sqlalchemy import cast, Date, func, text

    plan_dates = list(set(p.plan_date for p in plans))
    written_map: dict[tuple[str, int], int] = {}
    failed_map: dict[tuple[str, int], int] = {}
    reacted_map: dict[tuple[str, int], int] = {}
    if plan_dates and acc_ids:
        # Успешные: CommentLog created_at.date() ∈ plan_dates
        # ── Дата берётся ПО МЕСТНОМУ ВРЕМЕНИ, а не по UTC ────────────
        # В журналах время в UTC, а план живёт по киевскому дню (UTC+3).
        # Из-за прямого cast(created_at, Date) всё, написанное с полуночи
        # до трёх ночи, попадало в предыдущую строку плана: день 6 показывал
        # ноль комментов, хотя шесть уже было написано, а день 5 их себе
        # приписывал. Прибавляем три часа перед приведением к дате.
        _LOCAL = text("INTERVAL '3 hours'")
        wr = await db.execute(
            select(
                cast(CommentLog.created_at + _LOCAL, Date).label("d"),
                CommentLog.account_id,
                func.count(),
            )
            .where(
                CommentLog.campaign_id == c.id,
                CommentLog.account_id.in_(acc_ids),
                cast(CommentLog.created_at + _LOCAL, Date).in_(plan_dates),
            )
            .group_by(cast(CommentLog.created_at + _LOCAL, Date), CommentLog.account_id)
        )
        for d, aid, cnt in wr.all():
            written_map[(d.isoformat(), aid)] = cnt

        # Провалы: WarmupLog action='smart_comment' success=False
        fl = await db.execute(
            select(
                cast(WarmupLog.created_at + _LOCAL, Date).label("d"),
                WarmupLog.account_id,
                func.count(),
            )
            .where(
                WarmupLog.campaign_id == c.id,
                WarmupLog.account_id.in_(acc_ids),
                WarmupLog.action == "smart_comment",
                WarmupLog.success == False,
                cast(WarmupLog.created_at + _LOCAL, Date).in_(plan_dates),
            )
            .group_by(cast(WarmupLog.created_at + _LOCAL, Date), WarmupLog.account_id)
        )
        for d, aid, cnt in fl.all():
            failed_map[(d.isoformat(), aid)] = cnt

        # ── Реакции на чужие комменты ────────────────────────────────
        # Отдельный счётчик, чтобы в плане было видно не только сколько
        # реакций заложено, но и сколько реально встало.
        rc = await db.execute(
            select(cast(WarmupLog.created_at + _LOCAL, Date), WarmupLog.account_id,
                   func.count().label("cnt"))
            .where(
                WarmupLog.account_id.in_(acc_ids),
                WarmupLog.action == "react_to_comment",
                WarmupLog.success == True,
                cast(WarmupLog.created_at + _LOCAL, Date).in_(plan_dates),
            )
            .group_by(cast(WarmupLog.created_at + _LOCAL, Date), WarmupLog.account_id)
        )
        for d, aid, cnt in rc.all():
            reacted_map[(d.isoformat(), aid)] = cnt

    out = []
    for p in plans:
        acc = acc_map.get(p.account_id, {"phone": "?", "name": "?"})
        sessions = p.plan.get("sessions", [])
        key = (p.plan_date.isoformat(), p.account_id)
        out.append({
            "id": p.id,
            "account_id": p.account_id,
            "account_phone": acc["phone"],
            "account_name": acc["name"],
            "day_number": p.day_number,
            "plan_date": p.plan_date.isoformat(),
            "personality": p.plan.get("personality", "?"),
            "mood": p.plan.get("mood", "?"),
            "total_comments": p.total_comments,
            "smart_written": written_map.get(key, 0),
            "smart_failed": failed_map.get(key, 0),
            # Реакции на чужие комменты: сколько заложено в плане и сколько
            # реально поставлено. Считаем прямо из плана, чтобы цифра не
            # разъезжалась с тем, что увидит исполнитель.
            "total_reactions": sum(
                int(a.get("count", 1))
                for sx in sessions for a in (sx.get("actions") or [])
                if a.get("type") == "react_to_comment"
            ),
            "reactions_done": reacted_map.get(key, 0),
            "total_sessions": len(sessions),
            "executed_idx": p.executed_idx,
            "status": p.status,
            "sessions": sessions if with_sessions else [],
        })

    return out

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

    # Сначала получаем id всех кампаний пользователя
    user_camps_r = await db.execute(
        select(Campaign.id).where(Campaign.user_id == current_user.id)
    )
    user_camp_ids = [r[0] for r in user_camps_r.all()]

    if not user_camp_ids:
        return []

    query = select(CommentLog).where(CommentLog.campaign_id.in_(user_camp_ids))
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
        "created_at": l.created_at.isoformat() + "Z" if l.created_at else None,
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

    # ── 2. Warmup логи (включая plan_executor) ───────────
    # Логи от plan_executor имеют task_id=NULL, ищем по account_id
    account_ids = c.account_ids or []
    
    # ТОЛЬКО логи комментинга — по source='commenting' ИЛИ по campaign_id=этой кампании
    warmup_logs = []
    if account_ids:
        commenting_logs_result = await db.execute(
            select(WarmupLog)
            .where(
                WarmupLog.account_id.in_(account_ids),
                WarmupLog.created_at >= (c.started_at or c.created_at),
                # Явная привязка к кампании ИЛИ помечено как commenting
                (WarmupLog.campaign_id == c.id) | (WarmupLog.source == 'commenting'),
            )
            .order_by(WarmupLog.created_at.desc())
            .limit(limit)
        )
        warmup_logs.extend(commenting_logs_result.scalars().all())
    
    # Логи от warmup_v2 (с task_id через warmup_tasks)
    wt_result = await db.execute(
        select(WarmupTask.id).where(
            WarmupTask.account_id.in_(account_ids) if account_ids else WarmupTask.campaign_id == c.id,
        )
    )
    wt_ids = [r[0] for r in wt_result.all()]
    
    if wt_ids:
        wl_result = await db.execute(
            select(WarmupLog)
            .where(WarmupLog.task_id.in_(wt_ids))
            .order_by(WarmupLog.created_at.desc())
            .limit(limit)
        )
        warmup_logs.extend(wl_result.scalars().all())

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
            "created_at": q.created_at.isoformat() + "Z" if q.executed_at else None,
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