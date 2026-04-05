"""
GramGPT API — tasks/ai_tasks.py
Celery задачи для ИИ-диалогов.
Очередь: ai_dialogs

Логика:
1. Celery Beat запускает process_ai_dialogs каждые 30 сек
2. Таск находит все активные ИИ-диалоги (is_active=True)
3. Для каждого — проверяет новые входящие через Telethon
4. Если есть новое сообщение — отправляет в Claude API с системным промптом
5. Ответ Claude отправляет обратно контакту через Telethon
"""

import asyncio
import sys
import os
import logging
from datetime import datetime
from dotenv import load_dotenv

# Загружаем .env из api/ директории
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from celery_app import celery_app

logger = logging.getLogger(__name__)

# Подключаем api/ и корень проекта в sys.path
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


def run_async(coro):
    """Запускает async функцию внутри Celery (sync) воркера"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _get_cli_config():
    """Безопасно импортирует корневой config.py (с API_ID/API_HASH)"""
    import importlib.util

    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    config_path = os.path.join(root_dir, "config.py")

    spec = importlib.util.spec_from_file_location("cli_config", config_path)
    cli_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli_config)
    return cli_config


# ── Rate Limiter (защита от 429) ─────────────────────────────

import threading

_llm_lock = threading.Lock()
_llm_calls = []
LLM_MAX_PER_MINUTE = 10

def _check_rate_limit() -> bool:
    import time as _time
    with _llm_lock:
        now = _time.time()
        _llm_calls[:] = [t for t in _llm_calls if now - t < 60]
        if len(_llm_calls) >= LLM_MAX_PER_MINUTE:
            logger.warning(f"Rate limit: {len(_llm_calls)}/{LLM_MAX_PER_MINUTE} запросов/мин — пропускаю")
            return False
        _llm_calls.append(now)
        return True


# ── LLM Providers (для диалогов) ─────────────────────────────

def call_llm_dialog(provider: str, system_prompt: str, messages_history: list[dict]) -> str:
    """Вызывает выбранный LLM с проверкой rate limit"""
    if not _check_rate_limit():
        return ""
    if provider == "openai":
        return _call_openai_dialog(system_prompt, messages_history)
    elif provider == "gemini":
        return _call_gemini_dialog(system_prompt, messages_history)
    elif provider == "groq":
        return _call_groq_dialog(system_prompt, messages_history)
    else:
        return _call_claude_dialog(system_prompt, messages_history)


def _call_claude_dialog(system_prompt: str, messages_history: list[dict]) -> str:
    import httpx
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY не задан в .env!")
        return ""

    recent = messages_history[-20:]
    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1024,
                    "system": system_prompt,
                    "messages": recent,
                },
            )
            response.raise_for_status()
            for block in response.json().get("content", []):
                if block.get("type") == "text":
                    return block["text"]
    except Exception as e:
        logger.error(f"Claude API error: {e}")
    return ""


def _call_openai_dialog(system_prompt: str, messages_history: list[dict]) -> str:
    import httpx
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        logger.error("OPENAI_API_KEY не задан!")
        return ""

    recent = messages_history[-20:]
    msgs = [{"role": "system", "content": system_prompt}] + recent
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": "gpt-4o", "max_tokens": 1024, "messages": msgs},
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
    return ""


def _call_gemini_dialog(system_prompt: str, messages_history: list[dict]) -> str:
    import httpx
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        logger.error("GEMINI_API_KEY не задан!")
        return ""

    recent = messages_history[-20:]
    conversation = "\n".join([f"{'Я' if m['role']=='assistant' else 'Собеседник'}: {m['content']}" for m in recent])

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent?key={api_key}",
                headers={"Content-Type": "application/json"},
                json={
                    "system_instruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"parts": [{"text": conversation}]}],
                    "generationConfig": {"maxOutputTokens": 1024},
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


def _call_groq_dialog(system_prompt: str, messages_history: list[dict]) -> str:
    import httpx
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        logger.error("GROQ_API_KEY не задан!")
        return ""

    recent = messages_history[-20:]
    msgs = [{"role": "system", "content": system_prompt}] + recent
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": "llama-3.3-70b-versatile", "max_tokens": 1024, "messages": msgs},
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"Groq error: {e}")
    return ""


# ── Основная логика ──────────────────────────────────────────

async def _process_single_dialog(account_row, ai_dialog_row, db_session, proxy_row=None):
    """
    Обрабатывает один ИИ-диалог.
    Логика:
    1. Получаем последнее входящее сообщение
    2. Если его ID == last_msg_id в БД → уже обработано, пропускаем
    3. Если последнее сообщение наше (out) → мы уже ответили, пропускаем
    4. Новое входящее → вызываем LLM ОДИН РАЗ → отправляем ответ → сохраняем ID
    """
    from telethon import TelegramClient

    phone = account_row.phone
    contact_id = ai_dialog_row.contact_id
    system_prompt = ai_dialog_row.system_prompt
    session_file = account_row.session_file

    if not session_file or not os.path.exists(session_file):
        return

    # Создаём клиент с прокси (если назначен)
    from utils.telegram import make_telethon_client
    client = make_telethon_client(account_row, proxy_row)
    if not client:
        return

    try:
        await client.connect()
        if not await client.is_user_authorized():
            return

        # Получаем последние сообщения
        messages = await client.get_messages(contact_id, limit=30)
        if not messages:
            return

        last_msg = messages[0]

        # Последнее сообщение наше → мы уже ответили → ничего не делаем
        if last_msg.out:
            return

        # Нет текста → пропускаем
        if not last_msg.text:
            return

        # Уже обработали это сообщение (проверяем по ID) → пропускаем
        saved_id = getattr(ai_dialog_row, 'last_msg_id', 0) or 0
        if last_msg.id <= saved_id:
            return

        logger.info(f"[{phone}] Новое сообщение #{last_msg.id} от {contact_id}: {last_msg.text[:50]}...")

        # Сразу запоминаем ID чтобы не обрабатывать повторно (даже если LLM упадёт)
        ai_dialog_row.last_msg_id = last_msg.id
        ai_dialog_row.last_message = last_msg.text.strip()

        # Собираем историю
        history = []
        for m in reversed(messages):
            if not m.text:
                continue
            history.append({"role": "assistant" if m.out else "user", "content": m.text})

        # Вызываем LLM — ОДИН раз
        provider = getattr(ai_dialog_row, 'llm_provider', 'claude') or 'claude'
        reply = call_llm_dialog(provider, system_prompt, history)

        if not reply:
            logger.warning(f"[{phone}] LLM ({provider}) вернул пустой ответ для msg #{last_msg.id}")
            return

        # Отправляем ответ
        await client.send_message(contact_id, reply)
        logger.info(f"[{phone}] Ответ отправлен ({len(reply)} символов)")

        ai_dialog_row.messages_count = (ai_dialog_row.messages_count or 0) + 1
        ai_dialog_row.updated_at = datetime.utcnow()

    except Exception as e:
        logger.error(f"[{phone}] Ошибка в ИИ-диалоге с {contact_id}: {e}")
    finally:
        try:
            await client.disconnect()
        except:
            pass


async def _process_all_dialogs():
    """Находит все активные ИИ-диалоги и обрабатывает их"""
    api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select
    from config import DATABASE_URL
    from models.account import TelegramAccount
    from models.ai_dialog import AIDialog

    # Создаём ОТДЕЛЬНЫЙ engine для Celery (не переиспользуем API engine)
    task_engine = create_async_engine(DATABASE_URL, pool_size=2, max_overflow=0)
    TaskSession = async_sessionmaker(bind=task_engine, class_=AsyncSession, expire_on_commit=False)

    async with TaskSession() as db:
        from sqlalchemy.orm import joinedload
        try:
            result = await db.execute(
                select(AIDialog).where(AIDialog.is_active == True)
            )
            active_dialogs = result.scalars().all()

            if not active_dialogs:
                return {"processed": 0}

            logger.info(f"Активных ИИ-диалогов: {len(active_dialogs)}")
            
            processed = 0
            for ai_dialog in active_dialogs:
                acc_result = await db.execute(
                    select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(
                        TelegramAccount.id == ai_dialog.account_id
                    )
                )
                account = acc_result.scalar_one_or_none()

                if not account or account.status != "active":
                    continue

                # Загружаем прокси аккаунта
                from models.proxy import Proxy
                proxy = None
                if hasattr(account, 'proxy_id') and account.proxy_id:
                    proxy_r = await db.execute(select(Proxy).where(Proxy.id == account.proxy_id))
                    proxy = proxy_r.scalar_one_or_none()

                await _process_single_dialog(account, ai_dialog, db, proxy)
                processed += 1

            await db.commit()
            return {"processed": processed, "total": len(active_dialogs)}

        except Exception as e:
            logger.error(f"Ошибка обработки ИИ-диалогов: {e}")
            await db.rollback()
            return {"error": str(e)}
        finally:
            await task_engine.dispose()


# ── Celery Tasks ─────────────────────────────────────────────

@celery_app.task(bind=True, name="tasks.ai_tasks.process_ai_dialogs")
def process_ai_dialogs(self):
    """
    Основной таск — проверяет новые входящие и отвечает через Claude.
    Запускается Celery Beat каждые 30 секунд.
    """
    self.update_state(
        state="PROGRESS",
        meta={"message": "Проверяю новые входящие..."}
    )

    result = run_async(_process_all_dialogs())
    return result


@celery_app.task(bind=True, name="tasks.ai_tasks.process_single_account_dialogs")
def process_single_account_dialogs(self, account_id: int):
    """
    Обрабатывает ИИ-диалоги для одного конкретного аккаунта.
    Может вызываться вручную из API.
    """

    async def _process():
        api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        if api_dir not in sys.path:
            sys.path.insert(0, api_dir)

        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
        from sqlalchemy import select
        from config import DATABASE_URL
        from models.account import TelegramAccount
        from models.ai_dialog import AIDialog
        from sqlalchemy.orm import joinedload
        task_engine = create_async_engine(DATABASE_URL, pool_size=2, max_overflow=0)
        TaskSession = async_sessionmaker(bind=task_engine, class_=AsyncSession, expire_on_commit=False)

        async with TaskSession() as db:
            result = await db.execute(
                select(AIDialog).where(
                    AIDialog.account_id == account_id,
                    AIDialog.is_active == True,
                )
            )
            dialogs = result.scalars().all()

            acc_result = await db.execute(
                select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(TelegramAccount.id == account_id)
            )
            account = acc_result.scalar_one_or_none()

            if not account:
                await task_engine.dispose()
                return {"error": "Аккаунт не найден"}

            # Загружаем прокси
            from models.proxy import Proxy
            proxy = None
            if hasattr(account, 'proxy_id') and account.proxy_id:
                proxy_r = await db.execute(select(Proxy).where(Proxy.id == account.proxy_id))
                proxy = proxy_r.scalar_one_or_none()

            processed = 0
            for ai_dialog in dialogs:
                await _process_single_dialog(account, ai_dialog, db, proxy)
                processed += 1

            await db.commit()
            await task_engine.dispose()
            return {"processed": processed}

    return run_async(_process())