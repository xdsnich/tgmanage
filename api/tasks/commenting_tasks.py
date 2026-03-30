"""
GramGPT API — tasks/commenting_tasks.py
Нейрокомментинг: мониторинг постов в целевых каналах + AI-генерация комментариев.
Очередь: ai_dialogs (throttled)

Celery Beat запускает process_campaigns каждые 45 сек.
"""

import asyncio
import sys
import os
import random
import logging
import importlib.util
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Загружаем .env из api/ директории
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from celery_app import celery_app

logger = logging.getLogger(__name__)

API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _get_cli_config():
    config_path = os.path.join(ROOT_DIR, "config.py")
    spec = importlib.util.spec_from_file_location("cli_config", config_path)
    cli_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli_config)
    return cli_config


# ── LLM Providers ────────────────────────────────────────────

def call_llm(provider: str, system_prompt: str, post_text: str) -> str:
    """Вызывает Claude или OpenAI для генерации комментария"""
    import httpx

    if provider == "claude":
        return _call_claude(system_prompt, post_text)
    elif provider == "openai":
        return _call_openai(system_prompt, post_text)
    elif provider == "gemini":
        return _call_gemini(system_prompt, post_text)
    else:
        return _call_claude(system_prompt, post_text)


def _call_claude(system_prompt: str, post_text: str) -> str:
    import httpx
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY не задан!")
        return ""

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 300,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": post_text}],
                },
            )
            resp.raise_for_status()
            for block in resp.json().get("content", []):
                if block.get("type") == "text":
                    return block["text"]
    except Exception as e:
        logger.error(f"Claude error: {e}")
    return ""


def _call_openai(system_prompt: str, post_text: str) -> str:
    import httpx
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        logger.error("OPENAI_API_KEY не задан!")
        return ""

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o",
                    "max_tokens": 300,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": post_text},
                    ],
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
    return ""


def _call_gemini(system_prompt: str, post_text: str) -> str:
    import httpx
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        logger.error("GEMINI_API_KEY не задан!")
        return ""

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}",
                headers={"Content-Type": "application/json"},
                json={
                    "system_instruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"parts": [{"text": post_text}]}],
                    "generationConfig": {"maxOutputTokens": 300},
                },
            )
            resp.raise_for_status()
            candidates = resp.json().get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return parts[0].get("text", "")
    except Exception as e:
        logger.error(f"Gemini error: {e}")
    return ""


# ── Промпт-билдер ────────────────────────────────────────────

def build_prompt(tone: str, comment_length: str, custom_prompt: str) -> str:
    """Строит системный промпт по настройкам кампании"""

    if custom_prompt and tone == "custom":
        return custom_prompt

    tone_map = {
        "positive": "Напиши позитивный, одобрительный комментарий к этому посту. Будь искренним.",
        "negative": "Напиши критичный, скептичный комментарий к этому посту. Будь вежлив, но критикуй.",
        "question": "Задай автору интересный вопрос по теме поста. Покажи заинтересованность.",
        "analytical": "Напиши аналитический комментарий с собственным мнением и аргументами.",
        "short": "Напиши очень короткий комментарий (2-4 слова). Эмоджи допустимы.",
    }

    length_map = {
        "short": "Комментарий должен быть 1-2 предложения, максимум 50 символов.",
        "medium": "Комментарий должен быть 1-3 предложения, 50-150 символов.",
        "long": "Напиши развёрнутый комментарий 2-4 предложения, 100-300 символов.",
    }

    base = tone_map.get(tone, tone_map["positive"])
    length = length_map.get(comment_length, length_map["medium"])

    return f"""Ты — живой пользователь Telegram. {base}

{length}

ВАЖНЫЕ ПРАВИЛА:
- Пиши как реальный человек, не как бот.
- Не начинай с "Отличный пост!" или подобных шаблонов.
- Не используй маркетинговые клише.
- Пиши на языке поста (если пост на русском — отвечай на русском).
- Не упоминай что ты ИИ.
- Комментарий должен быть релевантен КОНКРЕТНО этому посту.
{f"Дополнительные инструкции: {custom_prompt}" if custom_prompt else ""}"""


# ── Основная логика ──────────────────────────────────────────

