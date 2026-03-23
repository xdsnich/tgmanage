"""
GramGPT — tdata_importer.py
Импорт аккаунтов из TData формата
По ТЗ раздел 1: поддержка форматов TData и Session

TData — это папка данных Telegram Desktop.
Содержит зашифрованные данные сессии.
Конвертируем TData → .session через opentele или telethon-tdata.
"""

import asyncio
import shutil
from pathlib import Path
from datetime import datetime

import config
import ui
from db import make_account_template, save_accounts
import trust as trust_module


# ============================================================
# ПРОВЕРКА ЗАВИСИМОСТЕЙ
# ============================================================

def check_dependencies() -> bool:
    """Проверяет наличие нужных библиотек для работы с TData"""
    try:
        import opentele  # type: ignore
        return True
    except ImportError:
        pass

    try:
        from telethon_tdata import TData  # type: ignore
        return True
    except ImportError:
        pass

    return False


# ============================================================
# ИМПОРТ ЧЕРЕЗ OPENTELE
# ============================================================

async def import_tdata_opentele(tdata_path: str, phone_hint: str = "") -> dict | None:
    """
    Конвертирует TData папку в Telethon-сессию через opentele.
    pip install opentele
    """
    try:
        from opentele.td import TDesktop  # type: ignore
        from opentele.api import UseCurrentSession  # type: ignore
    except ImportError:
        ui.err("opentele не установлен. Запусти: pip install opentele")
        return None

    path = Path(tdata_path)
    if not path.exists():
        ui.err(f"Папка TData не найдена: {tdata_path}")
        return None

    ui_log_plain(f"Читаю TData из: {tdata_path}")

    try:
        tdesk = TDesktop(str(path))

        if not tdesk.isLoaded():
            ui.err("Не удалось загрузить TData — возможно повреждена или зашифрована паролем")
            return None

        ui_log_plain(f"Аккаунтов в TData: {tdesk.accountsCount}")

        # Берём первый аккаунт
        account_td = tdesk.accounts[0]

        # Определяем номер телефона
        phone = phone_hint or f"tdata_{datetime.now().strftime('%H%M%S')}"

        # Путь для новой сессии
        session_path = config.SESSIONS_DIR / phone.replace("+", "")
        session_file = str(session_path) + ".session"

        ui_log_plain(f"Конвертирую в сессию: {session_file}")

        # Конвертируем TData → Telethon session
        client = await account_td.ToTelethon(
            session=str(session_path),
            flag=UseCurrentSession,
            api=account_td.api
        )

        await client.connect()

        if not await client.is_user_authorized():
            ui.err("TData загружена но авторизация не прошла")
            await client.disconnect()
            return None

        me = await client.get_me()
        real_phone = me.phone or phone
        ui_log_plain(f"Авторизован: {me.first_name} (+{real_phone})")

        # Переименовываем сессию по реальному номеру
        real_session_path = config.SESSIONS_DIR / real_phone
        real_session_file = str(real_session_path) + ".session"

        if str(session_path) != str(real_session_path):
            try:
                Path(session_file).rename(real_session_file)
                session_file = real_session_file
                ui_log_plain(f"Сессия переименована: {session_file}")
            except Exception as e:
                ui_log_plain(f"Не удалось переименовать: {e}")

        await client.disconnect()

        # Формируем аккаунт
        account = make_account_template(f"+{real_phone}")
        account["id"] = me.id
        account["first_name"] = me.first_name or ""
        account["last_name"] = me.last_name or ""
        account["username"] = me.username or ""
        account["phone"] = f"+{real_phone}"
        account["has_photo"] = bool(me.photo)
        account["session_file"] = session_file
        account["status"] = "active"
        account["trust_score"] = trust_module.calculate(account)

        ui.ok(f"TData импортирован: +{real_phone} ({me.first_name})")
        return account

    except Exception as e:
        ui.err(f"Ошибка импорта TData: {type(e).__name__}: {e}")
        return None


# ============================================================
# ИМПОРТ ЧЕРЕЗ TELETHON-TDATA (альтернатива)
# ============================================================

