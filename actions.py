"""
GramGPT — actions.py
Быстрые действия с чатами и кэшем
По ТЗ раздел 5: выход из чатов, отписка от каналов,
удаление переписок, прочитать все, карантин
"""

import asyncio
from datetime import datetime
from pathlib import Path

from telethon import errors
from telethon.tl.functions.messages import (
    DeleteHistoryRequest,
    ReadHistoryRequest,
    GetDialogsRequest,
)
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.types import (
    InputPeerEmpty,
    Channel,
    Chat,
    User,
    DialogFilter,
)
from telethon.tl.functions.messages import UpdateDialogFilterRequest

import config
import ui
import trust as trust_module
from tg_client import make_client, ui_log
from db import save_accounts


# ============================================================
# КАРАНТИН
# ============================================================

def set_quarantine(account: dict, reason: str = "системный мут") -> dict:
    """
    Переводит аккаунт в карантин.
    По ТЗ: умная защита при получении системного мута.
    Штраф Trust Score: -3 балла за системный мут.
    """
    account["status"] = "quarantine"
    account["quarantine_reason"] = reason
    account["quarantine_at"] = datetime.now().isoformat()
    # Штраф по ТЗ
    account["trust_score"] = max(0, account.get("trust_score", 0) + config.TRUST_SCORE["system_mute"])
    ui.warn(f"[{account['phone']}] → КАРАНТИН: {reason} (Trust −3)")
    return account


def lift_quarantine(account: dict) -> dict:
    """Снимает карантин вручную"""
    account["status"] = "active"
    account["quarantine_reason"] = None
    account["quarantine_at"] = None
    ui.ok(f"[{account['phone']}] Карантин снят")
    return account


# ============================================================
# ВЫХОД ИЗ ВСЕХ ЧАТОВ
# ============================================================

async def leave_all_chats(account: dict, delay: float = 1.5) -> dict:
    """
    Выходит из всех групп и чатов аккаунта.
    По ТЗ: автоматизированный выход из всех чатов.
    """
    phone = account["phone"]
    session_file = account.get("session_file", "")

    if not session_file or not Path(session_file).exists():
        ui.err(f"[{phone}] Файл сессии не найден")
        return account

    client = make_client(phone, session_path=session_file)
    left = 0
    failed = 0

    try:
        await client.connect()
        if not await client.is_user_authorized():
            ui.err(f"[{phone}] Сессия не активна")
            return account

        ui_log(phone, "Получаю список диалогов...")
        dialogs = await client.get_dialogs()
        groups = [d for d in dialogs if isinstance(d.entity, (Chat, Channel))
                  and not (isinstance(d.entity, Channel) and d.entity.broadcast)]

        ui_log(phone, f"Групп/чатов для выхода: {len(groups)}")

        for dialog in groups:
            try:
                entity = dialog.entity
                name = getattr(entity, 'title', str(entity.id))
                await client(LeaveChannelRequest(entity))
                ui_log(phone, f"Вышел из: {name}")
                left += 1
                await asyncio.sleep(delay)
            except errors.FloodWaitError as e:
                ui.warn(f"[{phone}] Flood wait {e.seconds}с — пауза...")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                ui_log(phone, f"Не удалось выйти из {name}: {e}")
                failed += 1

        ui.ok(f"[{phone}] Вышел из {left} чатов. Ошибок: {failed}")

    except Exception as e:
        ui.err(f"[{phone}] Ошибка: {type(e).__name__}: {e}")
    finally:
        await client.disconnect()

    return account


# ============================================================
# ОТПИСКА ОТ КАНАЛОВ
# ============================================================

async def leave_all_channels(account: dict, delay: float = 1.5) -> dict:
    """
    Отписывается от всех каналов (broadcast).
    По ТЗ: отписка от всех каналов.
    """
    phone = account["phone"]
    session_file = account.get("session_file", "")

    if not session_file or not Path(session_file).exists():
        ui.err(f"[{phone}] Файл сессии не найден")
        return account

    client = make_client(phone, session_path=session_file)
    left = 0
    failed = 0

    try:
        await client.connect()
        if not await client.is_user_authorized():
            ui.err(f"[{phone}] Сессия не активна")
            return account

        dialogs = await client.get_dialogs()
        channels = [d for d in dialogs
                    if isinstance(d.entity, Channel) and d.entity.broadcast]

        ui_log(phone, f"Каналов для отписки: {len(channels)}")

        for dialog in channels:
            try:
                name = getattr(dialog.entity, 'title', '?')
                await client(LeaveChannelRequest(dialog.entity))
                ui_log(phone, f"Отписался: {name}")
                left += 1
                await asyncio.sleep(delay)
            except errors.FloodWaitError as e:
                ui.warn(f"[{phone}] Flood wait {e.seconds}с")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                ui_log(phone, f"Ошибка ({name}): {e}")
                failed += 1

        ui.ok(f"[{phone}] Отписался от {left} каналов. Ошибок: {failed}")

    except Exception as e:
        ui.err(f"[{phone}] Ошибка: {type(e).__name__}: {e}")
    finally:
        await client.disconnect()

    return account


