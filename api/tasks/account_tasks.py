"""
GramGPT API — tasks/account_tasks.py
Celery задачи для работы с аккаунтами
Очередь: high_priority
"""

import asyncio
import sys
import os
from datetime import datetime

from celery import shared_task
from celery_app import celery_app

# Подключаем CLI модули
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


def run_async(coro):
    """Запускает async функцию внутри Celery (sync) воркера"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(bind=True, name="tasks.account_tasks.check_account")
def check_account(self, account_dict: dict, check_spam: bool = False) -> dict:
    """
    Проверяет статус одного аккаунта.
    bind=True — доступ к self для обновления прогресса.
    """
    phone = account_dict.get("phone", "?")

    self.update_state(
        state="PROGRESS",
        meta={"phone": phone, "step": "connecting", "message": f"Подключаюсь к {phone}..."}
    )

    try:
        import tg_client
        updated = run_async(tg_client.check(account_dict, check_spam=check_spam))

        self.update_state(
            state="PROGRESS",
            meta={
                "phone": phone,
                "step": "done",
                "message": f"{phone} — {updated.get('status', 'unknown')}",
                "trust_score": updated.get("trust_score", 0),
            }
        )

        return {
            "success": True,
            "phone": phone,
            "status": updated.get("status"),
            "trust_score": updated.get("trust_score"),
            "last_checked": updated.get("last_checked"),
            "account": updated,
        }

    except Exception as e:
        return {
            "success": False,
            "phone": phone,
            "error": str(e),
        }


@celery_app.task(bind=True, name="tasks.account_tasks.check_accounts_bulk")
def check_accounts_bulk(self, accounts: list[dict], check_spam: bool = False) -> dict:
    """
    Мультипоточная проверка нескольких аккаунтов.
    Обновляет прогресс после каждого аккаунта.
    """
    total = len(accounts)
    results = []

    self.update_state(
        state="PROGRESS",
        meta={"current": 0, "total": total, "message": "Начинаю проверку..."}
    )

    for i, account in enumerate(accounts):
        phone = account.get("phone", "?")

        self.update_state(
            state="PROGRESS",
            meta={
                "current": i + 1,
                "total": total,
                "percent": int((i + 1) / total * 100),
                "message": f"[{i+1}/{total}] Проверяю {phone}...",
                "phone": phone,
            }
        )

        try:
            import tg_client
            updated = run_async(tg_client.check(account, check_spam=check_spam))
            results.append({
                "success": True,
                "phone": phone,
                "status": updated.get("status"),
                "trust_score": updated.get("trust_score"),
                "account": updated,
            })
        except Exception as e:
            results.append({
                "success": False,
                "phone": phone,
                "error": str(e),
            })

    active = sum(1 for r in results if r.get("status") == "active")
    spam   = sum(1 for r in results if r.get("status") == "spamblock")

    return {
        "total":   total,
        "active":  active,
        "spam":    spam,
        "results": results,
    }


@celery_app.task(bind=True, name="tasks.account_tasks.authorize_account")
def authorize_account(self, phone: str, code: str, phone_code_hash: str) -> dict:
    """Завершает авторизацию аккаунта (второй шаг после send_code)"""
    self.update_state(
        state="PROGRESS",
        meta={"phone": phone, "step": "signing_in", "message": f"Вхожу в {phone}..."}
    )

    try:
        from telethon import TelegramClient, errors
        import config as cli_config

        session_path = str(cli_config.SESSIONS_DIR / phone.replace("+", ""))
        client = TelegramClient(
            session_path,
            cli_config.API_ID,
            cli_config.API_HASH,
            device_model="Desktop",
            system_version="Windows 10",
            app_version="4.14.15",
        )

        async def do_auth():
            await client.connect()
            await client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=phone_code_hash
            )
            me = await client.get_me()
            await client.disconnect()
            return me

        me = run_async(do_auth())

        return {
            "success": True,
            "phone": phone,
            "first_name": me.first_name or "",
            "username": me.username or "",
            "session_file": session_path + ".session",
        }

    except Exception as e:
        return {"success": False, "phone": phone, "error": str(e)}
