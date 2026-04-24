"""
GramGPT API — utils/telegram.py
Единая фабрика Telethon клиентов.

Ключевые принципы:
1. Device fingerprint детерминированный — один номер всегда даёт одно устройство
2. Платформа устройства СООТВЕТСТВУЕТ api_id приложения (безопасность!)
   - api_id от Android-клиента (=6) → Samsung/Xiaomi/Pixel device
   - api_id от iOS-клиента (=8) → iPhone/iPad device
   - api_id от Desktop (=2040) → PC device
   - api_id от macOS (=2834) → MacBook device
3. Прокси ОБЯЗАТЕЛЕН — без него не создаём клиент (безопасность)
4. device_fingerprint сохраняется в БД при первой авторизации — никогда не меняется
"""

from pathlib import Path
import hashlib
import logging

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# КОНФИГ CLI — для fallback на глобальные api_id/hash
# ═══════════════════════════════════════════════════════════

_cli_config = None

def get_cli_config():
    """Лениво загружает глобальный config.py из корня проекта."""
    global _cli_config
    if _cli_config is None:
        import os, sys
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        if root not in sys.path:
            sys.path.insert(0, root)
        import config as cfg
        _cli_config = cfg
    return _cli_config


# ═══════════════════════════════════════════════════════════
# ПОСТРОЕНИЕ PROXY для Telethon
# ═══════════════════════════════════════════════════════════

def _build_proxy(proxy_row):
    """Конвертирует DB-объект Proxy в формат для Telethon."""
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
    if login: proxy['username'] = login
    if password: proxy['password'] = password

    logger.info(f"✅ Прокси: {proto_str}://{host}:{port}")
    return proxy


# ═══════════════════════════════════════════════════════════
# DEVICE POOLS — пулы устройств по платформам
#
# Для каждой платформы свой пул — устройства соответствуют api_id.
# Telegram проверяет консистентность: api_id=6 (Android) + device="iPhone"
# выглядит как фейк и попадает под бан.
# ═══════════════════════════════════════════════════════════

DEVICE_POOLS = {
    "android": [
        {"device": "Samsung Galaxy S23",    "system": "Android 14", "app_version": "10.12.0"},
        {"device": "Samsung Galaxy S24",    "system": "Android 14", "app_version": "10.12.0"},
        {"device": "Samsung Galaxy A54",    "system": "Android 14", "app_version": "10.11.2"},
        {"device": "Samsung Galaxy S22",    "system": "Android 13", "app_version": "10.10.1"},
        {"device": "Xiaomi 13",             "system": "Android 13", "app_version": "10.10.1"},
        {"device": "Xiaomi 14 Pro",         "system": "Android 14", "app_version": "10.12.0"},
        {"device": "Xiaomi Redmi Note 12",  "system": "Android 13", "app_version": "10.11.2"},
        {"device": "Google Pixel 8",        "system": "Android 14", "app_version": "10.12.0"},
        {"device": "Google Pixel 8 Pro",    "system": "Android 14", "app_version": "10.12.0"},
        {"device": "Google Pixel 7 Pro",    "system": "Android 14", "app_version": "10.11.0"},
        {"device": "OnePlus 11",            "system": "Android 14", "app_version": "10.11.0"},
        {"device": "OnePlus 10 Pro",        "system": "Android 13", "app_version": "10.10.1"},
        {"device": "Honor Magic5 Pro",      "system": "Android 13", "app_version": "10.10.1"},
        {"device": "Honor 90",              "system": "Android 13", "app_version": "10.11.2"},
    ],
    "ios": [
        {"device": "iPhone 12",             "system": "iOS 17.2",   "app_version": "10.7.1"},
        {"device": "iPhone 12 Pro",         "system": "iOS 17.2",   "app_version": "10.7.1"},
        {"device": "iPhone 13",             "system": "iOS 17.2",   "app_version": "10.7.1"},
        {"device": "iPhone 13 Pro",         "system": "iOS 17.3",   "app_version": "10.8.0"},
        {"device": "iPhone 14",             "system": "iOS 17.4",   "app_version": "10.8.3"},
        {"device": "iPhone 14 Pro",         "system": "iOS 17.4",   "app_version": "10.8.3"},
        {"device": "iPhone 14 Pro Max",     "system": "iOS 17.4",   "app_version": "10.8.3"},
        {"device": "iPhone 15",             "system": "iOS 17.5",   "app_version": "10.9.1"},
        {"device": "iPhone 15 Pro",         "system": "iOS 17.5",   "app_version": "10.9.1"},
        {"device": "iPhone 15 Pro Max",     "system": "iOS 17.5",   "app_version": "10.9.1"},
        {"device": "iPad Pro",              "system": "iOS 17.4",   "app_version": "10.8.3"},
        {"device": "iPad Air",              "system": "iOS 17.3",   "app_version": "10.8.0"},
    ],
    "desktop": [
        {"device": "Desktop",               "system": "Windows 10", "app_version": "4.14.15"},
        {"device": "Desktop",               "system": "Windows 11", "app_version": "4.16.8"},
        {"device": "PC 64bit",              "system": "Windows 10", "app_version": "4.15.0"},
        {"device": "PC 64bit",              "system": "Windows 11", "app_version": "4.16.8"},
        {"device": "Desktop",               "system": "Linux 6.1",  "app_version": "4.16.8"},
    ],
    "macos": [
        {"device": "MacBook Pro",           "system": "macOS 14.2", "app_version": "10.6.2"},
        {"device": "MacBook Pro M3",        "system": "macOS 14.3", "app_version": "10.6.3"},
        {"device": "MacBook Air",           "system": "macOS 14.1", "app_version": "10.6.2"},
        {"device": "MacBook Air M2",        "system": "macOS 14.2", "app_version": "10.6.2"},
        {"device": "iMac",                  "system": "macOS 14.2", "app_version": "10.6.2"},
    ],
}