# ============================================================
# УДАЛЕНИЕ ЛИЧНЫХ ПЕРЕПИСОК
# ============================================================

async def delete_private_chats(account: dict, delay: float = 1.0) -> dict:
    """
    Удаляет историю личных переписок.
    По ТЗ: удаление личных переписок.
    """
    phone = account["phone"]
    session_file = account.get("session_file", "")

    if not session_file or not Path(session_file).exists():
        ui.err(f"[{phone}] Файл сессии не найден")
        return account

    client = make_client(phone, session_path=session_file)
    deleted = 0
    failed = 0

    try:
        await client.connect()
        if not await client.is_user_authorized():
            ui.err(f"[{phone}] Сессия не активна")
            return account

        dialogs = await client.get_dialogs()
        private = [d for d in dialogs if isinstance(d.entity, User)
                   and not d.entity.bot and not d.entity.is_self]

        ui_log(phone, f"Личных переписок: {len(private)}")

        for dialog in private:
            try:
                name = f"{getattr(dialog.entity, 'first_name', '')} {getattr(dialog.entity, 'last_name', '')}".strip()
                await client(DeleteHistoryRequest(
                    peer=dialog.entity,
                    max_id=0,
                    revoke=False  # False = удаляем только у себя
                ))
                ui_log(phone, f"Удалена переписка с: {name or dialog.entity.id}")
                deleted += 1
                await asyncio.sleep(delay)
            except errors.FloodWaitError as e:
                ui.warn(f"[{phone}] Flood wait {e.seconds}с")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                ui_log(phone, f"Ошибка: {e}")
                failed += 1

        ui.ok(f"[{phone}] Удалено {deleted} переписок. Ошибок: {failed}")

    except Exception as e:
        ui.err(f"[{phone}] Ошибка: {type(e).__name__}: {e}")
    finally:
        await client.disconnect()

    return account


# ============================================================
# ПРОЧИТАТЬ ВСЕ СООБЩЕНИЯ
# ============================================================

async def read_all_messages(account: dict, delay: float = 0.5) -> dict:
    """
    Отмечает все сообщения как прочитанные.
    По ТЗ: отметка всех сообщений как прочитанных.
    """
    phone = account["phone"]
    session_file = account.get("session_file", "")

    if not session_file or not Path(session_file).exists():
        ui.err(f"[{phone}] Файл сессии не найден")
        return account

    client = make_client(phone, session_path=session_file)
    read = 0

    try:
        await client.connect()
        if not await client.is_user_authorized():
            ui.err(f"[{phone}] Сессия не активна")
            return account

        dialogs = await client.get_dialogs()
        unread = [d for d in dialogs if d.unread_count > 0]
        ui_log(phone, f"Непрочитанных диалогов: {len(unread)}")

        for dialog in unread:
            try:
                await client.send_read_acknowledge(dialog.entity)
                read += 1
                await asyncio.sleep(delay)
            except Exception as e:
                ui_log(phone, f"Ошибка в {dialog.name}: {e}")

        ui.ok(f"[{phone}] Прочитано {read} диалогов")

    except Exception as e:
        ui.err(f"[{phone}] Ошибка: {type(e).__name__}: {e}")
    finally:
        await client.disconnect()

    return account


# ============================================================
# ОТКРЕПЛЕНИЕ ПАПОК (высвобождение лимитов)
# ============================================================

async def unpin_folders(account: dict) -> dict:
    """
    Удаляет все созданные папки (фильтры диалогов).
    По ТЗ: открепление созданных папок для высвобождения лимитов.
    """
    phone = account["phone"]
    session_file = account.get("session_file", "")

    if not session_file or not Path(session_file).exists():
        ui.err(f"[{phone}] Файл сессии не найден")
        return account

    client = make_client(phone, session_path=session_file)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            ui.err(f"[{phone}] Сессия не активна")
            return account

        # Удаляем все фильтры с id 2-10 (стандартный диапазон Telegram)
        removed = 0
        for folder_id in range(2, 11):
            try:
                await client(UpdateDialogFilterRequest(id=folder_id))
                removed += 1
            except Exception:
                pass

        ui.ok(f"[{phone}] Удалено папок: {removed}")

    except Exception as e:
        ui.err(f"[{phone}] Ошибка: {type(e).__name__}: {e}")
    finally:
        await client.disconnect()

    return account


# ============================================================
# ПАКЕТНЫЕ ОБЁРТКИ
# ============================================================

async def batch_action(accounts: list[dict], action_fn,
                       action_name: str, delay_between: float = 3.0) -> list[dict]:
    """
    Универсальная обёртка для пакетного выполнения любого действия.
    action_fn — async функция принимающая один аккаунт.
    """
    total = len(accounts)
    for i, account in enumerate(accounts):
        print(f"\n  [{i+1}/{total}] {account['phone']}")
        accounts[i] = await action_fn(account)

        if i < total - 1:
            ui_log(account['phone'], f"Пауза {delay_between}с перед следующим...")
            await asyncio.sleep(delay_between)

    save_accounts(accounts)
    ui.ok(f"{action_name} завершено для {total} аккаунтов")
    return accounts