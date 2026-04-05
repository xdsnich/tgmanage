"""
GramGPT API — tasks/bulk_tasks.py
Пакетные Celery задачи. Все через make_telethon_client (с прокси).
Очередь: bulk_actions
"""

import asyncio
import sys
import os
import logging
import time

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


async def _get_client_for_phone(phone: str):
    from sqlalchemy.orm import joinedload
    """Загружает аккаунт + прокси из БД, возвращает TelegramClient."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select
    from config import DATABASE_URL
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.telegram import make_telethon_client

    engine = create_async_engine(DATABASE_URL, pool_size=1, max_overflow=0)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        acc_r = await db.execute(select(TelegramAccount).options(joinedload(TelegramAccount.api_app)).where(TelegramAccount.phone == phone))
        account = acc_r.scalar_one_or_none()
        if not account:
            await engine.dispose()
            return None, None

        proxy = None
        if account.proxy_id:
            proxy_r = await db.execute(select(Proxy).where(Proxy.id == account.proxy_id))
            proxy = proxy_r.scalar_one_or_none()

        client = make_telethon_client(account, proxy)
        await engine.dispose()
        return client, proxy


# ── Универсальная обёртка ────────────────────────────────────

async def _run_bulk(accounts, action_fn, action_name):
    """Выполняет action_fn(client, phone) для каждого аккаунта с прокси."""
    results = []
    for acc in accounts:
        phone = acc.get("phone", "?")
        client, proxy = await _get_client_for_phone(phone)
        if not client:
            results.append({"phone": phone, "success": False, "error": "Аккаунт/сессия не найдены"})
            continue

        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                results.append({"phone": phone, "success": False, "error": "Сессия не активна"})
                continue

            msg = await action_fn(client, phone)
            await client.disconnect()
            results.append({"phone": phone, "success": True, "message": msg})
        except Exception as e:
            try: await client.disconnect()
            except: pass
            results.append({"phone": phone, "success": False, "error": str(e)[:200]})

    return results


# ── Action функции ───────────────────────────────────────────

async def _update_profile(client, phone, first_name=None, last_name=None, bio=None):
    from telethon.tl.functions.account import UpdateProfileRequest
    kwargs = {}
    if first_name is not None: kwargs['first_name'] = first_name
    if last_name is not None: kwargs['last_name'] = last_name
    if bio is not None: kwargs['about'] = bio
    if kwargs:
        await client(UpdateProfileRequest(**kwargs))
    return f"Профиль обновлён"


async def _set_avatar(client, phone, image_path):
    from telethon.tl.functions.photos import UploadProfilePhotoRequest
    if not os.path.exists(image_path):
        return "Файл аватарки не найден"
    file = await client.upload_file(image_path)
    await client(UploadProfilePhotoRequest(file=file))
    return "Аватарка установлена"


async def _leave_chats(client, phone):
    from telethon.tl.types import Chat, Channel
    from telethon.tl.functions.channels import LeaveChannelRequest
    dialogs = await client.get_dialogs()
    groups = [d for d in dialogs if isinstance(d.entity, (Chat, Channel))
              and not (isinstance(d.entity, Channel) and d.entity.broadcast)]
    left = 0
    for d in groups:
        try:
            await client(LeaveChannelRequest(d.entity))
            left += 1
            await asyncio.sleep(1)
        except: pass
    return f"Вышел из {left} чатов"


async def _leave_channels(client, phone):
    from telethon.tl.types import Channel
    from telethon.tl.functions.channels import LeaveChannelRequest
    dialogs = await client.get_dialogs()
    channels = [d for d in dialogs if isinstance(d.entity, Channel) and d.entity.broadcast]
    left = 0
    for d in channels:
        try:
            await client(LeaveChannelRequest(d.entity))
            left += 1
            await asyncio.sleep(1)
        except: pass
    return f"Отписался от {left} каналов"


async def _read_all(client, phone):
    dialogs = await client.get_dialogs()
    unread = [d for d in dialogs if d.unread_count > 0]
    read = 0
    for d in unread:
        try:
            await client.send_read_acknowledge(d.entity)
            read += 1
            await asyncio.sleep(0.3)
        except: pass
    return f"Прочитано {read} диалогов"


async def _delete_chats(client, phone):
    from telethon.tl.types import User as TgUser
    from telethon.tl.functions.messages import DeleteHistoryRequest
    dialogs = await client.get_dialogs()
    private = [d for d in dialogs if isinstance(d.entity, TgUser) and not d.entity.bot and not d.entity.is_self]
    deleted = 0
    for d in private:
        try:
            await client(DeleteHistoryRequest(peer=d.entity, max_id=0, revoke=False))
            deleted += 1
            await asyncio.sleep(0.5)
        except: pass
    return f"Удалено {deleted} переписок"


async def _unpin_folders(client, phone):
    from telethon.tl.functions.messages import UpdateDialogFilterRequest
    removed = 0
    for fid in range(2, 11):
        try:
            await client(UpdateDialogFilterRequest(id=fid))
            removed += 1
        except: pass
    return f"Удалено {removed} папок"


async def _terminate_sessions(client, phone):
    from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
    result = await client(GetAuthorizationsRequest())
    terminated = 0
    for auth in result.authorizations:
        if auth.current:
            continue
        try:
            await client(ResetAuthorizationRequest(hash=auth.hash))
            terminated += 1
            await asyncio.sleep(0.5)
        except: pass
    return f"Завершено {terminated} сессий"


async def _set_2fa(client, phone, password, hint):
    from telethon.tl.functions.account import UpdatePasswordSettingsRequest, GetPasswordRequest
    from telethon.tl.types import InputCheckPasswordEmpty
    from telethon.password import compute_check

    pwd = await client(GetPasswordRequest())
    if pwd.has_password:
        return "2FA уже установлена"

    await client(UpdatePasswordSettingsRequest(
        password=InputCheckPasswordEmpty(),
        new_settings={'new_algo': pwd.new_algo,
                      'new_password_hash': compute_check(pwd, password),
                      'hint': hint or ''}
    ))
    return "2FA установлена"


async def _create_channel(client, phone, title, description):
    from telethon.tl.functions.channels import CreateChannelRequest
    result = await client(CreateChannelRequest(title=title, about=description, broadcast=True, megagroup=False))
    ch = result.chats[0]
    link = f"https://t.me/{ch.username}" if ch.username else f"id{ch.id}"
    return {"id": ch.id, "title": title, "link": link}


# ── Celery Tasks ─────────────────────────────────────────────

@celery_app.task(bind=True, name="tasks.bulk_tasks.update_profiles_bulk")
def update_profiles_bulk(self, accounts: list[dict], first_name=None, last_name=None, bio=None):
    total = len(accounts)
    results = []
    for i, acc in enumerate(accounts):
        phone = acc.get("phone", "?")
        self.update_state(state="PROGRESS", meta={"current": i+1, "total": total,
                          "percent": int((i+1)/total*100), "message": f"[{i+1}/{total}] Профиль {phone}..."})
        try:
            async def do():
                client, _ = await _get_client_for_phone(phone)
                if not client: return "Не найден"
                await client.connect()
                msg = await _update_profile(client, phone, first_name, last_name, bio)
                await client.disconnect()
                return msg
            msg = run_async(do())
            results.append({"phone": phone, "success": True, "message": msg})
        except Exception as e:
            results.append({"phone": phone, "success": False, "error": str(e)[:200]})
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.set_avatars_bulk")
def set_avatars_bulk(self, accounts: list[dict], image_path: str):
    total = len(accounts)
    results = []
    for i, acc in enumerate(accounts):
        phone = acc.get("phone", "?")
        self.update_state(state="PROGRESS", meta={"current": i+1, "total": total,
                          "percent": int((i+1)/total*100), "message": f"[{i+1}/{total}] Аватарка {phone}..."})
        try:
            async def do():
                client, _ = await _get_client_for_phone(phone)
                if not client: return "Не найден"
                await client.connect()
                msg = await _set_avatar(client, phone, image_path)
                await client.disconnect()
                return msg
            msg = run_async(do())
            results.append({"phone": phone, "success": True, "message": msg})
        except Exception as e:
            results.append({"phone": phone, "success": False, "error": str(e)[:200]})
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.leave_chats_bulk")
def leave_chats_bulk(self, accounts: list[dict]):
    total = len(accounts)
    self.update_state(state="PROGRESS", meta={"current": 0, "total": total, "message": "Начинаю..."})
    results = run_async(_run_bulk(accounts, _leave_chats, "Выход из чатов"))
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.leave_channels_bulk")
def leave_channels_bulk(self, accounts: list[dict]):
    total = len(accounts)
    results = run_async(_run_bulk(accounts, _leave_channels, "Отписка от каналов"))
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.read_all_bulk")
def read_all_bulk(self, accounts: list[dict]):
    total = len(accounts)
    results = run_async(_run_bulk(accounts, _read_all, "Прочитать всё"))
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.delete_chats_bulk")
def delete_chats_bulk(self, accounts: list[dict]):
    total = len(accounts)
    results = run_async(_run_bulk(accounts, _delete_chats, "Удаление переписок"))
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.unpin_folders_bulk")
def unpin_folders_bulk(self, accounts: list[dict]):
    total = len(accounts)
    results = run_async(_run_bulk(accounts, _unpin_folders, "Открепление папок"))
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.set_2fa_bulk")
def set_2fa_bulk(self, accounts: list[dict], password: str, hint: str = ""):
    total = len(accounts)
    results = []
    for i, acc in enumerate(accounts):
        phone = acc.get("phone", "?")
        self.update_state(state="PROGRESS", meta={"current": i+1, "total": total,
                          "percent": int((i+1)/total*100), "message": f"[{i+1}/{total}] 2FA {phone}..."})
        try:
            async def do():
                client, _ = await _get_client_for_phone(phone)
                if not client: return "Не найден"
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    return "Сессия не активна"
                msg = await _set_2fa(client, phone, password, hint)
                await client.disconnect()
                return msg
            msg = run_async(do())
            results.append({"phone": phone, "success": True, "message": msg})
        except Exception as e:
            results.append({"phone": phone, "success": False, "error": str(e)[:200]})
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.terminate_sessions_bulk")
def terminate_sessions_bulk(self, accounts: list[dict]):
    total = len(accounts)
    results = run_async(_run_bulk(accounts, _terminate_sessions, "Завершение сессий"))
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}


@celery_app.task(bind=True, name="tasks.bulk_tasks.create_channels_bulk")
def create_channels_bulk(self, accounts: list[dict], title_template: str, description: str = "", delay: float = 4.0):
    total = len(accounts)
    results = []
    for i, acc in enumerate(accounts):
        phone = acc.get("phone", "?")
        name = acc.get("first_name", phone)
        title = title_template.replace("{n}", str(i+1)).replace("{name}", name)
        self.update_state(state="PROGRESS", meta={"current": i+1, "total": total,
                          "percent": int((i+1)/total*100), "message": f"[{i+1}/{total}] Канал '{title}'..."})
        try:
            async def do():
                client, _ = await _get_client_for_phone(phone)
                if not client: return None
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    return None
                ch = await _create_channel(client, phone, title, description)
                await client.disconnect()
                return ch
            channel = run_async(do())
            results.append({"phone": phone, "success": bool(channel), "channel": channel})
        except Exception as e:
            results.append({"phone": phone, "success": False, "error": str(e)[:200]})
        if i < total - 1:
            time.sleep(delay)
    return {"total": total, "success": sum(1 for r in results if r.get("success")), "results": results}