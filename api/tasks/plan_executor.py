"""
GramGPT — tasks/plan_executor.py
Выполняет планы кампаний — параллельно, по одной сессии на задачу.

Архитектура:
  dispatch_plans (<1с) → execute_plan_session(plan_id) × N параллельно

Все импорты моделей — внутри функций (lazy import).
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
    """
    Лёгкий (<1с): находит campaign_plans с сессиями которые пора выполнить.
    Для каждой → отдельная Celery задача.
    """
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select
    from config import DATABASE_URL
    from models.campaign_plan import CampaignPlan
    from models.campaign import Campaign, CampaignStatus

    engine = create_async_engine(DATABASE_URL, pool_size=2, max_overflow=0)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    dispatched = 0
    skipped = 0

    try:
        async with Session() as db:
            now = datetime.utcnow()
            today = now.date()
            current_hour = (now.hour + 3) % 24  # UTC+3
            current_minute = now.minute

            # Только сегодняшние активные планы
            from models.campaign import Campaign, CampaignStatus
            result = await db.execute(
                select(CampaignPlan)
                .join(Campaign, Campaign.id == CampaignPlan.campaign_id)
                .where(
                    CampaignPlan.plan_date == today,
                    CampaignPlan.status == "active",
                    Campaign.status == CampaignStatus.active,
                )
            )
            plans = result.scalars().all()
            warmup_result = await db.execute(
                select(CampaignPlan).where(
                    CampaignPlan.plan_date == today,
                    CampaignPlan.status == "active",
                    CampaignPlan.campaign_id == None,
                    CampaignPlan.warmup_task_id != None,
                )
            )
            warmup_plans = warmup_result.scalars().all()
            plans = list(plans) + list(warmup_plans)

            for plan in plans:
                sessions = plan.plan.get("sessions", [])
                if plan.executed_idx >= len(sessions):
                    plan.status = "done"
                    skipped += 1
                    continue

                next_session = sessions[plan.executed_idx]

                # Пропущенная сессия?
                if next_session.get("skipped"):
                    plan.executed_idx += 1
                    logger.info(f"[plan] Пропуск сессии #{plan.executed_idx} (акк {plan.account_id}): {next_session.get('skip_reason', '?')}")
                    if plan.executed_idx >= len(sessions):
                        plan.status = "done"
                    skipped += 1
                    continue

                # Пора?
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
                    queue="ai_dialogs",
                )
                dispatched += 1
            from models.campaign import Campaign, CampaignStatus
            from sqlalchemy import func
            active_campaigns = (await db.execute(
                select(Campaign).where(Campaign.status == CampaignStatus.active)
            )).scalars().all()

            logger.info(f"[autoclose] Проверка {len(active_campaigns)} активных кампаний")

            for camp in active_campaigns:
                logger.info(f"[autoclose]   camp {camp.id}: started_at={camp.started_at}, max_hours={camp.max_hours}, comments={camp.comments_sent}/{camp.max_comments}")
                if camp.started_at and camp.max_hours:
                    from datetime import datetime as _dt
                    elapsed = (_dt.utcnow() - camp.started_at).total_seconds() / 3600
                    if elapsed >= camp.max_hours:
                        camp.status = CampaignStatus.finished
                        logger.info(f"[autoclose] Кампания {camp.id} → finished (время истекло: {elapsed:.1f}ч / {camp.max_hours}ч)")
                        continue

                # Проверяем достигнут ли лимит комментов
                if camp.comments_sent >= camp.max_comments:
                    camp.status = CampaignStatus.finished
                    logger.info(f"[autoclose] Кампания {camp.id} → finished (лимит комментов)")
                    continue

                # Проверяем все ли планы выполнены
                remaining = (await db.execute(
                    select(func.count(CampaignPlan.id)).where(
                        CampaignPlan.campaign_id == camp.id,
                        CampaignPlan.status == "active",
                    )
                )).scalar() or 0

                if remaining == 0:
                    # Все планы done — проверяем не остались ли дни
                    future = (await db.execute(
                        select(func.count(CampaignPlan.id)).where(
                            CampaignPlan.campaign_id == camp.id,
                            CampaignPlan.plan_date > today,
                        )
                    )).scalar() or 0

                    if future == 0:
                        camp.status = CampaignStatus.finished
                        logger.info(f"[autoclose] Кампания {camp.id} → finished (все планы выполнены)")
            await db.commit()
            logger.info(f"[plan_dispatch] Отправлено {dispatched}, пропущено {skipped}")

    except Exception as e:
        logger.error(f"[plan_dispatch] Ошибка: {e}")
    finally:
        await engine.dispose()

    return {"dispatched": dispatched, "skipped": skipped}


# ═══════════════════════════════════════════════════════════
# EXECUTOR: выполняет одну сессию одного аккаунта
# ═══════════════════════════════════════════════════════════

async def _execute_plan_session(plan_id: int):
    """
    Выполняет одну сессию из плана:
    1. Загружает plan из БД
    2. Берёт текущую сессию (executed_idx)
    3. Подключает аккаунт через прокси
    4. Выполняет каждое действие по списку
    5. Обновляет executed_idx
    """
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from config import DATABASE_URL
    from models.campaign_plan import CampaignPlan
    from models.campaign import Campaign, CampaignStatus, CommentLog
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from models.warmup_log import WarmupLog
    from utils.telegram import make_telethon_client
    from utils.account_lock import acquire_account_lock, release_account_lock
    from services.llm import generate_comment, build_comment_prompt
    from tasks.behavior_engine import assign_style_profile

    engine = create_async_engine(DATABASE_URL, pool_size=2, max_overflow=0)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        try:
            # ── Загружаем план ────────────────────────────
            plan = (await db.execute(
                select(CampaignPlan).where(CampaignPlan.id == plan_id)
            )).scalar_one_or_none()

            if not plan or plan.status != "active":
                return {"status": "skip", "reason": "not active"}

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

            # ── Acquire lock ──
            if not acquire_account_lock(plan.account_id, ttl=1800):
                logger.info(f"[plan] Аккаунт {plan.account_id} занят (lock) — пропуск")
                return {"status": "locked"}

            try:
                # ── Аккаунт + прокси ─────────────────────
                acc = (await db.execute(
                    select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
                    .where(TelegramAccount.id == plan.account_id)
                )).scalar_one_or_none()

                if not acc or acc.status not in ("active", "unknown"):
                    plan.executed_idx += 1
                    await db.commit()
                    return {"status": "inactive"}

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

                # ── Кампания ──────────────────────────────
                campaign = None
                if plan.campaign_id:
                    campaign = (await db.execute(
                        select(Campaign).where(Campaign.id == plan.campaign_id)
                    )).scalar_one_or_none()

                # ── Подключаемся ──────────────────────────
                phone = acc.phone
                done_actions = 0
                sess_num = plan.executed_idx + 1
                total_sess = len(sessions)
                mood = plan.plan.get('mood', '?')

                logger.info(f"[plan][{phone}] ═══ Сессия {sess_num}/{total_sess} (день {plan.day_number}, {mood}) ═══")

                # Логируем начало
                await _safe_log(db,
                    task_id=None, account_id=acc.id,
                    action="session_start",
                    detail=f"План день {plan.day_number}, сессия {sess_num}/{total_sess} ({mood})",
                    success=True, created_at=datetime.utcnow(),
                )

                try:
                    await client.connect()
                    increment_connection(plan.account_id)
                    if not await client.is_user_authorized():
                        plan.executed_idx += 1
                        logger.warning(f"[plan][{phone}] Не авторизован")
                        await _safe_log(db, task_id=None, account_id=acc.id, action="error",
                                         detail="Не авторизован", success=False)
                        await db.commit()
                        return {"status": "not_authorized"}

                    logger.info(f"[plan][{phone}] Подключен, {len(actions)} действий")

                    # ── Выполняем действия ────────────────
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
                                                     channel=str(name), success=True)
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
                                                     detail=f"Просмотрел stories ({count})", success=True)
                                    done_actions += 1
                                except Exception:
                                    pass

                            elif action_type == "set_reaction":
                                try:
                                    from telethon.tl.functions.messages import SendReactionRequest
                                    from telethon.tl.types import ReactionEmoji
                                    emoji = action.get("emoji", "👍")
                                    channel_name = action.get("channel")

                                    if channel_name:
                                        entity = await client.get_entity(channel_name)
                                    else:
                                        dialogs = await client.get_dialogs(limit=10)
                                        channels = [d for d in dialogs if getattr(d, 'is_channel', False)]
                                        if not channels:
                                            continue
                                        entity = random.choice(channels)

                                    msgs = await client.get_messages(entity, limit=5)
                                    if msgs:
                                        target_msg = random.choice(msgs)
                                        await client(SendReactionRequest(
                                            peer=entity, msg_id=target_msg.id,
                                            reaction=[ReactionEmoji(emoticon=emoji)]
                                        ))
                                        name = channel_name or getattr(entity, 'name', '?') or getattr(entity, 'title', '?')
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 😍 Реакция {emoji} в «{name}»")
                                        await _safe_log(db, task_id=None, account_id=acc.id, action="set_reaction",
                                                         detail=f"Поставил {emoji} в «{name}»",
                                                         channel=str(name), success=True)
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
                                                         detail=f"Просмотрел профиль «{u.name or '?'}»", success=True)
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
                                                     detail=f"Поиск «{term}»", success=True)
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
                                                     detail=f"Saved: {text}", success=True)
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
                                                             detail=f"Переслал из «{ch.name or '?'}»", success=True)
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
                                                         detail=f"Прочитал ЛС от «{u.name or '?'}»", success=True)
                                        done_actions += 1
                                except Exception:
                                    pass

                            elif action_type == "smart_comment":
                                target_channel = action.get("channel", "")
                                if not target_channel or not campaign:
                                    continue

                                try:
                                    entity = await client.get_entity(target_channel)
                                    # Читаем последние посты
                                    posts = await client.get_messages(entity, limit=random.randint(3, 8))
                                    for p in posts:
                                        await client.send_read_acknowledge(entity, p)
                                        await asyncio.sleep(random.uniform(0.5, 3))

                                    if not posts:
                                        continue

                                    # Берём самый свежий пост
                                    target_post = posts[0]
                                    post_text = target_post.message or ""

                                    # Typing
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

                                    # Abort? (5-15%)
                                    if random.random() < random.uniform(0.05, 0.15):
                                        logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 🚫 Передумал комментировать @{target_channel}")
                                        await _safe_log(db, task_id=None, account_id=acc.id, action="smart_comment",
                                                         detail=f"Передумал комментировать @{target_channel}",
                                                         channel=target_channel, success=True)
                                        done_actions += 1
                                        continue

                                    # Генерируем комментарий
                                    style_profile = assign_style_profile(phone)
                                    prompt = build_comment_prompt(post_text, style_profile, plan.plan)
                                    provider = plan.plan.get("personality_data", {}).get("llm_provider", "groq")
                                    if campaign:
                                        provider = _val(campaign.llm_provider)

                                    comment_text = generate_comment(provider, prompt, post_text)
                                    if not comment_text:
                                        logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] ❌ LLM вернул пустой ответ")
                                        continue

                                    # Отправляем
                                    await client.send_message(entity=entity, message=comment_text, comment_to=target_post.id)

                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 💬 @{target_channel}: {comment_text[:60]}")
                                    await _safe_log(db, task_id=None, account_id=acc.id, action="smart_comment",
                                                     detail=f"💬 @{target_channel}: {comment_text[:60]}",
                                                     channel=target_channel, success=True)

                                    # CommentLog
                                    if campaign:
                                        campaign.comments_sent += 1
                                        db.add(CommentLog(
                                            campaign_id=campaign.id, account_id=acc.id,
                                            account_phone=phone, channel_username=target_channel,
                                            channel_title="", post_id=target_post.id,
                                            post_text=post_text[:500],
                                            comment_text=comment_text,
                                            llm_provider=_val(campaign.llm_provider),
                                        ))

                                    done_actions += 1

                                except Exception as e:
                                    err = str(e)
                                    logger.warning(f"[plan][{phone}]   [{action_num}/{len(actions)}] ❌ comment error: {err[:100]}")
                                    await _safe_log(db, task_id=None, account_id=acc.id, action="smart_comment",
                                                     detail=f"Ошибка: {err[:100]}", channel=target_channel,
                                                     success=False, error=err[:200])

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

                            elif action_type == "join_channel":
                                try:
                                    from telethon.tl.functions.channels import JoinChannelRequest
                                    popular = ["telegram", "durov", "techcrunch", "bbcnews", "reddit",
                                               "cryptonews", "nytimes", "theverge", "mashable", "wired",
                                               "sciencedaily", "nationalgeographic", "historyfacts"]
                                    ch = random.choice(popular)
                                    await client(JoinChannelRequest(ch))
                                    logger.info(f"[plan][{phone}]   [{action_num}/{len(actions)}] 📢 Подписался на @{ch}")
                                    await _safe_log(db, task_id=None, account_id=acc.id, action="join_channel",
                                                     detail=f"Подписался на @{ch}", channel=ch, success=True)
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

                            # Пауза после действия
                            pause = action.get("pause_after", random.randint(3, 30))
                            await asyncio.sleep(pause)

                            # Коммитим логи после каждого действия
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
                                     detail=f"Ошибка сессии: {str(e)[:200]}", success=False)
                finally:
                    try:
                        await client.disconnect()
                    except:
                        pass

                # Логируем конец
                logger.info(f"[plan][{phone}] ═══ Сессия {sess_num}/{total_sess} завершена: {done_actions}/{len(actions)} действий ═══")
                await _safe_log(db,
                    task_id=None, account_id=acc.id,
                    action="session_end",
                    detail=f"Сессия завершена: {done_actions} действий (план день {plan.day_number})",
                    success=True, created_at=datetime.utcnow(),
                )

                plan.executed_idx += 1
                if plan.executed_idx >= len(sessions):
                    plan.status = "done"

                await db.commit()
                return {"status": "done", "actions": done_actions, "account": phone}

            finally:
                release_account_lock(plan.account_id)

        except Exception as e:
            logger.error(f"[plan_executor] #{plan_id}: {e}")
            try:
                await db.rollback()
            except:
                pass
            return {"error": str(e)}
        finally:
            await engine.dispose()


# ═══════════════════════════════════════════════════════════
# CELERY TASKS
# ═══════════════════════════════════════════════════════════

@celery_app.task(bind=True, name="tasks.plan_executor.dispatch_plans")
def dispatch_plans(self):
    """Диспетчер планов (<1с)."""
    return run_async(_dispatch_plans())


@celery_app.task(bind=True, name="tasks.plan_executor.execute_plan_session")
def execute_plan_session(self, plan_id: int):
    """Одна сессия одного аккаунта — параллельно."""
    return run_async(_execute_plan_session(plan_id))