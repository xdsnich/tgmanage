# -*- coding: utf-8 -*-
"""Проверка аккаунтов после смены/продления прокси.

Каждый аккаунт: подключиться через СВОЙ прокси, спросить «авторизован ли»
и «кто я», отключиться. Больше ничего — ни сообщений, ни подписок.

Правила те же, что у программы:
  • замок адреса с владельцем: разные аккаунты одного адреса — не чаще
    раза в 10 минут, так что на прокси с тремя аккаунтами уходит ~25 мин;
  • замок аккаунта: если аккаунт сейчас работает в программе — пропускаем.
Все прокси идут параллельно. Жеку и Марию не подключаем.

Результат — в account_check_result.json и в лог.

Запуск из папки api:
    python ..\\tools\\account_check.py
"""
import asyncio, json, os, sys
from collections import Counter, defaultdict
from datetime import datetime
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.abspath("."))

НЕ_ТРОГАТЬ = {48, 95, 217}
T = os.path.dirname(os.path.abspath(__file__))
ЛОГ = os.path.join(T, "account_check.log")
ИТОГ = os.path.join(T, "account_check_result.json")
ЖДАТЬ_АДРЕС_СЕК = 25 * 60


def журнал(s):
    строка = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), s)
    print(строка, flush=True)
    with open(ЛОГ, "a", encoding="utf-8") as f:
        f.write(строка + "\n")


async def проверить(acc, proxy):
    from utils.telegram import make_telethon_client
    from utils.ip_throttle import acquire_ip_lock, release_ip_lock
    from utils.account_lock import acquire_account_lock, release_account_lock

    начало = datetime.now()
    while not acquire_ip_lock(proxy, owner=acc.id):
        if (datetime.now() - начало).total_seconds() > ЖДАТЬ_АДРЕС_СЕК:
            return {"итог": "адрес занят", "detail": "не дождался адреса"}
        await asyncio.sleep(30)
    if not acquire_account_lock(acc.id, ttl=180):
        release_ip_lock(proxy)
        return {"итог": "занят программой"}
    client = make_telethon_client(acc, proxy)
    if client is None:
        release_account_lock(acc.id)
        release_ip_lock(proxy)
        return {"итог": "нет сессии", "detail": acc.session_file}
    try:
        await asyncio.wait_for(client.connect(), 45)
        if not await asyncio.wait_for(client.is_user_authorized(), 30):
            return {"итог": "сессия не авторизована"}
        me = await asyncio.wait_for(client.get_me(), 30)
        if me is None:
            return {"итог": "аккаунт не отвечает"}
        return {"итог": "ok", "tg_id": me.id, "username": me.username or ""}
    except Exception as e:
        имя = type(e).__name__
        if имя in ("AuthKeyUnregisteredError", "SessionRevokedError", "AuthKeyDuplicatedError"):
            итог = "сессия мертва"
        elif имя in ("UserDeactivatedBanError", "UserDeactivatedError", "PhoneNumberBannedError"):
            итог = "аккаунт удалён/забанен"
        elif имя in ("TimeoutError", "ConnectionError", "OSError", "ProxyConnectionError",
                     "GeneralProxyError", "ProxyError"):
            итог = "нет связи через прокси"
        else:
            итог = "ошибка"
        return {"итог": итог, "detail": "%s: %s" % (имя, str(e)[:120])}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        release_account_lock(acc.id)
        release_ip_lock(proxy)


async def группа(proxy, accs, результаты):
    for acc in accs:
        r = await проверить(acc, proxy)
        r.update({"acc": acc.id, "phone": acc.phone, "status": str(getattr(acc.status, "value", acc.status)),
                  "proxy": "%s:%s" % (proxy.host, proxy.port)})
        результаты.append(r)
        журнал("acc#%-4s %-14s %-16s → %s %s" % (acc.id, acc.phone, proxy.host, r["итог"],
                                                 r.get("username") or r.get("detail") or ""))


async def main():
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as S
    from datetime import datetime as dt

    async with S() as db:
        прокси = {p.id: p for p in (await db.execute(select(Proxy).where(
            Proxy.is_valid.is_(True), Proxy.expires_at > dt.utcnow()))).scalars().all()}
        accs = (await db.execute(select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
                                 .where(TelegramAccount.proxy_id.in_(list(прокси))))).scalars().all()
    accs = [a for a in accs if a.id not in НЕ_ТРОГАТЬ]
    по_прокси = defaultdict(list)
    for a in accs:
        по_прокси[a.proxy_id].append(a)
    журнал("══ проверка %d аккаунтов на %d прокси ══" % (len(accs), len(по_прокси)))

    результаты = []
    await asyncio.gather(*(группа(прокси[pid], список, результаты) for pid, список in по_прокси.items()))

    json.dump(результаты, open(ИТОГ, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    счёт = Counter(r["итог"] for r in результаты)
    журнал("ИТОГ: %s" % ", ".join("%s %d" % (k, v) for k, v in счёт.most_common()))


asyncio.run(main())