def _should_comment(trigger_mode: str, trigger_percent: int, trigger_keywords: list, post_text: str) -> bool:
    """Решает — комментировать этот пост или нет"""
    if trigger_mode == "all":
        return True
    elif trigger_mode == "random":
        return random.randint(1, 100) <= trigger_percent
    elif trigger_mode == "keywords":
        text_lower = post_text.lower()
        return any(kw.lower() in text_lower for kw in trigger_keywords)
    return False


async def _process_campaign(campaign_row, db):
    """Обрабатывает одну кампанию: проверяет посты, генерит комменты"""
    from sqlalchemy import select
    from models.campaign import TargetChannel, CampaignStatus
    from models.account import TelegramAccount
    from telethon import TelegramClient

    cli_config = _get_cli_config()
    c = campaign_row
    _val = lambda x: x.value if hasattr(x, 'value') else x

    logger.info(f"[{c.name}] Обрабатываю кампанию (комментов: {c.comments_sent}/{c.max_comments})")

    # Проверяем лимиты
    if c.comments_sent >= c.max_comments:
        c.status = CampaignStatus.finished
        c.finished_at = datetime.utcnow()
        logger.info(f"[{c.name}] ⏹ Лимит комментариев достигнут ({c.max_comments})")
        return

    if c.started_at:
        hours_running = (datetime.utcnow() - c.started_at).total_seconds() / 3600
        if hours_running >= c.max_hours:
            c.status = CampaignStatus.finished
            c.finished_at = datetime.utcnow()
            logger.info(f"[{c.name}] ⏹ Лимит времени достигнут ({c.max_hours}ч)")
            return

    # Получаем активные каналы
    ch_result = await db.execute(
        select(TargetChannel).where(TargetChannel.campaign_id == c.id, TargetChannel.is_active == True)
    )
    channels = ch_result.scalars().all()
    if not channels:
        logger.warning(f"[{c.name}] Нет активных каналов")
        return

    logger.info(f"[{c.name}] Каналов: {len(channels)}")

    # Выбираем аккаунт
    account_ids = c.account_ids or []
    if not account_ids:
        logger.warning(f"[{c.name}] Нет аккаунтов в кампании")
        return

    acc_id = random.choice(account_ids)
    acc_result = await db.execute(
        select(TelegramAccount).where(TelegramAccount.id == acc_id)
    )
    account = acc_result.scalar_one_or_none()
    if not account or not account.session_file:
        logger.warning(f"[{c.name}] Аккаунт {acc_id} не найден или без сессии")
        return
    if account.status != "active":
        logger.warning(f"[{c.name}] Аккаунт {account.phone} статус={account.status}, пропускаю")
        return

    logger.info(f"[{c.name}] Использую аккаунт: {account.phone}")

    # Подключаемся
    session_path = account.session_file.replace(".session", "")
    client = TelegramClient(
        session_path, cli_config.API_ID, cli_config.API_HASH,
        device_model="Desktop", system_version="Windows 10", app_version="4.14.15",
    )

    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.warning(f"[{c.name}] Аккаунт {account.phone} — сессия не активна")
            return

        # Строим промпт
        system_prompt = build_prompt(_val(c.tone), c.comment_length, c.custom_prompt)

        for channel in channels:
            if c.comments_sent >= c.max_comments:
                break

            logger.info(f"[{c.name}] Проверяю канал @{channel.username} (last_post_id={channel.last_post_id})")

            try:
                # Получаем канал
                try:
                    entity = await client.get_entity(channel.username or channel.link)
                except Exception as e:
                    logger.error(f"[{c.name}] Не могу найти канал @{channel.username}: {e}")
                    continue

                channel.title = getattr(entity, 'title', channel.username)
                channel.channel_id = entity.id
                if hasattr(entity, 'participants_count'):
                    channel.subscribers = entity.participants_count or 0

                logger.info(f"[{c.name}] Канал найден: {channel.title} (id={entity.id})")

                # Получаем последние посты
                messages = await client.get_messages(entity, limit=5)
                logger.info(f"[{c.name}] Получено {len(messages)} постов")

                if not messages:
                    logger.info(f"[{c.name}] Нет постов в @{channel.username}")
                    continue

                found_new = False
                for msg in messages:
                    # Логируем каждый пост
                    msg_text = msg.text or ""
                    msg_caption = getattr(msg, 'message', '') or ""
                    post_text = msg_text or msg_caption

                    logger.info(f"[{c.name}] Пост #{msg.id}: текст={len(post_text)} символов, out={msg.out}, media={bool(msg.media)}")

                    # Пропускаем без текста вообще
                    if not post_text or len(post_text.strip()) < 3:
                        logger.info(f"[{c.name}] Пост #{msg.id}: пропуск (нет текста)")
                        continue

                    # Пропускаем уже обработанные
                    if msg.id <= channel.last_post_id:
                        continue

                    found_new = True
                    logger.info(f"[{c.name}] ★ Новый пост #{msg.id} в @{channel.username}: {post_text[:60]}...")

                    # Решаем комментировать или нет (триггер)
                    if not _should_comment(_val(c.trigger_mode), c.trigger_percent, c.trigger_keywords or [], post_text):
                        logger.info(f"[{c.name}] Пост #{msg.id}: не проходит триггер ({_val(c.trigger_mode)})")
                        channel.last_post_id = msg.id
                        continue

                    # Проверяем комментарии
                    has_discussion = msg.replies and getattr(msg.replies, 'comments', False)
                    if not has_discussion:
                        logger.info(f"[{c.name}] Пост #{msg.id}: комментарии отключены, пропускаю")
                        channel.last_post_id = msg.id
                        continue

                    # Задержка перед комментарием (имитация чтения), макс 60с за цикл
                    delay = min(c.delay_comment + random.randint(-30, 30), 60)
                    if delay > 5:
                        logger.info(f"[{c.name}] Задержка {delay}с перед комментарием...")
                        await asyncio.sleep(delay)

                    # Генерируем комментарий через LLM
                    provider = _val(c.llm_provider)
                    logger.info(f"[{c.name}] Вызываю LLM ({provider})...")
                    comment = call_llm(provider, system_prompt, post_text)

                    if not comment:
                        logger.warning(f"[{c.name}] LLM ({provider}) вернул пустой ответ для поста #{msg.id}")
                        channel.last_post_id = msg.id
                        continue

                    logger.info(f"[{c.name}] LLM ответ: {comment[:80]}...")

                    # Отправляем комментарий
                    try:
                        await client.send_message(entity=entity, message=comment, comment_to=msg.id)

                        c.comments_sent += 1
                        channel.comments_sent += 1
                        channel.last_post_id = msg.id
                        logger.info(f"[{c.name}] ✅ Коммент #{c.comments_sent} в @{channel.username}: {comment[:60]}...")

                        # Задержка между комментариями
                        between = min(c.delay_between + random.randint(-10, 10), 30)
                        if between > 0:
                            await asyncio.sleep(between)

                    except Exception as e:
                        logger.error(f"[{c.name}] ❌ Ошибка отправки коммента в @{channel.username}: {e}")
                        channel.last_post_id = msg.id

                    # Один коммент на канал за цикл
                    break

                if not found_new:
                    logger.info(f"[{c.name}] Нет новых постов в @{channel.username} (все <= {channel.last_post_id})")

            except Exception as e:
                logger.error(f"[{c.name}] Ошибка в канале @{channel.username}: {e}")
                continue

    except Exception as e:
        logger.error(f"[{c.name}] Ошибка Telethon: {e}")
    finally:
        try:
            await client.disconnect()
        except:
            pass


