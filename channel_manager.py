"""
GramGPT — channel_manager.py
Управление каналами
По ТЗ раздел 2.3:
  - Создание личных каналов (пакетно / одиночно)
  - Закрепление канала в профиле
  - Закрепление уже существующих каналов без создания новых
"""

import asyncio
from pathlib import Path

from telethon import errors
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    UpdateUsernameRequest,
)
from telethon.tl.functions.account import (
    UpdateProfileRequest,
    UpdatePersonalChannelRequest,
)
from telethon.tl.functions.users import GetFullUserRequest

import config
import ui
from tg_client import make_client, ui_log


# ============================================================
# СОЗДАНИЕ ЛИЧНОГО КАНАЛА
# ============================================================

async def create_channel(account: dict,
                         title: str,
                         description: str = "",
                         username: str = "") -> dict | None:
    """
    Создаёт личный Telegram-канал от имени аккаунта.
    Возвращает dict с данными канала или None при ошибке.
    """
    phone = account["phone"]
    session_file = account.get("session_file", "")

    if not session_file or not Path(session_file).exists():
        ui.err(f"[{phone}] Файл сессии не найден")
        return None

    client = make_client(phone, session_path=session_file)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            ui.err(f"[{phone}] Сессия не активна")
            return None

        ui_log(phone, f"Создаю канал '{title}'...")

        result = await client(CreateChannelRequest(
            title=title,
            about=description,
            broadcast=True,   # True = канал, False = группа
            megagroup=False,
        ))

        channel = result.chats[0]
        channel_id = channel.id
        channel_link = f"https://t.me/{channel.username}" if channel.username else f"id{channel_id}"

        ui_log(phone, f"Канал создан: {title} (id={channel_id})")

        # Устанавливаем username если передан
        if username:
            try:
                await client(UpdateUsernameRequest(
                    channel=channel,
                    username=username
                ))
                channel_link = f"https://t.me/{username}"
                ui_log(phone, f"Username установлен: @{username}")
            except errors.UsernameInvalidError:
                ui.warn(f"[{phone}] Username @{username} недоступен — пропускаю")
            except Exception as e:
                ui_log(phone, f"Ошибка username: {e}")

        ui.ok(f"[{phone}] Канал создан: {channel_link}")

        return {
            "id": channel_id,
            "title": title,
            "username": username,
            "link": channel_link,
            "description": description,
        }

    except errors.FloodWaitError as e:
        ui.err(f"[{phone}] Flood wait — подожди {e.seconds} сек")
        return None
    except errors.ChannelsAdminPublicTooMuchError:
        ui.err(f"[{phone}] Превышен лимит публичных каналов")
        return None
    except Exception as e:
        ui.err(f"[{phone}] Ошибка: {type(e).__name__}: {e}")
        return None
    finally:
        await client.disconnect()


async def batch_create_channels(accounts: list[dict],
                                title_template: str,
                                description: str = "",
                                delay: float = 4.0) -> list[dict]:
    """
    Создаёт каналы для нескольких аккаунтов.
    title_template может содержать {n} — номер аккаунта,
    {name} — имя аккаунта.
    Например: "Канал {name}" → "Канал Jeka"
    """
    total = len(accounts)
    for i, account in enumerate(accounts):
        phone = account["phone"]
        name = account.get("first_name", phone)
        title = title_template.replace("{n}", str(i + 1)).replace("{name}", name)

        print(f"\n  [{i+1}/{total}] {phone} → '{title}'")

        channel = await create_channel(account, title, description)
        if channel:
            # Сохраняем данные канала в аккаунт
            account.setdefault("channels", [])
            if channel not in account["channels"]:
                account["channels"].append(channel)

        if i < total - 1:
            ui_log(phone, f"Пауза {delay}с...")
            await asyncio.sleep(delay)

    return accounts


# ============================================================
# ЗАКРЕПЛЕНИЕ КАНАЛА В ПРОФИЛЕ
# По ТЗ: закрепление личных каналов в профиле
# ============================================================

