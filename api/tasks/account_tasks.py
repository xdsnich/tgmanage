"""
GramGPT API — tasks/account_tasks.py
Celery задачи для проверки аккаунтов.
Все подключения через make_telethon_client (с прокси).
Очередь: high_priority
"""

import asyncio
import sys
import os
import logging
from datetime import datetime

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


async def _check_account_with_proxy(account_dict: dict, check_spam: bool = False) -> dict:
    """Проверяет аккаунт через make_telethon_client с прокси из БД."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select
    from config import DATABASE_URL
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.telegram import make_telethon_client

    phone = account_dict.get("phone", "?")

    engine = create_async_engine(DATABASE_URL, pool_size=1, max_overflow=0)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with Session() as db:
            # Ищем аккаунт в БД по номеру
            acc_r = await db.execute(
                select(TelegramAccount).where(TelegramAccount.phone == phone)
            )
            account = acc_r.scalar_one_or_none()

            if not account or not account.session_file:
                return {**account_dict, "status": "error", "error": "Аккаунт не найден в БД",
                        "last_checked": datetime.utcnow().isoformat()}

            # Загружаем прокси
            proxy = None
            if account.proxy_id:
                proxy_r = await db.execute(select(Proxy).where(Proxy.id == account.proxy_id))
                proxy = proxy_r.scalar_one_or_none()
                if proxy:
                    logger.info(f"[{phone}] Проверка через прокси {proxy.host}:{proxy.port}")

            client = make_telethon_client(account, proxy)
            if not client:
                return {**account_dict, "status": "error", "error": "Session file не найден",
                        "last_checked": datetime.utcnow().isoformat()}

            try:
                await client.connect()
                authorized = await client.is_user_authorized()

                if not authorized:
                    await client.disconnect()
                    account.status = "frozen"
                    account.last_checked = datetime.utcnow()
                    await db.commit()
                    return {**account_dict, "status": "frozen",
                            "last_checked": datetime.utcnow().isoformat()}

                # Получаем инфу
                me = await client.get_me()
                from telethon.tl.functions.account import GetAuthorizationsRequest
                from telethon.tl.functions.users import GetFullUserRequest

                bio = ""
                try:
                    full = await client(GetFullUserRequest(me))
                    bio = full.full_user.about or ""
                except:
                    pass

                sessions_count = 1
                try:
                    auths = await client(GetAuthorizationsRequest())
                    sessions_count = len(auths.authorizations)
                except:
                    pass

                # SpamBot check
                status = "active"
                if check_spam:
                    try:
                        async with client.conversation("@SpamBot", timeout=10) as conv:
                            await conv.send_message("/start")
                            await asyncio.sleep(1)
                            response = await conv.get_response()
                            text = response.text.lower()
                            if any(w in text for w in ["spam", "limited", "ограничен", "заблокирован"]):
                                status = "spamblock"
                    except:
                        pass

                await client.disconnect()

                # Обновляем в БД
                account.status = status
                account.first_name = me.first_name or ""
                account.last_name = me.last_name or ""
                account.username = me.username or ""
                account.bio = bio
                account.has_photo = bool(me.photo)
                account.active_sessions = sessions_count
                account.tg_id = me.id
                account.last_checked = datetime.utcnow()

                # Trust score
                if ROOT_DIR not in sys.path:
                    sys.path.insert(0, ROOT_DIR)
                api_config_cache = sys.modules.pop('config', None)
                try:
                    import trust as trust_module
                    trust_dict = {
                        "first_name": me.first_name or "", "last_name": me.last_name or "",
                        "username": me.username or "", "bio": bio,
                        "has_photo": bool(me.photo), "status": status,
                    }
                    account.trust_score = trust_module.calculate(trust_dict)
                except:
                    pass
                finally:
                    if api_config_cache:
                        sys.modules['config'] = api_config_cache

                await db.commit()

                return {
                    "phone": phone, "status": status,
                    "trust_score": account.trust_score,
                    "first_name": me.first_name or "",
                    "username": me.username or "",
                    "last_checked": datetime.utcnow().isoformat(),
                }

            except Exception as e:
                try: await client.disconnect()
                except: pass

                err_name = type(e).__name__
                if "AuthKeyUnregistered" in err_name:
                    account.status = "frozen"
                elif "UserDeactivatedBan" in err_name:
                    account.status = "frozen"
                else:
                    account.status = "error"
                    account.error = str(e)[:200]

                account.last_checked = datetime.utcnow()
                await db.commit()

                return {**account_dict, "status": account.status,
                        "error": str(e)[:200], "last_checked": datetime.utcnow().isoformat()}

    finally:
        await engine.dispose()


@celery_app.task(bind=True, name="tasks.account_tasks.check_account")
def check_account(self, account_dict: dict, check_spam: bool = False) -> dict:
    """Проверяет статус одного аккаунта (с прокси)."""
    phone = account_dict.get("phone", "?")
    self.update_state(state="PROGRESS",
                      meta={"phone": phone, "step": "connecting", "message": f"Проверяю {phone}..."})

    try:
        result = run_async(_check_account_with_proxy(account_dict, check_spam))
        return {"success": True, **result}
    except Exception as e:
        return {"success": False, "phone": phone, "error": str(e)}


@celery_app.task(bind=True, name="tasks.account_tasks.check_accounts_bulk")
def check_accounts_bulk(self, accounts: list[dict], check_spam: bool = False) -> dict:
    """Проверка нескольких аккаунтов (с прокси)."""
    total = len(accounts)
    results = []

    self.update_state(state="PROGRESS",
                      meta={"current": 0, "total": total, "message": "Начинаю проверку..."})

    for i, account in enumerate(accounts):
        phone = account.get("phone", "?")
        self.update_state(state="PROGRESS",
                          meta={"current": i + 1, "total": total,
                                "percent": int((i + 1) / total * 100),
                                "message": f"[{i+1}/{total}] Проверяю {phone}..."})

        try:
            result = run_async(_check_account_with_proxy(account, check_spam))
            results.append({"success": True, "phone": phone,
                            "status": result.get("status"), "trust_score": result.get("trust_score")})
        except Exception as e:
            results.append({"success": False, "phone": phone, "error": str(e)})

    active = sum(1 for r in results if r.get("status") == "active")
    spam = sum(1 for r in results if r.get("status") == "spamblock")

    return {"total": total, "active": active, "spam": spam, "results": results}


@celery_app.task(bind=True, name="tasks.account_tasks.authorize_account")
def authorize_account(self, phone: str, code: str, phone_code_hash: str) -> dict:
    """Завершает авторизацию (не используется — авторизация через tg_auth.py)"""
    return {"success": False, "phone": phone, "error": "Используйте /tg-auth/confirm"}