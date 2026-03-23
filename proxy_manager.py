"""
GramGPT — proxy_manager.py
Всё что связано с прокси
Отвечает за: загрузку, проверку валидности, назначение на аккаунты
"""

import asyncio
import random
import socket
from datetime import datetime
from pathlib import Path

from config import MAX_WORKERS, BASE_DIR
from db import load_proxies, save_proxies, parse_proxy_line, load_accounts, save_accounts
import ui


# ============================================================
# ПРОВЕРКА ОДНОГО ПРОКСИ
# ============================================================

async def check_proxy(proxy: dict) -> dict:
    """Проверяет доступность прокси через TCP-соединение"""
    host = proxy["host"]
    port = proxy["port"]
    login = proxy.get("login", "")
    password = proxy.get("password", "")
    protocol = proxy.get("protocol", "socks5")

    try:
        if protocol == "socks5":
            try:
                import socks
            except ImportError:
                # Fallback: простая TCP-проверка
                return await _tcp_check(proxy)

            s = socks.socksocket()
            s.set_proxy(socks.SOCKS5, host, port,
                        username=login or None,
                        password=password or None)
            s.settimeout(8)
            s.connect(("api.telegram.org", 443))
            s.close()

        elif protocol == "http":
            # HTTP-прокси: простая TCP-проверка
            return await _tcp_check(proxy)

        proxy["is_valid"] = True
        proxy["last_checked"] = datetime.now().isoformat()
        proxy["error"] = None
        return proxy

    except Exception as e:
        proxy["is_valid"] = False
        proxy["last_checked"] = datetime.now().isoformat()
        proxy["error"] = str(e)[:80]
        return proxy


async def _tcp_check(proxy: dict) -> dict:
    """Базовая TCP-проверка доступности хоста:порта"""
    try:
        conn = asyncio.open_connection(proxy["host"], proxy["port"])
        reader, writer = await asyncio.wait_for(conn, timeout=8)
        writer.close()
        await writer.wait_closed()
        proxy["is_valid"] = True
    except Exception as e:
        proxy["is_valid"] = False
        proxy["error"] = str(e)[:80]
    proxy["last_checked"] = datetime.now().isoformat()
    return proxy


# ============================================================
# МАССОВАЯ ПРОВЕРКА
# ============================================================

async def check_all(proxies: list[dict]) -> list[dict]:
    """Проверяет все прокси в несколько потоков"""
    semaphore = asyncio.Semaphore(MAX_WORKERS)

    async def worker(proxy, index):
        async with semaphore:
            print(f"  [{index+1}/{len(proxies)}] Проверяю {proxy['id']}...", end=" ", flush=True)
            result = await check_proxy(proxy)
            if result["is_valid"]:
                print(f"\033[32m✅ OK\033[0m")
            else:
                print(f"\033[31m❌ Недоступен\033[0m")
            return result

    tasks = [worker(p, i) for i, p in enumerate(proxies)]
    results = await asyncio.gather(*tasks)
    return list(results)


# ============================================================
# НАЗНАЧЕНИЕ ПРОКСИ НА АККАУНТЫ
# ============================================================

def assign_proxies(accounts: list[dict], proxies: list[dict], mode: str = "sequential") -> tuple[list, list]:
    """
    Назначает валидные прокси на аккаунты без прокси.
    mode: 'sequential' — по порядку, 'random' — случайно
    Возвращает: (обновлённые аккаунты, обновлённые прокси)
    """
    valid = [p for p in proxies if p.get("is_valid") is True]
    if not valid:
        ui.warn("Нет валидных прокси. Сначала проверь прокси (пункт 4).")
        return accounts, proxies

    unassigned = [a for a in accounts if not a.get("proxy")]
    if not unassigned:
        ui.info("Все аккаунты уже имеют прокси.")
        return accounts, proxies

    if mode == "random":
        random.shuffle(valid)

    assigned_count = 0
    for i, account in enumerate(accounts):
        if account.get("proxy"):
            continue
        proxy = valid[assigned_count % len(valid)]
        account["proxy"] = proxy["id"]
        # Добавляем аккаунт в список назначений прокси
        if account["phone"] not in proxy.get("assigned_to", []):
            proxy.setdefault("assigned_to", []).append(account["phone"])
        assigned_count += 1

    ui.ok(f"Назначено прокси на {assigned_count} аккаунтов")
    return accounts, proxies


# ============================================================
# ЗАГРУЗКА ИЗ ФАЙЛА
# ============================================================

def load_from_file(filepath: str) -> list[dict]:
    """Парсит файл со списком прокси (по одному на строку)"""
    path = Path(filepath)
    if not path.exists():
        ui.err(f"Файл не найден: {filepath}")
        return []

    proxies = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            proxy = parse_proxy_line(line)
            if proxy:
                proxies.append(proxy)

    ui.ok(f"Загружено {len(proxies)} прокси из {path.name}")
    return proxies