def _get_device_for_platform(phone: str, platform: str) -> dict:
    """
    Детерминированный выбор устройства из пула нужной платформы.
    Один и тот же phone + platform → всегда одно устройство.
    """
    platform = (platform or "android").lower()
    pool = DEVICE_POOLS.get(platform)
    if not pool:
        logger.warning(f"Неизвестная платформа '{platform}', использую android")
        pool = DEVICE_POOLS["android"]

    if not phone:
        return pool[0]

    phone = phone.replace("+", "").replace(" ", "").strip()
    h = int(hashlib.md5(phone.encode()).hexdigest(), 16)
    return pool[h % len(pool)]


# ═══════════════════════════════════════════════════════════
# BACKWARD COMPATIBILITY
# Старая функция _get_device_fingerprint для мест где ещё нет platform
# (например в tdata.py до момента загрузки ApiApp)
# ═══════════════════════════════════════════════════════════

def _get_device_fingerprint(phone: str, platform: str = "android") -> dict:
    """Совместимость со старым кодом. Использует platform=android по умолчанию."""
    return _get_device_for_platform(phone, platform)


# Плоский список для legacy-кода. Не используется в новом коде.
DEVICE_PROFILES = (
    DEVICE_POOLS["desktop"]
    + DEVICE_POOLS["android"]
    + DEVICE_POOLS["ios"]
    + DEVICE_POOLS["macos"]
)


# ═══════════════════════════════════════════════════════════
# ГЛАВНАЯ ФУНКЦИЯ — make_telethon_client
# ═══════════════════════════════════════════════════════════

def make_telethon_client(
    account,
    proxy_row=None,
    api_id_override=None,
    api_hash_override=None,
    platform_override=None,
):
    """
    Создаёт TelegramClient для аккаунта.

    Приоритеты:
      api_id/hash:  override → account.api_app → глобальный config
      platform:     override → account.api_app.platform → 'android'
      device:       account.device_fingerprint (если сохранён) → из пула платформы

    Возвращает None если session_file не существует или нет прокси.
    """
    from telethon import TelegramClient

    session_file = account.session_file if hasattr(account, 'session_file') else account.get('session_file', '')
    if not session_file or not Path(session_file).exists():
        logger.warning(f"⛔ Session file не найден: {session_file}")
        return None

    session_path = session_file.replace(".session", "")
    tg_proxy = _build_proxy(proxy_row)

    # ── Прокси обязателен (безопасность!) ────────────────
    if not tg_proxy:
        logger.warning(f"⛔ Аккаунт {session_path} — нет прокси, подключение отменено")
        return None

    # ── API credentials ──────────────────────────────────
    used_api_id = api_id_override
    used_api_hash = api_hash_override
    used_platform = platform_override

    if not used_api_id:
        if hasattr(account, 'api_app') and account.api_app and account.api_app.is_active:
            used_api_id = account.api_app.api_id
            used_api_hash = account.api_app.api_hash
            if not used_platform:
                used_platform = getattr(account.api_app, 'platform', 'android') or 'android'
            logger.info(f"📱 API app: {account.api_app.title} (id={used_api_id}, platform={used_platform})")

    if not used_api_id:
        cli_config = get_cli_config()
        used_api_id = cli_config.API_ID
        used_api_hash = cli_config.API_HASH
        logger.info(f"📱 Global API: id={used_api_id}")

    if not used_platform:
        used_platform = "android"  # самое безопасное по умолчанию

    # ── Phone для вычисления fingerprint ─────────────────
    phone = ""
    if hasattr(account, 'phone'):
        phone = account.phone
    elif isinstance(account, dict):
        phone = account.get('phone', '')

    # ── Device Fingerprint ───────────────────────────────
    # 1. Есть в БД → используем (никогда не меняется)
    # 2. Нет → вычисляем по платформе + phone, сохраняем в объект

    saved_fp = None
    if hasattr(account, 'device_fingerprint'):
        saved_fp = account.device_fingerprint
    elif isinstance(account, dict):
        saved_fp = account.get('device_fingerprint')

    if saved_fp and "|" in saved_fp:
        parts = saved_fp.split("|")
        device = parts[0]
        system = parts[1]
        app_ver = parts[2]
        logger.info(f"📱 Fingerprint из БД: {device} / {system}")
    else:
        fp = _get_device_for_platform(phone, used_platform)
        device = fp["device"]
        system = fp["system"]
        app_ver = fp["app_version"]

        # Сохраняем в объект — запишется в БД при следующем commit
        fp_string = f"{device}|{system}|{app_ver}"
        if hasattr(account, 'device_fingerprint'):
            account.device_fingerprint = fp_string
            logger.info(f"📱 Fingerprint сохранён: {device} / {system} (platform={used_platform})")

    return TelegramClient(
        session_path, used_api_id, used_api_hash,
        proxy=tg_proxy,
        device_model=device,
        system_version=system,
        app_version=app_ver,
        lang_code="en",
        system_lang_code="en",
        timeout=30,
    )


# ═══════════════════════════════════════════════════════════
# Загрузка аккаунта с прокси и api_app
# ═══════════════════════════════════════════════════════════

async def get_account_with_proxy(db, account_id: int):
    """Загружает аккаунт + прокси + api_app из БД."""
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