"""
GramGPT — test_architecture.py
End-to-end test архитектуры (db_pool + redis_pool + asyncio + Telethon).

Что проверяет:
  1. Открытие БД-сессии через shared db_pool
  2. Redis через shared redis_pool (account_lock + connection_limiter)
  3. Подключение к Telegram через прокси
  4. is_user_authorized + get_me
  5. ПАРАЛЛЕЛЬНУЮ работу на 2 аккаунтах одновременно (asyncio.gather)
     — это ловит проблемы кросс-loop сессий, share connection между тредами и т.п.

Запуск:
  cd api
  python test_architecture.py                # первые 2 активных аккаунта
  python test_architecture.py 5 7            # конкретные ID
  python test_architecture.py 5 7 --parallel # параллельно (по умолчанию)
  python test_architecture.py 5 7 --serial   # один за другим (для сравнения)

Зачем 2 аккаунта параллельно: один аккаунт может скрыть проблемы
(один прокси работает, второй нет; один заморожен; gevent loop конфликт
проявляется только при 2+ одновременных тасках).
"""

import sys
import os
import asyncio
import argparse
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Тихий лог чтобы выхлоп был читаемым
logging.basicConfig(level=logging.WARNING, format='[%(levelname)s] %(name)s: %(message)s')

# Цвета
class C:
    R = "\033[31m"; G = "\033[32m"; Y = "\033[33m"; B = "\033[34m"
    C = "\033[36m"; M = "\033[35m"; BOLD = "\033[1m"; DIM = "\033[2m"; OFF = "\033[0m"


def step_ok(label, detail=""):
    print(f"  {C.G}✓{C.OFF} {label}{C.DIM} — {detail}{C.OFF}" if detail else f"  {C.G}✓{C.OFF} {label}")

def step_fail(label, err="", err_type=""):
    type_str = f"{C.Y}[{err_type}]{C.OFF} " if err_type else ""
    print(f"  {C.R}✕{C.OFF} {label} → {type_str}{C.R}{err}{C.OFF}")

def header(text):
    print(f"\n{C.M}{C.BOLD}═══ {text} ═══{C.OFF}")


# ─────────────────────────────────────────────────────────
# Test одного аккаунта (используется в asyncio.gather для параллелизма)
# ─────────────────────────────────────────────────────────
async def test_account(account_id: int, prefix: str = ""):
    """Полный прогон: БД → Redis lock → Telethon connect → get_me."""
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from utils.db_pool import async_session as Session
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.connection_limiter import check_connection_limit, get_connection_count
    from utils.telegram import make_telethon_client
    from models.account import TelegramAccount
    from models.proxy import Proxy

    result = {"account_id": account_id, "steps": [], "ok": False, "error": None}

    def log(msg, level="info"):
        if prefix:
            print(f"{prefix} {msg}")

    started = time.time()

    # 1. БД-сессия
    try:
        async with Session() as db:
            acc = (await db.execute(
                select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
                .where(TelegramAccount.id == account_id)
            )).scalar_one_or_none()
            if not acc:
                result["error"] = f"аккаунт #{account_id} не найден"
                return result
            proxy = None
            if acc.proxy_id:
                proxy = (await db.execute(select(Proxy).where(Proxy.id == acc.proxy_id))).scalar_one_or_none()
            phone = acc.phone
            status = acc.status
        result["steps"].append({"step": "db", "ok": True, "detail": f"{phone} status={status}"})
    except Exception as e:
        result["steps"].append({"step": "db", "ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"})
        result["error"] = "DB failure"
        return result

    # 2. Redis (connection_limiter)
    try:
        count = get_connection_count(account_id)
        limit_ok = check_connection_limit(account_id)
        result["steps"].append({"step": "redis_limiter", "ok": True, "detail": f"connects={count}/6, limit_ok={limit_ok}"})
    except Exception as e:
        result["steps"].append({"step": "redis_limiter", "ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"})

    # 3. Redis lock (acquire)
    if not acquire_account_lock(account_id, ttl=60):
        result["steps"].append({"step": "redis_lock", "ok": False, "error": "lock busy (другой воркер держит)"})
        result["error"] = "lock busy"
        return result
    result["steps"].append({"step": "redis_lock", "ok": True, "detail": "acquired"})

    try:
        # 4. Telethon client
        client = make_telethon_client(acc, proxy)
        if not client:
            result["steps"].append({"step": "telethon_client", "ok": False, "error": "нет файла сессии"})
            result["error"] = "no session"
            return result
        result["steps"].append({"step": "telethon_client", "ok": True, "detail": f"прокси: {proxy.host + ':' + str(proxy.port) if proxy else 'нет'}"})

        # 5. Connect
        try:
            await client.connect()
            if not await client.is_user_authorized():
                result["steps"].append({"step": "telethon_connect", "ok": False, "error": "not authorized"})
                result["error"] = "not authorized"
                return result
            result["steps"].append({"step": "telethon_connect", "ok": True, "detail": "authorized"})
        except Exception as e:
            result["steps"].append({"step": "telethon_connect", "ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"})
            result["error"] = "connect failed"
            return result

        # 6. get_me
        try:
            me = await client.get_me()
            result["steps"].append({"step": "get_me", "ok": True, "detail": f"@{me.username or '?'} ({me.first_name or '?'})"})
        except Exception as e:
            result["steps"].append({"step": "get_me", "ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"})
            result["error"] = "get_me failed"
            return result

        # 7. Бонус — попробуем БД ВНУТРИ asyncio loop'а параллельной задачи
        # Это самая важная проверка для shared engine!
        try:
            async with Session() as db:
                cnt = (await db.execute(select(TelegramAccount).where(TelegramAccount.id == account_id))).scalar_one_or_none()
                result["steps"].append({"step": "db_inside_tg_loop", "ok": True, "detail": "вторая БД-сессия в том же loop работает"})
        except Exception as e:
            result["steps"].append({"step": "db_inside_tg_loop", "ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"})
            result["error"] = "shared engine cross-loop issue"
            return result

        result["ok"] = True
        try:
            await client.disconnect()
        except Exception:
            pass

    finally:
        release_account_lock(account_id)

    result["elapsed"] = round(time.time() - started, 2)
    return result


