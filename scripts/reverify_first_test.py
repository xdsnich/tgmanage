"""Re-verify 15 комментов первого теста (~17:48).
Спустя 30-40 мин — смотрим сколько ещё удалилось помимо изначальных 4.
Данные хардкодом из test_check_only.log:
    +380955559146 → 8 успешных отправок (2 удалены сразу)
    +380951712059 → 3 успешные
    +380958239804 → 4 успешные (2 удалены сразу + 1 private)
"""
from __future__ import annotations
import asyncio
import os
import sys
import random
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_API_DIR = os.path.abspath(os.path.join(_HERE, "..", "api"))
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(_API_DIR, ".env"))

# ── Данные первого теста (17:48) ─────────────────────────
SENT_BY_PHONE = {
    "+380955559146": [
        ("partnerkin_job",     7258),
        ("neurorizida",       14116),
        ("vacancy_cs",         6380),
        ("ciinemaone",       189617),
        ("liliakozorez",     605160),
        ("carromanco",       100349),
        ("kuzovleva_ptichka",171727),
        ("kasbim_uz",         31192),
    ],
    "+380951712059": [
        ("mukhinss",         139934),
        ("neyrografinnya",   109045),
        ("mosaic_of_knowledge",2368),
    ],
    "+380958239804": [
        ("productmeme",        2064),
        ("aufbrat001",        37870),
        ("zmievskie",         75654),
        ("monkeysandfilms",  457697),
    ],
}


async def _load_acc(phone: str):
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as Session
    async with Session() as db:
        r = await db.execute(
            select(TelegramAccount)
            .options(joinedload(TelegramAccount.api_app))
            .where(TelegramAccount.phone == phone)
        )
        a = r.scalar_one_or_none()
        if not a: return None, None
        proxy = None
        if a.proxy_id:
            pr = await db.execute(select(Proxy).where(Proxy.id == a.proxy_id))
            proxy = pr.scalar_one_or_none()
        return a, proxy


async def _worker(phone: str, targets: list[tuple[str, int]], results: dict):
    from utils.telegram import make_telethon_client
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.tl.types import MessageEmpty
    from telethon.errors import FloodWaitError

    ps = "…" + phone[-6:]
    acc, proxy = await _load_acc(phone)
    if not acc or not proxy:
        print(f"[{ps}] ❌ acc/proxy не найден")
        return
    client = make_telethon_client(acc, proxy)
    if not client:
        print(f"[{ps}] ❌ клиент не создан")
        return
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print(f"[{ps}] ❌ не авторизован")
            return
        print(f"[{ps}] 🔗 подключён, проверяю {len(targets)} комментов")
        for idx, (uname, msg_id) in enumerate(targets, 1):
            try:
                ch = await client.get_entity(uname)
                full = await client(GetFullChannelRequest(ch))
                linked = getattr(full.full_chat, "linked_chat_id", 0) or 0
                disc = await client.get_entity(linked) if linked else ch
                msgs = await client.get_messages(disc, ids=[msg_id])
                m = msgs[0] if msgs else None
                is_del = (
                    m is None
                    or isinstance(m, MessageEmpty)
                    or (getattr(m, "message", None) is None
                        and getattr(m, "media", None) is None)
                )
                icon = "❌" if is_del else "✓"
                results[uname] = ("deleted" if is_del else "ok", msg_id)
                print(f"[{ps}]   [{idx}] {icon} @{uname} msg#{msg_id}")
            except FloodWaitError as e:
                print(f"[{ps}] ⏰ FloodWait {e.seconds}с — стоп")
                results[uname] = ("flood", str(e.seconds))
                return
            except Exception as e:
                results[uname] = ("unknown", type(e).__name__)
                print(f"[{ps}]   [{idx}] ⚠ @{uname}: {type(e).__name__}")
            await asyncio.sleep(random.uniform(1.5, 3.5))
    finally:
        try: await client.disconnect()
        except: pass


async def main():
    print("=" * 70)
    print(f"RE-VERIFY первого теста — {datetime.now()}")
    print(f"  Изначальный тест: 17:48, спустя ~30-40 мин")
    print("=" * 70)
    results: dict = {}
    tasks = [_worker(p, targets, results) for p, targets in SENT_BY_PHONE.items()]
    await asyncio.gather(*tasks, return_exceptions=True)

    print("\n" + "=" * 70)
    ok_c = sum(1 for v in results.values() if v[0] == "ok")
    del_c = sum(1 for v in results.values() if v[0] == "deleted")
    unk_c = sum(1 for v in results.values() if v[0] not in ("ok", "deleted"))
    print(f"NOW: ✓ ok={ok_c}   ❌ deleted={del_c}   ⚠ unknown={unk_c}")
    print(f"THEN (17:53): ✓ ok=10   ❌ deleted=4   ⚠ unknown=1")
    print("=" * 70)
    # Показываем изменившиеся статусы
    prev_deleted = {"partnerkin_job", "aufbrat001", "vacancy_cs", "zmievskie"}
    now_deleted = {ch for ch, (st, _) in results.items() if st == "deleted"}
    new_deletions = now_deleted - prev_deleted
    survived_deletion = prev_deleted - now_deleted
    if new_deletions:
        print(f"🆕 УДАЛЁННЫЕ ПОЗЖЕ (не были в первом верифе):")
        for ch in sorted(new_deletions):
            print(f"    ❌ @{ch}")
    else:
        print("🆕 Новых удалений за 30-40 мин НЕТ")
    if survived_deletion:
        print(f"⚠ Внезапно ожили (были удалены, теперь ok):")
        for ch in sorted(survived_deletion):
            print(f"    ? @{ch}")
    print("\nВСЕ:")
    for ch, (st, ex) in sorted(results.items()):
        icon = {"ok": "✓", "deleted": "❌"}.get(st, "?")
        marker = " 🆕" if ch in new_deletions else ""
        print(f"  {icon} @{ch:30s} {st}{marker}")


if __name__ == "__main__":
    asyncio.run(main())
