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


# ── Claude API ───────────────────────────────────────────────

def call_claude(system_prompt: str, messages_history: list[dict]) -> str:
    """
    Вызывает Claude API для генерации ответа.
    messages_history: [{role: "user"/"assistant", content: "..."}]
    """
    import httpx

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY не задан в .env!")
        return ""

    # Берём последние 20 сообщений для контекста (чтобы не перегружать)
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
            data = response.json()

            # Извлекаем текст из ответа
            for block in data.get("content", []):
                if block.get("type") == "text":
                    return block["text"]

            return ""

    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return ""


# ── Основная логика ──────────────────────────────────────────

async def _process_single_dialog(account_row, ai_dialog_row, db_session):
    """Обрабатывает один ИИ-диалог: проверяет новые входящие, отвечает через Claude"""
    from telethon import TelegramClient

    cli_config = _get_cli_config()
    phone = account_row.phone
    contact_id = ai_dialog_row.contact_id
    system_prompt = ai_dialog_row.system_prompt
    session_file = account_row.session_file

    if not session_file or not os.path.exists(session_file):
        logger.warning(f"[{phone}] Файл сессии не найден: {session_file}")
        return

    session_path = session_file.replace(".session", "")
    client = TelegramClient(
        session_path,
        cli_config.API_ID,
        cli_config.API_HASH,
        device_model="Desktop",
        system_version="Windows 10",
        app_version="4.14.15",
    )

    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.warning(f"[{phone}] Сессия не активна")
            return

        # Получаем последние сообщения с контактом
        messages = await client.get_messages(contact_id, limit=30)

        if not messages:
            return

        # Проверяем — последнее сообщение от контакта (не от нас)?
        last_msg = messages[0]
        if last_msg.out:
            # Последнее сообщение наше — значит мы уже ответили, пропускаем
            return

        if not last_msg.text:
            return

        # Проверяем не отвечали ли мы уже на это сообщение
        # Простая проверка: если last_message в БД совпадает — пропускаем
        last_text = last_msg.text.strip()
        if ai_dialog_row.last_message == last_text:
            return

        logger.info(f"[{phone}] Новое сообщение от {contact_id}: {last_text[:50]}...")

        # Собираем историю для Claude
        history = []
        for m in reversed(messages):
            if not m.text:
                continue
            role = "assistant" if m.out else "user"
            history.append({"role": role, "content": m.text})

        # Вызываем Claude API
        reply = call_claude(system_prompt, history)

        if not reply:
            logger.warning(f"[{phone}] Claude вернул пустой ответ")
            return

        # Отправляем ответ контакту
        await client.send_message(contact_id, reply)
        logger.info(f"[{phone}] Ответ отправлен ({len(reply)} символов)")

        # Обновляем last_message и счётчик в БД
        ai_dialog_row.last_message = last_text
        ai_dialog_row.messages_count = (ai_dialog_row.messages_count or 0) + 1
        ai_dialog_row.updated_at = datetime.utcnow()

        # Добавляем задержку чтобы не нагружать API
        await asyncio.sleep(2)

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
                    select(TelegramAccount).where(
                        TelegramAccount.id == ai_dialog.account_id
                    )
                )
                account = acc_result.scalar_one_or_none()

                if not account or account.status != "active":
                    continue

                await _process_single_dialog(account, ai_dialog, db)
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
                select(TelegramAccount).where(TelegramAccount.id == account_id)
            )
            account = acc_result.scalar_one_or_none()

            if not account:
                await task_engine.dispose()
                return {"error": "Аккаунт не найден"}

            processed = 0
            for ai_dialog in dialogs:
                await _process_single_dialog(account, ai_dialog, db)
                processed += 1

            await db.commit()
            await task_engine.dispose()
            return {"processed": processed}

    return run_async(_process())