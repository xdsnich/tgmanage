"""
GramGPT — tasks/commenting_tasks.py
Нейрокомментинг (публичные каналы):
  Проверка постов → веб-парсинг https://t.me/s/ (без аккаунта, 0 риск)
  Отправка коммента → Telethon (короткое подключение через прокси)

Закрытые каналы обрабатываются отдельно через run_listener.py
(persistent event listener — @client.on(events.NewMessage))
"""

import asyncio, sys, os, random, logging, re
from datetime import datetime, date
from collections import defaultdict
from celery_app import celery_app

logger = logging.getLogger(__name__)
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Per-account daily comment limit
MAX_COMMENTS_PER_ACCOUNT_PER_DAY = 5
_account_daily_comments: dict[int, dict] = {}  # account_id -> {"date": date, "count": int}


def _check_account_daily_limit(account_id: int) -> bool:
    """Returns True if account is under daily limit."""
    today = date.today()
    entry = _account_daily_comments.get(account_id)
    if not entry or entry["date"] != today:
        _account_daily_comments[account_id] = {"date": today, "count": 0}
        return True
    return entry["count"] < MAX_COMMENTS_PER_ACCOUNT_PER_DAY


def _increment_account_daily(account_id: int):
    today = date.today()
    entry = _account_daily_comments.get(account_id)
    if not entry or entry["date"] != today:
        _account_daily_comments[account_id] = {"date": today, "count": 1}
    else:
        entry["count"] += 1


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try: return loop.run_until_complete(coro)
    finally: loop.close()


def call_llm(provider, system_prompt, post_text):
    if API_DIR not in sys.path: sys.path.insert(0, API_DIR)
    try:
        from services.llm import generate_comment
        return generate_comment(provider, system_prompt, post_text)
    except Exception as e:
        logger.error(f"LLM ({provider}): {e}")
        return ""


def build_prompt(tone, comment_length, custom_prompt=""):
    tones = {"positive": "Тебе нравится пост. Напиши одобрительный комментарий.",
             "negative": "Конструктивная критика.", "question": "Задай вопрос автору.",
             "analytical": "Аналитический комментарий.", "short": "Очень короткий (2-5 слов).",
             "custom": custom_prompt or "Релевантный комментарий."}
    lengths = {"short": "2-5 слов.", "medium": "1-2 предложения, 30-100 символов.", "long": "2-4 предложения."}
    return f"""Ты — живой пользователь Telegram. {tones.get(tone, tones['positive'])}
{lengths.get(comment_length, lengths['medium'])}
Пиши как человек, на языке поста. Без шаблонов.
{f'Доп: {custom_prompt}' if custom_prompt else ''}"""


def _should_comment(mode, pct, keywords, text):
    if mode == "all": return True
    if mode == "random": return random.randint(1, 100) <= pct
    if mode == "keywords": return any(k.lower() in text.lower() for k in keywords)
    return False


def _val(x): return x.value if hasattr(x, 'value') else x


async def _send_comment_via_telethon(account, proxy, username, post_id, comment, name):
    """Короткое подключение Telethon ТОЛЬКО для отправки (через прокси)."""
    if API_DIR not in sys.path: sys.path.insert(0, API_DIR)
    from utils.telegram import make_telethon_client
    client = make_telethon_client(account, proxy)
    if not client: return False

    proxy_info = f"через {proxy.host}:{proxy.port}" if proxy else "НАПРЯМУЮ (⚠️ нет прокси!)"
    logger.info(f"[{name}] Подключение {proxy_info}")

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect(); return False
        entity = await client.get_entity(username)
        await client.send_message(entity=entity, message=comment, comment_to=post_id)
        await client.disconnect()
        return True
    except Exception as e:
        err = str(e)
        if "FLOOD_WAIT" in err:
            wait = int(re.search(r"(\d+)", err).group(1)) if re.search(r"(\d+)", err) else 60
            logger.warning(f"[{name}] FLOOD_WAIT_{wait} — sleeping and ending early")
            await asyncio.sleep(wait + random.randint(5, 15))
            try: await client.disconnect()
            except: pass
            return "flood_wait"
        elif "PEER_FLOOD" in err:
            logger.warning(f"[{name}] PEER_FLOOD — pausing account for 24h")
            try: await client.disconnect()
            except: pass
            return "peer_flood"
        elif "AUTH_KEY_UNREGISTERED" in err or "UserDeactivatedBan" in type(e).__name__:
            logger.warning(f"[{name}] Account frozen: {err[:80]}")
            try: await client.disconnect()
            except: pass
            return "frozen"
        elif "GetDiscussionMessage" in err or "MESSAGE_ID_INVALID" in err:
            logger.warning(f"[{name}] @{username} #{post_id}: комменты отключены на этом посте")
        elif "private" in err.lower() or "banned" in err.lower() or "CHANNEL_PRIVATE" in err:
            logger.warning(f"[{name}] ⛔ @{username}: нет доступа — деактивирую канал")
            return "deactivate"
        else:
            logger.error(f"[{name}] ❌ @{username}: {e}")
        try: await client.disconnect()
        except: pass
        return False


