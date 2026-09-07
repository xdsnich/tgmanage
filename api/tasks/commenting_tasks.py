"""
GramGPT — tasks/commenting_tasks.py  (v2 — Queue-based)
Нейрокомментинг v2:
  1. Веб-парсинг публичных каналов (без аккаунта, 0 риск)
  2. Вместо моментальной отправки — ставим комментарии в очередь (comment_queue)
  3. Round-robin выбор аккаунтов (не random.choice)
  4. Per-account лимиты из account_behavior
  5. Min 3 дня прогрева перед комментированием
  6. 80% комментариев в первые 30 мин, 20% позже

Отправка комментариев → comment_executor.py (отдельный процесс)
"""

import asyncio, sys, os, logging
from datetime import datetime, timedelta
from celery_app import celery_app

logger = logging.getLogger(__name__)
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Round-robin state: campaign_id -> last used index in account_ids
_round_robin_idx: dict[int, int] = {}


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try: return loop.run_until_complete(coro)
    finally: loop.close()


def _val(x):
    return x.value if hasattr(x, 'value') else x


def _should_comment(mode, pct, keywords, text):
    import random
    if mode == "all": return True
    if mode == "random": return random.randint(1, 100) <= pct
    if mode == "keywords": return any(k.lower() in text.lower() for k in keywords)
    return False


def _pick_account_round_robin(campaign_id: int, account_ids: list[int]) -> list[int]:
    """Round-robin через Redis — переживает рестарты."""
    if not account_ids:
        return []
    try:
        import redis as redis_lib
        r = redis_lib.Redis()
        key = f"gramgpt:rr:{campaign_id}"
        idx = r.incr(key) - 1
        r.expire(key, 86400)
    except Exception:
        # Fallback на память если Redis недоступен
        idx = _round_robin_idx.get(campaign_id, 0)
        _round_robin_idx[campaign_id] = idx + 1
    idx = idx % len(account_ids)
    return account_ids[idx:] + account_ids[:idx]


