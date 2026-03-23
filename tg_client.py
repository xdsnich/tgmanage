"""
GramGPT — tg_client.py
Вся работа с Telegram через Telethon

ВАЖНО — почему make_client именно такой:
  Telethon при каждом connect() отправляет Telegram параметры устройства.
  Если device_model/system_version меняются между сессиями —
  Telegram считает это новым устройством и завершает все остальные сессии.
  Поэтому параметры зафиксированы и одинаковы ВЕЗДЕ в проекте.

ВАЖНО — почему нет @SpamBot при авторизации:
  Отправка сообщений сразу после входа — красный флаг для Telegram.
  SpamBot проверяется только при явном запросе пользователя (пункт 3 меню).
"""

import asyncio
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, errors
from telethon.tl.functions.account import GetAuthorizationsRequest
from telethon.tl.functions.users import GetFullUserRequest

import config
from db import make_account_template
import trust as trust_module


# ============================================================
# ЕДИНАЯ ТОЧКА СОЗДАНИЯ КЛИЕНТА
# Все модули проекта должны использовать только эту функцию
# ============================================================

def make_client(phone: str, proxy: dict = None,
                session_path: str = None) -> TelegramClient:
    """
    Создаёт TelegramClient с фиксированными параметрами устройства.
    Одинаковые параметры при каждом подключении = Telegram не видит "новое устройство".

    session_path — если передан, используется этот путь (из account["session_file"]).
    Иначе строится из config.SESSIONS_DIR / phone.
    """
    if session_path:
        # Убираем .session — Telethon добавит сам
        session_path = str(session_path).replace(".session", "")
    else:
        session_path = str(config.SESSIONS_DIR / phone.replace("+", ""))

    tg_proxy = None
    if proxy and proxy.get("is_valid"):
        import socks
        proto = socks.SOCKS5 if proxy["protocol"] == "socks5" else socks.HTTP
        tg_proxy = (proto, proxy["host"], int(proxy["port"]),
                    True, proxy.get("login"), proxy.get("password"))

    return TelegramClient(
        str(session_path),
        config.API_ID,
        config.API_HASH,
        proxy=tg_proxy,
        device_model="Desktop",
        system_version="Windows 10",
        app_version="4.14.15",
        lang_code="ru",
        system_lang_code="ru"
    )


# ============================================================
# АВТОРИЗАЦИЯ
# ============================================================

async def authorize(phone: str, proxy: dict = None) -> dict:
    """
    Авторизует новый аккаунт.
    НЕ проверяет спамблок при авторизации — это делается отдельно.
    """
    if not config.API_ID or not config.API_HASH:
        return {**make_account_template(phone), "status": "error",
                "error": "API_ID / API_HASH не заполнены в .env"}

    client = make_client(phone, proxy)

    try:
        await client.connect()
        ui_log(phone, f"Подключился. Проверяю авторизацию...")

        if not await client.is_user_authorized():
            sent = await client.send_code_request(phone)

            code_type = type(sent.type).__name__
            if "App" in code_type:
                print(f"  📲 Код отправлен в приложение Telegram")
                print(f"     Открой Telegram на телефоне — сообщение от 'Telegram'")
            elif "Sms" in code_type:
                print(f"  📱 Код отправлен по SMS на {phone}")
            elif "Call" in code_type:
                print(f"  📞 Сейчас будет звонок с кодом")
            else:
                print(f"  📨 Код отправлен (тип: {code_type})")

            code = input(f"  Введи код (5 цифр): ").strip().replace(" ", "")

            try:
                await client.sign_in(
                    phone=phone,
                    code=code,
                    phone_code_hash=sent.phone_code_hash
                )
            except errors.SessionPasswordNeededError:
                print(f"  🔐 На аккаунте включена 2FA")
                password = input(f"  Введи пароль 2FA: ").strip()
                await client.sign_in(password=password)

        ui_log(phone, "Авторизован успешно. Получаю данные профиля...")
        # Получаем базовую инфу БЕЗ проверки спамблока
        account = await _fetch_info(client, phone, check_spam=False)
        return account

    except errors.PhoneCodeInvalidError:
        return {**make_account_template(phone), "status": "error",
                "error": "Неверный код"}
    except errors.PhoneCodeExpiredError:
        return {**make_account_template(phone), "status": "error",
                "error": "Код истёк — начни авторизацию заново"}
    except errors.PhoneNumberInvalidError:
        return {**make_account_template(phone), "status": "error",
                "error": "Неверный номер телефона"}
    except errors.FloodWaitError as e:
        return {**make_account_template(phone), "status": "error",
                "error": f"Слишком много попыток — подожди {e.seconds} сек"}
    except Exception as e:
        return {**make_account_template(phone), "status": "error", "error": str(e)}
    finally:
        await client.disconnect()
        ui_log(phone, "Отключился")


