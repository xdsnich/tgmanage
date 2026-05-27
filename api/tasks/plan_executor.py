"""
GramGPT — tasks/plan_executor.py
Выполняет планы кампаний — параллельно, по одной сессии на задачу.

Архитектура:
  dispatch_plans (<1с) → execute_plan_session(plan_id) × N параллельно

Все импорты моделей — внутри функций (lazy import).

ЗАЩИТА ОТ ОСИРОТЕВШИХ ПЛАНОВ:
- dispatch_plans делает JOIN с WarmupTask/Campaign, фильтруя мёртвые
- execute_plan_session перед выполнением проверяет что задача жива и running
- Если задача удалена/остановлена — план помечается как orphan и не выполняется
"""

import asyncio
import sys
import os
import random
import logging
from datetime import datetime, timedelta

from celery_app import celery_app

logger = logging.getLogger(__name__)
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _safe_log(db, **kwargs):
    """Логирует в warmup_logs без краша. Если ошибка — пропускает."""
    try:
        from models.warmup_log import WarmupLog
        if 'source' not in kwargs:
            kwargs['source'] = 'warmup'
        db.add(WarmupLog(**kwargs))
        await db.flush()
    except Exception:
        try:
            await db.rollback()
        except:
            pass


def _val(x):
    return x.value if hasattr(x, 'value') else x


# ═══════════════════════════════════════════════════════════
# ДИСПЕТЧЕР: находит сессии которые пора выполнить
# ═══════════════════════════════════════════════════════════