async def _process_campaign(c, db):
    """
    Веб-парсинг → проверка лимитов → постановка в очередь (НЕ отправка).
    """
    if API_DIR not in sys.path: sys.path.insert(0, API_DIR)

    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.campaign import TargetChannel, CampaignStatus
    from models.account import TelegramAccount
    from models.comment_queue import CommentQueue
    from services.channel_monitor import fetch_latest_posts_web
    from tasks.behavior_engine import (
        get_or_create_behavior, check_account_can_comment,
        check_warmup_age, assign_personality, assign_timing_profile,
        assign_style_profile, get_comment_delay,
    )

    logger.info(f"[{c.name}] {c.comments_sent}/{c.max_comments}")

    # Проверка завершения кампании
    if c.comments_sent >= c.max_comments:
        c.status = CampaignStatus.finished; c.finished_at = datetime.utcnow(); return
    if c.started_at and (datetime.utcnow() - c.started_at).total_seconds() / 3600 >= c.max_hours:
        c.status = CampaignStatus.finished; c.finished_at = datetime.utcnow(); return

    # Загрузка каналов
    ch_r = await db.execute(
        select(TargetChannel).where(TargetChannel.campaign_id == c.id, TargetChannel.is_active == True)
    )
    channels = ch_r.scalars().all()
    if not channels or not c.account_ids:
        return

    # Загрузка аккаунтов + behaviors
    account_behaviors = []
    for acc_id in c.account_ids:
        acc_r = await db.execute(
            select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
            .where(TelegramAccount.id == acc_id)
        )
        acc = acc_r.scalar_one_or_none()
        if not acc or not acc.session_file:
            continue
        if _val(acc.status) not in ("active", "unknown"):
            continue

        behavior = await get_or_create_behavior(db, acc.id, acc.phone)
        account_behaviors.append((acc, behavior))

    if not account_behaviors:
        logger.info(f"[{c.name}] Нет доступных аккаунтов")
        return

    # check_only: заранее вытаскиваем каналы где мы УЖЕ комментили
    # (по CommentLog за всё время этой кампании) — их нужно пропустить,
    # каждый канал получает ровно ОДИН коммент от нашей кампании.
    _is_check_only = getattr(c, "campaign_type", "regular") == "check_only"
    _already_commented_ch: set[str] = set()
    if _is_check_only:
        from models.campaign import CommentLog as _CL2
        _cl_r = await db.execute(
            select(_CL2.channel_username).where(_CL2.campaign_id == c.id).distinct()
        )
        _already_commented_ch = {r[0] for r in _cl_r.all() if r[0]}

    # Проверяем каналы
    for channel in channels:
        if c.comments_sent >= c.max_comments:
            break
        if not channel.username:
            continue

        # check_only: пропускаем каналы где уже писали (1 коммент/канал)
        if _is_check_only and channel.username in _already_commented_ch:
            continue

        logger.info(f"[{c.name}] [WEB] @{channel.username} (last={channel.last_post_id})")

        # Веб-парсинг
        new_posts = await fetch_latest_posts_web(channel.username, channel.last_post_id)
        if not new_posts:
            continue

        # Первый запуск — запоминаем без комментирования
        if channel.last_post_id == 0:
            latest = max(p.post_id for p in new_posts)
            channel.last_post_id = latest
            await db.commit()
            logger.info(f"[{c.name}] @{channel.username}: первый запуск → last={latest}")
            continue

        # Берём только самый свежий пост
        post = new_posts[-1]
        logger.info(f"[{c.name}] @{channel.username}: новый пост #{post.post_id}")

        channel.last_post_id = post.post_id
        await db.commit()

        if not _should_comment(_val(c.trigger_mode), c.trigger_percent, c.trigger_keywords or [], post.text):
            logger.info(f"[{c.name}] Пост #{post.post_id}: не проходит триггер")
            continue

        # ── Round-robin выбор аккаунта ──────────────────────
        ordered_ids = _pick_account_round_robin(c.id, [ab[0].id for ab in account_behaviors])
        selected_acc = None
        selected_behavior = None

        for acc_id in ordered_ids:
            acc, behavior = next((ab for ab in account_behaviors if ab[0].id == acc_id), (None, None))
            if not acc or not behavior:
                continue

            # Проверка лимитов (phone → динамический таргет 2-7 из get_daily_target)
            can_comment, limit_reason = check_account_can_comment(behavior, channel.username, phone=acc.phone)
            if not can_comment:
                logger.info(f"[{c.name}] Аккаунт {acc.phone}: {limit_reason}")
                continue

            selected_acc = acc
            selected_behavior = behavior
            break

        if not selected_acc:
            logger.info(f"[{c.name}] Все аккаунты заняты/на кулдауне для @{channel.username} — пост #{post.post_id} ПРОПУЩЕН, ждём следующий")
            continue

        # ── Ставим в очередь ────────────────────────────────
        personality = assign_personality(selected_acc.phone)
        timing = assign_timing_profile(selected_acc.phone)
        style = assign_style_profile(selected_acc.phone)

        delay_seconds = get_comment_delay(timing)
        scheduled_at = datetime.utcnow() + timedelta(seconds=delay_seconds)

        # ── NIGHT BOOST ─────────────────────────────────────
        # По умолчанию scheduled_at попадает в +5мин..+2ч от момента поста.
        # Целевые каналы почти не постят ночью → комментинг днём кучкой.
        # Юзер просил ~2 ночных сессии + 1 дневная. Реализация:
        #   С 30% шансом сдвигаем scheduled_at в ближайшее ночное окно
        #   01:00-05:00 UTC+3 (= 22:00-02:00 UTC). Это даёт ~1/3 ночных,
        #   что примерно соответствует "2 ночных из 5-6 сессий в сутки".
        # Cooldown 60-180 мин проверяется в comment_executor при взятии из
        # queue — если акк комментил час назад, ночная задача подождёт.
        import random as _r
        if _r.random() < 0.30:
            now_utc = datetime.utcnow()
            now_local = now_utc + timedelta(hours=3)   # UTC+3
            night_h = _r.randint(1, 4)                  # 01..04 local
            night_m = _r.randint(0, 59)
            # Если сейчас уже после 05:00 local — ночь = завтра
            if now_local.hour >= 5:
                night_local = (now_local + timedelta(days=1)).replace(
                    hour=night_h, minute=night_m, second=0, microsecond=0
                )
            else:
                night_local = now_local.replace(
                    hour=night_h, minute=night_m, second=0, microsecond=0
                )
            # +- 15 мин jitter чтобы не было ровно на минуте
            night_local += timedelta(minutes=_r.randint(-15, 15))
            scheduled_at = night_local - timedelta(hours=3)  # обратно UTC
            logger.info(
                f"[{c.name}] 🌙 Night-boost: @{channel.username} #{post.post_id} "
                f"сдвинут на {(scheduled_at + timedelta(hours=3)).strftime('%H:%M')} local"
            )

        # Проверяем дубликат в очереди
        existing_q = await db.execute(
            select(CommentQueue).where(
                CommentQueue.campaign_id == c.id,
                CommentQueue.channel == channel.username,
                CommentQueue.post_id == post.post_id,
                CommentQueue.status.in_(["scheduled", "executing"]),
            )
        )
        if existing_q.scalar_one_or_none():
            logger.info(f"[{c.name}] Пост #{post.post_id} уже в очереди")
            continue

        queue_item = CommentQueue(
            campaign_id=c.id,
            account_id=selected_acc.id,
            channel=channel.username,
            post_id=post.post_id,
            post_text=post.text[:2000],
            personality={**personality, "llm_provider": _val(c.llm_provider)},
            style=style,
            status="scheduled",
            scheduled_at=scheduled_at,
        )
        db.add(queue_item)

        logger.info(
            f"[{c.name}] 📋 В очередь: @{channel.username} #{post.post_id} → "
            f"аккаунт {selected_acc.phone} ({personality['name']}/{timing['name']}/{style['name']}) "
            f"через {delay_seconds}с ({scheduled_at.strftime('%H:%M:%S')})"
        )

    await db.commit()


async def _process_all_campaigns():
    logger.warning("[DEPRECATED] _process_all_campaigns — используйте plan_executor")
    return {"processed": 0, "deprecated": True}
    if API_DIR not in sys.path: sys.path.insert(0, API_DIR)
    from sqlalchemy import select
    from models.campaign import Campaign, CampaignStatus
    from utils.db_pool import async_session as Session

    async with Session() as db:
        try:
            result = await db.execute(select(Campaign).where(Campaign.status == CampaignStatus.active))
            campaigns = result.scalars().all()
            if not campaigns: return {"processed": 0}
            for c in campaigns: await _process_campaign(c, db)
            await db.commit()
            return {"processed": len(campaigns)}
        except Exception as e:
            logger.error(f"Ошибка: {e}"); await db.rollback()
            return {"error": str(e)}


@celery_app.task(bind=True, name="tasks.commenting_tasks.process_campaigns")
def process_campaigns(self):
    """Веб-парсинг → очередь комментариев (v2)."""
    self.update_state(state="PROGRESS", meta={"message": "Веб-парсинг + очередь..."})
    return run_async(_process_all_campaigns())