async def _process_all_campaigns():
    """Находит все активные кампании и обрабатывает"""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select
    from config import DATABASE_URL
    from models.campaign import Campaign, CampaignStatus

    engine = create_async_engine(DATABASE_URL, pool_size=2, max_overflow=0)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        try:
            result = await db.execute(
                select(Campaign).where(Campaign.status == CampaignStatus.active)
            )
            campaigns = result.scalars().all()

            if not campaigns:
                return {"processed": 0}

            logger.info(f"Активных кампаний: {len(campaigns)}")

            processed = 0
            for c in campaigns:
                await _process_campaign(c, db)
                processed += 1

            await db.commit()
            return {"processed": processed, "total": len(campaigns)}

        except Exception as e:
            logger.error(f"Ошибка обработки кампаний: {e}")
            await db.rollback()
            return {"error": str(e)}
        finally:
            await engine.dispose()


# ── Celery Tasks ─────────────────────────────────────────────

@celery_app.task(bind=True, name="tasks.commenting_tasks.process_campaigns")
def process_campaigns(self):
    """
    Основной таск — проверяет новые посты и комментирует.
    Запускается Celery Beat каждые 45 секунд.
    """
    self.update_state(state="PROGRESS", meta={"message": "Проверяю кампании..."})
    return run_async(_process_all_campaigns())