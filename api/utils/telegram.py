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


def make_telethon_client(account, proxy_row=None):
    """Создаёт TelegramClient для аккаунта с прокси."""
    from telethon import TelegramClient
    cli_config = get_cli_config()

    session_file = account.session_file if hasattr(account, 'session_file') else account.get('session_file', '')
    if not session_file or not Path(session_file).exists():
        logger.warning(f"Session file не найден: {session_file}")
        return None

    session_path = session_file.replace(".session", "")
    tg_proxy = _build_proxy(proxy_row)

    return TelegramClient(
        session_path, cli_config.API_ID, cli_config.API_HASH,
        proxy=tg_proxy,
        device_model="Desktop", system_version="Windows 10", app_version="4.14.15",
        lang_code="ru", system_lang_code="ru",
        timeout=30,
    )


async def get_account_with_proxy(db, account_id: int):
    from sqlalchemy import select
    from models.account import TelegramAccount
    from models.proxy import Proxy

    acc_r = await db.execute(select(TelegramAccount).where(TelegramAccount.id == account_id))
    account = acc_r.scalar_one_or_none()
    if not account:
        return None, None

    proxy = None
    if hasattr(account, 'proxy_id') and account.proxy_id:
        proxy_r = await db.execute(select(Proxy).where(Proxy.id == account.proxy_id))
        proxy = proxy_r.scalar_one_or_none()

    return account, proxy