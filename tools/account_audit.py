# -*- coding: utf-8 -*-
"""Быстрый аудит аккаунтов: ограничение, аватар, прикреплённый канал, ссылка.

Параллельно по прокси, внутри прокси — по очереди с короткой паузой.
Только чтение + один /start боту @SpamBot (так проверяется ограничение).

Запуск из папки api:  python ..\\tools\\account_audit.py
"""
import asyncio, json, os, re, sys
from collections import Counter
from datetime import datetime
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.abspath("."))

НЕ_ТРОГАТЬ = {48, 95, 217}
ВОРОНКА_ID = 4437551288            # «Amelie» — целевой канал
ВНУТРИ_ПАУЗА = 6                   # сек между аккаунтами одного прокси
РЕЗУЛЬТАТ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_audit_result.json")


async def аудит(acc, proxy):
    from utils.telegram import make_telethon_client
    from utils.ip_throttle import acquire_ip_lock, release_ip_lock
    from utils.account_lock import acquire_account_lock, release_account_lock
    from telethon.tl.functions.users import GetFullUserRequest

    # Замок адреса не берём: по_прокси идёт строго по очереди, двух
    # одновременных подключений с одного IP не будет, а owner-замок на
    # 600с иначе блокирует соседей по прокси (для разового аудита лишнее).
    out = {"acc": acc.id, "phone": acc.phone, "proxy": proxy.host}
    if not acquire_account_lock(acc.id, ttl=120):
        out["итог"] = "занят"; return out
    client = make_telethon_client(acc, proxy)
    if client is None:
        release_account_lock(acc.id)
        out["итог"] = "нет сессии"; return out
    try:
        await asyncio.wait_for(client.connect(), 40)
        if not await asyncio.wait_for(client.is_user_authorized(), 25):
            out["итог"] = "сессия мертва"; return out
        me = await asyncio.wait_for(client.get_me(), 25)
        out["username"] = me.username or ""
        out["имя"] = ((me.first_name or "") + " " + (me.last_name or "")).strip()
        out["аватар"] = bool(me.photo)

        # Профиль: описание + прикреплённый личный канал
        try:
            full = await asyncio.wait_for(client(GetFullUserRequest("me")), 25)
            fu = full.full_user
            out["bio"] = (getattr(fu, "about", "") or "")[:120]
            pcid = getattr(fu, "personal_channel_id", None)
        except Exception:
            out["bio"], pcid = "", None

        if pcid:
            try:
                ch = await asyncio.wait_for(client.get_entity(pcid), 25)
                out["канал"] = "@" + (ch.username or "") if ch.username else "(приватный, без адреса)"
                out["канал_назв"] = getattr(ch, "title", "")
                out["это_воронка"] = (pcid == ВОРОНКА_ID)
                out["ссылка_ок"] = bool(getattr(ch, "username", None))  # публичный адрес = ссылка рабочая
            except Exception as e:
                out["канал"] = "не открылся (%s)" % type(e).__name__
                out["ссылка_ок"] = False
        else:
            out["канал"] = "нет"
            out["ссылка_ок"] = False

        # Ограничение — спрашиваем @SpamBot
        try:
            async with client.conversation("@SpamBot", timeout=25) as conv:
                await conv.send_message("/start")
                await asyncio.sleep(1.5)
                текст = (await conv.get_response()).text or ""
            низ = текст.lower()
            if "good news" in низ or "no limits" in низ or "free as a bird" in низ:
                out["ограничен"] = False
            else:
                out["ограничен"] = True
                m = re.search(r"limited until ([0-9]{1,2} \w{3} [0-9]{4})", текст)
                out["до"] = m.group(1) if m else "бессрочно/не указано"
        except Exception as e:
            out["ограничен"] = "не узнал (%s)" % type(e).__name__
        out["итог"] = "ok"
    except Exception as e:
        out["итог"] = "сбой %s" % type(e).__name__
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), 10)
        except Exception:
            pass
        release_account_lock(acc.id)
    return out


async def по_прокси(accs, proxy, результаты):
    for i, acc in enumerate(accs):
        r = await аудит(acc, proxy)
        результаты.append(r)
        print("acc#%-4s %-14s %-16s %s" % (r["acc"], r["phone"], proxy.host, r.get("итог")), flush=True)
        if i < len(accs) - 1:
            await asyncio.sleep(ВНУТРИ_ПАУЗА)


async def main():
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from collections import defaultdict
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as S

    async with S() as db:
        # Все аккаунты с рабочим прокси, любой статус (без прокси подключить
        # нельзя). Защищённых (Жека, Мария) не трогаем.
        proxies = {p.id: p for p in (await db.execute(select(Proxy).where(
            Proxy.is_valid.is_(True)))).scalars().all()}
        accs = (await db.execute(select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
                .where(TelegramAccount.proxy_id.in_(list(proxies))))).scalars().all()
    accs = [a for a in accs if a.id not in НЕ_ТРОГАТЬ]
    груп = defaultdict(list)
    for a in accs:
        груп[a.proxy_id].append(a)
    print("аудит %d аккаунтов на %d прокси, параллельно" % (len(accs), len(груп)))
    t0 = datetime.now()
    результаты = []
    await asyncio.gather(*(по_прокси(lst, proxies[pid], результаты) for pid, lst in груп.items()))
    json.dump(результаты, open(РЕЗУЛЬТАТ, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    сек = (datetime.now() - t0).total_seconds()
    print("\n=== ИТОГ за %.0f сек ===" % сек)
    c = Counter(str(r.get("итог")) for r in результаты)
    print("вход:", dict(c))
    живы = [r for r in результаты if r.get("итог") == "ok"]
    огр = Counter(str(r.get("ограничен")) for r in живы)
    print("ограничение:", dict(огр))
    print("без аватара:", sum(1 for r in живы if r.get("аватар") is False))
    print("без прикреплённого канала:", sum(1 for r in живы if r.get("канал") == "нет"))
    print("ссылка не рабочая:", sum(1 for r in живы if not r.get("ссылка_ок")))
    своб = [r["acc"] for r in живы if r.get("ограничен") is False]
    огрн = [r["acc"] for r in живы if r.get("ограничен") is True]
    print("\nСВОБОДНЫХ: %d — %s" % (len(своб), sorted(своб)))
    print("ОГРАНИЧЕННЫХ: %d — %s" % (len(огрн), sorted(огрн)))
    print("\nфайл: %s" % РЕЗУЛЬТАТ)


asyncio.run(main())