async def pin_channel_to_profile(account: dict, channel_link: str) -> bool:
    """
    Закрепляет канал в профиле аккаунта через официальный API Telegram.
    Использует UpdatePersonalChannelRequest — фича доступна с марта 2024,
    не требует Premium.
    Результат: канал отображается в первой строке профиля.
    """
    phone = account["phone"]
    session_file = account.get("session_file", "")

    if not session_file or not Path(session_file).exists():
        ui.err(f"[{phone}] Файл сессии не найден")
        return False

    client = make_client(phone, session_path=session_file)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            ui.err(f"[{phone}] Сессия не активна")
            return False

        # Получаем entity канала по ссылке
        ui_log(phone, f"Ищу канал: {channel_link}")
        try:
            channel_entity = await client.get_entity(channel_link)
        except Exception as e:
            ui.err(f"[{phone}] Не удалось найти канал '{channel_link}': {e}")
            ui.info(f"[{phone}] Убедись что канал публичный и ссылка правильная")
            return False

        ui_log(phone, f"Канал найден: {getattr(channel_entity, 'title', channel_link)}")
        ui_log(phone, "Закрепляю канал в профиле через API...")

        # Официальный вызов — закрепляет канал в профиле
        await client(UpdatePersonalChannelRequest(channel=channel_entity))

        ui.ok(f"[{phone}] Канал закреплён в профиле ✓")
        ui.info(f"[{phone}] Открой профиль в Telegram — канал появится в первой строке")
        return True

    except errors.ChatAdminRequiredError:
        ui.err(f"[{phone}] Нужны права администратора канала")
        return False
    except errors.FloodWaitError as e:
        ui.err(f"[{phone}] Flood wait — подожди {e.seconds} сек")
        return False
    except Exception as e:
        ui.err(f"[{phone}] Ошибка: {type(e).__name__}: {e}")
        return False
    finally:
        await client.disconnect()

async def batch_pin_channels(accounts: list[dict], delay: float = 2.0) -> list[dict]:
    """
    Закрепляет каналы из account["channels"] в bio каждого аккаунта.
    Использует первый канал из списка.
    """
    total = len(accounts)
    for i, account in enumerate(accounts):
        phone = account["phone"]
        channels = account.get("channels", [])

        if not channels:
            ui.warn(f"[{phone}] Нет каналов для закрепления")
            continue

        channel = channels[0]
        link = channel.get("link", "")
        if not link:
            ui.warn(f"[{phone}] Нет ссылки на канал")
            continue

        print(f"\n  [{i+1}/{total}] {phone} → {link}")
        await pin_channel_to_profile(account, link)

        if i < total - 1:
            await asyncio.sleep(delay)

    return accounts


# ============================================================
# ЗАКРЕПЛЕНИЕ УЖЕ СУЩЕСТВУЮЩЕГО КАНАЛА
# По ТЗ: закрепление существующих каналов без создания новых
# ============================================================

async def pin_existing_channel(account: dict, channel_link: str) -> bool:
    """
    Закрепляет уже существующий канал в профиле аккаунта.
    channel_link — ссылка вида https://t.me/username или @username
    """
    # Нормализуем ссылку
    if channel_link.startswith("@"):
        channel_link = f"https://t.me/{channel_link[1:]}"
    elif not channel_link.startswith("http"):
        channel_link = f"https://t.me/{channel_link}"

    phone = account["phone"]
    ui_log(phone, f"Закрепляю существующий канал: {channel_link}")

    # Сохраняем в список каналов аккаунта
    account.setdefault("channels", [])
    existing = next((c for c in account["channels"] if c.get("link") == channel_link), None)
    if not existing:
        account["channels"].append({
            "id": None,
            "title": channel_link,
            "username": "",
            "link": channel_link,
            "description": "",
        })

    return await pin_channel_to_profile(account, channel_link)


async def get_my_channels(account: dict) -> list[dict]:
    """Возвращает список каналов которыми владеет аккаунт"""
    phone = account["phone"]
    session_file = account.get("session_file", "")

    if not session_file or not Path(session_file).exists():
        ui.err(f"[{phone}] Файл сессии не найден")
        return []

    client = make_client(phone, session_path=session_file)
    channels = []

    try:
        await client.connect()
        if not await client.is_user_authorized():
            ui.err(f"[{phone}] Сессия не активна")
            return []

        dialogs = await client.get_dialogs()
        from telethon.tl.types import Channel

        for dialog in dialogs:
            entity = dialog.entity
            if isinstance(entity, Channel) and entity.broadcast and entity.creator:
                link = f"https://t.me/{entity.username}" if entity.username else f"id{entity.id}"
                channels.append({
                    "id": entity.id,
                    "title": entity.title,
                    "username": entity.username or "",
                    "link": link,
                    "members": getattr(entity, "participants_count", 0),
                })
                ui_log(phone, f"Найден канал: {entity.title} ({link})")

        ui.ok(f"[{phone}] Найдено каналов: {len(channels)}")

    except Exception as e:
        ui.err(f"[{phone}] Ошибка: {type(e).__name__}: {e}")
    finally:
        await client.disconnect()

    return channels