# -*- coding: utf-8 -*-
"""Подписка аккаунтов на их каналы по назначениям кампании.

Каждый аккаунт вступает в свои каналы (assignments status='pending'),
помечая 'joined' по факту. Темп безопасный: суточный лимит на аккаунт,
паузы между вступлениями, защита от флуда (аккаунт выходит, адрес дальше
медленнее). Прокси параллельно, внутри прокси аккаунты по очереди.
Каждые сутки добирает остаток, пока все не 'joined'.

ENV:
  SUB_CAMPAIGN      кампания (148)
  SUB_PER_DAY       вступлений на аккаунт в сутки (40)
  SUB_DELAY_MIN/MAX пауза между вступлениями, сек (40/90)

Запуск из папки api:  python ..\\tools\\subscribe_run.py
"""
import asyncio, os, random, sys
from collections import defaultdict
from datetime import date, datetime, timedelta
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.abspath("."))

CAMP = int(os.getenv("SUB_CAMPAIGN", "148"))
PER_DAY = int(os.getenv("SUB_PER_DAY", "40"))
# Пачка за один заход на прокси: маленькая, чтобы realtime получал окна
# между пачками, а не ждал, пока аккаунт выберет всю суточную норму.
PER_VISIT = int(os.getenv("SUB_PER_VISIT", "8"))
DMIN = float(os.getenv("SUB_DELAY_MIN", "40"))
DMAX = float(os.getenv("SUB_DELAY_MAX", "90"))
# Пауза между кругами — в это время прокси свободны для realtime.
ROUND_GAP = int(os.getenv("SUB_ROUND_GAP", "180"))
MAX_CHANNELS = 480            # предел Telegram ~500, с запасом
ЛОГ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subscribe_run.log")
QUOTA = "gramgpt:subrun:{acc}:{day}"


def журнал(s):
    line = "[%s] %s" % (datetime.now().strftime("%d.%m %H:%M:%S"), s)
    print(line, flush=True)
    with open(ЛОГ, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def quota_left(acc_id):
    from utils.redis_pool import get_redis
    try:
        used = int(get_redis().get(QUOTA.format(acc=acc_id, day=date.today().isoformat())) or 0)
    except Exception:
        used = 0
    return max(0, PER_DAY - used)


def quota_use(acc_id, n=1):
    from utils.redis_pool import get_redis
    try:
        r = get_redis(); k = QUOTA.format(acc=acc_id, day=date.today().isoformat())
        r.incrby(k, n); r.expire(k, 172800)
    except Exception:
        pass


async def account_pass(acc_id, proxy, темп):
    from sqlalchemy import select, text as t
    from sqlalchemy.orm import joinedload
    from models.account import TelegramAccount
    from utils.db_pool import async_session as S
    from utils.telegram import make_telethon_client
    from utils.account_lock import acquire_account_lock, release_account_lock
    from utils.ip_throttle import acquire_ip_lock, refresh_ip_lock, release_ip_lock
    from utils.account_health import record_error, flood_remaining
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.errors import FloodWaitError

    left = min(quota_left(acc_id), PER_VISIT)
    if left <= 0 or flood_remaining(acc_id) > 0:
        return 0, темп
    async with S() as db:
        chans = [r[0] for r in (await db.execute(t("""
            SELECT channel_username FROM campaign_channel_assignments
            WHERE campaign_id=:c AND account_id=:a AND status='pending'
            ORDER BY random() LIMIT :n"""), {"c": CAMP, "a": acc_id, "n": left})).all()]
        if not chans:
            return 0, темп
        acc = (await db.execute(select(TelegramAccount).options(joinedload(TelegramAccount.api_app))
               .where(TelegramAccount.id == acc_id))).scalar_one()

    if not acquire_ip_lock(proxy, owner=acc_id):
        return 0, темп
    if not acquire_account_lock(acc_id, ttl=3600):
        release_ip_lock(proxy); return 0, темп
    client = make_telethon_client(acc, proxy)
    if client is None:
        release_account_lock(acc_id); release_ip_lock(proxy); return 0, темп
    done = 0
    try:
        await asyncio.wait_for(client.connect(), 45)
        if not await asyncio.wait_for(client.is_user_authorized(), 25):
            журнал("acc#%s: сессия не авторизована" % acc_id); return 0, темп
        for i, ch in enumerate(chans):
            try:
                ent = await asyncio.wait_for(client.get_entity(ch), 40)
                await asyncio.wait_for(client(JoinChannelRequest(ent)), 40)
                статус = "joined"
            except FloodWaitError as fw:
                record_error(acc_id, "", fw)
                темп = min(3.0, темп * 1.5)
                журнал("acc#%s FLOOD_WAIT %sс — выхожу, адрес %s медленнее ×%.1f"
                        % (acc_id, fw.seconds, proxy.host, темп))
                break
            except Exception as e:
                en = type(e).__name__
                if "CHANNELS_TOO_MUCH" in str(e) or en == "ChannelsTooMuchError":
                    журнал("acc#%s: предел каналов — стоп" % acc_id); break
                # «уже участник» — это тоже успех
                статус = "joined" if ("already" in str(e).lower() or "USER_ALREADY" in str(e)) else "failed"
            async with S() as db:
                await db.execute(t("""UPDATE campaign_channel_assignments
                    SET status=:s, joined_at=now() WHERE campaign_id=:c AND account_id=:a
                      AND channel_username=:ch AND status='pending'"""),
                    {"s": статус, "c": CAMP, "a": acc_id, "ch": ch})
                await db.commit()
            if статус == "joined":
                done += 1
            quota_use(acc_id, 1)
            if (i + 1) % 10 == 0:
                refresh_ip_lock(proxy)
            await asyncio.sleep(random.uniform(DMIN, DMAX) * темп)
    except Exception as e:
        журнал("acc#%s сбой %s" % (acc_id, type(e).__name__))
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), 10)
        except Exception:
            pass
        release_account_lock(acc_id); release_ip_lock(proxy)
        # Снимаем и замок-владельца адреса: иначе он висит 600с и всё это
        # время диспетчер realtime считает прокси занятым подписчиком и
        # откладывает отправку комментариев. Подписчик идёт по прокси
        # строго по очереди, так что освобождать адрес сразу безопасно.
        try:
            from utils.redis_pool import get_redis
            get_redis().delete("gramgpt:ip_owner:%s:%s" % (proxy.host, proxy.port))
        except Exception:
            pass
    return done, темп


