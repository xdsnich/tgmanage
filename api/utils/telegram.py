"""
GramGPT API — utils/telegram.py
Единая точка создания TelegramClient с прокси.
Использует python-socks (async) + dict формат — проверено, работает.
"""

import os
import logging
import importlib.util
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))


def get_cli_config():
    config_path = os.path.join(ROOT_DIR, "config.py")
    spec = importlib.util.spec_from_file_location("cli_config", config_path)
    cli_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli_config)
    return cli_config


def _build_proxy(proxy_row):
    """
    Строит proxy dict для Telethon + python-socks.
    Формат проверен — работает с asyncio.run() и ThreadPoolExecutor.
    """
    if not proxy_row:
        return None

    host = proxy_row.host if hasattr(proxy_row, 'host') else proxy_row.get('host')
    port = proxy_row.port if hasattr(proxy_row, 'port') else proxy_row.get('port')
    login = (proxy_row.login if hasattr(proxy_row, 'login') else proxy_row.get('login', '')) or ''
    password = (proxy_row.password if hasattr(proxy_row, 'password') else proxy_row.get('password', '')) or ''
    protocol = proxy_row.protocol if hasattr(proxy_row, 'protocol') else proxy_row.get('protocol', 'socks5')
    proto_str = protocol.value if hasattr(protocol, 'value') else str(protocol)

    proxy = {
        'proxy_type': proto_str,
        'addr': str(host),
        'port': int(port),
        'rdns': True,
    }

    if login:
        proxy['username'] = login
    if password:
        proxy['password'] = password

    logger.info(f"✅ Прокси: {proto_str}://{host}:{port}")
    return proxy


# ── Device Profiles ──────────────────────────────────────────
# lang НЕ хранится в профиле — всегда "en" (нейтрально для любого гео)

DEVICE_PROFILES = [
    {"device": "Desktop",           "system": "Windows 10",  "app_version": "4.14.15"},
    {"device": "Desktop",           "system": "Windows 11",  "app_version": "4.16.8"},
    {"device": "Desktop",           "system": "macOS 14.2",  "app_version": "10.6.2"},
    {"device": "iPhone 14 Pro",     "system": "iOS 17.4",    "app_version": "10.8.3"},
    {"device": "iPhone 13",         "system": "iOS 17.2",    "app_version": "10.7.1"},
    {"device": "iPhone 15",         "system": "iOS 17.5",    "app_version": "10.9.1"},
    {"device": "Samsung Galaxy S23","system": "Android 14",  "app_version": "10.12.0"},
    {"device": "Samsung Galaxy A54","system": "Android 14",  "app_version": "10.11.2"},
    {"device": "Xiaomi 13",         "system": "Android 13",  "app_version": "10.10.1"},
    {"device": "Google Pixel 8",    "system": "Android 14",  "app_version": "10.12.0"},
    {"device": "OnePlus 11",        "system": "Android 14",  "app_version": "10.11.0"},
    {"device": "iPad Pro",          "system": "iOS 17.4",    "app_version": "10.8.3"},
]


def _get_device_fingerprint(phone: str) -> dict:
    """
    Возвращает ВСЕГДА один и тот же профиль для одного номера.
    Детерминированный выбор по MD5 хешу номера.
    """
    if not phone:
        return DEVICE_PROFILES[0]
    # Нормализуем — убираем + и пробелы
    phone = phone.replace("+", "").replace(" ", "").strip()
    import hashlib
    h = int(hashlib.md5(phone.encode()).hexdigest(), 16)
    return DEVICE_PROFILES[h % len(DEVICE_PROFILES)]


def make_telethon_client(account, proxy_row=None, api_id_override=None, api_hash_override=None):
    """
    Создаёт TelegramClient для аккаунта.
    Приоритет: override → account.api_app → глобальный config
    Device fingerprint: из БД (если есть) → иначе вычисляем по хешу phone и сохраняем.
    """
    from telethon import TelegramClient

    session_file = account.session_file if hasattr(account, 'session_file') else account.get('session_file', '')
    if not session_file or not Path(session_file).exists():
        logger.warning(f"Session file не найден: {session_file}")
        return None

    session_path = session_file.replace(".session", "")
    tg_proxy = _build_proxy(proxy_row)

    # ── API credentials ──────────────────────────────────
    used_api_id = api_id_override
    used_api_hash = api_hash_override

    if not used_api_id:
        if hasattr(account, 'api_app') and account.api_app and account.api_app.is_active:
            used_api_id = account.api_app.api_id
            used_api_hash = account.api_app.api_hash
            logger.info(f"📱 API app: {account.api_app.title} (id={used_api_id})")

    if not used_api_id:
        cli_config = get_cli_config()
        used_api_id = cli_config.API_ID
        used_api_hash = cli_config.API_HASH

    # ── Прокси обязателен ────────────────────────────────
    if not tg_proxy:
        logger.warning(f"⛔ Аккаунт {session_path} — нет прокси, подключение отменено")
        return None

    # ── Device Fingerprint ───────────────────────────────
    # 1. Есть в БД → используем (никогда не меняется)
    # 2. Нет в БД → вычисляем по хешу phone, сохраняем в объект
    # 3. lang всегда "en" — нейтрально для любого гео

    phone = ""
    if hasattr(account, 'phone'):
        phone = account.phone
    elif isinstance(account, dict):
        phone = account.get('phone', '')

    # Читаем сохранённый fingerprint из БД
    saved_fp = None
    if hasattr(account, 'device_fingerprint'):
        saved_fp = account.device_fingerprint
    elif isinstance(account, dict):
        saved_fp = account.get('device_fingerprint')

    if saved_fp and "|" in saved_fp:
        # Формат: "device|system|app_version" (3 части)
        # или старый "device|system|app_version|lang" (4 части)
        parts = saved_fp.split("|")
        device = parts[0]
        system = parts[1]
        app_ver = parts[2]
    else:
        # Первый раз — вычисляем и сохраняем в объект
        fp = _get_device_fingerprint(phone)
        device = fp["device"]
        system = fp["system"]
        app_ver = fp["app_version"]

        # Сохраняем в объект → запишется в БД при следующем commit
        fp_string = f"{device}|{system}|{app_ver}"
        if hasattr(account, 'device_fingerprint'):
            account.device_fingerprint = fp_string
            logger.info(f"📱 Fingerprint сохранён: {device} / {system}")

    # Lang всегда "en" — нейтрально
    lang = "en"

    return TelegramClient(
        session_path, used_api_id, used_api_hash,
        proxy=tg_proxy,
        device_model=device,
        system_version=system,
        app_version=app_ver,
        lang_code=lang,
        system_lang_code=lang,
        timeout=30,
    )


async def get_account_with_proxy(db, account_id: int):
    """Загружает аккаунт + прокси из БД."""
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy

    acc_r = await db.execute(
        select(TelegramAccount)
        .options(joinedload(TelegramAccount.api_app))
        .where(TelegramAccount.id == account_id)
    )
    account = acc_r.scalar_one_or_none()
    if not account:
        return None, None

    proxy = None
    if hasattr(account, 'proxy_id') and account.proxy_id:
        proxy_r = await db.execute(select(Proxy).where(Proxy.id == account.proxy_id))
        proxy = proxy_r.scalar_one_or_none()

    return account, proxy