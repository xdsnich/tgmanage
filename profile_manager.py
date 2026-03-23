"""
GramGPT — profile_manager.py
Управление профилями аккаунтов
По ТЗ: имя/фамилия, bio, аватарка — одиночно и пакетно
Теги, роли, заметки

ВАЖНО: используем make_client из tg_client везде —
чтобы device_model/system_version были одинаковыми при каждом
подключении. Иначе Telegram видит "новое устройство" и
выкидывает все остальные сессии.
"""

import asyncio
from pathlib import Path

from telethon import errors
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.photos import UploadProfilePhotoRequest

import config
import ui
import trust as trust_module
# Используем make_client из tg_client — там правильные device параметры
from tg_client import make_client


# ============================================================
# ОБНОВЛЕНИЕ ИМЕНИ / ФАМИЛИИ / BIO
# ============================================================

async def update_profile(account: dict, first_name: str = None,
                         last_name: str = None, bio: str = None) -> bool:
    phone = account["phone"]
    session_file = account.get("session_file", "")

    # Проверяем что сессия существует
    if not session_file or not Path(session_file).exists():
        ui.err(f"[{phone}] Файл сессии не найден: {session_file}")
        ui.info(f"[{phone}] Переавторизуй аккаунт через пункт 1")
        return False

    ui.info(f"[{phone}] Сессия: {session_file}")

    # make_client использует тот же session_path что записался при авторизации
    # и те же device_model/system_version — Telegram не увидит "новое устройство"
    client = make_client(phone, session_path=session_file)

    try:
        ui.info(f"[{phone}] Подключаюсь...")
        await client.connect()

        authorized = await client.is_user_authorized()
        ui.info(f"[{phone}] Авторизован: {authorized}")

        if not authorized:
            ui.err(f"[{phone}] Сессия не активна — переавторизуй аккаунт (пункт 1)")
            return False

        kwargs = {}
        if first_name is not None:
            kwargs["first_name"] = first_name
            ui.info(f"[{phone}] Имя → {first_name}")
        if last_name is not None:
            kwargs["last_name"] = last_name
            ui.info(f"[{phone}] Фамилия → {last_name}")
        if bio is not None:
            kwargs["about"] = bio[:70]
            ui.info(f"[{phone}] Bio → {bio[:70]}")

        await client(UpdateProfileRequest(**kwargs))
        ui.ok(f"[{phone}] Профиль обновлён ✓")
        return True

    except errors.FloodWaitError as e:
        ui.err(f"[{phone}] Flood wait — подожди {e.seconds} сек")
        return False
    except Exception as e:
        ui.err(f"[{phone}] Ошибка: {type(e).__name__}: {e}")
        return False
    finally:
        await client.disconnect()
        ui.info(f"[{phone}] Отключился")


async def batch_update_profile(accounts: list[dict], first_name: str = None,
                               last_name: str = None, bio: str = None,
                               delay: float = 2.0) -> list[dict]:
    total = len(accounts)
    for i, account in enumerate(accounts):
        print(f"\n  [{i+1}/{total}] {account['phone']}")
        ok = await update_profile(account, first_name, last_name, bio)
        if ok:
            if first_name is not None:
                account["first_name"] = first_name
            if last_name is not None:
                account["last_name"] = last_name
            if bio is not None:
                account["bio"] = bio
            account["trust_score"] = trust_module.calculate(account)

        if i < total - 1:
            ui.info(f"Пауза {delay}с...")
            await asyncio.sleep(delay)

    return accounts


# ============================================================
# АВАТАРКА
# ============================================================

async def set_avatar(account: dict, image_path: str) -> bool:
    phone = account["phone"]
    path = Path(image_path)

    if not path.exists():
        ui.err(f"[{phone}] Файл не найден: {image_path}")
        return False
    if path.suffix.lower() not in [".jpg", ".jpeg", ".png"]:
        ui.err(f"[{phone}] Только JPG/PNG, получен: {path.suffix}")
        return False

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

        size_kb = path.stat().st_size // 1024
        ui.info(f"[{phone}] Загружаю {path.name} ({size_kb} KB)...")
        uploaded = await client.upload_file(str(path))
        await client(UploadProfilePhotoRequest(file=uploaded))
        ui.ok(f"[{phone}] Аватарка установлена ✓")
        return True

    except errors.FloodWaitError as e:
        ui.err(f"[{phone}] Flood wait — подожди {e.seconds} сек")
        return False
    except Exception as e:
        ui.err(f"[{phone}] Ошибка: {type(e).__name__}: {e}")
        return False
    finally:
        await client.disconnect()


async def batch_set_avatar(accounts: list[dict], image_path: str,
                           delay: float = 3.0) -> list[dict]:
    total = len(accounts)
    for i, account in enumerate(accounts):
        print(f"\n  [{i+1}/{total}] {account['phone']}")
        ok = await set_avatar(account, image_path)
        if ok:
            account["has_photo"] = True
            account["trust_score"] = trust_module.calculate(account)
        if i < total - 1:
            ui.info(f"Пауза {delay}с...")
            await asyncio.sleep(delay)
    return accounts


# ============================================================
# ТЕГИ, РОЛИ, ЗАМЕТКИ (локально — без запросов к Telegram)
# ============================================================

VALID_ROLES = ["default", "продавец", "прогреватель", "читатель", "консультант"]


def set_tag(account: dict, tag: str) -> dict:
    tags = account.get("tags", [])
    if tag not in tags:
        tags.append(tag)
    account["tags"] = tags
    ui.ok(f"[{account['phone']}] Тег '{tag}' добавлен")
    return account


def remove_tag(account: dict, tag: str) -> dict:
    before = len(account.get("tags", []))
    account["tags"] = [t for t in account.get("tags", []) if t != tag]
    if len(account["tags"]) < before:
        ui.ok(f"[{account['phone']}] Тег '{tag}' удалён")
    else:
        ui.warn(f"[{account['phone']}] Тег '{tag}' не найден")
    return account


def set_role(account: dict, role: str) -> dict:
    if role not in VALID_ROLES:
        ui.warn(f"Роль '{role}' нестандартная. Допустимые: {', '.join(VALID_ROLES)}")
    account["role"] = role
    ui.ok(f"[{account['phone']}] Роль → {role}")
    return account


def set_note(account: dict, note: str) -> dict:
    account["notes"] = note
    ui.ok(f"[{account['phone']}] Заметка сохранена")
    return account