async def import_tdata_telethon(tdata_path: str, phone_hint: str = "") -> dict | None:
    """
    Альтернативный метод через telethon-tdata.
    pip install telethon-tdata
    """
    try:
        from telethon_tdata import TData  # type: ignore
    except ImportError:
        ui.err("telethon-tdata не установлен. Запусти: pip install telethon-tdata")
        return None

    path = Path(tdata_path)
    if not path.exists():
        ui.err(f"Папка TData не найдена: {tdata_path}")
        return None

    try:
        ui_log_plain(f"Читаю TData: {tdata_path}")
        tdata = TData(str(path))
        sessions = tdata.export(str(config.SESSIONS_DIR))

        if not sessions:
            ui.err("Не удалось извлечь сессии из TData")
            return None

        session_file = sessions[0]
        ui_log_plain(f"Сессия извлечена: {session_file}")

        # Подключаемся чтобы получить данные аккаунта
        from telethon import TelegramClient
        client = TelegramClient(
            session_file.replace(".session", ""),
            config.API_ID,
            config.API_HASH
        )

        await client.connect()

        if not await client.is_user_authorized():
            ui.err("Сессия из TData не авторизована")
            await client.disconnect()
            return None

        me = await client.get_me()
        real_phone = f"+{me.phone}" if me.phone else phone_hint
        await client.disconnect()

        account = make_account_template(real_phone)
        account["id"] = me.id
        account["first_name"] = me.first_name or ""
        account["last_name"] = me.last_name or ""
        account["username"] = me.username or ""
        account["phone"] = real_phone
        account["has_photo"] = bool(me.photo)
        account["session_file"] = session_file
        account["status"] = "active"
        account["trust_score"] = trust_module.calculate(account)

        ui.ok(f"TData импортирован: {real_phone} ({me.first_name})")
        return account

    except Exception as e:
        ui.err(f"Ошибка: {type(e).__name__}: {e}")
        return None


# ============================================================
# ГЛАВНАЯ ФУНКЦИЯ ИМПОРТА
# Автоматически выбирает метод
# ============================================================

async def import_tdata(tdata_path: str, phone_hint: str = "") -> dict | None:
    """
    Импортирует аккаунт из TData папки.
    Автоматически пробует opentele, затем telethon-tdata.
    """
    ui_log_plain(f"Начинаю импорт TData: {tdata_path}")

    # Сначала пробуем opentele
    try:
        import opentele  # type: ignore
        ui_log_plain("Используем opentele")
        return await import_tdata_opentele(tdata_path, phone_hint)
    except ImportError:
        pass

    # Потом telethon-tdata
    try:
        import telethon_tdata  # type: ignore
        ui_log_plain("Используем telethon-tdata")
        return await import_tdata_telethon(tdata_path, phone_hint)
    except ImportError:
        pass

    # Ни одна библиотека не установлена
    ui.err("Не найдены библиотеки для TData импорта")
    print("""
  Для импорта TData установи одну из библиотек:

  Вариант 1 (рекомендуется):
    pip install opentele

  Вариант 2:
    pip install telethon-tdata
""")
    return None


async def batch_import_tdata(tdata_paths: list[str],
                             existing_accounts: list[dict],
                             delay: float = 2.0) -> list[dict]:
    """Импортирует несколько TData папок"""
    total = len(tdata_paths)
    imported = 0

    for i, path in enumerate(tdata_paths):
        print(f"\n  [{i+1}/{total}] Импортирую: {path}")
        account = await import_tdata(path)

        if account:
            # Проверяем нет ли уже такого аккаунта
            phone = account["phone"]
            exists = any(a["phone"] == phone for a in existing_accounts)
            if exists:
                ui.warn(f"Аккаунт {phone} уже существует — пропускаю")
            else:
                existing_accounts.append(account)
                imported += 1
                ui.ok(f"Добавлен: {phone}")

        if i < total - 1:
            await asyncio.sleep(delay)

    save_accounts(existing_accounts)
    ui.ok(f"Импортировано {imported} из {total} TData аккаунтов")
    return existing_accounts


# ============================================================
# УТИЛИТА
# ============================================================

def ui_log_plain(message: str):
    print(f"  ℹ️   {message}")