async def _dispatch_plans():
    """Лёгкий (<1с): находит campaign_plans с сессиями которые пора выполнить."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy import select, delete as sa_delete
    from models.campaign_plan import CampaignPlan
    from models.campaign import Campaign, CampaignStatus
    from models.warmup import WarmupTask
    from utils.db_pool import async_session as Session

    dispatched = 0
    skipped = 0
    cleaned = 0

    try:
        async with Session() as db:
            now = datetime.utcnow()
            local_now = now + timedelta(hours=3)  # UTC+3 Lviv
            today = local_now.date()
            current_hour = local_now.hour
            current_minute = local_now.minute

            # ═══ САНИТАРИЯ: удалить осиротевшие планы ═══
            # Планы без живой кампании
            orphan_camp = await db.execute(
                sa_delete(CampaignPlan).where(
                    CampaignPlan.campaign_id != None,
                    CampaignPlan.campaign_id.notin_(select(Campaign.id)),
                )
            )
            if orphan_camp.rowcount:
                cleaned += orphan_camp.rowcount
                logger.warning(f"[plan_dispatch] Удалено осиротевших campaign-планов: {orphan_camp.rowcount}")

            # Планы без живой warmup_task
            orphan_wt = await db.execute(
                sa_delete(CampaignPlan).where(
                    CampaignPlan.warmup_task_id != None,
                    CampaignPlan.warmup_task_id.notin_(select(WarmupTask.id)),
                )
            )
            if orphan_wt.rowcount:
                cleaned += orphan_wt.rowcount
                logger.warning(f"[plan_dispatch] Удалено осиротевших warmup-планов: {orphan_wt.rowcount}")

            if cleaned > 0:
                await db.commit()

            # ═══ COMMENTING планы: только для active кампаний ═══
            comm_result = await db.execute(
                select(CampaignPlan)
                .join(Campaign, Campaign.id == CampaignPlan.campaign_id)
                .where(
                    CampaignPlan.plan_date == today,
                    CampaignPlan.status == "active",
                    Campaign.status == CampaignStatus.active,
                )
            )
            plans = list(comm_result.scalars().all())

            # ═══ WARMUP планы: только для running задач ═══
            warmup_result = await db.execute(
                select(CampaignPlan)
                .join(WarmupTask, WarmupTask.id == CampaignPlan.warmup_task_id)
                .where(
                    CampaignPlan.plan_date == today,
                    CampaignPlan.status == "active",
                    CampaignPlan.campaign_id == None,
                    CampaignPlan.warmup_task_id != None,
                    WarmupTask.status == "running",
                )
            )
            warmup_plans = list(warmup_result.scalars().all())
            plans.extend(warmup_plans)

            for plan in plans:
                sessions = plan.plan.get("sessions", [])
                if plan.executed_idx >= len(sessions):
                    plan.status = "done"
                    skipped += 1
                    continue

                next_session = sessions[plan.executed_idx]

                if next_session.get("skipped"):
                    plan.executed_idx += 1
                    logger.info(f"[plan] Пропуск сессии #{plan.executed_idx} (акк {plan.account_id}): {next_session.get('skip_reason', '?')}")
                    if plan.executed_idx >= len(sessions):
                        plan.status = "done"
                    skipped += 1
                    continue

                sess_hour = next_session.get("connect_at_hour", 0)
                sess_min = next_session.get("connect_at_minute", 0)

                if current_hour < sess_hour:
                    skipped += 1
                    continue
                if current_hour == sess_hour and current_minute < sess_min:
                    skipped += 1
                    continue

                # Не слишком ли поздно? (если опоздали больше чем на 2 часа — пропускаем)
                sess_total_min = sess_hour * 60 + sess_min
                curr_total_min = current_hour * 60 + current_minute
                if curr_total_min - sess_total_min > 120:
                    plan.executed_idx += 1
                    logger.info(f"[plan] Пропуск просроченной сессии (акк {plan.account_id})")
                    if plan.executed_idx >= len(sessions):
                        plan.status = "done"
                    skipped += 1
                    continue

                # Отправляем задачу
                celery_app.send_task(
                    "tasks.plan_executor.execute_plan_session",
                    args=[plan.id],
                    queue="plans",
                )
                dispatched += 1

            # ═══ AUTO-CLOSE кампаний ═══
            from sqlalchemy import func
            active_campaigns = (await db.execute(
                select(Campaign).where(Campaign.status == CampaignStatus.active)
            )).scalars().all()

            logger.info(f"[autoclose] Проверка {len(active_campaigns)} активных кампаний")

            for camp in active_campaigns:
                logger.info(f"[autoclose]   camp {camp.id}: started_at={camp.started_at}, max_hours={camp.max_hours}, comments={camp.comments_sent}/{camp.max_comments}")

                # 1. Время истекло?
                if camp.started_at and camp.max_hours and camp.max_hours > 0:
                    started = camp.started_at
                    if hasattr(started, 'tzinfo') and started.tzinfo is not None:
                        started = started.replace(tzinfo=None)
                    now_utc = datetime.utcnow()
                    delta = now_utc - started
                    elapsed_sec = delta.total_seconds()

                    if elapsed_sec < 0:
                        logger.warning(f"[autoclose]   camp {camp.id}: started_at В БУДУЩЕМ! — пропуск")
                        continue

                    elapsed_hours = elapsed_sec / 3600
                    logger.info(f"[autoclose]   camp {camp.id}: прошло {elapsed_hours:.2f}ч / {camp.max_hours}ч")

                    if elapsed_hours >= camp.max_hours:
                        camp.status = CampaignStatus.finished
                        if not getattr(camp, 'finished_at', None):
                            camp.finished_at = datetime.utcnow()
                        logger.info(f"[autoclose] Кампания {camp.id} → finished (время истекло: {elapsed_hours:.1f}ч / {camp.max_hours}ч)")
                        continue

                # 2. Лимит комментов достигнут?
                if camp.max_comments and camp.comments_sent >= camp.max_comments:
                    camp.status = CampaignStatus.finished
                    if not getattr(camp, 'finished_at', None):
                        camp.finished_at = datetime.utcnow()
                    logger.info(f"[autoclose] Кампания {camp.id} → finished (лимит комментов {camp.comments_sent}/{camp.max_comments})")
                    continue

                # 3. Все планы выполнены?
                remaining = (await db.execute(
                    select(func.count(CampaignPlan.id)).where(
                        CampaignPlan.campaign_id == camp.id,
                        CampaignPlan.status == "active",
                    )
                )).scalar() or 0

                if remaining == 0:
                    future = (await db.execute(
                        select(func.count(CampaignPlan.id)).where(
                            CampaignPlan.campaign_id == camp.id,
                            CampaignPlan.plan_date > today,
                        )
                    )).scalar() or 0

                    if future == 0:
                        camp.status = CampaignStatus.finished
                        if not getattr(camp, 'finished_at', None):
                            camp.finished_at = datetime.utcnow()
                        logger.info(f"[autoclose] Кампания {camp.id} → finished (все планы выполнены)")

            await db.commit()
            logger.info(f"[plan_dispatch] Отправлено {dispatched}, пропущено {skipped}, очищено {cleaned}")

    except Exception as e:
        logger.error(f"[plan_dispatch] Ошибка: {e}")

    return {"dispatched": dispatched, "skipped": skipped, "cleaned": cleaned}


# ═══════════════════════════════════════════════════════════
# EXECUTOR: выполняет одну сессию одного аккаунта
# ═══════════════════════════════════════════════════════════

async def _execute_plan_session(plan_id: int):
    """Выполняет одну сессию из плана."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.campaign_plan import CampaignPlan
    from models.campaign import Campaign, CampaignStatus, CommentLog
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from models.warmup_log import WarmupLog
    from models.warmup import WarmupTask
    from utils.telegram import make_telethon_client
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.user_lock import acquire_user_slot, release_user_slot
    from utils.db_pool import async_session as Session
    from services.llm import generate_comment, build_comment_prompt
    from tasks.behavior_engine import assign_style_profile

    async with Session() as db:
        try:
            # ── Загружаем план ────────────────────────────
            plan = (await db.execute(
                select(CampaignPlan).where(CampaignPlan.id == plan_id)
            )).scalar_one_or_none()

            if not plan:
                logger.warning(f"[plan_executor] Plan #{plan_id} не найден — пропуск")
                return {"status": "plan_not_found"}

            if plan.status != "active":
                logger.info(f"[plan_executor] Plan #{plan_id} status={plan.status} — пропуск")
                return {"status": "not_active"}

            # Определяем тип плана: warmup или commenting
            plan_source = 'warmup' if plan.warmup_task_id else 'commenting'
            plan_campaign_id = plan.campaign_id if not plan.warmup_task_id else None

            # ═══ ПРОВЕРКА ЖИВОСТИ СВЯЗАННОЙ ЗАДАЧИ ═══
            if plan.warmup_task_id:
                wt = (await db.execute(
                    select(WarmupTask).where(WarmupTask.id == plan.warmup_task_id)
                )).scalar_one_or_none()
                if not wt:
                    logger.warning(f"[plan_executor] Plan #{plan_id} — WarmupTask {plan.warmup_task_id} УДАЛЕНА. Удаляем план.")
                    await db.delete(plan)
                    await db.commit()
                    return {"status": "orphan_warmup_task"}
                if wt.status != "running":
                    logger.info(f"[plan_executor] Plan #{plan_id} — WarmupTask {plan.warmup_task_id} status={wt.status}, пропуск")
                    return {"status": "warmup_not_running"}
            elif plan.campaign_id:
                camp = (await db.execute(
                    select(Campaign).where(Campaign.id == plan.campaign_id)
                )).scalar_one_or_none()
                if not camp:
                    logger.warning(f"[plan_executor] Plan #{plan_id} — Campaign {plan.campaign_id} УДАЛЕНА. Удаляем план.")
                    await db.delete(plan)
                    await db.commit()
                    return {"status": "orphan_campaign"}
                if _val(camp.status) != "active":
                    logger.info(f"[plan_executor] Plan #{plan_id} — Campaign {plan.campaign_id} status={_val(camp.status)}, пропуск")
                    return {"status": "campaign_not_active"}

            logger.info(f"[plan_executor] Plan #{plan_id} тип: {plan_source.upper()}")

            sessions = plan.plan.get("sessions", [])
            if plan.executed_idx >= len(sessions):
                plan.status = "done"
                await db.commit()
                return {"status": "done"}

            session = sessions[plan.executed_idx]

            if session.get("skipped"):
                plan.executed_idx += 1
                if plan.executed_idx >= len(sessions):
                    plan.status = "done"
                await db.commit()
                return {"status": "skipped"}

            actions = session.get("actions", [])
            if not actions:
                plan.executed_idx += 1
                await db.commit()
                return {"status": "empty_session"}

            # ── Lock аккаунта ─────────────────────────────
            from utils.connection_limiter import check_connection_limit, increment_connection
            if not check_connection_limit(plan.account_id):
                return {"status": "daily_limit"}

            if not acquire_account_lock(plan.account_id, ttl=1800):
                logger.info(f"[plan] Аккаунт {plan.account_id} занят (lock) — пропуск")
                return {"status": "locked"}

            slot_user_id = None  # для release в finally
            try:
                acc = (await db.execute(
                    select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
                    .where(TelegramAccount.id == plan.account_id)
                )).scalar_one_or_none()

                if not acc or acc.status not in ("active", "unknown"):
                    plan.executed_idx += 1
                    await db.commit()
                    return {"status": "inactive"}

                # Per-user concurrency limit — защита от того что один юзер
                # с 500 аккаунтами съест всю мощность воркера
                if not acquire_user_slot(acc.user_id):
                    logger.info(f"[plan] User {acc.user_id}: лимит одновременных сессий, пропуск")
                    return {"status": "user_at_limit"}
                slot_user_id = acc.user_id

                proxy = None
                if acc.proxy_id:
                    proxy = (await db.execute(
                        select(Proxy).where(Proxy.id == acc.proxy_id)
                    )).scalar_one_or_none()

                client = make_telethon_client(acc, proxy)
                if not client:
                    plan.executed_idx += 1
                    await db.commit()
                    return {"status": "no_client"}

                campaign = None
                if plan.campaign_id:
                    campaign = (await db.execute(
                        select(Campaign).where(Campaign.id == plan.campaign_id)
                    )).scalar_one_or_none()

                phone = acc.phone
                done_actions = 0
                sess_num = plan.executed_idx + 1
                total_sess = len(sessions)
                mood = plan.plan.get('mood', '?')

                logger.info(f"[plan][{phone}] ═══ Сессия {sess_num}/{total_sess} (день {plan.day_number}, {mood}) ═══")

                await _safe_log(db, task_id=None, account_id=acc.id, action="session_start",
                                 detail=f"Старт сессии {sess_num}/{total_sess} (день {plan.day_number}, {mood})",
                                 success=True, source=plan_source, campaign_id=plan_campaign_id)

                try:
                    await client.connect()
                    increment_connection(plan.account_id)
                    # История подключений в БД
                    from services.connection_logger import log_connection
                    await log_connection(db, plan.account_id, source=plan_source, proxy_id=proxy.id if proxy else None)
                    if not await client.is_user_authorized():
                        plan.executed_idx += 1
                        logger.warning(f"[plan][{phone}] Не авторизован")
                        await _safe_log(db, task_id=None, account_id=acc.id, action="error",
                                         detail="Не авторизован", success=False,
                                         source=plan_source, campaign_id=plan_campaign_id)
                        await db.commit()
                        return {"status": "not_authorized"}

                    logger.info(f"[plan][{phone}] Подключен, {len(actions)} действий")

                    for action_num, action in enumerate(actions, 1):
                        try:
                            action_type = action.get("type", "")

                            if action_type == "idle":
                                dur = action.get("duration", 30)
                                logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] ⏸ Пауза {dur}с")
                                await asyncio.sleep(dur)
                                continue

                            elif action_type == "read_feed":
                                channel_name = action.get("channel")
                                count = action.get("count", 5)
                                try:
                                    if channel_name:
                                        entity = await client.get_entity(channel_name)
                                    else:
                                        dialogs = await client.get_dialogs(limit=15)
                                        if dialogs:
                                            entity = random.choice(dialogs[:10])
                                        else:
                                            continue

                                    msgs = await client.get_messages(entity, limit=count)
                                    for m in msgs:
                                        await client.send_read_acknowledge(entity, m)
                                        await asyncio.sleep(random.uniform(0.5, 4))

                                    name = channel_name or getattr(entity, 'name', '?') or getattr(entity, 'title', '?')
                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📖 Прочитал {len(msgs)} постов в «{name}»")
                                    await _safe_log(db, task_id=None, account_id=acc.id, action="read_feed",
                                                     detail=f"Прочитал {len(msgs)} в «{name}»",
                                                     channel=str(name), success=True,
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    done_actions += 1
                                except Exception as e:
                                    logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📖 read_feed ошибка: {e}")

                            elif action_type == "view_stories":
                                try:
                                    from telethon.tl.functions.stories import GetAllReadPeerStoriesRequest
                                    await client(GetAllReadPeerStoriesRequest())
                                    count = action.get("count", 3)
                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 👁 Stories ({count})")
                                    await _safe_log(db, task_id=None, account_id=acc.id, action="view_stories",
                                                     detail=f"Просмотрел stories ({count})", success=True,
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    done_actions += 1
                                except Exception:
                                    pass

                            elif action_type == "set_reaction":
                                try:
                                    from telethon.tl.functions.messages import SendReactionRequest
                                    from telethon.tl.types import ReactionEmoji
                                    from telethon.tl.functions.channels import GetFullChannelRequest
                                    from telethon.errors import ReactionInvalidError
                                    channel_name = action.get("channel")

                                    if channel_name:
                                        entity = await client.get_entity(channel_name)
                                    else:
                                        dialogs = await client.get_dialogs(limit=10)
                                        channels = [d for d in dialogs if getattr(d, 'is_channel', False)]
                                        if not channels:
                                            continue
                                        entity = random.choice(channels)

                                    # Разбираем available_reactions канала.
                                    # ChatReactionsSome → только из whitelist
                                    # ChatReactionsAll / None → весь стандартный набор
                                    # ChatReactionsNone → реакции запрещены, скип
                                    allowed_emojis = []
                                    skip = False
                                    try:
                                        full = await client(GetFullChannelRequest(entity))
                                        available = getattr(full.full_chat, 'available_reactions', None)
                                        cls = type(available).__name__ if available is not None else "None"
                                        if cls == "ChatReactionsSome":
                                            for r in getattr(available, 'reactions', []):
                                                if hasattr(r, 'emoticon'):
                                                    allowed_emojis.append(r.emoticon)
                                        elif cls == "ChatReactionsNone":
                                            skip = True  # явно запрещены
                                        else:
                                            # ChatReactionsAll или None (legacy) → стандартные
                                            allowed_emojis = ["👍", "👎", "❤️", "🔥", "🥰", "👏", "😁",
                                                              "🤔", "🤯", "😱", "🤬", "😢", "🎉", "🤩", "🙏", "💯"]
                                    except Exception:
                                        allowed_emojis = ["👍", "❤️", "🔥", "👏"]

                                    if skip or not allowed_emojis:
                                        continue

                                    msgs = await client.get_messages(entity, limit=5)
                                    if not msgs:
                                        continue
                                    target_msg = random.choice(msgs)

                                    # Пробуем до 3 разных эмодзи — если канал отверг один, берём другой
                                    candidates = random.sample(allowed_emojis, min(3, len(allowed_emojis)))
                                    reacted_emoji = None
                                    for emoji in candidates:
                                        try:
                                            await client(SendReactionRequest(
                                                peer=entity, msg_id=target_msg.id,
                                                reaction=[ReactionEmoji(emoticon=emoji)]
                                            ))
                                            reacted_emoji = emoji
                                            break
                                        except ReactionInvalidError:
                                            continue

                                    if reacted_emoji:
                                        name = channel_name or getattr(entity, 'name', '?') or getattr(entity, 'title', '?')
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 😍 Реакция {reacted_emoji} в «{name}»")
                                        await _safe_log(db, task_id=None, account_id=acc.id, action="set_reaction",
                                                         detail=f"Реакция {reacted_emoji} в «{name}»",
                                                         channel=str(name), emoji=reacted_emoji, success=True,
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        done_actions += 1
                                except Exception as e:
                                    logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] 😍 reaction ошибка: {e}")

                            elif action_type == "view_profile":
                                try:
                                    dialogs = await client.get_dialogs(limit=15)
                                    users = [d for d in dialogs if getattr(d, 'is_user', False)]
                                    if users:
                                        u = random.choice(users[:5])
                                        from telethon.tl.functions.users import GetFullUserRequest
                                        await client(GetFullUserRequest(u.input_entity))
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 👤 Профиль «{u.name or '?'}»")
                                        await _safe_log(db, task_id=None, account_id=acc.id, action="view_profile",
                                                         detail=f"Просмотрел профиль «{u.name or '?'}»", success=True,
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        done_actions += 1
                                except Exception:
                                    pass

                            elif action_type == "search":
                                try:
                                    from telethon.tl.functions.contacts import SearchRequest
                                    terms = ["crypto", "news", "music", "sport", "tech", "games", "memes", "trade"]
                                    term = random.choice(terms)
                                    await client(SearchRequest(q=term, limit=5))
                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 🔍 Поиск «{term}»")
                                    await _safe_log(db, task_id=None, account_id=acc.id, action="search",
                                                     detail=f"Поиск «{term}»", success=True,
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    done_actions += 1
                                except Exception:
                                    pass

                            elif action_type == "send_saved":
                                try:
                                    text = action.get("text", "📌")
                                    me = await client.get_me()
                                    await client.send_message(me, text)
                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💬 Saved: {text}")
                                    await _safe_log(db, task_id=None, account_id=acc.id, action="send_saved",
                                                     detail=f"Saved: {text}", success=True,
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    done_actions += 1
                                except Exception:
                                    pass

                            elif action_type == "forward_saved":
                                try:
                                    dialogs = await client.get_dialogs(limit=10)
                                    channels = [d for d in dialogs if getattr(d, 'is_channel', False)]
                                    if channels:
                                        ch = random.choice(channels)
                                        msgs = await client.get_messages(ch, limit=5)
                                        if msgs:
                                            me = await client.get_me()
                                            await client.forward_messages(me, msgs[0])
                                            logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💾 Переслал из «{ch.name or '?'}»")
                                            await _safe_log(db, task_id=None, account_id=acc.id, action="forward_saved",
                                                             detail=f"Переслал из «{ch.name or '?'}»", success=True,
                                                             source=plan_source, campaign_id=plan_campaign_id)
                                            done_actions += 1
                                except Exception:
                                    pass

                            elif action_type == "reply_dm":
                                try:
                                    dialogs = await client.get_dialogs(limit=20)
                                    users = [d for d in dialogs if getattr(d, 'is_user', False) and d.unread_count > 0]
                                    if users:
                                        u = random.choice(users)
                                        msgs = await client.get_messages(u, limit=5)
                                        for m in msgs:
                                            await client.send_read_acknowledge(u, m)
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] ↩️ Прочитал ЛС от «{u.name or '?'}»")
                                        await _safe_log(db, task_id=None, account_id=acc.id, action="reply_dm",
                                                         detail=f"Прочитал ЛС от «{u.name or '?'}»", success=True,
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        done_actions += 1
                                except Exception:
                                    pass

                            elif action_type == "smart_comment":
                                target_channel = action.get("channel", "")
                                if not target_channel or not campaign:
                                    continue

                                # Guard: комментируем только в joined каналах.
                                # Применяется только для кампаний с assignment-записями
                                # (т.е. запущенных после migrate 024).
                                # Если таблица не создана — guard не мешает (обратная совместимость).
                                _allow_comment = True
                                if plan.campaign_id:
                                    try:
                                        from models.campaign_channel_assignment import CampaignChannelAssignment
                                        # begin_nested() = SAVEPOINT: если запрос упадёт (таблица не создана),
                                        # откатится только savepoint, внешняя транзакция не ломается.
                                        async with db.begin_nested():
                                            has_asgn = (await db.execute(
                                                select(CampaignChannelAssignment.id).where(
                                                    CampaignChannelAssignment.campaign_id == plan.campaign_id
                                                ).limit(1)
                                            )).scalar_one_or_none()
                                            if has_asgn is not None:
                                                # Кампания с organic funnel — применяем guard
                                                sub = (await db.execute(
                                                    select(CampaignChannelAssignment.id).where(
                                                        CampaignChannelAssignment.campaign_id      == plan.campaign_id,
                                                        CampaignChannelAssignment.account_id       == acc.id,
                                                        CampaignChannelAssignment.channel_username == target_channel,
                                                        CampaignChannelAssignment.status           == "joined",
                                                    )
                                                )).scalar_one_or_none()
                                                _allow_comment = (sub is not None)
                                    except Exception:
                                        pass  # Таблица не создана → старое поведение (разрешаем)

                                if not _allow_comment:
                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] ⏭ @{target_channel}: ещё не подписан — пропуск")
                                    continue

                                try:
                                    entity = await client.get_entity(target_channel)
                                    posts = await client.get_messages(entity, limit=random.randint(3, 8))
                                    for p in posts:
                                        await client.send_read_acknowledge(entity, p)
                                        await asyncio.sleep(random.uniform(0.5, 3))

                                    if not posts:
                                        continue

                                    target_post = posts[0]
                                    post_text = target_post.message or ""

                                    typing_dur = action.get("pause_before", random.randint(2, 20))
                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] ⌨️ Typing {typing_dur}с в @{target_channel}")
                                    try:
                                        from telethon.tl.functions.messages import SetTypingRequest, GetDiscussionMessageRequest
                                        from telethon.tl.types import SendMessageTypingAction
                                        disc = await client(GetDiscussionMessageRequest(peer=entity, msg_id=target_post.id))
                                        if disc and disc.messages:
                                            await client(SetTypingRequest(peer=disc.messages[0].peer_id, action=SendMessageTypingAction()))
                                    except Exception:
                                        pass
                                    await asyncio.sleep(typing_dur)

                                    if random.random() < random.uniform(0.05, 0.15):
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 🚫 Передумал комментировать @{target_channel}")
                                        await _safe_log(db, task_id=None, account_id=acc.id, action="smart_comment",
                                                         detail=f"🚫 Передумал комментировать @{target_channel}",
                                                         channel=target_channel, success=True,
                                                         source=plan_source, campaign_id=plan_campaign_id)
                                        done_actions += 1
                                        continue

                                    style_profile = assign_style_profile(phone)
                                    prompt = build_comment_prompt(post_text, style_profile, plan.plan)
                                    provider = plan.plan.get("personality_data", {}).get("llm_provider", "groq")
                                    if campaign:
                                        provider = _val(campaign.llm_provider)

                                    # Берём API ключ из БД (или fallback на env)
                                    from services.service_credentials import get_api_key
                                    api_key = await get_api_key(db, campaign.user_id, provider) if campaign else None

                                    comment_text = generate_comment(provider, prompt, post_text, api_key=api_key)
                                    if not comment_text:
                                        logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] ❌ LLM вернул пустой ответ")
                                        continue

                                    await client.send_message(entity=entity, message=comment_text, comment_to=target_post.id)

                                    try:
                                        from models.channel_ban_stats import ChannelBanStats
                                        st = (await db.execute(
                                            select(ChannelBanStats).where(
                                                ChannelBanStats.user_id == campaign.user_id,
                                                ChannelBanStats.channel_username == target_channel,
                                            )
                                        )).scalar_one_or_none()
                                        if not st:
                                            st = ChannelBanStats(
                                                user_id=campaign.user_id,
                                                channel_username=target_channel,
                                                total_attempts=0, banned_count=0,
                                            )
                                            db.add(st)
                                        st.total_attempts += 1
                                        st.last_updated = datetime.utcnow()
                                    except Exception as e:
                                        logger.warning(f"[stats] Ошибка записи: {e}")

                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💬 @{target_channel}: {comment_text[:60]}")

                                    if campaign:
                                        try:
                                            campaign.comments_sent += 1
                                            db.add(CommentLog(
                                                campaign_id=campaign.id, account_id=acc.id,
                                                account_phone=phone, channel_username=target_channel,
                                                channel_title="", post_id=target_post.id,
                                                post_text=post_text[:500],
                                                comment_text=comment_text,
                                                llm_provider=_val(campaign.llm_provider),
                                            ))
                                            await db.flush()
                                            await db.commit()
                                            logger.info(f"[plan][{phone}] ✅ CommentLog #{campaign.comments_sent} записан в БД (камп {campaign.id})")
                                        except Exception as log_err:
                                            logger.error(f"[plan][{phone}] ❌ Ошибка записи CommentLog: {log_err}")
                                            try: await db.rollback()
                                            except: pass

                                    await _safe_log(db, task_id=None, account_id=acc.id, action="smart_comment",
                                                     detail=f"💬 @{target_channel}: {comment_text[:80]}",
                                                     channel=target_channel, success=True,
                                                     source=plan_source, campaign_id=plan_campaign_id)

                                    done_actions += 1

                                except Exception as e:
                                    err = str(e)
                                    err_type = type(e).__name__
                                    logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] ❌ comment error ({err_type}): {err[:100]}")

                                    # ── Детектим бан двумя способами ─────
                                    # 1. По имени класса исключения (самый надёжный)
                                    ban_exception_classes = {
                                        "ChatWriteForbiddenError",
                                        "UserBannedInChannelError",
                                        "ChatRestrictedError",
                                        "ChannelPrivateError",
                                        "UserDeactivatedError",
                                        "UserDeactivatedBanError",
                                        "PeerIdInvalidError",  # часто означает "забанены/кикнуты"
                                    }
                                    # 2. По тексту сообщения (для случаев когда Telethon не задрафтил спецкласс)
                                    ban_message_patterns = [
                                        "can't write", "cannot write", "write in this chat",
                                        "banned from sending", "banned in this", "you're banned",
                                        "chat_write_forbidden", "user_banned", "user_restricted",
                                        "banned_rights", "channel_private", "you_blocked",
                                    ]
                                    is_ban = (
                                        err_type in ban_exception_classes or
                                        any(p.lower() in err.lower() for p in ban_message_patterns)
                                    )

                                    # ── Запись в channel_ban_stats ──
                                    # ВСЕГДА инкрементим total_attempts (даже на не-бан ошибках),
                                    # чтобы канал был виден в UI проходимости.
                                    # banned_count инкрементим только если is_ban.
                                    # transient-ошибки типа FLOOD_WAIT не считаем — это не вина канала.
                                    is_transient = (
                                        any(p in err for p in (
                                            "FLOOD_WAIT", "AUTH_KEY_UNREGISTERED",
                                            "PEER_FLOOD", "ServerError",
                                            "TimeoutError", "ConnectionError",
                                            # Telethon не знает новый Constructor ID от Telegram
                                            # (новая фича Telegram, нужен upgrade библиотеки).
                                            # Не вина канала, не вина аккаунта.
                                            "Could not find a matching Constructor",
                                            "TypeNotFoundError",
                                            # Подобные MTProto/parse ошибки которые сами по себе
                                            # не означают что нас банят
                                            "MTProtoError", "InvalidBufferError",
                                            "SecurityError",  # обычно битый пакет от сервера/прокси
                                        )) or
                                        err_type in (
                                            "TypeNotFoundError",
                                            "InvalidBufferError",
                                            "SecurityCheckMismatch",
                                            "AuthKeyDuplicatedError",  # отдельный кейс, аккаунт лезет с 2 мест
                                        )
                                    )

                                    if campaign and not is_transient:
                                        try:
                                            from models.channel_ban_stats import ChannelBanStats
                                            st = (await db.execute(
                                                select(ChannelBanStats).where(
                                                    ChannelBanStats.user_id == campaign.user_id,
                                                    ChannelBanStats.channel_username == target_channel,
                                                )
                                            )).scalar_one_or_none()
                                            if not st:
                                                st = ChannelBanStats(
                                                    user_id=campaign.user_id,
                                                    channel_username=target_channel,
                                                    total_attempts=0, banned_count=0,
                                                )
                                                db.add(st)
                                            st.total_attempts += 1
                                            if is_ban:
                                                st.banned_count += 1
                                                st.last_ban_reason = f"[{err_type}] {err[:180]}"
                                                logger.warning(f"[stats] 🚫 @{target_channel} БАН ({err_type}): {st.banned_count}/{st.total_attempts}")
                                            else:
                                                logger.info(f"[stats] ⚠ @{target_channel} ошибка-не-бан ({err_type}): {st.banned_count}/{st.total_attempts}")
                                            st.last_updated = datetime.utcnow()
                                        except Exception as se:
                                            logger.warning(f"[stats] Ошибка записи: {se}")

                                    await _safe_log(db, task_id=None, account_id=acc.id, action="smart_comment",
                                                     detail=f"❌ Ошибка @{target_channel}: {err[:100]}",
                                                     channel=target_channel, success=False, error=err[:200],
                                                     source=plan_source, campaign_id=plan_campaign_id)

                                    import re as _re
                                    if "FLOOD_WAIT" in err:
                                        wait = int(_re.search(r"(\d+)", err).group(1)) if _re.search(r"(\d+)", err) else 60
                                        logger.warning(f"[plan][{phone}] FLOOD_WAIT {wait}с — пауза")
                                        await asyncio.sleep(wait + random.randint(5, 15))
                                        break
                                    elif "PEER_FLOOD" in err or "AUTH_KEY_UNREGISTERED" in err:
                                        logger.error(f"[plan][{phone}] {err[:30]} — аккаунт frozen")
                                        acc.status = "frozen"
                                        break

                            elif action_type == "react_to_comment":
                                target_channel = action.get("channel", "")
                                react_count = action.get("count", 1)
                                if not target_channel:
                                    continue
                                try:
                                    from telethon.tl.functions.messages import SendReactionRequest
                                    from telethon.tl.functions.channels import GetFullChannelRequest
                                    from telethon.tl.types import ReactionEmoji
                                    from telethon.errors import ReactionInvalidError

                                    entity = await client.get_entity(target_channel)

                                    # Берём последний пост канала
                                    posts = await client.get_messages(entity, limit=1)
                                    if not posts:
                                        continue
                                    post = posts[0]

                                    # Ищем linked discussion group + available_reactions
                                    full = await client(GetFullChannelRequest(entity))
                                    linked_chat_id = getattr(full.full_chat, 'linked_chat_id', None)
                                    discussion = await client.get_entity(linked_chat_id) if linked_chat_id else entity

                                    # У discussion group свои available_reactions
                                    disc_full = full
                                    if linked_chat_id:
                                        try:
                                            disc_full = await client(GetFullChannelRequest(discussion))
                                        except Exception:
                                            pass

                                    allowed_emojis = []
                                    try:
                                        available = getattr(disc_full.full_chat, 'available_reactions', None)
                                        cls = type(available).__name__ if available is not None else "None"
                                        if cls == "ChatReactionsSome":
                                            for r in getattr(available, 'reactions', []):
                                                if hasattr(r, 'emoticon'):
                                                    allowed_emojis.append(r.emoticon)
                                        elif cls == "ChatReactionsNone":
                                            continue  # реакции в чате запрещены
                                        else:
                                            allowed_emojis = ["👍", "❤️", "🔥", "👏", "🤔", "😮", "🥰", "💯", "🎉"]
                                    except Exception:
                                        allowed_emojis = ["👍", "❤️", "🔥", "👏"]

                                    if not allowed_emojis:
                                        continue

                                    # Получаем комменты под постом
                                    comments = []
                                    async for cmsg in client.iter_messages(discussion, reply_to=post.id, limit=10):
                                        if cmsg.text:
                                            comments.append(cmsg)

                                    if not comments:
                                        continue

                                    targets = random.sample(comments, min(react_count, len(comments)))

                                    for ci, cmsg in enumerate(targets):
                                        # До 3 разных эмодзи на случай если первый отвергнут
                                        candidates = random.sample(allowed_emojis, min(3, len(allowed_emojis)))
                                        reacted_emoji = None
                                        for emoji in candidates:
                                            try:
                                                await client(SendReactionRequest(
                                                    peer=discussion,
                                                    msg_id=cmsg.id,
                                                    reaction=[ReactionEmoji(emoticon=emoji)],
                                                ))
                                                reacted_emoji = emoji
                                                break
                                            except ReactionInvalidError:
                                                continue

                                        if reacted_emoji:
                                            logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 😍 Реакция {reacted_emoji} на коммент в @{target_channel}")
                                            await _safe_log(db, task_id=None, account_id=acc.id, action="react_to_comment",
                                                             detail=f"Реакция {reacted_emoji} на коммент в @{target_channel}",
                                                             channel=target_channel, emoji=reacted_emoji, success=True,
                                                             source=plan_source, campaign_id=plan_campaign_id)
                                            done_actions += 1
                                        if ci < len(targets) - 1:
                                            await asyncio.sleep(random.randint(3, 10))

                                except Exception as e:
                                    logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] react_to_comment ошибка: {str(e)[:100]}")

                            elif action_type == "join_target_channel":
                                # Вступление в конкретный канал кампании
                                ch = action.get("channel", "")
                                if not ch:
                                    continue
                                try:
                                    from telethon.tl.functions.channels import JoinChannelRequest, GetParticipantRequest
                                    from telethon.tl.functions.contacts import ResolveUsernameRequest
                                    from telethon.errors import (
                                        UserAlreadyParticipantError, InviteRequestSentError,
                                        ChannelsTooMuchError, ChannelPrivateError,
                                        UsernameNotOccupiedError, UsernameInvalidError,
                                    )
                                    from models.campaign_channel_assignment import CampaignChannelAssignment

                                    clean_ch = ch.lstrip('@').strip()
                                    me = await client.get_me()

                                    # 1) Резолвим канал свежим запросом (минуем кэш Telethon)
                                    try:
                                        resolved = await client(ResolveUsernameRequest(clean_ch))
                                        if not resolved.chats:
                                            raise Exception(f"@{clean_ch}: канал не существует")
                                        entity = resolved.chats[0]
                                    except (UsernameNotOccupiedError, UsernameInvalidError) as e:
                                        raise Exception(f"@{clean_ch}: невалидный username")

                                    # 2) Уже подписаны?
                                    already_in = False
                                    try:
                                        await client(GetParticipantRequest(channel=entity, participant=me))
                                        already_in = True
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📢 @{clean_ch}: уже подписан (проверено)")
                                    except Exception:
                                        already_in = False

                                    # 3) Не подписаны → JoinChannelRequest + верификация
                                    if not already_in:
                                        join_request_sent = False
                                        try:
                                            await client(JoinChannelRequest(entity))
                                            join_request_sent = True
                                        except UserAlreadyParticipantError:
                                            already_in = True
                                        except InviteRequestSentError:
                                            raise Exception(f"@{clean_ch}: канал требует подтверждения (отправлена заявка)")
                                        except ChannelsTooMuchError:
                                            raise Exception(f"@{clean_ch}: лимит каналов на аккаунте")
                                        except ChannelPrivateError:
                                            raise Exception(f"@{clean_ch}: приватный канал")

                                        # 4) Проверяем что мы реально стали участником (один раз)
                                        if join_request_sent and not already_in:
                                            await asyncio.sleep(random.uniform(2, 4))
                                            try:
                                                await client(GetParticipantRequest(channel=entity, participant=me))
                                                logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📢 Вступил @{clean_ch} (подтверждено)")
                                            except Exception as verr:
                                                # Не в участниках после join → теневой бан / заморозка / флудвейт
                                                raise Exception(f"@{clean_ch}: join не прошёл (теневой бан/заморозка): {type(verr).__name__}")

                                    # 5) Обновляем dialogs cache чтобы next read_feed не словил MsgidDecreaseRetry
                                    try:
                                        await client.get_dialogs(limit=1, archived=False)
                                    except Exception:
                                        pass

                                    # 6) Сохраняем assignment как joined
                                    if plan.campaign_id:
                                        assignment = (await db.execute(
                                            select(CampaignChannelAssignment).where(
                                                CampaignChannelAssignment.campaign_id == plan.campaign_id,
                                                CampaignChannelAssignment.account_id  == acc.id,
                                                CampaignChannelAssignment.channel_username == ch,
                                            )
                                        )).scalar_one_or_none()
                                        if assignment:
                                            assignment.status    = "joined"
                                            assignment.joined_at = datetime.utcnow()
                                        else:
                                            db.add(CampaignChannelAssignment(
                                                campaign_id=plan.campaign_id,
                                                account_id=acc.id,
                                                channel_username=ch,
                                                status="joined",
                                                joined_at=datetime.utcnow(),
                                            ))
                                        await db.flush()

                                    detail = f"Уже был в @{clean_ch}" if already_in else f"Вступил в @{clean_ch} (подтверждено)"
                                    await _safe_log(db, task_id=None, account_id=acc.id, action="join_channel",
                                                     detail=detail, channel=clean_ch, success=True,
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    done_actions += 1
                                except Exception as e:
                                    err = str(e)
                                    logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📢 join_target_channel ошибка ({type(e).__name__}) @{ch}: {err[:120]}")
                                    # Фиксируем ошибку в матрице
                                    if plan.campaign_id:
                                        try:
                                            from models.campaign_channel_assignment import CampaignChannelAssignment
                                            assignment = (await db.execute(
                                                select(CampaignChannelAssignment).where(
                                                    CampaignChannelAssignment.campaign_id == plan.campaign_id,
                                                    CampaignChannelAssignment.account_id  == acc.id,
                                                    CampaignChannelAssignment.channel_username == ch,
                                                )
                                            )).scalar_one_or_none()
                                            if assignment:
                                                assignment.status = "failed"
                                                await db.flush()
                                        except Exception:
                                            pass
                                    # Логируем ошибку в warmup_logs чтобы было видно в UI
                                    await _safe_log(db, task_id=None, account_id=acc.id, action="join_channel",
                                                     detail=f"❌ @{ch}: {err[:120]}",
                                                     channel=ch, success=False, error=err[:200],
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    import re as _re
                                    if "FLOOD_WAIT" in err:
                                        wait = int(_re.search(r"(\d+)", err).group(1)) if _re.search(r"(\d+)", err) else 60
                                        await asyncio.sleep(wait + random.randint(5, 15))
                                        break

                            elif action_type == "join_channel":
                                # Устаревший тип: вступление в случайный популярный канал (прогрев)
                                try:
                                    from telethon.tl.functions.channels import JoinChannelRequest
                                    popular = ["telegram", "durov", "techcrunch", "bbcnews", "reddit",
                                               "cryptonews", "nytimes", "theverge", "mashable", "wired",
                                               "sciencedaily", "nationalgeographic", "historyfacts"]
                                    ch = random.choice(popular)
                                    await client(JoinChannelRequest(ch))
                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📢 Подписался на @{ch}")
                                    await _safe_log(db, task_id=None, account_id=acc.id, action="join_channel",
                                                     detail=f"Подписался на @{ch}",
                                                     channel=ch, success=True,
                                                     source=plan_source, campaign_id=plan_campaign_id)
                                    done_actions += 1
                                except Exception:
                                    pass

                            elif action_type == "typing":
                                try:
                                    me = await client.get_me()
                                    from telethon.tl.functions.messages import SetTypingRequest
                                    from telethon.tl.types import SendMessageTypingAction
                                    await client(SetTypingRequest(peer=me, action=SendMessageTypingAction()))
                                    dur = action.get("duration", random.randint(2, 10))
                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] ⌨️ Typing {dur}с")
                                    await asyncio.sleep(dur)
                                    done_actions += 1
                                except Exception:
                                    pass

                            pause = action.get("pause_after", random.randint(3, 30))
                            await asyncio.sleep(pause)

                            await db.flush()

                        except Exception as e:
                            err = str(e)
                            logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] ❌ {action.get('type')}: {err[:80]}")

                            if "FLOOD_WAIT" in err:
                                import re as _re
                                wait = int(_re.search(r"(\d+)", err).group(1)) if _re.search(r"(\d+)", err) else 60
                                await asyncio.sleep(wait + random.randint(5, 15))
                                break
                            elif "AUTH_KEY_UNREGISTERED" in err or "UserDeactivatedBan" in str(type(e)):
                                acc.status = "frozen"
                                break
                            elif "PEER_FLOOD" in err:
                                break

                except Exception as e:
                    logger.error(f"[plan][{phone}] Session error: {e}")
                    await _safe_log(db, task_id=None, account_id=acc.id, action="error",
                                     detail=f"Ошибка сессии: {str(e)[:200]}", success=False,
                                     source=plan_source, campaign_id=plan_campaign_id)
                finally:
                    try:
                        await client.disconnect()
                    except:
                        pass

                logger.info(f"[plan][{phone}] ═══ Сессия {sess_num}/{total_sess} завершена: {done_actions}/{len(actions)} действий ═══")
                await _safe_log(db,
                    task_id=None, account_id=acc.id,
                    action="session_end",
                    detail=f"Сессия завершена: {done_actions} действий (план день {plan.day_number})",
                    success=True, created_at=datetime.utcnow(),
                    source=plan_source, campaign_id=plan_campaign_id,
                )

                # Обновляем счётчики WarmupTask ТОЛЬКО если это прогрев
                if plan.warmup_task_id and plan_source == 'warmup':
                    wt = (await db.execute(
                        select(WarmupTask).where(WarmupTask.id == plan.warmup_task_id)
                    )).scalar_one_or_none()
                    if wt:
                        wt.actions_done = (wt.actions_done or 0) + done_actions
                        wt.today_actions = (wt.today_actions or 0) + done_actions
                        logger.info(f"[plan][{phone}] ✅ Обновлён WarmupTask #{wt.id}: +{done_actions} действий (всего {wt.actions_done})")
                elif plan.campaign_id and plan_source == 'commenting':
                    logger.info(f"[plan][{phone}] 📝 Commenting сессия — WarmupTask НЕ трогаем (actions: {done_actions})")

                plan.executed_idx += 1
                if plan.executed_idx >= len(sessions):
                    plan.status = "done"

                await db.commit()
                return {"status": "done", "actions": done_actions, "account": phone}

            finally:
                release_account_lock(plan.account_id)
                if slot_user_id is not None:
                    release_user_slot(slot_user_id)

        except Exception as e:
            logger.error(f"[plan_executor] #{plan_id}: {e}")
            try:
                await db.rollback()
            except:
                pass
            return {"error": str(e)}


# ═══════════════════════════════════════════════════════════
# CELERY TASKS
# ═══════════════════════════════════════════════════════════

@celery_app.task(bind=True, name="tasks.plan_executor.dispatch_plans",
                 acks_late=False, reject_on_worker_lost=False)
def dispatch_plans(self):
    """Диспетчер планов (<1с)."""
    return run_async(_dispatch_plans())


@celery_app.task(bind=True, name="tasks.plan_executor.execute_plan_session",
                 acks_late=False, reject_on_worker_lost=False)
def execute_plan_session(self, plan_id: int):
    """Одна сессия одного аккаунта — параллельно."""
    return run_async(_execute_plan_session(plan_id))