# ============================================================
# ПРОВЕРКА СТАТУСА (с опциональным спамботом)
# ============================================================

async def check(account: dict, check_spam: bool = True) -> dict:
    """
    Проверяет статус аккаунта.
    check_spam=True — проверяет спамблок через @SpamBot (медленнее)
    check_spam=False — только базовая проверка (быстро, безопасно)
    """
    phone = account["phone"]
    session_file = account.get("session_file", "")

    if not session_file or not Path(session_file).exists():
        ui_log(phone, f"Файл сессии не найден: {session_file}")
        account["status"] = "error"
        account["error"] = "Файл сессии не найден"
        account["last_checked"] = datetime.now().isoformat()
        return account

    client = make_client(phone, session_path=session_file)

    try:
        await client.connect()
        authorized = await client.is_user_authorized()
        ui_log(phone, f"Авторизован: {authorized}")

        if not authorized:
            account["status"] = "frozen"
            account["last_checked"] = datetime.now().isoformat()
            account["trust_score"] = trust_module.calculate(account)
            return account

        updated = await _fetch_info(client, phone, check_spam=check_spam)
        # Сохраняем мета-поля
        for key in ["tags", "notes", "role", "proxy", "added_at", "session_file"]:
            updated[key] = account.get(key)
        return updated

    except errors.AuthKeyUnregisteredError:
        ui_log(phone, "AuthKeyUnregistered — аккаунт заморожен")
        account["status"] = "frozen"
    except errors.UserDeactivatedBanError:
        ui_log(phone, "UserDeactivatedBan — аккаунт заблокирован")
        account["status"] = "frozen"
    except Exception as e:
        ui_log(phone, f"Ошибка: {type(e).__name__}: {e}")
        account["status"] = "error"
        account["error"] = str(e)
    finally:
        await client.disconnect()

    account["last_checked"] = datetime.now().isoformat()
    account["trust_score"] = trust_module.calculate(account)
    return account


# ============================================================
# ВНУТРЕННИЕ ФУНКЦИИ
# ============================================================

async def _fetch_info(client: TelegramClient, phone: str,
                      check_spam: bool = True) -> dict:
    """Получает данные аккаунта. check_spam контролирует проверку @SpamBot."""
    account = make_account_template(phone)

    me = await client.get_me()
    account["id"] = me.id
    account["first_name"] = me.first_name or ""
    account["last_name"] = me.last_name or ""
    account["username"] = me.username or ""
    account["phone"] = me.phone or phone
    account["has_photo"] = bool(me.photo)
    account["session_file"] = str(config.SESSIONS_DIR / phone.replace("+", "")) + ".session"

    ui_log(phone, f"Профиль: {account['first_name']} {account['username']}")

    # Bio
    try:
        full = await client(GetFullUserRequest(me))
        account["bio"] = full.full_user.about or ""
    except Exception:
        account["bio"] = ""

    # Активные сессии
    try:
        auths = await client(GetAuthorizationsRequest())
        account["active_sessions"] = len(auths.authorizations)
        ui_log(phone, f"Активных сессий: {account['active_sessions']}")
    except Exception:
        account["active_sessions"] = 1

    # Спамблок — только если явно запрошено
    if check_spam:
        print(f"    ⏳ Проверяю спамблок...", end=" ", flush=True)
        has_spam = await _check_spamblock(client)
        if has_spam:
            account["status"] = "spamblock"
            print("🚫 СПАМБЛОК")
        else:
            account["status"] = "active"
            print("✅ Чисто")
    else:
        account["status"] = "active"
        ui_log(phone, "Проверка спамблока пропущена (check_spam=False)")

    account["last_checked"] = datetime.now().isoformat()
    account["trust_score"] = trust_module.calculate(account)
    return account


async def _check_spamblock(client: TelegramClient) -> bool:
    """Проверяет спамблок через @SpamBot"""
    try:
        async with client.conversation("@SpamBot", timeout=config.BOT_TIMEOUT) as conv:
            await conv.send_message("/start")
            await asyncio.sleep(1)
            response = await conv.get_response()
            text = response.text.lower()
            if any(w in text for w in ["free", "no limits", "нет ограничений", "не ограничен"]):
                return False
            if any(w in text for w in ["spam", "limited", "ограничен", "заблокирован"]):
                return True
        return False
    except Exception:
        return False


# ============================================================
# УТИЛИТА — единый формат логов
# ============================================================

def ui_log(phone: str, message: str):
    """Единый формат лога для всех операций с аккаунтом"""
    print(f"  ℹ️  [{phone}] {message}")