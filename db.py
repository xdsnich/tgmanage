"""
GramGPT — db.py
Работа с локальной базой данных (JSON-файлы)
Отвечает за: сохранение, загрузку, поиск аккаунтов и прокси
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import ACCOUNTS_FILE, PROXIES_FILE


# ============================================================
# АККАУНТЫ
# ============================================================

def load_accounts() -> list[dict]:
    if ACCOUNTS_FILE.exists():
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_accounts(accounts: list[dict]):
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2, default=str)


def find_account(accounts: list[dict], phone: str) -> Optional[dict]:
    return next((a for a in accounts if a.get("phone") == phone), None)


def find_account_index(accounts: list[dict], phone: str) -> int:
    return next((i for i, a in enumerate(accounts) if a.get("phone") == phone), -1)


def upsert_account(accounts: list[dict], account: dict) -> list[dict]:
    """Обновляет аккаунт если есть, добавляет если нет"""
    idx = find_account_index(accounts, account["phone"])
    if idx >= 0:
        accounts[idx] = account
    else:
        accounts.append(account)
    save_accounts(accounts)
    return accounts


def delete_account(accounts: list[dict], phone: str) -> list[dict]:
    accounts = [a for a in accounts if a.get("phone") != phone]
    save_accounts(accounts)
    return accounts


def make_account_template(phone: str) -> dict:
    """Шаблон нового аккаунта"""
    return {
        "phone": phone,
        "id": None,
        "first_name": "",
        "last_name": "",
        "username": "",
        "bio": "",
        "has_photo": False,
        "active_sessions": 0,
        "status": "unknown",   # active | frozen | spamblock | error | unknown
        "trust_score": 0,
        "tags": [],
        "notes": "",
        "role": "default",
        "proxy": None,
        "session_file": "",
        "added_at": datetime.now().isoformat(),
        "last_checked": None,
        "error": None,
    }


# ============================================================
# ПРОКСИ
# ============================================================

def load_proxies() -> list[dict]:
    if PROXIES_FILE.exists():
        with open(PROXIES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_proxies(proxies: list[dict]):
    with open(PROXIES_FILE, "w", encoding="utf-8") as f:
        json.dump(proxies, f, ensure_ascii=False, indent=2, default=str)


def make_proxy_template(host: str, port: int, login: str = "", password: str = "", protocol: str = "socks5") -> dict:
    return {
        "id": f"{host}:{port}",
        "host": host,
        "port": port,
        "login": login,
        "password": password,
        "protocol": protocol,   # socks5 | http
        "is_valid": None,       # True | False | None (не проверен)
        "last_checked": None,
        "assigned_to": [],      # список phone номеров
    }


def parse_proxy_line(line: str) -> Optional[dict]:
    """
    Парсит строку прокси в разных форматах:
      socks5://login:pass@host:port
      host:port:login:pass
      host:port
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # Формат: protocol://login:pass@host:port
    if "://" in line:
        try:
            protocol, rest = line.split("://", 1)
            if "@" in rest:
                creds, addr = rest.rsplit("@", 1)
                login, password = creds.split(":", 1)
            else:
                addr = rest
                login, password = "", ""
            host, port = addr.rsplit(":", 1)
            return make_proxy_template(host, int(port), login, password, protocol)
        except Exception:
            return None

    # Формат: host:port:login:pass или host:port
    parts = line.split(":")
    if len(parts) == 4:
        host, port, login, password = parts
        return make_proxy_template(host, int(port), login, password)
    elif len(parts) == 2:
        host, port = parts
        return make_proxy_template(host, int(port))

    return None
