# -*- coding: utf-8 -*-
"""Весь поток в воронку за сутки: и ждущие решения, и уже принятые.

Прежний подсчёт спрашивал у Telegram только заявки со статусом
«ожидает» (requested=True) и поэтому показывал заниженное число: всех,
кого за день успели принять, он просто не видел.

Здесь два запроса вместо одного. Второй, с requested=False, отдаёт тех,
кто уже вступил по пригласительной ссылке, вместе с датой вступления —
это и есть недостающая половина.

Только чтение: ничего не принимаем и не отклоняем.
"""
import asyncio
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath("."))

ADMIN_ACC = 48                  # @jeka_opasniy
FUNNEL_ID = 4437551288          # «Amelie💓💔»
KYIV = timezone(timedelta(hours=3))


async def собрать(client, dest, ждущие: bool):
    """Все записи одного вида. Листаем до конца, без потолка."""
    from telethon.tl.functions.messages import GetChatInviteImportersRequest
    from telethon.tl.types import InputUser, InputUserEmpty

    out, offset_date, offset_user = [], None, InputUserEmpty()
    видели = set()
    while True:
        res = await client(GetChatInviteImportersRequest(
            peer=dest, requested=ждущие, q="",
            offset_date=offset_date, offset_user=offset_user, limit=100))
        if not res.importers:
            break
        свежие = [i for i in res.importers if i.user_id not in видели]
        видели.update(i.user_id for i in res.importers)
        out.extend(свежие)
        if len(res.importers) < 100 or not свежие:
            break

        # Указатель на следующую страницу строим из пользователей, которых
        # Telegram вернул в этом же ответе. Прежний код спрашивал его через
        # get_input_entity, а тот знает только уже виденных — на первом же
        # незнакомце падал, цикл обрывался, и список молча обрезался
        # первой сотней. Список идёт от новых к старым, поэтому наружу
        # выходили только самые свежие заявки.
        last = res.importers[-1]
        offset_date = last.date
        u = next((x for x in res.users if x.id == last.user_id), None)
        if u is None or getattr(u, "access_hash", None) is None:
            break
        offset_user = InputUser(user_id=u.id, access_hash=u.access_hash)
        print("   … собрано %d" % len(out), flush=True)
        await asyncio.sleep(0.4)
    return out


async def main():
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from models.proxy import Proxy
    from utils.db_pool import async_session as Session
    from utils.telegram import make_telethon_client
    from utils.ip_throttle import acquire_ip_lock
    from utils.account_lock import acquire_account_lock, release_account_lock

    async with Session() as db:
        acc = (await db.execute(select(TelegramAccount)
               .options(joinedload(TelegramAccount.api_app))
               .where(TelegramAccount.id == ADMIN_ACC))).scalar_one_or_none()
        proxy = (await db.execute(select(Proxy)
                 .where(Proxy.id == acc.proxy_id))).scalar_one_or_none()

    for попытка in range(20):
        if acquire_ip_lock(proxy, owner=ADMIN_ACC) and \
                acquire_account_lock(ADMIN_ACC, ttl=900):
            break
        print("окно занято, жду…", flush=True)
        await asyncio.sleep(30)
    else:
        print("не дождался свободного окна")
        return

    client = make_telethon_client(acc, proxy)
    try:
        await client.connect()
        me = await client.get_me()
        print("подключён: %s @%s" % (me.first_name, me.username), flush=True)

        dest = None
        async for d in client.iter_dialogs(limit=200):
            if getattr(d.entity, "id", None) == FUNNEL_ID:
                dest = d.entity
                break
        if dest is None:
            print("канал воронки не найден")
            return
        print("канал:    %s" % dest.title, flush=True)
        print(flush=True)

        ждут = await собрать(client, dest, True)
        print("собрал ждущих: %d" % len(ждут), flush=True)
        принятые = await собрать(client, dest, False)
        print("собрал принятых: %d" % len(принятые), flush=True)

        сегодня = datetime.now(KYIV).date()
        по_дням = defaultdict(lambda: [0, 0])
        по_часам = Counter()
        for кто, поле in ((ждут, 0), (принятые, 1)):
            for i in кто:
                d = i.date.astimezone(KYIV)
                по_дням[d.date()][поле] += 1
                if d.date() == сегодня:
                    по_часам[d.hour] += 1

        ж, п = по_дням[сегодня]
        print()
        print("=" * 54)
        print("  ЗА СЕГОДНЯ %s" % сегодня.strftime("%d.%m"))
        print("    ждут решения      %4d" % ж)
        print("    уже приняты       %4d" % п)
        print("    ВСЕГО ЗАЯВОК      %4d" % (ж + п))
        print("=" * 54)

        print()
        print("-- сегодня по часам --")
        for h in range(24):
            n = по_часам.get(h, 0)
            if n:
                print("  %02d:00  %s %d" % (h, "#" * min(60, n), n))

        print()
        print("-- по дням --")
        for d in sorted(по_дням, reverse=True)[:8]:
            ж2, п2 = по_дням[d]
            метка = "   <- сегодня" if d == сегодня else ""
            print("  %s  всего %4d  (ждут %3d, приняты %4d)%s"
                  % (d, ж2 + п2, ж2, п2, метка))
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        release_account_lock(ADMIN_ACC)


asyncio.run(main())
