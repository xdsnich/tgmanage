"""
GramGPT — security.py
Безопасность и управление сессиями
По ТЗ раздел 3:
  - Переавторизация (сброс старой сессии → новая)
  - Завершение всех сторонних сессий
  - Установка 2FA (одиночно и пакетно)
  - Получение кодов авторизации внутри программы
  - Экспорт сессий в JSON
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from telethon import errors
from telethon.tl.functions.account import (
    GetAuthorizationsRequest,
    ResetAuthorizationRequest,
    UpdatePasswordSettingsRequest,
    GetPasswordRequest,
)
from telethon.tl.functions.auth import (
    ResetAuthorizationsRequest,
    ExportAuthorizationRequest,
)
from telethon.tl.types import (
    InputCheckPasswordEmpty,
    InputCheckPasswordSRP,
)
from telethon.password import compute_check

import config
import ui
from tg_client import make_client, ui_log
from db import save_accounts


# ============================================================
# ПЕРЕАВТОРИЗАЦИЯ
# Сброс старой сессии + создание новой
# По ТЗ: отключает предыдущих владельцев аккаунта
# ============================================================

async def reauthorize(account: dict) -> dict:
    """
    Переавторизует аккаунт:
    1. Завершает ВСЕ активные сессии (включая текущую)
    2. Удаляет локальный .session файл
    3. Запускает новую авторизацию по SMS/коду
    Используется чтобы гарантированно отключить прежних владельцев.
    """
    phone = account["phone"]
    session_file = account.get("session_file", "")

    ui_log(phone, "Начинаю переавторизацию...")

    # Шаг 1 — завершаем все сессии через текущую
    client = make_client(phone, session_path=session_file)
    try:
        await client.connect()
        if await client.is_user_authorized():
            ui_log(phone, "Завершаю все активные сессии...")
            await client(ResetAuthorizationsRequest())
            ui_log(phone, "Все сессии завершены")
        await client.disconnect()
    except Exception as e:
        ui_log(phone, f"Не удалось завершить сессии: {e}")
        await client.disconnect()

    # Шаг 2 — удаляем локальный файл сессии
    if session_file and Path(session_file).exists():
        try:
            Path(session_file).unlink()
            ui_log(phone, f"Старая сессия удалена: {session_file}")
        except Exception as e:
            ui.err(f"[{phone}] Не удалось удалить файл сессии: {e}")

    # Шаг 3 — новая авторизация
    ui_log(phone, "Запускаю новую авторизацию...")
    from tg_client import authorize
    new_account = await authorize(phone)

    if new_account.get("status") == "error":
        ui.err(f"[{phone}] Ошибка переавторизации: {new_account.get('error')}")
        # Возвращаем старый аккаунт с пометкой
        account["status"] = "error"
        account["error"] = "Переавторизация не удалась"
        return account

    # Сохраняем мета-поля из старого аккаунта
    for key in ["tags", "notes", "role", "proxy", "added_at"]:
        new_account[key] = account.get(key)

    # Бонус Trust Score за успешную переавторизацию
    new_account["trust_score"] = min(100, new_account.get("trust_score", 0) + 1)
    ui.ok(f"[{phone}] Переавторизация успешна! Trust +1")
    return new_account


# ============================================================
# ЗАВЕРШЕНИЕ СТОРОННИХ СЕССИЙ
# Кикает все устройства кроме текущего
# ============================================================

async def terminate_other_sessions(account: dict) -> dict:
    """
    Завершает все сторонние активные сессии.
    Текущая сессия (программа) остаётся активной.
    """
    phone = account["phone"]
    session_file = account.get("session_file", "")

    if not session_file or not Path(session_file).exists():
        ui.err(f"[{phone}] Файл сессии не найден")
        return account

    client = make_client(phone, session_path=session_file)
    terminated = 0

    try:
        await client.connect()
        if not await client.is_user_authorized():
            ui.err(f"[{phone}] Сессия не активна")
            return account

        # Получаем все авторизации
        result = await client(GetAuthorizationsRequest())
        authorizations = result.authorizations

        ui_log(phone, f"Активных сессий: {len(authorizations)}")

        for auth in authorizations:
            # Пропускаем текущую сессию (current=True)
            if auth.current:
                ui_log(phone, f"Текущая сессия: {auth.app_name} ({auth.device_model}) — пропускаю")
                continue

            try:
                await client(ResetAuthorizationRequest(hash=auth.hash))
                ui_log(phone, f"Завершил: {auth.app_name} на {auth.device_model} ({auth.country})")
                terminated += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                ui_log(phone, f"Не удалось завершить {auth.app_name}: {e}")

        account["active_sessions"] = 1  # Осталась только текущая
        ui.ok(f"[{phone}] Завершено сторонних сессий: {terminated}")

    except Exception as e:
        ui.err(f"[{phone}] Ошибка: {type(e).__name__}: {e}")
    finally:
        await client.disconnect()

    return account


# ============================================================
# СПИСОК АКТИВНЫХ СЕССИЙ
# ============================================================

async def list_sessions(account: dict) -> list[dict]:
    """Возвращает список всех активных сессий аккаунта"""
    phone = account["phone"]
    session_file = account.get("session_file", "")

    if not session_file or not Path(session_file).exists():
        ui.err(f"[{phone}] Файл сессии не найден")
        return []

    client = make_client(phone, session_path=session_file)
    sessions = []

    try:
        await client.connect()
        if not await client.is_user_authorized():
            ui.err(f"[{phone}] Сессия не активна")
            return []

        result = await client(GetAuthorizationsRequest())

        for auth in result.authorizations:
            sessions.append({
                "hash": auth.hash,
                "app_name": auth.app_name,
                "app_version": auth.app_version,
                "device_model": auth.device_model,
                "platform": auth.platform,
                "system_version": auth.system_version,
                "country": auth.country,
                "region": auth.region,
                "current": auth.current,
                "date_created": str(auth.date_created),
                "date_active": str(auth.date_active),
            })

        ui_log(phone, f"Получено сессий: {len(sessions)}")

    except Exception as e:
        ui.err(f"[{phone}] Ошибка: {type(e).__name__}: {e}")
    finally:
        await client.disconnect()

    return sessions


# ============================================================
# УСТАНОВКА 2FA
# По ТЗ: одиночная и групповая установка
# ============================================================

async def set_2fa(account: dict, password: str, hint: str = "") -> bool:
    """
    Устанавливает двухфакторную аутентификацию.
    Если 2FA уже установлена — меняет пароль.
    """
    phone = account["phone"]
    session_file = account.get("session_file", "")

    if not session_file or not Path(session_file).exists():
        ui.err(f"[{phone}] Файл сессии не найден")
        return False

    if len(password) < 6:
        ui.err(f"[{phone}] Пароль слишком короткий (минимум 6 символов)")
        return False

    client = make_client(phone, session_path=session_file)

    try:
        await client.connect()
        if not await client.is_user_authorized():
            ui.err(f"[{phone}] Сессия не активна")
            return False

        ui_log(phone, "Получаю текущие настройки пароля...")
        pwd_info = await client(GetPasswordRequest())

        ui_log(phone, f"2FA сейчас: {'включена' if pwd_info.has_password else 'выключена'}")

        # Устанавливаем новый пароль через edit_2fa
        await client.edit_2fa(
            current_password=None if not pwd_info.has_password else None,
            new_password=password,
            hint=hint,
        )

        ui.ok(f"[{phone}] 2FA установлена успешно ✓")
        # Бонус Trust Score
        account["trust_score"] = min(100, account.get("trust_score", 0) + config.TRUST_SCORE.get("active_ok", 2))
        return True

    except errors.PasswordHashInvalidError:
        ui.err(f"[{phone}] Неверный текущий пароль 2FA")
        return False
    except errors.FloodWaitError as e:
        ui.err(f"[{phone}] Flood wait {e.seconds}с")
        return False
    except Exception as e:
        ui.err(f"[{phone}] Ошибка: {type(e).__name__}: {e}")
        return False
    finally:
        await client.disconnect()


async def batch_set_2fa(accounts: list[dict], password: str,
                        hint: str = "", delay: float = 3.0) -> list[dict]:
    """Устанавливает 2FA на несколько аккаунтов"""
    total = len(accounts)
    for i, account in enumerate(accounts):
        print(f"\n  [{i+1}/{total}] {account['phone']}")
        ok = await set_2fa(account, password, hint)
        if ok:
            account["has_2fa"] = True

        if i < total - 1:
            ui_log(account["phone"], f"Пауза {delay}с...")
            await asyncio.sleep(delay)

    return accounts


# ============================================================
# ПОЛУЧЕНИЕ КОДА АВТОРИЗАЦИИ
# По ТЗ: получение кодов внутри программы для входа с других устройств
# ============================================================

async def get_auth_code(account: dict) -> bool:
    """
    Читает последний код авторизации из системных сообщений Telegram.
    Telegram присылает коды через служебный аккаунт +42777.
    Это нужно когда кто-то пытается войти в аккаунт с другого устройства
    и нужно получить код не имея телефона рядом.
    """
    phone = account["phone"]
    session_file = account.get("session_file", "")

    if not session_file or not Path(session_file).exists():
        ui.err(f"[{phone}] Файл сессии не найден")
        return False

    client = make_client(phone, session_path=session_file)

    try:
        ui_log(phone, "Подключаюсь...")
        await client.connect()

        # Диагностика — показываем точный путь который используется
        ui_log(phone, f"Путь к сессии: {session_file}")
        ui_log(phone, f"Файл существует: {Path(session_file).exists()}")
        ui_log(phone, f"Размер файла: {Path(session_file).stat().st_size if Path(session_file).exists() else 0} байт")

        authorized = await client.is_user_authorized()
        ui_log(phone, f"is_user_authorized: {authorized}")

        if not authorized:
            # Пробуем напрямую get_me() — иногда is_user_authorized врёт
            ui_log(phone, "Пробую get_me() напрямую...")
            try:
                me = await client.get_me()
                if me:
                    ui_log(phone, f"get_me() успешно: {me.first_name} ({me.phone})")
                    authorized = True
                else:
                    ui.err(f"[{phone}] Сессия не активна — переавторизуй аккаунт")
                    return False
            except Exception as e:
                ui_log(phone, f"get_me() тоже не работает: {e}")
                ui.err(f"[{phone}] Сессия не активна — переавторизуй аккаунт")
                return False

        # 777000 — числовой ID официального сервисного аккаунта Telegram
        # Именно сюда приходят коды авторизации
        ui_log(phone, "Читаю сообщения от Telegram (ID 777000)...")

        try:
            messages = await client.get_messages(777000, limit=5)
        except Exception as e:
            ui_log(phone, f"Ошибка чтения 777000: {e}")
            messages = []

        if not messages:
            ui.warn(f"[{phone}] Нет сообщений от Telegram")
            ui.info(f"[{phone}] Попробуй войти с другого устройства — потом повтори")
            return False

        print(f"\n  {chr(9472) * 54}")
        print(f"  Последние сообщения от Telegram для {phone}:")
        print(f"  {chr(9472) * 54}")
        for msg in messages:
            if msg.text:
                date = str(msg.date)[:16].replace("T", " ")
                print(f"  [{date}] {msg.text[:120]}")
        print(f"  {chr(9472) * 54}")

        return True

    except errors.FloodWaitError as e:
        ui.err(f"[{phone}] Flood wait — подожди {e.seconds} сек")
        return False
    except Exception as e:
        ui.err(f"[{phone}] Ошибка: {type(e).__name__}: {e}")
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

# ============================================================
# ЭКСПОРТ СЕССИЙ В JSON
# По ТЗ: экспорт сессий для интеграции со сторонним ПО
# ============================================================

async def export_sessions_json(accounts: list[dict]) -> str:
    """
    Экспортирует данные сессий в JSON файл.
    Включает: путь к .session файлу, номер, статус, метаданные.
    """
    export_data = []

    for account in accounts:
        session_file = account.get("session_file", "")
        session_exists = Path(session_file).exists() if session_file else False

        entry = {
            "phone": account.get("phone"),
            "session_file": session_file,
            "session_exists": session_exists,
            "status": account.get("status"),
            "first_name": account.get("first_name"),
            "username": account.get("username"),
            "trust_score": account.get("trust_score"),
            "exported_at": datetime.now().isoformat(),
        }
        export_data.append(entry)

    export_path = config.DATA_DIR / f"sessions_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(export_path, "w", encoding="utf-8") as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)

    ui.ok(f"Экспортировано {len(export_data)} сессий → {export_path}")
    return str(export_path)