async def _process_campaign(c, db):
    """Веб-парсинг публичных каналов. Закрытые → run_listener.py."""
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.campaign import TargetChannel, CampaignStatus, CommentLog
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from services.channel_monitor import fetch_latest_posts_web

    logger.info(f"[{c.name}] {c.comments_sent}/{c.max_comments}")

    if c.comments_sent >= c.max_comments:
        c.status = CampaignStatus.finished; c.finished_at = datetime.utcnow(); return
    if c.started_at and (datetime.utcnow() - c.started_at).total_seconds() / 3600 >= c.max_hours:
        c.status = CampaignStatus.finished; c.finished_at = datetime.utcnow(); return

    ch_r = await db.execute(select(TargetChannel).where(TargetChannel.campaign_id == c.id, TargetChannel.is_active == True))
    channels = ch_r.scalars().all()
    if not channels or not c.account_ids: return

    # Pick an account that hasn't exceeded daily limit
    eligible_ids = [aid for aid in c.account_ids if _check_account_daily_limit(aid)]
    if not eligible_ids:
        logger.info(f"[{c.name}] Все аккаунты достигли дневного лимита ({MAX_COMMENTS_PER_ACCOUNT_PER_DAY}/день)")
        return

    acc_id = random.choice(eligible_ids)
    acc_r = await db.execute(select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(TelegramAccount.id == acc_id))
    account = acc_r.scalar_one_or_none()
    if not account or not account.session_file: return

    proxy = None
    if account.proxy_id:
        proxy_r = await db.execute(select(Proxy).where(Proxy.id == account.proxy_id))
        proxy = proxy_r.scalar_one_or_none()

    if not proxy:
        logger.warning(f"[{c.name}] ⚠️ Аккаунт {account.phone} без прокси — пропускаю (назначь прокси!)")
        return

    prompt = build_prompt(_val(c.tone), c.comment_length, c.custom_prompt)

    for channel in channels:
        if c.comments_sent >= c.max_comments: break
        if not channel.username: continue

        logger.info(f"[{c.name}] [WEB] @{channel.username} (last={channel.last_post_id})")

        # ═══ Веб-парсинг (без аккаунта!) ═══
        new_posts = await fetch_latest_posts_web(channel.username, channel.last_post_id)

        if not new_posts:
            logger.info(f"[{c.name}] @{channel.username}: нет новых (закрытый → listener)")
            continue

        # ═══ ПЕРВЫЙ ЗАПУСК: запоминаем последний пост БЕЗ комментирования ═══
        if channel.last_post_id == 0:
            latest = max(p.post_id for p in new_posts)
            channel.last_post_id = latest
            await db.commit()
            logger.info(f"[{c.name}] @{channel.username}: первый запуск → запомнил last={latest}, жду новые")
            continue

        # Берём ТОЛЬКО самый свежий пост (не все 20!)
        post = new_posts[-1]  # Последний = самый новый

        logger.info(f"[{c.name}] @{channel.username}: новый пост #{post.post_id}")

        channel.last_post_id = post.post_id
        await db.commit()

        if not _should_comment(_val(c.trigger_mode), c.trigger_percent, c.trigger_keywords or [], post.text):
            logger.info(f"[{c.name}] Пост #{post.post_id}: не проходит триггер")
            continue

        delay = max(c.delay_comment + random.randint(-30, 30), 30)
        if delay > 5:
            logger.info(f"[{c.name}] Задержка {delay}с...")
            await asyncio.sleep(delay)

        comment = call_llm(_val(c.llm_provider), prompt, post.text)
        if not comment: continue

        ok = await _send_comment_via_telethon(account, proxy, channel.username, post.post_id, comment, c.name)
        if ok == "deactivate":
            channel.is_active = False
            await db.commit()
            logger.warning(f"[{c.name}] ⛔ @{channel.username} деактивирован (нет доступа)")
            continue
        if ok == "frozen":
            account.status = "frozen"
            await db.commit()
            return
        if ok == "peer_flood" or ok == "flood_wait":
            return
        if ok is True:
            c.comments_sent += 1; channel.comments_sent += 1
            _increment_account_daily(account.id)
            db.add(CommentLog(
                campaign_id=c.id, account_id=account.id, account_phone=account.phone,
                channel_username=channel.username, channel_title=channel.title or "",
                post_id=post.post_id, post_text=post.text[:500],
                comment_text=comment, llm_provider=_val(c.llm_provider),
            ))
            logger.info(f"[{c.name}] ✅ #{c.comments_sent} @{channel.username}")
            await asyncio.sleep(max(c.delay_between + random.randint(-10, 10), 15))


async def _process_all_campaigns():
    if API_DIR not in sys.path: sys.path.insert(0, API_DIR)
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select
    from config import DATABASE_URL
    from models.campaign import Campaign, CampaignStatus

    engine = create_async_engine(DATABASE_URL, pool_size=2, max_overflow=0)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

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
        finally: await engine.dispose()


@celery_app.task(bind=True, name="tasks.commenting_tasks.process_campaigns")
def process_campaigns(self):
    """Веб-парсинг публичных каналов + отправка через Telethon."""
    self.update_state(state="PROGRESS", meta={"message": "Веб-парсинг..."})
    return run_async(_process_all_campaigns())