# ─────────────────────────────────────────────────────────
# Утилиты вывода
# ─────────────────────────────────────────────────────────
def print_result(r: dict, account_label: str):
    print(f"\n{C.C}{C.BOLD}── {account_label} (id={r['account_id']}) ──{C.OFF}")
    for s in r["steps"]:
        if s["ok"]:
            step_ok(s["step"], s.get("detail", ""))
        else:
            step_fail(s["step"], s.get("error", ""))
    if r["ok"]:
        print(f"  {C.G}{C.BOLD}РЕЗУЛЬТАТ: ОК{C.OFF} {C.DIM}({r.get('elapsed', '?')}с){C.OFF}")
    else:
        print(f"  {C.R}{C.BOLD}РЕЗУЛЬТАТ: ПРОВАЛ{C.OFF} — {C.R}{r.get('error', '?')}{C.OFF}")


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────
async def get_default_account_ids(n: int = 2):
    """Берём первые N активных аккаунтов из БД."""
    from sqlalchemy import select
    from utils.db_pool import async_session as Session
    from models.account import TelegramAccount

    async with Session() as db:
        result = await db.execute(
            select(TelegramAccount.id)
            .where(TelegramAccount.status.in_(("active", "unknown")))
            .limit(n)
        )
        return [r[0] for r in result.all()]


async def main(args):
    header("ТЕСТ АРХИТЕКТУРЫ — db_pool + redis_pool + asyncio")

    if args.account_ids:
        account_ids = args.account_ids
    else:
        try:
            account_ids = await get_default_account_ids(2)
        except Exception as e:
            print(f"{C.R}Не удалось взять аккаунты из БД:{C.OFF} {e}")
            return 1
        if len(account_ids) < 2:
            print(f"{C.Y}В БД меньше 2 активных аккаунтов. Найдено: {account_ids}{C.OFF}")
            if not account_ids:
                return 1

    print(f"{C.C}Тестируем аккаунты:{C.OFF} {account_ids}")
    print(f"{C.C}Режим:{C.OFF} {'ПАРАЛЛЕЛЬНО (gather)' if not args.serial else 'ПОСЛЕДОВАТЕЛЬНО'}")

    started = time.time()

    if args.serial:
        results = []
        for aid in account_ids:
            r = await test_account(aid)
            results.append(r)
    else:
        results = await asyncio.gather(
            *(test_account(aid) for aid in account_ids),
            return_exceptions=True,
        )

    # Вывод
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            print(f"\n{C.R}{C.BOLD}── Аккаунт #{account_ids[i]} — UNHANDLED EXCEPTION ──{C.OFF}")
            print(f"  {type(r).__name__}: {str(r)[:300]}")
        else:
            print_result(r, f"Аккаунт #{i+1}")

    # Итог
    header("ИТОГ")
    total = len(results)
    ok = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
    elapsed = time.time() - started
    color = C.G if ok == total else (C.Y if ok > 0 else C.R)
    print(f"  {color}{C.BOLD}{ok}/{total} OK{C.OFF}{C.DIM} за {elapsed:.2f}с{C.OFF}")

    # Параллельность реальная?
    if not args.serial and ok >= 2:
        max_indiv = max((r["elapsed"] for r in results if isinstance(r, dict) and r.get("elapsed")), default=0)
        if max_indiv > 0 and elapsed < max_indiv * 1.5:
            print(f"  {C.G}✓ Параллелизм работает{C.OFF}{C.DIM} (max account: {max_indiv:.2f}с, total: {elapsed:.2f}с){C.OFF}")
        else:
            print(f"  {C.Y}⚠ Похоже на сериализацию{C.OFF}{C.DIM} (max account: {max_indiv:.2f}с, total: {elapsed:.2f}с){C.OFF}")

    return 0 if ok == total else 1


def cli():
    parser = argparse.ArgumentParser(description="End-to-end test архитектуры на 2+ аккаунтах")
    parser.add_argument("account_ids", type=int, nargs="*", help="ID аккаунтов (по умолчанию — первые 2 активных)")
    parser.add_argument("--serial", action="store_true", help="последовательный режим (не параллельный)")
    args = parser.parse_args()

    rc = asyncio.run(main(args))
    sys.exit(rc)


if __name__ == "__main__":
    cli()