async def proxy_lane(proxy, acc_ids):
    темп = 1.0
    total = 0
    for acc_id in acc_ids:
        d, темп = await account_pass(acc_id, proxy, темп)
        total += d
        await asyncio.sleep(random.uniform(5, 15))
    return total


async def one_round():
    from sqlalchemy import text as t
    from models.proxy import Proxy
    from sqlalchemy import select
    from utils.db_pool import async_session as S
    async with S() as db:
        rows = (await db.execute(t("""
            SELECT a.proxy_id, a.id FROM accounts a
            WHERE a.id IN (SELECT DISTINCT account_id FROM campaign_channel_assignments
                           WHERE campaign_id=:c AND status='pending')
            ORDER BY a.proxy_id, a.id"""), {"c": CAMP})).all()
        prox = {p.id: p for p in (await db.execute(select(Proxy))).scalars().all()}
        осталось = (await db.execute(t("""SELECT count(*) FROM campaign_channel_assignments
            WHERE campaign_id=:c AND status='pending'"""), {"c": CAMP})).scalar()
        готово = (await db.execute(t("""SELECT count(*) FROM campaign_channel_assignments
            WHERE campaign_id=:c AND status='joined'"""), {"c": CAMP})).scalar()
    if not rows:
        return 0, осталось
    lanes = defaultdict(list)
    for pid, aid in rows:
        lanes[pid].append(aid)
    журнал("раунд: подписано %d, осталось %d, аккаунтов в работе %d на %d прокси"
            % (готово, осталось, len(set(a for _, a in rows)), len(lanes)))
    res = await asyncio.gather(*(proxy_lane(prox[pid], accs) for pid, accs in lanes.items() if pid in prox))
    return sum(res), осталось


async def main():
    журнал("══ СТАРТ подписки кампании %d: %d/сутки на аккаунт, пачка %d, пауза между кругами %dс ══"
            % (CAMP, PER_DAY, PER_VISIT, ROUND_GAP))
    while True:
        подписано, осталось = await one_round()
        журнал("за круг подписано %d, осталось pending %d" % (подписано, осталось))
        if осталось == 0:
            журнал("ГОТОВО: все подписаны"); return
        if подписано == 0:
            # Никто ничего не вступил — значит суточные квоты выбраны.
            завтра = (datetime.now() + timedelta(days=1)).replace(hour=0, minute=10, second=0)
            ждать = max(600, (завтра - datetime.now()).total_seconds())
            журнал("суточные квоты выбраны — жду до %s (realtime свободно пишет)"
                    % завтра.strftime("%d.%m %H:%M"))
            await asyncio.sleep(ждать)
        else:
            # Пауза между кругами: прокси свободны, realtime в это время шлёт.
            await asyncio.sleep(ROUND_GAP)


asyncio.run(main())
